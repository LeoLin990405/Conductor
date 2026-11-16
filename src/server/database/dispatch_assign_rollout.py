"""The persisted N73 Q8 rollout stage (migration 151, ``dispatch_assign_rollout``)."""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection


async def get_rollout() -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT stage, rolled_back, reasons FROM dispatch_assign_rollout "
                "WHERE rollout_id = 1"
            )
            row = await cur.fetchone()
    return dict(row) if row is not None else None


async def set_rollout(*, stage: str, rolled_back: bool, reasons: list[str]) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO dispatch_assign_rollout (rollout_id, stage, rolled_back, reasons) "
                "VALUES (1, %s, %s, %s) ON CONFLICT (rollout_id) DO UPDATE SET "
                "stage = EXCLUDED.stage, rolled_back = EXCLUDED.rolled_back, "
                "reasons = EXCLUDED.reasons, updated_at = NOW()",
                (stage, rolled_back, list(reasons)),
            )
