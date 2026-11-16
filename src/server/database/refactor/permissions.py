"""Per-thread permission mode (SQL layer, migration 093).

One row per thread that has been set explicitly; absence means the default mode,
so reads never need a seed row and a new thread costs nothing.
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row

from src.server.database import pool
from src.server.utils.pg_sanitize import normalize_uuid


async def get_mode(thread_id: str) -> dict[str, Any] | None:
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT thread_id::text AS thread_id, mode, updated_by, updated_at"
                " FROM agent_thread_permissions WHERE thread_id = %s",
                (normalize_uuid(thread_id),),
            )
            row = await cur.fetchone()
            return dict(row) if row is not None else None


async def set_mode(
    *, thread_id: str, mode: str, updated_by: str | None = None, conn: Any = None
) -> dict[str, Any]:
    normalized = normalize_uuid(thread_id)
    if normalized is None:
        raise ValueError("thread_id is not a UUID")
    async with pool.get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO agent_thread_permissions (thread_id, mode, updated_by)
                VALUES (%s, %s, %s)
                ON CONFLICT (thread_id) DO UPDATE
                   SET mode = EXCLUDED.mode,
                       updated_by = EXCLUDED.updated_by,
                       updated_at = NOW()
                RETURNING thread_id::text AS thread_id, mode, updated_by, updated_at
                """,
                (normalized, mode, updated_by),
            )
            row = await cur.fetchone()
            return dict(row)
