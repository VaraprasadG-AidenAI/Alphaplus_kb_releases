"""
Embed the written pages into a local Qdrant collection.

    python -m kb.summarise.vectors --check     # what exists, and is it usable
    python -m kb.summarise.vectors             # embed what has changed
    python -m kb.summarise.vectors --rebuild   # drop the collection and redo

Only the written pages are embedded - whatever levels the pyramid has
actually produced, which today is menus alone and not all of them.  Not
code, not identifiers, not field names.  `docs/kb/01-RESEARCH.md` settled that:
COBOL identifiers are OHDCNO and IWHQOH, so there is nothing for a vector to
mean, and it sanctions exactly this narrow case instead - "the ticket is
written in business language and the code is not".

## SQLite is the record; Qdrant only holds vectors

The `embedding` table says what was embedded, from which exact string, by which
model, at what width, with which prefix, and when.  That split is the point:

  * a vector on its own cannot say whether it is stale, and the page it
    describes can be rewritten underneath it;
  * a collection holding vectors from two different models is indistinguishable
    from one holding good vectors, and silently ranks nonsense;
  * re-embedding must overwrite rather than accumulate, which needs a stable id
    rather than a fresh one each run.

So ids are a UUID5 of `scope:key:library` - deterministic, so the second run
replaces the first - and every vector's provenance lands in SQLite where the
rest of this knowledge base keeps its facts.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from .. import config
from . import embed as E

COLLECTION = "alphaplus_pages"

#: Namespace for deterministic point ids.  Fixed forever: change it and every
#: id changes, so the next build inserts 113 duplicates beside the old ones
#: instead of replacing them.
NAMESPACE = uuid.UUID("6f1b6d3e-6c1a-4c9b-9d21-0a1b2c3d4e5f")

#: How many pages to embed per HTTP call.  Small because the pages are short
#: and a failed batch is re-embedded whole.
BATCH = 16


def point_id(scope: str, key: str, library: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{scope}:{key}:{library}"))


def _client(create: bool = False):
    """A local Qdrant, or a reason it cannot be had.

    Imported here rather than at module scope so that `kb` imports and runs
    with qdrant-client absent, which is the normal case: it is the first
    third-party dependency this project has ever taken and it stays optional.
    """
    from kb.store import vectors as vstore
    c = vstore.client(create=create)     # embedded, or ALPHAPLUS_QDRANT_URL
    from qdrant_client import models
    return c, models


def _pages(db: sqlite3.Connection) -> List[Dict]:
    db.row_factory = sqlite3.Row
    return [dict(r) for r in db.execute(
        "SELECT scope, key, library, level, text, written_by, input_hash "
        "  FROM summary WHERE text<>'' ORDER BY level, key")]


def _recorded(db: sqlite3.Connection) -> Dict[Tuple[str, str, str], Dict]:
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute("SELECT * FROM embedding").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {(r["scope"], r["key"], r["library"]): dict(r) for r in rows}


def _document(page: Dict) -> str:
    """The exact string that gets embedded.

    The key is prepended because a ticket often names the area in the words the
    menu is called - "the reports menu" - and the body may never repeat its own
    name. Whatever is built here is hashed into `text_sha`, so a change to this
    function invalidates every vector and the next run notices.
    """
    head = f"{page['scope']} {page['key']}"
    if page["library"]:
        head += f" in library {page['library']}"
    return f"{head}\n\n{page['text']}"


def status(db: sqlite3.Connection) -> int:
    info = dict(db.execute("SELECT key, value FROM build_info").fetchall())
    recorded = _recorded(db)
    pages = _pages(db)

    print()
    print(f"  pages written      {len(pages)}")
    print(f"  vectors recorded   {len(recorded)}")
    print(f"  model              {E.describe()}")
    if info.get("embedding_model"):
        print(f"  collection built   {info.get('embedding_built_at','?')[:19]}"
              f"  by {info['embedding_model']} "
              f"({info.get('embedding_dim','?')} dims)")
        if info["embedding_model"] != f"local/{config.EMBED_MODEL}":
            print(f"  !! the collection was built by "
                  f"{info['embedding_model']} and the configured model is "
                  f"local/{config.EMBED_MODEL}.\n"
                  f"     Vectors from different models are not comparable - "
                  f"rebuild before searching.")
    else:
        print("  collection         never built")

    stale = [p for p in pages if not _fresh(p, recorded)]
    if stale:
        print(f"  out of date        {len(stale)} page(s) need embedding")
    orphan = set(recorded) - {(p["scope"], p["key"], p["library"])
                              for p in pages}
    if orphan:
        print(f"  orphaned           {len(orphan)} vector(s) whose page is gone")

    try:
        client, _models = _client()
        count = client.get_collection(COLLECTION).points_count
        print(f"  collection points  {count}")
        client.close()
    except Exception as exc:
        print(f"  collection         unreadable: "
              f"{str(exc).splitlines()[0][:60]}")
    print()
    return 0


def _fresh(page: Dict, recorded: Dict) -> bool:
    """Is this page's vector current?

    Four ways it is not, and all four have to be checked: never embedded, the
    text changed, the page was rewritten, or the model changed underneath it.
    """
    row = recorded.get((page["scope"], page["key"], page["library"]))
    if row is None:
        return False
    if row["text_sha"] != E.text_sha(_document(page)):
        return False
    if (row["input_hash"] or "") != (page["input_hash"] or ""):
        return False
    if row["model"] != f"local/{config.EMBED_MODEL}":
        return False
    if row["dim"] != config.EMBED_DIM:
        return False
    if (row["doc_prefix"] or "") != config.EMBED_DOC_PREFIX:
        return False
    return True


def build(rebuild: bool = False, limit: Optional[int] = None) -> int:
    from kb import store
    ok, why = store.ready()
    if not ok:
        print(why, file=sys.stderr)
        return 1

    from kb import store
    db = store.facts(read_only=False)
    pages = _pages(db)
    if not pages:
        print("no written pages to embed. Run: python -m kb summarise",
              file=sys.stderr)
        return 1

    try:
        client, models = _client(create=True)
    except RuntimeError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1

    model_name = f"local/{config.EMBED_MODEL}"
    existing = {c.name for c in client.get_collections().collections}
    if rebuild and COLLECTION in existing:
        client.delete_collection(COLLECTION)
        db.execute("DELETE FROM embedding")
        existing.discard(COLLECTION)
        print("  dropped the old collection")

    if COLLECTION not in existing:
        client.create_collection(
            COLLECTION,
            vectors_config=models.VectorParams(
                size=config.EMBED_DIM, distance=models.Distance.COSINE))
        print(f"  created {COLLECTION}: {config.EMBED_DIM} dims, cosine")

    recorded = _recorded(db)
    todo = [p for p in pages if rebuild or not _fresh(p, recorded)]
    if limit:
        todo = todo[:limit]

    print(f"  {len(pages)} page(s), {len(todo)} to embed")
    if not todo:
        client.close()
        db.close()
        return 0

    started = time.time()
    done = 0
    for at in range(0, len(todo), BATCH):
        chunk = todo[at:at + BATCH]
        documents = [_document(p) for p in chunk]
        try:
            vectors = E.embed(documents)
        except E.EmbedError as exc:
            print(f"  ! {exc}", file=sys.stderr)
            db.commit()
            client.close()
            return 1

        stamp = datetime.now(timezone.utc).isoformat()
        points = []
        for page, document, vector in zip(chunk, documents, vectors):
            pid = point_id(page["scope"], page["key"], page["library"])
            sha = E.text_sha(document)
            # The payload carries identity and provenance only. Live facts stay
            # in SQLite: a payload that duplicates them goes stale the moment
            # the index is rebuilt, and then two sources disagree.
            points.append(models.PointStruct(
                id=pid,
                vector=vector,
                # Identity and a preview - what serving a hit needs, and
                # nothing else. model/dim/text_sha/input_hash/embedded_at/chars
                # used to be copied here too; their one home is the `embedding`
                # table, and the deterministic point id joins the two. Two
                # records of the same fact, kept in step by nothing, is the
                # placement smell this migration is removing.
                payload={
                    "scope": page["scope"],
                    "key": page["key"],
                    "library": page["library"] or "",
                    "preview": page["text"][:220],
                }))
            db.execute(
                "INSERT INTO embedding (scope, key, library, "
                "point_id, model, dim, doc_prefix, text_sha, input_hash, "
                "chars, embedded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT (scope, key, library) DO UPDATE SET "
                "point_id=excluded.point_id, model=excluded.model, "
                "dim=excluded.dim, doc_prefix=excluded.doc_prefix, "
                "text_sha=excluded.text_sha, input_hash=excluded.input_hash, "
                "chars=excluded.chars, embedded_at=excluded.embedded_at",
                (page["scope"], page["key"], page["library"] or "", pid,
                 model_name, config.EMBED_DIM, config.EMBED_DOC_PREFIX, sha,
                 page["input_hash"] or "", len(document), stamp))

        client.upsert(COLLECTION, points=points)
        db.commit()
        done += len(chunk)
        rate = (time.time() - started) / done
        print(f"    {done:>4}/{len(todo)}  {rate:.1f}s each", flush=True)

    # Vectors whose page has been deleted must go, or a search returns an area
    # that no longer exists.
    live = {point_id(p["scope"], p["key"], p["library"]) for p in pages}
    gone = [r["point_id"] for r in _recorded(db).values()
            if r["point_id"] not in live]
    if gone:
        client.delete(COLLECTION,
                      points_selector=models.PointIdsList(points=gone))
        db.executemany("DELETE FROM embedding WHERE point_id=?",
                       [(g,) for g in gone])
        print(f"  removed {len(gone)} vector(s) whose page is gone")

    for key, value in (("embedding_model", model_name),
                       ("embedding_dim", str(config.EMBED_DIM)),
                       ("embedding_distance", "cosine"),
                       ("embedding_doc_prefix", config.EMBED_DOC_PREFIX),
                       ("embedding_query_prefix", config.EMBED_QUERY_PREFIX),
                       ("embedding_built_at",
                        datetime.now(timezone.utc).isoformat())):
        db.execute("INSERT INTO build_info (key, value) "
                   "VALUES (?,?)"
            " ON CONFLICT (key) DO UPDATE SET value=excluded.value",
            (key, value))
    db.commit()

    count = client.get_collection(COLLECTION).points_count
    client.close()
    db.close()
    # vstore.where(), not qdrant_path(): with ALPHAPLUS_QDRANT_URL set the
    # vectors are on a server and naming the embedded directory sends anyone
    # checking to an empty folder.
    from kb.store import vectors as vstore
    print(f"  {count} vector(s) in {vstore.where()}")
    print(f"  done in {time.time() - started:.0f}s")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="what exists and whether it is usable")
    ap.add_argument("--rebuild", action="store_true",
                    help="drop the collection and embed every page again")
    ap.add_argument("--limit", type=int, help="only the first N pages")
    args = ap.parse_args(argv)

    from kb import store
    ok, why = store.ready()
    if not ok:
        print(why, file=sys.stderr)
        return 1
    if args.check:
        from kb import store
        db = store.facts()
        code = status(db)
        db.close()
        return code
    return build(args.rebuild, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
