"""
Incremental refresh.  Bring the graph up to date with facts.db without a reload.

    python -m kb.graph.update --status          # what differs, no writes
    python -m kb.graph.update                   # refresh everything that changed
    python -m kb.graph.update --member SEC403   # just this one
    python -m kb.graph.update --dry-run
    python -m kb.graph.update --prune           # remove orphaned leaf nodes
    python -m kb.graph.update --verify          # counts, after

## Why this exists rather than "just reload"

A full load is 72 seconds on this data and would be minutes on the real 40 GB
extract.  The SQLite index already refreshes incrementally - `kb.index.update`
re-parses only what changed - and a graph that can only be rebuilt turns a
two-second source edit into a full outage.  Worse, if nobody rebuilds it, the
graph silently serves last week's answers.  That has no error message.

## How it knows what changed - and why the answer lives in the graph

Every Member node carries the `sha256` it was loaded from, copied out of
`member_hash`.  So the diff is:

    facts.db member_hash.sha256   vs   Member.sha256 in the graph

added     in facts.db, not in the graph
modified  in both, hashes differ
deleted   in the graph, not in facts.db

No timestamp file, no external state, nothing to get out of step.  Point this at
a freshly-refreshed facts.db and it works out the rest.  A member with an empty
hash on either side is reported separately rather than guessed at.

## The delete, and the one thing that makes it safe

    MATCH ()-[r]->() WHERE r.src = $member
    CALL { WITH r DELETE r } IN TRANSACTIONS OF $batch ROWS

`r.src` is the member the fact was EXTRACTED FROM, set on every relationship by
the loader and indexed by `kb.graph.schema`.  So this removes exactly the facts
that member asserted - and deliberately NOT the edges pointing AT it.  If
ORD100 changes, `ORD100 CALLS ERRSRV` is ORD100's fact and goes; `CUS200 CALLS
ORD100` is CUS200's fact and stays.  Getting that backwards would delete half
the call graph on every edit, which is why `src` is on every relationship and
not just the ones that looked like they needed it.

Batched, for the same heap reason as the loader: an unbounded DELETE holding
every relationship is the shape that kept killing the discovery agent.  See
docs/NEO4J-LIMITS-REPORT.md.

## Proven, then written

The round trip was measured on SEC403 before this module existed: 307
relationships out in 865 ms, the Member node surviving intact, the tool
correctly reporting "files SEC403 opens: 0" in between, then 307 back in
1,027 ms with every one of the ten edge types at its exact original count.
`--verify` is how you confirm it on anything else.

## What is deliberately NOT incremental

The full-text index.  Lucene re-indexes a node when its `searchtext` changes,
so refreshing the property is enough and there is nothing to invalidate by
hand.  `refresh()` reloads `searchtext` for the members it touches.

## What `--prune` will and will not remove

Only STUB nodes with no remaining edges.  Every label in `model.NODES` is
loaded from its own table, so an isolated :Format or :Message is a real fact -
a record format no program writes, a message nobody sends.  That is dead code,
which is a finding worth reporting, not litter worth deleting.  Pruning by
"has no relationships" removed 4,653 such nodes on the first attempt and
`kb.graph.load --verify` reported it as a node fault immediately.  See
`prune_orphans`.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from kb.graph import driver, model
from kb.graph import settings as gs
from kb.graph.load import OWNER, _chunks, _connect, _facts_db

#: Labels a pruned node can carry.  Every label in `model.NODES` is loaded from
#: its OWN table, so an isolated node of any label is a real fact - and pruning
#: by "has no relationships" deletes real data.
#:
#: That is not hypothetical.  The first version of `prune_orphans` pruned every
#: unreferenced leaf and removed 4,605 :Format and 48 :Message nodes, which
#: `kb.graph.load --verify` immediately reported as node faults.  They were
#: formats defined in a DDS file that no program writes, and messages nobody
#: sends - dead code, which is exactly the kind of finding this knowledge base
#: is meant to surface, not silently delete.
#:
#: So the only thing safe to prune is a STUB: a node the loader invented because
#: a fact pointed at a name it could not resolve.  When the last fact pointing
#: at one goes, nothing is left that mentions it and it really is litter.
PRUNABLE_LABELS: Tuple[str, ...] = tuple(n.label for n in model.NODES)


# ---------------------------------------------------------------------------
# what changed
# ---------------------------------------------------------------------------

def _key(name: str, member_type: str, library: str) -> str:
    return model.uid(name or "", member_type or "", library or "")


def sqlite_hashes(conn: Optional[sqlite3.Connection] = None
                  ) -> Dict[str, Tuple[str, str, str, str]]:
    """uid -> (sha256, name, member_type, library) as facts.db has it."""
    own = conn is None
    conn = conn or _connect()
    out: Dict[str, Tuple[str, str, str, str]] = {}
    # The join comes from model.MEMBER_HASH_JOIN rather than being written out
    # again here.  It used to be a second hand-written copy of the one join the
    # entire diff rests on.
    for r in conn.execute("""
            SELECT o.name, o.member_type, COALESCE(o.library,'') AS library,
                   COALESCE(h.sha256,'') AS sha256
    """ + model.MEMBER_HASH_JOIN):
        out[_key(r["name"], r["member_type"], r["library"])] = (
            r["sha256"], r["name"], r["member_type"], r["library"])
    if own:
        conn.close()
    return out


def graph_hashes() -> Dict[str, Tuple[str, str, str, str]]:
    """uid -> (sha256, name, member_type, library) as the graph has it.

    Stubs excluded.  A stub is a node the loader created because something
    pointed at a name it could not resolve to a real member - it has no source
    and therefore no hash, and treating one as "deleted" would remove the very
    unresolved-target evidence Rule 2 exists to preserve.
    """
    rows = driver.read(
        "MATCH (m:Member) WHERE m.stub IS NULL "
        "RETURN m.uid AS uid, coalesce(m.sha256,'') AS sha256, "
        "       m.name AS name, coalesce(m.member_type,'') AS member_type, "
        "       coalesce(m.library,'') AS library",
        cap=10_000_000)
    return {r["uid"]: (r["sha256"], r["name"], r["member_type"], r["library"])
            for r in rows}


def diff(conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """added / modified / deleted / unchanged / unhashed, keyed by uid."""
    want = sqlite_hashes(conn)
    have = graph_hashes()

    added = sorted(set(want) - set(have))
    deleted = sorted(set(have) - set(want))
    modified: List[str] = []
    unchanged: List[str] = []
    unhashed: List[str] = []

    for uid in sorted(set(want) & set(have)):
        w, h = want[uid][0], have[uid][0]
        if not w or not h:
            # Cannot be compared.  Reported rather than assumed either way -
            # calling it unchanged hides a stale member, calling it modified
            # rewrites the whole graph on every run.
            unhashed.append(uid)
        elif w != h:
            modified.append(uid)
        else:
            unchanged.append(uid)

    return {"added": added, "modified": modified, "deleted": deleted,
            "unchanged": unchanged, "unhashed": unhashed,
            "sqlite": want, "graph": have}


def status(conn: Optional[sqlite3.Connection] = None) -> str:
    d = diff(conn)
    lines = [
        f"  facts.db : {_facts_db()}",
        f"  graph    : {gs.load().neo4j.uri}",
        "",
        f"  {'added':<12}{len(d['added']):>8,}   in facts.db, not in the graph",
        f"  {'modified':<12}{len(d['modified']):>8,}   hash differs",
        f"  {'deleted':<12}{len(d['deleted']):>8,}   in the graph, gone from facts.db",
        f"  {'unchanged':<12}{len(d['unchanged']):>8,}",
        f"  {'unhashed':<12}{len(d['unhashed']):>8,}   no sha256 either side - "
        f"cannot be compared",
    ]
    todo = len(d["added"]) + len(d["modified"]) + len(d["deleted"])
    lines.append("")
    if not todo and d["unhashed"] and not d["unchanged"]:
        # "Nothing to do" and "cannot tell" are different answers and must not
        # print the same sentence.  Every member unhashed means the diff has no
        # evidence at all, which is the one case where reporting "up to date"
        # would be a confident lie.
        lines.append(f"  CANNOT TELL - all {len(d['unhashed']):,} members are "
                     f"unhashed, so nothing can be compared.")
    elif not todo:
        lines.append("  The graph is up to date with facts.db.")
    else:
        lines.append(f"  {todo:,} member(s) need work.  "
                     f"Run `python -m kb.graph.update`.")
    for label in ("added", "modified", "deleted"):
        if d[label]:
            names = [u.split(model.SEP)[0] for u in d[label][:12]]
            more = f" (+{len(d[label]) - 12} more)" if len(d[label]) > 12 else ""
            lines.append(f"    {label:<9} {', '.join(names)}{more}")
    if d["unhashed"]:
        lines.append("")
        lines.append("  `unhashed` usually means facts.db predates the member_hash")
        lines.append("  column, or the graph was loaded before sha256 was carried.")
        lines.append("  Reload once with `python -m kb.graph.load` to fix it.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

def drop_facts(members: Sequence[str], cfg: Optional[gs.Settings] = None
               ) -> int:
    """Delete every relationship these members ASSERTED.  Not the ones about them.

    One statement per member rather than `r.src IN $names`: the index on
    `r.src` is a b-tree seek per value, and an IN over hundreds of values
    against 983k relationships planned as a scan in testing.  A seek each is
    faster and, more importantly, predictable.
    """
    cfg = cfg or gs.load()
    total = 0
    for name in members:
        while True:
            counters = driver.write_autocommit(
                "MATCH ()-[r]->() WHERE r.src = $src "
                "WITH r LIMIT $batch DELETE r",
                {"src": name, "batch": cfg.load.batch_rows})
            n = counters["relationships_deleted"]
            total += n
            if n == 0:
                break
    return total


def drop_members(uids: Sequence[str], cfg: Optional[gs.Settings] = None) -> int:
    """Remove Member nodes that no longer exist in the source.

    DETACH DELETE, because by the time a member is gone its own facts have been
    dropped but edges pointing AT it have not - those belong to members that
    still exist and still assert them.  Detaching converts them to nothing,
    which is right: the target really has vanished.  What is NOT right is
    leaving the node, because then `find_object` still reports a member that
    was deleted last week.
    """
    cfg = cfg or gs.load()
    total = 0
    for chunk in _chunk_list(list(uids), cfg.load.batch_rows):
        counters = driver.write_autocommit(
            "UNWIND $uids AS uid "
            "CALL { WITH uid MATCH (m:Member {uid: uid}) DETACH DELETE m } "
            "IN TRANSACTIONS OF $batch ROWS",
            {"uids": chunk, "batch": cfg.load.batch_rows})
        total += counters["nodes_deleted"]
    return total


def prune_orphans(cfg: Optional[gs.Settings] = None,
                  verbose: bool = True) -> Dict[str, int]:
    """Delete STUB nodes nothing points at any more.  Only stubs.

    `stub = true` marks a node the loader invented because a fact named
    something it could not resolve to a row in any table - an unresolved CALL
    target, a copybook outside the extract.  Keeping it is right while a fact
    still points at it: that is Rule 2 evidence, and the node is what carries
    it.  Once the last such fact is deleted, nothing in the graph mentions the
    name and the node is litter.

    Anything that is NOT a stub came from a table and stays, however isolated.
    A :Format with no WRITES_FORMAT edge is a record format no program writes;
    a :Message with no SENDS edge is a message nobody sends.  Those are dead
    code - a finding, not rubbish.  Deleting them cost 4,653 real nodes once,
    and `--verify` is what caught it.
    """
    cfg = cfg or gs.load()
    out: Dict[str, int] = {}
    for label in PRUNABLE_LABELS:
        gone = 0
        while True:
            counters = driver.write_autocommit(
                f"MATCH (n:{label}) WHERE n.stub = true AND NOT (n)--() "
                f"WITH n LIMIT $batch DELETE n",
                {"batch": cfg.load.batch_rows})
            n = counters["nodes_deleted"]
            gone += n
            if n == 0:
                break
        out[label] = gone
        if verbose and gone:
            print(f"  pruned {gone:,} orphaned stub :{label}")
    return out


def _chunk_list(items: List[Any], size: int) -> Iterable[List[Any]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ---------------------------------------------------------------------------
# reload
# ---------------------------------------------------------------------------

def _filtered(select: str, column: str, names: Sequence[str]) -> Tuple[str, tuple]:
    """Wrap a spec's SELECT so it returns only these members' rows.

    Wrapped rather than edited: every spec's SELECT already has its own WHERE
    and GROUP BY, and splicing a condition into arbitrary SQL is how you get a
    filter that silently applies before the grouping instead of after.
    """
    holes = ", ".join("?" for _ in names)
    return (f"SELECT * FROM ({select.strip()}) WHERE {column} IN ({holes})",
            tuple(names))


def reload_members(names: Sequence[str], conn: sqlite3.Connection,
                   cfg: gs.Settings, verbose: bool = True) -> Dict[str, int]:
    """Re-MERGE the Member nodes and re-load every fact they assert."""
    from kb.graph.load import _uid_expr

    stats = {"nodes": 0, "edges": 0}

    # --- the nodes themselves ------------------------------------------------
    spec = model.NODES[0]                       # Member
    assigns = [f"n.{p} = row.{p}" for p in spec.props]
    assigns.append(f"n.owner = '{OWNER}'")
    for k in spec.key:
        assigns.append(f"n.{k} = coalesce(row.{k}, '')")
    # A node that was a stub and is now a real member must stop being a stub,
    # or `--verify` keeps excluding it and every count stays one short.
    assigns.append("n.stub = null")
    node_cypher = f"""
    UNWIND $rows AS row
    CALL {{
      WITH row
      MERGE (n:Member {{uid: {_uid_expr(spec.key)}}})
      SET {', '.join(assigns)}
    }} IN TRANSACTIONS OF $batch ROWS
    """
    sql, params = _filtered(spec.select, "name", names)
    for batch in _chunks(conn.execute(sql, params), cfg.load.fetch_rows):
        c = driver.write_autocommit(
            node_cypher, {"rows": batch, "batch": cfg.load.batch_rows})
        stats["nodes"] += len(batch)

    # --- everything they assert ---------------------------------------------
    from kb.graph.load import load_edge

    for espec in model.EDGES:
        sql, params = _filtered(espec.select, espec.src_col, names)
        rows = list(conn.execute(sql, params))
        if not rows:
            continue
        # load_edge builds the cypher from the spec; reuse it by handing it a
        # cursor over the filtered rows rather than duplicating the builder.
        n = _load_edge_rows(espec, rows, cfg)
        stats["edges"] += n
        if verbose and n:
            print(f"    :{espec.rel:<16} {n:>7,} rows")

    return stats


def _load_edge_rows(spec: model.EdgeSpec, rows: List[sqlite3.Row],
                    cfg: gs.Settings) -> int:
    """Write pre-selected rows for one EdgeSpec.

    Deliberately reuses `load._edge_cypher` so the incremental path and the
    bulk path cannot drift.  A second copy of the MERGE-pattern logic here is
    exactly how the 4,701 lost WRITES_FIELD edges would come back.
    """
    from kb.graph.load import _edge_cypher, _expand_sql_tables

    cypher = _edge_cypher(spec)
    total = 0
    for batch in _chunks(iter(rows), cfg.load.fetch_rows):
        if spec.end_is_json_list:
            batch = _expand_sql_tables(batch)
            if not batch:
                continue
        driver.write_autocommit(
            cypher, {"rows": batch, "batch": cfg.load.batch_rows})
        total += len(batch)
    return total


def reload_findings(names: Sequence[str], conn: sqlite3.Connection,
                    cfg: gs.Settings) -> Dict[str, int]:
    """Re-attach `unresolved` and `header_disagreement` for these members.

    These are LIST properties built by appending, so they must be cleared
    first or a refresh doubles them - a member would appear to have twice the
    unresolved calls it has, which reads as a regression in coverage.
    """
    out = {"unresolved": 0, "disagreements": 0}
    for chunk in _chunk_list(list(names), cfg.load.batch_rows):
        driver.write_autocommit(
            "UNWIND $names AS n "
            "CALL { WITH n MATCH (m:Member {name: n}) "
            "       SET m.unresolved = null, m.disagreements = null } "
            "IN TRANSACTIONS OF $batch ROWS",
            {"names": chunk, "batch": cfg.load.batch_rows})

    holes = ", ".join("?" for _ in names)
    cypher = """
    UNWIND $rows AS row
    CALL {
      WITH row
      MATCH (m:Member {name: row.member})
      SET m.unresolved = coalesce(m.unresolved, []) + [row.detail]
    } IN TRANSACTIONS OF $batch ROWS
    """
    rows = conn.execute(f"""
        SELECT member,
               kind || ' @' || COALESCE(line,0) || ' ' ||
               COALESCE(raw,'') || ' (' || COALESCE(reason,'') || ')' AS detail
          FROM unresolved WHERE member IN ({holes})
    """, tuple(names))
    for batch in _chunks(rows, cfg.load.fetch_rows):
        driver.write_autocommit(cypher, {"rows": batch,
                                        "batch": cfg.load.batch_rows})
        out["unresolved"] += len(batch)

    cypher = """
    UNWIND $rows AS row
    CALL {
      WITH row
      MATCH (m:Member {name: row.member})
      SET m.disagreements = coalesce(m.disagreements, []) + [row.finding]
    } IN TRANSACTIONS OF $batch ROWS
    """
    rows = conn.execute(
        f"SELECT member, finding FROM header_disagreement "
        f"WHERE member IN ({holes})", tuple(names))
    for batch in _chunks(rows, cfg.load.fetch_rows):
        driver.write_autocommit(cypher, {"rows": batch,
                                        "batch": cfg.load.batch_rows})
        out["disagreements"] += len(batch)
    return out


# ---------------------------------------------------------------------------
# the whole operation
# ---------------------------------------------------------------------------

def refresh(members: Optional[Sequence[str]] = None, prune: bool = False,
            dry_run: bool = False, verbose: bool = True) -> Dict[str, Any]:
    """Delete then reload the facts of everything that changed.

    Order matters and is not arbitrary: facts are dropped BEFORE the nodes are
    re-merged.  The other way round, a relationship that no longer exists in
    the source survives, because MERGE only adds - it has no way to know what
    used to be there.  That is the stale-edge failure with no error message.
    """
    cfg = gs.load()

    # An empty graph is not a refresh, it is a first load, and the incremental
    # path is the wrong tool for it: 13,366 "added" members reload one by one
    # through the per-member statements, minutes instead of the bulk loader's
    # batches - and, until load.run() gained it, without the constraints MERGE
    # depends on.  Delegating makes every caller correct from zero, which is
    # what lets `kb sync` be the whole pipeline.
    if members is None and not dry_run and not graph_hashes():
        if verbose:
            print("  the graph is empty - running the full load instead of "
                  "a refresh")
        from kb.graph import load as bulk
        stats = bulk.run(verbose=verbose)
        stats["scope"] = "first load"
        return stats

    conn = _connect()
    t0 = time.perf_counter()
    out: Dict[str, Any] = {"dry_run": dry_run}

    if members:
        names = sorted({m.strip().upper() for m in members if m.strip()})
        d = diff(conn)
        want = d["sqlite"]
        uids = [u for u, v in want.items() if v[1] in names]
        gone = [u for u, v in d["graph"].items()
                if v[1] in names and u not in want]
        out["scope"] = "named"
    else:
        d = diff(conn)
        touched = d["added"] + d["modified"]
        names = sorted({d["sqlite"][u][1] for u in touched})
        uids = touched
        gone = d["deleted"]
        out["scope"] = "diff"

    out["members"] = names
    out["deleted_members"] = [u.split(model.SEP)[0] for u in gone]

    if verbose:
        print(f"\n  facts.db {_facts_db()}")
        print(f"  graph    {cfg.neo4j.uri}")
        print(f"  {len(names)} member(s) to refresh, {len(gone)} to remove")

    if dry_run:
        conn.close()
        out["seconds"] = round(time.perf_counter() - t0, 1)
        if verbose:
            print("\n  --dry-run: nothing was written.")
            for n in names[:20]:
                print(f"    would refresh {n}")
            for n in out["deleted_members"][:20]:
                print(f"    would remove  {n}")
        return out

    if not names and not gone:
        conn.close()
        out["seconds"] = round(time.perf_counter() - t0, 1)
        if verbose:
            print("\n  Nothing to do - the graph matches facts.db.")
        return out

    # 1. drop what the changed members asserted, and the removed members' too
    all_srcs = sorted(set(names) | {u.split(model.SEP)[0] for u in gone})
    out["edges_deleted"] = drop_facts(all_srcs, cfg)
    if verbose:
        print(f"\n  {out['edges_deleted']:,} relationship(s) deleted")

    # 2. remove members that are gone from the source entirely
    out["nodes_deleted"] = drop_members(gone, cfg) if gone else 0
    if verbose and out["nodes_deleted"]:
        print(f"  {out['nodes_deleted']:,} Member node(s) removed")

    # 3. reload
    if names:
        if verbose:
            print()
        stats = reload_members(names, conn, cfg, verbose)
        out.update(stats)
        out["findings"] = reload_findings(names, conn, cfg)
        from kb.graph import fulltext
        out["searchtext"] = 0 if not model.FULLTEXT else             fulltext.load_text(conn, cfg, members=names,
                                               verbose=verbose)

    # 4. optional tidy-up
    if prune:
        out["pruned"] = prune_orphans(cfg, verbose)

    conn.close()
    out["seconds"] = round(time.perf_counter() - t0, 1)
    if verbose:
        print(f"\n  {len(names)} member(s) refreshed in {out['seconds']}s")
    try:
        from . import fingerprint
        fingerprint.stamp("update")
    except Exception:                                     # noqa: BLE001
        pass
        if not prune:
            print("  Leaf nodes nothing points at any more were left in place; "
                  "add --prune to remove them.")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="bring the graph up to date with facts.db, incrementally")
    ap.add_argument("--status", action="store_true",
                    help="what differs between facts.db and the graph")
    ap.add_argument("--member", nargs="+", metavar="NAME",
                    help="refresh only these members, whatever the hashes say")
    ap.add_argument("--prune", action="store_true",
                    help="also delete leaf nodes nothing points at any more")
    ap.add_argument("--dry-run", action="store_true",
                    help="say what would happen, write nothing")
    ap.add_argument("--verify", action="store_true",
                    help="run kb.graph.load --verify afterwards")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    verbose = not args.quiet

    try:
        if args.status:
            print(status())
            return 0
        refresh(members=args.member, prune=args.prune,
                dry_run=args.dry_run, verbose=verbose)
        if args.verify:
            from kb.graph import load
            print()
            print(load.verify())
        return 0
    except driver.GraphUnavailable as exc:
        print(f"\n  {exc}", file=sys.stderr)
        return 1
    except sqlite3.OperationalError as exc:
        print(f"\n  cannot read facts.db: {exc}", file=sys.stderr)
        print(f"  looked at {_facts_db()}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
