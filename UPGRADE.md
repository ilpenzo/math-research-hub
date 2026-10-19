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

For a future update to a version 2 hub, back up your current tool and configuration
first. Update the complete `tools/` package together, review schema/release notes,
and preserve your catalogs, knowledge, workspace identity, rules and archives.
Copying the public skeleton over a working hub is not an upgrade procedure.
