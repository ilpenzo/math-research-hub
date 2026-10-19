"""Recall controls use disposable hubs; no live corpora or registries are changed."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import catalog_store as cs
from recall import corpus, index, providers, retrieve


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hub = Path(self.temp.name).resolve()
        self.cfg = dict(schema_version=2, catalog_layout='federated', roots={}, infrastructure_routes=['infra'], projects={})
        for owner, prefix in [('alpha', 'A-'), ('beta', 'B-')]:
            self.cfg['projects'][owner] = dict(title=owner, roots=[], state='active', id_prefix=prefix,
                catalog_state='initialized', document_routes=[f'projects/{owner}'], archive_selectors=[],
                shared_selectors=[], exclude_selectors=[], code_roots=[])
            for kind in cs.KINDS:
                self.write(owner, kind, [])
        (self.hub / 'hub.json').write_text(json.dumps(self.cfg))
        (self.hub / 'infra').mkdir()
        self.a = dict(id='A-R001', project='alpha', kind='result', title='Needle splitting criterion',
            statement=r'Over $\\mathbb{P}^{7}$, the first Chern class is $c_1$.',
            hypotheses=['Exact hypotheses preserved'], limitations='Numerical control only',
            status='reported', depends_on=[], source_refs=[], supersedes=[], subsumed_by=[],
            arbitrary_nested={'clauses': [{'unknown': 'keep verbatim'}]})
        self.write('alpha', 'records', [self.a])

    def write(self, owner, kind, entries):
        path = self.hub / f'projects/{owner}/catalog/{kind}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries))

    def document(self, path, text):
        file = self.hub / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
        return file

    def test_complete_projection_exact_text_exclusions_and_gaps(self):
        text = '# Frozen proof\n\n' + ('a' * 7100) + '\nμ remains exact.\n'
        self.document('projects/alpha/proofs/argument.txt', text)
        self.document('projects/alpha/implementation/answer.txt', 'secret fixture')
        self.document('projects/alpha/consultations/answer.md', 'secret fixture')
        self.document('projects/alpha/RAW-RESPONSE.md', 'secret fixture')
        manifest = corpus.freeze_manifest(self.hub)
        chunks = list(corpus.discover(self.hub, manifest))
        canonical = next(c for c in chunks if c['source_id'] == 'A-R001')
        self.assertEqual(canonical['metadata']['original'], self.a)
        self.assertEqual(json.loads(canonical['text']), self.a)
        parts = [c for c in chunks if c['path'].endswith('argument.txt')]
        self.assertEqual(''.join(c['text'] for c in parts), text)
        self.assertTrue(any('characters' in c['locator'] for c in parts))
        self.assertFalse(any('secret fixture' in c['text'] for c in chunks))
        self.assertGreater(manifest['exclusions']['directory-policy'], 0)

    def test_changed_manifest_file_is_gap_not_silent_success(self):
        file = self.document('projects/alpha/proof.md', 'needle original')
        manifest = corpus.freeze_manifest(self.hub)
        file.write_text('needle changed')
        with self.assertRaisesRegex(ValueError, 'changed after manifest'):
            list(corpus.discover(self.hub, manifest))
        result = index.refresh(self.hub, manifest=manifest)
        self.assertEqual(result['coverage']['eligible_files'], 1)
        self.assertEqual(result['coverage']['indexed_files'], 0)
        self.assertIn('changed after manifest', result['coverage']['parser_gaps'][0]['reason'])

    def test_stale_index_rechecks_transferred_owner(self):
        index.refresh(self.hub)
        self.write('alpha', 'records', [])
        self.write('beta', 'records', [dict(self.a, project='beta')])
        self.assertEqual(retrieve.search(self.hub, 'needle', project='alpha', semantic=False)['hits'], [])
        self.assertEqual(retrieve.search(self.hub, 'owner:alpha needle', semantic=False)['hits'], [])
        self.assertEqual(retrieve.search(self.hub, 'needle', project='beta', semantic=False)['hits'][0]['owner'], 'beta')

    def test_operational_checkpoint_does_not_change_research_ranking(self):
        from recall import knowledge
        index.refresh(self.hub)
        before = retrieve.search(self.hub, 'needle', project='alpha', semantic=False)
        envelope = knowledge.empty_envelope()
        envelope['checkpoints'] = [dict(id='alpha:frontier:test',project='alpha',revision=1,marker='unrelated operational bookkeeping')]
        self.write('alpha','knowledge',envelope)
        after = retrieve.search(self.hub, 'needle', project='alpha', semantic=False)
        self.assertEqual([(r['source_id'],r['score']) for r in before['hits']],[(r['source_id'],r['score']) for r in after['hits']])
        self.assertTrue(after['coverage']['catalog_current'])

    def test_semantics_keeps_qualifiers_ahead_of_large_evidence_metadata(self):
        entry = dict(self.a, statement='The actual map exists only in characteristic zero.',
                     limitations='No characteristic-two transfer is asserted.', evidence=[{'raw':'x'*20000}])
        chunk = corpus.canonical_chunk(cs.load_catalog(self.hub),entry)
        body = index.semantic_body(chunk)
        self.assertIn(entry['statement'],body)
        self.assertIn(entry['limitations'],body)
        self.assertNotIn('x'*100,body)
        self.assertEqual(json.loads(chunk['text']),entry)

    def test_nested_aid_inputs_retain_current_corrections(self):
        from recall import knowledge
        correction = dict(self.a,id='A-C001',kind='correction',title='Exact new limit',supersedes=['A-R001'])
        self.write('alpha','records',[self.a,correction])
        envelope=knowledge.empty_envelope()
        for key,kind,ref,title in [('A-K001','move','A-R001','Mechanism'),('A-K002','gap','A-K001','Zebraprint')]:
            envelope['aids'].append(dict(id=key,project='alpha',kind=kind,revision=1,title=title,
                references=[dict(id=ref,relation='input')],provenance=[],payload={'question':title},
                assessment=dict(state='parent-inspected',scope='fixture only'),disposition='open'))
        self.write('alpha','knowledge',envelope)
        result=retrieve.search(self.hub,'Zebraprint',project='alpha',semantic=False)
        self.assertIn('A-C001',{row['id'] for row in result['corrections']})
        self.assertEqual(result['hits'][0]['current_context']['unresolved'],[])

    def test_incremental_refresh_and_atomic_failure(self):
        self.document('projects/alpha/proof.md', 'needle proof')
        first = index.refresh(self.hub)
        second = index.refresh(self.hub)
        self.assertEqual(second['cache']['parsed_files_reused'], 1)
        self.assertNotEqual(first['generation'], second['generation'])
        with patch.object(index, '_connection', side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError):
                index.refresh(self.hub)
        self.assertEqual(index.status(self.hub)['generation'], second['generation'])
        self.assertTrue(retrieve.search(self.hub, 'needle', project='alpha')['hits'])

    def test_math_normalization_and_aliases_keep_original(self):
        index.refresh(self.hub)
        for query in ('P⁷', 'c1', 'first Chern', 'A-R001'):
            result = retrieve.search(self.hub, query, project='alpha', semantic=False)
            self.assertEqual(result['hits'][0]['source_id'], 'A-R001', query)
            self.assertEqual(result['hits'][0]['metadata']['original'], self.a)
        self.assertIn('ppow7', index.normalize(r'\mathbb{P}^{7}'))
        self.assertIn('mu', index.normalize('μ'))

    def test_math_qualifier_tokens_and_signed_twists(self):
        self.assertIn('hpow1',index.normalize('H^1'))
        self.assertIn('ppow5',index.normalize(r'\mathbf{P}^{5}'))
        self.assertIn('otwistneg1',index.normalize('O(-1)'))
        self.assertNotEqual(index.normalize('O(-1)'),index.normalize('O(1)'))
        self.assertNotEqual(index.normalize('Conjecture I'),index.normalize('Conjecture III'))
        self.assertIn('not',index.normalize('does not imply splitting').split())
        self.assertIn('non',index.normalize('non-split').split())
        negative=dict(self.a,title='O(-1) on P^5',statement='H^1, non-split, Conjecture I; does not imply III.')
        positive=dict(self.a,id='A-R002',title='O(1) on P^5',statement='Conjecture III')
        self.write('alpha','records',[positive,negative]);index.refresh(self.hub)
        result=retrieve.search(self.hub,'title:"O(-1)"',project='alpha',semantic=False)
        self.assertEqual([h['source_id'] for h in result['hits']],['A-R001'])
        self.assertEqual(result['hits'][0]['metadata']['original']['statement'],negative['statement'])

    def test_scope_foreign_prerequisite_and_fresh_correction(self):
        a = dict(self.a, depends_on=['B-R001'])
        b = dict(self.a, id='B-R001', project='beta', title='Needle prerequisite')
        unrelated = dict(b, id='B-R002', title='Needle foreign analogy')
        self.write('alpha', 'records', [a])
        self.write('beta', 'records', [b, unrelated])
        index.refresh(self.hub)
        correction = dict(b, id='B-C001', kind='correction', title='Counterexample unrelated search wording',
                          statement='A new limitation', supersedes=['B-R001'])
        self.write('beta', 'records', [b, unrelated, correction])
        result = retrieve.search(self.hub, 'needle', project='alpha', semantic=False)
        keys = {c['source_id'] for c in result['hits']}
        self.assertIn('A-R001', keys)
        self.assertIn('B-R001', keys)
        self.assertNotIn('B-R002', keys)
        self.assertIn('B-C001', {c['id'] for c in result['corrections']})
        own = next(c for c in result['hits'] if c['source_id'] == 'A-R001')
        self.assertIn('B-C001', {c['id'] for c in own['current_context']['corrections']})
        broad = retrieve.search(self.hub, 'needle', project='alpha', broad=True, semantic=False)
        self.assertIn('B-R002', {c['source_id'] for c in broad['hits']})
        self.assertTrue(result['warnings'])

    def test_live_replacement_deletion_and_no_evidence_scan_at_query(self):
        self.document('projects/alpha/proof.md', 'needle document')
        index.refresh(self.hub)
        self.write('alpha', 'records', [dict(self.a, status='refuted', limitations='Changed now')])
        with patch.object(corpus, 'parse_file', side_effect=AssertionError('query must not parse')):
            result = retrieve.search(self.hub, 'A-R001', project='alpha', semantic=False)
        self.assertEqual(result['hits'][0]['metadata']['original']['status'], 'refuted')
        self.write('alpha', 'records', [])
        result = retrieve.search(self.hub, 'A-R001', project='alpha', semantic=False)
        self.assertFalse(any(c['source_id'] == 'A-R001' for c in result['hits']))

    def test_diversification_and_field_filters(self):
        self.document('projects/alpha/one.txt', ('needle repeated\n' * 800))
        self.document('projects/alpha/two.txt', 'needle independent source\n')
        index.refresh(self.hub)
        result = retrieve.search(self.hub, 'needle', project='alpha', limit=3, semantic=False)
        self.assertEqual(len({h['source_id'] for h in result['hits']}), 3)
        result = retrieve.search(self.hub, 'title:criterion tier:canonical', project='alpha', semantic=False)
        self.assertEqual([h['source_id'] for h in result['hits']], ['A-R001'])
        result = retrieve.search(self.hub, 'needle owner:beta', project='alpha', semantic=False)
        self.assertEqual(result['hits'], [])

    def test_explicit_archive_literature_pdf_gap_and_safe_routes(self):
        archive = self.hub / 'external'
        archive.mkdir()
        (archive / 'old.txt').write_text('needle historical proof')
        (archive / 'paper.pdf').write_bytes(b'%PDF-1.4 fake')
        self.cfg['roots'] = {'archive': str(archive)}
        self.cfg['projects']['alpha']['roots'] = ['archive']
        self.cfg['projects']['alpha']['archive_selectors'] = [{'root': 'archive', 'path': '', 'selector': 'subtree'}]
        (self.hub / 'hub.json').write_text(json.dumps(self.cfg))
        base = corpus.freeze_manifest(self.hub)
        self.assertFalse(base['files'])
        selection = {'archives': [{'root': 'archive', 'path': 'old.txt', 'owner': 'alpha'}],
                     'literature': [{'root': 'archive', 'path': 'paper.pdf', 'owner': 'alpha'}]}
        manifest = corpus.freeze_manifest(self.hub, selection)
        with patch.object(corpus.shutil, 'which', return_value=None):
            result = index.refresh(self.hub, manifest=manifest)
        self.assertEqual(result['coverage']['chunks_by_tier']['archives'], 1)
        self.assertIn('PDF parser unavailable', result['coverage']['parser_gaps'][0]['reason'])
        with self.assertRaisesRegex(ValueError, 'escapes'):
            corpus.freeze_manifest(self.hub, {'archives': ['archive:../hub.json']})

    def test_optional_provider_absent_and_remote_rejected(self):
        result = index.refresh(self.hub, semantic=True)
        self.assertEqual(result['semantic']['state'], 'unavailable')
        self.assertTrue(retrieve.search(self.hub, 'needle')['hits'])
        for url in ('https://example.com', 'http://example.com', 'http://127.0.0.1@evil.test', 'http://127.0.0.1/path'):
            with self.assertRaises(ValueError):
                providers.Ollama({'model': 'model', 'url': url})

    def test_semantic_pin_cache_and_fallback(self):
        self.document('projects/alpha/one/same.txt', 'Identical fixture contents')
        self.document('projects/alpha/two/same.txt', 'Identical fixture contents')
        class FakeProvider:
            calls = 0
            inputs = 0
            def identity(self, timeout=1):
                return {'provider': 'ollama', 'model': 'fake', 'digest': 'artifact', 'url': 'http://127.0.0.1'}
            def embed(self, texts, timeout=1):
                self.calls += 1
                self.inputs += len(texts)
                return [[1., 0., .5] for text in texts]
        provider = FakeProvider()
        with patch.object(providers, 'configured', return_value=provider):
            first = index.refresh(self.hub, semantic=True)
            second = index.refresh(self.hub, semantic=True)
            self.assertEqual(provider.calls, 1)
            self.assertEqual(provider.inputs, 2)
            self.assertEqual(first['semantic']['unique_inputs'], 2)
            self.assertEqual(first['semantic']['deduplicated_inputs'], 1)
            self.assertEqual(second['semantic']['pin']['dimensions'], 3)
            self.assertEqual(second['cache']['embedding_chunks_reused'], 3)
            result = retrieve.search(self.hub, 'unrelated geometric association', project='alpha')
            self.assertTrue(result['semantic']['used'])
            self.assertEqual(result['hits'][0]['match_kind'], 'semantic')
            with patch.object(provider, 'identity', return_value={'provider': 'ollama', 'model': 'fake', 'digest': 'changed'}):
                fallback = retrieve.search(self.hub, 'needle', project='alpha')
            self.assertEqual(fallback['semantic']['state'], 'fallback')
            self.assertTrue(fallback['hits'])

    def test_failed_embedding_batches_resume_without_losing_lexical_generation(self):
        for i in range(20):
            self.document(f'projects/alpha/proof{i}.txt', f'needle independent argument {i}')
        class FakeProvider:
            inputs = 0
            fail = True
            def identity(self, timeout=1):
                return {'provider': 'ollama', 'model': 'fake', 'digest': 'artifact'}
            def embed(self, texts, timeout=1):
                if self.inputs and self.fail:
                    raise providers.ProviderUnavailable('interrupted batch')
                self.inputs += len(texts)
                return [[1., 0.] for text in texts]
        provider = FakeProvider()
        with patch.object(providers, 'configured', return_value=provider):
            interrupted = index.refresh(self.hub, semantic=True)
            self.assertEqual(interrupted['semantic']['state'], 'unavailable')
            self.assertTrue(retrieve.search(self.hub, 'needle', semantic=False)['hits'])
            provider.fail = False
            resumed = index.refresh(self.hub, semantic=True)
        self.assertEqual(provider.inputs, 21)
        self.assertEqual(resumed['semantic']['unique_cached_inputs'], 16)
        self.assertEqual(resumed['semantic']['indexed_vectors'], 21)

    def test_live_field_constraint_and_whole_query_model_budget(self):
        index.refresh(self.hub)
        self.write('alpha', 'records', [dict(self.a, title='Changed title', statement='needle in statement')])
        result = retrieve.search(self.hub, 'title:needle', project='alpha', semantic=False)
        self.assertEqual(result['hits'], [])
        class Provider:
            def identity(self, timeout=1):
                return {'provider': 'ollama', 'model': 'fake', 'digest': 'artifact'}
            def embed(self, texts, timeout=1):
                raise AssertionError('Exhausted query budget must not start embedding')
        with patch.object(providers, 'configured', return_value=Provider()), patch.object(
                providers.time, 'monotonic', side_effect=[0, 2]):
            with self.assertRaisesRegex(ValueError, 'budget exhausted'):
                providers.query_vector(self.hub, 'query', {'provider': 'ollama', 'model': 'fake', 'digest': 'artifact'})

    def test_unindexed_live_fallback_keeps_status_and_limits(self):
        result = retrieve.search(self.hub, 'needle', project='alpha')
        self.assertEqual(result['hits'][0]['metadata']['original']['limitations'], 'Numerical control only')
        self.assertFalse(result['semantic']['used'])
        self.assertIn('not been refreshed', result['warnings'][0])

    def test_knowledge_operational_entries_not_research_chunks(self):
        catalog = cs.load_catalog(self.hub)
        catalog.knowledge = {'alpha': {'aids': [{'id': 'A-K001', 'project': 'alpha', 'kind': 'gap',
            'title': 'Needle missing bridge', 'revision': 1, 'payload': {'missing': 'unknown'}}],
            'sessions': [{'id': 'session-private', 'project': 'alpha', 'title': 'needle session'}]}}
        with patch.object(corpus.cs, 'load_catalog', return_value=catalog):
            chunks = corpus.freeze_manifest(self.hub)['objects']
        self.assertIn('A-K001', {c['source_id'] for c in chunks})
        self.assertNotIn('session-private', {c['source_id'] for c in chunks})


if __name__ == '__main__':
    unittest.main()
