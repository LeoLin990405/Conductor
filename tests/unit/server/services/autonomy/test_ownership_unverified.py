"""Unverified daemon stops, exercised through the real stop transaction.

``request_ticket_stop`` is not mocked. A scripted cursor applies the stop,
release, and force-acknowledge statements to one ticket so a failed reassign
cannot hide a cancellation behind a fake return value.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

from src.server.database.autonomy import stops
from src.server.services.autonomy import ownership

TICKET_ID = "11111111-1111-4111-8111-111111111111"
DAEMON_ID = "22222222-2222-4222-8222-222222222222"
USER_ID = "u1"
NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)


def _ticket(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ticket_id": TICKET_ID,
        "user_id": USER_ID,
        "workspace_id": "w1",
        "title": "Ship it",
        "scope": "Ship it",
        "status": "queued",
        "mandate": {},
        "policy_version": "v1",
        "origin_trigger_id": None,
        "origin_thread_key": "manual:1",
        "router_thread_id": None,
        "worker_thread_id": None,
        "worker_run_id": None,
        "worker_provider": "codex",
        "worker_model": "",
        "worker_daemon_id": DAEMON_ID,
        "attempts": 2,
        "attention": None,
        "summary": "",
        "error": "",
        "revision": 5,
        "switches": [],
        "created_at": NOW,
        "updated_at": NOW,
        "started_at": NOW,
        "ended_at": None,
        "closed_at": None,
        "close_reason": "",
        "worker_lease_id": None,
        "worker_lease_claim_id": None,
        "worker_lease_expires_at": None,
        "worker_lease_heartbeat_at": None,
    }
    row.update(overrides)
    return row


def _stop(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "stop_id": "44444444-4444-4444-8444-444444444444",
        "ticket_id": TICKET_ID,
        "user_id": USER_ID,
        "daemon_id": DAEMON_ID,
        "lease_id": None,
        "mode": "daemon",
        "verification": "unverified",
        "requested_at": NOW,
        "deadline_at": NOW,
        "acknowledged_at": None,
        "verified_at": None,
        "escalated_at": NOW,
        "reason": "lease lost",
        "evidence": {},
        "attempts": 1,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


def _json_obj(value: Any) -> Any:
    return value.obj if hasattr(value, "obj") else value


class _Store:
    def __init__(self, ticket: dict[str, Any], stop: dict[str, Any] | None):
        self.ticket = ticket
        self.stop = stop
        self.sqls: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.transactions = 0


class _Cursor:
    def __init__(self, store: _Store):
        self.store = store
        self._pending: dict[str, Any] | None = None

    async def execute(self, sql: str, params=None):
        text = " ".join(sql.split())
        self.store.sqls.append(text)
        self._pending = None
        if "INSERT INTO autonomy_ticket_events" in text:
            detail = _json_obj(params[6])
            self.store.events.append(
                {"action": params[3], "detail": dict(detail or {})}
            )
            return
        if "INSERT INTO autonomy_stop_requests" in text:
            self._pending = self._apply_stop_insert(text, params or ())
            return
        if "UPDATE autonomy_tickets" in text and "status = 'cancelled'" in text:
            self._pending = self._cancel_ticket()
            return
        if "UPDATE autonomy_tickets" in text and "stop_unverified" in text:
            self._pending = self._clear_unverified_lease()
            return
        if "SELECT" in text and "FROM autonomy_stop_requests" in text:
            self._pending = dict(self.store.stop) if self.store.stop else None
            return
        if "SELECT" in text and "FROM autonomy_tickets" in text:
            row = dict(self.store.ticket)
            row["live_lease"] = False
            self._pending = row
            return
        raise AssertionError(f"unexpected SQL: {text}")

    def _apply_stop_insert(self, text: str, params: tuple) -> dict[str, Any]:
        if "verification = 'verified'" in text:
            evidence = _json_obj(params[5])
            self.store.stop = _stop(
                verification="verified",
                daemon_id=str(params[2]) if params[2] else None,
                lease_id=params[3],
                reason=params[4],
                evidence=dict(evidence or {}),
                verified_at=NOW,
                acknowledged_at=NOW,
            )
        else:
            # request_stop's upsert. A failed reassign must not reach this.
            self.store.stop = _stop(
                verification=params[5],
                mode=params[4],
                daemon_id=str(params[2]) if params[2] else None,
                lease_id=params[3],
                reason=params[9],
                evidence=dict(_json_obj(params[10]) or {}),
            )
        return dict(self.store.stop)

    def _cancel_ticket(self) -> dict[str, Any]:
        self.store.ticket["status"] = "cancelled"
        self.store.ticket["revision"] = int(self.store.ticket["revision"]) + 1
        self.store.ticket["ended_at"] = self.store.ticket.get("ended_at") or NOW
        return dict(self.store.ticket)

    def _clear_unverified_lease(self) -> dict[str, Any]:
        attention = self.store.ticket.get("attention")
        if isinstance(attention, dict) and attention.get("kind") == "stop_unverified":
            self.store.ticket["attention"] = None
        for key in (
            "worker_lease_id",
            "worker_lease_claim_id",
            "worker_lease_expires_at",
            "worker_lease_heartbeat_at",
        ):
            self.store.ticket[key] = None
        self.store.ticket["revision"] = int(self.store.ticket["revision"]) + 1
        return dict(self.store.ticket)

    async def fetchone(self):
        row = self._pending
        self._pending = None
        return row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _Connection:
    def __init__(self, store: _Store):
        self.store = store

    @asynccontextmanager
    async def cursor(self, **_kwargs):
        yield _Cursor(self.store)

    @asynccontextmanager
    async def transaction(self):
        self.store.transactions += 1
        yield self


def _bind(monkeypatch, store: _Store):
    @asynccontextmanager
    async def get_connection():
        yield _Connection(store)

    reassigns: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []

    async def get_ticket(_ticket_id: str):
        return dict(store.ticket)

    async def update_ticket(_ticket_id: str, **kwargs):
        updates.append(kwargs)
        return {**store.ticket, "status": kwargs.get("status", store.ticket["status"])}

    async def reassign(_ticket_id: str, **kwargs):
        reassigns.append(kwargs)
        status = store.ticket["status"]
        if status == "running" or (status == "cancelled" and kwargs.get("restart_cancelled")):
            status = "queued"
        return {**store.ticket, "status": status, "worker_daemon_id": kwargs.get("worker_daemon_id")}

    async def mandate(_row):
        return None

    async def enqueue(**_kwargs):
        return None

    monkeypatch.setattr(stops, "get_db_connection", get_connection)
    monkeypatch.setattr(ownership.ticket_db, "get_ticket", get_ticket)
    monkeypatch.setattr(ownership.ticket_db, "update_ticket", update_ticket)
    monkeypatch.setattr(ownership.line_db, "reassign_ticket", reassign)
    monkeypatch.setattr(ownership, "revoked_mandate_reason", mandate)
    monkeypatch.setattr(ownership, "enqueue_effect", enqueue)
    return reassigns, updates


def _cancelled(sql: str) -> bool:
    return "status = 'cancelled'" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "blocked", "waiting"])
@pytest.mark.parametrize("stop_verification", ["absent", "unverified"])
async def test_reassign_of_a_launched_daemon_ticket_does_not_cancel(
    monkeypatch, status, stop_verification
):
    revision = 5
    store = _Store(
        _ticket(status=status, revision=revision),
        None if stop_verification == "absent" else _stop(),
    )
    reassigns, updates = _bind(monkeypatch, store)

    with pytest.raises(ownership.TicketOpError, match="not changed") as err:
        await ownership.reassign_ticket(
            user_id=USER_ID, ticket_id=TICKET_ID, daemon_id=None
        )

    assert err.value.status_code == 409
    assert store.ticket["status"] == status
    assert store.ticket["revision"] == revision
    assert not any(_cancelled(sql) for sql in store.sqls)
    assert store.events == []
    assert reassigns == []
    assert updates == []


@pytest.mark.asyncio
async def test_reassign_of_a_cancelled_unverified_daemon_ticket_does_not_requeue(
    monkeypatch,
):
    store = _Store(
        _ticket(status="cancelled", ended_at=NOW, revision=4),
        _stop(),
    )
    reassigns, updates = _bind(monkeypatch, store)

    with pytest.raises(ownership.TicketOpError, match="not changed") as err:
        await ownership.reassign_ticket(
            user_id=USER_ID, ticket_id=TICKET_ID, daemon_id=None
        )

    assert err.value.status_code == 409
    assert store.ticket["status"] == "cancelled"
    assert store.ticket["revision"] == 4
    assert not any(_cancelled(sql) for sql in store.sqls)
    assert store.events == []
    assert reassigns == []
    assert updates == []


@pytest.mark.asyncio
async def test_resume_of_an_unverified_stop_leaves_the_ticket_cancelled(monkeypatch):
    store = _Store(_ticket(status="cancelled", ended_at=NOW, revision=4), _stop())
    _reassigns, updates = _bind(monkeypatch, store)

    with pytest.raises(ownership.TicketOpError, match="not changed"):
        await ownership.resume_ticket(user_id=USER_ID, ticket_id=TICKET_ID)

    assert store.ticket["status"] == "cancelled"
    assert store.ticket["revision"] == 4
    assert not any(_cancelled(sql) for sql in store.sqls)
    assert updates == []
    assert store.events == []


@pytest.mark.asyncio
async def test_acknowledge_unverified_then_reassign_restarts_without_another_cancel(
    monkeypatch,
):
    store = _Store(
        _ticket(
            status="cancelled",
            ended_at=NOW,
            revision=4,
            attention={"kind": "stop_unverified", "question": "confirm"},
        ),
        _stop(),
    )
    reassigns, _updates = _bind(monkeypatch, store)

    acknowledged = await ownership.acknowledge_unverified_stop(
        user_id=USER_ID, ticket_id=TICKET_ID
    )

    assert acknowledged["stop"]["verification"] == "verified"
    assert store.stop is not None
    assert store.stop["verification"] == "verified"
    assert store.ticket["status"] == "cancelled"
    assert store.ticket["revision"] == 5
    assert store.ticket["attention"] is None
    assert store.events == [
        {
            "action": "stop_acknowledged",
            "detail": {
                "override": "acknowledge_unverified",
                "forced": True,
                "prior_verification": "unverified",
                "from_status": "cancelled",
            },
        }
    ]
    assert store.transactions == 1
    assert not any(_cancelled(sql) for sql in store.sqls)

    updated = await ownership.reassign_ticket(
        user_id=USER_ID, ticket_id=TICKET_ID, daemon_id=None
    )

    assert updated["status"] == "queued"
    assert len(reassigns) == 1
    assert reassigns[0]["restart_cancelled"] is True
    assert reassigns[0]["expected_status"] == "cancelled"
    assert reassigns[0]["expected_revision"] == 5
    assert not any(_cancelled(sql) for sql in store.sqls)
    # The idempotent stop refresh must not write a second stopped event.
    assert len(store.events) == 1


@pytest.mark.asyncio
async def test_force_reassign_of_a_launched_ticket_does_not_cancel_it(monkeypatch):
    store = _Store(
        _ticket(
            status="blocked",
            revision=4,
            attention={"kind": "stop_unverified", "question": "confirm"},
        ),
        _stop(),
    )
    reassigns, _updates = _bind(monkeypatch, store)

    updated = await ownership.reassign_ticket(
        user_id=USER_ID, ticket_id=TICKET_ID, daemon_id=None, force=True
    )

    assert updated["status"] == "blocked"
    assert store.ticket["status"] == "blocked"
    assert store.ticket["revision"] == 5
    assert store.stop is not None and store.stop["verification"] == "verified"
    assert not any(_cancelled(sql) for sql in store.sqls)
    assert store.events[0]["detail"]["override"] == "acknowledge_unverified"
    assert reassigns[0]["restart_cancelled"] is False
    assert reassigns[0]["expected_status"] == "blocked"
    assert reassigns[0]["expected_revision"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_verification", ["absent", "unverified"])
async def test_archive_of_a_launched_blocked_ticket_does_not_cancel(
    monkeypatch, stop_verification
):
    store = _Store(
        _ticket(status="blocked", revision=4),
        None if stop_verification == "absent" else _stop(),
    )
    _reassigns, updates = _bind(monkeypatch, store)

    updated = await ownership.archive_ticket(user_id=USER_ID, ticket_id=TICKET_ID)

    assert updated["status"] == "archived"
    assert store.ticket["status"] == "blocked"
    assert store.ticket["revision"] == 4
    assert not any(_cancelled(sql) for sql in store.sqls)
    assert store.events == []
    assert updates[0]["expected_status"] == "blocked"
    assert updates[0]["status"] == "archived"
    assert updates[0]["event"]["action"] == "archived"
