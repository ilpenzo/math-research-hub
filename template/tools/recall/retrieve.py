"""Scoped hybrid recall over a derived index, with live canonical corrections."""
from __future__ import annotations

from collections import defaultdict
import json
import math
from pathlib import Path
import re
import sqlite3
import struct

import catalog_store as cs
from . import corpus, index, providers

FIELDS = {'title': 'title', 'text': 'text', 'id': 'identity', 'source_id': 'identity', 'aliases': 'aliases'}
STOP = {'a', 'an', 'the', 'of', 'to', 'for', 'and', 'or', 'in', 'on', 'with', 'from', 'is', 'are', 'how', 'can', 'does', 'what'}


def _query(query):
    clauses, terms, filters = [], [], {}
    for match in re.finditer(r'(?:(\w+):)?(?:"([^"]+)"|(\S+))', query):
        field, phrase, word = match.groups()
        raw = phrase if phrase is not None else word
        if field in {'owner', 'project', 'tier'}:
            filters['owner' if field == 'project' else field] = raw
            continue
        normalized = index.alias_text(raw)
        words = [w for w in normalized.split() if w not in STOP]
        terms.extend(words)
        if not words:
            continue
        column = FIELDS.get(field)
        # Quote every token so punctuation and FTS operators cannot become syntax.
        sub = ' OR '.join('"' + w.replace('"', '""') + '"' for w in dict.fromkeys(words))
        clauses.append((column + ' : ' if column else '') + '(' + sub + ')')
    return ' OR '.join(clauses), list(dict.fromkeys(terms)), filters


def _field_matches(chunk, query):
    """Keep explicit field constraints valid after live canonical replacement."""
    for match in re.finditer(r'(title|text|id|source_id|aliases):(?:"([^"]+)"|(\S+))', query):
        field, phrase, word = match.groups()
        if field in {'id', 'source_id'}:
            value = chunk['source_id']
        elif field == 'aliases':
            value = ' '.join(chunk.get('metadata', {}).get('original', {}).get('library', {}).get('aliases', []))
        else:
            value = chunk[field]
        normalized = index.normalize(value)
        if phrase is not None:
            # Derived math tokens are appended to the ranking form; a quoted
            # phrase need not sit next to those tokens in a longer field.
            if (index.normalize(phrase, expanded=False) not in index.normalize(value, expanded=False)
                    or not set(index.normalize(phrase).split()) <= set(normalized.split())):
                return False
        elif not set(index.alias_text(word).split()).intersection(index.alias_text(value).split()):
            return False
    return True


def _scope_sql(project, broad, allowed, include_knowledge, filters):
    clauses, params = [], []
    if project and not broad:
        keys = sorted(allowed)
        clauses.append('(c.owner = ?' + (' OR c.source_id IN (' + ','.join('?' for _ in keys) + ')' if keys else '') + ')')
        params.append(project)
        params.extend(keys)
    if not include_knowledge:
        clauses.append("c.tier != 'knowledge'")
    for field, value in filters.items():
        clauses.append('c.' + field + ' = ?')
        params.append(value)
    return (' AND '.join(clauses) or '1'), params


def _chunk(row):
    out = dict(row)
    out.pop('vector', None)
    out.pop('rank', None)
    out['metadata'] = json.loads(out['metadata'])
    return out


def _cosine(a, b):
    denominator = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    return sum(x*y for x, y in zip(a, b)) / denominator if denominator else 0.0


def _vector_scores(connection, scope, params, vector):
    cursor = connection.execute('SELECT c.id,c.vector FROM chunks c WHERE c.vector IS NOT NULL AND ' + scope, params)
    try:
        import numpy as np
    except ImportError:
        np = None
    scores = []
    if np is None:
        for row in cursor:
            values = struct.unpack('<' + 'f' * (len(row['vector']) // 4), row['vector'])
            if len(values) != len(vector):
                raise ValueError('Indexed vector dimensions do not match the model pin')
            scores.append((_cosine(vector, values), row['id']))
        return scores
    needle = np.asarray(vector, dtype=np.float32)
    needle_norm = float(np.linalg.norm(needle))
    while True:
        rows = cursor.fetchmany(512)
        if not rows:
            break
        if any(len(row['vector']) != len(vector) * 4 for row in rows):
            raise ValueError('Indexed vector dimensions do not match the model pin')
        matrix = np.frombuffer(b''.join(row['vector'] for row in rows), dtype='<f4').reshape(len(rows), len(vector))
        norms = np.linalg.norm(matrix, axis=1) * needle_norm
        values = np.divide(matrix @ needle, norms, out=np.zeros(len(rows), dtype=np.float32), where=norms != 0)
        scores.extend((float(value), row['id']) for row, value in zip(rows, values))
    return scores


def _current_context(catalog, seeds):
    """Current prerequisites and all incoming corrections, including foreign ones."""
    by_id = catalog.by_id()
    aids = {entry['id']: entry for envelope in catalog.knowledge.values() for entry in envelope['aids']}
    entries = {**by_id, **aids}
    incoming = defaultdict(list)
    for entry in catalog.entries():
        for key in entry.get('supersedes', []):
            incoming[key].append(entry['id'])
    queue, seen, corrections, dependencies, aid_inputs, missing = list(seeds), set(), {}, {}, {}, set()
    while queue:
        key = queue.pop()
        if key in seen:
            continue
        seen.add(key)
        entry = entries.get(key)
        if not entry:
            missing.add(key)
            continue
        for correction in incoming[key]:
            corrections[correction] = by_id[correction]
            queue.append(correction)
        for relation in cs.OUTGOING:
            for target in entry.get(relation, []):
                if target in by_id:
                    dependencies[target] = by_id[target]
                queue.append(target)
        if key in aids:
            aid_inputs[key] = entry
            for ref in entry.get('references', []):
                target = ref['id']
                if target in by_id:
                    dependencies[target] = by_id[target]
                queue.append(target)
    return dict(corrections=list(corrections.values()), prerequisites=list(dependencies.values()),
                aid_inputs=list(aid_inputs.values()), unresolved=sorted(missing))


def _live_candidates(catalog, query_terms, project, broad, allowed, include_knowledge, filters, indexed=None):
    entries = [(entry, False) for entry in catalog.entries()]
    if include_knowledge:
        entries += [(entry, True) for envelope in getattr(catalog, 'knowledge', {}).values()
                    for entry in envelope.get('aids', [])]
    output = []
    for entry, knowledge in entries:
        owner = entry.get('project', cs.default_project(catalog.cfg))
        if project and not broad and owner != project and entry['id'] not in allowed:
            continue
        if filters.get('owner') and owner != filters['owner']:
            continue
        tier = 'knowledge' if knowledge else 'canonical'
        if filters.get('tier') and tier != filters['tier']:
            continue
        chunk = corpus.canonical_chunk(catalog, entry, knowledge)
        if indexed is not None and indexed.get(entry['id']) == chunk['sha256']:
            continue
        words = set(index.alias_text(chunk['text']).split())
        overlap = len(words.intersection(query_terms))
        if overlap or not query_terms:
            output.append((overlap / max(len(query_terms), 1), chunk))
    return [chunk for _, chunk in sorted(output, key=lambda x: (-x[0], x[1]['source_id']))]


def search(hub, query, project=None, limit=8, semantic=True, broad=False, include_knowledge=True):
    if not isinstance(query, str) or not query.strip():
        raise ValueError('Recall query must be a nonempty string')
    if not isinstance(limit, int) or limit < 1 or limit > 100:
        raise ValueError('Recall limit must be between 1 and 100')
    hub = Path(hub).resolve()
    catalog = cs.load_catalog(hub)
    allowed = set()
    unresolved = []
    if project:
        allowed, _, unresolved = cs.closure(catalog, project)
    fts, terms, filters = _query(query)
    state = index.status(hub)
    generation = index.current_generation(hub)
    warnings = list(state.get('warnings', []))
    stale = state.get('catalog_hashes') != catalog.file_hashes
    scope, params = _scope_sql(project, broad, allowed, include_knowledge, filters)
    lexical, semantic_ids, candidates = [], [], {}
    indexed = None
    semantic_state = dict(state=state.get('semantic', {}).get('state', 'unavailable'), used=False)
    if generation:
        connection = sqlite3.connect((generation / 'index.sqlite').as_uri() + '?mode=ro&immutable=1', uri=True)
        connection.row_factory = sqlite3.Row
        try:
            # A read-only collaborator may not create SQLite temporary files.
            # Generations never mutate after publication; ranking sorts only IDs.
            connection.execute('PRAGMA temp_store=MEMORY')
            if stale:
                indexed = dict(connection.execute("SELECT source_id,sha256 FROM chunks WHERE tier IN ('canonical','knowledge')"))
                entries = catalog.entries() + [entry for env in catalog.knowledge.values() for entry in env['aids']]
                current_hashes = {entry['id']: corpus.digest(entry) for entry in entries}
                stale = indexed != current_hashes
                if stale:
                    warnings.append('Research entries changed since indexing; returned canonical objects and corrections are current. Refresh for complete new-text ranking.')
            if fts:
                ranked_rows = connection.execute('''SELECT c.id, bm25(search, 0, 6, 1, 0.25, 8) AS rank
                    FROM search JOIN chunks c ON c.id=search.id WHERE search MATCH ? AND ''' + scope +
                    ' ORDER BY rank LIMIT ?', [fts] + params + [max(200, limit * 20)]).fetchall()
                rows = [connection.execute('SELECT * FROM chunks WHERE id=?',(row['id'],)).fetchone() for row in ranked_rows]
            else:
                rows = connection.execute('SELECT c.* FROM chunks c WHERE ' + scope + ' ORDER BY c.source_id LIMIT ?',
                                          params + [max(200, limit * 20)]).fetchall()
            for row in rows:
                chunk = _chunk(row)
                lexical.append(chunk['id'])
                candidates[chunk['id']] = chunk
            if semantic and state.get('semantic', {}).get('state') == 'ready':
                try:
                    pin = state['semantic']['pin']
                    vector = providers.query_vector(hub, query, pin)
                    # Vector scan is confined to indexed rows, in bounded numeric
                    # batches when NumPy is available, with a stdlib fallback.
                    scores = _vector_scores(connection, scope, params, vector)
                    semantic_ids = [key for _, key in sorted(scores, reverse=True)[:max(100, limit * 10)]]
                    for key in semantic_ids:
                        if key not in candidates:
                            candidates[key] = _chunk(connection.execute('SELECT * FROM chunks WHERE id=?', (key,)).fetchone())
                    semantic_state = dict(state='ready', used=True, pin=pin, ranked_vectors=len(scores))
                except (OSError, ValueError) as exc:
                    semantic_state.update(state='fallback', reason=str(exc))
                    warnings.append('Local semantic retrieval unavailable; lexical recall remains active.')
            elif semantic:
                semantic_state['reason'] = state.get('semantic', {}).get('reason', 'No pinned semantic index; lexical recall remains active.')
        finally:
            connection.close()
    if stale or not generation:
        live = _live_candidates(catalog, terms, project, broad, allowed, include_knowledge, filters, indexed=indexed)
        live_ids = [chunk['id'] for chunk in live]
        lexical = live_ids + [key for key in lexical if key not in live_ids]
        candidates.update((chunk['id'], chunk) for chunk in live)
    scores, kinds = defaultdict(float), defaultdict(set)
    for kind, ordering in [('lexical', lexical), ('semantic', semantic_ids)]:
        for rank, key in enumerate(ordering, 1):
            scores[key] += 1 / (50 + rank)
            kinds[key].add(kind)
    by_id = catalog.by_id()
    knowledge = {e['id']: e for envelope in getattr(catalog, 'knowledge', {}).values() for e in envelope.get('aids', [])}
    ranked = []
    for key, chunk in candidates.items():
        source = chunk['source_id']
        if chunk['tier'] in {'canonical', 'knowledge'}:
            original = (knowledge if chunk['tier'] == 'knowledge' else by_id).get(source)
            if original is None:
                continue  # Deleted canonical data must not be resurrected by a stale index.
            current = corpus.canonical_chunk(catalog, original, chunk['tier'] == 'knowledge')
            if current['sha256'] != chunk['sha256']:
                current['metadata']['indexed_sha256'] = chunk['sha256']
                current['metadata']['index_stale'] = True
            chunk = current
        # Ownership may have changed since this immutable generation. Reapply
        # scope after hydration so a stale row cannot import a foreign result.
        if project and not broad and chunk['owner'] != project and source not in allowed:
            continue
        if filters.get('owner') and chunk['owner'] != filters['owner']:
            continue
        if filters.get('tier') and chunk['tier'] != filters['tier']:
            continue
        if not _field_matches(chunk, query):
            continue
        words = set(index.alias_text(chunk['title'] + ' ' + chunk['text']).split())
        matched = sorted(words.intersection(terms))
        if not matched and 'semantic' not in kinds[key] and terms:
            continue
        score = scores[key] + .02 * len(matched) / max(len(terms), 1)
        if query.strip().casefold() == source.casefold():
            score += 1
        original = chunk['metadata'].get('original', {})
        if original.get('kind') == 'correction':
            score += .004
        # Status is visible, never promoted; ranking does not imply proof strength.
        chunk.update(score=score, match_kind='hybrid' if len(kinds[key]) == 2 else next(iter(kinds[key]), 'lexical'),
                     matched_terms=matched, scope='own' if not project or chunk['owner'] == project else
                     ('foreign-prerequisite-or-correction' if source in allowed else 'broad-cross-project'))
        ranked.append(chunk)
    ranked.sort(key=lambda c: (-c['score'], c['source_id'], c['locator']))
    # First pass one chunk per source, then permit a second only if slots remain.
    hits, source_counts = [], defaultdict(int)
    for cap in (1, 2):
        for chunk in ranked:
            identity = chunk['source_id']
            if chunk in hits or source_counts[identity] >= cap:
                continue
            hits.append(chunk)
            source_counts[identity] += 1
            if len(hits) == limit:
                break
        if len(hits) == limit:
            break
    corrections = {}
    for hit in hits:
        seeds = [hit['source_id']] if hit['source_id'] in by_id else []
        if hit['tier'] == 'knowledge':
            seeds += [ref['id'] for ref in hit['metadata']['original'].get('references', [])]
        # Document citations may expose corrections even where the document is old.
        seeds += [key for key in set(re.findall(r'(?<![\w-])(?:[A-Z][A-Z0-9]*-)?[RLCFO]\d+(?!\w)', hit['text'])) if key in by_id]
        context = _current_context(catalog, seeds)
        hit['current_context'] = context
        for correction in context['corrections']:
            corrections[correction['id']] = correction
        if hit['tier'] not in {'canonical', 'knowledge'}:
            # Cheap returned-hit stat check, with no full-source hash/scan on query.
            try:
                path = corpus.resolve(hub, catalog.cfg, hit['root'], hit['path'])
                stat = path.stat()
                changed = stat.st_size != hit['metadata'].get('bytes') or stat.st_mtime_ns != hit['metadata'].get('mtime_ns')
                hit['metadata']['file_state'] = 'changed-since-refresh' if changed else 'stat-matches-index; not rehashed'
            except (OSError, ValueError):
                hit['metadata']['file_state'] = 'missing-since-refresh'
    coverage = dict(state.get('coverage', {}), project=project, broad=broad, catalog_current=not stale,
                    unresolved_prerequisites=unresolved, candidate_limit=max(200, limit * 20))
    coverage['query_scope'] = 'all indexed owners' if broad or not project else 'own owner plus explicit canonical prerequisites and current corrections'
    return dict(hits=hits, coverage=coverage, semantic=semantic_state,
                warnings=warnings, corrections=list(corrections.values()),
                generation=state.get('generation'))
