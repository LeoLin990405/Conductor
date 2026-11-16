"""The four ownership operations: guards, audit writes, and side effects.

Every collaborator is faked — ticket/line/daemon DB modules, the effect queue,
the mandate revalidation, and the thread dispatcher — so the tests pin the
service's decision logic, not SQL.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.server.services.autonomy import ownership

NOW = datetime.now(timezone.utc)
TICKET_ID = "22222222-2222-4222-8222-222222222222"


def _ticket(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ticket_id": TICKET_ID,
        "user_id": "test-user-123",
        "status": "queued",
        "mandate": {},
        "worker_daemon_id": None,
        "worker_provider": "kimi",
        "worker_model": "",
        "worker_thread_id": None,
        "worker_run_id": None,
        "attempts": 1,
        "ended_at": None,
        "revision": 0,
        "worker_lease_id": None,
    }
    row.update(overrides)
    return row


@pytest.fixture
def fakes(monkeypatch):
    ticket_db = SimpleNamespace(
        get_ticket=AsyncMock(),
        update_ticket=AsyncMock(),
    )
    events: list[dict[str, Any]] = []
    line_db = SimpleNamespace(
        reassign_ticket=AsyncMock(),
        record_ticket_event=AsyncMock(
            side_effect=lambda **kw: events.append(kw)
        ),
        set_ticket_line=AsyncMock(),
        get_line=AsyncMock(),
        owner_ref=ownership.line_db.owner_ref,
    )
    stop_request = AsyncMock(
        return_value={
            "outcome": "ok",
            "ticket": _ticket(status="cancelled", ended_at=NOW),
            "stop": {"mode": "backend", "verification": "not_applicable"},
        }
    )
    release = {
        "stop": None,
        "live_lease": False,
        "may_have_launched": False,
        "ticket": None,
    }

    async def release_snapshot(*, user_id, ticket_id):
        return {
            "outcome": "ok",
            "stop": release["stop"],
            "live_lease": release["live_lease"],
            "may_have_launched": release["may_have_launched"],
            "ticket": release["ticket"],
        }

    force_ack = AsyncMock(
        return_value={
            "outcome": "ok",
            "ticket": None,
            "stop": {
                "mode": "daemon",
                "verification": "verified",
                "daemon_id": "d1",
            },
        }
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.stop.request_ticket_stop", stop_request
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.stop.release_snapshot", release_snapshot
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.stop.force_acknowledge_unverified", force_ack
    )
    daemon_db = SimpleNamespace(get_daemon=AsyncMock())
    enqueue_effect = AsyncMock()
    dispatcher = MagicMock()
    dispatcher.return_value.cancel_thread = AsyncMock()
    revoked = AsyncMock(return_value=None)

    monkeypatch.setattr(ownership, "ticket_db", ticket_db)
    monkeypatch.setattr(ownership, "line_db", line_db)
    monkeypatch.setattr(ownership, "daemon_db", daemon_db)
    monkeypatch.setattr(ownership, "enqueue_effect", enqueue_effect)
    monkeypatch.setattr(ownership, "revoked_mandate_reason", revoked)
    monkeypatch.setattr(
        "src.server.services.projects.dispatch.ProjectDispatcher", dispatcher
    )
    return SimpleNamespace(
        ticket_db=ticket_db,
        line_db=line_db,
        stop_request=stop_request,
        release=release,
        force_ack=force_ack,
        daemon_db=daemon_db,
        enqueue_effect=enqueue_effect,
        dispatcher=dispatcher,
        revoked=revoked,
        events=events,
    )


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_uses_durable_request_and_records_the_event(fakes):
    row = _ticket(status="running", worker_thread_id="thread-1", worker_run_id="run-1")
    fakes.ticket_db.get_ticket.return_value = row
    fakes.ticket_db.update_ticket.return_value = _ticket(
        status="cancelled",
        ended_at=NOW,
        worker_thread_id="thread-1",
        worker_run_id="run-1",
    )

    updated = await ownership.stop_ticket(user_id="test-user-123", ticket_id=TICKET_ID)

    assert updated["ticket"]["status"] == "cancelled"
    assert updated["stop"]["verification"] == "not_applicable"
    # The stopped row is inserted in the stop transaction, not a second write.
    assert fakes.events == []
    fakes.stop_request.assert_awaited_once_with(
        user_id="test-user-123", ticket_id=TICKET_ID
    )


@pytest.mark.asyncio
async def test_stop_refuses_a_terminal_ticket(fakes):
    fakes.ticket_db.get_ticket.return_value = _ticket(status="completed")

    with pytest.raises(ownership.TicketOpError) as err:
        await ownership.stop_ticket(user_id="test-user-123", ticket_id=TICKET_ID)

    assert err.value.status_code == 409
    fakes.ticket_db.update_ticket.assert_not_awaited()
    assert fakes.events == []


@pytest.mark.asyncio
async def test_stop_maps_a_finished_stop_request_to_a_conflict(fakes):
    from src.server.services.autonomy import stop as stop_service

    fakes.ticket_db.get_ticket.return_value = _ticket(status="queued")
    fakes.stop_request.side_effect = stop_service.StopAlreadyEnded(TICKET_ID)

    with pytest.raises(ownership.TicketOpError) as err:
        await ownership.stop_ticket(user_id="test-user-123", ticket_id=TICKET_ID)

    assert err.value.status_code == 409


@pytest.mark.asyncio
async def test_stop_of_another_users_ticket_is_a_404(fakes):
    fakes.ticket_db.get_ticket.return_value = _ticket(user_id="someone-else")

    with pytest.raises(ownership.TicketOpError) as err:
        await ownership.stop_ticket(user_id="test-user-123", ticket_id=TICKET_ID)

    assert err.value.status_code == 404


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_requeues_records_the_event_and_reenqueues_launch(fakes):
    row = _ticket(status="cancelled", ended_at=NOW)
    fakes.ticket_db.get_ticket.return_value = row
    fakes.ticket_db.update_ticket.return_value = _ticket(status="queued", ended_at=None)

    updated = await ownership.resume_ticket(user_id="test-user-123", ticket_id=TICKET_ID)

    assert updated["status"] == "queued"
    kwargs = fakes.ticket_db.update_ticket.call_args.kwargs
    assert kwargs["expected_status"] == "cancelled"
    assert kwargs["status"] == "queued"
    assert kwargs["ended_at"] is None
    event = fakes.ticket_db.update_ticket.call_args.kwargs["event"]
    assert event["action"] == "resumed"
    assert event["to_owner"] == "cloud"
    fakes.enqueue_effect.assert_awaited_once()
    effect = fakes.enqueue_effect.call_args.kwargs
    assert effect["payload"] == {"ticket_id": TICKET_ID}
    assert TICKET_ID in effect["idempotency_key"]


@pytest.mark.asyncio
async def test_resume_refuses_a_ticket_that_was_not_stopped(fakes):
    fakes.ticket_db.get_ticket.return_value = _ticket(status="failed")

    with pytest.raises(ownership.TicketOpError) as err:
        await ownership.resume_ticket(user_id="test-user-123", ticket_id=TICKET_ID)

    assert err.value.status_code == 409
    fakes.ticket_db.update_ticket.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_with_a_revoked_mandate_stamps_and_refuses(fakes):
    fakes.ticket_db.get_ticket.return_value = _ticket(status="cancelled")
    fakes.revoked.return_value = "the Source is no longer enabled"

    with pytest.raises(ownership.TicketOpError) as err:
        await ownership.resume_ticket(user_id="test-user-123", ticket_id=TICKET_ID)

    assert err.value.status_code == 409
    assert "Mandate revoked" in err.value.detail
    stamp = fakes.ticket_db.update_ticket.call_args.kwargs
    assert stamp["expected_status"] == "cancelled"
    assert stamp["expected_revision"] == 0
    assert stamp["attention"]["question"].startswith("Mandate revoked")
    assert stamp["event"]["action"] == "mandate_revoked"
    assert stamp["event"]["detail"]["from_status"] == "cancelled"
    fakes.enqueue_effect.assert_not_awaited()


# ---------------------------------------------------------------------------
# reassign
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reassign_to_a_daemon_swaps_owner_and_audits_the_transition(fakes):
    row = _ticket(status="queued", worker_provider="kimi")
    fakes.ticket_db.get_ticket.return_value = row
    fakes.daemon_db.get_daemon.return_value = {
        "daemon_id": "d1", "name": "m5", "provider": "codex",
    }
    fakes.line_db.reassign_ticket.return_value = {
        "status": "queued", "worker_daemon_id": "d1",
    }

    updated = await ownership.reassign_ticket(
        user_id="test-user-123", ticket_id=TICKET_ID, daemon_id="d1"
    )

    assert updated["worker_daemon_id"] == "d1"
    kwargs = fakes.line_db.reassign_ticket.call_args.kwargs
    event = kwargs.pop("event")
    assert kwargs == {
        "user_id": "test-user-123",
        "from_daemon_id": None,
        "worker_daemon_id": "d1",
        "worker_provider": "codex",
        "worker_model": "",
        "expected_revision": 0,
        "expected_status": "queued",
        "expected_lease_id": None,
        "restart_cancelled": False,
    }
    assert event["action"] == "reassigned"
    assert event["from_owner"] == "cloud"
    assert event["to_owner"] == "d1"
    assert event["detail"]["daemon_name"] == "m5"


@pytest.mark.asyncio
async def test_reassign_to_cloud_keeps_the_ticket_provider(fakes):
    row = _ticket(status="blocked", worker_daemon_id="d1", worker_provider="codex")
    fakes.ticket_db.get_ticket.return_value = row
    fakes.stop_request.return_value = {
        "outcome": "ok",
        "ticket": _ticket(
            status="blocked", worker_daemon_id="d1", worker_provider="codex",
            revision=0,
        ),
        "stop": {"mode": "daemon", "verification": "verified", "daemon_id": "d1"},
    }
    fakes.line_db.reassign_ticket.return_value = {
        "status": "blocked", "worker_daemon_id": None,
    }

    await ownership.reassign_ticket(
        user_id="test-user-123", ticket_id=TICKET_ID, daemon_id=None
    )

    kwargs = fakes.line_db.reassign_ticket.call_args.kwargs
    assert kwargs["user_id"] == "test-user-123"
    assert kwargs["from_daemon_id"] == "d1"
    assert kwargs["worker_daemon_id"] is None
    assert kwargs["worker_provider"] == "codex"


@pytest.mark.asyncio
async def test_reassign_to_the_same_owner_is_an_idempotent_no_op(fakes):
    row = _ticket(status="queued", worker_daemon_id="d1")
    fakes.ticket_db.get_ticket.return_value = row

    updated = await ownership.reassign_ticket(
        user_id="test-user-123", ticket_id=TICKET_ID, daemon_id="d1"
    )

    assert updated is row
    fakes.line_db.reassign_ticket.assert_not_awaited()
    assert fakes.events == []


@pytest.mark.asyncio
async def test_reassign_to_a_missing_daemon_is_a_404(fakes):
    fakes.ticket_db.get_ticket.return_value = _ticket(status="queued")
    fakes.daemon_db.get_daemon.return_value = None

    with pytest.raises(ownership.TicketOpError) as err:
        await ownership.reassign_ticket(
            user_id="test-user-123", ticket_id=TICKET_ID, daemon_id="nope"
        )

    assert err.value.status_code == 404
    fakes.line_db.reassign_ticket.assert_not_awaited()


@pytest.mark.asyncio
async def test_reassign_cancels_a_running_cloud_thread(fakes):
    row = _ticket(status="running", worker_thread_id="thread-1")
    fakes.ticket_db.get_ticket.return_value = row
    fakes.daemon_db.get_daemon.return_value = {
        "daemon_id": "d1", "name": "m5", "provider": "codex",
    }
    fakes.line_db.reassign_ticket.return_value = {
        "status": "queued", "worker_daemon_id": "d1",
    }

    await ownership.reassign_ticket(
        user_id="test-user-123", ticket_id=TICKET_ID, daemon_id="d1"
    )

    fakes.dispatcher.return_value.cancel_thread.assert_awaited_once()


@pytest.mark.asyncio
async def test_reassign_waits_for_daemon_stop_ack_before_changing_owner(fakes):
    row = _ticket(status="running", worker_daemon_id="d1", revision=7)
    fakes.ticket_db.get_ticket.return_value = row
    fakes.release["live_lease"] = True
    fakes.stop_request.return_value = {
        "outcome": "ok",
        "ticket": _ticket(
            status="cancelled", worker_daemon_id="d1", revision=8,
            worker_lease_id="lease-1",
        ),
        "stop": {
            "mode": "daemon", "verification": "pending", "daemon_id": "d1"
        },
    }

    with pytest.raises(ownership.TicketOpError, match="ticket was stopped"):
        await ownership.reassign_ticket(
            user_id="test-user-123", ticket_id=TICKET_ID, daemon_id=None
        )

    fakes.stop_request.assert_awaited_once_with(
        user_id="test-user-123",
        ticket_id=TICKET_ID,
        expected={
            "revision": 7,
            "worker_daemon_id": "d1",
            "worker_lease_id": None,
        },
    )
    fakes.line_db.reassign_ticket.assert_not_awaited()
    fakes.enqueue_effect.assert_not_awaited()


@pytest.mark.asyncio
async def test_verified_daemon_reassignment_queues_cloud_with_new_attempt_key(fakes):
    fakes.ticket_db.get_ticket.return_value = _ticket(
        status="cancelled", worker_daemon_id="d1", worker_provider="codex",
        attempts=4, revision=9,
    )
    fakes.stop_request.return_value = {
        "outcome": "ok",
        "ticket": _ticket(
            status="cancelled", worker_daemon_id="d1", worker_provider="codex",
            attempts=4, revision=9, worker_lease_id=None,
        ),
        "stop": {
            "mode": "daemon", "verification": "verified", "daemon_id": "d1"
        },
    }
    fakes.line_db.reassign_ticket.return_value = _ticket(
        status="queued", worker_daemon_id=None, worker_provider="codex",
        attempts=4, revision=10,
    )

    updated = await ownership.reassign_ticket(
        user_id="test-user-123", ticket_id=TICKET_ID, daemon_id=None
    )

    assert updated["status"] == "queued"
    kwargs = fakes.line_db.reassign_ticket.call_args.kwargs
    assert kwargs["expected_revision"] == 9
    assert kwargs["expected_status"] == "cancelled"
    assert kwargs["expected_lease_id"] is None
    effect = fakes.enqueue_effect.call_args.kwargs
    assert (
        effect["idempotency_key"]
        == f"autonomy:launch:{TICKET_ID}:attempt:5:revision:10"
    )
    assert effect["payload"] == {"ticket_id": TICKET_ID}


@pytest.mark.asyncio
async def test_never_started_daemon_ticket_can_be_reassigned_after_verified_stop(fakes):
    fakes.ticket_db.get_ticket.return_value = _ticket(
        status="queued", worker_daemon_id="d1", attempts=0, revision=2
    )
    fakes.stop_request.return_value = {
        "outcome": "ok",
        "ticket": _ticket(
            status="cancelled", worker_daemon_id="d1", attempts=0, revision=3
        ),
        "stop": {
            "mode": "not_started", "verification": "verified", "daemon_id": "d1"
        },
    }
    fakes.line_db.reassign_ticket.return_value = _ticket(
        status="queued", worker_daemon_id=None, attempts=0, revision=4
    )

    await ownership.reassign_ticket(
        user_id="test-user-123", ticket_id=TICKET_ID, daemon_id=None
    )

    assert fakes.line_db.reassign_ticket.await_count == 1
    assert fakes.enqueue_effect.await_args.kwargs["idempotency_key"].endswith(
        "attempt:1:revision:4"
    )


@pytest.mark.asyncio
async def test_recovered_queued_daemon_ticket_is_not_reassigned_without_stop_proof(fakes):
    fakes.ticket_db.get_ticket.return_value = _ticket(
        status="queued", worker_daemon_id="d1", attempts=2, revision=5,
        worker_lease_id=None,
    )
    fakes.release["may_have_launched"] = True
    fakes.release["stop"] = {
        "mode": "daemon", "verification": "unverified", "daemon_id": "d1"
    }

    with pytest.raises(ownership.TicketOpError, match="not changed"):
        await ownership.reassign_ticket(
            user_id="test-user-123", ticket_id=TICKET_ID, daemon_id=None
        )

    fakes.stop_request.assert_not_awaited()
    fakes.line_db.reassign_ticket.assert_not_awaited()
    fakes.enqueue_effect.assert_not_awaited()


@pytest.mark.asyncio
async def test_reassign_of_a_cancelled_daemon_ticket_checks_the_mandate(fakes):
    fakes.ticket_db.get_ticket.return_value = _ticket(
        status="cancelled", worker_daemon_id="d1", attempts=2, revision=4
    )
    fakes.release["stop"] = {
        "mode": "daemon", "verification": "verified", "daemon_id": "d1"
    }
    fakes.stop_request.return_value = {
        "outcome": "ok",
        "ticket": _ticket(
            status="cancelled", worker_daemon_id="d1", attempts=2, revision=4
        ),
        "stop": {"mode": "daemon", "verification": "verified", "daemon_id": "d1"},
    }
    fakes.revoked.return_value = "the Source is no longer enabled"

    with pytest.raises(ownership.TicketOpError, match="Mandate revoked"):
        await ownership.reassign_ticket(
            user_id="test-user-123", ticket_id=TICKET_ID, daemon_id=None
        )

    fakes.line_db.reassign_ticket.assert_not_awaited()
    fakes.enqueue_effect.assert_not_awaited()
    stamp = fakes.ticket_db.update_ticket.call_args.kwargs
    assert stamp["expected_revision"] == 4
    assert stamp["attention"]["question"].startswith("Mandate revoked")
    assert stamp["event"]["action"] == "mandate_revoked"


@pytest.mark.asyncio
async def test_reassign_of_a_cancelled_cloud_ticket_checks_the_mandate(fakes):
    fakes.ticket_db.get_ticket.return_value = _ticket(status="cancelled", revision=2)
    fakes.daemon_db.get_daemon.return_value = {
        "daemon_id": "d1", "name": "m5", "provider": "codex"
    }
    fakes.revoked.return_value = "the Source is no longer enabled"

    with pytest.raises(ownership.TicketOpError, match="Mandate revoked"):
        await ownership.reassign_ticket(
            user_id="test-user-123", ticket_id=TICKET_ID, daemon_id="d1"
        )

    fakes.line_db.reassign_ticket.assert_not_awaited()
    stamp = fakes.ticket_db.update_ticket.call_args.kwargs
    assert stamp["expected_revision"] == 2
    assert stamp["attention"]["question"].startswith("Mandate revoked")
    assert stamp["event"]["action"] == "mandate_revoked"


@pytest.mark.asyncio
async def test_reassign_restarts_a_cancelled_ticket_when_the_mandate_holds(fakes):
    fakes.ticket_db.get_ticket.return_value = _ticket(
        status="cancelled", worker_provider="kimi", revision=2
    )
    fakes.daemon_db.get_daemon.return_value = {
        "daemon_id": "d1", "name": "m5", "provider": "codex"
    }
    fakes.line_db.reassign_ticket.return_value = _ticket(
        status="queued", worker_daemon_id="d1", revision=3
    )

    updated = await ownership.reassign_ticket(
        user_id="test-user-123", ticket_id=TICKET_ID, daemon_id="d1"
    )

    assert updated["status"] == "queued"
    assert fakes.line_db.reassign_ticket.call_args.kwargs["restart_cancelled"] is True
    fakes.revoked.assert_awaited()


@pytest.mark.asyncio
async def test_force_reassign_acknowledges_an_unverified_stop_then_moves(fakes):
    fakes.ticket_db.get_ticket.return_value = _ticket(
        status="cancelled", worker_daemon_id="d1", attempts=3, revision=6
    )
    fakes.release["may_have_launched"] = True
    fakes.release["stop"] = {
        "mode": "daemon", "verification": "unverified", "daemon_id": "d1"
    }
    fakes.force_ack.return_value = {
        "outcome": "ok",
        "ticket": _ticket(
            status="cancelled", worker_daemon_id="d1", attempts=3, revision=7
        ),
        "stop": {"mode": "daemon", "verification": "verified", "daemon_id": "d1"},
    }
    fakes.stop_request.return_value = {
        "outcome": "ok",
        "ticket": _ticket(
            status="cancelled", worker_daemon_id="d1", attempts=3, revision=7
        ),
        "stop": {"mode": "daemon", "verification": "verified", "daemon_id": "d1"},
    }
    fakes.line_db.reassign_ticket.return_value = _ticket(
        status="queued", worker_daemon_id=None, attempts=3, revision=8
    )

    updated = await ownership.reassign_ticket(
        user_id="test-user-123", ticket_id=TICKET_ID, daemon_id=None, force=True
    )

    assert updated["status"] == "queued"
    fakes.force_ack.assert_awaited_once()
    # The force acknowledgement bumped the revision to 7; the refreshed stop
    # must fence on that row rather than the pre-force revision 6.
    assert fakes.stop_request.call_args.kwargs["expected"]["revision"] == 7
    assert fakes.line_db.reassign_ticket.call_args.kwargs["restart_cancelled"] is True


@pytest.mark.asyncio
async def test_cloud_reassignment_generation_changes_after_daemon_roundtrip(fakes):
    fakes.ticket_db.get_ticket.side_effect = [
        _ticket(status="queued", worker_daemon_id="d1", attempts=0, revision=4),
        _ticket(status="queued", worker_daemon_id=None, attempts=0, revision=6),
        _ticket(status="queued", worker_daemon_id="d2", attempts=0, revision=7),
    ]
    fakes.daemon_db.get_daemon.return_value = {
        "daemon_id": "d2", "name": "m5", "provider": "codex"
    }
    fakes.stop_request.side_effect = [
        {
            "outcome": "ok",
            "ticket": _ticket(status="cancelled", worker_daemon_id="d1", attempts=0, revision=5),
            "stop": {"mode": "not_started", "verification": "verified", "daemon_id": "d1"},
        },
        {
            "outcome": "ok",
            "ticket": _ticket(status="cancelled", worker_daemon_id="d2", attempts=0, revision=8),
            "stop": {"mode": "not_started", "verification": "verified", "daemon_id": "d2"},
        },
    ]
    fakes.line_db.reassign_ticket.side_effect = [
        _ticket(status="queued", worker_daemon_id=None, attempts=0, revision=6),
        _ticket(status="queued", worker_daemon_id="d2", attempts=0, revision=7),
        _ticket(status="queued", worker_daemon_id=None, attempts=0, revision=9),
    ]

    await ownership.reassign_ticket(
        user_id="test-user-123", ticket_id=TICKET_ID, daemon_id=None
    )
    await ownership.reassign_ticket(
        user_id="test-user-123", ticket_id=TICKET_ID, daemon_id="d2"
    )
    await ownership.reassign_ticket(
        user_id="test-user-123", ticket_id=TICKET_ID, daemon_id=None
    )

    keys = [call.kwargs["idempotency_key"] for call in fakes.enqueue_effect.await_args_list]
    assert keys == [
        f"autonomy:launch:{TICKET_ID}:attempt:1:revision:6",
        f"autonomy:launch:{TICKET_ID}:attempt:1:revision:9",
    ]


@pytest.mark.asyncio
async def test_archive_does_not_require_a_verified_stop(fakes):
    fakes.ticket_db.get_ticket.return_value = _ticket(
        status="blocked", worker_daemon_id="d1", attempts=2, revision=4
    )
    fakes.release["may_have_launched"] = True
    fakes.release["stop"] = {
        "mode": "daemon", "verification": "unverified", "daemon_id": "d1"
    }
    fakes.ticket_db.update_ticket.return_value = _ticket(status="archived")

    updated = await ownership.archive_ticket(
        user_id="test-user-123", ticket_id=TICKET_ID
    )

    assert updated["status"] == "archived"
    fakes.stop_request.assert_not_awaited()
    kwargs = fakes.ticket_db.update_ticket.call_args.kwargs
    assert kwargs["expected_status"] == "blocked"
    assert kwargs["status"] == "archived"
    assert kwargs["event"]["action"] == "archived"


@pytest.mark.asyncio
async def test_resume_waits_for_daemon_stop_ack(fakes):
    fakes.ticket_db.get_ticket.return_value = _ticket(
        status="cancelled", worker_daemon_id="d1"
    )
    fakes.release["stop"] = {
        "mode": "daemon", "verification": "pending", "daemon_id": "d1"
    }

    with pytest.raises(ownership.TicketOpError, match="pending"):
        await ownership.resume_ticket(user_id="test-user-123", ticket_id=TICKET_ID)

    fakes.stop_request.assert_not_awaited()
    fakes.ticket_db.update_ticket.assert_not_awaited()
    fakes.enqueue_effect.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_uses_revision_from_verified_stop_snapshot(fakes):
    fakes.ticket_db.get_ticket.return_value = _ticket(
        status="cancelled", worker_daemon_id="d1", revision=3
    )
    fakes.stop_request.return_value = {
        "outcome": "ok",
        "ticket": _ticket(status="cancelled", worker_daemon_id="d1", revision=8),
        "stop": {"mode": "daemon", "verification": "verified", "daemon_id": "d1"},
    }
    fakes.ticket_db.update_ticket.return_value = None

    with pytest.raises(ownership.TicketOpError, match="Ticket changed"):
        await ownership.resume_ticket(user_id="test-user-123", ticket_id=TICKET_ID)

    assert fakes.ticket_db.update_ticket.call_args.kwargs["expected_revision"] == 8
    fakes.enqueue_effect.assert_not_awaited()


@pytest.mark.asyncio
async def test_closed_ticket_cannot_be_resumed_or_reassigned(fakes):
    fakes.ticket_db.get_ticket.return_value = _ticket(
        status="cancelled", worker_daemon_id="d1", closed_at=NOW
    )

    with pytest.raises(ownership.TicketOpError, match="closed"):
        await ownership.resume_ticket(user_id="test-user-123", ticket_id=TICKET_ID)
    with pytest.raises(ownership.TicketOpError, match="closed"):
        await ownership.reassign_ticket(
            user_id="test-user-123", ticket_id=TICKET_ID, daemon_id=None
        )

    fakes.stop_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_archive_of_a_live_daemon_stops_then_archives(fakes):
    fakes.ticket_db.get_ticket.return_value = _ticket(
        status="running", worker_daemon_id="d1", revision=2, worker_lease_id="lease-1"
    )
    fakes.release["live_lease"] = True
    fakes.stop_request.return_value = {
        "outcome": "ok",
        "ticket": _ticket(
            status="cancelled", worker_daemon_id="d1", revision=3, ended_at=NOW
        ),
        "stop": {"mode": "daemon", "verification": "pending", "daemon_id": "d1"},
    }
    fakes.ticket_db.update_ticket.return_value = _ticket(
        status="archived", worker_daemon_id="d1"
    )

    updated = await ownership.archive_ticket(
        user_id="test-user-123", ticket_id=TICKET_ID
    )

    assert updated["status"] == "archived"
    fakes.stop_request.assert_awaited_once()
    kwargs = fakes.ticket_db.update_ticket.call_args.kwargs
    assert kwargs["expected_status"] == "cancelled"
    assert kwargs["event"]["action"] == "archived"
    assert kwargs["event"]["detail"]["from_status"] == "cancelled"


@pytest.mark.asyncio
async def test_archive_fences_the_read_revision_without_a_verified_stop(fakes):
    fakes.ticket_db.get_ticket.return_value = _ticket(
        status="cancelled", worker_daemon_id="d1", revision=4
    )
    fakes.release["stop"] = {
        "mode": "daemon", "verification": "unverified", "daemon_id": "d1"
    }
    fakes.ticket_db.update_ticket.return_value = None

    with pytest.raises(ownership.TicketOpError, match="Ticket changed"):
        await ownership.archive_ticket(user_id="test-user-123", ticket_id=TICKET_ID)

    fakes.stop_request.assert_not_awaited()
    assert fakes.ticket_db.update_ticket.call_args.kwargs["expected_revision"] == 4


# ---------------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_settles_the_ticket_and_audits_it(fakes):
    row = _ticket(status="completed", ended_at=NOW)
    fakes.ticket_db.get_ticket.return_value = row
    fakes.ticket_db.update_ticket.return_value = _ticket(status="archived")

    updated = await ownership.archive_ticket(
        user_id="test-user-123", ticket_id=TICKET_ID
    )

    assert updated["status"] == "archived"
    kwargs = fakes.ticket_db.update_ticket.call_args.kwargs
    assert kwargs["status"] == "archived"
    assert kwargs["expected_status"] == "completed"
    assert kwargs["ended_at"] is NOW
    event = fakes.ticket_db.update_ticket.call_args.kwargs["event"]
    assert event["action"] == "archived"
    assert event["detail"]["from_status"] == "completed"


@pytest.mark.asyncio
async def test_archive_a_running_ticket_cancels_its_thread(fakes):
    row = _ticket(status="running", worker_thread_id="thread-9")
    fakes.ticket_db.get_ticket.return_value = row
    fakes.ticket_db.update_ticket.return_value = _ticket(status="archived")

    await ownership.archive_ticket(user_id="test-user-123", ticket_id=TICKET_ID)

    fakes.dispatcher.return_value.cancel_thread.assert_awaited_once()


@pytest.mark.asyncio
async def test_double_archive_is_refused(fakes):
    fakes.ticket_db.get_ticket.return_value = _ticket(status="archived")

    with pytest.raises(ownership.TicketOpError) as err:
        await ownership.archive_ticket(user_id="test-user-123", ticket_id=TICKET_ID)

    assert err.value.status_code == 409


# ---------------------------------------------------------------------------
# line assignment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_ticket_line_validates_the_line_and_recovers_the_row(fakes):
    row = _ticket()
    fakes.ticket_db.get_ticket.side_effect = [row, _ticket()]
    fakes.line_db.get_line.return_value = {"line_id": "line-1", "name": "ob-tickets"}

    updated = await ownership.assign_ticket_line(
        user_id="test-user-123", ticket_id=TICKET_ID, line_id="line-1"
    )

    assert updated["ticket_id"] == TICKET_ID
    fakes.line_db.set_ticket_line.assert_awaited_once_with(TICKET_ID, "line-1")
    assert fakes.ticket_db.get_ticket.await_count == 2


@pytest.mark.asyncio
async def test_assign_ticket_line_to_a_missing_line_is_a_404(fakes):
    fakes.ticket_db.get_ticket.return_value = _ticket()
    fakes.line_db.get_line.return_value = None

    with pytest.raises(ownership.TicketOpError) as err:
        await ownership.assign_ticket_line(
            user_id="test-user-123", ticket_id=TICKET_ID, line_id="nope"
        )

    assert err.value.status_code == 404
    fakes.line_db.set_ticket_line.assert_not_awaited()


@pytest.mark.asyncio
async def test_assign_ticket_line_none_clears_the_grouping(fakes):
    row = _ticket()
    fakes.ticket_db.get_ticket.side_effect = [row, row]

    await ownership.assign_ticket_line(
        user_id="test-user-123", ticket_id=TICKET_ID, line_id=None
    )

    fakes.line_db.get_line.assert_not_awaited()
    fakes.line_db.set_ticket_line.assert_awaited_once_with(TICKET_ID, None)


# ---------------------------------------------------------------------------
# stale verified receipts
# ---------------------------------------------------------------------------

OLD_LEASE = "11111111-1111-4111-8111-111111111111"
LIVE_LEASE = "22222222-2222-4222-8222-222222222222"


def _stale_verified_stop(**overrides):
    now = datetime.now(timezone.utc)
    stop = {
        "mode": "daemon",
        "verification": "verified",
        "daemon_id": "d1",
        "lease_id": OLD_LEASE,
        "requested_at": now - timedelta(hours=1),
    }
    stop.update(overrides)
    return stop


def test_stop_verified_rejects_a_receipt_for_another_lease():
    now = datetime.now(timezone.utc)
    stop = _stale_verified_stop()

    assert ownership._stop_verified(
        stop, "d1", lease_id=LIVE_LEASE, heartbeat_at=now, live=True
    ) is False
    assert ownership._stop_verified(
        stop, "d1", lease_id=LIVE_LEASE, heartbeat_at=now, live=False
    ) is False
    # No lease left on the ticket: this receipt is the attempt that was cleared.
    assert ownership._stop_verified(stop, "d1", lease_id=None, live=False) is True


def test_stop_verified_rejects_a_heartbeat_newer_than_the_request():
    now = datetime.now(timezone.utc)
    stop = _stale_verified_stop(lease_id=LIVE_LEASE, requested_at=now - timedelta(minutes=5))

    assert ownership._stop_verified(
        stop, "d1", lease_id=LIVE_LEASE, heartbeat_at=now, live=False
    ) is False
    assert ownership._stop_verified(
        stop,
        "d1",
        lease_id=LIVE_LEASE,
        heartbeat_at=now - timedelta(minutes=10),
        live=False,
    ) is True


@pytest.mark.asyncio
async def test_reassign_does_not_clear_a_live_lease_on_an_older_verified_stop(fakes):
    """Daemon reclaimed after a verified stop. The old receipt is not this run."""
    now = datetime.now(timezone.utc)
    # get_ticket does not select lease columns. The snapshot does.
    fakes.ticket_db.get_ticket.return_value = _ticket(
        status="running", worker_daemon_id="d1", revision=7, worker_lease_id=None
    )
    fakes.release["live_lease"] = True
    fakes.release["may_have_launched"] = True
    fakes.release["stop"] = _stale_verified_stop()
    fakes.release["ticket"] = {
        "worker_lease_id": LIVE_LEASE,
        "worker_lease_heartbeat_at": now,
    }
    fakes.stop_request.return_value = {
        "outcome": "ok",
        "ticket": _ticket(
            status="cancelled",
            worker_daemon_id="d1",
            revision=8,
            worker_lease_id=LIVE_LEASE,
        ),
        "stop": {
            "mode": "daemon",
            "verification": "pending",
            "daemon_id": "d1",
            "lease_id": LIVE_LEASE,
        },
    }

    with pytest.raises(ownership.TicketOpError, match="ticket was stopped"):
        await ownership.reassign_ticket(
            user_id="test-user-123", ticket_id=TICKET_ID, daemon_id=None
        )

    fakes.stop_request.assert_awaited_once_with(
        user_id="test-user-123",
        ticket_id=TICKET_ID,
        expected={
            "revision": 7,
            "worker_daemon_id": "d1",
            "worker_lease_id": LIVE_LEASE,
        },
    )
    fakes.line_db.reassign_ticket.assert_not_awaited()
    fakes.enqueue_effect.assert_not_awaited()


@pytest.mark.asyncio
async def test_reassign_requests_a_fresh_stop_when_the_expired_lease_differs(fakes):
    now = datetime.now(timezone.utc)
    fakes.ticket_db.get_ticket.return_value = _ticket(
        status="running", worker_daemon_id="d1", revision=7, attempts=2
    )
    fakes.release["live_lease"] = False
    fakes.release["may_have_launched"] = True
    fakes.release["stop"] = _stale_verified_stop()
    fakes.release["ticket"] = {
        "worker_lease_id": LIVE_LEASE,
        "worker_lease_heartbeat_at": now,
    }
    fakes.stop_request.return_value = {
        "outcome": "ok",
        "ticket": _ticket(
            status="cancelled",
            worker_daemon_id="d1",
            revision=8,
            worker_lease_id=LIVE_LEASE,
        ),
        "stop": {
            "mode": "daemon",
            "verification": "pending",
            "daemon_id": "d1",
            "lease_id": LIVE_LEASE,
        },
    }

    with pytest.raises(ownership.TicketOpError, match="ticket was stopped"):
        await ownership.reassign_ticket(
            user_id="test-user-123", ticket_id=TICKET_ID, daemon_id=None
        )

    fakes.stop_request.assert_awaited_once()
    fakes.line_db.reassign_ticket.assert_not_awaited()


@pytest.mark.asyncio
async def test_archive_of_a_live_lease_keeps_it_for_a_fresh_stop(fakes):
    now = datetime.now(timezone.utc)
    fakes.ticket_db.get_ticket.return_value = _ticket(
        status="running",
        worker_daemon_id="d1",
        revision=4,
        worker_lease_id=None,
    )
    fakes.release["live_lease"] = True
    fakes.release["stop"] = _stale_verified_stop()
    fakes.release["ticket"] = {
        "worker_lease_id": LIVE_LEASE,
        "worker_lease_heartbeat_at": now,
    }
    fresh = _ticket(
        status="cancelled",
        worker_daemon_id="d1",
        revision=5,
        worker_lease_id=LIVE_LEASE,
        ended_at=NOW,
    )
    fakes.stop_request.return_value = {
        "outcome": "ok",
        "ticket": fresh,
        "stop": {
            "mode": "daemon",
            "verification": "pending",
            "daemon_id": "d1",
            "lease_id": LIVE_LEASE,
        },
    }
    fakes.ticket_db.update_ticket.return_value = _ticket(status="archived")

    updated = await ownership.archive_ticket(
        user_id="test-user-123", ticket_id=TICKET_ID
    )

    assert updated["status"] == "archived"
    fakes.stop_request.assert_awaited_once()
    fakes.line_db.reassign_ticket.assert_not_awaited()
    kwargs = fakes.ticket_db.update_ticket.call_args.kwargs
    assert kwargs["expected_status"] == "cancelled"
    assert kwargs["status"] == "archived"
    assert "worker_lease_id" not in kwargs
    assert kwargs["event"]["action"] == "archived"


@pytest.mark.asyncio
async def test_revoked_mandate_does_not_stamp_a_moved_revision(fakes):
    fakes.ticket_db.get_ticket.return_value = _ticket(status="cancelled", revision=4)
    fakes.revoked.return_value = "the Source is no longer enabled"
    fakes.ticket_db.update_ticket.return_value = None

    with pytest.raises(ownership.TicketOpError, match="Ticket changed"):
        await ownership.resume_ticket(user_id="test-user-123", ticket_id=TICKET_ID)

    stamp = fakes.ticket_db.update_ticket.call_args.kwargs
    assert stamp["expected_revision"] == 4
    assert stamp["event"]["action"] == "mandate_revoked"
    fakes.enqueue_effect.assert_not_awaited()

@pytest.mark.asyncio
async def test_reassign_aborts_when_the_running_cloud_worker_cannot_stop(fakes):
    """A new owner must not be published while the old cloud Worker may run."""
    row = _ticket(status="running", worker_thread_id="thread-1", revision=5)
    fakes.ticket_db.get_ticket.return_value = row
    fakes.daemon_db.get_daemon.return_value = {
        "daemon_id": "d1", "name": "m5", "provider": "codex",
    }
    fakes.dispatcher.return_value.cancel_thread.return_value = False

    with pytest.raises(ownership.TicketOpError) as err:
        await ownership.reassign_ticket(
            user_id="test-user-123", ticket_id=TICKET_ID, daemon_id="d1"
        )

    assert err.value.status_code == 409
    fakes.line_db.reassign_ticket.assert_not_awaited()
    fakes.enqueue_effect.assert_not_awaited()


@pytest.mark.asyncio
async def test_archive_aborts_when_the_running_cloud_worker_cannot_stop(fakes):
    row = _ticket(status="running", worker_thread_id="thread-9")
    fakes.ticket_db.get_ticket.return_value = row
    fakes.dispatcher.return_value.cancel_thread.return_value = False

    with pytest.raises(ownership.TicketOpError) as err:
        await ownership.archive_ticket(user_id="test-user-123", ticket_id=TICKET_ID)

    assert err.value.status_code == 409
    fakes.ticket_db.update_ticket.assert_not_awaited()


@pytest.mark.asyncio
async def test_cloud_resume_is_fenced_by_revision(fakes):
    """Closing a cancelled cloud Ticket keeps its status, so the resume must
    also fence the revision it read or a concurrent close is overwritten."""
    row = _ticket(status="cancelled", ended_at=NOW, revision=11)
    fakes.ticket_db.get_ticket.return_value = row
    fakes.ticket_db.update_ticket.return_value = None

    with pytest.raises(ownership.TicketOpError) as err:
        await ownership.resume_ticket(user_id="test-user-123", ticket_id=TICKET_ID)

    assert err.value.status_code == 409
    kwargs = fakes.ticket_db.update_ticket.call_args.kwargs
    assert kwargs["expected_revision"] == 11
    fakes.enqueue_effect.assert_not_awaited()


@pytest.mark.asyncio
async def test_cloud_archive_is_fenced_by_the_snapshot_revision(fakes):
    """While the cloud Worker cancellation runs, a reassignment and daemon claim
    can advance the revision; the stale archive must match nothing."""
    row = _ticket(status="running", worker_thread_id="thread-7", revision=6)
    fakes.ticket_db.get_ticket.return_value = row
    fakes.ticket_db.update_ticket.return_value = None

    with pytest.raises(ownership.TicketOpError) as err:
        await ownership.archive_ticket(user_id="test-user-123", ticket_id=TICKET_ID)

    assert err.value.status_code == 409
    kwargs = fakes.ticket_db.update_ticket.call_args.kwargs
    assert kwargs["expected_revision"] == 6
    assert kwargs["status"] == "archived"


@pytest.mark.asyncio
async def test_archive_daemon_stop_is_fenced_to_the_original_snapshot(fakes):
    """While the archive prepares, a reassignment can give the Ticket a new
    owner; the internal stop must not cancel and archive that newer run."""
    row = _ticket(
        status="running",
        worker_daemon_id="d1",
        worker_lease_id="lease-1",
        revision=6,
    )
    fakes.ticket_db.get_ticket.return_value = row
    fakes.stop_request.return_value = {
        "outcome": "changed",
        "ticket": {**row, "worker_daemon_id": "d2", "revision": 9},
        "stop": None,
    }

    with pytest.raises(ownership.TicketOpError) as err:
        await ownership.archive_ticket(user_id="test-user-123", ticket_id=TICKET_ID)

    assert err.value.status_code == 409
    fakes.ticket_db.update_ticket.assert_not_awaited()
    expected = fakes.stop_request.call_args.kwargs["expected"]
    assert expected == {
        "revision": 6,
        "worker_daemon_id": "d1",
        "worker_lease_id": "lease-1",
    }


@pytest.mark.asyncio
async def test_force_resume_rebuilds_the_stop_fence_after_acknowledgement(fakes):
    """A forced resume acknowledges (revision 6 -> 7) and must then fence the
    refreshed stop on the acknowledgement's row, not the stale snapshot."""
    fakes.ticket_db.get_ticket.return_value = _ticket(
        status="cancelled", worker_daemon_id="d1", attempts=3, revision=6
    )
    fakes.release["may_have_launched"] = True
    fakes.release["stop"] = {
        "mode": "daemon", "verification": "unverified", "daemon_id": "d1"
    }
    fakes.force_ack.return_value = {
        "outcome": "ok",
        "ticket": _ticket(
            status="cancelled", worker_daemon_id="d1", attempts=3, revision=7
        ),
        "stop": {"mode": "daemon", "verification": "verified", "daemon_id": "d1"},
    }
    fakes.stop_request.return_value = {
        "outcome": "ok",
        "ticket": _ticket(
            status="cancelled", worker_daemon_id="d1", attempts=3, revision=7
        ),
        "stop": {"mode": "daemon", "verification": "verified", "daemon_id": "d1"},
    }
    fakes.ticket_db.update_ticket.return_value = _ticket(
        status="queued", attempts=3, revision=8
    )

    updated = await ownership.resume_ticket(
        user_id="test-user-123", ticket_id=TICKET_ID, force=True
    )

    assert updated["status"] == "queued"
    fakes.force_ack.assert_awaited_once()
    assert fakes.stop_request.call_args.kwargs["expected"]["revision"] == 7
    assert fakes.ticket_db.update_ticket.call_args.kwargs["expected_revision"] == 7
