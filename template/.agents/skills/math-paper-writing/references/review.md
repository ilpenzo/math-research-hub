# Frozen review briefs

Contents: 1 Freeze and accounting · 2 Brief A (proof audit) · 3 Brief B
(cold reader) · 4 Brief C (expert editorial report) · 5 Reconciliation

## 1. Freeze and accounting

- Copy the candidate to `FROZEN-paper-vN.tex`, and any companion to
  `FROZEN-guide-vN.tex`. Record each SHA-256 in the NOTE. The reviewer
  confirms the hash before and after review.
- **Supply:** the frozen text, the brief, the exact claimed theorems with
  hypotheses, the distinctions to preserve and, for audits, the cited results
  with locators from the dossier.
- **Withhold:** exploratory conversation, earlier favourable verdicts and the
  authoring history. A reviewer exposed to them is context-exposed, and the
  NOTE must say so.
- Keep the prompt, response, model, effort, timings and any usage telemetry
  in a review folder beside the NOTE.
- **Budgets.** Each review is a bounded helper job under `PROOF_SWARM.md`:
  - Codex standard: six jobs, two kept for review;
  - Claude economy: three jobs, one kept for review;
  - at most two running at a time.
- **Cross-model review.** One model family consults the other only when the
  owner has allowed it for the current task or in a standing permission
  written into PROOF_SWARM.md.

## 2. Brief A: assembled proof audit

Use the proof-checker role (`.claude/agents/proof-checker.md` or
`.codex/agents/proof-checker.toml`), preferably from the other model family.
Prompt skeleton:

> Audit the frozen mathematics preprint below (file name, SHA-256). Claimed
> result: [exact statements with all hypotheses]. Check the complete assembled
> argument: each lemma, the connections between lemmas and the final
> inference. Check also that every hypothesis of each cited result holds where
> it is applied; locators are supplied: [list]. Preserve these distinctions:
> [list]. Report: (1) a verdict for each section and overall: valid in stated
> scope, gap, false, or undecided; (2) the earliest unsupported step, with an
> exact quotation; (3) accepted versus actually checked dependencies; (4) any
> overclaim in the title, abstract or introduction relative to the proved
> statements. Say whether your review is fresh or context-exposed. Do not edit
> files or delegate. Use up to [N] words.

## 3. Brief B: cold-reader test

Give a fresh model, with no tools, only the paper: PDF text or TeX. This
adapts the reader-testing stage of Anthropic's doc-coauthoring skill. Ask it to
answer from the paper alone and quote locations:

1. State the main theorem precisely, with all hypotheses.
2. What was known before, and what exactly is new here?
3. What is the key idea of the proof, in three sentences?
4. Where is each hypothesis of the main theorem used?
5. Which sections can a specialist skip on first reading?
6. What does the paper not prove? What are its limits and open questions?
7. Which notation or term was unclear when first used?

Compare the answers with the spine. A wrong or vague answer to questions 1–4
is an exposition defect: fix the text, not the reader. Repeat the test after a
substantial revision.

## 4. Brief C: expert editorial report

> The author requests an editorial referee report on the attached [N]-page
> preprint, read from the perspective of a specialist in [field] (for
> instance, [names of plausible expert readers]). You are an AI reviewer taking
> that perspective, not a human referee. No earlier versions, verdicts or
> history are supplied. The exact claimed theorem is: [...]. Preserve these
> distinctions: [...]. This is not a theorem or priority audit. Flag
> mathematical ambiguity or overclaim that harms the exposition, but do not
> re-prove the theorem or research sources. Assess:
> (1) would an expert understand the contribution and the proof mechanism on
> first reading, and where must they backtrack, infer an unstated motivation
> or carry unnecessary notation?
> (2) is the central condition or mechanism motivated naturally?
> (3) is the balance of conceptual explanation and routine calculation right?
> Which passages are repetitive, defensive, tutorial or unnatural, and would
> another arrangement of sections and appendices work better?
> (4) are the title, abstract and introduction accurate and calibrated, with
> earlier work credited precisely?
> Return a candid verdict, prioritized suggestions that separate substantive
> problems from optional preferences, up to three replacement passages that
> preserve the mathematics, and a recommendation on readiness. Use about 1000
> to 1400 words, and do not manufacture criticisms to fill the space.

## 5. Reconciliation (in the NOTE)

| # | Suggestion (A/B/C) | Accepted or declined | Reason | Mathematical change? | Re-review |
|---|---|---|---|---|---|

- Accept suggestions that improve accuracy or readability. Decline others with
  a reason, such as "would add notation" or "outside the paper's scope".
- Any accepted mathematical change produces a new frozen version and a brief-A
  audit of the changed parts and their connections.
- A review certifies only its own scope. An editorial opinion is not a proof
  check. A proof audit is not a novelty check. Neither is a human referee's
  report.
