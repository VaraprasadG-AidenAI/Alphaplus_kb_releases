"""
Every shipped skill, checked mechanically: structure, references, tool names, no
client or replica names.

    python -m kb.eval.skillcheck

A skill is instructions, so nothing compiles it and nothing fails when it
drifts. These are the drifts that have actually happened: a skill pointing at
a reference file that is not installed, and a skill naming a tool the server
does not register - both of which fail silently, at the moment somebody wants
a report.

Dev-only: kb.eval is excluded from the build.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path
from typing import List

REPO = Path(__file__).resolve().parents[2]
SKILLS = ((REPO / "kb" / "skill", "alphaplus-impact"),
          (REPO / "kb" / "skill-implement", "alphaplus-implement"),
          (REPO / "kb" / "skill-report", "alphaplus-implement-report"))

#: Words shaped like a member name that are the platform's own vocabulary.
_PLATFORM = {"PSTKEY", "CHGPF", "OVRDBF", "DSPOBJD", "CRTPF", "QDDSSRC",
             "QCBLLESRC", "QCLSRC", "QCPYSRC", "QSQLSRC", "SQLCBLLE",
             "CBLLE", "DSPF", "PRTF", "MNUDDS", "MNUCMD", "RPGLE", "LVLCHK",
             "SHARE", "CPYF", "RNMF", "DCLF", "SNDRCVF", "SEU", "CDIF"}


def main() -> int:
    problems: List[str] = []
    for folder, name in SKILLS:
        problems += _one_skill(folder, name)
    print()
    if problems:
        print("PROBLEMS")
        for problem in problems:
            print("  " + problem)
        return 1
    print(f"{len(SKILLS)} skill(s): no problems")
    return 0


def _one_skill(folder: Path, name: str) -> List[str]:
    problems: List[str] = []
    skill_md = folder / "SKILL.md"
    if not skill_md.exists():
        return [f"{name}: SKILL.md is missing"]

    text = skill_md.read_text(encoding="utf-8")
    head = re.match(r"^---\nname: ([a-z0-9-]+)\ndescription: (.+?)\n---\n",
                    text, re.S)
    if not head:
        problems.append(f"{name}: frontmatter missing or malformed")
    else:
        if head.group(1) != name:
            problems.append(f"{name}: frontmatter says {head.group(1)}")
        if len(head.group(2)) > 1024:
            problems.append(f"{name}: description over 1024 characters")

    files = sorted(p for p in folder.rglob("*")
                   if p.suffix in (".md", ".py") and "__pycache__" not in p.parts)
    texts = {p: p.read_text(encoding="utf-8") for p in files}
    prose = "\n".join(t for p, t in texts.items() if p.suffix == ".md")
    blob = "\n".join(texts.values())
    print(f"{name}: {len(files)} file(s), "
          f"{sum(len(t.splitlines()) for t in texts.values()):,} lines")

    # The output contract, near the top where it is read in time. It has
    # drifted to the bottom of a long file before, and a skill that states its
    # deliverable on line 218 publishes an artifact on line 40.
    head = "\n".join(text.splitlines()[:60])
    if "Output contract" not in head:
        problems.append(f"{name}: no output contract in the first 60 lines - "
                        f"the deliverable must be stated before the work is")
    for phrase in ("markdown file", "project root"):
        if phrase not in head:
            problems.append(f"{name}: the output contract does not say "
                            f"\"{phrase}\"")
    if "artifact" not in head.lower():
        problems.append(f"{name}: the output contract does not rule out "
                        f"publishing an artifact")

    # every referenced file exists
    for ref in sorted(set(re.findall(r"`(references/[a-z-]+\.md)`", blob))
                      | set(re.findall(r"`(scripts/[a-z_]+\.py)`", blob))
                      | set(re.findall(r"`(IMPACT-REPORT\.md)`", blob))):
        # A skill may point at the other skill's file - the implementation
        # skill reads the impact report - so either folder resolves it.
        if not any((home / ref).exists() for home, _ in SKILLS):
            problems.append(f"{name}: names {ref}, which is in neither skill")

    # every tool the prose tells you to call is one the server registers
    from kb.server import server as srv
    registered = {t["name"] for t in srv.TOOLS}
    for tool in sorted(set(re.findall(r"`([a-z][a-z_]{3,})\([a-z_\"']", prose))):
        if tool not in registered:
            problems.append(f"{name}: names a tool the server does not "
                            f"register: {tool}()")

    # guard commands the prose tells you to run must exist
    guard_py = folder / "scripts" / "source_guard.py"
    if guard_py.exists():
        guard = texts.get(guard_py, guard_py.read_text(encoding="utf-8"))
        for command in sorted(set(re.findall(r"source_guard\.py (\w[\w-]*)",
                                             prose))):
            if f'add_parser("{command}"' not in guard:
                problems.append(f"{name}: tells you to run "
                                f"`source_guard.py {command}`, which the script "
                                f"does not have")

    # every CLI command the prose tells you to run must exist
    from kb import cli
    cli_commands = set(re.findall(r'add_parser\(\s*"([a-z][a-z-]*)"',
                                  Path(cli.__file__).read_text(encoding="utf-8")))
    for command in sorted(set(re.findall(r"alphaplus-kb ([a-z][a-z-]*)", prose))):
        if command not in cli_commands:
            problems.append(f"{name}: tells you to run `alphaplus-kb "
                            f"{command}`, which the CLI does not have")

    # no client or replica names, no ticket references
    database = REPO / "APPSRC" / "_alphaplus-kb" / "facts.db"
    if database.exists():
        connection = sqlite3.connect(str(database))
        members = {row[0].upper() for row in
                   connection.execute("SELECT name FROM object")}
        connection.close()
        words = set(re.findall(r"\b[A-Z][A-Z0-9#@$]{3,9}\b", blob)) - _PLATFORM
        for hit in sorted(words & members):
            problems.append(f"{name}: replica member name in a shipped file: "
                            f"{hit}")
    for ticket in re.findall(r"(?i)\b(?:alphaplus|jira|sar)[- ]?\d{2,}", blob):
        problems.append(f"{name}: ticket reference in a shipped file: {ticket}")
    return problems


if __name__ == "__main__":
    sys.exit(main())
