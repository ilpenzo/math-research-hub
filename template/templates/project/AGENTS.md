# {{TITLE}}: project rules

The hub's root AGENTS.md governs this project; these rules add to it.
Project folder: `projects/{{SLUG}}/`. Record IDs: `{{PREFIX}}-R001`, ...;
literature IDs: `{{PREFIX}}-L001`, ....

## Goal

{{DESCRIPTION}}

## Mathematical conventions

Every statement, proof, control and helper brief in this project keeps these
explicit. Helpers read this section.

{{CONVENTIONS}}
- A bounded search or finite computation is evidence about its stated cases,
  not a theorem.

## Success threshold for timed work

(The owner states what counts as substantive success, for example "a complete
proof that passed an independent review" or "a checked counterexample". Until
this is filled in, finishing planned jobs is never a reason to stop early.)

## Searching

`python3 tools/hub.py search "phrase" --project {{SLUG}}`; add `--archives`
to include this project's registered archives and `library/`.

New investigations go in `projects/{{SLUG}}/investigations/DATE-topic/NOTE.md`
(created with `python3 tools/hub.py new-note "topic" --project {{SLUG}}`).
