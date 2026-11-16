"""Postgres SQL contracts for main-agent claims use a transaction fake."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from src.server.database.main_agent import PostgresMainAgentRepository


class _GuardCursor:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.calls = []

    async def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    async def fetchone(self):
        return (self.acquired,)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _GuardConnection:
    def __init__(self, *, acquired=True):
        self.cur = _GuardCursor(acquired)
        self.close = AsyncMock()

    def cursor(self):
        return self.cur


class _Cursor:
    def __init__(self, selected=None, *, rowcount=1):
        self.selected = selected
        self.rowcount = rowcount
        self.calls = []

    async def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    async def fetchone(self):
        row, self.selected = self.selected, None
        return row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Transaction:
    def __init__(self):
        self.exit_exception = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, *_):
        self.exit_exception = exc_type
        return False


class _Connection:
    def __init__(self, cursor):
        self.cur = cursor
        self.tx = _Transaction()

    def cursor(self, row_factory=None):
        return self.cur

    def transaction(self):
        return self.tx


def _connection(cursor):
    conn = _Connection(cursor)

    @asynccontextmanager
    async def get_db_connection():
        yield conn

    return conn, get_db_connection


@pytest.mark.asyncio
async def test_postgres_claim_locks_owner_row_and_cas_updates_run_revision():
    payload = {
        "id": "r1",
        "status": "queued",
        "phase": "execute",
        "revision": 8,
        "tasks": [{"id": "t1", "status": "queued", "depends_on": [], "revision": 0}],
        "alerts": [],
    }
    cursor = _Cursor({"user_id": "u1", "revision": 8, "payload": payload})
    conn, fake = _connection(cursor)
    with patch("src.server.database.main_agent.get_db_connection", new=fake):
        result = await PostgresMainAgentRepository().claim_task(
            "r1", "t1", "worker-1", 0, 60, user_id="u1", daemon_id="d1"
        )

    assert result and result["assigned_agent_id"] == "worker-1"
    assert result["daemon_id"] == "d1"
    assert result["claim_token"]
    select_sql, select_params = cursor.calls[0]
    assert "AND user_id = %s FOR UPDATE" in select_sql
    assert select_params == ["r1", "u1"]
    update_sql, update_params = cursor.calls[1]
    assert "WHERE run_id = %s AND user_id = %s AND revision = %s" in update_sql
    assert update_params[-3:] == ("r1", "u1", 8)
    assert conn.tx.exit_exception is None


@pytest.mark.asyncio
async def test_postgres_task_revision_conflict_and_owner_miss_do_not_write():
    payload = {
        "id": "r1",
        "status": "queued",
        "phase": "execute",
        "revision": 12,
        "tasks": [{"id": "t1", "status": "queued", "depends_on": [], "revision": 4}],
    }
    cursor = _Cursor({"user_id": "u1", "revision": 12, "payload": payload})
    _, fake = _connection(cursor)
    with patch("src.server.database.main_agent.get_db_connection", new=fake):
        conflict = await PostgresMainAgentRepository().claim_task(
            "r1", "t1", "worker", 3, 60, user_id="u1"
        )
    assert conflict is None
    assert len(cursor.calls) == 1

    cursor = _Cursor(None)
    conn, fake = _connection(cursor)
    with patch("src.server.database.main_agent.get_db_connection", new=fake):
        foreign = await PostgresMainAgentRepository().claim_task(
            "r1", "t1", "worker", 4, 60, user_id="u2"
        )
    assert foreign is None
    assert len(cursor.calls) == 1
    assert cursor.calls[0][1] == ["r1", "u2"]
    assert conn.tx.exit_exception is None


@pytest.mark.asyncio
async def test_postgres_exception_escapes_transaction_for_rollback():
    cursor = _Cursor(
        {
            "user_id": "u1",
            "revision": 0,
            "payload": {
                "id": "r1",
                "status": "queued",
                "phase": "execute",
                "revision": 0,
                "tasks": [
                    {"id": "t1", "status": "queued", "depends_on": [], "revision": 0}
                ],
            },
        }
    )
    conn, fake = _connection(cursor)

    async def fail_mutation(*args, **kwargs):
        raise RuntimeError("write failed")

    conn.cur.execute = fail_mutation
    with patch("src.server.database.main_agent.get_db_connection", new=fake):
        with pytest.raises(RuntimeError, match="write failed"):
            await PostgresMainAgentRepository().claim_task(
                "r1", "t1", "worker", 0, 60, user_id="u1"
            )
    assert conn.tx.exit_exception is RuntimeError


@pytest.mark.asyncio
async def test_run_guard_releases_session_lock_and_closes_dedicated_connection():
    conn = _GuardConnection()
    with patch(
        "src.server.database.main_agent.open_session_connection",
        new=AsyncMock(return_value=conn),
    ):
        async with PostgresMainAgentRepository().run_guard("run-1") as acquired:
            assert acquired

    assert [sql for sql, _ in conn.cur.calls] == [
        "SELECT pg_try_advisory_lock(%s)",
        "SELECT pg_advisory_unlock(%s)",
    ]
    assert conn.cur.calls[0][1] == conn.cur.calls[1][1]
    conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_guard_contention_closes_session_without_unlocking():
    conn = _GuardConnection(acquired=False)
    with patch(
        "src.server.database.main_agent.open_session_connection",
        new=AsyncMock(return_value=conn),
    ):
        async with PostgresMainAgentRepository().run_guard("run-1") as acquired:
            assert not acquired

    assert [sql for sql, _ in conn.cur.calls] == ["SELECT pg_try_advisory_lock(%s)"]
    conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_guard_cancellation_releases_lock_and_closes_session():
    conn = _GuardConnection()
    started = asyncio.Event()

    async def hold_guard():
        async with PostgresMainAgentRepository().run_guard("run-1") as acquired:
            assert acquired
            started.set()
            await asyncio.Event().wait()

    with patch(
        "src.server.database.main_agent.open_session_connection",
        new=AsyncMock(return_value=conn),
    ):
        task = asyncio.create_task(hold_guard())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert [sql for sql, _ in conn.cur.calls] == [
        "SELECT pg_try_advisory_lock(%s)",
        "SELECT pg_advisory_unlock(%s)",
    ]
    conn.close.assert_awaited_once()
