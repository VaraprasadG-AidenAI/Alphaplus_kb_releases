# Working notes and ticket memory

An analysis of this codebase runs to dozens of tool calls, and the context
window is compacted or cleared long before a large ticket is finished. What
was established before that point survives only if it was written down. Two
stores do that job, and they answer different questions:

| Store | Scope | Holds | Written with |
|---|---|---|---|
| Notes file | one ticket, this and later sessions | everything needed to continue the work and write the report | `Read` / `Edit` / `Write` |
| Ticket memory | every ticket, every session | durable facts another ticket could rely on or contradict | `remember`, read with `recall` |

Contents: the notes file · entry reference · keeping it small · resuming ·
ticket memory · a worked sequence

## The notes file

`<project root>/.alphaplus-work/<TICKET-ID>.notes.md`

The project root is the session's starting working directory, resolved with
`pwd` - the same folder the report is written to (see
`report-template.md`). Create `.alphaplus-work/` there if it does not exist.

One file per ticket, shared by the impact analysis and the implementation of
the same ticket, so the implementation continues from where the analysis
stopped. The file is for Claude, not for people: it is dense, tagged and
never polished, and the report is written from it.

Create it at the start of the work with this skeleton:

```xml
<notes ticket="TICKET-ID" format="alphaplus-notes/1">
<criteria>
One line per acceptance criterion, in the ticket's own numbering, each marked
open | mapped | no-code-found. Written before the first tool call and updated
as code is found; the report's section 1.1 is built from this.
</criteria>
<brief>
The ticket in three to six lines: what is asked, target values quoted from the
ticket, who asked, which comments settled what.
</brief>
<end/>
</notes>
```

Append every entry by replacing `<end/>` with the entry followed by `<end/>`,
using `Edit`. That writes only the new entry; rewriting the whole file costs
tokens in proportion to its size and can lose an entry if two edits cross.

## Entry reference

One `<e>` per step that changed what is known. `n` counts up from 1; `step`
is the workflow step of the skill in use (`impact:5`, `implement:4`).

```xml
<e n="12" step="impact:5">
<did>where_is_field_used(MAILADR)</did>
<fact c="exact" ev="PGM1:212">MAILADR is AA-MAILADR in PGM1@LIB1 via COPY DDR-RORDFILE PREFIX BY "AA-"</fact>
<fact c="inferred" ev="CL4:31">F_ORDFILE@&amp;DTALIB opened by PGM1 through OVRDBF ORDFILE</fact>
<set name="edit" op="add">PGM1@LIB1 PGM2@LIB1</set>
<set name="recompile" op="call">what_uses(ORDFILE, kind='copybooks', limit=4000) = 381</set>
<decide why="LIKE follows the file at recompile">PGM7@LIB2 recompile, not edit</decide>
<ask id="q3">Is the 100-character width also required in the archive file ARCFILE?</ask>
<todo>resolve_files(F_ORDFILE); search_source("X\(50\)", member_type='CBLLE')</todo>
</e>
```

| Tag | Use | Attributes |
|---|---|---|
| `<did>` | the tool call or file read this entry records, verbatim | - |
| `<fact>` | one established fact, self-contained | `c` = exact, inferred or unresolved; `ev` = member:line or the call |
| `<set>` | a change to a working set: `edit`, `recompile`, `convert`, `alias`, `nochange` | `op` = add, remove, or call (the set is too large to list: give the call that reproduces it and its count) |
| `<decide>` | a choice between readings, with the reason | `why` |
| `<ac>` | one acceptance criterion and what satisfies it | `id` = the ticket's numbering; `state` = open, mapped or no-code-found; `ev` = member:line |
| `<ask>` | an open question for the ticket owner | `id` |
| `<answer>` | the answer to an earlier `<ask>` | `id`, `ev` |
| `<recall>` | what `recall` returned that bears on this ticket | `subject` |
| `<todo>` | the calls still owed from this step | - |
| `<change>` | implementation only: one edited site | `file`, `line`, `mark` |

Names carry their library as `NAME@LIB`. A name without a library is a claim
about every library, which is rarely what was meant.

At the end of every workflow step, and before any sweep that will take many
calls, write a checkpoint. It is the state to resume from:

```xml
<checkpoint n="18" step="impact:6">
<sets>edit 23 (e4 e9 e12 e15) | recompile 381 = what_uses(ORDFILE, kind='copybooks', limit=4000) | convert F_ORDFILE@LIB1 ARCFILE@LIB1</sets>
<open>q3 q5</open>
<unplaced>PGM9@LIB3 PGM10@LIB3 - returned by who_touches_file, not yet in a set</unplaced>
<next>step 7: parameter lists of PGM1 and its callers</next>
</checkpoint>
```

## Keeping it small

The notes file exists to save context, so it must stay far smaller than the
work it records.

- Record conclusions, not transcripts. Never paste tool output longer than a
  few lines; write what it established and the call that reproduces it.
- A list of more than about twenty names goes in as `op="call"` with the
  reproducing call and the count. The report re-runs the call.
- Evidence is a reference (`PGM1:212`), never the source line.
- One fact per `<fact>`. A paragraph of facts cannot be checked off later.
- Correct by appending: a new `<fact>` or `<decide>` that names the entry it
  replaces (`replaces="e12"`). Never edit an earlier entry; the history of a
  wrong turn is what stops it being taken twice.

## Resuming

After a compaction, a cleared context or a new session, before any tool call:

1. `Read` the whole notes file.
2. Take the last `<checkpoint>` as the state, then apply every entry after it.
3. Continue from its `<next>` and the `<todo>` lines after it.

Do not re-run a call whose result is already recorded unless the entry says
the result was incomplete (`PAGE 1 OF 4`, a cut, a hard stop).

## Ticket memory

`recall` and `remember` reach a store kept outside every source tree, which
survives re-indexing and switching releases. What it holds outlives this
ticket, so it takes only facts another ticket could use or contradict.

**At the start of a ticket**, once the ticket's subjects are known - the
members, fields and files it names or `trace_ticket` returns - call
`recall(subject=<name>)` for each, and `recall(query=<key terms>)` once.
Write what bears on this ticket as `<recall>` entries. A fact marked as
recorded against another source tree or an older index build is a lead to
recheck, not a fact.

**When `remember` or `recall` shows another ticket disagreeing** with this
analysis - `OTHER TICKETS ON ...` or `DIFFERS ACROSS TICKETS` - decide which
is right from the tools and the source, record the decision in the notes, and
report the disagreement in the report's risks section. Two tickets changing
the same field in different directions is a delivery risk the owners need to
see.

**At the end of the ticket**, once the report is drafted and before it is
delivered, `remember` the durable facts, one call each. A disagreement that
surfaces only now goes into the report before it is delivered.

| kind | Remember | Do not remember |
|---|---|---|
| `field` | a width, type or meaning the ticket established or changed | a width merely read from DDS - the index has it |
| `edit`, `convert` | each member edited and file converted, with the reason | the recompile set - it is derived and rebuilt by `what_uses` |
| `decision` | a scoping decision and its reason | routine steps |
| `finding` | a defect, risk or inconsistency in the code | tool limitations |
| `question` | a question and its answer, once answered | an unanswered question - it stays in the report |
| `change` | implementation only: each change made, with file and mark | - |

Each statement must stand on its own, without the notes file: subject,
library, what is true, and the ticket's evidence. `supersedes` corrects this
ticket's own earlier fact; another ticket's fact is never altered.

## A worked sequence

```
recall(subject="MAILADR")                 -> <recall subject="MAILADR"> T-812 widened MAILADR@LIB1 to 100 </recall>
where_is_field_used(MAILADR)              -> <e n="5"> facts, <set name="edit" op="add"> ...
... steps 6-8, a <checkpoint> after each ...
report drafted                            -> <e n="31" step="impact:13"><did>report drafted</did></e>
remember(ticket, "field", "MAILADR", "widened 50 -> 254 on ORDFILE@LIB1 and ARCFILE@LIB1", evidence="ORDFILE:14", library="LIB1")
                                          -> OTHER TICKETS ON MAILADR: T-812 says 100 -> added to risks, then delivered
```
