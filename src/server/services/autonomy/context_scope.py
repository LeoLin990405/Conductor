"""Explicit shared versus isolated context boundaries.

Every Ticket names a context scope. ``isolated`` means the Ticket's own Inputs
and Thread, and nothing about a sibling Ticket, are visible. ``shared`` is an
opt-in record with a small data policy: which data classes cross, whether A2A
is allowed, whether another account may join, and how many Tickets participate.

Sharing is never inferred from a thread or workspace. A caller must name a
shared scope before a Ticket can read a peer's rows or follow a peer Thread.
"""

from __future__ import annotations

from typing import Any, Iterable

from src.server.database.autonomy import context_scopes as scope_db
from src.server.models.autonomy import ContextScopePolicy


class ContextScopeError(ValueError):
    """A requested share is not allowed by the scope or its policy."""


def _policy_dict(policy: Any) -> dict[str, Any]:
    if isinstance(policy, ContextScopePolicy):
        return policy.model_dump()
    return ContextScopePolicy.model_validate(policy or {}).model_dump()


def validate_policy(kind: str, policy: Any) -> dict[str, Any]:
    """Canonicalize a scope policy and reject combinations the kind forbids."""
    if kind not in {"isolated", "shared"}:
        raise ContextScopeError("scope kind must be isolated or shared")
    raw = dict(policy or {})
    if kind == "isolated":
        raw.setdefault("max_participants", 1)
        raw.setdefault("allow_a2a", False)
        raw.setdefault("allow_cross_user", False)
    try:
        normalized = _policy_dict(raw)
    except ValueError as exc:
        raise ContextScopeError(f"invalid context scope policy: {exc}") from exc
    if kind == "isolated":
        if normalized["allow_a2a"] or normalized["allow_cross_user"]:
            raise ContextScopeError("an isolated scope cannot allow sharing")
        if normalized["max_participants"] != 1:
            raise ContextScopeError("an isolated scope has exactly one participant")
    return normalized


def policy_of(scope: dict[str, Any]) -> dict[str, Any]:
    return validate_policy(str(scope.get("kind") or "isolated"), scope.get("policy"))


def allows(scope: dict[str, Any], data_class: str) -> bool:
    return data_class in policy_of(scope).get("data_classes", [])


async def ensure_scope(ticket: dict[str, Any]) -> dict[str, Any]:
    """Return the Ticket's scope, creating its isolated scope when missing."""
    scope_id = str(ticket.get("context_scope_id") or "")
    if scope_id:
        scope = await scope_db.get_scope(scope_id)
        if scope is None:
            raise ContextScopeError("ticket context scope no longer exists")
        return scope
    ticket_id = str(ticket.get("ticket_id") or "")
    user_id = str(ticket.get("user_id") or "")
    if not ticket_id or not user_id:
        raise ContextScopeError("ticket id and user are required to resolve a scope")
    return await scope_db.create_isolated_scope(user_id=user_id, ticket_id=ticket_id)


async def lookup_scope(ticket: dict[str, Any]) -> dict[str, Any] | None:
    """The Ticket's current scope, or None; a read never creates one.

    ``ensure_scope`` backfills an isolated scope for a legacy Ticket, which is
    right before a share decision but wrong on a read path: a GET that lists
    messages must not write. A Ticket with no scope reads as isolated.
    """
    scope_id = str(ticket.get("context_scope_id") or "")
    if not scope_id:
        return None
    return await scope_db.get_scope(scope_id)


def can_share(ticket: dict[str, Any], other: dict[str, Any]) -> bool:
    """True only when both Tickets name the same shared scope and user mapping."""
    return bool(ticket.get("context_scope_id")) and ticket.get(
        "context_scope_id"
    ) == other.get("context_scope_id")


async def assert_can_share(
    ticket: dict[str, Any],
    other: dict[str, Any],
    *,
    scope: dict[str, Any] | None = None,
    data_class: str = "tickets",
) -> dict[str, Any]:
    scope = scope or await ensure_scope(ticket)
    if str(scope.get("kind")) != "shared":
        raise ContextScopeError("the ticket's context scope is isolated")
    if not allows(scope, data_class):
        raise ContextScopeError(f"the scope does not share {data_class}")
    other_scope_id = str(other.get("context_scope_id") or "")
    if other_scope_id != str(scope.get("scope_id")):
        raise ContextScopeError("the tickets are in different context scopes")
    other_user = str(other.get("user_id") or "")
    if other_user and other_user != str(ticket.get("user_id") or ""):
        if not policy_of(scope).get("allow_cross_user"):
            raise ContextScopeError("the scope does not allow cross-user sharing")
        # Both directions are mapped: the owner is authorized by ownership and
        # a peer by the explicit list, so a peer can answer the owner too.
        mapped = {str(scope.get("user_id") or "")} | {
            str(user) for user in scope.get("shared_with_user_ids") or []
        }
        if other_user not in mapped:
            raise ContextScopeError("the other user is not mapped into this scope")
    return scope


async def assert_can_follow_thread(
    ticket: dict[str, Any],
    thread_key: str,
    *,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A context subscription needs a peer Ticket on the Thread inside the scope."""
    scope = scope or await ensure_scope(ticket)
    ticket_id = str(ticket.get("ticket_id") or "")
    if thread_key == str(ticket.get("origin_thread_key") or ""):
        return scope
    if str(scope.get("kind")) != "shared":
        raise ContextScopeError("context-only Threads require a shared scope")
    if not allows(scope, "triggers"):
        raise ContextScopeError("the scope does not share Thread triggers")
    peer_ids = set(await scope_db.list_scope_ticket_ids(str(scope["scope_id"])))
    peer_ids.discard(ticket_id)
    subscribed = set(await scope_db.ticket_ids_for_thread(thread_key))
    if not (peer_ids & subscribed):
        raise ContextScopeError(
            "no Ticket in this shared scope is a member of that Thread"
        )
    return scope


async def assert_can_read_thread(
    ticket: dict[str, Any],
    thread_key: str,
    *,
    kind: str,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if kind == "own" or thread_key == str(ticket.get("origin_thread_key") or ""):
        return scope or await ensure_scope(ticket)
    return await assert_can_follow_thread(ticket, thread_key, scope=scope)


async def scope_member_ids(
    ticket: dict[str, Any], *, scope: dict[str, Any] | None = None
) -> set[str]:
    """Every Ticket in the scope, including this one.

    Membership is not data access: use ``readable_ticket_ids`` to decide which
    rows may cross. A shared scope's member count is policy-independent.
    """
    ticket_id = str(ticket.get("ticket_id") or "")
    members = {ticket_id} if ticket_id else set()
    scope = scope or await ensure_scope(ticket)
    if str(scope.get("kind")) == "shared":
        members.update(await scope_db.list_scope_ticket_ids(str(scope["scope_id"])))
    return members


async def readable_ticket_ids(
    ticket: dict[str, Any],
    *,
    scope: dict[str, Any] | None = None,
    data_class: str = "tickets",
) -> set[str]:
    """Ticket ids whose rows of ``data_class`` this Ticket may read.

    Self is always readable. A shared scope adds its members only when that
    exact data class is published; an isolated scope adds nobody. Tickets and
    Reports are separate classes, so allowing one never exposes the other.
    """
    ticket_id = str(ticket.get("ticket_id") or "")
    visible = {ticket_id} if ticket_id else set()
    scope = scope or await ensure_scope(ticket)
    if str(scope.get("kind")) == "shared" and allows(scope, data_class):
        visible.update(await scope_db.list_scope_ticket_ids(str(scope["scope_id"])))
    return visible


def filter_rows(
    rows: Iterable[dict[str, Any]], visible: set[str]
) -> list[dict[str, Any]]:
    """Keep only rows authored by a visible Ticket; rows without an id drop."""
    return [row for row in rows if str(row.get("ticket_id") or "") in visible]


def hide_unpublished_report_text(
    rows: Iterable[dict[str, Any]], *, scope: dict[str, Any]
) -> list[dict[str, Any]]:
    """Blank a Ticket's report text when Reports are not a published class.

    A Worker report copies its summary onto the Ticket row, and a blocked or
    failed report also copies report text into the attention question, so
    publishing Ticket rows alone would otherwise leak report text past the
    ``reports`` data class. Ticket metadata stays visible, including the fact
    that the Ticket waits for a person; only the report-derived text is
    withheld.
    """
    if allows(scope, "reports"):
        return [dict(row) for row in rows]
    return [_hide_report_text(row) for row in rows]


def _hide_report_text(row: dict[str, Any]) -> dict[str, Any]:
    """One Ticket row without the text a report put on it.

    A blocked or failed report puts its own question, or its summary, in the
    Ticket's attention slot, and the slot does not say which text it carries.
    The question is therefore blanked too; the slot itself stays, so a peer
    still sees that the Ticket waits for a person.
    """
    hidden = {**row, "summary": ""}
    attention = row.get("attention")
    if isinstance(attention, dict):
        hidden["attention"] = {**attention, "question": ""}
    return hidden
