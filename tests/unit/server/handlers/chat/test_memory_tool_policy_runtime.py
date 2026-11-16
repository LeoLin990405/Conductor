"""The owner policy guards native dispatch independently of memory recall."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.server.contracts.memory_tool_policy import MemoryToolPolicySnapshot, ToolIdentifier
from src.server.models.chat import ChatRequest
from src.server.services.turn_runtime import TurnRoute

PTC = "src.server.handlers.chat.ptc_run"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "expected_code", "expected_message"),
    [
        (
            MemoryToolPolicySnapshot(
                denied_tools=(ToolIdentifier(type="tool", name="Bash"),),
                digest="synthetic-policy",
            ),
            "memory_tool_policy_active",
            "owner-approved memory constraint",
        ),
        (
            MemoryToolPolicySnapshot(deny_all=True, digest="synthetic-failure"),
            "memory_tool_policy_unavailable",
            "policy is unavailable",
        ),
    ],
)
async def test_denied_policy_blocks_native_runtime_after_start_even_when_memory_is_off(
    policy, expected_code, expected_message
):
    from src.server.handlers.chat.ptc_run import astream_ptc_workflow

    request = ChatRequest(
        workspace_id="ws-1", messages=[{"role": "user", "content": "hello"}]
    )
    manager = MagicMock()
    manager.get_admission_lock = AsyncMock(return_value=asyncio.Lock())
    manager.cancel_stale_workflow = AsyncMock()
    workspace_manager = MagicMock()
    workspace_manager.has_ready_session.return_value = True
    # Both a verified active rule and a fail-closed read failure block the
    # native path; only the former is described as an owner-approved rule.
    run_handle = MagicMock(run_id="run-1", turn_index=1, finalized=False)
    turn_context = MagicMock(inherits_rules=False, tool_timezone="UTC")
    refusals = []
    native_calls = []

    async def fake_error_funnel(error, **_kwargs):
        refusals.append(error)
        yield "event: error\ndata: policy refused\n\n"

    async def fake_native(**_kwargs):
        native_calls.append(True)
        yield "event: native\ndata: started\n\n"

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
        stack.enter_context(patch(f"{PTC}.init_tracking", return_value=(None, None)))
        stack.enter_context(patch(f"{PTC}.begin_run", new=AsyncMock(return_value=run_handle)))
        load_policy = stack.enter_context(
            patch(
                "src.server.services.refactor.memory_tool_policy.load_memory_tool_policy",
                new=AsyncMock(return_value=policy),
            )
        )
        memory_enabled = stack.enter_context(
            patch(
                "src.server.services.refactor.memory_runtime_controls.automatic_memory_enabled",
                new=AsyncMock(return_value=False),
            )
        )
        stack.enter_context(
            patch(
                f"{PTC}.get_thread_metadata",
                new=AsyncMock(return_value={"native_runtime": {"provider": "codex"}}),
            )
        )
        stack.enter_context(patch(f"{PTC}.astream_native_workflow", side_effect=fake_native))
        stack.enter_context(
            patch(f"{PTC}.handle_workflow_error", side_effect=fake_error_funnel)
        )
        setup.agent_config = MagicMock()
        executor.get_instance.return_value = manager
        workspaces.get_instance.return_value = workspace_manager

        events = []
        try:
            async for event in astream_ptc_workflow(
                request=request,
                thread_id="thread-1",
                run_id="run-1",
                user_input="hello",
                user_id="user-1",
                route=TurnRoute("ws-1", "generalist"),
                config=config,
                dispatched=True,
            ):
                events.append(event)
        except Exception:
            # The workflow surfaces the SSE frame and then re-raises the
            # terminal exception for the detached executor's error boundary.
            pass

    load_policy.assert_awaited_once_with(user_id="user-1")
    memory_enabled.assert_awaited_once_with("user-1")
    assert not native_calls
    assert len(refusals) == 1
    assert type(refusals[0]).__name__ == "MemoryToolPolicyRefusal"
    assert refusals[0].code == expected_code
    assert expected_message in str(refusals[0])
    from src.server.handlers.chat.request_prep import DISPATCH_STARTED_MARKER

    assert events[0] == DISPATCH_STARTED_MARKER
    assert any("event: error" in event for event in events)
