"""Typed, bounded navigation over live canonical statements and research aids.

Associative edges are never inserted into the theorem dependency relation.
Similarity is a navigation suggestion only; path existence is not a proof.
"""
from __future__ import annotations

from collections import defaultdict, deque
import hashlib
import json
from pathlib import Path
import re

import catalog_store as cs
from .knowledge import KINDS

LOGICAL = {'depends_on': 'recorded dependency', 'source_refs': 'recorded source reference',
           'subsumed_by': 'recorded subsumption', 'supersedes': 'recorded correction/supersession'}
DERIVED = {'freshness', 'input_fingerprint', 'fingerprint', 'pins', 'pin', 'evidence_hashes',
           'inspected_input_fingerprint', 'inspected_inputs', 'derived_freshness'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def content(entry):
    """Exclude derived aid freshness pins so no self-hash recursion can arise."""
    if isinstance(entry, dict):
        return {key: content(value) for key, value in entry.items() if key not in DERIVED}
    if isinstance(entry, list):
        return [content(value) for value in entry]
    return entry


def entries(catalog):
    return dict(catalog.by_id(), **{e['id']: e for envelope in catalog.knowledge.values()
                                   for e in envelope.get('aids', [])})


def texts(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in {'provenance', 'evidence', 'review', 'assessment', 'sha256',
                           'fingerprint', 'input_fingerprint', 'inspected_input_fingerprint',
                           'created_at', 'updated_at'}:
                yield from texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from texts(item)


def words(value):
    stop = {'the', 'and', 'with', 'from', 'that', 'this', 'for', 'are', 'not', 'has',
            'was', 'any', 'all', 'can', 'only', 'does', 'its', 'into', 'same', 'but'}
    return {word.casefold() for word in re.findall(r'[\w]+(?:-[\w]+)*', ' '.join(texts(value)))
            if len(word) > 2 and word.casefold() not in stop}


def document_seeds(hit, objects):
    """Recover cited anchors, not just the document's disposable chunk identity.

    Incoming corrections are reassembled live from their targets. They are not
    frozen as permanent document references when a shortlist is retained.
    """
    keys = set(re.findall(r'(?<![\w-])(?:[A-Z][A-Z0-9]*-)?[RKLCFO]\d+(?!\w)',
                          hit.get('text', '')))
    if hit.get('source_id') in objects:
        keys.add(hit['source_id'])
    current = hit.get('current_context', {})
    for entry in current.get('prerequisites', []):
        keys.add(entry['id'])
    for entry in current.get('corrections', []):
        keys.update(entry.get('supersedes', []))
    return sorted(key for key in keys if key in objects)


def context(catalog, seeds, exclude_kinds=()):
    """Correction closure includes new incoming corrections of prerequisites."""
    objects = entries(catalog)
    incoming = defaultdict(list)
    for entry in catalog.entries():
        for key in entry.get('supersedes', []):
            incoming[key].append(entry['id'])
    queue, seen, corrections, prerequisites, missing = list(seeds), set(), set(), set(), set()
    while queue:
        key = queue.pop()
        if key in seen:
            continue
        seen.add(key)
        entry = objects.get(key)
        if entry is None:
            missing.add(key)
            continue
        if entry.get('kind') in exclude_kinds:
            continue
        for correction in incoming[key]:
            corrections.add(correction)
            queue.append(correction)
        targets = ([ref['id'] for ref in entry.get('references', [])]
                   if entry.get('kind') in KINDS else
                   [target for relation in cs.OUTGOING for target in entry.get(relation, [])])
        for target in targets:
            prerequisites.add(target)
            queue.append(target)
    visible = {key for key in seen & objects.keys() if objects[key].get('kind') not in exclude_kinds}
    return dict(corrections=[objects[key] for key in sorted(corrections) if key in visible],
                prerequisites=[objects[key] for key in sorted(prerequisites) if key in visible],
                unresolved=sorted(missing), ids=sorted(visible))


def scope(catalog, project, explicit=()):
    objects = entries(catalog)
    if project is not None and project not in catalog.cfg['projects']:
        raise ValueError('Unknown project')
    if project is None:
        owners = {catalog.project_of(objects[key]) for key in explicit if key in objects}
        if not owners:
            raise ValueError('A project or known explicit record is required')
    else:
        owners = {project}
    own = {key for key, entry in objects.items() if catalog.project_of(entry) in owners}
    allowed = own | set(explicit)
    allowed.update(context(catalog, allowed)['ids'])
    return objects, allowed, owners


def _edges(objects, allowed):
    edges, seen = [], set()
    def add(start, end, relation, category, explanation, **extra):
        if end not in allowed or end not in objects or start == end:
            return
        key = (start, end, relation, category)
        if key in seen:
            return
        seen.add(key)
        edges.append(dict(source=start, target=end, relation=relation, category=category,
                          explanation=explanation, **extra))
    pattern = re.compile(r'(?<![\w-])(?:[A-Z][A-Z0-9]*-)?[RKLCFO]\d+(?!\w)')
    for key in sorted(allowed & objects.keys()):
        entry = objects[key]
        if entry.get('kind') in KINDS:
            for ref in entry.get('references', []):
                category = 'stored-proposal' if entry['kind'] == 'connection' else 'associative'
                add(key, ref['id'], ref['relation'], category,
                    f"Stored {entry['kind']} reference; applicability is not certified")
            body = entry.get('payload', {})
        else:
            for relation, explanation in LOGICAL.items():
                for target in entry.get(relation, []):
                    add(key, target, relation, 'logical', explanation)
            body = {name: entry.get(name) for name in ('statement', 'hypotheses', 'limitations',
                    'reopening_condition', 'title')}
        for target in sorted(set(pattern.findall(' '.join(texts(body))))):
            add(key, target, 'mentions', 'explicit-mention', 'Explicit ID mention; not a dependency assertion')
    return edges


def _adjacency(edges):
    adjacency = defaultdict(list)
    for edge in edges:
        adjacency[edge['source']].append((edge['target'], dict(edge, traversed='forward')))
        adjacency[edge['target']].append((edge['source'], dict(edge, traversed='reverse')))
    return adjacency


def _bfs(adjacency, start, end=None, hops=2, cap=80):
    paths, queue = {start: []}, deque([start])
    while queue and len(paths) < cap:
        key = queue.popleft()
        if len(paths[key]) >= hops:
            continue
        for target, edge in adjacency[key]:
            if target in paths:
                continue
            paths[target] = paths[key] + [edge]
            if target == end:
                return paths
            queue.append(target)
            if len(paths) >= cap:
                break
    return paths


def neighbors(hub, key, project=None, hops=2):
    if type(hops) is not int or not 0 <= hops <= 4:
        raise ValueError('Graph hops must be between 0 and 4')
    catalog = cs.load_catalog(Path(hub))
    objects, allowed, owners = scope(catalog, project, [key])
    if key not in objects:
        raise ValueError(f'Unknown graph ID {key}')
    edges = _edges(objects, allowed)
    paths = _bfs(_adjacency(edges), key, hops=hops)
    # Similarity is displayed separately and never used by the logical path finder.
    seed_words = words(objects[key].get('title', ''))
    similar = []
    if seed_words and hops:
        for target in sorted(allowed - paths.keys()):
            other_words = words(objects[target].get('title', ''))
            overlap = sorted(seed_words & other_words)
            if len(overlap) >= 2:
                similar.append(dict(id=target, category='similarity-suggestion', matched_terms=overlap,
                                    score=len(overlap) / max(len(seed_words | other_words), 1),
                                    explanation='Shared title terms only; inspect hypotheses and maps'))
    similar.sort(key=lambda item: (-item['score'], item['id']))
    return dict(id=key, project=project, hops=hops,
                nodes=[dict(id=target, project=catalog.project_of(objects[target]),
                            original=objects[target], path=route,
                            path_kind='logical' if route and all(e['category'] == 'logical' for e in route)
                            else 'associative-navigation') for target, route in paths.items()],
                similarity_suggestions=similar[:3], current_context=context(catalog, paths),
                coverage=dict(owners=sorted(owners), explicit_ids=[key], node_cap=80,
                              truncated=len(paths) >= 80, scope='owners plus explicit prerequisites/corrections'),
                notice='Reverse traversal explains references, not implication direction. No path certifies composition.')


def path(hub, start, end, project=None):
    catalog = cs.load_catalog(Path(hub))
    objects, allowed, owners = scope(catalog, project, [start, end])
    for key in (start, end):
        if key not in objects:
            raise ValueError(f'Unknown graph ID {key}')
    edges = _edges(objects, allowed)
    paths = _bfs(_adjacency(edges), start, end, hops=6, cap=500)
    logical = _bfs(_adjacency([e for e in edges if e['category'] == 'logical']),
                   start, end, hops=6, cap=500)
    route = paths.get(end)
    return dict(start=start, end=end, project=project, found=route is not None,
                path=route, logical_path=logical.get(end),
                path_kind=('logical' if route and all(e['category'] == 'logical' for e in route)
                           else 'associative-navigation') if route is not None else None,
                current_context=context(catalog, [start, end]),
                coverage=dict(owners=sorted(owners), explicit_ids=[start, end], max_hops=6, node_cap=500,
                              truncated=len(paths) >= 500),
                notice='Relation directions are retained; reverse navigation is not a logical implication.')
