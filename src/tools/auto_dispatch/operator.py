"""Operator decisions that move a wedged line or a parked dispatch (N36, N40, N41).

Three ways out a human can take when the drive cannot move on its own:

- ``unblock``: a line that stopped mid-implementation before N40 (or before
  N38b) has no ``worker_exited`` checkpoint, so Stage-Gate refuses a reassign.
  When no run of the line is out, the checkpoint is written and Stage-Gate
  records the pause; ``dispatch reassign`` is then accepted.
- ``adjudicate``: a reviewer scope or security conflict waits in
  ``awaiting_adjudication``. The human ruling is Stage-Gate's
  ``adjudicate_conflict`` event: in scope upholds the BLOCK (the finding
  becomes the author's fix round), out of scope overturns it (the review
  passes and the finding is kept as a follow-up).
- ``cancel`` / ``reject`` of a parked dispatch: the lines are released, the
  dispatch ends in ABORT and every branch is kept.
- ``recover`` (N76 L3): run the line's next recovery-table step now, skipping
  its backoff; ``--split`` splits the line off this round; ``--human`` hands
  it to a person and stops the automatic steps. The request is a marker on
  the line's gate cell; the drive's liveness sweep performs the step.
  ``--rereview`` (N87) is applied here: the operator fixed the branch, so the
  line goes back to G2 and review on its current head with no Worker run.

Every function here is a pure row write: the service applies it to a parked
row, and a running drive applies the same request from its queue.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from uuid import uuid4

from src.server.services.autonomy.stage_gate import Event, Handoff, Stage, advance

from .gates import (
    REVIEW_BLOCKED,
    REVIEW_MERGED,
    REVIEW_UNAVAILABLE,
    REVIEW_UNPARSEABLE,
    TWO_FAILED_ROUNDS,
)
from .machine import (
    ADJUDICATION,
    FOLLOW_UPS,
    HUMAN_REASSIGNS,
    REASSIGN_BRIEFS,
    REQUEST_ADJUDICATE,
    REQUEST_RECOVER,
    REQUEST_UNBLOCK,
    REVIEW_PARKED,
    REVIEWERS_QUOTA_EXHAUSTED,
    WORKER_EXITED,
    _load_state,
    _remember_event,
    _store_state,
    _worker_exited_checkpoint,
    keep_rejected_findings,
    line_has_pending_fix,
    record_worker_exited,
    request_kind,
)
from .review_triage import REVIEW_INCONCLUSIVE
from .types import Dispatch, DispatchState, Line, LineStatus, Outcome

OPERATOR_UNBLOCK = "operator_unblock"
IN_SCOPE = "in-scope"
OUT_OF_SCOPE = "out-of-scope"
# The Planner's ruling, as Stage-Gate's adjudication option.
RULINGS = {IN_SCOPE: "maintain_block", OUT_OF_SCOPE: "overturn_block"}
# A dispatch an operator may cancel: parked, or left mid-phase by a drive
# that is gone. APPROVAL has its own reject path.
CANCELLABLE = frozenset(
    {
        DispatchState.ASSIGNED,
        DispatchState.LAUNCHING,
        DispatchState.MONITORING,
        DispatchState.REVIEW,
    }
)
_NOTE_CHARS = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _note(text: str | None) -> str:
    return " ".join((text or "").split())[:_NOTE_CHARS]


def normalise_ruling(raw: object) -> str | None:
    """``in-scope`` / ``out-of-scope`` (underscores and case accepted), else None."""
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower().replace("_", "-")
    return value if value in RULINGS else None


def _snapshot(dispatch: Dispatch, line: Line) -> dict[str, object] | None:
    root = dispatch.gates if isinstance(dispatch.gates, dict) else None
    lines = root.get("lines") if isinstance(root, dict) else None
    snap = lines.get(line.id) if isinstance(lines, dict) else None
    return snap if isinstance(snap, dict) else None


def _audit(dispatch: Dispatch, entry: dict[str, object]) -> None:
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
    audit.append(entry)


def live_line_runs(dispatch: Dispatch, line: Line) -> set[str]:
    """Live launches of this line in this process (run ids carry dispatch and line)."""
    from .execution import live_runs_for_line

    return live_runs_for_line(dispatch.id, line.id)


def _run_settled(dispatch: Dispatch, line: Line) -> bool:
    """True when the scheduler recorded this line's current run as finished."""
    root = dispatch.gates if isinstance(dispatch.gates, dict) else None
    scheduler = root.get("scheduler") if isinstance(root, dict) else None
    settled = scheduler.get("settled_lines") if isinstance(scheduler, dict) else None
    if not isinstance(settled, list):
        return False
    for item in settled:
        if item == line.id:
            return True
        if (
            isinstance(item, dict)
            and item.get("line_id") == line.id
            and item.get("run_id") == line.current_run
            and item.get("attempt") == line.attempts
        ):
            return True
    return False


def running_line_ids(dispatch: Dispatch) -> list[str]:
    """Lines whose worker may still be out.

    A live launch in this process always counts. Otherwise only a line that
    is still RUNNING (or ASSIGNED with a reserved run) and whose run has not
    been recorded as finished counts. A PASSED, BLOCKED or FAILED line keeps
    its last run id on the row, and a RUNNING line whose run settled is only
    waiting for its review: neither has a worker out.
    """
    running: list[str] = []
    for line in dispatch.lines:
        if live_line_runs(dispatch, line):
            running.append(line.id)
            continue
        if not line.current_run:
            continue
        if line.status not in (LineStatus.RUNNING, LineStatus.ASSIGNED):
            continue
        if _run_settled(dispatch, line):
            continue
        running.append(line.id)
    return running


# -- unblock (N40) -------------------------------------------------------------


def unblock_refusal(dispatch: Dispatch, line: Line) -> str | None:
    """Why this line cannot be moved to a ``worker_exited`` checkpoint, or None.

    Only a parked line whose pause Stage-Gate still reads as mid-implementation
    qualifies, and only while no run of it is out: no ``current_run`` on the
    row and no live launch for it.
    """
    snap = _snapshot(dispatch, line)
    if snap is None:
        return "not_gated"
    if line.status not in (LineStatus.BLOCKED, LineStatus.FAILED):
        return "line_not_parked"
    if line.current_run or live_line_runs(dispatch, line):
        return "run_out"
    if line_has_pending_fix(dispatch, line.id):
        return "fix_pending"
    state = _load_state(snap)
    if _review_parked(snap, line, state) is not None:
        return None
    if _legacy_review_park(dispatch, snap, line, state) is not None:
        return None
    if line.failure_reason in _RESUMABLE_REVIEW_PARKS and _at_or_from_review(state):
        # A review park with no run of this attempt to resume on.
        return "no_settled_run"
    if _worker_exited_checkpoint(snap, line, dispatch):
        return "already_at_checkpoint"
    if state.stage is Stage.IMPLEMENTING:
        return None
    if (
        state.stage is Stage.BLOCKED
        and state.blocked_from == Stage.IMPLEMENTING.value
    ):
        return None
    if (
        line.failure_reason in _EXITED_RUN_REASONS
        and not state.stage.terminal
        and state.stage is not Stage.BLOCKED
    ):
        # Parked before N40 in another stage (a fix round that made no commit
        # waits in ``fixing``): record the same checkpoint and pause.
        return None
    return "line_not_wedged"


# Run ends that leave no worker out (N40); a line parked with one before the
# checkpoint existed is unblocked the same way.
_EXITED_RUN_REASONS = frozenset({"no_new_commit", "uncommitted_changes"})


_REVIEW_STAGES = (Stage.PRIMARY_REVIEW.value, Stage.SECONDARY_REVIEW.value)
# Review parks that ``unblock`` resumes at the review stage with a fresh review
# of the same run. ``review_unparseable`` (N75) is the reviewer's format, not
# the author's work, so the same work is simply reviewed again.
_RESUMABLE_REVIEW_PARKS = frozenset({REVIEW_UNAVAILABLE, REVIEW_UNPARSEABLE, REVIEW_INCONCLUSIVE})


def _review_parked(
    snap: dict[str, object], line: Line, state
) -> dict[str, object] | None:
    """The record of a review parked after its retry budget (N42), if it is this pause's."""
    record = snap.get(REVIEW_PARKED)
    if not isinstance(record, dict) or line.failure_reason not in (
        REVIEW_UNAVAILABLE,
        REVIEW_UNPARSEABLE,
        REVIEWERS_QUOTA_EXHAUSTED,
        REVIEW_INCONCLUSIVE,
    ):
        return None
    if record.get("attempt") != line.attempts or record.get("author") != (
        line.assigned_model or ""
    ):
        return None
    if state.stage is not Stage.BLOCKED or state.blocked_from not in _REVIEW_STAGES:
        return None
    run_id = record.get("run_id")
    return record if isinstance(run_id, str) and run_id else None


def _at_or_from_review(state) -> bool:
    return state.stage.value in _REVIEW_STAGES or (
        state.stage is Stage.BLOCKED and state.blocked_from in _REVIEW_STAGES
    )


def _settled_run(dispatch: Dispatch, line: Line) -> str | None:
    """The last run of this attempt the scheduler recorded as finished."""
    root = dispatch.gates if isinstance(dispatch.gates, dict) else None
    scheduler = root.get("scheduler") if isinstance(root, dict) else None
    settled = scheduler.get("settled_lines") if isinstance(scheduler, dict) else None
    found: str | None = None
    for item in settled if isinstance(settled, list) else []:
        if (
            isinstance(item, dict)
            and item.get("line_id") == line.id
            and item.get("attempt") == line.attempts
            and isinstance(item.get("run_id"), str)
            and item["run_id"]
        ):
            found = item["run_id"]
    return found


def _legacy_review_park(
    dispatch: Dispatch, snap: dict[str, object], line: Line, state
) -> dict[str, object] | None:
    """A review park with no markers on the snapshot.

    A ``review_unavailable`` park from before N42, or a ``review_unparseable``
    one from before N75. The old code blocked the line and left Stage-Gate at
    the review stage (or a later pause blocked it from there). The work was
    submitted and its run settled; that run, for the line's current attempt,
    is what the review resumes on.
    """
    if line.failure_reason not in _RESUMABLE_REVIEW_PARKS:
        return None
    if snap.get(REVIEW_PARKED) is not None or snap.get("review_retry") is not None:
        return None
    if not _at_or_from_review(state):
        return None
    run_id = _settled_run(dispatch, line)
    if run_id is None:
        return None
    return {"run_id": run_id, "attempt": line.attempts, "legacy": True}


def _resume_review(
    dispatch: Dispatch, line: Line, snap: dict[str, object], record, note: str
) -> str | None:
    """Back to the review stage with the run that finished, for a fresh retry budget.

    A pause (Stage-Gate ``blocked`` from a review stage) is resumed only if
    Stage-Gate accepts it. A legacy park that left Stage-Gate at the review
    stage needs no transition: the line is only made reviewable again.
    """
    stamp = _now()
    state = _load_state(snap)
    to_stage = state.stage.value
    if state.stage is Stage.BLOCKED:
        transition = advance(
            state, Event(kind="resume", timestamp=stamp, provider="operator")
        )
        if not transition.accepted:
            return transition.reason or "resume_refused"
        if transition.state.stage.value not in _REVIEW_STAGES:
            return "resume_not_to_review"
        _remember_event(snap, transition)
        _store_state(snap, transition.state)
        to_stage = transition.state.stage.value
    elif state.stage.value not in _REVIEW_STAGES:
        return "resume_not_to_review"
    parked_reason = line.failure_reason or REVIEW_UNAVAILABLE
    line.status = LineStatus.RUNNING
    line.current_run = str(record["run_id"])
    line.failure_reason = None
    line.blockers = []
    snap.pop(REVIEW_PARKED, None)
    snap.pop(REVIEWERS_QUOTA_EXHAUSTED, None)
    snap.pop("review_retry", None)
    snap.pop("review_attempts", None)
    _audit(
        dispatch,
        {
            "kind": OPERATOR_UNBLOCK,
            "line_id": line.id,
            "from_stage": state.stage.value,
            "to_stage": to_stage,
            "failure_reason": parked_reason,
            "legacy": bool(record.get("legacy")),
            "note": _note(note),
            "at": stamp,
        },
    )
    return None


def apply_unblock(dispatch: Dispatch, line: Line, *, note: str = "") -> str | None:
    """Write the checkpoint and the Stage-Gate pause. The line stays parked as it was.

    A review parked ``review_unavailable`` (N42) is resumed instead: the line
    goes back to its review stage and the drive retries the review.
    """
    refused = unblock_refusal(dispatch, line)
    if refused:
        return refused
    snap = _snapshot(dispatch, line)
    assert snap is not None
    state = _load_state(snap)
    parked = _review_parked(snap, line, state) or _legacy_review_park(
        dispatch, snap, line, state
    )
    if parked is not None:
        # Stage-Gate decides; a refused resume leaves the line parked as it is.
        return _resume_review(dispatch, line, snap, parked, note)
    stamp = _now()
    before = _load_state(snap).stage.value
    record_worker_exited(
        dispatch,
        line,
        snap,
        run_id=f"{dispatch.id}:{line.id}:{line.attempts}:unblock-{uuid4().hex[:8]}",
        failure_class=OPERATOR_UNBLOCK,
        exit_code=None,
        stamp=stamp,
        note=note,
    )
    _audit(
        dispatch,
        {
            "kind": OPERATOR_UNBLOCK,
            "line_id": line.id,
            "from_stage": before,
            "failure_reason": line.failure_reason,
            "note": _note(note),
            "at": stamp,
        },
    )
    return None


# -- adjudicate (N41) ----------------------------------------------------------


def _conflict(snap: dict[str, object], line: Line) -> str:
    record = snap.get(ADJUDICATION)
    if isinstance(record, dict) and record.get("conflict") in ("scope", "security"):
        return str(record["conflict"])
    return "security" if line.failure_reason == "reviewer_conflict_security" else "scope"


def _findings(snap: dict[str, object], line: Line) -> list[str]:
    record = snap.get(ADJUDICATION)
    raw = record.get("findings") if isinstance(record, dict) else None
    items = [str(item) for item in raw] if isinstance(raw, list) else list(line.blockers)
    items = [item for item in items if item.strip()]
    if not items:
        # A conflict parked before findings were kept on the snapshot.
        items = [
            f"The reviewer raised a {_conflict(snap, line)} conflict; its finding was "
            "not recorded. Read the review history for this line."
        ]
    return items


def _other_blockers(snap: dict[str, object]) -> list[str]:
    """Blockers of the same verdict that are not part of the conflict."""
    record = snap.get(ADJUDICATION)
    raw = record.get("other_blockers") if isinstance(record, dict) else None
    return [str(item) for item in raw if str(item).strip()] if isinstance(raw, list) else []


def _other_count(snap: dict[str, object]) -> int:
    """How many other blockers there were, counted before any cap."""
    record = snap.get(ADJUDICATION)
    count = record.get("other_blocker_count") if isinstance(record, dict) else None
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        return max(count, len(_other_blockers(snap)))
    return len(_other_blockers(snap))


def _option(chosen: str, others: int) -> str:
    """Stage-Gate's option for this ruling.

    Out of scope overturns the BLOCK only when the conflict was all the
    reviewer raised. Any other blocker in the same verdict still stands, so
    the BLOCK is kept and the author fixes those, with the conflict finding
    filed as a follow-up.
    """
    if chosen == OUT_OF_SCOPE and not others:
        return RULINGS[OUT_OF_SCOPE]
    return RULINGS[IN_SCOPE]


def adjudication_refusal(
    dispatch: Dispatch, line: Line, ruling: object, note: str = ""
) -> str | None:
    """Why this ruling cannot be applied, or None. Nothing is written."""
    chosen = normalise_ruling(ruling)
    if chosen is None:
        return "invalid_ruling"
    snap = _snapshot(dispatch, line)
    if snap is None:
        return "not_gated"
    state = _load_state(snap)
    if state.stage is not Stage.AWAITING_ADJUDICATION:
        return "not_awaiting_adjudication"
    if line.current_run or line.status is LineStatus.RUNNING:
        return "run_out"
    if _conflict(snap, line) == "security" and chosen == OUT_OF_SCOPE and not _note(note):
        return "security_out_of_scope_needs_note"
    option = _option(chosen, _other_count(snap))
    transition = advance(state, _adjudication_event(state, option))
    if not transition.accepted:
        return transition.reason or "adjudication_refused"
    if transition.state.stage not in (
        Stage.FIXING,
        Stage.REPLANNING,
        Stage.INTEGRATION_CHECKS,
    ):
        # v1 raises conflicts only at the secondary review, so an overturn
        # never owes another review. Refuse rather than strand the line.
        return "adjudication_unsupported"
    return None


def _adjudication_event(state, option: str) -> Event:
    return Event(
        kind="adjudicate_conflict",
        timestamp=_now(),
        provider=state.conflict_reviewer,
        option=option,
    )


def _follow_up(
    dispatch: Dispatch,
    line: Line,
    *,
    conflict: str,
    reviewer: str,
    findings: list[str],
    note: str,
    stamp: str,
) -> None:
    if not isinstance(dispatch.gates, dict):
        dispatch.gates = {}
    follow_ups = dispatch.gates.get(FOLLOW_UPS)
    if not isinstance(follow_ups, list):
        follow_ups = []
        dispatch.gates[FOLLOW_UPS] = follow_ups
    follow_ups.append(
        {
            "line_id": line.id,
            "conflict": conflict,
            "reviewer": reviewer,
            "findings": findings,
            "note": note,
            "at": stamp,
        }
    )


def apply_adjudication(
    dispatch: Dispatch, line: Line, ruling: object, *, note: str = ""
) -> str | None:
    """Send the human ruling to Stage-Gate and move the line accordingly.

    In scope: the conflict finding and every other blocker of that verdict
    are the line's blockers and the same author gets a fix round on its
    branch (``fixing``; ``replanning`` after a second failed round, where
    ``dispatch reassign`` is the way on). Out of scope: the conflict finding
    is kept as a follow-up on the dispatch. When it was all the reviewer
    raised, the review counts as passed and the line PASSES; when the same
    verdict had other blockers, those still stand and the author fixes them.
    """
    refused = adjudication_refusal(dispatch, line, ruling, note)
    if refused:
        return refused
    chosen = normalise_ruling(ruling)
    assert chosen is not None
    snap = _snapshot(dispatch, line)
    assert snap is not None
    state = _load_state(snap)
    conflict = _conflict(snap, line)
    findings = _findings(snap, line)
    others = _other_blockers(snap)
    note_text = _note(note)
    transition = advance(state, _adjudication_event(state, _option(chosen, _other_count(snap))))
    if not transition.accepted:  # adjudication_refusal ran the same advance
        return transition.reason or "adjudication_refused"
    _remember_event(snap, transition)
    _store_state(snap, transition.state)
    landed = transition.state.stage
    stamp = _now()
    record = snap.get(ADJUDICATION)
    record = dict(record) if isinstance(record, dict) else {"conflict": conflict}
    record.update(
        {"ruling": chosen, "note": note_text, "decided_at": stamp, "findings": findings}
    )
    snap[ADJUDICATION] = record
    line.current_run = None
    if chosen == OUT_OF_SCOPE:
        _follow_up(
            dispatch,
            line,
            conflict=conflict,
            reviewer=state.conflict_reviewer,
            findings=findings,
            note=note_text,
            stamp=stamp,
        )
    if landed is Stage.INTEGRATION_CHECKS:
        line.status = LineStatus.PASSED
        line.blockers = []
        line.failure_reason = None
        snap["reason"] = REVIEW_MERGED
        dispatch.outcomes[line.id] = Outcome(
            line_id=line.id,
            model=line.assigned_model or "",
            duration_secs=0,
            tokens=0,
            passed=True,
            reviewer_verdict="merge",
        )
    else:
        if chosen == IN_SCOPE:
            blockers = [*findings, *others]
            if note_text:
                blockers.append(f"Planner ruling (in scope): {note_text}")
        else:
            blockers = list(others)
            blockers.append(
                "Planner ruling (out of scope, filed as a follow-up, not to fix "
                "here): " + "; ".join(findings) + (f" -- {note_text}" if note_text else "")
            )
        line.blockers = blockers
        # What the author must fix survives a later park (N79): the conflict
        # finding when it was ruled in scope, the other blockers either way.
        keep_rejected_findings(
            snap,
            line,
            [*findings, *others] if chosen == IN_SCOPE else list(others),
            reviewer=state.conflict_reviewer or "",
            at=stamp,
            head=record.get("head") or "",
        )
        if landed is Stage.FIXING:
            # The author's fix round: relaunched as a ready line, its prompt
            # built from these blockers on the existing branch.
            line.status = LineStatus.ASSIGNED
            line.failure_reason = None
            snap["reason"] = REVIEW_BLOCKED
            if not isinstance(dispatch.gates, dict):
                dispatch.gates = {}
            briefs = dispatch.gates.get(REASSIGN_BRIEFS)
            if not isinstance(briefs, dict):
                briefs = {}
                dispatch.gates[REASSIGN_BRIEFS] = briefs
            briefs[line.id] = {
                "author": line.assigned_model or "",
                "attempt": line.attempts,
                "blockers": copy.deepcopy(blockers),
            }
        else:
            line.status = LineStatus.BLOCKED
            line.failure_reason = TWO_FAILED_ROUNDS
            snap["reason"] = TWO_FAILED_ROUNDS
    snap.pop(WORKER_EXITED, None)
    _audit(
        dispatch,
        {
            "kind": "human_adjudication",
            "line_id": line.id,
            "ruling": chosen,
            "conflict": conflict,
            "to_stage": landed.value,
            "other_blockers": len(others),
            "note": note_text,
            "at": stamp,
        },
    )
    return None


# -- recover (N76 L3) ----------------------------------------------------------

RECOVER_NEXT = "next"
RECOVER_SPLIT = "split"
RECOVER_HUMAN = "human"
#: N87: the operator fixed the branch; re-run G2 and review on its head.
RECOVER_REREVIEW = "rereview"
RECOVER_MODES = frozenset({RECOVER_NEXT, RECOVER_SPLIT, RECOVER_HUMAN, RECOVER_REREVIEW})
#: The gate-cell key a recover request leaves for the drive's sweep.
RECOVER_MARKER = "liveness_operator"
OPERATOR_HUMAN_REASON = "operator_recover_human"
_IN_FLIGHT = frozenset(
    {LineStatus.PENDING, LineStatus.ASSIGNED, LineStatus.RUNNING, LineStatus.REVIEWING}
)


def normalise_recover_mode(raw: object) -> str | None:
    """``next`` (the default), ``split``, ``human`` or ``rereview``; else None."""
    if raw is None or raw == "":
        return RECOVER_NEXT
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    return value if value in RECOVER_MODES else None


def _liveness_policy():
    from .liveness import policy as liveness_policy

    try:
        return liveness_policy.load_policy()
    except liveness_policy.PolicyError:
        return liveness_policy.default_policy()


def recover_plan(dispatch: Dispatch, line: Line, table=None) -> dict[str, object]:
    """What ``recover`` would do for this line now, read from its liveness cell.

    ``next_step`` is the recovery-table row the next step runs (``human`` when
    a person owns the line or the table has nothing left), ``budget_left`` the
    automatic steps still allowed, ``next_at`` when the sweep would have run
    it on its own. Pure: nothing is written.
    """
    from .liveness import policy as liveness_policy
    from .liveness import record as liveness_record
    from .machine import _liveness_plan_index

    table = table if table is not None else _liveness_policy()
    snap = _snapshot(dispatch, line) or {}
    record = liveness_record.load(snap)
    reason = (record.reason if record is not None else None) or line.failure_reason
    group = table.group_for(reason)
    plan = table.plan_for(reason)
    if record is None:
        budget_left = table.max_steps if group != liveness_policy.HUMAN else 0
        index = 0
        needs_human = False
        next_at = None
    else:
        budget_left = record.budget_left
        index = _liveness_plan_index(record, group)
        needs_human = record.needs_human
        next_at = record.next_at
    if needs_human or not plan or index >= len(plan) or budget_left <= 0:
        step = RECOVER_HUMAN
    else:
        step = str(plan[index].action)
        if record is not None and record.next_action and record.next_action != "repair":
            step = record.next_action
    return {
        "reason": reason,
        "group": group,
        "next_step": step,
        "next_at": next_at,
        "budget_left": budget_left,
        "needs_human": needs_human,
    }


def _split_would_wait(dispatch: Dispatch, line: Line) -> bool:
    from .machine import _split_waits_for

    return bool(_split_waits_for(dispatch, line.id))


def recover_refusal(
    dispatch: Dispatch, line: Line, mode: object = RECOVER_NEXT
) -> str | None:
    """Why ``recover`` cannot run on this line now; None when it can.

    Only a gated, parked (BLOCKED) line with no run out is recovered. ``next``
    on a line a person owns, or whose table and budget are spent, answers
    ``needs_human``: there is no automatic step left, use ``--split``,
    reassign, adjudicate or cancel. A split that would leave nothing PASSED
    while other lines still run answers ``split_waits``.
    """
    chosen = normalise_recover_mode(mode)
    if chosen is None:
        return "invalid_mode"
    if _snapshot(dispatch, line) is None:
        return "not_gated"
    if line.status is LineStatus.FAILED and line.failure_reason == "split_off":
        return "already_split"
    if line.status is not LineStatus.BLOCKED:
        return "line_not_parked"
    if line.current_run or live_line_runs(dispatch, line):
        return "run_out"
    snap = _snapshot(dispatch, line) or {}
    if isinstance(snap.get(RECOVER_MARKER), dict):
        return "recover_pending"
    plan = recover_plan(dispatch, line)
    if chosen == RECOVER_HUMAN:
        return "already_needs_human" if plan["needs_human"] else None
    if chosen == RECOVER_REREVIEW:
        return rereview_refusal(dispatch, line)
    if chosen == RECOVER_SPLIT:
        return "split_waits" if _split_would_wait(dispatch, line) else None
    if plan["next_step"] == RECOVER_HUMAN:
        return "needs_human"
    if plan["next_step"] == RECOVER_SPLIT and _split_would_wait(dispatch, line):
        # The next row is the split, and it would wait for running lines.
        return "split_waits"
    return None


def apply_recover(
    dispatch: Dispatch, line: Line, mode: object = RECOVER_NEXT, *, note: str = ""
) -> str | None:
    """Leave the request for the sweep, or hand the line to a person now.

    ``human`` is complete here: the liveness record is latched to a person,
    with its one notification stamped (the operator is that person), so no
    automatic step runs again. ``next`` and ``split`` write the marker the
    drive's sweep consumes on its next pass, in every liveness mode: an
    operator request is not a shadow observation.
    """
    refused = recover_refusal(dispatch, line, mode)
    if refused:
        return refused
    from .liveness import record as liveness_record

    chosen = normalise_recover_mode(mode) or RECOVER_NEXT
    if chosen == RECOVER_REREVIEW:
        return apply_rereview(dispatch, line, note=note)
    snap = _snapshot(dispatch, line)
    assert snap is not None
    moment = _now()
    text = _note(note)
    if chosen == RECOVER_HUMAN:
        table = _liveness_policy()
        current = liveness_record.load(snap)
        if current is None:
            reason = line.failure_reason or "liveness_inconsistent"
            current = liveness_record.new_record(
                reason, table.group_for(reason), budget=0, at=moment
            )
        latched = current.mark_needs_human(OPERATOR_HUMAN_REASON, at=moment)
        snap.update(liveness_record.store(snap, latched))
        line.blockers = [
            *[str(item) for item in line.blockers],
            "A person took this line over (dispatch recover --human); "
            "no automatic recovery step runs again.",
        ]
    else:
        snap[RECOVER_MARKER] = {"mode": chosen, "at": moment, "note": text}
    _audit(
        dispatch,
        {
            "kind": "operator_recover",
            "line_id": line.id,
            "mode": chosen,
            "note": text,
            "at": moment,
        },
    )
    return None


# -- recover --rereview (N87) ---------------------------------------------------

#: The Stage-Gate provider an operator rereview records as the developer when
#: the line has to leave ``replanning`` (a swap needs a different provider).
OPERATOR_PROVIDER = "operator"
OPERATOR_REREVIEW = "operator_rereview"
#: Stages from which the drive runs G2 and then the review once the line's
#: run is settled: G2 itself, a fix waiting on G2, or a review stage.
_REREVIEW_TARGETS = frozenset(
    {Stage.FOCUSED_TESTS, Stage.FIXING, Stage.PRIMARY_REVIEW, Stage.SECONDARY_REVIEW}
)


def _rereview_handoff(line: Line) -> Handoff:
    files = tuple(str(item) for item in (line.write_scope or ()) if str(item).strip())
    return Handoff(
        branch=line.branch or "",
        commit="the branch head the operator left",
        files=files or ("(none recorded)",),
        test_command="G2 reruns on the branch head",
        results="the operator fixed the branch outside the dispatch",
        known_limitations="operator rereview; no Worker ran this attempt",
    )


def _rereview_walk(line: Line, state, stamp: str):
    """Legal Stage-Gate steps from the parked stage to G2 or review.

    Returns ``(transitions, refusal)``. Pure: ``state`` is not changed. A
    pause is resumed; ``replanning`` takes its ``replace_developer`` option
    with the operator as the developer (only if that option was offered);
    ``assigned`` starts and ``implementing`` submits the branch as it is.
    """
    transitions = []
    current = state
    for _ in range(6):
        if current.stage in _REREVIEW_TARGETS:
            return transitions, None
        if current.stage is Stage.BLOCKED:
            event = Event(kind="resume", timestamp=stamp, provider=OPERATOR_PROVIDER)
        elif current.stage is Stage.REPLANNING:
            if "replace_developer" not in (current.replan_options or ()):
                return transitions, "rereview_not_offered"
            provider = OPERATOR_PROVIDER
            if current.developer_provider == provider:
                provider = f"{OPERATOR_PROVIDER}-rereview"
            event = Event(
                kind="replan_decision",
                timestamp=stamp,
                option="replace_developer",
                provider=provider,
                handoff=_rereview_handoff(line),
            )
        elif current.stage is Stage.ASSIGNED:
            event = Event(
                kind="start_implementation",
                timestamp=stamp,
                provider=current.developer_provider or OPERATOR_PROVIDER,
            )
        elif current.stage is Stage.IMPLEMENTING:
            event = Event(
                kind="submit_work",
                timestamp=stamp,
                provider=current.developer_provider or OPERATOR_PROVIDER,
                handoff=_rereview_handoff(line),
            )
        else:
            return transitions, f"rereview_not_reachable:{current.stage.value}"
        transition = advance(current, event)
        if not transition.accepted:
            return transitions, transition.reason or "rereview_refused"
        transitions.append(transition)
        current = transition.state
    return transitions, "rereview_not_reachable"


def rereview_refusal(dispatch: Dispatch, line: Line) -> str | None:
    """Why ``recover --rereview`` cannot run on this line now; None when it can.

    The line must be gated, parked (BLOCKED), have a branch, no run out, no
    pending fix or recover request, and its stage must reach G2 or review
    through legal Stage-Gate steps.
    """
    snap = _snapshot(dispatch, line)
    if snap is None:
        return "not_gated"
    if line.status is LineStatus.FAILED and line.failure_reason == "split_off":
        return "already_split"
    if line.status is not LineStatus.BLOCKED:
        return "line_not_parked"
    if line.current_run or live_line_runs(dispatch, line):
        return "run_out"
    if line_has_pending_fix(dispatch, line.id):
        return "fix_pending"
    if isinstance(snap.get(RECOVER_MARKER), dict):
        return "recover_pending"
    if not (line.branch or "").strip():
        return "no_branch"
    _, refused = _rereview_walk(line, _load_state(snap), _now())
    return refused


def _settle_rereview_run(dispatch: Dispatch, line: Line) -> None:
    """Record the line's (synthetic) run as finished, as ``mark_run_settled`` does."""
    if not isinstance(dispatch.gates, dict):
        dispatch.gates = {}
    scheduler = dispatch.gates.setdefault("scheduler", {})
    if not isinstance(scheduler, dict):
        scheduler = {}
        dispatch.gates["scheduler"] = scheduler
    settled = scheduler.get("settled_lines")
    kept = [
        item
        for item in (settled if isinstance(settled, list) else [])
        if item != line.id
        and not (isinstance(item, dict) and item.get("line_id") == line.id)
    ]
    kept.append(
        {
            "dispatch_id": dispatch.id,
            "line_id": line.id,
            "run_id": line.current_run,
            "attempt": line.attempts,
        }
    )
    scheduler["settled_lines"] = kept


def _close_liveness_episode(dispatch: Dispatch, line: Line, snap: dict, stamp: str) -> None:
    from .liveness import record as liveness_record
    from .machine import release_human_latch

    if release_human_latch(dispatch, line):
        return
    current = liveness_record.load(snap)
    if current is None or current.outcome != liveness_record.PENDING:
        return
    snap.update(
        liveness_record.store(snap, current.finish(liveness_record.RECOVERED, at=stamp))
    )


def apply_rereview(dispatch: Dispatch, line: Line, *, note: str = "") -> str | None:
    """The operator fixed the branch: send the line back to G2 and review (N87).

    No Worker is launched. Stage-Gate is moved to G2 (or the review stage it
    paused in) through legal events, the G2 result is reset to pending so it
    runs again on the current branch head, and the line is RUNNING with a
    settled synthetic run, which the drive adopts like a finished run: G2
    runs, then the review. A refusal leaves the line as it was.
    """
    refused = rereview_refusal(dispatch, line)
    if refused:
        return refused
    snap = _snapshot(dispatch, line)
    assert snap is not None
    stamp = _now()
    state = _load_state(snap)
    transitions, refused = _rereview_walk(line, state, stamp)
    if refused:
        return refused
    for transition in transitions:
        _remember_event(snap, transition)
        _store_state(snap, transition.state)
    landed = transitions[-1].state.stage.value if transitions else state.stage.value
    parked_reason = line.failure_reason
    cells = snap.setdefault("gates", {})
    if isinstance(cells, dict):
        previous = cells.get("G2")
        cells["G2"] = {
            "result": "pending",
            "reason": None,
            "at": stamp,
            "commands": [],
            "attempt": line.attempts,
            "revision": None,
            # A fresh G2 fix budget: the operator's change is new work.
            "fix_launches": 0,
            **({"previous": previous.get("result")} if isinstance(previous, dict) else {}),
        }
    for key in (REVIEW_PARKED, REVIEWERS_QUOTA_EXHAUSTED, "review_retry", "review_attempts"):
        snap.pop(key, None)
    snap["reason"] = OPERATOR_REREVIEW
    line.status = LineStatus.RUNNING
    line.current_run = f"{dispatch.id}:{line.id}:{line.attempts}:rereview-{uuid4().hex[:8]}"
    line.failure_reason = None
    line.blockers = []
    _settle_rereview_run(dispatch, line)
    _close_liveness_episode(dispatch, line, snap, stamp)
    _audit(
        dispatch,
        {
            "kind": OPERATOR_REREVIEW,
            "line_id": line.id,
            "from_stage": state.stage.value,
            "to_stage": landed,
            "failure_reason": parked_reason,
            "note": _note(note),
            "at": stamp,
        },
    )
    return None


def liveness_status(dispatch: Dispatch, *, now: datetime | None = None) -> dict[str, object]:
    """Owner, next step, next time and budget of every open line, for status.

    ``needs_human`` lists the lines a person owns. A dispatch without gates
    (the ungated path) has nothing to show.
    """
    from .liveness import owner as liveness_owner
    from .liveness import record as liveness_record
    from .machine import pending_human_reassigns

    lines: dict[str, dict[str, object]] = {}
    people: list[str] = []
    if not isinstance(dispatch.gates, dict) or not isinstance(dispatch.gates.get("lines"), dict):
        return {"lines": lines, "needs_human": people}
    moment = now or datetime.now(timezone.utc)
    queued = pending_human_reassigns(dispatch)
    table = _liveness_policy()
    for line in dispatch.lines:
        if line.status in (LineStatus.PASSED, LineStatus.FAILED):
            continue
        snap = _snapshot(dispatch, line) or {}
        view = line.to_dict()
        for key in ("review_retry", "review", "liveness"):
            if key in snap:
                view[key] = snap.get(key)
        if line.id in queued:
            view["queued_request"] = {"state": "pending"}
        try:
            decision = liveness_owner.decide_owner(view, moment)
            owner_name = decision.owner
            deadline = decision.deadline.isoformat() if decision.deadline else None
        except Exception:  # noqa: BLE001 - a status read never fails on one cell
            owner_name, deadline = liveness_owner.OWNER_NONE, None
        plan = recover_plan(dispatch, line, table)
        record = liveness_record.load(snap)
        last = record.history[-1] if record is not None and record.history else None
        marker = snap.get(RECOVER_MARKER)
        entry = {
            "status": line.status.value,
            "owner": owner_name,
            "deadline": deadline,
            "next_step": plan["next_step"] if line.status is LineStatus.BLOCKED else None,
            "next_at": plan["next_at"],
            "budget_left": plan["budget_left"],
            "group": plan["group"],
            "needs_human": bool(plan["needs_human"]),
            "human_reason": record.human_reason if record is not None else "",
            "last_step": (
                {"action": last.action, "result": last.result, "detail": last.detail}
                if last is not None
                else None
            ),
            "recover_pending": marker.get("mode") if isinstance(marker, dict) else None,
        }
        lines[line.id] = entry
        if entry["needs_human"]:
            people.append(line.id)
    return {"lines": lines, "needs_human": people}


# -- the queue -----------------------------------------------------------------


def decision_refusal(
    dispatch: Dispatch, line: Line, request: dict[str, object]
) -> str | None:
    """The refusal for a queued unblock or adjudication, checked against the line now."""
    kind = request_kind(request)
    if kind == REQUEST_UNBLOCK:
        return unblock_refusal(dispatch, line)
    if kind == REQUEST_ADJUDICATE:
        return adjudication_refusal(
            dispatch, line, request.get("ruling"), str(request.get("note") or "")
        )
    if kind == REQUEST_RECOVER:
        return recover_refusal(dispatch, line, request.get("mode"))
    return "invalid_request"


def apply_decision(
    dispatch: Dispatch, line: Line, request: dict[str, object]
) -> str | None:
    kind = request_kind(request)
    note = str(request.get("note") or "")
    if kind == REQUEST_UNBLOCK:
        return apply_unblock(dispatch, line, note=note)
    if kind == REQUEST_ADJUDICATE:
        return apply_adjudication(dispatch, line, request.get("ruling"), note=note)
    if kind == REQUEST_RECOVER:
        return apply_recover(dispatch, line, request.get("mode"), note=note)
    return "invalid_request"


# -- cancel / reject a parked dispatch (N36) -----------------------------------


def cancel_refusal(dispatch: Dispatch, *, verb: str, force: bool) -> str | None:
    if dispatch.state not in CANCELLABLE:
        return (
            f"Dispatch {dispatch.id} is {dispatch.state.value}; {verb} needs a "
            "dispatch parked in REVIEW, LAUNCHING, MONITORING or ASSIGNED"
        )
    running = running_line_ids(dispatch)
    if running and not force:
        return (
            f"line_running: {', '.join(running)} may still have a run out; "
            "pass --force to stop it first"
        )
    return None


def cancel_parked(dispatch: Dispatch, *, note: str = "", verb: str = "cancel") -> None:
    """End a parked dispatch in place: lines released, branches kept.

    Each unfinished line is FAILED with ``dispatch_cancelled`` (or
    ``dispatch_rejected``) and its Stage-Gate task is cancelled; queued human
    requests, fix intents and serial waits are dropped. Nothing is merged or
    moved aside: every line branch and the integrate branch stay as they are.
    """
    stamp = _now()
    word = "rejected" if verb == "reject" else "cancelled"
    released: list[dict[str, object]] = []
    for line in dispatch.lines:
        if line.status is LineStatus.PASSED:
            continue
        released.append(
            {
                "line_id": line.id,
                "status": line.status.value,
                "failure_reason": line.failure_reason,
                "current_run": line.current_run,
            }
        )
        line.status = LineStatus.FAILED
        line.failure_reason = f"dispatch_{word}"
        line.current_run = None
        snap = _snapshot(dispatch, line)
        if snap is None:
            continue
        state = _load_state(snap)
        if state.stage.terminal:
            continue
        transition = advance(
            state, Event(kind="cancel", timestamp=stamp, provider="operator")
        )
        if transition.accepted:
            _remember_event(snap, transition)
            _store_state(snap, transition.state)
        snap.pop(WORKER_EXITED, None)
    if isinstance(dispatch.gates, dict):
        dispatch.gates[HUMAN_REASSIGNS] = {}
        dispatch.gates.pop("pending_fix", None)
        scheduler = dispatch.gates.get("scheduler")
        if isinstance(scheduler, dict):
            scheduler["pending_relaunch"] = []
    previous = dispatch.state.value
    note_text = _note(note)
    _audit(
        dispatch,
        {
            "kind": f"operator_{verb}",
            "from_state": previous,
            "released": released,
            "note": note_text,
            "at": stamp,
        },
    )
    message = (
        f"An operator {word} this dispatch while it was {previous}; "
        "the line branches are kept"
    )
    if note_text:
        message = f"{message}: {note_text[:200]}"
    dispatch.state = DispatchState.ABORT
    dispatch.message = message


__all__ = [
    "CANCELLABLE",
    "IN_SCOPE",
    "OPERATOR_UNBLOCK",
    "OUT_OF_SCOPE",
    "RECOVER_HUMAN",
    "RECOVER_MARKER",
    "RECOVER_MODES",
    "RECOVER_NEXT",
    "RECOVER_REREVIEW",
    "RECOVER_SPLIT",
    "adjudication_refusal",
    "apply_adjudication",
    "apply_decision",
    "apply_recover",
    "apply_rereview",
    "apply_unblock",
    "cancel_parked",
    "cancel_refusal",
    "decision_refusal",
    "liveness_status",
    "normalise_recover_mode",
    "normalise_ruling",
    "recover_plan",
    "recover_refusal",
    "rereview_refusal",
    "running_line_ids",
    "unblock_refusal",
]
