# Change-impact analysis

How to work a ticket through to a report. The rules in `SKILL.md` apply
throughout; the report's structure is in `references/report-template.md`.

## Contents

- What the report answers
- The workflow
- 1. Coverage
- 2. The whole ticket (acceptance criteria; a ticket you cannot read)
- 3. The change class
- 4. The subject (open a member before you exclude it; near-miss names)
- 5. Field questions: the alias set
- 6. Width changes: width-dependent code
- 7. Parameter lists (and the route the fix may take instead)
- 8. The recompile set
- 9. Physical files
- 10. Cross-checking against raw source
- 11. Recall and completeness
- 12. Confidence tiers
- 13. Accounting for every member

## What the report answers

Two questions, with two different answers from two different sources:

| | What it is | Found by |
|---|---|---|
| **Edit set** | members someone must change by hand | `where_is_field_used`, and `search_source` across every member type - these members restate the field or its width in their own text, so no dependency query finds them |
| **Recompile set** | members that only need rebuilding | `what_uses(<layout>, kind='copybooks')` - these are coupled through the compiler, so no text search finds them |

Build each from its own source. An edit set derived from the dependency graph
misses every program that redeclares the field in its own storage; a recompile
set derived from a text search misses everything coupled by `COPY`. The
recompile set is the easier half, and reports that go wrong usually go wrong
by being silent about the edit set.

## The workflow

Some of the order matters: coverage comes before any count, the change class
decides which of the later rules apply, and the accounting check comes last
because it checks everything before it.

Keep the ticket's notes file from the first step to the last, as
`references/working-notes.md` sets out: an entry after every step that changes
what is known, a checkpoint at the end of every numbered step below. The notes
are what the report is written from, and what the work resumes from after the
context is compacted. If a notes file for this ticket already exists, read it
before anything else and continue from its last checkpoint.

1. Open the notes file and read the whole ticket (§2), every page and every
   comment; list its acceptance criteria in the notes before any tool call;
   `recall` what earlier tickets recorded about its subjects
   (working-notes.md, Ticket memory).
2. Establish coverage (§1).
3. Classify the change (§3).
4. Find the subject (§4).
5. Build the edit set (§5-§7), the recompile set (§8) and the physical-file
   inventory (§9). These inform each other; when one turns up a new name, run
   it through the others.
6. Cross-check against raw source (§10), check recall and completeness (§11),
   and give every member a tier (§12).
7. Draft the report from the notes to `references/report-template.md`.
8. Account for every member the tools returned (§13), and close the two
   checks a reconciliation cannot make for you: every acceptance criterion has
   code or an open item (§2), and every excluded member was opened and carries
   evidence from inside it (§4).
9. `remember` the durable facts; add any disagreement with an earlier ticket
   to the report's risks.
10. Deliver the report file.

## 1. Coverage

`coverage_report` prints a row per library - files on disk, how many are
recognised as source, how many are indexed - and names every library that
contributes nothing. Each library is in one of three states, and they are
different facts:

| State | Means | A query about it returns |
|---|---|---|
| covered | files present, members indexed | the truth |
| unmapped only | files present, none recognised as source | silence |
| empty | the directory exists and holds nothing | silence |

Before the first count:

- Read the per-library block and compare it with the library directories under
  the source root. A library on disk that the report does not mention is the
  case that matters most.
- Probe each library the ticket touches with `find_object` on one member you
  expect there. A library can look covered and still be shallow.
- Record each library as covered, unmapped only or empty, and put that list in
  the report's coverage section.

While any library contributes nothing, state every count as a floor. If the
ticket's subject plausibly lives in such a library, say so and do not
estimate. A few empty libraries can hold a large share of a change set, and a
report that gives only a grand total of members indexed hides that entirely.

`coverage_report` also lists this extraction's ceilings - unresolved copy
edges, copybooks missing from the extraction, members that failed to parse,
languages parsed shallowly - and its permanent blind spots. §11 uses them.

## 2. The whole ticket

Read every part of the ticket before the first tool call:

| Part | Why it matters |
|---|---|
| Description | the original ask, and often the least current part |
| Every comment, oldest to newest | where the requirement usually settles - "we tried that, it is actually the credit-hold flag" arrives in a comment and the description is never edited |
| Attachments and screenshots | a screenshot often names the display file in its title, and an error shown in one is a message id you can look up |
| Linked, parent and duplicate tickets | why a previous attempt failed is rarely restated |
| Status and resolution history | a reopened ticket means the obvious fix was already tried |

### Read the ticket file with the reader, not by eye alone

A ticket arrives as a file - a PDF print, a Jira XML export, a Word export.
Run it through the reader before anything else:

```
alphaplus-kb ticket "<the ticket file>"
```

It writes `<project root>/.alphaplus-work/<TICKET-ID>.ticket.xml` and prints a
summary. That file carries the fields, the description, EVERY comment in
order, the acceptance criteria under their own ids, the identifiers worth a
tool call, and a `<quality>` block naming any page it could not read. Read it
in full before the first tool call.

When the hook is installed (`setup` does it), a message naming a ticket file
has already been through the reader and the same XML is in front of you - use
it rather than running the command again.

Three answers the `<quality>` block gives, and what each requires:

| What it says | What it means | What you do |
|---|---|---|
| `pages-with-text` equals `pages` | the whole file was read | carry on |
| `unreadable="3,4"` | those pages are images | read them yourself, page by page, and say so in §1 |
| a note that the file is empty or encrypted | there is no ticket input | stop and ask for the export again; never analyse from the title |

The reader never interprets the ticket. It is the ticket as the file carries
it, so anything it could not place is in `<unplaced>` and the raw page text is
at the end - read those before concluding a section is missing.

Tickets often arrive as PDFs; read every page, since the comment thread is at
the end.

### The acceptance criteria are the work order

The description says what went wrong. The acceptance criteria say what has to
be true when the work is done, and a delivered change usually satisfies all of
them - including criteria that have nothing to do with the reported symptom.
An analysis driven by the symptom alone answers part of the ticket and reads
as if it answered all of it.

1. Find the numbered list, however the shop writes it: `AC-001`, `AC 1`,
   `OQ-7`, `Akzeptanzkriterien`, `Abnahmekriterien`, a checklist, or numbered
   bullets in the description or a comment.
2. Put every criterion in the report as its own row (§1), in the ticket's own
   numbering and wording.
3. Give each row code, or say plainly that you found none:

   | Criterion | Code that satisfies it | Confidence |
   |---|---|---|
   | AC-001 widen the field | `XYZMAS:14`, `XYZ100:640` | Confirmed |
   | AC-007 mark the position | **no code found** - the analysis could not map this criterion; ask the reporter | - |

4. A criterion with no code is an open item in §9, not a silence. It is the
   most likely place for a member the rest of the analysis never reaches.
5. If the ticket carries no acceptance criteria at all, write that line
   explicitly: `Acceptance criteria: none in the ticket.`

### A ticket you cannot read is a finding, not a starting point

Check the ticket file before reasoning from it, and say what you found:

| What you see | What it means | What to do |
|---|---|---|
| 0 bytes, or the file will not open | there is no ticket input | Stop and ask for the export. Do not analyse from the title |
| Text search inside the PDF returns nothing | it is an image of a page with no text layer | Read it page by page as images; say in §1 that it was read by eye |
| Some pages unreadable | part of the requirement is unseen | Name the pages in §1 and in §9 |

Never infer a requirement from the file name or the ticket key.

Where a comment and the description disagree, the comment is usually the more
current. Say which reading you followed and what would change under the
other:

> The description asks for X. Comment 4 (developer, 12 Mar) says X was tried
> and the cause is Y. This analysis follows Y; under X the recompile set would
> differ.

Keep the two kinds of comment apart. **Ticket comments** are primary input.
**Source-code comments** - the header block in a COBOL member - are not
evidence; facts come from executable code, and a header that disagrees with
its code is a finding.

Record what you read in one line - `Read: description, 9 comments, 2
screenshots, linked TICKET-1180.` - and name anything that was not available.

## 3. The change class

Impact depends on the kind of change, not on the object changed:

| Class | Must be edited | Recompiles |
|---|---|---|
| Add a field | the DDS, and every program that must populate, validate or display it - a judgement, not a query | everything that copies the layout |
| Widen or retype a field | the DDS, and every width-dependent site (§6) | everything that copies the layout |
| Rename a field | the DDS, and every reference under every alias (§5) | everything that copies the layout |
| Delete a field | the DDS, and every reference under every alias | everything that copies the layout |
| Change a key or record format | the DDS, and programs that rely on arrival order or the format name | everything that copies the layout |
| New value in an existing field | validation, level-88s, screen text, the message file | nothing - no layout changed |
| Change a parameter list | the program and every caller (§7) | nothing; the callers are edits |
| Logic only | the one program | itself |

The recompile set is identical for the first five classes, so the recompile
rule alone gives the same answer to all of them and never reaches the edit
question. Two classes carry a particular risk: a widen or retype breaks
programs that never reference the field (a group move, a fixed-width record, a
screen position), and a parameter-list change fails silently at run time.

If the ticket does not say which class it is, name the class you assumed and
what would change under the other. Put the class and the target value - for
example `EMAILADR A(50) -> A(100)` - at the top of the report; two analyses of
the same ticket at different target widths are not comparable, and otherwise
nobody notices until reconciliation.

## 4. The subject

If the ticket names an identifier, `what_uses(<name>)` confirms what it is. If
it names nothing, three tools answer three different questions:

| Tool | Answers | From |
|---|---|---|
| `trace_ticket` | which members | facts; pass the ticket text as written |
| `route_ticket` | which area or menu | written descriptions - a lead, not evidence |
| `find_similar` | what else reads like this | written descriptions; takes a menu, area or library |

`trace_ticket` ranks the data a ticket is about, so its top rows are usually
files; turn one into programs with `who_touches_file(<file>, mode='write')`.
Its first line, `NOT IN THE INDEX:`, names every identifier-like word in the
ticket that matched no member, field, table or file. Resolve each before
reading the ranking: `search_source(<word>, member_type='CODE')`, then
`find_object`. A name absent from the index may be a program-side alias, a
literal inside code, or sit in a library that was not extracted - and a ticket
whose key identifier matched nothing is not yet understood, whatever the
ranking shows. A field the ticket names ranks the files and copybooks that
declare it (`declares <field>, named in the ticket`); description-text matches
rank below both.

### The target table: follow the writers, never the DDL member

When a ticket is about data a process loads or maintains, the verdict rests on
which table that process actually writes. Establish it this way before
anything else is built on it:

1. Start where the data enters - the load program, the import CL, the
   interface member the ticket names.
2. Follow the writes hop by hop: `what_it_touches(<member>, kind='files')` for
   what it writes, then `who_touches_file(<file>, mode='write')` and the CL
   around it for where that file goes next. A `CPYF` writes its `TOFILE` and
   only reads its `FROMFILE`; a staging file copied onto a live one is one hop.
3. Stop at the table nothing further copies from. That is the target.
4. Quote its `LABEL ON TABLE` (`describe_file(<table>)` shows it) and check the
   label agrees with the ticket. A label naming a different purpose - another
   maker, an archive, a work copy - means the chain went wrong.

An SQL DDL member is not a table. One member often creates several, and the
tools refuse to answer for a member that does (`... does not answer for the
member - ask about one`). A table named by any route other than this writer
chain is `inferred`, and a verdict built on it says so. Record each hop in the
notes as a `<fact>` with its evidence.

### How each program is started

For every program in the edit set, establish what starts it:

1. `who_calls(<program>, depth=4)`. The answer ends with `ROOTS` (nothing in
   the source calls them and no menu offers them), `STARTED FROM A MENU`, and
   `NOT A ROOT YET` (still called from above the depth).
2. Follow every `NOT A ROOT YET` name with its own `who_calls` until each
   chain ends in a root or a menu. A chain left open is listed in section 9.
3. A root is scheduler-started, operator-started, started by another system,
   or dead. The source cannot tell these apart: name the roots in the report
   and ask the client which they are.

### A header that says a member is retired

`find_object` and `who_calls` print `HEADER SAYS INACTIVE` when a member's
header block calls it inactive, obsolete, `nicht mehr aktiv` or similar. The
comment is never evidence, and neither is its absence. Weigh it against
reachability, which the same answer gives:

| Reachability | What to report |
|---|---|
| `STILL REACHABLE` - callers or a menu option | active; the header is out of date, or its callers are dead too (check their headers the same way) |
| `NOTHING IN THE SOURCE REACHES IT` | at most **Possible** that it is retired - a scheduler entry can still start it. Keep it in the edit or recompile set, marked Possible, and ask the client |

Never drop a member from a set because its header says it is retired.

### Open a member before you exclude it

A tool returns a member because something tied it to the ticket's own
identifiers. Excluding it from the file name, the tool row, or the matched
line alone is a guess with a tidy sentence on it - and it is how the one
member a change depends on gets filed as "not disturbed".

Before any member leaves the scope, read inside it:

1. `read_member(<name>, start=1, count=60)` - the header block says what the
   member is for, in the shop's own language. This is where a program states
   the rule the ticket is really about.
2. `read_member` around every matched line, with enough context to see what
   the statement does.
3. `what_it_touches(<name>)` - the files and fields it actually reaches.

Then write the exclusion with evidence from **inside** the member:

> `PRC410` - excluded. Its header at `PRC410:118-131` says the flag it reads
> decides which rate table a line is priced from; this change alters neither
> that flag nor the fields it guards.

"Existing reader, not disturbed", "unrelated module" and "different subsystem"
are not exclusions - they are assumptions. An exclusion with no `member:line`
from inside the member is not accepted (see §13 and the checklist).

The rule applies hardest to the members a search returned **because they
declare or read the ticket's own fields**. Those are the shop's own
documentation of what those fields mean.

### A near-miss name is a question, not a false positive

A text search matches substrings, so a search for `W-RATE` also returns
`POS-RATEKZ`. The match is not the same identifier - but the OTHER identifier
is now a fact about the ticket's subject area, and it may be the one the
change is really about.

Never write off such a hit as a false positive of the text search. For each
distinct name the search revealed:

1. `where_is_field_used(<other name>)` - what declares it, who writes it.
2. `describe_file` on a declaring file, and read the header of one program
   that uses it - what is this field FOR?
3. Record the answer in the notes as a fact, with its evidence.

Only then decide. "Different identifier, same area, not used by this change,
evidence `<member:line>`" is an exclusion; "false positive" on its own is not.

## 5. Field questions: the alias set

A field almost never appears in a program under its DDS name:

```
COPY DDR-RCUSTREC OF CUSTFILE PREFIX BY "AA-".
```

That one line imports every field of the record and renames each one, so
`EMAILADR` exists in that program only as `AA-EMAILADR`, and under a different
prefix in every program that copies it. One file is copied under many prefixes
and one prefix is reused for different files; the binding is per program, per
`COPY` statement, and there is no global prefix table.

Everything that gives a field another name:

| Mechanism | Effect |
|---|---|
| `PREFIX BY` | renames every field of the record |
| `COPY ... REPLACING ==A== BY ==B==` | textual substitution before compile |
| `REDEFINES` | a second name over the same bytes |
| `RENAMES` (level 66) | a second name over a span |
| level-88 | a condition name standing for a value |
| `<item> LIKE <field>` | a new name that inherits the width |
| `<field> OF <record>` | a qualified name split across tokens |

A text search for the DDS name finds only the one spelling that is not there,
and it fails without any sign of failing. Instead:

1. Call `where_is_field_used(<field>)`. It builds the alias set from each
   program's own `COPY` statements and prints each program's local name in the
   `called` column.
2. Quote the local name in every row of the report. Without `AA-EMAILADR`
   beside `CUSTFILE.EMAILADR`, the reader has the right member and still cannot
   find the line.
3. If the answer carries a `RECALL CEILING` line, put it in the report: a
   program whose layout did not expand has unknown usage, not none.
4. Cross-check with `search_source` and explain both differences (§10).

`CC-EMAILADR`, `CC-EMAILADR(2)`, `CC-EMAILADR(01:01)` and `:H-CC-EMAILADR` are
all the same field, and a search for `EMAILADR` as a word finds none of them.

**Whether a field is free to reuse.** `where_is_field_used` reports how many
programs write the field and lists them under `WRITTEN BY`. A field with
writers already carries a meaning, and repurposing it changes what those
programs produce. Treat an existing field as available only when it has no
writers, and name the writers when it has.

## 6. Width changes: width-dependent code

The must-edit test for a widen or retype is whether the code assumes the
current size. That is a property of code, so no list of declaring members
answers it. `where_is_field_used` reports `WIDTH-SENSITIVE` sites and flags
`offset` and `group move` rows; search for the rest.

| Construct | Why it breaks | How to find it |
|---|---|---|
| Reference modification `(n:m)` | literal offsets still point at the old bytes | flagged `offset`; `search_source("<field>\s*\(\s*\d+\s*:")` |
| A field carved from a flat buffer by position, `MOVE L-BUFFER(nnn:len) TO <field>` or CL `%SST(&VAR nnn len)` | worse than truncation: every later field in that record shifts, the program still compiles, and every downstream value is wrong. If another system writes the buffer, its layout is a contract with that system | `where_is_field_used(<field>)` lists them under `REACHED BY BYTE POSITION`; `describe_file(<file>)` gives each field's byte range. For a field before the changed one, check every site whose offset lies after it. A `position_not_mapped` gap in `what_it_touches(kind='unresolved')` is a site read by hand |
| Hard-coded `PIC` in WORKING-STORAGE or LINKAGE | the program holds its own copy at the old width | `search_source("<field>\s+PIC")` |
| `LIKE <field>` | follows the target at recompile - no edit - but it names the file, and code reading the item may still assume the old size | `search_source("LIKE\s+.*<field>")` |
| Group-level move | moves a whole record into a differently sized area | flagged `group move` |
| `MOVE CORRESPONDING` | pairs fields implicitly at compile time | `search_source("MOVE\s+CORR")` |
| `OCCURS` table | the entry size changes, so every subscript moves | `search_source("OCCURS")` around the field |
| Fixed-width export or import record | byte positions in a flat file | the record used in a `WRITE` or `READ INTO` |
| Screen and print positions | a display or printer file field has its own column and width | `explain_screen`, comparing the declared widths |
| Embedded SQL DDL, `<field> CHAR (nnn)` | the width is written into program text; a recompile does not change it | `search_source("<field>\s+CHAR")` |
| CL `DCL ... LEN(n)` passed by reference into COBOL | the parameter is the wrong size at run time and fails silently; no compiler sees it | `search_source("DCL.*<field>", member_type='CLLE')` |

Every construct here is an edit except `LIKE`. State the action on each row:
listing a construct as must-edit when it is not inflates the edit set, and an
inflated set is as unusable as a short one.

Interface programs - those exposing data over SQL or to another system - tend
to carry the width twice, once as a COBOL `PIC` on a host variable and once as
`CHAR (nnn)` in the SQL they run. Neither moves when the DDS moves, and a whole
library of them can sit outside a layout-driven analysis.

### Sweep the whole family, to completion

The ticket names one or two fields; the file usually holds related ones, and
the same logical field often appears at different widths on different files.
Widening one and leaving the others is the next defect. So:

- Find which member types hold members before trusting a nil: `search_source`
  on a type that holds nothing says so and names the types that do.
- List the related fields with `describe_file` and the declarations around the
  named field, and note every width each carries.
- Every sweep covers every code type. Use `member_type='CODE'` (COBOL,
  copybooks, CL, commands, RPG, SQL), or a list such as `'CBLLE,CPY,CLLE'`. A
  scoped answer ends with `NOT SEARCHED:` naming the types it skipped; each of
  those is searched too, or named in the report as not searched, with the
  reason. A sweep result quoted without that line is a zero nobody checked.
- Follow the value into receiving fields with other names - `MOVE AA-EMAILADR
  TO L-ADDR-IN-EMAIL1` - and search those names too.
- Search width-bearing syntax as well as the field name: `PIC X(nn)`,
  `CHAR (nnn)`, `DCL ... LEN(nn)`, `(offset:length)`. A program can hard-code
  the width without naming the field.

`search_source` counts every match. Its header names any member whose extra
lines were withheld from display, such as `PGM600 +7`; when one of those lines
could be a literal width or an offset, call again with a higher `per_member`
and look. A `*** HARD STOP ***` banner means the listed members were not read:
they are unknown, not unmatched. Split the search by `member_type` so each part
completes - the split is complete only when the parts together cover every type
the first `NOT SEARCHED:` line named - and if one cannot, report its count as a
floor.

## 7. Parameter lists

A record-format change fails loudly at open with a level check. A parameter
length mismatch fails silently: the called program lays its `LINKAGE` record
over the caller's storage, reads bytes it was never given and writes over
whatever follows. The CL `DCL ... LEN(n)`, the COBOL `LINKAGE SECTION` (and the
copybook it copies) and every caller are one change.

1. `program_interface(<program>)` for every program whose `LINKAGE` or
   `USING` the ticket touches, and for every program that copies a changed
   copybook into its `LINKAGE`. It returns each parameter's size in bytes, the
   copybook its layout comes from, every CL call with its argument sizes, and
   the mismatches.
2. Quote each `OVERRUN` and `MISSING` row in section 6 of the report as a
   defect, with both evidence lines. An `OVERRUN` that exists before the change
   is still a finding: the ticket widens the damage.
3. A width change to a `LINKAGE` copybook changes the parameter size. Every CL
   caller's `DCL LEN` must change with it; list them from step 1 as edit sites.
4. `program_interface` checks CL callers only. Its `NOT SIZE-CHECKED` line names
   the COBOL callers: read each one's `CALL ... USING` items and compare them by
   hand, or list them under "could not be established".
5. A parameter shown with size `?` could not be sized (a copybook not in the
   extraction, `OCCURS DEPENDING ON`). No caller is checked against it; say so.
6. State that a caller outside the extraction cannot be ruled out.

### The route you propose is one of several

A report proposes a repair. Developers routinely deliver a different one that
satisfies the same criteria - most often by threading a new parameter or flag
down the call chain instead of correcting the value in place. The members that
change are then the CALLERS and the CL drivers, not only the program with the
defect.

For every defect you name, write both:

| | Members that change |
|---|---|
| Route A - repair in place | the program holding the wrong value |
| Route B - carry the value in | that program, **plus** every caller passing it, the CL that builds the call, and whatever computes the value upstream |

Use `program_interface(<program>)` and `who_calls(<program>)` for route B's
member list, and put those members in the report even when you recommend
route A - marked `edit only under route B`. A member named ten times as a
caller, an override holder or a file creator, and never once as an edit site,
is the signature of this miss: decide explicitly whether it is edited, and say
which route the decision assumes.

## 8. The recompile set

A recompile is forced by a change to a record format's level identifier,
checked when the file opens.

`blast_radius(<name>)` gives the orientation in one call: counts of callers,
callees, copying members, logical files, writers, readers, existence checks and
overrides, followed by the calls that turn each count into names. Use it to see
which layers a change reaches; the names come from the calls it lists. If it
fails, go straight to `what_uses(<layout>, kind='copybooks', limit=4000)` and
say in the report where the counts came from.

Pass `limit=4000` to every call in this section - these are the lists that
fold, and a folded recompile set is a wrong report.

| What changed | What recompiles | Call |
|---|---|---|
| Copybook | every member that copies it | `what_uses(<cpy>, kind='copybooks', limit=4000)` |
| Physical or logical file | every program copying the layout under any name in the family | `what_uses(<file>, kind='copybooks', limit=4000)` |
| Display or printer file, a field added, removed, resized or moved | the file, and every program that declares it, copies its formats or writes them | `what_uses(<file>, kind='screens', limit=4000)` |
| Display or printer file, constant text or attributes only | the file only; no program recompiles | `explain_screen(<file>)` to show no field changes |
| Program called dynamically | itself | - |
| Module bound into a service program | rebind it, and everything bound to a changed signature | `what_uses(<pgm>, kind='calls', limit=4000)` |
| Message file, data area | nothing | - |

**Display and printer files.** Classify each one the ticket touches, in the
report, as *text-only* or *field-level*, from the lines that change: a quoted
constant, a `COLOR`/`DSPATR`/`TEXT` keyword or a position of a constant is
text-only; any named field, its length, type, usage or order is field-level. Only
a field-level change alters the record format's level identifier, so only it puts
the programs in the recompile set. `what_uses(kind='screens')` lists users by
how they are tied - declares (ASSIGN), copies formats, format writes - and any
of the three recompiles. A `NO PROGRAM USES` answer names what it checked; one
marked `NOT PROVEN` names source lines that were not read as a link, and each is
read before the zero is reported.

**The layout family.** A physical file and every logical file over it share one
layout, and which name a program wrote in its `COPY` is incidental. `what_uses`
and `blast_radius` answer for the whole family and print which names they
counted (`Counted across the layout family: ...`); quote that line. A logical
file with several record formats has one based-on physical file per format, so
check each.

**A program's own copybooks.** When the edit is inside a program, its
callers are not the whole reach. `blast_radius(<program>)` adds two blocks for a
program: `COPIES` lists every copybook it copies with how many other members
copy each, and `LINKAGE CLOSURE` counts the CL and COBOL callers and the members
copying its LINKAGE copybook. Any copybook the ticket edits puts all of its
copiers in the recompile set, and a LINKAGE copybook also puts every caller in
the edit set (section 7). Quote both blocks in the report.

**Copies through copybooks.** A copybook that itself copies the layout carries
the coupling a level further. `what_uses` follows these and reports the split -
direct copies plus copies through named intermediate copybooks - so quote it. A
copybook is also itself an affected member and belongs in the lists, not only
the programs that use it.

**Three things that bend the rule:**

- `LVLCHK(*NO)` suppresses the level check, so the program runs against the
  changed file and reads the wrong bytes. Run `resolve_overrides` over every CL
  that drives a program in the set; its `lvlchk` column and `RISK:` line name
  each such override. Every program reached through that CL belongs in the
  recompile set as mandatory, and the report says why: nothing will fail if one
  is missed.
- Static SQL rebuilds its own access plan, but a record-format change still
  level-checks an SQL COBOL program like any other.
- Generated source - precompiler output and `COPY` expansion - often sits
  beside real source in an extraction. It contains the field and is not an edit
  site, because the next compile regenerates it. Signals, most reliable first:
  a generated-source directory rather than the shop's source file (for example
  a `QZZ`-prefixed one beside `QDDSSRC`); `find_object` noting that a member
  looks like generated metadata shadowing a real one; a same-name member of very
  different size; a `DATE-COMPILED` marker or a generated-by banner. Exclude
  generated members and say that you did and why, since a silent exclusion is
  indistinguishable from a miss.

## 9. Physical files

A DDS file member is a template. It defines a record and holds no rows. The
rows live in data files created from it at run time, and one template
routinely becomes several:

```
$CRTF FROMFILE(<template>) TOFILE(<data file>)
```

A COBOL program never names the data file. It names a token, and the `OVRDBF`
in whichever CL launched it decides which file opens - so the same program can
read orders under one caller and quotations under another, with nothing in the
COBOL to say which.

| Layer | What it is | Its evidence | On a layout change |
|---|---|---|---|
| Template | the DDS physical or logical file member that defines the record | the source member | edit, once |
| Data file | a file created from a template at run time | the CL that creates, allocates, checks, copies and deletes it | convert each one, unless it is rebuilt every run |
| Program-side name | a token in a COBOL `SELECT` or `COPY` | `OVRDBF FILE(p) TOFILE(d)` in the calling CL | nothing |

What a name is follows from that CL evidence, not from how the extraction
stores it: a name delivered as a physical or logical file member can still be a
data file built from another template. Decide each name's role from
`resolve_files`, not from its file type.

Call `resolve_files(<name>)` for every file in scope. It accepts a template, a
data file or a program-side name, and returns every related name with a status,
the mechanism, the evidence, and how many CL members override onto it:

| Status | Action |
|---|---|
| `TEMPLATE` | edit - the layout lives here |
| `TEMPLATE-DUP` | resolve - the same name in more than one library; which compiles depends on the library list |
| `DATA-LIVE` | convert |
| `DATA-TRANSIENT` | none - dropped and rebuilt each run |
| `OVERRIDE-ALIAS` | none, but name its targets |
| `RUNTIME-VARIABLE` | convert once per value - the name is built at run time, and how many exist is a question for whoever runs the system |
| `MATCHCODE` | rebuild after conversion |
| `COPY-TRANSIENT` | none - a work or save copy |
| `NOT-IN-EXTRACT` | flag - referenced, but not in the extraction |
| `DATA-BUILT` | convert - built from a template by CL, in an extraction that holds source only; the object cannot be confirmed on disk, so say so |
| `LOADED-INTO` / `LOADED-FROM` | follow - a `CPYF` copies the data from one into the other; the target is the table the load feeds, and both layouts must fit the change |
| `SWAPPED-INTO` / `SWAPPED-FROM` | high risk - a `RENAME` replaces the live table with another at run time; every layout change is made to both, or the swap breaks the table |

### Tables another system replaces

A table can be rebuilt and swapped in by another product - a script that
creates a work copy, loads it, drops the live table and renames the copy onto
it. The live table then takes the work copy's layout on every run, so a change
made only to the live table is undone by the next swap.

For every table in scope - staging and target - check both:

1. `resolve_files(<table>)`: `SWAPPED-FROM`, `SWAPPED-INTO`, `LOADED-FROM` and
   `LOADED-INTO` rows name the other half of each swap and load, wherever the
   script lives.
2. `search_source("<table>", member_type='SQL')` and the same over every
   library that is not the application's own, then read each hit for
   `RENAME`, `DROP`, `TMP_`, `OLD_` or `_NEW` naming. A match is a High row in
   the risks section with both tables named.

A swap in a library that contributes nothing to the index cannot be seen;
name that library in §9.

`coverage_report` states which kind of extraction was indexed. In a full
download a physical file is confirmed by its data member; in a source-only
extraction, such as a version-control checkout, every data file is known from
the CL that creates and opens it, and `DATA-BUILT` is its normal status.

Give every name a row, including the ones that need no action. A missing row
reads as "not affected", which is a stronger claim than "an alias that resolves
elsewhere".

### What the CL proves

A data file is never declared. CL creates, allocates, checks and deletes it,
and those statements are the evidence that a name is a real file rather than a
token:

| Written in the CL | What it proves |
|---|---|
| `$CRTF FROMFILE(t) TOFILE(d)` | `d` is built from template `t` - the definitive edge |
| `$CPYF`, `$RNMF`, `$DLTF` | a reorganisation cycle: work and save copies of a live file |
| `$ALCF OBJ(d)`, `$DLCF OBJ(d)` | `d` is allocatable, so it is a real file |
| `$CHKF FILE(d)`, `RTVMBRD FILE(d)` | an existence or record-count check on a data file |
| `$MC<name> MODE('CRT')` | a site macro building match-code files for `<name>` |
| `OVRDBF FILE(p) TOFILE(d)` | program-side name `p` opens `d`, in this job only |
| `OVRDBF ... TOFILE(&VAR/d)` | the same, with the library read from a variable at run time - inferred; ask which library |
| `OVRDBF FILE(d) TOFILE(&VAR/d)` | the names match, so this redirects the library only; `d` is not an alias |
| `TOFILE(M *CAT &CO *TCAT '_FILE')` | the name is built at run time: one file per value of the variable |

A name built with `*CAT` or a variable multiplies the conversion by a number
the source cannot give. Say so, and leave the multiplier unknown rather than
counting one file.

Naming conventions are worth reading and not worth asserting. One shop's
prefixes may mean live data, save copy, work copy and per-company file; that is
a convention of that shop, and reasoning from a prefix is how a file gets
attributed to the wrong template. Confirm each against the `$CRTF` or `OVRDBF`
evidence.

### What the COBOL proves

The CL says which file; the COBOL says which layout:

| Written in the COBOL | What it tells you |
|---|---|
| `COPY DDS-ALL-FORMATS OF t` | binds template `t`'s layout; recompiles on any layout change |
| `COPY DDR-ALL-FORMATS OF t PREFIX BY "xx-"` | the same binding, often through an intermediate copybook |
| `LIKE <fld> OF <name>` | auto-sizes, but names the file - sometimes the only place the file name appears |
| `<fld> OF <rcdfmt> OF <host>` | an SQL host structure: record format `<rcdfmt>` of file `<host>` |
| a field prefix such as `AA-` | which record instance; one layout copied under several prefixes means one program may write all of them, so check every prefix |

Decide which file a program touches from the CL, not the COBOL.

### Ambiguous names and duplicate templates

When one program-side name resolves to several files, `resolve_files` prints an
`** AMBIGUOUS **` block. It is a risk finding: a program declaring that name
works on different data depending on its caller, and no compiler will say so.

When one template name exists in more than one library, it prints a
`TEMPLATE-DUP` row and a `** DUPLICATE TEMPLATE **` line. Which copy compiles
depends on the library list at build time, which the source cannot say; put it
in the report as an open question naming both libraries, addressed to whoever
controls the build.

The files in this section are run-time objects, not source members. Somebody
converts them on the machine; nobody edits them in the repository. Say that in
the report, because a list of file names beside a list of programs otherwise
reads as though both are edits.

### Normalise before counting

Decide what each name is - template, data file, program-side name, field or
program - before it enters a list; a count that mixes them cannot be
reconciled. Two queries look alike and answer different questions:

| Question | Query | Finds |
|---|---|---|
| Who reaches this file? | every `OVRDBF ... TOFILE(<file>)` | the alias set |
| Where does this name go? | every `OVRDBF FILE(<name>) TOFILE(...)` | the ambiguity |

Run both; only the second finds one name resolving to several files.

## 10. Cross-checking against raw source

For the primary object of every ticket, compare the tool's list with a raw
search:

```
search_source("<name>", member_type='CODE')
```

and explain both differences:

| Difference | Usually means |
|---|---|
| in the tool's list, not the raw search | correct - a copy through an intermediate copybook, an alias, a coupling through a logical file. A text search cannot see compile-time coupling; keep these |
| in the raw search, not the tool's list | a real miss - name the member and the reason: a layout that did not expand, a member that failed to parse, a language not analysed |

If a difference cannot be explained, say so rather than choosing the number
you prefer.

## 11. Recall and completeness

### Recall

A nil or short result is a hypothesis. This extraction's ceilings are in
`coverage_report`; the usual ones:

| Gap | Effect |
|---|---|
| a copybook or layout not in the extraction | field names in that program could not be resolved - usage unknown, not none |
| RPG members | field usage is not extracted - unknown, not none |
| SQL | parsed shallowly; host-variable usage is under-reported |
| members that failed to parse | invisible to every query |
| libraries not extracted | unreachable by any tool |

If `where_is_field_used` printed a `RECALL CEILING` line, it goes in the
report. A tool or shell call that failed is not a completed step: re-run it,
or record the gap.

### Completeness

Report no count without the names behind it. A recompile set of 400 members is
400 names, and that list is the deliverable; "and 43 others" is a wrong report,
not a short one. The header counts must equal the names in the appendix, so
count them.

When your total differs from a number you were given, keep your evidenced list
and explain the difference. Dropping evidenced members so a total lands on a
manual analysis's number fabricates agreement, and hides the very difference a
reconciliation exists to find.

**Scope tiers.** A change set is a series of widening scopes. Show them as one
cumulative table and headline the tier the ticket asks for; the wider tiers
follow as a labelled extension for the reader to accept or decline:

```
  66  programs naming the field the ticket names     <- the ticket's scope
 +16  programs receiving the value into a differently named field
  82  programs
  +4  CL programs
 +43  DDS file and screen definitions
  +7  copybooks
  +1  embedded SQL DDL
 ---
 137  everything the tools can evidence
```

**Compare like with like.** Before calling a difference a discrepancy, find out
what the other list counted. A manual analysis built from recompile attachments
counts programs, not DDS files or copybooks; setting the 137 above against its
68 reads as an error that does not exist. State the basis of both counts in one
sentence, compare the tiers that match, and name what each side is missing - a
manual list can omit a whole library because it was never opened.

**Name the population with every count.** `137 members` cannot be checked;
`70 programs naming EMAILADR or EMAILALT` can.

**One member in two libraries is two rows.** Two copies of one name with
different line counts are two programs; editing one and compiling the other is
a mismatch no compiler catches.

## 12. Confidence tiers

Every member sits in exactly one tier, which lets the report include uncertain
members without overstating them:

| Tier | Earns it | Reads as |
|---|---|---|
| Confirmed | a compile-time or run-time coupling read from executable source | "these recompile" or "these change" |
| Probable | the coupling is real but its target was resolved, not read - an `OVRDBF` through a variable, an SQL table matched by name | "probably affected - <reason>" |
| Possible | a text match or a heuristic - a hard-coded `PIC`, a `trace_ticket` rank | "these may also change, at lower confidence - <what matched>" |

Include uncertain members in their tier rather than leaving them out: a
Possible row with its reason costs a reader seconds, and a missing row can cost
a production defect. The tier records how well a row is evidenced, not how
convenient the total is, so an evidenced member is Confirmed however large the
list becomes; an "optional" tier is where evidenced members get lost. Promote
nothing to fill a gap. Give each row its own reason ("reached through
`OVRDBF FILE(WRKF02) TOFILE(&DTALIB/WRKF09)`, library from a variable"), and
give tier totals: `Confirmed 68 · Probable 9 · Possible 14`.

## 13. Accounting for every member

The server records every member name the tools return in a session. Before
delivering, pass it the whole report:

```
unaccounted(<report text>)
```

It returns each returned member that appears nowhere in the report, grouped by
the tool that returned it. Each one needs one of three states:

1. in the change set,
2. in the recompile set, or
3. excluded, with the tool result that justifies the exclusion.

Quoting a member in a risk note does not place it. That is the usual way
members leave a report: a sweep returns several, one is cited as an interesting
finding, and the rest reach no list. Place what it returns, then call
`unaccounted` again. An exclusion reads like this:

> `PGM100`, `PGM110` - returned by `search_source`, excluded: `what_it_touches`
> shows neither copies a changed layout; both reference a copybook whose name
> differs by one character.

Remove nothing from the report just to make the result clean - the ledger
records what the tools returned, not what you kept. A clean result means
nothing leaked between the tools and the report; what the tools never found is
the separate question §1 and §11 answer.
