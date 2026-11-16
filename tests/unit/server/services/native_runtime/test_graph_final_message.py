"""A native turn's final assistant message is kept apart for completion reports."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from src.server.services.autonomy import executor
from src.server.services.history.projector import (
    history_events_to_sse,
    messages_to_history_events,
)
from src.server.services.native_runtime.contracts import (
    NativeEvent,
    NativeRuntimeError,
    NativeRuntimeSpec,
)
from src.server.services.native_runtime.graph import build_native_graph
from src.server.services.projects.dispatch import last_assistant_text

REPORT = json.dumps(
    {
        "status": "done",
        "summary": "ok",
        "artifacts": {"commits": [], "files": [], "pr": None},
        "verification": [],
        "next": "",
        "handbacks": [],
    }
)


class FakeRuntime:
    provider = "fake"

    def __init__(self, events):
        self.events = events

    def capabilities(self):
        return None

    async def stream_turn(self, prompt, *, session_id=None, cwd=None):
        for event in self.events:
            yield event

    async def cancel(self):
        pass


async def _final_message(events, **kwargs) -> AIMessage:
    graph = build_native_graph(
        runtime=FakeRuntime(events),
        spec=NativeRuntimeSpec(provider="fake", cwd="/tmp/native-work"),
        checkpointer=MemorySaver(),
        **kwargs,
    )
    config = {"configurable": {"thread_id": "thread-1"}}
    await graph.ainvoke({"messages": [HumanMessage(content="work")]}, config=config)
    state = await graph.aget_state(config)
    message = state.values["messages"][-1]
    assert isinstance(message, AIMessage)
    return message


def _replay_text(message: AIMessage) -> str | None:
    items = history_events_to_sse(
        messages_to_history_events([message]), thread_id="thread-1"
    )
    sse = "".join(
        f"event: {item['event']}\ndata: {json.dumps(item['data'])}\n\n"
        for item in items
    )
    return last_assistant_text(sse)


_PROSE_THEN_TOOL = [
    NativeEvent(kind="text_delta", text="I will inspect the repo."),
    NativeEvent(
        kind="tool_started", tool_name="shell", tool_call_id="c1", data={"id": "c1"}
    ),
    NativeEvent(
        kind="tool_result", tool_call_id="c1", data={"id": "c1", "content": "ok"}
    ),
    NativeEvent(kind="text_delta", text=REPORT[:20]),
    NativeEvent(kind="text_delta", text=REPORT[20:]),
    NativeEvent(kind="turn_completed"),
]


@pytest.mark.asyncio
async def test_text_before_a_tool_call_is_not_part_of_the_final_message():
    message = await _final_message(_PROSE_THEN_TOOL)

    assert message.content == "I will inspect the repo." + REPORT
    assert message.additional_kwargs["native_final_text"] == REPORT
    assert _replay_text(message) == REPORT


@pytest.mark.asyncio
async def test_a_new_message_item_starts_a_new_final_message():
    message = await _final_message(
        [
            NativeEvent(kind="text_delta", text="Progress. ", data={"item_id": "m1"}),
            NativeEvent(kind="text_delta", text=REPORT, data={"item_id": "m2"}),
            NativeEvent(kind="turn_completed"),
        ]
    )

    assert message.additional_kwargs["native_final_text"] == REPORT
    assert _replay_text(message) == REPORT


@pytest.mark.asyncio
async def test_a_single_message_turn_keeps_its_old_shape():
    message = await _final_message(
        [
            NativeEvent(kind="text_delta", text="just ", data={"item_id": "m1"}),
            NativeEvent(kind="text_delta", text="this", data={"item_id": "m1"}),
            NativeEvent(kind="turn_completed"),
        ]
    )

    assert message.content == "just this"
    assert "native_final_text" not in message.additional_kwargs
    assert _replay_text(message) == "just this"


@pytest.mark.asyncio
async def test_a_tool_event_after_the_last_text_leaves_that_text_final():
    tool = _PROSE_THEN_TOOL[1:3]
    message = await _final_message(
        [NativeEvent(kind="text_delta", text="Prose. "), *tool]
        + [NativeEvent(kind="text_delta", text=REPORT), *tool]
        + [NativeEvent(kind="turn_completed")]
    )

    assert message.additional_kwargs["native_final_text"] == REPORT
    assert _replay_text(message) == REPORT


@pytest.mark.asyncio
async def test_reasoning_between_two_messages_is_a_boundary():
    message = await _final_message(
        [
            NativeEvent(kind="text_delta", text="Progress. "),
            NativeEvent(kind="thinking_delta", text="thinking"),
            NativeEvent(kind="text_delta", text=REPORT),
            NativeEvent(kind="turn_completed"),
        ]
    )

    assert message.additional_kwargs["native_final_text"] == REPORT


def test_codex_exec_stream_marks_each_agent_message_item():
    from src.server.services.native_runtime.codex_stream import parse_codex_line

    events = parse_codex_line(
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "item_3", "type": "agent_message", "text": "hi"},
            }
        )
    )
    assert [event.data["item_id"] for event in events] == ["item_3"]


@pytest.mark.asyncio
async def test_a_chat_turn_without_text_still_fails():
    with pytest.raises(NativeRuntimeError, match="no final answer"):
        await _final_message([NativeEvent(kind="turn_completed")])


@pytest.mark.asyncio
async def test_a_worker_turn_without_text_completes_empty():
    message = await _final_message(
        [NativeEvent(kind="turn_completed")], allow_empty_answer=True
    )

    assert message.content == ""
    assert _replay_text(message) is None


class _Cursor:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, *args, **kwargs):
        return None

    async def fetchall(self):
        return [("ticket-1", "user-1", "thread-1")]


class _Connection:
    def cursor(self):
        return _Cursor()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


async def _executor_report(assistant):
    ticket = {
        "ticket_id": "ticket-1",
        "status": "running",
        "worker_provider": "codex",
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-1",
        "attempts": 1,
        "mandate": {"read_only": True},
    }
    dispatcher = AsyncMock()
    dispatcher.thread_run_status = AsyncMock(return_value="completed")
    dispatcher.thread_last_assistant_text = AsyncMock(return_value=assistant)
    with (
        patch("src.server.database.pool.get_db_connection", return_value=_Connection()),
        patch.object(executor, "ProjectDispatcher", return_value=dispatcher),
        patch.object(executor.ticket_db, "get_ticket", AsyncMock(return_value=ticket)),
        patch.object(executor.ticket_db, "latest_report", AsyncMock(return_value=None)),
        patch.object(executor, "_release_settled_worktree", AsyncMock()),
        patch.object(executor, "apply_worker_report", AsyncMock()) as apply_report,
    ):
        await executor.poll_running_workers()
    return apply_report.await_args.kwargs


@pytest.mark.asyncio
async def test_server_worker_prose_before_a_tool_call_settles_from_the_report():
    """Regression: prose before a tool call must not void a valid report."""
    message = await _final_message(_PROSE_THEN_TOOL)

    report = await _executor_report(_replay_text(message))

    assert report["state"] == "ready"
    assert report["completion_report"].status == "done"


@pytest.mark.asyncio
async def test_server_worker_turn_without_text_settles_waiting_not_failed():
    """Regression: a completed Worker turn with no text is a missing report."""
    message = await _final_message(
        [NativeEvent(kind="turn_completed")], allow_empty_answer=True
    )

    report = await _executor_report(_replay_text(message))

    assert report["state"] == "waiting"
    assert report["attention_reason"] == "completion_report_missing"
