"""Tests for the preparatory federated catalog store; temporary hubs only."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('catalog_store', Path(__file__).with_name('catalog_store.py'))
cs = importlib.util.module_from_spec(spec)
sys.modules['catalog_store'] = cs  # dataclasses resolve annotations via sys.modules
spec.loader.exec_module(cs)

CFG = {'projects': {'research': {'title': 'VB', 'roots': ['vb']},
                    'second-project': {'title': 'CP', 'roots': ['cp']},
                    'third-project': {'title': 'KC', 'roots': ['toric']}}}


def record(key, **extra):
    base = dict(id=key, kind='result', title=f'title {key}', topics=['t'], status='reported',
                evidence_level='imported', novelty='unassessed', statement='s', hypotheses=['h'],
                limitations='l', evidence=[], depends_on=[], supersedes=[], subsumed_by=[],
                source_refs=[])
    base.update(extra)
    return base


def source(key, **extra):
    base = dict(id=key, citation=f'cite {key}', url='https://example.com', version='1',
                topics=['t'], locator='1', use='u', limitations='l', check_state='lead', evidence=[])
    base.update(extra)
    return base


class CatalogStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hub = Path(self.temp.name).resolve()
        (self.hub / 'catalog').mkdir()
        (self.hub / 'hub.json').write_text(json.dumps(CFG))
        # Legacy fixture: VB entries carry no project field, as in the real hub.
        self.records = [
            record('R031', title='HP U/V Ext'),
            record('O012', kind='obstruction', depends_on=['R031']),
            record('R100', source_refs=['L001']),
            record('B-R001', project='second-project', source_refs=['B-L001']),
            record('B-C001', kind='correction', project='second-project',
                   supersedes=['B-R001']),
        ]
        self.sources = [source('L001'), source('B-L001', project='second-project')]
        self.write_legacy(self.records, self.sources)

    def write_legacy(self, records, sources):
        (self.hub / 'catalog/records.json').write_text(cs.serialize(records))
        (self.hub / 'catalog/sources.json').write_text(cs.serialize(sources))

    def materialize(self, plan):
        for kind in cs.KINDS:
            os.remove(self.hub / f'catalog/{kind}.json')
        for proj, kinds in plan.items():
            for kind in cs.KINDS:
                path = cs.federated_path(self.hub, proj, kind)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(cs.serialize(kinds[kind]))

    def test_legacy_load_ownership_and_hashes(self):
        cat = cs.load_catalog(self.hub, CFG)
        self.assertEqual(cat.layout, 'legacy')
        self.assertEqual(len(cat.records), 5)
        self.assertEqual(cat.id_map()['R031']['project'], 'research')
        self.assertEqual(cat.id_map()['B-L001'], dict(project='second-project',
                                                       kind='sources', file='catalog/sources.json'))
        self.assertEqual(cat.file_hashes['catalog/records.json'],
                         cs.sha256_file(self.hub / 'catalog/records.json'))

    def test_split_round_trip_preserves_every_entry_exactly(self):
        cat = cs.load_catalog(self.hub, CFG)
        plan = cs.plan_split(cat)
        self.assertEqual(cs.verify_split(cat, plan), [])
        self.materialize(plan)
        fed = cs.load_catalog(self.hub, CFG)
        self.assertEqual(fed.layout, 'federated')
        self.assertEqual(sorted(map(cs.canonical, fed.entries())),
                         sorted(map(cs.canonical, cat.entries())))
        vb = json.loads(cs.federated_path(self.hub, 'research', 'records').read_text())
        self.assertTrue(all('project' not in e for e in vb), 'split must not add project fields')
        self.assertEqual(cs.federated_path(self.hub, 'third-project', 'records').read_text(), '[]\n')

    def test_declared_transfer_is_the_only_permitted_change(self):
        cat = cs.load_catalog(self.hub, CFG)
        transfers = {'R031': 'third-project', 'O012': 'third-project'}
        plan = cs.plan_split(cat, transfers)
        self.assertEqual(cs.verify_split(cat, plan, transfers), [])
        moved = {e['id'] for e in plan['third-project']['records']}
        self.assertEqual(moved, {'R031', 'O012'})
        self.assertTrue(all(e['project'] == 'third-project' for e in plan['third-project']['records']))
        tampered = copy.deepcopy(plan)
        tampered['third-project']['records'][0]['status'] = 'verified'
        self.assertTrue(any('content changed' in e for e in cs.verify_split(cat, tampered, transfers)))
        dropped = copy.deepcopy(plan)
        dropped['research']['records'].pop()
        self.assertTrue(any('ID sets differ' in e for e in cs.verify_split(cat, dropped, transfers)))
        with self.assertRaises(cs.CatalogError):
            cs.plan_split(cat, {'R999': 'third-project'})

    def test_mixed_and_conflicting_layouts_are_refused(self):
        path = cs.federated_path(self.hub, 'third-project', 'records')
        path.parent.mkdir(parents=True)
        path.write_text('[]\n')
        with self.assertRaises(cs.CatalogError):
            cs.load_catalog(self.hub, CFG)
        path.unlink()
        with self.assertRaises(cs.CatalogError):
            cs.load_catalog(self.hub, dict(CFG, catalog_layout='federated'))
        with self.assertRaises(cs.CatalogError):
            cs.load_catalog(self.hub, dict(CFG, catalog_layout='sharded'))

    def test_federated_rejects_duplicates_and_misfiled_entries(self):
        cat = cs.load_catalog(self.hub, CFG)
        plan = cs.plan_split(cat)
        plan['third-project']['records'].append(record('R031', project='third-project'))
        self.materialize(plan)
        with self.assertRaisesRegex(cs.CatalogError, 'Duplicate ID R031'):
            cs.load_catalog(self.hub, CFG)
        plan = cs.plan_split(cs.Catalog(cfg=CFG, layout='legacy', records=self.records,
                                        sources=self.sources,
                                        location={e['id']: ('x', 'y') for e in self.records + self.sources}))
        plan['third-project']['records'].append(record('C-R001'))  # no project field -> VB
        for kind in cs.KINDS:
            for proj in plan:
                cs.federated_path(self.hub, proj, kind).write_text(cs.serialize(plan[proj][kind]))
        with self.assertRaisesRegex(cs.CatalogError, 'C-R001: belongs to .research.'):
            cs.load_catalog(self.hub, CFG)

    def test_unknown_project_is_reported(self):
        self.write_legacy(self.records + [record('X-R1', project='nowhere')], self.sources)
        with self.assertRaisesRegex(cs.CatalogError, 'unknown project'):
            cs.load_catalog(self.hub, CFG)

    def test_closure_keeps_cross_project_corrections_and_prerequisites(self):
        extra = [record('C-R001', project='third-project', depends_on=['R031'],
                        source_refs=['B-L001']),
                 record('C900', kind='correction', supersedes=['C-R001']),
                 record('C-R002', project='third-project', depends_on=['C-R404'])]
        self.write_legacy(self.records + extra, self.sources)
        cat = cs.load_catalog(self.hub, CFG)
        ids, foreign, unresolved = cs.closure(cat, 'third-project')
        self.assertTrue({'C-R001', 'R031', 'B-L001', 'C900'} <= ids)
        self.assertEqual(foreign, {'R031', 'B-L001', 'C900'})
        self.assertEqual(unresolved, ['C-R404'])
        cross = cs.cross_project_references(cat)
        self.assertIn(('C-R001', 'depends_on', 'R031'), cross)
        self.assertIn(('C900', 'supersedes', 'C-R001'), cross)
        self.assertNotIn(('O012', 'depends_on', 'R031'), cross)

    def test_atomic_write_is_optimistic_and_leaves_no_temporary_files(self):
        path = self.hub / 'catalog/records.json'
        before = cs.sha256_file(path)
        with self.assertRaisesRegex(cs.CatalogError, 'changed since read'):
            cs.atomic_write_text(path, '[]\n', 'stale-hash')
        self.assertEqual(cs.sha256_file(path), before)
        new_hash = cs.atomic_write_text(path, '[]\n', before)
        self.assertEqual(path.read_text(), '[]\n')
        self.assertEqual(new_hash, cs.sha256_file(path))
        fresh = self.hub / 'projects/third-project/catalog/sources.json'
        cs.atomic_write_text(fresh, '[]\n', None)
        with self.assertRaises(cs.CatalogError):
            cs.atomic_write_text(fresh, '[]\n', None)
        leftovers = [p for p in self.hub.rglob('*.tmp')]
        self.assertEqual(leftovers, [])

    def test_lock_is_exclusive_and_released(self):
        with cs.catalog_lock(self.hub, 'hub', 'claude') as path:
            self.assertTrue(path.exists())
            for scope in ['hub', 'third-project', 'research']:
                with self.assertRaisesRegex(cs.CatalogError, 'busy'):
                    with cs.catalog_lock(self.hub, scope, 'codex'):
                        pass
        # Permanent inode; kernel ownership is released, not the file deleted.
        with cs.catalog_lock(self.hub, 'hub', 'codex'):
            pass
        for bad in ['', '../x', '.hidden']:
            with self.assertRaises(cs.CatalogError):
                with cs.catalog_lock(self.hub, bad, 'x'):
                    pass

    def test_dry_run_reports_pass_and_writes_nothing(self):
        before = sorted(p.relative_to(self.hub).as_posix() for p in self.hub.rglob('*') if '.hub' not in p.relative_to(self.hub).parts)
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = cs.dry_run(self.hub, {'R031': 'third-project', 'O012': 'third-project'})
        self.assertEqual(status, 0)
        self.assertIn('Split verification: PASS', stream.getvalue())
        # Closures must reflect the transfer: KC owns R031 and O012 after the move.
        self.assertIn('third-project: 2 records, 0 sources', stream.getvalue())
        self.assertRegex(stream.getvalue(), r'third-project: .*scoped closure 2 IDs \(0 foreign\)')
        after = sorted(p.relative_to(self.hub).as_posix() for p in self.hub.rglob('*') if '.hub' not in p.relative_to(self.hub).parts)
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
