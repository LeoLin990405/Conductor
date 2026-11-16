"""Switch, handoff, resume, and reassign refuse an unisolated cloud worker.

A configured local daemon is the allowed case: it stays pull-only and is not
failed closed. Each cloud path passes ``_CLOUD_WORKER_REJECTION_ERROR`` and
does not call ``ProjectDispatcher``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from src.server.app import autonomy as api
from src.server.models.autonomy import TicketReplyRequest
from src.server.services.autonomy import inputs, worker
from src.server.services.autonomy.effects import PermanentEffectError
from src.server.services.autonomy.host import AutonomyToolHost

_ERROR = worker._CLOUD_WORKER_REJECTION_ERROR


def _cloud(**overrides):
    ticket = {
        "ticket_id": "ticket-1",
        "user_id": "user-1",
        "workspace_id": "ws-1",
        "status": "queued",
        "revision": 4,
        "worker_daemon_id": None,
        "worker_thread_id": None,
        "worker_run_id": None,
        "worker_provider": "codex",
        "worker_model": "gpt-x",
        "mandate": {},
    }
    ticket.update(overrides)
    return ticket


def _daemon(**overrides):
    return _cloud(worker_daemon_id="daemon-1", **overrides)


@pytest.mark.asyncio
async def test_switch_refuses_an_unisolated_cloud_worker(monkeypatch):
    row = _cloud(status="running", worker_thread_id="old-thread", worker_run_id="run-1")
    switched = _cloud(
        status="queued", worker_thread_id=None, worker_run_id=None, revision=5
    )
    recorded = {}
    cancelled = []

    async def record_switch(_ticket_id, **kwargs):
        recorded.update(kwargs)
        return switched

    reject = AsyncMock(
        return_value={
            "ticket_id": "ticket-1",
            "status": "failed",
            "error": _ERROR,
            "revision": 6,
        }
    )
    monkeypatch.setattr(api, "_require_feature", AsyncMock())
    monkeypatch.setattr(api, "_ticket_view", lambda ticket: ticket)
    monkeypatch.setattr(api.tickets_db, "get_ticket", AsyncMock(return_value=row))
    monkeypatch.setattr(api.tickets_db, "record_switch", record_switch)
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", reject)

    class _Dispatcher:
        async def cancel_thread(self, **kwargs):
            cancelled.append(kwargs)
            return True

    monkeypatch.setattr(
        "src.server.services.projects.dispatch.ProjectDispatcher", _Dispatcher
    )

    result = await api.switch_ticket(
        UUID("00000000-0000-0000-0000-000000000001"),
        api.SwitchRequest(to_provider="grok", to_model="g4"),
        "user-1",
    )

    assert recorded["launch_effect"] is None
    assert result["status"] == "failed"
    assert result["error"] == _ERROR
    reject.assert_awaited_once()
    assert reject.await_args.kwargs["error"] == _ERROR
    assert reject.await_args.args[0]["revision"] == 5
    assert cancelled[0]["thread_id"] == "old-thread"
    assert cancelled[0]["run_id"] == "run-1"


@pytest.mark.asyncio
async def test_switch_allows_a_daemon_worker(monkeypatch):
    row = _daemon(
        status="running", worker_thread_id="old-thread", worker_run_id="run-1"
    )
    recorded = {}

    async def record_switch(_ticket_id, **kwargs):
        recorded.update(kwargs)
        return {**row, "worker_provider": "codex", "worker_model": "gpt-y"}

    reject = AsyncMock()
    cancelled = []

    class _Dispatcher:
        async def cancel_thread(self, **kwargs):
            cancelled.append(kwargs)
            return True

    monkeypatch.setattr(api, "_require_feature", AsyncMock())
    monkeypatch.setattr(api, "_ticket_view", lambda ticket: ticket)
    monkeypatch.setattr(api.tickets_db, "get_ticket", AsyncMock(return_value=row))
    monkeypatch.setattr(api.tickets_db, "record_switch", record_switch)
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", reject)
    monkeypatch.setattr(
        "src.server.services.projects.dispatch.ProjectDispatcher", _Dispatcher
    )

    await api.switch_ticket(
        UUID("00000000-0000-0000-0000-000000000001"),
        api.SwitchRequest(to_provider="codex", to_model="gpt-y"),
        "user-1",
    )

    assert recorded["launch_effect"] == ("user-1", "launch_worker")
    reject.assert_not_awaited()
    assert cancelled[0]["thread_id"] == "old-thread"


@pytest.mark.asyncio
async def test_handoff_refuses_an_unisolated_cloud_worker(monkeypatch):
    ticket = _cloud(mandate={"handoff": "switched context"})
    reject = AsyncMock(return_value={"ticket_id": "ticket-1", "status": "failed"})
    clear = AsyncMock()
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", reject)
    monkeypatch.setattr(worker, "clear_handoff", clear)

    delivered = await worker.deliver_staged_handoff(ticket, "do the work")

    assert delivered is None
    clear.assert_not_awaited()
    reject.assert_awaited_once()
    assert reject.await_args.kwargs["error"] == _ERROR


@pytest.mark.asyncio
async def test_handoff_allows_a_daemon_worker(monkeypatch):
    ticket = _daemon(mandate={"handoff": "switched context"})
    reject = AsyncMock()
    clear = AsyncMock()
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", reject)
    monkeypatch.setattr(worker, "clear_handoff", clear)

    delivered = await worker.deliver_staged_handoff(ticket, "do the work")

    assert delivered is not None
    assert delivered.startswith("switched context\n\n---\n\n")
    clear.assert_awaited_once_with("ticket-1", "switched context")
    reject.assert_not_awaited()


@pytest.mark.asyncio
async def test_launch_with_staged_handoff_does_not_dispatch(monkeypatch):
    ticket = _cloud(mandate={"handoff": "carry this"})
    reject = AsyncMock(return_value={"ticket_id": "ticket-1", "status": "failed"})
    clear = AsyncMock()
    monkeypatch.setattr(worker.ticket_db, "get_ticket", AsyncMock(return_value=ticket))
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", reject)
    monkeypatch.setattr(worker, "clear_handoff", clear)
    dispatcher = AsyncMock()
    monkeypatch.setattr(worker, "ProjectDispatcher", dispatcher)

    await worker.launch_worker_effect(
        SimpleNamespace(payload={"ticket_id": "ticket-1"})
    )

    reject.assert_awaited_once()
    assert reject.await_args.kwargs["error"] == _ERROR
    clear.assert_not_awaited()
    dispatcher.assert_not_called()


@pytest.mark.asyncio
async def test_resume_input_refuses_an_unisolated_cloud_worker(monkeypatch):
    ticket = _cloud(
        status="running", worker_thread_id="legacy-thread", worker_run_id=None
    )
    reject = AsyncMock(return_value={"ticket_id": "ticket-1", "status": "failed"})
    mark = AsyncMock()
    monkeypatch.setattr(
        inputs.ticket_db,
        "get_input",
        AsyncMock(return_value={"input_id": "in-1", "text": "go"}),
    )
    monkeypatch.setattr(inputs.ticket_db, "get_ticket", AsyncMock(return_value=ticket))
    monkeypatch.setattr(inputs.ticket_db, "update_ticket", mark)
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", reject)
    dispatcher = AsyncMock()
    monkeypatch.setattr(
        "src.server.services.projects.dispatch.ProjectDispatcher", dispatcher
    )

    with pytest.raises(PermanentEffectError, match="unisolated resume is disabled"):
        await inputs.deliver_input_effect(
            SimpleNamespace(payload={"input_id": "in-1", "ticket_id": "ticket-1"})
        )

    reject.assert_awaited_once()
    assert reject.await_args.kwargs["error"] == _ERROR
    mark.assert_not_awaited()
    dispatcher.assert_not_called()


@pytest.mark.asyncio
async def test_resume_input_allows_a_daemon_worker(monkeypatch):
    ticket = _daemon(status="waiting", worker_thread_id="legacy-thread")
    reject = AsyncMock()
    mark = AsyncMock()
    monkeypatch.setattr(
        inputs.ticket_db,
        "get_input",
        AsyncMock(return_value={"input_id": "in-1", "text": "go"}),
    )
    monkeypatch.setattr(inputs.ticket_db, "get_ticket", AsyncMock(return_value=ticket))
    monkeypatch.setattr(inputs.ticket_db, "update_ticket", mark)
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", reject)

    await inputs.deliver_input_effect(
        SimpleNamespace(payload={"input_id": "in-1", "ticket_id": "ticket-1"})
    )

    reject.assert_not_awaited()
    mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_resume_refuses_an_unisolated_cloud_worker(monkeypatch):
    """add_input bumps revision, so a refusal of the pre-insert snapshot misses."""
    state = {
        "ticket": _cloud(
            status="running",
            worker_thread_id="legacy-thread",
            worker_run_id=None,
            revision=4,
        )
    }

    async def get_ticket(_ticket_id):
        return dict(state["ticket"])

    async def add_input(**_kwargs):
        state["ticket"]["revision"] += 1
        return {"input_id": "in-1"}

    async def reject(ticket, *, error):
        if ticket.get("revision") != state["ticket"]["revision"]:
            return None
        state["ticket"] = {
            **state["ticket"],
            "status": "failed",
            "error": error,
            "revision": state["ticket"]["revision"] + 1,
        }
        return dict(state["ticket"])

    mark = AsyncMock()
    monkeypatch.setattr(api, "_require_feature", AsyncMock())
    monkeypatch.setattr(api.tickets_db, "get_ticket", get_ticket)
    monkeypatch.setattr(api.tickets_db, "add_input", add_input)
    monkeypatch.setattr(
        "src.server.services.autonomy.inputs.mark_cloud_input_for_review", mark
    )
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", reject)
    dispatcher = AsyncMock()
    monkeypatch.setattr(
        "src.server.services.projects.dispatch.ProjectDispatcher", dispatcher
    )

    result = await api.reply_ticket(
        UUID("00000000-0000-0000-0000-000000000001"),
        TicketReplyRequest(text="continue"),
        "user-1",
    )

    assert result["status"] == "failed"
    assert result["ticket_status"] == "failed"
    assert result["error"] == _ERROR
    assert state["ticket"]["status"] == "failed"
    mark.assert_not_awaited()
    dispatcher.assert_not_called()


@pytest.mark.asyncio
async def test_reply_resume_allows_a_daemon_worker(monkeypatch):
    ticket = _daemon(status="waiting", worker_thread_id="legacy-thread")
    reject = AsyncMock()
    monkeypatch.setattr(api, "_require_feature", AsyncMock())
    monkeypatch.setattr(api.tickets_db, "get_ticket", AsyncMock(return_value=ticket))
    monkeypatch.setattr(
        api.tickets_db, "add_input", AsyncMock(return_value={"input_id": "in-1"})
    )
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", reject)

    result = await api.reply_ticket(
        UUID("00000000-0000-0000-0000-000000000001"),
        TicketReplyRequest(text="continue"),
        "user-1",
    )

    assert result == {"input_id": "in-1", "status": "queued_for_daemon"}
    reject.assert_not_awaited()


@pytest.mark.asyncio
async def test_reassign_refuses_an_unisolated_cloud_worker():
    """move_undelivered_inputs bumps revision, so the pre-move snapshot misses."""
    state = {"revision": 8, "status": "queued"}
    seen: list[int] = []

    async def get_ticket(ticket_id, **_kwargs):
        if ticket_id == "from-1":
            return _cloud(ticket_id="from-1")
        return _cloud(
            ticket_id="to-1",
            status=state["status"],
            revision=state["revision"],
        )

    async def move(**_kwargs):
        state["revision"] += 1
        return [{"input_id": "in-1", "trigger_id": "trg-1"}]

    async def reject(ticket, *, error):
        seen.append(ticket.get("revision"))
        if ticket.get("revision") != state["revision"]:
            return None
        state["status"] = "failed"
        state["revision"] += 1
        return _cloud(
            ticket_id="to-1",
            status="failed",
            revision=state["revision"],
            error=error,
        )

    enqueue = AsyncMock()
    mark = AsyncMock()
    with (
        patch(
            "src.server.services.autonomy.host.ticket_db.thread_routing_lock", _lock()
        ),
        patch("src.server.services.autonomy.host.get_db_connection", _connection()),
        patch(
            "src.server.services.autonomy.host.ticket_db.get_ticket",
            get_ticket,
        ),
        patch(
            "src.server.services.autonomy.host.ticket_db.end_subscription",
            AsyncMock(return_value={"ticket_id": "from-1"}),
        ),
        patch(
            "src.server.services.autonomy.host.ticket_db.add_subscription", AsyncMock()
        ),
        patch(
            "src.server.services.autonomy.host.ticket_db.move_undelivered_inputs",
            move,
        ),
        patch(
            "src.server.services.autonomy.host.ticket_db.latest_report",
            AsyncMock(return_value=None),
        ),
        patch(
            "src.server.services.autonomy.host.ticket_db.record_handoff",
            AsyncMock(return_value={"handoff_id": "h-1"}),
        ),
        patch("src.server.services.autonomy.host.enqueue_effect", enqueue),
        patch(
            "src.server.services.autonomy.worker.ticket_db.fail_unisolated_cloud_worker",
            reject,
        ),
        patch(
            "src.server.services.autonomy.inputs.mark_cloud_input_for_review",
            mark,
        ),
    ):
        result = await AutonomyToolHost(user_id="user-1", thread_id="router").reassign(
            thread_key="thread-1",
            from_ticket_id="from-1",
            to_ticket_id="to-1",
        )

    assert result["decision"] == "reassigned"
    assert result["input_delivery"] == "refused"
    assert seen == [9]
    assert state["status"] == "failed"
    enqueue.assert_not_awaited()
    mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_reassign_allows_a_daemon_worker():
    reject = AsyncMock()
    enqueue = AsyncMock()
    target = _daemon(ticket_id="to-1", status="queued", revision=8)
    with (
        patch(
            "src.server.services.autonomy.host.ticket_db.thread_routing_lock", _lock()
        ),
        patch("src.server.services.autonomy.host.get_db_connection", _connection()),
        patch(
            "src.server.services.autonomy.host.ticket_db.get_ticket",
            AsyncMock(side_effect=[_cloud(ticket_id="from-1"), target, target]),
        ),
        patch(
            "src.server.services.autonomy.host.ticket_db.end_subscription",
            AsyncMock(return_value={"ticket_id": "from-1"}),
        ),
        patch(
            "src.server.services.autonomy.host.ticket_db.add_subscription", AsyncMock()
        ),
        patch(
            "src.server.services.autonomy.host.ticket_db.move_undelivered_inputs",
            AsyncMock(return_value=[{"input_id": "in-1", "trigger_id": "trg-1"}]),
        ),
        patch(
            "src.server.services.autonomy.host.ticket_db.latest_report",
            AsyncMock(return_value=None),
        ),
        patch(
            "src.server.services.autonomy.host.ticket_db.record_handoff",
            AsyncMock(return_value={"handoff_id": "h-1"}),
        ),
        patch("src.server.services.autonomy.host.enqueue_effect", enqueue),
        patch(
            "src.server.services.autonomy.worker.ticket_db.fail_unisolated_cloud_worker",
            reject,
        ),
    ):
        result = await AutonomyToolHost(user_id="user-1", thread_id="router").reassign(
            thread_key="thread-1",
            from_ticket_id="from-1",
            to_ticket_id="to-1",
        )

    assert result["decision"] == "reassigned"
    assert result["moved_inputs"] == 1
    assert result["input_delivery"] == "queued_for_daemon"
    enqueue.assert_not_awaited()
    reject.assert_not_awaited()


@pytest.mark.asyncio
async def test_switch_retries_cloud_refusal_when_revision_moves(monkeypatch):
    state = {
        "row": _cloud(
            status="running",
            worker_thread_id="old-thread",
            worker_run_id="run-1",
            revision=4,
        )
    }
    attempts: list[int] = []

    async def get_ticket(_ticket_id):
        return dict(state["row"])

    async def record_switch(_ticket_id, **_kwargs):
        snapshot = _cloud(
            status="queued",
            worker_thread_id=None,
            worker_run_id=None,
            revision=5,
            worker_provider="grok",
            worker_model="g4",
        )
        state["row"] = {**snapshot, "revision": 6}
        return snapshot

    async def reject(ticket, *, error):
        attempts.append(ticket.get("revision"))
        if ticket.get("revision") != state["row"]["revision"]:
            return None
        state["row"] = {
            **state["row"],
            "status": "failed",
            "error": error,
            "revision": state["row"]["revision"] + 1,
        }
        return dict(state["row"])

    class _Dispatcher:
        async def cancel_thread(self, **_kwargs):
            return True

    monkeypatch.setattr(api, "_require_feature", AsyncMock())
    monkeypatch.setattr(api, "_ticket_view", lambda ticket: ticket)
    monkeypatch.setattr(api.tickets_db, "get_ticket", get_ticket)
    monkeypatch.setattr(api.tickets_db, "record_switch", record_switch)
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", reject)
    monkeypatch.setattr(
        "src.server.services.projects.dispatch.ProjectDispatcher", _Dispatcher
    )

    result = await api.switch_ticket(
        UUID("00000000-0000-0000-0000-000000000001"),
        api.SwitchRequest(to_provider="grok", to_model="g4"),
        "user-1",
    )

    assert attempts == [5, 6]
    assert result["status"] == "failed"
    assert state["row"]["status"] == "failed"


@pytest.mark.asyncio
async def test_switch_fails_the_request_when_cloud_refusal_keeps_racing(monkeypatch):
    from fastapi import HTTPException

    row = _cloud(
        status="running",
        worker_thread_id="old-thread",
        worker_run_id="run-1",
        revision=4,
    )
    switched = _cloud(
        status="queued", worker_thread_id=None, worker_run_id=None, revision=5
    )
    reject = AsyncMock(return_value=None)
    enqueued = []

    async def enqueue(**kwargs):
        enqueued.append(kwargs)
        return True

    class _Dispatcher:
        async def cancel_thread(self, **_kwargs):
            return True

    monkeypatch.setattr(api, "_require_feature", AsyncMock())
    monkeypatch.setattr(api, "_ticket_view", lambda ticket: ticket)
    monkeypatch.setattr(api.tickets_db, "get_ticket", AsyncMock(return_value=switched))
    monkeypatch.setattr(api.tickets_db, "record_switch", AsyncMock(return_value=switched))
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", reject)
    monkeypatch.setattr(
        "src.server.database.autonomy.effects.enqueue_effect", enqueue
    )
    monkeypatch.setattr(
        "src.server.services.projects.dispatch.ProjectDispatcher", _Dispatcher
    )
    # The first read is the running row; later reads are the switched row.
    api.tickets_db.get_ticket = AsyncMock(side_effect=[row, switched, switched, switched])

    with pytest.raises(HTTPException) as raised:
        await api.switch_ticket(
            UUID("00000000-0000-0000-0000-000000000001"),
            api.SwitchRequest(to_provider="grok", to_model="g4"),
            "user-1",
        )

    assert raised.value.status_code == 503
    assert reject.await_count == 3
    assert enqueued and enqueued[0]["kind"] == "launch_worker"


@pytest.mark.asyncio
async def test_reassign_flags_moved_inputs_when_the_cloud_target_has_a_run():
    state = {"revision": 8}

    async def get_ticket(ticket_id, **_kwargs):
        if ticket_id == "from-1":
            return _cloud(ticket_id="from-1")
        return _cloud(
            ticket_id="to-1",
            status="running",
            worker_thread_id="live-thread",
            worker_run_id="run-9",
            revision=state["revision"],
        )

    async def move(**_kwargs):
        state["revision"] += 1
        return [{"input_id": "in-9", "trigger_id": "trg-9"}]

    async def reject(ticket, *, error):
        if ticket.get("revision") != state["revision"]:
            return None
        if ticket.get("worker_run_id"):
            return None
        return _cloud(ticket_id="to-1", status="failed", error=error)

    mark = AsyncMock()
    with (
        patch(
            "src.server.services.autonomy.host.ticket_db.thread_routing_lock", _lock()
        ),
        patch("src.server.services.autonomy.host.get_db_connection", _connection()),
        patch("src.server.services.autonomy.host.ticket_db.get_ticket", get_ticket),
        patch(
            "src.server.services.autonomy.host.ticket_db.end_subscription",
            AsyncMock(return_value={"ticket_id": "from-1"}),
        ),
        patch("src.server.services.autonomy.host.ticket_db.add_subscription", AsyncMock()),
        patch(
            "src.server.services.autonomy.host.ticket_db.move_undelivered_inputs",
            move,
        ),
        patch(
            "src.server.services.autonomy.host.ticket_db.latest_report",
            AsyncMock(return_value=None),
        ),
        patch(
            "src.server.services.autonomy.host.ticket_db.record_handoff",
            AsyncMock(return_value={"handoff_id": "h-1"}),
        ),
        patch("src.server.services.autonomy.host.enqueue_effect", AsyncMock()),
        patch(
            "src.server.services.autonomy.worker.ticket_db.fail_unisolated_cloud_worker",
            reject,
        ),
        patch(
            "src.server.services.autonomy.inputs.mark_cloud_input_for_review",
            mark,
        ),
    ):
        result = await AutonomyToolHost(user_id="user-1", thread_id="router").reassign(
            thread_key="thread-1",
            from_ticket_id="from-1",
            to_ticket_id="to-1",
        )

    assert result["input_delivery"] == "stored_for_review"
    mark.assert_awaited_once()
    assert mark.await_args.args[1] == "in-9"


@pytest.mark.asyncio
async def test_daemon_claim_asserts_when_the_handoff_is_not_delivered(monkeypatch):
    ticket = _cloud(mandate={"handoff": "carry this"}, worker_daemon_id=None)
    monkeypatch.setattr(
        api, "_require_daemon", AsyncMock(return_value={"daemon_id": "daemon-1"})
    )
    monkeypatch.setattr(worker, "claim_daemon_ticket", AsyncMock(return_value=ticket))
    monkeypatch.setattr(
        "src.server.database.autonomy.sources.get_source_any_user",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.shared_context.worker_shared_context_text",
        AsyncMock(return_value=""),
    )
    # The claim invariant is "a claimed row is daemon-bound, so delivery
    # cannot return None". Force that None without a database refusal.
    monkeypatch.setattr(
        worker, "refuse_unisolated_cloud_dispatch", AsyncMock(return_value=None)
    )

    with pytest.raises(AssertionError, match="daemon-bound"):
        await api.daemon_claim(x_daemon_token="token")


def _lock():
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock

    @asynccontextmanager
    async def _null(_thread_key, *, timeout_seconds=30.0):
        yield

    return MagicMock(side_effect=_null)


def _connection():
    from contextlib import asynccontextmanager

    class _Tx:
        @asynccontextmanager
        async def transaction(self):
            yield

    @asynccontextmanager
    async def _open():
        yield _Tx()

    return _open
