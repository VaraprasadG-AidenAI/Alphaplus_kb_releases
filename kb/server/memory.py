"""
Ticket memory - what earlier analyses established, kept across sessions.

    remember(ticket, kind, subject, statement, ...)   record one fact
    recall(query | subject | ticket, ...)             find what was recorded

## Why this exists

An impact analysis learns things the index cannot hold: that a field was
widened to 100 by one ticket, that a member was ruled out of another ticket's
edit set and why, that a question was put to the business and answered. The
next ticket touching the same field starts from nothing and can contradict the
last one without anyone noticing. This store keeps those facts, and every
`remember` answers with what earlier tickets said about the same subject, so a
contradiction surfaces at the moment it is written.

## Why SQLite with FTS5, and not a vector store

What links two tickets is an exact identifier - a member, a field, a physical
file - and keyword ranking (BM25) finds exact identifiers; embeddings are weak
at them. SQLite and FTS5 ship with Python and are already inside the build,
need no service, no network and no model download, and a client network grants
none of those without a change request.

## Where it lives

In the per-user settings folder, not the index folder: re-indexing replaces
`facts.db` and switching to another release changes the index folder, and
neither may erase memory. Each fact records the source tree and the index
build it was established against, because a fact about one release may not
hold in the next; `recall` marks facts from another tree or an older build.

Override the location with ALPHAPLUS_MEMORY_DB or `[memory] path` in
alphaplus.toml.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

from kb import bootstrap, config

#: What a fact is about. A closed set, so that `recall(kind=...)` means the
#: same thing in every session and a typo is rejected rather than stored.
KINDS: Dict[str, str] = {
    "edit": "a member the ticket edits",
    "recompile": "a member the ticket recompiles without editing",
    "convert": "a physical file whose data is converted",
    "field": "a field's width, type or meaning",
    "decision": "a scoping or design decision, with its reason",
    "finding": "a defect, risk or inconsistency found",
    "question": "a question put to the ticket owner, and its answer when known",
    "change": "a change implemented in the source",
}

CONFIDENCE = ("exact", "inferred", "unresolved")

MAX_SUBJECT = 120
MAX_STATEMENT = 2000
MAX_EVIDENCE = 500
MAX_TICKET = 80
MAX_LIBRARY = 30

#: Characters that stay inside one token, so AA-MAILADR, F_FILE1 and $CRTF are
#: each one searchable word rather than two or three.
_TOKENIZER = "unicode61 tokenchars '-_$#@'"
#: A search term: letters in any script (German text carries umlauts), digits
#: and the characters identifiers use.
_TERM = re.compile(r"[\w$#@-]+")

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    id            INTEGER PRIMARY KEY,
    ticket        TEXT NOT NULL,
    kind          TEXT NOT NULL,
    subject       TEXT NOT NULL,
    library       TEXT NOT NULL DEFAULT '',
    statement     TEXT NOT NULL,
    evidence      TEXT NOT NULL DEFAULT '',
    confidence    TEXT NOT NULL DEFAULT 'exact',
    source_root   TEXT NOT NULL DEFAULT '',
    index_built   TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    superseded_by INTEGER
);
CREATE INDEX IF NOT EXISTS ix_memory_subject ON memory(subject);
CREATE INDEX IF NOT EXISTS ix_memory_ticket ON memory(ticket);
"""

FTS_SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    subject, statement, evidence,
    content='memory', content_rowid='id',
    tokenize="{_TOKENIZER}"
);
CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
    INSERT INTO memory_fts(rowid, subject, statement, evidence)
    VALUES (new.id, new.subject, new.statement, new.evidence);
END;
CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, subject, statement, evidence)
    VALUES ('delete', old.id, old.subject, old.statement, old.evidence);
END;
CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE OF subject, statement, evidence ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, subject, statement, evidence)
    VALUES ('delete', old.id, old.subject, old.statement, old.evidence);
    INSERT INTO memory_fts(rowid, subject, statement, evidence)
    VALUES (new.id, new.subject, new.statement, new.evidence);
END;
"""

_LOCK = threading.Lock()
_CONNECTIONS: Dict[str, Tuple[sqlite3.Connection, bool]] = {}


# ===========================================================================
# Storage
# ===========================================================================

def memory_path() -> Path:
    """Where ticket memory is stored. Read on every call, so a test or a
    re-pointed process uses the location it set."""
    configured = config.setting("ALPHAPLUS_MEMORY_DB", "memory", "path",
                                default="")
    if configured:
        return Path(str(configured))
    return bootstrap.settings_dir() / "ticket-memory.db"


def _connect() -> Tuple[sqlite3.Connection, bool]:
    """(connection, full-text search available) for the current location.

    One connection per location, shared across calls under a lock: the MCP
    server handles one request at a time, and several server processes (one
    per Claude session) coordinate through SQLite's own locking, which WAL
    mode and the busy timeout make safe for this write volume.
    """
    path = memory_path()
    key = str(path)
    cached = _CONNECTIONS.get(key)
    if cached is not None:
        return cached
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass                                   # a network share may refuse WAL
    conn.executescript(SCHEMA)
    try:
        conn.executescript(FTS_SCHEMA)
        has_fts = True
    except sqlite3.OperationalError:
        # An SQLite built without FTS5. Search falls back to LIKE: slower and
        # unranked, but every fact is still found.
        has_fts = False
    conn.commit()
    _CONNECTIONS[key] = (conn, has_fts)
    return conn, has_fts


def close_all() -> None:
    """Close every cached connection (tests, and a re-pointed process)."""
    with _LOCK:
        for conn, _ in _CONNECTIONS.values():
            try:
                conn.close()
            except sqlite3.Error:
                pass
        _CONNECTIONS.clear()


def _index_context() -> Tuple[str, str]:
    """(source tree, index build time) of the index this server answers from."""
    built = ""
    try:
        from kb.server import tools
        rows = tools._rows("SELECT value FROM build_info WHERE key='built_at'")
        if rows:
            built = str(rows[0]["value"])
    except Exception:                                     # noqa: BLE001
        pass
    return str(config.SOURCE_ROOT), built


# ===========================================================================
# Validation
# ===========================================================================

def _clean(value: object, limit: int, label: str, required: bool) -> Tuple[str, str]:
    """(cleaned value, error). Collapses internal whitespace, enforces a limit."""
    text = " ".join(str(value or "").split())
    if required and not text:
        return "", f"{label} is required"
    if len(text) > limit:
        return "", f"{label} is {len(text):,} characters; the limit is {limit:,}"
    return text, ""


def _subject_key(subject: str) -> str:
    """Identifiers are case-insensitive on IBM i; topics are matched the same way."""
    return subject.upper()


def _fts_query(text: str) -> str:
    """A safe FTS5 MATCH expression: every term quoted, joined with OR.

    Passing the caller's text straight to MATCH lets `NEAR`, `*`, quotes or a
    stray parenthesis raise a syntax error or change the meaning. Quoting each
    term makes all of them literal.
    """
    terms = list(dict.fromkeys(t for t in _TERM.findall(text or "") if t))
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms[:32])


# ===========================================================================
# Formatting
# ===========================================================================

def _age_note(row: sqlite3.Row, source_root: str, built: str) -> str:
    notes = []
    if row["source_root"] and source_root and row["source_root"] != source_root:
        notes.append(f"recorded against another source tree: {row['source_root']}")
    elif row["index_built"] and built and row["index_built"] < built:
        notes.append("recorded against an older index build - recheck")
    return f"  ({'; '.join(notes)})" if notes else ""


def _line(row: sqlite3.Row, source_root: str, built: str) -> str:
    where = f"@{row['library']}" if row["library"] else ""
    evidence = f"  [{row['evidence']}]" if row["evidence"] else ""
    confidence = "" if row["confidence"] == "exact" else f" ({row['confidence']})"
    return (f"  #{row['id']:<5} {row['ticket']:<16} {row['kind']:<9} "
            f"{row['subject']}{where}: {row['statement']}{confidence}{evidence}"
            f"{_age_note(row, source_root, built)}")


# ===========================================================================
# Tools
# ===========================================================================

def remember(ticket: str, kind: str, subject: str, statement: str,
             evidence: str = "", library: str = "", confidence: str = "exact",
             supersedes: int = 0) -> str:
    """Record one fact from the analysis of a ticket.

    Idempotent: recording the same fact twice returns the existing entry, so a
    retried call does not duplicate it. `supersedes` corrects an earlier fact
    of the SAME ticket; facts of other tickets are never altered from here.
    The answer lists what other tickets recorded about the same subject.
    """
    ticket, e1 = _clean(ticket, MAX_TICKET, "ticket", True)
    subject, e2 = _clean(subject, MAX_SUBJECT, "subject", True)
    statement, e3 = _clean(statement, MAX_STATEMENT, "statement", True)
    evidence, e4 = _clean(evidence, MAX_EVIDENCE, "evidence", False)
    library, e5 = _clean(library, MAX_LIBRARY, "library", False)
    errors = [e for e in (e1, e2, e3, e4, e5) if e]
    kind = (kind or "").strip().lower()
    confidence = (confidence or "exact").strip().lower()
    if kind not in KINDS:
        errors.append(f"kind must be one of: {', '.join(KINDS)}")
    if confidence not in CONFIDENCE:
        errors.append(f"confidence must be one of: {', '.join(CONFIDENCE)}")
    if errors:
        return "Not recorded.\n  " + "\n  ".join(errors)

    subject = _subject_key(subject)
    library = library.upper()
    source_root, built = _index_context()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with _LOCK:
        conn, _ = _connect()
        existing = conn.execute(
            "SELECT id FROM memory WHERE ticket=? AND kind=? AND subject=? "
            "AND library=? AND statement=? AND superseded_by IS NULL",
            (ticket, kind, subject, library, statement)).fetchone()
        if existing:
            new_id = existing["id"]
            header = f"Already recorded as #{new_id} - nothing added."
        else:
            if supersedes:
                old = conn.execute("SELECT ticket, superseded_by FROM memory "
                                   "WHERE id=?", (supersedes,)).fetchone()
                if old is None:
                    return f"Not recorded.\n  supersedes: no entry #{supersedes}."
                if old["ticket"] != ticket:
                    return (f"Not recorded.\n  #{supersedes} belongs to ticket "
                            f"{old['ticket']}; an entry can only correct its own "
                            f"ticket's facts.")
                if old["superseded_by"]:
                    return (f"Not recorded.\n  #{supersedes} was already "
                            f"superseded by #{old['superseded_by']}.")
            cursor = conn.execute(
                "INSERT INTO memory (ticket, kind, subject, library, statement, "
                "evidence, confidence, source_root, index_built, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ticket, kind, subject, library, statement, evidence,
                 confidence, source_root, built, now))
            new_id = cursor.lastrowid
            if supersedes:
                conn.execute("UPDATE memory SET superseded_by=? WHERE id=?",
                             (new_id, supersedes))
            conn.commit()
            header = (f"Recorded #{new_id}: {ticket} {kind} {subject}"
                      + (f"@{library}" if library else "")
                      + (f" (supersedes #{supersedes})" if supersedes else ""))

        others = conn.execute(
            "SELECT * FROM memory WHERE subject=? AND ticket<>? "
            "AND superseded_by IS NULL AND (library=? OR library='' OR ?='') "
            "ORDER BY created_at DESC LIMIT 12",
            (subject, ticket, library, library)).fetchall()

    out = [header]
    if others:
        out += ["", f"  OTHER TICKETS ON {subject} - compare before relying on "
                    f"either:"]
        out += [_line(r, source_root, built) for r in others]
    return "\n".join(out)


def recall(query: str = "", subject: str = "", ticket: str = "",
           kind: str = "", limit: int = 30) -> str:
    """What earlier tickets recorded, by exact subject, free text, or ticket.

    Subjects with more than one current statement across tickets are flagged,
    because that is where two analyses disagree.
    """
    query, e1 = _clean(query, MAX_STATEMENT, "query", False)
    subject, e2 = _clean(subject, MAX_SUBJECT, "subject", False)
    ticket, e3 = _clean(ticket, MAX_TICKET, "ticket", False)
    kind = (kind or "").strip().lower()
    errors = [e for e in (e1, e2, e3) if e]
    if kind and kind not in KINDS:
        errors.append(f"kind must be one of: {', '.join(KINDS)}")
    if not (query or subject or ticket):
        errors.append("give at least one of query, subject or ticket")
    if errors:
        return "Nothing searched.\n  " + "\n  ".join(errors)
    limit = max(1, min(int(limit or 30), 200))

    where: List[str] = ["m.superseded_by IS NULL"]
    args: List[object] = []
    if subject:
        where.append("m.subject = ?")
        args.append(_subject_key(subject))
    if ticket:
        where.append("m.ticket = ?")
        args.append(ticket)
    if kind:
        where.append("m.kind = ?")
        args.append(kind)

    source_root, built = _index_context()
    with _LOCK:
        conn, has_fts = _connect()
        total = conn.execute("SELECT COUNT(*) c FROM memory").fetchone()["c"]
        if total == 0:
            return ("Ticket memory is empty - no earlier ticket has recorded "
                    "anything yet.\n  Record durable facts with remember at "
                    "the end of each analysis.")
        match = _fts_query(query) if query else ""
        if query and not match:
            return "Nothing searched.\n  query holds no searchable terms."
        if match and has_fts:
            sql = ("SELECT m.* FROM memory_fts f JOIN memory m ON m.id = f.rowid "
                   "WHERE memory_fts MATCH ? AND " + " AND ".join(where)
                   + " ORDER BY bm25(memory_fts), m.created_at DESC LIMIT ?")
            rows = conn.execute(sql, [match, *args, limit]).fetchall()
        else:
            if match:
                terms = _TERM.findall(query)
                like = " OR ".join("(m.subject LIKE ? OR m.statement LIKE ? "
                                   "OR m.evidence LIKE ?)" for _ in terms)
                where.append(f"({like})")
                for term in terms:
                    args += [f"%{term}%"] * 3
            sql = ("SELECT m.* FROM memory m WHERE " + " AND ".join(where)
                   + " ORDER BY m.subject, m.created_at DESC LIMIT ?")
            rows = conn.execute(sql, [*args, limit]).fetchall()
        superseded = conn.execute("SELECT COUNT(*) c FROM memory "
                                  "WHERE superseded_by IS NOT NULL").fetchone()["c"]

    if not rows:
        asked = ", ".join(f"{k}={v!r}" for k, v in
                          (("query", query), ("subject", subject),
                           ("ticket", ticket), ("kind", kind)) if v)
        return (f"No remembered fact matches {asked}.\n  {total:,} fact(s) are "
                f"stored; none are about this, so no earlier ticket recorded it.")

    by_subject: Dict[str, List[sqlite3.Row]] = {}
    for r in rows:
        by_subject.setdefault(r["subject"], []).append(r)

    out = [f"{len(rows)} remembered fact(s)"
           + (f" (limit {limit} reached - narrow the query)" if len(rows) >= limit else "")
           + f"; {total:,} stored in all."]
    for subj, group in by_subject.items():
        tickets = {r["ticket"] for r in group}
        statements = {r["statement"] for r in group}
        flag = ("   ** DIFFERS ACROSS TICKETS **"
                if len(tickets) > 1 and len(statements) > 1 else "")
        out += ["", f"  {subj}{flag}"]
        out += [_line(r, source_root, built) for r in group]
    if superseded:
        out += ["", f"  {superseded:,} corrected fact(s) are hidden; the "
                    f"corrections are shown."]
    return "\n".join(out)


def stats() -> Dict[str, object]:
    """For doctor: where memory is and how much it holds."""
    path = memory_path()
    if not path.exists():
        return {"path": str(path), "exists": False, "facts": 0, "tickets": 0}
    with _LOCK:
        conn, has_fts = _connect()
        facts = conn.execute("SELECT COUNT(*) c FROM memory").fetchone()["c"]
        tickets = conn.execute("SELECT COUNT(DISTINCT ticket) c FROM memory").fetchone()["c"]
    return {"path": str(path), "exists": True, "facts": facts,
            "tickets": tickets, "fts": has_fts}
