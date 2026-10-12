# Tool reference

What each alphaplus tool returns, and where its answer misleads. This list
matches the tools the server registers. A name that is not here is not a tool:
a call to it fails, and a plan that relied on it quietly loses a section, so
check here before writing a call into a plan, and compare against the
session's live tool list when in doubt.

Contents: impact and reach · files and data · programs · finding things ·
ticket memory · reading tool output · couplings the tools follow · counts that
are not what they look like

## Impact and reach

| Tool | Returns | How it misleads |
|---|---|---|
| `blast_radius(name, depth=3)` | counts: callers, callees, members copying the layout, logical files, writers, readers, existence checks, overrides onto it - then the calls that turn each count into names. For a program, also `COPIES` (its copybooks and how many others copy each) and `LINKAGE CLOSURE` (callers and copiers bound to its parameters) | Counts only. It opens an analysis; the names come from the calls it lists. The copy count is the recompile count, and it is the coupling a call graph does not show. On a program, `recompile 0` is about the program's name; its copybooks' copiers are in `COPIES` |
| `what_uses(name, kind='all', limit=40)` | everything reaching an object of any kind; `kind` is one of `all`, `calls`, `copybooks`, `files`, `overrides`, `screens`, `messages`, `dataareas`, `logicals`, `fields`, `defines` | The recompile-set tool (`kind='copybooks'`). Above `limit` it folds to a per-library summary, so pass `limit=4000` when you need names. It answers for the whole layout family and reports copies through intermediate copybooks |
| `what_it_touches(name, kind='all', limit=40)` | everything one member reaches; `kind='unresolved'` lists each statement the parser could not turn into a fact, with its line and why | The outward direction. Files marked `checks only` are named but never opened. The `all` view counts gaps by kind only; the lines are under `unresolved` |
| `path_between(frm, to, edges='calls', max_hops, max_paths)` | every shortest route, each hop labelled | `edges='calls'` follows `CALL` only; a member reaches a file by opening it, so a data question needs `edges='all'`. No route means none in the source - a scheduler or an operator running both is invisible |
| `who_calls(name, depth, page)`, `what_it_calls(name, depth, page)` | call edges; `who_calls` ends with `ROOTS`, `STARTED FROM A MENU` and `NOT A ROOT YET`, and `HEADER SAYS INACTIVE` when the member's header calls it retired | One hop unless `depth` is raised. A root is started from outside the source or is dead - the tool cannot say which. `what_it_calls` reports calls through a variable with their possible targets |
| `menu_path_to(name, depth=4)` | the menus, option numbers and labels that reach a program | A menu option is how a program is meant to be reached; a job or another program may also call it |

There is no single recompile-set tool. Build the set from one `what_uses(...,
kind='copybooks', limit=4000)` per changed object, and take the union.

## Files and data

| Tool | Returns | How it misleads |
|---|---|---|
| `who_touches_file(file, mode='any', page)` | programs that can change the file, read it or check that it exists, including through an `OVRDBF` rename | Folds in logical files, so its counts exceed `describe_file`'s by design. When no program opens the file it says how many members copy its layout - compile-time coupling it does not list |
| `describe_file(file, library)` | a file's fields with each one's byte range in its record, keys, logical files, and writer and reader counts | Does not fold in logical files. When there is no DDS member of that name, its answer points to `resolve_files`, since the name may be a program-side name or a data file |
| `resolve_files(name)` | every physical file a name touches, each with status, library, mechanism, evidence, template and overriding-CL count, then totals by status | The `** AMBIGUOUS **` block is the most important part and the easiest to skim past. Accepts a template, a data file or a program-side name |
| `resolve_overrides(program)` | the files a program actually opens once its callers' `OVRDBF` statements apply, with `SHARE` and `LVLCHK`; a `RISK:` line for every `LVLCHK(*NO)` | The library usually comes from a variable, so most rows are inferred - Probable, not Confirmed |
| `where_is_field_used(field, direction='any', page)` | the field's declarations, each program's local name (`called`), who writes and reads it, width-sensitive sites, and `REACHED BY BYTE POSITION` - statements that read or write it by offset without naming it | Reports `NOT ONE WIDTH` when declarations disagree, and a `RECALL CEILING` when some layouts did not expand - usage in those programs is unknown, not none. Programs listed under `WRITTEN BY` mean the field is in use |
| `what_uses(file, kind='screens')` on a display or printer file | every program tied to it, marked declares (ASSIGN), copies formats, or format writes | A zero is `NO PROGRAM USES` with what was checked, or `NOT PROVEN` with the lines that were not read as a link - read those before trusting it |
| `explain_screen(device_file)` | record formats and fields of a display or printer file | A device file whose formats could not be read - generated metadata, for example - is reported as unreadable, not as a screen with no fields |

## Programs

| Tool | Returns | How it misleads |
|---|---|---|
| `program_structure(name, member_type, max_rows=220)` | the paragraph graph: order, loops, and what each paragraph reads, writes, calls and displays; paragraphs nothing performs; `PERFORM` targets never declared | COBOL only. It does not return the parameter list |
| `what_paragraph_does(program, paragraph, member_type)` | one paragraph in detail | |
| `find_paragraph(name, touches, calls, writes, limit, page)` | paragraphs by name, or by what they touch, call or write | At least one argument is required; walk every page |
| `read_member(name, start, count, library, member_type)` | source lines, at most 400 per call | Read the range another tool gave you; members run to thousands of lines |
| `read_summary(name)` | a written description | Empty unless descriptions were generated for this index |
| `program_interface(program)` | the `USING` parameters with sizes in bytes and the copybook each layout comes from; every CL `CALL` with argument sizes; `OVERRUN`, `MISSING`, `LONGER` and `EXTRA` mismatches | Checks CL callers only - COBOL callers are listed under `NOT SIZE-CHECKED`. A size of `?` is unknown, and nothing is compared against it |

Whether a parameter is used is something no tool answers: read the paragraphs
that reference it, or say you could not establish it.

## Finding things

| Tool | Returns | How it misleads |
|---|---|---|
| `trace_ticket(text, limit=12)` | candidate members ranked from facts, with the reason for each | Leads, not answers - at best Possible until another tool confirms them. `NOT IN THE INDEX:` comes first and names every identifier-like word that matched nothing; resolve those before trusting the ranking |
| `find_object(name, library)` | what a name is and which library holds it | Distinguishes a source member, a program-side name that an `OVRDBF` redirects (with its targets), a name that is not in the index, and near-miss names. None of these means "does not exist" |
| `route_ticket(text, limit)` | which menu or area to start in | Reads descriptions a model wrote; empty when none were written |
| `find_similar(name, limit)` | descriptions that read like this one's | The same limit; takes a menu, area or library |
| `search_source(pattern, member_type, limit=40, page, per_member=12)` | literal or regex matches in source text; `member_type` is one type, a list (`'CBLLE,CPY'`) or `'CODE'` for every code type | Counts every match but shows at most `per_member` lines per member, and names members whose lines were withheld. A scoped search ends with `NOT SEARCHED:` - the types it did not read are unknown, not unmatched. A field reached through `COPY` is not in the program's text at all. `limit` is at most 400; page beyond it |
| `coverage_report()` | per-library coverage, fact confidence, what could not be worked out, shallowly parsed types, permanent blind spots | Call it first. It is the only place that separates "this library has no bearing" from "this library was never read" |
| `unaccounted(text)` | every member the tools returned this session that the given text never mentions, grouped by tool | The last call before delivering a report. Each name needs a state - change set, recompile set, or stated exclusion with its evidence. An exclusion line with no `member:line` and no tool call is listed as `EXCLUDED WITHOUT EVIDENCE` and does not count as placed |

## Ticket memory

Facts carried from one ticket's analysis to the next, kept outside every
source tree. When and what to record is in `references/working-notes.md`.

| Tool | Returns | How it misleads |
|---|---|---|
| `remember(ticket, kind, subject, statement, evidence, library, confidence, supersedes)` | the stored fact's id, or the existing id when the same fact is already stored; then `OTHER TICKETS ON <subject>` - what other tickets recorded about it | `kind` is one of `edit`, `recompile`, `convert`, `field`, `decision`, `finding`, `question`, `change`. `supersedes` corrects only this ticket's own fact. A stored fact is only as good as its evidence |
| `recall(query, subject, ticket, kind, limit=30)` | stored facts by exact subject, free text or ticket, grouped by subject; `DIFFERS ACROSS TICKETS` where tickets disagree | Memory is what analyses concluded, not the source. A fact marked as recorded against another source tree or an older index build is a lead to recheck. An empty answer means no ticket recorded it, not that nothing is there |

## Reading tool output

| You see | It means | Do |
|---|---|---|
| `Raise limit past N` | the list folded; there are more names | call again with `limit` above N |
| `PAGE 1 OF 4` | more rows on later pages | walk every page |
| `[CUT AT n OF m CHARACTERS]` | the answer exceeded the response size | narrow the question and merge the parts |
| `PGM600 +7` in a `search_source` header | seven more matching lines in that member, not shown | raise `per_member` if one could be a width or an offset |
| `*** HARD STOP ***` | the search stopped before reading every member | split it by `member_type`; report any count from it as a floor |
| a `[ledger]` note on a result | the session's reconciliation ledger has started | call `unaccounted` before delivering |
| an argument error | nothing ran | change the arguments; the message lists what the tool accepts |

## Couplings the tools follow and a text search does not

| Coupling | What goes wrong without it |
|---|---|
| `COPY DDS-ALL-FORMATS OF x` | the field list is not in the program's text, yet the program uses the fields and recompiles when the DDS changes |
| Logical files | a program reading `CUSTMAST` through `CUSTMASTL3` never names `CUSTMAST`; `where_is_field_used` follows it |
| `OVRDBF` renames | with `OVRDBF FILE(WRKF02) TOFILE(&DTALIB/WRKF09)` the COBOL says `WRKF02` and the job opens `WRKF09`; `who_touches_file` follows it in both directions |
| Indicators | `CA03` in the display file and `IN03` in the COBOL are linked only by the number |

## Counts that are not what they look like

`COPY DDR-<record> OF <file>` and `LIKE <item> OF <file>` couple at compile
time and are recorded against the file, so `blast_radius` and
`what_uses(kind='copybooks')` count them. They appear in no call graph and no
I/O list, which is the usual reason a tool total exceeds a hand-built one.
Report the larger number and give that reason.

`CHKOBJ` is not a read. It checks that an object exists and opens nothing, so
it appears as `checks only`, apart from readers. The dependency is real - the
job fails without the file - but no data is read.
