"""
Check the graph for the faults that have actually happened.

    python -m kb.graph.selftest
    python -m kb.graph.selftest --quiet     # exit code only, for CI

Every check here exists because the thing it checks for went wrong once and was
invisible when it did.  None of them is hypothetical, and each names what it
cost, so nobody has to guess whether a failure matters.

Run it after any load, any `kb.graph.update`, and before believing a tool.
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable, List, Tuple

from kb.graph import driver, model
from kb.graph import settings as gs


class Check:
    def __init__(self, name: str, why: str, fn: Callable[[], Tuple[bool, str]]):
        self.name, self.why, self.fn = name, why, fn


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------

def _stub_keys_present() -> Tuple[bool, str]:
    """Every stub carries its key properties, not just a uid.

    COST WHEN THIS FAILED: 3,291 :Field and 16,688 :Paragraph stubs held a uid
    and nothing else, because `ON CREATE SET` never set the key columns.  Every
    query of the form `MATCH (f:Field {name: $x})` silently missed them - and
    those are the working-storage names, so `where_is_field_used` returned
    NOTHING for WS-RECORDS-READ, which 3,231 programs read.  No error.  Fixed in
    `load._edge_cypher`; `load.repair_stubs` backfills an old graph.
    """
    bad: List[str] = []
    for spec in model.NODES:
        missing = " OR ".join(f"n.{k} IS NULL" for k in spec.key)
        n = driver.read(
            f"MATCH (n:{spec.label}) WHERE n.stub = true AND ({missing}) "
            f"RETURN count(n) AS n", cap=1)[0]["n"]
        if n:
            bad.append(f"{spec.label}:{n:,}")
    if bad:
        return False, (f"stubs missing key properties: {', '.join(bad)}. "
                       f"Run `python -m kb.graph.load --repair`.")
    return True, "every stub carries its key properties"


def _every_edge_has_src() -> Tuple[bool, str]:
    """`r.src` is set on every relationship.

    COST IF THIS FAILS: `kb.graph.update` finds what to delete with
    `WHERE r.src = $member`.  An edge with no `src` can never be deleted, so it
    survives every incremental refresh forever - a stale fact that no reload
    removes and no error reports.
    """
    bad: List[str] = []
    for spec in model.EDGES:
        n = driver.read(
            f"MATCH ()-[r:{spec.rel}]->() WHERE r.src IS NULL OR r.src = '' "
            f"RETURN count(r) AS n", cap=1)[0]["n"]
        if n:
            bad.append(f"{spec.rel}:{n:,}")
    if bad:
        return False, f"relationships with no src: {', '.join(bad)}"
    return True, "every relationship carries src"


def _uid_matches_keys() -> Tuple[bool, str]:
    """A node's uid still agrees with its key properties.

    COST IF THIS FAILS: `uid` is what the uniqueness constraint and every MERGE
    seek on.  If a node's uid and its key properties disagree, the next load
    creates a SECOND node for the same thing and the counts quietly double.
    """
    # toString on both sides, because `load._uid_expr` builds the uid with
    # toString: :Indicator's key is an INTEGER, so uid is '3' while `number` is
    # 3.  That is not a fault - MERGE only ever seeks on uid - but comparing
    # them raw reported all 42 indicators as broken, which is a check crying
    # wolf about its own formulation.
    bad: List[str] = []
    for spec in model.NODES:
        if len(spec.key) == 1:
            expr = f"toString(n.{spec.key[0]})"
        else:
            expr = (" + '" + model.SEP + "' + ").join(
                f"toString(coalesce(n.{k}, ''))" for k in spec.key)
        n = driver.read(
            f"MATCH (n:{spec.label}) WHERE n.stub IS NULL AND n.uid <> ({expr}) "
            f"RETURN count(n) AS n", cap=1)[0]["n"]
        if n:
            bad.append(f"{spec.label}:{n:,}")
    if bad:
        return False, f"uid disagrees with key properties: {', '.join(bad)}"
    return True, "every uid agrees with its key properties"


def _constraints_present() -> Tuple[bool, str]:
    """One uniqueness constraint per label.

    COST IF THIS FAILS: without the constraint's backing index, MERGE has
    nothing to seek on and degrades to a label scan - the load goes quadratic.
    It is a performance cliff, not a wrong answer, which is why it needs
    checking rather than noticing.
    """
    have = {r["name"] for r in driver.read(
        "SHOW CONSTRAINTS YIELD name RETURN name", cap=1000)}
    want = {f"ap_{n.label.lower()}_uid" for n in model.NODES}
    missing = sorted(want - have)
    if missing:
        return False, (f"missing constraint(s): {', '.join(missing)}. "
                       f"Run `python -m kb.graph.schema`.")
    return True, f"all {len(want)} uniqueness constraints present"


def _no_text_in_graph() -> Tuple[bool, str]:
    """The graph holds keys and relationships - never document text.

    `search.body` used to be copied onto every :Member as `searchtext` and
    indexed by Lucene: 5.5 MB of text in two stores, searched by two engines
    that stem differently. The relational store answers full text alone now,
    and this check is what stops the copy quietly coming back - a property
    write is one loader line, and nothing else would notice.
    """
    rows = driver.read(
        "MATCH (m:Member) WHERE m.searchtext IS NOT NULL "
        "RETURN count(m) AS n", cap=1)
    n = rows[0]["n"] if rows else 0
    if n:
        return False, (f"{n:,} member(s) still carry searchtext - run "
                       f"`python -m kb.graph.schema` then reload, or REMOVE "
                       f"the property")
    idx = driver.read(
        "SHOW INDEXES YIELD name, type WHERE type = 'FULLTEXT' "
        "RETURN count(*) AS n", cap=1)
    if idx and idx[0]["n"]:
        return False, ("a FULLTEXT index still exists - "
                       "`python -m kb.graph.schema` drops it")
    return True, "no document text in the graph, no Lucene index over it"



def _mapped_is_honest() -> Tuple[bool, str]:
    """Every tool in `backend.MAPPED` is genuinely routed through the graph.

    COST WHEN THIS FAILED: `MAPPED` claimed 11 tools read Neo4j.  Five of them -
    who_touches_file, where_is_field_used, resolve_overrides, find_object,
    program_structure - were never wired, so `doctor` reported a migration that
    had not happened and anyone reading it drew the wrong conclusion about what
    was tested.
    """
    from kb.graph import backend
    gaps = backend.unrouted()
    if gaps:
        return False, (f"in MAPPED but never routed: {', '.join(gaps)}. "
                       f"Wire them, or move them to backend.NEEDS_WORK.")
    return True, f"all {len(backend.MAPPED)} mapped tools are routed"


def _every_table_accounted_for() -> Tuple[bool, str]:
    """Every table in facts.db is either mapped to the graph or declared as staying.

    COST WHEN THIS FAILED: Mahesh added `file_resolved` - which physical file a
    program really opens once an OVRDBF rename is applied - and the graph had no
    spec for it and `STAYS_IN_SQLITE` did not mention it.  `load --verify` still
    reported "No faults", because it only checks the tables it already knows
    about: a whole table can go missing and every existing check stays green.

    So the question is asked the other way round here - from facts.db towards
    the model, not from the model towards facts.db.  That is the only direction
    that catches something NEW.
    """
    import sqlite3
    from kb.graph.load import _connect
    try:
        conn = _connect()
    except Exception as exc:                            # noqa: BLE001
        # Exception, not sqlite3.OperationalError: with a DSN the failure is a
        # psycopg error and the narrow clause would let it escape as a crash
        # instead of a verdict.
        # NOT a pass.  This used to `return True, "skipped"`, which meant an
        # unreachable store turned the one check that looks from the store
        # TOWARDS the model into a green tick.  That was survivable while the
        # store was a file next to the source; with a DSN it means one typo in
        # a connection string silently disables new-table detection.  A check
        # that cannot run has not passed.
        return False, (f"cannot read the fact store, so table coverage is "
                       f"UNKNOWN, not clean ({exc})")
    from kb import store as kbstore
    postgres = kbstore.backend() == "postgres"
    try:
        if postgres:
            have = {r[0] for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = ?", (kbstore.schema_from_env(),))}
        else:
            have = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()

    # FTS5 shadow tables are implementation detail, not facts.  PostgreSQL has
    # none - its full text is a generated column plus a GIN index - so the
    # filter is a no-op there rather than being skipped, which keeps the two
    # branches producing the same SET and not just the same count.
    real = {t for t in have
            if not t.startswith("sqlite_")
            and not any(t == f"{base}_{suffix}"
                        for base in ("search", "search_summary")
                        for suffix in ("config", "content", "data", "docsize",
                                       "idx"))}
    mapped = ({n.table for n in model.NODES}
              | {e.table for e in model.EDGES}
              | {f.table for f in model.FULLTEXT})
    known = mapped | set(model.STAYS_IN_SQLITE)
    missing = sorted(real - known)
    if missing:
        return False, (f"table(s) in facts.db that the graph neither maps nor "
                       f"declares: {', '.join(missing)}. Add an EdgeSpec, or a "
                       f"line in model.STAYS_IN_SQLITE saying why not.")

    # And the other direction, which the first version did not ask.  A spec
    # naming a table that no longer exists reads every row of nothing and
    # loads zero edges, with no error: `real - known` cannot see it, because
    # the table is absent from exactly the set being searched.  Latent while
    # the model and the schema agree; live the moment `search` stops being a
    # table and becomes a column.
    phantom = sorted(known - real - set(model.STAYS_IN_SQLITE))
    if phantom:
        return False, (f"spec(s) naming a table the fact store does not have: "
                       f"{', '.join(phantom)}. The loader would read zero rows "
                       f"and report success.")
    return True, f"all {len(real)} fact tables mapped or declared, none phantom"


def _no_orphan_stubs() -> Tuple[bool, str]:
    """A warning, not a failure: stubs nothing points at any more.

    These are litter from a refresh - `kb.graph.update --prune` removes them.
    Reported rather than failed because they make no answer wrong; they only
    inflate counts.
    """
    total = 0
    for spec in model.NODES:
        total += driver.read(
            f"MATCH (n:{spec.label}) WHERE n.stub = true AND NOT (n)--() "
            f"RETURN count(n) AS n", cap=1)[0]["n"]
    if total:
        return True, (f"{total:,} orphaned stub(s) - harmless, "
                      f"`kb.graph.update --prune` clears them")
    return True, "no orphaned stubs"


def _writers_agree() -> Tuple[bool, str]:
    """The graph and the relational store must agree on who can change a file.

    They did not.  `blast_radius` counted a writer as a program whose OPEN
    said `O` or `U`, and `who_touches_file` counts one whose OPEN says so OR
    which issues WRITE, REWRITE or DELETE whatever the OPEN said.  On CUSMAS
    that is 3 against 5: CSC954 and CUS950 open the file `read` and rewrite it
    anyway, which valid COBOL cannot do and this codebase does.

    schema.sql states the rule - "any query asking 'what can change this file'
    must read both, or it drops 294 programs from the answer" - and
    backend.NEEDS_WORK records it for who_touches_file's Cypher.  Nothing
    recorded it for blast_radius, whose whole purpose is sizing a change, so
    the tool that exists to prevent an underestimate was producing one.

    Checked on the files where the two definitions can actually differ, so a
    tree with no such contradiction still exercises the comparison rather than
    passing vacuously.
    """
    from kb import store as kbstore
    from kb.graph import driver

    db = kbstore.facts()
    try:
        rows = db.execute(
            "SELECT file FROM file_usage "
            " WHERE mode='I' AND (verbs LIKE '%WRITE%' OR verbs LIKE '%DELETE%')"
            " GROUP BY file ORDER BY COUNT(*) DESC LIMIT 5").fetchall()
        files = [r[0] for r in rows]
        if not files:
            return True, "no file has a contradicting open - nothing to compare"

        bad = []
        for name in files:
            want = db.execute(
                "SELECT COUNT(DISTINCT program) FROM file_usage WHERE file=? "
                "  AND (mode IN ('O','U') OR verbs LIKE '%WRITE%' "
                "       OR verbs LIKE '%REWRITE%' OR verbs LIKE '%DELETE%')",
                (name,)).fetchone()[0]
            got = driver.read(
                "MATCH (w:Member)-[o:OPENS]->(m:Member {name: $n}) "
                "WHERE o.mode IN ['O','U'] OR o.verbs CONTAINS 'WRITE' "
                "   OR o.verbs CONTAINS 'DELETE' "
                "RETURN count(DISTINCT w) AS n", {"n": name}, cap=1)
            have = int(got[0]["n"]) if got else 0
            if have != want:
                bad.append(f"{name}: graph {have}, store {want}")
        if bad:
            return False, "; ".join(bad)
        return True, f"{len(files)} contradicting file(s) agree"
    finally:
        db.close()


CHECKS: Tuple[Check, ...] = (
    Check("constraints", "MERGE needs the backing index", _constraints_present),
    Check("stub keys", "a stub with only a uid cannot be found by name",
          _stub_keys_present),
    Check("edge src", "no src means the edge can never be refreshed",
          _every_edge_has_src),
    Check("uid integrity", "a stale uid duplicates the node on next load",
          _uid_matches_keys),
    Check("no text in graph", "document text belongs to the relational store",
          _no_text_in_graph),
    Check("mapped honesty", "MAPPED must not claim an unwired tool",
          _mapped_is_honest),
    Check("table coverage", "a new SQLite table must not go unnoticed",
          _every_table_accounted_for),
    Check("orphan stubs", "litter from a refresh", _no_orphan_stubs),
    Check("writers agree",
          "an undercount here is an underestimated change",
          _writers_agree),
)


def run(verbose: bool = True) -> bool:
    ok = True
    if verbose:
        print(f"  graph {gs.load().neo4j.uri}")
        print()
    for check in CHECKS:
        try:
            passed, detail = check.fn()
        except Exception as exc:                          # noqa: BLE001
            passed, detail = False, f"{type(exc).__name__}: {exc}"
        ok &= passed
        if verbose:
            mark = "ok  " if passed else "FAIL"
            print(f"  {mark} {check.name:<16} {detail}")
    if verbose:
        print()
        print("  ALL CHECKS PASSED" if ok else
              "  FAILURES ABOVE - each names what it costs")
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="check the graph for faults that have actually happened")
    ap.add_argument("--quiet", action="store_true", help="exit code only")
    args = ap.parse_args(argv)
    try:
        return 0 if run(verbose=not args.quiet) else 1
    except driver.GraphUnavailable as exc:
        print(f"\n  {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
