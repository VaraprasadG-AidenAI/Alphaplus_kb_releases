"""
A row that behaves like `sqlite3.Row`, for psycopg.

    python -m kb.store.rows --selftest

`sqlite3.Row` is three things at once - a mapping, a sequence, and an iterable -
and this codebase uses all three faces, sometimes through the same connection:

    r["name"]                    the dominant style, hundreds of sites
    r[0]                         kb/index/status.py, kb/graph/load.py
    r.keys()                     kb/export.py, kb/graph/load.py
    dict(r)                      kb/summarise/store.py, kb/graph/bench.py
    for a, b in cur.execute(...) 12 sites, tuple unpacking straight off a cursor
    dict(cur.fetchall())         6 sites, over two-column results

Neither of psycopg's built-in row factories can do that.  `dict_row` breaks
every `r[0]` and every tuple unpacking - worse, unpacking a dict yields its KEYS,
so `for name, member_type in cur.execute(...)` would silently bind the strings
"name" and "member_type" instead of failing.  `tuple_row` breaks every
`r["name"]`.  `namedtuple_row` breaks `r["name"]` too, since attribute access is
not subscription.

The dangerous case is concrete: `kb.index.update.context()` sets
`row_factory = sqlite3.Row` on the shared connection, and `build.write()` then
runs `for name, member_type in cur.execute("SELECT name, member_type FROM
object")` on that same connection.  One factory has to serve both.

So this class exists to keep those 132 call sites unchanged, which is the
difference between a reviewable diff and a rewrite.  It is deliberately tested
against `sqlite3.Row` itself as the oracle rather than against a list of
expectations - see `--selftest`.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict, List, Sequence, Tuple, Union

__all__ = ["Row", "row_factory"]


class Row:
    """One result row, addressable by name or position.

    `__iter__` yields VALUES, matching `sqlite3.Row`, which is what makes tuple
    unpacking work.  `dict(row)` still yields {name: value}, because `dict()`
    prefers the mapping protocol - `keys()` plus `__getitem__` - over iteration.
    Both faces at once, and that is the point.
    """

    __slots__ = ("_names", "_values", "_lookup")

    def __init__(self, names: Tuple[str, ...], values: Tuple[Any, ...],
                 lookup: Dict[str, int]):
        self._names = names
        self._values = values
        # shared with every row of the same cursor, built once per query
        self._lookup = lookup

    # ---- mapping face --------------------------------------------------
    def keys(self) -> List[str]:
        """Column names, in order.  A list, as `sqlite3.Row.keys()` returns."""
        return list(self._names)

    def __getitem__(self, key: Union[str, int, slice]) -> Any:
        if isinstance(key, str):
            try:
                return self._values[self._lookup[key]]
            except KeyError:
                # sqlite3.Row raises IndexError, not KeyError, for a bad name.
                # Matching that matters: `except IndexError` around a column
                # lookup is code that would otherwise stop catching.
                raise IndexError(f"No item with that key: {key!r}") from None
        return self._values[key]

    # ---- sequence / iterable face --------------------------------------
    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __contains__(self, value: Any) -> bool:
        # sqlite3.Row tests VALUES, not keys.  Surprising, and copied on
        # purpose: `if x in row` must not change meaning between the stores.
        return value in self._values

    # ---- comparison and display ----------------------------------------
    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Row):
            return (self._names == other._names
                    and self._values == other._values)
        if isinstance(other, tuple):
            return self._values == other
        return NotImplemented

    def __ne__(self, other: Any) -> bool:
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self) -> int:
        return hash((self._names, self._values))

    def __repr__(self) -> str:
        inner = ", ".join(f"{n}={v!r}"
                          for n, v in zip(self._names, self._values))
        return f"<Row {inner}>"


def row_factory(cursor):
    """psycopg row factory producing `Row`.

    Called once per query; the name->index map is built here rather than per
    row.  `cursor.description` is None for a statement that returns nothing, in
    which case no row will ever be constructed.
    """
    description = cursor.description
    if description is None:
        return lambda values: None
    names = tuple(d.name for d in description)
    # First occurrence wins on a duplicate column name, which is what
    # sqlite3.Row does.  Duplicates happen in this codebase whenever a join
    # selects the same column from two tables without aliasing.
    lookup: Dict[str, int] = {}
    for i, name in enumerate(names):
        lookup.setdefault(name, i)

    def make(values: Sequence[Any]) -> Row:
        return Row(names, tuple(values), lookup)

    return make


# =======================================================================
# Selftest - sqlite3.Row is the oracle
# =======================================================================

def _selftest() -> int:
    import sqlite3

    print()
    print("  kb.store.rows - Row against sqlite3.Row")
    print("  " + "=" * 66)

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE t(name TEXT, member_type TEXT, n INTEGER)")
    db.executemany("INSERT INTO t VALUES (?,?,?)",
                   [("ORD100", "CBLLE", 7), ("CUSMAS", "PF", None)])

    cur = db.execute("SELECT name, member_type, n FROM t ORDER BY name")
    reference = cur.fetchall()

    # build our rows from exactly the same data
    names = tuple(d[0] for d in cur.description)
    lookup = {k: i for i, k in enumerate(names)}
    ours = [Row(names, tuple(r), lookup) for r in reference]

    checks: List[Tuple[str, Any, Any]] = []

    for label, fn in (
        ('r["name"]',            lambda r: r["name"]),
        ('r["n"] (NULL)',        lambda r: r["n"]),
        ("r[0]",                 lambda r: r[0]),
        ("r[-1]",                lambda r: r[-1]),
        ("r[0:2]",               lambda r: r[0:2]),
        ("r.keys()",             lambda r: r.keys()),
        ("len(r)",               lambda r: len(r)),
        ("tuple(r)",             lambda r: tuple(r)),
        ("list(r)",              lambda r: list(r)),
        ("dict(r)",              lambda r: dict(r)),
        ("unpack a, b, c",       lambda r: (lambda a, b, c: (a, b, c))(*r)),
        ("'CBLLE' in r",         lambda r: "CBLLE" in r),
        ("'name' in r",          lambda r: "name" in r),
    ):
        for i, (a, b) in enumerate(zip(reference, ours)):
            try:
                want = fn(a)
            except Exception as exc:                    # noqa: BLE001
                want = f"{type(exc).__name__}"
            try:
                got = fn(b)
            except Exception as exc:                    # noqa: BLE001
                got = f"{type(exc).__name__}"
            checks.append((f"{label}  row {i}", want, got))

    # a bad column name must raise the SAME exception type
    for i, (a, b) in enumerate(zip(reference, ours)):
        def bad(r):
            try:
                r["nope"]
                return "no exception"
            except Exception as exc:                    # noqa: BLE001
                return type(exc).__name__
        checks.append((f'r["nope"] raises  row {i}', bad(a), bad(b)))

    # dict() over a two-column result, the `dict(cur.fetchall())` pattern
    cur2 = db.execute("SELECT name, member_type FROM t ORDER BY name")
    ref2 = cur2.fetchall()
    names2 = tuple(d[0] for d in cur2.description)
    lookup2 = {k: i for i, k in enumerate(names2)}
    ours2 = [Row(names2, tuple(r), lookup2) for r in ref2]
    checks.append(("dict(fetchall()) over 2 columns",
                   dict(ref2), dict(ours2)))

    bad_count = 0
    for label, want, got in checks:
        ok = want == got
        bad_count += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {label:<34}{want!r}")
        if not ok:
            print(f"       sqlite3.Row {want!r}")
            print(f"       kb Row      {got!r}")

    print()
    print("  " + "-" * 66)
    print(f"  {len(checks)} comparisons against sqlite3.Row, "
          f"{bad_count} disagreement(s)")

    # the trap that motivated the class
    print()
    print("  why neither psycopg factory would do")
    print("  " + "-" * 66)
    d = {"name": "ORD100", "member_type": "CBLLE"}
    a, b = d
    print(f"  unpacking a dict yields its KEYS: a={a!r}, b={b!r}")
    print("  so dict_row would bind column NAMES to those variables at")
    print("  build.py's `for name, member_type in cur.execute(...)` and never")
    print("  raise. That is the failure this class exists to prevent.")
    a2, b2, _ = ours[0]
    print(f"  unpacking a Row yields its VALUES: a={a2!r}, b={b2!r}")
    print()
    return 1 if bad_count else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="sqlite3.Row-compatible row")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
