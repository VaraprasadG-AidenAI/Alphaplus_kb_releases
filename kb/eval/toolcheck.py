"""
Every tool, called over stdio, answered from SQLite and nothing else.

    python -m kb.eval.toolcheck

Three questions, and the third is the one that keeps being asked:

  1. Does every registered tool answer at all, or is one of them broken?
  2. Does every answer look like an answer - not empty, not a stack trace,
     not a bare error?
  3. Did any of it reach a database, a port, or a network?

Question 3 is settled by watching, not by asserting. The neo4j and psycopg
drivers are replaced with objects that raise the moment anything touches
them, so a tool that opens a connection fails here rather than on a client's
machine where the driver is absent and the failure is somebody else's
problem.

Dev-only: kb.eval is excluded from the build.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Dict, List, Tuple

#: One set of arguments per tool, chosen to return a real answer on any
#: indexed tree: the tool picks its own subject from the index when it can,
#: and where it cannot, the check fills the argument from the index first.
#: Nothing here names a member - see `_subjects`.
NO_ARGS = ("coverage_report", "unaccounted")


def _subjects(conn) -> Dict[str, str]:
    """Real names to ask about, taken from whatever this index holds."""
    def one(sql: str, default: str = "") -> str:
        try:
            row = conn.execute(sql).fetchone()
            return str(row[0]) if row else default
        except Exception:                                     # noqa: BLE001
            return default

    return {
        "program": one("SELECT name FROM object WHERE member_type IN "
                       "('CBLLE','CBL','SQLCBLLE') ORDER BY lines DESC LIMIT 1"),
        "file": one("SELECT name FROM object WHERE member_type='PF' "
                    "ORDER BY name LIMIT 1"),
        "field": one("SELECT name FROM field WHERE name<>'' "
                     "ORDER BY name LIMIT 1"),
        "screen": one("SELECT name FROM object WHERE member_type='DSPF' "
                      "ORDER BY name LIMIT 1"),
        "cl": one("SELECT name FROM object WHERE member_type='CLLE' "
                  "ORDER BY name LIMIT 1"),
        "caller": one("SELECT caller FROM call_edge LIMIT 1"),
        "callee": one("SELECT callee FROM call_edge LIMIT 1"),
        "paragraph": one("SELECT name FROM paragraph WHERE name<>'' LIMIT 1"),
    }


def _arguments(name: str, spec: Dict[str, Any],
               subject: Dict[str, str]) -> Dict[str, Any]:
    """Fill a tool's required arguments from the index."""
    schema = spec.get("inputSchema", {}) or {}
    required = schema.get("required", []) or []
    properties = schema.get("properties", {}) or {}
    out: Dict[str, Any] = {}
    for key in required:
        kind = (properties.get(key, {}) or {}).get("type", "string")
        if kind == "integer":
            out[key] = 1
            continue
        if kind == "boolean":
            out[key] = False
            continue
        lowered = key.lower()
        if "field" in lowered:
            out[key] = subject["field"]
        elif "screen" in lowered or "display" in lowered:
            out[key] = subject["screen"] or subject["file"]
        elif "file" in lowered:
            out[key] = subject["file"]
        elif lowered in ("frm", "from", "start"):
            out[key] = subject["caller"]
        elif lowered in ("to", "target", "end"):
            out[key] = subject["callee"]
        elif "paragraph" in lowered:
            out[key] = subject["paragraph"]
        elif lowered in ("text", "query", "pattern", "term", "ticket"):
            # Something every codebase contains, so the search is not empty.
            # Narrow on purpose: a pattern matching every member turns
            # this check into a 70-second full-text sweep and proves nothing
            # a narrow one does not.
            out[key] = "WORKING-STORAGE"
        else:
            out[key] = subject["program"]
    return out


class _Forbidden:
    """Stands where a database driver was. Raises if anything touches it."""

    def __init__(self, what: str) -> None:
        self._what = what

    def __getattr__(self, item: str) -> Any:
        raise AssertionError(
            f"a tool reached for {self._what}.{item} - this build answers "
            f"from SQLite only, and the packaged executable ships no such "
            f"driver")


def main() -> int:
    from kb import store
    from kb.graph import backend
    from kb.server import server as srv

    print()
    print(f"  fact store     {store.backend()}")
    print(f"  graph backend  {backend.mode()}  (configured: "
          f"{backend.configured()})")
    if store.backend() != "sqlite" or backend.mode() != "sqlite":
        print()
        print("  FAIL  this run is not SQLite-only, so it is not testing what "
              "ships.")
        print("        Set backend = \"sqlite\" in alphaplus.toml.")
        return 1

    # Anything that opens a connection from here on fails loudly.
    for module in ("neo4j", "psycopg", "psycopg2"):
        sys.modules[module] = _Forbidden(module)        # type: ignore[assignment]

    conn = store.facts()
    subject = _subjects(conn)
    conn.close()
    print("  asking about   " + ", ".join(
        f"{k}={v}" for k, v in sorted(subject.items()) if v))
    print()

    results: List[Tuple[str, bool, float, str]] = []
    for spec in sorted(srv.TOOLS, key=lambda t: str(t["name"])):
        name = str(spec["name"])
        # The server's own dispatch table, not one module: `remember` and
        # `recall` live in kb.server.memory, and a check that looked only at
        # kb.server.tools reported two healthy tools as missing.
        function = srv.HANDLERS.get(name)
        if function is None:
            results.append((name, False, 0.0,
                            "registered in TOOLS but not in HANDLERS - the "
                            "server would answer 'unknown tool'"))
            continue
        arguments = {} if name in NO_ARGS else _arguments(name, spec, subject)
        started = time.time()
        try:
            answer = str(function(**arguments))
            took = time.time() - started
        except AssertionError:
            raise                                   # a driver was touched
        except Exception as exc:                                # noqa: BLE001
            results.append((name, False, time.time() - started,
                            f"{type(exc).__name__}: {exc}"))
            continue

        note = ""
        ok = True
        if not answer.strip():
            ok, note = False, "empty answer"
        elif "Traceback (most recent call last)" in answer:
            ok, note = False, "a traceback reached the caller"
        else:
            note = f"{len(answer):,} chars"
        results.append((name, ok, took, note))

    for name, ok, took, note in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<22} {took:6.2f}s  {note}")

    failed = [r for r in results if not r[1]]
    print()
    print(f"  {len(results) - len(failed)}/{len(results)} tool(s) answered "
          f"from SQLite, no driver touched")
    if failed:
        print("  Do not build.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
