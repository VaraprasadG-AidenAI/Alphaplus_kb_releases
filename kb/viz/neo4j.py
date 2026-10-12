"""
Export the same graph as a Neo4j import set.

Written for people who would rather ask Cypher than click.  It is the same
facts as the HTML map - one node per member, one relationship per edge layer,
every relationship carrying its confidence - so an answer from either matches.

    python -m kb.viz.build --neo4j

Then copy kb/data/neo4j/*.csv into your Neo4j instance's import/ directory and
run load.cypher (`:source load.cypher` in Browser, or cypher-shell -f).
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict

from kb import config
from kb.viz.graph import LAYERS

#: layer key -> Neo4j relationship type
REL = {
    "call": "CALLS",
    "copy": "COPIES",
    "file": "OPENS",
    "override": "OVERRIDES_TO",
    "format": "WRITES_FORMAT",
    "based_on": "BASED_ON",
    "sqlalias": "SQL_ALIAS",
    "message": "SENDS",
    "dtaara": "USES_DTAARA",
}

LOAD_HEADER = """\
// ALPHAPLUS knowledge base - graph import
//
// 1. Find where the CSVs must go:
//      SHOW SETTINGS YIELD name, value
//      WHERE name = 'server.directories.import' RETURN value;
// 2. Copy every .csv beside this file into that directory.
// 3. Run this whole script (Neo4j Browser or the Query tab).
//
// Deliberately plain LOAD CSV - no CALL { } IN TRANSACTIONS, so it needs no
// :auto prefix and does not depend on a Cypher version.  The biggest file is
// 34k rows, which fits one transaction comfortably.
//
// Every relationship carries `confidence`: exact | inferred | unresolved.
// Nothing here is guessed - see Rule 2 in CLAUDE.md.

CREATE CONSTRAINT member_name IF NOT EXISTS
  FOR (m:Member) REQUIRE m.name IS UNIQUE;

LOAD CSV WITH HEADERS FROM 'file:///nodes.csv' AS row
MERGE (m:Member {name: row.name})
SET m.type       = row.type,
    m.alsoTypes  = row.also,
    m.subsystem  = row.subsystem,
    m.lines      = toInteger(row.lines),
    m.path       = row.path,
    m.status     = row.status,
    m.sqlAlias   = row.alias,
    m.degree     = toInteger(row.degree),
    m.inDegree   = toInteger(row.inDegree),
    m.isHub      = toInteger(row.inDegree) >= %(hubcut)d;
"""

# `kind` sits inside the MERGE pattern on purpose.  A program can both read
# and update the same file, and a caller can reach a callee by two different
# mechanisms; those are different facts and must not collapse into one edge.
LOAD_REL = """
LOAD CSV WITH HEADERS FROM 'file:///%(file)s' AS row
MATCH (a:Member {name: row.src})
MATCH (b:Member {name: row.dst})
MERGE (a)-[r:%(rel)s {kind: row.attr}]->(b)
SET r.confidence = row.confidence;
"""

QUERIES = """
// ---------------------------------------------------------------------------
// Worked queries
// ---------------------------------------------------------------------------

// 1. What must recompile if a copybook changes - the exact, transitive answer.
MATCH (c:Member {name: 'DBFLAGS'})<-[:COPIES*1..5]-(p:Member)
RETURN DISTINCT p.name, p.type ORDER BY p.name;

// 2. The hubs. Traversing *through* these is what makes a blast radius
//    meaningless - they are destinations, not corridors.
MATCH (m:Member) WHERE m.isHub
RETURN m.name, m.type, m.inDegree ORDER BY m.inDegree DESC LIMIT 25;

// 3. Data-flow impact, hub-damped: who reads what this program writes,
//    then who reads what *those* write. Note the WHERE that skips hubs.
MATCH (seed:Member {name: 'ORD100'})-[w:OPENS]->(f:Member)
WHERE w.kind IN ['O','U'] AND NOT f.isHub
MATCH (f)<-[r:OPENS]-(p:Member) WHERE r.kind IN ['I','U']
RETURN DISTINCT f.name AS via, p.name AS affected, p.type ORDER BY via, affected;

// 4. Everything the KB could not resolve. The map showing its own blank areas.
MATCH ()-[r]->() WHERE r.confidence <> 'exact'
RETURN type(r), r.confidence, count(*) ORDER BY count(*) DESC;

// 5. Shortest coupling path between two members, whatever the mechanism.
MATCH p = shortestPath((a:Member {name:'ORD100'})-[*..6]-(b:Member {name:'CUSMAS'}))
RETURN [n IN nodes(p) | n.name] AS hops, [r IN relationships(p) | type(r)] AS how;
"""


def export(g: Dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    names = g["names"]
    conf_names = config.CONFIDENCE_LEVELS

    with (out_dir / "nodes.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "type", "also", "subsystem", "lines", "path",
                    "status", "alias", "degree", "inDegree"])
        for i, name in enumerate(names):
            w.writerow([name, g["types"][i], g["also"][i], g["subs"][i],
                        g["lines"][i], g["paths"][i], g["status"][i],
                        g["alias"][i], g["deg"][i], g["touched"][i]])

    from kb.viz.graph import hub_cut
    script = [LOAD_HEADER % {"hubcut": hub_cut(g["touched"])}]

    for spec in LAYERS:
        key = spec["key"]
        edges = g["edges"][key]
        if not edges["pairs"]:
            continue
        fname = f"rel_{key}.csv"
        with (out_dir / fname).open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["src", "dst", "attr", "confidence"])
            for (s, t), a, c in zip(edges["pairs"], edges["attr"],
                                    edges["conf"]):
                w.writerow([names[s], names[t], spec["attrs"][a],
                            conf_names[c]])
        script.append(LOAD_REL % {"file": fname, "rel": REL[key]})

    script.append(QUERIES)
    (out_dir / "load.cypher").write_text("\n".join(script), encoding="utf-8")
    return out_dir
