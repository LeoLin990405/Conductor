"""Append-only collaboration audit rows.

Every call writes one row, including refusals. Parameters are reduced to a
SHA-256 digest before they touch the database, so an accidental token, task
body or review comment in a payload is not copied into the audit table.
"""

from __future__ import annotations

import re
from typing import Any

from psycopg.rows import dict_row

from src.collab.types import ERROR_CODES
from src.collab.types import payload_digest
from src.server.database.pool import get_db_connection

_AUDIT_COLUMNS = """
    audit_id, app_id, agent, tool, target, params_sha256, code, http_status,
    rate_limited, redacted, duration_ms, created_at
"""
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ZERO_DIGEST = "0" * 64


def _audit(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    if out.get("audit_id"):
        out["audit_id"] = str(out["audit_id"])
    if out.get("app_id"):
        out["app_id"] = str(out["app_id"])
    return out


async def record_audit(
    *,
    tool: str,
    app_id: str | None = None,
    agent: str = "",
    target: str = "",
    params: Any = None,
    params_sha256: str | None = None,
    code: str = "ok",
    http_status: int | None = None,
    rate_limited: bool = False,
    redacted: int = 0,
    duration_ms: int = 0,
    conn: Any = None,
) -> dict[str, Any]:
    """Write one call record; the raw ``params`` value is never persisted.

    Pass ``conn`` from :func:`collab_transaction` so the audit row joins the
    state change it describes: if the audit insert fails, the whole operation
    rolls back instead of committing a write with no matching audit row.
    """
    if params_sha256 is None:
        digest = _ZERO_DIGEST if params is None else payload_digest(params)
    else:
        digest = str(params_sha256).strip().lower()
        if not _SHA256.fullmatch(digest):
            raise ValueError("params_sha256 must be a SHA-256 hex digest")
    resolved_code = str(code or "ok")
    if http_status is None:
        http_status = ERROR_CODES.get(resolved_code, 500)
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO collab_audit (
                    app_id, agent, tool, target, params_sha256, code,
                    http_status, rate_limited, redacted, duration_ms
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {_AUDIT_COLUMNS}
                """,
                (
                    app_id,
                    agent[:64],
                    str(tool)[:100],
                    str(target)[:200],
                    digest,
                    resolved_code[:64],
                    int(http_status),
                    bool(rate_limited),
                    max(0, int(redacted)),
                    max(0, int(duration_ms)),
                ),
            )
            return _audit(await cur.fetchone())  # type: ignore[return-value]


async def list_audit(
    *,
    app_id: str | None = None,
    code: str | None = None,
    limit: int = 200,
    conn: Any = None,
) -> list[dict[str, Any]]:
    bounded = max(1, min(1000, int(limit)))
    conditions: list[str] = []
    params: list[Any] = []
    if app_id is not None:
        conditions.append("app_id = %s")
        params.append(app_id)
    if code is not None:
        conditions.append("code = %s")
        params.append(code)
    where = f"WHERE {' AND '.join(conditions)} " if conditions else ""
    params.append(bounded)
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_AUDIT_COLUMNS}
                FROM collab_audit
                {where}ORDER BY created_at DESC, audit_id DESC
                LIMIT %s
                """,
                tuple(params),
            )
            return [_audit(row) for row in await cur.fetchall() if row]  # type: ignore[misc]


record_call = record_audit


__all__ = ["list_audit", "record_audit", "record_call"]
