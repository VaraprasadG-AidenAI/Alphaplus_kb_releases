"""Write the process map as one self-contained HTML file, and serve it.

    python -m kb.calltree.page                 # write it
    python -m kb.calltree.page --serve 8800    # write it and serve it

The output has React, the app and the data all inlined, for the same reason
`kb/viz` does: the page has to open from a `file://` URL on a VDI with no
network. `--serve` exists only because a browser will not honour hash routing
consistently over `file://`, and the flows are worth linking to.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

from .. import config
from . import export

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SRC = ROOT / "viz-src"
SHELL = SRC / "process-shell.html"
BUNDLE = HERE / "process-bundle.js"
OUT = ROOT / "process-map.html"


def slim(doc: dict) -> dict:
    """Drop what the page never reads. 794 kB -> 462 kB."""
    def node(n: dict) -> dict:
        o = {"m": n["member"], "t": n["title"], "l": n["line"],
             "c": n["confidence"], "ty": n["type"], "ln": n["lines"],
             "v": n["via"], "p": n["paragraph"], "u": n["ubiquitous"]}
        if n["children"]:
            o["k"] = [node(k) for k in n["children"]]
        return o

    caps = []
    for c in doc["capabilities"]:
        caps.append({
            "key": c["key"], "t": c["title"], "l": c["line"],
            "i": c["interactive"], "b": c["batch"],
            "g": [{"key": g["key"], "title": g["title"], "line": g["line"],
                   "members": g["members"], "shared": g["shared"],
                   "count": g["count"]}
                  for g in c.get("groups", []) if g["title"]],
            "f": [{"key": f["key"], "kind": f["kind"], "lab": f["label"],
                   "menu": f["menu"], "opt": f["option"], "sub": f["submitted_by"],
                   "n": f["members"],
                   "r": node(f["root"]) if f["root"] else None,
                   "e": [{"n": e["name"], "ty": e["type"], "v": e["verbs"],
                          "w": e["writes"]} for e in f["entities"]]}
                  for f in c["features"]],
        })
    return {"totals": doc["totals"], "caps": caps}


def bundle(force: bool = False) -> str:
    """esbuild the app. Falls back to a previous bundle if node is absent."""
    if BUNDLE.exists() and not force:
        return BUNDLE.read_text(encoding="utf-8")
    if not shutil.which("node"):
        raise SystemExit("node is not on PATH and no bundle has been built yet")
    print("  bundling...")
    subprocess.run(
        ["node", "-e", f"""
import('esbuild').then(async esbuild => {{
  const out = await esbuild.build({{
    entryPoints: ['src/process.jsx'], bundle: true, minify: true,
    format: 'iife', target: 'es2018', outfile: {json.dumps(str(BUNDLE))},
    loader: {{'.jsx':'jsx'}}, jsx: 'automatic',
    define: {{'process.env.NODE_ENV':'"production"'}},
    legalComments: 'none', metafile: true }});
  const b = Object.values(out.metafile.outputs)[0].bytes;
  console.log('  process-bundle.js  ' + (b/1024).toFixed(1) + ' kB');
}});"""],
        cwd=SRC, check=True)
    return BUNDLE.read_text(encoding="utf-8")


def write(capabilities: Optional[Sequence[str]] = None,
          out: Path = OUT, rebuild: bool = False) -> Path:
    from ..store import connect
    with connect() as db:
        doc = export.document(db, capabilities)

    data = json.dumps(slim(doc), separators=(",", ":"))
    # `</script>` inside the JSON would close our own tag. There is none in
    # this data, but there is no reason to depend on that staying true.
    data = data.replace("</", "<\\/")

    html = SHELL.read_text(encoding="utf-8")
    html = html.replace("__DATA__", data)
    html = html.replace("__BUNDLE__", bundle(rebuild))
    out.write_text(html, encoding="utf-8", newline="\n")

    t = doc["totals"]
    print(f"  {t['capabilities']} capabilities, {t['flows']:,} flows "
          f"({t['interactive']:,} interactive, {t['batch']:,} batch)")
    print(f"  {t['named']:,}/{t['members']:,} members named")
    print(f"  {out}  ({out.stat().st_size/1024:.0f} kB)")
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("capabilities", nargs="*")
    ap.add_argument("-o", "--out", default=str(OUT))
    ap.add_argument("--rebuild", action="store_true", help="re-run esbuild")
    ap.add_argument("--serve", type=int, metavar="PORT", default=0)
    args = ap.parse_args(argv)

    out = Path(args.out)
    write(args.capabilities, out, args.rebuild)

    if args.serve:
        import functools
        import http.server
        import socketserver
        handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                    directory=str(out.parent))
        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer(("127.0.0.1", args.serve), handler) as srv:
            print(f"\n  http://127.0.0.1:{args.serve}/{out.name}")
            print("  ctrl-c to stop")
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                print("\n  stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
