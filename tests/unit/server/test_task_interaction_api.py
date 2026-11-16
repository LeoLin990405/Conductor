"""HTTP create/track/close stays on the task object."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import create_test_app

USER_ID = "test-user-123"
TICKET_ID = "66666666-6666-4666-8666-666666666666"
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def client():
    from src.server.app.autonomy import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


def _ticket(**overrides) -> dict:
    ticket = {
        "ticket_id": TICKET_ID,
        "user_id": USER_ID,
        "workspace_id": "w1",
        "title": "Fix the save button",
        "scope": "Change the label",
        "status": "running",
        "mandate": {},
        "origin_trigger_id": None,
        "origin_thread_key": "manual:task",
        "router_thread_id": None,
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-1",
        "worker_provider": "",
        "worker_model": "",
        "worker_daemon_id": None,
        "attempts": 1,
        "attention": {"question": "Which branch?"},
        "summary": "",
        "error": "",
        "created_at": NOW - timedelta(minutes=5),
        "updated_at": NOW,
        "started_at": NOW - timedelta(minutes=4),
        "ended_at": None,
        "closed_at": None,
        "close_reason": "",
    }
    ticket.update(overrides)
    return ticket


def _feature_on():
    return patch(
        "src.server.app.autonomy.features.user_feature_enabled",
        new=AsyncMock(return_value=True),
    )


@pytest.mark.asyncio
async def test_activity_is_the_task_record(client):
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_ticket(worker_thread_id="thread-secret")),
        ),
        patch(
            "src.server.app.autonomy.tickets_db.list_inputs",
            new=AsyncMock(
                return_value=[
                    {
                        "input_id": "i1",
                        "kind": "instruction",
                        "text": "Use the dev branch",
                        "created_at": NOW - timedelta(minutes=1),
                    }
                ]
            ),
        ),
        patch(
            "src.server.app.autonomy.tickets_db.list_reports",
            new=AsyncMock(return_value=[]),
        ),
    ):
        resp = await client.get(f"/api/v1/autonomy/tickets/{TICKET_ID}/activity")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticket_id"] == TICKET_ID
    assert [event["kind"] for event in body["events"]] == ["created", "instruction"]
    assert "thread-secret" not in resp.text
    assert "run-1" not in resp.text


@pytest.mark.asyncio
async def test_close_stops_open_work_and_records_the_task(client):
    updated = _ticket(
        status="cancelled",
        attention=None,
        ended_at=NOW,
        closed_at=NOW,
        close_reason="Signed off",
    )
    update = AsyncMock(return_value=updated)
    cancel = AsyncMock(return_value=True)
    dispatcher = MagicMock()
    dispatcher.cancel_thread = cancel
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_ticket()),
        ),
        patch("src.server.app.autonomy.tickets_db.update_ticket", update),
        patch(
            "src.server.services.projects.dispatch.ProjectDispatcher",
            return_value=dispatcher,
        ),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/close",
            json={"reason": "  Signed off  "},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert resp.json()["close_reason"] == "Signed off"
    kwargs = update.await_args.kwargs
    assert kwargs["expected_status"] == "running"
    assert kwargs["status"] == "cancelled"
    assert kwargs["close_reason"] == "Signed off"
    assert kwargs["attention"] is None
    cancel.assert_awaited_once()
    assert cancel.await_args.kwargs["thread_id"] == "thread-1"
    # The stored run id is not the cancel target. A reply leaves it pointing
    # at the finished run, and cancelling that id reports already_finished
    # while the new run continues.
    assert cancel.await_args.kwargs["run_id"] is None


@pytest.mark.asyncio
async def test_close_does_not_stamp_closed_when_cancellation_fails_and_retries(client):
    """A failed stop must not persist closed_at, or the next close skips it."""
    order: list[str] = []
    attempts = {"n": 0}

    async def cancel(**kwargs):
        attempts["n"] += 1
        order.append(f"cancel-{attempts['n']}")
        assert kwargs["thread_id"] == "thread-1"
        return attempts["n"] >= 2

    async def update(*_args, **kwargs):
        order.append("update")
        return _ticket(
            status="cancelled",
            attention=None,
            ended_at=kwargs["ended_at"],
            closed_at=kwargs["closed_at"],
            close_reason=kwargs["close_reason"],
        )

    dispatcher = MagicMock()
    dispatcher.cancel_thread = cancel
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_ticket()),
        ),
        patch("src.server.app.autonomy.tickets_db.update_ticket", update),
        patch(
            "src.server.services.projects.dispatch.ProjectDispatcher",
            return_value=dispatcher,
        ),
    ):
        first = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/close",
            json={"reason": "Stop"},
        )
        second = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/close",
            json={"reason": "Stop"},
        )
    assert first.status_code == 503
    assert first.json()["detail"] == "Could not stop execution; the task is still open"
    assert second.status_code == 200
    assert second.json()["status"] == "cancelled"
    assert second.json()["close_reason"] == "Stop"
    assert order == ["cancel-1", "cancel-2", "update"]


@pytest.mark.asyncio
async def test_close_of_a_completed_task_does_not_rewrite_the_outcome(client):
    completed = _ticket(status="completed", ended_at=NOW, worker_thread_id="thread-1")
    updated = {**completed, "closed_at": NOW, "close_reason": "Looks good"}
    update = AsyncMock(return_value=updated)
    dispatcher = MagicMock()
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=completed),
        ),
        patch("src.server.app.autonomy.tickets_db.update_ticket", update),
        patch(
            "src.server.services.projects.dispatch.ProjectDispatcher",
            return_value=dispatcher,
        ) as dispatcher_cls,
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/close",
            json={"reason": "Looks good"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
    assert "status" not in update.await_args.kwargs
    assert update.await_args.kwargs["close_reason"] == "Looks good"
    dispatcher_cls.assert_not_called()
    dispatcher.cancel_thread.assert_not_called()


@pytest.mark.asyncio
async def test_close_is_idempotent(client):
    closed = _ticket(status="cancelled", closed_at=NOW, close_reason="Signed off")
    update = AsyncMock()
    cancel = AsyncMock(return_value=True)
    dispatcher = MagicMock()
    dispatcher.cancel_thread = cancel
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=closed),
        ),
        patch("src.server.app.autonomy.tickets_db.update_ticket", update),
        patch(
            "src.server.services.projects.dispatch.ProjectDispatcher",
            return_value=dispatcher,
        ),
    ):
        resp = await client.post(f"/api/v1/autonomy/tickets/{TICKET_ID}/close", json={})
    assert resp.status_code == 200
    assert resp.json()["close_reason"] == "Signed off"
    update.assert_not_awaited()
    cancel.assert_awaited_once()
    assert cancel.await_args.kwargs["run_id"] is None
    assert cancel.await_args.kwargs["thread_id"] == "thread-1"


@pytest.mark.asyncio
async def test_close_cancels_the_live_run_not_the_stored_one(client):
    """A resumed task keeps worker_run_id on the finished original run."""
    resumed = _ticket(status="running", worker_run_id="run-finished")
    update = AsyncMock(
        return_value={
            **resumed,
            "status": "cancelled",
            "closed_at": NOW,
            "attention": None,
        }
    )
    cancel = AsyncMock(return_value=True)
    dispatcher = MagicMock()
    dispatcher.cancel_thread = cancel
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=resumed),
        ),
        patch("src.server.app.autonomy.tickets_db.update_ticket", update),
        patch(
            "src.server.services.projects.dispatch.ProjectDispatcher",
            return_value=dispatcher,
        ),
    ):
        resp = await client.post(f"/api/v1/autonomy/tickets/{TICKET_ID}/close", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    cancel.assert_awaited_once()
    assert cancel.await_args.kwargs["thread_id"] == "thread-1"
    assert cancel.await_args.kwargs["run_id"] is None
    assert "run-finished" not in repr(cancel.await_args.kwargs)


@pytest.mark.asyncio
async def test_second_close_stops_a_run_that_started_after_the_stamp(client):
    """Further closes must not skip the stop just because closed_at is set."""
    closed = _ticket(
        status="cancelled",
        closed_at=NOW,
        close_reason="Signed off",
        worker_run_id="run-finished",
    )
    update = AsyncMock()
    attempts = {"n": 0}

    async def cancel(**kwargs):
        attempts["n"] += 1
        assert kwargs["thread_id"] == "thread-1"
        assert kwargs["run_id"] is None
        return attempts["n"] >= 2

    dispatcher = MagicMock()
    dispatcher.cancel_thread = cancel
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=closed),
        ),
        patch("src.server.app.autonomy.tickets_db.update_ticket", update),
        patch(
            "src.server.services.projects.dispatch.ProjectDispatcher",
            return_value=dispatcher,
        ),
    ):
        first = await client.post(f"/api/v1/autonomy/tickets/{TICKET_ID}/close", json={})
        second = await client.post(f"/api/v1/autonomy/tickets/{TICKET_ID}/close", json={})
    assert first.status_code == 503
    assert first.json()["detail"] == "Could not stop execution"
    assert second.status_code == 200
    assert second.json()["close_reason"] == "Signed off"
    update.assert_not_awaited()
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_close_hides_another_users_task(client):
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_ticket(user_id="someone-else")),
        ),
    ):
        resp = await client.post(f"/api/v1/autonomy/tickets/{TICKET_ID}/close", json={})
    assert resp.status_code == 404
