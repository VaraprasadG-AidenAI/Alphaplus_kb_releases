"""
Rewrite SQLite SQL as psycopg SQL.

    python -m kb.store.sql --selftest --dsn "host=... dbname=..."

The knowledge base has ~132 SQL call sites written for sqlite3: `?` placeholders,
and a literal `%` in ten `LIKE '%WRITE%'` predicates.  Rewriting all 132 by hand
is 132 chances to get one wrong in a way no test covers.  One scanner, tested
against the real driver, is one chance.

## The rules, and why they are not symmetric

Determined by asking psycopg rather than by reading about it - see `CASES`, every
one of which `--selftest` executes against a live server:

  * **`%` is doubled EVERYWHERE** - in code, inside string literals, inside line
    and block comments.  psycopg's placeholder scanner respects none of them:
    `SELECT %s -- 50% done` raises "incomplete placeholder", and so does the same
    `%` inside `/* */` or inside `'...'`.

  * **`?` is converted ONLY outside string literals, quoted identifiers and
    comments.**  psycopg attaches no meaning to `?`, so converting one inside a
    literal would not be a no-op - it would turn the DATA `'why?'` into the
    placeholder `'why%s'` and silently consume an argument.

So `%` needs no context and `?` needs all of it.  That asymmetry is the reason
this is a scanner and not two `str.replace` calls.

## When NOT to translate

psycopg scans for placeholders only when parameters are passed, and `None` and
`()` are not the same thing:

    execute(sql, None)  ->  no scan;  a literal % is fine
    execute(sql, ())    ->  SCANS;    a literal % raises
    execute(sql, [])    ->  SCANS;    a literal % raises

That matters here specifically.  `kb.server.tools._rows(sql, *args)` calls
`execute(sql, args)`, and `args` is `()` whenever a query takes no arguments -
which is most of them, several carrying `LIKE '%WRITE%'`.  So `needs_params`
normalises an empty sequence to `None`, which is also the honest reading: a query
with no parameters has no placeholders to rewrite.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional, Sequence, Tuple

__all__ = ["to_pyformat", "needs_params", "CASES"]


def to_pyformat(sql: str) -> str:
    """SQLite `?` -> psycopg `%s`, and every literal `%` -> `%%`.

    Single pass.  The only state carried is "what am I inside", and it exists
    solely to decide whether a `?` is a placeholder or a character of data.
    """
    out: List[str] = []
    i, n = 0, len(sql)
    # at most one of these is true at a time; plain code when all are False
    in_string = False          # '...'   data
    in_ident = False           # "..."   identifier
    in_line_comment = False    # -- ... to end of line
    in_block_comment = False   # /* ... */

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        # ---- percent: unconditional, in every context ---------------------
        if ch == "%":
            out.append("%%")
            i += 1
            continue

        # ---- inside something: only the exit matters ----------------------
        if in_line_comment:
            out.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                out.append("*/")
                in_block_comment = False
                i += 2
                continue
            out.append(ch)
            i += 1
            continue

        if in_string:
            if ch == "'" and nxt == "'":       # an escaped quote, not the end
                out.append("''")
                i += 2
                continue
            out.append(ch)
            if ch == "'":
                in_string = False
            i += 1
            continue

        if in_ident:
            if ch == '"' and nxt == '"':
                out.append('""')
                i += 2
                continue
            out.append(ch)
            if ch == '"':
                in_ident = False
            i += 1
            continue

        # ---- plain code ---------------------------------------------------
        if ch == "?":
            out.append("%s")
            i += 1
            continue
        if ch == "'":
            in_string = True
        elif ch == '"':
            in_ident = True
        elif ch == "-" and nxt == "-":
            in_line_comment = True
            out.append("--")
            i += 2
            continue
        elif ch == "/" and nxt == "*":
            in_block_comment = True
            out.append("/*")
            i += 2
            continue
        out.append(ch)
        i += 1

    return "".join(out)


def needs_params(params):
    """`None` for anything empty, so psycopg does not scan for placeholders.

    See the module docstring: `()` is not `None` to psycopg, and the difference
    decides whether a literal `%` raises.
    """
    if params is None:
        return None
    try:
        if len(params) == 0:
            return None
    except TypeError:            # a mapping or an iterator; leave it alone
        return params
    return params


#: (label, sqlite sql, expected psycopg sql).  `--selftest --dsn` executes every
#: one against a real server, so these expectations are evidence, not opinion.
CASES: Tuple[Tuple[str, str, str], ...] = (
    ("bare placeholder",
     "SELECT ?", "SELECT %s"),
    ("two placeholders",
     "SELECT ?, ?", "SELECT %s, %s"),

    # the ten sites that made this module necessary
    ("LIKE with a literal percent",
     "SELECT 'abc' LIKE '%b%'", "SELECT 'abc' LIKE '%%b%%'"),
    ("_CAN_CHANGE, the real predicate",
     "SELECT 1 WHERE (mode IN ('O','U') OR verbs LIKE '%WRITE%' "
     "OR verbs LIKE '%DELETE%')",
     "SELECT 1 WHERE (mode IN ('O','U') OR verbs LIKE '%%WRITE%%' "
     "OR verbs LIKE '%%DELETE%%')"),
    ("placeholder and literal percent together",
     "SELECT ? WHERE v LIKE '%W%'", "SELECT %s WHERE v LIKE '%%W%%'"),

    # a ? that is DATA, not a placeholder
    ("question mark inside a string",
     "SELECT 'why?'", "SELECT 'why?'"),
    ("question mark in a string beside a real placeholder",
     "SELECT ?, 'why?'", "SELECT %s, 'why?'"),
    ("escaped quote, then a question mark",
     "SELECT 'it''s here?', ?", "SELECT 'it''s here?', %s"),

    # comments - psycopg scans these too
    ("percent in a line comment",
     "SELECT ? -- 50% done\n", "SELECT %s -- 50%% done\n"),
    ("question mark in a line comment",
     "SELECT ? -- really?\n", "SELECT %s -- really?\n"),
    ("percent in a block comment",
     "SELECT ? /* 50% done */", "SELECT %s /* 50%% done */"),
    ("block comment containing a star",
     "SELECT ? /* a * b */, 1", "SELECT %s /* a * b */, 1"),

    # quoted identifiers - kb/export.py uses AS "from" / AS "to"
    ("quoted identifier",
     'SELECT caller AS "from", callee AS "to" FROM call_edge WHERE caller=?',
     'SELECT caller AS "from", callee AS "to" FROM call_edge WHERE caller=%s'),
    ("question mark inside a quoted identifier",
     'SELECT x AS "odd?name" FROM t WHERE a=?',
     'SELECT x AS "odd?name" FROM t WHERE a=%s'),

    # %s that is data: psycopg scans string literals, so doubling keeps it data
    ("literal %s inside a string",
     "SELECT ?, 'a %s b'", "SELECT %s, 'a %%s b'"),

    # modulo needs doubling as well - psycopg refuses a lone %
    ("modulo",
     "SELECT 7 % 3, ?", "SELECT 7 %% 3, %s"),

    # the comma-list membership test from tools.py
    ("concatenated LIKE over a comma list",
     "SELECT 1 WHERE (','||paragraphs||',') LIKE ('%,'||?||',%')",
     "SELECT 1 WHERE (','||paragraphs||',') LIKE ('%%,'||%s||',%%')"),
)


def _placeholder_count(sql: str) -> int:
    """How many arguments the translated SQL will ask for.

    `%%` is a literal percent, so strip those before counting `%s` - otherwise
    `'a %%s b'` reads as a placeholder and the case asks for an argument too
    many.
    """
    return sql.replace("%%", "").count("%s")


def _selftest(dsn: Optional[str]) -> int:
    print()
    print("  kb.store.sql - placeholder translation")
    print("  " + "=" * 66)

    bad = 0
    for label, src, want in CASES:
        got = to_pyformat(src)
        ok = got == want
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
        if not ok:
            print(f"         in   {src!r}")
            print(f"         want {want!r}")
            print(f"         got  {got!r}")

    print()
    print("  an empty parameter sequence must become None")
    print("  " + "-" * 66)
    for label, value, want_none in (("None", None, True), ("()", (), True),
                                    ("[]", [], True), ("(1,)", (1,), False)):
        got = needs_params(value)
        ok = (got is None) == want_none
        bad += not ok
        shown = "None" if got is None else repr(got)
        print(f"  {'ok  ' if ok else 'FAIL'} {label:<8}-> {shown}")

    if not dsn:
        print()
        print("  No DSN given, so the driver half did not run - and it is the")
        print("  half that makes the expectations above evidence rather than")
        print("  a guess.  Pass --dsn to execute every case on a real server.")
        print()
        return 1 if bad else 0

    try:
        import psycopg
    except ImportError:
        print()
        print("  psycopg is not installed; driver half skipped")
        print()
        return 1 if bad else 0

    print()
    print("  every case executed against the server")
    print("  " + "-" * 66)
    conn = psycopg.connect(dsn)
    conn.autocommit = True
    for label, src, _ in CASES:
        sql = to_pyformat(src)
        n = _placeholder_count(sql)
        params = tuple(range(1, n + 1)) or None
        try:
            conn.execute(sql, params)
            print(f"  ok   {label}")
        except Exception as exc:                        # noqa: BLE001
            first = str(exc).splitlines()[0][:58]
            # a missing table is this repo's schema, not a translation fault
            if "does not exist" in first:
                print(f"  ok   {label}  (parsed; {first})")
            else:
                bad += 1
                print(f"  FAIL {label}")
                print(f"         sql {sql!r}")
                print(f"         {type(exc).__name__}: {first}")
    conn.close()

    print()
    print("  all cases pass" if not bad else f"  {bad} FAILURE(S) ABOVE")
    print()
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SQLite -> psycopg SQL")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dsn", default=None,
                    help="also execute every case against this server")
    ap.add_argument("--sql", default=None, help="translate one statement")
    a = ap.parse_args(argv)
    if a.sql:
        print(to_pyformat(a.sql))
        return 0
    if a.selftest:
        return _selftest(a.dsn)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
