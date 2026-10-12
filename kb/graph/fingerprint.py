"""
Has the graph changed since the map was built?

    python -m kb.graph.fingerprint            # what it is now
    python -m kb.graph.fingerprint --stamp    # mark the graph as just written

WHY THIS EXISTS
---------------
The map is built FROM Neo4j, but `sync` decided the map was stale by comparing
it against `facts.db`. So a change that touched the graph and not the index was
invisible, and the map stayed stale with nothing reporting it.

That is not hypothetical - it happened. A parser fix reclassified 13,264 CHKOBJ
edges from "reads the file" to "references it". The SOURCE never changed, so
`member_hash` never changed, so `kb.graph.update` reported "0 members to
refresh" and the map kept showing reads that had been corrected. Only a manual
`load --wipe` fixed it.

WHAT A FINGERPRINT CAN AND CANNOT SEE
-------------------------------------
Two halves, because neither is sufficient alone:

  COUNTS   total nodes and relationships, from Neo4j's count store - O(1), ~14ms.
           Catches anything ADDED or REMOVED, by us or by anyone else.
           Blind to a property edited in place: the CHKOBJ fix changed 13,264
           `mode` values and not one count.

  STAMP    a marker node this project writes on every load and every refresh,
           carrying the model version and the moment. Catches exactly the case
           counts miss - for OUR writes.

Combined they cover every change this system makes, and every structural change
anyone else makes.

THE HONEST GAP, STATED RATHER THAN GLOSSED: a third party editing a property in
place, by hand in Cypher, with no stamp, changes neither the counts nor the
marker. Detecting that needs a scan of 978,000 relationships, which is not a
thing to do every thirty seconds. `--stamp` is offered so an operator doing a
manual write can mark it, and `sync --force` rebuilds regardless.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
from typing import Any, Dict, Optional

#: The singleton marker. A label of its own so it can never be confused with a
#: :Member, and so `MATCH (b:GraphBuild)` is an index-free single-row lookup.
LABEL = "GraphBuild"


def stamp(note: str = "") -> str:
    """Record that the graph was just written, and return the new fingerprint.

    Called by the loader and the incremental refresh. Idempotent: one node,
    updated in place, so this never accumulates.
    """
    from . import driver, model
    token = hashlib.sha256(
        f"{time.time_ns()}|{len(model.NODES)}|{len(model.EDGES)}|{note}"
        .encode("utf-8")).hexdigest()[:16]
    driver.write(
        f"MERGE (b:{LABEL} {{id: 'singleton'}}) "
        f"SET b.token = $token, b.at = $at, b.note = $note, "
        f"    b.nodes = $n, b.edges = $e "
        f"RETURN b.token AS token",
        {"token": token, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "note": note[:200], "n": len(model.NODES), "e": len(model.EDGES)})
    return token


def current(timeout: float = 8.0) -> Optional[Dict[str, Any]]:
    """The graph's fingerprint right now, or None if it cannot be reached.

    None is a real answer and must not be treated as "changed": a graph that is
    down should not send the pipeline into a rebuild loop. Callers compare only
    when they have two fingerprints.
    """
    try:
        from . import driver
        # ONE query, two count-store lookups. Deliberately not per-label: the
        # `CALL {} IN` form that gives per-label counts measured 4.7 SECONDS,
        # because it scans rather than reading the count store. This is ~14ms.
        rows = driver.read(
            "MATCH (n) WITH count(n) AS nodes "
            "MATCH ()-[r]->() RETURN nodes, count(r) AS rels", cap=1)
        if not rows:
            return None
        out = {"nodes": rows[0]["nodes"], "rels": rows[0]["rels"], "token": ""}
        mark = driver.read(
            f"MATCH (b:{LABEL} {{id: 'singleton'}}) "
            f"RETURN b.token AS token, b.at AS at", cap=1)
        if mark:
            out["token"] = mark[0].get("token") or ""
            out["at"] = mark[0].get("at") or ""
        return out
    except Exception:                                     # noqa: BLE001
        return None


def as_text(fp: Optional[Dict[str, Any]]) -> str:
    """A fingerprint flattened to one comparable string.

    Stored in the map's payload so the two can be compared without the viewer
    needing to understand any of the above.
    """
    if not fp:
        return ""
    return f"{fp.get('nodes', 0)}:{fp.get('rels', 0)}:{fp.get('token', '')}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="has the graph changed since the map was built?")
    ap.add_argument("--stamp", action="store_true",
                    help="mark the graph as just written (after a manual edit)")
    ap.add_argument("--note", default="manual", help="what the stamp records")
    args = ap.parse_args(argv)

    if args.stamp:
        token = stamp(args.note)
        print(f"  stamped: {token}")
        return 0

    fp = current()
    if fp is None:
        print("  the graph cannot be reached - no fingerprint")
        return 1
    print(f"  nodes {fp['nodes']:,}  rels {fp['rels']:,}")
    print(f"  token {fp.get('token') or '(never stamped)'}"
          f"{'  at ' + fp['at'] if fp.get('at') else ''}")
    print(f"  fingerprint: {as_text(fp)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
