# math-research-hub

A research hub for mathematicians: one folder where AI assistants
([Claude Code](https://code.claude.com/docs) and/or
[Codex](https://github.com/openai/codex)) work on your research under rules
that keep the record honest. They search before inventing, check citations
against the papers, remember failed attempts, and send every claimed proof to
a reviewer that did not write it.

**New to AI tools? Read the guide: [ARTICLE.md](ARTICLE.md).**

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

You need Python 3.9+ and a paid plan for at least one assistant: Claude Pro,
Max, Team or Enterprise (or an Anthropic API account) for Claude Code; a
ChatGPT plan that includes Codex (or an OpenAI API key) for Codex.

## What you get

| | |
|---|---|
| `AGENTS.md`, `CLAUDE.md` | the rulebook both assistants load every session |
| `WORKFLOW.md` | the catalog contract: status, evidence level and novelty kept separate |
| `PROOF_SWARM.md` | a proof conductor plus five read-only helpers: archive scout, source checker, proof explorer, counterexample checker, proof checker |
| `PROMPTS.md` | ready-to-paste requests for everyday work |
| `projects/NAME/` | per-project rules, guide, topic map, changelog, catalog and one NOTE per investigation |
| `tools/hub.py` | one standard-library Python file: validated catalog writes, search across notes, archives and PDFs, dependency impact, evidence hashes, readable indexes, computation packages |
| `.claude/`, `.codex/` | helper roles for both assistants, Claude Code permissions protecting the catalog and archives, the math-paper-writing skill |

## Repository layout

- `install.sh`: the installer
- `template/`: the hub skeleton that the installer copies
- `ARTICLE.md`: the plain-English guide
- `tests/test_install.sh`: offline installer tests: unattended, the
  `curl | bash` path, the Codex trust entry, and (with `expect`) a full
  interactive run through a terminal, plus the tool's self-tests. They work in
  a temporary folder and never touch your settings.
- `tests/live_check.sh`: optional; asks the installed assistants (one short
  read-only call each, using your login) whether a fresh hub's rules, helper
  roles and paper skill load.

Run `bash tests/test_install.sh`, and `bash tests/live_check.sh` after
updating Claude Code or Codex.

## After installing

Everything in the hub is yours to edit, especially the rules. To use a newer
version of the tool, copy `template/tools/hub.py` from this repository over
your hub's `tools/hub.py` and run `python3 tools/hub.py check`.

## License

MIT. See [LICENSE](LICENSE).
