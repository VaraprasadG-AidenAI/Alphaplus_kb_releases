#!/usr/bin/env python3
"""
Guard rails for editing IBM i source exported to a Windows folder.

    marks     FILE                      encoding, line endings, longest line,
                                        change marks in columns 73-80 and the
                                        change-history lines of one member
    ruler     FILE LINE [--count N]     lines with a column ruler above them
    snapshot  --work DIR --root ROOT FILE [FILE ...]
                                        copy originals into DIR, and commit
                                        them to the tree's local git repo as
                                        the baseline, before editing
    check     --work DIR                compare every snapshotted file with its
                                        current state; exit 1 on any FAIL
    diff      --work DIR --out FILE     unified diff of every snapshotted file
    line      TEXT --kind KIND          check one proposed line before writing
    register  --work DIR                one row per changed line, for the report
    verify    --work DIR                check, register, commit and re-index,
                                        in one command - what a finished edit
                                        owes.  --out FILE also writes a patch;
                                        --no-git, --no-reindex to skip a step.
    history   FILE --root ROOT          every commit that touched one member

Version control is a LOCAL git repository in the source tree: `git init`, a
baseline commit of each member before it is edited, and a commit of the
change afterwards. Nothing is cloned, fetched, pushed or given a remote, and
only the touched members are committed - the estate is too large to hold
whole. `git show`, `git log` and `git revert` then work on the source, which
is what a patch file could never give.

It edits no source line of its own. `snapshot` writes into --work, `diff` to
--out, `verify` runs `alphaplus-kb update` (which rewrites the index, not the
source) and commits. Standard library only; Python 3.8 or later.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

MANIFEST = "manifest.json"
ORIGINALS = "originals"

_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_DATE = re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b")

#: Characters the system's EBCDIC code pages cannot hold, which editors and
#: word processors like to substitute for plain ASCII.
#: Quotes, dashes, ellipsis, bullet and non-breaking space.
_TYPOGRAPHIC = set("\u2018\u2019\u201a\u201c\u201d\u201e\u2013\u2014\u2026"
                   "\u2022\u00a0")
_REPLACEMENT = "\ufffd"

_COBOL_DIRS = {"QCBLLESRC", "QCBLSRC", "QCPYSRC", "QLBLSRC"}
_COBOL_EXTS = {"CBL", "CBLLE", "SQLCBLLE", "SQLCBL", "CPY", "CBLCPY"}
_DDS_DIRS = {"QDDSSRC"}
_DDS_EXTS = {"PF", "LF", "DSPF", "PRTF", "MNUDDS", "ICFF"}
_DEVICE_EXTS = {"DSPF", "PRTF", "MNUDDS"}


# ===========================================================================
# Reading
# ===========================================================================

def _os_path(path: Path) -> str:
    """A path Windows can open even when it is longer than 260 characters."""
    text = os.path.abspath(str(path))
    if os.name != "nt" or text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def read_bytes(path: Path) -> bytes:
    with open(_os_path(path), "rb") as handle:
        return handle.read()


def detect_encoding(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    try:
        raw.decode("ascii")
        return "ascii"
    except UnicodeDecodeError:
        pass
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1252"


def detect_eol(raw: bytes) -> str:
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    cr = raw.count(b"\r") - crlf
    kinds = [name for name, n in (("CRLF", crlf), ("LF", lf), ("CR", cr)) if n]
    if not kinds:
        return "none"
    return kinds[0] if len(kinds) == 1 else "mixed"


def split_lines(text: str) -> List[str]:
    lines = _LINE_BREAK.split(text)
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def decode(raw: bytes, encoding: str) -> str:
    return raw.decode(encoding if encoding != "ascii" else "cp1252",
                      errors="replace")


def describe(raw: bytes) -> Dict[str, object]:
    encoding = detect_encoding(raw)
    lines = split_lines(decode(raw, encoding))
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "encoding": encoding,
        "eol": detect_eol(raw),
        "final_newline": raw.endswith((b"\n", b"\r")),
        "lines": len(lines),
        "max_len": max((len(line.expandtabs(8)) for line in lines), default=0),
        "tabs": sum(line.count("\t") for line in lines),
        # Marks on code lines only: comment text that happens to reach column
        # 73 is not evidence of a marking convention.
        "has_marks": any(mark_of(line) for line in lines if not _is_comment(line)),
    }


def is_binary(raw: bytes) -> bool:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    return b"\x00" in raw[:8192]


def mark_of(line: str) -> str:
    """Columns 73-80, where change marks live.

    Text that runs straight across from column 72 into 73 - a long comment,
    a row of dashes - is the line's own text overflowing, not a mark.
    """
    text = line.expandtabs(8)
    if len(text) <= 72:
        return ""
    if text[71] != " " and text[72] != " ":
        return ""
    return text[72:80].strip()


def source_kind(relpath: str) -> str:
    """cobol, dds, device (display/printer/menu DDS) or other."""
    parts = relpath.replace("\\", "/").split("/")
    folder = parts[-2].upper() if len(parts) > 1 else ""
    ext = parts[-1].rpartition(".")[2].upper() if "." in parts[-1] else ""
    if ext in _DEVICE_EXTS:
        return "device"
    if ext in _DDS_EXTS or folder in _DDS_DIRS:
        return "dds"
    if ext in _COBOL_EXTS or folder in _COBOL_DIRS:
        return "cobol"
    return "other"


# ===========================================================================
# marks and ruler
# ===========================================================================

def _is_comment(line: str) -> bool:
    text = line.expandtabs(8)
    return (len(text) > 6 and text[6] in "*/") or "/*" in text[:72]


def cmd_marks(args) -> int:
    path = Path(args.file)
    raw = read_bytes(path)
    if is_binary(raw):
        print(f"{path}: binary, not source text")
        return 1
    info = describe(raw)
    lines = split_lines(decode(raw, str(info["encoding"])))
    print(f"{path}")
    print(f"  encoding       {info['encoding']}")
    print(f"  line endings   {info['eol']}   final newline: "
          f"{'yes' if info['final_newline'] else 'no'}")
    print(f"  lines          {info['lines']:,}")
    print(f"  longest line   {info['max_len']} characters")
    print(f"  tabs           {info['tabs']}")
    print(f"  kind           {source_kind('/'.join(path.parts[-2:]))}")

    for title, on_comments in (("code lines", False), ("comment lines", True)):
        first_seen: Dict[str, int] = {}
        counts: Dict[str, int] = {}
        for number, line in enumerate(lines, 1):
            if _is_comment(line) != on_comments:
                continue
            mark = mark_of(line)
            if mark:
                counts[mark] = counts.get(mark, 0) + 1
                first_seen.setdefault(mark, number)
        marked = sum(counts.values())
        print(f"\n  change marks in columns 73-80 on {title}: {len(counts)} "
              f"distinct value(s) on {marked:,} line(s)")
        if on_comments and counts:
            print("    (may be comment text that reaches column 73, not marks)")
        if not counts:
            continue
        ordered = sorted(counts, key=lambda m: first_seen[m])
        shown = ordered[-25:]
        if len(ordered) > len(shown):
            print(f"    ... {len(ordered) - len(shown)} earlier value(s) not shown")
        print("    mark      lines  first line")
        for mark in shown:
            print(f"    {mark:<8}  {counts[mark]:>5}  {first_seen[mark]:>10}")
        widths = sorted({len(m) for m in counts})
        print(f"    widths in use: {', '.join(str(w) for w in widths)}; "
              f"latest: {lines[first_seen[shown[-1]] - 1].rstrip()!r}")

    history = [(n, l) for n, l in enumerate(lines[:200], 1)
               if _is_comment(l) and _DATE.search(l)]
    print(f"\n  change-history lines (comments with a date, first 200 lines): "
          f"{len(history)}")
    shown = history if len(history) <= 6 else history[:3] + [None] + history[-3:]
    for item in shown:
        if item is None:
            print("      ...")
        else:
            print(f"    {item[0]:>5} | {item[1].rstrip()}")
    return 0


def _ruler(prefix: int, width: int) -> List[str]:
    tens = "".join(str((i // 10) % 10) if i % 10 == 0 else " "
                   for i in range(1, width + 1))
    units = "".join(str(i % 10) for i in range(1, width + 1))
    return [" " * prefix + tens, " " * prefix + units]


def cmd_ruler(args) -> int:
    path = Path(args.file)
    raw = read_bytes(path)
    lines = split_lines(decode(raw, detect_encoding(raw)))
    start = max(1, args.line)
    end = min(len(lines), start + max(1, args.count) - 1)
    if start > len(lines):
        print(f"{path} has {len(lines)} lines")
        return 1
    width = max(80, max(len(lines[i - 1].expandtabs(8)) for i in range(start, end + 1)))
    prefix = 8
    for text in _ruler(prefix, width):
        print(text)
    for number in range(start, end + 1):
        line = lines[number - 1]
        shown = line.replace("\t", "<TAB>")
        print(f"{number:>6} |{shown}|   ({len(line.expandtabs(8))} chars"
              + (f", mark '{mark_of(line)}'" if mark_of(line) else "") + ")")
    return 0


# ===========================================================================
# snapshot
# ===========================================================================

def _load_manifest(work: Path) -> Dict[str, object]:
    path = work / MANIFEST
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"root": "", "created": "", "files": {}}


def _save_manifest(work: Path, manifest: Dict[str, object]) -> None:
    work.mkdir(parents=True, exist_ok=True)
    (work / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n",
                                 encoding="utf-8")


def _relative(path: Path, root: Path) -> Optional[str]:
    try:
        rel = os.path.relpath(os.path.abspath(str(path)), os.path.abspath(str(root)))
    except ValueError:
        return None
    if rel == ".." or rel.startswith(".." + os.sep) or os.path.isabs(rel):
        return None
    return rel.replace("\\", "/")


# ===========================================================================
# Version control - a local git repository, and nothing else
# ===========================================================================
#
# Why here, and why local only.
#
# A patch file is a photograph of one change. It answers "what did this
# ticket do" and nothing else: not "what did the last four tickets do to this
# member", not "what did this line look like before any of them", and not
# "put it back". Those are the questions asked weeks later, and a folder of
# .patch files answers none of them.
#
# So: `git init` in the source tree, a commit of each touched member as it
# was BEFORE the edit, and a commit of it after. The history is then real -
# `git log` per member, `git show` for any ticket, `git revert` to undo one.
#
# Three limits, deliberately:
#
#   Local only. No remote is added, nothing is cloned, nothing is fetched or
#   pushed, ever. If the tree already has a remote, this says so and still
#   does not touch it. The client's own version control is theirs.
#
#   Only the touched members are committed. The real estate is tens of
#   gigabytes; committing all of it would double the disk and take an hour to
#   say nothing. A member enters the repository the first time a ticket
#   touches it, carrying its pre-edit content as its first commit, which is
#   the baseline that matters. `--git-all` overrides this for a small tree.
#
#   Identity is passed per command, never written. No `git config --global`,
#   no change to the machine.

#: Everything the knowledge base generates inside the source tree.
#: All of it is derived from the source and rebuilt by `alphaplus-kb update`,
#: so none of it belongs in version control - and `_alphaplus-kb/` alone is
#: hundreds of megabytes, which is enough to make a repository useless.
_GITIGNORE = """# Written by the Alphaplus knowledge base.
#
# Everything below is GENERATED from the source and rebuilt on demand by
# `alphaplus-kb update`. None of it is source, none of it is worth a commit,
# and the index alone runs to hundreds of megabytes.

# The index, built during parsing: facts.db, the lexicon, the codebase map,
# exports, logs. Whatever the data folder is called, it is derived.
_alphaplus-kb/
alphaplus-kb/
.alphaplus-kb/

# Per-ticket working files: the parsed ticket, the notes, the snapshots.
.alphaplus-work/

# Patch files, when one is written for somebody who cannot be given the repo.
*.patch

# Windows and editor litter, which has no business in a source extraction.
Thumbs.db
desktop.ini
*.bak
*~
"""

_AUTHOR = ("-c", "user.name=Alphaplus implementation guard",
           "-c", "user.email=alphaplus-kb@localhost",
           "-c", "commit.gpgsign=false",
           "-c", "core.autocrlf=false",
           "-c", "core.safecrlf=false")


def _git(root: Path, *args: str) -> Tuple[int, str]:
    """Run one git command inside the source tree. Never touches a network."""
    import subprocess
    try:
        run = subprocess.run(("git", "-C", str(root)) + _AUTHOR + args,
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=1800)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return run.returncode, (run.stdout or "") + (run.stderr or "")


def git_available() -> bool:
    import shutil
    return shutil.which("git") is not None


def _git_repo_root(root: Path) -> str:
    """The repository this folder is in, or "" - which may be a PARENT's."""
    code, out = _git(root, "rev-parse", "--show-toplevel")
    return out.strip() if code == 0 else ""


def git_prepare(root: Path) -> bool:
    """Make sure the source tree is a local git repository. True if usable.

    A repository that is already there is used as it is - including one the
    client made. Only a tree that has none gets `git init`.
    """
    if not git_available():
        print("SKIPPED  git is not on PATH, so there is no version control "
              "for this change.")
        print("         Install git, or use `diff --out` for a patch file.")
        return False

    existing = _git_repo_root(root)
    if existing:
        print(f"repo    {existing}")
        code, out = _git(root, "remote", "-v")
        if code == 0 and out.strip():
            first = out.strip().splitlines()[0].split()
            print(f"        this repository has a remote ({first[1] if len(first) > 1 else '?'}).")
            print("        Nothing is pushed, fetched or cloned - commits stay "
                  "on this machine.")
    else:
        code, out = _git(root, "init", "-q")
        if code != 0:
            print(f"SKIPPED  could not `git init` in {root}: {out.strip()[:200]}")
            return False
        print(f"repo    created a local repository in {root}")

    write_gitignore(root)
    return True


def write_gitignore(root: Path) -> None:
    """Make sure every generated folder is ignored, without losing any rule.

    Appends rather than overwrites. A tree may already carry a .gitignore -
    the client's, or one from an earlier version of this file - and replacing
    it would quietly drop rules somebody meant.
    """
    ignore = root / ".gitignore"
    try:
        existing = (ignore.read_text(encoding="utf-8", errors="replace")
                    if ignore.exists() else "")
    except OSError as exc:
        print(f"        could not read .gitignore: {exc}")
        return

    present = {line.strip() for line in existing.splitlines()}
    wanted = [line for line in _GITIGNORE.splitlines()
              if line.strip() and not line.startswith("#")]
    missing = [line for line in wanted if line not in present]
    if not missing:
        return

    try:
        if not existing:
            ignore.write_text(_GITIGNORE, encoding="utf-8")
            print(f"        wrote .gitignore ({len(wanted)} rule(s): the "
                  f"index and the work folder stay out of the history)")
            return
        lead = "" if existing.endswith("\n") else "\n"
        block = (lead + "\n# Added by the Alphaplus knowledge base - "
                 "generated files.\n" + "\n".join(missing) + "\n")
        with open(_os_path(ignore), "a", encoding="utf-8") as handle:
            handle.write(block)
        print(f"        added {len(missing)} rule(s) to the existing "
              f".gitignore: {', '.join(missing[:4])}"
              + (" ..." if len(missing) > 4 else ""))
    except OSError as exc:
        print(f"        could not write .gitignore: {exc}")


def git_commit(root: Path, paths: Sequence[str], message: str,
               all_files: bool = False) -> str:
    """Commit the named files. Returns the commit id, or "" if nothing to do."""
    if all_files:
        code, out = _git(root, "add", "-A")
    else:
        code, out = _git(root, "add", "--", *paths) if paths else (0, "")
    if code != 0:
        print(f"        git add failed: {out.strip()[:200]}")
        return ""

    code, out = _git(root, "diff", "--cached", "--name-only")
    if code != 0 or not out.strip():
        print("        nothing to commit - the files are already as recorded")
        return ""
    staged = [line for line in out.strip().splitlines() if line.strip()]

    code, out = _git(root, "commit", "-q", "-m", message)
    if code != 0:
        print(f"        git commit failed: {out.strip()[:200]}")
        return ""
    commit = _git(root, "rev-parse", "--short", "HEAD")[1].strip()
    print(f"commit  {commit}  {message}")
    for name in staged[:12]:
        print(f"        {name}")
    if len(staged) > 12:
        print(f"        ... and {len(staged) - 12} more")
    return commit


def cmd_history(args) -> int:
    """Every commit that touched a member, newest first.

    The question a patch file cannot answer: which tickets have been through
    this member, and what did each of them do.
    """
    root = Path(args.root)
    if not git_available() or not _git_repo_root(root):
        print(f"No local repository in {root} - nothing to show.")
        return 1
    code, out = _git(root, "log", "--follow",
                     "--pretty=format:%h  %ad  %s", "--date=short",
                     "--", args.file)
    if code != 0:
        print(out.strip()[:400])
        return 1
    if not out.strip():
        print(f"{args.file}: no commit has touched it.")
        return 0
    print(f"{args.file}")
    for line in out.strip().splitlines():
        print("  " + line)
    print()
    print("  git show <id> -- <file>       what one commit did")
    print("  git revert <id>               undo it, as a new commit")
    return 0


def cmd_snapshot(args) -> int:
    work = Path(args.work)
    root = Path(args.root)
    if not root.is_dir():
        print(f"FAIL  --root is not a folder: {root}")
        return 1
    manifest = _load_manifest(work)
    if manifest["root"] and os.path.normcase(os.path.abspath(str(manifest["root"]))) \
            != os.path.normcase(os.path.abspath(str(root))):
        print(f"FAIL  this work folder belongs to another source tree: {manifest['root']}")
        return 1
    manifest["root"] = os.path.abspath(str(root))
    manifest["created"] = manifest["created"] or _dt.datetime.now().isoformat(timespec="seconds")
    files: Dict[str, Dict[str, object]] = manifest["files"]  # type: ignore[assignment]
    failed = 0
    for name in args.files:
        path = Path(name)
        if not path.is_absolute():
            path = root / path
        rel = _relative(path, root)
        if rel is None:
            print(f"FAIL  not inside the source tree: {path}")
            failed += 1
            continue
        try:
            raw = read_bytes(path)
        except OSError as error:
            print(f"FAIL  cannot read {rel}: {error.strerror or error}")
            failed += 1
            continue
        if is_binary(raw):
            print(f"FAIL  {rel} is binary - not source, not editable here")
            failed += 1
            continue
        if rel in files:
            current = hashlib.sha256(raw).hexdigest()
            state = "unchanged since" if current == files[rel]["sha256"] else "CHANGED since"
            print(f"kept  {rel}  (snapshot from {files[rel]['snapshot_at']} kept; "
                  f"file {state} it)")
            continue
        target = work / ORIGINALS / rel
        os.makedirs(os.path.dirname(_os_path(target)), exist_ok=True)
        with open(_os_path(target), "wb") as handle:
            handle.write(raw)
        entry = describe(raw)
        entry["snapshot_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        entry["kind"] = source_kind(rel)
        files[rel] = entry
        print(f"saved {rel}  ({entry['lines']:,} lines, {entry['encoding']}, "
              f"{entry['eol']}, longest {entry['max_len']})")
    _save_manifest(work, manifest)
    print(f"\nwork folder: {work.resolve()}  ({len(files)} file(s) in the snapshot)")

    # The baseline commit: every touched member as it is NOW, before an edit.
    # Without it the first commit of a member would be the edited version and
    # the history would start with the change rather than with what was there.
    if not getattr(args, "no_git", False):
        print()
        if git_prepare(root):
            git_commit(root, sorted(files),
                       f"baseline before {args.mark or 'this change'}: "
                       f"{len(files)} member(s) as they were",
                       all_files=getattr(args, "git_all", False))
    return 1 if failed else 0


# ===========================================================================
# check
# ===========================================================================

def _changed_columns(old: str, new: str) -> str:
    a, b = old.expandtabs(8), new.expandtabs(8)
    width = max(len(a), len(b))
    a, b = a.ljust(width), b.ljust(width)
    ranges: List[List[int]] = []          # 1-based, inclusive
    for i in range(width):
        if a[i] == b[i]:
            continue
        column = i + 1
        if ranges and ranges[-1][1] == column - 1:
            ranges[-1][1] = column
        else:
            ranges.append([column, column])
    return ", ".join(str(s) if s == e else f"{s}-{e}" for s, e in ranges) or "none"


def _format_problems(line: str, kind: str) -> List[str]:
    """Fixed-format column sanity for one changed or added line."""
    text = line.expandtabs(8)
    found: List[str] = []
    if kind == "cobol":
        if len(text) > 6 and text[6] not in " *-/Dd":
            found.append(f"column 7 is {text[6]!r}; COBOL allows blank, *, /, - or D")
        if len(text) > 72 and text[6:7] not in ("*", "/") and text[71] != " " and text[72] != " ":
            found.append("text runs from column 72 into 73 - code past column 72 is not compiled")
    elif kind in ("dds", "device"):
        if len(text) < 6 or text[5] not in "Aa":
            found.append("column 6 is not 'A'")
            return found
        if len(text) > 6 and text[6] == "*":
            return found
        padded = text.ljust(80)
        if padded[16] not in " RKSOJrksoj":
            found.append(f"column 17 is {padded[16]!r}; expected blank, R, K, S, O or J")
        if not re.fullmatch(r"\S* *", padded[18:28]):
            found.append(f"name in columns 19-28 is not left-justified: {padded[18:28]!r}")
        if not re.fullmatch(r" *\d*", padded[29:34]):
            found.append(f"length in columns 30-34 is not right-justified digits: {padded[29:34]!r}")
        if padded[34] != " " and not padded[34].isalpha():
            found.append(f"data type in column 35 is {padded[34]!r}")
        if not re.fullmatch(r" *\d*", padded[35:37]):
            found.append(f"decimals in columns 36-37 are not right-justified digits: {padded[35:37]!r}")
        if padded[37] not in " BIOHMPbiohmp":
            found.append(f"usage in column 38 is {padded[37]!r}; expected blank, B, I, O, H, M or P")
        if kind == "device":
            if not re.fullmatch(r" *\+?\d*", padded[38:41]):
                found.append(f"line in columns 39-41 is not a number: {padded[38:41]!r}")
            if not re.fullmatch(r" *\+?\d*", padded[41:44]):
                found.append(f"position in columns 42-44 is not a number: {padded[41:44]!r}")
    return found


def check_file(rel: str, entry: Dict[str, object], root: Path, work: Path
               ) -> Tuple[List[str], List[str], List[str], Dict[str, int]]:
    fails: List[str] = []
    warns: List[str] = []
    notes: List[str] = []
    counts = {"modified": 0, "added": 0, "removed": 0}
    path = root / rel
    try:
        new_raw = read_bytes(path)
    except OSError as error:
        return [f"cannot read the current file: {error.strerror or error}"], [], [], counts
    old_raw = read_bytes(work / ORIGINALS / rel)
    if hashlib.sha256(old_raw).hexdigest() != entry["sha256"]:
        fails.append("the snapshot copy no longer matches its recorded hash")
    if new_raw == old_raw:
        notes.append("not changed")
        return fails, warns, notes, counts

    old = describe(old_raw)
    new = describe(new_raw)
    old_enc, new_enc = str(old["encoding"]), str(new["encoding"])
    if old_enc != new_enc:
        if old_enc == "ascii" and new_enc in ("utf-8", "cp1252"):
            warns.append(f"was pure ASCII, now contains non-ASCII text read as {new_enc}")
        else:
            fails.append(f"encoding changed: {old_enc} -> {new_enc}")
    if old["eol"] != new["eol"]:
        fails.append(f"line endings changed: {old['eol']} -> {new['eol']}")
    if old["final_newline"] != new["final_newline"]:
        fails.append("final newline " + ("removed" if old["final_newline"] else "added"))
    if int(new["tabs"]) > int(old["tabs"]):
        fails.append(f"tabs introduced: {int(new['tabs']) - int(old['tabs'])}")

    old_lines = split_lines(decode(old_raw, old_enc))
    new_lines = split_lines(decode(new_raw, new_enc))
    limit = int(old["max_len"]) if int(old["max_len"]) > 80 else 80
    known_chars = set("".join(old_lines))
    kind = str(entry.get("kind") or source_kind(rel))
    marks_used: Dict[str, int] = {}

    def inspect(number: int, line: str, old_line: Optional[str]) -> None:
        width = len(line.expandtabs(8))
        where = f"line {number}"
        if width > limit:
            fails.append(f"{where}: {width} characters, longer than the member allows ({limit})")
        new_chars = {c for c in line if ord(c) > 127 and c not in known_chars}
        typographic = new_chars & _TYPOGRAPHIC
        if typographic:
            fails.append(f"{where}: typographic character(s) the system cannot hold: "
                         + " ".join(f"U+{ord(c):04X}" for c in sorted(typographic)))
        elif new_chars:
            warns.append(f"{where}: new non-ASCII character(s): "
                         + " ".join(f"{c!r}" for c in sorted(new_chars)))
        if _REPLACEMENT in line:
            fails.append(f"{where}: contains a replacement character - text was damaged by re-encoding")
        for problem in _format_problems(line, kind):
            fails.append(f"{where}: {problem}")
        mark = mark_of(line)
        if mark:
            marks_used[mark] = marks_used.get(mark, 0) + 1
        elif old["has_marks"] and line.strip():
            warns.append(f"{where}: no change mark in columns 73-80, and the member uses marks")
        if old_line is not None:
            columns = _changed_columns(old_line, line)
            notes.append(f"{where}: modified, columns {columns}")
            a, b = old_line.expandtabs(8).ljust(6), line.expandtabs(8).ljust(6)
            if a[:6] != b[:6]:
                warns.append(f"{where}: sequence area (columns 1-6) changed")
        else:
            notes.append(f"{where}: added")

    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace" and (i2 - i1) == (j2 - j1):
            for offset in range(i2 - i1):
                counts["modified"] += 1
                inspect(j1 + offset + 1, new_lines[j1 + offset], old_lines[i1 + offset])
            continue
        if tag in ("replace", "delete"):
            for i in range(i1, i2):
                counts["removed"] += 1
                notes.append(f"old line {i + 1}: removed  {old_lines[i].rstrip()[:60]!r}")
        if tag in ("replace", "insert"):
            for j in range(j1, j2):
                counts["added"] += 1
                inspect(j + 1, new_lines[j], None)

    if len(marks_used) > 1:
        warns.append("more than one change mark on the changed lines: "
                     + ", ".join(f"{m} ({n})" for m, n in sorted(marks_used.items())))
    if marks_used:
        notes.append("marks on changed lines: "
                     + ", ".join(f"{m} ({n})" for m, n in sorted(marks_used.items())))
    return fails, warns, notes, counts


def cmd_check(args) -> int:
    work = Path(args.work)
    manifest = _load_manifest(work)
    files: Dict[str, Dict[str, object]] = manifest["files"]  # type: ignore[assignment]
    if not files:
        print(f"FAIL  no snapshot in {work} - run snapshot before editing")
        return 1
    root = Path(args.root) if args.root else Path(str(manifest["root"]))
    total_fail = total_warn = 0
    rows = []
    for rel in sorted(files):
        fails, warns, notes, counts = check_file(rel, files[rel], root, work)
        status = "FAIL" if fails else ("WARN" if warns else "PASS")
        if notes == ["not changed"]:
            status = "SAME" if not fails else status
        total_fail += len(fails)
        total_warn += len(warns)
        rows.append((rel, status, counts))
        print(f"\n{status}  {rel}   (kind {files[rel].get('kind', source_kind(rel))}, "
              f"{counts['modified']} modified, {counts['added']} added, "
              f"{counts['removed']} removed)")
        for text in fails:
            print(f"   FAIL  {text}")
        for text in warns:
            print(f"   WARN  {text}")
        for text in notes:
            print(f"         {text}")

    print("\nSUMMARY")
    print(f"  {'file':<60}  status  modified  added  removed")
    for rel, status, counts in rows:
        print(f"  {rel[-60:]:<60}  {status:<6}  {counts['modified']:>8}  "
              f"{counts['added']:>5}  {counts['removed']:>7}")
    print(f"\n  {total_fail} FAIL, {total_warn} WARN across {len(rows)} file(s)")
    return 1 if total_fail else 0


# ===========================================================================
# diff
# ===========================================================================

def cmd_diff(args) -> int:
    work = Path(args.work)
    manifest = _load_manifest(work)
    files: Dict[str, Dict[str, object]] = manifest["files"]  # type: ignore[assignment]
    if not files:
        print(f"no snapshot in {work}")
        return 1
    root = Path(args.root) if args.root else Path(str(manifest["root"]))
    out: List[str] = []
    summary = []
    for rel in sorted(files):
        old_raw = read_bytes(work / ORIGINALS / rel)
        try:
            new_raw = read_bytes(root / rel)
        except OSError as error:
            summary.append((rel, "unreadable: " + str(error.strerror or error)))
            continue
        if old_raw == new_raw:
            summary.append((rel, "unchanged"))
            continue
        old_lines = split_lines(decode(old_raw, detect_encoding(old_raw)))
        new_lines = split_lines(decode(new_raw, detect_encoding(new_raw)))
        chunk = list(difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{rel}",
                                          tofile=f"b/{rel}", lineterm="", n=3))
        added = sum(1 for l in chunk if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in chunk if l.startswith("-") and not l.startswith("---"))
        summary.append((rel, f"+{added} -{removed}"))
        out += chunk
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
    for rel, state in summary:
        print(f"  {state:<14} {rel}")
    print(f"\n{len(out)} diff line(s) -> {target.resolve()}")
    print("The patch is UTF-8; members keep their own encoding on disk.")
    return 0


# ===========================================================================

def _member_of(rel: str) -> Tuple[str, str, str, str]:
    """(library, source file, member, type) from a relative path.

    `SOMELIB/QDDSSRC/XYZMAS.PF` -> SOMELIB, QDDSSRC, XYZMAS, PF. A tree that
    nests deeper keeps the last three parts, which is where the library,
    source file and member always are on an IBM i extraction.
    """
    parts = rel.replace("\\", "/").split("/")
    name = parts[-1] if parts else rel
    member, _dot, suffix = name.partition(".")
    source_file = parts[-2] if len(parts) >= 2 else ""
    library = parts[-3] if len(parts) >= 3 else ""
    return library, source_file, member.upper(), suffix.upper()


def _register_rows(work: Path, root: Path) -> List[Dict[str, object]]:
    """One row per changed line: what it was, what it is, and where.

    Built from the snapshot, not from memory: the report's change register is
    the one table a reviewer checks line by line against the diff, so it is
    produced by comparing bytes rather than written by hand.
    """
    manifest = _load_manifest(work)
    files = manifest.get("files") or {}
    rows: List[Dict[str, object]] = []
    for rel in sorted(files):
        entry = files[rel]
        original = work / "originals" / rel
        current = root / rel
        if not original.exists() or not current.exists():
            continue
        old_raw, new_raw = read_bytes(original), read_bytes(current)
        encoding = str(entry.get("encoding") or detect_encoding(old_raw))
        old_lines = split_lines(decode(old_raw, encoding))
        new_lines = split_lines(decode(new_raw, detect_encoding(new_raw)))
        library, source_file, member, kind = _member_of(rel)
        matcher = difflib.SequenceMatcher(None, old_lines, new_lines,
                                          autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            if tag == "replace":
                for offset in range(max(i2 - i1, j2 - j1)):
                    before = old_lines[i1 + offset] if i1 + offset < i2 else ""
                    after = new_lines[j1 + offset] if j1 + offset < j2 else ""
                    action = _action_of(before, after, kind)
                    rows.append({
                        "library": library, "source_file": source_file,
                        "member": member, "type": kind,
                        "line": (j1 + offset + 1) if after else (i1 + offset + 1),
                        "action": action, "before": before, "after": after,
                        "mark": mark_of(after) if after else "",
                        "path": rel})
            elif tag == "insert":
                for offset in range(j2 - j1):
                    after = new_lines[j1 + offset]
                    rows.append({
                        "library": library, "source_file": source_file,
                        "member": member, "type": kind, "line": j1 + offset + 1,
                        "action": _action_of("", after, kind),
                        "before": "", "after": after,
                        "mark": mark_of(after), "path": rel})
            elif tag == "delete":
                for offset in range(i2 - i1):
                    rows.append({
                        "library": library, "source_file": source_file,
                        "member": member, "type": kind, "line": i1 + offset + 1,
                        "action": _action_of(old_lines[i1 + offset], "", kind),
                        "before": old_lines[i1 + offset],
                        "after": "", "mark": "", "path": rel})
    return rows


def _action_of(before: str, after: str, kind: str) -> str:
    """What was done to this line, in the words a maintained shop uses.

    A diff says modified, added, removed. Those are a tool's words, and two of
    them describe things this platform's shops do not do. A line is switched
    off - a `*` in column 7, the text kept and readable - or a new line is
    added beneath the one it supersedes, or a literal is changed in place.

    Worked out from the bytes rather than asked for, so the register cannot
    disagree with the file. A line that really was deleted is reported as
    `DELETED`, in capitals, because on this platform that is usually a
    mistake: the history goes with it.
    """
    # A blank line that came or went is a real change and stays in the
    # register - but shown as two empty cells it reads as a phantom row, so it
    # says what it is. It also must not raise the DELETED warning, which is
    # about losing the text of a statement, not a spacer.
    if not before.strip() and not after.strip():
        return ("blank line removed" if before and not after else
                "blank line added" if after and not before else
                "blank line changed")
    if before and not after:
        return "DELETED"
    if after and not before:
        return "add beneath"
    if _is_comment(after) and not _is_comment(before):
        # The same text, now commented, is the switch-off idiom. Compare the
        # text itself so a rewritten comment is not mistaken for one.
        if _uncommented(after).strip().startswith(before.strip()[:24].strip()):
            return "switch off"
        return "switch off and change"
    if _is_comment(before) and not _is_comment(after):
        return "switch back on"
    return "modify in place"


def _uncommented(line: str) -> str:
    """A commented COBOL line as its text, so two lines can be compared."""
    text = line.expandtabs(8)
    if len(text) > 6 and text[6] in "*/":
        return text[:6] + " " + text[7:]
    return text


def _cell(text: str, width: int = 84) -> str:
    """A source line inside a markdown table cell, readable and unambiguous."""
    shown = text.replace("\t", "<TAB>").rstrip()
    if not shown:
        return "-"
    if len(shown) > width:
        shown = shown[:width - 3] + "..."
    return "`" + shown.replace("|", "\\|") + "`"


def cmd_register(args) -> int:
    """Print the change register: one row per changed line.

    This is section 2.1 of the implementation report. It is printed rather
    than described so the report carries what the files actually say - a
    register typed from memory and a diff that disagrees with it is the one
    defect a reviewer cannot forgive.
    """
    work = Path(args.work)
    manifest = _load_manifest(work)
    root = Path(args.root or str(manifest.get("root") or ""))
    if not root.is_dir():
        print(f"FAIL  --root is not a folder: {root}")
        return 1
    rows = _register_rows(work, root)
    if args.format == "csv":
        print("library,source_file,member,type,line,action,before,after,mark")
        for row in rows:
            fields = [str(row[k]).replace('"', '""') for k in
                      ("library", "source_file", "member", "type", "line",
                       "action", "before", "after", "mark")]
            print(",".join('"%s"' % f for f in fields))
        return 0

    if not rows:
        print("No line changed against the snapshot.")
        return 0
    print("| # | Library | Source file | Member | Type | Line | Action | "
          "Before | After | Mark |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for number, row in enumerate(rows, 1):
        print("| %d | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            number, row["library"] or "-", row["source_file"] or "-",
            row["member"], row["type"] or "-", row["line"], row["action"],
            _cell(str(row["before"])), _cell(str(row["after"])),
            row["mark"] or "-"))
    members = sorted({(r["library"], r["member"]) for r in rows})
    tally: Dict[str, int] = {}
    for row in rows:
        verb = str(row["action"])
        tally[verb] = tally.get(verb, 0) + 1
    print()
    print(f"{len(rows)} changed line(s) across {len(members)} member(s): "
          + ", ".join(f"{n} {verb}" for verb, n in sorted(tally.items())) + ".")
    print("These counts belong in the report's change register and in the "
          "header's status block; if they disagree, the report is wrong.")

    deleted = tally.get("DELETED", 0)
    if deleted:
        print()
        print(f"WARN  {deleted} line(s) were DELETED rather than switched "
              f"off.")
        print("      On this platform a superseded line keeps its text with a "
              "`*` in column 7,")
        print("      so the next reader can see what it used to do and which "
              "change stopped it.")
        print("      Deleting is correct only when the whole member is being "
              "retired.")

    # A supersession is a pair. One half alone leaves both statements live,
    # the later one wins, and the fix does nothing with nothing failing - the
    # single most expensive shape a correct analysis can ship.
    by_member: Dict[str, List[Dict[str, object]]] = {}
    for row in rows:
        by_member.setdefault(str(row["member"]), []).append(row)
    lonely = []
    for name, group in sorted(by_member.items()):
        adds = [r for r in group if r["action"] == "add beneath"]
        offs = [r for r in group if str(r["action"]).startswith("switch off")]
        if adds and not offs:
            lonely.append((name, len(adds)))
    if lonely:
        print()
        for name, count in lonely:
            print(f"NOTE  {name}: {count} line(s) added, none switched off.")
        print("      If any of them supersedes a statement that is still "
              "live, both run and the")
        print("      later one wins. Check each target is assigned once in "
              "its paragraph, and")
        print("      say so in the report.")
    return 0


def _reindex(root: Path) -> None:
    """Re-read the changed source into the knowledge base.

    The tools answer from an index, and after an edit the index still holds
    the source as it was: `describe_file` returns the old layout, and it is
    right about the code it was given and wrong about the code on disk. That
    is a silent wrong answer, which is the worst kind, so the re-index is part
    of finishing an edit rather than a step somebody remembers.

    Only what changed is re-read, so this costs a second on a small change.
    A Claude session that is already open needs no restart - it reads the
    index again on its next call.
    """
    import shutil
    import subprocess

    exe = shutil.which("alphaplus-kb")
    command = ([exe, "update"] if exe else
               [sys.executable, "-m", "kb", "update"])
    try:
        run = subprocess.run(command, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=1800,
                             cwd=str(root) if root.is_dir() else None)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"SKIPPED  could not run `alphaplus-kb update`: {exc}")
        print("         The index still holds the source as it was before "
              "the edit. Say so in section 5.6 of the report.")
        return
    for line in (run.stdout or run.stderr or "").strip().splitlines():
        if line.strip():
            print("  " + line)
    if run.returncode != 0:
        print(f"FAIL  `alphaplus-kb update` exited {run.returncode}. Until it "
              f"succeeds every tool answers from the source as it was.")


def cmd_verify(args) -> int:
    """Everything owed after an edit, in one command.

    check, then the register, then the patch, then the re-index - in that
    order, because a register printed from source that fails the check would
    describe a change nobody can compile, and an index rebuilt from source
    that fails the check would make a broken edit look like the truth.
    """
    print("=" * 78)
    print("STEP 1  CHECK - every snapshotted file against its current state")
    print("=" * 78)
    failed = cmd_check(args)

    print()
    print("=" * 78)
    print("STEP 2  CHANGE REGISTER - section 2.1 of the report")
    print("=" * 78)
    register_args = argparse.Namespace(work=args.work, root=args.root,
                                       format="md")
    cmd_register(register_args)

    root = Path(args.root) if args.root else Path(
        str(_load_manifest(Path(args.work)).get("root") or Path.cwd()))

    if not getattr(args, "no_git", False):
        print()
        print("=" * 78)
        print("STEP 3  COMMIT - the change, in the source tree's own history")
        print("=" * 78)
        if failed:
            print("Not committing: the check did not pass. A commit of source "
                  "that fails the guard is a change somebody will trust.")
        elif git_prepare(root):
            rows = _register_rows(Path(args.work), root)
            mark = next((str(r["mark"]) for r in rows if r["mark"]), "")
            members = sorted({str(r["member"]) for r in rows})
            subject = (f"{mark or 'change'}: {len(rows)} line(s) in "
                       f"{', '.join(members[:4])}"
                       + (" and others" if len(members) > 4 else ""))
            commit = git_commit(root, sorted({str(r["path"]) for r in rows}),
                                subject,
                                all_files=getattr(args, "git_all", False))
            if commit:
                print()
                print(f"        git show {commit}            the whole change")
                print(f"        git revert {commit}          undo it")
                print(f"        git log --oneline -- <member>  a member's "
                      f"history")
                print(f"        Cite {commit} in the report; it replaces the "
                      f"patch file.")

    if args.out:
        print()
        print("=" * 78)
        print("STEP 3b  PATCH FILE - only because --out was given")
        print("=" * 78)
        diff_args = argparse.Namespace(work=args.work, root=args.root,
                                       out=args.out)
        cmd_diff(diff_args)
    if failed:
        print()
        print("The check FAILED. Fix the source, run verify again, and only "
              "then write the report.")
        print("The index was NOT refreshed: it does not get a change that "
              "does not pass.")
        return failed

    if not getattr(args, "no_reindex", False):
        print()
        print("=" * 78)
        print("STEP 4  RE-INDEX - so the tools answer from the edited source")
        print("=" * 78)
        _reindex(Path(args.root) if args.root else Path.cwd())
    return failed


def cmd_line(args) -> int:
    """Check one line of proposed text against the rules for its member type.

    The other commands compare a file with its snapshot, which can only be run
    AFTER the edit. A change is proposed before it is made, and a proposal
    that will not compile is worth catching then - so this takes the literal
    text of the line and answers the same questions: does it sit in the right
    columns, is it short enough, is every character in the system's code page,
    and does it carry a change mark.

        source_guard.py line --kind dds  "     A            CARCMP  5A"
        source_guard.py line --kind cobol --mark "<mark>" "           MOVE ..."
    """
    kind = args.kind.lower()
    if kind not in ("cobol", "dds", "device", "cl", "sql", "other"):
        print(f"FAIL  unknown --kind {args.kind!r}")
        return 1

    text = args.text
    problems: List[str] = []
    warnings: List[str] = []

    if "\t" in text:
        problems.append("the line contains a tab; IBM i source uses spaces")
    expanded = text.expandtabs(8)
    limit = int(args.width)
    if len(expanded) > limit:
        problems.append(f"the line is {len(expanded)} characters, past column {limit}")
    outside = sorted({c for c in text if ord(c) > 127})
    if outside:
        warnings.append("characters outside ASCII: "
                        + " ".join(f"{c!r} (U+{ord(c):04X})" for c in outside[:6]))
    problems.extend(_format_problems(text, kind))

    if args.mark:
        found = mark_of(text)
        if found != args.mark:
            problems.append(
                f"columns 73-80 hold {found or '(blank)'!r}, not the change "
                f"mark {args.mark!r}")

    for line in _ruler(0, max(limit, len(expanded))):
        print(line)
    print(expanded.replace("\t", "<TAB>"))
    print(f"  {len(expanded)} characters"
          + (f", mark {mark_of(text)!r}" if mark_of(text) else ", no change mark"))
    for text_problem in warnings:
        print(f"  WARN  {text_problem}")
    for text_problem in problems:
        print(f"  FAIL  {text_problem}")
    if not problems:
        print("  PASS  the line sits in the columns its format requires")
    return 1 if problems else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Guard rails for editing IBM i source. Reads the source "
                    "tree; writes only to --work and --out.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("marks", help="encoding, line endings, change marks and history of a member")
    p.add_argument("file")
    p.set_defaults(run=cmd_marks)

    p = sub.add_parser("ruler", help="lines with a column ruler")
    p.add_argument("file")
    p.add_argument("line", type=int)
    p.add_argument("--count", type=int, default=1)
    p.set_defaults(run=cmd_ruler)

    p = sub.add_parser("line", help="check one proposed line before writing it")
    p.add_argument("text", help="the line exactly as it would be written")
    p.add_argument("--kind", required=True,
                   help="cobol | dds | device | cl | sql | other")
    p.add_argument("--mark", help="the change mark that must sit in columns 73-80")
    p.add_argument("--width", default=80, help="longest allowed line (default 80)")
    p.set_defaults(run=cmd_line)

    p = sub.add_parser("snapshot", help="copy originals into the work folder before editing")
    p.add_argument("--work", required=True)
    p.add_argument("--root", required=True, help="the source tree the files are in")
    p.add_argument("files", nargs="+")
    p.add_argument("--mark", default="",
                   help="the change mark, used in the baseline commit message")
    p.add_argument("--no-git", action="store_true",
                   help="do not create or use a local repository")
    p.add_argument("--git-all", action="store_true",
                   help="commit the whole tree, not only the touched members")
    p.set_defaults(run=cmd_snapshot)

    p = sub.add_parser("check", help="verify every snapshotted file against its current state")
    p.add_argument("--work", required=True)
    p.add_argument("--root", help="defaults to the tree recorded at snapshot")
    p.set_defaults(run=cmd_check)

    p = sub.add_parser("register",
                       help="one row per changed line, for the report")
    p.add_argument("--work", required=True)
    p.add_argument("--root", help="defaults to the tree recorded at snapshot")
    p.add_argument("--format", choices=("md", "csv"), default="md")
    p.set_defaults(run=cmd_register)

    p = sub.add_parser("verify",
                       help="check, register, commit and re-index, in one command")
    p.add_argument("--work", required=True)
    p.add_argument("--root", help="defaults to the tree recorded at snapshot")
    p.add_argument("--out", help="also write a patch file, for a reviewer "
                                 "who cannot be given the repository")
    p.add_argument("--no-git", action="store_true",
                   help="do not commit; the change then has no history")
    p.add_argument("--git-all", action="store_true",
                   help="commit the whole tree, not only the touched members")
    p.add_argument("--no-reindex", action="store_true",
                   help="do not run `alphaplus-kb update` afterwards; the "
                        "tools then keep answering from the old source")
    p.set_defaults(run=cmd_verify)

    p = sub.add_parser("history",
                       help="every commit that touched one member")
    p.add_argument("file", help="path of the member, inside the source tree")
    p.add_argument("--root", required=True)
    p.set_defaults(run=cmd_history)

    p = sub.add_parser("diff", help="write a unified diff of every change")
    p.add_argument("--work", required=True)
    p.add_argument("--root", help="defaults to the tree recorded at snapshot")
    p.add_argument("--out", required=True)
    p.set_defaults(run=cmd_diff)

    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
