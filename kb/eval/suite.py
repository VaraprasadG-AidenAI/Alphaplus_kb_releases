"""
Every automated check, in one command.

    python -m kb.eval.suite                 run them all, print a summary
    python -m kb.eval.suite --quick         skip the ones that index a tree
    python -m kb.eval.suite --list          what would run

This exists so that "did we run the tests" is never a question anybody has to
answer from memory. It is the gate in front of a build: the exe is only worth
shipping if every one of these passed on the code going into it.

Each entry is a command that exits non-zero on failure, so a new check joins
the gate by being added to the list rather than by being remembered.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import List, NamedTuple, Optional, Sequence

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "kb" / "skill-implement" / "scripts" / "source_guard.py"


class Check(NamedTuple):
    name: str
    what: str
    command: List[str]
    heavy: bool = False          # indexes a tree; skipped by --quick


def checks() -> List[Check]:
    python = sys.executable
    return [
        Check("compile", "every module compiles",
              [python, "-m", "compileall", "-q", str(REPO / "kb")]),
        Check("imports", "every shipped module imports",
              [python, "-m", "kb.eval.importcheck"]),
        Check("construct", "the parsers, construct by construct - COBOL, CL, DDS",
              [python, "-m", "kb.eval.parser_fixture"]),
        Check("parser", "the parser self-test",
              [python, "-m", "kb.index.selftest"], heavy=True),
        Check("tools", "every MCP tool answers (28 tools, budget, schemas)",
              [python, "-m", "kb.server.server", "--selftest"], heavy=True),
        Check("sqlite", "every tool answers from SQLite, no driver touched",
              [python, "-m", "kb.eval.toolcheck"], heavy=True),
        Check("coupling", "the coupling fixture - one per fixed defect class",
              [python, "-m", "kb.eval.coupling_fixture"], heavy=True),
        Check("ticket", "the ticket reader: PDF, Jira XML, Word, HTML, hook",
              [python, "-m", "kb.eval.ticket_fixture"], heavy=True),
        Check("guard", "the edit guard: propose, snapshot, check, register, commit",
              [python, "-m", "kb.eval.guard_fixture"]),
        Check("skills", "every shipped skill: structure, references, tool names",
              [python, "-m", "kb.eval.skillcheck"]),
    ]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--quick", action="store_true",
                        help="skip the checks that index a source tree")
    parser.add_argument("--list", action="store_true", dest="show")
    args = parser.parse_args(argv)

    selected = [c for c in checks() if not (args.quick and c.heavy)]
    if args.show:
        for check in selected:
            print(f"  {check.name:<10} {check.what}")
        return 0

    print()
    print(f"  Running {len(selected)} check(s)")
    print("  " + "-" * 72)
    results = []
    for check in selected:
        started = time.time()
        run = subprocess.run(check.command, cwd=str(REPO),
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
        took = time.time() - started
        ok = run.returncode == 0
        results.append((check, ok, took, run.stdout + run.stderr))
        print(f"  {'PASS' if ok else 'FAIL'}  {check.name:<10} {check.what}"
              f"   [{took:.1f}s]")

    failed = [r for r in results if not r[1]]
    for check, _ok, _took, output in failed:
        print()
        print(f"  --- {check.name} ---")
        for line in output.strip().split("\n")[-25:]:
            print("  " + line)

    print("  " + "-" * 72)
    print(f"  {len(results) - len(failed)}/{len(results)} passed in "
          f"{sum(r[2] for r in results):.0f}s")
    if failed:
        print("  The build gate is CLOSED until these pass.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
