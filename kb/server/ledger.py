"""
A record of every tool call: what was asked, how long it took, what came back.

    python -m kb.server.ledger              the last 14 days, per tool
    python -m kb.server.ledger --days 1     just today
    python -m kb.server.ledger --calls      the raw lines, newest first

## Why this exists

There was no record of any tool call in any state.  The only use of the
`logging` module in 78 files was three lines turning the Neo4j driver's logging
OFF, `kb/server/web.py` suppresses its access log for every 2xx, and every
tools/call returns HTTP 200 - so a tool that failed on every invocation for a
month left no trace.  One had: `what_it_touches(kind='fields')` raised a
PostgreSQL type error on every call and nothing knew.

The question this is really for is "which of the 23 tools does an agent
actually use".  23 descriptions are ~1,800 tokens in the agent's context on
every turn of every conversation, so a tool nobody calls is not free - it is a
permanent tax.  kb/server/tools.py already makes that argument to justify two
kind-parameterised tools instead of eighteen; this is how it gets measured
rather than argued.

## Why JSONL and not the logging module

`logging` has global state that any import can mutate - one `basicConfig()`
anywhere attaches a handler, and this codebase already carries three lines
whose only job is silencing somebody else's logger.  An append to a file has no
global state, no handler ordering, and no configuration.

A telemetry write must never be able to make a tool slower or fail.  That is
a claim with a number behind it: one record costs ~15us, of which ~5 is the
write, against tool calls measured in milliseconds.  Two things buy that, and
the first version of this module had neither.

The append handle is HELD, not reopened.  `with open(path, "a")` per call
measured 9,374us on this machine - Windows scans a file on open, so 371 calls
meant 371 scans - and a held handle doing the same write and flush is 5.5us.
At 10ms a telemetry write cost more than the tool call it was recording.

The write is ONE write under a lock.  `record` is called from
ThreadingHTTPServer's daemon threads, and without the lock concurrent appends
interleave: measured, 7 lines of 15,281 written from 16 threads were not valid
JSON.  With it, 0 of 16,501.  The line is serialised to a string before the
lock is taken, so the critical section is the write alone - 43us per record
even with 16 threads contending, which is under 1% of a fast tool call.

Everything is still wrapped in a bare except, and a truncated final line from
a killed process costs one record rather than the file.

## Why not SQLite

The fact store is opened read-only on purpose.  A second, writable database is
a second thing to explain inside a client network, and two MCP servers - Claude
Code spawns one per workspace - contending on a write lock could block a tool
call.  The lock above is in-process and guards a 5us write; a database write
lock is cross-process and guards a transaction, which is a different risk.

Rollover is by date in the filename: no size check and no rename, because two
processes renaming one file on Windows is a PermissionError landing in the
middle of somebody's question.  `purge` will not unlink the file this process
holds open.

## What is not written down

Argument values are redacted by name, not by guesswork.  A member name is an
identifier and the ledger is useless without it - "which names are slow" is
half the value.  A ticket text is customer prose and this file sits inside a
client network, so it is stored as a hash and a length: enough to recognise the
same ticket twice, not enough to read it back.  ALPHAPLUS_TELEMETRY=args opts
into verbatim locally.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

#: Arguments that are prose, not identifiers.  Everything not named here is
#: stored verbatim, because everything else in this surface is a member name,
#: a member type or an enum value.
_PROSE = ("text",)

#: A human line on stderr for calls worth noticing.  stdout is the JSON-RPC
#: channel under stdio - anything printed there is parsed as part of a reply -
#: so stderr is the only console channel a server may use.  kb/graph/backend.py
#: documents the same rule for the same reason.
SLOW_MS = 1000.0

_SCHEMA = 1
_state: Dict[str, Any] = {"path": None, "warned": False, "on": None,
                          "handle": None, "handle_day": None}

#: Serialises the append.  See the comment in `record`.
_write_lock = threading.Lock()


# =======================================================================
# writing
# =======================================================================

def enabled() -> bool:
    """Default ON.

    Zero observability is the problem being fixed here, and a default-off
    telemetry switch stays off forever.
    """
    if _state["on"] is None:
        _state["on"] = os.environ.get("ALPHAPLUS_TELEMETRY", "1") != "0"
    return bool(_state["on"])


def directory() -> Path:
    from .. import config
    return config.DATA_DIR / "telemetry"


def _path() -> Optional[Path]:
    """Today's file, or None if we cannot write.  Re-resolved once per day."""
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    cached = _state["path"]
    if cached is not None and cached.name == f"tools-{day}.jsonl":
        return cached
    try:
        target = directory()
        target.mkdir(parents=True, exist_ok=True)
        found = target / f"tools-{day}.jsonl"
        _state["path"] = found
        return found
    except OSError as exc:
        if not _state["warned"]:
            _state["warned"] = True
            print(f"  [ledger] no telemetry: {exc}", file=sys.stderr, flush=True)
        return None


def _handle():
    """The open append handle for today's file, or None.

    ## Why the handle is held instead of reopened per call

    It WAS reopened per call - `with open(path, "a")` - which is the obvious
    shape and was 9,374us on this machine.  A held handle doing the same
    write and the same flush is 5.5us.  Three orders of magnitude, and all of
    it in the open/close pair: on Windows a file open is scanned, so opening
    the same file 371 times means 371 scans.

    That mattered because the cost landed on the tool call.  A narrowed
    `what_it_touches` answers in 6ms; a 10ms telemetry write made recording
    the call twice as expensive as answering it, which is the exact thing the
    module docstring promises does not happen.

    The flush stays.  It is 4.7us of the 5.5 and it is what makes a killed
    process lose nothing rather than a buffer's worth.

    Reopened when the date rolls over, so a long-lived server still lands in
    the right day's file, and closed at exit.
    """
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    handle = _state.get("handle")
    if handle is not None and _state.get("handle_day") == day:
        return handle
    _close_handle()
    path = _path()
    if path is None:
        return None
    try:
        _state["handle"] = open(path, "a", encoding="utf-8")
        _state["handle_day"] = day
        return _state["handle"]
    except OSError as exc:
        if not _state["warned"]:
            _state["warned"] = True
            print(f"  [ledger] no telemetry: {exc}", file=sys.stderr, flush=True)
        return None


def _close_handle() -> None:
    handle = _state.get("handle")
    _state["handle"] = None
    _state["handle_day"] = None
    if handle is not None:
        try:
            handle.close()
        except Exception:                                    # noqa: BLE001
            pass


atexit.register(_close_handle)


def _redact(args: Dict[str, Any]) -> Dict[str, Any]:
    if os.environ.get("ALPHAPLUS_TELEMETRY") == "args":
        return dict(args or {})
    out: Dict[str, Any] = {}
    for key, value in (args or {}).items():
        if key in _PROSE and isinstance(value, str):
            digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()
            out[key] = f"sha:{digest[:8]}/{len(value)}"
        else:
            out[key] = value
    return out


def record(tool: str, args: Dict[str, Any], ms: float, chars: int,
           is_error: bool = False, error_kind: str = "",
           truncated: bool = False,
           extra: Optional[Dict[str, Any]] = None) -> None:
    """One line per tools/call.  Never raises - see the module docstring."""
    if not enabled():
        return
    try:
        row: Dict[str, Any] = {
            "t": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "tool": tool,
            "ms": round(ms, 1),
            "args": _redact(args),
            "chars": chars,
            "isError": bool(is_error),
            "v": _SCHEMA,
        }
        if error_kind:
            row["error_kind"] = error_kind
        if truncated:
            row["truncated"] = True
        row.update(extra or {})

        # ONE write of the whole line, under the lock.
        #
        # Both halves are load-bearing.  Without the lock, concurrent appends
        # interleave and produce lines that are not JSON - measured, 7 corrupt
        # lines in 15,281 written from 16 threads.  `record` is called from
        # ThreadingHTTPServer's daemon threads, so that is the real shape and
        # not a synthetic one.  And building the string first means the lock
        # is held for a 5.5us write rather than for the serialisation too.
        blob = json.dumps(row, default=str) + "\n"
        with _write_lock:
            handle = _handle()
            if handle is not None:
                handle.write(blob)
                handle.flush()

        if is_error or ms > SLOW_MS:
            why = f"ERROR {error_kind}".strip() if is_error else f"{ms:.0f}ms"
            print(f"  [tool] {tool} {why}", file=sys.stderr, flush=True)
    except Exception:                                        # noqa: BLE001
        pass                    # telemetry may never break a tool call


def purge(days: int = 14) -> None:
    """Drop files older than `days`.

    Called once at process start, not per call: stat-ing the directory on
    every tool call is a cost with no reader.
    """
    try:
        keep_after = datetime.now(timezone.utc) - timedelta(days=days)
        for path in directory().glob("tools-*.jsonl"):
            stamp = path.stem.replace("tools-", "")
            when = datetime.strptime(stamp, "%Y%m%d").replace(tzinfo=timezone.utc)
            if when < keep_after and path != _state.get("path"):
                path.unlink()
    except Exception:                                        # noqa: BLE001
        pass


# =======================================================================
# reading
# =======================================================================

def read(days: int = 14) -> List[Dict[str, Any]]:
    """Every recorded call in the window, oldest first.

    A bad line is skipped rather than fatal - a killed process leaves one.
    """
    rows: List[Dict[str, Any]] = []
    try:
        files = sorted(directory().glob("tools-*.jsonl"))
    except Exception:                                        # noqa: BLE001
        return rows
    cutoff = datetime.now(timezone.utc) - timedelta(days=days + 1)
    for path in files:
        try:
            stamp = datetime.strptime(path.stem.replace("tools-", ""),
                                      "%Y%m%d").replace(tzinfo=timezone.utc)
            if stamp < cutoff:
                continue
            body = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def _pct(values: List[float], q: float) -> float:
    """A percentile without numpy, and without statistics' interpolation.

    Nearest rank: with 3 calls recorded, an interpolated p95 invents a number
    that no call took.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    i = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[i]


def report(days: int = 14) -> str:
    """Per tool: calls, p50/p95 ms, median size, errors, queries per call.

    And the line this module exists for - the tools nobody called.
    """
    rows = read(days)
    if not rows:
        return ("  No tool calls recorded.\n"
                f"  Looked in {directory()}\n"
                "  The server writes one line per call once it has served one.")

    out: List[str] = []
    by_tool: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_tool.setdefault(str(row.get("tool", "?")), []).append(row)

    out.append(f"  TOOL USE - {len(rows):,} call(s) over {days} day(s)")
    out.append("  " + "=" * 76)
    out.append(f"  {'tool':<24}{'calls':>7}{'p50ms':>8}{'p95ms':>8}"
               f"{'chars':>9}{'err':>5}{'sql/call':>10}")
    out.append("  " + "-" * 76)
    for tool in sorted(by_tool, key=lambda k: (-len(by_tool[k]), k)):
        calls = by_tool[tool]
        ms = [float(r.get("ms") or 0) for r in calls]
        chars = [float(r.get("chars") or 0) for r in calls]
        sql = [int(r["sql_n"]) for r in calls if r.get("sql_n") is not None]
        errors = sum(1 for r in calls if r.get("isError"))
        per_call = (sum(sql) / len(sql)) if sql else 0.0
        out.append(f"  {tool:<24}{len(calls):>7}{_pct(ms, 0.5):>8.0f}"
                   f"{_pct(ms, 0.95):>8.0f}{_pct(chars, 0.5):>9,.0f}"
                   f"{errors:>5}{per_call:>10.0f}")
    out.append("  " + "-" * 76)

    try:
        from . import server as rpc
        declared = {t["name"] for t in rpc.TOOLS}
    except Exception:                                        # noqa: BLE001
        declared = set()
    if declared:
        dead = sorted(declared - set(by_tool))
        out.append("")
        if dead:
            out.append(f"  NEVER CALLED in {days} day(s) - {len(dead)} of "
                       f"{len(declared)} tools:")
            out.append("    " + ", ".join(dead))
            out.append("  Every declared tool costs context on every turn of "
                       "every conversation.")
            out.append("  This is the list to argue about.")
        else:
            out.append(f"  All {len(declared)} declared tools were called.")

    failed = [r for r in rows if r.get("isError")]
    if failed:
        out.append("")
        out.append(f"  FAILURES - {len(failed)}")
        seen: Dict[str, int] = {}
        for row in failed:
            key = f"{row.get('tool')}  {row.get('error_kind') or 'unknown'}"
            seen[key] = seen.get(key, 0) + 1
        for key, count in sorted(seen.items(), key=lambda kv: -kv[1]):
            out.append(f"    {count:>4}x  {key}")

    out.append("")
    out.append("  SLOWEST CALLS")
    for row in sorted(rows, key=lambda r: -float(r.get("ms") or 0))[:5]:
        args = json.dumps(row.get("args") or {}, default=str)[:50]
        out.append(f"    {float(row.get('ms') or 0):>8.0f} ms  "
                   f"{str(row.get('tool')):<22} {args}")
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="What the MCP tools were asked, and what it cost.")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--calls", action="store_true",
                        help="print the raw records, newest first")
    args = parser.parse_args(argv)
    if args.calls:
        for row in reversed(read(args.days)):
            print(json.dumps(row, default=str))
        return 0
    print(report(args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
