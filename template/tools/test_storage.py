"""Storage/recovery regressions on disposable hubs, including incompressible data."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import catalog_store as cs
import hub as hub_cli
import storage
import storage_codec as codec
import storage_policy
import transactions as tx
from recall import checkpoint_store as bodies, knowledge, index, cache_storage, retrieve


def fixture(root):
    cfg = dict(schema_version=2, catalog_layout='federated', roots={}, projects={})
    cfg['projects']['alpha'] = dict(title='Alpha', roots=[], state='active', id_prefix='A-',
        catalog_state='initialized', document_routes=['projects/alpha'], archive_selectors=[], shared_selectors=[], code_roots=[])
    (root / 'hub.json').write_text(cs.serialize(cfg))
    catalog = root / 'projects/alpha/catalog'
    catalog.mkdir(parents=True)
    for kind in cs.KINDS:
        (catalog / (kind + '.json')).write_text('[]\n')
    return cfg


def legacy(root, identifier='legacy-1', before=b'old', after=b'new', state='committed'):
    directory = root / '.hub/transactions' / identifier
    directory.mkdir(parents=True)
    row = dict(path='value', index=0, before=tx.digest(before), after=tx.digest(after))
    for label, raw in [('before', before), ('after', after)]:
        if raw is not None:
            (directory / ('0.' + label)).write_bytes(raw)
    journal = dict(id=identifier, owner='fixture', scopes=['hub'], state=state,
                   created_at='2026-01-01T00:00:00+00:00', files=[row])
    (directory / 'journal.json').write_text(json.dumps(journal))
    if after is not None:
        (root / 'value').write_bytes(after)
    return directory, journal


class StorageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_codec_roundtrip_and_dependency_free_fallback(self):
        for data in (b'', b'abc' * 10000, os.urandom(200000)):
            for name in ('xz', codec.preferred_codec()):
                ref, packed = codec.pack(data, name)
                self.assertEqual(codec.unpack(packed, ref), data)
                with self.assertRaises(ValueError):
                    codec.unpack(packed + b'x', ref)
                with self.assertRaises(ValueError):
                    codec.unpack(packed, dict(ref, bytes=ref['bytes'] + 1))
        with patch.object(codec, '_zstd', None), patch.object(codec, '_zstandard', None):
            ref, packed = codec.pack(b'portable')
            self.assertEqual(ref['codec'], 'xz')
            self.assertEqual(codec.unpack(packed, ref), b'portable')

    def test_null_empty_dedup_and_noop(self):
        root = self.root
        first = tx.apply_batch(root, {'a': b'', 'b': b'same', 'c': b'same'}, {'a': None, 'b': None, 'c': None})
        jp = root / '.hub/transactions' / first / 'journal.json'
        j = json.loads(jp.read_bytes())
        self.assertEqual(j['schema_version'], 2)
        self.assertEqual(len(list((root / '.hub/transactions/blobs').glob('*/*'))), 2)
        self.assertIsNone(tx.apply_batch(root, {'b': b'same'}, {'b': tx.digest(b'same')}))
        tx.apply_batch(root, {'a': None}, {'a': tx.digest(b'')})
        self.assertFalse((root / 'a').exists())
        self.assertTrue(storage.verify_journal(root)['valid'])

    def test_reuse_deduplicates_across_codec_preferences(self):
        raw = b'previous runtime payload'
        ref, packed = codec.pack(raw, 'xz')
        with tx.locked(self.root, exclusive=True):
            tx._store_blob(self.root, ref, packed)
        identifier = tx.apply_batch(self.root, {'a': raw}, {'a': None})
        journal = json.loads((self.root / '.hub/transactions' / identifier / 'journal.json').read_bytes())
        self.assertEqual(journal['files'][0]['payloads']['after']['codec'], 'xz')
        self.assertEqual(len(list((self.root / '.hub/transactions/blobs').glob('*/*'))), 1)

    def test_pending_recovery_all_stages_and_directions(self):
        for phase in ('after_pending', 'before_commit'):
            for direction in ('rollback', 'finish'):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    (root / 'a').write_bytes(b'old')
                    with self.assertRaisesRegex(tx.TransactionError, 'Injected'):
                        tx.apply_batch(root, {'a': b'new', 'empty': b''},
                            {'a': tx.digest(b'old'), 'empty': None}, fail_phase=phase)
                    pending = tx.pending(root)[0]
                    tx.recover(root, pending, direction)
                    self.assertEqual((root / 'a').read_bytes(), b'old' if direction == 'rollback' else b'new')
                    self.assertEqual((root / 'empty').exists(), direction == 'finish')
                    self.assertTrue(storage.verify_journal(root)['valid'])

    def test_prejournal_orphan_collection_and_pending_roots(self):
        with self.assertRaises(tx.TransactionError):
            tx.apply_batch(self.root, {'a': b'orphan'}, {'a': None}, fail_phase='after_blobs')
        self.assertFalse(tx.pending(self.root))
        self.assertFalse((self.root / 'a').exists())
        self.assertEqual(storage.collect_journal_orphans(self.root)['unreferenced_blobs'], 1)
        storage.collect_journal_orphans(self.root, True)
        with self.assertRaises(tx.TransactionError):
            tx.apply_batch(self.root, {'a': b'pending'}, {'a': None}, fail_after=0)
        self.assertEqual(storage.collect_journal_orphans(self.root, True)['unreferenced_blobs'], 0)
        tx.recover(self.root, tx.pending(self.root)[0], 'finish')

    def test_space_refusal_precedes_publication(self):
        with patch.object(storage_policy.shutil, 'disk_usage', return_value=type('Space', (), {'free': 0})()):
            with self.assertRaisesRegex(tx.TransactionError, 'Insufficient free'):
                tx.apply_batch(self.root, {'a': b'new'}, {'a': None})
        self.assertFalse((self.root / 'a').exists())
        self.assertFalse(list((self.root / '.hub/transactions').glob('*/journal.json')))

    def test_enospc_after_pending_is_recoverable(self):
        replace = tx._replace
        def fail(path, data):
            if Path(path).resolve() == (self.root / 'a').resolve():
                raise OSError(28, 'No space left on device')
            return replace(path, data)
        with patch.object(tx, '_replace', side_effect=fail):
            with self.assertRaises(OSError):
                tx.apply_batch(self.root, {'a': b'new'}, {'a': None})
        self.assertEqual(len(tx.pending(self.root)), 1)
        tx.recover(self.root, tx.pending(self.root)[0], 'finish')
        self.assertEqual((self.root / 'a').read_bytes(), b'new')

    def test_commit_marker_failure_leaves_recoverable_pending_journal(self):
        write = tx._json_write
        def fail(path, value):
            if value.get('state') == 'committed':
                raise OSError(28, 'No space left on device')
            return write(path, value)
        with patch.object(tx, '_json_write', side_effect=fail):
            with self.assertRaises(OSError):
                tx.apply_batch(self.root, {'a': b'new'}, {'a': None})
        self.assertEqual((self.root / 'a').read_bytes(), b'new')
        tx.recover(self.root, tx.pending(self.root)[0], 'rollback')
        self.assertFalse((self.root / 'a').exists())

    def test_corrupt_blob_and_foreign_bytes_refuse_recovery(self):
        (self.root / 'a').write_bytes(b'old')
        with self.assertRaises(tx.TransactionError):
            tx.apply_batch(self.root, {'a': b'new'}, {'a': tx.digest(b'old')}, fail_after=0)
        identifier = tx.pending(self.root)[0]
        (self.root / 'a').write_bytes(b'foreign')
        with self.assertRaisesRegex(tx.TransactionError, 'foreign'):
            tx.recover(self.root, identifier)
        (self.root / 'a').write_bytes(b'old')
        jp = self.root / '.hub/transactions' / identifier / 'journal.json'
        ref = json.loads(jp.read_bytes())['files'][0]['payloads']['after']
        tx.blob_path(self.root, ref).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            tx.recover(self.root, identifier, 'finish')
        self.assertEqual((self.root / 'a').read_bytes(), b'old')

    def test_legacy_recovery_and_mixed_journals(self):
        _, journal = legacy(self.root, state='pending')
        tx.recover(self.root, journal['id'], 'rollback')
        self.assertEqual((self.root / 'value').read_bytes(), b'old')
        tx.apply_batch(self.root, {'other': b'value'}, {'other': None})
        self.assertTrue(storage.verify_journal(self.root)['valid'])

    def test_compaction_dryrun_lossless_resume_and_no_history_pruning(self):
        data = os.urandom(20000)
        first, original = legacy(self.root, before=data, after=b'')
        legacy(self.root, 'legacy-2', before=b'', after=data)
        before_names = {p.parent.name for p in (self.root / '.hub/transactions').glob('*/journal.json')}
        plan = storage.compact_journal(self.root)
        self.assertEqual(plan['unique_payloads'], 2)
        self.assertTrue((first / '0.before').exists())
        with self.assertRaisesRegex(tx.TransactionError, 'Injected'):
            storage.compact_journal(self.root, True, fail_phase='after_journal_switch')
        self.assertTrue((first / '0.before').exists())
        storage.compact_journal(self.root, True)
        self.assertFalse((first / '0.before').exists())
        self.assertEqual(before_names, {p.parent.name for p in (self.root / '.hub/transactions').glob('*/journal.json')})
        saved = json.loads((first / 'journal.json').read_bytes())
        for field in ('id', 'owner', 'state', 'created_at', 'scopes'):
            self.assertEqual(saved[field], original[field])
        self.assertEqual(tx.read_payload(self.root, first, saved, saved['files'][0], 'before'), data)
        self.assertEqual(storage.compact_journal(self.root, True)['converted_journals'], 0)
        self.assertTrue(storage.verify_journal(self.root)['valid'])

    def test_compaction_rejects_corrupt_raw_and_skips_pending(self):
        directory, _ = legacy(self.root)
        (directory / '0.before').write_bytes(b'corruption')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            storage.compact_journal(self.root, True)
        self.assertNotIn('schema_version', json.loads((directory / 'journal.json').read_bytes()))
        (directory / '0.before').write_bytes(b'old')
        _, pending = legacy(self.root, 'pending-1', state='pending')
        plan = storage.compact_journal(self.root)
        self.assertEqual(plan['skipped_pending'], [pending['id']])


class CheckpointStorageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        fixture(self.root)

    def checkpoint(self, count=4):
        state = dict(fingerprint='semantic-unchanged', objects={'A-R001': 'hash'},
                     sections={'locator': {'path': 'proof.md', 'status': 'unresolved'}},
                     unresolved=[{'reason': 'first'}, {'reason': 'first'}])
        items = {str(i): dict(watch_id='watch', candidate_id=str(i), input_state=copy.deepcopy(state)) for i in range(count)}
        items['0']['input_state']['unresolved'].append({'reason': 'different diagnostics with same fingerprint'})
        return dict(id='alpha:frontier-checkpoint:fixture', project='alpha', revision=1,
                    purpose=bodies.PURPOSE, captured_at='2026-01-01', fingerprint='manifest',
                    scope={'project': 'alpha'}, input_manifest={'objects': {}, 'watch_revisions': {}}, items=items)

    def test_full_content_dedup_preserves_diagnostics_lists_and_manifest(self):
        entry = self.checkpoint(200)
        compact, files = bodies.prepare(self.root, 'alpha', entry)
        self.assertNotIn('items', compact)
        restored = bodies.hydrate(self.root, 'alpha', compact, {p: x[0] for p, x in files.items()})
        self.assertEqual(restored, entry)
        self.assertEqual(list(restored['items']), list(entry['items']))
        small = bodies.compact({'items': entry['items']})
        large = self.checkpoint(400)
        # Doubling matches adds only item structure; common closure nodes stay shared.
        self.assertLess(len(bodies.canonical(bodies.compact({'items': large['items']}))), 2.1 * len(bodies.canonical(small)))

    def test_lossless_legacy_migration_idempotence_and_full_snapshot(self):
        entry = self.checkpoint()
        env = knowledge.empty_envelope(); env['checkpoints'] = [entry]
        path = cs.knowledge_path(self.root, 'alpha')
        path.write_text(cs.serialize(env))
        plan = storage.migrate_checkpoints(self.root)
        self.assertEqual(plan['projects'][0]['checkpoints'], [entry['id']])
        self.assertEqual(json.loads(path.read_bytes()), env)
        storage.migrate_checkpoints(self.root, apply=True)
        saved = cs.load_catalog(self.root).knowledge['alpha']['checkpoints'][0]
        self.assertEqual(cs.load_catalog(self.root).knowledge['alpha']['schema_version'], 2)
        restored = bodies.hydrate(self.root, 'alpha', saved)
        self.assertEqual(restored, dict(entry, revision=2))
        self.assertFalse(storage.migrate_checkpoints(self.root, apply=True)['projects'])
        import contextlib, io
        with patch.object(hub_cli, 'HUB', self.root):
            full = io.StringIO()
            with contextlib.redirect_stdout(full):
                hub_cli.export_snapshot('alpha')
            self.assertEqual(json.loads(full.getvalue())['knowledge']['checkpoints'][0], restored)
            small = io.StringIO()
            with contextlib.redirect_stdout(small):
                hub_cli.export_snapshot('alpha', True)
            self.assertEqual(set(json.loads(small.getvalue())), {'project', 'expected'})

    def test_body_corruption_missing_and_immutable_guards(self):
        saved = knowledge.publish_section(self.root, 'alpha', 'checkpoints', self.checkpoint(), 'fixture')['entry']
        relative = saved['body']['path']; path = self.root / relative
        original = path.read_bytes()
        with self.assertRaisesRegex(tx.TransactionError, 'cannot be replaced'):
            tx.apply_batch(self.root, {relative: None}, {relative: tx.digest(original)})
        path.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            cs.load_catalog(self.root)
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'Missing canonical'):
            cs.load_catalog(self.root)

    def test_one_hundred_updates_do_not_copy_large_checkpoint_bodies(self):
        entry = self.checkpoint()
        entry['input_manifest']['uncompressible'] = os.urandom(700000).hex()
        saved = knowledge.publish_section(self.root, 'alpha', 'checkpoints', entry, 'fixture')['entry']
        body = self.root / saved['body']['path']
        original = body.read_bytes()
        spec = dict(kind='move', title='Fixture', references=[], provenance=[], payload={'mechanism': 'test'},
                    assessment={'state': 'parent-inspected', 'scope': 'fixture'}, disposition='open')
        aid = knowledge.remember(self.root, spec, 'alpha', 'fixture')['aid']
        baseline = storage.usage(self.root / '.hub/transactions')['logical_bytes']
        for i in range(100):
            aid = knowledge.remember(self.root, dict(aid, expected_revision=aid['revision'], title=f'Update {i}'), 'alpha', 'fixture')['aid']
        added = storage.usage(self.root / '.hub/transactions')['logical_bytes'] - baseline
        self.assertLess(added, 2 * 1024**2)
        self.assertEqual(body.read_bytes(), original)
        self.assertLess(cs.knowledge_path(self.root, 'alpha').stat().st_size, 10000)


class CacheStorageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        fixture(self.root)
        (self.root / 'projects/alpha/note.md').write_text('needle fixture')

    def configure(self, **updates):
        path = self.root / storage_policy.CONFIG
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(dict(schema_version=1, **updates)))

    def test_reader_survives_two_refreshes_then_gc_retains_two(self):
        first = index.refresh(self.root)['generation']
        with cache_storage.reader(self.root) as pinned:
            self.assertEqual(pinned.name, first)
            index.refresh(self.root); index.refresh(self.root)
            self.assertTrue(pinned.exists())
            from contextlib import closing
            with closing(sqlite3.connect(pinned / 'index.sqlite')) as connection:
                self.assertGreater(connection.execute('SELECT count(*) FROM chunks').fetchone()[0], 0)
            self.assertTrue(index.collect(self.root)['reader_pinned'])
        index.collect(self.root)
        self.assertFalse(pinned.exists())
        self.assertEqual(len(list((self.root / '.hub/recall/generations').iterdir())), 2)
        self.assertTrue(retrieve.search(self.root, 'needle', semantic=False)['hits'])

    def test_budget_failure_keeps_current_and_cleans_failed_build(self):
        original = index.refresh(self.root)['generation']
        size = cache_storage.size(self.root / '.hub/recall/generations')
        self.configure(generation_budget_bytes=size + 65536)
        with self.assertRaisesRegex(ValueError, 'budget exhausted'):
            index.refresh(self.root)
        self.assertEqual(index.current_generation(self.root).name, original)
        self.assertEqual(len(list((self.root / '.hub/recall/generations').iterdir())), 1)

    def test_small_cache_budget_does_not_drop_parsed_content(self):
        self.configure(cache_budget_bytes=1)
        result = index.refresh(self.root)
        self.assertEqual(result['coverage']['indexed_files'], 1)
        self.assertLessEqual(cache_storage.size(self.root / '.hub/recall/cache'), 1)
        self.assertTrue(retrieve.search(self.root, 'needle', semantic=False)['hits'])

    def test_abandoned_ready_build_is_not_previous_generation(self):
        index.refresh(self.root)
        root = self.root / '.hub/recall/generations'
        orphan = root / ('f' * 32); orphan.mkdir()
        (orphan / 'status.json').write_text(json.dumps(dict(ready=True, created_at='9999')))
        index.collect(self.root)
        self.assertFalse(orphan.exists())


if __name__ == '__main__':
    unittest.main()
