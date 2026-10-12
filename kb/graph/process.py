"""
Start a local Neo4j on demand, so nobody has to remember to.

    python -m kb.graph.process --status
    python -m kb.graph.process --start
    python -m kb.graph.process --stop

Off by default.  Turn it on in alphaplus.toml:

    [neo4j.autostart]
    enabled    = true
    neo4j_home = "C:/neo4j/neo4j-community-5.26.0"

Then `kb.graph.driver` starts the server the first time anything actually needs
the graph - a tool call, `kb.graph.load`, `selftest` - and every later call finds
it already up.

## Why it is OFF by default, and must stay that way

On the client's VM Neo4j is a MANAGED SERVICE.  Autostart there would either
fight systemd for the port or spawn a second instance against the same store,
and two processes on one Neo4j store is how a store gets corrupted.  Production
sets `enabled = false` (the default) and lets the platform own the lifecycle.
This exists for a developer laptop, where the alternative is a stack trace and a
five-minute detour every morning.

## The four things that make this safe rather than convenient

**1. It never writes to our stdout.**  This is the one that would break
everything quietly.  The MCP server speaks JSON-RPC over stdout; a child process
printing one line of "Starting Neo4j." into that stream corrupts the protocol,
and the failure looks like a broken tool rather than a stray write.  The child
gets DEVNULL for stdin and a log file for stdout and stderr.  It never inherits
ours.

**2. It probes the port before doing anything.**  Something already listening
means the job is done - the common case, and it costs one TCP connect.  We do
not ask Neo4j, we do not read a pid file, we just see whether the port answers.

**3. It only ever starts a LOOPBACK server.**  You cannot start a process on
another machine, so a remote URI is a configuration error, not a thing to
attempt.

**4. One starter at a time.**  Two MCP sessions opening together would otherwise
both decide the server is down and both launch one.  A lock file with the
starter's pid makes the second one wait for the first instead.

## What it deliberately does not do

Stop the server when we exit.  A JVM that takes 30 seconds to start should not be
thrown away because a tool call finished - the next session wants it warm.  It
outlives us on purpose; `--stop` is explicit.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kb.graph import settings as gs

#: How long to wait for the bolt port after launching, if the config says
#: nothing.  A cold JVM on a laptop with a 1 GB page cache takes 15-25s.
DEFAULT_WAIT = 90

_attempted = False          # once per process, whatever the outcome


class StartFailed(RuntimeError):
    """Could not start Neo4j.  The message is meant for a user to read."""


# ---------------------------------------------------------------------------
# is it up?
# ---------------------------------------------------------------------------

def split_host_port(uri: str, default_port: int = 7687) -> Tuple[str, int]:
    """Host and port out of a bolt URI, IPv6 included."""
    rest = uri.split("://", 1)[-1].rsplit("@", 1)[-1]
    rest = rest.split("/", 1)[0]
    if rest.startswith("["):                       # [::1]:7687
        host, _, tail = rest.partition("]")
        host = host[1:]
        port = int(tail.lstrip(":")) if tail.lstrip(":") else default_port
        return host, port
    if ":" in rest:
        host, _, port_s = rest.rpartition(":")
        try:
            return host, int(port_s)
        except ValueError:
            return rest, default_port
    return rest, default_port


def is_listening(host: str, port: int, timeout: float = 0.4) -> bool:
    """One TCP connect.  No driver, no auth, no JVM - just: does it answer?

    Deliberately not `verify_connectivity()`: that constructs a driver and
    authenticates, which is the expensive thing we are trying to decide whether
    to bother with.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((host, port)) == 0
    except OSError:
        return False


# ---------------------------------------------------------------------------
# where is it installed?
# ---------------------------------------------------------------------------

def launcher(home: Path) -> Optional[Path]:
    """`bin/neo4j` or `bin/neo4j.bat`, whichever this platform has."""
    for name in (("neo4j.bat", "neo4j.ps1") if os.name == "nt" else ("neo4j",)):
        candidate = home / "bin" / name
        if candidate.is_file():
            return candidate
    return None


#: Where a Neo4j install is allowed to live, in the order we trust it.
#:
#: Discovery exists because "not configured" and "configured wrongly" produced
#: the same silence, and on this machine BOTH the server and its JDK were
#: present and neither was findable: Neo4j Desktop hides the server under
#: .Neo4jDesktop2/Cache/dbmss and ships a JDK that is on nobody's PATH. A first
#: run on a VDI must not depend on somebody knowing that.
#:
#: Explicit configuration always wins. This only answers when nothing said.
def _home_candidates() -> List[Path]:
    out: List[Path] = []
    home = Path.home()

    def add(p) -> None:
        try:
            q = Path(p).expanduser()
        except (TypeError, ValueError):
            return
        if q not in out:
            out.append(q)

    # 1. beside the index, which is where WE install it - see `--install-hint`
    try:
        from .. import config
        add(Path(config.DATA_DIR) / "neo4j")
    except Exception:                                     # noqa: BLE001
        pass
    # 2. the per-user location this project installs to
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if base:
        add(Path(base) / "Alphaplus" / "neo4j")
    add(home / ".alphaplus" / "neo4j")
    # 3. Neo4j Desktop's own cache - present on a developer machine
    add(home / ".Neo4jDesktop2" / "Cache" / "dbmss")
    add(home / ".Neo4jDesktop" / "relate-data" / "dbmss")
    # 4. conventional installs
    for p in ("C:/neo4j", "C:/Program Files/Neo4j", "/opt/neo4j",
              "/usr/local/neo4j", "/var/lib/neo4j"):
        add(p)
    add(home / "neo4j")
    return out


def _looks_like_home(p: Path) -> bool:
    return (p / "conf").is_dir() and launcher(p) is not None


def discover_home() -> Optional[Path]:
    """Find a Neo4j installation without being told where it is.

    Each candidate is checked as an install itself and then one level down, so
    a directory holding `neo4j-community-5.26.0` works as well as the install
    directory. Newest name last-sorted wins, so an upgrade is picked up.
    """
    for base in _home_candidates():
        if not base.is_dir():
            continue
        if _looks_like_home(base):
            return base
        try:
            kids = sorted((c for c in base.iterdir() if c.is_dir()),
                          key=lambda c: c.name, reverse=True)
        except OSError:
            continue
        for child in kids:
            if _looks_like_home(child):
                return child
    return None


def resolve_home(cfg: Optional[gs.Settings] = None) -> Optional[Path]:
    """The Neo4j installation: configured, or found.

    Order is deliberate. An explicit setting must win even when it is wrong,
    because silently starting a DIFFERENT server than the one somebody named is
    worse than failing - especially when the wrong one has its own store.
    """
    cfg = cfg or gs.load()
    raw = (cfg.autostart.neo4j_home
           or os.environ.get("ALPHAPLUS_NEO4J_HOME", "")
           or os.environ.get("NEO4J_HOME", ""))
    if raw:
        home = Path(raw).expanduser()
        return home if home.is_dir() else None
    return discover_home()


def discover_java() -> Optional[Path]:
    """A JDK the launcher can use, when the environment has none.

    Neo4j needs Java 17 or 21 and says so only in a log file it writes after
    the shell has already exited - so "no java on PATH" presents as a server
    that starts and vanishes. Finding one is cheaper than explaining that.
    """
    for var in ("JAVA_HOME", "ALPHAPLUS_JAVA_HOME"):
        raw = os.environ.get(var)
        if raw and (Path(raw) / "bin").is_dir():
            return Path(raw)
    if shutil.which("java"):
        return None                      # already on PATH; nothing to inject

    roots = []
    home = Path.home()
    # Neo4j Desktop bundles exactly the JDK its server wants, which makes it
    # the best guess on a machine that has Desktop but no system Java.
    roots.append(home / ".Neo4jDesktop2" / "Cache" / "runtime")
    for base in (os.environ.get("ProgramFiles"), "/usr/lib/jvm",
                 "/Library/Java/JavaVirtualMachines"):
        if base:
            roots.append(Path(base))
            roots.append(Path(base) / "Java")
            roots.append(Path(base) / "Eclipse Adoptium")
    best: Optional[Tuple[int, Path]] = None
    for root in roots:
        if not root.is_dir():
            continue
        try:
            kids = list(root.iterdir())
        except OSError:
            continue
        for cand in kids:
            exe = cand / "bin" / ("java.exe" if os.name == "nt" else "java")
            if not exe.is_file():
                continue
            # Prefer 21, then 17 - the versions Neo4j 5.x supports. A stray
            # Java 8 would start and then fail on a class file version.
            name = cand.name
            score = 21 if "21" in name else 17 if "17" in name else 1
            if best is None or score > best[0]:
                best = (score, cand)
    return best[1] if best else None


def _child_env(cfg: gs.Settings) -> Dict[str, str]:
    """The environment the server is launched with.

    JAVA_HOME is passed explicitly when configured rather than relying on the
    parent's, because the parent is often a frozen executable or an MCP client
    with no JDK on PATH at all - and the launcher's failure in that case is a
    line of shell output we have redirected to a log, so it would look like a
    silent no-start.
    """
    env = dict(os.environ)
    java = None
    if cfg.autostart.java_home:
        java = Path(cfg.autostart.java_home).expanduser()
    else:
        # Nothing configured: find one. Without this the launcher exits before
        # writing anything useful and the failure reads as "Neo4j did not open
        # the port", which sends you looking at ports.
        java = discover_java()
    if java:
        env["JAVA_HOME"] = str(java)
        env["PATH"] = str(java / "bin") + os.pathsep + env.get("PATH", "")
    return env


# ---------------------------------------------------------------------------
# the lock, so two callers do not both launch
# ---------------------------------------------------------------------------

def _start_command(exe: Path) -> List[str]:
    """`console` on Windows, `start` on POSIX.  This is not a stylistic choice.

    On Windows `neo4j.bat start` means "start the WINDOWS SERVICE", and without
    one installed (`neo4j windows-service install`) it prints the directory
    listing, **exits 0, and does nothing**.  Tested: exit 0, then
    `neo4j status` -> "Neo4j is not running".  A launcher that succeeds without
    starting anything is the worst possible thing to build an autostart on, and
    it is why the first version of this appeared to hang for 90 seconds.

    `console` runs the server in the foreground instead - which is exactly what
    is wanted here, because we detach the process ourselves and redirect its
    output to a log.  It needs no service and no admin rights.

    On POSIX `start` daemonises properly and is the right call.

    (If you want Neo4j up before anything runs on a Windows box, install the
    service - that is better than autostart. This is the fallback for a laptop.)
    """
    if os.name == "nt":
        if exe.suffix == ".ps1":
            return ["powershell", "-NoProfile", "-File", str(exe), "console"]
        return [str(exe), "console"]
    return [str(exe), "start"]


def _lock_path(home: Path) -> Path:
    return home / "run" / "alphaplus-autostart.lock"


def _take_lock(home: Path, stale_after: float = 180.0) -> bool:
    """True if we own the right to start it.

    O_EXCL, so the check and the claim are one operation - two processes cannot
    both succeed.  A lock older than `stale_after` is assumed to be a crashed
    starter and is broken, because otherwise one killed process means nobody can
    ever autostart again.
    """
    path = _lock_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return True                       # cannot lock; do not therefore refuse
    try:
        if path.exists() and time.time() - path.stat().st_mtime > stale_after:
            path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        return True
    try:
        os.write(fd, str(os.getpid()).encode())
    finally:
        os.close(fd)
    return True


def _release_lock(home: Path) -> None:
    try:
        _lock_path(home).unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------

def _log_file(home: Path) -> Path:
    logs = home / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path(os.devnull)
    return logs / "alphaplus-autostart.log"


def start(cfg: Optional[gs.Settings] = None, wait: bool = True,
          verbose: bool = False) -> Dict[str, Any]:
    """Launch Neo4j and, by default, wait for the bolt port to answer."""
    cfg = cfg or gs.load()
    host, port = split_host_port(cfg.neo4j.uri)
    out: Dict[str, Any] = {"host": host, "port": port, "started": False}

    if is_listening(host, port):
        out["already"] = True
        return out

    if not cfg.neo4j.is_local:
        raise StartFailed(
            f"cannot start Neo4j on {host} - it is not this machine. "
            f"Autostart only ever launches a loopback server; point "
            f"ALPHAPLUS_NEO4J_URI at a running instance instead.")

    home = resolve_home(cfg)
    if home is None:
        raise StartFailed(
            "neo4j.autostart.neo4j_home is not set, and NEO4J_HOME is not in "
            "the environment. Set one to the unpacked Neo4j directory - the one "
            "containing bin/ and conf/.")

    exe = launcher(home)
    if exe is None:
        raise StartFailed(
            f"{home} does not look like a Neo4j installation - no "
            f"bin/neo4j{'.bat' if os.name == 'nt' else ''} in it.")

    if not _take_lock(home):
        # Somebody else is starting it.  Wait for THEM rather than launching a
        # second JVM against the same store - two processes on one Neo4j store
        # is how a store gets corrupted, so this branch matters more than it
        # looks.
        out["by"] = "another process"
        if not wait:
            # Not an error.  Another starter is exactly the outcome we wanted;
            # the caller just cannot have the graph yet.
            if verbose:
                print("  another process is already starting Neo4j",
                      file=sys.stderr)
            return out
        if verbose:
            print("  another process is starting Neo4j; waiting", file=sys.stderr)
        if _wait_for_port(host, port, cfg.autostart.wait_seconds, verbose):
            out["started"] = True
            return out
        raise StartFailed(
            f"another process holds the autostart lock and Neo4j did not come "
            f"up within {cfg.autostart.wait_seconds}s. If no such process "
            f"exists, delete {_lock_path(home)}")

    log = _log_file(home)
    cmd = _start_command(exe)

    try:
        # NEVER inherit our stdout.  The MCP server's stdout IS the JSON-RPC
        # channel; one line of shell chatter into it corrupts the protocol and
        # the failure looks like a broken tool.
        with open(log, "ab") as fh:
            fh.write(f"\n--- autostart {time.strftime('%Y-%m-%d %H:%M:%S')} "
                     f"{' '.join(cmd)}\n".encode())
            fh.flush()
            kw: Dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": fh,
                "stderr": subprocess.STDOUT,
                "cwd": str(home),
                "env": _child_env(cfg),
            }
            # Detach, so the server outlives us: a JVM that takes 20s to start
            # must not die when the tool call that needed it returns.
            if os.name == "nt":
                kw["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                       | getattr(subprocess,
                                                 "DETACHED_PROCESS", 0))
            else:
                kw["start_new_session"] = True
            proc = subprocess.Popen(cmd, **kw)
        out["pid"] = proc.pid
        out["log"] = str(log)
    except OSError as exc:
        _release_lock(home)
        raise StartFailed(f"could not launch {exe}: {exc}") from None

    if not wait:
        # The lock is deliberately NOT released here.  It is what stops a second
        # process launching a second JVM while this one's is still coming up,
        # and the caller is not waiting around to release it.  `_take_lock`
        # breaks a lock older than 180s, which covers the case where this
        # process dies before the server is ready.
        out["launched"] = True
        return out

    try:
        if _wait_for_port(host, port, cfg.autostart.wait_seconds, verbose):
            out["started"] = True
            return out
    finally:
        _release_lock(home)

    raise StartFailed(
        f"Neo4j did not open {host}:{port} within "
        f"{cfg.autostart.wait_seconds}s. The launcher's own output is in {log} - "
        f"read that rather than guessing; a wrong JAVA_HOME and a port already "
        f"taken look identical from here.")


def _wait_for_port(host: str, port: int, timeout: float,
                   verbose: bool = False) -> bool:
    deadline = time.time() + max(1.0, timeout)
    said = False
    while time.time() < deadline:
        if is_listening(host, port):
            return True
        if verbose and not said:
            print(f"  waiting for Neo4j on {host}:{port} ...", file=sys.stderr)
            said = True
        time.sleep(0.5)
    return False


def stop(cfg: Optional[gs.Settings] = None, verbose: bool = False,
         timeout: float = 60.0) -> bool:
    """Ask Neo4j to shut down.  Explicit only - never on our exit.

    The exit code is NOT the answer.  `neo4j stop` exits 0 for "Neo4j is not
    running", so trusting it reports success having done nothing - which is what
    happened the first time this was tested: it printed "stopped" while a JVM
    with the port open sat there untouched.

    So the port decides.  It also catches the case the launcher genuinely cannot
    handle: an instance started OUTSIDE `neo4j start` - a bare `java` command, or
    another checkout's launcher - is invisible to Neo4j's own pid tracking, and
    no amount of `neo4j stop` will touch it.  Saying so is more use than a
    success message.
    """
    cfg = cfg or gs.load()
    host, port = split_host_port(cfg.neo4j.uri)
    if not is_listening(host, port):
        if verbose:
            print(f"  nothing listening on {host}:{port}", file=sys.stderr)
        return True

    home = resolve_home(cfg)
    if home is None:
        raise StartFailed("neo4j_home is not set, so there is nothing to stop")
    exe = launcher(home)
    if exe is None:
        raise StartFailed(f"no launcher in {home}/bin")
    cmd = ([str(exe), "stop"] if exe.suffix != ".ps1"
           else ["powershell", "-NoProfile", "-File", str(exe), "stop"])
    log = _log_file(home)
    with open(log, "ab") as fh:
        fh.write(f"\n--- stop {time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode())
        fh.flush()
        rc = subprocess.call(cmd, stdin=subprocess.DEVNULL, stdout=fh,
                             stderr=subprocess.STDOUT, cwd=str(home),
                             env=_child_env(cfg))
    _release_lock(home)

    # Shutdown is not instant - the store has to be flushed and closed.
    deadline = time.time() + max(1.0, timeout)
    while time.time() < deadline:
        if not is_listening(host, port):
            if verbose:
                print(f"  {host}:{port} closed", file=sys.stderr)
            return True
        time.sleep(0.5)

    if verbose:
        print(f"  `neo4j stop` exited {rc} but {host}:{port} is STILL open "
              f"after {timeout:.0f}s.", file=sys.stderr)
        print(f"  Most likely that instance was not started by this launcher, "
              f"so Neo4j's own", file=sys.stderr)
        print(f"  pid tracking does not know about it. Find the process on the "
              f"port and stop it", file=sys.stderr)
        print(f"  directly. Launcher output: {log}", file=sys.stderr)
    return False


# ---------------------------------------------------------------------------
# the entry point kb.graph.driver calls
# ---------------------------------------------------------------------------

def ensure_running(cfg: Optional[gs.Settings] = None) -> bool:
    """Launch Neo4j if we are allowed to, and DO NOT WAIT for it.  Never raises.

    Called from `driver.get()`, so it covers every entry point - the MCP server,
    the loader, selftest, bench - rather than only the one somebody remembered
    to wire.

    ## Why it does not wait

    The first version waited `wait_seconds` for the port.  A tool call against a
    stopped Neo4j therefore took **96 seconds** and then fell back to SQLite
    anyway.  That is worse than the problem it was solving: the caller was made
    to sit through a JVM start-up to receive the same answer it would have got
    immediately.

    So: launch, say so, return False.  THIS call falls back to SQLite - which is
    correct, and measurably faster than Neo4j on every query we have.  The JVM
    keeps starting in the background and every later call finds the graph up.
    One fast fallback beats one 96-second wait.

    `--start` still waits, because there a human asked for the server and is
    watching for it to come up.

    Tried once per process, and failure is swallowed after a line on stderr: an
    autostart that cannot work must degrade to the existing "unreachable ->
    SQLite" path, never replace a working fallback with a crash.
    """
    global _attempted
    try:
        cfg = cfg or gs.load()
    except gs.ConfigError:
        return False

    host, port = split_host_port(cfg.neo4j.uri)
    if is_listening(host, port):
        return True
    if not cfg.autostart.enabled or _attempted:
        return False

    _attempted = True
    try:
        out = start(cfg, wait=False, verbose=False)
    except StartFailed as exc:
        print(f"  [graph] autostart failed: {exc}", file=sys.stderr, flush=True)
        return False

    # A JVM needs 15-25s. Give it a moment in case it is quick, then get out of
    # the way - a second is a latency somebody will not notice, ninety is not.
    if _wait_for_port(host, port, 1.0):
        print(f"  [graph] started Neo4j on {host}:{port}",
              file=sys.stderr, flush=True)
        return True

    print(f"  [graph] starting Neo4j on {host}:{port} in the background"
          f"{' (pid ' + str(out['pid']) + ')' if out.get('pid') else ''}; "
          f"this call uses SQLite, later calls will use the graph",
          file=sys.stderr, flush=True)
    return False


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def status(cfg: Optional[gs.Settings] = None) -> str:
    cfg = cfg or gs.load()
    host, port = split_host_port(cfg.neo4j.uri)
    up = is_listening(host, port)
    home = resolve_home(cfg)
    exe = launcher(home) if home else None
    lines = [
        f"  {'ok  ' if up else 'DOWN'} {host}:{port}",
        f"       uri        {cfg.neo4j.uri}",
        f"       loopback   {cfg.neo4j.is_local}",
        f"       autostart  {'enabled' if cfg.autostart.enabled else 'disabled'}"
        f"  (neo4j.autostart.enabled)",
        f"       neo4j_home {home or '(not set)'}",
        f"       launcher   {exe or '(not found)'}",
        f"       java_home  {cfg.autostart.java_home or '(inherit PATH)'}",
        f"       wait       {cfg.autostart.wait_seconds}s",
    ]
    if home:
        lines.append(f"       log        {_log_file(home)}")
    if not up and not cfg.autostart.enabled:
        lines.append("")
        lines.append("  Neo4j is down and autostart is off, so tools will fall "
                     "back to SQLite.")
        lines.append("  Start it yourself, or set neo4j.autostart.enabled = "
                     "true in alphaplus.toml.")
    if not up and cfg.autostart.enabled and home is None:
        lines.append("")
        lines.append("  Autostart is on but neo4j_home is not set, so it cannot "
                     "do anything.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="start, stop or check a local Neo4j")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--start", action="store_true")
    g.add_argument("--stop", action="store_true")
    g.add_argument("--status", action="store_true")
    ap.add_argument("--no-wait", action="store_true",
                    help="launch and return without waiting for the port")
    args = ap.parse_args(argv)

    try:
        cfg = gs.load()
    except gs.ConfigError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1

    try:
        if args.stop:
            ok = stop(cfg, verbose=True)
            print("  stopped" if ok else "  stop reported a failure")
            return 0 if ok else 1
        if args.start:
            out = start(cfg, wait=not args.no_wait, verbose=True)
            if out.get("already"):
                print(f"  already listening on {out['host']}:{out['port']}")
            elif out.get("started"):
                print(f"  Neo4j is up on {out['host']}:{out['port']}"
                      + (f"  (pid {out['pid']})" if out.get("pid") else ""))
            return 0
        print(status(cfg))
        return 0
    except StartFailed as exc:
        print(f"\n  {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
