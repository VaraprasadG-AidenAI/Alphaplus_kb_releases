"""
Build the whole-codebase map: a single self-contained HTML file.

    python -m kb.viz.build                  # atlas layout only, seconds
    python -m kb.viz.build --force-layout   # adds the force layout (needs numpy)
    python -m kb.viz.build --neo4j          # also write the Neo4j import set

The output has no external references of any kind - no CDN, no fetch, no
web font - so it opens straight off the filesystem and survives being emailed
to someone behind a corporate proxy.  That matters more than it sounds: the
client may never let this run on a machine with internet access.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Sequence

from kb import config
from kb.viz import graph as G

from kb.viz import layout as L
#: Where the map's facts come from.
#:
#: `sqlite` - the index this build just wrote - and nothing else by default.
#: It is the only store the shipped product has: the exe bundles no Neo4j, no
#: driver and no JVM.
#:
#: The argument for defaulting to the graph was that a picture of the system
#: should be drawn from the system of record. That holds only where a graph
#: exists. Here it meant the last step of a successful index opened a bolt
#: connection to a server that was never installed, and ended a 662-second
#: run in a traceback.
#:
#: `--from neo4j` still works for a deployment that genuinely has one.
def _loader(which: str):
    if which == "sqlite":
        return G.load
    from kb.viz import fromgraph
    return fromgraph.load


HERE = Path(__file__).parent
TEMPLATE = HERE / "template.html"    # the classic single-file viewer
SHELL = HERE / "shell.html"          # the React shell
BUNDLE = HERE / "bundle.js"          # built by viz-src/build.mjs

#: Draw order and legend order.  Data first, then the things that act on it.
TYPE_ORDER = [
    # DATAFILE sits beside the file types because that is what it is: the
    # physical object a PF describes. It has no source member, so it
    # never appears in `object` - it is proven by its own directory on
    # disk, and without it every data file the application reads was
    # drawn as an unknown with no library.
    "PF", "LF", "DATAFILE", "SQL",
    "CBL", "CBLLE", "SQLCBLLE",
    "CLP", "CLLE",
    "RPG", "RPGLE", "SQLRPGLE",
    "CPY",
    "DSPF", "PRTF", "MNUDDS",
    "CMD", "MNUCMD", "BND",
    "MSGF", "MSGID", "DTAARA", "?",
]


def _compact(values: Sequence[int]):
    """A list of 132,000 identical zeroes is a scalar, not a list."""
    if not values:
        return 0
    first = values[0]
    for v in values:
        if v != first:
            return list(values)
    return first


#: Optional per-engagement module names, read from a JSON file beside the index:
#:
#:     <output>/module-names.json      {"ORDMEN": "Order management", ...}
#:
#: There is deliberately no built-in table. The replica's prefixes happen to be
#: readable English, and hardcoding them would produce a viewer that names
#: modules correctly on the replica and silently wrongly on the client's 21
#: libraries - which is worse than not naming them at all.
#:
#: So: the prefix is the name until somebody who knows the business writes this
#: file. `_flows` marks every unnamed module so the UI can say the name is a
#: code, not a description.
MODULE_NAMES_FILE = "module-names.json"


def _module_names() -> Dict[str, str]:
    """Whatever the engagement has chosen to call its modules, or nothing."""
    try:
        path = Path(config.DATA_DIR) / MODULE_NAMES_FILE
        if not path.is_file():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {str(k).upper(): str(v) for k, v in raw.items() if v}
    except (OSError, ValueError):
        return {}


def _business_names(db) -> Dict[str, Dict[str, Dict[str, str]]]:
    """Inferred business names, written by `kb.calltree.name`.

    Two maps, because the viewer needs them in two places. `caps` names the
    module cards - "Order Management" rather than ORD. `members` names every
    node in every view, which is the whole point: a card headed CS95IC tells a
    reader nothing, and the same card headed "Pricing Accuracy Management"
    with CS95IC kept beside it tells them what they came to find out.

    Absent table, absent names: the map is built from facts and still works.
    Nothing here is load-bearing for the graph itself.
    """
    out = {"caps": {}, "members": {}}
    try:
        rows = db.execute(
            "SELECT key, kind, title, line, confidence FROM calltree_name"
        ).fetchall()
    except Exception:                                   # noqa: BLE001
        return out                                      # not named yet
    for r in rows:
        entry = {"t": r["title"], "l": r["line"] or "",
                 "c": r["confidence"] or "medium"}
        bucket = "caps" if r["kind"] == "capability" else "members"
        out[bucket][r["key"]] = entry
    return out


def _families(menus: Sequence[str]) -> Dict[str, str]:
    """menu name -> the family it belongs to, derived from the names themselves.

    No convention is assumed. The rule that IS general on IBM i is that a menu
    family is a common prefix with a sequence number on the children:

        CUSMENU  CUSMEN01  CUSMEN02 ...
        APMAIN   APMAIN01  APMAIN02 ...

    So: strip trailing digits, then merge two stems when one is the other plus a
    single trailing character AND the shorter one has children of its own. That
    collapses CUSMENU into CUSMEN without knowing the letter is a U, and it
    leaves APMAIN alone because nothing is APMAI.

    Derived rather than hardcoded because the client's menus are not called
    anything we have seen. A rule that reads the data works on 21 libraries; a
    table of prefixes works on this one.
    """
    stems: Dict[str, List[str]] = {}
    for m in menus:
        stem = m.rstrip("0123456789") or m
        stems.setdefault(stem, []).append(m)

    # a stem with numbered children is a real family root
    numbered = {st for st, ms in stems.items()
                if any(x != st for x in ms)}

    merge: Dict[str, str] = {}
    for st in stems:
        if st in numbered:
            continue
        # CUSMENU -> CUSMEN, but only if CUSMEN is itself a family
        if len(st) > 4 and st[:-1] in numbered:
            merge[st] = st[:-1]

    out: Dict[str, str] = {}
    for st, ms in stems.items():
        fam = merge.get(st, st)
        for m in ms:
            out[m] = fam
    return out


def _processes() -> Dict:
    """The process map: capability -> trigger point -> the flow beneath it.

    Two sources, and the split matters. The STRUCTURE - which CL runs, which
    program it calls, which files that touches - comes from the parsed facts,
    because those edges exist nowhere else; a summary is prose about one member
    and contains no edges at all. The MEANING - what each of those members is,
    in business words - comes from the summaries. Generating the structure from
    summaries would mean inventing the edges.

    Empty dict when the names have not been written yet, and the viewer then
    falls back rather than showing an empty tab.
    """
    try:
        from kb.calltree import export as ct_export
        from kb.calltree import page as ct_page
        # facts(), not connect(): connect() is the PostgreSQL opener and
        # imports psycopg unconditionally, so on a SQLite install - which is
        # every shipped exe - this raised ModuleNotFoundError.
        from kb.store import facts
        db = facts()
        try:
            return ct_page.slim(ct_export.document(db))
        finally:
            db.close()
    except Exception as exc:                            # noqa: BLE001
        # Say what this IS, not which table was missing.
        #
        # The processes tab is written by the calltree naming pipeline, which
        # needs a language model to title each flow and stores the result in
        # `calltree_name`. The standalone build ships neither, so its absence
        # is the designed state of this product and not a fault - but the
        # message here was a raw exception, which reads to a client as a
        # broken install. Two of them in a row on one estate
        # ("ModuleNotFoundError: No module named 'psycopg'", then
        # "no such table: calltree_name") said nothing anyone could act on.
        missing = ("calltree_name" in str(exc)
                   or "psycopg" in str(exc)
                   or "call_edge" in str(exc))
        if missing:
            print("  processes: not built - this map is drawn from the index "
                  "alone.")
            print("             The processes tab needs the flow-naming pass "
                  "(python -m kb.calltree.name),")
            print("             which is not part of the standalone build. "
                  "Every other tab is complete.")
        else:
            print(f"  processes: not built ({type(exc).__name__}: {exc})")
        return {}


def _titles_by_index(names: Sequence[str], db_path=None) -> Dict:
    """Business names for the graph nodes, keyed by node index.

    Every view reads this, not only the flow view. The review that prompted it
    was about a node in the call tree: "I'm not talking about what type of file
    it is - I am talking what is the functionality of the file? What does it
    do?" A node labelled CS95IC cannot answer that; the same node labelled
    "Pricing Accuracy Management", with CS95IC kept as a property, can.

    `c` is the confidence, carried through so the UI can mark a name as
    inferred. A name that might be wrong is useful; a name that is silently
    presented as fact is not.
    """
    out: Dict[str, List] = {}
    try:
        db = G._conn(db_path)
        business = _business_names(db)
    except Exception:                                   # noqa: BLE001
        return out
    members = business["members"]
    for i, member in enumerate(names):
        got = members.get(member)
        if got:
            out[str(i)] = [got["t"], got["l"], got["c"]]
    return out


def _flows(names: Sequence[str], db_path=None) -> Dict:
    """The business decomposition: module -> component -> process.

    This is the part a business reader wants and the call graph cannot give
    them. A call graph says `ORD502C calls ORD502`. It does not say that a user
    picks option 2 of the Orders menu, that the option is called "Back Order
    posting", or that the whole thing belongs to Orders.

    All three levels come out of `menu_option`, which is the only table that
    holds business language:

        module     the menu family prefix - ORDMEN, CUSMEN, FINMEN
        component  one menu within it - ORDMEN01, ORDMEN02
        process    one option, with the LABEL a user actually reads

    The steps under a process - which CL driver, which COBOL program, which
    files - are NOT duplicated here. They are already in the call and file
    layers, and the viewer walks them. Copying them would double the payload and
    give two versions of one fact.
    """
    out: Dict = {"modules": [], "inferredNames": True}
    where = {name: i for i, name in enumerate(names)}
    chosen = _module_names()
    business: Dict[str, Dict[str, Dict[str, str]]] = {"caps": {}, "members": {}}
    try:
        db = G._conn(db_path)
        business = _business_names(db)
        rows = db.execute("""
            SELECT menu, library, option, label, command, action, target,
                   target_library, confidence
              FROM menu_option
             ORDER BY menu, CAST(option AS INTEGER), option
        """).fetchall()
    except Exception:                                   # noqa: BLE001
        # Exception, not sqlite3.DatabaseError: the store may be
        # PostgreSQL now, and a psycopg failure would escape the narrow
        # clause and take the whole map build down instead of degrading.
        return out

    # The family map is derived from the menu names actually present, so this
    # works on any naming scheme rather than the one this replica happens to use.
    family = _families(sorted({r["menu"] for r in rows if r["menu"]}))
    # The heading each menu shows its operator. Without this a component card
    # reads FINMEN01, and eleven of them read as a wall of codes.
    titles = _screen_titles(db)

    fams: Dict[str, Dict[str, List]] = {}
    for r in rows:
        menu = r["menu"] or ""
        if not menu:
            continue
        fam = family.get(menu, menu)
        fams.setdefault(fam, {}).setdefault(menu, []).append(r)

    for fam in sorted(fams):
        comps = []
        for menu in sorted(fams[fam]):
            procs = []
            for r in fams[fam][menu]:
                target = r["target"] or ""
                # `signoff` and `go` options are navigation, not business
                # processes - a "Return to the main menu" option is not a thing
                # anybody needs impact analysis on.
                action = (r["action"] or "").lower()
                if action in ("signoff",) or not target:
                    continue
                if target in where and family.get(target) == fam:
                    continue                     # a link back into this menu
                # The option's own label is business language already - it is
                # what the operator reads on the screen - so it stays the
                # process title. What was missing is the same courtesy one
                # level down: the target was shown as a bare id.
                named = business["members"].get(target)
                procs.append({
                    "opt": r["option"],
                    "label": (r["label"] or "").strip(),
                    "target": target,
                    "targetTitle": named["t"] if named else "",
                    "targetLine": named["l"] if named else "",
                    "node": where.get(target, -1),
                    "action": action,
                    "cmd": (r["command"] or "").strip()[:60],
                    "conf": r["confidence"] or "",
                })
            if not procs:
                continue
            comps.append({
                "menu": menu,
                # The heading the operator reads on that screen, from the DDS
                # constant - so a card says "Accounts Receivable And General
                # Ledger Inter" rather than FINMEN01. `named` is False when we
                # could not find one, and the UI then says the name is a code.
                "name": titles.get(menu, menu),
                "named": menu in titles,
                "node": where.get(menu, -1),
                "library": r["library"] or "",
                "processes": procs,
            })
        if not comps:
            continue
        # A heading that every screen in the family shares is context, not a
        # name. The replica does exactly this: all ten FINMENnn menus put
        # "ACCOUNTS RECEIVABLE AND GENERAL LEDGER INTER" on row 1, so titling
        # the cards by it produced ten identical cards - worse than the codes
        # it replaced. Where the heading does not distinguish, the menu id
        # stays the title and the heading becomes the subtitle.
        seen: Dict[str, int] = {}
        for c in comps:
            seen[c["name"]] = seen.get(c["name"], 0) + 1
        for c in comps:
            c["unique"] = c["named"] and seen.get(c["name"], 0) == 1
        # ...and a heading every screen in the family shares is precisely a name
        # for the FAMILY. That is the useful half of this: it names the nine
        # modules from executable DDS, so the hand-written module-names.json
        # becomes an override rather than the only source. A human-written name
        # still wins where one exists.
        shared = ""
        if seen and not any(c["unique"] for c in comps):
            top = max(seen.items(), key=lambda kv: (kv[1], -len(kv[0])))
            if top[1] > 1 or len(comps) == 1:
                shared = top[0]
        # A module's family stem is ORDMEN / APMAIN; the capability it was
        # named under is ORD / AP. Same rule both sides, applied here rather
        # than assumed.
        cap_key = re.sub(r"(?:MEN|MAIN).*$", "", fam) or fam
        inferred = business["caps"].get(cap_key)
        out["modules"].append({
            "key": fam,
            # A human-written name still wins. Below that the inferred
            # business name beats a shared screen heading, which beats the
            # code - because "Order Management" is what a reader is looking
            # for and ORDMEN is what they are trying to get away from.
            "name": (chosen.get(fam) or (inferred["t"] if inferred else "")
                     or shared or fam),
            "desc": inferred["l"] if inferred else "",
            "named": bool(chosen.get(fam) or inferred or shared),
            "nameFrom": ("file" if fam in chosen
                         else "inferred" if inferred
                         else "screen" if shared else "code"),
            "components": comps,
            # The subsystems the module's programs actually belong to. This IS a
            # fact from the source, so it is offered as the description where a
            # human-written name is absent.
            "subsystems": _subsystems_of(comps, names, db),
        })
    out["namesFile"] = MODULE_NAMES_FILE
    out["hasNames"] = bool(chosen)
    return out


#: Constants that sit on a menu's title row but are not its title: the product
#: banner in the right-hand columns, and rule-off lines. Matched by shape, not
#: by name, so this holds on a codebase whose banner says something else.
def _is_title_constant(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 4:
        return False
    # a rule-off line - dashes, equals, underscores and spaces only
    if not any(ch.isalnum() for ch in t):
        return False
    return True


def _screen_titles(db) -> Dict[str, str]:
    """The heading a user actually reads at the top of each menu.

    Read from **executable DDS**, not from the header comment. Two sources, in
    this order:

      1. the leftmost constant on the topmost row of the menu's record format -
         literally the title line of the green screen, e.g.
         `1  3'ACCOUNTS RECEIVABLE AND GENERAL LEDGER INTER'`
      2. the format's own `TEXT('...')` keyword

    Rule 1 matters here. Every replica menu also carries
    `A*  MENU : FINMEN01  ACCOUNTS RECEIVABLE AND GENERAL LEDGER I` in its
    comment block, and that is the easy thing to read and the wrong thing to
    read - the real codebase will not have it, and a comment can be stale where
    a screen constant cannot be. The constant IS what the operator sees.

    Titles are shouted in DDS (`ACCOUNTS RECEIVABLE AND...`), so they are cased
    for display. Words that are codes rather than words are left alone.
    """
    titles: Dict[str, str] = {}
    try:
        rows = db.execute("""
            SELECT device_file, format_name, row, col, constant
              FROM format_field
             WHERE constant <> '' AND field = ''
             ORDER BY device_file, row, col
        """).fetchall()
    except sqlite3.DatabaseError:
        return titles

    best: Dict[str, tuple] = {}
    for r in rows:
        dev = r["device_file"]
        if not dev:
            continue
        text = (r["constant"] or "").strip()
        if not _is_title_constant(text):
            continue
        key = (int(r["row"] or 0), int(r["col"] or 0))
        # topmost row wins, then leftmost column: the title sits above and to
        # the left of the banner.
        if dev not in best or key < best[dev][0]:
            best[dev] = (key, text)
    for dev, (_, text) in best.items():
        titles[dev] = _title_case(text)

    # the TEXT keyword, only where no constant was found
    try:
        for r in db.execute(
                "SELECT device_file, keywords FROM format "
                "WHERE kind = 'record' AND keywords LIKE '%TEXT(%'"):
            dev = r["device_file"]
            if dev in titles:
                continue
            m = re.search(r"TEXT\(\s*'([^']*)'", r["keywords"] or "")
            if m and _is_title_constant(m.group(1)):
                titles[dev] = _title_case(m.group(1).strip())
    except sqlite3.DatabaseError:
        pass
    return titles


#: Words that stay upper when a shouted screen title is cased for display.
#: Short all-caps tokens in this domain are nearly always acronyms or codes.
_KEEP_UPPER = {"AR", "AP", "GL", "PO", "EDI", "VAT", "GST", "SQL", "CL", "ID",
               "EOD", "EOM", "EOY", "FIFO", "LIFO", "OK", "PC", "UK", "US"}


#: Small words that stay lower inside a title. "Accounts Receivable And General
#: Ledger" reads as a machine casing it; "and" reads as a name.
_SMALL = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on",
          "or", "the", "to", "vs", "with"}


def _title_case(text: str) -> str:
    """Case a shouted DDS constant for display, leaving codes alone."""
    words = text.split()
    out: List[str] = []
    for i, w in enumerate(words):
        bare = w.strip(".,:;()[]/")
        if bare.upper() in _KEEP_UPPER:
            out.append(w.upper())
        elif any(ch.isdigit() for ch in bare):
            out.append(w.upper())          # a code, not a word
        elif not w.isupper():
            out.append(w)                  # already mixed case - leave it
        elif i > 0 and bare.lower() in _SMALL:
            out.append(w.lower())
        else:
            out.append(w.capitalize())
    s = " ".join(out)
    return s[:1].upper() + s[1:] if s else s


def _subsystems_of(comps: List[Dict], names: Sequence[str], db) -> List[str]:
    """Which subsystems a module's processes land in, commonest first."""
    targets = {p["target"] for c in comps for p in c["processes"] if p["target"]}
    if not targets:
        return []
    counts: Dict[str, int] = {}
    holes = ", ".join("?" for _ in targets)
    try:
        for r in db.execute(
                f"SELECT subsystem, COUNT(*) c FROM object "
                f"WHERE name IN ({holes}) AND subsystem <> '' "
                f"GROUP BY subsystem ORDER BY c DESC", tuple(targets)):
            counts[r["subsystem"]] = r["c"]
    except Exception:                                   # noqa: BLE001
        # Exception, not sqlite3.DatabaseError: the store may be
        # PostgreSQL now, and a psycopg failure would escape the narrow
        # clause and take the whole map build down instead of degrading.
        return []
    return list(counts)[:6]


def _descriptions(names: Sequence[str], db_path=None) -> Dict[str, str]:
    """The first sentences of any written page, keyed by node index.

    Only what is on screen matters, so this is an opening rather than a whole
    page: clicking a menu should say what the menu is for without the map
    carrying 113 full pages it will almost never show.

    Absent by design when summaries were never generated - the map is fully
    useful without them, and a missing description is not a gap to apologise
    for.
    """
    out: Dict[str, str] = {}
    try:
        db = G._conn(db_path)
        rows = db.execute("SELECT key, scope, text FROM summary").fetchall()
    except Exception:                                   # noqa: BLE001
        # Exception, not sqlite3.DatabaseError: the store may be
        # PostgreSQL now, and a psycopg failure would escape the narrow
        # clause and take the whole map build down instead of degrading.
        return out                          # an index built before the table
    where = {name: i for i, name in enumerate(names)}
    for row in rows:
        index = where.get(row["key"])
        if index is None:
            continue
        body = " ".join(line for line in row["text"].split("\n")
                        if line.strip() and not line.startswith("#"))
        out[str(index)] = f"[{row['scope']}] {body[:340]}"
    return out


def _graph_fingerprint(g) -> str:
    """The graph's fingerprint at the moment this map was built, or "".

    Empty when the map came from SQLite or the graph was unreachable: an empty
    fingerprint means "cannot compare", and `sync` treats that as "do not
    claim stale", never as "changed".
    """
    if (g or {}).get("source") != "neo4j":
        return ""
    try:
        from kb.graph import fingerprint as fp
        return fp.as_text(fp.current())
    except Exception:                                     # noqa: BLE001
        return ""


def payload(g: Dict, want_force: bool, db_path=None) -> Dict:
    types, subs = g["types"], g["subs"]
    libs, tiers = g["libs"], g["tiers"]

    type_table = TYPE_ORDER + sorted(set(types) - set(TYPE_ORDER))
    type_index = {t: i for i, t in enumerate(type_table)}
    sub_table = sorted(set(subs))
    sub_index = {s: i for i, s in enumerate(sub_table)}
    lib_table = sorted(set(libs))
    lib_index = {s: i for i, s in enumerate(lib_table)}
    tier_table = [t for t in L.TIER_ORDER if t in set(tiers)]
    tier_table += sorted(set(tiers) - set(tier_table))
    tier_index = {s: i for i, s in enumerate(tier_table)}

    atlas_xy = L.atlas(types, subs, TYPE_ORDER)
    # A second atlas grouped by library rather than by name prefix.  Same
    # function - it always took the grouping as a plain sequence - so this is a
    # different argument, not different code.
    library_xy = L.atlas(types, libs, TYPE_ORDER)
    layered_xy = L.layered(tiers, libs)

    force_xy: List[float] = []
    if want_force:
        structural = []
        for key in G.STRUCTURAL:
            structural.extend(g["edges"][key]["pairs"])
        force_xy = L.force(len(g["names"]), structural, g["deg"], atlas_xy)
        if not force_xy:
            print("  numpy not installed - force layout skipped", file=sys.stderr)

    # Paths are stored relative to SOURCE_ROOT, so most need nothing done.
    # The prefix strip stays for indexes built before that change, and for the
    # odd member that genuinely sits outside the source root.
    root = str(config.SOURCE_ROOT)
    rel = []
    for p in g["paths"]:
        if p.startswith(root):
            p = p[len(root):].lstrip("\\/")
        rel.append(p.replace("\\", "/"))

    layers = []
    for spec in G.LAYERS:
        e = g["edges"][spec["key"]]
        flat: List[int] = []
        for s, t in e["pairs"]:
            flat.append(s)
            flat.append(t)
        layers.append({
            "key": spec["key"],
            "label": spec["label"],
            "verb": spec["verb"],
            "attrs": spec["attrs"],
            "structural": spec["key"] in G.STRUCTURAL,
            "pairs": flat,
            "attr": _compact(e["attr"]),
            "conf": _compact(e["conf"]),
            # The source line each fact was read from. This is what lets the
            # sequence diagram order a participant's messages truthfully - an
            # OVRDBF on line 40 above a CALL on line 120 - instead of grouping
            # them by kind and admitting the order is a guess.
            "line": _compact(e["line"]),
        })

    return {
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        # WHICH graph this picture was taken of.
        #
        # `sync` compares this against the graph's fingerprint now - the only
        # way to tell the map is stale after a change that touched Neo4j and
        # not facts.db, which is precisely the case that shipped a stale map.
        #
        # FIRST in the dict on purpose. Python preserves insertion order and
        # json.dumps follows it, so this lands in the first few hundred bytes
        # and `sync._map_fingerprint` can read it with a small head read
        # instead of parsing 4 MB of JSON on every watch tick. It was 1.47 MB
        # in when it sat lower down, and the head read silently missed it.
        "graphFingerprint": _graph_fingerprint(g),
        "source": root,
        "n": len(g["names"]),
        "typeTable": type_table,
        "subTable": sub_table,
        "libTable": lib_table,
        "tierTable": tier_table,
        "confTable": list(config.CONFIDENCE_LEVELS),
        "name": g["names"],
        "ty": [type_index[t] for t in types],
        "sub": [sub_index[s] for s in subs],
        "lib": [lib_index[s] for s in libs],
        "tier": [tier_index[s] for s in tiers],
        # -1 rather than null: the array is read as numbers in the viewer, and
        # "nothing in the source reaches this" is a real answer that has to
        # survive the trip rather than become a hole.
        "depth": [-1 if d is None else d for d in g["depths"]],
        "ln": g["lines"],
        "deg": g["deg"],
        "tin": g["touched"],
        "path": rel,
        "also": {str(i): a for i, a in enumerate(g["also"]) if a},
        "alias": {str(i): a for i, a in enumerate(g["alias"]) if a},
        "atlas": atlas_xy,
        "library": library_xy,
        "layered": layered_xy,
        "force": force_xy,
        "islands": L.islands(subs, atlas_xy),
        "libIslands": L.islands(libs, library_xy),
        "bands": L.bands(tiers, layered_xy),
        "lanes": L.lanes(libs, layered_xy),
        "notes": _descriptions(g["names"], db_path),
        "flows": _flows(g["names"], db_path),
        # The business process view: capability -> trigger point -> flow.
        # Structure from the parsed facts, names from the summaries.
        "processes": _processes(),
        # member -> business name, for every view rather than just the flow
        # one. Keyed by node index so it costs an integer per entry instead of
        # repeating 13,366 member names that are already in `names`.
        "titles": _titles_by_index(g["names"], db_path),
        "layers": layers,
        "hubCut": G.hub_cut(g["touched"]),
        "gaps": {
            "danglingAlias": g["gaps"]["dangling_alias"][:200],
            "danglingAliasTotal": len(g["gaps"]["dangling_alias"]),
            "external": g["gaps"]["external"][:200],
            "externalTotal": len(g["gaps"]["external"]),
        },
    }


def build(out: Path, want_force: bool = False, db_path=None,
          classic: bool = False, source: str = "sqlite") -> Path:
    """Write the map as ONE self-contained HTML file.

    Two front ends share this one data payload:

      shell.html + bundle.js   the React map - hierarchy, call tree, clusters
      template.html            the original single-file canvas viewer

    The React one is the default.  The classic one is kept, and kept working,
    because it has no build step: if node is not installed on whichever machine
    has to regenerate this, `--classic` still produces a usable map. A viewer
    that can only be rebuilt on one laptop is not a deliverable.
    """
    g = _loader(source)(db_path)
    data = payload(g, want_force, db_path)
    blob = json.dumps(data, separators=(",", ":"))
    # `</` split so the payload can never terminate the script element, whatever
    # a member happens to be called.
    safe = blob.replace("</", "<\\/")

    if classic or not (SHELL.is_file() and BUNDLE.is_file()):
        # Only a developer can act on this, and only in a checkout.
        # In the shipped exe there is no viz-src to cd into and no
        # npm build to run, so it reads to a client as something
        # broken that they must fix. The classic viewer is a
        # complete deliverable, not a degraded one.
        if not classic and not config.FROZEN:
            print("  react bundle missing - falling back to the classic viewer.\n"
                  "  build it with:  cd viz-src && npm install && npm run build",
                  file=sys.stderr)
        html = TEMPLATE.read_text(encoding="utf-8")
        if "__GRAPH_DATA__" not in html:
            raise SystemExit("template.html has no __GRAPH_DATA__ placeholder")
        html = html.replace("__GRAPH_DATA__", safe)
    else:
        html = SHELL.read_text(encoding="utf-8")
        for token in ("__GRAPH_DATA__", "__BUNDLE__"):
            if token not in html:
                raise SystemExit(f"shell.html has no {token} placeholder")
        js = BUNDLE.read_text(encoding="utf-8")
        # The bundle is inlined, not linked, so the page is one file. Its own
        # `</script>` would close ours; there is none in minified React, but
        # escaping it costs nothing and removes the question.
        html = html.replace("__GRAPH_DATA__", safe)
        html = html.replace("__BUNDLE__", js.replace("</script", "<\\/script"))

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--out", type=Path,
                    default=config.DATA_DIR / "graph.html")
    ap.add_argument("--force-layout", action="store_true",
                    help="also compute the force-directed layout (needs numpy)")
    ap.add_argument("--neo4j", action="store_true",
                    help="also write CSVs and a load script for Neo4j")
    ap.add_argument("--classic", action="store_true",
                    help="the original single-file viewer, no node needed")
    ap.add_argument("--db", type=Path, default=None)
    # Default to the relational store, not the graph.
    #
    # This exe is the standalone build: one file, one index, no services. It
    # reads `facts.db` and nothing else. Defaulting to Neo4j meant the last
    # step of `setup` opened a bolt connection to a server that is not there -
    # on the client VDI Neo4j is blocked outright - and killed the run with a
    # traceback after a successful 662-second index.
    #
    # `--from neo4j` still works for the deployment that has a graph. It is
    # now opt-in, which is the right way round for a binary whose whole point
    # is that it needs nothing installed.
    ap.add_argument("--from", dest="source",
                    choices=("neo4j", "sqlite"), default="sqlite",
                    help="where the facts come from (default: sqlite, the "
                         "index this exe just built)")
    args = ap.parse_args(argv)

    # Where the facts come from, and what to do when they are not there.
    #
    # The default is the graph because it is the system of record when it is
    # running. It very often is not: on the client VDI Neo4j is blocked
    # outright, and `setup` calls this at the end of a successful index - so a
    # first run ended in a traceback and an exit code, with a perfectly good
    # 287 MB index sitting on disk and nothing to show for it.
    #
    # The relational store holds the same facts. Falling back to it and saying
    # so is strictly better than failing, because the map is a convenience and
    # the index is the product.
    source = args.source
    if source == "neo4j":
        try:
            g = _loader(source)(args.db)
        except Exception as exc:                        # noqa: BLE001
            name = type(exc).__name__
            print(f"  Neo4j is not available ({name}), reading the facts from")
            print("  the relational store instead. The map is the same; only")
            print("  the source of the facts differs.")
            print()
            source = "sqlite"
            g = _loader(source)(args.db)
    else:
        g = _loader(source)(args.db)
    print(G.summary(g))

    out = build(args.out, args.force_layout, args.db, args.classic,
                source=source)
    size = out.stat().st_size / 1e6
    which = "classic" if (args.classic or not BUNDLE.is_file()) else "react"
    print(f"\nwrote {out}  ({size:.1f} MB, {which} viewer)")

    if args.neo4j:
        from kb.viz import neo4j
        where = neo4j.export(g, args.out.parent / "neo4j")
        print(f"wrote {where}  (see load.cypher)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
