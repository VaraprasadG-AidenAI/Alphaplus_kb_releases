"""
The relational store: connection, rows and SQL dialect.

    from kb import store
    db = store.connect(read_only=True)
    for r in db.execute("SELECT name FROM object WHERE library=?", ("SERV",)):
        ...

Three modules, each with its own selftest:

    kb.store.sql    `?` -> `%s`, and literal `%` -> `%%`
    kb.store.rows   a row that is a mapping AND a sequence, like sqlite3.Row
    kb.store.pg     a connection with sqlite3.Connection's surface

plus two migration tools:

    kb.store.ddl    the PostgreSQL schema, generated from kb/index/schema.sql
    kb.store.load   facts.db -> PostgreSQL via COPY, and a row-count comparison
"""

from .pg import (Connection, Cursor, backend, connect, dsn_from_env,
                 describe, ready,
                 facts, schema_from_env)
from .rows import Row, row_factory
from .sql import needs_params, to_pyformat

__all__ = [
    "Connection", "Cursor", "backend", "connect", "dsn_from_env", "ready", "describe",
    "facts", "schema_from_env",
    "Row", "row_factory", "needs_params", "to_pyformat",
]
