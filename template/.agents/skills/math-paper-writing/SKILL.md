---
name: math-paper-writing
description: Plans, drafts, revises and checks mathematics research papers, preprints and short notes in LaTeX (amsart) from results recorded in this research hub. Use when asked to write up selected results as a paper or note, restructure or polish a manuscript's exposition, write a title, abstract or introduction, prepare arXiv-ready source and metadata, or audit a draft before it leaves the hub. Not for discovering or proving new results, and not for internal investigation NOTEs.
---

# Writing mathematics papers from the research hub

A paper is a durable public statement under the user's name. Its job is to
state exactly what the hub has established, credit earlier work precisely, and
let a specialist understand and verify the argument quickly. Good exposition
can make a correct result readable. It cannot add truth, review or novelty.

The hub rules still govern: root `AGENTS.md`, the owning project's `AGENTS.md`,
`WORKFLOW.md` and `PROOF_SWARM.md`. This skill adds the writing workflow on top
of them. Paths below are relative to the hub root unless they start with
`references/`, `assets/` or `scripts/`, which are inside this skill folder.

## Hard rules

1. **Claims follow catalog status.** A result appears as a Theorem,
   Proposition or Lemma only if its record status and review support it in the
   stated scope. Unreviewed, conditional, computational and conjectural
   material is labelled as such (Conjecture, Question, a Remark with explicit
   scope, "verified by computer for ...") or left out. Writing never changes a
   record status.
2. **Only checked citations.** Every cited result has a source record covering
   the exact statement and version, or a new check following
   `templates/source-check.md`. Cite with a locator, such as
   `\cite[Theorem~3.5]{HP11}`. Never cite from memory or from a search snippet.
   Mark anything unchecked `[VERIFY: what is missing]`. Any such marker blocks
   release.
3. **Writing never invents mathematics.** If restructuring exposes a gap,
   stop and record it in the NOTE for the proof conductor. A weaker correct
   statement is better than a polished false one.
4. **Changes to statements are mathematical changes.** Changing a hypothesis,
   quantifier, statement or proof step, even "for clarity", sends the affected
   part and its connections back through proof review.
5. **Some decisions belong to the user.** The user decides on posting,
   submission, contacting authors or editors, the licence, authorship,
   affiliation, acknowledgements, funding lines and the AI-assistance
   statement. Draft these; never decide or send them.
6. **Hub vocabulary stays out of the paper,** including TeX comments, since
   arXiv distributes the source. This covers record and source IDs, "control",
   "fingerprint", "missing implication", agent or helper names, review labels
   verification labels and NOTE paths.
7. **Never overwrite a frozen or circulated file.** Each draft gets a new
   version suffix (`paper-v3.tex`), and the diff from the previous version is
   kept.
8. **Released source is clean.** It has no TODO or `[VERIFY]` markers,
   placeholders, model meta-comments ("Here is the revised section") or
   commented-out alternative proofs. arXiv holds authors responsible for every
   reference and sentence. Since May 2026 it reportedly bans authors for a year
   on clear evidence of unchecked LLM output, such as invented references or
   leftover placeholders.

## Workflow

Copy this checklist into the NOTE and keep it current:

```
Paper progress
- [ ] 0 Brief and NOTE
- [ ] 1 Evidence dossier
- [ ] 2 Spine (shown to the user when scope or claims are new)
- [ ] 3 Draft vN: body, then introduction, abstract, title
- [ ] 4 Self-audit passes a-g
- [ ] 5 Build, check_paper.py, rendered pages inspected
- [ ] 6 Frozen review(s) and reconciliation
- [ ] 7 Release package and closeout
```

### 0. Brief

Record the following in the NOTE:
- the results to include (record IDs);
- the paper shape (`references/algebraic-geometry.md`, section 4);
- the audience, the intended venue (or arXiv only) and a target length;
- the integration owner, plus start time and deadline in UTC if the work is timed.

Use one investigation NOTE in the owning project (`templates/investigation.md`).
A write-up that continues an existing investigation reuses that NOTE. Keep
drafts, builds and reviews in a `paper/` folder beside the NOTE. Ask the user
about real choices, such as which results, the venue or the author block, but
not about style.

### 1. Evidence dossier (before any prose)

The dossier lives in the NOTE, with one row per claim the paper will make:

| Paper label | Record | Exact statement: hypotheses, object category, field and characteristic | Status and review scope | Proof file and SHA-256 | Corrections, foreign prerequisites | External inputs with locators |
|---|---|---|---|---|---|---|

Use `python3 tools/hub.py show ID`, `impact ID` and
`search "..." --project NAME`, and include corrections and failed approaches.
Also record two more things:
- **Novelty basis.** Which hub and literature searches were run, when, and
  what they found. This fixes the strongest wording the paper may use. "To our
  knowledge" claims nothing beyond the searches made.
- **Necessity examples.** Examples where a main hypothesis fails and the
  conclusion fails too. These become Examples or Remarks in the paper.

A statement without a dossier row does not appear in the paper as a result.

### 2. Spine (one page, before drafting)

Write the following in the NOTE:
- A working title, and one sentence of contribution that a specialist would
  repeat to a colleague.
- A draft abstract of 80 to 150 words.
- The main theorem or theorems, exactly as they will appear (Theorem 1.1, or
  Theorems A and B).
- For each closest prior result: who proved what, in what generality, and the
  one-line difference.
- The key idea in three to five sentences, naming the mechanism and the new
  difficulty it overcomes.
- The dependency list: each lemma with its inputs, and whether it is new or
  cited. Check that it is acyclic and that every hypothesis of a main result is
  used somewhere.
- Sharpness: the necessity examples, what is not proved, and open questions.
- A section plan, with routine verifications, long base cases and computer
  checks moved to later sections or appendices.

When the scope or claims of a paper are being set for the first time, show the
spine to the user before drafting.

### 3. Draft

Write the body first, then the introduction, the abstract and finally the
title. That way each summarizes text that already exists. Start from
`assets/paper-template.tex`, the hub's default style (the owner may
replace it with their own). Follow:
- `references/exposition.md` for structure, proofs and sentences;
- `references/algebraic-geometry.md` for conventions and model papers;
- `references/latex-and-arxiv.md` for LaTeX.

Two rules shape everything else:
- The title, abstract, introduction and body each describe the whole paper,
  each in more detail than the last, and each reads on its own.
- The main theorem is stated by the end of page 2.

### 4. Self-audit

Do each pass as a separate read, and fix problems before moving on:

a. **Statements match.** The introduction and body statements are identical,
   or the introduction labels its version as simplified. The title and
   abstract claim no more than the theorems.
b. **Hypotheses are used.** For every hypothesis of every main result, find
   where the proof uses it. One that is never used signals an error or an
   unnecessary assumption.
c. **Categories and quantifiers are right.** Check sheaf, bundle or object;
   general, very general or arbitrary; the characteristic; "for all" or "for
   some".
d. **Citations are complete.** Every external input has a locator and a
   dossier row. Cite the published version when one exists, otherwise the
   arXiv version number.
e. **Notation is consistent.** Each symbol is defined once, before its first
   use, and never reused for another object nearby. For papers over 15 pages,
   keep a notation table in the NOTE.
f. **Reverse outline.** List each paragraph's first sentence. The list should
   tell the argument. Delete repeated motivation.
g. **Language.** Apply the patterns-to-remove list in `references/exposition.md`.

### 5. Build and mechanical checks

```
latexmk -pdf -interaction=nonstopmode -halt-on-error paper-vN.tex > build-vN.txt 2>&1
python3 .agents/skills/math-paper-writing/scripts/check_paper.py paper/paper-vN.tex
pdftoppm -r 80 -png paper-vN.pdf page
```

Run the checker with paths from the hub root, or use its absolute path. It
reads the `.tex`, any `\input` files, the `.log` and the bibliography, and
never writes. Inspect these rendered pages: the title page, the densest
theorem page, any diagram page and the bibliography.

The gate: the checker exits 0, and each remaining warning is either fixed or
justified in the NOTE.

### 6. Frozen independent review

Freeze the candidate as `FROZEN-paper-vN.tex` and record its SHA-256 in the
NOTE. Then use the briefs in `references/review.md`:
- **A. Assembled proof audit.** Use a fresh checker with no exploratory
  history and no earlier favourable verdicts, preferably from the other model
  family when the owner uses both (Claude-authored text goes to Codex, and
  the reverse; see the cross-model section of PROOF_SWARM.md).
- **B. Cold-reader test.** A fresh reader gets only the paper and fixed
  questions, and the answers are compared with the spine.
- **C. Expert editorial report.**

Before a paper leaves the hub (sent to anyone, posted or submitted), A, B and C
are all required. A may be limited to changed parts and their connections only
if the unchanged proof text was already audited in frozen form. Say so
explicitly. For internal drafts, all three are optional.

Reviews count as bounded assignments under `PROOF_SWARM.md`. A Claude session
launches a Codex review only when the current task authorizes it. Reconcile
every suggestion in the NOTE. An accepted mathematical change makes a new
frozen version and a new audit of what changed.

### 7. Release package and closeout

- **Files.** Keep `paper-vN.tex` and `.pdf`, the diffs from earlier versions
  and `SHA256SUMS`.
- **Build record.** Use `hub.py package` (see WORKFLOW.md
  "Computations"). The source is the build recipe; the inputs are the
  `.tex`, `.bib`, figures and environment versions; the output is the PDF.
- **arXiv metadata draft.** Follow `references/latex-and-arxiv.md`: an ASCII
  abstract of at most 1920 characters, MSC 2020 codes, a comments line, and
  ancillary files for any code.
- **User decisions.** List the author block, acknowledgements, the drafted
  AI-assistance statement, the licence, the venue and the timing.
- **Closeout per AGENTS.md:**
  - add an integration receipt to the NOTE;
  - record evidence routes with `hub.py put` (writing changes no status);
  - update the project CHANGELOG and TOPICS;
  - run `check --project NAME` and `render --project NAME`.
- **Separate statuses.** Report five separately: draft complete; proof review
  (scope, fresh or context-exposed); editorial review; novelty (search scope
  and date); submission, which is never authorized unless the user said so.

## Revising an existing manuscript

For requests to polish or restructure:
- Rebuild the spine from the existing paper and keep the original bytes.
- Produce version N+1 with a diff.
- For every statement that was moved or reworded, record a
  mathematical-equivalence check in the NOTE.
- Scale the step-6 review to the changes.

Four changes that often improve a draft:
- lead with the theorem;
- move routine proofs to an appendix;
- separate the induction step from base-case computations;
- rename colliding notation.

## Reference files

- `references/exposition.md`: title, abstract, introduction architecture,
  body, proofs, sentence rules and patterns to remove.
- `references/algebraic-geometry.md`: the conventions checklist,
  distinctions to preserve, ten model papers, paper shapes and MSC codes,
  for algebraic geometry. For another field, ask the assistant to draft a
  matching `references/<field>.md` with the owner and use that instead.
- `references/latex-and-arxiv.md`: house preamble, labels, bibliography,
  build, arXiv package and metadata, and the AI-assistance statement.
- `references/review.md`: the freeze protocol, review briefs A, B and C, and
  the reconciliation table.
- `assets/paper-template.tex`: an amsart skeleton in the default style. Its
  placeholders are marked TODO, so the checker fails until they are filled.
- `scripts/check_paper.py`: a read-only checker. Run it with `--help` for
  options; `--release` also treats markers and IDs in comments as errors.
