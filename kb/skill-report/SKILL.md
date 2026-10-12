---
name: alphaplus-implement-report
description: Turns an impact analysis that already exists into an implementation report for an Alphaplus IBM i (AS/400) ticket - the exact code change, one row per line, naming the library, source file, member, line number, the text before and the text after. Reports the change without editing the source unless the edit guard is present. Carries the confidence tiers from the impact analysis and leaves every accuracy figure blank, because there is no ground truth to measure against. Use when an impact report exists and somebody wants the implementation report, the exact lines to change, a change register, or the same report produced again for a batch of already-analysed tickets.
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

# Implementation report, from an impact analysis

The impact analysis says **what** a ticket affects. This produces **the exact
code change**: for every member it named as changing, which line, what it says
now, what it should say, and why that line is valid.

It runs in the session that already holds the impact report, or in a new one
pointed at the report file. It does not repeat the impact analysis.

## Which skill is this

| You want | Skill |
|---|---|
| What does this ticket affect? | `alphaplus-impact` |
| Change the source, verify it, commit it | `alphaplus-implement` |
| The implementation report, from an analysis that already exists | this one |

This one does **not** edit source unless the edit guard is present. Its
deliverable is the report. If the guard is available and the user wants the
change applied, use `alphaplus-implement` instead - it does the same work and
carries the snapshot, commit and re-index that this one cannot.

## Before anything

Find the impact analysis. In this session, scroll back to it. In a new
session, read `<project root>/<TICKET-ID>-impact-report.md`.

**No impact analysis, no implementation report.** Say so and stop. Running
`alphaplus-impact` first is the answer, not guessing at a change set.

Then check the tools answer: `coverage_report`. If it fails, the session
cannot reach the knowledge base and nothing below will work.

## Decide the mode, and say which in the header

| If | Mode | Header says |
|---|---|---|
| `scripts/source_guard.py` is reachable and the user asked for the change to be made | Applied | `Applied` |
| Anything else | Proposed | `Proposed` |

In `Proposed` mode, **edit nothing**. Every line is still given in full, ready
to apply. The report is identical apart from the header word and section 5.

## Workflow

Copy this into the conversation and tick it.

```
Implementation report - <TICKET-ID>
- [ ] 1. Analysis read       the changing members listed, apart from recompile-only
- [ ] 2. Members confirmed   each one found on disk and read
- [ ] 3. Lines located       every line found by content, never by a line number
- [ ] 3b. Checked            capacity, single assignment, siblings, ownership
- [ ] 4. Lines written       every proposed line checked against its column rules
- [ ] 5. Reported            Implementation/<TICKET-ID>.md written
- [ ] 6. Metrics             counts and tiers filled, accuracy left blank
```

### 1. Read the analysis

List the members it says **change**, separately from the ones it says only
**recompile**. Only the changing members get a code change. The recompile set
is carried into section 6 of the report unchanged - do not recount it.

Note each changing member's confidence tier (Confirmed, Probable, Possible).
Section 10.2 needs them.

### 2. Confirm each member, and read it

| Need | Tool |
|---|---|
| the path and the library | `find_object` |
| a file's fields, types, widths | `describe_file` |
| a program's shape and ranges | `program_structure` |
| the lines themselves | `read_member`, or `Read` |

A member the analysis named that cannot be found now is a finding for section
8, not a silent drop.

Read a member narrowly. Take the range `program_structure` or a search gave
you; members run to thousands of lines.

### 3. Locate every line by content

**Never trust the line number in the impact report.** Line numbers drift
between an analysis and an implementation.

Search for the thing itself - the declaration, the `MOVE`, the `PIC`, the DDS
field name - with `search_source` or `Grep` over the member. Record the line
number you actually found, and the text you searched for. That text is the
anchor in the report, so a reviewer can find the line again.

`where_is_field_used` gives the sites across members. Run `unaccounted` at the
end: any member this session returned that has no place in the report is a gap
in the report, not a member that does not matter.

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

### 4. Write each replacement line in full

Columns are the whole job on this platform. `references/source-formats.md` has
the rules; the ones that catch people:

| Kind | Must hold |
|---|---|
| COBOL | indicator in column 7; nothing past column 72; change mark in 73-80 |
| DDS | `A` in column 6; name 19-28; length right-justified 30-34; type 35; decimals 36-37; usage 38; line/pos 39-44 |
| CL | continuation `+` at the end of the line, not a wrapped line |
| Copybook | the rules of the COBOL that includes it |

A line that breaks a column rule is not a proposal. Where the guard is
available, check each one before showing it:

```
python scripts/source_guard.py line --kind dds "     A            FLDNAME        5A"
python scripts/source_guard.py line --kind cobol --mark "<TICKET-ID>" "           MOVE ..."
```

### 5. Write the report

`<project root>/Implementation/<TICKET-ID>.md`, following
`references/report-layout.md` exactly. `<project root>` is the folder the
impact report is in.

In `Applied` mode only, snapshot first and verify afterwards:

```
python scripts/source_guard.py snapshot --work "<project root>/Implementation/<TICKET-ID>" --root "<source tree>" --mark "<TICKET-ID>" <file> [<file> ...]
   ... make the edits ...
python scripts/source_guard.py verify --work "<project root>/Implementation/<TICKET-ID>" --root "<source tree>"
```

`verify` prints the change register. Paste it into section 2 rather than
typing one.

### 6. Metrics

`references/metrics.md`. Counts and confidence tiers are filled in; every
accuracy figure is left **blank**, because there is no ground truth in this
engagement to measure against. Read that file before writing section 10 - the
blank cells are the point of it, and an estimate in one of them is worse than
an empty report.

## Rules

- **Facts come from executable source.** Not from a header comment block, not
  from a name. A member sounding like a batch program is a naming habit.
- **Say what you do not know.** Every fact carries `exact`, `inferred` or
  `unresolved`, and evidence as `member:line`. An unresolvable `CALL` through
  a variable, or an `OVRDBF` with a variable library, is recorded as
  unresolved, never dropped.
- **Words that mean the same thing are the same thing.** `SPACE` and `SPACES`;
  `ZERO`, `ZEROS` and `ZEROES`; `PIC` and `PICTURE`; `THRU` and `THROUGH`;
  `CORR` and `CORRESPONDING`; `COMP` and `PACKED-DECIMAL`. Every sweep searches
  every form, and the report says which forms were searched next to the count.
  A nil result from a single-form search is a search that was not finished, not
  a finding. `references/cobol-equivalences.md` has the list.
- **A line is never deleted.** Switched off, added beneath, or modified in
  place. "Delete" and "remove" describe nothing this shop does to a line.
- **A finding that makes the change wrong without it is an edit row**, marked
  `required for correctness` - never only prose.
- **Every changed line is reported.** Library, source file, member, line
  number, before, after - one row per line. Never a summary, never "and
  similar changes elsewhere".
- **No count without the names behind it.** "and 43 others" is a wrong report,
  not a short one.
- **One member in two libraries is two members.** Editing one and compiling
  the other is a mismatch no compiler catches.
- **Source libraries are not the object library.** The library in the register
  is the one the member sits in on disk.
- **No percentage that is not a count over a count.** Section 10.3 stays
  blank; nothing elsewhere fills it in by implication.
- **A compile has not run.** Say so in section 9, every time.
- **The report is a file.** Do not publish it as an artifact, a web page or
  HTML, and do not paste it into chat. One line in chat when it is done: the
  path, members and lines changed, the mode, and the verification result.

## Reference files

| File | Read it when |
|---|---|
| `references/report-layout.md` | writing the report - it is the layout, section by section |
| `references/metrics.md` | writing section 10 |
| `references/source-formats.md` | writing any replacement line |
| `references/house-style.md` | before proposing any line - the shop's verbs, marks and idioms |
| `references/correctness-checks.md` | step 3b, and before the report goes out |
