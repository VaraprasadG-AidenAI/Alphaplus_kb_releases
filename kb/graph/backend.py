"""
The seam.  One place decides whether a tool reads SQLite or Neo4j.

`kb/server/tools.py` calls `backend.rows("who_calls", sqlite_fn, name, depth)`.
Three behaviours, from `graph.backend` in alphaplus.toml:

    sqlite    call the SQLite function.  Nothing here does anything.
    neo4j     call the Cypher version; fall back to SQLite if the graph is
              unreachable, and say so once rather than per call.
    compare   run BOTH, diff them, log any disagreement, return the SQLite
              answer.  This is how a migration gets verified without betting
              the tool output on it.

## Why fall back rather than fail

A graph that is down should degrade a tool, not break it.  The knowledge base
is used to work tickets; an engineer with a slightly worse answer can carry on,
one with a stack trace cannot.  The fallback is logged once per process so it
cannot hide.

## Why `compare` returns the SQLite answer

SQLite is the store that has been measured - 87% on the ticket test, and
`kb.eval.probe` scores the ranking that comes off it.  Until Cypher matches it
row for row on the tickets we have, it is the challenger and not the champion.
`compare` exists to find out, not to hedge.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from kb.graph import settings as gs

#: Tool name -> the function in kb.graph.queries that answers it.
#:
#: ⚠️ Being in this dict does NOT make a tool read Neo4j.  The tool has to call
#: `backend.rows(...)` as well, and for five of these it did not: `MAPPED`
#: listed who_touches_file, where_is_field_used, resolve_overrides, find_object
#: and program_structure while `kb/server/tools.py` went straight to SQLite for
#: all five.  So `doctor` reported a migration that had not happened.
#:
#: A dict that has to agree with code elsewhere will drift, so it is checked
#: rather than trusted - see `unrouted()`, which reads tools.py and reports the
#: difference.  `mapped()` prints it.
MAPPED: Dict[str, str] = {
    "who_calls":            "who_calls",
    "what_it_calls":        "what_it_calls",
    "path_between":         "path_between",
    "blast_radius":         "blast_radius",
    "menu_path_to":         "menu_path_to",
    # search_text is deliberately NOT here any more. It was mapped after
    # Lucene proved capable, but capable is not the placement test: full text
    # is not a traversal question, the copy of every member's text it required
    # was the graph's only document data, and the two engines' rankings were
    # the one standing compare-mode disagreement. The relational store answers
    # it alone now.
}

#: Tools answered ONLY by the graph - there is no SQLite counterpart, so they
#: call `kb.graph.queries` directly and correctly do not appear in a routing
#: check.
GRAPH_ONLY = frozenset({"path_between", "blast_radius",
                        "menu_path_to"})

#: Cypher exists in `kb.graph.queries` for these, and it is NOT good enough to
#: route yet.  They were in MAPPED for a while and were never wired, so `doctor`
#: reported a migration that had not happened.  Removing the claim is the fix;
#: the reason each one is harder than "wire it up" is recorded here because that
#: is the part that gets lost.
#:
#: Two of the five do not answer the same QUESTION in Cypher, which is a
#: different problem from returning different columns and is not fixed by
#: renaming anything.
NEEDS_WORK: Dict[str, str] = {
    "resolve_overrides":
        "DIFFERENT QUESTION. The tool lists what a CL program DECLARES "
        "(override WHERE cl_program=?); the Cypher joins OPENS to OVERRIDES_TO "
        "to say which physical file a program actually opens. Both are useful; "
        "they are not interchangeable.",
    "who_touches_file":
        "DIFFERENT QUESTION, and the Cypher is weaker. The tool flags programs "
        "that open INPUT yet issue WRITE/REWRITE/DELETE - 294 programs across "
        "234 files, and a false negative there is the expensive kind. The "
        "Cypher has no contradiction check and no `evidence` column.",
    "where_is_field_used":
        "Wireable. Needs the Cypher to return 'read'/'write' rather than "
        "READS_FIELD/WRITES_FIELD, and field_definitions routed alongside it, "
        "or the formatter's direction split silently puts everything in reads.",
    "find_object":
        "Wireable, and an improvement - one round trip instead of six. Needs "
        "`paragraphs` added to the Cypher, and its `fields` count reconciled: "
        "the tool counts field DEFINITIONS (field WHERE owner=?), the Cypher "
        "counts distinct fields READ OR WRITTEN. Different numbers.",
    "program_structure":
        "The Cypher returns the paragraph tree only. The tool also renders "
        "notes, perform edges and loop flags, so this is a partial answer, not "
        "a drop-in.",
}

#: How long an unreachable graph stays written off before we try again.
#:
#: It used to be FOREVER - `_disabled` was a one-way latch, which was right when
#: nothing in this process could bring Neo4j back.  Autostart changed that:
#: `kb.graph.process` launches the server and returns without waiting, so the
#: graph is typically up 4 seconds after the call that found it down.  A
#: permanent latch meant the MCP server spent the whole session on SQLite while
#: the Neo4j it had just started sat there idle - and the three graph-only tools
#: (path_between, blast_radius, menu_path_to) have no fallback, so they
#: stayed
#: broken for that session.  Measured: exactly that, in the stdio test.
#:
#: 20s, so a retry costs at most one extra connect attempt per 20s in the
#: genuinely-down case, and recovers within one call of the server coming up.
RETRY_AFTER = 20.0

#: A configuration error is different and IS permanent: a typo in alphaplus.toml
#: will not fix itself, and retrying it every 20 seconds just repeats the noise.
_config_broken = threading.Event()

#: Messages already said, so each DISTINCT one is said once.
#:
#: This was a single Event, which meant the first degradation of a
#: session silenced every later one - the graph going down and the
#: config being wrong logged as one event, and whichever happened
#: second left no trace at all.  A set keyed on the message keeps the
#: original property (a repeated warning cannot spam a console) without
#: the one that hid faults.
_warned: set = set()
_disabled = threading.Event()
_disabled_at = 0.0
_lock = threading.Lock()
_diffs: List[str] = []


def _disable(permanent: bool = False) -> None:
    """Write the graph off, and remember when, so `mode()` can retry."""
    global _disabled_at
    with _lock:
        _disabled.set()
        _disabled_at = time.monotonic()
        if permanent:
            _config_broken.set()


def degraded() -> bool:
    """True when the graph WAS configured and then proved unreachable.

    `mode()` reports sqlite in that case, which is right for routing but wrong
    for a message: a graph-only tool must not tell someone to configure a
    backend they have already configured.
    """
    return _disabled.is_set()


def _shipped() -> bool:
    """True inside the packaged executable.

    Imported here rather than at module level: kb.config reads the
    environment, and this module is imported by things that run before that
    is settled.
    """
    try:
        from kb import config
        return bool(config.FROZEN)
    except Exception:                                         # noqa: BLE001
        return False


def configured() -> str:
    """What the configuration asks for, ignoring whether it is reachable."""
    if _shipped():
        return "sqlite"
    try:
        return gs.load().backend
    except gs.ConfigError:
        return "sqlite"


def mode() -> str:
    """sqlite | neo4j | compare.  Cheap enough to call per query.

    **A packaged build is always sqlite.** The executable ships one file-based
    store and no database driver, so a configuration that asks for anything
    else describes a thing that is not there. Deciding it here rather than
    letting the import fail means the answer is the same on every machine: no
    connection is attempted, no port is opened, no credential is read, and
    nothing can hang waiting for a server that was never installed.

    It also closes a gap in our own testing. A developer machine with a driver
    installed and a configuration asking for it exercises a different code
    path from the one every client runs, so a defect on the shipped path can
    pass every check here. Now both run the same code.
    """
    if _shipped():
        return "sqlite"
    if _disabled.is_set():
        if _config_broken.is_set():
            return "sqlite"
        # Give it another chance once the cool-off has passed.  Cheap: the next
        # call either connects or re-disables, and driver.get() proves the
        # server is there with verify_connectivity before any query runs.
        with _lock:
            if time.monotonic() - _disabled_at < RETRY_AFTER:
                return "sqlite"
            _disabled.clear()
    try:
        return gs.load().backend
    except gs.ConfigError as exc:
        # A broken config must not quietly become "sqlite" now that neo4j is
        # the default - that would hide a typo in alphaplus.toml as a
        # performance change nobody can explain.  Say it once, then degrade.
        _warn(f"configuration error, falling back to SQLite: {exc}")
        _disable(permanent=True)
        return "sqlite"


def _warn(msg: str) -> None:
    """Say each distinct message once, on stderr.

    stderr because a tool's stdout is the MCP response - a warning printed
    there would be parsed as part of the answer.
    """
    with _lock:
        if msg in _warned:
            return
        _warned.add(msg)
    print(f"  [kb] {msg}", file=sys.stderr, flush=True)


def _graph_fn(tool: str) -> Optional[Callable[..., Any]]:
    name = MAPPED.get(tool)
    if name is None:
        return None
    try:
        from kb.graph import queries
    except ImportError:
        return None
    return getattr(queries, name, None)


def _keys(rows: Any) -> Optional[set]:
    """The column names a result uses, or None if it has none."""
    if isinstance(rows, dict):
        return set(rows)
    if isinstance(rows, list):
        for r in rows:
            if isinstance(r, dict):
                return set(r)
    return None


def _normalise(rows: Any, only: Optional[set] = None) -> Any:
    """Make two answers comparable.

    Three differences are not disagreements and are flattened away:

      * Neo4j returns None where SQLite returns '' for an absent text column;
      * row order is not guaranteed either side for an unordered query;
      * **Cypher usually returns MORE columns.** It can hand back
        `member_type`, `library` and `tier` for free because it already has the
        node, where SQLite would need a join. Comparing the union of columns
        reports every row as differing when the shared columns agree exactly,
        which is a false alarm and hides real ones. So the diff is taken over
        the INTERSECTION of the two column sets.
    """
    def _row(r: Dict[str, Any]) -> tuple:
        items = ((k, "" if v is None else v) for k, v in r.items()
                 if only is None or k in only)
        return tuple(sorted(items))

    if isinstance(rows, dict):
        return _row(rows)
    if isinstance(rows, list):
        out = [_row(r) if isinstance(r, dict) else r for r in rows]
        return sorted(out, key=repr)
    return rows


def rows(tool: str, sqlite_fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
    """Get the rows for `tool` from whichever store is configured."""
    m = mode()
    if m == "sqlite":
        return sqlite_fn(*args, **kw)

    gfn = _graph_fn(tool)
    if gfn is None:
        return sqlite_fn(*args, **kw)

    from kb.graph import driver

    if m == "neo4j":
        try:
            return gfn(*args, **kw)
        except driver.GraphUnavailable as exc:
            # Worded without naming the optional store. This line reaches the
            # client's console and their logs, where a product they do not
            # have, cannot reach and must never be asked to install reads as
            # a broken installation rather than an unused option.
            _warn("the optional accelerator is not reachable; using the local "
                  "index for the rest of this session (no action needed)")
            _disable()
            return sqlite_fn(*args, **kw)

    # compare
    want = sqlite_fn(*args, **kw)
    try:
        got = gfn(*args, **kw)
    except driver.GraphUnavailable as exc:
        _warn(f"the optional accelerator is not reachable during compare: {exc}")
        _disable()
        return want
    # not `kw` - that is this function's **kw parameter
    k_want, k_got = _keys(want), _keys(got)
    shared = (k_want & k_got) if (k_want and k_got) else None
    if _normalise(want, shared) != _normalise(got, shared):
        n_want = len(want) if isinstance(want, list) else 1
        n_got = len(got) if isinstance(got, list) else 1
        note = (f"{tool}({', '.join(map(repr, args))}) "
                f"sqlite={n_want} rows, neo4j={n_got} rows")
        _diffs.append(note)
        print(f"  [kb] DIFFERS  {note}", file=sys.stderr, flush=True)
    return want


def diffs() -> List[str]:
    """Everything `compare` found disagreeing, this process."""
    return list(_diffs)


def unrouted() -> List[str]:
    """Tools in MAPPED that `kb/server/tools.py` never actually routes.

    Reads the source rather than the imports, because a tool that *could* call
    `backend.rows` and doesn't is exactly the case being looked for - and there
    is no runtime signal for "this function chose not to ask me".

    Returns tool names.  An empty list means MAPPED tells the truth.
    """
    try:
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] /
               "server" / "tools.py").read_text(encoding="utf-8")
    except OSError:
        return []                      # cannot check; do not invent a verdict
    out = []
    for tool in sorted(MAPPED):
        if tool in GRAPH_ONLY:
            continue
        if f'backend.rows("{tool}"' not in src and \
           f"backend.rows('{tool}'" not in src:
            out.append(tool)
    return out


def _page_count() -> str:
    """How many written pages exist, asked rather than remembered.

    This line used to read "112 of them, not 0".  It was wrong by a factor of
    seven and it was wrong inside a DIAGNOSTIC - the output somebody reads
    precisely when they are trying to find out what is true.  A hardcoded row
    count is bad prose anywhere and a bug here.
    """
    try:
        from kb import store as kbstore
        db = kbstore.facts()
        n = db.execute("SELECT COUNT(*) FROM summary WHERE text <> ''"
                       ).fetchone()[0]
        db.close()
        return str(n)
    except Exception:                                        # noqa: BLE001
        return "?"


def mapped() -> str:
    """Which tools read the graph, which do not.  For `doctor`."""
    gaps = set(unrouted())
    lines = [f"  backend: {mode()}", "", "  MAPPED to Neo4j"]
    for tool in sorted(MAPPED):
        if tool in gaps:
            lines.append(f"    {tool:<22}NOT ROUTED - still reads SQLite")
        elif tool in GRAPH_ONLY:
            lines.append(f"    {tool:<22}graph only, no SQLite counterpart")
        else:
            lines.append(f"    {tool}")
    if gaps:
        lines.append("")
        lines.append(f"  !!  {len(gaps)} tool(s) above are in MAPPED but "
                     f"kb/server/tools.py never calls backend.rows for them,")
        lines.append(f"      so they read SQLite whatever graph.backend says. "
                     f"Wire them or remove them from MAPPED.")

    lines.append("")
    lines.append("  CYPHER EXISTS, NOT ROUTED - and why, per tool")
    for tool in sorted(NEEDS_WORK):
        lines.append(f"    {tool}")
        why = NEEDS_WORK[tool]
        words, line = why.split(), "     "
        for w in words:
            if len(line) + len(w) + 1 > 78:
                lines.append(line)
                line = "     "
            line += " " + w
        if line.strip():
            lines.append(line)
    lines.append("")
    lines.append("  STILL SQLITE - aggregates, source text and the summary layer")
    for tool, why in (
        ("trace_ticket", "its FTS pass IS routed; the other 4 passes are not"),
        ("search_source", "a regex grep over source members, not an index"),
        ("read_member", "reads the source file itself"),
        ("read_summary", f"reads the written pages - {_page_count()} of "
                         f"them, counted just now"),
        ("route_ticket", "bm25 float column weights have no Lucene equivalent"),
        ("coverage_report", "aggregates - 1-4x slower in Cypher, see kb.graph.bench"),
        ("describe_file", ""), ("explain_screen", ""),
        ("find_paragraph", ""), ("what_paragraph_does", ""),
        ("what_it_touches", ""), ("what_uses", ""),
    ):
        lines.append(f"    {tool:<22}{why}")
    return "\n".join(lines)
