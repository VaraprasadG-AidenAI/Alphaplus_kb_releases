# The implementation report, section by section

Write it to `<project root>/Implementation/<TICKET-ID>.md`. Every section
below appears, in this order, even when a section is one line saying it is
empty. A section left out reads as an oversight; a section saying "none" reads
as an answer.

## Header

```
Ticket:            <TICKET-ID>
Status:            Applied | Proposed
Source tree:       <path>
Change mark:       <TICKET-ID, or the mark this tree already uses>
Members changed:   <n>
Lines changed:     <n>
Recompile set:     <n>   (from the impact analysis, unchanged)
Impact report:     <path of the analysis this was built from>
Written:           <date>
```

`Proposed` means nothing on disk has been touched. Say it here and nowhere
else; a reader who skims must not have to infer it.

## Metrics

Under the header, before section 1. Every row appears every time. A row with
no value carries its reason in its own column - never an empty cell, never a
guess.

| Metric | Value | Basis |
|---|---|---|
| Time taken | `<n>` min | wall clock, first tool call to this file |
| **Impact analysis accuracy** | | **no ground truth** |
| **Code accuracy** | | **no ground truth** |
| **Deviation %** | | **no ground truth** |
| **Confidence level** | `<n>` Confirmed · `<n>` Probable · `<n>` Possible | carried from the impact analysis |
| Risk level | `Low` \| `Medium` \| `High` | the highest in section 9, with its reason |
| Reasons and justifications | see section 3 per line, 8 deviations, 9 open items | every line carries its own |

The three blanks are the finding. Accuracy is a comparison and needs a second
list known to be right; there is none. Do not estimate them, do not put a
confidence figure in an accuracy row, and write no percentage anywhere that is
not one count from section 2 over another. Section 10 says what would make
them measurable.

## 1. What the change is

Three sentences at most. What the ticket asks for, and what the code change
is. No background, no restatement of the analysis.

## 2. Change register

**One row per changed line.** The centre of the report. A reviewer checks this
table against the diff line by line, so it carries whole lines, not fragments.

| # | Library | Source file | Member | Type | Line | Action | Before | After | Mark |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `<LIB>` | `QDDSSRC` | `<MEMBER>` | `PF` | 14 | modified | `<the whole line as it was>` | `<the whole line as it is now>` | `<TICKET-ID>` |
| 2 | `<LIB>` | `QCBLLESRC` | `<MEMBER>` | `CBLLE` | 640 | modified | `<...>` | `<...>` | `<TICKET-ID>` |
| 3 | `<LIB>` | `QDDSSRC` | `<MEMBER>` | `PF` | 6 | added | - | `<the new line>` | `<TICKET-ID>` |

Rules:

- The **whole** line, out to column 80. Not the part that changed.
- `Library` is the source library the member sits in, read from its path.
  Never the object library.
- `Action` is the shop's verb, from `house-style.md` A.1: `switch off`
  (`*` in column 7, the text kept), `add beneath`, or `modify in place`. Never
  `delete` or `remove` - they describe nothing this shop does to a line.
- A supersession is **two adjacent rows**, `add beneath` and `switch off`,
  both or neither, followed by the line *`<TARGET>` is assigned once in this
  paragraph.*
- Rows in the order the changes apply, grouped by member.
- In `Applied` mode this table is pasted from
  `source_guard.py register`, not typed.

Close the section with one line:

```
<n> changed line(s) across <n> member(s): <n> modified, <n> added, <n> removed.
```

Those three counts, the header's `Lines changed`, and the per-member totals in
section 3 must agree. If they do not, the report is wrong - not the
arithmetic.

## 2b. Decisions taken, and what was not built

**Every rule that used to stop and ask now builds - and every one of them
writes its alternative here.** A reviewer reading this section alone can
reverse any decision in the report without going back to the source.

One row per decision. A report with no such decision says `None - nothing in
this change set rested on a judgement.`

| # | Decision | What was built | What was NOT built | Where | Whose call | To swap |
|---|---|---|---|---|---|---|
| 1 | `<the question, in one line>` | `<the reading taken, and why the analysis calls it most likely>` | `<the other reading, written out - the lines, not a description>` | register rows `<n>-<n>` | `<ticket owner / reporter / interface owner>` | `<the exact edit: move one *, delete rows n-m>` |

The five kinds of row that belong here, each from a rule that now builds:

| Kind | Mark on the register rows | What the NOT BUILT column must carry |
|---|---|---|
| **Value conflict** - the analysis rejects a value the ticket states | `rests on decision <n>` | the ticket's value as a switched-off line directly beneath the live one, so both are in the file |
| **Open question** - nobody has decided how the line is written | `rests on decision <n>` | the other reading's lines in full |
| **Design not chosen** - the analysis recommends one of several | `rests on decision <n>` | the rejected design's edit set, member by member, with the line each would change |
| **Sibling** - an adjacent field the ticket did not name | `sibling, not named by the ticket` | nothing to swap; say instead what happens if the sibling is left alone |
| **Owned line** - another change's mark is on it | both marks, in column order | what the earlier change did to that line, and what this one does to it |

Rules for this section:

- **Write the alternative as code, not as a sentence.** "Design B would add a
  column instead" is not reversible. The lines are.
- **A value conflict ships both lines.** The derived line live, the ticket's
  value starred beneath it. The file then contains the answer either way and
  the swap is one character.
- **Every row here has at least one register row pointing at it.** A decision
  with no lines is not a decision this report took - it is an open item, and
  belongs in section 9.
- **Nothing in this section reduces the change set.** It records what was built
  and what the alternative was. A member is never left empty because a decision
  was open.

---

## 2.2 Before and after - the code itself

Section 2 says *which* line. This says *what the code was and what it is now*,
in full, for **every** change - one block per changed line, same order, same
numbering. A reviewer reading only this section sees the whole change as code.
Nothing is elided: forty changed lines is forty blocks.

---

**`<n>`. `<LIBRARY>` / `<SOURCE FILE>` / `<MEMBER>` (`<TYPE>`) - line `<n>`**

`<one clause: what this line does>`

| | |
|---|---|
| Action | `switch off` \| `add beneath` \| `modify in place` |
| Anchor | `<the text searched for to find this line>` |
| Mark | `<the change mark in columns 73-80>` |

```
         1         2         3         4         5         6         7         8
1234567890123456789012345678901234567890123456789012345678901234567890123456789012
BEFORE
<the line exactly as it is now, every column>

AFTER
<the line exactly as it should be, every column>
```

**Why this is valid** - the column rule the new line satisfies.
**Effect** - what changes at run time.

---

A supersession is **one** block, not two: the line switched off and the line
added beneath it in the same fence, closing with *after this edit, `<TARGET>`
is assigned once in this paragraph.* Split apart, a reader can approve half a
pair.

## 3. Each change in place

One subsection per member. Every register row appears here again, in full,
with its context.

### 3.\<n\> `<LIBRARY>/<SOURCE FILE>/<MEMBER>.<TYPE>`

| | |
|---|---|
| Path | `<full path on disk>` |
| Sites | `<n>` |
| In scope because | `<what the analysis evidenced, as member:line>` |
| Confidence | `Confirmed` \| `Probable` \| `Possible` |

#### 3.\<n\>.\<m\> Line `<number>` - `<what this line does, in one clause>`

```
         1         2         3         4         5         6         7         8
1234567890123456789012345678901234567890123456789012345678901234567890123456789012
BEFORE:
<the line exactly as it is now>
AFTER:
<the line exactly as it should be>
```

Include two or three unchanged lines either side when the change only makes
sense in context - a level number, a record name, a `PIC` in the group above.

- **Anchor** - the text searched for to find this line. A reviewer uses it to
  find the line again after any drift.
- **Why this line is valid** - the column rule it satisfies. For DDS, which
  columns the length and type land in. For COBOL, that nothing passes 72.
- **Effect** - what changes at run time.

End every member with:

#### 3.\<n\>.R Ready to apply

`<n>` line(s) in this member, each checked against its column rules. Nothing
else in the member is touched.

## 4. Members read and not changed

Every member this session returned that is not in section 3. One row each:

| Member | Library | Why it is not changed |
|---|---|---|
| `<MEMBER>` | `<LIB>` | recompile only - it binds to the file but names no changed field |
| `<MEMBER>` | `<LIB>` | read and found to need no change - `<evidence>` |
| `<MEMBER>` | `<LIB>` | out of scope - `<reason>` |

A member with no row here and none in section 3 is unaccounted for, which is a
defect in the report. `unaccounted` finds them.

## 4b. Correctness checks

The four in `correctness-checks.md`. A check not run says `not run`; a blank
is not an answer.

| Check | Result |
|---|---|
| Capacity - every assignment sized against its target | `<n>` checked, `<n>` short |
| Single assignment - no statement left dead by the fix | `<n>` targets checked, `<n>` need a supersession pair |
| Siblings - adjacent fields of the same shape considered | `<n>` found, `<n>` raised as questions |
| Ownership - marks read on every changed line | `<n>` lines, `<n>` owned by another change |

Every non-zero on the right is either a row in section 2 or a question in
section 9. A check that fires and produces neither was wasted.

## 5. Verification

| Check | Result |
|---|---|
| Guard check - columns, marks, width, encoding | `<PASS / n FAIL>`, or `not run - source not edited` |
| Register rows reconcile with section 3 | `<n> = <n>` |
| Lines past column 72 | `<n>` |
| Non-ASCII characters introduced | `<n>` |
| Line endings and encoding unchanged | `<yes/no>`, or `n/a` |
| Commit | `<id>`, or `not committed - Proposed` |
| Index refreshed | `<n>` member(s) re-parsed in `<n>` s, or `not run - Proposed` |

In `Proposed` mode most of these read `not run`. That is the honest answer and
it belongs in the table, not left out of it.

## 6. Build and deployment sequence

The order somebody follows on the system. One action per row, numbered.

| # | Action | Detail |
|---|---|---|
| 1 | convert the physical file | `<member>`, `<what the conversion is>` |
| 2 | recompile | the recompile set from the analysis - `<n>` members |
| 3 | `<...>` | `<...>` |

The recompile set is the analysis's, carried over. Do not recount it, and do
not silently change its total.

## 7. Rollback

- Originals: `Implementation/<TICKET-ID>/originals/`, one file per member at
  its relative path - in `Applied` mode.
- Commit: `git revert <id>` in the source tree - in `Applied` mode.
- In `Proposed` mode there is nothing to roll back; say so.
- On the system: compiled objects and converted files are rolled back there.
  Name what that involves.

## 8. Deviations from the impact analysis

Anything the analysis said that the source did not bear out. One row each,
with evidence:

| What the analysis said | What the source shows | Evidence |
|---|---|---|
| `<MEMBER>` changes | it no longer exists in the extraction | `find_object` returned nothing |
| `<MEMBER>:<line>` | the line is at `<line>`, not where it said | `<anchor text>` |
| `<MEMBER>` changes | read, and needs no change | `<member:line>` |
| - | `<MEMBER>` also needs a change; the analysis missed it | `<member:line>` |

An empty section here is a claim that the analysis was exactly right. Only
write "none" after checking.

## 9. Open items

- A sibling field the ticket did not name - adjacent, same shape, overlapping
  readers. A question for the reporter, never a quiet addition to scope.
- A line another change owns, where this proposal would reverse it.
- House style not recorded: if `house-style.md` Part B is unfilled, say the
  shape of the change follows general IBM i practice and may not match this
  team's.
- Sites resting on an inferred fact, stated as probable.
- Anything the tools could not resolve: a `CALL` through a variable, an
  `OVRDBF` with a variable library, a copybook not in the extraction.
- Job scheduler entries, which are not in the extraction at all.
- Questions for the ticket owner.
- **A compile has not run.** Write this plainly, every time. Nothing in this
  report has been compiled, and no column rule check is a substitute.

## 10. Metrics

See `metrics.md`. Three tables: what this run produced, the confidence tiers
carried over, and the accuracy table - which stays blank.
