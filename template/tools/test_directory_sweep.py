"""Regression controls for evidence lost by format filters and stale links."""
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path
import zipfile
from directory_sweep import role, checkpoint_info, zip_members, screen


class SweepTests(unittest.TestCase):
    def test_pdf_marker_has_page_locator(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'result.pdf'; p.write_bytes(b'fixture')
            with patch('directory_sweep.shutil.which', return_value='pdftotext'), patch(
                    'directory_sweep.subprocess.run', return_value=SimpleNamespace(stdout=b'Title\fTheorem on second page\f')):
                result = screen(p, 'vb', 'result.pdf', [])
            self.assertEqual(result['candidate_markers'][0]['page'], 2)
            self.assertEqual(result['mathematical_review'], 'pending')

    def test_word_paragraphs_are_screened_without_rendering_or_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'plan.docx'
            xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Theorem</w:t></w:r></w:p></w:body></w:document>'
            with zipfile.ZipFile(p, 'w') as z:
                z.writestr('word/document.xml', xml)
            result = screen(p, 'vb', 'plan.docx', [])
            self.assertEqual(result['candidate_markers'][0]['line'], 1)
            self.assertEqual(result['screen'], 'word-paragraph-markers')

    def test_unknown_evidence_is_not_silently_excluded(self):
        self.assertEqual(role('sources/theorem.html', False), 'unindexed-needs-disposition')
        self.assertEqual(role('code/witness.inc', False), 'unindexed-needs-disposition')
        self.assertEqual(role('.venv/lib/a.py', False), 'dependency-or-runtime-metadata')

    def test_distinct_checkpoint_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / '.ipynb_checkpoints').mkdir()
            p = root / '.ipynb_checkpoints/result-checkpoint.md'; p.write_text('old')
            self.assertFalse(checkpoint_info(p)['original_exists'])
            (root / 'result.md').write_text('new')
            self.assertFalse(checkpoint_info(p)['exact_duplicate'])

    def test_zip_contents_compared_without_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'old.md').write_text('same')
            zpath = root / 'archive.zip'
            with zipfile.ZipFile(zpath, 'w') as z:
                z.writestr('old.md', 'same'); z.writestr('../escape.md', 'distinct')
            items = zip_members(zpath)
            self.assertEqual(items[0]['disposition'], 'exact-duplicate-of-loose-file')
            self.assertEqual(items[1]['disposition'], 'distinct-member-review-pending')
            self.assertIsNone(items[1]['loose_path'])

    def test_line_locators_and_code_are_not_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'proof.md').write_text('proof')
            p = root / 'report.md'
            p.write_text('# Theorem\n[x](proof.md:12)\n```\n[x](fixture.md)\n```\n[x](missing.md)\n')
            result = screen(p, 'vb', 'report.md', [])
            self.assertEqual(len(result['missing_local_links']), 1)
            self.assertEqual(result['missing_local_links'][0]['target'], 'missing.md')
            self.assertTrue(result['candidate_markers'])


if __name__ == '__main__':
    unittest.main()
