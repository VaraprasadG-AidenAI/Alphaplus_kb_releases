"""
Read alphaplus.toml, let the environment win, and refuse unsafe combinations.

    from kb.graph import settings
    cfg = settings.load()
    cfg.neo4j.uri, cfg.load.batch_rows, cfg.backend

Three rules, in this order:

  1. the environment beats the file - a container sets variables, it does not
     edit a committed file;
  2. a password may come from a FILE as well as a variable, because that is how
     Docker and Kubernetes mount secrets and reading one is one line;
  3. the committed development password is refused against a remote host.
     Shipping a graph to a server still holding `alphaplus-dev` is the kind of
     mistake that is obvious afterwards and invisible before, so it is a
     startup error rather than a warning nobody reads.

`tomllib` is standard library from 3.11, so this adds no dependency.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

#: The development password committed in alphaplus.toml.  Never valid remotely.
DEV_PASSWORD = "alphaplus-dev"

#: Hosts we accept the development password against.
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"}

BACKENDS = ("sqlite", "neo4j", "compare")


class ConfigError(RuntimeError):
    """Raised for a configuration that would fail later, and worse."""


@dataclass(frozen=True)
class Pool:
    max_connection_pool_size: int = 50
    connection_timeout_seconds: int = 30
    max_transaction_retry_time: int = 30
    connection_acquisition_timeout: int = 60


@dataclass(frozen=True)
class Load:
    batch_rows: int = 5000
    fetch_rows: int = 20000
    strict: bool = True


@dataclass(frozen=True)
class Query:
    timeout_seconds: int = 30
    max_rows: int = 5000
    max_hops: int = 8


@dataclass(frozen=True)
class Autostart:
    """Whether we may launch a local Neo4j ourselves.

    `enabled` defaults to FALSE and that is not timidity.  On a deployed server
    Neo4j is a managed service; autostarting there would fight systemd for the
    port or - worse - open a second JVM against the same store, which is how a
    Neo4j store gets corrupted.  Production leaves this off and lets the
    platform own the lifecycle.  A developer laptop turns it on.
    """
    enabled: bool = False
    #: The unpacked Neo4j directory - the one with bin/ and conf/ in it.
    #: Falls back to $NEO4J_HOME.
    neo4j_home: str = ""
    #: A JRE to launch it with.  Passed explicitly because the parent process is
    #: often a frozen exe or an MCP client with no JDK on PATH at all.
    java_home: str = ""
    #: How long to wait for the bolt port.  A cold JVM takes 15-25s on a laptop.
    wait_seconds: int = 90


@dataclass(frozen=True)
class FullText:
    """Lucene knobs for ap_member_ft.

    `name_boost` is the caret operator's multiplier on the `name` field - it is
    what replaces FTS5's `bm25(table, w1, w2, w3)` column weights, which Lucene
    has no direct equivalent for.

    It defaults to 1.0 on purpose.  The `search` table it replaces had no
    column weights either, so 1.0 is the setting that makes the two stores
    comparable and lets the migration be judged on tokenisation alone.  Raising
    it is a deliberate ranking change and `kb.eval.probe` must be re-run
    afterwards - a boost that improves one ticket and quietly breaks four is
    exactly the change nobody notices.
    """
    name_boost: float = 1.0
    #: terms shorter than this are dropped before the query is built.  Matches
    #: the `len(word) > 3` filter the SQLite pass has always used.
    min_term_length: int = 4
    #: at most this many terms per query.  Lucene will take more; the SQLite
    #: pass capped at 12 and the cap is kept so the two stay comparable.
    max_terms: int = 12


@dataclass(frozen=True)
class Neo4j:
    uri: str = "bolt://127.0.0.1:7687"
    user: str = "neo4j"
    password: str = DEV_PASSWORD
    database: str = "neo4j"
    pool: Pool = field(default_factory=Pool)

    @property
    def host(self) -> str:
        """Host with the scheme, port and any credentials stripped off."""
        rest = self.uri.split("://", 1)[-1]
        rest = rest.rsplit("@", 1)[-1]          # bolt://user:pw@host
        if rest.startswith("["):                # bolt://[::1]:7687
            return rest.split("]", 1)[0] + "]"
        return rest.split(":", 1)[0]

    @property
    def is_local(self) -> bool:
        return self.host in LOCAL_HOSTS

    def redacted(self) -> str:
        """Safe to log."""
        return f"{self.uri} db={self.database} user={self.user} password=***"


@dataclass(frozen=True)
class Settings:
    neo4j: Neo4j = field(default_factory=Neo4j)
    load: Load = field(default_factory=Load)
    query: Query = field(default_factory=Query)
    fulltext: FullText = field(default_factory=FullText)
    autostart: Autostart = field(default_factory=Autostart)
    backend: str = "neo4j"
    facts_db: str = ""
    source: str = "defaults"


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def config_path() -> Optional[Path]:
    """alphaplus.toml, if we can find one.

    ALPHAPLUS_CONFIG wins.  Otherwise walk up from this file to the repo root,
    which covers running from a checkout and from an installed package alike.
    """
    explicit = os.environ.get("ALPHAPLUS_CONFIG")
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise ConfigError(f"ALPHAPLUS_CONFIG points at nothing: {p}")
        return p
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "alphaplus.toml"
        if candidate.is_file():
            return candidate
    return None


def _env_str(name: str, current: str) -> str:
    return os.environ.get(name, current)


def _env_int(name: str, current: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return current
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} is not a number: {raw!r}") from None


def _env_float(name: str, current: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return current
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} is not a number: {raw!r}") from None


def _env_bool(name: str, current: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return current
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _password(from_file: str) -> str:
    """Variable, then secret file, then whatever the toml said.

    A mounted secret usually arrives with a trailing newline; strip it, or the
    driver authenticates with a password that is one byte longer than the one
    that was set.
    """
    direct = os.environ.get("ALPHAPLUS_NEO4J_PASSWORD")
    if direct:
        return direct
    path = os.environ.get("ALPHAPLUS_NEO4J_PASSWORD_FILE")
    if path:
        p = Path(path).expanduser()
        if not p.is_file():
            raise ConfigError(f"ALPHAPLUS_NEO4J_PASSWORD_FILE missing: {p}")
        return p.read_text(encoding="utf-8").strip()
    return from_file


def load(path: Optional[Path] = None) -> Settings:
    """Build Settings from the file plus the environment, and validate."""
    raw: Dict[str, Any] = {}
    source = "defaults"
    p = path or config_path()
    if p is not None:
        try:
            raw = tomllib.loads(p.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{p} is not valid TOML: {exc}") from None
        source = str(p)

    n = raw.get("neo4j", {}) or {}
    pl = n.get("pool", {}) or {}
    ld = n.get("load", {}) or {}
    qy = n.get("query", {}) or {}
    ft = n.get("fulltext", {}) or {}
    au = n.get("autostart", {}) or {}
    gr = raw.get("graph", {}) or {}

    pool = Pool(
        max_connection_pool_size=_env_int(
            "ALPHAPLUS_NEO4J_POOL", pl.get("max_connection_pool_size", 50)),
        connection_timeout_seconds=_env_int(
            "ALPHAPLUS_NEO4J_CONNECT_TIMEOUT", pl.get("connection_timeout_seconds", 30)),
        max_transaction_retry_time=_env_int(
            "ALPHAPLUS_NEO4J_RETRY_TIME", pl.get("max_transaction_retry_time", 30)),
        connection_acquisition_timeout=_env_int(
            "ALPHAPLUS_NEO4J_ACQUIRE_TIMEOUT", pl.get("connection_acquisition_timeout", 60)),
    )

    neo = Neo4j(
        uri=_env_str("ALPHAPLUS_NEO4J_URI", n.get("uri", "bolt://127.0.0.1:7687")),
        user=_env_str("ALPHAPLUS_NEO4J_USER", n.get("user", "neo4j")),
        password=_password(n.get("password", DEV_PASSWORD)),
        database=_env_str("ALPHAPLUS_NEO4J_DATABASE", n.get("database", "neo4j")),
        pool=pool,
    )

    settings = Settings(
        neo4j=neo,
        load=Load(
            batch_rows=_env_int("ALPHAPLUS_NEO4J_BATCH", ld.get("batch_rows", 5000)),
            fetch_rows=_env_int("ALPHAPLUS_NEO4J_FETCH", ld.get("fetch_rows", 20000)),
            strict=_env_bool("ALPHAPLUS_NEO4J_STRICT", bool(ld.get("strict", True))),
        ),
        query=Query(
            timeout_seconds=_env_int(
                "ALPHAPLUS_NEO4J_QUERY_TIMEOUT", qy.get("timeout_seconds", 30)),
            max_rows=_env_int("ALPHAPLUS_NEO4J_MAX_ROWS", qy.get("max_rows", 5000)),
            max_hops=_env_int("ALPHAPLUS_NEO4J_MAX_HOPS", qy.get("max_hops", 8)),
        ),
        autostart=Autostart(
            enabled=_env_bool("ALPHAPLUS_NEO4J_AUTOSTART",
                              bool(au.get("enabled", False))),
            neo4j_home=_env_str("ALPHAPLUS_NEO4J_HOME",
                                au.get("neo4j_home", "")),
            java_home=_env_str("ALPHAPLUS_JAVA_HOME", au.get("java_home", "")),
            wait_seconds=_env_int("ALPHAPLUS_NEO4J_AUTOSTART_WAIT",
                                  au.get("wait_seconds", 90)),
        ),
        fulltext=FullText(
            name_boost=_env_float("ALPHAPLUS_FT_NAME_BOOST",
                                  float(ft.get("name_boost", 1.0))),
            min_term_length=_env_int("ALPHAPLUS_FT_MIN_TERM",
                                     ft.get("min_term_length", 4)),
            max_terms=_env_int("ALPHAPLUS_FT_MAX_TERMS",
                               ft.get("max_terms", 12)),
        ),
        backend=_env_str("ALPHAPLUS_GRAPH_BACKEND", gr.get("backend", "neo4j")).lower(),
        facts_db=_env_str("ALPHAPLUS_GRAPH_FACTS_DB", gr.get("facts_db", "")),
        source=source,
    )
    _validate(settings)
    return settings


def _validate(s: Settings) -> None:
    if s.backend not in BACKENDS:
        raise ConfigError(
            f"graph.backend must be one of {', '.join(BACKENDS)}, not {s.backend!r}")

    if not s.neo4j.uri.startswith(("bolt://", "bolt+s://", "bolt+ssc://",
                                   "neo4j://", "neo4j+s://", "neo4j+ssc://")):
        raise ConfigError(
            f"neo4j.uri must be a bolt:// or neo4j:// URI, not {s.neo4j.uri!r}")

    if not s.neo4j.password:
        raise ConfigError("neo4j password is empty - set ALPHAPLUS_NEO4J_PASSWORD")

    # The one that matters.  A remote server still using the committed
    # development password is a mistake worth refusing to start over.
    if s.neo4j.password == DEV_PASSWORD and not s.neo4j.is_local:
        raise ConfigError(
            f"refusing to use the committed development password against "
            f"{s.neo4j.host}. Set ALPHAPLUS_NEO4J_PASSWORD, or "
            f"ALPHAPLUS_NEO4J_PASSWORD_FILE to a mounted secret.")

    if s.load.batch_rows < 1:
        raise ConfigError("neo4j.load.batch_rows must be at least 1")
    if s.load.batch_rows > 50_000:
        raise ConfigError(
            f"neo4j.load.batch_rows={s.load.batch_rows} is large enough to "
            f"exhaust the JVM heap - see docs/NEO4J-LIMITS-REPORT.md")
    if s.query.max_hops < 1 or s.query.max_hops > 25:
        raise ConfigError("neo4j.query.max_hops must be between 1 and 25")

    if s.autostart.wait_seconds < 1 or s.autostart.wait_seconds > 600:
        raise ConfigError(
            "neo4j.autostart.wait_seconds must be between 1 and 600")
    if s.autostart.enabled and not s.neo4j.is_local:
        raise ConfigError(
            f"neo4j.autostart.enabled is true but the URI points at "
            f"{s.neo4j.host}, which is not this machine. Autostart can only "
            f"launch a loopback server - a remote Neo4j is somebody else's "
            f"process. Set enabled = false.")

    if s.fulltext.name_boost <= 0:
        raise ConfigError("neo4j.fulltext.name_boost must be greater than 0")
    if s.fulltext.min_term_length < 1:
        raise ConfigError("neo4j.fulltext.min_term_length must be at least 1")
    if s.fulltext.max_terms < 1:
        raise ConfigError("neo4j.fulltext.max_terms must be at least 1")


def describe(s: Optional[Settings] = None) -> str:
    """One block, safe to print, for `doctor` and for start-up logs."""
    s = s or load()
    return "\n".join([
        f"config   : {s.source}",
        f"backend  : {s.backend}",
        f"neo4j    : {s.neo4j.redacted()}",
        f"pool     : {s.neo4j.pool.max_connection_pool_size} connections, "
        f"{s.neo4j.pool.connection_timeout_seconds}s connect, "
        f"{s.neo4j.pool.max_transaction_retry_time}s retry",
        f"load     : {s.load.batch_rows:,} rows per transaction, "
        f"{s.load.fetch_rows:,} per fetch, strict={s.load.strict}",
        f"query    : {s.query.timeout_seconds}s timeout, "
        f"{s.query.max_rows:,} row cap, {s.query.max_hops} hop cap",
        f"autostart: {'on' if s.autostart.enabled else 'off'}"
        + (f", home {s.autostart.neo4j_home or '(NEO4J_HOME)'}, "
           f"{s.autostart.wait_seconds}s wait" if s.autostart.enabled else ""),
        f"fulltext : name boost {s.fulltext.name_boost}, "
        f"terms >={s.fulltext.min_term_length} chars, "
        f"max {s.fulltext.max_terms} terms",
    ])
