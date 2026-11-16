"""Persistence for a thread's native-runtime binding and CLI session id."""

from __future__ import annotations

import logging
from typing import Any, Mapping

from psycopg.types.json import Json

from src.server.database import pool
from src.server.services.native_runtime.contracts import NativeRuntimeSpec
from src.server.services.native_runtime.spec import with_session_metadata

logger = logging.getLogger(__name__)


async def _metadata_for_thread(cur, thread_id: str) -> dict[str, Any] | None:
    await cur.execute(
        "SELECT metadata FROM conversation_threads WHERE conversation_thread_id = %s",
        (thread_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row.get("metadata") or {})
    return dict(row[0] or {})


async def get_thread_metadata(thread_id: str) -> dict[str, Any] | None:
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            return await _metadata_for_thread(cur, thread_id)


async def set_native_runtime_spec(
    thread_id: str, spec: NativeRuntimeSpec | None
) -> dict[str, Any] | None:
    """Replace/remove the binding; other metadata annotations are preserved."""
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            metadata = await _metadata_for_thread(cur, thread_id)
            if metadata is None:
                return None
            if spec is None:
                metadata.pop("native_runtime", None)
            else:
                metadata.update(spec.to_metadata())
            await cur.execute(
                "UPDATE conversation_threads SET metadata = %s::jsonb "
                "WHERE conversation_thread_id = %s",
                (Json(metadata), thread_id),
            )
            return metadata


async def save_native_session(thread_id: str, session_id: str | None) -> None:
    """Persist the CLI's session id after a native turn.

    Best-effort: a lost session id costs cache continuity on the next turn but
    must not fail an answer that already reached the user.
    """
    if not session_id:
        return
    try:
        async with pool.get_db_connection() as conn:
            async with conn.cursor() as cur:
                metadata = await _metadata_for_thread(cur, thread_id)
                if metadata is None:
                    return
                current = metadata.get("native_runtime")
                if not isinstance(current, Mapping) or not current.get("provider"):
                    return
                spec = NativeRuntimeSpec.from_thread_metadata(metadata)
                if spec is None:
                    return
                metadata.update(with_session_metadata(metadata, spec.with_session(session_id)))
                await cur.execute(
                    "UPDATE conversation_threads SET metadata = %s::jsonb "
                    "WHERE conversation_thread_id = %s",
                    (Json(metadata), thread_id),
                )
    except Exception:
        logger.warning(
            f"[native-runtime] session id save failed for thread={thread_id}",
            exc_info=True,
        )


async def list_runtime_decisions(
    user_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    """Recent auto-routing decisions persisted on the user's threads.

    The decision lives in ``metadata.runtime_decision``; the joined binding is
    the immutable ``metadata.native_runtime``. This is read-only telemetry for
    evaluating the router, not a second source of truth.
    """
    from psycopg.rows import dict_row

    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT t.conversation_thread_id::text AS thread_id,
                       t.title,
                       t.metadata->'runtime_decision' AS decision,
                       t.metadata->'native_runtime'->>'provider' AS native_provider,
                       t.created_at
                FROM conversation_threads t
                JOIN workspaces w ON t.workspace_id = w.workspace_id
                WHERE w.user_id = %s
                  AND w.status != 'deleted'
                  AND t.metadata ? 'runtime_decision'
                ORDER BY t.created_at DESC, t.conversation_thread_id DESC
                LIMIT %s
                """,
                (user_id, max(1, min(int(limit), 100))),
            )
            rows = await cur.fetchall()
            return [dict(row) for row in rows]
