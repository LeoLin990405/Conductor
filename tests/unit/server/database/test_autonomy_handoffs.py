"""Handoff row SQL: shape, params, fenced settle."""

from __future__ import annotations

import pytest

from src.server.database.autonomy import handoffs
from psycopg.types.json import Json


class _Cursor:
    def __init__(self, rows: list[dict] | None = None, rowcount: int = 0):
        self.rows = rows or []
        self.rowcount = rowcount
        self.calls: list[tuple[str, tuple]] = []
        self._fetched = False

    async def execute(self, sql, params):
        self.calls.append((" ".join(sql.split()), tuple(params)))

    async def fetchone(self):
        return self.rows[0] if self.rows else None

    async def fetchall(self):
        return list(self.rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _Connection:
    def __init__(self, cur: _Cursor):
        self.cur = cur

    def cursor(self, row_factory=None):
        return self.cur

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _patch(monkeypatch, conn: _Connection) -> None:
    monkeypatch.setattr(handoffs, "get_db_connection", lambda: conn)


@pytest.mark.asyncio
async def test_create_handoff_inserts_manifest(monkeypatch):
    cur = _Cursor(
        rows=[
            {
                "handoff_id": "h-1",
                "ticket_id": "t-1",
                "user_id": "u1",
                "producer": "worker:glm",
                "consumer": "router",
                "purpose": "draft",
                "status": "registered",
                "files": [{"path": "a.md", "size": 1, "sha256": "a" * 64}],
                "verify_error": "",
                "verified_at": None,
            }
        ]
    )
    _patch(monkeypatch, _Connection(cur))
    row = await handoffs.create_handoff(
        ticket_id="t-1",
        user_id="u1",
        producer="worker:glm",
        consumer="router",
        purpose="draft",
        files=[{"path": "a.md", "size": 1, "sha256": "a" * 64}],
    )
    assert row["handoff_id"] == "h-1"
    sql, params = cur.calls[0]
    assert "INSERT INTO autonomy_ticket_handoffs" in sql
    assert params[:5] == ("t-1", "u1", "worker:glm", "router", "draft")
    wrapped = params[5]
    assert isinstance(wrapped, Json)
    assert wrapped.obj == [{"path": "a.md", "size": 1, "sha256": "a" * 64}]


@pytest.mark.asyncio
async def test_get_and_list_confine_to_ticket(monkeypatch):
    cur = _Cursor()
    _patch(monkeypatch, _Connection(cur))
    await handoffs.get_handoff("h-1")
    sql, params = cur.calls[0]
    assert "FROM autonomy_ticket_handoffs WHERE handoff_id = %s" in sql
    assert params == ("h-1",)

    await handoffs.list_handoffs("t-1", limit=5, offset=1)
    sql, params = cur.calls[1]
    assert "WHERE ticket_id = %s" in sql
    assert "ORDER BY created_at DESC" in sql
    assert params == ("t-1", 5, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["verified", "rejected"])
async def test_settle_is_fenced_by_registered_status(monkeypatch, status):
    cur = _Cursor(rows=[{"handoff_id": "h-1", "status": status}])
    _patch(monkeypatch, _Connection(cur))
    row = await handoffs.settle_handoff("h-1", status=status, verify_error="boom")
    assert row is not None
    sql, params = cur.calls[0]
    assert "WHERE handoff_id = %s AND status = %s" in sql
    assert "verified_at = CASE WHEN %s = 'verified' THEN NOW() ELSE NULL END" in sql
    assert params == (status, "boom", status, "h-1", "registered")


@pytest.mark.asyncio
async def test_settle_rejects_unknown_target_status(monkeypatch):
    with pytest.raises(ValueError):
        await handoffs.settle_handoff("h-1", status="pending")


@pytest.mark.asyncio
async def test_handoff_row_normalizes_ids_and_files(monkeypatch):
    cur = _Cursor(
        rows=[
            {
                "handoff_id": "11111111-1111-4111-8111-111111111111",
                "ticket_id": "22222222-2222-4222-8222-222222222222",
                "user_id": "u1",
                "producer": "p",
                "consumer": "c",
                "purpose": "",
                "status": "registered",
                "files": None,
                "verify_error": "",
                "verified_at": None,
            }
        ]
    )
    _patch(monkeypatch, _Connection(cur))
    row = await handoffs.get_handoff("11111111-1111-4111-8111-111111111111")
    assert isinstance(row["handoff_id"], str)
    assert isinstance(row["ticket_id"], str)
    assert row["files"] == []
