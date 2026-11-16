"""Daemon follow-up delivery: resume claims, cancel observation, and replies."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import create_test_app

USER_ID = "test-user-123"
DAEMON_ID = "55555555-5555-4555-8555-555555555555"
TICKET_ID = "66666666-6666-4666-8666-666666666666"

TOKEN_HEADERS = {"X-Daemon-Token": "a" * 43}


@pytest_asyncio.fixture
async def client():
    from src.server.app.autonomy import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _daemon_ticket(**overrides) -> dict:
    now = datetime.now(timezone.utc)
    ticket = {
        "ticket_id": TICKET_ID,
        "user_id": USER_ID,
        "workspace_id": "w1",
        "title": "Fix the save button",
        "scope": "Change the label to Save",
        "status": "blocked",
        "mandate": {"strength": "explicit", "source_id": "s1"},
        "origin_trigger_id": None,
        "origin_thread_key": "github:acme/widgets#12",
        "router_thread_id": None,
        "worker_thread_id": None,
        "worker_run_id": None,
        "worker_provider": "codex",
        "worker_model": "",
        "worker_daemon_id": DAEMON_ID,
        "lease_id": "77777777-7777-4777-8777-777777777777",
        "lease_expires_at": now,
        "lease_duration_seconds": 90,
        "attempts": 1,
        "attention": {"question": "Which branch?"},
        "summary": "",
        "error": "",
        "created_at": now,
        "updated_at": now,
        "started_at": now,
        "ended_at": None,
    }
    ticket.update(overrides)
    return ticket


def _feature_on():
    return patch(
        "src.server.app.autonomy.features.user_feature_enabled",
        new=AsyncMock(return_value=True),
    )


@asynccontextmanager
async def _noop_handoff(_ticket_id):
    yield


def _daemon_authed():
    return patch(
        "src.server.app.autonomy._require_daemon",
        new=AsyncMock(return_value={"daemon_id": DAEMON_ID, "user_id": USER_ID}),
    )


@pytest.mark.asyncio
async def test_claim_returns_resume_payload_with_followups(client):
    inputs = [
        {
            "input_id": "i1",
            "kind": "answer",
            "text": "Use the dev branch",
            "created_at": datetime.now(timezone.utc),
        }
    ]
    ticket = {**_daemon_ticket(status="running"), "inputs": inputs}
    calls: list[tuple[str, list]] = []

    def fake_append(prompt: str, delivered: list, **_kwargs) -> str:
        calls.append((prompt, delivered))
        return f"{prompt}\nFOLLOWUPS-MARKER"

    with (
        _daemon_authed(),
        patch(
            "src.server.services.autonomy.worker.claim_daemon_ticket",
            new=AsyncMock(return_value=ticket),
        ),
        patch(
            "src.server.app.autonomy.source_db.get_source_any_user",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.server.services.autonomy.shared_context.worker_shared_context_text",
            new=AsyncMock(return_value=""),
        ),
        patch("src.server.app.autonomy.append_followups", new=fake_append),
    ):
        resp = await client.post(
            "/api/v1/autonomy/daemon/claim", headers=TOKEN_HEADERS
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resume"] is True
    assert "FOLLOWUPS-MARKER" in body["prompt"]
    assert body["ticket"]["ticket_id"] == TICKET_ID
    assert body["ticket"]["worker_provider"] == "codex"
    assert body["ticket"]["speculative"] is False
    assert len(calls) == 1
    base_prompt, delivered = calls[0]
    assert delivered == inputs
    assert "Worker for one Super-agent Ticket" in base_prompt
    assert "Ticket: Fix the save button" in base_prompt


@pytest.mark.asyncio
async def test_claim_without_inputs_is_a_fresh_claim(client):
    calls: list[tuple[str, list]] = []

    def fake_append(prompt: str, delivered: list, **_kwargs) -> str:
        calls.append((prompt, delivered))
        return "SHOULD-NOT-APPEAR"

    with (
        _daemon_authed(),
        patch(
            "src.server.services.autonomy.worker.claim_daemon_ticket",
            new=AsyncMock(return_value=_daemon_ticket(status="running")),
        ),
        patch(
            "src.server.app.autonomy.source_db.get_source_any_user",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.server.services.autonomy.shared_context.worker_shared_context_text",
            new=AsyncMock(return_value=""),
        ),
        patch("src.server.app.autonomy.append_followups", new=fake_append),
    ):
        resp = await client.post(
            "/api/v1/autonomy/daemon/claim", headers=TOKEN_HEADERS
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resume"] is False
    assert "SHOULD-NOT-APPEAR" not in body["prompt"]
    assert "Worker for one Super-agent Ticket" in body["prompt"]
    assert calls == []


@pytest.mark.asyncio
async def test_daemon_ticket_status_reports_status_and_attention(client):
    owned = AsyncMock(return_value=_daemon_ticket(status="running"))
    with (
        _daemon_authed(),
        patch("src.server.database.autonomy.daemons.owned_ticket", owned),
        patch(
            "src.server.database.autonomy.stops.get_stop",
            new=AsyncMock(return_value=None),
        ),
    ):
        resp = await client.get(
            f"/api/v1/autonomy/daemon/tickets/{TICKET_ID}/status",
            headers=TOKEN_HEADERS,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticket_id"] == TICKET_ID
    assert body["status"] == "running"
    assert body["attention"] == {"question": "Which branch?"}
    assert body["stop"] is None
    owned.assert_awaited_once_with(DAEMON_ID, TICKET_ID)


@pytest.mark.asyncio
async def test_daemon_ticket_status_404s_for_a_ticket_it_does_not_own(client):
    owned = AsyncMock(return_value=None)
    with (
        _daemon_authed(),
        patch("src.server.database.autonomy.daemons.owned_ticket", owned),
    ):
        resp = await client.get(
            f"/api/v1/autonomy/daemon/tickets/{TICKET_ID}/status",
            headers=TOKEN_HEADERS,
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Ticket not owned by this daemon"
    owned.assert_awaited_once_with(DAEMON_ID, TICKET_ID)


@pytest.mark.asyncio
async def test_daemon_ticket_status_requires_a_token(client):
    resp = await client.get(f"/api/v1/autonomy/daemon/tickets/{TICKET_ID}/status")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_reply_to_a_daemon_ticket_queues_the_answer(client):
    add_input = AsyncMock(return_value={"input_id": "in1"})
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_daemon_ticket(status="blocked")),
        ),
        patch("src.server.database.autonomy.tickets.add_input", add_input),
        patch("src.server.services.projects.dispatch.ProjectDispatcher") as dispatcher,
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/reply",
            json={"text": "Use the dev branch"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"input_id": "in1", "status": "queued_for_daemon"}
    add_input.assert_awaited_once_with(
        ticket_id=TICKET_ID, trigger_id=None, kind="answer", text="Use the dev branch"
    )
    dispatcher.assert_not_called()


@pytest.mark.asyncio
async def test_reply_without_a_worker_session_queues_on_the_task(client):
    add_input = AsyncMock(return_value={"input_id": "in9"})
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(
                return_value=_daemon_ticket(worker_daemon_id=None, status="queued")
            ),
        ),
        patch("src.server.database.autonomy.tickets.add_input", add_input),
        patch("src.server.services.projects.dispatch.ProjectDispatcher") as dispatcher,
        patch(
            "src.server.services.autonomy.worker._instruction_handoff_lock",
            _noop_handoff,
        ),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/reply",
            json={"text": "Use the dev branch"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"input_id": "in9", "status": "queued_on_task"}
    add_input.assert_awaited_once_with(
        ticket_id=TICKET_ID,
        trigger_id=None,
        kind="instruction",
        text="Use the dev branch",
    )
    dispatcher.assert_not_called()


@pytest.mark.asyncio
async def test_reply_to_a_legacy_cloud_ticket_is_stored_for_review(client):
    ticket = _daemon_ticket(
        status="blocked", worker_thread_id="thread-1", worker_daemon_id=None
    )
    update = AsyncMock(return_value=ticket)
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=ticket),
        ),
        patch(
            "src.server.database.autonomy.tickets.add_input",
            new=AsyncMock(return_value={"input_id": "in2"}),
        ),
        patch(
            "src.server.database.autonomy.tickets.update_ticket", update
        ),
        patch(
            "src.server.services.projects.dispatch.ProjectDispatcher"
        ) as dispatcher_cls,
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/reply",
            json={"text": "Ship it"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"input_id": "in2", "status": "stored_for_review"}
    update.assert_awaited_once()
    args, kwargs = update.call_args
    assert args == (TICKET_ID,)
    assert kwargs["expected_status"] == "blocked"
    assert "status" not in kwargs
    assert kwargs["attention"]["input_id"] == "in2"
    assert "cannot be resumed" in kwargs["attention"]["question"]
    dispatcher_cls.assert_not_called()


@pytest.mark.asyncio
async def test_reply_to_a_cloud_ticket_with_a_finished_run_is_stored_for_review(client):
    """An old cloud run id is not a reason to start another unisolated turn."""
    ticket = _daemon_ticket(
        status="blocked",
        worker_thread_id="thread-1",
        worker_daemon_id=None,
        worker_run_id="run-old",
    )
    update = AsyncMock(return_value=ticket)
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=ticket),
        ),
        patch(
            "src.server.database.autonomy.tickets.add_input",
            new=AsyncMock(return_value={"input_id": "in3"}),
        ),
        patch("src.server.database.autonomy.tickets.update_ticket", update),
        patch("src.server.services.projects.dispatch.ProjectDispatcher") as dispatcher,
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/reply",
            json={"text": "Ship it"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"input_id": "in3", "status": "stored_for_review"}
    dispatcher.assert_not_called()
    update.assert_awaited_once()
    assert update.await_args.kwargs["expected_status"] == "blocked"
    assert "worker_run_id" not in update.await_args.kwargs


@pytest.mark.asyncio
async def test_reply_does_not_start_a_cloud_run_when_the_task_closed(client):
    ticket = _daemon_ticket(
        status="blocked",
        worker_thread_id="thread-1",
        worker_daemon_id=None,
        worker_run_id="run-old",
    )
    closed = {**ticket, "status": "cancelled", "closed_at": datetime.now(timezone.utc)}
    add_input = AsyncMock(return_value={"input_id": "in4"})
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(side_effect=[ticket, closed]),
        ),
        patch("src.server.database.autonomy.tickets.add_input", add_input),
        patch("src.server.services.projects.dispatch.ProjectDispatcher") as dispatcher,
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/reply",
            json={"text": "Ship it"},
        )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Ticket ended"
    add_input.assert_not_awaited()
    dispatcher.assert_not_called()


@pytest.mark.asyncio
async def test_reply_that_missed_the_thread_queues_delivery(client):
    """A threadless read that commits after publication must still be delivered."""
    waiting = _daemon_ticket(worker_daemon_id=None, status="queued")
    published = {**waiting, "status": "running", "worker_thread_id": "thread-new"}
    add_input = AsyncMock(return_value={"input_id": "in-late"})
    enqueue = AsyncMock(return_value=True)
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(side_effect=[waiting, published]),
        ),
        patch("src.server.database.autonomy.tickets.add_input", add_input),
        patch(
            "src.server.services.autonomy.worker._instruction_handoff_lock",
            _noop_handoff,
        ),
        patch("src.server.database.autonomy.effects.enqueue_effect", enqueue),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/reply",
            json={"text": "Use the dev branch"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"input_id": "in-late", "status": "queued_for_delivery"}
    enqueue.assert_awaited_once()
    assert enqueue.await_args.kwargs["idempotency_key"] == "autonomy:input:in-late"
    assert enqueue.await_args.kwargs["kind"] == "deliver_input"
    assert enqueue.await_args.kwargs["payload"] == {
        "ticket_id": TICKET_ID,
        "input_id": "in-late",
    }


@pytest.mark.asyncio
async def test_reply_to_a_running_daemon_ticket_conflicts_to_avoid_a_second_cli(client):
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_daemon_ticket(status="running")),
        ),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/reply",
            json={"text": "Use the dev branch"},
        )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Daemon Ticket is not waiting"
