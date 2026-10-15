# {{HUB_NAME}}

Created {{DATE}}. Pick a project; its guide holds the current state of the
research.

<!-- projects:start -->
<!-- projects:end -->

Rules for AI sessions are in [AGENTS.md](AGENTS.md), the record-keeping
contract is in [WORKFLOW.md](WORKFLOW.md), and the proof-team rules are in
[PROOF_SWARM.md](PROOF_SWARM.md). Ready-to-paste requests for the owner are
in [PROMPTS.md](PROMPTS.md). The readable catalog for each project is
`projects/NAME/indexes/CATALOG.md`, and the overview is
[indexes/OVERVIEW.md](indexes/OVERVIEW.md).

```
python3 tools/hub.py context --project NAME     # what to read first
python3 tools/hub.py search "phrase" --project NAME [--archives]
python3 tools/hub.py show ID                     # one record and what cites it
python3 tools/hub.py impact ID                   # what depends on it
python3 tools/hub.py new-note "topic" --project NAME
python3 tools/hub.py check --project NAME        # catalog consistency
python3 tools/hub.py render                      # rebuild readable indexes
python3 tools/hub.py doctor                      # check the installation
```

hub.json is the complete list of projects and read-only archives. Add a
project with `hub.py add-project` and a folder of earlier work with
`hub.py add-archive`.
