# Updating an existing hub

## Prospector search update

Copy `template/tools/prospector.py`, `template/tools/test_prospector.py`,
`template/PROSPECTOR.md` and the complete `template/templates/prospector/`
directory together into an existing version 2 hub; the tool and its templates
must match. Existing run directories and `run.json` files keep working
unchanged: every new setting has a built-in default, and behaviour that
changes selection or permissions is off unless a run file asks for it.

- Variant ladder. New files `variants.md` and `variants.schema.json`; new
  run settings `variant_ladder` (`prefer_open`, `inline_solved`,
  `max_per_slot`) and `default_slots.variants`. A run with no variants plans
  and prompts exactly as before. Prompts now also carry survivors' full
  sketches, counted against `max_inline_chars` after named context files.
- Selection. Default card choice for assay and dig now ranks recorded
  evidence before confidence in every run, old or new; pass `--cards` to
  choose yourself. Lens weighting needs the new run setting `lens_selection`
  (`mode`, `seed`, `explore_slots`); a run file without it keeps the library
  order.
- Kill scripts. `card.schema.json` gained a required `kill_script` on cards
  and `kill_script_result` on verdicts, so it must be copied together with
  the tool. Cards and verdicts recorded before the change are read as having
  no script and no result.
- Mechanism search. New file `mechanism-search.md`; new run setting
  `mechanism_search.refuse_shared_words`. `frontier.md` and the
  `analogy-transfer` and `literature-mechanism` lenses in `lenses.json`
  changed wording; if you edited your copies, merge by hand.
- Deep dig. New file `dig-deep.md`; new slot field `depth` and run setting
  `deep_dig` (`claude_tools`, `claude_tool_names`, `claude_max_turns`). With
  `claude_tools` absent or false, every Claude command is the same as before.
  Before turning it on, run `claude --help` and confirm `--restricted`,
  `--tools`, `--permission-mode`, `--permission-prompts` and `--settings`
  are listed; PROSPECTOR.md's Safety section says what is enforced.

## Prospector discovery update

This update adds `template/PROSPECTOR.md`, `template/tools/prospector.py`, its
offline tests and the complete `template/templates/prospector/` directory.
Copy these together into an existing version 2 hub, and reconcile the discovery
guidance in AGENTS, CLAUDE, WORKFLOW, PROOF_SWARM and the investigation/helper
templates with your own rules. Keep your research notes and catalogs intact.

New run files explicitly use `high` for prospect/assay and `xhigh` for selected
dig work in both backends. Override `effort_by_stage` in the run or `effort` in
an individual slot deliberately. Existing run files retain their settings;
neither global CLI settings nor old runs are migrated. Use `run --dry-run`
to inspect prompts and effective effort without launching or spending calls.
Tool-free context must be complete; replace an unedited hub seed and remove
retrieval instructions before dispatch. The first-return time target is soft;
proof calls have no timeout unless one is explicitly configured.

Update MathDesk's `harness/` package together for compatible worker filtering,
then restart it with `python3 harness/mathdesk.py restart`. Existing worker
captures remain on disk but are hidden from conversation and PDF routes.
The installer still creates a fresh hub; it does not overwrite a live one.

## Earlier releases

The connection-workspace release uses Python 3.11+, multiple Python modules and
a version 2 manifest. The old instruction to replace just `tools/hub.py` no
longer applies. The installer creates a new folder and refuses a nonempty one.

For a hub created by the earlier single-file kit:

1. Keep the existing hub intact, including hidden history and your customized
   rules. Save a separate backup before any migration.
2. Install this release into a new folder. Choose your own project and
   conventions. You can register the old hub as a read-only archive.
3. Inspect and import selected records/evidence through the validated `put` or
   batch `apply` commands. Preserve IDs, evidence bytes, locators, exact statuses
   and review scopes. Use project prefixes matching the original IDs. New
   questions, obstructions and corrections use Q, O and C namespaces.
4. Reconcile customized rules deliberately. Run `check`, `render`, and a small
   recall/session-recovery exercise before switching your daily workflow.

This release does not silently convert an existing research corpus or overwrite
its rules. An automatic in-place version 1 migration is not provided. The old
release remains accessible in GitHub history. Returning later begins with the
new installation's own checkpoints; private native history is never uploaded
or transferred automatically.

MathDesk (`harness/`) is self-contained. To add it to an existing version 2
hub, copy the kit's `template/harness/` folder into the hub as `harness/`,
then run `python3 harness/mathdesk.py install`. It reads the catalogs and
never writes them. `python3 harness/mathdesk.py uninstall` removes exactly
its own hooks. The kit's AGENTS.md now asks for TeX in conversation and notes,
in place of the earlier plain-text rule. To adopt this in an existing hub,
replace your "Write mathematics in plain text" bullet with the kit's
"Write mathematics as TeX" bullet.

For a future update to a version 2 hub, back up your current tool and configuration
first. Update the complete `tools/` package together, review schema/release notes,
and preserve your catalogs, knowledge, workspace identity, rules and archives.
Copying the public skeleton over a working hub is not an upgrade procedure.


## Storage update for version 2 hubs

Back up the current tools/configuration and stop running hub commands before
switching the complete tools package. Preserve your data and customized rules.
The installer is for new hubs; do not copy the entire skeleton over an existing
hub. Install new helper modules before replacing their existing callers, or
replace the tools directory while hub commands are stopped. Restart long-running
processes that import the tools. If MathDesk is installed, also update
`harness/core/hubdata.py` and run `python3 harness/mathdesk.py restart` so its
search queries protect active index readers during collection.

Then follow the inspect/apply sequence in [STORAGE.md](STORAGE.md): compact and
verify the journal, migrate checkpoint bodies, collect disposable caches, and
run check/render. Journal schema 2 and knowledge-envelope schema 2 are explicit
format changes; new readers support old data, while old knowledge readers reject
external-body catalogs. Full snapshots expand bodies when needed. Backups must
include `catalog/frontier/` and, for complete transaction history,
`.hub/transactions/`. Changing Python runtimes may require the optional
`zstandard` decoder for previously written zstd data.

Adopt `snapshot --hashes-only` in your publication workflow, and retain full
exports only when required. Review the new storage guidance when reconciling
AGENTS.md and WORKFLOW.md. None of these commands prunes retained research or
committed transaction history.
