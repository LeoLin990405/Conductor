"""The Super-agent P lane handler (``superagent_run.py``).

Drives ``astream_superagent_workflow`` end to end with the real producer and
graph: the executor and the Redis read-back are fakes that run the workflow
generator in a task and replay what it produced. No sandbox, no PTC graph,
no ``create_llm``: each is patched to fail if called.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ptc_agent.agent.main_state import MainAgentState
from src.server.contracts.memory_tool_policy import MemoryToolPolicySnapshot
from src.server.services.credit_gate import (
    CreditLease,
    CreditStopError,
    current_credit_gate,
)
from ptc_agent.agent.middleware.runtime_context.turn_context import TurnContext
from src.server.handlers.chat import superagent_run
from src.server.models.chat import ChatRequest
from src.server.services.runs.admission import RunScope
from src.server.services.superagent_runtime.prompts import WATCHDOG_SYSTEM_PROMPT
from src.server.services.superagent_runtime.selection import SuperagentLane
from src.server.services.turn_runtime import TurnRoute
from src.tools.autonomy.tools import AUTONOMY_WATCHDOG_TOOLS
from tests.unit.server.services.superagent_runtime.test_graph import (
    MODEL,
    RecordingHost,
    ScriptedChat,
    parse_frames,
    text_round,
    tool_round,
)

THREAD = "th-lane"


class FakeManager:
    """``LocalRunExecutor`` stand-in: drives the generator in its own task."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.frames: list[str] = []
        self.error: BaseException | None = None
        self.gate_seen = None
        self.task: asyncio.Task | None = None

    async def start_run(
        self,
        *,
        thread_id,
        run_id,
        workflow_generator,
        metadata,
        graph=None,
        on_registered=None,
        completion_callback=None,
    ):
        self.calls.append(
            {"thread_id": thread_id, "run_id": run_id, "metadata": metadata, "graph": graph}
        )
        if on_registered is not None:
            on_registered()

        async def drive():
            try:
                async for frame in workflow_generator:
                    if self.gate_seen is None:
                        self.gate_seen = current_credit_gate.get()
                    self.frames.append(frame)
            except Exception as exc:  # the executor finalizes failed here
                self.error = exc

        self.task = asyncio.create_task(drive())
        return SimpleNamespace(task=self.task)


class FakeConfig:
    """A resolved agent config whose client is already built."""

    def __init__(self, chat) -> None:
        self.chat = chat
        self.llm = SimpleNamespace(name=MODEL)
        self.llm_definition = None
        self.credential_source = None
        self.client_calls = 0

    def get_llm_client(self):
        self.client_calls += 1
        return self.chat


class FakeGate:
    """Enough of ``CreditGateState`` for ``run_with_credit_gate``."""

    def __init__(self, lease) -> None:
        self.lease = lease
        self.run_ref = "run-1"
        self.started = False
        self.closed = False

    async def start(self):
        self.started = True

    async def aclose(self):
        self.closed = True


class DenyAfterFirstLease(CreditLease):
    """A lease the quota service denies once the first round has run."""

    def should_stop(self, spent: float) -> bool:
        self._boundaries = getattr(self, "_boundaries", 0) + 1
        return self._boundaries > 1


def _forbid(name):
    def fail(*args, **kwargs):
        raise AssertionError(f"{name} must not be called on the P lane")

    return fail


@pytest.fixture
def lane_env(monkeypatch):
    manager = FakeManager()
    errors: list[BaseException] = []

    async def fake_stream_from_log(thread_id, run_id, last_event_id=None):
        assert manager.task is not None
        await manager.task
        for frame in manager.frames:
            yield frame

    async def fake_handle_workflow_error(exc, **kwargs):
        errors.append(exc)
        if False:
            yield ""  # pragma: no cover

    monkeypatch.setattr(
        superagent_run, "LocalRunExecutor", SimpleNamespace(get_instance=lambda: manager)
    )
    monkeypatch.setattr(superagent_run, "stream_from_log", fake_stream_from_log)
    monkeypatch.setattr(superagent_run, "handle_workflow_error", fake_handle_workflow_error)
    monkeypatch.setattr(superagent_run, "build_run_credit_gate", lambda *a, **k: None)

    import ptc_agent.agent.graph as ptc_graph_module
    import src.llms as llms
    import src.server.handlers.chat.ptc_run as ptc_run
    from src.server.services.workspace_manager import WorkspaceManager

    monkeypatch.setattr(
        WorkspaceManager, "get_session_for_workspace", _forbid("get_session_for_workspace")
    )
    monkeypatch.setattr(
        ptc_graph_module, "build_ptc_graph_with_session", _forbid("build_ptc_graph_with_session")
    )
    monkeypatch.setattr(
        ptc_run, "build_ptc_graph_with_session", _forbid("build_ptc_graph_with_session")
    )
    monkeypatch.setattr(llms, "create_llm", _forbid("create_llm"))
    return SimpleNamespace(manager=manager, errors=errors, monkeypatch=monkeypatch)


async def _run_lane(chat, *, saver=None, host=None, request=None, run_id="run-1", memory=None):
    saver = saver or MemorySaver()
    host = host or RecordingHost()
    config = FakeConfig(chat)
    run_handle = SimpleNamespace(checkpointer=saver, turn_index=0)
    scope = RunScope(user_id="u-1", burst_slot_id=None)
    lock = asyncio.Lock()
    await lock.acquire()
    scope.hold_admission(lock)
    lane = SuperagentLane(role="watchdog", host=host, tools=tuple(AUTONOMY_WATCHDOG_TOOLS))
    frames = [
        frame
        async for frame in superagent_run.astream_superagent_workflow(
            request=request
            or ChatRequest(messages=[{"role": "user", "content": "Review ticket t-1"}]),
            thread_id=THREAD,
            run_id=run_id,
            user_input="Review ticket t-1",
            user_id="u-1",
            route=TurnRoute(workspace_id="ws-home", role="generalist"),
            lane=lane,
            scope=scope,
            run_handle=run_handle,
            turn_context=TurnContext(timezone="Asia/Shanghai", tool_timezone="Asia/Shanghai"),
            config=config,
            is_byok=False,
            memory_context_block=memory,
            memory_tool_policy=MemoryToolPolicySnapshot(),
        )
    ]
    return SimpleNamespace(
        frames=frames,
        saver=saver,
        host=host,
        config=config,
        scope=scope,
        lock=lock,
        run_handle=run_handle,
    )


async def _checkpoint_messages(saver, thread_id=THREAD):
    tuple_ = await saver.aget_tuple(
        {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    )
    graph = StateGraph(MainAgentState)
    graph.add_node("noop", lambda state: {})
    graph.add_edge(START, "noop")
    graph.add_edge("noop", END)
    state = await graph.compile(checkpointer=saver).aget_state(tuple_.config)
    return list(state.values.get("messages") or [])


@pytest.mark.asyncio
async def test_lane_streams_the_standard_frames_without_sandbox_or_ptc_graph(lane_env):
    chat = ScriptedChat(rounds=[tool_round(), text_round()])
    result = await _run_lane(chat, memory="## Memory\n- watch t-1 closely")

    parsed = parse_frames(result.frames)
    assert [event for event, _, _ in parsed] == [
        "metadata",
        "message_chunk",
        "tool_calls",
        "tool_call_result",
        "message_chunk",
        "credit_usage",
    ]
    assert all(has_id for _, _, has_id in parsed)
    assert parsed[0][1]["run_id"] == "run-1"
    assert lane_env.manager.error is None
    assert lane_env.errors == []

    # The executor got the same run metadata the native lane passes.
    [call] = lane_env.manager.calls
    metadata = call["metadata"]
    assert call["thread_id"] == THREAD and call["run_id"] == "run-1"
    assert metadata["run_handle"] is result.run_handle
    assert metadata["handler"] is not None
    assert metadata["sandbox"] is None and metadata["sandbox_id"] is None
    assert metadata["superagent_runtime"] == "p"
    assert metadata["superagent_role"] == "watchdog"
    assert metadata["timezone"] == "Asia/Shanghai"
    # The handoff and the admission release happened.
    assert result.scope.handed_off is True
    assert not result.lock.locked()

    # The model is the config's resolved client, called once per turn.
    assert result.config.client_calls == 1
    # token_callback is the producer's tracker and saw on_llm_end per round.
    token_callback = metadata["token_callback"]
    assert metadata["handler"].token_callback is token_callback
    assert len(token_callback.get_per_call_records()) == 2

    # Tools went through the lane's host only.
    assert [name for name, _ in result.host.calls] == ["watchdog"]

    # System prompt: role text, clock in the user's zone, and the memory block.
    system = str(chat.seen[0][0].content)
    assert WATCHDOG_SYSTEM_PROMPT.strip().splitlines()[0] in system
    assert "(Asia/Shanghai)" in system
    assert "watch t-1 closely" in system

    messages = await _checkpoint_messages(result.saver)
    assert messages[0].id == "superagent-input-run-1"
    assert isinstance(messages[1], AIMessage) and messages[1].tool_calls[0]["id"] == "call-1"
    assert isinstance(messages[2], ToolMessage) and messages[2].tool_call_id == "call-1"
    assert messages[-1].content == "Ticket t-1 is progressing."


@pytest.mark.asyncio
async def test_second_turn_reads_the_first_from_the_checkpoint(lane_env):
    saver = MemorySaver()
    chat = ScriptedChat(rounds=[tool_round(), text_round()])
    await _run_lane(chat, saver=saver, run_id="run-1")

    lane_env.manager.__init__()
    chat_two = ScriptedChat(rounds=[text_round("Nothing new.")])
    await _run_lane(chat_two, saver=saver, run_id="run-2")

    sent = chat_two.seen[0]
    assert any(isinstance(m, ToolMessage) and m.tool_call_id == "call-1" for m in sent)
    messages = await _checkpoint_messages(saver)
    assert [m.id for m in messages if m.id.startswith("superagent-input-")] == [
        "superagent-input-run-1",
        "superagent-input-run-2",
    ]
    assert messages[-1].content == "Nothing new."


@pytest.mark.asyncio
async def test_mid_turn_credit_denial_fails_the_run_with_credit_stop(lane_env):
    lease = DenyAfterFirstLease(user_id="u-1", run_ref="run-1", port=None)
    lease.denial = {"message": "Daily credit limit reached."}
    gate = FakeGate(lease)
    lane_env.monkeypatch.setattr(
        superagent_run, "build_run_credit_gate", lambda *a, **k: gate
    )
    chat = ScriptedChat(rounds=[tool_round(), text_round("never sent")])

    result = await _run_lane(chat)

    # The run's own stream raised after the producer drained.
    assert isinstance(lane_env.manager.error, CreditStopError)
    assert str(lane_env.manager.error) == "Daily credit limit reached."
    events = [event for event, _, _ in parse_frames(result.frames)]
    assert events[0] == "metadata"
    assert events[-1] == "credit_usage"
    assert "interrupt" not in events
    # The gate wrapped the run like PTC's main lane.
    assert gate.started and gate.closed
    assert lane_env.manager.gate_seen is gate
    assert len(chat.seen) == 1

    messages = await _checkpoint_messages(result.saver)
    assert [type(m).__name__ for m in messages] == [
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "AIMessage",
    ]
    assert messages[-1].content == "Daily credit limit reached."
    assert messages[-1].additional_kwargs["superagent_stop"] == "credit"

    # Finalizing that failure stamps the ledger's credit_stop error type.
    from src.server.contracts.status import CREDIT_STOP_ERROR_TYPE
    from src.server.services.runs.finalization import (
        assemble_finalize_artifacts,
        classify_outcome,
    )

    exc = lane_env.manager.error
    status, phase, reason, pause, error = await classify_outcome(
        "failed", handler=None, graph=None, thread_id=THREAD, error=str(exc)
    )
    outcome = await assemble_finalize_artifacts(
        (THREAD, "run-1"),
        metadata={},
        status=status,
        phase=phase,
        interrupt_reason=reason,
        pause_message=pause,
        error=error,
        handler=None,
        cancelled_by_user=False,
        workspace_id=None,
        user_id=None,
        exc=exc,
    )
    assert outcome.metadata["error_type"] == CREDIT_STOP_ERROR_TYPE


@pytest.mark.asyncio
async def test_no_gate_runs_to_completion(lane_env):
    result = await _run_lane(ScriptedChat(rounds=[text_round("ok")]))
    assert lane_env.manager.error is None
    assert lane_env.manager.gate_seen is None
    assert parse_frames(result.frames)[-1][0] == "credit_usage"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"checkpoint_id": "cp-1"},
        {
            "hitl_response": {
                "int-1": {"decisions": [{"type": "approve"}]},
            }
        },
    ],
)
async def test_replay_and_hitl_resume_are_409(request_kwargs):
    request = ChatRequest(
        messages=[{"role": "user", "content": "x"}], **request_kwargs
    )
    with pytest.raises(HTTPException) as info:
        await superagent_run.check_p_lane_admissible(
            request=request,
            run_handle=SimpleNamespace(checkpointer=MemorySaver()),
            thread_id=THREAD,
            run_id="run-1",
            lane=SuperagentLane(role="watchdog", host=object(), tools=()),
        )
    assert info.value.status_code == 409


@pytest.mark.asyncio
async def test_a_clean_thread_is_admissible_on_p():
    await superagent_run.check_p_lane_admissible(
        request=ChatRequest(messages=[{"role": "user", "content": "x"}]),
        run_handle=SimpleNamespace(checkpointer=MemorySaver()),
        thread_id=THREAD,
        run_id="run-1",
        lane=SuperagentLane(role="watchdog", host=object(), tools=()),
    )


def test_pending_ptc_after_start_is_a_non_recoverable_labelled_failure():
    from src.server.handlers.chat.error_handling import (
        SuperagentPendingPtcError,
        _classify_non_recoverable_error_type,
        classify_error,
    )

    exc = SuperagentPendingPtcError("x")
    assert classify_error(exc)["is_recoverable"] is False
    assert _classify_non_recoverable_error_type(exc) == "superagent_pending_ptc"


@pytest.mark.asyncio
async def test_missing_config_is_503(lane_env):
    scope = RunScope(user_id="u-1", burst_slot_id=None)
    gen = superagent_run.astream_superagent_workflow(
        request=ChatRequest(messages=[{"role": "user", "content": "x"}]),
        thread_id=THREAD,
        run_id="run-1",
        user_input="x",
        user_id="u-1",
        route=TurnRoute(workspace_id="ws-home", role="generalist"),
        lane=SuperagentLane(role="supervisor", host=object(), tools=()),
        scope=scope,
        run_handle=SimpleNamespace(checkpointer=MemorySaver(), turn_index=0),
        turn_context=TurnContext(),
        config=None,
        is_byok=False,
        memory_context_block=None,
        memory_tool_policy=MemoryToolPolicySnapshot(),
    )
    with pytest.raises(HTTPException) as info:
        async for _ in gen:
            pass
    assert info.value.status_code == 503


# ---------------------------------------------------------------------------
# PTC fallback probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_is_false_for_a_new_or_finished_thread(lane_env):
    saver = MemorySaver()
    assert await superagent_run.superagent_needs_ptc_fallback(
        checkpointer=saver, thread_id=THREAD
    ) is False

    await _run_lane(ScriptedChat(rounds=[text_round()]), saver=saver)
    assert lane_env.manager.error is None
    assert await superagent_run.superagent_needs_ptc_fallback(
        checkpointer=saver, thread_id=THREAD
    ) is False


@pytest.mark.asyncio
async def test_fallback_is_true_for_a_pending_ptc_interrupt():
    async def paused(state):
        interrupt({"action_requests": [{"type": "credit_pause", "message": "x"}]})
        return {}

    saver = MemorySaver()
    builder = StateGraph(MainAgentState)
    builder.add_node("model", paused)
    builder.add_edge(START, "model")
    builder.add_edge("model", END)
    await builder.compile(checkpointer=saver).ainvoke(
        {"messages": [{"id": "h", "role": "user", "content": "hi"}]},
        config={"configurable": {"thread_id": THREAD}},
    )

    assert await superagent_run.superagent_needs_ptc_fallback(
        checkpointer=saver, thread_id=THREAD
    ) is True


@pytest.mark.asyncio
async def test_fallback_without_a_checkpointer_is_false():
    assert await superagent_run.superagent_needs_ptc_fallback(
        checkpointer=None, thread_id=THREAD
    ) is False


@pytest.mark.asyncio
async def test_settle_keeps_the_lane_on_a_clean_thread():
    lane = SuperagentLane(role="supervisor", host=object(), tools=())
    kept = await superagent_run.settle_superagent_lane(
        lane, checkpointer=MemorySaver(), thread_id=THREAD, run_id="run-1"
    )
    assert kept is lane


@pytest.mark.asyncio
async def test_settle_falls_back_to_ptc_when_the_probe_fails(caplog):
    class _Broken:
        async def aget_tuple(self, config):
            raise ConnectionError("checkpoint store down")

    lane = SuperagentLane(role="watchdog", host=object(), tools=())
    with caplog.at_level("WARNING"):
        kept = await superagent_run.settle_superagent_lane(
            lane, checkpointer=_Broken(), thread_id=THREAD, run_id="run-1"
        )
    assert kept is None
    assert "[SUPERAGENT_P] fallback=probe_error" in caplog.text
