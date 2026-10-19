"""Shared structural and evidence checks for snapshots and prospective batches."""
from datetime import datetime
import hashlib
import json
from pathlib import Path


def evidence_path(hub, cfg, item):
    roots = {k: Path(v).expanduser().resolve() for k, v in cfg.get('roots', {}).items()}
    roots['hub'] = Path(hub).resolve()
    base = roots[item['root']]
    relative = Path(item['path'])
    path = (base / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(base):
        raise ValueError(f'Path escapes root: {item["path"]}')
    return path


def validate(catalog, hub, evidence_ids=None, overrides=None):
    """Global schema/typed-graph checks; evidence only for the requested closure."""
    errors, overrides = [], overrides or {}
    rs, ss = catalog.records, catalog.sources
    by_id = catalog.by_id()
    records, sources = {r['id'] for r in rs}, {s['id'] for s in ss}
    required = {'id', 'kind', 'title', 'topics', 'status', 'evidence_level', 'novelty',
                'statement', 'hypotheses', 'limitations', 'evidence',
                'depends_on', 'supersedes', 'subsumed_by', 'source_refs'}
    enums = dict(kind={'result', 'conjecture', 'obstruction', 'correction', 'question'},
                 status={'reported', 'verified', 'conditional', 'open', 'refuted', 'superseded'},
                 evidence_level={'imported', 'draft-proof', 'primary-checked', 'internally-reviewed',
                                 'computation-reproduced', 'externally-reviewed'},
                 novelty={'unassessed', 'known', 'subsumed', 'candidate'})
    for r in rs:
        for name in required - r.keys():
            errors.append(f'{r["id"]}: missing {name}')
        for name, allowed in enums.items():
            if not isinstance(r.get(name), str) or r.get(name) not in allowed:
                errors.append(f'{r["id"]}: invalid {name}')
        for name in ['title', 'statement', 'limitations']:
            if not isinstance(r.get(name), str) or not r[name].strip():
                errors.append(f'{r["id"]}: invalid {name}')
        for name in ['topics', 'hypotheses']:
            if not isinstance(r.get(name), list) or not r[name] or any(not isinstance(x, str) for x in r[name]):
                errors.append(f'{r["id"]}: invalid {name}')
        if r.get('kind') == 'obstruction' and not r.get('reopening_condition'):
            errors.append(f'{r["id"]}: missing reopening condition')
        if r.get('status') == 'verified' and (r.get('evidence_level') == 'imported' or not r.get('review')):
            errors.append(f'{r["id"]}: verified needs non-imported evidence and explicit review')
    for s in ss:
        if 'library' in s:
            from library import validate as validate_library
            errors.extend(validate_library(s, by_id))
        for name in ['citation', 'url', 'version', 'topics', 'locator', 'use', 'limitations', 'check_state', 'evidence']:
            if not s.get(name):
                errors.append(f'{s["id"]}: missing source field {name}')
        for name in ['citation', 'url', 'version', 'locator', 'use', 'limitations', 'check_state']:
            if not isinstance(s.get(name), str) or not s[name].strip():
                errors.append(f'{s["id"]}: invalid source field {name}')
        if not isinstance(s.get('topics'), list) or not s['topics'] or any(not isinstance(t, str) or not t.strip() for t in s['topics']):
            errors.append(f'{s["id"]}: invalid source topics')
        if not isinstance(s.get('check_state'), str) or s.get('check_state') not in {'lead', 'imported', 'imported-source-check', 'primary-checked'}:
            errors.append(f'{s["id"]}: invalid source check state')
        if s.get('check_state') == 'primary-checked' and not s.get('review'):
            errors.append(f'{s["id"]}: primary check needs explicit review')
    relations = ['depends_on', 'supersedes', 'subsumed_by', 'source_refs']
    for item in rs + ss:
        for name in relations:
            refs = item.get(name, [])
            if not isinstance(refs, list) or any(not isinstance(x, str) for x in refs):
                errors.append(f'{item["id"]}: invalid {name} list')
                continue
            allowed = sources if name == 'source_refs' else records
            for target in refs:
                if target not in allowed or target == item['id']:
                    label = 'source' if name == 'source_refs' else name
                    errors.append(f'{item["id"]}: invalid {label} reference {target}')
        review = item.get('review')
        if review is not None:
            if not isinstance(review, dict) or not all(review.get(k) for k in ['date', 'scope', 'artifact']):
                errors.append(f'{item["id"]}: review needs date, scope and hashed artifact')
            else:
                try:
                    datetime.strptime(review['date'], '%Y-%m-%d')
                except (TypeError, ValueError):
                    errors.append(f'{item["id"]}: invalid review date')
        if not isinstance(item.get('evidence'), list) or not item['evidence']:
            errors.append(f'{item["id"]}: missing/invalid evidence')
            continue
        # Structure is always validated, even for evidence outside a scoped check.
        es = list(item['evidence'])
        if isinstance(review, dict) and review.get('artifact'):
            es.append(review['artifact'])
        for e in es:
            try:
                path = evidence_path(hub, catalog.cfg, e)
                if not e.get('locator') or not isinstance(e.get('sha256'), str) or len(e['sha256']) != 64:
                    raise ValueError('Evidence needs locator and SHA-256')
                if evidence_ids is not None and item['id'] not in evidence_ids:
                    continue
                relative = path.relative_to(Path(hub).resolve()).as_posix() if e['root'] == 'hub' else None
                data = overrides[relative] if relative in overrides else path.read_bytes()
                if data is None or hashlib.sha256(data).hexdigest() != e['sha256']:
                    errors.append(f'{item["id"]}: STALE evidence {path}')
                if path.name == 'artifact-manifest.json' and data:
                    from artifacts import verify_manifest
                    errors.extend(f'{item["id"]}: {x}' for x in verify_manifest(path.parent, json.loads(data)))
            except (OSError, KeyError, TypeError, ValueError, AttributeError) as exc:
                errors.append(f'{item["id"]}: invalid evidence/review: {exc}')
    for name in ['depends_on', 'supersedes', 'subsumed_by']:
        done, active = set(), set()
        def visit(key):
            if key in active:
                errors.append(f'{name} cycle at {key}')
                return
            if key in done or key not in by_id:
                return
            active.add(key)
            refs = by_id[key].get(name, [])
            for ref in refs if isinstance(refs, list) else []:
                if isinstance(ref, str):
                    visit(ref)
            active.remove(key)
            done.add(key)
        for key in by_id:
            visit(key)
    from recall import knowledge
    errors.extend(knowledge.validate(catalog, hub, overrides))
    return errors
