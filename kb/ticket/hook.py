"""
The Claude Code hook: a ticket file named in a message is parsed before the
model reads the message.

Registered by `setup` as a UserPromptSubmit hook, so the sequence is:

    you:     analyse @Tickets/ALPHAPLUS-1234.pdf
    hook:    alphaplus-kb ticket-hook          (this module)
    Claude:  sees the ticket already structured - fields, every comment,
             the acceptance criteria, and what could not be read

## Two rules it never breaks

**It never blocks the turn.** Any failure - an unreadable file, a path that is
not there, a parser fault - exits 0 with nothing added. A ticket reader that
can stop somebody working would be removed within a day, and rightly.

**It never invents.** What it adds is what the file said, with a quality block
naming the pages it could not read. The skill decides what that means.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import List, Optional

from . import SUPPORTED, parse, write

#: Most a hook adds to the prompt. A 5-page ticket is about 12,000 characters;
#: past this the structured half is kept and the raw pages are left in the
#: file, which the added text names.
MAX_CONTEXT = 40_000

#: Files this size are not tickets. Reading one would stall the turn.
MAX_BYTES = 64 * 1024 * 1024

#: At most this many ticket files from one message.
MAX_FILES = 3

_SUFFIXES = "|".join(s.lstrip(".") for s in SUPPORTED)
#: `@path`, "quoted path", or a bare path ending in a ticket suffix.
_PATH_RES = (
    re.compile(r"@\"([^\"\n]+?\.(?:" + _SUFFIXES + r"))\"", re.I),
    re.compile(r"@'([^'\n]+?\.(?:" + _SUFFIXES + r"))'", re.I),
    re.compile(r"@(\S+?\.(?:" + _SUFFIXES + r"))(?=[\s,;:]|$)", re.I),
    re.compile(r"\"([^\"\n]+?\.(?:" + _SUFFIXES + r"))\"", re.I),
    re.compile(r"'([^'\n]+?\.(?:" + _SUFFIXES + r"))'", re.I),
    re.compile(r"([A-Za-z]:[\\/][^\n\"'<>|?*]+?\.(?:" + _SUFFIXES + r"))(?=[\s,;:]|$)", re.I),
    re.compile(r"(?<![\w@\"'])((?:[\w.\-]+[\\/])*[\w.\- ]+?\.(?:" + _SUFFIXES + r"))(?=[\s,;:]|$)", re.I),
)

_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9_]{1,19}-\d{1,7}\b")

#: Where a ticket export is kept when it is not named with a path.
_TICKET_DIRS = ("", "Tickets", "tickets", "Ticket", "docs", "Doku")

#: Folders swept on every message, whatever the message says.
#:
#: The other route needs a filename in the text, and the attach button does
#: not put one there - it hands the file to the agent, and the hook sees a
#: message that mentions no file at all. So there is a place to put a ticket
#: instead: drop it in, and it is read on the next thing you type.
#: Folder names a ticket may be dropped into. Case variants are listed for
#: case-sensitive file systems; on Windows they are one folder, and
#: _drop_dirs() collapses them so a ticket is read once, not once per spelling.
_DROP_NAMES = ("tickets", "Tickets", "ticket", "Ticket")


def _drop_dirs(cwd: Path) -> List[Path]:
    """Every distinct drop folder that exists, each exactly once.

    Looked for beside the working directory and inside the configured source
    tree - whatever that tree is called. No installation's folder name is
    written here: the source root comes from configuration.
    """
    roots = [cwd]
    try:
        from .. import config
        source = Path(config.SOURCE_ROOT)
        if source.is_dir():
            roots.append(source)
    except Exception:                                     # noqa: BLE001
        pass            # a configuration problem must never stop a message
    seen, out = set(), []
    for root in roots:
        for name in _DROP_NAMES:
            directory = root / name
            try:
                if not directory.is_dir():
                    continue
                key = os.path.normcase(str(directory.resolve()))
            except OSError:
                continue
            if key not in seen:
                seen.add(key)
                out.append(directory)
    return out

#: A dropped file is read once. The structured copy beside it is the record
#: of that, so a folder holding forty tickets does not re-read forty tickets
#: on every message.
def _already_read(path: Path, root: Path) -> bool:
    from . import WORK_DIR
    work = root / WORK_DIR
    if not work.is_dir():
        return False
    try:
        newest = max((p.stat().st_mtime for p in work.glob("*.ticket.xml")),
                     default=0.0)
        return newest >= path.stat().st_mtime
    except OSError:
        return False


def dropped(cwd: Path) -> List[Path]:
    """Ticket files waiting in a drop folder that have not been read yet."""
    out: List[Path] = []
    for directory in _drop_dirs(cwd):
        try:
            for entry in sorted(directory.iterdir()):
                if not entry.is_file():
                    continue
                if entry.suffix.lower() not in SUPPORTED:
                    continue
                if _already_read(entry, cwd):
                    continue
                out.append(entry)
                if len(out) >= MAX_FILES:
                    return out
        except OSError:
            continue
    return out


def candidates(prompt: str, cwd: Path) -> List[Path]:
    """Ticket files this message names, in the order they appear."""
    found: List[Path] = []

    def offer(raw: str) -> None:
        raw = raw.strip().strip("\"'")
        if not raw:
            return
        path = Path(raw)
        for base in (None, cwd):
            candidate = path if path.is_absolute() or base is None else base / path
            try:
                if candidate.is_file() and candidate not in found:
                    found.append(candidate)
                    return
            except OSError:
                return

    for pattern in _PATH_RES:
        for match in pattern.finditer(prompt):
            offer(match.group(1))
            if len(found) >= MAX_FILES:
                return found[:MAX_FILES]

    # No path, but a ticket key: look where exports are kept.
    if not found:
        for key in dict.fromkeys(_KEY_RE.findall(prompt)):
            number = key.split("-")[-1]
            for folder in _TICKET_DIRS:
                directory = cwd / folder if folder else cwd
                try:
                    if not directory.is_dir():
                        continue
                    for entry in sorted(directory.iterdir()):
                        if not entry.is_file() or entry.suffix.lower() not in SUPPORTED:
                            continue
                        stem = entry.stem.upper()
                        if key.upper() in stem or re.search(r"\b" + number + r"\b", stem):
                            found.append(entry)
                            break
                except OSError:
                    continue
                if found:
                    break
            if len(found) >= MAX_FILES:
                break
    return found[:MAX_FILES]


def context_for(path: Path, root: Path) -> str:
    """The block of text added to the turn for one ticket file."""
    try:
        if path.stat().st_size > MAX_BYTES:
            return ""
    except OSError:
        return ""
    parsed = parse(path)
    written: Optional[Path] = None
    try:
        written = write(parsed, root, path)
    except OSError:
        written = None

    # Into the store, here, rather than when somebody remembers to. A ticket
    # read and not recorded leaves the knowledge base exactly as empty as it
    # was, and nothing says so.
    recorded = ""
    try:
        from .record import record
        _n, recorded = record(parsed, path)
    except Exception:                               # noqa: BLE001
        recorded = ""

    xml = parsed.xml
    if len(xml) > MAX_CONTEXT:
        cut = xml.find("  <pages count=")
        if cut > 0:
            xml = xml[:cut] + ("  <!-- the page text is in the file named below,\n"
                               "       too long to include here -->\n</ticket>\n")
        else:
            xml = xml[:MAX_CONTEXT]
    lines = [
        f"[alphaplus] Ticket file read: {path}",
        (f"[alphaplus] {recorded} - `recall` finds them in a later session"
         if recorded else ""),
        (f"[alphaplus] Structured copy written to: {written}" if written else
         "[alphaplus] The structured copy could not be written; it is below only."),
        "[alphaplus] This is the ticket as the file carries it. Read the "
        "acceptance criteria and every comment before the first tool call; "
        "an unreadable page is named in <quality> and must be read by eye.",
        "",
        xml,
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """Read the hook payload on stdin, print context on stdout, always exit 0."""
    try:
        raw = sys.stdin.read()
    except Exception:                               # noqa: BLE001
        return 0
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {"prompt": raw}

    prompt = str(payload.get("prompt") or "")
    cwd = Path(str(payload.get("cwd") or os.getcwd()))
    if not prompt.strip():
        return 0

    try:
        files = candidates(prompt, cwd)
    except Exception:                               # noqa: BLE001
        files = []
    # Whatever the message said, a ticket waiting in the drop folder is a
    # ticket somebody wants read. This is the route that works when the file
    # arrived by the attach button, which puts no path in the text.
    try:
        for extra in dropped(cwd):
            if extra not in files:
                files.append(extra)
    except Exception:                               # noqa: BLE001
        pass
    if not files:
        return 0
    files = files[:MAX_FILES]

    blocks: List[str] = []
    for path in files:
        try:
            block = context_for(path, cwd)
        except Exception as exc:                    # noqa: BLE001
            block = (f"[alphaplus] Could not read the ticket file {path}: "
                     f"{type(exc).__name__}. Read it yourself before analysing.")
        if block:
            blocks.append(block)
    if not blocks:
        return 0

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n\n".join(blocks),
        }
    }))
    return 0
