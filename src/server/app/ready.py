"""Unauthenticated readiness probe.

Checks the database, Redis, the MCP registry, dispatch configuration, and
daemon heartbeats. Each probe is replaceable so tests never open a real socket.

A configured daemon is fresh when either its server heartbeat
(``autonomy_daemons.last_seen_at``, written on every ``/daemon/heartbeat``) or
its local heartbeat file is inside the TTL. ``cond`` writes the file once when
it starts a daemon, so the file alone goes stale after the TTL while the
daemon keeps heartbeating to the server.
"""

from __future__ import annotations

import json
import os
import stat
import tomllib
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

router = APIRouter(tags=["Health"])

Probe = Callable[[], dict[str, object]]


@dataclass
class ReadyProbes:
    database: Probe
    redis: Probe
    mcp_registry: Probe
    dispatch_config: Probe
    daemon_heartbeats: Probe


_probes: ReadyProbes | None = None

# evaluate() sets this around the heartbeat probe when the database probe
# already failed, so the daemon lookup does not open a second connection that
# would fail the same way and burn another connect timeout.
_skip_server_last_seen: ContextVar[bool] = ContextVar(
    "ready_skip_server_last_seen", default=False
)

# A readiness probe must never hang behind a slow query: the connect has its
# own timeout above, and this bounds the statement itself.
_STATEMENT_TIMEOUT_OPTIONS = "-c statement_timeout=2000"


def set_probes(probes: ReadyProbes | None) -> None:
    global _probes
    _probes = probes


def get_probes() -> ReadyProbes:
    return _probes or default_probes()


def _ok(detail: str = "ok", **extra: object) -> dict[str, object]:
    return {"ok": True, "detail": detail, **extra}


def _bad(detail: str, **extra: object) -> dict[str, object]:
    return {"ok": False, "detail": detail, **extra}


def _db_params() -> dict[str, object] | None:
    host = os.environ.get("DB_HOST") or os.environ.get("PGHOST") or ""
    name = os.environ.get("DB_NAME") or os.environ.get("PGDATABASE") or ""
    if not host or not name:
        return None
    return {
        "host": host,
        "port": int(os.environ.get("DB_PORT") or os.environ.get("PGPORT") or "5432"),
        "user": os.environ.get("DB_USER") or os.environ.get("PGUSER") or "postgres",
        "password": os.environ.get("DB_PASSWORD") or os.environ.get("PGPASSWORD") or "",
        "dbname": name,
        "connect_timeout": 2,
    }


def probe_database() -> dict[str, object]:
    params = _db_params()
    if params is None:
        return _bad("not_configured")
    try:
        import psycopg

        conn = psycopg.connect(**params, options=_STATEMENT_TIMEOUT_OPTIONS)
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — readiness must report, not crash
        return _bad(type(exc).__name__)
    return _ok()


def probe_redis() -> dict[str, object]:
    url = os.environ.get("REDIS_URL", "").strip()
    host = os.environ.get("REDIS_HOST", "").strip()
    if not url and not host:
        return _bad("not_configured")
    try:
        import redis

        if url:
            client = redis.Redis.from_url(
                url, socket_connect_timeout=2, socket_timeout=2
            )
        else:
            port = int(os.environ.get("REDIS_PORT", "6379"))
            client = redis.Redis(
                host=host, port=port, socket_connect_timeout=2, socket_timeout=2
            )
        client.ping()
    except Exception as exc:  # noqa: BLE001 — readiness must report, not crash
        return _bad(type(exc).__name__)
    return _ok()


def probe_mcp_registry() -> dict[str, object]:
    raw = os.environ.get("CONDUCTOR_MCP_REGISTRY", "").strip()
    path = (
        Path(raw).expanduser()
        if raw
        else Path.home() / ".conductor" / "mcp" / "servers.toml"
    )
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        # The MCP hub owns this file. It is optional until that package merges.
        return _ok("not_configured")
    except OSError:
        return _bad("unreadable")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return _bad("unreadable")
    try:
        tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeError):
        return _bad("unreadable")
    return _ok()


def probe_dispatch_config() -> dict[str, object]:
    """``CONDUCTOR_DISPATCH_REPO``, or the backend's cwd when it is unset.

    The dispatch service defaults its repo to the current directory, and
    ``cond up`` starts the backend in the checkout, so an unset value is the
    documented default, not a missing setting. Readiness still requires that
    directory to be a git checkout.
    """
    raw = os.environ.get("CONDUCTOR_DISPATCH_REPO", "").strip()
    if raw:
        path = Path(raw).expanduser()
        detail = "ok"
    else:
        path = Path.cwd()
        detail = "default_cwd"
    if path.is_symlink() or not path.is_dir():
        return _bad("missing")
    if not (path / ".git").exists():
        return _bad("not_a_git_repo")
    return _ok(detail)


def _heartbeat_dir() -> Path:
    home = os.environ.get("CONDUCTOR_HOME", "").strip()
    root = Path(home).expanduser() if home else Path.home() / ".conductor"
    return root / "run" / "heartbeats"


def _daemons_file() -> Path:
    explicit = os.environ.get("CONDUCTOR_DAEMONS_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    home = os.environ.get("CONDUCTOR_HOME", "").strip()
    root = Path(home).expanduser() if home else Path.home() / ".conductor"
    return root / "daemons.toml"


def _configured_daemons() -> list[tuple[str, bool]] | None:
    """Active and retired ids. None means the fleet file is absent."""
    path = _daemons_file()
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    rows = data.get("daemon") or []
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return None
    configured: list[tuple[str, bool]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ident = str(row.get("id") or "")
        if ident:
            configured.append((ident, bool(row.get("retired"))))
    return configured


def _heartbeat_age(path: Path, now: datetime) -> tuple[str, float | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return "unreadable", None
    # A list or other non-object must not raise. Missing and invalid times
    # are unreadable too, not a fresh heartbeat.
    if not isinstance(payload, dict):
        return "unreadable", None
    raw = payload.get("at")
    if not isinstance(raw, str) or not raw.strip():
        return "unreadable", None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return "unreadable", None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return "ok", (now - stamp).total_seconds()


def server_last_seen(ids: list[str]) -> dict[str, datetime] | None:
    """``last_seen_at`` per daemon id from the server's own table.

    None means the lookup could not run (no database configured, or any error),
    so the caller falls back to the heartbeat files alone. Ids without a row are
    simply absent from the result.
    """
    params = _db_params()
    if params is None or not ids:
        return None
    try:
        import psycopg

        conn = psycopg.connect(**params, options=_STATEMENT_TIMEOUT_OPTIONS)
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT daemon_id::text, last_seen_at FROM autonomy_daemons"
                    " WHERE daemon_id::text = ANY(%s) AND last_seen_at IS NOT NULL",
                    (list(ids),),
                )
                rows = cursor.fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — readiness must report, not crash
        return None
    seen: dict[str, datetime] = {}
    for ident, stamp in rows:
        if isinstance(stamp, datetime):
            seen[str(ident)] = stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
    return seen


def probe_daemon_heartbeats() -> dict[str, object]:
    directory = _heartbeat_dir()
    ttl = int(os.environ.get("CONDUCTOR_HEARTBEAT_TTL", "120") or "120")
    now = datetime.now(UTC)
    fleet_path = _daemons_file()
    fleet_missing = fleet_path.is_symlink() or not fleet_path.is_file()
    configured = _configured_daemons()
    # None is a missing or unreadable fleet file: keep the directory scan.
    # A present file with no active daemon is not that case, even when every
    # id is retired. Retired ids are ignored in every branch below.
    if configured is not None:
        retired = {ident for ident, is_retired in configured if is_retired}
        active = [
            ident
            for ident, is_retired in configured
            if not is_retired and ident not in retired
        ]
        if not active:
            return _ok("none_active", stale=[], missing=[], unreadable=[])
    else:
        retired = set()
        active = []
    if configured is None:
        empty = "fleet_missing" if fleet_missing else "unreadable"
        if not directory.is_dir():
            return _ok(empty, stale=[], missing=[], unreadable=[])
        stale: list[str] = []
        unreadable: list[str] = []
        seen = 0
        for path in sorted(directory.glob("*.json")):
            if path.stem in retired:
                continue
            seen += 1
            state, age = _heartbeat_age(path, now)
            if state != "ok" or age is None:
                unreadable.append(path.stem)
            elif age > ttl:
                stale.append(path.stem)
        if seen == 0:
            return _ok(empty, stale=[], missing=[], unreadable=[])
        if stale or unreadable:
            return _bad(
                "daemon_heartbeat",
                stale=stale,
                missing=[],
                unreadable=unreadable,
            )
        return _ok(stale=[], missing=[], unreadable=[])
    missing: list[str] = []
    stale = []
    unreadable: list[str] = []
    # A failed database probe means this lookup would hit the same dead
    # database, so the heartbeat files alone decide freshness.
    server_seen = (
        {} if _skip_server_last_seen.get() else (server_last_seen(active) or {})
    )
    for ident in active:
        if ident in retired:
            continue
        seen_at = server_seen.get(ident)
        if seen_at is not None and (now - seen_at).total_seconds() <= ttl:
            continue
        path = directory / f"{ident}.json"
        if path.is_symlink() or not path.is_file():
            if path.exists():
                unreadable.append(ident)
            else:
                missing.append(ident)
            continue
        state, age = _heartbeat_age(path, now)
        if state != "ok" or age is None:
            unreadable.append(ident)
        elif age > ttl:
            stale.append(ident)
    if missing or stale or unreadable:
        return _bad(
            "daemon_heartbeat",
            missing=missing,
            stale=stale,
            unreadable=unreadable,
        )
    return _ok(missing=[], stale=[], unreadable=[])


def default_probes() -> ReadyProbes:
    return ReadyProbes(
        database=probe_database,
        redis=probe_redis,
        mcp_registry=probe_mcp_registry,
        dispatch_config=probe_dispatch_config,
        daemon_heartbeats=probe_daemon_heartbeats,
    )


def evaluate(
    probes: ReadyProbes | None = None, *, scope: str = ""
) -> tuple[int, dict[str, object]]:
    """Readiness. ``scope=backend`` stops before daemon heartbeats.

    A cold start has no heartbeats yet. The backend layer (database, Redis,
    MCP registry, dispatch config) has to pass before daemons are launched.
    Any other scope keeps the full check, including heartbeats.
    """
    selected = probes or get_probes()
    database = selected.database()
    checks: dict[str, object] = {
        "database": database,
        "redis": selected.redis(),
        "mcp_registry": selected.mcp_registry(),
        "dispatch_config": selected.dispatch_config(),
    }
    if scope != "backend":
        skip_server = isinstance(database, dict) and not bool(database.get("ok"))
        token = _skip_server_last_seen.set(skip_server)
        try:
            checks["daemon_heartbeats"] = selected.daemon_heartbeats()
        finally:
            _skip_server_last_seen.reset(token)
    ready = all(
        bool(item.get("ok")) for item in checks.values() if isinstance(item, dict)
    )
    return (200 if ready else 503), {
        "status": "ready" if ready else "not_ready",
        "checks": checks,
    }


@router.get("/ready")
async def ready(scope: str = "") -> JSONResponse:
    selected = scope if scope == "backend" else ""
    # The probes are synchronous drivers (psycopg, redis); running them here
    # would block the event loop for the whole probe, so they go to a worker.
    status, body = await run_in_threadpool(evaluate, scope=selected)
    return JSONResponse(body, status_code=status)
