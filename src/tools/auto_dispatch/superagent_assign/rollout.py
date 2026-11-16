"""Q8 staged rollout of Super-agent assignment, with automatic rollback.

Owner decision (2026-11-12, Q8): shadow for 30 lines, then a 50% sampled enforce until
40 lines have run on the Super-agent's choice, then full enforce; after the
switch, roll back to shadow automatically when the Super-agent cohort's pass
rate is more than 10 points below the selector cohort or its cost per line is
more than 50% higher (``modes.rollback_due``).

The stage is derived from the recorded assignment entries and outcomes of
recent dispatches -- no new table, no stored state to drift:

* A ``shadow``: fewer than ``SHADOW_LINES`` lines with a recorded shadow
  choice -> ``("shadow", 1.0)``;
* B ``sampled``: fewer than ``ADOPTED_LINES`` adopted lines ->
  ``("enforce", 0.5)``;
* C ``full``: ``("enforce", 1.0)``; rollback -> ``("shadow", 1.0)`` and
  ``rolled_back`` with the reasons.

Only ``CONDUCTOR_DISPATCH_ASSIGN_MODE=auto`` follows the stages (the owner,
2026-11-14). Unset means ``shadow``, L1's default; ``off``, ``shadow`` and
``enforce`` (with ``_SAMPLE``) are used as given.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from statistics import median
from typing import Any, Iterable, Mapping

from .modes import ASSIGN_MODE_ENV, assign_mode, assign_sample, rollback_reasons
from .record import assignment_entries

SHADOW_LINES = 30
ADOPTED_LINES = 40
SAMPLE_B = 0.5
MIN_COHORT = 10
AUTO = "auto"
STAGE_SHADOW = "shadow"
STAGE_SAMPLED = "sampled"
STAGE_FULL = "full"
STAGE_OVERRIDE = "override"
_ADOPTED_SOURCES = frozenset({"superagent", "superagent_reask"})


@dataclass(frozen=True)
class RolloutState:
    stage: str
    mode: str
    sample: float
    shadow_lines: int = 0
    adopted_lines: int = 0
    rolled_back: bool = False
    reasons: tuple[str, ...] = ()
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "mode": self.mode,
            "sample": self.sample,
            "shadow_lines": self.shadow_lines,
            "adopted_lines": self.adopted_lines,
            "rolled_back": self.rolled_back,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
        }


def _lines(dispatches: Iterable[Any]) -> Iterable[tuple[Any, str, Mapping[str, Any]]]:
    for dispatch in dispatches:
        gates = getattr(dispatch, "gates", None)
        cells = gates.get("lines") if isinstance(gates, Mapping) else None
        if not isinstance(cells, Mapping):
            continue
        for line_id, snap in cells.items():
            if isinstance(snap, Mapping) and snap.get("assignment"):
                yield dispatch, str(line_id), snap


def _cohorts(dispatches: Iterable[Any]) -> tuple[int, int, dict[str, list[tuple[bool, float]]]]:
    shadow = adopted = 0
    cohorts: dict[str, list[tuple[bool, float]]] = {"superagent": [], "selector": []}
    for dispatch, line_id, snap in _lines(dispatches):
        entries = assignment_entries(snap.get("assignment"))
        if not entries:
            continue
        first = entries[0]
        mode = str(first.get("mode") or "")
        source = str(first.get("source") or "")
        if mode == "shadow":
            shadow += 1
            continue
        if mode != "enforce":
            continue
        group = "superagent" if source in _ADOPTED_SOURCES else "selector"
        if group == "superagent":
            adopted += 1
        outcomes = getattr(dispatch, "outcomes", {}) or {}
        outcome = outcomes.get(line_id) if isinstance(outcomes, Mapping) else None
        if outcome is not None and hasattr(outcome, "passed"):
            cohorts[group].append(
                (bool(outcome.passed), float(getattr(outcome, "duration_secs", 0) or 0))
            )
    return shadow, adopted, cohorts


def cohort_metrics(cohorts: Mapping[str, list[tuple[bool, float]]]) -> dict[str, Any]:
    """The two §6.1 C rollback readings; ``insufficient`` under ``MIN_COHORT``."""
    sup = cohorts.get("superagent") or []
    sel = cohorts.get("selector") or []
    if len(sup) < MIN_COHORT or len(sel) < MIN_COHORT:
        return {"pass_rate": "insufficient", "cost_per_line": "insufficient"}

    def rate(rows: list[tuple[bool, float]]) -> float:
        return 100.0 * sum(1 for ok, _ in rows if ok) / len(rows)

    def cost(rows: list[tuple[bool, float]]) -> float:
        return float(median([secs for _, secs in rows]))

    return {
        "pass_rate": {"superagent": rate(sup), "selector": rate(sel)},
        "cost_per_line": {"superagent": cost(sup), "selector": cost(sel)},
    }


_ORDER = {STAGE_SHADOW: 0, STAGE_SAMPLED: 1, STAGE_FULL: 2}
_STAGE_MODE = {
    STAGE_SHADOW: ("shadow", 1.0),
    STAGE_SAMPLED: ("enforce", SAMPLE_B),
    STAGE_FULL: ("enforce", 1.0),
}


def rollout_state(
    dispatches: Iterable[Any],
    *,
    environ: Mapping[str, str] | None = None,
    persisted: RolloutState | None = None,
) -> RolloutState:
    """The effective mode and sample for the next assignment.

    ``persisted`` is the stored stage (``dispatch_assign_rollout``). The stage
    only moves forward, so a sliding window of recent dispatches cannot drop
    a ``full`` rollout back to shadow or a ``sampled`` one back to the start;
    a rollback is sticky until an operator clears the stored row. Rollback is
    checked in the ``sampled`` stage as well as in ``full``.
    """
    env = os.environ if environ is None else environ
    raw = str(env.get(ASSIGN_MODE_ENV, "") or "").strip().lower()
    if raw != AUTO:
        # Owner decision (2026-11-14): unset means shadow (L1's default); only an explicit
        # ``auto`` starts the staged rollout.
        return RolloutState(STAGE_OVERRIDE, assign_mode(env), assign_sample(env))
    if persisted is not None and persisted.rolled_back:
        return RolloutState(
            STAGE_SHADOW, "shadow", 1.0, persisted.shadow_lines, persisted.adopted_lines,
            True, tuple(persisted.reasons), dict(persisted.metrics),
        )
    shadow, adopted, cohorts = _cohorts(list(dispatches))
    if shadow < SHADOW_LINES:
        counted = STAGE_SHADOW
    elif adopted < ADOPTED_LINES:
        counted = STAGE_SAMPLED
    else:
        counted = STAGE_FULL
    stage = counted
    if persisted is not None and _ORDER.get(persisted.stage, -1) > _ORDER[counted]:
        stage = persisted.stage
    metrics: Mapping[str, Any] = {}
    if stage in (STAGE_SAMPLED, STAGE_FULL):
        metrics = cohort_metrics(cohorts)
        reasons = rollback_reasons(metrics)
        if reasons:
            return RolloutState(
                STAGE_SHADOW, "shadow", 1.0, shadow, adopted, True, tuple(reasons), metrics
            )
    mode, sample = _STAGE_MODE[stage]
    return RolloutState(stage, mode, sample, shadow, adopted, metrics=metrics)


class InMemoryRolloutStore:
    def __init__(self, state: RolloutState | None = None) -> None:
        self.state = state

    async def get(self) -> RolloutState | None:
        return self.state

    async def set(self, state: RolloutState) -> None:
        self.state = state


class PostgresRolloutStore:
    """The migration-151 ``dispatch_assign_rollout`` row."""

    async def get(self) -> RolloutState | None:
        from src.server.database.dispatch_assign_rollout import get_rollout

        row = await get_rollout()
        if row is None or row.get("stage") not in _ORDER:
            return None
        mode, sample = _STAGE_MODE[row["stage"]]
        return RolloutState(
            str(row["stage"]), mode, sample,
            rolled_back=bool(row.get("rolled_back")),
            reasons=tuple(row.get("reasons") or ()),
        )

    async def set(self, state: RolloutState) -> None:
        from src.server.database.dispatch_assign_rollout import set_rollout

        await set_rollout(
            stage=state.stage, rolled_back=state.rolled_back, reasons=list(state.reasons)
        )


async def advance(
    dispatches: Iterable[Any], store: Any, *, environ: Mapping[str, str] | None = None
) -> RolloutState:
    """Compute the next state from the stored one and persist it when it moved."""
    persisted = await store.get()
    state = rollout_state(dispatches, environ=environ, persisted=persisted)
    if state.stage == STAGE_OVERRIDE:
        return state
    if (
        persisted is None
        or persisted.stage != state.stage
        or persisted.rolled_back != state.rolled_back
    ):
        await store.set(state)
    return state
