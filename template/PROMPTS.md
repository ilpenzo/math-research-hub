# Things to say to your assistant

Copy a request, replace the parts in [brackets], and paste it into Claude Code
or Codex running in this folder. Plain English works; these wordings just make
sure the assistant follows the hub's rules. Always name the project.

## Getting started

**Tour.**
> Read START_HERE.md and AGENTS.md, then give me a ten-line tour of this hub
> and suggest what I should do first.

**Fill in the project's conventions.**
> Help me fill in the "Mathematical conventions" and "Success threshold"
> sections of projects/[project]/AGENTS.md. Ask me one question at a time,
> then show me the text before saving it.

**Import earlier work (two steps).**
> In project [project], search the archives for the main results, conjectures,
> computations and failed approaches in my earlier work. Do not record
> anything yet. Give me a numbered list with exact file locations and what
> each one claims.

and then:

> Record items [1, 3, 4] in the catalog as status "reported" with evidence
> level "imported" and the exact locators. Record item [5] as an obstruction
> with its reopening condition. Then run check and render.

**Add a folder of old work later.**
> Register [~/Documents/old-notes] as a read-only archive for project [project].

**A new project.**
> Create a new project "[short-name]" with prefix [XY] titled "[title]" about
> [question]. Then help me fill in its conventions.

## Doing research

**Where do we stand?**
> Where does project [project] stand? Read its START_HERE, TOPICS and catalog
> index and summarize the established results (with their status), the open
> implications, and the most promising next step.

**Brainstorm.**
> Give me five genuinely different strategies for [question] in project
> [project]. For each, give the mechanism, the first calculation that would
> test it, and where it is most likely to fail. Search the hub first so you
> do not repeat recorded failures. Label everything conjectural and record
> nothing.

**Start an investigation.**
> In project [project], start an investigation of [question]. Search first,
> then fill in the NOTE's Question and Prior work sections, and tell me the
> one missing implication before doing any proof work.

**Proof swarm.**
> Use the proof swarm on [statement or record ID] in project [project]. Aim
> for a proof or a rigorously checked partial result. [Economy mode / Standard
> mode / Budget: 5 helper jobs.] [Stop after 2 hours.]

**Timed session.**
> Work on [question] in project [project] for [3 hours]. Record the objective,
> start and deadline in UTC in the NOTE, keep checkpoints, and save the last
> quarter for review and closeout.

**Review my own proof.**
> I wrote a proof in [path]. Freeze it (record its sha256 in a NOTE) and have
> a fresh proof-checker audit the whole argument, including the connecting
> steps. Do not pass on my comments or anyone's earlier opinion of it.

**Look for a counterexample.**
> Use the counterexample-checker to stress-test [step 4 of the NOTE / the
> claim that ...]. Every hypothesis must be checked for any example it
> proposes.

**Check a citation.**
> Check whether [Theorem 3.5 of Author, Title] applies to [our object], using
> the PDF in library/[file]. Compare every hypothesis, then record the source
> with its exact locator and check state.

**Is this known?**
> Use the source-checker to look for [statement] in the literature. Report
> primary sources with exact theorem numbers, and tell me what you did not
> search. Record anything relevant as a lead.

**A computation.**
> Write and run a [Sage / Macaulay2 / Python] script that [does X] in a run
> folder inside the current investigation. Package the code, input and output
> with hub.py package, and record the result with evidence level
> "computation-reproduced" and a scope of exactly what ran.

## Finishing

**Close out.**
> Close out this session following AGENTS.md: finish the NOTE, record new and
> changed entries, review impact, update TOPICS and CHANGELOG, run check and
> render, then give me a five-line summary of what changed.

**Write a paper.**
> Using the math-paper-writing skill, plan a short paper on [IDs] from project
> [project]. Show me the one-page spine before drafting anything.

**Second opinion from the other AI** (if you use both).
> I allow one cross-model review for this task. Following PROOF_SWARM.md, ask
> [Claude / Codex] for a read-only audit of the frozen proof [path], and
> record its answer in the NOTE.

## Habits that help

- Name the project and say what you want back (a list, a NOTE section, a
  record).
- Ask for status labels: "Which of these steps are proved, assumed or
  conjectural?"
- When something sounds too good, ask "Who checked this, and what exactly did
  they check?"
- Start a fresh session for unrelated work; the hub, not the chat, carries
  the memory.
- End substantive sessions with the close-out request above.


## Return to an earlier conversation

> Recover our previous work on this question in this service. Tell me the
> direction, decisions, results with their status, open gap and proposed next
> step. Check for newer corrections and tell me whether native history needs
> catch-up. Wait for my current direction before starting a new investigation.

## Look for a connection

> Search the hub and prepare a brief for this gap. Compare the exact objects,
> hypotheses and maps of two plausible ingredients. Identify the first missing
> condition, try a bounded decisive check, and save its outcome and reopening
> condition without changing a theorem status.
