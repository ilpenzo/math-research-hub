"""Knowledge/kernel controls using disposable hubs; no live canonical writes."""
import copy
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import catalog_store as cs
import catalog_validation as cv
import hub_operations as ops
from recall import knowledge as kn
import transactions as tx


def publish_competing(root, marker, ready, start, results):
    """Two writers frozen at the same revision must not silently overwrite."""
    try:
        original = cs.load_catalog
        first = True
        def frozen_load(*args, **kwargs):
            nonlocal first
            catalog = original(*args, **kwargs)
            if first:
                first = False
                ready.put(marker)
                start.wait(5)
            return catalog
        with patch.object(cs, 'load_catalog', side_effect=frozen_load):
            result = kn.publish_section(Path(root), 'alpha', 'checkpoints',
                dict(id='alpha:session:one', marker=marker), 'fixture', expected_revision=1)
        results.put(('committed', result['entry']['revision']))
    except ValueError as exc:
        results.put(('refused', str(exc)))


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.cfg = dict(schema_version=2, catalog_layout='federated', roots={}, projects={})
        for name, prefix in [('alpha', 'A-'), ('beta', 'B-')]:
            self.cfg['projects'][name] = dict(title=name, roots=[], state='active', id_prefix=prefix,
                catalog_state='initialized', document_routes=[], archive_selectors=[],
                shared_selectors=[], code_roots=[])
            directory = self.root / f'projects/{name}/catalog'
            directory.mkdir(parents=True)
            for kind in cs.KINDS:
                (directory / f'{kind}.json').write_text('[]\n')
        (self.root / 'hub.json').write_text(cs.serialize(self.cfg))
        self.proof = self.root / 'proof.md'
        self.proof.write_text('fixture proof bytes\n')
        self.evidence = dict(root='hub', path='proof.md', locator='line 1', sha256=cs.sha256_file(self.proof))
        self.record = dict(id='A-R001', project='alpha', kind='result', title='fixture result', topics=['fixture'],
            status='reported', evidence_level='imported', novelty='unassessed', statement='fixture only',
            hypotheses=['fixture'], limitations='fixture only', evidence=[self.evidence],
            depends_on=[], supersedes=[], subsumed_by=[], source_refs=[])
        cs.federated_path(self.root, 'alpha', 'records').write_text(cs.serialize([self.record]))
        self.spec = dict(kind='gap', title='Specific missing bridge',
            references=[dict(id='A-R001', relation='input')],
            provenance=[dict(self.evidence, origin='fixture note')],
            payload=dict(question='Does the bridge hold?', missing=None,
                clauses=[dict(text='A supplied clause', provenance=[dict(self.evidence, origin='fixture clause')])]),
            assessment=dict(state='user-stated', scope='The question only'), disposition='open')

    def create(self, spec=None, project='alpha'):
        return kn.remember(self.root, copy.deepcopy(self.spec if spec is None else spec), project, 'fixture')

    def path(self, project='alpha'):
        return cs.knowledge_path(self.root, project)

    def batch(self, changes, scopes=('alpha',), **fields):
        cat = cs.load_catalog(self.root)
        expected = dict(cat.file_hashes)
        for relative in changes:
            expected.setdefault(relative, tx.digest(tx.read_bytes(self.root / relative)))
        return ops.apply(self.root, dict(changes=changes, expected=expected, **fields), scopes, 'fixture')

    def test_missing_envelopes_are_coherent_empty_snapshots(self):
        cat = cs.load_catalog(self.root)
        self.assertEqual(set(cat.knowledge), {'alpha', 'beta', 'hub'})
        for project in cat.knowledge:
            self.assertEqual(cat.knowledge[project], kn.empty_envelope())
            self.assertIsNone(cat.file_hashes[self.path(project).relative_to(self.root).as_posix()])
            self.assertFalse(self.path(project).exists())
        cat.knowledge['alpha']['aids'].append({'test': 1})
        self.assertEqual(cat.knowledge['beta']['aids'], [])

    def test_atomic_remember_allocation_payload_and_theorem_separation(self):
        before = cs.load_catalog(self.root)
        result = self.create()
        cat = cs.load_catalog(self.root)
        aid = result['aid']
        self.assertEqual((aid['id'], aid['revision'], aid['project']), ('A-K001', 1, 'alpha'))
        self.assertEqual(aid['payload'], self.spec['payload'])
        self.assertEqual(cat.by_id(), before.by_id())
        self.assertNotIn(aid['id'], cat.by_id())
        self.assertEqual(cat.knowledge_by_id()[aid['id']], aid)
        self.assertEqual(cat.knowledge_location[aid['id']][0], 'aids')
        self.assertEqual(result['affected_ids'], [])
        self.assertEqual(result['affected_knowledge_ids'], ['A-K001'])
        journal = json.loads((self.root / f'.hub/transactions/{result["transaction"]}/journal.json').read_bytes())
        self.assertEqual(journal['state'], 'committed')
        self.assertEqual({row['path'] for row in journal['files']}, {
            'projects/alpha/catalog/knowledge.json', 'projects/alpha/catalog/reservations.json'})
        self.assertEqual(self.create()['aid']['id'], 'A-K002')

    def test_invalid_creation_never_reserves_or_publishes(self):
        for bad in [dict(self.spec, references=[dict(id='MISSING', relation='input')]),
                    dict(self.spec, assessment=dict(state='verified', scope='No')),
                    dict(self.spec, status='verified'), dict(self.spec, kind=[]),
                    dict(self.spec, revision=True), dict(self.spec, payload={'bad': float('nan')})]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.create(bad)
            self.assertFalse(self.path().exists())
            self.assertFalse((self.root / 'projects/alpha/catalog/reservations.json').exists())
        self.assertEqual(self.create()['aid']['id'], 'A-K001')

    def test_revision_checked_update_preserves_supplied_structure(self):
        first = self.create()['aid']
        spec = dict(first, expected_revision=1, payload=dict(first['payload'], missing='New explicit bridge'))
        revised = self.create(spec)['aid']
        self.assertEqual(revised['revision'], 2)
        self.assertEqual(revised['payload']['missing'], 'New explicit bridge')
        with self.assertRaisesRegex(ValueError, 'Stale revision'):
            self.create(spec)
        with self.assertRaisesRegex(ValueError, 'expected_revision'):
            self.create(revised)
        with self.assertRaisesRegex(ValueError, 'existing aid'):
            self.create(dict(self.spec, id='A-K999', expected_revision=1))
        self.assertEqual(cs.load_catalog(self.root).knowledge_by_id()[first['id']], revised)

    def test_sections_namespaces_hub_and_revision_rules(self):
        for project in ('alpha', 'hub'):
            for section in ('dispositions', 'checkpoints'):
                entry = dict(id=f'{project}:{section}:one', arbitrary={'nested': [1, None, 'context']})
                created = kn.publish_section(self.root, project, section, entry, 'fixture')['entry']
                self.assertEqual(created['revision'], 1)
                with self.assertRaisesRegex(ValueError, 'expected_revision'):
                    kn.publish_section(self.root, project, section, entry, 'fixture')
                updated = kn.publish_section(self.root, project, section, dict(created, arbitrary='new'),
                                             'fixture', expected_revision=1)['entry']
                self.assertEqual(updated['revision'], 2)
                with self.assertRaisesRegex(ValueError, 'Stale revision'):
                    kn.publish_section(self.root, project, section, entry, 'fixture', expected_revision=1)
        with self.assertRaisesRegex(ValueError, 'owner'):
            self.create(project='hub')
        for section, entry in [('aids', {'id': 'alpha:one'}), ('checkpoints', {'id': 'session:one'}),
                               ('checkpoints', {'id': 'alpha:other', 'project': 'beta'})]:
            with self.subTest(section=section, entry=entry), self.assertRaises(ValueError):
                kn.publish_section(self.root, 'alpha', section, entry, 'fixture')

    def test_global_uniqueness_section_immutability_and_references(self):
        aid = self.create()['aid']
        connection = self.create(dict(self.spec, kind='connection', references=[dict(id=aid['id'], relation='gap')]))['aid']
        self.assertEqual(connection['references'][0]['id'], 'A-K001')
        session = dict(id='alpha:session:one')
        kn.publish_section(self.root, 'alpha', 'dispositions', session, 'fixture')
        with self.assertRaisesRegex(ValueError, 'duplicate global ID'):
            kn.publish_section(self.root, 'alpha', 'checkpoints', session, 'fixture')
        with self.assertRaisesRegex(ValueError, 'non-research reference'):
            self.create(dict(self.spec, references=[dict(id=session['id'], relation='context')]))
        cat = cs.load_catalog(self.root)
        envelope = copy.deepcopy(cat.knowledge['alpha'])
        moved = envelope['dispositions'].pop()
        envelope['checkpoints'].append(dict(moved, revision=2))
        with self.assertRaisesRegex(ValueError, 'section are immutable'):
            self.batch({self.path().relative_to(self.root).as_posix(): cs.serialize(envelope)})

    def test_malformed_envelopes_and_fields_are_value_errors(self):
        good = dict(self.spec, id='A-K001', project='alpha', revision=1)
        cases = [None, [], {}, dict(kn.empty_envelope(), schema_version=True),
                 dict(kn.empty_envelope(), sessions='bad'), dict(kn.empty_envelope(), aids=[None]),
                 dict(kn.empty_envelope(), unexpected=[])]
        for field, value in [('id', []), ('project', []), ('revision', '1'), ('revision', False),
                             ('kind', {}), ('title', None), ('references', None),
                             ('references', [{'id': [], 'relation': 'x'}]),
                             ('provenance', [{}]), ('assessment', []),
                             ('assessment', {'state': [], 'scope': 'x'}), ('payload', []),
                             ('disposition', None)]:
            cases.append(dict(kn.empty_envelope(), aids=[dict(good, **{field: value})]))
        for field in good:
            missing = copy.deepcopy(good)
            del missing[field]
            cases.append(dict(kn.empty_envelope(), aids=[missing]))
        for envelope in cases:
            with self.subTest(envelope=envelope):
                self.path().write_text(cs.serialize(envelope))
                with self.assertRaises(ValueError):
                    cs.load_catalog(self.root)
        self.path().unlink()

    def test_namespaces_provenance_scope_and_no_theorem_links_to_aids(self):
        aid = self.create()['aid']
        bad = dict(self.record, depends_on=[aid['id']])
        with self.assertRaisesRegex(ValueError, 'invalid depends_on'):
            self.batch({'projects/alpha/catalog/records.json': cs.serialize([bad])})
        bad = dict(self.record, source_refs=[aid['id']])
        with self.assertRaisesRegex(ValueError, 'invalid source'):
            self.batch({'projects/alpha/catalog/records.json': cs.serialize([bad])})
        for provenance in [dict(self.evidence, origin='fixture', path='../escape'),
                           dict(self.evidence, origin='fixture', root='unknown'),
                           dict(self.evidence, origin='fixture', sha256='z' * 64)]:
            with self.subTest(provenance=provenance), self.assertRaises(ValueError):
                self.create(dict(self.spec, provenance=[provenance]))
        cat = cs.load_catalog(self.root)
        envelope = copy.deepcopy(cat.knowledge['alpha'])
        envelope['aids'][0]['id'] = 'B-K001'
        after = copy.deepcopy(cat)
        after.knowledge['alpha'] = envelope
        self.assertTrue(any('namespace' in error for error in kn.validate(after, self.root)))
        after = copy.deepcopy(cat)
        after.knowledge['alpha']['aids'][0]['revision'] = 2
        self.assertTrue(any('scopes' in error for error in kn.validate_transition(cat, after, {'beta'})))

    def test_kernel_rejects_delete_kind_change_and_skipped_revision(self):
        self.create()
        cat = cs.load_catalog(self.root)
        for action in ('delete', 'kind', 'revision'):
            envelope = copy.deepcopy(cat.knowledge['alpha'])
            if action == 'delete':
                envelope['aids'] = []
            elif action == 'kind':
                envelope['aids'][0].update(kind='move', revision=2)
            else:
                envelope['aids'][0]['revision'] = 3
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.batch({self.path().relative_to(self.root).as_posix(): cs.serialize(envelope)})
        with self.assertRaisesRegex(ValueError, 'cannot delete'):
            self.batch({self.path().relative_to(self.root).as_posix(): None})

    def test_noncanonical_path_cannot_bypass_prospective_validation(self):
        self.create()
        for relative in ['projects/alpha/catalog/./knowledge.json', './projects/alpha/catalog/knowledge.json',
                         'projects/alpha//catalog/knowledge.json']:
            with self.subTest(relative=relative), self.assertRaisesRegex(ValueError, 'canonical relative spelling'):
                self.batch({relative: '{}\n'})
        self.assertEqual(len(cs.load_catalog(self.root).knowledge_by_id()), 1)

    def test_manual_aid_requires_reservation_and_joint_theorem_publication(self):
        reserved = ops.reserve_id(self.root, 'alpha', 'K', 'fixture')['id']
        aid = dict(self.spec, id=reserved, project='alpha', revision=1,
                   references=[dict(id='A-R002', relation='new input')])
        envelope = dict(kn.empty_envelope(), aids=[aid])
        theorem = dict(self.record, id='A-R002', title='New result')
        changes = {'projects/alpha/catalog/records.json': cs.serialize([self.record, theorem]),
                   'projects/alpha/catalog/knowledge.json': cs.serialize(envelope)}
        result = self.batch(changes)
        self.assertEqual(result['affected_ids'], ['A-R002'])
        self.assertEqual(result['affected_knowledge_ids'], [reserved])
        self.assertEqual(cs.load_catalog(self.root).knowledge_by_id()[reserved]['references'][0]['id'], 'A-R002')
        envelope['aids'].append(dict(aid, id='A-K999'))
        with self.assertRaisesRegex(ValueError, 'allocated project K ID'):
            self.batch({'projects/alpha/catalog/knowledge.json': cs.serialize(envelope)})

    def test_invalid_joint_batch_is_atomic_and_checks_evidence(self):
        reserved = ops.reserve_id(self.root, 'alpha', 'K', 'fixture')['id']
        aid = dict(self.spec, id=reserved, project='alpha', revision=1)
        bad_record = dict(self.record, evidence=[dict(self.evidence, sha256='0' * 64)])
        with self.assertRaisesRegex(ValueError, 'STALE evidence'):
            self.batch({'projects/alpha/catalog/records.json': cs.serialize([bad_record]),
                        'projects/alpha/catalog/knowledge.json': cs.serialize(dict(kn.empty_envelope(), aids=[aid]))})
        self.assertEqual(cs.load_catalog(self.root).by_id()['A-R001'], self.record)
        self.assertFalse(self.path().exists())

    def test_knowledge_only_does_not_hash_math_evidence_or_inventory(self):
        self.proof.write_text('evidence changed; theorem cannot be revalidated')
        old_read = Path.read_bytes
        def guard(path):
            if path == self.proof or path.name in {'inventory.json', 'coverage.json'}:
                self.fail('Supplemental publication read the mathematical evidence/inventory')
            return old_read(path)
        with patch.object(Path, 'read_bytes', guard):
            self.create()
            kn.publish_section(self.root, 'hub', 'checkpoints', dict(id='hub:session:one'), 'fixture')
        self.assertTrue(any('STALE' in error for error in cv.validate(cs.load_catalog(self.root), self.root)))

    def test_cross_project_associative_consumers_reported_separately(self):
        aid = self.create()['aid']
        foreign = self.create(dict(self.spec, kind='connection', references=[dict(id=aid['id'], relation='analogy')]), 'beta')['aid']
        result = self.create(dict(aid, expected_revision=1, disposition='revisit'))
        self.assertEqual(result['affected_projects'], ['alpha', 'beta'])
        self.assertEqual(result['affected_knowledge_ids'], [aid['id'], foreign['id']])
        self.assertEqual(result['affected_ids'], [])
        correction = dict(self.record, id='B-C001', project='beta', kind='correction', supersedes=['A-R001'])
        result = self.batch({'projects/beta/catalog/records.json': cs.serialize([correction])}, ('beta',))
        self.assertEqual(result['affected_ids'], ['A-R001', 'B-C001'])
        self.assertEqual(result['affected_knowledge_ids'], [aid['id'], foreign['id']])
        cat = cs.load_catalog(self.root)
        self.assertEqual(cat.by_id()['A-R001']['status'], 'reported')
        self.assertEqual(cat.knowledge_by_id()[foreign['id']]['revision'], 1)
        # Removing a correction's old target changes that target's current
        # correction membership even though no theorem status is rewritten.
        correction['supersedes'] = []
        result = self.batch({'projects/beta/catalog/records.json': cs.serialize([correction])}, ('beta',))
        self.assertEqual(result['affected_ids'], ['A-R001', 'B-C001'])
        self.assertEqual(result['affected_knowledge_ids'], [aid['id'], foreign['id']])

    def test_stale_missing_file_hash_and_concurrent_replacements(self):
        cat = cs.load_catalog(self.root)
        self.create()
        with self.assertRaisesRegex(ValueError, 'changed since read'):
            ops.apply(self.root, dict(changes={'projects/alpha/catalog/knowledge.json': cs.serialize(kn.empty_envelope())},
                                     expected=cat.file_hashes), ['alpha'])
        kn.publish_section(self.root, 'alpha', 'checkpoints', dict(id='alpha:session:one'), 'fixture')
        ctx = multiprocessing.get_context('spawn')
        ready, results, start = ctx.Queue(), ctx.Queue(), ctx.Event()
        workers = [ctx.Process(target=publish_competing, args=(str(self.root), marker, ready, start, results))
                   for marker in ('one', 'two')]
        for worker in workers:
            worker.start()
        for _ in workers:
            ready.get(timeout=5)
        start.set()
        for worker in workers:
            worker.join(10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(worker.exitcode, 0)
        outcomes = [results.get(timeout=5) for _ in workers]
        self.assertEqual([state for state, _ in outcomes].count('committed'), 1, outcomes)
        self.assertTrue(any('changed since read' in str(detail) for state, detail in outcomes if state == 'refused'))
        self.assertEqual(cs.load_catalog(self.root).knowledge_by_id()['alpha:session:one']['revision'], 2)

    def test_busy_retry_is_bounded_and_never_retries_stale_cas(self):
        original = ops.apply
        calls = []
        def busy(*args, **kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise tx.TransactionError('Hub is busy: fixture; retry')
            return original(*args, **kwargs)
        with patch.object(ops, 'apply', side_effect=busy):
            self.create()
        self.assertEqual(len(calls), 3)
        with patch.object(ops, 'apply', side_effect=tx.TransactionError('changed since read')) as mocked:
            with self.assertRaisesRegex(ValueError, 'changed since read'):
                self.create()
        self.assertEqual(mocked.call_count, 1)
        with patch.object(ops, 'apply', side_effect=tx.TransactionError('Hub is busy: fixture')) as mocked, patch.object(kn.time, 'sleep'):
            with self.assertRaisesRegex(ValueError, 'busy'):
                self.create()
        self.assertEqual(mocked.call_count, 8)

    def test_interrupted_atomic_create_can_recover_without_orphan_id(self):
        original = tx.apply_batch
        def interrupted(*args, **kwargs):
            return original(*args, **kwargs, fail_after=1)
        with patch.object(tx, 'apply_batch', side_effect=interrupted):
            with self.assertRaisesRegex(ValueError, 'Injected'):
                self.create()
        with self.assertRaisesRegex(ValueError, 'Interrupted'):
            cs.load_catalog(self.root)
        tx.recover(self.root, tx.pending(self.root)[0], 'rollback')
        self.assertFalse(self.path().exists())
        self.assertFalse((self.root / 'projects/alpha/catalog/reservations.json').exists())
        self.assertEqual(self.create()['aid']['id'], 'A-K001')

    def test_scaffold_and_paused_allocation_and_readonly_shared_lock(self):
        ops.add_project(self.root, 'delta', 'Fourth project', 'D-')
        self.assertEqual(json.loads(self.path('delta').read_bytes()), kn.empty_envelope())
        self.assertEqual(ops.reserve_id(self.root, 'delta', 'K', 'fixture')['id'], 'D-K001')
        cat = cs.load_catalog(self.root)
        cfg = copy.deepcopy(cat.cfg)
        cfg['projects']['alpha']['state'] = 'paused'
        self.batch({'hub.json': cs.serialize(cfg)}, ('hub',))
        with self.assertRaisesRegex(ValueError, 'active'):
            self.create()
        old_open = Path.open
        def read_only(path, mode='r', *args, **kwargs):
            if path.name == 'publication.lock' and mode != 'rb':
                self.fail('An existing shared-lock reader requested write access')
            return old_open(path, mode, *args, **kwargs)
        with patch.object(Path, 'open', read_only):
            cs.load_catalog(self.root)


if __name__ == '__main__':
    unittest.main()
