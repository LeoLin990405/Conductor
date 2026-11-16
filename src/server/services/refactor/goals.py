"""Dual-write entry point for goals and plans.

The service validates the contract and the state machine; the SQL layer performs
compare-and-set moves. Flag off → no-op ``None`` so a deployment can keep the old
behavior until the new readers exist.

Error taxonomy is deliberately small: ``ContractError`` for invalid input/state,
and the two store outcomes (not found, lost CAS race) as subclasses so a caller
can distinguish "retry with fresh state" from "the request was wrong".
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from src.server.contracts.refactor import (
    ContractError,
    GoalSpec,
    ensure_goal_transition,
    ensure_plan_transition,
)
from src.server.database.refactor import goals as db_goals
from src.server.services.refactor_flags import refactor_flag_enabled


class GoalNotFoundError(ContractError):
    """No goal row with that id."""


class GoalTransitionConflictError(ContractError):
    """Another writer moved the goal between our read and our CAS."""


class PlanNotFoundError(ContractError):
    """No plan row with that id."""


async def create_goal(
    *,
    user_id: str,
    thread_id: str,
    spec: GoalSpec | Mapping[str, Any],
    workspace_id: str | None = None,
) -> dict[str, Any] | None:
    """Create a live goal, or ``None`` when the ``goals`` flag is off.

    Raises ``ContractError`` when the thread already has a live goal (the
    partial unique index is the real arbiter; this surfaces it as a typed
    outcome rather than a raw unique violation).
    """
    if not refactor_flag_enabled("goals"):
        return None
    goal = spec if isinstance(spec, GoalSpec) else GoalSpec.from_mapping(spec)
    row = await db_goals.insert_goal(
        user_id=user_id,
        thread_id=thread_id,
        spec=goal.to_record(),
        workspace_id=workspace_id,
    )
    if row is None:
        raise ContractError("thread already has a live goal")
    return row


async def transition_goal(
    *,
    goal_id: str,
    target_status: str,
    verifier_verdict: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """CAS-move a goal after validating the transition, or no-op when flag off."""
    if not refactor_flag_enabled("goals"):
        return None
    current = await db_goals.get_goal(goal_id)
    if current is None:
        raise GoalNotFoundError(f"goal {goal_id} not found")
    ensure_goal_transition(current["status"], target_status)
    row = await db_goals.transition_goal(
        goal_id=goal_id,
        expected_status=current["status"],
        target_status=target_status,
        verifier_verdict=verifier_verdict,
    )
    if row is None:
        raise GoalTransitionConflictError(
            f"goal {goal_id} moved from {current['status']} while transitioning"
        )
    return row


async def create_plan(
    *,
    goal_id: str,
    steps: Sequence[Mapping[str, Any]],
    version: int = 1,
) -> dict[str, Any] | None:
    """Insert a plan version; ``None`` when the flag is off or the version exists."""
    if not refactor_flag_enabled("goals"):
        return None
    goal = await db_goals.get_goal(goal_id)
    if goal is None:
        raise GoalNotFoundError(f"goal {goal_id} not found")
    return await db_goals.insert_plan(
        goal_id=goal_id,
        version=version,
        steps=[dict(step) for step in steps],
    )


async def decide_plan(
    *,
    plan_id: str,
    approve: bool,
    feedback: str | None = None,
) -> dict[str, Any] | None:
    """Approve/reject a plan under review; ``None`` when flag off or version exists."""
    if not refactor_flag_enabled("goals"):
        return None
    current = await db_goals.get_plan(plan_id)
    if current is None:
        raise PlanNotFoundError(f"plan {plan_id} not found")
    target = "approved" if approve else "rejected"
    ensure_plan_transition(current["status"], target)
    row = await db_goals.decide_plan(
        plan_id=plan_id,
        expected_status=current["status"],
        target_status=target,
        feedback=feedback,
    )
    if row is None:
        raise GoalTransitionConflictError(
            f"plan {plan_id} moved from {current['status']} while deciding"
        )
    return row
