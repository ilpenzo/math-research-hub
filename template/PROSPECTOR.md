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

**1. Prospect.** `plan` assigns distinct strategy lenses, unused ones first,
from `templates/prospector/lenses.json`. Idea lenses go to Claude and
computing lenses to Codex, which may run Macaulay2, Sage and Python in its own
work directory. Edit `slots.json` (lens, backend, up to three named file
sections, a note), then `run`. Explorers do not see each other. From the
second round they see survivors, one-line kills and pending titles, except
for blind slots such as the wild card. Each returns one claim card by default
(the run may explicitly allow more):
statement, role in the gap, sketch, weakest step, fastest kill test,
evidence, what is new, confidence and cost.

**2. Assay.** Triage first: mark restatements and duplicates `duplicate` or
`parked` instead of paying to test them. `plan --stage assay` sends two cards
to each falsifier (Codex by default, so it can compute). A falsifier checks
circularity, recorded obstructions with their exact hypotheses, prior
records, and an explicit example satisfying the card's hypotheses. Verdicts
are recorded automatically; override them with `mark`.

**3. Dig.** `plan --stage dig --cards ID` develops one survivor into an
argument for one selected missing implication, by default with the model that
did not propose it. Explicitly chosen unassayed leads retain that status; the
template does not claim an assay occurred. The result's
`gap_after` is its earliest remaining gap. When that gap is smaller or
cleaner than the card, make it the frontier's new gap: this is how a hard
target is decomposed.

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

## Records

The tool keeps everything in the run directory: `run.json`, `FRONTIER.md`,
each slot's prompt, raw output, response and receipt with SHA-256 hashes,
`cards.jsonl`, `dispositions.jsonl` and `ledger.jsonl` (status, timing,
model and usage when the CLI reports it). Do not copy these into the NOTE.

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

Claude explorers have no tools. Codex explorers run in Codex's
`workspace-write` sandbox rooted at their own work directory, with
`OPENAI_API_KEY` removed so the ChatGPT login is used. Never add bypass
flags. Explorers do not run Git, write canonical files or contact anyone.
CLI login and sandbox availability depend on the local setup. Use
`doctor --live` only when the owner authorizes a connectivity call.

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
python3 tools/prospector.py plan RUN [-n 6]
python3 tools/prospector.py run RUN [--dry-run]
python3 tools/prospector.py cards RUN [--status open] [--show CARD]
python3 tools/prospector.py plan RUN --stage assay [--cards CARD ...]
python3 tools/prospector.py plan RUN --stage dig --cards CARD
python3 tools/prospector.py mark RUN CARD ... --status parked --reason "..."
python3 tools/prospector.py decide RUN --round 1 --slot s1 --action revise --reason "..." --next-question "..."
python3 tools/prospector.py status RUN
python3 tools/prospector.py close RUN --outcome partial --note "..."
python3 tools/prospector.py scoreboard
python3 tools/prospector.py doctor [--live]
```

Templates: `templates/prospector/` holds the run defaults, the frontier card,
the explorer, assay and dig prompts, the claim-card schema and the lens
library. Tests: `tools/test_prospector.py` (offline stub backend and local
subprocess monitoring; no model calls).
