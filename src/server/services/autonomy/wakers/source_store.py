"""Source-row wake commit and fix-CI repository lookup for RepoBar.

Kept under wakers/ so database/autonomy/sources.py stays outside this line.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Json

from src.server.database.pool import get_db_connection

async def commit_source_wakes(
    *,
    user_id: str,
    source_id: str,
    wakes: list[tuple[str, str, dict[str, Any]]],
    state: dict[str, Any],
) -> list[str]:
    """Insert wake effects, then advance the source cursor, in one transaction.

    The cursor must not move ahead of events that were not stored. A failed
    insert or a missing source row rolls both back, so the next poll reads
    the same window again. Idempotency keys make that retry safe.
    """
    from src.server.database.autonomy.effects import insert_effect
    from src.server.services.autonomy.wakers.base import EFFECT_WAKE

    accepted: list[str] = []
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                for owner, key, payload in wakes:
                    identity = str(key or "").strip()
                    if not identity:
                        continue
                    inserted = await insert_effect(
                        cur,
                        user_id=str(owner or user_id or ""),
                        kind=EFFECT_WAKE,
                        idempotency_key=identity,
                        payload=dict(payload or {}),
                    )
                    if inserted:
                        accepted.append(identity)
                await cur.execute(
                    """
                    UPDATE autonomy_sources
                    SET state = COALESCE(state, '{}'::jsonb) || %s::jsonb,
                        updated_at = NOW()
                    WHERE user_id = %s AND source_id = %s
                    """,
                    (Json(state), user_id, source_id),
                )
                if cur.rowcount != 1:
                    raise RuntimeError("source cursor was not updated")
    return accepted


async def fix_ci_repositories(user_id: str) -> set[str]:
    """Repositories whose enabled source opted into a fix-CI Ticket.

    The flag lives on the source config, not on the wake. An event cannot
    turn it on by naming itself. An empty user id lists every opted-in
    repository; a user id limits the set to that owner.
    """
    owner = str(user_id or "")
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT lower(config->>'repository')
                FROM autonomy_sources
                WHERE enabled
                  AND kind = 'github'
                  AND COALESCE(config->>'repository', '') <> ''
                  AND (
                    config->'fix_ci' = 'true'::jsonb
                    OR lower(config->>'fix_ci') IN ('true', '1', 'yes', 'on')
                  )
                  AND (%s = '' OR user_id = %s)
                """,
                (owner, owner),
            )
            rows = await cur.fetchall()
    return {str(row[0]) for row in rows if row and row[0]}


