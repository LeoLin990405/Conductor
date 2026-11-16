"""R8c: the per-role Super-agent runtime switch at the message route.

``_handle_send_message`` picks a P lane for a Router (``supervisor``) or
Watchdog dispatch when that role's switch says ``p``, and hands it to
``astream_ptc_workflow`` with a ``superagent_runtime=p`` START stamp. With the
switches off the call is what it was: no lane, the same host and tools.
Workers and ordinary turns never get a lane.

The ptc_run half checks the branch itself: the P lane skips
``cancel_stale_workflow`` and enters ``astream_superagent_workflow``, and a
thread with a pending interrupt falls back to PTC for that turn.

All in process (ASGITransport); no network, no database, no Redis.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.server.models.chat import ChatRequest
from src.server.services.turn_runtime import TurnRoute
from tests.unit.server.app.test_threads_dispatch_auth import (
    _BODY,
    TOKEN,
    _app,
    _post,
    _stub_ptc_workflow,
    _stub_workflow,
)

SUPERVISOR_ENV = "CONDUCTOR_SUPERAGENT_RUNTIME_SUPERVISOR"
WATCHDOG_ENV = "CONDUCTOR_SUPERAGENT_RUNTIME_WATCHDOG"
DISPATCH = {"X-Dispatch": "background", "X-Service-Token": TOKEN}
PTC = "src.server.handlers.chat.ptc_run"
SUPERAGENT_RUN = "src.server.handlers.chat.superagent_run"


def _flags(monkeypatch, *, supervisor: str, watchdog: str) -> None:
    # Set (never delete) both: a lazy load_dotenv() may repopulate a deleted
    # variable but never overrides one that is set.
    monkeypatch.setenv(SUPERVISOR_ENV, supervisor)
    monkeypatch.setenv(WATCHDOG_ENV, watchdog)


async def _dispatch(monkeypatch, *, platform: str | None, headers=DISPATCH) -> dict:
    """POST one turn on ``platform`` and return the kwargs the workflow got."""
    monkeypatch.setattr("src.config.settings.HOST_MODE", "platform")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", TOKEN)
    seen: dict = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return _stub_ptc_workflow(**kwargs)

    body = {**_BODY, **({"platform": platform} if platform else {})}
    ticket = {"ticket_id": "tk-1", "user_id": "usr-dispatch-001"}
    # The stored row is the thread's surface (RA-3), so it is the lane the
    # request names; a turn naming none is a report-back on a Router thread.
    with (
        _stub_workflow(platform=platform or "superagent_router"),
        patch("src.server.handlers.chat.astream_ptc_workflow", new=_capture),
        patch(
            "src.server.database.autonomy.tickets.get_ticket_by_worker_thread",
            new=AsyncMock(return_value=ticket),
        ),
    ):
        resp = await _post(_app(), headers=headers, body=body)
    assert resp.status_code == 200, resp.text
    seen["_response"] = resp.json()
    return seen


def _assert_ptc_call(seen: dict, *, platform: str) -> None:
    """The call carries no lane and today's host and tools."""
    from src.server.services.autonomy.host import AutonomyToolHost
    from src.tools.autonomy import AUTONOMY_ROUTER_TOOLS, AUTONOMY_WATCHDOG_TOOLS

    assert seen["superagent_lane"] is None
    assert seen["run_metadata"] is None
    expected = (
        AUTONOMY_WATCHDOG_TOOLS
        if platform == "superagent_watchdog"
        else AUTONOMY_ROUTER_TOOLS
    )
    assert seen["extra_tools"] is expected
    assert isinstance(seen["extra_configurable"]["autonomy_host"], AutonomyToolHost)
    assert set(seen["extra_configurable"]) == {"autonomy_host"}


def _assert_p_call(seen: dict, *, role: str) -> None:
    from src.tools.autonomy import AUTONOMY_ROUTER_TOOLS, AUTONOMY_WATCHDOG_TOOLS

    lane = seen["superagent_lane"]
    assert lane is not None
    assert lane.role == role
    assert lane.host is seen["extra_configurable"]["autonomy_host"]
    tools = AUTONOMY_WATCHDOG_TOOLS if role == "watchdog" else AUTONOMY_ROUTER_TOOLS
    assert seen["extra_tools"] is tools
    assert tuple(lane.tools) == tuple(tools)
    assert seen["run_metadata"] == {"superagent_runtime": "p"}


# ---------------------------------------------------------------------------
# messaging.py: lane selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["superagent_router", "superagent_watchdog"])
async def test_both_switches_off_keep_the_ptc_call(monkeypatch, platform):
    _flags(monkeypatch, supervisor="ptc", watchdog="ptc")
    seen = await _dispatch(monkeypatch, platform=platform)
    assert seen["_response"]["status"] == "dispatched"
    _assert_ptc_call(seen, platform=platform)


@pytest.mark.asyncio
async def test_supervisor_on_moves_only_the_router(monkeypatch):
    _flags(monkeypatch, supervisor="p", watchdog="ptc")
    router = await _dispatch(monkeypatch, platform="superagent_router")
    _assert_p_call(router, role="supervisor")
    watchdog = await _dispatch(monkeypatch, platform="superagent_watchdog")
    _assert_ptc_call(watchdog, platform="superagent_watchdog")


@pytest.mark.asyncio
async def test_watchdog_on_moves_only_the_watchdog(monkeypatch):
    _flags(monkeypatch, supervisor="ptc", watchdog="p")
    watchdog = await _dispatch(monkeypatch, platform="superagent_watchdog")
    _assert_p_call(watchdog, role="watchdog")
    router = await _dispatch(monkeypatch, platform="superagent_router")
    _assert_ptc_call(router, platform="superagent_router")


@pytest.mark.asyncio
async def test_both_on_leave_workers_on_ptc(monkeypatch):
    from src.tools.autonomy import AUTONOMY_WORKER_TOOLS

    _flags(monkeypatch, supervisor="p", watchdog="p")
    worker = await _dispatch(monkeypatch, platform="superagent_worker")
    assert worker["superagent_lane"] is None
    assert worker["run_metadata"] is None
    assert worker["extra_tools"] is AUTONOMY_WORKER_TOOLS


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["superagent_router", "superagent_watchdog"])
async def test_a_non_dispatch_request_is_gone(monkeypatch, platform):
    """Without X-Dispatch neither role is reachable: the door answers 410."""
    _flags(monkeypatch, supervisor="p", watchdog="p")
    monkeypatch.setattr("src.config.settings.HOST_MODE", "platform")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", TOKEN)
    with _stub_workflow(platform=platform):
        resp = await _post(
            _app(),
            headers={"X-Service-Token": TOKEN},
            body={**_BODY, "platform": platform},
        )
    assert resp.status_code == 410, resp.text
    assert resp.json()["detail"]["code"] == "chat_retired"


def _config_yaml(*, supervisor: str, watchdog: str):
    return SimpleNamespace(
        superagent_runtime=SimpleNamespace(supervisor=supervisor, watchdog=watchdog)
    )


@pytest.mark.asyncio
async def test_env_beats_config_yaml(monkeypatch):
    monkeypatch.setattr(
        "src.config.settings.get_infrastructure_config",
        lambda: _config_yaml(supervisor="p", watchdog="ptc"),
    )
    _flags(monkeypatch, supervisor="ptc", watchdog="p")
    router = await _dispatch(monkeypatch, platform="superagent_router")
    _assert_ptc_call(router, platform="superagent_router")
    watchdog = await _dispatch(monkeypatch, platform="superagent_watchdog")
    _assert_p_call(watchdog, role="watchdog")


@pytest.mark.asyncio
async def test_config_yaml_decides_when_env_is_unset(monkeypatch):
    monkeypatch.setattr(
        "src.config.settings.get_infrastructure_config",
        lambda: _config_yaml(supervisor="p", watchdog="ptc"),
    )
    monkeypatch.delenv(SUPERVISOR_ENV, raising=False)
    monkeypatch.delenv(WATCHDOG_ENV, raising=False)
    router = await _dispatch(monkeypatch, platform="superagent_router")
    _assert_p_call(router, role="supervisor")


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["P-lane", "native", "yes"])
async def test_an_unknown_value_means_ptc(monkeypatch, value):
    _flags(monkeypatch, supervisor=value, watchdog=value)
    for platform in ("superagent_router", "superagent_watchdog"):
        seen = await _dispatch(monkeypatch, platform=platform)
        _assert_ptc_call(seen, platform=platform)


# ---------------------------------------------------------------------------
# ptc_run.py: the P branch
# ---------------------------------------------------------------------------


class _StopAtPtcHalf(Exception):
    """Raised by the first PTC-half step so the test stops there."""


def _lane():
    from src.server.services.superagent_runtime.selection import SuperagentLane

    return SuperagentLane(role="supervisor", host=MagicMock(), tools=())


async def _run_ptc(
    *,
    lane,
    needs_fallback: bool | BaseException | None,
    request: ChatRequest | None = None,
    expect_raise: bool = False,
    saver=None,
    before_start=None,
):
    """Drive ``astream_ptc_workflow`` with a cold workspace and a P lane."""
    from src.server.handlers.chat.ptc_run import astream_ptc_workflow

    request = request or ChatRequest(
        workspace_id="ws-1",
        platform="superagent_router",
        messages=[{"role": "user", "content": "route these"}],
    )
    manager = MagicMock()
    manager.get_admission_lock = AsyncMock(return_value=asyncio.Lock())
    manager.cancel_stale_workflow = AsyncMock()
    workspace_manager = MagicMock()
    # Home never has a ready sandbox on the P lane.
    workspace_manager.has_ready_session.return_value = False
    run_handle = MagicMock(run_id="run-1", turn_index=1, finalized=False)
    turn_context = MagicMock(inherits_rules=False, tool_timezone="UTC")
    p_calls: list[dict] = []
    errors: list[BaseException] = []

    async def fake_superagent(**kwargs):
        p_calls.append(kwargs)
        yield "event: metadata\ndata: {}\n\n"

    async def fake_error_funnel(error, **_kwargs):
        errors.append(error)
        if False:
            yield ""

    config = MagicMock()
    config.llm.name = "synthetic-model"
    with ExitStack() as stack:
        setup = stack.enter_context(patch(f"{PTC}.setup"))
        executor = stack.enter_context(patch(f"{PTC}.LocalRunExecutor"))
        workspaces = stack.enter_context(patch(f"{PTC}.WorkspaceManager"))
        stack.enter_context(patch(f"{PTC}.dedup_retransmit_or_raise", new=AsyncMock()))
        stack.enter_context(
            patch(f"{PTC}.wait_or_steer", new=AsyncMock(return_value=(True, None)))
        )
        stack.enter_context(
            patch(f"{PTC}.ensure_thread", new=AsyncMock(return_value=MagicMock()))
        )
        stack.enter_context(
            patch(
                f"{PTC}.retried_on_its_surface",
                new=AsyncMock(return_value=(request, False)),
            )
        )
        stack.enter_context(
            patch(f"{PTC}.read_disk_notice", new=AsyncMock(return_value=(None, False)))
        )
        stack.enter_context(
            patch(f"{PTC}.get_user_profile_for_prompt", new=AsyncMock(return_value=None))
        )
        stack.enter_context(patch(f"{PTC}.build_turn_context", return_value=turn_context))
        stack.enter_context(patch(f"{PTC}._resolve_fork", return_value=("message", None)))
        stack.enter_context(patch(f"{PTC}.is_own_key_turn", return_value=False))
        async def fake_begin(*args, **kwargs):
            if before_start is not None:
                await before_start()
            return run_handle

        begin = stack.enter_context(patch(f"{PTC}.begin_run", new=AsyncMock(side_effect=fake_begin)))
        stack.enter_context(
            patch(
                "src.server.services.refactor.memory_runtime_controls.automatic_memory_enabled",
                new=AsyncMock(return_value=False),
            )
        )
        stack.enter_context(
            patch(f"{PTC}.get_thread_metadata", new=AsyncMock(return_value={}))
        )
        keep_model = stack.enter_context(patch(f"{PTC}.keep_named_model", new=AsyncMock()))
        init_tracking = stack.enter_context(
            patch(f"{PTC}.init_tracking", side_effect=_StopAtPtcHalf)
        )
        stack.enter_context(
            patch(f"{PTC}.handle_workflow_error", side_effect=fake_error_funnel)
        )
        stack.enter_context(
            patch(f"{SUPERAGENT_RUN}.astream_superagent_workflow", new=fake_superagent)
        )
        # needs_fallback=None keeps the real probe, reading ``saver``.
        fallback = None
        if needs_fallback is not None:
            fallback = stack.enter_context(
                patch(
                    f"{SUPERAGENT_RUN}.superagent_needs_ptc_fallback",
                    new=AsyncMock(side_effect=needs_fallback)
                    if isinstance(needs_fallback, BaseException)
                    else AsyncMock(return_value=needs_fallback),
                )
            )
        setup.agent_config = MagicMock()
        if saver is not None:
            setup.checkpointer = saver
            run_handle.checkpointer = saver
        executor.get_instance.return_value = manager
        workspaces.get_instance.return_value = workspace_manager

        events = []
        # Past START the workflow re-raises after its error funnel.
        with pytest.raises(Exception) if expect_raise else ExitStack():
            async for event in astream_ptc_workflow(
                request=request,
                thread_id="thread-1",
                run_id="run-1",
                user_input="route these",
                user_id="user-1",
                route=TurnRoute("ws-1", "supervisor"),
                config=config,
                dispatched=True,
                run_metadata={"superagent_runtime": "p"} if lane else None,
                superagent_lane=lane,
            ):
                events.append(event)

    return SimpleNamespace(
        events=events,
        p_calls=p_calls,
        errors=errors,
        manager=manager,
        begin=begin,
        fallback=fallback,
        init_tracking=init_tracking,
        run_handle=run_handle,
        setup=setup,
        keep_model=keep_model,
    )


@pytest.mark.asyncio
async def test_the_p_lane_skips_cancel_stale_and_enters_the_superagent_workflow():
    from src.server.handlers.chat.request_prep import DISPATCH_STARTED_MARKER

    lane = _lane()
    run = await _run_ptc(lane=lane, needs_fallback=False)

    run.manager.cancel_stale_workflow.assert_not_awaited()
    # Probed before admission, and re-checked after START through the run's
    # own checkpointer.
    assert run.fallback.await_args_list == [
        call(checkpointer=run.setup.checkpointer, thread_id="thread-1"),
        call(checkpointer=run.run_handle.checkpointer, thread_id="thread-1"),
    ]
    # The thread's model is kept as on PTC.
    run.keep_model.assert_awaited_once()
    assert run.events == [DISPATCH_STARTED_MARKER, "event: metadata\ndata: {}\n\n"]
    assert len(run.p_calls) == 1
    assert run.p_calls[0]["lane"] is lane
    assert run.p_calls[0]["run_handle"] is run.run_handle
    assert run.p_calls[0]["memory_context_block"] == ""
    # Nothing of the PTC half ran.
    run.init_tracking.assert_not_called()
    assert not run.errors
    stamp = run.begin.await_args.kwargs["extra_run_metadata"]
    assert stamp["superagent_runtime"] == "p"


@pytest.mark.asyncio
async def test_a_pending_interrupt_falls_back_to_ptc(caplog):
    from src.server.handlers.chat.request_prep import DISPATCH_STARTED_MARKER

    with caplog.at_level("INFO"):
        run = await _run_ptc(lane=_lane(), needs_fallback=True, expect_raise=True)

    assert not run.p_calls
    assert "[SUPERAGENT_P] fallback=pending_interrupt" in caplog.text
    # The turn went on to the PTC half: its marker, then init_tracking.
    assert run.events == [DISPATCH_STARTED_MARKER]
    run.init_tracking.assert_called_once()
    assert len(run.errors) == 1 and isinstance(run.errors[0], _StopAtPtcHalf)
    # A turn that falls back gets the whole PTC path: stale cancel included,
    # and its START row does not claim the P runtime.
    run.manager.cancel_stale_workflow.assert_awaited_once_with("thread-1")
    assert "superagent_runtime" not in run.begin.await_args.kwargs["extra_run_metadata"]


@pytest.mark.asyncio
async def test_a_failing_fallback_probe_falls_back_to_ptc(caplog):
    with caplog.at_level("WARNING"):
        run = await _run_ptc(
            lane=_lane(), needs_fallback=ConnectionError("store down"), expect_raise=True
        )
    assert not run.p_calls
    assert "[SUPERAGENT_P] fallback=probe_error" in caplog.text
    run.init_tracking.assert_called_once()
    run.manager.cancel_stale_workflow.assert_awaited_once_with("thread-1")


@pytest.mark.asyncio
async def test_without_a_lane_a_cold_workspace_still_cancels_stale():
    run = await _run_ptc(lane=None, needs_fallback=True, expect_raise=True)
    run.manager.cancel_stale_workflow.assert_awaited_once_with("thread-1")
    run.fallback.assert_not_awaited()
    assert not run.p_calls


@pytest.mark.asyncio
async def test_a_hitl_resume_on_a_paused_ptc_thread_falls_back_instead_of_409(caplog):
    # A Router thread PTC paused before the flip must still be resumable.
    request = ChatRequest(
        workspace_id="ws-1",
        platform="superagent_router",
        messages=[{"role": "user", "content": "route these"}],
        hitl_response={"int-1": {"decisions": []}},
    )
    with patch(f"{PTC}.process_hitl_response") as hitl, caplog.at_level("INFO"):
        hitl.return_value = SimpleNamespace(
            feedback_action=None, query_content="", metadata={}
        )
        run = await _run_ptc(
            lane=_lane(), needs_fallback=True, request=request, expect_raise=True
        )
    assert not run.p_calls
    assert "[SUPERAGENT_P] fallback=pending_interrupt" in caplog.text
    assert len(run.errors) == 1 and isinstance(run.errors[0], _StopAtPtcHalf)


@pytest.mark.asyncio
async def test_the_p_lane_receives_the_frozen_memory_tool_policy():
    run = await _run_ptc(lane=_lane(), needs_fallback=False)
    assert run.p_calls[0]["memory_tool_policy"] is not None


@pytest.mark.asyncio
async def test_ptc_work_that_appears_between_the_probe_and_start_ends_the_run():
    # The race R8c-1 closes: the pre-admission probe sees a clean thread, then
    # a PTC run on another worker crashes mid-tool (leaving __pregel_tasks)
    # before this turn's START. The P turn must not run on top of it.
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import MemorySaver

    from ptc_agent.agent.main_state import MainAgentState
    from src.server.handlers.chat.error_handling import SuperagentPendingPtcError

    class _Model(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    @tool
    def boom(x: str) -> str:
        """Always fails."""
        raise RuntimeError("tool crashed")

    saver = MemorySaver()

    async def ptc_crashes_on_another_worker():
        call_ = {"name": "boom", "args": {"x": "1"}, "id": "c1", "type": "tool_call"}
        agent = create_agent(
            _Model(messages=iter([AIMessage(content="", tool_calls=[call_], id="a1")])),
            tools=[boom],
            state_schema=MainAgentState,
            checkpointer=saver,
        )
        with pytest.raises(RuntimeError):
            await agent.ainvoke(
                {"messages": [HumanMessage(content="go", id="h1")]},
                {"configurable": {"thread_id": "thread-1"}},
            )
        tuple_ = await saver.aget_tuple({"configurable": {"thread_id": "thread-1", "checkpoint_ns": ""}})
        assert tuple_.checkpoint["channel_values"].get("__pregel_tasks")

    run = await _run_ptc(
        lane=_lane(),
        needs_fallback=None,
        saver=saver,
        before_start=ptc_crashes_on_another_worker,
        expect_raise=True,
    )

    # The pre-admission probe passed, so stale cancel was skipped and START
    # committed with the P stamp; the re-check then ended the turn.
    run.manager.cancel_stale_workflow.assert_not_awaited()
    assert not run.p_calls
    assert [type(e) for e in run.errors] == [SuperagentPendingPtcError]
    # Refused before the dispatch marker and before any PTC half or model.
    assert run.events == []
    run.init_tracking.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [{"checkpoint_id": "ckpt-1"}, {"hitl_response": {"int-1": {"decisions": []}}}],
)
async def test_replay_and_hitl_are_refused_before_the_dispatch_marker(extra):
    from fastapi import HTTPException

    request = ChatRequest(
        workspace_id="ws-1",
        platform="superagent_router",
        messages=[{"role": "user", "content": "route these"}],
        **extra,
    )
    with patch(f"{PTC}.process_hitl_response") as hitl:
        hitl.return_value = SimpleNamespace(
            feedback_action=None, query_content="", metadata={}
        )
        run = await _run_ptc(
            lane=_lane(), needs_fallback=False, request=request, expect_raise=True
        )
    assert not run.p_calls
    assert run.events == []
    assert [type(e) for e in run.errors] == [HTTPException]
    assert run.errors[0].status_code == 409
