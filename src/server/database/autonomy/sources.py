"""Autonomy Source configuration and polling state."""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.pool import get_db_connection

_SOURCE_COLUMNS = """
    source_id, user_id, kind, config, state, enabled, created_at, updated_at
"""


def _source(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["source_id"] = str(out["source_id"])
    out["config"] = dict(out.get("config") or {})
    out["state"] = dict(out.get("state") or {})
    return out


async def create_source(
    *, user_id: str, kind: str, config: dict[str, Any], enabled: bool = True
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO autonomy_sources (user_id, kind, config, enabled)
                VALUES (%s, %s, %s, %s)
                RETURNING {_SOURCE_COLUMNS}
                """,
                (user_id, kind, Json(config), bool(enabled)),
            )
            return _source(await cur.fetchone())  # type: ignore[return-value]


async def list_sources(user_id: str) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_SOURCE_COLUMNS}
                FROM autonomy_sources
                WHERE user_id = %s
                ORDER BY created_at
                """,
                (user_id,),
            )
            return [_source(row) for row in await cur.fetchall() if row]  # type: ignore[misc]


async def get_source(user_id: str, source_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_SOURCE_COLUMNS}
                FROM autonomy_sources
                WHERE user_id = %s AND source_id = %s
                """,
                (user_id, source_id),
            )
            return _source(await cur.fetchone())


async def get_source_any_user(source_id: str) -> dict[str, Any] | None:
    """Internal poller read; the source row already names its owner."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_SOURCE_COLUMNS} FROM autonomy_sources WHERE source_id = %s",
                (source_id,),
            )
            return _source(await cur.fetchone())


async def update_source(
    user_id: str,
    source_id: str,
    *,
    config: dict[str, Any] | None = None,
    enabled: bool | None = None,
    state: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Update owned fields; ``state`` is merged, not replaced, so a concurrent
    poller touch and a human save cannot erase each other's keys."""
    sets: list[str] = []
    params: list[Any] = []
    if config is not None:
        sets.append("config = %s")
        params.append(Json(config))
    if enabled is not None:
        sets.append("enabled = %s")
        params.append(bool(enabled))
    if state is not None:
        sets.append("state = COALESCE(state, '{}'::jsonb) || %s::jsonb")
        params.append(Json(state))
    if not sets:
        return await get_source(user_id, source_id)
    sets.append("updated_at = NOW()")
    params.extend([user_id, source_id])
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE autonomy_sources
                SET {", ".join(sets)}
                WHERE user_id = %s AND source_id = %s
                RETURNING {_SOURCE_COLUMNS}
                """,
                params,
            )
            return _source(await cur.fetchone())


async def list_enabled_sources(*, limit: int = 100) -> list[dict[str, Any]]:
    """Every enabled Source, used by the pass to enqueue due polls."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_SOURCE_COLUMNS}
                FROM autonomy_sources
                WHERE enabled
                ORDER BY updated_at
                LIMIT %s
                """,
                (limit,),
            )
            return [_source(row) for row in await cur.fetchall() if row]  # type: ignore[misc]


async def delete_source(user_id: str, source_id: str) -> bool:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM autonomy_sources WHERE user_id = %s AND source_id = %s",
                (user_id, source_id),
            )
            return cur.rowcount > 0
