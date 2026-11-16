"""Worker claims, progress, dependencies, and callbacks against real SQLite."""

import asyncio

import pytest

from src.server.database.main_agent import SQLiteMainAgentRepository
from src.server.services.main_agent.adapters import PassthroughClarificationGate
from src.server.services.main_agent.models import RunNotFound, TaskOwnershipError
from src.server.services.main_agent.orchestrator import MainAgentOrchestrator


class Divider:
    def __init__(self, tasks):
        self.tasks = tasks

    async def divide(self, request):
        del request
        return [dict(task) for task in self.tasks]


class Deliberation:
    async def deliberate(self, request, tasks):
        del request
        return {
            "conclusions": [
                {
                    "agent_id": task["deliberation_agent_id"],
                    "task_id": task["id"],
                    "conclusion": f"Review {task['id']}",
                }
                for task in tasks
            ],
            "consensus_plan": {"task_ids": [task["id"] for task in tasks]},
        }


class Planner:
    async def plan(self, request, tasks, deliberation):
        del request, deliberation
        return {"tasks": [dict(task) for task in tasks]}


class Merger:
    def __init__(self):
        self.calls = 0

    async def merge(self, request, tasks):
        self.calls += 1
        return {"objective": request["objective"], "results": [task.get("result") for task in tasks]}


class Reply:
    async def synthesize(self, request, merged):
        return {"content": f"{request['objective']}: {len(merged['results'])} results"}


def tasks(*rows):
    return [
        {
            "id": task_id,
            "description": task_id,
            "depends_on": dependencies,
            "assigned_agent_id": agent_id,
            "deliberation_agent_id": f"planner-{task_id}",
        }
        for task_id, dependencies, agent_id in rows
    ]


async def coordinator_for(tmp_path, task_rows):
    merger = Merger()
    coordinator = MainAgentOrchestrator(
        repository=SQLiteMainAgentRepository(tmp_path / "runs.sqlite3"),
        clarification_gate=PassthroughClarificationGate(),
        divider=Divider(task_rows),
        deliberation=Deliberation(),
        planner=Planner(),
        merger=merger,
        reply_synthesizer=Reply(),
    )
    run = await coordinator.submit("owner", {"objective": "review"})
    return coordinator, merger, run


@pytest.mark.asyncio
async def test_wrong_user_and_wrong_worker_cannot_claim_or_complete(tmp_path):
    coordinator, _merger, run = await coordinator_for(tmp_path, tasks(("task", [], "worker-a")))
    run_id = run["id"]

    with pytest.raises(RunNotFound):
        await coordinator.ready_tasks(run_id, "other-user")
    with pytest.raises(RunNotFound):
        await coordinator.claim_task(run_id, "other-user", "task", "worker-a")

    claim = await coordinator.claim_task(run_id, "owner", "task", "worker-a")
    with pytest.raises(TaskOwnershipError):
        await coordinator.complete_task(
            run_id, "owner", "task", "worker-b", {"ok": True}, claim_token=claim["claim_token"]
        )


@pytest.mark.asyncio
async def test_expired_attempt_token_cannot_complete_after_same_worker_retries(tmp_path):
    coordinator, _merger, run = await coordinator_for(tmp_path, tasks(("task", [], "worker-a")))
    run_id = run["id"]
    first = await coordinator.claim_task(run_id, "owner", "task", "worker-a")
    await coordinator.fail_task(
        run_id, "owner", "task", "worker-a", {"message": "retry me"},
        retryable=True, claim_token=first["claim_token"],
    )
    failed = await coordinator.tick(run_id, "owner")
    assert failed["status"] == "failed"
    await coordinator.retry_task(run_id, "owner", "task")
    reopened = await coordinator.get(run_id, "owner")
    assert reopened["status"] == "queued"
    assert reopened["phase"] == "execute"
    second = await coordinator.claim_task(run_id, "owner", "task", "worker-a")
    assert second["claim_token"] != first["claim_token"]

    with pytest.raises(TaskOwnershipError):
        await coordinator.complete_task(
            run_id, "owner", "task", "worker-a", {"stale": True}, claim_token=first["claim_token"]
        )
    await coordinator.complete_task(
        run_id, "owner", "task", "worker-a", {"ok": True}, claim_token=second["claim_token"]
    )


@pytest.mark.asyncio
async def test_parallel_roots_unlock_child_and_merge_waits_for_all_tasks(tmp_path):
    coordinator, merger, run = await coordinator_for(
        tmp_path,
        tasks(("root-a", [], "worker-a"), ("root-b", [], "worker-b"), ("join", ["root-a", "root-b"], "worker-c")),
    )
    run_id = run["id"]
    ready = await coordinator.ready_tasks(run_id, "owner")
    assert {task["id"] for task in ready} == {"root-a", "root-b"}
    claims = {
        task_id: await coordinator.claim_task(run_id, "owner", task_id, agent)
        for task_id, agent in [("root-a", "worker-a"), ("root-b", "worker-b")]
    }

    await coordinator.complete_task(
        run_id, "owner", "root-a", "worker-a", {"value": "a"}, claim_token=claims["root-a"]["claim_token"]
    )
    await coordinator.tick(run_id, "owner")
    assert merger.calls == 0
    assert await coordinator.ready_tasks(run_id, "owner") == []

    await coordinator.complete_task(
        run_id, "owner", "root-b", "worker-b", {"value": "b"}, claim_token=claims["root-b"]["claim_token"]
    )
    await coordinator.tick(run_id, "owner")
    child = (await coordinator.ready_tasks(run_id, "owner"))[0]
    assert child["id"] == "join"
    child_claim = await coordinator.claim_task(run_id, "owner", "join", "worker-c")
    await coordinator.complete_task(
        run_id, "owner", "join", "worker-c", {"value": "joined"}, claim_token=child_claim["claim_token"]
    )
    final = await coordinator.tick(run_id, "owner")
    assert final["status"] == "completed"
    assert final["reply"]["content"] == "review: 3 results"
    assert merger.calls == 1


@pytest.mark.asyncio
async def test_repeating_same_step_renews_heartbeat_without_advancing_progress(tmp_path):
    coordinator, _merger, run = await coordinator_for(tmp_path, tasks(("task", [], "worker-a")))
    run_id = run["id"]
    claim = await coordinator.claim_task(run_id, "owner", "task", "worker-a")
    first = await coordinator.heartbeat_task(
        run_id, "owner", "task", "worker-a", current_step="Reading inputs", claim_token=claim["claim_token"]
    )
    progress_at = first["progress_at"]
    await asyncio.sleep(0.01)
    second = await coordinator.heartbeat_task(
        run_id, "owner", "task", "worker-a", current_step="Reading inputs", claim_token=claim["claim_token"]
    )
    assert second["heartbeat_at"] != first["heartbeat_at"]
    assert second["progress_at"] == progress_at


@pytest.mark.asyncio
async def test_failed_parent_does_not_hide_live_sibling_or_release_child(tmp_path):
    coordinator, merger, run = await coordinator_for(
        tmp_path,
        tasks(("parent", [], "worker-a"), ("sibling", [], "worker-b"), ("child", ["parent"], "worker-c")),
    )
    run_id = run["id"]
    claims = {
        task_id: await coordinator.claim_task(run_id, "owner", task_id, agent)
        for task_id, agent in [("parent", "worker-a"), ("sibling", "worker-b")]
    }
    await coordinator.fail_task(
        run_id, "owner", "parent", "worker-a", {"message": "blocked"},
        claim_token=claims["parent"]["claim_token"],
    )
    midway = await coordinator.tick(run_id, "owner")
    assert midway["status"] == "running"
    assert {task["id"]: task["status"] for task in midway["tasks"]}["sibling"] == "running"
    assert await coordinator.ready_tasks(run_id, "owner") == []
    assert merger.calls == 0

    await coordinator.complete_task(
        run_id, "owner", "sibling", "worker-b", {"ok": True}, claim_token=claims["sibling"]["claim_token"]
    )
    final = await coordinator.tick(run_id, "owner")
    assert final["status"] == "failed"
    assert {task["id"]: task["status"] for task in final["tasks"]}["child"] == "queued"
    assert merger.calls == 0
