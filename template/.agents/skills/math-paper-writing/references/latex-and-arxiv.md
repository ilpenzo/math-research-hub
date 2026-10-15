# LaTeX house style, build and arXiv

Contents: 1 Preamble · 2 Labels and references · 3 Bibliography · 4 Build ·
5 arXiv package and metadata · 6 AI assistance and responsibility

## 1. Preamble

`assets/paper-template.tex` follows the user's 2026 preprints.

- Use `\documentclass[11pt,reqno]{amsart}` with T1 fontenc, `amsmath`,
  `amssymb`, `amsthm`, `lmodern`, `geometry` (1in margins), `microtype` and
  `hyperref` (`hidelinks`, with pdftitle and pdfauthor set). Add `mathtools`
  for `\coloneqq`, and `tikz-cd` only if the paper has diagrams.
- Use one shared counter, numbered within sections, for theorem, proposition,
  lemma, corollary, conjecture and question (plain style), definition and
  example (definition style) and remark (remark style) [Poonen 36].
- Theorems A and B in the introduction use a separate lettered environment.
  Restate each in the body as "Theorem 5.1 (= Theorem A)", or prove it there
  with a pointer.
- Declare operators with `\DeclareMathOperator`: Hom, Ext, End, rk, coker,
  Pic, Spec, Proj and so on. Define sheaf versions such as `\sExt` once.
- Use macros for repeated objects (`\PP`, `\OO`, `\Db`). Avoid macros that hide
  mathematics from a coauthor reading the source.
- Write `f\colon X\to Y` and `\coloneqq` [Poonen 39, 42].

## 2. Labels and references

- Use the label prefixes `sec:`, `thm:`, `prop:`, `lem:`, `cor:`, `def:`,
  `ex:`, `rem:`, `eq:` and `app:`. Never hard-code a number.
- Write `Theorem~\ref{thm:main}` with a tie. The house style uses `\ref`. If
  `cleveref` is adopted, use it throughout.
- Number only the equations that are referred to later.

## 3. Bibliography

- The house style is `thebibliography` with alphabetic keys such as `[HP11]`.
  BibTeX with `amsalpha` is also fine; for arXiv, include the `.bbl`.
- Each entry gives the authors with initials, `\emph{title}`, the journal
  abbreviation, `\textbf{volume}` (year) and pages, plus a DOI when one exists.
  An arXiv-only item gives arXiv:YYMM.NNNNNvK and the year [Poonen 11].
- Cite the published version when it exists. Cite a preprint only for a
  statement that exists only there [Poonen 10].
- Cite with locators: `\cite[Theorem~3.5]{HP11}` or
  `\cite[Prop.~2.1]{Har77}`. Cite a whole work only on purpose.
- Every entry matches a dossier row or a hub source record. Never write an
  entry from memory. An unchecked item carries a `[VERIFY: ...]` note in the
  text, and that blocks release.
- Internal drafts may carry `% source: ID` comments. Remove them from the
  release copy, because arXiv distributes the source with its comments.

## 4. Build

```
latexmk -pdf -interaction=nonstopmode -halt-on-error paper-vN.tex > build-vN.txt 2>&1
python3 .agents/skills/math-paper-writing/scripts/check_paper.py paper-vN.tex
pdftoppm -r 80 -png paper-vN.pdf page
```

- The gate requires:
  - no undefined references or citations;
  - no multiply-defined labels;
  - no remaining "Rerun" warning;
  - every overfull box over 1pt either fixed or justified.
- Inspect the title page, the densest theorem page, any diagram page and the
  bibliography.
- Retain the build with `hub.py package`:
  - **source:** the build recipe;
  - **input:** the `.tex`, `.bib`, figures and a `build-environment.json`
    with the latexmk, pdflatex and pdftoppm versions;
  - **output:** the PDF and any inspected PNGs.

## 5. arXiv package and metadata

This section was checked against https://info.arxiv.org/help/prep.html on
2026-10-14.

- **Source package.** The main `.tex`, the `.bbl` if BibTeX is used, and the
  figures. No unused files and no internal comments. Test-compile it in a
  clean directory.
- **Code and data.** These go in `anc/` as ancillary files. The paper says
  what they compute, how to run them, the software versions and whether the
  search was exhaustive.
- **Title.** No all-capitals and no Unicode. Spell out macros.
- **Authors.** "Firstname Lastname", every author listed. Author roles go in
  the comments field.
- **Abstract.**
  - At most 1920 characters, without the word "Abstract".
  - ASCII only, with TeX math through MathJax (`$...$`) and no font or
    spacing commands.
  - Line breaks are stripped unless the next line is indented.
  - Avoid citations.
- **Comments.** For example "23 pages". Add "to appear" information later; it
  is frozen once announced.
- **MSC class.** For example `14F08 (Primary) 14J26, 14J60 (Secondary)`.
- **Category.** Primary math.AG. Cross-list (math.RT, math.CT, math.CO) only
  when the paper is genuinely of interest there.
- **User decisions.** The licence, endorsement, timing and choice of journal.

## 6. AI assistance and responsibility

- arXiv policy (blog post, 31 January 2023): an AI tool cannot be an author.
  The authors are responsible for all content, including errors and
  references, and significant use of such tools should be reported.
- Press reports (TechCrunch, 16 May 2026) say arXiv now imposes a one-year ban
  when there is clear evidence of unchecked LLM output, such as invented
  references, leftover meta-comments or placeholders.
- Journals have their own policies. Check the target journal's author
  instructions before submission.
- Draft a neutral statement for the user to accept, edit or drop, for example
  in the acknowledgements:

  > The author used AI systems ([which]) for [literature search / drafting /
  > checking computations / exposition]. [The author checked ...] The author
  > takes full responsibility for the content.

  Fill the brackets only with what actually happened, and let the user settle
  the wording and placement.
