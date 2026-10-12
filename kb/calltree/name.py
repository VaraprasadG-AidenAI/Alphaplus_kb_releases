"""Give every node a business title, working from the top down.

Two rules came out of review and both are structural, not cosmetic.

**The inference is the headline.** `APMAIN` is a property; the title is
"Administration". `AGD100C` is a property; the title is what it does. A card
whose heading is a member name has failed regardless of how good the prose
beneath it is. Titles may be wrong - they are marked `inferred` and a reviewer
corrects them - but they may not be absent.

**Context flows downward.** "Go hierarchically, top to bottom, so it
understands the flow context... don't just show this file alone, summarize the
previous file, a few lines, so it understands what this is, where this is."
A program named in isolation gets "processes various transactions". The same
program named while told it sits under *Order Management -> Back Order posting*
gets "Back Order Release". So a child's prompt always carries its ancestors'
titles, and the levels are generated strictly in order.

The cheap half matters too: a member that already has a summary is named from
that summary (~400 tokens) rather than from its source (~37,000). On this
estate that is the difference between 5.0M input tokens and 0.2M.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .. import config
from ..summarise.provider import Provider, ProviderError
from . import discover

SYSTEM = (
    "You name parts of a legacy AS/400 COBOL system in business terms.\n"
    "You are given verified facts about one member and the business context it "
    "sits in. Answer with what it does for the business, never with what kind "
    "of file it is.\n"
    "Reply as JSON and nothing else: "
    '{"title": "...", "line": "...", "confidence": "high|medium|low"}\n'
    "  title       2-5 words, Title Case, a business capability or action. "
    "Never the member name, never a file type, never the word 'program', "
    "'module', 'file' or 'routine'.\n"
    "  line        one sentence, at most 20 words, saying what it does for the "
    "business. No member names.\n"
    "  confidence  low if the facts do not really tell you what it does. "
    "Say low rather than inventing something plausible."
)

#: Source is truncated at this many lines. The naming task needs the shape of a
#: member, not all of it; PROCEDURE DIVISION starts well inside this on every
#: member of this estate that was checked.
SOURCE_LINES = 400

_JSON = re.compile(r"\{.*\}", re.S)

#: IBM i system APIs, which are the same object on every IBM i in the world.
#:
#: These are platform constants, not estate knowledge: `QMHSNDPM` sends a
#: program message on a machine in Hamburg exactly as it does here, so naming
#: them is documentation rather than a guess. They are never in a customer's
#: source tree, and `discover.ubiquitous_members` finds them by SHAPE - an
#: unresolved call target named `Q...` - so a shop calling APIs not listed
#: here is handled without an edit.
#:
#: Two estate-wide utilities used to sit in this dict beside them. They were
#: members of the synthetic tree this was developed against, and on a real
#: estate those names mean nothing - or worse, mean something else. A member
#: is now treated as estate-wide because the call graph says it is reached
#: from everywhere, and gets GENERIC_UTILITY below rather than a label
#: somebody wrote down years ago.
FIXED: Dict[str, tuple] = {
    "QMHSNDPM": ("Send Program Message",
                 "IBM i system API that sends a message to a program queue."),
    "QCMDEXC":  ("Run Command",
                 "IBM i system API that executes a CL command string."),
    "QUSRJOBI": ("Retrieve Job Information",
                 "IBM i system API that returns attributes of a job."),
}

#: What an estate-wide utility is called when nothing better is known.
#:
#: Deliberately says what was MEASURED - reached from across the estate - and
#: nothing about what it does. A model asked to name such a member returns the
#: caller's business context, which is worse than admitting the shape is all
#: we know.
GENERIC_UTILITY = ("Shared Utility",
                   "Called from across the estate, so its business context "
                   "belongs to its callers rather than to itself.")

#: What a member's file type means for its place in a flow. Given to the model
#: so a CL driver and the program it calls do not come back with one title
#: between them.
#: The CL wording is deliberate. An earlier version said a CL driver "sets up
#: the environment and calls the program", and the model handed that straight
#: back as the title: "Setup Environment". Every CL in the estate sets up an
#: environment, so that names nothing. Name it for the work it launches.
ROLE = {
    "CLLE": ("the CL driver that launches this work. Name it for the business "
             "work it launches, never for its setup or housekeeping steps"),
    "CLP":  ("the CL driver that launches this work. Name it for the business "
             "work it launches, never for its setup or housekeeping steps"),
    "CBLLE": "the COBOL program holding the business logic",
    "CBL": "the COBOL program holding the business logic",
    "SQLCBLLE": "the COBOL program holding the business logic, with embedded SQL",
    "RPGLE": "an RPG program",
    "BND": "a bound service program called by others",
}


@dataclass
class Named:
    key: str
    kind: str                 # capability | feature | member
    title: str
    line: str
    confidence: str
    written_by: str
    context: str = ""


def _parse(reply: str, key: str) -> Optional[Named]:
    hit = _JSON.search(reply)
    if not hit:
        return None
    try:
        data = json.loads(hit.group(0))
    except json.JSONDecodeError:
        return None
    title = str(data.get("title") or "").strip()
    if not title or title.upper() == key.upper():
        return None
    return Named(key=key, kind="", title=title,
                 line=str(data.get("line") or "").strip(),
                 confidence=str(data.get("confidence") or "medium").strip(),
                 written_by="")


# ---------------------------------------------------------------------------
def ddl(db) -> None:
    cur = db.cursor()
    cur.execute("""
        create table if not exists kb.calltree_name (
            key         text primary key,
            kind        text not null,
            title       text not null,
            line        text not null default '',
            confidence  text not null default 'medium',
            written_by  text not null default '',
            context     text not null default ''
        )""")
    db.commit()


def existing(db) -> Dict[str, Named]:
    cur = db.cursor()
    cur.execute("select key, kind, title, line, confidence, written_by, "
                "context from calltree_name")
    return {r[0]: Named(*r) for r in cur.fetchall()}


def save(db, rows: Sequence[Named]) -> None:
    cur = db.cursor()
    for r in rows:
        cur.execute("""
            insert into calltree_name
                (key, kind, title, line, confidence, written_by, context)
            values (?, ?, ?, ?, ?, ?, ?)
            on conflict (key) do update set
                kind=excluded.kind, title=excluded.title, line=excluded.line,
                confidence=excluded.confidence,
                written_by=excluded.written_by, context=excluded.context
        """, (r.key, r.kind, r.title, r.line, r.confidence, r.written_by,
              r.context))
    db.commit()


# ---------------------------------------------------------------------------
class Namer:
    def __init__(self, db, provider: Provider, workers: int = 8) -> None:
        self.db = db
        self.provider = provider
        self.workers = workers
        self.lock = threading.Lock()
        self.done = 0
        self.failed: List[str] = []

        cur = db.cursor()
        cur.execute("select key, text from summary")
        self.summaries: Dict[str, str] = dict(cur.fetchall())
        cur.execute("select name, path, member_type, lines from object")
        self.meta = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}

    # -- evidence -------------------------------------------------------
    def _evidence(self, member: str) -> str:
        """The summary if we have one, else the head of the source."""
        page = self.summaries.get(member)
        if page:
            body = page.split("\n---\n")[0]        # prose, not the fact sheet
            return f"An existing description of {member}:\n\n{body[:2500]}"

        path, member_type, lines = self.meta.get(member, (None, "", 0))
        if not path:
            return f"{member}: no source located."
        # `object.path` is relative to the source root. Reading it as given
        # yields nothing, and a member with no evidence still gets named -
        # from its surrounding context alone, which is how a message-sending
        # API came back as "Order Data Archiving".
        p = Path(path)
        if not p.is_absolute():
            p = Path(config.SOURCE_ROOT) / path
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return f"{member}: source unreadable at {p}."
        head = text.split("\n")[:SOURCE_LINES]
        more = max(0, len(text.split("\n")) - SOURCE_LINES)
        if more:
            head.append(f"... {more:,} further lines not sent")
        return (f"Source of {member} ({member_type}, {lines:,} lines):\n\n"
                + "\n".join(head))

    def _ask(self, key: str, kind: str, context: str, evidence: str
             ) -> Optional[Named]:
        user = f"{context}\n\n{evidence}"
        try:
            reply = self.provider.complete(SYSTEM, user, max_tokens=260)
        except ProviderError as exc:
            with self.lock:
                self.failed.append(f"{key}: {exc}")
            return None
        named = _parse(reply, key)
        if named is None:
            with self.lock:
                self.failed.append(f"{key}: unusable reply")
            return None
        named.kind = kind
        named.written_by = f"{self.provider.name}/{self.provider.model}"
        named.context = context.replace("\n", " ")[:300]
        with self.lock:
            self.done += 1
            if self.done % 25 == 0:
                print(f"    named {self.done:,}...", flush=True)
        return named

    # -- the three levels -----------------------------------------------
    def capabilities(self, flows) -> Dict[str, Named]:
        """Name each family from the menu labels its options carry."""
        labels = defaultdict(list)
        for f in flows:
            if f.label:
                labels[f.capability].append(f.label)

        out: Dict[str, Named] = {}
        for cap, seen in sorted(labels.items()):
            sample = "\n".join(f"  - {l}" for l in sorted(set(seen))[:40])
            context = (f"An AS/400 application has a family of menus with the "
                       f"prefix {cap!r}. Name the business capability these "
                       f"menu options belong to.")
            named = self._ask(cap, "capability", context,
                              f"Its menu options are:\n{sample}")
            if named:
                out[cap] = named
        return out

    def groups(self, flows, caps: Dict[str, Named],
               skip: Optional[set] = None) -> List[Named]:
        """Name each bundle of trigger points that share written data.

        The evidence is what the bundle is: the entry points it contains and
        the files they all write. That is what makes them one process, so it
        is what the name should come from.
        """
        skip = skip or set()
        by_cap: Dict[str, List] = defaultdict(list)
        for flow in flows:
            by_cap[flow.capability].append(flow)

        jobs = []
        for cap, group_flows in sorted(by_cap.items()):
            cap_title = caps[cap].title if cap in caps else cap
            for bucket in discover.group(group_flows):
                if len(bucket) < 2:
                    continue
                key = f"{cap}:{bucket[0].key}"
                if key in skip:
                    continue
                labels = [f.label or (f.submitted_by and f"job {f.submitted_by}")
                          or f.key for f in bucket]
                shared = sorted({e.name for f in bucket for e in f.entities
                                 if e.writes})
                context = (
                    f"Business capability: {cap_title}\n"
                    f"These {len(bucket)} entry points all write the same data, "
                    f"so they are one business process. Name that process.")
                evidence = ("The entry points are:\n"
                            + "\n".join(f"  - {l}" for l in labels[:12])
                            + "\n\nThey share these written files:\n"
                            + "\n".join(f"  - {s}" for s in shared[:12]))
                jobs.append((key, context, evidence))

        print(f"    {len(jobs):,} groups to name")
        out: List[Named] = []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(self._ask, k, "group", c, e)
                       for k, c, e in jobs]
            for fut in futures:
                got = fut.result()
                if got:
                    out.append(got)
        return out

    def members(self, flows, caps: Dict[str, Named],
                skip: Optional[set] = None) -> List[Named]:
        """Name every member, with its capability and feature as context."""
        skip = skip or set()
        jobs = []
        seen = set()
        wide = set()
        for flow in flows:
            cap = caps.get(flow.capability)
            cap_title = cap.title if cap else flow.capability
            feature = flow.label or (
                f"a batch job submitted by {flow.submitted_by}")
            for node in flow.members():
                if node.name in seen or node.name in skip:
                    continue
                seen.add(node.name)
                if node.name in FIXED:
                    continue
                # Measured as reached from across the estate, so there is no
                # business context to ask for - see discover.ubiquitous_members.
                # Asking anyway returns the CALLER's context stated as fact.
                if getattr(node, "ubiquitous", False):
                    wide.add(node.name)
                    continue
                role = ROLE.get(node.member_type, "part of this flow")
                context = (
                    f"Business capability: {cap_title}\n"
                    f"This runs under the {'menu option' if flow.kind == 'interactive' else 'batch job'}"
                    f": {feature}\n"
                    f"{node.name} is {role}.\n"
                    f"Name the part of that work handled by {node.name}.")
                jobs.append((node.name, context))

        print(f"    {len(jobs):,} members to name "
              f"({sum(1 for n, _ in jobs if n in self.summaries):,} from an "
              f"existing summary, the rest from source)")

        out: List[Named] = [
            Named(key=k, kind="member", title=t, line=l, confidence="exact",
                  written_by="fixed", context="IBM i system API")
            for k, (t, l) in FIXED.items() if k in seen]
        # `inferred`, not `exact`: that the member is estate-wide is measured,
        # but the label says nothing about what it does, and Rule 2 asks for
        # the difference to show.
        out += [Named(key=n, kind="member", title=GENERIC_UTILITY[0],
                      line=GENERIC_UTILITY[1], confidence="inferred",
                      written_by="fixed",
                      context="called from across the estate")
                for n in sorted(wide)]
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(self._ask, n, "member", c,
                                   self._evidence(n)) for n, c in jobs]
            for fut in futures:
                got = fut.result()
                if got:
                    out.append(got)
        return out


# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="name the call tree, top down")
    ap.add_argument("capabilities", nargs="*", help="ORD CUS FIN ...")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after this many members (a smoke test)")
    ap.add_argument("--redo", action="store_true",
                    help="re-name members that already have a name")
    args = ap.parse_args(argv)

    provider = Provider()
    if not provider.usable:
        print(f"no model: {provider.why_not()}", file=sys.stderr)
        return 1
    print(f"  model    {provider.name}/{provider.model}")

    from ..store import connect
    with connect() as db:
        ddl(db)
        have = existing(db)
        flows = discover.discover(db, args.capabilities)
        print(f"  flows    {len(flows):,}")

        namer = Namer(db, provider, args.workers)

        print("  naming capabilities...")
        caps = {k: v for k, v in have.items() if v.kind == "capability"}
        # Name the ones that have no name yet, not "all or nothing": an
        # earlier run that named ORD must not stop CUS and FIN being named.
        todo = [f for f in flows
                if args.redo or f.capability not in caps]
        fresh = namer.capabilities(todo) if todo else {}
        caps.update(fresh)
        save(db, list(fresh.values()))
        for key, named in sorted(caps.items()):
            print(f"    {key:<12} {named.title}")

        print("  naming groups...")
        gskip = set() if args.redo else {k for k, v in have.items()
                                         if v.kind == "group"}
        save(db, namer.groups(flows, caps, gskip))

        print("  naming members...")
        skip = set() if args.redo else {k for k, v in have.items()
                                        if v.kind == "member"}
        if args.limit:
            flows = flows[:args.limit]
        rows = namer.members(flows, caps, skip)
        save(db, rows)

        print(f"\n  named    {len(rows):,}")
        print(f"  failed   {len(namer.failed):,}")
        for line in namer.failed[:10]:
            print(f"    {line}")
        print(f"  usage    {provider.cost_line()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
