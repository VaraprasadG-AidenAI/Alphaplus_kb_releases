# Implementation report

## Where the files go

```
<project root>/Implementation/<TICKET-ID>.md          the report
<project root>/Implementation/<TICKET-ID>/originals/  the snapshot, for rollback
<source tree>/.git                                    the change itself
```

The change is not a file in `Implementation/`. It is a commit in the source
tree's own local git repository, which `source_guard.py verify` made. The
report cites the commit id; `git show <id>` is the diff. Write a
`<TICKET-ID>.patch` beside the report only when somebody has to be given a
file - an attachment on the ticket, a reviewer with no access to the tree -
and say in section 5.0 that it is a copy of the commit, not the record.

`<project root>` is the folder the impact report `<TICKET-ID>-impact-report.md` is
in. Name the files after the ticket and nothing else. If a report for the
ticket exists, read it first; a second pass on the same ticket supersedes it and
says in section 8 what changed.

The report is this markdown file, written with `Write`. Do not publish it as
an artifact, a web page or HTML, and do not paste it into chat. The ticket's
working notes stay in `<project root>/.alphaplus-work/`; they are Claude's record, not
part of the deliverable.

## Register

Written for two readers: the developer who reviews the diff and deploys, and a
delivery manager who does not read COBOL. Expand a platform term the first time
it appears - "CHGPF (the command that changes a physical file and converts its
data)" - then use it plainly. No marketing adjectives.

Every section appears, in this order. A section with nothing to report says so
and gives the evidence, because a missing section reads as one nobody checked.

---

## Header

```
================================================================================
IMPLEMENTATION  ·  <TICKET-ID>
<Ticket title>
================================================================================
Prepared            <YYYY-MM-DD>
Application         Alphaplus  (IBM i / AS400)
Change class        <class> - <one line: what changed>
Impact report       <TICKET-ID>-impact-report.md  (<date>)
Source tree edited  <absolute path>
Approved by         <name or "the user in this session">, <YYYY-MM-DD>
Change mark         <value in columns 73-80>  ·  history line: <yes/no>
--------------------------------------------------------------------------------
STATUS              Implemented  |  Partly implemented  |  Not implemented
                    Members edited:            <n>   (<n> DDS · <n> COBOL · <n> CL · <n> other)
                    Lines changed:             <n> modified · <n> added · <n> removed
                    Sites reviewed, no edit:   <n>
                    Verification:              guard <PASS|n warnings|FAIL>
                                               re-sweep <clean|n open>
                                               unaccounted <0|n>
                    Pending on the system:     <n> file conversions · <n> compiles · deploy
================================================================================
```

Every figure in the status block is traceable to a section below.

### The metrics block

Immediately under the header, before section 0. Every row appears, every time.
A row with no value carries the reason in its own column - **never** leave a
cell simply empty, and never fill one with a guess.

| Metric | Value | Basis |
|---|---|---|
| Time taken | `<n>` min | wall clock, from the first tool call to this file being written |
| **Impact analysis accuracy** | | **no ground truth** - see below |
| **Code accuracy** | | **no ground truth** - see below |
| **Deviation %** | | **no ground truth** - see below |
| **Confidence level** | `<n>` Confirmed · `<n>` Probable · `<n>` Possible | how well each member's inclusion is evidenced, carried from the impact report |
| Risk level | `Low` \| `Medium` \| `High` | the highest risk in section 9, with its one-line reason |
| Reasons and justifications | see §3 per line, §8 deviations, §9 open items | every line carries its own |

**The three blank rows are a finding, not an omission.** Accuracy is a
comparison, and it needs a second list known to be right. This engagement has
none: no set of closed tickets with the change the client actually shipped,
member by member, and no compile to say whether a change even builds.

Do not estimate them. Do not put a confidence figure in an accuracy row -
`68 Confirmed` is not `68 correct`. Do not write any percentage in this report
that is not one count from section 2 divided by another.

What would make them measurable, one line each, so the blanks read as a known
gap rather than an oversight:

| To measure | What is needed |
|---|---|
| Impact analysis accuracy | the change set the client actually shipped for a set of closed tickets, member by member |
| Code accuracy | a compile of the changed members - it turns "did this build" into a mechanical answer |
| Deviation % | both of the above: deviation is this report's set against a known-correct set |

If either ever arrives, these rows are filled the same way as the rest: a
count over a count, with the basis stated.

---

## 0. Scope and approval

- The plan as approved: members, sites, change mark.
- Plan changes after approval, each with the reason and the user's answer.
- Source tree, and how each member was located: relative path from
  `find_object`, matched once on disk.

## 1. The ticket

Three to six lines: what the ticket asks, target values taken from it (new
width, rule, text), and the comments that settled a question.

## 2. Summary of changes

One row per edited member, in build order.

| # | Library | Source file | Member | Type | Path | Lines changed | What changed |
|---|---|---|---|---|---|---|---|
| 1 | `<LIB>` | `QDDSSRC` | `<MEMBER>` | PF | `<relative path>` | 2 modified, 1 added | `<field>` 50A -> 100A; history line |

### 2.1 Change register - every changed line

Generated, not typed: paste the table printed by

```
python scripts/source_guard.py register --work "<work folder>" --root "<source tree>"
```

which builds it by comparing every file with its snapshot. A register written
from memory and a patch that disagrees with it is the one defect a reviewer
cannot forgive.

The whole change on one page: one row per line written, in the order the edits
were applied. A reader who reads nothing else must still be able to say which
library, which member, which line, what was there and what is there now.

Nothing is summarised away here: five lines changed means five rows. The row
count must equal the "Lines changed" total in the table above, and the numbers
in the header's status block.

| # | Library | Source file | Member | Type | Line | Action | Before | After | Mark |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `<LIB>` | `QDDSSRC` | `<MEMBER>` | PF | 57 | modified | `A <FIELD> 50A B 5 18` | `A <FIELD> 100A B 5 18` | `<mark>` |
| 2 | `<LIB>` | `QDDSSRC` | `<MEMBER>` | PF | 12 | added | - | `A* 12.03.26 <mark> widened <FIELD>` | `<mark>` |

`Action` is the shop's verb, from `house-style.md` A.1: `switch off` (`*` in
column 7, the text kept), `add beneath`, or `modify in place`. Never `delete`
or `remove`. `Before` and `After` are the line as written, with leading spaces
shown as they are; where the full 80-column line does not fit the table, give
the significant part here and the full line in 2.2, which carries every line
whole.

Line numbers are the numbers **after** the edit, and section 3 gives the
anchor text for each site, because a line number alone stops being an address
as soon as anything above it moves.

### 2.2 Before and after - the code itself

The register above says *which* line. This says *what the code was and what it
is now*, in full, for **every** change. One block per changed line, in the
same order and with the same numbering, so a reviewer can read this section
alone and see the whole change as code.

Nothing is elided. A change to forty lines is forty blocks.

---

**`<n>`. `<LIBRARY>` / `<SOURCE FILE>` / `<MEMBER>` (`<TYPE>`) - line `<n>`**

`<one clause: what this line does>`

| | |
|---|---|
| Action | `switch off` \| `add beneath` \| `modify in place` |
| Anchor | `<the text searched for to find this line>` |
| Mark | `<the change mark written in columns 73-80>` |

```
         1         2         3         4         5         6         7         8
1234567890123456789012345678901234567890123456789012345678901234567890123456789012
BEFORE
<the line exactly as it was, every column, nothing trimmed>

AFTER
<the line exactly as it is now, every column, nothing trimmed>
```

**Why this is valid** - the column rule the new line satisfies. For DDS, which
columns the name, length, type and position land in. For COBOL, that the
indicator is in 7 and nothing passes 72.

**Effect** - what changes at run time.

---

Two rules for this section:

- **A supersession is one block, not two.** The line switched off and the line
  added beneath it belong together, with both shown in the same code fence,
  and a closing line: *after this edit, `<TARGET>` is assigned once in this
  paragraph.* Split across two blocks, a reader can approve half of a pair.
- **Context where it is needed.** Two or three unchanged lines either side
  when the change only makes sense with them - a level number, a record name,
  the `PIC` in the group above. Mark them as context, not as changed.

## 2b. Decisions taken, and what was not built

Every rule that used to stop and ask now **builds**, and records its
alternative here. A reviewer reading this section alone can reverse any
decision without opening the source.

No such decision: write `None - nothing in this change set rested on a
judgement.`

| # | Decision | What was built | What was NOT built | Register rows | Whose call | To swap |
|---|---|---|---|---|---|---|
| 1 | `<the question, one line>` | `<the reading taken, and why the analysis calls it most likely>` | `<the other reading, as lines - not as a sentence>` | `<n>-<n>` | `<ticket owner / reporter / interface owner>` | `<the exact edit: move one *, delete rows n-m>` |

Five kinds of row belong here, one per rule that now builds:

| Kind | Mark on the register rows | The NOT BUILT column carries |
|---|---|---|
| **Value conflict** - the analysis rejects a value the ticket states | `rests on decision <n>` | the ticket's value, switched off, directly beneath the live derived line |
| **Open question** - how the line is written is undecided | `rests on decision <n>` | the other reading's lines in full |
| **Design not chosen** - the analysis recommends one of several | `rests on decision <n>` | the rejected design's edit set, member by member |
| **Sibling** - an adjacent field the ticket did not name | `sibling, not named by the ticket` | what happens if the sibling is left alone |
| **Owned line** - another change's mark is on it | both marks, in column order | what the earlier change did, and what this one does to it |

- **The alternative is code, not prose.** "Design B would add a column instead"
  cannot be applied. Lines can.
- **A value conflict ships both lines** - derived live, ticket's value starred
  beneath. The file holds the answer either way and the swap is one character.
- **Every row points at register rows.** A decision with no lines is an open
  item and belongs in section 9, not here.
- **Nothing here shrinks the change set.** A member is never left empty because
  a decision was open.

## 3. Changes in detail

One subsection per member, in build order; inside it, one numbered block per
site, in line order. Every changed line in the register appears here exactly
once - a member with four changed lines has four blocks.

### 3.<n> `<LIB>/<SOURCEFILE>/<MEMBER>` (<type>)

| | |
|---|---|
| Library | `<LIB>` |
| Source file | `<SOURCEFILE>` (for example `QDDSSRC`, `QCBLLESRC`) |
| Member | `<MEMBER>` · type `<type>` |
| Path on disk | `<relative path from the source tree>` |
| Sites changed | `<n>` (matches the register) |
| In scope because | impact report §`<n>`, `<tool>` → `<MEMBER>:<line>` |

**Purpose.** One sentence tying this member's change to the ticket.

#### 3.<n>.<m> Line <line> - <what this site is>

**Before and after**, the full lines with the ruler, and one unchanged line
either side so the reader can see where the change sits:

```
          1         2         3         4         5         6         7         8
 12345678901234567890123456789012345678901234567890123456789012345678901234567890
   56 |     A            <FIELD-BEFORE-IT>  10A
BEFORE
   57 |     A            <FIELD>       50A  B  5 18
AFTER
   57 |     A            <FIELD>      100A  B  5 18                      <mark>
   58 |     A            <FIELD-AFTER-IT>   3A
```

**Anchor.** The unique text the site is found by, independent of its line
number - `<FIELD>` on the only line of this member declaring it. A reader
applying this change by hand uses the anchor, not the number.

**Why this line is valid.** The columns the format requires, named: for DDS,
length right-justified in 30-34, type in 35, decimals in 36-37, usage in 38;
for COBOL, the indicator in column 7, nothing past column 72, the period that
closes the statement; for CL, the `+` continuation. Verified with
`python scripts/source_guard.py line --kind <kind> "<the new line>"`, which
prints PASS or names the column that is wrong.

**Effect.** What this line now does differently.

### 3.<n>.R Ready to apply

The complete new text for this member, in one block, for a developer applying
it by hand in SEU or RDi rather than from the patch:

```
   12 |     A*  12.03.26  <mark>  widened <FIELD> to 100 - <TICKET-ID>
   57 |     A            <FIELD>      100A  B  5 18                      <mark>
```

**Member effect.** What the member now does differently as a whole, and what
it affects when compiled (for example: the record format changes, so every
program opening the file recompiles).

## 4. Sites reviewed and not changed

Every search hit and tool result that was examined and left alone.

| Member | Line | Text | Why no edit | Evidence |
|---|---|---|---|---|
| `<LIB>/<MEMBER>` | 212 | `05 WS-<FIELD> LIKE <FIELD> OF <FILE>` | follows the file at recompile | `search_source("LIKE\s+.*<FIELD>")` |

## 5. Verification

### 5.0 Commit

| Baseline commit | Change commit | Members | Lines |
|---|---|---|---|
| `<id>` before the edit | `<id>` | `<n>` | `<n>` |

The change is in the source tree's local git repository, not in a patch file.
`git show <id>` is the diff, `git show <id> --stat` the file list, and
`git revert <id>` the undo. Nothing was pushed, fetched or cloned; the
repository is on this machine only.

### 5.1 Guard check

The result of `source_guard.py verify`, per file - the check half; the
register half is section 2.1 and the commit is section 5.0:

| File | Encoding | Line endings | Tabs | Longest line | Non-ASCII added | Marks on changed lines | Result |
|---|---|---|---|---|---|---|---|

Explain every warning.

### 5.2 Re-sweep

| Search (over the edited files on disk) | Hits before | Hits after | Remaining hits are |
|---|---|---|---|
| `X\(50\)` | 14 | 3 | sites 4.1-4.3, no edit |

### 5.3 Reading

For each changed range: the lines read around it, and what was checked -
periods and scope terminators, continuations, level numbers, DDS columns.

### 5.4 Accounting

The result of `unaccounted` with the report text: `0 members without a place`,
or each member with its place.

### 5.5 Offsets and parameters

When the change moved positions or parameter lengths, the recomputed table:
item, old start and length, new start and length - or parameter number, called
item and length, each caller's item and length.

### 5.6 Index refreshed

`source_guard.py verify` runs `alphaplus-kb update` as its last step and
prints what it re-parsed. Copy the numbers here:

| Ran | Members re-parsed | Dependent members re-derived | Time |
|---|---|---|---|
| `alphaplus-kb update` (step 4 of `verify`) | `<n>` | `<n>` | `<n>` s |

Until this runs, every tool answers from the source as it was before the edit.
If it was skipped - `--no-reindex`, or an edit in a tree the knowledge base is
not pointed at - say so here and say which tree still needs it.

## 6. Build and deployment sequence

Not performed by this change. In order:

| Step | Action on the system | Objects |
|---|---|---|
| 1 | change the physical file and convert its data (`CHGPF`) | `<LIB>/<FILE>` |
| 2 | recompile logical files | ... |
| 3 | recompile display and printer files | ... |
| 4 | recompile programs | the recompile set from the impact report, appendix A |

## 7. Rollback

- Originals: `Implementation/<TICKET-ID>/originals/`, one file per edited member
  at its relative path, with the hash recorded at snapshot time.
- Commit: `git revert <id>` in the source tree undoes the change as a new
  commit, leaving both in the history. `git checkout <baseline id> -- <member>`
  restores one member.
- Objects already compiled or files already converted on the system are rolled
  back there; name what that involves.

## 8. Deviations from the impact report

Sites added, sites dropped, members whose on-disk text differed from what the
index held, and changes another ticket recorded in ticket memory on the same
members or fields (`recall`, and what `remember` reported) - each with the
reason and the evidence. "None" is a valid
answer when it is true.

## 9. Open questions and what could not be verified

- No compile has run; only the system can show a compile error.
- Callers or users outside the extraction, and libraries that contribute
  nothing to the index.
- Sites resting on inferred facts, stated as probable.
- Questions for the ticket owner.

## Appendix A - the diff

The commit id, and the diff itself when it is under about 400 lines - the
output of `git show <id>`. Over that, give `git show <id> --stat` and the
commit id, which anybody with the source tree can expand.

---

## Checklist before returning the report

- [ ] The metrics block is under the header, all seven rows present.
- [ ] Impact analysis accuracy, Code accuracy and Deviation % are BLANK, each
      with `no ground truth` in the Basis column. No estimate anywhere.
- [ ] No percentage in the report that is not one count from §2 over another.
- [ ] §2.2 has one block per changed line - the count equals the §2.1 register
      row count - each with its ruler, full BEFORE and full AFTER.
- [ ] Every supersession in §2.2 is ONE block, with both lines and the
      "assigned once in this paragraph" line.

- [ ] §2.1 has one row per changed line - library, source file, member, type,
      line number, action, before, after, mark - and the row count equals the
      "Lines changed" totals in §2 and in the header.
- [ ] Every register row appears once in §3 as a numbered site block, with the
      full before and after lines, the ruler, an anchor, and the line that says
      why the new line is valid.
- [ ] Every member in §3 ends with its "Ready to apply" block.

- [ ] Every member in section 2 has a subsection in section 3, and the counts
      in the status block add up to them.
- [ ] Every site in the approved plan is either in section 3 or in section 8.
- [ ] Every search hit is in section 3 or section 4.
- [ ] `source_guard.py check` has no `FAIL`, and every `WARN` is explained in
      5.1.
- [ ] `unaccounted` names no member without a place.
- [ ] `git show <id> --stat` names exactly the members in section 3, and no
      others.
- [ ] Section 6 lists every pending action on the system in order.
- [ ] Section 9 says a compile has not run.
- [ ] Every `<change>` in the notes is in section 3, and every open `<ask>` is
      in section 9.
- [ ] One `change` per edited member is recorded with `remember`, and any other
      ticket it reported on the same member is in section 8.
- [ ] The report is a markdown file in `Implementation/`, not an artifact or
      web page.
