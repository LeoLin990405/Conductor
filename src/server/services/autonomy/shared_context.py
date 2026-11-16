"""Assembles the bounded shared-thread background a Worker gets at launch.

The snapshot covers every Thread the Ticket is subscribed to (own or context):
its recent triggers, sibling Tickets, and their reports, plus the context-kind
inputs stored for the Ticket and any handoffs the Ticket inherited through
reassignment. A Ticket that shares nothing — only its own origin Thread, no
context inputs — gets None so its launch prompt stays byte-identical to a
pre-shared-thread Worker. Overflowing rows stay reachable through the
Worker's scoped autonomy_read.
"""

from __future__ import annotations

from typing import Any

from src.server.database.autonomy import tickets as ticket_db
from src.server.database.autonomy import triggers as trigger_db
from src.server.services.autonomy import context_scope

SHARED_CONTEXT_BUDGET_CHARS = 4000

_TRIGGERS_PER_THREAD = 5
_TICKETS_PER_THREAD = 8
_REPORTS_PER_THREAD = 5
_CONTEXT_INPUTS_MAX = 8
_TRUNCATION_LINE = "… truncated; full rows are available via autonomy_read(thread)."


def _clip(value: Any, chars: int) -> str:
    text = str(value or "")
    return text if len(text) <= chars else text[: chars - 1] + "…"


async def shared_context_for_ticket(ticket: dict[str, Any]) -> dict[str, Any] | None:
    """Assemble the shared-thread snapshot for a Ticket, or None if nothing is shared.

    A Ticket in an isolated scope only sees its own rows: sibling Ticket
    summaries and reports on the same Thread are dropped, and a context-only
    subscription is ignored. A shared scope publishes its members' rows only
    when its data policy says so; the scope is stated in the rendered block.
    """
    ticket_id = str(ticket["ticket_id"])
    scope = await context_scope.ensure_scope(ticket)
    shared_scope = str(scope.get("kind")) == "shared"
    members = await context_scope.scope_member_ids(ticket, scope=scope)
    visible_tickets = await context_scope.readable_ticket_ids(
        ticket, scope=scope, data_class="tickets"
    )
    visible_reports = await context_scope.readable_ticket_ids(
        ticket, scope=scope, data_class="reports"
    )
    subs = await ticket_db.list_subscriptions(ticket_id, active_only=True)
    if not subs:
        return None
    # Context-kind inputs grant no authority; they are surfaced verbatim so the
    # Worker knows what it has been told, wherever the input came from.
    context_inputs = [
        row
        for row in await ticket_db.list_inputs(ticket_id, limit=50)
        if row.get("kind") == "context"
    ]
    # Handoffs are inherited state: a Ticket that received a Thread through
    # reassignment must see the previous owner's last report at launch.
    handoffs = await ticket_db.list_handoffs_for_ticket(ticket_id)
    threads: list[dict[str, Any]] = []
    for sub in subs:
        thread_key = str(sub["thread_key"])
        if str(sub.get("kind")) == "context":
            if not shared_scope:
                continue
            try:
                await context_scope.assert_can_follow_thread(
                    ticket, thread_key, scope=scope
                )
            except context_scope.ContextScopeError:
                continue
        threads.append(
            {
                "thread_key": thread_key,
                "kind": str(sub["kind"]),
                "triggers": await trigger_db.list_triggers_for_thread(
                    thread_key, limit=10
                ),
                "tickets": context_scope.hide_unpublished_report_text(
                    context_scope.filter_rows(
                        await ticket_db.list_tickets_for_thread(thread_key),
                        visible_tickets,
                    ),
                    scope=scope,
                ),
                "reports": context_scope.filter_rows(
                    await ticket_db.list_reports_for_thread(thread_key, limit=10),
                    visible_reports,
                ),
            }
        )
    shared = (shared_scope and (len(members) > 1 or len(threads) > 1)) or any(
        thread["kind"] == "context" for thread in threads
    )
    if not shared and not context_inputs and not handoffs:
        return None
    data = {
        "ticket_id": ticket_id,
        "threads": threads,
        "context_inputs": context_inputs,
        "handoffs": handoffs,
    }
    if shared_scope:
        data["context_scope"] = {
            "scope_id": str(scope.get("scope_id") or ""),
            "name": str(scope.get("name") or ""),
            "kind": "shared",
            "participants": len(members),
        }
    return data


def render_shared_context(
    data: dict[str, Any], *, max_chars: int = SHARED_CONTEXT_BUDGET_CHARS
) -> str:
    """Render the snapshot into a budgeted text block, dropping what overflows."""
    lines: list[str] = []
    used = 0
    dropped = False

    def emit(line: str, *, structural: bool = False) -> bool:
        nonlocal used, dropped
        cost = len(line) + (1 if lines else 0)
        if used + cost > max_chars and not structural:
            dropped = True
            return False
        lines.append(line)
        used += cost
        return True

    scope = data.get("context_scope") or {}
    if scope.get("kind") == "shared":
        emit(
            f"Context scope shared: {scope.get('name') or scope.get('scope_id')} "
            f"({scope.get('participants', 0)} participant(s); "
            "these rows are shared only inside it).",
            structural=True,
        )

    for thread in data.get("threads") or []:
        tickets = list(thread.get("tickets") or [])
        emit(
            f"Thread {thread.get('thread_key')} ({thread.get('kind')}; "
            f"{len(tickets)} ticket(s))",
            structural=True,
        )
        triggers = list(thread.get("triggers") or [])
        dropped = dropped or len(triggers) > _TRIGGERS_PER_THREAD
        for row in triggers[:_TRIGGERS_PER_THREAD]:
            line = (
                f"- [trigger {row.get('trigger_id')}] "
                f"{str(row.get('actor_login') or '')}: {_clip(row.get('text'), 240)}"
            )
            if not emit(line):
                break
        dropped = dropped or len(tickets) > _TICKETS_PER_THREAD
        for row in tickets[:_TICKETS_PER_THREAD]:
            line = (
                f"- [ticket {row.get('ticket_id')}] "
                f"{_clip(row.get('title'), 80)} ({row.get('status')}): "
                f"{_clip(row.get('summary'), 160)}"
            )
            if not emit(line):
                break
        reports = list(thread.get("reports") or [])
        dropped = dropped or len(reports) > _REPORTS_PER_THREAD
        for row in reports[:_REPORTS_PER_THREAD]:
            line = f"- [report {row.get('state')}] {_clip(row.get('summary'), 200)}"
            if not emit(line):
                break

    context_inputs = list(data.get("context_inputs") or [])
    if context_inputs:
        emit(
            "Context inputs stored for this Ticket (grant no authority):",
            structural=True,
        )
        dropped = dropped or len(context_inputs) > _CONTEXT_INPUTS_MAX
        for row in context_inputs[:_CONTEXT_INPUTS_MAX]:
            line = f"- [input {row.get('input_id')}] {_clip(row.get('text'), 240)}"
            if not emit(line):
                break

    handoffs = list(data.get("handoffs") or [])
    if handoffs:
        emit(
            "Handoffs into this Ticket (state inherited from a previous owner):",
            structural=True,
        )
        for row in handoffs:
            state = dict(row.get("state") or {})
            latest = dict(state.get("latest_report") or {})
            line = (
                f"- [handoff {row.get('handoff_id')}] "
                f"{row.get('thread_key')} from ticket {row.get('from_ticket_id')}: "
                f"{_clip(latest.get('summary'), 200)}"
            )
            if not emit(line):
                break

    if dropped:
        lines.append(_TRUNCATION_LINE)
    return "\n".join(lines)


async def worker_shared_context_text(ticket_id: str) -> str:
    """Launch-prompt shared-context block for a Worker, or "" when it shares nothing."""
    ticket = await ticket_db.get_ticket(ticket_id)
    if ticket is None:
        return ""
    data = await shared_context_for_ticket(ticket)
    if data is None:
        return ""
    return render_shared_context(data)
