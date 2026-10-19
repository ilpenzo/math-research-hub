"""Evidence packets, section-sensitive input state and deterministic suggestion slots.

All language is either quoted from supplied data or an explicit comparison question.
No supplied hypothesis is inferred from a title or a similarity score.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import time

import catalog_store as cs
from catalog_validation import evidence_path
from . import graph, retrieve
from .knowledge import KINDS

AXES = {
    'base_category': 'Are the base field, characteristic and object category compatible?',
    'geometry_object': 'Are the actual objects identified, with the required maps?',
    'equivariance': 'Are the same equivariance and stabilizer conditions supplied?',
    'quantifier_family': 'Does the same witness or family satisfy the required quantifiers?',
    'implication_direction': 'Is the recorded implication in the direction needed?',
    'conventions': 'Do normalization, duality, signs and indexing conventions agree?',
    'local_global': 'Does the input supply the required global statement rather than a local control?',
}


def _source(entry, field):
    provenance = entry.get('provenance', entry.get('evidence', []))
    return dict(id=entry['id'], project=entry.get('project'), field=field,
                provenance=[{k: v for k, v in item.items() if k in {'root','path','locator','sha256','origin'}}
                            for item in provenance[:2]], provenance_count=len(provenance),
                provenance_excerpt=len(provenance) > 2,
                drill_down=f"hub.py show {entry['id']}")


def _quote(value, limit):
    if isinstance(value, str):
        text = value
    elif isinstance(value, dict) and isinstance(value.get('text'), str):
        text = value['text']
    else:
        text = '\n'.join(graph.texts(value))
    return dict(quote=text[:limit], excerpt=len(text) > limit,
                omitted_characters=max(0, len(text) - limit))


def view(entry, compact=False):
    result = {key: entry[key] for key in ('id', 'project', 'kind', 'title', 'status',
               'evidence_level', 'revision', 'assessment', 'disposition') if key in entry}
    if compact:
        for key in ('assessment', 'evidence_level'):
            result.pop(key, None)
    if entry.get('kind') in KINDS:
        selected = {'gap': ('question','missing'), 'move': ('needs','provides','changes'),
                    'episode': ('outcome','failure','reopening_input'),
                    'connection': ('mapping','missing_bridge','decisive_test'),
                    'reminder': ('original','interpretation','learned')}[entry['kind']]
        result['payload'] = {field: _quote(entry['payload'][field], 230 if compact else 600)
                             for field in selected if field in entry.get('payload',{})}
    else:
        for field, limit in [('statement', 300 if compact else 450), ('hypotheses', 180 if compact else 250), ('limitations', 250 if compact else 350),
                             ('reopening_condition', 200)]:
            if field in entry:
                result[field] = _quote(entry[field], limit)
        # Keep complete exclusion sentences even when the leading field excerpt
        # stops before them. These are literal quotes, not inferred conclusions.
        limitations = entry.get('limitations', '')
        if isinstance(limitations, str):
            sentences = re.split(r'(?<=[.!?])\s+', limitations)
            cautions = [s for s in sentences if re.search(r'\b(no|not|neither|unknown|unresolved|only|outside|distinct)\b',s,re.I)]
            result['scope_cautions'] = cautions[:2]
            result['scope_cautions_omitted'] = max(0,len(cautions)-2)
    result['drill_down'] = f"hub.py show {entry['id']}"
    if not compact:
        result['notice'] = 'Exact excerpts only; read full fields and evidence before applying.'
    return result


def _section(hub, catalog, provenance, file_cache):
    """Hash a named region; an unresolved locator remains explicit, never guessed.

    Supported locators: JSON record IDs/fields, Markdown headings/anchors, exact
    quoted text, and explicit line ranges. Unrelated material outside a named
    region does not invalidate it. We do not scan archives or infer new locators.
    """
    try:
        path = evidence_path(hub, catalog.cfg, provenance)
    except (ValueError, KeyError, OSError) as exc:
        return dict(state='unresolved-path', reason=str(exc))
    locator = provenance.get('locator', '')
    if not isinstance(locator, str) or not locator.strip():
        return dict(state='unresolved-locator', reason='No exact region was supplied')
    if path not in file_cache:
        try:
            if path.stat().st_size > 8 * 1024 * 1024:
                file_cache[path] = (None, 'Cited file exceeds the bounded region-reader limit')
            else:
                file_cache[path] = (path.read_text(errors='replace'), None)
        except OSError as exc:
            file_cache[path] = (None, str(exc))
    text, error = file_cache[path]
    if error:
        return dict(state='unresolved-file', reason=error)
    # Canonical JSON IDs and explicit fields are looked up in the coherent
    # snapshot rather than reading a possibly newer canonical file separately.
    objects = graph.entries(catalog)
    ids = re.findall(r'(?<![\w-])(?:[A-Z][A-Z0-9]*-)?[RKLCFO]\d+(?!\w)', locator)
    known = [key for key in ids if key in objects]
    if path.suffix == '.json' and known:
        selected = {}
        for key in known:
            fields = [name for name in objects[key] if re.search(r'\b' + re.escape(name) + r'\b', locator)]
            selected[key] = {name: objects[key][name] for name in fields} if fields else graph.content(objects[key])
        return dict(state='resolved', mode='canonical-fields', sha256=graph.digest(selected))
    lines = text.splitlines(keepends=True)
    match = re.fullmatch(r'(?:lines?\s*|L)(\d+)(?:\s*[-–:]\s*(?:L)?(\d+))?', locator.strip(), re.I)
    if match:
        start, end = int(match[1]), int(match[2] or match[1])
        if 1 <= start <= end <= len(lines):
            return dict(state='resolved', mode='lines', sha256=graph.digest(''.join(lines[start-1:end])))
        return dict(state='unresolved-locator', reason='Line range is outside current file')
    # An exact quoted passage can be cited without accepting a broad free-form locator.
    if locator.startswith('quote:'):
        quote = locator[6:].strip()
        if quote and text.count(quote) == 1:
            return dict(state='resolved', mode='quote', sha256=graph.digest(quote))
        return dict(state='unresolved-locator', reason='Quoted passage missing or ambiguous')
    heading = locator.strip().removeprefix('heading:').strip().lstrip('#').strip()
    heading = heading.split('#')[-1] if '#' in heading else heading
    slug = lambda value: re.sub(r'[^\w\s-]', '', value.casefold()).strip().replace(' ', '-')
    matches = []
    for i, line in enumerate(lines):
        match = re.match(r'^(#{1,6})\s+(.+?)\s*#*\s*$', line.rstrip('\n'))
        if match and (match[2] == heading or slug(match[2]) == heading.casefold()):
            matches.append((i, len(match[1])))
    if len(matches) == 1:
        start, level = matches[0]
        end = len(lines)
        for i in range(start + 1, len(lines)):
            match = re.match(r'^(#{1,6})\s', lines[i])
            if match and len(match[1]) <= level:
                end = i
                break
        return dict(state='resolved', mode='heading', sha256=graph.digest(''.join(lines[start:end]).rstrip()))
    return dict(state='unresolved-locator', reason='Locator is not an unambiguous supported region; no freshness claim')


def input_state(hub, catalog, seeds, file_cache=None, exclude_kinds=()):
    file_cache = {} if file_cache is None else file_cache
    objects = graph.entries(catalog)
    current = graph.context(catalog, seeds, exclude_kinds=exclude_kinds)
    manifest = {key: graph.digest(graph.content(objects[key])) for key in current['ids']}
    sections, unresolved = {}, list(current['unresolved'])
    for key in current['ids']:
        entry = objects[key]
        citations = list(entry.get('provenance', entry.get('evidence', [])))
        def nested_citations(value):
            if isinstance(value, dict):
                for name, child in value.items():
                    if name == 'provenance' and isinstance(child, list):
                        citations.extend(child)
                    elif name != 'provenance':
                        nested_citations(child)
            elif isinstance(value, list):
                for child in value:
                    nested_citations(child)
        nested_citations(entry.get('payload', {}))
        anchor = entry.get('payload', {}).get('anchor')
        if isinstance(anchor, dict) and all(name in anchor for name in ('root', 'path', 'locator')):
            citations.append(anchor)
        for citation in citations:
            if not isinstance(citation, dict):
                continue
            identity = {name: citation.get(name) for name in ('root', 'path', 'locator')}
            section_key = graph.digest(identity)
            if section_key not in sections:
                region = _section(hub, catalog, citation, file_cache)
                sections[section_key] = dict(identity, **region)
                if region['state'] != 'resolved':
                    unresolved.append(dict(cited_by=key, **identity, **region))
    fingerprint = graph.digest(dict(objects=manifest, sections=sections,
                                   correction_membership=[entry['id'] for entry in current['corrections']]))
    return dict(fingerprint=fingerprint, objects=manifest, sections=sections,
                correction_membership=[entry['id'] for entry in current['corrections']],
                unresolved=unresolved)


def compare(hub, ids, project=None):
    if len(ids) < 2 or len(ids) > 10:
        raise ValueError('Compare requires between two and ten explicit IDs')
    catalog = cs.load_catalog(Path(hub))
    objects, _, owners = graph.scope(catalog, project, ids)
    unknown = [key for key in ids if key not in objects]
    if unknown:
        raise ValueError('Unknown comparison IDs: ' + ', '.join(unknown))
    rows, comparison = [], []
    for key in ids:
        entry = objects[key]
        payload = entry.get('payload', {})
        supplied = payload.get('applicability', entry.get('applicability', {}))
        rows.append(dict(id=key, project=catalog.project_of(entry), verbatim_fields={field:entry[field] for field in
                         ('id','project','kind','title','status','statement','hypotheses','limitations','reopening_condition','payload') if field in entry},
                         explicit_map_dictionary=payload.get('mapping', 'unknown'),
                         object_dictionary=payload.get('objects', 'unknown'),
                         supplied_data={field: payload[field] for field in ('needs', 'provides',
                             'requirements', 'missing_bridge', 'preserves', 'changes') if field in payload},
                         provenance=_source(entry, 'payload' if entry.get('kind') in KINDS else 'statement/hypotheses/limitations')))
    for axis, question in AXES.items():
        values = []
        for key in ids:
            entry = objects[key]
            supplied = entry.get('payload', {}).get('applicability', entry.get('applicability', {}))
            value = supplied.get(axis) if isinstance(supplied, dict) else None
            values.append(dict(id=key, value=value if value is not None else 'unknown',
                               provenance=_source(entry, f'applicability.{axis}') if value is not None else None))
        known = [item['value'] for item in values if item['value'] != 'unknown']
        comparison.append(dict(axis=axis, values=values, question=question,
                               recorded_difference=(len({graph.digest(value) for value in known}) > 1) if len(known)==len(values) else None,
                               assessment=('Declared values only; difference does not itself establish incompatibility' if len(known)==len(values)
                                           else 'No complete structured applicability values supplied; compare the verbatim hypotheses.')))
    current = graph.context(catalog, ids)
    state = input_state(Path(hub), catalog, ids)
    return dict(project=project, inputs=rows, axes=comparison,
                current_context=dict(corrections=[view(e,compact=True) for e in current['corrections']],
                                     prerequisite_ids=[e['id'] for e in current['prerequisites']],unresolved_ids=current['unresolved']),
                input_state=dict(fingerprint=state['fingerprint'],input_ids=sorted(state['objects']),
                                 unresolved_locator_count=len(state['unresolved']),unresolved_locators=state['unresolved'][:2]),
                scope=dict(owners=sorted(owners), explicit_cross_project_ids=[key for key in ids
                    if project and catalog.project_of(objects[key]) != project]),
                notice='Unknown axes stay unknown. Read original hypotheses, maps, witnesses and failure scopes; this view does not certify composition.')


def _candidate_provisions(entry):
    payload = entry.get('payload', {})
    if entry.get('kind') == 'move':
        return [('payload.provides', payload.get('provides'))]
    if entry.get('kind') == 'episode':
        return [('payload.after', payload.get('after')), ('payload.reopening_input', payload.get('reopening_input'))]
    if entry.get('kind') not in KINDS:
        return [('statement', entry.get('statement'))]
    return []


def alignment_slots(need, candidates, exclude=(), limit=5):
    need_words, slots = graph.words(need), []
    for entry in candidates:
        if entry['id'] in exclude:
            continue
        for field, provision in _candidate_provisions(entry):
            if not provision:
                continue
            overlap = sorted(need_words & graph.words(provision))
            if not overlap:
                continue
            slots.append(dict(kind='generated-slot', builder='need-provision-alignment',
                              need=_quote(need, 250), candidate_id=entry['id'], candidate_provision=_quote(provision, 350),
                              provenance=_source(entry, field), matched_terms=overlap,
                              rank_score=len(overlap) / max(len(need_words), 1),
                              questions=[AXES['geometry_object'], AXES['quantifier_family']],
                              assessment='suggestion only; no requirement marked supplied'))
    return sorted(slots, key=lambda item: (-item['rank_score'], item['candidate_id']))[:limit]


def brief(hub, project, gap=None, task=None, limit=8):
    started = time.monotonic()
    if bool(gap) == bool(task):
        raise ValueError('Specify exactly one Gap ID or ephemeral task')
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('Brief limit must be between 1 and 20')
    hub = Path(hub)
    catalog = cs.load_catalog(hub)
    objects, allowed, _ = graph.scope(catalog, project)
    aids = [entry for key, entry in objects.items() if key in allowed and entry.get('kind') in KINDS]
    warnings = []
    if gap:
        selected = objects.get(gap)
        if selected is None or selected.get('kind') != 'gap' or selected.get('project') != project:
            raise ValueError('Gap must be a gap aid owned by the selected project')
        payload = selected['payload']
        query = ' '.join(graph.texts({name: payload.get(name) for name in ('question', 'target', 'missing')}))
        need = payload.get('missing') or payload.get('question') or selected['title']
        identity = dict(id=gap, revision=selected['revision'], anchor=payload.get('anchor'),
                        ephemeral=False, question=_quote(payload.get('question', selected['title']), 600))
        if not payload.get('anchor'):
            warnings.append('Gap has no supplied Q/NOTE anchor; retain an exact anchor before substantive reuse.')
        explicit = [ref['id'] for ref in selected.get('references', [])]
        anchor = payload.get('anchor')
        if isinstance(anchor,dict):
            anchor_ids = re.findall(r'(?<![\w-])(?:[A-Z][A-Z0-9]*-)?[RKLCFO]\d+(?!\w)',str(anchor.get('locator','')))
            explicit = list(dict.fromkeys([key for key in anchor_ids if key in objects] + explicit))
    else:
        if not isinstance(task, str) or not task.strip():
            raise ValueError('Task must be a nonempty string')
        query, need, explicit = task, task, []
        identity = dict(id=None, revision=None, anchor=None, ephemeral=True, question=task,
                        notice='No stored Gap was selected or revised by text similarity')
    retrieval_start = time.monotonic()
    recalled = retrieve.search(hub, query or selected['title'], project=project, limit=limit, semantic=True)
    retrieval_ms = round((time.monotonic() - retrieval_start) * 1000, 3)
    ranked_ids = list(dict.fromkeys(explicit + [hit['source_id'] for hit in recalled['hits']
                                               if hit['source_id'] in objects]))
    # One graph hop makes explicitly related prerequisites available to the builder.
    edge_rows = graph._edges(objects, allowed)
    graph_ids = []
    for edge in edge_rows:
        if edge['source'] in ranked_ids and edge['target'] not in ranked_ids:
            graph_ids.append(edge['target'])
    relevant_ids = list(dict.fromkeys(ranked_ids + graph_ids))[:max(limit * 2, 12)]
    candidates = [objects[key] for key in relevant_ids if key in objects]
    query_words = graph.words(query)
    relevant_aids = [aid for aid in aids if aid['id'] in ranked_ids or
                     any(ref['id'] in explicit + ([gap] if gap else []) for ref in aid.get('references', [])) or
                     len(query_words & graph.words(aid['payload'])) >= 2]
    # Aid-library matches are live, even immediately after publication before refresh.
    candidates += [aid for aid in relevant_aids if aid['id'] not in relevant_ids]
    attempts = [aid for aid in aids if aid['kind'] == 'episode' and gap and
                (aid['payload'].get('gap_id', aid['payload'].get('gap')) == gap or
                 any(ref['id'] == gap for ref in aid.get('references', [])))]
    tried = {ref['id'] for aid in attempts for ref in aid.get('references', [])
             if objects.get(ref['id'], {}).get('kind') == 'move'}
    tried.update(aid['payload'].get('move_id') for aid in attempts)
    document_seeds = {key for hit in recalled['hits'] if hit['source_id'] not in objects
                      for key in graph.document_seeds(hit, objects)}
    state_seeds = ([entry['id'] for entry in candidates] + ([gap] if gap else []) +
                   [a['id'] for a in attempts] + sorted(document_seeds))
    state = input_state(hub, catalog, state_seeds)
    # An Episode records an inspection; its creation/revision must not itself
    # change the inspected mathematical inputs. Bind this state to the explicit
    # Gap and linked Connection mappings, not to a changing ranked shortlist.
    linked_connections = [aid['id'] for aid in aids if aid['kind'] == 'connection' and gap and
                          any(ref['id'] == gap for ref in aid.get('references', []))]
    inspection_seeds = ([gap] + explicit + linked_connections) if gap else state_seeds
    inspection_state = input_state(hub, catalog, inspection_seeds, exclude_kinds={'episode'})
    unused = []
    for episode in [aid for aid in relevant_aids if aid['kind'] == 'episode']:
        move_ids = [ref['id'] for ref in episode.get('references', [])
                    if objects.get(ref['id'], {}).get('kind') == 'move']
        if episode['payload'].get('move_id') in objects:
            move_ids.append(episode['payload']['move_id'])
        for move_id in dict.fromkeys(move_ids):
            if move_id not in tried:
                unused.append(dict(kind='generated-slot', builder='move-from-similar-episode',
                    move_id=move_id, episode_id=episode['id'], scope=f'{project} recorded Gap-linked episodes',
                    assessment='No attempt found for this Gap in the searched ledger; this is not proof it was never tried',
                    question='Does the original move preserve the actual objects and maps required here?',
                    provenance=_source(episode, 'payload.move')))
    for episode in attempts:
        inspected = episode['payload'].get('inspected_input_fingerprint')
        if isinstance(inspected, dict):
            inspected = inspected.get('text')
        if inspected and inspected != inspection_state['fingerprint'] and episode['payload'].get('reopening_input'):
            unused.append(dict(kind='generated-slot', builder='changed-input-attempt', episode_id=episode['id'],
                reopening_input=episode['payload']['reopening_input'],
                before_fingerprint=inspected, after_fingerprint=inspection_state['fingerprint'],
                inspected_scope='Explicit Gap inputs and linked Connection mappings, excluding Episodes',
                assessment='External input fingerprint differs; inspect the actual change before reopening. A fingerprint alone does not prove the old failure is addressed.'))
    stored = [aid for aid in relevant_aids if aid['kind'] == 'connection']
    stored.sort(key=lambda aid: (aid['id'] not in explicit, aid['id'] not in linked_connections, aid['id']))
    current = graph.context(catalog, state_seeds)
    ingredients = []
    for entry in candidates:
        if entry.get('kind') not in {'gap', 'connection', 'episode', 'reminder'}:
            ingredients.append(dict(view(entry, compact=True), contribution='explicit-reference' if entry['id'] in explicit else
                                    ('graph-neighbor' if entry['id'] in graph_ids else 'retrieval')))
    doc_hits = [dict(source_id=hit['source_id'], title=hit['title'], root=hit['root'], path=hit['path'],
                     locator=hit['locator'], excerpt=_quote(hit['text'], 250), match_kind=hit['match_kind'],
                     cited_ids=graph.document_seeds(hit, objects))
                for hit in recalled['hits'] if hit['source_id'] not in objects][:1]
    shown, character_budget = [], 6000
    for ingredient in ingredients[:limit]:
        size = len(json.dumps(ingredient, ensure_ascii=False))
        if shown and character_budget - size < 0:
            break
        shown.append(ingredient)
        character_budget -= size
    coverage = compact_coverage(recalled['coverage'])
    return dict(project=project, gap=identity, fingerprint=inspection_state['fingerprint'],
                inspection_fingerprint=inspection_state['fingerprint'], packet_fingerprint=state['fingerprint'],
                inspection_inputs=sorted(inspection_state['objects']),
                inspection_scope='Explicit Gap inputs and linked Connection mappings; Episodes and derived pins excluded. Save this fingerprint on the checking Episode.',
                ingredients=shown, ingredient_drill_down=[entry['id'] for entry in ingredients[len(shown):]],
                document_ingredients=doc_hits,
                reminders=[view(aid, compact=True) for aid in relevant_aids if aid['kind'] == 'reminder'][:2],
                attempts=[view(aid, compact=True) for aid in attempts][:2],
                candidate_slots=alignment_slots(need, candidates, exclude=[gap], limit=3) + unused[:3],
                stored_connections=[dict(view(aid, compact=True), item_kind='stored-connection') for aid in stored[:2]],
                stored_connection_drill_down=[aid['id'] for aid in stored[2:]],
                parent_developed_proposals='None generated by this command; the parent develops and checks selected slots',
                corrections=[view(entry, compact=True) for entry in current['corrections']],
                unresolved_locators=state['unresolved'][:2],
                unresolved_locator_count=len(state['unresolved']),
                comparison_questions=list(AXES.values()),
                coverage=coverage, semantic=recalled['semantic'],
                warnings=warnings + recalled['warnings'],
                freshness=dict(input_ids=sorted(state['objects']), object_count=len(state['objects']),
                               correction_membership=state['correction_membership']),
                timing=dict(retrieval_ms=retrieval_ms, assembly_ms=round((time.monotonic()-started)*1000-retrieval_ms, 3)),
                notice='Exact excerpts are marked with omitted counts; show the named IDs for full hypotheses, limits and evidence before applying. Rankings/slots are leads; no status or goal authorization changes.')


def compact_coverage(coverage):
    """Large frozen manifests remain drill-down material, not default packets."""
    result = {}
    for key, value in coverage.items():
        if isinstance(value, list):
            result[key + '_count'] = len(value)
        elif isinstance(value, dict):
            result[key] = {name: item for name, item in value.items()
                           if isinstance(item, (str, int, float, bool, type(None)))}
        else:
            result[key] = value
    result['details'] = 'hub.py recall-index status --json; full retrieval coverage with recall --json'
    return result
