"""
Read the whole graph out of NEO4J, for the map.

    python -m kb.viz.build --from-neo4j

`kb/viz/graph.py` does the same job against `facts.db`. This module exists
because the graph is the system of record now, and a picture of the system
should be drawn from the system - not from a parallel store that merely agrees
with it today.

WHY THIS MATTERS MORE THAN IT LOOKS
-----------------------------------
Two stores holding "the same" facts do not stay the same. `load --verify`
proves they agree right now, and that is exactly the kind of guarantee that
quietly stops being true after an incremental refresh touches one and not the
other. Building the map from Neo4j removes the question: if the graph is wrong
the map is visibly wrong, instead of the map being right and the graph being
wrong and nobody noticing until a tool answers from the graph.

THE CONTRACT
------------
`load()` here returns EXACTLY the dict `graph.load()` returns - same keys, same
order, same node indexing - so `build.py` cannot tell which one it called. That
is deliberate: one payload shape, one viewer, and a `--compare` that can put the
two side by side precisely because they are the same shape.

WHAT IT COSTS
-------------
Nine SQL queries become nine Cypher queries over Bolt, and Bolt is slower per
row than a local SQLite file - measurably so, see `kb.graph.bench`. It is paid
ONCE PER BUILD, never per page view, because the output is a static snapshot.
The timing is printed rather than hidden so the trade is visible.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .. import config
from . import graph as G


def _rows(cypher: str, **params: Any) -> List[Dict[str, Any]]:
    """An UNCAPPED read, which `driver.read` deliberately cannot do.

    `driver.read` exists to protect a TOOL from a runaway question, so it caps
    rows - and its cap is a hard count, not a sentinel: passing 0 means "stop
    after zero", which returns ONE row and looks exactly like an empty graph.
    That mistake built a 0.4 MB map from 1 member and 8 edges, over the top of
    a good one, and the only reason it was caught is that the number was absurd
    rather than merely wrong.

    A bulk export is the opposite operation from a tool query: truncating it
    silently is the worst possible outcome, so it does its own read with no cap
    and no break, and the caller sanity-checks the count.
    """
    from ..graph import driver
    cfg = driver.current_settings()

    def _work(tx):
        result = tx.run(cypher, params or {},
                        timeout=max(300, cfg.query.timeout_seconds))
        return [r.data() for r in result]

    with driver.session() as s:
        return s.execute_read(_work)


def _first_line(value: Any) -> int:
    """The earliest source line an edge was read from.

    Neo4j keeps EVERY site, as a comma-separated string: a message sent at
    fifteen places is "825,1020,1232,...". The SQLite path stored MIN(line) as
    one integer, and the viewer wants that same single number - it is what
    orders a participant's messages in the sequence diagram.

    Reading `r.line` (singular) returned null everywhere, which silently
    stripped every line citation from the map. The tests caught it; nothing on
    screen would have.
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    best = 0
    for part in str(value).split(","):
        part = part.strip()
        if not part.isdigit():
            continue
        n = int(part)
        if best == 0 or n < best:
            best = n
    return best


def load(_db_path=None, verbose: bool = True) -> Dict:
    """Every node and every edge, from Neo4j, in `graph.load()`'s shape."""
    t0 = time.perf_counter()
    say = (lambda m: print(m)) if verbose else (lambda m: None)

    names: List[str] = []
    index: Dict[str, int] = {}
    types: List[str] = []
    also: List[str] = []
    subs: List[str] = []
    libs: List[str] = []
    tiers: List[str] = []
    depths: List[Optional[int]] = []
    lines: List[int] = []
    paths: List[str] = []
    status: List[str] = []
    alias: List[str] = []

    # -- nodes ---------------------------------------------------------------
    # Ordered by (name, member_type) to match graph.py exactly: node INDEXES
    # end up in the payload and in the precomputed layouts, so a different
    # order is a different file even when it is the same graph.
    node_rows = _rows("""
        MATCH (m:Member)
        RETURN m.name AS name, m.member_type AS member_type,
               m.subsystem AS subsystem, m.library AS library,
               m.tier AS tier, m.depth AS depth, m.lines AS lines,
               m.path AS path, m.parse_status AS parse_status,
               m.based_on AS based_on
        ORDER BY m.name, m.member_type
    """)
    say(f"  neo4j: {len(node_rows):,} member row(s)")

    # Same collapse rule as the SQLite path: 107 names carry two member types
    # (a menu is both MNUCMD and MNUDDS). One node, every type kept.
    for r in node_rows:
        name = r["name"]
        if not name:
            continue
        if name in index:
            i = index[name]
            extra = [t for t in also[i].split(",") if t] + [r["member_type"] or ""]
            also[i] = ",".join(sorted({t for t in extra if t}))
            lines[i] = max(lines[i], int(r["lines"] or 0))
            continue
        index[name] = len(names)
        names.append(name)
        types.append(r["member_type"] or "?")
        also.append("")
        subs.append(r["subsystem"] or "")
        libs.append(r["library"] or "")
        tiers.append(r["tier"] or "")
        depths.append(r["depth"])
        lines.append(int(r["lines"] or 0))
        paths.append(r["path"] or "")
        status.append(r["parse_status"] or "ok")
        alias.append("")

    def node(name: str, kind: str = "?") -> Optional[int]:
        """Index of `name`, inventing a placeholder if it is not a member.

        Rule 2 again: a CALL to something not in the tree is a FACT about the
        codebase - usually an IBM system API - not a parse failure to swallow.
        """
        if not name:
            return None
        if name in index:
            return index[name]
        index[name] = len(names)
        names.append(name)
        types.append(kind)
        also.append(""); subs.append(""); libs.append(""); tiers.append("")
        depths.append(None); lines.append(0); paths.append("")
        status.append("external"); alias.append("")
        return index[name]

    # -- edges ---------------------------------------------------------------
    edges: Dict[str, Dict[str, List]] = {
        lay["key"]: {"pairs": [], "attr": [], "conf": [], "line": []}
        for lay in G.LAYERS
    }

    def add(layer: str, src: Optional[int], dst: Optional[int],
            attr: str, conf: str = "exact", line: int = 0) -> None:
        if src is None or dst is None or src == dst:
            return
        spec = next(l for l in G.LAYERS if l["key"] == layer)
        try:
            a = spec["attrs"].index(attr)
        except ValueError:
            a = len(spec["attrs"]) - 1
        edges[layer]["pairs"].append((src, dst))
        edges[layer]["attr"].append(a)
        edges[layer]["line"].append(int(line or 0))
        edges[layer]["conf"].append(
            config.CONFIDENCE_LEVELS.index(conf)
            if conf in config.CONFIDENCE_LEVELS else 0)

    # Each block below is one relationship type, aggregated the way the SQLite
    # query aggregated it: MIN(line) so a member opened at three sites draws
    # once, cited at the first, and MIN(confidence) so the WEAKEST claim wins -
    # an edge is only as certain as its least certain evidence.
    def pull(label: str, cypher: str) -> List[Dict[str, Any]]:
        t = time.perf_counter()
        rows = _rows(cypher)
        say(f"    {label:<12} {len(rows):>7,} in {time.perf_counter() - t:5.2f}s")
        return rows

    say("  pulling relationships:")

    for r in pull("CALLS", """
        MATCH (a:Member)-[r:CALLS]->(b:Member)
        RETURN a.name AS caller, b.name AS callee, r.kind AS kind,
               min(r.confidence) AS confidence, min(r.lines) AS lines
        ORDER BY caller, callee, kind
    """):
        add("call", node(r["caller"]), node(r["callee"], "?"),
            r["kind"] or "cobol", r["confidence"] or "exact", _first_line(r["lines"]))

    for r in pull("COPIES", """
        MATCH (a:Member)-[r:COPIES]->(b:Member)
        RETURN a.name AS program, b.name AS copybook, r.kind AS kind,
               min(r.confidence) AS confidence, min(r.lines) AS lines
        ORDER BY program, copybook, kind
    """):
        add("copy", node(r["program"]), node(r["copybook"], "CPY"),
            r["kind"] or "copybook", r["confidence"] or "exact", _first_line(r["lines"]))

    # GROUPED BY MODE, not just by pair.
    #
    # A program that opens one file INPUT in one paragraph and UPDATE in
    # another is two facts, and the viewer colours them differently - it is how
    # you see that something both reads and writes the same file. Collapsing
    # mode away lost 352 edges and, worse, made a writer look like a reader.
    # The SQLite path groups by (program, file, mode); this must match.
    for r in pull("OPENS", """
        MATCH (a:Member)-[r:OPENS]->(b:Member)
        RETURN a.name AS program, b.name AS file, r.mode AS mode,
               min(r.confidence) AS confidence, min(r.lines) AS lines
        ORDER BY program, file, mode
    """):
        add("file", node(r["program"]), node(r["file"], "PF"),
            r["mode"] or "unknown", r["confidence"] or "exact", _first_line(r["lines"]))

    for r in pull("OVERRIDES", """
        MATCH (a:Member)-[r:OVERRIDES_TO]->(b:Member)
        RETURN a.name AS cl_program, b.name AS target_file, r.kind AS kind,
               min(r.confidence) AS confidence, min(r.lines) AS lines
        ORDER BY cl_program, target_file, kind
    """):
        add("override", node(r["cl_program"]), node(r["target_file"], "PF"),
            r["kind"] or "OVRDBF", r["confidence"] or "exact", _first_line(r["lines"]))

    for r in pull("FORMATS", """
        MATCH (a:Member)-[r:WRITES_FORMAT]->(f:Format)
        RETURN a.name AS program, f.device_file AS device_file,
               min(r.lines) AS lines
        ORDER BY program, device_file
    """):
        add("format", node(r["program"]), node(r["device_file"], "DSPF"),
            "record", "exact", _first_line(r["lines"]))

    for r in pull("BASED_ON", """
        MATCH (a:Member)-[:BASED_ON]->(b:Member)
        RETURN a.name AS name, a.member_type AS member_type, b.name AS based_on
        ORDER BY name
    """):
        src = index.get(r["name"])
        target = r["based_on"] or ""
        if src is None or not target:
            continue
        # An LF names its physical parent; an SQL member's `based_on` is a
        # FOR SYSTEM NAME alias, which is a different fact wearing the same
        # column. Split them the way graph.py does.
        if (r["member_type"] or "") in ("SQL",):
            if target in index:
                add("sqlalias", src, index[target], "system_name", "inferred")
            else:
                alias[src] = target
        else:
            add("based_on", src, node(target, "PF"), "PFILE")

    for r in pull("SENDS", """
        MATCH (a:Member)-[r:SENDS]->(m:Message)
        RETURN a.name AS program, m.msgid AS msgid, min(r.lines) AS lines
        ORDER BY program, msgid
    """):
        add("message", node(r["program"]), node(r["msgid"], "MSGID"),
            "msgid", "exact", _first_line(r["lines"]))

    for r in pull("DTAARA", """
        MATCH (a:Member)-[:USES_DTAARA]->(d:DataArea)
        RETURN DISTINCT a.name AS member, d.name AS name
        ORDER BY member, name
    """):
        add("dtaara", node(r["member"]), node(r["name"], "DTAARA"), "dtaara")

    for r in pull("MENU_OPTION", """
        MATCH (a:Member)-[r:MENU_OPTION]->(b:Member)
        RETURN a.name AS menu, b.name AS target,
               min(r.confidence) AS confidence, min(r.lines) AS lines
        ORDER BY menu, target
    """):
        # The menu layer rides on `call`, exactly as the SQLite path does: a
        # menu option IS a call, with `menu` as its kind.
        add("call", node(r["menu"]), node(r["target"], "?"),
            "menu", r["confidence"] or "exact", _first_line(r["lines"]))

    # -- degree and reach ----------------------------------------------------
    n = len(names)
    deg = [0] * n
    touched = [0] * n
    for lay in edges.values():
        for s, t in lay["pairs"]:
            deg[s] += 1
            deg[t] += 1
            touched[t] += 1

    dangling_alias = [names[i] for i, a in enumerate(alias) if a]
    total = sum(len(l["pairs"]) for l in edges.values())
    say(f"  neo4j: {n:,} node(s), {total:,} edge(s) "
        f"in {time.perf_counter() - t0:.1f}s")

    # REFUSE to hand back a graph that is obviously truncated.
    #
    # A capped read returned 1 member and 8 edges once, and `build` cheerfully
    # wrote a 0.4 MB map over a good 3.9 MB one. Nothing downstream can tell a
    # small codebase from a broken query, so the check belongs here, where the
    # expectation exists. The floor is deliberately low - a genuinely tiny
    # source tree must still build - it only has to catch a collapse.
    if n < 50 or total < 50:
        raise RuntimeError(
            f"the graph returned {n} node(s) and {total} edge(s), which is too "
            f"few to be a codebase. Refusing to build a map from it - the "
            f"previous map is left alone. Check that Neo4j is loaded:\n"
            f"    python -m kb.graph.load --verify")

    return {
        "names": names,
        "index": index,
        "types": types,
        "also": also,
        "subs": subs,
        "libs": libs,
        "tiers": tiers,
        "depths": depths,
        "lines": lines,
        "paths": paths,
        "status": status,
        "alias": alias,
        "deg": deg,
        "touched": touched,
        "edges": edges,
        "gaps": {
            "dangling_alias": dangling_alias,
            "external": [names[i] for i, s in enumerate(status)
                         if s == "external" and types[i] == "?"],
        },
        "source": "neo4j",
    }
