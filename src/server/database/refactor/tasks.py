"""Persisted agent task tree (SQL layer, migration 090).

The tree is a projection, not the run ledger: ``source='subagent'`` rows mirror a
delegated task via ``subagent_task_ref`` and never carry ``subagent_runs`` state.
Status moves are plain updates (the tree is advisory UI/audit state); the run
ledger keeps the authoritative lifecycle, so a stale tree row must never block a
turn.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from psycopg.rows import dict_row

from src.server.database import pool
from src.server.utils.pg_sanitize import normalize_uuid, refactor_owner_uuid, strip_pg_nul_str

_TASK_COLUMNS = (
    "task_node_id, user_id, thread_id, parent_node_id, title, status, source,"
    " subagent_task_ref, created_at, updated_at"
)


def _normalized_id(value: object, field: str) -> str | None:
    if value is None:
        return None
    normalized = normalize_uuid(value)
    if normalized is None:
        raise ValueError(f"{field} is not a UUID")
    return normalized


async def insert_task(
    *,
    user_id: str,
    thread_id: str,
    title: str,
    status: str = "pending",
    source: str = "todo",
    parent_node_id: str | None = None,
    subagent_task_ref: str | None = None,
) -> dict[str, Any]:
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        raise ValueError("user_id is required")
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO agent_tasks
                    (user_id, thread_id, parent_node_id, title, status, source,
                     subagent_task_ref)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING {_TASK_COLUMNS}
                """,
                (
                    owner,
                    _normalized_id(thread_id, "thread_id"),
                    _normalized_id(parent_node_id, "parent_node_id"),
                    strip_pg_nul_str(title)[:500] if title else "",
                    status,
                    source,
                    strip_pg_nul_str(subagent_task_ref),
                ),
            )
            return dict(await cur.fetchone())


async def update_task_status(*, task_node_id: str, status: str) -> dict[str, Any] | None:
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE agent_tasks SET status = %s, updated_at = NOW()
                 WHERE task_node_id = %s
                RETURNING {_TASK_COLUMNS}
                """,
                (status, _normalized_id(task_node_id, "task_node_id")),
            )
            return dict(row) if (row := await cur.fetchone()) else None


async def list_tasks(*, thread_id: str, limit: int = 500) -> list[dict[str, Any]]:
    if limit < 1 or limit > 2000:
        raise ValueError("task page size must be between 1 and 2000")
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_TASK_COLUMNS} FROM agent_tasks"
                " WHERE thread_id = %s ORDER BY created_at, task_node_id LIMIT %s",
                (_normalized_id(thread_id, "thread_id"), limit),
            )
            return [dict(row) for row in await cur.fetchall()]


async def sync_todo_snapshot(
    *,
    thread_id: str,
    todos: Sequence[Mapping[str, str]],
) -> list[dict[str, Any]] | None:
    """Reconcile one TodoWrite snapshot into ``agent_tasks``.

    Identity is ``(thread_id, title)`` (partial unique index from migration 091):
    a title present in the snapshot is upserted with its new status; a todo row
    still open but absent from the snapshot is cancelled. An empty snapshot
    therefore cancels every open todo. Returns the thread's todo rows after the
    sync, or ``None`` when the thread has no workspace (unknown owner).

    The owner is resolved in SQL from the thread, so callers in the SSE producer
    do not need to carry a user id onto the stream path.
    """
    titles = [str(todo["title"]) for todo in todos]
    statuses = [str(todo["status"]) for todo in todos]
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT w.user_id::text AS user_id"
                " FROM conversation_threads t"
                " JOIN workspaces w ON w.workspace_id = t.workspace_id"
                " WHERE t.conversation_thread_id = %s",
                (_normalized_id(thread_id, "thread_id"),),
            )
            owner = await cur.fetchone()
            if owner is None:
                return None
            # Workspace owners are text (OSS uses a non-UUID sentinel); the task
            # table is UUID-typed, so map through the same owner helper every
            # refactor surface uses.
            user_id = refactor_owner_uuid(owner["user_id"])
            if user_id is None:
                return None

            if titles:
                await cur.execute(
                    """
                    INSERT INTO agent_tasks
                        (user_id, thread_id, title, status, source)
                    SELECT %s, %s, snapshot.title, snapshot.status, 'todo'
                    FROM unnest(%s::text[], %s::text[]) AS snapshot(title, status)
                    ON CONFLICT (thread_id, title) WHERE source = 'todo'
                    DO UPDATE SET status = EXCLUDED.status, updated_at = NOW()
                    """,
                    (user_id, _normalized_id(thread_id, "thread_id"), titles, statuses),
                )
                await cur.execute(
                    "UPDATE agent_tasks SET status = 'cancelled', updated_at = NOW()"
                    " WHERE thread_id = %s AND source = 'todo'"
                    "   AND status NOT IN ('completed', 'cancelled')"
                    "   AND title <> ALL(%s::text[])",
                    (_normalized_id(thread_id, "thread_id"), titles),
                )
            else:
                await cur.execute(
                    "UPDATE agent_tasks SET status = 'cancelled', updated_at = NOW()"
                    " WHERE thread_id = %s AND source = 'todo'"
                    "   AND status NOT IN ('completed', 'cancelled')",
                    (_normalized_id(thread_id, "thread_id"),),
                )

            await cur.execute(
                f"SELECT {_TASK_COLUMNS} FROM agent_tasks"
                " WHERE thread_id = %s AND source = 'todo'"
                " ORDER BY created_at, task_node_id",
                (_normalized_id(thread_id, "thread_id"),),
            )
            return [dict(row) for row in await cur.fetchall()]
