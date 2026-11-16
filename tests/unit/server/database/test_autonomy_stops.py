"""Durable stop verification: request, acknowledge, timeout escalation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.server.database.autonomy import stops

TICKET_ID = "11111111-1111-4111-8111-111111111111"
DAEMON_ID = "22222222-2222-4222-8222-222222222222"
LEASE_ID = "33333333-3333-4333-8333-333333333333"
STOP_ID = "44444444-4444-4444-8444-444444444444"


def _ticket_row(status: str = "running", **overrides):
    now = datetime.now(timezone.utc)
    row = {
        "ticket_id": TICKET_ID,
        "user_id": "u1",
        "workspace_id": "w1",
        "title": "Ship it",
        "scope": "Ship it",
        "status": status,
        "mandate": {"source_id": "s1"},
        "policy_version": "v1",
        "origin_trigger_id": None,
        "origin_thread_key": "manual:1",
        "router_thread_id": None,
        "worker_thread_id": None,
        "worker_run_id": None,
        "worker_provider": "codex",
        "worker_model": "",
        "worker_daemon_id": DAEMON_ID,
        "attempts": 1,
        "attention": None,
        "summary": "",
        "error": "",
        "revision": 2,
        "created_at": now,
        "updated_at": now,
        "started_at": now,
        "ended_at": now if status == "cancelled" else None,
        "worker_lease_id": LEASE_ID if status == "running" else None,
        "worker_lease_claim_id": LEASE_ID if status == "running" else None,
        "worker_lease_expires_at": now + timedelta(seconds=60),
        "worker_lease_heartbeat_at": now - timedelta(seconds=5),
    }
    row.update(overrides)
    return row


def _stop_row(verification: str = "pending", **overrides):
    now = datetime.now(timezone.utc)
    row = {
        "stop_id": STOP_ID,
        "ticket_id": TICKET_ID,
        "user_id": "u1",
        "daemon_id": DAEMON_ID,
        "lease_id": LEASE_ID,
        "mode": "daemon",
        "verification": verification,
        "requested_at": now - timedelta(seconds=5),
        "deadline_at": now + timedelta(seconds=30),
        "acknowledged_at": None,
        "verified_at": None,
        "escalated_at": None,
        "reason": "",
        "evidence": {},
        "attempts": 0,
        "updated_at": now,
    }
    row.update(overrides)
    return row


def _joined(stop, **overrides):
    now = datetime.now(timezone.utc)
    row = {
        **stop,
        "ticket_status": "cancelled",
        "ended_at": now,
        "worker_daemon_id": DAEMON_ID,
        "worker_lease_id": LEASE_ID,
        "worker_lease_heartbeat_at": now - timedelta(seconds=10),
    }
    row.update(overrides)
    return row


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Cursor:
    def __init__(self, fetchone=(), fetchall=()):
        self._fetchone = list(fetchone)
        self._fetchall = list(fetchall)
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    async def fetchone(self):
        return self._fetchone.pop(0) if self._fetchone else None

    async def fetchall(self):
        return self._fetchall.pop(0) if self._fetchall else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, cursor):
        self.cur = cursor

    def cursor(self, row_factory=None):
        return self.cur

    def transaction(self):
        return _Tx()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch(monkeypatch, cursor):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def get_connection():
        yield _Conn(cursor)

    monkeypatch.setattr(stops, "get_db_connection", get_connection)


@pytest.mark.asyncio
async def test_request_stop_keeps_the_daemon_lease_pending_verification(monkeypatch):
    current = _ticket_row()
    updated = _ticket_row(status="cancelled", worker_lease_id=LEASE_ID)
    cursor = _Cursor(fetchone=[current, None, updated, _stop_row()])
    _patch(monkeypatch, cursor)

    result = await stops.request_stop(
        user_id="u1", ticket_id=TICKET_ID, timeout_seconds=30
    )

    assert result["outcome"] == "ok"
    assert result["stop"]["mode"] == "daemon"
    assert result["stop"]["verification"] == "pending"
    assert result["stop"]["verified"] is False
    update_sql, update_params = cursor.calls[2]
    assert "status = 'cancelled'" in update_sql
    assert "CASE WHEN %s THEN worker_lease_id ELSE NULL END" in update_sql
    assert update_params[0] is False
    assert update_params[1] is None
    assert update_params[2:6] == (True, True, True, True)
    assert cursor.calls[3][0].startswith("INSERT INTO autonomy_stop_requests")
    assert cursor.calls[3][1][6] == 30
    event_sql, event_params = cursor.calls[4]
    assert "INSERT INTO autonomy_ticket_events" in event_sql
    assert event_params[3] == "stopped"
    assert event_params[6].obj["from_status"] == "running"
    assert event_params[6].obj["verification"] == "pending"


@pytest.mark.asyncio
async def test_request_stop_does_not_verify_a_daemon_whose_lease_was_recovered(
    monkeypatch,
):
    # Attempts and started_at survive recover_expired_daemon_leases; the lease
    # does not. The CLI can still be running, so absence of a lease is not
    # proof of termination.
    current = _ticket_row(
        status="queued",
        worker_lease_id=None,
        worker_lease_claim_id=None,
        worker_lease_expires_at=None,
        worker_lease_heartbeat_at=None,
        attempts=1,
    )
    updated = _ticket_row(status="cancelled", worker_lease_id=None)
    unverified = _stop_row(
        verification="unverified",
        escalated_at=datetime.now(timezone.utc),
        reason=(
            "The worker lease was lost before the stop; its native "
            "process could not be verified"
        ),
    )
    cursor = _Cursor(fetchone=[current, None, updated, unverified])
    _patch(monkeypatch, cursor)

    result = await stops.request_stop(
        user_id="u1", ticket_id=TICKET_ID, timeout_seconds=30
    )

    assert result["outcome"] == "ok"
    assert result["stop"]["mode"] == "daemon"
    assert result["stop"]["verification"] == "unverified"
    assert result["stop"]["verified"] is False
    ticket_sql, ticket_params = cursor.calls[2]
    assert "attention = CASE WHEN %s THEN %s::jsonb ELSE NULL END" in ticket_sql
    assert ticket_params[0] is True
    assert ticket_params[1].obj["kind"] == "stop_unverified"
    insert_sql, insert_params = cursor.calls[3]
    assert "escalated_at" in insert_sql
    assert insert_params[5] == "unverified"
    assert insert_params[8] == "unverified"


@pytest.mark.asyncio
async def test_request_stop_does_not_verify_a_backend_launch_without_a_thread(
    monkeypatch,
):
    # A backend launch can be between create_thread and persisting the id.
    current = _ticket_row(
        status="queued",
        worker_daemon_id=None,
        worker_lease_id=None,
        worker_lease_claim_id=None,
        worker_lease_expires_at=None,
        worker_lease_heartbeat_at=None,
        attempts=1,
        started_at=None,
    )
    updated = _ticket_row(status="cancelled", worker_daemon_id=None)
    not_applicable = _stop_row(
        mode="backend",
        verification="not_applicable",
        daemon_id=None,
        lease_id=None,
    )
    cursor = _Cursor(fetchone=[current, None, updated, not_applicable])
    _patch(monkeypatch, cursor)

    result = await stops.request_stop(
        user_id="u1", ticket_id=TICKET_ID, timeout_seconds=30
    )

    assert result["stop"]["mode"] == "backend"
    assert result["stop"]["verification"] == "not_applicable"
    assert result["stop"]["verified"] is False


@pytest.mark.asyncio
async def test_request_stop_verifies_only_a_ticket_that_never_claimed_a_runner(
    monkeypatch,
):
    current = _ticket_row(
        status="queued",
        worker_lease_id=None,
        worker_lease_claim_id=None,
        worker_lease_expires_at=None,
        worker_lease_heartbeat_at=None,
        attempts=0,
        started_at=None,
    )
    updated = _ticket_row(status="cancelled", attempts=0, started_at=None)
    verified = _stop_row(
        mode="not_started",
        verification="verified",
        verified_at=datetime.now(timezone.utc),
    )
    cursor = _Cursor(fetchone=[current, None, updated, verified])
    _patch(monkeypatch, cursor)

    result = await stops.request_stop(
        user_id="u1", ticket_id=TICKET_ID, timeout_seconds=30
    )

    assert result["stop"]["mode"] == "not_started"
    assert result["stop"]["verification"] == "verified"
    insert_sql, insert_params = cursor.calls[3]
    assert insert_params[-1].obj == {"never_started": True}


@pytest.mark.asyncio
async def test_request_stop_is_idempotent_for_an_already_cancelled_ticket(monkeypatch):
    current = _ticket_row(status="cancelled")
    cursor = _Cursor(fetchone=[current, _stop_row(verification="verified")])
    _patch(monkeypatch, cursor)

    result = await stops.request_stop(
        user_id="u1", ticket_id=TICKET_ID, timeout_seconds=30
    )

    assert result["outcome"] == "ok"
    assert result["stop"]["verification"] == "verified"
    assert len(cursor.calls) == 2
    assert not any("autonomy_ticket_events" in sql for sql, _params in cursor.calls)


@pytest.mark.asyncio
async def test_new_stop_attempt_replaces_a_verified_receipt_for_the_current_lease(
    monkeypatch,
):
    current = _ticket_row(
        status="running", worker_lease_id=LEASE_ID, worker_lease_claim_id=LEASE_ID
    )
    old_receipt = _stop_row(
        verification="verified",
        lease_id="55555555-5555-4555-8555-555555555555",
        verified_at=datetime.now(timezone.utc),
    )
    updated = _ticket_row(status="cancelled", worker_lease_id=LEASE_ID)
    refreshed = _stop_row(verification="pending")
    cursor = _Cursor(fetchone=[current, old_receipt, updated, refreshed])
    _patch(monkeypatch, cursor)

    result = await stops.request_stop(
        user_id="u1", ticket_id=TICKET_ID, timeout_seconds=30
    )

    assert result["stop"]["verification"] == "pending"
    insert_sql, _params = cursor.calls[3]
    assert "ON CONFLICT (ticket_id) DO UPDATE SET" in insert_sql
    for field in (
        "lease_id = EXCLUDED.lease_id",
        "verification = EXCLUDED.verification",
        "requested_at = EXCLUDED.requested_at",
        "acknowledged_at = NULL",
        "attempts = 0",
    ):
        assert field in insert_sql


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "archived"])
async def test_request_stop_refuses_a_terminal_ticket(monkeypatch, status):
    current = _ticket_row(status=status)
    cursor = _Cursor(fetchone=[current, None])
    _patch(monkeypatch, cursor)

    result = await stops.request_stop(
        user_id="u1", ticket_id=TICKET_ID, timeout_seconds=30
    )

    assert result["outcome"] == "ended"
    assert len(cursor.calls) == 2


@pytest.mark.asyncio
async def test_acknowledgement_verifies_after_state_is_persisted(monkeypatch):
    now = datetime.now(timezone.utc)
    joined = _joined(_stop_row())
    verified = _stop_row(
        verification="verified",
        acknowledged_at=now,
        verified_at=now,
        attempts=1,
    )
    cursor = _Cursor(fetchone=[joined, verified])
    _patch(monkeypatch, cursor)

    result = await stops.acknowledge_stop(
        daemon_id=DAEMON_ID,
        ticket_id=TICKET_ID,
        lease_id=LEASE_ID,
        process_gone=True,
        evidence={"processes": [{"pid": 42, "alive": False}], "signal": "SIGKILL"},
    )

    assert result["outcome"] == "ok"
    assert result["stop"]["verification"] == "verified"
    assert result["stop"]["verified"] is True
    update_sql, params = cursor.calls[1]
    assert "verification = %s" in update_sql
    assert params[0] == "verified"
    assert params[1] is True
    assert params[2] is False
    lease_sql, lease_params = cursor.calls[2]
    assert "worker_lease_heartbeat_at = NULL" in lease_sql
    assert lease_params == (False, None, TICKET_ID)


@pytest.mark.asyncio
async def test_acknowledgement_rejects_a_heartbeat_after_the_stop_request(monkeypatch):
    now = datetime.now(timezone.utc)
    joined = _joined(
        _stop_row(requested_at=now - timedelta(seconds=5)),
        worker_lease_heartbeat_at=now - timedelta(seconds=1),
    )
    cursor = _Cursor(fetchone=[joined])
    _patch(monkeypatch, cursor)

    result = await stops.acknowledge_stop(
        daemon_id=DAEMON_ID,
        ticket_id=TICKET_ID,
        lease_id=LEASE_ID,
        process_gone=True,
        evidence={},
    )

    assert result["outcome"] == "heartbeat_active"
    assert len(cursor.calls) == 1


@pytest.mark.asyncio
async def test_acknowledgement_escalates_a_process_that_still_runs(monkeypatch):
    now = datetime.now(timezone.utc)
    joined = _joined(_stop_row())
    unverified = _stop_row(
        verification="unverified",
        acknowledged_at=now,
        escalated_at=now,
        reason="Daemon reported the native CLI process is still running",
        attempts=1,
    )
    cursor = _Cursor(fetchone=[joined, unverified])
    _patch(monkeypatch, cursor)

    result = await stops.acknowledge_stop(
        daemon_id=DAEMON_ID,
        ticket_id=TICKET_ID,
        lease_id=LEASE_ID,
        process_gone=False,
        evidence={"processes": [{"pid": 42, "alive": True}]},
    )

    assert result["outcome"] == "ok"
    assert result["stop"]["verification"] == "unverified"
    assert result["stop"]["escalated"] is True
    update_sql, params = cursor.calls[1]
    assert params[0] == "unverified"
    assert params[2] is True
    ticket_sql, ticket_params = cursor.calls[2]
    assert "attention = CASE WHEN %s THEN %s ELSE attention END" in ticket_sql
    assert ticket_params[0] is True
    assert ticket_params[2] == TICKET_ID


@pytest.mark.asyncio
async def test_expired_acknowledgements_escalate_and_revoke_the_lease(monkeypatch):
    cursor = _Cursor(fetchall=[[_stop_row()]])
    _patch(monkeypatch, cursor)

    assert await stops.expire_stop_requests() == 1

    stop_sql, stop_params = cursor.calls[1]
    assert "verification = 'unverified'" in stop_sql
    assert stop_params[-1] == STOP_ID
    ticket_sql, ticket_params = cursor.calls[2]
    assert "worker_lease_expires_at = NULL" in ticket_sql
    assert "status IN ('cancelled', 'archived')" in ticket_sql
    assert ticket_params[1] == TICKET_ID


@pytest.mark.asyncio
async def test_force_acknowledge_verifies_an_unverified_stop_and_audits_it(monkeypatch):
    now = datetime.now(timezone.utc)
    current = _ticket_row(status="cancelled", worker_lease_id=None, live_lease=False)
    current["live_lease"] = False
    existing = _stop_row(verification="unverified", escalated_at=now)
    verified = _stop_row(verification="verified", verified_at=now)
    updated = _ticket_row(status="cancelled", worker_lease_id=None, revision=3)
    cursor = _Cursor(fetchone=[current, existing, verified, updated])
    _patch(monkeypatch, cursor)

    result = await stops.force_acknowledge_unverified(
        user_id="u1", ticket_id=TICKET_ID, reason="daemon is dead"
    )

    assert result["outcome"] == "ok"
    assert result["stop"]["verification"] == "verified"
    assert result["ticket"]["revision"] == 3
    sqls = [sql for sql, _params in cursor.calls]
    assert any("verification = 'verified'" in sql for sql in sqls)
    assert any("stop_unverified" in sql for sql in sqls)
    event_sql, event_params = cursor.calls[-1]
    assert "INSERT INTO autonomy_ticket_events" in event_sql
    assert event_params[3] == "stop_acknowledged"
    assert event_params[6].obj["override"] == "acknowledge_unverified"
    assert event_params[6].obj["prior_verification"] == "unverified"
    upsert = next(sql for sql in sqls if "ON CONFLICT" in sql)
    assert "daemon_id = EXCLUDED.daemon_id" in upsert
    assert "lease_id = EXCLUDED.lease_id" in upsert
    assert "mode = EXCLUDED.mode" in upsert


@pytest.mark.asyncio
async def test_force_acknowledge_refuses_a_live_lease(monkeypatch):
    current = _ticket_row(status="running")
    current["live_lease"] = True
    cursor = _Cursor(fetchone=[current])
    _patch(monkeypatch, cursor)

    result = await stops.force_acknowledge_unverified(
        user_id="u1", ticket_id=TICKET_ID, reason="daemon is dead"
    )

    assert result["outcome"] == "live_lease"
    assert not any(
        "autonomy_ticket_events" in sql for sql, _params in cursor.calls
    )


@pytest.mark.asyncio
async def test_list_stops_returns_only_known_fields(monkeypatch):
    cursor = _Cursor(fetchall=[[_stop_row()]])
    _patch(monkeypatch, cursor)

    rows = await stops.list_stops([TICKET_ID])

    assert rows[0]["ticket_id"] == TICKET_ID
    assert rows[0]["verified"] is False
    assert "ticket_status" not in rows[0]


@pytest.mark.asyncio
async def test_acknowledgement_accepts_an_archived_ticket(monkeypatch):
    now = datetime.now(timezone.utc)
    joined = _joined(_stop_row(), ticket_status="archived", ended_at=now)
    verified = _stop_row(
        verification="verified",
        acknowledged_at=now,
        verified_at=now,
        attempts=1,
    )
    cursor = _Cursor(fetchone=[joined, verified])
    _patch(monkeypatch, cursor)

    result = await stops.acknowledge_stop(
        daemon_id=DAEMON_ID,
        ticket_id=TICKET_ID,
        lease_id=LEASE_ID,
        process_gone=True,
        evidence={"processes": [{"pid": 42, "alive": False}]},
    )

    assert result["outcome"] == "ok"
    assert result["stop"]["verification"] == "verified"
    lease_sql, lease_params = cursor.calls[2]
    assert "worker_lease_id = NULL" in lease_sql
    assert lease_params[2] == TICKET_ID


@pytest.mark.asyncio
async def test_acknowledgement_still_refuses_a_running_ticket(monkeypatch):
    joined = _joined(_stop_row(), ticket_status="running", ended_at=None)
    cursor = _Cursor(fetchone=[joined])
    _patch(monkeypatch, cursor)

    result = await stops.acknowledge_stop(
        daemon_id=DAEMON_ID,
        ticket_id=TICKET_ID,
        lease_id=LEASE_ID,
        process_gone=True,
        evidence={},
    )

    assert result["outcome"] == "state_not_persisted"
    assert len(cursor.calls) == 1


@pytest.mark.asyncio
async def test_force_acknowledge_rewrites_a_verified_receipt_for_another_lease(
    monkeypatch,
):
    old_lease = "55555555-5555-4555-8555-555555555555"
    now = datetime.now(timezone.utc)
    current = _ticket_row(
        status="running",
        worker_lease_id=LEASE_ID,
        worker_lease_expires_at=now - timedelta(seconds=30),
    )
    current["live_lease"] = False
    existing = _stop_row(
        verification="verified",
        lease_id=old_lease,
        verified_at=now - timedelta(hours=1),
        requested_at=now - timedelta(hours=1),
    )
    rewritten = _stop_row(verification="verified", lease_id=LEASE_ID, verified_at=now)
    updated = _ticket_row(status="running", worker_lease_id=None, revision=4)
    cursor = _Cursor(fetchone=[current, existing, rewritten, updated])
    _patch(monkeypatch, cursor)

    result = await stops.force_acknowledge_unverified(
        user_id="u1", ticket_id=TICKET_ID, reason="daemon is dead"
    )

    assert result["outcome"] == "ok"
    assert result["stop"]["lease_id"] == LEASE_ID
    assert result["ticket"]["worker_lease_id"] is None
    upsert = next(sql for sql, _params in cursor.calls if "ON CONFLICT" in sql)
    assert "daemon_id = EXCLUDED.daemon_id" in upsert
    assert "lease_id = EXCLUDED.lease_id" in upsert
    assert "mode = EXCLUDED.mode" in upsert
    event_sql, event_params = cursor.calls[-1]
    assert event_params[3] == "stop_acknowledged"
    assert event_params[6].obj["from_status"] == "running"


@pytest.mark.asyncio
async def test_force_acknowledge_is_a_no_op_when_this_attempt_is_already_verified(
    monkeypatch,
):
    current = _ticket_row(status="cancelled", worker_lease_id=None)
    current["live_lease"] = False
    existing = _stop_row(
        verification="verified",
        lease_id=None,
        daemon_id=DAEMON_ID,
        verified_at=datetime.now(timezone.utc),
    )
    cursor = _Cursor(fetchone=[current, existing])
    _patch(monkeypatch, cursor)

    result = await stops.force_acknowledge_unverified(
        user_id="u1", ticket_id=TICKET_ID, reason="daemon is dead"
    )

    assert result["outcome"] == "ok"
    assert result["stop"]["verification"] == "verified"
    assert not any("ON CONFLICT" in sql for sql, _params in cursor.calls)
    assert not any(
        "autonomy_ticket_events" in sql for sql, _params in cursor.calls
    )

def test_live_lease_detection_does_not_depend_on_running_status():
    """A normal stop keeps the lease while status is cancelled; that lease is
    still a live runner the operator cannot force past."""
    sql = " ".join(stops._live_lease_sql().split())
    assert "worker_lease_id IS NOT NULL" in sql
    assert "worker_lease_expires_at > clock_timestamp()" in sql
    assert "status = 'running'" not in sql


@pytest.mark.asyncio
async def test_force_acknowledge_refuses_an_unexpired_lease_after_a_stop(monkeypatch):
    current = _ticket_row(status="cancelled", worker_lease_id="lease-live")
    current["live_lease"] = True
    cursor = _Cursor(fetchone=[current])
    _patch(monkeypatch, cursor)

    result = await stops.force_acknowledge_unverified(
        user_id="u1", ticket_id=TICKET_ID, reason="daemon is dead"
    )

    assert result["outcome"] == "live_lease"
    assert not any(
        "verification = 'verified'" in sql for sql, _params in cursor.calls
    )
