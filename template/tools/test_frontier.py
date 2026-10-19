"""Typed connection/frontier hazards against disposable hubs only."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import catalog_store as cs
from recall import frontier, graph, knowledge, packets


class FrontierTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.hub = Path(temp.name)
        cfg = dict(schema_version=2, catalog_layout='federated', roots={}, projects={})
        for name, prefix in [('alpha', 'A-'), ('beta', 'B-')]:
            cfg['projects'][name] = dict(title=name, roots=[], state='active', id_prefix=prefix,
                catalog_state='initialized', document_routes=[], archive_selectors=[], shared_selectors=[], code_roots=[])
            directory = self.hub / f'projects/{name}/catalog'
            directory.mkdir(parents=True)
            for kind in cs.KINDS:
                (directory / f'{kind}.json').write_text('[]\n')
        (self.hub / 'hub.json').write_text(cs.serialize(cfg))
        self.note = self.hub / 'NOTE.md'
        self.note.write_text('# Relevant\nA globally generated bundle needs the same map.\n\n# Other\nUnrelated data.\n')
        self.evidence = dict(root='hub', path='NOTE.md', locator='# Relevant', origin='fixture')
        self.record = dict(id='A-R001', project='alpha', kind='result', title='Bundle generation transfer',
            topics=['generation'], status='reported', evidence_level='imported', novelty='unassessed',
            statement='A globally generated bundle supplies a surjective evaluation map.',
            hypotheses=['Same bundle and same evaluation map'], limitations='No claim for a different bundle.',
            evidence=[{key: value for key, value in self.evidence.items() if key != 'origin'}],
            depends_on=[], supersedes=[], subsumed_by=[], source_refs=[])
        self.records = [self.record]
        self.write_records()
        self.empty_recall = dict(hits=[], coverage={'exhaustive': False}, semantic={'used': False}, warnings=[])
        self.patcher = patch('recall.retrieve.search', return_value=self.empty_recall)
        self.mock_recall = self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def write_records(self, rows=None, project='alpha'):
        cs.federated_path(self.hub, project, 'records').write_text(cs.serialize(self.records if rows is None else rows))

    def aid(self, kind='gap', payload=None, refs=None, **updates):
        spec = dict(kind=kind, title=f'{kind} globally generated bundle',
            references=refs if refs is not None else [dict(id='A-R001', relation='input')],
            provenance=[self.evidence], payload=payload or dict(question='Does generation transfer?',
                missing='globally generated bundle with the same evaluation map', anchor=self.evidence),
            assessment=dict(state='parent-inspected', scope='Fixture interpretation only'), disposition='open')
        spec.update(updates)
        return knowledge.remember(self.hub, spec, 'alpha', 'fixture')['aid']

    def state(self, *ids):
        return packets.input_state(self.hub, cs.load_catalog(self.hub), list(ids))

    def candidate(self, output, watch, candidate='A-R001'):
        return next(item for item in output['items'] if item['watch_id'] == watch and item['candidate_id'] == candidate)

    def test_graph_associative_edges_are_never_logical_dependencies(self):
        aid = self.aid('connection', dict(requirements='global generation', mapping={'E': 'F'}),
                       refs=[dict(id='A-R001', relation='depends_on')])
        route = graph.path(self.hub, aid['id'], 'A-R001', 'alpha')
        self.assertTrue(route['found'])
        self.assertIsNone(route['logical_path'])
        self.assertEqual(route['path'][0]['category'], 'stored-proposal')
        self.assertNotIn(aid['id'], cs.load_catalog(self.hub).by_id())
        foreign = dict(self.record, id='B-R001', project='beta')
        self.write_records([foreign], 'beta')
        neighborhood = graph.neighbors(self.hub, 'A-R001', project='alpha')
        self.assertNotIn('B-R001', [row['id'] for row in neighborhood['nodes']])
        comparison = packets.compare(self.hub, ['A-R001', 'B-R001'], 'alpha')
        self.assertEqual(comparison['scope']['explicit_cross_project_ids'], ['B-R001'])

    def test_prerequisite_new_and_removed_corrections_change_state(self):
        prerequisite = dict(self.record, id='A-R002', title='Prerequisite')
        self.record['depends_on'] = ['A-R002']
        self.records.append(prerequisite)
        self.write_records()
        aid = self.aid()
        before = self.state(aid['id'])
        correction = dict(self.record, id='A-C001', kind='correction', depends_on=[],
                          title='Correction of prerequisite', supersedes=['A-R002'])
        self.records.append(correction)
        self.write_records()
        after = self.state(aid['id'])
        self.assertNotEqual(before['fingerprint'], after['fingerprint'])
        self.assertEqual(after['correction_membership'], ['A-C001'])
        packet = packets.compare(self.hub, [aid['id'], 'A-R001'], 'alpha')
        self.assertEqual(packet['current_context']['corrections'][0]['id'], 'A-C001')
        self.records.pop()
        self.write_records()
        self.assertEqual(before['fingerprint'], self.state(aid['id'])['fingerprint'])

    def test_cited_region_changes_but_unrelated_note_append_does_not(self):
        aid = self.aid()
        first = self.state(aid['id'])
        self.note.write_text(self.note.read_text() + '\n# Later\nAnother unrelated fact.\n')
        second = self.state(aid['id'])
        self.assertEqual(first['fingerprint'], second['fingerprint'])
        self.note.write_text(self.note.read_text().replace('same map.', 'different map.'))
        self.assertNotEqual(first['fingerprint'], self.state(aid['id'])['fingerprint'])
        unknown = self.aid('move', dict(needs='unknown'), provenance=[dict(self.evidence, locator='unspecified argument')])
        self.assertTrue(self.state(unknown['id'])['unresolved'])

    def test_compare_unknown_axes_and_explicit_maps(self):
        aid = self.aid('connection', dict(mapping={'source_bundle': 'target_bundle'},
            applicability={'base_category': 'complex smooth varieties'}))
        out = packets.compare(self.hub, [aid['id'], 'A-R001'], 'alpha')
        self.assertEqual(out['inputs'][0]['explicit_map_dictionary']['source_bundle'], 'target_bundle')
        base = out['axes'][0]
        self.assertEqual(base['values'][1]['value'], 'unknown')
        self.assertFalse(base['recorded_difference'])
        self.assertIsNone(base['recorded_difference'])
        for field in ('statement','hypotheses','limitations'):
            self.assertEqual(out['inputs'][1]['verbatim_fields'][field], self.record[field])

    def test_ephemeral_brief_does_not_bind_gap_and_generates_unused_move(self):
        gap = self.aid()
        move = self.aid('move', dict(needs='same map', provides='globally generated bundle and evaluation map'))
        episode = self.aid('episode', dict(before='globally generated bundle missing', move='Apply transfer',
            after='globally generated evaluation map', reopening_input='same bundle identification'),
            refs=[dict(id=move['id'], relation='move')])
        before = cs.load_catalog(self.hub).file_hashes
        ephemeral = packets.brief(self.hub, 'alpha', task='globally generated bundle with evaluation map')
        self.assertTrue(ephemeral['gap']['ephemeral'])
        self.assertIsNone(ephemeral['gap']['id'])
        self.assertEqual(before, cs.load_catalog(self.hub).file_hashes)
        out = packets.brief(self.hub, 'alpha', gap=gap['id'])
        self.assertEqual(out['gap']['revision'], 1)
        self.assertTrue(any(slot['builder'] == 'need-provision-alignment' for slot in out['candidate_slots']))
        self.assertTrue(any(slot.get('move_id') == move['id'] for slot in out['candidate_slots']))
        self.aid('episode', dict(gap_id=gap['id'], before='globally generated bundle missing',
            move_id=move['id'], after='Still open', failure='Different map'),
            refs=[dict(id=gap['id'], relation='gap'), dict(id=move['id'], relation='move')])
        out = packets.brief(self.hub, 'alpha', gap=gap['id'])
        self.assertFalse(any(slot.get('move_id') == move['id'] for slot in out['candidate_slots']))
        self.assertTrue(out['attempts'])

    def test_new_watch_checks_old_inputs_and_persists_checkpoint(self):
        first = frontier.scan(self.hub, 'alpha')
        self.assertEqual(first['coverage']['processed_watches'], 0)
        gap = self.aid()
        output = frontier.scan(self.hub, 'alpha', since=first['checkpoint']['id'])
        item = self.candidate(output, gap['id'])
        self.assertIn('New watch', item['why'])
        self.assertEqual(output['coverage']['lexical_queries'], 1)
        catalog = cs.load_catalog(self.hub)
        self.assertIn(output['checkpoint']['id'], catalog.knowledge_by_id())
        self.assertEqual(output['checkpoint']['id'], frontier.scan(self.hub, 'alpha')['checkpoint']['id'])

    def test_dismiss_survives_rebuild_and_unrelated_changes_but_new_correction_resurfaces(self):
        gap = self.aid()
        first = frontier.scan(self.hub, 'alpha')
        item = self.candidate(first, gap['id'])
        frontier.act(self.hub, 'alpha', item['id'], 'dismiss', 'The maps differ', 'fixture')
        out = frontier.scan(self.hub, 'alpha')
        self.assertIn(item['id'], [row['id'] for row in out['suppressed']])
        # A disposable index rebuild has no role in the stable identity/state.
        (self.hub / '.hub/recall').mkdir(exist_ok=True)
        (self.hub / '.hub/recall/CURRENT.json').write_text('{}')
        extra = dict(self.record, id='A-R002', title='Unrelated slope', statement='Separate slope theorem',
                     hypotheses=['Different object'], limitations='Separate problem')
        self.records.append(extra)
        self.write_records()
        out = frontier.scan(self.hub, 'alpha')
        self.assertIn(item['id'], [row['id'] for row in out['suppressed']])
        correction = dict(self.record, id='A-C001', kind='correction', title='Map correction',
                          statement='The earlier global map needs extra input', supersedes=['A-R001'])
        self.records.append(correction)
        self.write_records()
        out = frontier.scan(self.hub, 'alpha')
        updated = self.candidate(out, gap['id'])
        self.assertEqual(updated['id'], item['id'])
        self.assertEqual(updated['disposition'], 'resurfaced')
        self.assertEqual(updated['delta']['added_corrections'], ['A-C001'])
        frontier.act(self.hub, 'alpha', item['id'], 'defer', 'Need to inspect correction', 'fixture')
        self.records.pop()
        self.write_records()
        updated = self.candidate(frontier.scan(self.hub, 'alpha'), gap['id'])
        self.assertEqual(updated['delta']['removed_corrections'], ['A-C001'])
        frontier.act(self.hub, 'alpha', item['id'], 'reactivate', 'Proceed after inspection', 'fixture')
        self.assertEqual(self.candidate(frontier.scan(self.hub, 'alpha'), gap['id'])['disposition'], 'active')

    def test_changed_gap_mapping_and_cited_section_resurface(self):
        connection = self.aid('connection', dict(requirements='globally generated bundle', mapping={'E': 'F'}))
        item = self.candidate(frontier.scan(self.hub, 'alpha'), connection['id'])
        frontier.act(self.hub, 'alpha', item['id'], 'dismiss', 'Unclear identification', 'fixture')
        revised = dict(connection, expected_revision=1, payload=dict(connection['payload'], mapping={'E': 'G'}))
        knowledge.remember(self.hub, revised, 'alpha', 'fixture')
        changed = self.candidate(frontier.scan(self.hub, 'alpha'), connection['id'])
        self.assertIn(connection['id'], changed['delta']['changed'])
        frontier.act(self.hub, 'alpha', item['id'], 'dismiss', 'Still unclear', 'fixture')
        self.note.write_text(self.note.read_text().replace('same map', 'newly identified map'))
        changed = self.candidate(frontier.scan(self.hub, 'alpha'), connection['id'])
        self.assertTrue(changed['delta']['changed_sections'])

    def test_unlocks_returns_recorded_negative_controls(self):
        gap = self.aid()
        obstruction = dict(self.record, id='A-O001', kind='obstruction', title='Bundle generation obstruction',
                           statement='A different evaluation map fails global generation.')
        self.records.append(obstruction)
        self.write_records()
        out = frontier.unlocks(self.hub, 'globally generated bundle evaluation map', 'alpha')
        self.assertEqual(out['candidates'][0]['watch_id'], gap['id'])
        self.assertEqual(out['controls'][0]['id'], 'A-O001')
        self.assertIn('unproved', out['candidates'][0]['assessment'])

    def test_document_match_stability_and_missed_notification_recovery(self):
        gap = self.aid()
        hit = dict(source_id='hub:NOTE.md', title='Note', root='hub', path='NOTE.md', locator='lines 1-2',
                   tier='documents', matched_terms=['generated'], score=1, sha256='a'*64,
                   text='# Relevant\nA globally generated bundle needs the same map.\n', match_kind='lexical')
        self.mock_recall.return_value = dict(self.empty_recall, hits=[hit])
        out = frontier.scan(self.hub, 'alpha')
        item = next(row for row in out['items'] if row['candidate_id'].startswith('document:'))
        frontier.act(self.hub, 'alpha', item['id'], 'dismiss', 'Already inspected', 'fixture')
        self.note.write_text(self.note.read_text() + '\n# New unrelated section\nLater\n')
        self.mock_recall.return_value = dict(self.empty_recall, hits=[dict(hit, sha256='b'*64)])
        out = frontier.scan(self.hub, 'alpha')
        self.assertIn(item['id'], [row['id'] for row in out['suppressed']])
        self.mock_recall.return_value = self.empty_recall
        out = frontier.scan(self.hub, 'alpha')
        self.assertIn(item['id'], [row['id'] for row in out['suppressed']])
        self.note.write_text(self.note.read_text().replace('same map.', 'new map.'))
        out = frontier.scan(self.hub, 'alpha')
        changed = next(row for row in out['items'] if row['id'] == item['id'])
        self.assertTrue(changed['delta']['document_changed'])

    def test_shortlist_failure_does_not_undo_canonical_publication(self):
        gap = self.aid()
        self.mock_recall.side_effect = OSError('index unavailable')
        out = frontier.scan(self.hub, 'alpha')
        self.assertTrue(out['warnings'])
        self.assertIn(gap['id'], cs.load_catalog(self.hub).knowledge_by_id())
        self.assertTrue(self.candidate(out, gap['id']))

    def test_derived_pin_fields_do_not_create_self_hash_loops(self):
        entry = {'id': 'A-K001', 'revision': 1, 'payload': {'needs': 'actual map',
                 'inspected_input_fingerprint': 'old', 'pins': {'self': 'old'}}}
        revised_pin = copy.deepcopy(entry)
        revised_pin['payload']['inspected_input_fingerprint'] = 'new'
        revised_pin['payload']['pins']['self'] = 'new'
        self.assertEqual(graph.digest(graph.content(entry)), graph.digest(graph.content(revised_pin)))
        revised_pin['payload']['needs'] = 'different map'
        self.assertNotEqual(graph.digest(graph.content(entry)), graph.digest(graph.content(revised_pin)))

    def test_document_hits_use_first_region_per_source(self):
        self.aid()
        hit = dict(source_id='hub:NOTE.md', title='Note', root='hub', path='NOTE.md', locator='lines 1-2',
                   tier='documents', matched_terms=['generated'], score=1, sha256='a'*64,
                   text='# Relevant\nA globally generated bundle needs the same map.\n', match_kind='lexical')
        second = dict(hit, locator='lines 4-5', score=.5, text='# Other\nUnrelated data.\n')
        self.mock_recall.return_value = dict(self.empty_recall, hits=[hit, second])
        out = frontier.scan(self.hub, 'alpha')
        item = next(row for row in out['items'] if row['candidate_id'].startswith('document:'))
        self.assertEqual(item['candidate']['locator'], 'lines 1-2')

    def test_document_only_citation_corrections_reach_brief_and_resurface_dismissal(self):
        gap = self.aid(refs=[])
        hit = dict(source_id='hub:NOTE.md', title='Note', root='hub', path='NOTE.md', locator='lines 1-2',
                   tier='documents', matched_terms=['generated'], score=1, sha256='a'*64,
                   text='A-R001 supplies a generated bundle evaluation map.', match_kind='lexical',
                   current_context={'corrections': [], 'prerequisites': []})
        self.mock_recall.return_value = dict(self.empty_recall, hits=[hit])
        first = frontier.scan(self.hub, 'alpha')
        item = next(row for row in first['items'] if row['candidate_id'].startswith('document:'))
        frontier.act(self.hub, 'alpha', item['id'], 'dismiss', 'Document inspected before correction', 'fixture')
        correction = dict(self.record, id='A-C001', kind='correction', title='Document citation correction',
                          supersedes=['A-R001'], statement='The evaluation map requires another hypothesis.')
        self.records.append(correction)
        self.write_records()
        hit['current_context']['corrections'] = [correction]
        brief = packets.brief(self.hub, 'alpha', gap=gap['id'])
        self.assertIn('A-C001', [row['id'] for row in brief['corrections']])
        self.assertIn('A-R001', brief['document_ingredients'][0]['cited_ids'])
        output = frontier.scan(self.hub, 'alpha')
        changed = next(row for row in output['items'] if row['id'] == item['id'])
        self.assertEqual(changed['disposition'], 'resurfaced')
        self.assertEqual(changed['delta']['added_corrections'], ['A-C001'])
        self.assertIn('A-C001', [row['id'] for row in changed['corrections']])
        # Losing the bounded shortlist hit must not lose its saved citation anchors.
        self.mock_recall.return_value = self.empty_recall
        output = frontier.scan(self.hub, 'alpha')
        retained = next(row for row in output['items'] if row['id'] == item['id'])
        self.assertEqual(retained['disposition'], 'resurfaced')
        self.assertIn('A-C001', [row['id'] for row in retained['corrections']])

    def test_episode_capture_and_revision_do_not_reopen_without_external_change(self):
        gap = self.aid()
        connection = self.aid('connection', dict(requirements='globally generated bundle', mapping={'E': 'F'}),
            refs=[dict(id=gap['id'], relation='gap'), dict(id='A-R001', relation='candidate')])
        first = packets.brief(self.hub, 'alpha', gap=gap['id'])
        episode = self.aid('episode', dict(gap_id=gap['id'], before='Need bundle identification',
            failure='Maps do not agree', reopening_input='New map identification',
            inspected_input_fingerprint=first['fingerprint']), refs=[dict(id=gap['id'], relation='gap')])
        def reopened(packet):
            return [slot for slot in packet['candidate_slots'] if slot['builder'] == 'changed-input-attempt']
        second = packets.brief(self.hub, 'alpha', gap=gap['id'])
        self.assertEqual(first['inspection_fingerprint'], second['inspection_fingerprint'])
        self.assertFalse(reopened(second))
        episode = knowledge.remember(self.hub, dict(episode, expected_revision=1,
            payload=dict(episode['payload'], check='Inspection saved',
                         inspected_input_fingerprint=second['inspection_fingerprint'])), 'alpha', 'fixture')['aid']
        self.assertFalse(reopened(packets.brief(self.hub, 'alpha', gap=gap['id'])))
        self.record['statement'] += ' A new external map is available.'
        self.write_records()
        third = packets.brief(self.hub, 'alpha', gap=gap['id'])
        self.assertTrue(reopened(third))
        episode = knowledge.remember(self.hub, dict(episode, expected_revision=2,
            payload=dict(episode['payload'], inspected_input_fingerprint=third['inspection_fingerprint'])),
            'alpha', 'fixture')['aid']
        self.assertFalse(reopened(packets.brief(self.hub, 'alpha', gap=gap['id'])))
        knowledge.remember(self.hub, dict(connection, expected_revision=1,
            payload=dict(connection['payload'], mapping={'E': 'G'})), 'alpha', 'fixture')
        self.assertTrue(reopened(packets.brief(self.hub, 'alpha', gap=gap['id'])))


if __name__ == '__main__':
    unittest.main()
