"""Deterministic doubles for the collaborators that are not merged yet.

These are not test fixtures: they are the documented dev surface from the
design, so a dispatch can be planned, run, reviewed and gated on a machine
with no impact-map, no skills-hub and no memory-p0. The real adapters slot in
behind the same protocols without the machine changing.
"""

from __future__ import annotations

from collections.abc import Collection

from typing import Any

from .pool import DEV_MODEL_POOL
from .protocols import ModelRole
from .types import Dispatch, Line, Outcome, RunStatus, Verdict

FIXED_PLAN: tuple[tuple[str, str], ...] = (
    ("survey", "Survey the existing code and write a short findings note"),
    ("implement", "Implement the smallest change that satisfies the goal"),
    ("verify", "Run the relevant tests and report what passed and what did not"),
)


class FixedPlanner:
    """The design's mock planner: always the same three lines."""

    async def plan(self, goal: str) -> list[Line]:
        return [
            Line(id=f"line-{index}", name=name, task=task)
            for index, (name, task) in enumerate(FIXED_PLAN)
        ]


class RoundRobinSelector:
    """The design's mock selector: least-assigned eligible model, no quota view."""

    def __init__(
        self,
        pool: tuple[str, ...] = DEV_MODEL_POOL,
        *,
        allowed_workers: Collection[str] = (),
    ) -> None:
        self._pool = pool
        self._counts: dict[str, int] = {}
        #: L2: a dispatch's worker restriction narrows work picks only.
        self._allowed = frozenset(allowed_workers)

    async def select(
        self,
        line: Line,
        used_models: list[str],
        *,
        role: ModelRole = ModelRole.WORK,
        exclude: Collection[str] = (),
    ) -> str | None:
        skip = set(used_models) | {str(model) for model in exclude}
        candidates = [model for model in self._pool if model not in skip]
        if self._allowed and role is ModelRole.WORK:
            candidates = [model for model in candidates if model in self._allowed]
        if not candidates:
            return None
        candidates.sort(key=lambda model: (self._counts.get(model, 0), self._pool.index(model)))
        chosen = candidates[0]
        self._counts[chosen] = self._counts.get(chosen, 0) + 1
        return chosen


class AlwaysPassReviewer:
    """The design's mock reviewer: every line passes with no blockers."""

    def __init__(self) -> None:
        self.reviewed: list[tuple[str, str]] = []

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.reviewed.append((line_id, model))
        return Verdict.ok(f"reviewed by {model}")


class RecordingLauncher:
    """Hands out run ids and records what it was asked to launch.

    The counter is per line, so a run id says which line it belongs to
    (``run-<line>-<attempt>``) and a scripted monitor can key on it.
    """

    def __init__(self) -> None:
        self.launches: list[tuple[str, str, list[str]]] = []
        self._counts: dict[str, int] = {}

    async def launch(self, line: Line, model: str, skills: list[str]) -> str:
        seen = self._counts.get(line.id, 0)
        self._counts[line.id] = seen + 1
        # The machine persists this id before the call. Honor it so the worker
        # and the stored row are the same run.
        run_id = line.current_run or f"run-{line.id}-{seen + 1}"
        self.launches.append((line.id, model, list(skills)))
        return run_id


class ScriptedMonitor:
    """Replays a fixed script of statuses per run id, then reports completion.

    ``script`` maps a run id to the statuses to return before completion, so a
    test can make one line stall and another finish without any clock.
    """

    def __init__(self, script: dict[str, list[RunStatus]] | None = None) -> None:
        self._script = script or {}
        self._seen: dict[str, int] = {}
        self.cancelled: list[str] = []
        self.polls = 0

    async def poll(self, run_id: str) -> RunStatus:
        self.polls += 1
        statuses = self._script.get(run_id) or []
        index = self._seen.get(run_id, 0)
        self._seen[run_id] = index + 1
        if index < len(statuses):
            return statuses[index]
        return RunStatus(output=f"done {run_id}", complete=True, tokens=16)

    async def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)


class NoopMerger:
    """Records merges and stands in for the git-backed merger in tests."""

    def __init__(self, *, conflict_on: str | None = None) -> None:
        self.merged: list[str] = []
        self.pushed: list[str] = []
        self.aside: str | None = None
        self._conflict_on = conflict_on

    async def merge_line(self, line_id: str) -> str:
        if self._conflict_on == line_id:
            from .protocols import MergeConflictError

            raise MergeConflictError(f"conflict on {line_id}", [f"src/{line_id}.py"])
        self.merged.append(line_id)
        return "integrate/auto-dispatch-test"

    async def push(self, branch: str, approval_token: str) -> str:
        self.pushed.append(branch)
        return branch

    async def set_aside(self) -> str:
        self.aside = "integrate/auto-dispatch-test-rejected"
        return self.aside


class InMemoryDispatchStore:
    """A dispatch store with no database, for the CLI-less path and tests."""

    def __init__(self) -> None:
        self.rows: dict[str, Dispatch] = {}

    async def create(self, dispatch: Dispatch) -> Dispatch:
        self.rows[dispatch.id] = dispatch
        return dispatch

    async def save(self, dispatch: Dispatch) -> Dispatch:
        self.rows[dispatch.id] = dispatch
        return dispatch

    async def get(self, dispatch_id: str) -> Dispatch | None:
        return self.rows.get(dispatch_id)

    async def list(
        self,
        *,
        user_id: str | None = None,
        states: list[str] | None = None,
        limit: int = 50,
    ) -> list[Dispatch]:
        rows = list(self.rows.values())
        if user_id is not None:
            rows = [row for row in rows if row.user_id == user_id]
        if states:
            wanted = set(states)
            rows = [row for row in rows if row.state.value in wanted]
        return rows[:limit]

    async def pending_approval(self, user_id: str | None = None) -> list[Dispatch]:
        from .types import DispatchState

        return [
            row
            for row in await self.list(user_id=user_id, limit=len(self.rows) or 1)
            if row.state is DispatchState.APPROVAL
        ]


class InMemoryOutcomeStore:
    """The design's mock outcome store, in the shape memory-p0 will take."""

    def __init__(self) -> None:
        self.recorded: dict[str, dict[str, Outcome]] = {}

    async def record(self, dispatch_id: str, outcomes: dict[str, Outcome]) -> None:
        self.recorded[dispatch_id] = dict(outcomes)


def outcome_row(outcome: Outcome) -> dict[str, Any]:
    return outcome.to_dict()
