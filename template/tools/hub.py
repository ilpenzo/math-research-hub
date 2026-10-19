#!/usr/bin/env python3
"""Local research catalog: no dependencies, network, or writes to source archives."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tomllib
import contextlib
import functools
import re

sys.path.insert(0, str(Path(__file__).resolve().parent))
import catalog_store as cs
import transactions as tx
import catalog_validation as cv
import library as lib

HUB = Path(__file__).resolve().parents[1]
CFG = {}  # Loaded with the coherent command snapshot, never at import time.
ROOTS = {'hub': HUB}


_CAT = None
_EXTRA = {}
_OUTPUTS = None


def project_of(item):
    return cs.project_of(item, CFG)


def read_json(name):
    raw = _EXTRA.get(name) if name in _EXTRA else (HUB / name).read_bytes()
    if raw is None:
        raise FileNotFoundError(name)
    return json.loads(raw)


def records():
    return (_CAT or cs.load_catalog(HUB)).records


def sources():
    return (_CAT or cs.load_catalog(HUB)).sources


def snapshot_hashes():
    hashes = dict(_CAT.file_hashes)
    hashes.update({n: tx.digest(b) for n, b in _EXTRA.items()})
    return hashes


def command_snapshot(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        global _CAT, _EXTRA, _OUTPUTS, CFG, ROOTS
        if _CAT is not None:
            return fn(*args, **kwargs)
        previous = CFG, ROOTS
        try:
            with tx.locked(HUB):
                _CAT = cs.load_catalog(HUB, already_locked=True)
                CFG = _CAT.cfg
                ROOTS = {k: Path(v).expanduser().resolve() for k, v in CFG.get('roots', {}).items()}
                ROOTS['hub'] = HUB
                _EXTRA = {name: tx.read_bytes(HUB / name) for name in
                          ['catalog/coverage.json', 'indexes/inventory.json']}
            _OUTPUTS = {}
            result = fn(*args, **kwargs)
            expected = snapshot_hashes()
            if _OUTPUTS:
                for name in _OUTPUTS:
                    expected.setdefault(name, tx.digest(tx.read_bytes(HUB / name)))
                txid = tx.apply_batch(HUB, _OUTPUTS, expected, owner=f'hub:{fn.__name__}')
                print(f'Published transaction {txid}.')
            else:
                with tx.locked(HUB):
                    changed = [n for n, h in expected.items() if tx.digest(tx.read_bytes(HUB / n)) != h]
                if changed:
                    raise ValueError(f'Inputs changed during {fn.__name__}; retry: {changed}')
            return result
        finally:
            _CAT, _EXTRA, _OUTPUTS = None, {}, None
            CFG, ROOTS = previous
    return wrapped


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def evidence_path(item):
    base = ROOTS[item['root']]
    relative = Path(item['path'])
    path = (base / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(base):
        raise ValueError(f'Path escapes root: {item}')
    return path


def write(name, content):
    if _OUTPUTS is None:
        raise ValueError('Generated writes require a command snapshot')
    tx.safe_path(HUB, name)
    _OUTPUTS[name] = content


def link(item):
    return f"[{item['root']}:{item['path']}](<{evidence_path(item)}>) — {item['locator']}"


def evidence_state(item):
    """Check saved direct evidence plus recorded prerequisites; never infer proof validity."""
    all_items = {r['id']: r for r in records() + sources()}
    checked, seen, queue = 0, set(), [item]
    while queue:
        current = queue.pop()
        if current['id'] in seen:
            continue
        seen.add(current['id'])
        evidence = list(current.get('evidence', []))
        if isinstance(current.get('review'), dict) and current['review'].get('artifact'):
            evidence.append(current['review']['artifact'])
        for e in evidence:
            try:
                if digest(evidence_path(e)) != e['sha256']:
                    return 'STALE — evidence changed; review before use'
                checked += 1
            except (OSError, KeyError, ValueError):
                return 'MISSING/INVALID — review before use'
        refs = current.get('depends_on', []) + current.get('source_refs', []) + current.get('subsumed_by', [])
        refs += [r['id'] for r in all_items.values() if current['id'] in r.get('supersedes', [])]
        for ref in refs:
            if ref not in all_items:
                return 'MISSING/INVALID — unresolved prerequisite'
            queue.append(all_items[ref])
    return f'CURRENT — {checked} recorded evidence hashes match; proof validity not checked'


def walk(root_filter=None):
    for key, root in ROOTS.items():
        if key == 'hub' or (root_filter is not None and key != root_filter):
            continue
        if not root.is_dir():
            raise ValueError(f'Archive root unavailable: {root}')
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in CFG['exclude_dirs']
                             and (not d.startswith('.') or d in CFG.get('include_hidden_dirs', []))
                             and not (Path(directory) / d).is_symlink()
                             and (Path(directory) / d).resolve() != HUB)
            for name in sorted(files):
                path = Path(directory) / name
                if path.is_symlink() or (path.suffix.lower() not in CFG['extensions']
                                         and name not in CFG.get('include_filenames', [])):
                    continue
                yield key, root, path


@command_snapshot
def inventory(root_filter=None):
    entries, duplicates, counts, errors = [], defaultdict(list), Counter(), []
    root_snapshots = {}
    if root_filter is not None and (HUB / 'indexes/inventory.json').exists():
        previous = read_json('indexes/inventory.json')
        entries = [e for e in previous['entries'] if e['root'] != root_filter]
        errors = [e for e in previous.get('errors', []) if not e.startswith(root_filter + ':')]
        root_snapshots = previous.get('root_snapshots', {
            k: previous['generated_at'] for k in previous.get('roots', {})})
    for key, root, path in walk(root_filter):
        try:
            before = path.stat()
            item = dict(root=key, path=path.relative_to(root).as_posix(),
                        bytes=before.st_size, mtime_ns=before.st_mtime_ns,
                        sha256=digest(path), title=path.stem)
            if path.suffix.lower() in {'.md', '.tex', '.txt'}:
                with path.open(errors='replace') as stream:
                    for _, line in zip(range(40), stream):
                        if line.startswith('# '):
                            item['title'] = line[2:].strip()
                            break
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('File changed during inventory; rerun')
            entries.append(item)
        except (OSError, ValueError) as exc:
            errors.append(f'{key}:{path.relative_to(root)}: {exc}')
    stamp = datetime.now(timezone.utc).isoformat()
    for entry in entries:
        duplicates[entry['sha256']].append(entry)
        counts[entry['root']] += 1
    for key in ROOTS:
        if key != 'hub' and (root_filter is None or key == root_filter):
            root_snapshots[key] = stamp
    data = dict(generated_at=stamp, roots={k: str(v) for k, v in ROOTS.items() if k != 'hub'},
                root_snapshots=root_snapshots, entries=entries, errors=errors)
    write('indexes/inventory.json', json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    groups = [v for v in duplicates.values() if len(v) > 1]
    out = ['# Exact duplicate groups', '', 'Generated hash matches; do not delete automatically.', '']
    for group in groups:
        out.append(f"## {group[0]['sha256'][:16]}")
        out.extend([''] + [f"- {x['root']}:{x['path']}" for x in group] + [''])
    write('indexes/DUPLICATES.md', '\n'.join(out))
    coverage = ['# Coverage and limitations', '', f'Inventory generated: {stamp}', '',
                '| Root | Included files |', '|---|---:|']
    coverage += [f'| {k} | {n} |' for k, n in sorted(counts.items())]
    coverage += ['', 'Per-root inventory dates (a scoped refresh preserves other roots):', '']
    coverage += [f'- {k}: {v}' for k, v in sorted(root_snapshots.items())]
    coverage += ['', f'Total: {len(entries)}. Exact duplicate groups: {len(groups)}.', '',
                 'This is file coverage, not proof or literature review coverage.',
                 f'Selected curated records: {len(records())}; literature entries: {len(sources())}.',
                 'Curated counts are a snapshot; rerun inventory after adding records.', '',
                 'Scope: extensions, exclusions and hidden-directory exceptions are explicit',
                 'in hub.json. Source images, HTML, logs, include files, ZIPs, pickle caches',
                 'and notebook checkpoints are inventoried. Generated TeX auxiliaries, runtime',
                 'caches and environments remain excluded. Symlinks are not followed.',
                 'DIRECTORY_SWEEP.md covers its stated roots and snapshot only. Canonical',
                 'projectivization has a separate project coverage map. Unconfigured sibling',
                 'projects remain out of scope. PDFs/DOCX/notebooks are',
                 'hashed and discoverable by filename; content is not extracted. Archive',
                 'full-text search is case-insensitive literal matching, not semantic search.', '',
                 'BRANCH_COVERAGE.md records package dispositions; DOCUMENT_SCREEN.json records',
                 'mechanical result/correction candidates, not mathematical certification.',
                 'The initial pass read selected audits, ledgers, corrections and handoffs.',
                 'All mathematical records imported here retain their imported evidence level.',
                 'The toric inventory helper was read-only; no underlying computations were rerun.',
                 'No comprehensive current literature search was performed in this setup task.', '',
                 f'Inventory read errors: {len(errors)}.']
    coverage += ['- ' + e for e in errors]
    write('indexes/COVERAGE.md', '\n'.join(coverage) + '\n')
    print(f'Indexed {len(entries)} files: {dict(counts)}; {len(groups)} duplicate groups; {len(errors)} errors.')
    return bool(errors)


def format_record(r):
    lines = [f"## {r['id']} — {r['title']}", '',
             f"Owner: {project_of(r)}; evidence freshness: {evidence_state(r)}", '',
             f"{r['kind']} | status: {r['status']} | evidence: {r['evidence_level']} | novelty: {r['novelty']}",
             f"Topics: {', '.join(r['topics'])}", '', r['statement'], '', '**Hypotheses/scope**', '']
    lines += ['- ' + x for x in r['hypotheses']]
    lines += ['', '**Limits**', '', r['limitations'], '', '**Evidence**', '']
    lines += ['- ' + link(x) for x in r['evidence']]
    for field in ['depends_on', 'supersedes', 'subsumed_by', 'source_refs']:
        if r[field]:
            lines += ['', f"{field}: {', '.join(r[field])}"]
    corrections = [x for x in records() if r['id'] in x['supersedes']]
    if corrections:
        lines += ['', '**Read the corrections:** ' + '; '.join(f"{x['id']} — {x['title']}" for x in corrections)]
    if r.get('reopening_condition'):
        lines += ['', 'Reopen only with: ' + r['reopening_condition']]
    return '\n'.join(lines)


@command_snapshot
def render(project=None):
    if project is not None and project not in CFG['projects']:
        raise ValueError(f'Unknown project {project}')
    stamp = datetime.now(timezone.utc).isoformat()
    inputs = snapshot_hashes()
    signature = cs.sha256_text(json.dumps(inputs, sort_keys=True))
    stamp_text = f'Generated {stamp}; input snapshot {signature}. Navigation only; use show/check for current evidence.'
    def catalog_text(items, title):
        return '# ' + title + '\n\n' + ('Import earlier work using PROMPTS.md; no claims recorded yet.\n\n' if not items else '') + stamp_text + '\n\n' + '\n\n'.join(format_record(r) for r in items) + '\n'
    def literature_text(items, title):
        out = ['# ' + title, '', stamp_text, 'Source checks retain their exact scope.', '']
        for item in items:
            out += [f"## {item['id']} — {item['citation']}", '',
                    f"Owner: {project_of(item)}; status: {item['check_state']}; version: {item['version']}", '',
                    f"[Source link]({item['url']})", '', f"Locator: {item['locator']}", '',
                    'Use: ' + item['use'], '', 'Limits: ' + item['limitations'], '']
            out += ['- ' + link(e) for e in item['evidence']] + ['']
        return '\n'.join(out)
    if project is None:
        write('indexes/CATALOG.md', catalog_text(records(), 'Research catalog'))
        write('indexes/LITERATURE.md', literature_text(sources(), 'Literature map'))
        write('indexes/LIBRARY.md', lib.render_catalog(sources(), stamp_text))
        write('indexes/ACQUISITIONS.md', lib.render_gaps(sources(), stamp_text))
        if hasattr(_CAT, 'knowledge'):
            aids = [a for envelope in _CAT.knowledge.values() for a in envelope.get('aids', [])]
            write('indexes/KNOWLEDGE.md', '# Research aids\n\n' + stamp_text +
                  '\n\nResearch memory; no theorem certification.\n\n' +
                  '\n'.join(f'- {a["id"]} [{a["project"]}] {a["kind"]}: {a["title"]} (revision {a["revision"]})' for a in aids) + '\n')
        write('indexes/INPUTS.json', json.dumps(dict(generated_at=stamp, inputs=inputs), indent=2) + '\n')
    for name, info in CFG['projects'].items():
        if project is not None and name != project:
            continue
        own = [r for r in records() if project_of(r) == name]
        owned_sources = [r for r in sources() if project_of(r) == name]
        needed, foreign, unresolved = cs.closure(_CAT, name)
        if unresolved:
            raise ValueError(f'Unresolved references: {unresolved}')
        imported = [r for r in records() if r['id'] in foreign]
        text = catalog_text(own, info['title'] + ' catalog')
        if imported:
            text += '\n# Foreign prerequisites and corrections (read-only)\n\n'
            text += '\n\n'.join(format_record(r) for r in imported) + '\n'
        write(f'projects/{name}/indexes/CATALOG.md', text)
        shared_sources = [r for r in sources() if r['id'] in foreign]
        write(f'projects/{name}/indexes/LITERATURE.md', literature_text(owned_sources + shared_sources, info['title'] + ' sources'))
        write(f'projects/{name}/indexes/LIBRARY.md', lib.render_catalog(owned_sources + shared_sources, stamp_text))
        write(f'projects/{name}/indexes/ACQUISITIONS.md', lib.render_gaps(owned_sources + shared_sources, stamp_text))
        if hasattr(_CAT, 'knowledge'):
            envelope = _CAT.knowledge.get(name, {})
            aids = envelope.get('aids', [])
            write(f'projects/{name}/indexes/KNOWLEDGE.md', '# ' + info['title'] +
                  ' research aids\n\n' + stamp_text + '\n\nResearch memory; no theorem certification.\n\n' +
                  '\n'.join(f'- {a["id"]} {a["kind"]}: {a["title"]} (revision {a["revision"]})' for a in aids) +
                  f'\n\n{len(envelope.get("sessions", []))} saved session contexts; use context --resume for scoped recovery.\n')
        current_readings = set()
        for source in owned_sources:
            if source.get('library', {}).get('kind') == 'virtual':
                relative = f'projects/{name}/indexes/reading/{source["id"]}.md'
                current_readings.add(relative)
                write(relative, reading_text(source, stamp_text))
        for old in (HUB / f'projects/{name}/indexes/reading').glob('*.md'):
            relative = old.relative_to(HUB).as_posix()
            if relative not in current_readings:
                write(relative, '# Retired generated reading view\n\n' + stamp_text +
                      f'\n\n{old.stem} is no longer a virtual collection owned by this project. '
                      f'Use `hub.py show {old.stem}` for its current disposition.\n')
        write(f'projects/{name}/indexes/INPUTS.json', json.dumps(dict(
            generated_at=stamp, project=name, inputs=inputs, foreign_ids=sorted(foreign)), indent=2) + '\n')
    if project is None and _EXTRA.get('catalog/coverage.json') is not None:
        import coverage_map
        text = coverage_map.render_text(read_json('catalog/coverage.json'),
                                        read_json('indexes/inventory.json'), records())
        write('indexes/BRANCH_COVERAGE.md', text)
    print(f'Rendered {project or "all projects"}: {len(records())} global records and {len(sources())} sources.')


@command_snapshot
def check(project=None):
    ids, foreign = None, set()
    if project is not None:
        ids, foreign, unresolved = cs.closure(_CAT, project)
    errors = cv.validate(_CAT, HUB, ids)
    for path in sorted((HUB / '.codex').rglob('*.toml')):
        try:
            with path.open('rb') as stream:
                data = tomllib.load(stream)
            if path.parent.name == 'agents' and not all(data.get(f) for f in ['name', 'description', 'developer_instructions']):
                errors.append(f'{path}: missing role fields')
        except (OSError, tomllib.TOMLDecodeError) as exc:
            errors.append(str(exc))
    if _EXTRA.get('catalog/coverage.json') is not None:
        import coverage_map
        data, inv = read_json('catalog/coverage.json'), read_json('indexes/inventory.json')
        errors.extend(coverage_map.reconcile(data, inv, records(), CFG)[0])
        print(f'Coverage checked: {len(inv["entries"])} paths, {len(data["units"])} units.')
    if CFG.get('schema_version', 1) >= 2:
        for name in CFG['projects']:
            for kind, rel in cs.project_paths(name).items():
                if kind != 'investigations' and not (HUB / rel).is_file():
                    errors.append(f'{name}: missing {kind} route: {rel}')
    if errors:
        print('\n'.join(errors))
    else:
        scope = f'{project}: {len(ids)} evidence-closure IDs ({len(foreign)} foreign)' if project else 'full hub evidence'
        print(f'PASS: {len(records())} records, {len(sources())} sources; global structure and {scope}.')
        if project:
            print('Evidence outside this closure was not checked.')
        print('Catalog integrity is not proof certification or an agent-runtime loading test.')
    return bool(errors)


def selector_matches(selector, root, relative):
    if selector['root'] != root:
        return False
    base, kind = selector['path'].rstrip('/'), selector['selector']
    if kind == 'file':
        return relative == base
    if kind == 'directory':
        return str(Path(relative).parent) == (base or '.')
    return not base or relative.startswith(base + '/')


def archive_scope(root, relative, project, broad=False):
    if project is None or broad or CFG.get('schema_version', 1) < 2:
        return 'broad' if broad else 'own'
    info = CFG['projects'][project]
    if any(selector_matches(s, root, relative) for s in info.get('shared_selectors', [])):
        return 'shared'
    if any(selector_matches(s, root, relative) for s in info.get('exclude_selectors', [])):
        return None
    return 'own' if any(selector_matches(s, root, relative) for s in info.get('archive_selectors', [])) else None


def document_paths(project=None, infrastructure=False):
    routes = []
    if infrastructure or project is None:
        routes += [(p, 'infrastructure') for p in CFG.get('infrastructure_routes', [])]
    if not infrastructure:
        for name, info in CFG['projects'].items():
            if project is None or name == project:
                routes += [(p, name) for p in info.get('document_routes', [f'projects/{name}'])]
    skip_dirs = {'indexes', 'catalog', 'archives', 'migration-baseline', 'migration-stage', 'staging', '__pycache__', 'consultations', 'node_modules'}
    seen = set()
    for route, owner in routes:
        path = tx.safe_path(HUB, route)
        candidates = [path] if path.is_file() else []
        if path.is_dir():
            for directory, dirs, names in os.walk(path, followlinks=False):
                dirs[:] = sorted(d for d in dirs if d not in skip_dirs and not d.startswith('.') and not (Path(directory) / d).is_symlink())
                candidates += [Path(directory) / n for n in sorted(names)]
        for candidate in candidates:
            if candidate in seen or candidate.is_symlink() or not candidate.is_file():
                continue
            if candidate.suffix.lower() not in {'.md', '.tex'}:
                continue
            if re.search(r'(?:PROMPT|RAW|STDERR|STREAM|EXECUTION|RESPONSE)', candidate.name, re.I):
                continue
            seen.add(candidate)
            yield candidate, owner


@command_snapshot
def search(query, archive=False, limit=15, root_filter=None, project_filter=None,
           broad_archive=False, infrastructure=False):
    needle, count = query.casefold(), 0
    if project_filter is not None and project_filter not in CFG['projects']:
        raise ValueError(f'Unknown project {project_filter}')
    foreign = set()
    if project_filter:
        _, foreign, _ = cs.closure(_CAT, project_filter)
    matched = [r for r in records() + sources()
               if not infrastructure and (project_filter is None or project_of(r) == project_filter or r['id'] in foreign)
               and needle in json.dumps(r, ensure_ascii=False).casefold()]
    for item in matched[:limit]:
        label = 'foreign-prerequisite' if project_filter and project_of(item) != project_filter else 'own'
        print(f"CATALOG {item['id']} [{project_of(item)}; {label}]: {item.get('title', item.get('citation'))}")
        print('  ' + evidence_state(item))
        for correction in records():
            if item['id'] in correction.get('supersedes', []):
                print(f"  CORRECTION {correction['id']} [{project_of(correction)}]: {correction['title']}")
        count += 1
    if len(matched) > limit:
        print(f'Catalog truncated: {len(matched)} matches; showing {limit}.')
    hits, truncated = Counter(), set()
    def emit(key, line):
        nonlocal count
        hits[key] += 1
        if hits[key] <= limit:
            print(line)
            count += 1
        else:
            truncated.add(key)
    docs_seen = 0
    if root_filter is None or root_filter == 'hub':
        for path, owner in document_paths(project_filter, infrastructure):
            docs_seen += 1
            key = 'documents:' + owner
            if key in truncated:
                continue
            with path.open(errors='replace') as stream:
                for number, line in enumerate(stream, 1):
                    if needle in line.casefold():
                        emit(key, f'DOC [{owner}] hub:{path.relative_to(HUB)}:{number}: {line.strip()[:280]}')
                        if key in truncated:
                            break
        print(f'Searched project/infrastructure documents: {docs_seen} files; generated indexes and raw consultation streams excluded.')
    project_roots = CFG['projects'][project_filter]['roots'] if project_filter else ROOTS
    selected = [] if infrastructure else [k for k in ROOTS if k != 'hub' and k in project_roots and (root_filter is None or k == root_filter)]
    excluded = Counter()
    if archive or broad_archive:
        for key in selected:
            for _, root, path in walk(key):
                relative = path.relative_to(root).as_posix()
                label = archive_scope(key, relative, project_filter, broad_archive)
                if label is None:
                    excluded[key] += 1
                    continue
                if key in truncated or (path.suffix.lower() not in CFG['text_extensions'] and path.name not in CFG.get('text_filenames', [])):
                    continue
                try:
                    with path.open(errors='replace') as stream:
                        for number, line in enumerate(stream, 1):
                            if needle in line.casefold():
                                emit(key, f'{key}:{relative}:{number}: [{label}] {line.strip()[:280]}')
                                if key in truncated:
                                    break
                except OSError as exc:
                    print(f'Could not read {path}: {exc}', file=sys.stderr)
    elif selected:
        raw = _EXTRA.get('indexes/inventory.json')
        if raw is None:
            print('No saved archive inventory; use --archive for live text.')
        else:
            inv = json.loads(raw)
            print('Archive inventory assembled: ' + inv['generated_at'])
            for item in inv['entries']:
                if item['root'] not in selected:
                    continue
                label = archive_scope(item['root'], item['path'], project_filter)
                if label is None:
                    excluded[item['root']] += 1
                elif needle in (item['path'] + ' ' + item['title']).casefold():
                    emit(item['root'], f"FILE {item['root']}:{item['path']} [{label}] — {item['title']}")
    for key in selected:
        print(f"Searched {key} ({'live text' if archive or broad_archive else 'inventory metadata'}): {min(hits[key], limit)} shown; {excluded[key]} paths outside selected routes."
              + (' TRUNCATED; narrow query or increase --limit.' if key in truncated else ''))
    for key in sorted(truncated):
        if key.startswith('documents:'):
            print(f'{key}: TRUNCATED; narrow query or increase --limit.')
    if project_filter and not broad_archive:
        print('Archive routing is a relevance filter, not exhaustive coverage. Use --broad-archive for all associated archive paths.')
    if not count:
        print('No literal matches in the stated scope. Try synonyms or --broad-archive; this is not a novelty check.')


@command_snapshot
def impact(key):
    rs = records()
    if key not in {r['id'] for r in rs + sources()}:
        raise ValueError(f'Unknown record {key}')
    seen, queue = {key}, [key]
    origin = next((r for r in rs if r['id'] == key), {})
    for target in origin.get('supersedes', []):
        print(f'{target} — superseded by correction {key}; review this claim and its dependents')
        seen.add(target)
        queue.append(target)
    while queue:
        target = queue.pop(0)
        for r in rs:
            relations = [name for name in ['depends_on', 'supersedes', 'subsumed_by', 'source_refs'] if target in r[name]]
            if relations:
                print(f"{r['id']} — {','.join(relations)} {target}: {r['title']}")
                if r['id'] not in seen:
                    seen.add(r['id'])
                    queue.append(r['id'])
    if len(seen) == 1:
        print('No recorded dependents. The imported dependency graph is incomplete.')


@command_snapshot
def sweep():
    import directory_sweep
    directory_sweep.HUB = HUB
    return directory_sweep.run(publisher=write, snapshot=(CFG, read_json('indexes/inventory.json'), _CAT))


@command_snapshot
def show(key, structured=False):
    from recall.retrieve import _current_context
    for item in records() + sources():
        if item['id'] == key:
            if structured:
                print(json.dumps(dict(entry=item,entry_hash=__import__("kit").entry_hash(item),current_context=_current_context(_CAT,[key])),ensure_ascii=False,indent=2))
            else:
                print(format_record(item) if 'kind' in item else json.dumps(item, ensure_ascii=False, indent=2))
            return 0
    if hasattr(_CAT, 'knowledge_by_id') and key in _CAT.knowledge_by_id():
        item = _CAT.knowledge_by_id()[key]
        refs = [r['id'] for r in item.get('references',[]) if isinstance(r,dict)]
        result = dict(entry=item,current_context=_current_context(_CAT,refs),
                      notice='Research aid or operational context; no theorem authority or automatic applicability certification.')
        if structured:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            from recall.packets import view
            from recall.cli import emit
            current = result['current_context']
            emit(dict(entry=view(item), references=item.get('references',[]),
                      provenance=item.get('provenance',[]),
                      current_corrections=[view(row,compact=True) for row in current['corrections']],
                      prerequisite_ids=[row['id'] for row in current['prerequisites']],
                      unresolved_ids=current['unresolved'],
                      details=f'hub.py show {key} --json', notice=result['notice']))
        return 0
    raise ValueError(f'Unknown ID {key}')


@command_snapshot
def export_snapshot(project):
    if project not in CFG['projects'] and project != 'hub':
        raise ValueError('Unknown project')
    print(json.dumps(dict(project=project, expected=snapshot_hashes(),
        records=[r for r in records() if project_of(r) == project],
        sources=[s for s in sources() if project_of(s) == project],
        knowledge=getattr(_CAT, 'knowledge', {}).get(project, {})), ensure_ascii=False, indent=2))


@command_snapshot
def view_status(project):
    if project not in CFG['projects']:
        raise ValueError('Unknown project')
    path = HUB / f'projects/{project}/indexes/INPUTS.json'
    if not path.exists():
        print('MISSING: render this project'); return 1
    info = json.loads(path.read_bytes())
    changed = [name for name, h in info['inputs'].items() if tx.digest(tx.read_bytes(HUB / name)) != h]
    print('STALE: ' + ', '.join(changed) if changed else 'CURRENT input versions; evidence freshness still requires check/show.')
    return bool(changed)


@command_snapshot
def context_packet(project, role):
    if project not in CFG['projects']:
        raise ValueError('Unknown project')
    paths = ['AGENTS.md', f'projects/{project}/AGENTS.md']
    if role == 'conductor':
        paths += [f'projects/{project}/START_HERE.md', f'projects/{project}/TOPICS.md']
    print(json.dumps(dict(project=project, role=role, paths=paths,
        library=f'projects/{project}/indexes/LIBRARY.md (query on demand; do not preload)',
        claude_entry=f'projects/{project}/CLAUDE.md',
        helper_limit='Named inputs only; no campaign history or implicit goal resumption.'), indent=2))


@command_snapshot
def library(query='', project=None, limit=15, gaps=False):
    if project is not None and project not in CFG['projects']:
        raise ValueError('Unknown project')
    items = sources()
    if project:
        _, foreign, _ = cs.closure(_CAT, project)
        items = [s for s in items if project_of(s) == project or s['id'] in foreign]
    matches = lib.select(items, query)
    if gaps:
        matches = [s for s in matches if lib.needs_retention_review(s)]
    for source in matches[:limit]:
        print(f"{source['id']} [{project_of(source)}; {source['check_state']}] {source['citation']}")
        print('  ' + lib.availability(source))
        note = source.get('library', {}).get('availability_note')
        if note:
            print('  ' + note)
        if gaps:
            print('  Next: ' + lib.retention_issue(source))
        for c in source.get('library', {}).get('copies', []):
            print(f"  {c['role']}: {c['root']}:{c['path']}")
    print(f'{len(matches)} matching source entries; {min(len(matches), limit)} shown. Entries can be separate uses of one work.')
    print('Metadata lookup only; use show/check for evidence freshness. Unindexed does not mean absent.')


def reading_text(source, stamp='Live canonical snapshot'):
    if source.get('library', {}).get('kind') != 'virtual':
        raise ValueError('reading requires a virtual source collection')
    by_id = {r['id']: r for r in records()}
    lines = [f"# {source['citation']}", '', stamp, '', source['use'], '',
             'Internal reading collection, not a paper or a proof of the combination. Membership is navigation, not logical dependence.',
             'Limits: ' + source['limitations'], '']
    for member in source['library']['records']:
        record = by_id[member['id']]
        lines += [f"Selection {member['id']}: {lib.member_state(member, by_id)}", '',
                  format_record(record), '']
        review = record.get('review')
        if isinstance(review, dict):
            lines += [f"Review scope ({review.get('date', 'date unknown')}): {review.get('scope', 'not recorded')}", '']
            if isinstance(review.get('artifact'), dict):
                lines += ['Review artifact: ' + link(review['artifact']), '']
    return '\n'.join(lines) + '\n'


@command_snapshot
def reading(key):
    source = next((s for s in sources() if s['id'] == key), None)
    if source is None:
        raise ValueError('Unknown source ID')
    print(reading_text(source))


def main():
    global HUB, CFG, ROOTS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hub-dir', type=Path, help='Explicit hub directory (also used for fixture validation)')
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ['render', 'check']:
        sub.add_parser(command).add_argument('--project')
    sub.add_parser('sweep')
    inv = sub.add_parser('inventory')
    inv.add_argument('--root')
    find = sub.add_parser('search')
    find.add_argument('query')
    find.add_argument('--archive', '--archives', action='store_true')
    find.add_argument('--broad-archive', action='store_true')
    find.add_argument('--infrastructure', action='store_true')
    find.add_argument('--limit', type=int, default=15)
    find.add_argument('--root')
    find.add_argument('--project')
    cmd = sub.add_parser('library', help='Find papers, textbooks and virtual reading collections')
    cmd.add_argument('query', nargs='?', default='')
    cmd.add_argument('--project')
    cmd.add_argument('--limit', type=int, default=15)
    cmd.add_argument('--gaps', action='store_true', help='Sources without an identified full text; not necessarily absent')
    for command in ['show', 'impact', 'reading']:
        cmd = sub.add_parser(command)
        cmd.add_argument('id')
        if command == 'show':
            cmd.add_argument('--json', action='store_true')
    for command in ['snapshot', 'view-status', 'context']:
        cmd = sub.add_parser(command)
        cmd.add_argument('--project', required=command != 'context')
        if command == 'context':
            cmd.add_argument('--role', choices=['conductor', 'helper'], default='conductor')
            cmd.add_argument('--resume', action='store_true')
            cmd.add_argument('--service', choices=['codex', 'claude-code'])
            cmd.add_argument('--thread')
            cmd.add_argument('--query', default='')
            cmd.add_argument('--json', action='store_true')
    cmd = sub.add_parser('apply')
    cmd.add_argument('batch', type=Path)
    cmd.add_argument('--scope', action='append', required=True)
    cmd.add_argument('--owner', required=True)
    cmd = sub.add_parser('reserve-id')
    cmd.add_argument('--project', required=True)
    cmd.add_argument('--kind', required=True, choices=list('ROCQ LK'.replace(' ', '')))
    cmd.add_argument('--owner', required=True)
    cmd = sub.add_parser('add-project')
    cmd.add_argument('name')
    cmd.add_argument('--title', required=True)
    cmd.add_argument('--prefix', required=True)
    cmd.add_argument('--description', default='')
    cmd.add_argument('--conventions', default='')
    cmd = sub.add_parser('recover')
    cmd.add_argument('transaction')
    cmd.add_argument('--direction', choices=['rollback', 'finish'], default='rollback')
    from recall import cli as recall_cli
    import kit
    kit.configure(sub)
    recall_cli.configure(sub)
    args = parser.parse_args()
    if args.hub_dir:
        HUB = args.hub_dir.resolve()
    if args.command in kit.COMMANDS:
        return kit.dispatch(HUB, args)
    if args.command in recall_cli.COMMANDS or (args.command == 'context' and args.resume):
        return recall_cli.dispatch(HUB, args)
    # Recovery must work even while hub.json is one of the interrupted files.
    if args.command == 'recover':
        print(tx.recover(HUB, args.transaction, args.direction)); return 0
    if args.command in {'apply', 'reserve-id', 'add-project'}:
        import hub_operations as ops
        if args.command == 'apply':
            result = ops.apply(HUB, json.loads(args.batch.read_bytes()), args.scope, args.owner)
        elif args.command == 'reserve-id':
            result = ops.reserve_id(HUB, args.project, args.kind, args.owner)
        else:
            result = ops.add_project(HUB, args.name, args.title, args.prefix)
        print(json.dumps(result, indent=2)); return 0
    if args.command == 'search':
        if args.limit < 1 or not args.query.strip():
            parser.error('query must be nonempty and limit must be positive')
        if args.infrastructure and args.project:
            parser.error('--infrastructure and --project are separate scopes')
        return search(args.query, args.archive, args.limit, args.root, args.project,
                      args.broad_archive, args.infrastructure) or 0
    if args.command == 'library':
        if args.limit < 1:
            parser.error('limit must be positive')
        return library(args.query, args.project, args.limit, args.gaps) or 0
    if args.command == 'inventory':
        cfg = cs.load_catalog(HUB).cfg
        if args.root and args.root not in cfg['roots']:
            parser.error('Unknown root')
        return inventory(args.root) or 0
    if args.command in {'check', 'render'}:
        return globals()[args.command](args.project) or 0
    if args.command == 'sweep':
        return sweep() or 0
    if args.command == 'snapshot':
        return export_snapshot(args.project) or 0
    if args.command == 'view-status':
        return view_status(args.project) or 0
    if args.command == 'context':
        return context_packet(args.project, args.role) or 0
    if args.command == 'show':
        return show(args.id, args.json) or 0
    return globals()[args.command](args.id) or 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)
