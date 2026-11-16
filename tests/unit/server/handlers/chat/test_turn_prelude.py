"""The shared turn prelude and tail (R8c ruling 7, R9b L01).

``prepare_turn`` admits a turn and opens its run before any lane runs;
``TurnTail`` wraps every lane: a setup disconnect fails the run open, an
error goes to ``handle_workflow_error``, and the admission lock is released
last on every exit. Collaborators are looked up through ``ptc_run``, so its
patch targets keep working.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.server.handlers.chat.request_prep import PriorThread
from src.server.handlers.chat.turn_prelude import TurnPrelude, TurnTail, prepare_turn
from src.server.models.chat import ChatRequest
from src.server.services.turn_runtime import TurnRoute

PTC = "src.server.handlers.chat.ptc_run"
POLICY = "src.server.services.refactor.memory_tool_policy.load_memory_tool_policy"
MEMORY_ON = (
    "src.server.services.refactor.memory_runtime_controls.automatic_memory_enabled"
)


class _Scope:
    """A RunScope stand-in that records the order of its calls."""

    def __init__(self, calls: list[str], *, slot_owned: bool = True) -> None:
        self.calls = calls
        self.slot_owned = slot_owned
        self.handed_off = False
        self.owned_run_handle = None

    def hold_admission(self, lock) -> None:
        self.calls.append("hold_admission")

    async def release_slot(self) -> None:
        self.calls.append("release_slot")

    def release_admission(self) -> None:
        self.calls.append("release_admission")

    async def fail_open(self, reason: str) -> None:
        self.calls.append(f"fail_open:{reason}")


def _tail(calls: list[str], *, slot_owned: bool = True, request=None) -> TurnTail:
    request = request or ChatRequest(workspace_id="ws-1", messages=[])
    with patch(f"{PTC}.RunScope", return_value=_Scope(calls, slot_owned=slot_owned)):
        return TurnTail(
            request=request,
            thread_id="t-1",
            user_id="u-1",
            workspace_id="ws-1",
            start_time=0.0,
            is_byok=False,
        )


def _request() -> ChatRequest:
    return ChatRequest(
        workspace_id="ws-1", messages=[{"role": "user", "content": "hi"}]
    )


def _config():
    config = MagicMock()
    config.llm.name = "claude-test"
    return config


def _prelude_patches(stack_calls: list[str], *, ready=(True, None), begin=None):
    executor = MagicMock()
    executor.get_admission_lock = AsyncMock(return_value=asyncio.Lock())
    executor.cancel_stale_workflow = AsyncMock()
    workspaces = MagicMock()
    workspaces.has_ready_session.return_value = True
    handle = MagicMock(turn_index=0)
    policy = MagicMock(native_runtime_allowed=True, deny_all=False)
    return [
        patch(f"{PTC}.setup"),
        patch(f"{PTC}.LocalRunExecutor.get_instance", return_value=executor),
        patch(f"{PTC}.WorkspaceManager.get_instance", return_value=workspaces),
        patch(f"{PTC}.dedup_retransmit_or_raise", new=AsyncMock()),
        patch(f"{PTC}.wait_or_steer", new=AsyncMock(return_value=ready)),
        patch(f"{PTC}.ensure_thread", new=AsyncMock(return_value=PriorThread())),
        patch(f"{PTC}.read_disk_notice", new=AsyncMock(return_value=(None, False))),
        patch(f"{PTC}.get_user_profile_for_prompt", new=AsyncMock(return_value=None)),
        patch(f"{PTC}._resolve_fork", return_value=("initial", None)),
        patch(
            f"{PTC}.begin_run",
            new=begin or AsyncMock(return_value=handle),
        ),
        patch(POLICY, new=AsyncMock(return_value=policy)),
        patch(MEMORY_ON, new=AsyncMock(return_value=False)),
    ]


async def _prepare(tail: TurnTail, request: ChatRequest) -> TurnPrelude:
    return await prepare_turn(
        tail,
        request=request,
        thread_id="t-1",
        run_id="r-1",
        user_input="hi",
        user_id="u-1",
        route=TurnRoute("ws-1", "generalist"),
        is_byok=False,
        config=_config(),
        dispatched=False,
        steerable=True,
        run_metadata=None,
        superagent_lane=None,
    )


def _enter(patches):
    import contextlib

    stack = contextlib.ExitStack()
    entered = [stack.enter_context(p) for p in patches]
    return stack, entered


@pytest.mark.asyncio
async def test_prepare_turn_opens_the_run_and_records_it_on_the_tail():
    calls: list[str] = []
    tail = _tail(calls)
    request = _request()
    stack, entered = _enter(_prelude_patches(calls))
    with stack:
        prelude = await _prepare(tail, request)

    assert prelude.done is False and prelude.early_events == []
    assert prelude.run_handle is tail.run_handle is not None
    assert prelude.turn_context is tail.turn_context is not None
    assert prelude.request is tail.request
    # Preference off: the frozen no-memory answer is an empty block.
    assert prelude.memory_enabled is False
    assert prelude.memory_context_block == ""
    assert prelude.effective_model == "claude-test"
    # The lock is held for the lane; only the tail releases it.
    assert calls == ["hold_admission"]


@pytest.mark.asyncio
async def test_a_steer_ends_the_turn_in_the_prelude_after_releasing_admission():
    calls: list[str] = []
    tail = _tail(calls)
    stack, entered = _enter(_prelude_patches(calls, ready=(False, "steered-event")))
    with stack:
        ensure = entered[5]
        prelude = await _prepare(tail, _request())

    assert prelude.done is True
    assert prelude.early_events == ["steered-event"]
    assert calls == ["hold_admission", "release_slot", "release_admission"]
    ensure.assert_not_awaited()
    assert tail.run_handle is None


@pytest.mark.asyncio
async def test_a_busy_refusal_without_an_event_yields_nothing():
    calls: list[str] = []
    tail = _tail(calls)
    stack, _ = _enter(_prelude_patches(calls, ready=(False, None)))
    with stack:
        prelude = await _prepare(tail, _request())

    assert prelude.done is True and prelude.early_events == []


@pytest.mark.asyncio
async def test_a_start_failure_leaves_the_turn_context_for_the_error_path():
    calls: list[str] = []
    tail = _tail(calls)
    stack, _ = _enter(
        _prelude_patches(calls, begin=AsyncMock(side_effect=RuntimeError("boom")))
    )
    with stack, pytest.raises(RuntimeError):
        await _prepare(tail, _request())

    assert tail.turn_context is not None
    assert tail.run_handle is None


@pytest.mark.asyncio
async def test_the_tail_fails_a_setup_disconnect_open_before_releasing():
    calls: list[str] = []
    tail = _tail(calls, slot_owned=True)
    with pytest.raises(asyncio.CancelledError):
        async with tail:
            raise asyncio.CancelledError

    assert calls == [
        "fail_open:client disconnected during setup",
        "release_admission",
    ]


@pytest.mark.asyncio
async def test_the_tail_leaves_a_handed_off_run_alone_on_disconnect():
    calls: list[str] = []
    tail = _tail(calls, slot_owned=False)
    with pytest.raises(GeneratorExit):
        async with tail:
            raise GeneratorExit

    assert calls == ["release_admission"]


@pytest.mark.asyncio
async def test_the_tail_releases_on_success_and_on_error():
    calls: list[str] = []
    async with _tail(calls):
        pass
    assert calls == ["release_admission"]

    calls.clear()
    with pytest.raises(ValueError):
        async with _tail(calls):
            raise ValueError("x")
    assert calls == ["release_admission"]


@pytest.mark.asyncio
async def test_error_events_go_through_handle_workflow_error_with_tail_state():
    calls: list[str] = []
    retried = ChatRequest(workspace_id="ws-1", messages=[], platform="slack")
    tail = _tail(calls)
    tail.request = retried
    tail.turn_context = "ctx"
    seen = {}

    async def _handle(e, **kwargs):
        seen.update(kwargs, e=e)
        calls.append("error_frame")
        yield "error-frame"

    err = RuntimeError("boom")
    events = []
    with patch(f"{PTC}.handle_workflow_error", new=_handle):
        with pytest.raises(RuntimeError):
            async with tail:
                try:
                    raise err
                except Exception as e:
                    async for event in tail.error_events(
                        e, handler="h", token_callback="tc"
                    ):
                        events.append(event)
                    raise

    assert events == ["error-frame"]
    assert seen["e"] is err
    assert seen["request"] is retried
    assert seen["turn_context"] == "ctx"
    assert (seen["handler"], seen["token_callback"]) == ("h", "tc")
    assert seen["scope"] is tail.scope
    assert calls == ["error_frame", "release_admission"]


@pytest.mark.asyncio
async def test_a_disconnect_while_error_frames_stream_is_not_a_setup_disconnect():
    calls: list[str] = []
    tail = _tail(calls, slot_owned=True)

    async def _handle(e, **kwargs):
        yield "error-frame"

    with patch(f"{PTC}.handle_workflow_error", new=_handle):
        with pytest.raises(GeneratorExit):
            async with tail:
                try:
                    raise RuntimeError("boom")
                except Exception as e:
                    tail.error_events(e, handler=None, token_callback=None)
                    raise GeneratorExit

    assert calls == ["release_admission"]


@pytest.mark.asyncio
async def test_astream_ptc_workflow_yields_the_steer_event_and_releases():
    """End to end through ptc_run: the steer frame is the only event."""
    from src.server.handlers.chat.ptc_run import astream_ptc_workflow

    calls: list[str] = []
    stack, _ = _enter(_prelude_patches(calls, ready=(False, "steered-event")))
    with stack, patch(f"{PTC}.RunScope", return_value=_Scope(calls)):
        events = [
            event
            async for event in astream_ptc_workflow(
                request=_request(),
                thread_id="t-1",
                run_id="r-1",
                user_input="hi",
                user_id="u-1",
                route=TurnRoute("ws-1", "generalist"),
                config=_config(),
            )
        ]

    assert events == ["steered-event"]
    # Released by the prelude, then once more (idempotently) by the tail.
    assert calls == [
        "hold_admission",
        "release_slot",
        "release_admission",
        "release_admission",
    ]


async def _prepare_as(tail: TurnTail, request: ChatRequest, *, role: str, user_input: str) -> TurnPrelude:
    return await prepare_turn(
        tail,
        request=request,
        thread_id="t-1",
        run_id="r-1",
        user_input=user_input,
        user_id="u-1",
        route=TurnRoute("ws-1", role),
        is_byok=False,
        config=_config(),
        dispatched=True,
        steerable=True,
        run_metadata=None,
        superagent_lane=None,
    )


def _memory_on_patches(calls: list[str], build: AsyncMock):
    patches = [p for p in _prelude_patches(calls) if getattr(p, "attribute", None) != "automatic_memory_enabled"]
    patches.append(patch(MEMORY_ON, new=AsyncMock(return_value=True)))
    patches.append(
        patch("src.server.services.refactor.memory_context.build_turn_memory_block", new=build)
    )
    patches.append(
        patch(
            "src.server.services.refactor.dual_write.record_memory_injection",
            new=AsyncMock(),
        )
    )
    return patches


@pytest.mark.asyncio
async def test_a_worker_turn_never_injects_the_prompt_derived_memory_block():
    """P0: the Ticket prompt is the user message; recall on it would skip the Ticket gate."""
    from src.server.services.refactor.memory_context import ContextEntry, MemoryContext

    leaked = ContextEntry(
        tier="user",
        entry_key="m-private",
        index_text="[private] resume and recommendation letter",
        content_hash="h",
        tokens=4,
        rank=0,
        origin="user",
        confidence=0.9,
    )
    build = AsyncMock(
        return_value=(MemoryContext(query="q", budget_tokens=800, entries=(leaked,)), leaked.render())
    )
    calls: list[str] = []
    stack, _ = _enter(_memory_on_patches(calls, build))
    with stack:
        prelude = await _prepare_as(
            _tail(calls), _request(), role="worker", user_input="Ticket delivery contract ..."
        )

    assert prelude.memory_context_block == ""
    build.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_router_turn_goes_through_the_egress_gate():
    from src.server.services.refactor.memory_context import ContextEntry, MemoryContext

    private = ContextEntry(
        tier="user",
        entry_key="m-private",
        index_text="［ＰＲＩＶＡＴＥ］ payment notes",
        content_hash="h",
        tokens=4,
        rank=0,
        origin="user",
        confidence=0.9,
    )
    build = AsyncMock(
        return_value=(MemoryContext(query="q", budget_tokens=800, entries=(private,)), private.render())
    )
    calls: list[str] = []
    stack, _ = _enter(_memory_on_patches(calls, build))
    with stack:
        prelude = await _prepare_as(
            _tail(calls), _request(), role="supervisor", user_input="plan the payment notes cleanup"
        )

    assert callable(build.await_args.kwargs["row_filter"])
    assert "PRIVATE" not in prelude.memory_context_block.upper()


@pytest.mark.asyncio
async def test_render_switch_off_suppresses_the_ungated_legacy_baseline():
    build = AsyncMock(return_value=None)
    calls: list[str] = []
    stack, _ = _enter(_memory_on_patches(calls, build))
    with stack:
        prelude = await _prepare_as(
            _tail(calls), _request(), role="supervisor", user_input="plan the cleanup"
        )
    assert prelude.memory_context_block == ""
