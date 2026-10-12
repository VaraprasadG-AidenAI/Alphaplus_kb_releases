"""
Pull the whole graph out of facts.db.

Every node is a member from `object`.  Every edge type the schema records gets
its own layer, so nothing is silently merged: a COPY is not a CALL is not an
OVRDBF, and the viewer can turn each one off independently.

Rule 2 survives the trip.  Edges carry their confidence, and a CALL that never
resolved to a name becomes an edge to a `?` node rather than no edge at all.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Dict, List, Optional

from kb import config
from kb.index.degree import hub_cut


# ---------------------------------------------------------------------------
# Edge layers
# ---------------------------------------------------------------------------
#
# Each layer is (key, label, direction-in-English, the values its `attr`
# column can take).  `attr` is what the viewer colours sub-kinds by.

LAYERS: List[Dict] = [
    {"key": "call",     "label": "CALL",         "verb": "calls",
     "attrs": ["cobol", "cl", "menu", "sbmjob", "bound", "cmd"]},
    {"key": "copy",     "label": "COPY",         "verb": "copies",
     "attrs": ["copybook", "record_layout", "device_layout",
               "generated_layout", "framework_macro",
               "dds_all_formats"]},
    # "X" is a REFERENCE, not I/O: CHKOBJ/ALCOBJ/DLCOBJ/DSPFD name a file
    # without opening it. Kept as an edge because the dependency is real - the
    # job fails if the file is missing - but it must never render as a READ.
    {"key": "file",     "label": "file I/O",     "verb": "opens",
     "attrs": ["I", "U", "O", "X", "unknown"]},
    {"key": "override", "label": "OVRDBF",       "verb": "overrides to",
     "attrs": ["OVRDBF", "OVRPRTF", "OVRDSPF", "other"]},
    {"key": "format",   "label": "screen/report", "verb": "writes format to",
     "attrs": ["record"]},
    {"key": "based_on", "label": "LF over PF",   "verb": "is built over",
     "attrs": ["PFILE"]},
    {"key": "sqlalias", "label": "SQL alias",    "verb": "is the SQL form of",
     "attrs": ["system_name"]},
    {"key": "message",  "label": "message",      "verb": "sends",
     "attrs": ["msgid"]},
    {"key": "dtaara",   "label": "data area",    "verb": "uses",
     "attrs": ["dtaara"]},
]

#: Layers that describe real structural coupling.  Used for the force layout
#: and as the viewer's default-on set.  `message` and `dtaara` are excluded:
#: 70 message ids and 2 data areas are shared by thousands of members, so they
#: are hubs by construction and say nothing about who is coupled to whom.
STRUCTURAL = {"call", "copy", "file", "override", "format", "based_on"}


def _conn(db_path=None):
    """The fact store, or one specific SQLite file.

    `db_path` still works and is still honoured: `kb graph --db other.db` is a
    real use - comparing two indexes, or rendering an archived one. Repointing
    this at kb.store dropped that silently for a while, which is the kind of
    regression a viewer has no way to report.

    An explicit path only makes sense for SQLite; on PostgreSQL the store is
    named by the DSN, so a path is ignored rather than half-honoured.
    """
    from kb import store
    if db_path is not None and store.backend() != "postgres":
        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        return db
    return store.facts()


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def load(db_path=None) -> Dict:
    """Read every node and every edge.  Returns plain dicts and lists."""
    db = _conn(db_path)
    q = lambda sql, *a: db.execute(sql, a).fetchall()

    # -- nodes -------------------------------------------------------------
    rows = q("SELECT name, member_type, language, subsystem, library, tier, "
             "depth, path, lines, parse_status, based_on FROM object "
             "ORDER BY name, member_type")

    names: List[str] = []
    index: Dict[str, int] = {}
    types: List[str] = []
    also: List[str] = []           # the other member types sharing this name
    subs: List[str] = []
    libs: List[str] = []           # the source library it was extracted from
    tiers: List[str] = []          # entry driver program service contract data
    depths: List[Optional[int]] = []   # CALL hops from an entry; None if none
    lines: List[int] = []
    paths: List[str] = []
    status: List[str] = []
    alias: List[str] = []          # SQL 'FOR SYSTEM NAME', when there is one

    # The graph is addressed by name, because that is how the platform itself
    # resolves references - a CALL names a program, not a member type.  107
    # names here carry two member types (a menu is both MNUCMD and MNUDDS).
    # They collapse to one node, and every type is kept on it rather than one
    # of them being quietly dropped.
    for r in rows:
        name = r["name"]
        if name in index:
            i = index[name]
            extra = [t for t in also[i].split(",") if t] + [r["member_type"]]
            also[i] = ",".join(sorted(set(extra)))
            lines[i] = max(lines[i], r["lines"] or 0)
            continue
        index[name] = len(names)
        names.append(name)
        types.append(r["member_type"])
        also.append("")
        subs.append(r["subsystem"] or "")
        libs.append(r["library"] or "")
        tiers.append(r["tier"] or "")
        depths.append(r["depth"])
        lines.append(r["lines"] or 0)
        paths.append(r["path"] or "")
        status.append(r["parse_status"] or "ok")
        alias.append("")

    # Physical files: the name is not a source member, but the extraction
    # proves the object exists and says which library holds it.
    #
    # Without this every data file a program opens becomes a placeholder with
    # no library, and the map collects them into one nameless column - on one
    # estate about three thousand of them, drawn as if nobody knew what they
    # were. They are not unknowns. They are the files the application reads
    # and writes, and their library is on disk.
    file_objects: Dict[str, str] = {}
    try:
        for r in q("SELECT name, library FROM data_member "
                   "WHERE is_file_object = 1"):
            file_objects.setdefault(r["name"], r["library"] or "")
    except Exception:                                     # noqa: BLE001
        # An index built before data files were inventoried has no such table.
        pass
    # A source-only extraction has no data members at all. Its data files are
    # the ones CL builds and opens, so those are data files on the map too -
    # with the library the CL names, when it names one literally.
    if not file_objects:
        try:
            for r in q("SELECT target AS name, library FROM file_provenance "
                       "UNION SELECT target_file AS name, library FROM override"):
                if r["name"]:
                    lib = r["library"] or ""
                    if lib.startswith(("&", "*")):
                        lib = ""
                    file_objects.setdefault(r["name"], lib)
        except Exception:                                 # noqa: BLE001
            pass

    def node(name: str, kind: str = "?") -> Optional[int]:
        """Index of `name`, inventing a placeholder node if it is not a member.

        Rule 2: a call to something we cannot find is a fact about the
        codebase, not a parse failure to be swallowed.
        """
        if not name:
            return None
        if name in index:
            return index[name]
        index[name] = len(names)
        names.append(name)
        # A name with a file object on disk is a DATA FILE, not an unknown -
        # and its library is known even though its layout is not.
        held = file_objects.get(name)
        types.append("DATAFILE" if held is not None else kind)
        also.append("")
        subs.append("")
        libs.append(held or "")
        tiers.append("data" if held is not None else "")
        depths.append(None)
        lines.append(0)
        paths.append("")
        status.append("data file" if held is not None else "external")
        alias.append("")
        return index[name]

    # -- edges -------------------------------------------------------------
    edges: Dict[str, Dict[str, List]] = {
        lay["key"]: {"pairs": [], "attr": [], "conf": [], "line": []}
        for lay in LAYERS
    }

    def add(layer: str, src: Optional[int], dst: Optional[int],
            attr: str, conf: str = "exact", line: int = 0) -> None:
        """One edge.

        `line` is the source line the fact was read from, and it is what makes
        the sequence diagram a real sequence rather than a plausible one: the
        viewer sorts a participant's messages by it, so an OVRDBF on line 40
        draws above a CALL on line 120 because that is the order the program
        actually does them in.

        Zero means "not known", which is honest for the handful of facts that
        are derived rather than read - `based_on` comes from a column, not from
        a statement, so it has no line and is drawn last.
        """
        if src is None or dst is None or src == dst:
            return
        spec = next(l for l in LAYERS if l["key"] == layer)
        try:
            a = spec["attrs"].index(attr)
        except ValueError:
            a = len(spec["attrs"]) - 1        # the trailing 'other'/'unknown'
        edges[layer]["pairs"].append((src, dst))
        edges[layer]["attr"].append(a)
        edges[layer]["line"].append(int(line or 0))
        edges[layer]["conf"].append(
            config.CONFIDENCE_LEVELS.index(conf)
            if conf in config.CONFIDENCE_LEVELS else 0)

    seen: set = set()

    for r in q("SELECT caller, callee, kind, MIN(confidence) AS confidence, "
               "MIN(COALESCE(variable,'')) AS variable, MIN(line) AS line "
               "FROM call_edge GROUP BY caller, callee, kind"):
        # An unresolved dynamic call still gets an edge - to a node named for
        # the variable it goes through, so the hole is visible on the map.
        target = r["callee"] or f"?{r['variable'] or 'DYNAMIC'}"
        key = ("call", r["caller"], target, r["kind"])
        if key in seen:
            continue
        seen.add(key)
        add("call", node(r["caller"]), node(target, "?"), r["kind"],
            r["confidence"], r["line"])

    for r in q("SELECT program, copybook, kind, MIN(confidence) AS confidence, "
               "MIN(line) AS line FROM copy_edge "
               "GROUP BY program, copybook, kind"):
        add("copy", node(r["program"]), node(r["copybook"], "CPY"),
            r["kind"], r["confidence"], r["line"])

    for r in q("SELECT program, file, mode, MIN(confidence) AS confidence, "
               "MIN(line) AS line FROM file_usage "
               "GROUP BY program, file, mode"):
        add("file", node(r["program"]), node(r["file"], "PF"),
            r["mode"], r["confidence"], r["line"])

    for r in q("SELECT cl_program, target_file, kind, MIN(confidence) AS confidence, "
               "MIN(line) AS line FROM override "
               "GROUP BY cl_program, target_file, kind"):
        add("override", node(r["cl_program"]), node(r["target_file"], "PF"),
            r["kind"], r["confidence"], r["line"])

    for r in q("SELECT program, device_file, MIN(line) AS line FROM format_usage "
               "WHERE device_file<>'' GROUP BY program, device_file"):
        add("format", node(r["program"]), node(r["device_file"], "DSPF"),
            "record", "exact", r["line"])

    # `object.based_on` carries two different things and they must not be drawn
    # as the same edge.  For an LF it is the PFILE - a real parent.  For an SQL
    # member it is the member's own FOR SYSTEM NAME, an alias for itself, which
    # is not a parent at all.  See the note in docs/kb/ - the column is
    # overloaded and index/build.py only validates the LF case.
    dangling_alias: List[str] = []
    for r in q("SELECT name, member_type, based_on FROM object "
               "WHERE based_on<>''"):
        src = node(r["name"])
        target = r["based_on"]
        if r["member_type"] == "LF":
            add("based_on", src, node(target, "PF"), "PFILE")
        else:
            # An alias, so it belongs on the node, not on an invented node of
            # its own.  If the alias resolves to a real member we draw it as
            # its own layer; if it does not, we say so rather than inventing.
            if src is not None:
                alias[src] = target
            if target in index:
                add("sqlalias", src, index[target], "system_name", "inferred")
            else:
                dangling_alias.append(f"{r['name']} -> {target}")

    for r in q("SELECT program, msgid, MIN(line) AS line FROM message_usage "
               "GROUP BY program, msgid"):
        add("message", node(r["program"]), node(r["msgid"], "MSGID"), "msgid",
            "exact", r["line"])

    # `data_area` records WHICH member uses a data area, not where. There is no
    # line column to read, so the line stays 0 - which the viewer draws last
    # rather than pretending it knows the position.
    for r in q("SELECT DISTINCT member, name FROM data_area"):
        add("dtaara", node(r["member"]), node(r["name"], "DTAARA"), "dtaara")

    db.close()

    # -- degree ------------------------------------------------------------
    # Two numbers per node, and they mean different things.  `deg` is total
    # structural touches and drives node size.  `touched` counts only inbound
    # structural edges - how many members depend on this one - and that is the
    # number the hub cut uses, because a copybook copied 3,211 times is a hub
    # whether or not it copies anything itself.
    deg = [0] * len(names)
    touched = [0] * len(names)
    for key in STRUCTURAL:
        for s, t in edges[key]["pairs"]:
            deg[s] += 1
            deg[t] += 1
            touched[t] += 1

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
        # Rule 2: the map has to show where it is blank.
        "gaps": {
            "dangling_alias": dangling_alias,
            "external": [names[i] for i, s in enumerate(status)
                         if s == "external" and types[i] == "?"],
            "unresolved_calls": sum(
                1 for i, a in enumerate(edges["call"]["conf"])
                if a == config.CONFIDENCE_LEVELS.index(config.UNRESOLVED)),
        },
    }


# ---------------------------------------------------------------------------
# Hub cut
# ---------------------------------------------------------------------------
#
# `hub_cut` now lives in kb.index.degree and is re-exported here, because the
# layer model needs the same threshold to decide the `service` tier.  Two
# thresholds computed two ways would put a copybook in one tier on this map and
# another in the index, and an agent cannot tell which to believe.


def summary(g: Dict) -> str:
    counts = Counter(t + ("+" + a if a else "")
                     for t, a in zip(g["types"], g["also"]))
    per_layer = {l["key"]: len(g["edges"][l["key"]]["pairs"]) for l in LAYERS}
    total = sum(per_layer.values())
    out = [f"nodes {len(g['names']):,}   edges {total:,}", "",
           "  by member type:"]
    for kind, n in counts.most_common():
        out.append(f"    {kind:<10} {n:>6,}")
    out.append("  by edge layer:")
    for lay in LAYERS:
        out.append(f"    {lay['label']:<16} {per_layer[lay['key']]:>7,}")
    cut = hub_cut(g["touched"])
    hubs = sum(1 for d in g["touched"] if d >= cut)
    out.append(f"  hub cut (99th pct in-degree): {cut}  -> {hubs} hubs")
    gaps = g["gaps"]
    out.append("  gaps:")
    out.append(f"    call targets not in the source tree : "
               f"{len(gaps['external'])}")
    out.append(f"    SQL system names that resolve to nothing: "
               f"{len(gaps['dangling_alias'])}")
    return "\n".join(out)


if __name__ == "__main__":
    print(summary(load()))
