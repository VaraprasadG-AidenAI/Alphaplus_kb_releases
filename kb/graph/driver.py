"""
One Neo4j driver for the process, with the settings applied and health checks.

    from kb.graph import driver
    with driver.session() as s:
        rows = driver.read(s, "MATCH (m:Member) RETURN count(m) AS n")

## Why a module-level driver rather than one per call

The official driver *is* the connection pool.  Creating one per query throws the
pool away every time and pays TCP and auth on each call, which is exactly the
shape that made the 90 ms cold start show up in the published benchmarks.  One
driver, opened lazily, closed at exit.

## Retries

`session.execute_read` and `execute_write` already retry transient failures -
leader switches, deadlocks - for `max_transaction_retry_time`.  That is the
supported way and this module uses it rather than hand-rolling a loop.  What it
does NOT retry is a syntax error or a constraint violation, which are ours to
fix, so those surface immediately.

## The one thing this module refuses to do

There is no `run_many_in_one_transaction` helper here.  An unbounded
transaction holding every statement is what kept stopping the discovery agent -
see docs/NEO4J-LIMITS-REPORT.md.  Bulk writes go through `kb/graph/load.py`,
which batches with `CALL { } IN TRANSACTIONS OF n ROWS`, and there is
deliberately no shortcut past it.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from kb.graph import settings as gs

# The driver logs connection failures and retry notices at WARNING, straight to
# stderr.  With neo4j as the default backend a single unreachable server would
# print a paragraph of stack per tool call, and stderr is where our own
# one-line notice goes.  Ours is enough; the driver's is noise.
logging.getLogger("neo4j").setLevel(logging.ERROR)
logging.getLogger("neo4j.pool").setLevel(logging.ERROR)
logging.getLogger("neo4j.io").setLevel(logging.ERROR)

try:
    from neo4j import GraphDatabase, Driver, Session
    from neo4j.exceptions import (
        AuthError, ServiceUnavailable, ConfigurationError, Neo4jError,
    )
    HAVE_DRIVER = True
except ImportError:                                    # pragma: no cover
    GraphDatabase = None                               # type: ignore
    Driver = Session = object                          # type: ignore
    AuthError = ServiceUnavailable = ConfigurationError = Neo4jError = Exception  # type: ignore
    HAVE_DRIVER = False


DRIVER_HINT = (
    "the neo4j Python driver is not installed.\n"
    "    pip install -r requirements.txt\n"
    "  or, for just this:\n"
    "    pip install 'neo4j>=5.14'"
)


class GraphUnavailable(RuntimeError):
    """Neo4j cannot be reached, or cannot be used.  Message is user-facing."""


_lock = threading.Lock()
_driver: Optional[Driver] = None
_settings: Optional[gs.Settings] = None


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

def get(cfg: Optional[gs.Settings] = None) -> Driver:
    """The process-wide driver, opened on first use."""
    global _driver, _settings
    if not HAVE_DRIVER:
        raise GraphUnavailable(DRIVER_HINT)
    with _lock:
        if _driver is not None:
            return _driver
        _settings = cfg or gs.load()
        n = _settings.neo4j

        # If the server is not up and we are allowed to start it, do that first.
        #
        # Here rather than in the MCP server's start-up because EVERY entry
        # point needs it - the loader, selftest, bench, the CLI - and wiring it
        # in one place per caller is how one of them gets forgotten.  This is
        # the single choke point: nothing talks to Neo4j without passing
        # through get().
        #
        # It never raises and it is tried once per process.  A failed autostart
        # must fall through to the existing "unreachable -> SQLite" path, not
        # replace a working fallback with a crash.
        # NOTHING HERE SPAWNS A DATABASE.
        #
        # This used to autostart a local Neo4j on first use. Two things were
        # wrong with it and the second is the reason it is gone.
        #
        # It shipped ON in the committed alphaplus.toml, and kb/config.py
        # finds that file by walking the parents of its own path - so any
        # checkout underneath it opted in silently, and an ordinary tool call
        # printed "[graph] starting Neo4j ..." and launched a JVM nobody had
        # asked for.
        #
        # And the product it was launching for does not exist. The packaged
        # knowledge base is SQLite: the spec bundles no Neo4j, no driver and
        # no JVM. Starting a database for a graph backend the deliverable
        # never contains cannot help anyone, and reads as a half-installed
        # product to whoever sees the line.
        #
        # A machine that genuinely runs Neo4j starts it the way it starts any
        # other service. kb/graph/process.py is still there and still works
        # when a person runs it deliberately:
        #
        #     python -m kb.graph.process --start
        #

        try:
            _driver = GraphDatabase.driver(
                n.uri,
                auth=(n.user, n.password),
                max_connection_pool_size=n.pool.max_connection_pool_size,
                connection_timeout=n.pool.connection_timeout_seconds,
                max_transaction_retry_time=n.pool.max_transaction_retry_time,
                connection_acquisition_timeout=n.pool.connection_acquisition_timeout,
                # Neo4j emits an advisory notification for things like
                # "null value eliminated in set function", which OPTIONAL MATCH
                # legitimately produces.  The driver logs them to stderr, where
                # they get read as tool output.  WARNING and above only.
                notifications_min_severity="WARNING",
                notifications_disabled_classifications=["UNRECOGNIZED",
                                                        "UNSUPPORTED",
                                                        "GENERIC"],
            )
        except ConfigurationError as exc:
            raise GraphUnavailable(f"bad Neo4j configuration: {exc}") from None

        # Prove the server is there before any query runs.
        #
        # Without this, the first query enters the driver's managed-transaction
        # retry loop and keeps trying for max_transaction_retry_time - 30s by
        # default.  A tool pointed at a dead Neo4j appeared to hang for half a
        # minute before falling back, which reads as a broken server rather
        # than a missing one.  verify_connectivity respects connection_timeout
        # and does not retry, so failure is fast and the fallback is immediate.
        try:
            _driver.verify_connectivity()
        except Exception as exc:                        # noqa: BLE001
            try:
                _driver.close()
            except Exception:
                pass
            _driver = None
            raise GraphUnavailable(
                f"cannot reach Neo4j at {n.uri}: "
                f"{str(exc).splitlines()[0]}") from None
        return _driver


def close() -> None:
    """Drain the pool.  Registered at exit; safe to call twice."""
    global _driver
    with _lock:
        if _driver is not None:
            try:
                _driver.close()
            except Exception:
                pass
            _driver = None


atexit.register(close)


def current_settings() -> gs.Settings:
    global _settings
    if _settings is None:
        _settings = gs.load()
    return _settings


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

def session(**kw: Any) -> Session:
    """A session on the configured database."""
    cfg = current_settings()
    return get(cfg).session(database=cfg.neo4j.database, **kw)


def read(cypher: str, params: Optional[Dict[str, Any]] = None,
         *, cap: Optional[int] = None) -> List[Dict[str, Any]]:
    """Run a read query and return plain dicts.

    Rows are capped.  A tool that asks an unbounded question against a cyclic
    call graph can otherwise pull the whole graph into memory and time out
    looking like a hang rather than a bad query.
    """
    cfg = current_settings()
    limit = cap if cap is not None else cfg.query.max_rows

    def _work(tx) -> List[Dict[str, Any]]:
        result = tx.run(cypher, params or {},
                        timeout=cfg.query.timeout_seconds)
        out: List[Dict[str, Any]] = []
        for record in result:
            out.append(record.data())
            if len(out) >= limit:
                result.consume()
                break
        return out

    try:
        with session() as s:
            return s.execute_read(_work)
    except AuthError as exc:
        raise GraphUnavailable(
            f"Neo4j rejected the credentials for {cfg.neo4j.user}: {exc}") from None
    except ServiceUnavailable as exc:
        raise GraphUnavailable(
            f"cannot reach Neo4j at {cfg.neo4j.uri}: {exc}") from None


def write(cypher: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run one write statement and return its counters.

    For anything bulk use kb.graph.load - this is for schema statements and
    single-row maintenance.
    """
    cfg = current_settings()

    def _work(tx) -> Dict[str, Any]:
        result = tx.run(cypher, params or {})
        summary = result.consume()
        c = summary.counters
        return {
            "nodes_created": c.nodes_created,
            "nodes_deleted": c.nodes_deleted,
            "relationships_created": c.relationships_created,
            "relationships_deleted": c.relationships_deleted,
            "properties_set": c.properties_set,
            "labels_added": c.labels_added,
            "indexes_added": c.indexes_added,
            "constraints_added": c.constraints_added,
            "ms": summary.result_available_after,
        }

    try:
        with session() as s:
            return s.execute_write(_work)
    except AuthError as exc:
        raise GraphUnavailable(
            f"Neo4j rejected the credentials for {cfg.neo4j.user}: {exc}") from None
    except ServiceUnavailable as exc:
        raise GraphUnavailable(
            f"cannot reach Neo4j at {cfg.neo4j.uri}: {exc}") from None


def write_autocommit(cypher: str,
                    params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run one statement in an IMPLICIT transaction, and return its counters.

    `CALL { ... } IN TRANSACTIONS` manages its own transactions, so Neo4j
    refuses it inside a managed one:

        Neo.DatabaseError.Transaction.TransactionStartFailed
        A query with 'CALL { ... } IN TRANSACTIONS' can only be executed in an
        implicit transaction

    So the batched loader uses `session.run` rather than `execute_write`.

    What that costs: `execute_write` retries transient failures and this does
    not.  That is the right trade here rather than a gap - retrying a statement
    that has already committed some of its batches would re-apply them, and the
    only reason that is survivable at all is that every loader statement is a
    MERGE keyed on `uid`.  A failed load is meant to be re-run and verified
    with `--verify`, not silently retried mid-flight.
    """
    cfg = current_settings()
    try:
        with session() as s:
            result = s.run(cypher, params or {})
            summary = result.consume()
            c = summary.counters
            return {
                "nodes_created": c.nodes_created,
                "nodes_deleted": c.nodes_deleted,
                "relationships_created": c.relationships_created,
                "relationships_deleted": c.relationships_deleted,
                "properties_set": c.properties_set,
                "labels_added": c.labels_added,
                "indexes_added": c.indexes_added,
                "constraints_added": c.constraints_added,
                "ms": summary.result_available_after,
            }
    except AuthError as exc:
        raise GraphUnavailable(
            f"Neo4j rejected the credentials for {cfg.neo4j.user}: {exc}") from None
    except ServiceUnavailable as exc:
        raise GraphUnavailable(
            f"cannot reach Neo4j at {cfg.neo4j.uri}: {exc}") from None


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------

def probe() -> Dict[str, Any]:
    """Is Neo4j there, and what is it?  Never raises - returns a report.

    Used by `doctor` and by the MCP server at start-up, so a graph that is
    down degrades to a clear message rather than an exception per tool call.
    """
    cfg = gs.load()
    out: Dict[str, Any] = {
        "ok": False, "driver_installed": HAVE_DRIVER,
        "uri": cfg.neo4j.uri, "database": cfg.neo4j.database,
        "backend": cfg.backend, "config": cfg.source,
    }
    if not HAVE_DRIVER:
        out["error"] = DRIVER_HINT
        return out

    t0 = time.perf_counter()
    try:
        d = get(cfg)
        d.verify_connectivity()
        info = d.get_server_info()
        out["agent"] = info.agent
        out["address"] = str(info.address)

        rows = read("""
            CALL db.labels() YIELD label
            RETURN collect(label) AS labels
        """, cap=1)
        out["labels"] = rows[0]["labels"] if rows else []

        rows = read("MATCH (n) RETURN count(n) AS nodes", cap=1)
        out["nodes"] = rows[0]["nodes"] if rows else 0
        rows = read("MATCH ()-[r]->() RETURN count(r) AS rels", cap=1)
        out["relationships"] = rows[0]["rels"] if rows else 0

        rows = read("SHOW CONSTRAINTS YIELD name RETURN count(name) AS n", cap=1)
        out["constraints"] = rows[0]["n"] if rows else 0
        rows = read("SHOW INDEXES YIELD name RETURN count(name) AS n", cap=1)
        out["indexes"] = rows[0]["n"] if rows else 0

        out["ok"] = True
    except GraphUnavailable as exc:
        out["error"] = str(exc)
    except Neo4jError as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:                            # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


def report(p: Optional[Dict[str, Any]] = None) -> str:
    """probe() as a block of text."""
    p = p or probe()
    lines = [
        f"  {'ok  ' if p['ok'] else 'FAIL'} Neo4j at {p['uri']}  db={p['database']}",
        f"       config   {p.get('config')}",
        f"       backend  {p.get('backend')}",
    ]
    if p["ok"]:
        lines += [
            f"       server   {p.get('agent')} at {p.get('address')}",
            f"       content  {p.get('nodes', 0):,} nodes, "
            f"{p.get('relationships', 0):,} relationships",
            f"       schema   {p.get('constraints', 0)} constraints, "
            f"{p.get('indexes', 0)} indexes",
            f"       labels   {', '.join(p.get('labels') or []) or '(none)'}",
            f"       probed   {p.get('ms')} ms",
        ]
    else:
        for line in str(p.get("error", "unknown")).splitlines():
            lines.append(f"       {line}")
    return "\n".join(lines)
