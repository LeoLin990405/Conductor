"""De-duplicate Worker launches by (ticket or issue, line, base commit).

The same work was dispatched again and again: a new comment on a finished
issue opened another Ticket for the same issue on the same model line, and a
Ticket re-queued on its unchanged line was claimed again. This module holds
the pure rules: the launch key, the decision against earlier launches, the
mode switch and the JSON record kept on the Ticket's mandate (no migration).

Key
    ``subject``  ``issue:<origin_thread_key>`` when the Ticket came from a
                 Source thread, else ``ticket:<ticket_id>``.
    ``line``     the Worker line, ``(worker_provider, worker_model)``
                 normalised like :meth:`line_switch.ModelRef.key`.
    ``base``     the Ticket's durable ``worktree_base_sha`` pin. It is written
                 at the first worktree bind (first-write-wins, never cleared),
                 so a Ticket that never launched has none. An empty base is
                 "unpinned": it matches an in-flight launch on any base, but
                 never a done one, so a new request on a finished issue
                 launches. Records say ``base_match = "unpinned"``.

A prior launch counts while it is in flight (an open status) or done
(``completed``). A failed, cancelled or archived prior never blocks a new
launch: a retry after failure is the normal path.

Modes (``CONDUCTOR_TICKET_DEDUP_MODE``)
    ``off``      no lookup, no record: byte-identical to before.
    ``shadow``   default. Records "would dedupe" and launches as before.
    ``enforce``  skips the duplicate and keeps the existing launch.
Anything else is read as ``shadow`` with a warning. An operator ``force``
always launches, and is recorded.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

ENV_MODE = "CONDUCTOR_TICKET_DEDUP_MODE"
MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
MODES = (MODE_OFF, MODE_SHADOW, MODE_ENFORCE)
DEFAULT_MODE = MODE_SHADOW

#: The mandate key that holds the launch de-duplication record.
LAUNCH_DEDUP_MANDATE_KEY = "launch_dedup"
#: The version of the record written to the mandate.
RECORD_VERSION = 1
#: Older "would dedupe" entries beyond this many are dropped from the record.
MAX_HISTORY = 20

IN_FLIGHT_STATUSES = frozenset(
    {"queued", "running", "waiting", "blocked", "awaiting_clarification"}
)
DONE_STATUSES = frozenset({"completed"})

ACTION_LAUNCH = "launch"
ACTION_SKIP = "skip"

STATE_IN_FLIGHT = "in_flight"
STATE_DONE = "done"


def dedup_mode(env: Mapping[str, str] | None = None) -> str:
    """The configured mode; an unknown spelling is shadow, with a warning."""
    source = os.environ if env is None else env
    raw = source.get(ENV_MODE)
    if raw is None or raw == "":
        return DEFAULT_MODE
    if raw in MODES:
        return raw
    logger.warning(
        "[autonomy] %s=%r is not one of off|shadow|enforce; using shadow",
        ENV_MODE,
        raw,
    )
    return MODE_SHADOW


def is_forced(payload: Mapping[str, Any] | None) -> bool:
    """True only for an explicit operator force (``force: true``)."""
    return bool(payload) and payload.get("force") is True  # type: ignore[union-attr]


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


@dataclass(frozen=True, slots=True)
class LaunchKey:
    subject: str
    provider: str
    model: str
    base: str = ""
    #: The per-Ticket work token (see :func:`work_token`); empty for the
    #: thread-level key, which compares Tickets, not runs of one Ticket.
    work: str = ""

    @property
    def line(self) -> tuple[str, str]:
        return (self.provider, self.model)

    def as_dict(self) -> dict[str, str]:
        out = {
            "subject": self.subject,
            "provider": self.provider,
            "model": self.model,
            "base": self.base,
        }
        if self.work:
            out["work"] = self.work
        return out


def launch_key(
    ticket: Mapping[str, Any],
    *,
    provider: str | None = None,
    model: str | None = None,
    thread_key: str | None = None,
) -> LaunchKey:
    """The launch key of a Ticket row (or of a Ticket about to be created).

    ``provider``/``model``/``thread_key`` override the row's values, for a
    Ticket whose row does not exist yet.
    """
    thread = str(
        thread_key if thread_key is not None else ticket.get("origin_thread_key") or ""
    ).strip()
    subject = f"issue:{thread}" if thread else f"ticket:{ticket.get('ticket_id') or ''}"
    return LaunchKey(
        subject=subject,
        provider=_norm(provider if provider is not None else ticket.get("worker_provider")),
        model=_norm(model if model is not None else ticket.get("worker_model")),
        base=str(ticket.get("worktree_base_sha") or "").strip(),
    )


def prior_state(ticket: Mapping[str, Any]) -> str | None:
    """``in_flight``, ``done`` or None for a prior that never blocks."""
    if ticket.get("closed_at"):
        return None
    status = str(ticket.get("status") or "")
    if status in IN_FLIGHT_STATUSES:
        return STATE_IN_FLIGHT
    if status in DONE_STATUSES:
        return STATE_DONE
    return None


def _base_match(candidate: str, prior: str) -> str | None:
    if candidate and prior:
        return "exact" if candidate == prior else None
    return "unpinned"


@dataclass(frozen=True, slots=True)
class DedupDecision:
    mode: str
    action: str
    duplicate: bool
    key: LaunchKey
    forced: bool = False
    existing_ticket_id: str = ""
    existing_state: str = ""
    base_match: str = ""

    @property
    def skip(self) -> bool:
        return self.action == ACTION_SKIP

    def record(self, *, now: datetime | None = None, origin: str = "") -> dict[str, Any]:
        """One JSON entry describing this decision."""
        entry: dict[str, Any] = {
            "at": (now or datetime.now(timezone.utc)).isoformat(),
            "mode": self.mode,
            "action": self.action,
            "would_dedupe": self.duplicate and not self.forced,
            "forced": self.forced,
            "key": self.key.as_dict(),
            "existing_ticket_id": self.existing_ticket_id,
            "existing_state": self.existing_state,
            "base_match": self.base_match,
        }
        if origin:
            entry["origin"] = origin
        return entry


def decide(
    key: LaunchKey,
    priors: Iterable[Mapping[str, Any]],
    *,
    mode: str,
    force: bool = False,
    exclude_ticket_id: str = "",
) -> DedupDecision:
    """Compare a candidate launch with earlier Tickets' launches.

    An in-flight match wins over a done one, so enforce keeps the live line.
    Off never matches; shadow always launches; enforce skips a match unless
    forced.
    """
    if mode not in MODES:
        mode = MODE_SHADOW
    if mode == MODE_OFF:
        return DedupDecision(mode, ACTION_LAUNCH, False, key, forced=force)
    best: tuple[str, str, str] | None = None
    for prior in priors:
        prior_id = str(prior.get("ticket_id") or "")
        if exclude_ticket_id and prior_id == exclude_ticket_id:
            continue
        state = prior_state(prior)
        if state is None:
            continue
        other = launch_key(prior)
        if other.subject != key.subject or other.line != key.line:
            continue
        match = _base_match(key.base, other.base)
        if match is None or (match == "unpinned" and state == STATE_DONE):
            # An unpinned base proves nothing about a finished run: a new
            # request on a done issue launches.
            continue
        if best is None or (state == STATE_IN_FLIGHT and best[1] != STATE_IN_FLIGHT):
            best = (prior_id, state, match)
    if best is None:
        return DedupDecision(mode, ACTION_LAUNCH, False, key, forced=force)
    action = ACTION_SKIP if mode == MODE_ENFORCE and not force else ACTION_LAUNCH
    return DedupDecision(
        mode,
        action,
        True,
        key,
        forced=force,
        existing_ticket_id=best[0],
        existing_state=best[1],
        base_match=best[2],
    )


def merge_record(
    mandate: Mapping[str, Any] | None, entry: Mapping[str, Any]
) -> dict[str, Any]:
    """The mandate record after appending ``entry`` (bounded history)."""
    current = (mandate or {}).get(LAUNCH_DEDUP_MANDATE_KEY)
    history = []
    if isinstance(current, Mapping) and isinstance(current.get("history"), list):
        history = [item for item in current["history"] if isinstance(item, Mapping)]
    history = [*history, dict(entry)][-MAX_HISTORY:]
    would = sum(1 for item in history if item.get("would_dedupe"))
    merged = dict(current) if isinstance(current, Mapping) else {}
    merged.update(
        version=RECORD_VERSION,
        last=dict(entry),
        would_dedupe_count=would,
        history=[dict(item) for item in history],
    )
    return merged


# --- per-Ticket relaunch (re-queued row claimed again) ----------------------

#: The record field listing this Ticket's earlier launches.
LAUNCHES_FIELD = "launches"
#: The record field an operator retry sets so the next claim launches.
FORCE_NEXT_FIELD = "force_next"
MAX_LAUNCHES = 20
#: A daemon report state that means the run finished its work.
DONE_REPORT_STATES = frozenset({"ready"})


def work_token(ticket: Mapping[str, Any], latest_input_id: str = "") -> str:
    """A digest of the work a Ticket was asked to do.

    It changes whenever new work or input arrives: a rewritten scope or goal
    (a clarification), a new input row (an answer, a follow-up, an attached
    trigger) or a quota requeue. A re-run of the same Ticket for new work is
    therefore a new key, never a duplicate of the run that finished before.
    """
    mandate = ticket.get("mandate")
    mandate = mandate if isinstance(mandate, Mapping) else {}
    quota = mandate.get("quota_waker")
    retries = quota.get("retries") if isinstance(quota, Mapping) else None
    raw = json.dumps(
        [
            str(ticket.get("scope") or ""),
            str(mandate.get("goal") or ""),
            str(latest_input_id or ""),
            retries if isinstance(retries, int) else 0,
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def ticket_launch_key(ticket: Mapping[str, Any], *, work: str = "") -> LaunchKey:
    """The per-Ticket key: ``ticket:<id>``, the line, the base pin, the work."""
    return LaunchKey(
        subject=f"ticket:{ticket.get('ticket_id') or ''}",
        provider=_norm(ticket.get("worker_provider")),
        model=_norm(ticket.get("worker_model")),
        base=str(ticket.get("worktree_base_sha") or "").strip(),
        work=str(work or ""),
    )


def _record(mandate: Mapping[str, Any] | None) -> Mapping[str, Any]:
    current = (mandate or {}).get(LAUNCH_DEDUP_MANDATE_KEY)
    return current if isinstance(current, Mapping) else {}


def launches(mandate: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """This Ticket's earlier launches, oldest first."""
    items = _record(mandate).get(LAUNCHES_FIELD)
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, Mapping)]


def force_requested(mandate: Mapping[str, Any] | None) -> bool:
    """Whether an operator retry asked the next launch to skip de-dup."""
    return is_forced({"force": _record(mandate).get(FORCE_NEXT_FIELD)})


def with_force_next(mandate: Mapping[str, Any] | None) -> dict[str, Any]:
    """A copy of ``mandate`` whose record asks the next launch to be forced."""
    out = dict(mandate or {})
    record = dict(_record(mandate))
    record[FORCE_NEXT_FIELD] = True
    out[LAUNCH_DEDUP_MANDATE_KEY] = record
    return out


def decide_relaunch(
    key: LaunchKey,
    prior: Iterable[Mapping[str, Any]],
    *,
    outcomes: Mapping[str, Any],
    live_leases: Iterable[str] = (),
    current_lease: str = "",
    mode: str,
    force: bool = False,
    pending_input: bool = False,
) -> DedupDecision:
    """Decide a re-queued Ticket's new launch against its own earlier launches.

    ``prior`` are the recorded launch entries; ``outcomes`` maps a lease id to
    the report that lease committed (or nothing). An entry counts as in flight
    while its lease is in ``live_leases`` and as done when its report state is
    ``ready``. An entry written before the base was pinned inherits the
    Ticket's pin: the pin is first-write-wins and never cleared, and is written
    right after the first claim. A different line, or a different base, is a
    new key, and so is different work (:func:`work_token`): an entry without
    the candidate's work token never matches. An unpinned candidate never
    matches a done entry. ``pending_input`` (an input not yet delivered to a
    Worker) always launches: a Ticket with work waiting is never a duplicate.
    """
    if mode not in MODES:
        mode = MODE_SHADOW
    if mode == MODE_OFF or pending_input:
        return DedupDecision(mode, ACTION_LAUNCH, False, key, forced=force)
    live = {str(item) for item in live_leases if item}
    best: tuple[str, str, str] | None = None
    for entry in prior:
        lease = str(entry.get("lease_id") or "")
        if not lease or lease == current_lease:
            continue
        raw = entry.get("key")
        other = raw if isinstance(raw, Mapping) else {}
        if str(other.get("subject") or "") != key.subject:
            continue
        if (_norm(other.get("provider")), _norm(other.get("model"))) != key.line:
            continue
        if str(other.get("work") or "") != key.work:
            continue
        base = str(other.get("base") or "").strip() or key.base
        match = _base_match(key.base, base)
        if match is None:
            continue
        report = outcomes.get(lease)
        state = None
        if lease in live:
            state = STATE_IN_FLIGHT
        elif (
            isinstance(report, Mapping)
            and str(report.get("state") or "") in DONE_REPORT_STATES
            and match == "exact"
        ):
            state = STATE_DONE
        if state is None:
            continue
        if best is None or (state == STATE_IN_FLIGHT and best[1] != STATE_IN_FLIGHT):
            best = (lease, state, match)
    if best is None:
        return DedupDecision(mode, ACTION_LAUNCH, False, key, forced=force)
    action = ACTION_SKIP if mode == MODE_ENFORCE and not force else ACTION_LAUNCH
    return DedupDecision(
        mode,
        action,
        True,
        key,
        forced=force,
        existing_ticket_id=best[0],
        existing_state=best[1],
        base_match=best[2],
    )


def record_relaunch(
    mandate: Mapping[str, Any] | None,
    decision: DedupDecision,
    *,
    lease_id: str,
    now: datetime | None = None,
    origin: str = "daemon_claim",
) -> dict[str, Any]:
    """The record after this claim: the launch entry, the decision, force spent.

    ``existing_ticket_id`` of a per-Ticket decision holds the earlier lease id.
    A skipped launch is not added to ``launches``.
    """
    at = (now or datetime.now(timezone.utc)).isoformat()
    record = (
        merge_record(mandate, decision.record(now=now, origin=origin))
        if decision.duplicate or decision.forced
        else dict(_record(mandate))
    )
    record["version"] = RECORD_VERSION
    record.pop(FORCE_NEXT_FIELD, None)
    entries = [item for item in launches(mandate) if item.get("lease_id") != lease_id]
    if not decision.skip:
        entries.append({"lease_id": lease_id, "key": decision.key.as_dict(), "at": at})
    record[LAUNCHES_FIELD] = entries[-MAX_LAUNCHES:]
    return record


async def check_thread_launch(
    *,
    thread_key: str,
    user_id: str,
    provider: str,
    model: str,
    mode: str,
    list_thread: Any,
    get_ticket: Any,
    force: bool = False,
) -> DedupDecision:
    """Decide a new Ticket's launch against the Tickets already on its thread.

    ``list_thread(thread_key)`` and ``get_ticket(ticket_id)`` are injected (the
    DB helpers in production, fakes in tests). The thread listing has no base
    pin or close stamp, so a prior on the same line is re-read in full. Another
    user's Ticket is never a prior.
    """
    key = launch_key({}, provider=provider, model=model, thread_key=thread_key)
    if mode == MODE_OFF:
        return decide(key, (), mode=mode, force=force)
    priors: list[Mapping[str, Any]] = []
    for row in await list_thread(thread_key):
        if str(row.get("user_id") or "") != user_id:
            continue
        if launch_key(row).line != key.line or prior_state(row) is None:
            continue
        full = await get_ticket(str(row.get("ticket_id") or ""))
        if full is not None and str(full.get("user_id") or "") == user_id:
            priors.append(full)
    return decide(key, priors, mode=mode, force=force)


def log_decision(decision: DedupDecision, *, ticket_id: str = "", origin: str = "") -> None:
    if not decision.duplicate:
        return
    logger.info(
        "[autonomy] launch dedup mode=%s action=%s forced=%s origin=%s "
        "ticket=%s existing=%s state=%s subject=%s line=%s/%s base=%s match=%s",
        decision.mode,
        decision.action,
        decision.forced,
        origin,
        ticket_id,
        decision.existing_ticket_id,
        decision.existing_state,
        decision.key.subject,
        decision.key.provider,
        decision.key.model,
        decision.key.base or "-",
        decision.base_match,
    )


__all__ = [
    "ACTION_LAUNCH",
    "ACTION_SKIP",
    "DEFAULT_MODE",
    "ENV_MODE",
    "LAUNCH_DEDUP_MANDATE_KEY",
    "MODE_ENFORCE",
    "MODE_OFF",
    "MODE_SHADOW",
    "DedupDecision",
    "LaunchKey",
    "check_thread_launch",
    "decide",
    "dedup_mode",
    "is_forced",
    "launch_key",
    "log_decision",
    "merge_record",
    "prior_state",
]
