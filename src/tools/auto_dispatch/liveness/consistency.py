"""The line-status x Stage-Gate consistency table (PRD n76 §4.1, C3).

A line and its Stage-Gate snapshot are written by different callers, so they
drift.  ``_block_line`` changes ``Line.status`` and ``failure_reason`` but does
not itself advance Stage-Gate; callers that append the gate's ``block`` event
afterwards are consistent, while a caller that forgets leaves the gate in the
review or implementation stage.  The liveness sweep needs a mechanical answer
for every pair: which Stage-Gate stages may a line rest in, and when a missed
pair can be repaired, which event repairs it.

``allowed_stages`` is that pure table lookup.  ``check`` compares one
``(line_status, failure_reason, stage)`` snapshot and answers with exactly one
of three outcomes:

* :class:`Ok` -- the pair is allowed (or the dispatch is ungated).
* :class:`Repair` -- the line is parked with a known reason but the gate stayed
  behind; appending the Stage-Gate ``block`` event is safe and repairs it.
* :class:`Inconsistent` -- there is no safe repair here: hand the line to a
  person under ``liveness_inconsistent``.

The reason keys are deliberately string literals.  ``test_consistency_scan.py``
walks every ``_block_line`` call in ``machine.py``, resolves the constant,
``or``/``if`` expressions and its short list of non-constant call-site
expressions, and fails if a reason has no row here or a new, unresolved
expression appears.  That is what stops the next patch from adding a parking
call site without updating this table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from src.server.services.autonomy.stage_gate import Stage

from ..types import LineStatus

#: Stages where a line is legitimately parked while a human still has an
#: adjudication verb to use.  The gate may already have recorded the pause, or
#: the machine may be about to append it.
_AWAITING_ADJUDICATION_STAGES = frozenset({Stage.AWAITING_ADJUDICATION, Stage.BLOCKED})

#: Stages that mean "planning has not produced line work yet".  A planning
#: park leaves the line blocked but must not look like a mid-implementation
#: drift.
_PLANNING_STAGES = frozenset(
    {Stage.BLOCKED, Stage.PLANNING, Stage.SPEC_REVIEW, Stage.DAG_READY}
)

#: Stages around the integration gate.  A G5 stop is legitimately still in
#: ``integration_checks`` when the gate's own failure event has not landed yet.
_INTEGRATION_STAGES = frozenset({Stage.BLOCKED, Stage.INTEGRATION_CHECKS})

#: ``failure_reason`` -> stages allowed after ``_block_line`` parked the line.
#:
#: Every key is a literal because the scan test must be able to compare the
#: reasons at the machine's call sites with this table without importing the
#: machine's constants.
REASON_STAGES: dict[str, frozenset[Stage]] = {
    # Worker exit, watchdog, G2 repair exhaustion and a failed fix launch.  The
    # checkpoint helper appends the block event; a snapshot still in an earlier
    # stage is drift the sweep either repairs or hands to a person.
    "no_new_commit": frozenset({Stage.BLOCKED}),
    "uncommitted_changes": frozenset({Stage.BLOCKED}),
    "watchdog_timeout": frozenset({Stage.BLOCKED}),
    "g2_no_commands_risk_bearing": frozenset({Stage.BLOCKED}),
    "g2_revision_moved": frozenset({Stage.BLOCKED}),
    "focused_tests_exhausted": frozenset({Stage.BLOCKED}),
    "fix_launch_failed": frozenset({Stage.BLOCKED}),
    # Review unavailable / unparseable / no reviewer.  The inline review-attempt
    # park never advanced the gate, which is exactly the C3 drift the sweep
    # repairs with a block event.
    "review_unavailable": frozenset({Stage.BLOCKED}),
    "review_unparseable": frozenset({Stage.BLOCKED}),
    "no_eligible_reviewer": frozenset({Stage.BLOCKED}),
    "reviewers_quota_exhausted": frozenset({Stage.BLOCKED}),
    "no_secondary_reviewer": frozenset({Stage.BLOCKED}),
    "no_eligible_worker": frozenset({Stage.BLOCKED}),
    # N76 review admission holds a new line in ASSIGNED (or DAG_READY, before
    # it is stored) until the review backlog drains.
    "review_backlog": frozenset({Stage.DAG_READY, Stage.ASSIGNED, Stage.BLOCKED}),
    # A fix could not start because its handoff is not complete.
    "handoff_incomplete": frozenset({Stage.BLOCKED}),
    # The one replan is spent, or its base cannot be named: Stage-Gate keeps
    # the line in ``replanning`` so the Planner's ruling remains legal.
    "replan_parked": frozenset({Stage.BLOCKED, Stage.REPLANNING}),
    "replan_uncombined": frozenset({Stage.BLOCKED, Stage.REPLANNING}),
    "replan_base_unresolved": frozenset({Stage.BLOCKED, Stage.REPLANNING}),
    "replan_exhausted": frozenset({Stage.BLOCKED, Stage.REPLANNING}),
    # ``operator.adjudicate`` keeps a block on the second failed fix round:
    # the line parks BLOCKED while Stage-Gate waits in ``replanning``.  The
    # write is in operator.py, not a ``_block_line`` call.
    "two_failed_rounds": frozenset({Stage.BLOCKED, Stage.REPLANNING}),
    # A human rules, so the gate legitimately stays awaiting adjudication.
    "reviewer_conflict_scope": _AWAITING_ADJUDICATION_STAGES,
    "reviewer_conflict_security": _AWAITING_ADJUDICATION_STAGES,
    # Planning parks have no line work yet.
    "planner_failed": _PLANNING_STAGES,
    "resume_spec_missing": _PLANNING_STAGES,
    # G5 stops park instead of ERROR (PRD §4.4).
    "g5_not_configured": _INTEGRATION_STAGES,
    "g5_full_suite_failed": _INTEGRATION_STAGES,
    "g5_timeout": _INTEGRATION_STAGES,
    "g5_worktree_dirty": _INTEGRATION_STAGES,
    "git_evidence_unavailable": frozenset({Stage.BLOCKED}),
    # A Stage-Gate refusal was turned into a park; it is consistent once the
    # block event landed.  ``_apply_review_result`` parks the raw refusal code,
    # and the gate is still in the review stage at that moment, so each of the
    # codes the primary/secondary review handlers can return gets a row rather
    # than falling through the unknown-reason path.
    "review_blocked": frozenset({Stage.BLOCKED}),
    "unknown_event_kind": frozenset({Stage.BLOCKED}),
    "already_terminal": frozenset({Stage.BLOCKED}),
    "timestamp_missing": frozenset({Stage.BLOCKED}),
    "stage_gate_order": frozenset({Stage.BLOCKED}),
    "provider_required": frozenset({Stage.BLOCKED}),
    "self_review_forbidden": frozenset({Stage.BLOCKED}),
    "result_required": frozenset({Stage.BLOCKED}),
    "invalid_result": frozenset({Stage.BLOCKED}),
    "invalid_verdict": frozenset({Stage.BLOCKED}),
    "verdict_result_mismatch": frozenset({Stage.BLOCKED}),
    "secondary_review_required": frozenset({Stage.BLOCKED}),
    "reviewer_is_primary": frozenset({Stage.BLOCKED}),
    # The sweep's own human marker.
    "liveness_inconsistent": frozenset({Stage.BLOCKED}),
    # N86: the worker a human reassign pinned cannot run; the line waits for
    # a person instead of moving to another worker (worker-exited checkpoint).
    "pinned_worker_unusable": frozenset({Stage.BLOCKED}),
    # N29: a worker asked for more context handoffs than the line allows; the
    # line waits for an operator relaunch (worker-exited checkpoint).
    "context_rotation_exhausted": frozenset({Stage.BLOCKED}),
    # N27 enforce: the G2 failure is also on the base commit.
    "preexisting_failure": frozenset({Stage.BLOCKED}),
    # N59 enforce: the reviewer asked for evidence; unblock reviews again.
    "review_inconclusive": frozenset({Stage.BLOCKED}),
    # Existing worker recovery reasons: they append a block event themselves.
    "author_quota_exhausted": frozenset({Stage.BLOCKED}),
    "worker_auth_failed": frozenset({Stage.BLOCKED}),
    "worker_quota_exhausted": frozenset({Stage.BLOCKED}),
    "worker_context_exhausted": frozenset({Stage.BLOCKED}),
    "worker_confirmation_required": frozenset({Stage.BLOCKED}),
    "worker_turn_budget_exhausted": frozenset({Stage.BLOCKED}),
    "worker_tool_blocked": frozenset({Stage.BLOCKED}),
    "worker_rate_limited": frozenset({Stage.BLOCKED}),
    "worker_overloaded": frozenset({Stage.BLOCKED}),
    "worker_timeout": frozenset({Stage.BLOCKED}),
    "worker_network": frozenset({Stage.BLOCKED}),
    "worker_crash": frozenset({Stage.BLOCKED}),
    "worker_unknown": frozenset({Stage.BLOCKED}),
    # P0: a silent Worker (no model output); a BLOCKED line with it is parked.
    "worker_no_output": frozenset({Stage.BLOCKED}),
    "network_retries_exhausted": frozenset({Stage.BLOCKED}),
    "rate_limited_retries_exhausted": frozenset({Stage.BLOCKED}),
    "overloaded_retries_exhausted": frozenset({Stage.BLOCKED}),
    "timeout_retries_exhausted": frozenset({Stage.BLOCKED}),
    "context_exhausted_retries_exhausted": frozenset({Stage.BLOCKED}),
    "turn_budget_exhausted_retries_exhausted": frozenset({Stage.BLOCKED}),
    # N80 L4: an app's lease on a line lapsed or the app released it.  The
    # machine records a worker_exited checkpoint, which takes the Stage-Gate
    # block transition, so the line is BLOCKED for R-EXIT.
    "app_lease_expired": frozenset({Stage.BLOCKED}),
    "app_lease_released": frozenset({Stage.BLOCKED}),
}

#: ``failure_reason -> stages allowed for a FAILED line``.  A FAILED line is
#: terminal for this round, so only an intentional terminal transition is
#: consistent here; an unreasoned failure still uses ``STATUS_STAGES``.
FAILED_REASON_STAGES: dict[str, frozenset[Stage]] = {
    # PRD §4.4: a split-off line is excluded from integration wherever it was.
    "split_off": frozenset(Stage),
    # N48 marks the original line FAILED/replanned and resets its Stage-Gate to
    # PLANNING for the replacement children.
    "replanned": frozenset({Stage.PLANNING}),
}

#: ``line_status -> stages allowed when the reason does not override them``.
STATUS_STAGES: dict[LineStatus, frozenset[Stage]] = {
    LineStatus.PENDING: frozenset({Stage.PLANNING, Stage.SPEC_REVIEW, Stage.DAG_READY}),
    LineStatus.ASSIGNED: frozenset(
        {
            Stage.DAG_READY,
            Stage.ASSIGNED,
            Stage.IMPLEMENTING,
            # A review/G2 fix deferred behind a sibling is still ASSIGNED with
            # ``serial_wait``, while Stage-Gate already moved to ``fixing``.
            Stage.FIXING,
            Stage.BLOCKED,
        }
    ),
    LineStatus.RUNNING: frozenset(
        {
            Stage.ASSIGNED,
            Stage.IMPLEMENTING,
            Stage.FOCUSED_TESTS,
            Stage.FIXING,
            # ``_review_gated`` reviews RUNNING lines in the review stages and
            # marks them passed from INTEGRATION_CHECKS.
            Stage.PRIMARY_REVIEW,
            Stage.SECONDARY_REVIEW,
            Stage.INTEGRATION_CHECKS,
        }
    ),
    LineStatus.REVIEWING: frozenset(
        {
            Stage.FOCUSED_TESTS,
            Stage.PRIMARY_REVIEW,
            Stage.SECONDARY_REVIEW,
            Stage.AWAITING_ADJUDICATION,
            Stage.FIXING,
        }
    ),
    LineStatus.PASSED: frozenset(
        {Stage.INTEGRATION_CHECKS, Stage.AWAITING_HUMAN, Stage.MERGED}
    ),
    LineStatus.FAILED: frozenset({Stage.BLOCKED, Stage.CANCELLED}),
}

#: Every reason a BLOCKED line may carry.
BLOCK_REASONS = frozenset(REASON_STAGES)
#: Every reason this module knows, including the FAILED-line reasons.
KNOWN_REASONS = BLOCK_REASONS | frozenset(FAILED_REASON_STAGES)


class _ConsistencyTable(dict[str, frozenset[Stage]]):
    """The reason table, also indexable by a ``(status, reason)`` pair.

    The old L1 package aliased ``CONSISTENCY_TABLE`` to ``REASON_STAGES``.  The
    tuple form makes the PRD's ``(line_status, failure_reason)`` lookup
    available without duplicating a row for every status.
    """

    def __getitem__(self, key: Any) -> frozenset[Stage]:
        if isinstance(key, tuple) and len(key) == 2:
            status, reason = key
            return allowed_stages(status, reason)
        return super().__getitem__(key)


#: Backwards-compatible name for the table itself; tuple keys also resolve
#: through :func:`allowed_stages`.
CONSISTENCY_TABLE = _ConsistencyTable(REASON_STAGES)

#: Alias for callers that read the status half as its own table.
LINE_STATUS_STAGES = STATUS_STAGES

#: Reasons where appending ``block`` would destroy the state a human verb
#: needs (adjudication), or the marker itself.  These go straight to a person,
#: except from a stale review stage: there the line was parked while the gate
#: never recorded the pause, and appending ``block`` is exactly the missed
#: event (the conflict reasons allow ``BLOCKED``), not a new recovery step.
_NO_REPAIR = frozenset(
    {"reviewer_conflict_scope", "reviewer_conflict_security", "liveness_inconsistent"}
)

#: The stages a review park can leave the gate in before its ``block`` event
#: lands.  Only these turn a no-repair reason into a mechanical repair; any
#: other drift for those reasons still needs a person.
_REVIEW_STAGES = frozenset({Stage.PRIMARY_REVIEW, Stage.SECONDARY_REVIEW})


def _status(value: LineStatus | str) -> LineStatus:
    """Coerce a stored status; an unknown spelling is treated as PENDING."""
    if isinstance(value, LineStatus):
        return value
    text = str(value or "").strip().upper()
    try:
        return LineStatus(text)
    except ValueError:
        return LineStatus.PENDING


def _stage(value: Stage | str | None) -> Stage | None:
    """Coerce a stored stage; ``None`` means there is no Stage-Gate snapshot."""
    if value is None or isinstance(value, Stage):
        return value
    try:
        return Stage(str(value).strip().lower())
    except ValueError:
        return None


def _reason(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def allowed_stages(
    line_status: LineStatus | str, failure_reason: str | None = None
) -> frozenset[Stage]:
    """Allowed Stage-Gate stages for one ``(status, reason)`` pair.

    A BLOCKED line is governed by its reason.  A FAILED line may have an
    intentional terminal reason (``split_off`` / ``replanned``); otherwise the
    status row applies.  An unknown BLOCKED reason falls back to ``BLOCKED``:
    the static scan catches new reasons before they reach production, and an
    old row already in ``blocked`` is not something a sweep can repair.
    """
    current = _status(line_status)
    key = _reason(failure_reason)
    if current is LineStatus.BLOCKED:
        return REASON_STAGES.get(key or "", frozenset({Stage.BLOCKED}))
    if current is LineStatus.FAILED and key in FAILED_REASON_STAGES:
        return FAILED_REASON_STAGES[key]
    return STATUS_STAGES.get(current, frozenset())


#: Older spelling used by the L1 package and its callers.
stages_for = allowed_stages


@dataclass(frozen=True)
class Consistency:
    """Base class for the three answers ``check`` can return.

    The properties keep the older boolean surface (``consistent``, ``repair``,
    ``needs_human``) available while the concrete class names make the outcome
    explicit at the call site and in a ``match`` statement.
    """

    status: LineStatus
    reason: str | None
    stage: Stage | None
    allowed: frozenset[Stage]
    applicable: bool = True
    detail: str = ""

    kind: ClassVar[str] = "consistency"

    @property
    def consistent(self) -> bool:
        return self.kind == "ok"

    @property
    def repair(self) -> str | None:
        return None

    @property
    def needs_human(self) -> bool:
        return self.kind == "inconsistent"

    def repair_event(self) -> dict[str, str] | None:
        """The Stage-Gate event that repairs this line, when there is one."""
        return {"kind": self.repair} if self.repair else None


@dataclass(frozen=True)
class Ok(Consistency):
    """The line and its Stage-Gate snapshot agree (or the dispatch is ungated)."""

    kind: ClassVar[str] = "ok"


@dataclass(frozen=True)
class Repair(Consistency):
    """The park is known, but the gate is behind; append its ``block`` event."""

    kind: ClassVar[str] = "repair"
    event: str = "block"

    @property
    def repair(self) -> str:
        return self.event


@dataclass(frozen=True)
class Inconsistent(Consistency):
    """No safe automatic repair exists; a person owns the line now."""

    kind: ClassVar[str] = "inconsistent"


def check(
    line_status: LineStatus | str,
    failure_reason: str | None = None,
    stage: Stage | str | None = None,
) -> Consistency:
    """Compare a line snapshot with its Stage-Gate stage.

    ``stage=None`` means the dispatch has no Stage-Gate at all (the ungated
    path), so the answer is :class:`Ok` with ``applicable=False``.  A stage
    that was supplied but cannot be parsed is corruption, not an ungated
    dispatch: it goes to a person as :class:`Inconsistent`.
    """
    current = _status(line_status)
    key = _reason(failure_reason)
    if stage is None:
        return Ok(
            current,
            key,
            None,
            frozenset(),
            applicable=False,
            detail="no Stage-Gate snapshot",
        )

    parsed = _stage(stage)
    if parsed is None:
        return Inconsistent(
            current,
            key,
            None,
            allowed_stages(current, key),
            detail=f"unparseable Stage-Gate stage {stage!r}",
        )

    allowed = allowed_stages(current, key)
    if current is LineStatus.BLOCKED and key is None:
        return Inconsistent(
            current,
            None,
            parsed,
            allowed,
            detail="a blocked line has no failure reason",
        )
    if parsed in allowed:
        return Ok(current, key, parsed, allowed)

    if current is LineStatus.BLOCKED:
        if key not in REASON_STAGES:
            return Inconsistent(
                current,
                key,
                parsed,
                allowed,
                detail=f"no consistency row for reason {key!r}",
            )
        if parsed.terminal:
            return Inconsistent(
                current,
                key,
                parsed,
                allowed,
                detail=f"no safe repair from stage {parsed.value}",
            )
        if key in _NO_REPAIR and parsed not in _REVIEW_STAGES:
            return Inconsistent(
                current,
                key,
                parsed,
                allowed,
                detail=f"no safe repair from stage {parsed.value}",
            )
        return Repair(
            current,
            key,
            parsed,
            allowed,
            detail=f"append block to leave {parsed.value}",
        )

    # A nonterminal line can only leave its status through the machine, so a
    # stage outside its allowed set has no safe repair here.  Hand it to a
    # person instead of returning a mismatch the sweep would ignore.
    return Inconsistent(
        current,
        key,
        parsed,
        allowed,
        detail=f"{current.value} line is in stage {parsed.value}",
    )


def check_snapshot(
    line: Any,
    snapshot: dict[str, Any] | None,
    *,
    stage_from: str = "machine",
) -> Consistency:
    """Convenience for a ``Line`` plus its gates snapshot.

    ``stage_from`` reads the Stage-Gate state under ``snapshot[stage_from]``
    (the machine's spelling) or the older ``snapshot['phase']``.
    """
    if not isinstance(snapshot, dict):
        return check(
            getattr(line, "status", LineStatus.PENDING),
            getattr(line, "failure_reason", None),
            None,
        )
    raw_stage: Any = None
    machine = snapshot.get(stage_from)
    if isinstance(machine, dict):
        raw_stage = machine.get("stage")
    if raw_stage is None:
        raw_stage = snapshot.get("phase")
    return check(
        getattr(line, "status", LineStatus.PENDING),
        getattr(line, "failure_reason", None),
        raw_stage,
    )


__all__ = [
    "BLOCK_REASONS",
    "CONSISTENCY_TABLE",
    "Consistency",
    "FAILED_REASON_STAGES",
    "Inconsistent",
    "KNOWN_REASONS",
    "LINE_STATUS_STAGES",
    "Ok",
    "REASON_STAGES",
    "Repair",
    "STATUS_STAGES",
    "allowed_stages",
    "check",
    "check_snapshot",
    "stages_for",
]
