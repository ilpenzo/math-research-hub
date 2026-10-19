"""End-to-end federation controls in disposable hubs; no live catalog writes."""
import contextlib
import copy
import io
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import artifacts
import catalog_store as cs
import catalog_validation as cv
import hub
import hub_operations as ops
import transactions as tx


def competing_commit(root, relative, before, value, queue):
    for _ in range(100):
        try:
            tx.apply_batch(Path(root), {relative: value}, {relative: before}, scopes=['alpha'], owner=value)
            queue.put('committed')
            return
        except tx.TransactionError as exc:
            if 'busy' not in str(exc):
                queue.put(str(exc))
                return
            time.sleep(.01)
    queue.put('timeout')


def die_holding_lock(root):
    with tx.locked(Path(root), exclusive=True):
        os._exit(0)


class FederationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.cfg = dict(schema_version=2, catalog_layout='federated', roots={},
                        infrastructure_routes=['design'], exclude_dirs=[],
                        extensions=['.md'], text_extensions=['.md'], projects={})
        for name, prefix in [('alpha', 'A-'), ('beta', 'B-'), ('gamma', 'G-')]:
            self.cfg['projects'][name] = dict(title=name, roots=[], state='active', id_prefix=prefix,
                catalog_state='initialized', document_routes=[f'projects/{name}'],
                archive_selectors=[], shared_selectors=[], code_roots=[])
            for kind, rel in cs.project_paths(name).items():
                path = self.root / rel
                if kind == 'investigations':
                    path.mkdir(parents=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('[]\n' if kind in cs.KINDS else f'# {name}\n')
        (self.root / 'hub.json').write_text(cs.serialize(self.cfg))
        proof = self.root / 'proof.md'; proof.write_text('fixed evidence\n')
        self.evidence = dict(root='hub', path='proof.md', locator='line 1', sha256=cs.sha256_file(proof))
        self.record = dict(id='A-R001', project='alpha', kind='result', title='needle result', topics=['t'],
            status='reported', evidence_level='imported', novelty='unassessed', statement='fixture only',
            hypotheses=['fixture'], limitations='fixture', evidence=[self.evidence],
            depends_on=[], supersedes=[], subsumed_by=[], source_refs=[])
        self.write('alpha', 'records', [self.record])
        for name, value in [('HUB', self.root), ('CFG', self.cfg), ('ROOTS', {'hub': self.root})]:
            ctx = patch.object(hub, name, value); ctx.start(); self.addCleanup(ctx.stop)

    def write(self, project, kind, rows):
        cs.federated_path(self.root, project, kind).write_text(cs.serialize(rows))

    def capture(self, fn, *args, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = fn(*args, **kwargs)
        return result, out.getvalue()

    def test_missing_file_wrong_owner_mixed_override_and_crlf(self):
        missing = cs.federated_path(self.root, 'beta', 'sources')
        missing.unlink()
        with self.assertRaisesRegex(cs.CatalogError, 'Missing initialized'):
            cs.load_catalog(self.root)
        missing.write_text('[]\n')
        cat = cs.load_catalog(self.root)
        unmoved = cs.plan_split(cat)
        self.assertTrue(cs.verify_split(cat, unmoved, {'A-R001': 'beta'}))
        wrong = cs.plan_split(cat, {'A-R001': 'gamma'})
        self.assertTrue(cs.verify_split(cat, wrong, {'A-R001': 'beta'}))
        path = cs.federated_path(self.root, 'alpha', 'records')
        path.write_bytes(cs.serialize([self.record]).replace('\n', '\r\n').encode())
        cat = cs.load_catalog(self.root)
        self.assertEqual(cat.file_hashes[path.relative_to(self.root).as_posix()], cs.sha256_file(path))
        (self.root / 'catalog').mkdir()
        (self.root / 'catalog/records.json').write_text('[]\n')
        with self.assertRaisesRegex(cs.CatalogError, 'Mixed'):
            cs.load_catalog(self.root, layout='legacy')

    def test_manifest_guards_and_unknown_project(self):
        with self.assertRaisesRegex(cs.CatalogError, 'Unknown project'):
            cs.closure(cs.load_catalog(self.root), 'typo')
        for name in ['../escape', '/absolute', 'bad/name', '.hidden']:
            with self.assertRaises(ValueError):
                cs.federated_path(self.root, name, 'records')
        cfg = copy.deepcopy(self.cfg)
        cfg['projects']['beta']['id_prefix'] = 'A-'
        with self.assertRaisesRegex(cs.CatalogError, 'Duplicate'):
            cs.validate_config(self.root, cfg)
        cfg = copy.deepcopy(self.cfg)
        cfg['projects']['beta']['archive_selectors'] = 'not-a-list'
        with self.assertRaisesRegex(cs.CatalogError, 'must be a list'):
            cs.validate_config(self.root, cfg)
        cfg['schema_version'] = '2'
        with self.assertRaisesRegex(cs.CatalogError, 'schema version'):
            cs.validate_config(self.root, cfg)

    def test_malformed_edges_and_owner_fail_with_catalog_error(self):
        for extra in [dict(depends_on='A-R002'), dict(source_refs=[None]), dict(project=[]), dict(id=[] )]:
            with self.subTest(extra=extra):
                self.write('alpha', 'records', [dict(self.record, **extra)])
                with self.assertRaises(cs.CatalogError):
                    cs.load_catalog(self.root)

    def test_global_typed_edges_scoped_evidence_and_foreign_correction(self):
        source = dict(id='B-L001', project='beta', citation='fixture', url='https://example.com',
            version='1', topics=['t'], locator='1', use='fixture', limitations='fixture',
            check_state='lead', evidence=[self.evidence])
        correction = dict(copy.deepcopy(self.record), id='B-C001', project='beta', kind='correction',
                          supersedes=['A-R001'])
        self.record['source_refs'] = ['B-L001']
        self.write('alpha', 'records', [self.record]); self.write('beta', 'records', [correction])
        self.write('beta', 'sources', [source])
        foreign_bad = dict(copy.deepcopy(self.record), id='G-R001', project='gamma', source_refs=[],
                           evidence=[dict(self.evidence, sha256='0' * 64)])
        self.write('gamma', 'records', [foreign_bad])
        failed, output = self.capture(hub.check, 'alpha')
        self.assertFalse(failed, output)
        self.assertIn('Evidence outside this closure was not checked', output)
        self.assertTrue(self.capture(hub.check)[0])
        _, output = self.capture(hub.search, 'needle', project_filter='alpha')
        self.assertIn('CORRECTION B-C001 [beta]', output)
        self.assertNotIn('G-R001', output)
        correction['supersedes'] = ['MISSING']
        self.write('beta', 'records', [correction])
        cat = cs.load_catalog(self.root)
        self.assertIn('MISSING', cs.closure(cat, 'beta')[2])
        self.assertTrue(any('invalid supersedes' in e for e in cv.validate(cat, self.root, set())))

    def test_scoped_render_writes_only_own_project_and_marks_stale(self):
        self.capture(hub.render)
        keep = [p for p in self.root.rglob('*') if p.is_file() and 'indexes' in p.parts
                and not p.is_relative_to(self.root / 'projects/alpha')]
        before = {p: p.read_bytes() for p in keep}
        self.capture(hub.render, 'alpha')
        self.assertEqual(before, {p: p.read_bytes() for p in keep})
        self.assertEqual(self.capture(hub.view_status, 'alpha')[0], False)
        self.record['limitations'] += ' changed'
        self.write('alpha', 'records', [self.record])
        self.assertTrue(self.capture(hub.view_status, 'alpha')[0])

    def test_uncatalogued_notes_shared_archive_and_broad_fallback(self):
        (self.root / 'projects/alpha/investigations/NOTE.md').write_text('needle own note')
        (self.root / 'design').mkdir(); (self.root / 'design/NOTE.md').write_text('federation design')
        archive = self.root / 'archive'
        for directory in ['alpha', 'beta', 'shared']:
            (archive / directory).mkdir(parents=True)
            (archive / directory / 'NOTE.md').write_text('needle ' + directory)
        self.cfg['roots']['joint'] = str(archive)
        for project in ['alpha', 'beta']:
            info = self.cfg['projects'][project]
            info['roots'] = ['joint']
            info['archive_selectors'] = [dict(root='joint', selector='subtree', path=project)]
            info['shared_selectors'] = [dict(root='joint', selector='subtree', path='shared')]
        (self.root / 'hub.json').write_text(cs.serialize(self.cfg))
        _, out = self.capture(hub.search, 'needle', archive=True, project_filter='alpha')
        self.assertIn('DOC [alpha]', out); self.assertIn('joint:alpha/NOTE.md', out)
        self.assertIn('[shared]', out); self.assertNotIn('joint:beta/NOTE.md', out)
        _, broad = self.capture(hub.search, 'needle', broad_archive=True, project_filter='alpha')
        self.assertIn('joint:beta/NOTE.md', broad)
        self.assertIn('federation design', self.capture(hub.search, 'federation', infrastructure=True)[1])

    def test_fourth_project_no_archive_paused_dependency_and_id_reservation(self):
        ops.add_project(self.root, 'delta', 'Fourth project', 'D-')
        self.assertFalse(self.capture(hub.check)[0])
        first = ops.reserve_id(self.root, 'delta', 'R', 'one')['id']
        second = ops.reserve_id(self.root, 'delta', 'R', 'two')['id']
        self.assertEqual((first, second), ('D-R001', 'D-R002'))
        cat = cs.load_catalog(self.root)
        cfg = copy.deepcopy(cat.cfg); cfg['projects']['alpha']['state'] = 'paused'
        result = dict(copy.deepcopy(self.record), id=first, project='delta', depends_on=['A-R001'])
        rel = 'projects/delta/catalog/records.json'
        ops.apply(self.root, dict(changes={'hub.json': cs.serialize(cfg), rel: cs.serialize([result])},
                                 expected=cat.file_hashes), ['hub'])
        self.assertIn('A-R001', cs.closure(cs.load_catalog(self.root), 'delta')[0])
        with self.assertRaisesRegex(ValueError, 'active'):
            ops.reserve_id(self.root, 'alpha', 'R', 'owner')
        _, helper = self.capture(hub.context_packet, 'delta', 'helper')
        self.assertNotIn('START_HERE', helper); self.assertNotIn('TOPICS', helper)

    def test_invalid_batch_never_publishes_and_exact_transfer_does(self):
        cat = cs.load_catalog(self.root)
        rel = 'projects/alpha/catalog/records.json'
        bad = dict(copy.deepcopy(self.record), depends_on=['MISSING'])
        with self.assertRaisesRegex(ValueError, 'invalid depends_on'):
            ops.apply(self.root, dict(changes={rel: cs.serialize([bad])}, expected=cat.file_hashes), ['alpha'])
        self.assertEqual(cs.load_catalog(self.root).by_id()['A-R001'], self.record)
        moved = dict(copy.deepcopy(self.record), project='beta')
        batch = dict(changes={rel: '[]\n', 'projects/beta/catalog/records.json': cs.serialize([moved])},
                     expected=cat.file_hashes, transfers={'A-R001': 'beta'})
        with self.assertRaisesRegex(ValueError, 'outside'):
            ops.apply(self.root, batch, ['alpha'])
        ops.apply(self.root, batch, ['alpha', 'beta'])
        self.assertEqual(cs.load_catalog(self.root).id_map()['A-R001']['project'], 'beta')

    def test_recovery_every_step_including_layout_switch_and_deletion(self):
        # A minimal cutover changes old files, new files and configuration.
        for stop in range(5):
            for direction in ['rollback', 'finish']:
                with self.subTest(stop=stop, direction=direction), tempfile.TemporaryDirectory() as name:
                    root = Path(name)
                    (root / 'old.json').write_text('old')
                    (root / 'hub.json').write_text('legacy')
                    before = {'old.json': 'old', 'hub.json': 'legacy', 'new-a.json': None, 'new-b.json': None}
                    after = {'old.json': None, 'hub.json': 'federated', 'new-a.json': 'a', 'new-b.json': 'b'}
                    expected = {p: tx.digest(v.encode() if v is not None else None) for p, v in before.items()}
                    with self.assertRaisesRegex(tx.TransactionError, 'Injected'):
                        tx.apply_batch(root, after, expected, fail_after=stop)
                    with self.assertRaisesRegex(tx.TransactionError, 'Interrupted'):
                        tx.snapshot(root, before)
                    tx.recover(root, tx.pending(root)[0], direction)
                    wanted = before if direction == 'rollback' else after
                    self.assertEqual(tx.snapshot(root, wanted), {p: v.encode() if v is not None else None for p, v in wanted.items()})

    def test_recovery_refuses_later_edit(self):
        p = self.root / 'target'; p.write_text('old')
        with self.assertRaises(tx.TransactionError):
            tx.apply_batch(self.root, {'target': 'new'}, {'target': tx.digest(b'old')}, fail_after=1)
        p.write_text('unrelated later edit')
        with self.assertRaisesRegex(tx.TransactionError, 'later/foreign'):
            tx.recover(self.root, tx.pending(self.root)[0])
        self.assertEqual(p.read_text(), 'unrelated later edit')

    def test_competing_processes_no_lost_update_and_disjoint_commits(self):
        ctx = multiprocessing.get_context('spawn')
        for same in [True, False]:
            with self.subTest(same=same):
                for rel in ['one', 'two']:
                    (self.root / rel).write_text('base')
                queue = ctx.Queue()
                processes = [ctx.Process(target=competing_commit, args=(str(self.root), rel, tx.digest(b'base'), value, queue))
                             for rel, value in [('one', 'first'), ('one' if same else 'two', 'second')]]
                for proc in processes: proc.start()
                for proc in processes:
                    proc.join(10); self.assertFalse(proc.is_alive()); self.assertEqual(proc.exitcode, 0)
                results = [queue.get(timeout=2) for _ in processes]
                self.assertEqual(results.count('committed'), 1 if same else 2, results)
                if same:
                    self.assertTrue(any('changed since read' in x for x in results))

    def test_dead_process_releases_kernel_lock(self):
        proc = multiprocessing.get_context('spawn').Process(target=die_holding_lock, args=(str(self.root),))
        proc.start(); proc.join(10)
        self.assertEqual(proc.exitcode, 0)
        with tx.locked(self.root, exclusive=True):
            pass

    def test_artifact_package_retains_bytes_and_detects_changes(self):
        files = []
        for role in ['source', 'input', 'output']:
            path = self.root / f'{role}.txt'; path.write_text(role)
            files.append(dict(path=f'{role}.txt', role=role, **{'from': str(path)}))
        dest = 'projects/alpha/investigations/run/artifacts/one'
        spec = dict(command=['python3', 'source.txt'], environment={'python': 'fixture'},
                    domain='integers', exhaustiveness='bounded fixture', files=files)
        artifacts.pack(self.root, 'alpha', dest, spec)
        manifest = json.loads((self.root / dest / 'artifact-manifest.json').read_bytes())
        self.assertEqual(artifacts.verify_manifest(self.root / dest, manifest), [])
        (self.root / dest / 'source.txt').write_text('changed')
        self.assertTrue(artifacts.verify_manifest(self.root / dest, manifest))
        with self.assertRaisesRegex(ValueError, 'already exists'):
            artifacts.pack(self.root, 'alpha', dest, spec)


if __name__ == '__main__':
    unittest.main()
