"""Storage accounting and explicit, lossless maintenance for ResearchHub."""
from datetime import datetime, timezone, timedelta
import copy
import json
from pathlib import Path
import re
import shutil
import sys

import storage_codec as codec
import storage_policy as policy
import transactions as tx


def usage(path):
    path = Path(path)
    paths = [path] if path.is_file() else path.rglob('*') if path.exists() else []
    logical = allocated = files = 0
    for p in paths:
        if p.is_file() and not p.is_symlink():
            try:
                stat = p.stat()
            except FileNotFoundError:
                continue
            logical += stat.st_size
            allocated += getattr(stat, 'st_blocks', (stat.st_size + 511) // 512) * 512
            files += 1
    return dict(logical_bytes=logical, allocated_bytes=allocated, files=files)


def journals(hub):
    for path in sorted((Path(hub) / '.hub/transactions').glob('*/journal.json')):
        tx.safe_path(hub, path.relative_to(hub).as_posix())
        raw = path.read_bytes()
        data = json.loads(raw)
        if data.get('id') != path.parent.name or not re.fullmatch('[A-Za-z0-9-]+', data['id']):
            raise ValueError('Invalid journal identity')
        if data.get('schema_version', 1) not in (1, 2) or data.get('state') not in ('pending', 'committed', 'rolled-back'):
            raise ValueError('Unsupported journal format/state')
        yield path, raw, data


def status(hub):
    hub = Path(hub).resolve()
    limits = policy.load(hub)
    components = {name: usage(hub / name) for name in
                  ('.hub/transactions', '.hub/transactions/blobs', '.hub/recall/generations', '.hub/recall/cache')}
    catalogs = []
    for path in [hub / 'catalog/knowledge.json', *hub.glob('projects/*/catalog/knowledge.json')]:
        if path.exists():
            catalogs.append(dict(path=path.relative_to(hub).as_posix(), **usage(path)))
    bodies = [hub / 'catalog/frontier', *hub.glob('projects/*/catalog/frontier')]
    states, versions, warnings = {}, {}, []
    total_refs = logical_history = 0
    for _, _, journal in journals(hub):
        state, version = journal['state'], str(journal.get('schema_version', 1))
        states[state] = states.get(state, 0) + 1
        versions[version] = versions.get(version, 0) + 1
        total_refs += sum(item[side] is not None for item in journal['files'] for side in ('before', 'after'))
        if version == '2':
            logical_history += sum(ref['bytes'] for item in journal['files'] for ref in item['payloads'].values() if ref)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()
    # Actual new blob files, including an explicit historical migration. Metadata
    # and old raw payload growth are separately visible in component sizes.
    recent = sum(p.stat().st_size for p in (hub / '.hub/transactions/blobs').rglob('*')
                 if p.is_file() and not p.is_symlink() and p.stat().st_mtime >= cutoff)
    for row in catalogs:
        if row['logical_bytes'] > limits['catalog_warning_bytes']:
            warnings.append('Oversized live catalog: ' + row['path'])
    if components['.hub/transactions/blobs']['logical_bytes'] > limits['journal_warning_bytes']:
        warnings.append('Compressed journal store exceeds warning threshold; history is retained')
    if recent > limits['daily_growth_warning_bytes']:
        warnings.append('New journal blob bytes in 24 hours exceed threshold (includes migrations)')
    for component, field in (('.hub/recall/generations', 'generation_budget_bytes'), ('.hub/recall/cache', 'cache_budget_bytes')):
        if components[component]['logical_bytes'] > limits[field]:
            warnings.append(component + ' exceeds its budget; run collect-cache (active readers may pin generations)')
    free = shutil.disk_usage(hub).free
    if free < limits['free_reserve_bytes']:
        warnings.append('Free disk space is below publication reserve')
    return dict(observed_at=datetime.now(timezone.utc).isoformat(), components=components,
                catalogs=catalogs, checkpoint_bodies=dict(logical_bytes=sum(usage(p)['logical_bytes'] for p in bodies)),
                journals=dict(states=states, versions=versions, payload_references=total_refs,
                              decoded_v2_history_bytes=logical_history, new_blob_bytes_24h=recent),
                free_disk_bytes=free, limits=limits, warnings=warnings,
                scope='Live read-only observations; allocated bytes are filesystem-reported, not exclusive APFS clone ownership')


def _token(path):
    s = path.stat()
    return (s.st_ino, s.st_size, s.st_mtime_ns)


def _legacy_path(hub, directory, item, side):
    if type(item.get('index')) is not int or item['index'] < 0:
        raise ValueError('Invalid journal file index')
    return tx.safe_path(hub, (directory / f'{item["index"]}.{side}').relative_to(hub).as_posix())


def compact_journal(hub, apply=False, progress=None, fail_phase=None):
    """Compress outside the gate; switch one verified journal at a time.

    Dry run verifies every legacy payload and measures unique compressed bytes
    without writing a blob. Applying is resumable, including a crash after the
    descriptor switch and before raw-copy removal. Nothing committed is pruned.
    """
    hub = Path(hub).resolve()
    seen, converted, removed, raw_bytes, refs_count, skipped = {}, 0, 0, 0, 0, []
    for number, (jp, original, journal) in enumerate(journals(hub), 1):
        if journal['state'] == 'pending':
            skipped.append(journal['id'])
            continue
        version = journal.get('schema_version', 1)
        changed = copy.deepcopy(journal)
        staged, retire = {}, []
        for item in changed['files']:
            item.setdefault('payloads', {})
            for side in ('before', 'after'):
                wanted = item[side]
                if wanted is None:
                    item['payloads'][side] = None
                    continue
                legacy = _legacy_path(hub, jp.parent, item, side)
                if version == 1:
                    before_token = _token(legacy)
                    raw = legacy.read_bytes()
                    if tx.digest(raw) != wanted or _token(legacy) != before_token:
                        raise ValueError(f'Legacy payload checksum/identity mismatch: {legacy}')
                    if wanted in seen:
                        ref = seen[wanted]
                        packed = None
                        if apply and not tx.blob_path(hub, ref).exists():
                            ref, packed = codec.pack(raw, ref['codec'])
                    else:
                        ref, packed = tx.prepare_blob(hub, raw)
                    # No raw payload is removed until its complete decoded bytes
                    # have passed verification and its descriptor is durable.
                    retire.append((legacy, before_token, len(raw)))
                    raw_bytes += len(raw)
                    del raw
                else:
                    ref = item['payloads'][side]
                    if ref['sha256'] != wanted:
                        raise ValueError('Journal payload identity mismatch')
                    packed = tx.blob_path(hub, ref).read_bytes()
                    codec.unpack(packed, ref)
                    if legacy.exists():
                        before_token = _token(legacy)
                        raw = legacy.read_bytes()
                        if tx.digest(raw) != wanted or _token(legacy) != before_token:
                            raise ValueError('Unexpected raw bytes retained after journal conversion')
                        retire.append((legacy, before_token, len(raw)))
                        raw_bytes += len(raw)
                        del raw
                seen[wanted] = ref
                item['payloads'][side] = ref
                staged[wanted] = (ref, packed)
                refs_count += 1
        changed['schema_version'] = 2
        if version == 1:
            changed['storage_migrated_at'] = datetime.now(timezone.utc).isoformat()
        if apply and (version == 1 or retire):
            # Revalidate the metadata and raw file identities after staging.
            with tx.locked(hub, exclusive=True):
                if jp.read_bytes() != original:
                    raise tx.TransactionError('Journal changed during staging; rerun compaction')
                if any(_token(path) != token for path, token, _ in retire):
                    raise tx.TransactionError('Raw journal payload changed during staging')
                missing = sum(len(packed) for ref, packed in staged.values()
                              if packed is not None and not tx.blob_path(hub, ref).exists())
                # Maintenance can recover space within the normal reserve, but
                # must have room for every newly durable blob before switching.
                policy.require_space(hub, missing + 262144, reserve=0)
                stored = {}
                for key, (ref, packed) in staged.items():
                    if packed is None:
                        packed = tx.blob_path(hub, ref).read_bytes()
                    stored[key] = tx._store_blob(hub, ref, packed)
                for item in changed['files']:
                    for side in ('before', 'after'):
                        item['payloads'][side] = None if item[side] is None else stored[item[side]]
                tx._inject('before_journal_switch', fail_phase)
                tx._json_write(jp, changed)
                tx._inject('after_journal_switch', fail_phase)
                for path, _, count in retire:
                    path.unlink()
                    removed += count
                tx._sync_dir(jp.parent)
            if version == 1:
                converted += 1
        elif version == 1:
            converted += 1
        if progress and number % 50 == 0:
            progress(dict(journals=number, converted=converted, unique_payloads=len(seen), raw_bytes=raw_bytes))
    return dict(applied=apply, converted_journals=converted, unique_payloads=len(seen),
                verified_payload_references=refs_count, legacy_bytes=raw_bytes, removed_raw_bytes=removed,
                unique_compressed_bytes=sum(ref['stored_bytes'] for ref in seen.values()),
                skipped_pending=skipped, history_retention='All historical payloads retained losslessly')


def verify_journal(hub):
    hub = Path(hub).resolve()
    seen, references, count = set(), 0, 0
    for jp, _, journal in journals(hub):
        count += 1
        for item in journal['files']:
            for side in ('before', 'after'):
                if item[side] is None:
                    tx.read_payload(hub, jp.parent, journal, item, side)
                    continue
                references += 1
                if journal.get('schema_version', 1) == 2:
                    ref = item['payloads'][side]
                    key = json.dumps(ref, sort_keys=True)
                    if ref['sha256'] != item[side]:
                        raise ValueError('Journal payload identity mismatch')
                    if key in seen:
                        continue
                    seen.add(key)
                tx.read_payload(hub, jp.parent, journal, item, side)
    return dict(journals=count, verified_payload_references=references, unique_v2_payloads=len(seen), valid=True)


def collect_journal_orphans(hub, apply=False):
    """Only unreferenced compressed blobs; includes pending journals as GC roots."""
    hub = Path(hub).resolve()
    with tx.locked(hub, exclusive=True, allow_pending=True):
        referenced = set()
        for _, _, journal in journals(hub):
            if journal.get('schema_version', 1) == 2:
                for item in journal['files']:
                    for ref in item['payloads'].values():
                        if ref is not None:
                            referenced.add(tx.blob_path(hub, ref))
        candidates = [p for p in (hub / '.hub/transactions/blobs').glob('*/*')
                      if p.is_file() and not p.is_symlink() and p not in referenced
                      and re.fullmatch('[a-f0-9]{64}\\.(zst|xz)', p.name)]
        count = sum(p.stat().st_size for p in candidates)
        if apply:
            for path in candidates:
                path.unlink()
                tx._sync_dir(path.parent)
        return dict(applied=apply, unreferenced_blobs=len(candidates), bytes=count,
                    note='Referenced committed and pending history is never collected')


def migrate_checkpoints(hub, project=None, apply=False):
    import catalog_store as cs
    import hub_operations as ops
    from recall import checkpoint_store as bodies
    hub = Path(hub).resolve()
    initial = cs.load_catalog(hub)
    owners = [project] if project is not None else [*initial.cfg['projects'], 'hub']
    if set(owners) - set(initial.knowledge):
        raise ValueError('Unknown checkpoint owner')
    result = []
    for owner in owners:
        catalog = cs.load_catalog(hub)
        envelope = copy.deepcopy(catalog.knowledge[owner])
        changes, expected, converted = {}, dict(catalog.file_hashes), []
        for i, entry in enumerate(envelope['checkpoints']):
            if entry.get('purpose') != bodies.PURPOSE or 'body' in entry:
                continue
            compact, additions = bodies.prepare(hub, owner, entry)
            compact['revision'] = entry['revision'] + 1
            envelope['checkpoints'][i] = compact
            converted.append(entry['id'])
            for name, (raw, digest) in additions.items():
                changes[name], expected[name] = raw, digest
        if not converted:
            continue
        envelope['schema_version'] = 2
        relative = cs.knowledge_path(hub, owner).relative_to(hub).as_posix()
        changes[relative] = cs.serialize(envelope)
        receipt = dict(owner=owner, checkpoints=converted, old_catalog_bytes=(hub / relative).stat().st_size,
                       new_catalog_bytes=len(changes[relative].encode()),
                       body_bytes=sum(len(raw) for name, raw in changes.items() if name != relative))
        if apply:
            receipt['transaction'] = ops.apply(hub, dict(changes=changes, expected=expected), [owner], 'storage-checkpoint-migration')['transaction']
            # Compare every logical checkpoint, including diagnostics and metadata.
            saved = cs.load_catalog(hub).knowledge[owner]
            prior = {e['id']: e for e in catalog.knowledge[owner]['checkpoints']}
            for entry in saved['checkpoints']:
                if entry['id'] in converted:
                    expanded = bodies.hydrate(hub, owner, entry)
                    expanded['revision'] -= 1
                    if expanded != prior[entry['id']]:
                        raise ValueError('Post-publication checkpoint reconstruction mismatch')
        result.append(receipt)
    return dict(applied=apply, projects=result, note='All identities and historical bodies retained; revisions advance once')


def configure(sub):
    parser = sub.add_parser('storage', help='Storage reporting and explicit lossless maintenance')
    actions = parser.add_subparsers(dest='storage_action', required=True)
    actions.add_parser('status')
    actions.add_parser('verify-journal')
    for name in ('compact-journal', 'migrate-checkpoints', 'collect-cache', 'collect-orphans'):
        cmd = actions.add_parser(name)
        cmd.add_argument('--apply', action='store_true', help='Apply the maintenance operation; otherwise inspect only')
        if name == 'migrate-checkpoints':
            cmd.add_argument('--project')


def dispatch(hub, args):
    name = args.storage_action
    if name == 'status':
        result = status(hub)
    elif name == 'verify-journal':
        result = verify_journal(hub)
    elif name == 'compact-journal':
        result = compact_journal(hub, args.apply, lambda p: print(json.dumps(p), file=sys.stderr, flush=True))
    elif name == 'migrate-checkpoints':
        result = migrate_checkpoints(hub, args.project, args.apply)
    elif name == 'collect-orphans':
        result = collect_journal_orphans(hub, args.apply)
    elif args.apply:
        from recall import index
        result = index.collect(hub)
    else:
        result = dict(applied=False, current=status(hub)['components'], note='Use --apply to collect disposable caches; active readers are protected')
    print(json.dumps(result, indent=2))
    return 0
