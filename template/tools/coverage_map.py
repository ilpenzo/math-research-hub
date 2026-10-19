#!/usr/bin/env python3
"""Validate and render the curated coverage map, without reading archive bodies."""
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

HUB = Path(__file__).resolve().parents[1]
STATES = {'unreviewed', 'mapped', 'claims-extracted', 'reconciled'}


def matches(unit, entry):
    if unit['root'] != entry['root']:
        return False
    p, base = entry['path'], unit['path']
    if unit['selector'] == 'file':
        return p == base
    if unit['selector'] == 'directory':
        return str(Path(p).parent) == base
    if unit['selector'] == 'subtree':
        return p.startswith(base.rstrip('/') + '/')
    return False


def membership_hash(entries):
    return hashlib.sha256('\n'.join(sorted(e['path'] for e in entries)).encode()).hexdigest()


def reconcile(data, inventory, records, cfg=None):
    """Return errors and partition. Every inventoried path must match exactly once."""
    errors, assigned = [], defaultdict(list)
    units = data['units']
    ids = [u['id'] for u in units]
    if len(ids) != len(set(ids)):
        errors.append('Duplicate coverage unit IDs')
    known_topics = set(data['topics'])
    by_root = defaultdict(list)
    for u in units:
        if cfg and cfg.get('schema_version', 1) >= 2:
            if u.get('owner') not in cfg['projects']:
                errors.append(f"{u['id']}: unknown/missing coverage owner")
            if not isinstance(u.get('relevant_projects'), list) or set(u['relevant_projects']) - set(cfg['projects']):
                errors.append(f"{u['id']}: invalid relevant projects")
        by_root[u['root']].append(u)
        if u['state'] not in STATES or not u.get('disposition'):
            errors.append(f"{u['id']}: invalid state/disposition")
        if not u.get('topics') or set(u['topics']) - known_topics:
            errors.append(f"{u['id']}: unknown/empty topic")
        if u['selector'] not in {'file', 'directory', 'subtree'}:
            errors.append(f"{u['id']}: invalid selector")
    for e in inventory['entries']:
        hits = [u for u in by_root[e['root']] if matches(u, e)]
        if len(hits) != 1:
            errors.append(f"{e['root']}:{e['path']}: {len(hits)} coverage assignments")
        else:
            assigned[hits[0]['id']].append(e)
    known_records = {r['id'] for r in records}
    for u in units:
        es = assigned[u['id']]
        if not es:
            errors.append(f"{u['id']}: empty coverage unit")
        if len(es) != u['file_count'] or membership_hash(es) != u['membership_sha256']:
            errors.append(f"{u['id']}: inventory membership changed; remap explicitly")
        member_paths = {e['path'] for e in es}
        if cfg and cfg.get('schema_version', 1) >= 2:
            overrides = u.get('file_owners', {})
            if not isinstance(overrides, dict) or set(overrides) - member_paths or set(overrides.values()) - set(cfg['projects']):
                errors.append(f"{u['id']}: invalid file-owner override")
        for p in u.get('reviewed_paths', []):
            if p['path'] not in member_paths or not p.get('scope'):
                errors.append(f"{u['id']}: invalid reviewed path")
        if set(u.get('record_ids', [])) - known_records:
            errors.append(f"{u['id']}: unknown record")
        dispositions = u.get('file_dispositions', [])
        disposed_paths = [p['path'] for p in dispositions]
        if len(disposed_paths) != len(set(disposed_paths)):
            errors.append(f"{u['id']}: duplicate file disposition")
        for p in dispositions:
            if p['path'] not in member_paths or not p.get('disposition') or not p.get('basis'):
                errors.append(f"{u['id']}: invalid file disposition")
            if set(p.get('record_ids', [])) - known_records:
                errors.append(f"{u['id']}: unknown file-disposition record")
        if u['state'] in {'claims-extracted', 'reconciled'}:
            if not u.get('completion_basis'):
                errors.append(f"{u['id']}: completion needs explicit basis")
            if set(disposed_paths) != member_paths:
                errors.append(f"{u['id']}: completion needs a disposition for every member")
    if inventory.get('errors'):
        errors.append('Inventory has read errors')
    return errors, assigned


def load():
    import catalog_store as cs
    import transactions as tx
    with tx.locked(HUB):
        cat = cs.load_catalog(HUB, already_locked=True)
        data = json.loads((HUB / 'catalog/coverage.json').read_bytes())
        inv = json.loads((HUB / 'indexes/inventory.json').read_bytes())
    return data, inv, cat.records


def check():
    data, inv, records = load()
    return reconcile(data, inv, records)[0]


def render_text(data, inv, records):
    errors, assigned = reconcile(data, inv, records)
    if errors:
        raise ValueError('\n'.join(errors[:30]))
    units = data['units']
    out = ['# Branch coverage', '', 'Generated from `catalog/coverage.json`; do not edit this view.', '',
           f"Search/mapping date: {data['mapping_date']}. Inventory snapshot: {inv['generated_at']}.", '',
           f"{len(inv['entries'])} paths; {len(units)} coverage units; each path assigned exactly once.",
           'Topics may overlap. A mapped package still has claims pending; no claim is verified by this map.',
           'Selectors: file = exact path; directory = direct files only; subtree = all descendants.', '',
           'Original paths, hashes and exact duplicates remain in inventory.json and DUPLICATES.md.', '',
           '| Topic | Units | Files (overlap possible) | Extraction touched units | Claims-extracted / reconciled units |', '|---|---:|---:|---:|---:|']
    for t, info in data['topics'].items():
        us = [u for u in units if t in u['topics']]
        touched = sum(bool(u.get('record_ids')) for u in us)
        completed = sum(u['state'] in {'claims-extracted', 'reconciled'} for u in us)
        out.append(f"| {t}: {info['label']} | {len(us)} | {sum(u['file_count'] for u in us)} | {touched} | {completed} |")
    out += ['', '## Topic assembly status', '']
    for t, info in data['topics'].items():
        if not any(info.get(k) for k in ('review_scope', 'independent_review', 'next_units')):
            continue
        out += [f"### {t}", '']
        for key in ('last_claim_pass', 'review_scope', 'independent_review', 'next_units'):
            if info.get(key):
                out += [f"{key.replace('_', ' ').capitalize()}: {info[key]}", '']
    out += ['', '## Gaps and reconciliation controls', '']
    for gap in data['gaps']:
        out.append(f"- **{gap['id']} ({gap['status']})**: {gap['statement']} Evidence: {gap['evidence']}")
    out += ['', '## Units', '', '| ID / archive path | Selector | Topics | Files | State / disposition | Imported records |', '|---|---|---|---:|---|---|']
    for u in units:
        out.append(f"| {u['id']} {u['root']}:{u['path']} | {u['selector']} | {', '.join(u['topics'])} | {u['file_count']} | {u['state']} / {u['disposition']} | {', '.join(u.get('record_ids', [])) or 'pending'} |")
    out += ['', '## Reviewed scope and remaining work', '',
            'Completion below concerns extraction, not proof certification. Pending source/proof',
            'checks remain explicit even for claims-extracted units.', '']
    for u in units:
        if not any(u.get(k) for k in ('reviewed_paths', 'file_dispositions', 'pending_review', 'completion_basis')):
            continue
        out += [f"### {u['id']} — {u['root']}:{u['path']}", '']
        for key in ('completion_basis', 'pending_review'):
            if u.get(key):
                out += [f"{key.replace('_', ' ').capitalize()}: {u[key]}", '']
        for p in u.get('reviewed_paths', []):
            out.append(f"- Reviewed {p.get('date', 'undated')}: `{p['path']}` — {p['scope']}")
        if u.get('reviewed_paths'):
            out.append('')
        for p in u.get('file_dispositions', []):
            refs = ', '.join(p.get('record_ids', [])) or 'no distinct claim'
            out.append(f"- Disposition: `{p['path']}` — {p['disposition']}; {refs}. {p['basis']}")
        out.append('')
    out += ['', '## Exact duplicate disposition', '',
            'Hash-equal files remain in place. These links identify claim-bearing copies only;',
            'they do not adjudicate versions, mathematical equivalence or completeness.', '']
    duplicates = defaultdict(list)
    claim_hashes = defaultdict(set)
    for e in inv['entries']:
        duplicates[e['sha256']].append(e)
    for r in records:
        for e in r['evidence']:
            claim_hashes[e['sha256']].add(r['id'])
    unresolved = 0
    for digest, es in sorted(duplicates.items()):
        if len(es) < 2:
            continue
        refs = sorted(claim_hashes[digest])
        if refs:
            out.append(f"- {digest[:16]}: {', '.join(refs)}; aliases: " + '; '.join(f"{e['root']}:{e['path']}" for e in es))
        else:
            unresolved += 1
    out += ['', f'{unresolved} duplicate groups have no directly registered claim-bearing copy; extraction/disposition pending (see DUPLICATES.md).', '']
    return '\n'.join(out)


def render():
    # The hub command owns coherent snapshots and transactional publication.
    import hub
    return hub.render()


if __name__ == '__main__':
    import sys
    import hub
    sys.exit(hub.render() if len(sys.argv) == 2 and sys.argv[1] == 'render' else hub.check())
