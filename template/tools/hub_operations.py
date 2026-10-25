"""Canonical batch publication, project scaffolding and stable ID reservations."""
import copy
import json
from pathlib import Path
import re

import catalog_store as cs
import catalog_validation as cv
import transactions as tx


def prospective(hub, overrides, scopes, transfers=None):
    """Called only inside apply_batch's publication lock."""
    before = cs.load_catalog(hub, already_locked=True)
    after = cs.load_catalog(hub, overrides=overrides, already_locked=True)
    transfers = transfers or {}
    if not isinstance(transfers, dict):
        raise ValueError('Transfers must be an ID-to-project map')
    old, new = before.by_id(), after.by_id()
    errors = []
    if set(old) - set(new):
        errors.append('A canonical batch cannot silently delete existing IDs')
    for key, entry in new.items():
        owner = after.project_of(entry)
        if key not in old:
            info = after.cfg['projects'][owner]
            prefix = info.get('id_prefix', '')
            kind = entry.get('kind')
            letter = {'result': 'R', 'conjecture': 'R', 'obstruction': 'O', 'correction': 'C', 'question': 'Q'}.get(kind, 'L') if isinstance(kind, str) else 'L'
            if entry.get('project') != owner or not re.fullmatch(re.escape(prefix + letter) + r'[0-9]+', key):
                errors.append(f'{key}: new entries need explicit ownership and the allocated project namespace')
        elif owner != before.project_of(old[key]):
            if transfers.get(key) != owner:
                errors.append(f'{key}: ownership change needs an exact declared transfer')
            if 'hub' not in scopes and not {owner, before.project_of(old[key])} <= set(scopes):
                errors.append(f'{key}: transfer requires both project scopes')
    for key, target in transfers.items():
        if key not in old or key not in new or after.project_of(new[key]) != target:
            errors.append(f'Invalid or unperformed declared transfer: {key}={target}')
    from recall import knowledge, checkpoint_store
    errors.extend(knowledge.validate_transition(before, after, scopes))
    # Validate newly referenced bodies completely; ordinary readers only hash the
    # small compressed object. Check every changed descriptor, not just new paths.
    old_operational = before.knowledge_by_id()
    for project, envelope in after.knowledge.items():
        for entry in envelope.get('checkpoints', []):
            if 'body' in entry and old_operational.get(entry['id']) != entry:
                try:
                    checkpoint_store.hydrate(hub, project, entry, overrides)
                except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
                    errors.append(f'{entry["id"]}: {exc}')
    old_aids = {entry['id'] for envelope in before.knowledge.values() for entry in envelope['aids']}
    for project, envelope in after.knowledge.items():
        added = {entry['id'] for entry in envelope['aids']} - old_aids
        if not added:
            continue
        relative = f'projects/{project}/catalog/reservations.json'
        raw = overrides.get(relative) if relative in overrides else tx.read_bytes(Path(hub) / relative)
        allocated = {row['id'] for row in read_reservations(raw)}
        for key in sorted(added - allocated):
            errors.append(f'{key}: new aids require an allocated project K ID')
    knowledge_paths = {cs.knowledge_path(Path(hub).resolve(), project).relative_to(Path(hub).resolve()).as_posix()
                       for project in [*after.cfg['projects'], 'hub']}
    reservation_paths = {f'projects/{project}/catalog/reservations.json' for project in after.cfg['projects']}
    body_paths = {name for name in overrides if checkpoint_store.is_body_path(name)}
    referenced_bodies = {entry['body']['path'] for envelope in after.knowledge.values()
                         for entry in envelope.get('checkpoints', []) if 'body' in entry}
    if body_paths - referenced_bodies:
        errors.append('New checkpoint bodies must be referenced by canonical checkpoint metadata')
    # Supplemental publication cannot mutate theorem state. Its routine path
    # does not rehash the mathematical evidence corpus. A joint batch, changed
    # proof file or configuration always takes the existing full validation.
    recall_config = 'catalog/recall-config.json'
    supplemental_only = (set(overrides) <= knowledge_paths | reservation_paths | body_paths | {recall_config}
                         and before.cfg == after.cfg and old == new)
    if supplemental_only:
        errors.extend(knowledge.validate(after, hub, overrides))
        if recall_config in overrides and overrides[recall_config] is not None:
            try:
                from recall.providers import Ollama
                config = json.loads(overrides[recall_config])
                if not isinstance(config, dict) or config.get('provider', 'ollama') != 'ollama':
                    raise ValueError('Only the local Ollama provider is supported')
                if 'enabled' in config and type(config['enabled']) is not bool:
                    raise ValueError('Provider enabled must be boolean')
                Ollama(config)  # Syntax/locality validation only; no network or model call.
            except (ValueError, TypeError, AttributeError) as exc:
                errors.append(f'{recall_config}: {exc}')
    else:
        ids = None if 'hub' in scopes else set()
        if ids is not None:
            for scope in scopes:
                ids.update(cs.closure(after, scope)[0])
        errors.extend(cv.validate(after, hub, ids, overrides))
    if not supplemental_only:
        coverage_rel, inventory_rel = 'catalog/coverage.json', 'indexes/inventory.json'
        coverage_bytes = overrides.get(coverage_rel) if coverage_rel in overrides else tx.read_bytes(Path(hub) / coverage_rel)
        inventory_bytes = overrides.get(inventory_rel) if inventory_rel in overrides else tx.read_bytes(Path(hub) / inventory_rel)
        if coverage_bytes is not None:
            if inventory_bytes is None:
                errors.append('Coverage requires its retained inventory')
            else:
                import coverage_map
                errors.extend(coverage_map.reconcile(json.loads(coverage_bytes), json.loads(inventory_bytes), after.records, after.cfg)[0])
    if errors:
        raise ValueError('\n'.join(errors))
    return before, after


def apply(hub, batch, scopes, owner='operator'):
    if not isinstance(batch, dict) or not isinstance(batch.get('changes'), dict) or not isinstance(batch.get('expected'), dict):
        raise ValueError('Batch requires changes and expected maps')
    scopes = set(scopes)
    if not scopes:
        raise ValueError('Explicit project scopes or hub scope required')
    changes, expected = batch['changes'], batch['expected']
    config = cs.load_catalog(hub).cfg
    if scopes - set(config['projects']) - {'hub'}:
        raise ValueError('Unknown scope')
    for name in changes:
        if not isinstance(name, str) or Path(name).as_posix() != name:
            raise ValueError('Batch paths must use their canonical relative spelling')
        tx.safe_path(hub, name)
        if changes[name] is not None and not isinstance(changes[name], (str, bytes)):
            raise ValueError(f'{name}: change must contain bytes, text or null')
        if 'hub' not in scopes and not any(name.startswith(f'projects/{p}/') for p in scopes):
            raise ValueError(f'{name}: outside the declared project write scopes')
    affected, affected_ids, affected_knowledge = [], [], []
    def validate(overrides):
        before, after = prospective(hub, overrides, scopes, batch.get('transfers'))
        old_math, new_math = before.by_id(), after.by_id()
        changed = {key for key, entry in new_math.items() if old_math.get(key) != entry}
        # Include all transitive consumers and corrected claims; no status edits.
        queue = list(changed)
        while queue:
            key = queue.pop()
            adjacent = set(new_math.get(key, {}).get('supersedes', []))
            adjacent |= set(old_math.get(key, {}).get('supersedes', []))
            adjacent |= {e['id'] for e in before.entries() + after.entries() if key in cs.references(e)}
            for other in adjacent - changed:
                changed.add(other)
                queue.append(other)
        owners = {after.project_of(new_math[key]) for key in changed if key in new_math}
        owners |= {before.project_of(old_math[key]) for key in changed if key in old_math}
        # Associative consumers are reported separately and never added to the
        # mathematical dependency graph or given mathematical status changes.
        old_knowledge, new_knowledge = before.knowledge_by_id(), after.knowledge_by_id()
        knowledge_changed = {key for key, entry in new_knowledge.items() if old_knowledge.get(key) != entry}
        pending = list(changed | knowledge_changed)
        aids = [entry for envelope in after.knowledge.values() for entry in envelope['aids']]
        while pending:
            key = pending.pop()
            for entry in aids:
                if entry['id'] not in knowledge_changed and any(ref['id'] == key for ref in entry['references']):
                    knowledge_changed.add(entry['id'])
                    pending.append(entry['id'])
        owners |= {new_knowledge[key]['project'] for key in knowledge_changed}
        affected.extend(sorted(owners))
        affected_ids.extend(sorted(changed))
        affected_knowledge.extend(sorted(knowledge_changed))
    txid = tx.apply_batch(hub, changes, expected, scopes=scopes, owner=owner, validate=validate)
    return dict(transaction=txid, affected_projects=affected,
                affected_ids=affected_ids, affected_knowledge_ids=affected_knowledge,
                followup='Check applicability in affected projects; rerender derived views. No statuses were auto-promoted.')


def read_reservations(raw):
    """Decode allocator state without permitting malformed or duplicate IDs."""
    rows = [] if raw is None else json.loads(raw)
    if not isinstance(rows, list) or any(not isinstance(row, dict) or
            not isinstance(row.get('id'), str) or not row['id'] or
            not isinstance(row.get('owner'), str) or not row['owner'].strip() for row in rows):
        raise ValueError('Reservations must be a list of objects with string id and owner')
    if len({row['id'] for row in rows}) != len(rows):
        raise ValueError('Duplicate reserved ID')
    return rows


def reserve_id(hub, project, kind, owner):
    if not isinstance(kind, str) or kind not in {'R', 'O', 'C', 'Q', 'L', 'K'}:
        raise ValueError('Kind must be R, O, C, Q, L or K')
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError('Reservation owner must be a nonempty string')
    cat = cs.load_catalog(hub)
    if not isinstance(project, str) or project not in cat.cfg['projects']:
        raise ValueError('Unknown project')
    if cat.cfg['projects'][project].get('state') != 'active':
        raise ValueError('ID allocation requires an active project')
    rel = f'projects/{project}/catalog/reservations.json'
    data = tx.read_bytes(Path(hub) / rel)
    reservations = read_reservations(data)
    prefix = cat.cfg['projects'][project]['id_prefix'] + kind
    existing = set(cat.by_id()) | set(cat.knowledge_by_id()) | {r['id'] for r in reservations}
    nums = [int(k[len(prefix):]) for k in existing if k.startswith(prefix) and k[len(prefix):].isdigit()]
    key = prefix + f'{max(nums, default=0) + 1:03d}'
    reservations.append(dict(id=key, owner=owner))
    expected = dict(cat.file_hashes, **{rel: tx.digest(data)})
    txid = tx.apply_batch(hub, {rel: cs.serialize(reservations)}, expected, scopes=[project], owner=owner)
    return dict(id=key, transaction=txid, note='Reserved only; no claim or review status created.')


def add_project(hub, name, title, prefix):
    from recall.knowledge import empty_envelope
    cat = cs.load_catalog(hub)
    if name in cat.cfg['projects'] or cat.layout != 'federated':
        raise ValueError('Project already exists, or hub has not been federated')
    cs.federated_path(hub, name, 'records')
    cfg = copy.deepcopy(cat.cfg)
    base = f'projects/{name}'
    cfg['projects'][name] = dict(title=title, roots=[], state='active', id_prefix=prefix,
                                catalog_state='initialized', document_routes=[base],
                                archive_selectors=[], shared_selectors=[], code_roots=[])
    cs.validate_config(hub, cfg)
    changes = {
        'hub.json': cs.serialize(cfg),
        f'{base}/catalog/records.json': '[]\n', f'{base}/catalog/sources.json': '[]\n',
        f'{base}/catalog/knowledge.json': cs.serialize(empty_envelope()),
        f'{base}/START_HERE.md': f'# {title}\n\nRead [AGENTS.md](AGENTS.md), then [TOPICS.md](TOPICS.md).\n\nNo research results integrated yet. State the project question and controls before starting.\n',
        f'{base}/AGENTS.md': f'# {title}: project addendum\n\nShared policy: [../../AGENTS.md](../../AGENTS.md).\nProject: `{name}`; new IDs: `{prefix}`. Read START_HERE and one topic route.\nAdd applicable domain hypotheses and controls here; do not copy another project\'s target.\nHelpers receive this scope and named inputs, not campaign history.\n',
        f'{base}/CLAUDE.md': '@../../CLAUDE.md\n@AGENTS.md\n',
        f'{base}/TOPICS.md': f'# {title}: topics\n\nNo topic routes yet.\n',
        f'{base}/CHANGELOG.md': f'# {title}: changes\n\nProject scaffold created; research has not started.\n',
    }
    expected = dict(cat.file_hashes)
    for rel in changes:
        expected.setdefault(rel, tx.digest(tx.read_bytes(Path(hub) / rel)))
        if rel != 'hub.json' and expected[rel] is not None:
            raise ValueError(f'Refusing to overwrite an existing project path: {rel}')
    return apply(hub, dict(changes=changes, expected=expected), ['hub'], 'add-project')
