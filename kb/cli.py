"""
One entry point for the packaged knowledge base.

A frozen executable cannot honour `python -m kb.index.build`, so every command
the client needs is reachable as a subcommand here instead:

    alphaplus-kb setup        pick the source tree, index it, wire up Claude
    alphaplus-kb serve-http   the MCP server on a loopback port; stays running
    alphaplus-kb serve        the MCP server over stdio (Claude launches this)
    alphaplus-kb index        (re)build the index
    alphaplus-kb refresh      update if the source moved, skip if it did not
    alphaplus-kb update       re-index only what changed
    alphaplus-kb status       coverage, blind spots, what was not indexed
    alphaplus-kb lexicon      re-seed the business-term lexicon
    alphaplus-kb graph        write the HTML codebase map
    alphaplus-kb export-json  write the code graph as JSON, for showing
    alphaplus-kb token        show or rotate the MCP bearer token
    alphaplus-kb config       where am I pointed?
    alphaplus-kb doctor       check the install, end to end

Double-clicked with no arguments it runs `setup`, because that is what
somebody who just installed it wants.  Launched with no arguments and no
terminal attached it runs `serve`, because that means an MCP client started it
and forgot the argument.

kb.bootstrap.apply() must have run before this module is imported - see the
docstring there for why.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from . import bootstrap

BANNER = "Alphaplus knowledge base"


# ===========================================================================
# Helpers
# ===========================================================================

def _interactive() -> bool:
    """True when a human is watching.

    Both ends matter: the MCP server owns stdin and stdout, so if either is
    redirected we must not print or prompt.
    """
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _ask_directory(prompt: str) -> Optional[Path]:
    """A folder from the user: native picker if we can, typed path if not."""
    if _interactive():
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            chosen = filedialog.askdirectory(title=prompt)
            root.destroy()
            if chosen:
                return Path(chosen)
        except Exception:
            # No display, no tkinter in the bundle, or a locked-down desktop.
            # Typing the path always works.
            pass
    print(f"\n{prompt}")
    print("  Paste the full path and press Enter (or press Enter to cancel).")
    try:
        typed = input("  > ").strip().strip('"').strip("'")
    except EOFError:
        return None
    return Path(typed) if typed else None


def _confirm(question: str, default: bool = True) -> bool:
    if not _interactive():
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"  {question} {suffix} ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer.startswith("y")


def web_default_port() -> int:
    """The HTTP transport's default port, without importing kb.config early."""
    from .server import web
    return web.DEFAULT_PORT


def _is_network_path(path: Path) -> bool:
    """True when `path` is a UNC path or a mapped network drive.

    Only meaningful on Windows; anywhere else this returns False and the
    caller's check simply passes.
    """
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    if os.name != "nt" or len(text) < 2 or text[1] != ":":
        return False
    try:
        import ctypes
        # DRIVE_REMOTE == 4
        drive = f"{text[0]}:\\"
        return ctypes.windll.kernel32.GetDriveTypeW(drive) == 4
    except Exception:
        return False


def _open_in_browser(path: Path) -> bool:
    """Hand a local file to the desktop's default browser.

    webbrowser is stdlib and handles the platform differences.  A locked-down
    desktop can refuse, so the caller gets a bool and prints the path either
    way - never leave somebody with nothing to click.
    """
    try:
        import webbrowser
        return webbrowser.open(path.resolve().as_uri())
    except Exception:
        return False


def _count_source(root: Path) -> tuple:
    """(recognised, unrecognised) member counts under root, cheaply."""
    from . import config
    recognised = 0
    unrecognised = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in config.IGNORE_DIRS for part in path.parts):
            continue
        if config.member_type_for(path.suffix):
            recognised += 1
        else:
            unrecognised += 1
    return recognised, unrecognised


# ===========================================================================
# setup - the thing that runs on a double-click
# ===========================================================================

def _indexed_source_root() -> Optional[str]:
    """Which tree the existing index was built from, or None."""
    from . import config
    from kb import store
    if store.backend() != "postgres" and not config.FACTS_DB.exists():
        return None
    try:
        conn = store.facts()
        row = conn.execute(
            "SELECT value FROM build_info WHERE key='source_root'").fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:                                   # noqa: BLE001
        # Exception, not sqlite3.Error: with a DSN this is a psycopg failure,
        # and the caller only wants "cannot tell", never a traceback.
        return None


def _same_tree(indexed: Optional[str], current: Path) -> bool:
    if not indexed:
        return False
    try:
        return Path(indexed).resolve() == current.resolve()
    except OSError:
        return str(indexed).rstrip("\\/").lower() == \
            str(current).rstrip("\\/").lower()


def refresh_index(quiet: bool = False) -> int:
    """Bring the index up to date, doing the least work that is correct.

    Running the executable a second time should not re-read 13,000 members to
    discover that nothing changed.  Three cases:

      no index yet                  full build
      index is of a different tree  full build - the hashes belong to
                                    another codebase and a diff against
                                    them would be meaningless
      index is of this tree         incremental update, which hashes every
                                    member, re-parses only what moved, and
                                    says "nothing to do" when nothing did

    A full build stays the source of truth; this only decides when to spend
    one.  `index` still forces a rebuild for anyone who wants it.
    """
    from . import config
    indexed = _indexed_source_root()

    if indexed is None:
        if not quiet:
            print("\n  No index yet - building from scratch.")
        return cmd_index(argparse.Namespace(limit=None, workers=8,
                                            quiet=quiet))

    if not _same_tree(indexed, config.SOURCE_ROOT):
        if not quiet:
            print("\n  The existing index was built from a different tree:")
            print(f"    indexed : {indexed}")
            print(f"    now     : {config.SOURCE_ROOT}")
            print("  Rebuilding from scratch - the stored hashes belong to")
            print("  another codebase.")
        return cmd_index(argparse.Namespace(limit=None, workers=8,
                                            quiet=quiet))

    if not quiet:
        print("\n  An index for this tree already exists. Checking what")
        print("  changed since it was built ...")
    from .index import update as updater
    counts = updater.update(quiet=quiet)
    if not counts:
        # update() refuses when the database is missing; fall back rather
        # than leave the caller with nothing.
        return cmd_index(argparse.Namespace(limit=None, workers=8,
                                            quiet=quiet))
    if not quiet:
        moved = (counts.get("added", 0) + counts.get("modified", 0)
                 + counts.get("deleted", 0))
        if not moved:
            print("  The source has not changed - the index is already "
                  "current.")
    return 0


def _settle_index_location(source: Path, settings: dict):
    """Say where the index will be written, and let it be moved.

    The default is `<source>/_alphaplus-kb`, beside the code it describes,
    which is convenient and is the right answer on an ordinary local folder.
    It is the wrong answer twice:

      The index is SQLite in WAL mode - several files, locked and rewritten
      continuously while 13,000 members are parsed. On a network share, a
      mapped drive or a folder a sync client is watching, that locking is
      not reliable, and the failure looks like the parse stopping part-way
      and leaving an empty folder behind.

      And it is hundreds of megabytes appearing inside a client's source
      extraction, which is theirs.

    So: always say where it is going, and when the tree is on a network path
    default to somewhere local instead of quietly writing there anyway.
    """
    from . import config
    default = config.DATA_DIR
    risky = _is_network_path(default)

    print()
    print("  THE INDEX")
    print(f"    It will be written to:  {default}")
    print("    About 200-250 MB for an extraction this size. Nothing else is")
    print("    written into the source tree - members are only ever read.")

    if risky:
        print()
        print("    ** That is a network path. **")
        print("    The index is SQLite, which locks files continuously while")
        print("    it builds. Over a network share that locking is not")
        print("    reliable, and the usual symptom is the parse stopping")
        print("    part-way and leaving an empty folder behind.")
        print("    Somewhere local is strongly recommended.")

    if not _interactive():
        if risky:
            fallback = config._per_user_data_dir()
            print(f"    Not asking on a scripted run - using {fallback}")
            return fallback
        return default

    question = ("Put the index somewhere else?"
                if not risky else
                "Put the index on a local disk instead?")
    if not _confirm(question, default=risky):
        return default

    chosen = _ask_directory("Where should the index go?")
    if chosen is None:
        return default
    if _is_network_path(chosen):
        print("    That is also a network path. Using it anyway, as asked -")
        print("    if the build stops part-way, this is the first thing to "
              "change.")
    return chosen


def cmd_setup(args: argparse.Namespace) -> int:
    print()
    print(f"  {BANNER} - setup")
    print("  " + "=" * 58)

    settings = bootstrap.load_settings()
    source: Optional[Path] = Path(args.source) if args.source else None

    if source is None and settings.get("source"):
        current = Path(settings["source"])
        print(f"\n  Currently pointed at: {current}")
        if current.exists() and _confirm("Keep this source tree?"):
            source = current

    while source is None:
        source = _ask_directory("Where is the source code?")
        if source is None:
            print("\n  Cancelled. Nothing was changed.")
            _pause_if_double_clicked()
            return 1
        if not source.exists():
            print(f"  That path does not exist: {source}")
            source = None
            continue
        if not source.is_dir():
            print(f"  That is a file, not a folder: {source}")
            source = None
            continue

    print(f"\n  Scanning {source} ...")
    recognised, unrecognised = _count_source(source)
    print(f"    {recognised:,} source member(s) recognised")
    if unrecognised:
        print(f"    {unrecognised:,} file(s) with extensions we do not map "
              f"- these will be reported, not silently dropped")
    if not recognised:
        print()
        print("  No recognised source members were found under that folder.")
        print("  Expected one directory per source physical file, with the")
        print("  member type as the file extension - QCLSRC/*.CLLE,")
        print("  QDDSSRC/*.PF and so on.")
        print("  If your extraction uses different extensions, they need")
        print("  mapping before indexing will find anything.")
        if not _confirm("Save this path anyway?", default=False):
            return 1

    settings["source"] = str(source)
    if args.data:
        settings["data"] = str(args.data)
    saved = bootstrap.save_settings(settings)
    print(f"\n  Saved: {saved}")

    # kb.config was imported during the scan above, so its path constants are
    # already fixed. Setting the environment variable now would do nothing -
    # repoint() is what actually moves this process.
    from . import config
    config.repoint(source=source, data=settings.get("data"))

    data_dir = _settle_index_location(source, settings)
    if data_dir is None:
        print("\n  Cancelled. Nothing was changed.")
        _pause_if_double_clicked()
        return 1
    if str(data_dir) != str(config.DATA_DIR):
        settings["data"] = str(data_dir)
        bootstrap.save_settings(settings)
        config.repoint(source=source, data=str(data_dir))

    # Setup writes NOTHING into the source tree. It used to offer a local
    # git repository here, and that was the wrong place for it twice over:
    #
    #   A new `.git` makes the shell's icon-overlay handlers walk the whole
    #   tree to work out what badge each folder gets. On a forty-gigabyte
    #   extraction on a VDI that is a long, file-locking scan running at
    #   exactly the moment the parser wants to read every member.
    #
    #   And it is somebody else's folder. A tool that installs itself into a
    #   client's source tree before it has been asked to change anything is a
    #   tool people stop trusting with the next thing.
    #
    # Version control now happens where the change does: `source_guard.py
    # snapshot` creates the repository, the .gitignore and the baseline commit
    # the first time a ticket edits a member. Nothing before that.
    if recognised and _confirm("\n  Index the source now?", default=True):
        rc = refresh_index()
        if rc != 0:
            return rc
        if _confirm("\n  Re-seed the business-term lexicon?", default=True):
            cmd_lexicon(argparse.Namespace())

        # The map is the fastest way for somebody to see that the ingest
        # actually worked, so offer it here rather than burying it in a
        # subcommand nobody runs.
        print()
        print("  The codebase map is a single self-contained HTML file -")
        print("  every member, every call, every override, no internet")
        print("  needed. It opens straight off the filesystem.")
        if _confirm("  Build and open the codebase map now? "
                    "(about a minute)", default=True):
            cmd_graph(argparse.Namespace(out=None, force_layout=False,
                                         neo4j=False, open=True))

    transport = args.transport
    if transport == "ask":
        print()
        print("  How should Claude reach the knowledge base?")
        print()
        print("    http   The server runs as its own process on a loopback")
        print("           port. Start it once and leave it up; several VS Code")
        print("           windows share it and the index stays open.")
        print("           Needs a window (or a scheduled task) to stay running.")
        print()
        print("    stdio  Claude starts the server itself, per session.")
        print("           No port, no token, no process to keep alive.")
        print("           Fewer moving parts, but no shared long-lived server.")
        print()
        transport = "http" if _confirm("Use http?", default=True) else "stdio"

    settings["transport"] = transport
    bootstrap.save_settings(settings)

    _write_mcp_config(transport, getattr(args, 'scope', 'ask'))
    print()
    if transport == "http":
        print("  Setup is done. Claude needs the server running to reach it.")
        print("  Restart Claude Code as well, so it reads .mcp.json.")
        print()
        # Only offer this to a human.  serve_forever() never returns, so a
        # scripted `setup --source ...` would hang for ever instead of
        # finishing - which is exactly what a silent VDI install would do.
        if _interactive() and _confirm("  Start the MCP server now?",
                                       default=True):
            # Hand this window over to the server rather than ending here.
            # Double-clicked, the console closes the moment setup returns -
            # which looks like a crash and leaves nothing running for Claude
            # to talk to.  Serving from here fixes both.
            print()
            return cmd_serve_http(argparse.Namespace(
                port=None, token=None, quiet=False))
        print("  Start it later with:  alphaplus-kb serve-http")
    else:
        print("  Done. Next: restart Claude Code so it picks up the server.")
        print("  Claude starts and stops the server itself - nothing to run.")
    _pause_if_double_clicked()
    return 0


def _pause_if_double_clicked() -> None:
    """Hold the window open so a human can read what just happened.

    A console process launched by double-click takes its window with it when
    it exits.  Everything setup printed - the member count, where the index
    went, the config block - disappears in the same instant.  Reported as
    "the terminal exited", which is exactly what it looks like.
    """
    if not _interactive():
        return
    try:
        input("\n  Press Enter to close this window. ")
    except EOFError:
        pass


def _mcp_block(transport: str) -> dict:
    """The .mcp.json entry for the chosen transport."""
    from . import config
    from .server import web

    if transport == "http":
        # A token is minted now and handed to both sides: written into the
        # client config here, and into the runtime file when the server
        # starts. Reusing an existing one keeps a config valid across
        # restarts.
        existing = web.read_runtime()
        # The persistent token, not a fresh one: this block is written into the
        # client's .mcp.json and has to keep working after the server restarts.
        token = web.persistent_token()
        port = existing.get("port") or web.pick_port()
        return {
            "mcpServers": {
                "alphaplus": {
                    "type": "http",
                    "url": f"http://127.0.0.1:{port}{web.MCP_PATH}",
                    "headers": {"Authorization": f"Bearer {token}"},
                }
            }
        }

    command = (str(Path(sys.executable).resolve()) if config.FROZEN
               else sys.executable)
    args: List[str] = ["serve"] if config.FROZEN else ["-m", "kb", "serve"]
    return {
        "mcpServers": {
            "alphaplus": {
                "command": command,
                "args": args,
                "env": {
                    "ALPHAPLUS_SOURCE": str(config.SOURCE_ROOT),
                    "ALPHAPLUS_KB_DATA": str(config.DATA_DIR),
                },
            }
        }
    }


#: The skills this build ships: (name Claude Code lists it under, folder
#: beside this module). The impact skill decides what a ticket changes; the
#: implement skill edits the source and reports the edit. They are installed
#: and checked together, because the second follows the first's report.
SKILLS = (("alphaplus-impact", "skill"),
          ("alphaplus-implement", "skill-implement"),
          ("alphaplus-implement-report", "skill-report"))

#: Files in a skill directory that define its behaviour: its instructions and
#: the scripts those instructions run.
_SKILL_SUFFIXES = {".md", ".py"}


def _user_skill_dir(name: str = "alphaplus-impact") -> Path:
    """Where a skill installed for the whole machine lives."""
    return Path.home() / ".claude" / "skills" / name


def skill_fingerprint(directory: Path) -> str:
    """A short, stable fingerprint of a skill directory, or "" if absent.

    Answers the one question nobody could answer before: **is the skill on
    this machine the skill in this exe?** The skill is copied out at setup, so
    an upgraded exe beside a skill directory installed by an older one is not
    only possible, it is the normal state after every upgrade where somebody
    declined to re-run setup. Nothing said so, and a stale skill fails
    silently: the report simply comes back missing whatever the new version
    would have required.

    Content-addressed rather than a version number written by hand. A number
    has to be remembered on every edit and is wrong the first time it is not;
    a hash of the bytes cannot drift from what it describes. Path and content
    both feed it, so a renamed or deleted reference file changes the answer -
    an install missing `references/` reads as different, which it is.

    Instructions and scripts both count: a skill whose script changed behaves
    differently even when every word of its text is the same.
    """
    if not directory.is_dir():
        return ""
    import hashlib
    h = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if (not path.is_file() or path.suffix.lower() not in _SKILL_SUFFIXES
                or "__pycache__" in path.parts):
            continue
        h.update(path.relative_to(directory).as_posix().encode("utf-8"))
        # Newlines normalised: git checkouts and a copytree across platforms
        # differ on CRLF, and a skill that is byte-different but word-identical
        # is not a stale skill.
        h.update(path.read_bytes().replace(
            bytes([13, 10]), bytes([10])))
    return h.hexdigest()[:12]


def bundled_skill_dir(folder: str = "skill") -> Path:
    """A skill inside this build."""
    return Path(__file__).with_name(folder)


def installed_skill_dirs(name: str = "alphaplus-impact") -> "List[Path]":
    """Every place setup could have put a skill on this machine."""
    from . import config
    return [Path.home() / ".claude" / "skills" / name,
            config.SOURCE_ROOT / ".claude" / "skills" / name]


def _install_skills_under(skills_root: Path) -> bool:
    """Install every skill this build ships under one `.claude/skills` folder."""
    landed = True
    for name, folder in SKILLS:
        landed = _install_skill_at(skills_root / name, folder) and landed
    return landed


def _install_skill_at(target_dir: Path, folder: str = "skill") -> bool:
    """Copy the whole skill into target_dir.  True when it landed.

    The whole skill, not SKILL.md - and both halves of that matter, because a
    skill carries files it reads only when it needs them:

        SKILL.md                loaded whenever the skill triggers
        IMPACT-REPORT.md        the report layout, read when a report is wanted
        references/*.md         the template, playbook and worked example

    Keeping those out of SKILL.md is the point - it stays short and the rest
    is read on demand. But installing SKILL.md alone leaves a skill whose own
    text points at files that are not there, and nothing reports a problem:
    it fails at the moment somebody asks for a report, which is the worst
    time to find out.

    `copytree` rather than a glob over `*.md`, because `references/` is a
    directory and a glob at the top level does not reach into it.
    `dirs_exist_ok` so re-running setup after an upgrade replaces the files
    rather than failing on a directory that is already there.
    """
    source = bundled_skill_dir(folder)
    if not (source / "SKILL.md").exists():
        print(f"  The skill {target_dir.name} is missing from this build - "
              f"skipping.")
        return False
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target_dir, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    except OSError as exc:
        print(f"  Could not install the skill: {exc}")
        return False

    # `copytree` adds and replaces; it never removes. A reference file dropped
    # between versions, or a `__pycache__` an older installer copied before it
    # knew better, therefore survives every upgrade - and the fingerprint of
    # the install stops matching the fingerprint of the exe, which is the one
    # question this whole mechanism exists to answer. So: anything in the
    # target that the source does not have, goes.
    wanted = {p.relative_to(source).as_posix() for p in source.rglob("*")
              if p.is_file() and "__pycache__" not in p.parts
              and p.suffix.lower() != ".pyc"}
    for path in sorted(target_dir.rglob("*"), reverse=True):
        try:
            if path.is_file():
                if path.relative_to(target_dir).as_posix() not in wanted:
                    path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        except OSError:
            pass                      # a locked file is not worth failing over

    # Say what actually landed. A silent success here was how a one-file
    # install went unnoticed for weeks.
    files = sorted(p.relative_to(target_dir).as_posix()
                   for p in target_dir.rglob("*") if p.is_file())
    print(f"  Skill installed: {target_dir}")
    for name in files:
        print(f"      {name}")
    # The fingerprint, so "which skill is on that machine" is answerable
    # afterwards from a screenshot of this output alone. `doctor` prints the
    # same value and compares it with the one in the exe.
    print(f"      version {skill_fingerprint(target_dir)}   "
          f"(check later with: doctor)")
    return True


#: The hook that reads a ticket file named in a message, and the settings
#: file Claude Code reads it from.
HOOK_MARKER = "ticket-hook"


def _hook_command() -> "List[str]":
    """How this build is launched, as a hook command."""
    from . import config
    if config.FROZEN:
        return [str(Path(sys.executable).resolve()), HOOK_MARKER]
    return [sys.executable, "-m", "kb", HOOK_MARKER]


def _quote(part: str) -> str:
    return f'"{part}"' if " " in part else part


def install_hook(settings_path: Path) -> bool:
    """Add the ticket hook to a Claude Code settings file, keeping the rest.

    Merged rather than written: the file is the user's, and it holds their own
    hooks and permissions. An entry we already own is updated in place, so an
    upgraded exe does not leave a second hook pointing at the old path.
    """
    import json

    command = " ".join(_quote(part) for part in _hook_command())
    entry = {"type": "command", "command": command, "timeout": 60}

    data: dict = {}
    if settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  Could not read {settings_path}: {exc}")
            print("  Refusing to overwrite a settings file I cannot parse.")
            return False

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = data["hooks"] = {}
    matchers = hooks.setdefault("UserPromptSubmit", [])
    if not isinstance(matchers, list):
        matchers = hooks["UserPromptSubmit"] = []

    for matcher in matchers:
        if not isinstance(matcher, dict):
            continue
        commands = matcher.get("hooks")
        if not isinstance(commands, list):
            continue
        for index, existing in enumerate(commands):
            if isinstance(existing, dict) and HOOK_MARKER in str(existing.get("command", "")):
                commands[index] = entry              # ours: keep it current
                return _write_settings(settings_path, data)
    matchers.append({"hooks": [entry]})
    return _write_settings(settings_path, data)


def _write_settings(path: Path, data: dict) -> bool:
    import json
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = path.with_suffix(path.suffix + ".alphaplus-backup")
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"  Could not write {path}: {exc}")
        return False
    print(f"  Ticket hook registered in {path}")
    print("  A message naming a ticket PDF, XML or Word file is now read and "
          "structured before Claude answers.")
    return True


def _register_user_scope(block: dict) -> bool:
    """Add the server to Claude Code's user-level config.

    Claude Code owns ~/.claude.json and keeps other servers in it, so this
    merges one key rather than writing the file wholesale, and leaves anything
    it does not understand untouched.  Preferred over a per-project .mcp.json
    for a rollout: one registration covers every folder the user opens, and
    nothing lands inside their source repositories.
    """
    import json

    path = Path.home() / ".claude.json"
    entry = block["mcpServers"]["alphaplus"]

    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  Could not read {path}: {exc}")
            print("  Refusing to overwrite a file I cannot parse - Claude")
            print("  Code keeps its own settings in there.")
            return False

    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers
    servers["alphaplus"] = entry

    try:
        # Keep a copy of whatever was there. This file is Claude Code's, and
        # a bad merge should be recoverable without our help.
        if path.exists():
            backup = path.with_suffix(".json.alphaplus-backup")
            backup.write_text(path.read_text(encoding="utf-8"),
                              encoding="utf-8")
            print(f"  Backed up {path.name} to {backup.name}")
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"  Could not write {path}: {exc}")
        return False
    print(f"  Registered in {path}")
    return True


def _write_mcp_config(transport: str = "http", scope: str = "ask") -> None:
    """Register the server with Claude Code, at user or project scope."""
    block = _mcp_block(transport)

    import json
    text = json.dumps(block, indent=2)

    print()
    print("  This is what Claude Code needs in order to reach the server:")
    print()
    for line in text.splitlines():
        print("    " + line)

    if scope == "ask":
        print()
        print("  Where should it go?")
        print()
        print("    user     once, for every folder you open. Registered in")
        print("             ~/.claude.json, and the skill in ~/.claude/skills.")
        print("             Nothing lands in your source repositories.")
        print()
        print("    project  in one folder's .mcp.json and .claude/skills.")
        print("             You repeat this for every project.")
        print()
        scope = "user" if _confirm("  Install for the whole machine (user)?",
                                   default=True) else "project"

    if scope == "user":
        if _register_user_scope(block):
            _install_skills_under(_user_skill_dir().parent)
            install_hook(Path.home() / ".claude" / "settings.json")
            print()
            print("  Restart Claude Code. This applies in every folder now -")
            print("  no per-project files needed.")
        return

    target = _ask_mcp_target()
    if target is None:
        print("\n  Not written. Copy the block above into .mcp.json in the")
        print("  root of whichever project you open in VS Code.")
        return

    existing = {}
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
    servers = existing.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        servers = existing["mcpServers"] = {}
    servers["alphaplus"] = block["mcpServers"]["alphaplus"]
    target.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    print(f"\n  Written to {target}")

    # And the skill beside it. `_install_skill` existed for weeks and was
    # never called from anywhere, so project-scope setup wrote .mcp.json and
    # silently installed nothing - the agent then had 23 tool descriptions
    # and no method for choosing between them.
    _install_skills_under(target.parent / ".claude" / "skills")
    install_hook(target.parent / ".claude" / "settings.json")
    print()
    print("  Restart Claude Code in that folder. Type / and the skills are")
    print("  listed as " + " and ".join(name for name, _ in SKILLS) + ".")


def _ask_mcp_target() -> Optional[Path]:
    if not _interactive():
        return None
    if not _confirm("\n  Write this into a project's .mcp.json now?",
                    default=False):
        return None
    folder = _ask_directory("Which project folder do you open in VS Code?")
    if folder is None or not folder.is_dir():
        return None
    return folder / ".mcp.json"


# ===========================================================================
# The rest - thin wrappers over the existing modules
# ===========================================================================

def cmd_serve_http(args: argparse.Namespace) -> int:
    from .server import web
    return web.serve_http(port=args.port, token=args.token, quiet=args.quiet)


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import server
    if args.selftest:
        return server.main(["--selftest"])
    if _interactive():
        # Double-clicked, or run by hand. Without this the process sits
        # silently waiting on stdin and looks hung.
        print()
        print(f"  {BANNER} - MCP server")
        print("  " + "=" * 58)
        print("  This command speaks JSON-RPC on stdin/stdout. It is meant to")
        print("  be launched by Claude Code, not run by hand.")
        print()
        print("  To set up:        alphaplus-kb setup")
        print("  To check things:  alphaplus-kb doctor")
        print()
        return 0
    return server.main([])


def cmd_index(args: argparse.Namespace) -> int:
    from .index import build
    stats = build.build(limit=args.limit, workers=args.workers,
                        quiet=args.quiet)
    return 0 if stats else 1


def cmd_update(args: argparse.Namespace) -> int:
    from .index import update
    argv: List[str] = []
    if args.dry_run:
        argv.append("--dry-run")
    if args.verify:
        argv.append("--verify")
    if getattr(args, "rehash", False):
        argv.append("--rehash")
    return update.main(argv)


def cmd_status(args: argparse.Namespace) -> int:
    from .index import status
    return status.report()


def cmd_lexicon(args: argparse.Namespace) -> int:
    from .lexicon import terms
    seeded = terms.build()
    print(f"  seeded {len(seeded):,} words, plus {len(terms.manual_terms()):,} "
          f"hand-written")
    return 0


def cmd_summarise(args: argparse.Namespace) -> int:
    from .summarise import pyramid
    argv: List[str] = []
    for flag in ("estimate", "status", "dry_run", "refresh"):
        if getattr(args, flag, False):
            argv.append("--" + flag.replace("_", "-"))
    if args.level:
        argv += ["--level", args.level]
    if args.limit:
        argv += ["--limit", str(args.limit)]
    return pyramid.main(argv)


def cmd_semantic(args: argparse.Namespace) -> int:
    from .summarise import vectors
    argv: List[str] = []
    if args.check:
        argv.append("--check")
    if args.rebuild:
        argv.append("--rebuild")
    if args.limit:
        argv += ["--limit", str(args.limit)]
    return vectors.main(argv)


def cmd_graph(args: argparse.Namespace) -> int:
    from . import config
    from .viz import build as vb
    out = Path(args.out) if args.out else config.DATA_DIR / "graph.html"
    argv: List[str] = ["-o", str(out)]
    if args.force_layout:
        argv.append("--force-layout")
    if args.neo4j:
        argv.append("--neo4j")
    if getattr(args, "classic", False):
        argv.append("--classic")
    rc = vb.main(argv)
    if rc == 0 and getattr(args, "open", False) and out.exists():
        print()
        if _open_in_browser(out):
            print(f"  Opened in your browser: {out}")
        else:
            print("  Could not launch a browser. Open this file by hand:")
            print(f"    {out}")
    return rc


def cmd_sync(args: argparse.Namespace) -> int:
    """Keep the index, the graph and the map in step with the source."""
    from . import sync
    argv: List[str] = []
    if getattr(args, "source", None):
        argv += ["--source", args.source]
    if args.status:
        argv.append("--status")
    if getattr(args, "auto", False):
        argv.append("--auto")
    if args.watch:
        argv += ["--watch", "--every", str(args.every)]
    if args.force:
        argv.append("--force")
    if getattr(args, "open", False):
        argv.append("--open")
    return sync.main(argv)


def cmd_export_json(args: argparse.Namespace) -> int:
    from . import export
    argv = []
    if args.out:
        argv += ["-o", str(args.out)]
    if args.include_fields:
        argv.append("--include-fields")
    if args.include_field_usage:
        argv.append("--include-field-usage")
    if args.compact:
        argv.append("--compact")
    return export.main(argv)


def cmd_token(args: argparse.Namespace) -> int:
    """Show or rotate the bearer token.

    Rotation matters because the token is a long-lived shared secret with no
    expiry.  If it leaks - pasted into a ticket, committed to git, copied into
    a screenshot - the only remedy is to mint a new one and update whatever
    holds the old one.  A refresh-token flow would not help: nothing here
    crosses a network boundary, and an expiry would only add a way for the
    server to stop working overnight.
    """
    from . import config
    from .server import web

    if args.rotate:
        old = web.persistent_token()
        new = web.persistent_token(regenerate=True)
        print()
        print("  Token rotated.")
        print(f"    was : {old[:12]}...")
        print(f"    now : {new}")
        print(f"    file: {web.token_path()}")

        updated = _update_token_in_configs(new)
        print()
        if updated:
            for path in updated:
                print(f"  Updated {path}")
        else:
            print("  No .mcp.json was updated - none found in the places")
            print("  checked. Put the new token in yours by hand.")
        print()
        print("  Two things must happen for this to take effect:")
        print("    1. Restart the server   - it holds the old token in memory")
        print("    2. Restart Claude Code  - it caches the config")
        print()
        return 0

    token = web.persistent_token()
    print()
    print(f"  token : {token}")
    print(f"  file  : {web.token_path()}")
    print(f"  output: {config.DATA_DIR}")
    print()
    print("  This is a credential. It is the only thing standing between")
    print("  any local process and the whole index.")
    print("  Rotate it with:  alphaplus-kb token --rotate")
    print()
    return 0


def _update_token_in_configs(token: str) -> List[Path]:
    """Rewrite the Authorization header in every .mcp.json we can find.

    Only files that already name an `alphaplus` server are touched, and only
    that one entry within them - these files belong to Claude Code and may
    describe other servers we know nothing about.
    """
    import json

    candidates: List[Path] = []
    settings = bootstrap.load_settings()
    for raw in (settings.get("mcp_config"), settings.get("project")):
        if raw:
            candidates.append(Path(raw))
    candidates.append(Path.cwd() / ".mcp.json")
    home = Path.home() / ".claude.json"
    if home.exists():
        candidates.append(home)

    changed: List[Path] = []
    seen = set()
    for path in candidates:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        touched = False
        for holder in _mcp_server_holders(data):
            entry = holder.get("alphaplus")
            if isinstance(entry, dict) and "headers" in entry:
                entry["headers"]["Authorization"] = f"Bearer {token}"
                touched = True
        if touched:
            try:
                path.write_text(json.dumps(data, indent=2) + "\n",
                                encoding="utf-8")
                changed.append(path)
            except OSError:
                pass
    return changed


def _mcp_server_holders(data: dict):
    """Every dict of MCP servers inside a config, at any nesting we expect.

    A project .mcp.json has one at the top level.  Claude Code's user config
    keeps one per project, so yield those too rather than only rewriting the
    project file and leaving the user-scoped copy stale.
    """
    top = data.get("mcpServers")
    if isinstance(top, dict):
        yield top
    projects = data.get("projects")
    if isinstance(projects, dict):
        for entry in projects.values():
            if isinstance(entry, dict):
                nested = entry.get("mcpServers")
                if isinstance(nested, dict):
                    yield nested


def _doctor_hook() -> None:
    """Is the ticket hook installed, and does it point at THIS build?

    An exe upgraded in place leaves a hook command naming the old path, and
    the only symptom is that ticket files quietly stop being read - the same
    failure shape as a stale skill, which is why it is checked beside it.
    """
    import json

    expected = " ".join(_quote(part) for part in _hook_command())
    print()
    print("  TICKET HOOK")
    found = False
    for settings in (Path.home() / ".claude" / "settings.json",
                     Path.cwd() / ".claude" / "settings.json"):
        if not settings.exists():
            continue
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print("    %s: could not be read" % settings)
            continue
        hooks = (data.get("hooks") or {}).get("UserPromptSubmit") or []
        for matcher in hooks:
            for entry in (matcher or {}).get("hooks", []) or []:
                command = str((entry or {}).get("command", ""))
                if HOOK_MARKER not in command:
                    continue
                found = True
                same = command.strip() == expected.strip()
                print("    %s %s" % ("ok   " if same else "STALE", settings))
                if not same:
                    print("           registered: %s" % command)
                    print("           this build: %s" % expected)
                    print("           re-run setup so ticket files are read "
                          "by this version")
    if not found:
        print("    not installed - a ticket file named in a message is not "
              "read automatically.")
        print("    Run setup, or read one by hand: alphaplus-kb ticket <file>")


def cmd_ticket(args: argparse.Namespace) -> int:
    """Read a Jira export and write it as structured XML."""
    from . import ticket as ticket_reader

    source = Path(args.file).expanduser()
    if not source.exists():
        print(f"  No such file: {source}")
        return 1

    parsed = ticket_reader.parse(source)
    root = Path(args.out).expanduser() if args.out else Path.cwd()
    written = None
    try:
        written = ticket_reader.write(parsed, root, source)
    except OSError as exc:
        print(f"  Could not write the structured copy: {exc}")

    if args.print_xml:
        print(parsed.xml)
        return 0

    recorded = ""
    try:
        from .ticket.record import record
        _n, recorded = record(parsed, source)
    except Exception as exc:                                  # noqa: BLE001
        recorded = f"could not record into ticket memory: {exc}"

    print()
    print(parsed.summary())
    if written:
        print(f"\n  written    {written}")
        print("  Read that file before the first tool call: it carries the "
              "acceptance criteria and every comment.")
    if recorded:
        print(f"  memory     {recorded}")
        print("  `recall` finds them in a later session, without the file.")
    # A ticket nobody can read is a finding, not a result.
    return 0 if parsed.ticket.pages or parsed.ticket.description else 1


def cmd_ticket_hook(args: argparse.Namespace) -> int:
    """Claude Code UserPromptSubmit hook. Never fails a turn."""
    from .ticket import hook
    try:
        return hook.main()
    except Exception:                                     # noqa: BLE001
        return 0


def cmd_config(args: argparse.Namespace) -> int:
    from . import config
    print()
    print(f"  {BANNER}  v{config.BUILD_VERSION}")
    print("  " + "=" * 58)
    print("  " + config.describe().replace("\n", "\n  "))
    print(f"  output   : {config.DATA_DIR}")
    print(f"  settings : {bootstrap.settings_path()}")
    print(f"  frozen   : {config.FROZEN}")
    print()
    print("  Everything the knowledge base writes - the index, the codebase")
    print("  map, the JSON export - is in the output folder above.")
    print("  Source members are only ever read.")
    print()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check the things that actually go wrong on a client machine."""
    from . import config
    print()
    print(f"  {BANNER} v{config.BUILD_VERSION} - install check")
    print("  " + "=" * 58)

    problems: List[str] = []

    def check(label: str, ok: bool, detail: str = "",
              fatal: bool = True, advice: str = "") -> bool:
        # A non-fatal miss is 'warn', not 'FAIL' - otherwise the summary says
        # everything checks out directly under the word FAIL.
        mark = "ok  " if ok else ("FAIL" if fatal else "warn")
        print(f"  {mark} {label}")
        if detail:
            print(f"       {detail}")
        if advice and not ok:
            print(f"       {advice}")
        if not ok and fatal:
            problems.append(label)
        return ok

    # ---- is the SKILL on this machine the skill in this exe? ------------
    #
    # First thing checked, because every other answer here is about whether
    # the knowledge base can be reached, and this one is about whether the
    # instructions using it are the current ones. An exe upgraded without
    # re-running setup leaves the old skill in place and says nothing; the
    # only symptom is a report quietly missing whatever the new version
    # requires, which is indistinguishable from the model doing a poor job.
    _doctor_hook()

    for name, folder in SKILLS:
        bundled = skill_fingerprint(bundled_skill_dir(folder))
        if not bundled:
            check(f"skill {name} bundled in this build", False,
                  "not in this exe")
            continue
        found = [(d, skill_fingerprint(d)) for d in installed_skill_dirs(name)]
        live = [(d, f) for d, f in found if f]
        if not live:
            check(f"skill {name} installed", False, f"build {bundled}, "
                  f"installed nowhere this command knows to look", fatal=False,
                  advice="run setup, and choose where to install the skills")
        for d, f in live:
            check(f"skill up to date  ({d})", f == bundled,
                  f"build {bundled}, installed {f}"
                  + ("" if f == bundled else "  <- STALE"),
                  fatal=False,
                  advice="re-run setup to replace it - the exe overwrites the "
                         "installed copy")

    check("settings file", bootstrap.settings_path().exists(),
          str(bootstrap.settings_path()), fatal=False,
          advice="run setup to create it")

    # Ticket memory: where earlier analyses' facts are kept. Absent is normal
    # until the first ticket records something; unwritable is not.
    try:
        from .server import memory
        info = memory.stats()
        where = Path(str(info["path"]))
        # The folder is created on first use, so what must be writable is
        # the nearest folder that already exists.
        existing = where.parent
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        writable = os.access(str(existing), os.W_OK)
        check("ticket memory", writable,
              f"{where}  -  {info['facts']:,} fact(s) from "
              f"{info['tickets']:,} ticket(s)" if info["exists"]
              else f"{where}  -  empty until the first ticket records a fact",
              fatal=False,
              advice="set ALPHAPLUS_MEMORY_DB to a writable file")
    except Exception as exc:                              # noqa: BLE001
        check("ticket memory", False, f"cannot open: {exc}", fatal=False,
              advice="set ALPHAPLUS_MEMORY_DB to a writable file")

    source_ok = check("source tree readable", config.SOURCE_ROOT.exists(),
                      str(config.SOURCE_ROOT))
    if source_ok:
        recognised, unrecognised = _count_source(config.SOURCE_ROOT)
        check("recognised members", recognised > 0,
              f"{recognised:,} recognised, {unrecognised:,} unmapped")

        # Whether git is available at all, because the implementation skill
        # needs it when a change is made. NOT whether the source tree is a
        # repository: it is not one until a ticket edits a member, and
        # reporting that as a problem would send somebody looking for a fault
        # that is the normal state.
        import shutil as _shutil
        check("git available for change history", _shutil.which("git") is not None,
              _shutil.which("git") or "not on PATH", fatal=False,
              advice="install git if changes should carry a history; without "
                     "it the implementation skill delivers patch files")

    writable = False
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = config.DATA_DIR / ".write-probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        writable = True
    except OSError as exc:
        check("data directory writable", False, f"{config.DATA_DIR}: {exc}")
    from kb import store
    on_postgres = store.backend() == "postgres"

    if writable:
        check("data directory writable", True, str(config.DATA_DIR))
        # SQLite runs the index in WAL mode, which needs shared memory and is
        # not safe on a network filesystem. On a VDI with folder redirection
        # %LOCALAPPDATA% is often a share, so this is worth catching before it
        # corrupts an index rather than after.
        #
        # Only meaningful for SQLite. On PostgreSQL the index is not a file on
        # this machine at all, and printing a warning about WAL on a network
        # share would send somebody looking in the wrong place entirely.
        if not on_postgres:
            check("data directory is local disk",
                  not _is_network_path(config.DATA_DIR),
                  str(config.DATA_DIR), fatal=False,
                  advice="the index uses SQLite WAL, which is unreliable on a "
                         "network share - point ALPHAPLUS_KB_DATA at a local "
                         "disk")

    # "Is the index built" is a question about the STORE, not about a file.
    # This checked FACTS_DB.exists() unconditionally, which on PostgreSQL
    # reported the state of a database nobody is reading: "ok" when a stale
    # facts.db happened to be lying around, and "index not built" when
    # PostgreSQL held a perfectly good one. Both answers wrong, and the second
    # sends you off to rebuild something that does not need rebuilding.
    if on_postgres:
        try:
            conn = store.facts()
            n = conn.execute("SELECT COUNT(*) FROM object").fetchone()[0]
            conn.close()
            db_ok = check("index built", n > 0,
                          f"postgres {store.schema_from_env()}: "
                          f"{n:,} member(s)",
                          advice="python -m kb.store.ddl --apply, then "
                                 "ALPHAPLUS_STORE=postgres python -m "
                                 "kb.index.build")
        except Exception as exc:                        # noqa: BLE001
            db_ok = check("index built", False,
                          f"postgres unreachable: {exc}",
                          advice="check ALPHAPLUS_PG_DSN")
    else:
        db_ok = check("index built", config.FACTS_DB.exists(),
                      str(config.FACTS_DB))

    # The bundled read-only data files. A missing schema means no index can
    # ever be built, and it is the single most likely packaging mistake.
    from .index import build as builder
    check("bundled schema.sql", builder.SCHEMA.exists(), str(builder.SCHEMA))
    from .viz import build as vb
    check("bundled template.html", vb.TEMPLATE.exists(), str(vb.TEMPLATE),
          fatal=False)

    if db_ok:
        try:
            from .server import tools
            names = tools.db().execute(
                "SELECT name FROM object ORDER BY RANDOM() LIMIT 1").fetchone()
            check("index queryable", names is not None,
                  f"sample member: {names[0] if names else 'none'}")
            # trace_ticket is the one that pulls in the lexicon lazily, which
            # is the classic thing a bundle gets wrong.
            tools.trace_ticket("customer credit limit", limit=1)
            check("all tools importable", True, "including the lexicon")

            # Importable is not the same as populated. The auto-seeded half is
            # generated, so it is not in git and not in the bundle; a frozen
            # build finds neither the per-index copy nor the package copy and
            # silently falls back to an empty dict, which costs recall with no
            # error anywhere. Measured at two gold answers out of eighty on the
            # benchmark - small, but invisible, which is worse.
            from .lexicon import terms
            seeded = terms._seeded()
            check("lexicon seeded from this index", bool(seeded),
                  f"{len(seeded):,} auto-seeded words, "
                  f"{len(terms.manual_terms()):,} hand-written",
                  fatal=False,
                  advice="run: alphaplus-kb lexicon")
        except Exception as exc:
            check("index queryable", False, f"{type(exc).__name__}: {exc}")

    # The HTTP transport, if this installation has ever started one. Not a
    # failure when absent: stdio installations never write a runtime file.
    from .server import web
    runtime = web.read_runtime()
    if runtime:
        ok, detail = web.probe()
        check("http server answering", ok, detail, fatal=False,
              advice="start it with: alphaplus-kb serve-http")
    else:
        print("  --   http server not in use (stdio transport)")

    print()
    if problems:
        print(f"  {len(problems)} problem(s): " + ", ".join(problems))
        print()
        return 1
    print("  Everything checks out.")
    print()
    return 0


# ===========================================================================
# Dispatch
# ===========================================================================

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="alphaplus-kb", description=BANNER,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command")

    p = sub.add_parser("setup", help="pick the source tree and index it")
    p.add_argument("--source", help="skip the prompt and use this path")
    p.add_argument("--data", help="where to keep the index")
    p.add_argument("--transport", choices=("http", "stdio", "ask"),
                   default="ask",
                   help="how Claude reaches the server (default: ask)")
    p.add_argument("--scope", choices=("user", "project", "ask"),
                   default="ask",
                   help="register once for the machine, or per project")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("serve", help="MCP over stdio (Claude launches this)")
    p.add_argument("--selftest", action="store_true",
                   help="exercise every tool and exit")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("serve-http",
                       help="MCP on a loopback port; stays running")
    p.add_argument("--port", type=int,
                   help=f"default {web_default_port()}, or the next free one")
    p.add_argument("--token", help="reuse a token instead of generating one")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_serve_http)

    p = sub.add_parser("index", help="build the index")
    p.add_argument("--limit", type=int, help="only the first N members")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("update", help="re-index only what changed")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verify", action="store_true",
                   help="prove the incremental result matches a full build")
    p.add_argument("--rehash", action="store_true",
                   help="hash every member instead of trusting size and mtime")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("refresh",
                       help="bring the index up to date, doing the least "
                            "work that is correct")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=lambda a: refresh_index(quiet=a.quiet))

    p = sub.add_parser("status", help="coverage and blind spots")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("lexicon", help="re-seed the business-term lexicon")
    p.set_defaults(func=cmd_lexicon)

    p = sub.add_parser("summarise",
                       help="write descriptions of menus, areas and libraries")
    p.add_argument("--level", choices=("menu", "family", "library"))
    p.add_argument("--limit", type=int, help="only the first N of each level")
    p.add_argument("--refresh", action="store_true",
                   help="rewrite pages that are still fresh")
    p.add_argument("--dry-run", action="store_true",
                   help="build every prompt and print its size, call nothing")
    p.add_argument("--estimate", action="store_true",
                   help="what the run will cost, from measured throughput")
    p.add_argument("--status", action="store_true")
    p.set_defaults(func=cmd_summarise)

    p = sub.add_parser("semantic",
                       help="embed the written pages for hybrid routing")
    p.add_argument("--check", action="store_true",
                   help="what exists and whether it is usable")
    p.add_argument("--rebuild", action="store_true",
                   help="drop the collection and embed every page again")
    p.add_argument("--limit", type=int, help="only the first N pages")
    p.set_defaults(func=cmd_semantic)

    p = sub.add_parser("graph", help="write the HTML codebase map")
    p.add_argument("-o", "--out", help="output file")
    p.add_argument("--force-layout", action="store_true")
    p.add_argument("--neo4j", action="store_true")
    p.add_argument("--classic", action="store_true",
                   help="the original viewer, no node build needed")
    p.add_argument("--open", action="store_true",
                   help="open the map in a browser when it is written")
    p.set_defaults(func=cmd_graph)

    p = sub.add_parser("sync",
                       help="point at a source tree and build everything; "
                            "or bring an existing one up to date")
    p.add_argument("--source", metavar="PATH",
                   help="the source tree to index; saved for later runs")
    p.add_argument("--status", action="store_true", help="what is stale; change nothing")
    p.add_argument("--watch", action="store_true",
                   help="keep checking and rebuild when the source moves")
    p.add_argument("--every", type=int, default=30, metavar="SECONDS")
    p.add_argument("--auto", action="store_true",
                   help="shorthand for --watch --every 10")
    p.add_argument("--force", action="store_true")
    p.add_argument("--open", action="store_true",
                   help="open the codebase map when it has been rebuilt")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("export-json",
                       help="write the code graph as JSON, for showing")
    p.add_argument("-o", "--out", help="output file")
    p.add_argument("--include-fields", action="store_true")
    p.add_argument("--include-field-usage", action="store_true",
                   help="~1.5M rows; large")
    p.add_argument("--compact", action="store_true")
    p.set_defaults(func=cmd_export_json)

    p = sub.add_parser("token", help="show or rotate the bearer token")
    p.add_argument("--rotate", action="store_true",
                   help="mint a new token and update any .mcp.json found")
    p.set_defaults(func=cmd_token)

    p = sub.add_parser("ticket",
                       help="read a Jira export (PDF, XML, Word) into "
                            "structured XML")
    p.add_argument("file", help="the ticket export to read")
    p.add_argument("-o", "--out", metavar="DIR",
                   help="project root to write under (default: this folder)")
    p.add_argument("--print", dest="print_xml", action="store_true",
                   help="write the XML to stdout instead of a summary")
    p.set_defaults(func=cmd_ticket)

    p = sub.add_parser("ticket-hook",
                       help="Claude Code hook: structure any ticket file a "
                            "message names")
    p.set_defaults(func=cmd_ticket_hook)

    p = sub.add_parser("config", help="where am I pointed?")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("doctor", help="check the install end to end")
    p.set_defaults(func=cmd_doctor)

    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv:
        # No arguments. A human double-clicked it; a client forgot the verb.
        argv = ["setup"] if _interactive() else ["serve"]

    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n  Interrupted.")
        # Say what an interrupt actually did, because the honest answer is
        # "nothing was damaged" and that is not obvious. A build writes to a
        # NEW file and only replaces the live one on the last step, so Ctrl-C
        # leaves the existing index exactly as it was - but it can leave a
        # part-written `.new` beside it, which has been mistaken for a broken
        # or duplicated knowledge base more than once.
        try:
            from kb import config
            leftovers = sorted(config.FACTS_DB.parent.glob("*.new"))
        except Exception:
            leftovers = []
        print("  Your existing index is untouched - a build only replaces it")
        print("  on the last step, and this run did not get there.")
        if leftovers:
            print("  A part-written file was left behind; it is safe to delete:")
            for f in leftovers:
                print(f"    {f}")
        print("  Re-run the same command to start again.")
        return 130
