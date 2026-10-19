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


def _replace(path, data):
    """Internal primitive. Caller owns the publication lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
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


def apply_batch(hub, changes, expected, *, scopes=('hub',), owner='operator',
                validate=None, fail_after=None):
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
    with locked(hub, scopes, exclusive=True):
        for name, wanted in expected.items():
            if digest(read_bytes(safe_path(hub, name))) != wanted:
                raise TransactionError(f'{name}: changed since read; rebase the batch')
        if validate:
            validate(payload)
        payload = {n: b for n, b in payload.items() if b != read_bytes(safe_path(hub, n))}
        txid = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:10]
        directory = hub / '.hub/transactions' / txid
        directory.mkdir(parents=True)
        files = []
        for index, (name, after) in enumerate(sorted(payload.items())):
            before = read_bytes(safe_path(hub, name))
            for suffix, data in [('before', before), ('after', after)]:
                if data is not None:
                    _replace(directory / f'{index}.{suffix}', data)
            files.append(dict(path=name, before=digest(before), after=digest(after), index=index))
        journal = dict(id=txid, owner=owner, scopes=sorted(set(scopes)), state='pending',
                       created_at=datetime.now(timezone.utc).isoformat(), files=files)
        _json_write(directory / 'journal.json', journal)
        if fail_after == 0:
            raise TransactionError('Injected interruption before publication')
        for index, item in enumerate(files, 1):
            _replace(safe_path(hub, item['path']), payload[item['path']])
            if fail_after == index:
                raise TransactionError(f'Injected interruption after file {index}')
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
            data = None if item[target] is None else (directory / f'{item["index"]}.{target}').read_bytes()
            if digest(data) != item[target]:
                raise TransactionError('Recovery snapshot checksum mismatch')
            restored[item['path']] = data
        for name, data in restored.items():
            _replace(safe_path(hub, name), data)
        journal['state'] = 'rolled-back' if direction == 'rollback' else 'committed'
        journal['recovered_at'] = datetime.now(timezone.utc).isoformat()
        _json_write(directory / 'journal.json', journal)
        return journal['state']
