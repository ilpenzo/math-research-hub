"""Recovery controls on disposable hubs and synthetic native stores only."""
import copy
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import catalog_store as cs
from recall import knowledge, native, sessions


def competing_checkpoint(root, native_home, thread, ready, start, output):
    os.environ['CODEX_HOME'] = native_home
    os.environ.pop('CODEX_THREAD_ID', None)
    ready.put(True)
    start.wait(8)
    try:
        result = sessions.checkpoint(Path(root), dict(project='alpha', native_thread=thread,
            user_direction='Concurrent fixture ' + thread, selected_direction=thread, next_action='Wait'), 'codex', 'fixture')
        output.put(('ok', result['entry']['id']))
    except Exception as exc:
        output.put(('error', str(exc)))


class SessionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / 'hub'
        self.root.mkdir()
        self.cfg = dict(schema_version=2, catalog_layout='federated', roots={}, projects={})
        for name, prefix in [('alpha', 'A-'), ('beta', 'B-')]:
            self.cfg['projects'][name] = dict(title=name, roots=[], state='active', id_prefix=prefix,
                catalog_state='initialized', document_routes=[], archive_selectors=[], shared_selectors=[], code_roots=[])
            directory = self.root / f'projects/{name}/catalog'
            directory.mkdir(parents=True)
            for kind in cs.KINDS:
                (directory / f'{kind}.json').write_text('[]\n')
        (self.root / 'hub.json').write_text(cs.serialize(self.cfg))
        self.native_home = self.base / 'codex'
        self.claude_home = self.base / 'claude'
        self.native_home.mkdir()
        self.claude_home.mkdir()
        env = patch.dict(os.environ, CODEX_HOME=str(self.native_home), CLAUDE_CONFIG_DIR=str(self.claude_home), CODEX_THREAD_ID='new')
        env.start()
        self.addCleanup(env.stop)
        knowledge.publish_section(self.root, 'hub', 'checkpoints', dict(id=sessions.IDENTITY_ID,
            workspace_id='fixture-workspace', registered_root=str(self.root), prior_roots=[]), 'fixture')
        self.proof = self.root / 'proof.md'
        self.proof.write_text('Initial proof fixture.\n')
        self.record = dict(id='A-R001', project='alpha', kind='result', title='surface evaluation invariant',
            topics=['fixture'], status='reported', evidence_level='imported', novelty='unassessed',
            statement='Fixture condition only.', hypotheses=['fixture'], limitations='Not a mathematical test.',
            evidence=[dict(root='hub', path='proof.md', locator='line 1', sha256=cs.sha256_file(self.proof))],
            depends_on=[], source_refs=[], supersedes=[], subsumed_by=[])
        cs.federated_path(self.root, 'alpha', 'records').write_text(cs.serialize([self.record]))

    def spec(self, thread='one', project='alpha', **kwargs):
        return dict(project=project, native_thread=thread, user_direction='Study surface evaluation invariant',
            selected_direction='Check exact contraction hypotheses', constraints=['Do not launch an old goal'],
            decisions=[dict(text='Inspect recorded invariant', provenance='fixture request')],
            results=[dict(text='Fixture records identified', status='reported, not proof-reviewed')],
            unresolved=['Compatibility of the selected maps'], next_action='Compare recorded hypotheses',
            references=['A-R001'], **kwargs)

    def save(self, spec=None, service='codex'):
        return sessions.checkpoint(self.root, spec or self.spec(), service, 'fixture')['entry']

    def codex_db(self, schema=None):
        db = sqlite3.connect(self.native_home / 'state_5.sqlite')
        db.execute(schema or 'create table threads(id text, cwd text, rollout_path text, updated_at integer, title text, cli_version text, parent_thread_id text)')
        return db

    def transcript(self, thread='one', service='codex', content=None):
        if service == 'codex':
            path = self.native_home / 'sessions' / (thread + '.jsonl')
            content = content or [dict(type='response_item', timestamp='t1', payload=dict(type='message', role='user', content=[dict(type='input_text', text='Surface calculation')]))]
        else:
            path = self.claude_home / 'projects' / native._claude_key(self.root) / (thread + '.jsonl')
            content = content or [dict(type='user', sessionId=thread, cwd=str(self.root), version='2.1.fixture', timestamp='t1', message=dict(role='user', content='Surface calculation'))]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(json.dumps(r) for r in content) + '\n')
        return path

    def test_new_thread_recovery_is_read_only_and_isolates_services(self):
        saved = self.save()
        before = cs.load_catalog(self.root).file_hashes
        packet = sessions.resume(self.root, 'codex', query='surface contraction')
        self.assertEqual(packet['state'], 'recovered')
        self.assertEqual(packet['selected']['id'], saved['id'])
        self.assertFalse(packet['bound'])
        self.assertEqual(packet['native_freshness']['state'], 'unknown')
        self.assertEqual(packet['canonical_freshness']['state'], 'current')
        self.assertEqual(cs.load_catalog(self.root).file_hashes, before)
        self.assertEqual(sessions.resume(self.root, 'claude-code')['state'], 'no-checkpoint')

    def test_exact_thread_wins_and_multiple_contexts_remain_distinct(self):
        one = self.save(self.spec(context_key='gap-one'))
        self.save(self.spec(thread='two', context_key='other'))
        r = sessions.resume(self.root, 'codex', thread='one')
        self.assertEqual(r['selected']['id'], one['id'])
        self.assertEqual(r['current_native_thread'], 'new')
        self.assertEqual(r['requested_native_thread'], 'one')
        self.assertTrue(any('not bound' in warning for warning in r['warnings']))
        self.save(self.spec(context_key='gap-two'))
        r = sessions.resume(self.root, 'codex', thread='one')
        self.assertEqual(r['state'], 'ambiguous')
        self.assertEqual(len(r['choices']), 2)

    def test_explicit_unknown_thread_never_substitutes_another(self):
        self.save()
        packet = sessions.resume(self.root, 'codex', thread='missing')
        self.assertEqual(packet['state'], 'no-checkpoint')
        self.assertNotIn('selected', packet)

    def test_other_project_current_thread_does_not_hide_prior_context(self):
        prior = self.save(self.spec(thread='old-alpha', project='alpha'))
        self.save(self.spec(thread='current', project='beta'))
        with patch.dict(os.environ, CODEX_THREAD_ID='current'):
            packet = sessions.resume(self.root, 'codex', project='alpha')
        self.assertEqual(packet['selected']['id'], prior['id'])

    def test_native_id_query_matches_whole_research_identifier(self):
        row = dict(project='alpha', references=['K001'], user_direction='', selected_direction='', context_key='', locators=[], unresolved=[], next_action='')
        score, reason = sessions._rank(row, 'C-K001', None, None)
        self.assertEqual(score, 0)

    def test_recency_alone_never_selects_and_choices_are_bounded(self):
        for i in range(4):
            self.save(self.spec(thread=f'thread-{i}', context_key=f'gap-{i}'))
        r = sessions.resume(self.root, 'codex')
        self.assertEqual(r['state'], 'ambiguous')
        self.assertEqual(len(r['choices']), 3)

    def test_cwd_infers_project_current_request_overrides_cwd(self):
        alpha = self.save()
        beta = self.save(self.spec(thread='beta', project='beta'))
        r = sessions.resume(self.root, 'codex', cwd=self.root / 'projects/alpha')
        self.assertEqual(r['selected']['id'], alpha['id'])
        r = sessions.resume(self.root, 'codex', cwd=self.root / 'projects/alpha', query='beta project')
        self.assertEqual(r['selected']['id'], beta['id'])

    def test_unknown_native_identity_is_unbound_not_title_derived(self):
        row = self.save(self.spec(thread=None))
        self.assertIsNone(row['native_thread'])
        self.assertTrue(row['unbound_session'])
        row2 = self.save(self.spec(thread=None))
        self.assertNotEqual(row['id'], row2['id'])

    def test_revision_cas_and_kernel_identity_validation(self):
        row = self.save()
        spec = self.spec(id=row['id'], expected_revision=1)
        updated = self.save(spec)
        self.assertEqual(updated['revision'], 2)
        with self.assertRaisesRegex(ValueError, 'Stale revision'):
            self.save(spec)
        changed = dict(updated, native_thread='wrong')
        with self.assertRaisesRegex(ValueError, 'immutable'):
            knowledge.publish_section(self.root, 'alpha', 'sessions', changed, 'fixture', expected_revision=2)
        with self.assertRaisesRegex(ValueError, 'session_schema'):
            knowledge.publish_section(self.root, 'alpha', 'sessions', dict(id='alpha:bad-session'), 'fixture')

    def test_malformed_session_values_report_errors_without_crashing(self):
        row = self.save()
        for field in ('constraints', 'decisions', 'references', 'locators', 'canonical_inputs', 'captured_through', 'results'):
            for bad in (None, 2, 'wrong', {}):
                if field == 'captured_through' and bad == {}:
                    continue
                changed = dict(row, **{field: bad})
                self.assertTrue(sessions.validate_entry(changed), (field, bad))

    def test_new_correction_of_foreign_prerequisite_is_live(self):
        foreign = dict(self.record, id='B-R001', project='beta', title='foreign prerequisite')
        cs.federated_path(self.root, 'beta', 'records').write_text(cs.serialize([foreign]))
        original = dict(self.record, depends_on=['B-R001'])
        cs.federated_path(self.root, 'alpha', 'records').write_text(cs.serialize([original]))
        self.save()
        correction = dict(foreign, id='B-C001', kind='correction', title='New restriction',
                          statement='Fixture prerequisite needs an extra condition.', supersedes=['B-R001'])
        cs.federated_path(self.root, 'beta', 'records').write_text(cs.serialize([foreign, correction]))
        r = sessions.resume(self.root, 'codex', thread='one')
        f = r['canonical_freshness']
        self.assertEqual(f['state'], 'stale')
        self.assertIn('B-C001', f['changed_ids'])
        self.assertEqual(f['current_corrections'][0]['id'], 'B-C001')
        self.assertEqual(r['native_freshness']['state'], 'unknown')

    def test_locator_changed_missing_and_large_file_are_honest(self):
        row = self.save(self.spec(locators=[dict(root='hub', path='proof.md', locator='line 1')]))
        self.proof.write_text('Changed fixture')
        r = sessions.resume(self.root, 'codex', thread='one')
        self.assertEqual(r['canonical_freshness']['changed_locators'], 1)
        self.proof.unlink()
        r = sessions.resume(self.root, 'codex', thread='one')
        self.assertTrue(r['canonical_freshness']['unresolved_locators'])
        self.proof.write_bytes(b'a' * 600000)
        spec = self.spec(thread='large', locators=[dict(root='hub', path='proof.md', locator='large note')])
        self.save(spec)
        r = sessions.resume(self.root, 'codex', thread='large')
        self.assertEqual(r['canonical_freshness']['state'], 'unknown')

    def test_copy_isolated_and_explicit_move_preserves_identity(self):
        row = self.save()
        copied = self.base / 'copy'
        shutil.copytree(self.root, copied)
        self.assertEqual(sessions.resume(copied, 'codex')['state'], 'root-mismatch')
        with self.assertRaisesRegex(ValueError, 'Independent copy'):
            sessions.checkpoint(copied, self.spec(), 'codex', 'fixture')
        identity = cs.load_catalog(copied).knowledge['hub']['checkpoints'][0]
        identity.update(registered_root=str(copied), prior_roots=[str(self.root)])
        knowledge.publish_section(copied, 'hub', 'checkpoints', identity, 'fixture', expected_revision=1)
        self.assertEqual(sessions.resume(copied, 'codex', thread='one')['selected']['id'], row['id'])

    def test_storage_instance_isolated_and_missing_index_is_irrelevant(self):
        self.save()
        self.assertFalse((self.root / '.hub/recall').exists())
        self.assertEqual(sessions.resume(self.root, 'codex')['state'], 'recovered')
        with patch.dict(os.environ, CODEX_HOME=str(self.base / 'different-storage')):
            self.assertEqual(sessions.resume(self.root, 'codex')['state'], 'no-checkpoint')

    def test_codex_scoped_schema_detected_metadata_and_history_tail(self):
        path = self.transcript()
        db = self.codex_db()
        db.executemany('insert into threads values(?,?,?,?,?,?,?)', [
            ('one', str(self.root), str(path), 10, 'Surface fixture', '0.163.fixture', 'parent'),
            ('foreign', '/some/unrelated/workspace', str(path), 20, 'Must not be loaded', '0.163.fixture', None)])
        db.commit()
        db.close()
        result = sessions.refresh(self.root, 'codex', thread='one')
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(result['selected_tail']['state'], 'available')
        self.assertEqual(result['candidates'][0]['fork_parent'], 'parent')
        capture = result['selected_tail']['suggested_capture']
        self.assertEqual(native.freshness('codex', result['candidates'][0], capture)['state'], 'unknown')
        capture['scope'] = 'parent-inspected-conversation'
        row = self.save(self.spec(captured_through=capture))
        self.assertEqual(sessions.resume(self.root, 'codex', thread='one')['native_freshness']['state'], 'current')
        with path.open('a') as stream:
            stream.write('{}\n')
        self.assertEqual(sessions.resume(self.root, 'codex', thread='one')['native_freshness']['state'], 'catch-up-pending')
        path.unlink()
        self.assertEqual(sessions.resume(self.root, 'codex', thread='one')['native_freshness']['state'], 'unavailable')

    def test_native_schema_change_and_absent_history_keep_checkpoint(self):
        self.save()
        db = self.codex_db('create table threads(id text, renamed_cwd text)')
        db.close()
        self.assertEqual(sessions.refresh(self.root, 'codex')['state'], 'schema-unsupported')
        self.assertEqual(sessions.resume(self.root, 'codex')['state'], 'recovered')

    def test_claude_bounded_scoped_headers_and_malformed_native(self):
        path = self.transcript(service='claude-code')
        other = path.with_name('mismatch.jsonl')
        other.write_text(json.dumps(dict(sessionId='mismatch', cwd='/unrelated', message={})))
        malformed = path.with_name('bad.jsonl')
        malformed.write_text('{bad json\n')
        result = sessions.refresh(self.root, 'claude-code')
        self.assertEqual([r['native_thread'] for r in result['candidates']], ['one'])
        self.assertEqual(result['coverage']['skipped_unverified'], 2)
        self.assertEqual(result['coverage']['malformed_headers'], 1)
        packet = sessions.resume(self.root, 'claude-code', query='surface')
        self.assertEqual(packet['state'], 'no-checkpoint')
        self.assertEqual(packet['bootstrap_candidates'][0]['native_thread'], 'one')
        selected = sessions.refresh(self.root, 'claude-code', thread='one')
        self.assertEqual(selected['selected_tail']['state'], 'available')

    def test_tail_budget_and_untrusted_path(self):
        path = self.transcript(content=[dict(type='response_item', payload=dict(role='user', content=[dict(text='x' * 1000)])) for _ in range(100)])
        result = native.tail('codex', dict(path=str(path)), maximum=4096)
        self.assertLessEqual(result['read_bytes'], 4096)
        self.assertGreater(result['omitted_prefix_bytes'], 0)
        self.assertEqual(native.tail('codex', dict(path=str(self.proof)))['state'], 'unavailable')
        cursor = path.stat().st_size
        with path.open('a') as stream:
            stream.write(json.dumps(dict(type='response_item', payload=dict(role='user', content=[dict(text='First appended message must survive incremental boundary')]))))
            stream.write('\n')
        result = native.tail('codex', dict(path=str(path)), after=cursor)
        self.assertIn('First appended', result['excerpts'][0]['text'])

    def test_concurrent_distinct_checkpoints_reconcile_without_overwrite(self):
        ctx = multiprocessing.get_context('spawn')
        ready, output, start = ctx.Queue(), ctx.Queue(), ctx.Event()
        processes = [ctx.Process(target=competing_checkpoint, args=(str(self.root), str(self.native_home), name, ready, start, output)) for name in ('left', 'right')]
        for process in processes:
            process.start()
        for _ in processes:
            ready.get(timeout=8)
        start.set()
        results = [output.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(8)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual([r[0] for r in results], ['ok', 'ok'])
        rows = cs.load_catalog(self.root).knowledge['alpha']['sessions']
        self.assertEqual({r['native_thread'] for r in rows}, {'left', 'right'})


if __name__ == '__main__':
    unittest.main()
