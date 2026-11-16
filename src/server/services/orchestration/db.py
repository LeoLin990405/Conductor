"""Durable persistence for the native/direct side of the worker ledger."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def valid_thread_id(thread_id: str) -> bool:
    return bool(_UUID.match(str(thread_id or "")))


async def upsert(record: dict[str, Any]) -> None:
    thread_id = str(record.get("thread_id") or "")
    if not valid_thread_id(thread_id):
        return
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO orchestration_workers (
                        id, thread_id, kind, provider, label, status, parent_id,
                        depends_on, prompt_preview, result_preview, error, meta,
                        started_at, finished_at
                    ) VALUES (
                        %s, %s::uuid, %s, %s, %s, %s, %s,
                        %s::jsonb, %s, %s, %s, %s::jsonb,
                        NULLIF(%s, '')::timestamptz, NULLIF(%s, '')::timestamptz
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        status = EXCLUDED.status,
                        provider = EXCLUDED.provider,
                        label = EXCLUDED.label,
                        result_preview = EXCLUDED.result_preview,
                        error = EXCLUDED.error,
                        meta = EXCLUDED.meta,
                        finished_at = EXCLUDED.finished_at,
                        updated_at = NOW()
                    """,
                    (
                        record["id"], thread_id, record["kind"], record["provider"],
                        record["label"], record["status"], record["parent_id"],
                        json.dumps(record.get("depends_on") or []),
                        record["prompt_preview"], record["result_preview"],
                        record["error"], json.dumps(record.get("meta") or {}),
                        record.get("started_at") or "", record.get("finished_at") or "",
                    ),
                )
            await conn.commit()
    except Exception as exc:  # noqa: BLE001 - DB view is best effort
        logger.debug("[orchestration] db upsert skipped: %s", type(exc).__name__)


async def list_for_thread(thread_id: str) -> list[dict[str, Any]]:
    if not valid_thread_id(thread_id):
        return []
    try:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id, thread_id::text AS thread_id, kind, provider, label,
                           status, parent_id, depends_on, prompt_preview,
                           result_preview, error, meta,
                           COALESCE(started_at::text, '') AS started_at,
                           COALESCE(finished_at::text, '') AS finished_at
                    FROM orchestration_workers
                    WHERE thread_id = %s::uuid
                    ORDER BY started_at NULLS LAST, id
                    LIMIT 64
                    """,
                    (thread_id,),
                )
                rows = await cur.fetchall()
        return [dict(row) for row in rows]
    except Exception as exc:  # noqa: BLE001
        logger.debug("[orchestration] db read skipped: %s", type(exc).__name__)
        return []
