"""
The knowledge base tools.

Each function answers one question an agent asks while working a ticket, and
returns text rather than JSON - an agent reads the answer, it does not parse
it.  Every result carries `member:line` so the next step is always "go and
read the code", and carries its confidence so the agent knows what is solid.

The set is organised by direction, because that is how a question arrives:

  * outward   what does this member reach - what_it_touches
  * inward    what reaches this object   - what_uses
  * inside    how is this member built   - program_structure, what_paragraph_does
  * by name   I have a word, not a name  - trace_ticket, search_source, find_paragraph

The narrow tools (who_calls, who_touches_file, where_is_field_used, ...) are
the well-worn paths through the same facts and stay because an agent that
knows what it wants should not have to filter a general answer.

The knowledge base points at evidence.  It never replaces reading the source.
"""

from __future__ import annotations

import difflib
import functools
import json
import re
import os
import sqlite3
import textwrap
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .. import config
from ..parse import layout

#: Either a sqlite3.Connection or a kb.store.pg.Connection.  The two have
#: the same surface, which is the whole design of kb/store/pg.py.  Left
#: untyped rather than annotated as one of them.
_DB = None


class NotIndexedError(RuntimeError):
    """The source tree has not been indexed yet.

    Its own class so the server can answer with an instruction instead of a
    traceback - this is the error a new user meets first, and it is not a
    bug, it is a missing step.
    """


def store_backend() -> str:
    """`sqlite` or `postgres`.  Delegates to kb.store, which owns the answer."""
    from kb.store import backend
    return backend()


def _connect_postgres():
    """The PostgreSQL half of `db()`.

    NotIndexedError had ONE trigger - a file that is not there.  A server has
    two failure modes and they need different sentences: unreachable, or
    reachable and empty.  Telling someone to build an index when the real
    problem is a wrong host sends them to the wrong place entirely.
    """
    from kb import store
    try:
        conn = store.connect(read_only=True)
    except Exception as exc:                            # noqa: BLE001
        raise NotIndexedError(
            "Cannot reach the PostgreSQL knowledge base.\n"
            f"  error : {exc}\n"
            "  set   : ALPHAPLUS_PG_DSN, or ALPHAPLUS_PG_HOST and "
            "ALPHAPLUS_PG_DATABASE\n"
            "This is a connection problem, not a missing index."
        ) from exc
    try:
        n = conn.execute("SELECT COUNT(*) FROM object").fetchone()[0]
    except Exception as exc:                            # noqa: BLE001
        raise NotIndexedError(
            "Connected to PostgreSQL, but the knowledge base schema is not "
            "there.\n"
            f"  error  : {exc}\n"
            "  create : python -m kb.store.ddl --apply --dsn ...\n"
            "  load   : python -m kb.store.load --dsn ..."
        ) from exc
    if not n:
        raise NotIndexedError(
            "The PostgreSQL schema exists but holds no members, so there are "
            "no facts to answer from.\n"
            "  load: python -m kb.store.load --dsn ...\n"
            "Nothing is wrong with the question.")
    return conn



#: Set when the configured store could not answer and a local index did.
#:
#: Empty means nothing was degraded.  Tools read it to label an answer, the
#: way the graph tools label a relational one - an unlabelled fallback is
#: indistinguishable from the real thing, which is its own kind of wrong.
STORE_NOTE: str = ""


def _sqlite_fallback():
    """An open, populated facts.db, or None.

    Deliberately does NOT go through kb.store: `ready()` and `facts()` both
    ask `backend()` which store to use, and `backend()` is the thing that has
    already said "postgres".  This asks the only question that matters here -
    is there a readable local index right now.
    """
    try:
        if not config.FACTS_DB.exists():
            return None
        conn = sqlite3.connect(config.FACTS_DB, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT COUNT(*) FROM object").fetchone()[0]:
            conn.close()
            return None
        return conn
    except Exception:                                   # noqa: BLE001
        return None


def db():
    global _DB, STORE_NOTE
    if _DB is None:
        if store_backend() == "postgres":
            # A configured PostgreSQL that will not answer is not a reason to
            # fail while a built index sits on disk beside us.
            #
            # This is the same dead-end the graph tools had: blast_radius
            # answered a socket error four times in a row on one analysis
            # while the relational store could have answered the whole time.
            # Fixing it there fixed one layer.  Every one of those relational
            # fallbacks calls _rows() -> db(), so with a DSN configured and no
            # server behind it they dead-ended exactly as before, one level
            # down.  A tool that fails once gets worked around silently ever
            # after, so it must never dead-end.
            try:
                _DB = _connect_postgres()
                return _DB
            except NotIndexedError as pg_exc:
                local = _sqlite_fallback()
                if local is None:
                    raise
                first = str(pg_exc).splitlines()[0]
                STORE_NOTE = (
                    "  (local index; the configured PostgreSQL did not "
                    "answer)")
                import sys
                for line in (
                        f"alphaplus: {first}",
                        f"           falling back to {config.FACTS_DB}",
                        "           unset the [postgres] block in "
                        "alphaplus.toml to silence this."):
                    print(line, file=sys.stderr)
                _DB = local
                return _DB
        # store.ready(), not FACTS_DB.exists(): a 0-byte facts.db exists and
        # is not an index, and this gate passed one - so every tool got
        # through and died on "no such table: summary" with a raw traceback
        # instead of the sentence this error class exists to give.
        from kb import store as kbstore
        ok, why = kbstore.ready()
        if not ok:
            raise NotIndexedError(
                "This codebase has not been indexed yet, so there are no "
                "facts to answer from." + chr(10)
                + f"  {why}" + chr(10)
                + f"  source tree    : {config.SOURCE_ROOT}" + chr(10)
                + "Nothing is wrong with the question.")
        _DB = sqlite3.connect(config.FACTS_DB, check_same_thread=False)
        _DB.row_factory = sqlite3.Row
    return _DB


#: The first line number out of a comma list, on either store.
#:
#: `field_usage.lines` is '2522,4003,4268' - one row per distinct usage, with
#: the occurrences riding along (schema.sql:164-172).  Three queries wanted the
#: first of them and wrote `CAST(lines AS INTEGER)`, which worked only because
#: SQLite prefix-parses a cast: PostgreSQL raises 22P02 and handed the agent a
#: traceback.  what_it_touches(kind='fields') did that on every call.
#:
#: `instr` is SQLite-native and shimmed into PostgreSQL by ddl.COMPAT_SQL, so
#: this one expression serves both.  NULLIF guards the empty string, which
#: SQLite would cast to 0 and PostgreSQL would refuse; MIN ignores the NULL.
_FIRST_LINE = ("CAST(NULLIF(substr({c}||',', 1, instr({c}||',', ',') - 1), '') "
               "AS INTEGER)").format(c="lines")


#: What the last tool call spent in SQL.  Reset by the server at the start of
#: every tools/call and read back at the end - see kb.server.ledger.
#:
#: A dict rather than two module globals so the boundary can snapshot and clear
#: in one operation, and so a new counter does not need a new global.  Kept
#: here rather than in the ledger because `_rows` is the only place that knows
#: a query happened, and there are 114 call sites: wrapping them individually
#: was never going to stay done.
#:
#: `slowest` is only filled when TRACE_SQL is on.  It is what names a slow
#: query without a profiler - what_it_touches had one 803ms query out of 969ms
#: and nothing recorded which.
SQL_STATS: Dict[str, Any] = {"n": 0, "ms": 0.0, "slowest": []}

#: Off by default: the list costs an append per query and only a developer
#: chasing a specific tool wants it.
TRACE_SQL = os.environ.get("ALPHAPLUS_TRACE_SQL", "") == "1"


def reset_stats() -> None:
    SQL_STATS["n"] = 0
    SQL_STATS["ms"] = 0.0
    SQL_STATS["slowest"] = []


def _rows(sql: str, *args) -> List[Any]:
    started = time.perf_counter()
    try:
        return db().execute(sql, args).fetchall()
    finally:
        # perf_counter, not time(): monotonic, and immune to a clock step.
        took = time.perf_counter() - started
        SQL_STATS["n"] += 1
        SQL_STATS["ms"] += took * 1000.0
        if TRACE_SQL:
            SQL_STATS["slowest"].append((round(took * 1000.0, 1),
                                         " ".join(sql.split())[:110]))
            if len(SQL_STATS["slowest"]) > 40:
                SQL_STATS["slowest"] = sorted(
                    SQL_STATS["slowest"], reverse=True)[:5]


#: "Can this program change this file", in SQL.  A program counts when its OPEN
#: says so, or when it issues a mutating verb whatever the OPEN said - see the
#: file_usage comment in schema.sql.  Every tool that answers this question uses
#: this one predicate, because two tools giving two different counts for "who
#: writes CUSMAS" is worse than either being wrong alone.
_CAN_CHANGE = ("(mode IN ('O','U') OR verbs LIKE '%WRITE%' "
               "OR verbs LIKE '%DELETE%')")


#: Widest a single table cell may print before it is cut.
#:
#: This was 46, and it cut SILENTLY. A blast-radius row printed as
#: "members that COPY the layout (212 direct + 47" and another as
#: "CHKOBJ - the object must exist, nothing is rea" - both mid-word, with
#: nothing to say the sentence had been amputated. A reader cannot tell a
#: truncated cell from a badly written one, so a tool that is working
#: perfectly reads as broken.
#:
#: Wide enough that the sentences these tools actually write fit whole, and
#: anything that genuinely overruns now ends in an ellipsis, so a cut is
#: visible rather than inferred.
CELL_WIDTH = 200
_CUT = chr(0x2026)


def _table(headers: Sequence[str], rows: Sequence[Sequence],
           limit: int = 60) -> str:
    if not rows:
        return "  (none)"

    def fit(cell: str) -> str:
        return cell if len(cell) <= CELL_WIDTH else cell[:CELL_WIDTH - 1] + _CUT

    widths = [len(h) for h in headers]
    body = [[fit("" if c is None else str(c)) for c in r] for r in rows[:limit]]
    for row in body:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    out = ["  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    out.append("  " + "  ".join("-" * w for w in widths))
    for row in body:
        out.append("  " + "  ".join(
            cell.ljust(widths[i]) for i, cell in enumerate(row)))
    if len(rows) > limit:
        out.append(f"  ... and {len(rows) - limit:,} more")
    return chr(10).join(out)


# ===========================================================================
# Paging
# ===========================================================================
#
# Every long answer used to end in `... and 1,847 more`, which tells an agent
# that it has an incomplete answer and gives it no way to complete it.  It
# cannot ask for the rest, and it cannot tell whether the rest is one more
# call or forty, so it cannot decide whether finishing is worth the turns.
#
# The contract, and it is the same for every tool that returns one long list:
#
#     page=1 (default)    the first window
#     page=N              the Nth window
#     the header says     PAGE 2 OF 7   rows 41-80 of 271
#     the footer says     call again with page=3
#
# `page` and not `offset` deliberately: an agent incrementing a page number
# cannot land halfway through a window or skip rows by miscounting, and the
# total window count is the number it actually needs to plan with.
#
# A page past the end is not an error.  It says so and names the last real
# page, because an agent that has just been told there are 7 pages and asks
# for 8 has made an arithmetic slip, not an illegal request, and the recovery
# is one word.

#: Rows per window.  Chosen against the response budget, not for tidiness:
#: 60 rows of a 5-column table is ~4KB, so a full page is comfortably inside
#: even the smallest per-tool cap and an agent can hold two or three in
#: context without crowding out the source it is meant to be reading.
PAGE_ROWS = 60


# ---------------------------------------------------------------------------
# Why every paged query orders by something UNIQUE
# ---------------------------------------------------------------------------
#
# A page is `rows[start:start+size]` of whatever the store returned, so the
# pages only partition the answer if the row ORDER is the same on every call.
# `ORDER BY` alone does not give that.  Where the sort key has ties the
# database may order the tied rows differently between two executions - it is
# free to, nothing in SQL promises otherwise, and a different plan or a
# different scan order is enough.  The result is the worst kind of wrong: page
# 2 repeats rows from page 1 and silently drops others, and every page looks
# perfectly well-formed.
#
# Measured before this was fixed: `where_is_field_used` ordered by `program`,
# and WS-RECORDS-READ has 3,215 rows sharing a program.  `who_touches_file`
# ordered by `(mode, program)`, and ORDW29 has 8 rows sharing that pair.
#
# So every paged query ends with enough columns to make the key unique for a
# row, and where the natural key is not obviously unique the tie-break is the
# rest of what is displayed.  This is the same discipline `_rank_candidates`
# already applies to its fold - "sorting by name makes it reproducible" - for
# the same reason, one layer down.


def _slice(rows: Sequence[Any], page: int,
           size: int = PAGE_ROWS) -> Tuple[Sequence[Any], str, str]:
    """(this page's rows, header, footer).

    The header goes above the table and the footer below it, so a truncated
    response still carries the page number it was on - the size cap in
    kb/server/server.py cuts the tail, and losing the footer costs the agent
    a turn but not its place.
    """
    total = len(rows)
    size = max(1, size)
    pages = max(1, (total + size - 1) // size)
    page = max(1, int(page or 1))

    if total <= size:
        return rows, "", ""

    if page > pages:
        return ([], f"  PAGE {page} DOES NOT EXIST - there are {pages}, "
                    f"{total:,} row(s) in total.",
                f"  Ask for page {pages} or lower.")

    start = (page - 1) * size
    window = rows[start:start + size]
    head = (f"  PAGE {page} OF {pages}   rows {start + 1:,}-"
            f"{start + len(window):,} of {total:,}")
    if page < pages:
        foot = (f"  {total - start - len(window):,} row(s) not shown. "
                f"Call again with page={page + 1} for the next {size}.")
    else:
        foot = "  Last page."
    return window, head, foot


def _paged_table(headers: Sequence[str], rows: Sequence[Sequence],
                 page: int = 1, size: int = PAGE_ROWS) -> str:
    """A table that can be walked, rather than one that admits it is cut."""
    if not rows:
        return "  (none)"
    window, head, foot = _slice(rows, page, size)
    parts = []
    if head:
        parts.append(head)
    # limit=size, so `_table` never appends its own "... and N more" - the
    # footer says it properly and the two would contradict each other.
    parts.append(_table(headers, window, limit=size) if window
                 else "  (nothing on this page)")
    if foot:
        parts.append(foot)
    return "\n".join(parts)


# ===========================================================================
# Summaries - optional, and always labelled as generated
# ===========================================================================

def _summary(name: str) -> Optional[Tuple[str, bool]]:
    """(page, is_stale) if a summary page exists for this member.

    Stale means the member has changed since the page was written.  We still
    return it, because an out of date description is often still roughly
    right, but it is never returned without saying so - an agent handed a
    confident account of code that has since changed is worse off than one
    with no page at all.
    """
    # A written page may be about a member, a menu, a menu area or a library.
    # The table is asked first because it is the only place that knows which,
    # and because it carries the freshness that the file on disk does not.
    try:
        row = _rows("SELECT scope, text, written_by FROM summary "
                    "WHERE key=? ORDER BY level LIMIT 1", name)
    except sqlite3.OperationalError:
        row = []                            # an index built before the table
    if row:
        from ..summarise.store import page_path
        path = page_path(row[0]["scope"], name)
        try:
            page = path.read_text(encoding="utf-8")
        except OSError:
            page = row[0]["text"]
        # A page written from facts alone is not stale - there is no prose in it
        # to have gone out of date - but it is not a description either, and
        # saying so is the point.
        return page, False

    path = config.SUMMARY_DIR / f"{name}.md"
    if not path.exists():
        return None
    try:
        page = path.read_text(encoding="utf-8")
    except OSError:
        return None
    row = _rows("SELECT sha256, summary_hash FROM member_hash WHERE name=?",
                name)
    stale = bool(row) and row[0]["summary_hash"] != row[0]["sha256"]
    return page, stale


def read_summary(name: str) -> str:
    name = name.strip().upper()
    found = _summary(name)
    if not found:
        if not config.ENABLE_SUMMARIES:
            return ("Summaries are switched off for this knowledge base, so "
                    "there is no page for any member. Everything else - "
                    "facts, the call graph, file and field usage, the "
                    "lexicon - works without them. Use find_object and "
                    "describe_file instead.")
        return (f"No summary page for {name}. Facts are still available "
                f"through find_object, describe_file and what_it_calls.")
    page, stale = found
    header = ""
    if stale:
        header = ("STALE - the member has changed since this page was "
                  "written. Trust the facts and the source over the prose "
                  "below.\n\n")
    return header + page


# ===========================================================================
# 1. find_object
# ===========================================================================

#: A member's identity in one line: the library it lives in and its type.
#:
#: The library was in the database and in no tool's output. All 23 dropped it,
#: including `describe_file`, which reads `SELECT * FROM object` and had it in
#: the row. On this tree it costs nothing to omit - no name is in two
#: libraries - but the real extract is 21 libraries, where a name alone does
#: not identify a member and an answer that gives only the name cannot be
#: acted on.
#:
#: Rendered as `in ALPH` rather than a column, because it belongs to the
#: member and not to the row, and a column would repeat it on every line.
def _where(row) -> str:
    """`  in ALPH` when the row knows its library, else empty."""
    try:
        library = row["library"]
    except (KeyError, IndexError, TypeError):
        return ""
    return f"  in {library}" if library else ""


def _in_library(library: str) -> "Tuple[str, tuple]":
    """(SQL fragment, params) narrowing a query on `object` to one library.

    Returns an empty fragment when no library was asked for, so the caller
    can concatenate it unconditionally.

    This exists because a name does not identify a member. On this tree the
    ambiguity is 107 names that are two member types; on a 21-library extract
    it is also the same name in several libraries, and `describe_file` and
    `read_member` both resolved it by taking the first row the store handed
    back - so which library's source you read was not a decision anybody made.
    """
    want = (library or "").strip().upper()
    return (" AND library = ?", (want,)) if want else ("", ())


def _ambiguous(rows, subject: str, library: str = "") -> str:
    """A line naming the other candidates, or empty when there is one.

    Said rather than resolved. The source does not decide which library a
    bare name means - on IBM i the job's library list does, at run time, and
    that is not in the extract.
    """
    seen = []
    for r in rows:
        try:
            where = f"{r['library']}/{r['member_type']}"
        except (KeyError, IndexError, TypeError):
            continue
        if where not in seen:
            seen.append(where)
    if len(seen) < 2:
        return ""
    return (f"  {subject} is {len(seen)} members: {', '.join(seen)}. "
            f"Showing the first; narrow with library=.")


def _inactive_hint(name: str) -> List[str]:
    """A header calling the member retired, weighed against what reaches it.

    The comment is a hint. Whether anything in the source still calls the
    member, or a menu still offers it, is the evidence - and a member that
    nothing reaches in the source may still be started by the job scheduler,
    which the source cannot show. So the strongest this says is Possible.
    """
    try:
        hints = _rows("SELECT text, line FROM header_hint WHERE member=? "
                      "AND kind='inactive'", name)
    except sqlite3.OperationalError:
        return []
    if not hints:
        return []
    callers = [r["caller"] for r in _rows(
        "SELECT DISTINCT caller FROM call_edge WHERE callee=? ORDER BY caller", name)]
    menus = [f"{r['menu']} option {r['option']}" for r in _rows(
        "SELECT menu, option FROM menu_option WHERE target=? ORDER BY menu, option",
        name)]
    retired_callers = {r["member"] for r in _rows(
        "SELECT member FROM header_hint WHERE kind='inactive'")} & set(callers)
    out = ["", f"  HEADER SAYS INACTIVE (a comment, not evidence): "
               f"'{hints[0]['text']}'  {name}:{hints[0]['line']}"]
    if callers or menus:
        reach = []
        if callers:
            reach.append(f"{len(callers)} caller(s): {', '.join(callers[:8])}"
                         + (" ..." if len(callers) > 8 else ""))
        if menus:
            reach.append(f"{len(menus)} menu option(s): {', '.join(menus[:4])}")
        out.append(f"  STILL REACHABLE in the source - {'; '.join(reach)}.")
        if retired_callers and retired_callers == set(callers) and not menus:
            out.append("  Every caller's header also says inactive: follow them "
                       "with who_calls before trusting either.")
        else:
            out.append("  Treat it as active; the header is out of date or its "
                       "callers are dead code too.")
    else:
        out.append("  NOTHING IN THE SOURCE REACHES IT - no caller, no menu option.")
        out.append("  At most Possible that it is retired: a job scheduler entry "
                   "or an operator command")
        out.append("  can still start it, and neither is in the source.")
    return out


def find_object(name: str, library: str = "") -> str:
    name = name.strip().upper()
    clause, args = _in_library(library)
    rows = _rows(
        "SELECT name, member_type, language, src_file, subsystem, lines, "
        "parse_status, notes, based_on, path, library FROM object "
        "WHERE name = ?" + clause + " ORDER BY library, member_type",
        name, *args)
    if not rows:
        # An SQL table is not a member: its DDL member is. Say which, with the
        # label, before any other interpretation of the name.
        as_table = _sql_tables("name=? OR sql_name=?", name, name)
        if as_table:
            kinds = sorted({t["kind"] for t in as_table})
            lines = [f"{name} is an SQL {'/'.join(kinds).upper()}, not a "
                     f"source member.", "",
                     "  name           kind   created at" + " " * 10
                     + "label / base"]
            lines += [_table_line(t) for t in as_table]
            siblings = _sql_tables("ddl_member=? AND name<>? AND kind<>'index'",
                                   as_table[0]["ddl_member"], as_table[0]["name"])
            if siblings:
                lines += ["", f"  {as_table[0]['ddl_member']} also creates "
                          + ", ".join(t["name"] for t in siblings)
                          + " - a different table with its own programs; "
                          "never merge the two."]
            lines += ["", f"  describe_file({as_table[0]['name']}) for its "
                      f"columns, who_touches_file({as_table[0]['name']}, "
                      f"mode='write') for what writes it."]
            return "\n".join(lines)

        # Before answering "no object", ask whether the PHYSICAL FILE exists.
        #
        # A source member and a physical file are different things, and an
        # extraction can hold one without the other. A data file appears only
        # as its own directory with a single member inside; there is no source
        # to parse, so it is in no code table, and every query about it comes
        # back empty.
        #
        # Answering that with "does not exist" is a different and far more
        # expensive claim than "there is no source member for it here" - it
        # once put the central file of a ticket outside the analysis entirely.
        objects = _rows(
            "SELECT name, library, path, bytes FROM data_member "
            "WHERE name = ? AND is_file_object = 1 ORDER BY library", name)
        if objects:
            lines = [f"{name} is a PHYSICAL FILE, not a source member.",
                     ""]
            lines.append(_table(
                ["library", "evidence"],
                [(o["library"] or "?", o["path"]) for o in objects]))
            lines.append("")
            lines.append("  The object exists; its DDS is not in this "
                         "extraction, so nothing describes its layout here.")
            if len(objects) > 1:
                lines.append(f"  ** It exists in {len(objects)} libraries. "
                             f"Which one a job opens depends on the library "
                             f"list at run time. **")
            lines.append("  A COBOL program never names a physical file - it "
                         "names a token that an")
            lines.append("  OVRDBF in the calling CL redirects. Use "
                         "resolve_overrides on the CL, or")
            lines.append("  search_source for TOFILE(" + name + ") to find "
                         "what binds to it.")
            return "\n".join(lines)

        # A data file that CL builds, opens or deletes, with no object on
        # disk. In a source-only extraction that is how every data file looks;
        # in a full download it means the object was not extracted. Either way
        # it is a data file, and "not in the knowledge base" would be wrong.
        built = _rows("SELECT verb, source, cl_member, line, library_expr "
                      "FROM file_provenance WHERE target=? "
                      "ORDER BY verb, cl_member, line", name)
        opened = _rows("SELECT COUNT(*) c, COUNT(DISTINCT cl_program) p "
                       "FROM override WHERE target_file=?", name)[0]
        if built or opened["c"]:
            lines = [f"{name} is a DATA FILE named in CL, not a source member.",
                     ""]
            templates = sorted({b["source"] for b in built
                                if b["verb"] == "CRTF" and b["source"]})
            if templates:
                lines.append(f"  built from template: {', '.join(templates)}")
            for b in built[:12]:
                where = f"{b['cl_member']}:{b['line']}"
                lib = f"  library {b['library_expr']}" if b["library_expr"] else ""
                frm = f" from {b['source']}" if b["source"] else ""
                lines.append(f"  {b['verb']:<5} {where}{frm}{lib}")
            if len(built) > 12:
                lines.append(f"  ... {len(built) - 12} more CL statement(s)")
            if opened["c"]:
                lines.append(f"  opened through {opened['c']:,} OVRDBF "
                             f"statement(s) in {opened['p']:,} CL program(s)")
            lines.append("")
            if extraction_holds_data():
                lines.append("  This extraction holds data members, but none "
                             "for this file - its object")
                lines.append("  directory was not extracted, or the file is "
                             "built and dropped at run time.")
            else:
                lines.append(_source_only_note())
            lines.append(f"  Call resolve_files({name}) for its template, the "
                         f"other files built from it,")
            lines.append("  and the program-side names that open it.")
            return "\n".join(lines)

        like = _rows(
            "SELECT name, member_type, lines, library FROM object "
            "WHERE name LIKE ? ORDER BY name, library LIMIT 20",
            f"%{name}%")
        if not like:
            # Before saying "not here", ask whether the name is a
            # PROGRAM-SIDE name rather than an object. On IBM i a COBOL
            # program never names the physical file it opens; it names a
            # token the calling CL redirects. Such a token is real, is used
            # everywhere, and correctly has no object row - so the bare nil
            # is true and dangerously misleading, and it used to point at
            # coverage_report, which cannot answer this at all.
            alias = _rows(
                "SELECT target_file, COUNT(*) c FROM override "
                "WHERE logical_name=? GROUP BY target_file "
                "ORDER BY c DESC", name)
            if alias:
                sites = sum(r["c"] for r in alias)
                targets = [r["target_file"] for r in alias
                           if r["target_file"]]
                out = [f"{name} is a PROGRAM-SIDE FILE NAME, not a source "
                       f"member.",
                       f"  {sites:,} OVRDBF statement(s) redirect it, so it "
                       f"names a token rather than an object."]
                if targets:
                    shown = ", ".join(targets[:8])
                    more = (f" (+{len(targets) - 8} more)"
                            if len(targets) > 8 else "")
                    out.append(f"  It resolves to: {shown}{more}")
                if len(targets) > 1:
                    out.append(f"  ** AMBIGUOUS ** - which file it opens "
                               f"depends on which CL launched the program.")
                out.append(f"  Call resolve_files({name}) for the full chain, "
                           f"the template behind it,")
                out.append(f"  and which of those files a change actually has "
                           f"to touch.")
                return chr(10).join(out)

            return chr(10).join([
                f"{name} is not in the knowledge base - no source member and "
                f"no file object.",
                "  That is not the same as 'does not exist'. It may live in a "
                "library that was",
                "  not extracted - call coverage_report to see which libraries "
                "contribute nothing.",
                f"  If it is a file name, resolve_files({name}) can still say "
                f"what binds to it."])
        if library:
            # It may exist elsewhere, and "no object called ORD100" is a
            # worse answer than naming the library that has one.
            elsewhere = _rows("SELECT library, member_type FROM object "
                              "WHERE name=? ORDER BY library", name)
            if elsewhere:
                where = ", ".join(r["library"] + "/" + r["member_type"]
                                  for r in elsewhere)
                return (f"No {name} in library "
                        f"{library.strip().upper()}. It exists in: {where}")
        return (f"No object called exactly {name}. Similar names:\n"
                + _table(["name", "type", "library", "lines"],
                         [(r["name"], r["member_type"], r["library"] or "",
                           r["lines"]) for r in like]))

    out: List[str] = []
    for r in rows:
        out.append(f"{r['name']}  ({r['member_type']}, {r['language']}){_where(r)}")
        if r["member_type"] == "SQL":
            created = _sql_tables("ddl_member=?", r["name"])
            if created:
                out.append(f"  creates  {len(created)} object(s) - each is its "
                           f"own table; ask about them by name:")
                out += ["  " + line for line in map(_table_line, created)]
        out.append(f"  source   {r['path']}")
        out.append(f"  lines    {r['lines']:,}")
        if r["subsystem"]:
            out.append(f"  group    {r['subsystem']} (inferred from the name, "
                       f"not asserted)")
        if r["based_on"]:
            out.append(f"  built over {r['based_on']}")
        if r["parse_status"] != "ok":
            out.append(f"  PARSE    {r['parse_status']} - {r['notes']}")

        counts = {
            "opens": _rows("SELECT COUNT(*) c FROM file_usage WHERE program=?",
                           r["name"])[0]["c"],
            "calls": _rows("SELECT COUNT(DISTINCT callee) c FROM call_edge "
                           "WHERE caller=? AND callee<>''", r["name"])[0]["c"],
            "called by": _rows("SELECT COUNT(DISTINCT caller) c FROM call_edge "
                               "WHERE callee=?", r["name"])[0]["c"],
            "paragraphs": _rows("SELECT COUNT(*) c FROM paragraph "
                                "WHERE member=? AND member_type=?",
                                r["name"], r["member_type"])[0]["c"],
            "fields": _rows("SELECT COUNT(*) c FROM field WHERE owner=?",
                            r["name"])[0]["c"],
        }
        out.append("  " + ", ".join(f"{k} {v}" for k, v in counts.items() if v))

        if counts["paragraphs"] > 1:
            out.append(f"  call program_structure for {r['name']} to see how "
                       f"its paragraphs fit together")
        if _summary(r["name"]):
            out.append("  a written summary exists - call read_summary "
                       f"for {r['name']}")
    out.extend(_inactive_hint(name))
    return "\n".join(out)


# ===========================================================================
# 2/3. who_calls, what_it_calls
# ===========================================================================

def _format_call_levels(heading: str, rows: List[Dict[str, Any]],
                        col: str, empty: str, page: int = 1,
                        subject: str = "") -> str:
    """One formatter for who_calls and what_it_calls, either store.

    `level` comes from the rows rather than from a loop counter, because the
    Cypher version computes it with min(length(p)) and the SQLite version
    counts frontiers - and the point of the seam is that the formatter cannot
    tell which it got.

    ## One paged table, not a table per level

    It printed a table per level, each capped at 60 rows with no indication
    that a cap had been reached at all.  `who_calls("SYS100", depth=2)` has
    6,029 callers; it printed 120 of them under two innocent-looking headings
    and dropped 5,909 without a word.  A count in the summary line and 2% of
    the rows underneath it is the shape of answer that gets quoted as if it
    were complete.

    Now it is one table with a `level` column, ordered by level then name and
    paged.  The level is not lost - it is a column instead of a heading - and
    the row count, the page count and the way to get the next page are all
    stated.
    """
    out: List[str] = [heading]
    if not rows:
        out.append(empty + "\n"
                   "  That may mean it is dead, or that it is started by the "
                   "job scheduler or a data queue - neither of which is in the "
                   "source. See coverage_report.")
        return "\n".join(out)
    total_sites = sum(int(r.get("sites") or 1) for r in rows)
    levels = sorted({int(r.get("level") or 1) for r in rows})
    out.append(f"  {len(rows):,} program(s), from {total_sites:,} call "
               f"site(s), across {len(levels)} level(s).")

    ordered = sorted(rows, key=lambda r: (int(r.get("level") or 1),
                                          str(r.get(col) or "")))
    out.append("")
    # An unresolved call has no callee, and a blank cell reads as a glitch
    # rather than as the finding it is. Name what the call goes through
    # instead, and let evidence point at THIS member's line - the row is
    # about a site here, not about a member nobody could name.
    body = []
    for row in ordered:
        target = str(row.get(col) or "")
        if target:
            evidence = f"{target}:{row['line']}" if row.get("line") else target
        else:
            through = str(row.get("variable") or "")
            target = f"(through {through})" if through else "(not resolved)"
            evidence = (f"{subject}:{row['line']}"
                        if row.get("line") and subject else "-")
        body.append((str(row.get("level") or 1), target,
                     str(row.get("sites") or 1),
                     row.get("kind") or row.get("tier") or "",
                     evidence, row.get("confidence") or "exact"))
    out.append(_paged_table(
        ["level", col, "sites", "how", "evidence", "confidence"], body, page))
    return "\n".join(out)


#: The call graph, walked in the database instead of in Python.
#:
#: `_who_calls_sqlite` and `_what_it_calls_sqlite` walk breadth-first with one
#: query per frontier member.  At the default depth of 1 that is a single
#: query and there is nothing to fix.  At depth 2 from a hub it is not:
#: SYS100 has 3,689 direct callers, so level 2 issued 3,689 point queries and
#: took 815ms.  One recursive query answers the same question in 23ms, and it
#: barely notices depth - measured 23ms at 2, 50ms at 4.
#:
#: Two things make it correct rather than merely fast:
#:
#: `UNION`, not `UNION ALL`.  A COBOL call graph has cycles - A calls B calls
#: A through a service program - and `UNION ALL` on a cyclic graph does not
#: terminate.  `UNION` dedupes each iteration, which is what makes the
#: recursion stop.  Both stores accept either, so nothing warns you.
#:
#: `MIN(level)`.  Without it a program reachable at both level 1 and level 2
#: appears twice, and the walk's own dedupe (`seen`) means it should appear
#: once, at the shallowest level that reaches it.  Verified against the walk:
#: SYS100 at depth 2 gives 6,029 rows either way, 3,689 at level 1 and 2,340
#: at level 2 - and 3,689 is the number this file's own comment quotes.
_WALK_SQL = """
WITH RECURSIVE reach({near}, level) AS (
        SELECT {near}, 1 FROM call_edge WHERE {far} = ?
    UNION
        SELECT e.{near}, r.level + 1
          FROM call_edge e JOIN reach r ON e.{far} = r.{near}
         WHERE r.level < ?
), shallowest AS (
    SELECT {near}, MIN(level) AS level FROM reach GROUP BY {near}
)
SELECT s.{near} AS who, s.level AS level,
       MIN(e.kind) AS kind, MIN(e.line) AS line,
       MIN(e.confidence) AS confidence, COUNT(*) AS sites,
       MIN(e.variable) AS variable
  FROM shallowest s
  JOIN call_edge e ON e.{near} = s.{near}
  JOIN shallowest p ON p.{near} = e.{far} AND p.level = s.level - 1
 WHERE s.{near} <> ?
 GROUP BY s.{near}, s.level
UNION ALL
SELECT s.{near}, s.level,
       MIN(e.kind), MIN(e.line), MIN(e.confidence), COUNT(*),
       MIN(e.variable)
  FROM shallowest s
  JOIN call_edge e ON e.{near} = s.{near} AND e.{far} = ?
 WHERE s.level = 1 AND s.{near} <> ?
 GROUP BY s.{near}, s.level
"""


def _walk_calls(name: str, depth: int, inward: bool) -> List[Dict[str, Any]]:
    """Callers (inward) or callees (outward) by level, in one query.

    `sites` is the number of call sites from this program into the members one
    level nearer the subject - at level 1 that is calls into the subject
    itself, which is what the walk counted and what the header line reports.
    The walk counted level-2 sites against whichever level-1 member happened
    to discover them first, which depended on dictionary order; this does not.
    """
    near, far = ("caller", "callee") if inward else ("callee", "caller")
    depth = max(1, min(depth, 4))
    sql = _WALK_SQL.format(near=near, far=far)
    rows = _rows(sql, name, depth, name, name, name)
    out = [{near: r["who"], "kind": r["kind"], "line": r["line"],
            "confidence": r["confidence"], "sites": r["sites"],
            # An unresolved call has no name to show. The variable it went
            # through is the only thing a reader can act on, so it travels
            # with the row rather than being looked up again later.
            "variable": r["variable"], "level": r["level"]} for r in rows]
    # Level 1 is produced by the second branch and deeper levels by the first,
    # so sort here rather than relying on a UNION ALL's order.
    out.sort(key=lambda r: (r["level"], r[near]))
    return out


def _who_calls_sqlite(name: str, depth: int = 1) -> List[Dict[str, Any]]:
    """Callers by level, from the relational store.

    Returns the same shape as kb.graph.queries.who_calls so either store can
    feed the formatter and `backend.rows` can diff them.

    Kept as a named function rather than inlined: `backend.rows` takes the
    relational reader as a parameter, and `compare` mode diffs whatever this
    returns against the Cypher.

    ## What changed when this stopped being a Python walk

    It walked breadth-first with one query per frontier member.  Verified
    across SYS100, ERRSRV, ORD100 and AGD109 at depths 1-3, the recursive
    version returns the identical set of (program, level) pairs every time,
    and identical `kind` and `confidence` on every row.

    `sites` differs on a small number of level-2 rows and nowhere else - 15 of
    6,029 for SYS100, 29 of 1,647 for ERRSRV, 0 of 3,689 at depth 1.  The walk
    counted a level-2 caller's sites against whichever level-1 member
    discovered it first, which came out of dictionary iteration order; this
    counts its sites into every level-1 member it reaches.  Same class of
    correction as the fold-order bug recorded in `_rank_candidates`: the old
    number was not wrong so much as arbitrary.
    """
    return _walk_calls(name.strip().upper(), depth, inward=True)


def who_calls(name: str, depth: int = 1, page: int = 1) -> str:
    name = name.strip().upper()
    from kb.graph import backend
    rows = backend.rows("who_calls", _who_calls_sqlite, name, depth)
    answer = _format_call_levels(f"What reaches {name}:", rows, "caller",
                                 "  Nothing in the source calls it.", page,
                                 subject=name)
    if page != 1:
        return answer
    return "\n".join([answer] + _call_roots(name, rows, depth)
                     + _inactive_hint(name))


#: Past this many callers the root check is summarised, not listed: each one
#: costs two lookups, and a shared utility has thousands.
_ROOT_CHECK_LIMIT = 400


def _call_roots(name: str, rows: List[Dict[str, Any]], depth: int) -> List[str]:
    """Where the chain of callers starts, and whether this answer reached it.

    A caller that nothing calls and no menu offers is a ROOT: the job
    scheduler, an operator or another system starts it, or nothing does. One
    that still has callers beyond `depth` is not a root yet, and an analysis
    that stopped there has not found how the program is started.
    """
    members = {str(r.get("caller") or "") for r in rows} - {""}
    if not members:
        members = {name}
    if len(members) > _ROOT_CHECK_LIMIT:
        return ["", f"  ROOTS: not checked for {len(members):,} callers. Ask "
                    f"who_calls on the callers that matter to the ticket."]
    roots, open_ended, menus = [], [], []
    for member in sorted(members):
        above = {r["caller"] for r in _rows(
            "SELECT DISTINCT caller FROM call_edge WHERE callee=?", member)}
        offered = _rows("SELECT 1 FROM menu_option WHERE target=? LIMIT 1", member)
        if offered:
            menus.append(member)
        if not above and not offered:
            roots.append(member)
        elif above - members - {name}:
            open_ended.append(member)
    out = [""]
    if roots:
        out.append(f"  ROOTS - nothing in the source calls these and no menu "
                   f"offers them ({len(roots)}): {', '.join(roots[:20])}")
        out.append("    Each is started by the job scheduler, an operator or "
                   "another system - or it is dead. The source cannot say which.")
    if menus:
        out.append(f"  STARTED FROM A MENU ({len(menus)}): {', '.join(menus[:20])}"
                   f" - menu_path_to shows the route")
    if open_ended:
        out.append(f"  NOT A ROOT YET - still called from above depth {depth} "
                   f"({len(open_ended)}): {', '.join(open_ended[:20])}")
        if depth < 4:
            out.append(f"    Call who_calls({name}, depth={min(4, depth + 1)}) "
                       f"until every chain ends in a root or a menu.")
        else:
            out.append("    Depth 4 is the limit: call who_calls on each of "
                       "these to follow them further.")
    return out if len(out) > 1 else []


def _what_it_calls_sqlite(name: str, depth: int = 1) -> List[Dict[str, Any]]:
    """Callees by level.  Same shape as kb.graph.queries.what_it_calls.

    The mirror of `_who_calls_sqlite` - see there for what the move to a
    recursive query changed and what it did not.
    """
    return _walk_calls(name.strip().upper(), depth, inward=False)


def what_it_calls(name: str, depth: int = 1, page: int = 1) -> str:
    name = name.strip().upper()
    from kb.graph import backend
    rows = backend.rows("what_it_calls", _what_it_calls_sqlite, name, depth)
    return _format_call_levels(f"What {name} reaches:", rows, "callee",
                               "  It calls nothing in the source.", page,
                               subject=name)


def _sql_tables(where: str, *args) -> List[Any]:
    """Rows of sql_table, or none on an index built before the table existed."""
    try:
        return _rows("SELECT name, sql_name, kind, ddl_member, library, label, "
                     "base, line FROM sql_table WHERE " + where
                     + " ORDER BY name, library", *args)
    except sqlite3.OperationalError:
        return []


def _table_line(t: Any) -> str:
    alias = f" (SQL name {t['sql_name']})" if t["sql_name"] != t["name"] else ""
    label = f"  '{t['label']}'" if t["label"] else ""
    over = f"  over {t['base']}" if t["base"] else ""
    lib = f"@{t['library']}" if t["library"] else ""
    return (f"  {t['name']:<14} {t['kind']:<6} {t['ddl_member']}{lib}:{t['line']}"
            f"{alias}{label}{over}")


def resolve_table_name(name: str, tool: str) -> Tuple[str, str, Optional[str]]:
    """(name to answer for, note to put first, or a refusal to return instead).

    One SQL DDL member can create several tables, and every answer about "the
    member" used to be an answer about all of them at once: their writers,
    readers and columns merged, and an analysis named a table the process never
    writes. So a tool is never answered for a DDL member:

    - a table's SQL long name answers for its canonical (system) name;
    - a DDL member creating exactly one table answers for that table, and says
      so on the first line;
    - a DDL member creating several refuses and lists them - the caller asks
      about one.
    """
    as_table = _sql_tables("(name=? OR sql_name=?) AND kind<>'index'", name, name)
    if as_table:
        canonical = as_table[0]["name"]
        if canonical != name:
            return canonical, (f"{name} is the SQL name of {canonical}; answering "
                               f"for {canonical}, the name programs open."), None
        return name, "", None
    created = _sql_tables("ddl_member=? AND kind<>'index'", name)
    if not created:
        return name, "", None
    if len(created) == 1:
        t = created[0]
        label = f" '{t['label']}'" if t["label"] else ""
        return t["name"], (f"{name} is the DDL member that creates {t['kind']} "
                           f"{t['name']}{label}; answering for {t['name']}."), None
    refusal = [f"{name} is an SQL DDL member that creates {len(created)} "
               f"objects. Each has its own programs, writers and columns, so "
               f"{tool} does not answer for the member - ask about one:", "",
               "  name           kind   created at" + " " * 10 + "label / base"]
    refusal += [_table_line(t) for t in created]
    refusal += ["", f"  e.g. {tool}({created[0]['name']}). To find which one a "
                "process really writes, follow its writers with "
                "who_touches_file(<table>, mode='write')."]
    return name, "", "\n".join(refusal)


def table_aware(argument: str, tool: str):
    """Answer a tool for a table, never for the DDL member that creates it.

    Applied as a decorator so every return path of the tool gets the same
    treatment - a guard written into each body would miss the one return
    added next year. See resolve_table_name.
    """
    def decorate(function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            if argument in kwargs:
                raw = kwargs[argument]
            elif args:
                raw = args[0]
            else:
                return function(*args, **kwargs)
            name = str(raw or "").strip().upper()
            answer_for, lead, refusal = resolve_table_name(name, tool)
            if refusal:
                return refusal
            if argument in kwargs:
                kwargs[argument] = answer_for
            else:
                args = (answer_for,) + tuple(args[1:])
            result = function(*args, **kwargs)
            return f"{lead}\n\n{result}" if lead else result
        return wrapper
    return decorate


@table_aware("file", "who_touches_file")
def who_touches_file(file: str, mode: str = "any",
                     page: int = 1) -> str:
    file = file.strip().upper()
    canonical = _rows("SELECT based_on FROM object WHERE name=? AND based_on<>''",
                      file)
    note = ""
    targets = [file]
    if canonical:
        parent = canonical[0]["based_on"]
        note = (f"\n  {file} is a view over {parent}; programs reading it are "
                f"reading {parent}'s data.")
    # also include every logical over this file
    for r in _rows("SELECT name FROM object WHERE based_on=?", file):
        targets.append(r["name"])

    # Programs that reach this file through an OVRDBF that RENAMES it.
    #
    # A program whose COBOL says it reads CUSTDTA is really reading M1_CUSTDTA when
    # its driver said OVRDBF FILE(CUSTDTA) TOFILE(&DTALIB/M1_CUSTDTA). Asking who
    # touches M1_CUSTDTA used to answer with the CL that named it and not the
    # program that reads it, because the two facts were stored in different
    # tables and nothing joined them. See file_resolved in the schema.
    try:
        redirected = _rows(
            "SELECT program, logical_file, physical_file, library, via, hops, "
            "       confidence, evidence FROM file_resolved "
            " WHERE physical_file=? "
            " ORDER BY program, logical_file, via", file)
    except sqlite3.OperationalError:
        redirected = []                     # an index built before the table

    placeholders = ",".join("?" * len(targets))
    where = "f.file IN (" + placeholders + ")"
    if mode == "write":
        where += " AND " + _CAN_CHANGE.replace("mode", "f.mode").replace(
            "verbs", "f.verbs")
    elif mode == "read":
        where += " AND NOT " + _CAN_CHANGE.replace("mode", "f.mode").replace(
            "verbs", "f.verbs")

    rows = _rows(
        f"SELECT f.program, f.file, f.mode, f.verbs, f.device, f.confidence, "
        f"f.evidence, o.member_type, o.library, "
        f"  (f.mode='I' AND (f.verbs LIKE '%WRITE%' "
        f"   OR f.verbs LIKE '%DELETE%')) AS contradicts "
        f"FROM file_usage f "
        f"LEFT JOIN object o ON o.name = f.program "
        # mode, program is not unique: a program can open the same file
        # twice, and reaching it through two logicals produces two rows
        # with the same pair. file and line finish the key.
        f"WHERE {where} ORDER BY f.mode DESC, f.program, f.file, f.line",
        *targets)

    if not rows and not redirected:
        # "Nothing opens X" reads as "nothing depends on X", and for a record
        # layout that is the opposite of true: on one estate this was returned
        # for a customer master that 156 members COPY. Opening a file and
        # copying its layout are different couplings, and this tool only sees
        # the first. A nil that does not say what it did not look at is how a
        # dependency gets declared absent.
        copiers = _rows("SELECT COUNT(DISTINCT program) c FROM copy_edge "
                        "WHERE copybook=?", file)
        n = copiers[0]["c"] if copiers else 0
        out = [f"No program OPENS {file} - no READ, WRITE or file "
               f"declaration names it." + note]
        if n:
            out.append(f"  That is not the same as unused: {n:,} member(s) "
                       f"COPY its record layout, which is a")
            out.append(f"  compile-time coupling this tool does not see. "
                       f"Call what_uses({file}, kind='copybooks').")
        out.append(f"  A program may also reach it under another name - "
                   f"resolve_files({file}) shows which.")
        return chr(10).join(out)

    #: A writer is a program whose OPEN says it can change the file, or one
    #: that issues a mutating verb whatever the OPEN said.  Splitting on mode
    #: alone dropped 294 programs across 234 files out of the write list, and
    #: on this question a false negative is the expensive kind of wrong.
    writers = [r for r in rows
               if r["mode"] in ("U", "O") or r["contradicts"]]
    readers = [r for r in rows
               if r["mode"] not in ("U", "O") and not r["contradicts"]]

    # The headline count must describe the file, not the filter.  Counting the
    # already-filtered rows made "who writes CUSMAS" report "0 read it".
    totals = _rows(
        # COUNT(*) FILTER rather than SUM(<boolean>).  SQLite sums a boolean
        # happily; standard SQL has no such aggregate, and this is the line
        # that prints "12 program(s) can change it, 40 read it" - a headline
        # that must not become an error or, worse, a NULL rendered as 0.
        f"SELECT COUNT(*) FILTER (WHERE {_CAN_CHANGE}) w, "
        f"       COUNT(*) FILTER (WHERE NOT {_CAN_CHANGE}) r "
        f"FROM file_usage WHERE file IN ({placeholders})", *targets)[0]
    out = [f"{file}: {totals['w'] or 0} program(s) can change it, "
           f"{totals['r'] or 0} read it.{note}"]
    # CAN CHANGE IT answers the question this tool exists for, so
    # it goes on page 1 whole and is NOT repeated under each page
    # of READS. Repeating it made the pages overlap, re-sent its
    # full size on every call, and left "page 2" meaning two
    # different things inside one answer.
    if writers and mode in ("any", "write") and page == 1:
        out.append("\n  CAN CHANGE IT:")
        out.append(_table(
            ["program", "type", "library", "via", "opened", "verbs",
             "evidence"],
            [(r["program"] + (" (!)" if r["contradicts"] else ""),
              r["member_type"] or "", r["library"] or "", r["file"],
              {"I": "read", "O": "write", "U": "update"}.get(r["mode"],
                                                             r["mode"]),
              r["verbs"], r["evidence"]) for r in writers]))
        if any(r["contradicts"] for r in writers):
            out.append("  (!) opened INPUT yet issues a WRITE, REWRITE or "
                       "DELETE. Valid COBOL cannot")
            out.append("  do that, so it is listed rather than dropped - but "
                       "read it before believing")
            out.append("  it. kb.index.consistency --check "
                       "write_on_input_open.")
    elif writers and mode in ("any", "write"):
        out.append("")
        out.append(f"  CAN CHANGE IT: {len(writers)} program(s), listed on page 1.")
    if readers and mode in ("any", "read"):
        out.append("\n  READS:")
        out.append(_paged_table(["program", "type", "library", "via",
                                 "verbs", "evidence"],
                          [(r["program"], r["member_type"] or "",
                            r["library"] or "", r["file"],
                            r["verbs"], r["evidence"]) for r in readers], page, 40))

    if redirected:
        out.append(f"\n  REACHES IT UNDER ANOTHER NAME ({len(redirected)}):")
        out.append("  These programs' own source names a different file. An "
                   "OVRDBF in the CL that")
        out.append(f"  calls them redirects that name to {file}, so this is "
                   f"the data they really open.")
        out.append(_paged_table(
            ["program", "its source says", "redirected by", "hops",
             "confidence", "evidence"],
            [(r["program"], r["logical_file"], r["via"], r["hops"],
              r["confidence"], r["evidence"]) for r in redirected], page, 40))
        out.append("  Never `exact`: the mapping may be certain but whether "
                   "the override is still")
        out.append("  in force at the CALL depends on run-time scope, and only "
                   "DLTOVR is in the source.")
    return "\n".join(out)


# ===========================================================================
# 5. where_is_field_used
# ===========================================================================

#: The verb that produced each kind of file. Named in the answer because
#: "where did this come from" is a different question from "what is it", and a
#: conversion list that cannot cite the statement behind each row is not
#: checkable.
_MECHANISM = {
    "TEMPLATE": "DDS or SQL DDL",
    "TEMPLATE-DUP": "DDS or SQL DDL",
    "DATA-LIVE": "$CRTF",
    "DATA-TRANSIENT": "$CRTF then $DLTF",
    "OVERRIDE-ALIAS": "OVRDBF",
    "RUNTIME-VARIABLE": "*CAT at run time",
    "MATCHCODE": "$MC* macro",
    "COPY-TRANSIENT": "$CPYF / $RNMF",
    "NOT-IN-EXTRACT": "referenced only",
    "DATA-BUILT": "$CRTF",
    "LOADED-INTO": "CPYF",
    "LOADED-FROM": "CPYF",
    "SWAPPED-INTO": "RENAME",
    "SWAPPED-FROM": "RENAME",
}

#: What a name turns out to be, once the evidence is in.
#:
#: The taxonomy is the point of this tool. A name that needs no action still
#: gets a row saying why, because an absent row reads as "not affected" - a
#: different and far more expensive claim than "this is an alias that resolves
#: elsewhere". Filtering to the actionable rows is exactly what made the
#: central file of a real ticket disappear from five consecutive answers.
_FILE_STATUS_ACTION = {
    "TEMPLATE": "EDIT - the layout lives here",
    "TEMPLATE-DUP": "RESOLVE - same name in more than one library",
    "DATA-LIVE": "CONVERT - built from a template, object on disk",
    "DATA-TRANSIENT": "none - dropped and rebuilt each run",
    "OVERRIDE-ALIAS": "none - a program-side name only",
    "RUNTIME-VARIABLE": "CONVERT x N - the name is built at run time",
    "MATCHCODE": "REBUILD after conversion",
    "COPY-TRANSIENT": "none - a work or save copy",
    "NOT-IN-EXTRACT": "FLAG - referenced but nowhere on disk",
    "DATA-BUILT": "CONVERT - built from a template by CL; this extraction is "
                  "source only, so the object is not on disk to confirm",
    "LOADED-INTO": "FOLLOW - this data is copied into it; it is the table "
                   "the load feeds",
    "LOADED-FROM": "FOLLOW - it is loaded from this file; a layout change must "
                   "fit both",
    "SWAPPED-INTO": "HIGH RISK - renamed onto it at run time, replacing it; "
                    "both layouts must stay identical",
    "SWAPPED-FROM": "HIGH RISK - replaced at run time by renaming this one "
                    "onto it; change both layouts",
}


def extraction_holds_data() -> bool:
    """Does this extraction carry physical files' data members?

    A full download of the system does: each physical file appears as
    `<LIB>/<FILE>/<FILE>.MBR`, which proves the file exists. A source-control
    checkout does not - it holds source only - and there every data file is
    known from the CL that creates, overrides and deletes it. The two need
    different answers: in the first, a file CL creates but that is not on disk
    is a finding; in the second it is simply how every file looks, and calling
    it "not in the extraction" would flag the whole application.

    Decided from the index each time, so the answer follows whatever tree
    was indexed last.
    """
    try:
        return bool(_rows("SELECT 1 FROM data_member WHERE is_file_object=1 "
                          "LIMIT 1"))
    except Exception:                                     # noqa: BLE001
        return False


def _source_only_note() -> str:
    return ("  This extraction holds source only - no data members - so data "
            "files are known\n  from the CL that creates and overrides them, "
            "and their objects cannot be\n  confirmed on disk.")


@table_aware("name", "resolve_files")
def resolve_files(name: str) -> str:
    """Every physical file a name touches, and what each one is.

    Three name-spaces exist on IBM i and never coincide:

        template     <LIB>/QDDSSRC/<NAME>.dds   field definitions, no rows
        data file    <LIB>/<NAME>/<NAME>.MBR    the rows
        program name just a token                what the COBOL says

    A COBOL program never names a physical file. It names a token, and an
    OVRDBF in whichever CL launched it decides which file that token opens -
    so the same program can be reading orders under one caller and quotations
    under another. Nothing in the program says which.

    Confusing any two of these layers produces a wrong impact list whatever
    the ticket is about: edit the template and forget the conversion, convert
    one file when the template feeds four, or conclude a file is unused
    because its name was only ever an override target.

    Accepts any of the three. Returns all of them.
    """
    name = name.strip().upper()
    rows: List[tuple] = []

    # --- the template -----------------------------------------------------
    templates = _rows("SELECT name, library, member_type FROM object "
                      "WHERE name=? AND member_type IN ('PF','LF','DDS') "
                      "ORDER BY library", name)
    for t in templates:
        status = "TEMPLATE-DUP" if len(templates) > 1 else "TEMPLATE"
        rows.append((status, t["name"], t["library"], "", t["member_type"]))
    # An SQL table's layout lives in the DDL member that creates it.
    sql_defs = _sql_tables("name=? AND kind<>'index'", name)
    for t in sql_defs:
        status = ("TEMPLATE-DUP" if len(templates) + len(sql_defs) > 1
                  else "TEMPLATE")
        rows.append((status, t["name"], t["library"],
                     f"{t['ddl_member']}:{t['line']}",
                     f"SQL {t['kind']}"
                     + (f" '{t['label']}'" if t["label"] else "")))

    # --- what this template creates ---------------------------------------
    created = _rows("SELECT target, cl_member, line, library_expr, confidence "
                    "FROM file_provenance WHERE source=? AND verb='CRTF' "
                    "ORDER BY target", name)
    # --- and what created this name ---------------------------------------
    built_from = _rows("SELECT source, cl_member, line FROM file_provenance "
                       "WHERE target=? AND verb='CRTF' AND source<>''", name)

    seen = {r[1] for r in rows}
    holds_data = extraction_holds_data()
    for c in created:
        target = c["target"]
        if target in seen:
            continue
        seen.add(target)
        obj = _rows("SELECT library FROM data_member WHERE name=? AND "
                    "is_file_object=1", target)
        dropped = _rows("SELECT 1 FROM file_provenance WHERE target=? AND "
                        "verb='DLTF'", target)
        if c["library_expr"]:
            status = "RUNTIME-VARIABLE"
        elif obj:
            status = "DATA-LIVE"
        elif dropped:
            status = "DATA-TRANSIENT"
        elif holds_data:
            status = "NOT-IN-EXTRACT"
        else:
            status = "DATA-BUILT"
        rows.append((status, target, obj[0]["library"] if obj else "",
                     f"{c['cl_member']}:{c['line']}",
                     c["library_expr"] or name))

    # --- the name itself, if it is a data file ----------------------------
    if name not in seen:
        obj = _rows("SELECT library, path FROM data_member WHERE name=? AND "
                    "is_file_object=1 ORDER BY library", name)
        if obj:
            for o in obj:
                rows.append(("DATA-LIVE", name, o["library"],
                             o["path"],
                             built_from[0]["source"] if built_from else ""))
            seen.add(name)
        elif built_from and not holds_data:
            # Asked about a data file directly, in a source-only extraction:
            # the CL that builds it is the evidence there is.
            for b in built_from:
                rows.append(("DATA-BUILT", name, "",
                             f"{b['cl_member']}:{b['line']}", b["source"]))
            seen.add(name)

    # --- copies and renames around it -------------------------------------
    #
    # A copy or rename onto a file that is a real table or template is not a
    # work copy: it is how data reaches the table a process feeds (a load),
    # or how that table is replaced wholesale (a swap). Both used to read
    # "none - a work or save copy", which hid the one hop that decides which
    # table a process writes.
    def _is_real_file(file_name: str) -> bool:
        return bool(_rows("SELECT 1 FROM object WHERE name=? AND member_type "
                          "IN ('PF','LF','DDS') LIMIT 1", file_name)
                    or _sql_tables("name=? AND kind<>'index'", file_name))

    for c in _rows("SELECT verb, target, cl_member, line FROM file_provenance "
                   "WHERE source=? AND verb IN ('CPYF','RNMF') ORDER BY target",
                   name):
        if c["target"] in seen:
            continue
        seen.add(c["target"])
        real = _is_real_file(c["target"])
        status = ("SWAPPED-INTO" if c["verb"] == "RNMF" and real
                  else "LOADED-INTO" if real else "COPY-TRANSIENT")
        rows.append((status, c["target"], "",
                     f"{c['cl_member']}:{c['line']}", name))
    for c in _rows("SELECT verb, source, cl_member, line FROM file_provenance "
                   "WHERE target=? AND source<>'' AND verb IN ('CPYF','RNMF') "
                   "ORDER BY source", name):
        if c["source"] in seen:
            continue
        seen.add(c["source"])
        status = "SWAPPED-FROM" if c["verb"] == "RNMF" else "LOADED-FROM"
        rows.append((status, c["source"], "",
                     f"{c['cl_member']}:{c['line']}", name))

    # --- match-code files -------------------------------------------------
    for m in _rows("SELECT target, cl_member, line FROM file_provenance "
                   "WHERE verb='MC' AND target LIKE ? ORDER BY target",
                   f"{name[:3]}%"):
        if m["target"] in seen:
            continue
        seen.add(m["target"])
        rows.append(("MATCHCODE", m["target"], "",
                     f"{m['cl_member']}:{m['line']}", name))

    # --- program-side names that bind to any of these ---------------------
    targets = [r[1] for r in rows] or [name]
    marks = ",".join("?" * len(targets))
    aliases = _rows(f"SELECT logical_name, COUNT(*) n, "
                    f"COUNT(DISTINCT target_file) files "
                    f"FROM override WHERE target_file IN ({marks}) "
                    f"AND logical_name <> target_file "
                    f"GROUP BY logical_name ORDER BY n DESC", *targets)
    for a in aliases:
        if a["logical_name"] in seen:
            continue
        seen.add(a["logical_name"])
        rows.append(("OVERRIDE-ALIAS", a["logical_name"], "",
                     f"{a['n']} OVRDBF site(s)", ""))

    if not rows:
        return (f"Nothing in the index connects {name} to a physical file.\n"
                f"  That is not proof it has none: a file only appears here "
                f"if a CL creates,\n"
                f"  allocates or overrides it, or if its object directory was "
                f"extracted. Call\n"
                f"  coverage_report to see which libraries contribute "
                f"nothing.")

    # How many CL members bind a program name onto each file. It is the best
    # single measure of how widely a data file is used - a file hundreds of
    # CLs override onto is not one somebody converts quietly - and nothing
    # else in the answer conveys it.
    binds: Dict[str, int] = {}
    targets = [r[1] for r in rows]
    if targets:
        for part, marks in _in_chunks(targets):
            for row in _rows(f"SELECT target_file, COUNT(*) c FROM override "
                             f"WHERE target_file IN ({marks}) "
                             f"GROUP BY target_file", *part):
                binds[row["target_file"]] = row["c"]

    out = [f"{name} - every physical file it touches.", ""]
    out.append(_table(
        ["status", "name", "library", "mechanism", "evidence",
         "from", "overriding CLs"],
        [(s, n, lib or "-", _MECHANISM.get(s, "-"), ev or "-", fr or "-",
          f"{binds.get(n, 0):,}" if binds.get(n) else "-")
         for s, n, lib, ev, fr in rows], 200))

    out.append("")
    for status in sorted({r[0] for r in rows}):
        n = sum(1 for r in rows if r[0] == status)
        out.append(f"  {status:<18}{n:>3}   "
                   f"{_FILE_STATUS_ACTION.get(status, '')}")
    if not holds_data:
        out.append("")
        out.append(_source_only_note())

    # --- the finding nothing else surfaces --------------------------------
    ambiguous = _rows("SELECT logical_name, COUNT(DISTINCT target_file) n, "
                      "GROUP_CONCAT(DISTINCT target_file) t FROM override "
                      "GROUP BY logical_name HAVING n > 1 ORDER BY n DESC")
    mine = [a for a in ambiguous
            if any(t in seen for t in (a["t"] or "").split(","))]
    if mine:
        out.append("")
        out.append("  ** AMBIGUOUS - one program-side name, several files **")
        for a in mine:
            out.append(f"     {a['logical_name']:<14}-> {a['n']} files: "
                       f"{a['t']}")
        out.append("     A program declaring one of these opens a DIFFERENT "
                   "file depending on")
        out.append("     which CL launched it. Nothing in the COBOL says "
                   "which.")

    dups = [r for r in rows if r[0] == "TEMPLATE-DUP"]
    if dups:
        out.append("")
        out.append(f"  ** DUPLICATE TEMPLATE - {name} exists in "
                   f"{len(dups)} libraries. Which one compiles")
        out.append("     depends on the library list at build time. **")
    return "\n".join(out)


def where_is_field_used(field: str, direction: str = "any",
                        page: int = 1) -> str:
    field = field.strip().upper()

    # Match the DDS name, the local name, or a whole-record move.
    #
    # A field almost never appears in a program under its DDS name. It is
    # imported by `COPY DDR-<rec> OF <file> PREFIX BY "AA-"`, which renames
    # every field of the record, so MAILADR exists in the source only as
    # AA-MAILADR - and under a different prefix in every program that copies
    # it. Matching `field = ?` against program text is therefore matching the
    # one spelling that is not there, and it answered "no program references
    # it by that name" for a field nineteen programs use.
    #
    # `dds_field` is what the resolver bound that local name to, using the
    # program's own COPY statements. `group_of` catches `MOVE AA-RORDFILE TO
    # ...`, which touches every field of the record without naming any.
    #
    # Both sides of the group match exclude the empty record. A copybook or
    # screen field has no record, and `group_of` is '' on every row that is
    # not a group move, so `'' IN ('')` matched the whole table: a field
    # defined in a copybook came back written by every program there is.
    where = ("(field = ? OR dds_field = ? OR (group_of <> '' AND group_of IN "
             "(SELECT DISTINCT record FROM field WHERE name = ? "
             "AND record <> '')))")
    args: List = [field, field, field]
    if direction in ("read", "write"):
        where += " AND direction = ?"
        args.append(direction)

    rows = _rows(
        f"SELECT program, direction, file, paragraphs, lines, n, confidence, "
        f"field AS local_name, dds_field, refmod, group_of "
        f"FROM field_usage WHERE {where} "
        # program alone ties hard - WS-RECORDS-READ shares it across
        # 3,215 rows - so the file and the first line finish the key.
        f"ORDER BY direction DESC, program, file, "
        f"{_FIRST_LINE}", *args)

    # Members whose layout could not be expanded. Their field names could not
    # be resolved at all, so "not in the list" means "we cannot say", not
    # "does not use it" - and the difference has to reach the reader.
    unknown = _rows(
        "SELECT DISTINCT member, raw FROM unresolved "
        "WHERE kind = 'unexpanded_layout' ORDER BY member")

    definitions = _rows(
        "SELECT owner, owner_kind, dtype, length, decimals, text, is_key, "
        "values_json FROM field WHERE name=? "
        "ORDER BY owner_kind, owner, record, line", field)

    out: List[str] = []
    if definitions:
        # The declaration that actually carries a type.  `field` holds one
        # row per place the name appears, and a DSPF or PRTF reference
        # carries no dtype and no length - it only names the field.  Those
        # sort first (`owner_kind` 'device' before 'file'), so taking
        # definitions[0] printed the width as "()" on any field that also
        # appears on a screen.  On a widen-this-field ticket that is the one
        # number the report is about.
        typed = [r for r in definitions if r["dtype"]]
        # A DDS file first, then anything typed at all.  A widen happens in
        # the PF or LF; an SQL DDL table carries the same width but is not
        # where the edit goes.
        d = next((r for r in typed if r["owner_kind"] == "file"),
                 typed[0] if typed else definitions[0])
        if d["dtype"]:
            shape = f"{d['dtype']}({d['length']}"
            shape += f",{d['decimals']})" if d["decimals"] else ")"
            shape += f"  on {d['owner']}"
        else:
            shape = ("no width on any declaration - it is referenced by "
                     "screens or reports only")
        out.append(f"{field}  {shape}  {d['text']}".rstrip())

        # Widths that disagree.  A report that quotes one of them and lists
        # every declaring member as "widen this" is wrong about some of
        # them, and nothing else in the output would say so.
        widths = sorted({f"{r['dtype']}({r['length']}"
                         + (f",{r['decimals']})" if r["decimals"] else ")")
                         for r in typed})
        if len(widths) > 1:
            out.append(f"  NOT ONE WIDTH: declared as {', '.join(widths)}. "
                       f"Check each declaring member before changing it.")
        # The members that DECLARE the field, listed - not counted, and not
        # truncated to eight names.
        #
        # This is the set somebody edits to widen a field, so every name in it
        # is work. It was "defined on: " and eight names, which on CUSNAM
        # showed 8 of 51 and silently dropped the rest; counting them instead
        # is no better, because a count cannot be worked from. `_by_library`
        # prints them all under the library that ships them, and 51 names is
        # about 500 characters.
        owners = sorted({r["owner"] for r in definitions})
        out.append(f"  DECLARED BY {len(owners)} member(s) - this is what a "
                   f"change edits:")
        out.append(_by_library(owners))
        if d["values_json"]:
            try:
                values = json.loads(d["values_json"])
                if values:
                    out.append(f"  valid values: {'; '.join(values[:8])}")
            except json.JSONDecodeError:
                pass
    else:
        out.append(f"{field} is not defined on any file - it may be a "
                   f"working-storage name.")

    if not rows and _positional_sites(field):
        rows = []                      # fall through: positional sites are uses
    if not rows and not _positional_sites(field):
        out.append("\n  No program resolves to this field.")
        if unknown:
            out.append(f"  This is NOT the same as 'no program uses it'. "
                       f"{len(unknown):,} member(s) copy a layout that is not "
                       f"in the extraction, so their field names could not be "
                       f"resolved at all - any of them may use it.")
            out.append(_table(["member", "layout it copies"],
                              [(r["member"], r["raw"]) for r in unknown[:20]]))
            if len(unknown) > 20:
                out.append(f"  ... and {len(unknown) - 20:,} more.")
        else:
            out.append("  Every layout in the extraction expanded, so this is "
                       "a real absence rather than a gap.")
        return "\n".join(out)

    writes = [r for r in rows if r["direction"] == "write"]
    reads = [r for r in rows if r["direction"] == "read"]

    # Programs, not occurrences.  The old wording counted rows, so a field
    # touched 20 times by one program was reported as "20 write it", which
    # overstated blast radius by whatever the repeat factor happened to be.
    w_pgm = len({r["program"] for r in writes})
    r_pgm = len({r["program"] for r in reads})
    w_occ = sum(r["n"] for r in writes)
    r_occ = sum(r["n"] for r in reads)
    out.append(f"\n  {w_pgm} program(s) write it ({w_occ:,} occurrence(s)), "
               f"{r_pgm} read it ({r_occ:,} occurrence(s)).")

    def _ev(r) -> str:
        """member:line of the first occurrence, plus how many more."""
        first = (r["lines"] or "").split(",")[0]
        cite = f"{r['program']}:{first}" if first else r["program"]
        return f"{cite} +{r['n'] - 1}" if r["n"] > 1 else cite

    def _called(r) -> str:
        """What the field is CALLED in that program.

        Without this the reader has the right member and cannot find the line:
        they open PGM130 looking for MAILADR and it says AA-MAILADR. A whole
        record move names no field at all, so it says so.
        """
        if r["group_of"]:
            return f"(in {r['group_of']})"
        return r["local_name"]

    def _flag(r) -> str:
        """Width-sensitive: this site breaks if the field is widened."""
        marks = []
        if r["refmod"]:
            marks.append("offset")
        if r["group_of"]:
            marks.append("group move")
        return " ".join(marks)

    sensitive = [r for r in rows if r["refmod"] or r["group_of"]]
    if sensitive:
        out.append(f"\n  WIDTH-SENSITIVE: {len(sensitive)} site(s) assume the "
                   f"current width - a literal offset into the field, or a "
                   f"move of the whole record. These need an EDIT, not just a "
                   f"recompile.")

    # WRITTEN BY is not paged - it is the answer to "what could have put
    # this value here" and it belongs on the first page whole. So it is sent
    # ONCE. Repeating it under every page of READ BY made the pages overlap,
    # cost its full size on every call, and left "page 2" meaning two
    # different things in one answer.
    if writes and page == 1:
        out.append("\n  WRITTEN BY:")
        out.append(_table(
            ["program", "called", "via file", "evidence", "confidence",
             "width"],
            [(r["program"], _called(r), r["file"] or "?", _ev(r),
              r["confidence"], _flag(r)) for r in writes]))
    elif writes:
        out.append(f"\n  WRITTEN BY: {len(writes)} program(s) - on page 1.")
    if reads and direction != "write":
        out.append("\n  READ BY:")
        out.append(_paged_table(["program", "called", "via file", "evidence",
                                 "width"],
                          [(r["program"], _called(r), r["file"] or "?",
                            _ev(r), _flag(r))
                           for r in reads], page, 30))
    positional = _positional_sites(field)
    if positional:
        out.append(f"\n  REACHED BY BYTE POSITION ({len(positional)}) - these "
                   f"statements name no field; the")
        out.append("  offset lands on this one. A size change before it moves "
                   "the bytes they read:")
        out.append(_table(
            ["evidence", "access", "direction", "field bytes", "via"],
            [(f"{r['member']}:{r['line']}",
              f"{r['variable']}({r['start']}:{r['length'] or ''})",
              r["direction"],
              f"{r['field_start']}-{r['field_start'] + r['field_length'] - 1}"
              f" of {r['record'] or r['file']}", r["via"]) for r in positional]))
    if unknown:
        # Stated on every answer, not only the empty one. A list that looks
        # complete is the failure this whole path exists to prevent.
        # Say how much of the ceiling is a LIBRARY problem rather than a
        # parser one. A layout that did not expand because its library was
        # never extracted is not something better parsing will ever fix, and
        # the reader needs to know which kind of gap they are looking at.
        try:
            blind = {r[0] for r in _library_coverage()
                     if r[4] == 0 or r[5].startswith("INDEXED BUT EMPTY")}
        except Exception:                                     # noqa: BLE001
            blind = set()
        if blind:
            out.append("")
            out.append(f"  {len(blind)} librar"
                       f"{'y' if len(blind) == 1 else 'ies'} contribute "
                       f"NOTHING to this index: {', '.join(sorted(blind))}.")
            out.append("  Nothing in them appears above, whether or not it "
                       "uses this field.")
            out.append("  No amount of parsing fixes a library that was "
                       "never read.")
        out.append(f"\n  RECALL CEILING: {len(unknown):,} member(s) copy a "
                   f"layout that is not in the extraction. Their field names "
                   f"could not be resolved, so they are absent from the lists "
                   f"above whether or not they use this field. Call "
                   f"coverage_report for what is missing.")
    return "\n".join(out)


# ===========================================================================
# 6. describe_file
# ===========================================================================

def _positional_sites(field: str) -> List[Any]:
    try:
        return _rows("SELECT member, variable, start, length, direction, line, "
                     "file, record, field_start, field_length, via "
                     "FROM positional_access WHERE field=? "
                     "ORDER BY member, line", field)
    except sqlite3.OperationalError:
        return []


def _describe_sql_table(name: str, tables: List[Any]) -> str:
    """describe_file for a table an SQL DDL member creates."""
    out = [f"{name}  (SQL {tables[0]['kind']})"]
    for t in tables:
        lib = f" in {t['library']}" if t["library"] else ""
        out.append(f"  created by {t['ddl_member']}{lib}, line {t['line']}"
                   + (f"  -  SQL name {t['sql_name']}" if t["sql_name"] != name else ""))
        if t["label"]:
            out.append(f"  LABEL ON TABLE: '{t['label']}'")
        if t["base"]:
            out.append(f"  built over {t['base']}")
    if len(tables) > 1:
        out.append(f"  ** {len(tables)} definitions of {name}. Which one exists "
                   f"depends on the library list. **")
    member = tables[0]["ddl_member"]
    fields = _rows("SELECT name, dtype, length, decimals, text, is_key "
                   "FROM field WHERE owner=? AND record=? ORDER BY line",
                   member, name)
    if fields:
        out.append(f"\n  {len(fields)} columns:")
        out.append(_table(
            ["column", "type", "description"],
            [(f["name"], f"{f['dtype']}({f['length']}"
              + (f",{f['decimals']})" if f["decimals"] else ")"),
              f["text"]) for f in fields], 80))
    writers = _rows(f"SELECT DISTINCT program FROM file_usage WHERE file=? "
                    f"AND {_CAN_CHANGE} ORDER BY program", name)
    readers = _rows(f"SELECT COUNT(DISTINCT program) c FROM file_usage "
                    f"WHERE file=? AND NOT {_CAN_CHANGE}", name)
    out.append(f"\n  can be changed by {len(writers)}: "
               + (", ".join(r["program"] for r in writers[:15]) or "nothing"))
    out.append(f"  read by {readers[0]['c']} programs")
    out.append("  who_touches_file gives both lists with evidence, including "
               "programs that reach it through an OVRDBF.")
    return "\n".join(out)


@table_aware("file", "describe_file")
def describe_file(file: str, library: str = "") -> str:
    file = file.strip().upper()
    clause, args = _in_library(library)
    obj = _rows("SELECT * FROM object WHERE name=?" + clause
                + " ORDER BY library, member_type", file, *args)
    tables = _sql_tables("name=?" + clause.replace("library", "library"),
                         file, *args) if not obj else []
    if tables:
        return _describe_sql_table(file, tables)
    if not obj:
        # A file name absent from `object` is the single most dangerous nil
        # this product returns: on IBM i the name a program uses is usually
        # NOT the name of a file, and a bare "No file called X" has already
        # been read once as "X does not exist" for a name that was an OVRDBF
        # alias onto a live physical file with 186 binding sites.
        where = f" in library {library.strip().upper()}" if library else ""
        return chr(10).join([
            f"No file called {file}{where} - no DDS source member of that "
            f"name.",
            f"  That is not the same as 'no such file'. It may be a "
            f"PROGRAM-SIDE name that an",
            f"  OVRDBF redirects, or a physical file with no source: "
            f"resolve_files({file}) resolves",
            f"  both. find_object({file}) says what the name is; "
            f"coverage_report names the",
            f"  libraries that contribute nothing to this index."])
    o = obj[0]

    out = [f"{file}  ({o['member_type']}){_where(o)}  {o['path']}"]
    if o["based_on"]:
        out.append(f"  a view over {o['based_on']}")

    fields = _rows(
        "SELECT name, dtype, length, decimals, text, heading, is_key, "
        # ORDER BY line, not rowid.  This renders a file's fields in DDS
        # DECLARATION order, which SQLite carried only in the implicit
        # rowid - insertion order from build.write.  PostgreSQL has no
        # rowid and no guaranteed heap order.  `line` is the DDS source
        # line and reproduces it exactly: checked over 200 owners, all
        # 200 identical, 0 NULLs in 103,825 rows, and no (owner, line)
        # pair repeated - so the order is total, stable and meaningful.
        "values_json, record, keywords FROM field WHERE owner=? ORDER BY line",
        file)
    if fields:
        # Byte positions, so `REC(40:1)` or `%SST(&V 40 1)` can be read
        # against the layout. Per record format; after a field of unknown
        # size (an unresolved REFFLD) every later position is unknown.
        spans: Dict[Tuple[str, str], Tuple[Optional[int], Optional[int]]] = {}
        for record in dict.fromkeys(f["record"] or "" for f in fields):
            for name, first, size in layout.dds_spans(
                    (f["name"], f["dtype"], f["length"], f["decimals"],
                     f["keywords"] or "")
                    for f in fields if (f["record"] or "") == record):
                spans[(record, name)] = (first, size)

        def _bytes(f: Any) -> str:
            first, size = spans.get((f["record"] or "", f["name"]), (None, None))
            if first is None or size is None:
                return "?"
            return f"{first}-{first + size - 1}"

        out.append(f"\n  {len(fields)} fields:")
        out.append(_table(
            ["field", "type", "bytes", "key", "description"],
            [(f["name"],
              f"{f['dtype']}({f['length']}" +
              (f",{f['decimals']})" if f["decimals"] else ")"),
              _bytes(f),
              "KEY" if f["is_key"] else "",
              f["text"] or f["heading"]) for f in fields], 80))

    logicals = _rows("SELECT name FROM object WHERE based_on=? ORDER BY name",
                     file)
    if logicals:
        out.append(f"\n  {len(logicals)} logical file(s) over it: "
                   + ", ".join(r["name"] for r in logicals[:20]))

    #: The same predicate who_touches_file uses.  Two tools answering "who
    #: writes CUSMAS" with two different numbers - 3 here and 5 there, because
    #: this one read only the OPEN mode - is worse than either being wrong on
    #: its own: an agent cannot tell which to believe.
    writers = _rows(
        f"SELECT DISTINCT program FROM file_usage WHERE file=? "
        f"AND {_CAN_CHANGE} ORDER BY program", file)
    readers = _rows(
        f"SELECT COUNT(DISTINCT program) c FROM file_usage WHERE file=? "
        f"AND NOT {_CAN_CHANGE}", file)
    out.append(f"\n  can be changed by {len(writers)}: "
               + (", ".join(r["program"] for r in writers[:15]) or "nothing"))
    out.append(f"  read by {readers[0]['c']} programs")
    out.append("  who_touches_file gives the same two lists with evidence, "
               "and folds in the logicals.")
    return "\n".join(out)


# ===========================================================================
# 7. resolve_overrides
# ===========================================================================

def resolve_overrides(program: str) -> str:
    program = program.strip().upper()
    try:
        rows = _rows(
            "SELECT cl_program, logical_name, target_file, library, share, "
            "lvlchk, kind, line, confidence FROM override WHERE cl_program=? "
            "ORDER BY line", program)
    except sqlite3.OperationalError:
        # an index built before LVLCHK was recorded
        rows = _rows(
            "SELECT cl_program, logical_name, target_file, library, share, "
            "'' AS lvlchk, kind, line, confidence FROM override "
            "WHERE cl_program=? ORDER BY line", program)
    if not rows:
        drivers = _rows(
            "SELECT DISTINCT caller FROM call_edge WHERE callee=? "
            "AND kind IN ('cl','sbmjob')", program)
        if drivers:
            names = [d["caller"] for d in drivers]
            out = [f"{program} declares no overrides itself. It is called by "
                   f"{', '.join(names)}, which may override its files:"]
            for name in names[:3]:
                out.append("\n" + resolve_overrides(name))
            return "\n".join(out)
        return (f"No overrides found for {program}, and nothing in the source "
                f"calls it, so the files it opens are whatever the library "
                f"list resolves to at run time.")

    out = [f"{program} overrides these files before calling the program.",
           "This is what decides which physical file is really opened - the "
           "file name inside the COBOL never changes.\n"]
    out.append(_table(
        ["logical", "becomes", "library", "share", "lvlchk", "kind", "evidence",
         "confidence"],
        [(r["logical_name"], r["target_file"], r["library"] or "?",
          "yes" if r["share"] else "", r["lvlchk"], r["kind"],
          f"{r['cl_program']}:{r['line']}", r["confidence"]) for r in rows]))
    # LVLCHK(*NO) switches off the one run-time check that catches a program
    # compiled against an older record format. Add a field to the file and
    # the program opens it without CPF4131, reading every field after the
    # insertion point shifted.
    unchecked = [r for r in rows if (r["lvlchk"] or "").upper() == "*NO"]
    if unchecked:
        out.append(f"\n  RISK: {len(unchecked)} override(s) set LVLCHK(*NO): "
                   + ", ".join(sorted({r["logical_name"] for r in unchecked})))
        out.append("  A record-format change to these files is NOT caught when "
                   "the program opens them.")
        out.append("  Every program reached through this CL must be recompiled "
                   "with the format change;")
        out.append("  none of them will fail to tell you it was missed.")
    # A redirect is the case that actually changes which data is read, so it is
    # called out rather than left for the reader to spot by comparing two
    # columns. Every override in the replica maps a name to itself in another
    # library, which is why this distinction was easy to miss.
    renames = [r for r in rows if r["logical_name"] != r["target_file"]]
    if renames:
        out.append(f"\n  {len(renames)} of these RENAME the file, not just its "
                   f"library:")
        for r in renames:
            out.append(f"    the program's source says {r['logical_name']}, "
                       f"it really opens {r['target_file']}")
        out.append("  A search for the program's own file name will not find "
                   "that data, and a")
        out.append("  search for that data will not find the program - unless "
                   "it goes through")
        out.append("  file_resolved. who_touches_file lists them under "
                   "REACHES IT UNDER ANOTHER NAME.")

    inferred = [r for r in rows if r["confidence"] != "exact"]
    if inferred:
        out.append(f"\n  {len(inferred)} of these resolve through a CL "
                   f"variable, so the library is inferred rather than certain.")
    return "\n".join(out)


def program_interface(program: str) -> str:
    """The parameters a program receives, what each caller passes, and every
    size that does not match.

    A CL CALL passes its PARM variables by reference, and the called program
    lays its LINKAGE record over that storage. Nothing compares the sizes: a
    200-byte variable passed to a 1,000-byte record compiles, runs, and lets
    the program read and write 800 bytes it does not own.
    """
    name = program.strip().upper()
    try:
        params = _rows("SELECT position, name, bytes, via, source, line, "
                       "confidence FROM linkage_param WHERE program=? "
                       "ORDER BY position", name)
        passed = _rows("SELECT caller, position, argument, bytes, line "
                       "FROM call_argument WHERE callee=? "
                       "ORDER BY caller, line, position", name)
        makes = _rows("SELECT callee, position, argument, bytes, line "
                      "FROM call_argument WHERE caller=? "
                      "ORDER BY line, position", name)
        wrong = _rows("SELECT caller, callee, position, argument, caller_bytes, "
                      "callee_item, callee_bytes, kind, line FROM param_mismatch "
                      "WHERE callee=? OR caller=? ORDER BY caller, line, position",
                      name, name)
    except sqlite3.OperationalError:
        return (f"This index was built before parameter sizes were recorded. "
                f"Rebuild it to check what {name} receives and is passed.")
    who = _rows("SELECT member_type, language, library FROM object WHERE name=?",
                name)
    if not who:
        return f"No member called {name}."

    out: List[str] = []
    if params:
        out.append(f"{name} receives {len(params)} parameter(s) "
                   f"(PROCEDURE DIVISION USING):")
        out.append(_table(
            ["#", "item", "bytes", "layout from", "evidence", "confidence"],
            [(p["position"], p["name"], _size(p["bytes"]), p["via"] or "(inline)",
              f"{p['source'] or name}:{p['line']}", p["confidence"])
             for p in params]))
        unsized = [p for p in params if p["bytes"] is None]
        if unsized:
            out.append(f"  {len(unsized)} parameter(s) cannot be sized, so no "
                       f"caller is checked against them - read the LINKAGE "
                       f"SECTION and its copybooks.")
    elif any(w["language"] == "cobol" for w in who):
        out.append(f"{name} has no PROCEDURE DIVISION USING: it receives no "
                   f"parameters.")

    if passed:
        out.append(f"\nCL calls to {name}, with argument sizes:")
        out.append(_calls_table(passed, "caller", name, inbound=True))

    if makes:
        out.append(f"\nCalls {name} makes, with argument sizes:")
        out.append(_calls_table(makes, "callee", name, inbound=False))

    if wrong:
        out.append(f"\nMISMATCHES ({len(wrong)}) - nothing on the system "
                   f"checks these:")
        out.append(_table(
            ["kind", "call", "#", "passes", "receives"],
            [(w["kind"].upper(), f"{w['caller']}:{w['line']} -> {w['callee']}",
              w["position"],
              f"{w['argument'] or '(nothing)'} {_size(w['caller_bytes'])}",
              f"{w['callee_item'] or '(nothing)'} {_size(w['callee_bytes'])}")
             for w in wrong]))
        if any(w["kind"] == "overrun" for w in wrong):
            out.append("  OVERRUN: the program's record is longer than the "
                       "storage passed. Writes past the")
            out.append("  end corrupt whatever the caller holds next; reads "
                       "return that storage's bytes.")
        if any(w["kind"] == "missing" for w in wrong):
            out.append("  MISSING: the program uses a parameter the call does "
                       "not pass (MCH3601 at run time).")
    elif params and passed:
        out.append("\nEvery sized CL argument matches the parameter it lands on.")

    # Say what was not checked: a zero is only worth the list behind it.
    cobol_callers = _rows(
        "SELECT DISTINCT caller FROM call_edge WHERE callee=? AND kind='cobol' "
        "ORDER BY caller", name) if params else []
    if cobol_callers:
        shown = ", ".join(r["caller"] for r in cobol_callers[:10])
        more = (f" and {len(cobol_callers) - 10} more"
                if len(cobol_callers) > 10 else "")
        out.append(f"\nNOT SIZE-CHECKED: {len(cobol_callers)} COBOL caller(s) "
                   f"({shown}{more}). Compare their CALL ... USING items with "
                   f"the parameters above in the source.")
    if not out:
        return (f"{name} receives no parameters the index can see, and no CL "
                f"call passes it any.")
    return "\n".join(out)


def _calls_table(rows: Sequence[Any], other: str, name: str,
                 inbound: bool) -> str:
    """One row per CALL statement, its arguments and their sizes."""
    calls: Dict[Tuple[str, int], List[Any]] = {}
    for a in rows:
        calls.setdefault((a[other], a["line"]), []).append(a)
    return _table(
        ["call", "arguments (bytes)"],
        [((f"{who}:{line}" if inbound else f"{who} at {name}:{line}"),
          "  ".join(f"{a['argument']}={_size(a['bytes'])}" for a in args))
         for (who, line), args in calls.items()])


def _size(size: Optional[int]) -> str:
    return "?" if size is None else f"{size:,}"


# ===========================================================================
# 8. explain_screen
# ===========================================================================

#: Member types that HAVE record formats. Anything else asked of
#: explain_screen is a wrong-tool question, not a missing member.
_SCREEN_TYPES = {"DSPF", "PRTF", "MNUDDS"}


def explain_screen(device_file: str) -> str:
    name = device_file.strip().upper()
    formats = _rows(
        "SELECT name, kind, keywords, line FROM format WHERE device_file=? "
        "ORDER BY line", name)
    if not formats:
        # "No display or printer file called X" was returned for members that
        # find_object resolves perfectly well - two tools contradicting each
        # other about the same name in the same session. The member exists;
        # what it has no rows for is FORMATS, and the usual reason is on its
        # own parse_status. Say which of those two facts is true.
        who = _rows("SELECT member_type, library, parse_status, notes "
                    "FROM object WHERE name=?", name)
        if not who:
            return chr(10).join([
                f"No display or printer file called {name}, and no member of "
                f"that name either.",
                f"  Call find_object({name}) - a name absent here can still "
                f"be a physical file or an override alias."])
        r = who[0]
        kind = (r["member_type"] or "?").upper()
        where = r["library"] or "?"
        out = [f"{name} is a {kind} member in {where}, and no record formats "
               f"were extracted from it."]
        if kind not in _SCREEN_TYPES:
            # Not a device file at all - the caller asked the wrong tool, and
            # saying "no display file called X" invited them to conclude the
            # name does not exist.
            out.append(f"  It is not a display or printer file, so it has no "
                       f"screen formats to explain.")
            out.append(f"  find_object({name}) says what it is; "
                       f"what_it_touches({name}) says which device files it "
                       f"opens.")
            return chr(10).join(out)
        out.append(f"  parse status: {r['parse_status']}")
        if r["notes"]:
            for note in str(r["notes"]).split(" | "):
                out.append(f"    {note}")
        out.append("  This is a device file whose formats could NOT be read - "
                   "not a screen with no fields.")
        out.append("  Treat its layout as unknown: read the source, or obtain "
                   "the real display file")
        out.append("  if this member is generated metadata shadowing one.")
        out.append(f"  read_member({name}) shows what is actually in it.")
        return chr(10).join(out)

    out = [f"{name}: {len(formats)} record format(s)"]
    out.append(_table(["format", "kind", "keywords"],
                      [(f["name"], f["kind"], (f["keywords"] or "")[:44])
                       for f in formats]))

    keys = _rows(
        "SELECT DISTINCT number, purpose FROM indicator WHERE member=? "
        "AND role='defined' AND purpose LIKE 'C%' ORDER BY number", name)
    if keys:
        out.append("\n  function keys:")
        out.append(_table(["indicator", "purpose"],
                          [(k["number"], k["purpose"]) for k in keys]))

    users = _rows(
        "SELECT DISTINCT program, format_name, verb, line, confidence "
        "FROM format_usage WHERE device_file=? ORDER BY program, line", name)
    if users:
        out.append(f"\n  used by {len({u['program'] for u in users})} program(s):")
        out.append(_table(["program", "format", "verb", "evidence"],
                          [(u["program"], u["format_name"], u["verb"],
                            f"{u['program']}:{u['line']}") for u in users], 30))

    defined = {f["name"] for f in formats}
    bad = [u for u in users if u["format_name"] not in defined]
    if bad:
        out.append(f"\n  ! {len(bad)} program reference(s) name a format this "
                   f"file does not define - that would fail at run time:")
        for u in bad[:6]:
            out.append(f"    {u['program']}:{u['line']} writes "
                       f"'{u['format_name']}'")
    return "\n".join(out)


# ===========================================================================
# 9. read_member
# ===========================================================================

def read_member(name: str, start: int = 1, count: int = 120,
                library: str = "", member_type: str = "") -> str:
    """Source lines. `member_type` picks between members sharing a name.

    One name is routinely two members: a file and the copybook that
    lays it out carry the same name in the same library, and asking for
    the name alone returned whichever sorted first - the copybook. The
    DDS, which is what a layout question is about, could not be reached
    at all.
    """
    name = name.strip().upper()
    clause, args = _in_library(library)
    wanted_type = member_type.strip().upper().lstrip(".")
    type_clause = ""
    if wanted_type:
        type_clause = " AND member_type=?"
        args = list(args) + [wanted_type]
    rows = _rows("SELECT path, lines, member_type, library FROM object "
                 "WHERE name=?" + clause + type_clause +
                 " ORDER BY library, member_type",
                 name, *args)
    if not rows and wanted_type:
        have = _rows("SELECT DISTINCT member_type FROM object "
                     "WHERE name=?", name)
        if have:
            return (f"{name} has no {wanted_type} member. It exists as: "
                    + ", ".join(r["member_type"] for r in have))
    if not rows:
        if library:
            return (f"No member called {name} in library "
                    f"{library.strip().upper()}.")
        return chr(10).join([
            f"No member called {name} in the index.",
            f"  find_object({name}) does partial matching and says whether the name is a",
            f"  physical file or an override alias rather than a source member."])
    note = _ambiguous(rows, name, library)
    path = config.resolve_path(rows[0]["path"])
    total = rows[0]["lines"]
    if path is None or not path.exists():
        return (f"{name} is in the index but its source file is not where the "
                f"index says.\n"
                f"  looked for   : {path}\n"
                f"  source root  : {config.SOURCE_ROOT}\n"
                f"Either the source tree moved since it was indexed, or "
                f"ALPHAPLUS_SOURCE points somewhere else. The facts are still "
                f"valid; only reading the text needs the file.")

    start = max(1, start)
    count = max(1, min(count, 400))
    out = [f"{name} ({rows[0]['member_type']}"
           + (f" in {rows[0]['library']}" if rows[0]["library"] else "")
           + f") lines {start}-{min(start + count - 1, total)} of {total:,}"]
    if note:
        out.append(note)
    out.append("")
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for number, line in enumerate(fh, 1):
            if number < start:
                continue
            if number >= start + count:
                out.append(f"\n... {total - number + 1:,} more lines. "
                           f"Call again with start={number}.")
                break
            out.append(f"{number:6d}  {line.rstrip()}")
    return "\n".join(out)


# ===========================================================================
# 10. search_source
# ===========================================================================

#: Lines SHOWN from any one member. Not a limit on what is counted.
#:
#: It was three, and the line loop `break`-ed at it - so occurrences past the
#: third were never counted either, and the header announced "26 match(es)"
#: when 26 was merely the number it had chosen to print. A count that names
#: the wrong population is the one thing an impact report must never carry.
#:
#: Counting and display are now separate: every match is counted, this caps
#: only what is printed, and the header names each member whose lines were
#: withheld. Twelve rather than three because in this codebase the fourth
#: occurrence in a member is routinely the interesting one - a reference
#: modification, or a move into a differently sized field - and `per_member`
#: raises it per call for a caller that wants all of them.
PER_MEMBER = 12

#: Seconds one search may run before it gives up.
#:
#: This was 12, sized for an HTTP transport where something upstream would
#: time the request out first. Over stdio nothing does: the agent launches
#: the server itself and waits exactly as long as the answer takes, so a
#: budget buys nothing there and costs correctness.
#:
#: What it costs is the worst failure a search tool has. A sweep that stops
#: at 93% prints the same shape of answer as one that finished, and a member
#: it never reached is indistinguishable from a member that did not match.
#: On one estate eight consecutive sweeps stopped short, none of them ever
#: completed, and a record layout the whole analysis depended on was absent
#: from the results with nothing to say it had not been looked at.
#:
#: An hour is not a target. It is "no practical limit", written as a number
#: so that a genuinely stuck scan still ends rather than hanging a session.
SEARCH_BUDGET = float(config.setting(
    "ALPHAPLUS_SEARCH_BUDGET", "search", "budget_seconds", default=3600.0))

#: kb.index.build.decode_member, resolved on first use.
_DECODER = None

#: Cached listing of the source tree, filled on first search.
_ALL_PATHS: Optional[List[Path]] = None


def _decode(raw: bytes):
    """kb.index.build.decode_member, imported once and lazily.

    Lazily because kb.index.build pulls in every parser, and a search tool
    should not pay that at module import; once because a search reads
    thousands of files and an import lookup per file is not free.
    """
    global _DECODER
    if _DECODER is None:
        from kb.index.build import decode_member
        _DECODER = decode_member
    return _DECODER(raw)


def _members_of_type(member_type: str) -> List[Path]:
    """Source files, optionally narrowed to one member type.

    Narrowing is the entire point of this tool: '.PF' is 450 members where the
    tree is 13,366, so a scoped search costs a thirtieth of an unscoped one.
    """
    wanted = member_type.strip().upper().lstrip(".")
    extensions = {ext for ext, kind in config.EXT_TO_TYPE.items()
                  if not wanted or kind == wanted}
    # Types derived from the extension's own name (a shop's CBLLECPY is a
    # copybook) are in no table, so a wanted type may match no listed
    # extension and still be held by files on disk.
    derived = wanted in set(config.TYPE_TO_LANG)

    # A member type is a LOGICAL kind; an extension is whatever the
    # person who wrote the dump chose - the platform supplies none.
    # They agree on some trees and not on others: ask for PF where an
    # extraction spells its DDS `.dds` and the exact match finds
    # nothing, so the tool reports "no members" about the type that
    # holds most of the estate. A false nil gets believed.
    #
    # When the exact kind matches nothing, fall back to the LANGUAGE
    # it belongs to - PF, LF, DSPF and PRTF are all DDS; CBLLE and
    # SQLCBLLE are both COBOL. TYPE_TO_LANG already holds that, so
    # this is not a second table to keep in step.
    if wanted and not extensions and not derived:
        return []

    global _ALL_PATHS
    if _ALL_PATHS is None:
        # walking 13,366 paths on every search adds up; the tree does not
        # move under a running server
        _ALL_PATHS = [p for p in sorted(config.SOURCE_ROOT.rglob("*"))
                      if p.is_file()
                      and not any(part in config.IGNORE_DIRS
                                  for part in p.parts)]
    # Sorted, not walk order. rglob's order is not defined, and while the
    # cache makes it stable inside one process, a paged answer that
    # changes shape when the server restarts is the same bug with a
    # longer fuse.
    def kind_of(p: Path) -> str:
        return config.member_type_for(p.suffix)

    found = sorted((p for p in _ALL_PATHS
                    if (kind_of(p) == wanted if wanted else bool(kind_of(p)))),
                   key=lambda p: (p.stem.upper(), str(p)))
    # Members the index typed from their content - a file written out with no
    # extension - carry no type in their name at all.
    found += _content_typed_paths(wanted)
    if found or not wanted:
        return found

    # The kind is mapped but no FILE carries it. That is the case the fallback
    # is for, and testing the mapping instead of the files is what made this
    # look fixed when it was not: `.PF` is in EXT_TO_TYPE on every install, so
    # `extensions` is never empty for PF - it is the extraction that spells
    # its DDS differently.
    language = config.TYPE_TO_LANG.get(wanted, "")
    if not language:
        return []
    return sorted((p for p in _ALL_PATHS
                   if config.TYPE_TO_LANG.get(kind_of(p), "") == language),
                  key=lambda p: (p.stem.upper(), str(p)))


def _content_typed_paths(wanted: str) -> List[Path]:
    """Indexed members whose name carries no member type, narrowed to one type.

    A file with no extension, or a source member written out as `.MBR`, was
    typed by the indexer from its content and folder. The index holds that
    type; the file name never will.
    """
    try:
        rows = _rows("SELECT path, member_type FROM object")
    except Exception:
        return []
    out = []
    for r in rows:
        path = config.resolve_path(r["path"])
        if path is None or config.member_type_for(path.suffix):
            continue
        if wanted and r["member_type"] != wanted:
            continue
        out.append(path)
    return sorted(out, key=lambda p: (p.name.upper(), str(p)))


def search_source(pattern: str, member_type: str = "",
                  limit: int = 40, page: int = 1,
                  per_member: int = 0) -> str:
    """Text search over the source, scoped by member type.

    Implemented in pure Python rather than by shelling out to ripgrep. The
    knowledge base has no dependencies beyond the standard library anywhere
    else, and it is going to run inside a client network where installing a
    binary is a change request. It used to call `rg` and simply failed when
    that was not installed, which is worse than not offering the tool at all -
    an agent spends a turn discovering it does nothing.
    """
    try:
        matcher = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return (f"{pattern!r} is not a valid regular expression: {exc}\n"
                f"If you meant it literally, escape it.")

    types_known = sorted({kind for kind in config.EXT_TO_TYPE.values()})
    requested = _requested_types(member_type)
    unknown = [t for t in requested if t not in types_known
               and t not in config.TYPE_TO_LANG]
    if unknown:
        wanted = unknown[0]
    elif len(requested) > 1:
        wanted = member_type.strip().upper()       # as asked: CODE, or a list
    else:
        wanted = requested[0] if requested else ""
    if unknown:
        # Say what each type actually HOLDS. A bare list of names invites
        # probing types that exist in the mapping and in no extraction -
        # several wasted calls, and a false nil each time.
        held = {r["member_type"]: r["c"] for r in _rows(
            "SELECT member_type, COUNT(*) c FROM object GROUP BY member_type")}
        try:
            n_data = _rows("SELECT COUNT(*) c FROM data_member")[0]["c"]
        except Exception:                                     # noqa: BLE001
            n_data = 0
        listed = ", ".join(f"{k} ({held[k]:,})"
                           for k in sorted(held) if held[k])
        empty = sorted(k for k in types_known if not held.get(k))
        msg = [f"No member type {wanted!r} in this extraction."]
        if listed:
            msg.append(f"  Types that hold members: {listed}")
        if empty:
            msg.append(f"  Mapped but EMPTY here: {', '.join(empty)}")
        if n_data:
            msg.append(f"  {n_data:,} data member(s) are inventoried and hold "
                       f"no source - they are not searchable.")
            msg.append("  Call resolve_files to see what they are.")
        return chr(10).join(msg)

    # An unscoped search used to be refused, because under the old
    # 12-second budget it could only ever return the INCOMPLETE banner plus
    # whatever it happened to reach. That refusal pushed callers into
    # sweeping one member type at a time, and a per-type sweep is where a
    # layout goes missing: whoever runs it has to name every type that could
    # hold a hit, and a type they do not think of is silently absent from
    # the answer rather than reported as unsearched.
    #
    # With no practical budget the unscoped case simply finishes - slower,
    # and complete. Complete is the property that matters.

    if len(requested) > 1:
        seen_paths: Dict[str, Path] = {}
        for kind in requested:
            for path in _members_of_type(kind):
                seen_paths.setdefault(str(path), path)
        paths = sorted(seen_paths.values(), key=lambda p: (p.stem.upper(), str(p)))
    else:
        paths = _members_of_type(wanted)
    if not paths:
        return (f"No members to search under {config.SOURCE_ROOT}"
                + (f" of type {wanted}." if wanted else ".")
                + _unsearched_types(requested))

    started = time.time()
    hits: List[Tuple[str, int, str]] = []
    scanned = 0
    ran_out = False
    #: 0 means "the default"; a caller asking for more gets it, and a caller
    #: asking for everything passes something large. No silent ceiling.
    cap = per_member if per_member and per_member > 0 else PER_MEMBER
    total_matches = 0
    withheld: Dict[str, int] = {}

    for path in paths:
        if time.time() - started > SEARCH_BUDGET:
            ran_out = True
            break
        scanned += 1
        try:
            # The SAME decoder the index used, not utf-8-with-replacement.
            # This source is ISO-8859-1 out of EBCDIC CCSID 273 and roughly
            # 95% of it is not valid UTF-8, so `errors="replace"` put U+FFFD
            # in place of every accented character - a search for a pattern
            # containing one could never match, and the tool answered "no
            # match" for text plainly in the file. Tabs are expanded for the
            # same reason the parser expands them: a tab inside columns 1-7
            # shifts every column on the line.
            text, _ = _decode(path.read_bytes())
            if "	" in text:
                text = chr(10).join(l.expandtabs(8)
                                    for l in text.split(chr(10)))
        except OSError:
            continue
        if not matcher.search(text):        # cheap reject before going by line
            continue
        member = path.stem.upper()
        found_here = 0
        for number, line in enumerate(text.split(chr(10)), start=1):
            if matcher.search(line):
                found_here += 1
                # Count first, print second. The old loop broke out at the
                # cap, so the matches past it were never counted and the
                # header reported the number PRINTED as the number FOUND.
                if found_here <= cap:
                    hits.append((member, number, line.strip()))
        if found_here:
            total_matches += found_here
            if found_here > cap:
                withheld[member] = found_here - cap

    # Name the extensions actually searched, not the type that was asked
    # for. When a kind is mapped but no file carries it, _members_of_type
    # widens to the language - and a header claiming ".PF members" over a
    # result found in .dds files is a quiet lie about where the answer
    # came from.
    _searched = sorted({p.suffix.upper() or "(no extension)" for p in paths})
    if not wanted:
        scope = ""
    elif _searched == [f".{wanted}"]:
        scope = f" in .{wanted} members"
    else:
        scope = (f" in {wanted} members (searched "
                 f"{', '.join(_searched)})")
    not_searched = _unsearched_types(requested)
    if not hits:
        note = (f" (searched {scanned:,} of {len(paths):,} before running out "
                f"of time)" if ran_out else f" (searched {scanned:,} members)")
        return f"No match for {pattern!r}{scope}.{note}{not_searched}"

    members = len({m for m, _n, _l in hits})
    out = [f"{total_matches:,} match(es) across {members} member(s) for "
           f"{pattern!r}{scope}"]
    if withheld:
        # Say it BEFORE the lines, not in a footer: a reader who stops at the
        # first screen must still learn that a member has more.
        worst = sorted(withheld.items(), key=lambda kv: -kv[1])[:6]
        out.append(f"  {len(hits):,} line(s) shown - {cap} per member. "
                   f"{sum(withheld.values()):,} further match(es) exist in "
                   f"{len(withheld):,} member(s):")
        out.append("    " + "  ".join(f"{m} +{n}" for m, n in worst)
                   + ("  ..." if len(withheld) > 6 else ""))
        out.append(f"  Raise per_member to see them.")
    window, head, foot = _slice(hits, page, limit)
    if head:
        out.append(head)
    for member, number, line in window:
        out.append(f"  {member}:{number}  {line[:90]}")
    if foot:
        out.append(foot)
    if ran_out:
        # Naming this a HARD STOP, not "incomplete": the old wording said
        # "narrow with member_type to see the rest", which does not resume
        # anything - it searches a different set of members, so the part
        # skipped here stays unsearched and looks like it matched nothing.
        out.append(f"  *** HARD STOP after {SEARCH_BUDGET:.0f}s - "
                   f"{scanned:,} of {len(paths):,} member(s) searched. ***")
        out.append(f"  The remaining {len(paths) - scanned:,} were NOT read. "
                   f"They are unknown, not unmatched.")
        out.append("  Raise ALPHAPLUS_SEARCH_BUDGET and run it again; do not "
                   "report a count from this result.")
    out.append(f"  (at most {cap} lines shown per member)")
    if not_searched:
        out.append(not_searched.strip("\n"))
    return "\n".join(out)


#: Languages that hold logic or statements a change can reach: everything a
#: sweep for a field, a name or a literal must cover. DDS and message files are
#: declarations and are searched by naming their type.
_CODE_LANGUAGES = ("cobol", "copybook", "cl", "clcmd", "cmd", "rpg", "sql", "bnd")


def _requested_types(member_type: str) -> List[str]:
    """`CBLLE`, `CBLLE,CLLE`, or `CODE` (every code type) -> member types."""
    out: List[str] = []
    for part in (member_type or "").replace(";", ",").split(","):
        kind = part.strip().upper().lstrip(".")
        if not kind:
            continue
        if kind == "CODE":
            out.extend(t for t, lang in config.TYPE_TO_LANG.items()
                       if lang in _CODE_LANGUAGES)
        else:
            out.append(kind)
    return list(dict.fromkeys(out))


def _unsearched_types(requested: List[str]) -> str:
    """The member types this index holds that a scoped search did not read.

    A sweep scoped to one type answers for that type and is silent about the
    rest, and silence reads as "no match". Naming what was skipped turns it
    into a gap the reader can close.
    """
    if not requested:
        return ""
    held = _rows("SELECT member_type, COUNT(*) c FROM object GROUP BY member_type")
    skipped = [(r["member_type"], r["c"]) for r in held
               if r["member_type"] not in requested and r["c"]]
    if not skipped:
        return ""
    code = sorted((t, c) for t, c in skipped
                  if config.TYPE_TO_LANG.get(t, "") in _CODE_LANGUAGES)
    other = sorted((t, c) for t, c in skipped if (t, c) not in code)
    lines = ["", "  NOT SEARCHED:"]
    if code:
        lines.append("    code types " + ", ".join(f"{t} ({c:,})" for t, c in code)
                     + " - use member_type='CODE' to search all code")
    if other:
        lines.append("    other types " + ", ".join(f"{t} ({c:,})" for t, c in other))
    return "\n".join(lines)


# ===========================================================================
# 11. coverage_report
# ===========================================================================

#: Fact tables and the column naming the member each row came from.
_FACT_OWNERS = (("call_edge", "caller"), ("file_usage", "program"),
                ("copy_edge", "program"), ("field", "owner"),
                ("field_usage", "program"), ("sql_usage", "program"),
                ("format", "device_file"), ("override", "cl_program"),
                ("menu_option", "menu"), ("message", "member"))


def _members_with_facts() -> Dict[str, int]:
    """library -> how many of its members yielded at least one fact."""
    try:
        union = " UNION ".join(f"SELECT {column} AS name FROM {table}"
                               for table, column in _FACT_OWNERS)
        rows = _rows(f"SELECT UPPER(o.library) AS library, COUNT(*) c "
                     f"FROM object o WHERE o.library <> '' AND o.name IN "
                     f"(SELECT name FROM ({union})) GROUP BY UPPER(o.library)")
    except sqlite3.OperationalError:
        return {}
    return {r["library"]: r["c"] for r in rows}


def _library_coverage() -> "List[tuple]":
    """Every library on disk, and how much of it the index actually holds.

    A total says how many members were read. It cannot say which libraries
    were not read at all, and those are not the same fact: a library that
    contributes nothing to an answer because it has no bearing on the question
    reads exactly like a library that was never opened.

    That distinction decided a real analysis. Three libraries in an extraction
    were directory skeletons - every subdirectory present, every one empty -
    and the report said only "11,579 members indexed". Thirty-one per cent of
    the change set lived in those three, and nothing on the page hinted at it.

    So the unit here is the LIBRARY DIRECTORY as it exists on disk, walked
    from the source root, not the set of libraries that happen to appear in
    `object`. A library missing from `object` is precisely the case worth
    printing.
    """
    root = config.SOURCE_ROOT
    if not root.exists():
        return []

    indexed: Dict[str, int] = {}
    for row in _rows("SELECT library, COUNT(*) c FROM object "
                     "WHERE library <> '' GROUP BY library"):
        indexed[row["library"].upper()] = row["c"]

    # Where the library directories are. Not necessarily the top level: an
    # SVN checkout wraps them - <branch>/<source root>/<LIB>/QCBLLESRC/... - and
    # treating the source-root folder as a library reported the real ones as never read.
    # The index knows where each member's library directory is (the parent of
    # its source-file directory); the library directories are those and every
    # sibling of them, so a library that is only an empty skeleton still
    # appears, which is the case this report exists for.
    parents = set()
    for row in _rows("SELECT path FROM object WHERE library <> ''"):
        member = config.resolve_path(row["path"])
        if member is not None and len(member.parts) >= 3:
            parents.add(member.parent.parent.parent)
    if not parents:
        parents = {root}

    by_name: Dict[str, List[Path]] = {}
    for parent in sorted(parents):
        try:
            children = sorted(parent.iterdir())
        except OSError:
            continue
        for entry in children:
            if entry.is_dir() and entry.name not in config.IGNORE_DIRS:
                by_name.setdefault(entry.name.upper(), []).append(entry)

    with_facts = _members_with_facts()
    out = []
    for key in sorted(by_name):
        entries = by_name[key]
        files = recognised = unmapped = 0
        for entry in entries:
            for path in entry.rglob("*"):
                if not path.is_file():
                    continue
                files += 1
                if config.member_type_for(path.suffix):
                    recognised += 1
                else:
                    unmapped += 1
        entry = entries[0]
        held = indexed.get(key, 0)
        if files == 0:
            status = "EMPTY - directory skeleton, no files at all"
        elif held == 0 and recognised == 0:
            status = "ONLY UNMAPPED - nothing here is indexable"
        elif held == 0:
            status = "NOT INDEXED - files present, none in the index"
        elif not with_facts.get(key):
            # Read, and nothing came out of it. Counted as indexed, which is
            # true, and reads as "covered", which is not: every answer about
            # these members is empty for the same reason as a library never
            # read at all.
            status = "INDEXED BUT EMPTY - members read, no facts extracted"
        elif unmapped > recognised:
            status = "partial - more unmapped than recognised"
        else:
            status = "ok"
        out.append((entry.name, files, recognised, unmapped, held, status))
    return out


def coverage_report() -> str:
    out = ["What this knowledge base can and cannot see.\n"]
    total = _rows("SELECT COUNT(*) c FROM object")[0]["c"]
    states = _rows("SELECT parse_status, COUNT(*) c FROM object "
                   "GROUP BY parse_status")
    out.append(f"  {total:,} members indexed: "
               + ", ".join(f"{s['parse_status']} {s['c']:,}" for s in states))
    if extraction_holds_data():
        objects = _rows("SELECT COUNT(*) c FROM data_member "
                        "WHERE is_file_object=1")[0]["c"]
        out.append(f"  extraction: full download - {objects:,} physical "
                   f"file(s) confirmed by their data members")
    else:
        out.append("  extraction: source only - no data members. Data files "
                   "are known from the CL that\n  creates and opens them "
                   "(resolve_files marks them DATA-BUILT), not from objects "
                   "on disk.")

    # Per library, before anything else. A library holding none of the index
    # is the one thing a total can never say, and it is the difference between
    # "no impact" and "never read".
    built = _rows("SELECT value FROM build_info WHERE key='built_at'")
    if built:
        out.append(f"  index built: {built[0]['value'][:19].replace('T', ' ')} UTC"
                   f" - quote this date in any report header")
    libraries = _library_coverage()
    if libraries:
        blind = [r for r in libraries
                 if r[4] == 0 or r[5].startswith("INDEXED BUT EMPTY")]
        with_facts = _members_with_facts()
        out.append("\n  BY LIBRARY - what is on disk against what is indexed:")
        out.append(_table(
            ["library", "files", "recognised", "unmapped", "indexed",
             "with facts", "status"],
            [(name, f"{files:,}", f"{rec:,}", f"{unm:,}", f"{held:,}",
              (f"{with_facts.get(name.upper(), 0):,} "
               f"({100 * with_facts.get(name.upper(), 0) // held}%)"
               if held else "-"), status)
             for name, files, rec, unm, held, status in libraries], 40))
        if blind:
            out.append(f"\n  ** {len(blind)} LIBRAR"
                       f"{'Y' if len(blind) == 1 else 'IES'} CONTRIBUTE "
                       f"NOTHING TO THIS INDEX **")
            for name, files, rec, unm, held, status in blind:
                out.append(f"     {name:<16}{status}")
            out.append("     A question whose answer lives in one of these "
                       "returns nothing, and nothing")
            out.append("     about that result says so. Treat every count as "
                       "a floor until they are")
            out.append("     extracted, and name them in any report.")

    out.append("\n  confidence of each fact type:")
    rows = []
    for label, table in (("call edges", "call_edge"),
                         ("file usage", "file_usage"),
                         ("field usage (distinct)", "field_usage"),
                         ("overrides", "override"),
                         ("copy edges", "copy_edge")):
        counts = {r["confidence"]: r["c"] for r in _rows(
            f"SELECT confidence, COUNT(*) c FROM {table} GROUP BY confidence")}
        n = sum(counts.values())
        if n:
            rows.append((label, f"{n:,}", f"{counts.get('exact', 0):,}",
                         f"{counts.get('inferred', 0):,}",
                         f"{counts.get('unresolved', 0):,}"))
    out.append(_table(["fact", "total", "exact", "inferred", "unresolved"], rows))

    unresolved = _rows("SELECT kind, COUNT(*) c FROM unresolved "
                       "GROUP BY kind ORDER BY c DESC LIMIT 10")
    if unresolved:
        out.append("\n  things that could not be worked out:")
        out.append(_table(["kind", "count"],
                          [(u["kind"], f"{u['c']:,}") for u in unresolved]))

    # Files skipped at discovery. Reported here because an agent asking "what
    # can you not see" deserves to know about whole member types that never
    # entered the index, not just facts that failed to resolve.
    info = {r["key"]: r["value"] for r in
            _rows("SELECT key, value FROM build_info")}
    try:
        unrecognised = json.loads(info.get("unrecognised") or "{}")
    except (json.JSONDecodeError, TypeError):
        unrecognised = {}
    if unrecognised:
        # Only the extensions that might be members are worth an agent's
        # attention. A skipped .yaml is not a gap in the knowledge base, and
        # listing it beside a real unmapped member type makes the list noise.
        maybe = {e: n for e, n in unrecognised.items()
                 if e.upper() not in config.NON_SOURCE_EXTENSIONS}
        if maybe:
            out.append("\n  ! %s file(s) were SKIPPED at discovery because "
                       "their extension is not mapped."
                       % format(sum(maybe.values()), ","))
            out.append("  They are in no table and no tool can see them:")
            out.append(_table(["extension", "files"],
                              [(ext, format(n, ",")) for ext, n in
                               sorted(maybe.items(),
                                      key=lambda kv: (-kv[1], kv[0]))[:10]]))
            out.append("  If a ticket concerns one of these, say so plainly - "
                       "the member is not indexed.")

    # Known shallow spots. Stated explicitly because the confidence table
    # above cannot express "we parsed this member type less deeply".
    shallow = _rows(
        "SELECT o.member_type, COUNT(*) c FROM object o "
        "WHERE o.member_type IN ('RPGLE','SQLRPGLE','RPG','SQL','MSGF') "
        "GROUP BY o.member_type ORDER BY c DESC")
    if shallow:
        out.append("\n  These member types are parsed less deeply than COBOL "
                   "and CL:")
        for row in shallow:
            out.append(f"      {row['member_type']:<10} {row['c']:,} members")
        out.append("    RPG yields files, calls, copybooks, procedures, "
                   "embedded SQL and")
        out.append("    field DEFINITIONS - but no field USAGE, so "
                   "where_is_field_used")
        out.append("    cannot see which RPG statements read or write a "
                   "field. SQL access")
        out.append("    is bridged into file usage as 'inferred' by matching "
                   "table names.")
        out.append("    For these types treat a nil result as unknown rather "
                   "than none.")

    # ---- the written pages, and how much of the tree they cover ---------
    #
    # Three tool descriptions send an agent here for this number, so it has
    # to be here.  It also has to be COUNTED rather than quoted: seven places
    # in this package had "112 pages" written into their prose, which was an
    # estimate frozen as though it were a measurement, and the real figure is
    # a fraction of it.  route_ticket, find_similar and read_summary are all
    # only as good as this coverage, and an agent cannot discount an answer
    # it cannot size.
    pages = _rows("SELECT level, scope, COUNT(*) c FROM summary "
                  "WHERE text <> '' GROUP BY level, scope ORDER BY level")
    menus = _rows("SELECT COUNT(DISTINCT menu) c FROM menu_option")
    libraries = _rows("SELECT COUNT(DISTINCT library) c FROM object "
                      "WHERE library <> ''")
    written = sum(int(p["c"]) for p in pages)
    out.append("")
    if not written:
        out.append("  ! NO written pages exist, so route_ticket and "
                   "find_similar have nothing")
        out.append("    to match against and read_summary answers for no "
                   "member. The facts are")
        out.append("    unaffected - trace_ticket, what_uses and "
                   "search_source read the source.")
    else:
        have_menu = sum(int(p["c"]) for p in pages if p["scope"] == "menu")
        all_menus = int(menus[0]["c"]) if menus else 0
        all_libs = int(libraries[0]["c"]) if libraries else 0
        out.append(f"  WRITTEN PAGES - {written} in total. These are model "
                   f"prose, not facts:")
        out.append(_table(
            ["level", "scope", "pages"],
            [(str(p["level"]), p["scope"], f"{p['c']:,}") for p in pages]))
        gaps = []
        if all_menus and have_menu < all_menus:
            gaps.append(f"{have_menu} of {all_menus} menus")
        if not any(p["scope"] == "library" for p in pages) and all_libs:
            gaps.append(f"0 of {all_libs} libraries")
        if not any(p["scope"] == "family" for p in pages):
            gaps.append("no functional-area pages")
        if gaps:
            out.append("    Coverage is partial: " + ", ".join(gaps) + ".")
            out.append("    route_ticket can only name an area that has a "
                       "page, so treat a nil")
            out.append("    result there as unknown rather than none, and "
                       "fall back to trace_ticket.")

    out.append("""
  PERMANENT BLIND SPOTS - these are not in the source at all, so no amount
  of parsing will find them. Say so rather than guessing:

    - job schedule entries: what runs a program overnight
    - which program a *CMD member runs: the CPP is a CRTCMD parameter held
      on the command object, so all 456 commands here have no outbound edge.
      A menu option pointing at a command is a dead end in the source.
    - data queue and trigger attachments: made by CL at run time
    - whether the compiled object matches this source
    - user authority and adopted authority
    - anything about the data itself""")
    return "\n".join(out)


# ===========================================================================
# 12. trace_ticket - the entry point
# ===========================================================================

#: Questions the source cannot answer, however well it is indexed.  Each is
#: a phrase that means the ticket is asking about something that lives
#: outside the source tree entirely.
BLIND_SPOTS: List[Tuple[Tuple[str, ...], str]] = [
    (("job schedule", "scheduled", "schedule entry", "what time", "runs at",
      "cron", "job queue entry", "autostart"),
     "WHEN something runs is held in job schedule entries on the system, not "
     "in any source member. The source can tell you what the cycle does and "
     "in what order, but not when it is triggered."),
    (("compiled from", "in production", "object match", "patched the object",
      "recompiled", "what is actually running", "same as production"),
     "Whether the compiled object matches this source cannot be determined "
     "from source alone. It needs DSPOBJD / DSPPGM against the live library."),
    (("how many", "how much", "total for", "last month", "figures for",
      "volume of", "value of"),
     "This is a question about the DATA, not the code. The source describes "
     "the files; it does not contain their contents."),
    (("who authorised", "who approved", "approval", "sign off", "signed off",
      "change request record", "who requested"),
     "Change approval records live in the change management system, not in "
     "the source."),
    (("which users", "who is authorised", "who can run", "user profile",
      "permissions", "audit have asked", "authority to"),
     "User authority is data in the security file plus IBM i object "
     "authority. The source shows where authority is CHECKED, not who has "
     "it."),
]


def _blind_spot(text: str) -> Optional[str]:
    lowered = text.lower()
    for phrases, explanation in BLIND_SPOTS:
        if any(phrase in lowered for phrase in phrases):
            return explanation
    return None


#: Inflections a phrase's last word may carry in a ticket.  Nobody writes "we
#: have a back order problem"; they write "back orders are not being released",
#: and a plain \b after the phrase will not match that because there is no word
#: boundary between the 'r' of order and the 's' of orders.  This is the whole
#: reason the first attempt at phrase matching still missed ORDBOR.
_PHRASE_TAIL = r"(?:s|es|ed|ing)?\b"


def _phrase_re(phrase: str) -> str:
    """A regex matching `phrase` in a ticket, tolerating how people write.

    Flexible whitespace, because a ticket may hyphenate or wrap, and an
    optional inflection on the final word.
    """
    parts = [re.escape(word) for word in phrase.split()]
    return r"\b" + r"[\s\-]+".join(parts) + _PHRASE_TAIL


_INDEG: Optional[Dict[str, int]] = None


def _indegree_map() -> Dict[str, int]:
    """name -> how many distinct members depend on it, cached per process.

    Used to break ranking ties.  Shared with the index rather than recomputed,
    so the same member is not "important" here and ordinary there.
    """
    global _INDEG
    if _INDEG is None:
        from ..index.degree import indegree
        _INDEG = indegree(db().cursor())
    return _INDEG


def _search_text_sqlite(terms: Sequence[str],
                        limit: int = 25) -> List[Dict[str, Any]]:
    """FTS5 over the English in the codebase.  Same shape as the Cypher version.

    Terms are quoted.  Unquoted, a word FTS5 treats as syntax - `NEAR`, `OR`, a
    bare hyphen - makes the whole MATCH a syntax error, and the caller can only
    swallow it, which turns a retrieval pass into a silent no-op.
    """
    if not terms:
        return []

    if store_backend() == "postgres":
        # The one construct with no portable spelling.  FTS5's `MATCH` and its
        # negative `rank` become `@@` and a positive `ts_rank_cd`, so the ORDER
        # BY flips as well.
        #
        # The scores are not comparable between the two stores and do not need
        # to be: `_rank_candidates` consumes rank POSITION, not the value. On
        # the replica the two agree on the candidate SET exactly - 414 members
        # for CREDIT OR HOLD, from both - and differ only in ordering within
        # it, 8 of the top 10 shared.
        query = " | ".join(t.lower() for t in terms)
        try:
            rows = _rows(
                "SELECT name, kind, -ts_rank_cd(tsv, q) AS rank "
                "  FROM search, to_tsquery('english', ?) q "
                " WHERE tsv @@ q ORDER BY rank, name LIMIT ?",
                query, int(limit))
        except Exception:                               # noqa: BLE001
            # to_tsquery raises on a malformed expression exactly as FTS5's
            # MATCH does, and for the same reason: the terms come from a
            # ticket. Swallowed the same way, so a bad term costs this pass
            # and not the whole answer.
            return []
        return [{"name": r["name"], "kind": r["kind"], "rank": r["rank"]}
                for r in rows]

    query = " OR ".join(f'"{t}"' for t in terms)
    try:
        rows = _rows("SELECT name, kind, rank FROM search WHERE search MATCH ? "
                     "ORDER BY rank LIMIT ?", query, int(limit))
    except sqlite3.OperationalError:
        return []
    return [{"name": r["name"], "kind": r["kind"], "rank": r["rank"]}
            for r in rows]


def _rank_candidates(text: str) -> Tuple[List[Tuple[str, float]],
                                         Dict[str, List[str]], str,
                                         List[str]]:
    """The ranking behind trace_ticket.

    Returns (ranked, why, blind_spot, named). The annotation claimed three
    while one branch returned four and the other three - which is how the
    mismatch survived: nothing checked it and the common path worked.

    Split out from the formatting so that the benchmark probe can measure the
    ranking directly instead of parsing a table back out of a string, and so
    the tie diagnostics have something to report on.
    """
    from ..lexicon import terms

    # Some tickets cannot be answered from source at all.  Say so first and
    # plainly - an agent handed a confident list of candidates for "which job
    # schedule entry runs this" will answer the question instead of declining,
    # and a knowledge base that causes that is worse than no knowledge base.
    blind = _blind_spot(text)

    lowered = text.lower()
    words = set(re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", lowered))

    # Is this a "what would it take to change X" question?  Those are the
    # only ones where the copybooks that would need recompiling belong in the
    # answer.
    impact_question = bool(words & {
        "change", "changing", "widen", "widening", "extend", "remove",
        "removing", "delete", "deleting", "add", "adding", "breaks", "break",
        "impact", "affected", "recompile", "rename", "drop", "unused"})
    scores: Dict[str, float] = defaultdict(float)
    why: Dict[str, List[str]] = defaultdict(list)

    # Passes accumulate, and that was measured rather than assumed.
    #
    # ITMW02 reaching 33.7 on a back-order ticket - burying ORDBOR, which the
    # hand-written 'back order' term names outright - looks like unbounded
    # accumulation of correlated evidence, because the seeded lexicon is
    # extracted from field descriptions and a work file's layout is often a
    # superset of a master file's.
    #
    # Both fixes for that were tried against the probe and both lost:
    #
    #   cap every pass at its best match   50 -> 53 gold, but ties 9 -> 14
    #                                      tickets and four golds fell out
    #   cap only the seeded pass           50 gold, worse than doing nothing
    #
    # So the accumulation is carrying real signal and is left alone. The bucket
    # structure stays because it makes each pass's contribution inspectable,
    # which is what allowed those two attempts to be measured at all.
    pass_scores: Dict[str, Dict[str, float]] = defaultdict(dict)

    # The strongest KIND of evidence anyone has offered for a member, which
    # outranks the amount of evidence.
    #
    #   2  the ticket named it, or a hand-written term names it outright
    #   1  a hand-written term reaches it through one of its fields
    #   0  everything else - seeded terms, description text, messages, the graph
    #
    # Weights alone could not do this and the attempt is recorded in the code:
    # "Weighting them equally let 'rates' outrank 'VAT'". Raising the manual
    # weight to 7.0 was that fix, and it is still not enough, because eight
    # coincidences at 1.5 beat one certainty at 7.0. On a VAT ticket TAXRAT
    # scored 7.0 from the hand-written 'vat' term and finished 18th, behind
    # members assembled from field-name accidents like 'before' -> OHDGRS.
    #
    # Precedence says the quiet, certain answer is not outvoted by a crowd of
    # guesses. It is the same ordering the rest of the schema uses for
    # confidence: exact, then inferred, then unresolved.
    grade: Dict[str, int] = defaultdict(int)

    def bump(pass_name: str, member: str, value: float, reason: str,
             tier: int = 0) -> None:
        bucket = pass_scores[pass_name]
        bucket[member] = bucket.get(member, 0.0) + value
        why[member].append(reason)
        if tier > grade[member]:
            grade[member] = tier

    def bump_term(pass_name: str, entry: Dict, label: str,
                  weight: float, curated: bool = False) -> None:
        """Score one lexicon term against every member it names - once each.

        A term that names a member names it once, however many ways it does so.
        'stock' maps to seven fields - IWHMIN, IWHMAX, IWHSAF, IWHCNT, IWHCQT,
        IWHFRZ, IWHVAL - and all seven belong to ITMW02, so summing them gave
        one ticket word a sevenfold vote and pushed a warehouse work file to
        33.7 on a back-order ticket, twenty-five points clear of ORDBOR, which
        the hand-written 'back order' term names outright.
        """
        best: Dict[str, Tuple[float, str, int]] = {}
        breadth: Dict[str, int] = defaultdict(int)

        def offer(member: str, value: float, reason: str, tier: int) -> None:
            breadth[member] += 1
            if value > best.get(member, (0.0, "", 0))[0]:
                best[member] = (value, reason, tier)

        # Named outright by the term is stronger than reached through one of
        # the member's fields, and only the targets a person actually wrote
        # count as curated.  `entry` merges the hand-written and auto-seeded
        # halves, so asking whether the entry is manual is not the same as
        # asking whether this target is - see terms.lookup.
        hand = entry.get("curated") or {}
        hand_named = set(hand.get("files", []) + hand.get("programs", []))
        hand_fields = set(hand.get("fields", []))

        for target in entry.get("files", []) + entry.get("programs", []):
            if _rows("SELECT 1 FROM object WHERE name=? LIMIT 1", target):
                offer(target, weight,
                      f"'{label}'", 2 if target in hand_named else 0)
        for field in entry.get("fields", []):
            tier = 1 if field in hand_fields else 0
            for r in _rows("SELECT DISTINCT owner FROM field WHERE name=? "
                           "AND owner_kind='file' LIMIT 6", field):
                offer(r["owner"], weight * 0.6, f"'{label}' -> {field}", tier)

        # One vote, whatever the breadth.  Four variants were measured against
        # kb.eval.probe over all 80 gold names:
        #
        #   linear sum (as it was)   52 gold, 21 solved, vague 74%, 9 ties
        #   one term one vote        55 gold, 23 solved, vague 68%, 13 ties
        #   sqrt(breadth) damping    52 gold, 22 solved, vague 71%, 8 ties
        #
        # One vote wins on aggregate and is the only one of the three that can
        # be stated as a rule rather than a coefficient, so it is what ships.
        # It does cost three gold answers in the vague band that were being
        # found on accumulated field matches alone.
        #
        # These margins are 3 in 80 on forty hand-written tickets. Do not tune
        # this further against them - that is fitting the benchmark, not the
        # codebase. `breadth` is left computed and unused on purpose: it is the
        # number to reach for if a larger ticket set ever justifies revisiting.
        for member, (value, reason, tier) in best.items():
            bump(pass_name, member, value, reason, tier)

    # 1. identifiers named outright
    # 1. identifiers named outright
    #
    # Matched against the ticket as written, not against an uppercased copy.
    # Uppercasing first means every ordinary word in the ticket is offered to
    # the inventory, and any that happens to be a member name is then treated
    # as a deliberate reference: "point order entry at the archive library"
    # scored ARCHIVE 10.0 and top grade, level with ORD100, which that ticket
    # really does name - and that pushed the CL driver holding the answer out
    # of the list entirely.
    #
    # A member reference in a ticket is written in capitals. A lowercase word
    # is only accepted when it carries a digit, because no English word does
    # and 'ord100' is unambiguous where 'archive' is not.
    named: List[str] = []
    unknown: List[str] = []
    #: field named in the ticket -> the files, copybooks and tables declaring it
    named_fields: Dict[str, List[str]] = {}
    for token in _identifier_tokens(text):
        if _rows("SELECT 1 FROM object WHERE name=? LIMIT 1", token):
            scores[token] += 10.0
            why[token].append("named in the ticket")
            grade[token] = 2
            named.append(token)
            continue
        # A field the ticket names outright. Its declarers are weighed after
        # every other pass - see the named-field step below.
        owners = _rows("SELECT DISTINCT owner, owner_kind, record FROM field "
                       "WHERE name=? LIMIT 12", token)
        declaring = []
        for r in owners:
            if r["owner_kind"] in ("file", "copybook"):
                declaring.append(r["owner"])
            elif r["record"] and _sql_tables("name=?", r["record"]):
                declaring.append(r["record"])      # an SQL column: its table
        if declaring:
            named_fields[token] = list(dict.fromkeys(declaring))
            named.append(token)
        else:
            # Written like a member name and absent from the index. Kept
            # because at a nil this is the whole answer: the ticket named
            # something and this extraction has never heard of it, which is a
            # coverage finding rather than a failed search.
            unknown.append(token)

    # 2a. multi-word business terms, before single words get a look in.
    #
    # Ten of the sixty hand-written terms are phrases - 'back order',
    # 'free stock', 'on stop' - and every one of them was unreachable, because
    # the tokeniser below only ever produced single words.  'back order' maps to
    # ORDBOR, which is the gold answer for the one benchmark ticket that used
    # to score zero.
    #
    # A phrase is stronger evidence than either of its words, so it takes the
    # manual weight and its constituent words are then skipped rather than
    # scored again on their own.
    spoken_for: Set[str] = set()
    for phrase in terms.phrases():
        if not re.search(_phrase_re(phrase), lowered):
            continue
        entry = terms.lookup(phrase)
        if not entry:
            continue
        spoken_for.update(phrase.split())
        bump_term("phrase", entry, phrase, 7.0, curated=True)

    # 2b. business words mapped through the lexicon
    for word in sorted(words):
        if word in spoken_for:
            continue
        entry = terms.lookup(word)
        if not entry:
            continue
        # A hand-written mapping is somebody's knowledge of the system.  A
        # seeded one is a word that happened to appear in a field description.
        # Weighting them equally let 'rates' outrank 'VAT'.
        manual = entry.get("source") == "manual"
        bump_term("manual" if manual else "seeded", entry, word,
                  7.0 if manual else 1.5, curated=manual)

    # 3. free text over the English the codebase already contains
    #
    # The relevance is used, not discarded.  This pass gave all 25 hits a flat
    # 2.0, which made it the largest single producer of ties: 11 of the 15
    # nameless benchmark tickets had a gold answer sitting in a group of equal
    # scores, some of them 27 members wide, and which one surfaced first was
    # decided by dictionary insertion order.
    #
    # Decayed by position rather than by the raw bm25 value.  The pass keeps
    # roughly the influence it had - first hit 2.0, last around 0.8 - but its
    # own ordering now survives into the score.
    # Because only POSITION is used, this pass survives the move to Lucene
    # unchanged.  FTS5's `rank` is a negative bm25 and Lucene's `score` is a
    # positive one; they are not on the same scale and never could be, so a
    # decay by rank position is the only formulation that means the same thing
    # in both stores.  That was luck rather than foresight, but it is why the
    # migration is a routing change here and not a rewrite.
    # Its own name: `terms` in this function is the lexicon module above.
    search_terms = sorted(w for w in words if len(w) > 3)[:12]
    if search_terms:
        try:
            # Direct, not through the graph backend: search_text is not graph-
            # mapped, so that routing had exactly one destination - and
            # importing it here only added a way for this step to fail.
            hits = _search_text_sqlite(search_terms, 25)
            span = max(1, len(hits) - 1)
            for position, r in enumerate(hits):
                bump("fts", r["name"], 2.0 - 1.2 * (position / span),
                     "description text")
        except sqlite3.OperationalError:
            pass

    # 4. message text - written in the language the business uses
    #
    # sorted(), not list(): iterating a set meant *which* twenty words got
    # matched varied between runs, so the same ticket could rank differently
    # twice in a row.
    for word in sorted(words)[:20]:
        if len(word) < 5:
            continue
        for r in _rows("SELECT msgid, text FROM message WHERE text LIKE ? "
                       "LIMIT 4", f"%{word}%"):
            for u in _rows("SELECT DISTINCT program FROM message_usage "
                           "WHERE msgid=? LIMIT 8", r["msgid"]):
                bump("message", u["program"], 1.5,
                     f"message {r['msgid']}")

    for bucket in pass_scores.values():
        for member, value in bucket.items():
            scores[member] += value

    # A field the ticket names re-ranks the members other evidence found. Its
    # declarers are brought in as new candidates only when none was found and
    # nothing better than description text ranks - a word match must never
    # outrank a field the ticket spells out. Adding every declarer regardless
    # was measured with kb.eval.probe and lost a gold answer: a field in
    # thirty work files crowds out the copybook the ticket is really about.
    strong = any(grade[m] >= 1 for m in scores)
    for token, declaring in named_fields.items():
        found = [m for m in declaring if m in scores]
        for member in (found or ([] if strong else declaring[:6])):
            scores[member] += NAMED_FIELD_WEIGHT
            why[member].append(f"declares {token}, named in the ticket")
            grade[member] = max(grade[member], 1)

    if not scores:
        # FOUR values, like the other exit. This returned three, so
        # trace_ticket - the tool the skill calls START HERE - raised
        # "ValueError: not enough values to unpack (expected 4, got 3)" on
        # every ticket that matched nothing. It never showed up in testing
        # because a ticket that matches something takes the other path, and a
        # ticket matching nothing is exactly when the tool is needed most.
        #
        # `named` matters here rather than being padding: the ticket may name
        # identifiers that simply are not in this extraction, and saying which
        # is the most useful thing left to say.
        return [], why, blind, unknown

    # 5. one hop of graph expansion from the best matches
    top = sorted(scores, key=lambda n: (-scores[n], n))[:5]
    for name in top:
        for r in _rows(f"SELECT DISTINCT program FROM file_usage WHERE file=? "
                       f"AND {_CAN_CHANGE} LIMIT 8", name):
            if r["program"] not in scores:
                scores[r["program"]] += 1.0
                why[r["program"]].append(f"writes {name}")

        # Copybooks that describe this file.  There is no direct link between
        # CUSMAS and CUSREC in the source - but a program that does
        # COPY DDS-ALL-FORMATS OF CUSMAS and also COPYs CUSREC is telling us
        # they are the same record.
        #
        # Only for questions that are actually about changing a layout.
        # Adding them unconditionally displaced correct candidates and cost
        # more than it gained - measured, not guessed.
        if not impact_question:
            continue
        # Ranked by lift, not by raw co-occurrence.
        #
        # 1,109 programs COPY DDS-ALL-FORMATS OF CUSMAS, and taking the six
        # most frequent copybooks among them returns DBFLAGS and the other
        # members that everything copies. CUSREC - copied by fourteen programs,
        # almost all of them CUSMAS programs - never made the list, so the one
        # copybook that actually describes the record was the one the query
        # could not find. Dividing by how often a copybook is copied at all
        # asks the right question: which copybook is unusually specific to this
        # file, rather than which is merely popular.
        for r in _rows(
                # Wrapped in a subquery so `n` and `total` can be named.
                # SQLite lets a select-list alias be used in HAVING and inside
                # an ORDER BY expression; standard SQL does not, because HAVING
                # is evaluated before the select list exists. Both the
                # `HAVING n >= 2` and the `CAST(n AS REAL) / total` ordering
                # were relying on that, and PostgreSQL rejects each outright.
                # A derived table is portable and reads better than repeating
                # a correlated subquery three times.
                "SELECT copybook, n, total FROM ("
                "  SELECT c2.copybook AS copybook, COUNT(*) AS n, "
                "         (SELECT COUNT(*) FROM copy_edge t "
                "           WHERE t.copybook = c2.copybook) AS total "
                "    FROM copy_edge c1 "
                "    JOIN copy_edge c2 ON c2.program = c1.program "
                "    JOIN object o ON o.name = c2.copybook "
                "                  AND o.member_type='CPY' "
                "   WHERE c1.copybook = ? AND c1.kind IN "
                "('record_layout','device_layout','generated_layout',"
                "'dds_all_formats') "
                "   GROUP BY c2.copybook "
                ") x "
                " WHERE n >= 2 "
                " ORDER BY (CAST(n AS REAL) / total) DESC, n DESC, copybook "
                " LIMIT 6",
                name):
            scores[r["copybook"]] += 2.0
            why[r["copybook"]].append(f"copied alongside {name}")

    # 6. what a named member is built out of.
    #
    # A ticket that says "ORD100 shows the wrong total" is about ORD100 and
    # about the screen it writes, the CL that sets its overrides and the
    # copybook holding the record it reads - and the gold answers say so. Those
    # are all one hop away in facts the index already holds, and none of them
    # was reachable: the only expansion was file -> programs that write the
    # file, which never yields a display file, a driver or a copybook.
    #
    # Restricted to members the ticket named outright. Expanding from a member
    # that merely scored well pulls in a second neighbourhood of guesses.
    for name in named:
        for sql, weight, label in (
                ("SELECT DISTINCT device_file AS n FROM format_usage "
                 "WHERE program=? AND device_file<>'' LIMIT 4",
                 1.5, "screen or report it writes"),
                ("SELECT DISTINCT copybook AS n FROM copy_edge "
                 "WHERE program=? LIMIT 6",
                 1.5, "copybook it copies"),
                ("SELECT DISTINCT caller AS n FROM call_edge "
                 "WHERE callee=? LIMIT 4",
                 1.5, "calls it"),
                ("SELECT DISTINCT callee AS n FROM call_edge "
                 "WHERE caller=? AND callee<>'' LIMIT 4",
                 1.5, "it calls")):
            for r in _rows(sql, name):
                # Written straight into `scores`, not through bump(): the
                # per-pass buckets were merged before pass 5, so a bump() here
                # would be counted by nothing.
                neighbour = r["n"]
                if not neighbour or neighbour == name:
                    continue
                scores[neighbour] += weight
                why[neighbour].append(f"{label} {name}")
                # Top grade, alongside "the ticket named it". This is not a
                # text match that might be a coincidence - the code says
                # ORD100C calls ORD100, exactly, with a line number. It is the
                # strongest evidence in this whole function, and leaving it a
                # grade below curated text left ORD100C at #14 on a ticket
                # asking which file ORD100 opens.
                grade[neighbour] = max(grade[neighbour], 2)

    # A logical file is the same data as its physical, so seven logicals over
    # one file are one candidate, not seven.  Fold them up before ranking or
    # they crowd out everything else.
    folded: Dict[str, float] = defaultdict(float)
    folded_why: Dict[str, List[str]] = defaultdict(list)
    # sorted(), not .items(): this loop's ARITHMETIC depends on iteration
    # order, not just the order of the printed `via` list.
    #
    #     folded[root] = max(folded[root], score) + 0.5
    #
    # Fold A(1.0) then B(2.0) and the total is 2.5; fold B then A and it is
    # 3.0. Same facts, different score, different ranking - and `scores` is a
    # dict built from query results, so its order came from whichever store
    # answered. That is how the same ticket produced "via ORDDTLHL5" on SQLite
    # and "via ORDDTLHL1" on PostgreSQL: not a display difference, a scoring
    # one that happened to show up in the display.
    #
    # Sorting by name makes it reproducible. It does NOT make the accumulation
    # principled - a file with seven logicals collects +3.5 and it is not clear
    # that was intended - but changing that is a scoring decision that needs
    # measuring, and this is a determinism fix.
    for name, score in sorted(scores.items()):
        parent = _rows("SELECT based_on FROM object WHERE name=? "
                       "AND based_on<>'' LIMIT 1", name)
        root = parent[0]["based_on"] if parent else name
        folded[root] = max(folded[root], score) + (0.5 if parent else 0.0)
        folded_why[root].extend(why[name])
        if parent:
            folded_why[root].append(f"via {name}")
    scores, why = folded, folded_why

    # Equal scores are broken by how many members depend on the candidate, and
    # only then by name.
    #
    # Without this the winner of a tie was whichever name a defaultdict saw
    # first, which is not a judgement about anything. In-degree is: a file 38
    # programs open is a likelier subject for a ticket than a program one thing
    # calls, and in the 14-way tie that used to bury ITMWHS it is the number
    # that lifts it to the top. It is the same quantity the index uses to
    # decide the `service` tier, and it comes from the data rather than from a
    # naming convention.
    #
    # The final `name` key is not cosmetic - it is what makes two runs of the
    # same ticket produce the same answer.
    # Folding moved the score onto the physical file; carry the grade with it,
    # or a logical named by a hand-written term would lose its standing on the
    # way up to its parent.
    for name in list(scores):
        parent = _rows("SELECT based_on FROM object WHERE name=? "
                       "AND based_on<>'' LIMIT 1", name)
        if parent and grade[name] > grade[parent[0]["based_on"]]:
            grade[parent[0]["based_on"]] = grade[name]

    degree = _indegree_map()
    ranked = sorted(scores.items(),
                    key=lambda kv: (-grade[kv[0]], -kv[1],
                                    -degree.get(kv[0], 0), kv[0]))
    # `named` rides along so trace_ticket can decide whether the
    # semantic half of routing is worth 2.3 seconds.  It is computed
    # in pass 1 and was thrown away.
    return ranked, why, blind, named


#: Score for a file declaring a field the ticket names outright. Module level
#: so kb.eval.probe can measure the ranking with it switched off.
NAMED_FIELD_WEIGHT = 6.0


def _identifier_tokens(text: str) -> List[str]:
    """Words in a ticket written the way a member or field name is written.

    Capitals as written, or a lowercase word carrying a digit - no English word
    does, and 'ord100' is unambiguous where 'archive' is not.
    """
    tokens = set(re.findall(r"\b[A-Z][A-Z0-9_]{2,9}\b", text))
    tokens.update(tok.upper() for tok in
                  re.findall(r"\b[A-Za-z][A-Za-z0-9]{2,9}\b", text)
                  if any(ch.isdigit() for ch in tok))
    return sorted(tokens)


def _unknown_identifiers(text: str) -> List[str]:
    """Identifier-shaped words in the ticket that no fact in the index names.

    Checked against every kind of name the index holds - members, fields, SQL
    tables, data files, override targets - so a word listed here matched
    nothing at all, which is the first thing a reader of the ranking must know.
    """
    out = []
    for token in _identifier_tokens(text):
        found = (_rows("SELECT 1 FROM object WHERE name=? LIMIT 1", token)
                 or _rows("SELECT 1 FROM field WHERE name=? LIMIT 1", token)
                 or _sql_tables("name=? OR sql_name=?", token, token)
                 or _rows("SELECT 1 FROM override WHERE target_file=? "
                          "OR logical_name=? LIMIT 1", token, token)
                 or _rows("SELECT 1 FROM file_provenance WHERE target=? LIMIT 1",
                          token))
        if not found:
            try:
                found = _rows("SELECT 1 FROM data_member WHERE name=? LIMIT 1",
                              token)
            except sqlite3.OperationalError:
                found = []
        if not found:
            out.append(token)
    return out


def _tie_note(ranked: Sequence[Tuple[str, float]]) -> str:
    """How many candidates the evidence genuinely cannot separate.

    Reported rather than hidden.  The rule elsewhere in this schema is that
    where two facts disagree you record both and say so; presenting one
    arbitrary winner out of twenty-seven equals is the retrieval form of the
    same lie, and it is what makes an agent confidently open the wrong member.
    """
    if not ranked:
        return ""
    top = ranked[0][1]
    tied = [name for name, score in ranked if abs(score - top) < 0.05]
    if len(tied) < 2:
        return ""
    return (f"\n  {len(tied)} candidates score the same and the evidence does "
            f"not separate them:\n    " + ", ".join(tied[:12])
            + ("..." if len(tied) > 12 else "")
            + "\n  They are ordered by how many members depend on each. Read "
              "more than the first.")


def trace_ticket(text: str, limit: int = 12) -> str:
    """Business language in, ranked candidate members out."""
    ranked, why, blind, named = _rank_candidates(text)

    if not ranked:
        # At this branch `named` carries the identifier-shaped tokens that are
        # NOT in the index - see _rank_candidates.
        out = ["Nothing in the index matched this text."]
        if named:
            # The ticket DID name identifiers and none is here. That is a
            # coverage finding, not a search failure, and far more actionable
            # than "try again with better words".
            shown = ", ".join(sorted(dict.fromkeys(named))[:12])
            out.append(f"  It names {shown}, and none of those is in the "
                       f"knowledge base.")
            out.append("  That is NOT 'they do not exist'. Check each with "
                       "find_object - a name can be")
            out.append("  a physical file or a program-side alias - and call "
                       "coverage_report for the")
            out.append("  libraries that contribute nothing to this index.")
        else:
            out.append("  Try naming a file, a field or a program, or "
                       "search_source with a distinctive phrase.")
        out.append("  If the ticket is about scheduling, authority or the "
                   "data itself, the source")
        out.append("  cannot answer it - see coverage_report.")
        return chr(10).join(out)

    rows = []
    for name, score in ranked[:limit]:
        info = _rows("SELECT member_type, lines FROM object WHERE name=? "
                     "LIMIT 1", name)
        rows.append((name,
                     info[0]["member_type"] if info else "?",
                     f"{info[0]['lines']:,}" if info else "",
                     f"{score:.0f}",
                     "; ".join(dict.fromkeys(why[name]))[:44]))

    # Two blocks, scored separately and never mixed.
    #
    # WHERE TO START answers "which part of the application is this" from the
    # written descriptions. CANDIDATE MEMBERS answers "which member" from facts
    # read out of source. Folding the first into the second was considered and
    # rejected: _rank_candidates sorts by grade before score, and grade is
    # absolute, so a cosine hit graded 0 could never outrank a member the ticket
    # named - it would be invisible on every ticket that names an identifier.
    # Keeping them apart also means each can be measured on its own.
    start = ""
    try:
        from . import routing
        # Decline the embedding when the ticket already names members.
        # `_rank_candidates` sorts by grade before score and grade is
        # absolute, so on these tickets the routed block cannot change
        # which member comes first - it is context, and 2.3 seconds is
        # a lot to pay for context. bm25 still answers over the whole
        # corpus in tens of milliseconds and the note says so.
        hits, note = routing.route(db(), text, limit=4,
                                   dense=not named)
        if hits:
            start = routing.render(hits, note) + "\n\n"
    except Exception:
        # Routing is a convenience over generated prose. If it cannot answer,
        # the facts below still can, and an exception here must not take the
        # whole tool down with it.
        start = ""

    # Identifiers first. A name the ticket spells out and the index has
    # never seen is a coverage fact about this ticket, and a ranking printed
    # above it reads as if every name had been found.
    missing = _unknown_identifiers(text)
    lead = ""
    if missing:
        lead = (f"NOT IN THE INDEX: {', '.join(missing[:12])} - named in the "
                f"ticket, 0 matches as a member, field, table or file.\n"
                f"  Check each with search_source(<name>, member_type='CODE') "
                f"and find_object before trusting the list below.\n\n")
    candidates = ("CANDIDATE MEMBERS\n\n"
                  + "Best first. These are leads, not answers - go and read "
                    "the code.\n\n"
                  + _table(["member", "type", "lines", "score", "why"], rows)
                  + _tie_note(ranked[:limit]))
    # When the ticket names identifiers the facts lead and the descriptions
    # follow; when it names none, the area it is about is the better start.
    if named:
        body = lead + candidates + ("\n\n" + start.rstrip() if start else "")
    else:
        body = lead + start + candidates

    if blind:
        return ("THE SOURCE CANNOT ANSWER THIS QUESTION.\n\n  " + blind
                + "\n\n  Do not infer an answer from the members listed "
                  "below. They are only what the words in the ticket happen "
                  "to match, and here they will mislead you. Say plainly "
                  "that the source cannot answer it.\n\n" + body)
    return body
# ===========================================================================
# 14/15/16. Inside one program
# ===========================================================================
#
# Everything above answers questions between members: what calls what, what
# opens which file.  These three answer questions inside one member, which is
# where a ticket is actually fixed.
#
# A COBOL program has no functions.  It has paragraphs, it performs them in a
# shape, and that shape is the only high level structure the language gives
# you.  Recovering it is the difference between handing an agent 4,000 lines
# and handing it a design.

#: How a PERFORM form reads to somebody who has not read the schema.
_FORM_WORDS = {
    "simple": "",
    "thru": "THRU",
    "until": "repeatedly, UNTIL",
    "times": "repeatedly, TIMES",
    "varying": "repeatedly, VARYING",
    "goto": "GO TO - does not come back",
    "inline": "in-line block",
    "inline_until": "in-line loop, UNTIL",
    "inline_times": "in-line loop, TIMES",
    "inline_varying": "in-line loop, VARYING",
    "inline_forever": "in-line loop, FOREVER",
}


def _near_miss(target: str, candidates: Sequence[str]) -> str:
    """The declared paragraph a misspelt PERFORM target probably meant.

    A suggestion, labelled as one, never a fact - Rule 2 applies to guesses
    made by the tools as much as to facts read from source.

    It exists because the two halves of the finding land in different sections
    of the report.  1000-INITIALISE shows up as a target nothing declares and
    1000-INITIALIZE as a paragraph nothing performs; they are one defect, and
    joining them up by eye is work the tool should have done.  A reader who has
    to do it themselves reasonably concludes the tool is broken.
    """
    matches = difflib.get_close_matches(target, list(candidates), n=1,
                                        cutoff=0.86)
    return matches[0] if matches else ""


def _program_member(name: str, member_type: str = "") -> Optional[sqlite3.Row]:
    """The member whose internals we are being asked about.

    A name can exist as more than one member type.  When the caller does not
    say which, prefer the one that has paragraphs - asking about the internals
    of DTQ102 means the program with a procedure division, not the DDS.
    """
    if member_type:
        rows = _rows(
            "SELECT name, member_type, language, lines, path FROM object "
            "WHERE name=? AND member_type=?", name, member_type.upper())
        return rows[0] if rows else None
    rows = _rows(
        "SELECT o.name, o.member_type, o.language, o.lines, o.path, "
        "  (SELECT COUNT(*) FROM paragraph p WHERE p.member=o.name "
        "     AND p.member_type=o.member_type) n "
        "FROM object o WHERE o.name=? ORDER BY n DESC, o.member_type", name)
    return rows[0] if rows else None


#: memo for _paragraph_notes, keyed by (member, member_type).  find_paragraph
#: can return forty rows out of the same member and each one wants the same
#: five queries answered.
_NOTES_CACHE: Dict[Tuple[str, str], Dict[str, List[str]]] = {}


def _paragraph_notes(member: str, member_type: str) -> Dict[str, List[str]]:
    """One short line per paragraph saying what it does.

    Built from the facts attributed to the paragraph - I/O, screens, calls,
    SQL - and never from its name.  A name is the programmer's claim;
    5120-FORMAT-OUTPUT-FIELDS may format nothing at all.
    """
    key = (member, member_type)
    if key in _NOTES_CACHE:
        return _NOTES_CACHE[key]
    if len(_NOTES_CACHE) > 64:
        #: the server is long lived and there are 13,000 members.  A handful
        #: of recent ones is the whole benefit; keeping them all is a leak.
        _NOTES_CACHE.clear()
    notes: Dict[str, List[str]] = defaultdict(list)

    #: A screen or printer file is written through a record format, so the same
    #: statement shows up as both an io_site and a format_usage.  Report the
    #: format - it says which screen - and drop the duplicate.
    device_files = {r["file"] for r in _rows(
        "SELECT file FROM file_usage WHERE program=? AND device<>'database'",
        member)}

    for r in _rows(
            "SELECT paragraph, verb, file, COUNT(*) n FROM io_site "
            "WHERE member=? AND member_type=? AND paragraph<>'' "
            "GROUP BY paragraph, verb, file ORDER BY paragraph, verb, file",
            member, member_type):
        if r["file"] in device_files:
            continue
        times = f" x{r['n']}" if r["n"] > 1 else ""
        notes[r["paragraph"]].append(
            f"{r['verb'].lower()} {r['file']}{times}")

    screens: Dict[Tuple[str, str, str], List[str]] = defaultdict(list)
    for r in _rows(
            "SELECT paragraph, verb, device_file, format_name FROM "
            "format_usage WHERE program=? AND paragraph<>'' "
            "ORDER BY paragraph, line", member):
        screens[(r["paragraph"], r["verb"], r["device_file"] or "")].append(
            r["format_name"])
    for (paragraph, verb, device), formats in screens.items():
        names = ",".join(dict.fromkeys(formats))
        where = f"{device}/" if device else ""
        notes[paragraph].append(f"{verb.lower()} {where}{names}")

    for r in _rows(
            "SELECT paragraph, callee, variable FROM call_edge "
            "WHERE caller=? AND paragraph<>'' "
            # ORDER BY line as well: `paragraph` alone left the order within a
            # paragraph to whatever the store returned. SQLite gave insertion
            # order via the implicit rowid, PostgreSQL gives heap order, and
            # program_structure printed the CALLs of one paragraph in a
            # different sequence on each. `line` is the order they appear in
            # the source, which is the order a reader expects anyway.
            "ORDER BY paragraph, line, callee", member):
        target = r["callee"] or f"({r['variable']} unresolved)"
        notes[r["paragraph"]].append(f"CALL {target}")

    for r in _rows(
            "SELECT paragraph, verb, tables_json FROM sql_usage "
            "WHERE program=? AND paragraph<>'' "
            "ORDER BY paragraph, line, verb", member):
        try:
            tables = ", ".join(json.loads(r["tables_json"] or "[]")[:3])
        except json.JSONDecodeError:
            tables = ""
        notes[r["paragraph"]].append(
            f"SQL {r['verb']}{' ' + tables if tables else ''}")

    _NOTES_CACHE[key] = notes
    return notes


def _perform_edges(member: str, member_type: str) -> List[sqlite3.Row]:
    return _rows(
        "SELECT from_para, to_para, thru_para, form, is_loop, inline, "
        "in_loop, line FROM perform_edge WHERE member=? AND member_type=? "
        "ORDER BY line", member, member_type)


def program_structure(name: str, member_type: str = "",
                      max_rows: int = 220) -> str:
    """The paragraph graph of one program, walked from its entry paragraph."""
    name = name.strip().upper()
    member = _program_member(name, member_type)
    if member is None:
        return (f"No member called {name}. Try find_object for a partial "
                f"name match.")
    kind = member["member_type"]

    paragraphs = _rows(
        "SELECT name, kind, line, end_line FROM paragraph "
        "WHERE member=? AND member_type=? ORDER BY line", name, kind)
    if not paragraphs:
        return (f"{name} ({kind}) has no paragraphs recorded.\n"
                f"  Only COBOL members have a paragraph graph. CL has labels "
                f"but usually just one, RPG has procedures only when it is "
                f"free format, and DDS and copybooks have no procedure "
                f"division at all.\n"
                f"  Use find_object for what is known about {name}.")

    declared = {r["name"] for r in paragraphs}
    entry = paragraphs[0]["name"]
    edges = _perform_edges(name, kind)
    notes = _paragraph_notes(name, kind)

    #: from_para -> the edges leaving it, in source order.  Collapsed on
    #: (target, form, in_loop): a validation paragraph that performs
    #: 8100-SEND-MESSAGE at eleven different failure points is one edge in the
    #: structure and eleven identical rows of noise if it is not folded.
    out_edges: Dict[str, List[Dict]] = defaultdict(list)
    folded: Dict[Tuple[str, str, str, int], Dict] = {}
    inline_loops: Dict[str, int] = defaultdict(int)
    inbound: Dict[str, int] = defaultdict(int)
    dangling: List[sqlite3.Row] = []
    for e in edges:
        if e["inline"]:
            inline_loops[e["from_para"]] += 1
            continue
        if e["to_para"] in declared:
            inbound[e["to_para"]] += 1
        else:
            dangling.append(e)
        key = (e["from_para"], e["to_para"], e["form"], e["in_loop"])
        if key in folded:
            folded[key]["times"] += 1
            continue
        edge = {"to_para": e["to_para"], "form": e["form"],
                "in_loop": e["in_loop"], "line": e["line"], "times": 1}
        folded[key] = edge
        out_edges[e["from_para"]].append(edge)

    line_of = {r["name"]: r["line"] for r in paragraphs}
    span = {r["name"]: (r["line"], r["end_line"]) for r in paragraphs}

    out: List[str] = [
        f"{name}  ({kind}, {member['lines']:,} lines)",
        f"  {len(paragraphs)} paragraphs, "
        f"{sum(1 for e in edges if not e['inline'])} control edges, "
        f"{sum(inline_loops.values())} in-line loops",
        "",
        f"  Control flow from {entry}. Indentation is the PERFORM nesting. The "
        f"left column is",
        f"  where the paragraph is declared, not where it is performed. A "
        f"paragraph already",
        f"  shown is marked (again) and not expanded twice.",
        "",
        "    line  structure",
        "  ------  ---------",
    ]

    seen: Set[str] = set()
    #: Targets already flagged as undeclared. The full sentence is worth
    #: saying once per target; one program performs the same missing
    #: paragraph 44 times, and repeating it there cost 8,400 characters of a
    #: 21,000-character answer - 39% of one tool call spent on one phrase.
    flagged: Set[str] = set()
    rows: List[str] = []
    truncated = False

    def walk(para: str, depth: int, label: str) -> None:
        nonlocal truncated
        if len(rows) >= max_rows:
            truncated = True
            return
        start, end = span.get(para, (0, 0))
        pad = "  " * depth
        note = "; ".join(notes.get(para, []))[:96]
        loops = inline_loops.get(para, 0)
        if loops:
            note = (note + "; " if note else "") + f"{loops} in-line loop(s)"
        again = para in seen
        marker = "  (again)" if again else ""
        rows.append(f"  {start:>6}  {pad}{label}{para}{marker}"
                    + (f"   {note}" if note and not again else ""))
        if again:
            return
        seen.add(para)
        for edge in out_edges.get(para, []):
            target = edge["to_para"]
            bits = []
            if edge["form"] != "simple":
                bits.append(_FORM_WORDS.get(edge["form"], edge["form"]))
            if edge["in_loop"]:
                bits.append("inside a loop")
            if edge["times"] > 1:
                bits.append(f"from {edge['times']} places")
            prefix = f"[{', '.join(b for b in bits if b)}] " if any(bits) else ""
            if target not in declared:
                if target in flagged:
                    rows.append(f"  {edge['line']:>6}  {pad}  {prefix}{target}  *")
                    continue
                flagged.add(target)
                guess = _near_miss(target, sorted(declared))
                hint = (f" - did this mean {guess}, at {line_of[guess]}?"
                        if guess else "")
                rows.append(f"  {edge['line']:>6}  {pad}  {prefix}{target}"
                            f"   NOT DECLARED IN THIS MEMBER{hint}")
                continue
            walk(target, depth + 1, prefix)

    walk(entry, 0, "")
    out.extend(rows)
    if flagged:
        out.append("  * not declared in this member either - the full note is "
                   "on that target's first appearance, and every one is listed "
                   "again at the end.")
    if truncated:
        out.append(f"  ... stopped at {max_rows} rows. Use "
                   f"what_paragraph_does on a paragraph below to go further.")

    #: Everything the entry paragraph cannot reach.  Walked as well as counted,
    #: because a subtree of twelve paragraphs that nothing performs is a
    #: feature of the program worth seeing, and listing only its root hides it.
    unreached = [r for r in paragraphs
                 if r["name"] != entry and not inbound.get(r["name"])]
    if unreached:
        heads = [r for r in unreached if out_edges.get(r["name"])]
        leaves = [r for r in unreached if not out_edges.get(r["name"])]
        out.append("")
        out.append(f"  {len(unreached)} paragraph(s) nothing performs. They "
                   f"cannot run unless control")
        out.append("  falls through from the paragraph above, which in this "
                   "style of code it does not.")
        out.append("  Deliberate dead code is common in a system this old - "
                   "confirm before removing.")
        if heads:
            out.append("")
            out.append("  Unreachable, and performs others:")
            rows = []
            for head in heads:
                seen.discard(head["name"])
                walk(head["name"], 1, "")
            out.extend(rows)
        if leaves:
            out.append("")
            out.append("  Unreachable leaves:")
            out.append(_table(
                ["paragraph", "lines", "does"],
                [(r["name"], f"{r['line']}-{r['end_line']}",
                  "; ".join(notes.get(r["name"], []))[:46]) for r in leaves],
                40))

    if dangling:
        #: A target nothing declares and a paragraph nothing performs, whose
        #: names differ by a character, are one defect reported twice.  Say so
        #: here rather than leaving the reader to spot it across two sections.
        unreached_names = [r["name"] for r in paragraphs
                           if r["name"] != entry and not inbound.get(r["name"])]
        paired, plain = [], []
        for target, source, line in sorted(
                {(e["to_para"], e["from_para"], e["line"]) for e in dangling}):
            guess = _near_miss(target, unreached_names)
            row = (target, source or "(entry)", f"{name}:{line}")
            if guess:
                paired.append(row + (guess, str(line_of[guess])))
            else:
                plain.append(row)

        if paired:
            out.append("")
            out.append(f"  {len(paired)} target(s) look like a misspelling of "
                       f"a paragraph that is declared")
            out.append("  but never performed. Two halves of one defect: the "
                       "name on the left is")
            out.append("  performed and does not exist, the name on the right "
                       "exists and never runs.")
            out.append(_table(
                ["performed", "from", "evidence", "declared instead", "at"],
                paired, 25))
            out.append("  The suggestion is name similarity only, not a fact. "
                       "Read both lines before")
            out.append("  acting on it.")

        if plain:
            out.append("")
            out.append(f"  {len(plain)} PERFORM/GO TO target(s) this member "
                       f"does not declare, with no")
            out.append("  similar name anywhere in it:")
            out.append(_table(["target", "from", "evidence"], plain, 25))
            out.append("  Either a paragraph that arrives with a copybook "
                       "COPYed into the PROCEDURE")
            out.append("  DIVISION, or a target that genuinely is not there. "
                       "Check the COPY list first.")

    calls = _rows(
        "SELECT paragraph, callee, variable, line, confidence FROM call_edge "
        "WHERE caller=? ORDER BY paragraph, line", name)
    if calls:
        out.append("")
        out.append("  Where this program leaves itself:")
        out.append(_table(
            ["paragraph", "calls", "evidence", "confidence"],
            [(c["paragraph"] or "(before the first paragraph)",
              c["callee"] or f"({c['variable']})", f"{name}:{c['line']}",
              c["confidence"]) for c in calls], 40))

    out.append("")
    out.append("  Paragraph names are the programmer's own labels, not facts. "
               "The 'does' column")
    out.append("  is, and where the two disagree believe the second and read "
               "the source.")
    return "\n".join(out)


def what_paragraph_does(program: str, paragraph: str,
                        member_type: str = "") -> str:
    """One paragraph: who performs it, what it performs, what it touches."""
    program = program.strip().upper()
    paragraph = paragraph.strip().upper()
    member = _program_member(program, member_type)
    if member is None:
        return chr(10).join([
            f"No COBOL member called {program} - paragraph facts "
            f"exist for COBOL only.",
            f"  find_object({program}) says what the name actually "
            f"is."])
    kind = member["member_type"]

    rows = _rows(
        "SELECT name, kind, line, end_line FROM paragraph "
        "WHERE member=? AND member_type=? AND name=? ORDER BY line",
        program, kind, paragraph)
    if not rows:
        like = _rows(
            "SELECT name, line FROM paragraph WHERE member=? AND member_type=? "
            "AND name LIKE ? ORDER BY line LIMIT 15",
            program, kind, f"%{paragraph}%")
        if like:
            return (f"{program} has no paragraph called exactly {paragraph}. "
                    f"Similar:\n"
                    + _table(["paragraph", "line"],
                             [(r["name"], r["line"]) for r in like]))
        performed = _rows(
            "SELECT from_para, line FROM perform_edge WHERE member=? "
            "AND member_type=? AND to_para=? ORDER BY line",
            program, kind, paragraph)
        if performed:
            return (f"{program} performs {paragraph} but never declares it - "
                    f"performed from "
                    + ", ".join(f"{r['from_para']} ({program}:{r['line']})"
                                for r in performed[:6])
                    + ".\n  Either a misspelling or a paragraph that arrives "
                      "with a copybook. Check the COPY list.")
        return (f"{program} has no paragraph called {paragraph}. Use "
                f"program_structure for {program} to see the ones it has.")

    para = rows[0]
    start, end = para["line"], para["end_line"]
    out: List[str] = [
        f"{program} . {paragraph}   ({kind}, {para['kind']}, "
        f"lines {start}-{end}, {max(0, end - start + 1)} lines)",
        f"  source: read_member {program} start={start} "
        f"count={max(1, end - start + 1)}",
    ]

    inbound = _rows(
        "SELECT from_para, form, is_loop, in_loop, line FROM perform_edge "
        "WHERE member=? AND member_type=? AND (to_para=? OR thru_para=?) "
        "ORDER BY line", program, kind, paragraph, paragraph)
    out.append("")
    out.append("  PERFORMED BY")
    if inbound:
        out.append(_table(
            ["from", "how", "runs", "evidence"],
            [(r["from_para"] or "(top of the procedure division)",
              _FORM_WORDS.get(r["form"], r["form"]) or "PERFORM",
              "more than once" if (r["is_loop"] or r["in_loop"]) else "once",
              f"{program}:{r['line']}") for r in inbound]))
    else:
        out.append("    Nothing. Either this is the entry paragraph, or it is "
                   "dead code.")
        #: Before calling it dead, check whether something performs a name one
        #: character away from this one.  That is a misspelt PERFORM, not dead
        #: code, and the difference decides whether the paragraph runs at all.
        undeclared = [r["to_para"] for r in _rows(
            "SELECT DISTINCT e.to_para FROM perform_edge e WHERE e.member=? "
            "AND e.member_type=? AND e.to_para<>'' AND NOT EXISTS ("
            "  SELECT 1 FROM paragraph p WHERE p.member=e.member "
            "  AND p.member_type=e.member_type AND p.name=e.to_para)",
            program, kind)]
        guess = _near_miss(paragraph, undeclared)
        if guess:
            where = _rows(
                "SELECT from_para, line FROM perform_edge WHERE member=? "
                "AND member_type=? AND to_para=? ORDER BY line LIMIT 4",
                program, kind, guess)
            sites = ", ".join(f"{r['from_para'] or '(entry)'} "
                              f"({program}:{r['line']})" for r in where)
            out.append(f"    But {guess} IS performed, from {sites}, and no "
                       f"paragraph of that")
            out.append(f"    name is declared in {program}. On name similarity "
                       f"alone that is probably")
            out.append(f"    this paragraph misspelt - in which case it never "
                       f"runs and neither does")
            out.append(f"    anything it performs. Read both lines before "
                       f"acting on it.")

    outbound = _rows(
        "SELECT to_para, thru_para, form, is_loop, inline, in_loop, line "
        "FROM perform_edge WHERE member=? AND member_type=? AND from_para=? "
        "ORDER BY line", program, kind, paragraph)
    out.append("")
    out.append("  PERFORMS")
    if outbound:
        body = []
        for r in outbound:
            target = r["to_para"] or "(no target - the body is written here)"
            if r["thru_para"]:
                target += f" THRU {r['thru_para']}"
            body.append((
                target,
                _FORM_WORDS.get(r["form"], r["form"]) or "PERFORM",
                "more than once" if (r["is_loop"] or r["in_loop"]) else "once",
                f"{program}:{r['line']}"))
        out.append(_table(["target", "how", "runs", "evidence"], body))
    else:
        out.append("    Nothing - it is a leaf.")

    io = _rows(
        "SELECT verb, file, record, line, confidence FROM io_site "
        "WHERE member=? AND member_type=? AND paragraph=? ORDER BY line",
        program, kind, paragraph)
    if io:
        out.append("")
        out.append("  FILE I/O")
        out.append(_table(
            ["verb", "file", "record", "evidence", "confidence"],
            [(r["verb"], r["file"], r["record"] or "",
              f"{program}:{r['line']}", r["confidence"]) for r in io]))

    calls = _rows(
        "SELECT callee, variable, line, confidence FROM call_edge "
        "WHERE caller=? AND paragraph=? ORDER BY line", program, paragraph)
    if calls:
        out.append("")
        out.append("  CALLS OUT TO ANOTHER PROGRAM")
        out.append(_table(
            ["program", "evidence", "confidence"],
            [(r["callee"] or f"({r['variable']})", f"{program}:{r['line']}",
              r["confidence"]) for r in calls]))

    screens = _rows(
        "SELECT verb, format_name, device_file, line FROM format_usage "
        "WHERE program=? AND paragraph=? ORDER BY line", program, paragraph)
    if screens:
        out.append("")
        out.append("  SCREEN / REPORT FORMATS")
        out.append(_table(
            ["verb", "format", "device file", "evidence"],
            [(r["verb"], r["format_name"], r["device_file"] or "?",
              f"{program}:{r['line']}") for r in screens]))

    sql = _rows(
        "SELECT verb, tables_json, line FROM sql_usage "
        "WHERE program=? AND paragraph=? ORDER BY line", program, paragraph)
    if sql:
        out.append("")
        out.append("  SQL")
        body = []
        for r in sql:
            try:
                tables = ", ".join(json.loads(r["tables_json"] or "[]"))
            except json.JSONDecodeError:
                tables = ""
            body.append((r["verb"], tables, f"{program}:{r['line']}"))
        out.append(_table(["verb", "tables", "evidence"], body))

    writes = _rows(
        # paragraphs is a comma list now, so match a whole element.
        # CAST on `lines` picks up the leading integer, and lines are stored
        # ascending, so MIN(CAST(...)) is the first occurrence.
        "SELECT field, file, SUM(n) n, MIN(" + _FIRST_LINE + ") line "
        "FROM field_usage "
        "WHERE program=? AND direction='write' "
        "  AND (','||paragraphs||',') LIKE ('%,'||?||',%') "
        "GROUP BY field, file ORDER BY field", program, paragraph)
    if writes:
        out.append("")
        out.append(f"  FIELDS IT WRITES ({len(writes)})")
        out.append(_table(
            ["field", "on file", "evidence"],
            [(r["field"], r["file"] or "", f"{program}:{r['line']}")
             for r in writes], 30))

    #: MIN(m.text), not m.text: (msgid, member) is the key on message, so an
    #: id defined in two files would otherwise duplicate every usage row.
    msgs = _rows(
        "SELECT u.msgid, MIN(m.text) text, MIN(u.line) line "
        "FROM message_usage u LEFT JOIN message m ON m.msgid = u.msgid "
        "WHERE u.program=? AND u.paragraph=? GROUP BY u.msgid, u.line "
        "ORDER BY MIN(u.line)", program, paragraph)
    if msgs:
        out.append("")
        out.append("  MESSAGES")
        out.append(_table(
            ["msgid", "text", "evidence"],
            [(r["msgid"], (r["text"] or "")[:44], f"{program}:{r['line']}")
             for r in msgs], 20))

    return "\n".join(out)


def find_paragraph(name: str = "", touches: str = "", calls: str = "",
                   writes: str = "", limit: int = 40,
                   page: int = 1) -> str:
    """Find paragraphs across the whole codebase by name or by what they do."""
    name = name.strip().upper()
    touches = touches.strip().upper()
    calls = calls.strip().upper()
    writes = writes.strip().upper()
    if not (name or touches or calls or writes):
        return ("Give at least one of: name (a substring of the paragraph "
                "name), touches (a file), calls (a program), writes (a "
                "field).")

    limit = max(1, min(limit, 200))
    clauses: List[str] = []
    args: List = []
    if name:
        clauses.append("p.name LIKE ?")
        args.append(f"%{name}%")
    if touches:
        clauses.append(
            "EXISTS (SELECT 1 FROM io_site s WHERE s.member=p.member "
            "AND s.member_type=p.member_type AND s.paragraph=p.name "
            "AND s.file=?)")
        args.append(touches)
    if calls:
        clauses.append(
            "EXISTS (SELECT 1 FROM call_edge c WHERE c.caller=p.member "
            "AND c.paragraph=p.name AND c.callee=?)")
        args.append(calls)
    if writes:
        clauses.append(
            "EXISTS (SELECT 1 FROM field_usage f WHERE f.program=p.member "
            "AND (','||f.paragraphs||',') LIKE ('%,'||p.name||',%') "
            "AND f.field=? AND f.direction='write')")
        args.append(writes)

    rows = _rows(
        "SELECT p.member, p.member_type, p.name, p.line, p.end_line "
        "FROM paragraph p WHERE " + " AND ".join(clauses)
        + " ORDER BY p.member, p.line LIMIT ?", *args, limit + 1)

    asked = ", ".join(filter(None, [
        f"name contains {name}" if name else "",
        f"touches {touches}" if touches else "",
        f"calls {calls}" if calls else "",
        f"writes {writes}" if writes else ""]))

    if not rows:
        return (f"No paragraph matches: {asked}.\n"
                f"  Paragraph facts exist for COBOL members only. If you "
                f"searched by file name, try describe_file first - the "
                f"program may reach it through a logical under another name.")

    more = len(rows) > limit
    rows = rows[:limit]
    body: List[Tuple] = []
    for r in rows:
        notes = _paragraph_notes(r["member"], r["member_type"])
        body.append((r["member"], r["member_type"], r["name"],
                     f"{r['line']}-{r['end_line']}",
                     "; ".join(notes.get(r["name"], []))[:44]))

    out = [f"Paragraphs where {asked}:", "",
           # PAGE_ROWS, not `limit`. `limit` is how many paragraphs the
           # QUERY returns; the page size is how many are shown at once.
           # Passing limit as both made them the same number, so a 200-row
           # answer was one 200-row page and `page` could never do anything.
           _paged_table(["member", "type", "paragraph", "lines", "does"],
                        body, page, PAGE_ROWS)]
    if more:
        out.append(f"  More than {limit} paragraphs matched, so this is the first {limit}.")
        out.append("  Raise limit to widen the set and page through it. Narrowing")
        out.append("  with touches= or calls= costs less than paging a wide one.")
    out.append("")
    out.append("  Call what_paragraph_does on any of these for its edges, or "
               "read_member with the")
    out.append("  line range to read it.")
    return "\n".join(out)
# ===========================================================================
# 18/19. Both directions, every layer
# ===========================================================================
#
# The knowledge base held nine kinds of edge and only some of them could be
# asked about, and mostly in one direction.  33,935 COPY edges and 31,244
# message edges had no tool at all; nothing answered "what does this member
# copy" or "what does it read and write", which is the first question anybody
# asks about a member.
#
# Rather than one tool per layer per direction - eighteen tools - these two
# take a direction each and a `kind` to narrow.  That keeps the tool list
# short, which matters: the list itself sits in the agent's context on every
# turn of every conversation.
#
# Both are built to be read in a small context window.  With kind='all' they
# return counts per layer and a handful of examples; with kind set to one layer
# they return that layer in full.  A set of a thousand members is folded to one
# row per subsystem, because "1,109 programs COPY CUSMAS, here are 40 of them"
# is a worse answer than "1,109 across 63 subsystems, ORD has 187".

#: kind -> the layers it selects, for both tools.
_LAYER_ALIASES = {
    "files": "files", "file": "files", "io": "files",
    "copybooks": "copybooks", "copy": "copybooks", "copybook": "copybooks",
    "calls": "calls", "call": "calls",
    "overrides": "overrides", "override": "overrides", "ovrdbf": "overrides",
    "screens": "screens", "screen": "screens", "formats": "screens",
    "messages": "messages", "message": "messages", "msg": "messages",
    "dataareas": "dataareas", "dataarea": "dataareas", "dtaara": "dataareas",
    "sql": "sql",
    "fields": "fields", "field": "fields",
    "indicators": "indicators", "indicator": "indicators",
    "drift": "drift", "header": "drift",
    "unresolved": "unresolved", "gaps": "unresolved",
    "logicals": "logicals", "logical": "logicals",
    # Implemented since the message layer landed and reachable only
    # through kind='all': it was never in the enum and never here, so
    # an agent asking for it by name got the "all" report instead.
    "defines": "defines", "define": "defines",
}


def _layer(kind: str) -> str:
    kind = (kind or "all").strip().lower()
    if kind in ("", "all", "*"):
        return "all"
    return _LAYER_ALIASES.get(kind, kind)


#: In kind='all' a layer is expanded only when it is short enough to be worth
#: reading in full.  Anything bigger becomes a count and a pointer - the whole
#: point of these two tools is to be cheap to call on a codebase where nobody
#: can afford to read the full answer.
_ALL_INLINE_MAX = 12


#: How many names go into one `IN (...)` list.
#:
#: SQLite's SQLITE_MAX_VARIABLE_NUMBER is 999 before 3.32 and 32,766 after;
#: PostgreSQL's bind limit is 65,535.  900 is under the floor of both, so the
#: same chunk size is safe on either store and nothing has to ask which one it
#: is talking to.
_IN_CHUNK = 900


def _in_chunks(seq: Sequence[str], size: int = _IN_CHUNK):
    """Yield (values, placeholders) pairs for a chunked IN list.

    Deduplicates first, because the callers are folding sets and a repeated
    name buys a wider query for no extra answer.
    """
    unique = list(dict.fromkeys(seq))
    for i in range(0, len(unique), size):
        part = unique[i:i + size]
        yield part, ",".join("?" for _ in part)


def _duplicate_note(names: "Sequence[str]") -> str:
    """Names in this set that exist in more than one library.

    A member existing twice is two different programs wearing one name, and
    they differ - on one estate two copies of the same name were 4,142 and
    4,091 lines. Editing one and recompiling against the other produces a
    mismatch no compiler catches, and a folded list shows the name once with
    nothing to say which copy is meant.
    """
    if not names:
        return ""
    dupes = []
    for part, marks in _in_chunks(list(dict.fromkeys(names))):
        dupes.extend(_rows(
            f"SELECT name, COUNT(DISTINCT library) n, "
            f"GROUP_CONCAT(DISTINCT library) libs FROM object "
            f"WHERE name IN ({marks}) GROUP BY name HAVING n > 1", *part))
    if not dupes:
        return ""
    out = [f"  ** {len(dupes)} name(s) here exist in MORE THAN ONE LIBRARY - "
           f"each copy is its own change **"]
    for d in dupes[:12]:
        out.append(f"     {d['name']:<12}{d['libs']}")
    if len(dupes) > 12:
        out.append(f"     ... and {len(dupes) - 12:,} more")
    out.append("     Which one compiles depends on the library list at build "
               "time. List each copy separately.")
    return chr(10).join(out)


def _by_library(names: Sequence[str]) -> str:
    """Every name, under the library it lives in.

    An impact report has to say WHICH members change and WHICH recompile, not
    how many - somebody has to open each one. So when a set is small enough to
    print, it is printed whole, grouped by the library that ships it.

    Wrapped rather than tabulated: 1,109 member names as a one-column table is
    1,109 lines and 11KB of mostly whitespace, and the same names wrapped
    under four headings are 9KB and read as a work list. Both are inside the
    40,000-char response cap; only one is usable.

    One query, chunked - the same shape `_fold` uses below.
    """
    names = list(dict.fromkeys(names))
    if not names:
        return "  (none)"

    known: Dict[str, str] = {}
    for part, marks in _in_chunks(names):
        for row in _rows(f"SELECT DISTINCT name, library FROM object "
                         f"WHERE name IN ({marks})", *part):
            known[row["name"]] = row["library"] or ""

    groups: Dict[str, List[str]] = defaultdict(list)
    for name in names:
        # "?" for a name `object` does not hold - a reference to something
        # outside the extraction. Rule 2: it keeps its place in the count.
        groups[known.get(name) or "?"].append(name)

    out: List[str] = []
    for library in sorted(groups, key=lambda k: (-len(groups[k]), k)):
        members = sorted(groups[library])
        out.append(f"  {library}  ({len(members)})")
        out.extend(textwrap.wrap("  ".join(members), width=74,
                                 initial_indent="    ",
                                 subsequent_indent="    "))
    return chr(10).join(out)


def _fold(names: Sequence[str], limit: int) -> str:
    """A list of members, folded by LIBRARY once it stops being readable.

    Handing an agent 1,109 member names spends its context and tells it
    nothing.  The shape of the set - which libraries, how concentrated - is
    what decides where to look, and it fits in four rows.

    ## Library, not subsystem

    This folded by `subsystem`, which is a guess: `infer_subsystem` takes the
    first few characters of the name and keeps the prefix if five or more
    members share it.  The footer said so - "inferred from the name prefix,
    not asserted".  Library is read from the member's path and is a fact.

    It is also a better fold.  On CUSMAS the subsystem grouping produces 37
    groups and the table cuts at 25, so the answer is both a guess and
    incomplete; the library grouping produces 4 and is whole.

    And it is what an impact report needs.  "Which files do I change, and what
    rebuilds" is answered per library, because that is the unit somebody
    compiles and ships.  Getting there any other way costs one `find_object`
    per member - 1,109 calls and ~72,000 tokens on this file - because no
    other tool output carries a member's library.

    The query is unchanged in shape: `_in_chunks` already joins `object` in
    bulk, and `object` already holds both columns.  This selects the other one.
    """
    names = list(dict.fromkeys(names))
    note = _duplicate_note(names)
    if len(names) <= limit:
        body = _by_library(names)
        return body + (chr(10) + note if note else "")

    # One query per chunk, not one per name.  This ran a point lookup for
    # every member in the set, and the sets it exists to fold are large by
    # definition: 1,847 programs read CUSMAS, so folding that list cost 1,847
    # round trips.  Measured against the same data, one grouped query is
    # 12.8ms against 654ms - and on a PostgreSQL over a socket rather than a
    # local file, the 1,847 round trips are the whole cost.
    #
    # The grouping stays in Python.  Doing it in SQL would need
    # group_concat/string_agg for the examples, whose ordering semantics
    # differ between the two stores, and the sort is already here.
    known: Dict[str, str] = {}
    for part, marks in _in_chunks(names):
        # DISTINCT: a name can be two member types in one library - 91 menus
        # are an MNUCMD and an MNUDDS - and without it those members would be
        # counted twice and the groups would not sum to the count the caller
        # already printed.
        for row in _rows(f"SELECT DISTINCT name, library FROM object "
                         f"WHERE name IN ({marks})", *part):
            known[row["name"]] = row["library"] or ""

    groups: Dict[str, List[str]] = defaultdict(list)
    for name in names:
        # `or "?"` twice over: absent from `object`, or present with no
        # library.  Rule 2 - a name we cannot place goes in a bucket that
        # says so, it does not get dropped from the count.
        groups[known.get(name) or "?"].append(name)
    body = [(group, len(members), ", ".join(sorted(members)[:4]))
            for group, members in sorted(groups.items(),
                                         key=lambda kv: -len(kv[1]))]
    return (f"  {len(names):,} members across {len(groups)} librar"
            f"{'y' if len(groups) == 1 else 'ies'}, too many to list. "
            f"By library:\n"
            + _table(["library", "members", "for example"], body, 25)
            + f"\n  Raise limit past {len(names):,} to get the full list."
            + (chr(10) + note if note else ""))


def _sections(out: List[str], title: str, body: str) -> None:
    out.append("")
    out.append(f"  {title}")
    out.append(body)


# ---------------------------------------------------------------------------

def what_it_touches(name: str, kind: str = "all", limit: int = 40) -> str:
    """Everything one member reaches, in every layer."""
    name = name.strip().upper()
    want = _layer(kind)
    limit = max(1, min(limit, 4000))

    obj = _rows("SELECT name, member_type, language, lines, subsystem, path, "
                "based_on, parse_status, notes, library FROM object "
                "WHERE name=? ORDER BY library, member_type", name)
    if not obj:
        return (f"No member called {name}. find_object does partial name "
                f"matching.")

    out: List[str] = []
    for o in obj:
        out.append(f"{o['name']}  ({o['member_type']}, {o['language']}, "
                   f"{o['lines']:,} lines){_where(o)}")
    if want != "all":
        out.append(f"  showing only: {want}")

    counts: List[str] = []

    # Each query below is gated on the layer it feeds.  It used to run all
    # of them whatever `kind` said: asking for kind='copybooks' still queried
    # files, calls, overrides, screens, messages, data areas, SQL, logicals
    # and the field count - 13 queries to render 2.  The gates were already
    # written on the RENDERING a few lines under each one, so this is the
    # same condition moved up to where the work happens.
    #
    # Safe because `counts` is only rendered when want == "all" (see the
    # bottom of this function), so a narrowed call has no reader for the
    # numbers it is no longer fetching.

    # ---- files -----------------------------------------------------------
    files = _rows(
        "SELECT file, mode, verbs, device, line, confidence, "
        "  (mode='I' AND (verbs LIKE '%WRITE%' OR verbs LIKE '%DELETE%')) "
        "  AS contradicts "
        "FROM file_usage WHERE program=? ORDER BY device, mode DESC, file",
        name) if want in ("all", "files") else []
    if files:
        reads = sum(1 for f in files if f["mode"] == "I")
        writes = sum(1 for f in files if f["mode"] in ("O", "U"))
        counts.append(f"{len(files)} file(s) - {writes} it can change, "
                      f"{reads} read only")
    if files and want in ("all", "files"):
        shown = files if want == "files" else files[:10]
        _sections(out, "FILES IT OPENS", _table(
            ["file", "mode", "verbs", "device", "evidence", "confidence"],
            [(f["file"], {"I": "read", "O": "write", "U": "update"}.get(
                f["mode"], f["mode"]) + (" (!)" if f["contradicts"] else ""),
              f["verbs"] or "", f["device"],
              f"{name}:{f['line']}", f["confidence"]) for f in shown], limit))
        if any(f["contradicts"] for f in files):
            out.append("  (!) the OPEN says INPUT but a WRITE, REWRITE or "
                       "DELETE is issued on it.")
            out.append("  Valid COBOL cannot do that, so one of the two is "
                       "wrong and the file may")
            out.append("  well be changed here. kb.index.consistency --check "
                       "write_on_input_open.")
        if want == "all" and len(files) > 10:
            out.append(f"  ... {len(files) - 12} more. kind='files' for all "
                       f"of them.")
        io = _rows(
            "SELECT paragraph, verb, file, COUNT(*) n FROM io_site "
            "WHERE member=? AND paragraph<>'' GROUP BY paragraph, verb, file "
            "ORDER BY MIN(line)", name)
        if io and want == "files":
            _sections(out, "WHERE THE I/O HAPPENS", _table(
                ["paragraph", "verb", "file", "times"],
                [(r["paragraph"], r["verb"], r["file"], r["n"]) for r in io],
                limit))

    # ---- copybooks -------------------------------------------------------
    copies = _rows(
        "SELECT c.copybook, c.kind, c.replacing, c.line, c.confidence, "
        "  (SELECT o.member_type FROM object o WHERE o.name=c.copybook LIMIT 1) t "
        "FROM copy_edge c WHERE c.program=? ORDER BY c.kind, c.copybook", name) if want in ("all", "copybooks") else []
    if copies:
        missing = sum(1 for c in copies if not c["t"])
        counts.append(f"{len(copies)} COPY statement(s)"
                      + (f", {missing} unresolved" if missing else ""))
    if copies and want in ("all", "copybooks"):
        _sections(out, "COPYBOOKS AND RECORD LAYOUTS IT COPIES", _table(
            ["copybook", "how", "resolves to", "evidence"],
            [(c["copybook"],
              "COPY of a record layout"
              if c["kind"] in ("record_layout", "device_layout",
                               "generated_layout", "dds_all_formats")
              else ("COPY ... REPLACING" if c["replacing"] else "COPY"),
              c["t"] or "NOT IN THE SOURCE TREE", f"{name}:{c['line']}")
             for c in copies], limit if want == "copybooks" else 12))
        out.append("  A change to any of these recompiles this member.")

    # ---- calls -----------------------------------------------------------
    calls = _rows(
        "SELECT callee, variable, kind, paragraph, line, confidence "
        "FROM call_edge WHERE caller=? ORDER BY callee, line", name) if want in ("all", "calls") else []
    if calls:
        counts.append(f"{len(calls)} CALL(s)")
    if calls and want in ("all", "calls"):
        _sections(out, "PROGRAMS IT CALLS", _table(
            ["program", "how", "from paragraph", "evidence", "confidence"],
            [(c["callee"] or f"({c['variable']})", c["kind"],
              c["paragraph"] or "", f"{name}:{c['line']}", c["confidence"])
             for c in calls], limit if want == "calls" else 12))

    # ---- overrides -------------------------------------------------------
    overrides = _rows(
        "SELECT logical_name, target_file, library, share, kind, line, "
        "confidence FROM override WHERE cl_program=? ORDER BY logical_name",
        name) if want in ("all", "overrides") else []
    if overrides:
        counts.append(f"{len(overrides)} override(s)")
    if overrides and want in ("all", "overrides"):
        _sections(out, "FILE OVERRIDES IT SETS", _table(
            ["logical", "becomes", "library", "kind", "evidence", "confidence"],
            [(o["logical_name"], o["target_file"], o["library"] or "",
              o["kind"], f"{name}:{o['line']}", o["confidence"])
             for o in overrides], limit if want == "overrides" else 12))
        out.append("  These decide what the called program really opens - "
                   "resolve_overrides follows them through.")

    # ---- screens and reports ---------------------------------------------
    screens = _rows(
        "SELECT device_file, format_name, verb, paragraph, line, confidence "
        "FROM format_usage WHERE program=? ORDER BY device_file, line", name) if want in ("all", "screens") else []
    if screens:
        counts.append(f"{len(screens)} record format write(s)/read(s)")
    if screens and want in ("all", "screens"):
        _sections(out, "SCREEN AND REPORT FORMATS IT USES", _table(
            ["device file", "format", "verb", "from paragraph", "evidence"],
            [(s["device_file"] or "?", s["format_name"], s["verb"],
              s["paragraph"] or "", f"{name}:{s['line']}") for s in screens],
            limit if want == "screens" else 12))

    # ---- messages --------------------------------------------------------
    messages = _rows(
        "SELECT u.msgid, MIN(m.text) text, MIN(m.member) defined_in, "
        "COUNT(DISTINCT u.line) n, MIN(u.line) line "
        "FROM message_usage u LEFT JOIN message m ON m.msgid = u.msgid "
        "WHERE u.program=? GROUP BY u.msgid ORDER BY u.msgid", name) if want in ("all", "messages") else []
    if messages:
        unknown = sum(1 for m in messages if not m["text"])
        counts.append(f"{len(messages)} distinct message(s)"
                      + (f", {unknown} not defined in any MSGF" if unknown
                         else ""))
    if messages and want in ("all", "messages"):
        _sections(out, "MESSAGES IT SENDS", _table(
            ["msgid", "text", "defined in", "times", "first at"],
            [(m["msgid"], (m["text"] or "NOT DEFINED")[:44],
              m["defined_in"] or "?", m["n"], f"{name}:{m['line']}")
             for m in messages],
            limit if want == "messages" else 12))

    # ---- data areas ------------------------------------------------------
    areas = _rows("SELECT DISTINCT name FROM data_area WHERE member=? "
                  "ORDER BY name", name) if want in ("all", "dataareas") else []
    if areas:
        counts.append(f"{len(areas)} data area(s)")
    if areas and want in ("all", "dataareas"):
        _sections(out, "DATA AREAS IT READS OR CHANGES", _table(
            ["data area"], [(a["name"],) for a in areas], limit))
        out.append("  A data area is shared mutable state with no schema. "
                   "Two programs that")
        out.append("  use the same one are coupled and nothing in the source "
                   "says how.")

    # ---- sql -------------------------------------------------------------
    sql = _rows("SELECT verb, tables_json, paragraph, line FROM sql_usage "
                "WHERE program=? ORDER BY line", name) if want in ("all", "sql") else []
    if sql:
        counts.append(f"{len(sql)} EXEC SQL statement(s)")
    if sql and want in ("all", "sql"):
        body = []
        for r in sql:
            try:
                tables = ", ".join(json.loads(r["tables_json"] or "[]"))
            except json.JSONDecodeError:
                tables = ""
            body.append((r["verb"], tables, r["paragraph"] or "",
                         f"{name}:{r['line']}"))
        _sections(out, "SQL IT RUNS", _table(
            ["verb", "tables", "from paragraph", "evidence"], body,
            limit if want == "sql" else 12))

    # ---- logicals over it, when it is a file ------------------------------
    logicals = _rows("SELECT name, member_type FROM object WHERE based_on=? "
                     "ORDER BY name", name) if want in ("all", "logicals") else []
    if logicals:
        counts.append(f"{len(logicals)} logical file(s) over it")
    if logicals and want in ("all", "logicals"):
        _sections(out, "LOGICAL FILES BUILT OVER IT", _table(
            ["logical", "type"],
            [(r["name"], r["member_type"]) for r in logicals], limit))

    # ---- fields ----------------------------------------------------------
    if want == "fields":
        fields = _rows(
            "SELECT field, direction, file, paragraphs, SUM(n) n, "
            "MIN(" + _FIRST_LINE + ") line FROM field_usage WHERE program=? "
            "GROUP BY field, direction, file, paragraphs "
            "ORDER BY direction DESC, field", name)
        _sections(out, "FIELDS IT READS AND WRITES", _table(
            ["field", "direction", "on file", "paragraph", "evidence"],
            [(r["field"], r["direction"], r["file"] or "",
              (r["paragraphs"] or "").split(",")[0], f"{name}:{r['line']}")
             for r in fields],
            limit))
    elif want == "all":
        # A count for the summary line, so only the summary line needs it.
        n = _rows("SELECT SUM(n) c, "
                  "SUM(CASE WHEN direction='write' THEN n ELSE 0 END) w FROM "
                  "field_usage WHERE program=?", name)[0]
        if n["c"]:
            counts.append(f"{n['c']} field reference(s), {n['w'] or 0} writes "
                          f"(kind='fields')")

    # ---- indicators ------------------------------------------------------
    if want == "indicators":
        inds = _rows("SELECT number, role, purpose, COUNT(*) n, MIN(line) line "
                     # Same bare-column class as the field_usage one: SQLite
                     # picked an arbitrary purpose, PostgreSQL raises 42803.
                     # Grouped, so one row per distinct purpose - an
                     # indicator tested for two reasons is two facts.
                     "FROM indicator WHERE member=? "
                     "GROUP BY number, role, purpose "
                     "ORDER BY number", name)
        _sections(out, "INDICATORS", _table(
            ["number", "role", "purpose", "times", "first at"],
            [(r["number"], r["role"], r["purpose"] or "", r["n"],
              f"{name}:{r['line']}") for r in inds], limit))

    # ---- what we could not work out --------------------------------------
    # Gated for the same reason as the layers above: both are rendered only
    # under kind='all' or kind='drift'.
    _honesty = want in ("all", "drift")
    unresolved = _rows("SELECT kind, COUNT(*) n FROM unresolved WHERE member=? "
                       "GROUP BY kind ORDER BY n DESC",
                       name) if _honesty else []
    drift = _rows("SELECT finding FROM header_disagreement WHERE member=?",
                  name) if _honesty else []

    if want == "all":
        out.insert(len(obj), "  " + "; ".join(counts) if counts
                   else "  nothing recorded - see parse status below")
        paras = _rows("SELECT COUNT(*) c FROM paragraph WHERE member=?",
                      name)[0]["c"]
        tail = ["", "  Narrow with kind=files|copybooks|calls|overrides|"
                    "screens|messages|dataareas|sql|fields|indicators."]
        if paras > 1:
            tail.append(f"  program_structure shows how its {paras} paragraphs "
                        f"fit together.")
        tail.append("  what_uses goes the other way - what reaches this "
                    "member.")
        out.extend(tail)

    if unresolved and want in ("all", "drift"):
        _sections(out, "WHAT WE COULD NOT WORK OUT HERE", _table(
            ["kind", "times"],
            [(r["kind"], r["n"]) for r in unresolved], 12))
        out.append(f"  what_it_touches({name}, kind='unresolved') shows each "
                   f"source line and why.")

    if want == "unresolved":
        # The lines themselves. A count of "dynamic_call 3" says a gap exists;
        # only the statement says whether it matters to this ticket.
        gaps = _rows("SELECT kind, line, raw, reason FROM unresolved "
                     "WHERE member=? ORDER BY line, kind", name)
        if not gaps:
            out.append(f"\n  Nothing unresolved: every statement the parser "
                       f"read in {name} produced its facts.")
        else:
            _sections(out, f"UNRESOLVED IN {name} ({len(gaps)})", _table(
                ["evidence", "kind", "source", "why"],
                [(f"{name}:{g['line']}", g["kind"],
                  " ".join((g["raw"] or "").split())[:70],
                  (g["reason"] or "")[:80])
                 for g in gaps], limit))

    if drift and want in ("all", "drift"):
        _sections(out, f"HEADER COMMENT DISAGREES WITH THE CODE "
                       f"({len(drift)})", _table(
            ["finding"], [(r["finding"],) for r in drift],
            limit if want == "drift" else 6))
        out.append("  The code is the fact. This is listed so you do not "
                   "trust the header.")

    return "\n".join(out)


# ---------------------------------------------------------------------------

def _copy_closure(family: "Sequence[str]") -> "Tuple[List[dict], Dict[str, int]]":
    """Everything that copies these names, directly or through a copybook.

    A COPY binds at compile time, and it nests. A copybook that copies a
    record layout passes that layout on to everything copying the copybook, so
    those members recompile too - and a single-hop query cannot see them.

    Measured on a real analysis: 175 direct copiers, and 13 more reachable
    only through six intermediate copybooks. That is not a cosmetic gap. An
    unrecompiled member of that set reads shifted data at run time and fails
    silently, which is worse than failing at OPEN.

    Returns (rows, via) where `via` names each intermediate and how many
    members it brought in, so the two figures can be reported separately
    rather than merged into one number nobody can check.
    """
    seen_targets = set(family)
    frontier = list(family)
    direct: Dict[str, dict] = {}
    indirect: Dict[str, dict] = {}
    via: Dict[str, int] = {}
    hop = 0

    while frontier and hop < 8:
        marks = ",".join("?" * len(frontier))
        rows = _rows(f"SELECT DISTINCT program, kind, copybook FROM copy_edge "
                     f"WHERE copybook IN ({marks})", *frontier)
        nxt: List[str] = []
        for row in rows:
            program = row["program"]
            bucket = direct if hop == 0 else indirect
            if program not in direct and program not in indirect:
                bucket[program] = row
            elif hop > 0 and program in indirect:
                pass
            # A member that copies the layout AND is itself copied passes the
            # layout on. Following it is the whole point.
            if program in seen_targets:
                continue
            is_intermediate = _rows(
                "SELECT 1 FROM copy_edge WHERE copybook=? LIMIT 1", program)
            if is_intermediate:
                seen_targets.add(program)
                nxt.append(program)
                if hop == 0:
                    via.setdefault(program, 0)
        frontier = nxt
        hop += 1

    # Attribute each indirect member to the intermediate that reached it.
    for name in list(via):
        brought = _rows("SELECT DISTINCT program FROM copy_edge "
                        "WHERE copybook=?", name)
        via[name] = sum(1 for b in brought if b["program"] in indirect)
    via = {k: v for k, v in via.items() if v}

    out = list(direct.values()) + [r for k, r in indirect.items()
                                   if k not in direct]
    return out, via


def file_family(name: str) -> "List[str]":
    """A file and every other file that shares its record layout.

    A DDS layout change forces a recompile of everything that copies the
    LAYOUT, and the layout is shared by a physical file and every logical
    built over it. Which of those names a program wrote in its COPY statement
    is an accident of what it needed to read - so answering only for the name
    the ticket happened to mention loses the rest.

    Measured on this index: AUDTRL is copied by 376 programs and its logical
    AUDTRLL4 by 3. A layout change recompiles all 379. Asking about the
    logical answered 3 and said nothing about the other 376, which is the
    shape of a wrong recompile set that looks complete.

    Returns the name first, then the physical it is built over, then every
    logical over that physical.
    """
    family = [name]
    physical = name
    for row in _rows("SELECT based_on FROM object WHERE name=? "
                     "AND based_on<>''", name):
        physical = row["based_on"]
        break
    if physical != name and physical not in family:
        family.append(physical)
    for row in _rows("SELECT DISTINCT name FROM object WHERE based_on=? "
                     "ORDER BY name", physical):
        if row["name"] not in family:
            family.append(row["name"])
    return family


def _device_users(name: str) -> Dict[str, Dict[str, Any]]:
    """program -> how it uses a display or printer file.

    Three independent facts, any one of which ties a program to the file: the
    SELECT that declares it, the COPY that brings in its formats, and the
    FORMAT IS writes. Counting only the third made every program that never
    writes a named format - a program that COPYs the formats and writes the
    record, for one - invisible, and the answer read "no users".
    """
    users: Dict[str, Dict[str, Any]] = {}

    def entry(program: str) -> Dict[str, Any]:
        return users.setdefault(program, {"assign": False, "copy": False,
                                          "writes": 0, "formats": set()})

    for r in _rows("SELECT program, COUNT(*) n, GROUP_CONCAT(DISTINCT format_name) f "
                   "FROM format_usage WHERE device_file=? GROUP BY program", name):
        e = entry(r["program"])
        e["writes"] = r["n"]
        e["formats"].update(filter(None, (r["f"] or "").split(",")))
    for r in _rows("SELECT DISTINCT program FROM file_usage WHERE file=? "
                   "AND device IN ('workstation','printer')", name):
        entry(r["program"])["assign"] = True
    if _is_device_file(name):
        for r in _rows("SELECT DISTINCT program FROM copy_edge WHERE copybook=?",
                       name):
            entry(r["program"])["copy"] = True
    return users


def _is_device_file(name: str) -> bool:
    return bool(_rows("SELECT 1 FROM object WHERE name=? AND member_type IN "
                      "('DSPF','PRTF') LIMIT 1", name))


def _no_device_users(name: str) -> List[str]:
    """A zero, with what was checked and whether the check could have missed."""
    out = ["", f"  NO PROGRAM USES {name}: checked SELECT ... ASSIGN "
               f"WORKSTATION-/PRINTER-, COPY ... OF {name}, and FORMAT IS writes."]
    gaps = _rows("SELECT member, line FROM unresolved WHERE kind="
                 "'device_link_not_extracted' AND raw LIKE ? ORDER BY member",
                 f"%-{name}%")
    if gaps:
        out.append(f"  NOT PROVEN: {len(gaps)} device name(s) naming it were not "
                   f"read as a SELECT: "
                   + ", ".join(f"{g['member']}:{g['line']}" for g in gaps[:8]))
        out.append("  Read those lines; each is probably a user.")
    else:
        out.append(f"  Confirm with search_source(\"{name}\") - a CL "
                   f"SNDRCVF/DCLF or a program outside the extraction is not "
                   f"covered.")
    return out


@table_aware("name", "what_uses")
def what_uses(name: str, kind: str = "all", limit: int = 40) -> str:
    """Everything that reaches one object, whatever kind of object it is.

    The reverse of what_it_touches, and deliberately not typed by the caller.
    An agent holding the name CUSREC out of a ticket does not know whether it
    is a file, a copybook, a field, a record format or a message id, and having
    to guess which of five tools to call is how a lead gets dropped.

    kind='all' is a routing table, not an answer.  A busy file has 1,109
    programs copying it and 1,844 reading it; printing those by default spends
    six thousand characters of a context window to say "lots".  So `all`
    returns the count per layer and expands only what is short enough to read -
    plus the writers, always, because "what could have put this value here" is
    the question this tool exists for.
    """
    name = name.strip().upper()
    want = _layer(kind)
    limit = max(1, min(limit, 4000))

    obj = _rows("SELECT name, member_type, language, lines, based_on, "
                "library FROM object WHERE name=? "
                "ORDER BY library, member_type", name)
    identified: List[str] = [
        f"{o['member_type']} member"
        + (f" in {o['library']}" if o["library"] else "")
        + (f" over {o['based_on']}" if o["based_on"] else "") for o in obj]

    #: The layers that are not members at all.  These are the ones no tool
    #: reached before: a message id, a data area and a record format are all
    #: first-class things a ticket names, and none of them is in `object`.
    as_message = _rows("SELECT msgid, member, text FROM message WHERE msgid=?",
                       name)
    #: The other direction: this name is a message *file*, so what reaches it
    #: is every program that sends one of the ids it defines.
    defines = _rows("SELECT COUNT(*) c FROM message WHERE member=?",
                    name)[0]["c"]
    as_format = _rows("SELECT DISTINCT device_file FROM format WHERE name=?",
                      name)

    def count(sql: str, *args) -> int:
        return _rows(sql, *args)[0]["c"]

    n_calls = count("SELECT COUNT(DISTINCT caller) c FROM call_edge "
                    "WHERE callee=?", name)
    family = file_family(name) if any(
        o["member_type"] in ("PF", "LF") for o in obj) else [name]
    marks = ",".join("?" * len(family))
    _closure, _via = _copy_closure(family)
    n_copy = len({r["program"] for r in _closure})
    n_direct = count(f"SELECT COUNT(DISTINCT program) c FROM copy_edge "
                     f"WHERE copybook IN ({marks})", *family)
    #: A writer is a program whose OPEN says it can change the file, OR one
    #: that issues a mutating verb whatever the OPEN said.  Reading only the
    #: mode drops 294 programs across 234 files from this answer, and on
    #: "what could have put this value here" a false negative is the expensive
    #: kind of wrong.
    writers = _rows(
        "SELECT program, mode, verbs, "
        "  (mode='I' AND (verbs LIKE '%WRITE%' OR verbs LIKE '%DELETE%')) "
        "  AS contradicts "
        "FROM file_usage WHERE file=? AND (mode IN ('O','U') "
        "  OR verbs LIKE '%WRITE%' OR verbs LIKE '%DELETE%') "
        "ORDER BY program", name)
    readers = _rows(
        "SELECT program FROM file_usage WHERE file=? AND mode NOT IN ('O','U') "
        "AND verbs NOT LIKE '%WRITE%' AND verbs NOT LIKE '%DELETE%' "
        "ORDER BY program", name)
    n_over = count("SELECT COUNT(DISTINCT cl_program) c FROM override "
                   "WHERE target_file=?", name)
    n_logical = count("SELECT COUNT(*) c FROM object WHERE based_on=?", name)
    n_msg = count("SELECT COUNT(*) c FROM message_usage WHERE msgid=?", name)
    n_area = count("SELECT COUNT(DISTINCT member) c FROM data_area "
                   "WHERE name=?", name)
    n_fmt_prog = count("SELECT COUNT(DISTINCT program) c FROM format_usage "
                       "WHERE format_name=?", name)
    device_users = _device_users(name)
    n_device = len(device_users)
    n_field_w = count("SELECT COUNT(DISTINCT program) c FROM field_usage "
                      "WHERE field=? AND direction='write'", name)
    n_field_r = count("SELECT COUNT(DISTINCT program) c FROM field_usage "
                      "WHERE field=? AND direction='read'", name)

    if as_message or n_msg:
        identified.append("message id")
    if n_area:
        identified.append("data area")
    as_table = _sql_tables("name=? AND kind<>'index'", name)
    if as_table:
        identified.append(
            f"SQL {as_table[0]['kind']} created by {as_table[0]['ddl_member']}"
            + (f" '{as_table[0]['label']}'" if as_table[0]["label"] else ""))
    if as_format:
        identified.append("record format")
    if n_field_w or n_field_r:
        identified.append("field")
    if not obj and not as_table and (writers or readers or n_over):
        # A data file with no source of its own: CL creates, copies or
        # overrides it, and programs open it. Its users are real.
        identified.append("data file named in CL and programs")

    if not identified:
        near = _rows("SELECT name, member_type FROM object WHERE name LIKE ? "
                     "ORDER BY name LIMIT 12", f"%{name}%")
        if near:
            return (f"{name} is not a member, message, data area, record "
                    f"format or field. Similar names:\n"
                    + _table(["name", "type"],
                             [(r["name"], r["member_type"]) for r in near]))
        return (f"Nothing in the knowledge base is called {name}, in any "
                f"layer.\n  It may be a working-storage name, which is local "
                f"to one program - try search_source.")

    out: List[str] = [f"{name}  ({', '.join(identified)})"]
    if as_message:
        out.append(f"  message text: {as_message[0]['text'] or '(none)'}")
        where = ", ".join(r["member"] for r in as_message if r["member"])
        if where:
            out.append(f"  defined in: {where}"
                       + ("  - more than one definition, and which one a "
                          "program gets depends on the library list"
                          if len(as_message) > 1 else ""))
    elif n_msg:
        out.append("  NOT DEFINED in any message file in the source - it may "
                   "live in a MSGF that was not extracted.")
    if as_format:
        out.append("  a record format on: "
                   + ", ".join(r["device_file"] for r in as_format[:8]))

    #: (layer, count, label) - the routing table, and the order things are
    #: expanded in.
    layers = [
        ("calls", n_calls, "programs that CALL it"),
        ("copybooks", n_copy, "programs that COPY it - these recompile"),
        ("files", len(writers) + len(readers),
         f"programs that open it ({len(writers)} can change it, "
         f"{len(readers)} read only)"),
        ("overrides", n_over, "CL programs that override a file to it"),
        ("logicals", n_logical, "logical files built over it"),
        ("screens", n_fmt_prog or n_device,
         "programs that write its record formats" if n_fmt_prog
         else "programs that drive this device file"),
        ("messages", n_msg, "places that send it"),
        ("dataareas", n_area, "members that use this data area"),
        ("fields", n_field_w + n_field_r,
         f"programs that use it as a field ({n_field_w} write, "
         f"{n_field_r} read)"),
        ("defines", defines, "message ids this member defines"),
    ]
    live = [(key, n, label) for key, n, label in layers if n]

    if want == "all":
        out.append("")
        out.append("  What reaches it:")
        out.append(_table(
            ["layer", "count", "what that means", "for the list"],
            [(key, f"{n:,}", label,
              f"kind='{key}'" if n > _ALL_INLINE_MAX else "below")
             for key, n, label in live]))
        if not live:
            out.append("    Nothing in the source reaches it.")
            out.append("    Not proof it is dead: a program can be started by "
                       "the job scheduler or a")
            out.append("    data queue, and a file can be read by SQL, a query "
                       "tool or a trigger -")
            out.append("    none of which is in the source. See "
                       "coverage_report.")

    def show(key: str) -> bool:
        """Expand this layer? Always when asked for; in 'all' only if short."""
        if want == key:
            return True
        return want == "all" and dict((k, n) for k, n, _ in live).get(
            key, 0) <= _ALL_INLINE_MAX

    # ---- the writers, always ---------------------------------------------
    #: Never folded away.  "What could have put this value here" is the
    #: question, and the answer is almost always a handful of programs even on
    #: a file two thousand programs read.
    if writers and want in ("all", "files"):
        _sections(out, f"PROGRAMS THAT CAN CHANGE IT ({len(writers)})",
                  _table(["program", "opened", "verbs"],
                         [(r["program"] + (" (!)" if r["contradicts"] else ""),
                           {"O": "write", "U": "update",
                            "I": "read"}.get(r["mode"], r["mode"]),
                           r["verbs"] or "") for r in writers], limit)
                  if len(writers) <= limit
                  else _fold([r["program"] for r in writers], limit))
        if any(r["contradicts"] for r in writers):
            out.append("    (!) opened INPUT yet issues a mutating verb. "
                       "Valid COBOL cannot do")
            out.append("    that, so it is listed here rather than dropped - "
                       "but read it before")
            out.append("    believing it. kb.index.consistency --check "
                       "write_on_input_open.")
        sites = _rows(
            "SELECT member, paragraph, verb, COUNT(*) n FROM io_site "
            "WHERE file=? AND verb IN ('WRITE','REWRITE','DELETE') "
            "GROUP BY member, paragraph, verb ORDER BY member LIMIT ?",
            name, limit)
        if sites:
            out.append("    and the paragraphs that do it:")
            out.append(_table(
                ["member", "paragraph", "verb", "times"],
                [(r["member"], r["paragraph"] or "", r["verb"], r["n"])
                 for r in sites], limit))

    if readers and show("files"):
        _sections(out, f"PROGRAMS THAT READ IT ({len(readers)})",
                  _fold([r["program"] for r in readers], limit))

    # ---- everything else --------------------------------------------------
    if n_calls and show("calls"):
        callers = _rows("SELECT DISTINCT caller FROM call_edge WHERE callee=? "
                        "ORDER BY caller", name)
        _sections(out, f"CALLED BY ({n_calls})",
                  _fold([r["caller"] for r in callers], limit))

    if n_copy and show("copybooks"):
        copiers = sorted(_closure, key=lambda r: r["program"])
        dds = sum(1 for r in copiers
                  if r["kind"] in ("record_layout", "device_layout",
                                   "generated_layout", "dds_all_formats"))
        via = sorted({r["copybook"] for r in copiers})
        heading = f"COPIED BY ({n_copy}"
        if n_copy > n_direct:
            heading += f" = {n_direct} direct + {n_copy - n_direct} through "
            heading += f"{len(_via)} intermediate copybook"
            heading += "s" if len(_via) != 1 else ""
        elif dds:
            heading += f", {dds} through COPY DDS-ALL-FORMATS"
        heading += ") - every one of them recompiles"
        _sections(out, heading,
                  _fold([r["program"] for r in copiers], limit))
        if _via:
            # Name the intermediates. A number that cannot be checked is a
            # number nobody trusts, and these are the members a hand-built
            # list misses every time.
            out.append("  Reached through: " + "  ".join(
                f"{k} ({v})" for k, v in sorted(_via.items(),
                                                key=lambda kv: -kv[1])))
        if len(family) > 1:
            # Say which name each program wrote, or the answer looks like it
            # came from somewhere other than the source.
            out.append(f"  Counted across the layout family: "
                       f"{', '.join(family)}.")
            if len(via) > 1:
                out.append("  " + "; ".join(
                    f"{v}: {sum(1 for r in copiers if r['copybook'] == v)}"
                    for v in via))
            out.append("  A layout change recompiles all of them, whichever "
                       "name the program copied.")

    if n_over and show("overrides"):
        overriders = _rows(
            "SELECT DISTINCT cl_program, logical_name, line, confidence "
            "FROM override WHERE target_file=? ORDER BY cl_program", name)
        _sections(out, f"CL PROGRAMS THAT OVERRIDE A FILE TO IT ({n_over})",
                  _table(["cl program", "overrides", "evidence", "confidence"],
                         [(r["cl_program"], r["logical_name"],
                           f"{r['cl_program']}:{r['line']}", r["confidence"])
                          for r in overriders], limit)
                  if len(overriders) <= limit
                  else _fold([r["cl_program"] for r in overriders], limit))
        out.append("  Hidden coupling: those programs name one file and open "
                   "this one.")

    if n_logical and show("logicals"):
        logicals = _rows("SELECT name, member_type FROM object WHERE based_on=? "
                         "ORDER BY name", name)
        _sections(out, f"LOGICAL FILES OVER IT ({n_logical})",
                  _table(["logical", "type"],
                         [(r["name"], r["member_type"]) for r in logicals],
                         limit))
        out.append("  A program reaching one of these is reaching this data "
                   "without naming this file.")

    if n_fmt_prog and show("screens"):
        w = _rows("SELECT program, verb, device_file, paragraph, line "
                  "FROM format_usage WHERE format_name=? ORDER BY program",
                  name)
        _sections(out, f"PROGRAMS THAT WRITE OR READ THIS FORMAT ({n_fmt_prog})",
                  _table(["program", "verb", "device file", "paragraph",
                          "evidence"],
                         [(r["program"], r["verb"], r["device_file"] or "?",
                           r["paragraph"] or "",
                           f"{r['program']}:{r['line']}") for r in w], limit))
    elif n_device and show("screens"):
        _sections(out, f"PROGRAMS THAT USE THIS DEVICE FILE ({n_device})",
                  _table(["program", "declares (ASSIGN)", "copies formats",
                          "format writes", "formats"],
                         [(p, "yes" if u["assign"] else "",
                           "yes" if u["copy"] else "",
                           u["writes"] or "",
                           ",".join(sorted(u["formats"]))[:44])
                          for p, u in sorted(device_users.items())], limit))
        out.append("  A program that declares or copies the file recompiles "
                   "when a field changes;")
        out.append("  a change to constant text only needs the file "
                   "recreated.")
    if show("screens") and not n_device and _is_device_file(name):
        out.extend(_no_device_users(name))

    if n_msg and show("messages"):
        senders = _rows("SELECT program, paragraph, COUNT(*) n, MIN(line) line "
                        "FROM message_usage WHERE msgid=? "
                        "GROUP BY program, paragraph ORDER BY program", name)
        _sections(out, f"SENT FROM ({len(senders)} program/paragraph site(s))",
                  _table(["program", "paragraph", "times", "evidence"],
                         [(r["program"], r["paragraph"] or "", r["n"],
                           f"{r['program']}:{r['line']}") for r in senders],
                         limit)
                  if len(senders) <= limit
                  else _fold([r["program"] for r in senders], limit))

    if defines and show("defines"):
        #: A message file has no inbound edge of its own - nothing COPYs or
        #: CALLs it.  What reaches it is the programs that send the ids it
        #: defines, which is why it looked like an unreferenced member until
        #: `member` was recorded on the message table.
        senders = _rows(
            "SELECT m.msgid, m.text, "
            "  (SELECT COUNT(DISTINCT u.program) FROM message_usage u "
            "     WHERE u.msgid = m.msgid) programs, m.line "
            "FROM message m WHERE m.member=? ORDER BY m.msgid", name)
        used = sum(1 for r in senders if r["programs"])
        _sections(out, f"MESSAGE IDS IT DEFINES ({defines}, {used} of them "
                       f"actually sent)",
                  _table(["msgid", "text", "programs", "evidence"],
                         [(r["msgid"], (r["text"] or "")[:44], r["programs"],
                           f"{name}:{r['line']}") for r in senders], limit))
        out.append("  A message file is reached through its ids, never by "
                   "name - no program COPYs")
        out.append("  or CALLs it. An id with 0 programs is dead text.")

    if n_area and show("dataareas"):
        holders = _rows("SELECT DISTINCT member FROM data_area WHERE name=? "
                        "ORDER BY member", name)
        _sections(out, f"MEMBERS THAT USE THIS DATA AREA ({n_area})",
                  _fold([r["member"] for r in holders], limit))
        out.append("  Shared mutable state with no schema. Everything above is "
                   "coupled through")
        out.append("  it and the source does not say who writes and who reads.")

    if (n_field_w or n_field_r) and show("fields"):
        users = _rows("SELECT program, direction, paragraphs, file, "
                      "MIN(" + _FIRST_LINE + ") l "
                      "FROM field_usage WHERE field=? "
                      # `file` is grouped, not bare.  SQLite allowed the bare
                      # column and picked an arbitrary row; PostgreSQL raises
                      # 42803.  Grouping it is also the honest answer - one row
                      # per distinct usage is what this table already stores.
                      "GROUP BY program, direction, paragraphs, file "
                      "ORDER BY direction DESC, program LIMIT ?", name, limit)
        _sections(out, f"AS A FIELD ({n_field_w} program(s) write it, "
                       f"{n_field_r} read it)",
                  _table(["program", "direction", "paragraph", "on file",
                          "evidence"],
                         [(r["program"], r["direction"],
                           (r["paragraphs"] or "").split(",")[0],
                           r["file"] or "", f"{r['program']}:{r['l']}")
                          for r in users], limit))
        out.append("  where_is_field_used gives the field's definition and "
                   "valid values too.")

    if want == "all" and live:
        #: The framing impact_of_change used to carry.  That tool was a subset
        #: of this one with a worse presentation, so it went - but the question
        #: it was named for is the one people actually ask, and the answer has
        #: to be visible here or it is lost.
        blast = []
        if n_copy:
            blast.append(f"{n_copy} member(s) recompile")
        if writers:
            blast.append(f"{len(writers)} can change its data")
        if n_over:
            blast.append(f"{n_over} CL override to it")
        if n_logical:
            blast.append(f"{n_logical} logical(s) read through it")
        if blast:
            out.append("")
            out.append("  If you change it: " + "; ".join(blast) + ".")
            out.append("  None of that is a compile-time list on IBM i - "
                       "nothing checks it for you.")

        out.append("")
        if any(n > _ALL_INLINE_MAX for _, n, _ in live):
            out.append("  The layers marked kind='...' were too big to print. "
                       "Ask for one directly,")
            out.append("  or raise limit to get the members rather than the "
                       "groups.")
        out.append("  what_it_touches goes the other way: what this member "
                   "reaches.")
    return "\n".join(out)


# ===========================================================================
# Graph-only tools.  No SQLite counterpart - these are the questions the
# relational store could not answer without the caller stitching several
# queries together, which is where counting errors come from.
# ===========================================================================

#: Which table holds each kind of edge, and how it is directed.
#:
#: `path_between` and `menu_path_to` used to answer a socket error when no
#: graph was configured - the same dead-end `blast_radius` had. A packaged
#: build ships no graph at all, so on the deployment that matters those tools
#: could never answer anything. A relational walk is slower on deep questions
#: and it is not a claim to replace a graph; it is a claim that a degraded
#: answer, labelled as degraded, beats an error.
_EDGE_TABLES = {
    "calls": ("call_edge", "caller", "callee"),
    "copies": ("copy_edge", "program", "copybook"),
    "files": ("file_usage", "program", "file"),
}


def _walk_relational(start: str, target: str, table: str, src: str, dst: str,
                     max_hops: int) -> "List[List[str]]":
    """Shortest paths from `start` to `target`, breadth first.

    Breadth first because the question is "how does this reach that", and the
    shortest route is the one somebody will read. Deeper routes exist and are
    not more informative.
    """
    if start == target:
        return [[start]]
    seen = {start}
    frontier = [[start]]
    for _hop in range(max(1, max_hops)):
        nxt: List[List[str]] = []
        found: List[List[str]] = []
        heads = [p[-1] for p in frontier]
        if not heads:
            break
        for part, marks in _in_chunks(heads):
            rows = _rows(f"SELECT DISTINCT {src} AS s, {dst} AS d "
                         f"FROM {table} WHERE {src} IN ({marks})", *part)
            by_head: Dict[str, List[str]] = defaultdict(list)
            for r in rows:
                by_head[r["s"]].append(r["d"])
            for path in frontier:
                for nxt_name in by_head.get(path[-1], ()):
                    if nxt_name == target:
                        found.append(path + [nxt_name])
                    elif nxt_name not in seen:
                        seen.add(nxt_name)
                        nxt.append(path + [nxt_name])
        if found:
            return found
        frontier = nxt
    return []


def _path_between_relational(frm: str, to: str, edges: str,
                             max_hops: int, max_paths: int) -> str:
    frm, to = frm.strip().upper(), to.strip().upper()
    hops = max_hops or 6
    kinds = ([edges] if edges in _EDGE_TABLES
             else list(_EDGE_TABLES))
    out = ([f"How {frm} reaches {to}:"]
           + ([STORE_NOTE] if STORE_NOTE else []) + [""])
    any_found = False
    for kind in kinds:
        table, src, dst = _EDGE_TABLES[kind]
        paths = _walk_relational(frm, to, table, src, dst, hops)
        if not paths:
            continue
        any_found = True
        out.append(f"  via {kind} ({len(paths)} shortest route"
                   f"{'s' if len(paths) != 1 else ''}, "
                   f"{len(paths[0]) - 1} hop"
                   f"{'s' if len(paths[0]) != 2 else ''}):")
        for path in paths[:max_paths or 8]:
            out.append("    " + " -> ".join(path))
        if len(paths) > (max_paths or 8):
            out.append(f"    ... and {len(paths) - (max_paths or 8)} more")
    if not any_found:
        out.append(f"  No route from {frm} to {to} in the source, "
                   f"within {hops} hops.")
        out.append("  That is not proof there is none. A job scheduler, an "
                   "operator running both, or")
        out.append("  a CALL through a variable is invisible here - and a "
                   "library that was never")
        out.append("  extracted answers with silence. Call coverage_report.")
    return chr(10).join(out)


def _menu_path_to_relational(name: str, depth: int) -> str:
    """Which menu options reach this program, directly or through a driver."""
    name = name.strip().upper()
    reached = {name}
    layer = {name}
    chains: Dict[str, List[str]] = {name: [name]}
    for _hop in range(max(1, depth)):
        if not layer:
            break
        nxt = set()
        for part, marks in _in_chunks(sorted(layer)):
            for r in _rows(f"SELECT DISTINCT caller, callee FROM call_edge "
                           f"WHERE callee IN ({marks})", *part):
                if r["caller"] not in reached:
                    reached.add(r["caller"])
                    chains[r["caller"]] = [r["caller"]] + chains[r["callee"]]
                    nxt.add(r["caller"])
        layer = nxt

    rows = []
    for part, marks in _in_chunks(sorted(reached)):
        rows.extend(_rows(
            f"SELECT menu, library, option, label, target FROM menu_option "
            f"WHERE target IN ({marks}) ORDER BY menu, option", *part))
    out = ([f"How a user reaches {name}:"]
           + ([STORE_NOTE] if STORE_NOTE else []) + [""])
    if not rows:
        out.append(f"  No menu option reaches {name} within {depth} call "
                   f"hop(s).")
        out.append("  It may be reached by a job, by another program, or by "
                   "an operator - none of")
        out.append("  which is in the source. Absence here is not evidence "
                   "of absence.")
        return chr(10).join(out)
    out.append(_table(["menu", "option", "label", "runs", "then reaches"],
                      [(f"{r['library']}/{r['menu']}" if r["library"]
                        else r["menu"],
                        str(r["option"]), (r["label"] or "")[:34],
                        r["target"],
                        " -> ".join(chains.get(r["target"], [r["target"]])))
                       for r in rows], 40))
    return chr(10).join(out)


def path_between(frm: str, to: str, edges: str = "calls",
                 max_hops: int = 0, max_paths: int = 8) -> str:
    """How one member reaches another - every shortest route, each hop named.

    Was `call_path`, and the rename is the point: it followed CALLS and
    nothing else, so it answered "no path" about pairs with three real
    dependencies between them. ORD100 reaches CUSMAS by opening it, by
    COPYing it and by doing I/O on it, and the old tool saw none of that.
    """
    # No graph, or a graph that will not answer, is not a reason to
    # fail. See _path_between_relational / _menu_path_to_relational.
    try:
        from kb.graph import backend
        if backend.degraded() or backend.mode() == "sqlite":
            return _path_between_relational(frm, to, edges, max_hops, max_paths)
    except Exception:                                     # noqa: BLE001
        return _path_between_relational(frm, to, edges, max_hops, max_paths)
    from kb.graph import queries

    want = "all" if str(edges).strip().lower() == "all" else "calls"
    try:
        rows = queries.path_between(frm, to, edges=want,
                                    max_hops=(max_hops or None),
                                    max_paths=max_paths)
    except Exception:                                     # noqa: BLE001
        return _path_between_relational(frm, to, edges, max_hops, max_paths)

    a, b = frm.strip().upper(), to.strip().upper()
    if not rows:
        # Say what was searched, so "no path" is a fact about a bound the
        # caller can see rather than one it cannot. The old message named
        # neither the hop limit nor the edge types.
        hops = queries._hops(max_hops or None)
        tried = ("CALL edges only" if want == "calls"
                 else f"all {len(queries.MEMBER_EDGES)} member-to-member "
                      f"edge types")
        out = [f"No route from {a} to {b} within {hops} hop(s), following "
               f"{tried}."]
        if want == "calls":
            out.append("  Try edges='all': a member can reach another by "
                       "opening it, COPYing it,")
            out.append("  overriding a file to it or running it from a menu, "
                       "and none of those")
            out.append("  is a CALL.")
        out.append("  This is also not proof there is none. A CALL through a "
                   "variable is recorded")
        out.append("  as unresolved and cannot be followed - see "
                   "coverage_report.")
        return "\n".join(out)

    hops = int(rows[0].get("max_hops") or 0)
    shortest = min(int(r["hops"]) for r in rows)
    out = [f"{a} reaches {b} - {len(rows)} route(s), shortest is "
           f"{shortest} hop(s):", ""]

    body = []
    for r in rows:
        route, kinds = list(r["route"]), list(r.get("kinds") or [])
        # Spell the route out with the edge type between each pair, because
        # "ORD100 -> CUSMAS" means something different depending on whether
        # that arrow is a CALL, an OPEN or a COPY, and a route that does not
        # say which cannot be acted on.
        parts = [route[0]]
        for i, kind in enumerate(kinds):
            parts.append(f"-[{kind}]->")
            parts.append(route[i + 1] if i + 1 < len(route) else "?")
        body.append((str(r["hops"]), " ".join(parts)))
    out.append(_paged_table(["hops", "route"], body, 1, size=max(1, max_paths)))

    if len(rows) > 1:
        out.append("")
        out.append(f"  All {len(rows)} are the same length, so none of them is "
                   f"the route - they")
        out.append("  are alternatives. A change that travels one of them may "
                   "not travel the")
        out.append("  others, and which one matters depends on the edge types "
                   "above.")
    if want == "calls":
        out.append("  CALL edges only. edges='all' also follows opens, "
                   "COPYs, overrides,")
        out.append("  logical files, menu options, SQL and I/O.")
    out.append(f"  Searched to {hops} hop(s); routes longer than the shortest "
               f"are not listed.")
    return "\n".join(out)


def _blast_radius_relational(name: str, depth: int) -> str:
    """The seven counts, from the relational store.

    `blast_radius` used to need a graph, and answered a socket error when
    there was none - four times in a row on one analysis, while the relational
    store could have answered the whole time. The documentation calls this the
    primary impact tool, so it must never dead-end: a tool that fails once
    gets worked around silently ever after.

    The graph is faster at deep transitive walks and this is not a claim to
    replace it. It is a claim that a degraded answer, labelled as degraded,
    beats an error.
    """
    name = name.strip().upper()
    if not _rows("SELECT 1 FROM object WHERE name=? LIMIT 1", name):
        obj = (_rows("SELECT 1 FROM data_member WHERE name=? AND "
                     "is_file_object=1 LIMIT 1", name)
               # A data file known from its CL - the only evidence a
               # source-only extraction has - has a blast radius too.
               or _rows("SELECT 1 FROM file_provenance WHERE target=? LIMIT 1",
                        name)
               or _rows("SELECT 1 FROM override WHERE target_file=? LIMIT 1",
                        name)
               or _sql_tables("name=?", name))
        if not obj:
            return (f"{name} is not in the index - no source member and no "
                    f"file object.\n  Call find_object for what that "
                    f"distinction means, and coverage_report for the "
                    f"libraries\n  that contribute nothing.")

    family = file_family(name)
    marks = ",".join("?" * len(family))
    one = lambda sql, *a: _rows(sql, *a)[0]["c"]

    closure, via = _copy_closure(family)
    n_copy = len({r["program"] for r in closure})
    n_direct = one(f"SELECT COUNT(DISTINCT program) c FROM copy_edge "
                   f"WHERE copybook IN ({marks})", *family)

    counts = [
        ("callers", one("SELECT COUNT(DISTINCT caller) c FROM call_edge "
                        "WHERE callee=?", name),
         "programs that CALL it, one hop"),
        ("callees", one("SELECT COUNT(DISTINCT callee) c FROM call_edge "
                        "WHERE caller=?", name),
         "programs it calls, one hop"),
        ("recompile", n_copy,
         f"members that COPY the layout"
         + (f" ({n_direct} direct + {n_copy - n_direct} through "
            f"{len(via)} intermediate)" if n_copy > n_direct else "")),
        ("logicals", one("SELECT COUNT(*) c FROM object WHERE based_on=?",
                         name),
         "logical files over it"),
        ("writers", one(f"SELECT COUNT(DISTINCT program) c FROM file_usage "
                        f"WHERE file IN ({marks}) AND mode IN ('O','U','A')",
                        *family),
         "programs that can change it"),
        ("readers", one(f"SELECT COUNT(DISTINCT program) c FROM file_usage "
                        f"WHERE file IN ({marks}) AND mode = 'I'", *family),
         "programs that read it"),
        ("checks only", one(f"SELECT COUNT(DISTINCT program) c FROM "
                            f"file_usage WHERE file IN ({marks}) AND "
                            f"mode = 'C'", *family),
         "CHKOBJ - the object must exist, nothing is read"),
        ("overrides", one("SELECT COUNT(*) c FROM override WHERE "
                          "target_file=?", name),
         "OVRDBF statements pointing at it"),
    ]

    out = ([f"What a change to {name} could reach:"]
           + ([STORE_NOTE] if STORE_NOTE else []) + [""])
    out.append(_table(["layer", "count", "what that means"],
                      [(k, f"{v:,}", w) for k, v, w in counts]))
    out.append("")
    out.append("  These are COUNTS. Turn the ones that matter into NAMES:")
    out.append(f"    what_uses({name}, kind='copybooks', limit=4000)   "
               f"the recompile set")
    out.append(f"    who_touches_file({name}, mode='write')            "
               f"what can change it")
    out.append(f"    resolve_files({name})                             "
               f"the physical files behind it")
    if len(family) > 1:
        out.append(f"  Counted across the layout family: "
                   f"{', '.join(family)}.")
    out.extend(_unfollowable(name))
    out.extend(_program_coupling(name))
    return "\n".join(out)


def _unfollowable(name: str) -> List[str]:
    """Calls out of this member whose target could not be read.

    `callees` counts distinct names, and an unresolved call HAS no name - so
    a program with three call sites, one of them through a variable set at
    run time, reported "callees 2" and said nothing about the third. The
    count was right and the answer was wrong: a reader takes a blast radius
    for the whole reach, and the site nobody can follow is exactly the one
    worth knowing about.

    Unresolvable in principle, not a defect to fix: `CALL WS-PGM` where the
    variable is filled at run time cannot be resolved from source by anything,
    including a person. The remedy is to say so.
    """
    rows = _rows("SELECT line, variable, paragraph FROM call_edge "
                 "WHERE caller=? AND (callee IS NULL OR callee='') "
                 "ORDER BY line", name)
    if not rows:
        return []
    out = ["", f"  NOT FOLLOWED - {len(rows)} call site(s) out of {name} whose "
                "target could not be read:"]
    for row in rows[:12]:
        where = f" in {row['paragraph']}" if row["paragraph"] else ""
        through = (f"through {row['variable']}" if row["variable"]
                   else "target not in the source")
        out.append(f"    {name}:{row['line']}{where}  -  {through}")
    if len(rows) > 12:
        out.append(f"    ... and {len(rows) - 12} more")
    out.append("    These are NOT in the counts above. Whatever they reach is "
               "outside this radius,")
    out.append("    and no tool can say what it is - the target is decided at "
               "run time. Put them in")
    out.append("    the report's open items, with these line numbers, rather "
               "than leaving the")
    out.append("    reader to assume the radius is complete.")
    return out


#: Member languages whose own COPY and USING couple them to other members.
_PROGRAM_LANGUAGES = ("cobol", "cl", "rpg")


def _program_coupling(name: str) -> List[str]:
    """What a change INSIDE a program reaches besides its callers.

    The counts above answer "what uses this name". A program is also coupled
    through what it copies: a copybook it shares is edited for everyone that
    copies it, and its LINKAGE record is the other half of every caller's
    parameter. Neither shows up as a caller or a copier of the program itself,
    which is how a change to one program's copybook came back as reaching
    nothing but its callers.
    """
    who = _rows("SELECT language FROM object WHERE name=?", name)
    if not any(w["language"] in _PROGRAM_LANGUAGES for w in who):
        return []
    out: List[str] = []

    copied = _rows(
        "SELECT c.copybook, COUNT(DISTINCT o.program) AS others "
        "FROM copy_edge c LEFT JOIN copy_edge o "
        "  ON o.copybook = c.copybook AND o.program <> c.program "
        "WHERE c.program=? AND c.kind='copybook' "
        "GROUP BY c.copybook ORDER BY others DESC, c.copybook", name)
    try:
        params = _rows("SELECT position, name, bytes, via FROM linkage_param "
                       "WHERE program=? ORDER BY position", name)
        cl_callers = _rows("SELECT DISTINCT caller FROM call_argument "
                           "WHERE callee=? ORDER BY caller", name)
        mismatches = _rows("SELECT COUNT(*) c FROM param_mismatch "
                           "WHERE callee=? OR caller=?", name, name)[0]["c"]
    except sqlite3.OperationalError:
        params, cl_callers, mismatches = [], [], 0
    linkage_books = sorted({b for p in params for b in (p["via"] or "").split(",") if b})

    if copied:
        out.append("")
        out.append(f"  COPIES: {name} copies {len(copied)} copybook(s). A change to "
                   f"one recompiles every member copying it:")
        out.append(_table(
            ["copybook", "other members copying it", "role"],
            [(r["copybook"], f"{r['others']:,}",
              "LINKAGE record" if r["copybook"] in linkage_books else "")
             for r in copied], 25))
        out.append(f"    what_uses(<copybook>, kind='copybooks', limit=4000)   "
                   f"names them")

    if params:
        cobol_callers = _rows("SELECT DISTINCT caller FROM call_edge WHERE "
                              "callee=? AND kind='cobol' ORDER BY caller", name)
        sizes = ", ".join(f"{p['name']} "
                          f"{'?' if p['bytes'] is None else format(p['bytes'], ',')}"
                          for p in params)
        out.append("")
        out.append(f"  LINKAGE CLOSURE: {len(params)} parameter(s) ({sizes} bytes).")
        out.append(f"    A change to their layout also changes: {len(cl_callers)} CL "
                   f"caller(s) passing them (DCL LEN), {len(cobol_callers)} COBOL "
                   f"caller(s) (CALL USING)"
                   + (f", and every member copying {', '.join(linkage_books)}"
                      if linkage_books else "") + ".")
        if mismatches:
            out.append(f"    {mismatches} size mismatch(es) already exist.")
        out.append(f"    program_interface({name})   the callers, sizes and "
                   f"mismatches")
    return out


@table_aware("name", "blast_radius")
def blast_radius(name: str, depth: int = 3) -> str:
    """Everything a change to one member could reach, in one query.

    Six separate SQLite queries the caller had to combine.  Combining them was
    where the arithmetic went wrong in the ticket test, so the join is the
    database's job now.
    """
    # No graph, or a graph that will not answer, is not a reason to fail.
    # See _blast_radius_relational.
    try:
        from kb.graph import backend, driver
        if backend.degraded() or backend.mode() == "sqlite":
            return _blast_radius_relational(name, depth)
    except Exception:                                     # noqa: BLE001
        return _blast_radius_relational(name, depth)
    from kb.graph import queries
    try:
        b = queries.blast_radius(name, depth)
    except Exception:                                     # noqa: BLE001
        return _blast_radius_relational(name, depth)
    if not b:
        return chr(10).join([
            f"No member called {name.upper()} in the index.",
            f"  find_object({name.upper()}) says whether the name is "
            f"a physical file or an override alias."])

    name = name.upper()
    out = [f"What a change to {name} could reach:", ""]
    out.append(_table(
        ["layer", "count", "what that means"],
        [("callers", str(b.get("callers", 0)),
          f"programs that reach it, up to {depth} hop(s)"),
         ("callees", str(b.get("callees", 0)), "programs it reaches"),
         ("recompile", str(b.get("recompile", 0)),
          "members that COPY it - a change means a rebuild"),
         ("logicals", str(b.get("logicals", 0)),
          "logical files over it - they never name it in source"),
         ("writers", str(b.get("writers", 0)),
          "programs that can CHANGE its data"),
         ("readers", str(b.get("readers", 0)), "programs that only read it"),
         ("overrides", str(b.get("overrides", 0)),
          "CL drivers that redirect it - library is inferred, not exact")]))
    for label, key in (("WRITERS", "writer_names"),
                       ("LOGICALS", "logical_names"),
                       ("OVERRIDDEN BY", "override_names")):
        names = [x for x in (b.get(key) or []) if x]
        if names:
            out.append("")
            out.append(f"  {label}: {', '.join(sorted(names))}")
    out.append("")
    out.append("  Every override library above is inferred - it comes from a "
               "CL variable at run time.")
    out.append("  resolve_overrides on a writer shows which one.")
    out.extend(_program_coupling(name))
    return "\n".join(out)


def menu_path_to(name: str, depth: int = 4) -> str:
    """How a user reaches this program from a menu.

    The question a business user actually asks - "where do I click to get
    here" - and it was unanswerable until menu options were indexed.
    """
    # No graph, or a graph that will not answer, is not a reason to
    # fail. See _path_between_relational / _menu_path_to_relational.
    try:
        from kb.graph import backend
        if backend.degraded() or backend.mode() == "sqlite":
            return _menu_path_to_relational(name, depth)
    except Exception:                                     # noqa: BLE001
        return _menu_path_to_relational(name, depth)
    from kb.graph import queries
    try:
        rows = queries.menu_path_to(name, depth)
    except Exception:                                     # noqa: BLE001
        return _menu_path_to_relational(name, depth)
    if not rows:
        return (f"No menu reaches {name.upper()} within {depth} hop(s).\n"
                f"  It may be called only by another program, or started by "
                f"the job scheduler - which is not in the source.")
    # Shortest route per entry menu.
    #
    # Every submenu carries a "Return to the main menu" option, so APMAIN
    # reaches SEC407 both as option 3 and as option 11 followed by option 3.
    # Both are real - the second is two keystrokes and the route names both -
    # but a user standing on APMAIN presses 3, and printing the longer one
    # beside it invites an agent to quote either.
    #
    # Keeping the shortest PER MENU rather than overall is the point: from
    # APMAIN01 the two-keystroke route through APMAIN genuinely is how you get
    # there, and that row survives. Only the longer route from a menu that
    # also has a shorter one is dropped.
    shortest: Dict[str, int] = {}
    for r in rows:
        key = str(r.get("entry_menu") or "")
        span = int(r.get("hops") or 0) + int(r.get("call_hops") or 0)
        if key not in shortest or span < shortest[key]:
            shortest[key] = span
    rows = [r for r in rows
            if int(r.get("hops") or 0) + int(r.get("call_hops") or 0)
            == shortest.get(str(r.get("entry_menu") or ""), 0)]
    rows.sort(key=lambda r: (int(r.get("hops") or 0),
                             str(r.get("entry_menu") or "")))

    out = [f"How a user reaches {name.upper()}:", ""]
    body = []
    indirect = 0
    for r in rows:
        opts = " > ".join(str(o) for o in (r.get("options") or []))
        labels = " > ".join(x for x in (r.get("labels") or []) if x)
        # What the option actually starts, and how the program is reached from
        # it.  Almost always a CL driver: the option runs SEC407C and SEC407C
        # calls SEC407, so "runs" is what a user's keystroke starts and the
        # arrow chain is what happens next.  Reported rather than flattened,
        # because "option 3 runs this" and "option 3 runs a driver that calls
        # this" are different facts and the second is the common one.
        via = [v for v in (r.get("via") or []) if v]
        chain = " -> ".join(via) if len(via) > 1 else (r.get("runs") or "")
        if len(via) > 1:
            indirect += 1
        body.append((r["entry_menu"], str(r["hops"]), opts, labels, chain))
    out.append(_table(["entry menu", "hops", "options", "labels", "then"],
                      body, 30))
    if indirect:
        out.append("  The 'then' column is what the option starts and what "
                   "that calls. A menu")
        out.append("  option runs a CL driver, not a program directly, so the "
                   "driver is the")
        out.append("  member the option names and the program is one CALL "
                   "further on.")
    return "\n".join(out)


# ===========================================================================
# Routing and similarity over the written pages
#
# The pyramid, the embeddings and the RRF fuser all existed before these two
# handlers did, and no agent could reach any of it: `routing.route` was called
# in exactly one place, as a fixed four-line prefix inside `trace_ticket`, with
# no way to ask for more, no way to ask the area question on its own, and no
# way to get at similarity at all.
#
# These are the two questions the written pages can answer that the facts
# cannot:
#
#   route_ticket   "which part of the application is this about"
#   find_similar   "what else looks like this one"
#
# Both read PROSE a model wrote, not facts read from source, and both say so in
# their output. That distinction is Rule 1 and it does not stop mattering
# because the text is useful.
# ===========================================================================

def route_ticket(text: str, limit: int = 6) -> str:
    """Which menu, area or library a ticket is about.

    `trace_ticket` answers "which member"; this answers "which part of the
    application", which is a different question and often the one to ask first
    - a ticket that names no identifier still names a business area.

    Hybrid: bm25 (or tsvector, on PostgreSQL) over the page text, fused with
    cosine over the page vectors by Reciprocal Rank Fusion. Either half can be
    absent. The sparse half needs only the index; the dense half needs the
    embedding model reachable, because the QUERY has to be embedded with the
    same model the pages were. When it is not, this degrades to sparse and says
    so rather than pretending.
    """
    text = (text or "").strip()
    if not text:
        return ("route_ticket needs the ticket text. Give it the whole "
                "description in plain business language.")
    try:
        limit = max(1, min(int(limit or 6), 25))
    except (TypeError, ValueError):
        limit = 6

    from . import routing
    hits, note = routing.route(db(), text, limit=limit)
    if not hits:
        pages = _rows("SELECT COUNT(*) n FROM summary")
        if not pages or not pages[0]["n"]:
            return ("No written pages exist yet, so there is nothing to route "
                    "against. The facts are unaffected - use trace_ticket, "
                    "find_object or search_source.\n"
                    "  build the pages with: python -m kb summarise")
        return routing.render(hits, note)
    return routing.render(hits, note)


def find_similar(name: str, limit: int = 8) -> str:
    """Written pages that read like this one's.

    Cosine over the STORED vectors, so unlike `route_ticket` this needs no
    embedding model at query time: both sides of the comparison were embedded
    when the pages were written. It works whenever the vectors exist.

    Useful for "this menu is wrong, what else is like it" - the pages that
    describe similar work are where a defect of the same shape tends to live.
    """
    key = (name or "").strip().upper()
    if not key:
        return "find_similar needs a menu, area or library name."
    try:
        limit = max(1, min(int(limit or 8), 25))
    except (TypeError, ValueError):
        limit = 8

    rows = _rows("SELECT scope, key, library FROM embedding "
                 "WHERE UPPER(key)=? ORDER BY scope, library", key)
    if not rows:
        has_page = _rows("SELECT scope FROM summary WHERE UPPER(key)=? LIMIT 1",
                         key)
        if has_page:
            return (f"{key} has a written page but no vector, so similarity "
                    f"cannot be computed for it.\n"
                    f"  embed the pages with: python -m kb.summarise.vectors")
        pages = _rows("SELECT COUNT(*) n FROM summary")
        if not pages or not pages[0]["n"]:
            return ("No written pages exist yet, so nothing can be similar to "
                    "anything.\n  build them with: python -m kb summarise")
        return (f"No written page for {key}, so there is no vector to compare. "
                f"Pages exist for menus, areas and libraries - not for every "
                f"member. Try read_summary or find_object.")

    from . import semantic
    ok, why = semantic.available(db())
    if not ok:
        return f"{key}: {why}"

    scope, skey, library = rows[0]["scope"], rows[0]["key"], rows[0]["library"]
    found, why = semantic.similar_to(scope, skey, library, limit=limit)
    if why:
        return f"{key}: {why}"
    if not found:
        return f"Nothing else is close to {key}."

    out = [f"PAGES LIKE {scope} {skey}" + (f"  in {library}" if library else ""),
           ""]
    width = max((len(h.key) for h in found), default=8)
    for hit in found:
        score = list(hit.found_by.values())[0][1]
        where = f"  {hit.scope:<9}{hit.key:<{width + 2}}{score:.3f}"
        out.append(where + (f"  {hit.library}" if hit.library else ""))
        if hit.preview:
            out.append(f"      {hit.preview[:150]}")
    out.append("")
    out.append("  Similarity is between DESCRIPTIONS a model wrote, not "
               "between code. Two")
    out.append("  pages can read alike and share nothing - confirm with "
               "what_it_touches.")
    return "\n".join(out)


# ===========================================================================
# 25. unaccounted
# ===========================================================================

def unaccounted(text: str = "") -> str:
    """Which members the tools returned that the report never placed.

    Thin by design: the ledger and the subtraction live in
    kb/server/accounting.py, which the MCP server feeds from its own response
    path. This is only the door the agent knocks on.
    """
    from kb.server import accounting
    return accounting.report(text or "")
