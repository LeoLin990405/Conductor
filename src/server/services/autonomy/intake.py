"""Turn Source drafts into durable Triggers and Thread queues.

The Source is responsible for stable external ids; this module owns the
domain rule that a draft can only enter once and that a human comment from an
unlisted account is ignored before it can cost a model call.
"""

from __future__ import annotations

import logging
from typing import Any

from src.server.database.autonomy import tickets as ticket_db
from src.server.database.autonomy import triggers as trigger_db
from src.server.database.autonomy.effects import enqueue_effect
from src.server.services.autonomy.clarify import answer_via_channel
from src.server.services.autonomy.models import EFFECT_DELIVER_INPUT, EFFECT_LAUNCH_WORKER
from src.server.services.autonomy.prompt_refiner import hold_gate
from src.server.services.autonomy.routing import source_mode

logger = logging.getLogger(__name__)


def _still_owns_thread(ticket: dict[str, Any]) -> bool:
    """False once the Ticket can no longer take a follow-up on its Thread.

    A terminal Ticket never returns to running: the signal stays pending for a
    new Eval. That covers the cancelled work and the completed speculative
    plans the speculative line fenced, and every other completed Ticket under
    the lifecycle line's rule.
    """
    if ticket.get("status") in ("completed", "cancelled", "archived"):
        return False
    return not ticket_db.is_ended_speculative_plan(ticket)


def _allowed(config: dict[str, Any], login: str) -> bool:
    allowed = {
        str(item).strip().lower()
        for item in (config.get("allowed_logins") or [])
        if str(item).strip()
    }
    return bool(allowed) and login.lower() in allowed


async def receive_trigger(
    *, source: dict[str, Any], draft: dict[str, Any]
) -> bool:
    """Persist one Source draft; False for a duplicate or unlisted sender.

    A duplicate is a normal retry result, not an error: the unique constraints
    and the caller's stable trigger id make a replayed poll harmless.
    """
    config = dict(source.get("config") or {})
    mode = source_mode(config)
    if mode == "ignore":
        return False
    login = str(draft.get("actor_login") or "")
    if not login or draft.get("actor_is_bot"):
        return False
    if mode == "respond-only-explicit" and not bool(draft.get("addressed")):
        return False
    if not _allowed(config, login):
        return False
    row = await trigger_db.insert_trigger(
        trigger_id=str(draft["trigger_id"]),
        source_id=str(source["source_id"]),
        user_id=str(source["user_id"]),
        external_id=str(draft["external_id"]),
        kind=str(draft["kind"]),
        thread_key=str(draft["thread_key"]),
        actor_login=login,
        actor_is_bot=False,
        addressed=bool(draft.get("addressed")),
        text=str(draft.get("text") or ""),
        url=str(draft.get("url") or ""),
        payload=dict(draft.get("payload") or {}),
    )
    if row is None:
        return False
    await trigger_db.ensure_thread(
        thread_key=str(draft["thread_key"]),
        user_id=str(source["user_id"]),
        source_id=str(source["source_id"]),
        meta={"repository": config.get("repository", "")},
    )
    # A human message on a Thread a Ticket already follows is that Worker's
    # follow-up: it must not create a second Eval or a second Ticket.
    # Cancelled work and a completed speculative plan no longer own the Thread,
    # and under the lifecycle rule no terminal Ticket does: a later signal
    # stays pending for a new Eval instead of reviving the old Ticket. Leaving
    # the plan selected resumes a PTC Worker or stores a daemon input that
    # never launches. Selecting a Ticket and inserting its input are not one
    # critical section: discard can commit in between, so the insert locks the
    # ticket and refuses once it no longer owns the thread; the trigger then
    # stays pending for a new Eval instead of dying on the completed plan.
    # The owner snapshot and every insert it drives run under the Thread
    # routing lock, shared with reassignment: a handoff cannot land between
    # the read and the insert, so the input always targets the current owner.
    # The lock holds one dedicated session connection for the whole loop
    # while add_input(only_if_owning=True) checks out a second pool
    # connection for its FOR UPDATE ownership read — size the pool for two
    # connections per concurrently routed Thread.
    thread_key = str(draft["thread_key"])
    skipped: set[str] = set()
    ticket: dict[str, Any] = {}
    input_row: dict[str, Any] = {}
    async with ticket_db.thread_routing_lock(thread_key):
        while True:
            subscribed = await ticket_db.list_tickets_for_thread(thread_key)
            live = [
                item
                for item in subscribed
                if _still_owns_thread(item)
                and str(item.get("ticket_id") or "") not in skipped
            ]
            if not live:
                break
            # The Ticket that opened this Thread owns the message while its
            # subscription is 'own'; an attached subscriber is only a fallback
            # when no owner is live. A context-only re-follow after a handoff
            # (or an unsubscribe) keeps context semantics: it must not reclaim
            # routing from the Ticket that took the Thread over.
            owned = [
                item
                for item in live
                if item.get("subscription_kind") == "own"
                and str(item.get("origin_thread_key") or "") == thread_key
            ]
            own_subscribers = [
                item
                for item in live
                if item.get("subscription_kind") == "own"
                and str(item.get("origin_thread_key") or "") != thread_key
            ]
            ticket = (owned or own_subscribers or live)[0]
            owned_input = await ticket_db.add_input(
                ticket_id=str(ticket["ticket_id"]),
                trigger_id=str(draft["trigger_id"]),
                kind="trigger",
                text=str(draft.get("text") or ""),
                only_if_owning=True,
            )
            if owned_input and owned_input.get("input_id"):
                input_row = owned_input
                break
            skipped.add(str(ticket["ticket_id"]))
        if not input_row:
            await trigger_db.append_pending_trigger(
                thread_key, str(draft["trigger_id"])
            )
            return True
        if ticket.get("worker_thread_id"):
            await enqueue_effect(
                user_id=str(source["user_id"]),
                kind=EFFECT_DELIVER_INPUT,
                idempotency_key=f"autonomy:input:{input_row['input_id']}",
                payload={
                    "ticket_id": str(ticket["ticket_id"]),
                    "input_id": str(input_row["input_id"]),
                },
            )
        elif ticket.get("status") == "awaiting_clarification":
            # A reply to the gate's question: it becomes the Ticket's
            # confirmed goal and re-queues it, replacing the launch. An
            # empty message carries no answer — leave the Ticket parked.
            answer = str(draft.get("text") or "").strip()
            if answer:
                await answer_via_channel(ticket=ticket, text=answer)
        else:
            # Every Source here is an unattended channel (GitHub, Feishu,
            # Slack, email): nobody is there to confirm a refined rewrite, so
            # a request the refiner would touch is held for confirmation
            # instead of being refined and launched on its own.
            held = await hold_gate(ticket=ticket)
            if held == "passed":
                await enqueue_effect(
                    user_id=str(source["user_id"]),
                    kind=EFFECT_LAUNCH_WORKER,
                    idempotency_key=f"autonomy:launch:{ticket['ticket_id']}",
                    payload={"ticket_id": str(ticket["ticket_id"])},
                )
        # A context-only subscriber receives the message as a stored Input but
        # gets no delivery effect and no authority over the Thread.
        for context_ticket in await ticket_db.context_subscribers(
            thread_key,
            exclude_ticket_id=str(ticket["ticket_id"]),
        ):
            await ticket_db.add_input(
                ticket_id=str(context_ticket["ticket_id"]),
                trigger_id=str(draft["trigger_id"]),
                kind="context",
                text=str(draft.get("text") or ""),
            )
    return True
