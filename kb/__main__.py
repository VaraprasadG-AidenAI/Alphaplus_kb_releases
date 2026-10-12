"""
`python -m kb` - the same entry point the packaged executable uses.

Settings are applied to the environment before kb.cli is imported, because
kb.config freezes its paths at import time.  Nothing else may be imported
above bootstrap.
"""

from __future__ import annotations

import sys

from . import bootstrap

bootstrap.apply()

from . import cli  # noqa: E402  - must follow bootstrap.apply()

if __name__ == "__main__":
    sys.exit(cli.main())
