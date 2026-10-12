"""
Write the summary pages.

    python -m kb.summarise.build --estimate       what it would cost, no calls
    python -m kb.summarise.build --limit 10       try ten members
    python -m kb.summarise.build                  the cheap tier, everything
    python -m kb.summarise.build --deep 200       full source for the top 200
    python -m kb.summarise.build --stale          only what has changed

This layer is **optional**.  Facts, graph, search and lexicon answer most
tickets with no model involved at all, and the whole thing is off unless
ALPHAPLUS_SUMMARIES=1.  It exists for the vague ticket - "invoices are wrong
for Irish customers" - where the words in the ticket appear nowhere in the
source and something has to bridge business language to member names.

Two tiers, because cost varies by two orders of magnitude:

    cheap   every member, prompt built from facts only, ~1.5k tokens each
    deep    the most-changed members, full source read, ~10k tokens each

Staleness is tracked, not ignored.  Each page records the member hash it was
written from.  When the member moves on, the page is marked stale and the
tools say so.  Handing an agent a confident description of code that has since
changed is worse than handing it nothing.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .. import config
from . import prompts
from .provider import Provider, ProviderError

#: Rough token count.  Four characters per token is close enough for an
#: estimate, and an estimate is all this is for.
CHARS_PER_TOKEN = 4

#: Cap the source we send in the deep tier.  A few members run to thousands of
#: lines and the tail adds cost without adding understanding.
DEEP_SOURCE_LINES = 1200


def _page_path(name: str) -> Path:
    return config.SUMMARY_DIR / f"{name}.md"


def _candidates(db: sqlite3.Connection, stale_only: bool,
                deep: int) -> List[Tuple[str, str, str, bool]]:
    """(name, member_type, sha256, is_deep), most-depended-on first.

    Fan-in is the ranking because a program forty other programs call is where
    tickets land, and it is the only such signal readable from executable
    source.  Size breaks the tie: a 4,000 line member nobody calls still costs
    more to understand than a 30 line one.
    """
    db.row_factory = sqlite3.Row
    rows = list(db.execute("""
        SELECT h.name, h.member_type, h.sha256, h.summary_hash,
               (SELECT COUNT(DISTINCT e.caller) FROM call_edge e
                 WHERE e.callee = h.name) AS fan_in
        FROM member_hash h
        JOIN object o ON o.name = h.name
        ORDER BY fan_in DESC, o.lines DESC, h.name
    """))

    out: List[Tuple[str, str, str, bool]] = []
    for index, r in enumerate(rows):
        if stale_only and r["summary_hash"] == r["sha256"] \
                and _page_path(r["name"]).exists():
            continue
        out.append((r["name"], r["member_type"], r["sha256"], index < deep))
    return out


def _source_for(db: sqlite3.Connection, name: str) -> Optional[str]:
    if not config.LLM_SEND_SOURCE:
        return None
    row = db.execute("SELECT path FROM object WHERE name=?", (name,)).fetchone()
    if not row or not row[0]:
        return None
    # `object.path` is relative to the source root. Reading it as given always
    # raised OSError, which this function swallowed by returning None - so the
    # page was written from the fact sheet alone and nothing said so. Combined
    # with ALPHAPLUS_LLM_SOURCE defaulting off, no summary in this database has
    # ever seen a line of source.
    path = Path(row[0])
    if not path.is_absolute():
        path = Path(config.SOURCE_ROOT) / row[0]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.split("\n")
    if len(lines) > DEEP_SOURCE_LINES:
        lines = lines[:DEEP_SOURCE_LINES] + [
            f"... {len(lines) - DEEP_SOURCE_LINES:,} further lines not sent"]
    return "\n".join(lines)


def estimate(deep: int = 200) -> None:
    """What this would cost, without spending anything."""
    from kb import store
    db = store.facts(read_only=False)
    items = _candidates(db, stale_only=False, deep=deep)

    cheap_chars = deep_chars = 0
    sample = items[:400]                    # measure a sample, scale it up
    for name, _type, _sha, is_deep in sample:
        sheet = prompts.fact_sheet(db, name)
        size = len(prompts.build_prompt(sheet))
        if is_deep:
            deep_chars += size + 40_000     # source dominates the deep tier
        else:
            cheap_chars += size
    db.close()

    n_deep = min(deep, len(items))
    n_cheap = len(items) - n_deep
    sampled_cheap = max(1, sum(1 for i in sample if not i[3]))
    per_cheap = cheap_chars / sampled_cheap / CHARS_PER_TOKEN
    per_deep = 12_000

    in_tokens = n_cheap * per_cheap + n_deep * per_deep
    out_tokens = (n_cheap + n_deep) * 400

    provider = Provider()
    print(f"  provider   {provider.name} / {provider.model or '-'}")
    print(f"  members    {len(items):,}")
    print(f"  cheap tier {n_cheap:,} x ~{per_cheap:,.0f} input tokens")
    print(f"  deep tier  {n_deep:,} x ~{per_deep:,.0f} input tokens"
          f"  ({'source sent' if config.LLM_SEND_SOURCE else 'source NOT sent'})")
    print()
    print(f"  total      ~{in_tokens / 1e6:.1f}M input, "
          f"~{out_tokens / 1e6:.2f}M output")
    print()
    print("  Indicative cost at published rates:")
    for label, rate_in, rate_out in (
            ("deepseek-chat", 0.27, 1.10),
            ("gpt-4o-mini", 0.15, 0.60),
            ("claude-sonnet", 3.00, 15.00)):
        cost = in_tokens / 1e6 * rate_in + out_tokens / 1e6 * rate_out
        print(f"    {label:<16}${cost:,.0f}")
    print()
    print("  One-off. After this only changed members are re-written.")


def build(limit: Optional[int] = None, deep: int = 0, stale_only: bool = False,
          dry_run: bool = False) -> Dict[str, int]:
    if not config.ENABLE_SUMMARIES:
        print("summaries are switched off.\n"
              "  set ALPHAPLUS_SUMMARIES=1     to enable\n"
              "  set ALPHAPLUS_LLM=deepseek    to choose a model\n"
              "  set DEEPSEEK_API_KEY=...      to authorise it\n\n"
              "The knowledge base works without this - facts, graph, search\n"
              "and lexicon need no model.", file=sys.stderr)
        return {}
    from kb import store
    ok, why = store.ready()
    if not ok:
        print(why, file=sys.stderr)
        return {}

    provider = Provider()
    facts_only = not provider.usable
    if facts_only:
        print(f"  no model ({provider.why_not()})")
        print("  writing fact-only pages - nothing leaves the network\n")

    from . import store as pagestore
    config.SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    from kb import store
    db = store.facts(read_only=False)
    items = _candidates(db, stale_only=stale_only, deep=deep)
    if limit:
        items = items[:limit]

    counts = {"written": 0, "failed": 0, "skipped": 0}
    started = time.time()
    print(f"  {len(items):,} member(s) to write")

    for name, member_type, sha, is_deep in items:
        sheet = prompts.fact_sheet(
            db, name, source=_source_for(db, name) if is_deep else None)
        if not sheet:
            counts["skipped"] += 1
            continue
        if dry_run:
            counts["written"] += 1
            continue

        if facts_only:
            page = prompts.facts_only_page(name, sheet)
        else:
            try:
                body = provider.complete(prompts.SYSTEM,
                                         prompts.build_prompt(sheet))
            except ProviderError as exc:
                counts["failed"] += 1
                print(f"  ! {name:<12}{exc}"[:100])
                if counts["failed"] > 20 and counts["written"] == 0:
                    print("  stopping - nothing is succeeding")
                    break
                continue
            tier = "deep" if is_deep else "cheap"
            page = (f"# {name}\n\n{body}\n\n---\n"
                    f"_{tier} tier, written by {provider.name}/"
                    f"{provider.model} from source hash `{sha[:12]}`. "
                    f"Facts below are read from source; the prose above is "
                    f"generated and may be wrong._\n\n"
                    f"```\n{sheet}\n```\n")

        _page_path(name).write_text(page, encoding="utf-8", newline="\n")
        # ALSO record it in the `summary` table, not only on disk.
        #
        # vectors.py reads pages from `summary`; build.py wrote markdown
        # files and a member_hash marker and nothing else. So 500 member
        # pages existed on disk, the table stayed empty, and semantic search
        # had nothing to embed - the two halves of the pipeline never met.
        pagestore.write(db, pagestore.Page(
            scope="member", key=name, library="",
            text=page, written_by=(provider.name + "/" + provider.model
                                   if provider.name != "none"
                                   else pagestore.FACTS_ONLY),
            input_hash=sha), sheet)
        db.execute("UPDATE member_hash SET summary_hash=? "
                   "WHERE name=? AND member_type=?", (sha, name, member_type))
        counts["written"] += 1
        if counts["written"] % 50 == 0:
            db.commit()
            print(f"  {counts['written']:,} written  {provider.cost_line()}")

    db.commit()
    db.close()

    print()
    print(f"  written    {counts['written']:,}")
    if counts["failed"]:
        print(f"  failed     {counts['failed']:,}")
    print(f"  elapsed    {time.time() - started:.0f}s")
    if provider.calls:
        print(f"  usage      {provider.cost_line()}")
    print(f"  pages in   {config.SUMMARY_DIR}")
    return counts


def status() -> int:
    """How much of the codebase has a page, and how much of it is stale."""
    from kb import store
    ok, why = store.ready()
    if not ok:
        print(why, file=sys.stderr)
        return 1
    from kb import store
    db = store.facts(read_only=False)
    total, written, stale = 0, 0, 0
    for name, sha, summary_hash in db.execute(
            "SELECT name, sha256, summary_hash FROM member_hash"):
        total += 1
        if not _page_path(name).exists():
            continue
        written += 1
        if summary_hash != sha:
            stale += 1
    db.close()

    print(config.describe())
    print()
    print(f"  members    {total:,}")
    print(f"  with page  {written:,}  ({written / max(1, total):.0%})")
    print(f"  stale      {stale:,}   (member changed since the page was written)")
    if stale:
        print("\n  refresh with: python -m kb.summarise.build --stale")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="write summary pages")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--deep", type=int, default=0,
                    help="how many of the most-changed members get full source")
    ap.add_argument("--stale", action="store_true",
                    help="only members whose page is out of date")
    ap.add_argument("--estimate", action="store_true",
                    help="cost it without calling anything")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if args.status:
        return status()
    if args.estimate:
        estimate(deep=args.deep or 200)
        return 0
    build(limit=args.limit, deep=args.deep, stale_only=args.stale,
          dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
