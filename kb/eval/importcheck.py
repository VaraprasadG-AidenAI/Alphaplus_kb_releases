"""
Every shipped module imports, on its own.

    python -m kb.eval.importcheck

`compileall` proves the syntax; it does not prove the imports. A module that
names something that no longer exists compiles perfectly and fails the moment
PyInstaller, or a user, reaches it. Importing each one in turn is the cheapest
way to find that before a build does.

`kb.eval` is skipped: it is dev-only and excluded from the build.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import traceback

import kb

SKIP = ("kb.eval",)


def main() -> int:
    modules = sorted(m.name for m in
                     pkgutil.walk_packages(kb.__path__, "kb.")
                     if not m.name.startswith(SKIP))
    broken = []
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception:
            broken.append((name, traceback.format_exc().strip().split("\n")[-1]))

    print(f"{len(modules) - len(broken)}/{len(modules)} module(s) import")
    for name, why in broken:
        print(f"  FAIL  {name}: {why}")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
