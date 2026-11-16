"""Push the next queued item to a Worker after it files a valid report.

A Worker that reports ``done`` while a person's instruction is still queued on
its Ticket has not finished the Ticket: the queued instruction is the next
item. Completing the Ticket would strand it (a terminal Ticket never takes a
follow-up), so the report is filed as ``waiting`` and the queued instruction
is pushed through the ordinary follow-up delivery effect, which admits the
Ticket back to running and opens the Worker's next turn.

The delivery that was reported is not affected: the report's delivery is
measured against the HEAD frozen when the report arrived (see
``delivery_freeze``), so the commits the next item makes do not count towards
it.

A stored idle-wake notice is not a queued item. Once the Worker has reported,
"you have been silent" is no longer true, so it is stamped delivered instead
of being pushed.

Only one item is pushed per report: the next one. The rest stay queued and
the next valid report pushes the next of them.

The follow-up delivery opens a new run on the Worker's thread but does not
rebind the Ticket's ``worker_run_id``, so two things are fenced here with a
mandate record (``report_continuation``) written before the push:

* the run whose report was already filed can still be the thread's latest
  for a moment after the Ticket is admitted back to running; the executor
  recognises its text by digest and does not settle it a second time;
* the delivery state recorded in the mandate belongs to the filed report's
  run, not to the continuation, so the executor does not reuse it: the
  continuation's delivery is unknown and its report goes to a person
  instead of completing on another run's verdict.

Only a valid, non-speculative report that settles ``ready`` with no attention
reason continues. A
report that asks a question, needs review or failed keeps its outcome: a push
would clear the attention a person is meant to answer.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from typing import Any

from src.server.database.autonomy import tickets as ticket_db

logger = logging.getLogger(__name__)

#: The settled state a continuation applies to, and the state it files instead.
CONTINUE_FROM_STATE = "ready"
CONTINUE_AS_STATE = "waiting"
#: The mandate record that fences a pushed continuation.
CONTINUATION_MANDATE_KEY = "report_continuation"


def report_digest(text: str | None) -> str:
    """A stable fingerprint of the report text a run ended with."""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def continuation_marker(
    mandate: Mapping[str, Any] | None, *, run_id: str | None
) -> dict[str, Any] | None:
    """The continuation pushed after ``run_id``'s report, while it is still bound.

    The marker names the run whose report was filed. While the Ticket still
    names that run (nothing rebinds it), what the thread shows next is either
    that same run or the continuation's run reported under its id.
    """
    record = (mandate or {}).get(CONTINUATION_MANDATE_KEY)
    if not isinstance(record, Mapping) or not run_id:
        return None
    if str(record.get("run_id") or "") != run_id:
        return None
    return dict(record)


def _is_idle_wake_notice(text: str) -> bool:
    from src.server.services.autonomy.worker_idle import IDLE_WAKE_NOTICE

    prefix = IDLE_WAKE_NOTICE.split("{minutes}", 1)[0]
    return str(text or "").startswith(prefix)


def report_may_continue(
    *,
    report_state: str,
    has_completion_report: bool,
    attention_reason: str | None,
    mandate: dict[str, Any] | None,
) -> bool:
    """Whether this settled report is one a queued item may continue.

    Only a clean ``ready``: a report that carries any attention reason (a Git
    fact or a completion-check flag) keeps the outcome the Router table gives
    it, because that outcome is what a person is meant to look at.
    """
    mandate = mandate or {}
    if mandate.get("speculative") or mandate.get("read_only"):
        return False
    return (
        has_completion_report
        and report_state == CONTINUE_FROM_STATE
        and attention_reason is None
    )


async def queued_next_items(ticket_id: str) -> list[str] | None:
    """The queued instruction ids to push, oldest first; None when unreadable.

    None is not "nothing queued": completing the Ticket on a failed read would
    strand whatever is queued, so the caller holds the report for the next
    pass instead. Stored idle-wake notices are discarded here, best-effort.
    """
    from src.server.services.autonomy.worker import _pending_instruction_rows

    try:
        rows = await _pending_instruction_rows(ticket_id)
    except Exception:  # noqa: BLE001 - the report waits for the next read
        logger.warning(
            "[autonomy] could not read the queued items ticket=%s",
            ticket_id,
            exc_info=True,
        )
        return None
    queued: list[str] = []
    for row in rows:
        input_id = str(row.get("input_id") or "")
        if not input_id:
            continue
        if _is_idle_wake_notice(str(row.get("text") or "")):
            try:
                await ticket_db.mark_input_delivered(input_id)
            except Exception:  # noqa: BLE001 - a stale notice is not a failure
                logger.warning(
                    "[autonomy] could not discard a stale idle-wake notice "
                    "ticket=%s input=%s",
                    ticket_id,
                    input_id,
                )
            continue
        queued.append(input_id)
    return queued


async def push_queued_items(
    ticket: dict[str, Any],
    input_ids: list[str],
    *,
    run_id: str | None,
    report_text: str | None,
) -> bool:
    """Push the next queued item; whether it was handed to the delivery effect.

    The continuation record is written first, fenced on the Ticket still
    waiting on this line: without it the executor could settle the filed run
    twice or measure the continuation against the filed run's freeze, so a
    record that does not land pushes nothing and the Ticket waits for a
    person.

    The idempotency key names the report's run, so a retried pass does not
    push twice, while an earlier delivery effect for the same input (one that
    found no turn to join) does not swallow this push. The delivery effect
    claims the input before it dispatches, so a duplicate never sends twice.
    """
    from src.server.database.autonomy.effects import enqueue_effect
    from src.server.services.autonomy.models import EFFECT_DELIVER_INPUT

    ticket_id = str(ticket.get("ticket_id") or "")
    if not ticket_id or not input_ids:
        return False
    input_id = input_ids[0]
    recorded = await ticket_db.update_mandate_record(
        ticket_id,
        CONTINUATION_MANDATE_KEY,
        set_fields={
            "run_id": run_id or "",
            "attempt": int(ticket.get("attempts") or 0),
            "input_id": input_id,
            "report_digest": report_digest(report_text),
        },
        expected_status=CONTINUE_AS_STATE,
        expected_thread_id=str(ticket.get("worker_thread_id") or "") or None,
    )
    if recorded is None:
        logger.info(
            "[autonomy] next queued item not pushed; the Ticket moved ticket=%s",
            ticket_id,
        )
        return False
    await enqueue_effect(
        user_id=str(ticket["user_id"]),
        kind=EFFECT_DELIVER_INPUT,
        idempotency_key=f"autonomy:input:{input_id}:after-report:{run_id or 'none'}",
        payload={"ticket_id": ticket_id, "input_id": input_id},
    )
    logger.info(
        "[autonomy] pushed the next queued item after a report ticket=%s input=%s "
        "still_queued=%d",
        ticket_id,
        input_id,
        len(input_ids) - 1,
    )
    return True


__all__ = [
    "CONTINUATION_MANDATE_KEY",
    "CONTINUE_AS_STATE",
    "continuation_marker",
    "report_digest",
    "CONTINUE_FROM_STATE",
    "push_queued_items",
    "queued_next_items",
    "report_may_continue",
]
