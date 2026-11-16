"""The existing sweeps emit stuck-Ticket and dead-daemon notifications."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.server.services.autonomy import notifications as notes
from src.server.services.autonomy import scheduler, watchdog


@pytest.mark.asyncio
async def test_watchdog_emits_stuck_notifications_without_changing_the_flag_count(
    monkeypatch,
):
    from src.server.database.autonomy import tickets as ticket_db

    monkeypatch.setattr(
        ticket_db,
        "list_watchdog_candidates",
        AsyncMock(
            return_value=[
                {"ticket_id": "t-queued", "status": "queued", "stall_seconds": 2400}
            ]
        ),
    )
    monkeypatch.setattr(
        ticket_db, "update_ticket", AsyncMock(return_value={"ticket_id": "t-queued"})
    )
    monkeypatch.setattr(watchdog, "enqueue_effect", AsyncMock(return_value=True))
    monkeypatch.setattr(watchdog, "sweep_worker_idle_wakes", AsyncMock())
    check = AsyncMock(return_value=4)
    monkeypatch.setattr(notes, "check_stuck_tickets", check)

    assert await watchdog.watchdog_sweep() == 1
    check.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_watchdog_still_returns_flags_when_the_notification_check_fails(
    monkeypatch,
):
    from src.server.database.autonomy import tickets as ticket_db

    monkeypatch.setattr(ticket_db, "list_watchdog_candidates", AsyncMock(return_value=[]))
    monkeypatch.setattr(watchdog, "sweep_worker_idle_wakes", AsyncMock())

    async def boom() -> int:
        raise RuntimeError("notify down")

    monkeypatch.setattr(notes, "check_stuck_tickets", boom)
    assert await watchdog.watchdog_sweep() == 0


@pytest.mark.asyncio
async def test_scheduler_pass_runs_operator_checks_before_a_later_failure(monkeypatch):
    from src.server.database.autonomy import daemon_output, daemons

    monkeypatch.setattr(daemon_output, "expire_old_logs", AsyncMock(return_value=0))
    monkeypatch.setattr(daemons, "recover_expired_daemon_leases", AsyncMock(return_value=0))
    order: list[str] = []

    def spy(label: str):
        async def _inner(*_args, **_kwargs):
            order.append(label)
            return 0

        return _inner

    monkeypatch.setattr(notes, "check_dead_daemons", spy("dead"))
    monkeypatch.setattr(notes, "check_needs_user", spy("needs_user"))
    monkeypatch.setattr(notes, "check_failures", spy("failure"))
    monkeypatch.setattr(notes, "check_worker_quota", spy("quota"))

    async def later(*_args, **_kwargs):
        order.append("expire")
        raise RuntimeError("later")

    monkeypatch.setattr(
        "src.server.database.autonomy.stops.expire_stop_requests", later
    )
    instance = scheduler.AutonomyScheduler.__new__(scheduler.AutonomyScheduler)
    with pytest.raises(RuntimeError, match="later"):
        await instance._run_pass()
    assert order == ["dead", "needs_user", "failure", "quota", "expire"]


@pytest.mark.asyncio
async def test_a_notification_check_error_does_not_stall_the_pass(monkeypatch):
    from src.server.database.autonomy import daemon_output, daemons, stops

    monkeypatch.setattr(daemon_output, "expire_old_logs", AsyncMock(return_value=0))
    monkeypatch.setattr(daemons, "recover_expired_daemon_leases", AsyncMock(return_value=0))

    async def boom():
        raise RuntimeError("notify down")

    monkeypatch.setattr(notes, "check_dead_daemons", boom)
    expire = AsyncMock(return_value=0)
    monkeypatch.setattr(stops, "expire_stop_requests", expire)
    for name in (
        "enqueue_due_polls",
        "recover_stale_evals",
        "start_due_evals",
        "ensure_launch_effects",
        "poll_running_workers",
        "run_due_schedules",
        "watchdog_sweep",
        "sweep_worktree_cleanups",
    ):
        monkeypatch.setattr(scheduler, name, AsyncMock(return_value=0))
    monkeypatch.setattr(
        "src.server.services.refactor.memory_consolidation.run_due_memory_consolidations",
        AsyncMock(return_value={}),
    )
    instance = scheduler.AutonomyScheduler.__new__(scheduler.AutonomyScheduler)
    instance.memory_store = None
    instance.main_agent_orchestrator = None
    await instance._run_pass()
    expire.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_stuck_scan_sees_the_quiet_clock_before_attention_resets_it(monkeypatch):
    """A queued Ticket quiet for 30 minutes must alert on this sweep.

    The attention write sets updated_at to now. Scanning after that write
    makes the same Ticket look freshly touched, so the default alert waits
    another full threshold.
    """
    from src.server.database.autonomy import notifications as notification_db
    from src.server.database.autonomy import tickets as ticket_db
    from src.server.services.autonomy.notifications import (
        MemoryNotificationStore,
        override_store,
    )
    from src.server.services.autonomy.worker_idle import IDLE_WAKE_ENV

    monkeypatch.delenv(IDLE_WAKE_ENV, raising=False)
    clock = {"updated_at": "2026-01-01T00:00:00+00:00", "age_seconds": 1900}
    order: list[str] = []
    updates: list[dict] = []
    seen: dict[str, object] = {}

    async def update_ticket(_ticket_id, **kwargs):
        order.append("update")
        updates.append(kwargs)
        clock["updated_at"] = "now"
        clock["age_seconds"] = 0
        return {"ticket_id": "t-queued", "updated_at": clock["updated_at"]}

    async def list_stuck_tickets(
        *, min_seconds, limit, offset=0, running_seconds=None
    ):
        order.append("scan")
        seen["updated_at"] = clock["updated_at"]
        seen["age_seconds"] = clock["age_seconds"]
        seen["min_seconds"] = min_seconds
        if offset or clock["age_seconds"] < min_seconds:
            return []
        return [
            {
                "ticket_id": "t-queued",
                "user_id": "user-a",
                "status": "queued",
                "title": "Queued work",
                "age_seconds": clock["age_seconds"],
                "updated_at": clock["updated_at"],
            }
        ]

    monkeypatch.setattr(
        ticket_db,
        "list_watchdog_candidates",
        AsyncMock(
            return_value=[
                {
                    "ticket_id": "t-queued",
                    "user_id": "user-a",
                    "status": "queued",
                    "title": "Queued work",
                    "stall_seconds": 1900,
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            ]
        ),
    )
    monkeypatch.setattr(ticket_db, "update_ticket", update_ticket)
    monkeypatch.setattr(notification_db, "list_stuck_tickets", list_stuck_tickets)
    monkeypatch.setattr(notes, "database_is_open", lambda: True)
    monkeypatch.setattr(notes, "macos_deliver", lambda _title, _body: True)

    store = MemoryNotificationStore()
    with override_store(store):
        flagged = await watchdog.watchdog_sweep()

    assert flagged == 1
    assert order[0] == "scan"
    assert "update" in order
    assert seen["updated_at"] == "2026-01-01T00:00:00+00:00"
    assert seen["age_seconds"] >= seen["min_seconds"]
    assert updates[0]["attention"]["question"]
    assert clock["updated_at"] == "now"
    assert await store.unread_count("user-a") == 1


@pytest.mark.asyncio
async def test_attention_update_keeps_the_quiet_clock_in_sql(
    mock_db_connection, mock_cursor, monkeypatch
):
    """The stuck clock is updated_at. An attention flag must not move it to now."""
    from src.server.database.autonomy import tickets as ticket_db

    @asynccontextmanager
    async def _connection(conn=None):
        yield conn if conn is not None else mock_db_connection

    monkeypatch.setattr(ticket_db, "get_db_connection", _connection)
    quiet = datetime(2026, 1, 1, tzinfo=timezone.utc)
    mock_cursor.fetchone = AsyncMock(
        return_value={"ticket_id": "t-queued", "revision": 2, "updated_at": quiet}
    )

    await ticket_db.update_ticket(
        "t-queued",
        attention={"question": "Queued for 40 minutes without a Worker start."},
        preserve_updated_at=quiet,
    )
    sql, params = mock_cursor.execute.await_args.args
    assert "updated_at = %s" in sql
    assert "updated_at = NOW()" not in sql
    assert quiet in params
    assert "revision = revision + 1" in sql

    mock_cursor.execute.reset_mock()
    await ticket_db.update_ticket(
        "t-queued",
        attention={"question": "A real edit."},
    )
    sql, _params = mock_cursor.execute.await_args.args
    assert "updated_at = NOW()" in sql


@pytest.mark.asyncio
async def test_later_sweeps_keep_unresolved_stalls_and_drain_deferred_banners(
    monkeypatch,
):
    """Three sweeps of four unresolved stalls.

    The attention write applies ``updated_at = now``. Without putting the
    quiet clock back, the next scan is empty, retain drops the inbox
    (badge 4, then 0), and the banner still waiting is discarded. A later
    sweep inserts the stalls again and banners the first three a second
    time (cumulative banners 3, then 3, then 6).
    """
    from src.server.database.autonomy import notifications as notification_db
    from src.server.database.autonomy import tickets as ticket_db
    from src.server.services.autonomy.notifications import (
        MemoryNotificationStore,
        override_store,
    )
    from src.server.services.autonomy.worker_idle import IDLE_WAKE_ENV

    monkeypatch.delenv(IDLE_WAKE_ENV, raising=False)
    monkeypatch.setenv(notes.STUCK_THRESHOLD_ENV, "1800")
    quiet = "2026-01-01T00:00:00+00:00"
    statuses = ("queued", "waiting", "running", "blocked")
    tickets = [
        {
            "ticket_id": f"stall-{index}",
            "user_id": "user-a",
            "status": status,
            "title": f"Stall {status}",
            "updated_at": quiet,
            "age_seconds": 4000,
            "stall_seconds": 4000,
            "attention": None,
            "revision": 1,
        }
        for index, status in enumerate(statuses)
    ]
    by_id = {ticket["ticket_id"]: ticket for ticket in tickets}
    preserves: list[object] = []

    async def update_ticket(ticket_id, **kwargs):
        # The pre-fix write: every update is activity, so the quiet clock
        # becomes now even when the caller asked to keep it. The sweep has
        # to put that clock back on the row the next scan reads.
        row = by_id[ticket_id]
        preserves.append(kwargs.get("preserve_updated_at"))
        if "attention" in kwargs:
            row["attention"] = kwargs["attention"]
        row["updated_at"] = "now"
        row["age_seconds"] = 0
        row["stall_seconds"] = 0
        row["revision"] = int(row["revision"]) + 1
        return row

    async def list_stuck_tickets(
        *, min_seconds, limit, offset=0, running_seconds=None
    ):
        quiet_rows = [
            ticket
            for ticket in tickets
            if ticket["status"] in {"queued", "waiting", "running", "blocked"}
            and ticket.get("updated_at") == quiet
            and int(ticket.get("age_seconds") or 0) >= min_seconds
        ]
        return quiet_rows[offset : offset + limit]

    async def list_watchdog_candidates(**_kwargs):
        return [ticket for ticket in tickets if not ticket.get("attention")]

    monkeypatch.setattr(ticket_db, "list_watchdog_candidates", list_watchdog_candidates)
    monkeypatch.setattr(ticket_db, "update_ticket", update_ticket)
    monkeypatch.setattr(notification_db, "list_stuck_tickets", list_stuck_tickets)
    monkeypatch.setattr(notes, "database_is_open", lambda: True)
    banners: list[str] = []

    def deliver(_title: str, body: str) -> bool:
        banners.append(body)
        return True

    monkeypatch.setattr(notes, "macos_deliver", deliver)
    monkeypatch.setattr(watchdog, "enqueue_effect", AsyncMock(return_value=True))

    store = MemoryNotificationStore()
    badges: list[int] = []
    shown: list[int] = []
    with override_store(store):
        for _pass in range(3):
            await watchdog.watchdog_sweep()
            badges.append(await store.unread_count("user-a"))
            shown.append(len(banners))
            assert all(ticket["updated_at"] == quiet for ticket in tickets)
            assert all(ticket["age_seconds"] == 4000 for ticket in tickets)

    assert badges == [4, 4, 4]
    # Pass 1 shows the cap of three. Pass 2 shows the deferred banner.
    # Pass 3 does not banner those stalls again.
    assert shown == [3, 4, 4]
    assert len(set(banners)) == 4
    assert len(preserves) == 4
    assert all(stamp == datetime.fromisoformat(quiet) for stamp in preserves)


@pytest.mark.asyncio
async def test_attention_write_cannot_rewind_concurrent_progress(monkeypatch):
    """A status/revision change after the scan fences out the quiet-clock write."""
    from src.server.database.autonomy import tickets as ticket_db

    quiet = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    progress = datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc)
    scanned = {
        "ticket_id": "t-queued",
        "user_id": "user-a",
        "status": "queued",
        "title": "Queued work",
        "updated_at": quiet,
        "stall_seconds": 1900,
        "revision": 4,
    }
    current = dict(scanned)
    write: dict[str, object] = {}

    async def update_ticket(_ticket_id, **kwargs):
        write.update(kwargs)
        current.update(status="running", revision=5, updated_at=progress)
        if kwargs.get("expected_status", current["status"]) != current["status"]:
            return None
        if kwargs.get("expected_revision", current["revision"]) != current["revision"]:
            return None
        current["updated_at"] = kwargs.get("preserve_updated_at", progress)
        current["revision"] = int(current["revision"]) + 1
        return dict(current)

    monkeypatch.setattr(
        ticket_db, "list_watchdog_candidates", AsyncMock(return_value=[scanned])
    )
    monkeypatch.setattr(ticket_db, "update_ticket", update_ticket)
    monkeypatch.setattr(watchdog, "sweep_worker_idle_wakes", AsyncMock())
    monkeypatch.setattr(notes, "check_stuck_tickets", AsyncMock(return_value=0))

    assert await watchdog.watchdog_sweep() == 0
    assert write["expected_status"] == "queued"
    assert write["expected_revision"] == 4
    assert current["status"] == "running"
    assert current["revision"] == 5
    assert current["updated_at"] == progress
