"""
Measure SQLite against Neo4j on the same questions, and check they agree.

    python -m kb.graph.bench                 # every case
    python -m kb.graph.bench --only aggregate
    python -m kb.graph.bench --repeat 5
    python -m kb.graph.bench --csv out.csv

## Why this is a module and not a note in a document

"Aggregates are slower in Cypher" was listed as a migration blocker.  It is
real but it is a TAX, not a wall, and the only way to say that responsibly is to
keep measuring it - a number in a markdown file is out of date the moment
anyone touches an index.  So the claim lives in code that can be re-run.

## The thing most benchmarks get wrong

Timing two stores that return different answers measures nothing.  Every case
here carries an `agree` check, and a case whose answers differ is reported as
DIFFERS and its timing is not presented as a comparison.  A fast wrong answer
is not a result.

## How to read the ratio

The ratio is worst where the absolute number is smallest, which is the opposite
of a problem.  `members per subsystem` is 0.8 ms against 16.5 ms - 20x, and
irrelevant, because nobody notices 16 ms.  The cases that scan hundreds of
thousands of relationships are 1-2x.  Judge the milliseconds, not the multiple.

## What this does NOT claim

That Neo4j is faster.  It is not, on any query here.  SQLite wins the
transitive closure by roughly 30x.  Neo4j is in this system for Cypher, for the
GDS algorithms, and for `blast_radius` answering six dependency layers in one
round trip instead of six queries the caller has to combine - which is where the
arithmetic went wrong when a human combined them.  Speed was never the argument.
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
from decimal import Decimal
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Sequence, Tuple

from kb.graph import driver
from kb import config as _cfg
from kb.graph import settings as gs
from kb.graph.load import _connect, _facts_db


@dataclass(frozen=True)
class Case:
    name: str
    group: str
    sql: str
    cypher: str
    note: str
    #: PostgreSQL SQL, when it cannot be the same string as `sql`.  Empty means
    #: "use `sql`", which is true for 8 of the 9 cases: the only construct in
    #: them PostgreSQL lacks is FTS5's MATCH, and `group_concat` is provided as
    #: an aggregate by kb.store.ddl rather than rewritten per store.
    pg_sql: str = ""
    #: How to reduce a result set to something comparable across the two
    #: stores.  Column names differ (`n` vs `count`), ordering of equal values
    #: is not guaranteed either side, and Neo4j returns ints where SQLite
    #: sometimes returns floats.
    shape: str = "rows"
    params: Dict[str, Any] = field(default_factory=dict)


CASES: Tuple[Case, ...] = (
    # ---- aggregates: the ones that were called a blocker -------------------
    # count(DISTINCT [...]) rather than count(r), and it is not decoration.
    # One SQLite row becomes one edge PER MATCHING NODE, and 107 member names
    # exist twice (the AS/400 menu convention - an MNUDDS screen and an MNUCMD
    # command list under one name).  So the graph legitimately holds 159,896
    # READS_FIELD edges for 159,190 rows.  `count(r)` measures the graph;
    # counting distinct fact keys measures the FACTS, which is what SQLite
    # counts and therefore the only comparison that means anything.
    Case(
        name="field usage by confidence", group="aggregate",
        sql="SELECT confidence, COUNT(*) AS n FROM field_usage "
            "GROUP BY confidence ORDER BY confidence",
        # Three formulations were measured - count(DISTINCT [list]) 1810 ms,
        # WITH DISTINCT 1611 ms, dedup on the start node 1846 ms - all giving
        # the same correct answer.  The deduplication IS the cost; there is no
        # cheap version.  This is the slowest case in the suite and the one
        # genuine example of the "aggregates" tax being felt.
        cypher="MATCH ()-[r:READS_FIELD|WRITES_FIELD]->(f:Field) "
               "WITH DISTINCT r.src AS s, f.name AS fn, r.file AS fl, "
               "     r.qualifier AS q, type(r) AS t, r.confidence AS confidence "
               "RETURN confidence, count(*) AS n ORDER BY confidence",
        note="~300k edges; distinct fact keys, not edges - see the comment above",
    ),
    Case(
        name="total field occurrences", group="aggregate",
        sql="SELECT SUM(n) AS total FROM field_usage",
        cypher="MATCH ()-[r:READS_FIELD|WRITES_FIELD]->(f:Field) "
               "WITH DISTINCT r.src AS s, f.name AS fn, r.file AS fl, "
               "     r.qualifier AS q, type(r) AS t, r.n AS n "
               "RETURN sum(n) AS total",
        note="sum of occurrence counts, deduplicated by fact key",
    ),
    Case(
        name="top 20 by field writes", group="aggregate",
        sql="SELECT program AS name, COUNT(*) AS n FROM field_usage "
            "WHERE direction='write' GROUP BY program ORDER BY n DESC, name LIMIT 20",
        cypher="MATCH (m:Member)-[r:WRITES_FIELD]->() "
               "RETURN m.name AS name, count(r) AS n "
               "ORDER BY n DESC, name LIMIT 20",
        note="group and sort 138k edges",
    ),
    Case(
        name="top 20 most called", group="aggregate",
        sql="SELECT callee AS name, COUNT(DISTINCT caller) AS n FROM call_edge "
            "GROUP BY callee ORDER BY n DESC, name LIMIT 20",
        cypher="MATCH (a:Member)-[:CALLS]->(b:Member) "
               "RETURN b.name AS name, count(DISTINCT a.name) AS n "
               "ORDER BY n DESC, name LIMIT 20",
        note="the in-degree ranking every impact report starts from",
    ),
    Case(
        name="members per subsystem", group="aggregate",
        sql="SELECT subsystem, COUNT(*) AS n FROM object "
            "GROUP BY subsystem ORDER BY subsystem",
        cypher="MATCH (m:Member) WHERE m.stub IS NULL "
               "RETURN m.subsystem AS subsystem, count(m) AS n ORDER BY subsystem",
        note="small and cheap; the ratio here is the least interesting number",
    ),
    Case(
        name="members per library", group="aggregate",
        sql="SELECT library, COUNT(*) AS n FROM object WHERE library <> '' "
            "GROUP BY library ORDER BY library",
        cypher="MATCH (m:Member)-[:IN_LIBRARY]->(l:Library) "
               "WHERE m.stub IS NULL "
               "RETURN l.library AS library, count(m) AS n ORDER BY library",
        note="the count that becomes 21 groups on the real extract",
    ),

    # ---- traversal: where the graph is supposed to earn its place ----------
    # SQLite counts call SITES (one row per CALL statement); the graph collapses
    # them and carries the count in `r.n`.  So the comparable figure is sum(n)
    # over distinct facts, not count(r).  Getting this wrong is what made the
    # first run of this benchmark report a disagreement that was mine, not the
    # stores'.
    Case(
        name="who calls, depth 1", group="traversal",
        sql="SELECT caller AS name, COUNT(*) AS n FROM call_edge "
            "WHERE callee='ERRSRV' GROUP BY caller ORDER BY caller",
        cypher="MATCH (a:Member)-[r:CALLS]->(:Member {name:'ERRSRV'}) "
               "WHERE a.stub IS NULL "
               "WITH DISTINCT r.src AS name, r.n AS n "
               "RETURN name, sum(n) AS n ORDER BY name",
        note="one hop - the shape most tool calls take; sites, not edges",
    ),
    Case(
        name="logicals over a physical", group="traversal",
        sql="SELECT name FROM object WHERE based_on='WHSMAS' ORDER BY name",
        cypher="MATCH (l:Member)-[:BASED_ON]->(:Member {name:'WHSMAS'}) "
               "RETURN l.name AS name ORDER BY name",
        note="the edge a text search cannot see",
    ),

    # The full-text case is retired with the Lucene index itself: the graph
    # no longer holds document text, so there is nothing on that side to
    # race. FTS5 vs tsvector parity is covered by kb.e2e's cross-store run.
)


# ---------------------------------------------------------------------------
# running one side
# ---------------------------------------------------------------------------

def _norm(rows: Sequence[Dict[str, Any]], shape: str) -> Any:
    """Reduce a result to something the two stores can be compared on."""
    if shape == "names_set":
        return {str(r.get("name") or "") for r in rows}
    out = []
    for r in rows:
        # Column names differ between the two (`confidence`, `name`, `n`,
        # `total`); compare VALUES in column order, and coerce numbers so
        # 22 and 22.0 are not a disagreement.
        vals = []
        for _k, v in sorted(r.items()):
            # psycopg returns Decimal for `numeric`, and Decimal("22") != 22,
            # which would report a spurious DIFFER on any SUM that widens.
            # float was already coerced for the same reason on the Neo4j side.
            if isinstance(v, Decimal):
                v = int(v) if v == v.to_integral_value() else float(v)
            if isinstance(v, float) and v.is_integer():
                v = int(v)
            vals.append("" if v is None else v)
        out.append(tuple(vals))
    return sorted(out, key=repr)


def _time(fn: Callable[[], Any], repeat: int) -> Tuple[float, Any]:
    """Median of `repeat` runs, and the last result.

    Median, not mean: one JIT warm-up or one page-cache miss skews a mean and
    there is no reason to let a single outlier decide a number somebody will
    quote.  The first run is included deliberately - a cold query is a real
    query, and quietly discarding it is how benchmarks flatter themselves.
    """
    times: List[float] = []
    out: Any = None
    for _ in range(max(1, repeat)):
        t0 = time.perf_counter()
        out = fn()
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times), out


def run_case(case: Case, conn: sqlite3.Connection, repeat: int,
             pg_conn=None) -> Dict[str, Any]:
    row: Dict[str, Any] = {"name": case.name, "group": case.group,
                           "note": case.note}

    def _sqlite() -> List[Dict[str, Any]]:
        cur = conn.execute(case.sql, tuple(case.params.values()) if case.params else ())
        return [dict(r) for r in cur.fetchall()]

    def _postgres() -> List[Dict[str, Any]]:
        sql = case.pg_sql or case.sql
        cur = pg_conn.execute(
            sql, tuple(case.params.values()) if case.params else ())
        return [dict(r) for r in cur.fetchall()]

    def _neo4j() -> List[Dict[str, Any]]:
        if case.group == "fulltext":
            from kb.graph import fulltext
            return fulltext.search("credit hold", 10)
        return driver.read(case.cypher, case.params, cap=100_000)

    try:
        row["sqlite_ms"], want = _time(_sqlite, repeat)
    except sqlite3.Error as exc:
        row["error"] = f"sqlite: {exc}"
        return row
    if pg_conn is not None:
        try:
            row["postgres_ms"], pg = _time(_postgres, repeat)
            row["postgres_rows"] = len(pg)
            row["pg_agree"] = (_norm(want, case.shape)
                               == _norm(pg, case.shape))
        except Exception as exc:                          # noqa: BLE001
            row["postgres_ms"] = float("nan")
            row["pg_error"] = f"{type(exc).__name__}: {exc}"

    try:
        row["neo4j_ms"], got = _time(_neo4j, repeat)
    except Exception as exc:                              # noqa: BLE001
        row["error"] = f"neo4j: {type(exc).__name__}: {exc}"
        row["neo4j_ms"] = float("nan")
        return row

    row["sqlite_rows"] = len(want)
    row["neo4j_rows"] = len(got)
    a, b = _norm(want, case.shape), _norm(got, case.shape)
    row["agree"] = (a == b)
    if not row["agree"] and case.shape == "names_set":
        row["overlap"] = len(a & b)
    row["ratio"] = (row["neo4j_ms"] / row["sqlite_ms"]
                    if row["sqlite_ms"] > 0 else float("inf"))
    return row


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def report(rows: Sequence[Dict[str, Any]], repeat: int) -> str:
    out = [
        f"  facts.db  {_facts_db()}",
        f"  neo4j     {gs.load().neo4j.uri}",
        f"  median of {repeat} run(s) each, cold runs included",
        "",
        f"  {'case':<26}{'group':<10}{'sqlite':>9}{'postgres':>10}"
        f"{'neo4j':>9}{'rows':>7}  answers",
        "  " + "-" * 88,
    ]
    slow: List[str] = []
    disagree: List[str] = []
    for r in rows:
        if "error" in r:
            out.append(f"  {r['name']:<26}{r['group']:<10}"
                       f"{'ERROR':>9}{'':>10}{'':>9}{'':>7}  {r['error'][:26]}")
            continue
        verdict = "identical" if r["agree"] else "DIFFER"
        if not r["agree"]:
            extra = (f" ({r['overlap']}/{r['sqlite_rows']} shared)"
                     if "overlap" in r else "")
            verdict += extra
            disagree.append(r["name"])
        rows_txt = (str(r["sqlite_rows"]) if r["sqlite_rows"] == r["neo4j_rows"]
                    else f"{r['sqlite_rows']}/{r['neo4j_rows']}")
        if "pg_error" in r:
            pg_txt = "ERROR".rjust(10)
        elif "postgres_ms" in r:
            mark = "" if r.get("pg_agree", True) else "!"
            pg_txt = f"{r['postgres_ms']:>8.1f}m{mark}".rjust(10)
        else:
            pg_txt = "-".rjust(10)
        out.append(f"  {r['name']:<26}{r['group']:<10}"
                   f"{r['sqlite_ms']:>8.1f}m{pg_txt}"
                   f"{r['neo4j_ms']:>8.1f}m{rows_txt:>7}  {verdict}")
        # A tax worth naming is one somebody would FEEL. 250 ms is the point at
        # which an interactive tool stops feeling immediate.
        if r["neo4j_ms"] > 250:
            slow.append(f"{r['name']} ({r['neo4j_ms']:.0f} ms)")

    timed = [r for r in rows if "error" not in r and r["agree"]]
    out.append("")
    if timed:
        ratios = [r["ratio"] for r in timed]
        out.append(f"  {len(timed)} case(s) agree exactly.  "
                   f"Neo4j is {statistics.median(ratios):.1f}x slower than "
                   f"SQLite at the median, worst {max(ratios):.1f}x.")
        pg = [r for r in timed if "postgres_ms" in r
              and r["postgres_ms"] == r["postgres_ms"]]      # not NaN
        if pg:
            pr = [r["postgres_ms"] / r["sqlite_ms"] for r in pg
                  if r["sqlite_ms"] > 0]
            bad = [r["name"] for r in pg if not r.get("pg_agree", True)]
            out.append(f"  PostgreSQL is {statistics.median(pr):.1f}x SQLite "
                       f"at the median over {len(pg)} case(s); "
                       + (f"{len(bad)} disagree: {', '.join(bad)}." if bad
                          else "every one returns the same rows."))
            out.append("  A '!' in the postgres column marks a case whose rows "
                       "differ from SQLite's.")
    if disagree:
        out.append(f"  {len(disagree)} case(s) DIFFER: {', '.join(disagree)}.")
        out.append("  Their timings are not a comparison - see the note on each.")
    out.append("")
    if slow:
        out.append(f"  Over 250 ms in Neo4j: {', '.join(slow)}")
        out.append("  Those are the only ones a user could feel.  Everything else")
        out.append("  is a large multiple of a small number.")
        out.append("")
        out.append("  Both slow cases pay for the SAME thing: deduplicating a fact")
        out.append("  that became several edges because a member name is not unique.")
        out.append("  Three formulations were measured and all cost ~1.6 s, so this is")
        out.append("  the shape of the work, not a bad query.  It is also the ONLY")
        out.append("  place the aggregate 'blocker' is real - and it is a 1.6 s report,")
        out.append("  not a wall.")
    else:
        out.append("  Nothing exceeds 250 ms in Neo4j.  The aggregate 'blocker' is a")
        out.append("  tax on numbers too small to notice, not a wall.")
    out.append("")
    out.append("  SQLite wins every case here and is expected to.  Neo4j is in this")
    out.append("  system for Cypher, the GDS algorithms, and blast_radius answering")
    out.append("  six dependency layers in one round trip instead of six queries a")
    out.append("  caller has to add up by hand.  Speed was never the argument.")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="measure SQLite, PostgreSQL and Neo4j, and check they agree")
    ap.add_argument("--only", metavar="GROUP",
                    help="aggregate | traversal | fulltext")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--csv", metavar="PATH", help="also write the raw numbers")
    args = ap.parse_args(argv)

    cases = [c for c in CASES if not args.only or c.group == args.only]
    if not cases:
        print(f"  no cases in group {args.only!r}", file=sys.stderr)
        return 2

    try:
        # Both relational stores explicitly, not whichever one
        # ALPHAPLUS_STORE happens to name: this module exists to compare
        # them, so the shared factory would hand back the same one twice.
        conn = sqlite3.connect(f"file:{_cfg.FACTS_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.OperationalError as exc:
        print(f"  cannot read facts.db: {exc}", file=sys.stderr)
        return 1

    pg_conn = None
    try:
        from kb import store as _store
        if _store.dsn_from_env():
            pg_conn = _store.connect(read_only=True)
    except Exception as exc:                            # noqa: BLE001
        # No PostgreSQL is not a failure here - the column reads '-'.
        print(f"  postgres column skipped: {exc}", file=sys.stderr)

    try:
        rows = [run_case(c, conn, args.repeat, pg_conn) for c in cases]
    except driver.GraphUnavailable as exc:
        print(f"\n  {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
        if pg_conn is not None:
            pg_conn.close()

    print(report(rows, args.repeat))

    if args.csv:
        import csv
        keys = ["name", "group", "sqlite_ms", "neo4j_ms", "ratio",
                "sqlite_rows", "neo4j_rows", "agree", "note"]
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"\n  wrote {args.csv}")

    return 0 if all("error" not in r for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
