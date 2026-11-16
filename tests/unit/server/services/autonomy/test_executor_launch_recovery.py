from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from src.server.services.autonomy import executor


class _Cursor:
    def __init__(self):
        self.sql = ""

    async def execute(self, sql):
        self.sql = " ".join(sql.split())

    async def fetchall(self):
        return [("ticket-1", "user-1", 4)]


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    @asynccontextmanager
    async def cursor(self):
        yield self._cursor


@pytest.mark.asyncio
async def test_launch_recovery_uses_attempt_scoped_key(monkeypatch):
    cursor = _Cursor()

    @asynccontextmanager
    async def connection():
        yield _Connection(cursor)

    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr("src.server.database.pool.get_db_connection", connection)
    monkeypatch.setattr("src.server.database.autonomy.effects.enqueue_effect", enqueue)

    queued = await executor.ensure_launch_effects()

    assert queued == 1
    assert "SELECT ticket_id, user_id, attempts FROM autonomy_tickets" in cursor.sql
    assert "revision" not in cursor.sql
    assert (
        enqueue.await_args.kwargs["idempotency_key"]
        == "autonomy:launch:ticket-1:attempt:5"
    )
    assert enqueue.await_args.kwargs["payload"] == {"ticket_id": "ticket-1"}
