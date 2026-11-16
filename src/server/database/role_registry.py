"""Role registry rows (R6 L1, migration 151).

``role_registry`` holds one document per deployment with a revision for
compare-and-swap; ``role_registry_audit`` gets one row per change in the same
transaction, so a change without its audit row cannot commit. This module only
reads and writes rows; validation lives in the service.
"""

from __future__ import annotations

import json
from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection


class RegistryConflict(Exception):
    """The stored revision moved since the caller read it."""


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


async def get_registry() -> dict[str, Any] | None:
    """``{"document", "revision", "updated_by", "updated_at"}`` or ``None``."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT document, revision, updated_by, updated_at "
                "FROM role_registry WHERE registry_id = 1"
            )
            row = await cur.fetchone()
    if row is None:
        return None
    out = dict(row)
    out["updated_at"] = _iso(out.get("updated_at"))
    return out


async def save_registry(
    *,
    document: dict[str, Any],
    expected_revision: int,
    actor: str,
    role: str,
    reason: str,
    before: dict[str, Any] | None,
    after: dict[str, Any],
) -> int:
    """Write the document at ``expected_revision + 1`` plus its audit row.

    ``expected_revision`` 0 means "no row yet". Raises ``RegistryConflict``
    when another writer got there first.
    """
    new_revision = expected_revision + 1
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                if expected_revision == 0:
                    await cur.execute(
                        "INSERT INTO role_registry (registry_id, document, revision, updated_by) "
                        "VALUES (1, %s::jsonb, %s, %s) ON CONFLICT (registry_id) DO NOTHING "
                        "RETURNING revision",
                        (json.dumps(document), new_revision, actor),
                    )
                else:
                    await cur.execute(
                        "UPDATE role_registry SET document = %s::jsonb, revision = %s, "
                        "updated_by = %s, updated_at = NOW() "
                        "WHERE registry_id = 1 AND revision = %s RETURNING revision",
                        (json.dumps(document), new_revision, actor, expected_revision),
                    )
                row = await cur.fetchone()
                if row is None:
                    raise RegistryConflict(str(expected_revision))
                await cur.execute(
                    "INSERT INTO role_registry_audit "
                    "(revision, role, actor, reason, before, after) "
                    "VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)",
                    (
                        new_revision,
                        role,
                        actor,
                        reason,
                        None if before is None else json.dumps(before),
                        json.dumps(after),
                    ),
                )
    return new_revision


async def list_audit(*, limit: int = 50, role: str | None = None) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if role:
                await cur.execute(
                    "SELECT audit_id, revision, role, actor, reason, before, after, created_at "
                    "FROM role_registry_audit WHERE role = %s "
                    "ORDER BY created_at DESC, audit_id DESC LIMIT %s",
                    (role, limit),
                )
            else:
                await cur.execute(
                    "SELECT audit_id, revision, role, actor, reason, before, after, created_at "
                    "FROM role_registry_audit ORDER BY created_at DESC, audit_id DESC LIMIT %s",
                    (limit,),
                )
            rows = await cur.fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["created_at"] = _iso(item.get("created_at"))
        out.append(item)
    return out
