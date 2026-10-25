"""Cooperative catalog transactions with a recoverable multi-file journal.

Publication is deliberately serialized by one short kernel lock. Research and
staging remain concurrent; snapshots take its shared side. This keeps global
ID/graph validation coherent without a database or a lock-upgrade protocol.
Locks are advisory: every canonical writer must use this module. Kernel locks
are released on process death; lock files are permanent and must not be deleted.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
import warnings

import storage_codec as codec
import storage_policy


class TransactionError(ValueError):
    pass


def digest(data):
    return None if data is None else hashlib.sha256(data).hexdigest()


def read_bytes(path):
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None


def safe_path(hub, relative):
    hub = Path(hub).resolve()
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise TransactionError(f'Invalid relative path: {relative}')
    path = hub / rel
    if not path.resolve().is_relative_to(hub) or path.is_symlink():
        raise TransactionError(f'Path escapes hub or is a symlink: {relative}')
    return path


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdir(path):
    """Persist new directory entries too, not just files within them."""
    path = Path(path)
    if path.exists():
        return
    _mkdir(path.parent)
    path.mkdir(exist_ok=True)
    _sync_dir(path.parent)


def _replace(path, data):
    """Internal primitive. Caller owns the publication lock."""
    path = Path(path)
    _mkdir(path.parent)
    if data is None:
        path.unlink(missing_ok=True)
        _sync_dir(path.parent)
        return
    fd, tmp = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        _sync_dir(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def _json_write(path, value):
    _replace(path, (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode())


def pending(hub):
    directory = Path(hub) / '.hub/transactions'
    found = []
    for path in sorted(directory.glob('*/journal.json')):
        item = json.loads(path.read_bytes())
        if item['state'] == 'pending':
            found.append(item['id'])
    return found


@contextlib.contextmanager
def _file_lock(path, exclusive):
    # Existing shared-lock readers need no filesystem write permission. A new
    # hub still bootstraps the permanent lock file; never skip its gate merely
    # because a read-only checkout has not been initialized for transactions.
    if not exclusive and path.exists():
        stream = path.open('rb')
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open('a+b')
    with stream:
        try:
            fcntl.flock(stream, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TransactionError(f'Hub is busy: {path.name}; retry after the current transaction') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


@contextlib.contextmanager
def locked(hub, scopes=(), *, exclusive=False, allow_pending=False):
    """Gate first, then sorted scope locks; no nested acquisitions/upgrades."""
    hub = Path(hub)
    scopes = sorted(set(scopes))
    if any(not re.fullmatch(r'[a-z0-9][a-z0-9-]*', s) for s in scopes):
        raise TransactionError('Invalid lock scope')
    with contextlib.ExitStack() as stack:
        stack.enter_context(_file_lock(hub / '.hub/locks/publication.lock', exclusive))
        for scope in scopes:
            stack.enter_context(_file_lock(hub / '.hub/locks' / f'{scope}.lock', exclusive))
        if not allow_pending:
            unfinished = pending(hub)
            if unfinished:
                raise TransactionError(f'Interrupted transaction {unfinished}; run hub.py recover after inspection')
        yield


def snapshot(hub, paths):
    with locked(hub):
        return {name: read_bytes(safe_path(hub, name)) for name in paths}


def blob_path(hub, ref):
    name = codec.filename(ref)
    return safe_path(hub, '.hub/transactions/blobs/' + name[:2] + '/' + name)


def read_payload(hub, directory, journal, item, direction):
    wanted = item[direction]
    if wanted is None:
        if journal.get('schema_version', 1) == 2 and item['payloads'][direction] is not None:
            raise TransactionError('Null payload has a blob reference')
        return None
    version = journal.get('schema_version', 1)
    if version == 1:
        if type(item.get('index')) is not int or item['index'] < 0:
            raise TransactionError('Invalid journal file index')
        data = (directory / f'{item["index"]}.{direction}').read_bytes()
    elif version == 2:
        ref = item['payloads'][direction]
        if ref['sha256'] != wanted:
            raise TransactionError('Journal payload identity mismatch')
        data = codec.unpack(blob_path(hub, ref).read_bytes(), ref)
    else:
        raise TransactionError('Unsupported transaction journal version')
    if digest(data) != wanted:
        raise TransactionError('Recovery snapshot checksum mismatch')
    return data


def _store_blob(hub, ref, packed):
    """Caller holds the gate. Verify reused bytes and preserve their exact codec."""
    path = blob_path(hub, ref)
    existing = read_bytes(path)
    if existing is not None:
        actual = dict(ref, stored_sha256=digest(existing), stored_bytes=len(existing))
        codec.unpack(existing, actual)
        return actual
    _replace(path, packed)
    codec.unpack(path.read_bytes(), ref)
    return ref


def prepare_blob(hub, raw):
    """Reuse readable existing content even when the preferred codec changed."""
    wanted = digest(raw)
    for name in dict.fromkeys([codec.preferred_codec(), *codec.EXTENSIONS]):
        prototype = dict(codec=name, sha256=wanted, bytes=len(raw),
                         stored_sha256='0' * 64, stored_bytes=0)
        packed = read_bytes(blob_path(hub, prototype))
        if packed is not None:
            ref = codec.reference(raw, packed, name)
            codec.unpack(packed, ref)
            return ref, packed
    return codec.pack(raw)


def _inject(phase, requested):
    if phase == requested:
        raise TransactionError(f'Injected interruption at {phase}')


def apply_batch(hub, changes, expected, *, scopes=('hub',), owner='operator',
                validate=None, fail_after=None, fail_phase=None):
    """Commit bytes/str/None (deletion), comparing every declared base hash.

    validate(overrides) executes under the publication lock against current
    state plus this complete prospective batch, before publishing anything.
    fail_after is solely a crash-injection hook for isolated fixture tests.
    """
    hub = Path(hub).resolve()
    if set(changes) - set(expected):
        raise TransactionError('Every changed path needs an expected hash (null for a new file)')
    payload = {n: v.encode() if isinstance(v, str) else v for n, v in changes.items()}
    for name in set(payload) | set(expected):
        safe_path(hub, name)
        if Path(name).parts[0] in {'archives', '.hub'}:
            raise TransactionError(f'Batch cannot edit protected evidence/internal state: {name}')
    # Compress outside the exclusive gate. Each observed before image must still
    # match its expected hash under the gate; contention never rebases a batch.
    before_images = {n: read_bytes(safe_path(hub, n)) for n in payload}
    for name, before in before_images.items():
        # Canonical checkpoint objects are immutable. New versions get new names.
        if re.fullmatch(r'(?:projects/[a-z0-9][a-z0-9-]*/)?catalog/frontier/[a-f0-9]{64}\.(?:zst|xz)', name):
            if before is not None and payload[name] != before:
                raise TransactionError('Canonical checkpoint bodies cannot be replaced or deleted')
        if digest(before) != expected[name]:
            raise TransactionError(f'{name}: changed since read; rebase the batch')
    payload = {n: b for n, b in payload.items() if b != before_images[n]}
    staged = {}
    for name, after in payload.items():
        for data in (before_images[name], after):
            if data is not None and (key := digest(data)) not in staged:
                staged[key] = prepare_blob(hub, data)
    with locked(hub, scopes, exclusive=True):
        for name, wanted in expected.items():
            if digest(read_bytes(safe_path(hub, name))) != wanted:
                raise TransactionError(f'{name}: changed since read; rebase the batch')
        if validate:
            validate(payload)
        if not payload:
            return None
        policy = storage_policy.load(hub, payload)
        size = sum(len(b) for b in payload.values() if b is not None)
        if size > policy['transaction_warning_bytes']:
            warnings.warn(f'Large transaction: {size} uncompressed bytes', RuntimeWarning)
        # Account for new blobs, live growth, one replacement/rollback temporary,
        # and journal metadata. The reserve is not a guarantee against other apps.
        missing = sum(len(packed) for ref, packed in staged.values() if not blob_path(hub, ref).exists())
        growth = sum(max(0, len(after or b'') - len(before_images[n] or b'')) for n, after in payload.items())
        temporary = max(len(b or b'') for n, a in payload.items() for b in (a, before_images[n]))
        try:
            storage_policy.require_space(hub, missing + growth + temporary + 262144, overrides=payload)
        except ValueError as exc:
            raise TransactionError(str(exc)) from exc
        refs = {key: _store_blob(hub, ref, packed) for key, (ref, packed) in staged.items()}
        _inject('after_blobs', fail_phase)
        txid = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:10]
        directory = hub / '.hub/transactions' / txid
        _mkdir(directory)
        files = []
        for index, (name, after) in enumerate(sorted(payload.items())):
            before = before_images[name]
            files.append(dict(path=name, before=digest(before), after=digest(after), index=index,
                              payloads={label: None if raw is None else refs[digest(raw)]
                                        for label, raw in [('before', before), ('after', after)]}))
        journal = dict(schema_version=2, id=txid, owner=owner, scopes=sorted(set(scopes)), state='pending',
                       created_at=datetime.now(timezone.utc).isoformat(), files=files)
        _json_write(directory / 'journal.json', journal)
        _inject('after_pending', fail_phase)
        if fail_after == 0:
            raise TransactionError('Injected interruption before publication')
        for index, item in enumerate(files, 1):
            _replace(safe_path(hub, item['path']), payload[item['path']])
            if fail_after == index:
                raise TransactionError(f'Injected interruption after file {index}')
        _inject('before_commit', fail_phase)
        journal['state'] = 'committed'
        journal['completed_at'] = datetime.now(timezone.utc).isoformat()
        _json_write(directory / 'journal.json', journal)
        return txid


def recover(hub, txid, direction='rollback'):
    """Recover only an interrupted batch; never overwrite later/foreign bytes."""
    if not re.fullmatch(r'[A-Za-z0-9-]+', txid) or direction not in {'rollback', 'finish'}:
        raise TransactionError('Invalid recovery request')
    hub = Path(hub).resolve()
    with locked(hub, exclusive=True, allow_pending=True):
        directory = hub / '.hub/transactions' / txid
        journal = json.loads((directory / 'journal.json').read_bytes())
        if journal['state'] != 'pending':
            raise TransactionError('Only a pending transaction can be recovered')
        target = 'before' if direction == 'rollback' else 'after'
        restored = {}
        for item in journal['files']:
            path = safe_path(hub, item['path'])
            if digest(read_bytes(path)) not in {item['before'], item['after']}:
                raise TransactionError(f'{item["path"]}: later/foreign edit; recovery refused')
            restored[item['path']] = read_payload(hub, directory, journal, item, target)
        growth = sum(max(0, len(data or b'') - len(read_bytes(safe_path(hub, name)) or b''))
                     for name, data in restored.items())
        temporary = max((len(data or b'') for data in restored.values()), default=0)
        # Recovery may use the normal reserve but still needs room for atomic files.
        storage_policy.require_space(hub, growth + temporary + 65536, reserve=0)
        for name, data in restored.items():
            _replace(safe_path(hub, name), data)
        journal['state'] = 'rolled-back' if direction == 'rollback' else 'committed'
        journal['recovered_at'] = datetime.now(timezone.utc).isoformat()
        _json_write(directory / 'journal.json', journal)
        return journal['state']
