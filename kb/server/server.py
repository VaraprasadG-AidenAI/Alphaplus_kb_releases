"""
MCP server for the ALPHAPLUS knowledge base.

    python -m kb.server.server            speak MCP on stdin/stdout
    python -m kb.server.server --selftest exercise every tool and exit

MCP is JSON-RPC 2.0 over stdio, so this is implemented directly rather than
through the SDK.  That is a deliberate choice: it means the server has **no
dependencies at all** beyond the Python standard library, which matters when
this eventually has to run inside a client's locked-down network where
`pip install` is a change request.

Register it with Claude Code by putting this in .mcp.json:

    {"mcpServers": {"alphaplus": {
        "command": "python",
        "args": ["-m", "kb.server.server"],
        "cwd": "<repo root>"}}}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .. import config
from . import accounting
from . import ledger, memory, tools, validate
from .tools import NotIndexedError

PROTOCOL_VERSION = "2025-11-25"

#: Revisions we know about.  Kept for reference and for the selftest; the
#: handshake does not restrict itself to this set - see _looks_like_revision.
KNOWN_PROTOCOLS = frozenset({
    "2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25",
    "2026-07-28",
})

#: MCP revisions are dated, e.g. 2025-06-18.
_REVISION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _looks_like_revision(value: Any) -> bool:
    return isinstance(value, str) and bool(_REVISION_RE.match(value))
SERVER_NAME = "alphaplus"
def _build_version() -> str:
    """Which build is answering, for the client's handshake log."""
    try:
        from kb import config
        return str(config.BUILD_VERSION)
    except Exception:                                         # noqa: BLE001
        return "0.0"


SERVER_VERSION = _build_version()


def _text(description: str) -> Dict[str, Any]:
    return {"type": "string", "description": description}


#: The tool catalogue.  Descriptions are written for the agent, not for a
#: human reading documentation - they say when to reach for the tool, because
#: that is what decides whether it gets used at the right moment.
TOOLS: List[Dict[str, Any]] = [
    {
        "name": "who_touches_file",
        "description":
            "Which programs read or write a file. Use mode='write' for the "
            "question that matters most on a data bug: what could have put "
            "this value here. Text search cannot answer this - it cannot tell "
            "a read from a write, and it misses programs that reach the file "
            "through a logical.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": _text("the file name, e.g. CUSMAS"),
                "mode": {"type": "string", "enum": ["any", "read", "write"],
                         "description": "default any"},
                "page": {"type": "integer", "minimum": 1,
                         "description": "window of rows, default 1; the answer says how many"},
            },
            "required": ["file"],
        },
    },
    {
        "name": "resolve_files",
        "description":
            "Every PHYSICAL file a name touches, and what each is. A DDS member "
            "is a TEMPLATE; the rows live in files created from it at "
            "run time, and one template routinely becomes several. A "
            "COBOL program never names a physical file - it names a "
            "token an OVRDBF in the calling CL redirects, so the same "
            "program reads different files under different callers. "
            "Call before any layout or data change: separates what is "
            "EDITED (template) from CONVERTED (data files), and flags "
            "one name resolving to several. Takes a template, a data "
            "file or a program-side name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": _text("a file, template or override name"),
            },
            "required": ["name"],
        },
    },
    {
        "name": "where_is_field_used",
        "description":
            "Which programs read or write a specific field, and what the "
            "field is - its type, the files it lives on, and its valid values "
            "from the 88-levels. Use for 'why is this column wrong'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "field": _text("the field name, e.g. CUSBAL"),
                "direction": {"type": "string",
                              "enum": ["any", "read", "write"],
                              "description": "default any"},
                "page": {"type": "integer", "minimum": 1,
                         "description": "window of rows, default 1; the answer says how many"},
            },
            "required": ["field"],
        },
    },
    {
        "name": "resolve_overrides",
        "description":
            "Which PHYSICAL file a program actually opens. On IBM i the COBOL "
            "names a file but an OVRDBF in the CL driver decides what that "
            "resolves to at run time. Reading the COBOL alone gives the wrong "
            "answer. Use this before concluding a program touches a file.",
        "inputSchema": {
            "type": "object",
            "properties": {"program": _text("program or CL name")},
            "required": ["program"],
        },
    },
    {
        "name": "program_interface",
        "description":
            "A program's parameters in bytes, each CL CALL's argument sizes, "
            "and every mismatch. Use when a ticket changes a LINKAGE copybook "
            "or a CL variable passed to a program.",
        "inputSchema": {
            "type": "object",
            "properties": {"program": _text("program or CL name")},
            "required": ["program"],
        },
    },
    {
        "name": "who_calls",
        "description":
            "What reaches a program - callers, and callers of callers. Use "
            "before changing anything shared, and to find out whether a "
            "program is reachable at all.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": _text("program name"),
                "depth": {"type": "integer", "minimum": 1, "maximum": 4,
                          "description": "levels to follow, default 1"},
                "page": {"type": "integer", "minimum": 1,
                         "description": "which window of 60 rows, default 1. "
                                        "The answer says how many there are"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "what_it_calls",
        "description":
            "What a program calls, including calls made through a variable "
            "where the target cannot be known statically - those are reported "
            "with their possible targets rather than hidden.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": _text("program name"),
                "depth": {"type": "integer", "minimum": 1, "maximum": 4,
                          "description": "levels to follow, default 1"},
                "page": {"type": "integer", "minimum": 1,
                         "description": "which window of 60 rows, default 1. "
                                        "The answer says how many there are"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "describe_file",
        "description":
            "Everything about a file: every field with type and description, "
            "the keys, the logical files built over it, and which programs "
            "read and write it. Use when a ticket is about data rather than "
            "behaviour.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": _text("file name"),
                "library": _text(
                    "narrow to one source library, e.g. APPALPH. Only "
                    "needed when a name exists in more than one"),
            },
            "required": ["file"],
        },
    },
    {
        "name": "explain_screen",
        "description":
            "The record formats, fields and function keys of a display or "
            "printer file, and which programs write which format. Also "
            "reports where a program names a format the file does not define, "
            "which is a run-time failure waiting to happen.",
        "inputSchema": {
            "type": "object",
            "properties": {"device_file": _text("display or printer file")},
            "required": ["device_file"],
        },
    },
    {
        "name": "find_object",
        "description":
            "What a member is: type, size, path, how many files it opens "
            "and what calls it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": _text("member name, exact or partial"),
                "library": _text(
                    "narrow to one source library, e.g. APPALPH. Only "
                    "needed when a name exists in more than one"),
            },
            "required": ["name"],
        },
    },
    {
        "name": "read_member",
        "description":
            "Read part of a source member with line numbers. Use this rather "
            "than Read for large programs - some run to 8,000 lines and will "
            "fill the context window.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "member_type": _text(
                    "pick between members sharing a name - a file and its copybook often collide"),
                "name": _text("member name"),
                "library": _text(
                    "narrow to one source library, e.g. APPALPH. Only "
                    "needed when a name exists in more than one"),
                "start": {"type": "integer", "minimum": 1,
                          "description": "first line, default 1"},
                "count": {"type": "integer", "minimum": 1, "maximum": 400,
                          "description": "lines to return, default 120"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "search_source",
        "description":
            "Text search scoped to a member type, e.g. only .PF or only "
            ".CBLLE members - what makes it fast and what Grep cannot do. "
            "Unscoped is allowed and runs to completion, but costs ~30x. "
            "The header counts every match FOUND and names members whose "
            "extra lines were withheld; raise per_member for those.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": _text("text or regular expression"),
                "member_type": _text(
                    "PF, CBLLE, a list CBLLE,CLLE, or CODE for every code "
                    "type; unscoped is 30x the work"),
                "limit": {"type": "integer", "minimum": 1,
                          "maximum": 400,
                          "description": "matching members, default 40"},
                "page": {"type": "integer", "minimum": 1,
                         "description": "window of rows, default 1; the answer says how many"},
                "per_member": {"type": "integer", "minimum": 1,
                               "description": "lines shown from one member, "
                                              "default 12. Caps display only, "
                                              "never the count"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "read_summary",
        "description":
            "A written page on what a member does in business terms and the "
            "tickets that land on it - a quick check you are in the right "
            "place. Generated prose, labelled, and flagged when the member "
            "changed since; verify against facts and source. Empty when "
            "summaries are switched off.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": _text("member name")},
            "required": ["name"],
        },
    },
    {
        "name": "unaccounted",
        "description":
            "CALL BEFORE DELIVERING ANY IMPACT REPORT. Pass the report "
            "text; returns every member a tool handed you this session that "
            "appears nowhere in it. Each one needs a state: change set, "
            "recompile set, or stated exclusion with evidence.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": _text("the report, or the final member lists"),
            },
            "required": ["text"],
        },
    },
    {
        "name": "coverage_report",
        "description":
            "What this knowledge base can and cannot see. Read it before "
            "concluding something is absent from the application - it may "
            "simply be outside the source. Job schedules, trigger "
            "attachments, authority and the data itself are permanent blind "
            "spots.",
        "inputSchema": {"type": "object", "properties": {}},
    },    {
        "name": "trace_ticket",
        "description":
            "START HERE for any JIRA ticket. Give it the ticket text in plain "
            "business language; returns ranked candidate members with "
            "the reason each matched. Works when the ticket names no "
            "identifier - it maps business words onto files, fields "
            "and programs. Leads, not answers: read the code it points "
            "at.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": _text("the ticket text, in full"),
                "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                          "description": "candidates to return, default 12"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "route_ticket",
        "description":
            "WHICH PART OF THE APPLICATION a ticket is about - menu or "
            "functional area, not member - by matching business language "
            "against written menu descriptions. trace_ticket shows the top "
            "few; use this for the full list. Model-written descriptions, not "
            "facts, and only for menus with a written page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": _text("the ticket text, in full"),
                "limit": {"type": "integer",
                          "description": "areas to return, default 6, max 25"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "find_similar",
        "description":
            "Written pages that read like this one's, for a defect likely to "
            "repeat. Similarity is between descriptions, not code - confirm "
            "with what_it_touches or who_touches_file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": _text("a menu, area or library that has a page"),
                "limit": {"type": "integer",
                          "description": "pages to return, default 8, max 25"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "program_structure",
        "description":
            "The paragraph graph of one COBOL program: which performs which, in "
            "what order, which loop, and what each reads, writes, "
            "calls and displays. Use BEFORE reading a program - a "
            "4,000 line member becomes forty labelled steps. Also "
            "lists paragraphs nothing performs (dead code) and PERFORM "
            "targets never declared (usually a misspelling). COBOL "
            "only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": _text("program member name, e.g. ORD100"),
                "member_type": _text(
                    "only when one name exists as more than one member type, "
                    "e.g. CBLLE"),
                "max_rows": {"type": "integer", "minimum": 10, "maximum": 2000,
                             "description": "rows of tree to print, default 220"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "what_paragraph_does",
        "description":
            "One paragraph in full: who performs it and whether that happens "
            "inside a loop, what it performs, the files it reads and writes, "
            "the programs it calls, the screen formats it uses, the fields it "
            "sets and the messages it sends - with the exact line range to "
            "read. Use after program_structure or where_is_field_used names "
            "a paragraph.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "program": _text("program member name"),
                "paragraph": _text("paragraph name, e.g. 2200-LOAD-SUBFILE"),
                "member_type": _text("only needed to disambiguate a name"),
            },
            "required": ["program", "paragraph"],
        },
    },
    {
        "name": "find_paragraph",
        "description":
            "Find paragraphs anywhere by name or by what they do: touches a "
            "file, calls a program, writes a field. The step between a "
            "ticket and a diff - who_touches_file gives sixty programs, this "
            "gives the paragraph inside them. Combine arguments to narrow. "
            "At least one is required.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": _text("substring of the paragraph name, e.g. VALIDATE"),
                "touches": _text("a file the paragraph reads or writes"),
                "calls": _text("a program the paragraph CALLs"),
                "writes": _text("a field the paragraph assigns"),
                "limit": {"type": "integer", "description": "default 40"},
                "page": {"type": "integer", "minimum": 1,
                         "description": "window of rows, default 1; the answer says how many"},
            },
        },
    },
    {
        "name": "what_it_touches",
        "description":
            "OUTWARD view of one member: files it opens and whether it can "
            "change them, copybooks it COPYs, programs it CALLs, "
            "overrides it sets, screen and report formats, messages, "
            "data areas, SQL. If you do not know what kind of thing "
            "the name is, call what_uses - it works that out itself. "
            "kind='all' (default) gives counts plus examples; set "
            "kind to one layer for the full list.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": _text("member name, e.g. ORD100 or ORD100C"),
                "kind": {"type": "string", "enum": [
                    "all", "files", "copybooks", "calls", "overrides",
                    "screens", "messages", "dataareas", "sql", "fields",
                    "indicators", "logicals", "drift", "unresolved"],
                    "description": "one layer, or all (default)"},
                "limit": {"type": "integer", "description": "default 40"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "what_uses",
        "description":
            "INWARD view of any object - it works out the kind itself: member, "
            "file, copybook, record format, message id, data area or "
            "field. Returns what calls it, COPYs it, opens it and "
            "whether they can change it, which CL overrides to it, the "
            "logicals over it, who writes its formats. START HERE when "
            "a ticket names anything and you do not know what it is. "
            "Also the blast radius: what recompiles, what can change "
            "the data.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": _text("any name from a ticket: CUSMAS, CUSREC, "
                              "APP0063, SFLCTL, APPCTL, CUSBAL"),
                "kind": {"type": "string", "enum": [
                    "all", "calls", "copybooks", "files", "overrides",
                    "screens", "messages", "dataareas", "logicals",
                    "fields", "defines"],
                    "description": "one layer, or all (default)"},
                "limit": {"type": "integer",
                          "description": "default 40; large sets fold to one "
                                         "row per group until you raise it"},
            },
            "required": ["name"],
        },
    },
    # ---- graph-backed tools ------------------------------------------------
    # These need graph.backend = "neo4j".  With backend = "sqlite" they return a
    # one-line message saying so rather than failing, because a tool that
    # errors reads as a broken server.
    {
        "name": "path_between",
        "description":
            "HOW one member reaches another - every shortest route, each hop "
            "labelled by the kind of dependency. edges='calls' (default) "
            "follows CALL statements. edges='all' also follows file opens, "
            "COPYs, OVRDBF overrides, logical files, menu options and SQL - "
            "a member usually reaches a FILE by opening it, not calling it, "
            "so a data question wants 'all'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "frm": _text("member the route starts at"),
                "to": _text("member the route ends at"),
                "edges": {"type": "string", "enum": ["calls", "all"],
                          "description": "which dependencies to follow, "
                                         "default calls"},
                "max_hops": {"type": "integer", "minimum": 1, "maximum": 8,
                             "description": "how far to search, default 8"},
                "max_paths": {"type": "integer", "minimum": 1, "maximum": 25,
                              "description": "routes to return, default 8"},
            },
            "required": ["frm", "to"],
        },
    },
    {
        "name": "blast_radius",
        "description":
            "Everything a change to one member could reach, in one answer: "
            "callers, callees, recompiles, logical files, writers, "
            "readers and overrides - seven layers in one round trip "
            "instead of six queries you add up yourself, which is "
            "where the arithmetic goes wrong. Reach for it once "
            "what_uses or find_object has told you what the name is. "
            "Returns COUNTS: turn the ones that matter into NAMES "
            "with the calls it lists at the end of its own output.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": _text("member name"),
                "depth": {"type": "integer",
                          "description": "call hops to follow, default 3"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "menu_path_to",
        "description":
            "How a user reaches this program from a menu - which menu, which "
            "option numbers, which labels. The question a business user asks: "
            "where do I click to get here.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": _text("program or menu name"),
                "depth": {"type": "integer",
                          "description": "menu hops, default 4"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "remember",
        "description":
            "Store one durable fact from this ticket's analysis in ticket "
            "memory, kept across sessions and tickets. Returns what OTHER "
            "tickets recorded about the same subject - compare them.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket": _text("ticket id"),
                "kind": {"type": "string", "enum": list(memory.KINDS)},
                "subject": _text("member, field, file or short topic"),
                "statement": _text("the fact, self-contained"),
                "evidence": _text("member:line or tool call"),
                "library": _text("subject's library"),
                "confidence": {"type": "string", "enum": list(memory.CONFIDENCE)},
                "supersedes": {"type": "integer", "minimum": 1,
                               "description": "id of this ticket's fact it corrects"},
            },
            "required": ["ticket", "kind", "subject", "statement"],
        },
    },
    {
        "name": "recall",
        "description":
            "What earlier tickets stored in ticket memory, by exact subject, "
            "free text or ticket id. Flags subjects where tickets disagree.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": _text("words or names to search for"),
                "subject": _text("exact member, field or file name"),
                "ticket": _text("one ticket's facts"),
                "kind": {"type": "string", "enum": list(memory.KINDS)},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200,
                          "description": "default 30"},
            },
        },
    },
]

HANDLERS: Dict[str, Callable[..., str]] = {
    "who_touches_file": tools.who_touches_file,
    "resolve_files": tools.resolve_files,
    "where_is_field_used": tools.where_is_field_used,
    "resolve_overrides": tools.resolve_overrides,
    "program_interface": tools.program_interface,
    "who_calls": tools.who_calls,
    "what_it_calls": tools.what_it_calls,
    "path_between": tools.path_between,
    "blast_radius": tools.blast_radius,
    "menu_path_to": tools.menu_path_to,
    "describe_file": tools.describe_file,
    "explain_screen": tools.explain_screen,
    "find_object": tools.find_object,
    "read_member": tools.read_member,
    "search_source": tools.search_source,
    "read_summary": tools.read_summary,
    "route_ticket": tools.route_ticket,
    "find_similar": tools.find_similar,
    "coverage_report": tools.coverage_report,
    "unaccounted": tools.unaccounted,
    "trace_ticket": tools.trace_ticket,
    "program_structure": tools.program_structure,
    "what_paragraph_does": tools.what_paragraph_does,
    "find_paragraph": tools.find_paragraph,
    "what_it_touches": tools.what_it_touches,
    "what_uses": tools.what_uses,
    "remember": memory.remember,
    "recall": memory.recall,
}


#: Name -> declaration, for the argument check.  Built once; `selftest` asserts
#: it covers HANDLERS in both directions.
TOOLS_BY_NAME: Dict[str, Dict[str, Any]] = {t["name"]: t for t in TOOLS}


# =======================================================================
# The response budget
# =======================================================================
#
# Nothing capped tool output before this.  The measured worst case was 7,291
# tokens - `program_structure` on a 7,951-line member - which is under the
# client's 25,000-token ceiling, so nothing was actually failing.  But Claude
# Code WARNS at 10,000, four such answers in a session is 30,000 tokens, and
# `what_uses(kind='files', limit=4000)` is a legal call whose size is bounded
# only by how busy the file is.
#
# Chars, not tokens, and deliberately: counting tokens needs a tokeniser, every
# tokeniser is a dependency, and a wheel per client network is a change
# request.  ~4 chars/token is the documented rule of thumb; dense identifier
# tables tokenise worse than prose, so these are conservative on purpose.
#
# Aim under the WARNING, not under the ceiling.  A warning the user sees is a
# bug report.

#: ~10,000 tokens.
RESULT_CHARS_DEFAULT = 40_000

#: Tools that have earned more, with the reason.
RESULT_CHARS: Dict[str, int] = {
    # 400 numbered source lines, asked for explicitly and bounded by `count`.
    "read_member": 60_000,
    # A paragraph tree plus its dead-code and misspelling reports.
    "program_structure": 30_000,
    # Prose and four small tables; if this is near the cap something is wrong.
    "coverage_report": 20_000,
}


def _cap(name: str, text: str) -> Tuple[str, bool]:
    """Trim a response to its budget, and say so where the agent will read it.

    The tail is cut, not the head: every one of these outputs is ordered
    best-first, so the head IS the answer.  The cut lands on a line boundary
    because a mid-row cut inside a `_table` produces a line an agent can read
    as a member name - inventing an identifier is worse than losing a row.

    The notice names a concrete next action rather than saying "truncated",
    because truncation eats the "call what_paragraph_does next" trailer several
    tools end with, and losing that costs a turn.
    """
    cap = RESULT_CHARS.get(name, RESULT_CHARS_DEFAULT)
    if len(text) <= cap:
        return text, False

    room = cap - 400                      # leave space for the notice itself
    cut = text.rfind("\n", 0, room)
    if cut < room // 2:                   # one enormous line: hard cut
        cut = room
    return (text[:cut] + "\n\n"
            + f"  [CUT AT {cut:,} OF {len(text):,} CHARACTERS]\n"
            + "  This is a size cap, not the end of the answer. Nothing above\n"
            + "  this line is wrong; what is below it was not sent.\n"
            + "  Ask something narrower to see the rest: kind=<one layer>,\n"
            + "  member_type=<one type>, or a smaller limit=.\n"), True


# =======================================================================
# The machine-readable half of the declaration
# =======================================================================
#
# None of this was declared: 0 of 23 tools carried a `title`, `annotations` or
# any `_meta`.  The prose was already good - every tool has per-property
# descriptions and the whole tools/list payload is 1.68% of a 200k window - so
# what was missing was the part a client reads rather than the model.
#
# Applied in a loop rather than written into each literal, for one reason:
# the declared cap and the ENFORCED cap must be the same number.  Two
# structures that have to agree will drift - `kb/graph/backend.py` needed a
# whole `unrouted()` check to police exactly that - so they are not two
# structures.

#: Titles.  Short noun phrases; the client shows these in a permission prompt,
#: where "who_touches_file" is less readable than "Who touches a file".
_TITLES: Dict[str, str] = {
    "who_touches_file": "Who touches a file",
    "resolve_files": "Physical files a name touches",
    "where_is_field_used": "Where a field is used",
    "resolve_overrides": "Resolve file overrides",
    "program_interface": "Program parameters and sizes",
    "who_calls": "Who calls this",
    "what_it_calls": "What this calls",
    "describe_file": "Describe a file",
    "explain_screen": "Explain a screen or report",
    "find_object": "Find a member",
    "read_member": "Read source lines",
    "search_source": "Search source text",
    "read_summary": "Read a written page",
    "coverage_report": "What the index can see",
    "unaccounted": "Members not yet placed",
    "trace_ticket": "Trace a ticket to code",
    "route_ticket": "Route a ticket to an area",
    "find_similar": "Find similar pages",
    "program_structure": "Program paragraph structure",
    "what_paragraph_does": "What a paragraph does",
    "find_paragraph": "Find a paragraph",
    "what_it_touches": "What a member touches",
    "what_uses": "What uses this",
    "path_between": "Route between two members",
    "blast_radius": "Blast radius of a change",
    "menu_path_to": "Menu path to a program",
    "remember": "Remember a ticket fact",
    "recall": "Recall earlier ticket facts",
}

#: The one tool that writes. It adds a row to ticket memory - never to the
#: source or the index - and a repeated call returns the existing row, so it
#: is idempotent and not destructive.
_WRITES: Dict[str, Dict[str, bool]] = {
    "remember": {"readOnlyHint": False, "destructiveHint": False,
                 "idempotentHint": True, "openWorldHint": False},
}

for _tool in TOOLS:
    _name = _tool["name"]
    if _name in _TITLES:
        _tool["title"] = _TITLES[_name]
    # Every tool but `remember` is a read: nothing here writes, renames or
    # deletes source or index - `kb.config.library_of` only reads a library
    # out of a path and CLAUDE.md requires it stay that way. For a read,
    # `destructiveHint` and `idempotentHint` carry no information, so they are
    # omitted rather than set to their defaults.
    #
    # openWorldHint is false: the answers come from a local index of a fixed
    # source tree and a local memory file, not from the internet.
    _tool["annotations"] = dict(_WRITES.get(
        _name, {"readOnlyHint": True, "openWorldHint": False}))
    _tool["_meta"] = {
        "anthropic/maxResultSizeChars":
            RESULT_CHARS.get(_name, RESULT_CHARS_DEFAULT),
    }


def _store_name() -> str:
    """Which relational store answered, for the ledger."""
    try:
        from kb import store as kbstore
        return kbstore.backend()
    except Exception:                                        # noqa: BLE001
        return "?"


def _graph_name() -> str:
    """Which graph backend is configured, for the ledger."""
    try:
        from kb.graph import backend
        return backend.mode()
    except Exception:                                        # noqa: BLE001
        return "?"


# ===========================================================================
# JSON-RPC
# ===========================================================================

def _result(request_id: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def handle(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        # Answer in the version the client asked for, when we know it.
        #
        # This server hardcoded its own version and ignored the request, so a
        # client asking for 2025-06-18 was told 2024-11-05.  Nothing in this
        # server's surface differs between those revisions - it serves tools
        # and nothing else - but a client is entitled to refuse a version it
        # did not ask for, and that refusal looks like "cannot connect" with
        # a perfectly healthy HTTP 200 behind it.
        # Agree to any well-formed revision the client names, not just the
        # ones on a list here.  A fixed list is the same bug with a longer
        # fuse: the next revision Claude Code asks for would not be on it, we
        # would answer with our own, and the connection would fail again for
        # the same invisible reason.  This server serves tools and nothing
        # else, and that surface has not changed across revisions.
        asked = params.get("protocolVersion")
        agreed = asked if _looks_like_revision(asked) else PROTOCOL_VERSION
        return _result(request_id, {
            "protocolVersion": agreed,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return _result(request_id, {})

    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            return _error(request_id, -32601, f"no such tool: {name}")

        # This branch is the one place both transports meet - kb/server/web.py
        # calls rpc.handle() too - so the argument check, the size cap and the
        # ledger all live here rather than in 23 handlers.
        bad = validate.check(TOOLS_BY_NAME.get(name), arguments)
        if bad:
            ledger.record(name, arguments, 0.0, len(bad), True,
                          "InvalidArguments")
            return _result(request_id, {
                "content": [{"type": "text", "text": bad}],
                "isError": True,
            })

        failed, kind = False, ""
        tools.reset_stats()
        started = time.perf_counter()
        try:
            text = handler(**arguments)
        except TypeError as exc:
            # A signature mismatch the schema did not describe.  Still the
            # agent's to fix, so it still gets the detail.
            text = f"bad arguments for {name}: {exc}"
            failed, kind = True, "TypeError"
        except NotIndexedError as exc:
            # Expected on a fresh install: a plain instruction, not a stack
            # trace.  This is the first thing a new user hits.
            text = str(exc)
            failed, kind = True, "NotIndexedError"
        except Exception as exc:                # never kill the server
            # NOT the traceback.  It used to be, and it spent ~400 tokens of
            # the agent's context saying "our SQL was wrong" in a form that
            # invites the model to debug this server instead of answering the
            # question.  The traceback goes to stderr and the ledger, where a
            # developer can read it; the agent gets a sentence it can act on.
            kind = type(exc).__name__
            failed = True
            text = (f"The knowledge base failed on that call ({kind})."
                    + chr(10)
                    + "  This is a defect in the knowledge base, not in your"
                    + " question, and the same call will fail again."
                    + chr(10)
                    + "  Use a different tool, or narrower arguments."
                    + f" The failure is recorded server-side as {name}/{kind}.")
            print(f"  [tool] {name} raised {kind}", file=sys.stderr)
            traceback.print_exc(limit=6, file=sys.stderr)
            sys.stderr.flush()

        text = text or ""

        # Every member name this call hands over goes on the reconciliation
        # ledger BEFORE the response is capped, so a name that only appears
        # in a trimmed tail is still known to have been returned. See
        # kb/server/accounting.py - the agent is the thing being checked, so
        # the record cannot be kept by the agent.
        try:
            if accounting.note(name, text):
                notice = accounting.announce_once()
                if notice:
                    text = text + chr(10) + notice
        except Exception:                                    # noqa: BLE001
            pass

        capped, truncated = _cap(name, text)
        ms = (time.perf_counter() - started) * 1000.0
        ledger.record(name, arguments, ms, len(capped), failed, kind,
                      truncated,
                      extra={"sql_n": tools.SQL_STATS["n"],
                             "sql_ms": round(tools.SQL_STATS["ms"], 1),
                             "store": _store_name(),
                             "graph": _graph_name()})
        return _result(request_id, {
            "content": [{"type": "text", "text": capped}],
            # Reported honestly so the client can distinguish a real answer
            # from a failure instead of having to notice the word 'Traceback'.
            "isError": failed,
        })

    if request_id is None:
        return None
    return _error(request_id, -32601, f"unknown method: {method}")


def serve() -> int:
    """Read JSON-RPC from stdin, write responses to stdout, one per line.

    ## stdout belongs to the protocol, and only to the protocol

    Under stdio every byte on stdout is parsed as part of a reply, so one
    stray `print()` anywhere below this function kills the session - and the
    client reports it as "connection closed", which points at the transport
    rather than at the print.

    Two modules already in `kb/` print to stdout as their public API
    (`kb.index.consistency` and `kb.index.status`), and the obvious way to
    make either reachable from a tool is to call it.  So rather than rely on
    nobody ever doing that, the real stdout is kept private to this loop and
    `sys.stdout` is pointed at stderr for the life of the process.  A handler
    that prints is then merely noisy on the console instead of fatal.
    """
    out = sys.stdout
    sys.stdout = sys.stderr
    ledger.purge()

    # Decode stdin ourselves, replacing anything that is not UTF-8.
    #
    # Iterating `sys.stdin` raises UnicodeDecodeError on the first bad byte,
    # and that exception ends the process: one malformed frame - a truncated
    # paste, a mis-encoded path, a client bug - took the whole session down
    # mid-analysis, and the client reports it as "server disconnected". A
    # frame we cannot decode is a frame we answer with a parse error, exactly
    # like malformed JSON below.
    stream = getattr(sys.stdin, "buffer", None)
    lines = (iter(stream.readline, b"") if stream is not None
             else iter(sys.stdin.readline, ""))
    for raw in lines:
        line = (raw.decode("utf-8", "replace") if isinstance(raw, bytes)
                else raw)
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            # Answer rather than staying silent.  A dropped line that carried
            # an id would leave the client waiting for a reply that never
            # comes, with no timeout on either side.
            out.write(json.dumps(
                _error(None, -32700, f"parse error: {exc}")) + "\n")
            out.flush()
            continue
        if not isinstance(message, dict):
            out.write(json.dumps(_error(
                None, -32600,
                "invalid request: expected a JSON object")) + "\n")
            out.flush()
            continue
        try:
            response = handle(message)
        except Exception as exc:
            # The tools/call branch handles its own failures; reaching here
            # means the dispatcher itself broke, which is ours to fix and not
            # the agent's to read.  Same reasoning as the tool error path: the
            # traceback goes to stderr, the client gets a sentence.
            print("  [rpc] handle() raised", file=sys.stderr)
            traceback.print_exc(limit=6, file=sys.stderr)
            sys.stderr.flush()
            response = _error(message.get("id"), -32603,
                              f"internal error: {type(exc).__name__}")
        if response is not None:
            out.write(json.dumps(response) + "\n")
            out.flush()
    return 0


# ===========================================================================

def selftest() -> int:
    """Exercise every tool so a broken one is found before an agent meets it."""
    # Ticket memory is the user's own record. The probes below write to it, so
    # they write to a throwaway file that is removed afterwards.
    scratch = tempfile.mkdtemp(prefix="alphaplus-selftest-")
    previous_memory = os.environ.get("ALPHAPLUS_MEMORY_DB")
    os.environ["ALPHAPLUS_MEMORY_DB"] = os.path.join(scratch, "memory.db")
    memory.close_all()
    try:
        return _selftest()
    finally:
        memory.close_all()
        if previous_memory is None:
            os.environ.pop("ALPHAPLUS_MEMORY_DB", None)
        else:
            os.environ["ALPHAPLUS_MEMORY_DB"] = previous_memory
        shutil.rmtree(scratch, ignore_errors=True)


def _selftest() -> int:
    probes: List[tuple] = [
        ("remember", {"ticket": "SELFTEST-1", "kind": "field",
                      "subject": "SELFTEST-FIELD", "statement": "width 10",
                      "evidence": "selftest:1"}),
        ("remember", {"ticket": "SELFTEST-2", "kind": "field",
                      "subject": "SELFTEST-FIELD", "statement": "width 20"}),
        ("recall", {"subject": "SELFTEST-FIELD"}),
        ("recall", {"query": "width SELFTEST-FIELD", "limit": 5}),
        ("coverage_report", {}),
        ("find_object", {"name": "ORD100"}),
        ("who_touches_file", {"file": "CUSMAS", "mode": "write"}),
        ("where_is_field_used", {"field": "CUSBAL"}),
        ("resolve_overrides", {"program": "ORD100C"}),
        ("program_interface", {"program": "ORD100"}),
        ("who_calls", {"name": "SYS100"}),
        ("what_it_calls", {"name": "ORD100"}),
        ("describe_file", {"file": "CUSMAS"}),
        ("explain_screen", {"device_file": "ORD100D"}),
        ("read_member", {"name": "ORD100", "start": 1, "count": 5}),
        ("search_source", {"pattern": "CUSBAL", "member_type": "PF"}),
        ("read_summary", {"name": "ORD100"}),
        ("trace_ticket", {"text": "Irish customers billed the wrong VAT"}),
        ("route_ticket", {"text": "invoice totals disagree with the lines"}),
        ("find_similar", {"name": "APMAIN"}),
        ("program_structure", {"name": "ORD100"}),
        ("what_paragraph_does", {"program": "ORD100",
                                 "paragraph": "PROCESS-SCREEN"}),
        ("find_paragraph", {"touches": "CUSMAS", "limit": 5}),
        ("what_it_touches", {"name": "ORD100"}),
        ("what_it_touches", {"name": "AGD109", "kind": "copybooks"}),
        ("what_uses", {"name": "CUSREC"}),
        ("what_uses", {"name": "APP0063"}),
        ("what_uses", {"name": "ORD100D"}),
        # The three graph-only tools. They were absent from this list, which
        # is why two of them shipped broken: menu_path_to denied every program
        # reached through a CL driver - 720 members - and blast_radius
        # undercounted writers because it read the OPEN mode and not the
        # verbs. Both were found by hand, not here.
        #
        # SEC407 rather than ORD100 on purpose: it is reached from APMAIN
        # option 3 through SEC407C, so it exercises the MENU_OPTION-then-CALLS
        # path. ORD100 has no menu route at all in this tree, so it would
        # have passed the empty answer as an answer.
        ("path_between", {"frm": "ORD100", "to": "SYS100"}),
        ("path_between", {"frm": "ORD100", "to": "CUSMAS",
                          "edges": "all"}),
        ("blast_radius", {"name": "CUSMAS"}),
        ("menu_path_to", {"name": "SEC407"}),
    ]
    # Every enum value of every enum property, not just the defaults.  Three
    # PostgreSQL faults hid behind unprobed enum values - two bare columns in
    # a GROUP BY and one cast over a comma list - and each one handed the
    # agent a traceback on every call.  A probe list of happy paths could not
    # have found them.
    for tool in TOOLS:
        schema = tool.get("inputSchema") or {}
        required = list(schema.get("required") or [])
        base = None
        for name, arguments in probes:
            if name == tool["name"] and all(k in arguments for k in required):
                base = arguments
                break
        if base is None:
            continue
        for prop, spec in (schema.get("properties") or {}).items():
            for value in (spec.get("enum") or []):
                if base.get(prop) != value:
                    probes.append((tool["name"], dict(base, **{prop: value})))

    print(f"  {len(probes)} probes, {len(TOOLS)} tools declared")
    failures = 0
    sizes: List[tuple] = []
    for name, arguments in probes:
        if name not in HANDLERS:
            print(f"  FAIL {name:<22} not registered")
            failures += 1
            continue
        # Through handle(), so the argument check, the size cap and the
        # ledger are exercised too.  Calling HANDLERS directly tested the
        # tools and none of the machinery that now wraps them.
        reply = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": name, "arguments": arguments}})
        result = reply.get("result") or {}
        text = ((result.get("content") or [{}])[0]).get("text", "")
        shown = ", ".join(f"{k}={v!r}" for k, v in arguments.items()
                          if k != "text")[:34]
        if result.get("isError"):
            print(f"  FAIL {name:<22} {shown}")
            print(f"       {text.splitlines()[0][:70]}")
            failures += 1
            continue
        cap = RESULT_CHARS.get(name, RESULT_CHARS_DEFAULT)
        if len(text) > cap:
            print(f"  FAIL {name:<22} {len(text):,} chars over its "
                  f"{cap:,} cap")
            failures += 1
            continue
        sizes.append((len(text), name, shown))
        first = text.strip().split("\n")[0][:44]
        print(f"  ok   {name:<22} {len(text):>6} chars  {first}")

    # --- the declarations, in both directions ---------------------------
    print()
    missing = sorted({t["name"] for t in TOOLS} - set(HANDLERS))
    if missing:
        print(f"  FAIL declared but not implemented: {missing}")
        failures += len(missing)
    # The reverse was never checked, so a handler nobody declared - which is
    # a tool no agent can reach - passed silently.
    undeclared = sorted(set(HANDLERS) - {t["name"] for t in TOOLS})
    if undeclared:
        print(f"  FAIL implemented but not declared: {undeclared}")
        failures += len(undeclared)
    if not missing and not undeclared:
        print(f"  ok   {len(TOOLS)} tools declared and implemented, both ways")

    # --- the declaration is enforceable --------------------------------
    unenforceable = []
    for tool in TOOLS:
        for prop, spec in ((tool.get("inputSchema") or {})
                           .get("properties") or {}).items():
            for key in spec:
                if key not in validate.SUPPORTED:
                    unenforceable.append(f"{tool['name']}.{prop}: {key}")
    if unenforceable:
        print(f"  FAIL declared and not enforced: {unenforceable}")
        failures += len(unenforceable)
    else:
        print("  ok   every declared schema keyword is enforced")

    # --- the payload we pay for on every turn --------------------------
    blob = json.dumps(TOOLS)
    #: Every turn of every conversation pays for this, so it is a real cost
    #: and not a style rule.
    #:
    #: 20,000 was set at 23 tools. Two were added - resolve_files, which
    #: carries the template-vs-physical-file distinction the whole platform
    #: turns on, and unaccounted, which is the only mechanical check that a
    #: report placed every member it was handed. Both earn their room.
    #:
    #: Raised to 21,000 only AFTER cutting about 1,180 characters of genuine
    #: prose from eight descriptions - justification an agent does not need
    #: at selection time, which belongs in the playbook. Trimming further
    #: would have removed facts a caller uses to pick a tool or its
    #: arguments, and a wrong tool choice costs far more than 4% of a
    #: tools/list. Cut fat before raising this again.
    #:
    #: 22,000 at 27 tools: remember and recall add ticket memory, the only
    #: store that carries what one analysis established into the next. About
    #: 900 characters of justification were cut from six descriptions first,
    #: so the two tools cost roughly half their size.
    #:
    #: Held at 22,000 for 28: program_interface fits by a short description,
    #: and its detail lives in the playbook.
    budget = 22_000
    line = (f"  {'ok  ' if len(blob) <= budget else 'FAIL'} tools/list is "
            f"{len(blob):,} chars (~{len(blob) // 4:,} tokens) of a "
            f"{budget:,} budget")
    print(line)
    if len(blob) > budget:
        failures += 1

    if sizes:
        print()
        print("  LARGEST ANSWERS")
        for n, name, shown in sorted(sizes, reverse=True)[:4]:
            print(f"    {n:>7,} chars  {name:<22} {shown}")

    print()
    print("  all tools working" if not failures else f"  {failures} failure(s)")
    return 1 if failures else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="ALPHAPLUS knowledge base MCP server")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    return serve()


if __name__ == "__main__":
    sys.exit(main())
