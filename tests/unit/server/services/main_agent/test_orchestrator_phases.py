"""Phase progression and LLM adapter contracts without live model calls."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.server.database.main_agent import SQLiteMainAgentRepository
from src.server.services.main_agent.adapters import (
    DeliberationAdapter,
    LLMMainAgentAdapter,
)
from src.server.services.main_agent.models import InvalidPlan, MainAgentThresholds, TaskOwnershipError
from src.server.services.main_agent.orchestrator import MainAgentOrchestrator, validate_dag


class Gate:
    async def check(self, request):
        return {"required": True, "questions": ["Which region?"], "ticket": request}


class Divider:
    def __init__(self):
        self.requests = []
        self.fail_once = False

    async def divide(self, request):
        self.requests.append(dict(request))
        if self.fail_once:
            self.fail_once = False
            raise TimeoutError("temporary model timeout")
        return [{"id": "fetch", "description": "fetch evidence", "depends_on": []}]


class Deliberation:
    async def deliberate(self, request, tasks):
        del request
        return {
            "conclusions": [
                {
                    "agent_id": task.get("deliberation_agent_id") or "research-a",
                    "task_id": task["id"],
                    "conclusion": "use primary sources",
                }
                for task in tasks
            ],
            "consensus_plan": {"decision": "research then report"},
        }


class Planner:
    def __init__(self):
        self.deliberations = []

    async def plan(self, request, tasks, deliberation):
        del request
        self.deliberations.append(deliberation)
        return {"tasks": [dict(task) for task in tasks], "consensus": deliberation["consensus_plan"]}


class Merger:
    def __init__(self):
        self.calls = 0

    async def merge(self, request, tasks):
        self.calls += 1
        return {"objective": request["objective"], "results": [task["result"] for task in tasks]}


class Reply:
    def __init__(self):
        self.calls = 0

    async def synthesize(self, request, merged):
        self.calls += 1
        return {"content": f"{request['objective']}: {len(merged['results'])} result"}


async def _service(tmp_path, *, gate=None, divider=None):
    values = {
        "repository": SQLiteMainAgentRepository(tmp_path / "main-agent.sqlite3"),
        "divider": divider or Divider(),
        "deliberation": Deliberation(),
        "planner": Planner(),
        "merger": Merger(),
        "reply_synthesizer": Reply(),
        "clarification_gate": gate,
    }
    return MainAgentOrchestrator(**values), values


@pytest.mark.asyncio
async def test_clarification_answer_flows_into_division_and_consensus(tmp_path):
    service, ports = await _service(tmp_path, gate=Gate())
    run = await service.submit("owner", {"objective": "compare regions"})
    assert run["phase"] == "clarify"
    assert run["status"] == "clarification_required"
    assert run["clarification"]["questions"] == ["Which region?"]

    run = await service.answer_clarification(run["id"], "owner", {"answer": "APAC"})
    assert run["phase"] == "execute"
    assert [
        {key: task[key] for key in ("id", "description", "depends_on")}
        for task in run["division"]
    ] == [{"id": "fetch", "description": "fetch evidence", "depends_on": []}]
    assert ports["divider"].requests[0]["clarification_answer"] == {"answer": "APAC"}
    assert ports["planner"].deliberations[0]["consensus_plan"] == {"decision": "research then report"}


@pytest.mark.asyncio
async def test_failed_planning_call_leaves_phase_resumable(tmp_path):
    divider = Divider()
    divider.fail_once = True
    service, _ports = await _service(tmp_path, divider=divider)
    with pytest.raises(TimeoutError):
        await service.submit("owner", {"objective": "find evidence"})

    created = (await service.repository.list_runs("owner"))[0]
    assert created["phase"] == "divide"

    resumed = await service.advance(created["id"], "owner")
    assert resumed["phase"] == "execute"
    assert resumed["tasks"][0]["status"] == "queued"


def test_dag_validator_rejects_duplicate_missing_and_cyclic_tasks():
    with pytest.raises(InvalidPlan, match="unique"):
        validate_dag([{"id": "a"}, {"id": "a"}])
    with pytest.raises(InvalidPlan, match="missing"):
        validate_dag([{"id": "a", "depends_on": ["missing"]}])
    with pytest.raises(InvalidPlan, match="cycle"):
        validate_dag([{"id": "a", "depends_on": ["b"]}, {"id": "b", "depends_on": ["a"]}])


def test_dag_discards_worker_execution_metadata_from_planner():
    task = validate_dag([{
        "id": "a",
        "revision": 88,
        "attempt": 9,
        "attempt_history": [{"secret": "discard"}],
        "retryable": True,
        "assigned_agent_id": "model-chosen-worker",
        "daemon_id": "model-chosen-daemon",
        "claim_token": "model-invented-token",
        "current_step": "already running",
        "deliberation_agent_id": "research-agent",
    }])[0]
    assert task["revision"] == task["attempt"] == 0
    assert task["attempt_history"] == []
    assert task["retryable"] is False
    assert task["assigned_agent_id"] is task["daemon_id"] is task["claim_token"] is None
    assert task["current_step"] is None
    assert task["deliberation_agent_id"] == "research-agent"


@pytest.mark.asyncio
async def test_deliberation_adapter_requires_each_assigned_participant():
    async def partial_round(request, tasks):
        del request, tasks
        return {
            "conclusions": [{"agent_id": "research-a", "task_id": "a", "conclusion": "evidence"}],
            "consensus_plan": {},
        }

    adapter = DeliberationAdapter(partial_round)
    with pytest.raises(ValueError, match="research-b"):
        await adapter.deliberate({}, [
            {"id": "a", "deliberation_agent_id": "research-a"},
            {"id": "b", "deliberation_agent_id": "research-b"},
        ])

    async def complete_round(request, tasks):
        del request, tasks
        return {
            "conclusions": [
                {"agent_id": "research-a", "task_id": "a", "conclusion": "evidence"},
                {"agent_id": "research-b", "task_id": "b", "conclusion": "risks"},
            ],
            "consensus_plan": {"decision": "compare"},
        }

    result = await DeliberationAdapter(complete_round).deliberate({}, [
        {"id": "a", "deliberation_agent_id": "research-a"},
        {"id": "b", "deliberation_agent_id": "research-b"},
    ])
    assert len(result["conclusions"]) == 2


@pytest.mark.asyncio
async def test_llm_adapter_uses_user_aware_primary_schema_calls():
    class FakeLLM:
        def __init__(self):
            self.calls = []
            self.responses = [
                {"tasks": [{"id": "t1", "description": "research", "depends_on": []}]},
                {"tasks": [{"id": "t1", "description": "research", "depends_on": []}], "rationale": "consensus"},
                {"summary": "Evidence found", "findings": ["one source"]},
                {"content": "Here is the result."},
            ]

        async def complete(self, **kwargs):
            self.calls.append(kwargs)
            return kwargs["response_schema"](**self.responses.pop(0))

    llm = FakeLLM()
    adapter = LLMMainAgentAdapter(llm, timeout_seconds=2)
    request = {"user_id": "owner", "objective": "research"}
    tasks = await adapter.divide(request)
    plan = await adapter.plan(request, tasks, {"conclusions": [{"agent_id": "a", "conclusion": "yes"}]})
    merged = await adapter.merge(request, [{"status": "succeeded", "result": {"source": "x"}}])
    reply = await adapter.synthesize(request, merged)

    assert plan["rationale"] == "consensus"
    assert reply["content"] == "Here is the result."
    assert len(llm.calls) == 4
    assert all(call["user_id"] == "owner" and call["slot"] == "primary" for call in llm.calls)
    assert all(call["response_schema"] is not None for call in llm.calls)
    assert all('"user_id"' not in call["user_prompt"] for call in llm.calls)


@pytest.mark.asyncio
async def test_watchdog_heartbeat_boundary_and_progress_dedup_without_requeue(tmp_path):
    service, _ports = await _service(tmp_path)
    service.thresholds = MainAgentThresholds(
        heartbeat_stale_seconds=20, progress_stale_seconds=30, lease_seconds=120
    )
    run = await service.submit("owner", {"objective": "watch"})
    await service.claim_task(run["id"], "owner", "fetch", "worker-a")
    # A live heartbeat cannot hide genuinely stale progress.
    now = datetime.now(UTC)
    current = await service.get(run["id"], "owner")
    task = dict(current["tasks"][0])
    task.update(
        heartbeat_at=(now - timedelta(seconds=1)).isoformat(),
        progress_at=(now - timedelta(seconds=31)).isoformat(),
        lease_until=(now - timedelta(seconds=1)).isoformat(),
    )
    await service.repository.transition_run(
        run["id"], current["revision"], {"tasks": [task]}
    )

    first = await service.watchdog_sweep("owner")
    assert len(first) == 1
    assert first[0]["kind"] == "progress_stalled"
    assert first[0]["task_id"] == "fetch"
    assert await service.watchdog_sweep("owner") == []
    # Expiry creates an alert but does not give another worker the task claim.
    with pytest.raises(TaskOwnershipError):
        await service.claim_task(run["id"], "owner", "fetch", "worker-b")


@pytest.mark.asyncio
async def test_watchdog_heartbeat_threshold_and_finished_tasks(tmp_path):
    service, _ports = await _service(tmp_path)
    service.thresholds = MainAgentThresholds(
        heartbeat_stale_seconds=10, progress_stale_seconds=100, lease_seconds=120
    )
    run = await service.submit("owner", {"objective": "watch"})
    await service.claim_task(run["id"], "owner", "fetch", "worker-a")
    current = await service.get(run["id"], "owner")
    task = dict(current["tasks"][0])
    boundary = datetime.now(UTC) - timedelta(seconds=10)
    task.update(heartbeat_at=boundary.isoformat(), progress_at=boundary.isoformat())
    await service.repository.transition_run(run["id"], current["revision"], {"tasks": [task]})
    alerts = await service.watchdog_sweep("owner")
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "heartbeat_stalled"

    task_now = await service.get(run["id"], "owner")
    token = task_now["tasks"][0]["claim_token"]
    await service.complete_task(run["id"], "owner", "fetch", "worker-a", {"ok": True}, claim_token=token)
    assert await service.watchdog_sweep("owner") == []


@pytest.mark.asyncio
async def test_idle_tick_is_stable_and_completed_tick_does_not_resynthesize(tmp_path):
    service, ports = await _service(tmp_path)
    run = await service.submit("owner", {"objective": "finish once"})
    run = await service.tick(run["id"], "owner")
    revision = run["revision"]
    second = await service.tick(run["id"], "owner")
    assert second["revision"] == revision

    task = (await service.ready_tasks(run["id"], "owner"))[0]
    claim = await service.claim_task(run["id"], "owner", task["id"], "worker-a")
    await service.complete_task(
        run["id"], "owner", task["id"], "worker-a", {"ok": True}, claim_token=claim["claim_token"]
    )
    completed = await service.tick(run["id"], "owner")
    assert completed["status"] == "completed"
    assert ports["merger"].calls == ports["reply_synthesizer"].calls == 1
    assert (await service.tick(run["id"], "owner"))["reply"] == completed["reply"]
    assert ports["merger"].calls == ports["reply_synthesizer"].calls == 1
