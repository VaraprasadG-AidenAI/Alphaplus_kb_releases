"""
Generate the PostgreSQL schema from kb/index/schema.sql.

    python -m kb.store.ddl --print
    python -m kb.store.ddl --apply --dsn "host=... dbname=..." [--schema kb]
    python -m kb.store.ddl --check --dsn "host=... dbname=..."

Generated rather than hand-written, and kept in ONE file rather than two.
`kb/index/schema.sql` stays the source of truth: it carries the reasoning for
every column, and this repository has already paid twice for having two copies of
something that has to agree - the member_hash join, and _CAN_CHANGE. A
`schema.pg.sql` maintained beside it would drift the same way, and the drift
would be silent because nothing compares them.

## What is translated, and what is decided

The vocabulary is small: 27 tables, 2 FTS5 virtual tables, 53 indexes, 2
PRAGMAs, and only three column types. So this is a targeted translator, not a
general one, and it refuses anything it does not recognise rather than guessing.

Five decisions are encoded here because getting any of them wrong breaks
something with no obvious link to the cause:

  * **Flags stay `integer`, NOT `boolean`.**  `is_key`, `share`, `replacing`,
    `is_loop`, `inline`, `in_loop` hold 0/1 and are aggregated by `MAX(...)` and
    `MIN(...)` in kb/graph/model.py.  `MAX(boolean)` does not exist in
    PostgreSQL, so a "tidier" type breaks three edge specs and six write sites.

  * **Timestamps stay `text`.**  Every one is produced by Python's
    `datetime.isoformat()` and read back by slicing - `built_at[:19]`.  There is
    not one SQL date function in the whole codebase.  `timestamptz` would break
    both ends at once.

  * **`REAL` becomes `double precision`, NOT `numeric`.**  `member_hash.mtime`
    is compared with `abs(stat.st_mtime - mtime) < 0.001` in
    kb.index.update.  `numeric` comes back as `Decimal`, and subtracting a
    `Decimal` from a `float` raises TypeError.

  * **`member_hash.size` becomes `bigint`.**  It is a file size.  On the replica
    nothing is near 2 GB; on a 40 GB extract, betting the load on that is free
    to avoid.

  * **`''` stays the sentinel and is never converted to NULL.**  `WHERE
    callee<>''`, `WHERE tier<>''` and `COALESCE(x,'')<>''` are load-bearing in
    dozens of queries.

## Two things that genuinely change

**Primary keys become NOT NULL.**  Four of the six composite keys include
`library TEXT DEFAULT ''`.  A non-INTEGER primary key in SQLite is only a UNIQUE
index, and SQLite permits NULL in it - so `('ORD100','CBLLE',NULL)` can be
inserted repeatedly with no complaint.  PostgreSQL makes primary key columns NOT
NULL.  That is strictly better and it is also a behaviour change: anything that
has ever written a NULL library becomes a hard `23505` at load time instead of a
silent duplicate.  `--check` reports it rather than letting the loader discover
it.

**FTS5 becomes a `tsvector` column plus a GIN index.**  `search` and
`search_summary` stop being virtual tables and become ordinary tables with a
generated column.  The ranking is NOT equivalent and must not be presented as
such:

  * `porter unicode61` and PostgreSQL's `english` (Snowball) stem differently.
  * FTS5's `rank` is a NEGATIVE bm25; `ts_rank_cd` is positive.  Every
    `ORDER BY rank` becomes `ORDER BY score DESC`.
  * `bm25(search_summary, 0.2, 3.0, 1.0)` uses arbitrary float column weights.
    PostgreSQL has four discrete classes (A/B/C/D), so the ratio can be
    approximated by an assignment plus a weight array passed to `ts_rank_cd`,
    but it cannot be reproduced.  `route_ticket` must be re-measured with
    kb.eval.probe, not assumed.

The one piece of luck: `kb.server.tools._rank_candidates` decays by rank
POSITION, not by score, and `kb.server.routing` fuses by rank as well.  So the
ranking arithmetic survives a change of scoring function even though no score
does.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

DEFAULT_SCHEMA = "kb"

#: SQLite type -> PostgreSQL type.  Three entries, because the schema uses three.
TYPES: Dict[str, str] = {
    "TEXT": "text",
    "INTEGER": "integer",
    "REAL": "double precision",     # NOT numeric - see the module docstring
}

#: (table, column) -> a wider type than TYPES would give.
WIDER: Dict[Tuple[str, str], str] = {
    ("member_hash", "size"): "bigint",
}

#: The FTS5 virtual tables, and how each one's text is weighted once it is a
#: tsvector.  PostgreSQL offers four discrete classes where bm25() took floats,
#: so this is an ORDERING of the columns by importance, not a translation of the
#: numbers.  The numbers that were being approximated are recorded beside each.
FTS: Dict[str, Dict[str, object]] = {
    "search": {
        # queried with a plain MATCH and no column weights, so name and body
        # are simply both searchable; name first because an exact member name
        # is the strongest signal the index has.
        "weights": {"name": "A", "body": "B"},
        "carry": ["name", "kind", "body"],
        "note": "the English the codebase already contains",
    },
    "search_summary": {
        # bm25(search_summary, 0.2, 3.0, 1.0): key 3.0, body 1.0, scope 0.2.
        # key -> A, body -> B, scope -> D preserves that order.  The MAGNITUDES
        # do not survive; ts_rank_cd takes a {D,C,B,A} array so they can be
        # tuned, and kb.eval.probe is what decides the values.
        "weights": {"key": "A", "body": "B", "scope": "D"},
        "carry": ["scope", "key", "body"],
        "note": "text a model wrote about the codebase",
    },
}


#: SQLite functions PostgreSQL does not have, added rather than edited around.
#:
#: `group_concat` appears 22 times: 20 of them inside the `select` strings of
#: kb/graph/model.py, which are the SQL that feeds the Neo4j loader.  Rewriting
#: those to `string_agg` would mean either two copies of every spec or a
#: dialect-aware model, and "one definition, three consumers" is the entire
#: design of that file.  Teaching the server the function instead leaves all 22
#: call sites untouched and store-neutral.
#:
#: Three overloads, because SQLite is loosely typed and PostgreSQL is not:
#: `group_concat(line)` passes an integer, `group_concat(DISTINCT paragraph)`
#: passes text, and kb/index/consistency.py uses the two-argument form with an
#: explicit ' + ' separator.  Verified against SQLite on real data, including
#: the CALLS spec itself.
#:
#: In `public`, not in the KB schema, so it resolves whatever the search_path
#: is - a function that only works when someone remembered to set search_path
#: is a trap, not a shim.
#:
#: NULLs are skipped and the default separator is ',', both matching SQLite.
COMPAT_SQL = """
CREATE OR REPLACE FUNCTION public._gc_step(state text, value text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN value IS NULL THEN state
              WHEN state IS NULL THEN value
              ELSE state || ',' || value END
$$;

CREATE OR REPLACE FUNCTION public._gc_step_int(state text, value integer)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN value IS NULL THEN state
              WHEN state IS NULL THEN value::text
              ELSE state || ',' || value::text END
$$;

CREATE OR REPLACE FUNCTION public._gc_step_sep(state text, value text, sep text)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN value IS NULL THEN state
              WHEN state IS NULL THEN value
              ELSE state || sep || value END
$$;

-- SQLite's CAST('2522,4003' AS INTEGER) prefix-parses to 2522.  PostgreSQL
-- refuses it (22P02), and three tool queries relied on that behaviour to read
-- the first line number out of field_usage.lines, which is a comma list.
-- `instr` is SQLite-native and missing here, so the shim lets one portable
-- expression - see tools._FIRST_LINE - serve both stores.
CREATE OR REPLACE FUNCTION public.instr(haystack text, needle text)
RETURNS integer LANGUAGE sql IMMUTABLE AS $$
  SELECT strpos(haystack, needle)
$$;

DROP AGGREGATE IF EXISTS public.group_concat(text) CASCADE;
CREATE AGGREGATE public.group_concat(text)
    (SFUNC = public._gc_step, STYPE = text);

DROP AGGREGATE IF EXISTS public.group_concat(integer) CASCADE;
CREATE AGGREGATE public.group_concat(integer)
    (SFUNC = public._gc_step_int, STYPE = text);

DROP AGGREGATE IF EXISTS public.group_concat(text, text) CASCADE;
CREATE AGGREGATE public.group_concat(text, text)
    (SFUNC = public._gc_step_sep, STYPE = text);
"""


@dataclass
class Column:
    name: str
    sqlite_type: str
    not_null: bool = False
    default: Optional[str] = None
    inline_pk: bool = False
    comment: str = ""


@dataclass
class Table:
    name: str
    columns: List[Column] = field(default_factory=list)
    primary_key: List[str] = field(default_factory=list)
    comment_block: List[str] = field(default_factory=list)
    virtual: bool = False


class Unsupported(RuntimeError):
    """Something in schema.sql this translator will not guess at."""


# =======================================================================
# Reading schema.sql
# =======================================================================

def schema_path() -> Path:
    """Locate kb/index/schema.sql, wherever the kb.index half happens to live.

    kb.store ships as source; kb.index ships compiled, in the wheel.  Walking
    up from this file's own path finds schema.sql only when both halves sit in
    the same tree, which is true for a developer checkout and false for a
    client - there kb/index is in site-packages and the source tree has no
    kb/index at all.  importlib.resources asks the import system instead, so
    it resolves to whichever half actually holds the package.
    """
    from importlib.resources import files
    try:
        found = files("kb.index") / "schema.sql"
        if found.is_file():
            return Path(str(found))
    except (ImportError, ModuleNotFoundError, TypeError):
        pass
    # Fallback: the sibling directory, for a tree where kb.index is not
    # importable but the file is sitting there anyway.
    return Path(__file__).resolve().parents[1] / "index" / "schema.sql"


_CREATE = re.compile(
    r"CREATE\s+(VIRTUAL\s+)?TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)\s*"
    r"(?:USING\s+fts5)?\s*\((.*?)\n\);", re.S | re.I)
_INDEX = re.compile(
    r"CREATE\s+(UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+ON\s+"
    r"(\w+)\s*\(([^)]*)\)\s*;", re.I)
_COL = re.compile(
    r"^(\w+)\s+(TEXT|INTEGER|REAL)\b(.*)$", re.I)
_TABLE_PK = re.compile(r"^PRIMARY\s+KEY\s*\((.*?)\)", re.I)


def parse(sql: str) -> Tuple[List[Table], List[Tuple[str, bool, str, str]]]:
    """(tables, indexes) out of schema.sql.  Comments are kept."""
    tables: List[Table] = []
    for virtual, name, body in _CREATE.findall(sql):
        t = Table(name=name, virtual=bool(virtual.strip()))
        for raw in body.split("\n"):
            line = raw.strip().rstrip(",")
            if not line:
                continue
            if line.startswith("--"):
                continue
            # trailing comment on a column definition
            comment = ""
            if "--" in line:
                line, _, comment = line.partition("--")
                line, comment = line.strip().rstrip(","), comment.strip()
            if not line:
                continue

            pk = _TABLE_PK.match(line)
            if pk:
                t.primary_key = [c.strip() for c in pk.group(1).split(",")]
                continue

            if t.virtual:
                # fts5 body is a bare column list plus a tokenize= option
                first = line.split("=")[0].strip()
                if first.lower() == "tokenize":
                    continue
                t.columns.append(Column(name=first, sqlite_type="TEXT",
                                        comment=comment))
                continue

            m = _COL.match(line)
            if not m:
                raise Unsupported(f"{name}: cannot read column line {line!r}")
            cname, ctype, rest = m.group(1), m.group(2).upper(), m.group(3)
            col = Column(name=cname, sqlite_type=ctype, comment=comment)
            upper = rest.upper()
            col.not_null = "NOT NULL" in upper
            col.inline_pk = "PRIMARY KEY" in upper
            d = re.search(r"DEFAULT\s+('(?:[^']|'')*'|NULL|-?\d+)", rest, re.I)
            if d:
                col.default = d.group(1)
            leftover = re.sub(
                r"NOT\s+NULL|PRIMARY\s+KEY|DEFAULT\s+('(?:[^']|'')*'|NULL|-?\d+)",
                "", rest, flags=re.I).strip()
            if leftover:
                raise Unsupported(
                    f"{name}.{cname}: unrecognised modifier {leftover!r}")
            t.columns.append(col)
        tables.append(t)

    indexes = [(nm, bool(uniq.strip()), tbl, cols)
               for uniq, nm, tbl, cols in _INDEX.findall(sql)]
    return tables, indexes


# =======================================================================
# Emitting PostgreSQL
# =======================================================================

def _pg_type(table: str, col: Column) -> str:
    wider = WIDER.get((table, col.name))
    if wider:
        return wider
    try:
        return TYPES[col.sqlite_type]
    except KeyError:
        raise Unsupported(
            f"{table}.{col.name}: no PostgreSQL type for "
            f"{col.sqlite_type}") from None


def _table_ddl(t: Table, schema: str) -> str:
    if t.virtual:
        return _fts_ddl(t, schema)

    pk = list(t.primary_key)
    for c in t.columns:
        if c.inline_pk and not pk:
            pk = [c.name]

    # (definition, trailing comment) pairs.  Built as pairs rather than as
    # finished lines because the comma that separates two definitions has to go
    # BEFORE the comment, and a comma appended to a commented line is invisible
    # to any "strip the last comma" pass - which is exactly how this emitted an
    # unparseable table body the first time.
    parts: List[Tuple[str, str]] = []
    for c in t.columns:
        bits = [f"    {c.name.ljust(13)} {_pg_type(t.name, c)}"]
        # A primary key column is NOT NULL in PostgreSQL whether we say so or
        # not; saying so keeps the DDL honest about what changed.
        if c.not_null or c.name in pk:
            bits.append(" NOT NULL")
        if c.default is not None and c.default.upper() != "NULL":
            bits.append(f" DEFAULT {c.default}")
        parts.append(("".join(bits), c.comment))
    if pk:
        parts.append((f"    PRIMARY KEY ({', '.join(pk)})", ""))

    lines = []
    for i, (definition, comment) in enumerate(parts):
        text = definition + ("," if i < len(parts) - 1 else "")
        if comment:
            text = text.ljust(58) + f"-- {comment}"
        lines.append(text)
    body = "\n".join(lines)
    return f"CREATE TABLE IF NOT EXISTS {schema}.{t.name} (\n{body}\n);"


def _fts_ddl(t: Table, schema: str) -> str:
    spec = FTS.get(t.name)
    if spec is None:
        raise Unsupported(f"{t.name}: an FTS5 table with no entry in ddl.FTS")
    weights: Dict[str, str] = spec["weights"]          # type: ignore[assignment]
    carry: Sequence[str] = spec["carry"]               # type: ignore[assignment]
    note = spec["note"]

    cols = "\n".join(f"    {c:<14}text NOT NULL DEFAULT ''," for c in carry)
    pieces = " ||\n            ".join(
        f"setweight(to_tsvector('english', coalesce({c}, '')), '{w}')"
        for c, w in weights.items())
    return (
        f"-- {t.name}: {note}.\n"
        f"-- Was `CREATE VIRTUAL TABLE ... USING fts5(tokenize='porter "
        f"unicode61')`.\n"
        f"-- The stemming, the score sign and the column weighting all differ - "
        f"see\n"
        f"-- kb/store/ddl.py. Ranking must be re-measured, not assumed.\n"
        f"CREATE TABLE IF NOT EXISTS {schema}.{t.name} (\n"
        f"{cols}\n"
        f"    tsv           tsvector GENERATED ALWAYS AS (\n"
        f"            {pieces}\n"
        f"    ) STORED\n"
        f");\n"
        f"CREATE INDEX IF NOT EXISTS ix_{t.name}_tsv\n"
        f"    ON {schema}.{t.name} USING GIN (tsv);")


def _index_ddl(name: str, unique: bool, table: str, cols: str,
               schema: str) -> str:
    kind = "UNIQUE INDEX" if unique else "INDEX"
    tidy = ", ".join(c.strip() for c in cols.split(","))
    return (f"CREATE {kind} IF NOT EXISTS {name}\n"
            f"    ON {schema}.{table} ({tidy});")


def generate(schema_sql: Optional[Path] = None,
             schema: str = DEFAULT_SCHEMA) -> str:
    path = schema_sql or schema_path()
    tables, indexes = parse(path.read_text(encoding="utf-8"))

    out: List[str] = [
        "-- PostgreSQL schema for the ALPHAPLUS knowledge base.",
        "--",
        "-- GENERATED by `python -m kb.store.ddl --print` from",
        f"-- kb/index/schema.sql. Do not edit: edit that file instead, so the",
        "-- reasoning for every column stays in one place.",
        "--",
        "-- The two PRAGMAs in the source are dropped: journal_mode=WAL and",
        "-- synchronous=NORMAL are SQLite file-format settings with no meaning",
        "-- to a server.",
        "",
        f"CREATE SCHEMA IF NOT EXISTS {schema};",
        "",
        "-- " + "=" * 70,
        "-- SQLite compatibility: see ddl.COMPAT_SQL for why these exist",
        "-- " + "=" * 70,
        COMPAT_SQL.strip(),
        "",
    ]
    for t in tables:
        out.append(_table_ddl(t, schema))
        out.append("")
    out.append("-- " + "=" * 70)
    out.append(f"-- {len(indexes)} indexes")
    out.append("-- " + "=" * 70)
    out.append("")
    for name, unique, table, cols in indexes:
        out.append(_index_ddl(name, unique, table, cols, schema))
    out.append("")
    return "\n".join(out)


# =======================================================================
# Applying and checking
# =======================================================================

def apply(dsn: str, schema: str = DEFAULT_SCHEMA,
          verbose: bool = True) -> int:
    import psycopg
    ddl = generate(schema=schema)
    with psycopg.connect(dsn) as conn:
        conn.autocommit = True
        conn.execute(ddl)
        n = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = %s", (schema,)).fetchone()[0]
    if verbose:
        print(f"  applied: {n} tables in schema {schema}")
    return 0


def check(dsn: str, schema: str = DEFAULT_SCHEMA) -> int:
    """Compare the live schema against schema.sql, table and column."""
    import psycopg
    tables, indexes = parse(schema_path().read_text(encoding="utf-8"))
    want: Dict[str, Dict[str, str]] = {}
    for t in tables:
        if t.virtual:
            spec = FTS[t.name]
            cols = {c: "text" for c in spec["carry"]}   # type: ignore[index]
            cols["tsv"] = "tsvector"
        else:
            cols = {c.name: _pg_type(t.name, c) for c in t.columns}
        want[t.name] = cols

    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT table_name, column_name, data_type, is_nullable "
            "FROM information_schema.columns WHERE table_schema = %s",
            (schema,)).fetchall()
        idx = {r[0] for r in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = %s",
            (schema,)).fetchall()}

    have: Dict[str, Dict[str, str]] = {}
    nullable: Dict[Tuple[str, str], bool] = {}
    for tbl, col, dtype, isnull in rows:
        have.setdefault(tbl, {})[col] = dtype
        nullable[(tbl, col)] = (isnull == "YES")

    print()
    print(f"  kb.store.ddl --check   schema {schema}")
    print("  " + "=" * 66)
    bad = 0

    missing_tables = sorted(set(want) - set(have))
    extra_tables = sorted(set(have) - set(want))
    if missing_tables:
        bad += 1
        print(f"  MISSING table(s): {', '.join(missing_tables)}")
    if extra_tables:
        print(f"  extra table(s) not in schema.sql: {', '.join(extra_tables)}")

    for tbl in sorted(set(want) & set(have)):
        w, h = want[tbl], have[tbl]
        miss = sorted(set(w) - set(h))
        extra = sorted(set(h) - set(w))
        wrong = [(c, w[c], h[c]) for c in sorted(set(w) & set(h))
                 if not _type_matches(w[c], h[c])]
        if miss or extra or wrong:
            bad += 1
            print(f"  {tbl}")
            for c in miss:
                print(f"      missing column {c} ({w[c]})")
            for c in extra:
                print(f"      extra column   {c} ({h[c]})")
            for c, a, b in wrong:
                print(f"      {c}: want {a}, have {b}")

    print("  " + "-" * 66)
    print(f"  {len(want)} tables expected, {len(have)} present, "
          f"{len(idx)} indexes")

    # the behaviour change worth naming out loud
    forced = [f"{t}.{c}" for t in sorted(want)
              for c in want[t]
              if (t, c) in nullable and not nullable[(t, c)]
              and c == "library"]
    if forced:
        print()
        print("  PRIMARY KEY columns now NOT NULL (SQLite allowed NULL here):")
        for f in forced:
            print(f"      {f}")

    print()
    print("  schema matches schema.sql" if not bad
          else f"  {bad} DISAGREEMENT(S) ABOVE")
    print()
    return 1 if bad else 0


def _type_matches(want: str, have: str) -> bool:
    """information_schema spells some types differently from the DDL."""
    same = {
        "integer": {"integer"},
        "bigint": {"bigint"},
        "text": {"text"},
        "double precision": {"double precision"},
        "tsvector": {"tsvector"},
    }
    return have in same.get(want, {want})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="PostgreSQL schema from kb/index/schema.sql")
    ap.add_argument("--print", dest="do_print", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--schema", default=DEFAULT_SCHEMA)
    ap.add_argument("--out", default=None, help="write the DDL to a file")
    a = ap.parse_args(argv)

    if a.do_print or a.out:
        ddl = generate(schema=a.schema)
        if a.out:
            Path(a.out).write_text(ddl, encoding="utf-8")
            print(f"  wrote {a.out} ({len(ddl.splitlines())} lines)")
        else:
            print(ddl)
        return 0
    if a.apply:
        if not a.dsn:
            ap.error("--apply needs --dsn")
        return apply(a.dsn, a.schema)
    if a.check:
        if not a.dsn:
            ap.error("--check needs --dsn")
        return check(a.dsn, a.schema)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
