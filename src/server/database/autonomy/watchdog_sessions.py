"""Persistence for the one hidden Watchdog Session per user."""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection


async def get_watchdog_session(user_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT user_id, workspace_id, thread_id, created_at
                FROM autonomy_watchdog_sessions
                WHERE user_id = %s
                """,
                (user_id,),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {**dict(row), "thread_id": str(row["thread_id"])}


async def save_watchdog_session(
    user_id: str, *, workspace_id: str, thread_id: str
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO autonomy_watchdog_sessions
                    (user_id, workspace_id, thread_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO NOTHING
                RETURNING user_id, workspace_id, thread_id, created_at
                """,
                (user_id, workspace_id, thread_id),
            )
            row = await cur.fetchone()
            if row is None:
                await cur.execute(
                    """
                    SELECT user_id, workspace_id, thread_id, created_at
                    FROM autonomy_watchdog_sessions
                    WHERE user_id = %s
                    """,
                    (user_id,),
                )
                row = await cur.fetchone()
    if row is None:
        raise RuntimeError("watchdog session was not saved")
    return {**dict(row), "thread_id": str(row["thread_id"])}
