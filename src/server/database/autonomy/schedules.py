"""Recurring Super-agent task definitions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection

_COLUMNS = """
    schedule_id, user_id, workspace_id, title, prompt, cron_expression,
    timezone, enabled, worker_provider, worker_model, next_run_at,
    worker_daemon_id, last_run_at, last_error, created_at, updated_at
"""


def _row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["schedule_id"] = str(out["schedule_id"])
    if out.get("worker_daemon_id"):
        out["worker_daemon_id"] = str(out["worker_daemon_id"])
    return out


async def list_schedules(user_id: str) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM autonomy_schedules WHERE user_id = %s ORDER BY created_at DESC",
                (user_id,),
            )
            return [_row(row) for row in await cur.fetchall() if row]  # type: ignore[misc]


async def get_schedule(user_id: str, schedule_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM autonomy_schedules WHERE user_id = %s AND schedule_id = %s",
                (user_id, schedule_id),
            )
            return _row(await cur.fetchone())


async def create_schedule(
    *,
    user_id: str,
    workspace_id: str,
    title: str,
    prompt: str,
    cron_expression: str,
    timezone: str,
    next_run_at: datetime,
    worker_provider: str = "",
    worker_model: str = "",
    worker_daemon_id: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO autonomy_schedules (
                    user_id, workspace_id, title, prompt, cron_expression,
                    timezone, next_run_at, worker_provider, worker_model,
                    worker_daemon_id, enabled
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {_COLUMNS}
                """,
                (
                    user_id, workspace_id, title, prompt, cron_expression,
                    timezone, next_run_at, worker_provider, worker_model,
                    worker_daemon_id, enabled,
                ),
            )
            return _row(await cur.fetchone())  # type: ignore[return-value]


async def update_schedule(
    user_id: str,
    schedule_id: str,
    **fields: Any,
) -> dict[str, Any] | None:
    allowed = {
        "title", "prompt", "workspace_id", "cron_expression", "timezone", "enabled",
        "worker_provider", "worker_model", "next_run_at", "last_run_at",
        "last_error", "worker_daemon_id",
    }
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"unsupported schedule field {key!r}")
        sets.append(f"{key} = %s")
        params.append(value)
    if not sets:
        return await get_schedule(user_id, schedule_id)
    sets.append("updated_at = NOW()")
    params.extend([user_id, schedule_id])
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE autonomy_schedules SET {", ".join(sets)}
                WHERE user_id = %s AND schedule_id = %s
                RETURNING {_COLUMNS}
                """,
                params,
            )
            return _row(await cur.fetchone())


async def delete_schedule(user_id: str, schedule_id: str) -> bool:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM autonomy_schedules WHERE user_id = %s AND schedule_id = %s",
                (user_id, schedule_id),
            )
            return cur.rowcount > 0


async def due_schedules(*, limit: int = 50) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM autonomy_schedules
                WHERE enabled AND next_run_at <= NOW()
                ORDER BY next_run_at
                LIMIT %s
                """,
                (limit,),
            )
            return [_row(row) for row in await cur.fetchall() if row]  # type: ignore[misc]
