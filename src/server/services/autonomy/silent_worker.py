"""Classify a Ticket Worker that produced no output as a dispatch failure.

A Worker can start, receive its prompt, and never answer: the run stays open
with nothing in it, or it ends with an empty final answer. Neither looks like
a failure to the rest of the Ticket line, so the Ticket waits until the
two-hour stall flag. This sweep names the condition early, as its own
failure class (``worker_no_output``), alerts once per Worker run and, in
enforce mode, moves the line to another provider through the same
``plan_switch`` / ``record_switch`` path the switch endpoint uses.

What counts as output for the Worker's current attempt (any one is enough):

* a Ticket report (progress or completion) filed since the attempt started;
* a commit the current run made (the idle-wake delivery check);
* a live-output event the Worker streamed for its current lease or run;
* a non-empty last assistant message in the Worker thread's replay.

A daemon line (no cloud thread) is judged by its run log, the live output it
streamed for the current lease, read like the auto-dispatch run log
(``has_assistant_output``). A daemon run with no readable log is not
classified: a CLI that prints only when it ends has streamed nothing yet.

A run whose status cannot be read is not classified: an unknown is not a
silence.

A daemon line is switched only once its old run is confirmed stopped (its
lease is no longer live); otherwise the verdict is recorded and the line
stays, so two authors never write the same Ticket.

Switches:

* ``CONDUCTOR_TICKET_SILENT_WORKER_MODE``: ``off``, ``shadow`` (default) or
  ``enforce``, spelled exactly. Anything else is ``shadow`` with a warning.
  ``off`` reads nothing and writes nothing. ``shadow`` records and alerts, and
  writes one ``shadow_decision`` log line per classified run saying whether
  enforce would have switched it. ``enforce`` also switches the line away from
  the silent provider.
* ``CONDUCTOR_TICKET_SILENT_WORKER_MINUTES``: minutes since the attempt
  started before a silent Worker is classified (default 15; a missing,
  invalid or non-positive value is the default).

The record lives in the Ticket's mandate under ``worker_silent`` (JSON,
versioned), so no migration is needed. It names the run it judged, so one
run is classified, alerted and switched at most once.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.server.database.autonomy import tickets as ticket_db
from src.server.services.autonomy import line_switch as ls
from src.server.services.autonomy.notifications import (
    Channel,
    NotificationEvent,
    database_is_open,
    dedup_key,
    emit_event,
)
from src.server.services.autonomy.roles import canonical_provider
from src.utils.shadow_log import shadow_decision

logger = logging.getLogger(__name__)

MODE_ENV = "CONDUCTOR_TICKET_SILENT_WORKER_MODE"
MINUTES_ENV = "CONDUCTOR_TICKET_SILENT_WORKER_MINUTES"
MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
_MODES = frozenset({MODE_OFF, MODE_SHADOW, MODE_ENFORCE})
DEFAULT_MODE = MODE_SHADOW
DEFAULT_MINUTES = 15

#: The failure class this sweep assigns.
FAILURE_CLASS = "worker_no_output"
#: The notification kind of the alert.
KIND_WORKER_NO_OUTPUT = FAILURE_CLASS
#: The mandate key that holds the classification of the current run.
MANDATE_KEY = "worker_silent"
RECORD_VERSION = 1
#: The mandate key a Ticket may carry with its switch candidates, as a list
#: of ``{"provider": ..., "model": ...}`` in preference order.
CANDIDATES_MANDATE_KEY = "switch_candidates"

REASON_NO_OUTPUT = "no_output"
REASON_EMPTY_FINAL = "empty_final_answer"

ACTION_RECORDED = "recorded"
ACTION_SWITCH_PENDING = "switch_pending"
ACTION_SWITCHED = "switched"
ACTION_NO_TARGET = "no_eligible_target"
ACTION_SWITCH_REFUSED = "switch_refused"
ACTION_STOP_UNCONFIRMED = "stop_unconfirmed"

#: The most Tickets one pass probes. A probe reads the thread over HTTP, so
#: the bound keeps one pass short; the next pass continues.
MAX_PROBES_PER_PASS = 20
_CANDIDATE_PAGE = 50
#: Page cap, so a pass over a long list of already-judged rows still ends.
_MAX_PAGES = 20

_warned_modes: set[str] = set()


@dataclass(frozen=True, slots=True)
class SilenceVerdict:
    """Whether the Worker's current attempt produced output."""

    silent: bool
    reason: str


@dataclass(frozen=True, slots=True)
class SilentWorkerSweep:
    """What one pass did."""

    mode: str = MODE_OFF
    probed: int = 0
    classified: frozenset[str] = field(default_factory=frozenset)
    switched: frozenset[str] = field(default_factory=frozenset)


def silent_worker_mode(environ: Any | None = None) -> str:
    """The sweep mode. Only the exact spellings count; anything else is shadow."""
    source = os.environ if environ is None else environ
    raw = source.get(MODE_ENV)
    if raw is None or raw == "":
        return DEFAULT_MODE
    value = str(raw)
    if value in _MODES:
        return value
    if value not in _warned_modes:
        _warned_modes.add(value)
        logger.warning(
            "[autonomy] %s=%r is not off, shadow or enforce; using shadow",
            MODE_ENV,
            value,
        )
    return DEFAULT_MODE


def silent_worker_minutes(environ: Any | None = None) -> int:
    """Minutes before a silent Worker is classified; the default when unusable."""
    source = os.environ if environ is None else environ
    raw = source.get(MINUTES_ENV)
    if raw is None:
        return DEFAULT_MINUTES
    try:
        minutes = int(str(raw).strip())
    except ValueError:
        return DEFAULT_MINUTES
    return minutes if minutes > 0 else DEFAULT_MINUTES


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_record(mandate: Any) -> dict[str, Any] | None:
    """The ``worker_silent`` record of this version, or None."""
    if not isinstance(mandate, dict):
        return None
    record = mandate.get(MANDATE_KEY)
    if not isinstance(record, dict) or record.get("version") != RECORD_VERSION:
        return None
    return record


def _run_key(ticket: dict[str, Any]) -> str:
    """The identity of the Worker run being judged."""
    run_id = str(ticket.get("worker_run_id") or "")
    if run_id:
        return run_id
    lease = str(ticket.get("worker_lease_id") or "")
    if lease:
        return f"lease:{lease}"
    return f"attempt:{int(ticket.get('attempts') or 0)}"


def _already_handled(record: dict[str, Any] | None, run_key: str, mode: str) -> bool:
    """One run is judged once. A shadow verdict may still be enforced later."""
    if record is None or str(record.get("run_key") or "") != run_key:
        return False
    if mode == MODE_ENFORCE and record.get("mode") == MODE_SHADOW:
        return False
    return True


# --- output probes (each one fails toward "output seen" or "unknown") -------


async def _attempt_started_at(ticket: dict[str, Any]) -> datetime | None:
    from src.server.services.autonomy.worker_idle import (
        _attempt_started_at as attempt_started_at,
    )

    started = await attempt_started_at(ticket)
    if started is None:
        started = _as_datetime(ticket.get("started_at"))
    return _as_datetime(started) if started is not None else None


async def _has_report_since(ticket: dict[str, Any], started: datetime | None) -> bool:
    report = await ticket_db.latest_report(str(ticket.get("ticket_id") or ""))
    if not isinstance(report, dict):
        return False
    if started is None:
        # The attempt cannot be dated: a report is taken as this run's output.
        return True
    created = _as_datetime(report.get("created_at"))
    return created is None or created >= started


async def _has_commit(ticket: dict[str, Any]) -> bool:
    from src.server.services.autonomy.worker_idle import _worker_has_commit

    try:
        return await asyncio.to_thread(_worker_has_commit, ticket) is True
    except Exception:  # noqa: BLE001 - an unreadable checkout is not output
        return False


async def _has_streamed_output(ticket: dict[str, Any]) -> bool:
    """Whether the Worker streamed any live output for its current lease or run."""
    from src.server.database.autonomy import daemon_output as output_db

    session = await output_db.read_session(str(ticket.get("ticket_id") or ""))
    if not isinstance(session, dict) or int(session.get("next_seq") or 0) <= 0:
        return False
    lease = str(ticket.get("worker_lease_id") or "").lower()
    run_id = str(ticket.get("worker_run_id") or "")
    if lease and str(session.get("lease_id") or "").lower() == lease:
        return True
    return bool(run_id) and str(session.get("run_id") or "") == run_id


#: The most daemon log events one probe reads.
_LOG_EVENT_LIMIT = 500


async def _daemon_run_log(ticket: dict[str, Any]) -> tuple[str, bool] | None:
    """The current daemon run's streamed log: (text, saw a tool/reasoning event).

    None when there is no log of the current lease to read (no session, a
    session of another lease, or no events kept).
    """
    from src.server.database.autonomy import daemon_output as output_db

    ticket_id = str(ticket.get("ticket_id") or "")
    session = await output_db.read_session(ticket_id)
    if not isinstance(session, dict) or int(session.get("next_seq") or 0) <= 0:
        return None
    lease = str(ticket.get("worker_lease_id") or "").lower()
    if not lease or str(session.get("lease_id") or "").lower() != lease:
        return None
    events = await output_db.read_events(
        ticket_id, int(session.get("epoch") or 0), 0, _LOG_EVENT_LIMIT
    )
    if not events:
        return None
    lines: list[str] = []
    other = False
    for _seq, data in events:
        if not isinstance(data, dict):
            continue
        kind = str(data.get("kind") or "")
        if kind in {"tool", "reasoning"}:
            other = True
        elif kind == "text":
            lines.append(str(data.get("text") or ""))
    return "".join(lines), other


async def _daemon_log_verdict(ticket: dict[str, Any]) -> SilenceVerdict | None:
    from src.tools.auto_dispatch.silent_worker import has_assistant_output

    log = await _daemon_run_log(ticket)
    if log is None:
        return None
    text, other = log
    if other or has_assistant_output(text):
        return SilenceVerdict(False, "streamed")
    return SilenceVerdict(True, REASON_NO_OUTPUT)


async def _thread_state(ticket: dict[str, Any]) -> tuple[str | None, str | None]:
    """The thread's run status and last assistant text (None when unreadable)."""
    from src.server.services.projects.dispatch import ProjectDispatcher

    user_id = str(ticket.get("user_id") or "")
    thread_id = str(ticket.get("worker_thread_id") or "")
    dispatcher = ProjectDispatcher()
    status = await dispatcher.thread_run_status(user_id=user_id, thread_id=thread_id)
    if status is None:
        return None, None
    text = await dispatcher.thread_last_assistant_text(
        user_id=user_id, thread_id=thread_id
    )
    return status, text


async def probe_silence(ticket: dict[str, Any]) -> SilenceVerdict | None:
    """Whether the current attempt is silent; None when it cannot be told."""
    started = await _attempt_started_at(ticket)
    if await _has_report_since(ticket, started):
        return SilenceVerdict(False, "reported")
    if await _has_commit(ticket):
        return SilenceVerdict(False, "committed")
    if ticket.get("worker_daemon_id") and not ticket.get("worker_thread_id"):
        try:
            return await _daemon_log_verdict(ticket)
        except Exception:  # noqa: BLE001 - an unreadable log is unknown, not silence
            logger.warning(
                "[autonomy] silent worker could not read the daemon log ticket=%s",
                ticket.get("ticket_id"),
                exc_info=True,
            )
            return None
    try:
        streamed = await _has_streamed_output(ticket)
    except Exception:  # noqa: BLE001 - an unreadable stream is unknown, not silence
        logger.warning(
            "[autonomy] silent worker could not read live output ticket=%s",
            ticket.get("ticket_id"),
            exc_info=True,
        )
        return None
    if streamed:
        return SilenceVerdict(False, "streamed")
    if not ticket.get("worker_thread_id"):
        return None
    status, text = await _thread_state(ticket)
    if status is None:
        return None
    if isinstance(text, str) and text.strip():
        return SilenceVerdict(False, "answered")
    if status == "in_progress":
        return SilenceVerdict(True, REASON_NO_OUTPUT)
    return SilenceVerdict(True, REASON_EMPTY_FINAL)


# --- recording, alerting, switching -----------------------------------------


def _record_fields(
    ticket: dict[str, Any],
    *,
    run_key: str,
    mode: str,
    reason: str,
    action: str,
    minutes: int,
    now: datetime,
    target: dict[str, str] | None = None,
    detail: str = "",
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "version": RECORD_VERSION,
        "failure_class": FAILURE_CLASS,
        "run_key": run_key,
        "run_id": str(ticket.get("worker_run_id") or ""),
        "thread_id": str(ticket.get("worker_thread_id") or ""),
        "attempt": int(ticket.get("attempts") or 0),
        "provider": str(ticket.get("worker_provider") or ""),
        "model": str(ticket.get("worker_model") or ""),
        "reason": reason,
        "mode": mode,
        "action": action,
        "minutes": minutes,
        "detected_at": now.isoformat(),
    }
    if target:
        record["target"] = dict(target)
    if detail:
        record["detail"] = detail[:300]
    return record


async def _write_record(
    ticket_id: str,
    fields: dict[str, Any],
    *,
    expected_status: str | None,
    expected_thread_id: str | None,
    expected_revision: int | None,
    touch: bool = True,
) -> dict[str, Any] | None:
    removed = [name for name in ("target", "detail") if name not in fields]
    return await ticket_db.update_mandate_record(
        ticket_id,
        MANDATE_KEY,
        set_fields=fields,
        remove_fields=removed,
        expected_status=expected_status,
        expected_thread_id=expected_thread_id,
        expected_revision=expected_revision,
        touch=touch,
    )


def _what(reason: str, minutes: int) -> str:
    if reason == REASON_EMPTY_FINAL:
        return "ended with an empty final answer"
    return f"produced no output in {minutes} minutes"


def silent_worker_event(
    ticket: dict[str, Any], *, run_key: str, reason: str, minutes: int
) -> NotificationEvent | None:
    """The alert for one silent Worker run, de-duplicated by Ticket and run."""
    user_id = str(ticket.get("user_id") or "")
    ticket_id = str(ticket.get("ticket_id") or "")
    if not user_id or not ticket_id:
        return None
    provider = str(ticket.get("worker_provider") or "") or "unknown"
    title = " ".join(str(ticket.get("title") or "").split())[:80]
    label = title or ticket_id[:8]
    what = _what(reason, minutes)
    return NotificationEvent(
        kind=KIND_WORKER_NO_OUTPUT,
        dedup_key=dedup_key(KIND_WORKER_NO_OUTPUT, ticket_id, run_key),
        user_id=user_id,
        title="Worker produced no output",
        body=f"Ticket {label}: the {provider} Worker {what}.",
        subject_id=ticket_id,
    )


def _candidates(ticket: dict[str, Any], silent_provider: str) -> list[ls.ModelRef]:
    mandate = dict(ticket.get("mandate") or {})
    raw = mandate.get(CANDIDATES_MANDATE_KEY)
    out: list[ls.ModelRef] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            ref = ls.ModelRef(
                str(item.get("provider") or ""), str(item.get("model") or "")
            )
            if canonical_provider(ref.provider) == silent_provider:
                continue
            out.append(ref)
    if ticket.get("worker_daemon_id"):
        # Same rule as the switch endpoint: a daemon line stays on its provider.
        current = str(ticket.get("worker_provider") or "")
        out = [ref for ref in out if ref.provider == current]
    return out


def _switch_plan(ticket: dict[str, Any], *, reason: str, minutes: int) -> ls.SwitchPlan:
    """Choose the switch target from the same rules the switch endpoint uses.

    Pure and read-only: no record, no cancel, no launch. Shared by
    :func:`_switch` (which then writes) and :func:`_would_switch` (which only
    reports), so the shadow log cannot over-report a switch enforce would
    refuse -- a candidate that is the current model, the reviewer, an open
    breaker or a blank entry is filtered here, not by the caller.
    """
    provider = str(ticket.get("worker_provider") or "")
    silent = canonical_provider(provider)
    current = ls.ModelRef(provider, str(ticket.get("worker_model") or ""))
    reviewer_src = (ticket.get("mandate") or {}).get("reviewer")
    reviewer = (
        ls.ModelRef(
            str(reviewer_src.get("provider") or ""), str(reviewer_src.get("model") or "")
        )
        if isinstance(reviewer_src, dict)
        else None
    )
    handoff = ls.build_handoff(
        [],
        "",
        [
            f"{FAILURE_CLASS}: the previous Worker ({provider or 'unknown'}) "
            f"{_what(reason, minutes)}."
        ],
    )
    return ls.plan_switch(
        ls.LineState(current),
        _candidates(ticket, silent),
        handoff,
        reason=FAILURE_CLASS,
        reviewer=reviewer,
        # The silent provider is treated as an open breaker, so no
        # candidate of it can be chosen under any spelling.
        open_providers=[provider, silent],
        attempt=len(ticket.get("switches") or []) + 1,
    )


async def _switch(
    ticket: dict[str, Any], *, reason: str, minutes: int
) -> tuple[str, dict[str, Any] | None, dict[str, str] | None, str]:
    """Move the line off the silent provider: (action, updated row, target, detail)."""
    if ticket.get("worker_daemon_id") and not await _daemon_run_stopped(ticket):
        # A daemon process cannot be cancelled from here, and it keeps writing
        # while a second author would launch: record only.
        return ACTION_STOP_UNCONFIRMED, None, None, "daemon_run_live"
    try:
        plan = _switch_plan(ticket, reason=reason, minutes=minutes)
    except ls.NoEligibleTarget as exc:
        return ACTION_NO_TARGET, None, None, str(exc)
    current = plan.source

    from src.server.services.autonomy.cloud_runtime import (
        resolve_cloud_worker_isolation_async,
    )
    from src.server.services.autonomy.models import EFFECT_LAUNCH_WORKER
    from src.server.services.autonomy.worker import is_unisolated_cloud_worker

    if is_unisolated_cloud_worker(ticket) and not (
        await resolve_cloud_worker_isolation_async()
    ).launch_allowed:
        # The endpoint's refusal path belongs to a person; the sweep does not
        # move a line onto a runtime that may not launch it.
        return ACTION_SWITCH_REFUSED, None, plan.target.as_dict(), "cloud_unisolated"
    ticket_id = str(ticket.get("ticket_id") or "")
    updated = await ticket_db.record_switch(
        ticket_id,
        expected_status="running",
        expected_model=(current.provider, current.model),
        record=plan.record,
        handoff_text=plan.handoff_text,
        launch_effect=(str(ticket.get("user_id") or ""), EFFECT_LAUNCH_WORKER),
    )
    if updated is None:
        return ACTION_SWITCH_REFUSED, None, plan.target.as_dict(), "ticket_changed"
    thread_id = str(ticket.get("worker_thread_id") or "")
    if thread_id:
        from src.server.services.projects.dispatch import ProjectDispatcher

        try:
            await ProjectDispatcher().cancel_thread(
                user_id=str(ticket.get("user_id") or ""),
                thread_id=thread_id,
                run_id=ticket.get("worker_run_id"),
            )
        except Exception:  # noqa: BLE001 - the switch is committed; the stop is best effort
            logger.warning(
                "[autonomy] cancel of silent worker failed ticket=%s", ticket_id
            )
    return ACTION_SWITCHED, updated, plan.target.as_dict(), ""


async def _daemon_run_stopped(ticket: dict[str, Any]) -> bool:
    """True only when the daemon run's lease is confirmed no longer live."""
    from src.server.database.autonomy import daemons as daemon_db

    lease = str(ticket.get("worker_lease_id") or "")
    if not lease:
        return False
    try:
        live = await daemon_db.live_lease_ticket(
            str(ticket.get("worker_daemon_id") or ""),
            str(ticket.get("ticket_id") or ""),
            lease,
        )
    except Exception:  # noqa: BLE001 - an unreadable lease is not a stopped run
        logger.warning(
            "[autonomy] silent worker could not read the daemon lease ticket=%s",
            ticket.get("ticket_id"),
            exc_info=True,
        )
        return False
    return not live


async def _would_switch(
    ticket: dict[str, Any], *, reason: str, minutes: int
) -> tuple[bool, str]:
    """Whether enforce would move this line, and why not when it would not.

    Shadow runs the switch's own preconditions, read-only, so its log says what
    enforce would have done instead of guessing: a daemon run must be confirmed
    stopped, the target must be one :func:`_switch_plan` (the same
    ``plan_switch`` the endpoint uses) would pick -- not the current model, not
    the reviewer, no open breaker -- and an unisolated cloud line needs a
    launch-allowed runtime. Nothing here records, switches, cancels or alerts.
    """
    if ticket.get("worker_daemon_id") and not await _daemon_run_stopped(ticket):
        return False, "daemon_run_live"
    try:
        _switch_plan(ticket, reason=reason, minutes=minutes)
    except ls.NoEligibleTarget:
        return False, "no_eligible_target"
    from src.server.services.autonomy.worker import is_unisolated_cloud_worker

    if is_unisolated_cloud_worker(ticket):
        from src.server.services.autonomy.cloud_runtime import (
            resolve_cloud_worker_isolation_async,
        )

        if not (await resolve_cloud_worker_isolation_async()).launch_allowed:
            return False, "cloud_unisolated"
    return True, "switch"


async def _handle(
    ticket: dict[str, Any],
    *,
    mode: str,
    minutes: int,
    now: datetime,
    channel: Channel | None,
) -> tuple[bool, bool, bool]:
    """Judge one Ticket: (probed, classified, switched)."""
    ticket_id = str(ticket.get("ticket_id") or "")
    if not ticket_id or str(ticket.get("status") or "") != "running":
        return False, False, False
    mandate = dict(ticket.get("mandate") or {})
    if mandate.get("orchestration_only") or mandate.get("kind") == "main_agent_root":
        return False, False, False
    run_key = _run_key(ticket)
    if _already_handled(read_record(mandate), run_key, mode):
        return False, False, False
    verdict = await probe_silence(ticket)
    if verdict is None or not verdict.silent:
        return True, False, False

    thread_id = str(ticket.get("worker_thread_id") or "") or None
    revision = ticket.get("revision")
    action = ACTION_SWITCH_PENDING if mode == MODE_ENFORCE else ACTION_RECORDED
    recorded = await _write_record(
        ticket_id,
        _record_fields(
            ticket,
            run_key=run_key,
            mode=mode,
            reason=verdict.reason,
            action=action,
            minutes=minutes,
            now=now,
        ),
        expected_status="running",
        expected_thread_id=thread_id,
        expected_revision=revision if isinstance(revision, int) else None,
        # Shadow observes only: the record leaves the revision (and the
        # updated_at clock) alone, so no writer fenced on them is refused.
        touch=mode != MODE_SHADOW,
    )
    if recorded is None:
        logger.info("[autonomy] silent worker skipped; ticket changed ticket=%s", ticket_id)
        return True, False, False
    logger.warning(
        "[autonomy] silent worker classified ticket=%s failure_class=%s reason=%s "
        "provider=%s mode=%s",
        ticket_id,
        FAILURE_CLASS,
        verdict.reason,
        ticket.get("worker_provider"),
        mode,
    )
    event = silent_worker_event(
        ticket, run_key=run_key, reason=verdict.reason, minutes=minutes
    )
    if event is not None:
        try:
            await emit_event(event, channel=channel)
        except Exception:  # noqa: BLE001 - the mandate record is the classification
            logger.exception(
                "[autonomy] silent worker alert failed ticket=%s", ticket_id
            )
    if mode != MODE_ENFORCE:
        if mode == MODE_SHADOW:
            # One line per shadow judgement: what enforce would have done with
            # this run. The classification and the alert above are unchanged.
            try:
                would_switch, why = await _would_switch(
                    ticket, reason=verdict.reason, minutes=minutes
                )
            except Exception:  # noqa: BLE001 - the log is best effort; the record stands
                logger.warning(
                    "[autonomy] silent worker could not tell what enforce would do "
                    "ticket=%s",
                    ticket_id,
                    exc_info=True,
                )
                would_switch, why = False, "unknown"
            shadow_decision(
                logger,
                "silent_worker",
                would_change=would_switch,
                subject=ticket_id,
                current="record_only",
                enforced="switch" if would_switch else "record_only",
                reason=why,
                signal=verdict.reason,
                provider=ticket.get("worker_provider"),
                minutes=minutes,
            )
        return True, True, False

    base = {**ticket, **recorded}
    try:
        outcome, updated, target, detail = await _switch(
            base, reason=verdict.reason, minutes=minutes
        )
    except Exception as exc:  # noqa: BLE001 - recorded either way
        logger.exception("[autonomy] silent worker switch failed ticket=%s", ticket_id)
        outcome, updated, target, detail = ACTION_SWITCH_REFUSED, None, None, type(exc).__name__
    fields = _record_fields(
        ticket,
        run_key=run_key,
        mode=mode,
        reason=verdict.reason,
        action=outcome,
        minutes=minutes,
        now=now,
        target=target,
        detail=detail,
    )
    after = updated or recorded
    after_revision = after.get("revision")
    try:
        await _write_record(
            ticket_id,
            fields,
            expected_status=None if updated is not None else "running",
            expected_thread_id=None if updated is not None else thread_id,
            expected_revision=after_revision if isinstance(after_revision, int) else None,
        )
    except Exception:  # noqa: BLE001 - the switch record on the Ticket already says it
        logger.warning(
            "[autonomy] silent worker could not record the outcome ticket=%s", ticket_id
        )
    if outcome == ACTION_SWITCHED:
        logger.warning(
            "[autonomy] silent worker line switched ticket=%s from=%s to=%s",
            ticket_id,
            ticket.get("worker_provider"),
            (target or {}).get("provider"),
        )
    else:
        logger.warning(
            "[autonomy] silent worker not switched ticket=%s outcome=%s detail=%s",
            ticket_id,
            outcome,
            detail,
        )
    return True, True, outcome == ACTION_SWITCHED


async def sweep_silent_workers(
    *,
    mode: str | None = None,
    minutes: int | None = None,
    now: datetime | None = None,
    max_probes: int = MAX_PROBES_PER_PASS,
    channel: Channel | None = None,
) -> SilentWorkerSweep:
    """Classify, alert on and (enforce) switch the Worker runs that stayed silent.

    ``off`` returns before reading anything. Without an open app-data pool
    there is no Ticket store to read, so the pass does nothing either.
    """
    active = silent_worker_mode() if mode is None else mode
    if active not in _MODES or active == MODE_OFF:
        return SilentWorkerSweep(mode=MODE_OFF if active == MODE_OFF else active)
    if not database_is_open():
        return SilentWorkerSweep(mode=active)
    threshold = silent_worker_minutes() if minutes is None or minutes <= 0 else minutes
    moment = now or datetime.now(timezone.utc)
    probed = 0
    classified: set[str] = set()
    switched: set[str] = set()
    offset = 0
    for _ in range(_MAX_PAGES):
        if probed >= max_probes:
            break
        rows = await ticket_db.list_worker_idle_candidates(
            idle_seconds=threshold * 60, limit=_CANDIDATE_PAGE, offset=offset
        )
        if not rows:
            break
        for row in rows:
            if probed >= max_probes:
                break
            try:
                did_probe, was_classified, was_switched = await _handle(
                    row, mode=active, minutes=threshold, now=moment, channel=channel
                )
            except Exception:  # noqa: BLE001 - one Ticket must not stop the pass
                logger.exception(
                    "[autonomy] silent worker check failed ticket=%s",
                    row.get("ticket_id"),
                )
                did_probe, was_classified, was_switched = True, False, False
            ticket_id = str(row.get("ticket_id") or "")
            probed += int(did_probe)
            if was_classified:
                classified.add(ticket_id)
            if was_switched:
                switched.add(ticket_id)
        if len(rows) < _CANDIDATE_PAGE:
            break
        offset += len(rows)
    return SilentWorkerSweep(
        mode=active,
        probed=probed,
        classified=frozenset(classified),
        switched=frozenset(switched),
    )


__all__ = [
    "CANDIDATES_MANDATE_KEY",
    "DEFAULT_MINUTES",
    "DEFAULT_MODE",
    "FAILURE_CLASS",
    "KIND_WORKER_NO_OUTPUT",
    "MANDATE_KEY",
    "MINUTES_ENV",
    "MODE_ENV",
    "SilenceVerdict",
    "SilentWorkerSweep",
    "probe_silence",
    "read_record",
    "silent_worker_event",
    "silent_worker_minutes",
    "silent_worker_mode",
    "sweep_silent_workers",
]
