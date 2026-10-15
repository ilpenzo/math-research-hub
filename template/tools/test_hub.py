#!/usr/bin/env python3
"""Tests for hub.py. Run: python3 tools/test_hub.py  (tooling only, not mathematics)."""
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent
HUB_PY = TEMPLATE / "tools" / "hub.py"


class HubTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.hub = self.tmp / "hub"
        shutil.copytree(TEMPLATE, self.hub, symlinks=True,
                        ignore=shutil.ignore_patterns("projects", ".hub", "__pycache__", ".git",
                                                      "library", "archives", "indexes"))
        (self.hub / "hub.json").write_text(json.dumps({"schema_version": 1, "owner": "Tester",
                                                       "archives": {}, "projects": {}}))
        self.archive = self.tmp / "old-notes"
        self.archive.mkdir()
        (self.archive / "draft.tex").write_text("Lemma. The Frobenius twist is ample.\n")
        self.run_hub("add-project", "test-proj", "--title", "Test project", "--prefix", "TP")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_hub(self, *args, ok=True):
        out = subprocess.run([sys.executable, str(self.hub / "tools" / "hub.py"), *args],
                             capture_output=True, text=True, cwd=self.hub)
        if ok and out.returncode != 0:
            self.fail(f"hub.py {' '.join(args)} failed:\n{out.stdout}\n{out.stderr}")
        if not ok and out.returncode == 0:
            self.fail(f"hub.py {' '.join(args)} should have failed:\n{out.stdout}")
        return out.stdout + out.stderr

    def write(self, name, data):
        path = self.tmp / name
        path.write_text(json.dumps(data))
        return str(path)

    def note(self):
        rel = self.run_hub("new-note", "first question", "--project", "test-proj").strip()
        return rel

    def record(self, note, **extra):
        rec = {"id": "auto", "project": "test-proj", "kind": "result", "title": "A lemma",
               "topics": ["t"], "status": "reported", "evidence_level": "draft-proof",
               "novelty": "unassessed", "statement": "Every widget is ample.",
               "hypotheses": ["char 0"], "limitations": "Unreviewed.",
               "evidence": [{"root": "hub", "path": note, "locator": "Work", "sha256": "auto"}],
               "depends_on": [], "supersedes": [], "subsumed_by": [], "source_refs": []}
        rec.update(extra)
        return rec

    def test_full_cycle(self):
        note = self.note()
        out = self.run_hub("put", self.write("r1.json", self.record(note)))
        self.assertIn("Saved TP-R001", out)
        self.assertIn("0 errors", self.run_hub("check", "--project", "test-proj"))
        self.assertIn("TP-R001", self.run_hub("search", "widget ample", "--project", "test-proj"))
        self.run_hub("render")
        catalog = (self.hub / "projects/test-proj/indexes/CATALOG.md").read_text()
        self.assertIn("TP-R001", catalog)

    def test_update_requires_current_hash(self):
        note = self.note()
        self.run_hub("put", self.write("r1.json", self.record(note)))
        changed = self.record(note, id="TP-R001", title="A sharper lemma")
        out = self.run_hub("put", self.write("r2.json", changed), ok=False)
        self.assertIn("--expect TP-R001=", out)
        shown = self.run_hub("show", "TP-R001")
        digest = shown.split("hash: ")[1].split()[0]
        self.run_hub("put", self.write("r2.json", changed), "--expect", f"TP-R001={digest}")
        self.assertIn("A sharper lemma", self.run_hub("show", "TP-R001"))

    def test_rejections(self):
        note = self.note()
        bad = self.record(note, status="verified")
        self.assertIn("review object", self.run_hub("put", self.write("b.json", bad), ok=False))
        bad = self.record(note, kind="obstruction")
        self.assertIn("reopening_condition", self.run_hub("put", self.write("b.json", bad), ok=False))
        bad = self.record(note, depends_on=["TP-R999"])
        self.assertIn("unknown record", self.run_hub("put", self.write("b.json", bad), ok=False))
        bad = self.record(note, id="TP-R050")
        self.assertIn("not reserved", self.run_hub("put", self.write("b.json", bad), ok=False))
        bad = self.record(note, title="Short precise title")
        self.assertIn("placeholder", self.run_hub("put", self.write("b.json", bad), ok=False))
        self.assertEqual(json.loads((self.hub / "projects/test-proj/catalog/records.json").read_text()), [])

    def test_cycle_and_stale_evidence(self):
        note = self.note()
        self.run_hub("put", self.write("a.json", self.record(note)))
        self.run_hub("put", self.write("b.json", self.record(note, depends_on=["TP-R001"])))
        digest = self.run_hub("show", "TP-R001").split("hash: ")[1].split()[0]
        loop = self.record(note, id="TP-R001", depends_on=["TP-R002"])
        self.assertIn("cycle", self.run_hub("put", self.write("c.json", loop), "--expect",
                                            f"TP-R001={digest}", ok=False))
        (self.hub / note).write_text("changed\n")
        self.assertIn("STALE", self.run_hub("check", ok=False))
        self.assertIn("TP-R002", self.run_hub("impact", "TP-R001"))

    def test_correction_is_visible(self):
        note = self.note()
        self.run_hub("put", self.write("a.json", self.record(note)))
        fix = self.record(note, kind="correction", title="Correction to the lemma", supersedes=["TP-R001"],
                          statement="The lemma needs char 0.")
        self.run_hub("put", self.write("f.json", fix))
        self.assertIn("corrected or superseded by: TP-R002", self.run_hub("show", "TP-R001"))
        self.assertIn("corrected by TP-R002", self.run_hub("search", "widget", "--project", "test-proj"))

    def test_archive_search_and_reserve(self):
        self.run_hub("add-archive", str(self.archive), "--project", "test-proj", "--alias", "old")
        out = self.run_hub("search", "frobenius twist", "--project", "test-proj", "--archives")
        self.assertIn("old:draft.tex", out)
        self.assertIn("does not establish novelty", out)
        settings = json.loads((self.hub / ".claude/settings.json").read_text())
        self.assertIn(str(self.archive.resolve()), settings["permissions"]["additionalDirectories"])
        self.assertEqual(self.run_hub("reserve", "--project", "test-proj").strip(), "TP-R001")
        self.assertEqual(self.run_hub("reserve", "--project", "test-proj", "--kind", "L").strip(), "TP-L001")

    def test_package_and_roles(self):
        run = self.tmp / "run"
        run.mkdir()
        for name in ("code.py", "in.txt", "out.txt"):
            (run / name).write_text(name)
        spec = {"command": "python3 code.py", "environment": "Python", "domain": "n<5",
                "exhaustiveness": "exhaustive", "files": [
                    {"from": str(run / "code.py"), "role": "source"},
                    {"from": str(run / "in.txt"), "role": "input"},
                    {"from": str(run / "out.txt"), "role": "output"}]}
        dest = "projects/test-proj/investigations/run1"
        self.run_hub("package", self.write("spec.json", spec), "--project", "test-proj", "--dest", dest)
        self.assertTrue((self.hub / dest / "manifest.json").exists())
        self.run_hub("package", self.write("spec.json", spec), "--project", "test-proj", "--dest", dest, ok=False)
        self.run_hub("sync-roles")
        self.assertNotIn("out of date", self.run_hub("check"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
