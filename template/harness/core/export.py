"""MathDesk export: headless-Chrome PDF worker and sandboxed TeX -> SVG snippets.

PDF.  `PdfWorker` prints pages served by the local MathDesk server.  Chrome is
driven over the DevTools protocol on `--remote-debugging-pipe` (NUL-delimited
JSON on fds 3/4; no websocket, stdlib only) so that the worker can wait until
the page sets `document.title = "READY"` (or fails at once on "FAILED"), see
navigation/HTTP errors and then call Page.printToPDF.  Plain `--print-to-pdf` cannot wait on the title: it
prints after the load event, and only `--virtual-time-budget` (which pauses
virtual time while fetches are pending) delays it; `--timeout` does not.  That
mode is kept as `PdfWorker.mode = "cli"` for fallback.  One Chrome process
runs at a time; output is written to a temporary file and os.replace'd.

TeX.  `tex_to_svg` compiles one snippet with `latex` + `dvisvgm --no-fonts`
inside `sandbox-exec` (no network, writes only in the job directory, no reads
under $HOME except the job and TeX directories), with kpathsea paranoid
open modes, no shell escape, a file-size/CPU ulimit and a wall-clock timeout.
A token check refuses file, shell, catcode and csname-construction primitives
before anything runs; it is a first filter, the sandbox is the boundary.

Python 3.9 stdlib only.
"""

import base64
import fcntl
import hashlib
import itertools
import json
import os
import queue
import re
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time

# ---------------------------------------------------------------------------
# Chrome PDF worker

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
# Only the local server is printed; Chrome never fetches arbitrary URLs.  Print
# URLs may carry an auth nonce in the fragment, so errors show _redact(url) only.
ALLOWED_URL = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d{1,5})?([/?#]|$)")
READY_TITLE = "READY"
FAILED_TITLE = "FAILED"   # the print page's own rendering-failure signal
CHROME_FLAGS = (
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--use-mock-keychain", "--disable-background-networking", "--disable-component-update",
    "--disable-sync", "--disable-extensions", "--disable-default-apps", "--mute-audio",
    "--hide-scrollbars", "--disable-features=Translate,MediaRouter,OptimizationHints",
)
CLI_FLAGS = ("--no-pdf-header-footer", "--run-all-compositor-stages-before-draw",
             "--virtual-time-budget=15000")
_READY_JS = ("(() => { const n = performance.getEntriesByType('navigation')[0];"
             " return {title: document.title, status: n ? (n.responseStatus || 0) : 0}; })()")
# After READY, wait for web fonts (KaTeX) and eager images before printing.
_SETTLE_JS = ("Promise.all([document.fonts.ready].concat(Array.from(document.images)"
              ".filter(i => !i.complete && i.loading !== 'lazy').map(i => new Promise(r => {"
              " i.addEventListener('load', r); i.addEventListener('error', r); }))))"
              ".then(() => true)")
_NOISE = re.compile(r"task_policy_set|CVDisplayLink|interface_endpoint_client|"
                    r"registration_request|DEPRECATED_ENDPOINT|allocator multiple times|"
                    r"TensorFlow Lite|devtools_pipe_handler")
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def chrome_path():
    """Google Chrome, then Brave, then Chromium; None if none is installed."""
    for p in CHROME_CANDIDATES:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    for name in ("google-chrome", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _redact(url):
    return url.split("#", 1)[0].split("?", 1)[0]


def _scrub(text, url):
    """Remove the URL's query and fragment (which may hold a nonce) from text."""
    for part in (url.partition("#")[2], url.partition("?")[2]):
        if part:
            text = text.replace(part, "<redacted>")
    return text


def safe_key(key):
    """Flat file name for a key: [A-Za-z0-9._-] only, never '.', '..' or hidden.
    A key that had to be altered gets a hash suffix so distinct keys stay distinct."""
    if not isinstance(key, str) or not key:
        raise ValueError("empty key")
    s = _SAFE.sub("_", key).lstrip(".")[:100]
    if s != key:
        s = (s or "k") + "-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return s


class _Fail(Exception):
    pass


class _CdpError(_Fail):
    pass


class _Cdp(object):
    """Minimal DevTools client over Chrome's --remote-debugging-pipe."""

    def __init__(self, wfd, rfd):
        self.wfd, self.rfd, self.n = wfd, rfd, 0
        self.buf, self.scan = bytearray(), 0

    def _next(self, deadline):
        while True:
            i = self.buf.find(b"\0", self.scan)
            if i >= 0:
                raw = bytes(self.buf[:i])
                del self.buf[:i + 1]
                self.scan = 0
                return json.loads(raw.decode("utf-8"))
            self.scan = len(self.buf)
            left = deadline - time.monotonic()
            if left <= 0:
                raise _Fail("timed out waiting for Chrome")
            if select.select([self.rfd], [], [], left)[0]:
                chunk = os.read(self.rfd, 1 << 20)
                if not chunk:
                    raise _Fail("Chrome closed the DevTools pipe")
                self.buf += chunk

    def call(self, method, params=None, session=None, deadline=None):
        self.n += 1
        msg = {"id": self.n, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        data = json.dumps(msg).encode("utf-8") + b"\0"
        while data:
            data = data[os.write(self.wfd, data):]
        while True:
            m = self._next(deadline)
            if m.get("id") == self.n:
                if "error" in m:
                    raise _CdpError("%s: %s" % (method, m["error"].get("message")))
                return m.get("result") or {}


def _kill_group(proc, grace=3.0):
    try:
        proc.wait(grace)
    except subprocess.TimeoutExpired:
        pass
    try:  # also takes down helpers left in the group
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


def _stderr_tail(f, limit=2048):
    try:
        f.seek(0)
        text = f.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return ""
    lines = [l for l in text.splitlines() if l.strip() and not _NOISE.search(l)]
    return "\n".join(lines)[-limit:]


def _check_pdf(path):
    with open(path, "rb") as f:
        head = f.read(5)
    size = os.path.getsize(path)
    if head[:4] != b"%PDF" or size <= 1024:
        raise _Fail("output is not a PDF (%d bytes, starts %r)" % (size, head))


class PdfWorker(object):
    """Background PDF printer.  Keys map to out_dir/<safe_key>.pdf."""

    timeout = 30.0
    mode = "cdp"   # "cdp": wait for document.title == READY; "cli": --print-to-pdf

    def __init__(self, out_dir, profile_dir):
        self.out_dir = os.path.realpath(out_dir)
        self.profile_dir = os.path.realpath(profile_dir)
        for d in (self.out_dir, self.profile_dir):
            os.makedirs(d, mode=0o700, exist_ok=True)
        now = time.time()
        for name in os.listdir(self.out_dir):  # leftovers of an interrupted render
            p = os.path.join(self.out_dir, name)
            if name.startswith(".tmp-") and now - os.path.getmtime(p) > 3600:
                try:
                    os.unlink(p)
                except OSError:
                    pass
        self._jobs = {}   # key -> {"state": "pending"|"failed", "error", "seq"}; ready = file
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self._chrome = threading.Lock()   # one Chrome process at a time
        self._q = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="mathdesk-pdf", daemon=True)
        self._thread.start()

    def path_for(self, key):
        return os.path.join(self.out_dir, safe_key(key) + ".pdf")

    def status(self, key):
        path = self.path_for(key)
        with self._lock:
            j = dict(self._jobs.get(key) or {})
        exists = os.path.isfile(path)
        state = j.get("state")
        if state not in ("pending", "failed"):
            state = "ready" if exists else "none"
        return {"state": state, "path": path if exists else None,
                "error": j.get("error") if state == "failed" else None}

    def submit(self, key, url):
        """Queue a render unless one is pending or the PDF is ready."""
        path = self.path_for(key)
        with self._lock:
            j = self._jobs.get(key)
            if j and j["state"] == "pending":
                return
            if os.path.isfile(path) and not (j and j["state"] == "failed"):
                return
            seq = self._mark(key)
        self._q.put((key, url, seq))

    def render_now(self, key, url, timeout=30):
        """Render synchronously (always re-renders) and return status(key)."""
        with self._lock:
            seq = self._mark(key)
        return self._run(key, url, seq, timeout)

    def _mark(self, key):
        seq = next(self._seq)
        self._jobs[key] = {"state": "pending", "error": None, "seq": seq}
        return seq

    def _loop(self):
        while True:
            key, url, seq = self._q.get()
            self._run(key, url, seq, self.timeout)

    def _run(self, key, url, seq, timeout):
        with self._chrome:
            with self._lock:
                j = self._jobs.get(key)
                current = bool(j and j["seq"] == seq)   # else superseded by a newer request
            if current:
                try:
                    error = self._render(key, url, timeout)
                except Exception as e:   # keep the worker thread and callers alive
                    error = _scrub("internal error: %r" % (e,), url)
                with self._lock:
                    if self._jobs.get(key, {}).get("seq") == seq:
                        if error:
                            self._jobs[key].update(state="failed", error=error[:2048])
                        else:   # ready is read from disk; keep memory bounded
                            del self._jobs[key]
        return self.status(key)

    def _render(self, key, url, timeout):
        """Render url to the key's PDF.  Returns None on success, else an error string."""
        if not isinstance(url, str) or not ALLOWED_URL.match(url):
            return "refused URL (only http://127.0.0.1 / localhost pages are printed)"
        chrome = chrome_path()
        if not chrome:
            return "Chrome, Brave or Chromium not found"
        os.makedirs(self.out_dir, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".pdf", dir=self.out_dir)
        os.close(fd)
        try:
            with tempfile.TemporaryFile() as err:
                try:
                    if self.mode == "cli":
                        self._render_cli(chrome, url, tmp, timeout, err)
                    else:
                        self._render_cdp(chrome, url, tmp, timeout, err)
                    _check_pdf(tmp)
                except _Fail as e:
                    tail = _stderr_tail(err)
                    return _scrub(str(e) + ("\n" + tail if tail else ""), url)[:2048]
            os.replace(tmp, self.path_for(key))
            return None
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _base_args(self, chrome):
        return [chrome] + list(CHROME_FLAGS) + ["--user-data-dir=" + self.profile_dir]

    def _render_cli(self, chrome, url, tmp, timeout, err):
        args = self._base_args(chrome) + list(CLI_FLAGS) + ["--print-to-pdf=" + tmp, url]
        os.unlink(tmp)   # Chrome creates it; absence afterwards means failure
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=err, start_new_session=True)
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            raise _Fail("Chrome timed out after %gs" % timeout)
        finally:
            _kill_group(proc, 0)
        if not os.path.exists(tmp):
            raise _Fail("Chrome wrote no PDF (exit %s)" % proc.returncode)

    def _render_cdp(self, chrome, url, tmp, timeout, err):
        t0, grace = time.monotonic(), 0.5
        deadline, ready_by = t0 + timeout, t0 + timeout * 0.8
        # Chrome reads commands on fd 3 and writes replies on fd 4.  The child
        # ends are moved to fds >= 10 so the shell's dup2 cannot clobber them.
        cmd_r, cmd_w = os.pipe()
        out_r, out_w = os.pipe()
        dupfd = getattr(fcntl, "F_DUPFD_CLOEXEC", fcntl.F_DUPFD)
        a, b = fcntl.fcntl(cmd_r, dupfd, 10), fcntl.fcntl(out_w, dupfd, 10)
        os.close(cmd_r)
        os.close(out_w)
        sh = 'exec "$0" "$@" 3<&%d 4>&%d %d<&- %d>&-' % (a, b, a, b)
        try:
            proc = subprocess.Popen(
                ["/bin/sh", "-c", sh] + self._base_args(chrome)
                + ["--remote-debugging-pipe", "about:blank"],
                pass_fds=(a, b), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=err, start_new_session=True)
        except OSError as e:
            for fd in (a, b, cmd_w, out_r):
                os.close(fd)
            raise _Fail("cannot start Chrome: %s" % e)
        os.close(a)
        os.close(b)
        try:
            cdp = _Cdp(cmd_w, out_r)
            tid = cdp.call("Target.createTarget", {"url": "about:blank"},
                           deadline=deadline)["targetId"]
            sid = cdp.call("Target.attachToTarget", {"targetId": tid, "flatten": True},
                           deadline=deadline)["sessionId"]
            nav = cdp.call("Page.navigate", {"url": url}, sid, deadline)
            if nav.get("errorText"):
                raise _Fail("navigation failed: %s" % nav["errorText"])
            title = None
            while True:
                try:
                    v = cdp.call("Runtime.evaluate", {"expression": _READY_JS,
                                 "returnByValue": True}, sid, deadline)
                    v = (v.get("result") or {}).get("value") or {}
                except _CdpError:   # context replaced mid-navigation; poll again
                    v = {}
                title = v.get("title", title)
                if v.get("status", 0) >= 400:
                    raise _Fail("HTTP %d from %s" % (v["status"], _redact(url)))
                if title == READY_TITLE:
                    break
                if title == FAILED_TITLE:
                    raise _Fail("print page reported a rendering failure")
                if time.monotonic() > ready_by:
                    raise _Fail("page did not set document.title to %s within %.0fs "
                                "(title %r)" % (READY_TITLE, ready_by - t0, title))
                time.sleep(0.1)
            cdp.call("Runtime.evaluate", {"expression": _SETTLE_JS, "awaitPromise": True,
                                          "returnByValue": True}, sid, deadline)
            res = cdp.call("Page.printToPDF", {
                "printBackground": True, "preferCSSPageSize": True,
                "generateDocumentOutline": True, "transferMode": "ReturnAsBase64"},
                sid, deadline)
            with open(tmp, "wb") as f:
                f.write(base64.b64decode(res.get("data", "")))
            try:
                cdp.call("Browser.close", deadline=time.monotonic() + 2)
                grace = 3.0   # let Chrome shut down cleanly
            except (_Fail, OSError):
                pass
        except OSError as e:   # broken pipe etc.
            raise _Fail("DevTools pipe error: %s" % e)
        finally:
            os.close(cmd_w)
            os.close(out_r)
            _kill_group(proc, grace)


# ---------------------------------------------------------------------------
# Sandboxed TeX -> SVG

PREAMBLE_VERSION = "1"
MAX_TEX_BYTES = 8000
MAX_MACRO_BYTES = 32000
PACKAGES = "amsmath,amssymb,mathtools,amscd,tikz-cd,mathrsfs"
# 'dvisvgm' as a class option selects the dvisvgm drivers for graphics, pgf
# and l3backend; dvips specials would need Ghostscript, which dvisvgm lacks here.
# decorations.pathmorphing is what tikz-cd's squiggly arrows need.
DOCUMENT = (r"\documentclass[dvisvgm,border=1pt,varwidth=4000pt]{standalone}" "\n"
            r"\usepackage{" + PACKAGES + "}\n"
            r"\usetikzlibrary{decorations.pathmorphing}" "\n%s\n"
            r"\begin{document}" "\n%s\n" r"\end{document}" "\n")
DISPLAY_ENVS = re.compile(r"\\begin\s*\{(equation|align|gather|multline|flalign|alignat|"
                          r"eqnarray|displaymath)\*?\}")
_DENY = frozenset("""
    write immediate input include includeonly openin openout read readline special
    directlua latelua catcode csname ifcsname lastnamedcs scantokens jobname loop
    def gdef edef xdef let futurelet usepackage RequirePackage documentclass LoadClass
    makeatletter ExplSyntaxOn UseName ExpandArgs includegraphics IfFileExists
    InputIfFileExists usetikzlibrary usepgflibrary usepgfmodule pgfimage
    pgfdeclareimage shipout""".split())
_DENY_PREFIX = ("pdf", "lua", "tracing")
_DENY_ENV = frozenset(("document", "filecontents", "filecontents*"))
_TOKEN = re.compile(r"\\([A-Za-z@]+|.)", re.S)
_ENV = re.compile(r"\\(begin|end)(?![A-Za-z@])\s*(\{\s*([A-Za-z]+\*?)\s*\})?")
_MACRO_NAME = re.compile(r"^\\[A-Za-z]+$")
SANDBOX_PROFILE = """(version 1)
(allow default)
(deny network*)
(deny file-write* (require-not (subpath (param "JOB"))))
(allow file-write* (literal "/dev/null"))
(deny file-read* (subpath (param "HOME")))
(allow file-read* (subpath (param "JOB")) (subpath (param "TEXROOT"))
       (subpath (param "TEXMFHOME")) (subpath (param "TEXLIVEHOME")))
"""
FSIZE_BLOCKS = 20000   # ulimit -f for TeX children (1 KiB blocks in macOS sh: ~20 MB)
_TEX_SLOTS = threading.BoundedSemaphore(2)
_TEX_DIRS = ("/Library/TeX/texbin", "/usr/local/texlive/2025/bin/universal-darwin",
             "/opt/homebrew/bin", "/usr/local/bin")


def _denied(name):
    bare = name.replace("@", "")
    return bare in _DENY or bare.rstrip("*") in _DENY or bare.startswith(_DENY_PREFIX)


def check_tex(src):
    """None if src passes the token check, else the refusal message."""
    if "^^" in src:
        return "refused: ^^ character notation is not allowed"
    for m in _TOKEN.finditer(src):
        name = m.group(1)
        if name[0].isalpha() or name[0] == "@":
            if _denied(name):
                return "refused: \\%s is not allowed" % name
    for m in _ENV.finditer(src):
        env = m.group(3)
        if env is None:
            return "refused: \\%s must be followed by {environment-name}" % m.group(1)
        if env in _DENY_ENV or _denied(env):
            return "refused: environment {%s} is not allowed" % env
    return None


def tex_tool(name):
    path = os.pathsep.join([os.environ.get("PATH", "")] + list(_TEX_DIRS))
    return shutil.which(name, path=path)


def _tex_root(latex):
    d = os.path.dirname(os.path.realpath(latex))
    for _ in range(4):
        if os.path.isdir(os.path.join(d, "texmf-dist")):
            return d
        d = os.path.dirname(d)
    return os.path.dirname(os.path.realpath(latex))


def sandbox_argv(jobdir, argv, cpu=30):
    """Command list running argv under the TeX sandbox with ulimits."""
    home = os.path.realpath(os.path.expanduser("~"))
    latex = tex_tool("latex") or "/Library/TeX/texbin/latex"
    params = {"JOB": jobdir, "HOME": home, "TEXROOT": _tex_root(latex),
              "TEXMFHOME": os.path.join(home, "Library", "texmf"),
              "TEXLIVEHOME": os.path.join(home, "Library", "texlive")}
    sb = ["/usr/bin/sandbox-exec"]
    for k in sorted(params):
        sb += ["-D", "%s=%s" % (k, params[k])]
    sh = 'ulimit -f %d; ulimit -t %d; exec "$@"' % (FSIZE_BLOCKS, cpu)
    return ["/bin/sh", "-c", sh, "sh"] + sb + ["-p", SANDBOX_PROFILE] + list(argv)


def _tex_env(jobdir, texbin):
    return {"PATH": texbin + ":/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": os.path.expanduser("~"), "TMPDIR": jobdir, "LC_ALL": "C",
            "openin_any": "p", "openout_any": "p", "shell_escape": "f",
            "TEXMFOUTPUT": jobdir, "MKTEXPK": "0", "MKTEXTFM": "0", "MKTEXMF": "0",
            "MKTEXFMT": "0"}


def _run_sandboxed(argv, jobdir, env, deadline, outname):
    """Run argv sandboxed in jobdir; returns (exit code or None on timeout, output tail)."""
    left = deadline - time.monotonic()
    if left <= 0:
        return None, ""
    out_path = os.path.join(jobdir, outname)
    with open(out_path, "wb") as out:
        proc = subprocess.Popen(sandbox_argv(jobdir, argv, int(left) + 2), cwd=jobdir,
                                env=env, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = proc.wait(left)
        except subprocess.TimeoutExpired:
            code = None
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
    return code, _tail(out_path, 4096)


def _tail(path, nbytes):
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - nbytes))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _log_excerpt(path, n=40):
    """(first '!' line, up to n lines from it) from a TeX log; falls back to the tail."""
    lines = _tail(path, 65536).splitlines()
    for i, line in enumerate(lines):
        if line.startswith("!"):
            out = []
            for l in lines[i:i + n]:
                if l.startswith("Here is how much"):
                    break
                out.append(l)
            return line, "\n".join(out)
    return None, "\n".join(lines[-n:])


def _macro_defs(macros):
    defs = []
    for name in sorted(macros):
        val = macros[name]
        nargs = max([int(d) for d in re.findall(r"#([1-9])", val)] or [0])
        defs.append(r"\providecommand{%s}{}\renewcommand{%s}%s{%s}"
                    % (name, name, "[%d]" % nargs if nargs else "", val))
    return "\n".join(defs)


def _body(tex, display):
    """(document body, dvisvgm --bbox).  standalone's papersize is the exact TeX box
    (tight for tikz, keeps depth for inline math); full-width display environments
    such as align* use the ink bounding box instead."""
    body = tex.strip()
    for left, right, disp in (("$$", "$$", True), ("\\[", "\\]", True), ("\\(", "\\)", False)):
        if len(body) >= len(left) + len(right) and body.startswith(left) and body.endswith(right):
            body, display = body[len(left):-len(right)].strip(), disp
            break
    if DISPLAY_ENVS.match(body):
        return body, "1pt"
    return ("\\[\n%s\n\\]" if display else "$%s$") % body, "papersize"


def tex_key(tex, display, macros):
    blob = json.dumps({"v": PREAMBLE_VERSION, "display": bool(display), "tex": tex,
                       "macros": macros}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def tex_to_svg(tex, display, macros, cache_dir, timeout=15):
    """Typeset one snippet to cache_dir/<key>.svg with real LaTeX, sandboxed."""
    def result(ok=False, svg=None, error=None, log=None):
        return {"ok": ok, "key": key, "svg_path": svg, "error": error, "log": log}

    key = None
    macros = macros or {}
    if not isinstance(tex, str) or not isinstance(macros, dict):
        return result(error="tex must be a string and macros a dict")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in macros.items()):
        return result(error="macro names and values must be strings")
    key = tex_key(tex, display, macros)
    if len(tex.encode("utf-8")) > MAX_TEX_BYTES:
        return result(error="refused: input is larger than %d bytes" % MAX_TEX_BYTES)
    if len(json.dumps(macros)) > MAX_MACRO_BYTES:
        return result(error="refused: macros exceed %d bytes" % MAX_MACRO_BYTES)
    for name, val in macros.items():
        if not _MACRO_NAME.match(name):
            return result(error="refused: bad macro name %r" % name)
        problem = check_tex(name) or check_tex(val)
        if problem:
            return result(error="%s (macro %s)" % (problem, name))
    problem = check_tex(tex)
    if problem:
        return result(error=problem)

    cache_dir = os.path.realpath(cache_dir)
    final = os.path.join(cache_dir, key + ".svg")
    if os.path.isfile(final):
        return result(True, final)
    latex, dvisvgm = tex_tool("latex"), tex_tool("dvisvgm")
    if not (latex and dvisvgm and os.path.exists("/usr/bin/sandbox-exec")):
        return result(error="latex, dvisvgm or sandbox-exec not found")

    jobs = os.path.join(cache_dir, "jobs")
    os.makedirs(jobs, mode=0o700, exist_ok=True)
    if not _TEX_SLOTS.acquire(timeout=timeout):
        return result(error="LaTeX is busy with other snippets; try again")
    try:
        deadline = time.monotonic() + timeout
        # A unique job dir per call so concurrent requests for one key cannot collide.
        jobdir = os.path.realpath(tempfile.mkdtemp(prefix=key[:16] + "-", dir=jobs))
        try:
            body, bbox = _body(tex, display)
            with open(os.path.join(jobdir, "job.tex"), "w", encoding="utf-8") as f:
                f.write(DOCUMENT % (_macro_defs(macros), body))
            env = _tex_env(jobdir, os.path.dirname(latex))
            code, _ = _run_sandboxed([latex, "-interaction=nonstopmode", "-halt-on-error",
                                      "-no-shell-escape", "job.tex"],
                                     jobdir, env, deadline, "latex.out")
            if code != 0:
                bang, log = _log_excerpt(os.path.join(jobdir, "job.log"))
                if code is None:
                    return result(error="LaTeX timed out after %gs" % timeout, log=log)
                return result(error=bang or "LaTeX failed (exit %d)" % code, log=log)
            code, out = _run_sandboxed([dvisvgm, "--no-fonts", "--exact-bbox", "--bbox=" + bbox,
                                        "--cache=none", "-o", "job.svg", "job.dvi"],
                                       jobdir, env, deadline, "dvisvgm.out")
            svg = os.path.join(jobdir, "job.svg")
            if code != 0 or not os.path.isfile(svg):
                what = "dvisvgm timed out" if code is None else "dvisvgm failed (exit %s)" % code
                return result(error=what, log="\n".join(out.splitlines()[-40:]))
            with open(svg, "rb") as f:
                head = f.read(256)
            if b"<svg" not in head:
                return result(error="dvisvgm produced no SVG", log=out[-2048:])
            os.replace(svg, final)
            return result(True, final)
        finally:
            shutil.rmtree(jobdir, ignore_errors=True)
    finally:
        _TEX_SLOTS.release()
