"""Stage-Gate state machine for the subagent orchestration lifecycle.

The lifecycle lives in ``docs/SUBAGENT-ORCHESTRATION.md``::

    Mission -> Plan -> G0 SPEC accepted -> D1 Task DAG -> Assign
    -> Implement + handoff -> G2 focused tests -> Primary review
    -> Fixes / reassign -> Secondary review -> G5 full suite
    -> G6 human merge

This module is the *pure* half of that policy: :func:`advance` is a total
function over ``(state, event)`` that returns a :class:`Transition`. It never
reads a clock, never touches the filesystem or the network, and never raises on
an illegal transition -- it answers with a stable reason code instead, so a
scheduler can log the refusal and keep going.

Deliberate boundaries for v1:

* no import of :mod:`roles` / :mod:`role_plan`; roles arrive as plain provider
  strings inside the :class:`Event` the caller builds;
* no Ticket, Worker, scheduler, effect or database access -- the caller owns
  persistence and passes ``timestamp`` in;
* :class:`GateEvent` is the audit shape: a fixed field whitelist with no
  prompt, token or diff content. ``GateResult.reason`` / ``.evidence`` stay
  with the caller, which is what v2 writes next to the event.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, ClassVar, Mapping

__all__ = [
    "Stage",
    "Gate",
    "EventKind",
    "Verdict",
    "GateResult",
    "Handoff",
    "Event",
    "GateEvent",
    "StageGateState",
    "Transition",
    "Rule",
    "RULES",
    "REASON_CODES",
    "EVENT_FIELDS",
    "REPLAN_OPTIONS",
    "ADJUDICATION_OPTIONS",
    "PASS",
    "FAIL",
    "initial_state",
    "advance",
    "transition_table",
    "render_transition_table",
    "render_reason_table",
]


class Stage(str, Enum):
    """One node of the lifecycle. ``gate`` labels the gate that owns it."""

    PLANNING = "planning"
    SPEC_REVIEW = "spec_review"
    DAG_READY = "dag_ready"
    ASSIGNED = "assigned"
    IMPLEMENTING = "implementing"
    FOCUSED_TESTS = "focused_tests"
    PRIMARY_REVIEW = "primary_review"
    FIXING = "fixing"
    SECONDARY_REVIEW = "secondary_review"
    INTEGRATION_CHECKS = "integration_checks"
    AWAITING_HUMAN = "awaiting_human"
    AWAITING_ADJUDICATION = "awaiting_adjudication"
    MERGED = "merged"
    REPLANNING = "replanning"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"

    @property
    def gate(self) -> str:
        return _STAGE_GATES[self]

    @property
    def terminal(self) -> bool:
        return self in _TERMINAL_STAGES


_STAGE_GATES: Mapping[Stage, str] = {
    Stage.PLANNING: "",
    Stage.SPEC_REVIEW: "G0",
    Stage.DAG_READY: "D1",
    Stage.ASSIGNED: "",
    Stage.IMPLEMENTING: "",
    Stage.FOCUSED_TESTS: "G2",
    Stage.PRIMARY_REVIEW: "",
    Stage.FIXING: "",
    Stage.SECONDARY_REVIEW: "",
    Stage.INTEGRATION_CHECKS: "G5",
    Stage.AWAITING_HUMAN: "G6",
    Stage.AWAITING_ADJUDICATION: "",
    Stage.MERGED: "",
    Stage.REPLANNING: "",
    Stage.BLOCKED: "",
    Stage.CANCELLED: "",
}

_TERMINAL_STAGES = frozenset({Stage.MERGED, Stage.CANCELLED})


class Gate(str, Enum):
    """Gate labels that a ``gate_result`` event may reference."""

    G0 = "G0"
    D1 = "D1"
    G2 = "G2"
    G5 = "G5"
    G6 = "G6"


class EventKind(str, Enum):
    SPEC_SUBMITTED = "spec_submitted"
    GATE_RESULT = "gate_result"
    ASSIGN = "assign"
    REASSIGN = "reassign"
    START_IMPLEMENTATION = "start_implementation"
    SUBMIT_WORK = "submit_work"
    PRIMARY_REVIEW = "primary_review"
    SECONDARY_REVIEW = "secondary_review"
    ADJUDICATE_CONFLICT = "adjudicate_conflict"
    REJECT_DAG = "reject_dag"
    REPLAN_DECISION = "replan_decision"
    BLOCK = "block"
    RESUME = "resume"
    CANCEL = "cancel"


class Verdict(str, Enum):
    """Review verdict. ``MERGE`` never substitutes a secondary review."""

    MERGE = "merge"
    APPROVE = "approve"
    BLOCK = "block"


#: Outcome vocabulary of :class:`GateResult`.
PASS = "pass"
FAIL = "fail"

#: The two ways out of ``replanning``; the caller picks one.
REPLAN_OPTIONS: tuple[str, ...] = ("resplit_task", "replace_developer")

#: What a human can decide about a reviewer conflict. Neither option waives the
#: dual review or G5: an upheld BLOCK goes back to fixing, an overturned one
#: only counts that review as passed.
ADJUDICATION_OPTIONS: tuple[str, ...] = ("maintain_block", "overturn_block")

#: Fixed audit whitelist of :meth:`GateEvent.to_dict`.
EVENT_FIELDS: tuple[str, ...] = (
    "task_id",
    "from_stage",
    "to_stage",
    "gate",
    "result",
    "reason",
    "provider",
    "timestamp",
    "handoff_ref",
)


@dataclass(frozen=True, slots=True)
class GateResult:
    """What a gate decided. Carries no model output, only a verdict."""

    outcome: str
    reason: str = ""
    evidence: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.outcome == PASS

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class Handoff:
    """Everything a replacement developer needs, without the author's chat.

    All six fields are mandatory; an explicitly empty field means the handoff
    is incomplete and the machine refuses the reassignment. Use
    ``known_limitations="none"`` to state that there are none.
    """

    branch: str
    commit: str
    files: tuple[str, ...] = ()
    test_command: str = ""
    results: str = ""
    known_limitations: str = ""
    ref: str = ""

    REQUIRED: ClassVar[tuple[str, ...]] = (
        "branch",
        "commit",
        "files",
        "test_command",
        "results",
        "known_limitations",
    )

    def missing_fields(self) -> tuple[str, ...]:
        """Required fields that are absent, empty or whitespace-only."""
        missing: list[str] = []
        for name in self.REQUIRED:
            value = getattr(self, name)
            if isinstance(value, tuple):
                if not any(str(item).strip() for item in value):
                    missing.append(name)
            elif not str(value).strip():
                missing.append(name)
        return tuple(missing)

    @property
    def complete(self) -> bool:
        return not self.missing_fields()

    def reference(self) -> str:
        """Stable, content-free handle for the audit event."""
        if self.ref:
            return self.ref
        payload = json.dumps(
            {
                "branch": self.branch,
                "commit": self.commit,
                "files": list(self.files),
                "test_command": self.test_command,
                "results": self.results,
                "known_limitations": self.known_limitations,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return "handoff:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> dict[str, object]:
        return {
            "branch": self.branch,
            "commit": self.commit,
            "files": list(self.files),
            "test_command": self.test_command,
            "results": self.results,
            "known_limitations": self.known_limitations,
            "ref": self.ref,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Handoff:
        return cls(
            branch=str(data.get("branch", "")),
            commit=str(data.get("commit", "")),
            files=tuple(str(item) for item in data.get("files", ()) or ()),
            test_command=str(data.get("test_command", "")),
            results=str(data.get("results", "")),
            known_limitations=str(data.get("known_limitations", "")),
            ref=str(data.get("ref", "")),
        )


@dataclass(frozen=True, slots=True)
class Event:
    """What the caller observed. The machine reacts; it never observes itself."""

    kind: str
    timestamp: str = ""
    gate: str = ""
    result: GateResult | None = None
    provider: str = ""
    verdict: str = ""
    conflict: str = ""
    handoff: Handoff | None = None
    option: str = ""
    reason: str = ""
    handoff_ref: str = ""


@dataclass(frozen=True, slots=True)
class GateEvent:
    """One recorded transition. ``to_dict`` is the audit whitelist."""

    task_id: str
    from_stage: str
    to_stage: str
    gate: str
    result: str
    reason: str
    provider: str
    timestamp: str
    handoff_ref: str

    def to_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in EVENT_FIELDS}


@dataclass(frozen=True, slots=True)
class StageGateState:
    """The whole machine state. Serializable for v2 Ticket/database writes."""

    task_id: str
    stage: Stage = Stage.PLANNING
    risk_bearing: bool = False
    author_provider: str = ""
    developer_provider: str = ""
    primary_reviewer: str = ""
    secondary_reviewer: str = ""
    primary_verdict: str = ""
    primary_passed: bool = False
    secondary_passed: bool = False
    secondary_skipped: bool = False
    fix_rounds: int = 0
    blocking_reviewer: str = ""
    blocking_rounds: int = 0
    return_stage: str = ""
    handoff: Handoff | None = None
    handoff_ref: str = ""
    blocked_from: str = ""
    replan_options: tuple[str, ...] = ()
    #: every provider that actually implemented and submitted work on this
    #: task, in submission order. Never pruned: a replaced developer stays
    #: barred from reviewing their own code.
    contributors: tuple[str, ...] = ()
    #: the reviewer whose BLOCK a human is adjudicating, if any
    conflict_reviewer: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "stage": self.stage.value,
            "risk_bearing": self.risk_bearing,
            "author_provider": self.author_provider,
            "developer_provider": self.developer_provider,
            "primary_reviewer": self.primary_reviewer,
            "secondary_reviewer": self.secondary_reviewer,
            "primary_verdict": self.primary_verdict,
            "primary_passed": self.primary_passed,
            "secondary_passed": self.secondary_passed,
            "secondary_skipped": self.secondary_skipped,
            "fix_rounds": self.fix_rounds,
            "blocking_reviewer": self.blocking_reviewer,
            "blocking_rounds": self.blocking_rounds,
            "return_stage": self.return_stage,
            "handoff": self.handoff.to_dict() if self.handoff is not None else None,
            "handoff_ref": self.handoff_ref,
            "blocked_from": self.blocked_from,
            "replan_options": list(self.replan_options),
            "contributors": list(self.contributors),
            "conflict_reviewer": self.conflict_reviewer,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> StageGateState:
        stage = Stage(str(data["stage"]))
        handoff_data = data.get("handoff")
        handoff = Handoff.from_dict(handoff_data) if isinstance(handoff_data, Mapping) else None
        return cls(
            task_id=str(data["task_id"]),
            stage=stage,
            risk_bearing=bool(data.get("risk_bearing", False)),
            author_provider=str(data.get("author_provider", "")),
            developer_provider=str(data.get("developer_provider", "")),
            primary_reviewer=str(data.get("primary_reviewer", "")),
            secondary_reviewer=str(data.get("secondary_reviewer", "")),
            primary_verdict=str(data.get("primary_verdict", "")),
            primary_passed=bool(data.get("primary_passed", False)),
            secondary_passed=bool(data.get("secondary_passed", False)),
            secondary_skipped=bool(data.get("secondary_skipped", False)),
            fix_rounds=int(data.get("fix_rounds", 0) or 0),
            blocking_reviewer=str(data.get("blocking_reviewer", "")),
            blocking_rounds=int(data.get("blocking_rounds", 0) or 0),
            return_stage=str(data.get("return_stage", "")),
            handoff=handoff,
            handoff_ref=str(data.get("handoff_ref", "")),
            blocked_from=str(data.get("blocked_from", "")),
            replan_options=tuple(str(item) for item in data.get("replan_options", ()) or ()),
            contributors=tuple(str(item) for item in data.get("contributors", ()) or ()),
            conflict_reviewer=str(data.get("conflict_reviewer", "")),
        )


@dataclass(frozen=True, slots=True)
class Transition:
    """The answer to one :func:`advance` call."""

    accepted: bool
    state: StageGateState
    reason: str = ""
    detail: str = ""
    event: GateEvent | None = None
    options: tuple[str, ...] = ()

    @property
    def stage(self) -> Stage:
        return self.state.stage


def initial_state(task_id: str, *, risk_bearing: bool = False) -> StageGateState:
    """A fresh task at ``planning``."""
    return StageGateState(task_id=task_id, risk_bearing=risk_bearing)


# --------------------------------------------------------------------------
# transition table (single source of truth for legality + documentation)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rule:
    """One legal ``(stage, event kind)`` pair and what it may produce."""

    stage: Stage
    kind: EventKind
    outcomes: tuple[tuple[str, Stage, str], ...] = ()
    guard: str = ""
    note: str = ""
    dynamic: bool = False

    @property
    def key(self) -> tuple[Stage, EventKind]:
        return (self.stage, self.kind)


_G0_PASS = ("gate passes", Stage.DAG_READY, "spec_accepted")
_G0_FAIL = ("gate fails", Stage.REPLANNING, "spec_rejected")
_G2_PASS = ("gate passes", Stage.PRIMARY_REVIEW, "focused_tests_passed")
_G2_FAIL = ("gate fails", Stage.FIXING, "focused_tests_failed")
_G5_PASS = ("gate passes", Stage.AWAITING_HUMAN, "integration_checks_passed")
_G5_FAIL = ("gate fails", Stage.BLOCKED, "integration_checks_failed")
_G6_PASS = ("human approves", Stage.MERGED, "human_approved_merge")
_G6_FAIL = ("human rejects", Stage.REPLANNING, "human_rejected")

_STAGE_RULES: tuple[Rule, ...] = (
    Rule(
        Stage.PLANNING,
        EventKind.SPEC_SUBMITTED,
        outcomes=(("spec handed to review", Stage.SPEC_REVIEW, "spec_submitted"),),
        note="planner publishes the mission plan",
    ),
    Rule(
        Stage.SPEC_REVIEW,
        EventKind.GATE_RESULT,
        outcomes=(_G0_PASS, _G0_FAIL),
        note="G0; a rejected spec goes back to the planner",
    ),
    Rule(
        Stage.DAG_READY,
        EventKind.ASSIGN,
        outcomes=(("developer named", Stage.ASSIGNED, "assigned"),),
        note="D1 DAG plus the deterministic assignment",
    ),
    Rule(
        Stage.DAG_READY,
        EventKind.REJECT_DAG,
        outcomes=(("DAG invalid", Stage.REPLANNING, "dag_invalid"),),
        note="D1 is a milestone, not a pass/fail gate: a cyclic DAG, a missing dependency or a write-scope conflict is a planning failure",
    ),
    Rule(
        Stage.ASSIGNED,
        EventKind.START_IMPLEMENTATION,
        outcomes=(("work begins", Stage.IMPLEMENTING, "implementation_started"),),
        note="the developer starts on the assigned task",
    ),
    Rule(
        Stage.ASSIGNED,
        EventKind.REASSIGN,
        outcomes=(("checkpoint swap", Stage.ASSIGNED, "developer_reassigned"),),
        note="checkpoint: before entering review",
    ),
    Rule(
        Stage.IMPLEMENTING,
        EventKind.SUBMIT_WORK,
        outcomes=(("work + handoff published", Stage.FOCUSED_TESTS, "work_submitted"),),
        note="implement + handoff, then G2 runs",
    ),
    Rule(
        Stage.IMPLEMENTING,
        EventKind.REASSIGN,
        guard="reassignment_not_at_checkpoint",
        note="mid-implementation swaps are forbidden",
    ),
    Rule(
        Stage.FOCUSED_TESTS,
        EventKind.GATE_RESULT,
        outcomes=(_G2_PASS, _G2_FAIL),
        note="G2 focused tests",
    ),
    Rule(
        Stage.PRIMARY_REVIEW,
        EventKind.PRIMARY_REVIEW,
        outcomes=(
            ("approve", Stage.SECONDARY_REVIEW, "primary_review_passed"),
            ("merge, not risk-bearing", Stage.INTEGRATION_CHECKS, "primary_merge_no_risk"),
            ("block, initial (no failed fix round yet)", Stage.FIXING, "review_blocked"),
            ("block, 1st failed fix round", Stage.FIXING, "review_blocked"),
            ("block, 2nd failed fix round", Stage.REPLANNING, "two_failed_rounds"),
        ),
        note="Codex primary review; author may not review own code",
    ),
    Rule(
        Stage.FIXING,
        EventKind.SUBMIT_WORK,
        outcomes=(
            ("fix published, tests rerun", Stage.FOCUSED_TESTS, "fix_resubmitted"),
            ("fix published, primary re-reviews", Stage.PRIMARY_REVIEW, "fix_resubmitted"),
            ("fix published, secondary re-reviews", Stage.SECONDARY_REVIEW, "fix_resubmitted"),
        ),
        note="target is state.return_stage, the stage that blocked",
    ),
    Rule(
        Stage.FIXING,
        EventKind.REASSIGN,
        outcomes=(("checkpoint swap after BLOCK", Stage.ASSIGNED, "developer_reassigned"),),
        note="checkpoint: after a BLOCK",
    ),
    Rule(
        Stage.SECONDARY_REVIEW,
        EventKind.SECONDARY_REVIEW,
        outcomes=(
            ("approve", Stage.INTEGRATION_CHECKS, "secondary_review_passed"),
            ("block, initial (no failed fix round yet)", Stage.FIXING, "review_blocked"),
            ("block, 1st failed fix round", Stage.FIXING, "review_blocked"),
            ("block, 2nd failed fix round", Stage.REPLANNING, "two_failed_rounds"),
            ("scope conflict", Stage.AWAITING_ADJUDICATION, "reviewer_conflict_scope"),
            ("security conflict", Stage.AWAITING_ADJUDICATION, "reviewer_conflict_security"),
            ("other conflict", Stage.REPLANNING, "reviewer_conflict_planner"),
        ),
        note="another developer-pool model; author, contributors and primary may not take it",
    ),
    Rule(
        Stage.AWAITING_ADJUDICATION,
        EventKind.ADJUDICATE_CONFLICT,
        outcomes=(
            ("maintain_block, initial", Stage.FIXING, "review_blocked"),
            ("maintain_block, 1st failed fix round", Stage.FIXING, "review_blocked"),
            ("maintain_block, 2nd failed fix round", Stage.REPLANNING, "two_failed_rounds"),
            ("overturn_block, secondary still owed", Stage.SECONDARY_REVIEW, "conflict_overturned"),
            ("overturn_block, both reviews done", Stage.INTEGRATION_CHECKS, "conflict_overturned"),
        ),
        note="a human decides the reviewer conflict; neither option waives the dual review or G5",
    ),
    Rule(
        Stage.INTEGRATION_CHECKS,
        EventKind.GATE_RESULT,
        outcomes=(_G5_PASS, _G5_FAIL),
        note="G5 full suite + single Alembic head",
    ),
    Rule(
        Stage.AWAITING_HUMAN,
        EventKind.GATE_RESULT,
        outcomes=(_G6_PASS, _G6_FAIL),
        note="G6 human merge approval; a risk-bearing change still needs both reviews to have passed",
    ),
    Rule(
        Stage.REPLANNING,
        EventKind.REPLAN_DECISION,
        outcomes=(
            ("resplit_task", Stage.PLANNING, "replanned_split"),
            ("replace_developer", Stage.ASSIGNED, "developer_replaced"),
        ),
        note="caller picks one of state.replan_options",
    ),
    Rule(
        Stage.BLOCKED,
        EventKind.RESUME,
        outcomes=(("origin recorded", Stage.PLANNING, "resumed"),),
        dynamic=True,
        note="target is state.blocked_from; `planning` when nothing was recorded",
    ),
    Rule(
        Stage.BLOCKED,
        EventKind.REASSIGN,
        outcomes=(("paused outside implementation, or after the worker exited", Stage.ASSIGNED, "developer_reassigned"),),
        note="checkpoint: after a BLOCK; a pause from implementing needs option=worker_exited, else reassignment_not_at_checkpoint",
    ),
    Rule(
        Stage.BLOCKED,
        EventKind.BLOCK,
        guard="already_blocked",
        note="the task is already paused",
    ),
)

_GLOBAL_KINDS: tuple[EventKind, ...] = (EventKind.BLOCK, EventKind.CANCEL)
_GLOBAL_OUTCOMES: Mapping[EventKind, tuple[tuple[str, Stage, str], ...]] = {
    EventKind.BLOCK: (("operator or planner pause", Stage.BLOCKED, "operator_blocked"),),
    EventKind.CANCEL: (("task abandoned", Stage.CANCELLED, "cancelled"),),
}


def _build_rules() -> tuple[Rule, ...]:
    rules: list[Rule] = list(_STAGE_RULES)
    declared = {rule.key for rule in rules}
    for stage in Stage:
        if stage.terminal:
            continue
        for kind in _GLOBAL_KINDS:
            if (stage, kind) in declared:
                continue
            rules.append(
                Rule(stage, kind, outcomes=_GLOBAL_OUTCOMES[kind], note="available in every non-terminal stage")
            )
    return tuple(rules)


RULES: tuple[Rule, ...] = _build_rules()
_RULES_BY_KEY: Mapping[tuple[Stage, EventKind], Rule] = {rule.key: rule for rule in RULES}


def _kinds_for(stage: Stage) -> tuple[str, ...]:
    return tuple(rule.kind.value for rule in RULES if rule.stage is stage)


def transition_table() -> tuple[Rule, ...]:
    """The rules, expanded with the globally available ``block``/``cancel``."""
    return RULES


def render_transition_table() -> str:
    """Markdown rows for the transition table in ``docs/ports/stage-gate.md``."""
    lines = [
        "| from | event | condition | to | reason | note |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for rule in _STAGE_RULES:
        prefix = f"| `{rule.stage.value}` | `{rule.kind.value}` | "
        if rule.guard:
            lines.append(prefix + f"always refused | — | `{rule.guard}` | {rule.note} |")
            continue
        for condition, target, reason in rule.outcomes:
            shown = "`state.blocked_from`" if rule.dynamic else f"`{target.value}`"
            lines.append(prefix + f"{condition} | {shown} | `{reason}` | {rule.note} |")
    for kind in _GLOBAL_KINDS:
        for condition, target, reason in _GLOBAL_OUTCOMES[kind]:
            note = "every non-terminal stage"
            lines.append(f"| any non-terminal | `{kind.value}` | {condition} | `{target.value}` | `{reason}` | {note} |")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# advance(): pure, total, exception-free
# --------------------------------------------------------------------------

#: Failed fix rounds under one reviewer before the Planner takes over. The
#: initial BLOCK is not a round, so two failed rounds means three BLOCKs.
_MAX_BLOCKING_ROUNDS = 2
_RESUMABLE_REVIEW_STAGES = frozenset(
    {Stage.FOCUSED_TESTS, Stage.PRIMARY_REVIEW, Stage.SECONDARY_REVIEW}
)

Handler = Callable[[StageGateState, Event], Transition]


def _reject(
    state: StageGateState,
    reason: str,
    detail: str = "",
    options: tuple[str, ...] = (),
) -> Transition:
    """A refusal. The state is returned untouched and no event is produced."""
    return Transition(
        accepted=False, state=state, reason=reason, detail=detail, options=options
    )


def _accept(
    previous: StageGateState,
    new: StageGateState,
    event: Event,
    reason: str,
    *,
    result: str = "",
    provider: str = "",
    handoff: Handoff | None = None,
    options: tuple[str, ...] = (),
) -> Transition:
    target = new.stage
    if handoff is None and event.handoff is not None:
        handoff = event.handoff
    handoff_ref = event.handoff_ref or (
        handoff.reference() if handoff is not None else previous.handoff_ref
    )
    record = GateEvent(
        task_id=previous.task_id,
        from_stage=previous.stage.value,
        to_stage=target.value,
        # the gate this transition *concerns*: the one named on the event, else
        # the one the source stage owns, else the one the target opens
        gate=event.gate or previous.stage.gate or target.gate,
        result=result,
        reason=reason,
        provider=provider,
        timestamp=event.timestamp,
        handoff_ref=handoff_ref,
    )
    return Transition(
        accepted=True, state=new, reason=reason, event=record, options=options
    )


# -- app identity (N80 H4): the one normaliser every check uses -------------
#
# An app-claimed dispatch line records its author as ``app:<app_id>``. An app
# may act through any of its agents, so ``app:<id>/<agent>``, ``app:<id>:<agent>``
# and ``app:<id> <agent>`` (any case, space after the colon allowed) are all the
# same identity, ``app:<id>`` lowercase. A name without the ``app:`` prefix is a
# native Worker or reviewer (``codex`` the CLI), never an app.

_APP_PREFIX = "app:"
_APP_ID_END = frozenset("/:")


def app_identity(name: object) -> str:
    """``app:<id>`` for a name that carries the ``app:`` prefix, else ``""``."""
    text = str(name or "").strip()
    if text[: len(_APP_PREFIX)].lower() != _APP_PREFIX:
        return ""
    rest = text[len(_APP_PREFIX) :].strip()
    end = next(
        (i for i, ch in enumerate(rest) if ch in _APP_ID_END or ch.isspace()), len(rest)
    )
    ident = rest[:end].lower()
    return f"{_APP_PREFIX}{ident}" if ident else ""


def canonical_app(app_id: object) -> str:
    """The identity of a registered app, given its bare id or an ``app:`` name."""
    text = str(app_id or "").strip()
    named = app_identity(text)
    if named:
        return named
    return app_identity(_APP_PREFIX + text)


def reviewer_identity(name: object) -> str:
    """What a reviewer or author counts as: the app identity, or the stripped name."""
    return app_identity(name) or str(name or "").strip()


def _barred_reviewers(state: StageGateState) -> frozenset[str]:
    """Nobody who touched this implementation may review it.

    ``contributors`` is the persistent record of everyone who actually
    implemented and submitted work; the original author and the developer of
    record are barred even if their submission is still pending. The set holds
    the raw names and their :func:`reviewer_identity`, so an app is barred
    whichever of its agents asks to review.
    """
    names = set(state.contributors)
    names.update(name for name in (state.author_provider, state.developer_provider) if name)
    names.update(reviewer_identity(name) for name in tuple(names))
    return frozenset(names)


def _is_barred_reviewer(state: StageGateState, provider: str) -> bool:
    barred = _barred_reviewers(state)
    return provider in barred or reviewer_identity(provider) in barred


def _gate_check(
    state: StageGateState, event: Event, expected: Gate
) -> tuple[str, Transition | None]:
    gate = (event.gate or expected.value).strip()
    if gate != expected.value:
        return "", _reject(
            state,
            "gate_mismatch",
            f"stage {state.stage.value} owns gate {expected.value}, event named {gate!r}",
        )
    if event.result is None:
        return gate, _reject(
            state,
            "result_required",
            f"a gate_result event for {expected.value} needs a GateResult",
        )
    if event.result.outcome not in (PASS, FAIL):
        return gate, _reject(
            state,
            "invalid_result",
            f"GateResult.outcome must be {PASS!r} or {FAIL!r}",
        )
    return gate, None


def _review_verdict(
    state: StageGateState, event: Event
) -> tuple[str, Transition | None]:
    if event.result is None:
        return "", _reject(
            state, "result_required", "a review event needs a GateResult"
        )
    if event.result.outcome not in (PASS, FAIL):
        return "", _reject(
            state, "invalid_result", f"GateResult.outcome must be {PASS!r} or {FAIL!r}"
        )
    verdict = (event.verdict or "").strip().lower()
    if not verdict:
        verdict = Verdict.APPROVE.value if event.result.passed else Verdict.BLOCK.value
    if verdict not in {item.value for item in Verdict}:
        return "", _reject(state, "invalid_verdict", f"unknown verdict {event.verdict!r}")
    if event.result.passed and verdict == Verdict.BLOCK.value:
        return "", _reject(
            state, "verdict_result_mismatch", "a passing review cannot carry BLOCK"
        )
    if not event.result.passed and verdict != Verdict.BLOCK.value:
        return "", _reject(
            state, "verdict_result_mismatch", "a failing review carries the BLOCK verdict"
        )
    return verdict, None


def _clear_review(state: StageGateState) -> StageGateState:
    return replace(
        state,
        primary_reviewer="",
        primary_verdict="",
        primary_passed=False,
        secondary_passed=False,
        secondary_skipped=False,
        blocking_reviewer="",
        blocking_rounds=0,
        return_stage="",
        replan_options=(),
    )


def _clear_blocking(state: StageGateState) -> StageGateState:
    """Nobody is blocking and no fix round has failed: a fresh review cycle.

    ``fix_rounds`` travels with ``blocking_rounds``: both describe the round
    sequence the current reviewer is judging, so both restart together.
    """
    return replace(state, blocking_reviewer="", blocking_rounds=0, fix_rounds=0)


def _reset_cycle(state: StageGateState, stage: Stage) -> StageGateState:
    return replace(
        _clear_review(_clear_blocking(state)), stage=stage, fix_rounds=0, blocked_from=""
    )


def _blocked_step(
    state: StageGateState, event: Event, reviewer: str, origin: Stage
) -> Transition:
    """A reviewer BLOCK.

    The first BLOCK is not a fix round. A round exists once the author has fixed
    and resubmitted, so the BLOCK that follows a resubmission is the first
    *failed* round; a second consecutive failed round under the same reviewer
    sends the task back to the Planner.

    The count survives a G2 pass and a checkpoint swap of the developer: only a
    changed reviewer or a passing verdict restarts it. A changed reviewer also
    restarts ``fix_rounds``, so both counters describe the same round sequence.
    """
    same_reviewer = state.blocking_reviewer == reviewer
    rounds = state.blocking_rounds + 1 if same_reviewer else 0
    fix_rounds = state.fix_rounds if same_reviewer else 0
    if rounds >= _MAX_BLOCKING_ROUNDS:
        new = replace(
            state,
            stage=Stage.REPLANNING,
            blocking_reviewer=reviewer,
            blocking_rounds=rounds,
            fix_rounds=fix_rounds,
            replan_options=REPLAN_OPTIONS,
        )
        return _accept(
            state,
            new,
            event,
            "two_failed_rounds",
            result=FAIL,
            provider=reviewer,
            options=REPLAN_OPTIONS,
        )
    new = replace(
        state,
        stage=Stage.FIXING,
        blocking_reviewer=reviewer,
        blocking_rounds=rounds,
        fix_rounds=fix_rounds,
        return_stage=origin.value,
    )
    return _accept(
        state, new, event, "review_blocked", result=FAIL, provider=reviewer
    )


def _checkpoint_swap(
    state: StageGateState,
    event: Event,
    target: Stage,
    reason: str,
    *,
    fresh_cycle: bool = False,
) -> Transition:
    provider = (event.provider or "").strip()
    if not provider:
        return _reject(
            state, "provider_required", "a reassignment names the new developer"
        )
    if provider == state.developer_provider:
        return _reject(
            state,
            "reassignment_same_provider",
            f"{provider!r} already holds this task",
        )
    if event.handoff is None:
        return _reject(
            state,
            "handoff_required",
            "a checkpoint swap carries the outgoing developer's handoff",
        )
    missing = event.handoff.missing_fields()
    if missing:
        return _reject(
            state,
            "handoff_incomplete",
            "handoff fields missing: " + ", ".join(missing),
        )
    new = replace(
        state,
        stage=target,
        developer_provider=provider,
        handoff=event.handoff,
        handoff_ref=event.handoff.reference(),
    )
    if fresh_cycle:
        new = replace(_clear_review(_clear_blocking(new)), fix_rounds=0, blocked_from="")
    return _accept(
        state, new, event, reason, provider=provider, handoff=event.handoff
    )


def _record_contributor(state: StageGateState) -> StageGateState:
    """Remember who actually implemented and submitted this work."""
    developer = state.developer_provider
    if not developer or developer in state.contributors:
        return state
    return replace(state, contributors=state.contributors + (developer,))


def _publish_work(
    state: StageGateState,
    event: Event,
    target: Stage,
    reason: str,
) -> Transition:
    """Publish work (or a fix) for the next stage.

    A submission counts as a fix round when something is still blocking: the
    task sits in ``fixing`` after a BLOCK or a failed gate, or a reviewer is
    still blocking after a checkpoint swap moved the work back through
    ``implementing``. Deriving it here rather than from the caller keeps the two
    routes in sync, so ``blocking_rounds`` can never outrun ``fix_rounds``.

    Every submission also records its developer in ``contributors``, which is
    what keeps a replaced developer barred from reviewing their own code.
    """
    count_fix = state.stage is Stage.FIXING or bool(state.blocking_reviewer)
    fix_rounds = state.fix_rounds + 1 if count_fix else state.fix_rounds
    if event.handoff is not None:
        missing = event.handoff.missing_fields()
        if missing:
            return _reject(
                state,
                "handoff_incomplete",
                "handoff fields missing: " + ", ".join(missing),
            )
        new = replace(
            _record_contributor(state),
            stage=target,
            fix_rounds=fix_rounds,
            handoff=event.handoff,
            handoff_ref=event.handoff.reference(),
        )
    else:
        new = replace(
            _record_contributor(state), stage=target, fix_rounds=fix_rounds
        )
    return _accept(
        state,
        new,
        event,
        reason,
        provider=state.developer_provider,
        handoff=event.handoff,
    )


def _h_planning_spec_submitted(state: StageGateState, event: Event) -> Transition:
    return _accept(
        state, replace(state, stage=Stage.SPEC_REVIEW), event, "spec_submitted"
    )


def _h_spec_review_gate_result(state: StageGateState, event: Event) -> Transition:
    _, refusal = _gate_check(state, event, Gate.G0)
    if refusal is not None:
        return refusal
    if event.result.passed:  # narrowed by _gate_check
        new = replace(state, stage=Stage.DAG_READY)
        return _accept(state, new, event, "spec_accepted", result=PASS)
    new = replace(state, stage=Stage.REPLANNING, replan_options=("resplit_task",))
    return _accept(
        state,
        new,
        event,
        "spec_rejected",
        result=FAIL,
        options=("resplit_task",),
    )


def _h_dag_ready_reject_dag(state: StageGateState, event: Event) -> Transition:
    """D1 is a milestone, not a pass/fail gate: an invalid DAG is a plan failure."""
    new = replace(_clear_review(_clear_blocking(state)), stage=Stage.REPLANNING,
                  replan_options=REPLAN_OPTIONS)
    return _accept(
        state, new, event, "dag_invalid", provider=event.provider,
        options=REPLAN_OPTIONS,
    )


def _h_dag_ready_assign(state: StageGateState, event: Event) -> Transition:
    provider = (event.provider or "").strip()
    if not provider:
        return _reject(
            state, "provider_required", "an assignment names the developer provider"
        )
    new = replace(
        state,
        stage=Stage.ASSIGNED,
        developer_provider=provider,
        author_provider=state.author_provider or provider,
    )
    return _accept(state, new, event, "assigned", provider=provider)


def _h_assigned_start(state: StageGateState, event: Event) -> Transition:
    return _accept(
        state, replace(state, stage=Stage.IMPLEMENTING), event, "implementation_started"
    )


def _h_assigned_reassign(state: StageGateState, event: Event) -> Transition:
    return _checkpoint_swap(state, event, Stage.ASSIGNED, "developer_reassigned")


def _h_implementing_submit_work(state: StageGateState, event: Event) -> Transition:
    return _publish_work(state, event, Stage.FOCUSED_TESTS, "work_submitted")


def _h_implementing_reassign(state: StageGateState, event: Event) -> Transition:
    return _reject(
        state,
        "reassignment_not_at_checkpoint",
        "implementation is running; a swap needs a checkpoint (before review or after a BLOCK)",
    )


def _h_focused_tests_gate_result(state: StageGateState, event: Event) -> Transition:
    _, refusal = _gate_check(state, event, Gate.G2)
    if refusal is not None:
        return refusal
    if event.result.passed:
        # A G2 pass is not a reset point: the reviewer's round count survives
        # the tests, because the same reviewer still has to be convinced.
        new = replace(
            state,
            stage=Stage.PRIMARY_REVIEW,
            handoff_ref=event.handoff_ref or state.handoff_ref,
        )
        return _accept(state, new, event, "focused_tests_passed", result=PASS)
    new = replace(
        state,
        stage=Stage.FIXING,
        return_stage=Stage.FOCUSED_TESTS.value,
    )
    return _accept(state, new, event, "focused_tests_failed", result=FAIL)


def _h_primary_review(state: StageGateState, event: Event) -> Transition:
    provider = (event.provider or "").strip()
    if not provider:
        return _reject(
            state, "provider_required", "a review names the reviewer provider"
        )
    if _is_barred_reviewer(state, provider):
        return _reject(
            state,
            "self_review_forbidden",
            f"{provider!r} wrote this code and cannot primary-review it",
        )
    verdict, refusal = _review_verdict(state, event)
    if refusal is not None:
        return refusal
    if verdict == Verdict.BLOCK:
        return _blocked_step(state, event, provider, Stage.PRIMARY_REVIEW)
    if verdict == Verdict.MERGE and state.risk_bearing:
        return _reject(
            state,
            "secondary_review_required",
            "a risk-bearing change needs the secondary review; a primary MERGE does not replace it",
        )
    if verdict == Verdict.MERGE:
        new = replace(
            _clear_blocking(state),
            stage=Stage.INTEGRATION_CHECKS,
            primary_reviewer=provider,
            primary_verdict=Verdict.MERGE.value,
            primary_passed=True,
            secondary_skipped=True,
        )
        return _accept(
            state, new, event, "primary_merge_no_risk", result=PASS, provider=provider
        )
    new = replace(
        _clear_blocking(state),
        stage=Stage.SECONDARY_REVIEW,
        primary_reviewer=provider,
        primary_verdict=Verdict.APPROVE.value,
        primary_passed=True,
    )
    return _accept(
        state, new, event, "primary_review_passed", result=PASS, provider=provider
    )


def _h_fixing_submit_work(state: StageGateState, event: Event) -> Transition:
    target = Stage.FOCUSED_TESTS
    if state.return_stage:
        try:
            candidate = Stage(state.return_stage)
        except ValueError:
            candidate = Stage.FOCUSED_TESTS
        if candidate in _RESUMABLE_REVIEW_STAGES:
            target = candidate
    return _publish_work(state, event, target, "fix_resubmitted")


def _h_fixing_reassign(state: StageGateState, event: Event) -> Transition:
    return _checkpoint_swap(state, event, Stage.ASSIGNED, "developer_reassigned")


def _h_secondary_review(state: StageGateState, event: Event) -> Transition:
    provider = (event.provider or "").strip()
    if not provider:
        return _reject(
            state, "provider_required", "a review names the reviewer provider"
        )
    if _is_barred_reviewer(state, provider):
        return _reject(
            state,
            "self_review_forbidden",
            f"{provider!r} wrote this code and cannot take the secondary review",
        )
    if state.primary_reviewer and provider == state.primary_reviewer:
        return _reject(
            state,
            "reviewer_is_primary",
            "the secondary review belongs to another developer-pool model",
        )
    verdict, refusal = _review_verdict(state, event)
    if refusal is not None:
        return refusal
    if verdict == Verdict.BLOCK:
        conflict = (event.conflict or "").strip().lower()
        if conflict in ("security", "scope"):
            # A security or scope disagreement is not a fix round and not a
            # merge approval: a human adjudicates it in its own state, and
            # neither outcome waives the dual review or G5.
            new = replace(
                state,
                stage=Stage.AWAITING_ADJUDICATION,
                secondary_reviewer=provider,
                conflict_reviewer=provider,
            )
            return _accept(
                state,
                new,
                event,
                f"reviewer_conflict_{conflict}",
                result=FAIL,
                provider=provider,
            )
        if conflict:
            new = replace(
                state,
                stage=Stage.REPLANNING,
                secondary_reviewer=provider,
                replan_options=REPLAN_OPTIONS,
            )
            return _accept(
                state,
                new,
                event,
                "reviewer_conflict_planner",
                result=FAIL,
                provider=provider,
                options=REPLAN_OPTIONS,
            )
        return _blocked_step(state, event, provider, Stage.SECONDARY_REVIEW)
    new = replace(
        _clear_blocking(state),
        stage=Stage.INTEGRATION_CHECKS,
        secondary_reviewer=provider,
        secondary_passed=True,
    )
    return _accept(
        state, new, event, "secondary_review_passed", result=PASS, provider=provider
    )


def _h_awaiting_adjudication(state: StageGateState, event: Event) -> Transition:
    """A human decides a reviewer conflict.

    Upholding the BLOCK sends the task back to ``fixing`` and that reviewer's
    round count keeps running. Overturning it counts *this review* as passed and
    then runs whatever is still required before G5: a risk-bearing change whose
    secondary review has not passed goes back to the secondary review, anything
    else goes to the integration checks. Neither path reaches ``merged`` -- G6
    is only ever entered from a passing G5.
    """
    option = (event.option or "").strip().lower()
    reviewer = state.conflict_reviewer
    if option not in ADJUDICATION_OPTIONS:
        return _reject(
            state,
            "adjudication_option_unknown",
            f"unknown adjudication {event.option!r}; expected one of "
            + ", ".join(ADJUDICATION_OPTIONS),
        )
    if not reviewer:
        return _reject(
            state,
            "provider_required",
            "an adjudication names the reviewer whose BLOCK is decided",
        )

    if option == "maintain_block":
        # The BLOCK stands, so it is an ordinary BLOCK: the same reason codes
        # and the same round counting as a reviewer BLOCK, which keeps the
        # "two failed rounds" rule uniform for conflicts and plain blocks.
        return _blocked_step(state, event, reviewer, Stage.SECONDARY_REVIEW)

    passed = _clear_blocking(state)
    if state.primary_passed:
        # v1 raises conflicts only at the secondary review, so the overturned
        # review is that one and it counts as passed. An earlier review that
        # never passed can never be waved through by an adjudication.
        passed = replace(passed, secondary_passed=True, secondary_reviewer=reviewer)
    if state.risk_bearing and not passed.secondary_passed:
        new = replace(passed, stage=Stage.SECONDARY_REVIEW, conflict_reviewer="")
        return _accept(
            state, new, event, "conflict_overturned", result=PASS, provider=reviewer
        )
    new = replace(passed, stage=Stage.INTEGRATION_CHECKS, conflict_reviewer="")
    return _accept(
        state, new, event, "conflict_overturned", result=PASS, provider=reviewer
    )


def _h_integration_checks(state: StageGateState, event: Event) -> Transition:
    _, refusal = _gate_check(state, event, Gate.G5)
    if refusal is not None:
        return refusal
    if state.risk_bearing and not (state.primary_passed and state.secondary_passed):
        return _reject(
            state,
            "secondary_review_required",
            "a risk-bearing change needs both reviews to have passed before G5",
        )
    if event.result.passed:
        new = replace(state, stage=Stage.AWAITING_HUMAN)
        return _accept(state, new, event, "integration_checks_passed", result=PASS)
    new = replace(state, stage=Stage.BLOCKED, blocked_from=Stage.INTEGRATION_CHECKS.value)
    return _accept(state, new, event, "integration_checks_failed", result=FAIL)


def _h_awaiting_human(state: StageGateState, event: Event) -> Transition:
    _, refusal = _gate_check(state, event, Gate.G6)
    if refusal is not None:
        return refusal
    # Defence in depth: a state restored from v2 storage still has to carry a
    # passing secondary review for a risk-bearing change.
    if state.risk_bearing and not (state.primary_passed and state.secondary_passed):
        return _reject(
            state,
            "secondary_review_required",
            "a risk-bearing change needs both reviews to have passed before G6",
        )
    if event.result.passed:
        new = replace(state, stage=Stage.MERGED)
        return _accept(state, new, event, "human_approved_merge", result=PASS)
    new = replace(state, stage=Stage.REPLANNING, replan_options=("resplit_task",))
    return _accept(
        state, new, event, "human_rejected", result=FAIL, options=("resplit_task",)
    )


def _h_replanning(state: StageGateState, event: Event) -> Transition:
    option = (event.option or "").strip().lower()
    # ``replan_options`` is what this particular replan offered, so it is the
    # authority: a G0 rejection only offered a re-split, and a hand-built state
    # that offered nothing accepts nothing.
    offered = state.replan_options or ()
    if option not in offered:
        return _reject(
            state,
            "replan_option_unknown",
            f"replan option {event.option!r} was not offered; offered: "
            + (", ".join(offered) or "none"),
        )
    if option == "resplit_task":
        new = _reset_cycle(state, Stage.PLANNING)
        return _accept(state, new, event, "replanned_split", provider=event.provider)
    return _checkpoint_swap(
        state, event, Stage.ASSIGNED, "developer_replaced", fresh_cycle=True
    )


def _h_blocked_resume(state: StageGateState, event: Event) -> Transition:
    target = Stage.PLANNING
    if state.blocked_from:
        try:
            candidate = Stage(state.blocked_from)
        except ValueError:
            candidate = Stage.PLANNING
        if not candidate.terminal and candidate is not Stage.BLOCKED:
            target = candidate
    new = replace(state, stage=target, blocked_from="")
    return _accept(state, new, event, "resumed", provider=event.provider)


def _h_blocked_reassign(state: StageGateState, event: Event) -> Transition:
    # A pause taken mid-implementation is not a checkpoint: the swap rule for
    # ``implementing`` still holds. Otherwise the new developer starts a fresh
    # cycle at ``assigned``, with no review rounds or block origin carried over.
    # The caller passes ``option="worker_exited"`` only when the worker of a
    # pause from ``implementing`` has already exited: nothing runs, so it is
    # a checkpoint after all.
    if state.blocked_from == Stage.IMPLEMENTING.value and event.option != "worker_exited":
        return _reject(
            state,
            "reassignment_not_at_checkpoint",
            "the task was paused mid-implementation; a swap needs a checkpoint",
        )
    return _checkpoint_swap(
        state, event, Stage.ASSIGNED, "developer_reassigned", fresh_cycle=True
    )


def _h_block(state: StageGateState, event: Event) -> Transition:
    new = replace(state, stage=Stage.BLOCKED, blocked_from=state.stage.value)
    return _accept(state, new, event, "operator_blocked", provider=event.provider)


def _h_cancel(state: StageGateState, event: Event) -> Transition:
    new = replace(state, stage=Stage.CANCELLED, blocked_from="")
    return _accept(state, new, event, "cancelled", provider=event.provider)


def _h_already_blocked(state: StageGateState, event: Event) -> Transition:
    return _reject(state, "already_blocked", "the task is already blocked")


_HANDLERS: Mapping[tuple[Stage, EventKind], Handler] = {
    **{
        (stage, EventKind.BLOCK): _h_block
        for stage in Stage
        if not stage.terminal
    },
    **{
        (stage, EventKind.CANCEL): _h_cancel
        for stage in Stage
        if not stage.terminal
    },
    (Stage.PLANNING, EventKind.SPEC_SUBMITTED): _h_planning_spec_submitted,
    (Stage.SPEC_REVIEW, EventKind.GATE_RESULT): _h_spec_review_gate_result,
    (Stage.DAG_READY, EventKind.ASSIGN): _h_dag_ready_assign,
    (Stage.DAG_READY, EventKind.REJECT_DAG): _h_dag_ready_reject_dag,
    (Stage.ASSIGNED, EventKind.START_IMPLEMENTATION): _h_assigned_start,
    (Stage.ASSIGNED, EventKind.REASSIGN): _h_assigned_reassign,
    (Stage.IMPLEMENTING, EventKind.SUBMIT_WORK): _h_implementing_submit_work,
    (Stage.IMPLEMENTING, EventKind.REASSIGN): _h_implementing_reassign,
    (Stage.FOCUSED_TESTS, EventKind.GATE_RESULT): _h_focused_tests_gate_result,
    (Stage.PRIMARY_REVIEW, EventKind.PRIMARY_REVIEW): _h_primary_review,
    (Stage.FIXING, EventKind.SUBMIT_WORK): _h_fixing_submit_work,
    (Stage.FIXING, EventKind.REASSIGN): _h_fixing_reassign,
    (Stage.SECONDARY_REVIEW, EventKind.SECONDARY_REVIEW): _h_secondary_review,
    (Stage.INTEGRATION_CHECKS, EventKind.GATE_RESULT): _h_integration_checks,
    (Stage.AWAITING_HUMAN, EventKind.GATE_RESULT): _h_awaiting_human,
    (Stage.AWAITING_ADJUDICATION, EventKind.ADJUDICATE_CONFLICT): _h_awaiting_adjudication,
    (Stage.REPLANNING, EventKind.REPLAN_DECISION): _h_replanning,
    (Stage.BLOCKED, EventKind.RESUME): _h_blocked_resume,
    (Stage.BLOCKED, EventKind.REASSIGN): _h_blocked_reassign,
    (Stage.BLOCKED, EventKind.BLOCK): _h_already_blocked,
}


def _coerce_kind(kind: object) -> EventKind | None:
    try:
        return EventKind(str(kind))
    except ValueError:
        return None


def advance(state: StageGateState, event: Event) -> Transition:
    """Apply ``event`` to ``state``. Pure: no clock, no IO, no exceptions.

    An unknown event kind, a terminal stage, a missing timestamp, an illegal
    ``(stage, event)`` pair and every semantic violation come back as a
    :class:`Transition` with ``accepted=False``, an unchanged state and a stable
    ``reason`` code. Only an accepted transition produces a :class:`GateEvent`.
    """
    kind = _coerce_kind(event.kind)
    if kind is None:
        return _reject(state, "unknown_event_kind", f"unknown event kind {event.kind!r}")
    if state.stage.terminal:
        return _reject(
            state,
            "already_terminal",
            f"stage {state.stage.value} accepts no further events",
        )
    if not (event.timestamp or "").strip():
        return _reject(
            state,
            "timestamp_missing",
            "the caller owns the clock and must supply event.timestamp",
        )
    rule = _RULES_BY_KEY.get((state.stage, kind))
    if rule is None:
        return _reject(
            state,
            "stage_gate_order",
            f"event {kind.value} is not legal in stage {state.stage.value}; legal: "
            + (", ".join(_kinds_for(state.stage)) or "none"),
        )
    handler = _HANDLERS.get((state.stage, kind))
    if handler is None:  # pragma: no cover - guarded by the rule/handler parity test
        return _reject(
            state,
            "stage_gate_order",
            f"no handler for {kind.value} in stage {state.stage.value}",
        )
    return handler(state, event)


# --------------------------------------------------------------------------
# reason codes
# --------------------------------------------------------------------------

REASON_CODES: tuple[tuple[str, str], ...] = (
    ("spec_submitted", "plan handed to the G0 gate"),
    ("spec_accepted", "G0 passed"),
    ("spec_rejected", "G0 failed; the planner owns the next move"),
    ("assigned", "developer named for the D1 DAG"),
    ("developer_reassigned", "checkpoint swap, before review or after a BLOCK"),
    ("implementation_started", "developer began work"),
    ("work_submitted", "work plus handoff published for G2"),
    ("focused_tests_passed", "G2 passed"),
    ("focused_tests_failed", "G2 failed; the developer fixes"),
    ("primary_review_passed", "primary review approved"),
    ("primary_merge_no_risk", "primary MERGE on a change that is not risk-bearing"),
    ("review_blocked", "reviewer BLOCK; the initial BLOCK is not a failed fix round"),
    ("two_failed_rounds", "second consecutive failed fix round under the same reviewer"),
    ("fix_resubmitted", "fix published back to the blocking stage"),
    ("secondary_review_passed", "secondary review approved"),
    ("integration_checks_passed", "G5 passed"),
    ("integration_checks_failed", "G5 failed; the task is blocked for an operator"),
    ("human_approved_merge", "G6 approved"),
    ("human_rejected", "G6 rejected; the planner owns the next move"),
    ("reviewer_conflict_scope", "scope disagreement between reviewers; a human adjudicates it"),
    ("reviewer_conflict_security", "security disagreement between reviewers; a human adjudicates it"),
    ("reviewer_conflict_planner", "ordinary disagreement; the planner decides against the SPEC"),
    ("conflict_overturned", "the human overturned the BLOCK; that review counts as passed and the next required gate runs"),
    ("adjudication_option_unknown", "refusal: adjudication is neither maintain_block nor overturn_block"),
    ("dag_invalid", "the D1 DAG is invalid (cycle, missing dependency, write-scope conflict); a planning failure"),
    ("replanned_split", "planner re-decomposed the task"),
    ("developer_replaced", "planner swapped the developer after a replan"),
    ("resumed", "left `blocked` for the recorded origin"),
    ("operator_blocked", "operator or planner paused the task"),
    ("cancelled", "task abandoned"),
    ("unknown_event_kind", "refusal: event kind outside the vocabulary"),
    ("already_terminal", "refusal: stage is `merged` or `cancelled`"),
    ("timestamp_missing", "refusal: caller supplied no timestamp"),
    ("stage_gate_order", "refusal: event kind not legal in the current stage"),
    ("gate_mismatch", "refusal: gate_result named a gate the stage does not own"),
    ("result_required", "refusal: gate or review event without a GateResult"),
    ("invalid_result", "refusal: GateResult.outcome is neither pass nor fail"),
    ("invalid_verdict", "refusal: review verdict is not merge/approve/block"),
    ("verdict_result_mismatch", "refusal: verdict and outcome disagree"),
    ("provider_required", "refusal: the event names no provider"),
    ("self_review_forbidden", "refusal: author or developer cannot review this work"),
    ("reviewer_is_primary", "refusal: the secondary review needs another model"),
    ("secondary_review_required", "refusal: risk-bearing change still needs the secondary review"),
    ("reassignment_not_at_checkpoint", "refusal: no swap while implementation runs"),
    ("reassignment_same_provider", "refusal: the named developer already holds the task"),
    ("handoff_required", "refusal: a checkpoint swap needs a handoff"),
    ("handoff_incomplete", "refusal: handoff fields are missing"),
    ("replan_option_unknown", "refusal: replan option is not resplit_task/replace_developer"),
    ("already_blocked", "refusal: the task is already blocked"),
)


def render_reason_table() -> str:
    """Markdown rows for the reason-code table in ``docs/ports/stage-gate.md``."""
    lines = ["| reason | meaning |", "| --- | --- |"]
    for code, meaning in REASON_CODES:
        lines.append(f"| `{code}` | {meaning} |")
    return "\n".join(lines)
