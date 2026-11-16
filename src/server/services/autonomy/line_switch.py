"""Switch a stuck line (a Ticket's Worker) to another model, keeping its worktree.

Pure planning: detection, target choice and the handoff text. Persisting the
switch and relaunching the Worker belong to the caller, so every rule here is
testable with plain values.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

REASON_STALL = "stall"
REASON_FAILURES = "failures"
REASON_BREAKER = "breaker_open"
REASON_MANUAL = "manual"

_NOTICE = "[handoff] This line was switched to a new model; the worktree is unchanged."


class NoEligibleTarget(Exception):
    """No candidate satisfies the reviewer and breaker rules."""


@dataclass(frozen=True, slots=True)
class ModelRef:
    provider: str
    model: str

    def key(self) -> tuple[str, str]:
        return (self.provider.strip().lower(), self.model.strip().lower())

    def as_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "model": self.model}


@dataclass(frozen=True, slots=True)
class SwitchPolicy:
    stall_seconds: float = 900.0
    max_failures: int = 3


@dataclass(frozen=True, slots=True)
class LineState:
    current: ModelRef
    idle_seconds: float = 0.0
    consecutive_failures: int = 0


@dataclass(frozen=True, slots=True)
class Handoff:
    summary: str
    diff: str = ""
    findings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SwitchPlan:
    reason: str
    source: ModelRef
    target: ModelRef
    handoff_text: str
    record: dict[str, Any] = field(default_factory=dict)


def detect_stuck(
    line: LineState, policy: SwitchPolicy, open_providers: Iterable[str] = ()
) -> str | None:
    """Return why the line is stuck, or None. A tripped breaker wins."""
    if line.current.provider.lower() in {p.lower() for p in open_providers}:
        return REASON_BREAKER
    if line.consecutive_failures >= policy.max_failures:
        return REASON_FAILURES
    if line.idle_seconds >= policy.stall_seconds:
        return REASON_STALL
    return None


def pick_target(
    candidates: Sequence[ModelRef],
    current: ModelRef,
    reviewer: ModelRef | None = None,
    open_providers: Iterable[str] = (),
) -> ModelRef:
    """First candidate (in preference order) that is not the current model,
    not the reviewer, and whose provider breaker is closed."""
    blocked = {p.lower() for p in open_providers}
    for cand in candidates:
        if not cand.provider.strip() or not cand.model.strip():
            continue
        if cand.key() == current.key():
            continue
        if reviewer is not None and cand.key() == reviewer.key():
            continue
        if cand.provider.lower() in blocked:
            continue
        return cand
    raise NoEligibleTarget("no candidate is free of the current model, reviewer and open breakers")


def _clip(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def build_handoff(
    messages: Sequence[dict[str, str]],
    diff: str,
    findings: Sequence[str],
    *,
    limit: int = 4000,
    recent: int = 6,
) -> Handoff:
    """Compact the conversation tail, last diff and open findings.

    The budget is split so the diff and findings cannot crowd out the summary:
    summary 40%, diff 40%, findings the rest.
    """
    tail = [m for m in messages if str(m.get("content") or "").strip()][-recent:]
    lines = [f"{m.get('role', 'unknown')}: {' '.join(str(m['content']).split())}" for m in tail]
    per_line = max(limit * 4 // 10 // max(len(lines), 1), 40)
    summary = "\n".join(_clip(line, per_line) for line in lines)
    kept: list[str] = []
    budget = limit - limit * 8 // 10
    for item in findings:
        item = _clip(" ".join(str(item).split()), 200)
        if len(item) > budget:
            break
        kept.append(item)
        budget -= len(item)
    return Handoff(_clip(summary, limit * 4 // 10), _clip(diff, limit * 4 // 10), tuple(kept))


def render_handoff(handoff: Handoff) -> str:
    parts = [_NOTICE, "Recent conversation:\n" + (handoff.summary or "(none)")]
    if handoff.diff:
        parts.append("Last diff:\n" + handoff.diff)
    if handoff.findings:
        parts.append("Open findings:\n" + "\n".join(f"- {f}" for f in handoff.findings))
    return "\n\n".join(parts)


def inject_handoff(prompt: str, handoff: Handoff) -> str:
    return f"{render_handoff(handoff)}\n\n---\n\n{prompt}"


def plan_switch(
    line: LineState,
    candidates: Sequence[ModelRef],
    handoff: Handoff,
    *,
    reason: str,
    reviewer: ModelRef | None = None,
    open_providers: Iterable[str] = (),
    explicit: ModelRef | None = None,
    attempt: int = 0,
    now: datetime | None = None,
) -> SwitchPlan:
    """Choose the target (an explicit one must still pass the rules) and build the record."""
    pool = [explicit] if explicit is not None else list(candidates)
    target = pick_target(pool, line.current, reviewer, open_providers)
    text = render_handoff(handoff)
    record = {
        "at": (now or datetime.now(timezone.utc)).isoformat(),
        "reason": reason,
        "from": line.current.as_dict(),
        "to": target.as_dict(),
        "handoff_chars": len(text),
        "attempt": attempt,
    }
    return SwitchPlan(reason, line.current, target, text, record)
