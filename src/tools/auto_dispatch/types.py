"""Shapes of an auto-dispatch run. Documentation only; stores keep dicts.

The design in ``docs/ports/auto-dispatch.md`` is the source of truth; these
are the Python spellings of its YAML block.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class DispatchState(str, enum.Enum):
    """Where a dispatch is in the orchestrator's lifecycle."""

    SUBMITTED = "SUBMITTED"
    PLANNING = "PLANNING"
    ASSIGNED = "ASSIGNED"
    LAUNCHING = "LAUNCHING"
    MONITORING = "MONITORING"
    REVIEW = "REVIEW"
    APPROVAL = "APPROVAL"
    MERGE = "MERGE"
    COMPLETE = "COMPLETE"
    ABORT = "ABORT"
    ERROR = "ERROR"


TERMINAL_STATES = frozenset({DispatchState.COMPLETE, DispatchState.ABORT, DispatchState.ERROR})


class LineStatus(str, enum.Enum):
    """Where one parallel work line is."""

    PENDING = "PENDING"
    ASSIGNED = "ASSIGNED"
    RUNNING = "RUNNING"
    REVIEWING = "REVIEWING"
    PASSED = "PASSED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


def _str_tuple(value: Any) -> tuple[str, ...]:
    """A stored list (or a missing key) as a tuple of strings."""
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


@dataclass
class Line:
    """One parallel work line of a dispatch."""

    id: str
    name: str
    task: str
    assigned_model: str | None = None
    branch: str = ""
    attempts: int = 0
    status: LineStatus = LineStatus.PENDING
    current_run: str | None = None
    failure_reason: str | None = None
    blockers: list[str] = field(default_factory=list)
    log_tail: str = ""
    assignment_audit: str | None = None
    #: Ids of the lines this line waits on (the planner's DAG edges).
    depends_on: tuple[str, ...] = ()
    #: Path globs this line may write; the planner rejects overlapping scopes.
    write_scope: tuple[str, ...] = ()
    #: Paths this line owns while active, used to arbitrate across dispatches.
    declared_paths: tuple[str, ...] = ()
    #: Acceptance items; ``cmd: <argv>`` entries are run without a shell.
    acceptance: tuple[str, ...] = ()
    #: Set by the planner when the line needs a secondary review.
    risk_bearing: bool = False
    #: Planner's task-size hint: ``small``/``medium``/``large``, or ``""`` when
    #: the planner did not estimate. A ``large`` line is ranked by remaining
    #: quota so heavy work lands on the provider with the most headroom.
    estimated_size: str = ""
    base_commit: str = ""
    #: Planner metadata: ids this line must be integrated with or not at all.
    #: R-SPLIT refuses to remove one member of an atomic group.
    atomic_with: tuple[str, ...] = ()
    #: N80 L4: the Planner (or the owner) lets an interactive app claim this line
    #: instead of a Worker. Only ``CONDUCTOR_COLLAB_MODE=full`` acts on it.
    app_claimable: bool = False

    def __post_init__(self) -> None:
        if not self.declared_paths and self.write_scope:
            self.declared_paths = tuple(self.write_scope)

    def to_dict(self) -> dict[str, Any]:
        raw = {
            "id": self.id,
            "name": self.name,
            "task": self.task,
            "assigned_model": self.assigned_model,
            "branch": self.branch,
            "attempts": self.attempts,
            "status": self.status.value,
            "current_run": self.current_run,
            "failure_reason": self.failure_reason,
            "blockers": list(self.blockers),
            "log_tail": self.log_tail,
            "assignment_audit": self.assignment_audit,
            "depends_on": list(self.depends_on),
            "write_scope": list(self.write_scope),
            "acceptance": list(self.acceptance),
            "risk_bearing": self.risk_bearing,
        }
        # Optional fields are written only when set, which keeps records stored
        # before they existed round-tripping unchanged.
        if self.declared_paths:
            raw["declared_paths"] = list(self.declared_paths)
        if self.estimated_size:
            raw["estimated_size"] = self.estimated_size
        if self.base_commit:
            raw["base_commit"] = self.base_commit
        if self.atomic_with:
            raw["atomic_with"] = list(self.atomic_with)
        if self.app_claimable:
            raw["app_claimable"] = True
        return raw

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Line:
        status = data.get("status") or LineStatus.PENDING.value
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or data["id"]),
            task=str(data.get("task") or ""),
            assigned_model=data.get("assigned_model"),
            branch=str(data.get("branch") or ""),
            base_commit=str(data.get("base_commit") or ""),
            attempts=int(data.get("attempts") or 0),
            status=LineStatus(status) if not isinstance(status, LineStatus) else status,
            current_run=data.get("current_run"),
            failure_reason=data.get("failure_reason"),
            blockers=list(data.get("blockers") or []),
            log_tail=str(data.get("log_tail") or ""),
            assignment_audit=data.get("assignment_audit"),
            depends_on=_str_tuple(data.get("depends_on")),
            write_scope=_str_tuple(data.get("write_scope")),
            declared_paths=_str_tuple(data.get("declared_paths")),
            acceptance=_str_tuple(data.get("acceptance")),
            risk_bearing=bool(data.get("risk_bearing")),
            estimated_size=str(data.get("estimated_size") or ""),
            atomic_with=_str_tuple(data.get("atomic_with")),
            app_claimable=data.get("app_claimable") is True,
        )


@dataclass
class Outcome:
    """What one line cost and whether it passed review."""

    line_id: str
    model: str
    duration_secs: int
    tokens: int
    passed: bool
    failure_reason: str | None = None
    reviewer_verdict: str = ""
    #: R6 L4: the line's file-area category and the registry model version
    #: ("" = not recorded / following latest). Omitted from the dict when
    #: empty, so rows written before R6 keep their exact shape.
    category: str = ""
    model_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "line_id": self.line_id,
            "model": self.model,
            "duration_secs": self.duration_secs,
            "tokens": self.tokens,
            "passed": self.passed,
            "failure_reason": self.failure_reason,
            "reviewer_verdict": self.reviewer_verdict,
        }
        if self.category:
            data["category"] = self.category
        if self.model_version:
            data["model_version"] = self.model_version
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Outcome:
        return cls(
            line_id=str(data["line_id"]),
            model=str(data.get("model") or ""),
            duration_secs=int(data.get("duration_secs") or 0),
            tokens=int(data.get("tokens") or 0),
            passed=bool(data.get("passed")),
            failure_reason=data.get("failure_reason"),
            reviewer_verdict=str(data.get("reviewer_verdict") or ""),
            category=str(data.get("category") or ""),
            model_version=str(data.get("model_version") or ""),
        )


class SplitOffOutcome(dict[str, dict[str, Any]]):
    """Lines removed from a round by R-SPLIT (N76 §4.4).

    The record lives under ``dispatch.outcomes['split_off']`` because that is
    the column the liveness report already reads. It is a plain mapping of
    line id to follow-up details that also carries ``to_dict``/``from_dict``
    so the dispatch serializer and the report read one shape.
    """

    def __init__(
        self, lines: dict[str, dict[str, Any]] | None = None, **details: Any
    ) -> None:
        super().__init__()
        for line_id, entry in (lines or {}).items():
            if isinstance(entry, dict):
                self[str(line_id)] = dict(entry)
        if details:
            self.update(details)

    def add(self, line_id: str, **details: Any) -> None:
        entry = dict(details)
        entry.setdefault("line_id", line_id)
        self[str(line_id)] = entry

    def to_dict(self) -> dict[str, Any]:
        return {line_id: dict(details) for line_id, details in self.items()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SplitOffOutcome":
        raw = data.get("lines") if isinstance(data.get("lines"), dict) else data
        return cls(
            {
                str(line_id): dict(details)
                for line_id, details in raw.items()
                if isinstance(details, dict)
            }
        )


@dataclass
class Dispatch:
    """A whole auto-dispatch run: one goal, N parallel lines, one gate."""

    id: str
    goal: str
    user_id: str
    created_at: str
    #: The registered dispatch workspace this run resolved, and the checkout
    #: fields copied onto the row at start. Recovery reads the copy, not the
    #: registry, so deleting or editing a workspace cannot move a live run.
    workspace_id: str = ""
    repo_root: str = ""
    sandbox_policy: str = ""
    default_worker: str = ""
    lines: list[Line] = field(default_factory=list)
    integrate_branch: str = ""
    state: DispatchState = DispatchState.SUBMITTED
    approval_token: str | None = None
    #: Per-line cost/result, plus ``split_off`` (a :class:`SplitOffOutcome`)
    #: when R-SPLIT removed lines from this round.
    outcomes: dict[str, Outcome | SplitOffOutcome] = field(default_factory=dict)
    message: str | None = None
    #: Stage-Gate ``to_dict()`` snapshots, keyed by line id.
    gates: dict[str, Any] = field(default_factory=dict)
    #: Reference to the archived SPEC; ``""`` when there is none.
    spec_ref: str = ""

    @property
    def branch_prefix(self) -> str:
        return f"feature/auto-dispatch-{self.id}"

    def line(self, line_id: str) -> Line | None:
        for line in self.lines:
            if line.id == line_id:
                return line
        return None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "goal": self.goal,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "lines": [line.to_dict() for line in self.lines],
            "integrate_branch": self.integrate_branch,
            "state": self.state.value,
            "approval_token": self.approval_token,
            "outcomes": {key: value.to_dict() for key, value in self.outcomes.items()},
            "message": self.message,
            "gates": dict(self.gates),
            "spec_ref": self.spec_ref,
        }
        # The workspace copy is written only when a run selected one, so a
        # dispatch on the default checkout serializes exactly as before.
        for key in ("workspace_id", "repo_root", "sandbox_policy", "default_worker"):
            value = getattr(self, key)
            if value:
                payload[key] = value
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Dispatch:
        state = data.get("state") or DispatchState.SUBMITTED.value
        return cls(
            id=str(data["id"]),
            goal=str(data.get("goal") or ""),
            user_id=str(data.get("user_id") or ""),
            created_at=str(data.get("created_at") or ""),
            workspace_id=str(data.get("workspace_id") or ""),
            repo_root=str(data.get("repo_root") or ""),
            sandbox_policy=str(data.get("sandbox_policy") or ""),
            default_worker=str(data.get("default_worker") or ""),
            lines=[Line.from_dict(item) for item in data.get("lines") or []],
            integrate_branch=str(data.get("integrate_branch") or ""),
            state=DispatchState(state) if not isinstance(state, DispatchState) else state,
            approval_token=data.get("approval_token"),
            outcomes={
                str(key): (
                    SplitOffOutcome.from_dict(value)
                    if str(key) == "split_off"
                    and isinstance(value, dict)
                    and "model" not in value
                    else Outcome.from_dict(value)
                )
                for key, value in (data.get("outcomes") or {}).items()
            },
            message=data.get("message"),
            gates=dict(data.get("gates") or {}),
            spec_ref=str(data.get("spec_ref") or ""),
        )


@dataclass
class RunStatus:
    """What a Monitor saw the last time it looked at a run.

    ``stalled`` and ``timed_out`` both cost the line its attempt and send it
    back to ASSIGNED; only ``complete`` moves it to REVIEW.
    """

    output: str
    complete: bool = False
    stalled: bool = False
    timed_out: bool = False
    tokens: int = 0


@dataclass
class Verdict:
    """A cross-review decision on one line."""

    passed: bool
    blockers: list[str] = field(default_factory=list)
    verdict: str = ""

    @classmethod
    def ok(cls, verdict: str = "pass") -> Verdict:
        return cls(passed=True, verdict=verdict)

    @classmethod
    def blocked(cls, blockers: list[str], verdict: str = "blocked") -> Verdict:
        return cls(passed=False, blockers=list(blockers), verdict=verdict)
