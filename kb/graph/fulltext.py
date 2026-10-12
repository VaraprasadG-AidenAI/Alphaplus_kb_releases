"""
RETIRED. model.FULLTEXT is empty: the graph no longer carries member text or a
Lucene index over it - the relational store answers search_text alone, and
kb.graph.selftest enforces the absence. This module is kept as the record of
the FTS5-vs-Lucene measurement that once justified the copy; its entry points
no-op or fail plainly when the spec is gone.

Full text in Neo4j.  The blocker that turned out not to be one.

    python -m kb.graph.fulltext              # load the text, build, wait
    python -m kb.graph.fulltext --status     # is it there, is it ONLINE
    python -m kb.graph.fulltext --query "credit hold"
    python -m kb.graph.fulltext --compare    # this against SQLite FTS5

## What was claimed, and what is actually true

The migration notes said `search` could not move because "Neo4j full-text has
no bm25() to read a score from".  That is wrong, and it was asserted without
testing.  Neo4j's full-text indexes are Lucene:

  * `db.index.fulltext.queryNodes` YIELDs `node, score` - there IS a score;
  * the scoring is BM25, the same family as FTS5's;
  * 45 analyzers ship with it, and `english` stems and drops stopwords much as
    FTS5's `porter unicode61` does;
  * one index can span several labels and several properties.

The one real difference: FTS5 takes per-column weights as arguments to
`bm25(table, w1, w2, w3)`.  Lucene has no such call.  Weighting is expressed in
the query instead, with the caret boost operator on a field - `name:WHSMAS^5`.
Same effect, different place.  `settings.fulltext.name_boost` is that number.

## Why the text is a node property and not a node

`search` is one row per member - 13,366 rows, `kind` is the member_type, and
every `name` in it exists in `object`.  So it is not a new entity; it is a
property of a Member that happens to be long.  Making it a node would add a
join to every query for nothing.

## The failure mode this module is written against

A Lucene index populates ASYNCHRONOUSLY.  Query one that is still POPULATING
and you get partial results, ranked confidently, with no error and no warning.
That is a silently wrong answer - the worst kind here.  So:

  * `build()` waits for ONLINE and says how long it took;
  * `ensure_ready()` refuses to answer from an index that is not ONLINE;
  * `--status` prints the state and the population percentage.

## Tokenisation is not identical

Measured against FTS5 on this data with `--compare`: 2 of 6 probe queries
return an identical top-3.  "despatch note" and "price change" agree exactly;
"invoice print" returns the same three members in a different order; "credit
hold" and "warehouse" agree on 2 of 3; "dunning letter" agrees on 1 of 3 -
FTS5 puts FINMEN04 first, Lucene puts TAB_CUSX41 first.  Neither is wrong.
They stem and split differently.

## And it does not matter as much as that looks - measured, not assumed

`kb.eval.probe` over the 40-ticket benchmark, identical index, identical gold:

    backend                     recall   solved   gold names   ties
    sqlite (FTS5)                  82%    30/40        66/80  19/40
    neo4j, name_boost 1.0          82%    30/40        66/80  19/40
    neo4j, name_boost 2.0          82%    30/40        66/80  19/40
    neo4j, name_boost 5.0          82%    30/40        66/80  19/40
    neo4j, name_boost 10.0         82%    30/40        66/80  19/40
    pass 3 removed entirely        82%    29/40        66/80  19/40

Two things follow, and both are worth saying plainly:

  1. **The switch is safe.**  Not "close enough" - byte-identical on every
     reported figure.  The per-query rankings differ, but this pass is one of
     five and is decayed by rank POSITION, so a reshuffle inside it does not
     move the final order.
  2. **`name_boost` does nothing measurable here, and neither does most of this
     pass.**  Deleting pass 3 altogether costs ONE ticket out of forty and
     changes recall not at all.  So full-text was never the thing holding
     retrieval at 82%.  Tuning the boost is not where the next point comes
     from; the empty summary pyramid and the other four passes are.

That is the useful result, and it is the opposite of the impression "blocker
solved" gives.  Re-run `kb.eval.probe` after any change here anyway - the cost
is seconds and the alternative is finding out from a ticket.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from kb.graph import driver, model, schema
from kb.graph import settings as gs

#: Lucene query syntax characters.  Unescaped, a stray `-` or `:` or `~` in a
#: ticket makes the whole query a syntax error, and the caller can only swallow
#: it - which is how a retrieval pass silently returns nothing.  Escaped rather
#: than stripped, because `AR-101` is a real identifier a ticket will contain.
_LUCENE_SPECIAL = re.compile(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)')

#: Words Lucene's `english` analyzer would drop anyway, plus the AS/400 noise
#: that matches thousands of members and therefore ranks nothing.  Kept short
#: on purpose: an aggressive list here silently loses recall.
_NOISE = frozenset({
    "program", "programs", "file", "files", "field", "fields", "member",
    "members", "library", "record", "records", "system", "please", "should",
    "would", "there", "their", "which", "about", "after", "before",
})


# ---------------------------------------------------------------------------
# building the query
# ---------------------------------------------------------------------------

def terms_of(text: str, cfg: Optional[gs.Settings] = None) -> List[str]:
    """The words worth searching for, longest first.

    Deterministic order.  `sorted()` rather than iterating a set, because a set
    made the same ticket rank differently on two consecutive runs - the same
    bug that was fixed in the SQLite path and would have come straight back
    here.
    """
    cfg = cfg or gs.load()
    words = {w.upper() for w in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{1,}", text)}
    keep = [w for w in words
            if len(w) >= cfg.fulltext.min_term_length and w.lower() not in _NOISE]
    return sorted(keep, key=lambda w: (-len(w), w))[:cfg.fulltext.max_terms]


def escape(term: str) -> str:
    """Make one term safe to put in a Lucene query."""
    return _LUCENE_SPECIAL.sub(r"\\\1", term)


def lucene_query(text_or_terms, cfg: Optional[gs.Settings] = None,
                 spec: Optional[model.FullTextSpec] = None) -> str:
    """Build the Lucene expression, with the per-field boost applied.

    Produces, for name_boost 5.0 and one term:

        (name:WHSMAS^5.0 OR searchtext:WHSMAS)

    OR between terms, matching the SQLite pass this replaces - it joined its
    terms with OR too, so a ticket mentioning five things does not have to
    mention all five to match anything.
    """
    cfg = cfg or gs.load()
    spec = spec or model.FULLTEXT[0]
    terms = (list(text_or_terms) if not isinstance(text_or_terms, str)
             else terms_of(text_or_terms, cfg))
    if not terms:
        return ""

    boost = dict(spec.boost)
    boost["name"] = cfg.fulltext.name_boost

    clauses: List[str] = []
    for term in terms:
        safe = escape(term)
        per_field = []
        for prop in spec.props:
            w = boost.get(prop, 1.0)
            # ^1.0 is a no-op in Lucene; leaving it off keeps the query
            # readable in a log and identical to the unweighted form.
            suffix = f"^{w}" if abs(w - 1.0) > 1e-9 else ""
            per_field.append(f"{prop}:{safe}{suffix}")
        clauses.append("(" + " OR ".join(per_field) + ")")
    return " OR ".join(clauses)


# ---------------------------------------------------------------------------
# loading the text
# ---------------------------------------------------------------------------

# The connection factory lives in kb.graph.load and is imported, not copied.
# This module carried a byte-for-byte duplicate of both _facts_db and _connect,
# which meant a port that repointed the loader at another store left the
# full-text comparison silently reading the old one.
from kb.graph.load import _connect, _facts_db          # noqa: E402

def load_text(conn: Optional[sqlite3.Connection] = None,
              cfg: Optional[gs.Settings] = None,
              members: Optional[Sequence[str]] = None,
              verbose: bool = True) -> Dict[str, int]:
    """Copy `search.body` onto the Member nodes as `searchtext`.

    Matched on `(name, member_type)`, not on name alone.  `search.kind` IS the
    member_type, so this is the one place a name-only fact can be made precise
    for free - a menu's MNUDDS screen and its MNUCMD command list have different
    bodies and each lands on the right node.

    `members` restricts the pass to those member names, which is what
    `kb.graph.update` needs for an incremental refresh.
    """
    cfg = cfg or gs.load()
    own = conn is None
    conn = conn or _connect()
    spec = model.FULLTEXT[0]

    cypher = """
    UNWIND $rows AS row
    CALL {
      WITH row
      MATCH (m:Member {name: row.name, member_type: row.member_type})
      SET m.searchtext = row.body
    } IN TRANSACTIONS OF $batch ROWS
    """

    sql = spec.select
    params: Tuple[Any, ...] = ()
    if members:
        holes = ", ".join("?" for _ in members)
        sql = f"SELECT * FROM ({sql.strip()}) WHERE name IN ({holes})"
        params = tuple(members)

    total = props = 0
    t0 = time.perf_counter()
    batch: List[Dict[str, Any]] = []
    for r in conn.execute(sql, params):
        batch.append({"name": r["name"], "member_type": r["member_type"],
                      "body": r["body"] or ""})
        if len(batch) >= cfg.load.fetch_rows:
            c = driver.write_autocommit(
                cypher, {"rows": batch, "batch": cfg.load.batch_rows})
            total += len(batch)
            props += c["properties_set"]
            batch = []
    if batch:
        c = driver.write_autocommit(
            cypher, {"rows": batch, "batch": cfg.load.batch_rows})
        total += len(batch)
        props += c["properties_set"]

    took = time.perf_counter() - t0
    if own:
        conn.close()
    if verbose:
        print(f"  searchtext       {total:>9,} rows  {props:>9,} set  {took:>6.1f}s")
    return {"rows": total, "properties": props}


# ---------------------------------------------------------------------------
# building and waiting
# ---------------------------------------------------------------------------

def state(name: Optional[str] = None) -> Dict[str, Any]:
    """The one index's state, or the first one's."""
    want = name or model.FULLTEXT[0].name
    for st in schema.fulltext_state():
        if st["name"] == want:
            return st
    return {"name": want, "exists": False, "state": "MISSING", "pct": 0.0}


def wait_online(name: Optional[str] = None, timeout: float = 300.0,
                verbose: bool = True) -> Dict[str, Any]:
    """Block until the index is ONLINE, or give up and say so.

    Polled rather than awaited because Neo4j has no "notify me" for index
    population.  `db.awaitIndexes` exists but takes a whole-database timeout and
    returns nothing about which index was slow, so it cannot report progress.
    """
    want = name or model.FULLTEXT[0].name
    deadline = time.time() + timeout
    last = -1.0
    while time.time() < deadline:
        st = state(want)
        if not st["exists"]:
            raise driver.GraphUnavailable(
                f"full-text index {want} does not exist - run "
                f"`python -m kb.graph.schema` first")
        if st["state"] == "ONLINE":
            return st
        if st["state"] == "FAILED":
            raise driver.GraphUnavailable(
                f"full-text index {want} is FAILED - drop it with "
                f"`python -m kb.graph.schema --drop` and rebuild")
        if verbose and st["pct"] != last:
            print(f"  {want} {st['state']} {st['pct']:.1f}% ...")
            last = st["pct"]
        time.sleep(0.25)
    raise driver.GraphUnavailable(
        f"full-text index {want} was still {state(want)['state']} after "
        f"{timeout:.0f}s")


def ensure_ready(name: Optional[str] = None) -> None:
    """Refuse to answer from an index that would answer partially.

    A POPULATING index returns fewer hits, ranked as though they were all of
    them.  There is no error and no warning, and the answer looks fine.  Better
    to say the index is not ready.
    """
    st = state(name)
    if not st["exists"]:
        raise driver.GraphUnavailable(
            f"full-text index {st['name']} is missing - run "
            f"`python -m kb.graph.schema` then `python -m kb.graph.fulltext`")
    if st["state"] != "ONLINE":
        raise driver.GraphUnavailable(
            f"full-text index {st['name']} is {st['state']} "
            f"({st['pct']:.1f}% populated) - results would be partial. "
            f"Wait, or run `python -m kb.graph.fulltext --wait`")


def build(verbose: bool = True) -> Dict[str, Any]:
    """Create the index if needed, load the text, wait for ONLINE."""
    out: Dict[str, Any] = {}
    t0 = time.perf_counter()

    spec = model.FULLTEXT[0]
    props = ", ".join(f"n.{p}" for p in spec.props)
    driver.write(
        f"CREATE FULLTEXT INDEX {spec.name} IF NOT EXISTS "
        f"FOR (n:{spec.label}) ON EACH [{props}] "
        f"OPTIONS {{indexConfig: {{"
        f"`fulltext.analyzer`: '{spec.analyzer}', "
        f"`fulltext.eventually_consistent`: false}}}}")
    if verbose:
        print(f"  index {spec.name} present")

    out["text"] = load_text(cfg=gs.load(), verbose=verbose)
    out["state"] = wait_online(spec.name, verbose=verbose)
    out["seconds"] = round(time.perf_counter() - t0, 1)

    rows = driver.read(
        "MATCH (m:Member) WHERE m.searchtext IS NOT NULL "
        "RETURN count(m) AS n, sum(size(m.searchtext)) AS chars", cap=1)
    out["members"] = rows[0]["n"] if rows else 0
    out["chars"] = rows[0]["chars"] if rows else 0

    if verbose:
        print(f"\n  {out['members']:,} members carry searchtext "
              f"({(out['chars'] or 0) / 1e6:.1f} MB of text)")
        print(f"  {spec.name} is {out['state']['state']} in {out['seconds']}s")
    return out


# ---------------------------------------------------------------------------
# querying
# ---------------------------------------------------------------------------

def search(text: str, limit: int = 25) -> List[Dict[str, Any]]:
    """Rank members by the English in them.  The Cypher half of pass 3.

    Returns `name`, `kind`, `score` - deliberately the same first two columns
    the SQLite query returns (`name`, `kind`, `rank`), so `backend.compare` can
    diff the intersection and judge the two on the rows they agree about rather
    than on a score that is not on the same scale.

    `score` is Lucene's, unbounded and not comparable to FTS5's negative
    `rank`.  Callers must use POSITION, not the value - which is what the
    routing pass already does.
    """
    cfg = gs.load()
    q = lucene_query(text, cfg)
    if not q:
        return []
    ensure_ready()
    spec = model.FULLTEXT[0]
    return driver.read(
        f"""
        CALL db.index.fulltext.queryNodes($index, $q) YIELD node, score
        WHERE node.stub IS NULL
        RETURN node.name AS name,
               node.member_type AS kind,
               score
        ORDER BY score DESC, name
        LIMIT $limit
        """,
        {"index": spec.name, "q": q, "limit": int(limit)},
        cap=max(1, int(limit)))


# ---------------------------------------------------------------------------
# the comparison that justifies the switch
# ---------------------------------------------------------------------------

#: Queries with a known answer on this data, used by --compare.  Chosen because
#: each exercises a different thing: a two-word business phrase, a compound
#: noun, a bare identifier-ish word, and the one that is known to DIFFER.
PROBES: Tuple[str, ...] = (
    "credit hold",
    "despatch note",
    "warehouse",
    "dunning letter",
    "invoice print",
    "price change",
)


def compare(limit: int = 3) -> str:
    """FTS5 against Lucene, top-N, side by side.

    This is the evidence for the switch, and it is deliberately printed rather
    than asserted: the two do NOT agree on everything and pretending otherwise
    would hide the one case that matters.
    """
    conn = _connect()
    lines = [f"  {'query':<18}{'FTS5 (SQLite)':<34}{'Lucene (Neo4j)':<34}same",
             "  " + "-" * 92]
    agree = 0
    for probe in PROBES:
        words = " OR ".join(f'"{w}"' for w in terms_of(probe))
        try:
            want = [r["name"] for r in conn.execute(
                "SELECT name FROM search WHERE search MATCH ? "
                "ORDER BY rank LIMIT ?", (words, limit)).fetchall()]
        except sqlite3.OperationalError as exc:
            want = [f"<{exc}>"]
        got = [r["name"] for r in search(probe, limit)]
        same = "yes" if want == got else f"{len(set(want) & set(got))}/{limit}"
        if want == got:
            agree += 1
        lines.append(f"  {probe:<18}{', '.join(want):<34}"
                     f"{', '.join(got):<34}{same}")
    conn.close()
    lines.append("")
    lines.append(f"  {agree} of {len(PROBES)} queries return an identical top-{limit}.")
    lines.append("  The rest differ by TOKENISATION, not by correctness - Lucene's")
    lines.append("  `english` analyzer stems and splits differently from FTS5's")
    lines.append("  `porter unicode61`.  Neither ranking is wrong, and that is exactly")
    lines.append("  why `python -m kb.eval.probe` must be re-run before trusting this")
    lines.append("  path for retrieval.")
    return "\n".join(lines)


def status() -> str:
    st = state()
    cfg = gs.load()
    rows = driver.read(
        "MATCH (m:Member) WHERE m.searchtext IS NOT NULL "
        "RETURN count(m) AS n, sum(size(m.searchtext)) AS chars", cap=1)
    n = rows[0]["n"] if rows else 0
    chars = (rows[0]["chars"] if rows else 0) or 0
    spec = model.FULLTEXT[0]
    ok = "ok  " if st["state"] == "ONLINE" and n else "FAIL"
    return "\n".join([
        f"  {ok} {spec.name}",
        f"       state      {st['state']}  {st['pct']:.1f}% populated",
        f"       indexes    :{spec.label}({', '.join(spec.props)})",
        f"       analyzer   {spec.analyzer}",
        f"       text       {n:,} members, {chars / 1e6:.1f} MB",
        f"       name_boost {cfg.fulltext.name_boost}  "
        f"(1.0 = same weighting as the FTS5 table it replaces)",
        f"       terms      >={cfg.fulltext.min_term_length} chars, "
        f"max {cfg.fulltext.max_terms}",
    ])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="load and query the Neo4j full-text index")
    ap.add_argument("--status", action="store_true", help="state and coverage")
    ap.add_argument("--wait", action="store_true", help="block until ONLINE")
    ap.add_argument("--query", metavar="TEXT", help="search and print the ranking")
    ap.add_argument("--compare", action="store_true",
                    help="FTS5 against Lucene on known queries")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    verbose = not args.quiet

    try:
        if args.status:
            print(status())
            return 0
        if args.wait:
            st = wait_online(verbose=verbose)
            print(f"  {st['name']} is {st['state']}")
            return 0
        if args.compare:
            print(compare())
            return 0
        if args.query:
            cfg = gs.load()
            print(f"  terms  {', '.join(terms_of(args.query, cfg)) or '(none)'}")
            print(f"  lucene {lucene_query(args.query, cfg) or '(empty)'}")
            print()
            t0 = time.perf_counter()
            rows = search(args.query, args.limit)
            ms = (time.perf_counter() - t0) * 1000
            if not rows:
                print("  nothing matched.")
                return 0
            print(f"  {'#':>3}  {'member':<16}{'type':<12}score")
            for i, r in enumerate(rows, 1):
                print(f"  {i:>3}  {r['name']:<16}{r['kind'] or '':<12}"
                      f"{r['score']:.2f}")
            print(f"\n  {len(rows)} hit(s) in {ms:.1f} ms")
            return 0

        out = build(verbose=verbose)
        return 0 if out["state"]["state"] == "ONLINE" else 1
    except driver.GraphUnavailable as exc:
        print(f"\n  {exc}", file=sys.stderr)
        return 1
    except sqlite3.OperationalError as exc:
        print(f"\n  cannot read facts.db: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
