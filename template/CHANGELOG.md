# Hub changelog

Changes to the hub itself (tools, rules, configuration). Research changes go
in each project's CHANGELOG.md. Newest first.

## {{DATE}}

- Hub created with math-research-hub.

- Prospector search upgrade, variant ladder: a `variants` stage proposes
  easier neighbours of the gap; `plan` works on the easiest open ones first;
  solved variants keep a hashed complete proof with a review scope, and those
  proofs and survivors' full sketches are inlined whole or omitted with a
  note. Nothing from Prospector is a verified result.

- Prospector search upgrade, deep dig: `plan --stage dig --depth deep` gives
  one slot sustained work with a running scratch file, free computation and
  a full derivation, with no time or word target and no record-keeping
  duties. Claude stays tool-free unless `deep_dig.claude_tools` is set in the
  run file.

- Prospector search upgrade, mechanism search: a conductor step before
  prospecting restates the obstruction in field-neutral terms, searches
  across fields, and brings proof excerpts with exact locators into the
  prompts. Queries are screened for copied hub text and logged with their
  results; sources stay leads until checked.

- Prospector search upgrade, executable kill tests: claim cards carry a
  `kill_script` (language, source, expected outcome if true) and assay
  verdicts carry its result and raw output. Falsifiers run scripts in their
  own sandbox; `prospector.py` never executes model-written code.

- Connection-workspace release: modular publication kernel, scoped lexical/local
  semantic recall, graph/comparison, research aids and Gap briefs, durable frontier
  choices and same-service saved-context recovery. Fresh installation contains
  no research results or imported conversations.
