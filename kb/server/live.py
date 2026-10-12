"""
The live query API the codebase map talks to.

    python -m kb.server.live              # start it, print the URL
    python -m kb.server.live --port 8788
    python -m kb.server.live --status

WHY THIS EXISTS, AND WHY IT IS NOT `web.py`
-------------------------------------------
`kb/server/web.py` is MCP over HTTP: JSON-RPC, for Claude. This is a much
smaller thing for a *browser* - the map opens as a file:// page and needs three
answers no static file can give:

    is anything live?      /health
    answer this question   /query      (named queries, never raw Cypher)
    has the map changed?   /payload    (conditional, so a poll costs nothing)

The map works with none of this. That is the point: it is one self-contained
HTML file that opens on a VDI with no network, and it stays that way. When this
server IS reachable the map upgrades - live answers straight from Neo4j, and a
snapshot that refreshes itself when the source changes. When it is not, the map
degrades to the inlined snapshot and SAYS SO with the build time, rather than
quietly showing stale data as if it were current.

THE SECURITY POSTURE, STATED PLAINLY
------------------------------------
1. Loopback only. Never binds a routable address. There is no flag to change
   that, because a graph of a customer's source is not something to serve to a
   network by accident.
2. NAMED QUERIES ONLY. The browser sends a name and typed parameters; the
   Cypher lives here. Accepting Cypher from a page would be a remote-code
   surface on the customer's knowledge base, and "it is only localhost" is not
   an argument - a page in any tab can post to localhost.
3. Read-only sessions. The driver is asked for READ access, so a bug here
   cannot write to the graph.
4. Origin checked. A cross-origin *browser* page is rejected; file:// (which
   sends `null`) and loopback pages are allowed, because those are the map.
5. Bearer token OPTIONAL, and off by default. A file:// page cannot hold a
   secret - there is nowhere to put it that the page's own reader cannot see -
   so requiring one would be security theatre that also breaks the product.
   `--token` turns it on for deployments that front this with something else.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
No writes. No schema changes. No summaries - those come from the vector store
and are a separate concern. No raw Cypher. No non-loopback bind.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import config

#: Deliberately not 8787 - `kb serve-http` owns that, and two servers fighting
#: over one port is a confusing failure for something meant to be optional.
DEFAULT_PORT = 8788

#: A poll every few seconds must be nearly free, so /payload is conditional and
#: /health is a couple of counters. Both are cheap enough to answer from the
#: main thread without a cache.
MAX_BODY = 64 * 1024

RUNTIME_NAME = "live.json"


# ---------------------------------------------------------------------------
# the named queries
# ---------------------------------------------------------------------------
# The whole API surface, in one table. Adding a question means adding a row
# here, which is the point: there is one place to review.
#
# Each entry is (function, parameter spec). The spec is checked before the
# query runs - a wrong type is a 400, not a Cypher error, and never a way to
# smuggle something through.

def _q_registry() -> Dict[str, Tuple[Callable[..., Any], Dict[str, type]]]:
    from ..graph import queries as Q
    return {
        "who_calls":        (Q.who_calls,        {"name": str, "depth": int}),
        "what_it_calls":    (Q.what_it_calls,    {"name": str, "depth": int}),
        "path_between":     (Q.path_between,     {"frm": str, "to": str}),
        "blast_radius":     (Q.blast_radius,     {"name": str, "depth": int}),
        "menu_path_to":     (Q.menu_path_to,     {"name": str, "depth": int}),
        "who_touches_file": (Q.who_touches_file, {"file": str, "mode": str}),
        "where_is_field_used": (Q.where_is_field_used, {"field": str}),
        "field_definitions":   (Q.field_definitions,   {"field": str}),
        "resolve_overrides":   (Q.resolve_overrides,   {"program": str}),
        "program_structure":   (Q.program_structure,   {"member": str, "depth": int}),
        "find_object":      (Q.find_object,      {"name": str}),
        "search_text":      (Q.search_text,      {"terms": list, "limit": int}),
    }


def _coerce(spec: Dict[str, type], params: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only declared parameters, and only at the declared type.

    Unknown keys are DROPPED rather than passed through. A query function is
    ordinary Python, so an unexpected keyword would be a TypeError at best and
    a surprise at worst.
    """
    out: Dict[str, Any] = {}
    for key, want in spec.items():
        if key not in params or params[key] is None:
            continue
        val = params[key]
        if want is int:
            try:
                out[key] = int(val)
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number")
        elif want is str:
            if not isinstance(val, str):
                raise ValueError(f"{key} must be text")
            # A name is a member name. Length-cap it so a pathological string
            # cannot become a pathological query.
            out[key] = val[:200]
        elif want is list:
            if not isinstance(val, list):
                raise ValueError(f"{key} must be a list")
            out[key] = [str(v)[:100] for v in val[:40]]
        else:
            out[key] = val
    return out


# ---------------------------------------------------------------------------
# snapshot freshness
# ---------------------------------------------------------------------------

def _map_path() -> Path:
    return Path(config.DATA_DIR) / "graph.html"


def _map_stamp() -> Tuple[str, int]:
    """(etag, size) for the built map, or ('', 0) when it is not there.

    mtime+size rather than a hash: the map is ~4 MB and this is polled. The
    same stat cache the indexer uses for source members, for the same reason.
    """
    p = _map_path()
    try:
        st = p.stat()
    except OSError:
        return "", 0
    return f'"{int(st.st_mtime_ns)}-{st.st_size}"', st.st_size


def _payload_of(html: str) -> str:
    """Pull the JSON payload back out of the built map.

    The map is the single source of truth for what the viewer shows, so the
    live refresh serves exactly what a fresh download would - there is no
    second extraction path to drift from the real one.
    """
    open_tag = '<script id="graph-data" type="application/json">'
    i = html.find(open_tag)
    if i < 0:
        return ""
    start = i + len(open_tag)
    end = html.find("</script>", start)
    return html[start:end] if end > start else ""


# ---------------------------------------------------------------------------
# the server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    token: str = ""
    started: float = 0.0

    def log_message(self, fmt: str, *args) -> None:      # noqa: A003
        pass                                             # stay quiet

    # -- plumbing ----------------------------------------------------------
    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        # A file:// page sends `null`. Echoing the origin (rather than `*`)
        # keeps the response usable if a token is ever configured, because `*`
        # and credentials are mutually exclusive in CORS.
        self.send_header("Access-Control-Allow-Origin", origin or "*")
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "600")

    def _send(self, code: int, body: Any = None, *, raw: Optional[bytes] = None,
              ctype: str = "application/json", extra: Optional[Dict[str, str]] = None) -> None:
        data = raw if raw is not None else json.dumps(body or {}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _origin_ok(self) -> bool:
        """Reject a cross-origin browser page; allow the map.

        Same reasoning as web.py: only an http(s) Origin naming somewhere that
        is not us is a page we did not serve. `null` is file://, which IS the
        map, and an app scheme is an installed application.
        """
        origin = self.headers.get("Origin")
        if not origin or origin == "null":
            return True
        if not origin.startswith(("http://", "https://")):
            return True
        host = origin.split("//", 1)[1].split("/", 1)[0].split(":")[0]
        return host in ("127.0.0.1", "localhost", "[::1]", "::1")

    def _authorised(self) -> bool:
        if not self.token:
            return True                       # tokenless by default - see docstring
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:], self.token)

    # -- routes ------------------------------------------------------------
    def do_OPTIONS(self) -> None:             # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:                 # noqa: N802
        if not self._origin_ok():
            return self._send(403, {"error": "cross-origin"})
        if not self._authorised():
            return self._send(401, {"error": "unauthorised"})
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/", "/health"):
            return self._health()
        if path == "/payload":
            return self._payload()
        self._send(404, {"error": "no such endpoint"})

    def do_POST(self) -> None:                # noqa: N802
        if not self._origin_ok():
            return self._send(403, {"error": "cross-origin"})
        if not self._authorised():
            return self._send(401, {"error": "unauthorised"})
        if self.path.split("?", 1)[0].rstrip("/") != "/query":
            return self._send(404, {"error": "no such endpoint"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._send(400, {"error": "bad length"})
        if length <= 0 or length > MAX_BODY:
            return self._send(400, {"error": "body missing or too large"})
        try:
            req = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._send(400, {"error": "body is not JSON"})
        self._query(req if isinstance(req, dict) else {})

    # -- handlers ----------------------------------------------------------
    def _health(self) -> None:
        """Everything the map needs to decide what mode to run in."""
        etag, size = _map_stamp()
        out: Dict[str, Any] = {
            "ok": True,
            "service": "alphaplus-live",
            "source": str(config.SOURCE_ROOT),
            "map": {"etag": etag, "bytes": size},
            "queries": sorted(_q_registry()),
            "uptime": round(time.time() - Handler.started, 1),
        }
        # Neo4j reachability is the interesting half, and it must never throw:
        # "the graph is down" is an ANSWER, not an error, and the map is built
        # to degrade on it.
        try:
            from ..graph import driver
            t0 = time.perf_counter()
            rows = driver.read("MATCH (n) RETURN count(n) AS nodes", cap=1)
            out["neo4j"] = {
                "up": True,
                "nodes": rows[0]["nodes"] if rows else 0,
                "ping_ms": round((time.perf_counter() - t0) * 1000, 1),
            }
        except Exception as exc:                        # noqa: BLE001
            out["neo4j"] = {"up": False, "why": type(exc).__name__}
        self._send(200, out)

    def _payload(self) -> None:
        """The current graph payload, conditionally.

        The map polls this. A poll that has nothing to say must cost nothing,
        so an unchanged map answers 304 with no body - which is why the client
        can poll every few seconds without anybody noticing.
        """
        etag, _ = _map_stamp()
        if not etag:
            return self._send(503, {"error": "no map has been built"})
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self._cors()
            self.end_headers()
            return
        try:
            html = _map_path().read_text(encoding="utf-8")
        except OSError as exc:
            return self._send(500, {"error": f"cannot read the map: {exc}"})
        body = _payload_of(html)
        if not body:
            return self._send(500, {"error": "the map has no payload block"})
        self._send(200, raw=body.encode("utf-8"), extra={"ETag": etag})

    def _query(self, req: Dict[str, Any]) -> None:
        name = str(req.get("name") or "")
        reg = _q_registry()
        if name not in reg:
            # Name the alternatives. A 404 that does not say what IS available
            # turns a typo into a debugging session.
            return self._send(400, {"error": f"unknown query {name!r}",
                                    "known": sorted(reg)})
        fn, spec = reg[name]
        raw = req.get("params")
        try:
            params = _coerce(spec, raw if isinstance(raw, dict) else {})
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        t0 = time.perf_counter()
        try:
            rows = fn(**params)
        except Exception as exc:                        # noqa: BLE001
            # The graph being unreachable is the expected failure, and the map
            # falls back on it. 503 rather than 500 so the client can tell
            # "ask again later" from "this query is wrong".
            return self._send(503, {"error": type(exc).__name__, "detail": str(exc)[:300]})
        self._send(200, {"name": name, "rows": rows,
                         "ms": round((time.perf_counter() - t0) * 1000, 1)})


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def pick_port(preferred: int = DEFAULT_PORT) -> int:
    for port in range(preferred, preferred + 40):
        if port_is_free(port):
            return port
    raise RuntimeError(f"no free port in {preferred}..{preferred + 39}")


def runtime_path() -> Path:
    return Path(config.DATA_DIR) / RUNTIME_NAME


def read_runtime() -> Dict[str, Any]:
    try:
        return json.loads(runtime_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def serve(port: Optional[int] = None, token: str = "",
          quiet: bool = False) -> int:
    port = port or pick_port()
    Handler.token = token
    Handler.started = time.time()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True

    # The map discovers the server by trying the default port and then reading
    # this file is NOT possible from file:// - so the port really does have to
    # be predictable. This file is for `--status` and for a human.
    try:
        runtime_path().write_text(json.dumps({
            "url": f"http://127.0.0.1:{port}",
            "port": port, "pid": os.getpid(),
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "token": bool(token),
        }, indent=2), encoding="utf-8")
    except OSError:
        pass

    if not quiet:
        print(f"\n  Live query API   http://127.0.0.1:{port}")
        print( "    /health        is anything live, and how fresh is the map")
        print( "    /query         named queries, POST {name, params}")
        print( "    /payload       the current graph payload, conditional")
        print( "\n  The map upgrades itself when this is reachable and falls")
        print( "  back to its inlined snapshot when it is not. Ctrl-C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        if not quiet:
            print("\n  stopped")
    finally:
        try:
            runtime_path().unlink()
        except OSError:
            pass
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="the live query API the codebase map talks to")
    ap.add_argument("--port", type=int, help=f"default {DEFAULT_PORT}, or the next free")
    ap.add_argument("--token", help="require this bearer token (off by default)")
    ap.add_argument("--status", action="store_true", help="is it running?")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.status:
        rt = read_runtime()
        if not rt:
            print("  not running (no live.json)")
            return 1
        alive = not port_is_free(int(rt.get("port") or 0))
        print(f"  {rt.get('url')}  pid {rt.get('pid')}  "
              f"{'RUNNING' if alive else 'STALE - nothing is listening'}")
        return 0 if alive else 1

    return serve(args.port, args.token or "", args.quiet)


if __name__ == "__main__":
    sys.exit(main())
