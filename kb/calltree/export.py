"""Turn discovered flows plus their names into one JSON document.

Kept separate from rendering so the same document can feed a page, an MCP tool
or a diff between two releases. Nothing here calls a model; a member with no
name falls back to its member id and is flagged, because a hole that is visible
is worth more than a hole that is filled in silently.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from typing import Dict, List, Optional, Sequence

from . import discover, name as naming


def _node(node, names: Dict[str, naming.Named]) -> dict:
    got = names.get(node.name)
    return {
        "member": node.name,
        "title": got.title if got else node.name,
        "line": got.line if got else "",
        "confidence": got.confidence if got else "unnamed",
        "named": bool(got),
        "type": node.member_type,
        "library": node.library,
        "lines": node.lines,
        "via": node.via,
        "paragraph": node.paragraph,
        "at": node.line,
        "ubiquitous": node.ubiquitous,
        "children": [_node(k, names) for k in node.children],
    }


def _shared_writes(bucket) -> List[str]:
    """The files more than one flow in this bucket writes - why it is a group."""
    seen = defaultdict(int)
    for flow in bucket:
        for ent in flow.entities:
            if ent.writes:
                seen[ent.name] += 1
    return sorted(n for n, c in seen.items() if c > 1)


def document(db, capabilities: Optional[Sequence[str]] = None) -> dict:
    flows = discover.discover(db, capabilities)
    names = naming.existing(db)

    by_cap: Dict[str, List] = defaultdict(list)
    for flow in flows:
        by_cap[flow.capability].append(flow)

    out_caps = []
    for cap in sorted(by_cap):
        got = names.get(cap)
        features = []
        for flow in sorted(by_cap[cap],
                           key=lambda f: (f.kind, f.menu, f.option, f.key)):
            members = flow.members()
            named = sum(1 for m in members if m.name in names)
            # An interactive flow is named by the menu label the user reads.
            # A submitted job has no menu and therefore no label, so it takes
            # the business title of the program it runs - never the raw
            # "submitted by X" note, which names nothing.
            root_title = ""
            if flow.root and flow.root.name in names:
                root_title = names[flow.root.name].title
            features.append({
                "key": flow.key,
                "kind": flow.kind,
                "label": flow.label or root_title or flow.key,
                "menu": flow.menu,
                "option": flow.option,
                "submitted_by": flow.submitted_by,
                "note": flow.note,
                "root": _node(flow.root, names) if flow.root else None,
                "members": len(members),
                "named": named,
                "entities": [
                    {"name": e.name, "type": e.member_type,
                     "verbs": sorted(e.verbs), "writes": e.writes,
                     "touched_by": sorted(e.touched_by)}
                    for e in flow.entities],
            })
        # Trigger points that share written data, offered as one process. A
        # bucket of one is not a group - it is a flow that grouped with
        # nothing, and saying so is more honest than inventing a heading for
        # it. The viewer renders the real groups first and the rest as
        # standalone processes.
        buckets = discover.group(by_cap[cap])
        groups = []
        for bucket in buckets:
            if len(bucket) < 2:
                continue
            gkey = f"{cap}:{bucket[0].key}"
            gname = names.get(gkey)
            shared = _shared_writes(bucket)
            groups.append({
                "key": gkey,
                "title": gname.title if gname else "",
                "line": gname.line if gname else "",
                "named": bool(gname),
                "members": [f.key for f in bucket],
                "shared": shared,
                "count": len(bucket),
            })

        out_caps.append({
            "key": cap,
            "title": got.title if got else cap,
            "line": got.line if got else "",
            "named": bool(got),
            "groups": groups,
            "features": features,
            "interactive": sum(1 for f in by_cap[cap] if f.kind == "interactive"),
            "batch": sum(1 for f in by_cap[cap] if f.kind == "batch"),
        })

    every = {m.name for f in flows for m in f.members()}
    return {
        "capabilities": out_caps,
        "totals": {
            "capabilities": len(out_caps),
            "flows": len(flows),
            "interactive": sum(1 for f in flows if f.kind == "interactive"),
            "batch": sum(1 for f in flows if f.kind == "batch"),
            "members": len(every),
            "named": len([m for m in every if m in names]),
        },
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("capabilities", nargs="*")
    ap.add_argument("-o", "--out", default="calltree.json")
    args = ap.parse_args(argv)

    from ..store import connect
    with connect() as db:
        doc = document(db, args.capabilities)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    t = doc["totals"]
    print(f"  {t['capabilities']} capabilities, {t['flows']:,} flows "
          f"({t['interactive']:,} interactive, {t['batch']:,} batch)")
    print(f"  {t['named']:,}/{t['members']:,} members named")
    print(f"  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
