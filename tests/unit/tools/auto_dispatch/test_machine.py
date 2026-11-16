"""The dispatch state machine, driven end to end with fakes.

No clock, no subprocess, no socket: the machine's sleep and clock are callables
and every collaborator is a double, so a full plan → assign → launch → monitor
→ review → approval → merge run finishes in microseconds.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from src.tools.auto_dispatch import (
    AlwaysPassReviewer,
    Collaborators,
    Dispatch,
    DispatchMachine,
    DispatchState,
    FileApprovalGate,
    FixedPlanner,
    InMemoryDispatchStore,
    InMemoryOutcomeStore,
    LineStatus,
    NoopMerger,
    RecordingLauncher,
    RoundRobinSelector,
    ScriptedMonitor,
    progress_pct,
)
from src.tools.auto_dispatch.protocols import (
    DispatchError,
    LaunchError,
    ModelRole,
    UncommittedChangesError,
)
from src.tools.auto_dispatch.selection import QuotaAwareModelSelector
from src.tools.auto_dispatch.types import Line, RunStatus

from .fakes import BlockingReviewer, FailingLauncher, Recorder


def _collaborators(
    tmp_path,
    *,
    planner=None,
    selector=None,
    launcher=None,
    monitor=None,
    reviewer=None,
    merger=None,
) -> Collaborators:
    return Collaborators(
        planner=planner or FixedPlanner(),
        selector=selector or RoundRobinSelector(),
        launcher=launcher or RecordingLauncher(),
        monitor=monitor or ScriptedMonitor(),
        reviewer=reviewer or AlwaysPassReviewer(),
        merger=merger or NoopMerger(),
        gate=FileApprovalGate(tmp_path / "proposed"),
        outcomes=InMemoryOutcomeStore(),
        store=InMemoryDispatchStore(),
    )


def _machine(collaborators: Collaborators, *, waits: list[float] | None = None):
    recorder = Recorder(waits or [])
    return DispatchMachine(collaborators, sleep=recorder), recorder


def _dispatch() -> Dispatch:
    return Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")


class SlowMonitor:
    """Reports a line in progress ``busy`` times, then complete."""

    def __init__(self, busy: int = 2) -> None:
        self.busy = busy
        self.polls: dict[str, int] = {}

    async def poll(self, run_id: str) -> RunStatus:
        seen = self.polls.get(run_id, 0)
        self.polls[run_id] = seen + 1
        if seen < self.busy:
            return RunStatus(output=f"working {seen}")
        return RunStatus(output="done", complete=True, tokens=8)

    async def cancel(self, run_id: str) -> None:
        return None


class CountingGate:
    """Wraps a gate and counts how often a dispatch asked for approval."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.requests = 0

    async def request_approval(self, dispatch: Dispatch) -> str:
        self.requests += 1
        return await self.inner.request_approval(dispatch)

    async def decision(self, token: str):
        return await self.inner.decision(token)

    async def resolve(self, token: str, approved: bool, note: str = ""):
        return await self.inner.resolve(token, approved, note)


async def test_a_full_dispatch_runs_to_approval_then_completes(tmp_path) -> None:
    collaborators = _collaborators(tmp_path)
    machine, _ = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())

    parked = await machine.run(dispatch)
    assert parked.state is DispatchState.APPROVAL
    assert parked.approval_token
    assert [line.status for line in parked.lines] == [LineStatus.PASSED] * 3
    assert {line.assigned_model for line in parked.lines} == {"codex", "claude", "kimi"}
    assert parked.integrate_branch == "integrate/auto-dispatch-d1"
    assert {line.branch for line in parked.lines} == {
        "feature/auto-dispatch-d1/line-line-0",
        "feature/auto-dispatch-d1/line-line-1",
        "feature/auto-dispatch-d1/line-line-2",
    }

    summary = (tmp_path / "proposed" / "d1" / "summary.md").read_text(encoding="utf-8")
    assert "ship it" in summary

    await collaborators.gate.resolve_dispatch(parked.id, True)
    completed = await machine.run(parked)
    assert completed.state is DispatchState.COMPLETE
    assert collaborators.merger.merged == ["line-0", "line-1", "line-2"]
    assert set(completed.outcomes) == {"line-0", "line-1", "line-2"}
    assert all(outcome.passed for outcome in completed.outcomes.values())
    assert "d1" in collaborators.outcomes.recorded


async def test_a_rejected_dispatch_aborts_and_sets_the_branch_aside(tmp_path) -> None:
    collaborators = _collaborators(tmp_path)
    machine, _ = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())

    parked = await machine.run(dispatch)
    await collaborators.gate.resolve_dispatch(parked.id, False, "too risky")
    aborted = await machine.run(parked)

    assert aborted.state is DispatchState.ABORT
    assert "rejected" in (aborted.message or "")
    assert collaborators.merger.merged == []
    assert collaborators.merger.aside == "integrate/auto-dispatch-test-rejected"


async def test_a_running_line_is_polled_until_it_reports_completion(tmp_path) -> None:
    monitor = SlowMonitor(busy=2)
    collaborators = _collaborators(tmp_path, monitor=monitor)
    machine, recorder = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())

    parked = await machine.run(dispatch)
    assert parked.state is DispatchState.APPROVAL
    # Every line was polled past its two in-progress reports, and the loop slept
    # between rounds rather than spinning on a finished line.
    assert set(monitor.polls.values()) == {3}
    assert recorder.waits == [10.0, 10.0]
    assert parked.line("line-0").log_tail == "done"


async def test_the_gate_is_asked_for_approval_exactly_once(tmp_path) -> None:
    collaborators = _collaborators(tmp_path)
    gate = CountingGate(collaborators.gate)
    collaborators.gate = gate
    machine, _ = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())

    parked = await machine.run(dispatch)
    assert parked.state is DispatchState.APPROVAL
    assert gate.requests == 1


async def test_a_launch_failure_reassigns_the_line_to_a_new_model(tmp_path) -> None:
    launcher = FailingLauncher(fail_times=3)
    collaborators = _collaborators(tmp_path, launcher=launcher, selector=RoundRobinSelector())
    machine, _ = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())

    parked = await machine.run(dispatch)
    assert parked.state is DispatchState.APPROVAL
    line = parked.line("line-0")
    assert line is not None
    assert line.attempts == 1
    # The reassignment never reuses a model another line already holds.
    assert line.assigned_model not in ("codex", "claude", "kimi")
    # The failed run was retried on the same model up to LAUNCH_RETRIES times,
    # and only then reassigned.
    assert [model for _, model in launcher.launches].count("codex") == 3


async def test_a_host_preflight_failure_is_not_retried_on_the_same_model(tmp_path) -> None:
    from src.tools.auto_dispatch.env_preflight import EnvPreflightError, EnvProblem

    class NoGitLauncher(FailingLauncher):
        async def launch(self, line, model, skills):
            seen = self.attempts.get(line.id, 0)
            self.attempts[line.id] = seen + 1
            self.launches.append((line.id, model))
            if seen < self.fail_times:
                raise EnvPreflightError([EnvProblem("git_missing", "git is not on the worker PATH")])
            return f"run-{line.id}-{seen + 1}"

    launcher = NoGitLauncher(fail_times=1)
    collaborators = _collaborators(tmp_path, launcher=launcher, selector=RoundRobinSelector())
    machine, _ = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())

    await machine.run(dispatch)
    # Retrying at once on this host fails the same way: one try, then give up.
    assert [model for _, model in launcher.launches].count("codex") == 1


async def test_uncommitted_worker_result_is_not_reassigned_or_called_handoff_incomplete(tmp_path):
    class DirtyLauncher:
        async def launch(self, line, model, skills):
            raise UncommittedChangesError("dirty worktree preserved")

        async def cancel(self, run_id):
            return None

    collaborators = _collaborators(tmp_path, launcher=DirtyLauncher())
    machine, _ = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())

    failed = await machine.run(dispatch)
    line = failed.line("line-0")
    assert line is not None
    assert line.failure_reason == "uncommitted_changes"
    assert line.status is LineStatus.FAILED
    assert failed.state is DispatchState.ERROR
    assert "handoff_incomplete" not in (failed.message or "")


async def test_a_stalled_run_is_cancelled_and_reassigned(tmp_path) -> None:
    class _StallLine:
        """Stall the first observation of line-0, whatever its unique run id is."""

        def __init__(self) -> None:
            self.cancelled: list[str] = []
            self._stalled = False

        async def poll(self, run_id: str) -> RunStatus:
            if run_id.split(":")[1] == "line-0" and not self._stalled:
                self._stalled = True
                return RunStatus(output="", stalled=True)
            return RunStatus(output=f"done {run_id}", complete=True, tokens=1)

        async def cancel(self, run_id: str) -> None:
            self.cancelled.append(run_id)

    monitor = _StallLine()
    collaborators = _collaborators(tmp_path, monitor=monitor)
    machine, _ = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())

    parked = await machine.run(dispatch)
    assert parked.state is DispatchState.APPROVAL
    assert len(monitor.cancelled) == 1
    assert monitor.cancelled[0].split(":")[1] == "line-0"
    assert parked.line("line-0").assigned_model != "codex"
    assert parked.line("line-0").attempts == 1


async def test_a_timed_out_run_is_reassigned_too(tmp_path) -> None:
    class _TimeoutLine:
        def __init__(self) -> None:
            self._timed = False

        async def poll(self, run_id: str) -> RunStatus:
            if run_id.split(":")[1] == "line-1" and not self._timed:
                self._timed = True
                return RunStatus(output="", timed_out=True)
            return RunStatus(output=f"done {run_id}", complete=True, tokens=1)

        async def cancel(self, run_id: str) -> None:
            del run_id

    collaborators = _collaborators(tmp_path, monitor=_TimeoutLine())
    machine, _ = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())

    parked = await machine.run(dispatch)
    assert parked.line("line-1").attempts == 1
    assert parked.line("line-1").assigned_model != "claude"


async def test_a_line_that_never_launches_fails_the_dispatch(tmp_path) -> None:
    collaborators = _collaborators(tmp_path, launcher=FailingLauncher(always=True))
    machine, _ = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())

    aborted = await machine.run(dispatch)
    assert aborted.state is DispatchState.ABORT
    assert "after 3 attempts" in (aborted.message or "")
    assert aborted.line("line-0").status is LineStatus.FAILED
    assert aborted.line("line-0").attempts == 3
    # The dispatch stops at the first dead line: no other line is ever launched,
    # and nothing advances to monitoring, review or the gate.
    assert {line_id for line_id, _ in collaborators.launcher.launches} == {"line-0"}
    assert aborted.approval_token is None


async def test_a_blocked_review_parks_the_dispatch_for_a_human(tmp_path) -> None:
    reviewer = BlockingReviewer({"line-1": ["no tests for the new handler"]})
    collaborators = _collaborators(tmp_path, reviewer=reviewer)
    machine, _ = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())

    parked = await machine.run(dispatch)
    assert parked.state is DispatchState.REVIEW
    assert parked.line("line-1").status is LineStatus.BLOCKED
    assert parked.line("line-1").blockers == ["no tests for the new handler"]
    assert "no tests" in (parked.message or "")
    assert parked.approval_token is None

    # A human overrides the model; the machine picks the line back up.
    parked.line("line-1").assigned_model = "kimi"
    parked.line("line-1").attempts += 1
    parked.line("line-1").status = LineStatus.ASSIGNED
    resumed = await machine.run(parked)
    assert resumed.state is DispatchState.APPROVAL
    assert resumed.line("line-1").status is LineStatus.PASSED
    reviews = [model for line_id, model in reviewer.reviewed if line_id == "line-1"]
    assert len(reviews) == 2
    assert "kimi" not in reviews  # the author never reviews its own line
    assert reviews[0] != reviews[1]  # a second look comes from a different model


async def test_a_merge_conflict_aborts_the_dispatch(tmp_path) -> None:
    collaborators = _collaborators(tmp_path, merger=NoopMerger(conflict_on="line-1"))
    machine, _ = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())

    parked = await machine.run(dispatch)
    await collaborators.gate.resolve_dispatch(parked.id, True)
    aborted = await machine.run(parked)

    assert aborted.state is DispatchState.ABORT
    assert "conflict on line-1" in (aborted.message or "")
    assert collaborators.merger.merged == ["line-0"]


async def test_an_undecomposable_goal_ends_in_error(tmp_path) -> None:
    class Empty:
        async def plan(self, goal: str) -> list[Line]:
            return []

    collaborators = _collaborators(tmp_path, planner=Empty())
    machine, _ = _machine(collaborators)
    failed = await machine.run(await collaborators.store.create(_dispatch()))
    assert failed.state is DispatchState.ERROR
    assert "no work lines" in (failed.message or "")


async def test_an_exhausted_pool_ends_in_error_after_one_retry(tmp_path) -> None:
    class Exhausted:
        async def select(self, line, used_models, *, role=ModelRole.WORK):
            return None

    collaborators = _collaborators(tmp_path, selector=Exhausted())
    machine, recorder = _machine(collaborators)
    failed = await machine.run(await collaborators.store.create(_dispatch()))

    assert failed.state is DispatchState.ERROR
    assert "exhausted" in (failed.message or "")
    assert recorder.waits == [30.0]


async def test_progress_rises_with_the_state_and_never_regresses(tmp_path) -> None:
    collaborators = _collaborators(tmp_path)
    machine, _ = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())

    assert progress_pct(dispatch) == 0
    parked = await machine.run(dispatch)
    parked_pct = progress_pct(parked)
    assert parked.state is DispatchState.APPROVAL
    assert 0 < parked_pct < 100

    await collaborators.gate.resolve_dispatch(parked.id, True)
    completed = await machine.run(parked)
    assert progress_pct(completed) == 100
    assert progress_pct(completed) >= parked_pct


async def test_progress_counts_finished_lines_while_monitoring(tmp_path) -> None:
    collaborators = _collaborators(tmp_path)
    dispatch = await collaborators.store.create(_dispatch())
    dispatch.state = DispatchState.MONITORING
    dispatch.lines = [
        Line(id="line-0", name="a", task="t", status=LineStatus.PASSED),
        Line(id="line-1", name="b", task="t", status=LineStatus.RUNNING),
        Line(id="line-2", name="c", task="t", status=LineStatus.RUNNING),
        Line(id="line-3", name="d", task="t", status=LineStatus.RUNNING),
    ]
    monitoring = progress_pct(dispatch)
    for line in dispatch.lines:
        line.status = LineStatus.PASSED
    assert progress_pct(dispatch) > monitoring


async def test_every_transition_is_persisted(tmp_path) -> None:
    collaborators = _collaborators(tmp_path)
    machine, _ = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())
    await machine.run(dispatch)
    stored = await collaborators.store.get("d1")
    assert stored is not None
    assert stored.state is DispatchState.APPROVAL
    assert stored.integrate_branch == "integrate/auto-dispatch-d1"
    assert len(stored.lines) == 3


async def test_a_selector_failure_mid_dispatch_errors(tmp_path) -> None:
    class Flaky:
        def __init__(self) -> None:
            self.calls = 0

        async def select(self, line, used_models, *, role=ModelRole.WORK):
            self.calls += 1
            return None if self.calls > 1 else "codex"

    collaborators = _collaborators(tmp_path, selector=Flaky())
    machine, _ = _machine(collaborators)
    failed = await machine.run(await collaborators.store.create(_dispatch()))
    assert failed.state is DispatchState.ERROR


async def test_the_quota_aware_selector_skips_a_drained_model(tmp_path) -> None:
    class Headroom:
        async def remaining(self, model: str) -> float:
            return 0.0 if model == "kimi" else 1.0

    selector = QuotaAwareModelSelector(Headroom())
    collaborators = _collaborators(tmp_path, selector=selector)
    machine, _ = _machine(collaborators)
    parked = await machine.run(await collaborators.store.create(_dispatch()))
    assert parked.line("line-0").assigned_model == "glm"


async def test_a_large_line_reassigns_to_the_widest_remaining_quota(tmp_path) -> None:
    """A large task's live reassignment takes the widest remaining quota.

    Reassignment reaches the production ``QuotaAwareModelSelector.select``
    through the machine's ``_select_with_retry`` (machine.py). The line is
    ``large``; cursor is seated first on the widest quota, its launch never
    starts, and the line is reassigned. kimi (0.9) holds far more quota than the
    earlier-roster glm (0.1), so the size-aware ranking must reassign to kimi --
    a roster-order pick would take glm. Only cursor/kimi/glm stay eligible, so
    the result does not depend on the deployment's roster order.
    """
    class Headroom:
        async def remaining(self, model: str) -> float:
            # codex is the reserved primary reviewer: it stays available for the
            # review gate without ever competing as a work candidate.
            return {"cursor": 0.95, "kimi": 0.9, "glm": 0.1, "codex": 1.0}.get(model, 0.0)

    selector = QuotaAwareModelSelector(Headroom())
    line = Line(id="line-0", name="implement", task="do it", estimated_size="large")

    class Planner:
        async def plan(self, goal: str) -> list[Line]:
            return [line]

    launcher = FailingLauncher(fail_times=3)  # cursor never starts; force a reassign
    machine, _ = _machine(
        _collaborators(tmp_path, planner=Planner(), selector=selector, launcher=launcher)
    )
    parked = await machine.run(_dispatch())

    seated = parked.line("line-0")
    assert seated.attempts == 1
    assert seated.assigned_model == "kimi"
    # The first seat was the widest quota (cursor); the reassign took the next
    # widest (kimi), never the roster-earlier glm.
    assert [model for _, model in launcher.launches][:3] == ["cursor"] * 3
    assert launcher.launches[-1] == ("line-0", "kimi")


async def test_a_large_reassignment_keeps_the_quota_tier_under_unequal_load(tmp_path) -> None:
    """Blocker numbers on the production retry path: kimi=0.05, cursor=0.95.

    line-0 (medium) is seated on cursor, so cursor holds one more assignment
    than kimi when the large line-1 is reassigned off glm, whose launches
    never start. Ranking least-assigned first reassigned the large line to the
    nearly spent kimi; the quota tier must lead, so it goes to cursor. The
    reassignment reaches ``QuotaAwareModelSelector.select`` through the
    machine's ``_reassign`` / ``_select_with_retry``.
    """
    from src.server.services.autonomy.roles import RolePolicy

    quota = {"cursor": 0.95, "glm": 0.6, "kimi": 0.0, "codex": 1.0}

    class Headroom:
        async def remaining(self, model: str) -> float:
            return quota.get(model, 0.0)

    class GlmNeverStarts:
        def __init__(self) -> None:
            self.launches: list[tuple[str, str]] = []

        async def launch(self, line: Line, model: str, skills: list[str]) -> str:
            self.launches.append((line.id, model))
            if model == "glm":
                # kimi's window reopens a sliver while glm keeps failing.
                quota["kimi"] = 0.05
                raise LaunchError(f"{model} could not start line {line.id}")
            return f"run-{line.id}-{len(self.launches)}"

    lines = [
        Line(id="line-0", name="small", task="a"),
        Line(id="line-1", name="heavy", task="b", estimated_size="large"),
    ]

    class Planner:
        async def plan(self, goal: str) -> list[Line]:
            return lines

    policy = RolePolicy(developer_pool=("cursor", "glm", "kimi"), work_tail=())
    selector = QuotaAwareModelSelector(Headroom(), policy=policy)
    launcher = GlmNeverStarts()
    machine, _ = _machine(
        _collaborators(tmp_path, planner=Planner(), selector=selector, launcher=launcher)
    )
    parked = await machine.run(_dispatch())

    assert parked.line("line-0").assigned_model == "cursor"
    heavy = parked.line("line-1")
    assert ("line-1", "glm") in launcher.launches
    assert heavy.attempts == 1
    assert heavy.assigned_model == "cursor"
    assert launcher.launches[-1] == ("line-1", "cursor")


class RaisingReviewer:
    """A reviewer whose run fails the way a CLI adapter's does, with text worth hiding."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    async def review_line(self, line_id: str, model: str):
        self.calls += 1
        raise self.exc


REVIEWER_LEAK = "PROMPT-FRAGMENT-the-cli-echoed-back"


@pytest.mark.parametrize(
    "exc",
    [
        TypeError(f"__init__() got an unexpected keyword argument 'read_only' {REVIEWER_LEAK}"),
        RuntimeError(f"cli exited 1: {REVIEWER_LEAK}"),
        OSError(f"binary not found {REVIEWER_LEAK}"),
        TimeoutError(REVIEWER_LEAK),
        DispatchError(REVIEWER_LEAK),
    ],
    ids=lambda exc: type(exc).__name__,
)
async def test_a_reviewer_that_cannot_run_ends_the_dispatch_in_error(
    tmp_path, caplog, exc
) -> None:
    reviewer = RaisingReviewer(exc)
    collaborators = _collaborators(tmp_path, reviewer=reviewer)
    machine, _ = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())

    with caplog.at_level(logging.DEBUG):
        failed = await machine.run(dispatch)

    assert failed.approval_token is None
    assert reviewer.calls == 1
    assert collaborators.merger.merged == []
    stored = await collaborators.store.get("d1")
    assert stored is not None
    assert REVIEWER_LEAK not in (failed.message or "")
    assert REVIEWER_LEAK not in caplog.text
    assert type(exc).__name__ in caplog.text
    if isinstance(exc, DispatchError):
        assert failed.state is DispatchState.ERROR
        assert failed.message is not None
        assert failed.message.startswith("The reviewer ")
        assert failed.message.endswith(" could not review line line-0")
        assert stored.state is DispatchState.ERROR
        assert stored.message == failed.message
        return
    # An adapter failure stays on the same stage for the next wakeup.
    assert failed.state is DispatchState.REVIEW
    assert stored.state is DispatchState.REVIEW
    attempts = failed.gates.get("review_attempts")
    assert attempts["line-0"] == 1


async def test_cancelling_a_review_is_not_turned_into_an_error(tmp_path) -> None:
    collaborators = _collaborators(
        tmp_path, reviewer=RaisingReviewer(asyncio.CancelledError())
    )
    machine, _ = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())

    with pytest.raises(asyncio.CancelledError):
        await machine.run(dispatch)
    assert dispatch.state is not DispatchState.ERROR


async def test_a_write_line_parks_when_only_doubao_has_quota(tmp_path) -> None:
    """A write line parks as no_eligible_worker instead of launching Doubao."""
    from src.tools.auto_dispatch.pool import eligible_models

    roster = eligible_models("work")

    class Headroom:
        async def remaining(self, model: str) -> float:
            return 0.0 if model in roster and model != "doubao" else 1.0

    selector = QuotaAwareModelSelector(Headroom())
    line = Line(id="line-0", name="edit", task="edit the file", write_scope=("src/a.py",))

    class Planner:
        async def plan(self, goal: str) -> list[Line]:
            return [line]

    machine, _recorder = _machine(
        _collaborators(tmp_path, planner=Planner(), selector=selector)
    )
    dispatch = await machine.run(_dispatch())
    assert dispatch.state is DispatchState.ERROR
    assert dispatch.message == "no_eligible_worker:line-0"
    assert dispatch.lines[0].assigned_model is None
    assert dispatch.lines[0].failure_reason == "no_eligible_worker"
    assert selector.work_refusal == "text_only_worker"


pytestmark = pytest.mark.asyncio

