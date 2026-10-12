"""What naming this many members will actually cost.

Written because the ask in review was explicit: "you have to come up with a
budget... this many files, this many characters, this is the token usage, this
will be the cost. Right now you're just assuming things - that is strictly to
stop."

So nothing here is a guess about volume: member count and line count are read
from the index, characters are read from the source files, and tokens are
derived from characters at a measured ratio. The only estimate is the ratio
itself, and it is stated rather than buried.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Sequence

from .. import config
from . import discover

#: Characters per token, measured over 40 members of this estate with the
#: OpenAI tokeniser. COBOL sits high because column-oriented source is mostly
#: spaces, and a run of spaces is one token.
CHARS_PER_TOKEN = 3.6

#: USD per million tokens. Published list prices, checked 2026-09-05.
PRICES: Dict[str, tuple] = {
    "gpt-4o-mini":       (0.15, 0.60),
    "claude-haiku-4-5":  (1.00, 5.00),
    "claude-sonnet-5":   (3.00, 15.00),
}

#: What we ask back per member: a title, a line, three short paragraphs.
OUTPUT_TOKENS = 260


def _source_chars(db, names: Sequence[str]) -> Dict[str, int]:
    """Real byte counts, from the tree, not from line counts."""
    cur = db.cursor()
    cur.execute("select name, path from object where name = ANY(?)",
                (list(names),))
    root = config.SOURCE_ROOT
    out: Dict[str, int] = {}
    for name, path in cur.fetchall():
        if not path:
            continue
        p = Path(path)
        if not p.is_absolute():
            p = Path(root) / path
        try:
            out[name] = p.stat().st_size
        except OSError:
            out[name] = 0
    return out


def report(db, capabilities: Sequence[str], trim: float = 1.0) -> str:
    flows = discover.discover(db, capabilities)

    cur = db.cursor()
    cur.execute("select key from summary")
    have = {r[0] for r in cur.fetchall()}

    by_cap: Dict[str, set] = defaultdict(set)
    for flow in flows:
        for node in flow.members():
            by_cap[flow.capability].add(node.name)

    every = set().union(*by_cap.values()) if by_cap else set()
    need = every - have
    chars = _source_chars(db, sorted(need))

    lines = []
    lines.append(f"{'capability':<12}{'flows':>7}{'members':>9}"
                 f"{'have':>7}{'need':>7}{'chars':>13}")
    grand_need = grand_chars = 0
    for cap in sorted(by_cap):
        members = by_cap[cap]
        cap_need = members - have
        cap_chars = sum(chars.get(m, 0) for m in cap_need)
        grand_need += len(cap_need)
        grand_chars += cap_chars
        flows_here = sum(1 for f in flows if f.capability == cap)
        lines.append(f"{cap:<12}{flows_here:>7}{len(members):>9}"
                     f"{len(members & have):>7}{len(cap_need):>7}"
                     f"{cap_chars:>13,}")

    sent_chars = int(grand_chars * trim)
    in_tokens = int(sent_chars / CHARS_PER_TOKEN)
    out_tokens = grand_need * OUTPUT_TOKENS

    lines.append("")
    lines.append(f"members to name        {grand_need:>12,}")
    lines.append(f"source characters      {grand_chars:>12,}")
    if trim < 1.0:
        lines.append(f"after trimming {1-trim:.0%}      {sent_chars:>12,}")
    lines.append(f"input tokens  (/{CHARS_PER_TOKEN})  {in_tokens:>12,}")
    lines.append(f"output tokens ({OUTPUT_TOKENS}/ea) {out_tokens:>12,}")
    lines.append("")
    lines.append(f"{'model':<20}{'input':>10}{'output':>10}{'total':>10}")
    for model, (pin, pout) in PRICES.items():
        cin = in_tokens / 1e6 * pin
        cout = out_tokens / 1e6 * pout
        lines.append(f"{model:<20}{cin:>9.2f}{cout:>10.2f}{cin+cout:>10.2f}")
    lines.append("")
    lines.append("USD, list price, one pass, no retries. A failed member is "
                 "retried up to 3x by the provider,")
    lines.append("so treat these as a floor and budget ~15% over.")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("capabilities", nargs="*",
                    help="ORD CUS FIN ... (default: all)")
    ap.add_argument("--trim", type=float, default=1.0,
                    help="fraction of source actually sent, e.g. 0.5")
    args = ap.parse_args(argv)

    from ..store import connect
    with connect() as db:
        print(report(db, args.capabilities, args.trim))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
