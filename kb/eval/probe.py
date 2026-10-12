"""
Measure the ranking, not the agent.

    python -m kb.eval.probe
    python -m kb.eval.probe --band vague --show 5
    python -m kb.eval.probe --twice          # prove the answer is stable

`kb.eval.run` is the real benchmark: it spawns a Claude session per ticket and
measures what an agent actually does with the knowledge base.  That is the
number that matters and it takes one to two hours, needs a server up, and has
been ruined twice by an expired session scoring as a confident zero.

This is the other half of the pair.  It runs the same tickets through
`trace_ticket` alone and reports recall against the same gold answers, in
seconds, with no subprocess, no model and no network.  It cannot tell you
whether an agent will reach the right answer; it can tell you whether the
knowledge base put the right answer within reach, which is the part we control.

It also reports **ties**, because a tie is how the ranking fails without
looking like it failed.  A gold answer sitting in a group of twenty-seven
equally-scored candidates is not a hit in any useful sense: which one an agent
reads first is decided by nothing.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .. import config
from .tickets import MISLEADING, PRECISE, TICKETS, Ticket, UNANSWERABLE, VAGUE

#: Report order: how much help the ticket text gives, most to least.
BANDS = (PRECISE, VAGUE, MISLEADING, UNANSWERABLE)

#: How far down the candidate list a gold answer still counts as found.  The
#: same default `trace_ticket` itself uses, so the number means what an agent
#: would actually be handed.
DEFAULT_LIMIT = 12

#: Scores within this of each other are treated as equal.  The ranking is
#: floating point and a 0.01 difference is not a judgement about anything.
TIE_EPSILON = 0.05


@dataclass
class Outcome:
    ticket: str
    band: str
    gold: List[str]
    hits: List[str] = field(default_factory=list)
    missed: List[str] = field(default_factory=list)
    returned: List[str] = field(default_factory=list)
    fix_site_hit: bool = False
    #: how many candidates share the top score
    tie_at_top: int = 0
    #: gold answers that landed inside a tie group of more than one
    gold_in_tie: List[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return len(self.hits) / len(self.gold) if self.gold else 1.0


def _tie_groups(ranked: Sequence[Tuple[str, float]]) -> Dict[str, int]:
    """name -> how many candidates share its score."""
    sizes: Counter = Counter()
    for _name, score in ranked:
        sizes[round(score / TIE_EPSILON)] += 1
    return {name: sizes[round(score / TIE_EPSILON)] for name, score in ranked}


def run_one(ticket: Ticket, limit: int = DEFAULT_LIMIT) -> Outcome:
    from ..server.tools import _rank_candidates

    ranked, _why, _blind, _named = _rank_candidates(ticket.prompt)
    shown = ranked[:limit]
    names = [name for name, _ in shown]
    groups = _tie_groups(shown)

    out = Outcome(ticket=ticket.id, band=ticket.band, gold=list(ticket.gold),
                  returned=names)
    out.hits = [g for g in ticket.gold if g in names]
    out.missed = [g for g in ticket.gold if g not in names]
    out.fix_site_hit = bool(ticket.fix_site) and ticket.fix_site in names
    out.tie_at_top = groups.get(names[0], 0) if names else 0
    out.gold_in_tie = [g for g in out.hits if groups.get(g, 1) > 1]
    return out


def report(outcomes: Sequence[Outcome], show: int = 0) -> None:
    by_band: Dict[str, List[Outcome]] = {}
    for o in outcomes:
        by_band.setdefault(o.band, []).append(o)

    print()
    print("  ALPHAPLUS knowledge base - ranking probe")
    print("  " + "=" * 68)
    print("  trace_ticket only. Not an agent, so this is what the knowledge")
    print("  base offered, not what an agent did with it.")
    print()
    print(f"  {'band':<12}{'n':>4}{'recall':>9}{'solved':>9}{'fix site':>10}"
          f"{'gold in a tie':>15}")
    print("  " + "-" * 68)

    for band in BANDS:
        rows = by_band.get(band, [])
        if not rows:
            continue
        gold = sum(len(o.gold) for o in rows)
        hits = sum(len(o.hits) for o in rows)
        solved = sum(1 for o in rows if not o.missed)
        with_site = [o for o in rows if o.fix_site_hit]
        tied = sum(1 for o in rows if o.gold_in_tie)
        recall = f"{100 * hits / gold:.0f}%" if gold else "-"
        print(f"  {band:<12}{len(rows):>4}{recall:>9}{solved:>4}/{len(rows):<4}"
              f"{len(with_site):>10}{tied:>12}/{len(rows)}")

    gold = sum(len(o.gold) for o in outcomes)
    hits = sum(len(o.hits) for o in outcomes)
    tied = sum(1 for o in outcomes if o.gold_in_tie)
    print("  " + "-" * 68)
    print(f"  {'OVERALL':<12}{len(outcomes):>4}"
          f"{100 * hits / gold if gold else 0:>8.0f}%"
          f"{sum(1 for o in outcomes if not o.missed):>4}/{len(outcomes):<4}"
          f"{'':>10}{tied:>12}/{len(outcomes)}")
    print()
    print(f"  gold names found: {hits}/{gold}")
    print(f"  a gold answer sat in a tie group in {tied} ticket(s) - in those, "
          f"being 'found'")
    print(f"  means being one of several the evidence could not separate.")

    misses = [o for o in outcomes if o.missed]
    if misses:
        print()
        print("  WHAT WAS MISSED")
        print("  " + "-" * 68)
        for o in misses:
            print(f"  {o.ticket:<10}{o.band:<12}missed {','.join(o.missed)}")
            if show:
                print(f"  {'':<22}returned {','.join(o.returned[:show])}")

    ties = [o for o in outcomes if o.tie_at_top > 1]
    if ties:
        print()
        print("  TICKETS WHOSE TOP CANDIDATE IS NOT ALONE")
        print("  " + "-" * 68)
        for o in sorted(ties, key=lambda x: -x.tie_at_top):
            print(f"  {o.ticket:<10}{o.tie_at_top:>3} candidates share the "
                  f"top score")
    print()


def explain(ticket: Ticket, depth: int = 12) -> None:
    """Why this ticket scored the way it did.

    Written because the alternative is guessing.  Three attempts at fixing the
    ranking were made from a hypothesis about the cause and two of them made
    things worse; the one that worked came from reading a gold answer's actual
    evidence and seeing that one ticket word had voted seven times.  Reading
    beats reasoning here, so the reading is a flag rather than a script someone
    retypes each time.
    """
    from ..server.tools import _rank_candidates, _indegree_map

    ranked, why, blind, _named = _rank_candidates(ticket.prompt)
    position = {name: i for i, (name, _) in enumerate(ranked)}
    scored = dict(ranked)
    degree = _indegree_map()

    print()
    print(f"  {ticket.id}  [{ticket.band}]  {ticket.summary}")
    if blind:
        print(f"  blind spot: {blind[:70]}")
    print()
    print("  GOLD")
    print("  " + "-" * 68)
    for gold in ticket.gold:
        at = position.get(gold)
        where = f"#{at + 1}" if at is not None else "not ranked"
        verdict = "found" if at is not None and at < depth else "MISSED"
        print(f"  {gold:<10}{where:>12}{scored.get(gold, 0):>8.1f}"
              f"{degree.get(gold, 0):>7}  {verdict}")
        for reason in list(dict.fromkeys(why.get(gold, [])))[:4]:
            print(f"  {'':<12}{reason}")

    print()
    print(f"  TOP {depth}")
    print("  " + "-" * 68)
    print(f"  {'#':>3} {'member':<10}{'score':>8}{'in-deg':>8}  evidence")
    for i, (name, score) in enumerate(ranked[:depth]):
        mark = " *" if name in ticket.gold else "  "
        reasons = list(dict.fromkeys(why.get(name, [])))
        print(f"{mark}{i + 1:>2} {name:<10}{score:>8.1f}"
              f"{degree.get(name, 0):>8}  {len(reasons)} piece(s): "
              f"{', '.join(reasons[:2])[:46]}")
    print()


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    ap.add_argument("--band", choices=BANDS, help="only this band")
    ap.add_argument("--ticket", help="only this ticket id")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"candidates an agent is assumed to read "
                         f"(default {DEFAULT_LIMIT})")
    ap.add_argument("--show", type=int, default=0,
                    help="also print the first N candidates for a miss")
    ap.add_argument("--twice", action="store_true",
                    help="run everything twice and prove the result is "
                         "identical - the ranking must not depend on set "
                         "iteration order")
    ap.add_argument("--explain", metavar="TICKET",
                    help="show one ticket's gold answers, their scores and "
                         "the evidence behind them, instead of a summary")
    args = ap.parse_args(argv)

    if args.explain:
        from .tickets import BY_ID
        ticket = BY_ID.get(args.explain.upper())
        if ticket is None:
            print(f"no such ticket: {args.explain}", file=sys.stderr)
            return 1
        from kb import store
        ok, why = store.ready()
        if not ok:
            print(why, file=sys.stderr)
            return 1
        explain(ticket, args.limit)
        return 0

    from kb import store
    ok, why = store.ready()
    if not ok:
        print(why, file=sys.stderr)
        return 1

    chosen = [t for t in TICKETS
              if (not args.band or t.band == args.band)
              and (not args.ticket or t.id == args.ticket)]
    if not chosen:
        print("no tickets matched", file=sys.stderr)
        return 1

    outcomes = [run_one(t, args.limit) for t in chosen]
    report(outcomes, args.show)

    if args.twice:
        again = [run_one(t, args.limit) for t in chosen]
        same = all(a.returned == b.returned for a, b in zip(outcomes, again))
        print(f"  determinism: two runs {'AGREE' if same else 'DISAGREE'}")
        if not same:
            for a, b in zip(outcomes, again):
                if a.returned != b.returned:
                    print(f"    {a.ticket}: {a.returned[:4]} vs {b.returned[:4]}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
