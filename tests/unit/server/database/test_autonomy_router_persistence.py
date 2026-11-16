from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from psycopg.errors import UndefinedTable

from src.config import settings
from src.server.database.autonomy import router as router_db


class _Cursor:
    def __init__(self, *, rowcount: int = 1, rows: list[dict] | None = None) -> None:
        self.rowcount = rowcount
        self.rows = rows or []
        self.calls: list[tuple[str, tuple | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def execute(self, query, params=None):
        self.calls.append((query, params))

    async def fetchone(self):
        return self.rows[0] if self.rows else None

    async def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self, **_kwargs):
        return self._cursor


@pytest.mark.asyncio
async def test_snapshot_save_uses_generation_cas_and_integrity_digest(monkeypatch):
    cursor = _Cursor(rowcount=1)

    @asynccontextmanager
    async def fake_connection():
        yield _Connection(cursor)

    monkeypatch.setattr(router_db, "get_db_connection", fake_connection)
    payload = {"schema_version": 1, "channel_reference": "safe"}

    assert await router_db.save_router_baseline_snapshot("u1", payload, expected_generation=0)
    query, params = cursor.calls[-1]
    assert "ON CONFLICT (user_id) DO NOTHING" in query
    assert params[0] == "u1"
    assert params[2] == router_db.hashlib.sha256(
        router_db._canonical_json(payload).encode("utf-8")
    ).hexdigest()

    cursor.rowcount = 0
    assert not await router_db.save_router_baseline_snapshot("u1", payload, expected_generation=4)
    query, params = cursor.calls[-1]
    assert "WHERE user_id = %s AND generation = %s" in query
    assert params[-1] == 4


@pytest.mark.asyncio
async def test_metrics_reservation_and_route_recording_keep_unknowns_unknown(monkeypatch):
    cursor = _Cursor(rows=[{"decision_id": "00000000-0000-0000-0000-000000000002"}])

    @asynccontextmanager
    async def fake_connection():
        yield _Connection(cursor)

    monkeypatch.setattr(router_db, "get_db_connection", fake_connection)
    reserved_id = await router_db.reserve_router_dispatch_metric(
        user_id="u1",
        eval_id="00000000-0000-0000-0000-000000000001",
        decision_id="00000000-0000-0000-0000-000000000002",
        stable_decision_key="stable",
        trigger_ids=["candidate-a", "candidate-b"],
        batch_size=2,
        first_candidate_id="candidate-a",
    )
    query, _ = cursor.calls[-1]
    assert reserved_id == "00000000-0000-0000-0000-000000000002"
    assert "ON CONFLICT (eval_id)" in query
    assert "latency_ms" not in query
    assert "model_tokens" not in query

    await router_db.record_router_route_decision(
        user_id="u1",
        eval_id="00000000-0000-0000-0000-000000000001",
        trigger_id="candidate-a",
        decision="accept",
    )
    query, params = cursor.calls[-1]
    assert "route_decision IS NULL" in query
    assert "user_id = %s" in query
    assert params[-2:] == ("u1", "candidate-a")

    await router_db.complete_router_dispatch_metric(
        decision_id=reserved_id, latency_ms=4.0, dispatch_status="failed"
    )
    await router_db.complete_router_dispatch_metric(
        decision_id=reserved_id, latency_ms=6.0, dispatch_status="submitted"
    )
    query, params = cursor.calls[-1]
    assert "dispatch_status = 'failed' AND %s = 'submitted'" in query
    assert params[-1] == "submitted"


@pytest.mark.asyncio
async def test_latest_router_decision_is_user_batch_scoped_and_age_bounded(monkeypatch):
    row = {
        "stable_decision_key": "batch-key",
        "eval_id": "00000000-0000-0000-0000-000000000009",
        "routed_trigger_id": "trigger-2",
        "route_decision": "accept",
    }
    cursor = _Cursor(rows=[row])

    @asynccontextmanager
    async def fake_connection():
        yield _Connection(cursor)

    monkeypatch.setattr(router_db, "get_db_connection", fake_connection)
    actual = await router_db.latest_router_decision_for_batch(
        "u1", "batch-key", exclude_eval_id="eval-current", max_age_seconds=3600
    )
    query, params = cursor.calls[-1]
    assert actual == row
    assert "user_id = %s" in query
    assert "stable_decision_key = %s" in query
    assert "eval_id <> %s" in query
    assert "LIMIT 1" in query
    assert params == ("u1", "batch-key", "eval-current", 3600)


@pytest.mark.asyncio
async def test_metrics_endpoint_projection_is_user_scoped_and_bounded(monkeypatch):
    from src.server.services.autonomy import router_metrics as service

    called: list[tuple[str, int]] = []

    async def fake_list(user_id: str, *, limit: int):
        called.append((user_id, limit))
        return [{"route_decision": None, "model_tokens": None}]

    monkeypatch.setattr(service, "get_autonomy_router_snapshot_enabled", lambda: True)
    monkeypatch.setattr(service, "list_router_metrics", fake_list)
    result = await service.router_metrics_for_user("auth-user", limit=100)
    assert result["enabled"] is True
    assert called == [("auth-user", 100)]

    monkeypatch.setattr(service, "get_autonomy_router_snapshot_enabled", lambda: False)
    result = await service.router_metrics_for_user("auth-user", limit=100)
    assert result == {"enabled": False, "available": False, "items": []}
    assert called == [("auth-user", 100)]

    async def missing_table(_user_id: str, *, limit: int):
        raise UndefinedTable("table not installed")

    monkeypatch.setattr(service, "get_autonomy_router_snapshot_enabled", lambda: True)
    monkeypatch.setattr(service, "list_router_metrics", missing_table)
    result = await service.router_metrics_for_user("auth-user", limit=100)
    assert result == {"enabled": True, "available": False, "items": []}


@pytest.mark.asyncio
async def test_router_session_upsert_conflicts_on_user_and_shard(monkeypatch):
    thread_id = "11111111-1111-1111-1111-111111111111"
    cursor = _Cursor(
        rows=[
            {
                "user_id": "u1",
                "workspace_id": "ws",
                "thread_id": thread_id,
                "shard": 0,
                "created_at": None,
                "updated_at": None,
            }
        ]
    )

    @asynccontextmanager
    async def fake_connection():
        yield _Connection(cursor)

    monkeypatch.setattr(router_db, "get_db_connection", fake_connection)
    saved = await router_db.save_router_session("u1", "ws", thread_id, shard=-3)
    query, params = cursor.calls[-1]
    assert "ON CONFLICT (user_id, shard)" in query
    assert "ON CONFLICT (user_id)" not in query
    assert "(user_id, workspace_id, thread_id, shard)" in query
    assert params == ("u1", "ws", thread_id, 0)
    assert saved["shard"] == 0
    assert saved["thread_id"] == thread_id

    cursor.rows = [
        {
            "user_id": "u1",
            "workspace_id": "ws",
            "thread_id": thread_id,
            "shard": 2,
            "created_at": None,
            "updated_at": None,
        }
    ]
    loaded = await router_db.get_router_session("u1", shard=2)
    query, params = cursor.calls[-1]
    assert "WHERE user_id = %s AND shard = %s" in query
    assert params == ("u1", 2)
    assert loaded["shard"] == 2


def test_snapshot_flag_has_typed_config_default_and_named_env_override(monkeypatch):
    monkeypatch.delenv("AUTONOMY_ROUTER_SNAPSHOT_ENABLED", raising=False)
    monkeypatch.setattr(
        settings,
        "get_infrastructure_config",
        lambda: type("Config", (), {"autonomy_router_snapshot_enabled": False})(),
    )
    assert settings.get_autonomy_router_snapshot_enabled() is False

    monkeypatch.setenv("AUTONOMY_ROUTER_SNAPSHOT_ENABLED", "true")
    assert settings.get_autonomy_router_snapshot_enabled() is True
    monkeypatch.setenv("AUTONOMY_ROUTER_SNAPSHOT_ENABLED", "0")
    assert settings.get_autonomy_router_snapshot_enabled() is False
