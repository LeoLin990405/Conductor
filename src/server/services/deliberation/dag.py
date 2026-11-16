"""Pure plan-to-step DAG helpers. No store and no ticket claims."""

from __future__ import annotations

import uuid

from src.server.services.deliberation.errors import DeliberationError


def plan_to_dag(
    plan: dict,
    *,
    deliberation_id: str,
    daemon_by_agent: dict[str, str | None] | None = None,
) -> list[dict]:
    if not isinstance(plan, dict):
        raise DeliberationError("invalid_plan", "plan steps are required")
    raw_steps = plan.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise DeliberationError("invalid_plan", "plan steps are required")

    normalized: list[tuple[dict, str, str, list[str]]] = []
    seen: set[str] = set()
    for step in raw_steps:
        if not isinstance(step, dict):
            raise DeliberationError("invalid_plan", "step must be an object")
        step_key = step.get("step_key")
        assigned_agent = step.get("assigned_agent")
        if not isinstance(step_key, str) or not step_key.strip():
            raise DeliberationError("invalid_plan", "step_key is required")
        if not isinstance(assigned_agent, str) or not assigned_agent.strip():
            raise DeliberationError("invalid_plan", "assigned_agent is required")
        if step_key in seen:
            raise DeliberationError("invalid_plan", f"duplicate step_key {step_key}")
        seen.add(step_key)
        depends_on = _depends_on(step)
        normalized.append((step, step_key, assigned_agent, depends_on))

    known = {step_key for _, step_key, _, _ in normalized}
    deps = {step_key: depends_on for _, step_key, _, depends_on in normalized}
    for step_key, depends_on in deps.items():
        for dep in depends_on:
            if dep not in known:
                raise DeliberationError(
                    "invalid_plan",
                    f"depends_on references unknown step {dep}",
                )
    _assert_acyclic(deps)

    daemons = daemon_by_agent or {}
    built: list[dict] = []
    for step, step_key, assigned_agent, depends_on in normalized:
        built.append(
            {
                "step_id": str(uuid.uuid4()),
                "deliberation_id": deliberation_id,
                "step_key": step_key,
                "title": str(step.get("title") or ""),
                "prompt": str(step.get("prompt") or ""),
                "assigned_agent": assigned_agent,
                "depends_on": depends_on,
                "status": "pending",
                "daemon_id": daemons.get(assigned_agent),
                "child_ticket_id": None,
            }
        )
    return built


def handoff_ready_steps(steps: list[dict]) -> dict:
    completed = {
        step["step_key"] for step in steps if step.get("status") == "completed"
    }
    dispatched: list[dict] = []
    for step in steps:
        if step.get("status") != "pending":
            continue
        depends_on = step.get("depends_on") or []
        if all(dep in completed for dep in depends_on):
            step["status"] = "running"
            dispatched.append(step)
    return {
        "parallel": len(dispatched) >= 2,
        "dispatched": dispatched,
        "waiting": [step for step in steps if step.get("status") == "pending"],
    }


class RecordingExecutor:
    def __init__(self) -> None:
        self.waves: list[list[dict]] = []

    async def dispatch(self, steps: list[dict]) -> None:
        self.waves.append([dict(step) for step in steps])


def _depends_on(step: dict) -> list[str]:
    raw = step.get("depends_on")
    if raw is None:
        return []
    if isinstance(raw, str) or not isinstance(raw, list):
        raise DeliberationError("invalid_plan", "depends_on must be a list")
    return [str(item) for item in raw]


def _assert_acyclic(deps: dict[str, list[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visited:
            return
        if key in visiting:
            raise DeliberationError("invalid_plan", "plan has a cycle")
        visiting.add(key)
        for dep in deps[key]:
            visit(dep)
        visiting.remove(key)
        visited.add(key)

    for key in deps:
        visit(key)
