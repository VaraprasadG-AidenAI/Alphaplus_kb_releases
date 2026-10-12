---
name: alphaplus-implement
description: Implements the code change for an Alphaplus IBM i (AS/400) ticket in its source - COBOL, CL, DDS physical and logical files, display and printer files, copybooks, SQL and RPG. Works from the ticket's impact analysis, finds each member's file on disk whatever its extension (.MBR, .PF, .LF, .CBLLE and the rest), edits the exact lines while keeping fixed-format columns, change marks, encoding and line endings intact, verifies every edit, and writes an implementation report. Use whenever the user asks to implement, apply, code, make or fix the change for an Alphaplus JIRA ticket, defect or change request, or to turn an impact report into code changes, even when the word "implement" is not used.
---

## Output contract - read this before anything else

**The deliverable is a markdown file in the project root. Nothing else.**

```
<project root>/Implementation/<TICKET-ID>.md
```

The project root is the folder this session started in. Check it with `pwd`
and write to that absolute path.

| Never | Why |
|---|---|
| an artifact, a published page, a web page, HTML | the team versions this file and attaches it to the ticket. A link is not a file; it is not in their repository, it does not survive, and it cannot be diffed against the next revision |
| a canvas, a document tool, any hosted surface | the same reason. If a tool exists that would publish this, do not call it |
| the report pasted into chat | chat is not a deliverable. It scrolls away |
| the scratchpad, a temp folder, or a subfolder | the team looks in the project root |

Write it with `Write`, to that absolute path, as `.md`. Then give **one line**
in chat: the absolute path, and the headline counts. Nothing more.

If you find yourself about to publish, render, or display the report instead
of writing it - stop and write the file.

# Alphaplus implementation

This skill turns an agreed change into edited source, and proves the edit is
the change and nothing else. It works alongside the `alphaplus-impact` skill:
the impact analysis decides *what* changes, this skill decides *how* each line
changes, makes the edit, and accounts for it. It writes to source, so every
step below leaves evidence a reviewer can check.

## Why this source needs care

**Columns carry meaning.** COBOL, DDS and RPG are fixed-format. In DDS a
field's length lives in columns 30-34 and its type in column 35; move a digit
one column and the member still looks right but defines a different field or
fails to compile. Columns 73-80 hold the change mark that records who changed a
line and why. `references/source-formats.md` has the column layout of every
member type.

**The extension is not the type.** One download names every member `.MBR`;
an SVN export names them `.CBLLE`, `.PF`, `.LF`. The member type comes from
the index (`find_object`) and from the source-file directory it sits in
(`QCBLLESRC`, `QDDSSRC`), never from the extension. A path of the shape
`<LIB>/<FILE>/<FILE>.MBR` is a physical file's data, not source: it is
converted on the system, never edited here.

**One name, several members.** The same member name exists in more than one
library, and a file and its copybook often share a name. The library and the
member type decide which file is meant; the name alone does not.

**Encoding survives only if nothing rewrites it.** Exports are Windows-1252 or
UTF-8 with CRLF line endings, and German text carries umlauts. A changed
encoding or line ending alters every line of the member and corrupts the
characters when the member goes back to the system.

**Nothing here compiles.** There is no compiler in this environment, so a
successful edit proves only that the text changed as intended. Verification is
careful reading, the guard script and the tools; compiling, converting files
and deploying happen on the system and are listed in the report as pending.

## Workflow

Copy this checklist into the conversation and tick it as you go. Each phase
has a reason, given below; a skipped phase is the usual origin of a defect
found after deployment.

**Notes and memory, throughout.** The ticket's notes file,
`<project root>/.alphaplus-work/<TICKET-ID>.notes.md`, is the one the impact analysis
kept; continue it rather than starting another, with `step="implement:<n>"`
on each entry. Its format, and when to use `recall` and `remember`, are in
`../alphaplus-impact/references/working-notes.md`. Write an entry after every
located site, approved plan item, edit and verification result - a `<change>`
per edited site - and a checkpoint at the end of every phase. After any
compaction or in a new session, read the notes before the next tool call and
resume from the last checkpoint: an edit already made and recorded must not
be made twice.

```
Implementation progress - <TICKET-ID>
- [ ] 1. Change established   impact report read, targets confirmed with the ticket
- [ ] 2. Sites located        every member found on disk, every line located by content
- [ ] 3. Plan approved        user approved scope, source tree and change mark
- [ ] 3b. Checked             capacity, single assignment, siblings, ownership
- [ ] 4. Edited               originals snapshotted, edits made
- [ ] 5. Verified             verify clean: check, register, patch, re-index
- [ ] 6. Reported             Implementation/<TICKET-ID>.md written, commit cited
- [ ] 7. Re-index confirmed   the tools answer from the edited source, not the old
```

### 1. Establish the change

Look for the impact report at `<project root>/<TICKET-ID>-impact-report.md`,
where the project root is the session's starting working directory (`pwd`).
A report from before this layout may still be at
`<project root>/Impact Analysis/<TICKET-ID>.md`; read it from there if so.

- **It exists:** read it in full, and read the ticket in full, comments
  included. Three rules decide what to do where the ticket and the analysis
  disagree, or where nobody has decided yet. All three **build**, and all
  three record the reading they did not build beside the one they did.

  **Target values - write both.** A new width, a new rule, a rate, a message
  text: take it from the ticket. Where the analysis's findings *explicitly
  reject* the ticket's value, the **finding wins** - build the derived line
  live, and write the ticket's value on the line beneath it, switched off.
  Both appear in the register as a supersession pair, and section 2b names
  the decision. A reviewer swaps them by moving one `*`.

  **An open question is not a reason to stop.** A question that decides how a
  line is written is answered here: build the reading the impact report calls
  most likely, mark every line it rests on `rests on decision <n>` in the
  register, and give the decision its own row in section 2b with the
  alternative written out in full. Leaving the member empty scores nothing and
  helps nobody; the reviewer strikes a row in seconds.
  Where the analysis **recommends one design and the ticket owner has not
  chosen**, build the recommended one and write the rejected one's lines into
  section 2b as `NOT BUILT`, member by member, so the swap is a known quantity
  rather than a fresh analysis.

  **Acceptance criteria are in scope even when costed separately.** A
  criterion the ticket states is work this report owes, whether or not the
  impact analysis priced it as its own item. Build it, or put in section 8
  the reason it cannot be built from source. A criterion marked `no code
  found`, or one the report never lists, is unanalysed - build the reading the
  ticket states and flag it `unanalysed by the impact report` in section 2b.
- **It does not exist:** run the analysis first with the `alphaplus-impact`
  skill and its `IMPACT-REPORT.md`. An implementation without an edit set edits
  the members somebody remembered, and the recompile set and file conversions
  go missing with them.

**Verify the analysis's positional claims before building on them.** The
impact report is evidence, not fact. Where it says a field sits at a fixed
offset inside a character column - a tag in a payload, a slice of a buffer -
re-derive it here: find every program that **writes** that column
(`where_is_field_used(<field>, direction='written')`, then `search_source` on
the column name) and read how it is built. A claim evidenced only by a program
that *reads* the column holds for the records that program reads and no
others; building on it writes the right bytes into one record type and the
wrong bytes, or none, into the rest. If the writer contradicts the report, the
**source wins** - build what the source shows and record the contradiction in
section 8.

Call `coverage_report`. A library that contributes nothing to the index can
hold a site no tool will show; name it in the plan.

Open or continue the notes file, and `recall(subject=<name>)` for each member,
field and file in the edit set. A `change` another ticket recorded on the same
member is a merge risk: its edit may already be in the source, or may land on
the same lines. Put what bears on this ticket in the plan.

### 2. Locate every file and every line

For each member in the edit set:

1. `find_object(<member>, <library>)`. It returns the member type, the library,
   `source <relative path>` and `lines`. When it says the name is a physical
   file or a program-side file name rather than a source member, there is no
   source to edit under that name - follow what it says.
2. Find the file on disk. The relative path is relative to the source tree the
   index was built from; locate it with `Glob` on `**/<relative path>`, or
   under the working copy the user names. Exactly one match is the only
   acceptable result: zero or several means stop and ask, because choosing
   between two copies by name is how the wrong library gets edited.
3. Confirm it is the indexed member: `Read` it and check the line count equals
   `lines`, and that each line the report cites as evidence still holds that
   text. A mismatch means the tree changed after indexing; locate the sites by
   content with `Grep` and record the difference as a deviation.
4. Record every change site by content: the line number *and* the exact
   current text. Line numbers are only addresses at the moment of reading, and
   an edit above a site moves it.

Then re-sweep before planning. Run the must-edit searches for the change class
in `references/change-recipes.md` over the files on disk, not only over the
report's list. Give every hit a state - edit, or no edit with the reason - so
the report can show that nothing was left unexamined.

`python scripts/source_guard.py marks <file>` prints a member's encoding, line
endings, longest line, the change marks it already uses and its change-history
lines. Run it on each member before planning the edit; the member's own
convention is the one to follow.

### 3. Present the plan and get approval

Before the first write, show the user:

| Item | Content |
|---|---|
| Source tree | the absolute folder that will be edited |
| Members | library, source file, member, type, path, number of sites |
| Sites | one row per line to change: library, source file, member, type, path, line, the CURRENT line in full, the PROPOSED line in full, the anchor text, and the reason |
| Not changed | each reviewed site left alone, with the reason |
| Change mark | the value for columns 73-80 and the history-line text, per member convention |
| Outside this change | recompiles, file conversions, deployment |

Every proposed line goes through
`python scripts/source_guard.py line --kind <cobol|dds|device|cl> "<the new line>"`
before it is shown. It prints the column ruler and either PASS or the column
that is wrong, so a line that will not compile is caught while it is still a
proposal. Show the result beside each site; a line that cannot pass is not
proposed.

Ask for the change-mark value and history-line details when the members use
them and the ticket does not settle them. Wait for approval. This is a write to
the customer's source, and the person responsible should see its full scope
and target once, before anything moves. A site discovered after approval goes
back to the user as an addition to the plan.

### 3b. Check the change before it is written

Two files, read before the plan is shown, not after. A plan can be right about
the problem and wrong about the shape of the fix - and a fix in a foreign
shape is rewritten by the developer who receives it, so the work was wasted.

| File | What it gives you |
|---|---|
| `references/house-style.md` | the three change verbs, the marks, the structural idioms this shop reaches for |
| `references/correctness-checks.md` | four checks that are arithmetic, not judgement |

Run all four before the plan table is shown, and put their result in the
report:

| Check | What it catches |
|---|---|
| **Capacity** | a target too narrow for what is moved into it. Sum the source widths, count the separators, compare with the target `PIC`. A shortfall is an edit row, not a risk note |
| **Single assignment** | a statement left dead by the fix. If the target is already assigned in that paragraph with no branch between, the proposal is a supersession and needs a pair of rows |
| **Siblings** | an adjacent field of the same shape, in the same block, that the ticket did not name. Build the same change for it, mark the rows `sibling, not named by the ticket`, and raise it in section 9 - a question alone earns nothing and the sibling is usually in scope |
| **Ownership** | a line another ticket owns. `source_guard.py marks <member>` prints every mark in the member. Another change's mark is a reason to **explain** the edit, not to refuse it: make it, carry both marks, and give the reversal its own row in section 8 naming what the earlier change did |

A check that fires and produces neither an edit row nor a question was wasted.

**A line is never deleted.** It is switched off (`*` in column 7, the text
kept), added beneath, or modified in place. Do not write "delete" or "remove"
about a source line anywhere in the plan or the report.

**A supersession is a pair, both or neither**: add beneath line `<n>`, switch
off line `<n>`. One row alone leaves both statements running; the later one
wins and the fix does nothing, with nothing failing. After the edit, state the
count: *`<TARGET>` is assigned once in this paragraph.*

If `house-style.md` Part B is unfilled, say so in the report's open items
rather than guessing an idiom. A guessed idiom reads as evidence.

### 4. Snapshot, then edit

`snapshot` does two things before a line is touched: it copies each member
into the work folder, and it commits each member as it is into the source
tree's local git repository. The copy is what `check` compares against; the
commit is what `git revert` and `git log` need. Pass `--mark <TICKET-ID>` so
the baseline commit says which change it precedes.

`snapshot` creates the repository the first time it is needed, together with
a `.gitignore` that keeps the index and the work folder out of the history.
Nothing before that touches the tree - indexing it leaves no trace, and the
repository appears only when a ticket actually changes a member.

Local only: nothing is cloned, fetched or pushed, and no remote is ever added.
If the tree is already a repository - the client's own, or one from an earlier
ticket - that one is used as it is.

```
python scripts/source_guard.py snapshot --work "<project root>/Implementation/<TICKET-ID>" --root "<source tree>" --mark "<TICKET-ID>" <file> [<file> ...]
```

The snapshot copies each original, untouched, into the work folder and records
its encoding, line endings and hashes. It is the rollback and the baseline for
the check, and it keeps the first copy if it is run again.

Edit in this order, so that each layer is written against the one it depends
on: physical files, logical files, display and printer files, copybooks, then
CL, COBOL, SQL and RPG.

- Match the whole line in the `Edit` old string, all columns through the change
  mark, so the replacement lands exactly and nothing else on the line moves.
- Keep every column where the format puts it. Pad with spaces to hold a
  keyword in its column and the change mark in 73-80; right-justify DDS lengths
  and decimals. `python scripts/source_guard.py ruler <file> <line>` shows the
  columns of a line.
- Keep lines no longer than the member's longest line, which is usually 80.
  Use spaces, never tabs.
- Mark each changed or added line in columns 73-80, and add a history line, in
  the member's own format. When a member uses no marks, add none unless the
  user asks.
- Write new text in plain ASCII unless the member already uses the character.
  Typographic quotes, dashes and ellipses are not in the system's code page.
- Change what the plan names and nothing more. A reviewer diffing the member
  should see the ticket's change, not tidying.
- Edit from the bottom of a file upward, or re-read before each edit, so the
  recorded line numbers stay true.
- Record every written line in the notes as it is written: library, source
  file, member, type, line number after the edit, action (modified, added,
  removed), the line before, the line after, the anchor text and the change
  mark. Section 2.1 of the report is built from these entries, and a line
  recorded only from memory at the end is how a report and a diff stop
  agreeing.

### 5. Verify

1. One command does the mechanical half:

   ```
   python scripts/source_guard.py verify --work "<work folder>" --root "<source tree>"
   ```

   It runs four things in order and prints all of them:

   - **check** - every snapshotted file against its current state: encoding,
     line endings, final newline, tabs, line length, non-ASCII text
     introduced, the columns each changed line touched, the change marks, and
     fixed-format column sanity for COBOL and DDS. Resolve every `FAIL`;
     explain every `WARN` in the report.
   - **register** - one row per changed line, with the library, source file,
     member, type, line number, action, the text before, the text after and
     the mark. This is section 2.1 of the report, and it is pasted from here
     rather than written by hand.
   - **commit** - the change, in the source tree's own local git
     repository, as one commit named for the change mark. `snapshot` already
     committed each member as it was before the edit, so the repository holds
     a baseline and then the change. This is what a reviewer reads, and it
     replaces the patch file: `git show <id>` is the diff, `git revert <id>`
     is the undo, and `git log -- <member>` is every ticket that has been
     through that member. Add `--out <file>.patch` when somebody needs a file
     to attach to the ticket as well.
   - **re-index** - `alphaplus-kb update`, so the tools stop answering from
     the source as it was. Step 7 says what this does and what it costs.
     `--no-reindex` stops before it, for an edit in a tree the knowledge base
     is not pointed at.

   A `FAIL` means the report is not written yet, and nothing is committed
   or re-indexed either: a change that does not pass the guard does not reach
   the history or the knowledge base. Fix the source and run `verify` again.
2. `Read` each changed range with its surrounding lines. Read it as the
   compiler will: periods and scope terminators in COBOL, `+` and `-`
   continuations in CL and DDS, level numbers and `PIC` alignment, keyword
   columns in DDS.
3. Re-run the sweep searches with `Grep` over the edited files, not with the
   tools: `Grep` reads the source, the tools read the index, and only one of
   those is the thing you just edited until `verify` has re-indexed. The old
   width, offset or value should now remain only at sites the plan marks
   "no edit".
4. Call `unaccounted` with the report draft. Every member the tools returned
   this session needs a place: edited, reviewed and unchanged, or out of scope
   with its reason.
5. `verify` has committed the change. Run `git show <id> --stat` in the
   source tree and check it names every member in the register and nothing
   else. A file in the commit that is not in the register is an edit nobody
   planned.

### 6. Report

Write `<project root>/Implementation/<TICKET-ID>.md` with `Write`, following
`references/report-template.md` and working from the notes.

The report opens with the **metrics block**: time taken, impact analysis
accuracy, code accuracy, deviation %, confidence level, risk level, reasons.
The three accuracy rows are left BLANK with `no ground truth` as their basis -
they are a finding, not an omission, and an estimate in one of them is worse
than an empty report.

Then **§2.2 Before and after**: one code block per changed line, with its
ruler and the full 80-column BEFORE and AFTER. Every change, none elided.

The report's centre is the **change register** (§2.1): one row per line
written - library, source file, member, type, line number, action, the text
before, the text after, the change mark - and then §3, which repeats each of
those lines in full with its ruler, its anchor and the reason it is valid.
Reconcile the three counts before delivering: register rows, the "Lines
changed" totals in §2, and the header's status block. If they disagree, the
report is wrong, not the arithmetic. Before delivering
it, `remember` one `change` per edited member - what changed, the file and the
change mark - and add any disagreement with another ticket that this reports
to the report's deviations. Then give one line in chat: the report path,
members and lines changed, and the verification result.

The report is a file in the project and the change is a commit in the source
tree. Do not publish the report as an artifact, a web page or HTML, and do not
paste it into chat: it is reviewed beside `git show <id>` and attached to the
ticket, which only works for the file.

### 7. Re-index, so the knowledge base matches the source

`verify` already ran this. This section is what it did and when you have to
run it yourself.

The tools answer from the index, and after an edit the index still holds the
source as it was. Until it is rebuilt, `where_is_field_used` returns the old
width and `describe_file` the old layout - right about the code it was given,
wrong about the code on disk. That is a silent wrong answer, which is why it
is part of `verify` rather than a step somebody remembers.

```
alphaplus-kb update
```

- It re-reads only what changed, and whatever depends on it. Measured on a
  two-member tree: 0.5 seconds; on the full estate a full build is about
  three minutes, and `update` is a fraction of that.
- A Claude session that is already open picks the change up on its next tool
  call. Measured: `describe_file` returned the old width, `update` ran, and
  the same session returned the new width with no restart.
- Say in section 5.6 of the report that it ran and what it re-parsed. `verify`
  prints both.
- Run it by hand in the two cases `verify` cannot cover: an edit made outside
  this workflow, and an edit in a tree the knowledge base is not pointed at -
  a client working copy, or a second checkout. The index only catches up when
  `update` runs against the tree that actually changed.

`alphaplus-kb graph` rebuilds the codebase map from the same index; it is
optional and only worth running when somebody is going to look at the map.

## Rules for every implementation

- **Source members only.** Data members, compiled objects, compiler event files
  and the knowledge base's own output folder are never edited.
- **No action on the system.** Compiling, `CHGPF`, uploads and SVN commits are
  outside this skill; the report lists them in order as pending.
- **Approval covers the plan, not more.** Anything found later returns to the
  user as a plan change.
- **Carry the library with every member name**, in the plan, the edits and the
  report.
- **Words that mean the same thing are the same thing.** `SPACE` and `SPACES`;
  `ZERO`, `ZEROS` and `ZEROES`; `PIC` and `PICTURE`; `THRU` and `THROUGH`;
  `CORR` and `CORRESPONDING`; `COMP` and `PACKED-DECIMAL`. Every sweep searches
  every form, and the report says which forms were searched next to the count.
  A nil result from a single-form search is a search that was not finished, not
  a finding. `references/cobol-equivalences.md` has the list.
- **A line is never deleted.** Switched off, added beneath, or modified in
  place. The words "delete" and "remove" do not describe anything this shop
  does to a source line.
- **A finding that makes the change wrong without it is an edit row**, marked
  `required for correctness` - never only a paragraph in the risks. A risk
  table is read once; an edit table is worked through.
- **Every changed line is reported.** Library, source file, member, line
  number, what was there and what is there now - one row per line, never a
  summary such as "three lines updated". The diff and the register must agree
  line for line.
- **Confidence carries through.** A site that rests on an `inferred` fact - a
  library resolved through a CL variable - is stated as probable, with the
  reason, in the plan and the report.
- **State what could not be verified.** Callers outside the extraction, code in
  libraries that contribute nothing, and anything only a compile can show.

## Reference files

| File | Read when |
|---|---|
| `references/source-formats.md` | before editing any member type for the first time in a session: columns, continuation, change marks, extensions, encoding |
| `references/change-recipes.md` | when planning: what a change class must touch, how to find each site, how to verify it |
| `references/report-template.md` | when writing the report |
| `scripts/source_guard.py` | run it; `--help` lists the commands. It reads the source tree and writes only into the work folder |
| `../alphaplus-impact/references/working-notes.md` | at the start, and after any compaction: the notes format, resuming, and ticket memory |

Every tool, its arguments and how its answer misleads are listed in the
impact skill's playbook, installed beside this skill at
`../alphaplus-impact/references/tool-playbook.md`.
