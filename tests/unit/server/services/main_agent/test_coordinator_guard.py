from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.server.database.main_agent import SQLiteMainAgentRepository
from src.server.services.main_agent.models import CoordinatorBusy
from src.server.services.main_agent.orchestrator import MainAgentOrchestrator


class GuardedRepository:
    """Shared durable-store fake with nonblocking cross-instance ownership."""

    def __init__(self, repository):
        self.repository = repository
        self.locks: dict[str, asyncio.Lock] = {}

    def __getattr__(self, name):
        return getattr(self.repository, name)

    @asynccontextmanager
    async def run_guard(self, run_id):
        lock = self.locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            yield False
            return
        await lock.acquire()
        try:
            yield True
        finally:
            lock.release()


class BlockingDivider:
    def __init__(self):
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.calls = 0

    async def divide(self, _request):
        self.calls += 1
        self.started.set()
        await self.finish.wait()
        return [{"id": "task", "description": "work", "depends_on": []}]


class PassThrough:
    async def deliberate(self, _request, tasks):
        return {
            "conclusions": [
                {
                    "agent_id": task.get("deliberation_agent_id") or "research-a",
                    "task_id": task["id"],
                    "conclusion": "ready",
                }
                for task in tasks
            ],
            "consensus_plan": {},
        }


class Planner:
    async def plan(self, _request, tasks, _deliberation):
        return {"tasks": tasks}


class Merger:
    async def merge(self, _request, tasks):
        return {"results": [task.get("result") for task in tasks]}


class Reply:
    async def synthesize(self, _request, _merged):
        return {"content": "done"}


class ConfirmationGate:
    def __init__(self):
        self.confirm_calls = 0

    async def check(self, _request):
        return {"required": True, "questions": ["Clarify"]}

    async def confirm(self, _request, _answer):
        self.confirm_calls += 1
        return {"status": "queued"}


def _orchestrator(repository, divider, gate=None):
    return MainAgentOrchestrator(
        repository=repository,
        divider=divider,
        deliberation=PassThrough(),
        planner=Planner(),
        merger=Merger(),
        reply_synthesizer=Reply(),
        clarification_gate=gate,
    )


async def _create_run(repository, *, phase="divide", status="accepted", request=None):
    return await repository.create_run(
        "owner",
        {
            "id": "guarded-run",
            "user_id": "owner",
            "status": status,
            "revision": 0,
            "request": request or {"objective": "work"},
            "phase": phase,
            "clarification": None,
            "deliberation": None,
            "plan": None,
            "division": [],
            "merged_result": None,
            "reply": None,
            "tasks": [],
            "alerts": [],
            "created_at": "2026-11-02T00:00:00+00:00",
            "updated_at": "2026-11-02T00:00:00+00:00",
        },
    )


@pytest.mark.asyncio
async def test_two_orchestrators_skip_duplicate_adapter_work_while_guard_is_held(
    tmp_path,
):
    repository = GuardedRepository(SQLiteMainAgentRepository(tmp_path / "runs.sqlite"))
    await _create_run(repository)
    first_divider = BlockingDivider()
    second_divider = BlockingDivider()
    first = _orchestrator(repository, first_divider)
    second = _orchestrator(repository, second_divider)

    owner = asyncio.create_task(first.advance("guarded-run", "owner"))
    await first_divider.started.wait()
    loser_snapshot = await second.advance("guarded-run", "owner")

    assert loser_snapshot["phase"] == "divide"
    assert first_divider.calls == 1
    assert second_divider.calls == 0

    first_divider.finish.set()
    result = await owner
    assert result["phase"] == "execute"


@pytest.mark.asyncio
async def test_clarification_contention_is_explicit_409_and_does_not_drop_answer(
    tmp_path,
):
    repository = GuardedRepository(SQLiteMainAgentRepository(tmp_path / "runs.sqlite"))
    gate = ConfirmationGate()
    service = _orchestrator(repository, BlockingDivider(), gate)
    await _create_run(
        repository,
        phase="clarify",
        status="clarification_required",
        request={"objective": "work", "context": {"ticket_id": "ticket-1"}},
    )

    async with repository.run_guard("guarded-run") as acquired:
        assert acquired
        with pytest.raises(CoordinatorBusy, match="retry this request") as caught:
            await service.answer_clarification(
                "guarded-run", "owner", {"answer": "the requested scope"}
            )

    from src.server.app.main_agent import _translate

    response = _translate(caught.value)
    assert response.status_code == 409
    assert response.detail["code"] == "coordinator_busy"
    assert gate.confirm_calls == 0
    run = await repository.get_run("guarded-run", "owner")
    assert run["status"] == "clarification_required"
    assert run["clarification"] is None


@pytest.mark.asyncio
async def test_tick_contention_raises_retryable_conflict_without_adapter_work(tmp_path):
    repository = GuardedRepository(SQLiteMainAgentRepository(tmp_path / "runs.sqlite"))
    await _create_run(repository)
    divider = BlockingDivider()
    service = _orchestrator(repository, divider)

    async with repository.run_guard("guarded-run") as acquired:
        assert acquired
        with pytest.raises(CoordinatorBusy) as caught:
            await service.tick("guarded-run", "owner")

    from src.server.app.main_agent import _translate

    response = _translate(caught.value)
    assert response.status_code == 409
    assert response.detail["code"] == "coordinator_busy"
    assert divider.calls == 0


@pytest.mark.asyncio
async def test_report_hook_leaves_child_sync_to_guarded_tick():
    from src.server.services.main_agent import bridge_sync

    orchestrator = SimpleNamespace(tick=AsyncMock())
    app = SimpleNamespace(state=SimpleNamespace(main_agent_orchestrator=orchestrator))
    direct_sync = AsyncMock()
    with (
        patch("src.server.app.setup.app", app),
        patch.object(bridge_sync, "sync_child_ticket_report", direct_sync),
    ):
        await bridge_sync.sync_report_and_reconcile(
            {"run_id": "run-1", "user_id": "owner", "task_id": "task-1"}
        )

    orchestrator.tick.assert_awaited_once_with("run-1", "owner")
    direct_sync.assert_not_awaited()
