from unittest.mock import AsyncMock

import pytest

from src.server.services.autonomy import scheduler as scheduler_module
from src.server.services.autonomy.effects import PermanentEffectError
from src.server.services.autonomy.models import EFFECT_MAIN_AGENT_ADVANCE
from src.server.services.autonomy.scheduler import AutonomyScheduler
from src.server.services.main_agent.production import (
    ExecutionContextError,
    SourceUnavailableError,
)


@pytest.mark.asyncio
async def test_scheduler_enqueues_bounded_idempotent_coordinator_work(monkeypatch):
    class Repository:
        async def list_runs(self, user_id, statuses, limit, *, oldest_first=False):
            assert user_id is None
            assert statuses == ["accepted", "queued", "running"]
            assert limit == 5000
            assert oldest_first is False
            waiting = [
                {
                    "id": f"waiting-{index}",
                    "user_id": "u1",
                    "phase": "track",
                    "status": "running",
                    "revision": index,
                    "tasks": [{"id": "live", "status": "running"}],
                }
                for index in range(30)
            ]
            return waiting + [
                {
                    "id": "done",
                    "user_id": "u2",
                    "phase": "track",
                    "status": "running",
                    "revision": 3,
                    "tasks": [{"id": "t1", "status": "succeeded"}],
                },
                {
                    "id": "failed-with-child",
                    "user_id": "u5",
                    "phase": "execute",
                    "status": "running",
                    "revision": 6,
                    "tasks": [
                        {"id": "parent", "status": "failed"},
                        {"id": "child", "status": "queued", "depends_on": ["parent"]},
                    ],
                },
                {
                    "id": "planning",
                    "user_id": "u3",
                    "phase": "deliberate",
                    "status": "accepted",
                    "revision": 4,
                    "tasks": [],
                },
                {
                    "id": "waiting-user",
                    "user_id": "u4",
                    "phase": "clarify",
                    "status": "clarification_required",
                    "revision": 5,
                    "tasks": [],
                },
            ]

    enqueue = AsyncMock(side_effect=[True, False, True])
    monkeypatch.setattr(scheduler_module, "enqueue_effect", enqueue)
    orchestrator = type("Orchestrator", (), {})()
    orchestrator.repository = Repository()
    orchestrator.tick = AsyncMock()
    scheduler = AutonomyScheduler.__new__(AutonomyScheduler)
    scheduler.main_agent_orchestrator = orchestrator

    assert await scheduler._enqueue_main_agent_advances() == 2
    assert enqueue.await_count == 3
    assert enqueue.await_args_list[0].kwargs == {
        "user_id": "u2",
        "kind": EFFECT_MAIN_AGENT_ADVANCE,
        "idempotency_key": "main-agent:advance:done:track:3",
        "payload": {"run_id": "done", "user_id": "u2"},
    }
    assert (
        enqueue.await_args_list[1].kwargs["idempotency_key"]
        == "main-agent:advance:failed-with-child:execute:6"
    )
    assert (
        enqueue.await_args_list[2].kwargs["idempotency_key"]
        == "main-agent:advance:planning:deliberate:4"
    )
    orchestrator.tick.assert_not_awaited()


@pytest.mark.asyncio
async def test_main_agent_advance_effect_runs_one_isolated_tick():
    orchestrator = type("Orchestrator", (), {})()
    orchestrator.tick = AsyncMock(return_value={"status": "completed"})
    scheduler = AutonomyScheduler.__new__(AutonomyScheduler)
    scheduler.main_agent_orchestrator = orchestrator
    effect = type("Effect", (), {"payload": {"run_id": "r1", "user_id": "u1"}})()

    await scheduler._main_agent_advance_effect(effect)

    orchestrator.tick.assert_awaited_once_with("r1", "u1")


@pytest.mark.asyncio
async def test_missing_source_is_classified_as_permanent_effect_failure():
    orchestrator = type("Orchestrator", (), {})()
    orchestrator.tick = AsyncMock(
        side_effect=SourceUnavailableError("Source is not found or is disabled")
    )
    scheduler = AutonomyScheduler.__new__(AutonomyScheduler)
    scheduler.main_agent_orchestrator = orchestrator
    effect = type("Effect", (), {"payload": {"run_id": "r1", "user_id": "u1"}})()

    with pytest.raises(PermanentEffectError, match="Source is not found or is disabled"):
        await scheduler._main_agent_advance_effect(effect)

    orchestrator.tick.assert_awaited_once_with("r1", "u1")


@pytest.mark.asyncio
async def test_other_execution_context_errors_remain_retryable():
    orchestrator = type("Orchestrator", (), {})()
    error = ExecutionContextError("invalid daemon configuration")
    orchestrator.tick = AsyncMock(side_effect=error)
    scheduler = AutonomyScheduler.__new__(AutonomyScheduler)
    scheduler.main_agent_orchestrator = orchestrator
    effect = type("Effect", (), {"payload": {"run_id": "r1", "user_id": "u1"}})()

    with pytest.raises(ExecutionContextError) as raised:
        await scheduler._main_agent_advance_effect(effect)

    assert raised.value is error
