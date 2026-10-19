# math-research-hub

A research hub for mathematicians: one folder where AI assistants
([Claude Code](https://code.claude.com/docs) and/or
[Codex](https://github.com/openai/codex)) work on your research under rules
that keep the record honest. They search before inventing, check citations
against the papers, remember failed attempts, and send every claimed proof to
a reviewer that did not write it.

**New to AI tools? Start with the guide:
[Build your own research hub](https://ilpenzo.github.io/math-research-hub/).**

## Install (macOS, Linux, or Windows with WSL)

```
curl -fsSL https://raw.githubusercontent.com/ilpenzo/math-research-hub/main/install.sh | bash
```

or download this repository and run `bash install.sh`. The installer asks
about ten questions (your name, where the hub goes, your first project, the
conventions of your field, folders of earlier work, which assistants) and
creates one folder. It offers to install Claude Code and Codex with their
official installers, and pdftotext on a Mac with Homebrew. It changes nothing
else and uploads nothing.

You need Python 3.11+ and access to at least one supported assistant. Core catalog,
lexical retrieval and saved-context commands use the Python standard library.
Local semantic retrieval is optional; see [the connection guide](CONNECTIONS.md).

## Returning to work

In a new conversation, ask to return to your earlier investigation. The hub
recovers saved direction, decisions, results and next steps within the same
service, checks current corrections, and reports capture freshness. Old goals
are never restarted automatically. See [CONNECTIONS.md](CONNECTIONS.md) for
research memory, optional local embeddings and checkpoint setup.

## What you get

| | |
|---|---|
| `AGENTS.md`, `CLAUDE.md` | the rulebook both assistants load every session |
| `WORKFLOW.md` | the catalog contract: status, evidence level and novelty kept separate |
| `PROOF_SWARM.md` | a proof conductor plus five read-only helpers: archive scout, source checker, proof explorer, counterexample checker, proof checker |
| `PROMPTS.md` | ready-to-paste requests for everyday work |
| `projects/NAME/` | per-project rules, guide, topic map, changelog, catalog and one NOTE per investigation |
| `tools/hub.py` | modular tools: validated transactions, scoped search, local semantic recall, graph/comparison, Gap briefs, durable research memory, saved conversation recovery, literature and computation packages |
| `.claude/`, `.codex/` | helper roles for both assistants, Claude Code permissions protecting the catalog and archives, the math-paper-writing skill |

## Repository layout

- `install.sh`: the installer
- `template/`: the hub skeleton that the installer copies
- `docs/index.md`: the plain-English guide, published at
  https://ilpenzo.github.io/math-research-hub/
  (PDF: `docs/research-hub-guide.pdf`; after changing the page, rebuild it
  with `bash docs/build-pdf.sh` once the page is live)
- `tests/test_install.sh`: offline installer tests: unattended, the
  `curl | bash` path, the Codex trust entry, and (with `expect`) a full
  interactive run through a terminal, plus the tool's self-tests. They work in
  a temporary folder and never touch your settings.
- `tests/live_check.sh`: optional; asks the installed assistants (one short
  read-only call each, using your login) whether a fresh hub's rules, helper
  roles and paper skill load.

Run `python3 -m unittest discover -s template/tools -p 'test_*.py'` and
`bash tests/test_install.sh`. The optional live check uses your assistant account
and requires your explicit choice; it is not part of offline tests.

## After installing

Everything in the hub is yours to edit, especially the rules. This release uses
a modular tool and a version 2 manifest: **do not replace only `hub.py` in an
older hub**. Read [UPGRADE.md](UPGRADE.md) before updating an existing installation.

The public kit contains infrastructure and synthetic tests, with empty research
catalogs created during installation. It contains no personal research results,
retained papers, conversations, machine-specific settings or embedding caches.
See [PRIVACY.md](PRIVACY.md).

## License

MIT. See [LICENSE](LICENSE).
