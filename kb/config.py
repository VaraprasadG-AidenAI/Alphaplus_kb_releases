"""
Knowledge base configuration.

This is the only file that needs changing to point the knowledge base at a
different codebase.  Everything else reads SOURCE_ROOT from here.

Rule 0: nothing under kb/ imports from tools/.  In the real engagement we are
handed a source tree and nothing else - no generator, no catalog.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, List, Set

#: True when running from a PyInstaller bundle rather than a source checkout.
FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    # Inside a bundle __file__ points into a temporary extraction directory
    # that is deleted on exit, so it must never be used to locate anything we
    # intend to keep.  The executable's own directory is the stable anchor.
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parents[1]


#: Which build this is. One string, in one place, so "which exe is on that
#: machine" is answerable from a screenshot: `config`, `doctor` and the MCP
#: handshake all print it, and the build log is named after it. Bump it when
#: an executable is produced for somebody else to use.
BUILD_VERSION = "15.1"


#: The folder the knowledge base writes into, inside the source tree.  Named
#: with a leading underscore so it sorts to the top of a directory listing,
#: and listed in IGNORE_DIRS below so the parser never indexes its own output.
OUTPUT_DIR_NAME = "_alphaplus-kb"


def _per_user_data_dir() -> Path:
    """The fallback: a per-user directory that is always writable."""
    if not FROZEN:
        return ROOT / "kb" / "data"
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / "Alphaplus" / "kb"
    return Path.home() / ".alphaplus" / "kb"


def _writable(directory: Path) -> bool:
    """Can we actually create and write a file in there?

    Asked rather than assumed: a source tree can be a read-only mount, a
    locked-down share, or simply not ours to write to, and finding that out
    after a four minute parse is the wrong time.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _default_data_dir(source: Path) -> Path:
    """Where to keep the index when nobody has said.

    Beside the code it describes, in `<source>/_alphaplus-kb`, so the map and
    the export are somewhere an operator will actually find them.

    Falls back to a per-user directory when the source tree cannot be written
    to - a read-only extract is a perfectly reasonable way to hold source, and
    the index has to land somewhere either way.
    """
    if source.exists():
        candidate = source / OUTPUT_DIR_NAME
        if _writable(candidate):
            return candidate
    return _per_user_data_dir()


# ===========================================================================
# alphaplus.toml - configuration that survives a stripped environment
# ===========================================================================
#
# These settings were environment-only, and that made the knowledge base
# UNCONFIGURABLE in its primary delivery mode. An MCP stdio server is spawned
# by its client, and the MCP SDK spawns it with a security allowlist - APPDATA,
# PATH, TEMP, USERPROFILE and a few more. Every ALPHAPLUS_* variable is
# stripped, whatever the operator's shell holds. So a correctly configured
# machine still served an agent from the wrong store, and the only symptom was
# a "not indexed" message naming a database nobody had chosen.
#
# The toml is found by walking up from this file, so locating it needs no
# variable - which is the whole point.
#
# Environment still wins where it survives, matching kb/graph/settings.py: a
# container sets variables, it does not edit a committed file.
#
# NO SECRET GOES IN THE TOML. It carries host, port, database, user - and the
# PATH TO a password file. The committed file stays free of credentials and
# still configures a server with no environment at all.

def _read_toml() -> dict:
    """Tables from alphaplus.toml, or {} if there is none."""
    try:
        import tomllib
    except ModuleNotFoundError:
        return {}
    explicit = os.environ.get("ALPHAPLUS_CONFIG")
    candidates = ([Path(explicit)] if explicit else
                  [parent / "alphaplus.toml"
                   for parent in Path(__file__).resolve().parents])
    for c in candidates:
        try:
            if c.is_file():
                with c.open("rb") as fh:
                    return tomllib.load(fh)
        except (OSError, ValueError):
            return {}
    return {}


_TOML = _read_toml()


def setting(env: str, *path: str, default=None):
    """Environment, then alphaplus.toml, then the default.

    `path` walks the toml tables, so setting("ALPHAPLUS_PG_HOST", "postgres",
    "host") reads `host` from `[postgres]`.
    """
    v = os.environ.get(env)
    if v is not None and str(v).strip() != "":
        return v
    node = _TOML
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return default if node in (None, "") else node



# ===========================================================================
# The one thing to change
# ===========================================================================

#: Where the source lives.  Today the replica; tomorrow the real extracted
#: tree.  Override with the ALPHAPLUS_SOURCE environment variable.
SOURCE_ROOT = Path(setting("ALPHAPLUS_SOURCE", "source", "root",
                           default=ROOT / "APPSRC"))

#: True when the operator named the data directory, in which case repoint()
#: must not move it out from under them.
DATA_DIR_IS_EXPLICIT = bool(os.environ.get("ALPHAPLUS_KB_DATA"))

#: Where the knowledge base keeps its own artefacts.
DATA_DIR = (Path(os.environ["ALPHAPLUS_KB_DATA"]) if DATA_DIR_IS_EXPLICIT
            else _default_data_dir(SOURCE_ROOT))
FACTS_DB = DATA_DIR / "facts.db"
SUMMARY_DIR = DATA_DIR / "summaries"

#: Files that ship inside the bundle and are only ever read.  In a frozen
#: build they live under sys._MEIPASS, which is where __file__ already points,
#: so package-relative lookups keep working - this is only for code that
#: needs the bundle root explicitly.
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", "")) if FROZEN else ROOT

#: Send source to an LLM API to generate summaries?  The knowledge base must
#: be fully useful with this off - facts, search, graph and lexicon all work
#: with zero code leaving the network.  We do not yet know what the client
#: will permit.
ENABLE_SUMMARIES = os.environ.get("ALPHAPLUS_SUMMARIES", "0") == "1"


# ===========================================================================
# The summary provider
# ===========================================================================
#
# Which LLM writes the summaries.  Every provider here speaks over plain HTTPS
# with no SDK, so turning one on is a key and a name - not a pip install.
#
#     set ALPHAPLUS_SUMMARIES=1
#     set ALPHAPLUS_LLM=deepseek
#     set DEEPSEEK_API_KEY=sk-...
#
# 'none' is a real setting, not a placeholder.  With it the summariser writes
# pages from facts alone - no source, no prose, nothing leaves the network.
# The knowledge base has to be useful in that state, because we do not yet
# know what the client will allow.

LLM_PROVIDER = os.environ.get("ALPHAPLUS_LLM", "none").lower()

#: provider -> (base url, default model, environment variable holding the key)
#: OpenAI and DeepSeek share the /chat/completions shape, so they share code.
LLM_PROVIDERS: Dict[str, Dict[str, str]] = {
    "anthropic": {
        "url": "https://api.anthropic.com/v1/messages",
        "model": "claude-sonnet-5",
        "key_env": "ANTHROPIC_API_KEY",
        "dialect": "anthropic",
    },
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o-mini",
        "key_env": "OPENAI_API_KEY",
        "dialect": "openai",
    },
    "deepseek": {
        "url": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-chat",
        "key_env": "DEEPSEEK_API_KEY",
        "dialect": "openai",
    },
    # Anything OpenAI-shaped: Ollama, vLLM, LM Studio, a corporate gateway.
    # This is the one that matters if the client will not let code leave the
    # building but will run a model inside it.
    "local": {
        "url": os.environ.get("ALPHAPLUS_LLM_URL",
                              "http://localhost:11434/v1/chat/completions"),
        "model": os.environ.get("ALPHAPLUS_LLM_MODEL", "qwen2.5-coder"),
        "key_env": "ALPHAPLUS_LLM_KEY",
        "dialect": "openai",
    },
    "none": {"url": "", "model": "", "key_env": "", "dialect": "none"},
}

#: Override the model without touching the table.
LLM_MODEL = os.environ.get("ALPHAPLUS_LLM_MODEL", "")

#: Send full source to the model for the deep tier?  Off means the model only
#: ever sees extracted facts - names, calls, files, paragraph labels - which
#: is a materially easier conversation to have with a security team.
#: Send the member's source to the model, not only its fact sheet.
#:
#: Defaulted OFF for its whole life, and the effect was not visible: a
#: page written from a fact sheet alone reads like a page, so nothing
#: looked wrong. It just said "processes various financial transactions"
#: for every member. Naming a member from its identifiers alone is the one
#: thing this project was told not to do - some of it is right, some is
#: invented, and neither is marked.
#:
#: Now defaults ON. Set ALPHAPLUS_LLM_SOURCE=0 to send fact sheets only,
#: which is a deliberate cost decision rather than the silent default.
LLM_SEND_SOURCE = os.environ.get("ALPHAPLUS_LLM_SOURCE", "1") == "1"

#: How long to wait for one completion.
#:
#: 120 seconds is right for a hosted API and far too short for a local one. A
#: local model is loaded on the first call - several gigabytes off disk - and
#: then does its prefill on the CPU, so the first request of a run can take
#: minutes while every later one takes under two. At 120s the first call of a
#: fresh run times out, and because a connection error is terminal for the
#: local provider it does not even retry: the run fails at page one having
#: proved nothing except that the timeout was too low.
#:
#: So the default depends on the provider, and an explicit setting always wins.
LLM_TIMEOUT = int(os.environ.get(
    "ALPHAPLUS_LLM_TIMEOUT",
    "900" if os.environ.get("ALPHAPLUS_LLM", "").lower() == "local" else "120"))

# ===========================================================================
# Semantic routing - optional, and it must stay optional
# ===========================================================================
#
# A small embedding index over the WRITTEN PAGES only.  Never over code:
# docs/kb/01-RESEARCH.md settles that - COBOL identifiers are OHDCNO and
# IWHQOH, there is no meaning to match - and sanctions exactly this narrow
# case, "the ticket is written in business language and the code is not".
#
# Off by default, and everything works without it. The rest of this file
# already holds that line for summaries and it holds here for the same reason:
# we do not know what the client will permit, and a knowledge base that needs a
# vector store to answer anything is one that cannot be installed.
#
# It is also the first third-party dependency this project has ever had
# (qdrant-client). numpy is excluded from the frozen build over 60 MB, so this
# is excluded too - see alphaplus-kb.spec.

#: "auto" (default) | "on" | "off".  `1` and `0` still mean on and off.
#:
#: This used to default to OFF, and the reasoning above is why: a knowledge base
#: that needs a vector store to answer anything cannot be installed in a network
#: where a new service is a change request.
#:
#: But OFF-by-default made a working retriever invisible. Every vector was
#: present - 112 of them, 1024-wide, cosine, correctly normalised - the model
#: was pulled and answering, and `route_ticket` still returned bm25 alone and
#: said "set ALPHAPLUS_SEMANTIC=1 to use it". A feature nobody can discover
#: without reading the source is not optional, it is dark.
#:
#: `auto` keeps the property that mattered and drops the one that did not: if
#: qdrant-client is absent, or no vectors are stored, or the dimension does not
#: match, or the model does not answer, semantic degrades and SAYS WHICH - and
#: the rest of the knowledge base is unaffected. If everything is there, it
#: runs. `off` is still available for an environment where it must not, and is
#: the only setting that skips the capability check entirely.
SEMANTIC_MODE = (os.environ.get("ALPHAPLUS_SEMANTIC", "auto").strip().lower()
                 or "auto")
if SEMANTIC_MODE == "1":
    SEMANTIC_MODE = "on"
elif SEMANTIC_MODE == "0":
    SEMANTIC_MODE = "off"

#: Kept because four call sites read it.  "not off" rather than "on", so `auto`
#: reaches the capability checks instead of being treated as disabled.
ENABLE_SEMANTIC = SEMANTIC_MODE != "off"

#: The embedding model, and where it answers.  Ollama's /api/embed, not the
#: OpenAI chat shim the summariser uses - a different endpoint with a different
#: request shape.
EMBED_MODEL = setting("ALPHAPLUS_EMBED_MODEL", "qdrant", "embed_model",
                      default="snowflake-arctic-embed2")
EMBED_URL = setting("ALPHAPLUS_EMBED_URL", "qdrant", "embed_url",
                    default="http://localhost:11434/api/embed")

#: Arctic Embed v2 is asymmetric: a query is prefixed, a document is not.
#: Sending both the same way costs retrieval quality and reports no error at
#: all, so the convention is written down here rather than inlined at the call
#: site, and recorded per vector in the `embedding` table.
EMBED_QUERY_PREFIX = os.environ.get("ALPHAPLUS_EMBED_QUERY_PREFIX", "query: ")
EMBED_DOC_PREFIX = os.environ.get("ALPHAPLUS_EMBED_DOC_PREFIX", "")

#: 1024 for snowflake-arctic-embed2.  Asserted against the first vector the
#: model returns rather than trusted - a model swap that changes the dimension
#: would otherwise fill the collection with vectors nothing can compare.
EMBED_DIM = int(setting("ALPHAPLUS_EMBED_DIM", "qdrant", "embed_dim",
                        default=1024))


def embed_key() -> str:
    """The API key for the embedding endpoint, or "" for a local one.

    Read at call time rather than frozen at import, because a key exported
    after this module loaded must still work - and because a credential held in
    a module global outlives the call that needed it.

    ALPHAPLUS_EMBED_KEY wins, so the embedding provider can differ from the
    summariser's. Otherwise OPENAI_API_KEY, which is the common case: one
    OpenAI key doing both jobs. A local endpoint needs none, and sending an
    Authorization header to Ollama is at best ignored.
    """
    for var in ("ALPHAPLUS_EMBED_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return ""


def qdrant_path() -> Path:
    """Where the embedded vector store lives - beside the index it describes.

    A directory, not a server. This knowledge base is meant to run inside a
    client network where installing a service is a change request; the same
    reasoning that made search_source pure Python instead of ripgrep.
    """
    return DATA_DIR / "qdrant"


#: The context the local model was loaded with, in tokens.
#:
#: Not a request - a statement of fact that the summariser checks prompts
#: against. Ollama's OpenAI-compatible endpoint cannot set num_ctx, so a prompt
#: that does not fit is truncated with no error and the model summarises the
#: fragment. 4096 is Ollama's usual default; raise this only when the model has
#: actually been loaded with more.
LLM_CONTEXT = int(os.environ.get("ALPHAPLUS_LLM_CONTEXT", "4096"))


def llm_settings() -> Dict[str, str]:
    """The resolved provider, or the 'none' entry if it is not usable."""
    entry = dict(LLM_PROVIDERS.get(LLM_PROVIDER, LLM_PROVIDERS["none"]))
    if LLM_MODEL:
        entry["model"] = LLM_MODEL
    entry["name"] = LLM_PROVIDER
    entry["key"] = os.environ.get(entry.get("key_env", ""), "")
    return entry


# ===========================================================================
# Source layout
# ===========================================================================
#
# On IBM i, source lives as members inside source physical files inside
# libraries.  When extracted to a filesystem the member type usually becomes
# the file extension.  This map is the KB's own reference - it is NOT imported
# from the generator, and it is the first thing to revisit when a real
# extraction arrives with different conventions.

#: extension -> member type
EXT_TO_TYPE: Dict[str, str] = {
    ".PF": "PF", ".LF": "LF", ".DSPF": "DSPF", ".PRTF": "PRTF", ".LBL": "PRTF",
    ".CBL": "CBL", ".CBLLE": "CBLLE", ".SQLCBLLE": "SQLCBLLE",
    ".CPY": "CPY", ".CPYLE": "CPY", ".TXT": "CPY",
    ".CLP": "CLP", ".CLLE": "CLLE", ".CL": "CLP",
    ".CMD": "CMD",
    ".BND": "BND", ".BNDSRC": "BND",
    ".RPGLE": "RPGLE", ".SQLRPGLE": "SQLRPGLE", ".RPG": "RPG", ".RPG36": "RPG",
    ".SQL": "SQL",
    ".MSGF": "MSGF",
    ".MNUDDS": "MNUDDS", ".MNUCMD": "MNUCMD",
    # Generic extensions a real extraction often uses instead of the specific
    # member type.  These cannot be classified from the name - a .DDS is a
    # physical file, a logical, a screen or a report - so they are routed to
    # the DDS parser and reclassified from their own content.  See
    # kb.parse.dds.classify.
    ".DDS": "DDS", ".DDSSRC": "DDS",
    ".MNU": "MNU", ".MENU": "MNU",
    # OPM COBOL with embedded SQL, as an SVN export spells it.
    ".SQLCBL": "SQLCBL",
    # Member types with no parser here. Indexed all the same, so a program
    # that runs them names something that exists, and a search can read them.
    ".SRT": "FMTDTA", ".FMTDTA": "FMTDTA",
    ".TBL": "TBL",
    ".SQLC": "SQLC", ".SQLCPP": "SQLC",
}


#: The source physical files IBM i creates by default, and the member type each
#: normally holds. Used only for a file whose name carries no member type - no
#: extension, or `.MBR` - and whose content has no decisive marker: a copybook
#: has no division header, so its directory is the only signal it has. Content
#: still wins wherever it speaks (kb.parse.classify).
SOURCE_FILE_TYPES: Dict[str, str] = {
    "QCBLLESRC": "CBLLE", "QCBLSRC": "CBL",
    "QCPYSRC": "CPY", "QLBLSRC": "CPY",
    "QDDSSRC": "DDS",
    "QCLSRC": "CLP", "QCLLESRC": "CLLE",
    "QRPGLESRC": "RPGLE", "QRPGSRC": "RPG",
    "QSQLSRC": "SQL",
    "QCMDSRC": "CMD",
    "QMNUSRC": "MNU",
    "QSRVSRC": "BND",
}


def source_file_type(folder: str) -> str:
    """The member type a source physical file holds, or "" when unknown.

    On IBM i a member sits in a source physical file named for what it holds -
    QCLSRC holds CL, QCMDSRC holds commands - so for a member whose own name
    carries no type, the folder is the type declaration.

    An exact entry wins. After that the name is read the way shops name their
    own source files, for the same reason `member_type_for` does it for
    extensions: a site defines `QCBLLECPY` for ILE COBOL copybooks and
    `QRPGLECPY` for RPG's, and a list of every local variant would be wrong at
    the next site. `QCBLLECPY` was the one that cost us - a real copybook
    source file this map had never heard of, so its members were dropped.

        contains CPY, not RPG   -> CPY     a copybook
        contains RPG            -> RPGLE
        contains CBL            -> CBLLE
        contains DDS            -> DDS
        contains CMD            -> CMD
        contains SQL            -> SQL

    Deliberately narrower than `member_type_for`: only a name beginning `Q` is
    read this way, because that is the platform's convention for a source
    physical file and an ordinary folder called `COPY` is not one.
    """
    name = (folder or "").upper().strip()
    if not name:
        return ""
    if name in SOURCE_FILE_TYPES:
        return SOURCE_FILE_TYPES[name]
    if not name.startswith("Q") or not name.isalnum():
        return ""
    if "CPY" in name and "RPG" not in name:
        return "CPY"
    if "RPG" in name:
        return "RPGLE"
    if "CBL" in name:
        return "CBLLE"
    if "DDS" in name:
        return "DDS"
    if "CMD" in name:
        return "CMD"
    if "SQL" in name:
        return "SQL"
    return ""


def member_type_for(suffix: str) -> str:
    """The member type a file extension stands for, or "" when it is not one.

    An exact entry in EXT_TO_TYPE wins. After that the extension is read the
    way IBM i member types are named, because shops define their own types
    and an SVN export writes each one out as the extension: `CBLLECPY`,
    `CBLCPYIRN`, `CBLLECPYDY` and `CBLCPYSEL` are all COBOL copybooks, and a
    list of every local variant would be wrong at the next site. So:

        contains CPY, not RPG   -> CPY     a copybook
        contains RPG            -> RPGLE   RPG source or an RPG copy member
        contains CBL            -> CBLLE   a COBOL program variant

    The content check in kb.parse.classify still runs on every member, so a
    member whose content disagrees with its name is retyped or noted there.
    """
    ext = (suffix or "").upper()
    if not ext:
        return ""
    if ext in EXT_TO_TYPE:
        return EXT_TO_TYPE[ext]
    if ext in NON_SOURCE_EXTENSIONS or ext in DATA_EXTENSIONS:
        return ""
    token = ext.lstrip(".")
    if not token.isalnum():
        return ""
    if "RPG" in token:
        return "RPGLE"
    if "CPY" in token:
        return "CPY"
    if "CBL" in token:
        return "CBLLE"
    return ""

#: member type -> the language family that decides which parser runs
TYPE_TO_LANG: Dict[str, str] = {
    "PF": "dds", "LF": "dds", "DSPF": "dds", "PRTF": "dds", "MNUDDS": "dds",
    "CBL": "cobol", "CBLLE": "cobol", "SQLCBLLE": "cobol", "SQLCBL": "cobol",
    "CPY": "copybook",
    "CLP": "cl", "CLLE": "cl", "MNUCMD": "clcmd",
    "CMD": "cmd",
    "BND": "bnd",
    "RPGLE": "rpg", "SQLRPGLE": "rpg", "RPG": "rpg",
    "SQL": "sql",
    "MSGF": "msgf",
    # Reclassified during parsing; both go to the DDS reader first.
    "DDS": "dds", "MNU": "dds",

    # Types that only `kb.parse.classify` can produce, because they are
    # decided by content rather than by name. None of them is COBOL or DDS,
    # which is the whole point: handing them to those parsers is what produced
    # most of the `failed` count on the real extract.
    "FMTDTA": "",       # sort specification, not a printer file
    "EVENT": "",        # compiler artefact, not source
    "DATA": "",         # data export, not source
    "PRECOMPILED": "",  # SQL precompiler output; duplicates a real member
    "S36": "",          # System/36 conversion; an older variant
    "SCREENMETA": "",   # generated screen metadata; shadows a real DSPF
    "TBL": "",          # translation table source
    "SQLC": "",         # C with embedded SQL; no C parser here
}

#: Member types that describe a database file - the things programs open.
DATABASE_TYPES: Set[str] = {"PF", "LF"}

#: Member types that describe a screen or a report.
DEVICE_TYPES: Set[str] = {"DSPF", "PRTF", "MNUDDS"}

#: Member types that hold executable logic.
PROGRAM_TYPES: Set[str] = {
    "CBL", "CBLLE", "SQLCBLLE", "SQLCBL", "CLP", "CLLE", "RPGLE", "SQLRPGLE",
    "RPG",
}


# ===========================================================================
# Naming conventions
# ===========================================================================
#
# WARNING.  These are conventions of the replica and are very likely to be
# wrong for the real codebase.  Nothing in kb/ may treat them as fact - they
# are hints used only to *rank* candidates, never to assert a relationship.

#: Suffix hints: ORD100C is probably the CL driver for ORD100.
SUFFIX_HINTS: Dict[str, str] = {
    "C": "cl_driver",
    "D": "display_file",
    "P": "printer_file",
    "W": "work_file",
}

#: How many leading characters of an object name might indicate a subsystem.
#: Used only for grouping in reports.  Derived from the data at index time,
#: not hard-coded, so it survives a codebase with different conventions.
SUBSYSTEM_PREFIX_LEN = 3


# ===========================================================================
# Ingestion
# ===========================================================================

#: Extensions that are certainly not IBM i source.  Files with these are
#: still counted when skipped, but reported separately: mixing a stray
#: build script in with a member type we failed to map makes the list
#: useless, and the whole point of that list is that somebody triages it.
NON_SOURCE_EXTENSIONS: Set[str] = {
    ".PY", ".PYC", ".YAML", ".YML", ".JSON", ".XML", ".MD", ".TXT_", ".RST",
    ".EXE", ".DLL", ".PYD", ".SO", ".BAT", ".CMD_", ".PS1", ".SH",
    ".ZIP", ".GZ", ".TAR", ".SAVF", ".7Z", ".RAR", ".JAR",
    ".LOG", ".TMP", ".BAK", ".OLD", ".SWP", ".ORIG", ".REJ",
    ".PDF", ".DOC", ".DOCX", ".XLS", ".XLSX", ".PPT", ".PPTX", ".CSV",
    ".PNG", ".JPG", ".JPEG", ".GIF", ".BMP", ".ICO", ".SVG",
    ".HTML", ".HTM", ".CSS", ".JS", ".TS", ".MAP",
    ".GITIGNORE", ".GITATTRIBUTES", ".DS_STORE", ".INI", ".CFG", ".TOML",
    ".DB", ".SQLITE", ".SQLITE3", ".LOCK", ".SUM", ".MOD",
    # IDE and version-control metadata beside the members in an SVN checkout:
    # member properties, workspace preferences, project descriptors.
    ".PROPERTIES", ".PREFS", ".PROJECT", ".CLASSPATH",
}


#: Directories under SOURCE_ROOT to skip entirely.
IGNORE_DIRS: Set[str] = {".git", ".svn", "__pycache__", "node_modules",
                         OUTPUT_DIR_NAME}

#: On IBM i a member lives at LIBRARY/SOURCEFILE(MEMBER). A filesystem dump
#: flattens that to LIBRARY/SOURCEFILE/MEMBER.<ext>, and the extension is
#: invented by whoever wrote the dump - the platform supplies none. So the
#: SOURCE-FILE DIRECTORY is the reliable type signal and the extension is not.
#:
#: Routing on the extension alone is one bug in two directions: it skipped 873
#: compiler event files because .MBR is unmapped, and it fed 188 event files
#: to the COBOL parser because they happened to be named .cbl - where they
#: collided by name with the real members they were compiled from.

#: Compiler event files: OPTION(*EVENTF) and IDE-driven compiles write them
#: here, for every compiler on the platform. Standard IBM i, not a local
#: convention. They carry the copybook graph as the compiler resolved it.
EVENT_DIRS: Set[str] = {"EVFEVENT"}

#: How many bytes to read before deciding what a file is.
#:
#: Nothing here is decided by a directory name that is local to one shop. A
#: compiled object is recognised because it is BINARY, not because somebody
#: called its directory CSBDDSOBJ - which is not an IBM name and would be
#: spelled differently at the next site. Content survives that; a name list
#: does not.
SNIFF_BYTES = 512

#: Extensions that hold DATA rather than code once the directories above have
#: been routed. Inventoried - path, size - never parsed. A field-widening
#: ticket needs to know these files exist and how big they are.
DATA_EXTENSIONS: Set[str] = {".MBR"}

#: Characters that suggest an EBCDIC to ASCII conversion went wrong.  Their
#: presence is reported, never silently accepted.
EBCDIC_SUSPECTS = "¬¦¢µÐÞþ"

#: Real shops keep DEV, TEST and PROD copies of the same member.  If the
#: extraction includes more than one, list them here in priority order; the
#: first one wins and the others are reported as duplicates.
#:
#: Read by kb.index.build (which library's copy of a duplicated name wins) and
#: reported by kb.index.consistency --check duplicate_member.  A library not
#: listed here sorts after every library that is, then alphabetically, so an
#: empty list is a well defined "no preference" rather than a special case.
LIBRARY_PRIORITY: List[str] = []


#: How a member's home library is decided.
#:
#:   path      the library is the directory holding the source physical file:
#:             <LIBRARY>/QCBLLESRC/MEMBER.CBLLE.  This is what a CPYTOSTMF
#:             extraction of more than one library produces, and it is what
#:             the real Alphaplus extract will look like.
#:   none      every member is in one unnamed library.
#:
#: A flat tree (QCBLLESRC/MEMBER.CBLLE, no library level) yields "" under
#: either setting.  That is deliberate: a single-library extraction must be
#: reported as single-library, not given an invented library name.
LIBRARY_FROM = os.environ.get("ALPHAPLUS_LIBRARY_FROM", "path").lower()


def library_of(stored: str) -> str:
    """The library a member lives in, from its SOURCE_ROOT-relative path.

    Takes the *stored* path - already relative and forward-slashed by
    store_path - so this never sees a drive letter or a mount point.

    The library is the parent of the source physical file directory, not the
    first component of the path, so an extract wrapped in one more directory
    (EXTRACT/APPOBJ/QCBLLESRC/ORD100.CBLLE) still resolves to APPOBJ.  Getting
    this wrong does not fail loudly - it silently files every member under the
    wrapper - so it is worth the index arithmetic.

        QCBLLESRC/ORD100.CBLLE                  -> ''        (flat)
        APPOBJ/QCBLLESRC/ORD100.CBLLE           -> 'APPOBJ'
        EXTRACT/APPOBJ/QCBLLESRC/ORD100.CBLLE   -> 'APPOBJ'
    """
    if LIBRARY_FROM == "none":
        return ""
    parts = [p for p in str(stored).replace("\\", "/").split("/") if p]
    # parts[-1] is the member, parts[-2] the source physical file, so the
    # library is parts[-3].  Fewer than three components means there is no
    # library level in this tree.
    return parts[-3].upper() if len(parts) >= 3 else ""


def member_key(name: str, member_type: str, library: str = "") -> str:
    """The key that identifies ONE member: name, type and library.

    The same three columns as `object`'s primary key, and for the same
    reason - two libraries can each hold an ORD100.CBLLE, and they are
    different programs.

    The indexer used to key its parse results on name and type alone, so on
    a multi-library extract one of the two was never written to the database.
    That is invisible downstream: a member that was never indexed looks
    exactly like a member that does not exist.

    Split with `key.split(".")`, which gives [name, member_type, library].
    Member names, types and library names cannot contain a dot on this
    platform, so the split is unambiguous - and `split(".")[0]` still gives
    the name, which is what the callers that only want a name already do.
    """
    return f"{name}.{member_type}.{library}"


def key_parts(key: str) -> tuple:
    """(name, member_type, library) from a member key.

    Tolerates the two-part keys older callers may still construct, so a
    stale key degrades to an empty library rather than an IndexError.
    """
    bits = key.split(".")
    while len(bits) < 3:
        bits.append("")
    return bits[0], bits[1], bits[2]


def library_rank(library: str) -> tuple:
    """Sort key deciding which library's copy of a duplicated name wins.

    LIBRARY_PRIORITY first, in the order given; everything else after it,
    alphabetically.  Returning a tuple rather than an int keeps the tie-break
    stable, which matters because an unstable winner makes a rebuild
    non-deterministic.
    """
    upper = (library or "").upper()
    priority = [lib.upper() for lib in LIBRARY_PRIORITY]
    if upper in priority:
        return (0, priority.index(upper), upper)
    return (1, 0, upper)

#: Maximum bytes to read from a single member.  Guards against a corrupt
#: extraction producing a multi-gigabyte "member".
MAX_MEMBER_BYTES = 64 * 1024 * 1024


# ===========================================================================
# Confidence levels - see Rule 2
# ===========================================================================

EXACT = "exact"            # read straight from executable source
COMPILER = "compiler"      # observed in a compiler event file: the compiler
                           # itself resolved this, at a known date. Stronger
                           # than anything derived from reading source, weaker
                           # than the current source, because it records the
                           # LAST COMPILE and that may be years old.
INFERRED = "inferred"      # derived, e.g. a library resolved through a CL variable
UNRESOLVED = "unresolved"  # we know something is here but not what

CONFIDENCE_LEVELS = (EXACT, INFERRED, UNRESOLVED)


# ===========================================================================
# Member paths
# ===========================================================================
#
# Paths are stored in the index RELATIVE to SOURCE_ROOT, with forward
# slashes.  An absolute path bakes one machine's drive letter and mount point
# into a 224 MB database: re-map the share, hand the file to a colleague, or
# ship a prebuilt index, and every path is wrong.  Relative paths make the
# index portable and survive a remount.
#
# Both helpers tolerate an absolute stored path, so an index built before this
# change keeps working without a rebuild.

def store_path(path) -> str:
    """The form of `path` to write into the index."""
    p = Path(path)
    try:
        return p.resolve().relative_to(SOURCE_ROOT.resolve()).as_posix()
    except (ValueError, OSError):
        # Outside SOURCE_ROOT, or unresolvable. Keep it absolute and correct
        # rather than mangling it into something that looks relative.
        return p.as_posix()


def resolve_path(stored: str):
    """Turn a stored path back into something openable."""
    if not stored:
        return None
    p = Path(stored)
    return p if p.is_absolute() else SOURCE_ROOT / p


def repoint(source=None, data=None) -> None:
    """Point this process at a different source tree or index, in place.

    The path constants above are resolved once at import time, which is right
    for a library: everything downstream reads config.SOURCE_ROOT when it runs,
    so one assignment here moves the whole knowledge base.

    Setting the environment variable after import does NOT work - that is read
    only while this module is first imported.  Anything interactive that lets a
    user choose a path must call this instead, and `setup` is exactly that.
    """
    global SOURCE_ROOT, DATA_DIR, FACTS_DB, SUMMARY_DIR, DATA_DIR_IS_EXPLICIT
    if source is not None:
        SOURCE_ROOT = Path(source)
        os.environ["ALPHAPLUS_SOURCE"] = str(SOURCE_ROOT)
        if not DATA_DIR_IS_EXPLICIT and data is None:
            # The default output folder lives inside the source tree, so
            # pointing at a different tree must move it too - otherwise one
            # codebase's index sits inside another's directory.
            DATA_DIR = _default_data_dir(SOURCE_ROOT)
    if data is not None:
        DATA_DIR = Path(data)
        DATA_DIR_IS_EXPLICIT = True
        os.environ["ALPHAPLUS_KB_DATA"] = str(DATA_DIR)
    FACTS_DB = DATA_DIR / "facts.db"
    SUMMARY_DIR = DATA_DIR / "summaries"

    # Two modules capture a config value at import time and would otherwise
    # keep pointing at the old location.
    lexicon = sys.modules.get("kb.lexicon.terms")
    if lexicon is not None:
        lexicon.SEEDED = DATA_DIR / "seeded.json"
        lexicon._seeded_cache = None
    server_tools = sys.modules.get("kb.server.tools")
    if server_tools is not None and getattr(server_tools, "_DB", None):
        server_tools._DB.close()
        server_tools._DB = None
        server_tools._ALL_PATHS = None
        # Derived from the database, so it belongs to the old one.
        server_tools._INDEG = None


def describe() -> str:
    """One-line summary of where this knowledge base is pointed."""
    llm = llm_settings()
    state = "enabled" if ENABLE_SUMMARIES else "DISABLED"
    if ENABLE_SUMMARIES:
        if llm["dialect"] == "none":
            state += " (facts only - no model configured, nothing leaves)"
        elif not llm["key"]:
            state += f" ({llm['name']}: NO KEY in {llm['key_env']})"
        else:
            state += (f" ({llm['name']} / {llm['model']}, source "
                      f"{'sent' if LLM_SEND_SOURCE else 'NOT sent'})")
    libraries = f"from the path ({LIBRARY_FROM})"
    if LIBRARY_PRIORITY:
        libraries += f", priority {' > '.join(LIBRARY_PRIORITY)}"
    return (
        f"source   : {SOURCE_ROOT}\n"
        f"facts    : {FACTS_DB}\n"
        f"libraries: {libraries}\n"
        f"summaries: {state}"
    )


if __name__ == "__main__":
    print(describe())
