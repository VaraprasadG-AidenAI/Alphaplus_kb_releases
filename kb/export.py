"""
The code graph as JSON, for looking at.

This is a *display and handover* artefact, not how anything is queried.  The
MCP tools read `facts.db` directly with SQL: they ask narrow questions ("which
programs write CUSMAS?") and get back tens of rows.  Nothing loads this JSON,
and nothing should - a code graph large enough to be useful is far too large to
put in a context window, which is the whole reason the index exists.

So what is it for?

  - showing the client the shape of what was extracted
  - diffing two extractions, or two points in time
  - feeding a tool of theirs that already expects JSON
  - keeping parity with a graph they maintained by hand

Sizes, measured on a 13,366-member codebase:

    structural (the default)      ~110,000 rows      ~40 MB pretty-printed
    with fields                   ~215,000 rows      ~75 MB
    with field usage              ~1,700,000 rows    ~600 MB

Field usage is 1.49M rows on its own, so it is opt-in and warned about.  If
somebody asks for "the JSON" without saying which, they mean the default.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from . import config

#: Edge tables, and how to present each one.  Keeping the SQL here rather than
#: in a loop over table names means each relationship can name its own columns
#: honestly - a call edge and a file open are not the same shape.
_EDGE_QUERIES = [
    ("calls", """
        SELECT caller AS "from", callee AS "to", kind, line, confidence,
               variable, evidence
          FROM call_edge ORDER BY caller, line
     """, "caller -> callee. An empty 'to' with a 'variable' is a call whose "
          "target is decided at run time."),
    ("opens", """
        SELECT program AS "from", file AS "to", mode, verbs, device, line,
               confidence, evidence
          FROM file_usage ORDER BY program, file, mode, line
          -- mode+line as well: one program opens one file in two modes,
          -- and (program, file) alone leaves that order to the store
     """, "program -> file. mode I read, O write, U both. 'SQL <verb>' in "
          "verbs means the access is through embedded SQL."),
    ("copies", """
        SELECT program AS "from", copybook AS "to", kind, line, confidence,
               resolved_from
          FROM copy_edge ORDER BY program, line
     """, "program -> copybook or DDS file whose layout it includes."),
    ("overrides", """
        SELECT cl_program AS "from", logical_name, target_file AS "to",
               library, share, kind, line, confidence
          FROM override ORDER BY cl_program, line
     """, "CL program -> the file a program will really open. This is the "
          "coupling that reading the COBOL alone cannot show."),
    ("writes_format", """
        SELECT program AS "from", device_file AS "to", format_name, verb,
               line, confidence
          FROM format_usage ORDER BY program, line
     """, "program -> screen or report format it writes."),
    ("based_on", """
        SELECT name AS "from", based_on AS "to", member_type
          FROM object WHERE based_on <> '' ORDER BY name
     """, "logical file -> its parent physical file."),
]


def _open(path):
    """The fact store - or one specific SQLite file, when --db names one."""
    from kb import config, store
    if store.backend() == "postgres" and str(path) == str(config.FACTS_DB):
        return store.facts()
    return sqlite3.connect(path)


def _rows(db: sqlite3.Connection, sql: str) -> List[Dict[str, Any]]:
    db.row_factory = sqlite3.Row
    return [{k: r[k] for k in r.keys() if r[k] not in ("", None)}
            for r in db.execute(sql)]


def _stream_rows(db: sqlite3.Connection, sql: str) -> Iterator[Dict[str, Any]]:
    """Row by row, for the tables too large to hold in memory twice."""
    db.row_factory = sqlite3.Row
    for r in db.execute(sql):
        yield {k: r[k] for k in r.keys() if r[k] not in ("", None)}


def build(db_path: Optional[Path] = None, include_fields: bool = False,
          include_field_usage: bool = False) -> Dict[str, Any]:
    """The graph as a plain dict.  Field usage is never included here.

    Field usage is 1.5M rows; it is written by `write()` in a streaming pass
    so it never has to exist as a Python list.
    """
    path = db_path or config.FACTS_DB
    if not path.exists():
        raise FileNotFoundError(
            f"no index at {path} - build it first")
    db = _open(path)

    info = {k: v for k, v in db.execute("SELECT key, value FROM build_info")}
    try:
        unrecognised = json.loads(info.get("unrecognised") or "{}")
    except json.JSONDecodeError:
        unrecognised = {}

    out: Dict[str, Any] = {
        "note": ("A view of the extracted code graph, for reading and "
                 "diffing. The MCP tools query facts.db with SQL and never "
                 "read this file."),
        "source_root": info.get("source_root", ""),
        "built_at": info.get("built_at", ""),
        "members_indexed": int(info.get("members", 0) or 0),
        "not_indexed": unrecognised,
        "confidence": {
            "exact": "read straight from executable source",
            "inferred": "derived - e.g. a library resolved through a CL "
                        "variable, or a table matched by name",
            "unresolved": "we know something is here but not what",
        },
        "members": _rows(db, """
            SELECT name, member_type AS type, language, src_file, subsystem,
                   path, lines, parse_status, based_on, notes
              FROM object ORDER BY name, member_type
        """),
        "edges": {},
        "unresolved": _rows(db, """
            SELECT member, kind, line, raw, reason
              FROM unresolved ORDER BY member, line
        """),
        "header_disagreements": _rows(db, """
            SELECT member, finding FROM header_disagreement ORDER BY member
        """),
    }

    for name, sql, description in _EDGE_QUERIES:
        rows = _rows(db, sql)
        out["edges"][name] = {"description": description,
                              "count": len(rows), "rows": rows}

    if include_fields:
        out["fields"] = _rows(db, """
            SELECT owner, owner_kind, record, name, dtype, length, decimals,
                   text, heading, is_key, level, values_json
              FROM field ORDER BY owner, name
        """)

    out["counts"] = {
        "members": len(out["members"]),
        **{k: v["count"] for k, v in out["edges"].items()},
        "unresolved": len(out["unresolved"]),
        "header_disagreements": len(out["header_disagreements"]),
        **({"fields": len(out["fields"])} if include_fields else {}),
    }
    db.close()
    return out


def write(out_path: Path, db_path: Optional[Path] = None,
          include_fields: bool = False, include_field_usage: bool = False,
          compact: bool = False, quiet: bool = False) -> Path:
    """Write the JSON, streaming field usage if it was asked for."""
    graph = build(db_path, include_fields=include_fields)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    indent = None if compact else 2

    with out_path.open("w", encoding="utf-8", newline="\n") as fh:
        if not include_field_usage:
            json.dump(graph, fh, indent=indent, ensure_ascii=False)
        else:
            # Splice the huge table in without ever materialising it: write
            # the object up to the closing brace, then append one more key.
            body = json.dumps(graph, indent=indent, ensure_ascii=False)
            fh.write(body[:body.rstrip().rfind("}")])
            fh.write(',\n  "field_usage": [\n' if indent else ',"field_usage":[')
            path = db_path or config.FACTS_DB
            db = _open(path)
            first = True
            written = 0
            for row in _stream_rows(db, """
                    SELECT program, field, direction, file, confidence,
                           n, lines, paragraphs
                      FROM field_usage ORDER BY program, field, direction
                 """):
                if not first:
                    fh.write(",\n    " if indent else ",")
                elif indent:
                    fh.write("    ")
                json.dump(row, fh, ensure_ascii=False)
                first = False
                written += 1
                if not quiet and written % 250_000 == 0:
                    print(f"    {written:,} field usage rows ...")
            db.close()
            fh.write("\n  ]\n}\n" if indent else "]}")

    if not quiet:
        size = out_path.stat().st_size / (1024 * 1024)
        print()
        print(f"  wrote {out_path}  ({size:,.1f} MB)")
        for key, value in graph["counts"].items():
            print(f"    {key:<22}{value:>12,}")
        if include_field_usage:
            print("    field_usage           (streamed)")
        print()
        print("  This file is for reading and diffing. The MCP tools query")
        print("  the index directly - nothing loads this JSON.")
        print()
    return out_path


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="export the code graph as JSON, for showing")
    ap.add_argument("-o", "--out", type=Path,
                    default=None, help="output file")
    ap.add_argument("--include-fields", action="store_true",
                    help="add field definitions (~104k rows)")
    ap.add_argument("--include-field-usage", action="store_true",
                    help="add field-level read/write rows (~1.5M rows, large)")
    ap.add_argument("--compact", action="store_true",
                    help="no indentation - much smaller, unreadable")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    out = args.out or (config.DATA_DIR / "code-graph.json")
    try:
        write(out, include_fields=args.include_fields,
              include_field_usage=args.include_field_usage,
              compact=args.compact, quiet=args.quiet)
    except FileNotFoundError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
