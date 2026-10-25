# Updating an existing hub

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
