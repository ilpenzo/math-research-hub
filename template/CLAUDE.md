@AGENTS.md

# Notes for Claude Code

At entry run `python3 tools/hub.py context --resume --service claude-code
--query "CURRENT REQUEST"` and inspect saved direction, results and freshness.
From a project directory use `../../tools/hub.py`. Save session checkpoints as
directed by AGENTS.md; do not fabricate a native thread ID.

- The helper roles are in `.claude/agents/`: archive-scout, source-checker,
  proof-explorer, counterexample-checker and proof-checker. Start them by
  name, never as a fork: a named agent starts without this conversation, so
  its review is fresh. Give each one only the brief from
  `templates/subagent-task.md`.
- Claude Code runs the economy profile in PROOF_SWARM.md unless the owner
  asks for more: at most three helper jobs per request, one kept for review,
  and at most two running at once.
- Helpers are read-only: their role files omit the edit and write tools. A
  computation that must write files goes to a general-purpose agent with one
  named folder to write in.
- Read PDFs with page ranges or `pdftotext`, never all at once.
- Use the Workflow tool, scheduled agents or other background launches only
  when the owner asks for them.
- Auto memory is for the owner's preferences and workflow feedback. Research
  facts and statuses belong in the catalog and NOTEs.
