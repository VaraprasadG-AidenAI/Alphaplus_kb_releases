"""
A ticket, recorded into the knowledge store the moment it is read.

The reader turns a PDF, a Jira export or a Word file into structured XML. That
XML is a file, and a file is only knowledge if somebody opens it. Until now
the ticket's facts reached the database only when the agent chose to call
`remember` - so a ticket could be read, structured, written to disk, and leave
the store exactly as empty as before. The install check said as much on the
first real run: *"ticket memory - empty until the first ticket records a
fact"*, after a ticket had been read.

So the reader records what it read, itself, at the moment it reads it:

    the ticket and its title          one `decision` per ticket
    every acceptance criterion        one `decision` each, in the ticket's
                                      own numbering and wording
    every member-shaped identifier    one `question` each - "the ticket names
                                      this; is it in scope?" - which is what
                                      an identifier in a ticket actually is
                                      until the analysis confirms it
    anything that could not be read   one `finding`, so an unreadable page is
                                      a recorded gap rather than a silence

Nothing here decides anything about the code. It records what the ticket
says, with the ticket as its evidence, so `recall` can answer "what do we
already know about this ticket" in a later session - and so a second reading
of the same ticket adds nothing, because `remember` is idempotent.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, List, Tuple

if TYPE_CHECKING:                                   # pragma: no cover
    from . import Parsed

#: Enough of a statement to be useful in a later session, short enough that a
#: `recall` answer stays readable. The XML holds the full text either way.
_MAX = 600


def _short(text: str, limit: int = _MAX) -> str:
    clean = " ".join((text or "").split())
    return clean if len(clean) <= limit else clean[:limit - 1] + "…"


def facts_from(parsed: "Parsed", source: Path) -> List[Tuple[str, str, str, str]]:
    """(kind, subject, statement, evidence) for everything worth keeping.

    Built separately from the writing so it can be checked without a database
    and so the caller can see what would be recorded before anything is.
    """
    ticket = parsed.ticket
    key = ticket.key or "(no ticket key)"
    where = source.name
    out: List[Tuple[str, str, str, str]] = []

    title = _short(ticket.title)
    if title:
        out.append(("decision", key,
                    f"Ticket {key}: {title}",
                    f"{where}, read as {ticket.source_format}"))

    for number, criterion in enumerate(ticket.criteria, 1):
        text = _short(getattr(criterion, "text", str(criterion)))
        if not text:
            continue
        label = getattr(criterion, "id", "") or f"AC-{number:03d}"
        out.append(("decision", f"{key} {label}",
                    f"Acceptance criterion {label}: {text}",
                    f"{where}, criterion {number} of {len(ticket.criteria)}"))

    # An identifier in a ticket is a claim, not a fact: the ticket says this
    # name matters. Recording it as a question is what it is until the
    # analysis has confirmed the member exists and is in scope.
    for identifier in ticket.identifiers[:40]:
        name = (identifier or "").strip().upper()
        if not name:
            continue
        out.append(("question", name,
                    f"{key} names {name}. Confirm it exists, and say whether "
                    f"it is in scope or why it is not.",
                    where))

    if ticket.unreadable_pages:
        pages = ", ".join(str(p) for p in ticket.unreadable_pages)
        out.append(("finding", key,
                    f"Pages {pages} of {key} could not be read as text. "
                    f"Read them by eye before relying on the criteria list.",
                    where))

    for note in ticket.notes:
        text = _short(note)
        if text:
            out.append(("finding", key, f"Reading {key}: {text}", where))

    return out


def record(parsed: "Parsed", source: Path) -> Tuple[int, str]:
    """Record the ticket's facts. Returns (how many, a line for the user).

    Never raises. A knowledge store that cannot be written is worth saying so
    about, and worth nothing at all if it stops a ticket being read.
    """
    facts = facts_from(parsed, Path(source))
    if not facts:
        return 0, "nothing in the ticket to record"

    try:
        from kb.server import memory
    except Exception as exc:                                  # noqa: BLE001
        return 0, f"ticket memory unavailable: {exc}"

    key = parsed.ticket.key or Path(source).stem
    written = 0
    failed = 0
    for kind, subject, statement, evidence in facts:
        try:
            answer = memory.remember(ticket=key, kind=kind, subject=subject,
                                     statement=statement, evidence=evidence,
                                     confidence="exact")
        except Exception:                                     # noqa: BLE001
            failed += 1
            continue
        if str(answer).startswith("Not recorded"):
            failed += 1
        else:
            written += 1

    line = f"recorded {written} fact(s) about {key}"
    if failed:
        line += f"; {failed} could not be recorded"
    return written, line
