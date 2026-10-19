"""Versioned, explicitly routed recall corpus. Nothing here publishes evidence.

A frozen manifest pins all eligible files, canonical objects and exclusions. Text
is never normalized in a chunk: the derived index owns ranking normalization.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import catalog_store as cs

VERSION = 1
PARSER_VERSION = 'recall-text-v1'
TEXT_EXTENSIONS = {'.md', '.tex', '.txt', '.bib', '.html', '.htm'}
SKIP_DIRS = {'indexes', 'catalog', 'archives', 'migration-baseline', 'migration-stage',
             'staging', '__pycache__', 'consultations', 'node_modules', 'implementation',
             'baseline', 'benchmark', 'benchmarks', 'benchmark-output', 'benchmark-outputs',
             'raw', '.git', '.venv', 'venv'}
RAW_NAME = re.compile(r'(?:^|[_.-])(?:PROMPT|RAW|STDERR|STREAM|EXECUTION|RESPONSE)(?:[_.-]|$)', re.I)


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(value).hexdigest()


def _root(hub, cfg, name):
    if name == 'hub':
        return Path(hub).resolve()
    if name not in cfg.get('roots', {}):
        raise ValueError(f'Unknown corpus root {name!r}')
    return Path(cfg['roots'][name]).expanduser().resolve()


def resolve(hub, cfg, root, path):
    base = _root(hub, cfg, root)
    rel = Path(path)
    if rel.is_absolute() or '..' in rel.parts:
        raise ValueError(f'Corpus path escapes root: {root}:{path}')
    candidate = base / rel
    if not candidate.resolve().is_relative_to(base):
        raise ValueError(f'Corpus symlink escapes root: {root}:{path}')
    return candidate


def _excluded(path, directory=False):
    return any(p in SKIP_DIRS or p.startswith('.') for p in path.parts) or bool(RAW_NAME.search(path.name))


def _walk(path, base, excluded, recursive=True, allow_pdf=False):
    if path.is_file():
        candidates = [path]
    elif path.is_dir():
        candidates = []
        for directory, dirs, names in os.walk(path, followlinks=False):
            kept = []
            for name in sorted(dirs):
                child = Path(directory) / name
                if child.is_symlink() or _excluded(child.relative_to(base), True):
                    excluded['directory-policy'] += 1
                elif recursive:
                    kept.append(name)
            dirs[:] = kept
            candidates.extend(Path(directory) / name for name in sorted(names))
    else:
        return
    for candidate in candidates:
        relative = candidate.relative_to(base)
        # Archive root itself may be named archives; only its relative path matters.
        if candidate.is_symlink() or _excluded(relative):
            excluded['file-policy'] += 1
            continue
        if candidate.is_file() and candidate.suffix.lower() in TEXT_EXTENSIONS | ({'.pdf'} if allow_pdf else set()):
            yield candidate
        elif candidate.is_file():
            excluded['unsupported-extension'] += 1


def canonical_chunk(catalog, entry, knowledge=False):
    key = entry['id']
    owner = entry.get('project', cs.default_project(catalog.cfg))
    path = f'projects/{owner}/catalog/knowledge.json' if knowledge else catalog.location[key][1]
    if owner == 'hub' and knowledge:
        path = 'catalog/knowledge.json'
    text = json.dumps(entry, ensure_ascii=False, indent=2)
    return dict(id=digest(['knowledge' if knowledge else 'canonical', key]), owner=owner,
                tier='knowledge' if knowledge else 'canonical', source_id=key, root='hub',
                path=path, locator=f'JSON object id={key}', title=entry.get('title', entry.get('citation', key)),
                text=text, sha256=digest(entry), metadata={'original': entry,
                'original_scope': owner, 'projection': 'complete canonical JSON object'})


def _routes(selection, name):
    routes = selection.get(name, [])
    if isinstance(routes, (str, dict)):
        routes = [routes]
    if not isinstance(routes, list):
        raise ValueError(f'{name} selection must be a list')
    return routes


def _selector_matches(selector, root, path):
    if selector.get('root') != root:
        return False
    base = selector.get('path', '').rstrip('/')
    kind = selector.get('selector', 'subtree')
    if kind == 'file':
        return path == base
    if kind == 'directory':
        return str(Path(path).parent) == (base or '.')
    return not base or path == base or path.startswith(base + '/')


def _inferred_owners(cfg, root, path):
    owners = []
    for owner, info in cfg['projects'].items():
        shared = any(_selector_matches(s, root, path) for s in info.get('shared_selectors', []))
        excluded = any(_selector_matches(s, root, path) for s in info.get('exclude_selectors', []))
        selected = any(_selector_matches(s, root, path) for s in info.get('archive_selectors', []))
        if shared or (selected and not excluded):
            owners.append(owner)
    return owners or ['hub']


def freeze_manifest(hub, selection=None):
    """Capture all canonical/project documents plus explicitly selected full text.

    selection: archives/literature lists of {root,path,owner?,recursive?}; literature
    also accepts source IDs (expands registered copies). No arbitrary archive scan.
    A route may set selector=file/directory/subtree; its original owners are kept.
    """
    hub = Path(hub).resolve()
    catalog = cs.load_catalog(hub)
    cfg = catalog.cfg
    selection = selection or {}
    if not isinstance(selection, dict):
        raise ValueError('Corpus selection must be an object')
    objects = [canonical_chunk(catalog, e) for e in catalog.entries()]
    for owner, envelope in getattr(catalog, 'knowledge', {}).items():
        # Operational sessions/checkpoints are deliberately not research corpus.
        objects.extend(canonical_chunk(catalog, e, True) for e in envelope.get('aids', []))
    files, gaps, deferred, excluded, seen = [], [], [], Counter(), set()
    routes = []
    for owner, info in cfg['projects'].items():
        for path in info.get('document_routes', [f'projects/{owner}']):
            routes.append(dict(root='hub', path=path, owner=owner, tier='documents', recursive=True))
        for route in info.get('archive_selectors', []):
            deferred.append(dict(route, owner=owner, tier='archives', reason='not scanned unless explicitly selected'))
    for path in cfg.get('infrastructure_routes', []):
        routes.append(dict(root='hub', path=path, owner='hub', tier='documents', recursive=True))
    by_id = catalog.by_id()
    for tier in ('archives', 'literature'):
        for raw in _routes(selection, tier):
            if isinstance(raw, str) and raw in by_id and tier == 'literature':
                source = by_id[raw]
                copies = source.get('library', {}).get('copies', [])
                if not copies:
                    gaps.append(dict(tier=tier, source_id=raw, reason='no registered retained copies'))
                for copy in copies:
                    routes.append(dict(copy, source_id=raw, owner=catalog.project_of(source), tier=tier))
                continue
            if isinstance(raw, str):
                if ':' not in raw:
                    raise ValueError('Explicit corpus path must be root:path or a literature ID')
                root, path = raw.split(':', 1)
                raw = dict(root=root, path=path)
            if not isinstance(raw, dict) or 'path' not in raw:
                raise ValueError('Explicit corpus route needs root and path')
            route = dict(raw, tier=tier)
            route.setdefault('root', 'hub')
            route.setdefault('owner', route.get('project'))
            if route.get('source_id') in by_id and route['owner'] is None:
                route['owner'] = catalog.project_of(by_id[route['source_id']])
            if route['owner'] is not None and route['owner'] != 'hub' and route['owner'] not in cfg['projects']:
                raise ValueError(f'Unknown corpus owner {route["owner"]}')
            routes.append(route)
    for route in routes:
        root, relative, owner, tier = (route[k] for k in ('root', 'path', 'owner', 'tier'))
        base = _root(hub, cfg, root)
        path = resolve(hub, cfg, root, relative)
        if not path.exists():
            gaps.append(dict(route, reason='selected route missing'))
            continue
        recursive = route.get('recursive', route.get('selector') != 'directory')
        for candidate in _walk(path, base, excluded, recursive, allow_pdf=tier in {'literature', 'archives'}):
            rel = candidate.relative_to(base).as_posix()
            try:
                data = candidate.read_bytes()
                stat = candidate.stat()
            except OSError as exc:
                gaps.append(dict(route, path=rel, reason=f'unreadable: {exc}'))
                continue
            owners = [owner] if owner is not None else _inferred_owners(cfg, root, rel)
            for original_owner in owners:
                identity = (original_owner, tier, root, rel, route.get('source_id'))
                if identity in seen:
                    continue
                seen.add(identity)
                files.append(dict(root=root, path=rel, owner=original_owner, tier=tier,
                    source_id=route.get('source_id', f'{root}:{rel}'), sha256=digest(data),
                    bytes=len(data), mtime_ns=stat.st_mtime_ns, title=route.get('title', candidate.stem),
                    metadata=dict({k: v for k, v in route.items() if k not in {'root', 'path', 'owner', 'tier'}},
                                  ownership_basis='explicit route' if owner is not None else 'manifest archive/shared selectors')))
    # A whole selected route is eligible; only its unselected surroundings remain
    # deferred. Never imply that a few selected proofs exhaust their archive root.
    remaining = []
    for route in deferred:
        covered, subsets = False, []
        for chosen in routes:
            if chosen['tier'] != 'archives' or chosen['root'] != route['root']:
                continue
            if chosen.get('owner') not in (None, route['owner']):
                continue
            base = chosen['path'].rstrip('/')
            if route['path'] == base or (chosen.get('recursive', chosen.get('selector') != 'directory') and
                    (not base or route['path'].startswith(base + '/'))):
                covered = True
            elif _selector_matches(route, chosen['root'], chosen['path']):
                subsets.append(chosen['path'])
        if not covered:
            remaining.append(dict(route, eligible_subsets=subsets,
                                  reason='archive route extends beyond explicit eligible selections'))
    deferred = remaining
    # Unselected retained copies are disclosed as deferred references, not indexed text.
    selected_copies = {(f['root'], f['path']) for f in files if f['tier'] == 'literature'}
    for source in catalog.sources:
        for copy in source.get('library', {}).get('copies', []):
            if (copy['root'], copy['path']) not in selected_copies:
                deferred.append(dict(tier='literature', source_id=source['id'], root=copy['root'],
                                     path=copy['path'], reason='retained copy not selected'))
    files.sort(key=lambda f: (f['tier'], f['owner'], f['root'], f['path'], f['source_id']))
    manifest = dict(schema_version=VERSION, parser_version=PARSER_VERSION,
        created_at=datetime.now(timezone.utc).isoformat(), configuration=cfg,
        catalog_hashes=catalog.file_hashes, selection=selection, routes=routes,
        objects=objects, files=files, gaps=gaps, deferred=deferred, exclusions=dict(excluded),
        eligible=dict(Counter(o['tier'] for o in objects) + Counter(f['tier'] for f in files)),
        limits=['Only selected archive/literature routes are eligible; no claim of complete archives.',
                'Canonical projection preserves reported status; retrieval does not certify a claim.'])
    manifest['id'] = digest({k: v for k, v in manifest.items() if k != 'created_at'})
    return manifest


def _text_chunks(text, size=6500):
    """Exact non-overlapping slices with one-based line/character locators."""
    lines = text.splitlines(keepends=True)
    start, parts, length = 1, [], 0
    for number, line in enumerate(lines, 1):
        if parts and (length + len(line) > size or len(parts) >= 100):
            yield ''.join(parts), f'lines {start}-{number - 1}'
            parts, length = [], 0
        if len(line) > size:
            for offset in range(0, len(line), size):
                yield line[offset:offset + size], f'line {number}, characters {offset + 1}-{min(offset + size, len(line))}'
            start = number + 1
            continue
        if not parts:
            start = number
        parts.append(line)
        length += len(line)
    if parts:
        yield ''.join(parts), f'lines {start}-{len(lines)}'


def parse_file(hub, manifest, descriptor):
    path = resolve(hub, manifest['configuration'], descriptor['root'], descriptor['path'])
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValueError(f'File unavailable: {path}: {exc}') from exc
    if digest(data) != descriptor['sha256']:
        raise ValueError(f'File changed after manifest freeze: {descriptor["root"]}:{descriptor["path"]}')
    if path.suffix.lower() == '.pdf':
        executable = shutil.which('pdftotext')
        if not executable:
            raise ValueError('PDF parser unavailable: install pdftotext and refresh; no PDF scan occurs during queries')
        try:
            # Extract the already hashed bytes, so a concurrent source edit cannot
            # produce new text carrying an old source hash.
            with tempfile.NamedTemporaryFile(suffix='.pdf') as frozen:
                frozen.write(data)
                frozen.flush()
                result = subprocess.run([executable, '-layout', '-enc', 'UTF-8', frozen.name, '-'],
                                        capture_output=True, timeout=90, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(f'PDF extraction failed: {exc}') from exc
        text = result.stdout.decode('utf-8', errors='strict')
        if not text.strip():
            raise ValueError('PDF has no extractable text; OCR is not configured')
        units = [(page, f'PDF page {i}; ') for i, page in enumerate(text.split('\f'), 1)]
    else:
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise ValueError('Text parser requires UTF-8; original bytes retained, explicit parser gap') from exc
        units = [(text, '')]
    chunks = []
    for text, prefix in units:
        for content, locator in _text_chunks(text):
            locator = prefix + locator
            chunks.append(dict(id=digest([descriptor['owner'], descriptor['tier'], descriptor['root'],
                                         descriptor['path'], descriptor['source_id'], locator]),
                **{k: descriptor[k] for k in ('owner', 'tier', 'source_id', 'root', 'path', 'title', 'sha256')},
                locator=locator, text=content, metadata=dict(descriptor.get('metadata', {}),
                    original_scope=descriptor['owner'], parser=PARSER_VERSION,
                    extraction='pdftotext layout' if prefix else 'UTF-8 exact text',
                    bytes=descriptor['bytes'], mtime_ns=descriptor['mtime_ns'])))
    return chunks


def discover(hub, manifest=None):
    """Yield pinned chunks; parse failures are explicit errors, never silent skips."""
    manifest = manifest or freeze_manifest(hub)
    yield from manifest['objects']
    for descriptor in manifest['files']:
        yield from parse_file(hub, manifest, descriptor)
