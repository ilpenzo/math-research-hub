"""Durable same-service conversation checkpoints and fast local recovery.

The current request always governs. Recovery is read-only and never binds a new
thread, starts work, restores goals, or calls a model/index/transcript scanner.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

import catalog_store as cs
import transactions as tx
from . import knowledge, native

SESSION_SCHEMA = 1
IDENTITY_ID = 'hub:workspace-identity'
SESSION_FIELDS = ('user_direction', 'constraints', 'selected_direction', 'decisions',
                  'results', 'unresolved', 'next_action', 'references', 'locators',
                  'captured_through', 'inherited_from', 'fork_parent', 'context_key',
                  'unbound_session', 'native_thread')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def workspace_identity(hub, catalog=None):
    hub = Path(hub).resolve()
    catalog = catalog or cs.load_catalog(hub)
    row = next((r for r in catalog.knowledge['hub']['checkpoints'] if r.get('id') == IDENTITY_ID), None)
    if not row or not isinstance(row.get('workspace_id'), str) or not isinstance(row.get('registered_root'), str):
        return dict(state='unregistered', root=str(hub), workspace_id=None,
                    reason='Publish hub:workspace-identity with workspace_id and registered_root before saving sessions.')
    matches = str(hub) == str(Path(row['registered_root']).expanduser().resolve())
    return dict(state='current' if matches else 'root-mismatch', root=str(hub),
                workspace_id=row['workspace_id'], registered_root=row['registered_root'],
                revision=row['revision'], prior_roots=row.get('prior_roots', []),
                reason=None if matches else 'Independent copy or unregistered move: prior sessions are isolated. Explicitly register this root, preserving ID only for an intentional move.')


def _project(catalog, hub, project, cwd, query):
    if project:
        if project not in set(catalog.cfg['projects']) | {'hub'}:
            raise ValueError(f'Unknown project {project}')
        return project, 'explicit project'
    named = [name for name in catalog.cfg['projects'] if name in query]
    if len(named) == 1:
        return named[0], 'project named in current request'
    cwd = Path(cwd or Path.cwd()).resolve()
    for name in catalog.cfg['projects']:
        try:
            cwd.relative_to(Path(hub).resolve() / 'projects' / name)
            return name, 'project working directory'
        except ValueError:
            pass
    return None, None


def _rows(catalog):
    for envelope in catalog.knowledge.values():
        yield from envelope['sessions']


def _inputs(catalog, refs):
    entries = dict(catalog.by_id())
    entries.update({k: v for k, v in catalog.knowledge_by_id().items() if v.get('kind') in knowledge.KINDS})
    incoming = {}
    for row in catalog.entries():
        for target in row.get('supersedes', []):
            incoming.setdefault(target, []).append(row['id'])
    todo, seen, corrections, missing = list(refs), {}, {}, []
    while todo:
        key = todo.pop()
        if key in seen or key in missing:
            continue
        row = entries.get(key)
        if row is None:
            missing.append(key)
            continue
        seen[key] = _digest(row)
        todo += [x for field in cs.OUTGOING for x in row.get(field, [])]
        todo += [x['id'] for x in row.get('references', []) if isinstance(x, dict) and isinstance(x.get('id'), str)]
        for correcting in incoming.get(key, []):
            corrections[correcting] = entries[correcting]
            todo.append(correcting)
    return dict(hashes=seen, unresolved=sorted(missing)), list(corrections.values())


def _locator_state(hub, catalog, item):
    """Bounded small-file or selected byte-range checks; no archive sweep."""
    try:
        from catalog_validation import evidence_path
        path = evidence_path(Path(hub), catalog.cfg, item)
        info = path.stat()
        byte_range = item.get('byte_range')
        if byte_range is not None:
            if not isinstance(byte_range, list) or len(byte_range) != 2 or not all(type(n) is int for n in byte_range) or not 0 <= byte_range[0] <= byte_range[1] <= info.st_size or byte_range[1] - byte_range[0] > 524288:
                return dict(state='unresolved', reason='Invalid or oversized selected byte range.')
            with path.open('rb') as stream:
                stream.seek(byte_range[0])
                data = stream.read(byte_range[1] - byte_range[0])
        elif info.st_size <= 524288:
            data = path.read_bytes()
        else:
            return dict(state='unresolved', reason='File exceeds 512 KiB; supply a bounded byte_range for section freshness.',
                        size=info.st_size, mtime_ns=info.st_mtime_ns)
        return dict(state='checked', sha256=hashlib.sha256(data).hexdigest(), locator=item.get('locator'))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return dict(state='unresolved', reason=f'{type(exc).__name__}: {exc}')


def _locators(hub, catalog, locators):
    return {_digest(item): _locator_state(hub, catalog, item) for item in locators}


def _cache_path(hub, service):
    return Path(hub) / '.hub' / 'recall' / f'native-{service}.json'


def _cache(hub, service, identity):
    path = _cache_path(hub, service)
    try:
        if path.stat().st_size > 1048576:
            return {}
        value = json.loads(path.read_text())
        if value.get('workspace_id') == identity['workspace_id'] and value.get('root') == identity['root'] and value.get('storage_instance') == native.storage_instance(service):
            return value
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def validate_entry(entry):
    """Session schema validator, also callable by the kernel knowledge validator."""
    errors = []
    if entry.get('session_schema') != SESSION_SCHEMA:
        return ['unsupported session_schema']
    for field in ('workspace_id', 'workspace_root', 'storage_instance', 'context_key', 'user_direction', 'selected_direction', 'next_action', 'updated_at'):
        if not isinstance(entry.get(field), str):
            errors.append(f'{field} must be a string')
    if entry.get('service') not in native.SERVICES:
        errors.append('unsupported service')
    for field in ('constraints', 'decisions', 'results', 'unresolved', 'references', 'locators'):
        if not isinstance(entry.get(field), list):
            errors.append(f'{field} must be a list')
    for field in ('captured_through', 'canonical_inputs', 'locator_inputs', 'native_metadata'):
        if not isinstance(entry.get(field), dict):
            errors.append(f'{field} must be an object')
    if entry.get('native_thread') is not None and not isinstance(entry['native_thread'], str):
        errors.append('native_thread must be an actual string ID or null')
    if not entry.get('native_thread') and not isinstance(entry.get('unbound_session'), str):
        errors.append('unknown native thread requires an explicitly unbound session identity')
    if isinstance(entry.get('references'), list) and any(not isinstance(x, str) for x in entry['references']):
        errors.append('references must contain IDs as strings')
    if isinstance(entry.get('references'), list) and len(entry['references']) > 64:
        errors.append('references exceeds 64 IDs')
    for field in ('constraints', 'unresolved'):
        if isinstance(entry.get(field), list) and any(not isinstance(x, str) for x in entry[field]):
            errors.append(f'{field} must contain strings')
    for field in ('decisions', 'results'):
        if isinstance(entry.get(field), list) and any(not isinstance(x, dict) or not isinstance(x.get('text'), str) for x in entry[field]):
            errors.append(f'{field} must contain objects with text (and explicit provenance/status when known)')
    if isinstance(entry.get('results'), list) and any(isinstance(x, dict) and not isinstance(x.get('status'), str) for x in entry['results']):
        errors.append('each saved result requires its exact status')
    locators = entry.get('locators')
    if isinstance(locators, list) and (len(locators) > 16 or any(not isinstance(x, dict) or any(not isinstance(x.get(k), str) for k in ('root', 'path', 'locator')) for x in locators)):
        errors.append('locators must contain at most 16 root/path/locator objects')
    captured = entry.get('captured_through')
    if isinstance(captured, dict) and 'cursor' in captured and (type(captured['cursor']) is not int or captured['cursor'] < 0):
        errors.append('captured byte cursor must be a nonnegative integer')
    if isinstance(captured, dict) and 'scope' in captured and captured['scope'] not in ('selected-excerpts', 'parent-inspected-conversation'):
        errors.append('captured scope must be selected-excerpts or parent-inspected-conversation')
    inputs = entry.get('canonical_inputs')
    if isinstance(inputs, dict) and (not isinstance(inputs.get('hashes'), dict) or not isinstance(inputs.get('unresolved'), list)):
        errors.append('canonical_inputs requires hashes object and unresolved list')
    for field in ('fork_parent', 'inherited_from'):
        if entry.get(field) is not None and not isinstance(entry[field], str):
            errors.append(f'{field} must be an ID string or null')
    try:
        if len(json.dumps(entry, ensure_ascii=False).encode()) > 65536:
            errors.append('session checkpoint exceeds 64 KiB; reference existing evidence instead')
    except (TypeError, ValueError):
        errors.append('session checkpoint must contain JSON values')
    return errors


def checkpoint(hub, spec, service, owner):
    """Reconcile unrelated CAS collisions, never a changed checkpoint revision."""
    for attempt in range(5):
        try:
            return _checkpoint_once(hub, spec, service, owner)
        except tx.TransactionError as exc:
            if ('changed since read; rebase' not in str(exc) and 'Hub is busy:' not in str(exc)) or attempt == 4:
                raise
            time.sleep(.02 * (attempt + 1))


def _checkpoint_once(hub, spec, service, owner):
    """Publish a complete bounded checkpoint; updates require its exact revision."""
    if not isinstance(spec, dict):
        raise ValueError('checkpoint spec must be an object')
    native.service_home(service)  # Validate before any work.
    catalog = cs.load_catalog(hub)
    identity = workspace_identity(hub, catalog)
    if identity['state'] != 'current':
        raise ValueError(identity['reason'])
    project = spec.get('project')
    project, _ = _project(catalog, hub, project, spec.get('cwd'), '')
    if project is None:
        raise ValueError('Checkpoint needs an explicit project (hub for infrastructure).')
    existing = next((r for r in _rows(catalog) if r.get('id') == spec.get('id')), None)
    if existing and (existing.get('workspace_id'), existing.get('service'), existing.get('storage_instance')) != (identity['workspace_id'], service, native.storage_instance(service)):
        raise ValueError('Cannot transfer a checkpoint across workspace/service/storage identity.')
    thread = spec.get('native_thread', existing.get('native_thread') if existing else native.runtime_thread(service))
    context = spec.get('context_key', existing.get('context_key') if existing else project)
    unbound = spec.get('unbound_session', existing.get('unbound_session') if existing else None)
    if not thread:
        thread = None
        unbound = unbound or str(uuid.uuid4())
    key = [identity['workspace_id'], service, native.storage_instance(service), thread or 'unbound:' + unbound, context]
    entry_id = f'{project}:session:{service}:{_digest(key)[:24]}'
    if spec.get('id') and spec['id'] != entry_id:
        raise ValueError('Checkpoint ID does not match immutable conversation/context identity.')
    if existing and existing.get('native_thread') != thread:
        raise ValueError('Native thread identity is immutable; create a new checkpoint to bind a fresh thread.')
    refs = spec.get('references', [])
    locators = spec.get('locators', [])
    if not isinstance(refs, list) or any(not isinstance(r, str) for r in refs):
        raise ValueError('references must be a list of IDs')
    if not isinstance(locators, list) or len(locators) > 16 or any(not isinstance(x, dict) for x in locators):
        raise ValueError('locators must be at most 16 locator objects')
    metadata = dict(existing.get('native_metadata', {})) if existing else {}
    cache = _cache(hub, service, identity)
    matched = next((r for r in cache.get('candidates', []) if r.get('native_thread') == thread), None)
    if thread and matched is None:
        discovery = native.discover(hub, service, catalog.cfg['projects'], limit=1, thread=thread)
        matched = next((r for r in discovery['candidates'] if r['native_thread'] == thread), None)
    if matched:
        metadata = matched
    entry = {field: spec[field] for field in SESSION_FIELDS if field in spec}
    entry.update(id=entry_id, project=project, session_schema=SESSION_SCHEMA, service=service,
                 workspace_id=identity['workspace_id'], workspace_root=identity['root'],
                 storage_instance=native.storage_instance(service), native_thread=thread,
                 unbound_session=unbound, context_key=context, updated_at=_now(), native_metadata=metadata)
    for field in ('user_direction', 'selected_direction', 'next_action'):
        entry.setdefault(field, '')
    for field in ('constraints', 'decisions', 'results', 'unresolved', 'references', 'locators'):
        entry.setdefault(field, [])
    entry.setdefault('captured_through', {})
    entry.setdefault('fork_parent', metadata.get('fork_parent'))
    entry['canonical_inputs'], _ = _inputs(catalog, refs)
    entry['locator_inputs'] = _locators(hub, catalog, locators)
    errors = validate_entry(entry)
    if errors:
        raise ValueError('; '.join(errors))
    # Generic CAS protects both same-session and unrelated concurrent writes.
    return knowledge.publish_section(hub, project, 'sessions', entry, owner,
                                     expected_revision=spec.get('expected_revision'))


def _terms(text):
    return set(re.findall(r'[a-z0-9][a-z0-9_-]{2,}', text.casefold())) - {
        'the', 'and', 'with', 'from', 'this', 'that', 'for', 'can', 'please', 'continue', 'research', 'hub'}


def _rank(row, query, project, project_reason):
    score, reasons = 0, []
    if project == row['project']:
        score += 30
        reasons.append(project_reason)
    text = json.dumps({k: row.get(k) for k in ('user_direction', 'selected_direction', 'context_key', 'references', 'locators', 'unresolved', 'next_action')})
    overlap = sorted(_terms(query) & _terms(text))
    if overlap:
        score += min(25, len(overlap) * 3)
        reasons.append('current-request terms: ' + ', '.join(overlap[:6]))
    for ref in row.get('references', []):
        if re.search(r'(?<![\w-])' + re.escape(ref) + r'(?![\w-])', query, re.I):
            score += 30
            reasons.append('explicit ID ' + ref)
    return score, reasons


def _short(value, maximum=750):
    if isinstance(value, str):
        return value if len(value) <= maximum else value[:maximum] + ' … [checkpoint contains more]'
    return value


def _excerpt(row):
    result = {k: row.get(k) for k in ('id', 'project', 'revision', 'native_thread', 'unbound_session',
              'context_key', 'updated_at', 'fork_parent', 'inherited_from', 'captured_through')}
    for field in ('user_direction', 'selected_direction', 'next_action'):
        result[field] = _short(row.get(field, ''), 700 if field == 'user_direction' else 450)
    for field in ('constraints', 'decisions', 'results', 'unresolved'):
        values = row.get(field, [])
        result[field] = [_short(v, 240) if isinstance(v, str) else {k: _short(vv, 240) for k, vv in v.items()} if isinstance(v, dict) else v for v in values[:4]]
        if len(values) > 4:
            result[field + '_omitted'] = len(values) - 4
    result['references'] = row.get('references', [])
    result['locators'] = row.get('locators', [])[:4]
    if len(row.get('locators', [])) > 4:
        result['locators_omitted'] = len(row['locators']) - 4
    return result


def _freshness(hub, catalog, row):
    current, corrections = _inputs(catalog, row.get('references', []))
    old = row.get('canonical_inputs', {}).get('hashes', {})
    changes = [key for key in sorted(set(old) | set(current['hashes'])) if old.get(key) != current['hashes'].get(key)]
    locators = _locators(hub, catalog, row.get('locators', []))
    locator_changes = [key for key in locators if locators[key] != row.get('locator_inputs', {}).get(key)]
    unresolved = [dict(locator=item, reason=locators[_digest(item)].get('reason')) for item in row.get('locators', []) if locators[_digest(item)]['state'] == 'unresolved']
    compact = [dict(id=c['id'], title=c.get('title'), statement=_short(c.get('statement', ''), 700),
                    limitations=_short(c.get('limitations', ''), 450), status=c.get('status'), project=catalog.project_of(c)) for c in corrections]
    return dict(state='stale' if changes or locator_changes else ('unknown' if current['unresolved'] or unresolved else 'current'),
                changed_ids=changes, unresolved_ids=current['unresolved'], changed_locators=len(locator_changes),
                unresolved_locators=unresolved, current_corrections=compact,
                checked_ids=len(current['hashes']), scope='Referenced canonical inputs, their prerequisites, incoming corrections, and bounded cited files/sections; not proof certification.')


def resume(hub, service, thread=None, project=None, query='', cwd=None):
    native.service_home(service)
    catalog = cs.load_catalog(hub)
    identity = workspace_identity(hub, catalog)
    resolved_project, project_reason = _project(catalog, hub, project, cwd, query)
    requested_thread = thread
    runtime_thread = native.runtime_thread(service)
    thread = thread or runtime_thread
    packet = dict(state='no-checkpoint', workspace=identity, service=service,
                  storage_instance=native.storage_instance(service), current_native_thread=runtime_thread,
                  requested_native_thread=requested_thread,
                  current_request=_short(query, 1500), project=resolved_project, bound=False,
                  authority='Current request governs. Historical direction is context only; no goal, helper allowance, or work is reactivated.',
                  route=f'python3 tools/hub.py session refresh --service {service}', warnings=[])
    if identity['state'] != 'current':
        packet['state'] = identity['state']
        return packet
    saved = [row for row in _rows(catalog) if row.get('session_schema') == SESSION_SCHEMA and
             row.get('workspace_id') == identity['workspace_id'] and row.get('service') == service and
             row.get('storage_instance') == native.storage_instance(service)]
    if resolved_project:
        saved = [r for r in saved if r['project'] == resolved_project]
    exact = [r for r in saved if thread and r.get('native_thread') == thread]
    # --thread selects a specific prior conversation. Runtime identity, by
    # contrast, may be a brand-new conversation that needs prior-context ranking.
    candidates = exact if requested_thread else (exact or saved)
    ranked = sorted([(r, *_rank(r, query, resolved_project, project_reason)) for r in candidates],
                    key=lambda x: (x[1], x[0].get('updated_at', '')), reverse=True)
    if not ranked:
        if requested_thread:
            packet['warnings'].append('No checkpoint for the explicitly selected native thread in this scope; no other conversation was substituted.')
        cache = _cache(hub, service, identity)
        packet['native_discovery'] = dict(state=cache.get('state', 'not-refreshed'),
            observed_at=cache.get('observed_at'), candidate_count=len(cache.get('candidates', [])),
            coverage=cache.get('coverage', {}))
        bootstrap = []
        for row in cache.get('candidates', []):
            inferred, _ = _project(catalog, hub, None, row.get('cwd'), '')
            if resolved_project and inferred != resolved_project:
                continue
            overlap = _terms(query) & _terms(row.get('title') or '')
            score = len(overlap) + (30 if thread == row['native_thread'] else 0) + (10 if inferred == resolved_project and inferred else 0)
            bootstrap.append((score, row))
        bootstrap.sort(key=lambda item: item[0], reverse=True)
        packet['bootstrap_candidates'] = [dict(native_thread=row['native_thread'], title=row.get('title'),
            cwd=row.get('cwd'), reason='Scoped native metadata only; no direction/results captured.',
            refresh=f'python3 tools/hub.py session refresh --service {service} --thread {row["native_thread"]}') for _, row in bootstrap[:3]]
        packet['warnings'].append('No saved matching checkpoint. Explicit refresh can discover bounded same-workspace native metadata; parent inspection is needed before saving a checkpoint.')
        return packet
    top, score, reasons = ranked[0]
    ambiguous = len(ranked) > 1 and (score == 0 or score - ranked[1][1] < 5)
    if ambiguous:
        packet.update(state='ambiguous', choices=[dict(id=r['id'], project=r['project'], context_key=r['context_key'],
            native_thread=r.get('native_thread'), direction=_short(r.get('selected_direction') or r.get('user_direction', ''), 240),
            revision=r['revision'], reason=why or ['No request/project evidence distinguishes this context.']) for r, _, why in ranked[:3]])
        packet['warnings'].append('Several contexts remain plausible; select one labelled context before using historical direction. Recency alone did not select a conversation.')
        return packet
    if exact:
        reasons.insert(0, 'exact native thread in this service/storage/workspace')
    elif len(ranked) == 1 and not reasons:
        reasons.append('only saved same-service context in this workspace; confirm relevance against the current request')
    packet.update(state='recovered', selected=_excerpt(top), match_reason=reasons,
                  canonical_freshness=_freshness(hub, catalog, top),
                  native_freshness=native.freshness(service, top.get('native_metadata', {}), top.get('captured_through', {})))
    packet['route'] += ' --thread ' + top['native_thread'] if top.get('native_thread') else ''
    packet['route_purpose'] = 'Read the selected prior native metadata/excerpts; only the derived hub cache is updated. No native history or checkpoint is edited.'
    if top.get('native_thread') != runtime_thread:
        packet['warnings'].append('Prior context is not bound to the current conversation. Save a new checkpoint with inherited_from only after selecting this context.')
        packet['binding_route'] = f'python3 tools/hub.py session checkpoint NEW-SPEC.json --service {service}'
    return packet


def refresh(hub, service, **kwargs):
    catalog = cs.load_catalog(hub)
    identity = workspace_identity(hub, catalog)
    if identity['state'] != 'current':
        raise ValueError(identity['reason'])
    result = native.discover(hub, service, catalog.cfg['projects'], limit=kwargs.get('limit', 40), thread=kwargs.get('thread'))
    result.update(workspace_id=identity['workspace_id'], root=identity['root'])
    # Incremental discovery cache is disposable; durable checkpoint entries are not.
    previous = _cache(hub, service, identity)
    if kwargs.get('thread') and result['state'] == 'available':
        merged = {r['native_thread']: r for r in previous.get('candidates', [])}
        merged.update({r['native_thread']: r for r in result['candidates']})
        cached = dict(result, candidates=list(merged.values())[-200:])
    else:
        cached = result
    path = _cache_path(hub, service)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, prefix='.native-', delete=False) as stream:
        json.dump(cached, stream, ensure_ascii=False, indent=2)
        temporary = stream.name
    os.replace(temporary, path)
    if kwargs.get('thread'):
        selected = next((r for r in result['candidates'] if r['native_thread'] == kwargs['thread']), None)
        if selected:
            result = dict(result, selected_tail=native.tail(service, selected, after=kwargs.get('after')))
    result['checkpoint_effect'] = 'None. Metadata/excerpts are supplementary; parent must inspect and explicitly checkpoint captured direction/results.'
    return result
