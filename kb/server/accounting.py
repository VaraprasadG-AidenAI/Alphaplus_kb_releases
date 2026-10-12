"""
What the knowledge base has handed out, and what the report never placed.

    from kb.server import accounting
    accounting.note("what_uses", result_text)     # at the tool choke point
    accounting.reconcile(report_text)             # before the report goes out

## Why this exists

An impact report is wrong in two directions, and only one of them is visible.
A member that should not be in the list is argued about; a member that fell
out of the list is not, because nothing in the report says it was ever seen.

On one analysis a sweep returned seven copybooks that declared the changed
field at a literal width.  One was quoted in the risks section as an
interesting defect and the other six appeared in no change set, no recompile
list and no appendix.  The knowledge base had already handed over all seven.
No better search, no rebuilt index and no re-worded skill would have recovered
them, because nothing was missing from the data - the data was in hand and was
not carried forward.

The check that catches this is not a rule an agent follows.  It is a diff:

    every member any tool returned
      MINUS
    every member named in the final report
      =
    the members that leaked

That subtraction cannot be forgotten under context pressure, cannot be
half-done, and does not degrade over a long session the way an instruction
does.  This module is the left-hand side of it, kept by the server rather than
by the agent, because the agent is the thing being checked.

## Three states, and there is no fourth

A member the tools returned must end up in exactly one of:

    1. the change set        - it is edited
    2. the recompile set     - it is rebuilt
    3. explicitly excluded   - with the tool result that justifies excluding it

"Interesting enough to quote as a risk" is not a state.  Quoting a member as
evidence for a finding does not place it, and that is precisely how the six
went missing.

## Why names are validated against the index

The alternative is parsing each tool's table layout, which means this module
has to know the shape of every tool's output and breaks whenever one changes.
Instead every uppercase token in a result is intersected with the set of names
the index actually holds.  A token that is not a member name is discarded, so
prose, column headings and SQL keywords cannot pollute the ledger, and a tool
can change its formatting freely without touching this file.

## Scope

Per process, in memory.  An MCP stdio server is one conversation, which is
exactly the unit being reconciled - a report is written in one session.  It is
deliberately NOT persisted: a ledger that outlived the session would carry
members from somebody else's ticket into this one's reconciliation, which is a
different and more confusing kind of wrong.
"""

from __future__ import annotations

import re
import threading
from typing import Dict, List, Optional, Set, Tuple

#: Tools whose job is to ENUMERATE MEMBERS.
#:
#: The criterion is structural and has nothing to do with any one ticket: a
#: tool belongs here when a member name in its output is a lead the caller is
#: expected to act on.  Tools that describe a single named thing the caller
#: already had (`read_member`, `program_structure`) hand out nothing new, and
#: `coverage_report` names libraries rather than members.
#:
#: Being too generous here is safe - an extra name shows up as unaccounted and
#: gets one line of explanation.  Being too narrow is not: a missing name is
#: invisible, which is the whole failure this module exists to prevent.
SWEEP_TOOLS: Set[str] = {
    "search_source",
    "what_uses",
    "what_it_touches",
    "where_is_field_used",
    "who_touches_file",
    "who_calls",
    "what_it_calls",
    "resolve_files",
    "trace_ticket",
    "find_similar",
    "route_ticket",
    "blast_radius",
    "describe_file",
    "file_family",
    "menu_path_to",
    "path_between",
    "resolve_overrides",
    "explain_screen",
}

#: An IBM i object or member name: starts with a letter, at most ten
#: characters, from the character set the platform allows.  Deliberately
#: permissive - the index intersection below is what makes it precise.
_TOKEN = re.compile(r"[A-Z][A-Z0-9_$#@]{1,9}")

_lock = threading.Lock()

#: name -> the tools that returned it, in first-seen order.
_handed: Dict[str, List[str]] = {}

#: Every member name the index holds.  Loaded once; None until then.
_known: Optional[frozenset] = None


def _known_names() -> frozenset:
    """Every name the index holds, as one set.

    Loaded lazily and once. A miss here must never break a tool call: if the
    index cannot be read the ledger simply records nothing, because a broken
    reconciliation is worth less than a working `what_uses`.
    """
    global _known
    if _known is not None:
        return _known
    names: Set[str] = set()
    try:
        from kb.server import tools as _tools
        # Data files are names too, and in a source-only extraction the CL
        # that builds and opens them is where they are recorded.
        for sql in ("SELECT name FROM object",
                    "SELECT name FROM data_member",
                    "SELECT DISTINCT target AS name FROM file_provenance",
                    "SELECT DISTINCT target_file AS name FROM override"):
            try:
                names.update(r["name"] for r in _tools._rows(sql) if r["name"])
            except Exception:                             # noqa: BLE001
                continue
    except Exception:                                     # noqa: BLE001
        pass
    _known = frozenset(names)
    return _known


def members_in(text: str) -> Set[str]:
    """The member names in a piece of text, validated against the index."""
    if not text:
        return set()
    known = _known_names()
    if not known:
        return set()
    return {tok for tok in set(_TOKEN.findall(text.upper())) if tok in known}


def note(tool: str, text: str) -> int:
    """Record the members a tool call handed over. Returns how many are new.

    Never raises: this runs on the response path of every tool call, and a
    fault in bookkeeping must not turn a good answer into a failed one.
    """
    if tool not in SWEEP_TOOLS:
        return 0
    try:
        found = members_in(text)
    except Exception:                                     # noqa: BLE001
        return 0
    new = 0
    with _lock:
        for name in found:
            seen = _handed.setdefault(name, [])
            if not seen:
                new += 1
            if tool not in seen:
                seen.append(tool)
    return new


#: Whether the session has already been told the ledger exists.
_announced = False


def announce_once() -> str:
    """The one-time notice that a reconciliation ledger is being kept.

    Once, not per call. The contract has to reach the agent in-band - a rule
    that lives only in a skill file is read at the start of a long session and
    competes with everything that arrives after it, which is exactly how six
    copybooks were quoted and then lost. Attaching it to the first result ties
    it to the data instead.
    Repeating it on all eighteen sweep tools would be a tax on every call for
    a contract that only needs stating once.
    """
    global _announced
    with _lock:
        if _announced or not _handed:
            return ""
        _announced = True
    return chr(10).join([
        "  [ledger] Member names returned this session are being tracked.",
        "  Before you deliver a report, call unaccounted(<report text>): it "
        "names anything",
        "  handed to you that the report never placed. Each needs one of - "
        "change set,",
        "  recompile set, or stated exclusion with the evidence."])


def handed_out() -> Dict[str, List[str]]:
    with _lock:
        return {k: list(v) for k, v in _handed.items()}


def reset() -> None:
    global _announced
    with _lock:
        _handed.clear()
        _announced = False


#: Words that make a line an exclusion rather than a placement. Generic
#: report language only.
_EXCLUSION_RE = re.compile(
    r"\b(?:exclud\w*|not\s+affected|unaffected|out\s+of\s+scope|not\s+in\s+scope|"
    r"ignored?|skipped|not\s+relevant|irrelevant|no\s+impact|n/a)\b", re.I)

#: What counts as evidence on an exclusion line: a `member:line` citation, or
#: a knowledge-base tool call whose result justifies it.
_CITATION_RE = re.compile(r"\b[A-Z][A-Z0-9_$#@]{0,9}:\d+\b")
_TOOL_CALL_RE = re.compile(r"\b([a-z_]+)\s*\(")


def _has_evidence(line: str) -> bool:
    if _CITATION_RE.search(line.upper()):
        return True
    from kb.server import tools as _tools
    return any(hasattr(_tools, m.group(1)) and not m.group(1).startswith("_")
               for m in _TOOL_CALL_RE.finditer(line))


def unsupported_exclusions(text: str, names: Set[str]) -> Dict[str, str]:
    """name -> the exclusion line, for names placed ONLY by an exclusion that
    cites nothing.

    "Excluded - other platform" places a member as firmly as a recompile row,
    and is the easiest line in a report to write without checking. An
    exclusion is a finding like any other and carries its evidence: the
    `member:line` that shows it, or the tool call whose answer does.
    """
    lines_of: Dict[str, List[str]] = {}
    for line in text.splitlines():
        for name in set(_TOKEN.findall(line.upper())) & names:
            lines_of.setdefault(name, []).append(line)
    out: Dict[str, str] = {}
    for name, lines in lines_of.items():
        exclusions = [ln for ln in lines if _EXCLUSION_RE.search(ln)]
        if not exclusions or len(exclusions) < len(lines):
            continue                      # placed somewhere other than an exclusion
        if not any(_has_evidence(ln) for ln in exclusions):
            out[name] = exclusions[0].strip()
    return out


def reconcile(text: str) -> Tuple[List[Tuple[str, List[str]]], int, int]:
    """(unaccounted, handed_out_total, accounted_total).

    `text` is whatever the caller is about to deliver - the member lists, or
    the whole report. Anything the tools returned that does not appear in it
    has not been placed, and neither has anything whose only mention is an
    exclusion with no evidence.
    """
    ledger = handed_out()
    present = members_in(text)
    present -= set(unsupported_exclusions(text, present & set(ledger)))
    missing = sorted((n, t) for n, t in ledger.items() if n not in present)
    return missing, len(ledger), len(ledger) - len(missing)


def report(text: str, limit: int = 400) -> str:
    """The reconciliation, as the agent reads it."""
    if not _known_names():
        return ("The index holds no member names, so nothing can be "
                "reconciled against it.")

    missing, total, placed = reconcile(text)
    ledger = handed_out()
    unsupported = unsupported_exclusions(
        text, members_in(text) & set(ledger))
    if not total:
        return ("Nothing has been handed out yet in this session, so there is "
                "nothing to reconcile.\n"
                "  Run the sweeps first, then call this before writing the "
                "report.")

    head = [f"RECONCILIATION - {total:,} member(s) returned by tools this "
            f"session.",
            f"  placed in the text you supplied : {placed:,}",
            f"  NOT placed                      : {len(missing):,}",
            ""]
    if not missing:
        head.append("Every member the knowledge base handed you appears in "
                    "the report. Nothing leaked.")
        return "\n".join(head)

    head.append("These were returned by a tool and appear nowhere in the "
                "text you supplied, or only in an exclusion that cites nothing.")
    head.append("Each needs ONE of three states before the report goes out:")
    head.append("  1. in the change set     2. in the recompile set")
    head.append("  3. explicitly excluded, with the tool result that "
                "justifies it")
    head.append("")
    head.append("Quoting one as a risk note is NOT a fourth state - that is "
                "how members leak.")
    head.append("")
    if unsupported:
        head.append(f"  EXCLUDED WITHOUT EVIDENCE ({len(unsupported)}) - counted "
                    f"as not placed. Add the member:line or the tool call that "
                    f"justifies each:")
        for name in sorted(unsupported)[:limit]:
            head.append(f"    {name:<10} {unsupported[name][:100]}")
        head.append("")

    by_tool: Dict[str, List[str]] = {}
    for name, from_tools in missing:
        by_tool.setdefault(", ".join(from_tools), []).append(name)

    shown = 0
    for source in sorted(by_tool, key=lambda k: -len(by_tool[k])):
        names = sorted(by_tool[source])
        head.append(f"  from {source}  ({len(names)}):")
        for i in range(0, len(names), 8):
            if shown >= limit:
                head.append(f"    ... and {len(missing) - shown:,} more")
                return "\n".join(head)
            head.append("    " + "  ".join(names[i:i + 8]))
            shown += len(names[i:i + 8])
    return "\n".join(head)
