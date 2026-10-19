#!/usr/bin/env python3
"""Federated registry storage, manifest validation and coherent snapshots.

Legacy data preserves complete parsed entries. New entries require explicit
ownership; all canonical publication uses transactions.apply_batch. Read-only
snapshots share its kernel publication gate and reject interrupted batches.
"""
from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import re

sys.path.insert(0, str(Path(__file__).resolve().parent))
import transactions as tx

KINDS = ('records', 'sources')
LEGACY_DIR = 'catalog'
DEFAULT_PROJECT = 'research'
# Relations that make one entry a prerequisite of, or a correction to, another.
# These mirror hub.evidence_state/format_record: depends_on, source_refs,
# subsumed_by (outgoing) and supersedes (incoming corrections).
OUTGOING = ('depends_on', 'source_refs', 'subsumed_by')


class CatalogError(ValueError):
    """Structural catalog problem: layout, ownership, uniqueness or concurrency."""


def serialize(entries: list) -> str:
    """Canonical registry text, identical to the existing catalog files."""
    return json.dumps(entries, ensure_ascii=False, indent=2) + '\n'


def canonical(entry: dict) -> str:
    """Order-independent identity of one entry, used for split verification."""
    return json.dumps(entry, ensure_ascii=False, sort_keys=True)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def default_project(cfg: dict) -> str:
    return cfg.get('legacy_default_project', cfg.get('default_project', DEFAULT_PROJECT))


def project_of(entry: dict, cfg: dict) -> str:
    return entry.get('project', default_project(cfg))


def legacy_path(hub: Path, kind: str) -> Path:
    return hub / LEGACY_DIR / f'{kind}.json'


def federated_path(hub: Path, project: str, kind: str) -> Path:
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', project) or kind not in KINDS:
        raise CatalogError('Invalid project slug or registry kind')
    return tx.safe_path(hub, f'projects/{project}/catalog/{kind}.json')


def knowledge_path(hub: Path, project: str) -> Path:
    """Supplemental storage; deliberately not a mathematical registry kind."""
    if not isinstance(project, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', project):
        raise CatalogError('Invalid knowledge owner')
    relative = 'catalog/knowledge.json' if project == 'hub' else f'projects/{project}/catalog/knowledge.json'
    return tx.safe_path(hub, relative)


def project_paths(project):
    base = f'projects/{project}'
    return dict(guide=f'{base}/START_HERE.md', rules=f'{base}/AGENTS.md',
                topics=f'{base}/TOPICS.md', changelog=f'{base}/CHANGELOG.md',
                investigations=f'{base}/investigations',
                records=f'{base}/catalog/records.json', sources=f'{base}/catalog/sources.json')


def validate_config(hub, cfg):
    if not isinstance(cfg, dict) or not isinstance(cfg.get('projects'), dict) or not cfg['projects']:
        raise CatalogError('Manifest needs a nonempty projects map')
    version = cfg.get('schema_version', 1)
    if not isinstance(version, int) or version not in {1, 2}:
        raise CatalogError('Unsupported manifest schema version')
    if not isinstance(cfg.get('roots', {}), dict):
        raise CatalogError('Manifest roots must be a map')
    prefixes = set()
    for name, info in cfg['projects'].items():
        if name == 'hub':
            raise CatalogError('hub is reserved for infrastructure ownership')
        federated_path(hub, name, 'records')
        if not isinstance(info, dict):
            raise CatalogError(f'{name}: invalid project manifest')
        if version < 2:
            continue
        for field in ['roots', 'document_routes', 'archive_selectors', 'shared_selectors', 'exclude_selectors', 'code_roots']:
            if not isinstance(info.get(field, []), list):
                raise CatalogError(f'{name}: {field} must be a list')
        if not isinstance(info.get('title'), str) or not info['title']:
            raise CatalogError(f'{name}: title is required')
        if info.get('state') not in {'active', 'paused', 'archived'}:
            raise CatalogError(f'{name}: invalid lifecycle state')
        prefix = info.get('id_prefix')
        if not isinstance(prefix, str) or (prefix and not re.fullmatch(r'[A-Z][A-Z0-9]*-', prefix)):
            raise CatalogError(f'{name}: invalid ID prefix')
        if prefix in prefixes:
            raise CatalogError('Duplicate project ID prefix')
        prefixes.add(prefix)
        if not prefix and name != cfg.get('legacy_default_project', DEFAULT_PROJECT):
            raise CatalogError('Only the legacy project may have an empty prefix')
        if info.get('catalog_state') != 'initialized':
            raise CatalogError(f'{name}: registry must be explicitly initialized')
        if set(info.get('roots', [])) - set(cfg.get('roots', {})):
            raise CatalogError(f'{name}: unknown archive root')
        for field, rel in project_paths(name).items():
            tx.safe_path(hub, rel)
        for route in info.get('document_routes', []):
            tx.safe_path(hub, route)
        for selector in info.get('archive_selectors', []) + info.get('shared_selectors', []) + info.get('exclude_selectors', []):
            if selector.get('root') not in cfg.get('roots', {}) or selector.get('selector') not in {'file', 'directory', 'subtree'}:
                raise CatalogError(f'{name}: invalid archive selector')
            rel = selector.get('path', '')
            if not isinstance(rel, str) or Path(rel).is_absolute() or '..' in Path(rel).parts:
                raise CatalogError(f'{name}: archive selector escapes root')
        for root in info.get('code_roots', []):
            if root not in cfg.get('roots', {}):
                raise CatalogError(f'{name}: unknown code root')
    for route in cfg.get('infrastructure_routes', []):
        tx.safe_path(hub, route)



def detect_layout(hub: Path, cfg: dict, overrides=None) -> str:
    declared = cfg.get('catalog_layout')
    overrides = overrides or {}
    def exists(path):
        rel = path.relative_to(hub).as_posix()
        return overrides[rel] is not None if rel in overrides else path.exists()
    legacy = any(exists(legacy_path(hub, k)) for k in KINDS)
    federated = any(exists(federated_path(hub, p, k))
                    for p in cfg.get('projects', {}) for k in KINDS)
    if legacy and federated:
        raise CatalogError('Mixed catalog layout: legacy and federated registries both exist')
    if declared is not None:
        if declared not in ('legacy', 'federated'):
            raise CatalogError(f'Unknown catalog_layout {declared!r}')
        if declared == 'legacy' and federated or declared == 'federated' and legacy:
            raise CatalogError(f'catalog_layout is {declared!r} but the other layout has data')
        return declared
    return 'federated' if federated else 'legacy'


@dataclass
class Catalog:
    """All entries with their storage location; dict entries are left unchanged."""
    cfg: dict
    layout: str
    records: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    location: dict = field(default_factory=dict)   # id -> (kind, relative file)
    file_hashes: dict = field(default_factory=dict)  # relative file -> sha256 as read
    knowledge: dict = field(default_factory=dict)  # owner -> supplemental envelope
    knowledge_location: dict = field(default_factory=dict)  # id -> (section, file)

    def entries(self):
        return self.records + self.sources

    def by_id(self) -> dict:
        return {e['id']: e for e in self.entries()}

    def knowledge_by_id(self) -> dict:
        """Aids and operational entries only; no theorem/source entries."""
        return {e['id']: e for envelope in self.knowledge.values()
                for section in ('aids', 'sessions', 'dispositions', 'checkpoints')
                for e in envelope.get(section, [])}

    def project_of(self, entry: dict) -> str:
        return project_of(entry, self.cfg)

    def id_map(self) -> dict:
        """Lightweight global ownership map: id -> project, kind and file."""
        return {e['id']: dict(project=self.project_of(e), kind=self.location[e['id']][0],
                              file=self.location[e['id']][1]) for e in self.entries()}


def _read(hub, path, catalog, overrides):
    rel = path.relative_to(hub).as_posix()
    raw = overrides.get(rel) if rel in overrides else tx.read_bytes(path)
    if raw is None:
        raise CatalogError(f'Missing initialized registry: {rel}; an empty registry must contain []')
    data = json.loads(raw)
    if not isinstance(data, list) or any(not isinstance(e, dict) for e in data):
        raise CatalogError(f'{path}: registry must be a JSON list of objects')
    catalog.file_hashes[rel] = tx.digest(raw)
    return data


def load_catalog(hub: Path, cfg=None, layout=None, *, overrides=None, already_locked=False) -> Catalog:
    """One coherent snapshot. already_locked is for transaction validators only."""
    hub = Path(hub).resolve()
    overrides = overrides or {}
    gate = contextlib.nullcontext() if already_locked else tx.locked(hub)
    with gate:
        config_bytes = overrides.get('hub.json') if 'hub.json' in overrides else tx.read_bytes(hub / 'hub.json')
        if cfg is None:
            if config_bytes is None:
                raise CatalogError('Missing hub.json')
            cfg = json.loads(config_bytes)
        validate_config(hub, cfg)
        detected = detect_layout(hub, cfg, overrides)
        if layout is not None and layout != detected:
            raise CatalogError(f'Explicit layout {layout!r} disagrees with detected {detected!r}')
        catalog = Catalog(cfg=cfg, layout=detected)
        if config_bytes is not None:
            catalog.file_hashes['hub.json'] = tx.digest(config_bytes)
        errors = []
        for kind in KINDS:
            files = [(legacy_path(hub, kind), None)] if detected == 'legacy' else [
                (federated_path(hub, p, kind), p) for p in cfg['projects']]
            for path, owner in files:
                rel = path.relative_to(hub).as_posix()
                for entry in _read(hub, path, catalog, overrides):
                    key = entry.get('id')
                    if not isinstance(key, str) or not key:
                        errors.append(f'{rel}: entry without a string id')
                        continue
                    if key in catalog.location:
                        errors.append(f'Duplicate ID {key} in {rel} and {catalog.location[key][1]}')
                        continue
                    proj = project_of(entry, cfg)
                    if not isinstance(proj, str) or proj not in cfg['projects']:
                        errors.append(f'{key}: unknown project {proj!r}')
                    if owner is not None and proj != owner:
                        errors.append(f'{key}: belongs to {proj!r} but is stored in {rel}')
                    for relation in OUTGOING + ('supersedes',):
                        refs = entry.get(relation, [])
                        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
                            errors.append(f'{key}: invalid {relation} list')
                    catalog.location[key] = (kind, rel)
                    getattr(catalog, kind).append(entry)
        if errors:
            raise CatalogError('; '.join(errors))
        # Read supplemental state under the same publication gate. Missing files
        # are an explicit empty version and participate in compare-and-swap.
        from recall import knowledge
        for owner in [*cfg['projects'], 'hub']:
            path = knowledge_path(hub, owner)
            rel = path.relative_to(hub).as_posix()
            raw = overrides.get(rel) if rel in overrides else tx.read_bytes(path)
            catalog.file_hashes[rel] = tx.digest(raw)
            envelope = knowledge.empty_envelope() if raw is None else json.loads(raw)
            catalog.knowledge[owner] = envelope
        errors = knowledge.validate(catalog, hub, overrides)
        if errors:
            raise CatalogError('; '.join(errors))
        for owner, envelope in catalog.knowledge.items():
            rel = knowledge_path(hub, owner).relative_to(hub).as_posix()
            for section in knowledge.SECTIONS:
                for entry in envelope[section]:
                    catalog.knowledge_location[entry['id']] = (section, rel)
        return catalog


def references(entry: dict) -> list:
    refs = []
    for name in OUTGOING + ('supersedes',):
        refs += entry.get(name, [])
    return refs


def closure(catalog: Catalog, project: str) -> tuple[set, set, list]:
    """IDs a scoped check of `project` must consult.

    Starts from the project's own entries. It adds outgoing prerequisites
    transitively, and every correction that supersedes a member, so corrections
    stay visible across projects. Returns (closure, foreign IDs, unresolved
    references).
    """
    if project not in catalog.cfg['projects']:
        raise CatalogError(f'Unknown project {project!r}')
    by_id = catalog.by_id()
    incoming = {}
    for e in catalog.entries():
        for target in e.get('supersedes', []):
            incoming.setdefault(target, []).append(e['id'])
    start = [e['id'] for e in catalog.entries() if catalog.project_of(e) == project]
    seen, queue, unresolved = set(), list(start), []
    while queue:
        key = queue.pop()
        if key in seen:
            continue
        if key not in by_id:
            unresolved.append(key)
            continue
        seen.add(key)
        entry = by_id[key]
        queue += [r for name in OUTGOING for r in entry.get(name, [])]
        queue += incoming.get(key, [])
        # Outgoing correction targets are context, not evidence prerequisites.
        unresolved += [r for r in entry.get('supersedes', []) if r not in by_id]
    foreign = {k for k in seen if catalog.project_of(by_id[k]) != project}
    return seen, foreign, sorted(set(unresolved))


def cross_project_references(catalog: Catalog) -> list:
    """(from, relation, to) triples whose endpoints lie in different projects."""
    by_id, out = catalog.by_id(), []
    for e in catalog.entries():
        for name in OUTGOING + ('supersedes',):
            for target in e.get(name, []):
                if target in by_id and catalog.project_of(by_id[target]) != catalog.project_of(e):
                    out.append((e['id'], name, target))
    return out


def plan_split(catalog: Catalog, transfers: dict | None = None) -> dict:
    """Federated contents {project: {kind: [entries]}} without writing anything.

    `transfers` maps id -> new project. It is the only permitted change; the
    entry gets a `project` field (or loses it when moving to the default).
    Order within each file follows the source order.
    """
    transfers = transfers or {}
    by_id = catalog.by_id()
    unknown = sorted(set(transfers) - set(by_id))
    if unknown:
        raise CatalogError(f'Transfer of unknown IDs: {unknown}')
    plan = {p: {k: [] for k in KINDS} for p in catalog.cfg.get('projects', {})}
    for kind in KINDS:
        for entry in getattr(catalog, kind):
            moved = dict(entry)
            if entry['id'] in transfers:
                target = transfers[entry['id']]
                if target == default_project(catalog.cfg):
                    moved.pop('project', None)
                else:
                    moved['project'] = target
            proj = project_of(moved, catalog.cfg)
            if proj not in plan:
                raise CatalogError(f"{entry['id']}: unknown project {proj!r}")
            plan[proj][kind].append(moved)
    return plan


def catalog_from_plan(catalog: Catalog, plan: dict) -> Catalog:
    """In-memory federated view of a plan, for closures after transfers."""
    view = Catalog(cfg=catalog.cfg, layout='federated', knowledge=catalog.knowledge,
                   knowledge_location=catalog.knowledge_location)
    for proj, kinds in plan.items():
        for kind in KINDS:
            rel = f'projects/{proj}/catalog/{kind}.json'
            for entry in kinds[kind]:
                view.location[entry['id']] = (kind, rel)
                getattr(view, kind).append(entry)
    return view


def verify_split(catalog: Catalog, plan: dict, transfers: dict | None = None) -> list:
    """Compare complete entries, not just counts; return a list of discrepancies."""
    transfers = transfers or {}
    errors = []
    if set(plan) != set(catalog.cfg['projects']):
        errors.append('Planned project set differs from manifest')
    for key, target in transfers.items():
        if key not in catalog.by_id() or target not in catalog.cfg['projects']:
            errors.append(f'Invalid declared transfer {key}={target}')
    for kind in KINDS:
        original = {e['id']: e for e in getattr(catalog, kind)}
        planned = {}
        for proj, kinds in plan.items():
            for e in kinds[kind]:
                if e['id'] in planned:
                    errors.append(f"{kind}: {e['id']} planned twice")
                planned[e['id']] = e
                if project_of(e, catalog.cfg) != proj:
                    errors.append(f"{e['id']}: planned under {proj} but belongs to {project_of(e, catalog.cfg)}")
        if set(original) != set(planned):
            errors.append(f'{kind}: ID sets differ: missing {sorted(set(original) - set(planned))}, '
                          f'extra {sorted(set(planned) - set(original))}')
        for key in set(original) & set(planned):
            before, after = dict(original[key]), dict(planned[key])
            wanted = transfers.get(key, project_of(before, catalog.cfg))
            if project_of(after, catalog.cfg) != wanted:
                errors.append(f'{key}: expected owner {wanted}, found {project_of(after, catalog.cfg)}')
            if key in transfers:
                before.pop('project', None)
                after.pop('project', None)
            if canonical(before) != canonical(after):
                errors.append(f'{key}: content changed beyond the declared ownership transfer')
    return errors


def atomic_write_text(path: Path, text: str, expected_sha256: str | None) -> str:
    """Compatibility single-file transaction; uses the same publication gate."""
    path = Path(path).resolve()
    hub = next((p for p in path.parents if (p / 'hub.json').is_file()), None)
    if hub is None:
        raise CatalogError('Atomic writer requires a hub.json ancestor; use apply_batch')
    rel = path.relative_to(hub).as_posix()
    try:
        tx.apply_batch(hub, {rel: text}, {rel: expected_sha256}, owner='atomic-write')
    except tx.TransactionError as exc:
        raise CatalogError(str(exc)) from exc
    return sha256_text(text)


@contextlib.contextmanager
def catalog_lock(hub: Path, scope: str, owner: str):
    """Compatibility explicit freeze. Do not nest a writer inside this lock."""
    try:
        with tx.locked(hub, [scope], exclusive=True):
            yield hub / '.hub/locks' / f'{scope}.lock'
    except tx.TransactionError as exc:
        raise CatalogError(str(exc)) from exc


def dry_run(hub: Path, transfers: dict | None = None) -> int:
    """Read-only rehearsal of the split on the live catalog. Writes nothing."""
    cfg = json.loads((hub / 'hub.json').read_text())
    catalog = load_catalog(hub, cfg)
    plan = plan_split(catalog, transfers)
    errors = verify_split(catalog, plan, transfers)
    view = catalog_from_plan(catalog, plan)
    print(f'Layout: {catalog.layout}; {len(catalog.records)} records, {len(catalog.sources)} sources.')
    for name, h in sorted(catalog.file_hashes.items()):
        print(f'  read {name} sha256 {h}')
    print('Planned federated registries (closures computed after declared transfers):')
    for proj, kinds in plan.items():
        size = sum(len(serialize(kinds[k]).encode('utf-8')) for k in KINDS)
        own, foreign, unresolved = closure(view, proj)
        print(f'  {proj}: {len(kinds["records"])} records, {len(kinds["sources"])} sources, '
              f'{size} bytes; scoped closure {len(own)} IDs ({len(foreign)} foreign)'
              + (f'; UNRESOLVED {unresolved}' if unresolved else ''))
    cross = cross_project_references(view)
    print(f'Cross-project references: {len(cross)}' + (f' {cross[:10]}' if cross else ''))
    if transfers:
        print(f'Declared transfers: {transfers}')
    print('Split verification: ' + ('PASS (complete entries compared)' if not errors else 'FAIL'))
    for e in errors:
        print('  ' + e)
    return 1 if errors else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only rehearsal of the federated catalog split.')
    parser.add_argument('command', choices=['plan'])
    parser.add_argument('--transfer', action='append', default=[], metavar='ID=PROJECT',
                        help='Declared ownership transfer to rehearse (repeatable)')
    args = parser.parse_args(argv)
    transfers = {}
    for item in args.transfer:
        key, _, proj = item.partition('=')
        if not key or not proj:
            parser.error(f'Bad --transfer {item!r}; use ID=PROJECT')
        transfers[key] = proj
    return dry_run(Path(__file__).resolve().parents[1], transfers)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)
