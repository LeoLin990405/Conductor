"""Plan DAG validation and parallel handoff waves."""

from __future__ import annotations

import pytest

from src.server.services.deliberation.dag import handoff_ready_steps, plan_to_dag
from src.server.services.deliberation.errors import DeliberationError


def _plan_step(
    step_key: str,
    depends_on: list[str],
    assigned_agent: str = "codex",
) -> dict:
    return {
        "step_key": step_key,
        "depends_on": depends_on,
        "assigned_agent": assigned_agent,
    }


def _pending(
    step_key: str,
    depends_on: list[str],
    assigned_agent: str,
) -> dict:
    step = _plan_step(step_key, depends_on, assigned_agent)
    step["status"] = "pending"
    return step


def _dispatched_keys(handoff: dict) -> set[str]:
    return {step["step_key"] for step in handoff["dispatched"]}


def test_cycle_is_rejected() -> None:
    plan = {
        "steps": [
            _plan_step("a", ["b"], "codex"),
            _plan_step("b", ["a"], "grok"),
        ]
    }
    with pytest.raises(DeliberationError) as caught:
        plan_to_dag(plan, deliberation_id="d1")
    assert caught.value.code == "invalid_plan"


def test_unknown_dependency_is_rejected() -> None:
    plan = {
        "steps": [
            _plan_step("spec", [], "codex"),
            _plan_step("impl", ["missing"], "kimi"),
        ]
    }
    with pytest.raises(DeliberationError) as caught:
        plan_to_dag(plan, deliberation_id="d1")
    assert caught.value.code == "invalid_plan"


def test_parallel_waves() -> None:
    steps = [
        _pending("spec", [], "codex"),
        _pending("impl", [], "kimi"),
        _pending("review", ["spec", "impl"], "grok"),
    ]
    first = handoff_ready_steps(steps)
    assert first["parallel"] is True
    assert _dispatched_keys(first) == {"spec", "impl"}
    review = next(step for step in steps if step["step_key"] == "review")
    assert review["status"] == "pending"

    for step in steps:
        if step["step_key"] in {"spec", "impl"}:
            step["status"] = "completed"
    second = handoff_ready_steps(steps)
    assert _dispatched_keys(second) == {"review"}
    assert second["parallel"] is False

    blocked = [
        _pending("spec", [], "codex"),
        _pending("impl", ["spec"], "kimi"),
    ]
    ready = handoff_ready_steps(blocked)
    assert _dispatched_keys(ready) == {"spec"}
    impl = next(step for step in blocked if step["step_key"] == "impl")
    assert impl["status"] == "pending"
