"""Dispatch workspace rows: the checkouts a dispatch may run against.

A row records one operator-owned git checkout: its repo root, the remote a
``git fetch`` updates, the sandbox policy its workers inherit, and the default
worker provider. The service layer resolves a workspace id to a repo root and
remote before a dispatch starts; this module only reads and writes rows.
"""

from __future__ import annotations

from typing import Any

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection

WORKSPACE_COLUMNS = """
    workspace_id, user_id, name, path, git_remote, sandbox_policy,
    default_worker, created_at, updated_at
"""

#: Fields an update may change. ``user_id`` and the timestamps are never
#: writable through ``update_workspace``.
UPDATABLE_FIELDS = ("name", "path", "git_remote", "sandbox_policy", "default_worker")


class WorkspaceNameTaken(Exception):
    """The user already has a workspace with this name."""


def _workspace(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["workspace_id"] = str(out.get("workspace_id") or "")
    for key in ("created_at", "updated_at"):
        value = out.get(key)
        out[key] = value.isoformat() if hasattr(value, "isoformat") else value
    return out


async def create_workspace(
    *,
    user_id: str,
    name: str,
    path: str,
    git_remote: str = "",
    sandbox_policy: str = "",
    default_worker: str = "",
) -> dict[str, Any]:
    try:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    INSERT INTO dispatch_workspaces (
                        user_id, name, path, git_remote, sandbox_policy,
                        default_worker
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING {WORKSPACE_COLUMNS}
                    """,
                    (user_id, name, path, git_remote, sandbox_policy, default_worker),
                )
                row = await cur.fetchone()
    except UniqueViolation as exc:
        raise WorkspaceNameTaken(name) from exc
    workspace = _workspace(row)
    assert workspace is not None
    return workspace


async def list_workspaces(user_id: str) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {WORKSPACE_COLUMNS}
                FROM dispatch_workspaces
                WHERE user_id = %s
                ORDER BY name, created_at
                """,
                (user_id,),
            )
            return [_workspace(row) or {} for row in await cur.fetchall()]


async def get_workspace(user_id: str, workspace_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {WORKSPACE_COLUMNS}
                FROM dispatch_workspaces
                WHERE user_id = %s AND workspace_id = %s
                """,
                (user_id, workspace_id),
            )
            return _workspace(await cur.fetchone())


async def get_workspace_by_id(workspace_id: str) -> dict[str, Any] | None:
    """One workspace by id alone, for a caller that already checked ownership."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {WORKSPACE_COLUMNS}
                FROM dispatch_workspaces
                WHERE workspace_id = %s
                """,
                (workspace_id,),
            )
            return _workspace(await cur.fetchone())


async def get_workspace_by_name(user_id: str, name: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {WORKSPACE_COLUMNS}
                FROM dispatch_workspaces
                WHERE user_id = %s AND name = %s
                """,
                (user_id, name),
            )
            return _workspace(await cur.fetchone())


async def update_workspace(
    user_id: str, workspace_id: str, **fields: Any
) -> dict[str, Any] | None:
    """Change the given columns; a name collision raises ``WorkspaceNameTaken``."""
    changes = {key: value for key, value in fields.items() if key in UPDATABLE_FIELDS}
    if not changes:
        return await get_workspace(user_id, workspace_id)
    assignments = ", ".join(f"{key} = %s" for key in changes)
    values = [*changes.values(), user_id, workspace_id]
    try:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    UPDATE dispatch_workspaces
                    SET {assignments}, updated_at = NOW()
                    WHERE user_id = %s AND workspace_id = %s
                    RETURNING {WORKSPACE_COLUMNS}
                    """,
                    values,
                )
                return _workspace(await cur.fetchone())
    except UniqueViolation as exc:
        raise WorkspaceNameTaken(str(changes.get("name") or workspace_id)) from exc


async def delete_workspace(user_id: str, workspace_id: str) -> bool:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM dispatch_workspaces "
                "WHERE user_id = %s AND workspace_id = %s",
                (user_id, workspace_id),
            )
            return bool(cur.rowcount)
