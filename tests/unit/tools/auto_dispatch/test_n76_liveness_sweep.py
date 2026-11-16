"""N76: the liveness sweep owns a parked line after its owner is gone.

The sweep runs from the gated drive. ``off`` is main-identical, ``shadow``
records the step it would take, and ``enforce`` takes it under the per-line
budget. These tests call it directly with an injected clock so no drive has to
sleep.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.server.services.autonomy.stage_gate import Stage
from src.tools.auto_dispatch import DispatchMachine, MachineConfig
from src.tools.auto_dispatch.liveness import modes, record
from src.tools.auto_dispatch.machine import _load_state, _store_state
from src.tools.auto_dispatch.types import DispatchState, LineStatus

from .n76_fakes import make_dispatch, make_gates, make_line, make_machine, park_line, stored

pytestmark = pytest.mark.asyncio

WHEN = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


def _snapshot(gates, dispatch, line):
    return gates.line_snapshot(dispatch, line)


async def test_off_mode_leaves_a_parked_line_exactly_as_it_was(tmp_path) -> None:
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=modes.OFF)
    )
    line = make_line()
    dispatch = await stored(collaborators, make_dispatch(line))
    snapshot = park_line(gates, dispatch, line, "no_new_commit")

    assert await machine._sweep_liveness(dispatch, now=WHEN) == 0

    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == "no_new_commit"
    assert record.load(snapshot) is None
    assert "liveness" not in snapshot


async def test_shadow_records_the_step_it_would_have_taken(tmp_path) -> None:
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=modes.SHADOW)
    )
    line = make_line()
    dispatch = await stored(collaborators, make_dispatch(line))
    snapshot = park_line(gates, dispatch, line, "no_new_commit")

    assert await machine._sweep_liveness(dispatch, now=WHEN) >= 1

    observed = record.load(snapshot)
    assert observed is not None
    assert observed.spent == 0
    assert observed.shadowed == 1
    assert observed.outcome == record.PENDING
    assert [step.action for step in observed.history] == ["reassign"]
    assert observed.history[0].result == record.SHADOW
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == "no_new_commit"


async def test_shadow_does_not_spend_the_enforcement_budget(tmp_path) -> None:
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=modes.SHADOW)
    )
    line = make_line()
    dispatch = await stored(collaborators, make_dispatch(line))
    park_line(gates, dispatch, line, "no_new_commit")

    await machine._sweep_liveness(dispatch, now=WHEN)
    later = WHEN + timedelta(seconds=120)
    await machine._sweep_liveness(dispatch, now=later)

    observed = record.load(gates.line_snapshot(dispatch, line))
    assert observed is not None
    assert observed.spent == 0
    assert observed.shadowed == 2
    assert observed.budget_left == observed.budget


async def test_enforce_reassigns_a_parked_exit_to_a_new_author(tmp_path) -> None:
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=modes.ENFORCE)
    )
    line = make_line(assigned_model="codex")
    dispatch = await stored(collaborators, make_dispatch(line))
    park_line(gates, dispatch, line, "no_new_commit")
    dispatch.state = DispatchState.REVIEW

    await machine._sweep_liveness(dispatch, now=WHEN)

    assert line.status is LineStatus.ASSIGNED
    assert line.current_run is None
    assert line.attempts == 1
    assert line.assigned_model not in (None, "codex")
    # A reassign commits through the store, so read the adopted snapshot: the
    # pre-sweep dict is detached when ``_commit_line_locked`` replaces gates.
    snapshot = gates.line_snapshot(dispatch, line)
    updated = record.load(snapshot)
    assert updated is not None
    assert updated.outcome == record.RECOVERED
    assert updated.history[-1].action == "reassign"
    assert updated.history[-1].result == record.APPLIED
    assert snapshot["machine"]["stage"] == "assigned"


async def test_budget_exhaustion_hands_to_a_person_exactly_once(tmp_path) -> None:
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=modes.ENFORCE)
    )
    line = make_line()
    dispatch = await stored(collaborators, make_dispatch(line))
    snapshot = park_line(gates, dispatch, line, "no_new_commit")
    snapshot["escalation"] = {
        "summary": "two rounds of the same edge-case patch",
        "rounds": 2,
        "blockers": ["same bug"],
    }
    exhausted = record.new_record("no_new_commit", "R-EXIT", budget=1, at=WHEN)
    for _ in range(1):
        exhausted = exhausted.with_step(
            "reassign", at=WHEN, result=record.APPLIED
        )
    snapshot.update(record.store(snapshot, exhausted))

    await machine._sweep_liveness(dispatch, now=WHEN)
    snapshot = gates.line_snapshot(dispatch, line)
    first = record.load(snapshot)
    assert first is not None and first.needs_human
    assert first.notified_at
    assert any("needs a person" in blocker for blocker in line.blockers)
    inbox = tmp_path / "inbox" / "needs-human.md"
    note = inbox.read_text(encoding="utf-8")
    assert note.count("## d1 / line-0") == 1
    assert "two rounds of the same edge-case patch" in note

    await machine._sweep_liveness(dispatch, now=WHEN + timedelta(minutes=5))
    second = record.load(gates.line_snapshot(dispatch, line))
    assert second is not None and second.notified_at == first.notified_at
    assert inbox.read_text(encoding="utf-8").count("## d1 / line-0") == 1


async def test_a_reassign_brief_carries_the_escalation_summary(tmp_path) -> None:
    from src.tools.auto_dispatch.machine import reassign_brief

    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=modes.ENFORCE)
    )
    line = make_line(assigned_model="codex")
    dispatch = await stored(collaborators, make_dispatch(line))
    snapshot = park_line(gates, dispatch, line, "no_new_commit")
    snapshot["escalation"] = {
        "summary": "two rounds of the same edge-case patch",
        "rounds": 2,
        "blockers": ["same bug"],
    }

    await machine._sweep_liveness(dispatch, now=WHEN)

    brief = reassign_brief(dispatch, line)
    assert brief is not None
    assert any("Escalation summary" in item for item in brief)
    assert any("two rounds of the same edge-case patch" in item for item in brief)


async def test_reviewers_quota_exhausted_waits_then_resumes_the_review(tmp_path) -> None:
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=modes.ENFORCE)
    )
    line = make_line()
    dispatch = await stored(collaborators, make_dispatch(line))
    snapshot = park_line(
        gates, dispatch, line, "reviewers_quota_exhausted", stage=Stage.PRIMARY_REVIEW
    )
    resets_at = WHEN + timedelta(seconds=60)
    snapshot["reviewers_quota_exhausted"] = {
        "codex": {"reason": "quota_exhausted", "resets_at": resets_at.isoformat()}
    }

    await machine._sweep_liveness(dispatch, now=WHEN)
    snapshot = gates.line_snapshot(dispatch, line)
    waiting = record.load(snapshot)
    assert waiting is not None
    assert waiting.history[-1].action == "wait"
    assert waiting.next_action == "review"
    assert line.status is LineStatus.BLOCKED

    await machine._sweep_liveness(dispatch, now=WHEN + timedelta(seconds=120))
    snapshot = gates.line_snapshot(dispatch, line)
    resumed = record.load(snapshot)
    assert resumed is not None
    assert resumed.outcome == record.RECOVERED
    assert resumed.history[-1].action == "review"
    assert line.status is LineStatus.RUNNING
    assert line.failure_reason is None
    assert not machine._watching(line)
    # A process that starts after the resume adopts the same settled marker
    # and does not watch a run that no longer exists.
    from src.tools.auto_dispatch import DispatchMachine

    fresh = DispatchMachine(
        collaborators, config=MachineConfig(liveness_mode=modes.ENFORCE)
    )
    await fresh._adopt_settled_runs(dispatch)
    assert not fresh._watching(line)
    assert "reviewers_quota_exhausted" not in snapshot
    assert snapshot["machine"]["stage"] == Stage.PRIMARY_REVIEW.value


async def test_a_quota_wait_past_the_ceiling_goes_to_a_person(tmp_path) -> None:
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=modes.ENFORCE)
    )
    line = make_line()
    dispatch = await stored(collaborators, make_dispatch(line))
    snapshot = park_line(gates, dispatch, line, "reviewers_quota_exhausted")
    waited = record.new_record(
        "reviewers_quota_exhausted",
        "R-QUOTA",
        budget=4,
        at=WHEN - timedelta(hours=25),
    ).with_step("wait", at=WHEN - timedelta(hours=25), result=record.APPLIED)
    waited = waited.with_schedule(
        at=WHEN - timedelta(hours=25),
        next_at=WHEN,
        next_action="review",
    )
    snapshot.update(record.store(snapshot, waited))
    snapshot["reviewers_quota_exhausted"] = {
        "codex": {"resets_at": (WHEN + timedelta(hours=2)).isoformat()}
    }

    await machine._sweep_liveness(dispatch, now=WHEN)

    updated = record.load(gates.line_snapshot(dispatch, line))
    assert updated is not None and updated.needs_human
    assert dispatch.state is DispatchState.REVIEW


async def test_new_reviewer_headroom_resumes_before_the_stored_reset(tmp_path) -> None:
    """The first sweep must execute the REVIEW it selects, not the stored WAIT.

    The parked reset is still 60s away, but the live roster already has
    headroom when the drive starts, so the effective action changes from WAIT
    to REVIEW before the row runs. Executing the old WAIT step used to leave
    the line BLOCKED while its record moved on to RECOVERED.
    """
    gates = make_gates(tmp_path)

    class Selector:
        def __init__(self) -> None:
            self.calls = 0

        async def select(self, line, used, **kwargs):
            return "codex"

        async def review_blockers(self, line, used, *, exclude=()):
            self.calls += 1
            # The parked quota entry still says +60s; the live roster already
            # has headroom before the first sweep.
            return {}

    selector = Selector()
    machine, collaborators, _ = make_machine(
        tmp_path,
        gates=gates,
        selector=selector,
        config=MachineConfig(liveness_mode=modes.ENFORCE),
    )
    line = make_line()
    dispatch = await stored(collaborators, make_dispatch(line))
    snapshot = park_line(
        gates, dispatch, line, "reviewers_quota_exhausted", stage=Stage.PRIMARY_REVIEW
    )
    snapshot["reviewers_quota_exhausted"] = {
        "codex": {
            "reason": "quota_exhausted",
            "resets_at": (WHEN + timedelta(seconds=60)).isoformat(),
        }
    }

    await machine._sweep_liveness(dispatch, now=WHEN)

    assert selector.calls == 1
    assert line.status is LineStatus.RUNNING
    assert line.failure_reason is None
    assert not machine._watching(line)
    assert "reviewers_quota_exhausted" not in gates.line_snapshot(dispatch, line)
    updated = record.load(gates.line_snapshot(dispatch, line))
    assert updated is not None and updated.outcome == record.RECOVERED
    assert updated.history[-1].action == "review"
    assert updated.history[-1].result == record.APPLIED


async def test_no_eligible_worker_selects_an_author_before_running(tmp_path) -> None:
    """R-QUOTA's review row means "retry the capacity wait", not "resume a review".

    A worker-capacity park never launched an author. Executing the second row
    as REVIEW resumed the line as RUNNING with ``current_run=None`` and no
    model: an unlaunched line with nobody to do the work. The same row must
    select and install a worker first.
    """

    class Selector:
        def __init__(self) -> None:
            self.selected = 0

        async def select(self, line, used, **kwargs):
            self.selected += 1
            return "codex"

    gates = make_gates(tmp_path)
    selector = Selector()
    machine, collaborators, _ = make_machine(
        tmp_path,
        gates=gates,
        selector=selector,
        config=MachineConfig(liveness_mode=modes.ENFORCE),
    )
    line = make_line(assigned_model=None)
    dispatch = await stored(collaborators, make_dispatch(line))
    snapshot = park_line(
        gates, dispatch, line, "no_eligible_worker", stage=Stage.BLOCKED
    )
    state = _load_state(snapshot)
    _store_state(
        snapshot,
        replace(state, stage=Stage.BLOCKED, blocked_from=Stage.DAG_READY.value),
    )

    await machine._sweep_liveness(dispatch, now=WHEN)
    assert line.status is LineStatus.BLOCKED
    assert line.assigned_model is None

    await machine._sweep_liveness(dispatch, now=WHEN + timedelta(hours=2))

    assert selector.selected == 1
    assert line.status is LineStatus.ASSIGNED
    assert line.assigned_model == "codex"
    assert line.current_run is None
    updated = record.load(gates.line_snapshot(dispatch, line))
    assert updated is not None
    assert updated.outcome == record.RECOVERED
    assert updated.history[-1].action == "reassign"
    assert updated.history[-1].result == record.APPLIED


async def test_the_sweep_hands_an_ownerless_running_line_to_a_person(tmp_path) -> None:
    """A RUNNING line with no run and no review is not the sweep's blind spot.

    The old loop skipped every non-BLOCKED line, so a persisted RUNNING line
    whose ``current_run`` was lost stayed ownerless forever. It must be parked
    and handed to a person, with a record, in the same drive.
    """
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=modes.ENFORCE)
    )
    line = make_line(status=LineStatus.RUNNING, current_run=None)
    dispatch = await stored(collaborators, make_dispatch(line))
    snapshot = gates.line_snapshot(dispatch, line)
    state = _load_state(snapshot)
    _store_state(
        snapshot,
        replace(state, stage=Stage.IMPLEMENTING, blocked_from=""),
    )

    assert await machine._sweep_liveness(dispatch, now=WHEN) >= 1

    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == "liveness_inconsistent"
    assert any("needs a person" in item for item in line.blockers)
    assert dispatch.state is DispatchState.REVIEW
    updated = record.load(gates.line_snapshot(dispatch, line))
    assert updated is not None and updated.needs_human
    assert updated.group == "HUMAN"


async def test_shadow_leaves_an_ownerless_running_line_alone(tmp_path) -> None:
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=modes.SHADOW)
    )
    line = make_line(status=LineStatus.RUNNING, current_run=None)
    dispatch = await stored(collaborators, make_dispatch(line))
    snapshot = gates.line_snapshot(dispatch, line)
    state = _load_state(snapshot)
    _store_state(
        snapshot,
        replace(state, stage=Stage.IMPLEMENTING, blocked_from=""),
    )

    assert await machine._sweep_liveness(dispatch, now=WHEN) >= 1

    assert line.status is LineStatus.RUNNING
    assert line.failure_reason is None
    updated = record.load(gates.line_snapshot(dispatch, line))
    assert updated is not None
    assert not updated.needs_human
    assert updated.history[-1].action == "human"
    assert updated.history[-1].result == record.SHADOW


async def test_watching_a_run_less_running_line_matches_main() -> None:
    """Off mode must keep watching a RUNNING line with no ``current_run``.

    N76 added an early return for run-less RUNNING lines to keep a resumed
    review out of the monitor. It changed main's monitoring for every off/
    shadow dispatch. Only the explicit settled marker may exempt a line.
    """
    machine = DispatchMachine.__new__(DispatchMachine)
    machine._runs = {}
    line = make_line(status=LineStatus.RUNNING, current_run=None)

    assert machine._watching(line) is True

    machine._runs[line.id] = SimpleNamespace(complete=True)
    assert machine._watching(line) is False


async def test_no_eligible_worker_with_a_reserved_branch_still_assigns(tmp_path) -> None:
    """A branch reserved before selection is not delivered work.

    ``_apply_plan_ready`` writes ``line.branch`` before anyone is chosen, so a
    real capacity park has a branch and an incomplete handoff. Treating a
    nonempty branch as a launch skipped the synthetic handoff, and Stage-Gate
    then refused the reassignment after capacity returned.
    """

    class Selector:
        def __init__(self) -> None:
            self.selected = 0

        async def select(self, line, used, **kwargs):
            self.selected += 1
            return "codex"

    gates = make_gates(tmp_path)
    selector = Selector()
    machine, collaborators, _ = make_machine(
        tmp_path,
        gates=gates,
        selector=selector,
        config=MachineConfig(liveness_mode=modes.ENFORCE),
    )
    line = make_line(assigned_model=None)
    dispatch = await stored(collaborators, make_dispatch(line))
    line.branch = f"{dispatch.branch_prefix}/line-{line.id}"
    snapshot = park_line(gates, dispatch, line, "no_eligible_worker", stage=Stage.BLOCKED)
    state = _load_state(snapshot)
    _store_state(
        snapshot,
        replace(state, stage=Stage.BLOCKED, blocked_from=Stage.DAG_READY.value),
    )

    await machine._sweep_liveness(dispatch, now=WHEN)
    assert line.status is LineStatus.BLOCKED
    assert line.assigned_model is None

    await machine._sweep_liveness(dispatch, now=WHEN + timedelta(hours=2))

    assert selector.selected == 1
    assert line.status is LineStatus.ASSIGNED
    assert line.assigned_model == "codex"
    updated = record.load(gates.line_snapshot(dispatch, line))
    assert updated is not None
    assert updated.outcome == record.RECOVERED
    assert updated.history[-1].action == "reassign"


async def test_author_quota_retry_after_is_not_replaced_by_needs_human(tmp_path) -> None:
    """A future worker Retry-After still owns the line. HUMAN has an empty plan."""
    from src.tools.auto_dispatch.liveness.owner import decide_owner

    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=modes.ENFORCE)
    )
    line = make_line()
    dispatch = await stored(collaborators, make_dispatch(line))
    snapshot = park_line(gates, dispatch, line, "author_quota_exhausted", stage=Stage.BLOCKED)
    dispatch.gates["worker_recovery"] = {
        line.id: {
            "quota_retry_at": (WHEN + timedelta(hours=1)).timestamp(),
            "quota_retry_source": "retry_after",
            "parked_reason": "author_quota_exhausted",
        }
    }

    view = machine._liveness_view(dispatch, line, snapshot)
    decision = decide_owner(view, WHEN)
    assert decision.owner == "recovery_timer"
    assert decision.ownerless is False

    await machine._sweep_liveness(dispatch, now=WHEN)

    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == "author_quota_exhausted"
    parked = gates.line_snapshot(dispatch, line)
    assert parked.get("needs_human") is not True
    assert record.load(parked) is None
    assert "needs_human" not in dispatch.gates


async def test_owner_view_reports_a_worker_retry_after_without_a_line_status() -> None:
    """The owner repro has no line status. The timer is still the owner."""
    from src.tools.auto_dispatch.liveness.owner import decide_owner

    machine = DispatchMachine.__new__(DispatchMachine)
    dispatch = SimpleNamespace(
        gates={
            "worker_recovery": {
                "line-0": {"quota_retry_at": 4600, "quota_retry_source": "retry_after"}
            }
        }
    )
    line = SimpleNamespace(
        id="line-0",
        current_run=None,
        failure_reason=None,
        to_dict=lambda: {"id": "line-0"},
    )

    view = machine._liveness_view(dispatch, line, {})
    decision = decide_owner(view, datetime.fromtimestamp(1000, tz=timezone.utc))

    assert decision.owner == "recovery_timer"
    assert decision.ownerless is False
    assert view["liveness"]["next_at"].startswith("1970-01-01T01:16:40")


async def test_a_live_line_keeps_its_own_owner_ahead_of_a_leftover_worker_timer() -> None:
    from src.tools.auto_dispatch.liveness.owner import decide_owner

    machine = DispatchMachine.__new__(DispatchMachine)
    dispatch = SimpleNamespace(
        gates={
            "worker_recovery": {
                "line-0": {
                    "quota_retry_at": 4600,
                    "quota_retry_source": "retry_after",
                    "parked_reason": "author_quota_exhausted",
                }
            }
        }
    )
    line = SimpleNamespace(
        id="line-0",
        current_run=None,
        failure_reason=None,
        to_dict=lambda: {"id": "line-0", "status": "RUNNING"},
    )

    view = machine._liveness_view(dispatch, line, {})
    decision = decide_owner(view, datetime.fromtimestamp(1000, tz=timezone.utc))

    assert "liveness" not in view
    assert decision.ownerless is True


def _review_progress(reviews_spent: int):
    fresh = record.new_record(
        "review_unavailable",
        "R-REVIEW",
        budget=4,
        at=WHEN,
        next_at=WHEN,
        next_action="review",
    )
    for _ in range(reviews_spent):
        fresh = fresh.with_step("review", at=WHEN, result=record.APPLIED)
    return fresh.with_schedule(at=WHEN, next_at=WHEN, next_action="review")


async def _resume_review(
    tmp_path,
    *,
    reviews_spent: int,
    excluded: list[str] | None = None,
    primary: str = "codex",
    failed: list[str] | None = None,
):
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=modes.ENFORCE)
    )
    line = make_line()
    dispatch = await stored(collaborators, make_dispatch(line))
    snapshot = park_line(
        gates, dispatch, line, "review_unavailable", stage=Stage.PRIMARY_REVIEW
    )
    state = _load_state(snapshot)
    _store_state(snapshot, replace(state, primary_reviewer=primary))
    snapshot["review_parked"] = {
        "failed": list(failed if failed is not None else [primary]),
        "stage": Stage.PRIMARY_REVIEW.value,
    }
    if excluded:
        snapshot["liveness_excluded_reviewers"] = list(excluded)
    snapshot.update(record.store(snapshot, _review_progress(reviews_spent)))
    await machine._sweep_liveness(dispatch, now=WHEN)
    return machine, dispatch, line


async def test_the_first_review_row_retries_the_stored_reviewer(tmp_path) -> None:
    machine, dispatch, line = await _resume_review(tmp_path, reviews_spent=0)

    snapshot = machine._c.gates.line_snapshot(dispatch, line)
    state = _load_state(snapshot)
    assert "liveness_excluded_reviewers" not in snapshot
    assert await machine._pick_primary(dispatch, line, state) == "codex"


async def test_the_second_review_row_excludes_the_failed_reviewer(tmp_path) -> None:
    machine, dispatch, line = await _resume_review(tmp_path, reviews_spent=1)

    snapshot = machine._c.gates.line_snapshot(dispatch, line)
    excluded = {name.casefold() for name in snapshot.get("liveness_excluded_reviewers") or []}
    assert "codex" in excluded
    state = _load_state(snapshot)
    picked = await machine._pick_primary(dispatch, line, state)
    assert picked not in (None, "codex")
    assert picked == "claude"


async def test_a_later_review_row_keeps_the_earlier_exclusion(tmp_path) -> None:
    machine, dispatch, line = await _resume_review(
        tmp_path,
        reviews_spent=2,
        excluded=["codex"],
        primary="claude",
        failed=["claude"],
    )

    snapshot = machine._c.gates.line_snapshot(dispatch, line)
    excluded = {name.casefold() for name in snapshot.get("liveness_excluded_reviewers") or []}
    assert {"codex", "claude"} <= excluded
    state = _load_state(snapshot)
    assert await machine._pick_primary(dispatch, line, state) == "kimi"


async def test_quota_review_does_not_change_the_stored_reviewer(tmp_path) -> None:
    """R-QUOTA's review follows a wait, so it retries the same reviewer."""
    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=modes.ENFORCE)
    )
    line = make_line()
    dispatch = await stored(collaborators, make_dispatch(line))
    snapshot = park_line(
        gates, dispatch, line, "reviewers_quota_exhausted", stage=Stage.PRIMARY_REVIEW
    )
    state = _load_state(snapshot)
    _store_state(snapshot, replace(state, primary_reviewer="codex"))
    snapshot["review_parked"] = {"failed": ["codex"]}
    waited = record.new_record(
        "reviewers_quota_exhausted", "R-QUOTA", budget=4, at=WHEN
    ).with_step("wait", at=WHEN, result=record.APPLIED)
    waited = waited.with_schedule(at=WHEN, next_at=WHEN, next_action="review")
    snapshot.update(record.store(snapshot, waited))

    await machine._sweep_liveness(dispatch, now=WHEN)

    snapshot = machine._c.gates.line_snapshot(dispatch, line)
    assert "liveness_excluded_reviewers" not in snapshot
    assert line.status is LineStatus.RUNNING
    assert await machine._pick_primary(dispatch, line, _load_state(snapshot)) == "codex"


async def test_a_copying_store_keeps_an_earlier_needs_human_latch(tmp_path) -> None:
    """Reassigning line-1 must not drop line-0's latch from a copying store."""
    import copy
    from unittest.mock import AsyncMock, patch

    from src.tools.auto_dispatch.machine import Handoff

    class CopyStore:
        def __init__(self) -> None:
            self.row = None

        async def create(self, dispatch):
            self.row = copy.deepcopy(dispatch)
            return dispatch

        async def save(self, dispatch):
            self.row = copy.deepcopy(dispatch)
            return copy.deepcopy(dispatch)

        async def get(self, dispatch_id):
            return copy.deepcopy(self.row)

    gates = make_gates(tmp_path)
    store = CopyStore()
    machine, _, _ = make_machine(
        tmp_path,
        gates=gates,
        store=store,
        config=MachineConfig(liveness_mode=modes.ENFORCE),
    )
    first = make_line("line-0")
    second = make_line("line-1", assigned_model="codex")
    dispatch = make_dispatch(first, second)
    park_line(gates, dispatch, first, "handoff_incomplete")
    park_line(gates, dispatch, second, "no_new_commit")
    await store.create(dispatch)
    notes: list[str] = []
    machine._write_needs_human_note = lambda *_args, **_kwargs: notes.append("sent")
    machine._select_with_retry = AsyncMock(return_value="deepseek")
    handoff = Handoff(
        branch="old",
        commit="abc",
        files=("x.py",),
        test_command="pytest",
        results="passed",
        known_limitations="none",
    )
    with patch(
        "src.tools.auto_dispatch.machine._reassign_handoff",
        new=AsyncMock(return_value=handoff),
    ):
        await machine._sweep_liveness(dispatch, now=WHEN)

    saved = await store.get(dispatch.id)
    latched = gates.line_snapshot(saved, saved.line(first.id)).get("liveness", {})
    assert latched.get("needs_human") is True
    assert len(notes) == 1

    await machine._sweep_liveness(dispatch, now=WHEN + timedelta(minutes=5))
    assert len(notes) == 1


async def test_work_exclude_is_a_hard_exclusion() -> None:
    """The outgoing author is not seated again when another model can write."""
    from types import SimpleNamespace

    from src.server.services.autonomy.roles import RolePolicy
    from src.tools.auto_dispatch.selection import QuotaAwareModelSelector

    selector = QuotaAwareModelSelector(
        policy=RolePolicy(developer_pool=("kimi", "glm"), work_tail=())
    )
    machine = DispatchMachine(SimpleNamespace(selector=selector, monitor=None, gates=None))
    line = make_line(assigned_model="kimi", write_scope=("src/a.py",))
    dispatch = make_dispatch(line)

    chosen = await machine._select_with_retry(dispatch, line, exclude=["kimi"])

    assert chosen == "glm"


def _quota_record(snapshot: dict) -> None:
    """An R-QUOTA wait that is already due to resume as review."""
    waited = record.new_record(
        "reviewers_quota_exhausted", "R-QUOTA", budget=4, at=WHEN
    ).with_step("wait", at=WHEN, result=record.APPLIED)
    waited = waited.with_schedule(
        at=WHEN, next_at=WHEN + timedelta(seconds=60), next_action="review"
    )
    snapshot.update(record.store(snapshot, waited))


async def test_recovered_primary_resumes_while_another_reviewer_is_exhausted(
    tmp_path,
) -> None:
    """A non-empty review_blockers map is not proof that the stage is stuck.

    Codex can take primary review. StepFun is still exhausted, and the stored
    park still says Codex resets in two hours. The sweep resumes now.
    """
    from unittest.mock import AsyncMock

    resets_at = WHEN + timedelta(hours=2)
    selector = SimpleNamespace(
        select=AsyncMock(return_value="codex"),
        review_blockers=AsyncMock(
            return_value={"stepfun": {"resets_at": resets_at.isoformat()}}
        ),
    )
    gates = make_gates(tmp_path)
    machine, _, _ = make_machine(
        tmp_path,
        gates=gates,
        selector=selector,
        config=MachineConfig(liveness_mode=modes.ENFORCE),
    )
    machine._save = AsyncMock()
    line = make_line(assigned_model="kimi", risk_bearing=False)
    dispatch = make_dispatch(line)
    snapshot = park_line(
        gates, dispatch, line, "reviewers_quota_exhausted", stage=Stage.PRIMARY_REVIEW
    )
    _quota_record(snapshot)
    snapshot["reviewers_quota_exhausted"] = {
        "codex": {"resets_at": resets_at.isoformat()}
    }

    await machine._sweep_liveness(dispatch, now=WHEN + timedelta(seconds=61))

    assert line.status is LineStatus.RUNNING


async def test_quota_waits_when_the_only_stage_reviewer_is_still_exhausted(
    tmp_path,
) -> None:
    from unittest.mock import AsyncMock

    resets_at = WHEN + timedelta(hours=2)
    selector = SimpleNamespace(
        select=AsyncMock(return_value="stepfun"),
        review_blockers=AsyncMock(
            return_value={"stepfun": {"resets_at": resets_at.isoformat()}}
        ),
    )
    gates = make_gates(tmp_path)
    machine, _, _ = make_machine(
        tmp_path,
        gates=gates,
        selector=selector,
        config=MachineConfig(liveness_mode=modes.ENFORCE),
    )
    machine._save = AsyncMock()
    line = make_line(assigned_model="kimi", risk_bearing=False)
    dispatch = make_dispatch(line)
    snapshot = park_line(
        gates, dispatch, line, "reviewers_quota_exhausted", stage=Stage.PRIMARY_REVIEW
    )
    _quota_record(snapshot)
    snapshot["reviewers_quota_exhausted"] = {
        "stepfun": {"resets_at": resets_at.isoformat()}
    }

    await machine._sweep_liveness(dispatch, now=WHEN + timedelta(seconds=61))

    assert line.status is LineStatus.BLOCKED
    updated = record.load(gates.line_snapshot(dispatch, line))
    assert updated is not None
    assert record.parse_time(updated.next_at) == resets_at


async def test_secondary_quota_waits_until_a_non_primary_reviewer_recovers(
    tmp_path,
) -> None:
    """A recovered primary does not unblock the secondary stage."""
    from unittest.mock import AsyncMock

    from src.server.services.autonomy.roles import RolePolicy

    resets_at = WHEN + timedelta(hours=2)
    selector = SimpleNamespace(
        _policy=RolePolicy(
            primary_reviewer=("codex",),
            secondary_reviewer_pool=("stepfun",),
            developer_pool=("kimi",),
        ),
        select=AsyncMock(return_value="codex"),
        review_blockers=AsyncMock(
            return_value={"stepfun": {"resets_at": resets_at.isoformat()}}
        ),
    )
    gates = make_gates(tmp_path)
    machine, _, _ = make_machine(
        tmp_path,
        gates=gates,
        selector=selector,
        config=MachineConfig(liveness_mode=modes.ENFORCE),
    )
    machine._save = AsyncMock()
    line = make_line(assigned_model="kimi", risk_bearing=True)
    dispatch = make_dispatch(line)
    snapshot = park_line(
        gates,
        dispatch,
        line,
        "reviewers_quota_exhausted",
        stage=Stage.SECONDARY_REVIEW,
    )
    state = _load_state(snapshot)
    _store_state(snapshot, replace(state, primary_reviewer="codex"))
    _quota_record(snapshot)
    snapshot["reviewers_quota_exhausted"] = {
        "stepfun": {"resets_at": resets_at.isoformat()}
    }

    await machine._sweep_liveness(dispatch, now=WHEN + timedelta(seconds=61))

    assert line.status is LineStatus.BLOCKED
    selector.select.assert_not_called()


async def test_an_automatic_reassign_never_passes_max_reassigns(tmp_path) -> None:
    """H5 / PRD §7: the budget is not a way around MAX_REASSIGNS."""
    from src.tools.auto_dispatch.machine import MAX_REASSIGNS

    gates = make_gates(tmp_path)
    machine, collaborators, _ = make_machine(
        tmp_path, gates=gates, config=MachineConfig(liveness_mode=modes.ENFORCE)
    )
    line = make_line(assigned_model="codex")
    line.attempts = MAX_REASSIGNS
    dispatch = await stored(collaborators, make_dispatch(line))
    dispatch.state = DispatchState.REVIEW
    park_line(gates, dispatch, line, "no_new_commit")

    for minutes in (0, 5, 30, 120):
        await machine._sweep_liveness(dispatch, now=WHEN + timedelta(minutes=minutes))

    assert line.attempts == MAX_REASSIGNS
    assert line.assigned_model == "codex"
    assert line.status in (LineStatus.BLOCKED, LineStatus.FAILED)
    observed = record.load(gates.line_snapshot(dispatch, line))
    assert observed is not None
    assert all(
        step.result != record.APPLIED or step.action != "reassign" for step in observed.history
    )
    assert observed.spent <= observed.budget
