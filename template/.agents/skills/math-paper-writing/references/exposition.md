# Exposition

Sources, checked 2026-10-14. Bracketed numbers such as [P13] are item numbers
in Poonen's guide.
- B. Poonen, *Practical suggestions for mathematical writing*, version of
  6 January 2026: https://math.mit.edu/~poonen/papers/writing.pdf
- T. Tao, *On writing*: https://terrytao.wordpress.com/advice-on-writing-papers/
- S. Kleiman and G. Tesler, *Writing a Math Phase Two Paper*, MIT, 1999.
- P. Halmos, *How to write mathematics*, Enseign. Math. 16 (1970), 123–152.

Contents: 1 Title · 2 Abstract · 3 Introduction · 4 Body · 5 Proofs ·
6 Sentences · 7 Calibration and credit · 8 Patterns to remove

## 1. Title

- Make it specific enough to tell this paper apart from its neighbours. Name
  the object and the result [P14]. A good model: "Full strong exceptional
  collections of vector bundles on rational surfaces".
- Avoid "A note on", "Remarks on" and "On some" [P15]. Avoid formulas and
  macros where possible, because arXiv metadata needs plain text.

## 2. Abstract

- Use 80 to 150 words; the model papers use 85 to 135. State the main result
  in words with the hypotheses that fit. Add one sentence on the method if it
  is distinctive, and one on secondary results.
- Make it self-contained: no citations, no references to the body and no
  undefined notation [P16–17].
- Open with the result or the problem, not "In this paper we study".
- For a short paper a single exact sentence can be enough, as in Kawamata:
  "We prove that the derived categories for toric varieties have complete
  exceptional collections."

## 3. Introduction

Adapt this default order and leave out what does not apply. Do not pad.

1. **Problem and context.** One or two paragraphs saying why the question
   matters, in mathematical terms rather than as hype.
2. **Main result.** Theorem 1.1, or Theorem A, by the end of page 2. Kawamata
   states it in the first paragraph. If the full statement needs heavy set-up,
   state a special case or a version labelled informal, and point to the
   precise statement (Tao).
3. **Relation to earlier work.** Say who proved what, in what generality, and
   where the new result goes further or differs. Totaro writes "we get a bit
   more" and says exactly how. Name the new difficulty that earlier methods
   could not handle (Tao). Never claim "first" beyond what the dossier's
   searches support.
4. **Idea of the proof.** One or two paragraphs naming the mechanism, such as
   Kawamata's induction through the toric minimal model programme or Totaro's
   degeneration to characteristic 2. Say which step is new and which are
   standard.
5. **Sharpness and limits.**
   - Give examples where a hypothesis fails. BKR remarks that their condition
     fails in dimension at least 4.
   - State what the paper does not prove, as in Bridgeland's scope sentence.
   - Pose open questions plainly: "We do not know if hypothesis H is
     necessary" (Tao).
6. **Consequences.** Give concrete instances after the general theorem, such
   as Totaro's very general quartic fourfolds.
7. **Organization.** Give a roadmap with section numbers. Flag routine or
   technical parts that a reader can skip. BKR write that "the only nontrivial
   part is Section 2.6"; Efimov warns that one proof is technical.
8. **Conventions.** Use a short subsection at the end of the introduction, or
   a Section 2 (see `algebraic-geometry.md`).

There is no conclusions section [P19; Kleiman]. Discussion of results and
further work belongs in the introduction.

## 4. Body

- Section titles describe content, such as "The blow-up step" or "Proof of
  Theorem 1.1", never "Step 2" (Tao).
- Split long arguments into lemmas, even ones used once, so the reader holds
  less in mind [P5]. Say which results of a section are used later [P6].
- Definitions come before the theorem that uses them. Keep statements short
  [P20]. Explanation goes in the surrounding text, not inside a lemma
  statement.
- Show concrete cases before abstract ones where that helps: an example before
  a definition, as in Bridgeland §1.1, or the worked example and figure in
  Lazarsfeld–Mustață.
- Put routine verifications, long case checks and computer verifications in an
  appendix or after the conceptual argument. Keep the induction step separate
  from the base cases.
- Assemble the main proof in one place ("Proof of Theorem 1.1") that cites the
  lemmas by number.

## 5. Proofs

- Unless a claim follows from the previous sentence, say what it follows from
  [P1]. In a chain of equalities, attach a reason to each step [P2]. At each
  full stop the reader should know why everything so far is true, or have been
  told "as we now explain" [P3].
- Show where each hypothesis is used [P8].
- To claim an isomorphism, give the map and claim that it is an isomorphism
  [P24].
- Do the easier parts first [P23]. Prefer a direct proof to one by
  contradiction when they are equally easy [P34; Kleiman].
- Prefer a conceptual argument to a computational one where possible. Display
  computations so they can be checked line by line.
- Do not write "clearly", "obviously", "trivially" or "it is easy to see".
  Give the reason in a few words, or drop the adverb [P26].
- Do not announce proofs ("We now prove the following proposition") [P29].
  Do not write "This concludes the proof" before the end-of-proof mark [P57].
- Make the status of every assertion visible: proved here, cited with a
  locator, or open (Conjecture or Question). Avoid "it can be shown" without a
  proof or a reference (Tao; Kleiman).
- When omitting a standard argument, cite it and say whether it is routine
  (Kleiman).

## 6. Sentences

This section condenses Poonen §§3 and 5.

- Write so that a sentence cannot reasonably be read another way [P4].
- Make quantifiers explicit with "for all" or "for some" [P7]. No ∀, ∃ or ⇒
  in prose [P31]. No WLOG, iff, s.t. or contractions [P30, P33].
- Do not start a sentence with a symbol [P32]. Write "Let Z be the center of
  G" [P48]. Define before use rather than adding ", where ..." [P27].
- A display is part of its sentence. Punctuate it, and put no colon before it
  unless the grammar needs one [P50].
- Refer by number with a capital letter: "Theorem 3.2", not "the previous
  theorem" [P35, P49]. Put a tie before the reference: `Theorem~\ref{...}`.
- "So that" expresses purpose; "such that" imposes a condition [P43]. Use
  "since" rather than "as" [P64]. Write "Assume that G is finite" [P51].
- Write "nonzero" and "nonempty" without hyphens [P62]. Use numerals for
  mathematical quantities [P63].
- Cut "Note that" and "We remark that" unless removing them would leave a
  sentence starting with a symbol [P56]. Keep sentences short [P21].
- Use "we" and the present tense throughout.

## 7. Calibration and credit

- Neither overstate nor understate (Tao). Say what the theorem does not give,
  for example "the ranks grow rapidly; we give no bound".
- Credit precisely: name the author, the result and the locator. Attributing
  an idea from correspondence needs that person's agreement, which is a
  decision for the user.
- Cite "forthcoming work" only if a public preprint exists [P12].
- For computer-assisted parts, state the exact scope: what was computed, the
  software and versions, whether the computation was exhaustive, and where the
  code is.

## 8. Patterns to remove

These are typical of AI drafts and of hub-to-paper leaks:
- Signposting and filler: "Crucially", "Importantly", "Notably", "It is worth
  noting", "a key insight is", "delve", "leverage", "robust", "landscape".
- Arguments written as bullets or under bold headings inside proofs; inline
  headings within paragraphs; strings of dashes.
- Summaries that repeat the section just finished, "In summary" paragraphs,
  and a conclusions section.
- Defensive repetition of hypotheses or disclaimers; a tutorial tone about
  notions any expert knows; stacked hedges such as "may potentially".
- The same motivation repeated in the abstract, the introduction and the start
  of every section.
- Notation churn: two letters for one object, or one letter for different
  objects in nearby sections.
- Explanation placed inside a lemma statement.
- Hub vocabulary, model meta-comments and unreplaced placeholders (SKILL.md
  rules 6 and 8).
- Invented precision: page or theorem numbers, dates or quotations that are
  not in a checked source.
