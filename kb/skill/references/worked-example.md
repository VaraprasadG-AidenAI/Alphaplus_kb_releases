# A worked analysis, condensed

An illustrative analysis on generic names. It shows the order of work, the
evidence each claim rests on, and the two decisions that most often go wrong.
Tool output is abbreviated.

## The ticket

> TICKET-1234 - The customer master needs e-invoicing data: a routing id, a
> party id and an invoice format must be maintainable and reach the customer
> API. A discount-group parameter is accepted but has no effect.

Business language; it names no member, file or field. Read: description, 6
comments, 1 screenshot. Comment 3 says the routing id must hold 100 characters,
not the 50 in the description; the analysis follows comment 3.

## Coverage

```
coverage_report
  LIBA   2,729 files   2,086 recognised   ok
  LIBB   4,200 files   2,743 recognised   ok
  LIBC       0 files       0 recognised   EMPTY - directory skeleton
```

LIBC goes in the report's coverage section before any count, and every total
below is a floor.

## Change class

```
Change class   add a field (invoice format), and widen one - ROUTEID A(50) -> A(100)
```

Two classes with different edit sets and one recompile set.

## Calls, and what mattered in each

| # | Call | What mattered |
|---|---|---|
| 1 | `trace_ticket "customer e-invoicing routing id"` | CUSTMAST, PGM100, PGM105, SCRN01FM |
| 2 | `find_object CUSTMAST` | PF in LIBA, the customer master |
| 3 | `where_is_field_used INVFLAG` | the nearest existing field; 2 programs write it |
| 4 | `where_is_field_used ROUTEID` | `NOT ONE WIDTH: declared as A(46), A(50)` - the screen and the file disagree |
| 5 | `resolve_files CUSTMAST` | one template, two data files, one ambiguous alias |
| 6 | `what_uses CUSTMAST kind='copybooks' limit=4000` | 126 copying members, by name |
| 7 | `search_source "PROCEDURE DIVISION USING"`, then `read_member PGM100 812 40` | 27 parameters, the discount group among them |

## Does the database change?

The invoice format is a code naming an e-invoice format. The nearest existing
field is `INVFLAG`:

```
where_is_field_used INVFLAG
  INVFLAG  A(1)  on CUSTMAST
  2 program(s) write it (3 occurrence(s)), 5 read it (9 occurrence(s)).
  WRITTEN BY:
  program  called      via file   evidence     confidence
  PGM200   AA-INVFLAG  CUSTMAST   PGM200:412   exact
  PGM205   AA-INVFLAG  CUSTMAST   PGM205:88    exact
```

`INVFLAG` is a yes/no flag with two writers, so it already carries a meaning
and cannot hold a format code:

> Requires a new database column: YES - `INVFMT A(1)` on `CUSTMAST`, taken from
> FILLER.

This decision drives everything after it. Mapping the invoice format onto
`INVFLAG` from the description alone would conclude that no DDS changes, and
the whole recompile set would disappear with it.

## Which physical files hold the data?

```
resolve_files CUSTMAST
  status          name        library  mechanism         evidence           from      overriding CLs
  TEMPLATE        CUSTMAST    LIBA     DDS source        -                  PF        -
  DATA-LIVE       F_CUSTMAST  -        $CRTF             STARTCL:85         CUSTMAST  41
  DATA-TRANSIENT  W_CUSTMAST  -        $CRTF then $DLTF  NIGHTCL:120        CUSTMAST  2
  OVERRIDE-ALIAS  CUSTREC     -        OVRDBF            41 OVRDBF site(s)  -         -

  ** AMBIGUOUS - one program-side name, several files **
  CUSTREC  -> 2 files: F_CUSTMAST, W_CUSTMAST
```

One DDS edit, one data-file conversion (the transient file is rebuilt each
run), and an alias that resolves to both - so a program declaring `CUSTREC`
works on different data depending on its caller. None of that is visible in
the COBOL.

## What recompiles?

`blast_radius CUSTMAST` shows 126 members copying the layout; `what_uses
CUSTMAST kind='copybooks' limit=4000` gives the 126 names, which go into §5
grouped by library and into Appendix A in full.

The manual analysis attached to the ticket lists 91. The 35 extra copy the
layout through `COPY DDR-<record> OF CUSTMAST`, which couples at compile time
and appears in no call graph. The report gives 126, gives both figures, and
states that reason.

## Two defects the ticket did not ask about

The discount-group parameter is item 19 in PGM100's `USING` clause.
`search_source "DISCGRP" member_type='CBLLE'` returns the callers and the
declaration and nothing in PGM100's procedure division: the caller passes it
and the program never reads it, which explains "accepted but has no effect". No
tool answers this directly; it took the search, the read, and the paragraphs.

`ROUTEID` is `A(50)` on the file and `A(46)` on SCRN01FM, so four characters
are lost at the screen. Both findings go in §6.

## Accounting

```
unaccounted(<report text>)
  RECONCILIATION - 214 member(s) returned by tools this session.
    placed in the text you supplied : 209
    NOT placed                      : 5
    from search_source  (5):
      CPY010  CPY011  PGM880  PGM881  SCRN04FM
```

`CPY010`, `CPY011` and `SCRN04FM` declare `ROUTEID` at a literal width and join
the edit set. `PGM880` and `PGM881` are excluded with their evidence:
`what_it_touches` shows neither copies `CUSTMAST`. A second `unaccounted` call
returns nothing unplaced, and the report is delivered.
