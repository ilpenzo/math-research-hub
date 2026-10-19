"""Coverage must detect omissions, overlaps and unreviewed membership changes."""
import unittest
from coverage_map import matches, membership_hash, reconcile


class CoverageTests(unittest.TestCase):
    def fixture(self):
        entries = [dict(root='vb', path='branch/report.md')]
        unit = dict(id='P1', root='vb', path='branch', selector='subtree',
                    topics=['topic'], state='mapped', disposition='claims-pending',
                    file_count=1, membership_sha256=membership_hash(entries))
        return dict(topics={'topic': {}}, units=[unit]), dict(entries=entries), []

    def test_partition(self):
        self.assertEqual(reconcile(*self.fixture())[0], [])

    def test_overlap_and_omission(self):
        data, inv, records = self.fixture()
        data['units'].append(dict(data['units'][0], id='P2'))
        self.assertTrue(any('2 coverage assignments' in x for x in reconcile(data, inv, records)[0]))
        data['units'] = []
        self.assertTrue(any('0 coverage assignments' in x for x in reconcile(data, inv, records)[0]))

    def test_membership_change(self):
        data, inv, records = self.fixture()
        inv['entries'].append(dict(root='vb', path='branch/new.md'))
        self.assertTrue(any('membership changed' in x for x in reconcile(data, inv, records)[0]))

    def test_completion_requires_every_file_disposition(self):
        data, inv, records = self.fixture()
        unit = data['units'][0]
        unit.update(state='claims-extracted', completion_basis='All assertions accounted for.')
        self.assertTrue(any('every member' in x for x in reconcile(data, inv, records)[0]))
        unit['file_dispositions'] = [dict(path='branch/report.md',
            disposition='extracted', basis='Report fully read; no distinct claim.')]
        self.assertEqual(reconcile(data, inv, records)[0], [])

    def test_disposition_references_and_membership(self):
        data, inv, records = self.fixture()
        entry = dict(path='branch/absent.md', disposition='extracted',
                     basis='Read', record_ids=['R404'])
        data['units'][0]['file_dispositions'] = [entry, entry]
        errors = reconcile(data, inv, records)[0]
        self.assertTrue(any('duplicate file disposition' in x for x in errors))
        self.assertTrue(any('invalid file disposition' in x for x in errors))
        self.assertTrue(any('unknown file-disposition record' in x for x in errors))

    def test_directory_does_not_swallow_subpackage(self):
        unit = dict(root='vb', path='branch', selector='directory')
        self.assertTrue(matches(unit, dict(root='vb', path='branch/report.md')))
        self.assertFalse(matches(unit, dict(root='vb', path='branch/sub/report.md')))
        self.assertFalse(matches(unit, dict(root='toric', path='branch/report.md')))


if __name__ == '__main__':
    unittest.main()
