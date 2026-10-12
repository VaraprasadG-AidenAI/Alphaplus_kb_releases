# Report structure

## Where the file goes

`<project root>/<TICKET-ID>-impact-report.md` - directly in the project root,
not in a folder under it.

**The project root** is the folder this Claude Code session was started in -
the working directory at the start of the session, where `.mcp.json` or
`.claude/` sits. Resolve it before writing anything: run `pwd` (PowerShell:
`Get-Location`) and use that absolute path. Never the scratchpad or a temp
directory, the home folder, the skill's own folder, the source extraction, or
a subfolder of the root. If the working directory has moved during the session,
use the one the session started in; if it cannot be determined, ask the user.

Name the file after the ticket - no date, no other prefix; if the ticket has
only a title, use the title as a slug. If a report for this ticket already
exists in the root, read it first, then either supersede it and say what
changed, or use a new name. Once it is written, give one line in chat: the
absolute path, and the edit and recompile totals.

The report is this markdown file and nothing else. Write it with `Write`; do
not publish it as an artifact, a web page or HTML, and do not paste it into
chat. The team reads it from the project, versions it and attaches it to the
ticket, and none of that works for a copy that lives somewhere else. If the
user asks for another format as well, produce it from the file.

The ticket's working notes stay in `<project root>/.alphaplus-work/`
(`working-notes.md`). They are Claude's record, not part of the deliverable.

## Register

Technical, and readable by a delivery manager who does not write COBOL. Expand
a term the first time it appears - "OVRDBF (a CL command that redirects a file
at run time)" - then use it plainly. No marketing adjectives.

Every section below appears in every report, in this order. A section with
nothing to report says so and gives the query that showed it empty, because a
missing section reads as one nobody checked.

---

## Header

```
================================================================================
IMPACT ANALYSIS  ·  <TICKET-ID>
<Ticket title>
================================================================================
Prepared            <YYYY-MM-DD>
Application         Alphaplus  (IBM i / AS400)
Change class        <class> - <field> <old> -> <new> on <file>
Source scope        <n> libraries, <n> members indexed  (<n> contribute nothing)
Fact density        <lib> <n>% · <lib> <n>% ...  (members with facts, per library)
Knowledge base      alphaplus-kb <version>, index built <YYYY-MM-DD HH:MM> UTC
Analysis basis      <n> MCP tool calls over extracted facts
Confidence          <n> exact · <n> inferred · <n> unresolved
--------------------------------------------------------------------------------
VERDICT             Database change required:  YES <col>, <type>, on <file>  |  NO
                    Members to edit:           <n>
                    Members to recompile:      <n> confirmed
                                             + <n> probable
                                             + <n> possible  (lower confidence)
                    Physical files:            <n> to convert  ·  <n> rebuilt
                                             ·  <n> alias names, no action
                    Ambiguous names:           <n>  (one name, several files)
                    Estimated effort:          <n> days
================================================================================
```

The verdict is what an SME reads first, so every figure in it must be
traceable to the sections below.

The physical-file and ambiguous-name lines carry the hardest idea on this
platform - that the name in a program is usually not the name of a file - to
the place where decisions are made. Give one resolved example inline, with
figures from `resolve_files`:

```
Physical files:   1 to convert (CUSTFILE)  ·  2 rebuilt  ·  14 alias names, no action
Ambiguous names:  7  - e.g. ORDFILE is a program-side name that resolves to
                  F_ORDFILE and 8 other files, depending on which CL launched it
```

---

## Metrics

Directly under the header, before section 0. Every row appears every time. A
row with no value carries its reason in its own column - never an empty cell,
never a guess.

| Metric | Value | Basis |
|---|---|---|
| Time taken | `<n>` min | wall clock, first tool call to this file |
| Tool calls | `<n>` | every MCP call made for this analysis |
| Members examined | `<n>` | returned by a tool and read |
| **Impact analysis accuracy** | | **no ground truth** |
| **Recompile set accuracy** | | **no ground truth** |
| **Deviation %** | | **no ground truth** |
| **Confidence level** | `<n>` Confirmed · `<n>` Probable · `<n>` Possible | the tiers in section 3.2 |
| Risk level | `Low` \| `Medium` \| `High` | the highest in section 6, with its reason |
| Coverage | `<n>` of `<n>` libraries contribute facts | `coverage_report` |
| Blind spots hit | `<n>` - name each | section 9 |
| Reasons and justifications | per row in section 3, risks in 6, gaps in 9 | every row carries its own evidence |

The three blank rows are the finding, not an omission. Accuracy is a
comparison and needs a second list already known to be right; this engagement
has none. Do not estimate them, never put a confidence figure in an accuracy
row, and write no percentage anywhere that is not one count in this report
divided by another.

What would make them measurable: the developers' committed change list for the
same ticket. Given that, accuracy is the share of their changed members this
report named, and deviation the share it named that they did not touch.

---

## 0. Coverage

One line per library that contributes nothing to the index, from
`coverage_report`, with what it would have held:

```
NOT INDEXED   LIBC   directory skeleton, 0 files
              LIBD   files present, none recognised as source
INDEXED BUT   LIBE   412 members read, 0 with facts
EMPTY
Counts in this report are floors while these remain unextracted.
```

A library that is `INDEXED BUT EMPTY` is the same gap as one never read: its
members were counted and nothing was learned from them. While one exists and
could hold part of the answer, no count in the header is final - mark each as a
floor, and say which library blocks it. Give the `with facts` percentage for
every library in the header's fact-density line; a library far below the others
is named in section 9 even when it is not empty.

If every library is covered, say so.

## 1. The ticket

What was asked, in two or three sentences in the reporter's terms, then what it
means technically in one short paragraph.

### 1.1 Acceptance criteria

Every criterion the ticket carries, in its own numbering, each with the code
that satisfies it or a plain statement that none was found. A criterion with
no code is repeated in §9. If the ticket carries none, say so in one line.

| Criterion | What it requires | Code that satisfies it | Confidence |
|---|---|---|---|
| AC-001 | ... | `MEMBER:line` | Confirmed |
| AC-007 | ... | **no code found** - ask the reporter | - |

Then what you read:

    Read: description, 9 comments, 2 screenshots, linked TICKET-1180.
    Not available: attachment "trace.txt".

If a comment changed the requirement, say so here:

    The description asks for X. Comment 4 (developer, 12 Mar) says X was tried
    and the cause is Y. This analysis follows Y.

If the ticket is ambiguous, state the reading you adopted and what would change
under the other.

## 2. Executive summary

Six to ten lines of prose, no tables, which an SME and a delivery manager both
finish with the same picture: what changes, where, what that forces, what it
costs, and the one thing most likely to go wrong.

## 3. What changes

### 3.1 Database

State the verdict in these words:

- `Requires a new database column: YES - <name> <type> on <file>`, or
- `Requires a new database column: NO`

Then the field table, each row with its evidence:

| Ticket term | Maps to | File | Type | Status | Evidence |
|---|---|---|---|---|---|
| Invoice-Format | new column | CUSTMAST | A(1) | not defined | `where_is_field_used` - not defined on any file |
| Routing-ID | ROUTEID | CUSTMAST | A(50) | exists, no writers | `CUSTMAST.dds:161` |

Call an existing field available only when `where_is_field_used` shows no
program writing it. If programs write it, it already carries a meaning; say so
and name them.

### 3.1b Physical files

Every file name in the analysis gets a row, including those that need no
action, since an absent row reads as "not affected".

| Status | File | Library | Built from | Mechanism | Evidence (CL:line) | Overriding CLs | Action |
|---|---|---|---|---|---|---|---|

`Mechanism` is what produced the file - `$CRTF`, `$CPYF`, `OVRDBF`, `$MC*`, or
`*CAT` for a name built at run time. `Overriding CLs` is how many CL members
bind a program-side name onto the file; a file with hundreds is not one anybody
converts quietly. Source: `resolve_files(<name>)` for every file in scope.

Then:

- totals by status;
- the ambiguous names, each with its targets - a risk, since a program
  declaring that name works on different data depending on its caller;
- duplicate templates - the same name in more than one library, where which one
  compiles depends on the library list;
- names not in the extraction, and who to ask.

State that these are run-time objects that are converted on the machine, not
source members that are edited.

### 3.2 Programs - the edit set

| Member | Library | Called | Change | Why | Evidence |
|---|---|---|---|---|---|
| CUSTMAST.dds | LIBA | ROUTEID | widen to A(100) | the width itself | CUSTMAST:161 |
| PGM502 | LIBA | AA-ROUTEID | edit its own `PIC X(50)` | WORKING-STORAGE copy at the old width | PGM502:164 |
| PGM610 | LIBB | CC-ROUTEID | fix the offset | reference modification `(01:01)` | PGM610:411 |

`Called` is the program's local name for the field, from `where_is_field_used`;
without it the reader cannot find the line. A member is in the edit set or the
recompile set, never both.

**The `Change` column uses the shop's verbs, never "delete".** A line is
switched off (`*` in column 7, text kept), added beneath, or modified in
place. `references/house-style.md` A.1 has the three and what each one means.

**A supersession is two rows, not one.** When the change replaces a statement
that is still live, both rows appear together and neither stands alone:

| Member | Library | Called | Change | Why | Evidence |
|---|---|---|---|---|---|
| `<MEMBER>` | `<LIB>` | `<LOCAL NAME>` | add beneath line `<n>` | the new statement | `<MEMBER>:<n>` |
| `<MEMBER>` | `<LIB>` | `<LOCAL NAME>` | switch off line `<n>` | the statement it replaces | `<MEMBER>:<n>` |

followed by the verification line: *after this edit, `<TARGET>` is assigned
once in this paragraph.* One row alone leaves both statements running; the
later one wins and the fix is dead on arrival with nothing failing.

**Rows that correctness demands carry a marker.** A finding that means the
change is wrong without it is an edit row here, written
`required for correctness`, not a paragraph in section 6. The two classes that
have shipped broken are a target too narrow for what is moved into it, and a
sibling field that must change with the one the ticket names. If the line is
not yet identified, the row still exists and says
`required for correctness - line not yet identified`.

### 3.3 Correctness checks

The four checks in `references/correctness-checks.md`, run before this report
goes out. A check not run says `not run`; a blank is not an answer.

| Check | Result |
|---|---|
| Capacity - every assignment sized against its target | `<n>` checked, `<n>` short |
| Single assignment - no statement left dead by the fix | `<n>` targets checked, `<n>` need a supersession pair |
| Siblings - adjacent fields of the same shape considered | `<n>` found, `<n>` raised as questions |
| Ownership - marks read on every line to be changed | `<n>` lines, `<n>` owned by another change |

Every non-zero on the right is a row in 3.2 or a question in section 9. A
check that fires and produces neither is a check that was wasted.

## 4. Blast radius

A tree, then the same content as a table, because a tree does not sort:

```
CUSTMAST  (PF, customer master)
│
├── EDIT ................................ 3 members
│   ├── CUSTMAST.dds ................... add column, shrink FILLER
│   ├── PGM100 ......................... map and write the new field
│   └── PGM105 ......................... add to the outbound payload
│
├── RECOMPILE (copy the record layout) .. 64 members
│   ├── LIBA ........................... 41   PGM510 PGM530 PGM540 ...
│   ├── LIBB ........................... 13   PGM020 PGM060 PGM207 ...
│   └── LIBC ........................... 10   PGM700 PGM701 PGM702 ...
│
├── LOGICAL FILES OVER IT ............... n
├── WRITERS ............................. n
├── READERS ............................. n
└── REDIRECTED HERE BY OVRDBF ........... n
```

| Level | Coupling | Count | How coupled | Source |
|---|---|---|---|---|
| 1 | edit | 3 | the change is written here | analysis |
| 2 | recompile | 64 | `COPY` of the record layout | `what_uses` |
| 3 | logical files | 7 | a view over it | `blast_radius` |

Name the widest-reaching member and its caller count; a change there is the
highest-risk part of the work.

## 5. Recompile set

This is the section SMEs otherwise rebuild by hand, so it appears in every
report, including when the answer is zero.

1. The counts by tier: `64 confirmed · 9 probable · 14 possible`.
2. How each was obtained, with the call and its limit - `what_uses CUSTMAST
   kind='copybooks' limit=4000` - so a reader can see the list was not cut.
3. Every name, grouped by library with a count per group.
4. The probable and possible tiers in their own tables, with a reason per row.
5. What triggers the recompile.

| Library | Count | Members |
|---|---|---|
| LIBA | 41 | PGM510, PGM530, PGM540, PGM620, ... *(all 41 in the real report)* |
| LIBB | 13 | PGM020, PGM060, PGM207, ... |

**Probable (9)** - the coupling is real; the target was resolved, not read

| Member | Why | Evidence |
|---|---|---|
| WRKF09 | `OVRDBF FILE(WRKF02) TOFILE(&DTALIB/WRKF09)` - library from a variable | CTL110:44 |

**Possible (14)** - may also change, at lower confidence

| Member | Why | Evidence |
|---|---|---|
| PGM502 | declares its own `PIC X(30)`; a text match, not a copy edge | PGM502:164 |

If your total differs from a number you were given, say why - coupling through
`COPY DDR-` and `LIKE ... OF` is in no call graph, so a hand-built list usually
misses members - and name any of the given members you could not reach.

If the count is zero, the zero carries the list of what was checked - every
layout, copybook, display file and parameter the change could reach, the call
run for each, and the gaps each call reported:

> `Members to recompile: 0` - no DDS change is required (§3.1), so no record
> layout changes. Checked:
>
> | Object | Call | Result | Gaps reported |
> |---|---|---|---|
> | CUSTMAST | `what_uses CUSTMAST kind='copybooks' limit=4000` | <n> copiers, none reached | none |
> | CUSTD01 (DSPF) | `what_uses CUSTD01 kind='screens'` | text-only change | none |
> | CUST100 LINKAGE | `program_interface CUST100` | unchanged | 2 COBOL callers not size-checked, compared by hand |
>
> A row whose gap is not closed makes the zero a floor, and §9 says so.

## 6. Risks and defects found

Findings the ticket did not ask for, each with severity, consequence and
evidence.

**A finding that means this change is wrong without it does not belong here.**
It is an edit row in 3.2 marked `required for correctness`. This section is
for what the reader should know, not for what the implementer must do - a risk
table is read once, an edit table is worked through, and two correct
predictions have shipped broken because they were written here instead.

| # | Severity | Finding | Consequence | Evidence |
|---|---|---|---|---|

Every `OVERRUN` or `MISSING` row from `program_interface` and every
`LVLCHK(*NO)` override from `resolve_overrides` on a program in the change is a
row here, severity High, with both evidence lines. Also look for parameters
accepted and never used (search the procedure division for each name that
`program_interface` lists), field widths
that differ between a screen and its file, fields declared and never
referenced, ambiguous program-side names, and duplicate templates. These
explain most "the value does not save" tickets.

### 6.1 Earlier tickets

What ticket memory holds about this ticket's subjects: every `recall` result
that bears on the change, and every disagreement `remember` or `recall`
reported. A disagreement is a delivery risk - two tickets moving the same
field in different directions - so give both statements and which one the
evidence supports.

| Subject | Earlier ticket | It recorded | This ticket | Evidence | Consequence |
|---|---|---|---|---|---|

When memory holds nothing about these subjects, say so in one line.

## 7. Build and deployment sequence

In order, because order matters on IBM i: DDS before the programs that copy
it, conversion before the programs that read the converted file.

| Step | Action | Member(s) | Depends on |
|---|---|---|---|

## 8. Effort

| Item | Days | Basis |
|---|---|---|

Then the total, and the largest single risk to it.

## 9. What could not be established

With who to ask. There is always something here; the job scheduler, which is
not in the source, is always one entry.

| Question | Why the source cannot answer | Ask |
|---|---|---|

Three more belong here whenever they apply:

- **A sibling field the ticket did not name.** Adjacent, same shape, readers
  overlapping the field the ticket names. It is a question for the reporter,
  not a row quietly added to scope and not an observation in section 6.
- **A line owned by another change.** If the proposal reverses what a previous
  ticket did to a line, name the mark and ask whoever owns it.
- **House style not recorded.** If `references/house-style.md` Part B is
  unfilled, say so: the shape of the proposed change follows general IBM i
  practice and may not match this team's. Do not guess an idiom in its place -
  a guessed idiom reads as evidence.

## Appendix A - full recompile list

Every name, plain and sorted, one tier per block, so the confirmed block can go
into a build script as it is:

```
CONFIRMED (64)
PGM100 PGM104 PGM510 PGM530 ...

PROBABLE (9)
WRKF09 ...

POSSIBLE (14)
PGM502 ...
```

Each block's count must equal the figure in the header and in §5; if they
differ, a tool answer was cut and the analysis is unfinished.

## Appendix B - members named by tools and excluded from scope

One row per member, and every row carries evidence read from INSIDE the member
- its header block or the statement at the matched line. A row whose reason is
only a name, a tool row or a matched string is not an exclusion; open the
member and write what it says.

| Member | Why a tool returned it | Read | Why it is out of scope |
|---|---|---|---|
| `PRC410` | declares a field the search matched | `PRC410:118-131` | its header says the flag it guards decides which rate table a line is priced from; this change alters neither the flag nor those fields |

A near-miss name (a different identifier that shares a substring) is never
excluded as a "false positive": say what that identifier is for, with its
evidence, then exclude it.

**A member whose header block contains a term from the ticket is read in full
before it can be excluded.** That is the most likely place the answer is
hiding, and it is exactly the member a sweep surfaces and a reader files away.
If its header carries the ticket's subject and it is still out of scope, the
`Read` column cites the line in the header and the one that overrides it.

## Appendix C - evidence index

| § | Claim | Tool call |
|---|---|---|

## Footer

```
--------------------------------------------------------------------------------
BASIS OF ANALYSIS
  Facts extracted from executable source. Header comments are not evidence;
  where a header disagrees with its code, the code is used.
  Confidence: exact = read literally · inferred = resolved through a variable
  · unresolved = present but not determinable.

KNOWN BLIND SPOTS
  Job scheduler entries, run-time library resolution from data areas, and the
  data itself are not in the source. RPG members yield no field usage.
  <n> claims in this report are inferred; each is marked where it is used.
--------------------------------------------------------------------------------
alphaplus-kb <version>  ·  index <YYYY-MM-DD>  ·  <n> members  ·  <n> facts
================================================================================
```

---

## Checklist before returning the report

- [ ] No row in §3.2 says "delete" or "remove" about a source line.
- [ ] Every supersession in §3.2 is a pair - add and switch off - with its
      "assigned once in this paragraph" line.
- [ ] §3.3 has all four checks filled in, and every non-zero has a row in §3.2
      or a question in §9.
- [ ] Every proposed line carries its change mark, and the marks match what
      `source_guard.py marks` reports for that member.
- [ ] Any member excluded in Appendix B whose header block carries a term from
      the ticket was read in full, and the `Read` column says where.


The first item is a tool call, and the only one that cannot be passed by
believing it done. Run it first, and again after fixing what it finds.

- [ ] `unaccounted(<the whole report text>)` returns nothing unplaced, or every
      name it returns has a stated exclusion with its evidence. A clean result
      means the report placed what the TOOLS returned - never that nothing was
      missed; a member no tool returned cannot appear in it, which is why §1.1
      and Appendix B exist.
- [ ] The header and verdict are filled in, and every figure traces to a
      section below.
- [ ] §0 names every library contributing nothing, including any
      `INDEXED BUT EMPTY`, or says there are none; the header carries the
      index build date and the fact density per library.
- [ ] §1 says what was read, names what was unavailable, and surfaces any
      comment that changed the requirement. A ticket file that was empty,
      unreadable or an image with no text layer is stated there.
- [ ] §1.1 lists every acceptance criterion with code or `no code found`, and
      every `no code found` row is repeated in §9. If the ticket has none, §1.1
      says so in those words.
- [ ] Every member in Appendix B was opened and carries a `member:line` from
      inside it. No row reads only "existing reader", "unrelated" or "false
      positive of the text search".
- [ ] Every defect in §6 states the members that change under the in-place
      repair and under carrying the value through the call chain, and which
      route the recompile and edit counts assume.
- [ ] §3.1 states `Requires a new database column: YES/NO` in those words, and
      any "field exists" claim is backed by its writers in
      `where_is_field_used`.
- [ ] §3.1b is present even when no data file changes, and gives every file
      name a row with a status, plus the ambiguous, duplicate-template and
      not-in-extraction blocks. A report without it is not finished.
- [ ] §3.2 rows carry the local name and `member:line`.
- [ ] Every program whose `LINKAGE` the change reaches was run through
      `program_interface`; its mismatches are in §6 and its `NOT SIZE-CHECKED`
      callers are compared by hand or listed in §9.
- [ ] §4 has the tree and the table.
- [ ] §5 gives counts by tier, the call that produced each, and every name - or
      zero with evidence.
- [ ] Appendix A's counts equal the header's and §5's.
- [ ] No answer in the working set still says `Raise limit past N`, has an
      unwalked `PAGE 1 OF n`, ends in `[CUT AT ...]`, carries an unresolved
      `*** HARD STOP ***`, or withholds lines in a member where one could be a
      width or an offset. Only then describe the sweeps as complete.
- [ ] Every count names its population in the same sentence.
- [ ] Every probable and possible row carries its own reason, and every
      inferred fact is marked where it is used.
- [ ] A member in two libraries has a row for each.
- [ ] Where a manual analysis or a client figure exists, both figures are given
      and the difference is explained by category, with nothing trimmed to
      match.
- [ ] §9 is not empty.
- [ ] Every `<ask>` in the notes without an `<answer>` is in §9, and every
      figure in the report traces to a notes entry or a call re-run for it.
- [ ] §6.1 lists what `recall` returned for the ticket's subjects, or says
      memory held nothing.
- [ ] The durable facts are recorded with `remember`, and any disagreement it
      reported is in §6.1.
- [ ] The file is at `<project root>/<TICKET-ID>-impact-report.md` - the
      absolute path of the session's working directory, checked with `pwd` -
      written as markdown and
      not published as an artifact or web page.

When a check fails, fix the report rather than returning it with a note about
the gap.
