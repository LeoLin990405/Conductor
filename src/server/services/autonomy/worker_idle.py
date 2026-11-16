"""Wake a Worker that stopped moving: no commit, no completion report.

A Worker can hang: its turn is still open, but it has neither committed
anything nor filed a completion report. The Ticket watchdog notices a stalled
Ticket after two hours and flags it for a person. This sweep notices sooner
and wakes the Worker itself, so the work continues instead of waiting for a
human.

The switch is ``CONDUCTOR_WORKER_IDLE_WAKE_MINUTES``: the minutes of silence
after which a Worker is woken. It is off by default, because a wake is a
message to a model that may not need one.

The state (when a Worker was last woken, and the notice it is still owed)
lives in the Ticket's mandate, which is JSON, so no migration is needed.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.server.database.autonomy import tickets as ticket_db

logger = logging.getLogger(__name__)

#: The environment switch: the minutes of silence after which a Worker is woken.
IDLE_WAKE_ENV = "CONDUCTOR_WORKER_IDLE_WAKE_MINUTES"
#: The mandate key that holds the Ticket's idle-wake state.
IDLE_WAKE_MANDATE_KEY = "worker_idle_wake"
#: The version of the record written to the mandate.
IDLE_WAKE_VERSION = 1
#: The notice a woken Worker receives.
IDLE_WAKE_NOTICE = (
    "Scheduler notice: this Ticket's Worker has been silent for {minutes} "
    "minutes (no commit and no completion report were seen). If you are still "
    "working, continue; if you are stuck, say what blocks you so a person can "
    "look."
)
#: The report states that settle an attempt. A progress heartbeat is not one:
#: it leaves the Ticket running, so it is the report of the run that is
#: silent now, and it says nothing about the work being done.
COMPLETION_REPORT_STATES = frozenset(
    {"ready", "blocked", "failed", "dropped", "waiting"}
)
#: How many candidate rows one page of the sweep reads. The sweep walks the
#: following pages, so a page of Workers that are still active cannot hide
#: an idle Worker that sorts after them.
_CANDIDATE_PAGE = 50


@dataclass(frozen=True, slots=True)
class IdleWakeState:
    """What the Ticket remembers about its last wake."""

    last_wake_at: datetime | None = None
    wakes: int = 0
    #: The input that carries a wake the Worker has not received yet.
    pending_input_id: str = ""


@dataclass(frozen=True, slots=True)
class IdleWakeDecision:
    """Whether a Worker is woken, and why."""

    wake: bool
    reason: str
    idle_seconds: float


@dataclass(frozen=True, slots=True)
class IdleWakeSweep:
    """What one sweep did.

    The woken Tickets are named so a caller can see who was reached. The
    watchdog's stall and dead-run handling does not consult this set: a wake
    is not a reason to skip either one.
    """

    woken: int = 0
    tickets: frozenset[str] = field(default_factory=frozenset)


def idle_wake_minutes(environ: Any | None = None) -> int | None:
    """The minutes of silence after which a Worker is woken.

    None when the switch is off: unset, empty, not a number, or not a
    positive number of minutes. The switch is read from the environment
    unless a mapping is passed in.
    """
    source = os.environ if environ is None else environ
    raw = source.get(IDLE_WAKE_ENV)
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        minutes = int(text)
    except ValueError:
        return None
    return minutes if minutes > 0 else None


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def read_idle_state(mandate: Any) -> IdleWakeState:
    """The idle-wake state a mandate carries, or an empty one."""
    if not isinstance(mandate, dict):
        return IdleWakeState()
    record = mandate.get(IDLE_WAKE_MANDATE_KEY)
    if not isinstance(record, dict) or record.get("version") != IDLE_WAKE_VERSION:
        return IdleWakeState()
    wakes = record.get("wakes")
    pending = record.get("pending_input_id")
    return IdleWakeState(
        last_wake_at=_as_datetime(record.get("last_wake_at")),
        wakes=int(wakes) if isinstance(wakes, int) and wakes >= 0 else 0,
        pending_input_id=str(pending) if isinstance(pending, str) else "",
    )


def idle_state_fragment(
    *, last_wake_at: datetime, wakes: int, pending_input_id: str = ""
) -> dict[str, Any]:
    """The mandate patch that records a wake.

    ``pending_input_id`` is the delivery obligation: the notice exists, and
    the Worker has not received it yet. A sweep that finds it retries the
    delivery instead of spending another window.
    """
    record: dict[str, Any] = {
        "version": IDLE_WAKE_VERSION,
        "last_wake_at": last_wake_at.isoformat(),
        "wakes": max(0, int(wakes)),
    }
    if pending_input_id:
        record["pending_input_id"] = pending_input_id
    return {IDLE_WAKE_MANDATE_KEY: record}


def decide_idle_wake(
    *,
    threshold_minutes: int | None,
    idle_seconds: float,
    has_commit: bool | None,
    has_report: bool,
    last_wake_at: datetime | None = None,
    now: datetime | None = None,
) -> IdleWakeDecision:
    """Whether a Worker that has gone quiet is woken.

    A Worker is woken only when the silence is established: no completion
    report for at least the threshold, and no wake inside the same window (a
    Worker that was just woken gets the chance to answer).

    ``idle_seconds`` is the silence since the last thing the Worker did, so
    a Worker that committed and then went still is silent again once the
    window passes. ``has_commit`` says whether the current run committed at
    all; it is not a veto, only the reason a short silence is not a wake.
    None means the state cannot be read (a daemon owns its checkout on
    another host, or the baseline is gone). That is not a reason to stay
    silent: the report is the other half of the signal, and a notice that
    asks a stuck Worker to say what blocks it costs one message.
    """
    if threshold_minutes is None or threshold_minutes <= 0:
        return IdleWakeDecision(False, "disabled", idle_seconds)
    if has_report:
        return IdleWakeDecision(False, "reported", idle_seconds)
    threshold = threshold_minutes * 60
    if idle_seconds < threshold:
        reason = "committed" if has_commit is True else "not_yet_idle"
        return IdleWakeDecision(False, reason, idle_seconds)
    moment = now or datetime.now(timezone.utc)
    if last_wake_at is not None:
        if last_wake_at.tzinfo is None:
            last_wake_at = last_wake_at.replace(tzinfo=timezone.utc)
        if (moment - last_wake_at).total_seconds() < threshold:
            return IdleWakeDecision(False, "recently_woken", idle_seconds)
    return IdleWakeDecision(True, "idle", idle_seconds)


def _current_run_baseline(ticket: dict[str, Any]) -> Any:
    """The baseline the Worker's *current* run started from, or None.

    The Ticket keeps one baseline binding, and the binding names the thread
    and the attempt it belongs to. A binding from an earlier attempt says
    nothing about this one: a commit that earlier attempt made would then
    read as this run's work and silence the wake forever.

    The Ticket's delivery origin is deliberately not applied here. That
    origin makes a commit count for the whole Ticket, which is what the
    completion contract needs; the idle wake asks a narrower question: did
    *this* run commit anything?
    """
    from src.server.services.autonomy.git_delivery import GitSnapshot

    mandate = dict(ticket.get("mandate") or {})
    binding = mandate.get("git_delivery_baseline")
    if not isinstance(binding, dict) or binding.get("version") != 2:
        return None
    thread_id = str(ticket.get("worker_thread_id") or "")
    if thread_id and str(binding.get("thread_id") or "") != thread_id:
        return None
    run_id = str(ticket.get("worker_run_id") or "")
    bound_run = str(binding.get("run_id") or "")
    if bound_run and run_id and bound_run != run_id:
        return None
    attempt = binding.get("attempt")
    if isinstance(attempt, int) and attempt != int(ticket.get("attempts") or 0):
        # The binding belongs to an earlier attempt of this Ticket.
        return None
    return GitSnapshot.from_record(binding.get("snapshot"))


def _worker_has_commit(ticket: dict[str, Any]) -> bool | None:
    """Whether the Worker's current run committed anything; None when unknowable.

    The verdict comes from the delivery check, so "no commit" means the same
    thing here as it does in the completion contract. A checkout or a
    baseline that cannot be read leaves the question open.
    """
    from src.server.services.autonomy.git_delivery import safe_git_commit_delivery
    from src.server.services.autonomy.ticket_worktree import server_worktree_cwd

    cwd = server_worktree_cwd(ticket)
    if not cwd:
        return None
    baseline = _current_run_baseline(ticket)
    if baseline is None:
        return None
    verdict = safe_git_commit_delivery(cwd, baseline)
    if verdict == "delivered":
        return True
    if verdict == "missing":
        return False
    return None


def _worker_last_commit_at(ticket: dict[str, Any]) -> datetime | None:
    """When the Worker's checkout last moved, or None when it cannot be read."""
    from src.server.services.autonomy.git_delivery import safe_git_head_commit_time
    from src.server.services.autonomy.ticket_worktree import server_worktree_cwd

    cwd = server_worktree_cwd(ticket)
    if not cwd:
        return None
    return safe_git_head_commit_time(cwd)


async def _worker_silence(
    ticket: dict[str, Any], *, now: datetime
) -> tuple[float, bool | None]:
    """How long the Worker's current attempt has been silent, and whether it committed.

    The silence is measured from the later of the attempt's start and the
    branch's last commit, so a commit an earlier attempt made does not count
    as this attempt's silence. The
    attempt starts when its run ledger row was created. The Ticket's
    ``started_at`` is kept across a resume, so measuring from it would wake
    a fresh attempt immediately. A Worker that committed and then went still
    is silent again once the window passes, measured from the commit.

    When the attempt's start cannot be read and there is no commit, the
    silence is zero: the Ticket clock is not a substitute.
    """
    committed = await asyncio.to_thread(_worker_has_commit, ticket)
    committed_at = None
    if committed is True:
        committed_at = await asyncio.to_thread(_worker_last_commit_at, ticket)
        if committed_at is not None and committed_at.tzinfo is None:
            committed_at = committed_at.replace(tzinfo=timezone.utc)
    started = await _attempt_started_at(ticket)
    if started is not None and started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    # The later of the two: a commit an earlier attempt made does not make a
    # resumed attempt look silent since that commit.
    marks = [mark for mark in (committed_at, started) if mark is not None]
    if not marks:
        return 0.0, committed
    seconds = (now - max(marks)).total_seconds()
    return (seconds if seconds > 0 else 0.0), committed


async def _attempt_started_at(ticket: dict[str, Any]) -> datetime | None:
    """When the run that owns the Ticket now began, or None when it is unknown.

    The run ledger row is written once and never moves, so it is the one
    stable marker of an attempt. The Ticket's ``updated_at`` is not: every
    write to the Ticket moves it, so a report that belongs to this attempt
    stops looking like it does after a heartbeat, and a report of an earlier
    attempt starts looking like it does.
    """
    run_id = str(ticket.get("worker_run_id") or "")
    if not run_id:
        return None
    from src.server.database.runs import lifecycle as runs_db

    try:
        started = await runs_db.get_run_started_at(run_id)
    except Exception:  # noqa: BLE001 - an unreadable ledger leaves it unknown
        logger.warning(
            "[autonomy] idle wake could not read the run ledger ticket=%s",
            ticket.get("ticket_id"),
            exc_info=True,
        )
        return None
    return started


async def _completion_report_for_attempt(
    ticket: dict[str, Any], report: Any
) -> bool:
    """Whether the report settles the run that owns the Ticket now.

    A progress heartbeat is not a completion: it leaves the Ticket running,
    so it is the report of the run that is silent now, and it says nothing
    about the work being done. A completion report moves the Ticket out of
    ``running``, so a Ticket that is running again holds the report of an
    earlier attempt, and that report says nothing about the run that is
    silent now either.
    """
    if not isinstance(report, dict):
        return False
    if str(report.get("state") or "") not in COMPLETION_REPORT_STATES:
        return False
    attempt_started_at = await _attempt_started_at(ticket)
    if attempt_started_at is None:
        # The attempt cannot be identified, so the report cannot be this
        # attempt's completion: it must not silence the wake.
        return False
    created = _as_datetime(report.get("created_at"))
    if created is None:
        return False
    if attempt_started_at.tzinfo is None:
        attempt_started_at = attempt_started_at.replace(tzinfo=timezone.utc)
    return created >= attempt_started_at


async def _record_wake(
    ticket_id: str,
    ticket: dict[str, Any],
    state: IdleWakeState,
    *,
    input_id: str,
    now: datetime,
    expected_revision: int,
) -> dict[str, Any] | None:
    """Record the wake and the notice it owes on the Ticket's mandate.

    The record comes with the obligation: a sweep that repeats a wake every
    pass would be noise, and a notice that could not be delivered must be
    owed rather than lost.

    ``expected_revision`` is the Ticket revision after the notice row was
    inserted. That insert increments the revision, so the revision the sweep
    read no longer matches and must not be the fence.
    """
    record = idle_state_fragment(
        last_wake_at=now,
        wakes=state.wakes + 1,
        pending_input_id=input_id,
    )[IDLE_WAKE_MANDATE_KEY]
    return await ticket_db.update_mandate_record(
        ticket_id,
        IDLE_WAKE_MANDATE_KEY,
        set_fields=record,
        expected_status="running",
        expected_thread_id=str(ticket.get("worker_thread_id") or "") or None,
        expected_revision=expected_revision,
    )


async def _settle_pending_wake(
    ticket_id: str,
    ticket: dict[str, Any],
    *,
    expected_revision: int | None,
) -> dict[str, Any] | None:
    """Drop the delivery obligation, fenced on the revision after the stamp.

    ``mark_input_delivered`` increments the revision. Settling on the
    revision from before that stamp refuses the write and leaves the
    obligation in place. When the stamp did not report a revision, the
    settlement is skipped rather than fenced on a stale snapshot.
    """
    if not isinstance(expected_revision, int):
        return None
    return await ticket_db.update_mandate_record(
        ticket_id,
        IDLE_WAKE_MANDATE_KEY,
        remove_fields=("pending_input_id",),
        expected_status="running",
        expected_thread_id=str(ticket.get("worker_thread_id") or "") or None,
        expected_revision=expected_revision,
    )


async def _deliver_wake(
    ticket: dict[str, Any], *, input_id: str, minutes: int, now: datetime
) -> tuple[str, int | None]:
    """Hand the notice to the running turn; the outcome and the post-stamp revision.

    The running turn is steered when the Ticket has a thread, including a
    daemon or an isolated cloud Worker. A wake never starts a turn. When no
    running turn accepts the steer, the notice stays stored. That is not a
    wake: the Worker that is running did not receive it.

    The second value is the Ticket revision after ``mark_input_delivered``,
    and only when a turn actually carries the notice. The stamp increments
    the revision, so a settlement fenced on an earlier snapshot would refuse.
    """
    from src.server.services.autonomy.worker import deliver_worker_turn

    ticket_id = str(ticket.get("ticket_id") or "")
    notice = IDLE_WAKE_NOTICE.format(minutes=minutes)
    try:
        delivery = await deliver_worker_turn(
            ticket,
            prompt=notice,
            # The key names the notice, not the attempt: a retried delivery
            # is the same message, and the message door dedupes it.
            request_key=f"autonomy:idle-wake:{ticket_id}:{input_id}",
            input_id=input_id,
        )
    except Exception:  # noqa: BLE001 - the obligation is retried, not lost
        logger.exception("[autonomy] idle wake delivery failed ticket=%s", ticket_id)
        return "failed", None
    if delivery.outcome != "dispatched":
        return delivery.outcome, None
    # The turn carries the notice, so the stored copy must not be read
    # a second time on the Worker's next turn.
    try:
        revised = await ticket_db.mark_input_delivered(input_id)
    except Exception:  # noqa: BLE001 - a duplicate notice is not a failure
        logger.warning(
            "[autonomy] idle wake could not mark its notice delivered ticket=%s",
            ticket_id,
        )
        revised = None
    return "dispatched", revised if isinstance(revised, int) else None


async def _cancel_obsolete_notice(
    ticket_id: str, ticket: dict[str, Any], input_id: str
) -> None:
    """The pending notice is no longer true; do not deliver it on a later claim.

    The input is stamped delivered so a daemon's next claim does not read a
    "you have been silent" notice after the Worker has moved. The stamp
    moves the revision, and the obligation is cleared on that new revision.
    """
    try:
        revised = await ticket_db.mark_input_delivered(input_id)
    except Exception:  # noqa: BLE001 - the next sweep retries the cancel
        logger.warning(
            "[autonomy] idle wake could not cancel its notice ticket=%s",
            ticket_id,
        )
        revised = None
    await _settle_pending_wake(
        ticket_id,
        ticket,
        expected_revision=revised if isinstance(revised, int) else None,
    )


async def _discard_unowed_notice(ticket_id: str, input_id: str) -> None:
    """Stamp a notice no wake record owes, so no later turn reads it.

    The notice was stored before the record that owes it. When that record
    is refused, nothing tracks the row: left undelivered, every refused sweep
    would store one more copy for the Worker's next turn to read.
    """
    try:
        await ticket_db.mark_input_delivered(input_id)
    except Exception:  # noqa: BLE001 - a stray notice is not a failure
        logger.warning(
            "[autonomy] idle wake could not discard its unowed notice ticket=%s",
            ticket_id,
        )


async def _wake_if_idle(
    ticket: dict[str, Any], *, threshold_minutes: int, now: datetime
) -> bool:
    """Wake one Ticket's Worker when it has gone quiet."""
    ticket_id = str(ticket.get("ticket_id") or "")
    if not ticket_id:
        return False
    mandate = dict(ticket.get("mandate") or {})
    if mandate.get("speculative") or mandate.get("read_only"):
        # A plan run delivers nothing: its silence is what it looks like.
        return False
    state = read_idle_state(mandate)

    report = await ticket_db.latest_report(ticket_id)
    has_report = await _completion_report_for_attempt(ticket, report)
    idle_seconds, has_commit = await _worker_silence(ticket, now=now)

    if state.pending_input_id:
        # The notice is already owed. Retry it only while the attempt is
        # still silent. last_wake_at sits inside the window by construction
        # and is not activity: a commit or a completion report is. Either
        # one cancels the notice instead of dispatching it.
        decision = decide_idle_wake(
            threshold_minutes=threshold_minutes,
            idle_seconds=idle_seconds,
            has_commit=has_commit,
            has_report=has_report,
            last_wake_at=None,
            now=now,
        )
        if not decision.wake:
            await _cancel_obsolete_notice(
                ticket_id, ticket, state.pending_input_id
            )
            logger.info(
                "[autonomy] idle wake notice cancelled ticket=%s reason=%s",
                ticket_id,
                decision.reason,
            )
            return False
        outcome, marked = await _deliver_wake(
            ticket,
            input_id=state.pending_input_id,
            minutes=threshold_minutes,
            now=now,
        )
        if outcome == "failed":
            return False
        settle_revision = (
            marked if outcome == "dispatched" else ticket.get("revision")
        )
        await _settle_pending_wake(
            ticket_id,
            ticket,
            expected_revision=settle_revision
            if isinstance(settle_revision, int)
            else None,
        )
        if outcome != "dispatched":
            logger.info(
                "[autonomy] idle wake notice stored, not delivered ticket=%s",
                ticket_id,
            )
            return False
        logger.warning("[autonomy] idle worker woken on retry ticket=%s", ticket_id)
        return True

    decision = decide_idle_wake(
        threshold_minutes=threshold_minutes,
        idle_seconds=idle_seconds,
        has_commit=has_commit,
        has_report=has_report,
        last_wake_at=state.last_wake_at,
        now=now,
    )
    if not decision.wake:
        logger.info(
            "[autonomy] idle worker not woken ticket=%s reason=%s idle_s=%s",
            ticket_id,
            decision.reason,
            decision.idle_seconds,
        )
        return False

    notice = IDLE_WAKE_NOTICE.format(minutes=threshold_minutes)
    stored = await ticket_db.add_input(
        ticket_id=ticket_id, trigger_id=None, kind="instruction", text=notice
    )
    input_id = str((stored or {}).get("input_id") or "")
    stored_revision = (stored or {}).get("ticket_revision")
    if not input_id:
        # The notice could not be stored: the window is not consumed, so the
        # next sweep starts over rather than owing a notice that is not there.
        logger.warning(
            "[autonomy] idle wake could not store its notice ticket=%s", ticket_id
        )
        return False
    if not isinstance(stored_revision, int):
        # Without the post-insert revision the mandate fence would use the
        # snapshot from before the insert and refuse the wake, leaving the
        # notice undelivered.
        logger.warning(
            "[autonomy] idle wake could not read the revision its notice "
            "moved ticket=%s",
            ticket_id,
        )
        await _discard_unowed_notice(ticket_id, input_id)
        return False
    recorded = await _record_wake(
        ticket_id,
        ticket,
        state,
        input_id=input_id,
        now=now,
        expected_revision=stored_revision,
    )
    if recorded is None:
        # The record is the fence against a repeated wake: without it the
        # Worker is left alone rather than woken on every sweep.
        logger.info(
            "[autonomy] idle wake skipped; the ticket changed ticket=%s", ticket_id
        )
        await _discard_unowed_notice(ticket_id, input_id)
        return False
    outcome, marked = await _deliver_wake(
        ticket, input_id=input_id, minutes=threshold_minutes, now=now
    )
    if outcome == "failed":
        # The obligation stays on the Ticket: the next sweep retries the
        # delivery instead of the window passing in silence.
        logger.warning(
            "[autonomy] idle wake owed; the delivery failed ticket=%s", ticket_id
        )
        return False
    if outcome == "dispatched":
        settle_revision = marked
    else:
        settle_revision = recorded.get("revision")
    await _settle_pending_wake(
        ticket_id,
        ticket,
        expected_revision=settle_revision if isinstance(settle_revision, int) else None,
    )
    if outcome != "dispatched":
        # The Worker did not receive the notice now: the notice waits for
        # its next turn. This is not a wake.
        logger.info(
            "[autonomy] idle wake notice stored, not delivered ticket=%s",
            ticket_id,
        )
        return False
    logger.warning(
        "[autonomy] idle worker woken ticket=%s idle_s=%s threshold_minutes=%s",
        ticket_id,
        decision.idle_seconds,
        threshold_minutes,
    )
    return True


async def sweep_worker_idle_wakes(
    *,
    minutes: int | None = None,
    now: datetime | None = None,
    limit: int = 50,
) -> IdleWakeSweep:
    """Wake the Workers that stopped moving.

    The switch decides whether the sweep does anything at all: with it off,
    no Worker is woken and no ticket is read. Candidates are paged. A page
    of Workers that are still active does not end the sweep: the next page
    is read in the same pass, so a later idle Worker is not starved.
    """
    threshold = idle_wake_minutes() if minutes is None else minutes
    if threshold is None or threshold <= 0:
        return IdleWakeSweep()
    moment = now or datetime.now(timezone.utc)
    woken: set[str] = set()
    offset = 0
    while len(woken) < limit:
        rows = await ticket_db.list_worker_idle_candidates(
            idle_seconds=threshold * 60,
            limit=_CANDIDATE_PAGE,
            offset=offset,
        )
        if not rows:
            break
        for row in rows:
            if len(woken) >= limit:
                break
            try:
                if await _wake_if_idle(row, threshold_minutes=threshold, now=moment):
                    woken.add(str(row.get("ticket_id") or ""))
            except Exception:  # noqa: BLE001 - one Worker must not stop the sweep
                logger.exception(
                    "[autonomy] idle wake failed ticket=%s", row.get("ticket_id")
                )
        if len(rows) < _CANDIDATE_PAGE:
            break
        offset += len(rows)
    return IdleWakeSweep(woken=len(woken), tickets=frozenset(woken))


__all__ = [
    "COMPLETION_REPORT_STATES",
    "IDLE_WAKE_ENV",
    "IDLE_WAKE_MANDATE_KEY",
    "IDLE_WAKE_NOTICE",
    "IDLE_WAKE_VERSION",
    "IdleWakeDecision",
    "IdleWakeState",
    "IdleWakeSweep",
    "decide_idle_wake",
    "idle_state_fragment",
    "idle_wake_minutes",
    "read_idle_state",
    "sweep_worker_idle_wakes",
]
