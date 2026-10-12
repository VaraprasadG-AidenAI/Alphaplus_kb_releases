"""
A structured ticket, written as XML for the analysis to read.

The shape is chosen for the reader, not for a schema: the criteria and the
comments come first because they are what an analysis is most likely to skip,
and the raw page text is kept at the end so nothing that was read is lost if
the structure was guessed wrong. A parser that quietly drops half a ticket is
worse than no parser; every section it could not place lands in `<unplaced>`.

Written by hand rather than through a serialiser so the output stays readable
in a terminal and in a diff, and so a control character in a Jira comment
cannot produce a file that will not parse.
"""

from __future__ import annotations

import re
from typing import List

from .jira import Ticket

__all__ = ["to_xml"]

_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _escape(text: str) -> str:
    text = _ILLEGAL.sub(" ", text or "")
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;"))


def _attr(name: str, value: str) -> str:
    return f' {name}="{_escape(str(value))}"' if value not in (None, "") else ""


def _block(tag: str, body: str, indent: str = "  ") -> List[str]:
    if not (body or "").strip():
        return []
    lines = [f"{indent}<{tag}>"]
    for line in _escape(body.strip()).split("\n"):
        lines.append(f"{indent}  {line}".rstrip())
    lines.append(f"{indent}</{tag}>")
    return lines


def to_xml(ticket: Ticket) -> str:
    """The whole ticket as one XML document."""
    out: List[str] = ['<?xml version="1.0" encoding="utf-8"?>']
    pages = len(ticket.pages)
    readable = sum(1 for p in ticket.pages if len(p.strip()) >= 20)
    out.append(
        "<ticket" + _attr("key", ticket.key) + _attr("source", ticket.source_format)
        + _attr("pages", str(pages) if pages else "")
        + _attr("pages-with-text", str(readable) if pages else "")
        + ">")

    out += _block("title", ticket.title)

    if ticket.fields:
        out.append("  <fields>")
        for name in sorted(ticket.fields):
            value = ticket.fields[name]
            if value:
                out.append(f'    <field name="{_escape(name)}">'
                           f"{_escape(value)}</field>")
        out.append("  </fields>")

    # Criteria first: this is the part an analysis most often never reads.
    out.append(f'  <acceptance-criteria count="{len(ticket.criteria)}">')
    if not ticket.criteria:
        out.append("    <!-- none found in this ticket; the report must say so -->")
    for item in ticket.criteria:
        out.append(f'    <criterion id="{_escape(item.id)}"'
                   f'{_attr("from", item.source)}>{_escape(item.text)}</criterion>')
    out.append("  </acceptance-criteria>")

    out += _block("description", ticket.description)

    out.append(f'  <comments count="{len(ticket.comments)}"'
               + ('' if ticket.comments_parsed else ' split="failed"') + ">")
    for comment in ticket.comments:
        head = ("    <comment" + _attr("n", str(comment.index))
                + _attr("author", comment.author) + _attr("date", comment.created)
                + ">")
        out.append(head)
        for line in _escape(comment.body.strip()).split("\n"):
            out.append("      " + line.rstrip())
        out.append("    </comment>")
    out.append("  </comments>")

    if ticket.attachments:
        out.append("  <attachments>")
        for name in ticket.attachments:
            out.append(f"    <attachment>{_escape(name)}</attachment>")
        out.append("  </attachments>")

    if ticket.links:
        out.append("  <links>")
        for key in dict.fromkeys(ticket.links):
            out.append(f"    <link>{_escape(key)}</link>")
        out.append("  </links>")

    if ticket.identifiers:
        out.append("  <identifiers>")
        out.append("    <!-- words shaped like member, file or field names; "
                   "check each with find_object or search_source -->")
        out.append("    " + _escape(" ".join(ticket.identifiers)))
        out.append("  </identifiers>")

    out += _block("unplaced", ticket.unplaced)

    # Quality last, so it is the final thing read before the analysis starts.
    out.append("  <quality>")
    out.append(f'    <pages total="{pages}" with-text="{readable}"'
               + _attr("unreadable", ",".join(str(n) for n in ticket.unreadable_pages))
               + "/>")
    for note in ticket.notes:
        out.append(f"    <note>{_escape(note)}</note>")
    if ticket.unreadable_pages:
        out.append("    <note>read every unreadable page by eye before "
                   "starting the analysis, and say in the report that you "
                   "did</note>")
    out.append("  </quality>")

    # The text as read, so nothing depends on the structure above being right.
    if ticket.pages:
        out.append(f'  <pages count="{pages}">')
        for number, page in enumerate(ticket.pages, 1):
            out.append(f'    <page n="{number}">')
            for line in _escape(page.strip()).split("\n"):
                out.append("      " + line.rstrip())
            out.append("    </page>")
        out.append("  </pages>")

    out.append("</ticket>")
    return "\n".join(out) + "\n"
