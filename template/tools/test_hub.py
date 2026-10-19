"""Regression checks for retrieval and provenance failures; no mathematical tests."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('hub', Path(__file__).with_name('hub.py'))
hub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hub)


class CatalogIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.roots = {k: self.base / k for k in ['vb', 'toric', 'hub']}
        for path in self.roots.values():
            path.mkdir()
        self.hub = self.roots['hub']
        (self.hub / 'catalog').mkdir()
        for key in ['vb', 'toric']:
            (self.roots[key] / 'proof.md').write_text('needle one\nneedle two\n')
        self.evidence = dict(root='vb', path='proof.md', locator='line 1',
                             sha256=hub.digest(self.roots['vb'] / 'proof.md'))
        self.result = dict(id='R1', kind='result', title='needle result', topics=['test'],
                           status='reported', evidence_level='imported', novelty='unassessed',
                           statement='Fixture only', hypotheses=['fixture'], limitations='fixture',
                           evidence=[self.evidence], depends_on=[], supersedes=[], subsumed_by=[],
                           source_refs=['L1'])
        self.correction = dict(copy.deepcopy(self.result), id='C1', kind='correction',
                               title='Correction', supersedes=['R1'], source_refs=[])
        self.source = dict(id='L1', citation='Fixture', url='https://example.com', version='1',
                           topics=['test'], locator='1', use='fixture', limitations='fixture',
                           check_state='lead', evidence=[self.evidence])
        self.save([self.result, self.correction], [self.source])
        self.cfg = dict(schema_version=1, catalog_layout='legacy',
            roots={k: str(v) for k, v in self.roots.items() if k != 'hub'},
            projects={'research': {'title': 'VB', 'roots': ['vb', 'toric']},
                      'second-project': {'title': 'CP', 'roots': ['cp']}},
            exclude_dirs=[], extensions=['.md'], text_extensions=['.md'])
        (self.hub / 'hub.json').write_text(json.dumps(self.cfg))
        for name, value in [('HUB', self.hub), ('ROOTS', self.roots), ('CFG', self.cfg)]:
            p = patch.object(hub, name, value)
            p.start()
            self.addCleanup(p.stop)

    def save(self, records, sources):
        (self.hub / 'catalog/records.json').write_text(json.dumps(records))
        (self.hub / 'catalog/sources.json').write_text(json.dumps(sources))

    def capture(self, fn, *args):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            result = fn(*args)
        return result, stream.getvalue()

    def test_search_limit_cannot_hide_second_archive_or_correction(self):
        _, output = self.capture(hub.search, 'needle', True, 1)
        self.assertIn('CORRECTION C1', output)
        self.assertIn('vb:proof.md:1:', output)
        self.assertIn('toric:proof.md:1:', output)
        self.assertIn('Searched vb', output)
        self.assertIn('Searched toric', output)
        self.assertIn('TRUNCATED', output)

    def test_changed_evidence_is_visible_and_fails_validation(self):
        self.assertIn('CURRENT', hub.evidence_state(self.result))
        (self.roots['vb'] / 'proof.md').write_text('Changed statement\n')
        self.assertIn('STALE', hub.format_record(self.result))
        failed, output = self.capture(hub.check)
        self.assertTrue(failed)
        self.assertIn('STALE evidence', output)

    def test_project_search_separates_records_and_archive_roots(self):
        cp = self.base / 'cp'
        cp.mkdir()
        (cp / 'proof.md').write_text('needle cp\n')
        self.roots['cp'] = cp
        self.cfg['roots']['cp'] = str(cp)
        (self.hub / 'hub.json').write_text(json.dumps(self.cfg))
        cp_record = dict(self.result, id='B-R1', project='second-project',
                         source_refs=[], evidence=[dict(self.evidence, root='cp',
                         sha256=hub.digest(cp / 'proof.md'))])
        self.save([self.result, self.correction, cp_record], [self.source])
        _, output = self.capture(hub.search, 'needle', True, 10, None, 'second-project')
        self.assertIn('CATALOG B-R1', output)
        self.assertIn('cp:proof.md', output)
        self.assertNotIn('CATALOG R1', output)
        self.assertNotIn('vb:proof.md', output)
        self.assertNotIn('toric:proof.md', output)
        _, legacy = self.capture(hub.search, 'needle', True, 10, None, 'research')
        self.assertIn('CATALOG R1', legacy)
        self.assertNotIn('CATALOG B-R1', legacy)
        self.assertNotIn('cp:proof.md', legacy)

    def test_scoped_inventory_preserves_other_roots_snapshot(self):
        self.capture(hub.inventory)
        before = hub.read_json('indexes/inventory.json')
        (self.roots['toric'] / 'proof.md').write_text('changed outside requested root\n')
        (self.roots['vb'] / 'new.md').write_text('new evidence\n')
        self.capture(hub.inventory, 'vb')
        after = hub.read_json('indexes/inventory.json')
        self.assertEqual([e for e in before['entries'] if e['root'] == 'toric'],
                         [e for e in after['entries'] if e['root'] == 'toric'])
        self.assertEqual(before['root_snapshots']['toric'], after['root_snapshots']['toric'])
        self.assertIn('new.md', [e['path'] for e in after['entries'] if e['root'] == 'vb'])

    def test_project_render_keeps_generated_catalogs_separate(self):
        cp_record = dict(self.result, id='B-R1', project='second-project')
        self.save([self.result, cp_record], [self.source])
        self.capture(hub.render)
        cp = (self.hub / 'projects/second-project/indexes/CATALOG.md').read_text()
        legacy = (self.hub / 'projects/research/indexes/CATALOG.md').read_text()
        self.assertIn('## B-R1', cp)
        self.assertNotIn('## R1', cp)
        self.assertIn('## R1', legacy)
        self.assertNotIn('## B-R1', legacy)

    def test_corrections_and_literature_propagate_to_affected_claim(self):
        _, correction_impact = self.capture(hub.impact, 'C1')
        self.assertIn('R1 — superseded by correction C1', correction_impact)
        _, source_impact = self.capture(hub.impact, 'L1')
        self.assertIn('R1 — source_refs L1', source_impact)
        self.assertIn('C1 — Correction', hub.format_record(self.result))

    def test_truthy_review_is_not_a_valid_review(self):
        bad = dict(self.result, status='verified', evidence_level='internally-reviewed', review='yes')
        self.save([bad], [self.source])
        failed, output = self.capture(hub.check)
        self.assertTrue(failed)
        self.assertIn('review needs date, scope and hashed artifact', output)

    def test_missing_source_and_dependency_cycle_are_rejected(self):
        first = dict(self.result, depends_on=['C1'], source_refs=['MISSING'])
        second = dict(self.correction, depends_on=['R1'])
        self.save([first, second], [self.source])
        failed, output = self.capture(hub.check)
        self.assertTrue(failed)
        self.assertIn('invalid source reference MISSING', output)
        self.assertIn('depends_on cycle', output)

    def test_inventory_preserves_archives_and_reports_both_roots(self):
        before = {k: (p / 'proof.md').read_bytes() for k, p in self.roots.items() if k != 'hub'}
        failed, _ = self.capture(hub.inventory)
        self.assertFalse(failed)
        after = {k: (p / 'proof.md').read_bytes() for k, p in self.roots.items() if k != 'hub'}
        self.assertEqual(before, after)
        items = json.loads((self.hub / 'indexes/inventory.json').read_text())['entries']
        self.assertEqual({'vb', 'toric'}, {x['root'] for x in items})


if __name__ == '__main__':
    unittest.main()
