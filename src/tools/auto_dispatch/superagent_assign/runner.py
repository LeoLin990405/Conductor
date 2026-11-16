"""One assignment round: ask, check feasibility, re-ask once, fall back (N73 L4).

PRD 4.3-4.6. The dispatcher only checks *feasibility*; the Super-agent picks
model, effort and runtime. Per call:

1. one structured call for every unassigned line (``AssignBackend.assign``);
2. each choice is checked against the line's feasibility snapshot;
3. infeasible lines are asked again **once**, with their reason codes;
4. still infeasible, or an ERROR result (never a choice) -> today's selector.

The selector's pick is always the counterfactual (``selector_choice``) and is
recorded in every mode. ``shadow`` launches the selector's pick; ``enforce``
launches the Super-agent's pick on sampled lines (``ASSIGN_SAMPLE``); ``off``
never calls this module. Hard rules stay with feasibility: claude and codex
are never authors (Q1), the line's reviewer is never its author.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .feasibility import Snapshot, check_feasibility
from .modes import adopts_superagent
from .types import FeasibilityResult, LineChoice, Source, is_error

FALLBACK_ERROR_PREFIX = "error:"
FALLBACK_INFEASIBLE = "infeasible_twice"
FALLBACK_MISSING = "no_choice"


@dataclass
class LineDecision:
    line_id: str
    mode: str
    source: str
    choice: LineChoice | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    reask: bool = False
    fallback_reason: str | None = None
    latency_ms: float | None = None
    inputs: Mapping[str, Any] | None = None

    @property
    def usable(self) -> bool:
        return self.choice is not None and self.source in (
            Source.SUPERAGENT.value,
            Source.SUPERAGENT_REASK.value,
        )

    def adopted(self, sample: float) -> bool:
        return self.usable and adopts_superagent(self.line_id, mode=self.mode, sample=sample)


def _attempt(choice: LineChoice | None, result: FeasibilityResult) -> dict[str, Any]:
    out: dict[str, Any] = result.to_dict()
    if choice is not None:
        out["model"] = choice.model
    return out


async def decide(
    lines: Sequence[Any],
    *,
    mode: str,
    backend: Any,
    snapshot_for: Callable[[Any], Snapshot],
    inputs_for: Callable[[Sequence[Any]], Awaitable[Any]],
    hints: Sequence[tuple[str, str]] = (),
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, LineDecision]:
    """The decision for each line; ``{}`` for ``off`` or no lines."""
    if mode == "off" or not lines:
        return {}
    by_id = {str(line.id): line for line in lines}
    started = clock()
    inputs = await inputs_for(list(lines))
    if hints:
        inputs = replace(inputs, hints=tuple(hints))
    plan = await backend.assign(inputs)
    latency = round((clock() - started) * 1000.0, 1)
    decisions: dict[str, LineDecision] = {}
    if is_error(plan):
        reason = f"{FALLBACK_ERROR_PREFIX}{plan.kind.value}"
        for line_id in by_id:
            decisions[line_id] = LineDecision(
                line_id, mode, Source.SELECTOR_FALLBACK.value, fallback_reason=reason,
                latency_ms=latency,
            )
        return decisions
    choices = plan.by_line()
    retry: dict[str, str] = {}
    for line_id, line in by_id.items():
        choice = choices.get(line_id)
        if choice is None:
            decisions[line_id] = LineDecision(
                line_id, mode, Source.SELECTOR_FALLBACK.value,
                fallback_reason=FALLBACK_MISSING, latency_ms=latency,
            )
            continue
        result = check_feasibility(choice, snapshot_for(line))
        decision = LineDecision(
            line_id, mode, Source.SUPERAGENT.value, choice=choice,
            attempts=[_attempt(choice, result)], latency_ms=latency,
        )
        decisions[line_id] = decision
        if not result.is_ok:
            retry[line_id] = result.reason_code
    if not retry:
        return decisions
    # Re-ask once, only the infeasible lines, with their reason codes.
    again_inputs = await inputs_for([by_id[i] for i in retry])
    again_inputs = replace(
        again_inputs, hints=tuple(hints), infeasible=tuple(sorted(retry.items()))
    )
    second = await backend.assign(again_inputs)
    latency = round((clock() - started) * 1000.0, 1)
    second_choices = {} if is_error(second) else second.by_line()
    for line_id in retry:
        decision = decisions[line_id]
        decision.reask = True
        decision.latency_ms = latency
        choice = second_choices.get(line_id)
        if is_error(second):
            decision.source = Source.SELECTOR_FALLBACK.value
            decision.fallback_reason = f"{FALLBACK_ERROR_PREFIX}{second.kind.value}"
            decision.choice = None
            continue
        if choice is None:
            decision.source = Source.SELECTOR_FALLBACK.value
            decision.fallback_reason = FALLBACK_MISSING
            decision.choice = None
            continue
        result = check_feasibility(choice, snapshot_for(by_id[line_id]))
        decision.attempts.append(_attempt(choice, result))
        if result.is_ok:
            decision.source = Source.SUPERAGENT_REASK.value
            decision.choice = choice
        else:
            decision.source = Source.SELECTOR_FALLBACK.value
            decision.fallback_reason = FALLBACK_INFEASIBLE
            decision.choice = None
    return decisions
