"""Line CRUD, the ownership audit trail, reassignment, and the dashboard query."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import pytest

from src.server.database.autonomy import lines

NOW = datetime.now(timezone.utc)


class _Cursor:
    def __init__(self, fetchone_results=None, fetchall_results=None):
        self.fetchone_results = iter(fetchone_results or [])
        self.fetchall_results = iter(fetchall_results or [])
        self.executions: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, params=()):
        self.executions.append((" ".join(sql.split()), tuple(params)))

    async def fetchone(self):
        return next(self.fetchone_results)

    async def fetchall(self):
        return next(self.fetchall_results)


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

    monkeypatch.setattr(lines, "get_db_connection", get_connection)
    return connection


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_line_inserts_and_returns_the_row(monkeypatch):
    row = {
        "line_id": "line-1",
        "user_id": "u1",
        "name": "ob-tickets",
        "branch": "feat/ob-tickets",
        "worktree_path": "/wt/ob-tickets",
        "status": "active",
        "created_at": NOW,
        "updated_at": NOW,
    }
    cursor = _Cursor(fetchone_results=[row])
    _patch_connection(monkeypatch, cursor)

    out = await lines.create_line(
        user_id="u1", name="  ob-tickets  ", branch="feat/ob-tickets",
        worktree_path="/wt/ob-tickets",
    )

    sql, params = cursor.executions[0]
    assert sql.startswith("INSERT INTO autonomy_lines (user_id, name, branch, worktree_path)")
    assert params == ("u1", "ob-tickets", "feat/ob-tickets", "/wt/ob-tickets")
    assert out["line_id"] == "line-1"


@pytest.mark.asyncio
async def test_list_lines_hides_archived_unless_asked(monkeypatch):
    cursor = _Cursor(fetchall_results=[[{"line_id": "line-1", "status": "active"}]])
    _patch_connection(monkeypatch, cursor)

    await lines.list_lines("u1")

    sql, params = cursor.executions[0]
    assert "FROM autonomy_lines" in sql
    assert "status <> 'archived'" in sql
    assert params == ("u1", False)


@pytest.mark.asyncio
async def test_archive_line_is_guarded_and_user_scoped(monkeypatch):
    row = {"line_id": "line-1", "status": "archived"}
    cursor = _Cursor(fetchone_results=[row])
    _patch_connection(monkeypatch, cursor)

    out = await lines.archive_line("u1", "line-1")

    sql, params = cursor.executions[0]
    assert "UPDATE autonomy_lines" in sql
    assert "status <> 'archived'" in sql
    assert params == ("u1", "line-1")
    assert out["status"] == "archived"


@pytest.mark.asyncio
async def test_set_ticket_line_writes_the_grouping(monkeypatch):
    cursor = _Cursor()
    _patch_connection(monkeypatch, cursor)

    await lines.set_ticket_line("ticket-1", "line-1")

    sql, params = cursor.executions[0]
    assert "UPDATE autonomy_tickets SET line_id = %s" in sql
    assert "revision = revision + 1" in sql
    assert params == ("line-1", "ticket-1")


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_ticket_event_rejects_unknown_actions(monkeypatch):
    cursor = _Cursor()
    _patch_connection(monkeypatch, cursor)

    with pytest.raises(ValueError, match="unsupported ticket event action"):
        await lines.record_ticket_event(
            ticket_id="t1", user_id="u1", actor="user", action="deleted"
        )
    assert cursor.executions == []


@pytest.mark.asyncio
async def test_record_ticket_event_persists_owner_transition(monkeypatch):
    cursor = _Cursor()
    _patch_connection(monkeypatch, cursor)

    await lines.record_ticket_event(
        ticket_id="t1",
        user_id="u1",
        actor="user",
        action="reassigned",
        from_owner="cloud",
        to_owner="d1",
        detail={"from_status": "queued"},
    )

    sql, params = cursor.executions[0]
    assert "INSERT INTO autonomy_ticket_events" in sql
    assert params[:6] == ("t1", "u1", "user", "reassigned", "cloud", "d1")
    assert params[6].obj == {"from_status": "queued"}


def test_owner_ref_maps_null_to_cloud():
    assert lines.owner_ref(None) == "cloud"
    assert lines.owner_ref("") == "cloud"
    assert lines.owner_ref("d1") == "d1"


# ---------------------------------------------------------------------------
# Reassignment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reassign_ticket_fences_from_owner_and_open_status(monkeypatch):
    cursor = _Cursor(
        fetchone_results=[
            {
                "ticket_id": "t1",
                "status": "queued",
                "worker_daemon_id": None,
                "worker_provider": "kimi",
                "worker_model": "",
                "updated_at": NOW,
                "user_id": "u1",
                "attempts": 3,
                "revision": 8,
                "worker_lease_id": None,
            }
        ]
    )
    _patch_connection(monkeypatch, cursor)

    out = await lines.reassign_ticket(
        "t1",
        user_id="u1",
        from_daemon_id=None,
        worker_daemon_id="d1",
        worker_provider="codex",
        worker_model="",
        expected_revision=8,
        expected_status="queued",
        expected_lease_id=None,
    )

    sql, params = cursor.executions[0]
    assert "UPDATE autonomy_tickets" in sql
    assert "worker_daemon_id IS NOT DISTINCT FROM %s" in sql
    assert "user_id = %s" in sql
    assert "revision = %s" in sql
    assert "status = %s" in sql
    assert "worker_lease_id IS NOT DISTINCT FROM %s::uuid" in sql
    assert "status = ANY(%s)" in sql
    assert "worker_lease_id = NULL" in sql
    # The old Worker identity must not survive the ownership change.
    assert "worker_thread_id = NULL" in sql
    assert "worker_run_id = NULL" in sql
    assert "WHEN status = 'running' THEN 'queued'" in sql
    assert "WHEN status = 'cancelled' AND %s THEN 'queued'" in sql
    assert params == (
        "d1", "codex", "", False, False, False, False,
        "t1", "u1", None, 8, "queued", None,
        [*lines.OPEN_STATUSES, "cancelled"],
    )
    assert out["status"] == "queued"


@pytest.mark.asyncio
async def test_reassign_ticket_normalizes_uuid_fields_for_service_call(monkeypatch):
    ticket_id = UUID("11111111-1111-4111-8111-111111111111")
    daemon_id = UUID("22222222-2222-4222-8222-222222222222")
    cursor = _Cursor(
        fetchone_results=[
            {
                "ticket_id": ticket_id,
                "user_id": "u1",
                "worker_daemon_id": daemon_id,
                "status": "queued",
                "mandate": {},
                "attention": None,
                "attempts": 2,
            }
        ]
    )
    _patch_connection(monkeypatch, cursor)

    out = await lines.reassign_ticket(
        str(ticket_id),
        user_id="u1",
        from_daemon_id=None,
        worker_daemon_id=str(daemon_id),
        worker_provider="codex",
        worker_model="",
        expected_revision=3,
        expected_status="queued",
        expected_lease_id=None,
    )

    assert out["ticket_id"] == str(ticket_id)
    assert out["worker_daemon_id"] == str(daemon_id)


@pytest.mark.asyncio
async def test_reassign_ticket_returns_none_when_the_fence_misses(monkeypatch):
    cursor = _Cursor(fetchone_results=[None])
    _patch_connection(monkeypatch, cursor)

    out = await lines.reassign_ticket(
        "t1",
        user_id="u1",
        from_daemon_id="d2",
        worker_daemon_id="d1",
        worker_provider="codex",
        worker_model="",
        expected_revision=4,
        expected_status="running",
        expected_lease_id="lease-1",
    )

    assert out is None


@pytest.mark.asyncio
async def test_reassign_writes_the_audit_event_in_the_same_transaction(monkeypatch):
    cursor = _Cursor(
        fetchone_results=[
            {
                "ticket_id": "t1",
                "status": "queued",
                "worker_daemon_id": "d1",
                "user_id": "u1",
                "attempts": 1,
                "revision": 2,
            }
        ]
    )
    connection = _patch_connection(monkeypatch, cursor)

    await lines.reassign_ticket(
        "t1",
        user_id="u1",
        from_daemon_id=None,
        worker_daemon_id="d1",
        worker_provider="codex",
        worker_model="",
        expected_revision=2,
        expected_status="queued",
        expected_lease_id=None,
        restart_cancelled=True,
        event={
            "ticket_id": "t1",
            "user_id": "u1",
            "actor": "user",
            "action": "reassigned",
            "from_owner": "cloud",
            "to_owner": "d1",
            "detail": {"from_status": "cancelled", "status": "queued"},
        },
    )

    assert connection.transactions == 1
    assert "INSERT INTO autonomy_ticket_events" in cursor.executions[1][0]
    assert cursor.executions[1][1][3] == "reassigned"
    assert cursor.executions[0][1][3] is True


@pytest.mark.asyncio
async def test_reassign_fence_miss_does_not_write_an_audit_event(monkeypatch):
    cursor = _Cursor(fetchone_results=[None])
    _patch_connection(monkeypatch, cursor)

    out = await lines.reassign_ticket(
        "t1",
        user_id="u1",
        from_daemon_id="d1",
        worker_daemon_id=None,
        worker_provider="codex",
        worker_model="",
        expected_revision=1,
        expected_status="cancelled",
        expected_lease_id=None,
        restart_cancelled=True,
        event={
            "ticket_id": "t1",
            "user_id": "u1",
            "actor": "user",
            "action": "reassigned",
            "detail": {},
        },
    )

    assert out is None
    assert len(cursor.executions) == 1


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def _ticket_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "ticket_id": "t1",
        "line_id": None,
        "title": "Ticket",
        "status": "queued",
        "attempts": 0,
        "worker_provider": "",
        "updated_at": NOW,
        "owner": "cloud",
        "last_activity_at": NOW,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_overview_groups_tickets_by_line_and_counts_open(monkeypatch):
    line_rows = [
        {
            "line_id": "line-1",
            "user_id": "u1",
            "name": "ob-tickets",
            "branch": "feat/ob-tickets",
            "worktree_path": "/wt/ob-tickets",
            "status": "active",
            "created_at": NOW,
            "updated_at": NOW,
        }
    ]
    ticket_rows = [
        _ticket_row(ticket_id="t1", line_id="line-1", status="running", owner="m5"),
        _ticket_row(ticket_id="t2", line_id="line-1", status="completed"),
        _ticket_row(ticket_id="t3", line_id=None, status="blocked"),
    ]
    cursor = _Cursor(fetchall_results=[line_rows, ticket_rows])
    _patch_connection(monkeypatch, cursor)

    overview = await lines.ticket_lines_overview("u1")

    sql, params = cursor.executions[1]
    assert "LEFT JOIN autonomy_daemons d ON d.daemon_id = t.worker_daemon_id" in sql
    assert "LEFT JOIN autonomy_lines l ON l.line_id = t.line_id" in sql
    assert "COALESCE(d.name, 'cloud') AS owner" in sql
    assert "COALESCE(e.last_event_at, t.updated_at)" in sql
    assert "t.status <> 'archived'" in sql
    assert "l.line_id IS NULL OR l.status <> 'archived'" in sql
    assert params == ("u1", False, False, 500)

    (group,) = overview["lines"]
    assert group["line"]["name"] == "ob-tickets"
    assert group["ticket_count"] == 2
    assert group["open_count"] == 1
    assert [t["ticket_id"] for t in group["tickets"]] == ["t1", "t2"]
    assert group["tickets"][0]["owner"] == "m5"
    assert overview["ungrouped"]["ticket_count"] == 1
    assert overview["ungrouped"]["open_count"] == 1
    assert overview["ungrouped"]["line"] is None


@pytest.mark.asyncio
async def test_overview_omits_ungrouped_when_empty(monkeypatch):
    cursor = _Cursor(fetchall_results=[[], []])
    _patch_connection(monkeypatch, cursor)

    overview = await lines.ticket_lines_overview("u1")

    assert overview["lines"] == []
    assert overview["ungrouped"] is None


@pytest.mark.asyncio
async def test_list_ticket_events_returns_newest_first(monkeypatch):
    rows = [
        {"event_id": "e2", "ticket_id": "t1", "detail": {"a": 1}, "created_at": NOW},
        {"event_id": "e1", "ticket_id": "t1", "detail": None, "created_at": NOW},
    ]
    cursor = _Cursor(fetchall_results=[rows])
    _patch_connection(monkeypatch, cursor)

    events = await lines.list_ticket_events("t1")

    assert [e["event_id"] for e in events] == ["e2", "e1"]
    assert events[1]["detail"] == {}
