"""
Ticket reader: a Jira export in, structured XML out.

    from kb import ticket
    result = ticket.parse(Path("ALPHAPLUS-1234.pdf"))
    print(result.xml)

Why it exists: on the VDI a ticket arrives as a PDF, and the analysis has to
read all of it - every comment and, above all, the acceptance criteria, which
say what has to be true when the work is done. Reading a PDF by eye is
possible but nothing checks that it happened, and a criterion nobody read is a
member nobody changes. So the file is turned into text the same way source is:
mechanically, with a record of what could not be read.

Everything here is standard library only, for the same reason the rest of the
product is: one executable has to run inside a locked-down VDI with no
installs and no network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import documents, jira, render

__all__ = ["Parsed", "parse", "write", "WORK_DIR", "SUPPORTED"]

#: Where a parsed ticket is kept, beside the analysis notes, under the
#: project root. The skill reads both from there.
WORK_DIR = ".alphaplus-work"

#: The file kinds worth trying. Anything else is read as text and reported.
SUPPORTED = (".pdf", ".xml", ".docx", ".doc", ".html", ".htm", ".txt", ".md")


@dataclass
class Parsed:
    ticket: jira.Ticket
    xml: str
    path: Optional[Path] = None          # where the XML was written

    @property
    def key(self) -> str:
        return self.ticket.key

    def summary(self) -> str:
        """One screen for a human: what was read, and what was not."""
        t = self.ticket
        lines = [f"{t.key or '(no ticket key found)'}  {t.title}".rstrip(),
                 f"  read as   {t.source_format}"
                 + (f", {len(t.pages)} page(s)" if len(t.pages) > 1 else "")]
        lines.append(f"  criteria  {len(t.criteria)}"
                     + ("" if t.criteria else "  - none found, say so in the report"))
        lines.append(f"  comments  {len(t.comments)}"
                     + ("" if t.comments_parsed else "  - could not be split, kept whole"))
        if t.fields:
            shown = ", ".join(f"{k}={v}" for k, v in list(t.fields.items())[:4])
            lines.append(f"  fields    {shown}")
        if t.identifiers:
            lines.append(f"  names     {' '.join(t.identifiers[:12])}")
        if t.unreadable_pages:
            lines.append(f"  UNREADABLE PAGES: {t.unreadable_pages} - read them by eye")
        for note in t.notes:
            lines.append(f"  note      {note}")
        return "\n".join(lines)


def parse(path: Path) -> Parsed:
    """Read a ticket file and structure it. Never raises for a real file."""
    read = documents.read_file(Path(path))
    ticket = jira.from_read(read)
    return Parsed(ticket=ticket, xml=render.to_xml(ticket))


def output_path(parsed: Parsed, root: Path, source: Path) -> Path:
    """`<root>/.alphaplus-work/<KEY>.ticket.xml`, or the file's own name."""
    stem = parsed.key or _slug(source.stem) or "ticket"
    return Path(root) / WORK_DIR / f"{stem}.ticket.xml"


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text or "").strip("-")[:60]


def write(parsed: Parsed, root: Path, source: Path) -> Path:
    """Write the XML beside the ticket's working notes. Returns the path."""
    target = output_path(parsed, root, source)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(parsed.xml, encoding="utf-8")
    parsed.path = target
    return target
