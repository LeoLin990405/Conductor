"""Ticket-side task views, claimability and proposals (N80 4.3, 5.1, 5.2, P8).

A Ticket is claimable by an app only when it is ``queued``, has no Worker run,
daemon lease or live collab lease, and carries ``mandate.collab.app_claimable
= true`` (set by the Router, the owner, or a confirmed proposal; never by an app).
:func:`claim_refusal` walks the PRD 5.2 checks in order and names the first
one that fails, so ``task_list`` can show ``claim_refusal`` without the app
having to try and fail.

Ownership events use the existing ``autonomy_ticket_events`` actions (the
CHECK is not widened): ``claimed`` for a claim and ``reassigned`` for every
other collab transition. The collab-specific kind is
``detail.collab_event`` (``claimed``, ``released``, ``lease_expired``,
``handed_over``, ``reported``) and a veto reason is ``detail.reason``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import uuid4

from src.collab import modes
from src.collab.types import (
    InvalidSchemaError,
    NotFoundError,
    TaskRef,
    ticket_ref,
)
from src.server.database.autonomy import lines as line_db
from src.server.database.autonomy import tickets as ticket_db
from src.server.database.collab import leases as lease_db

OPEN_STATUSES = ("queued", "running", "waiting", "blocked", "awaiting_clarification")
#: Ticket states that end the work; an ended Ticket is never offered.
ENDED_STATUSES = frozenset({"completed", "cancelled", "archived", "failed"})
COLLAB_EVENTS = frozenset(
    {"claimed", "released", "lease_expired", "handed_over", "reported"}
)
PROPOSAL_POLICY_VERSION = "collab-proposal-v1"

# Claim refusal reasons shown in ``claim_refusal`` and refusal messages.
R_SCOPE = "scope_denied"
R_MODE = "mode_denied"
R_NOT_APP_CLAIMABLE = "not_app_claimable"
R_NOT_QUEUED = "not_queued"
R_ALREADY_CLAIMED = "already_claimed"
R_DOCS_ONLY = "mode_docs_only"
R_HIGH_RISK = "high_risk_path"
R_RISK_BEARING = "risk_bearing"
R_DISABLED = "disabled"
R_CANNOT_TAKE = "cannot_take_line"
R_MAX_LEASES = "max_leases"
R_NO_WORKTREE = "no_worktree"
R_LINES = "line_claims_not_enabled"

#: Refusal reason -> envelope code.
REFUSAL_CODES: dict[str, str] = {
    R_SCOPE: "scope_denied",
    R_MODE: "mode_denied",
    R_NOT_APP_CLAIMABLE: "not_claimable",
    R_NOT_QUEUED: "not_claimable",
    R_ALREADY_CLAIMED: "already_claimed",
    R_DOCS_ONLY: "mode_denied",
    R_HIGH_RISK: "mode_denied",
    R_RISK_BEARING: "mode_denied",
    R_DISABLED: "assignment_refused",
    R_CANNOT_TAKE: "assignment_refused",
    R_MAX_LEASES: "assignment_refused",
    R_NO_WORKTREE: "not_claimable",
    R_LINES: "not_claimable",
}


def canonical_uuid(value: Any) -> str | None:
    """The canonical lower-case UUID text, or ``None`` for anything else."""
    import uuid as _uuid

    text = str(value or "").strip().lower()
    try:
        parsed = _uuid.UUID(text)
    except (ValueError, AttributeError, TypeError):
        return None
    return text if str(parsed) == text else None


def mandate_of(ticket: Mapping[str, Any]) -> dict[str, Any]:
    value = ticket.get("mandate")
    return dict(value) if isinstance(value, Mapping) else {}


def collab_of(ticket: Mapping[str, Any]) -> dict[str, Any]:
    value = mandate_of(ticket).get("collab")
    return dict(value) if isinstance(value, Mapping) else {}


def _strings(value: Any, *, limit: int, item_chars: int) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Sequence):
        return []
    out = [str(item)[:item_chars] for item in value if isinstance(item, str) and item]
    return out[:limit]


def write_scope_of(ticket: Mapping[str, Any]) -> tuple[str, ...]:
    collab = collab_of(ticket)
    scope = collab.get("write_scope")
    if scope is None:
        scope = mandate_of(ticket).get("write_scope")
    return tuple(_strings(scope, limit=50, item_chars=200))


def acceptance_of(ticket: Mapping[str, Any]) -> list[str]:
    collab = collab_of(ticket)
    value = collab.get("acceptance")
    if value is None:
        value = mandate_of(ticket).get("acceptance")
    return _strings(value, limit=20, item_chars=500)


def risk_bearing_of(ticket: Mapping[str, Any]) -> bool:
    return bool(collab_of(ticket).get("risk_bearing") or mandate_of(ticket).get("risk_bearing"))


def task_facts(ticket: Mapping[str, Any]) -> modes.TaskFacts:
    """The mode table's view of a Ticket; ``app_claimable`` must be exactly True."""
    return modes.TaskFacts(
        kind="ticket",
        app_claimable=collab_of(ticket).get("app_claimable") is True,
        write_scope=write_scope_of(ticket),
        risk_bearing=risk_bearing_of(ticket),
    )


def repo_of(ticket: Mapping[str, Any]) -> str:
    collab = collab_of(ticket)
    mandate = mandate_of(ticket)
    return str(collab.get("repo") or mandate.get("cwd") or "")[:200]


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value:
        return value
    return None


def ref_of(ticket: Mapping[str, Any]) -> str:
    return ticket_ref(str(ticket["ticket_id"]))


def _high_risk(write_scope: Sequence[str], environ: Mapping[str, str] | None) -> bool:
    from src.tools.auto_dispatch.auto_merge import high_risk_globs, is_high_risk

    return is_high_risk([], list(write_scope), False, globs=high_risk_globs(environ=environ))


def has_worker_owner(ticket: Mapping[str, Any]) -> bool:
    return bool(ticket.get("worker_run_id") or ticket.get("worker_lease_id"))


def claim_refusal(
    ticket: Mapping[str, Any],
    *,
    scopes: frozenset[str],
    app_enabled: bool,
    can_write: bool,
    max_leases: int,
    live_lease: Mapping[str, Any] | None,
    live_count: int,
    mode: str,
    environ: Mapping[str, str] | None = None,
    revision: int | None = None,
) -> str | None:
    """The first PRD 5.2 check ``ticket`` fails for this app, or ``None``.

    ``revision`` (the caller's CAS token) is compared by the claim service,
    not here, so a stale revision is reported as ``stale_revision``.
    """
    if "claim" not in scopes:
        return R_SCOPE
    if "claim" not in modes.mode_scopes(mode):
        return R_MODE
    facts = task_facts(ticket)
    if not facts.app_claimable:
        return R_NOT_APP_CLAIMABLE
    if live_lease is not None:
        return R_ALREADY_CLAIMED
    if str(ticket.get("status") or "") != "queued" or has_worker_owner(ticket):
        return R_NOT_QUEUED
    decision = modes.decide("task_claim", mode=mode, task=facts, environ=environ)
    if not decision.allowed:
        if decision.reason == modes.REASON_NOT_DOCS_TASK:
            if facts.risk_bearing:
                return R_RISK_BEARING
            if facts.write_scope and _high_risk(facts.write_scope, environ):
                return R_HIGH_RISK
            return R_DOCS_ONLY
        if decision.reason == modes.REASON_NOT_APP_CLAIMABLE:
            return R_NOT_APP_CLAIMABLE
        return R_MODE
    # N73 feasibility, as it applies to an interactive app (5.2 step 4).
    if not app_enabled:
        return R_DISABLED
    if facts.write_scope and not can_write:
        return R_CANNOT_TAKE
    if live_count >= max_leases:
        return R_MAX_LEASES
    if not repo_of(ticket):
        return R_NO_WORKTREE
    return None


def task_view(
    ticket: Mapping[str, Any],
    *,
    refusal: str | None,
    live_lease: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``task_view.json`` shape. ``body`` and ``acceptance`` are untrusted."""
    view: dict[str, Any] = {
        "ref": ref_of(ticket),
        "kind": "ticket",
        "title": str(ticket.get("title") or "")[:200],
        "body": str(ticket.get("scope") or "")[:8000],
        "status": str(ticket.get("status") or ""),
        "claimable": refusal is None,
        "write_scope": list(write_scope_of(ticket)),
        "acceptance": acceptance_of(ticket),
        "risk_bearing": risk_bearing_of(ticket),
        "revision": int(ticket.get("revision") or 0),
    }
    if refusal is not None:
        view["claim_refusal"] = refusal
    repo = repo_of(ticket)
    if repo:
        view["repo"] = repo
    if live_lease is not None:
        owner: dict[str, Any] = {"kind": "app_lease", "id": f"app:{live_lease.get('app_id')}"}
        deadline = _iso(live_lease.get("expires_at"))
        if deadline:
            owner["deadline"] = deadline
        view["owner"] = owner
    elif has_worker_owner(ticket):
        view["owner"] = {"kind": "worker", "id": str(ticket.get("worker_run_id") or ticket.get("worker_lease_id"))}
    return view


async def load_ticket(ref: str, *, user_id: str, conn: Any = None) -> dict[str, Any]:
    """The caller's Ticket for ``ref``; another user's Ticket is ``not_found``."""
    try:
        parsed = TaskRef.parse(ref)
    except ValueError as exc:
        raise InvalidSchemaError(str(exc)) from exc
    if not parsed.is_ticket:
        raise NotFoundError(
            f"{R_LINES}: dispatch line references are not served until N80 L4"
        )
    ticket_id = canonical_uuid(parsed.ticket_id)
    if ticket_id is None:
        raise NotFoundError("not_found: no such task")
    ticket = await ticket_db.get_ticket(ticket_id, conn=conn)
    if ticket is None or str(ticket.get("user_id") or "") != user_id:
        raise NotFoundError("not_found: no such task")
    return ticket


async def ticket_for_lease(
    lease: Mapping[str, Any], *, user_id: str, conn: Any = None
) -> dict[str, Any]:
    ticket_id = str(lease.get("ticket_id") or "")
    if not ticket_id:
        raise NotFoundError(f"{R_LINES}: line leases are not served until N80 L4")
    ticket = await ticket_db.get_ticket(ticket_id, conn=conn)
    if ticket is None or str(ticket.get("user_id") or "") != user_id:
        raise NotFoundError("not_found: no such task")
    return ticket


async def record_event(
    conn: Any,
    *,
    ticket: Mapping[str, Any],
    actor: str,
    collab_event: str,
    from_owner: str = "",
    to_owner: str = "",
    reason: str = "",
    extra: Mapping[str, Any] | None = None,
) -> None:
    """One ``autonomy_ticket_events`` row on the caller's transaction."""
    if collab_event not in COLLAB_EVENTS:
        raise ValueError(f"unknown collab event {collab_event!r}")
    detail: dict[str, Any] = {"collab_event": collab_event}
    if reason:
        detail["reason"] = reason
    if extra:
        detail.update(dict(extra))
    async with conn.cursor() as cur:
        await line_db.insert_ticket_event(
            cur,
            ticket_id=str(ticket["ticket_id"]),
            user_id=str(ticket.get("user_id") or ""),
            actor=actor,
            action="claimed" if collab_event == "claimed" else "reassigned",
            from_owner=from_owner,
            to_owner=to_owner,
            detail=detail,
        )


async def live_leases(app_id: str, *, conn: Any = None) -> list[dict[str, Any]]:
    return await lease_db.list_active_leases(app_id, conn=conn)


def _cursor_offset(cursor: Any) -> int:
    if cursor in (None, ""):
        return 0
    text = str(cursor)
    if not text.startswith("o:") or not text[2:].isdigit():
        raise InvalidSchemaError("cursor is not one this server issued")
    return min(int(text[2:]), 100_000)


async def list_tasks(caller: Any, arguments: Mapping[str, Any], *, mode: str, environ: Mapping[str, str] | None) -> dict[str, Any]:
    """``task_list``: the caller's Tickets with claimability, paged by offset."""
    kind = str(arguments.get("kind") or "any")
    limit = int(arguments.get("limit") or 20)
    claimable_only = arguments.get("claimable", True) is not False
    mine = arguments.get("mine") is True
    statuses = tuple(arguments.get("status") or ()) or (
        ("queued",) if claimable_only else OPEN_STATUSES
    )
    offset = _cursor_offset(arguments.get("cursor"))
    if kind == "line":
        return {"tasks": [], "next_cursor": None}
    leases = await live_leases(caller.app_id)
    held = {str(row.get("ticket_id")): row for row in leases if row.get("ticket_id")}
    page_size = 200
    rows = await ticket_db.list_tickets(
        caller.user_id, statuses=statuses, limit=page_size, offset=offset
    )
    tasks: list[dict[str, Any]] = []
    consumed = 0
    for row in rows:
        consumed += 1
        if mine and str(row.get("ticket_id")) not in held:
            continue
        live = held.get(str(row.get("ticket_id")))
        if live is None:
            live = await lease_db.lease_for_target(ref_of(row))
        refusal = claim_refusal(
            row,
            scopes=caller.scopes,
            app_enabled=caller.enabled,
            can_write=caller.can_write,
            max_leases=caller.max_leases,
            live_lease=live,
            live_count=len(leases),
            mode=mode,
            environ=environ,
        )
        if claimable_only and refusal is not None:
            continue
        tasks.append(task_view(row, refusal=refusal, live_lease=live))
        if len(tasks) >= limit:
            break
    more = len(rows) == page_size or consumed < len(rows)
    next_cursor = f"o:{offset + consumed}" if more else None
    return {"tasks": tasks, "next_cursor": next_cursor}


async def get_task(caller: Any, ref: str, *, mode: str, environ: Mapping[str, str] | None) -> dict[str, Any]:
    ticket = await load_ticket(ref, user_id=caller.user_id)
    live = await lease_db.lease_for_target(ref_of(ticket))
    leases = await live_leases(caller.app_id)
    refusal = claim_refusal(
        ticket,
        scopes=caller.scopes,
        app_enabled=caller.enabled,
        can_write=caller.can_write,
        max_leases=caller.max_leases,
        live_lease=live,
        live_count=len(leases),
        mode=mode,
        environ=environ,
    )
    return {"task": task_view(ticket, refusal=refusal, live_lease=live)}


async def create_proposal(caller: Any, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """``task_create``: an ``awaiting_clarification`` Ticket, never claimable.

    The Router does not route it and no app can claim it until the owner confirms it
    through ``/tickets/{id}/clarification/confirm`` and decides
    ``app_claimable`` (``suggest_app_claimable`` is only the proposer's hint).
    """
    title = str(arguments["title"]).strip()
    if len(title) < 3:
        raise InvalidSchemaError("title must have at least 3 non-space characters")
    body = str(arguments.get("body") or "")
    repo = str(arguments["repo"]).strip()
    parent_ref = str(arguments.get("parent_ref") or "")
    workspace_id = f"collab:{caller.app_id}"
    if parent_ref:
        parent = await load_ticket(parent_ref, user_id=caller.user_id)
        workspace_id = str(parent.get("workspace_id") or workspace_id)
    collab: dict[str, Any] = {
        "proposed_by": caller.actor,
        "app_claimable": False,
        "suggest_app_claimable": arguments.get("suggest_app_claimable") is True,
        "write_scope": list(arguments.get("write_scope") or []),
        "acceptance": list(arguments.get("acceptance") or []),
        "repo": repo,
    }
    if parent_ref:
        collab["parent_ref"] = parent_ref
    mandate: dict[str, Any] = {
        "strength": "explicit",
        "source": "app",
        "scope": body[:1500],
        "collab": collab,
    }
    if os.path.isabs(repo):
        mandate["cwd"] = repo
    key = uuid4().hex
    ticket = await ticket_db.create_ticket(
        user_id=caller.user_id,
        workspace_id=workspace_id,
        title=title[:200],
        scope=body,
        mandate=mandate,
        policy_version=PROPOSAL_POLICY_VERSION,
        origin_trigger_id=f"collab:{caller.app_id}:{key}",
        origin_thread_key=f"collab:{caller.app_id}:{key}",
        initial_status="awaiting_clarification",
    )
    return task_view(ticket, refusal=R_NOT_APP_CLAIMABLE)


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

from src.server.services.collab.tools import ToolCall, handler  # noqa: E402


@handler("task_list")
async def _task_list(call: ToolCall) -> dict[str, Any]:
    return await list_tasks(call.caller, call.arguments, mode=call.mode, environ=call.environ)


@handler("task_get")
async def _task_get(call: ToolCall) -> dict[str, Any]:
    return await get_task(call.caller, str(call.arguments["ref"]), mode=call.mode, environ=call.environ)


async def withdraw_proposal(call: ToolCall, ticket_id: str) -> None:
    """Remove a proposal whose audit row could not be written.

    ``create_ticket`` owns its own connection, so the proposal and its audit
    row cannot share one transaction; an unaudited proposal is deleted again
    instead (only while it is still the untouched ``awaiting_clarification``
    row this call created).
    """
    async with call.transaction() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM autonomy_tickets
                WHERE ticket_id = %s
                  AND status = 'awaiting_clarification'
                  AND revision = 0
                """,
                (ticket_id,),
            )


@handler("task_create")
async def _task_create(call: ToolCall) -> dict[str, Any]:
    view = await create_proposal(call.caller, call.arguments)
    call.set_target(view["ref"])
    try:
        async with call.transaction() as conn:
            return await call.finish(conn, view)
    except BaseException:
        try:
            await withdraw_proposal(call, view["ref"][2:])
        except Exception:  # noqa: BLE001 - the audit failure is what is reported
            import logging

            logging.getLogger(__name__).error(
                "[collab] could not withdraw an unaudited proposal %s", view["ref"]
            )
        raise


__all__ = [
    "COLLAB_EVENTS",
    "ENDED_STATUSES",
    "OPEN_STATUSES",
    "REFUSAL_CODES",
    "acceptance_of",
    "claim_refusal",
    "collab_of",
    "create_proposal",
    "get_task",
    "has_worker_owner",
    "list_tasks",
    "live_leases",
    "load_ticket",
    "mandate_of",
    "record_event",
    "ref_of",
    "repo_of",
    "risk_bearing_of",
    "task_facts",
    "task_view",
    "ticket_for_lease",
    "write_scope_of",
]
