# Correctness checks - run these before the report goes out

Four checks. Each one exists because a report that was **right about the
problem** shipped a change that was wrong, and nothing in the process caught
it. They are arithmetic and counting, not judgement, so they are mandatory
rather than advisory.

Each check that fires produces an **edit row marked "required for
correctness"**, not a paragraph in the risks section. A risk table is read
once; an edit table is worked through.

---

## 1. Capacity - does it fit?

**When:** every `STRING`, every `MOVE` of a group, every concatenation, and
every `MOVE` whose source and target are different widths.

**Why:** a target one byte too narrow truncates silently. Nothing fails, no
compiler warns, and the loss shows up as a customer complaint months later.

### How

The tools already carry every `PIC`. `describe_file` gives a file's fields
with type and length; `program_structure` gives a program's working storage.
So this is arithmetic:

```
  source fields           <width> + <width> + ... 
  + separators            one byte per literal between them
  ─────────────────────────────────────────────────────
  = bytes produced        <total>

  target field            <width>
  ─────────────────────────────────────────────────────
  shortfall               <total - width>      ← if positive, it truncates
```

Count the separators. A space, a comma, a slash between two concatenated
fields each costs a byte, and they are what turns "exactly fits" into "one
byte short".

**A field's byte length is not its digit count.** `PIC S9(7)` is seven bytes
zoned, but `PIC S9(7) COMP` is four bytes packed - and on this platform `COMP`
means packed. Take the length from the declaration and from
`references/cobol-equivalences.md` section 3, never from the digits in the
picture.

**Count every spelling.** A target assigned `SPACES` in one program and
`SPACE` in another is assigned in both. See `cobol-equivalences.md`.

### What to write

If the shortfall is positive, the edit set gains a row:

| Member | Line | Action | Why |
|---|---|---|---|
| `<MEMBER>` | `<n>` | widen `<TARGET>` to `<n>` | **required for correctness** - `<n>` bytes are produced, the target holds `<n>` |

And widening a field is itself a change with a blast radius: call
`where_is_field_used` on the widened field and put its width-sensitive sites
in scope. A widened field with no recompile set is a second defect.

### If the target cannot be widened

Sometimes it cannot - a record layout, a screen position, an interface. Then
the row says what is truncated and which end, and that becomes a question for
the ticket owner, in the open items, **named as a decision** rather than left
as a risk.

---

## 2. Single assignment - does the fix actually run?

**When:** any proposal that adds a statement assigning a target that is
already assigned in the same paragraph.

**Why:** two live statements assigning the same target, with no branch between
them, means the second wins. The first is dead. Nothing fails. This is how a
correct fix ships and does nothing.

### How

Before proposing, read the whole paragraph - `what_paragraph_does`, then the
source lines themselves. For the target you intend to assign:

```
  assignments to <TARGET> in this paragraph, before the edit:  <n>
  branches between them:                                       <yes/no>
```

`MOVE ... TO <TARGET>`, `COMPUTE <TARGET> = ...`, `ADD ... TO <TARGET>`,
`SUBTRACT`, `STRING ... INTO <TARGET>`, `UNSTRING ... INTO <TARGET>`,
`INITIALIZE` of a group containing it - all of these assign.

If the count is already 1 or more and there is no branch, the proposal is a
**supersession**, and `house-style.md` A.3 applies: it is a pair of edit rows,
add and switch off, both or neither.

### After the edit

Run the same count against the proposed text, and put the result in the
report as a line:

> After this edit, `<TARGET>` is assigned once in this paragraph.

If it is not once, the edit is not finished.

---

## 3. Siblings - what changes with it?

**When:** any change keyed on a flag, a code, or a field that sits in a group
with others like it.

**Why:** fields declared next to each other, with the same picture, read by
overlapping sets of programs, are usually alternatives in one decision. A rule
written for one of them is nearly always wanted for the others, and the next
ticket says so.

### How

```
  describe_file <the copybook or file that declares it>
```

A sibling is a field that is **all three** of:

- declared adjacent to it, in the same record or group
- the same picture
- read by an overlapping set of programs - compare `where_is_field_used` on
  each

### What to write

A sibling that the ticket does not name is **not** silently included. It is a
row in the report saying:

| Field | Relationship | Is it in scope? |
|---|---|---|
| `<FIELD>` | adjacent, same picture, `<n>` of `<n>` readers shared with `<FIELD>` | **not in the ticket - ask the reporter** |

Put it in the open items as a question, not in the risks as an observation.
A question gets answered; an observation gets read once.

---

## 4. Ownership - who already owns this line?

**When:** before proposing any change to a line that carries a mark.

**Why:** a line another ticket switched off, or added, is a line with a
decision behind it. Changing it without knowing that decision reverses
somebody's work, and the report that does it looks careless to the team that
receives it.

### How

```
python scripts/source_guard.py marks <path to the member>
```

For the lines being changed, read their marks. If a line carries a mark that
is not this ticket's:

1. Say so in the report: the line is owned by another change.
2. Say what that change did to it - switched it off, added it, modified it.
3. **Make the edit**, carrying both marks on the line so the history stays
   readable, and give the reversal its own row in section 8 (deviations):
   the earlier mark, what it did, and what this change does to it.
4. Put the question to whoever owns that change in section 9 **as well** - as
   a review item against a line that exists, not as a reason the line is
   missing.

A silent reversal is the expensive kind of wrong, because it is found in
production by the person whose fix disappeared. A **declared** reversal is not:
it is one row a reviewer reads and either accepts or strikes. Refusing to
touch the line is not the safe option it looks like - measured across eleven
tickets, the lines another change owned were exactly the lines the developers
edited, and refusing them cost more than any reversal did.

---

## The summary block

Every report carries this, filled in, near the verification section. A check
that was not run says `not run`, never blank:

| Check | Result |
|---|---|
| Capacity - every assignment sized | `<n>` checked, `<n>` short |
| Single assignment - no dead statements | `<n>` targets checked, `<n>` need a supersession pair |
| Siblings - adjacent fields considered | `<n>` found, `<n>` raised as questions |
| Ownership - marks read on every changed line | `<n>` lines, `<n>` owned by another change |

Zero in the right-hand column is an answer. A blank is not.
