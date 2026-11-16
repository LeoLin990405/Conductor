"""Durable per-user Router state and decision observations."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.pool import get_db_connection


def _session_shard(shard: int) -> int:
    """Shard 0 is the single Router session; the column cannot be negative."""
    return max(0, int(shard))


def _row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["thread_id"] = str(out["thread_id"])
    out["shard"] = _session_shard(out.get("shard") or 0)
    return out


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


async def get_router_baseline_snapshot(user_id: str) -> dict[str, Any] | None:
    """Read the snapshot and generation used to guard the next refresh."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT snapshot_json, created_at, invalidated_at, generation,
                       integrity_sha256
                FROM autonomy_router_baseline_snapshots
                WHERE user_id = %s
                """,
                (user_id,),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        **dict(row),
        "snapshot_json": dict(row.get("snapshot_json") or {}),
        "generation": int(row.get("generation") or 0),
    }


async def save_router_baseline_snapshot(
    user_id: str,
    snapshot_json: dict[str, Any],
    *,
    expected_generation: int,
) -> bool:
    """Save only if no invalidation advanced the generation during building.

    The SHA-256 is an integrity checksum for accidental/corrupt changes; it is
    not a signature and does not establish authenticity.
    """
    canonical = _canonical_json(snapshot_json)
    checksum = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            if expected_generation == 0:
                await cur.execute(
                    """
                    INSERT INTO autonomy_router_baseline_snapshots (
                        user_id, snapshot_json, integrity_sha256, generation,
                        created_at, invalidated_at
                    ) VALUES (%s, %s::jsonb, %s, 1, NOW(), NULL)
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    (user_id, Json(snapshot_json), checksum),
                )
            else:
                await cur.execute(
                    """
                    UPDATE autonomy_router_baseline_snapshots
                    SET snapshot_json = %s::jsonb,
                        integrity_sha256 = %s,
                        generation = generation + 1,
                        created_at = NOW(),
                        invalidated_at = NULL
                    WHERE user_id = %s AND generation = %s
                    """,
                    (Json(snapshot_json), checksum, user_id, expected_generation),
                )
            return cur.rowcount == 1


async def invalidate_router_snapshot(user_id: str) -> None:
    """Advance the durable generation so an in-flight stale builder loses CAS."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO autonomy_router_baseline_snapshots (
                    user_id, snapshot_json, integrity_sha256, generation,
                    created_at, invalidated_at
                ) VALUES (%s, '{}'::jsonb, '', 1, NOW(), NOW())
                ON CONFLICT (user_id) DO UPDATE
                SET generation = autonomy_router_baseline_snapshots.generation + 1,
                    invalidated_at = NOW()
                """,
                (user_id,),
            )


async def reserve_router_dispatch_metric(
    *,
    user_id: str,
    eval_id: str,
    decision_id: str,
    stable_decision_key: str,
    trigger_ids: list[str],
    batch_size: int,
    first_candidate_id: str | None = None,
) -> str | None:
    """Reserve before dispatch so a fast successful route has a metric row."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO autonomy_router_decision_metrics (
                    user_id, eval_id, decision_id, stable_decision_key,
                    trigger_ids, batch_size,
                    first_candidate_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (eval_id) DO UPDATE
                SET decision_id = autonomy_router_decision_metrics.decision_id
                WHERE autonomy_router_decision_metrics.user_id = EXCLUDED.user_id
                RETURNING decision_id
                """,
                (
                    user_id, eval_id, decision_id, stable_decision_key,
                    trigger_ids, max(0, int(batch_size)), first_candidate_id,
                ),
            )
            row = await cur.fetchone()
            return str(row["decision_id"]) if row else None


async def complete_router_dispatch_metric(
    *,
    decision_id: str,
    latency_ms: float,
    dispatch_status: str = "submitted",
    model_tokens: int | None = None,
) -> None:
    """Record admission HTTP latency, not model or decision time."""
    if dispatch_status not in {"submitted", "failed"}:
        raise ValueError("dispatch_status must be submitted or failed")
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE autonomy_router_decision_metrics
                SET latency_ms = %s, model_tokens = %s, dispatch_status = %s
                WHERE decision_id = %s
                  AND (
                      latency_ms IS NULL
                      OR (dispatch_status = 'failed' AND %s = 'submitted')
                  )
                """,
                (
                    max(0.0, float(latency_ms)),
                    model_tokens if model_tokens is None else max(0, int(model_tokens)),
                    dispatch_status, decision_id, dispatch_status,
                ),
            )


async def record_router_route_decision(
    *, user_id: str, eval_id: str, trigger_id: str, decision: str
) -> None:
    """Attach the canonical route result once, only for a dispatched candidate."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE autonomy_router_decision_metrics
                SET routed_trigger_id = %s,
                    route_decision = %s,
                    accepted_candidate_id = CASE
                        WHEN %s IN ('accept', 'attach') THEN %s ELSE NULL
                    END,
                    first_candidate_accepted = CASE
                        WHEN %s IN ('accept', 'attach') AND first_candidate_id IS NOT NULL
                            THEN first_candidate_id = %s
                        WHEN %s IN ('attach', 'ignore', 'stop') THEN FALSE
                        ELSE NULL
                    END,
                    decided_at = NOW(),
                    decision_latency_ms = GREATEST(
                        0, EXTRACT(EPOCH FROM (NOW() - created_at)) * 1000
                    )
                WHERE eval_id = %s
                  AND user_id = %s
                  AND route_decision IS NULL
                  AND %s = ANY(trigger_ids)
                """,
                (
                    trigger_id, decision, decision, trigger_id,
                    decision, trigger_id, decision, eval_id, user_id, trigger_id,
                ),
            )


async def latest_router_decision_for_batch(
    user_id: str,
    stable_decision_key: str,
    *,
    exclude_eval_id: str,
    max_age_seconds: int = 3600,
) -> dict[str, Any] | None:
    """Find one recent canonical outcome for the same stable candidate batch."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT stable_decision_key, eval_id, routed_trigger_id,
                       route_decision, created_at, decided_at
                FROM autonomy_router_decision_metrics
                WHERE user_id = %s
                  AND stable_decision_key = %s
                  AND eval_id <> %s
                  AND route_decision IS NOT NULL
                  AND decided_at >= NOW() - (%s * INTERVAL '1 second')
                ORDER BY decided_at DESC
                LIMIT 1
                """,
                (user_id, stable_decision_key, exclude_eval_id,
                 min(86400, max(1, int(max_age_seconds)))),
            )
            row = await cur.fetchone()
    return dict(row) if row is not None else None


async def list_router_metrics(user_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Return a bounded history belonging only to the authenticated user."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT decision_id, stable_decision_key, eval_id, trigger_ids,
                       batch_size, dispatch_status,
                       latency_ms AS dispatch_latency_ms,
                       decision_latency_ms, model_tokens,
                       first_candidate_id,
                       routed_trigger_id, route_decision, accepted_candidate_id,
                       first_candidate_accepted, created_at, decided_at
                FROM autonomy_router_decision_metrics
                WHERE user_id = %s
                ORDER BY created_at DESC, decision_id DESC
                LIMIT %s
                """,
                (user_id, min(100, max(1, int(limit)))),
            )
            return [dict(row) for row in await cur.fetchall()]


async def get_router_session(user_id: str, shard: int = 0) -> dict[str, Any] | None:
    shard = _session_shard(shard)
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT user_id, workspace_id, thread_id, shard,
                       created_at, updated_at
                FROM autonomy_router_sessions
                WHERE user_id = %s AND shard = %s
                """,
                (user_id, shard),
            )
            return _row(await cur.fetchone())


async def save_router_session(
    user_id: str, workspace_id: str, thread_id: str, shard: int = 0
) -> dict[str, Any]:
    """Upsert one Router session.

    Migration 122's primary key is ``(user_id, shard)``. ``ON CONFLICT (user_id)``
    has no matching unique constraint after that change, so Postgres rejects it.
    """
    shard = _session_shard(shard)
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO autonomy_router_sessions
                    (user_id, workspace_id, thread_id, shard)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, shard) DO UPDATE
                SET workspace_id = EXCLUDED.workspace_id,
                    thread_id = EXCLUDED.thread_id,
                    updated_at = NOW()
                RETURNING user_id, workspace_id, thread_id, shard,
                          created_at, updated_at
                """,
                (user_id, workspace_id, thread_id, shard),
            )
            return _row(await cur.fetchone())  # type: ignore[return-value]


async def get_handoff_delivered(thread_id: str) -> str | None:
    """The Router handoff id whose note this Router thread already received."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT metadata ->> 'router_handoff_delivered' AS delivered "
                "FROM conversation_threads WHERE conversation_thread_id = %s",
                (thread_id,),
            )
            row = await cur.fetchone()
    return str(row["delivered"]) if row and row.get("delivered") else None


async def set_handoff_delivered(thread_id: str, handoff_id: str) -> None:
    """Record on the Router thread's own metadata (no registry revision)."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE conversation_threads SET metadata = jsonb_set("
                "COALESCE(metadata, '{}'::jsonb), '{router_handoff_delivered}', "
                "to_jsonb(%s::text), true) WHERE conversation_thread_id = %s",
                (handoff_id, thread_id),
            )
