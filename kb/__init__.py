"""Alphaplus knowledge base package.

The parser core - kb.parse, kb.index and kb.lexicon - can ship as compiled
.pyd from the alphaplus_kb_core wheel instead of as source.  When it does, the
wheel installs those three packages beside this file in a different location
(site-packages), and extend_path merges that location into this package's
search path, so every ``kb.*`` import resolves whichever half holds the module.

With no wheel installed this is a no-op: extend_path finds no second ``kb`` on
sys.path and returns the path unchanged, so the full source tree works exactly
as before.
"""

from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)
