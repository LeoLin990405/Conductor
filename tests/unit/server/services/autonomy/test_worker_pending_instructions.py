"""Pending instructions survive a full input page and thread creation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.server.database.autonomy.tickets import (
    UNDELIVERED_INSTRUCTION_KINDS,
    _LIST_UNDELIVERED_INSTRUCTIONS,
)
from src.server.services.autonomy import worker


def test_undelivered_instruction_query_skips_delivered_rows_and_other_kinds():
    sql = " ".join(_LIST_UNDELIVERED_INSTRUCTIONS.split())
    assert "delivered_at IS NULL" in sql
    assert "kind = ANY(%s)" in sql
    assert "btrim(COALESCE(text, '')) <> ''" in sql
    assert "ORDER BY created_at" in sql
    assert set(UNDELIVERED_INSTRUCTION_KINDS) == {"answer", "instruction"}


@asynccontextmanager
async def _noop_handoff(_ticket_id):
    yield


def _queued_ticket() -> dict:
    return {
        "ticket_id": "ticket-1",
        "user_id": "u-1",
        "workspace_id": "ws-1",
        "title": "Fix the thing",
        "scope": "Fix it",
        "status": "queued",
        "mandate": {},
        "attempts": 0,
        "worker_provider": "",
        "worker_model": "",
    }


@pytest.mark.asyncio
async def test_instruction_past_the_first_page_is_included_in_the_launch_prompt():
    now = datetime.now(timezone.utc)
    rows = [
        {
            "input_id": f"in-{index:02d}",
            "kind": "instruction",
            "text": f"note {index}",
            "delivered_at": None,
            "created_at": now,
        }
        for index in range(51)
    ]
    seen: list[tuple[int, int]] = []

    async def list_undelivered(ticket_id, *, limit=50, offset=0):
        assert ticket_id == "ticket-1"
        seen.append((limit, offset))
        return rows[offset : offset + limit]

    ticket = {
        "ticket_id": "ticket-1",
        "user_id": "u-1",
        "workspace_id": "ws-1",
        "title": "Fix the thing",
        "scope": "Fix it",
        "status": "running",
        "worker_thread_id": "thread-1",
        "mandate": {},
    }
    dispatch = AsyncMock(return_value=SimpleNamespace(run_id="run-1"))
    mark = AsyncMock()
    with (
        patch(
            "src.server.services.autonomy.worker.ticket_db.get_ticket",
            AsyncMock(return_value=ticket),
        ),
        patch(
            "src.server.services.autonomy.worker.ticket_db.update_ticket",
            AsyncMock(),
        ),
        patch(
            "src.server.services.autonomy.worker.ProjectDispatcher",
            return_value=SimpleNamespace(dispatch_turn=dispatch),
        ),
        patch(
            "src.server.services.autonomy.worker.ticket_db.list_undelivered_instructions",
            list_undelivered,
        ),
        patch(
            "src.server.services.autonomy.worker.ticket_db.mark_input_delivered",
            mark,
        ),
    ):
        prompt, pending_ids = await worker._prompt_with_pending_instructions(
            "ticket-1", "base prompt"
        )
        await worker._start_worker_turn(
            SimpleNamespace(
                dispatch_turn=dispatch,
                cancel_thread=AsyncMock(return_value=True),
            ),
            ticket,
            "ticket-1",
            "thread-1",
            prompt,
            pending_ids,
        )

    prompt = dispatch.await_args.kwargs["prompt"]
    assert "note 0" in prompt
    assert "note 50" in prompt
    marked = [call.args[0] for call in mark.await_args_list]
    assert marked[0] == "in-00"
    assert marked[-1] == "in-50"
    assert len(marked) == 51
    assert seen[0][1] == 0
    assert any(offset > 0 or limit > 50 for limit, offset in seen)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["metadata", "baseline_write"])
async def test_native_git_baseline_metadata_failure_does_not_block_launch(failure: str):
    ticket = {
        "ticket_id": "ticket-native",
        "user_id": "u-1",
        "workspace_id": "ws-1",
        "status": "running",
        "worker_provider": "codex",
        "worker_thread_id": "thread-native",
        "attempts": 1,
        "revision": 3,
        "mandate": {},
    }
    dispatch = AsyncMock(return_value=SimpleNamespace(run_id="run-native"))
    update = AsyncMock(
        side_effect=[None, ticket] if failure == "baseline_write" else [ticket]
    )
    metadata = AsyncMock(
        side_effect=RuntimeError("metadata unavailable")
        if failure == "metadata"
        else None,
        return_value={"native_runtime": {"provider": "codex"}},
    )
    with (
        patch(
            "src.server.services.autonomy.worker.ticket_db.get_ticket",
            AsyncMock(return_value=ticket),
        ),
        patch(
            "src.server.services.autonomy.worker.ticket_db.update_ticket", update
        ),
        patch(
            "src.server.database.conversation.native_runtime.get_thread_metadata",
            metadata,
        ),
        patch(
            "src.server.services.autonomy.worker.ProjectDispatcher",
            return_value=SimpleNamespace(dispatch_turn=dispatch),
        ),
    ):
        started = await worker._start_worker_turn(
            SimpleNamespace(
                dispatch_turn=dispatch,
                cancel_thread=AsyncMock(return_value=True),
            ),
            ticket,
            "ticket-native",
            "thread-native",
            "prompt",
            [],
        )

    assert started is True
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_instruction_accepted_during_thread_creation_queues_delivery():
    """In-memory fake: create_thread accepts an instruction the snapshot missed."""
    stored = [
        {
            "input_id": "early",
            "kind": "instruction",
            "text": "before launch",
            "delivered_at": None,
        }
    ]
    prompts: list[str] = []
    order: list[str] = []

    async def list_undelivered(ticket_id, *, limit=50, offset=0):
        return [dict(row) for row in stored[offset : offset + limit]]

    async def create_thread(**kwargs):
        order.append("create")
        prompts.append(kwargs["first_query"])
        stored.append(
            {
                "input_id": "late",
                "kind": "instruction",
                "text": "arrived during create",
                "delivered_at": None,
            }
        )
        return "thread-new"

    async def update_ticket(*_args, **kwargs):
        order.append("update-thread" if kwargs.get("worker_thread_id") else "update-run")
        return {**_queued_ticket(), "status": "running", "worker_thread_id": "thread-new"}

    enqueued: list[dict] = []

    async def enqueue_effect(**kwargs):
        order.append("enqueue")
        enqueued.append(kwargs)
        return True

    async def dispatch_turn(**kwargs):
        order.append("dispatch")
        prompts.append(kwargs["prompt"])
        return SimpleNamespace(run_id="run-1")

    mark = AsyncMock()
    dispatcher = SimpleNamespace(create_thread=create_thread, dispatch_turn=dispatch_turn)
    with (
        patch(
            "src.server.services.autonomy.worker.ticket_db.list_undelivered_instructions",
            list_undelivered,
        ),
        patch(
            "src.server.services.autonomy.worker.ticket_db.update_ticket",
            update_ticket,
        ),
        patch(
            "src.server.services.autonomy.worker.ticket_db.mark_input_delivered",
            mark,
        ),
        patch(
            "src.server.services.autonomy.worker.ProjectDispatcher",
            return_value=dispatcher,
        ),
        patch(
            "src.server.database.autonomy.effects.enqueue_effect",
            enqueue_effect,
        ),
        patch(
            "src.server.services.autonomy.worker._instruction_handoff_lock",
            _noop_handoff,
        ),
        patch(
            "src.server.services.autonomy.worker.ticket_db.get_ticket",
            AsyncMock(
                return_value={
                    **_queued_ticket(),
                    "status": "running",
                    "worker_thread_id": "thread-new",
                }
            ),
        ),
    ):
        await worker._launch_queued(_queued_ticket(), "ticket-1", "base prompt")

    assert "before launch" in prompts[0]
    assert "arrived during create" not in prompts[0]
    assert "arrived during create" not in prompts[1]
    assert "before launch" in prompts[1]
    mark.assert_awaited_once_with("early")
    assert order == ["create", "update-thread", "enqueue", "dispatch", "update-run"]
    assert enqueued == [
        {
            "user_id": "u-1",
            "kind": "deliver_input",
            "idempotency_key": "autonomy:input:late",
            "payload": {"ticket_id": "ticket-1", "input_id": "late"},
        }
    ]


def _running_view(state: dict) -> dict:
    return {**_queued_ticket(), **state}


@pytest.mark.asyncio
async def test_close_during_thread_creation_does_not_dispatch():
    """Closing while create_thread runs must not be overwritten into a launch."""
    state = {"status": "queued", "closed_at": None, "worker_thread_id": None}
    dispatched = AsyncMock()

    async def create_thread(**_kwargs):
        state["status"] = "cancelled"
        state["closed_at"] = datetime.now(timezone.utc)
        return "thread-new"

    async def update_ticket(_ticket_id, expected_status=None, **kwargs):
        # An unfenced publish overwrites the close. The database only applies
        # the row when the caller fences on the status it read.
        if expected_status is not None and state["status"] != expected_status:
            return None
        state.update(kwargs)
        return _running_view(state)

    cancel = AsyncMock(return_value=True)
    dispatcher = SimpleNamespace(
        create_thread=create_thread,
        dispatch_turn=dispatched,
        cancel_thread=cancel,
    )
    with (
        patch(
            "src.server.services.autonomy.worker.ticket_db.list_undelivered_instructions",
            AsyncMock(return_value=[]),
        ),
        patch(
            "src.server.services.autonomy.worker.ticket_db.update_ticket",
            update_ticket,
        ),
        patch(
            "src.server.services.autonomy.worker.ProjectDispatcher",
            return_value=dispatcher,
        ),
        patch(
            "src.server.services.autonomy.worker._instruction_handoff_lock",
            _noop_handoff,
        ),
    ):
        await worker._launch_queued(_queued_ticket(), "ticket-1", "base prompt")

    dispatched.assert_not_awaited()
    cancel.assert_awaited_once()
    assert cancel.await_args.kwargs["thread_id"] == "thread-new"
    assert cancel.await_args.kwargs["run_id"] is None
    assert state["status"] == "cancelled"
    assert state["worker_thread_id"] is None


@pytest.mark.asyncio
async def test_close_during_dispatch_cancels_the_run_just_started():
    state = {
        "status": "queued",
        "closed_at": None,
        "worker_thread_id": None,
    }

    async def create_thread(**_kwargs):
        return "thread-new"

    async def update_ticket(_ticket_id, expected_status=None, **kwargs):
        if expected_status is not None and state["status"] != expected_status:
            return None
        state.update(kwargs)
        return _running_view(state)

    async def get_ticket(_ticket_id):
        return _running_view(state)

    async def dispatch_turn(**_kwargs):
        state["status"] = "cancelled"
        state["closed_at"] = datetime.now(timezone.utc)
        return SimpleNamespace(run_id="run-new")

    cancel = AsyncMock(return_value=True)
    mark = AsyncMock()
    dispatcher = SimpleNamespace(
        create_thread=create_thread,
        dispatch_turn=dispatch_turn,
        cancel_thread=cancel,
    )
    with (
        patch(
            "src.server.services.autonomy.worker.ticket_db.list_undelivered_instructions",
            AsyncMock(return_value=[]),
        ),
        patch(
            "src.server.services.autonomy.worker.ticket_db.update_ticket",
            update_ticket,
        ),
        patch(
            "src.server.services.autonomy.worker.ticket_db.get_ticket",
            get_ticket,
        ),
        patch(
            "src.server.services.autonomy.worker.ticket_db.mark_input_delivered",
            mark,
        ),
        patch(
            "src.server.services.autonomy.worker.ProjectDispatcher",
            return_value=dispatcher,
        ),
        patch(
            "src.server.services.autonomy.worker._instruction_handoff_lock",
            _noop_handoff,
        ),
    ):
        await worker._launch_queued(_queued_ticket(), "ticket-1", "base prompt")

    cancel.assert_awaited_once()
    assert cancel.await_args.kwargs == {
        "user_id": "u-1",
        "thread_id": "thread-new",
        "run_id": "run-new",
    }
    mark.assert_not_awaited()
    assert state["status"] == "cancelled"
    assert "worker_run_id" not in state


@pytest.mark.asyncio
async def test_closed_launch_retry_stops_the_published_thread():
    now = datetime.now(timezone.utc)
    ticket = {
        **_queued_ticket(),
        "status": "cancelled",
        "closed_at": now,
        "worker_thread_id": "thread-1",
    }
    cancel = AsyncMock(return_value=True)
    dispatch = AsyncMock()
    dispatcher = SimpleNamespace(dispatch_turn=dispatch, cancel_thread=cancel)
    with (
        patch(
            "src.server.services.autonomy.worker.ticket_db.get_ticket",
            AsyncMock(return_value=ticket),
        ),
        patch(
            "src.server.services.autonomy.worker.ProjectDispatcher",
            return_value=dispatcher,
        ),
    ):
        await worker.launch_worker_effect(SimpleNamespace(payload={"ticket_id": "ticket-1"}))

    dispatch.assert_not_awaited()
    cancel.assert_awaited_once()
    assert cancel.await_args.kwargs["thread_id"] == "thread-1"
    assert cancel.await_args.kwargs["run_id"] is None


@pytest.mark.asyncio
async def test_instruction_inserted_as_the_thread_is_published_is_queued():
    """The post-publish snapshot has to see a reply that committed with it."""
    stored = [
        {
            "input_id": "early",
            "kind": "instruction",
            "text": "before launch",
            "delivered_at": None,
        }
    ]
    held = {"on": False}
    saw_late_under_lock = {"ok": False}

    @asynccontextmanager
    async def handoff(_ticket_id):
        held["on"] = True
        try:
            yield
        finally:
            held["on"] = False

    async def list_undelivered(_ticket_id, *, limit=50, offset=0):
        page = [dict(row) for row in stored[offset : offset + limit]]
        if any(row["input_id"] == "late" for row in page):
            saw_late_under_lock["ok"] = held["on"]
        return page

    async def update_ticket(_ticket_id, expected_status=None, **kwargs):
        if kwargs.get("worker_thread_id"):
            stored.append(
                {
                    "input_id": "late",
                    "kind": "instruction",
                    "text": "committed with publication",
                    "delivered_at": None,
                }
            )
        return {
            **_queued_ticket(),
            "status": "running",
            "worker_thread_id": kwargs.get("worker_thread_id") or "thread-new",
        }

    enqueued: list[dict] = []

    async def enqueue_effect(**kwargs):
        enqueued.append(kwargs)
        return True

    async def get_ticket(_ticket_id):
        return {**_queued_ticket(), "status": "running", "worker_thread_id": "thread-new"}

    dispatcher = SimpleNamespace(
        create_thread=AsyncMock(return_value="thread-new"),
        dispatch_turn=AsyncMock(return_value=SimpleNamespace(run_id="run-1")),
        cancel_thread=AsyncMock(return_value=True),
    )
    with (
        patch(
            "src.server.services.autonomy.worker.ticket_db.list_undelivered_instructions",
            list_undelivered,
        ),
        patch(
            "src.server.services.autonomy.worker.ticket_db.update_ticket",
            update_ticket,
        ),
        patch(
            "src.server.services.autonomy.worker.ticket_db.get_ticket",
            get_ticket,
        ),
        patch(
            "src.server.services.autonomy.worker.ticket_db.mark_input_delivered",
            AsyncMock(),
        ),
        patch(
            "src.server.services.autonomy.worker.ProjectDispatcher",
            return_value=dispatcher,
        ),
        patch(
            "src.server.services.autonomy.worker._instruction_handoff_lock",
            handoff,
        ),
        patch(
            "src.server.database.autonomy.effects.enqueue_effect",
            enqueue_effect,
        ),
    ):
        await worker._launch_queued(_queued_ticket(), "ticket-1", "base prompt")

    assert saw_late_under_lock["ok"] is True
    assert [item["idempotency_key"] for item in enqueued] == ["autonomy:input:late"]
    prompt = dispatcher.dispatch_turn.await_args.kwargs["prompt"]
    assert "committed with publication" not in prompt
    assert "before launch" in prompt


@pytest.mark.asyncio
async def test_threadless_reply_after_publish_queues_the_same_delivery_key():
    held = {"on": False}

    @asynccontextmanager
    async def handoff(_ticket_id):
        held["on"] = True
        try:
            yield
        finally:
            held["on"] = False

    inserted = {"under_lock": False}

    async def add_input(**kwargs):
        inserted["under_lock"] = held["on"]
        return {"input_id": "late", **kwargs}

    enqueued: list[str] = []

    async def enqueue_effect(**kwargs):
        enqueued.append(kwargs["idempotency_key"])
        return True

    published = {
        **_queued_ticket(),
        "status": "running",
        "worker_thread_id": "thread-new",
    }
    with (
        patch(
            "src.server.services.autonomy.worker.ticket_db.get_ticket",
            AsyncMock(return_value=published),
        ),
        patch(
            "src.server.services.autonomy.worker.ticket_db.add_input",
            add_input,
        ),
        patch(
            "src.server.services.autonomy.worker._instruction_handoff_lock",
            handoff,
        ),
        patch(
            "src.server.database.autonomy.effects.enqueue_effect",
            enqueue_effect,
        ),
    ):
        result = await worker.accept_threadless_instruction(
            ticket_id="ticket-1", user_id="u-1", text="after the snapshot"
        )

    assert inserted["under_lock"] is True
    assert result == {"input_id": "late", "status": "queued_for_delivery"}
    assert enqueued == ["autonomy:input:late"]


@pytest.mark.asyncio
async def test_threadless_reply_before_publish_does_not_queue_delivery():
    async def add_input(**kwargs):
        return {"input_id": "early", **kwargs}

    enqueue = AsyncMock()
    with (
        patch(
            "src.server.services.autonomy.worker.ticket_db.get_ticket",
            AsyncMock(return_value=_queued_ticket()),
        ),
        patch(
            "src.server.services.autonomy.worker.ticket_db.add_input",
            add_input,
        ),
        patch(
            "src.server.services.autonomy.worker._instruction_handoff_lock",
            _noop_handoff,
        ),
        patch(
            "src.server.database.autonomy.effects.enqueue_effect",
            enqueue,
        ),
    ):
        result = await worker.accept_threadless_instruction(
            ticket_id="ticket-1", user_id="u-1", text="before the thread"
        )

    assert result == {"input_id": "early", "status": "queued_on_task"}
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_instruction_handoff_lock_is_a_ticket_advisory_lock():
    sqls: list[tuple] = []

    class _Cursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, sql, params=None):
            sqls.append((sql, params))

    class _Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def cursor(self):
            return _Cursor()

    @asynccontextmanager
    async def connect():
        yield _Conn()

    with patch(
        "src.server.services.autonomy.worker.get_db_connection",
        connect,
    ):
        async with worker._instruction_handoff_lock("ticket-1"):
            assert sqls and "pg_advisory_lock" in sqls[0][0]

    assert sqls[0][1] == ("autonomy-instruction:ticket-1",)
    assert "pg_advisory_unlock" in sqls[1][0]
    assert sqls[1][1] == ("autonomy-instruction:ticket-1",)
