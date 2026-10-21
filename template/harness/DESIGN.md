# MathDesk design (frozen interfaces, v1)

MathDesk is the harness's name. This document is the contract that the parent
and builder subagents implement against. Change it only through the parent.

## Shape

```
Claude Code / Codex Stop + UserPromptSubmit hooks
  -> harness/hook.py (stdlib, py3.9, no rendering, no network, exits 0, no stdout)
  -> one JSON file per event in .hub/harness/spool/YYYYMMDD/ (tmp + os.replace)
  -> mathdesk server (stdlib ThreadingHTTPServer on 127.0.0.1:8741)
       spool index (restart = rescan), session index, transcript adapters,
       hub data, PDF worker (headless Chrome), sandboxed LaTeX snippets
  -> browser page (vanilla JS, vendored KaTeX/marked/DOMPurify, long-poll)
```

It is a companion, not a driver. Prompts and approvals stay in the terminal.
Turns are captured by the hooks, which is the robust layer: their payloads
are documented schemas. Transcripts add history and intermediate messages,
which is the best-effort layer. Adapters are versioned, and if one fails the
page shows a visible warning and falls back to the spool events.

## Layout

```
harness/
  hook.py                 parent        hook entry for both CLIs
  mathdesk.py             parent        CLI: serve | start | stop | status | open | install | uninstall | doctor
  core/__init__.py
  core/config.py          parent        paths, port, token, constants
  core/spool.py           parent        event schema, write (hook side), scan/index (server side)
  core/server.py          parent        HTTP routing, auth, security headers, long-poll
  core/transcripts.py     builder A     native transcript adapters
  core/hubdata.py         builder C     records, documents, search, library
  core/export.py          builder B     Chrome PDF worker, sandboxed TeX -> SVG
  static/index.html, app.js, render.js, mathsplit.js, style.css, macros.json   parent
  tests/                  test_<module>.py per owner; fixtures/<owner>/...
```

Runtime state lives in `.hub/harness/` (mode 0700): `spool/`, `token`, `server.pid`,
`server.log`, `pdf/`, `texcache/`, `pins.json`, `chrome-profile/`.

Stdlib only. `hook.py`, `core/spool.py`, `core/transcripts.py` and `core/export.py` must run
on `/usr/bin/python3` (3.9.6): no `match`, no runtime `X | Y` unions. The server runs on
the interpreter recorded at install (3.11+) because `hub.py` needs 3.11+
(`tomllib`); `core/hubdata.py` may therefore rely on 3.11+, but nothing else should.

## Event (spool file)

```json
{"v": 1, "id": "<20 hex>", "kind": "final" | "prompt",
 "service": "claude-code" | "codex", "session_id": "...", "turn_id": "..." | null,
 "cwd": "/abs", "project": "demo" | null, "transcript_path": "/abs" | null,
 "ts": "2026-10-21T07:00:00.123Z", "text": "...", "text_sha256": "...", "model": "..." | null}
```

Here `turn_id` is Codex's `turn_id` or Claude's `prompt_id` when present. The `id`
is a hash over the service, session, turn, kind, ts and text hash, so two
identical answers on different turns are both kept. The server drops an
event only when it repeats the same (service, session, turn_id, kind,
text_sha256) with a non-null turn_id.

## Builder interfaces

### core/transcripts.py (builder A)

```python
ADAPTER_VERSION = {"claude-code": "claude-jsonl/1", "codex": "codex-rollout/1"}

class Transcript:
    """Incremental reader for one native transcript file."""
    def __init__(self, service: str, path: str): ...
    def refresh(self) -> None:          # read appended bytes only; tolerate a partial last line,
                                        # truncation/rotation (restart from 0), malformed lines
    def items(self) -> list: ...        # ordered conversation items, see below
    def status(self) -> dict: ...       # {"adapter": str, "ok": bool, "warning": str|None,
                                        #  "lines": int, "malformed": int, "unknown": int, "bytes": int}

def discover(service: str, hub_root: str, since_days: int = 14, limit: int = 60) -> list:
    """Recent native sessions whose cwd is inside hub_root:
       [{"session_id", "transcript_path", "cwd", "started", "updated", "title"}]"""
```

Item: `{"kind": "user" | "assistant" | "tool" | "notice", "ts": iso|None,
"text": str, "name": str|None, "id": str|None}`.

- `user` items are only the prompts the person really typed. Tool results,
  injected context, system reminders, AGENTS.md or environment preambles and
  compaction summaries are excluded.
- `assistant` items carry the full visible text, never truncated. Thinking
  and reasoning are omitted.
- A `tool` item is a one-line summary: the tool name and a short argument
  gist of at most 160 characters.
- A `notice` item covers compaction, interruptions and unknown line types,
  which are counted.

The reader holds a byte offset, so a 50 MB transcript must not be reparsed
on every refresh. Never truncate assistant text; the hub's `tools/recall/native.py`
truncates on purpose and must not be reused for content.

### core/hubdata.py (builder C)

```python
class Hub:
    def __init__(self, root: str): ...
    def id_regex(self) -> str: ...             # JS-compatible source matching hub IDs, no false hits on R^2 etc.
    def projects(self) -> list: ...            # [{"name","title","root"}]
    def project_for_cwd(self, cwd: str) -> str|None
    def record(self, ident: str) -> dict|None  # {"id","project","kind","title","status","body_markdown",
                                               #  "fields": {..}, "corrections": [...], "paths": [...]}
    def search(self, q: str, project: str|None = None, limit: int = 20) -> list
                                               # [{"type": "record"|"doc", "id"|"path", "title", "snippet", "project"}]
    def documents(self, project: str|None = None) -> list   # [{"path" (hub-relative), "title", "kind", "project", "mtime"}]
    def read_document(self, relpath: str) -> dict|None      # {"path","text","mtime"}; .md/.txt/.tex only, realpath inside hub
    def library_pdf(self, ident: str) -> str|None           # absolute path of a retained PDF, only inside hub/archive roots
    def known_ids(self) -> list                             # every resolvable record/source ID (client links only these)
```

Everything is read-only, with no writes anywhere. The hub's own modules
(catalog_store, ...) may be imported in-process if they work on 3.9, or the
CLI may be called through subprocess with a timeout. Return plain dicts that
can be serialized to JSON.

### core/export.py (builder B)

```python
def chrome_path() -> str|None
class PdfWorker:
    def __init__(self, out_dir: str, profile_dir: str): ...   # one Chrome at a time, background thread
    def submit(self, key: str, url: str) -> None               # idempotent per key
    def status(self, key: str) -> dict                         # {"state": "pending"|"ready"|"failed"|"none", "path", "error"}
    def render_now(self, key: str, url: str, timeout: float = 30) -> dict   # synchronous, same result shape
def tex_to_svg(tex: str, display: bool, macros: dict, cache_dir: str, timeout: float = 15) -> dict
    # {"ok": bool, "key": sha256, "svg_path": str|None, "error": str|None, "log": str|None}
```

- PDF output is atomic: write to a temporary file and `os.replace`. Chrome
  runs headless with a dedicated `--user-data-dir`, `--no-pdf-header-footer`
  and a virtual time budget. The page signals readiness by setting
  `document.title` to `READY`.
- `tex_to_svg` runs only when the user clicks. It runs
  `latex -no-shell-escape -halt-on-error` and then `dvisvgm --no-fonts`
  inside `sandbox-exec`:
  - no network, and writes only in the job directory;
  - `openin_any=p` and `openout_any=p`, and inputs over 8 kB are refused;
  - `\write18`, `\input`, `\include`, `\openin`, `\openout`, `\special`,
    `\directlua` and `\catcode` are blocked by a token check before
    compiling;
  - the preamble contains amsmath, amssymb, mathtools, amscd, tikz-cd and
    mathrsfs, plus the shared macros.

  The SVG is served as an `<img>` and never inlined.

## Rendering contract (static/render.js, parent)

Code spans and fences are protected first. Math is extracted next, before
Markdown can consume backslashes: `$$..$$`, `\[..\]`, `\(..\)`, standalone
amsmath environments and ```` ```math ```` fences. `$..$` follows Pandoc's
rules: the opening `$` is followed by a non-space, the closing `$` is
preceded by a non-space and not followed by a digit, and no blank line falls
in between. A per-page toggle disables single-dollar math. The text then
goes through marked and DOMPurify, and the KaTeX HTML is spliced in, with
`trust: false`, `maxExpand: 1000` and `strict: "ignore"`. Each response starts
from a fresh copy of the shared macros, and `\DeclareMathOperator` becomes
`\operatorname`. An error box shows the source and the message and offers
"Typeset with LaTeX". Hub IDs are linked through text nodes only, skipping
code, math and links.

## Security

Revised after the second independent review. Cookies were dropped because browsers do not scope
them by port.

- The server binds 127.0.0.1 only.
- Host must be `127.0.0.1:8741` or `localhost:8741`.
- The page, `/print/` pages and `/static/` carry no data and need no
  credentials. Every data route (`/api/`, `/pdf/`, `/tex/`, `/library/`)
  needs `Authorization: Bearer TOKEN`.
- The token lives in `.hub/harness/token` (0600; created with `os.link`, so
  concurrent starts agree on it). It reaches the page once, in the URL
  fragment `#t=...`, which browsers never send over HTTP. The page keeps it
  in localStorage, which is scoped to this origin and port.
- PDFs and LaTeX images are fetched with the header into blob URLs.
- Print jobs get 5-minute read-only nonces instead of the token. A print page
  that fails to render reports it, and that PDF is never published.
- POSTs need the full token, `X-MathDesk: 1` and a same-origin `Origin`.
  There are no CORS headers.
- CSP: `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';
  img-src 'self' data: blob:; font-src 'self'; connect-src 'self';
  object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'`.
- Model output is untrusted.
  - Raw HTML is shown as text, and DOMPurify runs.
  - No img, svg or media tags from model output.
  - Math placeholders carry a random per-render nonce.
  - KaTeX runs with `trust: false`.
- File access is limited to the hub root and registered archive roots, and
  `.hub/` is never served. Transcript paths must lie under the CLIs' own
  session directories.
- LaTeX runs only on a click, under sandbox-exec with no network, writes only
  in the job directory, no reads under `$HOME`, paranoid kpathsea and a
  primitive deny list. Results are cached, and cached SVGs are reused without
  compiling.
- Codex reviews run only on a click plus confirmation, one at a time, and can
  be cancelled. Each is `codex exec -s read-only --ephemeral --ignore-user-config`
  with the API-key variables removed, after the ChatGPT login is checked.

## Decisions from the independent design review

These were adopted:

- A companion, not a driver.
- Hooks first, with versioned adapters.
- An automatic PDF snapshot of each final response, with pending, ready and
  failed states.
- The selected session stays put, and unread markers show new activity.
- No deduplication by text alone.
- Authentication beyond loopback.
- Hook installation preserves existing hooks.
- A general Markdown-to-LaTeX exporter is deferred, so the notebook exports
  through the browser PDF and as raw Markdown.
- Review packets: frozen pins with provenance, and a one-click Codex review.

These differ from that review, with reasons:

- **KaTeX rather than MathJax.** Rendering is synchronous, which keeps long
  transcripts and PDF readiness simple. It is already vendored and verified.
  Sandboxed real LaTeX covers what is left, such as tikz-cd, so the page
  never needs two browser engines.
- **Single-dollar math on by default, behind a toggle.** Real agent output
  uses `$..$`, and the Pandoc rules handle currency.
- **Python stdlib rather than Node.** The hub tooling is Python, the hub
  modules can be called in-process, and there is no dependency tree.
