# Source formats

How each member type is laid out, so an edit keeps every character in the
column the compiler reads it from.

Contents: finding the member type · encoding and line endings · change marks ·
COBOL · DDS (PF, LF, DSPF, PRTF) · CL · RPG IV · SQL · commands, menus,
messages · a column ruler

## Finding the member type

A member lives on the system at `LIBRARY/SOURCEFILE(MEMBER)`. A download
flattens that to `LIBRARY/SOURCEFILE/MEMBER.<extension>`, and the extension is
chosen by whoever wrote the download, not by the system.

| On disk | What it is | Edit? |
|---|---|---|
| `<LIB>/QCBLLESRC/<MEMBER>.CBLLE` | ILE COBOL source (SVN export naming) | yes |
| `<LIB>/QCBLLESRC/<MEMBER>.MBR` | the same member, in a download that names everything `.MBR` | yes |
| `<LIB>/QDDSSRC/<MEMBER>.PF` or `.LF` or `.MBR` | DDS source of a physical or logical file | yes |
| `<LIB>/<FILE>/<FILE>.MBR`, alone in its directory | the data member of physical file `<FILE>` | no - data, converted on the system |
| `EVFEVENT/...` | compiler event file | no |
| `_alphaplus-kb/...` | the knowledge base's own index and output | no |

Take the type from `find_object`, which reads the source-file directory and
the content, never from the extension alone. The source-file directory names
are IBM conventions, and a shop can add its own:

| Source file | Usual types |
|---|---|
| `QCBLLESRC`, `QCBLSRC` | `CBLLE`, `SQLCBLLE`, `CBL` |
| `QCPYSRC`, `QLBLSRC` | copybooks |
| `QDDSSRC` | `PF`, `LF`, `DSPF`, `PRTF` |
| `QCLSRC` | `CLLE`, `CLP` |
| `QRPGLESRC` | `RPGLE`, `SQLRPGLE` |
| `QSQLSRC` | `SQL` |
| `QCMDSRC` | `CMD` |
| `QMNUSRC` | `MNUDDS`, `MNUCMD`, `MNU` |
| `QMSGSRC` | message descriptions |
| `QSRVSRC` | binder source, `BND` |

When one relative path matches more than one file, or a name exists in more
than one library, the library in the impact report decides. If the report does
not settle it, ask.

## Encoding and line endings

Before the first edit, `python scripts/source_guard.py marks <file>` reports the
encoding (`ascii`, `utf-8`, `utf-8-sig`, `utf-16`, `cp1252`), the line endings
(`CRLF`, `LF`, mixed) and the longest line.

- Keep all three as they are. The guard's `check` fails a file whose encoding
  or line endings changed, because every line of the member would then differ
  and characters outside ASCII would be corrupted on upload.
- The system stores the member in an EBCDIC code page. Characters that code
  page cannot hold - typographic quotes, en and em dashes, ellipses, emoji - do
  not survive the upload. German letters in a member that already uses them are
  fine; new text is safest in plain ASCII.
- A source physical file has a fixed record length, commonly 92 bytes: 12 for
  the sequence number and date the system keeps, 80 for the text. A line longer
  than the text length is truncated on upload. Keep new lines within the
  member's longest existing line.
- Never tabs. The system does not expand them, so a tab shifts every later
  column.

## Change marks

Many members record changes in columns 73-80 of the changed line, and many
also keep a change-history block in their opening comment. Conventions differ
from shop to shop and sometimes from member to member, so read the member:

- `source_guard.py marks <file>` lists the values already in columns 73-80 with
  counts, and the comment lines that look like change history.
- Follow the member's own form: the same width, case and position of the mark;
  the same column layout of the history line (date format, ticket reference,
  initials, description).
- Mark each changed and added line. Where old lines are kept as comments, that
  is a convention too - follow it only where the member already does it.
- A member with no marks gets none unless the user asks. Inventing a
  convention in one member makes it differ from its neighbours.
- The ticket-specific values - the mark, the initials, the reference - come
  from the user or the ticket. Ask when neither settles them.

## COBOL (ILE COBOL, fixed format)

| Columns | Area | Holds |
|---|---|---|
| 1-6 | sequence | usually blank in a download; leave as found |
| 7 | indicator | `*` comment, `/` comment with page eject, `-` continuation, `D` debugging line |
| 8-11 | Area A | division, section and paragraph names; level numbers `01` and `77`; `FD`, `SD` |
| 12-72 | Area B | statements, clauses, level numbers `02`-`49`, `66`, `88` |
| 73-80 | identification | not compiled; change marks |

- **A continued literal** ends at column 72 without a closing quote; the next
  line has `-` in column 7 and restarts the literal with a quote in Area B.
- **Sentences end with a period.** Adding a statement inside an `IF` without
  `END-IF` can end the `IF` early if a period is moved or added. Read the whole
  conditional before and after the edit.
- **Level numbers and `PIC` clauses** are usually aligned in columns within a
  member. Keep the alignment of the neighbouring lines; it is not compiled,
  but a reviewer reads it.
- **`COPY ... PREFIX BY` and `COPY DDS-ALL-FORMATS`** import fields at compile
  time. Such a field is changed by editing the DDS or the copybook, not the
  program; the program only recompiles.
- **Embedded SQL** (`EXEC SQL ... END-EXEC`) follows the same columns; the SQL
  text stays within Area B.

## DDS - physical, logical, display and printer files

Every line has `A` in column 6. Positions are fixed:

| Columns | Holds |
|---|---|
| 1-5 | sequence, usually blank |
| 6 | form type `A` |
| 7 | `*` for a comment line |
| 8-16 | conditioning: 8 `A`/`O` for AND/OR, 9-16 up to three indicators such as `N03` |
| 17 | name type: `R` record format, `K` key, `S` select, `O` omit, `J` join; blank for a field |
| 18 | reserved |
| 19-28 | name, left-justified |
| 29 | `R` when the field is referenced from another file |
| 30-34 | length, right-justified |
| 35 | data type: `A` character, `P` packed, `S` zoned, `B` binary, `F` float, `L` date, `T` time, `Z` timestamp, `O` open, `H` hexadecimal |
| 36-37 | decimal positions, right-justified |
| 38 | usage: `B` both, `I` input, `O` output, `H` hidden, `M` message, `P` program-to-system |
| 39-41 | line (display and printer files) |
| 42-44 | position (display and printer files) |
| 45-80 | keywords; a `+` or `-` at the end continues onto the next line |

A field line from a display file, with the columns under it:

```
     A            PSTKEY        20A  B  5 18DSPATR(HI) CHECK(ME)
123456789012345678901234567890123456789012345678901234567890123456789012345678
         1         2         3         4         5         6         7
```

`PSTKEY` starts in 19, `20` ends in 34, `A` is 35, `B` is 38, line `5` ends in
41, position `18` ends in 44, the keywords start in 45.

**Physical files.** Changing a length changes the record format, and every
program that opens the file or copies its layout recompiles. The data is
converted on the system (`CHGPF`), which is not a source edit. A field defined
with `REFFLD` or `R` in column 29 takes its length from the referenced field:
change the referenced field, and every referencing file changes at its next
compile.

**Logical files.** A field listed without a length takes it from the physical
file and needs no edit, only a recompile. A length written on the logical, a
`SST` substring or a `CONCAT` carries its own width and positions: edit those.
Key and select/omit lines name fields and rarely change with a width.

**Display and printer files.** A field has a width and a place on the line.
Widening one can overlap the next field on the same line or run past the screen
width in `DSPSIZ` (80 or 132). Check every field on that line, and in a subfile
both the record and control formats. Indicators in keywords such as `CA03(03)`
link to `IN03` in the program by number only.

## CL (CLLE, CLP)

- Free-format, but held in 80-column records: keep lines within the member's
  longest line.
- A command continues onto the next line with `+` (leading blanks of the next
  line dropped) or `-` (kept) as the last non-blank character.
- Comments are `/* ... */`.
- `DCL VAR(&NAME) TYPE(*CHAR) LEN(n)` fixes a variable's length. A variable
  passed as a parameter to a program must match the receiving `LINKAGE` item
  byte for byte; nothing checks it at compile time.
- `%SST(&VAR start length)` and `CHGVAR` with literal lengths carry widths as
  numbers.

## RPG IV (RPGLE, SQLRPGLE)

A member starting `**FREE` is fully free-format. Otherwise specifications are
fixed and column 6 names the type (`H`, `F`, `D`, `I`, `C`, `O`, `P`); a free
block runs between `/FREE` and `/END-FREE` in older members.

| D specification columns | Holds |
|---|---|
| 7-21 | name |
| 22 | `E` externally described |
| 23 | data structure type |
| 24-25 | `DS`, `S`, `C`, `PR`, `PI` |
| 26-32 | from position |
| 33-39 | to position or length, right-justified |
| 40 | data type |
| 41-42 | decimal positions |
| 44-80 | keywords |

## SQL (QSQLSRC)

Free-format statements in 80-column records. Widths appear as `CHAR(n)`,
`VARCHAR(n)`, `DECIMAL(p, s)` and in `CAST`. A view or table created from this
source is recreated on the system; say so in the report.

## Commands, menus, messages

- **Commands (`CMD`):** `PARM ... LEN(n)` fixes the length a command passes to
  its processing program; it must match the program's parameter.
- **Menus (`MNUDDS`, `MNUCMD`):** DDS columns as above; an option runs the
  command in the matching command member.
- **Messages:** text sits in a message file on the system; in source it appears
  as `ADDMSGD` in CL or as `MSGID` keywords. Message text with substitution
  variables (`&1`) has fixed lengths defined with `FMT`.

## A column ruler

`python scripts/source_guard.py ruler <file> <line> [--count n]` prints lines
under a column ruler, ends each line with `|` so trailing spaces are visible,
and gives its length and change mark. It is the quickest way to place a length
in 30-34 or a mark in 73-80:

```
                 1         2         3         4         5         6         7         8
        12345678901234567890123456789012345678901234567890123456789012345678901234567890
    79 |     A            PSTKEY        20A  B  5 18DSPATR(HI) CHECK(ME)|   (64 chars)
```
