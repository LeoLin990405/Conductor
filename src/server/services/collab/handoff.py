"""Handoff between holders: request, list, accept (N80 6.1-6.3, P4).

``handoff_request`` moves the holder's lease to ``handoff_pending`` (it can no
longer report) and records the pending handoff with the holder's untrusted
``self_report`` and an optional ``conversation_ref`` (``{app, session_id}``
only). ``to = router`` gives the task straight back to the Router: the lease
is released in the same transaction and the Ticket is requeued.

``handoff_accept`` transfers the lease in one transaction with the
``handed_over`` event. The pack's ``system`` section is generated here,
deterministically, from Conductor's own state (task view, commits and last
completion report, gate results, open findings, from, reason). Conductor never
reads or relays a conversation body: the accepting app fetches it from memfw
itself with ``conversation_ref``, so memfw's sharing rules stay the only
privacy rule. The previous holder's next ``task_report`` gets ``lease_lost``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from src.collab.types import (
    AssignmentRefusedError,
    InvalidSchemaError,
    LeaseExpiredError,
    NotFoundError,
    TooLargeError,
    normalize_app_id,
)
from src.server.database.autonomy import tickets as ticket_db
from src.server.database.collab import apps as app_db
from src.server.database.collab import handoffs as handoff_db
from src.server.database.collab import leases as lease_db
from src.server.database.collab import reviews as review_db
from src.server.services.autonomy.completion import completion_report_from_text
from src.server.services.collab import tickets as task_svc
from src.server.services.collab.leases import (
    HEARTBEAT_EVERY_SECONDS,
    held_lease,
    iso,
    lease_refusal,
    lock_app_leases,
)
from src.server.services.collab.tools import ToolCall, handler

HANDOFF_SECONDS = 30 * 60
SELF_REPORT_FIELDS = ("done", "in_progress", "next", "pitfalls", "open_questions")
SELF_REPORT_MAX_ITEMS = 10
SELF_REPORT_MAX_ITEM_CHARS = 200
SELF_REPORT_MAX_BYTES = 4096


def _sanitize(text: str) -> str:
    from src.server.utils.sanitize import sanitize_report_text

    return sanitize_report_text(text)


def clean_self_report(value: Mapping[str, Any] | None) -> dict[str, list[str]]:
    """The #52 five fields, sanitized; over 4 KiB in total is ``too_large``."""
    out: dict[str, list[str]] = {}
    for name in SELF_REPORT_FIELDS:
        items = (value or {}).get(name)
        if not items:
            continue
        out[name] = [
            _sanitize(str(item))[:SELF_REPORT_MAX_ITEM_CHARS]
            for item in list(items)[:SELF_REPORT_MAX_ITEMS]
        ]
    if len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > SELF_REPORT_MAX_BYTES:
        raise TooLargeError("too_large: self_report is larger than 4 KiB")
    return out


def _target(to: str, caller_app: str) -> str:
    text = str(to or "router").strip()
    if text in {"router", "any"}:
        return text
    app = text[4:] if text.startswith("app:") else text
    try:
        app = normalize_app_id(app)
    except ValueError as exc:
        raise InvalidSchemaError("to must be an app id, 'any' or 'router'") from exc
    if app == caller_app:
        raise InvalidSchemaError("an app cannot hand a lease off to itself")
    return f"app:{app}"


def _handoff_id(value: str) -> str:
    text = task_svc.canonical_uuid(value)
    if text is None:
        raise NotFoundError("not_found: no such handoff")
    return text


@handler("handoff_request")
async def handoff_request(call: ToolCall) -> dict[str, Any]:
    args = call.arguments
    lease_id = str(args["lease_id"])
    call.set_target(lease_id)
    lease = await held_lease(call, lease_id, states=frozenset({"active"}))
    ticket = await task_svc.ticket_for_lease(lease, user_id=call.caller.user_id)
    call.require_task_mode(task_svc.task_facts(ticket))
    target = _target(str(args.get("to") or "router"), call.caller.app_id)
    note = _sanitize(str(args.get("note") or ""))[:1000]
    self_report = clean_self_report(args.get("self_report"))
    conversation = args.get("conversation_ref")
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=HANDOFF_SECONDS)
    async with call.transaction() as conn:
        pending = await lease_db.set_handoff_pending(
            str(lease["lease_id"]), app_id=call.caller.app_id, expires_at=expires_at, conn=conn
        )
        if pending is None:
            raise lease_refusal(
                await lease_db.get_lease(str(lease["lease_id"]), conn=conn), call.caller.app_id
            )
        row = await handoff_db.create_handoff(
            lease_id=str(lease["lease_id"]),
            from_app_id=call.caller.app_id,
            reason=str(args["reason"]),
            expires_at=expires_at,
            to_target=target,
            note=note,
            self_report=self_report,
            conversation_ref=dict(conversation) if isinstance(conversation, Mapping) else None,
            conn=conn,
        )
        if row is None:
            raise lease_refusal(
                await lease_db.get_lease(str(lease["lease_id"]), conn=conn), call.caller.app_id
            )
        if target == "router":
            released = await lease_db.release_lease(
                str(lease["lease_id"]),
                app_id=call.caller.app_id,
                reason="handoff_router",
                conn=conn,
            )
            if released is None:
                raise LeaseExpiredError("lease_expired: the lease ended before the handoff")
            # The Router takes it now; no app can accept this row any more.
            await handoff_db.cancel_handoff(
                str(row["handoff_id"]), app_id=call.caller.app_id, conn=conn
            )
            await task_svc.record_event(
                conn,
                ticket=ticket,
                actor=call.caller.actor,
                collab_event="handed_over",
                from_owner=f"app:{call.caller.app_id}",
                to_owner="router",
                reason=str(args["reason"]),
                extra={"lease_id": str(lease["lease_id"]), "handoff_id": str(row["handoff_id"])},
            )
        data = {"handoff_id": str(row["handoff_id"]), "expires_at": iso(row.get("expires_at")) or expires_at.isoformat()}
        return await call.finish(conn, data)


@handler("handoff_list")
async def handoff_list(call: ToolCall) -> dict[str, Any]:
    limit = int(call.arguments.get("limit") or 20)
    rows = await handoff_db.list_handoffs(app_id=call.caller.app_id, limit=50)
    owners: dict[str, str] = {}
    items: list[dict[str, Any]] = []
    for row in rows:
        source = str(row.get("from_app_id") or "")
        if source == call.caller.app_id:
            continue
        if source not in owners:
            app = await app_db.get_app(source)
            owners[source] = str((app or {}).get("user_id") or "")
        if owners[source] != call.caller.user_id:
            continue
        item: dict[str, Any] = {
            "handoff_id": str(row["handoff_id"]),
            "state": str(row.get("state") or ""),
            "from": f"app:{source}",
            "reason": str(row.get("reason") or ""),
            "note": str(row.get("note") or "")[:1000],
        }
        expires = iso(row.get("expires_at"))
        if expires:
            item["expires_at"] = expires
        items.append(item)
        if len(items) >= limit:
            break
    return {"handoffs": items}


def _last_report(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The previous completion report, re-validated; anything else is dropped."""
    if not row:
        return None
    try:
        body = json.loads(str(row.get("body") or ""))
    except ValueError:
        return None
    candidate = body.get("report") if isinstance(body, dict) and "report" in body else body
    if not isinstance(candidate, dict):
        return None
    candidate = {key: candidate.get(key) for key in ("status", "summary", "artifacts", "verification", "next", "handbacks")}
    if isinstance(candidate.get("artifacts"), dict) and candidate["artifacts"].get("pr") is None:
        candidate["artifacts"] = {**candidate["artifacts"], "pr": ""}
    report = completion_report_from_text(json.dumps(candidate, ensure_ascii=False))
    if report is None:
        return None
    payload = report.to_payload()
    if payload["artifacts"]["pr"] is None:
        payload["artifacts"]["pr"] = ""
    return payload


async def system_section(
    *,
    ticket: Mapping[str, Any],
    handoff: Mapping[str, Any],
    conn: Any = None,
) -> dict[str, Any]:
    """The deterministic part of the pack: Conductor's facts only."""
    latest = await ticket_db.latest_report(str(ticket["ticket_id"]), conn=conn)
    last = _last_report(latest)
    reviews = await review_db.list_reviews(
        app_id=None, reviewer_target=None, state="completed", limit=50
    )
    findings: list[str] = []
    ref = task_svc.ref_of(ticket)
    for review in reviews:
        if str(review.get("target_ref") or "") != ref:
            continue
        verdict = review.get("verdict") or {}
        if verdict.get("passed") is False:
            findings.extend(str(item)[:500] for item in verdict.get("blockers") or [])
    section: dict[str, Any] = {
        "task": task_svc.task_view(ticket, refusal=task_svc.R_ALREADY_CLAIMED),
        "commits": list((last or {}).get("artifacts", {}).get("commits", []))[:50],
        "gate_results": [],
        "open_findings": findings[:20],
        "from": f"app:{handoff.get('from_app_id')}",
        "reason": str(handoff.get("reason") or ""),
    }
    if last is not None:
        section["last_report"] = last
    return section


@handler("handoff_accept")
async def handoff_accept(call: ToolCall) -> dict[str, Any]:
    handoff_id = _handoff_id(str(call.arguments["handoff_id"]))
    call.set_target(handoff_id)
    handoff = await handoff_db.get_handoff(handoff_id)
    if handoff is None or str(handoff.get("state") or "") != "pending":
        raise NotFoundError("not_found: no such pending handoff")
    if str(handoff.get("from_app_id") or "") == call.caller.app_id:
        raise AssignmentRefusedError("own_handoff: an app cannot accept its own handoff")
    lease = await lease_db.get_lease(str(handoff["lease_id"]))
    if lease is None:
        raise NotFoundError("not_found: no such pending handoff")
    ticket = await task_svc.ticket_for_lease(lease, user_id=call.caller.user_id)
    call.require_task_mode(task_svc.task_facts(ticket))
    if task_svc.write_scope_of(ticket) and not call.caller.can_write:
        raise AssignmentRefusedError(f"{task_svc.R_CANNOT_TAKE}: the app is registered read-only")
    if not call.caller.enabled:
        raise AssignmentRefusedError(f"{task_svc.R_DISABLED}: the app is disabled")
    async with call.transaction() as conn:
        await lock_app_leases(conn, call.caller.app_id)
        if len(await lease_db.list_active_leases(call.caller.app_id, conn=conn)) >= call.caller.max_leases:
            raise AssignmentRefusedError(f"{task_svc.R_MAX_LEASES}: the app holds its lease limit")
        result = await handoff_db.accept_handoff(
            handoff_id=handoff_id,
            app_id=call.caller.app_id,
            agent=call.caller.agent,
            conn=conn,
        )
        if result is None:
            raise NotFoundError("not_found: no such pending handoff")
        moved = result.get("lease")
        if not moved:
            raise LeaseExpiredError("lease_expired: the handed-off lease has expired")
        await task_svc.record_event(
            conn,
            ticket=ticket,
            actor=call.caller.actor,
            collab_event="handed_over",
            from_owner=f"app:{handoff.get('from_app_id')}",
            to_owner=f"app:{call.caller.app_id}",
            reason=str(handoff.get("reason") or ""),
            extra={"lease_id": str(moved["lease_id"]), "handoff_id": handoff_id},
        )
        pack: dict[str, Any] = {
            "system": await system_section(ticket=ticket, handoff=handoff, conn=conn),
            "worktree": {
                "path": str(moved.get("worktree_path") or ""),
                "branch": str(moved.get("worktree_branch") or ""),
                "base_sha": str(moved.get("worktree_base_sha") or ""),
            },
        }
        reported = handoff.get("self_report") or {}
        if reported:
            pack["worker_reported"] = clean_self_report(reported)
        ref = handoff.get("conversation_ref")
        if isinstance(ref, Mapping) and ref.get("app") and ref.get("session_id"):
            pack["conversation_ref"] = {"app": str(ref["app"]), "session_id": str(ref["session_id"])[:200]}
        data = {
            "lease_id": str(moved["lease_id"]),
            # The accepting app heartbeats the moved lease exactly like a
            # claimed one, so it gets the same interval as ``task_claim``.
            "heartbeat_every_seconds": HEARTBEAT_EVERY_SECONDS,
            "pack": pack,
        }
        return await call.finish(conn, data)


__all__ = [
    "HANDOFF_SECONDS",
    "SELF_REPORT_FIELDS",
    "clean_self_report",
    "handoff_accept",
    "handoff_list",
    "handoff_request",
    "system_section",
]
