"""Escalate a line to the Planner after two BLOCKed fix rounds.

The Stage-Gate already moves a line to ``replanning`` when the same reviewer
BLOCKs a second fix submission. What it does not carry is the *history*: the
Planner sees the current blockers and the reason code, so it can keep asking
for one more edge-case patch without ever seeing that the previous two rounds
asked for the same kind of patch. This module keeps that history on the line
snapshot and renders the one instruction that makes the Planner reconsider the
approach instead of the next edge case.

The persisted shape is deliberately a plain dict under ``ESCALATION`` so a
snapshot written before this module existed still loads, and a dispatch written
by this module can be read by a process running an older one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: How many fix rounds may be BLOCKed before the Planner is asked to reconsider.
#: The product rule from ``docs/plans/next/collaboration-lessons.md`` (N6) fixes
#: this at two; the keyword exists for callers that must reason about the count,
#: not as a per-deployment setting.
ESCALATION_THRESHOLD = 2

#: Snapshot keys. Keep these strings stable: they are part of the persisted
#: dispatch row and the operator-facing snapshot.
ESCALATION = "escalation"
BLOCKER_BRIEF = "blocker_brief"
#: Older spelling kept for readers/tests that predate the module.
BLOCKERS_BRIEF = BLOCKER_BRIEF

#: The instruction the Planner must follow. It is a positive rule plus the
#: reason, because a bare "do not patch edge cases" is easy to ignore.
ESCALATION_INSTRUCTION = (
    "Prefer a new approach or a narrower specification over patching more edge "
    "cases. Two consecutive fix rounds on this line were BLOCKed, so the approach "
    "or the spec is wrong, not one more missing edge case. Do not spend another "
    "round on the same approach."
)
ESCALATION_PLANNER_PREFIX = "Escalation to the Planner:"

#: A brief is a prompt, not a log. Bound it like the other blocker briefs.
MAX_BLOCKED_ROUNDS = 20
MAX_BLOCKERS_PER_ROUND = 20
MAX_BLOCKER_CHARS = 1_000
MAX_SUMMARY_CHARS = 8_000


def _text(value: object, limit: int = MAX_BLOCKER_CHARS) -> str:
    """One bounded, single-object string. Empty values are dropped by callers."""
    return str(value or "").strip()[:limit]


def _items(value: object) -> tuple[str, ...]:
    """A blocker list as bounded non-empty strings, preserving order."""
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, Sequence):
        return ()
    out: list[str] = []
    for item in value:
        text = _text(item)
        if text:
            out.append(text)
        if len(out) >= MAX_BLOCKERS_PER_ROUND:
            break
    return tuple(out)


@dataclass(frozen=True, slots=True)
class BlockedRound:
    """One BLOCK and the blockers it left on the line.

    ``round`` is the BLOCK's position in this sequence (``0`` is the initial
    BLOCK, which is *not* a failed fix round). ``reviewer`` lets the detector
    restart the count when the reviewer changes, matching Stage-Gate.
    """

    round: int = 0
    reviewer: str = ""
    blockers: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "BlockedRound":
        try:
            number = int(raw.get("round") or 0)
        except (TypeError, ValueError):
            number = 0
        return cls(
            round=max(number, 0),
            reviewer=_text(raw.get("reviewer")),
            blockers=_items(raw.get("blockers") or raw.get("findings")),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "round": self.round,
            "reviewer": self.reviewer,
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True, slots=True)
class Escalation:
    """The Planner-facing file of a line with two failed fix rounds."""

    line_id: str = ""
    rounds: int = 0
    blockers: tuple[str, ...] = ()
    summary: str = ""
    instruction: str = ESCALATION_INSTRUCTION
    fix_round: int = 0
    threshold: int = ESCALATION_THRESHOLD
    history: tuple[BlockedRound, ...] = ()

    @property
    def blocker_count(self) -> int:
        return len(self.blockers)

    @property
    def due(self) -> bool:
        """True when the line has at least the configured failed rounds."""
        return self.rounds >= max(1, self.threshold)

    def to_dict(self) -> dict[str, object]:
        return {
            "line_id": self.line_id,
            "rounds": self.rounds,
            "blockers": list(self.blockers),
            "summary": self.summary,
            "instruction": self.instruction,
            "fix_round": self.fix_round,
            "threshold": self.threshold,
            "history": [entry.to_dict() for entry in self.history],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Escalation | None":
        if not isinstance(raw, Mapping):
            return None
        history_raw = raw.get("history")
        history = (
            tuple(
                BlockedRound.from_mapping(item)
                for item in history_raw
                if isinstance(item, Mapping)
            )
            if isinstance(history_raw, Sequence) and not isinstance(history_raw, (str, bytes))
            else ()
        )
        try:
            rounds = int(raw.get("rounds") or 0)
        except (TypeError, ValueError):
            rounds = 0
        try:
            fix_round = int(raw.get("fix_round") or 0)
        except (TypeError, ValueError):
            fix_round = 0
        try:
            threshold = int(raw.get("threshold") or ESCALATION_THRESHOLD)
        except (TypeError, ValueError):
            threshold = ESCALATION_THRESHOLD
        return cls(
            line_id=_text(raw.get("line_id")),
            rounds=max(rounds, 0),
            blockers=_items(raw.get("blockers")),
            summary=_text(raw.get("summary"), MAX_SUMMARY_CHARS),
            instruction=_text(raw.get("instruction"), MAX_SUMMARY_CHARS)
            or ESCALATION_INSTRUCTION,
            fix_round=max(fix_round, 0),
            threshold=max(threshold, 1),
            history=history,
        )

    def render(self) -> str:
        """The Planner prompt block: every blocker, then the instruction."""
        lines = [ESCALATION_PLANNER_PREFIX, self.summary or "Blockers so far: none recorded."]
        lines.extend(["", "Instruction:", self.instruction])
        return "\n".join(lines)

    #: The name a planner prompt helper is likely to reach for.
    def planner_note(self) -> str:
        return self.render()

    def brief(self) -> str:
        return self.render()


def _brief_entries(snapshot: Mapping[str, Any]) -> tuple[BlockedRound, ...]:
    raw = snapshot.get(BLOCKER_BRIEF)
    if isinstance(raw, Mapping):
        raw = raw.get("entries") or raw.get("rounds")
    entries: list[BlockedRound] = []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for item in raw:
            if isinstance(item, BlockedRound):
                entries.append(item)
            elif isinstance(item, Mapping):
                entries.append(BlockedRound.from_mapping(item))
    return tuple(entries)


#: The Stage-Gate review verdict is ``block``; older snapshots wrote ``fail``.
#: Both name a BLOCK, and neither may be skipped when rebuilding the brief.
_BLOCK_RESULTS = frozenset({"block", "blocked", "fail"})


def _history_entries(snapshot: Mapping[str, Any]) -> tuple[BlockedRound, ...]:
    history = snapshot.get("history")
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        return ()
    entries: list[BlockedRound] = []
    for item in history:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("result") or "").strip().lower() not in _BLOCK_RESULTS:
            continue
        try:
            number = int(item.get("round") or 0)
        except (TypeError, ValueError):
            number = 0
        entries.append(
            BlockedRound(
                round=max(number, 0),
                reviewer=_text(item.get("reviewer")),
                blockers=_items(item.get("blockers")),
            )
        )
    return tuple(entries)


def _entries_from_snapshot(snapshot: Mapping[str, Any]) -> tuple[BlockedRound, ...]:
    """Read the brief, falling back to (and merging with) review history.

    ``history`` is the older record of failed reviews; it may carry blocker
    text but no separate brief. Whichever source has the longer sequence is
    the sequence, and the other fills in blocker text it is missing. That
    keeps a direct ``_replan`` (which may only have history) from losing the
    earlier blockers when the current BLOCK was just appended to the brief.
    """
    brief = _brief_entries(snapshot)
    history = _history_entries(snapshot)
    if BLOCKER_BRIEF in snapshot and not brief:
        # An empty key is a deliberate sequence boundary (a passing review),
        # not an older snapshot that predates the key.
        return ()
    if not history:
        return brief[:MAX_BLOCKED_ROUNDS]
    if not brief:
        return history[:MAX_BLOCKED_ROUNDS]
    if len(history) <= len(brief):
        return brief[:MAX_BLOCKED_ROUNDS]
    merged = list(history)
    for index, entry in enumerate(brief):
        if not merged[index].blockers:
            merged[index] = BlockedRound(
                merged[index].round, merged[index].reviewer, entry.blockers
            )
        elif not merged[index].reviewer and entry.reviewer:
            merged[index] = BlockedRound(
                merged[index].round, entry.reviewer, merged[index].blockers
            )
    return tuple(merged[:MAX_BLOCKED_ROUNDS])


def blocker_brief(snapshot: Mapping[str, Any] | None) -> tuple[BlockedRound, ...]:
    """Every recorded BLOCK, in order. An absent snapshot has no history."""
    if not isinstance(snapshot, Mapping):
        return ()
    return _entries_from_snapshot(snapshot)


def record_blocked_round(
    snapshot: dict[str, Any],
    blockers: Sequence[str] | str,
    *,
    round: int | None = None,
    reviewer: str = "",
) -> tuple[BlockedRound, ...]:
    """Append (or replace) one BLOCK in the line's persisted brief.

    The same ``(round, reviewer)`` replaces its row so a retried save cannot
    duplicate a BLOCK. A changed reviewer starts a new Stage-Gate sequence and
    therefore gets its own row.
    """
    if not isinstance(snapshot, dict):
        raise TypeError("snapshot must be a dict")
    entries = list(_entries_from_snapshot(snapshot))
    try:
        number = int(round) if round is not None else len(entries)
    except (TypeError, ValueError):
        number = len(entries)
    entry = BlockedRound(
        round=max(number, 0),
        reviewer=_text(reviewer),
        blockers=_items(blockers),
    )
    # A caller that does not name a round is appending a new BLOCK. Two BLOCKs
    # can share Stage-Gate's ``round`` value (the first two are both 0), so
    # replacing by round would lose the earlier blocker text.
    if round is None:
        entries.append(entry)
    else:
        for index, existing in enumerate(entries):
            if existing.round == entry.round and existing.reviewer == entry.reviewer:
                entries[index] = entry
                break
        else:
            entries.append(entry)
    entries = entries[-MAX_BLOCKED_ROUNDS:]
    snapshot[BLOCKER_BRIEF] = [item.to_dict() for item in entries]
    return tuple(entries)


#: The spelling a caller is likely to use when only the brief, not the round, is at hand.
record_blocker_brief = record_blocked_round


def consecutive_blocks(entries: Sequence[BlockedRound]) -> int:
    """BLOCKs from the end that belong to one reviewer's uninterrupted sequence.

    The first BLOCK of a sequence is not itself a failed fix round, so the
    result is the run length minus one. A changed reviewer ends the run, which
    is the same reset Stage-Gate applies. Rows with no reviewer are treated as
    belonging to the current reviewer so older snapshots keep counting.
    """
    run = 0
    reviewer = ""
    for entry in reversed(tuple(entries)):
        if reviewer and entry.reviewer and entry.reviewer != reviewer:
            break
        reviewer = reviewer or entry.reviewer
        run += 1
    return max(run - 1, 0)


def failed_fix_rounds(snapshot: Mapping[str, Any] | None) -> int:
    """How many fix rounds on the current reviewer's sequence were BLOCKed.

    ``blocking_rounds`` is Stage-Gate's authority and wins when present. A
    snapshot written before that field, or a unit-test fixture built by hand,
    falls back to the brief, then to ``fix_round``.
    """
    if isinstance(snapshot, int) and not isinstance(snapshot, bool):
        return max(snapshot, 0)
    if not isinstance(snapshot, Mapping):
        return 0
    raw = snapshot.get("blocking_rounds")
    authoritative = 0
    if raw is not None:
        try:
            authoritative = max(int(raw), 0)
        except (TypeError, ValueError):
            authoritative = 0
        if authoritative:
            return authoritative
    entries = _entries_from_snapshot(snapshot)
    if entries:
        derived = consecutive_blocks(entries)
        return max(derived, authoritative)
    if BLOCKER_BRIEF in snapshot:
        # The brief key exists and is empty: the sequence restarted.
        return authoritative
    raw_fix = snapshot.get("fix_round")
    if raw_fix is None:
        return 0
    try:
        return max(int(raw_fix), 0)
    except (TypeError, ValueError):
        return 0


def should_escalate(
    snapshot: Mapping[str, Any] | None,
    *,
    threshold: int = ESCALATION_THRESHOLD,
) -> bool:
    """True when the current reviewer's failed fix rounds reach ``threshold``."""
    return failed_fix_rounds(snapshot) >= max(1, int(threshold))


#: Names that read naturally at call sites.
escalation_due = should_escalate
should_escalate_to_planner = should_escalate


def summarize_blockers(entries: Sequence[BlockedRound]) -> str:
    """A deterministic, bounded summary naming every blocker so far."""
    if not entries:
        return "Blockers so far: none recorded."
    blocks = len(entries)
    flattened = [blocker for entry in entries for blocker in entry.blockers]
    lines = [
        f"{blocks} BLOCK(s) on this line; {len(flattened)} blocker item(s) so far.",
    ]
    for entry in entries:
        label = f"BLOCK round {entry.round}" if entry.round else "initial BLOCK"
        if entry.reviewer:
            label += f" by {entry.reviewer}"
        lines.append(f"{label}:")
        if entry.blockers:
            lines.extend(f"- {blocker}" for blocker in entry.blockers)
        else:
            lines.append("- (no blocker text was stored)")
    text = "\n".join(lines)
    return text[:MAX_SUMMARY_CHARS]


def _line_id(value: object) -> str:
    if isinstance(value, str):
        return value
    found = getattr(value, "id", "")
    return str(found or "")


def _normalise_snapshot(
    line: object,
    snapshot: object,
) -> tuple[str, Mapping[str, Any]]:
    """Accept the call shapes a caller may reach for.

    ``build_escalation(snapshot)``, ``build_escalation(line, snapshot)`` and
    ``build_escalation(line_id, snapshot)`` all arrive here. A bare list of
    blockers is handled by :func:`build_escalation` before this point.
    """
    if isinstance(line, Mapping) and snapshot is None:
        return _line_id(line.get("line_id")), line
    return _line_id(line), snapshot if isinstance(snapshot, Mapping) else {}


def build_escalation(
    line: object = "",
    snapshot: object = None,
    *,
    threshold: int = ESCALATION_THRESHOLD,
    blockers: Sequence[str] | str | None = None,
    rounds: int | None = None,
    fix_round: int | None = None,
    history: Sequence[BlockedRound] | None = None,
    line_id: str = "",
) -> Escalation | None:
    """Build the Planner escalation, or ``None`` when it is not due yet.

    ``line`` may be a :class:`Line`, a line id, a snapshot mapping, or (when
    called with keywords) empty. ``snapshot`` is the persisted line snapshot.
    ``blockers`` adds the current BLOCK when the caller has not written it into
    the snapshot yet; ``rounds`` overrides the derived failed-round count.
    """
    # ``build_escalation(["a", "b"], 2)``: a bare blocker list and a count.
    if isinstance(line, int) and not isinstance(line, bool):
        if rounds is None:
            rounds = line
        line = ""
    if isinstance(line, (list, tuple)) and all(
        isinstance(item, str) for item in line
    ):
        if blockers is None:
            blockers = line
        line = ""
    if isinstance(snapshot, int) and not isinstance(snapshot, bool):
        if rounds is None:
            rounds = snapshot
        snapshot = None
    resolved_line, snap = _normalise_snapshot(line, snapshot)
    if line_id:
        resolved_line = line_id
    line_id = resolved_line

    entries = list(history if history is not None else _entries_from_snapshot(snap))
    if not entries and snap.get("history"):
        entries = list(_entries_from_snapshot(snap))
    if blockers:
        current = _items(blockers)
        if current:
            number = len(entries)
            entries.append(
                BlockedRound(
                    round=number,
                    reviewer=_text(snap.get("blocking_reviewer") or snap.get("reviewer")),
                    blockers=current,
                )
            )
    if not entries and isinstance(snap.get(BLOCKER_BRIEF), Sequence):
        entries = list(_entries_from_snapshot(snap))

    failed = (
        max(int(rounds), 0)
        if rounds is not None
        else failed_fix_rounds(snap)
    )
    if failed < max(1, int(threshold)):
        return None

    flattened: list[str] = []
    for entry in entries:
        for blocker in entry.blockers:
            if blocker not in flattened:
                flattened.append(blocker)
    # A caller that supplied only blockers still gets those in the summary.
    if blockers:
        for blocker in _items(blockers):
            if blocker not in flattened:
                flattened.append(blocker)

    try:
        current_fix = int(
            fix_round
            if fix_round is not None
            else snap.get("fix_round")
            or failed
        )
    except (TypeError, ValueError):
        current_fix = failed

    return Escalation(
        line_id=line_id or _text(snap.get("line_id")),
        rounds=failed,
        blockers=tuple(flattened),
        summary=summarize_blockers(entries),
        instruction=ESCALATION_INSTRUCTION,
        fix_round=max(current_fix, 0),
        threshold=max(int(threshold), 1),
        history=tuple(entries),
    )


#: Explicit names for callers that prefer a verb.
build_planner_escalation = build_escalation
planner_escalation = build_escalation
#: Readable aliases for the event object itself.
PlannerEscalation = Escalation
EscalationEvent = Escalation
FIX_ROUNDS_BEFORE_ESCALATION = ESCALATION_THRESHOLD


def detect_escalation(
    line: object = "",
    snapshot: object = None,
    *,
    threshold: int = ESCALATION_THRESHOLD,
    blockers: Sequence[str] | str | None = None,
    rounds: int | None = None,
    line_id: str = "",
) -> Escalation | None:
    """Return the escalation when due, otherwise ``None`` (an alias of build)."""
    return build_escalation(
        line,
        snapshot,
        threshold=threshold,
        blockers=blockers,
        rounds=rounds,
        line_id=line_id,
    )


def write_escalation(
    snapshot: dict[str, Any] | Escalation,
    escalation: Escalation | dict[str, Any],
) -> dict[str, object]:
    """Persist one escalation under the stable snapshot key."""
    # Accept either argument order; both are useful at call sites.
    if isinstance(snapshot, Escalation) and isinstance(escalation, dict):
        snapshot, escalation = escalation, snapshot
    if not isinstance(snapshot, dict):
        raise TypeError("snapshot must be a dict")
    if not isinstance(escalation, Escalation):
        parsed = Escalation.from_dict(escalation)
        if parsed is None:
            raise TypeError("escalation must be an Escalation")
        escalation = parsed
    payload = escalation.to_dict()
    snapshot[ESCALATION] = payload
    return payload


#: A caller may phrase it as applying the event to the line snapshot.
apply_escalation = write_escalation
record_escalation = write_escalation


def escalation_from_snapshot(
    snapshot: Mapping[str, Any] | None,
    line_id: str = "",
) -> Escalation | None:
    """Read a persisted escalation, or ``None`` when the line has none."""
    if not isinstance(snapshot, Mapping):
        return None
    raw = snapshot.get(ESCALATION)
    if not isinstance(raw, Mapping):
        return None
    escalation = Escalation.from_dict(raw)
    if escalation is None:
        return None
    if line_id and escalation.line_id and escalation.line_id != line_id:
        return None
    return escalation


#: Read-side spelling used by the machine and operator views.
read_escalation = escalation_from_snapshot


def render_escalation(escalation: Escalation | Mapping[str, Any]) -> str:
    """Render an escalation object or its persisted dict for a prompt."""
    if isinstance(escalation, Escalation):
        return escalation.render()
    parsed = Escalation.from_dict(escalation)
    return parsed.render() if parsed is not None else ""


def escalation_brief(snapshot: Mapping[str, Any] | None) -> str:
    """The prompt block for a snapshot that already holds an escalation."""
    escalation = escalation_from_snapshot(snapshot)
    return escalation.render() if escalation is not None else ""


__all__ = [
    "BLOCKER_BRIEF",
    "BLOCKERS_BRIEF",
    "ESCALATION",
    "ESCALATION_INSTRUCTION",
    "ESCALATION_PLANNER_PREFIX",
    "ESCALATION_THRESHOLD",
    "FIX_ROUNDS_BEFORE_ESCALATION",
    "MAX_BLOCKED_ROUNDS",
    "MAX_BLOCKERS_PER_ROUND",
    "MAX_BLOCKER_CHARS",
    "BlockedRound",
    "Escalation",
    "EscalationEvent",
    "PlannerEscalation",
    "apply_escalation",
    "blocker_brief",
    "build_escalation",
    "build_planner_escalation",
    "consecutive_blocks",
    "detect_escalation",
    "escalation_brief",
    "escalation_due",
    "escalation_from_snapshot",
    "failed_fix_rounds",
    "planner_escalation",
    "read_escalation",
    "record_escalation",
    "record_blocked_round",
    "record_blocker_brief",
    "render_escalation",
    "should_escalate",
    "should_escalate_to_planner",
    "summarize_blockers",
    "write_escalation",
]
