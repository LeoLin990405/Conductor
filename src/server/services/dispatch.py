"""Wiring and lifecycle for auto-dispatch runs.

Two adapter sets, chosen explicitly rather than by sniffing the host:

``dev`` (the default) uses the deterministic doubles in
``src.tools.auto_dispatch.dev`` — the impact-map, skills-hub and memory-p0
dependencies are not merged, so a dispatch plans, runs, reviews and gates
without reaching for any of them.

``live`` uses the real adapters: the Claude Code planner, the quota-aware selector,
the headless launch script, the log monitor, native-CLI cross-review and the
git-backed merger. The approval gate and the dispatch row are the same in
both, because those are the parts that must never be faked.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import os
import re
import uuid
from dataclasses import dataclass, field, replace
from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from src.server.services.dispatch_assign import build_assigner
from src.tools.auto_dispatch.approval import default_proposed_dir
from src.tools.auto_dispatch.auto_merge import auto_approve_enabled
from src.server.services.autonomy.service_roles import (
    ServiceChain,
    instrument_planner,
    instrument_reviewer,
    instrument_selector,
)
from src.server.database.dispatch import (
    create_dispatch,
    get_dispatch,
    list_dispatches,
    pending_approval,
    save_dispatch,
)
from src.server.database.dispatch import path_claim_lock as db_path_claim_lock
from src.tools.auto_dispatch import (
    Collaborators,
    Dispatch,
    DispatchMachine,
    DispatchState,
    FileApprovalGate,
    InMemoryOutcomeStore,
    LLMPlanner,
    LogMonitor,
    MachineConfig,
    MemoryP0OutcomeStore,
    QuotaAwareModelSelector,
    ScriptLauncher,
    progress_pct,
)
from src.conductor_cli import activity
from src.tools.auto_dispatch.busy import BusyProxy
from src.tools.auto_dispatch.drain import DrainAwareLauncher
from src.tools.auto_dispatch.dev import (
    AlwaysPassReviewer,
    FixedPlanner,
    NoopMerger,
    RecordingLauncher,
    RoundRobinSelector,
    ScriptedMonitor,
)
from src.tools.auto_dispatch.claude_planner import (
    DEFAULT_PLANNER_BIN,
    DEFAULT_PLANNER_MODEL,
    DEFAULT_PLANNER_TIMEOUT_SECONDS,
    ClaudeCodeComplete,
)
from src.tools.auto_dispatch.execution import CrossReviewer, GitMerger, branch_evidence
from src.tools.auto_dispatch.gates import DEFAULT_RISK_GLOBS, GateRunner, GateSettings
from src.tools.auto_dispatch.outcomes import DispatchRowMemoryPort
from src.tools.auto_dispatch.path_ownership import PathOwnership
from src.tools.auto_dispatch.protocols import ApprovalDecision, DispatchStore
from src.tools.auto_dispatch.headroom_codexbar import CodexBarHeadroom, PlanStatusCache
from src.tools.auto_dispatch.selection import (
    HeadroomPort,
    MostConstrainedHeadroom,
    SnapshotOrUnlimitedHeadroom,
)

logger = logging.getLogger(__name__)

#: The dispatch whose settings a synchronous adapter factory is building for.
#: ``_planner_complete`` and ``_gate_runner`` keep their long-standing no-arg
#: (or user-id-only) signatures -- tests inject them directly -- while still
#: seeing the workspace snapshot of the run being built.
_ACTIVE_DISPATCH: ContextVar[Dispatch | None] = ContextVar(
    "dispatch_active_row", default=None
)

_TERMINAL = frozenset({"COMPLETE", "ABORT", "ERROR"})
#: Gates keys the service owns (L2). A drive save or a Stage-Gate root reset
#: that does not carry one of them must not drop it from the row.
_SERVICE_GATES = (
    "allowed_workers",
    "delivery",
    "planning_restarts",
    "workspace_remote",
    # N86: the worker a person reassigned a line to (human_pin.HUMAN_PINS).
    "human_pins",
    # N80 L4: app claims on dispatch lines (app_lines.GATE_KEY).
    "app_line_claims",
)
#: How many dispatch ids the wakeup remembers as already checked for a
#: stranded plan; past this the oldest half is forgotten.
_RESTART_CHECKED_LIMIT = 4096
#: A dispatch restarted while planning is re-planned from its goal at most
#: this many times before it is left parked for a person.
_MAX_PLANNING_RESTARTS = 3
#: States ``cancel`` ends without the parked-line rules (L2): nothing has
#: launched yet, or the merge after approval is under way.
_UNPARKED_CANCEL = frozenset(
    {DispatchState.SUBMITTED, DispatchState.PLANNING, DispatchState.MERGE}
)
# A forced cancel rescans for launches that slipped past the epoch bump.
_LATE_LAUNCH_SCANS = 10
_LATE_LAUNCH_SCAN_SECONDS = 0.25
_HUMAN_REQUEST_AUDIT = frozenset(
    {"human_reassign", "human_unblock", "human_adjudicate", "human_recover"}
)
_PHASE_FIELDS = ("state", "message", "integrate_branch", "approval_token", "spec_ref")
_LINE_FIELDS = (
    "name",
    "task",
    "assigned_model",
    "branch",
    "attempts",
    "status",
    "current_run",
    "failure_reason",
    "blockers",
    "log_tail",
    "assignment_audit",
    "depends_on",
    "write_scope",
    "declared_paths",
    "acceptance",
    "risk_bearing",
    # A replan moves an atomic group onto the replacement lines. Without this
    # field, merge_drive keeps the partner pointing at the replanned parent
    # and G5 stays closed after the row is reloaded.
    "atomic_with",
    "app_claimable",
)


def merge_drive(fresh: Dispatch, before: Dispatch, after: Dispatch) -> Dispatch:
    """Copy onto ``fresh`` only the fields ``after`` changed since ``before``.

    A concurrent writer may have changed another line. Those fields stay.
    ``wakeup_retry`` belongs to recovery, not to the drive, so the fresh value
    stays even when the local snapshot never saw it. A result for a line whose
    ``(current_run, attempts, author)`` no longer matches the row is discarded,
    including a gate snapshot from that attempt. The only record is ``stale_result``.
    """
    original_gates = copy.deepcopy(fresh.gates) if isinstance(fresh.gates, dict) else {}
    stale = _stale_line_ids(fresh, before, after)
    if stale and _result_is_only_stale(before, after, stale):
        _note_stale_result(fresh)
        return fresh
    for name in _PHASE_FIELDS:
        if getattr(after, name) != getattr(before, name):
            setattr(fresh, name, getattr(after, name))
    _merge_lines(fresh, before, after, skip=stale)
    _merge_outcomes(fresh, before, after, skip=stale)
    preserved = _line_snapshots(fresh, stale)
    fresh.gates = _merge_value(fresh.gates, before.gates, after.gates)
    if not isinstance(fresh.gates, dict):
        fresh.gates = {}
    _restore_unowned_gates(fresh, original_gates)
    _merge_scheduler_lists(fresh, before, after, original_gates)
    _restore_line_snapshots(fresh, preserved)
    if stale:
        _note_stale_result(fresh)
    return fresh


def _planning_restarts(dispatch: Dispatch) -> int:
    gates = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    try:
        return max(0, int(gates.get("planning_restarts") or 0))
    except (TypeError, ValueError):
        return 0


def _stranded_in_planning(dispatch: Dispatch) -> bool:
    """A row a restart left with no plan: re-planning from the goal is safe.

    No lines were ever saved, so nothing launched and no branch exists. A
    PLANNING row with a spec reference is not stranded: the planner reads the
    archived plan back. The restart count bounds the loop.
    """
    from src.tools.auto_dispatch.gates import RESUME_SPEC_MISSING

    if dispatch.lines or _planning_restarts(dispatch) >= _MAX_PLANNING_RESTARTS:
        return False
    if dispatch.state is DispatchState.PLANNING:
        return not (dispatch.spec_ref or "").strip()
    return (
        dispatch.state is DispatchState.REVIEW
        and (dispatch.message or "").strip() == RESUME_SPEC_MISSING
    )


def _keep_service_gates(current: Dispatch, updated: Dispatch) -> None:
    """Put back a service-owned gates key that a write dropped (L2).

    A live drive's first Stage-Gate snapshot replaces ``gates`` wholesale, and
    a drive merge then reads that as a delete. The service owns these keys --
    the worker restriction, the delivery record, the planning restart count --
    and only ever sets them, so a write that lacks one keeps the stored value.
    """
    stored = current.gates if isinstance(current.gates, dict) else {}
    kept = {key: stored[key] for key in _SERVICE_GATES if key in stored}
    if not kept:
        return
    if not isinstance(updated.gates, dict):
        updated.gates = {}
    for key, value in kept.items():
        if key not in updated.gates:
            updated.gates[key] = copy.deepcopy(value)


def _restore_unowned_gates(fresh: Dispatch, original: dict[str, Any]) -> None:
    """Recovery owns ``wakeup_retry``. A drive save must not drop or replace it."""
    gates = fresh.gates if isinstance(fresh.gates, dict) else {}
    fresh.gates = gates
    if "wakeup_retry" in original:
        gates["wakeup_retry"] = copy.deepcopy(original["wakeup_retry"])
    else:
        gates.pop("wakeup_retry", None)


def _scheduler_dict(gates: object) -> dict[str, Any]:
    if not isinstance(gates, dict):
        return {}
    scheduler = gates.get("scheduler")
    if not isinstance(scheduler, dict):
        return {}
    return scheduler


def _as_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    return []


def _list_id(item: object) -> tuple[str, object]:
    if isinstance(item, dict) and item.get("id"):
        return ("id", item.get("id"))
    if isinstance(item, str):
        return ("str", item)
    return ("value", repr(item))


def _merge_id_list(fresh: list[Any], before: list[Any], after: list[Any]) -> list[Any]:
    """Apply the drive's additions and removals onto the fresh list.

    Items only the fresh row has stay. A stale local list does not replace them.
    """
    before_ids = {_list_id(item) for item in before}
    after_ids = {_list_id(item) for item in after}
    removed = before_ids - after_ids
    kept = [item for item in fresh if _list_id(item) not in removed]
    present = {_list_id(item) for item in kept}
    for item in after:
        identity = _list_id(item)
        if identity in before_ids or identity in present:
            continue
        kept.append(copy.deepcopy(item))
        present.add(identity)
    return kept


def _merge_scheduler_lists(
    fresh: Dispatch,
    before: Dispatch,
    after: Dispatch,
    original: dict[str, Any],
) -> None:
    """``pending`` and ``handled`` take the drive's edits, not a wholesale copy."""
    gates = fresh.gates if isinstance(fresh.gates, dict) else {}
    fresh.gates = gates
    original_scheduler = _scheduler_dict(original)
    before_scheduler = _scheduler_dict(before.gates)
    after_scheduler = _scheduler_dict(after.gates)
    existing = gates.get("scheduler")
    created = not isinstance(existing, dict)
    scheduler: dict[str, Any] = existing if isinstance(existing, dict) else {}
    if created:
        gates["scheduler"] = scheduler
    for key in ("pending", "handled"):
        merged = _merge_id_list(
            _as_list(original_scheduler.get(key)),
            _as_list(before_scheduler.get(key)),
            _as_list(after_scheduler.get(key)),
        )
        seen = (
            key in original_scheduler
            or key in before_scheduler
            or key in after_scheduler
        )
        if merged or seen:
            scheduler[key] = merged
        else:
            scheduler.pop(key, None)
    if created and not scheduler:
        gates.pop("scheduler", None)


def _line_identity(line: Any) -> tuple[object, object, str]:
    return (line.current_run, line.attempts, line.assigned_model or "")


def _line_changed(before_line: Any, after_line: Any) -> bool:
    return any(
        getattr(after_line, name) != getattr(before_line, name) for name in _LINE_FIELDS
    )


def _gate_lines(dispatch: Dispatch) -> dict[str, Any]:
    gates = dispatch.gates if isinstance(dispatch.gates, dict) else None
    if not isinstance(gates, dict):
        return {}
    lines = gates.get("lines")
    if not isinstance(lines, dict):
        return {}
    return lines


def _snapshot_of(dispatch: Dispatch, line_id: str) -> Any:
    lines = _gate_lines(dispatch)
    if line_id not in lines:
        return None
    return lines[line_id]


def _stale_line_ids(fresh: Dispatch, before: Dispatch, after: Dispatch) -> set[str]:
    """Lines whose live identity moved while this drive still had a result.

    A result is a line-field change, an outcome, or a gate snapshot. A line
    the drive did not touch stays writable by a concurrent reassign.
    """
    before_by = {line.id: line for line in before.lines}
    after_by = {line.id: line for line in after.lines}
    fresh_by = {line.id: line for line in fresh.lines}
    stale: set[str] = set()
    for line_id, before_line in before_by.items():
        fresh_line = fresh_by.get(line_id)
        after_line = after_by.get(line_id)
        if fresh_line is None or after_line is None or fresh_line is after_line:
            continue
        if _line_identity(fresh_line) == _line_identity(before_line):
            continue
        outcome_changed = before.outcomes.get(line_id) != after.outcomes.get(line_id)
        snapshot_changed = _snapshot_of(before, line_id) != _snapshot_of(after, line_id)
        if _line_changed(before_line, after_line) or outcome_changed or snapshot_changed:
            stale.add(line_id)
    return stale


def _result_is_only_stale(before: Dispatch, after: Dispatch, stale: set[str]) -> bool:
    before_by = {line.id: line for line in before.lines}
    changed: set[str] = set()
    for line in after.lines:
        previous = before_by.get(line.id)
        if previous is None or _line_changed(previous, line):
            changed.add(line.id)
    for key, value in after.outcomes.items():
        if before.outcomes.get(key) != value:
            changed.add(key)
    for key in before.outcomes:
        if key not in after.outcomes:
            changed.add(key)
    watched = set(_gate_lines(before)) | set(_gate_lines(after))
    for line_id in watched:
        if _snapshot_of(before, line_id) != _snapshot_of(after, line_id):
            changed.add(line_id)
    return bool(changed) and changed <= stale


def _line_snapshots(dispatch: Dispatch, line_ids: set[str]) -> dict[str, Any]:
    if not line_ids or not isinstance(dispatch.gates, dict):
        return {}
    lines = dispatch.gates.get("lines")
    if not isinstance(lines, dict):
        return {}
    return {
        line_id: copy.deepcopy(lines[line_id])
        for line_id in line_ids
        if line_id in lines
    }


def _restore_line_snapshots(dispatch: Dispatch, saved: dict[str, Any]) -> None:
    if not saved or not isinstance(dispatch.gates, dict):
        return
    lines = dispatch.gates.get("lines")
    if not isinstance(lines, dict):
        return
    for line_id, snap in saved.items():
        lines[line_id] = snap


def _note_stale_result(dispatch: Dispatch) -> None:
    """Append ``{kind: stale_result}`` on the scheduler audit and nowhere else."""
    gates = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    dispatch.gates = gates
    scheduler = gates.get("scheduler")
    if not isinstance(scheduler, dict):
        scheduler = {}
        gates["scheduler"] = scheduler
    audit = scheduler.get("audit")
    if not isinstance(audit, list):
        audit = []
        scheduler["audit"] = audit
    audit.append({"kind": "stale_result"})


def _merge_lines(
    fresh: Dispatch,
    before: Dispatch,
    after: Dispatch,
    *,
    skip: set[str] | None = None,
) -> None:
    skipped = skip or set()
    before_by = {line.id: line for line in before.lines}
    after_by = {line.id: line for line in after.lines}
    fresh_by = {line.id: line for line in fresh.lines}
    for line_id, after_line in after_by.items():
        if line_id in skipped:
            continue
        before_line = before_by.get(line_id)
        fresh_line = fresh_by.get(line_id)
        if fresh_line is None:
            fresh.lines.append(copy.deepcopy(after_line))
            continue
        if fresh_line is after_line or before_line is None:
            continue
        _copy_changed_line(fresh_line, before_line, after_line)
    removed = [
        line_id
        for line_id in before_by
        if line_id not in after_by and line_id not in skipped
    ]
    if removed:
        fresh.lines[:] = [line for line in fresh.lines if line.id not in removed]


def _copy_changed_line(fresh_line: Any, before_line: Any, after_line: Any) -> None:
    for name in _LINE_FIELDS:
        old = getattr(before_line, name)
        new = getattr(after_line, name)
        if new != old:
            setattr(fresh_line, name, copy.deepcopy(new))


def _merge_outcomes(
    fresh: Dispatch,
    before: Dispatch,
    after: Dispatch,
    *,
    skip: set[str] | None = None,
) -> None:
    skipped = skip or set()
    for key, value in after.outcomes.items():
        if key in skipped:
            continue
        if before.outcomes.get(key) != value:
            fresh.outcomes[key] = value
    for key in list(before.outcomes):
        if key in skipped:
            continue
        if key not in after.outcomes and fresh.outcomes.get(key) == before.outcomes.get(key):
            fresh.outcomes.pop(key, None)


def _merge_value(fresh: Any, before: Any, after: Any) -> Any:
    if isinstance(after, dict) and isinstance(before, dict) and isinstance(fresh, dict):
        return _merge_dict(fresh, before, after)
    if after != before:
        return copy.deepcopy(after)
    return fresh


def _merge_dict(fresh: dict[str, Any], before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    result = dict(fresh)
    for key, value in after.items():
        old = before.get(key, _MISSING)
        current = result.get(key, _MISSING)
        if old is _MISSING:
            if current is _MISSING:
                result[key] = copy.deepcopy(value)
            elif isinstance(value, dict) and isinstance(current, dict):
                result[key] = _merge_dict(current, {}, value)
            elif current != value:
                result[key] = copy.deepcopy(value)
            continue
        if value != old:
            if isinstance(value, dict) and isinstance(old, dict) and isinstance(current, dict):
                result[key] = _merge_dict(current, old, value)
            else:
                result[key] = copy.deepcopy(value)
    for key, old in before.items():
        if key not in after and result.get(key) == old:
            result.pop(key, None)
    return result


class _MISSING:
    pass


def _baseline_epoch(before: Dispatch | None, proposed: Dispatch) -> int:
    """The epoch a legacy ``save`` still has to match. A local bump is not it."""
    from src.tools.auto_dispatch.gates import drive_epoch

    if before is not None:
        return drive_epoch(before)
    return drive_epoch(proposed)


class _MergingStore:
    """A drive's saves apply a delta onto the row that is in the store now."""

    def __init__(
        self,
        service: DispatchService,
        inner: Any,
        baseline: dict[str, Dispatch],
    ) -> None:
        self._service = service
        self._inner = inner
        self._baseline = baseline

    async def save(self, dispatch: Dispatch) -> Dispatch:
        """Merge this drive object. Callers that have an epoch use ``drive_mutation``."""
        return await self.drive_mutation(
            dispatch.id,
            _baseline_epoch(self._baseline.get(dispatch.id), dispatch),
            lambda fresh: dispatch,
        )

    async def drive_mutation(
        self,
        dispatch_id: str,
        epoch0: int,
        fn: Callable[[Dispatch], Dispatch],
    ) -> Dispatch:
        """Epoch-check, then merge a drive write onto the current row."""

        def wrapped(fresh: Dispatch) -> Dispatch:
            proposed = fn(fresh)
            if proposed is None:
                proposed = fresh
            if proposed is fresh:
                return fresh
            before = self._baseline.get(dispatch_id)
            if before is None:
                return proposed
            return merge_drive(fresh, before, proposed)

        saved = await self._service.drive_mutation(dispatch_id, epoch0, wrapped)
        if saved is not None:
            # The saved row, not the pre-save local object. The local object
            # can lack a concurrent key, and the next diff would delete it.
            self._baseline[dispatch_id] = copy.deepcopy(saved)
        return saved

    async def apply_mutation(
        self,
        dispatch_id: str,
        fn: Callable[[Dispatch], Dispatch | Awaitable[Dispatch]],
    ) -> Dispatch:
        """A fresh-row mutation that must not be merged as this drive's delta."""
        return await self._service.apply_mutation(dispatch_id, fn)

    async def get(self, dispatch_id: str) -> Dispatch | None:
        return await self._inner.get(dispatch_id)

    async def create(self, dispatch: Dispatch) -> Dispatch:
        return await self._inner.create(dispatch)

    async def list(self, **kwargs: Any) -> list[Dispatch]:
        return await self._inner.list(**kwargs)

    async def pending_approval(self, user_id: str | None = None) -> list[Dispatch]:
        return await self._inner.pending_approval(user_id)

    def __getattr__(self, name: str) -> Any:
        # Optional store capabilities (path_claim_lock) pass through.
        if name == "path_claim_lock":
            return getattr(self._inner, name)
        raise AttributeError(name)

PLANNER_CLAUDE_CODE = "claude-code"
PLANNER_LLM = "llm"
#: A worker name a dispatch may be restricted to (``--workers``).
_WORKER_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

DEFAULT_RUNS_DIR = Path.home() / ".conductor" / "dispatch" / "runs"
# One native review turn. Codex on a large risk-bearing line needs more than five
# minutes (N74); CONDUCTOR_DISPATCH_REVIEW_TIMEOUT_SECONDS overrides it.
REVIEW_TIMEOUT_SECONDS = 900.0


@dataclass(frozen=True)
class DispatchSettings:
    """Where a dispatch keeps its runs, its pending summaries and its repo."""

    adapters: str = "dev"
    runs_dir: Path = DEFAULT_RUNS_DIR
    repo_root: Path = Path.cwd()
    # Resolved per instance under the Conductor state dir, never Path.cwd().
    proposed_dir: Path = field(default_factory=default_proposed_dir)
    launch_script: Path | None = None
    skills_hub: str = ""
    impact_map: str | None = None
    stall_seconds: float = 60.0
    timeout_seconds: float = 600.0
    poll_interval_seconds: float = 10.0
    gates_mode: str = "on"
    g2_command: str | None = None
    g2_timeout_seconds: float = 900.0
    g5_command: str | None = None
    g5_timeout_seconds: float = 3600.0
    review_timeout_seconds: float = REVIEW_TIMEOUT_SECONDS
    risk_globs: tuple[str, ...] = DEFAULT_RISK_GLOBS
    max_g2_fixes: int = 2
    max_replans: int = 1
    # The live planner backend: Claude Code in repo_root, or the LLMService call.
    # Dev adapters ignore it and keep FixedPlanner.
    planner: str = PLANNER_CLAUDE_CODE
    planner_bin: str = DEFAULT_PLANNER_BIN
    planner_model: str = DEFAULT_PLANNER_MODEL
    planner_timeout_seconds: float = DEFAULT_PLANNER_TIMEOUT_SECONDS
    # Independent lines of one dispatch that may run at once (N31). The
    # launcher still caps each provider (CONDUCTOR_DISPATCH_PROVIDER_CAPACITY).
    max_parallel_lines: int = 4
    #: A workspace may override the backend-wide checkout. These two are the
    #: workspace row's ``sandbox_policy`` and ``default_worker`` copied onto
    #: the dispatch at start; empty means "use the backend setting".
    sandbox_policy: str = ""
    default_worker: str = ""
    #: N30: the per-attempt cost ledger in ``gates["cost"]`` (default on; the
    #: machine reads the same env through ``cost.ledger_enabled``) and the soft
    #: cost input to the WORK selector (default off; off keeps today's order).
    cost_ledger: bool = True
    cost_selector: bool = False
    cost_weight: float = 0.15
    cost_min_merged: int = 3

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> DispatchSettings:
        source = os.environ if env is None else env
        defaults = cls()
        return cls(
            adapters=source.get("CONDUCTOR_DISPATCH_ADAPTERS", defaults.adapters),
            runs_dir=Path(source.get("CONDUCTOR_DISPATCH_RUNS_DIR", str(defaults.runs_dir))),
            repo_root=Path(source.get("CONDUCTOR_DISPATCH_REPO", str(defaults.repo_root))),
            proposed_dir=default_proposed_dir(source),
            launch_script=(
                Path(source["CONDUCTOR_DISPATCH_LAUNCH_SCRIPT"])
                if source.get("CONDUCTOR_DISPATCH_LAUNCH_SCRIPT")
                else None
            ),
            skills_hub=source.get("CONDUCTOR_DISPATCH_SKILLS_HUB", ""),
            impact_map=source.get("CONDUCTOR_DISPATCH_IMPACT_MAP") or None,
            stall_seconds=float(
                source.get("CONDUCTOR_DISPATCH_STALL_SECONDS", defaults.stall_seconds)
            ),
            timeout_seconds=float(
                source.get("CONDUCTOR_DISPATCH_TIMEOUT_SECONDS", defaults.timeout_seconds)
            ),
            poll_interval_seconds=float(
                source.get(
                    "CONDUCTOR_DISPATCH_POLL_SECONDS", defaults.poll_interval_seconds
                )
            ),
            gates_mode=source.get("CONDUCTOR_DISPATCH_GATES", defaults.gates_mode),
            g2_command=_blank_to_none(source.get("CONDUCTOR_DISPATCH_G2_COMMAND")),
            g2_timeout_seconds=float(
                source.get(
                    "CONDUCTOR_DISPATCH_G2_TIMEOUT_SECONDS", defaults.g2_timeout_seconds
                )
            ),
            g5_command=_blank_to_none(source.get("CONDUCTOR_DISPATCH_G5_COMMAND")),
            g5_timeout_seconds=float(
                source.get(
                    "CONDUCTOR_DISPATCH_G5_TIMEOUT_SECONDS", defaults.g5_timeout_seconds
                )
            ),
            review_timeout_seconds=float(
                source.get(
                    "CONDUCTOR_DISPATCH_REVIEW_TIMEOUT_SECONDS",
                    defaults.review_timeout_seconds,
                )
            ),
            risk_globs=_risk_globs(
                source.get("CONDUCTOR_DISPATCH_RISK_GLOBS"), defaults.risk_globs
            ),
            max_g2_fixes=int(
                source.get("CONDUCTOR_DISPATCH_MAX_G2_FIXES", defaults.max_g2_fixes)
            ),
            max_replans=int(
                source.get("CONDUCTOR_DISPATCH_MAX_REPLANS", defaults.max_replans)
            ),
            planner=_planner_choice(
                _registry_planner_cli() or source.get("CONDUCTOR_DISPATCH_PLANNER")
            ),
            planner_bin=(
                _blank_to_none(source.get("CONDUCTOR_DISPATCH_PLANNER_BIN"))
                or defaults.planner_bin
            ),
            planner_model=(
                _blank_to_none(source.get("CONDUCTOR_DISPATCH_PLANNER_MODEL"))
                or defaults.planner_model
            ),
            planner_timeout_seconds=float(
                source.get(
                    "CONDUCTOR_DISPATCH_PLANNER_TIMEOUT",
                    defaults.planner_timeout_seconds,
                )
            ),
            max_parallel_lines=_positive_int(
                source.get("CONDUCTOR_DISPATCH_MAX_PARALLEL_LINES"),
                defaults.max_parallel_lines,
            ),
            sandbox_policy=source.get(
                "CONDUCTOR_DISPATCH_SANDBOX_POLICY", defaults.sandbox_policy
            ),
            default_worker=source.get(
                "CONDUCTOR_DISPATCH_DEFAULT_WORKER", defaults.default_worker
            ),
            cost_ledger=_on_off(source.get("CONDUCTOR_DISPATCH_COST_LEDGER"), True),
            cost_selector=_on_off(source.get("CONDUCTOR_DISPATCH_COST_SELECTOR"), False),
            cost_weight=_cost_weight(source.get("CONDUCTOR_DISPATCH_COST_WEIGHT")),
            cost_min_merged=_positive_int(
                source.get("CONDUCTOR_DISPATCH_COST_MIN_MERGED"),
                defaults.cost_min_merged,
            ),
        )


PLANNER_CHOICES = (PLANNER_CLAUDE_CODE, PLANNER_LLM)


def _registry_planner_cli() -> str | None:
    """R6 L1: the stored role registry's planner CLI; None keeps the env fallback."""
    from src.server.services.autonomy.role_types import ROLE_PLANNER, stored_registry

    registry = stored_registry()
    if registry is None:
        return None
    value = registry.entry(ROLE_PLANNER).extra.get("planner_cli")
    return str(value).strip().lower() or None if value else None


def _planner_choice(raw: str | None) -> str:
    """``CONDUCTOR_DISPATCH_PLANNER``: unset or blank is Claude Code; a typo is refused."""
    value = (raw or "").strip().lower()
    if not value:
        return PLANNER_CLAUDE_CODE
    if value not in PLANNER_CHOICES:
        raise ValueError(
            "CONDUCTOR_DISPATCH_PLANNER must be one of "
            + ", ".join(PLANNER_CHOICES)
        )
    return value


class DbDispatchStore:
    """The ``DispatchStore`` protocol over the ``dispatches`` table."""

    async def create(self, dispatch: Dispatch) -> Dispatch:
        return await create_dispatch(dispatch)

    async def save(self, dispatch: Dispatch) -> Dispatch:
        return await save_dispatch(dispatch)

    async def get(self, dispatch_id: str) -> Dispatch | None:
        return await get_dispatch(dispatch_id)

    async def list(
        self,
        *,
        user_id: str | None = None,
        states: list[str] | None = None,
        limit: int = 50,
    ) -> list[Dispatch]:
        return await list_dispatches(user_id=user_id, states=states, limit=limit)

    async def pending_approval(self, user_id: str | None = None) -> list[Dispatch]:
        return await pending_approval(user_id)

    def path_claim_lock(self) -> AbstractAsyncContextManager[None]:
        """The database lock path ownership holds across check and assign."""
        return db_path_claim_lock()


def default_headroom() -> HeadroomPort:
    """The live quota reader when the service is not handed a port.

    ``CodexBarHeadroom`` reads the CodexBar snapshot ``conductor usage``
    writes (one local file, no CLI call, no network) and reports each Worker's
    remaining window as a fraction, which is what a large task's ranking needs.
    The native-runtime usage snapshot is asked too, because it alone sees a
    spent DeepSeek balance; it answers 1.0 when that reader is not configured.
    The more exhausted reading wins. A missing or stale CodexBar file answers
    1.0 for every Worker, so a host that never wrote one behaves as unlimited.

    Both readers grade a full plan with credits left as low, not spent, and
    both report ``plan_status``; ``MostConstrainedHeadroom`` merges those so
    N63's review rule sees the full plan and ``CONDUCTOR_DISPATCH_ALLOW_PAID_CREDITS``
    alone decides whether review spends the credits. Before the merge the
    combined port had no ``plan_status`` and CodexBar read the full window as
    0.0, so with the flag on codex still never reviewed.
    """
    return MostConstrainedHeadroom(CodexBarHeadroom(), SnapshotOrUnlimitedHeadroom())


def _disabled_by_operator(model: str) -> bool:
    """True when ``native_runtime_routing.disabled_providers`` switches this CLI off."""
    from src.server.services.native_runtime.registry import disabled_provider_reason

    return disabled_provider_reason(model) is not None


class NativeReviewBackend:
    """Runs a cross-review as one read-only native-CLI turn for that model.

    The adapter comes only from ``get_read_only_adapter``, which refuses a CLI with no
    read-only mode instead of running it writable; the selector already keeps those
    models out of the review role, so a refusal here means the two disagree. The same
    holds for a CLI the operator disabled: NativeAgent honours that list, so a reviewer
    must not be a way around it.
    """

    def __init__(
        self,
        *,
        cwd: Path | None = None,
        timeout_seconds: float = REVIEW_TIMEOUT_SECONDS,
        scratch_dir: Path | None = None,
    ) -> None:
        self._cwd = str(cwd) if cwd else None
        self._timeout_seconds = timeout_seconds
        # Base for a per-run writable scratch outside the repo, so a reviewer can
        # run tests (temp dirs, uv/pytest caches) while the repo stays read-only.
        self._scratch_dir = scratch_dir

    async def review(self, model: str, prompt: str) -> str:
        from src.llms.native_cli import NativeCliUnavailable, get_read_only_adapter

        if _disabled_by_operator(model):
            raise NativeCliUnavailable(f"{model!r} is disabled by operator policy")
        adapter = get_read_only_adapter(
            model, cwd=self._cwd, timeout_seconds=self._timeout_seconds
        )
        from contextlib import ExitStack

        from src.llms.native_cli.review_scratch import review_scratch, scratch_prompt_note

        with ExitStack() as stack:
            # Prepended: the review prompt must end with its reply-contract reminder.
            note = ""
            if self._scratch_dir is not None and self._cwd:
                try:
                    scratch = stack.enter_context(review_scratch(self._scratch_dir, self._cwd))
                except ValueError as exc:
                    logger.warning("review scratch refused, reviewing without one: %s", exc)
                else:
                    note = scratch_prompt_note(scratch) + "\n\n"
            result = await adapter.run(note + prompt)
        return result.text


class DispatchCoordinator:
    """Process-wide locks, drive registry, and wakeup lease.

    HTTP, the scheduled drive, and wakeup share one instance. A service built
    without one gets a private coordinator so unit tests do not share a registry.
    """

    def __init__(
        self,
        *,
        store: DispatchStore | None = None,
        settings: DispatchSettings | None = None,
        gate: FileApprovalGate | None = None,
        llm_service: Any = None,
        headroom: HeadroomPort | None = None,
        machine_factory: Callable[[Dispatch], DispatchMachine] | None = None,
        wakeup_lease: Any = None,
        delivery: Any = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.gate = gate
        self.llm_service = llm_service
        self.headroom = headroom
        self.machine_factory = machine_factory
        self.wakeup_lease = wakeup_lease
        self.delivery = delivery
        self._drive_locks: dict[str, asyncio.Lock] = {}
        self._body_locks: dict[str, asyncio.Lock] = {}
        self._delivery_locks: dict[str, asyncio.Lock] = {}
        # Owner tokens. A value is present only while that drive holds the body lock,
        # or while a human call has reserved the slot and the drive has not entered yet.
        self.active_drives: dict[str, object] = {}
        self.claims: set[object] = set()

    def drive_lock(self, dispatch_id: str) -> asyncio.Lock:
        return self._drive_locks.setdefault(dispatch_id, asyncio.Lock())

    def body_lock(self, dispatch_id: str) -> asyncio.Lock:
        return self._body_locks.setdefault(dispatch_id, asyncio.Lock())

    def delivery_lock(self, dispatch_id: str) -> asyncio.Lock:
        return self._delivery_locks.setdefault(dispatch_id, asyncio.Lock())

    def release_if_owner(self, dispatch_id: str, token: object) -> None:
        """Drop a reservation only when it is still this token.

        A done callback uses this. A primary owner with a different token stays.
        """
        if self.active_drives.get(dispatch_id) is token:
            self.active_drives.pop(dispatch_id, None)

    def service(self) -> DispatchService:
        """A service that uses this coordinator's locks, registry, and lease."""
        kwargs: dict[str, Any] = {"coordinator": self}
        if self.settings is not None:
            kwargs["settings"] = self.settings
        if self.store is not None:
            kwargs["store"] = self.store
        if self.gate is not None:
            kwargs["gate"] = self.gate
        if self.llm_service is not None:
            kwargs["llm_service"] = self.llm_service
        if self.headroom is not None:
            kwargs["headroom"] = self.headroom
        if self.machine_factory is not None:
            kwargs["machine_factory"] = self.machine_factory
        if self.wakeup_lease is not None:
            kwargs["wakeup_lease"] = self.wakeup_lease
        if self.delivery is not None:
            kwargs["delivery"] = self.delivery
        return DispatchService(**kwargs)


class DispatchService:
    """Starts, watches and gates dispatches; the only writer of dispatch rows."""

    def __init__(
        self,
        settings: DispatchSettings | None = None,
        *,
        store: DispatchStore | None = None,
        gate: FileApprovalGate | None = None,
        llm_service: Any = None,
        headroom: HeadroomPort | None = None,
        machine_factory: Callable[[Dispatch], DispatchMachine] | None = None,
        wakeup_lease: Any = None,
        coordinator: DispatchCoordinator | None = None,
        delivery: Any = None,
    ) -> None:
        self._settings = settings or DispatchSettings.from_env()
        #: L2: what pushes an approved COMPLETE dispatch and opens its PR.
        #: None builds the git/GitHub delivery for live adapters (unless
        #: ``CONDUCTOR_DISPATCH_DELIVERY=off``); dev has no delivery.
        self._delivery = delivery
        self._store = store or DbDispatchStore()
        #: N30: built on first use when the cost selector flag is on.
        self._cost_stats: Any = None
        self._gate = gate or FileApprovalGate(self._settings.proposed_dir)
        self._llm = llm_service
        # None means default_headroom(): the CodexBar snapshot port (plus the
        # native usage snapshot). An explicit port, including
        # UnlimitedHeadroom, is used as given.
        self._headroom = headroom
        # Tests inject the machine. Production builds it from the settings.
        self._machine_factory = machine_factory
        if coordinator is None:
            coordinator = DispatchCoordinator(wakeup_lease=wakeup_lease)
        elif wakeup_lease is not None and coordinator.wakeup_lease is None:
            coordinator.wakeup_lease = wakeup_lease
        self._coordinator = coordinator
        # Strong refs for saves. A shielded task with no other owner can be collected.
        self._save_tasks: set[asyncio.Task[Any]] = set()
        self._wakeup: Any = None
        #: Rows this service's wakeup has checked for a stranded plan (L2),
        #: oldest first; bounded by ``_RESTART_CHECKED_LIMIT``.
        self._restart_checked: dict[str, None] = {}
        # Wakeup retry stamps share this service's mutation. A second service
        # that reuses the store keeps the first service's locks.
        if getattr(self._store, "apply_mutation", None) is None:
            self._store.apply_mutation = self.apply_mutation  # type: ignore[attr-defined]
        if getattr(self._store, "drive_mutation", None) is None:
            self._store.drive_mutation = self.drive_mutation  # type: ignore[attr-defined]
        # An invalid CONDUCTOR_ROLE_POLICY_FILE fails here, not at the first
        # live selection and not by silently using the built-in roster.
        from src.server.services.autonomy.roles import load_role_policy

        self._policy = load_role_policy()

    @property
    def active_drives(self) -> dict[str, object]:
        """Owner tokens from the shared coordinator. Not tasks, and not per service."""
        return self._coordinator.active_drives

    @property
    def _drive_locks(self) -> dict[str, asyncio.Lock]:
        """Write locks from the coordinator. Tests inspect the same objects."""
        return self._coordinator._drive_locks

    # -- reads ----------------------------------------------------------------

    async def get(self, dispatch_id: str) -> Dispatch | None:
        return await self._store.get(dispatch_id)

    async def list(
        self,
        *,
        user_id: str | None = None,
        states: list[str] | None = None,
        limit: int = 50,
    ) -> list[Dispatch]:
        return await self._store.list(user_id=user_id, states=states, limit=limit)

    async def active(self, user_id: str | None = None) -> list[Dispatch]:
        live = [state.value for state in DispatchState if state.value not in _TERMINAL]
        return await self._store.list(user_id=user_id, states=live, limit=100)

    async def pending_approval(self, user_id: str | None = None) -> list[Dispatch]:
        return await self._store.pending_approval(user_id)

    # -- writes ---------------------------------------------------------------

    async def start(
        self,
        goal: str,
        user_id: str,
        *,
        workspace: dict[str, Any] | None = None,
        workers: Any = None,
        source_ref: str | None = None,
        force: bool = False,
    ) -> Dispatch:
        """Create the row and schedule its drive.

        ``workspace`` is the resolved registry row (``workspace_repo`` from the
        service layer). Its repository, sandbox policy and default worker are
        copied onto the dispatch row before the first save: the drive and every
        recovery read the copy, so editing or deleting the registry later cannot
        move a run that is already executing.

        ``workers`` (L2) restricts which workers this dispatch may use: the
        planner, the work selector and ``reassign`` are all held to it. It is
        kept on the row under ``gates["allowed_workers"]``; empty allows every
        worker the role policy allows.

        ``source_ref`` (P0 dedup) names the ticket or issue the dispatch is
        for. In ``CONDUCTOR_DISPATCH_DEDUP_MODE=enforce`` an active dispatch of
        the same user for the same ref is returned instead of a new one,
        unless ``force``; shadow only records it.
        """
        allowed = self._allowed_workers_for_start(workers)
        selection = workspace or {}
        dispatch = Dispatch(
            id=uuid.uuid4().hex,
            goal=(goal or "").strip(),
            user_id=user_id,
            created_at=datetime.now(UTC).isoformat(),
            workspace_id=str(selection.get("workspace_id") or "").strip(),
            repo_root=str(
                selection.get("repo_root") or selection.get("path") or ""
            ).strip(),
            sandbox_policy=str(selection.get("sandbox_policy") or "").strip(),
            default_worker=str(selection.get("default_worker") or "").strip().lower(),
        )
        if not dispatch.goal:
            raise ValueError("A dispatch goal is required")
        if allowed:
            from src.tools.auto_dispatch.selection import ALLOWED_WORKERS

            dispatch.gates[ALLOWED_WORKERS] = list(allowed)
        remote = str(selection.get("git_remote") or "").strip()
        if remote:
            # L2: delivery pushes to the remote the run started with, not to
            # whatever the registry says later. Never a credential.
            from src.server.services.dispatch_delivery import WORKSPACE_REMOTE, public_remote

            if public_remote(remote) and not remote.startswith("-"):
                dispatch.gates[WORKSPACE_REMOTE] = public_remote(remote)
        from src.tools.auto_dispatch import p0_hooks

        # P0: the duplicate check and the create are one step (no ref: no lock).
        async with p0_hooks.start_lock(user_id, source_ref, self._store):
            if source_ref:
                existing = p0_hooks.start_duplicate(
                    dispatch, source_ref, await self.active(user_id), force=force
                )
                if existing is not None:
                    return existing
            created = await self._store.create(dispatch)
        await self._resume(created)
        return created

    def _allowed_workers_for_start(self, workers: Any) -> tuple[str, ...]:
        """The normalised restriction, or a ``ValueError`` naming the bad worker."""
        from src.tools.auto_dispatch.selection import normalise_workers

        allowed = normalise_workers(workers)
        if len(allowed) > 16:
            raise ValueError("A dispatch may name at most 16 workers")
        for name in allowed:
            if not _WORKER_NAME.fullmatch(name):
                raise ValueError(f"Invalid worker name: {name[:64]!r}")
            if not self._policy.may_write_code(name):
                raise ValueError(
                    f"{name} is reserved for planning or review and cannot be "
                    "a dispatch worker unless allow_reserved_code is set"
                )
        return allowed

    def _settings_for(self, dispatch: Dispatch | None) -> DispatchSettings:
        """The backend settings with the run's workspace snapshot applied.

        The machine, planner, gate runner and every recovery path call this
        rather than reading ``self._settings`` directly, so a dispatch selected
        in one workspace never executes in the backend's default checkout.
        """
        if dispatch is None:
            return self._settings
        overrides: dict[str, Any] = {}
        if dispatch.repo_root:
            overrides["repo_root"] = Path(dispatch.repo_root)
        if dispatch.sandbox_policy:
            overrides["sandbox_policy"] = dispatch.sandbox_policy
        if dispatch.default_worker:
            overrides["default_worker"] = dispatch.default_worker
        # The launch script is infrastructure, not part of the checkout the
        # dispatch was pointed at. A relative CONDUCTOR_DISPATCH_LAUNCH_SCRIPT
        # (the documented ``scripts/dispatch_launch.sh``) is resolved against
        # the backend's own repo before repo_root is replaced; otherwise the
        # Worker would look for the script inside a foreign workspace that may
        # not contain it.
        script = self._settings.launch_script
        if script is not None and not script.is_absolute():
            overrides["launch_script"] = (
                Path(self._settings.repo_root).expanduser() / script
            ).resolve(strict=False)
        if not overrides:
            return self._settings
        return replace(self._settings, **overrides)

    def _refuse_if_drive_active(self, dispatch_id: str) -> None:
        """Human writes wait until this dispatch's owner token is gone."""
        if self.active_drives.get(dispatch_id) is not None:
            raise ValueError("drive_active")

    async def approve(self, dispatch_id: str, note: str = "") -> Dispatch:
        def mutation(row: Dispatch) -> Dispatch:
            self._refuse_if_drive_active(dispatch_id)
            if row.state is not DispatchState.APPROVAL:
                raise ValueError(f"Dispatch {dispatch_id} is not awaiting approval")
            from src.tools.auto_dispatch.gates import bump_epoch

            bump_epoch(row)
            return row

        dispatch = await self.apply_mutation(dispatch_id, mutation, claim=True)
        try:
            decision = await self._gate.resolve_dispatch(dispatch_id, True, note)
            if decision is not ApprovalDecision.APPROVED:
                raise ValueError(f"Dispatch {dispatch_id} is not awaiting approval")
        except BaseException:
            await self._release_claim(dispatch_id)
            raise
        return await self._replace_claim_with_drive(dispatch)

    async def reject(
        self, dispatch_id: str, note: str = "", *, force: bool = False
    ) -> Dispatch:
        """Reject a dispatch at the gate, or end one that is parked (N36).

        At APPROVAL the gate decides and the integrate branch is moved aside,
        as before. A dispatch parked in REVIEW, LAUNCHING, MONITORING or
        ASSIGNED is cancelled instead: see ``cancel``.
        """
        from src.tools.auto_dispatch.planner_reason import is_planner_failed

        current = await self._require(dispatch_id)
        if current.state is not DispatchState.APPROVAL:
            if is_planner_failed(current):
                return await self.cancel_planner_failed(dispatch_id, note)
            return await self._end_parked(dispatch_id, note, force=force, verb="reject")

        def mutation(row: Dispatch) -> Dispatch:
            self._refuse_if_drive_active(dispatch_id)
            if row.state is not DispatchState.APPROVAL:
                raise ValueError(f"Dispatch {dispatch_id} is not awaiting approval")
            from src.tools.auto_dispatch.gates import bump_epoch

            bump_epoch(row)
            return row

        dispatch = await self.apply_mutation(dispatch_id, mutation, claim=True)
        try:
            decision = await self._gate.resolve_dispatch(dispatch_id, False, note)
            if decision is not ApprovalDecision.REJECTED:
                raise ValueError(f"Dispatch {dispatch_id} is not awaiting approval")
        except BaseException:
            await self._release_claim(dispatch_id)
            raise
        return await self._replace_claim_with_drive(dispatch)

    async def cancel(
        self, dispatch_id: str, note: str = "", *, force: bool = False
    ) -> Dispatch:
        """End a parked dispatch: release its lines, end the drive, keep the branches.

        A dispatch parked on a planner failure takes ``cancel_planner_failed``.
        APPROVAL is refused (reject it at the gate). A line whose run may still
        be out (RUNNING, a run id on the row, or a live launch in this process)
        refuses with ``line_running`` unless ``force``, which stops each such
        run through the launcher's cancel path first. A drive that owns the
        dispatch refuses with ``drive_active`` unless ``force``; the epoch bump
        then fences that drive off the row.
        """
        from src.tools.auto_dispatch.planner_reason import is_planner_failed

        current = await self._require(dispatch_id)
        if current.state is DispatchState.APPROVAL:
            raise ValueError(
                f"Dispatch {dispatch_id} is awaiting approval; reject it instead"
            )
        if is_planner_failed(current):
            return await self.cancel_planner_failed(dispatch_id, note)
        # L2: a dispatch that has not launched (SUBMITTED, PLANNING) or is
        # merging after approval ends the same way; there is no parked line
        # to check, but a drive that owns it still needs ``force``.
        return await self._end_parked(
            dispatch_id,
            note,
            force=force,
            verb="cancel",
            unparked=current.state in _UNPARKED_CANCEL,
        )

    async def abort(
        self, dispatch_id: str, note: str = "", *, force: bool = False
    ) -> Dispatch:
        """End a dispatch in any live state, with the verb that state needs (L2).

        APPROVAL is rejected at the gate (the integrate branch is moved aside),
        a planner failure is cancelled as such, and every other live state --
        SUBMITTED, PLANNING, ASSIGNED, LAUNCHING, MONITORING, REVIEW, MERGE --
        is cancelled with its branches kept. ``force`` stops a running line or
        fences off the drive that owns the row, as for cancel. A terminal
        dispatch is refused: there is nothing left to end.
        """
        from src.tools.auto_dispatch.planner_reason import is_planner_failed

        current = await self._require(dispatch_id)
        if current.state.value in _TERMINAL:
            raise ValueError(f"Dispatch {dispatch_id} is already {current.state.value}")
        if current.state is DispatchState.APPROVAL:
            options = {"force": True} if force else {}
            return await self.reject(dispatch_id, note, **options)
        if is_planner_failed(current):
            return await self.cancel_planner_failed(dispatch_id, note)
        return await self.cancel(dispatch_id, note, force=force)

    async def _end_parked(
        self,
        dispatch_id: str,
        note: str,
        *,
        force: bool,
        verb: str,
        unparked: bool = False,
    ) -> Dispatch:
        from src.tools.auto_dispatch.execution import cancel_live_run, live_runs_for_line
        from src.tools.auto_dispatch.gates import bump_epoch
        from src.tools.auto_dispatch.operator import cancel_parked, cancel_refusal

        stop_late = {"value": False}

        async def mutation(row: Dispatch) -> Dispatch:
            owner = self.active_drives.get(dispatch_id)
            if owner is not None and (
                not force or owner in self._coordinator.claims
            ):
                # A drive owns the row (wait for it to park, or --force), or a
                # human approve or reject holds the slot.
                raise ValueError("drive_active")
            if not (unparked and row.state in _UNPARKED_CANCEL):
                refused = cancel_refusal(row, verb=verb, force=force)
                if refused:
                    raise ValueError(refused)
            if force:
                for line in row.lines:
                    runs = set(live_runs_for_line(row.id, line.id))
                    if line.current_run:
                        runs.add(line.current_run)
                    for run_id in sorted(runs):
                        stopped = await cancel_live_run(run_id)
                        logger.info(
                            "%s --force on %s: run %s %s",
                            verb,
                            dispatch_id,
                            run_id,
                            "stopped" if stopped else "not live in this process",
                        )
            bump_epoch(row)
            cancel_parked(row, note=note, verb=verb)
            stop_late["value"] = force
            return row

        ended = await self.apply_mutation(dispatch_id, mutation)
        if stop_late["value"]:
            await self._stop_late_launches(ended)
        return ended

    async def _stop_late_launches(self, row: Dispatch) -> None:
        """After a forced end is saved, stop any launch that slipped past it.

        A drive checks the stored epoch just before it spawns, but a spawn
        whose check ran before the bump was saved can still appear. Rescan a
        few times and stop every launch of the dispatch's lines that shows up.
        """
        from src.tools.auto_dispatch.execution import cancel_live_run, live_runs_for_line

        quiet = 0
        for _ in range(_LATE_LAUNCH_SCANS):
            found = sorted(
                run_id
                for line in row.lines
                for run_id in live_runs_for_line(row.id, line.id)
            )
            for run_id in found:
                await cancel_live_run(run_id)
                logger.warning(
                    "Stopped launch %s that started after %s ended", run_id, row.id
                )
            quiet = 0 if found else quiet + 1
            if quiet >= 2:
                return
            await asyncio.sleep(_LATE_LAUNCH_SCAN_SECONDS)

    async def unblock(self, dispatch_id: str, line_id: str, note: str = "") -> Dispatch:
        """Move a wedged line to a ``worker_exited`` checkpoint (N40).

        For a line parked mid-implementation without that checkpoint (blocked
        ``no_new_commit`` / ``uncommitted_changes`` before N40, or parked by
        worker recovery before N38b). It is refused unless no run of the line
        is out: no ``current_run`` and no live launch for it. The line stays
        parked; ``dispatch reassign`` is then accepted. A parked dispatch is
        driven again, so its other ready lines move; a running drive applies
        the request at its next loop step.
        """
        return await self._human_decision(
            dispatch_id, line_id, {"kind": "unblock", "note": note}
        )

    async def adjudicate(
        self, dispatch_id: str, line_id: str, ruling: str, note: str = ""
    ) -> Dispatch:
        """Rule on a reviewer scope/security conflict (N41).

        ``in-scope`` upholds the BLOCK: the finding becomes the line's blocker
        and its author gets a fix round. ``out-of-scope`` overturns it: the
        review passes and the finding is kept in ``gates.follow_ups``. A
        security conflict is ruled out of scope only with a note.
        """
        from src.tools.auto_dispatch.operator import normalise_ruling

        chosen = normalise_ruling(ruling)
        if chosen is None:
            raise ValueError("invalid_ruling: expected in-scope or out-of-scope")
        return await self._human_decision(
            dispatch_id,
            line_id,
            {"kind": "adjudicate", "ruling": chosen, "note": note},
        )

    async def recover(
        self, dispatch_id: str, line_id: str, mode: str = "next", note: str = ""
    ) -> Dispatch:
        """Run a parked line's next recovery step now, split it, or hand it to a person (N76).

        ``next`` skips the step's backoff, ``split`` runs R-SPLIT on the line,
        ``human`` stops the automatic steps and leaves the line to a person,
        ``rereview`` (N87) says the operator fixed the branch: G2 and review
        run again on its current head, with no Worker launched.
        The request goes through the same path as unblock: the same refusal
        checks, applied to a parked row and re-driven, or queued for a drive
        that is running. The drive's liveness sweep performs the step; the
        refusal codes are listed in ``operator.recover_refusal``.
        """
        from src.tools.auto_dispatch.operator import normalise_recover_mode

        chosen = normalise_recover_mode(mode)
        if chosen is None:
            raise ValueError("invalid_mode: expected next, split, human or rereview")
        return await self._human_decision(
            dispatch_id, line_id, {"kind": "recover", "mode": chosen, "note": note}
        )

    async def _human_decision(
        self, dispatch_id: str, line_id: str, fields: dict[str, Any]
    ) -> Dispatch:
        """Apply an unblock or adjudication to a parked row, or queue it for the drive.

        The same pure write runs in both places, so a refusal answers now:
        while a drive runs, it is checked on a copy before it is queued.
        """
        from src.tools.auto_dispatch.gates import bump_epoch
        from src.tools.auto_dispatch.machine import (
            apply_human_request,
            drop_human_reassign,
            human_request,
            human_reassign_request_refusal,
            note_human_reassign_audit,
            pending_human_reassigns,
            previous_assignment,
            queue_human_request,
        )

        queued = {"value": False}

        async def mutation(row: Dispatch) -> Dispatch:
            if row.state.value in _TERMINAL:
                raise ValueError(f"Dispatch {dispatch_id} is already {row.state.value}")
            owner = self.active_drives.get(dispatch_id)
            driving = owner is not None
            if driving and owner in self._coordinator.claims:
                raise ValueError("drive_active")
            line = row.line(line_id)
            if line is None:
                raise ValueError(f"Dispatch {dispatch_id} has no such line")
            if line.id in pending_human_reassigns(row):
                raise ValueError("request_pending")
            request = human_request(line, fields)
            refused = human_reassign_request_refusal(row, line, request)
            if refused:
                raise ValueError(refused)
            runner = self._gate_runner_for(row)
            if driving:
                probe = copy.deepcopy(row)
                probe_line = probe.line(line.id)
                assert probe_line is not None
                refused = await apply_human_request(probe, probe_line, request, runner)
                if refused:
                    raise ValueError(refused)
                queue_human_request(row, line, fields)
                queued["value"] = True
                return row
            previous = previous_assignment(line)
            refused = await apply_human_request(row, line, request, runner)
            if refused:
                raise ValueError(refused)
            drop_human_reassign(row, line.id)
            note_human_reassign_audit(
                row,
                line.id,
                request,
                previous=previous,
                refused=None,
                applied_by="service",
            )
            bump_epoch(row)
            return row

        return await self.apply_mutation(
            dispatch_id, mutation, resume=lambda: not queued["value"]
        )

    async def cancel_planner_failed(self, dispatch_id: str, note: str = "") -> Dispatch:
        """Abandon a dispatch parked on a planner failure.

        The planner never produced a plan, so nothing ran and there is no
        integrate branch to move aside: the row goes to ABORT and keeps the
        reason it parked with. A dispatch that is not parked this way is
        refused before any write, and the epoch is bumped so a drive that is
        already running cannot write over the decision.
        """
        from src.tools.auto_dispatch.machine import cancel_planner_failed as cancel_row
        from src.tools.auto_dispatch.planner_reason import is_planner_failed

        def mutation(row: Dispatch) -> Dispatch:
            self._refuse_if_drive_active(dispatch_id)
            if not is_planner_failed(row):
                raise ValueError(
                    f"Dispatch {dispatch_id} is not parked on a planner failure"
                )
            from src.tools.auto_dispatch.gates import bump_epoch

            bump_epoch(row)
            cancel_row(row, note=note)
            return row

        return await self.apply_mutation(dispatch_id, mutation)

    async def reassign(self, dispatch_id: str, line_id: str, model: str) -> Dispatch:
        """Override a blocked line's work model and drive it from its next attempt.

        The same reserved-role rule as live work selection applies here, in
        dev and in live: a planner or primary reviewer is refused unless the
        loaded policy sets ``allow_reserved_code``. That override stores
        ``assignment_audit``. A line parked with ``replan_exhausted``, or
        paused in Stage-Gate ``blocked``, is reassignable: reassign is the
        way out its park message names. Any refusal leaves the line unchanged.
        The new model relaunches on the line's existing branch, and its first
        prompt carries the previous blockers, as a fix round's does.

        While a drive owns the dispatch, a BLOCKED or FAILED line that has no
        run is not the drive's to watch, so the reassign is queued in
        ``gates.human_reassigns`` instead of refused. The queue write does not
        touch the line or the epoch, so the running drive keeps its row. That
        drive applies the request at its next loop step and relaunches the
        line once; a request still queued when the drive ends is applied by
        ``_drive`` before it releases the dispatch. A RUNNING, ASSIGNED or
        ``implementing`` line, and a human call in progress, are still
        refused with ``drive_active``.
        """
        chosen = model.strip()
        if not chosen:
            raise ValueError("A reassignment needs a model")
        from src.server.services.autonomy.roles import canonical_provider, reserved_work_audit

        chosen = canonical_provider(chosen)
        reserved = chosen in self._policy.canonical_reserved()
        if reserved and not self._policy.allow_reserved_code:
            raise ValueError(
                f"{chosen} is reserved for planning or review and cannot be "
                "assigned work unless allow_reserved_code is set"
            )
        audit = reserved_work_audit(chosen) if reserved else None
        queued = {"value": False}

        from src.tools.auto_dispatch.human_pin import (
            reassign_headroom_refusal,
            record_pin,
        )
        from src.tools.auto_dispatch.selection import (
            WORKER_NOT_ALLOWED,
            dispatch_allowed_workers,
        )

        async def mutation(row: Dispatch) -> Dispatch:
            from src.tools.auto_dispatch.machine import (
                apply_human_reassign,
                drop_human_reassign,
                human_reassign_blocker,
                line_has_pending_fix,
                pending_human_reassigns,
                queue_human_reassign,
            )

            owner = self.active_drives.get(dispatch_id)
            driving = owner is not None
            if driving and owner in self._coordinator.claims:
                # A human approve or reject holds the slot; nothing drives yet.
                raise ValueError("drive_active")
            line = row.line(line_id)
            if line is None:
                raise ValueError(f"Dispatch {dispatch_id} has no such line")
            # L2: a dispatch restricted to some workers is reassigned only
            # among them, before any queue, Stage-Gate or epoch write.
            allowed = dispatch_allowed_workers(row)
            if allowed and chosen not in allowed:
                raise ValueError(
                    f"{WORKER_NOT_ALLOWED}: {chosen} is not one of this dispatch's "
                    f"workers ({', '.join(sorted(allowed))})"
                )
            # A replan-exhausted line is reassignable: its park message names
            # reassign as the way out. A pending fix is refused before
            # Stage-Gate advance, and before any author, attempt, epoch, or
            # intent write.
            if line_has_pending_fix(row, line.id):
                raise ValueError("fix_pending")
            # N86: refuse a worker this dispatch skips for quota, or whose
            # headroom reads exhausted, now -- not by a silent move later.
            refused_headroom = await reassign_headroom_refusal(
                row, chosen, self._reassign_headroom
            )
            if refused_headroom:
                raise ValueError(refused_headroom)
            runner = self._gate_runner_for(row)
            if driving:
                blocker = human_reassign_blocker(row, line)
                if blocker:
                    raise ValueError(blocker)
                if line.id in pending_human_reassigns(row):
                    raise ValueError("reassign_pending")
                # Check Stage-Gate on a copy so a refusal answers now. The
                # drive runs the same check again when it applies the request.
                probe = copy.deepcopy(row)
                probe_line = probe.line(line.id)
                assert probe_line is not None
                reason = await apply_human_reassign(probe, probe_line, chosen, audit, runner)
                if reason:
                    raise ValueError(reason)
                queue_human_reassign(row, line, chosen, audit)
                record_pin(row, line, chosen, queued=True)
                queued["value"] = True
                return row
            reason = await apply_human_reassign(row, line, chosen, audit, runner)
            if reason:
                raise ValueError(reason)
            record_pin(row, line, chosen)
            # An older queued request for this line is superseded by this one.
            drop_human_reassign(row, line.id)
            from src.tools.auto_dispatch.gates import bump_epoch

            bump_epoch(row)
            return row

        return await self.apply_mutation(
            dispatch_id, mutation, resume=lambda: not queued["value"]
        )

    def _reassign_headroom(self) -> HeadroomPort:
        """The quota reader a human reassign is checked against (N86)."""
        return self._headroom if self._headroom is not None else default_headroom()

    async def apply_mutation(
        self,
        dispatch_id: str,
        fn: Callable[[Dispatch], Dispatch | Awaitable[Dispatch]],
        *,
        resume: bool | Callable[[], bool] = False,
        claim: bool = False,
    ) -> Dispatch:
        """Re-read the row, apply ``fn`` to a new row, and save that row.

        The lock is held until the shielded save finishes, so the caller does
        not resume a drive against an older snapshot. Cancelling the caller,
        more than once, does not cancel the save. A failed save raises
        ``persist_failed`` and leaves the stored row unchanged. A refusal from
        ``fn`` is not a failed save.
        """
        return await self._locked_write(
            dispatch_id,
            fn,
            resume=resume,
            claim=claim,
            epoch0=None,
        )

    async def drive_mutation(
        self,
        dispatch_id: str,
        epoch0: int,
        fn: Callable[[Dispatch], Dispatch],
    ) -> Dispatch:
        """Write a drive's row only when the stored epoch is still ``epoch0``.

        A mismatch records ``stale_drive`` and raises ``StaleDrive``. ``fn``
        does not run, and the drive's other fields are not written.
        """
        from src.tools.auto_dispatch.machine import StaleDrive

        try:
            return await self._locked_write(dispatch_id, fn, epoch0=epoch0)
        except StaleDrive:
            raise

    async def _locked_write(
        self,
        dispatch_id: str,
        fn: Callable[[Dispatch], Dispatch | Awaitable[Dispatch]],
        *,
        resume: bool | Callable[[], bool] = False,
        claim: bool = False,
        epoch0: int | None = None,
    ) -> Dispatch:
        from src.tools.auto_dispatch.gates import drive_epoch, record_stale_drive
        from src.tools.auto_dispatch.machine import StaleDrive

        lock = self._coordinator.drive_lock(dispatch_id)
        async with lock:
            current = await self._require(dispatch_id)
            if epoch0 is not None and drive_epoch(current) != epoch0:
                draft = copy.deepcopy(current)
                record_stale_drive(draft)
                await self._await_durable_save(draft)
                raise StaleDrive
            draft = copy.deepcopy(current)
            updated = fn(draft)
            if inspect.isawaitable(updated):
                updated = await updated
            if updated is None:
                updated = draft
            _keep_service_gates(current, updated)
            saved = await self._await_durable_save(updated)
            # A callable decides after ``fn`` ran, still under the lock.
            if resume() if callable(resume) else resume:
                await self._resume(saved)
            elif claim:
                self._claim_unlocked(dispatch_id)
            return saved

    async def _await_durable_save(self, updated: Dispatch) -> Dispatch:
        """Save on its own task. Repeated cancellation still lets that task finish.

        ``uncancel`` runs before the next shield wait. On 3.14 a task whose
        ``cancelling()`` count is still positive raises ``CancelledError`` at
        once, without waiting for the save.
        """
        task = asyncio.create_task(self._store.save(updated))
        self._save_tasks.add(task)
        task.add_done_callback(self._save_tasks.discard)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
                current = asyncio.current_task()
                pending = current.cancelling() if current is not None else 0
                if current is not None:
                    for _ in range(pending):
                        current.uncancel()
            except Exception:
                # The save failed. ``task.result()`` maps that to persist_failed.
                break
        if cancelled:
            if task.done() and not task.cancelled():
                task.exception()
            raise asyncio.CancelledError
        try:
            return task.result()
        except Exception:
            raise ValueError("persist_failed") from None

    def _claim_unlocked(self, dispatch_id: str) -> None:
        """Hold the registry until the human call finishes its gate I/O."""
        if self.active_drives.get(dispatch_id) is not None:
            return
        token = object()
        self._coordinator.claims.add(token)
        self.active_drives[dispatch_id] = token

    def _drop_claim_unlocked(self, dispatch_id: str) -> None:
        token = self.active_drives.get(dispatch_id)
        if token is None or token not in self._coordinator.claims:
            return
        self.active_drives.pop(dispatch_id, None)
        self._coordinator.claims.discard(token)

    async def _release_claim(self, dispatch_id: str) -> None:
        lock = self._coordinator.drive_lock(dispatch_id)
        async with lock:
            self._drop_claim_unlocked(dispatch_id)

    async def _replace_claim_with_drive(self, dispatch: Dispatch) -> Dispatch:
        lock = self._coordinator.drive_lock(dispatch.id)
        async with lock:
            self._drop_claim_unlocked(dispatch.id)
            return await self._resume(dispatch)

    # -- driving --------------------------------------------------------------

    async def _drive(self, dispatch_id: str, owner: object | None = None) -> Dispatch:
        """Drive the dispatch, then deliver it when the drive reached COMPLETE (L2).

        Delivery runs after the drive has released the row, so a slow push or
        PR call never holds the drive slot; it never raises out of here.
        """
        result = await self._drive_once(dispatch_id, owner)
        return await self._deliver_after_drive(result)

    async def _drive_once(self, dispatch_id: str, owner: object | None = None) -> Dispatch:
        """Run the machine outside the write lock, then merge what it changed.

        The owner token is published only after this task holds the body lock.
        A second drive waits on that lock and does not replace the token. The
        token is removed on the way out only when it is still this drive's.
        """
        from src.tools.auto_dispatch.machine import StaleDrive

        token = owner if owner is not None else object()
        body = self._coordinator.body_lock(dispatch_id)
        async with body:
            current = self.active_drives.get(dispatch_id)
            owns = current is None or current is token
            if owns:
                self.active_drives[dispatch_id] = token
            try:
                if not owns:
                    row = await self._store.get(dispatch_id)
                    if row is None:
                        raise KeyError(dispatch_id)
                    return row
                while True:
                    try:
                        result = await self._drive_body(dispatch_id)
                    except StaleDrive:
                        row = await self._store.get(dispatch_id)
                        if row is None:
                            raise KeyError(dispatch_id) from None
                        result = row
                    # A reassign queued after the drive's last loop step is
                    # applied here, and the body runs again for it. The owner
                    # token is dropped under the same lock that found the
                    # queue empty, so a reassign either sees this drive and
                    # queues before that check, or sees no drive at all.
                    if not await self._hand_off_queued_reassigns(dispatch_id, token):
                        return result
            finally:
                if owns and self.active_drives.get(dispatch_id) is token:
                    self.active_drives.pop(dispatch_id, None)

    async def _hand_off_queued_reassigns(self, dispatch_id: str, token: object) -> bool:
        """Apply reassigns still queued when a drive body ends, or release the drive.

        True means at least one line was reassigned and the caller drives
        again with the same token. False means the queue was empty, or every
        request was refused, and the token is already gone. Each request is
        applied like a parked reassign: Stage-Gate check, line fields, and an
        epoch bump, all in one write that also removes it from the queue.
        """
        from src.tools.auto_dispatch.gates import bump_epoch
        from src.tools.auto_dispatch.machine import (
            apply_human_request,
            drop_human_reassign,
            human_reassign_request_refusal,
            note_human_reassign_audit,
            pending_human_reassigns,
            previous_assignment,
        )

        lock = self._coordinator.drive_lock(dispatch_id)
        async with lock:
            row = await self._store.get(dispatch_id)
            queue = pending_human_reassigns(row) if row is not None else {}
            if row is None or not queue:
                if self.active_drives.get(dispatch_id) is token:
                    self.active_drives.pop(dispatch_id, None)
                return False
            draft = copy.deepcopy(row)
            runner = self._gate_runner_for(draft)
            applied = 0
            for line_id, request in queue.items():
                line = draft.line(line_id)
                previous = previous_assignment(line)
                refused = human_reassign_request_refusal(draft, line, request)
                if refused is None and line is not None:
                    refused = await apply_human_request(draft, line, request, runner)
                if refused is None:
                    applied += 1
                else:
                    logger.warning(
                        "Dropped a queued reassign of %s on %s: %s",
                        line_id,
                        dispatch_id,
                        refused,
                    )
                drop_human_reassign(draft, line_id)
                note_human_reassign_audit(
                    draft,
                    line_id,
                    request,
                    previous=previous,
                    refused=refused,
                    applied_by="service",
                )
            if applied:
                bump_epoch(draft)
            await self._await_durable_save(draft)
            if not applied and self.active_drives.get(dispatch_id) is token:
                self.active_drives.pop(dispatch_id, None)
            return bool(applied)

    async def _drive_body(self, dispatch_id: str) -> Dispatch:
        from src.tools.auto_dispatch.machine import StaleDrive

        dispatch = await self._store.get(dispatch_id)
        if dispatch is None:
            raise KeyError(dispatch_id)
        baseline: dict[str, Dispatch] = {dispatch_id: copy.deepcopy(dispatch)}
        machine = self._machine(dispatch)
        collab = getattr(machine, "_c", None)
        previous = None
        if collab is not None:
            previous = collab.store
            collab.store = _MergingStore(self, self._store, baseline)
        try:
            try:
                driven = await machine.run(dispatch)
            except StaleDrive:
                current = await self._store.get(dispatch_id)
                return current if current is not None else dispatch
        finally:
            # A cancel is sticky: the first await would raise before ``aclose``
            # ran. Drop it, join the actor and the save it already started,
            # restore the store, then raise the cancel again. ``_drive`` pops
            # the owner only after that join.
            cancelled_again = False
            while True:
                current = asyncio.current_task()
                held = current.cancelling() if current is not None else 0
                if current is not None and held:
                    for _ in range(held):
                        current.uncancel()
                    cancelled_again = True
                try:
                    close = getattr(machine, "aclose", None)
                    if close is not None:
                        await close()
                    if collab is not None and previous is not None:
                        collab.store = previous
                    break
                except asyncio.CancelledError:
                    cancelled_again = True
            if cancelled_again:
                raise asyncio.CancelledError
        if getattr(machine, "_abandoned", False):
            current = await self._store.get(dispatch_id)
            return current if current is not None else driven

        epoch0 = getattr(machine, "_epoch0", 0)

        def mutation(fresh: Dispatch) -> Dispatch:
            before = baseline.get(dispatch_id)
            if before is None:
                return fresh
            return merge_drive(fresh, before, driven)

        try:
            return await self.drive_mutation(dispatch_id, epoch0, mutation)
        except StaleDrive:
            current = await self._store.get(dispatch_id)
            return current if current is not None else driven

    async def _resume(self, dispatch: Dispatch) -> Dispatch:
        """Schedule the drive. Reserve the registry only when the slot is empty.

        The reservation happens before the write lock drops, so a second human
        call loses. The drive republishes that same token after the body lock
        and does not hand the slot to a task that is still waiting.
        """
        token = self.active_drives.get(dispatch.id)
        minted = token is None
        if minted:
            token = object()
        task = _schedule(
            f"dispatch-{dispatch.id}",
            lambda owner=token, dispatch_id=dispatch.id: self._drive(dispatch_id, owner),
        )
        # A test double that does not return a task must not leave a token behind.
        # A live task reserves the slot before this returns, still under the write lock.
        if isinstance(task, asyncio.Task) and not task.done() and minted:
            if self.active_drives.get(dispatch.id) is None:
                self.active_drives[dispatch.id] = token
                dispatch_id = dispatch.id
                task.add_done_callback(
                    lambda _done, owner=token: self._coordinator.release_if_owner(
                        dispatch_id, owner
                    )
                )
        return dispatch

    # -- delivery (L2) -----------------------------------------------------------

    def _delivery_port(self, dispatch: Dispatch) -> Any:
        """The delivery for this dispatch, or None when this backend has none.

        An injected delivery is used as given. Otherwise only a live backend
        that builds its own machine delivers (an injected machine is a test
        seam), and ``CONDUCTOR_DISPATCH_DELIVERY=off`` turns it off.
        """
        if self._delivery is not None:
            return self._delivery
        if self._coordinator.delivery is not None:
            return self._coordinator.delivery
        from src.server.services.dispatch_delivery import DispatchDelivery, delivery_enabled

        settings = self._settings_for(dispatch)
        if (
            settings.adapters != "live"
            or self._machine_factory is not None
            or not delivery_enabled()
        ):
            return None
        return DispatchDelivery(
            gate=self._gate,
            repo_root_for=lambda row: Path(self._settings_for(row).repo_root),
            worktrees_dir_for=lambda row: (
                self._settings_for(row).runs_dir.parent / "dispatch-worktrees"
            ),
        )

    async def _deliver_after_drive(self, result: Dispatch) -> Dispatch:
        """Deliver once, automatically, when a drive leaves a fresh COMPLETE row."""
        from src.server.services.dispatch_delivery import DELIVERY

        if result is None or result.state is not DispatchState.COMPLETE:
            return result
        if isinstance(result.gates, dict) and DELIVERY in result.gates:
            return result
        port = self._delivery_port(result)
        if port is None:
            return result
        try:
            return await self._deliver_with(port, result.id, automatic=True)
        except Exception as exc:  # noqa: BLE001 - delivery never fails a finished drive
            logger.warning(
                "Dispatch %s delivery failed: %s", result.id, type(exc).__name__
            )
            return result

    async def deliver(self, dispatch_id: str) -> Dispatch:
        """Push the approved integrate branch and open its PR, or say why not (L2).

        A COMPLETE dispatch only. An ``opened`` record is kept as it is; any
        other record (no token yet, a failed push) is retried. The outcome is
        stored under ``gates["delivery"]`` and as the dispatch message.
        """
        current = await self._require(dispatch_id)
        if current.state is not DispatchState.COMPLETE:
            raise ValueError(
                f"Dispatch {dispatch_id} is {current.state.value}; only a COMPLETE "
                "dispatch is delivered"
            )
        port = self._delivery_port(current)
        if port is None:
            raise ValueError(
                "delivery_disabled: this backend does not deliver dispatches "
                "(dev adapters, or CONDUCTOR_DISPATCH_DELIVERY=off)"
            )
        return await self._deliver_with(port, dispatch_id, automatic=False)

    async def _deliver_with(self, port: Any, dispatch_id: str, *, automatic: bool) -> Dispatch:
        from src.server.services.dispatch_delivery import DELIVERY, OPENED
        from src.tools.auto_dispatch.approval import is_auto_ready
        from src.tools.auto_dispatch.protocols import ApprovalDecision

        async with self._coordinator.delivery_lock(dispatch_id):
            row = await self._require(dispatch_id)
            # Review fix 1: auto:n73 never delivers; only a human APPROVED gate.
            if is_auto_ready(row) and (
                await self._gate.decision(row.approval_token or "")
                is not ApprovalDecision.APPROVED
            ):
                raise ValueError(
                    "human_approval_required: auto:n73 only marks the integrate "
                    "branch ready; push and PR need a human approval"
                )
            previous = row.gates.get(DELIVERY) if isinstance(row.gates, dict) else None
            if isinstance(previous, dict) and (
                automatic or previous.get("state") == OPENED
            ):
                return row
            record = dict(await port.deliver(row))
            attempts = int(previous.get("attempts") or 0) if isinstance(previous, dict) else 0
            record["attempts"] = attempts + 1

            def mutation(fresh: Dispatch) -> Dispatch:
                if fresh.state is not DispatchState.COMPLETE:
                    raise ValueError(
                        f"Dispatch {dispatch_id} left COMPLETE during delivery"
                    )
                if not isinstance(fresh.gates, dict):
                    fresh.gates = {}
                fresh.gates[DELIVERY] = record
                fresh.message = str(record.get("message") or fresh.message or "")
                return fresh

            saved = await self.apply_mutation(dispatch_id, mutation)
            logger.info(
                "Dispatch %s delivery: %s", dispatch_id, record.get("state")
            )
            return saved

    # -- restart recovery (L2) ----------------------------------------------------

    async def _recover_drive(self, dispatch_id: str) -> Dispatch:
        """The wakeup's recover: restart stranded planning once, then drive.

        The first time this backend's wakeup meets a row, a dispatch the last
        process left mid-plan (PLANNING with no archived spec, or parked as
        ``resume_spec_missing`` with no lines) is put back to SUBMITTED so the
        drive plans it again from its goal instead of parking it for a
        person. A row this process is already driving is left alone.
        """
        checked = self._restart_checked
        if dispatch_id not in checked:
            if len(checked) >= _RESTART_CHECKED_LIMIT:
                # Only rows met right after startup can be stranded by it; a
                # long-running backend forgets the oldest ids.
                for stale in list(checked)[: len(checked) // 2]:
                    checked.pop(stale, None)
            checked[dispatch_id] = None
            if self.active_drives.get(dispatch_id) is None:
                try:
                    await self._restart_stranded_planning(dispatch_id)
                except Exception as exc:  # noqa: BLE001 - the drive still runs
                    logger.warning(
                        "Dispatch %s planning restart failed: %s",
                        dispatch_id,
                        type(exc).__name__,
                    )
        return await self._drive(dispatch_id)

    async def _restart_stranded_planning(self, dispatch_id: str) -> bool:
        """SUBMITTED again for a dispatch a restart stranded in planning."""
        from src.tools.auto_dispatch.gates import bump_epoch

        current = await self._store.get(dispatch_id)
        if current is None or not _stranded_in_planning(current):
            return False

        class _NotStranded(ValueError):
            pass

        def mutation(row: Dispatch) -> Dispatch:
            if self.active_drives.get(dispatch_id) is not None:
                raise _NotStranded
            if not _stranded_in_planning(row):
                raise _NotStranded
            previous = row.state.value
            row.gates["planning_restarts"] = _planning_restarts(row) + 1
            row.state = DispatchState.SUBMITTED
            row.spec_ref = ""
            row.message = (
                f"Planning restarted from the goal: the backend stopped while "
                f"this dispatch was {previous}"
            )
            bump_epoch(row)
            return row

        try:
            await self.apply_mutation(dispatch_id, mutation)
        except _NotStranded:
            return False
        logger.info("Dispatch %s planning restarted after a backend restart", dispatch_id)
        return True

    def _spec_archive(self, dispatch: Dispatch):
        from src.tools.auto_dispatch.planning import FileSpecArchive

        settings = self._settings_for(dispatch)
        return FileSpecArchive(settings.runs_dir.parent / "specs")

    def _stamp_spec_ref(self, dispatch_id: str) -> Callable[[str], Awaitable[None]]:
        """Keep a fresh plan's archive reference on the row (L2 restart recovery)."""

        async def stamp(spec_ref: str) -> None:
            def mutation(row: Dispatch) -> Dispatch:
                if row.state.value in _TERMINAL:
                    raise ValueError("terminal")
                row.spec_ref = spec_ref
                return row

            try:
                await self.apply_mutation(dispatch_id, mutation)
            except ValueError:
                return

        return stamp

    async def open(self) -> None:
        """Start the consumer and return once it is ready.

        Recovery keeps running after this returns. It does not restart this
        process or any other service. v1 is a single instance: the lease is
        the memory lease unless the caller passes one, and a database store
        takes the advisory lock instead.
        """
        from src.server.services.dispatch_wakeup import DispatchWakeup, lease_for_store

        if self._wakeup is None:
            lease = self._coordinator.wakeup_lease
            if lease is None:
                lease = lease_for_store(self._store)
                self._coordinator.wakeup_lease = lease
            self._wakeup = DispatchWakeup(
                store=self._store,
                recover=self._recover_drive,
                poll_interval_seconds=self._settings.poll_interval_seconds,
                lease=lease,
            )
        await self._wakeup.start()

    async def close(self) -> None:
        """Stop the wakeup consumer. A service that was never opened is unchanged."""
        wakeup = self._wakeup
        if wakeup is None:
            return
        await wakeup.stop()

    def _cost_selector_kwargs(self, settings: DispatchSettings) -> dict[str, Any]:
        """N30: the soft cost input, only when its flag is on (live adapters).

        Off passes nothing, so the selector's ordering is today's exactly. One
        ``CostStats`` per service keeps its 60 s cache across machines.
        """
        if not settings.cost_selector or settings.cost_weight <= 0:
            return {}
        stats = self._cost_stats
        if stats is None:
            from src.tools.auto_dispatch.cost import CostStats

            stats = CostStats(self._store, min_merged=settings.cost_min_merged)
            self._cost_stats = stats
        return {"cost_stats": stats, "cost_weight": settings.cost_weight}

    async def cost_aggregate(
        self, *, limit: int = 50, user_id: str | None = None
    ) -> dict[str, Any]:
        """N30: by-model and waste-by-class tables over the latest ``limit`` dispatches."""
        from src.tools.auto_dispatch.cost import aggregate_cost_report

        limit = max(1, min(500, int(limit)))
        rows = await self._store.list(user_id=user_id, limit=limit)
        return aggregate_cost_report(rows, limit=limit)

    def _machine(self, dispatch: Dispatch) -> DispatchMachine:
        if self._machine_factory is not None:
            return self._machine_factory(dispatch)
        settings = self._settings_for(dispatch)
        gate = self._gate
        from src.tools.auto_dispatch.selection import dispatch_allowed_workers

        allowed = tuple(sorted(dispatch_allowed_workers(dispatch)))
        if settings.adapters == "live":
            # ``cond drain`` holds new Worker starts here, outside the machine
            # (N64); monitoring, G2 and review do not go through the launcher.
            launcher = DrainAwareLauncher(ScriptLauncher(
                settings.runs_dir,
                script=settings.launch_script,
                cwd=settings.repo_root,
                worktrees_dir=settings.runs_dir.parent / "dispatch-worktrees",
                dispatch_id=dispatch.id,
                skills_hub=settings.skills_hub,
                impact_map=settings.impact_map,
                sandbox_policy=settings.sandbox_policy,
            ))
            monitor = LogMonitor(
                settings.runs_dir,
                stall_seconds=settings.stall_seconds,
                timeout_seconds=settings.timeout_seconds,
                canceller=launcher.cancel,
            )
            # N64: a drained restart waits for merges and reviewer CLIs too.
            merger = BusyProxy(
                GitMerger(
                    settings.repo_root,
                    dispatch,
                    gate=gate,
                    worktrees_dir=settings.runs_dir.parent / "dispatch-worktrees",
                ),
                activity.MERGE,
                activity.busy,
            )
            reviewer = BusyProxy(
                CrossReviewer(
                    NativeReviewBackend(
                        cwd=settings.repo_root,
                        timeout_seconds=settings.review_timeout_seconds,
                        scratch_dir=settings.runs_dir / "review-scratch",
                    ),
                    branch_evidence(dispatch, settings.repo_root),
                ),
                activity.REVIEW,
                activity.busy,
            )
            # One store feeds both the write and the selector's success rate,
            # and the rate is read back from dispatch rows that already exist.
            # Headroom is the injected port, or default_headroom() -- the real
            # CodexBar-backed port -- shared by the planner's DAG assignment and
            # the selector, so a plan and a reassignment read one quota.
            outcomes = MemoryP0OutcomeStore(DispatchRowMemoryPort(self._store))
            headroom = self._headroom if self._headroom is not None else default_headroom()
            # N63-1: one plan_status read per model per drive.
            headroom = PlanStatusCache(headroom)
            # R6 L3: plan -> assign -> review as recorded service-role calls.
            chain = ServiceChain(dispatch)
            instrument_reviewer(reviewer, chain)
            collaborators = Collaborators(
                # Same HeadroomPort for planner DAG assignment and the
                # selector: machine skips the selector when the planner
                # already set assigned_model.
                planner=instrument_planner(
                    LLMPlanner(
                        self._planner_complete_for(dispatch),
                        policy=self._policy,
                        headroom=headroom,
                        default_worker=settings.default_worker,
                        # L2: the plan is archived and its reference kept on the
                        # row, so a restart mid-dispatch reloads it instead of
                        # stranding the run; the worker restriction holds the
                        # plan to the dispatch's own workers.
                        archive=self._spec_archive(dispatch),
                        resume_spec_ref=dispatch.spec_ref,
                        on_archived=self._stamp_spec_ref(dispatch.id),
                        allowed_workers=allowed,
                    ),
                    chain,
                    model=settings.planner,
                ),
                selector=instrument_selector(
                    QuotaAwareModelSelector(
                        headroom,
                        outcomes,
                        review_refused=_disabled_by_operator,
                        policy=self._policy,
                        path_ownership=PathOwnership(self._store),
                        allowed_workers=allowed,
                        **self._cost_selector_kwargs(settings),
                    ),
                    chain,
                ),
                launcher=launcher,
                monitor=monitor,
                reviewer=reviewer,
                merger=merger,
                gate=gate,
                outcomes=outcomes,
                store=self._store,
                gates=self._gate_runner_for(dispatch),
                # N73 L5: the Super-agent picks model, effort and runtime
                # (Q8 rollout; ASSIGN_MODE=off wires nothing).
                auto_approve=auto_approve_enabled(),
                assigner=build_assigner(
                    llm=self._llm,
                    policy=self._policy,
                    headroom=headroom,
                    store=self._store,
                    outcomes_port=DispatchRowMemoryPort(self._store),
                ),
            )
        else:
            # Dev drills keep RoundRobinSelector over DEV_MODEL_POOL. Reserved-role
            # limits apply to the live selector above and to reassign, not here.
            collaborators = Collaborators(
                planner=FixedPlanner(),
                selector=RoundRobinSelector(allowed_workers=allowed),
                launcher=RecordingLauncher(),
                monitor=ScriptedMonitor(),
                reviewer=AlwaysPassReviewer(),
                merger=NoopMerger(),
                gate=gate,
                outcomes=InMemoryOutcomeStore(),
                store=self._store,
                gates=None,
            )
        return DispatchMachine(
            collaborators,
            config=MachineConfig(
                poll_interval_seconds=settings.poll_interval_seconds,
                # Dev drills keep one line at a time; live dispatches run
                # independent lines in parallel (N31).
                max_parallel_lines=(
                    settings.max_parallel_lines if settings.adapters == "live" else 1
                ),
            ),
        )

    def _planner_complete_for(self, dispatch: Dispatch):
        """Bind the active row while the planner factory is built."""
        token = _ACTIVE_DISPATCH.set(dispatch)
        try:
            return self._planner_complete(dispatch.user_id)
        finally:
            _ACTIVE_DISPATCH.reset(token)

    def _planner_complete(self, user_id: str):
        """The live planner's ``complete``: Claude Code unless ``llm`` is chosen.

        The planner's ``cwd`` is the dispatch's own checkout, not the backend's
        default repository: a plan written for another checkout names paths that
        do not exist in this one.
        """
        settings = self._settings_for(_ACTIVE_DISPATCH.get())
        if settings.planner == PLANNER_LLM:
            return self._complete(user_id)
        from src.tools.auto_dispatch.planner_prompt import plan_json_schema

        return ClaudeCodeComplete(
            cwd=settings.repo_root,
            binary=settings.planner_bin,
            model=settings.planner_model,
            timeout_seconds=settings.planner_timeout_seconds,
            json_schema=plan_json_schema(),
        )

    def _complete(self, user_id: str):
        llm = self._llm

        async def complete(prompt: str, system: str) -> str:
            if llm is None:
                raise RuntimeError("No LLM service is configured for live dispatch planning")
            return str(
                await llm.complete(
                    user_id=user_id, user_prompt=prompt, system_prompt=system
                )
            )

        return complete

    def _gate_runner_for(self, dispatch: Dispatch) -> GateRunner | None:
        """Bind the active row while the gate runner factory is built."""
        token = _ACTIVE_DISPATCH.set(dispatch)
        try:
            return self._gate_runner()
        finally:
            _ACTIVE_DISPATCH.reset(token)

    def _gate_runner(self) -> GateRunner | None:
        """Live dispatches run G2 and G5 unless the operator turns gates off.

        Dev never reaches this. ``CONDUCTOR_DISPATCH_GATES=off`` keeps the live
        machine on the same path as dev. The gates read the dispatch's own
        repository, so a workspace run is gated in the checkout it changed.
        """
        settings = self._settings_for(_ACTIVE_DISPATCH.get())
        if settings.adapters != "live" or settings.gates_mode.strip().lower() == "off":
            return None
        return GateRunner(
            GateSettings(
                runs_dir=settings.runs_dir,
                repo_root=settings.repo_root,
                worktrees_dir=settings.runs_dir.parent / "dispatch-worktrees",
                g2_command=settings.g2_command,
                g2_timeout_seconds=settings.g2_timeout_seconds,
                g5_command=settings.g5_command,
                g5_timeout_seconds=settings.g5_timeout_seconds,
                risk_globs=settings.risk_globs,
                max_g2_fixes=settings.max_g2_fixes,
                max_replans=settings.max_replans,
            ),
            busy=activity.busy,
        )

    async def _require(self, dispatch_id: str) -> Dispatch:
        dispatch = await self._store.get(dispatch_id)
        if dispatch is None:
            raise KeyError(dispatch_id)
        return dispatch


def _public_gates(
    dispatch: Dispatch, *, cost: dict[str, Any] | None | object = ...
) -> dict[str, Any]:
    if not isinstance(dispatch.gates, dict):
        return {}
    gates = dict(dispatch.gates)
    scheduler = gates.get("scheduler")
    if isinstance(scheduler, dict):
        from src.tools.auto_dispatch.gates import scheduler_view

        gates["scheduler"] = scheduler_view(scheduler)
    if "cost" in gates:
        # N30: the raw per-attempt ledger is served by /{id}/cost only; the
        # row view keeps the line-level summary so GET /{id} stays small.
        gates["cost"] = _cost_summary(dispatch) if cost is ... else cost
    return gates


def _without_ledger(dispatch: Dispatch) -> Dispatch:
    """A shallow copy whose gates lack ``cost``: the legacy-basis view."""
    import copy

    bare = copy.copy(dispatch)
    gates = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    bare.gates = {key: value for key, value in gates.items() if key != "cost"}
    return bare


def _cost_safely(build: Callable[[Dispatch], dict[str, Any]], dispatch: Dispatch) -> dict[str, Any] | None:
    """Run a cost builder; a corrupt ledger falls back to the legacy basis.

    ``gates["cost"]`` is a JSONB cell anything may have written. A view must
    render whatever it holds, so a failure retries without the ledger (the
    ``outcomes`` + ``worker_recovery`` basis), and only if that fails too is
    the answer None.
    """
    try:
        return build(dispatch)
    except Exception:  # noqa: BLE001 - status must render whatever one cell holds
        logger.warning("Cost ledger unreadable for dispatch %s", dispatch.id, exc_info=True)
    try:
        return build(_without_ledger(dispatch))
    except Exception:  # noqa: BLE001
        logger.warning("Legacy cost view failed for dispatch %s", dispatch.id, exc_info=True)
        return None


def _cost_summary(dispatch: Dispatch) -> dict[str, Any] | None:
    """N30: line-level cost summary; a broken ledger never breaks the row view."""
    from src.tools.auto_dispatch.cost import dispatch_cost_summary

    return _cost_safely(dispatch_cost_summary, dispatch)


def cost_report(dispatch: Dispatch) -> dict[str, Any] | None:
    """N30: the full report for ``GET /{id}/cost``; legacy basis on a corrupt ledger."""
    from src.tools.auto_dispatch.cost import dispatch_cost_report

    return _cost_safely(dispatch_cost_report, dispatch)


def ops_state() -> dict[str, Any]:
    """What this backend process sees of the drain flag and its busy markers (N64).

    ``cond status`` compares the path with its own, so a backend started with
    another ``CONDUCTOR_HOME`` (which would never see the operator's drain) is
    visible instead of silent.
    """
    from src.conductor_cli.drain import conductor_home

    home = conductor_home()
    return {
        "conductor_home": str(home),
        "drain": drain_state(home),
        "busy": activity.busy_counts(home),
    }


def drain_state(home: Any = None) -> dict[str, Any]:
    """The drain flag as this backend process reads it."""
    from src.conductor_cli.drain import drain_path, read_drain

    flag = read_drain(home)
    return {
        "drained": flag is not None,
        "path": str(drain_path(home)),
        "set_at": str((flag or {}).get("set_at") or ""),
        "reason": str((flag or {}).get("reason") or ""),
    }


def dispatch_summary(dispatch: Dispatch) -> dict[str, Any]:
    """The read-only shape the API and the CLI both render."""
    cost = _cost_summary(dispatch)
    return {
        "id": dispatch.id,
        "goal": dispatch.goal,
        "state": dispatch.state.value,
        "progress_pct": progress_pct(dispatch),
        "integrate_branch": dispatch.integrate_branch,
        "approval_token": dispatch.approval_token,
        "message": dispatch.message,
        "lines": [line.to_dict() for line in dispatch.lines],
        "outcomes": {key: value.to_dict() for key, value in dispatch.outcomes.items()},
        "gates": _public_gates(dispatch, cost=cost),
        "spec_ref": dispatch.spec_ref,
        "human_reassigns": _human_reassign_view(dispatch),
        "drain": drain_state(),
        "liveness": _liveness_view(dispatch),
        "cost": cost,
        "assignment": assignment_view(dispatch),
    }


def assignment_view(dispatch: Dispatch) -> dict[str, Any]:
    """N73 L5: per line, the latest Super-agent choice next to the selector's.

    Facts only (model, effort, runtime, source, mode, selector's pick,
    feasibility codes, fallback); the reason text is already redacted of the
    task by the record. ``entries`` counts the line's recorded attempts.
    """
    from src.tools.auto_dispatch.superagent_assign.record import assignment_entries

    gates = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    cells = gates.get("lines") if isinstance(gates.get("lines"), dict) else {}
    out: dict[str, Any] = {}
    for line_id, snap in cells.items():
        if not isinstance(snap, dict) or not snap.get("assignment"):
            continue
        try:
            entries = assignment_entries(snap.get("assignment"))
        except (TypeError, ValueError):
            continue
        if not entries:
            continue
        latest = entries[-1]
        choice = latest.get("choice") or {}
        out[str(line_id)] = {
            "mode": latest.get("mode"),
            "source": latest.get("source"),
            "model": choice.get("model"),
            "model_id": choice.get("model_id"),
            "effort": choice.get("effort"),
            "runtime": choice.get("runtime"),
            "reason": choice.get("reason"),
            "selector_choice": (latest.get("selector_choice") or {}).get("model"),
            "feasibility": latest.get("feasibility"),
            "reask": latest.get("reask"),
            "fallback_reason": latest.get("fallback_reason"),
            "entries": len(entries),
        }
    return out


def _liveness_view(dispatch: Dispatch) -> dict[str, Any]:
    """N76: owner, next step, next time and budget of each open line."""
    from src.tools.auto_dispatch.liveness import modes
    from src.tools.auto_dispatch.operator import liveness_status

    try:
        view = liveness_status(dispatch)
    except Exception:  # noqa: BLE001 - status must render whatever one cell holds
        logger.warning("Liveness view failed for dispatch %s", dispatch.id, exc_info=True)
        view = {"lines": {}, "needs_human": []}
    return {"mode": modes.liveness_mode(), **view}


def _human_reassign_view(dispatch: Dispatch) -> dict[str, list[dict[str, Any]]]:
    """Reassigns queued for a running drive, and the ones dropped, with why."""
    from src.tools.auto_dispatch.machine import pending_human_reassigns

    queued = [
        {
            "line_id": line_id,
            "kind": request.get("kind") or "reassign",
            "model": request.get("model"),
            "ruling": request.get("ruling"),
            "mode": request.get("mode"),
            "requested_at": request.get("requested_at"),
        }
        for line_id, request in pending_human_reassigns(dispatch).items()
    ]
    gates = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    scheduler = gates.get("scheduler")
    audit = scheduler.get("audit") if isinstance(scheduler, dict) else None
    dropped = [
        {
            "line_id": item.get("line_id"),
            "model": item.get("model"),
            "reason": item.get("reason"),
            "applied_by": item.get("applied_by"),
        }
        for item in (audit if isinstance(audit, list) else [])
        if isinstance(item, dict)
        and str(item.get("kind") or "") in _HUMAN_REQUEST_AUDIT
        and item.get("result") == "refused"
    ]
    return {"queued": queued, "dropped": dropped}


def _positive_int(value: str | None, default: int) -> int:
    """A count of at least 1; a blank or invalid value keeps the default."""
    try:
        parsed = int(str(value).strip()) if value is not None and str(value).strip() else default
    except ValueError:
        logger.warning("Ignoring an invalid count %r; using %s", value, default)
        return default
    return parsed if parsed >= 1 else default


_ON_VALUES = frozenset({"on", "1", "true", "yes", "enabled"})
_OFF_VALUES = frozenset({"off", "0", "false", "no", "disabled"})


def _on_off(value: str | None, default: bool) -> bool:
    """An on/off flag; blank or unrecognised keeps the default."""
    text = (value or "").strip().lower()
    if text in _ON_VALUES:
        return True
    if text in _OFF_VALUES:
        return False
    return default


def _cost_weight(value: str | None) -> float:
    """``CONDUCTOR_DISPATCH_COST_WEIGHT``: default 0.15, clamped to [0, 0.5]."""
    from src.tools.auto_dispatch.selection import MAX_COST_WEIGHT

    try:
        parsed = float(str(value).strip()) if value is not None and str(value).strip() else 0.15
    except ValueError:
        logger.warning("Ignoring an invalid cost weight %r; using 0.15", value)
        parsed = 0.15
    if parsed != parsed:  # NaN
        parsed = 0.15
    return max(0.0, min(MAX_COST_WEIGHT, parsed))


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _risk_globs(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    return tuple(part.strip() for part in value.split(",") if part.strip())


_wakeup_service: DispatchService | None = None
_process_coordinator: DispatchCoordinator | None = None


def bind_process_coordinator(coordinator: DispatchCoordinator | None) -> None:
    """Install the lifespan coordinator. ``None`` clears it."""
    global _process_coordinator
    _process_coordinator = coordinator


def process_coordinator() -> DispatchCoordinator | None:
    return _process_coordinator


async def start_wakeup() -> None:
    """Process entry for the dispatch consumer. One service, started once."""
    global _wakeup_service
    if _wakeup_service is None:
        coordinator = process_coordinator()
        if coordinator is None:
            coordinator = DispatchCoordinator()
            bind_process_coordinator(coordinator)
        # The coordinator's service carries its LLM service to resumed planning.
        _wakeup_service = coordinator.service()
    await _wakeup_service.open()


async def stop_wakeup() -> None:
    """Cancel the process consumer. Nothing else is restarted or signalled.

    The service is cleared so the next ``start_wakeup`` in this process builds
    a service on the coordinator bound at that moment.
    """
    global _wakeup_service
    if _wakeup_service is not None:
        await _wakeup_service.close()
        _wakeup_service = None


def _schedule(key: str, factory: Any) -> asyncio.Task[Any] | None:
    """Hold a strong reference to the driving task.

    Imported lazily: ``src.server.app`` builds the FastAPI app, which imports
    this module's router, so importing the registry at module scope is a cycle.
    """
    from src.server.app.background_starts import schedule_start

    scheduled = schedule_start(key, factory)
    return scheduled if isinstance(scheduled, asyncio.Task) else None
