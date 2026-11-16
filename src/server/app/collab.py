"""``/api/v1/collab``: the HTTP surface of the common collaboration protocol.

One route per tool, exactly the N80 4.1 table (and the L3 client's
``ENDPOINTS``). Path parameters (``ref``, ``lease_id``, ``handoff_id``,
``review_id``) go in the URL; a ``GET`` takes the rest as query parameters
(booleans ``true``/``false``, integers, repeated keys for arrays) and a
``POST`` takes a JSON object body. The tool input is rebuilt as
``{**body_or_query, **path}`` and handed to
``src.server.services.collab.tools.invoke``, which owns every check. Each
answer is the collab envelope with the HTTP status from ``ERROR_CODES``.

Tool routes accept only ``Authorization: Bearer <app token>``; the
operator's ``/admin/apps`` routes require the internal service token in
``X-Service-Token`` (with ``X-User-Id``, as for any service caller) and never
accept an app token. Every route except the read-only
``/health`` refuses with ``mode_denied`` while ``CONDUCTOR_COLLAB_MODE`` is
``off`` (the default). A request body is read only after the loopback and
mode checks (for ``/admin/apps``, after the operator credential) and never
past ``MAX_BODY_BYTES`` (``too_large``, 413).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.collab.envelope import error_envelope, ok_envelope
from src.collab.schemas import TOOL_INPUT_SCHEMAS, schema_for
from src.collab.types import CollabError, InvalidSchemaError, TooLargeError
from src.server.database import encryption
from src.server.services.collab import apps as app_svc
from src.server.services.collab import auth, tools

tools.load_handlers()

router = APIRouter(prefix="/api/v1/collab", tags=["collab"])

#: Marker key for a body that is not a JSON object: the schema refuses it,
#: so the refusal is audited like any other invalid input.
_BAD_BODY = "__body_not_a_json_object__"


def _respond(status: int, envelope: dict[str, Any]) -> JSONResponse:
    headers = {}
    retry = envelope.get("retry_after")
    if status == 429 and isinstance(retry, (int, float)):
        headers["Retry-After"] = str(max(1, int(round(retry))))
    return JSONResponse(envelope, status_code=status, headers=headers)


def _client_host(request: Request) -> str | None:
    return request.client.host if request.client else None


#: The largest request body any collab route reads (a task body is at most
#: 8000 characters, a report or message far less). A declared
#: ``Content-Length`` over it is refused before a byte is read, and a body
#: without one stops being read at the first chunk past it: ``too_large``.
MAX_BODY_BYTES = 256 * 1024


async def _read_capped(request: Request, limit: int = MAX_BODY_BYTES) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        text = declared.strip()
        if not text.isdigit():
            raise InvalidSchemaError("invalid_schema: Content-Length is not a byte count")
        if int(text) > limit:
            raise TooLargeError(f"too_large: the request body is over {limit} bytes")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise TooLargeError(f"too_large: the request body is over {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def _body(request: Request) -> dict[str, Any]:
    raw = await _read_capped(request)
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {_BAD_BODY: True}
    return value if isinstance(value, dict) else {_BAD_BODY: True}


def _coerce(value: str, spec: dict[str, Any]) -> Any:
    kind = spec.get("type")
    if kind == "boolean":
        return {"true": True, "false": False}.get(value, value)
    if kind == "integer":
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    return value


def _query(request: Request, tool: str) -> dict[str, Any]:
    """Query parameters typed by the tool's schema; unknown keys stay text."""
    properties = schema_for(TOOL_INPUT_SCHEMAS[tool]).get("properties", {})
    out: dict[str, Any] = {}
    for key in request.query_params.keys():
        spec = properties.get(key, {})
        values = request.query_params.getlist(key)
        if spec.get("type") == "array":
            item_spec = spec.get("items", {})
            out[key] = [_coerce(item, item_spec) for item in values]
        else:
            out[key] = _coerce(values[-1], spec)
    return out


async def _call(request: Request, tool: str, path: dict[str, str] | None = None) -> JSONResponse:
    # A POST body is read by ``invoke`` only after the loopback and mode
    # checks pass, and never past ``MAX_BODY_BYTES``.
    is_get = request.method == "GET"
    arguments = {**(_query(request, tool) if is_get else {}), **(path or {})}

    async def read_body() -> dict[str, Any]:
        return await _body(request)

    status, envelope = await tools.invoke(
        tool,
        arguments=arguments,
        headers=dict(request.headers),
        client_host=_client_host(request),
        read_arguments=None if is_get else read_body,
    )
    return _respond(status, envelope)


# --- health (read-only; the one route open while the mode is off) ----------


@router.get("/health")
async def collab_health(request: Request) -> JSONResponse:
    try:
        auth.require_loopback(_client_host(request), dict(request.headers))
    except Exception as exc:  # noqa: BLE001 - ScopeDeniedError
        return _respond(403, error_envelope("scope_denied", str(exc)))
    return _respond(200, ok_envelope(tools.mode_health(), untrusted=[]))


# --- tools (N80 4.1) ---------------------------------------------------------


@router.get("/whoami")
async def collab_whoami(request: Request) -> JSONResponse:
    return await _call(request, "whoami")


@router.get("/tasks")
async def collab_task_list(request: Request) -> JSONResponse:
    return await _call(request, "task_list")


@router.post("/tasks")
async def collab_task_create(request: Request) -> JSONResponse:
    return await _call(request, "task_create")


@router.get("/tasks/{ref}")
async def collab_task_get(ref: str, request: Request) -> JSONResponse:
    return await _call(request, "task_get", {"ref": ref})


@router.post("/tasks/{ref}/claim")
async def collab_task_claim(ref: str, request: Request) -> JSONResponse:
    return await _call(request, "task_claim", {"ref": ref})


@router.post("/leases/{lease_id}/heartbeat")
async def collab_task_heartbeat(lease_id: str, request: Request) -> JSONResponse:
    return await _call(request, "task_heartbeat", {"lease_id": lease_id})


@router.post("/leases/{lease_id}/release")
async def collab_task_release(lease_id: str, request: Request) -> JSONResponse:
    return await _call(request, "task_release", {"lease_id": lease_id})


@router.post("/leases/{lease_id}/report")
async def collab_task_report(lease_id: str, request: Request) -> JSONResponse:
    return await _call(request, "task_report", {"lease_id": lease_id})


@router.post("/leases/{lease_id}/handoff")
async def collab_handoff_request(lease_id: str, request: Request) -> JSONResponse:
    return await _call(request, "handoff_request", {"lease_id": lease_id})


@router.get("/handoffs")
async def collab_handoff_list(request: Request) -> JSONResponse:
    return await _call(request, "handoff_list")


@router.post("/handoffs/{handoff_id}/accept")
async def collab_handoff_accept(handoff_id: str, request: Request) -> JSONResponse:
    return await _call(request, "handoff_accept", {"handoff_id": handoff_id})


@router.post("/messages")
async def collab_agent_ask(request: Request) -> JSONResponse:
    return await _call(request, "agent_ask")


@router.get("/messages/inbox")
async def collab_agent_inbox(request: Request) -> JSONResponse:
    return await _call(request, "agent_inbox")


@router.post("/reviews")
async def collab_review_request(request: Request) -> JSONResponse:
    return await _call(request, "review_request")


@router.get("/reviews")
async def collab_review_list(request: Request) -> JSONResponse:
    return await _call(request, "review_list")


@router.post("/reviews/{review_id}/verdict")
async def collab_review_submit(review_id: str, request: Request) -> JSONResponse:
    return await _call(request, "review_submit", {"review_id": review_id})


# --- operator app management (L3 ADMIN_ENDPOINTS) ----------------------------


def _user_resolver(request: Request):
    async def resolve() -> str:
        from fastapi.security import HTTPAuthorizationCredentials

        from src.server.utils.api import get_current_user_id

        header = request.headers.get("authorization") or ""
        scheme, _, token = header.partition(" ")
        credentials = (
            HTTPAuthorizationCredentials(scheme=scheme, credentials=token.strip())
            if scheme.lower() == "bearer" and token.strip()
            else None
        )
        return await get_current_user_id(request, credentials)

    return resolve


#: Admin actions that write a new app token to the user's vault, which
#: needs ``BYOK_ENCRYPTION_KEY``. Without it they would fail deep in the
#: vault write and answer an opaque ``internal_error``.
_VAULT_ACTIONS = frozenset({"register", "rotate"})


class _EncryptionKeyMissing(CollabError):
    """Raised from the body reader, i.e. only after the loopback, mode and
    operator checks passed, so an unauthenticated caller never learns how
    the server is configured. ``admin_invoke`` audits it as a refusal; the
    route then answers ``byok_key_missing`` (503) in place of its envelope."""


def _key_missing_envelope() -> dict[str, Any]:
    envelope = error_envelope(
        "internal_error",
        encryption.ENCRYPTION_KEY_MISSING_MESSAGE,
        data={"missing_setting": encryption.ENCRYPTION_KEY_ENV},
        untrusted=[],
    )
    # The collab vocabulary has no code for a server configuration gap;
    # this one is specific to the operator routes and the CLI knows it.
    envelope["code"] = encryption.ENCRYPTION_KEY_MISSING_CODE
    return envelope


async def _admin(
    request: Request, action: str, app_id: str = "", *, with_body: bool = False
) -> JSONResponse:
    key_missing = False

    async def read_body() -> dict[str, Any]:
        nonlocal key_missing
        if action in _VAULT_ACTIONS and not encryption.encryption_configured():
            key_missing = True
            raise _EncryptionKeyMissing(
                encryption.ENCRYPTION_KEY_MISSING_MESSAGE, status_code=503
            )
        return await _body(request) if with_body else {}

    status, envelope = await app_svc.admin_invoke(
        action,
        headers=dict(request.headers),
        client_host=_client_host(request),
        resolve_user=_user_resolver(request),
        app_id=app_id,
        query=dict(request.query_params),
        read_body=read_body if (with_body or action in _VAULT_ACTIONS) else None,
    )
    if key_missing:
        return _respond(503, _key_missing_envelope())
    return _respond(status, envelope)


@router.post("/admin/apps")
async def collab_apps_register(request: Request) -> JSONResponse:
    return await _admin(request, "register", with_body=True)


@router.get("/admin/apps")
async def collab_apps_list(request: Request) -> JSONResponse:
    return await _admin(request, "list")


@router.post("/admin/apps/{app_id}/scopes")
async def collab_apps_scopes(app_id: str, request: Request) -> JSONResponse:
    return await _admin(request, "scopes", app_id, with_body=True)


@router.post("/admin/apps/{app_id}/rotate")
async def collab_apps_rotate(app_id: str, request: Request) -> JSONResponse:
    return await _admin(request, "rotate", app_id)


@router.post("/admin/apps/{app_id}/revoke")
async def collab_apps_revoke(app_id: str, request: Request) -> JSONResponse:
    return await _admin(request, "revoke", app_id)


@router.get("/admin/apps/{app_id}/audit")
async def collab_apps_audit(app_id: str, request: Request) -> JSONResponse:
    return await _admin(request, "audit", app_id)


__all__ = ["router"]
