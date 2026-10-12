"""
Export gold answers from the generator catalog.

**This is the only file under kb/ permitted to import from tools/.**  It is
run by hand, its output is checked in, and nothing in the pipeline imports it.
Rule 0 compliance is tested with:

    grep -rl "tools\\." kb/ --include=*.py

which must return this file and nothing else.

Why it exists: to score a benchmark we need to know the right answer, and the
right answer has to come from somewhere the knowledge base has never seen.
The generator catalog is that place - it is the specification the source was
written from, so it is genuinely independent of anything the parsers derived.

    python -m kb.eval.export_gold
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
GOLD = HERE / "gold.json"

# the one permitted crossing of the boundary
sys.path.insert(0, str(ROOT / "tools"))


def export() -> Dict:
    from gen.catalog import files as catalog_files          # noqa: F401
    from gen.catalog.derived import build_derived
    from gen.catalog.model import FILES
    from gen.catalog.programs import build_programs

    build_derived()
    programs = build_programs()

    updaters: Dict[str, List[str]] = defaultdict(list)
    readers: Dict[str, List[str]] = defaultdict(list)
    calls: Dict[str, List[str]] = {}
    screens: Dict[str, str] = {}
    reports: Dict[str, str] = {}
    drivers: Dict[str, str] = {}

    for spec in programs:
        for name in spec.updates:
            updaters[name].append(spec.name)
        for name in spec.inputs:
            readers[name].append(spec.name)
        if spec.calls:
            calls[spec.name] = sorted(spec.calls)
        if spec.display:
            screens[spec.name] = spec.display
        if spec.printer:
            reports[spec.name] = spec.printer
        if spec.cl_driver:
            drivers[spec.name] = spec.cl_driver

    fields: Dict[str, List[str]] = {}
    logicals: Dict[str, List[str]] = defaultdict(list)
    for df in FILES.values():
        if df.kind == "PF":
            fields[df.name] = [f.name for f in df.fields]
        elif df.based_on:
            logicals[df.based_on].append(df.name)

    return {
        "note": "Exported from the generator catalog - independent of anything "
                "the knowledge base parsed.  Regenerate with "
                "python -m kb.eval.export_gold",
        "programs": len(programs),
        "files": len(FILES),
        "updaters": {k: sorted(v) for k, v in sorted(updaters.items())},
        "readers": {k: sorted(v) for k, v in sorted(readers.items())},
        "calls": dict(sorted(calls.items())),
        "screens": dict(sorted(screens.items())),
        "reports": dict(sorted(reports.items())),
        "drivers": dict(sorted(drivers.items())),
        "fields": dict(sorted(fields.items())),
        "logicals": {k: sorted(v) for k, v in sorted(logicals.items())},
    }


def main() -> int:
    data = export()
    GOLD.write_text(json.dumps(data, indent=1), encoding="utf-8")
    size = GOLD.stat().st_size / 1024
    print(f"  wrote {GOLD.name}  ({size:,.0f} KB)")
    print(f"  {data['programs']:,} programs, {data['files']:,} files")
    print(f"  {len(data['updaters']):,} files with a declared updater")
    print(f"  {len(data['calls']):,} programs with declared calls")
    return 0


if __name__ == "__main__":
    sys.exit(main())
