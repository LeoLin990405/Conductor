"""The one call pipeline every collab tool goes through (N80 3, 4, 8, 9).

``invoke`` is transport-neutral: the HTTP routes call it, and so could any
other front end. For every call, in this order:

1. loopback check (403), whatever the server bind;
2. rollout mode: ``off`` refuses everything (``mode_denied``), and a scope the
   stage does not allow is refused before authentication is attempted;
3. ``X-Service-Token`` / ``X-User-Id`` refused (401), bearer app token looked
   up by its SHA-256 digest (401 for missing, unknown, revoked or disabled);
4. the app's own scopes (``scope_denied``);
5. the in-process token bucket, which fails closed (``rate_limited``);
6. the tool's JSON schema, never repaired (``invalid_schema``);
7. credential-shaped text in a stored payload (``secret_in_payload``);
8. the tool handler, which applies the task-level mode limit and does the
   work. A write handler commits its state change and its audit row in one
   transaction through :meth:`ToolCall.finish`;
9. output redaction (known MCP and vault secrets) with the count in
   ``redacted``, and output schema validation.

Every call writes exactly one ``collab_audit`` row, refusals included.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from jsonschema import ValidationError

from src.collab import modes
from src.collab.envelope import error_envelope, ok_envelope
from src.collab.payload import assert_no_secrets
from src.collab.ratelimit import RateLimiter, get_default_limiter
from src.collab.schemas import validate_tool_input, validate_tool_output
from src.collab.scopes import TOOL_SCOPES, require_scope
from src.collab.types import (
    CollabError,
    InternalError,
    InvalidSchemaError,
    ModeDeniedError,
    RateLimitedError,
)
from src.server.database.collab import apps as app_db
from src.server.database.collab.transactions import collab_transaction
from src.server.services.collab import auth
from src.server.services.collab.audit import (
    AuditRecord,
    params_digest,
    write_audit,
    write_refusal_audit,
)

logger = logging.getLogger(__name__)

#: Fields whose text is stored or relayed; each is checked for credentials.
SECRET_CHECKED_FIELDS: dict[str, tuple[str, ...]] = {
    "task_create": ("title", "body", "write_scope", "acceptance"),
    "agent_ask": ("text",),
    "handoff_request": ("note", "self_report"),
    "review_request": ("focus",),
    "review_submit": ("verdict",),
    "task_release": ("note",),
    "task_heartbeat": ("progress",),
}

#: Argument keys that name the target of a call, for the audit ``target``.
_TARGET_KEYS = ("ref", "lease_id", "handoff_id", "review_id", "to")

#: Mode refusals that do not depend on a task; they are checked up front.
_PRE_AUTH_REASONS = frozenset(
    {modes.REASON_MODE_OFF, modes.REASON_UNKNOWN_TOOL, modes.REASON_SCOPE_NOT_IN_MODE}
)

Handler = Callable[["ToolCall"], Awaitable[dict[str, Any]]]
HANDLERS: dict[str, Handler] = {}


def handler(tool: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        if tool not in TOOL_SCOPES:
            raise ValueError(f"unknown collab tool {tool}")
        HANDLERS[tool] = fn
        return fn

    return register


# ---------------------------------------------------------------------------
# Seams: secrets and redaction (patched in tests; never read real vaults there)
# ---------------------------------------------------------------------------


async def vault_secrets(user_id: str) -> dict[str, str]:
    """The owner's redactable secrets; a failed read propagates (fail closed)."""
    from src.server.utils.secret_redactor import get_vault_secrets_for_redaction

    return await get_vault_secrets_for_redaction(user_id)


def redactor() -> Any:
    from src.server.utils.secret_redactor import get_redactor

    return get_redactor()


def _redact_text(text: str, engine: Any, vault: Mapping[str, str]) -> tuple[str, int]:
    cleaned = engine.redact(text, vault_secrets=dict(vault))
    if cleaned == text:
        return text, 0
    return cleaned, max(1, cleaned.count("[REDACTED:") - text.count("[REDACTED:"))


def redact_data(value: Any, vault: Mapping[str, str]) -> tuple[Any, int]:
    """Redact every string in ``value``; returns the copy and the fragment count."""
    engine = redactor()
    count = 0

    def walk(item: Any) -> Any:
        nonlocal count
        if isinstance(item, str):
            cleaned, hits = _redact_text(item, engine, vault)
            count += hits
            return cleaned
        if isinstance(item, Mapping):
            return {key: walk(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [walk(child) for child in item]
        return item

    return walk(value), count


def _extra_secrets(vault: Mapping[str, str]) -> tuple[str, ...]:
    values = [value for value in vault.values() if isinstance(value, str) and len(value) >= 8]
    service = os.getenv("INTERNAL_SERVICE_TOKEN", "")
    if len(service) >= 8:
        values.append(service)
    return tuple(values)


# ---------------------------------------------------------------------------
# The call object handlers receive
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    tool: str
    arguments: dict[str, Any]
    caller: auth.Caller
    mode: str
    environ: Mapping[str, str] | None
    vault: Mapping[str, str]
    record: AuditRecord
    audited: bool = False
    redacted: int = 0
    response: dict[str, Any] = field(default_factory=dict)

    @property
    def extra_secrets(self) -> tuple[str, ...]:
        return _extra_secrets(self.vault)

    def set_target(self, target: str) -> None:
        self.record.target = str(target or "")[:200]

    def require_task_mode(self, facts: modes.TaskFacts | None) -> None:
        """The task-level part of the mode table (docs limit, claimability)."""
        modes.require_mode(self.tool, mode=self.mode, task=facts, environ=self.environ)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Any]:
        async with collab_transaction() as conn:
            yield conn

    async def finish(self, conn: Any, data: dict[str, Any]) -> dict[str, Any]:
        """Redact the answer and write the ``ok`` audit row on ``conn``.

        Call it last inside the write's transaction: if the audit insert fails,
        the exception rolls the write back with it.
        """
        cleaned, count = redact_data(data, self.vault)
        _validate_output(self.tool, cleaned)
        self.redacted = count
        self.record.redacted = count
        await write_audit(self.record, code="ok", conn=conn)
        self.audited = True
        return cleaned


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


def _validate_output(tool: str, data: Any) -> None:
    """Refuse an off-schema answer; inside ``finish`` this rolls the write back."""
    try:
        validate_tool_output(tool, data)
    except ValidationError as exc:
        logger.error("[collab] %s produced an off-schema answer: %s", tool, exc.message)
        raise InternalError("the server produced an answer outside the schema") from exc


def _describe(record: AuditRecord, arguments: Mapping[str, Any]) -> None:
    """The audit row's target and parameter digest for ``arguments``."""
    record.target = _target_of(arguments)
    try:
        record.params_sha256 = params_digest(dict(arguments))
    except Exception:  # noqa: BLE001 - an undigestable payload is still audited
        record.params_sha256 = params_digest({})


def _target_of(arguments: Mapping[str, Any]) -> str:
    for key in _TARGET_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value[:200]
    return ""


def _pre_auth_mode(tool: str, mode: str) -> None:
    decision = modes.decide(tool, mode=mode)
    if not decision.allowed and decision.reason in _PRE_AUTH_REASONS:
        raise ModeDeniedError(
            f"{decision.reason}: {tool} is refused in collab mode {mode}",
            details={"reason": decision.reason},
        )


def _check_secrets(tool: str, arguments: Mapping[str, Any], extra: tuple[str, ...]) -> None:
    fields = SECRET_CHECKED_FIELDS.get(tool, ())
    payload = {name: arguments[name] for name in fields if name in arguments}
    if payload:
        assert_no_secrets(payload, extra_secrets=extra)


def _status(error: CollabError) -> int:
    return int(error.status_code)


async def invoke(
    tool: str,
    *,
    arguments: Mapping[str, Any] | None,
    headers: Mapping[str, str],
    client_host: str | None,
    environ: Mapping[str, str] | None = None,
    limiter: RateLimiter | None = None,
    read_arguments: Callable[[], Awaitable[Mapping[str, Any]]] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Run one collab tool call; returns ``(http_status, envelope)``.

    ``read_arguments`` (the HTTP body) is awaited only after the loopback and
    mode checks pass, and ``arguments`` (the path parameters) win over it.
    """
    args = dict(arguments or {})
    record = AuditRecord(tool=tool)
    _describe(record, args)
    authenticated = False
    try:
        if tool not in HANDLERS:
            raise InvalidSchemaError(f"unknown collaboration tool: {tool}")
        auth.require_loopback(client_host, headers)
        mode = modes.resolve_mode(None, environ=environ)
        _pre_auth_mode(tool, mode)
        if read_arguments is not None:
            args = {**dict(await read_arguments()), **args}
            _describe(record, args)
        auth.refuse_impersonation(headers)
        caller = await auth.authenticate(headers, agent=auth.agent_from(headers, args))
        authenticated = True
        record.app_id = caller.app_id
        record.agent = caller.agent
        require_scope(caller.scopes, tool)
        decision = (limiter or get_default_limiter()).evaluate(caller.app_id, tool)
        if not decision.allowed:
            record.rate_limited = True
            raise RateLimitedError(
                f"{decision.reason or 'limit_exceeded'}: the app has exceeded its "
                "collaboration rate limit",
                retry_after=decision.retry_after,
            )
        try:
            validate_tool_input(tool, args)
        except ValidationError as exc:
            where = "/".join(str(part) for part in exc.absolute_path) or "$"
            raise InvalidSchemaError(f"invalid_schema at {where}: {exc.validator}") from exc
        try:
            vault = await vault_secrets(caller.user_id)
        except Exception as exc:  # noqa: BLE001 - never serve unredacted output
            raise InternalError("the redaction secrets could not be loaded") from exc
        _check_secrets(tool, args, _extra_secrets(vault))
        try:
            await app_db.touch_app(caller.app_id)
        except Exception:  # noqa: BLE001 - last-used time is informational
            logger.debug("[collab] could not update last_used_at app=%s", caller.app_id)
        call = ToolCall(
            tool=tool,
            arguments=args,
            caller=caller,
            mode=mode,
            environ=environ,
            vault=vault,
            record=record,
        )
        data = await HANDLERS[tool](call)
        if not call.audited:
            data, call.redacted = redact_data(data, vault)
            record.redacted = call.redacted
        if not call.audited:
            _validate_output(tool, data)
            await write_audit(record, code="ok")
        return 200, ok_envelope(data, redacted=call.redacted)
    except CollabError as exc:
        await write_refusal_audit(
            record,
            code=exc.code,
            http_status=_status(exc),
            authenticated=authenticated,
            peer=client_host,
            limiter=limiter,
        )
        return _status(exc), error_envelope(
            exc.code, str(exc), retry_after=exc.retry_after
        )
    except Exception:  # noqa: BLE001 - one closed error shape, no traceback text
        logger.exception("[collab] %s failed", tool)
        await write_refusal_audit(
            record,
            code="internal_error",
            http_status=500,
            authenticated=authenticated,
            peer=client_host,
            limiter=limiter,
        )
        return 500, error_envelope("internal_error", "the collaboration call failed")


def mode_health(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The read-only health answer: the stage and which tools it allows."""
    mode = modes.resolve_mode(None, environ=environ)
    return {"mode": mode, "tools": list(modes.allowed_tools(mode))}


def load_handlers() -> None:
    """Import every handler module so ``HANDLERS`` is complete."""
    from src.server.services.collab import (  # noqa: F401
        apps,
        handoff,
        leases,
        messages,
        report,
        reviews,
        tickets,
    )


__all__ = [
    "HANDLERS",
    "SECRET_CHECKED_FIELDS",
    "ToolCall",
    "handler",
    "invoke",
    "load_handlers",
    "mode_health",
    "redact_data",
    "redactor",
    "vault_secrets",
]
