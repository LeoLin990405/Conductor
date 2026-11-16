"""The dispatch state machine: one goal in, one gated integrate branch out.

The machine is a plain coroutine that parks rather than blocks. It returns as
soon as it needs a human — a blocked review, a pending approval — and picks up
from the persisted state when that human answers, so no turn is held open
waiting and no worker holds a lock while a person thinks.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import inspect
import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, fields as dataclass_fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable
from uuid import uuid4

from src.server.services.autonomy.stage_gate import (
    Event,
    GateResult,
    Handoff,
    Stage,
    StageGateState,
    Transition,
    advance,
)

from .gates import (
    FIX_LAUNCH_FAILED,
    FOCUSED_TESTS_EXHAUSTED,
    G2_NO_COMMANDS_RISK_BEARING,
    G2_REVISION_MOVED,
    HANDOFF_INCOMPLETE,
    NO_SECONDARY_REVIEWER,
    PLANNER_FAILED,
    REPLAN_EXHAUSTED,
    REPLANNED,
    RESUME_SPEC_MISSING,
    REVIEW_ATTEMPT_LIMIT,
    REVIEW_BLOCKED,
    REVIEW_MERGED,
    REVIEW_UNAVAILABLE,
    REVIEW_UNPARSEABLE,
    REVIEWER_CHANGED,
    g2_failure_note,
    git_changed_paths as gate_git_changed_paths,
    git_revision as gate_git_revision,
    with_g2_failure,
    bump_epoch,
    drive_epoch,
    record_stale_drive,
    record_stale_fix_intent,
    record_stale_result,
    REVIEWER_CONFLICT_SCOPE,
    REVIEWER_CONFLICT_SECURITY,
    SECONDARY_REQUIRED,
    TWO_FAILED_ROUNDS,
    GateRunner,
    topological_lines,
)
from .planner_reason import (
    is_planner_failed,
    parked_planner_reason,
    planner_failure_message,
    planner_failure_reason,
)
from .protocols import (
    ApprovalDecision,
    ApprovalGate,
    DispatchError,
    DispatchStore,
    LaunchError,
    Launcher,
    MergeConflictError,
    ModelRole,
    ModelSelector,
    Monitor,
    Merger,
    NoNewCommitError,
    OutcomeStore,
    Planner,
    Reviewer,
    UncommittedChangesError,
)
from .types import (
    Dispatch,
    DispatchState,
    Line,
    LineStatus,
    Outcome,
    RunStatus,
    SplitOffOutcome,
    Verdict,
)
from .execution import (
    SESSION_LOST_REASON_CODES,
    LaunchResult,
    WorkerLaunchFailure,
    decide_launch_admission,
    requires_cli_owned_session,
    supports_explicit_session_id,
    supports_session_resume,
    verdict_unparseable,
)
from .liveness import consistency as liveness_consistency
from .liveness import modes as liveness_modes
from . import debug_switch, dynamic_assign, event_tags, review_isolation
from src.server.services.autonomy.role_types import ROLE_WORKER, pinned_version
from .liveness import owner as liveness_owner
from .liveness import policy as liveness_policy
from .liveness import record as liveness_record
from .review_admission import QUEUE, REVIEW_BACKLOG_REASON
from . import app_lines, completion_evidence, handoff_pack, human_pin, review_triage
from . import text_normalize
from .preexisting import PREEXISTING_FAILURE
from .review_triage import REVIEW_INCONCLUSIVE
from . import p0_hooks, review_strict, silent_worker
from .review_failure_tail import review_failure_text
from .escalation import (
    BLOCKER_BRIEF,
    ESCALATION_THRESHOLD,
    build_escalation,
    escalation_brief,
    escalation_from_snapshot,
    record_blocked_round,
    render_escalation,
    write_escalation,
)
from .rulings import (
    FIX_RULING,
    RULING_CHECK,
    Ruling,
    RulingCheck,
    build_ruling,
    check_confirmation,
    only_unconfirmed,
    reissue_ruling,
    render_ruling,
    ruling_from_dict as _ruling_from_dict,
    unconfirmed_blockers,
)
from .context_budget import (
    HANDOFF_OK,
    delete_handoff_request,
    handoff_request_path,
    pending_handoff_request,
    read_handoff_request,
)
from .self_report import lesson_candidates, read_self_report
from .worker_recovery import Action, decide
from . import cost as dispatch_cost
from src.llms.native_cli.base import resumable_session_id
from src.llms.native_cli.failure import redact_credentials
from src.llms.native_cli.failure import FailureClass
from src.llms.native_cli.session_manager import (
    ACTIVE_ROTATION,
    ROTATION_EXHAUSTED_REASON,
    WORKER_REQUESTED,
)
from src.llms.native_cli.worker_report import WORKER_REPORT_TIMEOUT_SECONDS
from src.server.services.autonomy.git_delivery import run_isolated_git_readonly
from src.server.utils.sanitize import sanitize_output_text

logger = logging.getLogger(__name__)


class StaleDrive(Exception):
    """The stored epoch is not the one this drive started with."""


@dataclass(frozen=True)
class _ReservedWorkerFailure(Exception):
    failure: WorkerLaunchFailure
    attempt: int
    author: str
    epoch: int

    def __str__(self) -> str:
        return str(self.failure)


class DispatchLoopMismatch(RuntimeError):
    """The machine was called from an event loop other than the one it is bound to.

    The actor queue, mailbox lock and worker tasks belong to the first loop that
    drove the machine. The machine does not rebuild them on a new loop, because
    queued events would be lost and the single writer would be split in two.
    Run every step for one machine on one loop, for example one
    ``asyncio.Runner`` kept for the whole run.
    """


def _audit_tail_is_stale(dispatch: Dispatch) -> bool:
    """True when the latest scheduler audit entry is already ``stale_drive``."""
    gates = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    scheduler = gates.get("scheduler")
    if not isinstance(scheduler, dict):
        return False
    audit = scheduler.get("audit")
    if not isinstance(audit, list) or not audit:
        return False
    last = audit[-1]
    return isinstance(last, dict) and last.get("kind") == "stale_drive"


async def drive_mutation(
    store: Any,
    dispatch_id: str,
    epoch0: int,
    fn: Callable[[Dispatch], Dispatch],
) -> Dispatch | None:
    """Persist ``fn`` only when the stored epoch is still ``epoch0``.

    A bound ``store.drive_mutation`` (the service, or a merging store) owns
    the lock. A raw store saves through its own writer. The only ``save``
    call on this side of the drive lives in this function.
    """
    bound = getattr(store, "drive_mutation", None)
    if bound is not None and bound is not drive_mutation:
        return await bound(dispatch_id, epoch0, fn)
    row = await store.get(dispatch_id)
    if row is None:
        return None
    if drive_epoch(row) != epoch0:
        record_stale_drive(row)
        writer = getattr(store, "save", None)
        if writer is not None:
            await writer(row)
        raise StaleDrive
    updated = fn(row)
    if updated is None:
        updated = row
    writer = getattr(store, "save", None)
    if writer is None:
        return updated
    return await writer(updated)


# Every available reviewer once held the line (N38). The line parks, not ERROR.
NO_ELIGIBLE_REVIEWER = "no_eligible_reviewer"
# Run-head records kept per line for the no-new-commit check (N34).
_MAX_RUN_RECORDS = 32
# A line gets its first model plus this many reassignments before it is failed.
MAX_REASSIGNS = 2
# N76: a line held back by review-backlog admission is parked under this
# reason so it is not picked again until the backlog admits it.
REVIEW_BACKLOG = REVIEW_BACKLOG_REASON
# N76: the worker-side reason that used to raise DispatchError (ERROR).
NO_ELIGIBLE_WORKER = "no_eligible_worker"
# N76: worker-capacity parks retry the assignment, not a review. The R-QUOTA
# table's second row is ``review`` because that is the verb for reviewer
# capacity; for a line that never got an author the same row selects one.
_WORKER_ASSIGNMENT_REASONS = frozenset({NO_ELIGIBLE_WORKER})
# N76: reviewers a later R-REVIEW row must not ask again. The first row retries
# the stored reviewer; the alternate row keeps this list and excludes it.
_LIVENESS_EXCLUDED_REVIEWERS = "liveness_excluded_reviewers"
# Stages that mean a worker has not started. A reserved branch name is not one
# of them: ``_apply_plan_ready`` writes ``line.branch`` before anyone is chosen.
_PRE_LAUNCH_STAGES = frozenset(
    {
        "",
        Stage.PLANNING.value,
        Stage.SPEC_REVIEW.value,
        Stage.DAG_READY.value,
        Stage.ASSIGNED.value,
    }
)
# ``blocked`` is not a launch. A park that never recorded an earlier stage
# leaves that origin, and the branch name was reserved before anyone started.
_NOT_A_LAUNCH_STAGE = _PRE_LAUNCH_STAGES | {Stage.BLOCKED.value}
# N76: non-BLOCKED lines the sweep still checks for a missing owner. PENDING
# waits on its dependencies and ASSIGNED is the drive's ready pool.
_OPEN_LIVENESS_STATUSES = frozenset({LineStatus.RUNNING, LineStatus.REVIEWING})
# N76: the stages where a settled, run-less RUNNING line is a resumed review.
_LIVENESS_REVIEW_STAGES = frozenset({Stage.PRIMARY_REVIEW, Stage.SECONDARY_REVIEW})
# N76: a terminal line needs no owner and no recovery.
_TERMINAL_LINE_STATUSES = frozenset({LineStatus.PASSED, LineStatus.FAILED})
# N76: a line removed from this round by R-SPLIT.
SPLIT_OFF = "split_off"
# N76: how long the sweep waits before it observes the same shadow step again.
LIVENESS_SHADOW_DELAY_SECONDS = 60.0
#: How long a due R-SPLIT waits before looking again while no line has PASSED
#: yet but other lines are still in progress (they may still pass).
LIVENESS_SPLIT_WAIT_SECONDS = 300.0
# N76: a review-backlog queued line retries admission after this pause.
REVIEW_ADMISSION_WAIT_SECONDS = 30.0
# Model selection is retried once after this pause before the dispatch errors.
ASSIGN_RETRY_SECONDS = 30.0
# How long between log polls while lines run.
POLL_INTERVAL_SECONDS = 10.0
# A launch is retried on the same model this many times before reassigning.
LAUNCH_RETRIES = 2
# The planner's task cap. Duplicated so this module does not edit planning.py.
MAX_LINE_TASK_BYTES = 8_000
# A gated review whose reviewer cannot run is retried on the drive after
# each of these waits (1, 5, 15 minutes); one more failure parks the line.
REVIEW_RETRY_DELAYS = (60.0, 300.0, 900.0)
REPLAN_EXHAUSTED_MESSAGE = (
    "Line {line_id} was blocked twice after fixes and could not be replanned; "
    "reassign it or reject the dispatch"
)
# N48: this reason string lives here, not in gates.py, so this line does not
# share gates.py's reason block with the sibling lines that own it (L3).
REPLAN_PARKED = "replan_parked"
# N48: one replan per line. Past it the line is not retried and not failed:
# it waits for the Planner, whose decision is the only way on.
# The way out every N48 park names: there is no operator command that
# resplits a line, so the message names the two that exist and work while the
# dispatch waits in REVIEW (``reject`` answers only at APPROVAL).
_PLANNER_WAY_OUT = (
    "reassign it with `conductor dispatch reassign {dispatch_id} {line_id} <model>`, "
    "or stop the dispatch with `conductor dispatch cancel {dispatch_id}`"
)
REPLAN_PARKED_MESSAGE = (
    "Line {line_id} used its one replan and is parked for the Planner; "
    + _PLANNER_WAY_OUT
)
# N48: several passed dependencies sit on parallel branches, so no commit the
# dispatch can name carries them all and the line has no base to split onto.
# Same park and the same way out as the cap; only the sentence differs,
# because the Planner is asked to resplit rather than to rule on a split.
REPLAN_UNCOMBINED = "replan_uncombined"
REPLAN_UNCOMBINED_MESSAGE = (
    "Line {line_id} waits on passed lines whose branches share no commit and is "
    "parked for the Planner; " + _PLANNER_WAY_OUT
)
# N48: a passed dependency whose branch git cannot name has no head to split
# onto. Basing the children on the dispatch base instead would hand them the
# stubs N48 exists to stop, so the line parks with the same way out.
REPLAN_UNRESOLVED = "replan_base_unresolved"
REPLAN_UNRESOLVED_MESSAGE = (
    "Line {line_id} waits on passed lines whose branches git cannot name and is "
    "parked for the Planner; " + _PLANNER_WAY_OUT
)
_IMMEDIATE_G2 = frozenset(
    {G2_NO_COMMANDS_RISK_BEARING, G2_REVISION_MOVED, PREEXISTING_FAILURE}
)
_MACHINE_STRINGS = (
    "task_id",
    "author_provider",
    "developer_provider",
    "primary_reviewer",
    "secondary_reviewer",
    "primary_verdict",
    "blocking_reviewer",
    "return_stage",
    "handoff_ref",
    "blocked_from",
    "conflict_reviewer",
)
# Reasons that park a line for a human ruling instead of retrying or failing
# it. The drive turns any line still carrying one into a parked dispatch.
_PARKED_REASONS = (REPLAN_EXHAUSTED, REPLAN_PARKED, REPLAN_UNCOMBINED, REPLAN_UNRESOLVED)
# N76 G5 parks. Any other G5 reason (an Alembic parse failure) is a generic
# integration-test failure for the recovery table.
_G5_PARK_REASONS = frozenset(
    {"g5_not_configured", "g5_full_suite_failed", "g5_timeout", "g5_worktree_dirty"}
)
_PARK_MESSAGES = {
    REPLAN_PARKED: REPLAN_PARKED_MESSAGE,
    REPLAN_UNCOMBINED: REPLAN_UNCOMBINED_MESSAGE,
    REPLAN_UNRESOLVED: REPLAN_UNRESOLVED_MESSAGE,
}


@dataclass(frozen=True)
class _SiblingBase:
    """Where a replanned line's children start, or why they cannot (N48).

    ``head`` is the commit every child starts from, empty when the line waits
    on nothing that passed (its children start from the integrate base).
    ``uncombined`` names the passed dependencies no single head carries, and
    ``unresolved`` the passed dependencies whose branch git cannot name:
    either way the line parks instead of being split onto a base that lacks
    what it waits on.
    """

    head: str = ""
    uncombined: tuple[Line, ...] = ()
    unresolved: tuple[Line, ...] = ()


@dataclass
class Collaborators:
    """Everything the machine needs that it does not own itself."""

    planner: Planner
    selector: ModelSelector
    launcher: Launcher
    monitor: Monitor
    reviewer: Reviewer
    merger: Merger
    gate: ApprovalGate
    outcomes: OutcomeStore
    store: DispatchStore
    gates: GateRunner | None = None
    session_manager: Any | None = None
    # Receives lesson candidates made from a self-report's pitfalls. None,
    # the default, turns the memory hook off.
    lesson_sink: Callable[[list[dict[str, object]]], object] | None = None
    # N73 L4: the Super-agent assignment collaborator
    # (``superagent_assign.assigner.SuperagentAssigner``). None, the default,
    # keeps main's selector-only assignment.
    assigner: Any | None = None
    # N73 S7: low-risk G6 auto-approval. The live service turns it on
    # (``CONDUCTOR_DISPATCH_AUTO_APPROVE`` defaults on, ``=0`` keeps G6
    # human); a machine built without it keeps main's human gate.
    auto_approve: bool = False
    # M2c: where a quota handoff's conversation pack comes from (the memfw
    # CLI port by default; tests pass a fake). Off mode never reads it.
    handoff_pack_port: Any | None = None


class _AppCellMoved(Exception):
    """N80 L4: the stored claim cell changed after the drive decided a step."""


class _ReviewersSkipped(Exception):
    """Every reviewer that could take the line is skipped until ``until`` (N25a)."""

    def __init__(self, until: float) -> None:
        super().__init__(str(until))
        self.until = until


class _NoEligibleReviewer(Exception):
    """Every available reviewer once held the line, so none may review it."""

    def __init__(self, holders: list[str]) -> None:
        super().__init__(", ".join(holders))
        self.holders = holders


class _NoSecondaryReviewer(Exception):
    """The selector found no reviewer at all, so the line parks (N76)."""


class _ReviewUnavailable(Exception):
    """The reviewer adapter failed. The dispatch stays recoverable.

    ``failure_class`` / ``reason_code`` / ``retry_after`` are the native-CLI
    classifier's reading of the failure (N63); never the diagnostic itself.
    """

    def __init__(
        self,
        failure_class: str = "",
        reason_code: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(failure_class or "review_unavailable")
        self.failure_class = failure_class
        self.reason_code = reason_code
        self.retry_after = retry_after


class _ReviewersQuotaExhausted(Exception):
    """No reviewer may take the line: each one is out of quota or rate limited (N63)."""

    def __init__(self, blockers: dict[str, dict[str, object]]) -> None:
        super().__init__(", ".join(sorted(blockers)))
        self.blockers = blockers


class _ReviewRetry(Exception):
    """End this drive. The next wakeup retries the same review stage."""


@dataclass
class MachineConfig:
    max_reassigns: int = MAX_REASSIGNS
    assign_retry_seconds: float = ASSIGN_RETRY_SECONDS
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS
    launch_retries: int = LAUNCH_RETRIES
    # How many independent lines of one dispatch may run at once (N31). One
    # keeps the v1 serial invariant. Lines that share a provider beyond its
    # capacity, or whose write scopes overlap, never run together.
    max_parallel_lines: int = 1
    # N76: ``off`` | ``shadow`` | ``enforce``; None reads
    # ``CONDUCTOR_DISPATCH_LIVENESS_MODE`` (default ``shadow``). Tests and
    # embedded callers can pin it without mutating the process environment.
    liveness_mode: str | None = None
    #: A prepared Policy, or an explicit override file for
    #: ``~/.conductor/liveness_policy.yaml``.
    liveness_policy: object | None = None
    liveness_policy_path: Path | None = None
    #: How long a review-backlog queued line pauses before it asks admission
    #: again.
    review_admission_wait_seconds: float = REVIEW_ADMISSION_WAIT_SECONDS


@dataclass
class _Run:
    """Per-line execution bookkeeping the machine keeps while it drives."""

    started_at: float = 0.0
    tokens: int = 0
    complete: bool = False
    #: UTC wall time the run started (N30 cost ledger); memory only.
    started_wall: datetime | None = None
    #: The launcher's own usage and the quota reading after it returned.
    usage: Any | None = None
    quota_after: dict[str, Any] | None = None


@dataclass(frozen=True)
class Heartbeat:
    """The watchdog worker saw that a beat is due. It does not touch local state."""

    dispatch_id: str
    seq: int = 0


@dataclass(frozen=True)
class Deadline:
    """The watchdog worker reached the stage deadline for one run.

    It is dropped only when a completion for this run was queued earlier
    (``completion.seq < deadline.seq``) or the run has already completed.
    A later completion does not win.
    """

    dispatch_id: str
    line_id: str
    run_id: str | None
    attempt: int
    seq: int = 0


@dataclass(frozen=True)
class PlanReady:
    """The planner returned. Only the writer task installs the lines."""

    dispatch_id: str
    lines: tuple[Line, ...]
    seq: int = 0


@dataclass(frozen=True)
class Assigned:
    """Models chosen for lines. Only the writer task marks them ASSIGNED."""

    dispatch_id: str
    models: tuple[tuple[str, str], ...]
    seq: int = 0


@dataclass(frozen=True)
class PollResult:
    """A monitor observation for one ``(dispatch, line, run, attempt)``."""

    dispatch_id: str
    line_id: str
    run_id: str | None
    attempt: int
    output: str
    complete: bool
    stalled: bool
    timed_out: bool
    tokens: int
    seq: int = 0
    author: str = ""
    epoch: int = 0
    failure: WorkerLaunchFailure | None = None
    #: A finished run's longer report tail, when the monitor provides one.
    report: str = ""
    #: P0: the monitor's silent-worker finding, when it has one.
    silent: Any = None


@dataclass(frozen=True)
class LaunchDone:
    """The launcher returned. The writer adopts it only when the reservation matches."""

    dispatch_id: str
    line_id: str
    run_id: str
    attempt: int
    launched: str
    seq: int = 0
    author: str = ""
    session_id: str | None = None
    usage: Any | None = None
    #: N30: wall time before the launcher was called and the quota reading
    #: taken before the reservation. In both launcher modes the call returns
    #: only after the Worker process exited; the after-reading is taken by
    #: the writer in ``_apply_launch_done``.
    started_wall: datetime | None = None
    quota_before: dict[str, Any] | None = None
    #: N29: the post-run context estimate, and whether the run ended in a
    #: worker-requested handoff (its turn does not reset the rotation count).
    estimate: Any | None = None
    worker_requested: bool = False
    #: N29: the saved handoff decision (``handoff_pending``). Set, the run is
    #: confirmed but the line stays ASSIGNED: the drive never polls it.
    handoff: dict[str, Any] | None = None


@dataclass(frozen=True)
class ReviewResult:
    """A reviewer verdict for one ``(dispatch, line, run, attempt)``."""

    dispatch_id: str
    line_id: str
    run_id: str | None
    attempt: int
    passed: bool
    blockers: tuple[str, ...]
    seq: int = 0


@dataclass(frozen=True)
class ReassignNeeded:
    """A poll worker saw a stall or a timeout.

    The worker does not launch and does not write state. The drive reassigns
    the line and then launches the replacement run.
    """

    dispatch_id: str
    line_id: str
    run_id: str | None
    attempt: int
    reason: str
    seq: int = 0


_QUEUED_EVENTS = (
    Heartbeat,
    Deadline,
    PlanReady,
    Assigned,
    PollResult,
    LaunchDone,
    ReviewResult,
    ReassignNeeded,
)


def _callable_accepts_keyword(callable_obj: Callable[..., Any], keyword: str) -> bool:
    """Inspect a collaborator once; some builtin or extension callables are opaque."""
    try:
        parameters = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return False
    return any(
        name == keyword
        and parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        for name, parameter in parameters.items()
    ) or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _accepts_keyword_only(callable_obj: Callable[..., Any], keyword: str) -> bool:
    """True when ``keyword`` is a keyword-only parameter (or ``**kwargs``)."""
    try:
        parameters = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return False
    return any(
        (name == keyword and parameter.kind is inspect.Parameter.KEYWORD_ONLY)
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for name, parameter in parameters.items()
    )


class DispatchMachine:
    """Drives one dispatch through its states until it parks or terminates."""

    def __init__(
        self,
        collaborators: Collaborators,
        *,
        config: MachineConfig | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._c = collaborators
        monitor = getattr(collaborators, "monitor", None)
        poll = getattr(monitor, "poll", None)
        self._poll_accepts_model = _callable_accepts_keyword(poll, "model")
        self._dedup_book = p0_hooks.DedupBook()
        self._config = config or MachineConfig()
        self._sleep = sleep
        self._clock = clock
        # Review retries scheduled by this drive: the loop guard is offset by them.
        self._review_retries = 0
        # Human requests applied inside a monitor or review pass (N77): the
        # loop guard is offset by them, as for the ones absorbed at its top.
        self._midpass_absorbed = 0
        # Wall time for persisted review retries (N42); survives a restart.
        self._wall: Callable[[], float] = time.time
        self._runs: dict[str, _Run] = {}
        self._outputs: dict[str, str] = {}
        self._epoch0 = 0
        self._abandoned = False
        self._self_report_line: str | None = None
        self._baseline: Dispatch | None = None
        self._local: Dispatch | None = None
        # Mailbox only: never held across a save or an external wait.
        self._mailbox = asyncio.Lock()
        self._queue: asyncio.Queue[
            tuple[Any, tuple[Any, ...], dict[str, Any], asyncio.Future[Any]]
        ] = asyncio.Queue()
        self._actor_task: asyncio.Task[None] | None = None
        self._actor_ready: asyncio.Event | None = None
        self._closing = False
        self._drive_owner = False
        self._event_seq = 0
        self._inflight: list[asyncio.Task[Any]] = []
        # Parallel lines (N31). Each line task runs one ``_launch_line`` and
        # counts as the writer while it holds ``_writer``; the drive and every
        # line task hand ``_writer`` over at each external wait, so exactly one
        # coroutine changes the row at a time.
        self._writer = asyncio.Lock()
        self._writer_owner: asyncio.Task[Any] | None = None
        self._line_tasks: dict[str, asyncio.Task[Any]] = {}
        self._stale_fix_dropped = False
        self._own_abandoned: Callable[[bool], None] | None = None
        self._own_epoch: Callable[[int], None] | None = None
        self._own_baseline: Callable[[Dispatch | None], None] | None = None
        self._own_local: Callable[[Dispatch | None], None] | None = None
        self._own_put_run: Callable[[str, _Run], None] | None = None
        self._own_pop_run: Callable[[str], None] | None = None
        self._own_ensure_run: Callable[[str], _Run] | None = None
        self._own_put_output: Callable[[str, str], None] | None = None
        self._bound_loop: asyncio.AbstractEventLoop | None = None

    def _check_loop(self) -> None:
        """Bind to the first running loop; refuse any other loop by name."""
        loop = asyncio.get_running_loop()
        bound = self._bound_loop
        if bound is None:
            self._bound_loop = loop
            return
        if bound is not loop:
            raise DispatchLoopMismatch(
                "DispatchMachine is bound to another event loop; run every "
                "step for this machine on the loop that first drove it"
            )

    def _on_actor(self) -> bool:
        task = self._actor_task
        current = asyncio.current_task()
        if task is not None and current is task:
            return True
        return current is not None and self._is_line_task(current)

    def _is_line_task(self, task: asyncio.Task[Any]) -> bool:
        return any(task is owned for owned in self._line_tasks.values())

    def _install_owners(self) -> None:
        """Closures are the only writers of the machine's private fields."""

        def set_abandoned(value: bool) -> None:
            self._abandoned = value

        def set_epoch(value: int) -> None:
            self._epoch0 = value

        def set_baseline(value: Dispatch | None) -> None:
            self._baseline = value

        def set_local(value: Dispatch | None) -> None:
            self._local = value

        def put_run(line_id: str, run: _Run) -> None:
            self._runs[line_id] = run

        def pop_run(line_id: str) -> None:
            self._runs.pop(line_id, None)

        def ensure_run(line_id: str) -> _Run:
            return self._runs.setdefault(
                line_id, _Run(started_at=self._clock(), started_wall=self._cost_wall())
            )

        def put_output(line_id: str, text: str) -> None:
            self._outputs[line_id] = text

        self._own_abandoned = set_abandoned
        self._own_epoch = set_epoch
        self._own_baseline = set_baseline
        self._own_local = set_local
        self._own_put_run = put_run
        self._own_pop_run = pop_run
        self._own_ensure_run = ensure_run
        self._own_put_output = put_output

    async def _actor_loop(self) -> None:
        """Apply one queued event at a time when ``run`` has not claimed the drive.

        ``run`` itself is the writer. This loop remains for callers that submit
        work without entering ``run``.
        """
        self._install_owners()
        ready = self._actor_ready
        if ready is not None:
            ready.set()
        while True:
            async with self._mailbox:
                if self._queue.empty():
                    self._actor_task = None
                    return
                fn, args, kwargs, fut = self._queue.get_nowait()
            await self._run_queued(fn, args, kwargs, fut)

    def _stamp_locked(self, args: tuple[Any, ...]) -> None:
        """Give the event its queue sequence. The mailbox lock is already held."""
        for arg in args:
            if isinstance(arg, _QUEUED_EVENTS):
                self._event_seq += 1
                object.__setattr__(arg, "seq", self._event_seq)
                return

    async def _claim_drive(self) -> None:
        """The coroutine inside ``run`` is the only writer until it returns."""
        self._install_owners()
        await self._take_writer()
        async with self._mailbox:
            self._actor_task = asyncio.current_task()
            self._drive_owner = True
            if self._actor_ready is not None:
                self._actor_ready.set()

    def _release_drive(self) -> None:
        if self._actor_task is asyncio.current_task():
            self._actor_task = None
        self._drive_owner = False
        if self._writer_owner is asyncio.current_task():
            self._give_writer()

    async def _take_writer(self) -> None:
        await self._writer.acquire()
        self._writer_owner = asyncio.current_task()

    def _give_writer(self) -> None:
        self._writer_owner = None
        self._writer.release()

    async def _run_queued(
        self,
        fn: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        fut: asyncio.Future[Any],
    ) -> None:
        try:
            result = await fn(*args, **kwargs)
        except asyncio.CancelledError:
            if not fut.done():
                fut.cancel()
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            return
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
            return
        if not fut.done():
            fut.set_result(result)

    async def aclose(self) -> None:
        """Stop new work, then finish the actor command that is already saving.

        Close does not cancel that save. Workers that have not started cleanup
        are cancelled once. A worker already in cleanup is only awaited, so a
        second cancel cannot skip it. The caller keeps the drive owner until
        this returns.
        """
        self._check_loop()
        async with self._mailbox:
            self._closing = True
        for task in list(self._inflight):
            if not task.done() and task.cancelling() == 0:
                task.cancel()
        while True:
            actor = self._actor_task
            current = asyncio.current_task()
            # A dead actor is not restarted. Close already refused new work, and
            # spinning on a leftover queue would wedge loop teardown.
            actor_alive = actor is not None and actor is not current and not actor.done()
            pending = [task for task in list(self._inflight) if not task.done()]
            if actor_alive:
                await asyncio.shield(actor)
                continue
            if pending:
                await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))
                continue
            return

    async def _submit(self, fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
        """Run ``fn`` on the writer. A call from the writer runs inline.

        Every queued event takes its ``seq`` under the mailbox lock, before it
        is visible to another coroutine. The drive owns that writer during
        ``run`` and is the only consumer. Other callers start ``_actor_loop``.
        """
        self._check_loop()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        inline = False
        ready: asyncio.Event | None = None
        async with self._mailbox:
            self._stamp_locked(args)
            if self._on_actor():
                inline = True
            else:
                if self._closing:
                    raise asyncio.CancelledError
                self._queue.put_nowait((fn, args, kwargs, fut))
                if not self._drive_owner and (
                    self._actor_task is None or self._actor_task.done()
                ):
                    ready = asyncio.Event()
                    self._actor_ready = ready
                    self._actor_task = asyncio.create_task(
                        self._actor_loop(), name="dispatch-actor"
                    )
                else:
                    ready = self._actor_ready
        if inline:
            return await fn(*args, **kwargs)
        if ready is not None and not ready.is_set():
            await ready.wait()
        return await fut

    async def _leave(self, awaitable: Awaitable[Any]) -> Any:
        """Wait outside the actor. From the actor, keep draining events until it finishes.

        A line task (N31) hands the writer back while it waits, so the drive
        and the other lines go on, and takes it again before it continues.
        """
        if not self._on_actor():
            return await awaitable
        current = asyncio.current_task()
        if current is not self._actor_task:
            self._give_writer()
            try:
                return await awaitable
            finally:
                await self._take_writer()
        task = asyncio.create_task(self._passthrough(awaitable), name="dispatch-worker")
        self._inflight.append(task)
        try:
            return await self._pump_until(task)
        finally:
            if task in self._inflight:
                self._inflight.remove(task)

    async def _passthrough(self, awaitable: Awaitable[Any]) -> Any:
        return await awaitable

    async def _pump_until(self, task: asyncio.Task[Any]) -> Any:
        """The actor keeps applying events while one worker is still waiting.

        The writer is free while the drive waits, so a line task can go on;
        the drive takes it for each queued event and again before it returns.
        """
        # Only a drive that holds the writer hands it over (``_actor_loop``
        # callers never take it).
        handed = self._writer_owner is asyncio.current_task()
        if handed:
            self._give_writer()
        try:
            while not task.done():
                getter = asyncio.create_task(self._queue.get(), name="dispatch-actor-pump")
                try:
                    done, _pending = await asyncio.wait(
                        {task, getter}, return_when=asyncio.FIRST_COMPLETED
                    )
                except asyncio.CancelledError:
                    if not getter.done():
                        getter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await getter
                    raise
                if getter in done:
                    fn, args, kwargs, fut = getter.result()
                    if handed:
                        await self._take_writer()
                    try:
                        await self._run_queued(fn, args, kwargs, fut)
                    finally:
                        if handed:
                            self._give_writer()
                elif not getter.done():
                    getter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await getter
        finally:
            if handed:
                await self._take_writer()
        return task.result()

    async def _activate(self, dispatch: Dispatch) -> None:
        assert self._own_local is not None
        assert self._own_epoch is not None
        assert self._own_abandoned is not None
        assert self._own_baseline is not None
        self._own_local(dispatch)
        self._own_epoch(drive_epoch(dispatch))
        self._own_abandoned(False)
        self._own_baseline(copy.deepcopy(dispatch))

    async def _abandon(self, dispatch: Dispatch) -> None:
        assert self._own_abandoned is not None
        self._own_abandoned(True)
        fresh = await self._c.store.get(dispatch.id)
        if fresh is None or not _audit_tail_is_stale(fresh):
            await self._record_stale_drive(dispatch)

    async def _flag_abandoned(self, value: bool = True) -> None:
        assert self._own_abandoned is not None
        self._own_abandoned(value)

    async def run(self, dispatch: Dispatch) -> Dispatch:
        """Advance the dispatch as far as it can go without a human.

        This coroutine is the only writer. Workers enqueue events; between
        steps this task applies them and saves.
        """
        self._check_loop()
        self._closing = False
        await self._claim_drive()
        try:
            try:
                await self._activate(dispatch)
                # N73 review fix 5: adopted launch flags survive a restart.
                if self._c.assigner is not None:
                    from .superagent_assign import launch_env as _launch_env

                    _launch_env.restore(dispatch)
                if dispatch.state in (DispatchState.APPROVAL, DispatchState.REVIEW):
                    await self._resume(dispatch)
                elif dispatch.state is DispatchState.SUBMITTED:
                    if not await self._plan(dispatch):
                        # A planner failure parked the dispatch; nothing launches.
                        return dispatch
                    await self._assign(dispatch)
                    if _path_ownership_queued(dispatch):
                        return dispatch
                    if self._c.gates is None and any(
                        line.status is LineStatus.BLOCKED for line in dispatch.lines
                    ):
                        await self._save(dispatch)
                        return dispatch
                    # A line that burned its attempts fails the whole dispatch, and
                    # a dispatch that has already failed must not advance.
                    if self._c.gates is None:
                        await self._launch(dispatch)
                        if not self._stopped(dispatch):
                            await self._monitor(dispatch)
                        if not self._stopped(dispatch):
                            await self._review(dispatch)
                    elif not self._stopped(dispatch):
                        await self._run_gated(dispatch)
                elif dispatch.state in (DispatchState.MERGE,):
                    await self._merge(dispatch)
                elif (
                    self._c.gates is not None
                    and dispatch.state
                    in (DispatchState.LAUNCHING, DispatchState.MONITORING)
                ):
                    await self._run_gated(dispatch)
                elif dispatch.state is DispatchState.ASSIGNED:
                    if _path_ownership_queued(dispatch):
                        await self._assign(dispatch)
                        if _path_ownership_queued(dispatch):
                            return dispatch
                    if self._c.gates is not None:
                        await self._run_gated(dispatch)
                    else:
                        await self._launch(dispatch)
                        if not self._stopped(dispatch):
                            await self._monitor(dispatch)
                        if not self._stopped(dispatch):
                            await self._review(dispatch)
                elif dispatch.state is DispatchState.PLANNING:
                    await self._resume_planning(dispatch)
                else:
                    raise DispatchError(
                        f"Dispatch {dispatch.id} is already {dispatch.state.value}"
                    )
            except _ReviewRetry:
                return dispatch
            except StaleDrive:
                await self._abandon(dispatch)
                return dispatch
            except (DispatchError, MergeConflictError) as exc:
                await self._fail(dispatch, str(exc))
            return dispatch
        finally:
            try:
                await self._stop_line_tasks()
            except Exception as exc:  # the drive is ending either way
                logger.warning("Could not stop line tasks: %s", type(exc).__name__)
            try:
                if not self._abandoned and dispatch.state in (
                    DispatchState.COMPLETE,
                    DispatchState.ABORT,
                    DispatchState.ERROR,
                ):
                    from .superagent_assign import launch_env as _launch_env

                    _launch_env.clear_dispatch(dispatch.id)
                    await self._cleanup_terminal_worktrees(dispatch)
            finally:
                self._release_drive()

    async def _cleanup_terminal_worktrees(self, dispatch: Dispatch) -> None:
        """Clean terminal worktrees; preserve dirty edits in local recovery archives."""
        launcher_cleanup = getattr(self._c.launcher, "cleanup_worktrees", None)
        merger_cleanup = getattr(self._c.merger, "cleanup_worktree", None)
        cleanup_errors: list[str] = []
        for cleanup, args in (
            (launcher_cleanup, (dispatch,)),
            (merger_cleanup, ()),
        ):
            if not callable(cleanup):
                continue
            try:
                result = cleanup(*args)
                if inspect.isawaitable(result):
                    result = await self._leave(result)
                if isinstance(result, str) and result:
                    cleanup_errors.append(result)
                elif isinstance(result, list):
                    cleanup_errors.extend(item for item in result if isinstance(item, str))
            except Exception as exc:
                logger.warning("Could not clean terminal dispatch worktree: %s", type(exc).__name__)
                cleanup_errors.append(f"terminal cleanup failed ({type(exc).__name__})")
        if cleanup_errors:
            if not isinstance(dispatch.gates, dict):
                dispatch.gates = {}
            dispatch.gates["worktree_cleanup"] = {
                "status": "partial",
                "errors": cleanup_errors,
            }
            try:
                await self._save(dispatch)
            except Exception as exc:
                logger.warning("Could not persist worktree cleanup errors: %s", type(exc).__name__)

    def _stopped(self, dispatch: Dispatch) -> bool:
        """True when this drive must not start another phase.

        ``ABORT`` and ``ERROR`` are terminal. ``_abandoned`` means a newer
        epoch owns the row, so the rest of this drive is not allowed to write.
        """
        return self._abandoned or dispatch.state in (DispatchState.ABORT, DispatchState.ERROR)

    # -- states ---------------------------------------------------------------

    async def _enter_phase(self, dispatch: Dispatch, state: DispatchState) -> None:
        """Record a phase on the actor. Callers do not assign ``dispatch.state``."""
        if not self._on_actor():
            await self._submit(self._enter_phase, dispatch, state)
            return
        dispatch.state = state

    async def _apply_plan_ready(self, event: PlanReady, dispatch: Dispatch) -> None:
        if not self._on_actor():
            await self._submit(self._apply_plan_ready, event, dispatch)
            return
        if dispatch.id != event.dispatch_id:
            return
        dispatch.lines = list(event.lines)
        dispatch.integrate_branch = f"integrate/auto-dispatch-{dispatch.id}"
        for line in dispatch.lines:
            line.branch = f"{dispatch.branch_prefix}/line-{line.id}"
            line.status = LineStatus.PENDING
        if self._c.gates is not None:
            await self._c.gates.prepare_async(dispatch)
        await self._save_locked(dispatch)

    async def _apply_assigned(self, event: Assigned, dispatch: Dispatch) -> None:
        if not self._on_actor():
            await self._submit(self._apply_assigned, event, dispatch)
            return
        if dispatch.id != event.dispatch_id:
            return
        chosen = dict(event.models)
        for line in dispatch.lines:
            model = chosen.get(line.id)
            if model is None or line.status is LineStatus.FAILED:
                continue
            line.assigned_model = model
            line.status = LineStatus.ASSIGNED
        dispatch.state = DispatchState.ASSIGNED
        await self._save_locked(dispatch)

    async def _mark_passed(self, dispatch: Dispatch, line: Line) -> None:
        if not self._on_actor():
            await self._submit(self._mark_passed, dispatch, line)
            return
        line.status = LineStatus.PASSED
        line.blockers = []
        drop_reassign_brief(dispatch, line.id)
        self._record_outcome(dispatch, line, reviewer_verdict=REVIEW_MERGED)

    async def _plan(self, dispatch: Dispatch) -> bool:
        """Plan the goal. False means the planner raised and the dispatch is parked.

        A planner failure parks in REVIEW as ``planner_failed:<reason>`` with no
        lines. The reason is one of the fixed codes, so an operator reads why
        the plan never arrived; the exception's own text may carry provider
        output and never reaches the row, the message or a log line.
        An empty plan still fails the dispatch.
        """
        await self._enter_phase(dispatch, DispatchState.PLANNING)
        await self._save(dispatch)
        try:
            lines = await self._leave(self._c.planner.plan(dispatch.goal))
        except (StaleDrive, _ReviewRetry):
            raise
        except Exception as exc:
            reason = planner_failure_reason(exc)
            logger.warning(
                "Dispatch planning failed %s %s %s",
                dispatch.id,
                type(exc).__name__,
                reason,
            )
            await self._park_planning(dispatch, PLANNER_FAILED, reason=reason)
            return False
        if not lines:
            raise DispatchError("The goal produced no work lines; refine it and retry")
        await self._apply_plan_ready(PlanReady(dispatch.id, tuple(lines)), dispatch)
        return True

    async def _resume_planning(self, dispatch: Dispatch) -> None:
        """Continue PLANNING from the archived spec, or park when it is missing.

        An empty plan or a planner failure parks. It does not become ERROR.
        """
        if not (dispatch.spec_ref or "").strip():
            await self._park_missing_spec(dispatch)
            return
        try:
            lines = await self._leave(self._c.planner.plan(dispatch.spec_ref))
        except (StaleDrive, _ReviewRetry):
            raise
        except Exception as exc:
            reason = planner_failure_reason(exc)
            logger.warning(
                "Dispatch resume planning failed %s %s %s",
                dispatch.id,
                type(exc).__name__,
                reason,
            )
            await self._park_planning(dispatch, PLANNER_FAILED, reason=reason)
            return
        if not lines:
            await self._park_missing_spec(dispatch)
            return
        await self._apply_plan_ready(PlanReady(dispatch.id, tuple(lines)), dispatch)
        await self._assign(dispatch)
        if _path_ownership_queued(dispatch):
            return
        if self._stopped(dispatch):
            return
        if self._c.gates is None and any(
            line.status is LineStatus.BLOCKED for line in dispatch.lines
        ):
            await self._save(dispatch)
            return
        if self._c.gates is None:
            await self._launch(dispatch)
            if not self._stopped(dispatch):
                await self._monitor(dispatch)
            if not self._stopped(dispatch):
                await self._review(dispatch)
            return
        await self._run_gated(dispatch)

    async def _park_missing_spec(self, dispatch: Dispatch) -> None:
        """Park ``resume_spec_missing``. A dispatch with no lines still stays REVIEW."""
        await self._park_planning(dispatch, RESUME_SPEC_MISSING)

    async def _park_planning(
        self, dispatch: Dispatch, code: str, reason: str = ""
    ) -> None:
        """Park a planning stop under ``code``. A dispatch with no lines stays REVIEW.

        A planner failure carries its fixed reason, so the parked message is
        ``planner_failed:<reason>``: the operator reads why the plan never
        arrived, and the model's own words stay out of the row.
        """
        if dispatch.lines:
            for line in dispatch.lines:
                if line.status is not LineStatus.FAILED:
                    self._block_line(dispatch, line, code)
        gates = self._c.gates
        if gates is not None:
            sched = gates._scheduler(dispatch)
            audit = sched.setdefault("audit", [])
            if isinstance(audit, list):
                audit.append({"kind": code})
        if dispatch.lines:
            await self._park_blocked(dispatch)
            return
        await self._enter_phase(dispatch, DispatchState.REVIEW)
        dispatch.message = planner_failure_message(reason) if reason else code
        await self._save(dispatch)

    async def _assign(self, dispatch: Dispatch) -> None:
        chosen: list[tuple[str, str]] = []
        queued = False
        parked_capacity = False
        # N73 L4: one Super-agent call for every unassigned line ({} when off).
        superagent = await self._superagent_decisions(dispatch)
        for line in dispatch.lines:
            if line.status is LineStatus.FAILED:
                continue
            # The planner may already have chosen the model. Leaving that line
            # PENDING skips launch, because only ASSIGNED lines start.
            if line.assigned_model:
                checker = getattr(self._c.selector, "check_path_ownership", None)
                if checker is not None:
                    decision = await self._leave(checker(line, dispatch=dispatch))
                    if not decision.allowed:
                        if decision.queued:
                            queued = True
                            continue
                        raise DispatchError(decision.reason)
                chosen.append((line.id, line.assigned_model))
                continue
            model = await self._select_with_retry(dispatch, line)
            if model is None:
                if getattr(self._c.selector, "work_decision", None) == "queue":
                    queued = True
                    continue
                ownership = getattr(self._c.selector, "path_ownership_decision", None)
                if ownership is not None and ownership.refused:
                    raise DispatchError(ownership.reason)
                # text_only_worker is the filter that removed a text-only
                # Worker from a write line. no_eligible_worker is the park
                # code once nobody who can write the line still has quota.
                if getattr(self._c.selector, "work_refusal", None) == "text_only_worker":
                    if liveness_modes.should_act(self._liveness_mode()):
                        # N76 enforce: a line nobody can staff is a park, never ERROR.
                        # Keep selecting the other lines. Returning here left them
                        # PENDING with no author; the later REVIEW drive does not
                        # assign, and those PENDING lines become a false DAG error
                        # once this line recovers.
                        self._park_no_eligible_worker(dispatch, line)
                        parked_capacity = True
                        continue
                    # off/shadow keep main's ERROR path.
                    line.failure_reason = "no_eligible_worker"
                    raise DispatchError(f"no_eligible_worker:{line.id}")
                raise DispatchError(
                    f"No eligible model for line {line.id}; the pool is exhausted or excluded"
                )
            model = self._apply_superagent(dispatch, line, model, superagent.get(line.id))
            chosen.append((line.id, model))
        # Path ownership reads claims from saved rows. Model selection above
        # may sleep and is not guarded; the final check of every chosen line
        # and the save that turns them ASSIGNED (claims) run under one lock,
        # so two dispatches cannot both pass the check on overlapping paths.
        guard = getattr(self._c.selector, "path_claim_guard", None)
        held = guard() if callable(guard) else contextlib.nullcontext()
        async with held:
            checker = getattr(self._c.selector, "check_path_ownership", None)
            if checker is not None and not queued:
                for line_id, _model in chosen:
                    line = dispatch.line(line_id)
                    if line is None:
                        continue
                    decision = await self._leave(checker(line, dispatch=dispatch))
                    if not decision.allowed:
                        if not decision.queued:
                            raise DispatchError(decision.reason)
                        queued = True
            if queued:
                # All or nothing: a dispatch waiting on another's paths holds
                # no claim of its own (only ASSIGNED and later lines claim), so
                # two partly queued dispatches cannot each hold what the other
                # waits for.
                chosen = []
            await self._apply_assigned(Assigned(dispatch.id, tuple(chosen)), dispatch)
        if queued:
            reasons = sorted(
                line.failure_reason
                for line in dispatch.lines
                if line.status is LineStatus.PENDING and line.failure_reason
            )
            dispatch.message = "Lines queued by path ownership: " + ", ".join(reasons)
            await self._save(dispatch)
        elif parked_capacity:
            # Chosen lines are already ASSIGNED. Parking now, instead of
            # returning from the selector loop, leaves the dispatch in REVIEW
            # without stranding the other lines as PENDING. The next drive
            # does not assign, and those PENDING lines become a false DAG
            # error once this line recovers.
            await self._park_blocked(dispatch)

    async def _launch(self, dispatch: Dispatch) -> None:
        await self._enter_phase(dispatch, DispatchState.LAUNCHING)
        await self._save(dispatch)
        for line in dispatch.lines:
            if line.status is not LineStatus.ASSIGNED:
                continue
            await self._launch_line(dispatch, line)
            if self._stopped(dispatch):
                return

    async def _monitor(self, dispatch: Dispatch) -> None:
        await self._enter_phase(dispatch, DispatchState.MONITORING)
        await self._save(dispatch)
        # Lines a mid-pass request put back under watch (N77). They wait for
        # the next pass, which polls them first under a deadline of their own.
        deferred: set[str] = set()

        def watched() -> list[Line]:
            return [
                line
                for line in dispatch.lines
                if self._watching(line) and line.id not in deferred
            ]

        running = watched()
        while running:
            if self._abandoned:
                return
            gates = self._c.gates
            if gates is None:
                for line in running:
                    await self._poll_line(dispatch, line)
                    if self._abandoned:
                        return
                running = [line for line in dispatch.lines if self._watching(line)]
                if not running:
                    break
                await self._sleep(self._config.poll_interval_seconds)
                continue
            if gates._settings.watchdog_seconds <= 0:
                for line in running:
                    await self._poll_line(dispatch, line)
                    if self._abandoned:
                        return
                running = [line for line in dispatch.lines if self._watching(line)]
                if not running:
                    break
                gates.beat(dispatch)
                if gates.watch_expired(dispatch):
                    gates.clear_watch(dispatch)
                    await self._watchdog(dispatch, running)
                    return
                continue
            if gates.arm_watch(dispatch, self._config.poll_interval_seconds):
                await self._save(dispatch)
            expired = await self._race_poll(dispatch, running)
            if self._abandoned:
                return
            running = watched()
            if expired:
                gates.clear_watch(dispatch)
                if running:
                    await self._watchdog(dispatch, running)
                return
            if not running:
                break
            # A request queued while these lines run is applied now, not
            # after every line settles and every review of the pass (N77).
            before = {line.id for line in running}
            if await self._absorb_midpass(dispatch):
                deferred.update(
                    line.id
                    for line in dispatch.lines
                    if self._watching(line) and line.id not in before
                )
            if self._abandoned:
                return
            running = watched()
            if not running:
                break
            delay = self._post_poll_delay(dispatch)
            if delay <= 0 and self._deadline_due(dispatch):
                gates.clear_watch(dispatch)
                if running:
                    await self._watchdog(dispatch, running)
                return
            await self._sleep(delay)
            if gates.heartbeat_due(dispatch):
                await self._submit(self._apply_heartbeat, Heartbeat(dispatch.id), dispatch)
            if gates.watch_expired(dispatch):
                running = watched()
                gates.clear_watch(dispatch)
                if running:
                    await self._watchdog(dispatch, running)
                return
        if self._c.gates is not None:
            self._c.gates.clear_watch(dispatch)

    async def _race_poll(self, dispatch: Dispatch, running: list[Line]) -> bool:
        """Poll and the deadline run together. A stuck poll cannot hide the timeout."""
        gates = self._c.gates
        assert gates is not None
        poll_task = asyncio.create_task(
            self._poll_running(dispatch, running), name="dispatch-poll"
        )
        wait_task = asyncio.create_task(
            self._wait_deadline(dispatch), name="dispatch-deadline"
        )
        try:
            await self._wait_first({poll_task, wait_task})
        finally:
            await self._drain_raced(poll_task, wait_task)
        return gates.watch_expired(dispatch)

    async def _wait_first(self, tasks: set[asyncio.Task[Any]]) -> None:
        """Wait until one task finishes. The writer applies the mailbox meanwhile.

        A child blocked in ``_submit`` would deadlock if this wait did not
        drain the queue. Callers that are not the writer leave that to the
        actor loop.
        """
        if not self._on_actor():
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            return
        while not any(task.done() for task in tasks):
            getter = asyncio.create_task(self._queue.get(), name="dispatch-actor-pump")
            try:
                done, _pending = await asyncio.wait(
                    {*tasks, getter}, return_when=asyncio.FIRST_COMPLETED
                )
            except asyncio.CancelledError:
                if not getter.done():
                    getter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await getter
                raise
            if getter in done and not getter.cancelled():
                fn, args, kwargs, fut = getter.result()
                await self._run_queued(fn, args, kwargs, fut)
            elif not getter.done():
                getter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await getter

    async def _drain_raced(
        self,
        poll_task: asyncio.Task[None],
        wait_task: asyncio.Task[None],
    ) -> None:
        """Cancel the loser, then drain cleanup even if the parent is cancelled again.

        A second cancel must not abort a ``finally`` that has already started.
        The parent cancel is raised again only after that cleanup finishes.
        """
        children = (poll_task, wait_task)
        for child in children:
            if not child.done() and child.cancelling() == 0:
                child.cancel()
        pending = set(children)
        cancelled_again = False
        while pending:
            current = asyncio.current_task()
            held = current.cancelling() if current is not None else 0
            if current is not None:
                for _ in range(held):
                    current.uncancel()
            if held:
                cancelled_again = True
            try:
                await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))
                pending.clear()
            except asyncio.CancelledError:
                cancelled_again = True
                pending = {t for t in pending if not t.done()}
        if cancelled_again:
            raise asyncio.CancelledError
        if poll_task.done() and not poll_task.cancelled():
            error = poll_task.exception()
            if error is not None:
                raise error

    async def _poll_running(self, dispatch: Dispatch, running: list[Line]) -> None:
        for line in list(running):
            if self._abandoned:
                return
            if self._watching(line):
                await self._poll_line(dispatch, line)

    async def _wait_deadline(self, dispatch: Dispatch) -> None:
        """Sleep on the machine clock and persist a heartbeat while the poll is in flight."""
        gates = self._c.gates
        assert gates is not None
        while not gates.watch_expired(dispatch):
            if self._abandoned:
                return
            before = gates._clock()
            delay = self._wake_delay(dispatch, before)
            await self._sleep(delay)
            if self._abandoned:
                return
            if gates.heartbeat_due(dispatch):
                await self._submit(self._apply_heartbeat, Heartbeat(dispatch.id), dispatch)
            if gates.watch_expired(dispatch):
                for line in dispatch.lines:
                    if line.status is not LineStatus.RUNNING:
                        continue
                    await self._submit(
                        self._apply_deadline,
                        Deadline(
                            dispatch.id,
                            line.id,
                            line.current_run,
                            line.attempts,
                        ),
                        dispatch,
                    )
                return
            if gates._clock() == before:
                # ``_sleep`` did not move this clock. A real wait keeps a frozen
                # test clock from spinning, and it stays cancellable.
                await asyncio.sleep(delay if delay > 0 else gates._settings.heartbeat_interval_seconds)

    def _wake_delay(self, dispatch: Dispatch, now: object) -> float:
        """Sleep until the next heartbeat, or until the armed deadline.

        A heartbeat of 30 seconds must not hide a deadline that is sooner.
        The comparison is strict ``>``, so a clock that lands on the deadline
        takes one extra short step.
        """
        gates = self._c.gates
        assert gates is not None
        interval = gates._settings.heartbeat_interval_seconds
        if interval < 0:
            interval = 0.0
        sched = dispatch.gates.get("scheduler") if isinstance(dispatch.gates, dict) else None
        raw = sched.get("deadline_at") if isinstance(sched, dict) else None
        if not isinstance(raw, str) or not raw or not isinstance(now, datetime):
            return interval
        try:
            deadline = datetime.fromisoformat(raw)
        except ValueError:
            return interval
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        remaining = (deadline - now.astimezone(timezone.utc)).total_seconds()
        if remaining <= 0:
            return min(interval, 0.001) if interval > 0 else 0.001
        return min(interval, remaining)

    def _deadline_remaining(self, dispatch: Dispatch) -> float | None:
        """Seconds until the armed deadline, or None when none is armed."""
        gates = self._c.gates
        if gates is None:
            return None
        sched = dispatch.gates.get("scheduler") if isinstance(dispatch.gates, dict) else None
        raw = sched.get("deadline_at") if isinstance(sched, dict) else None
        if not isinstance(raw, str) or not raw:
            return None
        now = gates._clock()
        if not isinstance(now, datetime):
            return None
        try:
            deadline = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return (deadline - now.astimezone(timezone.utc)).total_seconds()

    def _deadline_due(self, dispatch: Dispatch) -> bool:
        remaining = self._deadline_remaining(dispatch)
        return remaining is not None and remaining <= 0

    def _post_poll_delay(self, dispatch: Dispatch) -> float:
        """Sleep ``min(poll, deadline - now, heartbeat)``, and never below zero."""
        poll = self._config.poll_interval_seconds
        if poll < 0:
            poll = 0.0
        gates = self._c.gates
        assert gates is not None
        heartbeat = gates._settings.heartbeat_interval_seconds
        if heartbeat < 0:
            heartbeat = 0.0
        remaining = self._deadline_remaining(dispatch)
        if remaining is None:
            return min(poll, heartbeat)
        return max(0.0, min(poll, heartbeat, remaining))

    async def _review(self, dispatch: Dispatch) -> None:
        if await self._fence(dispatch):
            return
        await self._enter_phase(dispatch, DispatchState.REVIEW)
        blocked = await self._review_running(dispatch)
        await self._save(dispatch)
        if blocked:
            # A human decides: reassign the line through the API, or leave it
            # blocked and the dispatch stays parked here.
            dispatch.message = _blocked_message(blocked)
            await self._save(dispatch)
            return
        await self._gate(dispatch)

    async def _review_running(self, dispatch: Dispatch) -> list[Line]:
        """Review lines that are still RUNNING. Gated lines wait until G2 allows it."""
        blocked: list[Line] = []
        for line in dispatch.lines:
            if line.status is not LineStatus.RUNNING:
                continue
            if self._c.gates is not None and not await self._c.gates.allows_review_async(dispatch, line.id):
                continue
            line.status = LineStatus.REVIEWING
            # Ungated: the HEAD this run left enters review now.
            self._note_reviewed_head(dispatch, line, self._latest_run_head(dispatch, line))
            token = _result_token(line)
            holders = sorted(self._line_holders(dispatch, line))
            reviewer = await self._select_with_retry(
                dispatch,
                line,
                role=ModelRole.REVIEW,
                exclude=[line.assigned_model or "", *holders],
            )
            if _result_token(line) != token:
                await self._discard_stale(dispatch)
                continue
            if reviewer is None and await self._only_holders_could_review(
                dispatch, line, [line.assigned_model or ""], holders
            ):
                self._park_no_eligible_reviewer(dispatch, line, holders)
                blocked.append(line)
                continue
            if reviewer is None:
                quota = await self._review_quota_blockers(dispatch, line, holders)
                if quota:
                    # No reviewer has quota left: park, never ERROR (N63).
                    line.blockers = [
                        "No reviewer has quota left for this line: "
                        + "; ".join(
                            f"{name}: {entry.get('reason') or 'no headroom'} until "
                            f"{entry.get('resets_at') or 'unknown'}"
                            for name, entry in sorted(quota.items())
                        )
                    ]
                    self._block_line(dispatch, line, REVIEWERS_QUOTA_EXHAUSTED)
                    blocked.append(line)
                    continue
                refusal = getattr(self._c.selector, "review_refusal", None)
                if refusal == NO_SECONDARY_REVIEWER:
                    if liveness_modes.should_act(self._liveness_mode()):
                        self._park_no_secondary_reviewer(dispatch, line)
                        blocked.append(line)
                        continue
                    # off/shadow keep main's ERROR path.
                    line.failure_reason = NO_SECONDARY_REVIEWER
                    raise DispatchError(
                        f"No reviewer model is available for line {line.id}: "
                        f"{NO_SECONDARY_REVIEWER}"
                    )
                raise DispatchError(f"No reviewer model is available for line {line.id}")
            try:
                verdict = await self._review_line(line, reviewer, dispatch)
            except _ReviewUnavailable:
                line.status = LineStatus.RUNNING
                await self._note_review_unavailable(dispatch, line)
                raise _ReviewRetry from None
            if _result_token(line) != token:
                await self._discard_stale(dispatch)
                continue
            if verdict_unparseable(verdict) or review_strict.is_strict_inconclusive(verdict):
                retry = await self._review_after_unparseable(
                    dispatch,
                    line,
                    verdict,
                    token=token,
                    barred=[line.assigned_model or "", reviewer],
                )
                if retry is _STALE_REVIEW:
                    continue
                if retry is _PARKED_REVIEW:
                    blocked.append(line)
                    continue
                reviewer, verdict = retry
            self._clear_review_attempts(dispatch, line)
            # N51/N59: off/shadow leave the verdict as it is (ungated: no record).
            triage = review_triage.triage_verdict(verdict, subject=line.id)
            verdict = triage.verdict
            if triage.action == review_triage.EVIDENCE_REQUEST:
                line.status = LineStatus.BLOCKED
                line.failure_reason = REVIEW_INCONCLUSIVE
                line.blockers = review_triage.evidence_blockers(
                    list(triage.record.get("needed_evidence") or [])
                )
                blocked.append(line)
            elif verdict.passed:
                line.status = LineStatus.PASSED
                line.blockers = []
            else:
                line.status = LineStatus.BLOCKED
                line.blockers = list(verdict.blockers)
                blocked.append(line)
            self._record_outcome(dispatch, line, reviewer_verdict=verdict.verdict)
        return blocked

    async def _review_line(
        self, line: Line, reviewer: str, dispatch: Dispatch | None = None
    ) -> Verdict:
        """Ask the reviewer. An adapter failure is transient, not ``DispatchError``.

        A missing binary, a lapsed login, a timeout, or a non-zero exit leaves the
        stage where it is. The caller counts ``review_attempts`` and parks with
        ``review_unavailable`` after three. Only ``DispatchError`` is an invariant
        failure. The log records the exception type and that reason code.
        """
        review = self._c.reviewer.review_line
        notes = self._review_notes(line, dispatch, reviewer=reviewer)
        reused, stage, head = self._review_dedup(dispatch, line, notes)
        if reused is not None:
            return reused
        try:
            if notes and _callable_accepts_keyword(review, "notes"):
                verdict = await self._leave(review(line.id, reviewer, notes=notes))
            else:
                verdict = await self._leave(review(line.id, reviewer))
            self._review_noted(dispatch, line, verdict, reviewer, stage, head)
            return verdict
        except DispatchError:
            logger.warning(
                "Reviewer failed %s %s %s",
                line.id,
                "DispatchError",
                "dispatch_error",
            )
            raise DispatchError(
                f"The reviewer {reviewer} could not review line {line.id}"
            ) from None
        except Exception as exc:
            classified = _classify_review_failure(reviewer, exc)
            logger.warning(
                "Reviewer failed %s %s %s %s",
                line.id,
                type(exc).__name__,
                REVIEW_UNAVAILABLE,
                classified.failure_class or "unclassified",
            )
            raise classified from None

    def _review_dedup(
        self, dispatch: Dispatch | None, line: Line, notes: str = ""
    ) -> tuple[Verdict | None, str, str]:
        """P0: ``(reused verdict or None, stage, head key)`` for the review about to run."""
        gates = self._c.gates
        if dispatch is None or gates is None:
            return None, "", ""
        snap = gates.line_snapshot(dispatch, line)
        stage = _load_state(snap).stage.value
        head = p0_hooks.review_head_key(str(snap.get(HEAD_UNDER_REVIEW) or ""), notes, snap)
        reused = p0_hooks.review_reuse(
            self._dedup_book, dispatch, line, stage=stage, head=head
        )
        return reused, stage, head

    def _review_noted(
        self, dispatch: Dispatch | None, line: Line, verdict: Verdict,
        reviewer: str, stage: str, head: str,
    ) -> None:
        """P0: keep the strict-review attempts and a usable verdict for dedup."""
        pop = getattr(self._c.reviewer, "pop_strict_attempts", None)
        attempts = pop(line.id) if callable(pop) else None
        gates = self._c.gates
        if dispatch is None or gates is None:
            return
        p0_hooks.record_strict_attempts(
            gates.line_snapshot(dispatch, line), attempts, now=gates.stamp()
        )
        p0_hooks.review_record(
            self._dedup_book, dispatch, line, verdict,
            stage=stage, head=head, reviewer=reviewer,
        )

    def _strict_inconclusive(
        self, dispatch: Dispatch, line: Line, verdict: Verdict, failed: list[str]
    ) -> bool:
        """P0: after the fallback reviewer also failed. True when enforce parked it.

        Enforce parks ``review_inconclusive`` (never PASS, FAIL or a fix
        round); shadow records that it would have; off does nothing.
        """
        mode = review_strict.strict_mode()
        if mode == "off":
            return False
        gates = self._c.gates
        snap = gates.line_snapshot(dispatch, line) if gates is not None else None
        kind = p0_hooks.last_strict_kind(snap)
        if snap is not None:
            review_strict.record_attempt(
                snap, reviewer=",".join(failed), kind=kind, retried=True,
                outcome="inconclusive" if mode == "enforce" else "would_inconclusive",
                mode=mode, now=gates.stamp(),
            )
        if mode != "enforce":
            return False
        inconclusive = review_strict.inconclusive_verdict(kind, failed)
        line.blockers = list(inconclusive.blockers)
        if gates is None or snap is None:
            self._block_line(dispatch, line, REVIEW_INCONCLUSIVE)
            return True
        state = _load_state(snap)
        snap[REVIEW_PARKED] = {
            "run_id": line.current_run,
            "attempt": line.attempts,
            "author": line.assigned_model or "",
            "stage": state.stage.value,
            "failed": list(dict.fromkeys(failed)),
            "needed_evidence": list(getattr(inconclusive, "needed_evidence", []) or []),
        }
        self._block_line(dispatch, line, REVIEW_INCONCLUSIVE)
        if state.stage is not Stage.BLOCKED and not state.stage.terminal:
            self._advance(dispatch, line, "block", provider=line.assigned_model or "")
        return True

    async def _review_after_unparseable(
        self,
        dispatch: Dispatch,
        line: Line,
        verdict: Verdict,
        *,
        token: tuple[str, str | None, int, str],
        barred: list[str],
    ) -> tuple[str, Verdict] | object:
        """One review by another eligible reviewer after an unusable reply.

        ``CrossReviewer`` already asked this model once more for a verdict
        object, so a reply that is still unparseable is the reviewer's
        format and not the author's work: it must never become a fix round.
        One other eligible reviewer -- ``barred`` keeps it off the author and
        off the reviewer that just failed -- gets one review. When nobody else
        is eligible, or that reply carries no verdict either, the line parks
        ``BLOCKED`` with ``review_unparseable`` and the dispatch waits in
        ``REVIEW`` for a human: ``dispatch unblock`` reviews the same work
        again, ``dispatch reassign`` hands the line to another developer.

        Selecting and reviewing both leave the actor, which keeps applying
        events meanwhile, so ``token`` -- the caller's ``_result_token`` from
        before its own review -- is checked after each before anything is
        committed. A moved line discards the result and returns
        ``_STALE_REVIEW``; a park returns ``_PARKED_REVIEW``; a usable verdict
        returns ``(reviewer, verdict)`` on a line that still matches ``token``.
        ``_ReviewUnavailable`` and ``DispatchError`` keep their usual handling.
        """
        other = await self._select_with_retry(
            dispatch,
            line,
            role=ModelRole.REVIEW,
            exclude=[*barred, *sorted(self._line_holders(dispatch, line))],
        )
        if _result_token(line) != token:
            await self._discard_stale(dispatch)
            return _STALE_REVIEW
        if other is not None:
            try:
                verdict = await self._review_line(line, other, dispatch)
            except _ReviewUnavailable:
                if _result_token(line) != token:
                    await self._discard_stale(dispatch)
                    return _STALE_REVIEW
                line.status = LineStatus.RUNNING
                await self._note_review_unavailable(dispatch, line)
                raise _ReviewRetry from None
            if _result_token(line) != token:
                await self._discard_stale(dispatch)
                return _STALE_REVIEW
            if not verdict_unparseable(verdict) and not review_strict.is_strict_inconclusive(
                verdict
            ):
                return other, verdict
            logger.warning(
                "Reviewer reply for line %s carried no verdict object twice", line.id
            )
        else:
            logger.warning(
                "No other reviewer for line %s after an unparseable reply", line.id
            )
        failed = [name for name in (*barred[1:2], other or "") if name]
        if not self._strict_inconclusive(dispatch, line, verdict, failed):
            self._park_review_unparseable(dispatch, line, verdict, failed)
        await self._park_blocked(dispatch)
        return _PARKED_REVIEW

    def _park_review_unparseable(
        self, dispatch: Dispatch, line: Line, verdict: Verdict, failed: list[str]
    ) -> None:
        """Park ``review_unparseable`` so ``dispatch unblock`` or ``reassign`` can move it (N75).

        Like a ``review_unavailable`` park (N42): the run under review and its
        author are kept as ``review_parked``, and Stage-Gate records a pause
        from the review stage. ``unblock`` resumes that stage for a fresh
        review of the same work; ``reassign`` is a legal swap from the pause.
        Ungated dispatches have no Stage-Gate and only block the line.
        """
        line.blockers = list(verdict.blockers)
        gates = self._c.gates
        if gates is None:
            self._block_line(dispatch, line, REVIEW_UNPARSEABLE)
            return
        snap = gates.line_snapshot(dispatch, line)
        state = _load_state(snap)
        snap[REVIEW_PARKED] = {
            "run_id": line.current_run,
            "attempt": line.attempts,
            "author": line.assigned_model or "",
            "stage": state.stage.value,
            "failed": list(dict.fromkeys(failed)),
        }
        self._block_line(dispatch, line, REVIEW_UNPARSEABLE)
        if state.stage is not Stage.BLOCKED and not state.stage.terminal:
            self._advance(dispatch, line, "block", provider=line.assigned_model or "")

    def _park_review_inconclusive(
        self, dispatch: Dispatch, line: Line, triage: object, reviewer: str
    ) -> None:
        """N59 enforce: park for the evidence the reviewer named; no fix round.

        Stage-Gate gets an operator pause (``block``), never a review BLOCK,
        so no blocking round is counted. ``dispatch unblock`` reviews the same
        run again once the evidence is there; enforce only parks when that
        exit exists (``review_triage.inconclusive_exit_ready``).
        """
        record = getattr(triage, "record", {}) or {}
        needed = [str(item) for item in record.get("needed_evidence") or []]
        line.blockers = review_triage.evidence_blockers(needed)
        gates = self._c.gates
        assert gates is not None
        snap = gates.line_snapshot(dispatch, line)
        state = _load_state(snap)
        snap[REVIEW_PARKED] = {
            "run_id": line.current_run,
            "attempt": line.attempts,
            "author": line.assigned_model or "",
            "stage": state.stage.value,
            "failed": [],
            "reviewer": reviewer,
            "needed_evidence": needed,
        }
        self._block_line(dispatch, line, REVIEW_INCONCLUSIVE)
        if state.stage is not Stage.BLOCKED and not state.stage.terminal:
            self._advance(dispatch, line, "block", provider=line.assigned_model or "")

    async def _note_completion_evidence(self, dispatch: Dispatch, line: Line) -> None:
        """N60: the system's evidence for the HEAD under review. Never an outcome."""
        gates = self._c.gates
        if gates is None or not completion_evidence.enabled():
            return
        try:
            snap = gates.line_snapshot(dispatch, line)
            head = str(snap.get(HEAD_UNDER_REVIEW) or "")
            changed = await self._leave(
                asyncio.to_thread(completion_evidence.changed_files, gates, line, head)
            )
            entry = completion_evidence.verify_completion(
                snap,
                head=head,
                write_scope=line.write_scope,
                changed=changed,
                claim=snap.get(completion_evidence.WORKER_CLAIM),
            )
            completion_evidence.record_evidence(
                dispatch.gates, snap, entry,
                line_id=line.id, branch=line.branch or "", at=gates.stamp(),
            )
        except Exception:  # noqa: BLE001 - a shadow record must never end a drive
            logger.exception("Completion evidence failed for line %s", line.id)

    def _note_worker_claim(self, dispatch: Dispatch, line: Line, launched: object) -> None:
        """N60: keep the worker's advisory claim with the HEAD its run left."""
        gates = self._c.gates
        if gates is None:
            return
        try:
            completion_evidence.note_claim(
                gates.line_snapshot(dispatch, line),
                getattr(launched, "output", ""),
                head=getattr(launched, "head", None),
                start_head=getattr(launched, "start_head", None),
            )
        except Exception:  # noqa: BLE001 - advisory only
            logger.exception("Worker claim not recorded for line %s", line.id)

    async def _gate(self, dispatch: Dispatch) -> None:
        if await self._fence(dispatch):
            return
        await self._enter_phase(dispatch, DispatchState.APPROVAL)
        token = await self._leave(self._c.gate.request_approval(dispatch))
        if await self._fence(dispatch):
            return
        dispatch.approval_token = token
        await self._save(dispatch)

    async def _run_gated(self, dispatch: Dispatch) -> None:
        """Launch and merge in DAG order. G2, review fixes and G5 go through Stage-Gate.

        ``gates is None`` never reaches here, so the dev path stays the one above.
        A fixable G2 failure starts a fix round. The line blocks when that cap
        is used up, or when the failure is not something a code fix can repair.
        """
        gates = self._c.gates
        assert gates is not None
        if not dispatch.gates:
            await gates.prepare_async(dispatch)
            await self._save(dispatch)
        else:
            topological_lines(dispatch.lines)
        await self._adopt_settled_runs(dispatch)
        gates.beat(dispatch)
        await self._sweep_wakeups(dispatch)
        try:
            await self._sweep_liveness(dispatch)
        except Exception:  # noqa: BLE001 - the sweep must never end a drive
            # A malformed liveness cell or a policy bug is logged; the drive
            # goes on exactly as it would without the sweep (N76 rollout).
            logger.exception("Liveness sweep failed for dispatch %s", dispatch.id)
        guard = 0
        fixes = max(gates._settings.max_g2_fixes, 0)
        replans = max(gates._settings.max_replans, 0)
        base = max(len(dispatch.lines), 1)
        limit = base * (8 + fixes) * (replans + 1) + 4
        retries_seen = self._review_retries
        while not self._stopped(dispatch):
            if self._abandoned:
                return
            # A request absorbed inside the last monitor or review pass may
            # relaunch a line, as one absorbed below does.
            limit += self._take_midpass_absorbed() * (8 + fixes)
            guard += 1
            # Each scheduled review retry costs loop steps (the wait, the
            # retry): offset the guard so an outage plus fix rounds is not
            # read as a cycle.
            if self._review_retries > retries_seen:
                limit += 2 * (self._review_retries - retries_seen)
                retries_seen = self._review_retries
            if guard > limit:
                raise DispatchError("The plan's line dependencies are not a DAG")
            gates.beat(dispatch)
            self._reap_line_tasks(dispatch)
            # A reserved fix whose LaunchDone already landed drops its intent
            # here, in the drive, before the monitor treats that run as live.
            if self._clear_confirmed_fixes(dispatch):
                await self._save(dispatch)
            # A human reassign queued while this drive ran becomes an ASSIGNED
            # line here, so the ready-line pick below relaunches it once.
            absorbed = await self._absorb_human_reassigns(dispatch)
            if self._stopped(dispatch) or self._abandoned:
                return
            if absorbed:
                # A queued ``dispatch recover`` is a marker; run its step now.
                await self._safe_operator_sweep(dispatch)
            limit += absorbed * (8 + fixes)
            # N80 L4: app claims and reports on app_claimable lines (full only).
            limit += await self._absorb_app_lines(dispatch) * (8 + fixes)
            # Restored RUNNING / implementing lines converge before any launch.
            if any(self._watching(line) for line in dispatch.lines):
                await self._monitor(dispatch)
                if self._stopped(dispatch) or self._abandoned:
                    return
                await self._apply_g2(dispatch)
                if self._stopped(dispatch) or self._abandoned:
                    return
                await self._enter_phase(dispatch, DispatchState.REVIEW)
                await self._review_gated(dispatch)
                await self._save(dispatch)
                continue
            waits = self._review_waits(dispatch)
            if _completed_running(dispatch, self._runs, skip=set(waits)):
                await self._apply_g2(dispatch)
                if self._stopped(dispatch):
                    return
                await self._enter_phase(dispatch, DispatchState.REVIEW)
                await self._review_gated(dispatch)
                await self._save(dispatch)
                continue
            pending_ids = self._pending_relaunch_ids(dispatch)
            # A line whose task is still launching it is not ready again (N31).
            app_held = app_lines.held_ids(dispatch, now=self._wall())
            ready = [
                line
                for line in _ready_lines(dispatch)
                if line.id not in self._line_tasks and line.id not in app_held
            ]
            # A fix intent is resumed even when pending_relaunch was cleared
            # at reservation. Ordinary ready lines are not a substitute.
            fix_ready = [
                line
                for line in ready
                if self._pending_fix(dispatch, line.id) is not None
            ]
            if fix_ready and not self._nonpending_active(dispatch):
                chosen = _next_serial_line(dispatch, fix_ready)
                if await self._resume_one_fix(dispatch, chosen):
                    return
                continue
            pending = [line for line in ready if line.id in pending_ids]
            # Deferred relaunches go before any new ready line, and only once
            # every other active line has already converged above.
            if pending and not self._nonpending_active(dispatch):
                chosen = _next_serial_line(dispatch, pending)
                # A deferred fix is rebuilt and launched as a fix. An ordinary
                # pending relaunch still uses the line's stored task.
                if self._pending_fix(dispatch, chosen.id) is not None:
                    if await self._resume_one_fix(dispatch, chosen):
                        return
                    continue
                if await self._launch_and_converge(dispatch, chosen):
                    return
                continue
            if ready:
                # v1 runs one ready line at a time, in topological order.
                # A line already in ``implementing`` is resumed before any other.
                # The serial check itself lives in ``_launch_guarded``.
                active_ids = {line.id for line in _serial_active(dispatch, gates)}
                owned = [
                    line
                    for line in ready
                    if line.id in active_ids and line.id not in pending_ids
                ]
                pool = owned or [line for line in ready if line.id not in pending_ids]
                pool = [
                    line
                    for line in pool
                    if self._pending_fix(dispatch, line.id) is None
                ]
                if pool and self._parallel_limit() > 1:
                    batch = self._parallel_batch(dispatch, pool)
                    if batch:
                        await self._enter_phase(dispatch, DispatchState.LAUNCHING)
                        await self._save(dispatch)
                        for line in batch:
                            self._spawn_line_task(dispatch, line)
                        self._note_parallel(dispatch)
                        continue
                    pool_ids = {item.id for item in pool}
                    if not self._line_tasks and all(
                        active.id in pool_ids for active in _serial_active(dispatch, gates)
                    ):
                        # Nothing runs here and the only ready lines wait for a
                        # provider slot another dispatch holds: launch the next
                        # one on the drive, which waits for that slot.
                        chosen = _next_serial_line(dispatch, pool)
                        if await self._launch_and_converge(dispatch, chosen):
                            return
                        continue
                elif pool:
                    chosen = _next_serial_line(dispatch, pool)
                    if await self._launch_and_converge(dispatch, chosen):
                        return
                    continue
            if self._line_tasks:
                # Lines are launching in parallel and nothing else can move:
                # wait for the first to finish (or the earliest review retry).
                limit += 1
                await self._wait_line_tasks(dispatch, waits)
                continue
            if _ready_for_g5(dispatch):
                if await self._fence(dispatch, claim=True):
                    return
                await self._g5_then_gate(dispatch)
                return
            if waits:
                # Only scheduled review retries are left to move (N42).
                await self._wait_for_review_retry(dispatch, waits)
                continue
            if app_held:
                # N80 L4: only lines an app holds (or may claim) are left.
                limit += 1
                await self._leave(self._sleep(app_lines.POLL_SECONDS))
                continue
            queued = [
                line
                for line in dispatch.lines
                if line.status is LineStatus.ASSIGNED
                and line.failure_reason == REVIEW_BACKLOG
            ]
            if queued:
                # Review admission held these lines back; nothing else can
                # move. Pause once: if the backlog reopened, release them and
                # carry on. A still-saturated queue is a recoverable park, not
                # a cycle: clearing the reason and re-entering the ready pool
                # would spin until the loop guard raised a bogus DAG error.
                if await self._wait_queued_admission(dispatch):
                    continue
                if self._stopped(dispatch) or self._abandoned:
                    return
                await self._park_review_backlog(dispatch, queued)
                return
            if any(line.status is LineStatus.BLOCKED for line in dispatch.lines):
                await self._park_blocked(dispatch)
                return
            exhausted = [
                line
                for line in dispatch.lines
                if line.failure_reason in _PARKED_REASONS
                and line.status is not LineStatus.BLOCKED
            ]
            if exhausted:
                for line in exhausted:
                    self._block_line(
                        dispatch,
                        line,
                        line.failure_reason or REPLAN_EXHAUSTED,
                    )
                await self._park_blocked(dispatch)
                return
            if any(
                line.status in (LineStatus.ASSIGNED, LineStatus.PENDING)
                for line in dispatch.lines
            ):
                raise DispatchError("The plan's line dependencies are not a DAG")
            return

    # -- provider skips (N25a) -----------------------------------------------

    def _provider_skips(self, dispatch: Dispatch) -> dict[str, object]:
        if not isinstance(dispatch.gates, dict):
            dispatch.gates = {}
        skips = dispatch.gates.get(PROVIDER_SKIPS)
        if not isinstance(skips, dict):
            skips = {}
            dispatch.gates[PROVIDER_SKIPS] = skips
        return skips

    def _skipped_providers(self, dispatch: Dispatch) -> list[str]:
        """Providers skipped for new launches right now (reset not reached)."""
        root = dispatch.gates if isinstance(dispatch.gates, dict) else None
        skips = root.get(PROVIDER_SKIPS) if isinstance(root, dict) else None
        if not isinstance(skips, dict):
            return []
        now = self._wall()
        found: list[str] = []
        for provider, entry in skips.items():
            if not isinstance(entry, dict):
                continue
            until = entry.get("until")
            if isinstance(until, (int, float)) and not isinstance(until, bool) and until > now:
                found.append(str(provider))
        return found

    def _note_reviewer_skip(
        self, dispatch: Dispatch, reviewer: str, failure: _ReviewUnavailable
    ) -> None:
        """A review that failed for quota or a rate limit skips that reviewer (N63)."""
        try:
            failure_class = FailureClass(failure.failure_class)
        except ValueError:
            return
        self._note_provider_skip(
            dispatch,
            reviewer,
            SimpleNamespace(
                failure_class=failure_class,
                reason_code=failure.reason_code,
                retry_after_seconds=failure.retry_after,
            ),
        )

    async def _review_quota_blockers(
        self, dispatch: Dispatch, line: Line, exclude: list[str]
    ) -> dict[str, dict[str, object]]:
        """Reviewers held back by quota: skipped providers and spent plans (N63).

        Each entry names why and until when, for the park message. Empty when
        nothing quota-related keeps a reviewer away.
        """
        blockers: dict[str, dict[str, object]] = {}
        lowered = {name.lower() for name in exclude if name}
        root = dispatch.gates if isinstance(dispatch.gates, dict) else {}
        skips = root.get(PROVIDER_SKIPS) if isinstance(root, dict) else None
        roster = _review_roster(self._c.selector)
        now = self._wall()
        if isinstance(skips, dict):
            for name, entry in skips.items():
                provider = str(name).lower()
                if provider in lowered or not isinstance(entry, dict):
                    continue
                if roster is not None and provider not in roster:
                    continue
                until = entry.get("until")
                if isinstance(until, (int, float)) and not isinstance(until, bool) and until > now:
                    blockers[provider] = {
                        "reason": str(entry.get("reason") or "skipped"),
                        "resets_at": datetime.fromtimestamp(float(until), timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    }
        review_blockers = getattr(self._c.selector, "review_blockers", None)
        if callable(review_blockers):
            try:
                found = await self._leave(
                    review_blockers(line, [], exclude=tuple(sorted(lowered)))
                )
            except Exception as exc:  # the park message is a courtesy
                logger.warning("Could not read reviewer headroom: %s", type(exc).__name__)
                found = {}
            for name, entry in (found or {}).items():
                if not isinstance(entry, dict):
                    continue
                key = str(name).lower()
                if key not in blockers:
                    blockers[key] = dict(entry)
                    continue
                # Skipped and out of plan: the later of the two is when it is
                # back (a weekly reset, not a 30-minute backoff).
                later = _later_iso(blockers[key].get("resets_at"), entry.get("resets_at"))
                if later != blockers[key].get("resets_at"):
                    blockers[key] = {**dict(entry), "resets_at": later}
        return blockers

    def _park_reviewers_quota_exhausted(
        self, dispatch: Dispatch, line: Line, blockers: dict[str, dict[str, object]]
    ) -> None:
        """No reviewer may take the line: park it naming each provider and its reset."""
        gates = self._c.gates
        assert gates is not None
        snap = gates.line_snapshot(dispatch, line)
        state = _load_state(snap)
        parts = [
            f"{name}: {entry.get('reason') or 'no headroom'} until "
            f"{entry.get('resets_at') or 'unknown'}"
            for name, entry in sorted(blockers.items())
        ]
        snap[REVIEW_PARKED] = {
            "run_id": line.current_run,
            "attempt": line.attempts,
            "author": line.assigned_model or "",
            "stage": state.stage.value,
            "failed": sorted(blockers),
        }
        snap[REVIEWERS_QUOTA_EXHAUSTED] = {name: dict(entry) for name, entry in blockers.items()}
        line.blockers = [
            "No reviewer has quota left for this line: " + "; ".join(parts)
            + f". When one is back, run `conductor dispatch unblock {dispatch.id} {line.id}`."
        ]
        self._block_line(dispatch, line, REVIEWERS_QUOTA_EXHAUSTED)
        if state.stage is not Stage.BLOCKED and not state.stage.terminal:
            self._advance(dispatch, line, "block", provider=line.assigned_model or "")

    def _note_reviewer_fallback(
        self,
        dispatch: Dispatch,
        line: Line,
        stage: str,
        previous: str,
        reviewer: str,
        passed_over: list[str],
    ) -> None:
        """A review went to another reviewer than the first pick (N63): audit and status."""
        gates = self._c.gates
        if gates is None:
            return
        entry = {
            "kind": "reviewer_fallback",
            "line_id": line.id,
            "stage": stage,
            "from": previous,
            "reviewer": reviewer,
            "passed_over": sorted({name for name in passed_over if name}),
            "at": gates.stamp(),
        }
        audit = gates._scheduler(dispatch).setdefault("audit", [])
        if isinstance(audit, list):
            audit.append(entry)
        gates.line_snapshot(dispatch, line)["review_fallback"] = dict(entry)
        logger.info(
            "Line %s %s: reviewer %s instead of %s", line.id, stage, reviewer, previous
        )

    def _note_provider_skip(
        self, dispatch: Dispatch, author: str, failure: WorkerLaunchFailure
    ) -> None:
        """A rate limit or a spent quota: skip the provider for new launches (N25a).

        Until its reset (``retry_after``) or a backoff when the reset is not
        known. The failing line keeps its own retry policy; only lines that
        have not run yet move to another developer.
        """
        name = failure.failure_class.value
        reason_code = failure.reason_code or ""
        if reason_code == ZCODE_PLAN_QUOTA_EXHAUSTED:
            reason = reason_code
        elif name in (FailureClass.RATE_LIMITED.value, FailureClass.QUOTA_EXHAUSTED.value):
            reason = name
        else:
            return
        provider = (author or "").strip().lower()
        if not provider:
            return
        now = self._wall()
        retry_after = failure.retry_after_seconds
        if retry_after is not None and retry_after >= 0:
            until = now + min(float(retry_after), _PROVIDER_SKIP_MAX_SECONDS)
            source = "retry_after"
        else:
            backoff = (
                _RATE_LIMIT_SKIP_SECONDS
                if reason == FailureClass.RATE_LIMITED.value
                else _QUOTA_SKIP_SECONDS
            )
            until = now + backoff
            source = "backoff"
        gates = self._c.gates
        skips = self._provider_skips(dispatch)
        current = skips.get(provider)
        held = current.get("until") if isinstance(current, dict) else None
        if isinstance(held, (int, float)) and not isinstance(held, bool) and held >= until:
            # A later failure never shortens an active skip.
            return
        skips[provider] = {
            "reason": reason,
            "until": until,
            "source": source,
            "at": gates.stamp() if gates is not None else datetime.now(timezone.utc).isoformat(),
        }

    async def _move_off_limited_provider(
        self,
        dispatch: Dispatch,
        line: Line,
        failure: WorkerLaunchFailure,
        *,
        author: str,
        reason: str,
    ) -> bool:
        """A line whose provider is rate limited or out of quota moves on (N25(a)).

        When another developer is eligible (not skipped, never a holder of the
        line), the worker's exit is recorded as a checkpoint and the line is
        reassigned through the same Stage-Gate path as a human reassign: it
        keeps its branch and its work, any uncommitted edits stay in the
        worktree and their diff goes into the new model's first prompt. With
        nobody eligible the caller's retry policy applies (the same provider
        after its backoff). True when the line moved.
        """
        if human_pin.blocks_automatic_move(dispatch, line):
            return False  # N86: a human reassign sticks
        gates = self._c.gates
        assert gates is not None
        replacement = await self._select_model(
            dispatch,
            line,
            self._selection_used(dispatch, line, [author]),
            role=ModelRole.WORK,
            never=[
                *self._skipped_providers(dispatch),
                author,
                *sorted(self._line_holders(dispatch, line)),
            ],
        )
        # M2c: the pack is built now, before anything changes. Building it
        # waits outside the writer, so nothing may wait between the reassign
        # below and its save; the run check below runs after this wait.
        pack = (
            await self._quota_handoff_pack(dispatch, line, author, replacement, failure.failure_class)
            if replacement
            else None
        )
        if not replacement or line.current_run != failure.run_id:
            return False
        # Check the whole move on a copy first: a refusal leaves the line as
        # it is, for the ordinary retry policy.
        probe = copy.deepcopy(dispatch)
        probe_line = probe.line(line.id)
        assert probe_line is not None
        if self._park_exited_for_move(probe, probe_line, failure, reason) or (
            await apply_human_reassign(probe, probe_line, replacement, None, gates)
        ):
            return False
        self._park_exited_for_move(dispatch, line, failure, reason)
        refused = await apply_human_reassign(dispatch, line, replacement, None, gates)
        if refused:
            logger.warning("Could not move %s off %s: %s", line.id, author, refused)
            return False
        entry = self._recovery_line(dispatch, line)
        # The worktree is kept as it is: the new model may start on it.
        entry["allow_dirty_worktree"] = True
        briefs = dispatch.gates.get(REASSIGN_BRIEFS) if isinstance(dispatch.gates, dict) else None
        brief = briefs.get(line.id) if isinstance(briefs, dict) else None
        if isinstance(brief, dict):
            brief["uncommitted"] = True
        # M2c: record the pack built above, under the writer, in this save.
        if pack is not None:
            gates.line_snapshot(dispatch, line)[handoff_pack.GATE_KEY] = pack
            if pack.get("injected") and isinstance(brief, dict):
                brief[handoff_pack.BRIEF_KEY] = pack.get("path")
        skip = self._provider_skips(dispatch).get((author or "").lower())
        audit = gates._scheduler(dispatch).setdefault("audit", [])
        if isinstance(audit, list):
            audit.append(
                {
                    "kind": "provider_skipped",
                    "line_id": line.id,
                    "provider": (author or "").lower(),
                    "replacement": replacement,
                    "reason": skip.get("reason") if isinstance(skip, dict) else reason,
                    "until": skip.get("until") if isinstance(skip, dict) else None,
                    "moved": True,
                    "at": gates.stamp(),
                }
            )
        logger.info("Line %s moved off %s (%s) to %s", line.id, author, reason, replacement)
        await self._save_locked(dispatch)
        return True

    async def _quota_handoff_pack(
        self,
        dispatch: Dispatch,
        line: Line,
        author: str,
        replacement: str,
        failure_class: object,
    ) -> dict[str, object] | None:
        """M2c: build and save the handoff pack of a quota move; its gate record.

        Off, or a failure that is not ``quota_exhausted``, gives None and
        builds nothing. Nothing on the dispatch changes here: the caller
        records the result in the same save as the move. The replacement was
        already chosen by ``_select_model``; the pack only goes along.
        """
        mode = handoff_pack.quota_handoff_mode()
        gates = self._c.gates
        if mode == handoff_pack.OFF or gates is None:
            return None
        if getattr(failure_class, "value", failure_class) != FailureClass.QUOTA_EXHAUSTED.value:
            return None
        old = (author or "").lower()
        entry = dict(self._recovery_line(dispatch, line))
        key = f"dispatch:{dispatch.id}:line:{line.id}:author:{old}"
        facts = handoff_pack.LineFacts(
            line_id=line.id,
            task=line.task,
            predecessor=old,
            branch=line.branch or "",
            base_commit=line.base_commit or "",
            open_review=tuple(str(item) for item in line.blockers),
            park_reason=str(line.failure_reason or ""),
            reason="quota_exhausted",
        )
        port = self._c.handoff_pack_port or handoff_pack.default_port()
        runs_dir = Path(gates._settings.runs_dir)
        # The move adds one attempt; the pack is named for the new one.
        attempt, new = line.attempts + 1, (replacement or "").lower()
        locate = getattr(self._c.launcher, "line_worktree", None)
        subject = copy.deepcopy(line)

        def build() -> dict[str, object]:
            try:
                manager = self._session_manager()
            except Exception:  # noqa: BLE001 - the pack is context only
                manager = None
            try:
                worktree = locate(subject) if callable(locate) else None
            except Exception:  # noqa: BLE001 - the pack is context only
                worktree = None
            sessions = handoff_pack.sessions_for(entry, manager, key, old)
            return handoff_pack.prepare(
                port, runs_dir, dispatch.id, attempt, facts, sessions,
                mode=mode, for_agent=new, cwd=worktree,
            )

        return await self._leave(asyncio.to_thread(build))

    def _park_exited_for_move(
        self, dispatch: Dispatch, line: Line, failure: WorkerLaunchFailure, reason: str
    ) -> str | None:
        """The worker exited: a ``worker_exited`` checkpoint and the Stage-Gate pause."""
        gates = self._c.gates
        assert gates is not None
        line.status = LineStatus.BLOCKED
        line.failure_reason = reason
        line.current_run = None
        record_worker_exited(
            dispatch,
            line,
            gates.line_snapshot(dispatch, line),
            run_id=failure.run_id,
            failure_class=failure.failure_class.value,
            exit_code=getattr(failure, "exit_code", None),
            stamp=gates.stamp(),
        )
        return None

    async def _skip_limited_provider(self, dispatch: Dispatch, line: Line) -> None:
        """Before a line's first run: a skipped provider hands it to the next developer.

        Only a line with no run on its branch, not a fix and not a human pick
        (a reassign brief) moves; its branch and Stage-Gate stage stay, the
        new model becomes the author of record. With nobody eligible the line
        keeps its model and the launch's own retry policy applies.
        """
        provider = (line.assigned_model or "").strip().lower()
        if not provider or provider not in self._skipped_providers(dispatch):
            return
        if self._branch_runs(dispatch, line) or reassign_brief(dispatch, line) is not None:
            return
        gates = self._c.gates
        assert gates is not None
        state = _load_state(gates.line_snapshot(dispatch, line))
        if state.stage not in (Stage.DAG_READY, Stage.ASSIGNED, Stage.IMPLEMENTING):
            return
        used = self._selection_used(dispatch, line, [line.assigned_model or ""])
        replacement = await self._select_model(
            dispatch,
            line,
            used,
            role=ModelRole.WORK,
            never=[
                *self._skipped_providers(dispatch),
                line.assigned_model or "",
                *sorted(self._line_holders(dispatch, line)),
            ],
        )
        entry = self._provider_skips(dispatch).get(provider)
        skip = entry if isinstance(entry, dict) else {}
        audit = gates._scheduler(dispatch).setdefault("audit", [])
        if isinstance(audit, list):
            audit.append(
                {
                    "kind": "provider_skipped",
                    "line_id": line.id,
                    "provider": provider,
                    "replacement": replacement or "",
                    "reason": skip.get("reason"),
                    "until": skip.get("until"),
                    "at": gates.stamp(),
                }
            )
        if replacement:
            logger.info(
                "Line %s: %s is skipped (%s); launching with %s",
                line.id,
                provider,
                skip.get("reason"),
                replacement,
            )
            line.assigned_model = replacement
            self._install_worker(dispatch, line, replacement)
        await self._save(dispatch)

    # -- parallel lines (N31) ------------------------------------------------

    def _parallel_limit(self) -> int:
        try:
            return max(1, int(self._config.max_parallel_lines))
        except (TypeError, ValueError):
            return 1

    def _provider_capacity(self, provider: str) -> int:
        capacity_for = getattr(self._c.launcher, "capacity_for", None)
        if callable(capacity_for):
            try:
                value = int(capacity_for(provider))
            except (TypeError, ValueError):
                value = 1
            return max(1, value)
        return 1

    def _provider_free(self, line: Line) -> bool:
        """True when the launcher would start this line's provider without waiting."""
        if getattr(self._c.launcher, "capacity_for", None) is None:
            return True
        from .execution import provider_slots_free

        provider = (line.assigned_model or "").strip().lower()
        return not provider or provider_slots_free(
            provider, capacity=self._provider_capacity(provider)
        )

    def _line_conflicts(self, line: Line, others: list[Line]) -> list[Line]:
        """Lines among ``others`` that ``line`` may not run beside.

        The same provider beyond its capacity, or write scopes that overlap.
        """
        provider = (line.assigned_model or "").strip().lower()
        same = [
            other
            for other in others
            if (other.assigned_model or "").strip().lower() == provider
        ]
        conflicts: list[Line] = []
        if provider and len(same) >= self._provider_capacity(provider):
            conflicts.extend(same)
        for other in others:
            if other in conflicts:
                continue
            if _scopes_overlap(line.write_scope, other.write_scope):
                conflicts.append(other)
        return conflicts

    def _parallel_batch(self, dispatch: Dispatch, pool: list[Line]) -> list[Line]:
        """Ready lines that may start now beside every active and chosen line."""
        active = [
            line
            for line in _serial_active(dispatch, self._c.gates)
            if line.id not in {item.id for item in pool}
        ]
        for line_id in self._line_tasks:
            other = dispatch.line(line_id)
            if other is not None and other not in active:
                active.append(other)
        room = self._parallel_limit() - len(active)
        chosen: list[Line] = []
        for line in topological_lines(dispatch.lines):
            if room <= 0:
                break
            if line not in pool:
                continue
            if self._line_conflicts(line, [*active, *chosen]):
                continue
            if not self._provider_free(line):
                # Another dispatch holds every slot of this provider: a line
                # task would only sit on its reservation waiting for one.
                continue
            chosen.append(line)
            room -= 1
        return chosen

    def _spawn_line_task(self, dispatch: Dispatch, line: Line) -> None:
        """Run one ordinary launch of ``line`` beside the drive.

        The task is the writer only while it holds ``_writer``; every external
        wait inside ``_launch_line`` hands it back.
        """

        async def body() -> None:
            await self._take_writer()
            try:
                await self._launch_line(dispatch, line)
            finally:
                if self._writer_owner is asyncio.current_task():
                    self._give_writer()

        task = asyncio.create_task(body(), name=f"dispatch-line-{line.id}")
        self._line_tasks[line.id] = task
        self._inflight.append(task)

    def _reap_line_tasks(self, dispatch: Dispatch) -> None:
        """Forget finished line tasks; a failure in one is the drive's failure."""
        failure: BaseException | None = None
        for line_id, task in list(self._line_tasks.items()):
            if not task.done():
                continue
            self._line_tasks.pop(line_id, None)
            if task in self._inflight:
                self._inflight.remove(task)
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None and failure is None:
                failure = exc
        if self._c.gates is not None and (failure is None or self._line_tasks):
            self._note_parallel(dispatch)
        if failure is not None:
            raise failure

    async def _wait_line_tasks(self, dispatch: Dispatch, waits: dict[str, float]) -> None:
        tasks: set[asyncio.Task[Any]] = set(self._line_tasks.values())
        sleeper: asyncio.Task[Any] | None = None
        if waits:
            sleeper = asyncio.create_task(self._sleep(min(waits.values())))
            tasks.add(sleeper)
        try:
            await self._leave(asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED))
        finally:
            if sleeper is not None and not sleeper.done():
                sleeper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sleeper
        if sleeper is not None and sleeper.done() and not sleeper.cancelled():
            await self._mark_review_retries_due(dispatch, waits, min(waits.values()))

    async def _stop_line_tasks(self) -> None:
        """At the end of a drive no line task may outlive it."""
        if not self._line_tasks:
            return
        tasks = list(self._line_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        await self._leave(asyncio.gather(*tasks, return_exceptions=True))
        for task in tasks:
            if task in self._inflight:
                self._inflight.remove(task)
        self._line_tasks.clear()

    def _note_parallel(self, dispatch: Dispatch) -> None:
        gates = self._c.gates
        if gates is None or self._parallel_limit() <= 1:
            return
        active = sorted(
            {*self._line_tasks, *(line.id for line in _serial_active(dispatch, gates))}
        )
        gates._scheduler(dispatch)["parallel"] = {
            "limit": self._parallel_limit(),
            "active": active,
        }

    async def _resume_one_fix(self, dispatch: Dispatch, chosen: Line) -> bool:
        """Resume one pending fix. True when this drive should stop.

        A stale intent is cleared in one save and the line stays as it is.
        The caller then continues from that current assignment.
        """
        intent = self._pending_fix(dispatch, chosen.id)
        if intent is not None and self._fix_intent_stale(dispatch, chosen, intent):
            await self._drop_stale_fix(dispatch, chosen)
            return self._stopped(dispatch) or self._abandoned
        self._stale_fix_dropped = False
        await self._enter_phase(dispatch, DispatchState.LAUNCHING)
        await self._save(dispatch)
        ok = await self._launch_fix(
            dispatch, chosen, list(chosen.blockers), resume=True
        )
        if self._stale_fix_dropped:
            return self._stopped(dispatch) or self._abandoned
        if not ok or self._stopped(dispatch):
            if not self._stopped(dispatch):
                await self._park_blocked(dispatch)
            # A line that needs a decision does not end the drive: other
            # independent ready lines still run, and the loop parks once
            # nothing else can move.
            return self._stopped(dispatch) or self._abandoned
        return await self._converge_launched(dispatch)

    def _fix_intent_stale(
        self, dispatch: Dispatch, line: Line, intent: dict[str, object]
    ) -> bool:
        """True when the intent disagrees with the current line.

        Author, attempt, epoch, and source must all match. A missing epoch is
        a mismatch. The caller clears the intent and does not copy it onto
        the line.
        """
        author = intent.get("author")
        if not isinstance(author, str) or author != (line.assigned_model or ""):
            return True
        attempt = intent.get("attempt")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt != line.attempts:
            return True
        epoch = intent.get("epoch")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch != drive_epoch(dispatch):
            return True
        return not self._pending_fix_current(dispatch, line, intent)

    async def _drop_stale_fix(self, dispatch: Dispatch, line: Line) -> None:
        """Clear one stale intent and record why, without touching the assignment."""
        self._clear_pending_fix(dispatch, line.id)
        self._clear_pending_relaunch(dispatch, line)
        record_stale_fix_intent(dispatch)
        await self._save(dispatch)

    async def _launch_ready(self, dispatch: Dispatch, lines: list[Line]) -> None:
        await self._enter_phase(dispatch, DispatchState.LAUNCHING)
        await self._save(dispatch)
        for line in lines:
            if line.status is not LineStatus.ASSIGNED:
                continue
            await self._launch_line(dispatch, line)
            if self._stopped(dispatch):
                return

    async def _launch_and_converge(self, dispatch: Dispatch, chosen: Line) -> bool:
        """Launch one line, then monitor, G2, and review. True when the drive stops."""
        await self._launch_ready(dispatch, [chosen])
        return await self._converge_launched(dispatch)

    async def _converge_launched(self, dispatch: Dispatch) -> bool:
        """Monitor, G2, and review after a launch. True when the drive stops."""
        if self._stopped(dispatch):
            return True
        await self._monitor(dispatch)
        if self._stopped(dispatch) or self._abandoned:
            return True
        await self._apply_g2(dispatch)
        if self._stopped(dispatch) or self._abandoned:
            return True
        await self._enter_phase(dispatch, DispatchState.REVIEW)
        await self._review_gated(dispatch)
        await self._save(dispatch)
        return False

    async def _note_g2_on_writer(
        self,
        dispatch: Dispatch,
        line: Line,
        attempt: int,
        revision: str | None,
    ) -> None:
        """G2 records the worktree HEAD on the writer, not on the command worker."""
        if not self._on_actor():
            await self._submit(self._note_g2_on_writer, dispatch, line, attempt, revision)
            return
        gates = self._c.gates
        assert gates is not None
        gates._note_g2_target(dispatch, line, attempt, revision)
        # The worktree HEAD G2 actually reads also counts as submitted.
        self._note_reviewed_head(dispatch, line, revision)

    async def _finish_g2_on_writer(
        self,
        dispatch: Dispatch,
        line: Line,
        result: str,
        reason: str,
        recorded: list[dict[str, object]],
        attempt: int,
        revision: str | None,
    ) -> object:
        """Apply the immutable G2 result on the writer."""
        if not self._on_actor():
            return await self._submit(
                self._finish_g2_on_writer,
                dispatch,
                line,
                result,
                reason,
                recorded,
                attempt,
                revision,
            )
        gates = self._c.gates
        assert gates is not None
        # Every G2 settle path ends here, including a skipped G2 that never
        # read a worktree: the revision it settled on counts as submitted.
        self._note_reviewed_head(dispatch, line, revision)
        copied = [dict(item) for item in recorded]
        return gates._finish_g2(
            dispatch,
            line,
            result=result,
            reason=reason,
            recorded=copied,
            attempt=attempt,
            revision=revision,
        )

    async def _finish_g5_on_writer(
        self,
        dispatch: Dispatch,
        full: dict[str, object],
        alembic: dict[str, object],
        reason: str,
        check: str,
    ) -> object:
        """Apply the immutable G5 result on the writer."""
        if not self._on_actor():
            return await self._submit(
                self._finish_g5_on_writer,
                dispatch,
                full,
                alembic,
                reason,
                check,
            )
        gates = self._c.gates
        assert gates is not None
        return gates._finish_g5(
            dispatch,
            full=copy.deepcopy(full),
            alembic=copy.deepcopy(alembic),
            reason=reason,
            check=check,
        )

    async def _apply_g2(self, dispatch: Dispatch) -> None:
        gates = self._c.gates
        assert gates is not None
        ran = False
        for line in list(dispatch.lines):
            if line.status is not LineStatus.RUNNING:
                continue
            run = self._runs.get(line.id)
            if run is None or not run.complete:
                continue
            if await gates.settled_async(dispatch, line.id):
                continue
            ran = True
            # The HEAD this run left enters G2 now.
            self._note_reviewed_head(dispatch, line, self._latest_run_head(dispatch, line))
            self._ensure_submitted(dispatch, line)
            # The in-progress row is saved before the command. run_g2 saves
            # again after it has read the worktree HEAD, still before argv.
            await gates.mark_g2_running_async(dispatch, line)
            await self._save(dispatch)

            async def _persist() -> None:
                await self._save(dispatch)

            token = _result_token(line)

            async def _note(attempt: int, revision: str | None) -> None:
                await self._note_g2_on_writer(dispatch, line, attempt, revision)

            async def _finish(
                result: str,
                reason: str,
                recorded: list[dict[str, object]],
                attempt: int,
                revision: str | None,
            ) -> object:
                return await self._finish_g2_on_writer(
                    dispatch, line, result, reason, recorded, attempt, revision
                )

            step = await self._leave(
                gates.run_g2(
                    dispatch,
                    line,
                    persist=_persist,
                    note_target=_note,
                    finish=_finish,
                )
            )
            if _result_token(line) != token:
                await self._discard_stale(dispatch)
                continue
            await self._after_g2(dispatch, line, step)
        if ran:
            await self._save(dispatch)

    async def _after_g2(self, dispatch: Dispatch, line: Line, step) -> None:
        """Record the G2 outcome, then fix, review, or block."""
        gates = self._c.gates
        assert gates is not None
        snap = gates.line_snapshot(dispatch, line)
        state = _load_state(snap)
        handoff_ref = await self._prepare_g2_handoff(dispatch, line) if step.passed else ""
        if state.stage is Stage.FIXING:
            if step.passed:
                self._advance(dispatch, line, "submit_work")
                landed = _load_state(gates.line_snapshot(dispatch, line))
                if landed.stage is Stage.FOCUSED_TESTS:
                    self._advance(
                        dispatch,
                        line,
                        "gate_result",
                        gate="G2",
                        result=GateResult(outcome="pass", reason=step.reason),
                        handoff_ref=handoff_ref,
                    )
                return
            await self._g2_failed(dispatch, line, step)
            return
        if state.stage is Stage.FOCUSED_TESTS:
            outcome = "pass" if step.passed else "fail"
            self._advance(
                dispatch,
                line,
                "gate_result",
                gate="G2",
                result=GateResult(outcome=outcome, reason=step.reason),
                **({"handoff_ref": handoff_ref} if handoff_ref else {}),
            )
        if step.passed:
            return
        await self._g2_failed(dispatch, line, step)

    async def _prepare_g2_handoff(self, dispatch: Dispatch, line: Line) -> str:
        """Persist available branch/G2 facts and return a complete handoff ref."""
        gates = self._c.gates
        assert gates is not None
        facts = await gates.handoff_facts_async(
            dispatch, line, limitations="none"
        )
        files = facts.get("files") or ()
        handoff = Handoff(
            branch=str(facts.get("branch") or ""),
            commit=str(facts.get("commit") or ""),
            files=tuple(files) if isinstance(files, (tuple, list)) else (),
            test_command=str(facts.get("test_command") or ""),
            results=str(facts.get("results") or ""),
            known_limitations=str(facts.get("known_limitations") or "none"),
        )
        gates.record_handoff_view(dispatch, line, facts)
        snap = gates.line_snapshot(dispatch, line)
        if not handoff.complete:
            return ""
        reference = handoff.reference()
        snap["handoff_ref"] = reference
        return reference

    async def _g2_failed(self, dispatch: Dispatch, line: Line, step) -> None:
        if step.reason in _IMMEDIATE_G2:
            self._block_line(dispatch, line, step.reason)
            return
        gates = self._c.gates
        assert gates is not None
        snap = gates.line_snapshot(dispatch, line)
        g2 = _g2_cell(snap)
        launched = int(g2.get("fix_launches") or 0)
        if launched >= gates._settings.max_g2_fixes:
            self._block_line(dispatch, line, FOCUSED_TESTS_EXHAUSTED)
            return
        if await self._debug_switch(dispatch, line, debug_switch.G2_FAILED, launched):
            return
        g2["fix_launches"] = launched + 1
        await self._launch_fix(dispatch, line, (), kind="g2")

    async def _review_gated(self, dispatch: Dispatch) -> None:
        """Review lines whose G2 allows it. A BLOCK starts a fix, not a park."""
        gates = self._c.gates
        assert gates is not None
        for line in list(dispatch.lines):
            if line.status is not LineStatus.RUNNING:
                continue
            if not await gates.allows_review_async(dispatch, line.id):
                continue
            if line.id in self._review_waits(dispatch):
                continue
            snap = gates.line_snapshot(dispatch, line)
            state = _load_state(snap)
            if state.stage in (Stage.SECONDARY_REVIEW, Stage.PRIMARY_REVIEW):
                if ruling_enforced():
                    # Opt-in (CONDUCTOR_DISPATCH_RULING_ENFORCE=1): a fix report
                    # that does not confirm every ruling item is a BLOCK before
                    # any reviewer sees it.
                    if await self._reject_unconfirmed_ruling(dispatch, line):
                        continue
                else:
                    # Default: record it and let the real reviewer judge it.
                    self._note_unconfirmed_ruling(dispatch, line)
            if state.stage is Stage.SECONDARY_REVIEW:
                await self._review_once(dispatch, line, secondary=True)
            elif state.stage is Stage.PRIMARY_REVIEW:
                await self._review_once(dispatch, line, secondary=False)
            elif state.stage is Stage.INTEGRATION_CHECKS:
                await self._mark_passed(dispatch, line)
                continue
            else:
                continue
            # One review can take many minutes: apply a request queued
            # meanwhile before the next line's review starts (N77).
            await self._absorb_midpass(dispatch)

    async def _review_once(self, dispatch: Dispatch, line: Line, *, secondary: bool) -> None:
        gates = self._c.gates
        assert gates is not None
        snap = gates.line_snapshot(dispatch, line)
        state = _load_state(snap)
        risk = state.risk_bearing or line.risk_bearing
        await self._note_head_under_review(dispatch, line)
        await self._note_completion_evidence(dispatch, line)
        try:
            if secondary:
                reviewer = await self._pick_secondary(dispatch, line, state)
                kind = "secondary_review"
            else:
                reviewer = await self._pick_primary(dispatch, line, state)
                kind = "primary_review"
        except _NoEligibleReviewer as exc:
            self._park_no_eligible_reviewer(dispatch, line, exc.holders)
            await self._park_blocked(dispatch)
            return
        except _ReviewersSkipped as exc:
            # A rate-limited reviewer is a wait, never an ERROR: review again
            # once the earliest skip expires.
            await self._wait_for_skipped_reviewers(dispatch, line, exc.until)
            return
        except _ReviewersQuotaExhausted as exc:
            self._park_reviewers_quota_exhausted(dispatch, line, exc.blockers)
            await self._park_blocked(dispatch)
            return
        except _NoSecondaryReviewer:
            if not liveness_modes.should_act(self._liveness_mode()):
                line.failure_reason = NO_SECONDARY_REVIEWER
                raise DispatchError(
                    f"No reviewer model is available for line {line.id}: "
                    f"{NO_SECONDARY_REVIEWER}"
                ) from None
            self._park_no_secondary_reviewer(dispatch, line)
            await self._park_blocked(dispatch)
            return
        if reviewer is None:
            return
        primaries = _primary_reviewers(self._c.selector)
        if (
            not secondary
            and primaries
            and reviewer.lower() not in primaries
            and (snap.get("review_fallback") or {}).get("reviewer") != reviewer
        ):
            out = await self._primaries_out(dispatch, line, primaries)
            if out:
                # The primary reviewer is out (skipped, or no headroom): this
                # review goes to a secondary one. Say so in the audit and
                # status. A primary that only holds the line is no fallback.
                self._note_reviewer_fallback(dispatch, line, kind, out[0], reviewer, out)
        retry = _review_retry(snap)
        if retry is not None:
            # A scheduled retry (N42) is due. The drive may have waited for
            # it: make sure this drive still owns the row before reviewing.
            if await self._fence(dispatch):
                return
            failed = [str(name) for name in retry.get("failed") or []]
            same = retry.get("reviewer")
            if int(retry.get("attempt") or 0) < 2:
                # The first retry asks the reviewer that failed once more.
                if (
                    isinstance(same, str)
                    and same
                    and same not in self._line_holders(dispatch, line)
                    and self._reviewer_available(same)
                    and same.lower() not in self._skipped_providers(dispatch)
                ):
                    reviewer = same
            if int(retry.get("attempt") or 0) >= 2 or reviewer.lower() in self._skipped_providers(dispatch):
                fallback = await self._fallback_reviewer(
                    dispatch, line, failed, secondary=secondary, state=state
                )
                if fallback is not None and fallback != reviewer:
                    self._note_reviewer_fallback(
                        dispatch, line, kind, reviewer, fallback, failed
                    )
                    reviewer = fallback
                    # Another reviewer gives this round's verdict: Stage-Gate
                    # restarts its round count, as for an unparseable reply.
                    gates.line_snapshot(dispatch, line)["reason"] = REVIEWER_CHANGED
        token = _result_token(line)
        try:
            verdict = await self._review_line(line, reviewer, dispatch)
        except _ReviewUnavailable as exc:
            # A spent quota or a rate limit is not a flaky reviewer: skip that
            # provider for the next pick (N63), then retry on the schedule.
            self._note_reviewer_skip(dispatch, reviewer, exc)
            await self._schedule_review_retry(dispatch, line, reviewer)
            return
        gates.line_snapshot(dispatch, line).pop(REVIEW_RETRY, None)
        if _result_token(line) != token:
            await self._discard_stale(dispatch)
            return
        if verdict_unparseable(verdict) or review_strict.is_strict_inconclusive(verdict):
            barred = [line.assigned_model or "", reviewer]
            if secondary and state.primary_reviewer:
                barred.append(state.primary_reviewer)
            retry = await self._review_after_unparseable(
                dispatch, line, verdict, token=token, barred=barred
            )
            if retry is _STALE_REVIEW or retry is _PARKED_REVIEW:
                return
            reviewer, verdict = retry
            # Another reviewer gives this round's verdict. The Stage-Gate
            # restarts its round count for a new provider, as it does when
            # _pick_primary falls back from an unavailable stored reviewer.
            gates.line_snapshot(dispatch, line)["reason"] = REVIEWER_CHANGED
        # N51/N59: a blocker without a repro, or an INCONCLUSIVE verdict. In
        # off/shadow the verdict is unchanged; the record says what enforce does.
        snap_now = gates.line_snapshot(dispatch, line)
        triage = review_triage.triage_verdict(verdict, subject=line.id)
        review_triage.record_triage(
            snap_now,
            triage,
            reviewer=reviewer,
            head=str(snap_now.get(HEAD_UNDER_REVIEW) or ""),
            at=gates.stamp(),
        )
        if triage.action == review_triage.EVIDENCE_REQUEST:
            self._park_review_inconclusive(dispatch, line, triage, reviewer)
            await self._park_blocked(dispatch)
            return
        verdict = triage.verdict
        event = ReviewResult(
            dispatch_id=dispatch.id,
            line_id=line.id,
            run_id=line.current_run,
            attempt=line.attempts,
            passed=verdict.passed,
            blockers=tuple(verdict.blockers),
        )
        await self._apply_review_result(
            event,
            dispatch,
            line,
            secondary=secondary,
            reviewer=reviewer,
            kind=kind,
            risk=risk,
            verdict=verdict,
            state=state,
        )

    async def _apply_review_result(
        self,
        event: ReviewResult,
        dispatch: Dispatch,
        line: Line,
        *,
        secondary: bool,
        reviewer: str,
        kind: str,
        risk: bool,
        verdict: Verdict,
        state: StageGateState,
    ) -> None:
        """Apply one review on the actor, then save. External retries stay outside."""
        if not self._on_actor():
            await self._submit(
                self._apply_review_result,
                event,
                dispatch,
                line,
                secondary=secondary,
                reviewer=reviewer,
                kind=kind,
                risk=risk,
                verdict=verdict,
                state=state,
            )
            return
        if dispatch.id != event.dispatch_id or line.id != event.line_id:
            await self._discard_stale(dispatch)
            return
        if line.current_run != event.run_id or line.attempts != event.attempt:
            await self._discard_stale(dispatch)
            return
        gates = self._c.gates
        assert gates is not None
        self._clear_review_attempts(dispatch, line)
        stage_verdict = _stage_verdict(verdict, risk=risk, secondary=secondary)
        conflict = _conflict_of(verdict) if secondary and not event.passed else ""
        before_rounds = state.blocking_rounds
        transition = self._advance(
            dispatch,
            line,
            kind,
            provider=reviewer,
            verdict=stage_verdict,
            result=GateResult(
                outcome="pass" if verdict.passed else "fail",
                reason=stage_verdict,
            ),
            conflict=conflict,
        )
        if not transition.accepted:
            self._block_line(dispatch, line, transition.reason or REVIEW_BLOCKED)
            return
        landed = transition.state
        reviewed_head = _note_last_review(
            gates.line_snapshot(dispatch, line),
            line,
            verdict,
            reviewer=reviewer,
            # Only a BLOCK that sends the line to a fix round or the Planner
            # is the author's to fix. An adjudicated conflict may be ruled
            # out of scope, so its findings never reach a later author here.
            rejected=landed.stage in (Stage.FIXING, Stage.REPLANNING),
            at=gates.stamp(),
        )
        _remember_review(
            gates.line_snapshot(dispatch, line),
            # The Stage-Gate counts rounds per blocking reviewer: a verdict from
            # anyone else starts the count again, and the history says so.
            round_n=(
                before_rounds
                if state.blocking_reviewer in ("", reviewer)
                else 0
            ),
            gate=kind,
            result=stage_verdict,
            reviewer=reviewer,
            blocker_count=len(verdict.blockers),
            reason=transition.reason,
            at=gates.stamp(),
            blockers=() if verdict.passed else list(verdict.blockers),
        )
        cell_name = "secondary_review" if secondary else "primary_review"
        _review_cell(
            gates.line_snapshot(dispatch, line),
            cell_name,
            result="pass" if verdict.passed else "fail",
            reason=transition.reason,
            reviewer=reviewer,
            at=gates.stamp(),
        )
        if landed.stage is Stage.INTEGRATION_CHECKS:
            line.status = LineStatus.PASSED
            line.blockers = []
            line.failure_reason = None
            snap_now = gates.line_snapshot(dispatch, line)
            snap_now["reason"] = REVIEW_MERGED
            self._record_outcome(dispatch, line, reviewer_verdict=stage_verdict)
            return
        if landed.stage is Stage.SECONDARY_REVIEW:
            snap_now = gates.line_snapshot(dispatch, line)
            snap_now["reason"] = SECONDARY_REQUIRED
            await self._review_once(dispatch, line, secondary=True)
            return
        if landed.stage is Stage.FIXING:
            snap_now = gates.line_snapshot(dispatch, line)
            snap_now["reason"] = REVIEW_BLOCKED
            line.blockers = list(verdict.blockers)
            prior = int(snap_now.get("fix_round") or 0)
            if await self._debug_switch(dispatch, line, debug_switch.REVIEW_BLOCKED, prior):
                return
            await self._launch_fix(dispatch, line, verdict.blockers)
            return
        if landed.stage is Stage.REPLANNING:
            snap_now = gates.line_snapshot(dispatch, line)
            snap_now["reason"] = TWO_FAILED_ROUNDS
            await self._replan(dispatch, line, verdict.blockers, transition.reason)
            return
        if landed.stage is Stage.AWAITING_ADJUDICATION:
            reason = (
                REVIEWER_CONFLICT_SECURITY
                if conflict == "security"
                else REVIEWER_CONFLICT_SCOPE
            )
            self._block_line(dispatch, line, reason)
            # The finding a human rules on with ``dispatch adjudicate`` (N41).
            # Only blockers tagged with this conflict are the finding; every
            # other blocker of the same verdict stays a blocker whatever the
            # ruling. They are split before any cap, so the count is exact.
            # Routing is tag-only (R2 review item 1): a keyword such as "token"
            # must not park an ordinary fix round here; it only exempts the
            # blocker from the N51 repro downgrade (review_triage._exempt).
            kind = "security" if conflict == "security" else "scope"
            every = [str(item) for item in verdict.blockers if str(item).strip()]
            findings = [item for item in every if conflict_tag(item) == kind]
            others = [item for item in every if conflict_tag(item) != kind]
            line.blockers = [item[:_BRIEF_MAX_CHARS] for item in every][:_BRIEF_MAX_BLOCKERS]
            gates.line_snapshot(dispatch, line)[ADJUDICATION] = {
                "conflict": kind,
                "reviewer": reviewer,
                "findings": [item[:_BRIEF_MAX_CHARS] for item in findings][
                    :_BRIEF_MAX_BLOCKERS
                ],
                "other_blockers": [item[:_BRIEF_MAX_CHARS] for item in others][
                    :_BRIEF_MAX_BLOCKERS
                ],
                "other_blocker_count": len(others),
                # The head the conflict was raised on, for an in-scope ruling (N79).
                "head": reviewed_head,
                "at": gates.stamp(),
            }
            return
        self._block_line(dispatch, line, transition.reason or REVIEW_BLOCKED)

    async def _pick_primary(self, dispatch: Dispatch, line: Line, state: StageGateState) -> str | None:
        # A BLOCK records the reviewer on blocking_reviewer and leaves
        # primary_reviewer empty until a passing verdict. Later rounds must
        # send that same provider or the round count restarts. An alternate
        # R-REVIEW row lists that provider under liveness_excluded_reviewers
        # so this pick cannot return it again.
        snap = self._c.gates.line_snapshot(dispatch, line) if self._c.gates else None
        excluded = _excluded_reviewers(snap)
        excluded_keys = {name.casefold() for name in excluded}
        stored = state.primary_reviewer or state.blocking_reviewer
        if stored and stored.casefold() in excluded_keys:
            stored = ""
            if isinstance(snap, dict):
                snap["reason"] = REVIEWER_CHANGED
        if stored and stored in self._line_holders(dispatch, line):
            # A reassign can make an earlier reviewer a holder of the line.
            stored = ""
            if isinstance(snap, dict):
                snap["reason"] = REVIEWER_CHANGED
        if (
            stored
            and self._reviewer_available(stored)
            and await self._reviewer_usable(dispatch, line, stored)
        ):
            return stored
        if stored and isinstance(snap, dict):
            snap["reason"] = REVIEWER_CHANGED
        exclude = [name for name in (line.assigned_model, *excluded) if name]
        return await self._select_reviewer(dispatch, line, exclude=exclude)

    async def _pick_secondary(
        self, dispatch: Dispatch, line: Line, state: StageGateState
    ) -> str | None:
        stored = state.secondary_reviewer
        if (
            not stored
            and state.blocking_reviewer
            and state.blocking_reviewer != state.primary_reviewer
        ):
            stored = state.blocking_reviewer
        snap = self._c.gates.line_snapshot(dispatch, line) if self._c.gates else None
        excluded = _excluded_reviewers(snap)
        barred = {line.assigned_model or "", state.primary_reviewer, *excluded}
        barred |= self._line_holders(dispatch, line)
        barred_keys = {name.casefold() for name in barred if name}
        if (
            stored
            and stored not in barred
            and (
                not liveness_modes.should_act(self._liveness_mode())
                or stored.casefold() not in barred_keys
            )
            and self._reviewer_available(stored)
            and await self._reviewer_usable(dispatch, line, stored)
        ):
            return stored
        exclude = [
            name
            for name in (line.assigned_model, state.primary_reviewer, *excluded)
            if name
        ]
        return await self._select_reviewer(dispatch, line, exclude=exclude)

    async def _select_reviewer(
        self, dispatch: Dispatch, line: Line, *, exclude: list[str]
    ) -> str | None:
        # No model that ever held the line reviews it.
        holders = sorted(self._line_holders(dispatch, line))
        reviewer = await self._select_with_retry(
            dispatch, line, role=ModelRole.REVIEW, exclude=[*exclude, *holders]
        )
        if reviewer is not None:
            return reviewer
        refusal = getattr(self._c.selector, "review_refusal", None)
        blockers = await self._review_quota_blockers(dispatch, line, [*exclude, *holders])
        until = await self._skipped_reviewers_until(dispatch, line, [*exclude, *holders])
        if until is not None and (
            not blockers
            or until - self._wall() <= REVIEW_RETRY_DELAYS[-1]
        ):
            raise _ReviewersSkipped(until)
        if blockers:
            raise _ReviewersQuotaExhausted(blockers)
        if await self._only_holders_could_review(dispatch, line, exclude, holders):
            # Someone could review, but only a former holder: park, not ERROR.
            raise _NoEligibleReviewer(holders)
        if refusal == NO_SECONDARY_REVIEWER:
            line.failure_reason = NO_SECONDARY_REVIEWER
            raise _NoSecondaryReviewer(line.id)
        raise DispatchError(f"No reviewer model is available for line {line.id}")

    def _selection_used(
        self,
        dispatch: Dispatch,
        line: Line,
        exclude: list[str] | None,
        *,
        role: ModelRole = ModelRole.WORK,
    ) -> list[str]:
        """Models the selector must skip: other lines' authors, ``exclude``,
        and workers whose quota is still exhausted.

        A gated review pick does not skip other lines' authors (N63): only the
        holders of this line may not review it (Stage-Gate's rule), and leaving out every
        developer of the dispatch left the failing primary as the only
        reviewer of a risk-bearing line's secondary review.
        """
        used = (
            []
            if role is ModelRole.REVIEW and self._c.gates is not None
            else [
                other.assigned_model
                for other in dispatch.lines
                if other.assigned_model and other is not line
            ]
        )
        used.extend(exclude or [])
        recovery = self._recovery_data(dispatch)
        worker_states = recovery.get("workers")
        wall_now = datetime.now(timezone.utc).timestamp()
        if isinstance(worker_states, dict):
            used.extend(
                provider
                for provider, state in worker_states.items()
                if isinstance(provider, str)
                and isinstance(state, dict)
                and state.get("exhausted") is True
                and not (
                    state.get("retry_source") == "retry_after"
                    and isinstance(state.get("retry_at"), (int, float))
                    and wall_now >= float(state["retry_at"])
                )
            )
        # A provider skipped for new launches (rate limited, out of quota).
        used.extend(self._skipped_providers(dispatch))
        return used

    async def _only_holders_could_review(
        self,
        dispatch: Dispatch,
        line: Line,
        exclude: list[str],
        holders: list[str],
    ) -> bool:
        """One selector call without the holders excluded: no retry sleep, and
        the selector's ``review_refusal`` is left as the real pick set it."""
        if not holders:
            return False
        selector = self._c.selector
        had_refusal = hasattr(selector, "review_refusal")
        refusal = getattr(selector, "review_refusal", None)
        try:
            used = self._selection_used(dispatch, line, exclude, role=ModelRole.REVIEW)
            probe = await self._leave(selector.select(line, used, role=ModelRole.REVIEW))
        finally:
            if had_refusal:
                selector.review_refusal = refusal
        return probe is not None

    def _park_no_eligible_reviewer(
        self, dispatch: Dispatch, line: Line, holders: list[str]
    ) -> None:
        """Block the line with the reason and the one action that resolves it.

        Gated: Stage-Gate is paused from the review stage, so a human reassign
        is accepted from ``blocked`` once another reviewer is available.
        """
        line.blockers = [
            "No eligible reviewer: every available reviewer once held this line "
            f"({', '.join(holders)}). Make a reviewer that never held it available "
            "(role policy or headroom), then run `conductor dispatch reassign "
            f"{dispatch.id} {line.id} <model>`"
        ]
        self._block_line(dispatch, line, NO_ELIGIBLE_REVIEWER)
        gates = self._c.gates
        if gates is not None:
            state = _load_state(gates.line_snapshot(dispatch, line))
            if state.stage is not Stage.BLOCKED:
                self._advance(dispatch, line, "block", provider=line.assigned_model or "")

    def _reviewer_available(self, name: str) -> bool:
        pool = getattr(self._c.selector, "_pool", None)
        if not pool:
            return True
        return name in pool

    async def _launch_fix(
        self,
        dispatch: Dispatch,
        line: Line,
        blockers: tuple[str, ...] | list[str],
        *,
        kind: str = "review",
        resume: bool = False,
    ) -> bool:
        """Same author, same attempt count. The stored task is not rewritten.

        ``resume`` rebuilds the task with ``_fix_task`` from the handoff view
        already stored for this line. It does not record a second round.
        """
        if resume:
            return await self._resume_fix(dispatch, line)
        gates = self._c.gates
        assert gates is not None
        count = len(blockers)
        limitations = "none" if count == 0 else f"{count} blockers"
        facts = await gates.handoff_facts_async(
            dispatch, line, limitations=limitations
        )
        facts["blocker_count"] = count
        handoff = Handoff(
            branch=str(facts["branch"]),
            commit=str(facts["commit"]),
            files=tuple(facts["files"]) if isinstance(facts["files"], tuple) else tuple(facts["files"] or []),
            test_command=str(facts["test_command"]),
            results=str(facts["results"]),
            known_limitations=str(facts["known_limitations"]),
        )
        if not handoff.complete:
            self._block_line(dispatch, line, HANDOFF_INCOMPLETE)
            return False
        gates.record_handoff_view(dispatch, line, facts)
        snap = gates.line_snapshot(dispatch, line)
        kind_name = "g2" if kind == "g2" else "review"
        # A new round starts a new contract. A G2 fix has no review ruling, and
        # a review fix replaces the previous round's confirmation entirely.
        previous_ruling = _ruling_from_dict(snap.get(FIX_RULING))
        snap.pop(FIX_RULING, None)
        snap.pop(RULING_CHECK, None)
        snap.pop(RULING_UNCONFIRMED, None)
        # A BLOCK made only of the ruling check's own "not confirmed" lines is
        # the same contract again, not a reviewer's new finding (see below).
        reissue = (
            ruling_enforced()
            and kind_name == "review"
            and previous_ruling is not None
            and only_unconfirmed(blockers)
        )
        state = _load_state(snap)
        if reissue:
            # Only the confirmations were missing: this head may come back
            # unchanged with a confirming report (see _unreviewed_head_continues).
            snap[RULING_REISSUE_HEAD] = handoff.commit
        else:
            snap.pop(RULING_REISSUE_HEAD, None)
        if kind_name == "review" and blockers and not reissue:
            record_blocked_round(
                snap,
                blockers,
                reviewer=(
                    state.blocking_reviewer
                    or state.primary_reviewer
                    or str(snap.get("blocking_reviewer") or snap.get("reviewer") or "")
                ),
            )
        snap["fix_round"] = int(snap.get("fix_round") or 0) + 1
        snap["handoff_ref"] = handoff.reference()
        escalation = build_escalation(line, snap, threshold=ESCALATION_THRESHOLD)
        if escalation is not None:
            write_escalation(snap, escalation)
        ruling = None
        if reissue:
            assert previous_ruling is not None
            ruling = reissue_ruling(
                previous_ruling,
                fix_round=int(snap.get("fix_round") or 0),
                escalation=escalation,
            )
            snap[FIX_RULING] = ruling.to_dict()
        elif kind_name == "review" and blockers:
            ruling = build_ruling(
                line.id,
                blockers,
                fix_round=int(snap.get("fix_round") or 0),
                escalation=escalation,
            )
            snap[FIX_RULING] = ruling.to_dict()
        task = _fix_task(line.task, handoff, blockers, ruling=ruling)
        g2_note = ""
        if kind_name == "g2":
            # The failed G2 command, its exit code and output tail (N27). Read
            # now, while the G2 cell still holds the failure: the launch resets
            # it, so the note is kept with the fix intent for a restart.
            g2_note = self._g2_note(dispatch, line)
            task = with_g2_failure(task, g2_note)
        await self._submit(self._pop_run, line.id)
        line.current_run = None
        line.status = LineStatus.ASSIGNED
        line.failure_reason = None
        await self._begin_fix(
            dispatch,
            line,
            kind_name,
            line.assigned_model or "",
            line.attempts,
            "",
            task=task,
            g2_note=g2_note,
        )
        return not self._stopped(dispatch)

    async def _resume_fix(self, dispatch: Dispatch, line: Line) -> bool:
        """Launch a deferred fix again from the persisted view and intent."""
        gates = self._c.gates
        assert gates is not None
        intent = self._pending_fix(dispatch, line.id)
        if intent is None:
            return await self._park_fix_incomplete(dispatch, line)
        if self._fix_intent_stale(dispatch, line, intent):
            await self._drop_stale_fix(dispatch, line)
            self._stale_fix_dropped = True
            return True
        author = intent.get("author")
        if isinstance(author, str) and author:
            line.assigned_model = author
        attempt = intent.get("attempt")
        if isinstance(attempt, int) and not isinstance(attempt, bool):
            line.attempts = attempt
        snap = gates.line_snapshot(dispatch, line)
        cell = snap.get("gates")
        view = cell.get("handoff") if isinstance(cell, dict) else None
        if not isinstance(view, dict):
            return await self._park_fix_incomplete(dispatch, line)
        kind = str(intent.get("kind") or "")
        try:
            count = int(view.get("blocker_count") or 0)
        except (TypeError, ValueError):
            count = -1
        if kind == "g2":
            blockers: tuple[str, ...] | list[str] = ()
            findings_match = count == 0
        else:
            blockers = list(line.blockers)
            findings_match = count >= 0 and len(blockers) == count
        if not findings_match:
            return await self._park_fix_incomplete(dispatch, line)
        limitations = "none" if count == 0 else f"{count} blockers"
        handoff = _handoff_from_view(view, limitations=limitations)
        stored_ref = str(snap.get("handoff_ref") or "")
        if not handoff.complete or handoff.reference() != stored_ref:
            return await self._park_fix_incomplete(dispatch, line)
        ruling = _ruling_from_dict(snap.get(FIX_RULING))
        if ruling is None and kind != "g2" and blockers:
            ruling = build_ruling(
                line.id,
                blockers,
                fix_round=int(snap.get("fix_round") or 0),
                escalation=escalation_from_snapshot(snap),
            )
            snap[FIX_RULING] = ruling.to_dict()
        task = _fix_task(line.task, handoff, blockers, ruling=ruling)
        stored_note = intent.get("g2_note")
        g2_note = stored_note if kind == "g2" and isinstance(stored_note, str) else ""
        # The note stored when the fix was first written: the same task again.
        task = with_g2_failure(task, g2_note)
        await self._submit(self._pop_run, line.id)
        line.current_run = None
        line.status = LineStatus.ASSIGNED
        line.failure_reason = None
        stored_attempt = intent.get("attempt")
        attempt = stored_attempt if isinstance(stored_attempt, int) else line.attempts
        if isinstance(attempt, bool):
            attempt = line.attempts
        await self._begin_fix(
            dispatch,
            line,
            str(intent.get("kind") or "review"),
            str(intent.get("author") or line.assigned_model or ""),
            attempt,
            str(intent.get("source_ref") or ""),
            task=task,
            g2_note=g2_note,
        )
        return not self._stopped(dispatch)

    async def _park_fix_incomplete(self, dispatch: Dispatch, line: Line) -> bool:
        """A fix whose persisted findings no longer match does not launch."""
        self._clear_pending_fix(dispatch, line.id)
        self._clear_pending_relaunch(dispatch, line)
        self._block_line(dispatch, line, HANDOFF_INCOMPLETE)
        await self._park_blocked(dispatch)
        return False

    async def _replan(
        self,
        dispatch: Dispatch,
        line: Line,
        blockers: list[str],
        reason: str,
    ) -> None:
        """Split one blocked line for the planner, once (N48).

        A line is replanned at most once: past that the machine stops
        deciding and parks the line for the Planner, whose ruling is the only
        way on. The children of the one allowed replan start from the branch
        of a dependency that already passed, so a child that needs its
        dependency's code has the code instead of a stub of it.
        """
        gates = self._c.gates
        assert gates is not None
        snap = gates.line_snapshot(dispatch, line)
        state = _load_state(snap)
        if blockers:
            record_blocked_round(
                snap,
                blockers,
                reviewer=(
                    state.blocking_reviewer
                    or state.primary_reviewer
                    or str(snap.get("blocking_reviewer") or snap.get("reviewer") or "")
                ),
            )
        escalation = build_escalation(line, snap, threshold=ESCALATION_THRESHOLD)
        if escalation is not None:
            write_escalation(snap, escalation)
        used = int(snap.get("replans") or 0)
        if used >= gates._settings.max_replans:
            self._park_for_planner(dispatch, line)
            return
        sibling_base = await self._sibling_base(dispatch, line)
        if sibling_base.unresolved:
            # A dependency passed but its branch has no head git can name:
            # there is nothing to split onto, and the integrate base lacks
            # the dependency the children would then stub.
            self._park_unresolved(dispatch, line, sibling_base.unresolved)
            return
        if sibling_base.uncombined:
            # No head carries every passed dependency, so there is no base to
            # split onto: the Planner decides before a child is asked to
            # build on half of what its line waits on.
            self._park_uncombined(dispatch, line, sibling_base.uncombined)
            return
        prose = "\n".join(
            item for item in line.acceptance if isinstance(item, str) and not item.startswith("cmd:")
        )
        template = (
            "Replan this line.\n"
            f"Goal: {dispatch.goal}\n"
            f"Line task: {line.task}\n"
            f"Acceptance: {prose}\n"
            f"Blocker count: {len(blockers)}\n"
            f"Reason: {reason}\n"
        )
        if escalation is not None:
            template += "\n" + render_escalation(escalation) + "\n"
        try:
            planned = await self._leave(self._c.planner.plan(template))
        except Exception as exc:
            logger.warning("Replan of %s failed: %s", line.id, type(exc).__name__)
            self._exhaust_replan(dispatch, line)
            return
        if not planned:
            self._exhaust_replan(dispatch, line)
            return
        # The planner call let the actor apply events and adopt a saved row:
        # read the snapshot again, or these writes go to a stale copy.
        snap = gates.line_snapshot(dispatch, line)
        number = used + 1
        # A line that waits on nothing starts its children from the integrate
        # base, as every first-generation line does, not from a sibling.
        integrate_base = (
            dispatch.gates.get("base_commit") if isinstance(dispatch.gates, dict) else None
        )
        base_head = sibling_base.head or (
            integrate_base if isinstance(integrate_base, str) else ""
        )
        fresh: list[Line] = []
        for index, source in enumerate(planned, start=1):
            new_id = f"{line.id}-r{number}-{index}"
            fresh.append(
                replace(
                    source,
                    id=new_id,
                    depends_on=line.depends_on,
                    status=LineStatus.ASSIGNED,
                    assigned_model=line.assigned_model,
                    attempts=0,
                    current_run=None,
                    blockers=[],
                    failure_reason=None,
                    branch=f"{dispatch.branch_prefix}/line-{new_id}",
                    # N48: the child starts from its passed dependency's
                    # branch, not from the dispatch base, so the dependency is
                    # present in the worktree instead of stubbed.
                    base_commit=base_head or source.base_commit,
                    log_tail="",
                )
            )
        line.status = LineStatus.FAILED
        line.failure_reason = REPLANNED
        line.current_run = None
        dispatch.lines.extend(fresh)
        _retarget_dependents(dispatch, line.id, tuple(child.id for child in fresh))
        await gates.prepare_async(dispatch)
        # prepare and the awaits before it may have replaced the snapshot.
        snap = gates.line_snapshot(dispatch, line)
        snap["replans"] = number
        snap["reason"] = REPLANNED
        for child in fresh:
            child_snap = gates.line_snapshot(dispatch, child)
            child_snap["replans"] = number
            child_snap["replanned_from"] = line.id
        self._advance(dispatch, line, "replan_decision", option="resplit_task")
        siblings_out = any(
            other.id != line.id
            and (
                other.current_run
                or other.status is LineStatus.RUNNING
                or other.id in self._line_tasks
            )
            for other in dispatch.lines
        )
        if siblings_out:
            # A sibling's reservation carries this drive's epoch (N31): a bump
            # would drop its later result as stale, with its retry accounting,
            # skip and backoff. The replanned line is fenced by its own
            # status and run; the children are new.
            await self._save(dispatch)
            return
        # The epoch bump is inside the mutation, after the stored epoch is
        # checked. The drive adopts only the row that mutation returns.

        def mutation(fresh: Dispatch) -> Dispatch:
            bump_epoch(dispatch)
            return fresh if fresh is dispatch else dispatch

        try:
            saved = await drive_mutation(self._c.store, dispatch.id, self._epoch0, mutation)
        except StaleDrive:
            await self._submit(self._flag_abandoned, True)
            return
        if saved is None:
            await self._submit(self._flag_abandoned, True)
            return
        await self._submit(self._adopt_pair, dispatch, saved)

    def _exhaust_replan(self, dispatch: Dispatch, line: Line) -> None:
        self._block_line(dispatch, line, REPLAN_EXHAUSTED)
        dispatch.message = REPLAN_EXHAUSTED_MESSAGE.format(line_id=line.id)

    def _park_for_planner(self, dispatch: Dispatch, line: Line) -> None:
        """The line used its one replan: park it for the Planner (N48).

        Not a failure and not a retry. Stage-Gate still holds the line in
        ``replanning`` with its options, so the Planner's ruling -- resplit
        the task or replace the developer -- is what moves it, and the reason
        says so instead of reading as a machine that gave up.
        """
        self._block_line(dispatch, line, REPLAN_PARKED)
        line.blockers = [
            "This line used its one replan; the Planner decides how it goes on: "
            + _PLANNER_WAY_OUT.format(dispatch_id=dispatch.id, line_id=line.id)
        ]
        dispatch.message = REPLAN_PARKED_MESSAGE.format(
            dispatch_id=dispatch.id, line_id=line.id
        )

    def _park_uncombined(self, dispatch: Dispatch, line: Line, siblings: tuple[Line, ...]) -> None:
        """Passed dependencies no one branch carries: the Planner decides (N48).

        The same park, reason and way out as the replan cap -- the line is
        blocked, Stage-Gate still holds it in ``replanning`` with its options,
        and a reassign or a Planner ruling relaunches it. Only the sentence
        differs, because the Planner is asked something else: resplit the line
        so every child waits on one branch, or replace the developer.
        """
        self._block_line(dispatch, line, REPLAN_UNCOMBINED)
        line.blockers = [
            "This line waits on passed lines ("
            + ", ".join(other.id for other in siblings)
            + ") whose branches share no commit, so no branch can be the base "
            "of its split; the Planner decides how it goes on: "
            + _PLANNER_WAY_OUT.format(dispatch_id=dispatch.id, line_id=line.id)
        ]
        dispatch.message = REPLAN_UNCOMBINED_MESSAGE.format(
            dispatch_id=dispatch.id, line_id=line.id
        )

    def _park_unresolved(
        self, dispatch: Dispatch, line: Line, siblings: tuple[Line, ...]
    ) -> None:
        """A passed dependency whose branch git cannot name: park (N48).

        Skipping it would split the line onto the integrate base, which lacks
        the dependency's code -- the stubbing N48 exists to stop. The same park
        and way out as the cap; the blocker names the branches so the operator
        can see which one is gone.
        """
        self._block_line(dispatch, line, REPLAN_UNRESOLVED)
        line.blockers = [
            "This line waits on passed lines ("
            + ", ".join(f"{other.id} on {other.branch or 'no branch'}" for other in siblings)
            + ") whose branch git cannot name, so there is no base to split it "
            "onto; the Planner decides how it goes on: "
            + _PLANNER_WAY_OUT.format(dispatch_id=dispatch.id, line_id=line.id)
        ]
        dispatch.message = REPLAN_UNRESOLVED_MESSAGE.format(
            dispatch_id=dispatch.id, line_id=line.id
        )

    def _passed_dependencies(self, dispatch: Dispatch, line: Line) -> tuple[Line, ...]:
        """The passed lines this line waits on, in its ``depends_on`` order (N48).

        Only the line's own ``depends_on`` names a base: a child of a line
        that waits on line-a needs line-a's code, and the code of whichever
        sibling happened to pass last is no substitute. A line that waits on
        nothing has nothing to build on and starts from the integrate base.
        """
        by_id = {other.id: other for other in dispatch.lines}
        return tuple(
            by_id[dependency]
            for dependency in line.depends_on
            if dependency in by_id and by_id[dependency].status is LineStatus.PASSED
        )

    async def _sibling_base(self, dispatch: Dispatch, line: Line) -> _SiblingBase:
        """The branch head a replanned line's children start from (N48).

        R8c split a line into children that had to write stubs of a sibling's
        unmerged code, because every child started from the dispatch base. A
        child that starts from its dependency's branch has the real code in
        its worktree, and its own diff stays its own.

        One head comes back, or the line parks:

        - A passed dependency whose branch git cannot name parks the line
          (``unresolved``): skipping it would base the children on a commit
          without that dependency.
        - Ancestry is asked before parking several heads: a head the
          integrate base already contains adds nothing, and a head another
          dependency's head contains is carried by it. What is left after
          that is the base when it is one head.
        - Two or more heads git does not show on one chain sit on parallel
          branches, and writing a merge commit is not this machine's to do,
          so the line parks for the Planner (``uncombined``).
        """
        gates = self._c.gates
        if gates is None:
            return _SiblingBase()
        passed = self._passed_dependencies(dispatch, line)
        heads: list[tuple[Line, str]] = []
        unresolved: list[Line] = []
        for other in passed:
            head = await self._leave(asyncio.to_thread(gates._revision, other))
            value = head.strip() if isinstance(head, str) else ""
            # A revision git could not name is not a base, and neither is a
            # value that is not one bare object id.
            if not value or len(value.split()) != 1:
                unresolved.append(other)
                continue
            if all(value != known for _other, known in heads):
                heads.append((other, value))
        if unresolved:
            return _SiblingBase(unresolved=tuple(unresolved))
        if len(heads) > 1:
            heads = await self._uncontained_heads(dispatch, heads)
        if len(heads) > 1:
            return _SiblingBase(uncombined=tuple(other for other, _head in heads))
        return _SiblingBase(head=heads[0][1] if heads else "")

    async def _uncontained_heads(
        self, dispatch: Dispatch, heads: list[tuple[Line, str]]
    ) -> list[tuple[Line, str]]:
        """The heads no other candidate (nor the integrate base) already carries.

        An answer git cannot give counts as "not contained", so doubt keeps a
        head and the line parks rather than losing a dependency.
        """
        integrate_base = (
            dispatch.gates.get("base_commit") if isinstance(dispatch.gates, dict) else None
        )
        kept: list[tuple[Line, str]] = []
        for other, head in heads:
            if isinstance(integrate_base, str) and integrate_base:
                if await self._is_ancestor(head, integrate_base):
                    continue  # already merged into the integrate base
            if any(
                [await self._is_ancestor(head, rival) for _rival_line, rival in heads if rival != head]
            ):
                continue  # another dependency's head carries this one
            kept.append((other, head))
        return kept

    async def _is_ancestor(self, ancestor: str, commit: str) -> bool:
        """True only when git says ``ancestor`` is contained in ``commit``."""
        gates = self._c.gates
        if gates is None:
            return False
        repo = Path(gates._settings.repo_root)
        return await self._leave(asyncio.to_thread(_git_is_ancestor, repo, ancestor, commit)) is True

    async def _schedule_review_retry(
        self, dispatch: Dispatch, line: Line, reviewer: str
    ) -> None:
        """Record a failed review and when to try again, on the line (N42).

        Waiting for a wakeup left lines RUNNING with no worker for hours when
        nothing else in the dispatch was active, and waiting inline stopped
        every other line. The retry is persisted instead: ``review_retry``
        holds the attempt, the reviewers that failed and ``retry_at`` (wall
        clock), so it survives a restart. ``_review_gated`` skips the line
        until it is due and the drive keeps moving the others; when nothing
        else can move, the drive sleeps until the earliest one. From the
        second failure on the next eligible reviewer is asked. After the last
        delay the line parks ``review_unavailable``; ``dispatch unblock``
        resumes it.
        """
        gates = self._c.gates
        assert gates is not None
        attempts = self._bump_review_attempts(dispatch, line)
        self._review_retries += 1
        snap = gates.line_snapshot(dispatch, line)
        previous = _review_retry(snap) or {}
        failed = [str(name) for name in previous.get("failed") or []]
        failed = list(dict.fromkeys([*failed, reviewer]))
        if attempts > len(REVIEW_RETRY_DELAYS):
            self._park_review_unavailable(dispatch, line, failed)
            await self._park_blocked(dispatch)
            return
        delay = REVIEW_RETRY_DELAYS[attempts - 1]
        snap[REVIEW_RETRY] = {
            "attempt": attempts,
            "reviewer": reviewer,
            "failed": failed,
            "delay_seconds": delay,
            "retry_at": self._wall() + delay,
            "at": gates.stamp(),
        }
        logger.warning(
            "Reviewer %s unavailable for %s (failure %s); retry in %ss",
            reviewer,
            line.id,
            attempts,
            delay,
        )
        await self._save(dispatch)

    def _review_waits(self, dispatch: Dispatch) -> dict[str, float]:
        """RUNNING lines whose scheduled review retry is not due yet: seconds left."""
        gates = self._c.gates
        if gates is None:
            return {}
        now = self._wall()
        waits: dict[str, float] = {}
        for line in dispatch.lines:
            if line.status is not LineStatus.RUNNING:
                continue
            retry = _review_retry(gates.line_snapshot(dispatch, line))
            if retry is None:
                continue
            due = retry.get("retry_at")
            if isinstance(due, (int, float)) and not isinstance(due, bool) and due > now:
                waits[line.id] = float(due) - now
        return waits

    async def _wait_for_review_retry(self, dispatch: Dispatch, waits: dict[str, float]) -> None:
        """Nothing else can move: sleep until the earliest retry, then make it due."""
        delay = min(waits.values())
        await self._leave(self._sleep(delay))
        await self._mark_review_retries_due(dispatch, waits, delay)

    async def _mark_review_retries_due(
        self, dispatch: Dispatch, waits: dict[str, float], delay: float
    ) -> None:
        if self._abandoned or self._stopped(dispatch):
            return
        gates = self._c.gates
        assert gates is not None
        now = self._wall()
        for line in dispatch.lines:
            if line.id not in waits or waits[line.id] > delay:
                continue
            retry = _review_retry(gates.line_snapshot(dispatch, line))
            if retry is not None:
                # The drive waited exactly this long: due, whatever the clock says.
                retry["retry_at"] = min(float(retry.get("retry_at") or now), now)

    async def _fallback_reviewer(
        self,
        dispatch: Dispatch,
        line: Line,
        failed: list[str],
        *,
        secondary: bool,
        state: StageGateState,
    ) -> str | None:
        """The next eligible reviewer, or None to keep asking the current one."""
        exclude = [line.assigned_model or "", *failed]
        if secondary and state.primary_reviewer:
            exclude.append(state.primary_reviewer)
        try:
            return await self._select_reviewer(dispatch, line, exclude=exclude)
        except (
            _NoEligibleReviewer,
            _NoSecondaryReviewer,
            _ReviewersSkipped,
            _ReviewersQuotaExhausted,
            DispatchError,
        ):
            # Nobody else may take it right now: keep asking the current
            # reviewer; the retry budget decides when the line parks.
            return None

    async def _reviewer_usable(self, dispatch: Dispatch, line: Line, name: str) -> bool:
        """A stored reviewer is reused only when not skipped and with review headroom."""
        if name.lower() in self._skipped_providers(dispatch):
            return False
        blockers = await self._headroom_blocked(line, [name])
        return name.lower() not in blockers

    async def _headroom_blocked(self, line: Line, names: list[str]) -> set[str]:
        """Which of ``names`` the selector's headroom keeps from reviewing."""
        review_blockers = getattr(self._c.selector, "review_blockers", None)
        if not callable(review_blockers):
            return set()
        try:
            found = await self._leave(review_blockers(line, []))
        except Exception as exc:  # advisory
            logger.warning("Could not read reviewer headroom: %s", type(exc).__name__)
            return set()
        wanted = {name.lower() for name in names}
        return {str(name).lower() for name in (found or {})} & wanted

    async def _primaries_out(
        self, dispatch: Dispatch, line: Line, primaries: set[str]
    ) -> list[str]:
        """Primary reviewers out for quota (skipped or no headroom), not mere holders."""
        holders = {name.lower() for name in self._line_holders(dispatch, line)}
        candidates = sorted(name for name in primaries if name not in holders)
        if not candidates:
            return []
        skipped = set(self._skipped_providers(dispatch))
        blocked = await self._headroom_blocked(line, candidates)
        return [name for name in candidates if name in skipped or name in blocked]

    async def _skipped_reviewers_until(
        self, dispatch: Dispatch, line: Line, exclude: list[str]
    ) -> float | None:
        """When only skipped providers could review: the earliest skip expiry.

        One selector call with the skips left out of the exclusions; the
        selector's ``review_refusal`` is restored as the real pick set it.
        """
        skipped = self._skipped_providers(dispatch)
        if not skipped:
            return None
        selector = self._c.selector
        had_refusal = hasattr(selector, "review_refusal")
        refusal = getattr(selector, "review_refusal", None)
        lowered = {name.lower() for name in skipped}
        try:
            used = [
                name
                for name in self._selection_used(
                    dispatch, line, exclude, role=ModelRole.REVIEW
                )
                if name.lower() not in lowered
            ]
            probe = await self._leave(selector.select(line, used, role=ModelRole.REVIEW))
        finally:
            if had_refusal:
                selector.review_refusal = refusal
        if probe is None or probe.strip().lower() not in lowered:
            return None
        # Only a reviewer that could take this line counts: on the review
        # roster, and not excluded (a holder, the primary on a secondary pick).
        # A developer-only provider's short skip must not postpone a park.
        roster = _review_roster(selector)
        barred = {name.lower() for name in exclude if name}
        root = dispatch.gates.get(PROVIDER_SKIPS) if isinstance(dispatch.gates, dict) else {}
        expiries = [
            float(entry["until"])
            for name, entry in (root or {}).items()
            if str(name).lower() in lowered
            and str(name).lower() not in barred
            and (roster is None or str(name).lower() in roster)
            and isinstance(entry, dict)
            and isinstance(entry.get("until"), (int, float))
        ]
        return min(expiries) if expiries else None

    async def _wait_for_skipped_reviewers(
        self, dispatch: Dispatch, line: Line, until: float
    ) -> None:
        """Schedule the review for when the earliest skipped reviewer is back.

        The retry budget is not spent: nobody failed, the reviewers are only
        rate limited. ``_review_waits`` skips the line until then.
        """
        gates = self._c.gates
        assert gates is not None
        snap = gates.line_snapshot(dispatch, line)
        previous = _review_retry(snap) or {}
        snap[REVIEW_RETRY] = {
            "attempt": int(previous.get("attempt") or 0),
            "reviewer": str(previous.get("reviewer") or ""),
            "failed": list(previous.get("failed") or []),
            "retry_at": until,
            "reason": "reviewers_skipped",
            "at": gates.stamp(),
        }
        self._review_retries += 1
        logger.warning("Every reviewer for %s is skipped; review again at %s", line.id, until)
        await self._save(dispatch)

    def _park_review_unavailable(
        self, dispatch: Dispatch, line: Line, failed: list[str]
    ) -> None:
        """Block after the retry budget; keep what ``dispatch unblock`` needs to resume."""
        gates = self._c.gates
        assert gates is not None
        snap = gates.line_snapshot(dispatch, line)
        state = _load_state(snap)
        snap.pop(REVIEW_RETRY, None)
        snap[REVIEW_PARKED] = {
            "run_id": line.current_run,
            "attempt": line.attempts,
            "author": line.assigned_model or "",
            "stage": state.stage.value,
            "failed": list(dict.fromkeys(failed)),
        }
        names = ", ".join(dict.fromkeys(failed))
        line.blockers = [
            f"No reviewer answered after {len(REVIEW_RETRY_DELAYS)} retries "
            f"({names}). When a reviewer is back, run `conductor dispatch unblock "
            f"{dispatch.id} {line.id}` to retry the review."
        ]
        self._block_line(dispatch, line, REVIEW_UNAVAILABLE)
        if state.stage is not Stage.BLOCKED and not state.stage.terminal:
            self._advance(dispatch, line, "block", provider=line.assigned_model or "")

    async def _note_review_unavailable(self, dispatch: Dispatch, line: Line) -> None:
        """Count a transient reviewer failure. Three in a row parks the line."""
        attempts = self._bump_review_attempts(dispatch, line)
        if attempts >= REVIEW_ATTEMPT_LIMIT:
            self._block_line(dispatch, line, REVIEW_UNAVAILABLE)
            await self._park_blocked(dispatch)
            return
        await self._save(dispatch)

    def _bump_review_attempts(self, dispatch: Dispatch, line: Line) -> int:
        gates = self._c.gates
        if gates is None:
            if not isinstance(dispatch.gates, dict):
                dispatch.gates = {}
            raw = dispatch.gates.get("review_attempts")
            bucket = dict(raw) if isinstance(raw, dict) else {}
            try:
                attempts = int(bucket.get(line.id) or 0) + 1
            except (TypeError, ValueError):
                attempts = 1
            bucket[line.id] = attempts
            dispatch.gates["review_attempts"] = bucket
            return attempts
        snap = gates.line_snapshot(dispatch, line)
        try:
            attempts = int(snap.get("review_attempts") or 0) + 1
        except (TypeError, ValueError):
            attempts = 1
        snap["review_attempts"] = attempts
        return attempts

    def _clear_review_attempts(self, dispatch: Dispatch, line: Line) -> None:
        gates = self._c.gates
        if gates is None:
            raw = dispatch.gates.get("review_attempts") if isinstance(dispatch.gates, dict) else None
            if isinstance(raw, dict):
                raw.pop(line.id, None)
            return
        gates.line_snapshot(dispatch, line).pop("review_attempts", None)

    def _block_line(self, dispatch: Dispatch, line: Line, reason: str) -> None:
        line.status = LineStatus.BLOCKED
        line.failure_reason = reason
        line.current_run = None
        gates = self._c.gates
        if gates is None:
            return
        snap = gates.line_snapshot(dispatch, line)
        snap["reason"] = reason
        if reason == FOCUSED_TESTS_EXHAUSTED:
            snap["phase"] = "focused_tests"
        # A parked line has no review in flight: a scheduled retry or a
        # count from an earlier run must not carry over to the next one.
        _clear_review_retry(snap)

    def _checkpoint_exited(
        self,
        dispatch: Dispatch,
        line: Line,
        reason: str,
        run_id: str | None,
        *,
        exit_code: int | None = None,
    ) -> None:
        """A run of this line ended and nothing of it is out: a checkpoint (N40).

        Exit 0 with no new commit and a dirty tree are such ends. The same
        ``worker_exited`` record worker recovery writes (N38b) goes on the
        snapshot, and Stage-Gate leaves ``implementing`` for ``blocked``, so
        the line no longer holds the serial slot and a human reassign is
        accepted. Ungated dispatches have no Stage-Gate and are left alone.
        """
        gates = self._c.gates
        if gates is None or line.current_run:
            return
        record_worker_exited(
            dispatch,
            line,
            gates.line_snapshot(dispatch, line),
            run_id=run_id or f"{dispatch.id}:{line.id}:{line.attempts}:exited",
            failure_class=reason,
            exit_code=exit_code,
            stamp=gates.stamp(),
        )

    def _ensure_submitted(self, dispatch: Dispatch, line: Line) -> None:
        gates = self._c.gates
        assert gates is not None
        state = _load_state(gates.line_snapshot(dispatch, line))
        provider = line.assigned_model or ""
        if state.stage is Stage.DAG_READY:
            self._advance(dispatch, line, "assign", provider=provider)
            self._advance(dispatch, line, "start_implementation")
            self._advance(dispatch, line, "submit_work")
        elif state.stage is Stage.ASSIGNED:
            self._advance(dispatch, line, "start_implementation")
            self._advance(dispatch, line, "submit_work")
        elif state.stage is Stage.IMPLEMENTING:
            self._advance(dispatch, line, "submit_work")

    def _advance(self, dispatch: Dispatch, line: Line, kind: str, **fields: object) -> Transition:
        gates = self._c.gates
        assert gates is not None
        snap = gates.line_snapshot(dispatch, line)
        state = _load_state(snap)
        event = Event(kind=kind, timestamp=gates.stamp(), **fields)  # type: ignore[arg-type]
        transition = advance(state, event)
        if transition.accepted:
            _remember_event(snap, transition)
            _store_state(snap, transition.state)
        else:
            logger.warning(
                "Stage-Gate refused %s on %s: %s", kind, line.id, transition.reason
            )
        return transition

    async def _adopt_settled_runs(self, dispatch: Dispatch) -> None:
        """A saved completion is enough. The new process does not poll the old one."""
        if not self._on_actor():
            await self._submit(self._adopt_settled_runs, dispatch)
            return
        gates = self._c.gates
        if gates is None:
            return
        ensure = self._own_ensure_run
        assert ensure is not None
        for line in dispatch.lines:
            if line.status is not LineStatus.RUNNING:
                continue
            if not gates.run_is_settled(dispatch, line):
                continue
            run = ensure(line.id)
            run.complete = True

    async def _sweep_wakeups(self, dispatch: Dispatch) -> None:
        if not self._on_actor():
            await self._submit(self._sweep_wakeups, dispatch)
            return
        gates = self._c.gates
        assert gates is not None
        settled = False
        stale = False
        reset = False
        recovery_root = self._recovery_data(dispatch)
        wall_now = datetime.now(timezone.utc).timestamp()
        for line in dispatch.lines:
            entry = recovery_root.get(line.id)
            if (
                not isinstance(entry, dict)
                or line.status is not LineStatus.BLOCKED
                or line.failure_reason != "author_quota_exhausted"
                or entry.get("parked_reason") != "author_quota_exhausted"
            ):
                continue
            retry_at = entry.get("quota_retry_at")
            if (
                entry.get("quota_retry_source") == "retry_after"
                and isinstance(retry_at, (int, float))
                and wall_now >= float(retry_at)
            ):
                # Retry-After allows a same-author retry. It does not clear the
                # provider-level exhausted marker; a successful launch does.
                snap = gates.line_snapshot(dispatch, line)
                state = _load_state(snap)
                if state.stage is Stage.BLOCKED:
                    transition = self._advance(
                        dispatch,
                        line,
                        "resume",
                        provider=line.assigned_model or "",
                    )
                    if not transition.accepted:
                        continue
                line.status = LineStatus.ASSIGNED
                line.failure_reason = None
                line.current_run = None
                entry.pop("quota_retry_at", None)
                entry.pop("quota_retry_source", None)
                entry.pop("parked_reason", None)
                reset = True
        ensure = self._own_ensure_run
        assert ensure is not None
        for item in gates.peek_wakeups(dispatch):
            if item.get("kind") != "run_complete":
                continue
            line = dispatch.line(str(item.get("line_id") or ""))
            if line is None or line.status is not LineStatus.RUNNING:
                continue
            if not gates.event_matches(line, item):
                gates.note_stale_event(dispatch)
                stale = True
                continue
            gates.mark_run_settled(dispatch, line)
            run = ensure(line.id)
            run.complete = True
            settled = True
        if settled or stale or reset:
            await self._save(dispatch)
        if gates.pull_wakeups(dispatch):
            await self._save(dispatch)

    # -- N76 dispatch liveness ------------------------------------------------

    def _liveness_mode(self) -> str:
        """The configured sweep mode; a bad value normalizes to ``shadow``."""
        if self._config.liveness_mode is not None:
            return liveness_modes.normalize_mode(self._config.liveness_mode)
        return liveness_modes.liveness_mode()

    def _liveness_policy(self) -> liveness_policy.Policy:
        """The frozen recovery table plus any tightening local override."""
        configured = self._config.liveness_policy
        if isinstance(configured, liveness_policy.Policy):
            return configured
        try:
            return liveness_policy.load_policy(self._config.liveness_policy_path)
        except liveness_policy.PolicyError as exc:
            logger.warning("Liveness policy ignored: %s", exc)
            return liveness_policy.default_policy()

    def _liveness_view(
        self, dispatch: Dispatch, line: Line, snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        """The plain mapping ``decide_owner`` reads: line fields plus its gate cell."""
        view = line.to_dict()
        for key in ("review_retry", "review", "liveness", "needs_human", "human_reason"):
            if key in snapshot:
                view[key] = snapshot.get(key)
        view["current_run"] = line.current_run
        if pending_human_reassigns(dispatch).get(line.id) is not None:
            view["queued_request"] = {"state": "pending"}
        # The worker-recovery Retry-After is an owner. Copy it onto the view
        # only: persisting it would replace a real recovery row, and leaving
        # it out makes a future author_quota_exhausted park look ownerless.
        _overlay_worker_recovery_timer(dispatch, line, view)
        lease = app_lines.liveness_overlay(dispatch, line)
        if lease is not None:
            view[liveness_owner.APP_LEASE_KEY] = lease
        return view

    def _liveness_record_for(
        self,
        dispatch: Dispatch,
        line: Line,
        snapshot: dict[str, Any],
        moment: datetime,
        policy: liveness_policy.Policy,
        *,
        previous: liveness_record.LivenessRecord | None = None,
    ) -> liveness_record.LivenessRecord:
        """Create the record a park owns, at this group's next table row.

        ``spent`` survives the re-park. The same group therefore continues at
        ``plan[spent]`` (R-EXIT's third row is SPLIT, not another REASSIGN).
        A different group starts at its own first row. Scheduling ``plan[0]``
        on every re-park left ``next_action`` on REASSIGN after the table had
        already selected SPLIT.
        """
        reason = line.failure_reason or "liveness_inconsistent"
        group = policy.group_for(reason)
        plan = policy.plan_for(reason)
        index = _liveness_plan_index(previous, group)
        chosen = plan[index] if index < len(plan) else None
        next_at, next_action = (
            _liveness_schedule(chosen, moment, policy) if chosen is not None else (moment, None)
        )
        return liveness_record.new_record(
            reason,
            group,
            budget=policy.max_steps if group != liveness_policy.HUMAN else 0,
            at=moment,
            next_at=next_at,
            next_action=next_action,
            previous=previous,
        )

    def _liveness_needs_human(
        self,
        dispatch: Dispatch,
        line: Line,
        snapshot: dict[str, Any],
        moment: datetime,
        policy: liveness_policy.Policy,
        reason: str,
        detail: str,
        *,
        act: bool,
    ) -> bool:
        """Record that a person owns this line; notify at most once.

        In ``shadow`` this is a recorded observation only: the line and the
        dispatch keep exactly what main wrote. In ``enforce`` it latches the
        one-way ``needs_human`` marker, keeps the note on the line's blockers
        and appends the one inbox line.
        """
        record = liveness_record.load(snapshot)
        if record is None:
            record = self._liveness_record_for(
                dispatch, line, snapshot, moment, policy
            )
        if not act:
            observed = record.with_step(
                "human",
                at=moment,
                result=liveness_record.SHADOW,
                detail=f"would hand to a human: {detail}"[:2000],
                next_at=moment + timedelta(seconds=LIVENESS_SHADOW_DELAY_SECONDS),
                next_action="human",
            )
            snapshot.update(liveness_record.store(snapshot, observed))
            return True
        first = not record.needs_human
        latched = record.mark_needs_human(reason, at=moment)
        snapshot.update(liveness_record.store(snapshot, latched))
        if not first:
            return True
        dispatch.state = DispatchState.REVIEW
        note = _liveness_human_note(line, latched, reason, detail)
        escalation = escalation_brief(snapshot)
        if escalation:
            note = f"{note}\n\n{escalation}"
        existing = [str(item) for item in line.blockers]
        if note not in existing:
            line.blockers = [*existing, note][:_BRIEF_MAX_BLOCKERS + 1]
        if not isinstance(dispatch.gates, dict):
            dispatch.gates = {}
        notes = dispatch.gates.setdefault("needs_human", {})
        if isinstance(notes, dict):
            notes[line.id] = {
                "reason": reason,
                "detail": detail,
                "group": latched.group,
                "at": latched.human_at,
                "note": note,
            }
        self._write_needs_human_note(dispatch, line, note)
        return True

    def _write_needs_human_note(
        self, dispatch: Dispatch, line: Line, note: str
    ) -> None:
        """Append one line to the operator inbox; a write failure is not fatal."""
        path = _liveness_inbox_path(self._c.gates)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"\n## {dispatch.id} / {line.id}\n\n{note}\n"
                )
        except OSError as exc:
            logger.warning("Could not write the needs-human note: %s", type(exc).__name__)

    def _review_admission(self, dispatch: Dispatch):
        primaries = _primary_reviewers(self._c.selector)
        return decide_launch_admission(
            dispatch,
            primary_concurrency=max(1, len(primaries)),
            gates=self._c.gates,
        )

    async def _admit_launch(self, dispatch: Dispatch, line: Line) -> bool:
        """Hold a new line while review is over its backlog cap (N76 §4.5).

        A queued line keeps ``failure_reason = review_backlog`` so the drive
        does not pick it again immediately. It asks once more after the
        admission pause; still queued, it stays parked for the next drive.
        """
        if not liveness_modes.should_act(self._liveness_mode()):
            # Review admission is an N76 enforce action. Off is main, and
            # shadow may not queue or sleep for it.
            return True
        admission = self._review_admission(dispatch)
        if admission.decision != QUEUE:
            if line.failure_reason == REVIEW_BACKLOG:
                line.failure_reason = None
            return True
        line.failure_reason = REVIEW_BACKLOG
        dispatch.message = (
            f"Review backlog {admission.limit} reached; line {line.id} queued "
            f"({REVIEW_BACKLOG})"
        )
        await self._save(dispatch)
        logger.info(
            "Line %s queued: %s at review backlog %s",
            line.id,
            REVIEW_BACKLOG,
            admission.limit,
        )
        await self._leave(
            self._sleep(max(0.0, self._config.review_admission_wait_seconds))
        )
        if self._stopped(dispatch) or self._abandoned:
            return False
        if self._review_admission(dispatch).decision == QUEUE:
            return False
        if line.failure_reason == REVIEW_BACKLOG:
            line.failure_reason = None
        return True

    async def _wait_queued_admission(self, dispatch: Dispatch) -> bool:
        """Nothing else can move: pause, then retry the saturated review queue.

        Returns True when admission reopened and the queued lines were
        released. When the queue is still full the ``review_backlog`` reason
        stays on the lines, so they do not re-enter the ready pool and spin the
        drive; the caller parks the dispatch recoverably instead.
        """
        await self._leave(
            self._sleep(max(0.0, self._config.review_admission_wait_seconds))
        )
        if self._stopped(dispatch) or self._abandoned:
            return False
        if self._review_admission(dispatch).decision == QUEUE:
            await self._save(dispatch)
            return False
        for line in dispatch.lines:
            if line.failure_reason == REVIEW_BACKLOG:
                line.failure_reason = None
        await self._save(dispatch)
        return True

    async def _park_review_backlog(
        self, dispatch: Dispatch, queued: list[Line]
    ) -> None:
        """Stop a drive on a full review queue without erroring (N76 §4.5).

        The queued lines keep ``review_backlog``; the dispatch rests in REVIEW
        so the next drive retries admission and the liveness sweep services
        any recovery timers. This is the recoverable park that replaces the
        old spin-until-the-loop-guard path.
        """
        if await self._fence(dispatch):
            return
        names = ", ".join(line.id for line in queued)
        dispatch.state = DispatchState.REVIEW
        dispatch.message = (
            f"Review backlog is still full; line(s) {names} are queued "
            f"({REVIEW_BACKLOG}). The next drive retries admission."
        )
        await self._save(dispatch)

    async def _sweep_liveness(
        self, dispatch: Dispatch, *, now: datetime | str | float | int | None = None
    ) -> int:
        """Give every open line an owner, a deadline and its next step (N76).

        Runs once per gated drive, after the wakeup sweep. It first checks the
        line status against the Stage-Gate stage; a known drift is repaired by
        appending the missed ``block`` event, an unknown one becomes
        ``liveness_inconsistent`` and a person's line. An open line that lost
        its owner (a RUNNING line with no run and no review) is handed to a
        person the same way. It then applies the recovery table for every
        parked line whose owner is gone or expired: ``enforce`` takes the
        step, ``shadow`` records what it would have been, ``off`` does neither.
        The per-line ``liveness`` cell carries the budget across restarts, so a
        re-park cannot renew it.
        """
        gates = self._c.gates
        if gates is None:
            return 0
        # An operator ``recover`` runs in every mode: it is a person's request,
        # not an automatic step. Without one this is a no-op (off stays main).
        handled = await self._sweep_operator_requests(dispatch, now=now)
        mode = self._liveness_mode()
        if mode == liveness_modes.OFF:
            return handled
        act = liveness_modes.should_act(mode)
        policy = self._liveness_policy()
        moment = liveness_record.parse_time(now) or datetime.now(timezone.utc)
        changed = False
        steps = 0
        for line in list(dispatch.lines):
            if line.status in _TERMINAL_LINE_STATUSES:
                continue
            snapshot = gates.line_snapshot(dispatch, line)
            if line.status is not LineStatus.BLOCKED:
                # An ownerless RUNNING line never reaches the parked-line
                # recovery table. Check every open line here: drift goes to a
                # person, and a live review/run is left to the monitor.
                if await self._sweep_open_line(
                    dispatch, line, snapshot, moment, policy, act=act
                ):
                    changed = True
                    steps += 1
                continue
            verdict = liveness_consistency.check(
                line.status, line.failure_reason, _snapshot_stage(snapshot)
            )
            if isinstance(verdict, liveness_consistency.Inconsistent):
                if act:
                    self._block_line(dispatch, line, "liveness_inconsistent")
                changed |= self._liveness_needs_human(
                    dispatch,
                    line,
                    snapshot,
                    moment,
                    policy,
                    "liveness_inconsistent",
                    verdict.detail,
                    act=act,
                )
                steps += 1
                continue
            if isinstance(verdict, liveness_consistency.Repair):
                if act:
                    transition = self._advance(
                        dispatch, line, "block", provider=line.assigned_model or ""
                    )
                    if not transition.accepted:
                        changed |= self._liveness_needs_human(
                            dispatch,
                            line,
                            snapshot,
                            moment,
                            policy,
                            "liveness_inconsistent",
                            transition.reason or verdict.detail,
                            act=True,
                        )
                        steps += 1
                        continue
                else:
                    record = liveness_record.load(snapshot) or self._liveness_record_for(
                        dispatch, line, snapshot, moment, policy
                    )
                    observed = record.with_step(
                        "repair",
                        at=moment,
                        result=liveness_record.SHADOW,
                        detail=f"would append block from {verdict.stage.value}",
                        next_at=moment + timedelta(seconds=LIVENESS_SHADOW_DELAY_SECONDS),
                        next_action="repair",
                    )
                    snapshot.update(liveness_record.store(snapshot, observed))
                    changed = True
                    steps += 1
                    continue
                changed = True
                steps += 1
                snapshot = gates.line_snapshot(dispatch, line)

            # author_quota_exhausted already has a Retry-After owner. A future
            # timer must not be replaced by the empty HUMAN plan's latch.
            if _author_quota_waiting(dispatch, line, moment):
                continue

            record = liveness_record.load(snapshot)
            if record is not None and record.needs_human:
                continue
            if record is not None and record.outcome != liveness_record.PENDING:
                # The line parked again after an earlier recovery. That is a
                # new episode: keep the old history and budget, and schedule
                # the next row of this group (another group starts at its first).
                record = self._liveness_record_for(
                    dispatch, line, snapshot, moment, policy, previous=record
                )
                snapshot.update(liveness_record.store(snapshot, record))
                changed = True
            live_quota: dict[str, object] | None = None
            # ``review_blockers`` names exhausted reviewers. An empty map means
            # the stage can run; a non-empty map does not mean it cannot.
            stage_open = False
            quota_park = (
                (record is not None and record.reason == REVIEWERS_QUOTA_EXHAUSTED)
                or line.failure_reason == REVIEWERS_QUOTA_EXHAUSTED
            )
            if act and quota_park:
                live_quota = await self._live_review_blockers(dispatch, line)
                stage_open = await self._review_stage_available(
                    dispatch, line, live_quota
                )
            decision = liveness_owner.decide_owner(
                self._liveness_view(dispatch, line, snapshot), moment
            )
            if (
                not liveness_owner.needs_recovery(decision, moment)
                and not stage_open
                and live_quota != {}
            ):
                continue
            if record is None or (
                line.failure_reason is not None and record.reason != line.failure_reason
            ):
                record = self._liveness_record_for(
                    dispatch, line, snapshot, moment, policy, previous=record
                )
                snapshot.update(liveness_record.store(snapshot, record))
                changed = True
            due = liveness_record.parse_time(record.next_at)
            if due is not None and due > moment:
                if not stage_open and live_quota != {}:
                    continue
                # Someone this stage can seat already has headroom. Do not sit
                # on another reviewer's reset, or on the stored timestamp.
                if stage_open or live_quota == {}:
                    due = moment
            plan = policy.plan_for(record.reason)
            index = _liveness_plan_index(record)
            if record.budget_left <= 0 or index >= len(plan):
                if _author_quota_waiting(dispatch, line, moment):
                    continue
                changed |= self._liveness_needs_human(
                    dispatch,
                    line,
                    snapshot,
                    moment,
                    policy,
                    "liveness_inconsistent" if not plan else record.reason,
                    "automatic recovery budget exhausted"
                    if plan
                    else "no automatic step is defined for this reason",
                    act=act,
                )
                steps += 1
                continue
            step = plan[index]
            # The selected row owns the verb. A re-park used to store plan[0]
            # in next_action, and that overrode a later SPLIT with REASSIGN.
            # A quota wait may already have been scheduled to resume as review
            # while the row itself is still the wait.
            action = step.action
            if (
                step.action == liveness_policy.WAIT
                and record.next_action == liveness_policy.REVIEW
            ):
                action = liveness_policy.REVIEW
            if (
                stage_open
                and action == liveness_policy.WAIT
                and record.reason == REVIEWERS_QUOTA_EXHAUSTED
            ):
                # The wait exists only for headroom this stage can use. A
                # recovered primary is enough; another exhausted reviewer is not.
                action = liveness_policy.REVIEW
            if (
                action == liveness_policy.REVIEW
                and record.reason in _WORKER_ASSIGNMENT_REASONS
            ):
                # The R-QUOTA row says "review" because it waits for reviewer
                # capacity. A line parked with no author has no review to
                # resume: select and install a worker instead. Resuming it as
                # RUNNING would leave it assigned to nobody.
                action = liveness_policy.REASSIGN
            if not act:
                observed = record.with_step(
                    action,
                    at=moment,
                    result=liveness_record.SHADOW,
                    detail=f"would {action} ({record.reason})",
                    next_at=moment + timedelta(seconds=LIVENESS_SHADOW_DELAY_SECONDS),
                    next_action=action,
                )
                snapshot.update(liveness_record.store(snapshot, observed))
                changed = True
                steps += 1
                continue
            if (
                record.reason == REVIEWERS_QUOTA_EXHAUSTED
                and action == liveness_policy.REVIEW
            ):
                if live_quota is None:
                    live_quota = await self._live_review_blockers(dispatch, line)
                    stage_open = await self._review_stage_available(
                        dispatch, line, live_quota
                    )
                parked = liveness_record.parse_time(record.parked_at) or moment
                cap = parked + timedelta(seconds=liveness_policy.QUOTA_WAIT_SECONDS)
                if stage_open:
                    # A reviewer this stage may seat has headroom. Other
                    # exhausted names in the blocker map do not postpone it.
                    pass
                elif live_quota:
                    if moment >= cap:
                        changed |= self._liveness_needs_human(
                            dispatch,
                            line,
                            snapshot,
                            moment,
                            policy,
                            "liveness_inconsistent",
                            "reviewer quota did not recover inside the 24-hour wait",
                            act=True,
                        )
                        steps += 1
                        continue
                    # Nobody legal for this stage yet. Wait for one of the
                    # reviewers who are actually out, not a stale stored reset
                    # for someone who has already recovered.
                    resume_at = _earliest_reset(live_quota) or self._quota_resume_at(
                        dispatch, line
                    )
                    until = min(
                        item for item in (resume_at, cap) if item is not None
                    )
                    if until > moment:
                        snapshot.update(
                            liveness_record.store(
                                snapshot,
                                record.with_schedule(
                                    at=moment,
                                    next_at=until,
                                    next_action=liveness_policy.REVIEW,
                                ),
                            )
                        )
                        changed = True
                        steps += 1
                        continue
                elif moment >= cap:
                    changed |= self._liveness_needs_human(
                        dispatch,
                        line,
                        snapshot,
                        moment,
                        policy,
                        "liveness_inconsistent",
                        "reviewer quota did not recover inside the 24-hour wait",
                        act=True,
                    )
                    steps += 1
                    continue
            waiting = (
                _split_waits_for(dispatch, line.id)
                if action == liveness_policy.SPLIT
                else 0
            )
            if waiting:
                snapshot.update(
                    liveness_record.store(
                        snapshot,
                        record.with_schedule(
                            at=moment,
                            next_at=moment
                            + timedelta(seconds=LIVENESS_SPLIT_WAIT_SECONDS),
                            next_action=liveness_policy.SPLIT,
                        ),
                    )
                )
                changed = True
                steps += 1
                continue
            result, detail, next_at, next_action = await self._apply_liveness_step(
                dispatch, line, record, step, policy, moment, action=action
            )
            # A reassign commits through the store and adopts the saved row,
            # replacing ``dispatch.gates``. Re-read the cell so this step's
            # record lands on the snapshot the drive will save.
            snapshot = gates.line_snapshot(dispatch, line)
            latched = liveness_record.load(snapshot)
            if latched is not None and latched.needs_human:
                # A step may refuse without touching the line (a split with
                # nothing left to integrate) and latch a person itself. Keep
                # that latch instead of overwriting it with this step.
                changed = True
                steps += 1
                continue
            updated = record.with_step(
                action,
                at=moment,
                result=result,
                detail=detail,
                next_at=next_at,
                next_action=next_action,
            )
            if result == liveness_record.APPLIED:
                if action == liveness_policy.SPLIT:
                    updated = updated.finish(liveness_record.SPLIT_OFF, at=moment)
                elif action in (liveness_policy.REASSIGN, liveness_policy.REVIEW):
                    updated = updated.finish(liveness_record.RECOVERED, at=moment)
            elif next_at is None:
                later_at, later_action = _liveness_schedule_after(
                    plan, updated.spent, moment, policy
                )
                updated = updated.with_schedule(
                    at=moment, next_at=later_at, next_action=later_action
                )
            snapshot.update(liveness_record.store(snapshot, updated))
            changed = True
            steps += 1
        if changed:
            await self._save(dispatch)
        return steps

    async def _safe_operator_sweep(self, dispatch: Dispatch) -> None:
        """Run queued recover requests; a failure is logged, never the drive's end."""
        try:
            await self._sweep_operator_requests(dispatch)
        except Exception:  # noqa: BLE001 - same rule as the liveness sweep
            logger.exception("Recover requests failed for dispatch %s", dispatch.id)

    async def _sweep_operator_requests(
        self, dispatch: Dispatch, *, now: datetime | str | float | int | None = None
    ) -> int:
        """Perform the ``dispatch recover`` requests left on parked lines (N76 L3).

        ``next`` runs the line's next recovery-table row now: its backoff and a
        quota wait are skipped. ``split`` runs R-SPLIT on the line. Either way
        the step is recorded on the line's liveness record like an automatic
        one and spends its budget. The marker is removed whatever happens, so
        a request runs once. Lines are read without creating gate cells, so a
        dispatch with no request is left exactly as it was.
        """
        from .operator import RECOVER_MARKER, RECOVER_SPLIT

        gates = self._c.gates
        root = dispatch.gates.get("lines") if isinstance(dispatch.gates, dict) else None
        if gates is None or not isinstance(root, dict):
            return 0
        marked = [
            line
            for line in dispatch.lines
            if isinstance(root.get(line.id), dict)
            and isinstance(root[line.id].get(RECOVER_MARKER), dict)
        ]
        if not marked:
            return 0
        policy = self._liveness_policy()
        moment = liveness_record.parse_time(now) or datetime.now(timezone.utc)
        handled = 0
        for line in marked:
            snapshot = gates.line_snapshot(dispatch, line)
            marker = snapshot.pop(RECOVER_MARKER, None) or {}
            handled += 1
            if line.status is not LineStatus.BLOCKED or line.current_run:
                logger.warning(
                    "Dispatch %s dropped recover of %s: line is %s",
                    dispatch.id,
                    line.id,
                    line.status.value,
                )
                _note_recover_outcome(dispatch, line, marker, "dropped", "line_not_parked")
                continue
            verdict = liveness_consistency.check(
                line.status, line.failure_reason, _snapshot_stage(snapshot)
            )
            if isinstance(verdict, liveness_consistency.Repair):
                transition = self._advance(
                    dispatch, line, "block", provider=line.assigned_model or ""
                )
                snapshot = gates.line_snapshot(dispatch, line)
                if not transition.accepted:
                    verdict = liveness_consistency.Inconsistent(
                        line.status,
                        line.failure_reason,
                        verdict.stage,
                        verdict.allowed,
                        detail=transition.reason or verdict.detail,
                    )
            if isinstance(verdict, liveness_consistency.Inconsistent):
                self._liveness_needs_human(
                    dispatch,
                    line,
                    snapshot,
                    moment,
                    policy,
                    "liveness_inconsistent",
                    verdict.detail,
                    act=True,
                )
                continue
            record = liveness_record.load(snapshot)
            if record is None or record.outcome != liveness_record.PENDING or (
                line.failure_reason is not None and record.reason != line.failure_reason
            ):
                record = self._liveness_record_for(
                    dispatch, line, snapshot, moment, policy, previous=record
                )
            if marker.get("mode") == RECOVER_SPLIT:
                action = liveness_policy.SPLIT
                step = liveness_policy.Step(liveness_policy.SPLIT, 1)
            else:
                plan = policy.plan_for(record.reason)
                index = _liveness_plan_index(record)
                if record.needs_human or record.budget_left <= 0 or index >= len(plan):
                    snapshot.update(liveness_record.store(snapshot, record))
                    self._liveness_needs_human(
                        dispatch,
                        line,
                        snapshot,
                        moment,
                        policy,
                        record.reason,
                        "recover: no automatic step is left",
                        act=True,
                    )
                    continue
                step = plan[index]
                action = step.action
                if action == liveness_policy.WAIT:
                    # The operator skips the wait: resume the review now.
                    action = liveness_policy.REVIEW
                if (
                    action == liveness_policy.REVIEW
                    and record.reason in _WORKER_ASSIGNMENT_REASONS
                ):
                    action = liveness_policy.REASSIGN
            if action == liveness_policy.SPLIT and _split_waits_for(dispatch, line.id):
                # The request was checked when it was made; a line that is in
                # flight again since then makes the split wait. Say so.
                snapshot.update(
                    liveness_record.store(
                        snapshot,
                        record.with_schedule(
                            at=moment,
                            next_at=moment + timedelta(seconds=LIVENESS_SPLIT_WAIT_SECONDS),
                            next_action=liveness_policy.SPLIT,
                        ),
                    )
                )
                _note_recover_outcome(dispatch, line, marker, "dropped", "split_waits")
                continue
            snapshot.update(liveness_record.store(snapshot, record))
            result, detail, next_at, next_action = await self._apply_liveness_step(
                dispatch, line, record, step, policy, moment, action=action
            )
            snapshot = gates.line_snapshot(dispatch, line)
            latched = liveness_record.load(snapshot)
            if latched is not None and (latched.needs_human or latched.outcome != record.outcome):
                # The split (or its refusal) already closed or latched the
                # record; add the operator's step to that one.
                record = latched
            updated = record.with_step(
                action,
                at=moment,
                result=result,
                detail=f"operator recover: {detail}",
                next_at=next_at,
                next_action=next_action,
            )
            if result == liveness_record.APPLIED and updated.outcome == liveness_record.PENDING:
                if action == liveness_policy.SPLIT:
                    updated = updated.finish(liveness_record.SPLIT_OFF, at=moment)
                else:
                    updated = updated.finish(liveness_record.RECOVERED, at=moment)
            elif result != liveness_record.APPLIED and next_at is None and not updated.needs_human:
                later_at, later_action = _liveness_schedule_after(
                    policy.plan_for(record.reason), updated.spent, moment, policy
                )
                updated = updated.with_schedule(
                    at=moment, next_at=later_at, next_action=later_action
                )
            snapshot.update(liveness_record.store(snapshot, updated))
        await self._save(dispatch)
        return handled

    async def _sweep_open_line(
        self,
        dispatch: Dispatch,
        line: Line,
        snapshot: dict[str, Any],
        moment: datetime,
        policy: liveness_policy.Policy,
        *,
        act: bool,
    ) -> bool:
        """Check one non-BLOCKED open line: stage drift, then ownerlessness.

        The parked-line table never sees a RUNNING line, so a line that lost
        its run (a crash before the run record, a resume that never started a
        review) would stay ownerless forever. This covers it without taking a
        live run from the monitor: a line with ``current_run`` set, or a
        settled review resume, is left alone. A line with drift or no owner is
        parked ``liveness_inconsistent`` and handed to a person; ``shadow``
        only records the observation, and ``off`` never reaches here.
        """
        gates = self._c.gates
        assert gates is not None
        stage = _snapshot_stage(snapshot)
        verdict = liveness_consistency.check(line.status, line.failure_reason, stage)
        if isinstance(verdict, liveness_consistency.Inconsistent):
            if act:
                self._park_liveness_inconsistent(dispatch, line)
            return self._liveness_needs_human(
                dispatch,
                line,
                snapshot,
                moment,
                policy,
                "liveness_inconsistent",
                verdict.detail,
                act=act,
            )
        if line.status not in _OPEN_LIVENESS_STATUSES:
            return False
        if line.current_run:
            # The monitor owns a line with a run; a run id alone is not proof
            # of life, but taking it here would race the poll loop.
            return False
        if stage in _LIVENESS_REVIEW_STAGES and gates.run_is_settled(
            dispatch, line
        ):
            # ``_resume_review_from_park`` leaves RUNNING with no worker run
            # and marks the review settled. The review is the next step, not
            # the absence of an owner.
            return False
        decision = liveness_owner.decide_owner(
            self._liveness_view(dispatch, line, snapshot), moment
        )
        if not decision.ownerless:
            return False
        if act:
            self._park_liveness_inconsistent(dispatch, line)
        where = f"stage {stage.value}" if stage is not None else "no stage"
        return self._liveness_needs_human(
            dispatch,
            line,
            snapshot,
            moment,
            policy,
            "liveness_inconsistent",
            f"{line.status.value} line has no owner ({where})",
            act=act,
        )

    def _park_liveness_inconsistent(self, dispatch: Dispatch, line: Line) -> None:
        """Park an open line the sweep cannot assign an owner to."""
        self._block_line(dispatch, line, "liveness_inconsistent")
        transition = self._advance(
            dispatch, line, "block", provider=line.assigned_model or ""
        )
        if not transition.accepted:
            logger.warning(
                "Stage-Gate refused the liveness block on %s: %s",
                line.id,
                transition.reason,
            )

    async def _apply_liveness_step(
        self,
        dispatch: Dispatch,
        line: Line,
        record: liveness_record.LivenessRecord,
        step: liveness_policy.Step,
        policy: liveness_policy.Policy,
        moment: datetime,
        *,
        action: str,
    ) -> tuple[str, str, datetime | None, str | None]:
        """Perform one recovery-table row. Returns the step's recorded result.

        ``action`` is the effective action, which may differ from ``step.action``:
        live headroom can turn the quota ``wait`` into ``review`` before the row
        runs, and a worker-capacity park retries assignment rather than a review.
        Executing ``step.action`` instead of ``action`` was the N76 repair bug:
        the record moved to the new step while the line was left parked.
        """
        if action == liveness_policy.WAIT:
            until = self._quota_resume_at(dispatch, line)
            parked = liveness_record.parse_time(record.parked_at) or moment
            cap = parked + timedelta(seconds=liveness_policy.QUOTA_WAIT_SECONDS)
            if until is None:
                until = moment + timedelta(seconds=min(float(step.budget or 0), 3600.0))
            if until > cap:
                until = cap
            return (
                liveness_record.APPLIED,
                f"wait for reviewer headroom until {liveness_record.format_time(until)}",
                until,
                liveness_policy.REVIEW,
            )
        if action == liveness_policy.REVIEW:
            if record.reason in _WORKER_ASSIGNMENT_REASONS:
                if not await self._liveness_reassign(dispatch, line, record.reason):
                    return (
                        liveness_record.REFUSED,
                        "no eligible worker could be assigned",
                        None,
                        None,
                    )
                return (liveness_record.APPLIED, "worker assigned", None, None)
            if not self._resume_review_from_park(
                dispatch,
                line,
                alternate=_review_row_changes_reviewer(record, policy),
            ):
                return (
                    liveness_record.REFUSED,
                    "Stage-Gate refused to resume the review",
                    None,
                    None,
                )
            return (liveness_record.APPLIED, "review resumed", None, None)
        if action == liveness_policy.REASSIGN:
            if not await self._liveness_reassign(dispatch, line, record.reason):
                return (
                    liveness_record.REFUSED,
                    "no eligible author could be assigned",
                    None,
                    None,
                )
            return (liveness_record.APPLIED, "author reassigned", None, None)
        if action == liveness_policy.SPLIT:
            victims = await self._split_off_line(
                dispatch, line, record.reason, moment, persist=False
            )
            if not victims:
                return (
                    liveness_record.REFUSED,
                    "R-SPLIT refused; a person owns the line",
                    None,
                    None,
                )
            return (liveness_record.APPLIED, "line split off", None, None)
        return (liveness_record.SKIPPED, f"unknown action {action}", None, None)

    async def _live_review_blockers(
        self, dispatch: Dispatch, line: Line
    ) -> dict[str, object] | None:
        """Current reviewer-headroom blockers for this line, or None if unknown.

        The parked ``reviewers_quota_exhausted`` snapshot is a snapshot: a
        provider may regain headroom, or a paid plan may become available,
        before its recorded reset. A later drive asks the selector again.
        """
        review_blockers = getattr(self._c.selector, "review_blockers", None)
        if not callable(review_blockers):
            return None
        try:
            found = await self._leave(review_blockers(line, []))
        except Exception as exc:  # advisory: the stored timer still works
            logger.warning(
                "Could not read reviewer headroom: %s", type(exc).__name__
            )
            return None
        if not isinstance(found, dict):
            return None
        return {
            str(name): dict(entry)
            for name, entry in found.items()
            if isinstance(entry, dict)
        }

    async def _review_stage_available(
        self,
        dispatch: Dispatch,
        line: Line,
        blockers: dict[str, object] | None,
    ) -> bool:
        """Whether this line's review stage can seat someone right now.

        ``review_blockers`` lists exhausted reviewers. A non-empty map is not
        proof that the stage has nobody: a primary may have recovered while
        another reviewer is still out. An unknown map (the selector has no
        headroom view) leaves the stored timer in charge.
        """
        if not isinstance(blockers, dict):
            return False
        if not blockers:
            return True
        from src.server.services.autonomy.roles import canonical_provider

        def canon(name: object) -> str:
            text = str(name or "").strip()
            if not text:
                return ""
            return canonical_provider(text).casefold()

        exhausted = {canon(name) for name in blockers}
        exhausted.discard("")
        exhausted.update(canon(name) for name in self._skipped_providers(dispatch))
        gates = self._c.gates
        state = (
            _load_state(gates.line_snapshot(dispatch, line))
            if gates is not None
            else None
        )
        barred = {canon(name) for name in self._line_holders(dispatch, line)}
        if _quota_stage_is_secondary(state) and state is not None:
            barred.add(canon(state.primary_reviewer))
        barred.discard("")
        roster = _review_roster(self._c.selector)
        if roster:
            return any(
                canon(name) not in exhausted and canon(name) not in barred
                for name in roster
            )
        chosen = await self._probe_review_stage(dispatch, line, barred | exhausted)
        if not chosen:
            return False
        key = canon(chosen)
        return bool(key) and key not in exhausted and key not in barred

    async def _probe_review_stage(
        self, dispatch: Dispatch, line: Line, barred: set[str]
    ) -> str | None:
        """One review selection, undone, for a selector that has no roster.

        The production selector already skips models without headroom. A fake
        that ignores exclusions is checked again against ``barred``.
        """
        selector = self._c.selector
        had_refusal = hasattr(selector, "review_refusal")
        refusal = getattr(selector, "review_refusal", None)
        exclude = [name for name in barred if name]
        try:
            used = self._selection_used(
                dispatch, line, exclude, role=ModelRole.REVIEW
            )
            try:
                chosen = await self._select_model(
                    dispatch, line, used, role=ModelRole.REVIEW, never=exclude
                )
            except Exception as exc:  # a probe must not fail the sweep
                logger.warning(
                    "Could not probe reviewer availability: %s", type(exc).__name__
                )
                return None
        finally:
            if had_refusal:
                selector.review_refusal = refusal
        if chosen and hasattr(selector, "release"):
            selector.release(chosen)
        return chosen

    def _quota_resume_at(self, dispatch: Dispatch, line: Line) -> datetime | None:
        gates = self._c.gates
        if gates is None:
            return None
        snapshot = gates.line_snapshot(dispatch, line)
        raw = snapshot.get(REVIEWERS_QUOTA_EXHAUSTED)
        moments: list[datetime] = []
        if isinstance(raw, dict):
            for entry in raw.values():
                if not isinstance(entry, dict):
                    continue
                parsed = liveness_record.parse_time(entry.get("resets_at"))
                if parsed is not None:
                    moments.append(parsed)
        return min(moments) if moments else None

    def _resume_review_from_park(
        self, dispatch: Dispatch, line: Line, *, alternate: bool = False
    ) -> bool:
        """Unblock a review park and put the line back in front of a reviewer.

        The first R-REVIEW row retries the stored reviewer. A later review row
        keeps the failed identity and the pick skips it. R-QUOTA's review
        follows a wait, so ``alternate`` is false and the stored reviewer stays.
        """
        gates = self._c.gates
        if gates is None:
            return False
        snapshot = gates.line_snapshot(dispatch, line)
        state = _load_state(snapshot)
        if state.stage.terminal:
            return False
        if alternate:
            _remember_excluded_reviewers(snapshot, state)
        else:
            # A stale exclusion from an earlier episode must not force a
            # reviewer change on the row that retries the same one.
            snapshot.pop(_LIVENESS_EXCLUDED_REVIEWERS, None)
        if state.stage is Stage.BLOCKED:
            transition = self._advance(
                dispatch, line, "resume", provider=line.assigned_model or ""
            )
            if not transition.accepted:
                return False
        snapshot.pop(REVIEW_PARKED, None)
        snapshot.pop(REVIEWERS_QUOTA_EXHAUSTED, None)
        snapshot.pop("review_attempts", None)
        _clear_review_retry(snapshot)
        line.status = LineStatus.RUNNING
        line.failure_reason = None
        line.current_run = None
        # There is no worker to poll: the review itself is the next step. Mark
        # the line's run bookkeeping settled (in memory and on the scheduler
        # snapshot) so a fresh drive routes it to ``_review_gated`` instead of
        # watching a run that does not exist. ``_runs`` is the machine's own
        # field; writing it directly keeps the settled marker visible even
        # before ``_install_owners`` has run.
        gates.mark_run_settled(dispatch, line)
        run = self._runs.setdefault(
            line.id, _Run(started_at=self._clock(), started_wall=self._cost_wall())
        )
        run.complete = True
        return True

    async def _liveness_reassign(
        self, dispatch: Dispatch, line: Line, reason: str
    ) -> bool:
        """R-EXIT/R-REPLAN: give the parked line a new author, honouring the cap.

        The author swap goes through the same Stage-Gate transition as a human
        reassign (``replan_decision replace_developer`` out of REPLANNING), so
        the gate is legal before the line is relaunched.
        """
        if line.attempts + 1 > self._config.max_reassigns:
            return False
        # A reassign commits from the stored row and adopts it. Flush this
        # sweep first: a copying store would otherwise drop an earlier line's
        # needs_human latch, and the next sweep would notify again.
        await self._save(dispatch)
        blockers = list(line.blockers)
        # The outgoing author is a hard exclusion. ``used_models`` is not:
        # the production work selector ignores it and would seat the same
        # author again, and Stage-Gate then refuses the swap.
        outgoing = line.assigned_model or ""
        model = await self._select_with_retry(
            dispatch, line, exclude=[outgoing] if outgoing else None
        )
        if model is None:
            return False
        gates = self._c.gates
        gated = gates is not None and isinstance(dispatch.gates, dict) and bool(
            dispatch.gates.get("lines")
        )
        if gated:
            assert gates is not None
            handoff = await self._leave(_reassign_handoff(dispatch, line, gates))
            if not handoff.complete and not _work_was_launched(
                dispatch, line, gates, handoff
            ):
                # The line never launched, so there is no outgoing work to
                # read. A reserved branch name is not that work: the planner
                # writes it before anyone is chosen. The new author is told
                # that explicitly instead of the swap being refused as an
                # incomplete checkpoint.
                handoff = Handoff(
                    branch=f"{dispatch.branch_prefix}/line-{line.id}",
                    commit="unknown",
                    files=line.write_scope or ("(none recorded)",),
                    test_command="not run",
                    results="no work was delivered",
                    known_limitations="automatic liveness reassign",
                )
            refusal = await self._leave(
                human_reassign_refusal(
                    dispatch, line, model, gates, handoff=handoff
                )
            )
            if refusal:
                logger.info(
                    "Liveness reassign refused for %s: %s", line.id, refusal
                )
                return False
        decision = await self._submit(self._reassign_prepare, dispatch, line, reason)
        if decision != "select":
            return False
        saved = bool(
            await self._submit(
                self._reassign_finish, dispatch, line, reason, model, force=True
            )
        )
        if saved:
            # The brief is keyed by the new author and the bumped attempt, so
            # it is written after the swap. The launch reads it next.
            _write_reassign_brief(
                dispatch, line, blockers, line.failure_reason or reason, escalation=True
            )
        return saved

    async def _split_off_line(
        self,
        dispatch: Dispatch,
        line: Line,
        reason: str,
        moment: datetime,
        *,
        persist: bool = True,
    ) -> list[str]:
        """R-SPLIT: remove a line and its DAG dependants from this round.

        Each removed line keeps its work on its branch and gets a follow-up
        draft under ``<runs_dir>/../followups``. The split list is persisted
        under ``outcomes["split_off"]`` for the report and the approval gate.

        Two cases are not a split: a round with no PASSED line left to
        integrate, and an atomic group that would lose only one member. Both
        hand the line to a person before anything is mutated.
        """
        victims = _split_off_victims(dispatch, line.id)
        victim_ids = {victim.id for victim in victims}
        remaining = [
            item
            for item in dispatch.lines
            if item.id not in victim_ids and item.status is not LineStatus.FAILED
        ]
        if not any(item.status is LineStatus.PASSED for item in remaining):
            self._refuse_split(
                dispatch,
                line,
                moment,
                "no PASSED line would remain to integrate after the split",
            )
            if persist:
                await self._save(dispatch)
            return []
        atomic = _split_off_breaks_atomic(dispatch, victim_ids)
        if atomic:
            self._refuse_split(
                dispatch,
                line,
                moment,
                f"atomic group {', '.join(sorted(atomic))} cannot be split",
            )
            if persist:
                await self._save(dispatch)
            return []
        raw = dispatch.outcomes.get(SPLIT_OFF)
        split = raw if isinstance(raw, SplitOffOutcome) else SplitOffOutcome()
        gates = self._c.gates
        root = _liveness_followups_root(gates)
        for victim in victims:
            snapshot = (
                gates.line_snapshot(dispatch, victim)
                if gates is not None
                else {}
            )
            record = liveness_record.load(snapshot)
            if record is not None:
                snapshot.update(
                    liveness_record.store(
                        snapshot,
                        record.finish(liveness_record.SPLIT_OFF, at=moment),
                    )
                )
            own_reason = victim.failure_reason
            victim.status = LineStatus.FAILED
            victim.failure_reason = SPLIT_OFF
            victim.current_run = None
            path = root / f"{dispatch.id}-{victim.id}.md"
            _write_split_followup(
                path,
                dispatch,
                victim,
                snapshot,
                reason,
                moment,
                parked_reason=(
                    own_reason
                    if victim.id == line.id
                    else f"dependant of {line.id} ({own_reason or 'not parked'})"
                ),
            )
            split.add(
                victim.id,
                reason=SPLIT_OFF,
                parked_reason=reason,
                branch=victim.branch,
                followup=str(path),
                atomic_with=list(victim.atomic_with),
                at=liveness_record.format_time(moment),
            )
        dispatch.outcomes[SPLIT_OFF] = split
        if not any(item.status is LineStatus.PASSED for item in dispatch.lines):
            dispatch.state = DispatchState.REVIEW
            dispatch.message = (
                f"Every line of dispatch {dispatch.id} was split off; "
                f"{len(victims)} follow-up draft(s) need a person. "
                f"See {root}."
            )
        if persist:
            await self._save(dispatch)
        return [victim.id for victim in victims]

    def _refuse_split(
        self,
        dispatch: Dispatch,
        line: Line,
        moment: datetime,
        detail: str,
    ) -> None:
        """Hand a refused R-SPLIT to a person before any line is mutated."""
        gates = self._c.gates
        line.blockers = [
            *[str(item) for item in line.blockers],
            f"R-SPLIT refused: {detail}. The line stays in this round.",
        ]
        dispatch.state = DispatchState.REVIEW
        dispatch.message = (
            f"R-SPLIT refused for line {line.id}: {detail}. "
            "A person must decide whether to split it."
        )
        if gates is not None:
            self._liveness_needs_human(
                dispatch,
                line,
                gates.line_snapshot(dispatch, line),
                moment,
                self._liveness_policy(),
                line.failure_reason or "liveness_inconsistent",
                detail,
                act=True,
            )

    async def _park_g5_failure(self, dispatch: Dispatch, step: Any) -> None:
        """G5 failure is a park in enforce, and main's ERROR stop otherwise.

        The lines stay in this round rather than becoming a terminal dispatch,
        so ``recover`` and ``cancel`` still work. A person owns the line; the
        follow-up is either a G5 configuration fix or a real test failure.
        ``shadow`` records the hand-to-a-person it would make but keeps main's
        line statuses and dispatch state; ``off`` is main exactly.
        """
        raw_reason = str(getattr(step, "reason", "") or "g5_full_suite_failed")
        reason = (
            raw_reason
            if raw_reason in _G5_PARK_REASONS
            else "g5_full_suite_failed"
        )
        detail = str(getattr(step, "check", "") or "G5 integration checks failed")
        gates = self._c.gates
        mode = self._liveness_mode()
        if not liveness_modes.should_act(mode):
            # Main's pre-N76 behaviour: the dispatch is terminal ERROR and the
            # line keeps the gate's raw reason. Shadow only adds a record.
            for line in dispatch.lines:
                if line.status is LineStatus.FAILED:
                    continue
                if line.status is LineStatus.PASSED and gates is not None:
                    self._advance(
                        dispatch,
                        line,
                        "gate_result",
                        gate="G5",
                        result=GateResult(outcome="fail", reason=raw_reason),
                    )
                line.status = LineStatus.BLOCKED
                line.failure_reason = raw_reason
            dispatch.state = DispatchState.ERROR
            dispatch.message = (
                f"G5 integration checks failed on {dispatch.integrate_branch}: "
                f"{step.check}"
            )
            logger.error("Dispatch %s failed G5", dispatch.id)
            if mode == liveness_modes.SHADOW and gates is not None:
                policy = self._liveness_policy()
                moment = datetime.now(timezone.utc)
                for line in dispatch.lines:
                    if line.status is not LineStatus.BLOCKED:
                        continue
                    self._liveness_needs_human(
                        dispatch,
                        line,
                        gates.line_snapshot(dispatch, line),
                        moment,
                        policy,
                        reason,
                        detail,
                        act=False,
                    )
            await self._save(dispatch)
            return
        policy = self._liveness_policy()
        moment = datetime.now(timezone.utc)
        for line in dispatch.lines:
            if line.status is LineStatus.FAILED:
                continue
            if line.status is LineStatus.PASSED and gates is not None:
                self._advance(
                    dispatch,
                    line,
                    "gate_result",
                    gate="G5",
                    result=GateResult(outcome="fail", reason=reason),
                )
            self._block_line(dispatch, line, reason)
            if gates is not None:
                snapshot = gates.line_snapshot(dispatch, line)
                state = _load_state(snapshot)
                if state.stage is not Stage.BLOCKED and not state.stage.terminal:
                    self._advance(
                        dispatch, line, "block", provider=line.assigned_model or ""
                    )
                    snapshot = gates.line_snapshot(dispatch, line)
                self._liveness_needs_human(
                    dispatch,
                    line,
                    snapshot,
                    moment,
                    policy,
                    reason,
                    detail,
                    act=True,
                )
        dispatch.state = DispatchState.REVIEW
        blocked = [line for line in dispatch.lines if line.status is LineStatus.BLOCKED]
        dispatch.message = _gated_block_message(blocked, dispatch.id)
        await self._save(dispatch)

    def _seed_liveness_record(self, dispatch: Dispatch, line: Line) -> None:
        """Give a line parked by a state-machine branch its first owner."""
        gates = self._c.gates
        if gates is None:
            return
        snapshot = gates.line_snapshot(dispatch, line)
        if liveness_record.load(snapshot) is not None:
            return
        moment = datetime.now(timezone.utc)
        policy = self._liveness_policy()
        fresh = self._liveness_record_for(dispatch, line, snapshot, moment, policy)
        snapshot.update(liveness_record.store(snapshot, fresh))

    def _park_git_evidence_unavailable(self, dispatch: Dispatch, line: Line) -> None:
        """Missing git evidence is a park the sweep can split, not a dead ERROR."""
        line.blockers = [
            "Git evidence for this line is unavailable, so it cannot be "
            "reviewed or integrated from here. The liveness sweep splits it "
            "out of this round; or run "
            f"`conductor dispatch recover {dispatch.id} {line.id}`."
        ]
        self._block_line(dispatch, line, "git_evidence_unavailable")
        self._advance_park(dispatch, line)
        self._seed_liveness_record(dispatch, line)

    def _park_no_eligible_worker(self, dispatch: Dispatch, line: Line) -> None:
        """A write line nobody can staff is a park, not a terminal dispatch."""
        line.blockers = [
            f"No eligible worker can write this line ({NO_ELIGIBLE_WORKER}): the "
            "pool is exhausted or every remaining Worker is text-only. Add "
            f"capacity or run `conductor dispatch reassign {dispatch.id} "
            f"{line.id} <model>`."
        ]
        self._block_line(dispatch, line, NO_ELIGIBLE_WORKER)
        self._advance_park(dispatch, line)
        self._seed_liveness_record(dispatch, line)

    def _park_no_secondary_reviewer(self, dispatch: Dispatch, line: Line) -> None:
        """The selector refused a reviewer: park for the capacity wait (N76)."""
        line.blockers = [
            "No reviewer model is available for this line "
            f"({NO_SECONDARY_REVIEWER}). When one is back, the liveness sweep "
            "resumes the review; or run "
            f"`conductor dispatch reassign {dispatch.id} {line.id} <model>`."
        ]
        self._block_line(dispatch, line, NO_SECONDARY_REVIEWER)
        self._advance_park(dispatch, line)
        self._seed_liveness_record(dispatch, line)

    def _advance_park(self, dispatch: Dispatch, line: Line) -> None:
        gates = self._c.gates
        if gates is None:
            return
        state = _load_state(gates.line_snapshot(dispatch, line))
        if state.stage is not Stage.BLOCKED and not state.stage.terminal:
            self._advance(dispatch, line, "block", provider=line.assigned_model or "")

    async def _watchdog(self, dispatch: Dispatch, running: list[Line]) -> None:
        if not self._on_actor():
            await self._submit(self._watchdog, dispatch, running)
            return
        gates = self._c.gates
        assert gates is not None
        if running and all(line.failure_reason == "watchdog_timeout" for line in running):
            return
        sched = gates._scheduler(dispatch)
        audit = sched.setdefault("audit", [])
        if not isinstance(audit, list):
            audit = []
            sched["audit"] = audit
        moment = gates.stamp()
        for line in running:
            self._block_line(dispatch, line, "watchdog_timeout")
            audit.append(
                {
                    "kind": "watchdog_timeout",
                    "line_id": line.id,
                    "state": DispatchState.REVIEW.value,
                    "at": moment,
                }
            )
        dispatch.state = DispatchState.REVIEW
        dispatch.message = _gated_block_message(
            [line for line in dispatch.lines if line.status is LineStatus.BLOCKED],
            dispatch.id,
        )
        await self._save(dispatch)

    async def _g5_then_gate(self, dispatch: Dispatch) -> None:
        gates = self._c.gates
        assert gates is not None
        if await self._fence(dispatch, claim=True):
            return
        if gates.integration_phase(dispatch) == "passed":
            if dispatch.state is not DispatchState.APPROVAL:
                await self._open_g6(dispatch)
            return
        if gates.integration_phase(dispatch) == "failed":
            return
        gates.mark_merging(dispatch)
        await self._save(dispatch)
        if await self._fence(dispatch, claim=True):
            return
        merged = set(gates.merged_ids(dispatch))
        for line in topological_lines(dispatch.lines):
            if line.status is not LineStatus.PASSED or line.id in merged:
                continue
            try:
                await self._leave(self._c.merger.merge_line(line.id))
            except MergeConflictError as exc:
                if await self._fence(dispatch):
                    return
                gates.mark_merge_conflict(dispatch)
                dispatch.state = DispatchState.ABORT
                dispatch.message = str(exc)
                logger.warning("Dispatch %s aborted on a merge conflict", dispatch.id)
                await self._save(dispatch)
                return
            gates.note_merged(dispatch, line.id)
            merged.add(line.id)
            await self._save(dispatch)
            if await self._fence(dispatch, claim=True):
                return
        if await self._fence(dispatch):
            return
        gates.mark_running(dispatch)
        await self._save(dispatch)
        if await self._fence(dispatch):
            return
        tokens = {item.id: _result_token(item) for item in dispatch.lines}

        async def _finish_g5(
            full: dict[str, object],
            alembic: dict[str, object],
            reason: str,
            check: str,
        ) -> object:
            return await self._finish_g5_on_writer(dispatch, full, alembic, reason, check)

        step = await self._leave(gates.run_g5(dispatch, finish=_finish_g5))
        if await self._fence(dispatch):
            return
        if any(_result_token(item) != tokens.get(item.id) for item in dispatch.lines):
            await self._discard_stale(dispatch)
            return
        if not step.passed:
            logger.warning("Dispatch %s failed G5; parking for a person", dispatch.id)
            await self._park_g5_failure(dispatch, step)
            return
        for line in dispatch.lines:
            if line.status is not LineStatus.PASSED:
                continue
            self._advance(
                dispatch,
                line,
                "gate_result",
                gate="G5",
                result=GateResult(outcome="pass", reason=step.reason),
            )
        await self._open_g6(dispatch)

    async def _open_g6(self, dispatch: Dispatch) -> None:
        if await self._fence(dispatch):
            return
        gates = self._c.gates
        if gates is not None:
            gates.mark_awaiting_human(dispatch)
        await self._gate(dispatch)
        if self._abandoned:
            return
        if gates is not None:
            gates.mark_awaiting_human(dispatch)
        dispatch.message = (
            f"Integrate branch {dispatch.integrate_branch} passed G5; awaiting human approval"
        )
        await self._save(dispatch)
        # N73 S7: a low-risk dispatch is marked auto_ready (no push, no PR).
        await self._auto_approve(dispatch)

    async def _auto_approve(self, dispatch: Dispatch) -> bool:
        """Mark a low-risk dispatch ``auto_ready``; never open the human gate.

        Conditions: G2, a non-author MERGE from each required reviewer and G5
        passed, complete changed-file evidence, no scope/worktree/record-only
        finding, and no high-risk path. INCONCLUSIVE, ERROR, an unparseable
        reply, a park and a human unblock of ``review_unavailable`` stay
        human. The switch is off unless the service turns it on
        (``CONDUCTOR_DISPATCH_AUTO_APPROVE=1``).

        What it does: records ``gates.auto_approval = {state: auto_ready,
        approver: auto:n73}`` and an audit entry. The approval gate stays
        PENDING, so ``GitMerger.push`` and delivery stay refused; the lines
        are already in the local integrate branch (G5 ran on it). Push and PR
        remain a human approval.
        """
        gates = self._c.gates
        if gates is None or not dispatch.approval_token or not self._c.auto_approve:
            return False
        from .approval import AUTO_APPROVAL_KEY, AUTO_APPROVE_AUDIT, AUTO_READY, auto_approval

        decision = auto_approval(dispatch)
        audit = gates._scheduler(dispatch).setdefault("audit", [])
        if isinstance(audit, list):
            audit.append(
                {
                    "kind": AUTO_APPROVE_AUDIT,
                    "approve": decision.approve,
                    "reason": decision.reason,
                    "approver": decision.approver,
                    "at": gates.stamp(),
                }
            )
        if not decision.approve:
            await self._save(dispatch)
            return False
        dispatch.gates[AUTO_APPROVAL_KEY] = {
            "state": AUTO_READY,
            "approver": decision.approver,
            "branch": dispatch.integrate_branch,
            "at": gates.stamp(),
        }
        dispatch.message = (
            f"Integrate branch {dispatch.integrate_branch} passed G5; marked "
            f"{AUTO_READY} by {decision.approver}. Push and PR need a human approval."
        )
        await self._save(dispatch)
        return True

    async def _complete_already_merged(self, dispatch: Dispatch) -> None:
        """The lines were merged before G5, so approval must not merge them again."""
        if await self._fence(dispatch):
            return
        gates = self._c.gates
        if gates is not None:
            for line in dispatch.lines:
                if line.status is LineStatus.FAILED:
                    continue
                self._advance(
                    dispatch,
                    line,
                    "gate_result",
                    gate="G6",
                    result=GateResult(outcome="pass", reason="human_approved"),
                )
            gates.mark_approved(dispatch)
        if await self._fence(dispatch):
            return
        await self._leave(self._c.outcomes.record(dispatch.id, dispatch.outcomes))
        if await self._fence(dispatch):
            return
        dispatch.state = DispatchState.COMPLETE
        dispatch.message = f"Integrate branch {dispatch.integrate_branch} is ready for review"
        await self._save(dispatch)

    async def _park_blocked(self, dispatch: Dispatch) -> None:
        if await self._fence(dispatch):
            return
        dispatch.state = DispatchState.REVIEW
        blocked = [line for line in dispatch.lines if line.status is LineStatus.BLOCKED]
        dispatch.message = _gated_block_message(blocked, dispatch.id)
        await self._save(dispatch)

    async def _merge(self, dispatch: Dispatch) -> None:
        if await self._fence(dispatch, claim=True):
            return
        dispatch.state = DispatchState.MERGE
        await self._save(dispatch)
        if await self._fence(dispatch, claim=True):
            return
        for line in dispatch.lines:
            if line.status is not LineStatus.PASSED:
                continue
            try:
                await self._leave(self._c.merger.merge_line(line.id))
            except MergeConflictError as exc:
                # A conflict is the dispatch's own doing, not the machine's, so
                # it aborts with the conflicting paths rather than erroring.
                if await self._fence(dispatch):
                    return
                dispatch.state = DispatchState.ABORT
                dispatch.message = str(exc)
                logger.warning("Dispatch %s aborted on a merge conflict: %s", dispatch.id, exc)
                await self._save(dispatch)
                return
        if await self._fence(dispatch):
            return
        await self._leave(self._c.outcomes.record(dispatch.id, dispatch.outcomes))
        if await self._fence(dispatch):
            return
        dispatch.state = DispatchState.COMPLETE
        dispatch.message = f"Integrate branch {dispatch.integrate_branch} is ready for review"
        await self._save(dispatch)

    # -- resuming a parked dispatch ------------------------------------------

    async def _resume(self, dispatch: Dispatch) -> None:
        decision = (
            await self._leave(self._c.gate.decision(dispatch.approval_token or ""))
            if dispatch.state is DispatchState.APPROVAL
            else ApprovalDecision.PENDING
        )
        if decision is ApprovalDecision.REJECTED:
            if await self._fence(dispatch):
                return
            await self._leave(self._c.merger.set_aside())
            if self._c.gates is not None:
                for line in dispatch.lines:
                    self._advance(
                        dispatch,
                        line,
                        "gate_result",
                        gate="G6",
                        result=GateResult(outcome="fail", reason="human_rejected"),
                    )
                self._c.gates.mark_rejected(dispatch)
            dispatch.state = DispatchState.ABORT
            dispatch.message = "A human rejected this dispatch; the integrate branch was moved aside"
            await self._save(dispatch)
            return
        if decision is ApprovalDecision.APPROVED:
            if _g5_already_passed(dispatch, self._c.gates):
                await self._complete_already_merged(dispatch)
                return
            await self._merge(dispatch)
            return
        # Still parked on the gate, or parked on a blocked review.
        if dispatch.state is DispatchState.REVIEW:
            if self._c.gates is not None:
                await self._run_gated(dispatch)
                return
            if any(line.status is LineStatus.RUNNING for line in dispatch.lines):
                await self._review(dispatch)
                return
            reassigned = [line for line in dispatch.lines if line.status is LineStatus.ASSIGNED]
            if reassigned:
                await self._launch(dispatch)
                if not self._stopped(dispatch):
                    await self._monitor(dispatch)
                if not self._stopped(dispatch):
                    await self._review(dispatch)

    # -- line-level helpers ---------------------------------------------------

    async def _begin_fix(
        self,
        dispatch: Dispatch,
        line: Line,
        kind: str,
        author: str,
        attempt: int,
        source_ref: str,
        *,
        task: str,
        g2_note: str = "",
    ) -> None:
        """Write one fix intent, then enter the guarded launch.

        Review fixes, G2 fixes, deferred fixes, and direct fixes all start
        here. The write happens before ``_launch_guarded`` reserves a run or
        defers behind a sibling, so that save already holds the intent.
        """
        gates = self._c.gates
        assert gates is not None
        # Invalidate before naming the source. A direct G2 fix otherwise
        # stores the failed cell's revision, and resume cannot match it.
        if await gates.invalidate_g2_async(dispatch, line, force=True):
            line.failure_reason = None
            source_ref = self._fix_source_ref(dispatch, line, kind) or source_ref
        elif not source_ref:
            source_ref = self._fix_source_ref(dispatch, line, kind)
        if author:
            line.assigned_model = author
        if isinstance(attempt, int) and not isinstance(attempt, bool):
            line.attempts = attempt
        self._remember_pending_fix(
            dispatch,
            line,
            kind=kind,
            author=line.assigned_model or "",
            attempt=line.attempts,
            source_ref=source_ref,
            g2_note=g2_note,
        )
        await self._launch_line(dispatch, line, task=task, force_g2=True)

    async def _reassign_task(self, dispatch: Dispatch, line: Line) -> str | None:
        """A human-reassigned line's prompt: its task plus the previous blockers.

        Built like a fix round's task, on the line's existing branch, so the
        new model fixes those findings instead of starting over. None when
        no reassign brief matches this attempt and author.
        """
        blockers = reassign_brief(dispatch, line)
        if blockers is None:
            return None
        # M2c (enforce): the quota handoff pack goes after the diff, before the task.
        pack = handoff_pack.prompt_section(_current_brief(dispatch, line))
        diff_item: str | None = None
        if reassign_brief_has_uncommitted(dispatch, line):
            diff = await self._uncommitted_diff(line)
            if diff:
                diff_item = (
                    "The previous model left these uncommitted changes in this "
                    "line's worktree. They are kept there: review them, commit "
                    "what belongs to the task and revert the rest.\n" + diff
                )
                if pack is None:
                    blockers = [*blockers, diff_item]
        gates = self._c.gates
        if gates is not None:
            handoff = await self._leave(_reassign_handoff(dispatch, line, gates))
        else:
            handoff = Handoff(
                branch=line.branch or "",
                commit="",
                files=(),
                test_command="",
                results="",
                known_limitations="human reassign",
            )
        # The rejected-tip lead is always the brief's first item (N79).
        rejected = bool(blockers) and str(blockers[0]).startswith(_REJECTED_LEAD)
        # A pending fix ruling stays the line's contract across a reassign: the
        # review gate checks the next report against it, so the new author must
        # be shown it, or a report that never saw the items is blocked for them.
        ruling = (
            _ruling_from_dict(gates.line_snapshot(dispatch, line).get(FIX_RULING))
            if gates is not None
            else None
        )
        return handoff_pack.with_pack(
            _fix_task(line.task, handoff, blockers, keep_blockers=rejected, ruling=ruling),
            pack,
            diff_item,
        )

    def _g2_note(self, dispatch: Dispatch, line: Line) -> str:
        """The note for the line's failed G2 cell, or "" (N27)."""
        gates = self._c.gates
        if gates is None:
            return ""
        cell = gates.line_snapshot(dispatch, line).get("gates")
        g2 = cell.get("G2") if isinstance(cell, dict) else None
        return g2_failure_note(g2, gates._settings.runs_dir)

    async def _uncommitted_diff(self, line: Line) -> str | None:
        """The bounded diff of the line's dirty worktree, when the launcher can read it."""
        reader = getattr(self._c.launcher, "uncommitted_diff", None)
        if not callable(reader):
            return None
        try:
            diff = await self._leave(reader(line, max_bytes=_UNCOMMITTED_DIFF_BYTES))
        except Exception as exc:  # the diff is context only; the relaunch goes on
            logger.warning("Could not read the uncommitted diff of %s: %s", line.id, type(exc).__name__)
            return None
        return diff if isinstance(diff, str) and diff.strip() else None

    def _fix_source_ref(self, dispatch: Dispatch, line: Line, kind: str) -> str:
        """The review handoff, or the G2 revision once that cell has one."""
        gates = self._c.gates
        if gates is None:
            return ""
        snap = gates.line_snapshot(dispatch, line)
        handoff_ref = str(snap.get("handoff_ref") or "")
        if kind == "g2":
            revision = _g2_cell(snap).get("revision")
            if isinstance(revision, str) and revision:
                return revision
        return handoff_ref

    async def _launch_line(
        self,
        dispatch: Dispatch,
        line: Line,
        *,
        task: str | None = None,
        force_g2: bool = False,
        fix: dict[str, object] | None = None,
    ) -> None:
        """Prepare on the writer, then let a worker call the launcher.

        ``clear_run_settled``, the stage move, and the run reservation are
        state writes. They stay on the drive even when a poll worker noticed
        that a replacement launch is required. A fix intent is written by
        ``_begin_fix`` before this method runs. ``fix`` is not a second copy.
        """
        if not self._on_actor():
            await self._submit(
                self._launch_line,
                dispatch,
                line,
                task=task,
                force_g2=force_g2,
                fix=fix,
            )
            return
        if line.failure_reason == REPLAN_EXHAUSTED:
            self._block_line(dispatch, line, REPLAN_EXHAUSTED)
            await self._park_blocked(dispatch)
            return
        if line.status is LineStatus.RUNNING and line.current_run:
            return
        fresh = fix is None and task is None and not force_g2
        if (
            fix is None
            and task is None
            and not force_g2
            and liveness_modes.should_act(self._liveness_mode())
        ):
            if not await self._admit_launch(dispatch, line):
                return
        if self._c.gates is not None:
            # A new run gets a new review: no retry or count from the last one.
            _clear_review_retry(self._c.gates.line_snapshot(dispatch, line))
            if task is None and not force_g2 and fix is None:
                await self._skip_limited_provider(dispatch, line)
        self._open_ledger_if_new_branch(dispatch, line)
        if task is None:
            task = await self._reassign_task(dispatch, line)
        if task is None and self._c.gates is not None:
            # A pending fix ruling (R5 N6) is checked against this run's
            # report, so a run started from the bare task (an automatic
            # reassign or retry of a fix run) is shown the ruling too.
            pending = _ruling_from_dict(
                self._c.gates.line_snapshot(dispatch, line).get(FIX_RULING)
            )
            if pending is not None:
                task = _with_ruling(line.task, pending)
        if not line.branch:
            line.branch = f"{dispatch.branch_prefix}/line-{line.id}"
        if not line.base_commit and self._c.gates is not None:
            base = dispatch.gates.get("base_commit") if isinstance(dispatch.gates, dict) else None
            if isinstance(base, str):
                line.base_commit = base
        if not line.base_commit and self._c.gates is None:
            pin_base = getattr(self._c.launcher, "pin_base_commit", None)
            if callable(pin_base):
                base = await self._leave(pin_base())
                if isinstance(base, str) and base:
                    line.base_commit = base
                    await self._save(dispatch)
        if fresh and p0_hooks.launch_check(self._dedup_book, dispatch, line):
            # P0 dedup enforce: the same line, author and base already ran.
            await self._save(dispatch)
            return
        # A launch in ``fixing`` is a fix round however it was started (an
        # adjudicated BLOCK, a retried fix run): its G2 must run again.
        fixing = self._c.gates is not None and self._is_fix_round(dispatch, line)
        if self._c.gates is not None and await self._c.gates.invalidate_g2_async(
            dispatch, line, force=force_g2 or fixing
        ):
            line.failure_reason = None
        if self._c.gates is not None:
            self._c.gates.clear_run_settled(dispatch, line)
            if not force_g2 and self._enter_implementation(dispatch, line):
                await self._save(dispatch)
        legacy_attempt = 0
        while True:
            if not await self._wait_for_recovery_retry(dispatch, line):
                return
            try:
                run_id = await self._launch_guarded(
                    dispatch, line, task=task, fix=fix
                )
            except _ReservedWorkerFailure as exc:
                if not await self._recover_worker_failure(
                    dispatch,
                    line,
                    exc.failure,
                    force_g2 or fix is not None or self._is_fix_round(dispatch, line),
                    attempt=exc.attempt,
                    author=exc.author,
                    epoch=exc.epoch,
                ):
                    return
                continue
            except UncommittedChangesError as exc:
                exited_run = getattr(exc, "run_id", None) or line.current_run
                self._block_line(dispatch, line, "uncommitted_changes")
                self._cost_end(
                    dispatch, line, exited_run, result="failed",
                    reason_code="uncommitted_changes", usage=getattr(exc, "usage", None),
                )
                if self._c.gates is None:
                    line.status = LineStatus.FAILED
                line.blockers = ["Worker worktree has uncommitted changes; preserved for recovery"]
                self._checkpoint_exited(dispatch, line, "uncommitted_changes", exited_run)
                if self._c.gates is None:
                    dispatch.state = DispatchState.ERROR
                    dispatch.message = f"uncommitted_changes for line {line.id}"
                    await self._save(dispatch)
                    return
                await self._save(dispatch)
                await self._park_blocked(dispatch)
                return
            except NoNewCommitError as exc:
                exited_run = getattr(exc, "run_id", None) or line.current_run
                self._block_line(dispatch, line, "no_new_commit")
                self._cost_end(
                    dispatch, line, exited_run, result="failed",
                    reason_code="no_new_commit", usage=getattr(exc, "usage", None),
                )
                if self._c.gates is None:
                    line.status = LineStatus.FAILED
                line.blockers = ["Worker exited without creating a commit; line branch preserved"]
                self._checkpoint_exited(dispatch, line, "no_new_commit", exited_run, exit_code=0)
                if self._c.gates is None:
                    dispatch.state = DispatchState.ERROR
                    dispatch.message = f"no_new_commit for line {line.id}"
                    await self._save(dispatch)
                    return
                await self._save(dispatch)
                await self._park_blocked(dispatch)
                return
            except LaunchError as exc:
                logger.warning("Launch of %s failed (attempt %s): %s", line.id, legacy_attempt, exc)
                if getattr(exc, "worker_started", False):
                    if not await self._allow_dirty_retry_after_worker_exit(
                        dispatch, line, exc
                    ):
                        return
                # A host problem (no git, gh unauthenticated) is not retried
                # at once on the same host: it would fail the same way.
                if (
                    legacy_attempt >= self._config.launch_retries
                    or getattr(exc, "retryable", True) is False
                ):
                    await self._give_up_launch(dispatch, line, str(exc), fix=force_g2)
                    return
                legacy_attempt += 1
                continue
            if run_id is None:
                return
            return
        await self._give_up_launch(dispatch, line, "launch never produced a run", fix=force_g2)

    async def _launch_guarded(
        self,
        dispatch: Dispatch,
        line: Line,
        *,
        task: str | None = None,
        fix: dict[str, object] | None = None,
    ) -> str | None:
        """Save this run, then start it. The only call to the launcher.

        The first step is the serial check. Another active line means this
        line waits: ``pending_relaunch`` / ``serial_wait``, and no launcher
        call. A fix intent is already in ``pending_fix`` when this method
        runs. The defer save and the reservation save both keep that entry.
        The reservation save records ``reserved_run_id``. The intent is
        cleared when LaunchDone confirms the start, or when the line is
        parked. The worker starts only after the save has accepted the run
        id and the attempt.
        """
        del fix
        original_task = task
        # N49: a fix round must commit beyond its own start head. The launcher
        # reads a clean tree on an ahead branch as a submission of the work
        # already there, which for a fix round would resubmit the head the
        # review already refused, so it is told which kind of run this is.
        fix_round = self._is_fix_round(dispatch, line)
        if self._abandoned:
            return None
        if self._c.gates is not None and self._launch_blockers(dispatch, line):
            self._mark_pending_relaunch(dispatch, line)
            await self._save(dispatch)
            return None
        if self._c.gates is not None:
            self._clear_pending_relaunch(dispatch, line)
        # N29: finish a handoff a crash interrupted; unpark an operator retry.
        if not await self._finish_pending_handoff(dispatch, line):
            return None
        await self._clear_rotation_park(dispatch, line)
        if not await self._rotate_before_launch(dispatch, line):
            return None
        if self._abandoned:
            return None
        if self._c.gates is not None and self._launch_blockers(dispatch, line):
            self._mark_pending_relaunch(dispatch, line)
            await self._save(dispatch)
            return None
        # N30: the quota before this attempt, read synchronously before the
        # reservation so the ledger adds no await point to the launch path.
        quota_before = self._cost_quota(line.assigned_model or "")
        reserved = await self._submit(self._reserve_run, dispatch, line)
        if reserved is None:
            return None
        run_id, previous, attempt = reserved
        reserved_author = line.assigned_model or ""
        reserved_epoch = drive_epoch(dispatch)
        reserved_status = line.status
        session_id: str | None = None
        usage: Any | None = None
        estimate: Any | None = None
        handoff: LaunchResult | None = None
        note = self._retry_prompt_note(dispatch, line)
        if note:
            task = f"{(task if task is not None else line.task).rstrip()}\n\n{note}"
        subject = line if task is None else replace(line, task=task, current_run=run_id)
        started_wall = self._cost_wall()
        try:
            launch_worker = getattr(self._c.launcher, "launch_worker", None)
            if launch_worker is None:
                await self._fence_spawn(dispatch)
                legacy_kwargs: dict[str, object] = {}
                if _callable_accepts_keyword(self._c.launcher.launch, "before_spawn"):
                    legacy_kwargs["before_spawn"] = lambda: self._fence_spawn(dispatch)
                launched = await self._leave(
                    self._c.launcher.launch(
                        subject, line.assigned_model or "", [], **legacy_kwargs
                    )
                )
            else:
                session_id, session_mode = self._launch_session(dispatch, line)
                # Persist the requested per-line session before the external
                # CLI can create or resume it.
                await self._save_locked(dispatch)
                worker_kwargs: dict[str, object] = {
                    "session_id": session_id,
                    "session_mode": session_mode,
                    "handoff_path": (
                        Path(str(self._recovery_line(dispatch, line)["handoff_path"]))
                        if self._recovery_line(dispatch, line).get("handoff_path")
                        else None
                    ),
                }
                if self._recovery_line(dispatch, line).get("allow_dirty_worktree") and (
                    _callable_accepts_keyword(launch_worker, "allow_dirty_worktree")
                ):
                    worker_kwargs["allow_dirty_worktree"] = True
                if fix_round and _callable_accepts_keyword(launch_worker, "fix_round"):
                    worker_kwargs["fix_round"] = True
                if _callable_accepts_keyword(launch_worker, "before_spawn"):
                    # Checked again after the worktree is ready, right before
                    # the process starts: the preparation can take a while.
                    worker_kwargs["before_spawn"] = lambda: self._fence_spawn(dispatch)
                if _callable_accepts_keyword(launch_worker, "context_key"):
                    worker_kwargs["context_key"] = self._session_key(dispatch, line)
                await self._fence_spawn(dispatch)
                launched = await self._leave(
                    launch_worker(
                        subject,
                        line.assigned_model or "",
                        [],
                        **worker_kwargs,
                    )
                )
                if isinstance(launched, LaunchResult):
                    session_id = launched.session_id
                    usage = launched.usage
                    self._note_worker_claim(dispatch, line, launched)
                    estimate = getattr(launched, "estimate", None)
                    if getattr(launched, "handoff_request", None) == HANDOFF_OK and (
                        reserved_author == (line.assigned_model or "")
                    ):
                        # N29: a valid worker-requested handoff, decided below
                        # before any LaunchDone: the line never shows RUNNING.
                        handoff = launched
                    elif launched.start_head and launched.head == launched.start_head:
                        # N49 reads a clean tree on an ahead branch as a
                        # submission of the head already there. Whether that
                        # head may go to G2 and review is N38's call, not the
                        # launcher's: a head the review already refused (or a
                        # line whose ledger is in doubt) must still stop
                        # ``no_new_commit``. The handler below asks the ledger.
                        unchanged = NoNewCommitError(
                            f"no_new_commit for line {line.id}",
                            run_id=launched.run_id,
                            branch_has_commits=True,
                        )
                        unchanged.head = launched.head
                        unchanged.start_head = launched.start_head
                        unchanged.session_id = launched.session_id
                        unchanged.usage = launched.usage
                        raise unchanged
                    # A finished run goes on to G2: its HEAD is submitted.
                    self._note_run_head(
                        dispatch, line, reserved_author, attempt,
                        launched.start_head, launched.head, submitted=handoff is None,
                    )
                    launched = launched.run_id
        except WorkerLaunchFailure as exc:
            # The run may have committed before it failed (a rate limit after
            # a commit). Keep where it left the branch, not yet submitted.
            self._note_run_head(
                dispatch, line, reserved_author, attempt,
                getattr(exc, "start_head", None), getattr(exc, "head", None),
                submitted=False,
            )
            # Keep the reserved run id until the actor applies the typed
            # recovery decision; it is the fencing token for this failure.
            raise _ReservedWorkerFailure(
                exc, attempt, reserved_author, reserved_epoch
            ) from exc
        except (UncommittedChangesError, NoNewCommitError) as exc:
            if isinstance(exc, UncommittedChangesError):
                # Committed work may sit under the dirty files: keep the chain.
                self._note_run_head(
                    dispatch, line, reserved_author, attempt,
                    getattr(exc, "start_head", None), getattr(exc, "head", None),
                    submitted=False,
                )
            continues = (
                isinstance(exc, NoNewCommitError)
                and getattr(exc, "run_id", None) == run_id
                and self._unreviewed_head_continues(dispatch, line, exc)
            )
            if continues:
                self._note_run_head(
                    dispatch, line, reserved_author, attempt,
                    exc.head, exc.head, submitted=True,
                )
            if getattr(exc, "run_id", None) == run_id:
                await self._submit(
                    self._apply_launch_done,
                    LaunchDone(
                        dispatch_id=dispatch.id,
                        line_id=line.id,
                        run_id=run_id,
                        attempt=attempt,
                        launched=run_id,
                        author=reserved_author,
                        session_id=getattr(exc, "session_id", None),
                        usage=getattr(exc, "usage", None),
                        started_wall=started_wall,
                        quota_before=quota_before,
                    ),
                    dispatch,
                    line,
                )
                if continues:
                    # Nothing left to change, and the branch head has never
                    # been through G2 or review: it goes on to both. Every
                    # model that held the line is kept off that review.
                    if line.current_run == run_id:
                        # The launch was accepted: record the delivery (N78).
                        self._note_inherited_tip(
                            dispatch, line, run_id, exc.head, reserved_author, attempt
                        )
                    logger.info(
                        "Line %s run %s made no new commit; its unreviewed "
                        "branch head goes on to G2",
                        line.id,
                        run_id,
                    )
                    return run_id
            else:
                await self._submit(
                    self._drop_unstarted_run, dispatch, line, run_id, previous
                )
            raise
        except LaunchError as exc:
            # An exit outside the classified codes can still have committed.
            self._note_run_head(
                dispatch, line, reserved_author, attempt,
                getattr(exc, "start_head", None), getattr(exc, "head", None),
                submitted=False,
            )
            if (
                getattr(exc, "worker_started", False)
                and getattr(exc, "run_id", None) == run_id
            ):
                exc.worker_attempt = attempt
                exc.worker_author = reserved_author
                exc.worker_epoch = reserved_epoch
                await self._submit(
                    self._apply_launch_done,
                    LaunchDone(
                        dispatch_id=dispatch.id,
                        line_id=line.id,
                        run_id=run_id,
                        attempt=attempt,
                        launched=run_id,
                        author=reserved_author,
                        session_id=session_id,
                        usage=usage,
                        started_wall=started_wall,
                        quota_before=quota_before,
                    ),
                    dispatch,
                    line,
                )
            else:
                await self._submit(
                    self._drop_unstarted_run, dispatch, line, run_id, previous
                )
            raise
        except DispatchError as exc:
            await self._submit(
                self._drop_unstarted_run, dispatch, line, run_id, previous
            )
            if str(exc) == f"Git evidence for line {line.id} is unavailable":
                if liveness_modes.should_act(self._liveness_mode()):
                    # FAILED is terminal: the sweep skips it, so R-SPLIT never
                    # runs and the dispatch stays ERROR. Park instead.
                    self._park_git_evidence_unavailable(dispatch, line)
                    return None
                line.status = LineStatus.FAILED
                line.failure_reason = "git_evidence_unavailable"
                line.current_run = None
            raise
        except Exception:
            await self._submit(
                self._drop_unstarted_run, dispatch, line, run_id, previous
            )
            raise
        if handoff is not None:
            outcome = await self._worker_requested_handoff(
                dispatch, line, handoff, (run_id, attempt, reserved_author, reserved_epoch),
                task=original_task, status=reserved_status,
            )
            if outcome is not _NO_HANDOFF:
                return outcome
            # Nothing rotated: the run goes on as a normal success.
            self._note_run_head(
                dispatch, line, reserved_author, attempt,
                handoff.start_head, handoff.head, submitted=True,
            )
        await self._submit(
            self._apply_launch_done,
            LaunchDone(
                dispatch_id=dispatch.id,
                line_id=line.id,
                run_id=run_id,
                attempt=attempt,
                launched=str(launched),
                author=reserved_author,
                session_id=session_id,
                usage=usage,
                started_wall=started_wall,
                quota_before=quota_before,
                estimate=estimate,
            ),
            dispatch,
            line,
        )
        return str(launched)

    def _retry_prompt_note(self, dispatch: Dispatch, line: Line) -> str | None:
        """The recovery note for this author and attempt's retried run, if any."""
        entry = self._recovery_data(dispatch).get(line.id)
        record = entry.get("prompt_note") if isinstance(entry, dict) else None
        if not isinstance(record, dict):
            return None
        if record.get("author") != (line.assigned_model or "") or record.get(
            "attempt"
        ) != line.attempts:
            return None
        note = record.get("note")
        return note if isinstance(note, str) and note.strip() else None

    async def _fence_spawn(self, dispatch: Dispatch) -> None:
        """Re-read the stored epoch immediately before a worker is spawned.

        The reservation was saved under this drive's epoch, but an operator
        cancel --force (or any epoch bump) can land between that save and the
        spawn. A stale epoch abandons the drive before any process starts; the
        caller's handler drops the unstarted reservation.
        """
        if self._abandoned or not await self._epoch_matches(dispatch):
            assert self._own_abandoned is not None
            self._own_abandoned(True)
            raise StaleDrive

    async def _allow_dirty_retry_after_worker_exit(
        self, dispatch: Dispatch, line: Line, error: LaunchError
    ) -> bool:
        """Permit same-owner retries only for the exact fenced run that exited."""
        if not getattr(error, "worker_started", False):
            return False
        run_id = getattr(error, "run_id", None)
        attempt = getattr(error, "worker_attempt", None)
        author = getattr(error, "worker_author", None)
        epoch = getattr(error, "worker_epoch", None)
        if (
            not isinstance(run_id, str)
            or line.current_run != run_id
            or attempt != line.attempts
            or author != (line.assigned_model or "")
            or epoch != drive_epoch(dispatch)
            or line.status is not LineStatus.RUNNING
            or self._abandoned
        ):
            return False
        entry = self._recovery_line(dispatch, line)
        entry["allow_dirty_worktree"] = True
        await self._save(dispatch)
        return (
            not self._abandoned
            and line.current_run == run_id
            and line.attempts == attempt
            and (line.assigned_model or "") == author
            and drive_epoch(dispatch) == epoch
        )

    def _recovery_root(self, dispatch: Dispatch) -> dict[str, object]:
        if not isinstance(dispatch.gates, dict):
            dispatch.gates = {}
        root = dispatch.gates.get("worker_recovery")
        if not isinstance(root, dict):
            root = {}
            dispatch.gates["worker_recovery"] = root
        return root

    def _recovery_data(self, dispatch: Dispatch) -> dict[str, object]:
        if not isinstance(dispatch.gates, dict):
            return {}
        root = dispatch.gates.get("worker_recovery")
        return root if isinstance(root, dict) else {}

    async def _wait_for_recovery_retry(self, dispatch: Dispatch, line: Line) -> bool:
        """Honor a persisted same-session retry deadline across restarts."""
        recovery = self._recovery_data(dispatch)
        entry = recovery.get(line.id)
        if not isinstance(entry, dict):
            return True
        deadline = entry.get("retry_at")
        if (
            entry.get("retry_source") not in ("retry_after", "backoff")
            or isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
        ):
            return True
        captured = (
            dispatch.id,
            line.current_run,
            line.attempts,
            line.assigned_model or "",
            drive_epoch(dispatch),
        )
        remaining = max(0.0, float(deadline) - datetime.now(timezone.utc).timestamp())
        if remaining:
            await self._leave(self._sleep(remaining))
        if (
            self._abandoned
            or captured[0] != dispatch.id
            or captured[1] != line.current_run
            or captured[2] != line.attempts
            or captured[3] != (line.assigned_model or "")
            or captured[4] != drive_epoch(dispatch)
            or line.status is not LineStatus.ASSIGNED
        ):
            return False
        entry = self._recovery_line(dispatch, line)
        entry.pop("retry_at", None)
        entry.pop("retry_source", None)
        await self._save_locked(dispatch)
        return (
            not self._abandoned
            and captured[1] == line.current_run
            and captured[2] == line.attempts
            and captured[3] == (line.assigned_model or "")
            and captured[4] == drive_epoch(dispatch)
            and line.status is LineStatus.ASSIGNED
        )

    async def _rotate_before_launch(self, dispatch: Dispatch, line: Line) -> bool:
        """Rotate a session that used up its share of the window, before the next launch.

        This is the proactive rotation. The old session is asked for a
        structured self-report first (one bounded turn), and the report is
        appended to the handover note as ``worker_reported``. A timeout, a bad
        answer or a provider that cannot resume falls back to the
        deterministic note. The passive rotation, after a ``context_exhausted``
        failure, stays in ``_recover_worker_failure`` and never asks: that
        session has already failed.

        Returns False when the line changed while this ran.
        """
        provider = (line.assigned_model or "").lower()
        # Read only: a line with no recovery state must not get one here.
        entry = self._recovery_data(dispatch).get(line.id)
        if not isinstance(entry, dict):
            return True
        old_session = entry.get("session_id")
        if (
            entry.get("session_author") != provider
            or entry.get("session_mode") != "resume"
            or not isinstance(old_session, str)
            or not old_session
        ):
            return True
        captured = (
            dispatch.id,
            line.current_run,
            line.attempts,
            provider,
            drive_epoch(dispatch),
        )

        def still_current() -> bool:
            return not self._abandoned and captured == (
                dispatch.id,
                line.current_run,
                line.attempts,
                (line.assigned_model or "").lower(),
                drive_epoch(dispatch),
            )

        manager = await self._leave(asyncio.to_thread(self._session_manager))
        if manager is None or not still_current():
            return still_current()
        key = self._session_key(dispatch, line)
        due = getattr(manager, "proactive_rotation_due", None)
        if not callable(due) or not await self._leave(asyncio.to_thread(due, key)):
            return still_current()
        record = await self._leave(asyncio.to_thread(manager.current, key))
        if not still_current():
            return False
        if record is None or record.session_id != old_session:
            return True
        ask = getattr(self._c.launcher, "request_self_report", None)
        reply = None
        ask_raised = False
        if callable(ask):
            # No await between this check and the ask: a sibling that arrives
            # during the waits above defers, and this line does not start a report.
            if self._c.gates is not None and self._launch_blockers(dispatch, line):
                return True
            self._self_report_line = line.id
            try:
                reply = await self._leave(
                    ask(
                        line,
                        line.assigned_model or "",
                        session_id=old_session,
                        timeout=WORKER_REPORT_TIMEOUT_SECONDS,
                    )
                )
            except (LaunchError, OSError, ValueError) as exc:
                logger.warning("Self-report for %s failed: %s", line.id, type(exc).__name__)
                reply = None
                ask_raised = True
            finally:
                self._self_report_line = None
            if not still_current():
                return False
        outcome = read_self_report(reply)
        # A normal None is "unsupported" (the provider was not asked). Only a raised ask is "failed".
        if ask_raised:
            outcome = replace(outcome, status="failed")

        def rotate() -> Any:
            return manager.rotate(
                key,
                reason=ACTIVE_ROTATION,
                worker_report=outcome.report,
                self_report=outcome.status,
                self_report_bytes=outcome.byte_count,
            )

        result = await self._leave(asyncio.to_thread(rotate))
        if not still_current():
            return False
        if result.rotated:
            entry = self._recovery_line(dispatch, line)
            entry["session_id"] = result.session_id
            entry["session_mode"] = "fresh" if result.session_id else None
            entry["session_author"] = provider if result.session_id else None
            # The rotated id is the session manager's, not one a CLI reported.
            entry.pop("session_origin", None)
            entry["handoff_path"] = str(result.handoff_path) if result.handoff_path else None
            sink = self._c.lesson_sink
            if sink is not None and outcome.report is not None and outcome.report.pitfalls:
                try:
                    sink(
                        lesson_candidates(
                            outcome.report,
                            worker=provider,
                            dispatch_id=dispatch.id,
                            line_id=line.id,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - the hook never blocks a launch
                    logger.warning("Lesson hook for %s failed: %s", line.id, type(exc).__name__)
            await self._save_locked(dispatch)
        return still_current()

    async def _worker_requested_handoff(
        self,
        dispatch: Dispatch,
        line: Line,
        result: LaunchResult,
        reserved: tuple[str, int, str, int],
        *,
        task: str | None,
        status: LineStatus,
    ) -> Any:
        """N29: the Worker asked for a fresh session: rotate, then relaunch the same task.

        Decided before any LaunchDone, so the line never shows RUNNING with an
        unsettled run and the drive never polls (or reviews) the handoff work.
        Nothing is submitted (no G2, no review, same attempt and fix round). The
        rotation counts against ``MAX_ROTATIONS``; past it the line is parked.
        Returns ``_NO_HANDOFF`` when nothing rotated (the caller confirms the
        run as a normal success), None when the line moved on or was parked.
        """
        run_id, attempt, author, epoch = reserved

        # ``status`` is the line's status at the reservation: a change made
        # while the run was out (a cancel) is never overwritten.
        def still_current() -> bool:
            return not self._abandoned and (
                line.current_run, line.attempts, line.assigned_model or "",
                drive_epoch(dispatch), line.status,
            ) == (run_id, attempt, author, epoch, status)

        request_file = getattr(result, "handoff_request_file", None)
        manager = await self._leave(asyncio.to_thread(self._session_manager))
        if not still_current():
            return None
        if manager is None:
            _drop_request_file(request_file)
            return _NO_HANDOFF
        if result.session_id:
            # The old session's turn is recorded before it is rotated away.
            await self._record_session_success(
                dispatch,
                line,
                LaunchResult(
                    run_id, result.session_id, "", usage=result.usage,
                    estimate=getattr(result, "estimate", None),
                ),
                worker_requested=True,
            )
            if not still_current():
                return None
        outcome = await self._complete_handoff(
            dispatch, line, manager, run_id=run_id, attempt=attempt, author=author,
            report=result.worker_report, report_bytes=result.self_report_bytes,
            request_file=str(request_file) if request_file else None,
            marker=None, still_current=still_current,
        )
        if outcome is not _HANDED_OFF:
            return outcome
        # Relaunch only from the state the handoff left: this run, ASSIGNED,
        # with its saved decision. A cancel or any other change wins.
        entry = self._recovery_data(dispatch).get(line.id)
        if self._abandoned or line.current_run != run_id or line.status is not (
            LineStatus.ASSIGNED
        ) or not (isinstance(entry, dict) and isinstance(entry.get("handoff_pending"), dict)):
            return None
        logger.info("Line %s run %s: worker-requested handoff; relaunching", line.id, run_id)
        return await self._launch_guarded(dispatch, line, task=task)

    async def _complete_handoff(
        self,
        dispatch: Dispatch,
        line: Line,
        manager: Any,
        *,
        run_id: str,
        attempt: int,
        author: str,
        report: Any,
        report_bytes: int,
        request_file: str | None,
        marker: dict[str, Any] | None,
        still_current: Callable[[], bool],
    ) -> Any:
        """Rotate once for this run's handoff and save the decision (``handoff_pending``).

        The marker is saved before the rotation (phase ``rotating``, with the
        lineage it starts from) and again, rotated, by the LaunchDone that
        confirms the run. A rotation the manager already made (a crash after
        it, before that save) is adopted, never repeated. The request file is
        deleted only after the confirming save. Returns ``_HANDED_OFF``,
        ``_NO_HANDOFF`` or None (moved on, or parked).
        """
        key = self._session_key(dispatch, line)
        lineage_of = getattr(manager, "lineage", None)

        async def lineage_now() -> str | None:
            if not callable(lineage_of):
                return None
            found = await self._leave(asyncio.to_thread(lineage_of, key))
            return getattr(found, "current", None)

        if marker is None or marker.get("phase") != "rotating":
            before = await lineage_now()
            if not still_current():
                return None
            marker = {
                "run_id": run_id,
                "author": author,
                "phase": "rotating",
                "lineage_before": before,
                "request_file": request_file,
            }
            self._recovery_line(dispatch, line)["handoff_pending"] = dict(marker)
            await self._save_locked(dispatch)
            if not still_current():
                return None
        before = marker.get("lineage_before")
        current = await lineage_now()
        if not still_current():
            return None
        parked = False
        if isinstance(before, str) and before and current and current != before:
            # Rotated already, and the save after it never happened: adopt it.
            record = await self._leave(asyncio.to_thread(manager.current, key))
            note_of = getattr(manager, "handoff_path", None)
            note = await self._leave(asyncio.to_thread(note_of, key)) if callable(note_of) else None
            rotated, session_id = True, getattr(record, "session_id", None)
        else:
            result = await self._leave(asyncio.to_thread(
                manager.rotate, key, reason=WORKER_REQUESTED, worker_report=report,
                self_report_bytes=report_bytes, self_report="ok",
            ))
            parked = bool(getattr(result, "parked", False))
            rotated, session_id = bool(result.rotated), result.session_id
            note = result.handoff_path
        if not still_current():
            return None
        entry = self._recovery_line(dispatch, line)
        if parked or not rotated:
            entry.pop("handoff_pending", None)
            _drop_request_file(request_file)
        if parked:
            entry["parked_reason"] = ROTATION_EXHAUSTED_REASON
            # An operator retry of this line unparks the session (N29 item 3).
            entry[_ROTATION_PARKED_KEY] = key
            self._clear_pending_fix(dispatch, line.id)
            self._block_line(dispatch, line, ROTATION_EXHAUSTED_REASON)
            self._checkpoint_exited(dispatch, line, ROTATION_EXHAUSTED_REASON, run_id, exit_code=0)
            if self._c.gates is None:
                line.status = LineStatus.FAILED
                dispatch.state = DispatchState.ERROR
                dispatch.message = f"{ROTATION_EXHAUSTED_REASON} for line {line.id}"
            await self._save(dispatch)
            if self._c.gates is not None:
                await self._park_blocked(dispatch)
            return None
        if not rotated:
            return _NO_HANDOFF
        decided = {
            **marker,
            "phase": "rotated",
            "session_id": session_id,
            "handoff_path": str(note) if note else None,
        }
        await self._submit(
            self._apply_launch_done,
            LaunchDone(
                dispatch_id=dispatch.id,
                line_id=line.id,
                run_id=run_id,
                attempt=attempt,
                launched=run_id,
                author=author,
                handoff=decided,
            ),
            dispatch,
            line,
        )
        entry = self._recovery_data(dispatch).get(line.id)
        saved = isinstance(entry, dict) and entry.get("handoff_pending") == decided
        if saved:
            _drop_request_file(request_file)
        return _HANDED_OFF if saved and line.current_run == run_id else None

    async def _finish_pending_handoff(self, dispatch: Dispatch, line: Line) -> bool:
        """N29 recovery: an exit-0 run that asked for a handoff is never a completion.

        A restart can find the line ASSIGNED on that run in three states: the
        decision saved (``handoff_pending`` rotated: only the request file may
        be left), saved only as ``rotating`` (the rotation is finished, or
        adopted when the manager already made it), or not saved at all (the
        run's ``ok`` request file is still there). Returns False when the line
        moved on or was parked and must not launch.
        """
        run_id = line.current_run
        if not run_id or line.status is not LineStatus.ASSIGNED:
            return True
        author = line.assigned_model or ""
        entry = self._recovery_data(dispatch).get(line.id)
        marker = entry.get("handoff_pending") if isinstance(entry, dict) else None
        request: Any = None
        if isinstance(marker, dict):
            if marker.get("run_id") != run_id or marker.get("author") != author:
                return True  # another run's decision; this launch's LaunchDone drops it
            request_file = marker.get("request_file")
            request_file = request_file if isinstance(request_file, str) and request_file else None
            if marker.get("phase") == "rotated":
                _drop_request_file(request_file)
                return True
            if request_file is not None:
                # One lstat and at most 8 KiB: read on the writer, no yield.
                request = read_handoff_request(Path(request_file), delete=False)
        else:
            marker = None
            runs_dir = getattr(self._c.launcher, "runs_dir", None)
            if (
                not isinstance(runs_dir, (str, Path))
                or not run_id.startswith(f"{dispatch.id}:{line.id}:{line.attempts}:")
                or not isinstance(entry, dict)
                or entry.get("session_author") != author.lower()
            ):
                return True
            request = pending_handoff_request(Path(runs_dir), run_id)
            if getattr(request, "status", None) != HANDOFF_OK:
                return True
            request_file = str(handoff_request_path(Path(runs_dir), run_id))
        captured = (run_id, line.attempts, author, drive_epoch(dispatch), line.status)

        def still_current() -> bool:
            return not self._abandoned and captured == (
                line.current_run, line.attempts, line.assigned_model or "",
                drive_epoch(dispatch), line.status,
            )

        manager = await self._leave(asyncio.to_thread(self._session_manager))
        if not still_current():
            return False
        if manager is None:
            return True
        ok = getattr(request, "status", None) == HANDOFF_OK
        logger.info("Line %s run %s: finishing an interrupted worker-requested handoff",
                    line.id, run_id)
        outcome = await self._complete_handoff(
            dispatch, line, manager, run_id=run_id, attempt=line.attempts, author=author,
            report=request.report if ok else None,
            report_bytes=request.byte_count if ok else 0,
            request_file=request_file, marker=marker, still_current=still_current,
        )
        return outcome is not None

    async def _clear_rotation_park(self, dispatch: Dispatch, line: Line) -> None:
        """N29: a line parked ``context_rotation_exhausted`` that launches again.

        Only an operator (unblock, recover, retry or reassign) launches such a
        line again, so its session is unparked first: otherwise the manager
        would park the next rotation at once. One hook for every operator
        path, since each of them ends in this launch.
        """
        entry = self._recovery_data(dispatch).get(line.id)
        key = entry.get(_ROTATION_PARKED_KEY) if isinstance(entry, dict) else None
        if not isinstance(key, str) or not key:
            return
        manager = await self._leave(asyncio.to_thread(self._session_manager))
        clear = getattr(manager, "clear_parked", None)
        if callable(clear):
            await self._leave(asyncio.to_thread(clear, key))
        entry = self._recovery_data(dispatch).get(line.id)
        if isinstance(entry, dict) and entry.get(_ROTATION_PARKED_KEY) == key:
            entry.pop(_ROTATION_PARKED_KEY, None)
            if entry.get("parked_reason") == ROTATION_EXHAUSTED_REASON:
                entry.pop("parked_reason", None)

    def _recovery_line(self, dispatch: Dispatch, line: Line) -> dict[str, object]:
        root = self._recovery_root(dispatch)
        entry = root.get(line.id)
        if not isinstance(entry, dict):
            entry = {"attempts": {}, "session_id": None, "session_mode": None}
            root[line.id] = entry
        if not isinstance(entry.get("attempts"), dict):
            entry["attempts"] = {}
        return entry

    def _note_run_head(
        self,
        dispatch: Dispatch,
        line: Line,
        author: str,
        attempt: int,
        start: object,
        head: object,
        *,
        submitted: bool,
    ) -> None:
        """Record where one run of this line left its branch, and for whom.

        ``submitted`` marks a head that went on to G2 and review. The no-new-
        commit check and reviewer exclusion read these records. Unknown heads
        are not recorded.
        """
        if not isinstance(head, str) or not head:
            return
        entry = self._recovery_line(dispatch, line)
        runs = entry.get("runs")
        if not isinstance(runs, list):
            runs = []
            entry["runs"] = runs
        runs.append(
            {
                "author": author,
                "attempt": attempt,
                "branch": line.branch or "",
                "start": start if isinstance(start, str) else "",
                "head": head,
                "submitted": submitted,
            }
        )
        del runs[:-_MAX_RUN_RECORDS]

    def _branch_runs(self, dispatch: Dispatch, line: Line) -> list[dict[str, object]]:
        entry = self._recovery_data(dispatch).get(line.id)
        runs = entry.get("runs") if isinstance(entry, dict) else None
        if not isinstance(runs, list):
            return []
        branch = line.branch or ""
        return [run for run in runs if isinstance(run, dict) and run.get("branch") == branch]

    def _unreviewed_head_continues(
        self, dispatch: Dispatch, line: Line, error: NoNewCommitError
    ) -> bool:
        """True when an empty run sends the branch head on to G2 and review (N38).

        Git must say the branch is strictly ahead of base_commit
        (``base..HEAD`` > 0 and ``HEAD..base`` == 0), and this HEAD must never
        have been submitted to G2 or review for the line. The evidence is the
        line's reviewed-heads ledger (``gates.reviewed_heads``): every HEAD is
        written there as it enters G2 or review, it is never capped, and it is
        complete only when it was opened before the branch's first run. A
        HEAD that was reviewed and rejected therefore still blocks, and a line
        whose ledger is missing or in doubt blocks too. (The G2 cell is no
        evidence: the launch clears it before the run.) Who made the commits is
        not asked: every model that ever held the line is kept off its review
        instead (``_line_holders``), so a new holder that finds the previous
        holder's commit already fixes the findings can send it on.
        """
        head = getattr(error, "head", None)
        if not error.branch_has_commits or not isinstance(head, str) or not head:
            return False
        gates = self._c.gates
        if gates is not None:
            # A ruling-check BLOCK (R5 N6) asked only for confirmations: no
            # reviewer rejected this head, so it may go on without a new commit.
            reissued = gates.line_snapshot(dispatch, line).get(RULING_REISSUE_HEAD)
            if isinstance(reissued, str) and reissued.strip().lower() == head.strip().lower():
                return True
        if head in self._submitted_heads(dispatch, line):
            return False
        # Positive evidence: a complete ledger for this branch that does not
        # list this HEAD. No ledger, another branch's ledger, or one opened
        # with any doubt fails closed -- except a pre-N78 replan child's
        # ledger, whose only doubt was the replan count it inherited (N78).
        ledger = _reviewed_ledger(dispatch, line)
        if ledger is None:
            return False
        if head in (ledger.get("heads") or []):
            return False
        if ledger.get("complete") is True:
            return True
        return self._legacy_child_ledger_vouches(dispatch, line, ledger)

    def _legacy_child_ledger_vouches(
        self, dispatch: Dispatch, line: Line, ledger: dict[str, object]
    ) -> bool:
        """A pre-N78 replan child's ledger may still vouch for ``head`` (N78).

        Before N78 every replan child opened its ledger incomplete: the
        parent's replan count it carries read as "not fresh", although its
        branch was born with the child and the ledger was opened before its
        first run, so every head that entered G2 or review since is listed.
        Such a ledger records no ``doubt``. It vouches only when the line is
        still on its birth branch and its run records reach back to that
        birth: the earliest starts at ``base_commit``, none was evicted by the
        cap, and every head they submitted is in the ledger. The caller has
        already checked that no run record submitted ``head`` and that the
        ledger does not list it. Any recorded doubt fails closed as before.
        """
        if "doubt" in ledger:
            return False
        gates = self._c.gates
        if gates is None:
            return False
        if not _on_replan_child_branch(dispatch, line, gates.line_snapshot(dispatch, line)):
            return False
        entry = self._recovery_data(dispatch).get(line.id)
        recorded = entry.get("runs") if isinstance(entry, dict) else None
        if not isinstance(recorded, list) or len(recorded) >= _MAX_RUN_RECORDS:
            return False
        runs = self._branch_runs(dispatch, line)
        if not runs or not line.base_commit or runs[0].get("start") != line.base_commit:
            return False
        listed = ledger.get("heads") or []
        return all(
            run.get("head") in listed for run in runs if run.get("submitted")
        )

    def _note_inherited_tip(
        self,
        dispatch: Dispatch,
        line: Line,
        run_id: str,
        head: object,
        author: str,
        attempt: int,
    ) -> None:
        """Record on the gate snapshot that an empty run delivered the branch tip (N78)."""
        gates = self._c.gates
        if gates is None:
            return
        snap = gates.line_snapshot(dispatch, line)
        snap[DELIVERED_INHERITED_TIP] = {
            "reason": DELIVERED_INHERITED_TIP,
            "run_id": run_id,
            "head": head if isinstance(head, str) else "",
            "base": line.base_commit or "",
            "author": author,
            "attempt": attempt,
        }

    def _open_ledger_if_new_branch(self, dispatch: Dispatch, line: Line) -> None:
        """Before a launch: open the reviewed-heads ledger for this branch.

        It is complete only when nothing on this branch can have entered G2
        or review yet: no run records here, no outcome, and (gated) a
        Stage-Gate state still before implementation with G2 never run and
        no review cell. Anything else opens it incomplete, so N38 fails closed.
        The dev path (no gates) writes no ledger and keeps ``dispatch.gates``
        empty; with no ledger, N38 never continues there.
        """
        if self._c.gates is None:
            return
        if _reviewed_ledger(dispatch, line) is not None:
            return
        _open_reviewed_ledger(
            dispatch,
            line,
            complete=self._branch_is_fresh(dispatch, line),
            doubt=LEDGER_DOUBT_NOT_FRESH,
        )

    def _branch_is_fresh(self, dispatch: Dispatch, line: Line) -> bool:
        # Attempt 0 with no run reserved: every reassign (human or automatic)
        # raises the attempt, so a reassigned old line whose review cells,
        # G2 cell and outcome were cleared still does not look fresh.
        if line.attempts != 0 or line.current_run:
            return False
        if self._branch_runs(dispatch, line) or line.id in dispatch.outcomes:
            return False
        recovery = self._recovery_data(dispatch).get(line.id)
        if isinstance(recovery, dict) and (recovery.get("events") or recovery.get("last")):
            return False
        gates = self._c.gates
        if gates is None:
            return True
        snap = gates.line_snapshot(dispatch, line)
        # N78: a replan child carries its parent's replan count, but its
        # branch is born with it, so the count says nothing about this branch.
        if snap.get("handoff_ref") or (
            snap.get("replans") and not _on_replan_child_branch(dispatch, line, snap)
        ):
            return False
        if _load_state(snap).stage not in (Stage.DAG_READY, Stage.ASSIGNED):
            return False
        cells = snap.get("gates")
        if not isinstance(cells, dict):
            return False
        g2 = cells.get("G2")
        if isinstance(g2, dict) and g2.get("result") not in (None, "", "pending"):
            return False
        return cells.get("primary_review") is None and cells.get("secondary_review") is None

    def _note_reviewed_head(self, dispatch: Dispatch, line: Line, head: object) -> None:
        """A HEAD enters G2 or review: write it to the never-capped ledger."""
        if self._c.gates is None or not isinstance(head, str) or not head:
            return
        ledger = _reviewed_ledger(dispatch, line)
        if ledger is None:
            # Nobody opened a ledger for this branch before its first run.
            ledger = _open_reviewed_ledger(
                dispatch, line, complete=False, doubt=LEDGER_DOUBT_UNOPENED
            )
        heads = ledger.setdefault("heads", [])
        if isinstance(heads, list) and head not in heads:
            heads.append(head)

    async def _note_head_under_review(self, dispatch: Dispatch, line: Line) -> None:
        """Gated review starts: write the HEAD it reviews, read from git.

        The line branch's commit comes from G2's own lookup. When git cannot
        say, the ledger can no longer vouch for what was reviewed and becomes
        incomplete; the latest run head is still written (recording more only
        blocks more), but it may belong to an older run, so it never keeps
        the ledger complete.
        """
        gates = self._c.gates
        if gates is None:
            return
        head = await self._leave(asyncio.to_thread(gates._revision, line))
        snap = gates.line_snapshot(dispatch, line)
        # A real review now judges this head: it is no longer one that only
        # lacked ruling confirmations, so N38 applies to it again.
        snap.pop(RULING_REISSUE_HEAD, None)
        if isinstance(head, str) and head:
            self._note_reviewed_head(dispatch, line, head)
            snap[HEAD_UNDER_REVIEW] = head
            return
        fallback = self._latest_run_head(dispatch, line)
        if isinstance(fallback, str) and fallback:
            self._note_reviewed_head(dispatch, line, fallback)
        # Possibly an older run's head: kept only to name the rejected tip.
        snap[HEAD_UNDER_REVIEW] = fallback if isinstance(fallback, str) else ""
        _open_reviewed_ledger(
            dispatch, line, complete=False, doubt=LEDGER_DOUBT_HEAD_UNKNOWN
        )

    def _latest_run_head(self, dispatch: Dispatch, line: Line) -> str | None:
        runs = self._branch_runs(dispatch, line)
        head = runs[-1].get("head") if runs else None
        return head if isinstance(head, str) and head else None

    def _submitted_heads(self, dispatch: Dispatch, line: Line) -> set[str]:
        """Branch heads this line already sent to G2 or review."""
        return {
            str(run.get("head"))
            for run in self._branch_runs(dispatch, line)
            if run.get("submitted") and run.get("head")
        }

    def _line_holders(self, dispatch: Dispatch, line: Line) -> set[str]:
        """Every model that ever held this line's branch. None may review it.

        Sources: the current assignment, Stage-Gate's author, developer and
        contributors, every recorded run's author on this branch, and human
        reassigns in the scheduler audit (a human reassign keeps the branch).
        Commits are not attributed; holding the branch is enough. An
        automatic reassign is not a source: it moves the line to a fresh
        ``-aN`` branch from base, so the outgoing model's work is not on the
        branch under review, and Stage-Gate already swapped it out as author.
        """
        holders = {line.assigned_model or ""}
        for run in self._branch_runs(dispatch, line):
            holders.add(str(run.get("author") or ""))
        gates_root = dispatch.gates if isinstance(dispatch.gates, dict) else {}
        scheduler = gates_root.get("scheduler")
        audit = scheduler.get("audit") if isinstance(scheduler, dict) else None
        for item in audit if isinstance(audit, list) else []:
            if (
                isinstance(item, dict)
                and item.get("kind") == "human_reassign"
                and item.get("line_id") == line.id
            ):
                holders.add(str(item.get("previous_model") or ""))
                if item.get("result") == "applied":
                    holders.add(str(item.get("model") or ""))
        gates = self._c.gates
        if gates is not None:
            state = _load_state(gates.line_snapshot(dispatch, line))
            holders.update(state.contributors)
            holders.update((state.author_provider, state.developer_provider))
        holders.discard("")
        return holders

    def _launch_session(self, dispatch: Dispatch, line: Line) -> tuple[str | None, str | None]:
        provider = (line.assigned_model or "").lower()
        entry = self._recovery_line(dispatch, line)
        session_id = entry.get("session_id")
        mode = entry.get("session_mode")
        if entry.get("session_author") != provider:
            session_id = None
            mode = None
            entry["session_id"] = None
            entry["session_mode"] = None
            entry.pop("session_origin", None)
            entry.pop("handoff_path", None)
            entry["session_author"] = provider
        if isinstance(session_id, str) and session_id and resumable_session_id(session_id) is None:
            # A legacy sentinel ("continue") names no session: start fresh.
            session_id = None
            mode = None
            entry["session_id"] = None
            entry["session_mode"] = None
            entry.pop("session_origin", None)
        if requires_cli_owned_session(provider):
            # Kimi and Grok resume only an id they created (N22). Never send a
            # caller-owned id: resume the id dispatch captured from the CLI's
            # own output, or run fresh and capture the id the CLI reports.
            if (
                isinstance(session_id, str)
                and session_id
                and mode == "resume"
                and entry.get("session_origin") == "cli"
            ):
                return session_id, "resume"
            if mode == "resume":
                # Persisted before N22 (a caller-owned id): never resumable.
                entry["session_id"] = None
                entry["session_mode"] = None
                entry.pop("session_origin", None)
            return None, None
        if isinstance(session_id, str) and session_id:
            if mode == "fresh":
                return (session_id, "fresh") if supports_explicit_session_id(provider) else (None, None)
            if supports_session_resume(provider):
                entry["session_mode"] = "resume"
                return session_id, "resume"
            return None, None
        if not supports_explicit_session_id(provider):
            return None, None
        if not isinstance(session_id, str) or not session_id:
            session_id = uuid4().hex
            entry["session_id"] = session_id
            mode = "fresh"
            entry["session_mode"] = mode
            entry.pop("session_origin", None)
        if mode not in ("fresh", "resume"):
            mode = "fresh"
            entry["session_mode"] = mode
        if mode == "resume" and not supports_session_resume(provider):
            return None, None
        return session_id, str(mode)

    def _remember_session(self, dispatch: Dispatch, line: Line, session_id: str | None) -> None:
        if not session_id:
            return
        entry = self._recovery_line(dispatch, line)
        entry["session_id"] = session_id
        entry["session_mode"] = "resume"
        entry["session_author"] = (line.assigned_model or "").lower()
        # The id came back in the CLI's own output (LaunchResult.session_id):
        # it is the one a fix round may resume (N22).
        entry["session_origin"] = "cli"

    def _session_manager(self) -> Any | None:
        injected = self._c.session_manager
        if injected is not None:
            return injected
        factory = getattr(self._c.launcher, "recovery_session_manager", None)
        return factory() if callable(factory) else None

    def _session_key(self, dispatch: Dispatch, line: Line) -> str:
        provider = (line.assigned_model or "unknown").lower()
        return f"dispatch:{dispatch.id}:line:{line.id}:author:{provider}"

    async def _record_session_success(
        self, dispatch: Dispatch, line: Line, result: LaunchResult, *, worker_requested: bool = False
    ) -> None:
        manager = await self._leave(asyncio.to_thread(self._session_manager))
        if manager is None or not result.session_id:
            return
        key = self._session_key(dispatch, line)
        kwargs: dict[str, object] = {"session_id": result.session_id, "usage": result.usage}
        # N29: injected managers may predate the estimate keywords.
        estimate = getattr(result, "estimate", None)
        if estimate is not None and _callable_accepts_keyword(manager.record_turn, "estimate"):
            kwargs["estimate"] = estimate
        if worker_requested and _callable_accepts_keyword(manager.record_turn, "worker_requested"):
            kwargs["worker_requested"] = True
        await self._leave(asyncio.to_thread(manager.record_turn, key, **kwargs))

    async def _recover_worker_failure(
        self,
        dispatch: Dispatch,
        line: Line,
        failure: WorkerLaunchFailure,
        is_fix_round: bool,
        *,
        attempt: int,
        author: str,
        epoch: int,
    ) -> bool:
        """Apply a classified launch failure without persisting its raw output."""
        if self._abandoned:
            return False
        if (
            line.current_run != failure.run_id
            or line.status is LineStatus.RUNNING
            or attempt != line.attempts
            or author != (line.assigned_model or "")
            or epoch != drive_epoch(dispatch)
        ):
            # A matching id is still this failed launch's unstarted reservation.
            # Drop only that id; a newer run or its current owner is untouched.
            if line.current_run == failure.run_id and line.status is not LineStatus.RUNNING:
                await self._drop_unstarted_run(dispatch, line, failure.run_id, None)
            return False

        def still_current() -> bool:
            return (
                not self._abandoned
                and line.current_run == failure.run_id
                and line.attempts == attempt
                and (line.assigned_model or "") == author
                and line.status is LineStatus.ASSIGNED
                and drive_epoch(dispatch) == epoch
            )
        entry = self._recovery_line(dispatch, line)
        attempts = entry["attempts"]
        assert isinstance(attempts, dict)
        name = failure.failure_class.value
        count = attempts.get(name, 0)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            count = 0
        action = decide(
            failure.failure_class,
            failure.retry_after_seconds,
            attempts,
            is_fix_round,
            datetime.now(timezone.utc),
        )
        # The CLI says the id it was asked to resume does not exist (N22). Never
        # retry that id: drop it, and run fresh once without spending the
        # crash budget. A second lost session falls back to the normal decision.
        session_lost = failure.reason_code in SESSION_LOST_REASON_CODES
        lost_retry = False
        if session_lost:
            entry["session_id"] = None
            entry["session_mode"] = None
            entry.pop("session_origin", None)
            lost = attempts.get("session_lost", 0)
            if not (isinstance(lost, int) and not isinstance(lost, bool) and lost > 0):
                action = Action("retry_fresh_once")
                lost_retry = True
        safe_reason = str(redact_credentials(action.reason or failure.reason_code))
        safe_event = redact_credentials({
            "failure_class": name,
            "reason": safe_reason,
            "action": action.kind,
            "attempt": count + 1,
            "author": line.assigned_model or "",
            "run_id": failure.run_id,
            "epoch": drive_epoch(dispatch),
        })
        assert isinstance(safe_event, dict)
        event_tags.tag(safe_event, role="worker", session=failure.run_id, snapshot=entry)
        entry["last"] = safe_event
        events = entry.get("events")
        if not isinstance(events, list):
            events = []
            entry["events"] = events
        events.append(dict(safe_event))
        del events[:-32]
        # The same save as worker_recovery carries the attempt's cost entry.
        self._cost_end(
            dispatch, line, failure.run_id, result="failed", failure_class=name,
            reason_code=failure.reason_code or action.reason,
            usage=getattr(failure, "usage", None),
            tokens_est=len(failure.output or "") // 4, model=author, attempt=attempt,
        )
        gates = self._c.gates
        if gates is not None:
            snap = gates.line_snapshot(dispatch, line)
            stage = _load_state(snap).stage.value
            timeline = snap.setdefault("events", [])
            if not isinstance(timeline, list):
                timeline = []
                snap["events"] = timeline
            gate_event = redact_credentials(
                {
                    "task_id": line.id,
                    "from_stage": stage,
                    "to_stage": stage,
                    "gate": _load_state(snap).stage.gate,
                    "result": "recovery",
                    "reason": safe_reason,
                    "provider": author,
                    "timestamp": gates.stamp(),
                    "handoff_ref": str(snap.get("handoff_ref") or ""),
                    "class": name,
                    "action": action.kind,
                    "attempt": count + 1,
                }
            )
            assert isinstance(gate_event, dict)
            timeline.append(gate_event)
            del timeline[:-50]
        self._note_provider_skip(dispatch, author, failure)
        if (
            self._c.gates is not None
            and not is_fix_round
            and name in (FailureClass.RATE_LIMITED.value, FailureClass.QUOTA_EXHAUSTED.value)
            and await self._move_off_limited_provider(
                dispatch, line, failure, author=author, reason=safe_reason
            )
        ):
            # The line waits ASSIGNED on another developer; the drive
            # launches it there on its own branch.
            return False
        if name == "quota_exhausted":
            retry_after = failure.retry_after_seconds
            workers = self._recovery_root(dispatch).setdefault("workers", {})
            if isinstance(workers, dict):
                workers[(line.assigned_model or "unknown").lower()] = {
                    "exhausted": True,
                    "failure_class": name,
                    "retry_at": (
                        datetime.now(timezone.utc).timestamp()
                        + min(float(retry_after), 600.0)
                        if retry_after is not None and retry_after >= 0
                        else None
                    ),
                    "retry_source": "retry_after" if retry_after is not None else "unknown",
                }
            entry["quota_retry_at"] = (
                datetime.now(timezone.utc).timestamp()
                + min(float(retry_after), 600.0)
                if retry_after is not None and retry_after >= 0
                else None
            )
            entry["quota_retry_source"] = (
                "retry_after" if retry_after is not None else "unknown"
            )
        if action.kind in ("retry_same", "retry_fresh_once", "rotate_session"):
            if lost_retry:
                attempts["session_lost"] = 1
            else:
                attempts[name] = count + 1
            entry["allow_dirty_worktree"] = True
        if action.kind in ("retry_same", "retry_fresh_once", "rotate_session"):
            if action.prompt_note:
                # The retried run's prompt carries this (N39): the Worker is
                # told why its last turn stopped, for this author and attempt.
                entry["prompt_note"] = {
                    "note": action.prompt_note,
                    "author": author,
                    "attempt": attempt,
                }
            else:
                entry.pop("prompt_note", None)
        if action.kind == "retry_same" and failure.session_id:
            entry["session_id"] = failure.session_id
            entry["session_mode"] = "resume"
            # Parsed from the failed run's output, or the id that run resumed:
            # dispatch never hands a CLI-owned provider an id it invented.
            entry["session_origin"] = "cli"
        if action.kind == "retry_same":
            entry["retry_at"] = (
                datetime.now(timezone.utc).timestamp() + action.delay_seconds
            )
            entry["retry_source"] = (
                "retry_after" if failure.retry_after_seconds is not None else "backoff"
            )
        elif action.kind == "retry_fresh_once":
            entry["session_id"] = None
            entry["session_mode"] = None
            entry.pop("session_origin", None)
        await self._save_locked(dispatch)
        # `_save_locked` adopts a deep copy from the store, so nested dict
        # references captured above no longer point into the live dispatch.
        entry = self._recovery_line(dispatch, line)
        if action.kind == "retry_same":
            if action.delay_seconds:
                await self._leave(self._sleep(action.delay_seconds))
                if not still_current():
                    return False
            return still_current()
        if action.kind == "retry_fresh_once":
            return still_current()
        if action.kind == "rotate_session":
            provider = (line.assigned_model or "").lower()
            explicit = supports_explicit_session_id(provider)
            manager = await self._leave(asyncio.to_thread(self._session_manager))
            if not still_current():
                return False
            key = self._session_key(dispatch, line)
            manager_record = (
                await self._leave(asyncio.to_thread(manager.current, key))
                if manager is not None
                else None
            )
            if not still_current():
                return False
            if (
                manager is not None
                and failure.session_id
                and (
                    manager_record is None
                    or manager_record.session_id != failure.session_id
                )
            ):
                await self._leave(
                    asyncio.to_thread(manager.store.set, key, failure.session_id)
                )
                if not still_current():
                    return False
                manager_record = await self._leave(asyncio.to_thread(manager.current, key))
                if not still_current():
                    return False
            if (
                manager is not None
                and manager_record is not None
                and manager_record.session_id == failure.session_id
            ):
                def rotate() -> tuple[str | None, str | None]:
                    manager.record_failure(key, name)
                    result = manager.rotate(key, reason=name)
                    if not result.rotated:
                        return None, None
                    path = str(result.handoff_path) if result.handoff_path else None
                    return result.session_id, path

                session_id, handoff_path = await self._leave(asyncio.to_thread(rotate))
                if not still_current():
                    return False
                entry = self._recovery_line(dispatch, line)
                entry["session_id"] = session_id
                entry["session_mode"] = "fresh" if session_id else None
                entry["session_author"] = provider if session_id else None
                entry.pop("session_origin", None)
                entry["handoff_path"] = handoff_path
            else:
                # First-turn failures have no manager record to archive. A
                # fresh explicit ID still isolates the retry from other lines.
                entry = self._recovery_line(dispatch, line)
                entry["session_id"] = uuid4().hex if explicit else None
                entry["session_mode"] = "fresh" if explicit else None
                entry["session_author"] = provider if explicit else None
                entry.pop("session_origin", None)
                entry["handoff_path"] = None
            await self._save_locked(dispatch)
            return still_current()
        if action.kind == "reassign_fallback":
            await self._save_locked(dispatch)
            return await self._reassign(dispatch, line, action.reason or "worker_failure")
        entry = self._recovery_line(dispatch, line)
        entry["parked_reason"] = safe_reason
        self._block_line(dispatch, line, safe_reason)
        if self._c.gates is not None:
            snap = self._c.gates.line_snapshot(dispatch, line)
            state = _load_state(snap)
            if state.stage is Stage.IMPLEMENTING:
                # The worker has exited and nothing runs: a checkpoint, so a
                # human may reassign this pause (N38b). A pause that does
                # not record this stays mid-implementation.
                snap[WORKER_EXITED] = {
                    "author": author,
                    "attempt": attempt,
                    "run_id": failure.run_id,
                    "failure_class": name,
                    "exit_code": getattr(failure, "exit_code", None),
                }
            if state.stage is not Stage.BLOCKED:
                self._advance(
                    dispatch,
                    line,
                    "block",
                    provider=line.assigned_model or "",
                )
        await self._save_locked(dispatch)
        return False

    async def _reserve_run(
        self, dispatch: Dispatch, line: Line
    ) -> tuple[str, str | None, int] | None:
        """Persist a globally unique run id after the epoch check, before launch."""
        if self._abandoned:
            return None
        run_id = f"{dispatch.id}:{line.id}:{line.attempts}:{uuid4().hex[:12]}"
        previous = line.current_run
        attempt = line.attempts
        line.current_run = run_id
        self._stamp_reserved_fix(dispatch, line, run_id)
        try:
            await self._save_locked(dispatch)
        except StaleDrive:
            line.current_run = previous
            raise
        return run_id, previous, attempt

    async def _apply_launch_done(
        self, event: LaunchDone, dispatch: Dispatch, line: Line
    ) -> None:
        if self._abandoned:
            return
        if not _event_current(dispatch, line, event) or (
            event.author and event.author != (line.assigned_model or "")
        ):
            await self._discard_stale(dispatch)
            return
        if event.handoff is not None:
            await self._apply_handoff_done(event, dispatch, line)
            return
        line.current_run = event.launched
        line.status = LineStatus.RUNNING
        # The reassigned attempt's run has started and ended: its brief is
        # spent, so a later relaunch of the same attempt does not reuse it.
        drop_reassign_brief(dispatch, line.id)
        put = self._own_put_run
        assert put is not None
        # Before the fix intent is cleared: the entry says work or fix.
        self._cost_start(dispatch, line, event)
        put(
            line.id,
            _Run(
                started_at=self._clock(),
                started_wall=event.started_wall or self._cost_wall(),
                usage=event.usage,
                # Read on the writer, synchronously: no hand-off between the
                # launcher returning and this run being installed.
                quota_after=self._cost_quota(event.author or line.assigned_model or ""),
            ),
        )
        # The same save that confirms the start drops the fix intent.
        self._clear_pending_fix(dispatch, line.id)
        if event.session_id:
            self._remember_session(dispatch, line, event.session_id)
        recovery_entry = self._recovery_data(dispatch).get(line.id)
        if isinstance(recovery_entry, dict):
            recovery_entry["attempts"] = {}
            recovery_entry.pop("allow_dirty_worktree", None)
            recovery_entry.pop("handoff_pending", None)
            recovery_entry.pop("retry_at", None)
            recovery_entry.pop("retry_source", None)
            recovery_entry.pop("prompt_note", None)
        recovery = self._recovery_data(dispatch)
        worker_states = recovery.get("workers")
        if isinstance(worker_states, dict):
            worker_states.pop((line.assigned_model or "unknown").lower(), None)
        await self._save_locked(dispatch)
        if event.session_id:
            await self._record_session_success(
                dispatch,
                line,
                LaunchResult(
                    event.run_id, event.session_id, "", usage=event.usage, estimate=event.estimate
                ),
                worker_requested=event.worker_requested,
            )

    # N30 cost ledger (gates["cost"], logic in cost.py). Stage-Gate mode only
    # (dev mode keeps no gates). Best effort: a hook never stops the dispatch
    # and stores numbers and stable codes only, no log or model text. Each
    # runs on the writer before the caller's save.
    def _cost_on(self) -> bool:
        return self._c.gates is not None and dispatch_cost.ledger_enabled()

    def _cost_wall(self) -> datetime:
        """The Stage-Gate clock its events are stamped with, else the wall clock."""
        clock = getattr(self._c.gates, "_clock", None)
        moment = clock() if callable(clock) else None
        if isinstance(moment, datetime):
            return moment.astimezone(timezone.utc)
        return datetime.fromtimestamp(self._wall(), timezone.utc)

    def _cost_quota(self, model: str) -> dict[str, Any] | None:
        """One small snapshot-file read, synchronous on purpose.

        An ``await`` here would hand the writer off mid-launch (a monitor event
        could install a run that the adopt step then overwrites), so the
        ledger adds no await point to the state machine.
        """
        if not model or not self._cost_on():
            return None
        try:
            return dispatch_cost.read_quota(model)
        except Exception:  # noqa: BLE001 - a quota reading never stops a launch
            return None

    def _cost_meta(self, dispatch: Dispatch, line: Line) -> tuple[str, int]:
        lines = dispatch.gates.get("lines") if isinstance(dispatch.gates, dict) else None
        snap = lines.get(line.id) if isinstance(lines, dict) else None
        raw = snap.get("fix_round") if isinstance(snap, dict) else 0
        fix_round = raw if isinstance(raw, int) and not isinstance(raw, bool) else 0
        return ("fix" if self._is_fix_round(dispatch, line) else "work"), fix_round

    def _cost_start(self, dispatch: Dispatch, line: Line, event: LaunchDone) -> None:
        if not self._cost_on():
            return
        try:
            kind, fix_round = self._cost_meta(dispatch, line)
            dispatch_cost.record_attempt_start(
                dispatch.gates,
                line.id,
                run_id=event.run_id,
                attempt=event.attempt,
                model=event.author or line.assigned_model or "",
                kind=kind,
                fix_round=fix_round,
                started_at=event.started_wall or self._cost_wall(),
                quota_before=event.quota_before,
            )
        except Exception:  # noqa: BLE001 - the ledger never stops a dispatch
            logger.warning("cost ledger: start of %s not recorded", event.run_id, exc_info=True)

    def _cost_end(
        self,
        dispatch: Dispatch,
        line: Line,
        run_id: str | None,
        *,
        result: str,
        failure_class: str | None = None,
        reason_code: str | None = None,
        usage: Any | None = None,
        tokens_est: int = 0,
        quota_after: dict[str, Any] | None = None,
        model: str | None = None,
        attempt: int | None = None,
    ) -> None:
        if not run_id or not self._cost_on():
            return
        tokens_in = getattr(usage, "input_tokens", None)
        tokens_out = getattr(usage, "output_tokens", None)
        if not (tokens_in or tokens_out):
            # An empty usage (no parser, Kimi) is not a CLI count of zero.
            tokens_in = tokens_out = None
        try:
            kind, fix_round = self._cost_meta(dispatch, line)
            dispatch_cost.record_attempt_end(
                dispatch.gates,
                line.id,
                run_id=run_id,
                ended_at=self._cost_wall(),
                result=result,
                failure_class=failure_class,
                reason_code=reason_code,
                tokens_in=None if tokens_in is None else int(tokens_in),
                tokens_out=None if tokens_out is None else int(tokens_out),
                tokens_est=max(0, int(tokens_est or 0)),
                quota_after=quota_after,
                model=model or line.assigned_model or "",
                attempt=line.attempts if attempt is None else attempt,
                kind=kind,
                fix_round=fix_round,
            )
        except Exception:  # noqa: BLE001 - the ledger never stops a dispatch
            logger.warning("cost ledger: end of %s not recorded", run_id, exc_info=True)

    async def _apply_handoff_done(
        self, event: LaunchDone, dispatch: Dispatch, line: Line
    ) -> None:
        """N29: confirm a run that ended in a worker-requested handoff.

        The same bookkeeping as a confirmed start, except that the line stays
        ASSIGNED (a status the poll ignores) on this run with no ``_Run``, the
        recovery entry names the rotated session, and the decision is saved
        as ``handoff_pending`` for a restart. The fix intent and a reassign
        brief stay for the relaunch, whose own LaunchDone spends them.
        """
        handoff = dict(event.handoff or {})
        line.current_run = event.launched
        line.status = LineStatus.ASSIGNED
        entry = self._recovery_line(dispatch, line)
        entry["attempts"] = {}
        for name in ("retry_at", "retry_source", "prompt_note"):
            entry.pop(name, None)
        session_id = handoff.get("session_id")
        entry["session_id"] = session_id
        entry["session_mode"] = "fresh" if session_id else None
        entry["session_author"] = (event.author or "").lower() if session_id else None
        entry.pop("session_origin", None)
        entry["handoff_path"] = handoff.get("handoff_path")
        # Uncommitted work may be left for the next session.
        entry["allow_dirty_worktree"] = True
        entry["handoff_pending"] = handoff
        worker_states = self._recovery_data(dispatch).get("workers")
        if isinstance(worker_states, dict):
            worker_states.pop((line.assigned_model or "unknown").lower(), None)
        await self._save_locked(dispatch)

    async def _drop_unstarted_run(
        self,
        dispatch: Dispatch,
        line: Line,
        run_id: str,
        previous: str | None,
    ) -> None:
        """A launch that never returned must not leave its reservation behind."""
        if not self._on_actor():
            await self._submit(self._drop_unstarted_run, dispatch, line, run_id, previous)
            return
        if line.current_run != run_id or line.status is LineStatus.RUNNING:
            return
        line.current_run = previous
        if self._abandoned:
            return
        try:
            await self._save_locked(dispatch)
        except StaleDrive:
            return

    def _enter_implementation(self, dispatch: Dispatch, line: Line) -> bool:
        """Assign and start, then the caller saves, and only then the worker starts.

        A fix round is already in ``fixing`` and is left there. Returns True
        when the snapshot changed and must be saved before ``launch``.
        """
        gates = self._c.gates
        assert gates is not None
        state = _load_state(gates.line_snapshot(dispatch, line))
        provider = line.assigned_model or ""
        if state.stage is Stage.DAG_READY:
            self._advance(dispatch, line, "assign", provider=provider)
            self._advance(dispatch, line, "start_implementation")
            return True
        if state.stage is Stage.ASSIGNED:
            self._advance(dispatch, line, "start_implementation")
            return True
        return False

    async def _give_up_launch(
        self, dispatch: Dispatch, line: Line, reason: str, *, fix: bool
    ) -> None:
        """A fix stays with its author. An ordinary launch may still be reassigned."""
        # Unclassified launch errors: one entry for the attempt, a stable code.
        self._cost_end(
            dispatch, line, f"{dispatch.id}:{line.id}:{line.attempts}:launch_failed",
            result="failed", reason_code=FIX_LAUNCH_FAILED if fix else "launch_failed",
        )
        if fix:
            self._clear_pending_relaunch(dispatch, line)
            self._clear_pending_fix(dispatch, line.id)
            self._block_line(dispatch, line, FIX_LAUNCH_FAILED)
            await self._park_blocked(dispatch)
            return
        if not await self._reassign(dispatch, line, reason):
            return
        await self._launch_line(dispatch, line)

    async def _poll_line(self, dispatch: Dispatch, line: Line) -> None:
        if not line.current_run:
            return
        watched = line.current_run
        attempt = line.attempts
        author = line.assigned_model or ""
        epoch = drive_epoch(dispatch)
        # The poll itself is a worker. A heartbeat may be saving this same row.
        poll = self._c.monitor.poll
        if self._poll_accepts_model:
            status: RunStatus = await self._leave(poll(watched, model=author))
        else:
            status = await self._leave(poll(watched))
        failure = None
        exit_code = getattr(status, "exit_code", None)
        failure_class = getattr(status, "failure_class", None)
        if exit_code not in (None, 0) and failure_class:
            recovery = self._recovery_data(dispatch)
            line_state = recovery.get(line.id)
            session_id = (
                line_state.get("session_id")
                if isinstance(line_state, dict)
                and isinstance(line_state.get("session_id"), str)
                else None
            )
            failure = WorkerLaunchFailure(
                run_id=watched,
                exit_code=exit_code,
                output=status.output,
                session_id=session_id,
                retry_after_seconds=(
                    int(getattr(status, "retry_after_seconds", None))
                    if getattr(status, "retry_after_seconds", None) is not None
                    else None
                ),
                failure_class=FailureClass(failure_class),
                reason_code=getattr(status, "reason_code", None) or "worker_failure",
            )
        event = PollResult(
            dispatch_id=dispatch.id,
            line_id=line.id,
            run_id=watched,
            attempt=attempt,
            output=status.output,
            complete=status.complete,
            stalled=status.stalled,
            timed_out=status.timed_out,
            tokens=status.tokens,
            author=author,
            epoch=epoch,
            failure=failure,
            report=str(getattr(status, "report", "") or ""),
            silent=getattr(status, "silent", None),
        )
        action = await self._submit(self._apply_poll_event, event, dispatch, line)
        if action == "recover":
            await self._launch_line(dispatch, line)
            return
        if action not in ("reassign", p0_hooks.REASSIGN_SILENT):
            return
        # Cancel is worker I/O. Reassign and the next launch are drive writes.
        await self._leave(self._c.monitor.cancel(watched))
        await self._submit(
            self._apply_reassign_needed,
            ReassignNeeded(
                dispatch_id=dispatch.id,
                line_id=line.id,
                run_id=watched,
                attempt=attempt,
                reason=(
                    silent_worker.WORKER_NO_OUTPUT
                    if action == p0_hooks.REASSIGN_SILENT
                    else "log stalled" if status.stalled else "run timed out"
                ),
            ),
            dispatch,
            line,
        )

    async def _apply_reassign_needed(
        self, event: ReassignNeeded, dispatch: Dispatch, line: Line
    ) -> None:
        """Reassign and relaunch on the writer. A poll worker only enqueues this."""
        if not self._on_actor():
            await self._submit(self._apply_reassign_needed, event, dispatch, line)
            return
        if line.status is LineStatus.BLOCKED or line.failure_reason == "watchdog_timeout":
            return
        if await self._fence(dispatch):
            return
        if not _event_current(dispatch, line, event):
            await self._discard_stale(dispatch)
            return
        if not await self._reassign(dispatch, line, event.reason):
            return
        await self._launch_line(dispatch, line)

    async def _apply_poll_event(
        self, event: PollResult, dispatch: Dispatch, line: Line
    ) -> str:
        """Apply one poll on the actor. Returns ``done``, ``reassign``, or ``ignore``."""
        if await self._fence(dispatch):
            return "ignore"
        # A deadline that already won stays won. The later completion is audit only.
        if line.status is LineStatus.BLOCKED or line.failure_reason == "watchdog_timeout":
            await self._discard_stale(dispatch)
            return "ignore"
        if (
            not _event_current(dispatch, line, event)
            or (event.author and event.author != (line.assigned_model or ""))
            or (event.epoch and event.epoch != drive_epoch(dispatch))
        ):
            await self._discard_stale(dispatch)
            return "ignore"
        if event.failure is not None:
            line.status = LineStatus.ASSIGNED
            recovery = await self._recover_worker_failure(
                dispatch,
                line,
                event.failure,
                self._is_fix_round(dispatch, line),
                attempt=event.attempt,
                author=event.author,
                epoch=event.epoch,
            )
            return "recover" if recovery else "ignore"
        ensure = self._own_ensure_run
        put_output = self._own_put_output
        assert ensure is not None and put_output is not None
        run = ensure(line.id)
        run.tokens = event.tokens
        put_output(line.id, event.output)
        line.log_tail = event.output
        if event.silent is not None and self._note_silent(dispatch, line, event):
            # P0 enforce: no model output costs the attempt; the reassign
            # that follows saves this with the line's new state.
            self._cost_end(
                dispatch, line, event.run_id or line.current_run, result="failed",
                reason_code=silent_worker.WORKER_NO_OUTPUT,
                usage=run.usage, tokens_est=run.tokens, quota_after=run.quota_after,
            )
            return p0_hooks.REASSIGN_SILENT
        if event.complete:
            p0_hooks.launch_settle(self._dedup_book, dispatch, line, "done")
            self._cost_end(
                dispatch, line, event.run_id or line.current_run, result="ok",
                usage=run.usage, tokens_est=run.tokens, quota_after=run.quota_after,
            )
            # The line stays RUNNING — that is what REVIEW picks up — but the
            # monitor loop stops watching it, or a finished line is polled
            # forever. The completion fact and the submit transition are saved
            # before G2 starts.
            run.complete = True
            if self._c.gates is not None:
                self._c.gates.mark_run_settled(dispatch, line)
                event_id = f"run-{line.current_run or line.id}"
                self._c.gates.push_wakeup(
                    dispatch,
                    event_id,
                    "run_complete",
                    line.id,
                    run_id=line.current_run,
                    attempt=line.attempts,
                )
                self._c.gates.ack_wakeup(dispatch, event_id)
                state = _load_state(self._c.gates.line_snapshot(dispatch, line))
                if state.stage is Stage.IMPLEMENTING:
                    self._ensure_submitted(dispatch, line)
            self._record_ruling_check(dispatch, line, event.report or event.output)
            await self._save_locked(dispatch)
            return "done"
        if event.stalled or event.timed_out:
            # The reassign that follows saves this with the line's new state.
            self._cost_end(
                dispatch, line, event.run_id or line.current_run, result="failed",
                reason_code="worker_timed_out" if event.timed_out else "worker_stalled",
                usage=run.usage, tokens_est=run.tokens, quota_after=run.quota_after,
            )
            return "reassign"
        return "ignore"

    def _note_silent(self, dispatch: Dispatch, line: Line, event: PollResult) -> bool:
        """P0: record a silent-worker finding, alert once per run; True in enforce."""
        if not silent_worker.should_record():
            return False
        gates = self._c.gates
        alert = None
        if gates is not None or os.environ.get("CONDUCTOR_DISPATCH_INBOX_DIR"):
            def alert(text: str) -> None:
                self._write_needs_human_note(dispatch, line, text)
        cell = p0_hooks.silent_cell(dispatch, line, gates)
        author = event.author or line.assigned_model or ""
        return p0_hooks.note_silent(
            cell, event.silent, line_id=line.id, run_id=event.run_id,
            attempt=event.attempt, author=author, alert=alert,
        ) and p0_hooks.silent_enforce_allowed(
            cell, event.silent, self._branch_runs(dispatch, line),
            attempt=event.attempt, author=author,
        )

    async def _apply_heartbeat(self, event: Heartbeat, dispatch: Dispatch) -> None:
        del event
        gates = self._c.gates
        assert gates is not None
        gates.beat(dispatch)
        await self._save_locked(dispatch)

    def _completion_queued(self, event: Deadline) -> bool:
        """True when an earlier completion for this run is still in the mailbox.

        ``seq`` is taken at enqueue. A completion that was queued later has a
        higher seq and must not discard this deadline.
        """
        pending = getattr(self._queue, "_queue", ())
        for item in pending:
            args = item[1]
            if not args or not isinstance(args[0], PollResult):
                continue
            poll = args[0]
            if (
                poll.complete
                and poll.seq < event.seq
                and poll.dispatch_id == event.dispatch_id
                and poll.line_id == event.line_id
                and poll.run_id == event.run_id
                and poll.attempt == event.attempt
            ):
                return True
        return False

    def _deadline_obsolete(self, dispatch: Dispatch, event: Deadline) -> bool:
        """A deadline loses to an earlier completion, and to a new run."""
        line = dispatch.line(event.line_id)
        if line is None:
            return True
        if line.current_run != event.run_id or line.attempts != event.attempt:
            return True
        if line.status is not LineStatus.RUNNING:
            return True
        run = self._runs.get(line.id)
        if run is not None and run.complete:
            return True
        gates = self._c.gates
        if gates is not None and gates.run_is_settled(dispatch, line):
            return True
        return self._completion_queued(event)

    async def _apply_deadline(self, event: Deadline, dispatch: Dispatch) -> None:
        """Settle a deadline without waiting for a selector or a retry sleep.

        A completion that is already applied, or that was queued earlier, is
        kept. A completion queued after this deadline does not rewrite the line.
        """
        if not self._on_actor():
            await self._submit(self._apply_deadline, event, dispatch)
            return
        if self._deadline_obsolete(dispatch, event):
            return
        line = dispatch.line(event.line_id)
        if line is None:
            return
        for task in list(self._inflight):
            if not task.done():
                task.cancel()
        await self._watchdog(dispatch, [line])

    async def _pop_run(self, line_id: str) -> None:
        pop = self._own_pop_run
        assert pop is not None
        pop(line_id)

    async def _adopt_pair(self, dispatch: Dispatch, saved: Dispatch) -> None:
        self._note_epoch(dispatch, saved)

    async def _reassign(self, dispatch: Dispatch, line: Line, reason: str) -> bool:
        """Give the line a fresh model, or fail it once its attempts run out.

        Selection and the retry sleep are workers. The actor never waits on them,
        so a deadline can be applied while the selector is still stalled.
        """
        decision = await self._submit(self._reassign_prepare, dispatch, line, reason)
        if decision != "select":
            return bool(decision)
        # The current author is only a soft skip. Round-robin moves on; a
        # selector that ignores used_models may return the same author, and
        # that reassignment keeps the branch. Liveness recovery passes the
        # outgoing author as ``exclude``, which is the hard exclusion.
        current = line.assigned_model or ""
        try:
            # Skipped providers are a hard exclusion (_select_with_retry).
            model = await self._select_with_retry(
                dispatch, line, used_extra=[current] if current else None
            )
        except asyncio.CancelledError:
            raise
        if line.failure_reason == "watchdog_timeout" or line.status is LineStatus.BLOCKED:
            return False
        return bool(
            await self._submit(self._reassign_finish, dispatch, line, reason, model)
        )

    async def _reassign_prepare(self, dispatch: Dispatch, line: Line, reason: str) -> bool | str:
        p0_hooks.launch_settle(self._dedup_book, dispatch, line, "failed")
        line.attempts += 1
        line.failure_reason = reason
        if line.attempts > self._config.max_reassigns:
            line.status = LineStatus.FAILED
            line.current_run = None
            dispatch.state = DispatchState.ABORT
            dispatch.message = (
                f"Line {line.id} failed after {line.attempts} attempts: {reason}"
            )
            await self._save_locked(dispatch)
            return False
        return "select"

    async def _reassign_finish(
        self,
        dispatch: Dispatch,
        line: Line,
        reason: str,
        model: str | None,
        *,
        force: bool = False,
    ) -> bool:
        if not force and (
            line.status is LineStatus.BLOCKED or line.failure_reason == "watchdog_timeout"
        ):
            return False
        if not force and dispatch.state in (
            DispatchState.ABORT,
            DispatchState.ERROR,
            DispatchState.REVIEW,
        ):
            if line.status is not LineStatus.RUNNING:
                return False
        if model is None:
            # N86: a human-pinned line parks instead of aborting the dispatch.
            if human_pin.park_pinned_unusable(dispatch, line, reason):
                await self._save_locked(dispatch)
                return False
            line.status = LineStatus.FAILED
            dispatch.state = DispatchState.ABORT
            dispatch.message = f"Line {line.id} could not be reassigned: {reason}"
            await self._save_locked(dispatch)
            return False
        previous_author = line.assigned_model or ""
        line.assigned_model = model
        line.status = LineStatus.ASSIGNED
        line.current_run = None
        if previous_author.lower() != model.lower():
            old_branch = line.branch
            if old_branch:
                recovery_entry = self._recovery_line(dispatch, line)
                branch_events = recovery_entry.get("branch_events")
                if not isinstance(branch_events, list):
                    branch_events = []
                    recovery_entry["branch_events"] = branch_events
                new_branch = f"{dispatch.branch_prefix}/line-{line.id}-a{line.attempts}"
                branch_events.append(
                    {
                        "event": "author_reassigned",
                        "from_author": previous_author,
                        "to_author": model,
                        "old_branch": old_branch,
                        "new_branch": new_branch,
                        "attempt": line.attempts,
                    }
                )
                line.branch = new_branch
                if self._c.gates is not None:
                    # A new branch from base: nothing on it was ever reviewed.
                    # A reused name keeps its existing ledger and heads.
                    _open_reviewed_ledger(dispatch, line, complete=True)
            recovery_entry = self._recovery_data(dispatch).get(line.id)
            if isinstance(recovery_entry, dict):
                recovery_entry["session_id"] = None
                recovery_entry["session_mode"] = None
                recovery_entry["session_author"] = None
                recovery_entry.pop("session_origin", None)
                recovery_entry.pop("handoff_path", None)
                recovery_entry["attempts"] = {}
                recovery_entry.pop("retry_at", None)
                recovery_entry.pop("retry_source", None)
                recovery_entry.pop("allow_dirty_worktree", None)
        self._install_worker(dispatch, line, model)
        saved = await self._commit_line_locked(dispatch, line, bump=True)
        return saved is not None

    def _install_worker(self, dispatch: Dispatch, line: Line, model: str) -> None:
        """The fallback worker is the author of record before launch.

        A failed launch has not submitted, so the previous model is replaced.
        No Stage-Gate event is emitted. A fix launch does not call this.
        """
        gates = self._c.gates
        if gates is None:
            return
        snap = gates.line_snapshot(dispatch, line)
        state = _load_state(snap)
        _store_state(
            snap,
            replace(
                state,
                author_provider=model,
                developer_provider=model,
                contributors=(model,),
            ),
        )

    async def _select_with_retry(
        self,
        dispatch: Dispatch,
        line: Line,
        *,
        role: ModelRole = ModelRole.WORK,
        exclude: list[str] | None = None,
        never: list[str] | None = None,
        used_extra: list[str] | None = None,
    ) -> str | None:
        # ``exclude`` is a hard work exclusion: the production selector ignores
        # ``used_models`` for work, so a model named only there can be seated
        # again. ``used_extra`` stays soft (a same-author reassign may keep it).
        # Review already honours ``used_models``, so exclude stays soft there.
        used = self._selection_used(
            dispatch,
            line,
            [*(exclude or []), *(used_extra or [])],
            role=role,
        )
        hard = [*self._skipped_providers(dispatch), *(never or [])]
        if role is ModelRole.WORK:
            hard.extend(name for name in (exclude or []) if name)
            # R6 L2: the role registry's worker constraints and owner order.
            hard.extend(self._registry_assignment(dispatch, line))
        model = await self._select_model(dispatch, line, used, role=role, never=hard)
        if model is not None:
            return model
        if role is ModelRole.WORK and getattr(self._c.selector, "work_decision", None) == "queue":
            return None
        await self._leave(self._sleep(self._config.assign_retry_seconds))
        return await self._select_model(dispatch, line, used, role=role, never=hard)

    async def _superagent_decisions(self, dispatch: Dispatch) -> dict[str, Any]:
        """N73 L4: the Super-agent's choices, or ``{}`` (off, unwired, or a fault)."""
        assigner = self._c.assigner
        if assigner is None:
            return {}
        lines = [
            line
            for line in dispatch.lines
            if line.status is not LineStatus.FAILED and not line.assigned_model
        ]
        if not lines:
            return {}
        try:
            return dict(await self._leave(assigner.decide(dispatch, lines)))
        except asyncio.CancelledError:
            raise
        except Exception:  # an ERROR is never a choice; the selector stands
            logger.warning("Super-agent assignment failed for %s", dispatch.id, exc_info=True)
            return {}

    def _apply_superagent(
        self, dispatch: Dispatch, line: Line, selected: str, decision: Any
    ) -> str:
        """Record the choice next to the selector's; adopt it only when sampled.

        ``shadow`` launches ``selected`` (the selector's pick). ``enforce``
        launches the Super-agent's pick on a sampled line, still inside the
        role registry's worker constraints (R6 L2). Returns the model to seat.
        """
        if decision is None:
            return selected
        # Lazy: importing the package must not load the sibling modules.
        from .superagent_assign import launch_env as superagent_launch_env
        from .superagent_assign import record as superagent_record

        assigner = self._c.assigner
        sample = assigner.sample() if assigner is not None else 0.0
        adopt = decision.adopted(sample)
        source = decision.source
        fallback = decision.fallback_reason
        if adopt and decision.choice is not None:
            from .pool import eligible_models

            roster = [decision.choice.model, *eligible_models("work")]
            never = set(dynamic_assign.decide(line, roster).never)
            if decision.choice.model in never:
                adopt = False
                source = superagent_record.SOURCE_SELECTOR_FALLBACK
                fallback = "registry_excluded"
        if decision.mode == "enforce" and not adopt and decision.usable and fallback is None:
            # An unsampled enforce line runs the selector on purpose.
            source = superagent_record.SOURCE_SELECTOR
        model = selected
        env: dict[str, str] = {}
        if adopt and decision.choice is not None:
            model = decision.choice.model
            if model != selected:
                release = getattr(self._c.selector, "release", None)
                if callable(release):
                    release(selected)
                # Review fix 6: the adopted model carries the load count.
                reserve = getattr(self._c.selector, "_take", None)
                if callable(reserve):
                    reserve(model)
            env = superagent_launch_env.env_for_choice(decision.choice)
            superagent_launch_env.register(dispatch.id, line.id, env)
        else:
            superagent_launch_env.clear(dispatch.id, line.id)
        gates = self._c.gates
        if gates is not None:
            snap = gates.line_snapshot(dispatch, line)
            # Review fix 5: persisted, so a restarted drive rebuilds it.
            if env:
                snap[superagent_launch_env.SNAPSHOT_KEY] = dict(env)
            else:
                snap.pop(superagent_launch_env.SNAPSHOT_KEY, None)
            try:
                updated = superagent_record.append_assignment(
                    {"lines": {line.id: snap}},
                    line_id=line.id,
                    run_id=f"{line.id}:assign:{line.attempts}",
                    mode=decision.mode,
                    source=source,
                    choice=decision.choice.to_dict() if decision.choice is not None else None,
                    selector_choice={"model": selected},
                    feasibility=decision.attempts,
                    reask=decision.reask,
                    fallback_reason=fallback,
                    latency_ms=decision.latency_ms,
                )
                snap["assignment"] = updated["lines"][line.id]["assignment"]
            except (TypeError, ValueError):
                logger.warning("assignment record refused for %s", line.id, exc_info=True)
        return model

    def _registry_assignment(self, dispatch: Dispatch, line: Line) -> list[str]:
        """R6 L2: exclusions from the role registry; sets the owner preference.

        A legacy (fallback) registry returns nothing and changes nothing, so
        an install without a stored registry selects exactly as before.
        """
        from .pool import eligible_models

        try:
            decision = dynamic_assign.decide(line, eligible_models(ModelRole.WORK.value))
        except Exception:  # the registry is advisory input; never fail a pick
            logger.warning("role registry unavailable for %s", line.id, exc_info=True)
            return []
        selector = self._c.selector
        if hasattr(selector, "owner_preference"):
            selector.owner_preference = decision.preferred
        if decision.active and self._c.gates is not None:
            dynamic_assign.record(self._c.gates.line_snapshot(dispatch, line), decision)
        return list(decision.never)

    async def _debug_switch(
        self, dispatch: Dispatch, line: Line, point: str, prior_failures: int
    ) -> bool:
        """R6 L4: a different model in a new session at a repeated debug point.

        Returns True when the line was handed to someone else (enforce only).
        ``shadow`` logs the plan and keeps it in ``debug_switch_audit``.
        """
        mode = debug_switch.mode()
        if mode == "off":
            return False
        plan = debug_switch.plan(
            point, line, prior_failures, session=str(line.current_run or "")
        )
        audit = self.__dict__.setdefault("debug_switch_audit", [])
        audit.append({"mode": mode, **plan.to_dict()})
        del audit[: -debug_switch.AUDIT_MAX]
        logger.info("debug switch %s: %s %s", mode, line.id, plan.reason)
        if mode != "enforce" or not plan.switch:
            return False
        if self._c.gates is not None:
            snap = self._c.gates.line_snapshot(dispatch, line)
            records = snap.setdefault("debug_switch", [])
            if isinstance(records, list):
                records.append(plan.to_dict())
                del records[: -debug_switch.AUDIT_MAX]
        return bool(await self._reassign(dispatch, line, plan.reason))

    async def _select_model(
        self,
        dispatch: Dispatch,
        line: Line,
        used: list[str],
        *,
        role: ModelRole,
        never: list[str],
    ) -> str | None:
        """One selector call. ``never`` is a hard exclusion the selector honours.

        The live selector reads ``used_models`` only for review, so a model a
        line must leave (a skipped provider, its previous author) goes in
        ``exclude``. A selector without that keyword gets it in ``used`` and
        the result is checked again.
        """
        selector = self._c.selector
        names = [name for name in dict.fromkeys(never) if name]
        kwargs: dict[str, Any] = {}
        selected_used = used
        if names and _accepts_keyword_only(selector.select, "exclude"):
            kwargs["exclude"] = tuple(names)
        elif names:
            selected_used = [*used, *names]
        if _callable_accepts_keyword(selector.select, "dispatch"):
            kwargs["dispatch"] = dispatch
        model = await self._leave(
            selector.select(line, selected_used, role=role, **kwargs)
        )
        if model is not None and model.strip().lower() in {n.strip().lower() for n in names}:
            return None
        return model

    def _require_serial_idle(
        self, dispatch: Dispatch, launching: list[Line] | None = None
    ) -> None:
        """Raise while some other line is RUNNING or implementing.

        ``_launch_guarded`` does not raise. It defers that line. This helper
        stays so a test can still catch the same overlap.
        """
        allowed = {line.id for line in launching or []}
        active = self._active_except(dispatch, allowed)
        if not active:
            return
        names = ", ".join(line.id for line in active)
        raise DispatchError(
            f"serial invariant: {len(active)} active line(s) ({names})"
        )

    def _active_except(self, dispatch: Dispatch, skip: set[str]) -> list[Line]:
        return [
            line
            for line in _serial_active(dispatch, self._c.gates)
            if line.id not in skip
        ]

    def _pending_relaunch_ids(self, dispatch: Dispatch) -> set[str]:
        """Line ids waiting on ``serial_wait``. Stored on the scheduler."""
        sched = self._scheduler_dict(dispatch)
        found: set[str] = set()
        raw = sched.get("pending_relaunch") if sched is not None else None
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict) or item.get("reason") != "serial_wait":
                    continue
                line_id = item.get("line_id")
                if isinstance(line_id, str) and line_id:
                    found.add(line_id)
        if found:
            return found
        return {
            line.id
            for line in dispatch.lines
            if line.failure_reason == "serial_wait" and line.status is LineStatus.ASSIGNED
        }

    def _scheduler_dict(self, dispatch: Dispatch) -> dict[str, object] | None:
        gates = self._c.gates
        if gates is None:
            return None
        return gates._scheduler(dispatch)

    def _nonpending_active(self, dispatch: Dispatch) -> list[Line]:
        """Active lines that are not themselves waiting to relaunch."""
        return self._active_except(dispatch, self._pending_relaunch_ids(dispatch))

    def _launch_blockers(self, dispatch: Dispatch, line: Line) -> list[Line]:
        """Other unfinished lines. This line and other deferred lines are not blockers.

        A line whose self-report is in progress is included, so a sibling defers
        with ``serial_wait`` until that ask finishes.
        """
        skip = set(self._pending_relaunch_ids(dispatch))
        skip.add(line.id)
        blockers = self._active_except(dispatch, skip)
        if self._parallel_limit() > 1 and len(blockers) < self._parallel_limit():
            # Parallel lines (N31): only a line this one may not run beside
            # blocks it (the same provider beyond capacity, overlapping scope).
            blockers = self._line_conflicts(line, blockers)
        reporting_id = self._self_report_line
        if (
            reporting_id is not None
            and reporting_id != line.id
            and all(blocker.id != reporting_id for blocker in blockers)
        ):
            for other in dispatch.lines:
                if other.id == reporting_id:
                    blockers.append(other)
                    break
        return blockers

    def _mark_pending_relaunch(self, dispatch: Dispatch, line: Line) -> None:
        line.failure_reason = "serial_wait"
        sched = self._scheduler_dict(dispatch)
        if sched is None:
            return
        raw = sched.get("pending_relaunch")
        entries = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
        entries = [item for item in entries if item.get("line_id") != line.id]
        entries.append({"line_id": line.id, "reason": "serial_wait"})
        sched["pending_relaunch"] = entries

    def _clear_pending_relaunch(self, dispatch: Dispatch, line: Line) -> None:
        if line.failure_reason == "serial_wait":
            line.failure_reason = None
        sched = self._scheduler_dict(dispatch)
        if sched is None:
            return
        raw = sched.get("pending_relaunch")
        if not isinstance(raw, list):
            return
        sched["pending_relaunch"] = [
            item
            for item in raw
            if not (isinstance(item, dict) and item.get("line_id") == line.id)
        ]

    def _pending_fix(self, dispatch: Dispatch, line_id: str) -> dict[str, object] | None:
        """The structured fix intent for one line, or None."""
        root = dispatch.gates if isinstance(dispatch.gates, dict) else None
        if not isinstance(root, dict):
            return None
        raw = root.get("pending_fix")
        if not isinstance(raw, dict):
            return None
        item = raw.get(line_id)
        return dict(item) if isinstance(item, dict) else None

    def _pending_fix_current(
        self, dispatch: Dispatch, line: Line, intent: dict[str, object]
    ) -> bool:
        """True when ``source_ref`` still names the persisted review or G2 record."""
        if intent.get("policy") != "fix":
            return False
        kind = intent.get("kind")
        source = intent.get("source_ref")
        if kind not in ("review", "g2") or not isinstance(source, str) or not source:
            return False
        gates = self._c.gates
        if gates is None:
            return False
        snap = gates.line_snapshot(dispatch, line)
        handoff_ref = snap.get("handoff_ref")
        if kind == "review":
            return handoff_ref == source
        revision = _g2_cell(snap).get("revision")
        return revision == source or handoff_ref == source

    def _is_fix_round(self, dispatch: Dispatch, line: Line) -> bool:
        if self._pending_fix(dispatch, line.id) is not None:
            return True
        gates = self._c.gates
        if gates is None:
            return False
        return _load_state(gates.line_snapshot(dispatch, line)).stage is Stage.FIXING

    def _unconfirmed_ruling(
        self, dispatch: Dispatch, line: Line
    ) -> tuple[str, ...]:
        """Blocker lines for a fix whose report did not confirm its ruling.

        A fix round carries a ruling only when it answers a review BLOCK, so an
        empty result means there is nothing to enforce. A missing or malformed
        check is *not* a pass: an unverified contract must not reach review.
        """
        gates = self._c.gates
        if gates is None:
            return ()
        snap = gates.line_snapshot(dispatch, line)
        ruling = _ruling_from_dict(snap.get(FIX_RULING))
        if ruling is None:
            return ()
        raw = snap.get(RULING_CHECK)
        check = RulingCheck.from_dict(raw) if isinstance(raw, Mapping) else None
        if check is not None and check.complete:
            return ()
        return unconfirmed_blockers(ruling, check)

    def _note_unconfirmed_ruling(self, dispatch: Dispatch, line: Line) -> None:
        """Record an unconfirmed ruling instead of blocking on it (the default).

        A synthetic BLOCK would cost a fix round and count as a Stage-Gate
        review round on work no reviewer judged (a stall path, N76). The items
        are kept on the line snapshot (``ruling_unconfirmed``), one audit entry
        is written per new set, and ``_review_line`` puts them in the next
        reviewer's prompt.
        """
        gates = self._c.gates
        if gates is None:
            return
        snap = gates.line_snapshot(dispatch, line)
        items = list(self._unconfirmed_ruling(dispatch, line))
        if not items:
            snap.pop(RULING_UNCONFIRMED, None)
            return
        if snap.get(RULING_UNCONFIRMED) == items:
            return
        snap[RULING_UNCONFIRMED] = items
        ruling = _ruling_from_dict(snap.get(FIX_RULING))
        audit = gates._scheduler(dispatch).setdefault("audit", [])
        if isinstance(audit, list):
            audit.append(
                {
                    "kind": RULING_UNCONFIRMED,
                    "line_id": line.id,
                    "fix_round": ruling.fix_round if ruling is not None else None,
                    "items": [item.split(":", 1)[0].removeprefix("RULING ") for item in items],
                    "at": gates.stamp(),
                }
            )
        logger.info("Fix ruling for %s left unconfirmed; recorded for review", line.id)

    def _review_notes(
        self, line: Line, dispatch: Dispatch | None, *, reviewer: str | None = None
    ) -> str:
        """The unconfirmed ruling items the reviewer is asked to judge, or "".

        R6 L5 review isolation: a ruling is built from a blocking reviewer's
        conclusions. A secondary reviewer sees facts only (diff, G2 results),
        so it gets the items only when it issued that BLOCK itself.
        """
        gates = self._c.gates
        if gates is None or dispatch is None:
            return ""
        snap = gates.line_snapshot(dispatch, line)
        items = snap.get(RULING_UNCONFIRMED)
        if not isinstance(items, list) or not items:
            return ""
        if not review_isolation.notes_visible(snap, reviewer):
            return ""
        # Item ids and texts only: the synthetic "RULING Mx: not confirmed"
        # wording stays out, so a reviewer quoting it is never mistaken for
        # the ruling check's own BLOCK.
        listed = "\n".join(
            "- " + _UNCONFIRMED_WORDING.sub(r"\1:", str(item))[:_BRIEF_MAX_CHARS]
            for item in items[:_BRIEF_MAX_BLOCKERS]
        )
        return (
            "Unconfirmed rulings: this fix round carried a must / must-not ruling, "
            "and the author's report did not confirm these items. Check the code "
            "against each one and block on any that is not met:\n" + listed
        )

    async def _reject_unconfirmed_ruling(
        self, dispatch: Dispatch, line: Line
    ) -> bool:
        """Turn an unconfirmed fix report into a BLOCK, never an acceptance."""
        blockers = self._unconfirmed_ruling(dispatch, line)
        if not blockers:
            return False
        gates = self._c.gates
        assert gates is not None
        snap = gates.line_snapshot(dispatch, line)
        state = _load_state(snap)
        secondary = state.stage is Stage.SECONDARY_REVIEW
        reviewer = (
            state.blocking_reviewer
            or (state.secondary_reviewer if secondary else state.primary_reviewer)
            or ""
        )
        barred = {
            name
            for name in (
                state.author_provider,
                state.developer_provider,
                *state.contributors,
            )
            if name
        }
        if not reviewer or reviewer in barred:
            # Stage-Gate keeps the BLOCK's reviewer as the next blocking
            # reviewer, so a made-up name would later be launched as a real
            # review. With no eligible reviewer the real review runs instead.
            return False
        verdict = Verdict(passed=False, blockers=list(blockers))
        event = ReviewResult(
            dispatch_id=dispatch.id,
            line_id=line.id,
            run_id=line.current_run,
            attempt=line.attempts,
            passed=False,
            blockers=blockers,
        )
        await self._apply_review_result(
            event,
            dispatch,
            line,
            secondary=secondary,
            reviewer=reviewer,
            kind="secondary_review" if secondary else "primary_review",
            risk=state.risk_bearing or line.risk_bearing,
            verdict=verdict,
            state=state,
        )
        return True

    def _record_ruling_check(
        self, dispatch: Dispatch, line: Line, output: str
    ) -> None:
        """Check a fix round's report against its ruling and persist the result.

        The result is not merely audit data: ``_review_gated`` reads it and
        turns an incomplete check into a BLOCK before any reviewer accepts the
        fix. A report that says "done" without confirming each item therefore
        costs another fix round instead of passing.
        """
        gates = self._c.gates
        if gates is None:
            return
        snap = gates.line_snapshot(dispatch, line)
        ruling = _ruling_from_dict(snap.get(FIX_RULING))
        if ruling is None:
            return
        result = check_confirmation(output, ruling)
        snap[RULING_CHECK] = result.to_dict()
        if not result.complete:
            logger.info(
                "Fix ruling for %s is unconfirmed on %s",
                line.id,
                ", ".join(result.missing),
            )

    def _remember_pending_fix(
        self,
        dispatch: Dispatch,
        line: Line,
        *,
        kind: str,
        author: str,
        attempt: int,
        source_ref: str,
        g2_note: str = "",
    ) -> None:
        """Store the one fix intent. No task text. ``_begin_fix`` is the caller.

        A G2 fix keeps its G2 note (already redacted and bounded): the launch
        resets the G2 cell, so a resumed fix could not read it again.
        """
        root = dispatch.gates if isinstance(dispatch.gates, dict) else None
        gates = self._c.gates
        if not isinstance(root, dict) or gates is None:
            return
        snap = gates.line_snapshot(dispatch, line)
        try:
            fix_round = int(snap.get("fix_round") or 0)
        except (TypeError, ValueError):
            fix_round = 0
        raw = root.get("pending_fix")
        pending = dict(raw) if isinstance(raw, dict) else {}
        pending[line.id] = {
            "kind": "g2" if kind == "g2" else "review",
            "author": author,
            "attempt": attempt,
            "source_ref": source_ref,
            "policy": "fix",
            "fix_round": fix_round,
            "epoch": drive_epoch(dispatch),
        }
        if kind == "g2" and g2_note:
            pending[line.id]["g2_note"] = g2_note
        root["pending_fix"] = pending

    def _fix_start_confirmed(self, line: Line, intent: dict[str, object]) -> bool:
        """True when the reserved fix run is already RUNNING in storage."""
        reserved = intent.get("reserved_run_id")
        if not isinstance(reserved, str) or not reserved:
            return False
        return line.status is LineStatus.RUNNING and bool(line.current_run)

    def _clear_confirmed_fixes(self, dispatch: Dispatch) -> bool:
        """Drop intents whose reserved run LaunchDone already confirmed.

        An ASSIGNED line that only has ``reserved_run_id`` is not confirmed.
        That row is resumed through ``_launch_fix`` instead.
        """
        root = dispatch.gates if isinstance(dispatch.gates, dict) else None
        if not isinstance(root, dict) or not isinstance(root.get("pending_fix"), dict):
            return False
        changed = False
        for line in list(dispatch.lines):
            intent = self._pending_fix(dispatch, line.id)
            if intent is None or not self._fix_start_confirmed(line, intent):
                continue
            self._clear_pending_fix(dispatch, line.id)
            changed = True
        return changed

    def _stamp_reserved_fix(self, dispatch: Dispatch, line: Line, run_id: str) -> None:
        """Record the reserved run on the existing fix intent. No task text."""
        root = dispatch.gates if isinstance(dispatch.gates, dict) else None
        if not isinstance(root, dict):
            return
        raw = root.get("pending_fix")
        if not isinstance(raw, dict):
            return
        item = raw.get(line.id)
        if not isinstance(item, dict) or not run_id:
            return
        pending = dict(raw)
        updated = dict(item)
        updated["reserved_run_id"] = run_id
        pending[line.id] = updated
        root["pending_fix"] = pending

    def _clear_pending_fix(self, dispatch: Dispatch, line_id: str) -> None:
        """Drop one fix intent. An empty map is removed so idle rows stay unchanged."""
        root = dispatch.gates if isinstance(dispatch.gates, dict) else None
        if not isinstance(root, dict):
            return
        raw = root.get("pending_fix")
        if not isinstance(raw, dict) or line_id not in raw:
            return
        pending = dict(raw)
        pending.pop(line_id, None)
        if pending:
            root["pending_fix"] = pending
        else:
            root.pop("pending_fix", None)

    def _watching(self, line: Line) -> bool:
        """A line the monitor loop still has to poll.

        ``status`` stays RUNNING until REVIEW decides, so the loop needs its own
        record of which runs have already reported completion. A RUNNING line
        with no run id is still watched, exactly as main did: only the settled
        marker (in memory here, restored by ``_adopt_settled_runs``) exempts a
        resumed review. Skipping every run-less RUNNING line changed off-mode
        monitoring, so the exception is the explicit completion bit instead.
        """
        if line.status is not LineStatus.RUNNING:
            return False
        return not self._runs.get(line.id, _Run()).complete

    def _record_outcome(self, dispatch: Dispatch, line: Line, *, reviewer_verdict: str) -> None:
        run = self._runs.get(line.id) or _Run()
        dispatch.outcomes[line.id] = Outcome(
            line_id=line.id,
            model=line.assigned_model or "",
            duration_secs=max(0, int(self._clock() - run.started_at)) if run.started_at else 0,
            tokens=run.tokens,
            passed=line.status is LineStatus.PASSED,
            failure_reason=line.failure_reason,
            reviewer_verdict=reviewer_verdict,
            # R6 L4: a pinned registry version is recorded so success is
            # counted per version; "latest" writes nothing (row shape as main).
            # The category is derived from the line when outcomes are read.
            model_version=pinned_version(ROLE_WORKER, line.assigned_model or ""),
        )

    async def _fail(self, dispatch: Dispatch, message: str) -> None:
        if self._abandoned:
            return
        dispatch.state = DispatchState.ERROR
        dispatch.message = message
        logger.error("Dispatch %s failed: %s", dispatch.id, message)
        await self._save(dispatch)

    async def _discard_stale(self, dispatch: Dispatch) -> None:
        """Drop a result whose line identity moved during the await."""
        runner = self._c.gates
        if runner is not None:
            runner.note_stale_result(dispatch)
        else:
            record_stale_result(dispatch)
        await self._save(dispatch)

    async def _fence(self, dispatch: Dispatch, *, claim: bool = False) -> bool:
        """Stop this drive when a newer epoch owns the row.

        A claim is the placeholder written before an external merge. The merge
        runs only after that mutation has seen this drive's epoch. A mismatch
        records ``stale_drive`` and leaves every other field alone. The next
        wakeup starts a drive from the persisted row.
        """
        if not self._on_actor():
            return await self._submit(self._fence, dispatch, claim=claim)
        if self._abandoned:
            return True
        if claim:
            current = await self._claim_epoch_locked(dispatch)
        else:
            current = await self._epoch_matches(dispatch)
        if current:
            return False
        assert self._own_abandoned is not None
        self._own_abandoned(True)
        await self._record_stale_drive(dispatch)
        return True

    async def _epoch_matches(self, dispatch: Dispatch) -> bool:
        fresh = await self._c.store.get(dispatch.id)
        if fresh is None:
            return False
        return drive_epoch(fresh) == self._epoch0

    async def _claim_epoch(self, dispatch: Dispatch) -> bool:
        if self._on_actor():
            return await self._claim_epoch_locked(dispatch)
        return await self._submit(self._claim_epoch_locked, dispatch)

    async def _claim_epoch_locked(self, dispatch: Dispatch) -> bool:
        holder = {"ok": False}

        def mutation(row: Dispatch) -> Dispatch:
            gates = row.gates if isinstance(row.gates, dict) else {}
            row.gates = gates
            gates["drive_claim"] = self._epoch0
            holder["ok"] = True
            return row

        try:
            saved = await drive_mutation(self._c.store, dispatch.id, self._epoch0, mutation)
        except StaleDrive:
            return False
        if saved is not None:
            self._adopt_saved(dispatch, saved)
        return holder["ok"]

    async def _record_stale_drive(self, dispatch: Dispatch) -> None:
        """The mismatch write records the audit. A matching epoch does not."""
        try:
            await drive_mutation(
                self._c.store,
                dispatch.id,
                self._epoch0,
                lambda row: row,
            )
        except StaleDrive:
            return

    async def _commit_line(self, dispatch: Dispatch, line: Line, *, bump: bool) -> Dispatch | None:
        """Write this line, and the epoch bump, on the row the store re-reads."""
        if self._on_actor():
            return await self._commit_line_locked(dispatch, line, bump=bump)
        return await self._submit(self._commit_line_locked, dispatch, line, bump=bump)

    async def _absorb_midpass(self, dispatch: Dispatch) -> int:
        """Apply queued human requests inside a monitor or review pass (N77).

        A stopped drive absorbs nothing, as the loop top would not either.
        The same refusal checks run as at the loop top, so a request for a
        line that is still running or under review is refused, never applied
        to it. A line made ASSIGNED here is launched by the loop's ready-line
        pick; a resumed review is reviewed by this pass or the next one.
        Returns how many requests were applied.
        """
        if self._stopped(dispatch):
            return 0
        absorbed = await self._absorb_human_reassigns(dispatch)
        if absorbed:
            await self._safe_operator_sweep(dispatch)
            self._midpass_absorbed += absorbed
            # A resumed review's run already settled: record it, so the
            # monitor does not wait on (or time out) a run that is over.
            await self._adopt_settled_runs(dispatch)
        return absorbed

    def _take_midpass_absorbed(self) -> int:
        absorbed, self._midpass_absorbed = self._midpass_absorbed, 0
        return absorbed

    async def _absorb_app_lines(self, dispatch: Dispatch) -> int:
        """N80 L4: act on the app claim cells; how many lines changed hands.

        A dispatch with no marked line and no claim cell returns at once
        with no store read (off / shadow / docs drive as before). The stored
        row is adopted first, so claims and reports the service wrote while
        this drive ran are seen. Each step is then saved on its own, and only
        when the stored cell is still the one the step was decided from: a
        claim, heartbeat or report the service wrote in between wins, and the
        step is undone and decided again on the next drive step.
        """
        if not app_lines.engaged(dispatch) or self._c.gates is None:
            return 0
        fresh = await self._c.store.get(dispatch.id)
        if fresh is not None and app_lines.cells(fresh) != app_lines.cells(dispatch):
            await self._save_locked(dispatch)
        moved = 0
        for step in app_lines.steps(dispatch, now=self._wall()):
            line = dispatch.line(step.line_id)
            if line is None or self._abandoned:
                continue
            seen = copy.deepcopy(app_lines.cell(dispatch, line.id))
            found: dict[str, object] = {}
            if step.kind == "prepare":
                found = await self._prepare_app_worktree(line)
            elif step.kind == "adopt":
                found = await self._app_branch_facts(line, seen or {})
            # A save while this step waited may have replaced the cell.
            if app_lines.cell(dispatch, line.id) != seen:
                continue
            gates_before = copy.deepcopy(dispatch.gates)
            line_before = copy.deepcopy(line)
            run_before = self._runs.get(line.id)
            changed = self._apply_app_step(dispatch, line, step.kind, found)
            if await self._save_app_step(dispatch, line.id, seen):
                moved += changed
                continue
            # The service changed the cell first: undo this step only.
            dispatch.gates = gates_before
            for name in dataclass_fields(Line):
                setattr(line, name.name, getattr(line_before, name.name))
            put, pop = self._own_put_run, self._own_pop_run
            assert put is not None and pop is not None
            if run_before is None:
                pop(line.id)
            else:
                put(line.id, run_before)
            await self._save_locked(dispatch)
        return moved

    async def _save_app_step(self, dispatch: Dispatch, line_id: str, seen: object) -> bool:
        """Save one app step only if the stored cell is still ``seen``."""
        if self._abandoned:
            return False

        def mutation(stored: Dispatch) -> Dispatch:
            if app_lines.cell(stored, line_id) != seen:
                raise _AppCellMoved(line_id)
            return dispatch

        try:
            saved = await drive_mutation(self._c.store, dispatch.id, self._epoch0, mutation)
        except _AppCellMoved:
            return False
        except StaleDrive:
            assert self._own_abandoned is not None
            self._own_abandoned(True)
            raise
        if saved is not None:
            self._note_epoch(dispatch, saved)
        return True

    def _apply_app_step(
        self, dispatch: Dispatch, line: Line, kind: str, found: dict[str, object]
    ) -> int:
        """One step's effect on the local row (no waiting); lines moved."""
        now = self._wall()
        item = app_lines.cell(dispatch, line.id) or {}
        if kind == "open":
            app_lines.mark(dispatch, line.id, app_lines.OPEN, now=now)
        elif kind == "lapse":
            app_lines.mark(dispatch, line.id, app_lines.LAPSED, now=now)
        elif kind == "prepare":
            item.update(found)
        elif kind == "adopt":
            refused = app_lines.adopt_refusal(line, item, found)
            if refused:
                self._end_app_claim(dispatch, line, item, "release", now, why=refused)
                return 1
            return self._adopt_app_report(dispatch, line, item, now)
        elif kind in ("expire", "release"):
            self._end_app_claim(dispatch, line, item, kind, now)
            return 1
        return 0

    async def _prepare_app_worktree(self, line: Line) -> dict[str, object]:
        """The claimed line's own worktree (with the push guard), for its cell (§5.3)."""
        prepare = getattr(self._c.launcher, "prepare_app_worktree", None)
        if not callable(prepare):
            return {"worktree_error": "launcher_cannot_prepare"}
        try:
            prepared = await self._leave(prepare(line))
        except Exception as exc:  # noqa: BLE001 - the lease expires as usual
            return {"worktree_error": type(exc).__name__}
        if isinstance(prepared, Mapping):
            return {
                "worktree": str(prepared.get("path") or ""),
                "push_guard": prepared.get("push_guard") is True,
            }
        return {"worktree": str(prepared), "push_guard": False}

    async def _app_branch_facts(self, line: Line, item: Mapping[str, Any]) -> dict[str, object]:
        """The branch as it is now (tip, paths since base, dirty), read by Conductor."""
        read = getattr(self._c.launcher, "app_branch_facts", None)
        if not callable(read):
            return {}
        base = str(item.get("base_commit") or line.base_commit or "")
        try:
            facts = await self._leave(read(line, base))
        except Exception:  # noqa: BLE001 - unknown facts refuse the report
            return {}
        return dict(facts) if isinstance(facts, Mapping) else {}

    def _adopt_app_report(
        self, dispatch: Dispatch, line: Line, item: dict[str, Any], now: float
    ) -> int:
        """A ``done`` report is the line's completion claim: it enters G2 now.

        The app becomes the developer of record (Stage-Gate assign or
        checkpoint swap), so ``_barred_reviewers`` keeps it off its own
        review (H4). A refused transition gives the line back instead.
        """
        report = item.get("report") if isinstance(item.get("report"), dict) else {}
        author = str(item.get("author") or app_lines.app_author(str(item.get("app_id"))))
        head = str(report.get("head") or "")
        gates = self._c.gates
        assert gates is not None
        stage = _load_state(gates.line_snapshot(dispatch, line)).stage
        transition: Transition | None = None
        if stage is Stage.DAG_READY:
            transition = self._advance(dispatch, line, "assign", provider=author)
        elif stage is Stage.ASSIGNED:
            transition = self._advance(
                dispatch,
                line,
                "reassign",
                provider=author,
                handoff=app_lines.claim_handoff(line, item),
            )
        if transition is None or not transition.accepted:
            self._end_app_claim(dispatch, line, item, "release", now, why="stage_gate_refused")
            return 1
        line.assigned_model = author
        line.status = LineStatus.RUNNING
        line.current_run = f"{dispatch.id}:{line.id}:{line.attempts}:{author}"
        line.failure_reason = None
        put = self._own_put_run
        assert put is not None
        put(line.id, _Run(started_at=self._clock(), complete=True))
        self._note_run_head(
            dispatch, line, author, line.attempts, item.get("base_commit") or line.base_commit,
            head, submitted=True,
        )
        gates.mark_run_settled(dispatch, line)
        app_lines.mark(dispatch, line.id, app_lines.ADOPTED, now=now, adopted_head=head)
        return 1

    def _end_app_claim(
        self,
        dispatch: Dispatch,
        line: Line,
        item: dict[str, Any],
        kind: str,
        now: float,
        *,
        why: str = "",
    ) -> None:
        """The app's lease ended without an adopted report: park it for N76 R-EXIT."""
        reason = app_lines.REASON_EXPIRED if kind == "expire" else app_lines.REASON_RELEASED
        author = str(item.get("author") or app_lines.app_author(str(item.get("app_id"))))
        pack = app_lines.line_pack(dispatch, line, item)
        extra = {"end_reason": why} if why else {}
        app_lines.mark(
            dispatch, line.id, app_lines.EXPIRED if kind == "expire" else app_lines.RELEASED,
            now=now, handoff_pack=handoff_pack.pack_bytes(pack), **extra,
        )
        line.assigned_model = author
        line.blockers = [app_lines.exit_note(item, pack, reason)]
        gates = self._c.gates
        assert gates is not None
        record_worker_exited(
            dispatch,
            line,
            gates.line_snapshot(dispatch, line),
            run_id=f"{dispatch.id}:{line.id}:{line.attempts}:{author}",
            failure_class=reason,
            exit_code=None,
            stamp=gates.stamp(),
        )
        self._block_line(dispatch, line, reason)

    async def _absorb_human_reassigns(self, dispatch: Dispatch) -> int:
        """Apply human reassigns the service queued while this drive ran.

        Returns how many lines were reassigned. The store is read first, so
        an empty queue costs one read and no write. A request is applied in
        the same locked write that removes it from the queue, so it is
        applied once. The epoch is not bumped: this drive owns the row, and
        the new attempt and author already fence any old result for the line.
        """
        if not self._on_actor():
            return await self._submit(self._absorb_human_reassigns, dispatch)
        if self._abandoned:
            return 0
        fresh = await self._c.store.get(dispatch.id)
        if fresh is None or not pending_human_reassigns(fresh):
            return 0
        # Flush this drive's own changes and adopt the stored queue, so the
        # line writes below start from the row that is in the store now.
        await self._save_locked(dispatch)
        applied = 0
        for line_id, request in pending_human_reassigns(dispatch).items():
            if self._abandoned:
                break
            if await self._apply_queued_reassign(dispatch, line_id, request):
                applied += 1
        return applied

    async def _apply_queued_reassign(
        self, dispatch: Dispatch, line_id: str, request: dict[str, object]
    ) -> bool:
        line = dispatch.line(line_id)
        previous = previous_assignment(line)
        refused = human_reassign_request_refusal(dispatch, line, request)
        kind = request_kind(request)
        if refused is None and line is not None:
            refused = await apply_human_request(
                dispatch, line, request, self._c.gates
            )
        if refused is None and line is not None:
            logger.info(
                "Drive %s applied a human %s of %s",
                dispatch.id,
                kind,
                line_id,
            )
        else:
            logger.warning(
                "Drive %s dropped a human %s of %s: %s",
                dispatch.id,
                kind,
                line_id,
                refused,
            )
        expected = copy.deepcopy(request)
        assignment_audit = copy.deepcopy(line.assignment_audit) if line is not None else None
        applied = refused is None and line is not None
        briefs = dispatch.gates.get(REASSIGN_BRIEFS) if isinstance(dispatch.gates, dict) else None
        brief = copy.deepcopy(briefs.get(line_id)) if isinstance(briefs, dict) else None
        # An adjudication may record an outcome and a follow-up note.
        outcome = copy.deepcopy(dispatch.outcomes.get(line_id)) if applied else None
        follow_ups = (
            copy.deepcopy(dispatch.gates.get(FOLLOW_UPS))
            if applied and isinstance(dispatch.gates, dict)
            else None
        )

        def finish(row: Dispatch) -> None:
            queued = pending_human_reassigns(row).get(line_id)
            if queued == expected:
                drop_human_reassign(row, line_id)
            if applied:
                current = row.line(line_id)
                if current is not None:
                    current.assignment_audit = copy.deepcopy(assignment_audit)
                if isinstance(brief, dict):
                    if not isinstance(row.gates, dict):
                        row.gates = {}
                    stored = row.gates.setdefault(REASSIGN_BRIEFS, {})
                    if isinstance(stored, dict):
                        stored[line_id] = copy.deepcopy(brief)
                if outcome is not None:
                    row.outcomes[line_id] = copy.deepcopy(outcome)
                if isinstance(follow_ups, list):
                    if not isinstance(row.gates, dict):
                        row.gates = {}
                    row.gates[FOLLOW_UPS] = copy.deepcopy(follow_ups)
            note_human_reassign_audit(
                row,
                line_id,
                expected,
                previous=previous,
                refused=refused,
                applied_by="drive",
            )

        if line is None:
            finish(dispatch)
            await self._save_locked(dispatch)
            return False
        saved = await self._commit_line_locked(dispatch, line, bump=False, extra=finish)
        return applied and saved is not None

    async def _commit_line_locked(
        self,
        dispatch: Dispatch,
        line: Line,
        *,
        bump: bool,
        extra: Callable[[Dispatch], None] | None = None,
    ) -> Dispatch | None:
        gates = self._c.gates
        snap = (
            copy.deepcopy(gates.line_snapshot(dispatch, line))
            if gates is not None
            else None
        )
        recovery = self._recovery_data(dispatch)
        recovery_snapshot = copy.deepcopy(recovery) if recovery else None
        line_id = line.id
        fields = {
            name: copy.deepcopy(getattr(line, name))
            for name in (
                "assigned_model",
                "branch",
                "base_commit",
                "status",
                "attempts",
                "current_run",
                "failure_reason",
                "blockers",
                "atomic_with",
            )
        }

        def mutation(row: Dispatch) -> Dispatch:
            current = row.line(line_id)
            if current is None:
                if extra is not None:
                    extra(row)
                return row
            for name, value in fields.items():
                setattr(current, name, copy.deepcopy(value))
            if gates is not None and snap is not None:
                target = gates.line_snapshot(row, current)
                target.clear()
                target.update(copy.deepcopy(snap))
            if recovery_snapshot is not None:
                if not isinstance(row.gates, dict):
                    row.gates = {}
                row.gates["worker_recovery"] = copy.deepcopy(recovery_snapshot)
            if extra is not None:
                extra(row)
            if bump:
                bump_epoch(row)
            return row

        try:
            saved = await drive_mutation(self._c.store, dispatch.id, self._epoch0, mutation)
        except StaleDrive:
            assert self._own_abandoned is not None
            self._own_abandoned(True)
            return None
        if saved is None:
            return None
        self._note_epoch(dispatch, saved)
        return saved

    def _note_epoch(self, dispatch: Dispatch, saved: Dispatch) -> None:
        """Pair the local row with the row the store just kept."""
        self._adopt_saved(dispatch, saved)

    def _adopt_saved(self, local: Dispatch, saved: Dispatch) -> None:
        """Baseline and local both become the saved row, keeping line identity."""
        snapshot = copy.deepcopy(saved)
        assert self._own_baseline is not None and self._own_epoch is not None
        self._own_baseline(snapshot)
        self._own_epoch(drive_epoch(snapshot))
        _copy_row(local, snapshot)

    async def _save(self, dispatch: Dispatch) -> None:
        if self._on_actor():
            await self._save_locked(dispatch)
            return
        await self._submit(self._save_locked, dispatch)

    async def _save_locked(self, dispatch: Dispatch) -> None:
        if self._abandoned:
            return

        def mutation(fresh: Dispatch) -> Dispatch:
            return dispatch

        try:
            saved = await drive_mutation(self._c.store, dispatch.id, self._epoch0, mutation)
        except StaleDrive:
            assert self._own_abandoned is not None
            self._own_abandoned(True)
            raise
        if saved is None:
            # No stored row, so there is no epoch to compare. Insert the object
            # this drive was given. A later write goes through ``drive_mutation``.
            existing = await self._c.store.get(dispatch.id)
            if existing is None:
                await self._c.store.create(dispatch)
            return
        self._note_epoch(dispatch, saved)

    @property
    def outputs(self) -> dict[str, str]:
        return dict(self._outputs)


def _copy_row(local: Dispatch, saved: Dispatch) -> None:
    """Copy ``saved`` onto ``local`` without replacing line objects the caller holds."""
    from src.server.services.dispatch import _LINE_FIELDS, _PHASE_FIELDS

    for name in _PHASE_FIELDS:
        setattr(local, name, copy.deepcopy(getattr(saved, name)))
    saved_gates = copy.deepcopy(saved.gates) if isinstance(saved.gates, dict) else {}
    if isinstance(local.gates, dict):
        local.gates.clear()
        local.gates.update(saved_gates)
    else:
        local.gates = saved_gates
    saved_ids = {line.id for line in saved.lines}
    local_by = {line.id: line for line in local.lines}
    for line in saved.lines:
        current = local_by.get(line.id)
        if current is None:
            local.lines.append(copy.deepcopy(line))
            continue
        for name in _LINE_FIELDS:
            setattr(current, name, copy.deepcopy(getattr(line, name)))
    local.lines[:] = [line for line in local.lines if line.id in saved_ids]
    local.outcomes.clear()
    for key, value in saved.outcomes.items():
        local.outcomes[key] = copy.deepcopy(value)


# What _review_after_unparseable returns when it did not produce a verdict.
_STALE_REVIEW = object()
def _drop_request_file(path: object) -> None:
    """Delete a handoff request file once its decision is saved (or void)."""
    if isinstance(path, (str, Path)) and str(path):
        delete_handoff_request(Path(path))


# N29 handoff outcomes: nothing rotated (a normal success), or the decision
# is saved and the line is ready for its relaunch.
_NO_HANDOFF = object()
_HANDED_OFF = object()
#: Recovery-entry key: the session key a rotation-exhausted park parked.
_ROTATION_PARKED_KEY = "rotation_parked_key"
_PARKED_REVIEW = object()


def _result_token(line: Line) -> tuple[str, str | None, int, str]:
    """``(line_id, run_id, attempt, author)`` captured around an awaited result."""
    return (line.id, line.current_run, line.attempts, line.assigned_model or "")


def _event_current(
    dispatch: Dispatch,
    line: Line,
    event: PollResult | LaunchDone | ReviewResult | ReassignNeeded,
) -> bool:
    """True when the event still names the line's current run."""
    return (
        dispatch.id == event.dispatch_id
        and line.id == event.line_id
        and line.current_run == event.run_id
        and line.attempts == event.attempt
    )


def _reviewer_name(value: object) -> str | None:
    """A stored reviewer, or None when the shell has not chosen one."""
    if not isinstance(value, str) or not value or value == "None":
        return None
    return value


def _load_state(snap: dict[str, object]) -> StageGateState:
    raw = dict(snap.get("machine") or {}) if isinstance(snap.get("machine"), dict) else {}
    for key in _MACHINE_STRINGS:
        # The shell stores these as JSON null. from_dict does str(None) == "None",
        # which is a truthy reviewer name and skips real selection.
        if raw.get(key) is None or raw.get(key) == "None":
            raw[key] = ""
    for key in ("primary_passed", "secondary_passed", "secondary_skipped", "risk_bearing"):
        if raw.get(key) is None:
            raw[key] = False
    options = raw.get("replan_options")
    raw["replan_options"] = list(options) if isinstance(options, list) else []
    contributors = raw.get("contributors")
    raw["contributors"] = list(contributors) if isinstance(contributors, list) else []
    if not isinstance(raw.get("handoff"), dict):
        raw["handoff"] = None
    raw["task_id"] = str(raw.get("task_id") or snap.get("line_id") or "")
    raw["stage"] = str(raw.get("stage") or Stage.DAG_READY.value)
    return StageGateState.from_dict(raw)


def _store_state(snap: dict[str, object], state: StageGateState) -> None:
    data = state.to_dict()
    data["handoff"] = None
    snap["machine"] = data
    if state.stage is Stage.FIXING and state.return_stage:
        snap["phase"] = state.return_stage
    else:
        snap["phase"] = state.stage.value
    snap["primary_reviewer"] = _reviewer_name(
        state.primary_reviewer or state.blocking_reviewer or snap.get("primary_reviewer")
    )
    snap["secondary_reviewer"] = _reviewer_name(
        state.secondary_reviewer or snap.get("secondary_reviewer")
    )
    snap["blocking_rounds"] = state.blocking_rounds
    snap["handoff_ref"] = state.handoff_ref or None
    snap["risk_bearing"] = state.risk_bearing


def _remember_event(snap: dict[str, object], transition: Transition) -> None:
    if transition.event is None:
        return
    events = snap.setdefault("events", [])
    if not isinstance(events, list):
        events = []
        snap["events"] = events
    machine = snap.get("machine") if isinstance(snap.get("machine"), dict) else {}
    stage = str(machine.get("stage") or "")
    events.append(
        event_tags.tag(
            transition.event.to_dict(),
            role="reviewer" if stage.endswith("review") else "worker",
            session=str(machine.get("run_id") or ""),
            snapshot=snap,
        )
    )
    if len(events) > 50:
        del events[:-50]


def _remember_review(
    snap: dict[str, object],
    *,
    round_n: int,
    gate: str,
    result: str,
    reviewer: str,
    blocker_count: int,
    reason: str,
    at: str,
    blockers: tuple[str, ...] | list[str] = (),
) -> None:
    history = snap.setdefault("history", [])
    if not isinstance(history, list):
        history = []
        snap["history"] = history
    entry: dict[str, object] = {
        "round": round_n,
        "gate": gate,
        "result": result,
        "reviewer": reviewer,
        "blocker_count": blocker_count,
        "reason": reason,
        "at": at,
    }
    # Only a BLOCK carries blockers. Keeping them beside the count makes the
    # escalation summary possible without a second, lossy ledger, and a passing
    # entry stays exactly the shape older snapshots used. Stage-Gate names a
    # failing review ``block``; ``fail`` is the older spelling some readers and
    # fixtures still use, and both must keep the brief instead of clearing it.
    blocked = str(result).strip().lower() in {"block", "blocked", "fail"}
    if not blocked:
        # A pass resets Stage-Gate's round counters; the previous sequence's
        # blockers must not make a later first BLOCK look like a second failure.
        # Keep an empty key as the boundary, but do not add one to snapshots
        # that never had a blocker brief (the old shape stays byte-identical).
        if BLOCKER_BRIEF in snap:
            snap[BLOCKER_BRIEF] = []
        # A passing review settles the round's ruling. A later run (a reassign
        # or a G2 retry) must not be checked against a contract already met.
        snap.pop(FIX_RULING, None)
        snap.pop(RULING_CHECK, None)
        snap.pop(RULING_REISSUE_HEAD, None)
        snap.pop(RULING_UNCONFIRMED, None)
    if blocked:
        items = tuple(
            str(item)[:_BRIEF_MAX_CHARS]
            for item in blockers or ()
            if str(item).strip()
        )[:_BRIEF_MAX_BLOCKERS]
        if items:
            entry["blockers"] = list(items)
    history.append(entry)


def _review_cell(
    snap: dict[str, object],
    name: str,
    *,
    result: str,
    reason: str,
    reviewer: str,
    at: str,
) -> None:
    gates = snap.get("gates")
    if not isinstance(gates, dict):
        return
    gates[name] = {
        "result": result,
        "reason": reason,
        "reviewer": reviewer,
        "at": at,
    }


def _g2_cell(snap: dict[str, object]) -> dict[str, object]:
    gates = snap.get("gates")
    if not isinstance(gates, dict):
        gates = {}
        snap["gates"] = gates
    current = gates.get("G2")
    if not isinstance(current, dict):
        current = {}
        gates["G2"] = current
    return current


def _handoff_from_view(view: dict[str, object], *, limitations: str) -> Handoff:
    """Rebuild the handoff ``_launch_fix`` already recorded. No second git read."""
    files = view.get("files") or ()
    if isinstance(files, (list, tuple)):
        stored = tuple(str(item) for item in files)
    else:
        stored = ()
    return Handoff(
        branch=str(view.get("branch") or ""),
        commit=str(view.get("commit") or ""),
        files=stored,
        test_command=str(view.get("test_command") or ""),
        results=str(view.get("results") or ""),
        known_limitations=limitations or "none",
    )


def _fix_task(
    task: str,
    handoff: Handoff,
    blockers: tuple[str, ...] | list[str],
    *,
    keep_blockers: bool = False,
    ruling: Ruling | None = None,
) -> str:
    """The fix prompt, capped at MAX_LINE_TASK_BYTES from its end.

    ``keep_blockers`` cuts the task text first instead (N79): a reassign
    brief that says the tip was rejected must reach the author even when the
    task alone is near the cap. A structured ``ruling`` (R5 N6) rides with the
    blockers, so the same task-first cut keeps it.
    """
    listed = "\n".join(f"- {item}" for item in blockers) or "- none"
    rest = (
        "\n\nHandoff:\n"
        f"branch: {handoff.branch}\n"
        f"commit: {handoff.commit}\n"
        f"files: {', '.join(handoff.files)}\n"
        f"tests: {handoff.test_command}\n"
        f"results: {handoff.results}\n"
        f"Blockers:\n{listed}\n"
    )
    if ruling is not None:
        rest += "\n" + render_ruling(ruling) + "\n"
    body = task + rest
    raw = body.encode("utf-8")
    if len(raw) <= MAX_LINE_TASK_BYTES:
        return body
    note = "\ntruncated"
    if keep_blockers:
        cut = " [task truncated]"
        room = MAX_LINE_TASK_BYTES - len(rest.encode("utf-8")) - len(cut.encode("utf-8"))
        # Keep at least a quarter of the cap for the task itself.
        room = max(room, MAX_LINE_TASK_BYTES // 4)
        task_raw = task.encode("utf-8")
        if len(task_raw) > room:
            body = task_raw[:room].decode("utf-8", "ignore") + cut + rest
            raw = body.encode("utf-8")
            if len(raw) <= MAX_LINE_TASK_BYTES:
                return body
    room = max(MAX_LINE_TASK_BYTES - len(note.encode("utf-8")), 0)
    return raw[:room].decode("utf-8", "ignore") + note


def _with_ruling(task: str, ruling: Ruling) -> str:
    """``task`` followed by the rendered ruling, the task cut first to fit."""
    rest = "\n\n" + render_ruling(ruling) + "\n"
    body = task + rest
    if len(body.encode("utf-8")) <= MAX_LINE_TASK_BYTES:
        return body
    cut = " [task truncated]"
    room = max(
        MAX_LINE_TASK_BYTES - len(rest.encode("utf-8")) - len(cut.encode("utf-8")), 0
    )
    return task.encode("utf-8")[:room].decode("utf-8", "ignore") + cut + rest


def _stage_verdict(verdict: Verdict, *, risk: bool, secondary: bool) -> str:
    if not verdict.passed:
        return "block"
    if secondary:
        return "merge"
    return "approve" if risk else "merge"


def conflict_tag(blocker: str) -> str:
    """``scope`` / ``security`` when the blocker carries the conflict tag, else ""."""
    return text_normalize.conflict_tag(blocker)


def _conflict_of(verdict: Verdict) -> str:
    return text_normalize.conflict_of(str(item) for item in verdict.blockers)


def _completed_running(
    dispatch: Dispatch, runs: dict[str, _Run], *, skip: set[str] | None = None
) -> bool:
    return any(
        line.status is LineStatus.RUNNING
        and runs.get(line.id, _Run()).complete
        and line.id not in (skip or set())
        for line in dispatch.lines
    )


def _scope_root(entry: str) -> str:
    """The literal path part of a write-scope entry, before any glob character."""
    text = entry.strip().lstrip("./")
    for index, char in enumerate(text):
        if char in "*?[{":
            text = text[:index]
            break
    return text.rstrip("/")


def _scopes_overlap(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    """True when two lines may write the same path. An empty scope may write anywhere."""
    if not first or not second:
        return True
    for left in first:
        a = _scope_root(left)
        for right in second:
            b = _scope_root(right)
            if not a or not b or a == b:
                return True
            if a.startswith(b + "/") or b.startswith(a + "/"):
                return True
    return False


def _classify_review_failure(reviewer: str, exc: BaseException) -> _ReviewUnavailable:
    """The native-CLI classifier's reading of a review failure (N63).

    Only the class, reason code and retry hint leave this function; the
    diagnostic text stays here.
    """
    # Only the trailing error lines: stderr can echo code or a prompt, and a
    # phrase in it must not read as the CLI's own quota message.
    # N63-1: only known adapter prefixes are stripped.
    text = review_failure_text(str(exc))
    try:
        from src.llms.native_cli.failure import classify_failure

        classified = classify_failure(reviewer, text)
    except Exception:  # classification is advisory
        return _ReviewUnavailable()
    failure_class = getattr(classified, "failure_class", None)
    return _ReviewUnavailable(
        getattr(failure_class, "value", "") or "",
        str(getattr(classified, "reason_code", "") or ""),
        getattr(classified, "retry_after_seconds", None),
    )


def _later_iso(first: object, second: object) -> object:
    """The later of two ISO timestamps; an unparseable or missing one loses."""

    def parse(value: object) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    a, b = parse(first), parse(second)
    if a is None:
        return second if b is not None else first
    if b is None:
        return first
    return first if a >= b else second


def _quota_stage_is_secondary(state: StageGateState | None) -> bool:
    """The review a quota park will resume: secondary, or primary otherwise."""
    if state is None:
        return False
    if state.stage is Stage.SECONDARY_REVIEW:
        return True
    return (
        state.stage is Stage.BLOCKED
        and state.blocked_from == Stage.SECONDARY_REVIEW.value
    )


def _earliest_reset(blockers: dict[str, object]) -> datetime | None:
    """The soonest ``resets_at`` among live reviewer blockers."""
    moments: list[datetime] = []
    for entry in blockers.values():
        if not isinstance(entry, dict):
            continue
        parsed = liveness_record.parse_time(entry.get("resets_at"))
        if parsed is not None:
            moments.append(parsed)
    return min(moments) if moments else None


def _primary_reviewers(selector: object) -> set[str]:
    policy = getattr(selector, "_policy", None)
    names = getattr(policy, "primary_reviewer", ()) if policy is not None else ()
    return {str(name).lower() for name in names or ()}


def _review_roster(selector: object) -> set[str] | None:
    """The live selector's review roster, when it can say."""
    policy = getattr(selector, "_policy", None)
    if policy is None:
        return None
    try:
        from .pool import eligible_models

        return {name.lower() for name in eligible_models("review", policy)}
    except Exception:
        return None


def _clear_review_retry(snap: dict[str, object]) -> None:
    snap.pop(REVIEW_RETRY, None)
    snap.pop("review_attempts", None)


def _review_retry(snap: dict[str, object]) -> dict[str, object] | None:
    record = snap.get(REVIEW_RETRY)
    return record if isinstance(record, dict) else None


def _blocked_message(blocked: list[Line]) -> str:
    parts = [f"Line {line.id} needs a decision: " + "; ".join(line.blockers) for line in blocked]
    return " ".join(parts)


def _git_is_ancestor(repo: Path, ancestor: str, commit: str) -> bool | None:
    """Whether ``commit`` contains ``ancestor``: True, False, or None when git cannot say.

    Asked as ``rev-list --count commit..ancestor`` (zero commits of
    ``ancestor`` outside ``commit``), the isolated runner's allowlisted form
    of ``merge-base --is-ancestor``: two full object ids, no ref or option.
    """
    try:
        done = run_isolated_git_readonly(
            str(repo), ["rev-list", "--count", f"{commit}..{ancestor}"], timeout=5
        )
    except Exception as exc:  # noqa: BLE001 -- an unknown answer is "not contained"
        logger.warning("Ancestry check failed: %s", type(exc).__name__)
        return None
    if done.returncode != 0:
        return None
    count = done.stdout.decode("utf-8", "replace").strip()
    return count == "0" if count.isdigit() else None


def _snapshot_stage(snapshot: dict[str, Any]) -> Stage | None:
    """The Stage-Gate stage on a line snapshot, or None for an ungated line."""
    if not isinstance(snapshot, dict):
        return None
    machine = snapshot.get("machine")
    raw: object = None
    if isinstance(machine, dict):
        raw = machine.get("stage")
    if raw is None:
        raw = snapshot.get("phase")
    if raw is None:
        return None
    if isinstance(raw, Stage):
        return raw
    try:
        return Stage(str(raw).strip().lower())
    except ValueError:
        return None


def _liveness_plan_index(
    record: liveness_record.LivenessRecord | None, group: str | None = None
) -> int:
    """Which recovery-table row ``record`` should run next.

    ``spent`` is the index when every budget-consuming step still belongs to
    this group, including after history is trimmed. A step from another group
    means this group is counted from the steps that name it, so a re-park into
    a new group starts at its first row instead of ``plan[spent]``.
    """
    if record is None:
        return 0
    target = group if group is not None else record.group
    same_group = group is None or record.group == group
    foreign = any(
        step.group and step.group != target and step.result != liveness_record.SHADOW
        for step in record.history
    )
    if same_group and not foreign:
        return record.spent
    return sum(
        1
        for step in record.history
        if step.result != liveness_record.SHADOW and step.group == target
    )


def _review_row_changes_reviewer(
    record: liveness_record.LivenessRecord, policy: liveness_policy.Policy
) -> bool:
    """True when this review row is not the group's first review.

    R-REVIEW's second and third rows change reviewer. R-QUOTA's review follows
    a wait, so the stored reviewer is retried.
    """
    plan = policy.plan_for(record.reason)
    index = _liveness_plan_index(record)
    if index >= len(plan) or plan[index].action != liveness_policy.REVIEW:
        return False
    earlier = sum(1 for step in plan[:index] if step.action == liveness_policy.REVIEW)
    return earlier >= 1


def _reviewer_identities(*values: object) -> list[str]:
    """Unique reviewer names, original spelling kept, comparison case-folded."""
    found: list[str] = []
    seen: set[str] = set()

    def take(value: object) -> None:
        if isinstance(value, str):
            items: list[object] = [value]
        elif isinstance(value, (list, tuple)):
            items = list(value)
        elif isinstance(value, dict):
            nested: list[object] = []
            failed = value.get("failed")
            reviewer = value.get("reviewer")
            if isinstance(failed, (list, tuple)):
                nested.extend(failed)
            elif isinstance(failed, str):
                nested.append(failed)
            if isinstance(reviewer, str):
                nested.append(reviewer)
            items = nested
        else:
            return
        for item in items:
            name = _reviewer_name(item)
            if name is None:
                continue
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            found.append(name)

    for value in values:
        take(value)
    return found


def _excluded_reviewers(snapshot: object) -> list[str]:
    if not isinstance(snapshot, dict):
        return []
    return _reviewer_identities(snapshot.get(_LIVENESS_EXCLUDED_REVIEWERS))


def _remember_excluded_reviewers(snapshot: dict[str, Any], state: StageGateState) -> None:
    """Keep the reviewer an alternate R-REVIEW row must not ask again.

    The failed list, the retry record and the stored primary or blocking
    reviewer are the identity. A secondary reviewer is included only when it
    is already in one of those lists. The list accumulates, so the third row
    does not go back to the reviewer the second row excluded.
    """
    parked = snapshot.get(REVIEW_PARKED)
    retry = snapshot.get(REVIEW_RETRY)
    names = _reviewer_identities(
        snapshot.get(_LIVENESS_EXCLUDED_REVIEWERS),
        parked.get("failed") if isinstance(parked, dict) else (),
        retry if isinstance(retry, dict) else (),
        state.primary_reviewer,
        state.blocking_reviewer,
    )
    snapshot[_LIVENESS_EXCLUDED_REVIEWERS] = names


def _worker_recovery_entry(dispatch: object, line_id: object) -> dict[str, Any] | None:
    gates = getattr(dispatch, "gates", None)
    if not isinstance(gates, dict):
        return None
    root = gates.get("worker_recovery")
    if not isinstance(root, dict):
        return None
    entry = root.get(str(line_id))
    return entry if isinstance(entry, dict) else None


def _worker_recovery_deadline(dispatch: object, line: object) -> datetime | None:
    """The Retry-After moment for this line, if worker recovery still owns it."""
    entry = _worker_recovery_entry(dispatch, getattr(line, "id", ""))
    if entry is None:
        return None
    if entry.get("quota_retry_source") == "retry_after":
        parsed = liveness_record.parse_time(entry.get("quota_retry_at"))
        if parsed is not None:
            return parsed
    if entry.get("retry_source") == "retry_after":
        return liveness_record.parse_time(entry.get("retry_at"))
    return None


def _author_quota_waiting(dispatch: object, line: object, moment: datetime) -> bool:
    """True while author-quota recovery is still waiting on Retry-After."""
    if getattr(line, "failure_reason", None) != "author_quota_exhausted":
        return False
    deadline = _worker_recovery_deadline(dispatch, line)
    return deadline is not None and deadline > moment


_LIVE_LINE_STATUSES = frozenset(
    {"running", "reviewing", "assigned", "pending", "passed", "failed"}
)


def _worker_timer_owns_view(
    view: Mapping[str, Any], line: object, entry: Mapping[str, Any] | None
) -> bool:
    """Whether the worker timer is this line's owner for ``decide_owner``.

    A live run or review is not hidden behind a leftover timer. A blocked
    line with some other failure reason keeps that reason's recovery table.
    A missing status is the owner-view repro: the timer is the only owner.
    """
    status = str(view.get("status") or "").strip().casefold()
    raw_reason = view.get("failure_reason", getattr(line, "failure_reason", None))
    reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
    parked = ""
    if isinstance(entry, Mapping):
        raw_parked = entry.get("parked_reason")
        if isinstance(raw_parked, str):
            parked = raw_parked.strip()
    # A live line keeps its own owner. A leftover Retry-After entry must not
    # become the recovery timer just because the park reason is still recorded.
    if status in _LIVE_LINE_STATUSES:
        return False
    if reason == "author_quota_exhausted" or parked == "author_quota_exhausted":
        return True
    return not reason and parked in ("", "author_quota_exhausted")


def _overlay_worker_recovery_timer(
    dispatch: object, line: object, view: dict[str, Any]
) -> None:
    """Surface a future Retry-After on the owner view without storing it."""
    entry = _worker_recovery_entry(dispatch, getattr(line, "id", ""))
    deadline = _worker_recovery_deadline(dispatch, line)
    if deadline is None or not _worker_timer_owns_view(view, line, entry):
        return
    existing = view.get("liveness")
    if isinstance(existing, dict):
        group = str(existing.get("group") or "")
        timer = liveness_record.parse_time(
            existing.get("next_at")
            or existing.get("deadline")
            or existing.get("due_at")
        )
        # A real non-HUMAN recovery row keeps its own schedule. The empty
        # HUMAN plan's next_at is "now", which would hide the worker timer.
        if group and group != liveness_policy.HUMAN and timer is not None:
            return
        merged = dict(existing)
    else:
        merged = {}
    merged["next_at"] = liveness_record.format_time(deadline)
    merged.setdefault("next_action", "worker_recovery")
    view["liveness"] = merged


def _work_was_launched(
    dispatch: Dispatch, line: Line, gates: GateRunner, handoff: Handoff
) -> bool:
    """True when a worker actually started, not merely reserved a branch.

    ``_apply_plan_ready`` writes ``line.branch`` before selection. An
    unlaunched ``no_eligible_worker`` park therefore has a branch and an
    incomplete handoff. Stage-Gate refuses the reassignment unless the
    synthetic unlaunched handoff is used. A real commit, a live run, or a
    stage past assignment means the work did launch.
    """
    commit = str(handoff.commit or "").strip()
    if commit and commit.casefold() != "unknown":
        return True
    if line.current_run:
        return True
    state = _load_state(gates.line_snapshot(dispatch, line))
    origin = (state.blocked_from or "").strip().casefold()
    if origin not in _NOT_A_LAUNCH_STAGE:
        return True
    if state.stage is not Stage.BLOCKED and state.stage.value not in _NOT_A_LAUNCH_STAGE:
        return True
    return False


def _liveness_schedule(
    step: liveness_policy.Step, moment: datetime, policy: liveness_policy.Policy
) -> tuple[datetime, str]:
    """When the first execution of ``step`` is due and under which name."""
    if step.action in (liveness_policy.REVIEW,):
        return moment + timedelta(seconds=float(step.budget or 0)), step.action
    return moment, step.action


def _liveness_schedule_after(
    plan: tuple[liveness_policy.Step, ...],
    index: int,
    moment: datetime,
    policy: liveness_policy.Policy,
) -> tuple[datetime | None, str | None]:
    """When the row after ``index`` is due; None when the plan is spent."""
    if index >= len(plan):
        return None, None
    step = plan[index]
    if step.action == liveness_policy.WAIT:
        return moment, step.action
    return _liveness_schedule(step, moment, policy)


def _liveness_human_note(
    line: Line,
    record: liveness_record.LivenessRecord,
    reason: str,
    detail: str,
) -> str:
    """The one note a person reads on the line and in the inbox."""
    detail_text = f" ({detail})" if detail else ""
    return (
        f"Line {line.id} needs a person: {reason}{detail_text}. "
        f"Parked as {line.failure_reason or 'unknown'} in {record.group}; "
        f"{record.budget_left} automatic step(s) left. "
        f"Run `conductor dispatch recover <dispatch-id> {line.id}` for the "
        "next step, or reassign it."
    )


def _liveness_followups_root(gates: GateRunner | None) -> Path:
    """Where R-SPLIT writes its follow-up drafts."""
    override = os.environ.get("CONDUCTOR_DISPATCH_FOLLOWUPS_DIR")
    if override:
        return Path(override)
    settings = getattr(gates, "_settings", None)
    runs_dir = getattr(settings, "runs_dir", None)
    if runs_dir is not None:
        return Path(runs_dir).parent / "followups"
    return Path.home() / ".conductor" / "dispatch" / "followups"


def _liveness_inbox_path(gates: GateRunner | None) -> Path:
    override = os.environ.get("CONDUCTOR_DISPATCH_INBOX_DIR")
    if override:
        return Path(override) / "needs-human.md"
    settings = getattr(gates, "_settings", None)
    runs_dir = getattr(settings, "runs_dir", None)
    if runs_dir is not None:
        return Path(runs_dir).parent / "inbox" / "needs-human.md"
    return Path.home() / ".conductor" / "inbox" / "needs-human.md"


def _split_off_breaks_atomic(
    dispatch: Dispatch, victim_ids: set[str]
) -> set[str]:
    """Atomic group members left behind by a split, or an empty set.

    Two lines are atomic when either names the other in ``atomic_with``. The
    split is legal only when every member of the group is in ``victim_ids``;
    otherwise it would leave a PASSED partner eligible for G5 on its own.
    """
    broken: set[str] = set()
    by_id = {line.id: line for line in dispatch.lines}
    for victim_id in victim_ids:
        victim = by_id.get(victim_id)
        if victim is None:
            continue
        group = set(victim.atomic_with)
        for other in dispatch.lines:
            if victim_id in other.atomic_with:
                group.add(other.id)
        group.discard(victim_id)
        if group and any(member not in victim_ids for member in group):
            broken.add(victim_id)
            broken.update(member for member in group if member not in victim_ids)
    return broken


_SPLIT_IN_FLIGHT = frozenset(
    {LineStatus.PENDING, LineStatus.ASSIGNED, LineStatus.RUNNING, LineStatus.REVIEWING}
)


def _split_waits_for(dispatch: Dispatch, line_id: str) -> int:
    """Lines a due R-SPLIT of ``line_id`` should wait for, or 0 to decide now.

    "Zero PASSED does not split" is a property of the finished round. While
    no line has PASSED but an independent line is still running, the refusal
    would latch a person permanently even after that line passes, so the
    split waits instead.
    """
    victim_ids = {victim.id for victim in _split_off_victims(dispatch, line_id)}
    remaining = [item for item in dispatch.lines if item.id not in victim_ids]
    if any(item.status is LineStatus.PASSED for item in remaining):
        return 0
    return sum(1 for item in remaining if item.status in _SPLIT_IN_FLIGHT)


def _note_recover_outcome(
    dispatch: Dispatch, line: Line, marker: dict[str, Any], result: str, reason: str
) -> None:
    """Audit a recover request the drive could not perform as asked."""
    if not isinstance(dispatch.gates, dict):
        dispatch.gates = {}
    scheduler = dispatch.gates.setdefault("scheduler", {})
    if not isinstance(scheduler, dict):
        return
    audit = scheduler.setdefault("audit", [])
    if isinstance(audit, list):
        audit.append(
            {
                "kind": "human_recover",
                "line_id": line.id,
                "mode": marker.get("mode"),
                "result": "refused" if result == "dropped" else result,
                "reason": reason,
                "applied_by": "drive",
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )


def _split_off_victims(dispatch: Dispatch, line_id: str) -> list[Line]:
    """``line_id`` plus every DAG downstream line, in topological order."""
    victims = {line_id}
    changed = True
    while changed:
        changed = False
        for line in dispatch.lines:
            if line.id in victims:
                continue
            if any(dependency in victims for dependency in line.depends_on):
                victims.add(line.id)
                changed = True
    ordered = topological_lines(dispatch.lines)
    return [line for line in ordered if line.id in victims]


def _write_split_followup(
    path: Path,
    dispatch: Dispatch,
    line: Line,
    snapshot: dict[str, Any],
    reason: str,
    moment: datetime,
    *,
    parked_reason: str | None = None,
) -> Path:
    """One follow-up draft: task, scope, acceptance, park history, escalation."""
    record = liveness_record.load(snapshot)
    scope = ", ".join(line.write_scope) or "—"
    acceptance = "\n".join(f"- {item}" for item in line.acceptance) or "- (none recorded)"
    lines = [
        f"# Follow-up: {line.id} (dispatch {dispatch.id})",
        "",
        f"**Dispatch goal:** {dispatch.goal}",
        f"**Line task:** {line.task}",
        f"**Branch:** `{line.branch or '—'}`",
        f"**Split off at:** {liveness_record.format_time(moment) or ''}",
        f"**Parked reason:** {parked_reason or reason}",
        f"**Split from:** {reason}",
        f"**Write scope:** {scope}",
        f"**Atomic with:** {', '.join(line.atomic_with) or '—'}",
        "",
        "## Acceptance",
        "",
        acceptance,
        "",
        "## Parking history",
        "",
    ]
    if record is not None:
        for step in record.history:
            lines.append(
                f"- step {step.number} ({step.at}): {step.action} -> "
                f"{step.result}; {step.detail or step.reason}"
            )
        for episode in record.episodes:
            lines.append(
                f"- episode {episode.number}: {episode.reason} / {episode.group}; "
                f"{episode.outcome} at {episode.resolved_at}"
            )
        if record.needs_human:
            lines.append(f"- needs human: {record.human_reason}")
    else:
        lines.append(f"- parked as {(line.failure_reason or reason)}")
    blockers = [str(item) for item in line.blockers if str(item).strip()]
    if blockers:
        lines.extend(["", "## Blockers", "", *[f"- {item}" for item in blockers]])
    escalation = escalation_brief(snapshot)
    if escalation:
        lines.extend(["", "## Escalation", "", escalation])
    text = "\n".join(lines) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


def _gated_block_message(blocked: list[Line], dispatch_id: str = "<dispatch-id>") -> str:
    """A park message. Review text stays as it was; a G2 failure names only its reason code."""
    parts: list[str] = []
    for line in blocked:
        if line.failure_reason == REPLAN_EXHAUSTED:
            parts.append(REPLAN_EXHAUSTED_MESSAGE.format(line_id=line.id))
        elif line.failure_reason in _PARK_MESSAGES:
            parts.append(
                _PARK_MESSAGES[line.failure_reason].format(
                    dispatch_id=dispatch_id, line_id=line.id
                )
            )
        elif line.blockers:
            parts.append(f"Line {line.id} needs a decision: " + "; ".join(line.blockers))
        else:
            parts.append(f"Line {line.id} blocked: {line.failure_reason or 'blocked'}")
    return " ".join(parts)


def _retarget_dependents(dispatch: Dispatch, parent_id: str, child_ids: tuple[str, ...]) -> None:
    """Point downstream lines and atomic partners at the split children.

    The parent id is replaced where it appears in ``depends_on``. Other
    dependencies stay, in order. The children keep the parent's own
    dependencies.

    An atomic partner that still names the replanned parent keeps G5 closed
    after every replacement has passed, and the drive then returns with no
    recovery owner. The partner and the children name each other instead,
    and the parent leaves the group.
    """
    if not child_ids:
        return
    skip = {parent_id, *child_ids}
    for other in dispatch.lines:
        if other.id in skip or parent_id not in other.depends_on:
            continue
        rewritten: list[str] = []
        for dep in other.depends_on:
            if dep == parent_id:
                rewritten.extend(child_ids)
            else:
                rewritten.append(dep)
        other.depends_on = tuple(rewritten)
    _retarget_atomic(dispatch, parent_id, child_ids)


def _retarget_atomic(dispatch: Dispatch, parent_id: str, child_ids: tuple[str, ...]) -> None:
    """Move the parent's atomic group onto its replacement lines."""
    ids = {line.id for line in dispatch.lines}
    child_set = set(child_ids)
    parent = next((line for line in dispatch.lines if line.id == parent_id), None)
    parent_names = tuple(parent.atomic_with) if parent is not None else ()
    partners: list[str] = []
    seen: set[str] = set()
    for line in dispatch.lines:
        if line.id == parent_id or line.id in child_set:
            continue
        linked = parent_id in line.atomic_with or line.id in parent_names
        if linked and line.id not in seen:
            seen.add(line.id)
            partners.append(line.id)
    for name in parent_names:
        if name in ids and name not in child_set and name != parent_id and name not in seen:
            seen.add(name)
            partners.append(name)
    if not partners:
        return
    group = [*partners, *[child for child in child_ids if child in ids]]
    group_set = set(group)
    for line in dispatch.lines:
        if line.id == parent_id:
            line.atomic_with = ()
            continue
        if line.id not in group_set and parent_id not in line.atomic_with:
            continue
        kept = [
            name
            for name in line.atomic_with
            if name != parent_id and name not in group_set
        ]
        others = [name for name in group if name != line.id]
        line.atomic_with = tuple(dict.fromkeys([*kept, *others]))


def _next_serial_line(dispatch: Dispatch, ready: list[Line]) -> Line:
    """First ready line in topological order. v1 launches only that line."""
    wanted = {line.id: line for line in ready}
    for line in topological_lines(dispatch.lines):
        if line.id in wanted:
            return wanted[line.id]
    return ready[0]


def _path_ownership_queued(dispatch: Dispatch) -> bool:
    return any(
        line.status is LineStatus.PENDING
        and (line.failure_reason or "").startswith("path_ownership_conflict:")
        for line in dispatch.lines
    )


def _serial_active(dispatch: Dispatch, gates: GateRunner | None) -> list[Line]:
    """Lines a recovered drive must finish before it launches another.

    ``RUNNING`` is the persisted status. ``implementing`` is the stage of a
    line whose worker is still out, including a snapshot reloaded from storage.
    """
    active: list[Line] = []
    for line in dispatch.lines:
        if line.status is LineStatus.RUNNING:
            if (
                gates is not None
                and _review_retry(gates.line_snapshot(dispatch, line)) is not None
                and gates.run_is_settled(dispatch, line)
            ):
                # Its run finished; only a scheduled review retry is left (N42).
                # No worker is out, so it does not hold the serial slot.
                continue
            active.append(line)
            continue
        if gates is None:
            continue
        if _load_state(gates.line_snapshot(dispatch, line)).stage is Stage.IMPLEMENTING:
            active.append(line)
    return active


def _ready_lines(dispatch: Dispatch) -> list[Line]:
    passed = {line.id for line in dispatch.lines if line.status is LineStatus.PASSED}
    return [
        line
        for line in dispatch.lines
        if line.status is LineStatus.ASSIGNED
        and line.failure_reason not in (REPLAN_EXHAUSTED, REVIEW_BACKLOG)
        and all(dep in passed for dep in line.depends_on)
    ]


def _g5_already_passed(dispatch: Dispatch, gates: GateRunner | None) -> bool:
    """True when G5 already merged the lines, even if this process has no runner.

    Turning gates off after that merge must not send approve through ``_merge``
    a second time. The snapshot is the record; the runner is only a reader.
    """
    if gates is not None:
        return gates.integration_phase(dispatch) == "passed"
    saved = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    integration = saved.get("integration")
    return isinstance(integration, dict) and integration.get("phase") == "passed"


def _ready_for_g5(dispatch: Dispatch) -> bool:
    active = [
        line
        for line in dispatch.lines
        if line.failure_reason not in (REPLANNED, SPLIT_OFF)
    ]
    if not active:
        return False
    if not all(line.status is LineStatus.PASSED for line in active):
        return False
    # An atomic group is integrated as a whole. If one member was split off,
    # failed, or replanned, the remaining PASSED member is not eligible alone.
    active_ids = {line.id for line in active}
    for line in dispatch.lines:
        group = set(line.atomic_with)
        for other in dispatch.lines:
            if line.id in other.atomic_with:
                group.add(other.id)
        group.discard(line.id)
        if not group:
            continue
        if (line.id in active_ids or group & active_ids) and not group <= active_ids:
            return False
    return True


async def _reassign_handoff(dispatch: Dispatch, line: Line, gates: GateRunner) -> Handoff:
    facts = await gates.handoff_facts_async(
        dispatch, line, limitations="human reassign"
    )
    files = facts.get("files") or ()
    handoff = Handoff(
        branch=str(facts.get("branch") or ""),
        commit=str(facts.get("commit") or ""),
        files=tuple(files) if isinstance(files, tuple) else tuple(files),
        test_command=str(facts.get("test_command") or ""),
        results=str(facts.get("results") or ""),
        known_limitations=str(facts.get("known_limitations") or "none"),
    )
    if handoff.complete or _g2_ran(gates.line_snapshot(dispatch, line)):
        return handoff
    return await _pre_g2_handoff(line, gates) or handoff


# The handoff test command of a line whose worker exited before G2 ran (N57).
PRE_G2_TEST_COMMAND = "none (exited before G2)"


def _g2_ran(snap: dict[str, object]) -> bool:
    """False only for the blank G2 shell: nothing ran, pinned or was decided."""
    cells = snap.get("gates") if isinstance(snap, dict) else None
    g2 = cells.get("G2") if isinstance(cells, dict) else None
    if not isinstance(g2, dict):
        return False
    return bool(
        g2.get("result") not in (None, "", "pending")
        or g2.get("revision")
        or g2.get("commands")
        or g2.get("at")
    )


async def _pre_g2_handoff(line: Line, gates: GateRunner) -> Handoff | None:
    """The handoff of committed work that never reached G2 (N57), or None.

    The blank G2 shell pins no revision, so the generic facts call the commit
    ``unknown`` and list no files. Here the branch diff is the evidence: the
    branch tip, its commit count past base and its changed files. A branch
    with nothing past base, or one Git cannot read, has no handoff.
    """
    branch = line.branch or ""
    settings = getattr(gates, "_settings", None)
    if not branch or settings is None:
        return None
    repo = Path(settings.repo_root)
    base = line.base_commit or "HEAD"
    commit = await asyncio.to_thread(gate_git_revision, repo, branch) or ""
    if not commit:
        return None
    files = await asyncio.to_thread(gate_git_changed_paths, repo, commit, base)
    if not files:
        return None
    count = await asyncio.to_thread(_git_commit_count, repo, base, commit)
    commits = f"{count} commit(s)" if count is not None else "commits"
    return Handoff(
        branch=branch,
        commit=commit,
        files=files,
        test_command=PRE_G2_TEST_COMMAND,
        results=(
            f"G2 never ran; evidence is the branch diff: {commits} "
            f"{base[:12]}..{commit[:12]}, {len(files)} file(s) changed"
        ),
        known_limitations=(
            "human reassign; the outgoing worker exited before G2, "
            "so no focused test ran on this work"
        ),
    )


def _git_commit_count(repo: Path, base: str, commit: str) -> int | None:
    """Commits on ``commit`` past ``base``; None unless both are full ids."""
    if not (_is_full_object_id(base) and _is_full_object_id(commit)):
        return None
    try:
        done = run_isolated_git_readonly(
            str(repo), ["rev-list", "--count", f"{base}..{commit}"], timeout=5
        )
    except Exception as exc:  # noqa: BLE001 -- an unknown count is left out
        logger.warning("Commit count failed: %s", type(exc).__name__)
        return None
    count = done.stdout.decode("utf-8", "replace").strip()
    return int(count) if done.returncode == 0 and count.isdigit() else None


def _is_full_object_id(value: str) -> bool:
    return len(value) in (40, 64) and all(ch in "0123456789abcdef" for ch in value)


# The never-capped record of every HEAD that entered G2 or review, per line
# and per branch: {line_id: {branch: {"complete": bool, "heads": [str]}}} (N38).
# A branch's entry is never replaced, so a reused branch name keeps its heads.
REVIEWED_HEADS = "reviewed_heads"
# Why a ledger is incomplete (N78). Only a pre-N78 ledger (no doubt recorded)
# of a replan child's own branch may still vouch for an unlisted head.
LEDGER_DOUBT_NOT_FRESH = "branch_not_fresh"
LEDGER_DOUBT_UNOPENED = "reviewed_before_ledger"
LEDGER_DOUBT_HEAD_UNKNOWN = "review_head_unknown"
LEDGER_DOUBT_UNKNOWN = "unknown"
# The reason recorded when an empty run delivers the inherited branch tip (N78).
DELIVERED_INHERITED_TIP = "delivered_inherited_tip"


def _reviewed_ledger(dispatch: Dispatch, line: Line) -> dict[str, object] | None:
    """The ledger of the line's current branch, or None."""
    root = dispatch.gates if isinstance(dispatch.gates, dict) else None
    ledgers = root.get(REVIEWED_HEADS) if isinstance(root, dict) else None
    branches = ledgers.get(line.id) if isinstance(ledgers, dict) else None
    ledger = branches.get(line.branch or "") if isinstance(branches, dict) else None
    return ledger if isinstance(ledger, dict) else None


def _open_reviewed_ledger(
    dispatch: Dispatch, line: Line, *, complete: bool, doubt: str = ""
) -> dict[str, object]:
    """Open the current branch's ledger. An existing one is kept as it is.

    Its heads stay (a rejected HEAD still blocks); only ``complete=False``
    can change it, and only towards incomplete. An incomplete ledger names
    why in ``doubt`` (N78); the first doubt stays. A ledger written before
    N78 has no ``doubt`` key.
    """
    existing = _reviewed_ledger(dispatch, line)
    if existing is not None:
        if not complete:
            existing["complete"] = False
            if not existing.get("doubt"):
                existing["doubt"] = doubt or LEDGER_DOUBT_UNKNOWN
        return existing
    if not isinstance(dispatch.gates, dict):
        dispatch.gates = {}
    ledgers = dispatch.gates.get(REVIEWED_HEADS)
    if not isinstance(ledgers, dict):
        ledgers = {}
        dispatch.gates[REVIEWED_HEADS] = ledgers
    branches = ledgers.get(line.id)
    if not isinstance(branches, dict):
        branches = {}
        ledgers[line.id] = branches
    ledger: dict[str, object] = {"complete": complete, "heads": []}
    if not complete:
        ledger["doubt"] = doubt or LEDGER_DOUBT_UNKNOWN
    branches[line.branch or ""] = ledger
    return ledger


def _on_replan_child_branch(
    dispatch: Dispatch, line: Line, snap: dict[str, object]
) -> bool:
    """True when ``line`` is a replan child still on the branch it was born with (N78).

    A replan child gets a new line id and a new ``line-<id>`` branch from its
    dependency's head or the integrate base. Nothing on that branch can have
    been reviewed before the child existed.
    """
    parent = snap.get("replanned_from")
    if not isinstance(parent, str) or not parent:
        return False
    return (line.branch or "") == f"{dispatch.branch_prefix}/line-{line.id}"


# Written on a line snapshot when worker recovery parks it after the worker
# exited mid-implementation (N38b), when a run ends with no new commit or a
# dirty tree (N40), and by ``dispatch unblock`` for a line parked before that.
WORKER_EXITED = "worker_exited"
# Providers skipped for new launches after a rate limit or a spent quota (N25a):
# {provider: {"reason", "until" (epoch seconds), "source", "at"}}.
PROVIDER_SKIPS = "provider_skips"
ZCODE_PLAN_QUOTA_EXHAUSTED = "zcode_plan_quota_exhausted"
_RATE_LIMIT_SKIP_SECONDS = 300.0
_QUOTA_SKIP_SECONDS = 1800.0
_PROVIDER_SKIP_MAX_SECONDS = 6 * 3600.0
# A line no reviewer may take: each is out of quota or rate limited (N63).
REVIEWERS_QUOTA_EXHAUSTED = "reviewers_quota_exhausted"
# The finding of a reviewer conflict a human adjudicates (N41).
ADJUDICATION = "adjudication"
# A review parked after its retry budget: the run and stage to resume (N42).
REVIEW_PARKED = "review_parked"
# A scheduled review retry: attempt, failed reviewers, retry_at (N42).
REVIEW_RETRY = "review_retry"
_MAX_RECOVERY_EVENTS = 32


def record_worker_exited(
    dispatch: Dispatch,
    line: Line,
    snap: dict[str, object],
    *,
    run_id: str,
    failure_class: str,
    exit_code: int | None,
    stamp: str,
    note: str = "",
) -> None:
    """Record that no run of this line is out, then pause it in Stage-Gate.

    The checkpoint names the current author, attempt and run; the same run is
    written as the line's last recovery event, which is what
    ``_worker_exited_checkpoint`` matches. A stage other than ``blocked``
    takes the ``block`` transition, so a pause from ``implementing`` is
    recorded and a human reassign of it passes ``option=worker_exited``.
    """
    author = line.assigned_model or ""
    if not isinstance(dispatch.gates, dict):
        dispatch.gates = {}
    root = dispatch.gates.get("worker_recovery")
    if not isinstance(root, dict):
        root = {}
        dispatch.gates["worker_recovery"] = root
    entry = root.get(line.id)
    if not isinstance(entry, dict):
        entry = {"attempts": {}, "session_id": None, "session_mode": None}
        root[line.id] = entry
    event: dict[str, object] = {
        "failure_class": failure_class,
        "reason": failure_class,
        "action": "park",
        "attempt": line.attempts,
        "author": author,
        "run_id": run_id,
        "epoch": drive_epoch(dispatch),
    }
    event_tags.tag(event, role="worker", session=run_id, snapshot=entry)
    entry["last"] = event
    events = entry.get("events")
    if not isinstance(events, list):
        events = []
        entry["events"] = events
    events.append(dict(event))
    del events[:-_MAX_RECOVERY_EVENTS]
    record: dict[str, object] = {
        "author": author,
        "attempt": line.attempts,
        "run_id": run_id,
        "failure_class": failure_class,
        "exit_code": exit_code,
    }
    note_text = " ".join((note or "").split())
    if note_text:
        record["note"] = note_text[:200]
    snap[WORKER_EXITED] = record
    state = _load_state(snap)
    if state.stage.terminal or state.stage is Stage.BLOCKED:
        return
    transition = advance(
        state, Event(kind="block", timestamp=stamp, provider=author)
    )
    if transition.accepted:
        _remember_event(snap, transition)
        _store_state(snap, transition.state)


def _worker_exited_checkpoint(
    snap: dict[str, object], line: Line, dispatch: Dispatch
) -> bool:
    """True when this line's pause was taken after its own worker exited.

    The record must match the current attempt and author, and its run must
    be the line's last recovery event: a run reserved or failed since then
    means the checkpoint is not this pause's.
    """
    record = snap.get(WORKER_EXITED)
    if not isinstance(record, dict) or line.current_run:
        return False
    if line.status not in (LineStatus.BLOCKED, LineStatus.FAILED):
        return False
    if record.get("attempt") != line.attempts or record.get("author") != (
        line.assigned_model or ""
    ):
        return False
    root = dispatch.gates.get("worker_recovery") if isinstance(dispatch.gates, dict) else None
    entry = root.get(line.id) if isinstance(root, dict) else None
    last = entry.get("last") if isinstance(entry, dict) else None
    run_id = record.get("run_id")
    return (
        isinstance(run_id, str)
        and bool(run_id)
        and isinstance(last, dict)
        and last.get("run_id") == run_id
    )


def line_has_pending_fix(dispatch: Dispatch, line_id: str) -> bool:
    """True when this line still has a structured fix intent."""
    root = dispatch.gates if isinstance(dispatch.gates, dict) else None
    if not isinstance(root, dict):
        return False
    raw = root.get("pending_fix")
    if not isinstance(raw, dict):
        return False
    return isinstance(raw.get(line_id), dict)


# A human reassign that arrives while the dispatch's drive runs. The service
# records it here and does not touch the line or the epoch. The owning drive
# applies it at its next loop step; the service applies any leftover request
# before it releases the drive. Keyed by line id.
HUMAN_REASSIGNS = "human_reassigns"
_REASSIGNABLE_WHILE_DRIVING = frozenset({LineStatus.BLOCKED, LineStatus.FAILED})


def pending_human_reassigns(dispatch: Dispatch) -> dict[str, dict[str, object]]:
    """Queued human reassigns, by line id. A malformed entry is skipped."""
    root = dispatch.gates if isinstance(dispatch.gates, dict) else None
    if not isinstance(root, dict):
        return {}
    raw = root.get(HUMAN_REASSIGNS)
    if not isinstance(raw, dict):
        return {}
    return {
        str(line_id): request
        for line_id, request in raw.items()
        if isinstance(request, dict)
    }


# The kinds of human request the queue carries. A request without ``kind``
# is a reassign, as every request was before N40/N41.
REQUEST_REASSIGN = "reassign"
REQUEST_UNBLOCK = "unblock"
REQUEST_ADJUDICATE = "adjudicate"
REQUEST_RECOVER = "recover"


def request_kind(request: dict[str, object]) -> str:
    kind = request.get("kind")
    return kind if isinstance(kind, str) and kind else REQUEST_REASSIGN


def queue_human_reassign(
    dispatch: Dispatch, line: Line, model: str, audit: dict[str, object] | None
) -> None:
    """Record a reassign for the running drive. The line itself is not changed."""
    queue_human_request(
        dispatch,
        line,
        {
            "model": model,
            "audit": copy.deepcopy(audit) if audit is not None else None,
        },
    )


def human_request(line: Line, fields: dict[str, object]) -> dict[str, object]:
    """A request as the queue stores it: its fields plus the line it was made for."""
    return {
        **copy.deepcopy(fields),
        "attempts": line.attempts,
        "status": line.status.value,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }


def queue_human_request(
    dispatch: Dispatch, line: Line, fields: dict[str, object]
) -> None:
    """Record a human request for the running drive. The line itself is not changed."""
    if not isinstance(dispatch.gates, dict):
        dispatch.gates = {}
    queue = dispatch.gates.get(HUMAN_REASSIGNS)
    if not isinstance(queue, dict):
        queue = {}
        dispatch.gates[HUMAN_REASSIGNS] = queue
    queue[line.id] = human_request(line, fields)


def drop_human_reassign(dispatch: Dispatch, line_id: str) -> None:
    """Remove one queued request. The key stays, so a merge removes only this line."""
    root = dispatch.gates if isinstance(dispatch.gates, dict) else None
    if not isinstance(root, dict):
        return
    queue = root.get(HUMAN_REASSIGNS)
    if isinstance(queue, dict):
        queue.pop(line_id, None)


def human_reassign_blocker(dispatch: Dispatch, line: Line) -> str | None:
    """Why this line cannot be reassigned while a drive owns the dispatch.

    Only a BLOCKED or FAILED line with no run and no worker out is safe: the
    drive is not watching it. A RUNNING, ASSIGNED, or ``implementing`` line
    belongs to the drive, so the reason stays ``drive_active``.
    """
    if line.status not in _REASSIGNABLE_WHILE_DRIVING or line.current_run:
        return "drive_active"
    lines = dispatch.gates.get("lines") if isinstance(dispatch.gates, dict) else None
    snap = lines.get(line.id) if isinstance(lines, dict) else None
    if isinstance(snap, dict) and _load_state(snap).stage is Stage.IMPLEMENTING:
        return "drive_active"
    return None


async def apply_human_reassign(
    dispatch: Dispatch,
    line: Line,
    model: str,
    audit: dict[str, object] | None,
    gates: GateRunner | None,
) -> str | None:
    """Stage-Gate check, then the line fields of a human reassign.

    The parked path and the drive hand-off share this, so both relaunch the
    line the same way. Returns the refusal reason and leaves the line alone,
    or None after the line is ASSIGNED to ``model`` on its next attempt. The
    caller persists the row and decides whether to bump the epoch.
    """
    gated = gates is not None and isinstance(dispatch.gates, dict) and bool(
        dispatch.gates.get("lines")
    )
    if gated:
        assert gates is not None
        reason = await human_reassign_refusal(dispatch, line, model, gates)
        if reason:
            return reason
    previous_blockers = list(line.blockers)
    previous_reason = line.failure_reason
    rejected = (
        await _rejected_tip_brief(dispatch, line, gates) if gated and gates is not None else []
    )
    line.assigned_model = model
    line.attempts += 1
    line.status = LineStatus.ASSIGNED
    line.current_run = None
    line.blockers = []
    # A cleared reason lets a replan-exhausted line be picked and launched.
    line.failure_reason = None
    line.assignment_audit = copy.deepcopy(audit) if audit is not None else None
    _write_reassign_brief(
        dispatch, line, previous_blockers, previous_reason, rejected=rejected
    )
    _reset_worker_retries(dispatch, line)
    lines_root = dispatch.gates.get("lines") if isinstance(dispatch.gates, dict) else None
    reassigned_snap = lines_root.get(line.id) if isinstance(lines_root, dict) else None
    if isinstance(reassigned_snap, dict):
        _clear_review_retry(reassigned_snap)
    if previous_reason == UNCOMMITTED_CHANGES:
        # The previous run's edits stay in the worktree, which is never
        # reset; the new model starts on them. The confirmed start clears it.
        root = dispatch.gates.setdefault("worker_recovery", {})
        if isinstance(root, dict):
            entry = root.setdefault(
                line.id, {"attempts": {}, "session_id": None, "session_mode": None}
            )
            if isinstance(entry, dict):
                entry["allow_dirty_worktree"] = True
    release_human_latch(dispatch, line)
    return None


def release_human_latch(dispatch: Dispatch, line: Line) -> bool:
    """A person acted on a line they were handed: give it back to the sweep (N76).

    The latch keeps the sweep off a line while a person owns it and makes the
    notification happen once. Once a human reassign, unblock or adjudication
    is applied, the person is done with this episode: the record is stamped
    handled and closed, so a later park of the line is a new episode the
    sweep owns again (and pages again if it ends with a person). The budget
    already spent is kept. A line with no latched record is left as it is.
    """
    root = dispatch.gates.get("lines") if isinstance(dispatch.gates, dict) else None
    cell = root.get(line.id) if isinstance(root, dict) else None
    if not isinstance(cell, dict):
        return False
    current = liveness_record.load(cell)
    if current is None or not current.needs_human:
        return False
    moment = datetime.now(timezone.utc)
    released = replace(
        current.mark_human_handled(at=moment),
        # A person-group park records a zero cap, and a re-park keeps the
        # smaller cap; give the line the per-line cap back (spent is kept).
        budget=max(current.budget, liveness_policy.max_steps()),
        needs_human=False,
        notified_at=None,
        next_at=None,
        next_action=None,
        outcome=liveness_record.RECOVERED,
    )
    cell.update(liveness_record.store(cell, released))
    return True


# Bounded like a fix round's blockers: at most this many, each at most this
# long. ``_fix_task`` still caps the whole prompt at MAX_LINE_TASK_BYTES.
REASSIGN_BRIEFS = "reassign_briefs"
# Out-of-scope adjudications: findings recorded for later, not fixed here (N41).
FOLLOW_UPS = "follow_ups"
UNCOMMITTED_CHANGES = "uncommitted_changes"
# Room for the previous run's uncommitted diff inside MAX_LINE_TASK_BYTES.
_UNCOMMITTED_DIFF_BYTES = 4_000
_BRIEF_MAX_BLOCKERS = 20
_BRIEF_MAX_CHARS = 1_000
# The findings of the last review that rejected the branch tip, on the gate
# snapshot until a review passes (N79), and the head that review saw.
LAST_REVIEW_BLOCKERS = "last_review_blockers"
#: The head a ruling-check BLOCK sent back for confirmations only (R5 N6).
RULING_REISSUE_HEAD = "ruling_reissue_head"
#: Ruling items a fix report left unconfirmed, for the next reviewer (R5 N6).
RULING_UNCONFIRMED = "ruling_unconfirmed"
#: ``1`` turns an unconfirmed ruling into a BLOCK before review; anything else
#: (the default) records it and leaves the verdict to the reviewer.
RULING_ENFORCE_ENV = "CONDUCTOR_DISPATCH_RULING_ENFORCE"


_UNCONFIRMED_WORDING = re.compile(r"^RULING ([MN]\d{1,3}): not confirmed(?: -)?")


def ruling_enforced(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return str(source.get(RULING_ENFORCE_ENV) or "").strip() == "1"
HEAD_UNDER_REVIEW = "head_under_review"
_REVIEW_FINDING = "Review finding: "
_REJECTED_LEAD = "Review by "


def _write_reassign_brief(
    dispatch: Dispatch,
    line: Line,
    blockers: list[str],
    failure_reason: str | None,
    *,
    rejected: list[str] | None = None,
    escalation: bool | None = None,
) -> None:
    """Keep the previous attempt's findings for the new model's first launch.

    The brief names the attempt and author it is for, so it is used only by
    that relaunch and its same-attempt retries, never by a later attempt.
    ``rejected`` is the review's own word on the branch tip (N79): it comes
    first, so the cap drops an operator message before a finding.
    """
    items = list(rejected or ())[:_BRIEF_MAX_BLOCKERS]
    seen = set(items)
    for item in blockers:
        text = str(item)[:_BRIEF_MAX_CHARS]
        if not text.strip() or text in seen or _as_finding(text) in seen:
            continue
        items.append(text)
        seen.add(text)
    items = items[:_BRIEF_MAX_BLOCKERS]
    # N76 §4.2: the R5 L3 escalation summary rides with the brief, so the new
    # author sees that two fix rounds were BLOCKed for the same reason. It
    # counts against the cap; the stop reason below is the one line that may
    # follow it, which is the order N79 established.
    lines = dispatch.gates.get("lines") if isinstance(dispatch.gates, dict) else None
    snapshot = lines.get(line.id) if isinstance(lines, dict) else None
    # Only enforce adds it: off and shadow keep main's brief exactly (a brief
    # is worker input, not a record). ``None`` reads the env.
    if escalation is None:
        escalation = liveness_modes.should_act()
    summary = escalation_brief(snapshot) if escalation else ""
    if summary:
        text = f"Escalation summary (do not repeat the same approach):\n{summary}"
        items.append(text[:_BRIEF_MAX_CHARS])
        items = items[:_BRIEF_MAX_BLOCKERS]
    if failure_reason:
        items.append(f"Previous attempt stopped with: {failure_reason}"[:_BRIEF_MAX_CHARS])
    if not isinstance(dispatch.gates, dict):
        dispatch.gates = {}
    briefs = dispatch.gates.get(REASSIGN_BRIEFS)
    if not isinstance(briefs, dict):
        briefs = {}
        dispatch.gates[REASSIGN_BRIEFS] = briefs
    briefs[line.id] = {
        "author": line.assigned_model or "",
        "attempt": line.attempts,
        "blockers": items,
    }
    if failure_reason == UNCOMMITTED_CHANGES:
        # The dirty tree stays in the line's worktree; the relaunch is shown
        # its diff and may start on it (N40).
        briefs[line.id]["uncommitted"] = True


def _brief_text(value: object) -> str:
    """One reviewer finding as a brief item: credentials out, controls out, capped."""
    text = str(redact_credentials(str(value)))
    return sanitize_output_text(text, limit=0).strip()[:_BRIEF_MAX_CHARS]


def _note_last_review(
    snap: dict[str, object],
    line: Line,
    verdict: Verdict,
    *,
    reviewer: str,
    rejected: bool,
    at: str,
) -> str:
    """Keep the findings of a review that rejected the branch tip (N79).

    ``line.blockers`` is overwritten when the line parks (the replan cap,
    ``no_new_commit``), so the reviewer's own text lives on the gate snapshot
    until a review passes. A reassign reads it back into the new author's
    brief, so an unchanged tip is not mistaken for finished work.
    """
    # Only this review's result needs the head it saw; it moves into the entry.
    head = snap.pop(HEAD_UNDER_REVIEW, None)
    if not verdict.passed and only_unconfirmed(verdict.blockers):
        # The ruling check's own BLOCK (R5 N6) judged no code: the last real
        # reviewer's findings stay the ones a reassign brief carries.
        return head if isinstance(head, str) else ""
    if verdict.passed or not rejected:
        snap.pop(LAST_REVIEW_BLOCKERS, None)
        return head if isinstance(head, str) else ""
    keep_rejected_findings(
        snap, line, list(verdict.blockers), reviewer=reviewer, at=at, head=head
    )
    return head if isinstance(head, str) else ""


def keep_rejected_findings(
    snap: dict[str, object],
    line: Line,
    blockers: list[str],
    *,
    reviewer: str,
    at: str,
    head: object = "",
) -> None:
    """Store the findings the branch tip was rejected for (N79), or forget them.

    Also used by an in-scope adjudication, whose ruling makes the conflict
    finding the author's to fix. ``head`` is the commit that review saw.
    """
    findings = [_brief_text(item) for item in blockers]
    findings = [item for item in findings if item]
    if not findings:
        snap.pop(LAST_REVIEW_BLOCKERS, None)
        return
    snap[LAST_REVIEW_BLOCKERS] = {
        "head": head if isinstance(head, str) else "",
        "branch": line.branch or "",
        "reviewer": reviewer,
        "count": len(findings),
        "blockers": findings[: _BRIEF_MAX_BLOCKERS - 1],
        "at": at,
    }


async def _rejected_tip_brief(
    dispatch: Dispatch, line: Line, gates: GateRunner
) -> list[str]:
    """The last rejecting review's findings for a reassign brief, or [] (N79).

    Only for the branch the review saw. When the branch tip is that reviewed
    head (or git cannot say), the brief opens with an instruction that the
    tip was rejected and must change: N38 never resubmits it unchanged, so an
    author who only verifies it parks ``no_new_commit`` again.
    """
    root = dispatch.gates if isinstance(dispatch.gates, dict) else None
    lines = root.get("lines") if isinstance(root, dict) else None
    snap = lines.get(line.id) if isinstance(lines, dict) else None
    entry = snap.get(LAST_REVIEW_BLOCKERS) if isinstance(snap, dict) else None
    if not isinstance(entry, dict):
        return []
    if str(entry.get("branch") or "") != (line.branch or ""):
        return []
    raw = entry.get("blockers")
    findings = [_brief_text(item) for item in raw] if isinstance(raw, list) else []
    findings = [item for item in findings if item]
    if not findings:
        return []
    head = str(entry.get("head") or "")
    try:
        tip = await asyncio.to_thread(gates._revision, line)
    except Exception:  # context only: an unknown tip is read as the reviewed one
        tip = None
    ledger = _reviewed_ledger(dispatch, line)
    listed = ledger.get("heads") if isinstance(ledger, dict) else None
    known = isinstance(tip, str) and bool(tip)
    # As N38 reads it: a tip the ledger lists, or one it cannot vouch for,
    # is not resubmitted by a run that leaves it unchanged.
    reviewed = (
        not known
        or tip == head
        or (isinstance(listed, list) and tip in listed)
        or not isinstance(ledger, dict)
        or ledger.get("complete") is not True
    )
    reviewer = str(entry.get("reviewer") or "the reviewer")
    named = f" ({head[:12]})" if head and (not known or tip == head) else ""
    if reviewed:
        lead = (
            f"{_REJECTED_LEAD}{reviewer} rejected this line's current branch tip{named}. "
            "It is not finished work: do not just verify it and exit. Change the "
            "code to address every review finding below, run the tests, and "
            "commit. A tip that review rejected is never resubmitted unchanged, "
            "so a run that leaves it as it is stops with no_new_commit again."
        )
    else:
        lead = (
            f"{_REJECTED_LEAD}{reviewer} rejected an earlier tip of this branch{named}. "
            "Make sure the code on the branch addresses every review finding "
            "below, and commit what it still needs."
        )
    return [lead[:_BRIEF_MAX_CHARS], *(_as_finding(item) for item in findings)]


def _as_finding(text: str) -> str:
    """A review finding as the brief lists it, within the per-item cap."""
    return (_REVIEW_FINDING + _brief_text(text))[:_BRIEF_MAX_CHARS]


def _current_brief(dispatch: Dispatch, line: Line) -> dict[str, object] | None:
    root = dispatch.gates if isinstance(dispatch.gates, dict) else None
    briefs = root.get(REASSIGN_BRIEFS) if isinstance(root, dict) else None
    brief = briefs.get(line.id) if isinstance(briefs, dict) else None
    if not isinstance(brief, dict):
        return None
    if brief.get("author") != (line.assigned_model or ""):
        return None
    attempt = brief.get("attempt")
    if isinstance(attempt, bool) or attempt != line.attempts:
        return None
    return brief


def reassign_brief_has_uncommitted(dispatch: Dispatch, line: Line) -> bool:
    """True when this attempt's brief was written after an ``uncommitted_changes`` stop."""
    brief = _current_brief(dispatch, line)
    return brief is not None and brief.get("uncommitted") is True


def reassign_brief(dispatch: Dispatch, line: Line) -> list[str] | None:
    """The blockers for this line's current attempt and author, if a reassign left any."""
    brief = _current_brief(dispatch, line)
    if brief is None:
        return None
    items = brief.get("blockers")
    if not isinstance(items, list):
        return None
    return [str(item) for item in items]


def drop_reassign_brief(dispatch: Dispatch, line_id: str) -> None:
    """Forget a line's brief. The key stays, so a merge removes only this line."""
    root = dispatch.gates if isinstance(dispatch.gates, dict) else None
    briefs = root.get(REASSIGN_BRIEFS) if isinstance(root, dict) else None
    if isinstance(briefs, dict):
        briefs.pop(line_id, None)


def _reset_worker_retries(dispatch: Dispatch, line: Line) -> None:
    """The new model starts with its own retry budget, as an automatic reassign does."""
    root = dispatch.gates.get("worker_recovery") if isinstance(dispatch.gates, dict) else None
    entry = root.get(line.id) if isinstance(root, dict) else None
    if not isinstance(entry, dict):
        return
    entry["attempts"] = {}
    for key in ("retry_at", "retry_source", "quota_retry_at", "quota_retry_source", "parked_reason"):
        entry.pop(key, None)


def note_human_reassign_audit(
    dispatch: Dispatch,
    line_id: str,
    request: dict[str, object],
    *,
    previous: dict[str, object],
    refused: str | None,
    applied_by: str,
) -> None:
    """One scheduler audit entry per handled request, applied or dropped."""
    if not isinstance(dispatch.gates, dict):
        dispatch.gates = {}
    scheduler = dispatch.gates.get("scheduler")
    if not isinstance(scheduler, dict):
        scheduler = {}
        dispatch.gates["scheduler"] = scheduler
    audit = scheduler.get("audit")
    if not isinstance(audit, list):
        audit = []
        scheduler["audit"] = audit
    kind = request_kind(request)
    entry: dict[str, object] = {
        "kind": "human_reassign" if kind == REQUEST_REASSIGN else f"human_{kind}",
        "line_id": line_id,
        "model": request.get("model"),
        "applied_by": applied_by,
        "result": "refused" if refused else "applied",
        **previous,
    }
    if refused:
        entry["reason"] = refused
    audit.append(entry)


def previous_assignment(line: Line | None) -> dict[str, object]:
    if line is None:
        return {}
    return {
        "previous_model": line.assigned_model or "",
        "previous_status": line.status.value,
        "previous_failure_reason": line.failure_reason,
        "previous_blockers": list(line.blockers),
    }


def human_reassign_request_refusal(
    dispatch: Dispatch, line: Line | None, request: dict[str, object]
) -> str | None:
    """Re-check a queued request against the line as it is now."""
    if line is None:
        return "no_such_line"
    kind = request_kind(request)
    if kind == REQUEST_REASSIGN:
        model = request.get("model")
        if not isinstance(model, str) or not model:
            return "invalid_request"
    elif kind not in (REQUEST_UNBLOCK, REQUEST_ADJUDICATE, REQUEST_RECOVER):
        return "invalid_request"
    attempts = request.get("attempts")
    if isinstance(attempts, bool) or attempts != line.attempts:
        return "line_moved"
    if line_has_pending_fix(dispatch, line.id):
        return "fix_pending"
    if kind == REQUEST_REASSIGN:
        return human_reassign_blocker(dispatch, line)
    from .operator import decision_refusal

    return decision_refusal(dispatch, line, request)


async def apply_human_request(
    dispatch: Dispatch,
    line: Line,
    request: dict[str, object],
    gates: GateRunner | None,
) -> str | None:
    """Apply one queued human request: a reassign, an unblock or an adjudication.

    Returns the refusal reason and leaves the line alone, or None once applied.
    """
    kind = request_kind(request)
    if kind == REQUEST_REASSIGN:
        raw_audit = request.get("audit")
        return await apply_human_reassign(
            dispatch,
            line,
            str(request.get("model") or ""),
            raw_audit if isinstance(raw_audit, dict) else None,
            gates,
        )
    from .operator import apply_decision

    refused = apply_decision(dispatch, line, request)
    if refused is None and kind != REQUEST_RECOVER:
        release_human_latch(dispatch, line)
    return refused


async def human_reassign_refusal(
    dispatch: Dispatch,
    line: Line,
    model: str,
    gates: GateRunner,
    *,
    handoff: Handoff | None = None,
) -> str | None:
    """Run Stage-Gate ``advance`` for a human reassignment.

    Returns the transition's reason when it is refused. ``implementing`` is
    ``reassignment_not_at_checkpoint``. A legal checkpoint with missing handoff
    fields is ``handoff_incomplete``. An acceptance writes that transition onto
    the line snapshot and returns None. The caller commits the line fields and
    the epoch in the same mutation.

    ``handoff`` lets an automatic recovery describe a line that never launched
    (there is no branch or delivered work to read from Git yet); a human path
    still reads the real facts.
    """
    if handoff is None:
        handoff = await _reassign_handoff(dispatch, line, gates)
    snap = gates.line_snapshot(dispatch, line)
    state = _load_state(snap)
    paused: Transition | None = None
    if (
        line.status is LineStatus.BLOCKED
        and line.failure_reason == REVIEW_UNPARSEABLE
        and state.stage in (Stage.PRIMARY_REVIEW, Stage.SECONDARY_REVIEW)
    ):
        # Parked before N75 without a Stage-Gate pause: the line is not under
        # review any more, so record that pause first. It is kept only if the
        # reassign that follows is accepted.
        paused = advance(
            state,
            Event(kind="block", timestamp=gates.stamp(), provider=line.assigned_model or ""),
        )
        if paused.accepted:
            state = paused.state
        else:
            paused = None
    if state.stage is Stage.REPLANNING and "replace_developer" in (state.replan_options or ()):
        # A replan-exhausted line waits in ``replanning``. Stage-Gate's own
        # human path out of it is the ``replace_developer`` decision.
        event = Event(
            kind="replan_decision",
            timestamp=gates.stamp(),
            provider=model,
            handoff=handoff,
            option="replace_developer",
        )
    else:
        exited = (
            state.stage is Stage.BLOCKED
            and state.blocked_from == Stage.IMPLEMENTING.value
            and _worker_exited_checkpoint(snap, line, dispatch)
        )
        event = Event(
            kind="reassign",
            timestamp=gates.stamp(),
            provider=model,
            handoff=handoff,
            # Tells Stage-Gate the pause came after the worker exited.
            option=WORKER_EXITED if exited else "",
        )
    transition = advance(state, event)
    if not transition.accepted:
        return transition.reason or "reassignment_refused"
    if paused is not None:
        _remember_event(snap, paused)
    _remember_event(snap, transition)
    snap.pop(WORKER_EXITED, None)
    new_state = transition.state
    if handoff.files:
        # The branch already has changes beyond base, so the outgoing
        # developer's work may be on it, recorded or not. Make them a
        # contributor: no reviewer pick for this line may choose them.
        outgoing = [
            name
            for name in (state.developer_provider, line.assigned_model or "")
            if name and name != model and name not in new_state.contributors
        ]
        if outgoing:
            new_state = replace(
                new_state, contributors=(*new_state.contributors, *dict.fromkeys(outgoing))
            )
    _store_state(snap, new_state)
    if handoff.complete:
        snap["handoff_ref"] = handoff.reference()
    return None


async def note_human_reassign(
    dispatch: Dispatch, line: Line, model: str, gates: GateRunner
) -> None:
    """Tell Stage-Gate about a human reassignment. A refusal leaves the line as the service set it."""
    snap = gates.line_snapshot(dispatch, line)
    handoff = await _reassign_handoff(dispatch, line, gates)
    if not handoff.complete:
        logger.warning("Human reassign of %s has an incomplete handoff", line.id)
        return
    state = _load_state(snap)
    transition = advance(
        state,
        Event(
            kind="reassign",
            timestamp=gates.stamp(),
            provider=model,
            handoff=handoff,
        ),
    )
    if not transition.accepted:
        logger.warning(
            "Stage-Gate refused reassign on %s: %s", line.id, transition.reason
        )
        return
    _remember_event(snap, transition)
    _store_state(snap, transition.state)
    snap["handoff_ref"] = handoff.reference()


def cancel_planner_failed(dispatch: Dispatch, *, note: str = "") -> None:
    """Abandon a dispatch parked on a planner failure, in place.

    The planner never produced a plan, so nothing ran: there is no integrate
    branch to move aside and no worker to stop. The dispatch goes to ABORT and
    keeps the reason it parked with, so an operator reads one terminal row
    instead of a run that never moves again. The note is the operator's own
    words and is kept beside the reason.

    A row parked before the reason codes existed carries the bare
    ``planner_failed``; cancelling it writes the current spelling, with the
    reason it never recorded read as ``planner_error``.
    """
    if not is_planner_failed(dispatch):
        raise DispatchError(
            f"Dispatch {dispatch.id} is not parked on a planner failure"
        )
    message = f"{planner_failure_message(parked_planner_reason(dispatch))}:cancelled"
    note_text = " ".join((note or "").split())
    if note_text:
        message = f"{message}: {note_text[:200]}"
    dispatch.state = DispatchState.ABORT
    dispatch.message = message


def progress_pct(dispatch: Dispatch) -> int:
    """How far a dispatch is, for the active list a dashboard renders."""
    order = [
        DispatchState.SUBMITTED,
        DispatchState.PLANNING,
        DispatchState.ASSIGNED,
        DispatchState.LAUNCHING,
        DispatchState.MONITORING,
        DispatchState.REVIEW,
        DispatchState.APPROVAL,
        DispatchState.MERGE,
        DispatchState.COMPLETE,
    ]
    if dispatch.state in (DispatchState.ABORT, DispatchState.ERROR):
        return 100
    if dispatch.state not in order:
        return 0
    base = order.index(dispatch.state) * 100 // (len(order) - 1)
    if dispatch.state is DispatchState.MONITORING and dispatch.lines:
        done = sum(1 for line in dispatch.lines if line.status is not LineStatus.RUNNING)
        span = 100 // (len(order) - 1)
        return base + (done * span // len(dispatch.lines))
    return base


__all__ = [
    "Collaborators",
    "DispatchMachine",
    "MachineConfig",
    "cancel_planner_failed",
    "progress_pct",
]
