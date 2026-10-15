# A research hub for mathematicians: setting up AI assistants that remember, check and build on your work

*A plain-English guide. No programming or AI experience needed. Setup takes
about thirty minutes and one command.*

---

## Who this is for

You are a mathematician. You may have tried a chatbot: asked it about a lemma,
got a fluent answer, and wondered how much of it to believe. Perhaps it cited a
theorem that does not exist. Perhaps a week later it had forgotten everything
you discussed.

This guide shows how to set up something different: a **research hub**. It is
a folder on your computer where AI assistants work on your research under a
fixed set of rules. The rules make them search before inventing, check
citations against the actual papers, record failed attempts so nobody repeats
them, and have every claimed proof audited by a reviewer that did not write it.
Everything they learn is written down in plain files you can read, so the next
session starts where the last one stopped.

The design comes from a hub in daily use for research in algebraic geometry. It
is built for research output: the assistants generate and develop ideas
aggressively, and the rules keep the record honest about what has actually
been established.

What this guide does not promise: the assistants do not replace your judgement,
do not certify proofs, and can be wrong. The hub makes their errors easier to
catch. It does not make them disappear.

---

## Part 1. What is an "AI agent"?

A chatbot is a conversation in a web page. An **agent** is the same kind of AI
model given hands: a program that runs on your computer, inside one folder,
and can

- read and write files in that folder (notes, TeX, code);
- run programs (Python, Sage, Macaulay2, Magma, pdftotext, LaTeX);
- search the web and read papers;
- start helper agents for side questions.

You talk to it in ordinary English in a **terminal**, the text window where you
type commands. It shows each action it wants to take and, by default, asks
your permission before running a command or changing a file.

The two agents this hub supports are:

- **Claude Code**, from Anthropic. It needs a paid Claude plan (Pro, Max,
  Team or Enterprise) or an Anthropic API account. The free plan does not
  include it.
- **Codex**, from OpenAI. It needs a ChatGPT plan that includes Codex (Plus,
  Pro, Business, Edu or Enterprise) or an OpenAI API key.

Either one is enough. With both, each can review the other's proofs, and two
different model families catch more errors than one.

The AI model itself runs on the company's servers. When the assistant reads
one of your files, that file's contents are sent to the company. Part 7 says
more about what this means for confidential material.

---

## Part 2. What goes wrong without a hub

If you have used a chatbot for mathematics, you will recognize these.

1. **It forgets.** Each conversation starts from nothing. You re-explain your
   setup, your notation and last week's dead end.
2. **It is confidently wrong.** It states false lemmas fluently and cites
   theorems with the wrong hypotheses, or theorems that do not exist.
3. **It grades its own homework.** Ask it whether its proof is right and it
   tends to say yes.
4. **It repeats failures.** It proposes again the approach that collapsed last
   month, because nothing recorded why it collapsed.
5. **Labels drift upward.** "This might work" becomes "we showed" and then
   "it is known that".

Each piece of the hub is aimed at one of these problems.

---

## Part 3. The design in five ideas

### 1. A folder, not a chat

Everything lives in ordinary files in one folder: plain-text notes, a catalog
of results, PDFs of papers, your code. You can open any of it in a text
editor. The conversation is disposable and the folder is the memory. A new
session reads a short guide and knows where the research stands.

### 2. A rulebook read at the start of every session

The folder contains a rulebook (`AGENTS.md`) that both Claude Code and Codex
load automatically. It says things like:

- search the hub before proposing anything, including corrections and failed
  attempts;
- state every hypothesis: the field, the characteristic, the category of
  objects;
- a theorem recalled from memory is a lead, not evidence: check the exact
  statement in the primary source, or say it is unchecked;
- a finite computation proves only the cases it ran;
- label every unproved step as an assumption or a conjecture;
- never post, submit or email anything without your explicit instruction.

Each project also has its own short rules file listing the conventions in your
field that must always be explicit. For example: "smooth vs. singular",
"sheaf vs. vector bundle", "over an algebraically closed field of
characteristic 0 unless stated".

You can edit the rulebook. It is plain English.

### 3. A catalog with honest labels

Results go into a catalog, one entry per claim, each with an ID such as
`HF-R007`. Every entry keeps four questions apart:

| Question | Recorded as |
|---|---|
| Is it true? | **status**: reported, conditional, verified, open, refuted, superseded |
| Who checked it, and how much? | **evidence level**: imported, draft proof, computation reproduced, checked against the primary source, internally reviewed, externally reviewed |
| Is it new? | **novelty**: unassessed, known, subsumed, candidate |
| Does it advance the goal? | the project guide and topic map |

"Verified" has a strict meaning: a reviewer who did not write the proof
audited a frozen copy of it, and the review is on file. A label is a pointer
to evidence, never a substitute for it.

Entries also record their logical links: what each result depends on, which
correction replaces which claim, and which paper is cited. So when a lemma
turns out to be wrong, one command lists everything built on it.

The catalog is changed only through a small tool that checks every change
first. It refuses a change that would break a link, mark something verified
without a recorded review, or overwrite an edit someone else made in the
meantime. It also notices when a file cited as evidence has changed since it
was recorded.

### 4. A lab notebook for every question

Each investigation gets one notebook file (`NOTE.md`) with a fixed shape:

- the exact question, with all hypotheses;
- **the one missing implication** that blocks progress;
- what was searched and found, including failed attempts;
- **control examples**, with which hypotheses each one satisfies;
- the work, with each step marked proved, assumed, conjectural or refuted;
- the result, what failed, and what new input would reopen it.

The last item matters most. A failed attempt is recorded as a fingerprint:
the statement, the hypotheses, the mechanism, where it broke, and what would
have to change to try again. The rules forbid retrying a recorded failure
without that new input. They also say that one failed mechanism does not
close a whole direction.

### 5. A small team with a referee

For serious proof work, the session you talk to acts as the **proof
conductor**. It develops the argument itself and can call on five specialist
helpers, each given one narrow question:

| Helper | Job |
|---|---|
| archive scout | find earlier results, corrections and failed attempts in the hub and in your old files |
| source checker | find a theorem in the literature, or check whether a specific theorem really applies |
| proof explorer | make one bold attempt at the missing step |
| counterexample checker | test a step against examples that satisfy all of its hypotheses |
| proof checker | audit a frozen proof, including the steps that connect the lemmas |

Three rules make this work. The author of a proof never reviews it. The
reviewer gets the written proof but not the author's enthusiasm or earlier
verdicts. And the reviewer checks a frozen copy, identified by a fingerprint
of its exact contents, so nobody can quietly edit the proof during the review.

The team is deliberately small: by default at most three helper jobs per
request in Claude Code (one kept for review) and six in Codex (two for
review), with no more than two running at once. Small teams with focused
questions produce more than large panels arguing in circles, and they use
less of your subscription.

### Also included

- **Your earlier work becomes searchable but untouchable.** You register
  folders of old notes, TeX files and code as read-only archives. The
  assistants search them, the rules forbid changing them, and in Claude Code
  the settings also block its editing tools there.
- **A library for papers.** Put PDFs in `library/`, and the assistants check
  theorem statements against them rather than against memory.
- **Computations are kept exactly.** One command stores the exact code, input
  and output of a computation, with the software versions and how exhaustive
  the search was.
- **A paper-writing guide.** When results are ready, a built-in skill plans
  and drafts an amsart paper from the catalog. It keeps unreviewed claims out
  of the theorem environments and refuses unchecked citations.

---

## Part 4. What you need

- **A Mac or a Linux computer.** On Windows, first install WSL (Windows
  Subsystem for Linux, a free Microsoft component) and use its Ubuntu
  terminal.
- **A subscription** to Claude (Pro or above), to ChatGPT (Plus or above), or
  to both. Prices and usage limits change, so check the current plans. Both
  companies limit how much you can use per few hours; the hub's small-team
  defaults are designed with those limits in mind.
- **Python 3.9 or newer.** Most Macs have it once Apple's free developer tools
  are installed: run `xcode-select --install` if the installer asks.
- **About thirty minutes.**

Optional extras the installer can set up: `pdftotext` (so the assistants can
search PDFs), git (an "undo history" for the whole folder) and LaTeX (for
papers).

---

## Part 5. Installing

### Open a terminal

- **Mac:** press Cmd+Space, type `Terminal`, press Return.
- **Linux:** open the "Terminal" application.
- **Windows:** open "Ubuntu" from the Start menu (after installing WSL).

### Run the one command

Copy this line, paste it into the terminal, and press Return:

```
curl -fsSL https://raw.githubusercontent.com/ilpenzo/math-research-hub/main/install.sh | bash
```

This downloads the installer and runs it. If you prefer to read a script
before running it, which is a good habit, download the repository from
https://github.com/ilpenzo/math-research-hub, open `install.sh` in a text
editor, and then run `bash install.sh` from that folder.

### Answer the questions

Press Return to accept the suggested answer shown in [brackets].

1. **Your name.** Used in notes and the changelog.
2. **Where the hub lives.** The default is a folder called `research-hub` in
   your home folder.
3. **Your first project.** A title such as "Rationality of cubic fourfolds",
   a short folder name (suggested for you), and two to five capital letters
   used to label its results (`RCF-R001`, ...). You can add projects later.
4. **The main question,** in a sentence or two. Optional.
5. **Conventions.** The things in your field that must never be left implicit,
   one per line: the base field, smoothness, the category of objects,
   normalizations. Optional, but worth five minutes. Every helper brief
   repeats them.
6. **Earlier work.** Folders of notes, TeX, code or PDFs to register as
   read-only archives, one per line. Optional.
7. **Which assistants.** Claude Code, Codex or both. If one is not installed,
   the installer offers to install it using the company's official installer.
8. **PDF search.** On a Mac with Homebrew it offers to install `pdftotext`.
9. **Codex trust** (if you chose Codex). Whether to mark the hub as a
   trusted folder in your Codex settings, so Codex loads the hub's helper
   roles. The installer backs up that settings file first.
10. **Version history.** Whether to keep a local git history (recommended;
   nothing is uploaded).

The installer then shows a summary and asks for confirmation. It creates the
folder, runs a self-test and prints your next steps. It changes nothing else
on your computer except the installations you agreed to.

---

## Part 6. Your first session

1. Open a **new** terminal window, so that newly installed programs are found.
2. Go to your hub and start an assistant:

   ```
   cd ~/research-hub
   claude
   ```

   (or `codex` for Codex).
3. **Log in.** The first time, Claude Code opens your browser to sign in to
   your Claude account. Codex asks you to choose "Sign in with ChatGPT".
4. **Trust the folder.** Claude Code asks whether you trust the files in this
   folder. Answer yes: until you do, it ignores the hub's settings, including
   the protection of your archives. Codex likewise loads the hub's settings
   and helper roles only for a trusted folder. The installer offers to mark
   the hub as trusted for Codex, and if Codex asks anyway, answer yes.
5. **Say hello.** Paste:

   > Read START_HERE.md and AGENTS.md, then give me a ten-line tour of this hub
   > and suggest what I should do first.

### Permission prompts

The assistant asks before running commands or editing files. Read each
request. A reasonable policy:

- **Approve** the hub's own commands (`python3 tools/hub.py ...`; in Claude
  Code the common read-only ones are pre-approved), reading files, and edits
  to notes inside the hub.
- **Think before approving** anything that installs software, deletes files,
  or reaches outside the hub folder.
- **Decline** anything you did not ask for. The assistant will adjust.

To stop the assistant mid-task, press Esc. To leave, press Ctrl+C twice.
Nothing is lost when you quit, because the work is in the files.

### What the installer created

```
research-hub/
  START_HERE.md      the front page: your projects and the main commands
  AGENTS.md          the rulebook every session reads
  WORKFLOW.md        how the catalog and labels work
  PROOF_SWARM.md     how the proof team works
  PROMPTS.md         ready-to-paste requests (start here when unsure)
  projects/
    your-project/
      START_HERE.md  where the project stands
      AGENTS.md      the project's conventions
      TOPICS.md      one row per line of attack
      CHANGELOG.md   what changed, when
      catalog/       the results and literature (changed only by the tool)
      investigations/  one folder and NOTE.md per question
      indexes/CATALOG.md  a readable table of the catalog
  library/           PDFs of papers you cite
  templates/         shapes for notes, records and helper briefs
  tools/hub.py       the small tool behind the catalog and search
  .claude/ .codex/   settings and helper roles for the two assistants
```

---

## Part 7. Working with the hub

`PROMPTS.md` in your hub has ready-to-paste requests. A typical sequence:

**Bring in your earlier work.** In two steps, so you see the list before
anything is recorded:

> In project [name], search the archives for the main results, conjectures,
> computations and failed approaches in my earlier work. Do not record
> anything yet. Give me a numbered list with exact file locations.

then

> Record items 1, 3 and 4 as "reported" with evidence level "imported" and
> exact locators. Record item 5 as an obstruction with its reopening condition.

**Brainstorm.**

> Give me five genuinely different strategies for [question]. For each, give
> the mechanism, the first calculation that would test it, and where it is
> most likely to fail. Search the hub first. Label everything conjectural.

**Investigate one question.**

> In project [name], start an investigation of [question]. Search first, then
> fill in the NOTE's Question and Prior work sections, and tell me the one
> missing implication before doing any proof work.

**Run the proof team.**

> Use the proof swarm on [statement or record ID] in project [name]. Aim for a
> proof or a rigorously checked partial result. Stop after 2 hours.

**Check a citation.**

> Check whether Theorem 3.5 of [paper] applies to [our object], using the PDF
> in library/. Compare every hypothesis, then record the source.

**Close the session.** This step is what makes the next session productive:

> Close out this session following AGENTS.md, then give me a five-line summary
> of what changed.

**Write it up,** when results have been reviewed:

> Using the math-paper-writing skill, plan a short paper on [IDs]. Show me the
> one-page outline before drafting anything.

### Habits that help

- Name the project and say what you want back.
- Ask "Which of these steps are proved, assumed or conjectural?"
- When something sounds too good, ask "Who checked this, and what exactly did
  they check?"
- Start a fresh session for unrelated work. The hub carries the memory, so
  you lose nothing.
- Read the reviews of anything you intend to rely on. A reviewer's "valid" is
  evidence, not a guarantee.

---

## Part 8. Limits and cautions

**The assistants can be wrong, including the reviewers.** Independent review
catches many errors, but two AI reviewers can share a blind spot. Treat
"verified" as "survived an independent audit of the stated scope", read the
argument yourself before you put your name on it, and seek human review for
anything that matters.

**You are the author.** Journals, arXiv and funding bodies have policies on
AI assistance, and they change. Check the current policy wherever you submit,
and disclose AI use as it requires. The paper-writing skill drafts a
disclosure statement but never decides it for you.

**Confidentiality.** Files the assistant reads are sent to the AI company.
Check your account's privacy and data-use settings. Do not put refereeing
material, manuscripts shared with you in confidence, student information or
passwords into the hub.

**Novelty.** A search that finds nothing proves nothing. The tools say so
every time. A literature check covers only the searches actually made.

**Cost and limits.** Subscriptions have usage limits that reset over hours or
days. Long proof campaigns use more. The defaults (a small team, short
briefs) keep usage modest; you can raise them per request ("budget: 6 helper
jobs").

**Software changes.** Both assistants update often, and a new version can
change how they load a folder's settings. The repository includes a short
check (`tests/live_check.sh`) that asks each installed assistant what it can
see in a fresh hub. If a helper role ever goes missing, the rules fall back
to an ordinary helper briefed with the role's text. The workflow still runs,
but the helper's read-only restriction is then a matter of instruction
rather than enforcement.

---

## Part 9. Troubleshooting

- **"command not found: claude" (or codex).** Open a new terminal window. If
  it persists, add `export PATH="$HOME/.local/bin:$PATH"` to the file
  `~/.zshrc` (Mac) or `~/.bashrc` (Linux), then open a new window.
- **The installer says Python is missing.** On a Mac run
  `xcode-select --install`; on Ubuntu run `sudo apt install python3`. Then run
  the installer again.
- **The assistant edits something it should not.** If you chose git, ask it
  to "show me what changed since the last commit and undo the edit to [file]".
  The catalog also keeps automatic backups in `.hub/history/`.
- **Something seems broken.** Run `python3 tools/hub.py doctor` and
  `python3 tools/hub.py check` in the hub folder, and paste the output to the
  assistant.
- **Adding a project later.** Ask: "Create a new project 'short-name' with
  prefix XY about [question]."
- **Adding a folder of old work later.** Ask: "Register [folder] as a
  read-only archive for project [name]."

---

## Glossary

- **Terminal:** a window where you type commands instead of clicking.
- **Command:** a line of text you type into the terminal and run with Return.
- **Folder path:** a folder's address, like `~/research-hub`, where `~`
  means your home folder.
- **Agent:** an AI model that can act (read, write, run programs), not just
  talk.
- **Session:** one conversation with an agent. The hub's files persist
  between sessions; the conversation does not.
- **Helper (subagent):** a second agent started by the first one for a narrow
  question, with a fresh memory.
- **Model family:** the AI behind an assistant (Anthropic's Claude models for
  Claude Code, OpenAI's models for Codex).
- **Hash (sha256):** a fingerprint of a file's exact contents. If one
  character changes, the fingerprint changes. The hub uses hashes to notice
  when cited evidence has changed, not to judge whether it is correct.
- **git:** a program that keeps every past version of a folder, so changes
  can be undone. Everything stays on your computer unless you choose to
  upload it.

---

*The kit is open source:
[github.com/ilpenzo/math-research-hub](https://github.com/ilpenzo/math-research-hub).
It is plain text and a single standard-library Python file, so you can read
everything it does.*
