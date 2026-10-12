"""
Run the ticket benchmark.

    python -m kb.eval.run --arm baseline --limit 3     a quick smoke test
    python -m kb.eval.run --arm baseline               all 40 tickets
    python -m kb.eval.run --arm kb                     with the MCP tools
    python -m kb.eval.run --report                     score what has been run

Each ticket is handed to a fresh Claude Code session with a restricted tool
set, and we score what came back against the gold answer.

Two arms:

    baseline   Read, Grep, Glob only - what a good developer with ripgrep can
               do, and the number the knowledge base has to beat
    kb         the same plus the knowledge base tools

Running the same tickets both ways is the whole point.  A knowledge base that
scores 80% means nothing until we know that plain search scores 75%.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

from .. import config
from .tickets import BY_ID, TICKETS, Ticket, UNANSWERABLE

RESULTS = Path(__file__).resolve().parent / "results"

BASELINE_TOOLS = "Read,Grep,Glob"
KB_TOOLS = "Read,Grep,Glob,mcp__alphaplus"

#: The CLI printing one of these means the run never happened.  Scoring it as
#: a zero would quietly turn an outage into a bad benchmark result - which is
#: exactly what happened twice, and cost half a run each time.
ABORTED = (
    "spend limit", "usage limit", "rate limit", "/usage-credits",
    "credit balance", "overloaded_error", "authentication_error",
    "invalid api key", "please run /login", "failed to authenticate",
    "oauth session expired", "could not be refreshed", "session expired",
    "api error", "connection error", "econnrefused",
)


def _aborted(output: str) -> bool:
    """Did the CLI fail rather than answer?

    Two runs have now been ruined by this - a spend limit the first time, an
    expired OAuth session the second - and both scored as confident zeros that
    made the knowledge base look worse than grep. A phrase list alone keeps
    missing the next variant, so there is a structural test as well: a real
    answer to a ticket is never one short line. Even a refusal on the
    unanswerable band runs to a paragraph explaining what is missing.
    """
    text = (output or "").strip()
    if not text:
        return True
    lowered = text.lower()
    if any(phrase in lowered for phrase in ABORTED):
        return True
    return len(text.splitlines()) <= 1 and len(text) < 200


#: Phrases that mean the agent declined to answer.  Kept deliberately broad -
#: we are testing whether it refused, not how elegantly.
CANNOT_ANSWER = (
    "cannot be determined", "cannot tell", "can't tell", "not in the source",
    "not available in the source", "no way to know", "cannot answer",
    "can't answer", "not something the source", "outside the source",
    "not recorded in the source", "would need access", "not possible to "
    "determine", "cannot be answered", "not visible in the source",
    "job schedule", "not stored in the code",
)


@dataclass
class Result:
    ticket: str
    band: str
    arm: str
    gold: List[str]
    mentioned: List[str] = field(default_factory=list)
    hits: List[str] = field(default_factory=list)
    recall: float = 0.0
    fix_site: str = ""
    fix_site_hit: bool = False
    declined: bool = False
    false_confidence: bool = False
    seconds: float = 0.0
    ok: bool = True
    error: str = ""
    output: str = ""
    #: Which tools the agent actually called, in order, with repeats.  Parsed
    #: from the stream, not guessed from the prose - the scorer below infers
    #: recall by looking for member names, and inferring tool use the same way
    #: would be inventing data.
    tools_used: List[str] = field(default_factory=list)
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


def _member_names() -> Set[str]:
    """Every object name in the codebase, so we can spot them in prose."""
    import sqlite3
    from kb import store
    if not store.ready()[0]:
        return set()
    from kb import store
    db = store.facts()
    names = {r[0] for r in db.execute("SELECT DISTINCT name FROM object")}
    db.close()
    return names


def _mentioned(text: str, known: Set[str]) -> List[str]:
    """Object names the answer actually names.

    Matched against the real inventory rather than by pattern, so ordinary
    English words in capitals are not mistaken for member names.
    """
    found: List[str] = []
    for token in set(re.findall(r"\b[A-Z][A-Z0-9]{2,9}\b", text.upper())):
        if token in known and token not in found:
            found.append(token)
    return sorted(found)


def _declined(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in CANNOT_ANSWER)


def _read_stream(output: str) -> Dict[str, object]:
    """Pull tool calls, turns and token spend out of a stream-json run.

    ## Why this replaces reading the prose

    The scorer works by looking for member names in what the agent wrote,
    which is the right way to measure recall and the wrong way to measure
    anything else.  It could not say which tools were called, so the question
    the knowledge base exists to answer - is this surface actually used, and
    which parts of it are dead weight - had no instrument at all.

    `--output-format stream-json` carries one JSON object per line, including
    an `assistant` message per turn whose content blocks name each tool call,
    and a final `result` envelope with the turn count and token usage.  Both
    arms emit it, which matters: the server-side ledger cannot see the
    baseline arm at all, because Read, Grep and Glob never reach our server,
    and a measurement that only covers one arm cannot compare them.

    Tolerant by design.  The stream shape is not a versioned contract, so
    only the few fields used here are read and every unknown key is ignored.
    A line that will not parse is skipped rather than failing the ticket.
    """
    used: List[str] = []
    turns = 0
    tokens_in = tokens_out = 0
    cost = 0.0
    text_parts: List[str] = []

    for line in output.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")

        if kind == "assistant":
            turns += 1
            message = event.get("message") or {}
            for block in (message.get("content") or []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = str(block.get("name") or "")
                    if name:
                        used.append(name)
                elif block.get("type") == "text":
                    text_parts.append(str(block.get("text") or ""))

        elif kind == "result":
            usage = event.get("usage") or {}
            tokens_in = int(usage.get("input_tokens") or 0)
            tokens_out = int(usage.get("output_tokens") or 0)
            try:
                cost = float(event.get("total_cost_usd") or 0.0)
            except (TypeError, ValueError):
                cost = 0.0
            if event.get("num_turns"):
                turns = int(event["num_turns"])
            if event.get("result"):
                text_parts.append(str(event["result"]))

    return {"tools_used": used, "turns": turns,
            "input_tokens": tokens_in, "output_tokens": tokens_out,
            "cost_usd": cost, "text": "\n".join(text_parts)}


def run_one(ticket: Ticket, arm: str, known: Set[str],
            timeout: int = 300) -> Result:
    result = Result(ticket=ticket.id, band=ticket.band, arm=arm,
                    gold=list(ticket.gold), fix_site=ticket.fix_site)

    tools = BASELINE_TOOLS if arm == "baseline" else KB_TOOLS
    command = [
        "claude", "-p", ticket.prompt,
        "--allowed-tools", tools,
        "--permission-mode", "acceptEdits",
        # stream-json needs --verbose alongside -p.  Everything the scorer
        # used to read out of the plain text is still in the stream, and the
        # tool calls and token counts are only in the stream.
        "--output-format", "stream-json", "--verbose",
    ]

    started = time.time()
    try:
        completed = subprocess.run(
            command, cwd=str(config.ROOT), capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace")
        output = completed.stdout or ""
        if completed.returncode != 0 and not output.strip():
            result.ok = False
            result.error = (completed.stderr or "")[:400]
    except subprocess.TimeoutExpired:
        result.ok = False
        result.error = f"timed out after {timeout}s"
        output = ""
    except FileNotFoundError:
        result.ok = False
        result.error = "the claude CLI is not on PATH"
        output = ""

    result.seconds = time.time() - started

    # The prose the scorer greps for now comes out of the stream.  Falling
    # back to the raw capture keeps this working if the stream shape changes
    # or the CLI ignores the flag, rather than scoring every ticket zero.
    stream = _read_stream(output)
    prose = str(stream.get("text") or "")
    if prose:
        result.tools_used = list(stream.get("tools_used") or [])
        result.turns = int(stream.get("turns") or 0)
        result.input_tokens = int(stream.get("input_tokens") or 0)
        result.output_tokens = int(stream.get("output_tokens") or 0)
        result.cost_usd = float(stream.get("cost_usd") or 0.0)
        output = prose
    result.output = output[-4000:]

    if _aborted(output):
        result.ok = False
        result.error = result.error or (
            "run aborted: " + output.strip()[:120] if output.strip()
            else "no output")
        return result

    result.mentioned = _mentioned(output, known)
    result.hits = [g for g in ticket.gold if g in result.mentioned]
    result.recall = (len(result.hits) / len(ticket.gold)) if ticket.gold else 1.0
    result.fix_site_hit = bool(ticket.fix_site) and \
        ticket.fix_site in result.mentioned
    result.declined = _declined(output)

    if ticket.expect_cannot_answer:
        # naming members confidently instead of declining is the failure we
        # most want to catch
        result.false_confidence = not result.declined and len(result.mentioned) >= 2
        result.recall = 0.0 if result.false_confidence else 1.0

    return result


def _completed(arm: str) -> Dict[str, Result]:
    """Tickets already answered in this arm, from the newest run backwards."""
    out: Dict[str, Result] = {}
    for path in sorted(RESULTS.glob(f"{arm}-*.json")):
        for raw in json.loads(path.read_text(encoding="utf-8")):
            row = Result(**raw)
            if _aborted(row.output):
                continue
            if row.ok:
                out[row.ticket] = row
    return out


def run(arm: str, limit: Optional[int] = None, only: Optional[str] = None,
        timeout: int = 300, resume: bool = False) -> List[Result]:
    known = _member_names()
    if not known:
        print("no facts database - run kb.index.build first", file=sys.stderr)
        return []
    if shutil.which("claude") is None:
        print("the claude CLI is not on PATH", file=sys.stderr)
        return []

    chosen = [BY_ID[only]] if only else TICKETS
    if limit:
        chosen = chosen[:limit]

    results: List[Result] = []
    if resume:
        done = _completed(arm)
        results = [done[t.id] for t in chosen if t.id in done]
        chosen = [t for t in chosen if t.id not in done]
        print(f"  resuming: {len(results)} already answered, "
              f"{len(chosen)} to run")
        if not chosen:
            score(results, arm)
            return results

    print(f"  arm {arm}, {len(chosen)} ticket(s)")
    print("  " + "-" * 68)
    for ticket in chosen:
        result = run_one(ticket, arm, known, timeout=timeout)
        results.append(result)
        if not result.ok and "aborted" in result.error:
            print(f" ! {ticket.id:<10}{result.error[:60]}")
            print("   stopping - the rest would score as zeros. "
                  "Re-run with --resume when it clears.")
            break
        flag = "  " if result.ok else " !"
        note = ""
        if ticket.expect_cannot_answer:
            note = "declined" if result.declined else "ANSWERED ANYWAY"
        else:
            note = f"{len(result.hits)}/{len(ticket.gold)}"
        print(f"{flag} {ticket.id:<10}{ticket.band:<14}{note:<18}"
              f"{result.seconds:>6.0f}s  {result.error[:30]}")

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = RESULTS / f"{arm}-{stamp}.json"
    path.write_text(json.dumps([asdict(r) for r in results], indent=1),
                    encoding="utf-8")
    print(f"\n  wrote {path.relative_to(config.ROOT)}")
    score(results, arm)
    return results


def _median(values: Sequence[float]) -> float:
    """Median without importing statistics for one call.

    Median rather than mean throughout: one timed-out or one retried ticket
    skews a mean, and `kb/graph/bench.py` already argues the same thing about
    query timings.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def score(results: Sequence[Result], arm: str,
          keep: Optional[Set[str]] = None) -> Dict[str, float]:
    if not results:
        return {}
    dropped = [r for r in results if not r.ok
               or (keep is not None and r.ticket not in keep)]
    results = [r for r in results if r.ok
               and (keep is None or r.ticket in keep)]
    if not results:
        print(f"\n  arm {arm}: every run failed, nothing to score")
        return {}

    bands: Dict[str, List[Result]] = {}
    for r in results:
        bands.setdefault(r.band, []).append(r)

    print()
    print(f"  SCORE - arm {arm}")
    if dropped:
        print(f"  ({len(dropped)} run(s) excluded - did not complete)")
    print("  " + "=" * 68)
    print(f"  {'band':<16}{'n':>4}{'recall':>10}{'fix site':>11}{'avg s':>8}")
    print("  " + "-" * 68)
    out: Dict[str, float] = {}
    for band, items in bands.items():
        recall = sum(r.recall for r in items) / len(items)
        with_site = [r for r in items if r.fix_site]
        site = (sum(1 for r in with_site if r.fix_site_hit) / len(with_site)
                if with_site else float("nan"))
        seconds = sum(r.seconds for r in items) / len(items)
        site_text = "-" if with_site == [] else f"{site:.0%}"
        print(f"  {band:<16}{len(items):>4}{recall:>9.0%}{site_text:>11}"
              f"{seconds:>8.0f}")
        out[band] = recall
    print("  " + "-" * 68)
    overall = sum(r.recall for r in results) / len(results)
    print(f"  {'OVERALL':<16}{len(results):>4}{overall:>9.0%}")

    # ---- what the answer COST -------------------------------------------
    # Recall says whether the knowledge base can find the right member.  It
    # has never said what finding it cost, and on the last full run that was
    # the whole story: recall 88% against 90%, which is noise, and a median
    # 219s against 125s, which is not.  A surface whose advantage is
    # efficiency has to report efficiency.
    turns = [r.turns for r in results if r.turns]
    if turns:
        tokens = [r.input_tokens + r.output_tokens for r in results
                  if r.input_tokens]
        calls = [len(r.tools_used) for r in results if r.tools_used]
        print()
        print(f"  COST TO GET THERE - arm {arm}")
        print("  " + "-" * 68)
        print(f"    median turns      {_median(turns):>10,.0f}")
        if calls:
            print(f"    median tool calls {_median(calls):>10,.0f}")
        if tokens:
            print(f"    median tokens     {_median(tokens):>10,.0f}")
            print(f"    total tokens      {sum(tokens):>10,}")
        spend = sum(r.cost_usd for r in results)
        if spend:
            print(f"    total cost        {spend:>10.2f} USD")

    # ---- which tools were actually reached for ---------------------------
    # The question the surface is judged by.  A declared tool costs context
    # on every turn of every conversation, so one that no ticket reaches is
    # not free - and until the stream was parsed, nothing could tell.
    counted: Dict[str, int] = {}
    for r in results:
        for name in r.tools_used:
            counted[name] = counted.get(name, 0) + 1
    ours = {k: v for k, v in counted.items() if "alphaplus" in k}
    if counted:
        print()
        print(f"  TOOLS THE AGENT CHOSE - arm {arm}")
        print("  " + "-" * 68)
        for name, n in sorted(counted.items(), key=lambda kv: (-kv[1], kv[0])):
            short = name.split("__")[-1]
            tickets = sum(1 for r in results if name in r.tools_used)
            print(f"    {short:<26}{n:>5} call(s) across {tickets:>3} ticket(s)")
        if ours:
            try:
                from kb.server import server as rpc
                declared = {t["name"] for t in rpc.TOOLS}
                reached = {k.split("__")[-1] for k in ours}
                dead = sorted(declared - reached)
                if dead:
                    print()
                    print(f"    NEVER REACHED - {len(dead)} of "
                          f"{len(declared)} declared tools:")
                    print("      " + ", ".join(dead))
            except Exception:                                # noqa: BLE001
                pass

    liars = [r for r in results if r.false_confidence]
    if liars:
        print()
        print(f"  ! {len(liars)} unanswerable ticket(s) answered anyway: "
              + ", ".join(r.ticket for r in liars))
        print("    A knowledge base that invents an answer is worse than none.")
    print()
    out["overall"] = overall
    return out


def report() -> int:
    """Compare the most recent run of each arm."""
    if not RESULTS.exists():
        print("no results yet", file=sys.stderr)
        return 1
    latest: Dict[str, Path] = {}
    for path in sorted(RESULTS.glob("*.json")):
        arm = path.name.split("-")[0]
        latest[arm] = path
    if not latest:
        print("no results yet", file=sys.stderr)
        return 1

    loaded: Dict[str, List[Result]] = {}
    for arm, path in latest.items():
        rows = [Result(**r)
                for r in json.loads(path.read_text(encoding="utf-8"))]
        # results written before the abort guard existed can still be marked
        # bad, because the evidence is sitting in the saved output
        for row in rows:
            if _aborted(row.output):
                row.ok = False
                row.error = row.error or "run aborted"
        loaded[arm] = rows

    # Compare like with like: only tickets that completed in EVERY arm.  An
    # arm that died half way through must not look worse than one that ran.
    keep: Optional[Set[str]] = None
    if len(loaded) > 1:
        sets = [{r.ticket for r in rows if r.ok} for rows in loaded.values()]
        keep = set.intersection(*sets)
        total = max(len(rows) for rows in loaded.values())
        if len(keep) < total:
            print(f"\n  comparing the {len(keep)} of {total} ticket(s) that "
                  f"completed in every arm")

    scores: Dict[str, Dict[str, float]] = {}
    for arm, rows in loaded.items():
        scores[arm] = score(rows, arm, keep=keep)

    if "baseline" in scores and "kb" in scores:
        print("  BASELINE vs KNOWLEDGE BASE")
        print("  " + "=" * 68)
        print(f"  {'band':<16}{'baseline':>12}{'kb':>12}{'delta':>12}")
        print("  " + "-" * 68)
        for band in sorted(set(scores["baseline"]) | set(scores["kb"])):
            a = scores["baseline"].get(band, 0.0)
            b = scores["kb"].get(band, 0.0)
            print(f"  {band:<16}{a:>11.0%}{b:>12.0%}{b - a:>+12.0%}")
        print()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="run the ticket benchmark")
    ap.add_argument("--arm", choices=("baseline", "kb"), default="baseline")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--ticket", help="run one ticket by id")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--resume", action="store_true",
                    help="run only the tickets this arm has not answered yet")
    ap.add_argument("--report", action="store_true",
                    help="score the most recent run of each arm")
    args = ap.parse_args(argv)

    if args.report:
        return report()
    run(args.arm, limit=args.limit, only=args.ticket, timeout=args.timeout,
        resume=args.resume)
    return 0


if __name__ == "__main__":
    sys.exit(main())
