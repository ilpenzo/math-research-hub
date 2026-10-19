"""The paper checker must block markers, hub IDs and broken references, and stay read-only."""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / '.agents/skills/math-paper-writing/scripts/check_paper.py'
spec = importlib.util.spec_from_file_location('check_paper', SCRIPT)
check_paper = importlib.util.module_from_spec(spec)
sys.dont_write_bytecode = True  # keep the skill folder free of __pycache__
spec.loader.exec_module(check_paper)

ABSTRACT = ' '.join(['word'] * 90)
CLEAN = r"""\documentclass{amsart}
\newtheorem{theorem}{Theorem}[section]
\newtheorem{lemma}[theorem]{Lemma}
\begin{document}
\title{Exceptional collections on toric surfaces}
\begin{abstract}
%s
\end{abstract}
\maketitle
\section{Introduction}\label{sec:intro}
\begin{theorem}\label{thm:main}
Every surface has a property.
\end{theorem}
By \cite[Theorem~1.1]{Kaw06} and Theorem~\ref{thm:main}, the claim holds.
\begin{thebibliography}{Kaw06}
\bibitem[Kaw06]{Kaw06} Y.~Kawamata, \emph{Derived categories of toric varieties}.
\end{thebibliography}
\end{document}
""" % ABSTRACT
LOG = 'This is pdfTeX\nOutput written on paper.pdf (3 pages).\n'


class CheckPaperTests(unittest.TestCase):
    def run_check(self, tex, log=LOG, **options):
        with tempfile.TemporaryDirectory() as tmp:
            paper = Path(tmp) / 'paper.tex'
            paper.write_text(tex)
            if log is not None:
                (Path(tmp) / 'paper.log').write_text(log)
            before = sorted(p.name for p in Path(tmp).iterdir())
            report = check_paper.check(paper, **options)
            self.assertEqual(before, sorted(p.name for p in Path(tmp).iterdir()))
            return report

    def test_clean_paper_passes(self):
        report = self.run_check(CLEAN)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.warnings, [])

    def test_markers_and_hub_ids_are_errors(self):
        tex = CLEAN.replace('Every surface', '[VERIFY: locator] Every surface, as in ZZ-R038,')
        errors = '\n'.join(self.run_check(tex).errors)
        self.assertIn('[VERIFY', errors)
        self.assertIn('ZZ-R038', errors)

    def test_comment_markers_block_release_only(self):
        tex = CLEAN.replace('\\maketitle', '\\maketitle % TODO cite R031')
        self.assertEqual(self.run_check(tex).errors, [])
        self.assertEqual(len(self.run_check(tex, release=True).errors), 2)

    def test_missing_bibliography_key_and_label(self):
        tex = CLEAN.replace('{Kaw06} and', '{Kaw05} and').replace('{thm:main}, the', '{thm:other}, the')
        errors = '\n'.join(self.run_check(tex).errors)
        self.assertIn("'Kaw05'", errors)
        self.assertIn("'thm:other'", errors)

    def test_log_undefined_reference(self):
        log = LOG + "LaTeX Warning: Reference `thm:x' on page 2 undefined on input line 9.\n"
        self.assertTrue(any('thm:x' in e for e in self.run_check(CLEAN, log=log).errors))

    def test_release_requires_log_and_short_abstract(self):
        long = CLEAN.replace(ABSTRACT, ' '.join(['abstraction'] * 190))
        self.assertEqual(self.run_check(long, log=None).errors, [])
        errors = '\n'.join(self.run_check(long, log=None, release=True).errors)
        self.assertIn('no build log', errors)
        self.assertIn('1920', errors)

    def test_style_warnings(self):
        tex = CLEAN.replace('the claim holds.', 'clearly the claim holds iff $f: X \\to Y$ exists.')
        warnings = '\n'.join(self.run_check(tex).warnings)
        for fragment in ['clearly', 'iff', 'colon']:
            self.assertIn(fragment, warnings)


if __name__ == '__main__':
    unittest.main()
