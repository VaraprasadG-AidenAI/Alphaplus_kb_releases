"""
Settings that must be known before anything else is imported.

kb.config resolves SOURCE_ROOT and DATA_DIR into module-level constants at
import time, which is the right shape for a library and the wrong shape for an
executable: by the time a command-line handler runs, config has already been
imported and the constants are fixed.  So the source path has to be applied to
the environment *first*, and that is what this module does.

It deliberately imports nothing from kb.  The small amount of path logic below
is duplicated from kb.config on purpose - importing config here would defeat
the entire point.  Keep the two in step; there is a test for it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

#: Written by `setup`, read on every launch.  Lives with the index rather than
#: with the executable, because Program Files is usually not writable.
SETTINGS_NAME = "settings.json"


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def settings_dir() -> Path:
    """Where settings.json lives - a fixed per-user path, on purpose.

    The index and the map now default to `<source>/_alphaplus-kb`, which is
    far easier for an operator to find.  settings.json cannot live there: it
    is the file that tells us where the source *is*, so putting it under the
    source would mean needing it to find itself.

    So the two are deliberately separate.  This is the small fixed anchor;
    kb.config.DATA_DIR is the large movable output.
    """
    if not frozen():
        return Path(__file__).resolve().parents[1] / "kb" / "data"
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / "Alphaplus" / "kb"
    return Path.home() / ".alphaplus" / "kb"


#: Kept as an alias: callers that only want somewhere writable still work.
default_data_dir = settings_dir


def data_dir() -> Path:
    """Honour an explicit override, else the fixed settings location."""
    override = os.environ.get("ALPHAPLUS_KB_DATA")
    return Path(override) if override else settings_dir()


def settings_path() -> Path:
    # Always the fixed location, never the override: an operator who moves the
    # index with ALPHAPLUS_KB_DATA should not thereby lose their saved source
    # path, and on a fresh run we would not know to look there anyway.
    return settings_dir() / SETTINGS_NAME


def load_settings() -> dict:
    path = settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        # A corrupt settings file must not stop the server from starting; the
        # user can re-run setup.  Falling back to defaults is safer than
        # refusing to run.
        return {}


def save_settings(settings: dict) -> Path:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2, sort_keys=True),
                    encoding="utf-8")
    return path


def apply() -> dict:
    """Push saved settings into the environment.

    Must be called before anything imports kb.config.  An environment variable
    that is already set always wins, so a client's MCP config can override the
    saved setting without editing files.
    """
    settings = load_settings()
    for key, env in (("source", "ALPHAPLUS_SOURCE"),
                     ("data", "ALPHAPLUS_KB_DATA")):
        value = settings.get(key)
        if value and not os.environ.get(env):
            os.environ[env] = str(value)
    return settings
