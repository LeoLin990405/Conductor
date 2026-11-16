"""/ready bounds its database probes and keeps the event loop free.

Three contracts, one test each: ``statement_timeout`` rides both readiness
connects, ``evaluate`` runs in a worker thread instead of on the loop, and a
failed database probe stops the daemon heartbeat probe from opening a second
connection that would fail the same way. psycopg and the probes are mocked,
so nothing here opens a socket.
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import UTC, datetime, timedelta

import pytest

from src.server.app import ready

FLEET = """
[[daemon]]
provider = "codex"
id = "d-codex"
"""


class _Cursor:
    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, *_args: object) -> None:
        return None

    def fetchall(self) -> list[object]:
        return []


class _Conn:
    def cursor(self) -> _Cursor:
        return _Cursor()

    def close(self) -> None:
        return None


def _db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DB_NAME", "conductor_test")


def _fake_psycopg(monkeypatch: pytest.MonkeyPatch, connect) -> None:
    class FakePsycopg:
        pass

    FakePsycopg.connect = staticmethod(connect)
    monkeypatch.setitem(sys.modules, "psycopg", FakePsycopg)


def _record_connect(seen: dict[str, object]):
    def connect(**kwargs: object) -> _Conn:
        seen.update(kwargs)
        return _Conn()

    return connect


def test_probe_database_bounds_the_statement_not_just_the_connect(monkeypatch):
    _db_env(monkeypatch)
    seen: dict[str, object] = {}
    _fake_psycopg(monkeypatch, _record_connect(seen))

    assert ready.probe_database()["ok"] is True
    assert seen["options"] == "-c statement_timeout=2000"
    assert seen["connect_timeout"] == 2


def test_server_last_seen_bounds_the_statement_not_just_the_connect(monkeypatch):
    _db_env(monkeypatch)
    seen: dict[str, object] = {}
    _fake_psycopg(monkeypatch, _record_connect(seen))

    assert ready.server_last_seen(["d-codex"]) == {}
    assert seen["options"] == "-c statement_timeout=2000"
    assert seen["connect_timeout"] == 2


@pytest.mark.asyncio
async def test_ready_dispatches_evaluate_through_the_threadpool(monkeypatch):
    calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    async def fake_run_in_threadpool(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return 200, {"status": "ready", "checks": {}}

    monkeypatch.setattr(ready, "run_in_threadpool", fake_run_in_threadpool)

    response = await ready.ready(scope="backend")

    assert response.status_code == 200
    assert calls == [(ready.evaluate, (), {"scope": "backend"})]


@pytest.mark.asyncio
async def test_ready_really_runs_the_probes_off_the_event_loop(monkeypatch):
    # The dispatch above is mocked, so prove the real starlette helper moves
    # the synchronous probes onto a worker thread.
    threads: list[str] = []

    def fake_evaluate(*, scope: str = ""):
        threads.append(threading.current_thread().name)
        return 200, {"status": "ready"}

    monkeypatch.setattr(ready, "evaluate", fake_evaluate)

    response = await ready.ready()

    assert response.status_code == 200
    assert len(threads) == 1
    assert threads[0] != threading.current_thread().name


def _fleet(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))
    monkeypatch.delenv("CONDUCTOR_DAEMONS_FILE", raising=False)
    monkeypatch.setenv("CONDUCTOR_HEARTBEAT_TTL", "120")
    (tmp_path / "daemons.toml").write_text(FLEET.strip() + "\n", encoding="utf-8")
    beats = tmp_path / "run" / "heartbeats"
    beats.mkdir(parents=True)
    at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    (beats / "d-codex.json").write_text(
        json.dumps({"id": "d-codex", "at": at}), encoding="utf-8"
    )


def _probes(*, database_ok: bool) -> ready.ReadyProbes:
    database = {
        "ok": database_ok,
        "detail": "ok" if database_ok else "OperationalError",
    }
    ok = {"ok": True, "detail": "ok"}
    return ready.ReadyProbes(
        database=lambda: database,
        redis=lambda: ok,
        mcp_registry=lambda: ok,
        dispatch_config=lambda: ok,
        daemon_heartbeats=ready.probe_daemon_heartbeats,
    )


def test_a_failed_database_probe_skips_the_daemon_table_lookup(tmp_path, monkeypatch):
    _fleet(tmp_path, monkeypatch)
    lookups: list[list[str]] = []

    def tracking(ids):
        lookups.append(list(ids))
        return {"d-codex": datetime.now(UTC)}

    monkeypatch.setattr(ready, "server_last_seen", tracking)

    status, body = ready.evaluate(_probes(database_ok=False))

    # The database check is what fails readiness; the heartbeat files alone
    # keep the daemon fresh, so the probe never reaches the table.
    assert status == 503
    assert body["checks"]["database"]["ok"] is False
    assert body["checks"]["daemon_heartbeats"]["ok"] is True
    assert lookups == []

    # The skip is scoped to one evaluate() call: a healthy probe asks again.
    healthy_status, healthy_body = ready.evaluate(_probes(database_ok=True))

    assert healthy_status == 200
    assert healthy_body["checks"]["daemon_heartbeats"]["ok"] is True
    assert lookups == [["d-codex"]]
