"""
The dense half of routing: cosine over the written pages.

Read-only, lazy, and it never raises at the caller.  Every way this can be
unavailable - the extra not installed, the store never built, the collection
built by a different embedding model - returns `([], reason)` so `routing.py`
can fall back to bm25 and say what it could not use.  A tool that fails because
an optional extra is missing is worse than one that says so and answers anyway.

The model mismatch check is the one that matters.  Vectors from two different
models are not comparable, and a collection holding both ranks nonsense while
looking perfectly healthy - so what built the collection is compared against
what is configured now, and a mismatch refuses to search rather than returning
plausible rubbish.
"""

from __future__ import annotations

import atexit
import sqlite3
from typing import Dict, List, Optional, Sequence, Tuple

from .. import config

#: One client per process.  Qdrant's local mode takes a lock on the directory,
#: so opening it per query would serialise every request behind the last one.
_CLIENT = None
_FAILED = ""


def _client():
    """(client, reason).  A reason means no client, and it is quotable."""
    global _CLIENT, _FAILED
    if _CLIENT is not None:
        return _CLIENT, ""
    if _FAILED:
        return None, _FAILED
    try:
        from kb.store import vectors as vstore
        _CLIENT = vstore.client()   # embedded, or ALPHAPLUS_QDRANT_URL
    except Exception as exc:                            # noqa: BLE001
        _FAILED = f"semantic routing off: {str(exc).splitlines()[0][:90]}"
        return None, _FAILED
    return _CLIENT, ""


def close() -> None:
    """Release the directory lock.  Used by config.repoint and the tests."""
    global _CLIENT, _FAILED
    if _CLIENT is not None:
        try:
            _CLIENT.close()
        except Exception:
            pass
    _CLIENT = None
    _FAILED = ""


# Closed before the interpreter tears down, because Qdrant's own destructor
# runs too late: at that point sys.meta_path is already None, its close() fails
# on an import, and the traceback lands in the MCP server's log where it looks
# like a real fault. Closing here means the destructor finds nothing to do.
atexit.register(close)


def _model_matches(db: sqlite3.Connection) -> Tuple[bool, str]:
    """Were these vectors made by the model we are about to query with?

    Read from the `embedding` TABLE first, and from build_info only as a
    fallback.  That ordering is the fix for a silent failure: build_info's
    embedding_* keys are not in build.GENERATED_TABLES, so a rebuild carried the
    112 embedding rows and the Qdrant collection forward and dropped the three
    provenance keys - after which this function said "the vector store has not
    been built" about a vector store that was sitting right there, and the whole
    dense half of routing turned itself off with a message that sent you to
    rebuild something already built.

    The `embedding` table is the better source anyway: it is per-row, it IS
    carried forward, and it cannot disagree with itself.
    """
    rows = {}
    try:
        found = db.execute(
            "SELECT DISTINCT model, dim FROM embedding").fetchall()
        if len(found) > 1:
            models = ", ".join(sorted(str(r[0]) for r in found))
            return False, (f"semantic routing off: the embedding table holds "
                           f"vectors from more than one model ({models}). "
                           f"They cannot be compared - rebuild with "
                           f"python -m kb.summarise.vectors --rebuild")
        if found:
            rows = {"embedding_model": found[0][0],
                    "embedding_dim": str(found[0][1])}
    except Exception:                                   # noqa: BLE001
        rows = {}
    if not rows:
        try:
            rows = dict(db.execute(
                "SELECT key, value FROM build_info WHERE key LIKE 'embedding_%'"
            ).fetchall())
        except Exception:                               # noqa: BLE001
            return False, "semantic routing off: this index predates embeddings"
    built = rows.get("embedding_model")
    if not built:
        return False, ("semantic routing off: the vector store has not been "
                       "built (python -m kb.summarise.vectors)")
    want = f"local/{config.EMBED_MODEL}"
    if built != want:
        return False, (f"semantic routing off: the collection was built by "
                       f"{built} but {want} is configured. Vectors from "
                       f"different models cannot be compared - rebuild with "
                       f"python -m kb.summarise.vectors --rebuild")
    if rows.get("embedding_dim") and int(rows["embedding_dim"]) != config.EMBED_DIM:
        return False, (f"semantic routing off: the collection is "
                       f"{rows['embedding_dim']} dimensions and "
                       f"{config.EMBED_DIM} is configured - rebuild")
    return True, ""


def search(db: sqlite3.Connection, text: str,
           limit: int = 12) -> Tuple[List, str]:
    """(hits, reason).  Hits are routing.Hit, so the fuser needs no branch."""
    from .routing import Hit                 # local: routing imports this too

    if config.SEMANTIC_MODE == "off":
        return [], "semantic routing off (ALPHAPLUS_SEMANTIC=0)"
    ok, reason = _model_matches(db)
    if not ok:
        return [], reason
    client, reason = _client()
    if client is None:
        return [], reason

    try:
        from ..summarise import embed as E
        vector = E.embed_one(text, is_query=True)
    except Exception as exc:
        return [], (f"semantic routing off: the embedding model did not "
                    f"answer - {str(exc).splitlines()[0][:70]}")

    from ..summarise.vectors import COLLECTION
    try:
        found = client.query_points(COLLECTION, query=vector, limit=limit,
                                    with_payload=True).points
    except Exception as exc:
        return [], f"semantic routing off: {str(exc).splitlines()[0][:70]}"

    out: List = []
    for rank, point in enumerate(found):
        payload = point.payload or {}
        out.append(Hit(scope=payload.get("scope", "?"),
                       key=payload.get("key", "?"),
                       library=payload.get("library", ""),
                       found_by={"cosine": (rank, float(point.score))},
                       preview=payload.get("preview", "")))
    return out, ""


def describe(db: sqlite3.Connection) -> str:
    """One line for coverage_report and doctor."""
    if not config.ENABLE_SEMANTIC:
        return "semantic routing: off (ALPHAPLUS_SEMANTIC=0)"
    ok, reason = _model_matches(db)
    if not ok:
        return reason
    client, why = _client()
    if client is None:
        return why
    try:
        from ..summarise.vectors import COLLECTION
        n = client.get_collection(COLLECTION).points_count
        return (f"semantic routing: {n} page vector(s), "
                f"local/{config.EMBED_MODEL}, cosine")
    except Exception as exc:
        return f"semantic routing: unreadable - {str(exc).splitlines()[0][:60]}"


def available(db: sqlite3.Connection) -> Tuple[bool, str]:
    """(usable, reason).  Everything similarity needs, EXCEPT the model.

    Deliberately weaker than what `search` requires.  Searching by text has to
    embed the query, so it needs the embedding model reachable; comparing two
    stored vectors does not, because both were embedded when the pages were
    written.  Treating those as one condition would switch similarity off every
    time Ollama happened not to be running, for no reason.
    """
    if config.SEMANTIC_MODE == "off":
        return False, "semantic search off (ALPHAPLUS_SEMANTIC=0)"
    ok, why = _model_matches(db)
    if not ok:
        return False, why
    client, why = _client()
    if client is None:
        return False, why
    return True, ""


def similar_to(scope: str, key: str, library: str = "",
               limit: int = 8) -> Tuple[List, str]:
    """(hits, reason).  Pages nearest an EXISTING page's vector.

    Queries by point id, so no embedding model is involved: Qdrant already
    holds the vector this is asking about.  The page itself is dropped from the
    result - it is trivially its own nearest neighbour, and printing it wastes
    a row and reads like a bug.
    """
    from .routing import Hit
    from ..summarise.vectors import COLLECTION, point_id

    client, why = _client()
    if client is None:
        return [], why

    pid = point_id(scope, key, library)
    try:
        found = client.query_points(COLLECTION, query=pid, limit=limit + 1,
                                    with_payload=True).points
    except Exception as exc:                            # noqa: BLE001
        return [], f"similarity unavailable: {str(exc).splitlines()[0][:70]}"

    out: List = []
    for rank, point in enumerate(found):
        if str(point.id) == pid:
            continue                    # itself
        payload = point.payload or {}
        out.append(Hit(scope=payload.get("scope", "?"),
                       key=payload.get("key", "?"),
                       library=payload.get("library", ""),
                       found_by={"cosine": (rank, float(point.score))},
                       preview=payload.get("preview", "")))
    return out[:limit], ""
