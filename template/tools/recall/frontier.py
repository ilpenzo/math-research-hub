"""Durable, input-sensitive research watches and hypothetical unlock queries.

The index is an optional shortlist. Canonical input comparison recovers missed
publication hints. Matches do not discharge a mathematical requirement.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import catalog_store as cs
from . import graph, knowledge, packets, retrieve, checkpoint_store

MAX_WATCHES = 32
MAX_MATCHES = 3


def _now():
    return datetime.now(timezone.utc).isoformat()


def _watches(objects, project):
    rows = []
    for entry in objects.values():
        if entry.get('project') != project or entry.get('disposition') in {'closed', 'retired', 'superseded'}:
            continue
        payload, kind = entry.get('payload', {}), entry.get('kind')
        if kind == 'gap':
            need = payload.get('missing') or payload.get('question')
        elif kind == 'connection':
            need = payload.get('requirements') or payload.get('missing_bridge')
        elif kind == 'episode':
            need = payload.get('reopening_input')
        elif kind == 'reminder':
            need = payload.get('triggers')
        else:
            continue
        if need:
            rows.append(dict(id=entry['id'], revision=entry['revision'], kind=kind,
                             need=need, references=[ref['id'] for ref in entry.get('references', [])]))
    return sorted(rows, key=lambda row: row['id'])


def _delta(before, after):
    before_objects, after_objects = before.get('objects', {}), after.get('objects', {})
    before_sections, after_sections = before.get('sections', {}), after.get('sections', {})
    return dict(added=sorted(after_objects.keys() - before_objects.keys()),
                removed=sorted(before_objects.keys() - after_objects.keys()),
                changed=sorted(key for key in before_objects.keys() & after_objects.keys()
                               if before_objects[key] != after_objects[key]),
                added_corrections=sorted(set(after.get('correction_membership', [])) - set(before.get('correction_membership', []))),
                removed_corrections=sorted(set(before.get('correction_membership', [])) - set(after.get('correction_membership', []))),
                changed_sections=sorted(key for key in before_sections.keys() | after_sections.keys()
                                        if before_sections.get(key) != after_sections.get(key)),
                document_changed=before.get('document') != after.get('document'))


def _checkpoint(catalog, project, since):
    rows = [entry for entry in catalog.knowledge[project]['checkpoints']
            if entry.get('purpose') == 'frontier-input-manifest']
    if since:
        selected = next((row for row in rows if since in {row['id'], row.get('fingerprint')}), None)
        if selected is None:
            raise ValueError('Unknown frontier checkpoint for the selected project')
        return selected
    return max(rows, key=lambda row: row.get('captured_at', ''), default=None)


def _manifest(hub, catalog, project, watches, objects, allowed, file_cache):
    # Every eligible canonical/aid object enters the baseline, independent of
    # notification journals and disposable index generation.
    hashes = {key: graph.digest(graph.content(objects[key])) for key in sorted(allowed)}
    relevant = packets.input_state(hub, catalog, [watch['id'] for watch in watches], file_cache)
    result = dict(objects=hashes, sections=relevant['sections'],
                  watch_revisions={watch['id']: watch['revision'] for watch in watches},
                  correction_membership=relevant['correction_membership'])
    return dict(result, fingerprint=graph.digest(result))


def _compute(hub, project, since=None):
    hub = Path(hub)
    catalog = cs.load_catalog(hub)
    objects, allowed, _ = graph.scope(catalog, project)
    prior = _checkpoint(catalog, project, since)
    if prior is not None:
        prior = checkpoint_store.hydrate(hub, project, prior)
    dispositions = {entry['item_id']: entry for entry in catalog.knowledge[project]['dispositions']
                    if entry.get('purpose') == 'frontier-disposition' and entry.get('item_id')}
    watches = _watches(objects, project)
    prior_watches = (prior or {}).get('input_manifest', {}).get('watch_revisions', {})
    # A new watch is checked immediately against current inputs, including old results.
    watches.sort(key=lambda watch: (watch['id'] in prior_watches, watch['id']))
    processed, deferred = watches[:MAX_WATCHES], watches[MAX_WATCHES:]
    file_cache, all_items, queries, warnings, document_coverage = {}, [], 0, [], {}
    previous_items = (prior or {}).get('items', {})
    candidates = {key: objects[key] for key in sorted(allowed)}
    candidate_words = {key: graph.words({field: entry.get(field) for field in
                        ('title', 'statement', 'hypotheses', 'limitations', 'reopening_condition', 'payload')})
                       for key, entry in candidates.items()}
    for watch in processed:
        need_words = graph.words(watch['need'])
        ranked = []
        for key, entry in candidates.items():
            if key == watch['id'] or entry.get('kind') in {'gap', 'connection', 'reminder'}:
                continue
            overlap = sorted(need_words & candidate_words[key])
            if overlap:
                score = len(overlap) / max(len(need_words), 1)
                ranked.append((score, key, overlap))
        ranked.sort(key=lambda row: (-row[0], row[1]))
        matches = {key: dict(candidate_id=key, matched_terms=overlap, rank_score=score,
                            original=entry if (entry := candidates[key]) else None)
                   for score, key, overlap in ranked[:MAX_MATCHES]}
        # Explicit references are not proof of a match, but their changes must
        # not disappear just because text ranking shifts after a correction.
        for key in watch['references']:
            if key in objects and key != watch['id']:
                matches.setdefault(key, dict(candidate_id=key, matched_terms=[], rank_score=None,
                                             original=objects[key], match_reason='explicit watch reference'))
        for item in list(previous_items.values()) + list(dispositions.values()):
            if item.get('watch_id') == watch['id']:
                key = item.get('candidate_id')
                if key:
                    matches.setdefault(key, dict(candidate_id=key, matched_terms=[], rank_score=None,
                        original=objects.get(key), match_reason='previously inspected input',
                        retained_document=item.get('input_state', {}).get('document')))
        # One bounded lexical query per watch; no all-pairs model requests.
        query = ' '.join(graph.texts(watch['need']))[:1500]
        if query:
            try:
                recalled = retrieve.search(hub, query, project=project, limit=3, semantic=False)
                queries += 1
                warnings.extend(message for message in recalled['warnings'] if message not in warnings)
                document_coverage = packets.compact_coverage(recalled['coverage'])
                seen_documents = set()
                for hit in recalled['hits']:
                    if hit['tier'] in {'canonical', 'knowledge'}:
                        continue
                    identity = 'document:' + graph.digest({key: hit[key] for key in ('root', 'path', 'source_id')})[:24]
                    if identity in seen_documents:
                        continue
                    seen_documents.add(identity)
                    matches[identity] = dict(candidate_id=identity, matched_terms=hit['matched_terms'],
                        rank_score=hit['score'], document=dict({key: hit[key] for key in
                            ('root', 'path', 'source_id', 'locator', 'sha256', 'text')},
                            canonical_citations=graph.document_seeds(hit, objects)),
                        match_reason='bounded lexical document match')
            except (ValueError, OSError) as exc:
                warnings.append(f"{watch['id']}: document shortlist unavailable: {exc}; live canonical candidates checked")
        for key, match in matches.items():
            item_id = f"{project}:frontier:" + graph.digest([watch['id'], key])[:24]
            cited_document = match.get('document') or match.get('retained_document') or {}
            seeds = ([watch['id']] + ([key] if key in objects else []) +
                     cited_document.get('canonical_citations', []))
            state = packets.input_state(hub, catalog, seeds, file_cache)
            if match.get('document'):
                # This fingerprint declares indexed content, not a current-file proof.
                state['document'] = {name: value for name, value in match['document'].items() if name != 'text'}
                region = packets._section(hub, catalog, state['document'], file_cache)
                state['document']['current_region'] = region
                if region['state'] == 'resolved':
                    state['document'].pop('sha256', None)
                state['fingerprint'] = graph.digest({name: value for name, value in state.items()
                                                      if name not in {'fingerprint', 'unresolved'}})
            elif key.startswith('document:'):
                # An old document match whose selected text disappeared from the
                # current shortlist is explicitly unresolved, never silently fresh.
                retained = match.get('retained_document')
                if retained and all(name in retained for name in ('root', 'path', 'locator')):
                    state['document'] = dict(retained)
                    state['document']['current_region'] = packets._section(hub, catalog, retained, file_cache)
                else:
                    state['document'] = {'state': 'not-in-current-bounded-shortlist', 'candidate_id': key}
                state['fingerprint'] = graph.digest({name: value for name, value in state.items()
                                                      if name not in {'fingerprint', 'unresolved'}})
            disposition = dispositions.get(item_id)
            baseline = disposition.get('input_state') if disposition else previous_items.get(item_id, {}).get('input_state')
            changed = bool(baseline and baseline['fingerprint'] != state['fingerprint'])
            status = 'active'
            if disposition and disposition.get('action') in {'dismiss', 'defer'} and not changed:
                status = 'dismissed' if disposition['action'] == 'dismiss' else 'deferred'
            elif disposition and changed:
                status = 'resurfaced'
            reason = ('Relevant inspected inputs changed' if changed else
                      ('New watch checks inputs already in the current corpus' if watch['id'] not in prior_watches
                       else match.get('match_reason', 'Current need/provision vocabulary match')))
            current = graph.context(catalog, seeds)
            all_items.append(dict(id=item_id, watch_id=watch['id'], watch_revision=watch['revision'],
                candidate_id=key, requirement=watch['need'], disposition=status,
                why=reason, matched_terms=match['matched_terms'], rank_score=match['rank_score'],
                fingerprint=state['fingerprint'], input_state=state,
                delta=_delta(baseline, state) if baseline else None,
                candidate=packets.view(match['original']) if match.get('original') else
                    ({**{name: value for name, value in match.get('document', {}).items() if name != 'text'},
                      'excerpt': packets._quote(match['document']['text'], 500)} if match.get('document') else
                     {'id': key, 'state': 'missing-or-not-in-shortlist'}),
                corrections=[packets.view(entry) for entry in current['corrections']],
                prior_reason=disposition.get('reason') if disposition else None,
                assessment='Worth checking only; no requirement proved, no goal activated'))
    manifest = _manifest(hub, catalog, project, processed, objects, allowed, file_cache)
    manifest['watch_matches'] = {item['id']: item['fingerprint'] for item in all_items}
    manifest['fingerprint'] = graph.digest({name: value for name, value in manifest.items()
                                          if name != 'fingerprint'})
    checkpoint_id = f"{project}:frontier-checkpoint:" + manifest['fingerprint'][:24]
    checkpoint = dict(id=checkpoint_id, purpose='frontier-input-manifest', captured_at=_now(),
                      fingerprint=manifest['fingerprint'], input_manifest=manifest,
                      items={item['id']: {key: item[key] for key in
                             ('watch_id', 'candidate_id', 'input_state')} for item in all_items},
                      scope=dict(project=project, watched_ids=[watch['id'] for watch in processed],
                                 deferred_watch_ids=[watch['id'] for watch in deferred]))
    return dict(project=project, checkpoint=checkpoint, fingerprint=manifest['fingerprint'],
                baseline=dict(id=prior['id'], captured_at=prior.get('captured_at'),
                              fingerprint=prior.get('fingerprint')) if prior else None,
                canonical_delta=_delta((prior or {}).get('input_manifest', {}), manifest),
                all_items=all_items, warnings=warnings,
                coverage=dict(canonical_objects=len(candidates), processed_watches=len(processed),
                    deferred_watch_ids=[watch['id'] for watch in deferred], lexical_queries=queries,
                    document_index=document_coverage,
                    per_watch_top_matches=MAX_MATCHES,
                    scope='Live project inputs plus explicit prerequisites/corrections; bounded indexed documents',
                    exhaustive=False, missed_events='Recovered by live canonical manifest comparison, independent of notification/cache'),
                notice='A new or changed match is a research lead. Mathematical assessment and queue disposition remain separate.')


def scan(hub, project, since=None):
    result = _compute(hub, project, since)
    checkpoint = result['checkpoint']
    catalog = cs.load_catalog(Path(hub))
    existing = next((entry for entry in catalog.knowledge[project]['checkpoints']
                     if entry['id'] == checkpoint['id']), None)
    # Identical manifests reuse the checkpoint; repeated retrieval cannot mutate
    # mathematical data or erase a dismissal.
    if existing is None:
        try:
            saved = knowledge.publish_section(hub, project, 'checkpoints', checkpoint, 'recall-frontier')
            checkpoint = saved['entry']
        except ValueError as exc:
            result['warnings'].append('Checkpoint publication failed; computed results remain valid for the displayed snapshot: ' + str(exc))
            checkpoint = dict(checkpoint, persistence='failed')
    else:
        checkpoint = existing
    result['checkpoint'] = {key: checkpoint.get(key) for key in
                            ('id', 'revision', 'captured_at', 'fingerprint', 'scope', 'persistence')}
    all_items = result.pop('all_items')
    result['items'] = [item for item in all_items if item['disposition'] in {'active', 'resurfaced'}]
    for item in result['items']:
        state = item.pop('input_state')
        item['freshness'] = dict(input_ids=sorted(state['objects']),
                                 unresolved_locator_count=len(state['unresolved']),
                                 correction_membership=state['correction_membership'])
    result['suppressed'] = [{key: item[key] for key in ('id', 'watch_id', 'candidate_id', 'disposition',
                              'fingerprint', 'prior_reason')} for item in all_items
                            if item['disposition'] in {'dismissed', 'deferred'}]
    return result


def act(hub, project, item_id, action, reason, owner):
    if action not in {'dismiss', 'defer', 'reactivate'}:
        raise ValueError('Frontier action must be dismiss, defer or reactivate')
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('Frontier disposition requires a reason')
    result = _compute(hub, project)
    item = next((row for row in result['all_items'] if row['id'] == item_id), None)
    if item is None:
        raise ValueError('Unknown frontier item in the selected project; rescan before acting')
    catalog = cs.load_catalog(Path(hub))
    disposition_id = item_id.replace(':frontier:', ':frontier-disposition:', 1)
    previous = next((row for row in catalog.knowledge[project]['dispositions']
                     if row['id'] == disposition_id), None)
    entry = dict(id=disposition_id, purpose='frontier-disposition', item_id=item_id,
                 watch_id=item['watch_id'], candidate_id=item['candidate_id'], action=action,
                 reason=reason.strip(), inspected_at=_now(), fingerprint=item['fingerprint'],
                 input_state=item['input_state'])
    saved = knowledge.publish_section(hub, project, 'dispositions', entry, owner,
                                      expected_revision=previous['revision'] if previous else None)
    return dict(item_id=item_id, action=action, reason=reason.strip(), fingerprint=item['fingerprint'],
                disposition=saved['entry'], transaction=saved['transaction'],
                notice='Disposition applies to these inspected inputs; relevant changes may resurface the item.')


def unlocks(hub, query, project):
    if not isinstance(query, str) or not query.strip():
        raise ValueError('Hypothetical unlock query must be nonempty')
    catalog = cs.load_catalog(Path(hub))
    objects, allowed, _ = graph.scope(catalog, project)
    query_words = graph.words(query)
    candidates = []
    for watch in _watches(objects, project):
        overlap = sorted(query_words & graph.words(watch['need']))
        if overlap:
            candidates.append(dict(watch_id=watch['id'], kind=watch['kind'], requirement=watch['need'],
                matched_terms=overlap, potential_benefit='The proposed lemma may address this recorded requirement',
                remaining_questions=list(packets.AXES.values()),
                provenance=packets._source(objects[watch['id']], 'payload'),
                assessment='Conditional usefulness only; lemma and applicability unproved'))
    candidates.sort(key=lambda item: (-len(item['matched_terms']), item['watch_id']))
    recalled = retrieve.search(hub, query, project=project, limit=8, semantic=False)
    controls = []
    for key in sorted(allowed):
        entry = objects[key]
        failure = entry.get('kind') in {'correction', 'obstruction', 'failed-approach', 'failed-attempt'}
        is_episode = entry.get('kind') == 'episode' and bool(entry.get('payload', {}).get('failure'))
        if (failure or is_episode or entry.get('reopening_condition')) and query_words & graph.words(entry):
            controls.append(entry)
    # Corrections of every selected candidate/prerequisite remain visible even
    # when they do not lexically mention the hypothetical lemma.
    current = graph.context(catalog, [row['watch_id'] for row in candidates[:8]])
    controls_by_id = {entry['id']: entry for entry in controls + current['corrections']}
    return dict(project=project, hypothetical=query, candidates=candidates[:8],
                controls=[packets.view(entry) for entry in list(controls_by_id.values())[:8]],
                related_evidence=[dict(source_id=hit['source_id'], title=hit['title'], locator=hit['locator'],
                    path=hit['path'], excerpt=packets._quote(hit['text'], 250)) for hit in recalled['hits'][:4]],
                corrections=[packets.view(entry) for entry in current['corrections']],
                coverage=dict(packets.compact_coverage(recalled['coverage']),
                              controls_scope='recorded failures/corrections and explicit reopening conditions'),
                notice='A hypothetical lemma is not a fact. Controls and failure scopes may limit its usefulness; no status changes.')
