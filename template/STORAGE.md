# Storage and maintenance

Run these commands from an installed hub.

ResearchHub stores research and history permanently while bounding disposable
search caches. Small session edits do not copy the bodies of historical frontier
checkpoints. All publication still uses the kernel gate and recoverable journal.

## Storage layout

| Data | Location | Policy |
|---|---|---|
| Claims, literature, aids and session summaries | Project `catalog/` | Canonical; retained |
| Frontier checkpoint metadata | `catalog/knowledge.json` | IDs and historical selection retained |
| Immutable checkpoint bodies | Project `catalog/frontier/` | Canonical; include in backups |
| Transaction audit metadata | `.hub/transactions/ID/journal.json` | Retained |
| Compressed transaction payloads | `.hub/transactions/blobs/` | Shared by content hash; retained |
| Search index generations | `.hub/recall/generations/` | Current and preceding generation, plus active readers |
| Parsed text and embeddings | `.hub/recall/cache/` | Disposable; evicted within a byte budget |

Checkpoint bodies preserve the complete input manifests, items, locator states
and unresolved diagnostics. Interned structures share complete values; semantic
freshness fingerprints are not used as storage identities. Bodies are immutable
and checked by digest. Full snapshots and `show` expand them when requested.

New journals use schema version 2 and store each payload once by SHA-256 of the
original bytes. Recovery supports legacy raw journals and compressed journals,
checks both stored and decoded digests/lengths, and refuses later or foreign edits.
Null payloads represent absent files; empty files remain distinct. Committed
transactions cannot be rolled back with `recover`.

Python 3.14 uses standard-library zstd. Earlier supported Python versions use
the optional `zstandard` package when available, otherwise standard-library XZ.
Both representations are lossless. To read zstd data on Python 3.11–3.13, install
`zstandard` in that Python environment, or use Python 3.14+. Keep the required
decoder available when moving a hub or changing Python environments. No model,
network service or compression executable is required.

## Routine use

Use a small concurrency-base snapshot for publication work:

```
python3 tools/hub.py snapshot --project NAME --hashes-only
python3 tools/hub.py storage status
```

`snapshot --full` explicitly exports the project's records, sources and expanded
knowledge. The old no-flag full export remains supported for compatibility;
workflows should use `--hashes-only` unless the full content is needed. Existing
investigation snapshots are not rewritten.

`storage status` is read-only JSON. It reports logical and filesystem-allocated
bytes, catalogs, bodies, journal formats, new blob bytes over 24 hours, free space,
budgets and warnings. Allocated sizes do not establish exclusive APFS ownership.
The 24-hour blob figure includes deliberate migrations.

Defaults are a 1 GiB generation budget, a separate 1 GiB parsing/embedding cache
budget, and a 5 GiB free-space reserve for ordinary publication. Override selected
fields through a normal hub-scoped batch for `catalog/storage-config.json`:

```
{"schema_version": 1, "generation_budget_bytes": 1073741824,
 "cache_budget_bytes": 1073741824, "free_reserve_bytes": 5368709120}
```

Warning defaults are 20 MiB per live catalog, 50 MiB per planned uncompressed
transaction, 2 GiB of compressed journal blobs and 250 MiB of new blobs per day.
The other field names are `catalog_warning_bytes`, `transaction_warning_bytes`,
`journal_warning_bytes` and `daily_growth_warning_bytes`. Limits are positive
integers; the reserve may explicitly be zero. No warning silently prunes history.

Refreshes collect unused generations and trim caches automatically. Readers pin
generations until their query finishes. In-progress database and metadata output
count toward the generation budget; a refresh that cannot fit keeps the selected
index and reports failure. Large parse/embedding values can be used without
caching them. A failed build cannot become the selected generation.

## Upgrading existing storage

Update the complete tools package together and finish running hub commands before
switching versions. Preserve customized rules, catalogs, archives and all hidden
history. Do not install a fresh public skeleton over an existing hub. A backup
must include the canonical `catalog/frontier/` directories and `.hub/transactions/`
if historical transaction payloads are to be retained.

Catalogs containing external checkpoint bodies use knowledge-envelope schema 2.
New readers accept schemas 1 and 2; old readers reject schema 2 rather than
silently ignoring saved frontier inputs. Restart long-running tools that import
the hub modules after upgrading, and do not downgrade just one module.
If MathDesk is installed, update `harness/core/hubdata.py` with the tools and
run `python3 harness/mathdesk.py restart`. Its search queries use the same
reader protection without creating files or changing hub data.

The following operations have a read-only inspection mode by default. Review
their output, then use `--apply` for the intended maintenance operation:

```
python3 tools/hub.py storage compact-journal
python3 tools/hub.py storage compact-journal --apply
python3 tools/hub.py storage verify-journal
python3 tools/hub.py storage migrate-checkpoints
python3 tools/hub.py storage migrate-checkpoints --apply
python3 tools/hub.py storage collect-cache --apply
python3 tools/hub.py check
python3 tools/hub.py render
python3 tools/hub.py storage status
```

Compaction verifies every legacy payload, stages compression outside the global
gate, then atomically switches one journal's descriptor before removing its
redundant raw copies. It is resumable, including interruption between that switch
and cleanup. Pending transactions are not converted; inspect and recover them
first. Their blobs always remain protected. Repeated compaction retains the same
history. `migrate-checkpoints --project NAME` can narrow the canonical migration;
it preserves each ID and advances each converted entry's revision once.

`storage collect-orphans` reports compressed blobs referenced by no journal;
`--apply` removes only those orphan blobs. All committed, rolled-back and pending
journals participate in reference checking. It never prunes retained history or
canonical checkpoint objects. `collect-cache` touches only disposable recall
storage. Neither command deletes archives or investigation evidence.

Ordinary writes check peak blob, live-file replacement and recovery space before
publication. Other programs can still exhaust the disk afterward. If publication
is interrupted, preserve the journal and use the existing `recover` workflow.
Recovery and verified compaction may use the normal reserve to restore service;
they still require enough space to write their temporary files safely. Never
delete a lock file as a recovery action.

Retaining all research and history permits gradual growth. Compression and shared
objects remove repeated-copy amplification; cache budgets bound generated storage.
A permanent total-size cap would require a separate history archival or retention
decision.
