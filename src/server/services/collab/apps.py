"""App registration, scopes, rotation, revocation and ``whoami`` (N80 3.2).

The operator (the owner) manages apps through the ``/api/v1/collab/admin/apps``
routes. Every admin call needs an explicit operator credential: the internal
service token in ``X-Service-Token`` (compared in constant time with
``INTERNAL_SERVICE_TOKEN``), with the user named by ``X-User-Id`` exactly as
for any other service-token caller. The normal user check alone is not
enough, because in OSS mode it answers the local user with no credential.
An app token is never an admin credential, even next to the service token.
Registration starts with ``read`` unless more scopes are named.

Tokens: the server generates 32 random bytes, stores only the SHA-256 digest
in ``collab_apps``, writes the plaintext once to the user's vault as
``COLLAB_APP_TOKEN_<APP_ID>``, and answers it exactly once in
``data.token`` of the register/rotate response so the CLI can put it in the
keychain (``conductor-collab/<app_id>``). It never appears in a tool
response, a log line or an audit row. Rotation invalidates the old token at
once; revocation also releases every lease the app holds (requeuing their
Tickets) in the same transaction as the ``released`` events and the audit row.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.collab import modes
from src.collab.envelope import error_envelope, ok_envelope
from src.collab.scopes import normalize_scopes
from src.collab.types import (
    AssignmentRefusedError,
    CollabError,
    InvalidSchemaError,
    ModeDeniedError,
    NotFoundError,
    UnauthorizedError,
    normalize_app_id,
)
from src.server.database.collab import apps as app_db
from src.server.database.collab import audit as audit_db
from src.server.database.collab import leases as lease_db
from src.server.database.collab.transactions import collab_transaction
from src.server.services.collab import auth
from src.server.services.collab import tickets as task_svc
from src.server.services.collab.audit import (
    AuditRecord,
    params_digest,
    write_audit,
    write_refusal_audit,
)
from src.server.services.collab.tools import ToolCall, handler

logger = logging.getLogger(__name__)

KEYCHAIN_SERVICE = "conductor-collab"
ADMIN_ACTIONS = ("register", "list", "scopes", "rotate", "revoke", "audit")
_REGISTER_KEYS = frozenset({"app_id", "scopes", "can_write", "max_leases", "name"})
_SCOPES_KEYS = frozenset({"add", "remove"})


def vault_secret_name(app_id: str) -> str:
    return "COLLAB_APP_TOKEN_" + normalize_app_id(app_id).upper().replace("-", "_")


def keychain_ref(app_id: str) -> str:
    return f"keychain:{KEYCHAIN_SERVICE}/{normalize_app_id(app_id)}"


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def app_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """An app as the operator sees it: never a token, only a digest prefix."""
    return {
        "app_id": str(row.get("app_id") or ""),
        "name": str(row.get("name") or ""),
        "scopes": sorted(row.get("scopes") or []),
        "token_prefix": str(row.get("token_sha256") or "")[:8],
        "last_used_at": _iso(row.get("last_used_at")),
        "enabled": bool(row.get("enabled")),
        "revoked": row.get("revoked_at") is not None,
        "max_leases": int(row.get("max_leases") or 1),
        "can_write": bool(row.get("can_write")),
    }


# ---------------------------------------------------------------------------
# Vault seam (tests replace it; never a real vault there)
# ---------------------------------------------------------------------------


async def store_vault_token(user_id: str, app_id: str, token: str, *, conn: Any = None) -> None:
    """Write (or overwrite) the app token in the user's vault, in ``conn``'s transaction.

    The name carries ``SANDBOX_WITHHELD_PREFIXES``, so the row is never
    published to a sandbox or worker (``user_vault_secrets``).
    """
    from src.server.database import user_vault_secrets as vault_db

    name = vault_secret_name(app_id)
    if not vault_db.is_sandbox_withheld(name):  # pragma: no cover - a renamed prefix
        raise RuntimeError("a collab app token must be withheld from sandboxes")
    description = f"Conductor collab app token for {app_id}"
    if name in await vault_db.get_user_secret_names(user_id, conn=conn):
        await vault_db.update_user_secret(
            user_id, name, value=token, description=description, conn=conn
        )
        return
    await vault_db.create_user_secret(user_id, name, token, description, conn=conn)


# ---------------------------------------------------------------------------
# whoami
# ---------------------------------------------------------------------------


@handler("whoami")
async def whoami(call: ToolCall) -> dict[str, Any]:
    caller = call.caller
    data: dict[str, Any] = {
        "app_id": caller.app_id,
        "user_id": caller.user_id,
        "scopes": sorted(caller.scopes),
        "can_write": caller.can_write,
        "max_leases": caller.max_leases,
        "enabled": caller.enabled,
    }
    if caller.agent:
        data["agent"] = caller.agent
    return data


# ---------------------------------------------------------------------------
# Operator actions
# ---------------------------------------------------------------------------


@dataclass
class AdminCall:
    action: str
    user_id: str
    app_id: str
    body: dict[str, Any]
    query: dict[str, Any]
    record: AuditRecord


async def _owned_app(user_id: str, app_id: str) -> dict[str, Any]:
    row = await app_db.get_app(app_id)
    if row is None or str(row.get("user_id") or "") != user_id:
        raise NotFoundError("not_found: no such app")
    return row


def _bounded_leases(value: Any) -> int:
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3:
        raise InvalidSchemaError("max_leases must be an integer from 1 to 3")
    return value


def _scope_list(value: Any, name: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InvalidSchemaError(f"{name} must be a list of scope names")
    try:
        return normalize_scopes(value) if value else frozenset()
    except ValueError as exc:
        raise InvalidSchemaError(str(exc)) from exc


async def _register(call: AdminCall) -> dict[str, Any]:
    body = call.body
    if set(body) - _REGISTER_KEYS:
        raise InvalidSchemaError("unknown registration field")
    try:
        app_id = normalize_app_id(str(body.get("app_id") or ""))
    except ValueError as exc:
        raise InvalidSchemaError(str(exc)) from exc
    call.record.target = app_id
    scopes = _scope_list(body.get("scopes"), "scopes") or frozenset({"read"})
    can_write = body.get("can_write", False)
    if not isinstance(can_write, bool):
        raise InvalidSchemaError("can_write must be a boolean")
    max_leases = _bounded_leases(body.get("max_leases"))
    if await app_db.get_app(app_id) is not None:
        raise AssignmentRefusedError("app_exists: that app id is already registered")
    token = app_db.generate_token()
    async with collab_transaction() as conn:
        row = await app_db.create_app(
            user_id=call.user_id,
            app_id=app_id,
            token_sha256=app_db.hash_token(token),
            scopes=sorted(scopes),
            can_write=can_write,
            max_leases=max_leases,
            name=str(body.get("name") or "")[:200],
            conn=conn,
        )
        await store_vault_token(call.user_id, app_id, token, conn=conn)
        call.record.app_id = app_id
        await write_audit(call.record, code="ok", conn=conn)
    return {
        **app_view(row),
        "vault_secret": vault_secret_name(app_id),
        "keychain_ref": keychain_ref(app_id),
        "token": token,
    }


async def _list(call: AdminCall) -> dict[str, Any]:
    rows = await app_db.list_apps(call.user_id)
    await write_audit(call.record, code="ok")
    return {"apps": [app_view(row) for row in rows]}


async def _scopes(call: AdminCall) -> dict[str, Any]:
    if set(call.body) - _SCOPES_KEYS:
        raise InvalidSchemaError("scopes takes only add and remove")
    current = await _owned_app(call.user_id, call.app_id)
    add = _scope_list(call.body.get("add"), "add")
    remove = _scope_list(call.body.get("remove"), "remove")
    granted = (frozenset(current.get("scopes") or []) | add) - remove
    async with collab_transaction() as conn:
        row = await app_db.update_scopes(call.app_id, sorted(granted), conn=conn)
        if row is None:
            raise NotFoundError("not_found: the app is revoked")
        await write_audit(call.record, code="ok", conn=conn)
    return app_view(row)


async def _rotate(call: AdminCall) -> dict[str, Any]:
    current = await _owned_app(call.user_id, call.app_id)
    if current.get("revoked_at") is not None:
        raise NotFoundError("not_found: the app is revoked")
    token = app_db.generate_token()
    # The digest is written first and the vault after it, in one transaction:
    # an app revoked meanwhile (no row) leaves the vault holding the old
    # token, and a vault write that fails rolls the digest back, so the old
    # token keeps working and nothing is half-rotated either way.
    async with collab_transaction() as conn:
        row = await app_db.rotate_token(call.app_id, app_db.hash_token(token), conn=conn)
        if row is None:
            raise NotFoundError("not_found: the app is revoked")
        await store_vault_token(call.user_id, call.app_id, token, conn=conn)
        await write_audit(call.record, code="ok", conn=conn)
    return {**app_view(row), "vault_secret": vault_secret_name(call.app_id), "keychain_ref": keychain_ref(call.app_id), "token": token}


async def _revoke(call: AdminCall) -> dict[str, Any]:
    await _owned_app(call.user_id, call.app_id)
    async with collab_transaction() as conn:
        row = await app_db.revoke_app(call.app_id, conn=conn)
        released = await lease_db.release_app_leases(call.app_id, reason="revoked", conn=conn)
        for lease in released:
            if not lease.get("ticket_id"):
                continue
            await task_svc.record_event(
                conn,
                ticket={"ticket_id": lease["ticket_id"], "user_id": call.user_id},
                actor=f"app:{call.app_id}",
                collab_event="released",
                from_owner=f"app:{call.app_id}",
                to_owner="router",
                reason="revoked",
                extra={"lease_id": str(lease.get("lease_id") or ""), "by": "operator"},
            )
        await write_audit(call.record, code="ok", conn=conn)
    view = app_view(row or {"app_id": call.app_id})
    view["released_leases"] = len(released)
    return view


def _parse_since(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidSchemaError("since must be an ISO 8601 time") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _audit(call: AdminCall) -> dict[str, Any]:
    await _owned_app(call.user_id, call.app_id)
    raw_limit = call.query.get("limit", 200)
    try:
        limit = max(1, min(1000, int(raw_limit)))
    except (TypeError, ValueError) as exc:
        raise InvalidSchemaError("limit must be an integer") from exc
    since = _parse_since(call.query.get("since"))
    rows = await audit_db.list_audit(app_id=call.app_id, limit=limit)
    items = []
    for row in rows:
        created = row.get("created_at")
        if since is not None and isinstance(created, datetime):
            stamp = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
            if stamp < since:
                continue
        items.append(
            {
                "audit_id": str(row.get("audit_id") or ""),
                "agent": str(row.get("agent") or ""),
                "tool": str(row.get("tool") or ""),
                "target": str(row.get("target") or ""),
                "params_sha256": str(row.get("params_sha256") or ""),
                "code": str(row.get("code") or ""),
                "http_status": int(row.get("http_status") or 0),
                "rate_limited": bool(row.get("rate_limited")),
                "redacted": int(row.get("redacted") or 0),
                "duration_ms": int(row.get("duration_ms") or 0),
                "created_at": _iso(created),
            }
        )
    await write_audit(call.record, code="ok")
    return {"app_id": call.app_id, "audit": items}


_ACTIONS: dict[str, Callable[[AdminCall], Awaitable[dict[str, Any]]]] = {
    "register": _register,
    "list": _list,
    "scopes": _scopes,
    "rotate": _rotate,
    "revoke": _revoke,
    "audit": _audit,
}


async def admin_invoke(
    action: str,
    *,
    headers: Mapping[str, str],
    client_host: str | None,
    resolve_user: Callable[[], Awaitable[str]],
    app_id: str = "",
    body: Mapping[str, Any] | None = None,
    query: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    read_body: Callable[[], Awaitable[Mapping[str, Any]]] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Run one operator action; returns ``(http_status, envelope)``.

    ``read_body`` (the HTTP body) is awaited only once the operator has
    authenticated; refusals before that are metered per peer in the audit.
    """
    record = AuditRecord(tool=f"apps_{action}", target=str(app_id or "")[:200])
    record.params_sha256 = params_digest({**dict(body or {}), "app_id": app_id})
    authenticated = False
    try:
        if action not in _ACTIONS:
            raise InvalidSchemaError(f"unknown app action: {action}")
        auth.require_loopback(client_host, headers)
        mode = modes.resolve_mode(None, environ=environ)
        if mode == modes.OFF:
            raise ModeDeniedError(f"{modes.REASON_MODE_OFF}: collab mode is off")
        # The internal service token is the operator credential here (and
        # only here: the tool routes still refuse it). ``X-User-Id`` comes
        # with it, as on every other service-token call.
        auth.require_operator_credential(headers, environ)
        if await auth.token_belongs_to_app(headers):
            raise UnauthorizedError("app_token_refused: an app token cannot manage apps")
        try:
            user_id = await resolve_user()
        except CollabError:
            raise
        except Exception as exc:  # noqa: BLE001 - HTTPException from the user auth
            raise UnauthorizedError("unauthorized: operator authentication failed") from exc
        authenticated = True
        if read_body is not None:
            body = {**dict(body or {}), **dict(await read_body())}
            record.params_sha256 = params_digest({**body, "app_id": app_id})
        if app_id:
            try:
                app_id = normalize_app_id(app_id)
            except ValueError as exc:
                raise NotFoundError("not_found: no such app") from exc
            existing = await app_db.get_app(app_id)
            if existing is not None and str(existing.get("user_id") or "") == user_id:
                record.app_id = app_id
        call = AdminCall(
            action=action,
            user_id=user_id,
            app_id=app_id,
            body=dict(body or {}),
            query=dict(query or {}),
            record=record,
        )
        data = await _ACTIONS[action](call)
        return 200, ok_envelope(data, untrusted=[])
    except CollabError as exc:
        await write_refusal_audit(
            record,
            code=exc.code,
            http_status=int(exc.status_code),
            authenticated=authenticated,
            peer=client_host,
        )
        return int(exc.status_code), error_envelope(exc.code, str(exc), retry_after=exc.retry_after)
    except Exception:  # noqa: BLE001
        logger.exception("[collab] app action %s failed", action)
        await write_refusal_audit(
            record,
            code="internal_error",
            http_status=500,
            authenticated=authenticated,
            peer=client_host,
        )
        return 500, error_envelope("internal_error", "the app action failed")


__all__ = [
    "ADMIN_ACTIONS",
    "KEYCHAIN_SERVICE",
    "admin_invoke",
    "app_view",
    "keychain_ref",
    "store_vault_token",
    "vault_secret_name",
    "whoami",
]
