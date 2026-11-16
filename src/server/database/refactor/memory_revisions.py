"""Owner-scoped reads and serialization for immutable memory revisions."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any

from psycopg.rows import dict_row

from src.server.database import pool
from src.server.utils.pg_sanitize import normalize_uuid, refactor_owner_uuid

logger = logging.getLogger(__name__)


async def get_revision(*, user_id: str, entry_id: str) -> dict[str, Any] | None:
    owner = refactor_owner_uuid(user_id)
    revision_id = normalize_uuid(entry_id)
    if owner is None or revision_id is None:
        return None
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT entry_id::text AS entry_id, user_id::text AS user_id,
                       tier, namespace, entry_key, status, index_text, content_hash, metadata,
                       source_thread_id::text AS source_thread_id,
                       source_response_id::text AS source_response_id,
                       source_entry_id::text AS source_entry_id
                  FROM memory_entries
                 WHERE user_id = %s AND entry_id = %s
                   AND entry_key NOT LIKE 'proposed/%%'
                   AND NOT EXISTS (
                       SELECT 1 FROM memory_forget_tombstones f
                        WHERE f.user_id = memory_entries.user_id
                          AND f.identity_hash = memory_entries.identity_hash
                   )
                 LIMIT 1
                """,
                (owner, revision_id),
            )
            row = await cur.fetchone()
            return dict(row) if row is not None else None


async def get_source_turn_slice(*, thread_id: str, response_id: str) -> dict[str, Any] | None:
    thread = normalize_uuid(thread_id)
    response = normalize_uuid(response_id)
    if thread is None or response is None:
        return None
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT conversation_thread_id::text AS thread_id,
                       conversation_response_id::text AS response_id,
                       slice_codec, slice
                  FROM turn_slices
                 WHERE conversation_thread_id = %s
                   AND conversation_response_id = %s
                 LIMIT 1
                """,
                (thread, response),
            )
            row = await cur.fetchone()
            if row is None or row.get("slice") is None or not row.get("slice_codec"):
                return None
            result = dict(row)
            result["slice"] = bytes(result["slice"])
            return result


async def get_parent_response_id(*, thread_id: str, task_run_id: str) -> str | None:
    thread = normalize_uuid(thread_id)
    task_run = normalize_uuid(task_run_id)
    if thread is None or task_run is None:
        return None
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT parent_run_id::text FROM subagent_runs "
                "WHERE thread_id = %s AND task_run_id = %s",
                (thread, task_run),
            )
            row = await cur.fetchone()
            return str(row[0]) if row and row[0] else None


@asynccontextmanager
async def owner_memory_lock(user_id: str) -> AsyncIterator[None]:
    """Serialize Store index read-modify-write with manual approvals.

    The session-level lock rides a dedicated unpooled connection: the code
    under the lock keeps using the shared pool (and its own transactions), so
    holding a pooled connection here would nest acquisitions and can exhaust a
    small pool. ``pg_advisory_lock`` is released explicitly, and the server
    releases it anyway when the connection closes after a crash.
    """
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        raise ValueError("user_id is required")
    lock_name = f"conductor:memory-approval:{owner}"
    async with pool.get_dedicated_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (lock_name,)
            )
            await cur.fetchone()
        try:
            yield
        finally:
            # Best effort: a failed unlock still ends with the connection close
            # below, which releases every session lock the server holds.
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                        (lock_name,),
                    )
                    await cur.fetchone()
            except Exception:  # noqa: BLE001 - release is fail-safe on close
                logger.warning(
                    "[memory] owner lock release failed; closing connection", exc_info=True
                )
