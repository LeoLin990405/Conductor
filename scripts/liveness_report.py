#!/usr/bin/env python3
"""Read-only N76 liveness report for auto-dispatch runs.

The report answers the five questions PRD ``n76`` section 4.6 asks of a week
of ``shadow`` records:

* how many dispatches reached APPROVAL;
* which reasons parked a line, for how long, and whether an automatic step
  recovered it;
* how often a line needed a person, and how long the person took;
* which lines were split off and whether their follow-up completed;
* how long reviews waited and how often a review was unparseable.

The input is a dispatch-rows JSON export: a JSON array of rows, a JSON object
with a ``dispatches`` / ``items`` / ``rows`` / ``data`` list, or JSON Lines.
Rows may be API summaries (``id``) or database rows (``dispatch_id``); the
``lines``, ``outcomes`` and ``gates`` columns may themselves be JSON strings.

Two read-only sources are supported. Exactly one is required::

    python scripts/liveness_report.py --from-json /tmp/dispatches.json --json
    python scripts/liveness_report.py --api http://127.0.0.1:8000/api/v1/dispatch/status/active

``--api`` issues one plain HTTP GET and never a POST. The script only reads:
it never writes a file, a database row, or anything through the API. Output
goes to stdout as text, or as JSON with ``--json``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

#: A split-off line is FAILED with this reason, and is also listed under
#: ``outcomes.split_off`` by the dispatch state machine.
SPLIT_OFF = "split_off"
REVIEW_UNPARSEABLE = "review_unparseable"

#: The liveness record's outcomes. ``pending`` still has an owner.
PENDING = "pending"
RECOVERED = "recovered"
NEEDS_HUMAN = "needs_human"
#: A step result. Only ``applied`` means an automatic action actually ran.
APPLIED = "applied"

_APPROVAL_STATES = frozenset({"APPROVAL", "MERGE", "COMPLETE"})
_APPROVAL_RESULTS = frozenset({"pass", "passed", "approved", "approval"})
#: A G6 rejection is only written after the approval gate opened, so it is
#: evidence of reaching APPROVAL, not of never getting there.
_APPROVAL_FAILURES = frozenset({"fail", "failed", "reject", "rejected"})
_APPROVAL_REASONS = frozenset({"awaiting_human", "human_approved", "human_rejected"})
_COMPLETED_WORDS = frozenset({"complete", "completed", "done", "merged", "closed"})
_PENDING_WORDS = frozenset(
    {"pending", "open", "draft", "drafted", "not_started", "unstarted", "todo"}
)
#: Stages where a line is waiting on a reviewer. Spec review is G0, not this queue.
_REVIEW_STAGES = frozenset({"primary_review", "secondary_review"})


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def parse_time(value: object) -> datetime | None:
    """A stored timestamp (ISO string, epoch seconds, or datetime) as UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=UTC)
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
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_time(moment: datetime | str | float | int | None) -> str | None:
    """A timestamp as UTC ``YYYY-MM-DDTHH:MM:SS+00:00`` (or ``None``)."""
    parsed = parse_time(moment)
    if parsed is None:
        return None
    return parsed.replace(microsecond=0).isoformat()


def _as_dict(value: object) -> dict[str, Any]:
    """A row/line/record as a dict, including dataclass-like objects."""
    if isinstance(value, dict):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        return dict(result) if isinstance(result, dict) else {}
    return {}


def _coerce_json(value: object) -> object:
    """Decode a JSONB column that an export left as text."""
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def _status(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().upper()


def _text(value: object) -> str:
    return str(value or "").strip()


def _int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Parking events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParkEvent:
    """One line's stay in a parked state, from a liveness record or the line."""

    dispatch_id: str
    line_id: str
    reason: str
    parked_at: str | None
    resolved_at: str | None
    outcome: str
    steps_applied: int = 0
    #: Refused/error/skipped steps in this episode. They spent budget without
    #: recovering the line, so they belong in the success-rate denominator.
    failed_steps: int = 0
    needs_human: bool = False
    human_at: str | None = None
    human_handled_at: str | None = None

    def duration(self, now: datetime) -> float | None:
        """Seconds parked, or ``None`` when the start time is unknown.

        An open automatic park runs to ``now``. A human park runs to the
        person's handling time, or to ``now`` while it is still open; the
        notification time is not a resolution.
        """
        start = parse_time(self.parked_at)
        if start is None:
            return None
        end = parse_time(self.resolved_at)
        if self.needs_human:
            # Handling closes every copy of the handoff. A re-park stamp on
            # an archived episode is not the person finishing, and an open
            # copy must not keep running after a sibling recorded handling.
            handled = parse_time(self.human_handled_at)
            if handled is not None:
                end = handled
            elif end is None:
                end = now
        elif end is None and self.outcome == PENDING:
            end = now
        if end is None:
            return None
        return max(0.0, (end - start).total_seconds())


@dataclass(frozen=True)
class _ReviewWait:
    seconds: float
    start: datetime | None = None
    end: datetime | None = None
    #: A Stage-Gate visit. A retry timer that falls inside one is the same
    #: episode and must not become a second, shorter sample.
    episode: bool = False


@dataclass
class _Accumulator:
    now: datetime
    followups_dir: Path | None = None
    dispatches: list[dict[str, Any]] = field(default_factory=list)
    parks: list[ParkEvent] = field(default_factory=list)
    split_lines: list[tuple[str, str]] = field(default_factory=list)
    split_info: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    review_waits: list[_ReviewWait] = field(default_factory=list)
    review_retries: int = 0
    #: Real steps ``spent`` still counts after history eviction, when more
    #: than one episode exists and the trail does not say which stay it was.
    unattributed_attempts: int = 0


def _gates_lines(gates: dict[str, Any]) -> dict[str, Any]:
    lines = gates.get("lines")
    return lines if isinstance(lines, dict) else {}


def _gates_line(gates: dict[str, Any], line_id: str) -> dict[str, Any]:
    snap = _gates_lines(gates).get(line_id)
    return _as_dict(snap)


#: A shadow step re-states what the sweep would do; it never acts.
SHADOW = "shadow"


def _counted_steps(raw_steps: object) -> dict[int, tuple[int, int]]:
    """``episode -> (applied, failed)`` for a record's bounded step trail.

    ``history`` holds every recorded step, including ``shadow`` observations.
    A shadow step did not act, so it is neither an attempt nor a failure.
    Every other non-applied result (``refused``, ``error``, ``skipped``) spent
    budget without recovering the line, so it has to reach the success-rate
    denominator; counting only applied steps would call a week of failures
    "no attempts" and report 100%.
    """
    counts: dict[int, list[int]] = {}
    if not isinstance(raw_steps, list):
        return {}
    for raw in raw_steps:
        step = _as_dict(raw)
        result = _text(step.get("result")).lower()
        if not result or result == SHADOW:
            continue
        episode = _int(step.get("episode")) or 1
        bucket = counts.setdefault(episode, [0, 0])
        if result == APPLIED:
            bucket[0] += 1
        else:
            bucket[1] += 1
    return {episode: (applied, failed) for episode, (applied, failed) in counts.items()}


def _steps_applied(raw_steps: object, episode: int) -> int:
    """Applied automatic steps in one parking episode."""
    return _counted_steps(raw_steps).get(episode, (0, 0))[0]


def _cover_evicted_attempts(
    events: list[ParkEvent],
    spent: int,
    oldest_kept: int | None = None,
    numbers: list[int] | None = None,
) -> tuple[list[ParkEvent], int]:
    """Keep a real attempt after bounded history has dropped its step.

    ``history`` keeps a fixed tail and that tail counts shadow observations,
    so one error, a re-park, and a full tail of shadows leaves ``spent`` at 1
    with no failed step anywhere in the record. ``spent`` excludes shadows
    and is not trimmed. Dropping it removes the attempt from the denominator,
    and the next recovery then reports a perfect success rate.

    A record with one stay can only have lost that step from that stay, so
    the step is attributed there. A record with several stays does not say
    which summary lost the step. Painting it onto the oldest empty episode
    turns a recovered ``no_new_commit`` that applied nothing into an
    automatic success. Those steps stay unattributed: they count as attempts
    that did not recover, and they are not filed under a reason.

    The success-rate denominator is in episode units. A lost step can only
    add an attempted episode when some stay has no counted step yet; when
    every stay already counts as attempted, the step belonged to one of
    them and adds nothing. Otherwise the lost steps add one attempted stay
    that did not recover; the report does not guess at more.

    History drops its oldest entries first, so a lost step is no newer than
    the oldest entry still kept (``oldest_kept``). An empty stay after that
    episode cannot own it: any real step of that stay would still be kept.
    ``numbers`` gives each event's episode number, in the same order.
    """
    if spent <= 0 or not events:
        return events, 0
    accounted = sum(event.steps_applied + event.failed_steps for event in events)
    missing = spent - accounted
    if missing <= 0:
        return events, 0
    if len(events) == 1:
        event = events[0]
        if event.steps_applied == 0 and event.failed_steps == 0:
            # One step is enough for the stay to count. Further trimmed
            # steps on that same stay are not extra parks.
            return [replace(event, failed_steps=1)], 0
        return events, 0
    if numbers is None or len(numbers) != len(events):
        numbers = []
        oldest_kept = None
    candidates = [
        event
        for index, event in enumerate(events)
        if not (event.steps_applied or event.failed_steps)
        and (oldest_kept is None or numbers[index] <= oldest_kept)
    ]
    if not candidates:
        return events, 0
    return events, 1


def _record_events(
    dispatch_id: str,
    line_id: str,
    raw: object,
    line_reason: str,
) -> tuple[list[ParkEvent], int]:
    """Every parking episode a ``gates.lines[id].liveness`` record holds.

    The record keeps bounded summaries of closed episodes plus the current
    one. Each is its own park, so a line that re-parked under a new reason is
    not counted only under its latest reason.
    """
    data = _as_dict(raw)
    if not data:
        return [], 0
    reason = _text(data.get("reason")) or line_reason
    outcome = _text(data.get("outcome")).lower() or PENDING
    needs_human = bool(data.get("needs_human")) or outcome == NEEDS_HUMAN
    human_at = data.get("human_at") or data.get("needs_human_at")
    handled_at = data.get("human_handled_at") or data.get("handled_at")
    updated_at = data.get("updated_at")

    # The L1 producer writes the bounded trail under ``history``; older
    # exports used ``steps``. Count both so a refused/error attempt is seen.
    history = data.get("history")
    if not isinstance(history, list):
        history = data.get("steps")
    counted = _counted_steps(history)

    events: list[ParkEvent] = []
    numbers: list[int] = []
    episodes = data.get("episodes")
    if isinstance(episodes, list):
        for raw_episode in episodes:
            episode = _as_dict(raw_episode)
            if not episode:
                continue
            number = _int(episode.get("episode")) or 1
            applied, failed = counted.get(number, (0, 0))
            inline = episode.get("history")
            if not isinstance(inline, list):
                inline = episode.get("steps")
            inline_counts = _counted_steps(inline).get(number, (0, 0))
            applied = applied or inline_counts[0]
            failed = failed or inline_counts[1]
            episode_human = bool(episode.get("needs_human")) or (
                _text(episode.get("outcome")).lower() == NEEDS_HUMAN
            )
            episode_handled = episode.get("human_handled_at") or episode.get(
                "handled_at"
            )
            resolved = episode.get("resolved_at") or episode.get("updated_at")
            if episode_human:
                resolved = episode_handled or None
            numbers.append(number)
            events.append(
                ParkEvent(
                    dispatch_id=dispatch_id,
                    line_id=line_id,
                    reason=_text(episode.get("reason")) or reason,
                    parked_at=(
                        _text(episode.get("parked_at"))
                        or _text(episode.get("started_at"))
                        or None
                    ),
                    resolved_at=_text(resolved) or None,
                    outcome=_text(episode.get("outcome")).lower()
                    or (NEEDS_HUMAN if episode_human else PENDING),
                    steps_applied=_int(episode.get("steps_applied")) or applied,
                    failed_steps=failed,
                    needs_human=episode_human,
                    human_at=_text(episode.get("human_at")) or None,
                    human_handled_at=_text(episode_handled) or None,
                )
            )

    current_episode = _int(data.get("episode")) or 1
    applied, failed = counted.get(current_episode, (0, 0))
    applied = _int(data.get("steps_applied")) or applied
    resolved = handled_at if needs_human else None
    if not needs_human and outcome != PENDING:
        resolved = updated_at
    parked_at = data.get("parked_at") or data.get("updated_at") or data.get("at")
    if reason or parked_at:
        numbers.append(current_episode)
        events.append(
            ParkEvent(
                dispatch_id=dispatch_id,
                line_id=line_id,
                reason=reason,
                parked_at=_text(parked_at) or None,
                resolved_at=_text(resolved) or None,
                outcome=outcome,
                steps_applied=applied,
                failed_steps=failed,
                needs_human=needs_human,
                human_at=_text(human_at) or None,
                human_handled_at=_text(handled_at) or None,
            )
        )
    kept = [
        _int(_as_dict(step).get("episode")) or 1
        for step in (history if isinstance(history, list) else [])
        if _as_dict(step)
    ]
    covered, unattributed = _cover_evicted_attempts(
        events,
        _int(data.get("spent")),
        oldest_kept=min(kept) if kept else None,
        numbers=numbers,
    )
    return _share_handoff_handling(covered), unattributed


def _share_handoff_handling(events: list[ParkEvent]) -> list[ParkEvent]:
    """Give every copy of one handoff the handling time any copy recorded.

    Re-parking stores the same ``needs_human`` latch and ``human_at`` on the
    archived episode and on the current one. Only the current copy usually
    gains ``human_handled_at``. The archived copy then has no end of its own,
    so its parking clock keeps running after the person has handled it.
    """
    handled_at: dict[tuple[str, str, str], str] = {}
    for event in events:
        if event.needs_human and event.human_handled_at:
            key = (
                event.dispatch_id,
                event.line_id,
                event.human_at or event.parked_at or "",
            )
            handled_at.setdefault(key, event.human_handled_at)
    if not handled_at:
        return events
    shared: list[ParkEvent] = []
    for event in events:
        if not event.needs_human or event.human_handled_at:
            shared.append(event)
            continue
        key = (event.dispatch_id, event.line_id, event.human_at or event.parked_at or "")
        stamp = handled_at.get(key)
        if stamp is None:
            shared.append(event)
            continue
        shared.append(
            replace(event, human_handled_at=stamp, resolved_at=event.resolved_at or stamp)
        )
    return shared


def _fallback_event(
    dispatch_id: str,
    line_id: str,
    line: dict[str, Any],
) -> ParkEvent | None:
    """A BLOCKED line with a reason but no liveness record.

    Older rows, and rows written while liveness was off, still carry the
    reason on the line. They count as a park but contribute no duration.
    """
    status = _status(line.get("status"))
    reason = _text(line.get("failure_reason"))
    if status != "BLOCKED" or not reason:
        return None
    needs_human = bool(line.get("needs_human"))
    return ParkEvent(
        dispatch_id=dispatch_id,
        line_id=line_id,
        reason=reason,
        parked_at=_text(line.get("parked_at")) or None,
        resolved_at=_text(line.get("resolved_at")) or None,
        outcome=_text(line.get("liveness_outcome")).lower()
        or (NEEDS_HUMAN if needs_human else PENDING),
        steps_applied=_int(line.get("steps_applied")),
        needs_human=needs_human,
        human_at=_text(line.get("human_at")) or None,
        human_handled_at=_text(line.get("human_handled_at")) or None,
    )


def _human_handoffs(parks: Iterable[ParkEvent]) -> list[ParkEvent]:
    """One record per request to a person, not one per parking episode.

    A liveness record keeps bounded episode summaries plus the current one.
    When the line re-parks while a person already owns it, the record
    producer preserves the same ``needs_human`` latch and ``human_at`` on
    every copy. Those copies are one handoff even though they remain separate
    parking stays, so the parking counts stay untouched and only this view
    collapses them. The identity is the line and the time it became a
    person's job; an export with neither timestamp collapses per line because
    there is nothing left to distinguish the requests.
    """
    order: list[tuple[str, str, str]] = []
    handoffs: dict[tuple[str, str, str], ParkEvent] = {}
    for park in parks:
        if not park.needs_human:
            continue
        key = (park.dispatch_id, park.line_id, park.human_at or park.parked_at or "")
        current = handoffs.get(key)
        if current is None:
            order.append(key)
            handoffs[key] = park
            continue
        # Copies of one latch can disagree only if one of them was written
        # before the person handled it; the handled timestamp wins.
        if current.human_handled_at is None and park.human_handled_at is not None:
            handoffs[key] = replace(current, human_handled_at=park.human_handled_at)
    return [handoffs[key] for key in order]


# ---------------------------------------------------------------------------
# Split-off and follow-up tracking
# ---------------------------------------------------------------------------


def _add_split(
    app: _Accumulator,
    dispatch_id: str,
    line_id: object,
    info: object = None,
) -> None:
    line = _text(line_id)
    if not dispatch_id or not line:
        return
    pair = (dispatch_id, line)
    if pair not in app.split_lines:
        app.split_lines.append(pair)
    details = _as_dict(info)
    if details:
        app.split_info.setdefault(pair, {}).update(details)


def _split_completion(details: dict[str, Any]) -> bool | None:
    """Whether a split-off line's follow-up completed, when the row says so."""
    for key in ("completed", "followup_completed", "follow_up_completed"):
        if key in details and details[key] is not None:
            return bool(details[key])
    status = _text(
        details.get("status")
        or details.get("followup_status")
        or details.get("follow_up_status")
    ).lower().replace("-", "_")
    if status in _COMPLETED_WORDS:
        return True
    if status in _PENDING_WORDS:
        return False
    return None


def _collect_split_outcomes(
    app: _Accumulator, dispatch_id: str, outcomes: object
) -> None:
    """The dispatch-level list the state machine writes after a split."""
    data = _as_dict(outcomes)
    raw = data.get(SPLIT_OFF) if data else None
    if isinstance(raw, str):
        _add_split(app, dispatch_id, raw)
    elif isinstance(raw, dict):
        for line_id, details in raw.items():
            # ``{"line-1": {"completed": true}}`` and
            # ``{"line_id": "line-1", ...}`` are both exports in the wild.
            if line_id in {"line_id", "id"} and isinstance(details, str):
                _add_split(app, dispatch_id, details)
                continue
            _add_split(app, dispatch_id, line_id, details)
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                _add_split(app, dispatch_id, item)
            elif isinstance(item, dict):
                line_id = item.get("line_id") or item.get("id")
                if line_id:
                    _add_split(app, dispatch_id, line_id, item)


def _followups(app: _Accumulator) -> dict[str, Any]:
    """Count follow-up drafts and any completion the input records."""
    expected = len(app.split_lines)
    completed = 0
    pending = 0
    unknown = 0
    for pair in app.split_lines:
        state = _split_completion(app.split_info.get(pair, {}))
        if state is True:
            completed += 1
        elif state is False:
            pending += 1
        else:
            unknown += 1

    if app.followups_dir is None:
        present: int | None = None
        missing: list[str] | None = None
        directory: str | None = None
    else:
        present = 0
        missing = []
        for dispatch_id, line_id in app.split_lines:
            name = f"{dispatch_id}-{line_id}.md"
            if (app.followups_dir / name).is_file():
                present += 1
            else:
                missing.append(name)
        directory = str(app.followups_dir)

    return {
        "expected": expected,
        "completed": completed,
        "pending": pending,
        "unknown": unknown,
        "present": present,
        "missing": missing,
        "directory": directory,
    }


# ---------------------------------------------------------------------------
# Review waits
# ---------------------------------------------------------------------------


def _explicit_wait(data: dict[str, Any]) -> float | None:
    """A duration the export already measured, not a scheduled delay."""
    for key in ("wait_seconds", "seconds", "duration_seconds"):
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0.0, float(value))
    return None


def _review_observed(
    raw: object, *, now: datetime | None = None
) -> _ReviewWait | None:
    """One observed review wait, with the interval when the row has a clock.

    ``delay_seconds`` is the full scheduled retry. A retry armed at 10:00 for
    15 minutes has been waiting for one minute at 10:01; reporting 900 counts
    time that has not elapsed. The clock is the start and the earlier of
    ``now`` and the deadline. A row that kept only the scheduled delay, with
    no start time, still contributes that delay because nothing else was stored.
    """
    data = _as_dict(raw)
    if not data:
        return None
    recorded = _explicit_wait(data)
    start = parse_time(
        data.get("at")
        or data.get("started_at")
        or data.get("requested_at")
        or data.get("enqueued_at")
    )
    end = parse_time(
        data.get("retry_at")
        or data.get("finished_at")
        or data.get("completed_at")
        or data.get("resolved_at")
    )
    if end is None and start is not None:
        delay = data.get("delay_seconds")
        if isinstance(delay, (int, float)) and not isinstance(delay, bool):
            end = start + timedelta(seconds=max(0.0, float(delay)))
    if start is not None and end is not None:
        bound = end if now is None or end <= now else now
        seconds = recorded
        if seconds is None:
            seconds = max(0.0, (bound - start).total_seconds())
        return _ReviewWait(seconds, start, bound)
    if start is not None and now is not None and data.get("status") in {
        "running",
        "reviewing",
        "in_progress",
    }:
        seconds = recorded
        if seconds is None:
            seconds = max(0.0, (now - start).total_seconds())
        return _ReviewWait(seconds, start, now)
    if recorded is not None:
        return _ReviewWait(recorded)
    delay = data.get("delay_seconds")
    if isinstance(delay, (int, float)) and not isinstance(delay, bool):
        return _ReviewWait(max(0.0, float(delay)))
    return None


def _review_wait_seconds(
    raw: object, *, now: datetime | None = None
) -> float | None:
    """Seconds of review wait already observed. See :func:`_review_observed`."""
    observed = _review_observed(raw, now=now)
    if observed is None:
        return None
    return observed.seconds


def _machine_stage(snap: dict[str, Any]) -> str:
    machine = _as_dict(snap.get("machine"))
    return _text(machine.get("stage") or snap.get("phase")).lower()


def _review_cell_at(snap: dict[str, Any], stage: str) -> datetime | None:
    """The review cell's completion time, when that cell was written."""
    gates = snap.get("gates")
    if not isinstance(gates, dict):
        return None
    cell = _as_dict(gates.get(stage))
    if not cell:
        return None
    return parse_time(cell.get("at") or cell.get("timestamp"))


def _stage_review_waits(snap: dict[str, Any], now: datetime) -> list[_ReviewWait]:
    """Ordinary review waits from Stage-Gate events and review cells.

    A retry timer is a special case. The usual wait is the time a line sits
    in ``primary_review`` or ``secondary_review``. The snapshot records the
    entry on ``events`` (``to_stage`` plus ``timestamp``) and the finish on
    the review cell (``gates.<stage>.at``). A line still in that stage has
    been waiting since the entry; the cell supplies the end when the leaving
    event has fallen out of the bounded log.
    """
    raw_events = snap.get("events")
    events = (
        [_as_dict(item) for item in raw_events] if isinstance(raw_events, list) else []
    )
    current = _machine_stage(snap)
    waits: list[_ReviewWait] = []
    open_stage: str | None = None
    open_at: datetime | None = None

    def close(end: datetime) -> None:
        nonlocal open_stage, open_at
        if open_stage is not None and open_at is not None:
            waits.append(
                _ReviewWait(
                    max(0.0, (end - open_at).total_seconds()),
                    open_at,
                    end,
                    True,
                )
            )
        open_stage = None
        open_at = None

    for event in events:
        to_stage = _text(event.get("to_stage")).lower()
        from_stage = _text(event.get("from_stage")).lower()
        moment = parse_time(event.get("timestamp") or event.get("at"))
        # A same-stage recovery note is not the review becoming ready again,
        # and it does not end the wait that is already open.
        if (
            open_stage is not None
            and moment is not None
            and to_stage
            and to_stage != open_stage
        ):
            close(moment)
        if to_stage in _REVIEW_STAGES and moment is not None and to_stage != from_stage:
            open_stage = to_stage
            open_at = moment

    if open_stage is not None and open_at is not None:
        cell_at = _review_cell_at(snap, open_stage)
        if cell_at is not None and cell_at >= open_at:
            close(cell_at if cell_at <= now else now)
        elif current == open_stage:
            close(now)
    return waits


def _overlaps(left: _ReviewWait, right: _ReviewWait) -> bool:
    """True when two waits cover the same stretch of clock."""
    if (
        left.start is None
        or left.end is None
        or right.start is None
        or right.end is None
    ):
        return False
    return left.start < right.end and right.start < left.end


def _one_wait_per_episode(waits: list[_ReviewWait]) -> list[_ReviewWait]:
    """Keep the Stage-Gate visit when a retry timer sits inside it.

    The visit is the review episode: time since review became ready. The
    retry delay is the tail of that same episode. Averaging the two reports
    a shorter wait than the line has actually spent in review.
    """
    episodes = [
        wait
        for wait in waits
        if wait.episode and wait.start is not None and wait.end is not None
    ]
    if not episodes:
        return waits
    return [
        wait
        for wait in waits
        if wait.episode or not any(_overlaps(wait, episode) for episode in episodes)
    ]


def _collect_review(
    app: _Accumulator,
    snap: dict[str, Any],
    line: dict[str, Any],
) -> None:
    waits: list[_ReviewWait] = []
    retry = _as_dict(snap.get("review_retry"))
    if retry:
        app.review_retries += 1
        observed = _review_observed(retry, now=app.now)
        if observed is not None:
            waits.append(observed)

    # Newer rows may record the review itself, not only the retry timer.
    for raw in (snap.get("review"), line.get("review")):
        observed = _review_observed(raw, now=app.now)
        if observed is not None:
            waits.append(observed)
            break

    observed = _review_observed(
        {
            "wait_seconds": line.get("review_wait_seconds"),
            "started_at": line.get("review_started_at"),
            "finished_at": line.get("review_finished_at"),
        },
        now=app.now,
    )
    if observed is not None:
        waits.append(observed)

    waits.extend(_stage_review_waits(snap, app.now))
    app.review_waits.extend(_one_wait_per_episode(waits))


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def _collect_line(
    app: _Accumulator, dispatch_id: str, line: dict[str, Any], snap: dict[str, Any]
) -> None:
    line_id = _text(line.get("id") or line.get("line_id"))
    if not line_id:
        return
    reason = _text(line.get("failure_reason"))

    raw_record = snap.get("liveness")
    if raw_record is None:
        raw_record = line.get("liveness")
    events, unattributed = _record_events(dispatch_id, line_id, raw_record, reason)
    app.parks.extend(events)
    app.unattributed_attempts += unattributed

    if not events:
        fallback = _fallback_event(dispatch_id, line_id, line)
        if fallback is not None:
            app.parks.append(fallback)

    if reason == SPLIT_OFF:
        _add_split(app, dispatch_id, line_id, line.get("followup"))

    _collect_review(app, snap, line)


def _normalise_dispatch(row: object) -> dict[str, Any]:
    data = _as_dict(row)
    if "id" not in data:
        data["id"] = data.get("dispatch_id") or data.get("dispatchId")
    if not data.get("state"):
        data["state"] = data.get("dispatch_state") or data.get("dispatchState")
    for key in ("lines", "outcomes", "gates", "followups"):
        data[key] = _coerce_json(data.get(key))
    if not isinstance(data.get("lines"), list):
        data["lines"] = []
    if not isinstance(data.get("outcomes"), dict):
        data["outcomes"] = {}
    if not isinstance(data.get("gates"), dict):
        data["gates"] = {}
    return data


def _collect(app: _Accumulator, dispatch: object) -> None:
    row = _normalise_dispatch(dispatch)
    app.dispatches.append(row)
    dispatch_id = _text(row.get("id"))
    gates = row.get("gates") if isinstance(row.get("gates"), dict) else {}
    for raw_line in row.get("lines") or []:
        line = _as_dict(raw_line)
        line_id = _text(line.get("id") or line.get("line_id"))
        _collect_line(app, dispatch_id, line, _gates_line(gates, line_id))
    _collect_split_outcomes(app, dispatch_id, row.get("outcomes"))
    _collect_split_outcomes(app, dispatch_id, row.get("followups"))

    # A newer row may keep the split list at dispatch level as well.
    for key in ("split_off", "split_offs"):
        _collect_split_outcomes(
            app, dispatch_id, {"split_off": row.get(key)}
        )


def _approval_reached(approval: dict[str, Any]) -> bool:
    """Whether a root ``approval`` record proves G6 was reached.

    ``GateRunner._ensure_root`` writes the shell
    ``{"result": "pending", "token_present": False, "at": None}`` before a
    dispatch starts, so the record's presence alone is not evidence.
    ``mark_awaiting_human`` stamps ``at`` (and whether a token exists), and a
    later approve/reject changes the result. A rejection therefore means the
    dispatch reached APPROVAL and a person turned it down.
    """
    if not approval:
        return False
    if approval.get("approved") is True or approval.get("reached") is True:
        return True
    result = _text(
        approval.get("result") or approval.get("status") or approval.get("decision")
    ).lower()
    if result in _APPROVAL_RESULTS or result in _APPROVAL_FAILURES:
        return True
    if approval.get("at") is not None or approval.get("token_present") is True:
        return True
    return _text(approval.get("reason")).lower() in _APPROVAL_REASONS


def _line_approval_reached(gates: dict[str, Any]) -> bool:
    """Line snapshots also stamp G6 when the approval gate opens."""
    for snap in _gates_lines(gates).values():
        line_gates = _as_dict(_as_dict(snap).get("gates"))
        g6 = _as_dict(line_gates.get("G6"))
        if _text(g6.get("reason")).lower() in _APPROVAL_REASONS:
            return True
    return False


def _approved(row: dict[str, Any]) -> bool:
    """Whether the dispatch reached APPROVAL, whether or not it was approved."""
    if _status(row.get("state")) in _APPROVAL_STATES:
        return True
    gates = row.get("gates") if isinstance(row.get("gates"), dict) else {}
    if _approval_reached(_as_dict(gates.get("approval"))):
        return True
    return _line_approval_reached(gates)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _ratio(part: int, whole: int) -> float | None:
    return (part / whole) if whole else None


def _average(values: Iterable[float]) -> float | None:
    items = list(values)
    return (sum(items) / len(items)) if items else None


def summarize(
    dispatches: list[Any],
    *,
    now: datetime | None = None,
    followups_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Build the PRD 4.6 report from dispatch rows (dicts or objects)."""
    moment = parse_time(now) or datetime.now(UTC)
    directory = Path(followups_dir) if followups_dir is not None else None
    app = _Accumulator(now=moment, followups_dir=directory)
    for dispatch in dispatches:
        _collect(app, dispatch)

    by_reason: dict[str, int] = {}
    durations: list[float] = []
    durations_by_reason: dict[str, list[float]] = {}
    for park in app.parks:
        by_reason[park.reason] = by_reason.get(park.reason, 0) + 1
        duration = park.duration(moment)
        if duration is not None:
            durations.append(duration)
            durations_by_reason.setdefault(park.reason, []).append(duration)

    # An attempt is any real step, whether it applied or failed. The producer
    # only increments ``steps_applied`` for an applied step, so filtering on
    # that alone would drop refused/error attempts and inflate the ratio.
    attempted = [
        park
        for park in app.parks
        if park.steps_applied > 0 or park.failed_steps > 0
    ]
    recovered = [
        park
        for park in attempted
        if park.outcome == RECOVERED and not park.needs_human
    ]
    attempted_by_reason: dict[str, int] = {}
    recovered_by_reason: dict[str, int] = {}
    for park in attempted:
        attempted_by_reason[park.reason] = attempted_by_reason.get(park.reason, 0) + 1
    for park in recovered:
        recovered_by_reason[park.reason] = recovered_by_reason.get(park.reason, 0) + 1

    # The PRD asks for count, average parking duration and automatic recovery
    # success *per reason*, not only as one global aggregate.
    per_reason: dict[str, dict[str, Any]] = {}
    for reason, count in sorted(by_reason.items()):
        tried = attempted_by_reason.get(reason, 0)
        won = recovered_by_reason.get(reason, 0)
        per_reason[reason] = {
            "count": count,
            "average_seconds": _average(durations_by_reason.get(reason, [])),
            # Flat aliases keep one reason easy to read; ``automatic`` mirrors
            # the global shape.
            "attempted": tried,
            "recovered": won,
            "success_ratio": _ratio(won, tried),
            "automatic": {
                "attempted": tried,
                "recovered": won,
                "success_ratio": _ratio(won, tried),
            },
        }

    # One request to a person is one handoff, even when the line re-parked
    # into another episode with the same latch. Parking itself keeps counting
    # every separate stay above.
    human = _human_handoffs(app.parks)
    handled: list[float] = []
    for park in human:
        start = parse_time(park.human_at) or parse_time(park.parked_at)
        end = parse_time(park.human_handled_at)
        if start is not None and end is not None:
            handled.append(max(0.0, (end - start).total_seconds()))

    approval = sum(1 for row in app.dispatches if _approved(row))
    review_unparseable = by_reason.get(REVIEW_UNPARSEABLE, 0)
    # Unattributed steps are attempts the trail cannot place. They are not
    # recoveries, and they are not added to a reason we would only be guessing.
    attempted_count = len(attempted) + app.unattributed_attempts
    automatic: dict[str, Any] = {
        "attempted": attempted_count,
        "recovered": len(recovered),
        "success_ratio": _ratio(len(recovered), attempted_count),
    }
    if app.unattributed_attempts:
        automatic["unattributed"] = app.unattributed_attempts

    return {
        "generated_at": format_time(moment),
        "dispatches": {
            "total": len(app.dispatches),
            "approval": approval,
            "approval_ratio": _ratio(approval, len(app.dispatches)),
        },
        "parking": {
            "total": len(app.parks),
            "by_reason": per_reason,
            "by_reason_counts": dict(sorted(by_reason.items())),
            "average_seconds": _average(durations),
            "automatic": automatic,
        },
        "needs_human": {
            "total": len(human),
            "open": sum(1 for park in human if park.human_handled_at is None),
            "average_seconds_to_handle": _average(handled),
        },
        "split_off": {
            "lines": len(app.split_lines),
            "followups": _followups(app),
        },
        "review": {
            "unparseable": review_unparseable,
            "scheduled_retries": app.review_retries,
            "average_wait_seconds": _average(wait.seconds for wait in app.review_waits),
        },
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _seconds(value: object) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "n/a"
    return f"{value:.0f}s"


def _ratio_text(value: object) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "n/a"
    return f"{value:.0%}"


def render_text(report: dict[str, Any]) -> str:
    """A short human-readable rendering of :func:`summarize`."""
    dispatches = report.get("dispatches") or {}
    parking = report.get("parking") or {}
    automatic = parking.get("automatic") or {}
    human = report.get("needs_human") or {}
    split = report.get("split_off") or {}
    review = report.get("review") or {}
    lines = [f"Liveness report ({report.get('generated_at')})"]
    if report.get("scope") == ACTIVE_ONLY:
        lines.append(
            "Scope: active dispatches only; finished dispatches are not in this "
            "list, so use --from-json with a full export for the approval ratio"
        )
    unplaced = (report.get("window") or {}).get("unplaced")
    if unplaced:
        lines.append(f"Window: {unplaced} row(s) without a timestamp were not filtered")
    lines += [
        "",
        (
            "Dispatches: "
            f"{dispatches.get('approval', 0)}/{dispatches.get('total', 0)} "
            f"reached APPROVAL ({_ratio_text(dispatches.get('approval_ratio'))})"
        ),
        (
            "Parking: "
            f"{parking.get('total', 0)} across "
            f"{len(parking.get('by_reason') or {})} reason(s); "
            f"average {_seconds(parking.get('average_seconds'))}"
        ),
        (
            "Automatic recovery: "
            f"{automatic.get('recovered', 0)}/{automatic.get('attempted', 0)} "
            f"({_ratio_text(automatic.get('success_ratio'))})"
            + (
                f"; {automatic['unattributed']} unattributed"
                if automatic.get("unattributed")
                else ""
            )
        ),
        (
            "Needs human: "
            f"{human.get('total', 0)} total, {human.get('open', 0)} open; "
            f"average to handle {_seconds(human.get('average_seconds_to_handle'))}"
        ),
        f"Split off: {split.get('lines', 0)} line(s)",
        (
            "Review: "
            f"{review.get('unparseable', 0)} unparseable; "
            f"{review.get('scheduled_retries', 0)} scheduled retries; "
            f"average wait {_seconds(review.get('average_wait_seconds'))}"
        ),
    ]

    followups = split.get("followups") or {}
    if followups.get("expected"):
        known = int(followups.get("completed") or 0) + int(
            followups.get("pending") or 0
        )
        if known:
            lines.append(
                "  follow-ups: "
                f"{followups.get('completed', 0)}/{known} known complete"
            )
        if followups.get("missing"):
            lines.append(
                "  missing drafts: " + ", ".join(followups.get("missing") or [])
            )

    by_reason = parking.get("by_reason") or {}
    if by_reason:
        lines.extend(["", "Parking by reason:"])
        for reason, entry in by_reason.items():
            if isinstance(entry, dict):
                rate = entry.get("automatic") or {}
                lines.append(
                    f"  {reason}: {entry.get('count', 0)} "
                    f"(avg {_seconds(entry.get('average_seconds'))}, "
                    f"recovery {_ratio_text(rate.get('success_ratio'))})"
                )
            else:
                # Tolerate a report produced by an older script version.
                lines.append(f"  {reason}: {entry}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Read-only loaders
# ---------------------------------------------------------------------------


def _dispatch_rows(raw: object) -> list[Any]:
    """Unwrap a list/export envelope, accepting a single row as one dispatch."""
    if isinstance(raw, list):
        return list(raw)
    if isinstance(raw, dict):
        for key in ("dispatches", "items", "rows", "data", "results"):
            value = raw.get(key)
            if isinstance(value, list):
                return list(value)
            if isinstance(value, dict):
                try:
                    return _dispatch_rows(value)
                except ValueError:
                    continue
        if raw.get("id") or raw.get("dispatch_id"):
            return [raw]
    raise ValueError(
        "the input JSON must be a dispatch array or an object containing "
        "a 'dispatches', 'items', 'rows' or 'data' list"
    )


def _loads(text: str) -> list[Any]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        # A JSONL export is one row per line; keep that convenience while
        # still rejecting a malformed single-object file below.
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        if not rows:
            raise ValueError("the input JSON is empty")
        return rows
    return _dispatch_rows(raw)


def load_json(path: Path | str) -> list[Any]:
    """Read a dispatch JSON export (``-`` reads stdin). Never writes."""
    if str(path) == "-":
        return _loads(sys.stdin.read())
    return _loads(Path(path).read_text(encoding="utf-8"))


#: Backwards-compatible names: older callers called this ``load_input``.
load_input = load_json
load_from_json = load_json


def _read_response(response: object) -> str:
    try:
        body = response.read()  # type: ignore[attr-defined, no-any-return]
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    if isinstance(body, bytes):
        return body.decode("utf-8")
    if isinstance(body, str):
        return body
    raise ValueError("the API response body is not UTF-8 text")


def urlopen(request: urllib.request.Request, timeout: float):
    """One HTTP GET, kept as a module seam so tests can intercept it."""
    return urllib.request.urlopen(request, timeout=timeout)


def load_api(
    url: str,
    *,
    token: str | None = None,
    timeout: float = 15.0,
) -> list[Any]:
    """GET one dispatch export URL. There is deliberately no POST path."""
    if not str(url).lower().startswith(("http://", "https://")):
        raise ValueError("--api must be an http(s) URL")
    headers = {
        "Accept": "application/json",
        "User-Agent": "conductor-liveness-report",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        response = urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise ValueError(f"GET {url} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ValueError(f"GET {url} failed: {exc}") from exc
    try:
        text = _read_response(response)
    except UnicodeDecodeError as exc:
        raise ValueError(f"GET {url} did not return UTF-8 JSON") from exc
    return _loads(text)


load_from_api = load_api


def _api_target(value: str | None) -> str:
    """The URL for ``--api``; a bare flag targets the local active list."""
    if value:
        return value
    base = os.environ.get("CONDUCTOR_API_URL") or "http://127.0.0.1:8000"
    return base.rstrip("/") + "/api/v1/dispatch/status/active"


#: ``report["scope"]`` for a bare ``--api``: the active list leaves out every
#: finished dispatch, so its approval ratio is not the PRD 4.6 baseline.
ACTIVE_ONLY = "active_only"


def _row_time(dispatch: Any) -> datetime | None:
    row = _normalise_dispatch(dispatch)
    return parse_time(row.get("created_at")) or parse_time(row.get("updated_at"))


def _unplaced(dispatches: list[Any]) -> int:
    """Rows a time window cannot place because they carry no timestamp."""
    return sum(1 for dispatch in dispatches if _row_time(dispatch) is None)


def _filter_window(
    dispatches: list[Any], *, since: datetime | None, until: datetime | None
) -> list[Any]:
    if since is None and until is None:
        return dispatches
    kept: list[Any] = []
    for dispatch in dispatches:
        created = _row_time(dispatch)
        if created is None:
            # An export without a timestamp cannot be placed in the window;
            # keeping it is less surprising than silently dropping it.
            kept.append(dispatch)
            continue
        if since is not None and created < since:
            continue
        if until is not None and created > until:
            continue
        kept.append(dispatch)
    return kept


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _timestamp_arg(value: str) -> datetime:
    parsed = parse_time(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"not an ISO timestamp: {value!r}")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="liveness_report",
        description=(
            "Read-only PRD 4.6 liveness report for auto-dispatch rows. "
            "Reads one JSON export or one GET; writes nothing."
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--from-json",
        "--input",
        dest="from_json",
        metavar="PATH",
        help="dispatch rows JSON export ('-' reads stdin)",
    )
    source.add_argument(
        "--api",
        nargs="?",
        const="",
        metavar="URL",
        help=(
            "GET a dispatch rows JSON export (GET only, never a write route). "
            "Without URL, reads CONDUCTOR_API_URL and its active-dispatch endpoint "
            "(running dispatches only; the report is marked scope=active_only)"
        ),
    )
    parser.add_argument(
        "--api-token",
        default=None,
        help="bearer token for --api (default: CONDUCTOR_API_TOKEN)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="HTTP timeout in seconds for --api (default: 15)",
    )
    parser.add_argument("--days", type=float, default=None, help="only the last N days")
    parser.add_argument("--since", type=_timestamp_arg, default=None, help="ISO lower bound")
    parser.add_argument("--until", type=_timestamp_arg, default=None, help="ISO upper bound")
    parser.add_argument(
        "--limit", type=int, default=0, help="read at most N rows (0 means every row)"
    )
    parser.add_argument(
        "--followups-dir",
        type=Path,
        default=None,
        help="read-only directory of <dispatch>-<line>.md follow-up drafts",
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    until = args.until or now
    since = args.since
    if since is None and args.days is not None:
        if args.days < 0:
            parser.error("--days must be nonnegative")
        since = until - timedelta(days=args.days)

    try:
        if args.from_json is not None:
            dispatches = load_json(args.from_json)
        else:
            token = args.api_token
            if token is None:
                token = os.environ.get("CONDUCTOR_API_TOKEN") or os.environ.get(
                    "CONDUCTOR_TOKEN"
                )
            dispatches = load_api(
                _api_target(args.api), token=token, timeout=args.timeout
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"liveness_report: {exc}", file=sys.stderr)
        return 1

    if args.limit and args.limit > 0:
        dispatches = dispatches[: args.limit]
    window_asked = (
        args.since is not None or args.until is not None or args.days is not None
    )
    unplaced = _unplaced(dispatches) if window_asked else 0
    dispatches = _filter_window(dispatches, since=since, until=until)
    report = summarize(dispatches, now=now, followups_dir=args.followups_dir)
    report["window"] = {
        "since": format_time(since),
        "until": format_time(until),
    }
    if unplaced:
        # The API summary has no created_at/updated_at. Say so instead of
        # presenting an unfiltered report as a windowed one.
        report["window"]["unplaced"] = unplaced
        print(
            f"liveness_report: {unplaced} row(s) have no created_at/updated_at; "
            "the time window did not filter them",
            file=sys.stderr,
        )
    if args.from_json is None and not args.api:
        report["scope"] = ACTIVE_ONLY
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
