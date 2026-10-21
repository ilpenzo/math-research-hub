# MathDesk

MathDesk is a local reading desk for doing mathematics with Claude Code and
Codex in a research hub. Whenever an agent turn ends anywhere under the hub,
its response is typeset at **http://127.0.0.1:8741/**, with no action from
you. The same page lets you browse each session, look up any record or source
cited in it, read hub documents with their mathematics rendered, collect
excerpts in a notebook, send them to Codex for an independent review, and
save anything as a PDF.

It is a companion to the terminal and does not replace it. Prompts,
approvals and interrupts stay in Claude Code and Codex.

## Everyday use

```sh
python3 harness/mathdesk.py open      # open the desk (starts the server if needed)
python3 harness/mathdesk.py status    # server, hooks and capabilities
python3 harness/mathdesk.py doctor    # status plus end-to-end checks
```

Once the hooks are installed you never need to start it yourself. The first
hub turn starts the server. If a response contains mathematics and no
MathDesk tab is open, a tab opens in the background. A tab that is already
open updates in place. A session you are reading never jumps: other sessions
get an unread badge instead.

| Where | What it does |
|---|---|
| Left rail | Live and earlier sessions from Claude Code (C), Codex (X) and documents pushed to the desk (D) |
| Session | Your prompts and the agent's messages, typeset. Tool calls fold into one line. Each response has **PDF**, **Link**, **Pin**, **Copy source** and **Source** |
| IDs | Every record or source ID (R031, CP-R065, KC-L038, L195...) is a link. Clicking one opens its card, corrections included, in a side drawer |
| Search (`/`) | The hub's ranked search. Typing an exact ID opens that record |
| Documents | NOTEs, PROOFs, GOALs, TOPICS and guides, rendered, with a table of contents and a PDF |
| Notebook (`n`) | Pinned excerpts are **frozen copies** with provenance (service, session, time, sha256). You can add a note to each, reorder them, copy them as a review packet, download them as Markdown, export a PDF, or **Ask Codex to review** |
| Compose (`c`) | A scratchpad with live TeX preview. Copy the text and paste it into the terminal |

Keys: `/` search, `c` compose, `n` notebook, `g` latest, `j`/`k` next and
previous response, `Esc` close. Alt-click any formula to copy its TeX.

## Mathematics

KaTeX typesets the following, and the shared macros are in
[static/macros.json](static/macros.json):

- `$..$`, `$$..$$`, `\(..\)` and `\[..\]`;
- amsmath environments and `CD` diagrams;
- `` ```math `` fences;
- `\DeclareMathOperator`, honoured within a single response.

`$..$` follows Pandoc's rules, so "costs $5 and $10" stays text. The `$…$`
switch in the top bar turns single-dollar math off.

The hub's AGENTS.md asks assistants to write mathematics this way in
conversation and notes, with `\(..\)` inline and `\[..\]` displayed. Each
substantive response opens with a one-line plain-text conclusion, so the
terminal stays readable. Catalog records keep plain-text mathematics.

A formula KaTeX cannot handle, such as tikz-cd, shows its source and the
error. **Typeset with LaTeX** then compiles that one formula with your TeX
Live inside a macOS sandbox: no network, writes only in a scratch directory,
and dangerous primitives refused. The result is shown as an image.

## Pushing documents to the desk

An agent or a shell command can publish any Markdown or TeX text:

```sh
python3 harness/mathdesk.py push draft.md --title "Proof of Lemma 3"
python3 tools/hub.py show R031 | python3 harness/mathdesk.py push - --title R031
```

## Independent reviews

In the notebook, **Ask Codex to review** sends the packet of frozen excerpts
and your notes to Codex for an independent review. Only one review runs at a
time, and nothing is sent without your click and confirmation. The reply
appears under **Desk → reviews**, carrying the packet's sha256; the packet
and the reply are kept in `.hub/harness/consult/`. You can cancel a review
from the notebook.

The run is `codex exec` with these limits:

- read-only and ephemeral;
- without your `config.toml`, so no MCP servers and no user hooks;
- only on a ChatGPT login: the API-key variables are removed, and the login
  is checked before launch.

The reviewer's name, model and effort are settings (`reviewer_name`,
`consult_model`, `consult_effort` in `.hub/harness/settings.json`). Agreement
between models never becomes a status: the hub's review rules are
unchanged.

## Installation and removal

```sh
python3 harness/mathdesk.py install     # hooks for Claude Code and Codex; backups kept
python3 harness/mathdesk.py uninstall   # removes exactly those hooks
```

`install` records the current `python3` as the server interpreter. That
interpreter must be 3.11 or newer for hub lookups. It then
adds UserPromptSubmit and Stop hooks to `~/.claude/settings.json` and
`~/.codex/hooks.json`, keeping existing hooks such as the phone notification.
Codex asks you to review and trust the new hooks the next time it starts.
The hooks run `/usr/bin/python3 harness/hook.py` (any Python 3.9+ works),
ignore sessions outside the hub, never print, and always exit 0.

What MathDesk needs:

- Python 3.9+ for the hook, and 3.11+ for the server;
- a Chromium browser (Chrome, Brave or Chromium) for PDFs;
- TeX Live with `latex` and `dvisvgm` for diagrams KaTeX cannot draw;
- macOS `sandbox-exec` for the LaTeX sandbox.

Each optional piece is reported by `doctor` and degrades gracefully: without
it, the matching button explains what is missing.

## Privacy and safety

- Everything stays on this computer. The server listens only on 127.0.0.1
  and checks the Host header.
- Data routes require a bearer token, which is stored in `.hub/harness/token`
  with mode 0600. `open` hands it to the page in the URL fragment (never sent
  over HTTP), and the page keeps it in origin-scoped storage. No cookies are
  used, because cookies are not scoped by port.
- Cross-origin requests are refused, and nothing is fetched from a CDN.
- Model output is sanitized: raw HTML is shown as text, remote images are
  blocked by the content security policy, and KaTeX runs with `trust: false`.
- Files are served only from the hub and its registered archive roots. Hub
  data is read-only.
- Runtime state lives in `.hub/harness/`: the spool of captured turns, PDFs,
  pins, the TeX cache and logs.

## Layout

`hook.py` handles capture. `mathdesk.py` is the CLI. `core/` holds the
server, spool, transcripts, hub data, export, consult and config modules.
`static/` holds the page; the vendored libraries are pinned with their hashes
in `static/vendor/VERSIONS.txt`. `tests/` holds the tests. The design is in
[DESIGN.md](DESIGN.md).

Run the tests with `python3 harness/tests/test_core.py`, the same for
`test_transcripts.py`, `test_hubdata.py` and `test_export.py`, and
`node harness/tests/mathsplit.test.js`. Chrome, LaTeX and macOS tests skip
when their tool is missing.

## Third-party code

The page bundles [KaTeX](https://katex.org) 0.19.0 (MIT),
[marked](https://marked.js.org) 18.0.14 (MIT) and
[DOMPurify](https://github.com/cure53/DOMPurify) 3.4.16 (Apache-2.0 or MPL-2.0),
unmodified. Their licences are in `static/vendor/`, and their versions and
hashes are in [static/vendor/VERSIONS.txt](static/vendor/VERSIONS.txt).
MathDesk itself is MIT-licensed with the rest of this kit.
