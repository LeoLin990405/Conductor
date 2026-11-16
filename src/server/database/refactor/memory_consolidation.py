"""Durable, owner-bound claims and proposal writes for memory consolidation."""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence

from psycopg.rows import dict_row

from src.server.database import pool
from src.server.utils.pg_sanitize import (
    SafeJson,
    normalize_uuid,
    refactor_owner_uuid,
    strip_pg_nul_str,
)

MAX_SOURCE_ENTRIES = 16
MAX_GROUPS_PER_PAGE = 20
MAX_GROUPS_PER_DAY = 25
MAX_RUN_ATTEMPTS = 6
RUN_LEASE_SECONDS = 300
POLICY_VERSION = "v1"
DAILY_CUTOFF_HOUR_UTC = 3


async def due_run_date() -> date | None:
    """Use the database UTC clock; a delayed scheduler catches up after cutoff."""
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                WITH utc_now AS (
                    SELECT timezone('UTC', clock_timestamp()) AS value
                )
                SELECT CASE
                    WHEN value::time >= %s::time THEN value::date
                    ELSE NULL
                END
                FROM utc_now
                """,
                (f"{DAILY_CUTOFF_HOUR_UTC:02d}:00:00",),
            )
            row = await cur.fetchone()
            return row[0] if row and isinstance(row[0], date) else None


async def list_opted_in_users(*, limit: int = 100) -> list[str]:
    """Return only IDs whose server-managed opt-in is explicitly true."""
    if limit < 1 or limit > 1000:
        raise ValueError("opt-in user page must be between 1 and 1000")
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT user_id
                  FROM user_preferences
                 WHERE other_preference #>>
                       '{feature_overrides,memory_consolidation}' = 'true'
                   AND NOT EXISTS (
                       SELECT 1 FROM memory_consolidation_runs r
                        WHERE r.raw_user_id = user_preferences.user_id
                          AND r.run_date =
                              (clock_timestamp() AT TIME ZONE 'UTC')::date
                          AND r.policy_version = %s
                          AND (
                              r.status = 'complete'
                              OR r.attempts >= %s
                              OR (r.status = 'running'
                                  AND r.lease_until > clock_timestamp())
                              OR (r.retry_after IS NOT NULL
                                  AND r.retry_after > clock_timestamp())
                          )
                   )
                 ORDER BY user_id
                 LIMIT %s
                """,
                (POLICY_VERSION, MAX_RUN_ATTEMPTS, limit),
            )
            return [str(row[0]) for row in await cur.fetchall() if row and row[0]]


async def is_user_opted_in(*, user_id: str) -> bool:
    """Recheck the explicit toggle without fetching unrelated feature state."""
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT other_preference #>>
                       '{feature_overrides,memory_consolidation}' = 'true'
                  FROM user_preferences
                 WHERE user_id = %s
                """,
                (user_id,),
            )
            row = await cur.fetchone()
            return bool(row and row[0] is True)


async def claim_daily_run(
    *, user_id: str, run_date: date, policy_version: str = POLICY_VERSION
) -> dict[str, Any] | None:
    """Create/reclaim one owner-day row under a short database lease."""
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        return None
    async with pool.get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    INSERT INTO memory_consolidation_runs
                        (user_id, raw_user_id, run_date, policy_version,
                         status, cursor_key)
                    VALUES (%s, %s, %s, %s, 'pending', '')
                    ON CONFLICT (user_id, run_date, policy_version) DO NOTHING
                    """,
                    (owner, user_id, run_date, policy_version),
                )
                await cur.execute(
                    """
                    SELECT run_id::text AS run_id, user_id::text AS user_id,
                           raw_user_id,
                           run_date, status, cursor_key, groups_processed,
                           proposals_created, attempts
                      FROM memory_consolidation_runs
                     WHERE user_id = %s AND run_date = %s
                       AND policy_version = %s
                     FOR UPDATE
                    """,
                    (owner, run_date, policy_version),
                )
                row = await cur.fetchone()
                if row is None or row["status"] == "complete":
                    return None
                if int(row["attempts"] or 0) >= MAX_RUN_ATTEMPTS:
                    return None
                await cur.execute(
                    """
                    UPDATE memory_consolidation_runs
                       SET status = 'running', attempts = attempts + 1,
                           lease_until = clock_timestamp()
                               + make_interval(secs => %s),
                           last_error_code = NULL, updated_at = clock_timestamp()
                     WHERE run_id = %s
                       AND (status <> 'running' OR lease_until <= clock_timestamp())
                       AND (retry_after IS NULL OR retry_after <= clock_timestamp())
                    RETURNING run_id::text AS run_id, user_id::text AS user_id,
                              raw_user_id,
                              run_date, status, cursor_key, groups_processed,
                              proposals_created, attempts
                    """,
                    (RUN_LEASE_SECONDS, row["run_id"]),
                )
                claimed = await cur.fetchone()
                return dict(claimed) if claimed is not None else None


async def list_candidate_groups(
    *, user_id: str, after_cursor: str, limit: int = MAX_GROUPS_PER_PAGE + 1
) -> list[dict[str, Any]]:
    """Page explicit duplicate/lineage groups; payloads contain metadata, not bodies."""
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        return []
    if limit < 1 or limit > MAX_GROUPS_PER_PAGE + 1:
        raise ValueError("candidate group page exceeds its bound")
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                WITH eligible AS (
                    SELECT m.entry_id, m.user_id, m.tier, m.namespace,
                           m.entry_key, m.content_hash, m.metadata,
                           m.source_entry_id
                      FROM memory_entries m
                     WHERE m.user_id = %s
                       AND m.tier = 'user'
                       AND m.status = 'active'
                       AND m.entry_key NOT LIKE 'proposed/%%'
                       AND NOT EXISTS (
                           SELECT 1 FROM memory_forget_tombstones f
                            WHERE f.user_id = m.user_id
                              AND f.identity_hash = m.identity_hash
                       )
                       AND COALESCE(m.metadata->>'sensitivity', 'private')
                           IN ('public', 'private')
                       AND COALESCE(m.metadata->>'kind', '') <> 'constraint'
                       AND NOT jsonb_exists(m.metadata::jsonb, 'denied_tools')
                       AND NOT EXISTS (
                           SELECT 1 FROM memory_tool_denials d
                            WHERE d.user_id = m.user_id
                              AND d.entry_id = m.entry_id
                              AND d.active IS TRUE
                              AND d.approval_source = 'manual'
                       )
                ), candidate_groups AS (
                    SELECT 'duplicate'::text AS group_kind,
                           user_id, namespace, content_hash AS group_ref,
                           array_agg(entry_id ORDER BY entry_id) AS source_ids,
                           array_agg(entry_key ORDER BY entry_id) AS source_keys,
                           array_agg(content_hash ORDER BY entry_id) AS source_hashes,
                           array_agg(metadata ORDER BY entry_id) AS source_metadata,
                           string_agg(entry_id::text || ':' || content_hash,
                                      ',' ORDER BY entry_id) AS source_signature,
                           min(entry_id::text) AS first_entry_id,
                           count(*)::int AS source_count
                      FROM eligible
                     GROUP BY user_id, namespace, content_hash
                    HAVING count(*) >= 2
                    UNION ALL
                    SELECT 'conflict'::text AS group_kind,
                           user_id, namespace, source_entry_id::text AS group_ref,
                           array_agg(entry_id ORDER BY entry_id) AS source_ids,
                           array_agg(entry_key ORDER BY entry_id) AS source_keys,
                           array_agg(content_hash ORDER BY entry_id) AS source_hashes,
                           array_agg(metadata ORDER BY entry_id) AS source_metadata,
                           string_agg(entry_id::text || ':' || content_hash,
                                      ',' ORDER BY entry_id) AS source_signature,
                           min(entry_id::text) AS first_entry_id,
                           count(*)::int AS source_count
                      FROM eligible
                     WHERE source_entry_id IS NOT NULL
                     GROUP BY user_id, namespace, source_entry_id
                    HAVING count(DISTINCT content_hash) >= 2
                ), bounded AS (
                    SELECT *, group_kind || ':' || namespace::text || ':' || group_ref
                               AS cursor_key
                      FROM candidate_groups
                     WHERE source_count <= %s
                       AND NOT EXISTS (
                           SELECT 1 FROM agent_memory_proposals p
                            WHERE p.user_id = %s
                              AND p.producer_kind = 'memory_consolidation'
                              AND p.source_entry_ids = candidate_groups.source_ids
                              AND p.producer_metadata->>'source_signature'
                                  = candidate_groups.source_signature
                              AND p.status IN ('proposed', 'approved', 'rejected')
                       )
                )
                SELECT group_kind, user_id::text AS user_id, namespace, group_ref, source_ids,
                       source_keys, source_hashes, source_metadata,
                       source_signature, first_entry_id,
                       source_count, cursor_key
                  FROM bounded
                 WHERE cursor_key > %s
                 ORDER BY cursor_key
                 LIMIT %s
                """,
                (owner, MAX_SOURCE_ENTRIES, owner, after_cursor, limit),
            )
            return [dict(row) for row in await cur.fetchall()]


async def get_active_source_entries(
    *, user_id: str, source_entry_ids: Sequence[str]
) -> list[dict[str, Any]]:
    """Load a bounded, owner-scoped active source set for manual approval."""
    owner = refactor_owner_uuid(user_id)
    ids = [normalize_uuid(value) for value in source_entry_ids]
    if (
        owner is None
        or len(ids) < 2
        or len(ids) > MAX_SOURCE_ENTRIES
        or any(value is None for value in ids)
        or len(set(ids)) != len(ids)
    ):
        return []
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT m.entry_id::text AS entry_id, m.user_id::text AS user_id, "
                "m.tier, m.namespace, m.entry_key, m.content_hash, m.status, m.metadata "
                "FROM memory_entries m WHERE m.user_id = %s AND m.tier = 'user' "
                "AND m.status = 'active' AND m.entry_id = ANY(%s::uuid[]) "
                "AND m.entry_key NOT LIKE 'proposed/%%' "
                "AND NOT EXISTS (SELECT 1 FROM memory_forget_tombstones f "
                "WHERE f.user_id = m.user_id AND f.identity_hash = m.identity_hash) "
                "ORDER BY m.entry_id",
                (owner, ids),
            )
            return [dict(row) for row in await cur.fetchall()]


async def get_source_entries_for_retry(
    *, user_id: str, source_entry_ids: Sequence[str]
) -> list[dict[str, Any]]:
    """Load known same-owner source revisions for an idempotent approval retry."""
    owner = refactor_owner_uuid(user_id)
    ids = [normalize_uuid(value) for value in source_entry_ids]
    if (
        owner is None
        or len(ids) < 2
        or len(ids) > MAX_SOURCE_ENTRIES
        or any(value is None for value in ids)
        or len(set(ids)) != len(ids)
    ):
        return []
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT m.entry_id::text AS entry_id, m.user_id::text AS user_id, "
                "m.tier, m.namespace, m.entry_key, m.content_hash, m.status, m.metadata "
                "FROM memory_entries m WHERE m.user_id = %s AND m.tier = 'user' "
                "AND m.status IN ('active', 'superseded') "
                "AND m.entry_id = ANY(%s::uuid[]) AND m.entry_key NOT LIKE 'proposed/%%' "
                "AND NOT EXISTS (SELECT 1 FROM memory_forget_tombstones f "
                "WHERE f.user_id = m.user_id AND f.identity_hash = m.identity_hash) "
                "ORDER BY m.entry_id",
                (owner, ids),
            )
            return [dict(row) for row in await cur.fetchall()]


async def record_page(
    *,
    run_id: str,
    user_id: str,
    attempt: int,
    expected_cursor: str,
    next_cursor: str,
    groups_processed: int,
    proposals: Sequence[Mapping[str, Any]],
    complete: bool,
    error_code: str | None = None,
) -> int | None:
    """Insert idempotent producer rows and advance the lease cursor atomically.

    Runs under the owner memory lock so a concurrent memory forget cannot
    interleave between the source check and the proposal insert (forget holds
    the same lock while it tombstones and purges source-linked proposals).
    """
    from src.server.database.refactor import memory_revisions

    async with memory_revisions.owner_memory_lock(user_id):
        return await _record_page_locked(
            run_id=run_id,
            user_id=user_id,
            attempt=attempt,
            expected_cursor=expected_cursor,
            next_cursor=next_cursor,
            groups_processed=groups_processed,
            proposals=proposals,
            complete=complete,
            error_code=error_code,
        )


async def _record_page_locked(
    *,
    run_id: str,
    user_id: str,
    attempt: int,
    expected_cursor: str,
    next_cursor: str,
    groups_processed: int,
    proposals: Sequence[Mapping[str, Any]],
    complete: bool,
    error_code: str | None = None,
) -> int | None:
    """Insert idempotent producer rows and advance the lease cursor atomically."""
    run = normalize_uuid(run_id)
    owner = refactor_owner_uuid(user_id)
    if run is None or owner is None:
        return 0
    inserted = 0
    async with pool.get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT 1 FROM memory_consolidation_runs
                     WHERE run_id = %s AND user_id = %s
                       AND status = 'running'
                       AND lease_until > clock_timestamp()
                       AND attempts = %s
                       AND cursor_key = %s
                     FOR UPDATE
                    """,
                    (run, owner, attempt, expected_cursor),
                )
                if await cur.fetchone() is None:
                    return None
                for proposal in proposals:
                    source_ids = [
                        normalized
                        for raw in proposal.get("source_entry_ids", ())
                        if (normalized := normalize_uuid(raw)) is not None
                    ]
                    raw_source_ids = list(proposal.get("source_entry_ids", ()))
                    if (
                        not source_ids
                        or len(source_ids) != len(raw_source_ids)
                        or len(source_ids) > MAX_SOURCE_ENTRIES
                    ):
                        continue
                    await cur.execute(
                        "SELECT 1 FROM memory_forget_tombstones"
                        " WHERE user_id = %s AND entry_id = ANY(%s::uuid[])"
                        " LIMIT 1",
                        (owner, source_ids),
                    )
                    if await cur.fetchone() is not None:
                        # A source was forgotten after the group was computed;
                        # never publish (or re-publish) its copied body.
                        continue
                    await cur.execute(
                        """
                        INSERT INTO agent_memory_proposals
                            (user_id, thread_id, task_run_id, producer_kind,
                             producer_id, source_entry_ids, producer_metadata,
                             proposal_type, proposal_key, text, text_hash,
                             origin, confidence, evidence_refs, evidence_count,
                             counts_as_evidence, change_kind, replaces_entry_key,
                             reason, metadata)
                        VALUES (%s, NULL, NULL, 'memory_consolidation', %s,
                                %s, %s, %s, %s, %s, %s, 'assistant_inference',
                                %s, '[]'::jsonb, 0, FALSE, 'correction', %s,
                                %s, %s)
                        ON CONFLICT (producer_kind, producer_id,
                                     proposal_type, text_hash)
                            WHERE producer_kind = 'memory_consolidation'
                         DO NOTHING
                        """,
                        (
                            owner,
                            run,
                            source_ids,
                            SafeJson(dict(proposal.get("producer_metadata") or {})),
                            str(proposal.get("proposal_type") or "user"),
                            strip_pg_nul_str(str(proposal.get("proposal_key") or "")),
                            strip_pg_nul_str(str(proposal.get("text") or "")),
                            str(proposal.get("text_hash") or ""),
                            float(proposal.get("confidence", 0.5)),
                            str(proposal.get("replaces_entry_key") or ""),
                            strip_pg_nul_str(str(proposal.get("reason") or "")),
                            SafeJson(dict(proposal.get("metadata") or {})),
                        ),
                    )
                    inserted += cur.rowcount or 0
                await cur.execute(
                    """
                    UPDATE memory_consolidation_runs
                       SET cursor_key = %s,
                           groups_processed = groups_processed + %s,
                           proposals_created = proposals_created + %s,
                           status = CASE WHEN %s THEN 'complete' ELSE 'running' END,
                           lease_until = CASE WHEN %s THEN NULL ELSE
                               clock_timestamp() + make_interval(secs => %s) END,
                           retry_after = NULL,
                           last_error_code = %s,
                           updated_at = clock_timestamp()
                     WHERE run_id = %s AND user_id = %s
                    """,
                    (
                        next_cursor,
                        groups_processed,
                        inserted,
                        complete,
                        complete,
                        RUN_LEASE_SECONDS,
                        error_code,
                        run,
                        owner,
                    ),
                )
    return inserted


async def fail_run(
    *, run_id: str, user_id: str, attempt: int, error_code: str
) -> None:
    """Release one owner's claim without storing exception text or memory data."""
    run = normalize_uuid(run_id)
    owner = refactor_owner_uuid(user_id)
    if run is None or owner is None:
        return
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE memory_consolidation_runs
                   SET status = 'pending', lease_until = NULL,
                       retry_after = clock_timestamp() + make_interval(
                           secs => LEAST(300, attempts * 30)
                       ),
                       last_error_code = %s, updated_at = clock_timestamp()
                 WHERE run_id = %s AND user_id = %s AND attempts = %s
                   AND status = 'running'
                """,
                (error_code[:80], run, owner, attempt),
            )
