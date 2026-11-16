"""Owner and deadline decision for one unfinished dispatch line (N76 §4.1).

The sweep does not infer ownership from ``line.status`` alone. It calls
:func:`decide_owner` with a plain line snapshot mapping -- the persisted line
record merged with the gates snapshot for the same line -- and the caller's
clock. Exactly one owner comes back:

``worker_run``
    ``current_run`` is set and its launch has positive liveness evidence (an
    alive/running marker or live state); a run id or pid alone is identity, not
    proof the process still exists.
``scheduled_review``
    A review is running -- dated from its own review mapping, not from a stale
    ``review_retry`` -- or ``review_retry`` has a due time.
``recovery_timer``
    ``liveness.next_at`` schedules the next recovery step.
``queued_request``
    An operator request is queued for the line.
``needs_human``
    The one-way liveness latch handed the line to a person.
``app_lease``
    An outside app holds a live lease on the line (N80 L4). The machine puts
    ``app_lease = {"app_id", "state", "expires_at", "max_lease_until"}``
    (epoch seconds) into the snapshot; the deadline is the earlier of the two
    times, and a lapsed lease is the sweep's work (R-EXIT, a new author).
``none``
    There is no owner, or the line is terminal.

Precedence is terminal, human, queued request, app lease, live worker,
review, recovery timer. Human comes first because the latch is one-way; a queued request comes
before a live worker because it is the line's next move; and a live worker
comes before a scheduled review because the process is still acting.

The liveness entry is the shape owned by the SPEC (``record.py``)::

    {
      "reason": "no_new_commit",       # parking reason
      "group": "R-EXIT",               # recovery group
      "parked_at": "...", "updated_at": "...",
      "budget": 4, "spent": 1,
      "needs_human": true,             # one-way latch, optional
      "human_reason": "handoff_incomplete",
      "next_at": "...",                # recovery timer deadline, optional
      "next_action": "reassign",
      "outcome": "pending"
    }

Only ``needs_human``/``human_reason`` and ``next_at``/``next_action`` take part
in ownership; the rest belongs to the policy and report layers. A top-level
``needs_human`` latch is read too, so a caller does not have to nest it.

``deadline`` is an aware UTC datetime or ``None``. A live worker uses its run
start plus ``CONDUCTOR_DISPATCH_TIMEOUT_SECONDS`` (default 600) when the
snapshot carries no explicit deadline; a review uses
``CONDUCTOR_DISPATCH_REVIEW_TIMEOUT_SECONDS`` (default 900); a queued request
uses ``CONDUCTOR_DISPATCH_QUEUED_TIMEOUT_SECONDS`` (default 600). A scheduled
review's deadline is its ``next_at``, and a recovery timer's deadline is its
``next_at``. ``needs_human`` has no deadline by design: only a person can end
it. A non-human owner with no deadline evidence is reported ``expired`` so the
sweep acts instead of waiting forever, but it is not ``ownerless``.

Everything here is pure: no store, launcher, clock read or sleep. The caller
passes ``now`` and the snapshot; the snapshot is never mutated.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

#: The key under one line's gates snapshot.
LIVENESS_KEY = "liveness"

WORKER_TIMEOUT_ENV = "CONDUCTOR_DISPATCH_TIMEOUT_SECONDS"
REVIEW_TIMEOUT_ENV = "CONDUCTOR_DISPATCH_REVIEW_TIMEOUT_SECONDS"
QUEUED_TIMEOUT_ENV = "CONDUCTOR_DISPATCH_QUEUED_TIMEOUT_SECONDS"

#: Fallbacks mirror the dispatch defaults without importing the server package.
DEFAULT_WORKER_TIMEOUT_SECONDS = 600.0
DEFAULT_REVIEW_TIMEOUT_SECONDS = 900.0
DEFAULT_QUEUED_TIMEOUT_SECONDS = 600.0

OWNER_WORKER_RUN = "worker_run"
OWNER_SCHEDULED_REVIEW = "scheduled_review"
OWNER_RECOVERY_TIMER = "recovery_timer"
OWNER_QUEUED_REQUEST = "queued_request"
OWNER_NEEDS_HUMAN = "needs_human"
OWNER_APP_LEASE = "app_lease"
OWNER_NONE = "none"

#: Public aliases: the values are data and callers name them both ways.
WORKER_RUN = OWNER_WORKER_RUN
SCHEDULED_REVIEW = OWNER_SCHEDULED_REVIEW
RECOVERY_TIMER = OWNER_RECOVERY_TIMER
QUEUED_REQUEST = OWNER_QUEUED_REQUEST
NEEDS_HUMAN = OWNER_NEEDS_HUMAN
APP_LEASE = OWNER_APP_LEASE
NONE = OWNER_NONE

OWNERS = frozenset(
    {
        OWNER_WORKER_RUN,
        OWNER_SCHEDULED_REVIEW,
        OWNER_RECOVERY_TIMER,
        OWNER_QUEUED_REQUEST,
        OWNER_NEEDS_HUMAN,
        OWNER_APP_LEASE,
        OWNER_NONE,
    }
)
OWNER_KINDS = OWNERS

#: A line in one of these states is finished; it needs no owner.
TERMINAL_STATUSES = frozenset(
    {
        "PASSED",
        "FAILED",
        "CANCELLED",
        "CANCELED",
        "ABORTED",
        "COMPLETE",
        "COMPLETED",
        "SKIPPED",
        "SUPERSEDED",
    }
)

_DEADLINE_KEYS = ("deadline", "deadline_at", "expires_at", "timeout_at", "due_at")
_START_KEYS = ("started_at", "start_at", "launched_at", "created_at", "at")
_RUN_START_KEYS = (
    "started_at",
    "start_at",
    "launched_at",
    "run_started_at",
    "worker_started_at",
)
_REVIEW_START_KEYS = ("started_at", "start_at", "review_started_at")
_QUEUED_START_KEYS = ("requested_at", "queued_at", "created_at", "at")
_REVIEW_SCHEDULE_KEYS = ("next_at", "retry_at", "at", "due_at")
_LIVENESS_TIMER_KEYS = ("next_at", "deadline", "deadline_at", "due_at")
_STABLE_START_KEYS = ("updated_at", "parked_at", "created_at", "started_at")

_TRUE = frozenset(
    {
        "1",
        "true",
        "yes",
        "on",
        "alive",
        "running",
        "active",
        "launched",
        "started",
        "pending",
        "queued",
        "scheduled",
        "requested",
    }
)
_FALSE = frozenset(
    {
        "",
        "0",
        "false",
        "no",
        "off",
        "dead",
        "exited",
        "stopped",
        "failed",
        "finished",
        "done",
        "cancelled",
        "canceled",
        "complete",
        "completed",
        "none",
        "null",
    }
)
_ALIVE_STATES = frozenset({"alive", "running", "launched", "started", "active"})
_DEAD_STATES = frozenset(
    {"dead", "exited", "stopped", "failed", "finished", "done", "complete"}
)


@dataclass(frozen=True, slots=True)
class OwnerDecision:
    """One owner, its deadline, and what the sweep should do with the line.

    ``ownerless`` means no owner candidate was found on an unfinished line;
    ``terminal`` means the line is finished and therefore needs no owner.
    ``expired`` is true when a deadline is behind the clock, and also when a
    non-human owner has no deadline evidence at all: a never-expiring owner
    would be the stuck line N76 exists to remove.
    """

    owner: str
    deadline: datetime | None = None
    expired: bool = False
    ownerless: bool = False
    reason: str = ""
    detail: str = ""
    terminal: bool = False

    @property
    def kind(self) -> str:
        """The owner under the name the older code used."""
        return self.owner

    @property
    def is_expired(self) -> bool:
        return self.expired

    @property
    def is_ownerless(self) -> bool:
        return self.ownerless

    @property
    def is_human(self) -> bool:
        return self.owner == OWNER_NEEDS_HUMAN

    @property
    def active(self) -> bool:
        """Whether a live owner still holds the line."""
        return not self.terminal and not self.ownerless and not self.expired

    @property
    def deadline_text(self) -> str | None:
        """The deadline in the record layer's ISO form, for display/JSON."""
        return format_time(self.deadline)

    def to_dict(self) -> dict[str, object]:
        """A JSON-safe view for ``dispatch status`` and tests."""
        raw: dict[str, object] = {
            "owner": self.owner,
            "deadline": self.deadline_text,
            "expired": self.expired,
            "ownerless": self.ownerless,
        }
        if self.reason:
            raw["reason"] = self.reason
        if self.detail:
            raw["detail"] = self.detail
        if self.terminal:
            raw["terminal"] = True
        return raw


#: A descriptive alias for callers that call the value an owner.
Owner = OwnerDecision
Decision = OwnerDecision


def parse_time(value: object) -> datetime | None:
    """A stored timestamp (ISO string, epoch seconds, or datetime) as UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, bool):
        return None
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_time(moment: datetime | str | float | int | None) -> str | None:
    """UTC ``YYYY-MM-DDTHH:MM:SS+00:00``; invalid input becomes ``None``."""
    parsed = parse_time(moment)
    if parsed is None:
        return None
    return parsed.replace(microsecond=0).isoformat()


def as_utc(value: datetime | str | float | int | None = None) -> datetime:
    """``value`` as an aware UTC datetime; ``now`` when it is not a timestamp."""
    parsed = parse_time(value)
    return parsed if parsed is not None else datetime.now(timezone.utc)


def _text(value: object) -> str:
    return str(value) if value else ""


def _status(snapshot: Mapping[str, Any]) -> str:
    raw = snapshot.get("status")
    if isinstance(raw, Enum):
        raw = raw.value
    return _text(raw).strip().upper()


def _optional_bool(value: object) -> bool | None:
    """A bool-like value, or None when the value is not a marker at all."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    return None


def _latch(value: object) -> bool:
    """``needs_human`` may be ``True`` or a reason string; both mean latched."""
    marker = _optional_bool(value)
    if marker is not None:
        return marker
    return bool(value)


def _state_alive(mapping: Mapping[str, Any]) -> bool | None:
    """Read a launch mapping's alive marker.

    A bare pid or run id is identity, not liveness: the process may be gone and
    the record stale. Only an explicit alive/running marker or a live state
    counts as positive evidence; an absent marker returns ``None``.
    """
    alive = _optional_bool(mapping.get("alive"))
    if alive is None:
        alive = _optional_bool(mapping.get("running"))
    if alive is None:
        state = _text(mapping.get("state") or mapping.get("status")).strip().lower()
        if state in _ALIVE_STATES:
            alive = True
        elif state in _DEAD_STATES:
            alive = False
    return alive


def _first_time(
    mapping: Mapping[str, Any] | None, keys: tuple[str, ...]
) -> datetime | None:
    if not isinstance(mapping, Mapping):
        return None
    for key in keys:
        parsed = parse_time(mapping.get(key))
        if parsed is not None:
            return parsed
    return None


def _deadline_from_start(
    started: datetime | None, seconds: float
) -> datetime | None:
    if started is None:
        return None
    return started + timedelta(seconds=seconds)


def _positive_seconds(
    env: Mapping[str, str], name: str, default: float
) -> float:
    """The env override when it is a positive finite number, else ``default``."""
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or value <= 0:
        return default
    return value


def _timeout(
    env: Mapping[str, str] | None, name: str, default: float
) -> float:
    source: Mapping[str, str] = os.environ if env is None else env
    return _positive_seconds(source, name, default)


def worker_timeout_seconds(env: Mapping[str, str] | None = None) -> float:
    """The worker-run bound used when the snapshot has no explicit deadline."""
    return _timeout(env, WORKER_TIMEOUT_ENV, DEFAULT_WORKER_TIMEOUT_SECONDS)


def review_timeout_seconds(env: Mapping[str, str] | None = None) -> float:
    """The review bound used when the snapshot has no explicit deadline."""
    return _timeout(env, REVIEW_TIMEOUT_ENV, DEFAULT_REVIEW_TIMEOUT_SECONDS)


def queued_timeout_seconds(env: Mapping[str, str] | None = None) -> float:
    """The queued-request bound used when the snapshot has no deadline."""
    return _timeout(env, QUEUED_TIMEOUT_ENV, DEFAULT_QUEUED_TIMEOUT_SECONDS)


@dataclass(frozen=True, slots=True)
class _RunView:
    present: bool
    alive: bool
    started: datetime | None
    deadline: datetime | None


def _run_view(snapshot: Mapping[str, Any]) -> _RunView:
    raw = snapshot.get("current_run")
    if raw is None:
        raw = snapshot.get("worker_run")
    if raw is None:
        raw = snapshot.get("run")

    present = False
    alive: bool | None = None
    started: datetime | None = None
    deadline: datetime | None = None

    if isinstance(raw, Mapping):
        present = bool(raw)
        run_id = _text(raw.get("id") or raw.get("run_id") or raw.get("current_run"))
        if run_id:
            present = True
        alive = _state_alive(raw)
        started = _first_time(raw, _RUN_START_KEYS)
        deadline = _first_time(raw, _DEADLINE_KEYS)
    elif raw:
        present = True
        alive = None

    for key in ("run_alive", "launch_alive", "worker_alive", "alive"):
        marker = _optional_bool(snapshot.get(key))
        if marker is not None:
            alive = marker
            break

    launch = snapshot.get("launch")
    if isinstance(launch, Mapping):
        if alive is None:
            alive = _state_alive(launch)
        started = started or _first_time(launch, _RUN_START_KEYS)
        deadline = deadline or _first_time(launch, _DEADLINE_KEYS)

    if alive is None:
        # ``current_run``/``pid`` is the durable identity of an attempt, not
        # proof the launch is still alive. Without positive liveness evidence
        # the attempt may be stale, so it does not own the line: a recovery
        # timer or the sweep must take it instead.
        alive = False
    return _RunView(present=present, alive=bool(alive), started=started, deadline=deadline)


@dataclass(frozen=True, slots=True)
class _ReviewView:
    present: bool
    running: bool
    started: datetime | None
    scheduled: datetime | None
    deadline: datetime | None


def _review_view(snapshot: Mapping[str, Any]) -> _ReviewView:
    raw_retry = snapshot.get("review_retry")
    retry: Mapping[str, Any] | None = (
        raw_retry if isinstance(raw_retry, Mapping) and raw_retry else None
    )
    raw_review = snapshot.get("review")
    review: Mapping[str, Any] | None = (
        raw_review if isinstance(raw_review, Mapping) and raw_review else None
    )

    running = _optional_bool(snapshot.get("review_running"))
    if running is None:
        running = _optional_bool(snapshot.get("review_in_progress"))
    if running is None and review is not None:
        running = _state_alive(review)

    # A running review's own mapping is authoritative for its start and
    # deadline. A stale ``review_retry`` from an earlier attempt must not date
    # the new attempt, or a freshly started review looks expired immediately.
    active = review if running and review is not None else None
    source = active if active is not None else (retry if retry is not None else review)

    scheduled = None
    if source is not None:
        scheduled = _first_time(source, _REVIEW_SCHEDULE_KEYS)
        if scheduled is None:
            scheduled = _first_time(source, _DEADLINE_KEYS)

    started = _first_time(source, _REVIEW_START_KEYS)
    deadline = _first_time(source, _DEADLINE_KEYS)

    present = bool(running) or scheduled is not None or retry is not None
    return _ReviewView(
        present=present,
        running=bool(running),
        started=started,
        scheduled=scheduled,
        deadline=deadline,
    )


@dataclass(frozen=True, slots=True)
class _QueuedView:
    present: bool
    started: datetime | None
    deadline: datetime | None


def _queued_view(snapshot: Mapping[str, Any]) -> _QueuedView:
    for key in ("queued_request", "pending_request", "operator_request", "queued"):
        if key not in snapshot:
            continue
        raw = snapshot.get(key)
        if isinstance(raw, Mapping):
            if not raw:
                continue
            marker = _optional_bool(raw.get("pending"))
            if marker is None:
                marker = _optional_bool(raw.get("queued"))
            if marker is None:
                marker = _optional_bool(raw.get("state") or raw.get("status"))
            if marker is False:
                continue
            started = _first_time(raw, _QUEUED_START_KEYS)
            deadline = _first_time(raw, _DEADLINE_KEYS)
            return _QueuedView(True, started, deadline)
        marker = _optional_bool(raw)
        if marker is False:
            continue
        if marker is True or raw:
            return _QueuedView(True, None, None)
    return _QueuedView(False, None, None)


#: The snapshot key the machine fills from ``app_lines.liveness_overlay``.
APP_LEASE_KEY = "app_lease"
#: Lease states that still hold the line (``app_lines.LIVE``).
_APP_LEASE_LIVE = frozenset({"active", "reported"})


def _epoch(value: object) -> datetime | None:
    """An epoch-second number as UTC; bools, strings and non-finite are None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return parse_time(value)


def _app_lease_deadline(snapshot: Mapping[str, Any]) -> datetime | None:
    """The live app lease's deadline, or ``None`` when there is no live lease.

    A missing, non-live or malformed ``app_lease`` is treated as absent so a
    snapshot without one decides exactly as before N80 L4.
    """
    raw = snapshot.get(APP_LEASE_KEY)
    if not isinstance(raw, Mapping):
        return None
    state = raw.get("state")
    if not isinstance(state, str) or state.strip().lower() not in _APP_LEASE_LIVE:
        return None
    times = [
        moment
        for moment in (_epoch(raw.get("expires_at")), _epoch(raw.get("max_lease_until")))
        if moment is not None
    ]
    return min(times) if times else None


def _human_label(value: object) -> str:
    """A reason string hidden in a truthy latch, or ``""`` for a plain bool."""
    if isinstance(value, Mapping):
        return _text(value.get("reason") or value.get("human_reason"))
    text = _text(value).strip()
    if not text or text.lower() in _TRUE or text.lower() in _FALSE:
        return ""
    return text


def _human_reason(
    snapshot: Mapping[str, Any], liveness: Mapping[str, Any] | None
) -> str | None:
    if _latch(snapshot.get("needs_human")):
        return (
            _text(snapshot.get("human_reason"))
            or _human_label(snapshot.get("needs_human"))
            or _text(snapshot.get("failure_reason"))
            or OWNER_NEEDS_HUMAN
        )
    if liveness is not None:
        outcome = _text(liveness.get("outcome")).strip().lower()
        if _latch(liveness.get("needs_human")) or outcome == OWNER_NEEDS_HUMAN:
            return (
                _text(liveness.get("human_reason"))
                or _human_label(liveness.get("needs_human"))
                or _text(liveness.get("reason"))
                or OWNER_NEEDS_HUMAN
            )
    return None


def _generic_deadline(snapshot: Mapping[str, Any]) -> datetime | None:
    return _first_time(snapshot, _DEADLINE_KEYS)


def _stable_start(snapshot: Mapping[str, Any]) -> datetime | None:
    return _first_time(snapshot, _STABLE_START_KEYS)


def _decision(
    owner: str,
    deadline: datetime | None,
    now: datetime,
    *,
    reason: str = "",
    detail: str = "",
) -> OwnerDecision:
    if deadline is None:
        # A non-human owner with no deadline evidence is already past due: the
        # sweep must supply one instead of inventing a deadline from ``now``,
        # which would move forward on every tick and never expire.
        expired = True
    else:
        expired = deadline <= now
    return OwnerDecision(
        owner=owner,
        deadline=deadline,
        expired=expired,
        ownerless=False,
        reason=reason,
        detail=detail,
    )


def decide_owner(
    snapshot: Mapping[str, Any],
    now: datetime | str | float | int | None = None,
    env: Mapping[str, str] | None = None,
) -> OwnerDecision:
    """The single owner of one line at ``now``, with its deadline.

    ``snapshot`` is a plain mapping: the persisted line record (``status``,
    ``current_run``, ``failure_reason``, ...) merged with the line's gates
    snapshot (``review_retry``, ``liveness``). Nothing is mutated.

    ``now`` defaults to the current UTC time but tests should pass it. ``env``
    defaults to ``os.environ``; pass a mapping to keep timeout fallbacks
    deterministic.
    """
    if not isinstance(snapshot, Mapping):
        raise TypeError("line snapshot must be a mapping")

    moment = as_utc(now)
    source: Mapping[str, str] = os.environ if env is None else env

    if _optional_bool(snapshot.get("terminal")) is True:
        return OwnerDecision(OWNER_NONE, reason="terminal", terminal=True)
    if _status(snapshot) in TERMINAL_STATUSES:
        return OwnerDecision(OWNER_NONE, reason="terminal", terminal=True)

    raw_liveness = snapshot.get(LIVENESS_KEY)
    liveness: Mapping[str, Any] | None = (
        raw_liveness if isinstance(raw_liveness, Mapping) else None
    )

    human = _human_reason(snapshot, liveness)
    if human is not None:
        return OwnerDecision(OWNER_NEEDS_HUMAN, deadline=None, reason=human)

    queued = _queued_view(snapshot)
    if queued.present:
        timeout = queued_timeout_seconds(source)
        deadline = (
            queued.deadline
            or _first_time(snapshot, ("queued_deadline", "request_deadline"))
            or _deadline_from_start(queued.started, timeout)
            or _deadline_from_start(
                _first_time(
                    snapshot, ("queued_started_at", "queued_at", "requested_at")
                ),
                timeout,
            )
            or _generic_deadline(snapshot)
            or _deadline_from_start(_stable_start(snapshot), timeout)
        )
        return _decision(
            OWNER_QUEUED_REQUEST, deadline, moment, reason="operator request queued"
        )

    lease_deadline = _app_lease_deadline(snapshot)
    if lease_deadline is not None:
        app_id = snapshot[APP_LEASE_KEY].get("app_id")
        return _decision(
            OWNER_APP_LEASE,
            lease_deadline,
            moment,
            reason="app lease is live",
            detail=_text(app_id),
        )

    run = _run_view(snapshot)
    if run.present and run.alive:
        timeout = worker_timeout_seconds(source)
        deadline = (
            run.deadline
            or _first_time(
                snapshot, ("worker_deadline", "run_deadline", "run_deadline_at")
            )
            or _deadline_from_start(
                run.started
                or _first_time(
                    snapshot,
                    ("run_started_at", "run_start_at", "worker_started_at"),
                ),
                timeout,
            )
            or _generic_deadline(snapshot)
            or _deadline_from_start(_stable_start(snapshot), timeout)
        )
        return _decision(
            OWNER_WORKER_RUN, deadline, moment, reason="worker run is alive"
        )

    review = _review_view(snapshot)
    if review.present:
        timeout = review_timeout_seconds(source)
        if review.running:
            deadline = (
                review.deadline
                or _first_time(snapshot, ("review_deadline", "review_deadline_at"))
                or _deadline_from_start(
                    review.started
                    or _first_time(
                        snapshot, ("review_started_at", "review_start_at")
                    ),
                    timeout,
                )
                or _generic_deadline(snapshot)
                or _deadline_from_start(_stable_start(snapshot), timeout)
            )
            return _decision(
                OWNER_SCHEDULED_REVIEW,
                deadline,
                moment,
                reason="review is running",
            )
        deadline = (
            review.scheduled
            or review.deadline
            or _first_time(snapshot, ("review_deadline", "review_deadline_at"))
            or _deadline_from_start(review.started, timeout)
            or _generic_deadline(snapshot)
            or _deadline_from_start(_stable_start(snapshot), timeout)
        )
        return _decision(
            OWNER_SCHEDULED_REVIEW,
            deadline,
            moment,
            reason="review is scheduled",
        )

    if liveness is not None:
        next_at = _first_time(liveness, _LIVENESS_TIMER_KEYS)
        if next_at is not None:
            return _decision(
                OWNER_RECOVERY_TIMER,
                next_at,
                moment,
                reason=_text(liveness.get("next_action")) or "liveness recovery",
            )

    return OwnerDecision(OWNER_NONE, ownerless=True, reason="no owner")


def needs_recovery(
    decision: OwnerDecision, now: datetime | str | float | int | None = None
) -> bool:
    """Whether the sweep should take this line.

    The sweep never takes a terminal line or a person's line. An unowned line,
    an ownerless line, a line whose deadline has passed, and a non-human owner
    with no deadline are all the sweep's work.
    """
    if decision.terminal or decision.owner == OWNER_NEEDS_HUMAN:
        return False
    if decision.owner == OWNER_NONE or decision.ownerless:
        return True
    if decision.deadline is None:
        return True
    if now is None:
        return decision.expired
    return as_utc(now) >= decision.deadline


def determine_owner(
    snapshot: Mapping[str, Any],
    now: datetime | str | float | int | None = None,
    env: Mapping[str, str] | None = None,
) -> OwnerDecision:
    """Alias for :func:`decide_owner`, kept for the older call site."""
    return decide_owner(snapshot, now, env=env)


__all__ = [
    "APP_LEASE",
    "APP_LEASE_KEY",
    "DEFAULT_QUEUED_TIMEOUT_SECONDS",
    "DEFAULT_REVIEW_TIMEOUT_SECONDS",
    "DEFAULT_WORKER_TIMEOUT_SECONDS",
    "Decision",
    "LIVENESS_KEY",
    "NEEDS_HUMAN",
    "NONE",
    "OWNERS",
    "OWNER_APP_LEASE",
    "OWNER_KINDS",
    "OWNER_NEEDS_HUMAN",
    "OWNER_NONE",
    "OWNER_QUEUED_REQUEST",
    "OWNER_RECOVERY_TIMER",
    "OWNER_SCHEDULED_REVIEW",
    "OWNER_WORKER_RUN",
    "Owner",
    "OwnerDecision",
    "QUEUED_REQUEST",
    "QUEUED_TIMEOUT_ENV",
    "RECOVERY_TIMER",
    "REVIEW_TIMEOUT_ENV",
    "SCHEDULED_REVIEW",
    "TERMINAL_STATUSES",
    "WORKER_RUN",
    "WORKER_TIMEOUT_ENV",
    "as_utc",
    "decide_owner",
    "determine_owner",
    "format_time",
    "needs_recovery",
    "parse_time",
    "queued_timeout_seconds",
    "review_timeout_seconds",
    "worker_timeout_seconds",
]
