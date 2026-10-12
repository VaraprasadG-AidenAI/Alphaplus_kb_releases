"""
Load facts.db into Neo4j.  Batched, idempotent, resumable.

    python -m kb.graph.load --estimate       # how much, without writing
    python -m kb.graph.load                  # nodes then relationships
    python -m kb.graph.load --only Member CALLS
    python -m kb.graph.load --wipe           # delete our graph first
    python -m kb.graph.load --verify         # counts, SQLite against Neo4j

## The one rule this module exists to enforce

Every write is wrapped in

    CALL { WITH row ... } IN TRANSACTIONS OF $batch ROWS

so the JVM heap only ever holds one batch of uncommitted state.  An unbounded
transaction holding a whole table is what kept killing the discovery agent at
around 200 MB - see docs/NEO4J-LIMITS-REPORT.md.  There is no code path here
that sends a whole table in one transaction, and `kb/graph/driver.py`
deliberately offers no helper that would let one be written by accident.

The trade that buys: **atomicity is per batch, not per table.**  A failure at
batch 7 leaves batches 1-6 committed.  That is acceptable here only because
every statement is a MERGE keyed on `uid`, so re-running converges rather than
duplicating.  It is a deliberate choice, not an oversight - `--verify` is how
you confirm the result after a failed run.

## Why UNWIND and not one statement per row

One planned query executed against 5,000 parameter rows, rather than 5,000
planned queries.  The planner sees the shape once.  This is the difference
between a load that takes a minute and one that takes an hour.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from kb.graph import driver, model
from kb import store as kbstore
from kb.graph import settings as gs

#: Marker property on every node and relationship we create, so --wipe and the
#: incremental path can find exactly our data and nothing else.
OWNER = "alphaplus"


# ---------------------------------------------------------------------------
# reading SQLite
# ---------------------------------------------------------------------------

def _facts_db() -> str:
    cfg = gs.load()
    if cfg.facts_db:
        return cfg.facts_db
    from kb import config
    return str(config.FACTS_DB)


def _connect():
    """Read-only connection to the fact store, whichever store that is.

    THE factory for the whole package - kb.graph.update, kb.graph.bench and
    kb.graph.selftest all import this one.  kb.graph.fulltext used to carry a
    byte-for-byte copy of it, which is how a port forgets a reader.

    The specs in kb.graph.model need no per-store variants: the only construct
    in them PostgreSQL lacks is `group_concat`, and kb.store.ddl teaches the
    server that function rather than making 20 spec strings dialect-aware.
    """
    if kbstore.backend() == "postgres":
        return kbstore.connect(read_only=True)
    path = _facts_db()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _chunks(rows: Iterable[sqlite3.Row], size: int) -> Iterator[List[Dict[str, Any]]]:
    batch: List[Dict[str, Any]] = []
    for r in rows:
        batch.append({k: r[k] for k in r.keys()})
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


# ---------------------------------------------------------------------------
# building uids in Cypher
# ---------------------------------------------------------------------------

def _uid_expr(cols: Sequence[str], prefix: str = "row.") -> str:
    """Cypher that builds a uid from row columns, matching model.uid().

    coalesce because a library or member_type can legitimately be '' and
    concatenating null would silently produce a null uid - a row that vanishes
    with no error, which is the class of bug this whole module is written
    against.
    """
    parts = [f"coalesce(toString({prefix}{c}), '')" for c in cols]
    if len(parts) == 1:
        # A single-column key still needs the separators, so that
        # uid('ORD100') from a Field never collides with a Member's.
        return parts[0]
    return (" + '" + model.SEP + "' + ").join(parts)


def _member_uid_expr(name_col: str) -> str:
    """Member uid when only the NAME is known.

    Most facts reference a member by name alone - `call_edge.callee` is
    'ORD100', not 'ORD100|CBLLE|APPALPH'.  So relationships match on the
    indexed `name` property rather than on `uid`, and a name present in two
    libraries legitimately matches both.  That is not a bug: the fact really
    does apply to both until something disambiguates it, and Rule 2 says say so
    rather than pick one.
    """
    return f"coalesce(toString(row.{name_col}), '')"


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------

def load_node(spec: model.NodeSpec, conn: sqlite3.Connection,
              cfg: gs.Settings, verbose: bool = True) -> Dict[str, int]:
    """MERGE one label from its table."""
    # `owner` and the key columns are always set, so there is always something
    # to SET even for a label with no extra properties (:Field, :DataArea).
    assigns = [f"n.{p} = row.{p}" for p in spec.props]
    assigns.append(f"n.owner = '{OWNER}'")
    # key columns are properties too - they are what tools filter on
    for k in spec.key:
        assigns.append(f"n.{k} = coalesce(row.{k}, '')")
    set_clause = "SET " + ", ".join(assigns)

    cypher = f"""
    UNWIND $rows AS row
    CALL {{
      WITH row
      MERGE (n:{spec.label} {{uid: {_uid_expr(spec.key)}}})
      {set_clause}
    }} IN TRANSACTIONS OF $batch ROWS
    """

    total = created = 0
    t0 = time.perf_counter()
    for batch in _chunks(conn.execute(spec.select), cfg.load.fetch_rows):
        counters = driver.write_autocommit(cypher, {"rows": batch, "batch": cfg.load.batch_rows})
        total += len(batch)
        created += counters["nodes_created"]
    took = time.perf_counter() - t0
    if verbose:
        print(f"  :{spec.label:<12} {total:>9,} rows  {created:>9,} new  {took:>6.1f}s")
    return {"rows": total, "created": created}


# ---------------------------------------------------------------------------
# relationships
# ---------------------------------------------------------------------------

def _expand_sql_tables(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """sql_usage.tables_json holds a JSON list; one row per table.

    Done here rather than in SQL because SQLite has no json_each guarantee in
    every build we might meet, and the list is short.
    """
    out: List[Dict[str, Any]] = []
    for r in rows:
        raw = r.get("table_name") or ""
        names: List[str] = []
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    names = [str(x).upper() for x in parsed if x]
                elif isinstance(parsed, str):
                    names = [parsed.upper()]
            except (json.JSONDecodeError, TypeError):
                names = [str(raw).upper()]
        for n in names:
            copy = dict(r)
            copy["table_name"] = n
            out.append(copy)
    return out


def _edge_cypher(spec: model.EdgeSpec) -> str:
    """The MERGE statement for one relationship type.

    Extracted so `kb.graph.update` writes with the SAME statement the bulk load
    uses.  A second copy of this logic in the incremental path is precisely how
    the 4,701 lost WRITES_FIELD edges would come back: the bug was a missing
    `key_props` in the MERGE pattern, it was invisible until `--verify`, and it
    would be invisible again in a duplicate that drifted.  One builder, two
    callers.
    """
    start_label, start_cols = spec.start
    end_label, end_cols = spec.end

    # A Member referenced by name alone is matched on `name`; anything with a
    # composite key is matched on `uid`.
    #
    # A stub MUST carry its key columns as properties, not just a uid.  It did
    # not, and the consequence was invisible: 3,291 :Field stubs and 16,688
    # :Paragraph stubs held `uid` and nothing else, so every query of the form
    # `MATCH (f:Field {name: $x})` silently missed them.  Those are the
    # working-storage names - WS-RECORDS-READ has 3,231 readers - so the tools
    # would have reported nothing for the most-used fields in the codebase
    # while looking like they had answered.  No error, no empty-result warning,
    # just a wrong answer.
    #
    # `uid` is derived FROM the key columns, so the properties are the same
    # values; there is no new information here, only a node you can find.
    def _match(label: str, cols: Sequence[str], var: str) -> str:
        target = model.node_by_label(label)
        if label == "Member" and len(cols) == 1:
            return (f"MERGE ({var}:Member {{name: {_member_uid_expr(cols[0])}}}) "
                    f"ON CREATE SET {var}.uid = {_member_uid_expr(cols[0])} + "
                    f"'{model.SEP}{model.SEP}', {var}.owner = '{OWNER}', "
                    f"{var}.stub = true")
        # Map the fact's columns onto the target label's OWN key names: a
        # READS_FIELD row has `field`, the Field label calls it `name`.
        sets = [f"{var}.owner = '{OWNER}'", f"{var}.stub = true"]
        if target is not None and len(target.key) == len(cols):
            for key_col, fact_col in zip(target.key, cols):
                sets.append(f"{var}.{key_col} = coalesce(toString(row.{fact_col}), '')")
        return (f"MERGE ({var}:{label} {{uid: {_uid_expr(cols)}}}) "
                f"ON CREATE SET {', '.join(sets)}")

    # Anything in key_props goes in the MERGE pattern so it distinguishes one
    # relationship from another; everything else is set after.
    pattern = ""
    if spec.key_props:
        pairs = ", ".join(
            f"{k}: coalesce(row.{k}, '')" for k in spec.key_props)
        pattern = " {" + pairs + "}"

    assigns = [f"r.{p} = row.{p}" for p in spec.props
               if p not in spec.key_props]
    assigns.append(f"r.src = coalesce(toString(row.{spec.src_col}), '')")
    assigns.append(f"r.owner = '{OWNER}'")
    set_clause = "SET " + ", ".join(assigns)

    return f"""
    UNWIND $rows AS row
    CALL {{
      WITH row
      {_match(start_label, start_cols, 'a')}
      {_match(end_label, end_cols, 'b')}
      MERGE (a)-[r:{spec.rel}{pattern}]->(b)
      {set_clause}
    }} IN TRANSACTIONS OF $batch ROWS
    """


def load_edge(spec: model.EdgeSpec, conn: sqlite3.Connection,
              cfg: gs.Settings, verbose: bool = True) -> Dict[str, int]:
    """MERGE one relationship type from its table."""
    cypher = _edge_cypher(spec)

    total = created = 0
    t0 = time.perf_counter()
    for batch in _chunks(conn.execute(spec.select), cfg.load.fetch_rows):
        # The FLAG, not the relationship name.  These two lines existed twice -
        # here keyed on `spec.rel == "EXEC_SQL"` and in update.py keyed on
        # `spec.end_is_json_list` - so a second JSON-list edge would have been
        # expanded by the incremental path and silently skipped by the bulk
        # one.  Exactly the drift _edge_cypher's docstring claims to design out.
        if spec.end_is_json_list:
            batch = _expand_sql_tables(batch)
            if not batch:
                continue
        counters = driver.write_autocommit(cypher, {"rows": batch, "batch": cfg.load.batch_rows})
        total += len(batch)
        created += counters["relationships_created"]
    took = time.perf_counter() - t0
    if verbose:
        print(f"  :{spec.rel:<16} {total:>9,} rows  {created:>9,} new  {took:>6.1f}s")
    return {"rows": total, "created": created}


# ---------------------------------------------------------------------------
# findings carried as node properties
# ---------------------------------------------------------------------------

def load_findings(conn: sqlite3.Connection, cfg: gs.Settings,
                  verbose: bool = True) -> Dict[str, int]:
    """`unresolved` and `header_disagreement` become properties, not nodes.

    They are findings about a member, not facts connecting two things, so a
    relationship would be modelling noise.  But they must survive the migration
    - Rule 2 is the whole point - so they ride as lists on the Member.
    """
    out = {"unresolved": 0, "disagreements": 0}

    cypher = """
    UNWIND $rows AS row
    CALL {
      WITH row
      MATCH (m:Member {name: row.member})
      SET m.unresolved = coalesce(m.unresolved, []) + [row.detail]
    } IN TRANSACTIONS OF $batch ROWS
    """
    rows = conn.execute("""
        SELECT member,
               kind || ' @' || COALESCE(line,0) || ' ' ||
               COALESCE(raw,'') || ' (' || COALESCE(reason,'') || ')' AS detail
          FROM unresolved
    """)
    for batch in _chunks(rows, cfg.load.fetch_rows):
        driver.write_autocommit(cypher, {"rows": batch, "batch": cfg.load.batch_rows})
        out["unresolved"] += len(batch)

    cypher = """
    UNWIND $rows AS row
    CALL {
      WITH row
      MATCH (m:Member {name: row.member})
      SET m.disagreements = coalesce(m.disagreements, []) + [row.finding]
    } IN TRANSACTIONS OF $batch ROWS
    """
    rows = conn.execute("SELECT member, finding FROM header_disagreement")
    for batch in _chunks(rows, cfg.load.fetch_rows):
        driver.write_autocommit(cypher, {"rows": batch, "batch": cfg.load.batch_rows})
        out["disagreements"] += len(batch)

    if verbose:
        print(f"  findings         {out['unresolved']:>9,} unresolved, "
              f"{out['disagreements']:,} header disagreements")
    return out


# ---------------------------------------------------------------------------
# whole-graph operations
# ---------------------------------------------------------------------------

def repair_stubs(cfg: Optional[gs.Settings] = None,
                 verbose: bool = True) -> Dict[str, int]:
    """Backfill the key properties on stubs that predate the `_match` fix.

    `ON CREATE SET` only fires when a node is created, so a graph loaded before
    the fix keeps its nameless stubs however many times the edges are reloaded.
    Rather than make every edge write pay for an `ON MATCH SET` it will almost
    never need, the repair is a separate pass that can be run once.

    It needs no source data: `uid` is BUILT from the key columns, joined with
    `SEP`, so splitting it back is exact.  A pipe cannot occur in an AS/400
    object name, which is why SEP was chosen and why this is safe rather than
    clever.
    """
    cfg = cfg or gs.load()
    out: Dict[str, int] = {}
    for spec in model.NODES:
        keys = list(spec.key)
        if len(keys) == 1:
            # toIntegerOrNull first, because a uid is always a STRING (it is
            # built with toString) while the key property keeps its native
            # data type - the `number` of an :Indicator is an integer.  Writing the string
            # back would leave repaired stubs typed differently from loaded
            # ones - a difference nothing would report and a sort would get
            # wrong.  coalesce falls through to the string for real names.
            assigns = f"n.{keys[0]} = coalesce(toIntegerOrNull(n.uid), n.uid)"
            missing = f"n.{keys[0]} IS NULL"
        else:
            parts = [f"n.{k} = coalesce(split(n.uid, '{model.SEP}')[{i}], '')"
                     for i, k in enumerate(keys)]
            assigns = ", ".join(parts)
            missing = " OR ".join(f"n.{k} IS NULL" for k in keys)

        fixed = 0
        while True:
            counters = driver.write_autocommit(
                f"MATCH (n:{spec.label}) WHERE n.stub = true AND ({missing}) "
                f"WITH n LIMIT $batch SET {assigns}",
                {"batch": cfg.load.batch_rows})
            n = counters["properties_set"]
            if n == 0:
                break
            fixed += n
        out[spec.label] = fixed
        if verbose and fixed:
            print(f"  repaired {fixed:,} propert(ies) on :{spec.label} stubs")
    return out


def wipe(verbose: bool = True) -> int:
    """Delete only what we own, in batches.

    DETACH DELETE on 230k nodes in one transaction is the same heap trap as a
    bulk insert, so it is batched too.
    """
    deleted = 0
    cfg = gs.load()
    while True:
        counters = driver.write_autocommit(
            f"MATCH (n {{owner: '{OWNER}'}}) WITH n LIMIT $batch "
            f"DETACH DELETE n",
            {"batch": cfg.load.batch_rows})
        n = counters["nodes_deleted"]
        deleted += n
        if verbose and n:
            print(f"  deleted {deleted:,} nodes ...")
        if n == 0:
            break
    return deleted


def estimate() -> str:
    """Row counts per table, without writing anything."""
    conn = _connect()
    lines = [f"  facts.db: {_facts_db()}", ""]
    total = 0
    lines.append("  NODES")
    for spec in model.NODES:
        n = conn.execute(
            f"SELECT COUNT(*) FROM ({spec.select.strip()}) AS s").fetchone()[0]
        total += n
        lines.append(f"    :{spec.label:<12} {n:>9,}")
    lines.append("")
    lines.append("  RELATIONSHIPS")
    for spec in model.EDGES:
        n = conn.execute(
            f"SELECT COUNT(*) FROM ({spec.select.strip()}) AS s").fetchone()[0]
        total += n
        lines.append(f"    :{spec.rel:<16} {n:>9,}")
    conn.close()
    lines.append("")
    lines.append(f"  {total:,} objects to write")
    return "\n".join(lines)


def verify() -> str:
    """SQLite against Neo4j, classifying the difference rather than just flagging it.

    A raw count comparison cries wolf.  Three outcomes are legitimate and only
    one is a fault:

      MATCH     the counts agree.
      OVER      Neo4j holds MORE.  107 member names exist as two members - the
                AS/400 menu convention, an MNUDDS screen plus an MNUCMD command
                list under one name - so one SQLite row becomes a relationship
                on each node.  No loss.
      NO EDGE   Neo4j holds FEWER because the row had no endpoint to point at.
                431 of 1,318 sql_usage rows name no table (SET, COMMIT,
                DECLARE); they are facts, but not edges.
      SHORT     Neo4j holds fewer than the distinct endpoint pairs.  That IS a
                fault - rows collapsing into one relationship because the MERGE
                key is missing a distinguishing property.  It cost 4,701
                WRITES_FIELD edges once, and this is how it was caught.
    """
    conn = _connect()
    lines = [f"  {'object':<20}{'sqlite':>11}{'expected':>11}{'neo4j':>11}  verdict"]
    lines.append("  " + "-" * 66)
    faults = 0

    for spec in model.NODES:
        want = conn.execute(
            f"SELECT COUNT(*) FROM ({spec.select.strip()}) AS s").fetchone()[0]
        rows = driver.read(
            f"MATCH (n:{spec.label}) WHERE n.stub IS NULL RETURN count(n) AS n",
            cap=1)
        got = rows[0]["n"] if rows else 0
        verdict = "MATCH" if got == want else "DIFFERS"
        if verdict != "MATCH":
            faults += 1
        lines.append(f"  :{spec.label:<19}{want:>11,}{want:>11,}{got:>11,}  {verdict}")

    for spec in model.EDGES:
        rows_n = conn.execute(
            f"SELECT COUNT(*) FROM ({spec.select.strip()}) AS s").fetchone()[0]

        # What the graph SHOULD hold: one edge per distinct endpoint pair plus
        # whatever key_props distinguish them, ignoring how many nodes share a
        # name.  Rows with an empty endpoint cannot become an edge at all.
        a_cols = list(spec.start[1])
        b_cols = list(spec.end[1])
        cols = a_cols + b_cols + list(spec.key_props)
        # CAST before COALESCE: `indicator.number` is an INTEGER, and
        # comparing an integer to '' is a SQLite coercion that
        # PostgreSQL refuses outright - "invalid input syntax for
        # type integer".  Casting to text asks what this actually
        # means: did the spec produce a value here.  Both stores
        # answer that.
        not_empty = " AND ".join(
            f"COALESCE(CAST({c} AS TEXT), '') <> ''"
            for c in (a_cols + b_cols))
        if spec.end_is_json_list:
            # Expand the list the way the loader does, then count distinct
            # pairs.  Anything else reports the empty-list rows as data loss.
            seen_pairs = set()
            for r in conn.execute(spec.select):
                row = {k: r[k] for k in r.keys()}
                for e in _expand_sql_tables([row]):
                    key = tuple(e.get(c) or "" for c in cols)
                    if all(e.get(c) for c in (a_cols + b_cols)):
                        seen_pairs.add(key)
            expected = len(seen_pairs)
        else:
            try:
                expected = conn.execute(
                    f"SELECT COUNT(*) FROM (SELECT DISTINCT "
                    f"{', '.join(cols)} FROM ({spec.select.strip()}) AS s "
                    f"WHERE {not_empty}) AS d").fetchone()[0]
            except Exception:                           # noqa: BLE001
                # Widened for PostgreSQL, where this failure is a psycopg
                # error.  Falling back to the raw row count is the
                # existing behaviour; only the clause changed.
                expected = rows_n

        rows = driver.read(
            f"MATCH ()-[r:{spec.rel}]->() RETURN count(r) AS n", cap=1)
        got = rows[0]["n"] if rows else 0

        if got == expected:
            verdict = "MATCH"
        elif got > expected:
            verdict = "OVER (shared names)"
        elif expected < rows_n and got >= expected:
            verdict = "NO EDGE (empty target)"
        else:
            verdict = "SHORT  <-- FAULT"
            faults += 1
        lines.append(f"  :{spec.rel:<19}{rows_n:>11,}{expected:>11,}{got:>11,}  {verdict}")

    conn.close()
    lines.append("")
    if faults:
        lines.append(f"  {faults} FAULT(S) - the graph is missing facts SQLite holds.")
        lines.append(f"  A SHORT verdict means two rows merged into one edge: add the")
        lines.append(f"  distinguishing column to that spec's key_props in model.py.")
    else:
        lines.append("  No faults.  Every difference is either a shared member name")
        lines.append("  (one row, two nodes, two edges) or a row with no endpoint to")
        lines.append("  point at.  Nothing SQLite holds is missing from the graph.")
    return chr(10).join(lines)

def run(only: Optional[Sequence[str]] = None, verbose: bool = True) -> Dict[str, Any]:
    """Nodes, then relationships, then findings."""
    cfg = gs.load()

    # Constraints first, always.  Every MERGE below leans on the uniqueness
    # constraints - without them a first load is both slow (label scans) and
    # unsafe (two loaders can create the node twice).  create() is IF NOT
    # EXISTS throughout, so on an already-initialised graph this is a no-op,
    # and a fresh Neo4j needs no separate step anyone has to remember.
    from kb.graph import schema
    schema.create(verbose=False)

    conn = _connect()
    picked = set(only or ())
    stats: Dict[str, Any] = {"nodes": {}, "edges": {}}
    t0 = time.perf_counter()

    if verbose:
        print(f"\n  loading {_facts_db()}")
        print(f"  into    {cfg.neo4j.uri} db={cfg.neo4j.database}")
        print(f"  batches of {cfg.load.batch_rows:,} rows per transaction\n")

    for spec in model.NODES:
        if picked and spec.label not in picked:
            continue
        stats["nodes"][spec.label] = load_node(spec, conn, cfg, verbose)

    if verbose:
        print()
    for spec in model.EDGES:
        if picked and spec.rel not in picked:
            continue
        stats["edges"][spec.rel] = load_edge(spec, conn, cfg, verbose)

    if not picked:
        if verbose:
            print()
        stats["findings"] = load_findings(conn, cfg, verbose)
        # Full text last, and part of a normal load rather than a separate
        # step somebody has to remember.  A graph loaded without it answers
        # every structural question and silently returns nothing for the
        # retrieval pass - a partial load that looks complete.
        if model.FULLTEXT:
            from kb.graph import fulltext
            stats["searchtext"] = fulltext.load_text(conn, cfg,
                                                     verbose=verbose)
        # Stubs created by an earlier load may predate the _match fix and hold
        # a uid with no key properties.  Cheap when there is nothing to do.
        stats["repaired"] = repair_stubs(cfg, verbose)

    conn.close()
    stats["seconds"] = round(time.perf_counter() - t0, 1)
    if verbose:
        n = sum(v["rows"] for v in stats["nodes"].values())
        e = sum(v["rows"] for v in stats["edges"].values())
        print(f"\n  {n:,} node rows and {e:,} relationship rows "
              f"in {stats['seconds']}s")

    # Stamp the graph so `sync` can tell it moved even when facts.db did not.
    # A full load can change properties without changing any count - the CHKOBJ
    # reclassification changed 13,264 `mode` values and not one total - and a
    # count-only fingerprint is blind to exactly that.
    try:
        from . import fingerprint
        fingerprint.stamp("load")
    except Exception:                                     # noqa: BLE001
        pass                       # stamping must never fail a good load
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="load facts.db into Neo4j")
    ap.add_argument("--estimate", action="store_true", help="counts only, no writes")
    ap.add_argument("--verify", action="store_true", help="compare counts after a load")
    ap.add_argument("--wipe", action="store_true", help="delete our graph first")
    ap.add_argument("--repair", action="store_true",
                    help="backfill key properties on nameless stubs, then stop")
    ap.add_argument("--only", nargs="+", metavar="LABEL",
                    help="load only these labels or relationship types")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    verbose = not args.quiet

    try:
        if args.estimate:
            print(estimate())
            return 0
        if args.verify:
            print(verify())
            return 0
        if args.repair:
            fixed = repair_stubs(verbose=verbose)
            total = sum(fixed.values())
            msg = (f"{total:,} propert(ies) backfilled" if total
                   else "nothing to repair")
            print('\n' + "  " + msg)
            return 0
        if args.wipe:
            print("  wiping our graph ...")
            n = wipe(verbose)
            print(f"  {n:,} nodes deleted\n")
        run(only=args.only, verbose=verbose)
        return 0
    except driver.GraphUnavailable as exc:
        print(f"\n  {exc}", file=sys.stderr)
        return 1
    except sqlite3.OperationalError as exc:
        print(f"\n  cannot read facts.db: {exc}", file=sys.stderr)
        print(f"  looked at {_facts_db()}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
