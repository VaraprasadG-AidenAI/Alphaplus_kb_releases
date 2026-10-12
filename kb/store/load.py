"""
Copy the facts from SQLite into PostgreSQL.

    python -m kb.store.load --dsn "host=... dbname=..." [--schema kb]
    python -m kb.store.load --dsn "..." --verify        (counts only, no write)

A migration step, not a permanent part of the build.  Once `kb.index.build`
writes to PostgreSQL directly this module's job is to prove the two stores agree,
and after SQLite is removed it goes with it.

## COPY, not executemany

`kb.index.build` issues 24 `executemany` calls per member - about 294,000 round
trips for a full build.  That is fine against a file on the same disk and a cliff
over a socket.  `COPY ... FROM STDIN` streams instead, and
docs/NEO4J-LIMITS-REPORT.md already measured it on this data: 1.6M rows in 9.8 s.

Rows are streamed straight off a SQLite cursor, which is lazy, so no table is
ever materialised as a Python list.  That matters most for `field_usage`, and
kb/export.py already relies on the same property for the same reason.

## The two constraints SQLite did not enforce

A full load is the first time anything checks these, so they are checked BEFORE a
single row is written rather than discovered 900,000 rows into a COPY that then
has to be rolled back:

  * **NULL in a primary key column.**  A non-INTEGER primary key in SQLite is
    only a UNIQUE index and permits NULL, so `library` - which defaults to `''`
    but is nullable - can hold NULL in four of the six composite keys.
    PostgreSQL makes primary key columns NOT NULL.
  * **Duplicate primary key tuples.**  Same reason: SQLite's UNIQUE index treats
    two NULLs as distinct, so rows that PostgreSQL will reject can already be
    sitting in the file.

Neither exists in the replica.  Both are plausible on a 21-library extract, which
is exactly when a clear refusal beats a stack trace.

## What is NOT copied

`search` and `search_summary` carry a generated `tsvector` column, so only their
text columns are copied and PostgreSQL computes `tsv` itself.  The ranking that
column produces is NOT equivalent to FTS5's - see kb/store/ddl.py.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import ddl

#: Text columns of the two former FTS5 tables.  `tsv` is generated.
FTS_COLUMNS: Dict[str, Sequence[str]] = {
    "search": ("name", "kind", "body"),
    "search_summary": ("scope", "key", "body"),
}


def _schema_sql() -> str:
    return ddl.schema_path().read_text(encoding="utf-8")


def _tables() -> List[ddl.Table]:
    tables, _ = ddl.parse(_schema_sql())
    return tables


def _primary_key(t: ddl.Table) -> List[str]:
    return list(t.primary_key) or [c.name for c in t.columns if c.inline_pk]


def preflight(conn: sqlite3.Connection) -> List[str]:
    """Problems PostgreSQL will reject, found before anything is written."""
    problems: List[str] = []
    for t in _tables():
        if t.virtual:
            continue
        pk = _primary_key(t)
        if not pk:
            continue
        for col in pk:
            n = conn.execute(
                f"SELECT COUNT(*) FROM {t.name} WHERE {col} IS NULL"
            ).fetchone()[0]
            if n:
                problems.append(
                    f"{t.name}.{col} is NULL in {n:,} row(s); it is part of the "
                    f"primary key and PostgreSQL requires NOT NULL there")
        cols = ", ".join(pk)
        dup = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT {cols} FROM {t.name} "
            f"GROUP BY {cols} HAVING COUNT(*) > 1) x").fetchone()[0]
        if dup:
            problems.append(
                f"{t.name} has {dup:,} duplicated primary key(s) on ({cols}); "
                f"SQLite tolerated them because two NULLs compare unequal")
    return problems


def _columns_for(t: ddl.Table) -> Sequence[str]:
    if t.virtual:
        return FTS_COLUMNS[t.name]
    return [c.name for c in t.columns]


def load(sqlite_path: Path, dsn: str, schema: str = ddl.DEFAULT_SCHEMA,
         verbose: bool = True) -> int:
    import psycopg

    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    problems = preflight(src)
    if problems:
        print()
        print("  REFUSING to load - PostgreSQL would reject these:")
        for p in problems:
            print(f"    {p}")
        print()
        src.close()
        return 1

    tables = _tables()
    results: List[Tuple[str, int, int, float]] = []
    total_rows = 0
    t_start = time.perf_counter()

    with psycopg.connect(dsn) as dst:
        with dst.cursor() as cur:
            # One transaction for the whole load: a half-migrated store is
            # worse than no migrated store, and this is also what makes the
            # row-count comparison below meaningful.
            for t in tables:
                cols = list(_columns_for(t))
                collist = ", ".join(cols)
                cur.execute(f"TRUNCATE {schema}.{t.name}")

                t0 = time.perf_counter()
                n = 0
                copy_sql = (f"COPY {schema}.{t.name} ({collist}) FROM STDIN")
                with cur.copy(copy_sql) as cp:
                    for row in src.execute(f"SELECT {collist} FROM {t.name}"):
                        cp.write_row(row)
                        n += 1
                took = time.perf_counter() - t0
                results.append((t.name, n, 0, took))
                total_rows += n
                if verbose and n:
                    print(f"    {t.name:<22}{n:>10,}  {took:>6.2f}s")
        dst.commit()

        # counts read back AFTER the commit, from the server
        with dst.cursor() as cur:
            for i, (name, n, _, took) in enumerate(results):
                got = cur.execute(
                    f"SELECT COUNT(*) FROM {schema}.{name}").fetchone()[0]
                results[i] = (name, n, got, took)

    elapsed = time.perf_counter() - t_start
    src.close()

    bad = [r for r in results if r[1] != r[2]]
    print()
    print("  " + "-" * 66)
    print(f"  {total_rows:,} rows into {len(results)} tables in {elapsed:.1f}s")
    if bad:
        print()
        print("  ROW COUNT DISAGREEMENT:")
        for name, sent, got, _ in bad:
            print(f"    {name:<22}sent {sent:,}, server has {got:,}")
        return 1
    print("  every table's server count equals the rows sent")
    print()
    return 0


def verify(sqlite_path: Path, dsn: str,
           schema: str = ddl.DEFAULT_SCHEMA) -> int:
    """Compare row counts table by table, writing nothing."""
    import psycopg

    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    tables = _tables()
    print()
    print("  kb.store.load --verify")
    print("  " + "=" * 66)
    print(f"  {'table':<22}{'sqlite':>12}{'postgres':>12}")
    print("  " + "-" * 66)

    bad = 0
    tot_s = tot_p = 0
    with psycopg.connect(dsn) as dst:
        for t in tables:
            a = src.execute(f"SELECT COUNT(*) FROM {t.name}").fetchone()[0]
            b = dst.execute(
                f"SELECT COUNT(*) FROM {schema}.{t.name}").fetchone()[0]
            tot_s += a
            tot_p += b
            flag = "" if a == b else "   <-- DIFFERS"
            bad += a != b
            if a or b:
                print(f"  {t.name:<22}{a:>12,}{b:>12,}{flag}")
    src.close()
    print("  " + "-" * 66)
    print(f"  {'TOTAL':<22}{tot_s:>12,}{tot_p:>12,}")
    print()
    print("  the two stores hold the same number of rows in every table"
          if not bad else f"  {bad} TABLE(S) DIFFER")
    print()
    return 1 if bad else 0


def main(argv=None) -> int:
    from kb import config

    ap = argparse.ArgumentParser(description="SQLite facts -> PostgreSQL")
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--schema", default=ddl.DEFAULT_SCHEMA)
    ap.add_argument("--sqlite", default=None,
                    help="path to facts.db (default: kb.config.FACTS_DB)")
    ap.add_argument("--verify", action="store_true",
                    help="compare row counts only, write nothing")
    a = ap.parse_args(argv)

    path = Path(a.sqlite) if a.sqlite else config.FACTS_DB
    if not path.exists():
        print(f"  no facts database at {path}", file=sys.stderr)
        return 1
    if a.verify:
        return verify(path, a.dsn, a.schema)
    print()
    print(f"  loading {path}")
    print(f"  into    {a.schema} on the server")
    print()
    return load(path, a.dsn, a.schema)


if __name__ == "__main__":
    sys.exit(main())
