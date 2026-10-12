"""
Build into a staging schema, then make it live in one statement.

    python -m kb.store.swap --status --dsn "..."
    python -m kb.store.swap --promote --dsn "..."     (normally done by build)

The SQLite equivalent of this is `kb.index.build.create` and `.swap`: write to
`facts.db.new`, then `os.replace` it over `facts.db`. That whole mechanism -
the `.new` sidecar, the `-wal`/`-shm` cleanup, the six-line diagnostic naming
`tasklist` and the MCP Inspector - exists for ONE reason. A SQLite database is a
file, an open file cannot be deleted on Windows, and "rebuild" was implemented
as delete-and-recreate. We hit exactly that this week: DBeaver held facts.db and
the swap could not complete.

PostgreSQL removes the whole class of failure, because a reader and a writer do
not contend for a file handle. The build writes into `kb_build`, and one
transaction renames it into place:

    BEGIN;
      DROP SCHEMA IF EXISTS kb_old CASCADE;
      ALTER SCHEMA kb       RENAME TO kb_old;
      ALTER SCHEMA kb_build RENAME TO kb;
    COMMIT;

Readers on `search_path = kb` see the old data until the commit and the new data
after it. Nobody is evicted and nothing has to be timed.

## Why a staging schema rather than TRUNCATE in place

`TRUNCATE` takes an ACCESS EXCLUSIVE lock, so a reader mid-query blocks the
build - which is the same contention the file lock caused, moved down a layer.
The rename takes that lock too, but for the duration of a catalogue update
rather than for the length of a 100-second load.

## Why the build needs no other change

Every query in kb/index/build.py names tables WITHOUT a schema. So pointing the
build at the staging schema is one `SET search_path TO kb_build`, and the 24
`executemany` calls, the layer pass and the carry-forward all land there
untouched. That is the same property that let the graph loader move stores
without editing a single spec.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional, Tuple

from . import ddl

#: The schema a build writes into, and the one it becomes.
STAGING_SUFFIX = "_build"
PREVIOUS_SUFFIX = "_old"


def staging_name(schema: str) -> str:
    return f"{schema}{STAGING_SUFFIX}"


def previous_name(schema: str) -> str:
    return f"{schema}{PREVIOUS_SUFFIX}"


def prepare(dsn: str, schema: str = ddl.DEFAULT_SCHEMA,
            verbose: bool = True) -> str:
    """Create an empty staging schema with the full DDL.  Returns its name.

    Dropped and recreated rather than truncated: a build that changed
    schema.sql should not have to reconcile the old shape, and CASCADE here
    only ever removes a previous *staging* schema, never the live one.
    """
    import psycopg
    stage = staging_name(schema)
    with psycopg.connect(dsn) as conn:
        conn.autocommit = True
        conn.execute(f"DROP SCHEMA IF EXISTS {stage} CASCADE")
    ddl.apply(dsn, stage, verbose=False)
    if verbose:
        print(f"  staging schema {stage} created")
    return stage


def promote(dsn: str, schema: str = ddl.DEFAULT_SCHEMA,
            keep_previous: bool = False, verbose: bool = True) -> bool:
    """Make the staging schema live, in one transaction.

    Returns False and says why rather than raising, matching
    `kb.index.build.swap`: the work is not lost, it just is not live yet.
    """
    import psycopg
    stage, prev = staging_name(schema), previous_name(schema)
    try:
        with psycopg.connect(dsn) as conn:
            with conn.transaction():
                have = {r[0] for r in conn.execute(
                    "SELECT nspname FROM pg_namespace "
                    "WHERE nspname = ANY(%s)",
                    ([schema, stage, prev],)).fetchall()}
                if stage not in have:
                    print(f"  no staging schema {stage} to promote",
                          file=sys.stderr)
                    return False
                conn.execute(f"DROP SCHEMA IF EXISTS {prev} CASCADE")
                if schema in have:
                    conn.execute(f"ALTER SCHEMA {schema} RENAME TO {prev}")
                conn.execute(f"ALTER SCHEMA {stage} RENAME TO {schema}")
            if not keep_previous:
                conn.autocommit = True
                conn.execute(f"DROP SCHEMA IF EXISTS {prev} CASCADE")
    except Exception as exc:                                # noqa: BLE001
        print(f"  could not promote {stage} to {schema}: {exc}",
              file=sys.stderr)
        print(f"  the data is still in {stage}; nothing was lost.",
              file=sys.stderr)
        return False
    if verbose:
        kept = f", previous kept as {prev}" if keep_previous else ""
        print(f"  {stage} is now {schema}{kept}")
    return True


def status(dsn: str, schema: str = ddl.DEFAULT_SCHEMA) -> int:
    import psycopg
    stage, prev = staging_name(schema), previous_name(schema)
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT n.nspname, COUNT(c.oid) "
            "  FROM pg_namespace n "
            "  LEFT JOIN pg_class c ON c.relnamespace = n.oid "
            "                      AND c.relkind = 'r' "
            " WHERE n.nspname = ANY(%s) GROUP BY n.nspname ORDER BY n.nspname",
            ([schema, stage, prev],)).fetchall()
    print()
    print("  kb.store.swap")
    print("  " + "=" * 50)
    if not rows:
        print(f"  none of {schema}, {stage}, {prev} exist")
    for name, n in rows:
        role = ("live" if name == schema else
                "staging" if name == stage else "previous")
        print(f"  {name:<14}{n:>3} table(s)   {role}")
    print()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="staging-schema swap")
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--schema", default=ddl.DEFAULT_SCHEMA)
    ap.add_argument("--prepare", action="store_true")
    ap.add_argument("--promote", action="store_true")
    ap.add_argument("--keep-previous", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args(argv)
    if a.prepare:
        prepare(a.dsn, a.schema)
        return 0
    if a.promote:
        return 0 if promote(a.dsn, a.schema, a.keep_previous) else 1
    return status(a.dsn, a.schema)


if __name__ == "__main__":
    sys.exit(main())
