"""The native graph must not replay Conductor history into the CLI."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from src.server.services.native_runtime.contracts import (
    NativeEvent,
    NativeRuntimeSpec,
)
from src.server.services.native_runtime.graph import build_native_graph


class FakeRuntime:
    provider = "fake"

    def __init__(self, events):
        self.events = events
        self.calls = []

    def capabilities(self):
        return None

    async def stream_turn(self, prompt, *, session_id=None, cwd=None):
        self.calls.append({"prompt": prompt, "session_id": session_id, "cwd": cwd})
        for event in self.events:
            yield event

    async def cancel(self):
        pass


@pytest.mark.asyncio
async def test_native_graph_uses_only_current_human_and_persists_projection():
    runtime = FakeRuntime(
        [
            NativeEvent(kind="text_delta", text="native ", session_id="s-2"),
            NativeEvent(kind="text_delta", text="answer", session_id="s-2"),
            NativeEvent(kind="usage", data={"input_tokens": 10}),
            NativeEvent(kind="turn_completed", session_id="s-2"),
        ]
    )
    seen_sessions = []

    async def on_session(session_id):
        seen_sessions.append(session_id)

    graph = build_native_graph(
        runtime=runtime,
        spec=NativeRuntimeSpec(
            provider="fake", session_id="s-1", cwd="/tmp/native-work"
        ),
        checkpointer=MemorySaver(),
        on_session=on_session,
    )
    config = {"configurable": {"thread_id": "thread-1"}}
    await graph.ainvoke(
        {
            "messages": [
                HumanMessage(content="old question"),
                AIMessage(content="old answer"),
                HumanMessage(content="new question"),
            ]
        },
        config=config,
    )
    # Only the current message crosses the native boundary; prior transcript
    # stays the CLI's/pre-existing graph's, not this node's prompt.
    # A resumed native session already owns its history: first-turn context
    # is not re-injected, which would invalidate the native prompt cache.
    assert runtime.calls == [
        {"prompt": "new question", "session_id": "s-1", "cwd": "/tmp/native-work"}
    ]
    assert seen_sessions == ["s-2"]

    state = await graph.aget_state(config)
    projection = state.values["messages"][-1]
    assert isinstance(projection, AIMessage)
    assert projection.content == "native answer"
    assert projection.additional_kwargs["native_session_id"] == "s-2"


@pytest.mark.asyncio
async def test_first_turn_context_is_an_explicit_handoff():
    runtime = FakeRuntime([NativeEvent(kind="text_delta", text="ok", session_id="s-1")])
    graph = build_native_graph(
        runtime=runtime,
        spec=NativeRuntimeSpec(provider="fake"),
        checkpointer=MemorySaver(),
        context_block="MEMORY CONTEXT",
    )
    await graph.ainvoke(
        {"messages": [HumanMessage(content="hello")]},
        config={"configurable": {"thread_id": "thread-context"}},
    )
    assert runtime.calls[0]["prompt"] == "MEMORY CONTEXT\n\nhello"


@pytest.mark.asyncio
async def test_native_graph_surfaces_runtime_error():
    runtime = FakeRuntime(
        [NativeEvent(kind="error", text="mcode login required")]
    )
    graph = build_native_graph(
        runtime=runtime,
        spec=NativeRuntimeSpec(provider="fake"),
        checkpointer=MemorySaver(),
    )
    with pytest.raises(Exception, match="mcode login required"):
        await graph.ainvoke(
            {"messages": [HumanMessage(content="hello")]},
            config={"configurable": {"thread_id": "thread-err"}},
        )


@pytest.mark.asyncio
async def test_the_graph_closes_an_aborted_turn_stream():
    """An aborted turn is closed, so the runtime can give its lock back.

    A native runtime keeps the provider lock for the whole turn. The graph
    raises on an error event, which stops the consumption without closing
    the stream, so the graph has to close it: otherwise the lock stays held
    until the generator is collected, and every later ZCode call on the
    host waits for it.
    """
    closed = False

    class FailingRuntime:
        provider = "fake-failing"

        def capabilities(self):
            return None

        async def stream_turn(self, prompt, *, session_id=None, cwd=None):
            nonlocal closed
            try:
                yield NativeEvent(kind="error", text="native runtime failed")
            finally:
                closed = True

        async def cancel(self):
            pass

    graph = build_native_graph(
        runtime=FailingRuntime(),
        spec=NativeRuntimeSpec(provider="fake-failing"),
        checkpointer=MemorySaver(),
    )
    with pytest.raises(Exception, match="native runtime failed"):
        await graph.ainvoke(
            {"messages": [HumanMessage(content="hello")]},
            config={"configurable": {"thread_id": "thread-abort"}},
        )
    assert closed


@pytest.mark.asyncio
async def test_native_tool_events_enter_the_checkpoint_projection_for_replay():
    runtime = FakeRuntime(
        [
            NativeEvent(
                kind="tool_started",
                tool_name="get_index_spot",
                tool_call_id="call-1",
                data={"name": "get_index_spot", "args": {"symbol": "000001.SH"}},
            ),
            NativeEvent(
                kind="tool_result",
                tool_name="get_index_spot",
                tool_call_id="call-1",
                data={"content": "3648.30", "status": "success"},
            ),
            NativeEvent(kind="text_delta", text="done", session_id="s-1"),
        ]
    )
    graph = build_native_graph(
        runtime=runtime,
        spec=NativeRuntimeSpec(provider="fake"),
        checkpointer=MemorySaver(),
    )
    config = {"configurable": {"thread_id": "thread-tools"}}
    await graph.ainvoke(
        {"messages": [HumanMessage(content="quote please")]}, config=config
    )
    messages = (await graph.aget_state(config)).values["messages"]
    names = [getattr(message, "type", "") for message in messages]
    assert "ai" in names and "tool" in names
    assert messages[-1].content == "done"
    tool_message = next(m for m in messages if getattr(m, "type", "") == "tool")
    assert tool_message.tool_call_id == "call-1"
    assert tool_message.content == "3648.30"


@pytest.mark.asyncio
async def test_a_tool_start_whose_result_never_arrived_still_gets_a_tool_message():
    """A skipped or lost result must not leave a dangling tool call behind.

    The projection is replayed to the UI and read by other models if the thread
    changes provider, and a provider refuses an assistant tool call that no tool
    message answers.
    """
    runtime = FakeRuntime(
        [
            NativeEvent(
                kind="tool_started",
                tool_name="read_file",
                tool_call_id="call-lost",
                data={"name": "read_file", "args": {"path": "big.log"}},
            ),
            NativeEvent(
                kind="tool_started",
                tool_name="grep",
                tool_call_id="call-ok",
                data={"name": "grep", "args": {"pattern": "x"}},
            ),
            NativeEvent(
                kind="tool_result",
                tool_name="grep",
                tool_call_id="call-ok",
                data={"content": "no match", "status": "success"},
            ),
            NativeEvent(kind="text_delta", text="done", session_id="s-1"),
        ]
    )
    graph = build_native_graph(
        runtime=runtime,
        spec=NativeRuntimeSpec(provider="fake"),
        checkpointer=MemorySaver(),
    )
    config = {"configurable": {"thread_id": "thread-orphan"}}
    await graph.ainvoke({"messages": [HumanMessage(content="go")]}, config=config)
    messages = (await graph.aget_state(config)).values["messages"]

    asked = [
        call["id"]
        for message in messages
        if getattr(message, "type", "") == "ai"
        for call in getattr(message, "tool_calls", None) or []
    ]
    answered = [
        message.tool_call_id
        for message in messages
        if getattr(message, "type", "") == "tool"
    ]
    assert sorted(asked) == ["call-lost", "call-ok"]
    assert sorted(answered) == sorted(asked)

    lost = next(
        message
        for message in messages
        if getattr(message, "type", "") == "tool"
        and message.tool_call_id == "call-lost"
    )
    assert lost.status == "error"
    assert "no result" in lost.content.lower()
    # The answer that really arrived is untouched, and the final text is last.
    ok = next(
        message
        for message in messages
        if getattr(message, "type", "") == "tool"
        and message.tool_call_id == "call-ok"
    )
    assert (ok.content, ok.status) == ("no match", "success")
    assert messages[-1].content == "done"


def _tool_shape(messages):
    """The projection as (type, id-of-the-call-it-is-about) pairs, in order."""
    shape = []
    for message in messages:
        kind = getattr(message, "type", "")
        if kind == "ai" and getattr(message, "tool_calls", None):
            shape.append(("call", message.tool_calls[0]["id"]))
        elif kind == "tool":
            shape.append(("result", message.tool_call_id))
    return shape


@pytest.mark.asyncio
async def test_every_projected_call_is_followed_by_its_own_result():
    """Parallel calls arrive as call, call, result, result.

    A provider takes an assistant tool call only with its tool message right
    behind it, so the projection is laid out call/result, call/result even when
    one of the results never came.
    """
    runtime = FakeRuntime(
        [
            NativeEvent(kind="tool_started", tool_name="a", tool_call_id="A", data={"args": {}}),
            NativeEvent(kind="tool_started", tool_name="b", tool_call_id="B", data={"args": {}}),
            NativeEvent(
                kind="tool_result",
                tool_name="a",
                tool_call_id="A",
                data={"content": "from-a", "status": "success"},
            ),
            NativeEvent(kind="text_delta", text="done", session_id="s-1"),
        ]
    )
    graph = build_native_graph(
        runtime=runtime,
        spec=NativeRuntimeSpec(provider="fake"),
        checkpointer=MemorySaver(),
    )
    config = {"configurable": {"thread_id": "thread-parallel"}}
    await graph.ainvoke({"messages": [HumanMessage(content="go")]}, config=config)
    messages = (await graph.aget_state(config)).values["messages"]

    assert _tool_shape(messages) == [
        ("call", "A"),
        ("result", "A"),
        ("call", "B"),
        ("result", "B"),
    ]
    by_call = {m.tool_call_id: m for m in messages if getattr(m, "type", "") == "tool"}
    assert by_call["A"].content == "from-a"
    assert by_call["B"].status == "error"


@pytest.mark.asyncio
async def test_a_call_id_a_later_turn_reuses_does_not_overwrite_the_earlier_turn():
    """The CLI numbers its tool items from the start of every turn."""

    def turn(result):
        events = [
            NativeEvent(kind="tool_started", tool_name="read", tool_call_id="item_1", data={"args": {}}),
        ]
        if result is not None:
            events.append(
                NativeEvent(
                    kind="tool_result",
                    tool_name="read",
                    tool_call_id="item_1",
                    data={"content": result, "status": "success"},
                )
            )
        events.append(NativeEvent(kind="text_delta", text="done", session_id="s-1"))
        return events

    config = {"configurable": {"thread_id": "thread-reuse"}}
    saver = MemorySaver()
    for result in ("first-turn-result", None):
        graph = build_native_graph(
            runtime=FakeRuntime(turn(result)),
            spec=NativeRuntimeSpec(provider="fake"),
            checkpointer=saver,
        )
        await graph.ainvoke({"messages": [HumanMessage(content="go")]}, config=config)

    messages = (await graph.aget_state(config)).values["messages"]
    results = [m for m in messages if getattr(m, "type", "") == "tool"]
    assert [m.content for m in results] == ["first-turn-result", "No result was recorded for this tool call."]
    assert [m.status for m in results] == ["success", "error"]
    calls = [m for m in messages if getattr(m, "type", "") == "ai" and m.tool_calls]
    assert len(calls) == 2
    assert len({m.id for m in messages}) == len(messages)
