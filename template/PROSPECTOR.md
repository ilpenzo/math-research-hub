# Prospector

Prospector is ResearchHub's proof-search system. It is built to find proofs
and constructions; [PROOF_SWARM](PROOF_SWARM.md) certifies them. A run makes
many cheap, deliberately different attempts at one gap, kills weak ideas
quickly, develops the survivors and hands a complete candidate to
PROOF_SWARM's frozen fresh review.

Nothing Prospector produces is a verified result. Its cards are leads, like
recalled theorems: check them before use.

## Why it is separate

PROOF_SWARM runs one bounded attempt at a time and spends most of its
assignments on retrieval and review. Those rules protect certification but
starve search. Prospector optimizes the chance that some idea works:
breadth, forced diversity of strategy and model, computation inside the
attempt, fast falsification, and feeding survivors and failures forward.
Rigor is concentrated at the end, on the one candidate that deserves it.

## Authorization and budget

- A run needs a user request naming the target or Gap. Quote it in
  `run.json` under `authorization`. Historical runs never authorize new ones.
- The budget lives in `run.json`: completed calls, Claude calls, Claude calls
  per round and concurrency default to 24, 6, 2 and 3. Proof calls have no
  time limit by default: `budget.timeout_seconds` is `null` (omitting it also
  disables the deadline). Only an explicitly configured positive number
  imposes a timeout; zero and negative values are invalid. The tool refuses
  to exceed call limits. Failed and timed-out calls are logged but not
  counted. Raise a budget only on the user's instruction and append an entry
  to `budget_changes`. The connectivity check `doctor --live` separately
  retains its 300-second timeout.
- Prospector calls do not debit PROOF_SWARM assignments. Certifying a
  candidate uses PROOF_SWARM's review rules and reserve.
- Models: Codex inherits the user's Codex model unless `run.json` sets it.
  New runs set effort explicitly by stage as described below; legacy runs
  without an effort setting retain CLI inheritance. Claude defaults to Opus
  with no tools, on idea-heavy lenses.
  Codex handles computing slots; each run records its backend allowances.
- The run's authorization covers sending FRONTIER.md and the named context to
  the configured backends only within the owner's sharing authorization.
  Exclude credentials and unrelated private data.

## The loop

### Discovery cadence

Discovery defaults shorten the interval from an idea to the next
mathematical decision. These are discovery defaults; an explicitly assigned
full proof or certification keeps its required depth.

- **One move per assignment.** Name one object, map, construction or calculation
  and the decision it will enable. Supply about 600 words of context plus only
  the necessary evidence sections. State accepted inputs and their review
  scopes once. Do not bundle mechanism invention, all-sector coverage, a control
  suite and a polished report into every first attempt.
- **Choose effort explicitly.** New Codex and Claude runs use `high` for
  prospect and assay, and `xhigh` only for a selected dig. These are
  explicit, configurable defaults; mathematical quality equivalence is unmeasured. Compare
  useful deductions, correctness and repair burden alongside wall time.
  `backends.BACKEND.effort_by_stage` supplies stage settings; an explicit slot
  `effort` overrides them, then legacy backend `effort` is the fallback. Dry
  runs show the effective setting and receipts retain it. Existing run files
  and global CLI configurations are unchanged. Frozen certification keeps its
  separately authorized settings; an assay is not certification.
- **Prepare usable inputs once.** The conductor retrieves relevant results,
  corrections and failed attempts before dispatch. Write each obstruction as
  a complete sentence containing hypotheses and its precise conclusion; IDs
  are provenance. Tool-free briefs include every needed statement inline and
  contain no instructions to search or open files. `run --dry-run` rejects
  truncated tool-free context, unedited hub seeds and obvious retrieval
  directives; this catches preparation errors, not mathematical completeness.
  Tool-enabled helpers use only named inputs, search only for a specific
  blocking fact, and report that fact, query and result. Do not repeat hub
  entry/routing or read the full NOTE in every slot.
- **Return useful mathematics early.** Aim for 5–10 minutes to a rough derivation
  or exact blockage; this is a soft planning target, not a deadline. A writable
  helper writes `FIRST_RETURN.txt` once, atomically, in its assigned work
  directory (about 100–250 words; no placeholder), then finishes the same bounded
  task. A tool-free helper returns one concise structured result directly.
  Longer promising work can continue with its early result already available.
- **Use short, adaptive waves.** New runs default to two prospects and one card
  per slot. Breadth comes from distinct short attempts; only selected survivors
  receive deeper development. Existing run settings and explicit allocations
  take precedence. A new result or user steering can replace the next queued
  question; no need to finish an obsolete broad wave. Never silently retask a
  running worker; genuinely new follow-up work still consumes its allowance.
- **Keep the parent doing mathematics.** Work on the central derivation while
  helpers answer complementary bounded questions. Inspect their decisive
  reasoning promptly, combine actual maps, and rewrite the frontier before the
  next dependent dispatch. Early notes are provisional and never automatically
  fed to another worker or promoted to a claim.
- **Keep current mathematics on one page.** Maintain a roughly 500–700-word
  `Current mathematics` section near the top of the existing investigation
  NOTE: exact objects/maps, established deductions and review scope, candidate
  connection, decisive controls, one next question and evidence pointers.
  Rewrite it when the mathematical position changes; keep historical sections
  and frozen evidence intact. Read this section first, then only evidence
  needed for the current decision. Supply selected sections to helpers.
- **Compute before commissioning when useful.** Spend a few minutes of parent
  time on one predictive calculation in an actual control, using Macaulay2 or
  another suitable tool, before requesting a broad intuition report. Preserve
  original maps and hypotheses; for bundles, null-correlation or a charge-two
  instanton can test a specific restriction/jump calculation. Record what the
  computation should distinguish and delegate the generalization it suggests.
  This is a selective practice, not a mandatory control suite or new campaign.
- **Check selectively and record lightly.** Use one cheap control aimed at the
  vulnerable inference when helpful. Retain raw input/source/output and a short
  delta in the existing NOTE. Batch routine checkpoints, publication and source
  cataloguing at substantive decisions and closeout. Preserve already-consulted
  literature; defer comprehensive audits and artifact packages to serious
  survivors. Report meaningful discovery changes without repeating that the
  ultimate proof is unfinished.

`status` surfaces captured first returns while a helper is running. Read the
note, then record the mathematical decision with:

```
python3 tools/prospector.py decide RUN --round 1 --slot s1 --action develop \
  --reason "The calculation identifies the missing normal twist" \
  --next-question "Construct the resulting comparison map"
```

Actions are `develop`, `revise`, or `park`. This records evidence and latency;
it does not launch/cancel workers, alter budgets, change card status or rewrite
the frontier. Use `mark` separately for a card disposition. The observed note
arrival and the first conductor decision per attempt are separate measurements;
neither automatically measures mathematical quality. Tool-free slots use their
final response as the first available return. Compare decision latency and
usefulness at the next authorized session, not report count. No speedup is
assumed from shorter prompts alone.

**0. Frame.** Work on one Gap; create it with `hub.py remember` if needed.
`init` seeds FRONTIER.md from `hub.py brief`. Cut it to one page: exact
target, the one gap attacked now, obstructions and failed routes (one line
each, with the hypotheses that make them bite), useful results, controls,
and the best current ideas. FRONTIER.md is the only context every explorer
shares, so its quality bounds the run's quality. Rewrite it between rounds;
do not append history.

**0b. Mechanism search (optional, before prospecting).** Look across fields
for a proof that overcame an obstruction of the same shape, and give
explorers the proof itself. See [Mechanism search](#mechanism-search).

**1. Prospect.** `plan` assigns distinct strategy lenses, unused ones first,
from `templates/prospector/lenses.json`. Idea lenses go to Claude and
computing lenses to Codex, which may run Macaulay2, Sage and Python in its own
work directory. Edit `slots.json` (lens, backend, up to three named file
sections, a note), then `run`. Explorers do not see each other. From the
second round they see survivors, one-line kills and pending titles, except
for blind slots such as the wild card. Survivors also arrive with their full
statement and sketch, each whole or omitted with a note (see
[Variant ladder](#variant-ladder)). Each returns one claim card by default
(the run may explicitly allow more):
statement, role in the gap, sketch, weakest step, fastest kill test,
evidence, what is new, confidence and cost. A card also carries a
`kill_script` when a finite computation could refute it: the language, the
source and the outcome expected if the claim is true. It is empty when no
finite test exists.

**2. Assay.** Triage first: mark restatements and duplicates `duplicate` or
`parked` instead of paying to test them. `plan --stage assay` sends two cards
to each falsifier (Codex by default, so it can compute). A falsifier first
runs each card's `kill_script`, unchanged, inside its own sandbox, after
reading it, and reports the raw output with a status: `passed`, `failed`,
`error`, `not-run` or `none`. Then it checks circularity, recorded
obstructions with their exact hypotheses, prior records, and an explicit
example satisfying the card's hypotheses. Verdicts are recorded
automatically with the script result; override them with `mark`. A passed
script is finite evidence for its cases and changes no status; a failed one
kills a card only if the script really tests the stated claim.

`prospector.py` never executes model-written code. It saves each script's
text under the falsifier's `work/kill_scripts/` and nothing more. A tool-free
Claude falsifier cannot run a script, so the tool records `not-run` for its
verdicts whatever the answer claims, and `none` for a card without a script.

**3. Dig.** `plan --stage dig --cards ID` develops one survivor into an
argument for one selected missing implication, by default with the model that
did not propose it. Explicitly chosen unassayed leads retain that status; the
template does not claim an assay occurred. The result's
`gap_after` is its earliest remaining gap. When that gap is smaller or
cleaner than the card, make it the frontier's new gap: this is how a hard
target is decomposed. For a step that needs sustained work, plan the slot
with `--depth deep` (see [Deep dig](#deep-dig)).

**4. Repeat, steering.**

- Explore distinct bounded moves before investing deeply in a selected one.
- Read the surviving sketches yourself. Combining ideas is the conductor's
  job; put a proposed combination in the note of a next-round slot.
- After two prospect rounds with no survivor, change the question, not just
  the lenses: weaken it, specialize it, choose a different gap or attack the
  obstruction itself.
- Add project lenses with `lenses_extra` when a domain move keeps recurring.
- Keep working on the mathematics directly between rounds. Prospector
  supplies ideas and tests; it does not replace the conductor's thinking.

**5. Certify.** When a dig card reports `gap_after: none`, assemble a
complete candidate, freeze it and follow PROOF_SWARM's dispatch step 5:
a fresh checker that did not develop it, plus the cross-model check. Mark the
card `promoted`. Only reviewed results enter canonical records.

## Variant ladder

A model cannot be retrained for one problem. What it can use is the local
structure shown by solved neighbours of the target, with their complete
proofs in the prompt. The ladder collects those neighbours and climbs them
from the easiest.

- **Ask for variants.** `plan RUN --stage variants` plans one explorer that
  proposes variants of the current gap: a special case, lower dimension or
  rank, a stronger hypothesis, a weaker conclusion, an analogue in another
  setting. Each has an exact statement, its relation to the target (implies
  it, is implied by it, or analogue; the relation itself proved or
  conjectured) and a difficulty from 1 to 5. It is one ordinary call.
  `variant RUN add` records a variant you choose yourself, for instance a
  known case from the literature.
- **Status.** A variant is `open`, `solved`, `refuted` or `parked`. This is
  run state in `variants.jsonl` and `variant-events.jsonl`, not a catalog
  status. `variant RUN list` shows the ladder, easiest first;
  `variant RUN mark ID --status refuted --reason "..."` records a decision.
- **Climb from the easiest.** `plan` aims each prospect slot at a different
  open variant, easiest first; slots left over attack the target. Use
  `--target-only` to attack the target anyway, `--variant ID` to aim every
  slot at one variant, or set `variant_ladder.prefer_open` to false. The slot
  states the variant as its assignment; its cards remember it, and their
  assay and dig prompts show its statement.
- **Keep the proof.** When a variant is settled and you have read the
  argument, write the complete proof to a file and run
  `variant RUN solve ID --proof FILE`. The tool copies it to
  `variants/ID/proof-HASH.md` and records its SHA-256 and a review scope. The
  default scope is "unreviewed lead"; pass `--review-scope` to say who
  checked what. A solved variant is not a verified result. Only the
  conductor marks one solved; the tool never does.
- **Proofs in the prompt.** Every prospect, assay and dig prompt inlines the
  complete proofs of solved variants: by default the two most recently
  solved (`variant_ladder.inline_solved`), or the IDs in a slot's
  `solved_variants` list (`[]` for none). Each proof carries its review
  scope. The tool refuses a proof whose bytes no longer match the recorded
  hash. Blind slots receive the proofs but still no earlier-round ideas.
- **Whole or omitted.** Named context files, then solved proofs, then
  survivor sketches share `max_inline_chars`. A proof or sketch that does not
  fit is left out and the prompt says so; it is never clipped. `run` warns
  about each omission, so check a dry run. The prompt-size warning counts the
  prompt without these inlined texts, which are expected to be long.

## Mechanism search

A model's main advantage is breadth, so search for proof mechanisms across
fields, not for more facts within one. The conductor does this in the main
session before prospecting. Explorers get no network access; they receive
what the conductor brings back.

1. **Restate the obstruction in field-neutral terms.**
   `mechanism RUN start` writes the worksheet `MECHANISM_SEARCH.md`. Fill in
   the quantifier shape, what must be selected or glued, and what fails, with
   none of the project's objects or notation.
2. **Search, hub first.** Search the hub and its library, then the literature
   in several fields, for proofs that overcame an obstruction of that shape.
   Outside queries contain no unpublished statement from the hub. Register
   each one before sending it with `mechanism RUN query "..." --where "..."`.
   The tool refuses a query that contains a `hidden` item or repeats six
   consecutive words (`mechanism_search.refuse_shared_words`) of the
   frontier, the variants or the cards. This catches copied text only;
   keeping a paraphrase neutral is the conductor's duty. Record what came
   back, including nothing, with `mechanism RUN result q1 --result "..."`.
   Queries and results are kept in `mechanism-queries.jsonl`. The tool itself
   searches nothing and contacts nothing.
3. **Bring the proofs back.** For each candidate, save the statement and the
   decisive steps of the proof, quoted, and stamp the file with
   `mechanism RUN excerpt FILE --title "..." --source "..." --locator "..."`.
   Source and exact locator are required. The stamped copy in `mechanisms/`
   states its review scope: a lead, until `--checked` records a source check.
   Add its path to `context_files` or to one slot's `files`, and put a
   one-line mechanism summary in the frontier's "Tools and results that may
   help", which may carry a proof excerpt or mechanism summary as well as a
   statement.
4. **Use them.** The `analogy-transfer` and `literature-mechanism` lenses
   work from supplied excerpts: they map the quoted proof's objects to ours
   and name the first step that does not transfer.

Sources found this way stay leads until checked and retained under the
library rules. Web use follows the hub's own rules on searching and on
retaining consulted literature.

## Deep dig

A hard step can need hours of sustained work with computation, which a short
structured return cannot hold. `plan RUN --stage dig --cards ID --depth deep`
(or `"depth": "deep"` on a dig slot in `slots.json`) uses
`templates/prospector/dig-deep.md` for that slot. It is an explicit, per-slot
exception to the discovery cadence, meant for a selected survivor.

- **No time or word target.** The worker keeps a running `SCRATCH.md` in its
  work directory, computes small cases freely, writes the full derivation to
  `DERIVATION.md` and returns one `argument` card whose `gap_after` is the
  earliest remaining gap. A tool-free worker puts its scratch and derivation
  in its answer instead, and cannot compute.
- **No record-keeping for the worker.** The deep template asks for no ledger,
  hash, catalog or closeout work. The conductor records afterwards: read the
  derivation, then use `decide`, `mark` and, for a settled variant,
  `variant RUN solve ID --proof .../work/DERIVATION.md`. The slot's receipt
  lists both files with their SHA-256.
- **Budget.** A deep dig is one call however long it runs, and it has no
  deadline unless `budget.timeout_seconds` sets one. Weigh that before
  planning several.
- **Claude.** By default a deep Claude slot is the same as every other Claude
  slot: no tools, one turn. Setting `deep_dig.claude_tools` to `true` in
  `run.json` gives deep Claude dig slots, and no other slot, the tools in
  `deep_dig.claude_tool_names` (at most Read, Write, Edit, Glob, Grep and
  Bash) and up to `deep_dig.claude_max_turns` turns (default 60). A dry run
  shows the tools and turn limit. If the turn limit ends the session before
  a final answer, the call is recorded as failed and is not counted; the
  scratch and derivation files stay in the work directory. Codex deep slots
  use the same sandbox as other Codex slots.

[Safety](#safety) says which parts of the confinement are enforced.

## Records

The tool keeps everything in the run directory: `run.json`, `FRONTIER.md`,
each slot's prompt, raw output, response and receipt with SHA-256 hashes,
`cards.jsonl`, `dispositions.jsonl` and `ledger.jsonl` (status, timing,
model and usage when the CLI reports it). The ladder adds `variants.jsonl`,
`variant-events.jsonl` and the hashed proofs under `variants/`. Mechanism
search adds `MECHANISM_SEARCH.md`, `mechanism-queries.jsonl`,
`mechanisms.jsonl` and the stamped excerpts under `mechanisms/`. Do not copy
these into the NOTE.

While a model process runs, its slot's `active.json` records the child and
supervisor PIDs, elapsed time, output byte counts, last-output age and current
timeout; `status RUN` displays these separately from completed calls. A
30-second heartbeat prints and appends the same progress to `progress.jsonl`,
without prompt or model text. Active metadata is refreshed about every second
and removed when the child exits; progress history stays. An interrupted
supervisor may leave a stale record, so inspect its PIDs before acting. To
cancel deliberately, send `SIGTERM` to the recorded child PID; the supervisor
will retain the output and record the failure.

The monitor rereads `budget.timeout_seconds` from `run.json` about every
second: setting it to `null` removes an active deadline; a positive limit is
measured from the process's original start. An unreadable or invalid setting
temporarily pauses deadline enforcement and emits a warning until corrected.
These changes apply to newly launched supervisors. Python supervisors already
running the older code keep their original deadline and cannot acquire this
behavior by editing the code or their run file.

For writable workers the monitor captures a newly written `FIRST_RETURN.txt`
as an immutable, hashed `first-return-*.txt` snapshot with `first-return.json`
metadata. It ignores stale, empty, symlinked or oversized notes and never
extracts hidden reasoning from backend streams. Early-note arrival causes a
progress notification, not termination. `decisions.jsonl` records manual
conductor decisions; `status` reports the median first decision from attempt
start. Retries are distinguished by their start time. The wall-time total is
aggregate helper time, not elapsed user-session time.

- Put runs at `projects/NAME/investigations/DATE-TOPIC/prospector/RUN/`.
  The NOTE gets the link, the authorization, one line per round saying what
  changed, and the closeout.
- Killed ideas stay as brief dispositions. A kill becomes a canonical obstruction only
  when the owner wants it established.
- At closeout run `close --outcome ...`, revise the Gap and, when useful,
  save one Episode with the surviving mechanisms and the kill fingerprints.
  Save a session checkpoint.
- Citations in cards are leads. Check and retain sources under the library
  rules before relying on them.

## Safety

Claude explorers have no tools by default. Codex explorers run in Codex's
`workspace-write` sandbox rooted at their own work directory, with
`OPENAI_API_KEY` removed so the ChatGPT login is used. Never add bypass
flags. Explorers do not run Git, write canonical files or contact anyone.
Kill scripts are written by one model and run by another: only a falsifier
runs them, inside its own sandbox (for Codex, `workspace-write` in its work
directory), and the tool itself never does. The falsifier is told to read a
script first and not to run one that does more than compute; that reading is
instructed, the sandbox is what is enforced.
CLI login and sandbox availability depend on the local setup. Use
`doctor --live` only when the owner authorizes a connectivity call.

The one exception to tool-free Claude is the opt-in deep dig. Its command is
`claude --print --restricted --tools NAMES --permission-mode acceptEdits
--permission-prompts none --settings SANDBOX ... --max-turns N`, started in
the slot's work directory, where SANDBOX turns on the CLI's command sandbox
(`enabled`, `failIfUnavailable`, `autoAllowBashIfSandboxed`, and
`allowUnsandboxedCommands: false`). The flags were taken from `claude --help`
of version 2.1.325 and the setting names from that version's settings schema.
`--max-turns` is a hidden option there: it is absent from the help text but
defined in the CLI. Check `claude --help` again after a CLI update.

| Safeguard for a deep Claude slot with tools | Status |
|---|---|
| Tools only when `run.json` and the slot both opt in; only Read, Write, Edit, Glob, Grep, Bash; no web tool; no permission-bypass flag, and a command containing one is refused | Enforced by `prospector.py`, covered by offline tests |
| File tools confined to the work directory; user and project settings ignored | Enforced by the CLI's `--restricted` mode according to its help text; not exercised by offline tests |
| Shell commands run only inside the CLI's sandbox, which restricts writes and network access; the call fails at startup if the sandbox cannot start | Enforced by the CLI's sandbox according to its settings schema; not exercised by offline tests |
| Anything that would ask for permission is denied, since nobody answers prompts | Enforced by the CLI (`--permission-prompts none`); not exercised by offline tests |
| Shell commands reading files elsewhere on the machine, including hub records and `hidden` material | Instructed only; the sandbox does not hide the disk |
| No Git, no publishing, no software installs | Instructed; partly backed by the sandbox's write and network limits |

After the first deep Claude slot with tools, read its `receipt.json`:
`permission_denials` counts refused tool uses (`null` if the CLI did not
report them), and a shell that never ran suggests the sandbox settings did
not take effect on that machine.

## Measuring

`close` records each run's outcome and `scoreboard` lists all runs with
calls, cards, survivors and outcome. To compare designs without spending on
open problems, run a retrodiction benchmark: pick a reviewed hub result, list
its record IDs and evidence paths under `hidden` in `run.json`, frame the
frontier as it stood before the result, and see whether a run rediscovers it
and at what cost. The tool refuses to inline hidden material and tells
explorers not to open it; Codex explorers can still read the hub, so this is
instructed, not enforced.

## Commands

```
python3 tools/prospector.py init RUN --project P --gap K-ID --target "..." --authorization "..."
python3 tools/prospector.py mechanism RUN start
python3 tools/prospector.py mechanism RUN query "..." --where "arXiv"
python3 tools/prospector.py mechanism RUN result q1 --result "..." [--source "..."]
python3 tools/prospector.py mechanism RUN excerpt FILE --title "..." --source "..." --locator "..."
python3 tools/prospector.py mechanism RUN list
python3 tools/prospector.py plan RUN [-n 6] [--target-only | --variant ID]
python3 tools/prospector.py plan RUN --stage variants
python3 tools/prospector.py variant RUN list
python3 tools/prospector.py variant RUN add --title "..." --statement "..." --kind special-case --relation implied-by-target --difficulty 2
python3 tools/prospector.py variant RUN mark ID --status refuted --reason "..."
python3 tools/prospector.py variant RUN solve ID --proof FILE [--review-scope "..."]
python3 tools/prospector.py run RUN [--dry-run]
python3 tools/prospector.py cards RUN [--status open] [--show CARD]
python3 tools/prospector.py plan RUN --stage assay [--cards CARD ...]
python3 tools/prospector.py plan RUN --stage dig --cards CARD [--depth deep]
python3 tools/prospector.py mark RUN CARD ... --status parked --reason "..."
python3 tools/prospector.py decide RUN --round 1 --slot s1 --action revise --reason "..." --next-question "..."
python3 tools/prospector.py status RUN
python3 tools/prospector.py close RUN --outcome partial --note "..."
python3 tools/prospector.py scoreboard
python3 tools/prospector.py doctor [--live]
```

Templates: `templates/prospector/` holds the run defaults, the frontier card,
the explorer, assay, dig, deep-dig and variants prompts, the mechanism-search
worksheet, the claim-card and variants schemas and the lens library. Tests: `tools/test_prospector.py` (offline stub backend and local
subprocess monitoring; no model calls).
