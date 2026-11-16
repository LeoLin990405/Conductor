"""The ``gates.lines[id].liveness`` entry: owner, budget and every step taken.

N76 adds no migration. The whole recovery history lives beside the line's
Stage-Gate snapshot under one key, so a restart resumes with the budget the
previous process had and the report can read a week of parks from dispatches
it already loads.

The record is append-only for history. It is created when a line parks; every
attempted recovery appends one step (number, action, time, result, reason
code); and ``needs_human`` is a one-way latch. The first ``mark_needs_human``
stamps the single notification, and a later sweep calling it again gets the
same record back, so a line never pages a person twice.

A ``shadow`` step is an observation, not a recovery action. It stays in the
bounded history for the audit trail, but it is counted in ``shadowed`` instead
of ``spent``. The enforcement budget therefore survives a shadow rollout: a
line that recorded four things it would have done still has its four steps
when the mode changes to ``enforce``. ``spent`` only counts attempts that could
have acted (applied, refused, failed), so it remains the hard loop bound.

Re-parking is a new episode, not a reset. Earlier steps stay in the bounded
history and both counters keep counting every step ever taken, so the per-line
budget cannot be renewed by parking the line again. A later park may only
tighten that cap, and zero is a real cap rather than an unset one: ``spent``
stays, so steps already taken still count against the smaller limit. Closed
episodes are kept as small summaries, bounded too, so the report can count
each park separately instead of attributing the whole history to the latest
reason. The open episode's applied-step total is stored on the record, not
recounted from ``history``: the trail drops its oldest steps at
``HISTORY_MAX``, and later observations can evict an applied step that the
archived summary still has to report.

Every function that takes a record returns a new value. ``store`` and
``clear`` return updated copies of the line snapshot instead of mutating the
caller's dict, so a failed save cannot leave an in-memory dispatch half-written.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

#: The key inside one line's gates snapshot.
LIVENESS_KEY = "liveness"

#: Always keep the newest steps, drop older ones. The step budget is small
#: (4 by default), so 64 entries are a deep audit trail without an unbounded
#: JSON cell. ``MAX_HISTORY_STEPS`` is the older spelling, kept for callers
#: that already import it.
HISTORY_MAX = 64
MAX_HISTORY_STEPS = HISTORY_MAX

#: Earlier parking episodes, kept as summaries. A line can re-park often; the
#: report needs every episode's reason, duration and outcome, not just the
#: latest one, while the gates JSON must stay bounded.
MAX_EPISODES = 32
EPISODE_LIMIT = MAX_EPISODES

#: A step result: what the sweep actually did with the chosen action.
APPLIED = "applied"
SHADOW = "shadow"
REFUSED = "refused"
SKIPPED = "skipped"
ERROR = "error"

#: A record outcome. ``pending`` is still owned; the others are terminal.
PENDING = "pending"
RECOVERED = "recovered"
NEEDS_HUMAN = "needs_human"
SPLIT_OFF = "split_off"
MANAGED = "managed"
OUTCOMES = frozenset({PENDING, RECOVERED, NEEDS_HUMAN, SPLIT_OFF, MANAGED})


def parse_time(value: object) -> datetime | None:
    """A stored timestamp (ISO string, epoch seconds, or datetime) as UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
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
    """UTC ``YYYY-MM-DDTHH:MM:SS+00:00``; a string is parsed then normalised."""
    if moment is None:
        return None
    if isinstance(moment, str):
        parsed = parse_time(moment)
        if parsed is None:
            return None
        moment = parsed
    elif isinstance(moment, (int, float)) and not isinstance(moment, bool):
        try:
            moment = datetime.fromtimestamp(float(moment), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(moment, datetime):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _stamp(value: datetime | str | None) -> str:
    parsed = parse_time(value)
    if parsed is None:
        raise ValueError("a liveness record needs a timestamp")
    return format_time(parsed) or ""


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _text(value: object) -> str:
    return str(value) if value else ""


@dataclass(frozen=True)
class LivenessStep:
    """One recovery attempt: what was tried, when, and what happened."""

    number: int
    action: str
    reason: str
    at: str
    result: str
    detail: str = ""
    #: The parking episode this step belongs to. Kept for the audit trail.
    episode: int = 1
    #: The recovery group this step belongs to. Same-group re-parks continue
    #: that group's progression instead of starting it over.
    group: str = ""

    @property
    def step(self) -> int:
        """The number under its historical key name."""
        return self.number

    def to_dict(self) -> dict[str, object]:
        raw: dict[str, object] = {
            "step": self.number,
            "episode": self.episode,
            "action": self.action,
            "reason": self.reason,
            "at": self.at,
            "result": self.result,
        }
        if self.group:
            raw["group"] = self.group
        if self.detail:
            raw["detail"] = self.detail
        return raw

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LivenessStep:
        return cls(
            number=int(data.get("step") or data.get("number") or 0),
            action=_text(data.get("action")),
            reason=_text(data.get("reason")),
            at=_text(data.get("at")),
            result=_text(data.get("result")),
            detail=_text(data.get("detail")),
            episode=max(1, _int(data.get("episode"))),
            group=_text(data.get("group")),
        )


@dataclass(frozen=True)
class LivenessEpisode:
    """A closed parking episode, summarised so an earlier park is not lost.

    Only the counters the report needs are kept: which reason parked the line,
    when it started and stopped, how it ended and whether a person handled it.
    The full step trail stays in :class:`LivenessRecord.history`, bounded there.
    """

    number: int
    reason: str
    group: str
    parked_at: str
    resolved_at: str
    outcome: str = PENDING
    steps_applied: int = 0
    needs_human: bool = False
    human_at: str | None = None
    human_handled_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        raw: dict[str, object] = {
            "episode": self.number,
            "reason": self.reason,
            "group": self.group,
            "parked_at": self.parked_at,
            "resolved_at": self.resolved_at,
            "outcome": self.outcome,
            "steps_applied": self.steps_applied,
        }
        if self.needs_human:
            raw["needs_human"] = True
        if self.human_at:
            raw["human_at"] = self.human_at
        if self.human_handled_at:
            raw["human_handled_at"] = self.human_handled_at
        return raw

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LivenessEpisode:
        return cls(
            number=max(1, _int(data.get("episode"))),
            reason=_text(data.get("reason")),
            group=_text(data.get("group")),
            parked_at=_text(data.get("parked_at")),
            resolved_at=_text(data.get("resolved_at") or data.get("updated_at")),
            outcome=_text(data.get("outcome")) or PENDING,
            steps_applied=max(0, _int(data.get("steps_applied"))),
            needs_human=bool(data.get("needs_human")),
            human_at=_text(data.get("human_at")) or None,
            human_handled_at=_text(data.get("human_handled_at")) or None,
        )


@dataclass(frozen=True)
class LivenessRecord:
    """The persisted recovery state for one parked line."""

    reason: str
    group: str
    parked_at: str
    updated_at: str
    budget: int = 0
    #: Every budget-consuming step this line has taken, across all parking
    #: episodes. Shadow observations are *not* included: they are counted in
    #: ``shadowed`` so a shadow rollout cannot exhaust the enforcement budget.
    #: It is monotonic even when old steps are trimmed from ``history``, so the
    #: line budget cannot reset on a re-park.
    spent: int = 0
    #: The current parking episode (1 for the first park). Steps carry their
    #: episode so policy progression can restart without losing the audit trail
    #: of earlier episodes.
    episode: int = 1
    #: Applied steps in the current episode only. Refused, skipped and failed
    #: attempts spend budget but are not applied, and a new park starts this
    #: count again. It is stored rather than counted from ``history``: the
    #: trail keeps only ``HISTORY_MAX`` steps, so an early applied step is
    #: evicted by later observations, and a re-park must still archive it.
    steps_applied: int = 0
    history: tuple[LivenessStep, ...] = ()
    #: Closed episodes, newest last and bounded by ``MAX_EPISODES``. The
    #: current episode lives in the fields above and in ``history``.
    episodes: tuple[LivenessEpisode, ...] = ()
    needs_human: bool = False
    human_reason: str = ""
    human_at: str | None = None
    human_handled_at: str | None = None
    notified_at: str | None = None
    next_at: str | None = None
    next_action: str | None = None
    outcome: str = PENDING
    #: Every shadow observation across all episodes. These share ``history``
    #: with real attempts but never consume ``budget``; the counter is kept
    #: separately (and persisted) so switching to enforce starts fresh.
    shadowed: int = 0

    @property
    def recorded(self) -> int:
        """Every step ever recorded, whether observed or budget-consuming."""
        return self.spent + self.shadowed

    @property
    def steps(self) -> tuple[LivenessStep, ...]:
        """The bounded history under its older key name."""
        return self.history

    @property
    def budget_left(self) -> int:
        return max(0, self.budget - self.spent)

    @property
    def exhausted(self) -> bool:
        return self.budget_left <= 0

    @property
    def notification_due(self) -> bool:
        """Whether a human-owned record still owes its one notification."""
        return self.needs_human and self.notified_at is None

    @property
    def active(self) -> bool:
        return self.outcome == PENDING and not self.needs_human

    def to_episode(self) -> LivenessEpisode:
        """The current episode as the bounded summary a re-park archives."""
        return LivenessEpisode(
            number=self.episode,
            reason=self.reason,
            group=self.group,
            parked_at=self.parked_at,
            resolved_at=self.updated_at,
            outcome=self.outcome,
            steps_applied=self.steps_applied,
            needs_human=self.needs_human,
            human_at=self.human_at,
            human_handled_at=self.human_handled_at,
        )

    def _next_step_number(self) -> int:
        """One past the newest recorded step, including trimmed history."""
        highest = max((step.number for step in self.history), default=0)
        return max(self.recorded, highest) + 1

    def with_step(
        self,
        action: str,
        *,
        at: datetime | str,
        result: str,
        detail: str = "",
        reason: str | None = None,
        next_at: datetime | str | None = None,
        next_action: str | None = None,
    ) -> LivenessRecord:
        """Append one attempt and move the schedule to the next one.

        A ``shadow`` result records what the sweep would have done without
        spending budget; every other result is a real attempt and counts
        against the line's automatic-step cap.
        """
        moment = _stamp(at)
        result_text = _text(result)
        observed = result_text == SHADOW
        applied = result_text == APPLIED
        step = LivenessStep(
            number=self._next_step_number(),
            action=_text(action),
            reason=_text(reason) or self.reason,
            at=moment,
            result=result_text,
            detail=_text(detail)[:2000],
            episode=self.episode,
            group=self.group,
        )
        return replace(
            self,
            history=(*self.history, step)[-HISTORY_MAX:],
            spent=self.spent + (0 if observed else 1),
            shadowed=self.shadowed + (1 if observed else 0),
            steps_applied=self.steps_applied + (1 if applied else 0),
            updated_at=moment,
            next_at=format_time(next_at),
            next_action=_text(next_action) or None,
        )

    def add_step(
        self,
        action: str,
        *,
        at: datetime | str,
        result: str,
        detail: str = "",
        reason: str | None = None,
        next_at: datetime | str | None = None,
        next_action: str | None = None,
    ) -> LivenessRecord:
        """Alias for :meth:`with_step`, the name the sweep reads more naturally."""
        return self.with_step(
            action,
            at=at,
            result=result,
            detail=detail,
            reason=reason,
            next_at=next_at,
            next_action=next_action,
        )

    def with_schedule(
        self,
        *,
        at: datetime | str,
        next_at: datetime | str | None,
        next_action: str | None,
    ) -> LivenessRecord:
        """Record when the next step is due without adding a step yet."""
        return replace(
            self,
            updated_at=_stamp(at),
            next_at=format_time(next_at),
            next_action=_text(next_action) or None,
        )

    def mark_needs_human(
        self, reason: str, *, at: datetime | str
    ) -> LivenessRecord:
        """Latch the line to a person and stamp the one notification.

        The first call changes the record; every later call returns the same
        record unchanged, so a sweep that repeats its decision cannot notify a
        person twice. ``mark_notified`` remains for callers that separate the
        decision from the delivery: if it already stamped ``notified_at``,
        this method keeps that earlier stamp.
        """
        if self.needs_human:
            return self
        moment = _stamp(at)
        return replace(
            self,
            needs_human=True,
            human_reason=_text(reason),
            human_at=moment,
            notified_at=self.notified_at or moment,
            updated_at=moment,
            next_at=None,
            next_action=None,
            outcome=NEEDS_HUMAN,
        )

    #: Older callers used ``mark_human``; the behaviour is the same latch.
    mark_human = mark_needs_human

    def mark_notified(self, *, at: datetime | str) -> LivenessRecord:
        """Stamp the one notification if it has not been stamped yet."""
        if self.notified_at is not None:
            return self
        moment = _stamp(at)
        return replace(self, notified_at=moment, updated_at=moment)

    def mark_human_handled(self, *, at: datetime | str) -> LivenessRecord:
        """Record that the person who was notified has acted on the line."""
        if self.human_handled_at is not None:
            return self
        return replace(
            self,
            human_handled_at=_stamp(at),
            updated_at=_stamp(at),
        )

    def finish(self, outcome: str, *, at: datetime | str) -> LivenessRecord:
        """Close the record: recovered, split off, or handed to a person."""
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown liveness outcome {outcome!r}")
        moment = _stamp(at)
        human = outcome == NEEDS_HUMAN or self.needs_human
        return replace(
            self,
            outcome=outcome,
            updated_at=moment,
            next_at=None,
            next_action=None,
            needs_human=human,
            human_at=self.human_at or (moment if human else None),
        )

    def to_dict(self) -> dict[str, object]:
        raw: dict[str, object] = {
            "reason": self.reason,
            "group": self.group,
            "parked_at": self.parked_at,
            "updated_at": self.updated_at,
            "budget": self.budget,
            "spent": self.spent,
            "shadowed": self.shadowed,
            "episode": self.episode,
            "steps_applied": self.steps_applied,
            "history": [step.to_dict() for step in self.history],
            "outcome": self.outcome,
        }
        if self.episodes:
            raw["episodes"] = [episode.to_dict() for episode in self.episodes]
        if self.needs_human:
            raw["needs_human"] = True
            raw["human_reason"] = self.human_reason
            if self.human_at:
                raw["human_at"] = self.human_at
            if self.human_handled_at:
                raw["human_handled_at"] = self.human_handled_at
        if self.notified_at:
            raw["notified_at"] = self.notified_at
        if self.next_at:
            raw["next_at"] = self.next_at
        if self.next_action:
            raw["next_action"] = self.next_action
        return raw

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LivenessRecord:
        raw_history = data.get("history")
        if not isinstance(raw_history, (list, tuple)):
            # Records written before ``history`` was the key used ``steps``.
            raw_history = data.get("steps")
        all_steps = (
            tuple(
                LivenessStep.from_dict(item)
                for item in raw_history
                if isinstance(item, dict)
            )
            if isinstance(raw_history, (list, tuple))
            else ()
        )
        history = all_steps[-HISTORY_MAX:]
        raw_episodes = data.get("episodes")
        all_episodes = (
            tuple(
                LivenessEpisode.from_dict(item)
                for item in raw_episodes
                if isinstance(item, dict)
            )
            if isinstance(raw_episodes, (list, tuple))
            else ()
        )
        if "shadowed" in data:
            # Current shape: the two counters are authoritative. They survive
            # trimming, so a reload cannot reclassify shadow observations as
            # enforcement spend (or the reverse).
            spent = max(0, _int(data.get("spent")))
            shadowed = max(0, _int(data.get("shadowed")))
        else:
            # Legacy record: ``spent`` counted every step, including shadow
            # observations. Infer the split from whatever history survived.
            # A record with visible shadow steps is treated conservatively:
            # only visible non-shadow attempts are enforcement spend, and the
            # remaining legacy total becomes shadow history. That keeps an old
            # shadow rollout from handing an exhausted budget to enforce.
            legacy_total = max(0, _int(data.get("spent")))
            if "episode" not in data:
                # Older legacy record: ``spent`` counted earlier episodes only
                # and ``steps`` held the current one.
                legacy_total += len(all_steps)
            else:
                legacy_total = max(legacy_total, len(all_steps))
            visible_shadow = sum(1 for step in all_steps if step.result == SHADOW)
            visible_enforced = len(all_steps) - visible_shadow
            if visible_shadow:
                spent = visible_enforced
                shadowed = max(0, legacy_total - spent)
            else:
                spent = legacy_total
                shadowed = 0

        # Keep numbering monotonic even if a persisted counter is missing or
        # lower than the newest step in the bounded history. Classify the gap
        # as observation rather than spend so recovery cannot overcharge.
        highest = max((step.number for step in all_steps), default=0)
        if spent + shadowed < highest:
            shadowed += highest - (spent + shadowed)

        episode = max(1, _int(data.get("episode")))
        # ``steps_applied`` is authoritative once written, and it can be higher
        # than the steps still in the bounded trail. Records from before the
        # counter existed have no key; the applied steps still visible are the
        # floor, counted before trimming so a long legacy trail is not shorted.
        visible_applied = sum(
            1
            for step in all_steps
            if step.episode == episode and step.result == APPLIED
        )
        steps_applied = max(
            0, _int(data.get("steps_applied")), visible_applied
        )

        return cls(
            reason=_text(data.get("reason")),
            group=_text(data.get("group")),
            parked_at=_text(data.get("parked_at") or data.get("updated_at")),
            updated_at=_text(data.get("updated_at") or data.get("parked_at")),
            budget=max(0, _int(data.get("budget"))),
            spent=spent,
            episode=episode,
            steps_applied=steps_applied,
            history=history,
            episodes=all_episodes[-MAX_EPISODES:],
            needs_human=bool(data.get("needs_human")),
            human_reason=_text(data.get("human_reason")),
            human_at=_text(data.get("human_at")) or None,
            human_handled_at=_text(data.get("human_handled_at")) or None,
            notified_at=_text(data.get("notified_at")) or None,
            next_at=_text(data.get("next_at")) or None,
            next_action=_text(data.get("next_action")) or None,
            outcome=_text(data.get("outcome")) or PENDING,
            shadowed=shadowed,
        )


def new_record(
    reason: str,
    group: str,
    *,
    budget: int,
    at: datetime | str,
    next_at: datetime | str | None = None,
    next_action: str | None = None,
    previous: LivenessRecord | None = None,
) -> LivenessRecord:
    """Create the record a park writes before the first recovery step.

    ``previous`` is the line's earlier episode, if any. Its history stays in
    the bounded trail (so the persisted audit record and the report keep it),
    its summary is appended to ``episodes``, while ``spent`` and ``shadowed``
    carry their separate totals. Steps keep their group, so re-parking in the
    same recovery group continues that group's progression while a different
    group starts at its first step. The per-line budget therefore does not
    reset on a re-park: the cap is the smaller of the previous cap and the
    one requested now, including zero, while ``spent`` is kept. A tighter
    policy cannot be ignored, and a looser one cannot hand the line more
    automatic steps. Shadow observations still do not consume it.
    If ``previous`` was latched to a person, that latch, its notification stamp
    and any handling stamp carry over; a re-park must not make the line active
    again or page the same person twice.
    """
    moment = _stamp(at)
    spent = 0
    shadowed = 0
    episode = 1
    history: tuple[LivenessStep, ...] = ()
    episodes: tuple[LivenessEpisode, ...] = ()
    needs_human = False
    human_reason = ""
    human_at = None
    human_handled_at = None
    notified_at = None
    outcome = PENDING
    if previous is not None:
        spent = previous.spent
        shadowed = previous.shadowed
        episode = previous.episode + 1
        history = previous.history[-HISTORY_MAX:]
        episodes = (*previous.episodes, previous.to_episode())[-MAX_EPISODES:]
        # ``or`` treats 0 as missing and also keeps a larger old cap, so one
        # recovery under cap 4 followed by a re-park at cap 1 still had three
        # steps left. The smaller cap wins, including an explicit zero.
        budget = min(max(0, previous.budget), max(0, int(budget)))
        if previous.needs_human:
            # A re-park is a new episode, not a way out of a human hand-off.
            # Carrying the latch, its notification and any handling stamp keeps
            # the line inactive and prevents a second page for the same line.
            needs_human = True
            human_reason = previous.human_reason
            human_at = previous.human_at
            human_handled_at = previous.human_handled_at
            notified_at = previous.notified_at
            outcome = NEEDS_HUMAN
    return LivenessRecord(
        reason=_text(reason),
        group=_text(group),
        parked_at=moment,
        updated_at=moment,
        budget=max(0, int(budget)),
        spent=max(0, spent),
        episode=episode,
        history=history,
        episodes=episodes,
        needs_human=needs_human,
        human_reason=human_reason,
        human_at=human_at,
        human_handled_at=human_handled_at,
        notified_at=notified_at,
        next_at=format_time(next_at),
        next_action=_text(next_action) or None,
        outcome=outcome,
        shadowed=shadowed,
    )


def add_step(
    record: LivenessRecord,
    action: str,
    *,
    at: datetime | str,
    result: str,
    detail: str = "",
    reason: str | None = None,
    next_at: datetime | str | None = None,
    next_action: str | None = None,
) -> LivenessRecord:
    """Module-level form of :meth:`LivenessRecord.with_step`."""
    return record.with_step(
        action,
        at=at,
        result=result,
        detail=detail,
        reason=reason,
        next_at=next_at,
        next_action=next_action,
    )


def mark_needs_human(
    record: LivenessRecord, reason: str, *, at: datetime | str
) -> LivenessRecord:
    """Module-level form of :meth:`LivenessRecord.mark_needs_human`."""
    return record.mark_needs_human(reason, at=at)


def load(snapshot: dict[str, Any] | None) -> LivenessRecord | None:
    """The record on one line snapshot, or None when the line never parked."""
    if not isinstance(snapshot, dict):
        return None
    raw = snapshot.get(LIVENESS_KEY)
    if not isinstance(raw, dict) or not raw.get("reason"):
        return None
    return LivenessRecord.from_dict(raw)


def store(snapshot: dict[str, Any], record: LivenessRecord) -> dict[str, Any]:
    """A copy of one line snapshot with the record written under its key.

    The caller's snapshot is never mutated; the returned dict is the value to
    save to the dispatch.
    """
    updated = dict(snapshot)
    updated[LIVENESS_KEY] = record.to_dict()
    return updated


def clear(snapshot: dict[str, Any]) -> dict[str, Any]:
    """A copy of one line snapshot with the record dropped.

    The caller's snapshot is never mutated; the returned dict is the value to
    save to the dispatch.
    """
    updated = dict(snapshot)
    updated.pop(LIVENESS_KEY, None)
    return updated


__all__ = [
    "APPLIED",
    "EPISODE_LIMIT",
    "ERROR",
    "HISTORY_MAX",
    "LIVENESS_KEY",
    "MANAGED",
    "MAX_EPISODES",
    "MAX_HISTORY_STEPS",
    "NEEDS_HUMAN",
    "OUTCOMES",
    "PENDING",
    "RECOVERED",
    "REFUSED",
    "SHADOW",
    "SKIPPED",
    "SPLIT_OFF",
    "LivenessEpisode",
    "LivenessRecord",
    "LivenessStep",
    "add_step",
    "clear",
    "format_time",
    "load",
    "mark_needs_human",
    "new_record",
    "parse_time",
    "store",
]
