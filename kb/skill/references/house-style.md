# House style - how this shop writes a change

A report can be right about the problem and wrong about the shape of the fix.
That is the usual failure, and it is worse than being wrong, because a fix in
a foreign shape gets rewritten by the developer who receives it - so the
analysis was work nobody could use.

The team has a style. It is consistent and it is visible in the source. This
file is where it is written down, so a report proposes the shop's own
construct rather than re-deriving one from first principles.

Two parts. **Part A** is true of every IBM i shop and is filled in. **Part B**
is this engagement's own, and is filled in from the source by somebody who has
read it.

---

## Part A - always true

### A.1 The three change verbs

A line is never deleted. These are the only three things that happen to one:

| Verb | What it means | What the edit row says |
|---|---|---|
| **switch off** | put `*` in column 7. The text stays, readable, with its history | `switch off line <n>` |
| **add beneath** | a new line under the one it supersedes, with a new change mark | `add beneath line <n>` |
| **modify in place** | change a literal or an operand on the existing line | `modify line <n>` |

**Never write "delete" or "remove" about a source line.** They describe an
action this shop does not take, and a reader who follows them destroys the
history the next analyst needs. If a statement must stop running, it is
switched off.

Removing a line is only correct when the whole member is being retired, and
that is a separate decision with its own approval.

### A.2 Every proposed line carries its change mark

A line without a mark cannot be attributed later. Two marks exist, and they
are not the same thing:

| Mark | Where | What it says |
|---|---|---|
| **ticket mark** | the left margin, in the columns this shop uses | which ticket owns this line |
| **release mark** | the right margin, columns 73-80 | which delivery carries it |

Read the marks the member already uses before proposing one:

```
python scripts/source_guard.py marks <path to the member>
```

It prints every distinct mark, how many lines carry it, and where each first
appears - on code lines and comment lines separately. **Follow what is there.**
A member with its own convention is evidence; a convention from another member
is a guess.

A line that is switched off keeps its original mark and gains the new one, so
both tickets are attributable.

### A.3 Supersession is a pair, never a single edit

When a proposal replaces a statement that is still live, the edit set carries
**two rows that stand or fall together**:

```
  add beneath line <n>        the new statement, with the new mark
  switch off line <n>         the statement it replaces
```

Proposing only the first leaves both statements running. The second one wins,
the first is dead on arrival, and nothing fails - which is why this survives
review. It is the most common way a correct analysis ships a fix that does
nothing.

Every supersession pair carries a verification line naming the target and the
expected count:

> After this edit, `<TARGET>` is assigned once in this paragraph.

### A.4 Findings that imply an edit are edit rows, not prose

A risk table is read once. An edit table is worked through, line by line, by
somebody applying changes.

A finding that means *the change is wrong without it* belongs in the edit set,
marked **required for correctness** - not only in the risks section. Two
classes have shipped broken because they were prose:

- a target too narrow for what is moved into it (see `correctness-checks.md`)
- a sibling field that must change with the one the ticket names

If the analysis cannot say which line to change, the row still exists and
says so: `required for correctness - line not yet identified`.

### A.5 An exclusion quotes the member, not the search

A member is excluded from scope only on evidence read **inside** it. The
search hit that surfaced it is not evidence; it is the reason to open it.

Two rules:

1. Every exclusion quotes a line from inside the member, with its number.
2. Any member whose header comment block contains a term from the ticket is
   **read in full** before it can be excluded. A header that mentions the
   ticket's subject is the most likely place the answer is hiding, and it is
   exactly the member a sweep surfaces and a reader files away.

### A.6 Coverage is not reconciliation

`unaccounted` reconciles the members the **tools returned** against the ones
the report placed. A member no tool ever returned cannot fail it.

So a clean `unaccounted` means *"every member I saw has a place"*. It does
**not** mean *"nothing was missed"*, and a report must never say it does.
Completeness is answered by the acceptance-criteria ledger, which is keyed on
what the ticket asked for rather than on what the tools happened to return.

---

## Part B - this engagement's style

> **Unfilled.** Fill this in from the source, by reading members the team
> wrote recently. Until it is filled, a report must say in its open items:
> *"House style is not recorded; the shape of the proposed change follows
> general IBM i practice and may not match this team's."*
>
> Do not invent entries here. A guessed idiom is worse than none, because it
> reads as evidence.

### B.1 Mark format

| | Fill in |
|---|---|
| Ticket mark columns | `<columns>` |
| Ticket mark format | `<e.g. a letter then the ticket number>` |
| Release mark columns | `<columns>` |
| Release mark format | `<e.g. a number then a letter>` |
| How the next release mark is chosen | `<what the team does>` |

### B.2 Structural idioms

One block per idiom. An idiom is a shape the team reaches for repeatedly; a
report that needs that capability proposes the shape rather than inventing
one.

```
Name             <what the team calls it>
Purpose          <the problem it solves>
Signature        <the mechanical shape - what a search would match>
How to find it   <the tool call that returns every instance>
Seen in          <how many members, and how to list them>
Propose it when  <the condition that calls for it>
```

Candidates worth recording, in the order they usually matter:

| Kind | What it looks like |
|---|---|
| **pre-scan** | a section that reads a file through to the end *before* the main loop, so the loop has totals or order-level knowledge it could not otherwise have. The alternative - accumulating inline - is what an outsider proposes, and it is rewritten every time |
| **sign reversal** | a test on a document-type field that multiplies an amount by -1 |
| **config lookup then flag** | a call to a settings routine, its answer stored in a working flag, the flag tested later rather than the routine called again |
| **error path** | how the team reports a failure - a shared routine, a message file, a flag |
| **new field on an existing record** | where it goes, what it is initialised to, which programs must be touched |

### B.3 Worked example

One real change, before and after, showing the marks in their columns. This is
the single most useful thing in this file: a proposal is checked against it
rather than against a description.

```
BEFORE
<paste the lines as they were>

AFTER
<paste the lines as they are now, marks included>

WHY THIS SHAPE
<one paragraph: why the team did it this way and not another>
```

### B.4 Things the team never does

Written as prohibitions, because they are easier to check than preferences.

| Never | Why |
|---|---|
| `<fill in>` | `<fill in>` |

---

## How a report uses this file

1. Read it before proposing any line.
2. If Part B is unfilled, say so in the open items. Do not guess.
3. When a needed capability matches an idiom in B.2, **propose the idiom** -
   name it, say where it already appears, and follow its signature.
4. Check every proposed line against B.3 before it goes in the report.
5. Use A.1's verbs. Never "delete".
