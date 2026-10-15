# Keeping the research record useful

## The loop

Retrieve, formulate the missing implication, test controls, investigate,
review, then record what changed. Search prior attempts before starting a long
run. When the session ends, a new conversation should be able to tell what
changed without rereading the old one.

Use one NOTE per investigation. Keep scratch calculations beside it when they
matter, and link full proofs and reproducible outputs from it. Do not write a
report, a handoff, a summary and a continuation with the same content. An
independent review is its own document only when it contains distinct work.

## The catalog

Each project has `catalog/records.json` (claims) and `catalog/sources.json`
(literature). IDs look like `HF-R001` (records) and `HF-L001` (sources), with
the project's prefix. IDs never change and entries are never deleted: a wrong
claim is marked refuted or superseded and linked to its correction.

### Changing the catalog

Never edit the JSON. Write the entry, or a list of entries, to a scratch JSON
file (start from `templates/record.json` or `templates/source.json`) and run:

```
python3 tools/hub.py put /path/to/entry.json
```

- A new entry may use `"id": "auto"`, or an ID reserved earlier with
  `python3 tools/hub.py reserve --project NAME --kind R` (or `--kind L`).
- To change an existing entry, first run `hub.py show ID`, then make the
  change against the version shown and pass the hash it printed:
  `hub.py put entry.json --expect ID=HASH`. If someone changed the entry in
  the meantime, `put` refuses; re-read and merge.
- Evidence hashes may be written as `"sha256": "auto"`; `put` fills them in
  from the current file. Only do this after actually reading that file.
- `put` validates the whole catalog first and writes nothing if the change
  would introduce an error. It keeps a backup in `.hub/history/` and a line
  in `.hub/journal.jsonl`.

### Record fields

| Field | Meaning |
|---|---|
| `kind` | result, conjecture, question, obstruction (a failed approach or a proved barrier), or correction |
| `title` | short and precise |
| `statement` | one precise claim; assumptions separated from conclusions |
| `hypotheses` | a list: field, characteristic, category, dimension, rank, smoothness and so on |
| `limitations` | what this does not establish, including review and novelty gaps |
| `status` | see below |
| `evidence_level` | see below |
| `novelty` | unassessed, known, subsumed, or candidate (a candidate still needs a literature review before anyone calls it new) |
| `evidence` | a list of `{root, path, locator, sha256}`: root is `hub` or an archive alias; locator is the exact theorem, section or lines |
| `depends_on` | records this one logically uses; must not form a cycle |
| `supersedes` | for a correction: the records it corrects or replaces |
| `subsumed_by` | a record that is at least as strong under hypotheses that apply here |
| `source_refs` | literature IDs used |
| `review` | required when verified: `{date, reviewer, scope, artifact}`, with artifact an evidence item pointing at the written review |
| `reopening_condition` | required for an obstruction: what new input would justify trying again |

### Status

- `reported`: an argument or claim exists, in an archive or a NOTE, but no
  independent review is on record.
- `conditional`: proved assuming something still unproved; the hypotheses say
  what.
- `verified`: a fresh reviewer who did not write the proof audited a frozen
  version in the stated scope, and the review is recorded as evidence.
- `open`: a question or conjecture.
- `refuted`: shown false; link the counterexample.
- `superseded`: replaced by a correction that lists it in `supersedes`.

### Evidence level

- `imported`: taken from older material and not rechecked.
- `draft-proof`: a written argument in the hub, not yet independently reviewed.
- `computation-reproduced`: a finite computation rerun with a retained
  package. It certifies only the stated cases.
- `primary-checked`: a literature result checked against the primary source.
- `internally-reviewed`: audited by a fresh AI or human reviewer inside the hub.
- `externally-reviewed`: checked by an outside expert, such as a referee or
  colleague.

A hash proves that a file has not changed since it was recorded. It does not
prove that the file is correct. `check` reports STALE evidence when a file
changes; read the changed evidence and review what depends on it (`impact`)
before recording a new hash.

### Links

`depends_on` expresses a real logical dependence, not topical resemblance.
When a stronger result has a different scope, write the comparison in
`limitations` instead of adding a false `subsumed_by` link. After changing a
claim, run `python3 tools/hub.py impact ID` and review each entry it lists.
The graph only knows what has been recorded, so also search by topic.

## Literature

Each source entry records the citation, URL and version, the exact locator
(theorem number with printed page), the statement as checked, its use here,
its limitations and its `check_state`:

- `lead`: recalled or found in a search, not yet checked. Never cite a lead
  as established.
- `imported`: checked in older material, not rechecked here.
- `primary-checked`: checked against the primary source using
  `templates/source-check.md`; the completed check is the evidence.

Keep PDFs of papers in `library/`. A source that is checked for one use is
not automatically checked for another: compare the hypotheses each time.

## Controls and proof review

For each proposed implication, test known examples and local countermodels.
Say whether each control is an actual object of the stated kind, a simpler
model, or only numerical data. A failure in one category invalidates only the
inferences that use the corresponding hypotheses.

When reviewing, look for: missing hypotheses, hidden equivalences, circular
reasoning, local versus global, special versus general position, quantifier
order, and whether the result is already known or subsumed. Stop a failed
approach at its precise failure and write down the new hypothesis that would
reopen it.

## Computations

Package a computation's exact bytes before recording it as evidence:

```
python3 tools/hub.py package spec.json --project NAME \
    --dest projects/NAME/investigations/DATE-topic/runs/RUN-NAME
```

`templates/computation-spec.json` shows the spec: the command, environment
(software and versions), domain, exhaustiveness, optional seed, and the files
with their roles (`source`, `input`, `output`). The tool copies the files,
hashes them, writes `manifest.json` and never overwrites. It does not run any
code. Register the manifest as evidence of the record.

## Closeout

1. Finish the NOTE's result and closeout sections.
2. Record changed entries with `put`, after reviewing `impact` for each
   changed existing entry.
3. Update the project TOPICS.md row and CHANGELOG.md.
4. Run `check --project NAME` and `render --project NAME`.
5. Commit locally if the hub uses git.
