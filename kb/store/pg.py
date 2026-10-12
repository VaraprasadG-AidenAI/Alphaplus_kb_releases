"""
A PostgreSQL connection that behaves like `sqlite3.Connection`.

    from kb import store
    db = store.connect()          # honours ALPHAPLUS_STORE / the DSN settings
    rows = db.execute("SELECT name FROM object WHERE library=?", ("SERV",))

The whole point is that the ~132 existing call sites do not change.  They were
written against `sqlite3`, they use `?` placeholders, they call `.execute()` on
the CONNECTION rather than on a cursor, and they read rows as both mappings and
sequences.  Every one of those is preserved here:

  * `?` is translated by `kb.store.sql.to_pyformat`, which also doubles literal
    `%` - and an empty parameter sequence becomes `None`, because psycopg scans
    for placeholders on `()` but not on `None`.
  * `.execute()` on the connection returns a cursor, and a NEW cursor each time.
    That last part is not cosmetic: `kb.lexicon.terms.build` runs a query inside
    the iteration of another query on the same connection, which works on
    sqlite3 because `Connection.execute` hands back a fresh cursor.  psycopg's
    own `Connection.execute` reuses one and would destroy the outer result set
    mid-loop.
  * rows come back as `kb.store.rows.Row`.
  * `row_factory` can be assigned and is ignored.  Several modules set
    `conn.row_factory = sqlite3.Row` and would otherwise need editing to say
    nothing.

## Read-only is no longer in the URI

SQLite expressed it as `file:...?mode=ro`, and five readers relied on that.
PostgreSQL has no URI equivalent, so `connect(read_only=True)` issues
`SET default_transaction_read_only = on` instead.  It is the cheapest
correctness property in the codebase and the easiest to lose in a port, which is
why it is a parameter here rather than a convention.

## Threads

`kb.server.tools` keeps ONE connection and shares it across
`ThreadingHTTPServer`'s daemon threads - `check_same_thread=False`.  psycopg
connections are not thread-safe for concurrent execution, so `connect()` returns
a connection whose `execute` is serialised by a lock.  A pool would be faster and
is the right answer under real load; a lock is the right answer for a port,
because it preserves the existing semantics exactly and cannot introduce a
transaction-visibility change.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Iterable, Iterator, List, Optional, Sequence

from .rows import Row, row_factory
from .sql import needs_params, to_pyformat

DEFAULT_SCHEMA = "kb"


# =======================================================================
# Where the DSN comes from
# =======================================================================

def dsn_from_env() -> Optional[str]:
    """A libpq DSN from the environment or alphaplus.toml, or None.

    Environment first, because a container sets variables rather than editing a
    committed file. Then [postgres] in the toml - which matters more than it
    looks: an MCP stdio server is spawned with the SDK's env allowlist and sees
    no ALPHAPLUS_* at all, so the toml is the only channel that reaches it.

    The password is never IN the toml, only the path to a file holding it.
    """
    from kb.config import setting
    full = str(setting("ALPHAPLUS_PG_DSN", "postgres", "dsn", default="")).strip()
    if full:
        return full

    host = str(setting("ALPHAPLUS_PG_HOST", "postgres", "host", default="")).strip()
    db = str(setting("ALPHAPLUS_PG_DATABASE", "postgres", "database",
                     default="")).strip()
    if not (host or db):
        return None

    user = str(setting("ALPHAPLUS_PG_USER", "postgres", "user", default="")).strip()
    port = str(setting("ALPHAPLUS_PG_PORT", "postgres", "port", default="")).strip()
    password = str(os.environ.get("ALPHAPLUS_PG_PASSWORD", ""))
    if not password:
        path = str(setting("ALPHAPLUS_PG_PASSWORD_FILE", "postgres",
                           "password_file", default="")).strip()
        if path:
            try:
                with open(path, encoding="utf-8") as fh:
                    password = fh.read().strip()
            except OSError:
                password = ""

    parts = []
    if host:
        parts.append(f"host={host}")
    if port:
        parts.append(f"port={port}")
    if db:
        parts.append(f"dbname={db}")
    if user:
        parts.append(f"user={user}")
    if password:
        parts.append(f"password={password}")
    return " ".join(parts) if parts else None



def schema_from_env() -> str:
    from kb.config import setting
    return str(setting("ALPHAPLUS_PG_SCHEMA", "postgres", "schema",
                       default=DEFAULT_SCHEMA)).strip() or DEFAULT_SCHEMA


def backend() -> str:
    """`sqlite` or `postgres`.  ONE definition, read by everything.

    Unset means: postgres if a DSN is configured, else sqlite.  Configuring a
    DSN is already the statement "my facts live in PostgreSQL" - requiring a
    second variable to repeat it meant both stores were being written and only
    one read, which is the redundancy this migration exists to remove.  A box
    with no DSN - the frozen-exe case, a laptop with nothing installed - still
    gets the file, and nothing new to set up.

    `ALPHAPLUS_STORE=sqlite|postgres` still overrides, for tests and for the
    parity comparison in kb.e2e, which must pin each side explicitly.

    Lives here rather than in kb.server.tools because kb/graph and kb/index
    need it too, and a second copy of a two-line function that decides which
    database everything talks to is exactly the drift this migration has been
    cleaning up.
    """
    v = os.environ.get("ALPHAPLUS_STORE", "").strip().lower()
    if v in ("sqlite", "postgres"):
        return v
    return "postgres" if dsn_from_env() else "sqlite"


def ready() -> "tuple[bool, str]":
    """(ok, why not) - is the fact store there, with facts in it.

    Replaces 13 copies of `if not config.FACTS_DB.exists()`, which asked the
    wrong question twice over: on PostgreSQL that file is not the store at all,
    and on SQLite a 0-byte facts.db exists without being an index - any
    sqlite3.connect() that never writes a schema leaves one behind. Both cases
    let callers through to die on "no such table" with a raw traceback.

    One query, both stores: `facts()` has already chosen which.
    """
    try:
        db = facts()
        n = db.execute("SELECT COUNT(*) FROM object").fetchone()[0]
        db.close()
    except Exception as exc:                            # noqa: BLE001
        return False, f"{describe()} is not readable: {str(exc).splitlines()[0][:70]}"
    if not n:
        return False, f"{describe()} holds no members - run python -m kb.index.build"
    return True, ""


def describe() -> str:
    """Which store, in a phrase an error message can carry."""
    if backend() == "postgres":
        return f"postgres schema {schema_from_env()}"
    from kb import config
    return str(config.FACTS_DB)



def facts(read_only: bool = True, row_factory_rows: bool = True):
    """A connection to the fact store, whichever store that is.

    The one opener for every module that just wants to read facts -
    kb/index/status.py, kb/index/check.py, kb/export.py, kb/viz, kb/lexicon and
    the rest.  Before this each of them wrote its own
    `sqlite3.connect(config.FACTS_DB)` plus a `row_factory` line, which is 15
    places to remember when the store changes and 15 places to miss one.

    Read-only by default, because almost every caller is.  On SQLite that is
    the `mode=ro` URI; on PostgreSQL it is
    `SET default_transaction_read_only = on`, which also makes the connection
    autocommit so one failed query cannot poison the rest of the session.
    """
    if backend() == "postgres":
        return connect(read_only=read_only)
    import sqlite3
    from kb import config
    if read_only:
        conn = sqlite3.connect(f"file:{config.FACTS_DB}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(config.FACTS_DB)
    if row_factory_rows:
        conn.row_factory = sqlite3.Row
    return conn


# =======================================================================
# The connection
# =======================================================================

class Cursor:
    """Thin wrapper so a psycopg cursor reads like a sqlite3 one."""

    __slots__ = ("_cur",)

    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self) -> List[Row]:
        return self._cur.fetchall()

    def fetchmany(self, size: int = 1) -> List[Row]:
        return self._cur.fetchmany(size)

    def __iter__(self) -> Iterator[Row]:
        return iter(self._cur)

    @property
    def description(self):
        return self._cur.description

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def lastrowid(self):
        # sqlite3 exposes this; nothing in kb/ reads it, and PostgreSQL has no
        # equivalent without a RETURNING clause.  None rather than a lie.
        return None

    def close(self) -> None:
        self._cur.close()

    def execute(self, sql: str, params: Any = ()) -> "Cursor":
        p = needs_params(params)
        self._cur.execute(to_pyformat(sql) if p is not None else sql, p)
        return self

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> "Cursor":
        rows = list(seq)
        if rows:
            self._cur.executemany(to_pyformat(sql), rows)
        return self


class Connection:
    """`sqlite3.Connection`'s surface, over psycopg."""

    def __init__(self, conn, schema: str = DEFAULT_SCHEMA):
        self._conn = conn
        self._schema = schema
        self._lock = threading.RLock()
        #: assigned by several modules and deliberately ignored - rows are
        #: always kb.store.rows.Row, which does everything sqlite3.Row does
        self.row_factory = None

    # ---- the sqlite3 surface -------------------------------------------
    def execute(self, sql: str, params: Any = ()) -> Cursor:
        """A NEW cursor every call, as sqlite3 does.

        kb.lexicon.terms.build depends on this: it iterates one query and runs
        another inside the loop.  Reusing a cursor there would silently truncate
        the outer result set.
        """
        with self._lock:
            cur = Cursor(self._conn.cursor(row_factory=row_factory))
            return cur.execute(sql, params)

    def executemany(self, sql: str,
                    seq: Iterable[Sequence[Any]]) -> Cursor:
        with self._lock:
            cur = Cursor(self._conn.cursor(row_factory=row_factory))
            return cur.executemany(sql, seq)

    def executescript(self, script: str) -> None:
        """Multiple statements, no parameters.

        The only caller is kb.index.build.create, and only for schema.sql.  The
        PRAGMAs in that file are SQLite file-format settings and are dropped by
        kb.store.ddl, which is what should generate the script passed here.
        """
        with self._lock:
            self._conn.execute(script)

    def cursor(self) -> Cursor:
        with self._lock:
            return Cursor(self._conn.cursor(row_factory=row_factory))

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def rollback(self) -> None:
        with self._lock:
            self._conn.rollback()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- context manager, matching sqlite3's commit-on-exit ------------
    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()

    # ---- extras ---------------------------------------------------------
    @property
    def schema(self) -> str:
        return self._schema

    @property
    def raw(self):
        """The psycopg connection, for COPY and server-side cursors."""
        return self._conn


def connect(dsn: Optional[str] = None, schema: Optional[str] = None,
            read_only: bool = False,
            autocommit: Optional[bool] = None) -> Connection:
    """Open a connection with `search_path` set and rows as `Row`.

    `autocommit` defaults to whatever `read_only` is, and that default is the
    important part.  Inside a transaction, PostgreSQL aborts the WHOLE
    transaction on the first error and answers everything after it with
    "current transaction is aborted" - so one bad query on the long-lived
    connection that `kb.server.tools.db()` holds would take down every
    subsequent tool call for the life of the process.  sqlite3 does not behave
    that way and the tools were written against sqlite3.  A read-only
    connection has no transaction worth protecting, so each statement stands
    alone and one failure stays one failure.
    """
    import psycopg

    dsn = dsn or dsn_from_env()
    if not dsn:
        raise RuntimeError(
            "no PostgreSQL DSN: set ALPHAPLUS_PG_DSN, or ALPHAPLUS_PG_HOST and "
            "ALPHAPLUS_PG_DATABASE (with ALPHAPLUS_PG_PASSWORD or "
            "ALPHAPLUS_PG_PASSWORD_FILE)")
    schema = schema or schema_from_env()

    conn = psycopg.connect(dsn, row_factory=row_factory)
    conn.autocommit = True
    # search_path so every existing query can keep naming bare tables
    conn.execute(f"SET search_path TO {schema}, public")
    if read_only:
        # SQLite said this in the URI - `file:...?mode=ro`.  There is no URI
        # equivalent here, and five readers depend on the guarantee.
        conn.execute("SET default_transaction_read_only = on")
    conn.autocommit = read_only if autocommit is None else autocommit
    return Connection(conn, schema)
