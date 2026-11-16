"""Durable owner-scoped memory tool-denial rows (migration 134 schema)."""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row

from src.server.contracts.memory_tool_policy import (
    denied_tools_as_json,
    normalize_validity_window,
)
from src.server.database import pool
from src.server.utils.pg_sanitize import SafeJson, normalize_uuid, refactor_owner_uuid


async def list_active_denials(*, user_id: str) -> list[dict[str, Any]]:
    """Read policy authority from SQL, never from Store frontmatter claims.

    Migration 134 owns ``memory_tool_denials``. Its active flag and approved
    validity window are authoritative. Projection edits cannot retire a rule;
    the entry hash is returned only to report drift while retaining the rule.
    """
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        return []
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT d.entry_id::text AS entry_id, d.denied_tools,"
                " d.approved_content_hash, d.valid_from, d.valid_until,"
                " e.content_hash AS current_content_hash,"
                " e.status AS current_status, e.metadata AS current_metadata"
                " FROM memory_tool_denials d"
                " LEFT JOIN memory_entries e ON e.entry_id = d.entry_id"
                " AND e.user_id = d.user_id"
                " WHERE d.user_id = %s AND d.active IS TRUE"
                " AND d.approval_source = 'manual'"
                " AND NOT EXISTS ("
                "   SELECT 1 FROM memory_forget_tombstones f"
                "   WHERE f.user_id = d.user_id AND f.entry_id = d.entry_id"
                " ) ORDER BY d.entry_id",
                (owner,),
            )
            rows = [dict(row) for row in await cur.fetchall()]

    active: list[dict[str, Any]] = []
    for row in rows:
        entry_id = normalize_uuid(row.get("entry_id"))
        if entry_id is None:
            continue
        valid_from, valid_until = normalize_validity_window(
            row.get("valid_from"), row.get("valid_until")
        )
        current_metadata = row.get("current_metadata")
        if not isinstance(current_metadata, dict):
            current_metadata = {}
        projection_drift = (
            str(row.get("approved_content_hash") or "")
            != str(row.get("current_content_hash") or "")
            or row.get("current_status") != "active"
            or current_metadata.get("kind") != "constraint"
            or current_metadata.get("lifecycle", "active") != "active"
        )
        active.append(
            {
                "entry_id": entry_id,
                "denied_tools": denied_tools_as_json(row.get("denied_tools")),
                "valid_from": valid_from,
                "valid_until": valid_until,
                "integrity_mismatch": projection_drift,
            }
        )
    return active


async def has_active_denials(*, user_id: str, entry_ids: tuple[str, ...] | list[str]) -> bool:
    """Check frozen authority for a bounded set, independent of projection metadata."""
    owner = refactor_owner_uuid(user_id)
    ids = tuple(normalize_uuid(raw) for raw in entry_ids)
    if not ids:
        return False
    if owner is None or any(entry_id is None for entry_id in ids):
        raise ValueError("owner and valid policy source IDs are required")
    unique_ids = tuple(dict.fromkeys(ids))
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT EXISTS (SELECT 1 FROM memory_tool_denials d"
                " WHERE d.user_id = %s AND d.entry_id = ANY(%s::uuid[])"
                " AND d.active IS TRUE AND d.approval_source = 'manual'"
                " AND NOT EXISTS (SELECT 1 FROM memory_forget_tombstones f"
                " WHERE f.user_id = d.user_id AND f.entry_id = d.entry_id))",
                (owner, list(unique_ids)),
            )
            row = await cur.fetchone()
    return bool(row and row[0])


async def get_denials_snapshot(*, user_id: str) -> list[dict[str, Any]]:
    """Owner tool-denial rows an activation may mutate, for rollback."""
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        return []
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT user_id::text, entry_id::text, denied_tools,"
                " approved_content_hash, valid_from, valid_until,"
                " approval_source, approved_at, active"
                " FROM memory_tool_denials WHERE user_id = %s"
                " ORDER BY entry_id",
                (owner,),
            )
            return [dict(row) for row in await cur.fetchall()]


async def restore_denials_snapshot(
    *, user_id: str, rows: list[dict[str, Any]], entry_ids: list[str] | None = None
) -> None:
    """Restore the owner tool-denial rows affected by the activation.

    ``entry_ids`` scopes the delete to the entries this activation could have
    touched (snapshotted rows plus newly projected entries); unrelated owner
    denials are never deleted by a rollback.
    """
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        return
    scoped = [
        normalized
        for raw in (entry_ids or [])
        if (normalized := normalize_uuid(raw)) is not None
    ]
    async with pool.get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                if scoped:
                    await cur.execute(
                        "DELETE FROM memory_tool_denials"
                        " WHERE user_id = %s AND entry_id = ANY(%s::uuid[])",
                        (owner, scoped),
                    )
                for row in rows:
                    await cur.execute(
                        "INSERT INTO memory_tool_denials"
                        " (user_id, entry_id, denied_tools,"
                        "  approved_content_hash, valid_from, valid_until,"
                        "  approval_source, approved_at, active)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                        " ON CONFLICT (user_id, entry_id) DO NOTHING",
                        (
                            owner,
                            row.get("entry_id"),
                            SafeJson(row.get("denied_tools") or []),
                            str(row.get("approved_content_hash") or ""),
                            row.get("valid_from"),
                            row.get("valid_until"),
                            str(row.get("approval_source") or "manual"),
                            row.get("approved_at"),
                            bool(row.get("active")),
                        ),
                    )


async def replace_denials(
    *,
    user_id: str,
    entry_id: str,
    denied_tools: Any,
    approval_source: str,
    approved_content_hash: str,
    valid_from: Any = None,
    valid_until: Any = None,
    replaced_entry_ids: tuple[str, ...] = (),
) -> None:
    """Persist a denial set only for an owner-authorized manual decision."""
    owner = refactor_owner_uuid(user_id)
    entry = normalize_uuid(entry_id)
    if owner is None or entry is None:
        raise ValueError("owner and memory entry are required")
    if approval_source != "manual":
        raise ValueError("tool-denial policy requires manual owner approval")
    if (
        not isinstance(approved_content_hash, str)
        or len(approved_content_hash) != 71
        or not approved_content_hash.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in approved_content_hash[7:])
    ):
        raise ValueError("a content hash from the approved memory revision is required")
    normalized = denied_tools_as_json(denied_tools)
    approved_start, approved_end = normalize_validity_window(valid_from, valid_until)
    replaced_values = tuple(replaced_entry_ids)
    replaced_normalized = tuple(normalize_uuid(raw) for raw in replaced_values)
    if any(value is None for value in replaced_normalized):
        raise ValueError("replacement entry IDs must be valid UUIDs")
    replaced = tuple(dict.fromkeys(replaced_normalized))
    async with pool.get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                constraint_clause = (
                    " AND metadata->>'kind' = 'constraint'" if normalized else ""
                )
                await cur.execute(
                    "SELECT m.content_hash FROM memory_entries m"
                    " WHERE m.user_id = %s AND m.entry_id = %s"
                    " AND m.tier = 'user' AND m.status = 'active'"
                    " AND m.entry_key NOT LIKE 'proposed/%%'"
                    " AND NOT EXISTS (SELECT 1 FROM memory_forget_tombstones f"
                    " WHERE f.user_id = m.user_id AND f.entry_id = m.entry_id)"
                    + constraint_clause.replace("metadata->>'kind'", "m.metadata->>'kind'")
                    + " FOR UPDATE",
                    (owner, entry),
                )
                entry_row = await cur.fetchone()
                if entry_row is None or str(entry_row[0]) != approved_content_hash:
                    raise ValueError("approved owner memory revision is required")
                if replaced:
                    await cur.execute(
                        "UPDATE memory_tool_denials SET active = FALSE,"
                        " approved_at = NOW() WHERE user_id = %s"
                        " AND entry_id = ANY(%s::uuid[]) AND active IS TRUE",
                        (owner, list(replaced)),
                    )
                if normalized:
                    await cur.execute(
                        "INSERT INTO memory_tool_denials"
                        " (user_id, entry_id, denied_tools, approved_content_hash,"
                        " valid_from, valid_until, approval_source, active)"
                        " VALUES (%s, %s, %s, %s, %s::timestamptz,"
                        " %s::timestamptz, 'manual', TRUE)"
                        " ON CONFLICT (user_id, entry_id) DO UPDATE SET"
                        " denied_tools = EXCLUDED.denied_tools,"
                        " approved_content_hash = EXCLUDED.approved_content_hash,"
                        " valid_from = EXCLUDED.valid_from,"
                        " valid_until = EXCLUDED.valid_until, active = TRUE,"
                        " approval_source = 'manual', approved_at = NOW()",
                        (
                            owner,
                            entry,
                            SafeJson(normalized),
                            approved_content_hash,
                            approved_start,
                            approved_end,
                        ),
                    )
                else:
                    await cur.execute(
                        "UPDATE memory_tool_denials SET active = FALSE,"
                        " approved_at = NOW() WHERE user_id = %s AND entry_id = %s",
                        (owner, entry),
                    )


async def purge_forgotten_denials(
    *, user_id: str, entry_ids: tuple[str, ...] | list[str]
) -> int:
    """Purge forgotten policy data only after every requested ID is tombstoned."""
    owner = refactor_owner_uuid(user_id)
    normalized_ids = tuple(normalize_uuid(raw) for raw in entry_ids)
    if owner is None or any(entry_id is None for entry_id in normalized_ids):
        raise ValueError("owner and valid forgotten entry IDs are required")
    unique_ids = tuple(dict.fromkeys(normalized_ids))
    if not unique_ids:
        return 0
    async with pool.get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT count(*) FROM memory_forget_tombstones"
                    " WHERE user_id = %s AND entry_id = ANY(%s::uuid[])",
                    (owner, list(unique_ids)),
                )
                row = await cur.fetchone()
                if int(row[0] if row else 0) != len(unique_ids):
                    raise ValueError("all forgotten entries must be tombstoned first")
                await cur.execute(
                    "DELETE FROM memory_tool_denials WHERE user_id = %s"
                    " AND entry_id = ANY(%s::uuid[])",
                    (owner, list(unique_ids)),
                )
                return int(getattr(cur, "rowcount", 0) or 0)


async def copy_reverted_denials(
    *,
    user_id: str,
    source_entry_id: str,
    restored_entry_id: str,
    restored_content_hash: str,
    replaced_entry_ids: tuple[str, ...] = (),
) -> bool:
    """Copy frozen authority only for an explicit owner-requested revert.

    This is a fresh manual owner decision: an inactive historical authority
    may be restored with its original validity window and identifiers. A
    forgotten source cannot regain authority through revision restoration.
    """
    owner = refactor_owner_uuid(user_id)
    source = normalize_uuid(source_entry_id)
    restored = normalize_uuid(restored_entry_id)
    if owner is None or source is None or restored is None:
        raise ValueError("owner and valid source/restored entry IDs are required")
    if (
        not isinstance(restored_content_hash, str)
        or len(restored_content_hash) != 71
        or not restored_content_hash.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in restored_content_hash[7:])
    ):
        raise ValueError("restored memory content hash is required")
    replaced_values = tuple(replaced_entry_ids)
    replaced_normalized = tuple(normalize_uuid(raw) for raw in replaced_values)
    if any(value is None for value in replaced_normalized):
        raise ValueError("replacement entry IDs must be valid UUIDs")
    replaced = tuple(dict.fromkeys(replaced_normalized))
    async with pool.get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT m.content_hash, m.metadata->>'kind' AS kind"
                    " FROM memory_entries m WHERE m.user_id = %s"
                    " AND m.entry_id = %s AND m.tier = 'user'"
                    " AND m.status = 'active'"
                    " AND m.entry_key NOT LIKE 'proposed/%%'"
                    " AND NOT EXISTS (SELECT 1 FROM memory_forget_tombstones f"
                    " WHERE f.user_id = m.user_id AND f.entry_id = m.entry_id)"
                    " FOR UPDATE",
                    (owner, restored),
                )
                restored_row = await cur.fetchone()
                if (
                    restored_row is None
                    or str(restored_row["content_hash"]) != restored_content_hash
                ):
                    raise ValueError("restored owner revision is not verified")

                async def retire_replaced() -> None:
                    if replaced:
                        await cur.execute(
                            "UPDATE memory_tool_denials SET active = FALSE,"
                            " approved_at = NOW() WHERE user_id = %s"
                            " AND entry_id = ANY(%s::uuid[]) AND active IS TRUE",
                            (owner, list(replaced)),
                        )

                await cur.execute(
                    "SELECT d.denied_tools, d.valid_from, d.valid_until"
                    " FROM memory_tool_denials d"
                    " WHERE d.user_id = %s AND d.entry_id = %s"
                    " AND d.approval_source = 'manual'"
                    " AND NOT EXISTS (SELECT 1 FROM memory_forget_tombstones f"
                    " WHERE f.user_id = d.user_id AND f.entry_id = d.entry_id)"
                    " FOR UPDATE",
                    (owner, source),
                )
                original = await cur.fetchone()
                if original is None:
                    await retire_replaced()
                    return False
                denied_tools = denied_tools_as_json(original["denied_tools"])
                if not denied_tools or restored_row.get("kind") != "constraint":
                    await retire_replaced()
                    return False
                await retire_replaced()
                await cur.execute(
                    "INSERT INTO memory_tool_denials"
                    " (user_id, entry_id, denied_tools, approved_content_hash,"
                    " valid_from, valid_until, approval_source, active)"
                    " VALUES (%s, %s, %s, %s, %s, %s, 'manual', TRUE)"
                    " ON CONFLICT (user_id, entry_id) DO UPDATE SET"
                    " denied_tools = EXCLUDED.denied_tools,"
                    " approved_content_hash = EXCLUDED.approved_content_hash,"
                    " valid_from = EXCLUDED.valid_from,"
                    " valid_until = EXCLUDED.valid_until, active = TRUE,"
                    " approval_source = 'manual', approved_at = NOW()",
                    (
                        owner,
                        restored,
                        SafeJson(denied_tools),
                        restored_content_hash,
                        original["valid_from"],
                        original["valid_until"],
                    ),
                )
    return True
