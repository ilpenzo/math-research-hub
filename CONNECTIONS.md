# Research memory and returning later

This release works with any mathematical subject. Each project supplies its own
question, conventions, objects and controls. Installation creates empty catalogs;
the examples below name a hypothetical project, `groups`, with prefix `FG-`.

## A normal research session

Start with the project's instructions and prior work:

```sh
python3 tools/hub.py context --project groups
python3 tools/hub.py search "your phrase" --project groups
python3 tools/hub.py recall-index refresh --no-semantic
python3 tools/hub.py recall "your missing implication" --project groups
```

The explicit refresh indexes current catalogs and permitted documents. It does
not scan every archive during ordinary queries. `recall-index manifest --help`
explains how to select additional archive/literature routes. Coverage output
distinguishes indexed material, gaps and deferred routes. An empty search never
establishes novelty. Exact source text and locators remain available for review.

Use `templates/knowledge.json` to record an anchored Gap, then publish it:

```sh
python3 tools/hub.py remember gap.json --project groups --owner session
python3 tools/hub.py brief --project groups --gap FG-K001
python3 tools/hub.py compare FG-R001 FG-R002 --project groups
python3 tools/hub.py graph neighbors FG-R001 --project groups
```

Use the ID returned by `remember`; `FG-K001` is illustrative. IDs in comparisons
must already exist. Research aids have five kinds: Gap, move, Episode, Connection
and reminder. Their inspection assessments are separate from theorem truth,
proof review and novelty. `brief --task "question"` is an ephemeral alternative
that does not silently select or revise a saved Gap.

The assistant develops a small number of proposed Connections, checks the exact
objects/maps and missing hypotheses, and records a finite check as an Episode.
Rejected or unresolved combinations remain useful memory. Complete aid updates
retain the same ID and require `expected_revision`. Save a checking Episode's
`payload.inspected_input_fingerprint` from the brief's `inspection_fingerprint`.

```sh
python3 tools/hub.py frontier --project groups
python3 tools/hub.py frontier --project groups --defer ITEM-ID --reason "Missing an actual map"
python3 tools/hub.py unlocks "hypothetical missing lemma" --project groups
```

Defer/dismiss decisions survive index rebuilds. Relevant new inputs can bring a
suggestion back. A match or hypothetical unlock is a reason to inspect, never
proof that a hypothesis holds. Commands offer `--json` for structured output.

## Return in a new conversation

The installer registers a fresh workspace identity. The assistant reads this
entry route at the start of each conversation:

```sh
python3 tools/hub.py context --resume --service codex --query "your current request"
# Claude Code:
python3 tools/hub.py context --resume --service claude-code --query "your current request"
```

From a project directory, use `python3 ../../tools/hub.py`; the project is
inferred from the directory. `--project NAME` narrows routing, and `--thread ID`
selects an actual native thread. Otherwise saved same-service contexts are ranked
by the current request and project, with at most three choices for ambiguity.
Selecting a context does not bind a new conversation or restart old work.

Adapt `templates/session.json` and save direction, constraints, decisions,
result statuses, unresolved work and the next proposed action during work:

```sh
python3 tools/hub.py session checkpoint session.json --service codex --owner session
```

Updates need the returned ID, `expected_revision` and the complete payload.
Use `project: hub` for infrastructure. Supply an actual `native_thread` when
known; an unknown native identity remains explicitly unbound. Saved contexts
are local private data, excluded from this public repository.

`session refresh --service SERVICE` discovers bounded workspace-matching native
metadata. Adding `--thread ID` reads a bounded excerpt for parent inspection.
Do not assert full-history capture from an excerpt. The packet reports canonical
correction/input freshness separately from native-history capture. Missing or
changed native formats leave saved checkpoints usable. Ordinary saved entry
does not start an embedding model or scan complete transcripts.

After intentionally moving a hub, run `register-workspace --move`; for an
independent copy, use `register-workspace --new-identity`. A mismatch isolates
old contexts until this explicit choice. Do not share a private hub merely by
pushing the directory: see [PRIVACY.md](PRIVACY.md).

## Optional local semantic retrieval

Lexical retrieval and saved-context recovery work without additional Python
packages or a model. For semantic retrieval, install Ollama separately, keep its
server on localhost, and install a small embedding model such as embeddinggemma:

```sh
ollama pull embeddinggemma
python3 tools/hub.py enable-semantics --model embeddinggemma
python3 tools/hub.py recall-index refresh --semantic
python3 tools/hub.py recall-index status --json
```

`enable-semantics` verifies the installed local artifact and pins its digest;
it does not download software or contact a hosted inference API. A model change
requires explicit reconfiguration and refresh. NumPy is an optional acceleration
for vector ranking. PDF extraction requires `pdftotext`. Provider failures and
query timeouts produce an explicit lexical fallback. Large refreshes can take
minutes and exceed 512 MiB; ordinary queries reuse complete index generations.

The installer does not bundle a model, machine paths, workspace identity from
another installation, research aids, or someone's measured benchmark corpus.
Run tests and measure latency on your own material before relying on a target.
