"""
Where the vectors live: a Qdrant server, or an embedded directory.

    ALPHAPLUS_QDRANT_URL=http://localhost:6333   -> the server
    (unset)                                      -> QdrantClient(path=...), no server

One resolver, because two modules open Qdrant - kb.summarise.vectors writes,
kb.server.semantic searches - and they were each constructing their own client
against a hard-coded `path=`. When a server appeared on this machine, both kept
silently reading the embedded directory: the same forgotten-reader failure the
relational seam already paid for once with kb/graph/fulltext.py's copied
connection factory.

Embedded stays the default on purpose. This knowledge base installs inside
client networks where a new service is a change request; the embedded mode is a
directory beside the index and needs nothing. A server is opt-in via the URL,
and everything above this module is identical either way - qdrant-client speaks
the same API to both.

One embedded-only caveat: the directory takes a LOCK, one process at a time.
The server has no such limit, which is itself a reason to set the URL on a
machine that has one.
"""

from __future__ import annotations

import os
from typing import Tuple

COLLECTION = "alphaplus_pages"


def resolve() -> Tuple[str, str]:
    """('server', url) or ('embedded', path) - where vectors live here."""
    from kb.config import setting
    url = str(setting("ALPHAPLUS_QDRANT_URL", "qdrant", "url",
                      default="")).strip().rstrip("/")
    if url:
        return "server", url
    from kb import config
    return "embedded", str(config.qdrant_path())


def where() -> str:
    """One quotable line for status output."""
    kind, target = resolve()
    return f"qdrant {kind} at {target}"


def client(create: bool = False):
    """A QdrantClient for wherever vectors live.

    `create=False` refuses an embedded path that does not exist yet, so a
    search cannot conjure an empty store and report "no results" as if that
    were an answer. A server is not checked the same way - connecting IS the
    check, and a missing collection is its own clear error downstream.

    Raises RuntimeError with a quotable reason; callers decide whether that is
    fatal (the writer) or a degradation (the searcher).
    """
    try:
        from qdrant_client import QdrantClient
    except ImportError:
        raise RuntimeError(
            "qdrant-client is not installed.\n"
            "       python -m pip install --user qdrant-client\n"
            "       Everything else in the knowledge base works without it."
        ) from None

    kind, target = resolve()
    if kind == "server":
        try:
            c = QdrantClient(url=target, timeout=10)
            c.get_collections()          # fail here, with the URL in the error
            return c
        except Exception as exc:         # noqa: BLE001
            raise RuntimeError(
                f"qdrant server at {target} is not answering: "
                f"{str(exc).splitlines()[0][:70]}") from None

    from pathlib import Path
    path = Path(target)
    if not create and not path.exists():
        raise RuntimeError(
            f"no vector store at {path}\n"
            f"       python -m kb.summarise.vectors")
    path.mkdir(parents=True, exist_ok=True)
    return QdrantClient(path=str(path))
