"""
What each level of the pyramid is written from.

One renderer per level, and one of them - the flow - is the whole reason the
pyramid is affordable. A menu option's facts already read as a sentence:

     3  Audit recalculation        SEC407C -> SEC407    writes AUDMAS
        [APMAIN.MNUDDS:29, APMAIN.MNUCMD:7]

That is exact, carries its evidence, costs nothing and cannot hallucinate.
There are 1,105 of them and a model would take about eleven hours to write
worse versions. So flows are rendered, and the model is spent where synthesis
across many options is actually needed: menus, families, libraries.

The conventions follow prompts.fact_sheet, which these sit beside: fixed
section order, a section omitted when it is empty, and an `[inferred]` or
`[unresolved]` suffix wherever confidence is not exact.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Dict, List, Optional, Sequence, Tuple

from .. import config

#: A menu name with its trailing digits removed: APMAIN01 -> APMAIN.
#:
#: This is the family, and it exists because ALPH holds 61 menus. Rolling 61
#: menu summaries straight into one library page is ~9,000 tokens, six minutes
#: of prefill on this hardware and past the model's context; the eleven
#: families it collapses to are ~2,600. The grouping is a naming convention, so
#: it groups and is never asserted as a fact about anything.
_FAMILY_RE = re.compile(r"^(.*?)(\d+)$")


def family_of(menu: str) -> str:
    match = _FAMILY_RE.match(menu)
    return match.group(1) if match and match.group(1) else menu


def _mark(confidence: str) -> str:
    return "" if confidence == config.EXACT else f"  [{confidence}]"


def _rows(db: sqlite3.Connection, sql: str, *args) -> List[sqlite3.Row]:
    db.row_factory = sqlite3.Row
    return list(db.execute(sql, args))


# ---------------------------------------------------------------------------
# The flow level: rendered, never written
# ---------------------------------------------------------------------------

def flow_lines(db: sqlite3.Connection, menu: str) -> List[str]:
    """One line per option on a menu: what it says, what it runs, what it writes.

    The whole chain in one row, because that is the unit a ticket arrives in.
    Nobody reports "SEC407 is broken"; they report "option 3 on the audit menu
    does nothing".
    """
    out: List[str] = []
    for option in _rows(db,
                        "SELECT option, label, action, target, target_library, "
                        "       label_line, command_line, confidence "
                        "  FROM menu_option WHERE menu=? ORDER BY option",
                        menu):
        label = option["label"] or "(nothing on the screen)"
        # A GO to another menu, a SIGNOFF or a RETURN runs no program, so it
        # opens no files. Following its target's callees made option 11 claim
        # "Return to the main menu ... writes CURW04" - which is what the main
        # menu's own options write, not anything this option does.
        runs_a_program = option["action"] in ("call", "command")
        target = (option["target"] or "") if runs_a_program else ""
        chain = ""
        if target:
            # The driver's own target, so the line reaches the program that
            # holds the logic rather than stopping at the CL that sets up for it.
            onward = _rows(db,
                           "SELECT DISTINCT callee FROM call_edge "
                           " WHERE caller=? AND callee<>'' AND kind='cl' "
                           " LIMIT 2", target)
            chain = target
            if onward:
                chain += " -> " + ",".join(r["callee"] for r in onward)

        # device='database' on purpose: a program "writes" its display file on
        # every screen, and listing ORD100D beside ORDHDR makes the line read
        # as though the screen were data.
        writes = _rows(db,
                       "SELECT DISTINCT f.file FROM file_usage f "
                       " WHERE f.program IN (SELECT ? UNION "
                       "       SELECT callee FROM call_edge "
                       "        WHERE caller=? AND callee<>'') "
                       "   AND f.device='database' "
                       f"  AND {_CAN_CHANGE} LIMIT 4", target, target) \
            if target else []

        evidence = []
        if option["label_line"]:
            evidence.append(f"{menu}.MNUDDS:{option['label_line']}")
        if option["command_line"]:
            evidence.append(f"{menu}.MNUCMD:{option['command_line']}")

        # The label is never truncated. It was cut to 36 characters to keep the
        # table aligned, and the model read the cut as a defect in the
        # application - reporting a 40-character label as "misspelled or
        # truncated". Alignment is not worth manufacturing findings for, so the
        # label gets its own line and the facts get the next one.
        out.append(f"  option {option['option']}: {label}")

        if option["action"] in ("signoff", "return"):
            detail = f"signs off" if option["action"] == "signoff" \
                else "returns to the caller"
        elif option["action"] == "go":
            detail = f"goes to menu {option['target'] or '(unknown)'}"
        elif chain:
            detail = f"runs {chain}"
            if writes:
                detail += ", changes " + ", ".join(r["file"] for r in writes)
        else:
            detail = "runs nothing - the command half has no line here"
        out.append(f"      {detail}{_mark(option['confidence'])}")
        if evidence:
            out.append(f"      evidence: {', '.join(evidence)}")
    return out


#: "Can this program change this file", the same predicate the server tools
#: share.  Duplicated as a string rather than imported because kb/summarise
#: must not depend on kb/server - the only difference is the `f.` alias this
#: module's joins need.
#:
#: It carried a fourth clause, `f.verbs LIKE '%REWRITE%'`, which was dead:
#: '%WRITE%' already matches REWRITE, since REWRITE contains WRITE.  Harmless
#: to evaluate and actively misleading to read - it made this predicate look
#: BROADER than kb.server.tools._CAN_CHANGE when the two are equivalent, and a
#: reader comparing the two would reasonably have "fixed" the other one to
#: match.  Two tools reporting two different counts for "who writes CUSMAS" is
#: the failure both definitions exist to prevent.
_CAN_CHANGE = ("(f.mode IN ('O','U') OR f.verbs LIKE '%WRITE%' "
               "OR f.verbs LIKE '%DELETE%')")


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

def menu_sheet(db: sqlite3.Connection, menu: str) -> Tuple[str, str]:
    """(sheet, source) for one menu.

    The source is both halves of the member pair, in full - about 63 lines
    together, which is small enough to send whole. That is the one level where
    the model gets to read actual code, and it is the level where reading it
    helps most: the screen text is the only place the application says what it
    thinks an option is for.
    """
    info = _rows(db, "SELECT name, library FROM object "
                     " WHERE name=? AND member_type='MNUCMD' LIMIT 1", menu)
    library = info[0]["library"] if info else ""

    out = [f"menu      {menu}"]
    if library:
        out.append(f"library   {library}")
    out.append(f"family    {family_of(menu)}")

    lines = flow_lines(db, menu)
    if lines:
        out.append("\noptions, and what each one runs")
        out.extend(lines)

    reached = _rows(db,
                    "SELECT DISTINCT o.name, o.member_type, o.tier "
                    "  FROM menu_option m JOIN object o ON o.name=m.target "
                    " WHERE m.menu=? AND m.target<>'' LIMIT 20", menu)
    if reached:
        out.append("\nwhat the options reach")
        for r in reached:
            out.append(f"  {r['name']:<12}{r['member_type']:<10}{r['tier']}")

    unresolved = _rows(db, "SELECT kind, raw, reason FROM unresolved "
                           " WHERE member=? LIMIT 6", menu)
    if unresolved:
        out.append("\nthings we could not work out")
        for r in unresolved:
            out.append(f"  {r['kind']:<26}{(r['raw'] or '')[:30]}")

    source_parts: List[str] = []
    for member_type in ("MNUDDS", "MNUCMD"):
        row = _rows(db, "SELECT path FROM object WHERE name=? "
                        "AND member_type=? LIMIT 1", menu, member_type)
        if not row:
            continue
        path = config.resolve_path(row[0]["path"])
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        source_parts.append(f"--- {menu}.{member_type} ---\n{text}")

    return "\n".join(out), "\n".join(source_parts)


# ---------------------------------------------------------------------------
# Family
# ---------------------------------------------------------------------------

def family_menus(db: sqlite3.Connection, family: str) -> List[str]:
    menus = [r["menu"] for r in
             _rows(db, "SELECT DISTINCT menu FROM menu_option ORDER BY menu")]
    return [m for m in menus if family_of(m) == family]


def family_sheet(db: sqlite3.Connection, family: str) -> str:
    """The menu pages under one family, plus what they collectively touch."""
    menus = family_menus(db, family)
    out = [f"family    {family}",
           f"menus     {len(menus)}: {', '.join(menus)}"]

    libraries = _rows(db, "SELECT DISTINCT library FROM menu_option "
                          " WHERE menu IN (%s)"
                          % ",".join("?" * len(menus)), *menus) if menus else []
    if libraries:
        out.append("library   "
                   + ", ".join(r["library"] for r in libraries if r["library"]))

    out.append("\nwhat each menu is for")
    for menu in menus:
        page = _rows(db, "SELECT text FROM summary WHERE scope='menu' "
                         "AND key=? LIMIT 1", menu)
        count = _rows(db, "SELECT COUNT(*) c FROM menu_option WHERE menu=?",
                      menu)[0]["c"]
        summary = _first_sentences(page[0]["text"], 2) if page else \
            "(no page written yet)"
        out.append(f"  {menu}  ({count} options)")
        out.append(f"    {summary}")

    if menus:
        files = _rows(db,
                      "SELECT f.file, COUNT(DISTINCT m.menu) menus "
                      "  FROM menu_option m "
                      "  JOIN call_edge c ON c.caller = m.target "
                      "  JOIN file_usage f ON f.program = c.callee "
                      " WHERE m.menu IN (%s) AND %s "
                      " GROUP BY f.file ORDER BY menus DESC, f.file LIMIT 12"
                      % (",".join("?" * len(menus)), _CAN_CHANGE), *menus)
        if files:
            out.append("\nfiles these menus' programs can change")
            for r in files:
                out.append(f"  {r['file']:<12}reached from {r['menus']} menu(s)")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

def library_sheet(db: sqlite3.Connection, library: str) -> str:
    """A whole source library, from its families, its shape and its edges.

    Written to work when the library owns no menus at all. SERVPLUS owns none -
    it is entered by 100 menu options in ALPH - and on the real 21-library tree
    a back-office library with no menus of its own will be normal, not an edge
    case. When there are no families the inbound flows and the inventory carry
    the page.
    """
    out = [f"library   {library}"]

    tiers = _rows(db, "SELECT tier, COUNT(*) c FROM object "
                      " WHERE library=? AND tier<>'' "
                      " GROUP BY tier ORDER BY c DESC", library)
    if tiers:
        out.append("\nwhat it is made of")
        for r in tiers:
            out.append(f"  {r['tier']:<12}{r['c']:>6}")

    families = sorted({family_of(r["menu"]) for r in
                       _rows(db, "SELECT DISTINCT menu FROM menu_option "
                                 " WHERE library=?", library)})
    if families:
        out.append("\nthe menu areas it owns")
        for family in families:
            page = _rows(db, "SELECT text FROM summary WHERE scope='family' "
                             "AND key=? LIMIT 1", family)
            out.append(f"  {family}")
            out.append(f"    {_first_sentences(page[0]['text'], 3) if page else '(no page written yet)'}")
    else:
        out.append("\nthis library owns no menus - nothing in it is reached "
                   "directly from a menu")

    inbound = _rows(db,
                    "SELECT so.library src, COUNT(*) options "
                    "  FROM menu_option m "
                    "  JOIN object so ON so.name=m.menu "
                    "                AND so.member_type='MNUCMD' "
                    "  JOIN object tl ON tl.name=m.target "
                    " WHERE tl.library=? AND so.library<>tl.library "
                    " GROUP BY so.library ORDER BY options DESC", library)
    if inbound:
        out.append("\nhow it is entered from elsewhere")
        for r in inbound:
            out.append(f"  {r['options']} menu option(s) in {r['src']}")

    # Ranked by how many programs open the file, not by how many write it.
    #
    # Writers alone put the scratch files on top - 15 programs write ARAW01
    # where 53 open ARTW02 - and neither number identifies a master file. There
    # is no measure here that does: in this codebase work files genuinely have
    # many readers, and telling them apart would mean trusting the W0n naming
    # convention, which config.py says nothing in kb/ may treat as fact. So the
    # heading says what the number is and claims nothing more.
    files = _rows(db,
                  "SELECT o.name, COUNT(DISTINCT f.program) users, "
                  "       COUNT(*) FILTER (WHERE " + _CAN_CHANGE +
                  ") writers "
                  "  FROM object o JOIN file_usage f ON f.file = o.name "
                  " WHERE o.library=? AND o.member_type='PF' "
                  " GROUP BY o.name ORDER BY users DESC LIMIT 12", library)
    if files:
        out.append("\nthe files most of its programs open")
        for r in files:
            out.append(f"  {r['name']:<12}{r['users']:>5} program(s) open it, "
                       f"{r['writers'] or 0} can change it")

    blind = _rows(db, "SELECT COUNT(*) c FROM object "
                      " WHERE library=? AND depth IS NULL "
                      "   AND tier IN ('driver','program')", library)
    if blind and blind[0]["c"]:
        out.append(f"\nunreached: {blind[0]['c']} program(s) and driver(s) that "
                   f"no menu option or CALL chain in the source reaches. That "
                   f"is not the same as dead - nothing in the source says what "
                   f"runs a program overnight.")

    return "\n".join(out)


def _first_sentences(text: str, count: int) -> str:
    """The opening of a child page, for a parent's sheet.

    Roll-ups send openings rather than whole pages because the whole point of
    the level is to fit: eleven families at 180 words each is already 2,600
    tokens and prefill is the expensive half on this hardware.
    """
    body = " ".join(line for line in text.split("\n")
                    if line.strip() and not line.startswith("#"))
    parts = re.split(r"(?<=[.!?])\s+", body.strip())
    return " ".join(parts[:count])[:400]
