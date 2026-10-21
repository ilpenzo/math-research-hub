"""Tests for core/export.py (PDF worker, sandboxed TeX -> SVG).

Run: python3 harness/tests/test_export.py   (Python 3.9+; Chrome and TeX tests
skip when the binaries are missing).
"""
import http.server
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HARNESS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE = os.path.join(HARNESS, "core", "export.py")


def _load():
    sys.path.insert(0, HARNESS)
    try:
        from core import export
        if os.path.abspath(export.__file__) == MODULE:
            return export
    except ImportError:
        pass
    spec = importlib.util.spec_from_file_location("mathdesk_export", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ex = _load()
HAVE_CHROME = ex.chrome_path() is not None
HAVE_TEX = bool(ex.tex_tool("latex") and ex.tex_tool("dvisvgm")
                and os.path.exists("/usr/bin/sandbox-exec"))
HAVE_SANDBOX = os.path.exists("/usr/bin/sandbox-exec")

# Content arrives by a delayed fetch and READY is set by a timer afterwards, so a
# multi-page PDF proves the worker waited for the title rather than the load event.
PAGE = b"""<!doctype html><html><head><meta charset="utf-8"><title>loading</title>
<style>@page { size: A4; margin: 15mm } body { font: 14px serif }</style></head>
<body><h1>Session</h1><div id="c"></div><script>
fetch('/data').then(r => r.json()).then(j => {
  let s = '';
  for (let i = 0; i < j.n; i++) s += '<h2>Turn ' + i + '</h2><p>delayed paragraph ' + i + '</p>';
  document.getElementById('c').innerHTML = s;
  setTimeout(() => { document.title = 'READY'; }, 300);
});
</script></body></html>"""
NEVER = b"<!doctype html><html><head><title>busy</title></head><body>never ready</body></html>"
FAILED = (b"<!doctype html><html><head><title>busy</title></head><body>broken<script>"
          b"setTimeout(() => { document.title = 'FAILED'; }, 200);</script></body></html>")


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/data":
            time.sleep(0.8)
            body, ctype, code = json.dumps({"n": 120}).encode(), "application/json", 200
        elif self.path == "/never":
            body, ctype, code = NEVER, "text/html", 200
        elif self.path == "/failed":
            body, ctype, code = FAILED, "text/html", 200
        elif self.path == "/":
            body, ctype, code = PAGE, "text/html", 200
        else:
            body, ctype, code = b"not found", "text/plain", 404
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _pages(path):
    with open(path, "rb") as f:
        return len(re.findall(rb"/Type\s*/Page[^s]", f.read()))


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class KeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="mathdesk-export-"))
        self.worker = ex.PdfWorker(os.path.join(self.tmp, "pdf"), os.path.join(self.tmp, "prof"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_safe_key_mapping(self):
        self.assertEqual(ex.safe_key("turn-123.x_y"), "turn-123.x_y")
        self.assertNotEqual(ex.safe_key("a/b"), ex.safe_key("a_b"))
        self.assertRaises(ValueError, ex.safe_key, "")
        self.assertLessEqual(len(ex.safe_key("x" * 5000)), 120)
        for key in ("a/b", "../../etc/passwd", "/abs/path", "a/../../b", "..", ".", "....",
                    "a\x00b", "\u00fcn\u00efcode", ".hidden", "sess/../../../x"):
            name = ex.safe_key(key)
            self.assertRegex(name, r"^[A-Za-z0-9._-]+$")
            self.assertFalse(name.startswith("."), name)
            path = self.worker.path_for(key)
            self.assertEqual(os.path.dirname(os.path.realpath(path)), self.worker.out_dir)

    def test_status_none_then_ready_from_disk(self):
        self.assertEqual(self.worker.status("turn/9")["state"], "none")
        with open(self.worker.path_for("turn/9"), "wb") as f:
            f.write(b"%PDF-1.4\n" + b"0" * 2000)
        fresh = ex.PdfWorker(self.worker.out_dir, self.worker.profile_dir)  # "after a restart"
        st = fresh.status("turn/9")
        self.assertEqual(st["state"], "ready")
        self.assertEqual(st["path"], self.worker.path_for("turn/9"))
        fresh.submit("turn/9", "http://127.0.0.1:1/")   # ready: ignored, nothing queued
        self.assertEqual(fresh._q.qsize(), 0)

    def test_allowed_urls(self):
        for url in ("http://127.0.0.1:8741/print/turn/abc#t=NONCE", "http://127.0.0.1:8741#t=x",
                    "http://localhost:8741/?a=1", "http://127.0.0.1/"):
            self.assertTrue(ex.ALLOWED_URL.match(url), url)
        for url in ("https://example.com/", "http://127.0.0.1.evil.com/", "http://127.0.0.1@evil.com/",
                    "file:///etc/passwd", "http://localhost:8741evil/"):
            self.assertFalse(ex.ALLOWED_URL.match(url), url)

    def test_refused_url(self):
        t = time.time()
        st = self.worker.render_now("x", "https://example.com/", timeout=5)
        self.assertEqual(st["state"], "failed")
        self.assertIn("refused URL", st["error"])
        self.assertLess(time.time() - t, 1)


class TexCheckTests(unittest.TestCase):
    def test_key_is_deterministic(self):
        a = ex.tex_key("x", True, {"\\A": "1", "\\B": "2"})
        b = ex.tex_key("x", True, {"\\B": "2", "\\A": "1"})
        self.assertEqual(a, b)
        self.assertNotEqual(a, ex.tex_key("x", False, {"\\A": "1", "\\B": "2"}))
        self.assertRegex(a, r"^[0-9a-f]{64}$")

    def test_refusals_before_compiling(self):
        cache = tempfile.mkdtemp(prefix="mathdesk-texcheck-")
        try:
            bad = [r"\input{/etc/passwd}", r"\immediate\write18{ls}", r"\write18{ls}",
                   r"^^5cinput{/etc/passwd}", r"\begin{input}/etc/passwd\end{input}",
                   r"\newcommand\foo{input}\begin\foo", r"\csname input\endcsname",
                   r"\def\x{y}", r"\catcode`\~=0", r"\pdffiledump{/etc/passwd}",
                   r"\openin1=/etc/passwd", r"\special{x}", r"\directlua{os.exit()}",
                   r"\usepackage{x}", r"\begin{document}", r"\@@input /etc/passwd",
                   "x" * 8001]
            for tex in bad:
                r = ex.tex_to_svg(tex, True, {}, cache)
                self.assertFalse(r["ok"], tex[:40])
                self.assertTrue(r["error"].startswith("refused"), (tex[:40], r["error"]))
            r = ex.tex_to_svg("x", True, {"\\X": "\\input{/etc/passwd}"}, cache)
            self.assertFalse(r["ok"])
            self.assertIn("macro", r["error"])
            r = ex.tex_to_svg("x", True, {"X y": "1"}, cache)
            self.assertFalse(r["ok"])
            self.assertFalse(os.path.exists(os.path.join(cache, "jobs")))  # nothing ran
        finally:
            shutil.rmtree(cache, ignore_errors=True)

    def test_allowed_tokens(self):
        for tex in (r"a \\ b", r"\begin{tikzcd} A \arrow[r] & B \end{tikzcd}", r"\operatorname{Spec} R",
                    r"\mathscr{O}_{\mathbb{P}^n}(-1)", r"\newcommand{\Hom}{\operatorname{Hom}}\Hom"):
            self.assertIsNone(ex.check_tex(tex), tex)


@unittest.skipUnless(HAVE_TEX, "latex, dvisvgm or sandbox-exec missing")
class TexTests(unittest.TestCase):
    def setUp(self):
        self.cache = os.path.realpath(tempfile.mkdtemp(prefix="mathdesk-tex-"))

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)

    def _jobs_left(self):
        jobs = os.path.join(self.cache, "jobs")
        return os.listdir(jobs) if os.path.isdir(jobs) else []

    def test_tikzcd_display_and_cache_hit(self):
        tex = r"\begin{tikzcd} A \arrow[r] & B \end{tikzcd}"
        t = time.time()
        r = ex.tex_to_svg(tex, True, {}, self.cache)
        first = time.time() - t
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["svg_path"], os.path.join(self.cache, r["key"] + ".svg"))
        with open(r["svg_path"], "rb") as f:
            self.assertIn(b"<svg", f.read(512))
        self.assertLess(first, 5.0)
        t = time.time()
        r2 = ex.tex_to_svg(tex, True, {}, self.cache)
        hit = time.time() - t
        self.assertEqual(r2["svg_path"], r["svg_path"])
        self.assertLess(hit, 0.05)
        self.assertEqual(self._jobs_left(), [])
        sys.stderr.write("\n  tikz-cd compile %.2fs, cache hit %.4fs " % (first, hit))

    def test_inline_with_macros(self):
        macros = {"\\OO": "\\mathcal{O}", "\\abs": "\\left|#1\\right|", "\\O": "\\Omega"}
        r = ex.tex_to_svg(r"\OO_X(-1) \otimes \abs{\O}", False, macros, self.cache)
        self.assertTrue(r["ok"], r)
        self.assertTrue(os.path.isfile(r["svg_path"]))

    def test_latex_error_reports_bang_line(self):
        r = ex.tex_to_svg(r"\frac{1}{2} \undefinedmacro x", True, {}, self.cache)
        self.assertFalse(r["ok"])
        self.assertTrue(r["error"].startswith("!"), r["error"])
        self.assertIn("! Undefined control sequence.", r["log"])
        self.assertIsNone(r["svg_path"])
        self.assertEqual(self._jobs_left(), [])

    def test_timeout_kills_runaway(self):
        t = time.time()
        r = ex.tex_to_svg(r"\newcommand\x{\x}\x", True, {}, self.cache, timeout=3)
        self.assertFalse(r["ok"])
        self.assertIn("timed out", r["error"])
        self.assertLess(time.time() - t, 6)
        self.assertEqual(self._jobs_left(), [])

    def test_kpathsea_paranoid_blocks_absolute_input(self):
        # Defence in depth: even if the token check missed \input, kpathsea refuses.
        job = tempfile.mkdtemp(dir=self.cache)
        with open(os.path.join(job, "job.tex"), "w") as f:
            f.write("\\documentclass{article}\\begin{document}\\input{/etc/hosts}\\end{document}\n")
        latex = ex.tex_tool("latex")
        code, out = ex._run_sandboxed([latex, "-interaction=nonstopmode", "-halt-on-error",
                                       "-no-shell-escape", "job.tex"], job,
                                      ex._tex_env(job, os.path.dirname(latex)),
                                      time.monotonic() + 15, "out.txt")
        self.assertNotEqual(code, 0)
        self.assertIn("openin_any = p", out)


@unittest.skipUnless(HAVE_SANDBOX, "sandbox-exec missing")
class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="mathdesk-sb-"))
        self.job = os.path.join(self.root, "job")
        os.mkdir(self.job)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, argv):
        return subprocess.run(ex.sandbox_argv(self.job, argv), cwd=self.job,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)

    def test_writes_only_in_job_dir(self):
        outside = os.path.join(self.root, "outside")
        p = self._run(["/usr/bin/touch", outside])
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(b"Operation not permitted", p.stdout)
        self.assertFalse(os.path.exists(outside))
        p = self._run(["/usr/bin/touch", os.path.join(self.job, "inside")])
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.job, "inside")))

    def test_network_denied(self):
        code = ("import socket\ns = socket.socket()\ns.settimeout(3)\n"
                "try:\n    s.connect(('1.1.1.1', 80)); print('CONNECTED')\n"
                "except OSError as e:\n    print('errno', e.errno)\n")
        p = self._run(["/usr/bin/python3", "-c", code])
        self.assertIn(b"errno 1", p.stdout)   # EPERM from the sandbox, not a routing error

    def test_home_reads_denied(self):
        home = os.path.realpath(os.path.expanduser("~"))
        if not os.path.realpath(MODULE).startswith(home + os.sep):
            self.skipTest("harness is not under $HOME")
        p = self._run(["/bin/cat", MODULE])
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(b"Operation not permitted", p.stdout)


@unittest.skipUnless(HAVE_CHROME, "Chrome/Brave/Chromium missing")
class PdfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.tmp = os.path.realpath(tempfile.mkdtemp(prefix="mathdesk-pdf-"))
        cls.worker = ex.PdfWorker(os.path.join(cls.tmp, "pdf"), os.path.join(cls.tmp, "prof"))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        left = subprocess.run(["pgrep", "-f", cls.worker.profile_dir],
                              stdout=subprocess.PIPE).stdout.strip()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        assert not left, "Chrome processes left behind: %r" % left

    def _check_pdf(self, st, min_pages=1):
        self.assertEqual(st["state"], "ready", st)
        self.assertTrue(st["path"].startswith(self.worker.out_dir + os.sep))
        with open(st["path"], "rb") as f:
            self.assertEqual(f.read(4), b"%PDF")
        self.assertGreater(os.path.getsize(st["path"]), 1024)
        self.assertGreaterEqual(_pages(st["path"]), min_pages)
        self.assertEqual([n for n in os.listdir(self.worker.out_dir) if n.startswith(".tmp-")], [])

    def test_render_now_waits_for_ready(self):
        t = time.time()
        st = self.worker.render_now("turn/../now", self.base + "/", timeout=30)
        sys.stderr.write("\n  cdp render %.2fs " % (time.time() - t))
        self._check_pdf(st, min_pages=2)

    def test_submit_is_async_and_idempotent(self):
        self.worker.submit("sess/a", self.base + "/")
        self.assertEqual(self.worker.status("sess/a")["state"], "pending")
        seq = self.worker._jobs["sess/a"]["seq"]
        self.worker.submit("sess/a", self.base + "/")
        self.assertEqual(self.worker._jobs["sess/a"]["seq"], seq)
        deadline = time.time() + 45
        while self.worker.status("sess/a")["state"] == "pending" and time.time() < deadline:
            time.sleep(0.2)
        self._check_pdf(self.worker.status("sess/a"), min_pages=2)
        self.worker.submit("sess/a", self.base + "/")   # ready: ignored
        self.assertEqual(self.worker.status("sess/a")["state"], "ready")
        self.assertNotIn("sess/a", self.worker._jobs)

    def test_unreachable_url_fails_within_timeout(self):
        t = time.time()
        st = self.worker.render_now("down", "http://127.0.0.1:%d/" % _closed_port(), timeout=10)
        self.assertEqual(st["state"], "failed")
        self.assertIn("ERR_CONNECTION_REFUSED", st["error"])
        self.assertLessEqual(len(st["error"]), 2048)
        self.assertLess(time.time() - t, 13)

    def test_http_error_and_missing_ready(self):
        st = self.worker.render_now("missing", self.base + "/nope", timeout=10)
        self.assertEqual(st["state"], "failed")
        self.assertIn("HTTP 404", st["error"])
        t = time.time()
        st = self.worker.render_now("never", self.base + "/never", timeout=5)
        self.assertEqual(st["state"], "failed")
        self.assertIn("READY", st["error"])
        self.assertLess(time.time() - t, 8)

    def test_failed_title_is_immediate_failure(self):
        t = time.time()
        st = self.worker.render_now("failed", self.base + "/failed#t=SECRETNONCE", timeout=20)
        self.assertEqual(st["state"], "failed")
        self.assertIn("print page reported a rendering failure", st["error"])
        self.assertIsNone(st["path"])                      # nothing published
        self.assertLess(time.time() - t, 8)

    def test_fragment_nonce_accepted_and_never_logged(self):
        st = self.worker.render_now("frag", self.base + "/#t=SECRETNONCE", timeout=30)
        self._check_pdf(st, min_pages=2)
        st = self.worker.render_now("frag404", self.base + "/nope?x=SECRETQ#t=SECRETNONCE", timeout=10)
        self.assertEqual(st["state"], "failed")
        self.assertIn("HTTP 404", st["error"])
        self.assertNotIn("SECRET", st["error"])

    def test_cli_mode(self):
        w = ex.PdfWorker(self.worker.out_dir, self.worker.profile_dir)
        w.mode = "cli"
        t = time.time()
        st = w.render_now("cli", self.base + "/", timeout=30)
        sys.stderr.write("\n  cli render %.2fs " % (time.time() - t))
        self._check_pdf(st, min_pages=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
