"""Keep Ticket inputs durable and prevent unisolated cloud Worker resumes."""

from __future__ import annotations

import logging

from src.server.database.autonomy import tickets as ticket_db
from src.server.services.autonomy.effects import PermanentEffectError
from src.server.services.projects.dispatch import ProjectDispatcher

logger = logging.getLogger(__name__)

_TERMINAL = ("completed", "cancelled", "archived")
_CLOUD_RESUME_QUESTION = (
    "This legacy cloud Worker cannot be resumed without an OS-isolated runtime. "
    "The input is saved for manual review."
)


async def mark_cloud_input_for_review(ticket: dict, input_id: str) -> dict | None:
    """Keep a legacy cloud input durable without dispatching it to an unisolated thread."""
    attention = dict(ticket.get("attention") or {})
    attention.update({"question": _CLOUD_RESUME_QUESTION, "input_id": input_id})
    return await ticket_db.update_ticket(
        str(ticket["ticket_id"]),
        expected_status=str(ticket.get("status") or ""),
        attention=attention,
    )

# Context and policy notices inform a Worker that is already running. They are
# not a human answer, so they must never resume a parked Ticket or clear the
# attention question that is waiting for one.
_INFORMATIONAL_INPUT_KINDS = frozenset({"context", "policy"})


async def deliver_input_effect(effect) -> None:
    input_id = str(effect.payload.get("input_id") or "")
    ticket_id = str(effect.payload.get("ticket_id") or "")
    if not input_id or not ticket_id:
        raise ValueError("input effect is missing ids")
    row = await ticket_db.get_input(input_id)
    ticket = await ticket_db.get_ticket(ticket_id)
    if row is None or ticket is None:
        return
    owner = str(row.get("ticket_id") or "")
    if owner and owner != ticket_id:
        # A reassignment moved this input to another Ticket. That path does
        # not enqueue a delivery: a daemon target pulls, and an unisolated
        # cloud target is refused or stored for review. A stale effect for
        # the previous owner must not attach the text to the wrong Ticket.
        # A row that does not name an owner is still this effect's ticket.
        logger.info(
            "[autonomy] input moved to another ticket; skipping stale delivery input=%s",
            input_id,
        )
        return
    if row.get("delivered_at"):
        return
    if ticket["status"] in _TERMINAL or ticket.get("closed_at"):
        # A terminal ticket never returns to running: the follow-up dies with
        # the ticket instead of resurrecting it. A close stamp is the same,
        # and a completed speculative plan (discarded or dropped) is one of
        # these terminal cases. A Trigger that lost the settlement's re-queue
        # scan (this delivery claimed it first) must still return to its
        # thread's queue: the effect retry after a failed requeue lands here,
        # so heal that trigger before returning. Answers, context and policy
        # are text for a plan that can no longer consume them and are dropped.
        if str(row.get("kind") or "") == "trigger":
            # Ownership check, queue append and delivered stamp commit in one
            # transaction: a retry after a failure cannot queue the Trigger
            # twice or lose it.
            await ticket_db.requeue_terminal_trigger_input(input_id, ticket_id)
            logger.info(
                "[autonomy] terminal ticket trigger re-queued for a new eval "
                "ticket=%s input=%s",
                ticket_id,
                input_id,
            )
        return
    if ticket.get("worker_daemon_id"):
        # Daemon-bound work is always pull-only, including legacy rows that
        # still carry a backend thread id.
        logger.info(
            "[autonomy] daemon ticket input left for pull ticket=%s input=%s",
            ticket_id,
            input_id,
        )
        return
    thread_id = ticket.get("worker_thread_id")
    if not thread_id:
        raise ValueError("ticket has no Worker thread yet")
    # Importing here keeps the worker module from importing this effect at load.
    from src.server.services.autonomy.worker import (
        format_worker_input,
        is_unisolated_cloud_worker,
        refuse_unisolated_cloud_dispatch,
    )

    if is_unisolated_cloud_worker(ticket):
        # A shared cloud runtime may not execute this follow-up. An eligible
        # no shared cloud launch is started; any other input stays durable for review
        # without dispatching it to the unisolated thread.
        refused = await refuse_unisolated_cloud_dispatch(ticket)
        if refused is None:
            await mark_cloud_input_for_review(ticket, input_id)
        raise PermanentEffectError(
            "legacy cloud Worker input saved for manual review; "
            "unisolated resume is disabled"
        )
    text = str(row.get("text") or "")
    informational = str(row.get("kind") or "") in _INFORMATIONAL_INPUT_KINDS
    if informational and (
        str(ticket.get("status") or "") != "running" or ticket.get("attention")
    ):
        # A peer's context message is stored and readable, but only a live
        # Worker may consume it now. Anything parked (blocked, waiting,
        # awaiting clarification, or flagged for attention) stays parked.
        logger.info(
            "[autonomy] informational input left stored ticket=%s input=%s status=%s",
            ticket_id,
            input_id,
            ticket.get("status"),
        )
        return
    prompt = (
        format_worker_input(text, input_id)
        if row.get("kind") in {"trigger", "context"}
        else f"{text}\n\n[session input id: {input_id}]"
    )
    # Claim the input before dispatching: the guarded UPDATE is the ownership
    # fence. It commits only while the input still belongs to this ticket and
    # is undelivered — reassignment moves undelivered inputs only, so a claim
    # that wins keeps the delivery, while one that loses to a handoff matches
    # zero rows and the new owner's own delivery effect carries the input.
    claimed = await ticket_db.claim_input_delivery(input_id, ticket_id)
    if claimed is None:
        logger.info(
            "[autonomy] input already delivered or moved; skipping delivery "
            "ticket=%s input=%s",
            ticket_id,
            input_id,
        )
        return
    if informational:
        # A peer's context message reaches the Worker as a steer into the
        # turn it is already running. It never starts a turn of its own: the
        # thread's admission decides, so a Worker that finished between the
        # Ticket read above and this delivery cannot be resumed by it.
        try:
            receipt = await ProjectDispatcher().dispatch_turn(
                user_id=str(ticket["user_id"]),
                thread_id=str(thread_id),
                workspace_id=str(ticket["workspace_id"]),
                prompt=prompt,
                # The request key carries the owning ticket: a reassigned
                # input dispatches under the new owner's key, so the message
                # door cannot dedupe B's delivery against A's
                # already-executed dispatch.
                request_key=f"autonomy:input:{ticket_id}:{input_id}",
                platform="superagent_worker",
                steer_only=True,
                origin={"type": "agent", "agent": "superagent_peer"},
            )
        except Exception:
            # The steer reached no turn: release the claim so a later
            # delivery attempt can still carry the input.
            await ticket_db.release_input_delivery(
                input_id, ticket_id, claimed["delivered_at"]
            )
            raise
        if receipt.status == "not_running":
            # No turn to steer: the message stays stored and readable, and
            # the Worker is not resumed by it. The claim is released so a
            # delivery against a running turn can still carry it.
            await ticket_db.release_input_delivery(
                input_id, ticket_id, claimed["delivered_at"]
            )
            logger.info(
                "[autonomy] informational input left stored ticket=%s input=%s: "
                "no running turn to steer",
                ticket_id,
                input_id,
            )
            return
        logger.info(
            "[autonomy] context delivered to the running turn ticket=%s input=%s",
            ticket_id,
            input_id,
        )
        return
    # The ticket read above can be stale. Admission takes the same row lock
    # discard uses: a settlement that committed between the read and the
    # UPDATE makes it match nothing, and no dispatch happens. When admission
    # loses that race the claim is NOT released: the settlement's re-queue
    # scan only sees undelivered inputs, so a released claim would sit
    # undelivered on a terminal ticket no retry ever visits. The trigger goes
    # back on the eval queue instead, and the input keeps its delivery mark
    # so nothing double-sends it.
    try:
        admitted = await ticket_db.admit_followup_delivery(ticket_id)
    except Exception:
        # The claim above commits before admission. If admission itself fails,
        # release the claim so the retried effect can still carry the input;
        # otherwise the retry sees a delivered row and drops the follow-up.
        await ticket_db.release_input_delivery(
            input_id, ticket_id, claimed["delivered_at"]
        )
        raise
    if admitted is None:
        try:
            requeued = await ticket_db.requeue_input_trigger(input_id)
        except Exception:
            # Requeue can fail transiently after the claim committed. Release
            # the claim so the retried effect re-claims, loses admission again
            # and retries the requeue; otherwise the retry would exit at the
            # delivered-row check and silently drop the trigger.
            await ticket_db.release_input_delivery(
                input_id, ticket_id, claimed["delivered_at"]
            )
            raise
        logger.info(
            "[autonomy] follow-up not admitted; trigger %s ticket=%s input=%s",
            "re-queued for a new eval" if requeued else "absent (input kept)",
            ticket_id,
            input_id,
        )
        return
    try:
        await ProjectDispatcher().dispatch_turn(
            user_id=str(admitted["user_id"]),
            thread_id=str(admitted.get("worker_thread_id") or thread_id),
            workspace_id=str(admitted["workspace_id"]),
            prompt=prompt,
            # The request key carries the owning ticket: a reassigned input
            # dispatches under the new owner's key, so the message door cannot
            # dedupe B's delivery against A's already-executed dispatch.
            request_key=f"autonomy:input:{ticket_id}:{input_id}",
            platform="superagent_worker",
        )
        logger.info(
            "[autonomy] follow-up delivered ticket=%s input=%s", ticket_id, input_id
        )
    except Exception:
        # Admission moved a non-running ticket to running for this dispatch; put
        # that status back so the ticket neither blocks a discard nor pretends to
        # run until the watchdog notices. The rollback is fenced to the revision
        # this delivery's own admission produced: a newer admission (or any
        # ticket write) advances the revision, and this restore must not park a
        # ticket another delivery already moved. Restore BEFORE releasing the
        # claim, because release_input_delivery bumps the ticket revision and a
        # restore after it would miss its own fence.
        prior_status = str(admitted.get("prior_status") or "")
        if prior_status and prior_status != "running":
            try:
                await ticket_db.update_ticket(
                    ticket_id,
                    expected_status="running",
                    expected_revision=admitted.get("revision"),
                    status=prior_status,
                )
            except Exception:
                logger.exception(
                    "[autonomy] failed to restore ticket status after dispatch "
                    "failure ticket=%s",
                    ticket_id,
                )
        # The claim fenced the retry: release it so the re-run actually
        # dispatches (a crash after a successful dispatch is deduped by the
        # request key, which stays stable across the retry).
        await ticket_db.release_input_delivery(
            input_id, ticket_id, claimed["delivered_at"]
        )
        raise
    settled = await ticket_db.get_ticket(ticket_id)
    if settled is None or str(settled.get("status") or "") in _TERMINAL:
        # The ticket settled (discard or any terminal transition) while the
        # dispatch was in flight. The prompt is on the worker thread; the
        # status is whatever the settlement wrote. Read-only probe: a write
        # here could clobber a worker report that landed mid-dispatch.
        logger.info(
            "[autonomy] ticket ended before delivery settled; not reviving "
            "ticket=%s input=%s",
            ticket_id,
            input_id,
        )
        return
