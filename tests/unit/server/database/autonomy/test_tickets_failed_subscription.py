"""Failed Tickets end live subscriptions without becoming follow-up-terminal.

The fake cursor mirrors the psycopg3 async surface the module uses, so these
tests pin the SQL and its parameters without a live Postgres. ``failed`` is
not in ``_TERMINAL`` (a failed Ticket still owns a follow-up), but intake
routes by active subscription rows, so a failed write must end them.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from src.server.database.autonomy import tickets

_SUBSCRIPTION_END = (
    "UPDATE autonomy_subscriptions SET ended_at = NOW(), end_reason = %s "
    "WHERE ticket_id = %s AND ended_at IS NULL"
)


class _Cursor:
    def __init__(self, fetchone_results=None):
        self.fetchone_results = iter(fetchone_results or [])
        self.executions: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, params=()):
        self.executions.append((" ".join(sql.split()), tuple(params)))

    async def fetchone(self):
        return next(self.fetchone_results)


class _Connection:
    def __init__(self, cursor: _Cursor):
        self.fake_cursor = cursor
        self.transactions = 0

    @asynccontextmanager
    async def cursor(self, **_kwargs):
        yield self.fake_cursor

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        yield self


def _patch_connection(monkeypatch, cursor: _Cursor) -> _Connection:
    connection = _Connection(cursor)

    @asynccontextmanager
    async def get_connection():
        yield connection

    monkeypatch.setattr(tickets, "get_db_connection", get_connection)
    return connection


def _subscription_updates(cursor: _Cursor) -> list[tuple[str, tuple]]:
    return [
        (sql, params)
        for sql, params in cursor.executions
        if sql.startswith("UPDATE autonomy_subscriptions")
    ]


def _assert_subscription_ended(cursor: _Cursor, ticket_id: str = "t-1") -> None:
    updates = _subscription_updates(cursor)
    assert updates == [(_SUBSCRIPTION_END, (tickets.END_TICKET_ENDED, ticket_id))]


@pytest.mark.asyncio
async def test_failed_update_ends_subscriptions(monkeypatch):
    cursor = _Cursor(fetchone_results=[{"ticket_id": "t-1", "status": "failed"}])
    connection = _patch_connection(monkeypatch, cursor)

    updated = await tickets.update_ticket("t-1", status="failed")

    assert updated is not None
    assert connection.transactions == 1
    _assert_subscription_ended(cursor)


@pytest.mark.asyncio
async def test_running_update_does_not_end_subscriptions(monkeypatch):
    cursor = _Cursor(fetchone_results=[{"ticket_id": "t-1", "status": "running"}])
    _patch_connection(monkeypatch, cursor)

    await tickets.update_ticket("t-1", status="running")

    assert _subscription_updates(cursor) == []


@pytest.mark.asyncio
async def test_failed_update_with_no_row_does_not_end_subscriptions(monkeypatch):
    cursor = _Cursor(fetchone_results=[None])
    _patch_connection(monkeypatch, cursor)

    missed = await tickets.update_ticket("t-1", status="failed", expected_status="running")

    assert missed is None
    assert _subscription_updates(cursor) == []


@pytest.mark.asyncio
async def test_worker_report_failed_status_ends_subscriptions(monkeypatch):
    cursor = _Cursor(
        fetchone_results=[{"ticket_id": "t-1", "status": "failed"}, {"report_id": "r-1"}]
    )
    connection = _patch_connection(monkeypatch, cursor)

    report = await tickets.record_worker_report(
        ticket_id="t-1",
        expected_thread_id="th-1",
        state="failed",
        summary="runtime exploded",
        ticket_fields={"status": "failed", "summary": "runtime exploded"},
    )

    assert report is not None
    assert connection.transactions == 1
    _assert_subscription_ended(cursor)


@pytest.mark.asyncio
async def test_worker_report_running_status_does_not_end_subscriptions(monkeypatch):
    cursor = _Cursor(
        fetchone_results=[{"ticket_id": "t-1", "status": "running"}, {"report_id": "r-1"}]
    )
    _patch_connection(monkeypatch, cursor)

    await tickets.record_worker_report(
        ticket_id="t-1",
        expected_thread_id="th-1",
        state="working",
        summary="still going",
        ticket_fields={"status": "running", "summary": "still going"},
    )

    assert _subscription_updates(cursor) == []


@pytest.mark.asyncio
async def test_worker_report_failed_with_no_row_does_not_end_subscriptions(monkeypatch):
    cursor = _Cursor(fetchone_results=[None])
    _patch_connection(monkeypatch, cursor)

    missed = await tickets.record_worker_report(
        ticket_id="t-1",
        expected_thread_id="th-1",
        state="failed",
        summary="late",
        ticket_fields={"status": "failed"},
    )

    assert missed is None
    assert _subscription_updates(cursor) == []


@pytest.mark.asyncio
async def test_fail_ticket_with_report_ends_subscriptions(monkeypatch):
    cursor = _Cursor(fetchone_results=[{"ticket_id": "t-1", "status": "failed"}])
    connection = _patch_connection(monkeypatch, cursor)

    updated = await tickets.fail_ticket_with_report(
        "t-1",
        error="runtime exploded",
        summary="runtime exploded",
        expected_status="queued",
    )

    assert updated is not None
    assert updated["status"] == "failed"
    assert connection.transactions == 1
    _assert_subscription_ended(cursor)
    assert any("INSERT INTO autonomy_ticket_reports" in sql for sql, _params in cursor.executions)


@pytest.mark.asyncio
async def test_fail_ticket_with_report_no_row_does_not_end_subscriptions(monkeypatch):
    cursor = _Cursor(fetchone_results=[None])
    _patch_connection(monkeypatch, cursor)

    missed = await tickets.fail_ticket_with_report(
        "t-1", error="x", summary="x", expected_revision=3
    )

    assert missed is None
    assert _subscription_updates(cursor) == []
    assert not any(
        "INSERT INTO autonomy_ticket_reports" in sql for sql, _params in cursor.executions
    )


@pytest.mark.asyncio
async def test_fail_unisolated_cloud_worker_ends_subscriptions(monkeypatch):
    cursor = _Cursor(fetchone_results=[{"ticket_id": "t-1", "status": "failed"}])
    connection = _patch_connection(monkeypatch, cursor)

    updated = await tickets.fail_unisolated_cloud_worker(
        {
            "ticket_id": "t-1",
            "status": "queued",
            "revision": 4,
            "worker_daemon_id": None,
            "worker_thread_id": None,
            "worker_run_id": None,
        },
        error="OS-isolated runtime required",
    )

    assert updated is not None
    assert updated["status"] == "failed"
    assert connection.transactions == 1
    _assert_subscription_ended(cursor)


@pytest.mark.asyncio
async def test_fail_unisolated_cloud_worker_no_row_does_not_end_subscriptions(monkeypatch):
    cursor = _Cursor(fetchone_results=[None])
    connection = _patch_connection(monkeypatch, cursor)

    missed = await tickets.fail_unisolated_cloud_worker(
        {
            "ticket_id": "t-1",
            "status": "queued",
            "revision": 4,
            "worker_daemon_id": None,
        },
        error="OS-isolated runtime required",
    )

    assert missed is None
    assert connection.transactions == 1
    assert _subscription_updates(cursor) == []


def test_owns_followup_of_a_failed_row_stays_true():
    assert tickets.owns_followup({"status": "failed", "mandate": {}}) is True


def test_terminal_statuses_are_unchanged():
    assert tickets._TERMINAL == ("completed", "cancelled", "archived")
    assert "failed" not in tickets._TERMINAL
    assert tickets._ENDS_SUBSCRIPTIONS == (*tickets._TERMINAL, "failed")
