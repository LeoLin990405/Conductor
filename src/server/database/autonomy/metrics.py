"""Per-user pipeline backlog counters for the autonomy pass."""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection


async def pipeline_metrics(user_id: str) -> dict[str, Any]:
    """Counts that explain where Super-agent work is waiting."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    (SELECT COALESCE(SUM(cardinality(pending_trigger_ids)), 0)
                     FROM autonomy_threads WHERE user_id = %s) AS pending_triggers,
                    (SELECT COUNT(*) FROM autonomy_threads
                     WHERE user_id = %s
                       AND cardinality(pending_trigger_ids) > 0) AS pending_threads,
                    (SELECT COUNT(*) FROM autonomy_evals e
                     JOIN autonomy_threads t ON t.thread_key = e.thread_key
                     WHERE t.user_id = %s
                       AND e.status IN ('queued', 'delivered')) AS active_evals,
                    (SELECT COUNT(*) FROM autonomy_effects
                     WHERE user_id = %s AND status = 'pending') AS pending_effects,
                    (SELECT COUNT(*) FROM autonomy_effects
                     WHERE user_id = %s AND status = 'claimed') AS claimed_effects,
                    (SELECT COUNT(*) FROM autonomy_tickets
                     WHERE user_id = %s AND status = 'queued') AS queued_tickets,
                    (SELECT COUNT(*) FROM autonomy_tickets
                     WHERE user_id = %s AND status = 'running') AS running_tickets,
                    (SELECT EXTRACT(EPOCH FROM (NOW() - MIN(created_at)))::int
                     FROM autonomy_effects
                     WHERE user_id = %s AND status = 'pending') AS oldest_pending_effect_seconds
                """,
                (user_id,) * 8,
            )
            row = await cur.fetchone() or {}
    return {key: int(value or 0) for key, value in dict(row).items()}
