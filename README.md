# Alphaplus knowledge base

The Alphaplus knowledge base for the IBM i (AS/400) COBOL estate.

This repository holds the knowledge base source. The parser core -
`kb.parse`, `kb.index` and `kb.lexicon` - is **not** here: it ships
as a single compiled wheel on the
[Releases](../../releases) tab.

## Install

```
git clone <this repo>
cd <this repo>
python get_core_wheel.py
```

`get_core_wheel.py` picks the release asset matching your
interpreter and platform (`cp314` / `win_amd64`) and pip-installs
it. The repo is public, so no token is needed.

## How the two halves fit together

| Half | How it ships | Contains |
|---|---|---|
| Parser core | the wheel, via `python get_core_wheel.py` | `kb.parse`, `kb.index`, `kb.lexicon` as compiled `.pyd` |
| Everything else | this repository | `kb.server`, `kb.cli`, `kb.config`, ... as `.py` |

`kb/__init__.py` calls `pkgutil.extend_path`, so both halves answer
to the same `kb.` import names. No import anywhere has to change.

**Do not create `kb/parse`, `kb/index` or `kb/lexicon` directories
here.** One holding an `__init__.py` would sit first on the merged
search path and shadow the compiled package, and imports would
start failing with no obvious cause.

## Requirements

Python 3.14 on Windows x64 - the wheel is tagged
`cp314-win_amd64`. Another interpreter or OS needs its own build.
