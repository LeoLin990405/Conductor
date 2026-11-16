"""The one-node P graph on a real checkpointer, driven by the real producer.

Offline: the model is a scripted ``BaseChatModel``, the checkpointer is
``MemorySaver``, and the tools run against a recording host.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ConfigDict, Field

from ptc_agent.agent.main_state import MainAgentState
from src.server.contracts.memory_tool_policy import MemoryToolPolicySnapshot
from src.server.handlers.chat.request_prep import init_tracking
from src.server.services.runs.sse_producer import RunSSEProducer
from src.server.services.superagent_runtime.bindings import (
    HostToolPort,
    LeaseCreditPort,
    PrecomputedRecall,
)
from src.server.services.superagent_runtime.graph import (
    SuperagentPorts,
    build_superagent_graph,
    checkpoint_has_pending_work,
)
from src.server.services.superagent_runtime.ports import CreditVerdict, TurnInput
from src.tools.autonomy.tools import AUTONOMY_WATCHDOG_TOOLS

MODEL = "scripted-model"
USAGE = {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}


# ---------------------------------------------------------------------------
# Doubles (imported by tests/unit/server/handlers/chat/test_superagent_run.py)
# ---------------------------------------------------------------------------


class ScriptedChat(BaseChatModel):
    """A chat model that streams pre-scripted rounds and records its inputs."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    rounds: list[list[AIMessageChunk]] = Field(default_factory=list)
    seen: list[list[BaseMessage]] = Field(default_factory=list)
    bound: list[list[Any]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        self.bound.append(list(tools))
        return self.bind(tool_names=[getattr(t, "name", str(t)) for t in tools])

    def _next_round(self, messages) -> list[AIMessageChunk]:
        self.seen.append(list(messages))
        if not self.rounds:
            return [AIMessageChunk(content="(no more script)")]
        return [chunk.model_copy(deep=True) for chunk in self.rounds.pop(0)]

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        chunks = self._next_round(messages)
        merged = chunks[0]
        for chunk in chunks[1:]:
            merged = merged + chunk
        message = AIMessage(
            content=merged.content,
            tool_calls=list(merged.tool_calls or []),
            usage_metadata=merged.usage_metadata,
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _astream(
        self, messages, stop=None, run_manager=None, **kwargs
    ) -> AsyncIterator[ChatGenerationChunk]:
        for chunk in self._next_round(messages):
            yield ChatGenerationChunk(message=chunk)


def usage_chunk() -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        usage_metadata=dict(USAGE),
        response_metadata={"model_name": MODEL},
    )


def tool_round(
    *,
    text: str = "Checking the ticket. ",
    call_id: str = "call-1",
    ticket_id: str = "t-1",
) -> list[AIMessageChunk]:
    """Text, then one tool call whose JSON args arrive in three pieces."""
    return [
        AIMessageChunk(content=text),
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": "autonomy_watchdog",
                    "args": f'{{"ticket_id": "{ticket_id}", ',
                    "id": call_id,
                    "index": 0,
                    "type": "tool_call_chunk",
                }
            ],
        ),
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": None,
                    "args": '"decision": "continue", ',
                    "id": None,
                    "index": 0,
                    "type": "tool_call_chunk",
                }
            ],
        ),
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": None,
                    "args": '"reason": "tests pass"}',
                    "id": None,
                    "index": 0,
                    "type": "tool_call_chunk",
                }
            ],
        ),
        usage_chunk(),
    ]


def text_round(text: str = "Ticket t-1 is progressing.") -> list[AIMessageChunk]:
    return [AIMessageChunk(content=text), usage_chunk()]


class RecordingHost:
    """Stands in for ``AutonomyToolHost``; records what the tools asked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def watchdog(self, **kwargs):
        self.calls.append(("watchdog", kwargs))
        return {"status": "recorded", "decision": kwargs["decision"]}

    async def ticket_status(self, ticket_id):
        self.calls.append(("ticket_status", {"ticket_id": ticket_id}))
        return {"ticket_id": ticket_id, "status": "running"}


class ScriptedCredit:
    """Allows the first ``allow`` rounds, then denies with ``reason``."""

    def __init__(self, allow: int, reason: str = "Credits ran out.") -> None:
        self.allow = allow
        self.reason = reason
        self.calls = 0

    async def authorize(self) -> CreditVerdict:
        self.calls += 1
        if self.calls <= self.allow:
            return CreditVerdict(True)
        return CreditVerdict(False, self.reason)


class CountingRecall:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.calls = 0

    async def recall(self) -> str:
        self.calls += 1
        return self.text


def parse_frames(frames: list[str]) -> list[tuple[str, dict, bool]]:
    """(event, data, has_id_line) for each SSE frame."""
    parsed = []
    for frame in frames:
        lines = frame.strip("\n").split("\n")
        has_id = lines[0].startswith("id: ") and lines[0][4:].isdigit()
        event = next(line[7:] for line in lines if line.startswith("event: "))
        data = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
        parsed.append((event, data, has_id))
    return parsed


def factory(role: str = "watchdog", prompt: str = "You are the Watchdog."):
    captured: list[TurnInput] = []

    def build(history, user_text):
        turn = TurnInput(
            role=role,
            system_prompt=prompt,
            history=list(history),
            user_text=user_text,
        )
        captured.append(turn)
        return turn

    build.captured = captured  # type: ignore[attr-defined]
    return build


async def drive(graph, *, thread_id: str, run_id: str, text: str, token_callback=None, tool_tracker=None):
    producer = RunSSEProducer(
        thread_id=thread_id,
        run_id=run_id,
        token_callback=token_callback,
        tool_tracker=tool_tracker,
    )
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    if token_callback is not None:
        config["callbacks"] = [token_callback]
    return [
        frame
        async for frame in producer.stream_workflow(
            graph=graph,
            input_state={
                "messages": [
                    {"id": f"superagent-input-{run_id}", "role": "user", "content": text}
                ]
            },
            config=config,
        )
    ]


def _graph(chat, *, saver, host=None, credit=None, recall=None, callbacks=(), build=None, max_rounds=12):
    ports = SuperagentPorts(
        credit=credit or LeaseCreditPort(None),
        recall=recall or PrecomputedRecall(""),
        tools=HostToolPort(
            AUTONOMY_WATCHDOG_TOOLS, host or RecordingHost(), policy=MemoryToolPolicySnapshot()
        ),
        callbacks=callbacks,
        max_rounds=max_rounds,
    )
    graph = build_superagent_graph(chat, build or factory(), ports, checkpointer=saver)
    return graph, ports


async def _messages(graph, thread_id):
    state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    return list(state.values.get("messages") or [])


def _shape(messages):
    out = []
    for m in messages:
        if isinstance(m, ToolMessage):
            out.append(("tool", m.tool_call_id, m.status))
        elif isinstance(m, AIMessage):
            out.append(("ai", tuple(c["id"] for c in m.tool_calls)))
        elif isinstance(m, HumanMessage):
            out.append(("human",))
        else:
            out.append((type(m).__name__,))
    return out


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frame_sequence_through_the_real_producer():
    chat = ScriptedChat(rounds=[tool_round(), text_round()])
    host = RecordingHost()
    token_callback, tool_tracker = init_tracking("th-frames")
    graph, ports = _graph(
        chat, saver=MemorySaver(), host=host, callbacks=(token_callback,)
    )

    frames = await drive(
        graph,
        thread_id="th-frames",
        run_id="run-1",
        text="Review ticket t-1",
        token_callback=token_callback,
        tool_tracker=tool_tracker,
    )
    parsed = parse_frames(frames)

    assert [event for event, _, _ in parsed] == [
        "metadata",
        "message_chunk",
        "tool_calls",
        "tool_call_result",
        "message_chunk",
        "credit_usage",
    ]
    assert all(has_id for _, _, has_id in parsed)
    ids = [int(frame.split("\n", 1)[0][4:]) for frame in frames]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)

    _, metadata, _ = parsed[0]
    assert metadata == {"thread_id": "th-frames", "run_id": "run-1"}

    _, first_text, _ = parsed[1]
    assert first_text["agent"] == "main"
    assert first_text["role"] == "assistant"
    assert first_text["content"] == "Checking the ticket. "
    assert first_text["content_type"] == "text"

    _, calls, _ = parsed[2]
    assert calls["agent"] == "main"
    assert calls["tool_calls"] == [
        {
            "id": "call-1",
            "name": "autonomy_watchdog",
            "args": {"ticket_id": "t-1", "decision": "continue", "reason": "tests pass"},
            "type": "tool_call",
        }
    ]

    _, result, _ = parsed[3]
    assert result["tool_call_id"] == "call-1"
    assert result["status"] == "success"
    assert json.loads(result["content"]) == {"status": "recorded", "decision": "continue"}

    _, final_text, _ = parsed[4]
    assert final_text["content"] == "Ticket t-1 is progressing."

    # The tool ran through the host with the args the model streamed.
    assert host.calls == [
        (
            "watchdog",
            {"ticket_id": "t-1", "decision": "continue", "question": "", "reason": "tests pass"},
        )
    ]
    # The model saw the role's real tool objects.
    assert [t.name for t in chat.bound[0]] == [t.name for t in AUTONOMY_WATCHDOG_TOOLS]
    # Usage reached the run's tracker through on_llm_end.
    records = token_callback.get_per_call_records()
    assert len(records) == 2
    assert ports.outcome.status == "ready"


@pytest.mark.asyncio
async def test_streamed_text_is_not_sent_twice_and_ids_match_the_checkpoint():
    chat = ScriptedChat(rounds=[text_round("One answer.")])
    saver = MemorySaver()
    graph, _ = _graph(chat, saver=saver)

    parsed = parse_frames(await drive(graph, thread_id="th-dup", run_id="run-1", text="hi"))
    chunks = [data for event, data, _ in parsed if event == "message_chunk"]
    assert "".join(c.get("content", "") for c in chunks) == "One answer."

    messages = await _messages(graph, "th-dup")
    final = messages[-1]
    assert isinstance(final, AIMessage)
    assert {c["id"] for c in chunks} == {final.id}


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_holds_paired_messages_and_turn_two_sees_turn_one():
    chat = ScriptedChat(
        rounds=[tool_round(), text_round(), text_round("Still progressing.")]
    )
    saver = MemorySaver()
    build = factory()
    graph, _ = _graph(chat, saver=saver, build=build)

    await drive(graph, thread_id="th-cp", run_id="run-1", text="Review t-1")
    turn_one = await _messages(graph, "th-cp")
    assert _shape(turn_one) == [
        ("human",),
        ("ai", ("call-1",)),
        ("tool", "call-1", "success"),
        ("ai", ()),
    ]
    # The input keeps the id the caller minted; the loop's copy is not added.
    assert turn_one[0].id == "superagent-input-run-1"
    assert turn_one[0].content == "Review t-1"

    await drive(graph, thread_id="th-cp", run_id="run-2", text="Review again")
    turn_two = await _messages(graph, "th-cp")
    assert _shape(turn_two) == [*_shape(turn_one), ("human",), ("ai", ())]

    # The factory got turn one as history and turn two's text as input.
    assert [m.id for m in build.captured[1].history] == [m.id for m in turn_one]
    assert build.captured[1].user_text == "Review again"
    # And the model was actually sent turn one's exchange.
    sent = chat.seen[-1]
    assert any(isinstance(m, ToolMessage) and m.tool_call_id == "call-1" for m in sent)
    assert any(isinstance(m, HumanMessage) and m.content == "Review t-1" for m in sent)


@pytest.mark.asyncio
async def test_a_ptc_shaped_checkpoint_seed_is_readable_and_survives():
    saver = MemorySaver()
    seed = [
        HumanMessage(content="Earlier batch", id="ptc-h1"),
        AIMessage(
            content="",
            id="ptc-a1",
            tool_calls=[
                {
                    "name": "autonomy_ticket_status",
                    "args": {"ticket_id": "t-0"},
                    "id": "ptc-call-1",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content='{"status": "done"}', tool_call_id="ptc-call-1", id="ptc-t1"),
        AIMessage(content="Ticket t-0 is done.", id="ptc-a2"),
    ]

    async def ptc_turn(state):
        return {"messages": seed, "loaded_skills": ["router-norms"]}

    ptc = StateGraph(MainAgentState)
    ptc.add_node("model", ptc_turn)
    ptc.add_edge(START, "model")
    ptc.add_edge("model", END)
    ptc_graph = ptc.compile(checkpointer=saver)
    await ptc_graph.ainvoke(
        {"messages": []}, config={"configurable": {"thread_id": "th-ptc"}}
    )
    tuple_ = await saver.aget_tuple({"configurable": {"thread_id": "th-ptc", "checkpoint_ns": ""}})
    assert checkpoint_has_pending_work(tuple_) is False

    chat = ScriptedChat(rounds=[text_round("New answer.")])
    build = factory()
    graph, _ = _graph(chat, saver=saver, build=build)
    await drive(graph, thread_id="th-ptc", run_id="run-p", text="Next batch")

    assert [m.id for m in build.captured[0].history] == [m.id for m in seed]
    assert any(
        isinstance(m, AIMessage) and m.content == "Ticket t-0 is done." for m in chat.seen[0]
    )

    # Reading back through PTC's own schema: P's messages follow PTC's, and
    # the PTC-only channel is still there.
    state = await ptc_graph.aget_state({"configurable": {"thread_id": "th-ptc"}})
    assert [m.id for m in state.values["messages"][:4]] == [m.id for m in seed]
    assert _shape(state.values["messages"][4:]) == [("human",), ("ai", ())]
    assert state.values["loaded_skills"] == ["router-norms"]


# ---------------------------------------------------------------------------
# Credit and round limits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mid_turn_credit_denial_keeps_partial_messages_and_a_stop_note():
    chat = ScriptedChat(rounds=[tool_round(), text_round("never sent")])
    credit = ScriptedCredit(allow=1, reason="Daily credit limit reached.")
    graph, ports = _graph(chat, saver=MemorySaver(), credit=credit)

    frames = parse_frames(await drive(graph, thread_id="th-cr", run_id="run-1", text="go"))

    assert ports.outcome.status == "stopped"
    assert ports.outcome.stop_reason == "Daily credit limit reached."
    assert ports.outcome.credit_stopped is True
    assert len(chat.seen) == 1  # the second round was never paid for
    messages = await _messages(graph, "th-cr")
    assert _shape(messages) == [
        ("human",),
        ("ai", ("call-1",)),
        ("tool", "call-1", "success"),
        ("ai", ()),
    ]
    note = messages[-1]
    assert note.content == "Daily credit limit reached."
    assert note.additional_kwargs.get("superagent_stop") == "credit"
    # No interrupt: the node finished, so nothing is pending on the thread.
    assert not any(event == "interrupt" for event, _, _ in frames)
    state = await graph.aget_state({"configurable": {"thread_id": "th-cr"}})
    assert state.next == ()
    assert not state.interrupts


@pytest.mark.asyncio
async def test_denial_before_the_first_round_skips_recall_and_the_model():
    chat = ScriptedChat(rounds=[text_round()])
    recall = CountingRecall("memory")
    graph, ports = _graph(
        chat, saver=MemorySaver(), credit=ScriptedCredit(allow=0), recall=recall
    )

    await drive(graph, thread_id="th-cr0", run_id="run-1", text="go")

    assert recall.calls == 0
    assert chat.seen == []
    assert ports.outcome.credit_stopped is True
    messages = await _messages(graph, "th-cr0")
    assert _shape(messages) == [("human",), ("ai", ())]
    assert messages[-1].content == "Credits ran out."


@pytest.mark.asyncio
async def test_recall_runs_once_before_the_first_round():
    chat = ScriptedChat(rounds=[text_round()])
    recall = CountingRecall("## Memory\n- Leo prefers short replies")
    graph, _ = _graph(chat, saver=MemorySaver(), recall=recall)

    await drive(graph, thread_id="th-mem", run_id="run-1", text="go")

    assert recall.calls == 1
    system = chat.seen[0][0]
    assert "Leo prefers short replies" in str(system.content)
    # Memory is not checkpointed as a turn.
    messages = await _messages(graph, "th-mem")
    assert all("Leo prefers" not in str(m.content) for m in messages)


@pytest.mark.asyncio
async def test_round_limit_is_not_a_credit_stop():
    chat = ScriptedChat(
        rounds=[tool_round(call_id="call-a"), tool_round(call_id="call-b")]
    )
    graph, ports = _graph(chat, saver=MemorySaver(), max_rounds=2)

    await drive(graph, thread_id="th-max", run_id="run-1", text="loop")

    assert ports.outcome.status == "stopped"
    assert ports.outcome.stop_reason == "max_rounds"
    assert ports.outcome.credit_stopped is False
    messages = await _messages(graph, "th-max")
    assert messages[-1].additional_kwargs.get("superagent_stop") == "max_rounds"
    # Every call the model made is answered in the checkpoint.
    called = [c["id"] for m in messages if isinstance(m, AIMessage) for c in m.tool_calls]
    answered = [m.tool_call_id for m in messages if isinstance(m, ToolMessage)]
    assert called == answered == ["call-a", "call-b"]


@pytest.mark.asyncio
async def test_a_turn_without_a_user_message_fails():
    graph, _ = _graph(ScriptedChat(), saver=MemorySaver())
    with pytest.raises(ValueError, match="no user message"):
        await graph.ainvoke(
            {"messages": []}, config={"configurable": {"thread_id": "th-empty"}}
        )


# ---------------------------------------------------------------------------
# Pending work on a thread (PRD 4.2 point 3)
# ---------------------------------------------------------------------------


async def _tuple(saver, thread_id):
    return await saver.aget_tuple(
        {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    )


def _ptc_like_graph(saver, second):
    async def model(state):
        return {"messages": [AIMessage(content="thinking", id="ptc-m1")]}

    builder = StateGraph(MainAgentState)
    builder.add_node("model", model)
    builder.add_node("tools", second)
    builder.add_edge(START, "model")
    builder.add_edge("model", "tools")
    builder.add_edge("tools", END)
    return builder.compile(checkpointer=saver)


@pytest.mark.asyncio
async def test_no_checkpoint_has_no_pending_work():
    assert checkpoint_has_pending_work(await _tuple(MemorySaver(), "nobody")) is False


@pytest.mark.asyncio
async def test_a_ptc_interrupt_is_pending_work():
    async def pause(state):
        interrupt({"action_requests": [{"type": "credit_pause", "message": "x"}]})
        return {}

    saver = MemorySaver()
    graph = _ptc_like_graph(saver, pause)
    await graph.ainvoke(
        {"messages": [HumanMessage(content="hi", id="h")]},
        config={"configurable": {"thread_id": "th-int"}},
    )
    assert (await graph.aget_state({"configurable": {"thread_id": "th-int"}})).interrupts
    assert checkpoint_has_pending_work(await _tuple(saver, "th-int")) is True


@pytest.mark.asyncio
async def test_an_unfinished_ptc_run_is_pending_work():
    async def crash(state):
        raise RuntimeError("worker died")

    saver = MemorySaver()
    graph = _ptc_like_graph(saver, crash)
    with pytest.raises(RuntimeError):
        await graph.ainvoke(
            {"messages": [HumanMessage(content="hi", id="h")]},
            config={"configurable": {"thread_id": "th-crash"}},
        )
    assert (await graph.aget_state({"configurable": {"thread_id": "th-crash"}})).next == ("tools",)
    assert checkpoint_has_pending_work(await _tuple(saver, "th-crash")) is True


@pytest.mark.asyncio
async def test_a_failed_p_turn_does_not_send_the_thread_to_ptc():
    class Boom(ScriptedChat):
        async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
            raise RuntimeError("provider down")
            yield  # pragma: no cover

    saver = MemorySaver()
    graph, _ = _graph(Boom(), saver=saver)
    with pytest.raises(RuntimeError):
        await graph.ainvoke(
            {"messages": [{"id": "in-1", "role": "user", "content": "go"}]},
            config={"configurable": {"thread_id": "th-pfail"}},
        )
    # Our own leftover trigger is discarded by the next P input, as LangGraph
    # does for any graph, so it must not force a PTC fallback.
    assert checkpoint_has_pending_work(await _tuple(saver, "th-pfail")) is False

    chat = ScriptedChat(rounds=[text_round("Recovered.")])
    graph, _ = _graph(chat, saver=saver)
    await drive(graph, thread_id="th-pfail", run_id="run-2", text="again")
    messages = await _messages(graph, "th-pfail")
    assert messages[-1].content == "Recovered."
    assert checkpoint_has_pending_work(await _tuple(saver, "th-pfail")) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True], ids=["single", "parallel"])
async def test_a_real_ptc_agent_that_died_inside_a_tool_is_pending_work(parallel):
    # create_agent runs tools through Send, so a crash mid-tool leaves the
    # queued calls in __pregel_tasks with no branch trigger. Continuing that
    # thread on P would drop the tools node and any sibling results.
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.tools import tool

    class _Model(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    @tool
    def boom(x: str) -> str:
        """Always fails."""
        raise RuntimeError("tool crashed")

    @tool
    def fine(x: str) -> str:
        """Always works."""
        return "fine"

    calls = [{"name": "boom", "args": {"x": "1"}, "id": "c1", "type": "tool_call"}]
    if parallel:
        calls.insert(0, {"name": "fine", "args": {"x": "1"}, "id": "c0", "type": "tool_call"})
    saver = MemorySaver()
    agent = create_agent(
        _Model(messages=iter([AIMessage(content="", tool_calls=calls, id="a1")])),
        tools=[boom, fine],
        state_schema=MainAgentState,
        checkpointer=saver,
    )
    config = {"configurable": {"thread_id": "th-send"}}
    with pytest.raises(RuntimeError, match="tool crashed"):
        await agent.ainvoke({"messages": [HumanMessage(content="go", id="h1")]}, config)
    assert (await agent.aget_state(config)).next == ("tools",)
    assert checkpoint_has_pending_work(await _tuple(saver, "th-send")) is True
