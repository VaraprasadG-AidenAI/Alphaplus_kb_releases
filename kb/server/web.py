"""
MCP over HTTP, for the case where the server is a long-running process rather
than something Claude spawns.

Two transports, one implementation.  `server.handle()` takes a JSON-RPC
message and returns a JSON-RPC response with no knowledge of how it arrived,
so stdio and HTTP are both thin shells around it.

Why HTTP as well as stdio:

  stdio  Claude starts the exe itself, as a child process, once per session.
         Simplest thing that works, nothing to configure, no port, no token.

  http   The exe is started once - by a person, or by a scheduled task - and
         stays up.  Claude connects to it.  The index stays open between
         sessions, several VS Code windows share one server, and there is a
         visible process an operator can check.  This is what you want when
         the server is treated as a service.

Named web.py rather than http.py so that `from . import http` can never
shadow the standard library module of the same name.

SECURITY.  The listener binds 127.0.0.1 only - never a routable address - and
every request must carry a bearer token generated at setup.  Both matter even
on a single-user VDI: without the token any local process, including a browser
page, could read the whole index; without the loopback bind the index would be
readable from the network.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import config
from . import server as rpc

#: Where the running server records how to reach it.  Written on start,
#: removed on a clean stop.  `setup` reads it to build the client config and
#: `doctor` reads it to say whether the server is up.
RUNTIME_NAME = "runtime.json"

#: Default port.  Nothing standard about it; high enough to need no privilege,
#: unusual enough not to collide with the obvious things.
DEFAULT_PORT = 8787

#: The path Claude posts to.  Kept explicit rather than accepting any path, so
#: a stray request to / does not look like a protocol error.
MCP_PATH = "/mcp"

MAX_BODY = 4 * 1024 * 1024      # a JSON-RPC call; nothing here is large

#: Longest chunk-size line we will read.  A chunk header is a few bytes;
#: anything larger is a malformed or hostile stream, not a real request.
_CHUNK_LINE_MAX = 1024


def runtime_path() -> Path:
    return config.DATA_DIR / RUNTIME_NAME


def read_runtime() -> Dict[str, Any]:
    path = runtime_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_runtime(port: int, token: str) -> Path:
    path = runtime_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "url": f"http://127.0.0.1:{port}{MCP_PATH}",
        "port": port,
        "token": token,
        "pid": os.getpid(),
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, indent=2), encoding="utf-8")
    # The token is a credential. Best effort on Windows, where chmod is
    # mostly advisory - the real protection is that it lives under the
    # user's own profile.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _clear_runtime() -> None:
    try:
        runtime_path().unlink()
    except OSError:
        pass


#: The bearer token lives here, and it must OUTLIVE the server process.
#: runtime.json is deleted on a clean stop - it is the "is it running" marker -
#: so a token kept only there was regenerated on every restart, silently
#: invalidating the .mcp.json the client had already been given.  One restart
#: and Claude stops working for a reason nobody can see.
TOKEN_NAME = "mcp-token"


def token_path() -> Path:
    return config.DATA_DIR / TOKEN_NAME


def new_token() -> str:
    return secrets.token_urlsafe(32)


def persistent_token(regenerate: bool = False) -> str:
    """The token for this installation, minted once and then reused."""
    path = token_path()
    if not regenerate:
        try:
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError:
            pass
    token = new_token()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:
        os.chmod(path, 0o600)          # advisory on Windows; the profile is the real guard
    except OSError:
        pass
    return token


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def pick_port(preferred: int = DEFAULT_PORT) -> int:
    """The preferred port, or the next free one above it."""
    for candidate in range(preferred, preferred + 40):
        if port_is_free(candidate):
            return candidate
    raise RuntimeError(
        f"no free port between {preferred} and {preferred + 39}")


# ===========================================================================
# The handler
# ===========================================================================

class _Handler(BaseHTTPRequestHandler):
    server_version = f"alphaplus-kb/{rpc.SERVER_VERSION}"
    protocol_version = "HTTP/1.1"

    #: set on the server instance by serve_http()
    token: str = ""

    # -- plumbing ---------------------------------------------------------

    def handle_one_request(self) -> None:
        """As the base class, but quiet about clients that just go away.

        A browser tab closing, or a client that disposes its connection after
        reading the response, resets the socket mid-read.  The stdlib prints a
        full traceback for that, which on an operator's screen looks exactly
        like the server crashing.  It is neither our fault nor a fault.
        """
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError,
                BrokenPipeError, TimeoutError):
            self.close_connection = True

    def log_message(self, fmt: str, *args) -> None:
        # Default logging writes to stderr for every request, which fills a
        # console with noise. Only surface failures.
        if args and str(args[0]).startswith(("4", "5")):
            sys.stderr.write("  %s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, payload: Optional[Dict[str, Any]] = None,
              extra: Optional[Dict[str, str]] = None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        if code >= 400:
            # We reject 401/403/404 before reading the request body, so the
            # body is still sitting in the socket.  On a keep-alive connection
            # the next read starts mid-payload and the server parses a chunk
            # size line as a request line - "Bad request syntax ('ab')" - and
            # every later request on that connection fails for no visible
            # reason.  Closing after an error is simpler and safer than
            # draining a body we already decided not to trust.
            self.close_connection = True
        self.send_response(code)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _rpc_error(self, code: int, rpc_code: int, message: str,
                   request_id: Any = None) -> None:
        self._send(code, rpc._error(request_id, rpc_code, message))

    # -- checks -----------------------------------------------------------

    def _authorised(self) -> bool:
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        # Constant time: a timing side channel on a localhost token is a
        # small risk, but it costs one function call to remove it.
        return hmac.compare_digest(header[len(prefix):], self.token)

    def _origin_ok(self) -> bool:
        """Reject cross-origin *browser* requests (DNS rebinding).

        The threat is a web page causing requests to this server.  Such a page
        always presents an http or https Origin naming a host that is not us.
        Everything else is not a browser page:

          - no Origin at all: curl, a script, most MCP clients
          - a loopback http Origin: something served from this machine
          - a non-web scheme: vscode-file://, vscode-webview://, app://,
            file:// - an installed application, not a page

        Claude Code runs inside VS Code, so it sends an app origin.  An
        earlier version of this check allowed only loopback http origins and
        answered 403 to every request Claude made, before the token was even
        looked at.  Narrowing the check to http(s) keeps the defence and stops
        rejecting the client we exist to serve.
        """
        origin = self.headers.get("Origin")
        if not origin or origin == "null":
            return True
        lowered = origin.lower()
        if not lowered.startswith(("http://", "https://")):
            # An application scheme. Not a web page, so not the threat.
            return True
        port = self.server.server_address[1]
        allowed = {
            f"http://127.0.0.1:{port}", f"http://localhost:{port}",
            f"http://[::1]:{port}",
        }
        if lowered in allowed:
            return True
        # A loopback origin on any port is still this machine, and the token
        # is what actually guards the data.
        host = lowered.split("://", 1)[1].split("/", 1)[0].rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost", "[::1]", "::1")

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:                      # noqa: N802
        if self.path.rstrip("/") == "/health":
            # Deliberately unauthenticated and deliberately free of detail:
            # enough for an operator or a start-up script to see the process
            # is listening, nothing that helps an unauthorised caller.
            self._send(200, {"status": "ok", "server": rpc.SERVER_NAME,
                             "version": rpc.SERVER_VERSION})
            return
        if self.path.rstrip("/") == MCP_PATH.rstrip("/"):
            # Streamable HTTP allows a GET for a server-initiated SSE stream.
            # This server never pushes anything, so decline it clearly rather
            # than holding a connection open forever.
            self._send(405, {"error": "this server does not open SSE streams; "
                                      "POST JSON-RPC to " + MCP_PATH})
            return
        self._send(404, {"error": "not found"})

    def _read_body(self) -> Optional[bytes]:
        """The request body, however the client chose to frame it.

        Node's fetch sends a streamed body as Transfer-Encoding: chunked with
        no Content-Length.  Reading only Content-Length gave 0 bytes, which
        failed to parse and answered 400 to every request Claude Code made -
        so chunked has to be decoded here rather than assumed away.

        Returns None when the body is malformed or too large; the caller
        answers with the appropriate error.
        """
        encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in encoding:
            parts: List[bytes] = []
            total = 0
            while True:
                line = self.rfile.readline(_CHUNK_LINE_MAX)
                if not line:
                    return None                       # truncated
                # A chunk header may carry extensions after a semicolon.
                head = line.strip().split(b";", 1)[0]
                try:
                    size = int(head, 16)
                except ValueError:
                    return None
                if size == 0:
                    # Consume trailers up to the blank line that ends them.
                    while True:
                        trailer = self.rfile.readline(_CHUNK_LINE_MAX)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                total += size
                if total > MAX_BODY:
                    return None
                chunk = self.rfile.read(size)
                if len(chunk) != size:
                    return None
                parts.append(chunk)
                self.rfile.read(2)                    # the CRLF after a chunk
            return b"".join(parts)

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length > MAX_BODY:
            return None
        return self.rfile.read(length) if length else b""

    def do_DELETE(self) -> None:                   # noqa: N802
        """Session teardown.

        Streamable HTTP lets a client DELETE the endpoint to end its session.
        This server keeps no per-session state, so there is nothing to discard
        - but answering 501 makes a well-behaved client look broken, so
        acknowledge it.
        """
        if self.path.rstrip("/") != MCP_PATH.rstrip("/"):
            self._send(404, {"error": "not found"})
            return
        if not self._authorised():
            self._send(401, {"error": "missing or invalid bearer token"},
                       extra={"WWW-Authenticate": "Bearer"})
            return
        self._send(204)

    def do_POST(self) -> None:                     # noqa: N802
        if self.path.rstrip("/") != MCP_PATH.rstrip("/"):
            self._send(404, {"error": f"POST {MCP_PATH}"})
            return
        if not self._origin_ok():
            self._rpc_error(403, -32600, "origin not allowed")
            return
        if not self._authorised():
            self._send(401, {"error": "missing or invalid bearer token"},
                       extra={"WWW-Authenticate": "Bearer"})
            return

        raw = self._read_body()
        if raw is None:
            self._rpc_error(400, -32600,
                            "bad or oversized request body")
            return
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._rpc_error(400, -32700, f"parse error: {exc}")
            return

        # A batch is a list. Handle it rather than rejecting it; clients are
        # allowed to send one.
        if isinstance(message, list):
            responses = []
            for item in message:
                if not isinstance(item, dict):
                    continue
                reply = self._dispatch(item)
                if reply is not None:
                    responses.append(reply)
            if not responses:
                self._send(202)
            else:
                self._send(200, responses)      # type: ignore[arg-type]
            return

        if not isinstance(message, dict):
            self._rpc_error(400, -32600,
                            "invalid request: expected a JSON object")
            return

        reply = self._dispatch(message)
        if reply is None:
            # A notification. 202 with no body is the correct answer.
            self._send(202)
            return
        self._send(200, reply)

    def _dispatch(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            return rpc.handle(message)
        except Exception:                       # never kill the server
            import traceback
            return rpc._error(message.get("id"), -32603,
                              traceback.format_exc(limit=3))


# ===========================================================================
# Entry point
# ===========================================================================

def serve_http(port: Optional[int] = None, token: Optional[str] = None,
               quiet: bool = False) -> int:
    """Run the MCP server on a loopback port until interrupted."""
    chosen = port or pick_port(DEFAULT_PORT)
    if port and not port_is_free(port):
        print(f"  port {port} is already in use", file=sys.stderr)
        existing = read_runtime()
        if existing.get("port") == port:
            print(f"  a server may already be running (pid "
                  f"{existing.get('pid')}) - check with: "
                  f"alphaplus-kb doctor", file=sys.stderr)
        return 1

    secret = token or persistent_token()

    handler = type("_BoundHandler", (_Handler,), {"token": secret})
    httpd = ThreadingHTTPServer(("127.0.0.1", chosen), handler)
    httpd.daemon_threads = True

    written = _write_runtime(chosen, secret)

    if not quiet:
        print()
        print("  Alphaplus knowledge base - MCP server")
        print("  " + "=" * 58)
        print(f"  listening  http://127.0.0.1:{chosen}{MCP_PATH}")
        print(f"  source     {config.SOURCE_ROOT}")
        print(f"  index      {config.FACTS_DB}")
        print(f"  runtime    {written}")
        print()
        print("  Loopback only. Every request needs the bearer token in")
        print("  the runtime file above - Claude Code reads it from")
        print("  .mcp.json, which setup wrote for you.")
        print()
        print("  Leave this window open. Ctrl+C to stop.")
        print()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        if not quiet:
            print("\n  stopping ...")
    finally:
        httpd.shutdown()
        httpd.server_close()
        _clear_runtime()
    return 0


def probe(url: Optional[str] = None, token: Optional[str] = None
          ) -> Tuple[bool, str]:
    """Is a server answering?  Returns (ok, detail).

    Used by `doctor`.  Reads the runtime file when not told otherwise, so it
    checks the server this installation actually started.
    """
    import urllib.error
    import urllib.request

    runtime = read_runtime()
    target = url or runtime.get("url")
    if not target:
        return False, "no server has been started from this installation"
    secret = token or runtime.get("token", "")

    health = target.rsplit(MCP_PATH, 1)[0] + "/health"
    try:
        with urllib.request.urlopen(health, timeout=3) as response:
            if response.status != 200:
                return False, f"health returned HTTP {response.status}"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"not answering on {health} ({exc})"

    # Health needs no token; prove the authenticated path works too.
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": "tools/list", "params": {}}).encode()
    request = urllib.request.Request(
        target, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {secret}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return False, f"authenticated call returned HTTP {exc.code}"
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return False, f"authenticated call failed ({exc})"

    tools = (payload.get("result") or {}).get("tools") or []
    return True, f"{len(tools)} tools available on {target}"
