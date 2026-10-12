"""
Where written descriptions live, and whether they are still true.

Two things are centralised here because they were duplicated and drifting:
the page path (`kb/summarise/build.py` and `kb/server/tools.py` each built
`SUMMARY_DIR / f"{name}.md"` independently) and the freshness rule.

Freshness is not "has the member changed" once there are roll-ups. A menu page
is written from two members; a family page from its menus; a library page from
its families. So a page records the hash of *what it was written from*, and a
roll-up additionally records the hash of its children's hashes. A library page
goes stale because a menu three levels down moved, and nothing else can express
that.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .. import config

#: scope -> level.  Level is what the build orders by: children before parents.
LEVELS: Dict[str, int] = {"library": 0, "family": 1, "menu": 2, "member": 3}

#: What `written_by` says when no model was configured.
FACTS_ONLY = "facts"


@dataclass
class Page:
    scope: str
    key: str
    library: str
    text: str
    written_by: str
    input_hash: str
    child_hash: str = ""
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def level(self) -> int:
        return LEVELS[self.scope]

    @property
    def from_facts_only(self) -> bool:
        return self.written_by == FACTS_ONLY


def digest(*parts: str) -> str:
    """A short stable hash of whatever a page was written from."""
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


def page_path(scope: str, key: str) -> Path:
    """Where a page is written on disk.

    One function, because two copies of this convention is how the server and
    the summariser ended up able to disagree about where a page lives.  Scoped
    into a subdirectory per level so a menu called ORD100 and a program called
    ORD100 cannot overwrite each other - the flat `<NAME>.md` scheme could not
    express that and silently lost one of them.
    """
    if scope == "member":
        # Unchanged, so pages written before this existed are still found.
        return config.SUMMARY_DIR / f"{key}.md"
    return config.SUMMARY_DIR / scope / f"{key}.md"


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def read(db: sqlite3.Connection, scope: str, key: str,
         library: str = "") -> Optional[Page]:
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT * FROM summary WHERE scope=? AND key=? AND library=?",
        (scope, key, library)).fetchone()
    if row is None:
        return None
    return Page(scope=row["scope"], key=row["key"], library=row["library"],
                text=row["text"], written_by=row["written_by"],
                input_hash=row["input_hash"], child_hash=row["child_hash"],
                tokens_in=row["tokens_in"] or 0,
                tokens_out=row["tokens_out"] or 0)


def children_hash(db: sqlite3.Connection, scope: str,
                  keys: Sequence[str]) -> str:
    """The hash a roll-up compares against to see if anything below it moved."""
    db.row_factory = sqlite3.Row
    parts: List[str] = []
    for key in sorted(keys):
        row = db.execute("SELECT input_hash FROM summary "
                         "WHERE scope=? AND key=?", (scope, key)).fetchone()
        parts.append(f"{key}:{row['input_hash'] if row else ''}")
    return digest(*parts)


def is_fresh(existing: Optional[Page], input_hash: str,
             child_hash: str = "", model_available: bool = True) -> bool:
    """Can this page be left alone?

    A facts-only page is never fresh once a model is available.  The member
    summariser stamped the source hash onto facts-only pages, so a run without
    a model marked every member done and --stale then skipped all of them
    forever, even after a model was configured.  There was no way to tell the
    two kinds of page apart except by grepping the prose.
    """
    if existing is None:
        return False
    if existing.from_facts_only and model_available:
        return False
    if existing.input_hash != input_hash:
        return False
    if child_hash and existing.child_hash != child_hash:
        return False
    return True


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def write(db: sqlite3.Connection, page: Page, sheet: str) -> None:
    """Record a page in the database, on disk, and in the summary index.

    The sheet is embedded verbatim under the prose. That is not decoration: it
    is the only thing that lets a reader check a generated claim against the
    facts it was supposedly written from, and the model has already been caught
    opening a menu summary with "allows users to manage pricing" for a menu
    with no pricing option.
    """
    db.execute(
        "INSERT INTO summary (scope, key, library, level, text, "
        "written_by, input_hash, child_hash, tokens_in, tokens_out, "
        "written_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT (scope, key, library) DO UPDATE SET "
            "level=excluded.level, text=excluded.text, "
            "written_by=excluded.written_by, input_hash=excluded.input_hash, "
            "child_hash=excluded.child_hash, tokens_in=excluded.tokens_in, "
            "tokens_out=excluded.tokens_out, written_at=excluded.written_at",
        (page.scope, page.key, page.library, page.level, page.text,
         page.written_by, page.input_hash, page.child_hash,
         page.tokens_in, page.tokens_out,
         datetime.now(timezone.utc).isoformat()))

    db.execute("DELETE FROM search_summary WHERE scope=? AND key=?",
               (page.scope, page.key))
    db.execute("INSERT INTO search_summary (scope, key, body) VALUES (?,?,?)",
               (page.scope, page.key, page.text[:20000]))

    path = page_path(page.scope, page.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    provenance = (
        f"_{page.scope} page for {page.key}"
        + (f" in {page.library}" if page.library else "")
        + (f", written by {page.written_by}" if not page.from_facts_only
           else ", written from facts alone - no model configured")
        + f". The prose above is generated and may be wrong; the facts below "
          f"are read from source. Verify before acting._")
    path.write_text(f"# {page.key}\n\n{page.text}\n\n---\n{provenance}\n\n"
                    f"```\n{sheet}\n```\n",
                    encoding="utf-8", newline="\n")


def status(db: sqlite3.Connection) -> List[Dict]:
    """Per level: how many pages exist and who wrote them."""
    db.row_factory = sqlite3.Row
    return [dict(r) for r in db.execute(
        "SELECT scope, level, COUNT(*) pages, "
        "       COUNT(*) FILTER (WHERE written_by=?) facts_only, "
        "       SUM(tokens_in) tokens_in, SUM(tokens_out) tokens_out "
        "  FROM summary GROUP BY scope, level ORDER BY level",
        (FACTS_ONLY,))]
