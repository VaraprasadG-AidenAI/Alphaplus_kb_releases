---
name: alphaplus-impact
description: Analyses change impact on the Alphaplus IBM i (AS/400) COBOL application through the alphaplus knowledge-base MCP tools - which programs a change edits and which it recompiles, which physical files it converts, where a field is used under each program's local name, and how a user reaches a screen. Use for any JIRA ticket, defect, change request, estimate or "what does this affect" question about that codebase, including tickets written in business language that name no program, file or field, and whenever an impact report is requested.
---

## Output contract - read this before anything else

**The deliverable is a markdown file in the project root. Nothing else.**

```
<project root>/<TICKET-ID>-impact-report.md
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

# Alphaplus impact analysis

The `alphaplus` MCP tools answer from an index built from one extraction of
the application's source: COBOL, CL, DDS, copybooks, display and printer
files, menus and messages. Every fact comes from executable code, carries a
confidence level, and cites `member:line`. Extractions differ in which
libraries they hold and what they miss, so carry no counts from one session
to the next - `coverage_report` says what this one holds.

## Four things about this codebase that mislead

**A program never names the physical file it opens.** COBOL names a token in
its `SELECT` or `COPY`; the `OVRDBF` in whichever CL launched the program
decides which file that token opens, often through a library held in a
variable. One DDS template also becomes several data files at run time
through `$CRTF FROMFILE(template) TOFILE(datafile)`. So three names are in
play and they rarely coincide: the template (edited once), each data file
built from it (converted), and the program-side name (no action).
`resolve_files` separates them; `resolve_overrides` shows which files a
program actually opens once its callers' overrides apply.

**A field rarely appears under its DDS name.** `COPY DDR-<record> OF <file>
PREFIX BY "AA-"` imports every field of a record and renames each one, and
every program chooses its own prefix. `EMAILADR` in the DDS is `AA-EMAILADR`
in one program and `CC-EMAILADR` in the next. `where_is_field_used` builds
that alias set from each program's own `COPY` statements; a text search for
the DDS name finds only the spelling that is not there.

**The widest coupling is invisible to a call graph.** `COPY` and `LIKE ... OF`
bind at compile time, so a record-layout change recompiles every program that
copies the layout, including programs that never mention the changed field.
`what_uses(<layout>, kind='copybooks')` finds them. A text search and a
hand-built list both miss them, which is why a tool total is usually larger
than a manual one - and usually right.

**An SQL DDL member is not a table.** One member often creates several
tables, and each has its own writers, readers and columns. Answer for the
table, never the member, and establish which table a process feeds by
following its writers hop by hop to the last copy, then quoting that table's
`LABEL ON TABLE` (`IMPACT-REPORT.md` §4). A verdict about the wrong table is
correct in every detail and useless.

Two smaller couplings behave the same way: a logical file reaching its
physical file, and a display-file indicator such as `CA03` linked to `IN03` in
the COBOL only by its number. The tools follow both.

## Where to start

| You have | Call |
|---|---|
| any name - member, file, copybook, field, format, data area, message id | `what_uses` - it works out which kind the name is |
| a ticket in business language that names nothing | `trace_ticket` with the ticket text as written |
| a data bug on a known file | `who_touches_file(<file>, mode='write')` - the programs that could have written the value |
| a field | `where_is_field_used` |
| a file name, or a name that might be an alias | `resolve_files` |
| a program you are about to read | `program_structure`, then `read_member` over the range you need |
| a question about what the index cannot see | `coverage_report` |
| what earlier tickets established about a name | `recall(subject=<name>)` |

`trace_ticket` ranks the data a ticket is about, so its top rows are usually
files; turn one into the programs that could cause the behaviour with
`who_touches_file`. `route_ticket` and `find_similar` read descriptions a model
wrote, so treat what they return as places to look, never as evidence.

`references/tool-playbook.md` lists every tool with its arguments, what it
returns and how it misleads. It matches the tools the server registers; a name
that is not in it is not a tool, so check it before writing a call into a
plan.

## Confidence

| Level | Means | Say it as |
|---|---|---|
| `exact` | read literally from executable source | a plain statement |
| `inferred` | derived - a library resolved through a CL variable, an SQL table matched by name | "probably", with the reason |
| `unresolved` | something is there and its target cannot be determined | say so, and do not guess the target |

An `inferred` override stated as fact is how somebody edits the wrong
library's data.

## Before you propose any line

Two files, read before the first edit row is written, not after:

| File | What it gives you |
|---|---|
| `references/house-style.md` | how this shop writes a change - the three change verbs, the marks, the structural idioms it reaches for |
| `references/correctness-checks.md` | four checks that are arithmetic, not judgement, and that each exist because a correct analysis shipped a broken change |

**A report can be right about the problem and wrong about the shape of the
fix.** That is the usual failure and it is worse than being wrong, because a
fix in a foreign shape is rewritten by the developer who receives it - so the
analysis was work nobody could use.

Three rules from those files that change how the report reads:

- **A line is never deleted.** It is switched off (`*` in column 7), added
  beneath, or modified in place. Do not write "delete" or "remove" about a
  source line.
- **A finding that means the change is wrong without it is an edit row**,
  marked `required for correctness` - not a paragraph in the risks section. A
  risk table is read once; an edit table is worked through.
- **Superseding a live statement is a pair of rows**, add and switch off, both
  or neither. One row alone leaves both statements running, the later one
  wins, and the fix is dead on arrival with nothing failing.

If `house-style.md` Part B is unfilled, say so in the open items rather than
guessing at an idiom. A guessed idiom is worse than none: it reads as
evidence.

## Rules for every answer

These apply to a one-line question as much as to a full report.

- **Establish coverage before counting anything.** Call `coverage_report`
  first. A library that was never read answers every question with silence,
  and nothing in that silence says so. While any library contributes nothing
  to the index, every count is a floor; say so.
- **A nil is "not found in the index", never "does not exist".** The tools'
  nil answers say what they did not look at - `find_object`, for example,
  distinguishes a source member, a program-side name that an `OVRDBF`
  redirects, and a name that is not in the index. Quote the answer the tool
  gives rather than turning it into an absence.
- **Read the ticket file through the reader first.** `alphaplus-kb ticket
  "<file>"` turns a PDF, Jira XML or Word export into
  `.alphaplus-work/<TICKET-ID>.ticket.xml`: fields, description, every comment,
  the acceptance criteria and a `<quality>` block naming what it could not
  read. A page it could not read is one you read by eye and say so; an empty
  or encrypted file is a stop-and-ask, not a reason to work from the title.
- **Answer the whole ticket, not the reported symptom.** The acceptance
  criteria are the work order: list every one, give each code or `no code
  found`, and treat an unmapped criterion as an open item. A delivered change
  usually satisfies all of them, including criteria the symptom never mentions.
- **A field inside a text column: trace the writer, never a reader.** When a
  field is said to live at a fixed offset inside a character column - a tag in
  an XML payload, a slice of a buffer, a position in a flat record - find
  **every program that writes that column** (`where_is_field_used(field,
  direction='written')`, then `search_source` for the column name) and read how
  it builds it. A program that *reads* the column proves the layout of the
  records it reads and nothing else: quote a reader as evidence for the whole
  column and every other record type is silently wrong. Where the column is
  assembled by `STRING` with `DELIMITED BY SPACE`, or by any variable-length
  part, the offset **moves between records** - say so, and say the fix must
  scan for the marker rather than index to a constant.
- **Open a member before you exclude it.** A tool returned it because
  something tied it to the ticket. Read its header block and its matched lines
  with `read_member`, then write the exclusion with a `member:line` from inside
  it. "Existing reader", "unrelated module" and "false positive of the text
  search" are assumptions, not exclusions - and a near-miss identifier is a
  question to answer (what is that field for?), never a hit to discard.
- **`unaccounted` reconciles what the tools returned, nothing more.** A clean
  result cannot see a member no tool ever named. It is a check against
  dropping evidence, not proof of coverage.
- **A zero comes with what was checked.** "0 recompiles", "no screen users",
  "no callers" is a finding only beside the calls that produced it, the member
  types and libraries they covered, and any gap they reported (`NOT SEARCHED`,
  `NOT PROVEN`, `INDEXED BUT EMPTY`, `NOT SIZE-CHECKED`). A zero without that
  list is not reported.
- **A count is not an answer; names are.** List tools fold above `limit` and
  print `Raise limit past N`, long answers page (`PAGE 1 OF 4`), and an
  oversized answer ends with `[CUT AT ... CHARACTERS]`. Each means the tool
  holds more. Call again - a higher `limit`, the next `page`, a narrower
  question - until every name is on the page.
- **Place every member a tool returned.** Before giving a list of affected
  members, call `unaccounted` with the answer text. The server records every
  member name the tools return in a session, and this names the ones the answer
  does not mention. Give each a place - the edit set, the recompile set, or an
  exclusion with the tool result behind it. A member mentioned only in a risk
  note, or not at all, is the usual way one drops out of an answer.
- **Carry the library with every member name.** The same name exists in more
  than one library, and two copies of one name are two programs.
- **Source-code comments are not evidence; ticket comments are.** A COBOL
  header block that disagrees with its own code is a finding, not a fact. A
  JIRA comment thread is primary input and is read in full.
- **Answer from what the tools returned, not from what a name suggests.**
  `XYZ400` sounding like a batch order program is a naming habit, not a fact.
- **An exclusion quotes the member, not the search.** The hit that surfaced
  a member is the reason to open it, not evidence for setting it aside. Every
  exclusion carries a line read from inside the member, with its number - and
  any member whose header block contains a term from the ticket is read in
  full before it can be excluded.
- **A clean `unaccounted` is not "nothing was missed".** It reconciles the
  members the tools returned against the ones the report placed; a member no
  tool returned cannot fail it. Completeness is answered by the acceptance
  criteria, which are keyed on what the ticket asked for. Never report the two
  as the same thing.
- **Words that mean the same thing are the same thing.** `SPACE` and `SPACES`;
  `ZERO`, `ZEROS` and `ZEROES`; `PIC` and `PICTURE`; `THRU` and `THROUGH`;
  `CORR` and `CORRESPONDING`; `COMP` and `PACKED-DECIMAL`. Every sweep searches
  every form, and the report says which forms were searched next to the count.
  A nil result from a single-form search is a search that was not finished, not
  a finding. `references/cobol-equivalences.md` has the list.
- **Read a program narrowly.** Members run to thousands of lines; take the
  range that `program_structure` or a search gave you.
- **An argument error means nothing ran.** The message names the arguments the
  tool accepts; change them rather than retrying the same call.

## Working notes and ticket memory

A ticket analysis outgrows the context window: dozens of tool calls, each
answer hundreds of lines, and the context is compacted before a large ticket
is done. Anything not written down by then is gone, and the report is then
written from whatever happens to remain. So every ticket keeps two records,
set out in `references/working-notes.md`:

- **A notes file**, `<project root>/.alphaplus-work/<TICKET-ID>.notes.md`, written for
  Claude alone in the tagged format that reference defines. An entry goes in
  after every step that changes what is known, a checkpoint at the end of every
  workflow step. After any compaction or in a new session, read it before the
  next tool call and resume from its last checkpoint. The report is written
  from it.
- **Ticket memory**, through `recall` and `remember`, which carries durable
  facts from one ticket to the next. `recall` the ticket's subjects at the
  start; `remember` what was established before delivering the report. Both
  report where another ticket recorded something different - a disagreement
  goes in the report.

For a quick question that takes a few calls and no report, neither is needed.

## Tickets, change requests and impact reports

Before the first tool call of a ticket analysis, `Read` `IMPACT-REPORT.md`
beside this file in full, and `references/working-notes.md`. If either file is
not there, this is an incomplete or outdated copy of the skill: say so to the
user - running the knowledge base's `setup` installs the complete one - rather
than analysing without the workflow.

Follow `IMPACT-REPORT.md`. It sets out how to build the edit
set, the recompile set and the physical-file inventory, and how to check that
every member the tools returned reached the report.
`references/report-template.md` fixes the report's structure and where the
file is written; `references/worked-example.md` is one condensed analysis.

The report is a markdown file written with `Write` directly into the project
root: `<project root>/<TICKET-ID>-impact-report.md`. The project root is the
folder the session was started in - check it with `pwd` and write to that
absolute path, never to the scratchpad, a temp folder or a subfolder. Do not
publish it as an artifact, a web page or HTML, and do not paste it into chat:
the team versions it and attaches it to the ticket, which only works for the
file. Give one line in chat with the absolute path when it is written.

Lead with the answer, carry the `member:line` evidence through from the tool
output, use tables for lists, and state what could not be established.

When the change is to be made in the source, the `alphaplus-implement` skill
takes the report from here: it locates each member on disk, edits and verifies
the lines, and writes the implementation report.
