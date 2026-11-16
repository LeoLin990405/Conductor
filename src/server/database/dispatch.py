"""Dispatch rows: the durable half of an auto-dispatch run.

The running orchestrator is the only writer; the API, the CLI and the deploy
preflight read what it last saved. The dispatch's own shape is carried in the
``lines``, ``outcomes`` and ``gates`` JSONB columns (and the ``spec_ref`` text
column, migration 141) so a state transition is one UPDATE rather than a
fan-out over child rows.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.pool import get_db_connection
from src.server.database.session_lock import release_session_lock
from src.server.database.sql_fences import advisory_key
from src.tools.auto_dispatch.gates_schema import migrate_dispatch_gates
from src.tools.auto_dispatch.types import Dispatch

logger = logging.getLogger(__name__)

_COLUMNS = """
    dispatch_id, user_id, goal, state, integrate_branch,
    workspace_id, repo_root, sandbox_policy, default_worker,
    approval_token, lines, outcomes, message, gates, spec_ref,
    created_at, updated_at
"""


class DispatchNotFoundError(KeyError):
    """No dispatch row with that id."""


def _dispatch(row: dict[str, Any] | None) -> Dispatch | None:
    if row is None:
        return None
    dispatch = Dispatch.from_dict(
        {
            "id": str(row["dispatch_id"]),
            "goal": row["goal"],
            "user_id": row["user_id"],
            "created_at": _iso(row["created_at"]),
            "workspace_id": row.get("workspace_id") or "",
            "repo_root": row.get("repo_root") or "",
            "sandbox_policy": row.get("sandbox_policy") or "",
            "default_worker": row.get("default_worker") or "",
            "lines": list(row.get("lines") or []),
            "integrate_branch": row.get("integrate_branch") or "",
            "state": row["state"],
            "approval_token": row.get("approval_token"),
            "outcomes": dict(row.get("outcomes") or {}),
            "message": row.get("message"),
            "gates": dict(row.get("gates") or {}),
            "spec_ref": row.get("spec_ref") or "",
        }
    )
    # N45: rows written by older code are upgraded when loaded.
    migrate_dispatch_gates(dispatch)
    return dispatch


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


async def create_dispatch(dispatch: Dispatch) -> Dispatch:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO dispatches (
                    dispatch_id, user_id, goal, state, integrate_branch,
                    approval_token, lines, outcomes, message, gates, spec_ref,
                    workspace_id, repo_root, sandbox_policy, default_worker
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {_COLUMNS}
                """,
                (
                    dispatch.id,
                    dispatch.user_id,
                    dispatch.goal,
                    dispatch.state.value,
                    dispatch.integrate_branch,
                    dispatch.approval_token,
                    Json([line.to_dict() for line in dispatch.lines]),
                    Json({key: value.to_dict() for key, value in dispatch.outcomes.items()}),
                    dispatch.message,
                    Json(dict(dispatch.gates)),
                    dispatch.spec_ref,
                    dispatch.workspace_id,
                    dispatch.repo_root,
                    dispatch.sandbox_policy,
                    dispatch.default_worker,
                ),
            )
            row = await cur.fetchone()
    saved = _dispatch(row)
    assert saved is not None
    return saved


async def save_dispatch(dispatch: Dispatch) -> Dispatch:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dispatches
                SET state = %s,
                    integrate_branch = %s,
                    approval_token = %s,
                    lines = %s,
                    outcomes = %s,
                    message = %s,
                    gates = %s,
                    spec_ref = %s,
                    workspace_id = %s,
                    repo_root = %s,
                    sandbox_policy = %s,
                    default_worker = %s,
                    updated_at = NOW()
                WHERE dispatch_id = %s
                RETURNING {_COLUMNS}
                """,
                (
                    dispatch.state.value,
                    dispatch.integrate_branch,
                    dispatch.approval_token,
                    Json([line.to_dict() for line in dispatch.lines]),
                    Json({key: value.to_dict() for key, value in dispatch.outcomes.items()}),
                    dispatch.message,
                    Json(dict(dispatch.gates)),
                    dispatch.spec_ref,
                    dispatch.workspace_id,
                    dispatch.repo_root,
                    dispatch.sandbox_policy,
                    dispatch.default_worker,
                    dispatch.id,
                ),
            )
            row = await cur.fetchone()
    if row is None:
        raise DispatchNotFoundError(dispatch.id)
    saved = _dispatch(row)
    assert saved is not None
    return saved


async def get_dispatch(dispatch_id: str) -> Dispatch | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM dispatches WHERE dispatch_id = %s",
                (dispatch_id,),
            )
            return _dispatch(await cur.fetchone())


async def list_dispatches(
    *,
    user_id: str | None = None,
    states: list[str] | None = None,
    limit: int = 50,
) -> list[Dispatch]:
    clauses: list[str] = []
    params: list[Any] = []
    if user_id is not None:
        clauses.append("user_id = %s")
        params.append(user_id)
    if states:
        clauses.append("state = ANY(%s)")
        params.append(list(states))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM dispatches {where} ORDER BY created_at DESC LIMIT %s",
                tuple(params),
            )
            rows = await cur.fetchall()
    return [dispatch for dispatch in (_dispatch(row) for row in rows) if dispatch]


async def pending_approval(user_id: str | None = None) -> list[Dispatch]:
    return await list_dispatches(user_id=user_id, states=["APPROVAL"], limit=100)


async def delete_dispatch(dispatch_id: str) -> bool:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM dispatches WHERE dispatch_id = %s", (dispatch_id,))
            return cur.rowcount > 0


#: One advisory key for every dispatch's path-ownership check-and-assign.
PATH_CLAIMS_LOCK_KEY = advisory_key("dispatch_path_claims")


@asynccontextmanager
async def path_claim_lock() -> AsyncIterator[None]:
    """Hold the path-claims advisory lock across a check and its assign save.

    Session-scoped, not ``pg_advisory_xact_lock``: the guarded section reads
    rows and saves the assignment on other pooled connections (each store call
    opens its own), so no single transaction spans it, and holding a
    transaction open across those awaits would pin a snapshot. The lock is
    released, or its session closed, however the holder exits.
    """
    async with get_db_connection() as conn:
        try:
            await conn.execute("SELECT pg_advisory_lock(%s)", (PATH_CLAIMS_LOCK_KEY,))
        except BaseException:
            await release_session_lock(conn, PATH_CLAIMS_LOCK_KEY)
            raise
        try:
            yield
        finally:
            await release_session_lock(conn, PATH_CLAIMS_LOCK_KEY)
