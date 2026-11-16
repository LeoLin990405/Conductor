"""Super-agent autonomy API: Sources first, then Tickets/Inbox.

Mounted under ``/api/v1/autonomy``. Every route is gated by the per-user
``super_agent`` feature; a Source stores only the vault *name* of its GitHub
PAT, never the token itself.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ValidationError
from psycopg.errors import UniqueViolation

from src.server.database.autonomy import a2a as a2a_db
from src.server.database.autonomy import clarifications as clarification_db
from src.server.database.autonomy import context_scopes as scope_db
from src.server.database.autonomy import lines as line_db
from src.server.database.autonomy import outbox as outbox_db
from src.server.database.autonomy import sources as source_db
from src.server.database.autonomy import tickets as tickets_db
from src.server.database.autonomy import handoffs as handoff_db
from src.server.database.user_vault_secrets import reveal_user_secret
from src.server.models.autonomy import (
    A2AAgentCreate,
    A2AAgentCreated,
    A2AAgentList,
    A2AAgentView,
    A2AEnvelope,
    A2AMessageList,
    A2AMessageView,
    AutonomyOverview,
    ContextScopeAttach,
    ContextScopeCreate,
    ContextScopeView,
    EmailSourceConfig,
    FeishuSourceConfig,
    GitHubSourceConfig,
    HandoffList,
    HandoffManifestRequest,
    HandoffRegisterRequest,
    HandoffVerifyRequest,
    HandoffVerifyResponse,
    HandoffView,
    LineCreate,
    LineView,
    OutboxList,
    OutboxView,
    PolicyUpdate,
    OverviewCounts,
    SlackSourceConfig,
    SourceCheckView,
    SourceCreate,
    SourceList,
    SourceUpdate,
    RefinementConfirmRequest,
    DaemonCreate,
    DaemonConcurrencyUpdate,
    DaemonReport,
    DaemonOutputBatch,
    DaemonProgress,
    DaemonStopAck,
    DaemonView,
    DaemonClaimRequest,
    DaemonHeartbeatRequest,
    DaemonWorktreeBind,
    DaemonWorktreeCleanup,
    SourceView,
    ManualTaskCreate,
    ScheduleCreate,
    ScheduleList,
    ScheduleUpdate,
    ScheduleView,
    ClarificationConfirmRequest,
    TicketLinesResponse,
    TicketReassignRequest,
    TicketLineSetRequest,
    TicketReplyRequest,
    TicketClarificationView,
    TaskActivity,
    TaskCloseRequest,
)
from src.server.services import features
from src.server.services.autonomy import a2a as a2a_service
from src.server.services.autonomy import context_scope as scope_service
from src.server.services.autonomy import ownership
from src.server.services.autonomy.email import (
    EmailConfig,
    EmailSourceError,
    check_email_source,
)
from src.server.services.autonomy.github import (
    GitHubConfig,
    GitHubSourceError,
    check_github_source,
)
from src.server.services.autonomy.clarify import confirm_clarification
from src.server.services.autonomy.prompt_refiner import (
    confirm_refinement,
    reject_refinement,
)
from src.server.services.autonomy.feishu import check_feishu_source
from src.server.services.autonomy.slack import (
    SlackConfig,
    SlackSourceError,
    check_slack_source,
)
from src.server.services.autonomy.progress import build_ticket_progress
from src.server.services.autonomy.worker import append_followups
from src.server.utils.api import CurrentUserId, require_workspace_owner
from src.server.utils.error_sanitization import schema_names, validation_error_text

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/autonomy", tags=["Super-agent"])


async def _require_feature(user_id: str) -> None:
    if not await features.user_feature_enabled(user_id, "super_agent"):
        # Hidden surface, not a permission error: the feature catalog is the
        # only thing that should tell a client this exists.
        raise HTTPException(status_code=404, detail="Feature not enabled")


def _view(row: dict[str, Any]) -> SourceView:
    return SourceView.model_validate(row)


async def _check_source(
    user_id: str, kind: str, config: dict[str, Any]
) -> dict[str, Any]:
    """Check one Source with the adapter its kind names."""
    if kind == "feishu":
        return await check_feishu_source(config)
    if kind == "email":
        return check_email_source(config)
    if kind == "slack":
        return check_slack_source(config)
    return await check_github_source(user_id=user_id, config=config)


@router.get("/overview", response_model=AutonomyOverview)
async def get_overview(x_user_id: CurrentUserId) -> AutonomyOverview:
    await _require_feature(x_user_id)
    from src.config.settings import get_autonomy_router_model

    sources = await source_db.list_sources(x_user_id)
    return AutonomyOverview(
        counts=OverviewCounts(**await tickets_db.counts(x_user_id)),
        sources=len(sources),
        enabled_sources=sum(1 for source in sources if source.get("enabled")),
        router_model=str(get_autonomy_router_model() or ""),
    )


@router.get("/sources", response_model=SourceList)
async def list_sources(x_user_id: CurrentUserId) -> SourceList:
    await _require_feature(x_user_id)
    return SourceList(
        sources=[_view(row) for row in await source_db.list_sources(x_user_id)]
    )


def _config_refusal(
    exc: ValidationError, model: type[BaseModel], *, status_code: int = 400
) -> HTTPException:
    """A Source config the model refused, as each field and its rule.

    ``str(exc)`` quotes every rejected value, and a Source config is where a
    vault secret's name, a mailbox or a webhook host is typed, which is the
    place a credential gets pasted by mistake.

    ``status_code`` is which answer that refusal is: a GitHub config is
    measured against its wire model before anything else looks at it, the way
    every route answers a body its own shape refuses, while the kinds whose
    model is checked after the shared prechecks keep the 400 a client that
    predates the wire models reads.
    """
    return HTTPException(
        status_code=status_code,
        detail=validation_error_text(exc, owned=schema_names(model)),
    )


async def _github_config(user_id: str, cfg: GitHubSourceConfig) -> dict[str, Any]:
    """The GitHub Source config that is stored, from one the model accepted.

    Every field the caller set survives into the dump — an admission cell, a
    narrowed channel, the approval flag — because a later poll, an eval and a
    reply all read the stored config, not the request.
    """
    # The model refuses a blank workspace id, so the only check left is whose
    # workspace the one it names is.
    if await _workspace(user_id, cfg.workspace_id) is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    # ``exclude_none`` keeps an unset optional out of the dump — a later
    # edit reads the absence, not a null — and
    # ``CredentialAllowlistConfig`` carries the allowlist's own omitted /
    # explicit-null distinction through it.
    dump = cfg.model_dump(exclude_none=True)
    # An approval flag the caller left out is not the model's own default:
    # the delivery channel decides, and a GitHub Source may narrow its channel
    # to one that holds by default (``email``). Storing the default would
    # silently override that, which is why ``update_source`` merges an omitted
    # flag out of the stored config for the same reason.
    if "require_approval" not in cfg.model_fields_set:
        dump.pop("require_approval", None)
    try:
        github_cfg = GitHubConfig.from_mapping(dump)
    except GitHubSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # The normalized list is the one the poll matches a sender against, so
    # it decides whether anyone can address this Source: a login that
    # normalizes away to nothing leaves nobody, whatever the caller typed.
    if not github_cfg.allowed_logins:
        raise HTTPException(
            status_code=400,
            detail="At least one allowed GitHub login is required",
        )
    # The vault is read by the name the poll will read it by, which is the
    # normalized one.
    if not await reveal_user_secret(user_id, github_cfg.secret_name):
        raise HTTPException(status_code=400, detail="vault secret not found")
    return dump


async def _validate_config(
    user_id: str, config: dict[str, Any] | None, kind: str
) -> dict[str, Any]:
    if config is None:
        raise HTTPException(status_code=400, detail="config is required")
    if kind == "github":
        # A GitHub config is measured against its own wire model before the
        # checks every kind shares: a field the body's shape refuses is that
        # field's answer, and the model already refuses a routing mode it does
        # not name, so the shared routing-mode check would only restate one.
        try:
            cfg = GitHubSourceConfig.model_validate(config)
        except ValidationError as exc:
            raise _config_refusal(exc, GitHubSourceConfig, status_code=422) from exc
        return await _github_config(user_id, cfg)
    routing_mode = config.get("routing_mode", "candidate")
    if routing_mode not in ("respond-only-explicit", "candidate", "ignore"):
        raise HTTPException(status_code=400, detail="Invalid Source routing_mode")
    workspace_id = str(config.get("workspace_id") or "")
    workspace = await _workspace(user_id, workspace_id) if workspace_id else None
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if kind == "feishu":
        try:
            cfg = FeishuSourceConfig.model_validate(config)
        except ValidationError as exc:
            raise _config_refusal(exc, FeishuSourceConfig) from exc
        if not cfg.allowed_logins:
            raise HTTPException(
                status_code=400,
                detail="At least one allowed Feishu open id is required",
            )
        return cfg.model_dump()
    if kind == "email":
        try:
            cfg = EmailSourceConfig.model_validate(config)
        except ValidationError as exc:
            raise _config_refusal(exc, EmailSourceConfig) from exc
        try:
            EmailConfig.from_mapping(cfg.model_dump())
        except EmailSourceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not await reveal_user_secret(user_id, cfg.secret_name):
            raise HTTPException(status_code=400, detail="vault secret not found")
        return cfg.model_dump()
    if kind == "slack":
        try:
            cfg = SlackSourceConfig.model_validate(config)
        except ValidationError as exc:
            raise _config_refusal(exc, SlackSourceConfig) from exc
        try:
            SlackConfig.from_mapping(cfg.model_dump())
        except SlackSourceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not await reveal_user_secret(user_id, cfg.webhook_secret_name):
            raise HTTPException(status_code=400, detail="vault secret not found")
        return cfg.model_dump()
    raise HTTPException(status_code=400, detail="Unsupported Source kind")


async def _workspace(user_id: str, workspace_id: str) -> dict[str, Any] | None:
    from src.server.database.workspace import get_workspace

    workspace = await get_workspace(workspace_id)
    if workspace is not None:
        require_workspace_owner(workspace, user_id=user_id)
    return workspace


def _native_cli_providers() -> set[str]:
    from src.llms.native_cli.registry import list_adapters
    from src.utils.provider_roles import provider_role

    return {
        str(row["provider"])
        for row in list_adapters()
        if provider_role(str(row["provider"])) == "native"
    }


async def _bind_daemon(
    user_id: str, daemon_id: str | None, worker_provider: str
) -> tuple[str | None, str]:
    """Validate user ownership and keep an assigned CLI aligned with its daemon."""
    daemon_id = str(daemon_id or "").strip()
    provider = str(worker_provider or "").strip().lower()
    if not daemon_id:
        return None, provider
    try:
        normalized_id = str(UUID(daemon_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid daemon id") from exc
    from src.server.database.autonomy import daemons as daemon_db

    daemon = await daemon_db.get_daemon(user_id, normalized_id)
    if daemon is None:
        raise HTTPException(status_code=404, detail="Daemon not found")
    daemon_provider = str(daemon.get("provider") or "").strip().lower()
    allowed = _native_cli_providers()
    if daemon_provider not in allowed:
        raise HTTPException(status_code=409, detail="Daemon provider is not supported")
    if provider and provider != daemon_provider:
        raise HTTPException(
            status_code=400,
            detail="worker_provider must match the assigned daemon provider",
        )
    return normalized_id, daemon_provider


def _scope_view(scope: dict[str, Any], ticket_ids: list[str]) -> ContextScopeView:
    return ContextScopeView.model_validate({**scope, "ticket_ids": ticket_ids})


async def _scope_tickets(scope_id: str) -> list[str]:
    return await scope_db.list_scope_ticket_ids(scope_id)


async def _attach_ticket_to_scope(
    *, scope: dict[str, Any], ticket_id: str, user_id: str
) -> dict[str, Any]:
    """Add the caller's Ticket to a scope, enforcing the explicit user mapping."""
    ticket = await tickets_db.get_ticket(ticket_id)
    if ticket is None or str(ticket.get("user_id") or "") != user_id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if str(ticket.get("status") or "") in {"completed", "cancelled", "archived"}:
        raise HTTPException(status_code=409, detail="Ticket has ended")
    scope_id = str(scope["scope_id"])
    current = str(ticket.get("context_scope_id") or "")
    if current == scope_id:
        return ticket
    if str(scope.get("kind")) != "shared":
        raise HTTPException(
            status_code=409,
            detail="A ticket cannot join another ticket's isolated scope",
        )
    policy = scope_service.policy_of(scope)
    ticket_user = str(ticket.get("user_id") or "")
    if ticket_user != str(scope.get("user_id") or ""):
        peers = set(scope.get("shared_with_user_ids") or [])
        if not policy.get("allow_cross_user") or ticket_user not in peers:
            raise HTTPException(
                status_code=403,
                detail="This user is not mapped into the context scope",
            )
    try:
        row = await scope_db.set_ticket_scope(
            scope_id=scope_id,
            ticket_id=ticket_id,
            max_participants=int(policy.get("max_participants") or 1),
        )
    except scope_db.ScopeFullError as exc:
        raise HTTPException(status_code=409, detail="Context scope is full") from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if str(row.get("context_scope_id") or "") != scope_id:
        raise HTTPException(
            status_code=409,
            detail="Ticket already belongs to another shared scope",
        )
    return ticket


@router.post("/context-scopes", response_model=ContextScopeView, status_code=201)
async def create_context_scope(
    body: ContextScopeCreate, x_user_id: CurrentUserId
) -> ContextScopeView:
    await _require_feature(x_user_id)
    try:
        # Only fields the caller set: the model's defaults would otherwise make
        # an isolated scope look like it asked for a multi-participant policy.
        policy = scope_service.validate_policy(
            body.kind, body.policy.model_dump(exclude_unset=True)
        )
    except scope_service.ContextScopeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    peers = sorted({peer.strip() for peer in body.shared_with_user_ids if peer.strip()})
    peers = [peer for peer in peers if peer != x_user_id]
    if body.kind == "isolated" and peers:
        raise HTTPException(
            status_code=400, detail="An isolated scope cannot name other users"
        )
    if policy.get("allow_cross_user") and not peers:
        raise HTTPException(
            status_code=400,
            detail="allow_cross_user needs at least one shared_with_user_ids entry",
        )
    try:
        scope = await scope_db.create_scope(
            user_id=x_user_id,
            name=body.name.strip(),
            kind=body.kind,
            shared_with_user_ids=peers,
            policy=policy,
        )
    except UniqueViolation as exc:
        raise HTTPException(
            status_code=409, detail="Scope name already exists"
        ) from exc
    if body.ticket_id and body.kind == "shared":
        await _attach_ticket_to_scope(
            scope=scope, ticket_id=body.ticket_id, user_id=x_user_id
        )
    return _scope_view(scope, await _scope_tickets(str(scope["scope_id"])))


@router.get("/context-scopes", response_model=list[ContextScopeView])
async def list_context_scopes(x_user_id: CurrentUserId) -> list[ContextScopeView]:
    await _require_feature(x_user_id)
    scopes = await scope_db.list_scopes(x_user_id)
    return [
        _scope_view(scope, await _scope_tickets(str(scope["scope_id"])))
        for scope in scopes
    ]


@router.post(
    "/context-scopes/{scope_id}/tickets",
    response_model=ContextScopeView,
    status_code=201,
)
async def attach_context_scope_ticket(
    scope_id: UUID, body: ContextScopeAttach, x_user_id: CurrentUserId
) -> ContextScopeView:
    await _require_feature(x_user_id)
    scope = await scope_db.get_scope(str(scope_id))
    if scope is None:
        raise HTTPException(status_code=404, detail="Context scope not found")
    if x_user_id != str(scope.get("user_id") or "") and x_user_id not in set(
        scope.get("shared_with_user_ids") or []
    ):
        raise HTTPException(status_code=404, detail="Context scope not found")
    await _attach_ticket_to_scope(
        scope=scope, ticket_id=body.ticket_id, user_id=x_user_id
    )
    return _scope_view(scope, await _scope_tickets(str(scope["scope_id"])))


def _a2a_http_error(exc: a2a_service.A2AError) -> HTTPException:
    """One error shape for every A2A failure: a machine code plus a message."""
    return HTTPException(
        status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}
    )


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise a2a_service.A2AError(
            "invalid A2A token", status_code=401, code="unauthenticated"
        )
    return authorization[7:].strip()


async def _authenticate_a2a_agent(
    authorization: str | None,
) -> tuple[dict[str, Any], str]:
    """Resolve the bearer credential to its (agent, token).

    A disabled feature answers exactly like a bad token: once ``authenticate``
    has succeeded, a distinct 404 would confirm the token to a guessing
    caller, so both failures are the same 401 ``unauthenticated``.
    """
    try:
        token = _bearer_token(authorization)
        agent = await a2a_service.authenticate(token)
    except a2a_service.A2AError as exc:
        raise _a2a_http_error(exc) from exc
    if not await features.user_feature_enabled(
        str(agent.get("user_id") or ""), "super_agent"
    ):
        raise _a2a_http_error(
            a2a_service.A2AError(
                "invalid A2A token", status_code=401, code="unauthenticated"
            )
        )
    return agent, token


def _message_view(row: dict[str, Any]) -> A2AMessageView:
    return A2AMessageView.model_validate(
        {
            **row,
            "agent_id": str(row.get("sender_agent_id") or ""),
            "scope_id": str(row.get("scope_id") or ""),
            "input_id": str(row["input_id"]) if row.get("input_id") else None,
        }
    )


@router.post("/a2a/agents", response_model=A2AAgentCreated, status_code=201)
async def register_a2a_agent(
    body: A2AAgentCreate, x_user_id: CurrentUserId
) -> A2AAgentCreated:
    await _require_feature(x_user_id)
    try:
        agent, token = await a2a_service.register_agent(
            user_id=x_user_id, ticket_id=body.ticket_id, name=body.name
        )
    except a2a_service.A2AError as exc:
        raise _a2a_http_error(exc) from exc
    return A2AAgentCreated.model_validate({**agent, "token": token})


@router.get("/a2a/agents", response_model=A2AAgentList)
async def list_a2a_agents(x_user_id: CurrentUserId) -> A2AAgentList:
    await _require_feature(x_user_id)
    return A2AAgentList(
        agents=[
            A2AAgentView.model_validate(row)
            for row in await a2a_db.list_agents(x_user_id)
        ]
    )


@router.delete("/a2a/agents/{agent_id}", status_code=204)
async def revoke_a2a_agent(agent_id: UUID, x_user_id: CurrentUserId) -> Response:
    await _require_feature(x_user_id)
    try:
        await a2a_service.revoke_agent(agent_id=str(agent_id), user_id=x_user_id)
    except a2a_service.A2AError as exc:
        raise _a2a_http_error(exc) from exc
    return Response(status_code=204)


@router.post("/a2a/messages", response_model=A2AMessageView, status_code=201)
async def send_a2a_message(
    body: A2AEnvelope,
    authorization: str | None = Header(None),
    x_a2a_signature: str | None = Header(None, alias="X-A2A-Signature"),
) -> A2AMessageView:
    try:
        agent, token = await _authenticate_a2a_agent(authorization)
        row = await a2a_service.send_message(
            agent=agent,
            token=token,
            envelope=body,
            signature=x_a2a_signature or "",
        )
    except a2a_service.A2AError as exc:
        raise _a2a_http_error(exc) from exc
    return _message_view(row)


@router.get("/a2a/messages", response_model=A2AMessageList)
async def list_a2a_messages(
    authorization: str | None = Header(None), limit: int = 50
) -> A2AMessageList:
    agent, _token = await _authenticate_a2a_agent(authorization)
    rows = await a2a_service.list_messages(
        str(agent.get("ticket_id") or ""), limit=limit
    )
    return A2AMessageList(messages=[_message_view(row) for row in rows])


@router.get("/metrics", response_model=Any)
async def get_metrics(x_user_id: CurrentUserId) -> Any:
    await _require_feature(x_user_id)
    from src.server.database.autonomy import metrics
    from src.server.services.autonomy.worker import max_worker_slots

    data = await metrics.pipeline_metrics(x_user_id)
    data["max_workers"] = max_worker_slots()
    return data


@router.get("/policy", response_model=Any)
async def get_policy(x_user_id: CurrentUserId) -> Any:
    await _require_feature(x_user_id)
    from src.server.models.autonomy import PolicyView
    from src.server.services.autonomy import policy

    row = await policy.current_policy(x_user_id)
    return PolicyView(
        accept_mode=str(row.get("accept_mode") or "explicit"),
        version=str(row.get("version") or ""),
        rules=list(row.get("rules") or []),
        admission=dict(row.get("admission") or {}),
    )


@router.patch("/policy", response_model=Any)
async def update_policy(body: PolicyUpdate, x_user_id: CurrentUserId) -> Any:
    await _require_feature(x_user_id)
    from src.server.models.autonomy import PolicyView
    from src.server.services.autonomy import policy

    before = await policy.current_policy(x_user_id)
    if body.admission is not None:
        row = await policy.set_admission(x_user_id, body.admission)
    elif body.accept_mode is not None:
        row = await policy.set_accept_mode(x_user_id, body.accept_mode)
    else:
        row = before
    if row.get("version") != before.get("version"):
        discarded = await policy.policy_changed(x_user_id)
        notified = await policy.notify_policy_change(
            x_user_id, before=before, after=row
        )
        if discarded or notified:
            logger.info(
                "[autonomy] policy change discarded evals=%s notified tickets=%s user=%s",
                discarded,
                notified,
                x_user_id,
            )
    return PolicyView(
        accept_mode=str(row.get("accept_mode") or "explicit"),
        version=str(row.get("version") or ""),
        rules=list(row.get("rules") or []),
        admission=dict(row.get("admission") or {}),
    )


@router.post("/sources", response_model=SourceView, status_code=201)
async def create_source(body: SourceCreate, x_user_id: CurrentUserId) -> SourceView:
    await _require_feature(x_user_id)
    config = await _validate_config(x_user_id, body.config, body.kind)
    daemon_id, provider = await _bind_daemon(
        x_user_id,
        config.get("worker_daemon_id"),
        str(config.get("worker_provider") or ""),
    )
    config["worker_daemon_id"] = daemon_id
    if daemon_id:
        config["worker_provider"] = provider
    try:
        row = await source_db.create_source(
            user_id=x_user_id, kind=body.kind, config=config, enabled=body.enabled
        )
    except UniqueViolation:
        raise HTTPException(
            status_code=409, detail="A Source for this repository already exists"
        ) from None
    try:
        checked = await _check_source(x_user_id, body.kind, config)
    except Exception as exc:  # noqa: BLE001 - source stays editable after a bad check
        logger.info("[autonomy] source check failed after create: %s", exc)
        row = (
            await source_db.update_source(
                x_user_id,
                row["source_id"],
                state={"last_error": str(exc)[:1000]},
            )
            or row
        )
    else:
        row = (
            await source_db.update_source(
                x_user_id,
                row["source_id"],
                state={
                    "agent_login": checked["agent_login"],
                    "repository": checked["repository"],
                    "last_error": "",
                    # Poll promptly after creation instead of waiting a full cycle.
                    "next_poll_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            or row
        )
    return _view(row)


@router.patch("/sources/{source_id}", response_model=SourceView)
async def update_source(
    source_id: UUID, body: SourceUpdate, x_user_id: CurrentUserId
) -> SourceView:
    await _require_feature(x_user_id)
    existing = await source_db.get_source(x_user_id, str(source_id))
    if existing is None:
        raise HTTPException(status_code=404, detail="Source not found")
    config = None
    state = None
    if body.config is not None:
        incoming = dict(body.config)
        existing_config = dict(existing.get("config") or {})
        # A UI rebuild often omits credential_allowlist. Replacing the stored
        # config wholesale would drop an explicit restriction back to the
        # permissive default; keep the stored policy unless the edit names it.
        if (
            "credential_allowlist" not in incoming
            and existing_config.get("credential_allowlist") is not None
        ):
            incoming["credential_allowlist"] = existing_config["credential_allowlist"]
        # An omitted approval flag must not reset reply delivery to the default.
        if (
            "require_approval" not in incoming
            and existing_config.get("require_approval") is not None
        ):
            incoming["require_approval"] = existing_config["require_approval"]
        config = await _validate_config(
            x_user_id, incoming, str(existing.get("kind") or "github")
        )
        daemon_id, provider = await _bind_daemon(
            x_user_id,
            config.get("worker_daemon_id"),
            str(config.get("worker_provider") or ""),
        )
        config["worker_daemon_id"] = daemon_id
        if daemon_id:
            config["worker_provider"] = provider
        # A different repository/chat invalidates the old cursor: reset the
        # read state, re-check the source, and let the scheduler poll soon.
        try:
            checked = await _check_source(
                x_user_id, str(existing.get("kind") or "github"), config
            )
        except Exception as exc:  # noqa: BLE001 - a failed check is a state
            state = {"last_error": str(exc)[:1000], "cursor": ""}
        else:
            state = {
                "agent_login": checked["agent_login"],
                "repository": checked["repository"],
                "last_error": "",
                "cursor": "",
                "last_poll_at": "",
                "next_poll_at": datetime.now(timezone.utc).isoformat(),
            }
    row = await source_db.update_source(
        x_user_id, str(source_id), config=config, enabled=body.enabled, state=state
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return _view(row)


@router.delete("/sources/{source_id}", status_code=204)
async def delete_source(source_id: UUID, x_user_id: CurrentUserId) -> Response:
    """Delete a Source. Its Triggers and Thread rows cascade; Tickets keep
    their mandate with the Source id set null."""
    await _require_feature(x_user_id)
    if not await source_db.delete_source(x_user_id, str(source_id)):
        raise HTTPException(status_code=404, detail="Source not found")
    return Response(status_code=204)


@router.post("/sources/{source_id}/poll", response_model=SourceView)
async def poll_source_now(source_id: UUID, x_user_id: CurrentUserId) -> SourceView:
    await _require_feature(x_user_id)
    from src.server.services.autonomy.poll import poll_source_now as queue_poll

    row = await source_db.get_source(x_user_id, str(source_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if not row.get("enabled"):
        raise HTTPException(status_code=409, detail="Source is disabled")
    if not await queue_poll(x_user_id, str(source_id)):
        raise HTTPException(status_code=409, detail="Source could not be polled")
    return SourceView.model_validate(row)


@router.post("/sources/{source_id}/check", response_model=SourceCheckView)
async def check_source(source_id: UUID, x_user_id: CurrentUserId) -> SourceCheckView:
    await _require_feature(x_user_id)
    row = await source_db.get_source(x_user_id, str(source_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Source not found")
    try:
        checked = await _check_source(
            x_user_id, str(row.get("kind") or "github"), dict(row.get("config") or {})
        )
    except Exception as exc:  # noqa: BLE001 - a failed check is a result
        await source_db.update_source(
            x_user_id, str(source_id), state={"last_error": str(exc)[:1000]}
        )
        return SourceCheckView(ok=False, error=str(exc)[:1000])
    await source_db.update_source(
        x_user_id,
        str(source_id),
        state={
            "agent_login": checked["agent_login"],
            "repository": checked["repository"],
            "last_error": "",
        },
    )
    return SourceCheckView(ok=True, **checked)


# ---------------------------------------------------------------------------
# Reply approval queue
# ---------------------------------------------------------------------------


@router.get("/outbox", response_model=OutboxList)
async def list_outbox(
    x_user_id: CurrentUserId, status: str = "pending_approval", limit: int = 50
) -> OutboxList:
    """List this user's replies, newest first.

    The default list is the approval queue: the replies a channel held until
    a person reads them.
    """
    await _require_feature(x_user_id)
    rows = await outbox_db.list_outbox(x_user_id, status=status, limit=limit)
    return OutboxList(replies=[OutboxView.model_validate(row) for row in rows])


async def _held_reply(outbox_id: str, user_id: str) -> dict[str, Any]:
    row = await outbox_db.get_outbox(outbox_id)
    if row is None or str(row.get("user_id") or "") != user_id:
        raise HTTPException(status_code=404, detail="Reply not found")
    if str(row.get("status") or "") != "pending_approval":
        raise HTTPException(status_code=409, detail="Reply is not waiting for approval")
    return row


@router.post("/outbox/{outbox_id}/approve", response_model=OutboxView)
async def approve_reply(outbox_id: UUID, x_user_id: CurrentUserId) -> OutboxView:
    """Approve one held reply and release it for delivery."""
    await _require_feature(x_user_id)
    await _held_reply(str(outbox_id), x_user_id)
    if not await outbox_db.mark_approved(str(outbox_id), x_user_id):
        raise HTTPException(status_code=409, detail="Reply is not waiting for approval")
    from src.server.database.autonomy.effects import enqueue_effect
    from src.server.services.autonomy.models import EFFECT_REPLY

    # A distinct key: the reply may already have an effect row from the run
    # that held it, and that row has been consumed.
    await enqueue_effect(
        user_id=x_user_id,
        kind=EFFECT_REPLY,
        idempotency_key=f"autonomy:reply-effect:{outbox_id}:approved",
        payload={"outbox_id": str(outbox_id)},
    )
    row = await outbox_db.get_outbox(str(outbox_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Reply not found")
    return OutboxView.model_validate(row)


@router.post("/outbox/{outbox_id}/reject", response_model=OutboxView)
async def reject_reply(outbox_id: UUID, x_user_id: CurrentUserId) -> OutboxView:
    """Settle one held reply that its reader does not want to send."""
    await _require_feature(x_user_id)
    await _held_reply(str(outbox_id), x_user_id)
    if not await outbox_db.mark_rejected(str(outbox_id), x_user_id):
        raise HTTPException(status_code=409, detail="Reply is not waiting for approval")
    row = await outbox_db.get_outbox(str(outbox_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Reply not found")
    return OutboxView.model_validate(row)


@router.get("/evals", response_model=Any)
async def list_evals(x_user_id: CurrentUserId, status: str = "unavailable") -> Any:
    await _require_feature(x_user_id)
    if status != "unavailable":
        raise HTTPException(status_code=400, detail="only unavailable is listed")
    from src.server.database.autonomy import evals as eval_db

    return {"evals": await eval_db.list_unavailable_for_user(x_user_id)}


@router.post("/evals/{eval_id}/requeue", response_model=Any)
async def requeue_eval(eval_id: str, x_user_id: CurrentUserId) -> Any:
    await _require_feature(x_user_id)
    from src.server.database.autonomy import evals as eval_db

    if not await eval_db.requeue_unavailable(eval_id, x_user_id):
        raise HTTPException(status_code=404, detail="Eval not found or not unavailable")
    return {"status": "queued"}


# ---------------------------------------------------------------------------
# BYOM local daemons
# ---------------------------------------------------------------------------


def _daemon_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _require_daemon(token: str | None) -> dict[str, Any]:
    if (
        not token
        or len(token) > 128
        or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token)
    ):
        raise HTTPException(status_code=401, detail="Invalid daemon token")
    from src.server.database.autonomy import daemons as daemon_db

    row = await daemon_db.daemon_for_token(_daemon_hash(token))
    if row is None:
        raise HTTPException(status_code=401, detail="Unknown daemon token")
    return row


@router.get("/daemons", response_model=Any)
async def list_daemons(x_user_id: CurrentUserId) -> Any:
    await _require_feature(x_user_id)
    from src.server.database.autonomy import daemons as daemon_db

    rows = await daemon_db.list_daemons(x_user_id)
    return {"daemons": [DaemonView.model_validate(row) for row in rows]}


@router.post("/daemons", response_model=Any, status_code=201)
async def create_daemon(body: DaemonCreate, x_user_id: CurrentUserId) -> Any:
    await _require_feature(x_user_id)
    from src.server.database.autonomy import daemons as daemon_db

    provider = str(body.provider or "").strip().lower()
    if provider not in _native_cli_providers():
        raise HTTPException(status_code=400, detail="Unsupported native CLI provider")
    token = secrets.token_urlsafe(32)
    row = await daemon_db.create_daemon(
        user_id=x_user_id,
        name=body.name,
        token_hash=_daemon_hash(token),
        provider=provider,
        max_concurrency=body.max_concurrency,
    )
    return {**DaemonView.model_validate(row).model_dump(), "token": token}


@router.patch("/daemons/{daemon_id}", response_model=DaemonView)
async def update_daemon(
    daemon_id: UUID, body: DaemonConcurrencyUpdate, x_user_id: CurrentUserId
) -> DaemonView:
    await _require_feature(x_user_id)
    from src.server.database.autonomy import daemons as daemon_db

    row = await daemon_db.update_daemon_concurrency(
        x_user_id, str(daemon_id), body.max_concurrency
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Daemon not found")
    return DaemonView.model_validate(row)


@router.delete("/daemons/{daemon_id}", status_code=204)
async def delete_daemon(daemon_id: UUID, x_user_id: CurrentUserId) -> Response:
    await _require_feature(x_user_id)
    from src.server.database.autonomy import daemons as daemon_db

    result = await daemon_db.delete_daemon(x_user_id, str(daemon_id))
    if result == "not_found":
        raise HTTPException(status_code=404, detail="Daemon not found")
    if result == "in_use":
        raise HTTPException(
            status_code=409,
            detail="Unassign this daemon from Sources and schedules, then finish or cancel its Tickets first",
        )
    return Response(status_code=204)


@router.post("/daemon/heartbeat", response_model=Any)
async def daemon_heartbeat(
    body: DaemonHeartbeatRequest | None = None,
    x_daemon_token: str | None = Header(None, alias="X-Daemon-Token"),
) -> Any:
    daemon = await _require_daemon(x_daemon_token)
    from src.server.database.autonomy import daemons as daemon_db

    leases = [ref.model_dump(mode="json") for ref in (body.leases if body else [])]
    renewed = await daemon_db.heartbeat(str(daemon["daemon_id"]), leases)
    return {"status": "ok", "leases": renewed}


@router.post("/daemon/claim", response_model=Any)
async def daemon_claim(
    body: DaemonClaimRequest | None = None,
    x_daemon_token: str | None = Header(None, alias="X-Daemon-Token"),
) -> Any:
    daemon = await _require_daemon(x_daemon_token)
    from src.server.database.autonomy import sources as source_db
    from src.server.services.autonomy.worker import (
        DaemonClaimBusy,
        claim_daemon_ticket,
        deliver_staged_handoff,
        worker_issue_context,
        worker_memory_context,
        worker_prompt,
    )

    try:
        ticket = await claim_daemon_ticket(
            str(daemon["daemon_id"]),
            str(body.claim_id) if body and body.claim_id else None,
            policy_version=str(body.policy_version) if body else "",
        )
    except DaemonClaimBusy as exc:
        raise HTTPException(
            status_code=503, detail=str(exc), headers={"Retry-After": "1"}
        ) from exc
    if ticket is None:
        return {"ticket": None}
    # The daemon builds the checkout on its own host from this spec. The base
    # is resolved and pinned server-side before the spec is returned, so a
    # repeated claim can never see a moved base; failing to resolve it fails
    # closed instead of handing out a Ticket without its durable base.
    if _claim_spec_may_run_git(ticket):
        settled = await _serialized_claim_worktree_spec(ticket, daemon)
        if settled is None:
            return {"ticket": None}
        ticket, worktree = settled
    else:
        worktree = await _claim_worktree_spec(ticket, daemon)
    worktree_path = _daemon_worktree_path(ticket, worktree, str(daemon["daemon_id"]))
    if worktree is not None:
        worktree = {**worktree, "worktree_path": worktree_path}
    source = await source_db.get_source_any_user(
        str((ticket.get("mandate") or {}).get("source_id") or "")
    )
    speculative = bool((ticket.get("mandate") or {}).get("speculative"))
    try:
        from src.server.services.autonomy import shared_context as shared_ctx

        shared = await shared_ctx.worker_shared_context_text(str(ticket["ticket_id"]))
    except Exception:
        logger.exception(
            "[autonomy] shared context assembly failed ticket=%s",
            ticket.get("ticket_id"),
        )
        shared = ""
    memory_context = await worker_memory_context(ticket)
    issue_context = await worker_issue_context(ticket)
    # Only a daemon that announces the JSON completion contract gets its
    # closing instructions; an older one keeps the marker lines it parses.
    completion_contract = bool(body is not None and body.completion_contract >= 1)
    prompt = worker_prompt(
        ticket,
        source or {},
        native=True,
        speculative=speculative,
        shared_context=shared,
        memory_context=memory_context,
        issue_context=issue_context,
        completion_contract=completion_contract,
    )
    delivered = await deliver_staged_handoff(ticket, prompt)
    if delivered is None:
        # Claimed rows are daemon-bound, so the cloud refusal inside
        # deliver_staged_handoff does not run. None here means the invariant
        # broke; a 409 with the cloud-worker text would describe a different
        # failure than the one that happened.
        logger.error(
            "[autonomy] daemon claim produced no prompt ticket=%s daemon=%s",
            ticket.get("ticket_id"),
            ticket.get("worker_daemon_id"),
        )
        raise AssertionError(
            "daemon claim has no prompt; a claimed ticket is daemon-bound"
        )
    prompt = delivered
    inputs = ticket.get("inputs") or []
    resume = bool(inputs)
    if resume:
        prompt = append_followups(
            prompt, inputs, completion_contract=completion_contract
        )
    from src.conductor_cli.daemon_grant import GrantError
    from src.server.services.autonomy.daemon_grants import attach_lease_grant

    try:
        grant = await attach_lease_grant(
            daemon_id=str(daemon["daemon_id"]),
            ticket_id=str(ticket["ticket_id"]),
            lease_id=str(ticket["lease_id"]),
            policy_version=str(ticket.get("policy_version") or ""),
            expires_at=ticket["lease_expires_at"],
        )
    except GrantError as exc:
        logger.exception(
            "[autonomy] daemon lease grant was not signed lease=%s",
            ticket.get("lease_id"),
        )
        raise HTTPException(
            status_code=503, detail="Lease signing key is unusable"
        ) from exc
    claimed = {
        "ticket": {
            "ticket_id": str(ticket["ticket_id"]),
            "title": ticket.get("title"),
            # The Ticket's own request, so the device can check it against its
            # own policy without parsing the launch prompt.
            "scope": ticket.get("scope"),
            "workspace_id": ticket.get("workspace_id"),
            "worker_provider": ticket.get("worker_provider"),
            "worker_model": ticket.get("worker_model"),
            # The legacy mandate cwd is only for Tickets without a worktree
            # spec; with a spec the daemon runs in its own host checkout.
            "cwd": (ticket.get("mandate") or {}).get("cwd") if worktree is None else None,
            "worktree_path": worktree_path,
            "worktree": worktree,
            "speculative": speculative,
            # Echoed only when the prompt asks for the JSON completion report,
            # so the daemon parses what was asked for.
            "completion_contract": 1 if completion_contract else 0,
            # The claim count (this claim included): a daemon whose baseline
            # is in memory only skips the commit check on a later attempt.
            "attempts": int(ticket.get("attempts") or 0),
            "lease_id": str(ticket["lease_id"]),
            "lease_expires_at": ticket["lease_expires_at"].isoformat(),
            "lease_duration_seconds": int(ticket["lease_duration_seconds"]),
            "policy_version": str(ticket.get("policy_version") or ""),
            "daemon_id": str(daemon["daemon_id"]),
        },
        "prompt": prompt,
        "resume": resume,
    }
    if grant is not None:
        claimed["grant"] = grant
    return claimed


# How long a claim waits for another request of the same Ticket to finish its
# worktree spec. The daemon's HTTP client gives up after 30 s and retries the
# same claim id; answering 503 well before that keeps the retry orderly.
_CLAIM_WORKTREE_LOCK_SECONDS = 15.0


def _claim_spec_may_run_git(ticket: dict[str, Any]) -> bool:
    """Whether building this claim's worktree spec may run git on the server.

    Only that step (resolving and pinning the base) is slow, bounded by the
    server's git timeout, and can fail for a reason a retry of the same claim
    does not share, so only such claims are serialised per Ticket. It mirrors
    ``daemon_claim_worktree_spec``: a read-only or speculative mandate has no
    spec, and the base comes from ``CONDUCTOR_DISPATCH_REPO`` or a
    server-visible git mandate cwd. An already pinned base does not skip the
    lock: the request that pinned it may still be settling a failure.
    """
    from src.server.services.autonomy import ticket_worktree as worktree_service

    mandate = ticket.get("mandate")
    mandate = mandate if isinstance(mandate, dict) else {}
    if mandate.get("speculative") or mandate.get("read_only"):
        return False
    if os.environ.get("CONDUCTOR_DISPATCH_REPO", "").strip():
        return True
    return worktree_service.configured_cwd_is_git_repo(mandate.get("cwd"))


async def _claim_worktree_spec(
    ticket: dict[str, Any], daemon: dict[str, Any]
) -> dict[str, Any] | None:
    """The claim's worktree spec; a failure is settled and raised as a 503."""
    from src.server.services.autonomy import ticket_worktree as worktree_service

    try:
        return await worktree_service.daemon_claim_worktree_spec(ticket)
    except Exception as exc:
        raise await _unlaunchable_daemon_claim(ticket, daemon, exc) from exc


def _claim_busy(detail: str) -> HTTPException:
    return HTTPException(status_code=503, detail=detail, headers={"Retry-After": "1"})


async def _serialized_claim_worktree_spec(
    ticket: dict[str, Any], daemon: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    """Build a claim's spec under its Ticket's worktree lock; None if given back.

    A daemon whose request outlives its client timeout retries the same claim
    id, and the replay returns the same live lease while the first request is
    still resolving the base. Unserialised, the replay is delivered and the
    first request then fails and releases the lease the daemon now runs on.
    Under the lock a request first checks that its claim is still live: one
    that finds it released (an earlier request failed and gave it back)
    answers no Ticket. A spec failure is settled (released or failed) inside
    the lock, so it always precedes any response for that claim; a later
    request reads the base the earlier one pinned and does not run git again.
    A request that cannot get the lock in time answers 503 and keeps the
    claim, so the daemon's retry of the same claim id gets it.
    """
    from src.server.database.autonomy import daemons as daemon_db

    ticket_id = str(ticket["ticket_id"])
    daemon_id = str(daemon["daemon_id"])
    lease_id = str(ticket.get("lease_id") or "")
    try:
        async with tickets_db.ticket_worktree_lock(
            ticket_id, timeout_seconds=_CLAIM_WORKTREE_LOCK_SECONDS
        ):
            try:
                live = await daemon_db.live_lease_ticket(daemon_id, ticket_id, lease_id)
                fresh = await tickets_db.get_ticket(ticket_id) if live else None
            except Exception as exc:
                logger.exception(
                    "[autonomy] could not recheck claim ticket=%s lease=%s",
                    ticket_id,
                    lease_id,
                )
                raise _claim_busy("Claim could not be checked; retry this claim id") from exc
            if not live:
                logger.info(
                    "[autonomy] claim was given back before delivery ticket=%s lease=%s",
                    ticket_id,
                    lease_id,
                )
                return None
            if fresh is not None:
                ticket = {
                    **ticket,
                    **{k: fresh[k] for k in tickets_db.WORKTREE_COLUMNS if k in fresh},
                }
            return ticket, await _claim_worktree_spec(ticket, daemon)
    except HTTPException:
        raise
    except Exception as exc:
        # The lock was not acquired (busy past the wait, or unreachable). The
        # claim is untouched: the retry of the same claim id replays it.
        logger.warning(
            "[autonomy] claim worktree lock unavailable ticket=%s lease=%s: %s",
            ticket_id,
            lease_id,
            exc,
        )
        raise _claim_busy("Ticket worktree is busy; retry this claim id") from exc


# The Ticket failure text the Source and the daemon see. It is fixed: the
# exception text is git's output, which can carry server paths, remote URLs
# or credentials embedded in them.
_UNRESOLVABLE_BASE_SUMMARY = (
    "The Ticket's worktree base could not be resolved on the server. Check the "
    "Ticket's repository and base branch, then retry the Ticket."
)
# Back-off for a server-wide worktree outage (no repo, git missing or hung):
# the Ticket stays queued and every claim would hit the same failure.
_WORKTREE_UNAVAILABLE_RETRY_AFTER = "30"


async def _unlaunchable_daemon_claim(
    ticket: dict[str, Any], daemon: dict[str, Any], exc: Exception
) -> HTTPException:
    """Settle a claim whose worktree spec could not be built; the 503 to raise.

    The claim already committed a lease. Leaving it held keeps the Ticket
    running until the lease expires, then puts it back at the head of the
    queue. Only a per-Ticket failure (``TicketWorktreeBaseError``: this
    Ticket's base cannot be resolved) fails the Ticket. A server-wide outage
    (``TicketWorktreeUnavailable``) or any other error is not the Ticket's
    fault: the lease is released and the Ticket stays queued for a later claim.
    """
    from src.server.services.autonomy import ticket_worktree as worktree_service

    ticket_id = str(ticket.get("ticket_id") or "")
    daemon_id = str(daemon["daemon_id"])
    base_error = getattr(worktree_service, "TicketWorktreeBaseError", None)
    if isinstance(base_error, type) and isinstance(exc, base_error):
        logger.warning(
            "[autonomy] ticket worktree base unresolvable, failing claim ticket=%s: %s",
            ticket_id,
            exc,
        )
        if not await _fail_unlaunchable_daemon_claim(ticket, daemon_id):
            await _release_unlaunchable_daemon_claim(ticket, daemon)
        return HTTPException(
            status_code=503,
            detail="Ticket worktree base could not be resolved",
            headers={"Retry-After": "1"},
        )
    if isinstance(exc, worktree_service.TicketWorktreeError):
        logger.warning(
            "[autonomy] ticket worktree unavailable, releasing claim ticket=%s: %s",
            ticket_id,
            exc,
        )
    else:
        logger.error(
            "[autonomy] ticket worktree spec failed, releasing claim ticket=%s",
            ticket_id,
            exc_info=exc,
        )
    await _release_unlaunchable_daemon_claim(ticket, daemon)
    return HTTPException(
        status_code=503,
        detail="Ticket worktrees are unavailable on the server; retry later",
        headers={"Retry-After": _WORKTREE_UNAVAILABLE_RETRY_AFTER},
    )


async def _fail_unlaunchable_daemon_claim(ticket: dict[str, Any], daemon_id: str) -> bool:
    """Fail a claimed Ticket whose base cannot be resolved; True when settled.

    The failure goes through the normal daemon report commit, which settles
    the Ticket as failed and releases the lease in one write, fenced on this
    daemon's live lease (a lease that moved meanwhile is left alone).
    """
    from src.server.services.autonomy.host import apply_daemon_worker_report

    ticket_id = str(ticket.get("ticket_id") or "")
    lease_id = str(ticket.get("lease_id") or "")
    try:
        result = await apply_daemon_worker_report(
            daemon_id=daemon_id,
            ticket_id=ticket_id,
            lease_id=lease_id,
            state="failed",
            summary=_UNRESOLVABLE_BASE_SUMMARY,
        )
    except Exception:
        logger.exception(
            "[autonomy] could not fail unlaunchable claim ticket=%s lease=%s",
            ticket_id,
            lease_id,
        )
        return False
    if result is None or result.get("refused"):
        logger.error(
            "[autonomy] unlaunchable claim was not settled ticket=%s lease=%s: %s",
            ticket_id,
            lease_id,
            (result or {}).get("refused") or "lease is no longer live",
        )
        return False
    return True


async def _release_unlaunchable_daemon_claim(
    ticket: dict[str, Any], daemon: dict[str, Any]
) -> None:
    """Give back a claim that could not be launched, without settling the Ticket.

    ``daemons.release_daemon_claim`` (when present) requeues the Ticket at once,
    fenced on this daemon's live lease. Without it the lease is revoked: the
    daemon never received it, nothing can report on it, and
    ``recover_expired_daemon_leases`` requeues the Ticket when it runs out.
    Neither path writes a failure onto the Ticket.
    """
    from src.server.database.autonomy import daemons as daemon_db

    ticket_id = str(ticket.get("ticket_id") or "")
    daemon_id = str(daemon["daemon_id"])
    lease_id = str(ticket.get("lease_id") or "")
    if not lease_id:
        return
    release = getattr(daemon_db, "release_daemon_claim", None)
    try:
        if release is not None:
            # The lease heartbeat read with the claim: the release is refused
            # once the daemon has heartbeated, i.e. holds the claim.
            heartbeat_at = ticket.get("worker_lease_heartbeat_at")
            fence = {"heartbeat_at": heartbeat_at} if heartbeat_at is not None else {}
            released = await release(daemon_id, ticket_id, lease_id, **fence)
            if not released:
                logger.warning(
                    "[autonomy] unlaunchable claim already moved ticket=%s lease=%s",
                    ticket_id,
                    lease_id,
                )
            return
        await daemon_db.revoke_lease(str(daemon["user_id"]), lease_id)
    except Exception:
        logger.exception(
            "[autonomy] could not release unlaunchable claim ticket=%s lease=%s",
            ticket_id,
            lease_id,
        )


def _daemon_worktree_path(
    ticket: dict[str, Any], worktree: dict[str, Any] | None, daemon_id: str
) -> str:
    """The persisted checkout path this daemon may reuse, or ''.

    Only a daemon-host binding made by this same daemon is returned: a server
    absolute path, or another device's path, means nothing on this host.
    """
    path = str(
        (worktree or {}).get("worktree_path") or ticket.get("worktree_path") or ""
    )
    if not path:
        return ""
    host = str(ticket.get("worktree_host") or "")
    if host and host != tickets_db.WORKTREE_HOST_DAEMON:
        return ""
    bound_daemon = str(ticket.get("worktree_daemon_id") or "")
    if bound_daemon and bound_daemon != daemon_id:
        return ""
    return path


@router.get("/daemon/lease/{lease_id}/status", response_model=Any)
async def daemon_lease_status(
    lease_id: UUID,
    x_daemon_token: str | None = Header(None, alias="X-Daemon-Token"),
) -> Any:
    """Whether this lease may still run. The daemon fails closed on any other answer."""
    daemon = await _require_daemon(x_daemon_token)
    from src.server.database.autonomy import daemons as daemon_db

    status = await daemon_db.lease_status(str(daemon["daemon_id"]), str(lease_id))
    if status is None:
        raise HTTPException(status_code=404, detail="Lease not found")
    revoked_at = status.get("revoked_at")
    return {
        "lease_id": status["lease_id"],
        "daemon_id": status["daemon_id"],
        "policy_version": status["policy_version"],
        "revoked": bool(status["revoked"]),
        "revoked_at": revoked_at.isoformat() if revoked_at is not None else None,
    }


@router.post("/daemon/lease/{lease_id}/revoke", response_model=Any)
async def daemon_lease_revoke(lease_id: UUID, x_user_id: CurrentUserId) -> Any:
    """Withdraw a lease. The daemon's next status poll refuses it."""
    await _require_feature(x_user_id)
    from src.server.database.autonomy import daemons as daemon_db

    outcome = await daemon_db.revoke_lease(x_user_id, str(lease_id))
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="Lease not found")
    return {"lease_id": str(lease_id), "revoked": True}


@router.get("/daemon/tickets/{ticket_id}/status", response_model=Any)
async def daemon_ticket_status(
    ticket_id: UUID,
    x_daemon_token: str | None = Header(None, alias="X-Daemon-Token"),
) -> Any:
    daemon = await _require_daemon(x_daemon_token)
    from src.server.database.autonomy import daemons as daemon_db
    from src.server.database.autonomy import stops as stop_db

    ticket = await daemon_db.owned_ticket(str(daemon["daemon_id"]), str(ticket_id))
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not owned by this daemon")
    return {
        "ticket_id": str(ticket_id),
        "status": ticket["status"],
        "attention": ticket.get("attention"),
        "stop": await stop_db.get_stop(str(ticket_id)),
    }


@router.post("/daemon/tickets/{ticket_id}/worktree", response_model=Any)
async def daemon_bind_worktree(
    ticket_id: UUID,
    body: DaemonWorktreeBind,
    x_daemon_token: str | None = Header(None, alias="X-Daemon-Token"),
) -> Any:
    """Persist the checkout a daemon created on its own host for a live lease.

    Every fence (this daemon's live lease, open Ticket, matching base pin, no
    other live checkout) is evaluated inside the one bind write. There is no
    separate pre-check here: one could go stale between the check and the
    write and let an expired request overwrite a newer attempt's binding.
    """
    daemon = await _require_daemon(x_daemon_token)
    daemon_id = str(daemon["daemon_id"])
    fences: dict[str, Any] = {}
    replace_generation = _departed_daemon_generation(
        await tickets_db.get_ticket(str(ticket_id)), daemon_id
    )
    if replace_generation is not None:
        fences["replace_generation"] = replace_generation
    row = await tickets_db.bind_ticket_worktree(
        str(ticket_id),
        worktree_path=body.worktree_path,
        branch=body.branch,
        base_sha=body.base_sha,
        repo="",
        host=tickets_db.WORKTREE_HOST_DAEMON,
        daemon_id=daemon_id,
        lease_id=str(body.lease_id),
        **fences,
    )
    if row is None:
        raise HTTPException(
            status_code=409,
            detail="Lease is not live for this Ticket, or the worktree binding changed",
        )
    return {
        "worktree_path": str(row.get("worktree_path") or ""),
        "worktree_generation": int(row.get("worktree_generation") or 0),
    }


def _departed_daemon_generation(
    row: dict[str, Any] | None, daemon_id: str
) -> int | None:
    """The generation of a departed daemon's binding this daemon may replace.

    After a handover the row can still hold the previous daemon's checkout
    (its lease was gone, so the binding could not be cleared on that lease).
    That checkout lives on the other host and blocks nobody here: the bind
    replaces it, fenced on the generation read now, so a stale read can only
    make the bind refuse, never replace a newer binding. The old daemon's
    cleanup report is then accepted as a stale note.
    """
    if not row:
        return None
    bound_daemon = str(row.get("worktree_daemon_id") or "")
    if (
        str(row.get("worktree_host") or "") != tickets_db.WORKTREE_HOST_DAEMON
        or not str(row.get("worktree_path") or "")
        or str(row.get("worker_daemon_id") or "") != daemon_id
        or not bound_daemon
        or bound_daemon == daemon_id
    ):
        return None
    return int(row.get("worktree_generation") or 0)


async def _daemon_runner_still_live(
    row: dict[str, Any], daemon_id: str, lease_id: str
) -> str:
    """Why this daemon's runner may still own the checkout, or ''.

    Cleanup is only accepted once the Ticket has ended on the reporting lease:
    an ending status, and for a cancellation a verified stop for that lease
    (cancellation is recorded before the daemon has stopped the process).
    """
    from src.server.database.autonomy import daemons as daemon_db
    from src.server.database.autonomy import stops as stop_db

    status = str(row.get("status") or "")
    if status in tickets_db.OPEN_STATUSES:
        return "Ticket is still open"
    # Any live lease this daemon holds on the Ticket may still have a runner
    # in the checkout, whichever lease the report names. Omitting lease_id
    # must never be a weaker report than sending it.
    current = await daemon_db.owned_ticket(daemon_id, str(row["ticket_id"]))
    expires = (current or {}).get("worker_lease_expires_at")
    live_lease = str((current or {}).get("worker_lease_id") or "")
    if (
        current is not None
        and live_lease
        and expires is not None
        and expires > datetime.now(timezone.utc)
    ):
        stop = await stop_db.get_stop(str(row["ticket_id"]))
        if not (
            stop
            and stop.get("verified")
            and str(stop.get("lease_id") or "") == live_lease
        ):
            return "The lease is still running"
    if status == "cancelled":
        stop = await stop_db.get_stop(str(row["ticket_id"]))
        if stop is None or not stop.get("verified"):
            return "The stop has not been verified"
        stop_lease = str(stop.get("lease_id") or "")
        if lease_id and stop_lease and stop_lease != lease_id:
            return "The verified stop belongs to another lease"
    return ""


@router.post("/daemon/tickets/{ticket_id}/worktree/cleanup", response_model=Any)
async def daemon_worktree_cleanup(
    ticket_id: UUID,
    body: DaemonWorktreeCleanup,
    x_daemon_token: str | None = Header(None, alias="X-Daemon-Token"),
) -> Any:
    """Record the outcome of a daemon removing its own host checkout.

    The write is fenced on the reported path and generation (and lease when
    given), so a late or replayed report for an older attempt, including one
    sent after replaying an old stop acknowledgement, can neither clear nor
    mark the binding of a newer attempt. The server never removes a daemon
    checkout itself.
    """
    daemon = await _require_daemon(x_daemon_token)
    daemon_id = str(daemon["daemon_id"])
    row = await tickets_db.get_ticket(str(ticket_id))
    if row is None or str(row.get("user_id")) != str(daemon["user_id"]):
        raise HTTPException(status_code=404, detail="Ticket not found")
    bound_here = (
        str(row.get("worktree_host") or "") == tickets_db.WORKTREE_HOST_DAEMON
        and str(row.get("worktree_daemon_id") or "") == daemon_id
    )
    current = (
        bound_here
        and str(row.get("worktree_path") or "") == body.worktree_path
        and int(row.get("worktree_generation") or 0) == body.worktree_generation
    )
    if not current and await _resolve_stale_daemon_cleanup(
        str(ticket_id), daemon_id, body
    ):
        # A newer owner replaced this daemon's binding; the report settles
        # the note kept for the old checkout and never touches the binding.
        return {"ok": True, "pending": body.outcome == "failed", "stale": True}
    if not bound_here:
        raise HTTPException(
            status_code=409, detail="The worktree is not bound to this daemon"
        )
    lease_id = str(body.lease_id) if body.lease_id else ""
    refused = await _daemon_runner_still_live(row, daemon_id, lease_id)
    if refused:
        raise HTTPException(status_code=409, detail=refused)
    if body.outcome == "removed":
        ok = await tickets_db.clear_ticket_worktree_binding(
            str(ticket_id),
            worktree_path=body.worktree_path,
            worktree_generation=body.worktree_generation,
            lease_id=lease_id or None,
            host=tickets_db.WORKTREE_HOST_DAEMON,
            daemon_id=daemon_id,
        )
        pending = False
    else:
        logger.warning(
            "[autonomy] daemon worktree removal failed ticket=%s generation=%s: %s",
            ticket_id,
            body.worktree_generation,
            body.detail[:500],
        )
        ok = await tickets_db.record_worktree_cleanup_pending(
            str(ticket_id),
            worktree_path=body.worktree_path,
            worktree_generation=body.worktree_generation,
            lease_id=lease_id or None,
            host=tickets_db.WORKTREE_HOST_DAEMON,
            daemon_id=daemon_id,
        )
        pending = True
    if not ok:
        raise HTTPException(
            status_code=409,
            detail="Cleanup report does not match the current worktree binding",
        )
    return {"ok": True, "pending": pending}


async def _resolve_stale_daemon_cleanup(
    ticket_id: str, daemon_id: str, body: DaemonWorktreeCleanup
) -> bool:
    """Settle a replaced binding's cleanup note for this daemon; True if one matched.

    The note names the daemon, path and generation of the binding a newer
    owner replaced, so a report for any other checkout, or by another
    daemon, does not match and keeps its 409.
    """
    resolve = getattr(tickets_db, "resolve_stale_worktree_cleanup", None)
    if resolve is None:
        return False
    if body.outcome == "failed":
        logger.warning(
            "[autonomy] daemon removal of replaced worktree failed ticket=%s generation=%s: %s",
            ticket_id,
            body.worktree_generation,
            body.detail[:500],
        )
    return bool(
        await resolve(
            ticket_id,
            daemon_id=daemon_id,
            worktree_path=body.worktree_path,
            worktree_generation=body.worktree_generation,
            outcome=body.outcome,
        )
    )


@router.post("/daemon/tickets/{ticket_id}/stop/ack", response_model=Any)
async def daemon_stop_ack(
    ticket_id: UUID,
    body: DaemonStopAck,
    x_daemon_token: str | None = Header(None, alias="X-Daemon-Token"),
) -> Any:
    daemon = await _require_daemon(x_daemon_token)
    from src.server.services.autonomy import stop as stop_service

    evidence = {
        "process_gone": body.process_gone,
        "signal": body.signal,
        "pid": body.pid,
        "pgid": body.pgid,
        "checked_at": body.checked_at.isoformat() if body.checked_at else None,
        "processes": body.processes,
    }
    result = await stop_service.acknowledge_ticket_stop(
        daemon_id=str(daemon["daemon_id"]),
        ticket_id=str(ticket_id),
        lease_id=str(body.lease_id),
        process_gone=body.process_gone,
        evidence=evidence,
    )
    if result["outcome"] == "not_found":
        raise HTTPException(status_code=404, detail="No stop request for this Ticket")
    if result["outcome"] == "not_owner":
        raise HTTPException(status_code=404, detail="Ticket not owned by this daemon")
    if result["outcome"] == "stale_lease":
        raise HTTPException(
            status_code=409, detail="Lease does not own this stop request"
        )
    if result["outcome"] == "state_not_persisted":
        raise HTTPException(
            status_code=409, detail="Ticket cancellation is not persisted yet"
        )
    if result["outcome"] == "heartbeat_active":
        raise HTTPException(
            status_code=409, detail="A lease heartbeat arrived after the stop request"
        )
    return {"stop": result["stop"]}


@router.post("/daemon/tickets/{ticket_id}/progress", response_model=Any)
async def daemon_progress(
    ticket_id: UUID,
    body: DaemonProgress,
    x_daemon_token: str | None = Header(None, alias="X-Daemon-Token"),
) -> Any:
    daemon = await _require_daemon(x_daemon_token)
    from src.server.database.autonomy import daemons as daemon_db

    ok = await daemon_db.record_progress(
        str(daemon["daemon_id"]), str(ticket_id), str(body.lease_id), body.summary
    )
    if not ok:
        raise HTTPException(
            status_code=409, detail="Lease is stale or Ticket is no longer running"
        )
    return {"report_id": ok, "status": "recorded"}


@router.post("/daemon/tickets/{ticket_id}/output", response_model=Any)
async def daemon_output(
    ticket_id: UUID,
    body: DaemonOutputBatch,
    x_daemon_token: str | None = Header(None, alias="X-Daemon-Token"),
) -> Any:
    """Accept one bounded, fenced batch of live Worker output (best effort).

    The daemon posts model text/tool names while the vendor CLI runs. The
    server owns the stream epoch and the scan state (see
    ``services.autonomy.daemon_output``): a batch is applied in one transaction
    (the events it keeps and the guard that describes them commit together, or
    nothing does; an event the scanner refuses is never stored, and the events
    that were safe before it are), and an unavailable store answers
    ``stored: false`` instead of failing the Worker. A daemon may keep streaming
    only on ``stored: true, tainted: false`` with the epoch it holds, and must
    stop on anything else (a missing or non-boolean field included) and on any
    409.
    """
    daemon = await _require_daemon(x_daemon_token)
    from src.server.services.autonomy import daemon_output as output_store

    events = output_store.normalize_output_events(body.events)
    if not events:
        return {"stored": False, "events": 0, "epoch": body.epoch or 0}
    result = await output_store.append_output(
        str(daemon["daemon_id"]),
        str(ticket_id),
        str(body.lease_id),
        body.run_id,
        body.epoch,
        body.batch_seq,
        events,
    )
    if result.code == output_store.CODE_STALE_LEASE:
        raise HTTPException(
            status_code=409, detail="Lease is stale or Ticket is no longer running"
        )
    if result.code in {
        output_store.CODE_STALE_EPOCH,
        output_store.CODE_DUPLICATE,
        output_store.CODE_BAD_BATCH,
    }:
        raise HTTPException(
            status_code=409,
            detail={"code": result.code, "epoch": result.epoch},
        )
    return {
        "stored": result.stored,
        "events": result.events or len(events),
        "epoch": result.epoch,
        "head": result.head,
        "tainted": result.tainted,
        **({"reason": result.reason} if result.reason else {}),
    }


@router.post("/daemon/tickets/{ticket_id}/report", response_model=Any)
async def daemon_report(
    ticket_id: UUID,
    body: DaemonReport,
    x_daemon_token: str | None = Header(None, alias="X-Daemon-Token"),
) -> Any:
    daemon = await _require_daemon(x_daemon_token)
    from src.server.database.autonomy import daemons as daemon_db
    from src.server.services.autonomy.daemon_grants import report_divergence
    from src.server.services.autonomy.host import apply_daemon_worker_report

    binding = await daemon_db.lease_binding(str(daemon["daemon_id"]), str(body.lease_id))
    divergence = report_divergence(
        binding,
        daemon_id=body.daemon_id,
        policy_version=body.policy_version,
        state=body.state,
    )
    if divergence:
        raise HTTPException(status_code=409, detail=divergence)
    result = await apply_daemon_worker_report(
        daemon_id=str(daemon["daemon_id"]),
        ticket_id=str(ticket_id),
        lease_id=str(body.lease_id),
        state=body.state,
        summary=body.summary,
        body=body.body,
        question=body.question or None,
        attention_reason=body.attention_reason,
        reported_daemon_id=str(body.daemon_id or ""),
        reported_policy_version=str(body.policy_version or ""),
        capability_audit=body.capability_audit.model_dump()
        if body.capability_audit is not None
        else None,
    )
    if result is not None and result.get("refused"):
        # The claim transaction rechecked the signed binding and revocation:
        # the owner can revoke between this request's precheck and the commit.
        raise HTTPException(status_code=409, detail=str(result["refused"]))
    if result is None:
        raise HTTPException(
            status_code=409, detail="Lease is stale or Ticket is no longer running"
        )
    return result


async def _daemon_ticket(ticket_id: UUID, daemon: dict[str, Any]) -> dict[str, Any]:
    """A Ticket this daemon may speak about, owned by the daemon's user."""
    from src.server.database.autonomy import daemons as daemon_db

    row = await daemon_db.owned_ticket(str(daemon["daemon_id"]), str(ticket_id))
    if row is None:
        row = await tickets_db.get_ticket(str(ticket_id))
        if row is None or row["user_id"] != daemon["user_id"]:
            raise HTTPException(status_code=404, detail="Ticket not found")
    return row


def _handoff_workspace_root(body: dict[str, Any]) -> Path:
    """Resolve the workspace root a handoff is confined to.

    The same boundary as a native CLI cwd: the user's home, ``/tmp``, or the
    deployment's ``NATIVE_CLI_WORKSPACE_ROOT``. A handoff must never name files
    outside a root the deployment already trusts.
    """
    from src.server.services.native_runtime.cwd import resolve_native_cwd

    try:
        resolved = resolve_native_cwd(str(body.get("workspace_root") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not resolved:
        raise HTTPException(
            status_code=400, detail="workspace_root is required for a handoff"
        )
    return Path(resolved)


@router.get("/daemon/tickets/{ticket_id}/handoffs", response_model=Any)
async def daemon_list_handoffs(
    ticket_id: UUID,
    x_daemon_token: str | None = Header(None, alias="X-Daemon-Token"),
) -> Any:
    daemon = await _require_daemon(x_daemon_token)
    await _daemon_ticket(ticket_id, daemon)
    rows = await handoff_db.list_handoffs(str(ticket_id))
    return HandoffList.model_validate({"handoffs": rows})


@router.post(
    "/daemon/tickets/{ticket_id}/handoffs", response_model=Any, status_code=201
)
async def daemon_register_handoff(
    ticket_id: UUID,
    body: HandoffRegisterRequest,
    x_daemon_token: str | None = Header(None, alias="X-Daemon-Token"),
) -> Any:
    """Register a handoff from the workspace the daemon actually sees.

    The manifest (size + sha256) is computed here from the real files; the
    request only names workspace-relative paths, so a caller cannot plant a
    checksum it did not produce.
    """
    daemon = await _require_daemon(x_daemon_token)
    ticket = await _daemon_ticket(ticket_id, daemon)
    root = _handoff_workspace_root(body.model_dump())
    from src.server.services.autonomy import handoff as handoff_service

    try:
        row = await handoff_service.register_handoff(
            root,
            ticket_id=str(ticket["ticket_id"]),
            user_id=str(ticket["user_id"]),
            producer=body.producer,
            consumer=body.consumer,
            purpose=body.purpose,
            paths=body.paths,
        )
    except handoff_service.HandoffError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return HandoffView.model_validate(row)


@router.post(
    "/daemon/tickets/{ticket_id}/handoffs/{handoff_id}/verify", response_model=Any
)
async def daemon_verify_handoff(
    ticket_id: UUID,
    handoff_id: UUID,
    body: HandoffVerifyRequest | None = None,
    x_daemon_token: str | None = Header(None, alias="X-Daemon-Token"),
) -> Any:
    """Re-hash every manifest file and settle the handoff.

    The consumer calls this before using the files: any missing or tampered
    file settles the row as ``rejected`` with the first reason, and a settled
    row never flips.
    """
    daemon = await _require_daemon(x_daemon_token)
    ticket = await _daemon_ticket(ticket_id, daemon)
    row = await handoff_db.get_handoff(str(handoff_id))
    if row is None or row["ticket_id"] != str(ticket["ticket_id"]):
        raise HTTPException(status_code=404, detail="Handoff not found")
    root = _handoff_workspace_root(body.model_dump() if body is not None else {})
    from src.server.services.autonomy import handoff as handoff_service

    try:
        settled = await handoff_service.verify_handoff(root, row)
    except handoff_service.HandoffError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return HandoffVerifyResponse(
        handoff=HandoffView.model_validate(settled),
        outcome=str(settled.get("status") or ""),
        error=str(settled.get("verify_error") or ""),
    )


# ---------------------------------------------------------------------------
# Tickets / Inbox
# ---------------------------------------------------------------------------


def _ticket_view(row: dict[str, Any], stop: dict[str, Any] | None = None) -> Any:
    from src.server.models.autonomy import TicketView

    payload = dict(row)
    if stop is not None:
        payload["stop"] = stop
    return TicketView.model_validate(payload)


async def _owned_ticket(ticket_id: UUID, user_id: str) -> dict[str, Any]:
    row = await tickets_db.get_ticket(str(ticket_id))
    if row is None or row["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return row


@router.get("/tickets", response_model=Any)
async def list_tickets(
    x_user_id: CurrentUserId,
    status: str | None = None,
    source_id: str | None = None,
    schedule_id: str | None = None,
    failed: bool = False,
    attention: bool = False,
    q: str | None = None,
    sort: str = "updated",
    limit: int = 50,
    offset: int = 0,
) -> Any:
    await _require_feature(x_user_id)
    from src.server.database.autonomy import stops as stop_db
    from src.server.models.autonomy import TicketList

    statuses = ["failed", "blocked"] if failed else ([status] if status else None)
    rows = await tickets_db.list_tickets(
        x_user_id,
        statuses=statuses,
        source_id=source_id,
        schedule_id=schedule_id,
        attention=attention,
        q=q,
        sort=sort,
        limit=max(1, min(limit, 200)),
        offset=max(0, offset),
    )
    stops = {
        stop["ticket_id"]: stop
        for stop in await stop_db.list_stops([str(row["ticket_id"]) for row in rows])
    }
    return TicketList(
        tickets=[_ticket_view(row, stops.get(str(row["ticket_id"]))) for row in rows]
    )


@router.get("/tickets/lines", response_model=Any)
async def list_ticket_lines(
    x_user_id: CurrentUserId, include_archived: bool = False
) -> Any:
    await _require_feature(x_user_id)
    return TicketLinesResponse.model_validate(
        await line_db.ticket_lines_overview(
            x_user_id, include_archived=include_archived
        )
    )


# A short id typed in a CLI: lower-case hex and dashes only, so it carries no
# LIKE wildcard. Registered before ``/tickets/{ticket_id}`` (typed UUID).
_TICKET_PREFIX_RE = re.compile(r"[0-9a-f-]{1,36}")
_TICKET_PREFIX_MATCH_LIMIT = 10


@router.get("/tickets/resolve", response_model=Any)
async def resolve_ticket_prefix(
    x_user_id: CurrentUserId,
    prefix: str = Query(..., min_length=1, max_length=36),
) -> Any:
    """Resolve a short Ticket id to the caller's Tickets that start with it.

    Always answers 200 with the candidates (newest first, at most ten, and
    ``more`` when there are others). ``ticket_id`` is set only for a unique
    match; the client turns no match or several into its own error.
    """
    await _require_feature(x_user_id)
    needle = prefix.strip().lower()
    if not _TICKET_PREFIX_RE.fullmatch(needle):
        raise HTTPException(
            status_code=400,
            detail="A Ticket id prefix is hex digits and dashes",
        )
    rows = await tickets_db.find_tickets_by_id_prefix(
        x_user_id, needle, limit=_TICKET_PREFIX_MATCH_LIMIT + 1
    )
    matches = [
        {
            "ticket_id": str(row["ticket_id"]),
            "title": str(row.get("title") or ""),
            "status": str(row.get("status") or ""),
            "updated_at": row.get("updated_at"),
        }
        for row in rows[:_TICKET_PREFIX_MATCH_LIMIT]
    ]
    return {
        "prefix": needle,
        "ticket_id": matches[0]["ticket_id"] if len(rows) == 1 else None,
        "matches": matches,
        "more": len(rows) > _TICKET_PREFIX_MATCH_LIMIT,
    }


@router.post("/tickets/{ticket_id}/resume", response_model=Any)
async def resume_ticket_route(
    ticket_id: UUID, x_user_id: CurrentUserId, force: bool = False
) -> Any:
    await _require_feature(x_user_id)
    try:
        updated = await ownership.resume_ticket(
            user_id=x_user_id, ticket_id=str(ticket_id), force=force
        )
    except ownership.TicketOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return _ticket_view(updated)


@router.post("/tickets/{ticket_id}/reassign", response_model=Any)
async def reassign_ticket_route(
    ticket_id: UUID, body: TicketReassignRequest, x_user_id: CurrentUserId
) -> Any:
    await _require_feature(x_user_id)
    try:
        updated = await ownership.reassign_ticket(
            user_id=x_user_id,
            ticket_id=str(ticket_id),
            daemon_id=str(body.daemon_id) if body.daemon_id else None,
            force=body.force,
        )
    except ownership.TicketOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return _ticket_view(updated)


@router.post("/tickets/{ticket_id}/archive", response_model=Any)
async def archive_ticket_route(ticket_id: UUID, x_user_id: CurrentUserId) -> Any:
    await _require_feature(x_user_id)
    try:
        updated = await ownership.archive_ticket(
            user_id=x_user_id, ticket_id=str(ticket_id)
        )
    except ownership.TicketOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    await _release_server_worktree(updated, reason="archive")
    return _ticket_view(updated)


@router.put("/tickets/{ticket_id}/line", response_model=Any)
async def set_ticket_line_route(
    ticket_id: UUID, body: TicketLineSetRequest, x_user_id: CurrentUserId
) -> Any:
    await _require_feature(x_user_id)
    try:
        updated = await ownership.assign_ticket_line(
            user_id=x_user_id,
            ticket_id=str(ticket_id),
            line_id=str(body.line_id) if body.line_id else None,
        )
    except ownership.TicketOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return _ticket_view(updated)


@router.get("/tickets/{ticket_id}", response_model=Any)
async def get_ticket(ticket_id: UUID, x_user_id: CurrentUserId) -> Any:
    await _require_feature(x_user_id)
    from src.server.database.autonomy import stops as stop_db
    from src.server.models.autonomy import TicketDetail

    row = await tickets_db.get_ticket(str(ticket_id))
    if row is None or row["user_id"] != x_user_id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    detail = dict(row)
    detail["inputs"] = await tickets_db.list_inputs(str(ticket_id))
    detail["reports"] = await tickets_db.list_reports(str(ticket_id))
    detail["events"] = await line_db.list_ticket_events(str(ticket_id))
    detail["stop"] = await stop_db.get_stop(str(ticket_id))
    return TicketDetail.model_validate(detail)


@router.get("/tickets/{ticket_id}/progress", response_model=Any)
async def get_ticket_progress(ticket_id: UUID, x_user_id: CurrentUserId) -> Any:
    """Progress and the final result. Worker transcripts stay off this route."""
    await _require_feature(x_user_id)
    row = await tickets_db.get_ticket(str(ticket_id))
    if row is None or row["user_id"] != x_user_id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    reports = await tickets_db.list_reports(str(ticket_id))
    return build_ticket_progress(row, reports)


@router.get("/tickets/{ticket_id}/handoffs", response_model=HandoffList)
async def list_ticket_handoffs(
    ticket_id: UUID, x_user_id: CurrentUserId
) -> HandoffList:
    """Handoffs of one Ticket: manifests are metadata, never secret material."""
    await _require_feature(x_user_id)
    await _owned_ticket(ticket_id, x_user_id)
    rows = await handoff_db.list_handoffs(str(ticket_id))
    return HandoffList.model_validate({"handoffs": rows})


@router.post(
    "/tickets/{ticket_id}/handoffs", response_model=HandoffView, status_code=201
)
async def create_ticket_handoff(
    ticket_id: UUID, body: HandoffManifestRequest, x_user_id: CurrentUserId
) -> HandoffView:
    """Register a handoff with a caller-supplied manifest.

    The user-facing route has no filesystem access, so the checksums come with
    the request and are only shape-validated here. The daemon route recomputes
    them from the real workspace and is the path agents should use.
    """
    await _require_feature(x_user_id)
    await _owned_ticket(ticket_id, x_user_id)
    from src.server.services.autonomy import handoff as handoff_service

    try:
        files = handoff_service.validate_manifest(
            [entry.model_dump() for entry in body.files],
            purpose=body.purpose,
            producer=body.producer,
            consumer=body.consumer,
        )
    except handoff_service.HandoffError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = await handoff_db.create_handoff(
        ticket_id=str(ticket_id),
        user_id=x_user_id,
        producer=body.producer.strip(),
        consumer=body.consumer.strip(),
        purpose=body.purpose.strip(),
        files=files,
    )
    return HandoffView.model_validate(row)


class SwitchRequest(BaseModel):
    to_provider: str = ""
    to_model: str = ""
    reason: str = "manual"
    candidates: list[dict[str, str]] = []
    messages: list[dict[str, str]] = []
    diff: str = ""
    findings: list[str] = []


@router.post("/tickets/{ticket_id}/switch", response_model=Any)
async def switch_ticket(
    ticket_id: UUID, body: SwitchRequest, x_user_id: CurrentUserId
) -> Any:
    """Move a stuck line to another model; the worktree and thread stay, a handoff rides the next run."""
    from src.server.services.autonomy import line_switch as ls

    await _require_feature(x_user_id)
    row = await tickets_db.get_ticket(str(ticket_id))
    if row is None or row["user_id"] != x_user_id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if row["status"] in {"completed", "cancelled", "archived"}:
        raise HTTPException(status_code=409, detail="Ticket is finished")
    current = ls.ModelRef(
        row.get("worker_provider") or "", row.get("worker_model") or ""
    )
    # The reviewer comes from the mandate only; a caller must not name its own.
    reviewer_src = (row.get("mandate") or {}).get("reviewer")
    reviewer = (
        ls.ModelRef(reviewer_src.get("provider", ""), reviewer_src.get("model", ""))
        if reviewer_src
        else None
    )
    explicit = (
        ls.ModelRef(body.to_provider, body.to_model)
        if body.to_provider and body.to_model
        else None
    )
    handoff = ls.build_handoff(body.messages, body.diff, body.findings)
    candidates = [
        ls.ModelRef(c.get("provider", ""), c.get("model", "")) for c in body.candidates
    ]
    if row.get("worker_daemon_id"):
        # A daemon only claims Tickets of its own provider, and the cloud skips
        # daemon Tickets, so a cross-provider move would strand the line.
        if explicit is not None and explicit.provider != current.provider:
            raise HTTPException(
                status_code=409,
                detail="A daemon line can only switch within its provider",
            )
        candidates = [c for c in candidates if c.provider == current.provider]
    try:
        plan = ls.plan_switch(
            ls.LineState(current),
            candidates,
            handoff,
            reason=body.reason,
            reviewer=reviewer,
            explicit=explicit,
            attempt=len(row.get("switches") or []) + 1,
        )
    except ls.NoEligibleTarget as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    from src.server.services.autonomy.models import EFFECT_LAUNCH_WORKER
    from src.server.services.autonomy.worker import (
        is_unisolated_cloud_worker,
        refuse_unisolated_cloud_dispatch_retrying,
    )

    # A daemon switch still enqueues a launch effect; the effect handler
    # leaves daemon tickets for the claim path. A cloud worker that has no
    # verified isolated runtime must not get that effect — the refusal is
    # applied to the switched row. When the runtime verifies, the cloud switch
    # is a normal launch and the effect runs the Worker inside that boundary.
    from src.server.services.autonomy.cloud_runtime import (
        resolve_cloud_worker_isolation_async,
    )

    cloud = is_unisolated_cloud_worker(row) and not (
        await resolve_cloud_worker_isolation_async()
    ).launch_allowed
    updated = await tickets_db.record_switch(
        str(ticket_id),
        expected_status=row["status"],
        expected_model=(current.provider, current.model),
        record=plan.record,
        handoff_text=plan.handoff_text,
        launch_effect=None if cloud else (x_user_id, EFFECT_LAUNCH_WORKER),
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Ticket changed; retry")
    refusal_error: BaseException | None = None
    if cloud:
        try:
            outcome = await refuse_unisolated_cloud_dispatch_retrying(updated)
        except RuntimeError as exc:
            refusal_error = exc
        else:
            if outcome.ticket is not None and (
                outcome.failed or str(outcome.ticket.get("status") or "") == "failed"
            ):
                updated = outcome.ticket
    if row["status"] == "running" and row.get("worker_thread_id"):
        # Stopping the old run is best effort. The switch is already committed:
        # a daemon line has its replacement launch, and a cloud line is refused
        # rather than started on the shared runtime.
        from src.server.services.projects.dispatch import ProjectDispatcher

        try:
            await ProjectDispatcher().cancel_thread(
                user_id=x_user_id,
                thread_id=str(row["worker_thread_id"]),
                run_id=row.get("worker_run_id"),
            )
        except Exception:
            logger.warning("[autonomy] cancel of switched worker failed ticket=%s", ticket_id)
    if refusal_error is not None:
        # The switch is committed and the fence lost every read. Leave a
        # launch effect so the runner nacks and retries the refusal. The
        # effect does not start a shared cloud runtime.
        from src.server.database.autonomy.effects import enqueue_effect

        await enqueue_effect(
            user_id=x_user_id,
            kind=EFFECT_LAUNCH_WORKER,
            idempotency_key=(
                f"autonomy:cloud-refuse:{ticket_id}:{updated.get('revision')}"
            ),
            payload={"ticket_id": str(ticket_id)},
        )
        logger.error(
            "[autonomy] cloud switch refusal did not settle ticket=%s",
            ticket_id,
        )
        raise HTTPException(
            status_code=503,
            detail="Cloud worker refusal did not settle; retry",
        ) from refusal_error
    return _ticket_view(updated)


@router.post("/tickets/{ticket_id}/stop", response_model=Any)
async def stop_ticket(ticket_id: UUID, x_user_id: CurrentUserId) -> Any:
    await _require_feature(x_user_id)
    try:
        result = await ownership.stop_ticket(
            user_id=x_user_id, ticket_id=str(ticket_id)
        )
    except ownership.TicketOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    # The service only removes once the runner is confirmed ended; otherwise
    # the checkout stays and the cleanup is retried durably.
    await _release_server_worktree(result.get("ticket"), reason="stop")
    return _ticket_view(result["ticket"], result.get("stop"))


@router.post(
    "/tickets/{ticket_id}/stop/acknowledge-unverified", response_model=Any
)
async def acknowledge_unverified_stop(
    ticket_id: UUID, x_user_id: CurrentUserId
) -> Any:
    """Operator override when a dead daemon can never confirm the stop."""
    await _require_feature(x_user_id)
    try:
        result = await ownership.acknowledge_unverified_stop(
            user_id=x_user_id, ticket_id=str(ticket_id)
        )
    except ownership.TicketOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return _ticket_view(result.get("ticket") or {}, result.get("stop"))


@router.post("/tickets/{ticket_id}/retry", response_model=Any)
async def retry_ticket(ticket_id: UUID, x_user_id: CurrentUserId) -> Any:
    await _require_feature(x_user_id)
    row = await tickets_db.get_ticket(str(ticket_id))
    if row is None or row["user_id"] != x_user_id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if row["status"] not in {"failed", "blocked", "waiting"}:
        raise HTTPException(status_code=409, detail="Ticket is not retryable")
    revoked = await ownership.revoked_mandate_reason(row)
    if revoked:
        cancelled = await tickets_db.update_ticket(
            str(ticket_id),
            expected_status=row["status"],
            status="cancelled",
            attention={"question": f"Mandate revoked: {revoked}"},
            error=f"Mandate revoked: {revoked}",
            ended_at=datetime.now(timezone.utc),
        )
        await _release_server_worktree(cancelled, reason="mandate_revoked")
        raise HTTPException(status_code=409, detail=f"Mandate revoked: {revoked}")
    mandate = dict(row.get("mandate") or {})
    retry_fields: dict[str, Any] = {
        "status": "queued",
        "attention": None,
        "error": "",
    }
    if mandate.get("speculative"):
        # Approving the plan records who accepted the speculative outcome,
        # and keeps the Worker's evidence, before the executing Worker starts.
        from src.server.services.autonomy.host import record_speculative_decision

        mandate = record_speculative_decision(
            mandate,
            outcome="approved",
            evidence={"actor": "user", "attempts": row.get("attempts")},
        )
        mandate["speculative"] = False
        retry_fields["mandate"] = mandate
    from src.server.services.autonomy import launch_dedup

    # An operator retry is a deliberate relaunch: it bypasses launch de-dup.
    forced = launch_dedup.dedup_mode() != launch_dedup.MODE_OFF
    if forced:
        retry_fields["mandate"] = launch_dedup.with_force_next(mandate)
    updated = await tickets_db.update_ticket(
        str(ticket_id),
        expected_status=row["status"],
        **retry_fields,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Ticket changed; retry")
    from src.server.database.autonomy.effects import enqueue_effect
    from src.server.services.autonomy.models import EFFECT_LAUNCH_WORKER

    await enqueue_effect(
        user_id=x_user_id,
        kind=EFFECT_LAUNCH_WORKER,
        idempotency_key=f"autonomy:launch:{ticket_id}:{int(row.get('attempts') or 0) + 1}",
        payload={"ticket_id": str(ticket_id), **({"force": True} if forced else {})},
    )
    return _ticket_view(updated)


@router.post("/tickets/{ticket_id}/discard", response_model=Any)
async def discard_ticket(ticket_id: UUID, x_user_id: CurrentUserId) -> Any:
    """End a speculative plan quietly, keeping the Worker's evidence."""
    await _require_feature(x_user_id)
    row = await tickets_db.get_ticket(str(ticket_id))
    if row is None or row["user_id"] != x_user_id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    mandate = dict(row.get("mandate") or {})
    if not mandate.get("speculative"):
        raise HTTPException(status_code=409, detail="Ticket is not a speculative plan")
    if row["status"] in {"completed", "cancelled", "archived"}:
        raise HTTPException(status_code=409, detail="Ticket ended")
    if row["status"] not in {"waiting", "blocked", "failed"}:
        raise HTTPException(
            status_code=409, detail="Speculative run is still in progress"
        )
    from src.server.services.autonomy.host import record_speculative_decision

    mandate = record_speculative_decision(
        mandate,
        outcome="discarded",
        evidence={"actor": "user", "ticket_status": row["status"]},
    )
    # Completion and subscription end commit together. A plan that stays
    # subscribed is still selected for the next signal on its Thread.
    updated = await tickets_db.complete_discarded_plan(
        str(ticket_id),
        expected_status=row["status"],
        mandate=mandate,
        summary="",
        ended_at=datetime.now(timezone.utc),
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Ticket changed; retry")
    await _release_server_worktree(updated, reason="discard")
    # A discarded speculative run stays quiet: no relay, no Router wake.
    return _ticket_view(updated)


@router.post("/tickets/{ticket_id}/reply", response_model=Any)
async def reply_ticket(
    ticket_id: UUID, body: TicketReplyRequest, x_user_id: CurrentUserId
) -> Any:
    await _require_feature(x_user_id)
    row = await tickets_db.get_ticket(str(ticket_id))
    if row is None or row["user_id"] != x_user_id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if row["status"] in {"completed", "cancelled", "archived"} or row.get("closed_at"):
        raise HTTPException(status_code=409, detail="Ticket ended")
    if row.get("worker_daemon_id"):
        if row["status"] not in {"blocked", "waiting"}:
            raise HTTPException(status_code=409, detail="Daemon Ticket is not waiting")
        # Daemon-bound Tickets stay pull-only even if a legacy row has a
        # backend thread id. The daemon owns the execution lease.
        from src.server.database.autonomy.tickets import add_input

        input_row = await add_input(
            ticket_id=str(ticket_id), trigger_id=None, kind="answer", text=body.text
        )
        return {"input_id": input_row["input_id"], "status": "queued_for_daemon"}
    if not row.get("worker_thread_id"):
        # No worker session yet. The instruction belongs to the task. The
        # handoff lock re-reads before the insert, so a thread published
        # while this request waited is queued for delivery instead of being
        # missed by both the launch prompt and the delivery snapshot.
        from src.server.services.autonomy.worker import accept_threadless_instruction

        accepted = await accept_threadless_instruction(
            ticket_id=str(ticket_id), user_id=x_user_id, text=body.text
        )
        if accepted["status"] == "missing":
            raise HTTPException(status_code=404, detail="Ticket not found")
        if accepted["status"] == "ended":
            raise HTTPException(status_code=409, detail="Ticket ended")
        return accepted
    fresh = await tickets_db.get_ticket(str(ticket_id))
    if (
        fresh is None
        or fresh.get("closed_at")
        or fresh["status"] in {"completed", "cancelled", "archived"}
    ):
        # Close can stamp the task after the first read. Archive ends it
        # without a close stamp. Do not start an unisolated cloud turn, and
        # do not store a review input for a task that has already ended.
        raise HTTPException(status_code=409, detail="Ticket ended")
    from src.server.database.autonomy.tickets import add_input
    from src.server.services.autonomy.inputs import mark_cloud_input_for_review
    from src.server.services.autonomy.worker import (
        refuse_unisolated_cloud_dispatch_retrying,
    )

    input_row = await add_input(
        ticket_id=str(ticket_id), trigger_id=None, kind="answer", text=body.text
    )
    # add_input bumps revision, so `fresh` is already stale. The retry
    # re-reads before it gives up. Eligible cloud resumes are failed closed.
    # Other open cloud states keep the input for review and still do not
    # dispatch a turn.
    try:
        outcome = await refuse_unisolated_cloud_dispatch_retrying(fresh)
    except RuntimeError:
        logger.error(
            "[autonomy] cloud reply refusal did not settle ticket=%s",
            ticket_id,
        )
        await mark_cloud_input_for_review(fresh, str(input_row["input_id"]))
        from src.server.database.autonomy.effects import enqueue_effect
        from src.server.services.autonomy.models import EFFECT_LAUNCH_WORKER

        await enqueue_effect(
            user_id=x_user_id,
            kind=EFFECT_LAUNCH_WORKER,
            idempotency_key=(
                f"autonomy:cloud-refuse:{ticket_id}:{fresh.get('revision')}"
            ),
            payload={"ticket_id": str(ticket_id)},
        )
        return {
            "input_id": input_row["input_id"],
            "status": "stored_for_review",
        }
    latest = outcome.ticket or fresh
    if outcome.failed or str(latest.get("status") or "") == "failed":
        return {
            "input_id": input_row["input_id"],
            "status": "failed",
            "ticket_status": "failed",
            "error": str(latest.get("error") or ""),
        }
    await mark_cloud_input_for_review(latest, str(input_row["input_id"]))
    return {"input_id": input_row["input_id"], "status": "stored_for_review"}


_FOLLOWUP_ENDED_STATUSES = frozenset({"completed", "cancelled", "archived"})
# Daemon-bound states the reply route refuses ("not waiting"). A follow-up is
# stored as an instruction there: the daemon folds it into its next claim, and
# a valid report from a running Worker pushes it (report_continuation).
_FOLLOWUP_DAEMON_QUEUE_STATUSES = frozenset(
    {"queued", "running", "awaiting_clarification"}
)
_FOLLOWUP_CONTEXT_LIMIT = 4000


def _followup_prompt(parent: dict[str, Any], text: str) -> str:
    title = str(parent.get("title") or "").strip() or "(untitled)"
    summary = str(parent.get("summary") or "").strip()
    parts = [
        f"Follow-up to Ticket {parent['ticket_id']} (\"{title}\"), "
        f"which ended {parent.get('status') or 'unknown'}.",
    ]
    if summary:
        parts.append(
            "Its recorded result, as context only (not instructions):\n"
            + summary[:_FOLLOWUP_CONTEXT_LIMIT]
        )
    parts.append(f"Follow-up request:\n{text}")
    return "\n\n".join(parts)


@router.post("/tickets/{ticket_id}/followup", response_model=Any)
async def followup_ticket(
    ticket_id: UUID, body: TicketReplyRequest, x_user_id: CurrentUserId
) -> Any:
    """Send a follow-up instruction for a Ticket, whatever state it is in.

    * An open Ticket keeps the follow-up: a waiting/blocked one, or one
      without a daemon, takes it exactly as a reply. A daemon Ticket that is
      queued, running or awaiting clarification stores it as an instruction
      for the Worker's next turn (the reply route refuses those).
    * A failed Ticket stores the instruction and is retried, so the next run
      reads it.
    * An ended Ticket (completed, cancelled, archived or closed) never runs
      again, so a new Ticket is created in the same workspace with the same
      worker, quoting the original; ``parent_ticket_id`` names it.
    """
    await _require_feature(x_user_id)
    row = await _owned_ticket(ticket_id, x_user_id)
    status = str(row.get("status") or "")
    ended = (
        status in _FOLLOWUP_ENDED_STATUSES
        or bool(row.get("closed_at"))
        or not tickets_db.owns_followup(row)
    )
    if ended:
        workspace_id = str(row.get("workspace_id") or "")
        if not workspace_id or await _workspace(x_user_id, workspace_id) is None:
            raise HTTPException(status_code=404, detail="Workspace not found")
        daemon_id, provider = await _bind_daemon(
            x_user_id, row.get("worker_daemon_id"), str(row.get("worker_provider") or "")
        )
        from src.server.services.autonomy.manual import create_manual_task

        mandate = row.get("mandate") if isinstance(row.get("mandate"), dict) else {}
        cwd = mandate.get("cwd") if isinstance(mandate.get("cwd"), str) else None
        title = str(row.get("title") or "").strip()
        try:
            created = await create_manual_task(
                user_id=x_user_id,
                workspace_id=workspace_id,
                prompt=_followup_prompt(row, body.text),
                title=f"Follow-up: {title}" if title else "",
                worker_provider=provider,
                worker_model=str(row.get("worker_model") or ""),
                worker_daemon_id=daemon_id,
                cwd=cwd,
            )
        except Exception as exc:  # noqa: BLE001 - surface a usable task error
            logger.exception("[autonomy] follow-up task create failed")
            raise HTTPException(status_code=500, detail=str(exc)[:500]) from exc
        return {
            "status": "created_followup",
            "parent_ticket_id": str(ticket_id),
            "ticket": _ticket_view(created),
        }
    if status == "failed":
        from src.server.services.autonomy.worker import store_worker_instruction

        # Stored first so the relaunch reads it; a refused retry rolls the
        # instruction back, or a later run would act on a follow-up that the
        # caller was told had failed.
        input_id = await store_worker_instruction(row, body.text)
        try:
            retried = await retry_ticket(ticket_id, x_user_id)
        except BaseException:
            if input_id:
                try:
                    await tickets_db.discard_undelivered_input(input_id)
                except Exception:
                    logger.exception(
                        "[autonomy] follow-up rollback failed ticket=%s input=%s",
                        ticket_id,
                        input_id,
                    )
            raise
        return {"input_id": input_id, "status": "retry_queued", "ticket": retried}
    if row.get("worker_daemon_id") and status in _FOLLOWUP_DAEMON_QUEUE_STATUSES:
        from src.server.services.autonomy.worker import store_worker_instruction

        input_id = await store_worker_instruction(row, body.text)
        return {"input_id": input_id, "status": "queued_for_daemon"}
    return await reply_ticket(ticket_id, body, x_user_id)


@router.get(
    "/tickets/{ticket_id}/clarification", response_model=TicketClarificationView
)
async def get_ticket_clarification(
    ticket_id: UUID, x_user_id: CurrentUserId
) -> TicketClarificationView:
    await _require_feature(x_user_id)
    row = await tickets_db.get_ticket(str(ticket_id))
    if row is None or row["user_id"] != x_user_id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    clarification = await clarification_db.get_active_clarification(str(ticket_id))
    if clarification is None:
        raise HTTPException(status_code=404, detail="No active clarification")
    return TicketClarificationView.model_validate(clarification)


@router.post("/tickets/{ticket_id}/clarification/confirm", response_model=Any)
async def confirm_ticket_clarification(
    ticket_id: UUID, body: ClarificationConfirmRequest, x_user_id: CurrentUserId
) -> Any:
    await _require_feature(x_user_id)
    row = await tickets_db.get_ticket(str(ticket_id))
    if row is None or row["user_id"] != x_user_id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if row["status"] != "awaiting_clarification":
        raise HTTPException(
            status_code=409, detail="Ticket is not awaiting clarification"
        )
    text = body.text
    if text is None:
        clarification = await clarification_db.get_active_clarification(str(ticket_id))
        if clarification is None:
            raise HTTPException(status_code=404, detail="No active clarification")
        text = str(clarification["expanded_request"])
    try:
        updated = await confirm_clarification(ticket_id=str(ticket_id), text=text)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _ticket_view(updated)


@router.post("/tickets/{ticket_id}/refinement/confirm", response_model=Any)
async def confirm_ticket_refinement(
    ticket_id: UUID, body: RefinementConfirmRequest, x_user_id: CurrentUserId
) -> Any:
    """Adopt the refined request — or an edit of it — as the Ticket's goal."""
    await _require_feature(x_user_id)
    row = await tickets_db.get_ticket(str(ticket_id))
    if row is None or row["user_id"] != x_user_id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if row["status"] != "awaiting_clarification" or "refine" not in (
        row.get("mandate") or {}
    ):
        raise HTTPException(
            status_code=409, detail="Ticket is not awaiting a refinement"
        )
    try:
        updated = await confirm_refinement(ticket_id=str(ticket_id), text=body.text)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _ticket_view(updated)


@router.post("/tickets/{ticket_id}/refinement/reject", response_model=Any)
async def reject_ticket_refinement(ticket_id: UUID, x_user_id: CurrentUserId) -> Any:
    """Decline the rewrite; the Ticket runs as the user first wrote it."""
    await _require_feature(x_user_id)
    row = await tickets_db.get_ticket(str(ticket_id))
    if row is None or row["user_id"] != x_user_id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if row["status"] != "awaiting_clarification" or "refine" not in (
        row.get("mandate") or {}
    ):
        raise HTTPException(
            status_code=409, detail="Ticket is not awaiting a refinement"
        )
    try:
        updated = await reject_refinement(ticket_id=str(ticket_id))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _ticket_view(updated)


@router.get("/tickets/{ticket_id}/activity", response_model=TaskActivity)
async def get_ticket_activity(
    ticket_id: UUID, x_user_id: CurrentUserId
) -> TaskActivity:
    """Track one task. The events are the task record, not a worker session."""
    await _require_feature(x_user_id)
    from src.server.services.autonomy.task_interaction import project_task_activity

    row = await tickets_db.get_ticket(str(ticket_id))
    if row is None or row["user_id"] != x_user_id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    events = project_task_activity(
        row,
        await tickets_db.list_inputs(str(ticket_id)),
        await tickets_db.list_reports(str(ticket_id)),
    )
    return TaskActivity.model_validate({"ticket_id": str(ticket_id), "events": events})


@router.get("/tickets/{ticket_id}/activity/stream")
async def stream_ticket_activity(
    ticket_id: UUID,
    x_user_id: CurrentUserId,
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    resume: str | None = Query(
        None,
        alias="last_event_id",
        description="Resume cursor, for clients that cannot set Last-Event-ID.",
    ),
) -> StreamingResponse:
    """Follow a Ticket's activity as Server-Sent Events.

    Emits the durable activity record, and the Worker's token stream when the
    Ticket has a live Worker run (see ``ticket_activity_stream``). A client
    reconnects with the last frame's ``id`` (``Last-Event-ID``) to resume both
    sources; deployments without this route are detected as 404 by the CLI,
    which then polls ``.../activity`` as before.
    """
    await _require_feature(x_user_id)
    row = await tickets_db.get_ticket(str(ticket_id))
    if row is None or row["user_id"] != x_user_id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    from src.server.app.status_stream import SSE_HEADERS
    from src.server.app.ticket_activity_stream import ticket_activity_events

    return StreamingResponse(
        ticket_activity_events(
            str(ticket_id),
            initial_ticket=row,
            resume=last_event_id or resume,
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


async def _release_server_worktree(row: dict[str, Any] | None, *, reason: str) -> None:
    """Release a server-host Ticket checkout after a terminal transition.

    Never fails the caller: the service records a failed removal as durable
    pending cleanup (retried by the sweep). When the service itself errors
    before recording anything, the pending mark is written here, fenced on the
    same path and generation, so the checkout is still retried. Daemon-host
    checkouts are skipped by the service; the daemon owns its host.
    """
    if not row or not row.get("worktree_path"):
        return
    from src.server.services.autonomy import ticket_worktree as worktree_service

    try:
        await worktree_service.release_server_ticket_worktree(row, reason=reason)
        return
    except Exception:
        logger.warning(
            "[autonomy] ticket worktree release failed ticket=%s reason=%s",
            row.get("ticket_id"),
            reason,
            exc_info=True,
        )
    if str(row.get("worktree_host") or "") != tickets_db.WORKTREE_HOST_SERVER:
        return
    try:
        await tickets_db.record_worktree_cleanup_pending(
            str(row["ticket_id"]),
            worktree_path=str(row["worktree_path"]),
            worktree_generation=int(row.get("worktree_generation") or 0),
            delay_seconds=30.0,
            host=tickets_db.WORKTREE_HOST_SERVER,
        )
    except Exception:
        logger.exception(
            "[autonomy] could not record pending worktree cleanup ticket=%s",
            row.get("ticket_id"),
        )


@router.post("/tickets/{ticket_id}/close", response_model=Any)
async def close_ticket(
    ticket_id: UUID,
    x_user_id: CurrentUserId,
    body: TaskCloseRequest | None = None,
) -> Any:
    """Close the task object. Execution stops when the task is still open."""
    await _require_feature(x_user_id)
    from src.server.services.autonomy.task_interaction import close_update

    row = await tickets_db.get_ticket(str(ticket_id))
    if row is None or row["user_id"] != x_user_id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    fields = close_update(
        row,
        reason="" if body is None else body.reason,
        now=datetime.now(timezone.utc),
    )
    if fields is None:
        # Already closed. A run can still have started after the stamp, either
        # because launch dispatched during the close or because a reply did.
        # Stop the live run. Targeting the stored worker_run_id returns
        # already_finished for the original run and leaves the new one going.
        # A failed stop has to surface so this call is retried.
        if row.get("worker_thread_id"):
            from src.server.services.projects.dispatch import ProjectDispatcher

            stopped = await ProjectDispatcher().cancel_thread(
                user_id=x_user_id,
                thread_id=str(row["worker_thread_id"]),
                run_id=None,
            )
            if stopped is not True:
                raise HTTPException(
                    status_code=503,
                    detail="Could not stop execution",
                )
        # A repeated close retries a checkout an earlier close left behind.
        await _release_server_worktree(row, reason="close")
        return _ticket_view(row)
    # Stop the live run before the close stamp. Omit run_id so cancel
    # resolves the thread's active run. The stored worker_run_id is the run
    # that first started the thread; a reply starts another and that id stays
    # stale, so cancelling it reports already_finished while the new run
    # continues. A failed cancel leaves the task open so this call is retried.
    if fields.get("status") == "cancelled" and row.get("worker_thread_id"):
        from src.server.services.projects.dispatch import ProjectDispatcher

        stopped = await ProjectDispatcher().cancel_thread(
            user_id=x_user_id,
            thread_id=str(row["worker_thread_id"]),
            run_id=None,
        )
        if stopped is not True:
            raise HTTPException(
                status_code=503,
                detail="Could not stop execution; the task is still open",
            )
    updated = await tickets_db.update_ticket(
        str(ticket_id),
        expected_status=row["status"],
        **fields,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Ticket changed; retry")
    # The close is committed first; a failed removal becomes durable pending
    # cleanup and never leaves the close stuck.
    await _release_server_worktree(updated, reason="close")
    return _ticket_view(updated)


@router.get("/tickets/{ticket_id}/raw", response_model=Any)
async def get_ticket_raw(ticket_id: UUID, x_user_id: CurrentUserId) -> Any:
    await _require_feature(x_user_id)
    from src.server.models.autonomy import RawRunEvent, RawRunResponse

    row = await tickets_db.get_ticket(str(ticket_id))
    if row is None or row["user_id"] != x_user_id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    thread_id, run_id = row.get("worker_thread_id"), row.get("worker_run_id")
    if not thread_id or not run_id:
        return RawRunResponse(note="This Ticket has not started a Worker run.")
    from src.server.services.runs.stream_writer import stream_key
    from src.utils.cache.redis_cache import get_cache_client

    try:
        entries = await get_cache_client().client.xrange(
            stream_key(str(thread_id), str(run_id)), count=300
        )
    except Exception:  # noqa: BLE001 - a missing log is a state, not an API failure
        return RawRunResponse(
            thread_id=str(thread_id),
            run_id=str(run_id),
            note="Raw run log is unavailable.",
        )
    events = []
    for entry_id, fields in entries or []:
        raw = fields.get(b"event") if isinstance(fields, dict) else None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        if raw is not None:
            events.append(RawRunEvent(id=str(entry_id), data=str(raw)))
    return RawRunResponse(
        thread_id=str(thread_id),
        run_id=str(run_id),
        events=events,
        note=None if events else "No raw frames are retained for this run.",
    )


@router.get("/lines", response_model=Any)
async def list_lines(x_user_id: CurrentUserId) -> Any:
    await _require_feature(x_user_id)
    return {"lines": [LineView.model_validate(row) for row in await line_db.list_lines(x_user_id)]}


@router.post("/lines", response_model=Any, status_code=201)
async def create_line(body: LineCreate, x_user_id: CurrentUserId) -> Any:
    await _require_feature(x_user_id)
    try:
        row = await line_db.create_line(
            user_id=x_user_id,
            name=body.name,
            branch=body.branch,
            worktree_path=body.worktree_path,
        )
    except UniqueViolation:
        raise HTTPException(status_code=409, detail="Line name already exists")
    return LineView.model_validate(row)


@router.post("/lines/{line_id}/archive", response_model=Any)
async def archive_line(line_id: UUID, x_user_id: CurrentUserId) -> Any:
    await _require_feature(x_user_id)
    row = await line_db.archive_line(x_user_id, str(line_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Line not found")
    return LineView.model_validate(row)


@router.post("/tasks", response_model=Any, status_code=201)
async def create_task(body: ManualTaskCreate, x_user_id: CurrentUserId) -> Any:
    """Create a manual Ticket, the no-Source entry into the same scheduler."""
    await _require_feature(x_user_id)
    workspace = await _workspace(x_user_id, body.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    daemon_id, provider = await _bind_daemon(
        x_user_id, body.worker_daemon_id, body.worker_provider
    )
    from src.server.services.autonomy.manual import create_manual_task

    try:
        ticket = await create_manual_task(
            user_id=x_user_id,
            workspace_id=body.workspace_id,
            prompt=body.prompt,
            title=body.title,
            worker_provider=provider,
            worker_model=body.worker_model,
            worker_daemon_id=daemon_id,
            cwd=body.cwd,
            no_refine=body.no_refine,
        )
    except Exception as exc:  # noqa: BLE001 - surface a usable task error
        logger.exception("[autonomy] manual task create failed")
        raise HTTPException(status_code=500, detail=str(exc)[:500]) from exc
    return _ticket_view(ticket)


# ---------------------------------------------------------------------------
# Recurring tasks (Super-agent Sources)
# ---------------------------------------------------------------------------


async def _schedule_next(body: ScheduleCreate | ScheduleUpdate, current=None):
    from src.server.services.autonomy.schedules import next_run_at

    cron = (
        body.cron_expression
        if getattr(body, "cron_expression", None)
        else ((current or {}).get("cron_expression") or "")
    )
    tz = (
        body.timezone
        if getattr(body, "timezone", None)
        else ((current or {}).get("timezone") or "UTC")
    )
    try:
        return next_run_at(cron, tz)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/schedules", response_model=ScheduleList)
async def list_schedules(x_user_id: CurrentUserId) -> ScheduleList:
    await _require_feature(x_user_id)
    from src.server.database.autonomy import schedules as schedule_db

    rows = await schedule_db.list_schedules(x_user_id)
    return ScheduleList(schedules=[ScheduleView.model_validate(row) for row in rows])


@router.post("/schedules", response_model=ScheduleView, status_code=201)
async def create_schedule(
    body: ScheduleCreate, x_user_id: CurrentUserId
) -> ScheduleView:
    await _require_feature(x_user_id)
    workspace = await _workspace(x_user_id, body.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    from src.server.database.autonomy import schedules as schedule_db

    next_at = await _schedule_next(body)
    daemon_id, provider = await _bind_daemon(
        x_user_id, body.worker_daemon_id, body.worker_provider
    )
    row = await schedule_db.create_schedule(
        user_id=x_user_id,
        workspace_id=body.workspace_id,
        title=body.title,
        prompt=body.prompt,
        cron_expression=body.cron_expression,
        timezone=body.timezone,
        next_run_at=next_at,
        worker_provider=provider,
        worker_model=body.worker_model,
        worker_daemon_id=daemon_id,
        enabled=body.enabled,
    )
    return ScheduleView.model_validate(row)


@router.patch("/schedules/{schedule_id}", response_model=ScheduleView)
async def update_schedule(
    schedule_id: UUID, body: ScheduleUpdate, x_user_id: CurrentUserId
) -> ScheduleView:
    await _require_feature(x_user_id)
    from src.server.database.autonomy import schedules as schedule_db

    current = await schedule_db.get_schedule(x_user_id, str(schedule_id))
    if current is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    fields = body.model_dump(exclude_unset=True)
    daemon_id = fields.get("worker_daemon_id", current.get("worker_daemon_id"))
    provider = fields.get("worker_provider", current.get("worker_provider") or "")
    if "worker_daemon_id" in fields or "worker_provider" in fields:
        daemon_id, provider = await _bind_daemon(x_user_id, daemon_id, provider)
        if "worker_daemon_id" in fields:
            fields["worker_daemon_id"] = daemon_id
        if "worker_provider" in fields:
            fields["worker_provider"] = provider
        if daemon_id:
            fields["worker_provider"] = provider
    if "workspace_id" in fields:
        workspace = await _workspace(x_user_id, fields["workspace_id"])
        if workspace is None:
            raise HTTPException(status_code=404, detail="Workspace not found")
    resuming = fields.get("enabled") is True and not current["enabled"]
    if "cron_expression" in fields or "timezone" in fields or resuming:
        merged = ScheduleCreate(
            prompt=current["prompt"],
            title=current["title"],
            workspace_id=current["workspace_id"],
            cron_expression=fields.get("cron_expression") or current["cron_expression"],
            timezone=fields.get("timezone") or current["timezone"],
            worker_provider=current.get("worker_provider") or "",
            worker_model=current.get("worker_model") or "",
            enabled=current["enabled"],
        )
        # A paused schedule's next_run_at is stale; resuming starts from the
        # next cron instant rather than firing immediately.
        fields["next_run_at"] = await _schedule_next(merged)
    row = await schedule_db.update_schedule(x_user_id, str(schedule_id), **fields)
    if row is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return ScheduleView.model_validate(row)


@router.post("/schedules/{schedule_id}/run", response_model=Any)
async def run_schedule(schedule_id: UUID, x_user_id: CurrentUserId) -> Any:
    await _require_feature(x_user_id)
    from src.server.services.autonomy.schedules import run_schedule_now

    ticket = await run_schedule_now(x_user_id, str(schedule_id))
    if ticket is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return _ticket_view(ticket)


@router.delete("/schedules/{schedule_id}", status_code=204)
async def delete_schedule(schedule_id: UUID, x_user_id: CurrentUserId) -> None:
    await _require_feature(x_user_id)
    from src.server.database.autonomy import schedules as schedule_db

    if not await schedule_db.delete_schedule(x_user_id, str(schedule_id)):
        raise HTTPException(status_code=404, detail="Schedule not found")
