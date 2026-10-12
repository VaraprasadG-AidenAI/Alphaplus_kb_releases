# Change recipes

What each class of change must touch, how to find every site, and how to show
it was done. The impact analysis names the members; these recipes are the
line-level checklist for editing them. Placeholders in angle brackets stand
for the names in the ticket.

Contents: how to use a recipe · widen or retype a field · add a field ·
change program logic · change a CL program · change a screen or report ·
change a message · parameters between programs · after every recipe

## How to use a recipe

1. Pick the recipe for the change class the impact report states. A ticket can
   need two - a new field and the logic that fills it.
2. Run each search in the recipe over the files on disk with `Grep`, and each
   tool call over the index. The index holds the source as it was when it was
   built; the files hold it as it is now.
3. Put every hit in the plan as edit or no edit, with the reason. "No edit"
   with a reason is a result; a hit that is not in the plan is an open item.
4. Search the width-bearing syntax as well as the field name. Code can
   hard-code a width without naming the field.

## Widen or retype a field

The largest and most error-prone class. Work outward from the file.

| # | Site | Find it | Edit |
|---|---|---|---|
| 1 | the field in the physical file DDS | `describe_file(<file>)`, then the field line | length in 30-34, type in 35, decimals in 36-37 |
| 2 | fields defined by reference to it | `search_source("REFFLD\(<field>")` and `R` in column 29 over `QDDSSRC` | none when the length is blank - they follow at compile; an explicit length is an edit |
| 3 | related fields of the same meaning on other files | `describe_file` of each file in the report, and `where_is_field_used` for each related name | same width change where the ticket's intent covers them; otherwise list as not changed |
| 4 | logical files over the physical | `what_uses(<file>, kind='logicals')` | an explicit length, `SST` or `CONCAT` on the field |
| 5 | display and printer file fields | `where_is_field_used(<field>)`, then `explain_screen` on each device file | length, and position when the wider field no longer fits on its line |
| 6 | copybooks with a hand-written layout | `search_source("<field>\s+PIC", member_type=<copybook type>)` | the `PIC`; copybooks generated from DDS need no edit |
| 7 | program working storage and linkage | `search_source("<field>\s+PIC")` per program type, and each receiving field the value is moved into | the `PIC` of every item that holds the full value |
| 8 | reference modification `(start:length)` | `where_is_field_used` rows flagged `offset`; `search_source("<field>\s*\(\s*\d+\s*:")` | the length, and every later start position in the same record |
| 9 | records built by position from a buffer | `search_source("BUFFER\s*\(")` and reads of any `MOVE <area>(n:m)` | rebuild the offset table for every field after the widened one |
| 10 | embedded SQL | `search_source("<field>\s+(VAR)?CHAR")`, `CAST` | the SQL width, and the host variable `PIC` |
| 11 | CL variables | `search_source("DCL.*<name>", member_type='CLLE')`, `%SST` | `LEN(n)` and literal substring lengths |
| 12 | program parameters | see *Parameters between programs* | every caller and the called program together |
| 13 | validation and limits | literal lengths in `IF`, `INSPECT`, `STRING ... POINTER`, `UNSTRING`, loop bounds over the field | the literal |
| 14 | the data | - | not a source edit: `CHGPF` or a conversion program on the system, listed in the report |

**Verify.** `Grep` the edited files for the old width in every form -
`X(<old>)`, `CHAR(<old>)`, `LEN(<old>)`, `:<old>)`, the old length right-
justified in columns 30-34 on the field's DDS line. Each remaining hit is a
site the plan marks "no edit". Recompute every changed offset table by hand
in the report: field, old start, old length, new start, new length.

## Add a field

| # | Site | Edit |
|---|---|---|
| 1 | physical file DDS | the new field line. Position in the record matters to any program that reads the record by position, so add it where the ticket or the analysis says, usually at the end |
| 2 | logical files | only those that list fields explicitly and should expose the new one |
| 3 | display and printer files | the field with its line, position and usage; the heading or label beside it |
| 4 | programs that write the record | move a value into the field; an unset new field is spaces or zeros, which may be wrong for the business |
| 5 | programs that read it for display or export | the move out, and any fixed-width export record |
| 6 | SQL views | the column list when the view names columns |

Every program that opens the file recompiles, because the record format
changed. **Verify** that each writer sets the field, and that no program builds
the record by position without the new bytes.

## Change program logic

1. `program_structure(<program>)` for the paragraph graph, then
   `what_paragraph_does` for the paragraph the change belongs to.
2. `read_member` or `Read` the whole paragraph and every paragraph it performs
   that the change affects.
3. Make the smallest edit that implements the rule. Prefer an existing
   condition name (`88` level), error routine and message mechanism over a new
   one; the member's existing pattern is the one the next reader expects.
4. Check that each new data name is declared, each `PERFORM` target exists,
   each `IF` is closed where it was, and that a period has not moved the end of
   a conditional.
5. When the rule is enforced elsewhere as well - a second program writing the
   same file, an online and a batch path - `who_touches_file(<file>,
   mode='write')` finds them, and the ticket decides whether they change.

**Verify** by reading the changed paragraph from its first line to its last,
and every path into it that `program_structure` shows.

## Change a CL program

- **An override:** `resolve_overrides(<program>)` and `resolve_files(<file>)`
  show which files the change redirects. Keep the library expression as found
  unless the ticket is about the library.
- **A call or its parameters:** see *Parameters between programs*.
- **A library list or a job attribute:** read the whole CL; every command after
  the change runs in the new environment.

**Verify** continuation characters on each edited command, and that each
variable used is declared with the right type and length.

## Change a screen or report

- Record formats and fields: `explain_screen(<device file>)`.
- Function keys and indicators: the key in the device file (`CA03(03)`) and the
  indicator in the program (`IN03`, or an `INDIC` area) change together.
- A new or moved field: check the line against every other field and constant
  on it, and `DSPSIZ` for the screen width.
- A subfile: record format, control format, `SFLPAG`, `SFLSIZ` and the program
  loop that fills it.

**Verify** by listing, for each edited line of the device file, every field on
that line with its start and end column, in the report.

## Change a message

- Find how the program sends it: `search_source("<message id>")`.
- Message text on the system is changed with `CHGMSGD` or `ADDMSGD`; where the
  shop keeps that in CL source, edit the CL. Otherwise the change is a system
  action, listed in the report.
- Keep substitution variables (`&1`) and their `FMT` lengths consistent with
  the data the program passes.

## Parameters between programs

A length mismatch between a caller and a called program compiles cleanly and
corrupts every later parameter at run time.

1. `what_uses(<program>, kind='calls', limit=4000)` for every caller.
2. In the called program, `search_source("PROCEDURE DIVISION USING")` or
   `*ENTRY PLIST` / `PI`, then read the whole parameter list and its `LINKAGE`
   items.
3. In each caller: the `CALL ... USING` items or the CL `PARM` list, and the
   declarations behind them.
4. Change the called program and every caller in one plan. Write a table in the
   report: parameter number, called program's item and length, each caller's
   item and length, before and after.
5. A caller outside the extraction cannot be ruled out; say so.

## After every recipe

- The guard's `check` is clean, or each warning is explained.
- Every hit of every search has a state in the report.
- `unaccounted` with the report draft names no member without a place.
- The report's build sequence lists what must happen on the system, in order:
  physical file changes and conversions, logical files, device files, then
  programs in the order the recompile set requires.
