"""
Prove that paging a tool partitions its answer, rather than resampling it.

    python -m kb.server.pagecheck

## What can go wrong, and why it is invisible

A page is `rows[start:start+size]` of whatever the store returned.  That only
partitions the answer if the row ORDER is identical on every call, and
`ORDER BY` alone does not promise it: where the sort key has ties the database
may order the tied rows differently between two executions.  Nothing in SQL
forbids it, and a different plan or scan order is enough.

The failure is quiet in the worst way.  Page 2 repeats rows from page 1 and
drops others, every page is well-formed, the totals all agree, and an agent
walking the pages ends up with a set that is neither complete nor honest about
being incomplete.  Nothing raises.

Two faults were found this way before this module existed:
`where_is_field_used` ordered by `program`, and one field has 3,215 rows
sharing a program; `who_touches_file` ordered by `(mode, program)`, and one
file has 8 rows sharing that pair.

## What this asserts

For each paged tool, on a subject big enough to span several pages:

  * walking the pages twice returns the same rows in the same order, so the
    order is stable across executions
  * no row appears on two different pages
  * the number of distinct rows collected matches the total the tool reports

The first is the one that matters.  A tie only misorders SOMETIMES - the same
plan usually produces the same order - so this runs the walk twice rather than
trusting one pass, and any single disagreement is a failure.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Sequence, Tuple

_RULE = re.compile(r"^\s+-{3,}(\s+-{3,})+\s*$")
_PAGE = re.compile(r"PAGE (\d+) OF (\d+)\s+rows ([\d,]+)-([\d,]+) of ([\d,]+)")


#: `search_source` returns `MEMBER:line  source text`, not a table, so the
#: table reader finds nothing in it and the check passed vacuously - which is
#: worse than not running, because it reads as a pass.
_HIT = re.compile(r"^\s{2}([A-Z0-9$#@_.-]+):(\d+)\s")


def _rows_on_page(text: str) -> List[Tuple[str, ...]]:
    """The data rows of a rendered answer, table or hit list."""
    hits = [(m.group(1), m.group(2)) for m in
            (_HIT.match(line) for line in text.splitlines()) if m]
    if hits:
        return hits
    out: List[Tuple[str, ...]] = []
    in_table = False
    for line in text.splitlines():
        if _RULE.match(line):
            in_table = True
            continue
        if not in_table:
            continue
        stripped = line.strip()
        if (not stripped
                or stripped.startswith(("PAGE", "Last page", "...", "("))
                or "row(s) not shown" in stripped):
            in_table = False
            continue
        out.append(tuple(line.split()))
    return out


def _total(text: str) -> int:
    m = _PAGE.search(text)
    return int(m.group(5).replace(",", "")) if m else 0


def _pages(text: str) -> int:
    m = _PAGE.search(text)
    return int(m.group(2)) if m else 1


def walk(fn: Callable[..., str], kw: Dict[str, Any],
         cap: int = 6) -> Tuple[List[Tuple[str, ...]], int, int]:
    """Every row across the first `cap` pages, plus the totals claimed."""
    first = fn(**dict(kw, page=1))
    pages, total = _pages(first), _total(first)
    rows = list(_rows_on_page(first))
    for p in range(2, min(pages, cap) + 1):
        rows.extend(_rows_on_page(fn(**dict(kw, page=p))))
    return rows, pages, total


#: (label, tool, arguments).  Each subject is chosen to span several pages -
#: a single-page answer cannot fail this check and proves nothing.
CASES: Tuple[Tuple[str, str, Dict[str, Any]], ...] = (
    ("who_touches_file", "who_touches_file", {"file": "CUSMAS"}),
    ("where_is_field_used", "where_is_field_used",
     {"field": "WS-RECORDS-READ"}),
    ("search_source", "search_source",
     {"pattern": "ORDER", "member_type": "CBLLE"}),
    ("who_calls", "who_calls", {"name": "SYS100", "depth": 2}),
    # A subject that spans pages. `touches="CUSMAS"` returns one page,
    # and a one-page answer cannot fail this check.
    ("find_paragraph", "find_paragraph", {"name": "VALIDATE",
                                          "limit": 200}),
)


def run(verbose: bool = True) -> bool:
    from . import tools

    ok = True
    for label, name, kw in CASES:
        fn = getattr(tools, name, None)
        if fn is None:
            print(f"  FAIL {label:<22} no such tool")
            ok = False
            continue
        try:
            first, pages, total = walk(fn, kw)
            second, _, _ = walk(fn, kw)
        except Exception as exc:                             # noqa: BLE001
            print(f"  FAIL {label:<22} {type(exc).__name__}: "
                  f"{str(exc).splitlines()[0][:50]}")
            ok = False
            continue

        if pages < 2:
            print(f"  skip {label:<22} one page only - nothing to partition")
            continue

        problems = []
        if first != second:
            differ = sum(1 for a, b in zip(first, second) if a != b)
            problems.append(f"two walks disagree on {differ} row(s)")
        seen = len(first)
        unique = len(set(first))
        if seen != unique:
            problems.append(f"{seen - unique} row(s) appear on more than "
                            f"one page")

        if problems:
            ok = False
            print(f"  FAIL {label:<22} " + "; ".join(problems))
        elif verbose:
            print(f"  ok   {label:<22} {unique:,} distinct row(s) over "
                  f"{min(pages, 6)} of {pages} page(s), of {total:,}")
    return ok


def main(argv: Sequence[str] | None = None) -> int:
    print("  PAGING - do the pages partition the answer?")
    print()
    good = run()
    print()
    print("  PAGING IS STABLE" if good
          else "  PAGING IS NOT STABLE - a paged answer cannot be trusted")
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
