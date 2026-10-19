"""Source-retention, retrieval and virtual-reading controls in disposable hubs."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import catalog_store as cs
import catalog_validation as cv
import hub
import library
import test_federation as fixtures


class LibraryTests(unittest.TestCase):
    setUp = fixtures.FederationTests.setUp
    write = fixtures.FederationTests.write
    capture = fixtures.FederationTests.capture

    def spec(self, **changes):
        return dict(project='alpha', kind='textbook', citation='Fixture geometry textbook',
                    url='https://example.org/book', version='edition 1', topics=['geometry'],
                    aliases=['OSS'], locator='title page only', use='reference lookup',
                    limitations='No theorem application checked', **changes)

    def retain(self, spec=None):
        path = self.root / 'input.pdf'
        path.write_bytes(b'%PDF-1.4\nfixture source bytes\n')
        return library.capture(self.root, spec or self.spec(), local_file=path, owner='test')

    def test_capture_is_atomic_hashed_and_does_not_promote(self):
        result = self.retain()
        cat = cs.load_catalog(self.root)
        source = cat.by_id()[result['id']]
        self.assertEqual(source['check_state'], 'lead')
        self.assertEqual(cv.validate(cat, self.root), [])
        c = source['library']['copies'][0]
        self.assertEqual((self.root / c['path']).read_bytes(), (self.root / 'input.pdf').read_bytes())
        self.assertEqual(c['sha256'], cs.sha256_file(self.root / c['path']))
        (self.root / c['path']).write_bytes(b'changed')
        self.assertTrue(any('STALE' in e for e in cv.validate(cat, self.root)))

    def test_repeat_capture_is_idempotent_and_preserves_existing_scope(self):
        key = self.retain()['id']
        before = cs.load_catalog(self.root).by_id()[key]
        spec = self.spec(id=key)
        spec['use'] = 'must not replace the checked use'
        self.retain(spec)
        after = cs.load_catalog(self.root).by_id()[key]
        self.assertEqual(before, after)
        with self.assertRaisesRegex(ValueError, 'Existing source'):
            self.retain()

    def test_false_pdf_is_rejected_before_allocating(self):
        path = self.root / 'bad.pdf'; path.write_bytes(b'<html>login</html>')
        with self.assertRaisesRegex(ValueError, 'not a PDF'):
            library.capture(self.root, self.spec(), local_file=path)
        self.assertEqual(cs.load_catalog(self.root).sources, [])
        self.assertFalse((self.root / 'projects/alpha/catalog/reservations.json').exists())

    def test_existing_archive_copy_is_not_moved_or_duplicated(self):
        path = self.root / 'archive.pdf'; path.write_bytes(b'%PDF-1.4\nexisting')
        result = library.capture(self.root, self.spec(), existing='hub:archive.pdf')
        self.assertEqual(result['copies'][0]['path'], 'archive.pdf')
        self.assertEqual(list((self.root / 'projects/alpha/library').rglob('*.pdf')), [])
        self.assertEqual(path.read_bytes(), b'%PDF-1.4\nexisting')

    def test_unavailable_material_is_recorded_without_a_full_text_claim(self):
        result = library.capture(self.root, self.spec(availability_note='Publisher access unavailable'))
        source = cs.load_catalog(self.root).by_id()[result['id']]
        self.assertEqual(source['library']['copies'], [])
        self.assertNotIn('full text registered', library.availability(source))
        self.assertEqual(cv.validate(cs.load_catalog(self.root), self.root), [])

    def test_alias_retrieval_and_scoped_library(self):
        key = self.retain()['id']
        _, out = self.capture(hub.library, 'OSS', 'alpha')
        self.assertIn(key, out)
        self.assertNotIn(key, self.capture(hub.library, 'OSS', 'beta')[1])
        unrelated = dict(cs.load_catalog(self.root).by_id()[key], library={}, citation='Loss of sections', use='loss')
        self.assertEqual(library.select([unrelated], 'OSS'), [])

    def test_virtual_membership_is_not_a_proof_dependency_and_detects_change(self):
        key = self.retain()['id']
        source = cs.load_catalog(self.root).by_id()[key]
        record = dict(self.record, status='verified', evidence_level='internally-reviewed',
                      review=dict(date='2026-10-19', scope='fixture', artifact=self.evidence))
        self.write('alpha', 'records', [record])
        source['library'] = dict(kind='virtual', aliases=['reading'], copies=[],
             availability_note='Generated from current records',
             records=[dict(id=record['id'], sha256=cs.sha256_text(cs.canonical(record)))])
        self.write('alpha', 'sources', [source])
        self.assertEqual(cs.references(source), [])
        self.assertEqual(cv.validate(cs.load_catalog(self.root), self.root), [])
        _, out = self.capture(hub.reading, key)
        self.assertIn('unchanged verified record', out)
        self.assertIn('Hypotheses/scope', out)
        changed = dict(record, limitations='New limitation')
        correction = dict(copy.deepcopy(self.record), id='B-C001', project='beta',
                          kind='correction', title='Important correction', supersedes=[record['id']])
        self.write('alpha', 'records', [changed]); self.write('beta', 'records', [correction])
        _, out = self.capture(hub.reading, key)
        self.assertIn('STALE selection', out)
        self.assertIn('B-C001', out)
        # New evidence may invalidate a reading selection without blocking publication.
        self.assertEqual(cv.validate(cs.load_catalog(self.root), self.root), [])
        changed['status'] = 'refuted'; self.write('alpha', 'records', [changed])
        self.assertIn('REVIEW NEEDED', self.capture(hub.reading, key)[1])

    def test_library_structure_and_members_are_validated(self):
        key = self.retain()['id']
        source = cs.load_catalog(self.root).by_id()[key]
        source['library']['copies'][0]['path'] = 'unregistered.pdf'
        source['library']['records'] = [dict(id='MISSING', sha256='bad')]
        self.write('alpha', 'sources', [source])
        errors = '\n'.join(cv.validate(cs.load_catalog(self.root), self.root))
        self.assertIn('registered as hashed evidence', errors)
        self.assertIn('existing mathematical record', errors)
        self.assertIn('only virtual', errors)

    def test_malformed_capture_is_rejected_before_reservation(self):
        for changes in [dict(citation=123), dict(topics='geometry'), dict(aliases=[None]),
                        dict(kind=[]), dict(accessed=7)]:
            spec = self.spec(); spec.update(changes, availability_note='Missing')
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                library.capture(self.root, spec)
        self.assertEqual(cs.load_catalog(self.root).sources, [])
        self.assertFalse((self.root / 'projects/alpha/catalog/reservations.json').exists())

    def test_null_library_and_evidence_return_validation_errors(self):
        key = self.retain()['id']
        source = cs.load_catalog(self.root).by_id()[key]
        for change, message in [(dict(library=None), 'library must be an object'),
                                (dict(evidence=None), 'registered as hashed evidence'),
                                (dict(citation=123), 'invalid source field citation')]:
            self.write('alpha', 'sources', [dict(source, **change)])
            errors = '\n'.join(cv.validate(cs.load_catalog(self.root), self.root))
            self.assertIn(message, errors)

    def test_repeat_capture_never_repairs_retained_bytes_silently(self):
        key = self.retain()['id']
        source = cs.load_catalog(self.root).by_id()[key]
        path = self.root / source['library']['copies'][0]['path']
        path.write_bytes(b'changed evidence')
        with self.assertRaisesRegex(ValueError, 'Retained bytes differ'):
            self.retain(self.spec(id=key))
        self.assertEqual(path.read_bytes(), b'changed evidence')
        self.assertEqual(source, cs.load_catalog(self.root).by_id()[key])

    def test_optional_copies_and_specific_limit_survive_capture(self):
        key = self.retain(self.spec(availability_note='Missing appendix/plates'))['id']
        source = cs.load_catalog(self.root).by_id()[key]
        del source['library']['copies']
        self.write('alpha', 'sources', [source])
        self.retain(self.spec(id=key))
        source = cs.load_catalog(self.root).by_id()[key]
        self.assertEqual(source['library']['availability_note'], 'Missing appendix/plates')
        self.assertEqual(len(source['library']['copies']), 1)
        self.assertIn('Missing appendix/plates', library.render_catalog([source], 'stamp'))

    def test_other_edition_does_not_clear_exact_version_gap(self):
        key = library.capture(self.root, self.spec(availability_note='Missing first edition'))['id']
        spec = self.spec(id=key); spec['version'] = 'edition 2'
        self.retain(spec)
        source = cs.load_catalog(self.root).by_id()[key]
        self.assertEqual(source['version'], 'edition 1')
        self.assertTrue(library.needs_retention_review(source))
        self.assertIn(key, library.render_gaps([source], 'stamp'))
        self.retain(self.spec(id=key))
        self.assertFalse(library.needs_retention_review(cs.load_catalog(self.root).by_id()[key]))

    def test_obsolete_generated_reading_is_retired(self):
        key = self.retain()['id']
        source = cs.load_catalog(self.root).by_id()[key]
        source['library'] = dict(kind='virtual', aliases=[], copies=[],
             availability_note='Fixture reading',
             records=[dict(id=self.record['id'], sha256=cs.sha256_text(cs.canonical(self.record)))])
        self.write('alpha', 'sources', [source]); self.capture(hub.render)
        path = self.root / f'projects/alpha/indexes/reading/{key}.md'
        self.assertIn(self.record['statement'], path.read_text())
        source['library'].update(kind='paper', records=[])
        self.write('alpha', 'sources', [source]); self.capture(hub.render)
        self.assertIn('Retired generated reading view', path.read_text())
        self.assertNotIn('Hypotheses/scope', path.read_text())
        self.assertFalse(self.capture(hub.view_status, 'alpha')[0])

    def test_acquisition_list_distinguishes_missing_and_other_versions(self):
        key = self.retain()['id']
        source = cs.load_catalog(self.root).by_id()[key]
        self.assertIsNone(library.retention_issue(source))
        source['library']['copies'][0]['version'] = 'edition 2'
        self.assertEqual(library.retention_issue(source), 'Full text retained; reconcile registered version')
        source['library']['copies'] = []
        source['evidence'] = [self.evidence]
        self.assertEqual(library.retention_issue(source), 'No primary copy indexed; locate or acquire')
        source['library']['kind'] = 'reference'
        self.assertIn('Reference or internal source', library.retention_issue(source))
        source['library']['availability_note'] = 'Specific unresolved source issue'
        self.assertIn('Specific unresolved source issue', library.render_gaps([source], 'stamp'))

    def test_download_failure_and_error_page_leave_no_catalog_entry(self):
        from urllib.error import URLError
        with patch('library.urllib.request.urlopen', side_effect=URLError('offline')):
            with self.assertRaises(URLError):
                library.capture(self.root, self.spec(), download='https://example.org/book.pdf')
        class Response:
            url = 'https://example.org/book.pdf'
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b'<html>Sign in</html>'
        with patch('library.urllib.request.urlopen', return_value=Response()):
            with self.assertRaisesRegex(ValueError, 'not a PDF'):
                library.capture(self.root, self.spec(), download=Response.url)
        self.assertEqual(cs.load_catalog(self.root).sources, [])
        self.assertFalse((self.root / 'projects/alpha/catalog/reservations.json').exists())

    def test_download_retains_exact_bytes_and_final_url(self):
        class Response:
            url = 'https://example.org/publisher/book.pdf'
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b'%PDF-1.4\ndownload fixture\n'
        with patch('library.urllib.request.urlopen', return_value=Response()):
            result = library.capture(self.root, self.spec(), download='https://example.org/book.pdf')
        c = result['copies'][0]
        self.assertEqual(c['url'], Response.url)
        self.assertEqual((self.root / c['path']).read_bytes(), Response().read(1))
        self.assertEqual(cv.validate(cs.load_catalog(self.root), self.root), [])


if __name__ == '__main__':
    unittest.main()
