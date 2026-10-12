"""
Whatever a ticket arrives as, turned into text plus a note on how well it read.

Jira exports reach an analyst in five shapes, and the shape is never stated:

    PDF          a browser print, or Jira's own exporter
    XML          Jira's issue view; every field already labelled
    DOCX         Jira "Export -> Word" in newer instances
    HTML / DOC   Jira "Export -> Word" in older ones: HTML with a .doc name
    TXT          a paste into a file

So the format is decided by content, never by the extension - the same rule
kb.parse applies to source members, and for the same reason: the name is a
claim, the bytes are the fact.

Nothing here interprets the ticket. It produces text and a `Read` record that
says how much of the file was legible, which is what lets the report say
"page 3 is a scan" instead of quietly analysing four pages of five.
"""

from __future__ import annotations

import html.parser
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from . import pdftext

__all__ = ["Read", "read_file", "html_to_text"]


@dataclass
class Read:
    """The text of a ticket file, and how well it could be read."""
    path: str
    fmt: str = "unknown"                 # pdf | jira-xml | docx | html | text
    text: str = ""
    pages: List[str] = field(default_factory=list)
    #: Pages that hold no readable text at all - a scan, or a drawing.
    unreadable_pages: List[int] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    ok: bool = True

    @property
    def page_count(self) -> int:
        return len(self.pages)


# ===========================================================================
# Format detection
# ===========================================================================

def sniff(data: bytes, path: Path) -> str:
    head = data[:4096].lstrip()
    if head.startswith(b"%PDF"):
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        return "docx"
    lowered = head[:2048].lower()
    if lowered.startswith(b"<?xml") or lowered.startswith(b"<rss"):
        return "jira-xml" if b"<rss" in lowered or b"<item" in lowered else "xml"
    if b"<html" in lowered or b"<!doctype html" in lowered or b"<body" in lowered:
        return "html"
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md", ".log"):
        return "text"
    if suffix in (".htm", ".html", ".doc"):
        return "html"
    if suffix == ".xml":
        return "jira-xml"
    return "text"


# ===========================================================================
# HTML (also Jira's "Word" export, and rich text inside a Jira XML export)
# ===========================================================================

class _Text(html.parser.HTMLParser):
    """HTML to plain text, keeping the block structure a reader needs."""

    _BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5",
              "h6", "table", "blockquote", "section", "header", "footer"}
    #: Elements whose text is not content. `meta` and `link` are NOT here:
    #: they are void, so they never close, and skipping on one turned the
    #: whole document into silence - an HTML ticket read as three bullets.
    _SKIP = {"script", "style", "head", "title", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP:
            self._skipping += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in ("td", "th"):
            self.parts.append("\t")
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _Text()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:                               # noqa: BLE001 - malformed
        pass
    text = "".join(parser.parts)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


# ===========================================================================
# DOCX
# ===========================================================================

def _docx_text(data: bytes, read: Read) -> str:
    import io
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if "word/document.xml" not in archive.namelist():
                read.notes.append("a ZIP file, but not a Word document")
                read.ok = False
                return ""
            body = archive.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        read.notes.append(f"cannot open the Word file: {exc}")
        read.ok = False
        return ""
    # Paragraph and row ends become newlines; tabs stay tabs.
    body = re.sub(r"</w:p>", "\n", body)
    body = re.sub(r"<w:tab[^>]*/>", "\t", body)
    body = re.sub(r"<w:br[^>]*/>", "\n", body)
    text = re.sub(r"<[^>]+>", "", body)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&apos;", "'"))
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ===========================================================================
# Entry point
# ===========================================================================

def read_file(path: Path) -> Read:
    """Read a ticket file of any supported shape. Never raises."""
    read = Read(path=str(path))
    try:
        data = path.read_bytes()
    except OSError as exc:
        read.ok = False
        read.notes.append(f"cannot read the file: {exc}")
        return read

    if not data.strip():
        read.ok = False
        read.notes.append("the file is empty (0 bytes) - ask for the export again")
        return read

    read.fmt = sniff(data, path)

    if read.fmt == "pdf":
        try:
            document = pdftext.extract(data)
        except pdftext.PdfError as exc:
            read.ok = False
            read.notes.append(str(exc))
            return read
        read.notes.extend(document.notes)
        read.pages = [page.text for page in document.pages]
        read.text = "\n".join(read.pages)
        for page in document.pages:
            if len(page.text.strip()) >= 20:
                continue
            read.unreadable_pages.append(page.number)
            read.notes.append(
                f"page {page.number} holds no readable text - "
                + ("it is an image; read it by eye" if page.text_operators == 0
                   else "its text could not be decoded"))
        if document.encrypted:
            read.ok = False
        elif not read.text.strip():
            read.ok = False
            read.notes.append(
                "no text could be extracted from any page - treat this export "
                "as a set of images and read every page by eye")
        return read

    if read.fmt == "docx":
        read.text = _docx_text(data, read)
        read.pages = [read.text]
        return read

    text = data.decode("utf-8", "replace")
    if text.count("�") > len(text) / 50:       # not UTF-8 after all
        text = data.decode("cp1252", "replace")

    if read.fmt in ("jira-xml", "xml"):
        read.text = text                            # parsed as XML downstream
        read.pages = [text]
        return read

    if read.fmt == "html":
        read.text = html_to_text(text)
        read.pages = [read.text]
        return read

    read.text = text
    read.pages = [text]
    return read
