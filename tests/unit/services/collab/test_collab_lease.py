"""Lease lifecycle: heartbeat, release, expiry sweep, revoke, sweeper loop (N80 L2).

Every persistence call goes to the in-memory ``FakeStore`` (see ``conftest``);
expiry is simulated by moving ``expires_at`` / ``max_lease_until`` (or a
handoff's ``expires_at``) into the past. No database, socket or real git
repository is touched; the push-guard test runs ``git`` only against an empty
``tmp_path`` directory with discovery and global config fenced off.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.collab.schemas import validate_tool_output
from src.server.services.collab import apps as app_svc
from src.server.services.collab import leases as lease_svc
from src.server.services.collab import sweep
from tests.unit.services.collab.collab_fakes import USER, FakeStore, now

APP = "app-a"
OTHER = "app-b"
FULL = {"CONDUCTOR_COLLAB_MODE": "full"}


async def _claim(
    store: FakeStore,
    call: Any,
    *,
    app: str = APP,
    write_scope: tuple[str, ...] = ("docs/**",),
    token: str | None = None,
) -> tuple[str, str, str]:
    """Register ``app`` (claim scope), seed a queued Ticket and claim it."""
    if token is None:
        token = store.add_app(app, scopes=("read", "claim"))
    ticket = store.add_ticket(write_scope=write_scope)
    status, envelope = await call(
        "task_claim",
        token,
        ref=f"t:{ticket['ticket_id']}",
        revision=ticket["revision"],
    )
    assert status == 200, envelope
    lease_id = envelope["data"]["lease_id"]
    assert store.leases[lease_id]["state"] == "active"
    assert store.tickets[ticket["ticket_id"]]["status"] == "running"
    return token, ticket["ticket_id"], lease_id


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _collab_events(store: FakeStore, ticket_id: str, name: str) -> list[dict[str, Any]]:
    return [
        event
        for event in store.events_for(ticket_id)
        if event["detail"].get("collab_event") == name
    ]


# ---------------------------------------------------------------------------
# 1. Heartbeat renews, never beyond the ceiling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_renews_expiry(store, call):
    token, _, lease_id = await _claim(store, call)
    store.leases[lease_id]["expires_at"] = now() + timedelta(seconds=30)
    before = store.leases[lease_id]["expires_at"]

    status, envelope = await call("task_heartbeat", token, lease_id=lease_id)

    assert status == 200, envelope
    data = envelope["data"]
    validate_tool_output("task_heartbeat", data)
    assert data["lease_id"] == lease_id
    assert data["state"] == "active"
    renewed = store.leases[lease_id]["expires_at"]
    assert renewed > before
    assert renewed <= store.leases[lease_id]["max_lease_until"]
    assert _parse(data["expires_at"]) == renewed
    assert "heartbeat_at" in data


@pytest.mark.asyncio
async def test_heartbeat_never_extends_past_max_lease_until(store, call):
    token, ticket_id, lease_id = await _claim(store, call)
    ceiling = now() + timedelta(minutes=2)
    store.leases[lease_id]["max_lease_until"] = ceiling
    store.leases[lease_id]["expires_at"] = now() + timedelta(seconds=30)

    status, envelope = await call("task_heartbeat", token, lease_id=lease_id)

    assert status == 200, envelope
    data = envelope["data"]
    validate_tool_output("task_heartbeat", data)
    assert store.leases[lease_id]["expires_at"] <= ceiling
    assert _parse(data["expires_at"]) <= ceiling
    assert data.get("ref") in (None, f"t:{ticket_id}")


# ---------------------------------------------------------------------------
# 2. Heartbeat refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_on_expired_unswept_lease_is_lease_expired(store, call):
    token, _, lease_id = await _claim(store, call)
    store.leases[lease_id]["expires_at"] = now() - timedelta(seconds=1)

    status, envelope = await call("task_heartbeat", token, lease_id=lease_id)

    assert status == 409
    assert envelope["ok"] is False
    assert envelope["code"] == "lease_expired"
    # Not swept: the row is untouched by the refusal.
    assert store.leases[lease_id]["state"] == "active"


@pytest.mark.asyncio
async def test_heartbeat_on_another_apps_lease_is_lease_lost(store, call):
    _, _, lease_id = await _claim(store, call)
    other = store.add_app(OTHER, scopes=("read", "claim"))
    before = store.leases[lease_id]["expires_at"]

    status, envelope = await call("task_heartbeat", other, lease_id=lease_id)

    assert status == 409
    assert envelope["code"] == "lease_lost"
    assert store.leases[lease_id]["expires_at"] == before
    assert store.leases[lease_id]["app_id"] == APP


@pytest.mark.asyncio
@pytest.mark.parametrize("lease_id", [str(uuid.uuid4()), "not-a-uuid", "", "../etc/passwd"])
async def test_heartbeat_on_unknown_lease_is_not_found(store, call, lease_id):
    token = store.add_app(APP, scopes=("read", "claim"))

    status, envelope = await call("task_heartbeat", token, lease_id=lease_id)

    assert status == 404
    assert envelope["code"] == "not_found"


# ---------------------------------------------------------------------------
# 3. Ceiling in the past
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_lease_until_in_past_refuses_heartbeat_and_sweep_expires(store, call):
    token, ticket_id, lease_id = await _claim(store, call)
    revision = store.tickets[ticket_id]["revision"]
    store.leases[lease_id]["max_lease_until"] = now() - timedelta(seconds=1)
    assert store.leases[lease_id]["expires_at"] > now()

    status, envelope = await call("task_heartbeat", token, lease_id=lease_id)
    assert status == 409
    assert envelope["code"] == "lease_expired"

    result = await sweep.sweep_once(environ=FULL)

    assert result["expired"] == 1
    assert store.leases[lease_id]["state"] == "expired"
    assert store.tickets[ticket_id]["status"] == "queued"
    assert store.tickets[ticket_id]["revision"] == revision + 1
    assert _collab_events(store, ticket_id, "lease_expired")


# ---------------------------------------------------------------------------
# 4. Release
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_requeues_ticket_and_records_event(store, call):
    token, ticket_id, lease_id = await _claim(store, call)
    revision = store.tickets[ticket_id]["revision"]

    status, envelope = await call(
        "task_release", token, lease_id=lease_id, reason="cannot_do", note="no time"
    )

    assert status == 200, envelope
    assert envelope["ok"] is True
    validate_tool_output("task_release", envelope["data"])
    assert envelope["data"]["state"] == "released"
    assert store.leases[lease_id]["state"] == "released"
    assert store.leases[lease_id]["end_reason"] == "cannot_do"
    assert store.tickets[ticket_id]["status"] == "queued"
    assert store.tickets[ticket_id]["revision"] == revision + 1

    released = _collab_events(store, ticket_id, "released")
    assert len(released) == 1
    event = released[0]
    assert event["action"] == "reassigned"
    assert event["detail"]["reason"] == "cannot_do"
    assert event["detail"]["lease_id"] == lease_id
    assert event["actor"] == f"app:{APP}"
    assert event["from_owner"] == f"app:{APP}"
    assert event["to_owner"] == "router"
    assert "ok" in store.audit_codes()


@pytest.mark.asyncio
async def test_release_twice_is_lease_lost(store, call):
    token, ticket_id, lease_id = await _claim(store, call)
    status, _ = await call("task_release", token, lease_id=lease_id, reason="other")
    assert status == 200
    events_after_first = len(store.events_for(ticket_id))
    revision_after_first = store.tickets[ticket_id]["revision"]

    status, envelope = await call("task_release", token, lease_id=lease_id, reason="other")

    assert status == 409
    assert envelope["code"] == "lease_lost"
    assert len(store.events_for(ticket_id)) == events_after_first
    assert store.tickets[ticket_id]["revision"] == revision_after_first


# ---------------------------------------------------------------------------
# 5. Expiry sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_expires_stale_lease_and_requeues(store, call):
    token, ticket_id, lease_id = await _claim(store, call)
    revision = store.tickets[ticket_id]["revision"]
    store.leases[lease_id]["expires_at"] = now() - timedelta(seconds=5)

    result = await sweep.sweep_once(environ=FULL)

    assert result["expired"] == 1
    assert result["mode_expired"] == 0
    assert store.tickets[ticket_id]["status"] == "queued"
    assert store.tickets[ticket_id]["revision"] == revision + 1
    assert store.leases[lease_id]["state"] == "expired"
    events = _collab_events(store, ticket_id, "lease_expired")
    assert len(events) == 1
    assert events[0]["actor"].startswith("app:")
    assert events[0]["actor"] == f"app:{APP}"
    assert events[0]["detail"]["reason"] == "lease_expired"
    assert events[0]["detail"]["lease_id"] == lease_id

    status, envelope = await call("task_heartbeat", token, lease_id=lease_id)
    assert status == 409
    assert envelope["code"] in {"lease_expired", "lease_lost"}

    again = await sweep.sweep_once(environ=FULL)
    assert again["expired"] == 0
    assert len(_collab_events(store, ticket_id, "lease_expired")) == 1


@pytest.mark.asyncio
async def test_sweep_keeps_a_live_lease(store, call):
    _, ticket_id, lease_id = await _claim(store, call)

    result = await sweep.sweep_once(environ=FULL)

    assert result == {"expired": 0, "handoffs": 0, "reviews": 0, "mode_expired": 0}
    assert store.leases[lease_id]["state"] == "active"
    assert store.tickets[ticket_id]["status"] == "running"


# ---------------------------------------------------------------------------
# 6. Mode change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_in_docs_mode_expires_non_docs_lease(store, call):
    _, ticket_id, lease_id = await _claim(store, call, write_scope=("src/a.py",))

    result = await sweep.sweep_once(environ={"CONDUCTOR_COLLAB_MODE": "docs"})

    assert result["mode_expired"] == 1
    assert store.leases[lease_id]["state"] == "expired"
    assert store.leases[lease_id]["end_reason"] == "mode_change"
    assert store.tickets[ticket_id]["status"] == "queued"
    events = _collab_events(store, ticket_id, "lease_expired")
    assert len(events) == 1
    assert events[0]["detail"]["reason"] == "mode_change"


@pytest.mark.asyncio
async def test_sweep_in_docs_mode_keeps_docs_lease(store, call):
    _, ticket_id, lease_id = await _claim(store, call, write_scope=("docs/**",))

    result = await sweep.sweep_once(environ={"CONDUCTOR_COLLAB_MODE": "docs"})

    assert result["mode_expired"] == 0
    assert store.leases[lease_id]["state"] == "active"
    assert store.tickets[ticket_id]["status"] == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "shadow"])
async def test_sweep_in_off_or_shadow_expires_every_lease(store, call, mode):
    _, docs_ticket, docs_lease = await _claim(store, call, write_scope=("docs/**",))
    _, code_ticket, code_lease = await _claim(store, call, app=OTHER, write_scope=("src/a.py",))

    result = await sweep.sweep_once(environ={"CONDUCTOR_COLLAB_MODE": mode})

    assert result["mode_expired"] == 2
    for ticket_id, lease_id in ((docs_ticket, docs_lease), (code_ticket, code_lease)):
        assert store.leases[lease_id]["state"] == "expired"
        assert store.leases[lease_id]["end_reason"] == "mode_change"
        assert store.tickets[ticket_id]["status"] == "queued"
        events = _collab_events(store, ticket_id, "lease_expired")
        assert [e["detail"]["reason"] for e in events] == ["mode_change"]


@pytest.mark.asyncio
async def test_sweep_in_full_mode_keeps_non_docs_lease(store, call):
    _, ticket_id, lease_id = await _claim(store, call, write_scope=("src/a.py",))

    result = await sweep.sweep_once(environ=FULL)

    assert result["mode_expired"] == 0
    assert store.leases[lease_id]["state"] == "active"
    assert store.tickets[ticket_id]["status"] == "running"


# ---------------------------------------------------------------------------
# 7. Handoff expiry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_releases_lease_of_expired_handoff(store, call):
    token, ticket_id, lease_id = await _claim(store, call)
    store.add_app("other", scopes=("read", "claim"))
    status, envelope = await call(
        "handoff_request", token, lease_id=lease_id, reason="context_full", to="app:other"
    )
    assert status == 200, envelope
    handoff_id = envelope["data"]["handoff_id"]
    assert store.leases[lease_id]["state"] == "handoff_pending"
    revision = store.tickets[ticket_id]["revision"]

    store.handoffs[handoff_id]["expires_at"] = now() - timedelta(seconds=1)
    result = await sweep.sweep_once(environ=FULL)

    assert result["handoffs"] == 1
    assert store.handoffs[handoff_id]["state"] == "expired"
    assert store.leases[lease_id]["state"] == "released"
    assert store.tickets[ticket_id]["status"] == "queued"
    assert store.tickets[ticket_id]["revision"] == revision + 1
    released = _collab_events(store, ticket_id, "released")
    assert len(released) == 1
    assert released[0]["detail"]["reason"] == "handoff_expired"
    assert released[0]["actor"] == f"app:{APP}"


# ---------------------------------------------------------------------------
# 8. Revoke releases at once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_releases_leases_and_kills_token(store, call, monkeypatch):
    token, ticket_id, lease_id = await _claim(store, call)
    revision = store.tickets[ticket_id]["revision"]
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "fake-operator-credential")

    async def resolve_user() -> str:
        return USER

    status, envelope = await app_svc.admin_invoke(
        "revoke",
        headers={"x-service-token": "fake-operator-credential"},
        client_host="127.0.0.1",
        resolve_user=resolve_user,
        app_id=APP,
    )

    assert status == 200, envelope
    assert envelope["data"]["released_leases"] == 1
    assert envelope["data"]["revoked"] is True
    assert store.leases[lease_id]["state"] == "released"
    assert store.leases[lease_id]["end_reason"] == "revoked"
    assert store.tickets[ticket_id]["status"] == "queued"
    assert store.tickets[ticket_id]["revision"] == revision + 1
    released = _collab_events(store, ticket_id, "released")
    assert len(released) == 1
    assert released[0]["detail"]["reason"] == "revoked"
    assert released[0]["actor"] == f"app:{APP}"
    assert "token" not in envelope["data"]

    status, envelope = await call("whoami", token)
    assert status == 401
    assert envelope["code"] == "unauthorized"

    status, envelope = await call("task_heartbeat", token, lease_id=lease_id)
    assert status == 401


# ---------------------------------------------------------------------------
# 9. CollabLeaseSweeper
# ---------------------------------------------------------------------------


def _count_sweeps(monkeypatch) -> list[int]:
    calls: list[int] = []

    async def fake_sweep_once(*, environ=None):
        calls.append(1)
        return {"expired": 0, "handoffs": 0, "reviews": 0, "mode_expired": 0}

    monkeypatch.setattr(sweep, "sweep_once", fake_sweep_once)
    return calls


@pytest.mark.asyncio
async def test_sweeper_in_off_mode_runs_one_pass_then_stops(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", "off")
    calls = _count_sweeps(monkeypatch)
    sweeper = sweep.CollabLeaseSweeper(interval=0.01)

    sweeper.start()
    task = sweeper._task
    assert task is not None
    await asyncio.wait_for(task, timeout=2)

    assert task.done() and not task.cancelled()
    assert task.exception() is None
    assert len(calls) == 1
    assert sweeper.running is False
    await sweeper.stop()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_sweeper_in_full_mode_loops_until_stopped(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", "full")
    calls = _count_sweeps(monkeypatch)
    sweeper = sweep.CollabLeaseSweeper(interval=0.01)

    sweeper.start()
    task = sweeper._task
    sweeper.start()  # idempotent while running
    assert sweeper._task is task

    async def wait_for_passes() -> None:
        while len(calls) < 3:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait_for_passes(), timeout=2)
    assert sweeper.running is True

    await sweeper.stop()

    assert task is not None and task.done()
    assert task.cancelled()
    assert sweeper.running is False
    assert sweeper._task is None
    seen = len(calls)
    await asyncio.sleep(0.05)
    assert len(calls) == seen


# ---------------------------------------------------------------------------
# 10. install_push_guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_push_guard_outside_a_repo_returns_false(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    # Fence git in: no discovery above tmp_path, no global or system config.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(name, raising=False)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    before = sorted(p.name for p in tmp_path.iterdir())

    guarded = await lease_svc.install_push_guard(worktree)

    assert guarded is False
    assert list(worktree.iterdir()) == []
    assert not (tmp_path / ".wt.collab-hooks").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == before


@pytest.mark.asyncio
async def test_install_push_guard_missing_directory_returns_false(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    missing = Path(tmp_path) / "missing"

    assert await lease_svc.install_push_guard(missing) is False
    assert not missing.exists()
    assert not (tmp_path / ".missing.collab-hooks").exists()


@pytest.mark.asyncio
async def test_heartbeat_audit_shares_the_lease_transaction(store, call):
    token, _ticket_id, lease_id = await _claim(store, call)
    before = store.leases[lease_id]["heartbeat_at"]
    store.fail_audit_for = {"ok"}
    status, envelope = await call("task_heartbeat", token, lease_id=lease_id)
    assert (status, envelope["code"]) == (500, "internal_error")
    # The renewal rolled back with the audit row that could not be written.
    assert store.leases[lease_id]["heartbeat_at"] == before
    store.fail_audit_for = set()
    status, envelope = await call("task_heartbeat", token, lease_id=lease_id)
    assert status == 200
    assert store.audit[-1]["in_transaction"] is True


@pytest.mark.asyncio
async def test_off_schema_answer_rolls_the_write_back(store, call, monkeypatch):
    from src.server.services.collab import leases as lease_svc

    token, ticket_id, lease_id = await _claim(store, call)
    monkeypatch.setattr(lease_svc, "lease_state", lambda row: {"lease_id": str(row["lease_id"])})
    status, envelope = await call("task_release", token, lease_id=lease_id, reason="cannot_do")
    assert (status, envelope["code"]) == (500, "internal_error")
    assert store.leases[lease_id]["state"] == "active"
    assert store.tickets[ticket_id]["status"] == "running"
    assert store.audit_codes()[-1] == "internal_error"
