"""Declared-path claims for active lines, across every dispatch in the store."""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.server.services.autonomy.role_plan import scopes_overlap

from .types import Dispatch, DispatchState, Line, LineStatus

ALLOW = "allow"
QUEUE = "queue"
REFUSE = "refuse"
PATH_OWNERSHIP_CONFLICT = "path_ownership_conflict"
MISSING_DECLARED_PATHS = "missing_declared_paths"
PATH_OWNERSHIP_CONTEXT_REQUIRED = "path_ownership_context_required"

_ACTIVE_LINE_STATES = frozenset(
    {
        LineStatus.ASSIGNED,
        LineStatus.RUNNING,
        LineStatus.REVIEWING,
        LineStatus.BLOCKED,
    }
)
_ACTIVE_DISPATCH_STATES = tuple(
    state.value
    for state in DispatchState
    if state not in (DispatchState.COMPLETE, DispatchState.ABORT, DispatchState.ERROR)
)
_BLOCKER_PREFIX = "Declared paths overlap active line "


#: One asyncio lock per event loop: every PathOwnership in the process (one is
#: built per selector) serializes check-and-assign on it.
_PROCESS_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def _process_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _PROCESS_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _PROCESS_LOCKS[loop] = lock
    return lock


def declared_paths(line: Line) -> tuple[str, ...]:
    """The line's ownership declaration, with its persisted legacy fallback."""
    return tuple(line.declared_paths or line.write_scope)


def _repository_identity(repo_root: str | Path | None) -> str:
    """One canonical spelling for a checkout, or ``""`` when it is unknown."""
    raw = str(repo_root or "").strip()
    if not raw:
        return ""
    try:
        return str(Path(raw).expanduser().resolve(strict=False))
    except OSError:
        return raw


def _same_repository(first: str, second: str) -> bool:
    """Whether two claims can touch the same files.

    Different registered workspace repositories are independent even when
    their lines declare the same relative path (``README.md`` in two
    checkouts is two files). An empty identity is the backend's default
    checkout or a legacy row that predates the column, so it is compared
    against every repository rather than being assumed disjoint.
    """
    if not first or not second:
        return True
    return first == second


@dataclass(frozen=True, slots=True)
class PathClaim:
    dispatch_id: str
    line_id: str
    paths: tuple[str, ...]
    #: The checkout the claim belongs to (``Dispatch.repo_root``). Empty for a
    #: dispatch that never selected a workspace.
    repo: str = ""


@dataclass(frozen=True, slots=True)
class OwnershipDecision:
    action: Literal["allow", "queue", "refuse"]
    reason: str = ""
    conflicts: tuple[PathClaim, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW

    @property
    def queued(self) -> bool:
        return self.action == QUEUE

    @property
    def refused(self) -> bool:
        return self.action == REFUSE


class PathOwnership:
    """Snapshot active path claims and decide whether a line may proceed.

    A conflict inside one dispatch is a permanent planning error and is refused.
    A conflict with another dispatch is temporary and is queued. Unstarted
    PENDING lines do not claim paths, so two queued dispatches cannot deadlock.
    The candidate line carries the stable reason, including both overlapping
    declarations; ``decisions`` retains the same verdict for an audit trail.
    """

    def __init__(
        self,
        store: object | None = None,
        *,
        claim_lock: Callable[[], AbstractAsyncContextManager[object]] | None = None,
    ) -> None:
        self._store = store
        self._claim_lock = claim_lock
        self._claims: dict[tuple[str, str], PathClaim] = {}
        self.decisions: list[OwnershipDecision] = []

    @asynccontextmanager
    async def guard(self) -> AsyncIterator[None]:
        """Hold while a dispatch checks its lines and saves the assignment.

        Claims are read from saved rows, so the check and the save that makes
        a line ASSIGNED (a claim) must not interleave with another dispatch's
        check. A process lock covers drives in this process; a store that
        offers ``path_claim_lock`` (the Postgres store) adds a database lock
        so another process that drives dispatches is serialized too.
        """
        async with _process_lock():
            factory = self._claim_lock or getattr(self._store, "path_claim_lock", None)
            if factory is None:
                yield
                return
            async with factory():
                yield

    @property
    def claims(self) -> tuple[PathClaim, ...]:
        return tuple(self._claims[key] for key in sorted(self._claims))

    def record(self, dispatch: Dispatch) -> None:
        """Replace one dispatch's claims with its current active lines."""
        for key in [key for key in self._claims if key[0] == dispatch.id]:
            self._claims.pop(key, None)
        if dispatch.state in (DispatchState.COMPLETE, DispatchState.ABORT, DispatchState.ERROR):
            return
        for line in dispatch.lines:
            if line.status not in _ACTIVE_LINE_STATES:
                continue
            paths = declared_paths(line)
            if not paths:
                continue
            key = (dispatch.id, line.id)
            self._claims[key] = PathClaim(
                dispatch.id,
                line.id,
                paths,
                _repository_identity(getattr(dispatch, "repo_root", "")),
            )

    def record_many(self, dispatches: Iterable[Dispatch]) -> None:
        for dispatch in dispatches:
            self.record(dispatch)

    def release(self, dispatch_id: str, line_id: str | None = None) -> None:
        """Release one line, or every line of a terminal dispatch."""
        for key in [
            key
            for key in self._claims
            if key[0] == dispatch_id and (line_id is None or key[1] == line_id)
        ]:
            self._claims.pop(key, None)

    def check(
        self, dispatch_id: str, line: Line, *, repo: str = ""
    ) -> OwnershipDecision:
        """Decide against the current snapshot without performing I/O.

        ``repo`` is the candidate's checkout identity. A claim from another
        checkout never conflicts, because the same relative path names a
        different file in each working tree.
        """
        paths = declared_paths(line)
        if not paths:
            return self._record_decision(
                line,
                OwnershipDecision(REFUSE, f"{MISSING_DECLARED_PATHS}:{line.id}"),
            )
        conflicts = tuple(
            claim
            for claim in self.claims
            if (claim.dispatch_id, claim.line_id) != (dispatch_id, line.id)
            and _same_repository(claim.repo, _repository_identity(repo))
            and any(
                scopes_overlap(mine, theirs)
                for mine in paths
                for theirs in claim.paths
            )
        )
        if not conflicts:
            self._clear_reason(line)
            return OwnershipDecision(ALLOW)
        owner = conflicts[0]
        action = (
            REFUSE
            if any(claim.dispatch_id == dispatch_id for claim in conflicts)
            else QUEUE
        )
        mine, theirs = next(
            (mine, theirs)
            for mine in paths
            for theirs in owner.paths
            if scopes_overlap(mine, theirs)
        )
        reason = (
            f"{PATH_OWNERSHIP_CONFLICT}:{owner.dispatch_id}:{owner.line_id}:"
            f"{mine}|{theirs}"
        )
        return self._record_decision(
            line,
            OwnershipDecision(action, reason, conflicts),
        )

    async def decide(
        self,
        dispatch_id: str,
        line: Line,
        dispatch: Dispatch | None = None,
    ) -> OwnershipDecision:
        """Refresh persisted active claims, then decide for one candidate line."""
        if self._store is not None:
            reader = getattr(self._store, "list", None)
            if reader is None:
                raise TypeError("PathOwnership store must provide list()")
            rows = await reader(states=list(_ACTIVE_DISPATCH_STATES), limit=10_000)
            self._claims.clear()
            self.record_many(rows)
        if dispatch is not None:
            self.record(dispatch)
        return self.check(
            dispatch_id,
            line,
            repo=_repository_identity(
                getattr(dispatch, "repo_root", "") if dispatch is not None else ""
            ),
        )

    def _record_decision(self, line: Line, decision: OwnershipDecision) -> OwnershipDecision:
        self.decisions.append(decision)
        line.failure_reason = decision.reason
        blocker = _blocker(decision)
        if blocker and blocker not in line.blockers:
            line.blockers.append(blocker)
        return decision

    @staticmethod
    def _clear_reason(line: Line) -> None:
        if (line.failure_reason or "").startswith(
            (PATH_OWNERSHIP_CONFLICT, MISSING_DECLARED_PATHS, PATH_OWNERSHIP_CONTEXT_REQUIRED)
        ):
            line.failure_reason = None
        line.blockers[:] = [
            item for item in line.blockers if not item.startswith(_BLOCKER_PREFIX)
        ]


def _blocker(decision: OwnershipDecision) -> str:
    if not decision.conflicts:
        return decision.reason
    owner = decision.conflicts[0]
    return (
        f"{_BLOCKER_PREFIX}{owner.dispatch_id}/{owner.line_id}: "
        + ", ".join(owner.paths)
    )


__all__ = [
    "ALLOW",
    "MISSING_DECLARED_PATHS",
    "PATH_OWNERSHIP_CONFLICT",
    "PATH_OWNERSHIP_CONTEXT_REQUIRED",
    "QUEUE",
    "REFUSE",
    "OwnershipDecision",
    "PathClaim",
    "PathOwnership",
    "declared_paths",
]
