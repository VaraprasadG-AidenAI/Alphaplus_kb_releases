"""
Guard regression fixture: the checks an implementation report's claims rest on.

    python -m kb.eval.guard_fixture              build, run, check
    python -m kb.eval.guard_fixture --keep DIR   keep the tree

The implementation skill promises a report that names, for every changed line,
the library, the member, the line number, the text before and the text after -
and that each new line sits in the columns its format requires. That promise is
only worth what `scripts/source_guard.py` can actually prove, so every command
it offers is exercised here against a member built for the purpose:

    line       a proposed line, before it is written
    marks      what convention the member already follows
    ruler      the columns of a line, as the compiler counts them
    snapshot   the original, kept for rollback
    check      what changed, and whether it is still valid
    diff       the patch a reviewer reads beside the report

Dev-only: kb.eval is excluded from the build.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, Tuple

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "kb" / "skill-implement" / "scripts" / "source_guard.py"
LIB = "CATLIB"

#: A physical-file member in the shop's own style: 'A' in column 6, the field
#: name from column 19, the length right-justified in 30-34, and a change
#: history block at the top.
DDS_BEFORE = [
    "     A*%%TS  SD  20240102  120000  ALPHA       REL-V9R7M0",
    "     A*",
    "     A*   CARRIER MASTER",
    "     A*",
    "     A*   12.01.24  SAR1001  first version",
    "     A*",
    "     A          R CARMASR",
    "     A            CARCMP         3A         TEXT('Carrier company')",
    "     A            CARNUM         7P 0       TEXT('Carrier number')",
    "     A            CARNAM        30A         TEXT('Carrier name')",
    "     A          K CARCMP",
    "     A          K CARNUM",
]

COBOL_BEFORE = [
    "      *****************************************************************",
    "      *   CARRIER PRINT                                               *",
    "      *****************************************************************",
    "       IDENTIFICATION DIVISION.",
    "       PROGRAM-ID. CARPRT.",
    "       DATA DIVISION.",
    "       WORKING-STORAGE SECTION.",
    "       01  WS-CARRIER.",
    "           05  WS-CARCMP          PIC X(3).",
    "           05  WS-CARNAM          PIC X(30).",
    "       PROCEDURE DIVISION.",
    "           MOVE CARCMP TO WS-CARCMP.",
    "           GOBACK.",
]


def git(root: Path, *args: str) -> str:
    """Read the repository the guard made, with git and nothing else."""
    run = subprocess.run(["git", "-C", str(root)] + list(args),
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace")
    return (run.stdout or "") + (run.stderr or "")


def guard(*args: str) -> Tuple[int, str]:
    run = subprocess.run([sys.executable, str(GUARD), *args],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace")
    return run.returncode, run.stdout + run.stderr


def write(path: Path, lines: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("cp1252"))


Check = Tuple[str, str, Callable[[], bool]]


def checks(root: Path) -> List[Check]:
    tree = root / "src"
    work = root / "work"
    dds = tree / LIB / "QDDSSRC" / "CARMAS.PF"
    cbl = tree / LIB / "QCBLLESRC" / "CARPRT.CBLLE"
    write(dds, DDS_BEFORE)
    write(cbl, COBOL_BEFORE)

    # The tree already carries rules of somebody else's. They must survive:
    # a client's .gitignore replaced by ours is their rule silently dropped.
    (tree / ".gitignore").write_text("# the client's own\n*.log\n",
                                     encoding="utf-8")

    # ---- before the edit: is the proposed line valid? --------------------
    good = guard("line", "--kind", "dds",
                 "     A            CARCMP         5A         TEXT('Carrier company')")
    bad_columns = guard("line", "--kind", "dds",
                        "     A            CARCMP        5A          TEXT('x')")
    too_long = guard("line", "--kind", "cobol",
                     "           MOVE CARCMP TO WS-CARCMP-LONG-NAME-THAT-RUNS-ON AND ON AND ON.")
    tabbed = guard("line", "--kind", "cobol", "\tMOVE A TO B.")
    marked = guard("line", "--kind", "cobol", "--mark", "SAR2002",
                   "           MOVE CARCMP TO WS-CARCMP.                                     SAR2002")
    mark_missing = guard("line", "--kind", "cobol", "--mark", "SAR2002",
                         "           MOVE CARCMP TO WS-CARCMP.")

    # ---- what convention does the member already follow? -----------------
    marks_out = guard("marks", str(dds))
    ruler_out = guard("ruler", str(dds), "8")

    # ---- snapshot, edit, check ------------------------------------------
    snap = guard("snapshot", "--work", str(work), "--root", str(tree),
                 str(dds), str(cbl))
    original_kept = (work / "originals" / LIB / "QDDSSRC" / "CARMAS.PF").exists()

    after_dds = list(DDS_BEFORE)
    after_dds[7] = ("     A            CARCMP         5A         "
                    "TEXT('Carrier company')").ljust(72) + "SAR2002"
    after_dds.insert(5, "     A*   12.03.26  SAR2002  CARCMP widened to 5")
    write(dds, after_dds)

    after_cbl = list(COBOL_BEFORE)
    after_cbl[8] = "           05  WS-CARCMP          PIC X(5).".ljust(72) + "SAR2002"
    write(cbl, after_cbl)

    clean = guard("check", "--work", str(work), "--root", str(tree))

    # ---- an edit that breaks the format is caught ------------------------
    broken = list(after_dds)
    broken[8] = "     A            CARCMP        5A"          # length misaligned
    write(dds, broken)
    caught = guard("check", "--work", str(work), "--root", str(tree))
    write(dds, after_dds)                                      # put it back

    # ---- an edit past column 72 is caught --------------------------------
    long_cbl = list(after_cbl)
    long_cbl[11] = ("           MOVE CARCMP TO WS-CARCMP AND THEN SOME MORE TEXT "
                    "RUNNING PAST THE EDGE.")
    write(cbl, long_cbl)
    caught_long = guard("check", "--work", str(work), "--root", str(tree))
    write(cbl, after_cbl)

    # ---- the patch a reviewer reads --------------------------------------
    patch = root / "change.patch"
    diffed = guard("diff", "--work", str(work), "--root", str(tree),
                   "--out", str(patch))
    patch_text = patch.read_text(encoding="utf-8") if patch.exists() else ""

    # ---- the register: the report's change table, built by the tool -------
    register = guard("register", "--work", str(work), "--root", str(tree))
    register_csv = guard("register", "--work", str(work), "--root", str(tree),
                         "--format", "csv")

    # ---- verify: everything a finished edit owes, in one command ---------
    verified = guard("verify", "--work", str(work), "--root", str(tree),
                     "--out", str(root / "verify.patch"), "--no-reindex")

    # ---- version control: the source tree's own history ------------------
    # `snapshot` above ran without --no-git, so the tree is a repository and
    # the pre-edit state is its first commit; `verify` has just committed the
    # change on top. Both are read back through git itself, not through the
    # guard's own output, because the claim being tested is that a reviewer
    # with nothing but `git` can see the change.
    log = git(tree, "log", "--oneline")
    show = git(tree, "show", "HEAD", "--", "CATLIB/QDDSSRC/CARMAS.PF")
    history = guard("history", str(dds), "--root", str(tree))
    ignored = (tree / ".gitignore").read_text(encoding="utf-8") \
        if (tree / ".gitignore").exists() else ""
    remotes = git(tree, "remote", "-v")

    # A failing check must stop before the index is touched, because an index
    # rebuilt from source that does not pass makes a broken edit look true.
    write(dds, broken)
    refused = guard("verify", "--work", str(work), "--root", str(tree))
    write(dds, after_dds)

    return [
        ("PROPOSE", "a correct DDS line passes before it is written",
         lambda: good[0] == 0 and "PASS" in good[1]),
        ("PROPOSE", "a misaligned length is refused, naming the columns",
         lambda: bad_columns[0] == 1 and "30-34" in bad_columns[1]),
        ("PROPOSE", "a COBOL line past column 72 is refused",
         lambda: too_long[0] == 1 and "72" in too_long[1]),
        ("PROPOSE", "a tab is refused",
         lambda: tabbed[0] == 1 and "tab" in tabbed[1]),
        ("PROPOSE", "the change mark in columns 73-80 is checked",
         lambda: marked[0] == 0 and mark_missing[0] == 1
         and "73-80" in mark_missing[1]),
        ("CONVENTION", "the member's own marks and history are reported",
         lambda: marks_out[0] == 0 and "SAR1001" in marks_out[1]),
        ("CONVENTION", "the ruler shows the columns of a line",
         lambda: ruler_out[0] == 0 and "CARCMP" in ruler_out[1]),
        ("SNAPSHOT", "the original is copied before any edit",
         lambda: snap[0] == 0 and original_kept),
        ("CHECK", "a correct edit passes, and the changed lines are seen",
         lambda: clean[0] == 0 and "0 FAIL" in clean[1]
         and "CARMAS.PF" in clean[1] and "1 modified, 1 added" in clean[1]),
        ("CHECK", "a misaligned DDS length is caught after the edit too",
         lambda: caught[0] == 1 and "30-34" in caught[1]),
        ("CHECK", "text running past column 72 is caught",
         lambda: caught_long[0] == 1 and "72" in caught_long[1]),
        ("DIFF", "the patch carries both members, before and after",
         lambda: diffed[0] == 0
         and "CARMAS.PF" in patch_text and "CARPRT.CBLLE" in patch_text
         and "-     A            CARCMP         3A" in patch_text
         and "+     A            CARCMP         5A" in patch_text),
        ("DIFF", "the added history line is in the patch",
         lambda: "+     A*   12.03.26  SAR2002" in patch_text),
        ("REGISTER", "one row per changed line, with library, member and line",
         lambda: register[0] == 0
         and "| CATLIB | QDDSSRC | CARMAS | PF | 6 | add beneath |"
         in register[1]
         and "| CATLIB | QDDSSRC | CARMAS | PF | 9 | modify in place |"
         in register[1]
         and "| CATLIB | QCBLLESRC | CARPRT | CBLLE | 9 | modify in place |"
         in register[1]),
        ("REGISTER", "the row carries the line before and the line after",
         lambda: "A            CARCMP         3A" in register[1]
         and "A            CARCMP         5A" in register[1]
         and "SAR2002" in register[1]),
        ("REGISTER", "the counts a report must reconcile are printed",
         lambda: "3 changed line(s) across 2 member(s)" in register[1]
         and "1 add beneath" in register[1]
         and "2 modify in place" in register[1]),
        ("VERBS", "the register uses the shop's words, not a diff tool's",
         lambda: "modified" not in register[1] and "removed" not in register[1]
         and "deleted" not in register[1].lower().replace("deleting", "")),
        ("REGISTER", "csv for a spreadsheet, one header and one row per line",
         lambda: register_csv[0] == 0
         and register_csv[1].splitlines()[0].startswith("library,source_file")
         and len([r for r in register_csv[1].splitlines() if r.strip()]) == 4),
        ("VERIFY", "one command checks, registers, commits and patches",
         lambda: verified[0] == 0
         and "STEP 1  CHECK" in verified[1]
         and "STEP 2  CHANGE REGISTER" in verified[1]
         and "STEP 3  COMMIT" in verified[1]
         and "STEP 3b  PATCH FILE" in verified[1]
         and (root / "verify.patch").exists()),
        ("VERIFY", "a failing check stops before the index is rebuilt",
         lambda: refused[0] == 1
         and "RE-INDEX" not in refused[1]
         and "index was NOT refreshed" in refused[1]),
        ("GIT", "the source tree became a local repository",
         lambda: (tree / ".git").is_dir()),
        ("GIT", "the state before the edit is a commit, so there is a baseline",
         lambda: "baseline before" in log),
        ("GIT", "the change is a commit of its own, named for the mark",
         lambda: "SAR2002" in log.splitlines()[0]),
        ("GIT", "git show gives the before and after without a patch file",
         lambda: "-     A            CARCMP         3A" in show
         and "+     A            CARCMP         5A" in show),
        ("GIT", "history lists every commit that touched the member",
         lambda: history[0] == 0
         and "baseline before" in history[1] and "SAR2002" in history[1]),
        ("GIT", "every generated folder is ignored, under any of its names",
         lambda: all(rule in ignored for rule in
                     ("_alphaplus-kb/", "alphaplus-kb/", ".alphaplus-kb/",
                      ".alphaplus-work/", "*.patch"))),
        ("GIT", "git itself ignores them - not just the file that says so",
         lambda: "_alphaplus-kb" in git(tree, "check-ignore", "-v",
                                        "_alphaplus-kb/facts.db")
         and "alphaplus-work" in git(tree, "check-ignore", "-v",
                                     ".alphaplus-work/T.notes.md")),
        ("GIT", "rules the tree already had are kept, not replaced",
         lambda: "# the client's own" in ignored and "*.log" in ignored),
        ("GIT", "nothing generated reaches the commit",
         lambda: "_alphaplus-kb" not in git(tree, "log", "--name-only")
         and ".alphaplus-work" not in git(tree, "log", "--name-only")),
        ("GIT", "no remote was added - nothing can be pushed",
         lambda: remotes.strip() == ""),
        ("GIT", "a failing check is not committed",
         lambda: "Not committing" in refused[1]),
    ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", help="build in this folder and keep it")
    args = parser.parse_args(argv)
    work = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="guard-"))
    try:
        results = []
        for layer, name, test in checks(work):
            try:
                ok = bool(test())
            except Exception as exc:                          # noqa: BLE001
                ok = False
                name += f"  ({type(exc).__name__}: {exc})"
            results.append(ok)
            print(f"{'PASS' if ok else 'FAIL'}  {layer:<10} {name}")
        failed = results.count(False)
        print(f"\n{len(results) - failed}/{len(results)} passed")
        return 1 if failed else 0
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
