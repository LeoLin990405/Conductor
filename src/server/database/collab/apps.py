"""Persisted collaboration apps: digest-only tokens, scopes and lifecycle."""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Any

from psycopg.rows import dict_row

from src.collab.scopes import normalize_scopes
from src.collab.types import normalize_app_id
from src.server.database.pool import get_db_connection

_APP_COLUMNS = """
    app_id, user_id, name, token_sha256, scopes, can_write, max_leases,
    enabled, revoked_at, last_used_at, created_at, updated_at
"""
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def validate_token_sha256(value: str) -> str:
    """A stored token must be a SHA-256 digest, never a plaintext credential."""
    digest = str(value or "").strip().lower()
    if not _SHA256.fullmatch(digest):
        raise ValueError("token_sha256 must be a lower-case SHA-256 hex digest")
    return digest


def hash_token(token: str) -> str:
    """The only form of a bearer token that may reach the database."""
    if not isinstance(token, str) or not token:
        raise ValueError("a collab token must be a non-empty string")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    """Generate 32 random bytes as URL-safe text. The caller writes it once."""
    return secrets.token_urlsafe(32)


def _app(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["app_id"] = str(out.get("app_id") or "")
    out["scopes"] = list(out.get("scopes") or [])
    return out


async def create_app(
    *,
    user_id: str,
    app_id: str,
    token_sha256: str,
    scopes: Any = None,
    can_write: bool = False,
    max_leases: int = 1,
    name: str = "",
    conn: Any = None,
) -> dict[str, Any]:
    """Register one app. The caller already holds the plaintext token.

    ``token_sha256`` is validated here as well as at the route so an accidental
    plaintext write fails before it reaches Postgres. ``scopes`` defaults to
    the read-only registration grant. Pass ``conn`` from
    :func:`collab_transaction` to join the caller's transaction, for example
    when the registration and its audit row must land together.
    """
    normalized_id = normalize_app_id(app_id)
    digest = validate_token_sha256(token_sha256)
    normalized_scopes = sorted(normalize_scopes(scopes))
    if not 1 <= int(max_leases) <= 3:
        raise ValueError("max_leases must be between 1 and 3")
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO collab_apps (
                    user_id, app_id, name, token_sha256, scopes,
                    can_write, max_leases
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING {_APP_COLUMNS}
                """,
                (
                    user_id,
                    normalized_id,
                    name.strip(),
                    digest,
                    normalized_scopes,
                    bool(can_write),
                    int(max_leases),
                ),
            )
            return _app(await cur.fetchone())  # type: ignore[return-value]


async def get_app(app_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_APP_COLUMNS} FROM collab_apps WHERE app_id = %s",
                (normalize_app_id(app_id),),
            )
            return _app(await cur.fetchone())


async def app_for_token_hash(token_sha256: str) -> dict[str, Any] | None:
    """Resolve an authenticated caller; revoked and disabled apps do not match."""
    digest = validate_token_sha256(token_sha256)
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_APP_COLUMNS}
                FROM collab_apps
                WHERE token_sha256 = %s
                  AND revoked_at IS NULL
                  AND enabled = TRUE
                """,
                (digest,),
            )
            return _app(await cur.fetchone())


async def touch_app(app_id: str, *, conn: Any = None) -> None:
    async with get_db_connection(conn) as db:
        async with db.cursor() as cur:
            await cur.execute(
                "UPDATE collab_apps SET last_used_at = NOW() WHERE app_id = %s",
                (normalize_app_id(app_id),),
            )


async def update_scopes(
    app_id: str, scopes: Any, *, conn: Any = None
) -> dict[str, Any] | None:
    normalized = sorted(normalize_scopes(scopes))
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE collab_apps
                SET scopes = %s, updated_at = NOW()
                WHERE app_id = %s AND revoked_at IS NULL
                RETURNING {_APP_COLUMNS}
                """,
                (normalized, normalize_app_id(app_id)),
            )
            return _app(await cur.fetchone())


async def rotate_token(
    app_id: str, token_sha256: str, *, conn: Any = None
) -> dict[str, Any] | None:
    digest = validate_token_sha256(token_sha256)
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE collab_apps
                SET token_sha256 = %s, last_used_at = NULL, updated_at = NOW()
                WHERE app_id = %s AND revoked_at IS NULL
                RETURNING {_APP_COLUMNS}
                """,
                (digest, normalize_app_id(app_id)),
            )
            return _app(await cur.fetchone())


async def set_enabled(
    app_id: str, enabled: bool, *, conn: Any = None
) -> dict[str, Any] | None:
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE collab_apps
                SET enabled = %s, updated_at = NOW()
                WHERE app_id = %s AND revoked_at IS NULL
                RETURNING {_APP_COLUMNS}
                """,
                (bool(enabled), normalize_app_id(app_id)),
            )
            return _app(await cur.fetchone())


async def revoke_app(app_id: str, *, conn: Any = None) -> dict[str, Any] | None:
    """Revoke an app; its existing rounds are invalidated at authentication."""
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE collab_apps
                SET revoked_at = COALESCE(revoked_at, NOW()),
                    enabled = FALSE,
                    updated_at = NOW()
                WHERE app_id = %s
                RETURNING {_APP_COLUMNS}
                """,
                (normalize_app_id(app_id),),
            )
            return _app(await cur.fetchone())


async def list_apps(user_id: str | None = None) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if user_id is None:
                await cur.execute(
                    f"SELECT {_APP_COLUMNS} FROM collab_apps ORDER BY created_at DESC"
                )
            else:
                await cur.execute(
                    f"""
                    SELECT {_APP_COLUMNS}
                    FROM collab_apps
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    """,
                    (user_id,),
                )
            return [_app(row) for row in await cur.fetchall() if row]  # type: ignore[misc]


__all__ = [
    "app_for_token_hash",
    "create_app",
    "generate_token",
    "get_app",
    "hash_token",
    "list_apps",
    "revoke_app",
    "rotate_token",
    "set_enabled",
    "touch_app",
    "update_scopes",
    "validate_token_sha256",
]
