# Mechanism search: {{title}}

Conductor worksheet, filled in the main session before prospecting. Explorers
never see this file and have no network access. They see only the proof
excerpts you bring back and name in `context_files`.

The aim is a proof mechanism from any field that overcame an obstruction of
the same shape as ours, not more facts about our own objects.

## 1. The obstruction in field-neutral terms

Restate the gap without this project's objects, names or notation.

- **Quantifier shape.** What is given, what must exist, what must hold for
  all, and what must be uniform or canonical.
- **What must be selected or glued.** A choice, a section, a compatible
  family, an extension, a limit, a common refinement.
- **What fails.** The natural construction and the exact point where it
  breaks: a non-canonical choice, an obstruction class, a loss of
  compactness or finiteness, a failure of functoriality, a jump in a family.
- **Neutral sentence.** "Given [local or partial data] with [property],
  produce [global object] such that [conclusion], although [the obvious
  candidate] fails because [reason]."

## 2. Queries

Search the hub and its library first. Then search outside, in several fields
on purpose: algebra, topology, analysis, combinatorics, logic, dynamics,
probability, number theory, theoretical computer science.

Outside queries name shapes and techniques. They contain no unpublished
statement from the hub. Register each one before sending it:

```
python3 tools/prospector.py mechanism RUN query "selection of compatible local sections when the obstruction class is torsion" --where "arXiv"
```

The tool refuses a query that repeats a run of words from the frontier, the
variants or the cards, or that contains a hidden item. That is a screen for
copied text only; keeping the wording neutral is your job. Then record what
came back, including nothing:

```
python3 tools/prospector.py mechanism RUN result q1 --result "..." --source "..."
```

## 3. Candidate mechanisms

One row per proof that overcame an obstruction of this shape.

| # | Source and exact locator | Field | The obstruction there | What the proof does | What it would need here |
|---|---|---|---|---|---|
| | | | | | |

## 4. Bring the proofs back

For each candidate worth trying, save the proof text itself to a file: the
statement and the decisive steps, quoted, with their exact locator. Stamp it:

```
python3 tools/prospector.py mechanism RUN excerpt FILE --title "..." --source "..." --locator "Theorem 3.2 and its proof, pp. 14-16" --query q1 --mechanism "..."
```

Add the path it prints to `context_files` in `run.json` (every slot) or to
one slot's `files`. Put a one-line mechanism summary under "Tools and results
that may help" in FRONTIER.md. The `analogy-transfer` and
`literature-mechanism` lenses work from these excerpts.

## 5. Status of sources

Every source found this way is a lead. The stamp on each excerpt says so.
Before any result relies on one, check the exact statement, version and
hypotheses in the primary source and retain it under the hub's library rules
(`templates/source-check.md`), then stamp the excerpt again with `--checked`.
