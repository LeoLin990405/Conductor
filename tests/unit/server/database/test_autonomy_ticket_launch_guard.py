"""The fail-closed cloud launch transition is fenced by its ticket snapshot."""

from contextlib import asynccontextmanager

import pytest

from src.server.database.autonomy import tickets


class _Cursor:
    def __init__(self, row):
        self.row = row
        self.executions: list[tuple[str, tuple]] = []

    async def execute(self, sql, params):
        self.executions.append((" ".join(sql.split()), tuple(params)))

    async def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, cursor):
        self.fake_cursor = cursor

    @asynccontextmanager
    async def cursor(self, **_kwargs):
        yield self.fake_cursor

    @asynccontextmanager
    async def transaction(self):
        yield self


@pytest.mark.asyncio
async def test_cloud_worker_rejection_is_revision_and_route_fenced(monkeypatch):
    cursor = _Cursor({"ticket_id": "t1", "status": "failed"})

    @asynccontextmanager
    async def get_connection():
        yield _Connection(cursor)

    monkeypatch.setattr(tickets, "get_db_connection", get_connection)
    result = await tickets.fail_unisolated_cloud_worker(
        {
            "ticket_id": "t1",
            "status": "running",
            "revision": 8,
            "worker_daemon_id": None,
            "worker_thread_id": "old-thread",
            "worker_run_id": None,
        },
        error="OS-isolated runtime required",
    )

    assert result["status"] == "failed"
    ticket_sql, ticket_params = next(
        item for item in cursor.executions if "UPDATE autonomy_tickets" in item[0]
    )
    assert "status = %s AND revision = %s" in ticket_sql
    assert "worker_daemon_id IS NULL" in ticket_sql
    assert "worker_thread_id IS NOT DISTINCT FROM %s" in ticket_sql
    assert "worker_run_id IS NOT DISTINCT FROM %s" in ticket_sql
    assert "status = 'queued' OR" in ticket_sql
    assert "worker_thread_id IS NOT NULL" in ticket_sql
    assert "worker_run_id IS NULL" in ticket_sql
    assert "orchestration_only" in ticket_sql
    assert "orchestration_child" in ticket_sql
    assert "ended_at = NOW()" in ticket_sql
    assert ticket_params == (
        "OS-isolated runtime required",
        "t1",
        "running",
        8,
        "old-thread",
        None,
    )


@pytest.mark.asyncio
async def test_cloud_worker_rejection_refuses_missing_revision_without_database(monkeypatch):
    def forbidden_connection():
        raise AssertionError("missing revision must not open the database")

    monkeypatch.setattr(tickets, "get_db_connection", forbidden_connection)
    result = await tickets.fail_unisolated_cloud_worker(
        {"ticket_id": "t1", "status": "queued", "revision": None},
        error="OS-isolated runtime required",
    )
    assert result is None


@pytest.mark.asyncio
async def test_cloud_worker_rejection_refuses_active_running_run_without_database(
    monkeypatch,
):
    def forbidden_connection():
        raise AssertionError("active run must not be changed")

    monkeypatch.setattr(tickets, "get_db_connection", forbidden_connection)
    result = await tickets.fail_unisolated_cloud_worker(
        {
            "ticket_id": "t1",
            "status": "running",
            "revision": 9,
            "worker_thread_id": "active-thread",
            "worker_run_id": "active-run",
            "worker_daemon_id": None,
        },
        error="OS-isolated runtime required",
    )
    assert result is None
