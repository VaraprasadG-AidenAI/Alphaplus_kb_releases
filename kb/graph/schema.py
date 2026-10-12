"""
Create the constraints and indexes.  This is the graph's equivalent of the
42 indexes in schema.sql, and it must run before the first load.

    python -m kb.graph.schema            # create what is missing
    python -m kb.graph.schema --show     # what exists now
    python -m kb.graph.schema --drop     # remove ours, leave anything else

## Constraints first, and why it is not optional

Every loader statement is a MERGE on `uid`.  Without a uniqueness constraint on
`uid`, MERGE has no index to seek on, so each MERGE degrades to a label scan -
the load goes quadratic and a 230k-node graph that should take a minute takes
an afternoon.  The constraint is what makes the load fast, not just what makes
it correct.  Neo4j creates a backing index for a uniqueness constraint
automatically, so a separate index on the same property would be redundant and
is not created.

## Two kinds of index here

  * **uniqueness constraints** on `uid`, one per label - identity, and the
    seek the loader needs.
  * **plain indexes** on the properties the MCP tools filter by - `name`,
    `subsystem`, `tier`, `library`. A tool that asks "every member in
    subsystem ORD" scans without one.

Names are prefixed `ap_` so `--drop` can find ours and leave anything a DBA
added alone.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List, Tuple

from kb.graph import driver, model

PREFIX = "ap_"


def _constraint_name(label: str) -> str:
    return f"{PREFIX}{label.lower()}_uid"


def _index_name(label: str, prop: str) -> str:
    return f"{PREFIX}{label.lower()}_{prop.lower()}"


def statements() -> List[Tuple[str, str]]:
    """(name, cypher) for everything we own, constraints before indexes."""
    out: List[Tuple[str, str]] = []

    for node in model.NODES:
        name = _constraint_name(node.label)
        out.append((name,
                    f"CREATE CONSTRAINT {name} IF NOT EXISTS "
                    f"FOR (n:{node.label}) REQUIRE n.uid IS UNIQUE"))

    for node in model.NODES:
        for prop in node.indexed:
            # uid already has the constraint's backing index
            if prop == "uid":
                continue
            name = _index_name(node.label, prop)
            out.append((name,
                        f"CREATE INDEX {name} IF NOT EXISTS "
                        f"FOR (n:{node.label}) ON (n.{prop})"))

    # Relationship property indexes.  `src` is how an incremental refresh finds
    # what to delete, and `confidence` is filtered on by several tools; both are
    # scans without an index.
    for edge in model.EDGES:
        out.append((f"{PREFIX}{edge.rel.lower()}_src",
                    f"CREATE INDEX {PREFIX}{edge.rel.lower()}_src IF NOT EXISTS "
                    f"FOR ()-[r:{edge.rel}]-() ON (r.src)"))

    # Full text, last.  It is the only index here that is not a b-tree: Lucene
    # sits beside the store, and `db.index.fulltext.queryNodes` is the only way
    # to read it - a WHERE clause will not use it.  The analyzer is fixed at
    # creation, so changing it means dropping and rebuilding, which is why
    # `--drop` exists rather than an ALTER.
    if not model.FULLTEXT:
        # The index is retired, not just unlisted: a graph built before the
        # retirement still carries it, and a Lucene index over a property
        # nothing writes any more is exactly the stale-but-confident state
        # this module exists to prevent.
        out.append(("drop ap_member_ft",
                    "DROP INDEX ap_member_ft IF EXISTS"))
    for ft in model.FULLTEXT:
        props = ", ".join(f"n.{p}" for p in ft.props)
        out.append((ft.name,
                    f"CREATE FULLTEXT INDEX {ft.name} IF NOT EXISTS "
                    f"FOR (n:{ft.label}) ON EACH [{props}] "
                    f"OPTIONS {{indexConfig: {{"
                    f"`fulltext.analyzer`: '{ft.analyzer}', "
                    f"`fulltext.eventually_consistent`: false}}}}"))

    return out


def fulltext_state() -> List[Dict[str, Any]]:
    """Whether each full-text index exists, is ONLINE, and how far populated.

    A full-text index is populated ASYNCHRONOUSLY.  Querying one that is still
    POPULATING returns partial results with no error - a silently wrong answer,
    which is the failure mode this whole package is written against.  So the
    state is checkable, and `kb.graph.fulltext --wait` blocks on it.
    """
    out: List[Dict[str, Any]] = []
    rows = driver.read("""
        SHOW INDEXES YIELD name, type, state, populationPercent, entityType,
                           labelsOrTypes, properties
        WHERE type = 'FULLTEXT'
        RETURN name, state, populationPercent AS pct, entityType,
               labelsOrTypes AS labels, properties
        ORDER BY name
    """)
    have = {r["name"]: r for r in rows}
    for ft in model.FULLTEXT:
        r = have.get(ft.name)
        out.append({
            "name": ft.name,
            "exists": r is not None,
            "state": (r or {}).get("state", "MISSING"),
            "pct": (r or {}).get("pct", 0.0),
            "properties": (r or {}).get("properties") or list(ft.props),
        })
    return out


def create(verbose: bool = True) -> Dict[str, int]:
    """Apply everything.  Idempotent - IF NOT EXISTS throughout."""
    made = {"constraints": 0, "indexes": 0, "already": 0}
    for name, cypher in statements():
        counters = driver.write(cypher)
        added = counters["constraints_added"] + counters["indexes_added"]
        if counters["constraints_added"]:
            made["constraints"] += 1
        elif counters["indexes_added"]:
            made["indexes"] += 1
        else:
            made["already"] += 1
        if verbose:
            mark = "new " if added else "have"
            print(f"  {mark} {name}")
    return made


def show() -> str:
    """What exists on the server now, ours marked."""
    lines: List[str] = []
    rows = driver.read("""
        SHOW CONSTRAINTS YIELD name, type, labelsOrTypes, properties
        RETURN name, type, labelsOrTypes AS labels, properties
        ORDER BY name
    """)
    lines.append(f"CONSTRAINTS ({len(rows)})")
    for r in rows:
        mine = "*" if str(r["name"]).startswith(PREFIX) else " "
        labels = ",".join(r["labels"] or [])
        props = ",".join(r["properties"] or [])
        lines.append(f" {mine} {r['name']:<34} {r['type']:<24} :{labels}({props})")

    rows = driver.read("""
        SHOW INDEXES YIELD name, type, entityType, labelsOrTypes, properties
        RETURN name, type, entityType, labelsOrTypes AS labels, properties
        ORDER BY name
    """)
    lines.append("")
    lines.append(f"INDEXES ({len(rows)})")
    for r in rows:
        mine = "*" if str(r["name"]).startswith(PREFIX) else " "
        labels = ",".join(r["labels"] or [])
        props = ",".join(r["properties"] or [])
        lines.append(f" {mine} {r['name']:<34} {r['type']:<16} "
                     f"{r['entityType']:<5} :{labels}({props})")
    # Full text separately, because "exists" is not the same as "usable" for
    # one of these - a POPULATING index answers queries with partial data.
    lines.append("")
    lines.append("FULL TEXT")
    for st in fulltext_state():
        mark = "*" if st["exists"] else " "
        warn = "" if st["state"] == "ONLINE" else "   <-- not ONLINE, results will be partial"
        lines.append(f" {mark} {st['name']:<34} {st['state']:<12} "
                     f"{st['pct']:>5.1f}%  ({', '.join(st['properties'])}){warn}")

    lines.append("")
    lines.append("  * = created by kb.graph.schema")
    return "\n".join(lines)


def drop(verbose: bool = True) -> int:
    """Remove only what we created.  Anything a DBA added is left alone."""
    dropped = 0
    for kind, cmd in (("INDEXES", "DROP INDEX"), ("CONSTRAINTS", "DROP CONSTRAINT")):
        rows = driver.read(f"SHOW {kind} YIELD name RETURN name")
        for r in rows:
            name = r["name"]
            if not str(name).startswith(PREFIX):
                continue
            driver.write(f"{cmd} {name} IF EXISTS")
            dropped += 1
            if verbose:
                print(f"  dropped {name}")
    return dropped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="create the graph's constraints and indexes")
    ap.add_argument("--show", action="store_true", help="list what exists, do not change")
    ap.add_argument("--drop", action="store_true", help="remove what kb.graph created")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    try:
        if args.show:
            print(show())
            return 0
        if args.drop:
            n = drop(verbose=not args.quiet)
            print(f"\n  dropped {n} object(s)")
            return 0
        print("  creating constraints and indexes ...")
        made = create(verbose=not args.quiet)
        print(f"\n  {made['constraints']} constraint(s) created, "
              f"{made['indexes']} index(es) created, "
              f"{made['already']} already present")
        return 0
    except driver.GraphUnavailable as exc:
        print(f"\n  {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
