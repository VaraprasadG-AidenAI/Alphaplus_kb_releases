"""
Turning facts into a prompt, and a fact sheet into a page.

The important design decision is here rather than in the model call: **the
prompt is built from extracted facts, not from source.**  A program's fact
sheet - the files it opens and how, what it calls, what calls it, its
paragraph names, its screen formats - is about 1.5k tokens where the source is
often 8,000 lines.  That is roughly a twentieth of the cost, and it means the
cheap tier can cover all 13,363 members instead of a favoured few.

It also makes the security conversation much easier.  Paragraph names and file
names are not the same disclosure as shipping the source, and with
ALPHAPLUS_LLM_SOURCE off that is all the model ever sees.

The deep tier reads real source, and is reserved for the members that get
changed most, because those are the ones a ticket lands on.

Every page ends with the same instruction: say what the program does in the
language of the business, and do not invent anything the facts do not support.
"""

from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional

SYSTEM = (
    "You document a 30 year old IBM i (AS/400) wholesale distribution system "
    "written in COBOL, CL, DDS and RPG. Your reader is a developer or a "
    "coding agent who has just been handed a JIRA ticket and needs to know, "
    "in ten seconds, whether this member is where the problem lives.\n\n"
    "Rules:\n"
    "- Lead with what it does in business terms. No preamble.\n"
    "- Only state what the facts support. If something is unclear, say so.\n"
    "- Never invent a file, field, program or business rule.\n"
    "- Short sentences. No marketing language. No bullet-point padding.\n"
    "- British spelling."
)

TEMPLATE = (
    "Write a short page about this member, using exactly these headings:\n\n"
    "## What it does\n"
    "Two or three sentences, in business language.\n\n"
    "## When you would come here\n"
    "The kinds of ticket that land on this member. Two or three lines.\n\n"
    "## Watch out for\n"
    "Anything in the facts that would trip somebody up - a file opened I-O "
    "that the name does not suggest, an override pointing somewhere "
    "unexpected, a call that cannot be resolved, dead paragraphs. Omit the "
    "heading entirely if there is nothing worth saying.\n\n"
    "Do not restate the fact sheet. The reader has it.\n\n"
    "---\n{sheet}"
)


def _rows(db: sqlite3.Connection, sql: str, *args) -> List[sqlite3.Row]:
    return list(db.execute(sql, args))


def fact_sheet(db: sqlite3.Connection, name: str,
               source: Optional[str] = None) -> str:
    """Everything the knowledge base knows about one member, as text.

    This is also useful on its own - with no model configured at all, this is
    what gets written to the page.
    """
    db.row_factory = sqlite3.Row
    out: List[str] = []

    obj = db.execute(
        "SELECT name, member_type, subsystem, lines, path "
        "FROM object WHERE name=?",
        (name,)).fetchone()
    if not obj:
        return ""
    out.append(f"member    {obj['name']}  ({obj['member_type']}, "
               f"{obj['lines'] or 0:,} lines)")
    if obj["subsystem"]:
        out.append(f"subsystem {obj['subsystem']}")

    files = _rows(db, "SELECT file, mode, verbs, confidence FROM file_usage "
                      "WHERE program=? ORDER BY file", name)
    if files:
        out.append("\nfiles it opens")
        for r in files:
            note = "" if r["confidence"] == "exact" else f"  [{r['confidence']}]"
            out.append(f"  {r['file']:<12}{r['mode'] or '?':<8}"
                       f"{(r['verbs'] or '')[:34]}{note}")

    calls = _rows(db, "SELECT callee, kind, confidence FROM call_edge "
                      "WHERE caller=? ORDER BY callee", name)
    if calls:
        out.append("\nit calls")
        for r in calls:
            note = "" if r["confidence"] == "exact" else f"  [{r['confidence']}]"
            out.append(f"  {r['callee']:<12}{r['kind'] or ''}{note}")

    dynamic = _rows(db, "SELECT variable, line FROM call_candidate "
                        "WHERE caller=? LIMIT 8", name)
    if dynamic:
        out.append("\ncalls through a variable (target not known statically)")
        for r in dynamic:
            out.append(f"  {r['variable']}  at line {r['line']}")

    callers = _rows(db, "SELECT DISTINCT caller FROM call_edge WHERE callee=? "
                        "ORDER BY caller LIMIT 20", name)
    if callers:
        out.append("\ncalled by")
        out.append("  " + ", ".join(r["caller"] for r in callers))

    overrides = _rows(db, "SELECT logical_name, target_file, library, "
                          "confidence FROM override WHERE cl_program=?", name)
    if overrides:
        out.append("\noverrides it sets (these decide what really gets opened)")
        for r in overrides:
            library = f"{r['library']}/" if r["library"] else ""
            note = "" if r["confidence"] == "exact" else f"  [{r['confidence']}]"
            out.append(f"  {r['logical_name']:<12}-> "
                       f"{library}{r['target_file']}{note}")

    formats = _rows(db, "SELECT DISTINCT device_file, format_name, verb "
                        "FROM format_usage WHERE program=? LIMIT 20", name)
    if formats:
        out.append("\nscreen or report formats it writes")
        for r in formats:
            out.append(f"  {r['device_file']}.{r['format_name']}  {r['verb']}")

    paragraphs = _rows(db, "SELECT name FROM paragraph WHERE member=? "
                           "ORDER BY line LIMIT 60", name)
    if paragraphs:
        out.append("\nparagraphs, in order")
        out.append("  " + ", ".join(r["name"] for r in paragraphs))

    #: The paragraph graph, not just the list.  A list of sixty names says the
    #: program has sixty paragraphs; "MAIN-LINE performs INITIALIZE then
    #: PROCESS-SCREEN then TERMINATE" says what it does, in the same tokens.
    #: In-line PERFORMs are excluded - they are loops, not edges.
    structure = _rows(db, "SELECT from_para, to_para, is_loop, in_loop "
                          "FROM perform_edge WHERE member=? AND inline=0 "
                          "AND to_para<>'' ORDER BY line LIMIT 120", name)
    if structure:
        out.append("\nwhat performs what")
        grouped: Dict[str, List[str]] = {}
        for r in structure:
            target = r["to_para"]
            if r["is_loop"] or r["in_loop"]:
                target += " (repeatedly)"
            bucket = grouped.setdefault(r["from_para"] or "(entry)", [])
            if target not in bucket:
                bucket.append(target)
        # `performer`, not `source`: this loop used to rebind the `source`
        # parameter, so by the time the source section below tested it, it held
        # the last paragraph name instead of the member's code. Every deep-tier
        # member with a non-inline PERFORM got a paragraph name under
        # "--- source ---" and no source at all.
        for performer, targets in grouped.items():
            out.append(f"  {performer} -> " + ", ".join(targets[:10]))

    fields = _rows(db, "SELECT name, dtype, length, text FROM field "
                       "WHERE owner=? AND text<>'' LIMIT 40", name)
    if fields:
        out.append("\nfields")
        for r in fields:
            out.append(f"  {r['name']:<12}{r['dtype'] or '':<3}"
                       f"{r['length'] or '':<5}{r['text']}")

    unresolved = _rows(db, "SELECT kind, raw, reason FROM unresolved "
                           "WHERE member=? LIMIT 10", name)
    if unresolved:
        out.append("\nthings we could not work out")
        for r in unresolved:
            out.append(f"  {r['kind']}: {(r['raw'] or '')[:44]}  "
                       f"({r['reason']})")

    if source:
        out.append("\n--- source ---")
        out.append(source)

    return "\n".join(out)


def build_prompt(sheet: str) -> str:
    return TEMPLATE.format(sheet=sheet)


# ===========================================================================
# The pyramid: menu, family, library
# ===========================================================================
#
# Three templates, and the word caps are not stylistic. Prefill is the
# expensive half on CPU - 23.9 tokens/sec measured against 10 for decode - so a
# page's length is a tax on every parent that reads it. The arithmetic that
# sets them:
#
#   menu    <=110 words   read 9-11 at a time by a family roll-up
#   family  <=180 words   eleven of them must fit one library prompt (~2,600 tok)
#   library <=250 words   21 libraries x 250 words is ~7k tokens, so the whole
#                         application fits in one agent's context beside real work
#
# Every template forbids going beyond the sheet. That is not boilerplate: asked
# to summarise APMAIN, this model opened with "allows users to manage pricing"
# for a menu with no pricing option. The sheet is also embedded verbatim under
# the prose in the written page, so a reader can always check.

#: The rules that survived a first pass, each one earned.
#:
#: Given a free hand, this model called a menu of SYS/AUD/SEC/UTL programs "an
#: accounting system", said all twelve options called programs when the sheet
#: plainly showed one signing off, invented the program name SEC105C for an
#: option that runs UTL200C, and filled a sentence with "No unknown elements
#: present." Every clause below exists to stop one of those.
_HONESTY = (
    "Rules:\n"
    "- Use only the facts below. Never name a file, program or menu that does "
    "not appear in them.\n"
    "- Do not count things, and do not summarise what all the options have in "
    "common. Getting a count wrong is worse than omitting it.\n"
    "- Do not judge whether a label matches what the option runs. You cannot "
    "tell, and a label is often abbreviated.\n"
    "- Do not report defects. Something else looks for those.\n"
    "- Do not say that something is absent, correct, consistent or "
    "unremarkable. Say nothing instead.\n"
    "- Where the facts are thin, write less. Do not pad."
)

MENU_TEMPLATE = (
    "Below is one menu: its display source, its command list, and for each "
    "option the text a user sees and what that option runs.\n\n"
    "Write at most 110 words of plain prose. No headings, no preamble, no "
    "lists. Cover:\n"
    "- what part of the business this menu is for, in the words a user would "
    "use;\n"
    "- the kinds of ticket that would land on it.\n\n"
    "Do not comment on individual options at all. Describe the whole menu.\n\n"
    + _HONESTY + "\n\n---\n{sheet}"
)

FAMILY_TEMPLATE = (
    "Below is a group of related menus and a short description of each, plus "
    "the files their programs can change.\n\n"
    "Write at most 180 words, no headings, no preamble: what area of the "
    "business this group of menus covers, what it does to which data, and "
    "which menu a reader should start from for what kind of question.\n\n"
    "Describe the area, not the list. Do not restate each menu in turn - the "
    "reader has them.\n\n"
    + _HONESTY + "\n\n---\n{sheet}"
)

LIBRARY_TEMPLATE = (
    "Below is one source library of a wholesale distribution application: "
    "what it is made of, the menu areas it owns, how it is entered from other "
    "libraries, and its most written-to files.\n\n"
    "Write at most 250 words under exactly these headings:\n\n"
    "## What lives here\n"
    "What part of the business this library holds.\n\n"
    "## How you get into it\n"
    "Which menus, or - if it owns none - what reaches it from elsewhere.\n\n"
    "## What to watch\n"
    "What a reader should know before changing anything in it.\n\n"
    "If this library owns no menus, say so plainly. It means nothing in it is "
    "reached directly by a user, which is a fact about the library worth "
    "stating, not an omission to work around.\n\n"
    + _HONESTY + "\n\n---\n{sheet}"
)

#: scope -> (template, output word cap, max_tokens to allow the model)
#:
#: max_tokens is roughly two tokens per word plus room to finish a sentence.
#: The member tier's 900 was never overridden and is far more than a 110-word
#: menu page needs; a tighter budget is a directly shorter wait on CPU.
TEMPLATES = {
    "menu": (MENU_TEMPLATE, 110, 260),
    "family": (FAMILY_TEMPLATE, 180, 400),
    "library": (LIBRARY_TEMPLATE, 250, 560),
}


def level_prompt(scope: str, sheet: str, source: str = "") -> str:
    """The user prompt for one level of the pyramid."""
    template, _cap, _budget = TEMPLATES[scope]
    body = sheet
    if source:
        body = f"{sheet}\n\n--- source ---\n{source}"
    return template.format(sheet=body)


def facts_only_page(name: str, sheet: str) -> str:
    """The page written when no model is configured.

    Deliberately not apologetic about it.  A fact sheet with evidence is more
    use on a ticket than a paragraph of generated prose, and it is the only
    thing we can write if the client will not let code leave the network.
    """
    return (f"# {name}\n\n"
            f"_Facts only - no model configured. "
            f"Everything below is read from source._\n\n"
            f"```\n{sheet}\n```\n")
