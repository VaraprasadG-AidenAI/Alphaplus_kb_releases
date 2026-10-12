"""
End-to-end test: source tree -> index -> graph -> tools -> answers.

    python -m kb.e2e                          check the current state
    python -m kb.e2e --store postgres         ... on a specific store
    python -m kb.e2e --all                    every store/graph combination
    python -m kb.e2e --rebuild                REBUILD the index first, then check
    python -m kb.e2e --list                   what each stage does, and skip nothing

Everything this runs already existed as a separate command.  What did not exist
was one thing that runs them in the right order, against a named combination of
stores, and prints a single verdict.  Nine `python -m` invocations remembered in
the right sequence is not a test, it is a ritual.

## Stages, and why in this order

Each stage can only be trusted if the one before it passed, so a failure stops
the dependent stages rather than reporting a cascade:

    1  preflight     the store answers, the source tree is readable
    2  build         (--rebuild only) index the source
    3  schema        the PostgreSQL DDL still matches kb/index/schema.sql
    4  parity        both stores hold the same row count in every table
    5  graph load    (--rebuild only) facts -> Neo4j
    6  offline       kb.index.selftest, kb.store.sql, kb.store.rows
    7  graph         kb.graph.selftest - 8 checks, needs a live Neo4j
    8  tools         every declared MCP tool answers without raising
    9  retrieval     kb.eval.probe - 40 tickets, no LLM, deterministic
    10 map           the HTML map builds
    11 cross-store   every tool's OUTPUT, SQLite against PostgreSQL

Stage 11 is the one that catches a silent divergence, and it is the reason this
module runs stages as SUBPROCESSES with a prepared environment rather than
importing and calling: `kb.server.tools` caches its connection in a module
global, so switching stores in-process gives you whichever store got there
first.  A subprocess per store is the only honest way to compare them.

## What a SKIP means

A stage that cannot run says SKIP and explains, and SKIP never fails the run.
Neo4j not installed, no DSN set, the vector store absent - none of those are
defects in the knowledge base, and a harness that reports them as failures is
one nobody runs twice.  A stage that could run and did not pass says FAIL.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]

PASS, FAIL, SKIP = "pass", "FAIL", "skip"


class Stage:
    """One step, its command, and how to tell whether it worked."""

    def __init__(self, name: str, argv: Sequence[str], *,
                 expect: str = "", forbid: str = "",
                 needs: Sequence[str] = (), timeout: int = 900,
                 detail_re: str = "", destructive: bool = False):
        self.name = name
        self.argv = list(argv)
        #: a string that MUST appear in the output.  Exit code alone is not
        #: enough: several of these commands print failures and exit 0.
        self.expect = expect
        #: a string that must NOT appear
        self.forbid = forbid
        #: capability names that have to be available
        self.needs = list(needs)
        self.timeout = timeout
        self.detail_re = detail_re
        self.destructive = destructive


def _run(stage: Stage, env: Dict[str, str]) -> Tuple[str, str, float]:
    t0 = time.perf_counter()
    try:
        p = subprocess.run([sys.executable, "-m"] + stage.argv,
                           capture_output=True, text=True, cwd=str(ROOT),
                           env=env, timeout=stage.timeout)
    except subprocess.TimeoutExpired:
        return FAIL, f"timed out after {stage.timeout}s", time.perf_counter() - t0
    took = time.perf_counter() - t0
    out = (p.stdout or "") + (p.stderr or "")

    if stage.expect and stage.expect not in out:
        last = [l for l in out.splitlines() if l.strip()][-1:] or [""]
        return FAIL, f"expected {stage.expect!r}; last line: {last[0].strip()[:60]}", took
    if stage.forbid and stage.forbid in out:
        return FAIL, f"output contained {stage.forbid!r}", took
    if p.returncode != 0 and not stage.expect:
        last = [l for l in out.splitlines() if l.strip()][-1:] or [""]
        return FAIL, f"exit {p.returncode}: {last[0].strip()[:60]}", took

    detail = ""
    if stage.detail_re:
        import re
        m = re.search(stage.detail_re, out)
        if m:
            detail = " ".join(m.group(0).split())
    return PASS, detail, took


# =======================================================================
# What is available
# =======================================================================

def capabilities(store: str) -> Dict[str, str]:
    """What can be tested here.  Reasons, not booleans, so SKIP can explain."""
    caps: Dict[str, str] = {}

    from kb import config
    caps["source"] = ("" if Path(config.SOURCE_ROOT).is_dir()
                      else f"source tree missing: {config.SOURCE_ROOT}")

    if store == "postgres":
        dsn = os.environ.get("ALPHAPLUS_PG_DSN", "")
        if not dsn:
            caps["store"] = "ALPHAPLUS_PG_DSN is not set"
        else:
            try:
                import psycopg
                with psycopg.connect(dsn, connect_timeout=5) as c:
                    c.execute("SELECT 1")
                caps["store"] = ""
            except Exception as exc:                    # noqa: BLE001
                caps["store"] = f"postgres unreachable: {str(exc)[:50]}"
    else:
        caps["store"] = ("" if Path(config.FACTS_DB).is_file()
                         else f"no index at {config.FACTS_DB}")

    # Whether a SQLite index exists is a SEPARATE capability from which store
    # is selected: the cross-store stages read both, and a Postgres-only
    # install has nothing to compare against. Without this they FAILED on a
    # perfectly healthy machine, which is the "harness nobody runs twice"
    # failure the module docstring warns about.
    #
    # A 0-byte facts.db counts as absent - any sqlite3.connect() that never
    # writes a schema leaves one behind, and it is not an index.
    try:
        sqlite_ok = (Path(config.FACTS_DB).is_file()
                     and Path(config.FACTS_DB).stat().st_size > 0)
    except OSError:
        sqlite_ok = False
    caps["sqlite_index"] = ("" if sqlite_ok
                            else f"no SQLite index at {config.FACTS_DB}")

    try:
        import neo4j                                    # noqa: F401
        from kb.graph import process, settings as gs
        cfg = gs.load()
        host, port = process.split_host_port(cfg.neo4j.uri)
        caps["neo4j"] = ("" if process.is_listening(host, port)
                         else f"nothing listening on {host}:{port}")
    except ImportError:
        caps["neo4j"] = "the neo4j driver is not installed"
    except Exception as exc:                            # noqa: BLE001
        caps["neo4j"] = f"cannot tell: {str(exc)[:40]}"
    return caps


# =======================================================================
# The plan
# =======================================================================

def plan(store: str, graph: str, rebuild: bool) -> List[Stage]:
    s: List[Stage] = []

    if rebuild:
        s.append(Stage("build index", ["kb.index.build"],
                       expect="parse status:", needs=["source", "store"],
                       timeout=1800, destructive=True))

    if store == "postgres":
        s.append(Stage("schema matches", ["kb.store.ddl", "--check", "--dsn",
                                          os.environ.get("ALPHAPLUS_PG_DSN", "")],
                       expect="schema matches schema.sql", needs=["store"],
                       detail_re=r"\d+ tables expected, \d+ present, \d+ indexes"))
        s.append(Stage("both stores agree", ["kb.store.load", "--dsn",
                                            os.environ.get("ALPHAPLUS_PG_DSN", ""),
                                            "--verify"],
                       expect="same number of rows in every table",
                       needs=["store", "sqlite_index"],
                       detail_re=r"TOTAL\s+[\d,]+\s+[\d,]+"))

    if rebuild:
        s.append(Stage("load graph", ["kb.graph.load"],
                       expect="", needs=["store", "neo4j"], timeout=1800,
                       destructive=True))

    s.append(Stage("index selftest", ["kb.index.selftest"],
                   expect="all cases pass", needs=[]))
    s.append(Stage("sql translation", ["kb.store.sql", "--selftest", "--dsn",
                                       os.environ.get("ALPHAPLUS_PG_DSN", "")],
                   expect="all cases pass", needs=["store"]
                   if store == "postgres" else []))
    s.append(Stage("row compatibility", ["kb.store.rows", "--selftest"],
                   expect="0 disagreement", needs=[]))
    s.append(Stage("graph selftest", ["kb.graph.selftest"],
                   expect="ALL CHECKS PASSED", needs=["neo4j"],
                   detail_re=r"ALL CHECKS PASSED"))
    s.append(Stage("graph vs store", ["kb.graph.load", "--verify"],
                   expect="No faults", needs=["store", "neo4j"], timeout=600))
    s.append(Stage("all tools", ["kb.server.server", "--selftest"],
                   expect="all tools working", forbid="  FAIL ",
                   needs=["store"]))
    # Paging is checked separately from "all tools" because a paged answer
    # can be perfectly well-formed on every page and still be wrong across
    # them: the selftest asks one page and cannot see an overlap.
    s.append(Stage("paging partitions", ["kb.server.pagecheck"],
                   expect="PAGING IS STABLE", forbid="  FAIL ",
                   needs=["store"], timeout=600))
    s.append(Stage("retrieval probe", ["kb.eval.probe"],
                   expect="gold names found", needs=["store"], timeout=600,
                   detail_re=r"OVERALL\s+\d+\s+\d+%\s+\d+/\d+"))
    s.append(Stage("doctor", ["kb", "doctor"], expect="index built",
                   needs=["store"], timeout=300))
    return s


def _env_for(store: str, graph: str) -> Dict[str, str]:
    env = dict(os.environ)
    env["ALPHAPLUS_STORE"] = store
    env["ALPHAPLUS_GRAPH_BACKEND"] = graph
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# =======================================================================
# Cross-store output comparison - stage 11
# =======================================================================

_DUMP = r'''
import json, sys, inspect
import kb.server.server as S
src = inspect.getsource(S.selftest)
start = src.index("probes: List[tuple] = [")
end = src.index("]", src.index('"what_uses", {"name": "ORD100D"}'))
ns = {}
exec("probes = [" + src[start + len("probes: List[tuple] = ["):end] + "]", ns)
out = {}
for i, (name, args) in enumerate(ns["probes"]):
    key = "%02d:%s" % (i, name)
    try:
        out[key] = S.HANDLERS[name](**args)
    except Exception as exc:
        out[key] = "__ERROR__ %s: %s" % (type(exc).__name__, exc)
json.dump(out, open(sys.argv[1], "w", encoding="utf-8"))
'''


def cross_store(graph: str, tmp: Path,
                verbose: bool = True) -> Tuple[str, str]:
    """Run every tool under BOTH stores and diff the text they produced."""
    if not os.environ.get("ALPHAPLUS_PG_DSN"):
        return SKIP, "ALPHAPLUS_PG_DSN is not set"
    # The same 0-byte-aware test capabilities() uses, so this stage and
    # "both stores agree" cannot disagree about whether SQLite is there.
    if capabilities("postgres").get("sqlite_index"):
        return SKIP, "no SQLite index to compare against"

    script = tmp / "_e2e_dump.py"
    script.write_text(_DUMP, encoding="utf-8")
    got: Dict[str, dict] = {}
    for store in ("sqlite", "postgres"):
        target = tmp / f"_e2e_{store}.json"
        p = subprocess.run([sys.executable, str(script), str(target)],
                           capture_output=True, text=True, cwd=str(ROOT),
                           env=_env_for(store, graph), timeout=900)
        if not target.is_file():
            last = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
            return FAIL, f"{store} dump failed: {(last or [''])[-1][:60]}"
        got[store] = json.loads(target.read_text(encoding="utf-8"))

    a, b = got["sqlite"], got["postgres"]
    errs = [k for k in a if str(a[k]).startswith("__ERROR__")
            or str(b[k]).startswith("__ERROR__")]
    if errs:
        return FAIL, f"{len(errs)} tool(s) raised: {', '.join(sorted(errs)[:3])}"
    diff = [k for k in a if a[k] != b[k]]
    same = len(a) - len(diff)
    if verbose and diff:
        for k in diff:
            print(f"        differs: {k.split(':')[1]} "
                  f"({len(a[k])} vs {len(b[k])} chars)")
    # Two tools are expected to differ, both for the SAME reason and both
    # named rather than pattern-matched, so that a third one appearing is a
    # finding rather than something the allowlist quietly swallows:
    #
    #   route_ticket   ranks written pages with bm25 on SQLite and
    #                  ts_rank * idf on PostgreSQL
    #   trace_ticket   prints route_ticket's first four rows as its header
    #
    # No configuration makes those identical - bm25 and ts_rank saturate term
    # frequency and normalise length differently. What was made to match is the
    # part that mattered: both now weight rare terms over common ones. See
    # kb/server/routing.py.
    KNOWN_DIFFERENT = ("route_ticket", "trace_ticket")
    unexpected = [k for k in diff
                  if not k.split(":")[1] in KNOWN_DIFFERENT]
    if unexpected:
        return FAIL, (f"{same}/{len(a)} identical; UNEXPECTED difference in "
                      f"{', '.join(k.split(':')[1] for k in unexpected)}")
    if diff:
        named = ", ".join(sorted(k.split(":")[1] for k in diff))
        return PASS, f"{same}/{len(a)} byte-identical ({named} differ, as documented)"
    return PASS, f"{same}/{len(a)} byte-identical"


# =======================================================================
# Driving it
# =======================================================================

def run(store: str, graph: str, rebuild: bool, tmp: Path,
        skip_cross: bool = False) -> bool:
    print()
    print(f"  {'=' * 72}")
    print(f"  END TO END   relational={store}  graph={graph}"
          + ("   REBUILDING" if rebuild else ""))
    print(f"  {'=' * 72}")

    caps = capabilities(store)
    unavailable = {k: v for k, v in caps.items() if v}
    if unavailable:
        print("  not available here:")
        for k, why in unavailable.items():
            print(f"    {k:<10}{why}")
        print()

    env = _env_for(store, graph)
    results: List[Tuple[str, str, str, float]] = []
    blocked = False

    for st in plan(store, graph, rebuild):
        missing = [n for n in st.needs if caps.get(n)]
        if missing:
            results.append((st.name, SKIP,
                            f"needs {missing[0]}: {caps[missing[0]]}", 0.0))
            continue
        if blocked:
            results.append((st.name, SKIP, "an earlier stage failed", 0.0))
            continue
        print(f"    running {st.name} ...", flush=True)
        verdict, detail, took = _run(st, env)
        results.append((st.name, verdict, detail, took))
        if verdict == FAIL and st.name in ("build index", "schema matches"):
            blocked = True

    if not skip_cross:
        print("    running cross-store comparison ...", flush=True)
        v, d = cross_store(graph, tmp)
        results.append(("cross-store output", v, d, 0.0))

    print()
    print(f"    {'stage':<22}{'':<6}{'time':>8}  detail")
    print(f"    {'-' * 70}")
    for name, verdict, detail, took in results:
        t = f"{took:.1f}s" if took else ""
        print(f"    {name:<22}{verdict:<6}{t:>8}  {detail}")

    failed = [r for r in results if r[1] == FAIL]
    skipped = [r for r in results if r[1] == SKIP]
    print(f"    {'-' * 70}")
    print(f"    {len(results) - len(failed) - len(skipped)} passed, "
          f"{len(failed)} failed, {len(skipped)} skipped")
    return not failed


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="end-to-end test of the knowledge base")
    ap.add_argument("--store", choices=("sqlite", "postgres"), default=None,
                    help="relational store (default: ALPHAPLUS_STORE, or sqlite)")
    ap.add_argument("--graph", choices=("sqlite", "neo4j", "compare"),
                    default=None, help="graph backend (default: neo4j)")
    ap.add_argument("--all", action="store_true",
                    help="every store/graph combination")
    ap.add_argument("--rebuild", action="store_true",
                    help="REBUILD the index and reload the graph first")
    ap.add_argument("--no-cross", action="store_true",
                    help="skip the cross-store output comparison")
    ap.add_argument("--list", action="store_true", help="print the plan only")
    a = ap.parse_args(argv)

    if a.list:
        print(__doc__)
        return 0

    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="alphaplus-e2e-"))

    combos: List[Tuple[str, str]]
    if a.all:
        combos = [(s, g) for s in ("sqlite", "postgres")
                  for g in ("sqlite", "neo4j")]
    else:
        # store.backend(), not a literal: it resolves a configured DSN to
        # postgres, and hardcoding "sqlite" here made a bare `kb.e2e` test
        # the store nobody is using and fail four stages on a healthy box.
        from kb import store as kbstore
        store = a.store or kbstore.backend()
        graph = a.graph or os.environ.get("ALPHAPLUS_GRAPH_BACKEND") or "neo4j"
        combos = [(store, graph)]

    if a.rebuild:
        print()
        print("  --rebuild REBUILDS THE INDEX. On SQLite that deletes facts.db")
        print("  and recreates it; the written pages are carried forward, but")
        print("  only if the old file is readable. On PostgreSQL the previous")
        print("  schema is kept as kb_old and can be renamed back.")
        if len(combos) > 1:
            print("  Refusing to rebuild once per combination - run --rebuild")
            print("  for one combination, then check the others without it.")
            return 2

    ok = True
    for store, graph in combos:
        ok &= run(store, graph, a.rebuild, tmp, skip_cross=a.no_cross)

    print()
    print("  END TO END: PASS" if ok else "  END TO END: FAILURES ABOVE")
    print()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
