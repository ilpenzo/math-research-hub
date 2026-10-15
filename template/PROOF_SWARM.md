# Proof swarm

A small, adaptive proof team. The main session owns the mathematical argument,
chooses the next bottleneck and integrates evidence. Helpers answer bounded
questions. Unused helper capacity is fine. Nothing here starts a run by itself
or promises a proof.

## The conductor

The main session you talk to is the **proof conductor**. It is a
responsibility, not an extra program. It:

1. States the exact target and keeps the argument's dependency list in the
   investigation NOTE, then picks the most useful unresolved implication.
2. Works through the central argument personally. It delegates only bounded
   questions that are worth the briefing cost, and keeps the strategy and the
   job ledger itself.
3. Reads the decisive evidence behind each helper report. It reconciles
   hypotheses, notation and conclusions before combining lemmas, and settles
   conflicts at the first mathematical disagreement, never by majority vote.
4. Assembles one complete candidate proof, including the steps that connect
   the lemmas and the final quantifiers. Individually checked lemmas do not
   make a checked proof.
5. Freezes the candidate (a file and its sha256 from `hub.py hash`) and sends
   it to a fresh proof checker that reviews the connections as well as the
   parts. It repairs gaps and gets the repair rechecked.
6. Owns the scope of the result, the honest list of remaining gaps, the
   update to the owner and the catalog changes. It keeps "full proof",
   "partial result" and "review pending" distinct.

## Developing ideas

- Separate idea generation, development and certification. A promising idea
  may be wrong; give it a constructive pass first. Identify the analogy, the
  structure it suggests, and the statement or calculation that would develop
  it. Label conjectural bridges as conjectural.
- Combine results through the structures they expose: two descriptions of one
  object, a local/global difference, a family of parameters, a change of
  category, an obstruction that can be used as data. Naming an adjacent
  theory is not a mechanism; explain the map between the ingredients.
- Turn failures into questions. A vanishing composition may suggest an
  extension; independent local pieces may suggest a global compatibility
  condition. These are heuristics, not theorems.
- Test the exact proposed assertion. A lower-dimensional counterexample kills
  a dimension-free claim, not a dimension-sensitive mechanism. A failed
  universal claim can motivate a corrected hypothesis.
- Save the reusable move in the NOTE: starting facts, connection, proposed
  implication, decisive calculation, exact failure or surviving scope, and the
  changed next question.

## Starting a run

Example request: "Use the proof swarm on [statement or record ID] in project
[NAME]. Aim for a proof or a rigorously checked partial result."

Create or reuse the NOTE (`hub.py new-note`) and record: the quantified
statement with all hypotheses; the one missing implication; relevant prior
results, corrections, failed approaches and controls; a success criterion and
an informative negative outcome; the effort profile and the ledger.

## Effort profiles

| Profile | Helper jobs in total | Kept for review | Running at once |
|---|---:|---:|---:|
| Economy (Claude Code default) | 3 | 1 | at most 2 |
| Standard (Codex default) | 6 | 2 | at most 2 |
| Custom ("budget: N helper jobs") | N, stated before starting | at least 1 for any claimed result | at most 2 |

A job is one bounded request, including a follow-up that adds work. Failed or
interrupted jobs still count. The cap covers the whole request, including
continuations; renaming a route or opening a new NOTE does not reset it.
These caps limit delegation, not the conductor's own work. The owner can
change them at any time.

A default brief has one question, about 600 words of context and up to four
named evidence files. A default answer is about 400 words, or up to 700 when
proof detail needs it; a proof is never truncated to fit. For literature
discovery the default is three query formulations and two primary sources.

## The five helper roles

| Role | Job | Must return |
|---|---|---|
| archive-scout | Find the closest prior claim, correction and failed mechanism | exact records and locators; the failure and its reopening condition |
| source-checker | Find a primary theorem for one gap, OR check one theorem's application | exact statement, version and locator; hypothesis-by-hypothesis comparison |
| proof-explorer | Make one bold attempt at the missing implication | argument or reduction; dependencies; earliest gap; what is new versus earlier failures |
| counterexample-checker | Stress-test one inference against admissible controls | a counterexample with every hypothesis checked, or a precisely limited negative search |
| proof-checker | Independently audit a frozen candidate | valid in stated scope / gap / false / undecided, with decisive evidence |

The roles are defined in `.claude/agents/` for Claude Code and generated into
`.codex/agents/` for Codex (`hub.py sync-roles`). Change the Claude file, then
sync. Helpers do not read this file; their role and brief carry the rules.
Check which roles the session actually offers. If a role is not available
(Codex offers project roles only after the folder is trusted), start an ordinary helper,
paste the role file's text at the top of its brief, and tell it to stay
read-only. Its read-only status is then instructed, not enforced.

## Dispatch loop

1. **Retrieve before inventing.** Search the project, including corrections
   and failed mechanisms, then the archives. Do not send two helpers the same
   question.
2. **Find the bottleneck.** Write a dependency list: each step's premises,
   evidence and state (proved, assumed, conjectural, refuted). Compare the
   route with earlier failures by fingerprint: statement, hypotheses,
   mechanism, earliest failure, and the genuinely new input.
3. **Choose one attempt.** The conductor or one proof explorer pursues one
   concrete mechanism. Use a second slot only for independent work such as a
   source check or a control. No panels of near-identical attempts.
4. **Accept only a specific change.** Progress means a proved lemma, a valid
   counterexample, a checked source application, or a reduction with a proved
   relation to the target and a concrete advantage. Renaming the gap is not
   progress.
5. **Review the surviving claim.** Freeze it. Give a fresh checker the
   hypotheses, the full written proof, its dependencies and known corrections,
   but not praise, earlier favourable verdicts or the author's exploratory
   chat. The author never reviews its own work. The checker confirms the hash
   before and after. First find the earliest unsupported step, then audit
   every remaining step, including the assembly and the final inference. If
   the argument is too large, report the checked scope and leave the rest
   pending.
6. **Resolve and record.** A changed step needs a new check. If capacity runs
   out, record the repair as pending review. Only the conductor updates the
   catalog, TOPICS and CHANGELOG.

## Fresh versus context-exposed review

In Claude Code, start helpers by their role name, never as a fork, so they
start without the conversation. In Codex, spawn without inherited
conversation where the tool allows it. A reviewer that saw the author's
exploration or earlier verdicts is **context-exposed**: say so, and keep the
independent review pending. Reading the frozen proof itself does not make a
review context-exposed.

Agents built on the same model can share blind spots. Agreement is not proof,
a finite test is not a universal result, and a local object is not a global
counterexample.

## Cross-model review

If the owner uses both Claude Code and Codex, a claimed full proof should also
get one review from the other model family. Give it the frozen file, its
sha256, the hypotheses and dependencies, and the full written proof, but not
the other model's verdicts or exploratory chat. Read-only commands:

```
claude -p --permission-mode plan "$(cat brief.md)"        # from a Codex session
codex exec -s read-only "$(cat brief.md)"                  # from a Claude session
```

Run these only when the owner has allowed cross-model consultation for the
current task, or has written a standing permission into this file. Keep the
exact brief and answer in the NOTE.

## Stopping and reusing helpers

- Stop a helper once its question is answered, a decisive flaw appears, or a
  needed input is missing.
- Cancel jobs whose premise has failed; keep independent useful work.
- After two exploratory jobs in a row produce no new evidence or mathematical
  change, stop exploring. Use the remaining effort to review what survives and
  record the obstruction. Retrying needs a changed input.
- At the cap or the deadline, stop delegating. Record the best partial result,
  the exact gap and the next experiment. Never call an unresolved target
  proved.
- No helper may start other helpers.

## Ledger in the NOTE

Use the ledger table in the NOTE template. Debit each job before dispatching
it. At closeout report: the actual mathematical change; the review scope and
unresolved dependencies; jobs used against the cap; measured usage if the
tool reports it, otherwise "unknown"; and whether the catalog was updated.
