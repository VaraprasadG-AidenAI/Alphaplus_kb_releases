"""
A Jira ticket, structured.

Two inputs, one result:

    Jira XML export     every field already labelled - nothing is guessed
    anything else       a printed or exported page, read by its labels

`Ticket` is deliberately close to what a Jira issue actually holds, because
the analysis reads it: the description, EVERY comment in order, the acceptance
criteria, and the identifiers worth a tool call.

## Why the acceptance criteria get their own treatment

A ticket's description says what went wrong; its acceptance criteria say what
has to be true when the work is done, and a delivered change usually satisfies
all of them. An analysis driven by the symptom alone answers part of the
ticket and reads as though it answered everything - which is exactly how a
member changed for criterion seven was missed on a real analysis. So the
criteria are lifted out under whatever numbering the shop uses, and the
skill's report lists each one with the code that satisfies it or `no code
found`.

## Rule 1 still applies

Nothing here is evidence about the CODE. A ticket says what people want; the
source says what the programs do. The two are reconciled by the skill, with
the ticket as the requirement and the index as the fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .documents import Read, html_to_text

__all__ = ["Ticket", "Comment", "Criterion", "from_read"]


@dataclass
class Comment:
    author: str = ""
    created: str = ""
    body: str = ""
    index: int = 0


@dataclass
class Criterion:
    id: str                                   # AC-001, OQ-7, 3, ...
    text: str
    source: str = "description"               # where it was found


@dataclass
class Ticket:
    key: str = ""
    title: str = ""
    fields: Dict[str, str] = field(default_factory=dict)
    description: str = ""
    criteria: List[Criterion] = field(default_factory=list)
    comments: List[Comment] = field(default_factory=list)
    attachments: List[str] = field(default_factory=list)
    links: List[str] = field(default_factory=list)
    identifiers: List[str] = field(default_factory=list)
    #: Text that was read but not placed in any section. Never dropped: a
    #: parser that silently loses half a ticket is worse than no parser.
    unplaced: str = ""
    source_format: str = ""
    notes: List[str] = field(default_factory=list)
    pages: List[str] = field(default_factory=list)
    unreadable_pages: List[int] = field(default_factory=list)
    comments_parsed: bool = True


# ===========================================================================
# Labels, in the languages these instances run in
# ===========================================================================

#: field label -> canonical name. Matched on a line of its own or as
#: `Label: value`, case-insensitively.
_FIELDS: Dict[str, str] = {
    "type": "type", "typ": "type", "issue type": "type", "vorgangstyp": "type",
    "status": "status",
    "priority": "priority", "priorität": "priority", "prioritat": "priority",
    "resolution": "resolution", "lösung": "resolution", "losung": "resolution",
    "reporter": "reporter", "melder": "reporter", "berichterstatter": "reporter",
    "assignee": "assignee", "bearbeiter": "assignee", "zugewiesen an": "assignee",
    "created": "created", "erstellt": "created",
    "updated": "updated", "aktualisiert": "updated",
    "due date": "due", "fälligkeitsdatum": "due",
    "component/s": "components", "components": "components",
    "komponente/n": "components", "komponenten": "components",
    "labels": "labels", "stichwörter": "labels", "stichworter": "labels",
    "fix version/s": "fix_versions", "fix versions": "fix_versions",
    "affects version/s": "affects_versions",
    "sprint": "sprint", "epic link": "epic", "story points": "points",
    "project": "project", "projekt": "project",
}

_SECTIONS: Dict[str, str] = {
    "description": "description", "beschreibung": "description",
    "summary": "summary", "zusammenfassung": "summary",
    "environment": "environment", "umgebung": "environment",
    "acceptance criteria": "criteria", "akzeptanzkriterien": "criteria",
    "abnahmekriterien": "criteria", "definition of done": "criteria",
    "attachments": "attachments", "anhänge": "attachments",
    "anhange": "attachments", "dateianhänge": "attachments",
    "issue links": "links", "verknüpfte vorgänge": "links",
    "verknupfte vorgange": "links", "links": "links",
    "comments": "comments", "kommentare": "comments",
    "activity": "comments", "aktivität": "comments", "aktivitat": "comments",
    "sub-tasks": "subtasks", "unteraufgaben": "subtasks",
    "details": "details", "people": "people", "personen": "people",
    "dates": "dates", "daten": "dates", "attachments:": "attachments",
}

_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]{1,19}-\d{1,7})\b")
_FIELD_LINE_RE = re.compile(r"^\s*([A-Za-zÄÖÜäöüß/ .\-]{2,26})\s*:\s*(.*)$")

#: `AC-001`, `AC 1`, `AC1`, `OQ-7`, `Akzeptanzkriterium 3`, `Kriterium 2`.
_CRITERION_RE = re.compile(
    r"^\s*(?:[-*••]\s*)?(?:\[[ xX]\]\s*)?(?:\d{1,3}[.)]\s*)?"
    r"((?:AC|OQ|CR|AK)\s?[-–]?\s?\d{1,3}"
    r"|(?:Akzeptanzkriterium|Abnahmekriterium|Kriterium|Criterion)\s+\d{1,3})"
    r"\s*[:.)\]-]?\s*(.*)$", re.I)
#: A numbered item under an acceptance-criteria heading: `1. ...` / `1) ...`
_NUMBERED_RE = re.compile(r"^\s*(?:[-*••]\s*)?(\d{1,3})[.)]\s+(\S.*)$")

#: How a printed Jira page announces a comment.
_COMMENT_RES = (
    re.compile(r"^(?P<author>.{2,60}?)\s+added a comment\s*[-–]\s*(?P<when>.+?)\s*$", re.I),
    re.compile(r"^(?P<author>.{2,60}?)\s+hat einen Kommentar hinzugef[üu]gt\s*[-–]\s*(?P<when>.+?)\s*$", re.I),
    re.compile(r"^Comment by\s+(?P<author>.{2,60}?)\s*\[\s*(?P<when>.+?)\s*\]\s*$", re.I),
    re.compile(r"^Kommentar von\s+(?P<author>.{2,60}?)\s*\[\s*(?P<when>.+?)\s*\]\s*$", re.I),
)

#: Identifier-shaped words worth a tool call: a member name carries a digit,
#: a field or variable name carries a hyphen or an ampersand.
_IDENT_RES = (
    re.compile(r"\b(?=[A-Z0-9_]*\d)[A-Z][A-Z0-9_]{2,9}\b"),
    re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+){1,4}\b"),
    re.compile(r"&[A-Z][A-Z0-9_]{1,9}\b"),
)
#: Words shaped like identifiers that never are.
_IDENT_STOP = {
    "XML", "PDF", "SQL", "CSV", "JSON", "HTML", "HTTP", "HTTPS", "URL", "API",
    "OK", "ID", "AC", "OQ", "TODO", "FIXME", "UTF", "ISO", "EUR", "USD",
    "ZIP", "DOC", "DOCX", "XLSX", "JIRA", "E-MAIL", "EMAIL",
}


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


#: Every `Label:` on a line. A printed Jira table puts a whole row on one
#: line - `Typ: Fehler  Status: In Arbeit` - so reading only the first label
#: loses half the fields, and status is exactly the half that goes missing.
_ANY_LABEL_RE = re.compile(
    r"(?<![A-Za-zÄÖÜäöüß])("
    + "|".join(sorted((re.escape(k) for k in _FIELDS), key=len, reverse=True))
    + r")\s*:\s*", re.I)


def _read_fields(line: str, ticket: "Ticket") -> None:
    """Take every labelled field a printed line carries."""
    matches = list(_ANY_LABEL_RE.finditer(line))
    for index, match in enumerate(matches):
        canonical = _FIELDS.get(re.sub(r"\s+", " ", match.group(1).strip().lower()))
        if not canonical:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
        value = _clean(line[match.end():end])
        if value and canonical not in ticket.fields:
            ticket.fields[canonical] = value


# ===========================================================================
# Jira XML export - the reliable path
# ===========================================================================

def _xml_text(node) -> str:
    """An element's text, with Jira's embedded HTML flattened."""
    parts: List[str] = []
    if node is None:
        return ""
    if node.text:
        parts.append(node.text)
    for child in list(node):
        parts.append(_xml_text(child))
        if child.tail:
            parts.append(child.tail)
    return html_to_text("".join(parts))


def from_jira_xml(markup: str, ticket: Ticket) -> bool:
    """Fill `ticket` from a Jira XML export. False if it is not one."""
    # Entity declarations are refused rather than resolved: an export is data
    # from outside, and an expanding entity is the classic XML attack.
    if re.search(r"<!(?:DOCTYPE|ENTITY)\b", markup[:4096], re.I):
        ticket.notes.append(
            "the XML declares a DOCTYPE or ENTITY and was not parsed; "
            "export it again from Jira")
        return False
    try:
        from xml.etree import ElementTree
        root = ElementTree.fromstring(markup)
    except Exception as exc:                        # noqa: BLE001
        ticket.notes.append(f"the XML could not be parsed: {exc}")
        return False

    item = root.find(".//item")
    if item is None:
        return False

    ticket.source_format = "jira-xml"
    ticket.key = _clean(item.findtext("key") or "")
    ticket.title = _clean(item.findtext("summary") or item.findtext("title") or "")
    if ticket.title.startswith("[") and ticket.key and ticket.key in ticket.title:
        ticket.title = _clean(ticket.title.split("]", 1)[-1])

    for tag, name in (("type", "type"), ("status", "status"),
                      ("priority", "priority"), ("resolution", "resolution"),
                      ("assignee", "assignee"), ("reporter", "reporter"),
                      ("created", "created"), ("updated", "updated"),
                      ("resolved", "resolved"), ("due", "due"),
                      ("project", "project"), ("version", "affects_versions"),
                      ("fixVersion", "fix_versions"), ("component", "components")):
        values = [_clean(e.text or "") for e in item.findall(tag)]
        values = [v for v in values if v]
        if values:
            ticket.fields[name] = ", ".join(values)
    labels = [_clean(e.text or "") for e in item.findall(".//label")]
    if any(labels):
        ticket.fields["labels"] = ", ".join(v for v in labels if v)

    ticket.description = _xml_text(item.find("description"))

    for index, element in enumerate(item.findall(".//comment"), 1):
        ticket.comments.append(Comment(
            author=_clean(element.get("author") or ""),
            created=_clean(element.get("created") or ""),
            body=_xml_text(element), index=index))

    for element in item.findall(".//attachment"):
        name = _clean(element.get("name") or "")
        if name:
            ticket.attachments.append(name)

    for element in item.findall(".//issuelink//issuekey"):
        key = _clean(element.text or "")
        if key:
            ticket.links.append(key)

    # Custom fields, including whichever one holds the acceptance criteria.
    for custom in item.findall(".//customfield"):
        name = _clean(custom.findtext("customfieldname") or "")
        # Jira nests the value inside `<customfieldvalues><customfieldvalue>`,
        # but some exporters put the text straight in the plural element.
        values = [_xml_text(v) for v in custom.findall(".//customfieldvalue")]
        if not any(values):
            values = [_xml_text(v) for v in custom.findall("customfieldvalues")]
        body = "\n".join(v for v in values if v)
        if not name or not body:
            continue
        ticket.fields.setdefault(name, _clean(body)[:400])
        if _SECTIONS.get(name.strip().lower()) == "criteria":
            ticket.criteria.extend(_criteria_from(body, source=name))

    # The description, and then the comments.
    #
    # Only custom fields were read for criteria, and a Jira instance that
    # holds its acceptance criteria in a custom field is the exception. The
    # usual place is the description - a heading and a numbered list, typed
    # by whoever raised the ticket - and reading an export of such a ticket
    # produced no criteria at all, with a note saying the ticket had none.
    #
    # A report's criteria ledger is built from this. Empty, it says the
    # ticket asked for nothing, which is the most confident wrong answer the
    # reader can give.
    #
    # Later sources do not displace earlier ones: a criterion stated in a
    # custom field is the same criterion if it is repeated in the
    # description, and it is recorded once, under the first source that
    # carried it.
    seen = {_criterion_key(c) for c in ticket.criteria}
    for text, where in ((ticket.description, "description"),
                        ("\n\n".join(c.body for c in ticket.comments),
                         "comments")):
        if not (text or "").strip():
            continue
        for found in _criteria_from(text, source=where):
            marker = _criterion_key(found)
            if marker in seen:
                continue
            seen.add(marker)
            ticket.criteria.append(found)

    # The names a ticket puts in its own text are the first thing an analysis
    # looks for, and the XML path never collected them.
    if not ticket.identifiers:
        body = "\n".join(filter(None, [
            ticket.summary if hasattr(ticket, "summary") else "",
            ticket.description,
            "\n".join(c.body for c in ticket.comments)]))
        ticket.identifiers = _identifiers(ticket, body)
    return True


# ===========================================================================
# Printed or exported page - read by its labels
# ===========================================================================

def _strip_page_furniture(pages: List[str]) -> List[str]:
    """Drop the header and footer a browser stamps on every printed page."""
    if len(pages) < 3:
        return [p for p in pages]
    counts: Dict[str, int] = {}
    for page in pages:
        for line in {l.strip() for l in page.split("\n") if l.strip()}:
            counts[line] = counts.get(line, 0) + 1
    repeated = {line for line, n in counts.items()
                if n >= max(3, len(pages) - 1) and len(line) < 120}
    out = []
    for page in pages:
        out.append("\n".join(l for l in page.split("\n")
                             if l.strip() not in repeated))
    return out


def _criterion_key(criterion: Criterion) -> str:
    """What makes two criteria the same one, whichever section carried it."""
    return (criterion.id.strip().upper() + "|"
            + " ".join(criterion.text.split()).lower()[:120])


def _criteria_from(text: str, source: str) -> List[Criterion]:
    """Every acceptance criterion in a block of text, in the ticket's own ids."""
    found: List[Criterion] = []
    lines = text.split("\n")
    heading = False
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        lowered = re.sub(r"[:\s]+$", "", line.strip().lower())
        if _SECTIONS.get(lowered) == "criteria":
            heading = True
            continue
        if lowered in _SECTIONS and _SECTIONS[lowered] != "criteria":
            heading = False

        match = _CRITERION_RE.match(line)
        if match:
            ident = re.sub(r"\s+", "", match.group(1).upper()).replace("–", "-")
            if not re.match(r"^[A-Z]+-", ident):
                ident = re.sub(r"^([A-Z]+)", r"\1-", ident)
            body = _clean(match.group(2))
            if body or ident:
                found.append(Criterion(id=ident, text=body, source=source))
            continue
        if heading:
            numbered = _NUMBERED_RE.match(line)
            if numbered:
                found.append(Criterion(id=numbered.group(1),
                                       text=_clean(numbered.group(2)),
                                       source=source))
            elif found and len(line.strip()) > 2 and not line.strip().endswith(":"):
                found[-1].text = _clean(found[-1].text + " " + line.strip())
    # Same id twice: keep the longer text, which is the fuller statement.
    best: Dict[str, Criterion] = {}
    for item in found:
        if item.id not in best or len(item.text) > len(best[item.id].text):
            best[item.id] = item
    return [best[key] for key in sorted(best, key=lambda k: (len(k), k))]


def from_text(read: Read, ticket: Ticket) -> None:
    """Fill `ticket` from a printed or exported ticket page."""
    ticket.source_format = read.fmt
    pages = _strip_page_furniture(read.pages or [read.text])
    text = "\n".join(pages)
    lines = [line.rstrip() for line in text.split("\n")]

    # --- key and title -----------------------------------------------------
    name = str(read.path).replace("\\", "/").split("/")[-1]
    in_name = _KEY_RE.search(name)
    keys = _KEY_RE.findall(text)
    ticket.key = in_name.group(1) if in_name else (keys[0] if keys else "")
    for line in lines:
        if line.strip():
            candidate = _clean(line)
            if ticket.key and ticket.key in candidate:
                ticket.title = _clean(candidate.replace(ticket.key, "")
                                      .strip(" []-–—:"))
            else:
                ticket.title = candidate
            break
    if not ticket.title and in_name:
        ticket.title = _clean(name.split("]", 1)[-1].rsplit(".", 1)[0])

    # --- sections ----------------------------------------------------------
    section = "header"
    buckets: Dict[str, List[str]] = {}
    for line in lines:
        stripped = line.strip()
        lowered = re.sub(r"[:\s]+$", "", stripped.lower())
        if lowered in _SECTIONS and len(stripped) <= 40:
            section = _SECTIONS[lowered]
            continue
        buckets.setdefault(section, []).append(line)

        _read_fields(stripped, ticket)

    ticket.description = "\n".join(buckets.get("description", [])).strip()
    if not ticket.description:
        ticket.description = "\n".join(buckets.get("summary", [])).strip()

    for line in buckets.get("attachments", []):
        for item in re.split(r"[,;]\s*|\s{2,}", line.strip()):
            item = item.strip()
            if re.search(r"\.[A-Za-z0-9]{2,5}$", item):
                ticket.attachments.append(item)
    for line in buckets.get("links", []):
        ticket.links.extend(_KEY_RE.findall(line))

    # --- comments ----------------------------------------------------------
    comment_text = "\n".join(buckets.get("comments", []))
    ticket.comments = _comments_from(comment_text)
    if comment_text.strip() and not ticket.comments:
        ticket.comments_parsed = False
        ticket.comments = [Comment(author="", created="",
                                   body=comment_text.strip(), index=1)]
        ticket.notes.append(
            "the comment thread could not be split into individual comments; "
            "it is kept whole, read it in full")

    # --- criteria ----------------------------------------------------------
    ticket.criteria = _criteria_from(text, source="ticket")

    # --- anything not placed ----------------------------------------------
    placed = {"description", "summary", "comments", "attachments", "links"}
    leftover = []
    for key, value in buckets.items():
        if key in placed:
            continue
        body = "\n".join(value).strip()
        if body:
            leftover.append(body)
    ticket.unplaced = "\n".join(leftover).strip()


def _comments_from(text: str) -> List[Comment]:
    comments: List[Comment] = []
    current: Optional[Comment] = None
    for line in text.split("\n"):
        stripped = line.strip()
        header = None
        for pattern in _COMMENT_RES:
            header = pattern.match(stripped)
            if header:
                break
        if header:
            current = Comment(author=_clean(header.group("author")),
                              created=_clean(header.group("when")),
                              index=len(comments) + 1)
            comments.append(current)
            continue
        if current is not None and stripped:
            current.body = (current.body + "\n" + stripped).strip()
    return [c for c in comments if c.body or c.author]


# ===========================================================================
# Identifiers
# ===========================================================================

def _identifiers(ticket: Ticket, text: str) -> List[str]:
    found: List[str] = []
    for pattern in _IDENT_RES:
        for token in pattern.findall(text):
            token = token.strip()
            if token in _IDENT_STOP or token == ticket.key:
                continue
            if _KEY_RE.match(token):                # another ticket key
                continue
            if re.fullmatch(r"(?:AC|OQ|CR|AK)-?\d+", token, re.I):
                continue
            if re.fullmatch(r"\d[\d.,-]*", token):
                continue
            if token not in found:
                found.append(token)
    return found[:80]


# ===========================================================================
# Entry point
# ===========================================================================

def from_read(read: Read) -> Ticket:
    """Structure a ticket from a file that `documents.read_file` has read."""
    ticket = Ticket(source_format=read.fmt, notes=list(read.notes),
                    pages=read.pages, unreadable_pages=read.unreadable_pages)
    if not read.ok and not read.text.strip():
        return ticket

    handled = False
    if read.fmt in ("jira-xml", "xml"):
        handled = from_jira_xml(read.text, ticket)
        if not handled:                             # XML, but not Jira's
            ticket.notes.append(
                "not a Jira XML export; read as plain text instead")
            read.text = html_to_text(read.text)
            read.pages = [read.text]
    if not handled:
        from_text(read, ticket)

    body = "\n".join([ticket.title, ticket.description, ticket.unplaced]
                     + [c.body for c in ticket.comments]
                     + [c.text for c in ticket.criteria])
    ticket.identifiers = _identifiers(ticket, body)
    if not ticket.criteria:
        ticket.notes.append(
            "no acceptance criteria found in this ticket - say so in the "
            "report rather than leaving the section out")
    return ticket
