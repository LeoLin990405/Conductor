"""A native turn takes in the notice the server sends while it runs.

The PTC lane reads the steers sent into a live turn between the model's
calls. A native CLI owns its turn, so the notice is taken between the events
the CLI streams, and the turn that opens next carries it on the same CLI
session. The runtime is what receives it: the notice is a prompt, like any
other.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from src.server.services.native_runtime.contracts import (
    NativeEvent,
    NativeRuntimeSpec,
)
from src.server.services.native_runtime.graph import build_native_graph


class _Runtime:
    """A runtime that records the prompts it is given.

    A turn that hangs is the case the notice exists for: the CLI is still
    working, so the turn does not end and the server can say something to it.
    """

    provider = "fake"

    def __init__(
        self,
        *,
        hang_until_notice: bool = False,
        text: str = "working",
        fails_after_text: bool = False,
        streaming: bool = False,
    ):
        self.calls = []
        self.hang_until_notice = hang_until_notice
        self.text = text
        self.fails_after_text = fails_after_text
        self.streaming = streaming

    def capabilities(self):
        return None

    async def stream_turn(self, prompt, *, session_id=None, cwd=None):
        self.calls.append({"prompt": prompt, "session_id": session_id, "cwd": cwd})
        if self.fails_after_text:
            yield NativeEvent(kind="text_delta", text=self.text, session_id="s-1")
            raise RuntimeError("the cli fell over")
        if self.streaming and "Scheduler notice" not in prompt:
            # A CLI that keeps working: the events never stop.
            while True:
                yield NativeEvent(kind="text_delta", text=self.text, session_id="s-1")
                await asyncio.sleep(0.001)
        if self.hang_until_notice and "Scheduler notice" not in prompt:
            yield NativeEvent(kind="text_delta", text=self.text, session_id="s-1")
            # The CLI is still working: the turn does not end on its own.
            await asyncio.sleep(3600)
            return
        yield NativeEvent(kind="text_delta", text=self.text, session_id="s-1")

    async def cancel(self):
        pass


@pytest.mark.asyncio
async def test_the_runtime_receives_the_notice():
    """The notice reaches the runtime as the prompt of the next turn."""
    runtime = _Runtime(hang_until_notice=True)
    notice = AsyncMock(return_value="Scheduler notice: you have been silent.")
    graph = build_native_graph(
        runtime=runtime,
        spec=NativeRuntimeSpec(provider="fake", session_id="s-1", cwd="/tmp/work"),
        checkpointer=MemorySaver(),
        thread_id="thread-1",
        run_id="run-1",
    )
    with (
        patch(
            "src.server.services.native_runtime.wake.take_native_notice", notice
        ),
        patch(
            "src.server.services.native_runtime.graph._NOTICE_POLL_SECONDS", 0.01
        ),
    ):
        await graph.ainvoke(
            {"messages": [HumanMessage(content="do the work")]},
            config={"configurable": {"thread_id": "thread-1"}},
        )

    notice.assert_awaited()
    # The runtime was given the notice as a prompt, on the same session.
    assert runtime.calls[0]["prompt"] == "do the work"
    assert runtime.calls[1]["prompt"] == "Scheduler notice: you have been silent."
    assert runtime.calls[1]["session_id"] == "s-1"


@pytest.mark.asyncio
async def test_a_runtime_that_fails_mid_turn_raises():
    """A half turn is not a turn.

    A runtime that streams some text and then fails must not read as a turn
    that ended: the node raises, so the run is a failure and the executor
    settles the Ticket as waiting rather than as a Worker that answered.
    """
    runtime = _Runtime(fails_after_text=True)
    graph = build_native_graph(
        runtime=runtime,
        spec=NativeRuntimeSpec(provider="fake", session_id="s-1", cwd="/tmp/work"),
        checkpointer=MemorySaver(),
        thread_id="thread-1",
        run_id="run-1",
    )
    with pytest.raises(RuntimeError, match="the cli fell over"):
        await graph.ainvoke(
            {"messages": [HumanMessage(content="do the work")]},
            config={"configurable": {"thread_id": "thread-1"}},
        )


@pytest.mark.asyncio
async def test_the_notice_is_polled_while_the_cli_keeps_streaming():
    """A busy CLI must not be able to hold the notice off.

    The poll is on elapsed time: a CLI that keeps sending events would
    otherwise never leave the reader waiting long enough to look.
    """
    runtime = _Runtime(streaming=True)
    notice = AsyncMock(return_value="Scheduler notice: say what blocks you.")
    graph = build_native_graph(
        runtime=runtime,
        spec=NativeRuntimeSpec(provider="fake", session_id="s-1", cwd="/tmp/work"),
        checkpointer=MemorySaver(),
        thread_id="thread-1",
        run_id="run-1",
    )
    with (
        patch(
            "src.server.services.native_runtime.wake.take_native_notice", notice
        ),
        patch(
            "src.server.services.native_runtime.graph._NOTICE_POLL_SECONDS", 0.01
        ),
    ):
        await graph.ainvoke(
            {"messages": [HumanMessage(content="do the work")]},
            config={"configurable": {"thread_id": "thread-1"}},
        )

    notice.assert_awaited()
    assert runtime.calls[1]["prompt"] == "Scheduler notice: say what blocks you."


@pytest.mark.asyncio
async def test_a_turn_without_a_notice_source_reads_nothing():
    """A chat turn costs no polling: it has no run to take a notice in."""
    runtime = _Runtime()
    notice = AsyncMock(return_value="never read")
    graph = build_native_graph(
        runtime=runtime,
        spec=NativeRuntimeSpec(provider="fake", session_id="s-1", cwd="/tmp/work"),
        checkpointer=MemorySaver(),
        thread_id="thread-1",
    )
    with patch(
        "src.server.services.native_runtime.wake.take_native_notice", notice
    ):
        await graph.ainvoke(
            {"messages": [HumanMessage(content="hello")]},
            config={"configurable": {"thread_id": "thread-1"}},
        )

    notice.assert_not_awaited()
    assert len(runtime.calls) == 1


@pytest.mark.asyncio
async def test_the_notice_ends_the_message_the_turn_produced():
    """The text the Worker writes after the notice is its closing message."""
    runtime = _Runtime(hang_until_notice=True, text="before")
    notice = AsyncMock(return_value="Scheduler notice: say what blocks you.")
    graph = build_native_graph(
        runtime=runtime,
        spec=NativeRuntimeSpec(provider="fake", session_id="s-1", cwd="/tmp/work"),
        checkpointer=MemorySaver(),
        thread_id="thread-1",
        run_id="run-1",
    )
    config = {"configurable": {"thread_id": "thread-1"}}
    with (
        patch(
            "src.server.services.native_runtime.wake.take_native_notice", notice
        ),
        patch(
            "src.server.services.native_runtime.graph._NOTICE_POLL_SECONDS", 0.01
        ),
    ):
        await graph.ainvoke(
            {"messages": [HumanMessage(content="do the work")]},
            config=config,
        )

    state = await graph.aget_state(config)
    projection = state.values["messages"][-1]
    assert isinstance(projection, AIMessage)
    assert projection.content == "beforebefore"
    # The closing message is what the notice opened.
    assert projection.additional_kwargs["native_final_text"] == "before"


@pytest.mark.asyncio
async def test_a_failure_after_partial_text_is_not_a_successful_notice():
    """Partial text plus a notice must not hide a runtime that then raised.

    The poll can be due, and a notice can be waiting, while the failure is
    already queued behind the text. The turn raises. It does not answer.
    """
    runtime = _Runtime(fails_after_text=True)
    notice = AsyncMock(return_value="Scheduler notice: you have been silent.")
    graph = build_native_graph(
        runtime=runtime,
        spec=NativeRuntimeSpec(provider="fake", session_id="s-1", cwd="/tmp/work"),
        checkpointer=MemorySaver(),
        thread_id="thread-1",
        run_id="run-1",
    )
    with (
        patch(
            "src.server.services.native_runtime.wake.take_native_notice", notice
        ),
        patch(
            "src.server.services.native_runtime.graph._NOTICE_POLL_SECONDS", 0.0
        ),
        pytest.raises(RuntimeError, match="the cli fell over"),
    ):
        await graph.ainvoke(
            {"messages": [HumanMessage(content="do the work")]},
            config={"configurable": {"thread_id": "thread-1"}},
        )

    assert len(runtime.calls) == 1


@pytest.mark.asyncio
async def test_a_full_queue_is_polled_on_elapsed_time(monkeypatch):
    """Events that keep the queue full must not hold the notice off.

    The poll used to run only when waiting for an event timed out. A queue
    that always has an event never timed out, so the notice was never read.
    """
    import time

    from src.server.services.native_runtime import graph as graph_module

    class _Burst:
        provider = "fake"

        def __init__(self):
            self.calls = []

        async def stream_turn(self, prompt, *, session_id=None, cwd=None):
            self.calls.append(prompt)
            if "Scheduler notice" in prompt:
                yield NativeEvent(kind="text_delta", text="ack", session_id="s-1")
                return
            for _ in range(40):
                yield NativeEvent(kind="text_delta", text="x", session_id="s-1")
            await asyncio.sleep(30)

        async def cancel(self):
            pass

    ticks = {"n": 0}
    real_monotonic = time.monotonic

    def monotonic():
        import sys

        # Only the graph's own clock jumps. The event loop keeps real time,
        # so a queued burst is not drained by a fake timeout.
        frame = sys._getframe(1)
        if not frame.f_code.co_filename.endswith("graph.py"):
            return real_monotonic()
        ticks["n"] += 1
        # The first reads arm the poll. Later reads are past it, while the
        # burst is still sitting in the queue.
        if ticks["n"] <= 3:
            return real_monotonic()
        return real_monotonic() + 10_000.0

    monkeypatch.setattr(graph_module.time, "monotonic", monotonic)
    monkeypatch.setattr(graph_module, "_NOTICE_POLL_SECONDS", 10.0)
    runtime = _Burst()
    notice = AsyncMock(return_value="Scheduler notice: say what blocks you.")
    graph = build_native_graph(
        runtime=runtime,
        spec=NativeRuntimeSpec(provider="fake", session_id="s-1", cwd="/tmp/work"),
        checkpointer=MemorySaver(),
        thread_id="thread-1",
        run_id="run-1",
    )
    with patch(
        "src.server.services.native_runtime.wake.take_native_notice", notice
    ):
        await graph.ainvoke(
            {"messages": [HumanMessage(content="do the work")]},
            config={"configurable": {"thread_id": "thread-1"}},
        )

    notice.assert_awaited()
    assert runtime.calls[1] == "Scheduler notice: say what blocks you."


@pytest.mark.asyncio
async def test_a_failed_replacement_turn_does_not_confirm_the_notice():
    """The notice stays owed until the replacement turn is checkpointed.

    A restart that raises gives the rows back. Confirming when the notice
    was only read would drop it.
    """
    order: list[str] = []

    class _FailSecond:
        provider = "fake"

        def __init__(self):
            self.calls = []

        async def stream_turn(self, prompt, *, session_id=None, cwd=None):
            self.calls.append(prompt)
            order.append(f"turn:{prompt}")
            if len(self.calls) == 1:
                yield NativeEvent(kind="text_delta", text="working", session_id="s-1")
                await asyncio.sleep(3600)
                return
            raise RuntimeError("replacement died")

        async def cancel(self):
            pass

    async def list_takeable(*_args, **_kwargs):
        return [
            {
                "hook_outbox_id": "job-1",
                "payload": {"content": "Scheduler notice: hold me"},
            }
        ]

    async def hold_jobs(ids, *, owner):
        order.append("hold")
        return list(ids)

    async def confirm_jobs(ids, *, owner):
        order.append("confirm")
        return list(ids)

    async def release_jobs(ids, *, owner):
        order.append("release")
        return list(ids)

    runtime = _FailSecond()
    graph = build_native_graph(
        runtime=runtime,
        spec=NativeRuntimeSpec(provider="fake", session_id="s-1", cwd="/tmp/work"),
        checkpointer=MemorySaver(),
        thread_id="thread-1",
        run_id="run-1",
    )
    with (
        patch(
            "src.server.database.runs.outbox.list_takeable_jobs",
            side_effect=list_takeable,
        ),
        patch(
            "src.server.database.runs.outbox.hold_jobs",
            side_effect=hold_jobs,
        ),
        patch(
            "src.server.database.runs.outbox.confirm_jobs",
            side_effect=confirm_jobs,
        ),
        patch(
            "src.server.database.runs.outbox.release_jobs",
            side_effect=release_jobs,
        ),
        patch(
            "src.server.services.native_runtime.graph._NOTICE_POLL_SECONDS", 0.01
        ),
        pytest.raises(RuntimeError, match="replacement died"),
    ):
        await graph.ainvoke(
            {"messages": [HumanMessage(content="do the work")]},
            config={"configurable": {"thread_id": "thread-1"}},
        )

    assert "confirm" not in order
    assert "hold" in order
    assert "release" in order
    assert order.index("hold") < order.index("release")
    assert any(item.startswith("turn:") for item in order)


@pytest.mark.asyncio
async def test_the_notice_is_confirmed_only_after_the_replacement_turn():
    """Durable acceptance is the checkpoint, which is after the turn runs."""
    order: list[str] = []

    class _ThenAnswer:
        provider = "fake"

        async def stream_turn(self, prompt, *, session_id=None, cwd=None):
            order.append(f"turn:{prompt}")
            if "Scheduler notice" not in prompt:
                yield NativeEvent(kind="text_delta", text="working", session_id="s-1")
                await asyncio.sleep(3600)
                return
            yield NativeEvent(kind="text_delta", text="answered", session_id="s-1")

        async def cancel(self):
            pass

    async def list_takeable(*_args, **_kwargs):
        return [
            {
                "hook_outbox_id": "job-1",
                "payload": {"content": "Scheduler notice: hold me"},
            }
        ]

    async def hold_jobs(ids, *, owner):
        order.append("hold")
        return list(ids)

    async def confirm_jobs(ids, *, owner):
        order.append("confirm")
        return list(ids)

    async def release_jobs(ids, *, owner):
        order.append("release")
        return list(ids)

    graph = build_native_graph(
        runtime=_ThenAnswer(),
        spec=NativeRuntimeSpec(provider="fake", session_id="s-1", cwd="/tmp/work"),
        checkpointer=MemorySaver(),
        thread_id="thread-1",
        run_id="run-1",
    )
    with (
        patch(
            "src.server.database.runs.outbox.list_takeable_jobs",
            side_effect=list_takeable,
        ),
        patch(
            "src.server.database.runs.outbox.hold_jobs",
            side_effect=hold_jobs,
        ),
        patch(
            "src.server.database.runs.outbox.confirm_jobs",
            side_effect=confirm_jobs,
        ),
        patch(
            "src.server.database.runs.outbox.release_jobs",
            side_effect=release_jobs,
        ),
        patch(
            "src.server.services.native_runtime.graph._NOTICE_POLL_SECONDS", 0.01
        ),
    ):
        await graph.ainvoke(
            {"messages": [HumanMessage(content="do the work")]},
            config={"configurable": {"thread_id": "thread-1"}},
        )

    assert "release" not in order
    turn_at = next(
        index for index, item in enumerate(order) if item.startswith("turn:Scheduler")
    )
    assert turn_at < order.index("confirm")


def _outbox_patches(order: list[str], *, confirm_fails: bool = False):
    async def list_takeable(*_args, **_kwargs):
        if "hold" in order:
            return []
        return [
            {
                "hook_outbox_id": "job-1",
                "payload": {"content": "Scheduler notice: hold me"},
            }
        ]

    async def hold_jobs(ids, *, owner):
        order.append("hold")
        return list(ids)

    async def confirm_jobs(ids, *, owner):
        order.append("confirm")
        if confirm_fails:
            raise RuntimeError("outbox is down")
        return list(ids)

    async def release_jobs(ids, *, owner):
        order.append("release")
        return list(ids)

    return (
        patch(
            "src.server.database.runs.outbox.list_takeable_jobs",
            side_effect=list_takeable,
        ),
        patch("src.server.database.runs.outbox.hold_jobs", side_effect=hold_jobs),
        patch(
            "src.server.database.runs.outbox.confirm_jobs", side_effect=confirm_jobs
        ),
        patch(
            "src.server.database.runs.outbox.release_jobs", side_effect=release_jobs
        ),
        patch("src.server.services.native_runtime.graph._NOTICE_POLL_SECONDS", 0.01),
    )


@pytest.mark.asyncio
async def test_a_failed_confirm_does_not_fail_the_turn_that_carried_the_notice():
    """The replacement turn already read the notice; its work must survive."""
    order: list[str] = []

    class _ThenAnswer:
        provider = "fake"

        async def stream_turn(self, prompt, *, session_id=None, cwd=None):
            order.append(f"turn:{prompt}")
            if "Scheduler notice" not in prompt:
                yield NativeEvent(kind="text_delta", text="working", session_id="s-1")
                await asyncio.sleep(3600)
                return
            yield NativeEvent(kind="text_delta", text="answered", session_id="s-1")

        async def cancel(self):
            pass

    graph = build_native_graph(
        runtime=_ThenAnswer(),
        spec=NativeRuntimeSpec(provider="fake", session_id="s-1", cwd="/tmp/work"),
        checkpointer=MemorySaver(),
        thread_id="thread-1",
        run_id="run-1",
    )
    patches = _outbox_patches(order, confirm_fails=True)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="do the work")]},
            config={"configurable": {"thread_id": "thread-1"}},
        )

    assert "confirm" in order
    assert isinstance(result["messages"][-1], AIMessage)
    assert "answered" in str(result["messages"][-1].content)


@pytest.mark.asyncio
async def test_a_notice_turn_that_pauses_on_approval_confirms_before_the_pause():
    """``interrupt`` raises out of the node; a held notice must not be left held."""
    from langgraph.types import Command

    from src.server.services.native_runtime.approvals import (
        PendingNativeApproval,
        get_approval_broker,
    )

    thread_id = "thread-notice-approval"
    order: list[str] = []
    broker = get_approval_broker()
    broker.clear(thread_id)

    class _NoticeThenApproval:
        provider = "fake"

        def capabilities(self):
            return None

        async def stream_turn(self, prompt, *, session_id=None, cwd=None):
            order.append(f"turn:{prompt}")
            if "Scheduler notice" not in prompt:
                yield NativeEvent(kind="text_delta", text="working ", session_id="s-1")
                await asyncio.sleep(3600)
                return
            broker.park(
                PendingNativeApproval(
                    thread_id=thread_id,
                    runtime=self,
                    request_id=1,
                    approval_id="approval-1",
                    kind="command",
                    interrupt_payload={"action_requests": [], "kind": "tool_approval"},
                    text_parts=["working "],
                )
            )
            yield NativeEvent(
                kind="approval_requested",
                text="approval needed",
                session_id="s-1",
                data={"approval_id": "approval-1"},
            )

        async def resume_turn(self, response):
            broker.clear(thread_id)
            yield NativeEvent(kind="text_delta", text="done", session_id="s-1")
            yield NativeEvent(kind="turn_completed", session_id="s-1")

        async def cancel(self):
            pass

    graph = build_native_graph(
        runtime=_NoticeThenApproval(),
        spec=NativeRuntimeSpec(provider="fake", session_id="s-1", cwd="/tmp/work"),
        checkpointer=MemorySaver(),
        thread_id=thread_id,
        run_id="run-1",
    )
    config = {"configurable": {"thread_id": thread_id}}
    patches = _outbox_patches(order)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        first = await graph.ainvoke(
            {"messages": [HumanMessage(content="do the work")]}, config=config
        )
        assert first.get("__interrupt__")
        # The pause already settled the notice the replacement turn carried.
        assert order.count("confirm") == 1
        assert "release" not in order
        await graph.ainvoke(
            Command(resume={"interrupt-id": {"decisions": [{"type": "approve"}]}}),
            config=config,
        )
    assert order.count("hold") == 1
    broker.clear(thread_id)


@pytest.mark.asyncio
async def test_events_buffered_when_the_notice_is_taken_are_not_dropped():
    """What the CLI already streamed is consumed before the notice ends the turn."""

    class _BurstThenHang:
        provider = "fake"

        def __init__(self):
            self.prompts = []

        def capabilities(self):
            return None

        async def stream_turn(self, prompt, *, session_id=None, cwd=None):
            self.prompts.append(prompt)
            if "Scheduler notice" in prompt:
                yield NativeEvent(kind="text_delta", text=" after", session_id="s-1")
                return
            for index in range(3):
                yield NativeEvent(
                    kind="text_delta", text=f" chunk-{index}", session_id="s-1"
                )
            await asyncio.sleep(3600)

        async def cancel(self):
            pass

    calls = {"n": 0}

    async def take_notice(_thread_id, _run_id):
        calls["n"] += 1
        if calls["n"] > 1:
            return ""
        # The reader runs while the notice is read, so the burst is buffered.
        await asyncio.sleep(0.05)
        return "Scheduler notice: you have been silent."

    runtime = _BurstThenHang()
    graph = build_native_graph(
        runtime=runtime,
        spec=NativeRuntimeSpec(provider="fake", session_id="s-1", cwd="/tmp/work"),
        checkpointer=MemorySaver(),
        thread_id="thread-burst",
        run_id="run-1",
    )
    with (
        patch(
            "src.server.services.native_runtime.wake.take_native_notice",
            side_effect=take_notice,
        ),
        patch("src.server.services.native_runtime.graph._NOTICE_POLL_SECONDS", 0.0),
    ):
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="do the work")]},
            config={"configurable": {"thread_id": "thread-burst"}},
        )

    text = str(result["messages"][-1].content)
    assert "chunk-2" in text
    assert "after" in text
    assert runtime.prompts[1] == "Scheduler notice: you have been silent."


def test_notice_intake_is_off_while_the_idle_wake_is_off(monkeypatch):
    """With CONDUCTOR_WORKER_IDLE_WAKE_MINUTES unset a Worker turn polls nothing."""
    from src.server.handlers.chat.native_run import _native_notice_run_id

    monkeypatch.delenv("CONDUCTOR_WORKER_IDLE_WAKE_MINUTES", raising=False)
    assert _native_notice_run_id("run-1", worker_role=True) is None

    monkeypatch.setenv("CONDUCTOR_WORKER_IDLE_WAKE_MINUTES", "0")
    assert _native_notice_run_id("run-1", worker_role=True) is None

    monkeypatch.setenv("CONDUCTOR_WORKER_IDLE_WAKE_MINUTES", "30")
    assert _native_notice_run_id("run-1", worker_role=True) == "run-1"
    # A chat turn never polls, whatever the switch says.
    assert _native_notice_run_id("run-1", worker_role=False) is None
