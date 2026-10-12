"""Find the trigger points, and the flow under each one.

No model is involved here. Everything below is read from the indexed facts, so
it is reproducible and can be checked against the source. The naming pass in
`name.py` is the only part that infers anything.

Two kinds of trigger point, because the machine has two ways of starting work:

    interactive   a menu option - `kb.menu_option` where action = 'call'
    batch         a job submission - `kb.call_edge` where kind = 'sbmjob'

The job *scheduler* remains a blind spot; the replica has no scheduler and the
real extract will not ship one. So a batch trigger here means "source submits
this job", never "this job runs at 02:00". That limit is carried on the flow
itself rather than left for the reader to discover.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set

#: How far to walk below a trigger. Review asked for "two, three levels"; the
#: fourth adds only shared utilities (message senders, date routines) that
#: appear under every flow and tell you nothing about this one.
DEPTH = 3

#: How widely a member must be called before it stops characterising anything.
#:
#: A member reached from most of the estate has no business context of its
#: own: whatever surrounds it belongs to its caller. Asked to name the OS
#: message-sending API beneath an order flow, a model answered "Order Data
#: Archiving" - confidently, and about the caller.
#:
#: This WAS a literal set of five names, two of which were members of the
#: synthetic tree this was developed against. Those names mean nothing on a
#: real estate, and worse, a customer member that happens to share one would
#: have been silently mislabelled. The property is not "is called ERRSRV"; it
#: is "is called from everywhere", which is a fact in the call graph and is
#: therefore measured rather than listed.
#:
#: 5% of all callers is deliberately low. The cost of marking a member
#: ubiquitous is one generic label instead of a guessed one; the cost of
#: missing one is a wrong business title on a shared utility, repeated under
#: every flow it appears in.
UBIQUITOUS_SHARE = 0.05


def ubiquitous_members(db) -> "Set[str]":
    """Members called from so much of the estate that they characterise none.

    Two populations, both derived:

    * **estate-wide utilities** - distinct callers at or above
      `UBIQUITOUS_SHARE` of every calling member in the index.
    * **system APIs** - a call target that is not a member of this source tree
      and is named like an IBM i program object (`Q` + letters/digits, up to
      ten characters). `QMHSNDPM`, `QCMDEXC` and `QUSRJOBI` are found by that
      shape, not by being written down, so a shop calling a different set of
      OS APIs gets the same treatment with no edit here.
    """
    cur = db.cursor()
    cur.execute("select count(distinct caller) from call_edge")
    row = cur.fetchone()
    total = (row[0] if row else 0) or 0
    if not total:
        return set()
    floor = max(2, int(total * UBIQUITOUS_SHARE))

    cur.execute("select callee, count(distinct caller) c from call_edge "
                "group by callee having c >= ?", (floor,))
    wide = {r[0] for r in cur.fetchall() if r[0]}

    cur.execute("select distinct e.callee from call_edge e "
                "left join object o on o.name = e.callee "
                "where o.name is null and e.callee like 'Q%'")
    system = {r[0] for r in cur.fetchall()
              if r[0] and len(r[0]) <= 10 and r[0][1:].isalnum()}
    return wide | system

_FAMILY = re.compile(r"^([A-Z]+?)(?:MEN|MAIN)")


def family_of(menu: str) -> str:
    """ORDMEN01 -> ORD, APMAIN07 -> APMAIN, RPTMENU -> RPT."""
    hit = _FAMILY.match(menu)
    if hit and hit.group(1):
        return hit.group(1)
    return re.sub(r"(MENU|MEN)?\d*$", "", menu) or menu


@dataclass
class Node:
    """One member inside a flow."""
    name: str
    member_type: str = ""
    library: str = ""
    lines: int = 0
    depth: int = 0
    via: str = ""                       # how the parent reached it
    paragraph: str = ""                 # where in the parent
    line: int = 0
    children: List["Node"] = field(default_factory=list)
    ubiquitous: bool = False


@dataclass
class Entity:
    """A file the flow reads or writes."""
    name: str
    verbs: Set[str] = field(default_factory=set)
    member_type: str = ""
    touched_by: Set[str] = field(default_factory=set)

    @property
    def writes(self) -> bool:
        return bool(self.verbs & {"WRITE", "REWRITE", "UPDATE", "DELETE"})


@dataclass
class Flow:
    """One trigger point and everything under it."""
    key: str
    kind: str                           # interactive | batch
    capability: str
    menu: str = ""
    option: str = ""
    label: str = ""                     # the label the menu carries, verbatim
    root: Optional[Node] = None
    entities: List[Entity] = field(default_factory=list)
    submitted_by: str = ""              # batch only
    note: str = ""

    def members(self) -> List[Node]:
        out: List[Node] = []

        def walk(n: Node) -> None:
            out.append(n)
            for kid in n.children:
                walk(kid)

        if self.root:
            walk(self.root)
        return out


class Graph:
    """The slice of the index this package needs, read once."""

    def __init__(self, db) -> None:
        self.ubiquitous = ubiquitous_members(db)
        cur = db.cursor()

        cur.execute("select name, member_type, library, lines from object")
        self.meta: Dict[str, tuple] = {
            r[0]: (r[1] or "", r[2] or "", r[3] or 0) for r in cur.fetchall()}

        cur.execute("select caller, callee, kind, paragraph, line "
                    "from call_edge "
                    "where kind in ('cl','cobol','menu','sbmjob')")
        self.calls: Dict[str, List[tuple]] = defaultdict(list)
        for caller, callee, kind, para, line in cur.fetchall():
            self.calls[caller].append((callee, kind, para or "", line or 0))

        cur.execute("select member, file, verb from io_site")
        self.io: Dict[str, List[tuple]] = defaultdict(list)
        for member, fil, verb in cur.fetchall():
            self.io[member].append((fil, (verb or "").upper()))

        cur.execute("select menu, option, label, target, command "
                    "from menu_option "
                    "where action = 'call' and target is not null "
                    "and target <> ''")
        self.options = cur.fetchall()

    # ------------------------------------------------------------------
    def _node(self, name: str, depth: int, via: str = "",
              paragraph: str = "", line: int = 0) -> Node:
        member_type, library, lines = self.meta.get(name, ("", "", 0))
        return Node(name=name, member_type=member_type, library=library,
                    lines=lines, depth=depth, via=via, paragraph=paragraph,
                    line=line, ubiquitous=name in self.ubiquitous)

    def _walk(self, name: str, depth: int, seen: Set[str]) -> Node:
        node = self._node(name, depth)
        if depth >= DEPTH:
            return node
        for callee, kind, para, line in sorted(self.calls.get(name, [])):
            if callee in seen:
                continue                # a cycle, or a utility already shown
            seen.add(callee)
            kid = self._walk(callee, depth + 1, seen)
            kid.via, kid.paragraph, kid.line = kind, para, line
            node.children.append(kid)
        return node

    def _entities(self, members: Sequence[str]) -> List[Entity]:
        found: Dict[str, Entity] = {}
        for member in members:
            for fil, verb in self.io.get(member, []):
                ent = found.setdefault(fil, Entity(
                    name=fil, member_type=self.meta.get(fil, ("", "", 0))[0]))
                ent.verbs.add(verb)
                ent.touched_by.add(member)
        return sorted(found.values(), key=lambda e: (not e.writes, e.name))

    # ------------------------------------------------------------------
    def interactive(self) -> List[Flow]:
        flows = []
        for menu, option, label, target, command in self.options:
            root = self._walk(target, 0, {target})
            flow = Flow(key=f"{menu}#{option}", kind="interactive",
                        capability=family_of(menu), menu=menu,
                        option=str(option), label=(label or "").strip(),
                        root=root)
            flow.entities = self._entities([n.name for n in flow.members()])
            flows.append(flow)
        return flows

    def batch(self, owner: Optional[Dict[str, str]] = None) -> List[Flow]:
        """Job submissions. The submitter is the evidence; there is no clock.

        A submitted job has no menu, so it has no family to take a capability
        from. It inherits the capability of whoever submits it - which is known
        whenever that submitter appears in an interactive flow. When it does
        not, the flow is `UNASSIGNED` rather than filed under a member name;
        an honest gap beats a capability called `UTL401C`.
        """
        owner = owner or {}
        flows, seen = [], set()
        for caller, edges in sorted(self.calls.items()):
            for callee, kind, para, line in edges:
                if kind != "sbmjob" or callee in seen:
                    continue
                seen.add(callee)
                root = self._walk(callee, 0, {callee})
                flow = Flow(
                    key=f"SBMJOB:{callee}", kind="batch",
                    capability=owner.get(caller, "UNASSIGNED"),
                    submitted_by=caller, label="", root=root,
                    note=("submitted by " + caller +
                          "; no schedule is visible in source"))
                flow.entities = self._entities(
                    [n.name for n in flow.members()])
                flows.append(flow)
        return flows


#: A file written by more than this share of all flows is plumbing, not a
#: subject. Audit and security tables are written by everything; grouping on
#: them merges an entire capability into one meaningless blob.
PLUMBING_SHARE = 0.05


def group(flows: Sequence[Flow]) -> List[List[Flow]]:
    """Trigger points that work on the same data, gathered into one process.

    Asked for in review, with an example: an HR user opens an onboarding form,
    an employee uploads a document. Two entry points, two flows - but one
    business process, "employee onboarding". The instruction was explicit that
    this is the holistic half and entry-point flows alone were acceptable:
    "I'm not asking ready-made that both processes should be correlated and
    grouped. At least entry-point faced flow you show me."

    So the grouping is offered, never forced. Two flows join when they WRITE a
    file in common - shared writes mean shared subject matter, whereas shared
    reads only mean both consulted the customer master. Flows that group with
    nothing stay on their own rather than being filed under an invented
    heading.

    Union-find over shared written files, per capability.
    """
    if not flows:
        return []

    writes = Counter()
    for flow in flows:
        for ent in flow.entities:
            if ent.writes:
                writes[ent.name] += 1
    cut = max(2, int(len(flows) * PLUMBING_SHARE))
    plumbing = {n for n, c in writes.items() if c > cut}

    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    by_file: Dict[str, List[str]] = defaultdict(list)
    for flow in flows:
        find(flow.key)
        for ent in flow.entities:
            if ent.writes and ent.name not in plumbing:
                by_file[ent.name].append(flow.key)
    for keys in by_file.values():
        for other in keys[1:]:
            union(keys[0], other)

    buckets: Dict[str, List[Flow]] = defaultdict(list)
    for flow in flows:
        buckets[find(flow.key)].append(flow)
    out = list(buckets.values())
    out.sort(key=lambda g: (-len(g), g[0].key))
    return out


def discover(db, capabilities: Optional[Sequence[str]] = None) -> List[Flow]:
    """Every flow, optionally restricted to some capabilities.

    Interactive first, because the batch flows take their capability from
    whichever interactive flow already owns the submitting member.
    """
    graph = Graph(db)
    interactive = graph.interactive()

    owner: Dict[str, str] = {}
    for flow in interactive:
        for node in flow.members():
            owner.setdefault(node.name, flow.capability)

    flows = interactive + graph.batch(owner)
    if capabilities:
        wanted = {c.upper() for c in capabilities}
        flows = [f for f in flows if f.capability in wanted]
    return flows
