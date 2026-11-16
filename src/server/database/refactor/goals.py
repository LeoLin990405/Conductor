"""Goal and plan queries for the deep refactor (SQL layer).

Goals are a Phase-1 addition (migration 086). The status vocabulary and legal
moves live in ``src.server.contracts.refactor``; this module only persists a move
the service layer already validated. Every update is a compare-and-set on the
expected status, so two workers cannot both "complete" a goal: the loser gets
``None`` and the service re-reads instead of overwriting the winner.
"""

from __future__ import annotations

from typing import Any, Mapping

from psycopg.rows import dict_row

from src.server.database import pool
from src.server.utils.pg_sanitize import (
    SafeJson,
    normalize_uuid,
    refactor_owner_uuid,
    strip_pg_nul_str,
)

#: ``agent_goals`` columns the API/UI needs; ``SELECT *`` would leak nothing today
#: but pins the shape here so a future column does not silently reach a client.
_GOAL_COLUMNS = (
    "goal_id, user_id, thread_id, workspace_id, title, objective,"
    " stop_conditions, status, budget, verifier_config, verifier_verdict,"
    " created_at, updated_at, completed_at"
)
_PLAN_COLUMNS = (
    "plan_id, goal_id, version, steps, status, feedback, created_at, decided_at"
)


def _normalized_id(value: object, field: str) -> str:
    normalized = normalize_uuid(value)
    if normalized is None:
        raise ValueError(f"{field} is not a UUID")
    return normalized


def _row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


async def insert_goal(
    *,
    user_id: str,
    thread_id: str,
    spec: Mapping[str, Any],
    workspace_id: str | None = None,
) -> dict[str, Any] | None:
    """Create a live goal. ``None`` when the thread already has a live goal.

    The partial unique index (thread_id where active/paused) is the arbiter;
    ``ON CONFLICT DO NOTHING`` turns the race into a typed no-result instead of
    an exception the caller must parse.
    """
    workspace = _normalized_id(workspace_id, "workspace_id") if workspace_id else None
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        raise ValueError("user_id is required")
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO agent_goals
                    (user_id, thread_id, workspace_id, title, objective,
                     stop_conditions, budget, verifier_config)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING {_GOAL_COLUMNS}
                """,
                (
                    owner,
                    _normalized_id(thread_id, "thread_id"),
                    workspace,
                    strip_pg_nul_str(str(spec["title"])),
                    strip_pg_nul_str(str(spec["objective"])),
                    SafeJson(list(spec["stop_conditions"])),
                    SafeJson(dict(spec.get("budget") or {})),
                    SafeJson(dict(spec.get("verifier_config") or {})),
                ),
            )
            return _row(await cur.fetchone())


async def get_goal(goal_id: str) -> dict[str, Any] | None:
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_GOAL_COLUMNS} FROM agent_goals WHERE goal_id = %s",
                (_normalized_id(goal_id, "goal_id"),),
            )
            return _row(await cur.fetchone())


async def get_live_goal(thread_id: str) -> dict[str, Any] | None:
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_GOAL_COLUMNS} FROM agent_goals"
                " WHERE thread_id = %s AND status IN ('active', 'paused')"
                " ORDER BY created_at DESC LIMIT 1",
                (_normalized_id(thread_id, "thread_id"),),
            )
            return _row(await cur.fetchone())


async def transition_goal(
    *,
    goal_id: str,
    expected_status: str,
    target_status: str,
    verifier_verdict: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """CAS-move a goal, returning the new row (or None if another writer won).

    ``completed_at`` is stamped only by terminal moves; a pause/resume cycle
    keeps the original completion time absent.
    """
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE agent_goals
                   SET status = %s,
                       updated_at = NOW(),
                       completed_at = CASE
                           WHEN %s IN ('completed', 'cancelled') THEN NOW()
                           ELSE completed_at
                       END,
                       verifier_verdict = COALESCE(%s::jsonb, verifier_verdict)
                 WHERE goal_id = %s AND status = %s
                RETURNING {_GOAL_COLUMNS}
                """,
                (
                    target_status,
                    target_status,
                    SafeJson(dict(verifier_verdict)) if verifier_verdict else None,
                    _normalized_id(goal_id, "goal_id"),
                    expected_status,
                ),
            )
            return _row(await cur.fetchone())


async def insert_plan(
    *,
    goal_id: str,
    version: int,
    steps: list[Mapping[str, Any]],
    status: str = "draft",
) -> dict[str, Any] | None:
    """Insert one plan version; ``None`` when that version already exists."""
    if version < 1:
        raise ValueError("plan version must be >= 1")
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO agent_plans (goal_id, version, steps, status)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (goal_id, version) DO NOTHING
                RETURNING {_PLAN_COLUMNS}
                """,
                (
                    _normalized_id(goal_id, "goal_id"),
                    int(version),
                    SafeJson([dict(step) for step in steps]),
                    status,
                ),
            )
            return _row(await cur.fetchone())


async def get_plan(plan_id: str) -> dict[str, Any] | None:
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_PLAN_COLUMNS} FROM agent_plans WHERE plan_id = %s",
                (_normalized_id(plan_id, "plan_id"),),
            )
            return _row(await cur.fetchone())


async def get_latest_plan(goal_id: str) -> dict[str, Any] | None:
    """The highest-version plan for a goal (the one a review surface shows)."""
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_PLAN_COLUMNS} FROM agent_plans"
                " WHERE goal_id = %s ORDER BY version DESC LIMIT 1",
                (_normalized_id(goal_id, "goal_id"),),
            )
            return _row(await cur.fetchone())


async def decide_plan(
    *,
    plan_id: str,
    expected_status: str,
    target_status: str,
    feedback: str | None = None,
) -> dict[str, Any] | None:
    """CAS the plan's review decision; ``None`` means another writer won."""
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE agent_plans
                   SET status = %s,
                       feedback = COALESCE(%s, feedback),
                       decided_at = NOW()
                 WHERE plan_id = %s AND status = %s
                RETURNING {_PLAN_COLUMNS}
                """,
                (
                    target_status,
                    strip_pg_nul_str(feedback),
                    _normalized_id(plan_id, "plan_id"),
                    expected_status,
                ),
            )
            return _row(await cur.fetchone())
