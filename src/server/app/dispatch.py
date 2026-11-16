"""Read-only view of auto-dispatch runs, plus the human decisions.

The GETs are the surface a dashboard or the CLI polls. The POSTs other than
start are the operator's writes, and they exist because a human has to answer
them: approve, reject or cancel a dispatch, override a blocked line's model,
unblock a wedged line so it can be reassigned, or adjudicate a reviewer scope
conflict. Every refusal from the service is a 409 with its reason. The one
route that pushes is ``deliver`` (L2), and only for a COMPLETE dispatch whose
approval gate holds APPROVED; the merger checks the gate again before Git runs.
``abort`` ends a dispatch in any live state.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from src.server.services.dispatch import (
    cost_report,
    ops_state,
    DispatchService,
    dispatch_summary,
    process_coordinator,
)
from src.server.services.dispatch_workspaces import (
    WorkspaceNotFound,
    fetch_before_start,
    get_workspace as get_dispatch_workspace,
)
from src.server.utils.api import CurrentUserId, handle_api_exceptions
from src.tools.auto_dispatch.planner_reason import is_planner_failed
from src.tools.auto_dispatch.planning import MAX_GOAL_BYTES
from src.tools.auto_dispatch.types import Dispatch

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/dispatch", tags=["auto-dispatch"])


class DispatchStartRequest(BaseModel):
    goal: str = Field(min_length=1)
    #: A registered dispatch workspace (id or name). The server resolves it,
    #: fetches its remote before starting, and refuses an unknown one.
    workspace: str | None = None
    #: L2: the workers this dispatch may use (``["kimi", "glm"]`` or
    #: ``"kimi,glm"``). The planner, the selector and reassign are held to
    #: them; omitted or empty allows every worker the role policy allows.
    workers: list[str] | str | None = None
    #: P0 dedup: the ticket or issue this dispatch is for (``#59``,
    #: ``owner/repo#59``, ``ticket:<id>``). With dedup enforce, an active
    #: dispatch for the same ref is returned instead of a new one.
    source_ref: str | None = Field(default=None, max_length=256)
    #: Start a new dispatch even when one for ``source_ref`` is active.
    force: bool = False

    @field_validator("goal")
    @classmethod
    def _goal_fits_the_planner(cls, value: str) -> str:
        """The goal is held to the planner's byte ceiling, here as well.

        The planner refuses a goal over the ceiling before it calls a model,
        so the API refuses the same goal: one limit in one place, and the
        operator hears it at once instead of after a planning round. The
        ceiling is counted in UTF-8 bytes, which is how the planner counts it.
        """
        try:
            size = len(value.strip().encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("the dispatch goal is not valid UTF-8") from None
        if size > MAX_GOAL_BYTES:
            raise ValueError(
                f"the dispatch goal is longer than {MAX_GOAL_BYTES} bytes"
            )
        return value


class DispatchDecisionRequest(BaseModel):
    note: str = ""
    # Reject/cancel only: stop a running line first instead of refusing.
    force: bool = False


class DispatchNoteRequest(BaseModel):
    note: str = ""


ADJUDICATION_RULINGS = ("in-scope", "out-of-scope")


class DispatchAdjudicateRequest(BaseModel):
    ruling: str
    note: str = ""

    @field_validator("ruling")
    @classmethod
    def _known_ruling(cls, value: str) -> str:
        """``in-scope`` or ``out-of-scope``; the underscore spelling is accepted."""
        ruling = value.strip().lower().replace("_", "-")
        if ruling not in ADJUDICATION_RULINGS:
            raise ValueError("ruling must be 'in-scope' or 'out-of-scope'")
        return ruling


RECOVER_MODES = ("next", "split", "human")


class DispatchRecoverRequest(BaseModel):
    #: ``next`` runs the next recovery step now; ``split`` / ``human`` as named.
    mode: str = "next"
    note: str = ""

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        mode = (value or "next").strip().lower()
        if mode not in RECOVER_MODES:
            raise ValueError("mode must be 'next', 'split', 'human' or 'rereview'")
        return mode


class DispatchReassignRequest(BaseModel):
    model: str = Field(min_length=1)


class DispatchSummary(BaseModel):
    id: str
    goal: str
    state: str
    progress_pct: int
    integrate_branch: str
    approval_token: str | None = None
    message: str | None = None
    lines: list[dict]
    outcomes: dict
    gates: dict = Field(default_factory=dict)
    spec_ref: str = ""
    # {"queued": [...], "dropped": [...]}: human reassigns waiting for the
    # running drive, and the ones it dropped with a reason.
    human_reassigns: dict = Field(default_factory=dict)
    # N64: the drain flag as this backend reads it.
    drain: dict = Field(default_factory=dict)
    # N76: {"mode", "lines": {id: {owner, next_step, next_at, budget_left,
    # needs_human, ...}}, "needs_human": [ids]} for the open lines.
    liveness: dict = Field(default_factory=dict)
    # N30: line-level cost summary ({"v", "basis", "counts", "lines"}); the
    # per-attempt ledger is served by GET /{id}/cost only.
    cost: dict | None = None
    # N73 L5: {line_id: {mode, source, model, effort, runtime, reason,
    # selector_choice, feasibility, reask, fallback_reason, entries}}.
    assignment: dict = Field(default_factory=dict)


class DispatchListResponse(BaseModel):
    dispatches: list[DispatchSummary]


def _summary(dispatch: Dispatch) -> DispatchSummary:
    return DispatchSummary(**dispatch_summary(dispatch))


def _service() -> DispatchService:
    """One coordinator for every request. Unbound tests keep a private one."""
    coordinator = process_coordinator()
    if coordinator is None:
        return DispatchService()
    return coordinator.service()


@router.get("/status/active", response_model=DispatchListResponse)
@handle_api_exceptions("list active dispatches", logger)
async def active_dispatches(user_id: CurrentUserId) -> DispatchListResponse:
    """Dispatches that have not reached a terminal state, newest first."""
    dispatches = await _service().active(user_id)
    return DispatchListResponse(dispatches=[_summary(item) for item in dispatches])


@router.get("/status/ops")
@handle_api_exceptions("read the dispatch ops state", logger)
async def dispatch_ops_state() -> dict:
    """N64: the drain flag and busy markers this backend sees, for ``cond status``.

    No user id: it names a path and counts, nothing a user owns, and ``cond``
    calls it over loopback without credentials, like ``/health``.
    """
    return ops_state()


@router.get("/cost")
@handle_api_exceptions("read the dispatch cost report", logger)
async def dispatch_cost_aggregate(
    user_id: CurrentUserId, limit: int = Query(50, ge=1, le=500)
) -> dict:
    """N30: cost per merged line and waste by class over the latest ``limit``
    dispatches of the caller. Declared before ``/{dispatch_id}`` so the
    literal path is not read as an id. Numbers and codes only."""
    return await _service().cost_aggregate(limit=limit, user_id=user_id)


@router.post("", response_model=DispatchSummary, status_code=201)
@handle_api_exceptions("start a dispatch", logger)
async def start_dispatch(
    request: DispatchStartRequest, user_id: CurrentUserId
) -> DispatchSummary:
    """Start a dispatch for a goal and return it before any line has run.

    With ``workspace``, the registered checkout is resolved and fetched first.
    A failed fetch does not refuse the start -- an unreachable remote must not
    make every dispatch impossible -- but it is logged with the reason.
    """
    selection: dict | None = None
    if request.workspace:
        try:
            workspace = await get_dispatch_workspace(user_id, request.workspace)
        except WorkspaceNotFound as exc:
            raise HTTPException(status_code=404, detail="Workspace not found") from exc
        fetched = await fetch_before_start(workspace["workspace_id"], user_id=user_id)
        if not fetched["fetched"]:
            logger.warning(
                "dispatch workspace %s fetched before start failed: %s",
                workspace["workspace_id"],
                fetched["stderr"] or fetched["returncode"],
            )
        # Snapshot the checkout now. The row, not the registry, is what the
        # drive and every recovery read -- see ``DispatchService.start``.
        path = workspace.get("path")
        selection = {
            "workspace_id": workspace["workspace_id"],
            "repo_root": (
                str(Path(path).expanduser().resolve(strict=False)) if path else ""
            ),
            "sandbox_policy": workspace.get("sandbox_policy") or "",
            "default_worker": workspace.get("default_worker") or "",
        }
        if workspace.get("git_remote"):
            # L2: the push destination, snapshotted with the checkout.
            selection["git_remote"] = workspace["git_remote"]
    try:
        options: dict = {"workers": request.workers} if request.workers else {}
        if request.source_ref:
            options["source_ref"] = request.source_ref
            options["force"] = request.force
        dispatch = await _service().start(
            request.goal, user_id, workspace=selection, **options
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _summary(dispatch)


@router.get("/{dispatch_id}", response_model=DispatchSummary)
@handle_api_exceptions("load a dispatch", logger)
async def get_dispatch(dispatch_id: str, user_id: CurrentUserId) -> DispatchSummary:
    """One dispatch: its state, its lines and its integrate branch."""
    dispatch = await _owned(user_id, dispatch_id)
    return _summary(dispatch)


@router.get("/{dispatch_id}/cost")
@handle_api_exceptions("load a dispatch cost report", logger)
async def dispatch_cost(dispatch_id: str, user_id: CurrentUserId) -> dict:
    """N30: one dispatch's cost report, every recorded attempt included."""
    dispatch = await _owned(user_id, dispatch_id)
    report = cost_report(dispatch)
    if report is None:
        raise HTTPException(status_code=500, detail="Cost report unavailable")
    return report


@router.get("/{dispatch_id}/assignments", response_model=dict)
@handle_api_exceptions("load dispatch assignments", logger)
async def dispatch_assignments(dispatch_id: str, user_id: CurrentUserId) -> dict:
    """N73 L5: every recorded assignment entry per line, oldest first."""
    from src.tools.auto_dispatch.superagent_assign.record import assignment_entries

    dispatch = await _owned(user_id, dispatch_id)
    gates = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    cells = gates.get("lines") if isinstance(gates.get("lines"), dict) else {}
    lines: dict[str, list] = {}
    for line_id, snap in cells.items():
        if isinstance(snap, dict) and snap.get("assignment"):
            try:
                lines[str(line_id)] = assignment_entries(snap.get("assignment"))
            except (TypeError, ValueError):
                continue
    return {"dispatch_id": dispatch.id, "lines": lines}


@router.get("/{dispatch_id}/lines", response_model=list[dict])
@handle_api_exceptions("load dispatch lines", logger)
async def dispatch_lines(dispatch_id: str, user_id: CurrentUserId) -> list[dict]:
    """The lines of one dispatch, in the order they were planned."""
    dispatch = await _owned(user_id, dispatch_id)
    return [line.to_dict() for line in dispatch.lines]


@router.post("/{dispatch_id}/approve", response_model=DispatchSummary)
@handle_api_exceptions("approve a dispatch", logger)
async def approve_dispatch(
    dispatch_id: str, request: DispatchDecisionRequest, user_id: CurrentUserId
) -> DispatchSummary:
    """Approve a parked dispatch: the gate opens and the merge runs."""
    service = _service()
    await _owned(user_id, dispatch_id)
    try:
        dispatch = await service.approve(dispatch_id, request.note)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _summary(dispatch)


@router.post("/{dispatch_id}/reject", response_model=DispatchSummary)
@handle_api_exceptions("reject a dispatch", logger)
async def reject_dispatch(
    dispatch_id: str, request: DispatchDecisionRequest, user_id: CurrentUserId
) -> DispatchSummary:
    """Reject a parked dispatch: the integrate branch is moved aside.

    A dispatch parked on a planner failure never reached the gate, so there is
    no branch to move aside and nothing to merge: rejecting it ends the run
    instead, and the reason it parked with stays on the row.
    """
    service = _service()
    dispatch = await _owned(user_id, dispatch_id)
    try:
        if is_planner_failed(dispatch):
            dispatch = await service.cancel_planner_failed(dispatch_id, request.note)
        else:
            # ``force`` is passed only when set, so an unforced reject is the
            # same call it always was.
            options = {"force": True} if request.force else {}
            dispatch = await service.reject(dispatch_id, request.note, **options)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _summary(dispatch)


@router.post("/{dispatch_id}/cancel", response_model=DispatchSummary)
@handle_api_exceptions("cancel a dispatch", logger)
async def cancel_dispatch(
    dispatch_id: str, request: DispatchDecisionRequest, user_id: CurrentUserId
) -> DispatchSummary:
    """End a dispatch parked short of the gate: lines released, branches kept.

    A running line refuses unless ``force`` stops it first. A dispatch waiting
    at the approval gate refuses too: that decision is ``reject``. A planner
    failure ends the same way the reject route ends it.
    """
    service = _service()
    dispatch = await _owned(user_id, dispatch_id)
    try:
        if is_planner_failed(dispatch):
            dispatch = await service.cancel_planner_failed(dispatch_id, request.note)
        else:
            dispatch = await service.cancel(
                dispatch_id, request.note, force=request.force
            )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _summary(dispatch)


@router.post("/{dispatch_id}/abort", response_model=DispatchSummary)
@handle_api_exceptions("abort a dispatch", logger)
async def abort_dispatch(
    dispatch_id: str, request: DispatchDecisionRequest, user_id: CurrentUserId
) -> DispatchSummary:
    """End a dispatch in any live state (L2).

    At the approval gate this is a reject; a planner failure is cancelled as
    such; every other live state, PLANNING and MERGE included, is cancelled
    with its branches kept. ``force`` stops a running line or fences off the
    drive that owns the row. A terminal dispatch answers 409.
    """
    service = _service()
    await _owned(user_id, dispatch_id)
    try:
        dispatch = await service.abort(dispatch_id, request.note, force=request.force)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _summary(dispatch)


@router.post("/{dispatch_id}/deliver", response_model=DispatchSummary)
@handle_api_exceptions("deliver a dispatch", logger)
async def deliver_dispatch(dispatch_id: str, user_id: CurrentUserId) -> DispatchSummary:
    """Push an approved, COMPLETE dispatch and open its pull request (L2).

    The outcome is ``gates.delivery`` and the dispatch message. A missing
    GitHub token is a ``needs_token`` record, not an error; the token itself
    is read from the vault and never returned.
    """
    service = _service()
    await _owned(user_id, dispatch_id)
    try:
        dispatch = await service.deliver(dispatch_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _summary(dispatch)


@router.post("/{dispatch_id}/lines/{line_id}/reassign", response_model=DispatchSummary)
@handle_api_exceptions("reassign a dispatch line", logger)
async def reassign_line(
    dispatch_id: str,
    line_id: str,
    request: DispatchReassignRequest,
    user_id: CurrentUserId,
) -> DispatchSummary:
    """Override a blocked line's model and drive it from its next attempt."""
    service = _service()
    await _owned(user_id, dispatch_id)
    try:
        dispatch = await service.reassign(dispatch_id, line_id, request.model)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _summary(dispatch)


@router.post("/{dispatch_id}/lines/{line_id}/unblock", response_model=DispatchSummary)
@handle_api_exceptions("unblock a dispatch line", logger)
async def unblock_line(
    dispatch_id: str,
    line_id: str,
    request: DispatchNoteRequest,
    user_id: CurrentUserId,
) -> DispatchSummary:
    """Move a wedged line to a worker-exited checkpoint so it can be reassigned."""
    service = _service()
    await _owned(user_id, dispatch_id)
    try:
        dispatch = await service.unblock(dispatch_id, line_id, request.note)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _summary(dispatch)


@router.post(
    "/{dispatch_id}/lines/{line_id}/adjudicate", response_model=DispatchSummary
)
@handle_api_exceptions("adjudicate a dispatch line", logger)
async def adjudicate_line(
    dispatch_id: str,
    line_id: str,
    request: DispatchAdjudicateRequest,
    user_id: CurrentUserId,
) -> DispatchSummary:
    """Rule a reviewer scope conflict on a line in-scope or out-of-scope."""
    service = _service()
    await _owned(user_id, dispatch_id)
    try:
        dispatch = await service.adjudicate(
            dispatch_id, line_id, request.ruling, request.note
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _summary(dispatch)


@router.post("/{dispatch_id}/lines/{line_id}/recover", response_model=DispatchSummary)
@handle_api_exceptions("recover a dispatch line", logger)
async def recover_line(
    dispatch_id: str,
    line_id: str,
    request: DispatchRecoverRequest,
    user_id: CurrentUserId,
) -> DispatchSummary:
    """Run a parked line's next recovery step now, split it off, or hand it to a person."""
    service = _service()
    await _owned(user_id, dispatch_id)
    try:
        dispatch = await service.recover(
            dispatch_id, line_id, request.mode, request.note
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _summary(dispatch)


async def _owned(user_id: str, dispatch_id: str) -> Dispatch:
    """Load a dispatch the caller owns, or 404 — never 403, to avoid confirming ids."""
    dispatch = await _service().get(dispatch_id)
    if dispatch is None or dispatch.user_id != user_id:
        raise HTTPException(status_code=404, detail="Dispatch not found")
    return dispatch


__all__ = ["router"]
