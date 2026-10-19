"""Supplemental research aids and operational state, never theorem records.

The catalog loader owns coherent reads. All writes go through the existing
recoverable kernel transaction, with full-file CAS plus per-entry revisions.
Payloads are retained as supplied; this module does not infer mathematical truth.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import re
import time

import catalog_store as cs
import transactions as tx

SECTIONS = ('aids', 'sessions', 'dispositions', 'checkpoints')
OPERATIONAL_SECTIONS = SECTIONS[1:]
KINDS = frozenset({'gap', 'move', 'episode', 'connection', 'reminder'})
ASSESSMENTS = frozenset({'extracted', 'user-stated', 'parent-inspected'})
THEOREM_FIELDS = frozenset({'status', 'evidence_level', 'novelty', 'review',
                          'depends_on', 'source_refs', 'supersedes', 'subsumed_by'})


def empty_envelope():
    return dict(schema_version=1, **{section: [] for section in SECTIONS})


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _revision(value):
    return type(value) is int and value > 0


def _json_value(value):
    """Strict JSON (including finite numbers), without lossy coercion."""
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_value(item) for key, item in value.items())
    return False


def _rows(catalog):
    """Tolerant iterator for diagnostics on malformed in-memory candidates."""
    knowledge = getattr(catalog, 'knowledge', {})
    if not isinstance(knowledge, dict):
        return
    for owner, envelope in knowledge.items():
        if not isinstance(envelope, dict):
            continue
        for section in SECTIONS:
            rows = envelope.get(section, [])
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        yield owner, section, row


def validate(catalog, hub, overrides=None):
    """Validate types, ownership, namespaces and prospective references.

    Provenance is a locator, not a theorem-evidence assertion. Optional hashes
    are syntax-checked here; no provenance files or mathematical evidence are
    read during ordinary catalog lookup. `overrides` matches the shared catalog
    validator API; the caller supplies an already prospective Catalog.
    """
    errors = []
    knowledge = getattr(catalog, 'knowledge', {})
    if not isinstance(knowledge, dict):
        return ['knowledge must be an owner-to-envelope map']
    projects = catalog.cfg.get('projects', {})
    owners = set(projects) | {'hub'}
    math_ids = set(catalog.by_id())
    all_ids = set(math_ids)
    aid_ids = set()
    for owner, section, entry in _rows(catalog):
        key = entry.get('id')
        if _text(key):
            if key in all_ids:
                errors.append(f'{key}: duplicate global ID')
            all_ids.add(key)
            if section == 'aids':
                aid_ids.add(key)
    for owner, envelope in knowledge.items():
        if not isinstance(owner, str) or owner not in owners:
            errors.append(f'knowledge: unknown owner {owner!r}')
        if not isinstance(envelope, dict):
            errors.append(f'{owner}: knowledge envelope must be an object')
            continue
        if type(envelope.get('schema_version')) is not int or envelope['schema_version'] != 1:
            errors.append(f'{owner}: unsupported knowledge schema_version')
        for extra in set(envelope) - {'schema_version', *SECTIONS}:
            errors.append(f'{owner}: unknown knowledge section {extra}')
        for section in SECTIONS:
            rows = envelope.get(section)
            if not isinstance(rows, list):
                errors.append(f'{owner}: {section} must be a list')
                continue
            if owner == 'hub' and section == 'aids' and rows:
                errors.append('hub knowledge cannot contain research aids')
            for index, entry in enumerate(rows):
                label = f'{owner}/{section}[{index}]'
                if not isinstance(entry, dict):
                    errors.append(f'{label}: entry must be an object')
                    continue
                key = entry.get('id')
                if _text(key):
                    label = key
                else:
                    errors.append(f'{label}: id must be a nonempty string')
                if entry.get('project') != owner:
                    errors.append(f'{label}: explicit project must match storage owner {owner}')
                if not _revision(entry.get('revision')):
                    errors.append(f'{label}: revision must be a positive integer')
                if not _json_value(entry):
                    errors.append(f'{label}: entry must contain finite JSON values only')
                if section != 'aids':
                    if _text(key) and not key.startswith(f'{owner}:'):
                        errors.append(f'{label}: operational ID must start with {owner}:')
                    if section == 'sessions':
                        from .sessions import validate_entry
                        errors.extend(f'{label}: {error}' for error in validate_entry(entry))
                    continue
                prefix = projects.get(owner, {}).get('id_prefix', '') if isinstance(owner, str) else ''
                if _text(key) and not re.fullmatch(re.escape(prefix) + r'K[0-9]+', key):
                    errors.append(f'{label}: aid ID must use the project K namespace')
                kind = entry.get('kind')
                if not isinstance(kind, str) or kind not in KINDS:
                    errors.append(f'{label}: invalid aid kind')
                for field in ('title', 'disposition'):
                    if not _text(entry.get(field)):
                        errors.append(f'{label}: {field} must be a nonempty string')
                if not isinstance(entry.get('payload'), dict):
                    errors.append(f'{label}: payload must be an object')
                for field in THEOREM_FIELDS & entry.keys():
                    errors.append(f'{label}: {field} belongs to mathematical records, not aids')
                assessment = entry.get('assessment')
                if not isinstance(assessment, dict):
                    errors.append(f'{label}: assessment must have state and scope')
                else:
                    state = assessment.get('state')
                    if not isinstance(state, str) or state not in ASSESSMENTS:
                        errors.append(f'{label}: invalid assessment state')
                    if not _text(assessment.get('scope')):
                        errors.append(f'{label}: assessment scope must be explicit')
                references = entry.get('references')
                if not isinstance(references, list):
                    errors.append(f'{label}: references must be a list')
                else:
                    for ref in references:
                        if not isinstance(ref, dict) or not _text(ref.get('id')) or not _text(ref.get('relation')):
                            errors.append(f'{label}: reference needs string id and relation')
                        elif ref['id'] not in math_ids | aid_ids:
                            errors.append(f'{label}: unresolved/non-research reference {ref["id"]}')
                provenance = entry.get('provenance')
                if not isinstance(provenance, list):
                    errors.append(f'{label}: provenance must be a list')
                else:
                    for item in provenance:
                        if not isinstance(item, dict) or not all(_text(item.get(field)) for field in ('root', 'path', 'locator', 'origin')):
                            errors.append(f'{label}: provenance needs root, path, locator and origin')
                            continue
                        try:
                            from catalog_validation import evidence_path
                            evidence_path(hub, catalog.cfg, item)
                        except (KeyError, TypeError, ValueError, OSError) as exc:
                            errors.append(f'{label}: invalid provenance path: {exc}')
                        if 'sha256' in item and (not isinstance(item['sha256'], str) or not re.fullmatch(r'[a-fA-F0-9]{64}', item['sha256'])):
                            errors.append(f'{label}: invalid provenance SHA-256')
    return errors


def validate_transition(before, after, scopes):
    """Stable identities and monotonic revisions, independent of theorem status."""
    errors, scopes = [], set(scopes)
    old = {entry['id']: (owner, section, entry) for owner, section, entry in _rows(before)
           if _text(entry.get('id'))}
    new = {entry['id']: (owner, section, entry) for owner, section, entry in _rows(after)
           if _text(entry.get('id'))}
    for key in old.keys() - new.keys():
        errors.append(f'{key}: cannot delete supplemental entries; record a disposition instead')
    for key, (owner, section, entry) in new.items():
        prior = old.get(key)
        if prior is not None and prior == (owner, section, entry):
            continue
        if owner not in scopes and 'hub' not in scopes:
            errors.append(f'{key}: knowledge update outside declared scopes')
        if prior is None:
            if entry.get('revision') != 1 or not _revision(entry.get('revision')):
                errors.append(f'{key}: new entries must start at revision 1')
        else:
            old_owner, old_section, old_entry = prior
            if (old_owner, old_section) != (owner, section):
                errors.append(f'{key}: knowledge owner and section are immutable')
            if section == 'aids' and old_entry.get('kind') != entry.get('kind'):
                errors.append(f'{key}: aid kind is immutable')
            if section == 'sessions':
                for field in ('workspace_id', 'service', 'storage_instance', 'native_thread', 'unbound_session', 'context_key'):
                    if old_entry.get(field) != entry.get(field):
                        errors.append(f'{key}: session {field} is immutable')
            if not _revision(old_entry.get('revision')) or not _revision(entry.get('revision')) or entry['revision'] != old_entry['revision'] + 1:
                errors.append(f'{key}: changed entry requires exactly the next revision')
    return errors


def _retry_busy(function):
    """Bounded contention retry only; stale CAS/revisions must reach the caller."""
    for attempt in range(8):
        try:
            return function()
        except tx.TransactionError as exc:
            if 'Hub is busy:' not in str(exc) or attempt == 7:
                raise
            time.sleep(min(.01 * 2 ** attempt, .1))


def _check_owner(catalog, project, *, aid=False):
    if not isinstance(project, str) or project not in set(catalog.cfg['projects']) | ({'hub'} if not aid else set()):
        raise ValueError('Unknown project or invalid research-aid owner')


def _replacement(spec, previous, expected_revision):
    if previous is None:
        if expected_revision is not None:
            raise ValueError('Cannot update a missing entry with expected_revision')
        revision = 1
    else:
        if not _revision(expected_revision):
            raise ValueError('Replacing an entry requires expected_revision')
        if expected_revision != previous['revision']:
            raise ValueError(f'Stale revision: expected {expected_revision}, current {previous["revision"]}')
        revision = previous['revision'] + 1
    acceptable = {revision} | ({previous['revision']} if previous is not None else set())
    if 'revision' in spec and (not _revision(spec['revision']) or spec['revision'] not in acceptable):
        raise ValueError(f'Published revision must be {revision}; copied current revision is also accepted')
    result = copy.deepcopy(spec)
    result['revision'] = revision
    return result


def _publish(hub, catalog, project, section, entry, owner, extra=None):
    import hub_operations as ops
    envelope = copy.deepcopy(catalog.knowledge[project])
    rows = envelope[section]
    rows[:] = [entry if row['id'] == entry['id'] else row for row in rows]
    if not any(row['id'] == entry['id'] for row in rows):
        rows.append(entry)
    relative = cs.knowledge_path(Path(hub).resolve(), project).relative_to(Path(hub).resolve()).as_posix()
    changes = {relative: cs.serialize(envelope)}
    expected = dict(catalog.file_hashes)
    for name, (content, digest) in (extra or {}).items():
        changes[name], expected[name] = content, digest
    # Keep this original snapshot on contention retries. Never rebase a stale
    # user's replacement silently onto a newer catalog revision.
    result = _retry_busy(lambda: ops.apply(hub, dict(changes=changes, expected=expected), [project], owner))
    result['aid' if section == 'aids' else 'entry'] = copy.deepcopy(entry)
    return result


def remember(hub, spec, project, owner):
    """Create an allocated aid atomically, or replace one at an exact revision."""
    if not isinstance(spec, dict) or not _json_value(spec):
        raise ValueError('Aid specification must be a JSON object')
    if not _text(owner):
        raise ValueError('Publication owner must be a nonempty string')
    catalog = _retry_busy(lambda: cs.load_catalog(hub))
    _check_owner(catalog, project, aid=True)
    spec = copy.deepcopy(spec)
    if 'project' in spec and spec['project'] != project:
        raise ValueError('Aid project differs from requested project')
    spec['project'] = project
    expected_revision = spec.pop('expected_revision', None)
    extra = {}
    if 'id' not in spec:
        if expected_revision is not None:
            raise ValueError('New aid must omit expected_revision')
        if catalog.cfg['projects'][project].get('state') != 'active':
            raise ValueError('ID allocation requires an active project')
        import hub_operations as ops
        relative = f'projects/{project}/catalog/reservations.json'
        raw = tx.read_bytes(tx.safe_path(hub, relative))
        reservations = ops.read_reservations(raw)
        prefix = catalog.cfg['projects'][project].get('id_prefix', '') + 'K'
        keys = set(catalog.by_id()) | set(catalog.knowledge_by_id()) | {row['id'] for row in reservations}
        numbers = [int(key[len(prefix):]) for key in keys if key.startswith(prefix) and key[len(prefix):].isdigit()]
        spec['id'] = prefix + f'{max(numbers, default=0) + 1:03d}'
        reservations.append(dict(id=spec['id'], owner=owner))
        extra[relative] = (cs.serialize(reservations), tx.digest(raw))
        previous = None
    else:
        if not _text(spec['id']):
            raise ValueError('Aid id must be a nonempty string')
        previous = next((row for row in catalog.knowledge[project]['aids'] if row['id'] == spec['id']), None)
        if previous is None:
            raise ValueError('Explicit aid id must name an existing aid; omit id to allocate')
    entry = _replacement(spec, previous, expected_revision)
    return _publish(hub, catalog, project, 'aids', entry, owner, extra)


def publish_section(hub, project, section, entry, owner, expected_revision=None):
    """Create or revision-check one session, disposition or checkpoint row."""
    if not isinstance(section, str) or section not in OPERATIONAL_SECTIONS:
        raise ValueError('Section must be sessions, dispositions or checkpoints')
    if not isinstance(entry, dict) or not _json_value(entry) or not _text(entry.get('id')):
        raise ValueError('Operational entry must be a JSON object with a string id')
    if not _text(owner):
        raise ValueError('Publication owner must be a nonempty string')
    catalog = _retry_busy(lambda: cs.load_catalog(hub))
    _check_owner(catalog, project)
    if 'project' in entry and entry['project'] != project:
        raise ValueError('Operational entry project differs from requested project')
    spec = copy.deepcopy(entry)
    spec['project'] = project
    previous = next((row for row in catalog.knowledge[project][section] if row['id'] == entry['id']), None)
    result = _replacement(spec, previous, expected_revision)
    return _publish(hub, catalog, project, section, result, owner)
