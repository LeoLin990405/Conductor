"""The collaborator contracts an auto-dispatch run is built from.

Every one is a ``Protocol`` so the orchestrator can be written, run and tested
before impact-map, skills-hub and memory-p0 land: the dev doubles in
``dev.py`` satisfy these shapes and the real adapters slot in behind them.
"""

from __future__ import annotations

from collections.abc import Collection

import enum
from typing import Any, Protocol, runtime_checkable

from .types import Dispatch, Line, Outcome, RunStatus, Verdict


class ModelRole(str, enum.Enum):
    """What a model is being picked for, which decides who is eligible."""

    WORK = "work"
    REVIEW = "review"
    RESEARCH = "research"


@runtime_checkable
class Planner(Protocol):
    """Decomposes a goal into parallel lines."""

    async def plan(self, goal: str) -> list[Line]:
        """Return the lines to run in parallel; empty means the goal was not decomposable."""
        ...


@runtime_checkable
class ModelSelector(Protocol):
    """Assigns a model to a line, honouring quota and role constraints."""

    async def select(
        self,
        line: Line,
        used_models: list[str],
        *,
        role: ModelRole = ModelRole.WORK,
        exclude: Collection[str] = (),
        dispatch: Dispatch | None = None,
        dispatch_id: str | None = None,
    ) -> str | None:
        """Return an eligible model, or None when the pool is exhausted.

        ``exclude`` names models that must never be returned, for any role (a
        provider skipped for new launches, the model a line is moving off).
        ``used_models`` is only a preference for work picks. Work selectors
        with path ownership also need ``dispatch`` or ``dispatch_id``.
        """
        ...


@runtime_checkable
class Launcher(Protocol):
    """Starts a line's work and hands back a run id for the Monitor."""

    async def launch(self, line: Line, model: str, skills: list[str]) -> str:
        """Return the run id, or raise :class:`LaunchError` when the run never started."""
        ...


@runtime_checkable
class Monitor(Protocol):
    """Watches a running line and reports progress."""

    async def poll(self, run_id: str) -> RunStatus:
        """Return the latest status; ``stalled`` and ``complete`` are mutually exclusive."""
        ...

    async def cancel(self, run_id: str) -> None:
        """Stop a run whose stall or timeout has burned its attempt."""
        ...


@runtime_checkable
class Reviewer(Protocol):
    """Cross-reviews one line's output with a model that did not write it."""

    async def review_line(self, line_id: str, model: str) -> Verdict:
        """Return pass, or the blockers the author has to clear."""
        ...


@runtime_checkable
class Merger(Protocol):
    """Merges line branches into the dispatch's integrate branch."""

    async def merge_line(self, line_id: str) -> str:
        """Merge one line and return the integrate branch it landed on."""
        ...

    async def push(self, branch: str, approval_token: str) -> str:
        """Push an integrate branch, but only against a resolved approval token."""
        ...

    async def set_aside(self) -> str:
        """Move a rejected integrate branch aside rather than deleting it."""
        ...


class ApprovalDecision(str, enum.Enum):
    """Where a pending dispatch's gate stands."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@runtime_checkable
class ApprovalGate(Protocol):
    """The human gate between a finished dispatch and any merge or push."""

    async def request_approval(self, dispatch: Dispatch) -> str:
        """Store a human-readable summary and return the token that unblocks the gate."""
        ...

    async def decision(self, token: str) -> ApprovalDecision:
        """Return the gate's current state for a token."""
        ...

    async def resolve(self, token: str, approved: bool, note: str = "") -> ApprovalDecision:
        """Record a human decision and return it."""
        ...


@runtime_checkable
class OutcomeStore(Protocol):
    """Where a completed dispatch's per-line outcomes are recorded."""

    async def record(self, dispatch_id: str, outcomes: dict[str, Outcome]) -> None:
        """Persist the outcomes for one dispatch."""
        ...


@runtime_checkable
class DispatchStore(Protocol):
    """Durable dispatch state: the single writer is the running orchestrator."""

    async def create(self, dispatch: Dispatch) -> Dispatch: ...

    async def save(self, dispatch: Dispatch) -> Dispatch: ...

    async def get(self, dispatch_id: str) -> Dispatch | None: ...

    async def list(
        self,
        *,
        user_id: str | None = None,
        states: list[str] | None = None,
        limit: int = 50,
    ) -> list[Dispatch]: ...

    async def pending_approval(self, user_id: str | None = None) -> list[Dispatch]:
        """Dispatches parked at APPROVAL, which is what a deploy preflight blocks on."""
        ...


class LaunchError(RuntimeError):
    """A line's run never started; the line is reassigned, not failed."""


class UncommittedChangesError(LaunchError):
    """A Worker finished with dirty files; its worktree must be kept for recovery."""

    def __init__(self, message: str, *, run_id: str | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id


class NoNewCommitError(LaunchError):
    """A Worker exited cleanly without creating a commit on its line branch.

    ``branch_has_commits`` is True when the branch is already ahead of the
    line's ``base_commit``: an earlier run committed the work, and this run
    (a retry or a fix round) found nothing left to change. The machine then
    sends the existing branch on to G2 and review. False is a line with no
    commit at all, which still blocks.
    """

    def __init__(
        self,
        message: str,
        *,
        run_id: str | None = None,
        branch_has_commits: bool = False,
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.branch_has_commits = branch_has_commits
        # The branch HEAD the run left (the same as its start): the machine
        # matches it against the heads it recorded for this line.
        self.head: str | None = None
        # The resolved base_commit id: where the run chain starts when no
        # head of this line has been submitted yet.
        self.base: str | None = None


class MergeConflictError(RuntimeError):
    """A line could not be merged; carries the conflicting paths."""

    def __init__(self, message: str, paths: list[str] | None = None) -> None:
        super().__init__(message)
        self.paths = list(paths or [])


class GateNotApprovedError(RuntimeError):
    """A merge or push was attempted without a resolved approval token."""


class DispatchError(RuntimeError):
    """A dispatch-level failure that ends the run in ERROR."""


def line_payload(line: Line) -> dict[str, Any]:
    """The line's serialised form, kept here so stores and the CLI agree on it."""
    return line.to_dict()
