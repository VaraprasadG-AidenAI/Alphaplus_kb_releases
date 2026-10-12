"""
Point it at a source tree. It does the rest.

    python -m kb sync --source D:/extract/ALPHAPLUS     # cold start, end to end
    python -m kb sync --source D:/extract/ALPHAPLUS --open   # ... and show it
    python -m kb sync                                   # bring it up to date
    python -m kb sync --watch                           # ... and keep doing it
    python -m kb sync --status                          # what is stale
    python -m kb sync --watch --every 30

`--source` is the whole deployment story. Give it the path the client's 21
libraries were extracted to and it parses, indexes, projects the graph and
writes the map, with nothing else to configure: the source path is saved, so
every later run needs no argument at all.

Nothing in the pipeline is specific to this codebase. Libraries come from the
path, member types from the file suffix, menu families from the menu names
themselves, and module names from an optional JSON file somebody who knows the
business can write later. There is no built-in table of prefixes, because a
table that is right here would be silently wrong there.

Four artefacts derive from the source tree, and they go stale in this order:

    source members
      -> the relational index   (facts: kb.index.update, incremental)
        -> the Neo4j graph      (a projection: kb.graph.update, incremental)
        -> the codebase map     (one HTML file: kb.viz.build, full rewrite)

Each step already existed and each was already incremental. What did not exist
was anything that ran them in the right order, so in practice the index got
refreshed and the graph and the map quietly kept serving last week's answers.
That failure has no error message, which is the only reason it is worth a
module.

## Why a poller and not a file-system watcher

`watchdog` would give sub-second notification and is a dependency. This tree is
13,366 members on a network share in the real deployment, and the index already
uses size+mtime as a stat cache - so asking "did anything change" is the same
walk the incremental update was going to do anyway, and it costs about a second.
A 30-second poll is indistinguishable from instant for a codebase that changes
when somebody promotes a member, and it adds nothing to install.

## Why no API

Nothing here serves anything. The map is a file, the graph is a database the
MCP tools already reach, and the MCP server reads the index directly. Adding an
HTTP layer between them would be a service to run, a port to open and a thing
to authenticate, in exchange for no capability at all.

## What it will not do

Write summaries. Those cost money per member, and a source edit must never
silently start a paid run - see `kb.summarise`. `--status` reports them as
stale; bringing them back is a decision, not a refresh.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from kb import config

#: The poll interval when nothing is changing.  Long enough to be free, short
#: enough that a promoted member is picked up before anyone asks about it.
DEFAULT_EVERY = 30


# ---------------------------------------------------------------------------
# what is stale
# ---------------------------------------------------------------------------

def _newest_source(root: Path, limit: int = 60_000) -> Tuple[float, int]:
    """(newest mtime, count) under the source tree.

    One walk, no hashing.  This is only ever compared against a stored
    timestamp to decide whether to bother; the index's own size+mtime cache is
    what decides which members actually get re-parsed.
    """
    newest = 0.0
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # never descend into what we wrote ourselves
        dirnames[:] = [d for d in dirnames if not d.startswith("_alphaplus")]
        for fn in filenames:
            try:
                st = os.stat(os.path.join(dirpath, fn))
            except OSError:
                continue
            if st.st_mtime > newest:
                newest = st.st_mtime
            seen += 1
            if seen >= limit:
                return newest, seen
    return newest, seen


def _source_root() -> Path:
    """The source tree, reading the environment BEFORE the frozen config.

    `kb.config` resolves SOURCE_ROOT at import time, which is right for every
    other caller and wrong for exactly one: `adopt()` changes the source inside
    a process that has already imported config, so `config.SOURCE_ROOT` still
    holds the old tree.

    The steps themselves were never affected - they run as subprocesses and get
    the new environment - but the status report printed the previous path
    underneath a successful build of the new one, which is the sort of detail
    that makes somebody distrust the whole output.
    """
    env = os.environ.get("ALPHAPLUS_SOURCE")
    return Path(env) if env else Path(config.SOURCE_ROOT)


def _index_built_at() -> tuple:
    """(epoch, label) for the index, whichever store holds it.

    The whole staleness model here compares source mtimes against ONE number,
    and on SQLite that number is the file's mtime.  PostgreSQL has no file, so
    reading `facts.db`'s mtime there answers a question about a database nobody
    is using - and answers it "ok", which is the worst way to be wrong in a
    dashboard whose only job is to say what is stale.

    `build_info.built_at` is the honest equivalent: the build writes it, both
    stores carry it, and it is what `kb.index.status` already prints.

    Asked of BOTH stores, not just PostgreSQL, because a file existing is not
    an index existing: a 0-byte facts.db gets created by any
    `sqlite3.connect()` that never writes a schema, and the mtime version
    reported that as "ok index, 1.7h ago" while every query against it failed
    with "no such table: object". Same class of wrong answer as reading the
    file on PostgreSQL, just from the other direction.
    """
    from kb import store
    postgres = store.backend() == "postgres"
    label = (f"postgres {store.schema_from_env()}" if postgres
             else str(config.FACTS_DB))
    try:
        from datetime import datetime
        db = store.facts()
        row = db.execute(
            "SELECT value FROM build_info WHERE key='built_at'").fetchone()
        db.close()
        stamp = datetime.fromisoformat(row[0]).timestamp() if row else 0.0
    except Exception:                                     # noqa: BLE001
        stamp = 0.0
    if not stamp:
        return (0.0, f"{label} (no build_info - not indexed)")
    return (stamp, f"{label} (build_info.built_at)")


def _map_fingerprint(path: Path) -> str:
    """The graph fingerprint the map was built from, or "".

    Read by scanning for the key rather than parsing the payload: the payload
    is ~3.7 MB of JSON and this runs on every watch tick. The key appears once,
    early, and json.loads on 3.7 MB every thirty seconds to read one short
    string would be the most expensive thing in the loop.
    """
    if not path.is_file():
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(400_000)          # it sits near the front of the payload
    except OSError:
        return ""
    key = '"graphFingerprint":"'
    i = head.find(key)
    if i < 0:
        return ""
    j = head.find('"', i + len(key))
    return head[i + len(key):j] if j > 0 else ""


class _NotPackaged(Exception):
    """The graph is not part of a packaged build. Not an error."""


def status() -> Dict[str, object]:
    """What exists, what is stale, and how we know.  Reads only."""
    src = _source_root()
    facts = Path(config.FACTS_DB)
    graph_html = Path(config.DATA_DIR) / "graph.html"
    # the map lands beside the index, wherever that is
    if not graph_html.is_file():
        alt = facts.parent / "graph.html"
        if alt.is_file():
            graph_html = alt

    newest, count = _newest_source(src)
    _index_at, _index_label = _index_built_at()
    out: Dict[str, object] = {
        "source": str(src),
        "source_files": count,
        "source_newest": newest,
        "facts_db": _index_label,
        "facts_exists": _index_at > 0.0,
        "facts_mtime": _index_at,
        "map": str(graph_html),
        "map_exists": graph_html.is_file(),
        "map_mtime": graph_html.stat().st_mtime if graph_html.is_file() else 0.0,
    }

    out["index_stale"] = (not out["facts_exists"]) or newest > out["facts_mtime"]
    out["map_stale"] = (not out["map_exists"]) or out["facts_mtime"] > out["map_mtime"]

    # ...and the map is ALSO stale when the GRAPH has moved.
    #
    # The map is built from Neo4j, so comparing it against facts.db alone
    # misses every change that touched the graph and not the index. That is not
    # a theoretical gap: a parser fix reclassified 13,264 CHKOBJ edges from
    # "reads" to "references", the source never changed, `graph.update`
    # reported "0 members to refresh", and the map kept showing reads that had
    # been corrected until somebody noticed by eye.
    #
    # The map records the fingerprint it was built from. Comparing the two is
    # the only thing that closes it.
    out["graph_fp_map"] = _map_fingerprint(graph_html)
    out["graph_fp_now"] = ""
    try:
        from kb.graph import fingerprint as gfp
        out["graph_fp_now"] = gfp.as_text(gfp.current())
    except Exception:                                     # noqa: BLE001
        pass
    # Compare ONLY when both are known. An unreachable graph, or a map built
    # from SQLite, gives an empty fingerprint - and "cannot compare" must never
    # be read as "changed", or a graph that is down puts the pipeline into a
    # rebuild loop that can never succeed.
    if out["graph_fp_map"] and out["graph_fp_now"]:
        if out["graph_fp_map"] != out["graph_fp_now"]:
            out["map_stale"] = True
            out["map_note"] = (f"the graph has moved since the map was built "
                               f"({out['graph_fp_map']} -> {out['graph_fp_now']})")

    # the graph knows for itself, and says so without being told
    try:
        if config.FROZEN:
            # A packaged build has no Neo4j and no driver for one - the spec
            # excludes both. Asking anyway made the exe print
            #
            #   cannot ask the graph: GraphUnavailable: cannot reach Neo4j at
            #   bolt://127.0.0.1:7687
            #
            # on every `sync` and `status`, which reads as a broken install and
            # is about a component that is deliberately not there.
            raise _NotPackaged
        from kb.graph import settings as gs
        cfg = gs.load()
        out["backend"] = cfg.backend
        if cfg.backend == "sqlite":
            out["graph_stale"] = False
            out["graph_note"] = "graph.backend is sqlite - nothing to keep in step"
        else:
            from kb.graph import update as gupdate
            d = gupdate.diff()
            todo = len(d["added"]) + len(d["modified"]) + len(d["deleted"])
            out["graph_stale"] = todo > 0
            out["graph_todo"] = todo
            out["graph_note"] = (f"{todo} member(s) differ" if todo
                                 else "in step with the index")
    except _NotPackaged:
        out["backend"] = "sqlite"
        out["graph_stale"] = False
        out["graph_note"] = ""
    except Exception as exc:                              # noqa: BLE001
        out["graph_stale"] = None
        out["graph_note"] = f"cannot ask the graph: {type(exc).__name__}: {exc}"

    # summaries are reported, never refreshed - see the module docstring
    try:
        # store.facts(), not sqlite3.connect: this read the file directly, so
        # on PostgreSQL it fell into the except below and reported 0 summaries -
        # wrong information rather than an error, which is worse.
        from kb import store
        db = store.facts()
        out["summaries"] = db.execute("SELECT COUNT(*) FROM summary").fetchone()[0]
        db.close()
    except Exception:                                     # noqa: BLE001
        out["summaries"] = 0
    return out


def report(st: Optional[Dict[str, object]] = None) -> str:
    st = st or status()
    def age(t: float) -> str:
        if not t:
            return "never"
        d = time.time() - t
        if d < 90:
            return f"{d:.0f}s ago"
        if d < 5400:
            return f"{d / 60:.0f}m ago"
        if d < 172800:
            return f"{d / 3600:.1f}h ago"
        return f"{d / 86400:.1f}d ago"

    # The graph row is omitted entirely when there is no graph - a packaged
    # build is SQLite only. A row saying "nothing to keep in step" about a
    # component that is not installed is noise on every single run.
    mark = lambda stale: "STALE" if stale else ("ok   " if stale is False else "?    ")
    lines = [
        f"  source   {st['source']}",
        f"           {st['source_files']:,} files, newest {age(st['source_newest'])}",
        "",
        f"  {mark(st['index_stale'])} index    {age(st['facts_mtime'])}   {st['facts_db']}",
        f"  {mark(st['map_stale'])} map      {age(st['map_mtime'])}   {st['map']}",
        f"  --    summaries {st['summaries']:,} page(s) - refreshed only on request, "
        f"because they cost money",
    ]
    if st.get("graph_note"):
        lines.insert(4, f"  {mark(st['graph_stale'])} graph    {st['graph_note']}")
    todo = [k for k in ("index", "graph", "map") if st.get(f"{k}_stale")]
    lines.append("")
    lines.append(f"  {len(todo)} stale: {', '.join(todo)}" if todo
                 else "  Everything is in step with the source.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# doing it
# ---------------------------------------------------------------------------

#: `python -m ...` form -> the frozen executable's own subcommand.
#:
#: A step is launched as a subprocess on purpose (see `_run`), and outside the
#: exe that is `python -m kb refresh`. Inside the exe there IS no python:
#: `sys.executable` is alphaplus-kb.exe, which has never accepted `-m`, so
#: every step failed in under a second with
#:
#:     error: argument command: invalid choice: 'kb'
#:
#: and `sync` reported "stopped at index" having done nothing at all. It was
#: broken in every packaged build until this was found. `index` and `refresh`
#: were unaffected - they run in-process - which is why it survived so long.
_FROZEN_STEP = {
    ("-m", "kb", "refresh"): ("refresh",),
    ("-m", "kb", "lexicon"): ("lexicon",),
    # No packaged equivalent, and none is needed: the graph step only runs
    # when the backend is Neo4j, and a packaged build is always SQLite. None
    # means "skip, and say why" rather than "crash".
    ("-m", "kb.graph.update"): None,
    ("-m", "kb.viz.build"): ("graph",),
}


def _command(argv: List[str]) -> Optional[List[str]]:
    """The argv to actually launch, or None if this step cannot run packaged."""
    if not config.FROZEN:
        return [sys.executable, *argv]
    key = tuple(argv)
    if key not in _FROZEN_STEP:
        # Name the step that has no mapping. A step added later without one
        # used to surface as an argparse complaint about 'kb', which points
        # at nothing a reader can act on.
        raise KeyError(
            f"no packaged equivalent for {' '.join(argv)} - add it to "
            f"kb.sync._FROZEN_STEP")
    mapped = _FROZEN_STEP[key]
    if mapped is None:
        return None
    return [str(Path(sys.executable).resolve()), *mapped]


def _run(label: str, argv: List[str], quiet: bool) -> bool:
    """One step, as a subprocess.

    Subprocess rather than an in-process call, deliberately: `kb.config` freezes
    its paths at import time and the index build swaps the database file under
    whoever has it open. A fresh interpreter per step is the difference between
    a refresh that works twice and one that works once.
    """
    if not quiet:
        print(f"\n  -> {label}")
    t0 = time.perf_counter()
    command = _command(argv)
    if command is None:
        if not quiet:
            print("     not available in the packaged build - skipped")
        return False
    proc = subprocess.run(command, capture_output=quiet, text=True)
    took = time.perf_counter() - t0
    ok = proc.returncode == 0
    if not quiet:
        print(f"     {'done' if ok else 'FAILED'} in {took:.1f}s")
    elif not ok:
        print(f"  {label} FAILED", file=sys.stderr)
        if proc.stderr:
            print(proc.stderr[-1200:], file=sys.stderr)
    return ok


def adopt(source: str, quiet: bool = False) -> bool:
    """Point the knowledge base at a source tree and remember it.

    Saved rather than passed every time, because the MCP server, the loader and
    the viewer are separate processes and an argument only one of them saw is
    how three components end up reading three different trees.

    An existing index built from a DIFFERENT tree is left alone rather than
    updated: its stored hashes belong to another codebase, so a diff against
    them is meaningless. `once()` detects that and does a full build.
    """
    root = Path(source).expanduser()
    if not root.is_dir():
        print(f"  not a directory: {root}", file=sys.stderr)
        return False

    members = 0
    for _dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith("_alphaplus")]
        members += len(filenames)
        if members > 200:
            break
    if members == 0:
        print(f"  {root} has no files in it", file=sys.stderr)
        return False

    from kb import bootstrap
    settings = bootstrap.load_settings()
    was = settings.get("source")
    settings["source"] = str(root)
    where = bootstrap.save_settings(settings)
    os.environ["ALPHAPLUS_SOURCE"] = str(root)

    if not quiet:
        print('\n' + f"  source   {root}")
        if was and str(was) != str(root):
            print(f"  was      {was}")
            print("  The index will be rebuilt from scratch - the stored hashes")
            print("  belong to the other tree.")
        print(f"  saved to {where}")
    return True


def ensure_graph(quiet: bool = False) -> bool:
    """Make sure Neo4j is answering, starting it if we can find it.

    Best effort, and deliberately so. Every outcome below is normal somewhere:

      already up          the usual case, and the cheapest - a port probe
      started by us       a developer laptop, or a VDI where we installed it
      cannot find one     Neo4j is simply not deployed here
      refused to start    autostart is off, which production REQUIRES, because
                          a second JVM against a managed store corrupts it

    None of those may stop the pipeline: the index and the map are built from
    facts.db and do not need the graph at all. So this reports what happened
    and returns, and the caller carries on either way.
    """
    try:
        from kb.graph import process as P, settings as gs
    except Exception:                                     # noqa: BLE001
        return False
    cfg = gs.load()
    host, port = P.split_host_port(cfg.neo4j.uri)
    if P.is_listening(host, port):
        return True
    home = P.resolve_home(cfg)
    if not home:
        if not quiet:
            print("  graph: Neo4j is not running and no installation was found"
                  " - skipping", file=sys.stderr)
        return False
    if not cfg.autostart.enabled:
        if not quiet:
            print(f"  graph: Neo4j is not running at {host}:{port}. Autostart"
                  " is off, so start it yourself:\n"
                  "         python -m kb.graph.process --start", file=sys.stderr)
        return False
    if not quiet:
        print(f"  graph: starting Neo4j from {home}")
    try:
        P.start(cfg, wait=True)
    except Exception as exc:                              # noqa: BLE001
        if not quiet:
            print(f"  graph: could not start Neo4j - {exc}", file=sys.stderr)
        return False
    return P.is_listening(host, port)


def once(force: bool = False, quiet: bool = False) -> Dict[str, object]:
    """Bring the index, the graph and the map up to date. Least work first."""
    st = status()
    did: List[str] = []

    if force or st["index_stale"]:
        if not _run("index: re-parse what changed", ["-m", "kb", "refresh"], quiet):
            return {"ok": False, "step": "index", "did": did}
        did.append("index")
        st = status()          # the graph's staleness depends on the new index

    # The lexicon is derived from the index and costs seconds, but only
    # `kb setup` ever offered to build it - and setup is interactive, so a
    # box brought up through sync alone ran with degraded ranking and nothing
    # said so.  Seed it whenever it is missing; re-seeding on every index
    # change stays a deliberate `kb lexicon`, because the manual half of the
    # glossary is curated and churn there should be a choice.
    from .lexicon import terms as _terms
    if not _terms.SEEDED.exists():
        if _run("lexicon: seed the business-term glossary",
                ["-m", "kb", "lexicon"], quiet):
            did.append("lexicon")

    if st.get("backend") != "sqlite" and (force or st.get("graph_stale")):
        # Bring the graph up first if it is not already. On a VDI nobody is
        # going to remember to start Neo4j before running the pipeline, and a
        # refresh against a dead port is a confusing way to find that out.
        # `ensure_graph` is best-effort by design: it reports and moves on.
        ensure_graph(quiet)
        if not _run("graph: refresh the changed members",
                    ["-m", "kb.graph.update"], quiet):
            # A graph that will not refresh must not stop the map being
            # rebuilt: the map reads the index, not the graph.
            print("  graph refresh failed - carrying on to the map",
                  file=sys.stderr)
        else:
            did.append("graph")

    if force or did or st["map_stale"]:
        if not _run("map: rewrite the codebase map",
                    ["-m", "kb.viz.build"], quiet):
            return {"ok": False, "step": "map", "did": did}
        did.append("map")

    if not did and not quiet:
        print("  Nothing to do.")
    return {"ok": True, "did": did}


def watch(every: int = DEFAULT_EVERY, quiet: bool = False) -> int:
    """Poll, and rebuild whatever the source has moved past.

    Ctrl-C is the way out and is not an error. Anything else is logged and the
    loop continues: a watcher that dies on one bad member is worse than one that
    reports it and keeps going, because nobody is watching the watcher.
    """
    print(f"  watching {_source_root()}")
    print(f"  every {every}s   Ctrl-C to stop\n")
    last_seen = 0.0
    runs = 0
    try:
        while True:
            newest, count = _newest_source(_source_root())
            st = status()
            stale = st["index_stale"] or st.get("graph_stale") or st["map_stale"]
            if stale:
                stamp = time.strftime("%H:%M:%S")
                what = [k for k in ("index", "graph", "map") if st.get(f"{k}_stale")]
                print(f"  [{stamp}] stale: {', '.join(what)} - rebuilding")
                out = once(quiet=quiet)
                runs += 1
                print(f"  [{time.strftime('%H:%M:%S')}] "
                      f"{'refreshed ' + ', '.join(out['did']) if out.get('did') else 'nothing changed'}"
                      f"   (run {runs})\n")
            last_seen = newest
            time.sleep(max(2, every))
    except KeyboardInterrupt:
        print(f"\n  stopped after {runs} rebuild(s)")
        return 0


def _open_map() -> bool:
    """Show the map that was just built.

    Note what this is NOT: there is no server to start. The map is one HTML
    file with the React bundle and the whole payload inlined, so "opening the
    viewer" is opening a file. That is the point - it has to work on a VDI with
    no node, no npm and no network.

    `viz-src/dev.mjs` serves the same app on a port with hot reload, but it is a
    development tool and is deliberately not wired in here: it needs node and a
    populated node_modules, neither of which the target machine has.
    """
    st = status()
    path = Path(str(st.get("map", "")))
    if not path.is_file():
        print("\n  No map to open - it has not been built yet.", file=sys.stderr)
        return False
    try:
        import webbrowser
        if webbrowser.open(path.resolve().as_uri()):
            print(f"\n  Opened the codebase map: {path}")
            return True
    except Exception:
        pass
    # A headless or locked-down desktop is a normal outcome, not a failure -
    # print the path so it is still one copy-paste away.
    print("\n  Could not launch a browser. Open this file by hand:")
    print(f"    {path}")
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="keep the index, graph and codebase map in step with the source")
    ap.add_argument("--source", metavar="PATH",
                    help="point the knowledge base at this source tree, save "
                         "the path, and build everything from it")
    ap.add_argument("--status", action="store_true",
                    help="what is stale; change nothing")
    ap.add_argument("--watch", action="store_true",
                    help="keep checking, and rebuild when the source moves")
    ap.add_argument("--every", type=int, default=DEFAULT_EVERY,
                    metavar="SECONDS", help=f"poll interval (default {DEFAULT_EVERY})")
    ap.add_argument("--auto", action="store_true",
                    help="shorthand for --watch --every 10, for a live demo")
    ap.add_argument("--force", action="store_true",
                    help="rebuild everything even if nothing looks stale")
    ap.add_argument("--open", action="store_true", dest="open_map",
                    help="open the codebase map when it has been rebuilt")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.source:
        if not adopt(args.source, args.quiet):
            return 2
        # A new tree means everything downstream is stale by definition, so the
        # first run after adopting is always a full one.
        args.force = True

    if args.status:
        print(report())
        return 0
    if args.auto:
        args.watch = True
        args.every = min(args.every, 10)
    if args.watch:
        return watch(args.every, args.quiet)

    print(report())
    out = once(force=args.force, quiet=args.quiet)
    if out.get("ok"):
        print(f"\n  refreshed: {', '.join(out['did']) or 'nothing'}")
        if args.open_map:
            _open_map()
        return 0
    print(f"\n  stopped at {out.get('step')}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
