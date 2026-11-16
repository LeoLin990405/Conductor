#!/usr/bin/env python3
"""Run two Main Agent tickets offline using SQLite and concurrent mock workers."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

# Direct execution sets sys.path to scripts/, not the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.server.database.main_agent import SQLiteMainAgentRepository
from src.server.services.main_agent.adapters import DeliberationAdapter, GateAdapter
from src.server.services.main_agent.orchestrator import MainAgentOrchestrator


class DemoGate:
    async def __call__(self, ticket: dict[str, Any]) -> dict[str, Any]:
        return {"required": False, "questions": [], "ticket": ticket.get("objective")}


class DemoDivision:
    async def divide(self, request: dict[str, Any]) -> list[dict[str, Any]]:
        # Independent roots must finish before their join task can be claimed.
        ticket = str(request["objective"]).replace(" ", "-").lower()
        return [
            {"id": "inspect", "description": f"Inspect {ticket}", "depends_on": [], "assigned_agent_id": "worker-a", "deliberation_agent_id": "planner-inspect"},
            {"id": "count", "description": f"Count {ticket}", "depends_on": [], "assigned_agent_id": "worker-b", "deliberation_agent_id": "planner-count"},
            {"id": "join", "description": f"Join {ticket}", "depends_on": ["inspect", "count"], "assigned_agent_id": "worker-join", "deliberation_agent_id": "planner-join"},
        ]


class DemoPlanner:
    async def plan(self, request, tasks, deliberation):
        return {
            "tasks": [dict(task) for task in tasks],
            "rationale": "Two independent checks feed one dependent join.",
            "consensus_plan": deliberation.get("consensus_plan", {}),
        }


class DemoWorkers:
    def __init__(self) -> None:
        self.coordinator: MainAgentOrchestrator | None = None
        self.active = 0
        self.max_active = 0
        self.completed: set[tuple[str, str]] = set()
        self.worker_execution_order: list[dict[str, str]] = []

    async def execute(self, run_id: str, user_id: str, task: dict[str, Any]) -> None:
        if self.coordinator is None:
            raise RuntimeError("worker callback is not connected to coordinator")
        task_id = str(task["id"])
        agent_id = str(task["assigned_agent_id"])
        daemon_id = str(task.get("daemon_id") or "")
        claim_token = str(task["claim_token"])
        if daemon_id != "demo-daemon":
            raise AssertionError("mock worker claim lost its daemon identity")
        dependencies = list(task.get("depends_on") or [])
        if task_id == "join" and any((run_id, dep) not in self.completed for dep in dependencies):
            raise AssertionError("join task was dispatched before its dependencies succeeded")
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.worker_execution_order.append({"run_id": run_id, "task_id": task_id, "daemon_id": daemon_id})
        try:
            await self.coordinator.heartbeat_task(
                run_id, user_id, task_id, agent_id, claim_token=claim_token,
                current_step=f"Running mock callback for {task_id}",
            )
            await asyncio.sleep(0.03)
            result = {"task_id": task_id, "summary": f"Mock result for {task['description']}"}
            await self.coordinator.complete_task(
                run_id, user_id, task_id, agent_id, result, claim_token=claim_token
            )
            self.completed.add((run_id, task_id))
        finally:
            self.active -= 1


class DemoMerger:
    async def merge(self, request, tasks):
        return {
            "objective": request["objective"],
            "summaries": [task["result"]["summary"] for task in tasks],
        }


class DemoReply:
    async def synthesize(self, request, merged):
        return {"content": f"Completed {request['objective']}: " + "; ".join(merged["summaries"])}


async def _deliberate(ticket, tasks):
    return {
        "conclusions": [
            {
                "agent_id": task["deliberation_agent_id"],
                "task_id": task["id"],
                "conclusion": f"Review {task['id']} within {len(tasks)} planned tasks.",
            }
            for task in tasks
        ],
        "consensus_plan": {"parallel_roots": ["inspect", "count"], "join": "join"},
    }


async def run_demo(db_path: str | Path) -> dict[str, Any]:
    repository = SQLiteMainAgentRepository(db_path)
    workers = DemoWorkers()
    coordinator = MainAgentOrchestrator(
        repository=repository,
        clarification_gate=GateAdapter(DemoGate()),
        divider=DemoDivision(),
        deliberation=DeliberationAdapter(_deliberate),
        planner=DemoPlanner(),
        merger=DemoMerger(),
        reply_synthesizer=DemoReply(),
    )
    workers.coordinator = coordinator
    user_id = "offline-demo"
    runs = await asyncio.gather(
        coordinator.submit(user_id, {"objective": "Ticket Alpha"}),
        coordinator.submit(user_id, {"objective": "Ticket Beta"}),
    )
    run_ids = [str(run["id"]) for run in runs]
    daemon_id = "demo-daemon"
    for _ in range(12):
        candidates = [
            (run_id, task)
            for run_id in run_ids
            for task in await coordinator.ready_tasks(run_id, user_id)
        ]
        claims = await asyncio.gather(*(
            coordinator.claim_task(
                run_id, user_id, str(task["id"]), str(task.get("assigned_agent_id") or "worker-pool"),
                daemon_id=daemon_id,
            )
            for run_id, task in candidates
        ))
        workers.coordinator = coordinator
        await asyncio.gather(*(
            workers.execute(run_id, user_id, claim)
            for (run_id, _task), claim in zip(candidates, claims)
        ))
        # The production outbox effect runner calls this same service method;
        # the offline demo simulates that effect without a client API request.
        await asyncio.gather(*(coordinator.tick(run_id, user_id) for run_id in run_ids))
        runs = await asyncio.gather(*(coordinator.get(run_id, user_id) for run_id in run_ids))
        if all(run.get("status") == "completed" for run in runs):
            break
    final_runs = [await coordinator.get(run_id, user_id) for run_id in run_ids]
    if not all(run.get("status") == "completed" for run in final_runs):
        raise RuntimeError("offline demo did not complete all tickets")
    if workers.max_active < 2:
        raise RuntimeError("offline demo did not demonstrate concurrent worker callbacks")
    return {
        "mode": "offline SQLite + mock worker callbacks",
        "completion_path": "scheduler outbox advance effect",
        "max_concurrent_workers": workers.max_active,
        "worker_execution_order": workers.worker_execution_order,
        "runs": [
            {"run_id": run["id"], "status": run["status"], "phase": run["phase"], "reply": run["reply"]}
            for run in final_runs
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="Keep the SQLite demo database at this path; default uses a temporary directory")
    args = parser.parse_args()
    if args.db:
        result = asyncio.run(run_demo(args.db))
    else:
        with tempfile.TemporaryDirectory(prefix="conductor-main-agent-") as temp_dir:
            result = asyncio.run(run_demo(Path(temp_dir) / "main-agent.sqlite3"))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
