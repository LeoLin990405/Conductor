"""Declared-path ownership across active dispatches and through the planner."""

from __future__ import annotations

import pytest

from src.tools.auto_dispatch.dev import (
    AlwaysPassReviewer,
    InMemoryDispatchStore,
    InMemoryOutcomeStore,
    NoopMerger,
    RecordingLauncher,
    ScriptedMonitor,
)
from src.tools.auto_dispatch.approval import FileApprovalGate
from src.tools.auto_dispatch.machine import Collaborators, DispatchMachine
from src.tools.auto_dispatch.path_ownership import (
    ALLOW,
    PATH_OWNERSHIP_CONFLICT,
    QUEUE,
    REFUSE,
    PathOwnership,
)
from src.tools.auto_dispatch.planner_prompt import PLAN_JSON_SCHEMA, PLANNER_SYSTEM_PROMPT
from src.tools.auto_dispatch.planning import PlanRejection, validate_plan
from src.tools.auto_dispatch.selection import QuotaAwareModelSelector
from src.tools.auto_dispatch.types import Dispatch, DispatchState, Line, LineStatus


def _line(
    line_id: str = "line-0",
    *,
    paths: tuple[str, ...] = ("src/shared.py",),
    status: LineStatus = LineStatus.RUNNING,
    model: str | None = None,
) -> Line:
    return Line(
        id=line_id,
        name=line_id,
        task=f"Change {', '.join(paths)}",
        assigned_model=model,
        status=status,
        write_scope=paths,
        declared_paths=paths,
        acceptance=("cmd: pytest -q tests/unit/test_shared.py",),
    )


def _dispatch(
    dispatch_id: str,
    *lines: Line,
    state: DispatchState = DispatchState.MONITORING,
    repo_root: str = "",
) -> Dispatch:
    return Dispatch(
        id=dispatch_id,
        goal="change files",
        user_id="user",
        created_at="now",
        repo_root=repo_root,
        lines=list(lines),
        state=state,
    )


def test_records_every_active_line_and_releases_finished_work() -> None:
    ownership = PathOwnership()
    ownership.record_many(
        [
            _dispatch(
                "one",
                _line("running", paths=("src/a.py",)),
                _line("blocked", paths=("src/b.py",), status=LineStatus.BLOCKED),
                _line("passed", paths=("src/c.py",), status=LineStatus.PASSED),
                _line("failed", paths=("src/d.py",), status=LineStatus.FAILED),
            ),
            _dispatch("two", _line("assigned", paths=("src/e.py",), status=LineStatus.ASSIGNED)),
        ]
    )

    assert {(claim.dispatch_id, claim.line_id) for claim in ownership.claims} == {
        ("one", "running"),
        ("one", "blocked"),
        ("two", "assigned"),
    }

    ownership.record(_dispatch("one", _line("running"), state=DispatchState.COMPLETE))
    ownership.release("two", "assigned")
    assert ownership.claims == ()


def test_disjoint_paths_are_allowed() -> None:
    ownership = PathOwnership()
    ownership.record(_dispatch("owner", _line(paths=("src/one.py",))))
    candidate = _line(paths=("src/two.py",), status=LineStatus.PENDING)

    decision = ownership.check("new", candidate)

    assert decision.action == ALLOW
    assert decision.allowed
    assert candidate.failure_reason is None


def test_the_same_relative_path_in_independent_repositories_is_allowed(tmp_path) -> None:
    """README.md is a different file in each registered checkout."""
    ownership = PathOwnership()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    ownership.record(
        _dispatch("owner", _line(paths=("README.md",)), repo_root=str(first))
    )
    candidate = _line(paths=("README.md",), status=LineStatus.PENDING)

    decision = ownership.check("new", candidate, repo=str(second))

    assert decision.action == ALLOW
    assert decision.conflicts == ()
    assert candidate.failure_reason is None


def test_the_same_relative_path_in_one_repository_still_queues(tmp_path) -> None:
    """The repository is the partition key, not an escape from the claim."""
    ownership = PathOwnership()
    repo = tmp_path / "repo"
    repo.mkdir()
    ownership.record(
        _dispatch("owner", _line(paths=("README.md",)), repo_root=str(repo))
    )
    candidate = _line(paths=("README.md",), status=LineStatus.PENDING)

    decision = ownership.check("new", candidate, repo=str(repo))

    assert decision.action == QUEUE
    assert decision.conflicts[0].repo == str(repo.resolve())


@pytest.mark.asyncio
async def test_the_store_decides_by_repository_identity(tmp_path) -> None:
    """Persisted rows from two checkouts do not queue on the same relative path."""
    store = InMemoryDispatchStore()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    owner = _dispatch("owner", _line(paths=("README.md",)), repo_root=str(first))
    independent = _dispatch(
        "independent",
        _line(paths=("README.md",), status=LineStatus.PENDING),
        state=DispatchState.SUBMITTED,
        repo_root=str(second),
    )
    shared = _dispatch(
        "shared",
        _line(paths=("README.md",), status=LineStatus.PENDING),
        state=DispatchState.SUBMITTED,
        repo_root=str(first),
    )
    for row in (owner, independent, shared):
        await store.create(row)
    ownership = PathOwnership(store)

    allowed = await ownership.decide(
        independent.id, independent.lines[0], independent
    )
    queued = await ownership.decide(shared.id, shared.lines[0], shared)

    assert allowed.action == ALLOW
    assert queued.action == QUEUE
    assert queued.conflicts[0].dispatch_id == "owner"
    assert queued.conflicts[0].repo == str(first.resolve())


def test_two_pending_dispatches_do_not_deadlock_each_other() -> None:
    ownership = PathOwnership()
    first = _dispatch(
        "first",
        _line(status=LineStatus.PENDING),
        state=DispatchState.SUBMITTED,
    )
    second = _dispatch(
        "second",
        _line(status=LineStatus.PENDING),
        state=DispatchState.SUBMITTED,
    )
    ownership.record_many([first, second])

    assert ownership.claims == ()
    assert ownership.check("first", first.lines[0]).action == ALLOW

    first.lines[0].status = LineStatus.ASSIGNED
    ownership.record(first)
    queued = ownership.check("second", second.lines[0])

    assert queued.action == QUEUE
    assert queued.conflicts[0].dispatch_id == "first"
    assert "src/shared.py" in queued.reason


def test_same_dispatch_overlap_is_refused_with_a_recorded_reason() -> None:
    ownership = PathOwnership()
    dispatch = _dispatch(
        "same",
        _line("first", paths=("src/package",), status=LineStatus.ASSIGNED),
        _line("second", paths=("src/package/module.py",), status=LineStatus.PENDING),
    )
    ownership.record(dispatch)

    decision = ownership.check("same", dispatch.lines[1])

    assert decision.action == REFUSE
    assert decision.reason == (
        f"{PATH_OWNERSHIP_CONFLICT}:same:first:src/package/module.py|src/package"
    )
    assert dispatch.lines[1].failure_reason == decision.reason
    assert "same/first" in dispatch.lines[1].blockers[-1]
    assert ownership.decisions[-1] == decision


@pytest.mark.asyncio
async def test_cross_dispatch_overlap_queues_until_the_owner_completes() -> None:
    store = InMemoryDispatchStore()
    owner = _dispatch("owner", _line(paths=("src/**",)))
    candidate_dispatch = _dispatch(
        "candidate",
        _line(paths=("src/shared.py",), status=LineStatus.PENDING),
        state=DispatchState.SUBMITTED,
    )
    await store.create(owner)
    await store.create(candidate_dispatch)
    ownership = PathOwnership(store)

    queued = await ownership.decide(
        candidate_dispatch.id, candidate_dispatch.lines[0], candidate_dispatch
    )

    assert queued.action == QUEUE
    assert queued.queued
    assert queued.reason == (
        f"{PATH_OWNERSHIP_CONFLICT}:owner:line-0:src/shared.py|src/**"
    )
    assert candidate_dispatch.lines[0].failure_reason == queued.reason
    assert "src/shared.py" in candidate_dispatch.lines[0].failure_reason

    owner.state = DispatchState.COMPLETE
    owner.lines[0].status = LineStatus.PASSED
    await store.save(owner)
    allowed = await ownership.decide(
        candidate_dispatch.id, candidate_dispatch.lines[0], candidate_dispatch
    )

    assert allowed.action == ALLOW
    assert candidate_dispatch.lines[0].failure_reason is None
    assert candidate_dispatch.lines[0].blockers == []


@pytest.mark.asyncio
async def test_selector_enforces_queue_decision_and_records_it() -> None:
    store = InMemoryDispatchStore()
    await store.create(_dispatch("owner", _line()))
    candidate_dispatch = _dispatch(
        "candidate",
        _line(status=LineStatus.PENDING),
        state=DispatchState.SUBMITTED,
    )
    selector = QuotaAwareModelSelector(path_ownership=PathOwnership(store))

    chosen = await selector.select(candidate_dispatch.lines[0], [], dispatch=candidate_dispatch)

    assert chosen is None
    assert selector.work_decision == QUEUE
    assert selector.work_refusal == (
        f"{PATH_OWNERSHIP_CONFLICT}:owner:line-0:src/shared.py|src/shared.py"
    )
    assert selector.path_ownership_decision is not None
    assert selector.path_ownership_decision.conflicts[0].dispatch_id == "owner"


def _planner_payload(*, declared_paths: object = ...) -> dict[str, object]:
    line: dict[str, object] = {
        "name": "one",
        "task": "Change src/one.py.",
        "depends_on": [],
        "write_scope": ["src/one.py"],
        "acceptance": ["cmd: pytest -q tests/unit/test_one.py"],
        "risk_bearing": False,
    }
    if declared_paths is not ...:
        line["declared_paths"] = declared_paths
    return {
        "schema_version": 1,
        "spec": {"title": "One", "body": "## Goal\nChange one file."},
        "lines": [line],
    }


def test_planner_requires_and_carries_declared_paths() -> None:
    # The schema requires the field; a reply without it (no-schema planner, a
    # replan) owns its write scope instead of being refused as empty_write_scope.
    missing = validate_plan(_planner_payload())
    assert not isinstance(missing, PlanRejection)
    assert missing.lines[0].declared_paths == missing.lines[0].write_scope
    invalid = validate_plan(_planner_payload(declared_paths=[]))
    assert isinstance(invalid, PlanRejection)
    assert invalid.code == "empty_write_scope:one"

    result = validate_plan(_planner_payload(declared_paths=["src/one.py", "src/generated.py"]))
    assert not isinstance(result, PlanRejection)
    assert result.lines[0].declared_paths == ("src/one.py", "src/generated.py")
    assert result.lines[0].to_dict()["declared_paths"] == ["src/one.py", "src/generated.py"]

    required = PLAN_JSON_SCHEMA["properties"]["lines"]["items"]["required"]
    assert "declared_paths" in required
    assert '"declared_paths"' in PLANNER_SYSTEM_PROMPT
    assert "active lines of every dispatch" in PLANNER_SYSTEM_PROMPT


def test_planner_rejects_declared_path_overlap_even_when_write_scopes_are_disjoint() -> None:
    payload = _planner_payload(declared_paths=["src/owned"])
    second = _planner_payload(declared_paths=["src/owned/module.py"])["lines"][0]
    second["name"] = "two"
    second["write_scope"] = ["src/two.py"]
    payload["lines"].append(second)

    result = validate_plan(payload)

    assert isinstance(result, PlanRejection)
    assert result.code == "declared_path_conflict:one:two"


class _OneLinePlanner:
    def __init__(self, line: Line) -> None:
        self.line = line

    async def plan(self, goal: str) -> list[Line]:
        return [self.line]


@pytest.mark.asyncio
async def test_preassigned_line_queues_then_launches_after_owner_finishes(tmp_path) -> None:
    store = InMemoryDispatchStore()
    owner = _dispatch("owner", _line())
    await store.create(owner)
    line = _line(status=LineStatus.PENDING, model="kimi")
    dispatch = _dispatch(
        "candidate",
        line,
        state=DispatchState.SUBMITTED,
    )
    await store.create(dispatch)
    launcher = RecordingLauncher()
    collaborators = Collaborators(
        planner=_OneLinePlanner(line),
        selector=QuotaAwareModelSelector(path_ownership=PathOwnership(store)),
        launcher=launcher,
        monitor=ScriptedMonitor(),
        reviewer=AlwaysPassReviewer(),
        merger=NoopMerger(),
        gate=FileApprovalGate(tmp_path / "proposed"),
        outcomes=InMemoryOutcomeStore(),
        store=store,
    )
    machine = DispatchMachine(collaborators)

    queued = await machine.run(dispatch)

    assert queued.state is DispatchState.ASSIGNED
    assert queued.lines[0].status is LineStatus.PENDING
    assert queued.lines[0].failure_reason == (
        f"{PATH_OWNERSHIP_CONFLICT}:owner:line-0:src/shared.py|src/shared.py"
    )
    assert PATH_OWNERSHIP_CONFLICT in (queued.message or "")
    assert "src/shared.py" in (queued.message or "")
    assert launcher.launches == []

    owner.state = DispatchState.COMPLETE
    owner.lines[0].status = LineStatus.PASSED
    await store.save(owner)
    resumed = await machine.run(dispatch)

    assert launcher.launches == [("line-0", "kimi", [])]
    assert resumed.lines[0].failure_reason is None
    assert resumed.lines[0].status is LineStatus.PASSED


class _SlowCopyStore(InMemoryDispatchStore):
    """Rows read as saved copies, after a pause: claims only from saves.

    The pause lets a second drive check its lines while the first has decided
    but not yet saved its assignment, the window the guard has to close.
    """

    async def list(self, **kwargs):
        import asyncio
        import copy

        rows = await super().list(**kwargs)
        await asyncio.sleep(0.01)
        return [copy.deepcopy(row) for row in rows]


class _HeldMonitor:
    """Runs stay unfinished until ``release`` is set, so a launched line keeps its claim."""

    def __init__(self) -> None:
        import asyncio

        self.release = asyncio.Event()

    async def poll(self, run_id: str):
        from src.tools.auto_dispatch.types import RunStatus

        if not self.release.is_set():
            return RunStatus(output="", complete=False)
        return RunStatus(output=f"done {run_id}", complete=True, tokens=1)

    async def cancel(self, run_id: str) -> None:
        del run_id


def _racing_machine(tmp_path, store, line: Line, launcher, monitor, name: str) -> DispatchMachine:
    from src.tools.auto_dispatch.machine import MachineConfig

    return DispatchMachine(
        Collaborators(
            planner=_OneLinePlanner(line),
            selector=QuotaAwareModelSelector(path_ownership=PathOwnership(store)),
            launcher=launcher,
            monitor=monitor,
            reviewer=AlwaysPassReviewer(),
            merger=NoopMerger(),
            gate=FileApprovalGate(tmp_path / f"proposed-{name}"),
            outcomes=InMemoryOutcomeStore(),
            store=store,
        ),
        config=MachineConfig(poll_interval_seconds=0.01),
    )


@pytest.mark.asyncio
async def test_two_concurrent_drives_on_overlapping_paths_launch_only_one(tmp_path) -> None:
    import asyncio

    store = _SlowCopyStore()
    launcher = RecordingLauncher()
    monitor = _HeldMonitor()
    first_line = _line(status=LineStatus.PENDING, model="kimi")
    second_line = _line(status=LineStatus.PENDING, model="glm")
    first = _dispatch("first", first_line, state=DispatchState.SUBMITTED)
    second = _dispatch("second", second_line, state=DispatchState.SUBMITTED)
    await store.create(first)
    await store.create(second)

    drives = [
        asyncio.ensure_future(
            _racing_machine(tmp_path, store, first_line, launcher, monitor, "a").run(first)
        ),
        asyncio.ensure_future(
            _racing_machine(tmp_path, store, second_line, launcher, monitor, "b").run(second)
        ),
    ]
    # The loser returns queued while the winner's run is still unfinished.
    done, _pending = await asyncio.wait(drives, return_when=asyncio.FIRST_COMPLETED, timeout=10)
    assert len(done) == 1
    loser = next(iter(done)).result()
    assert len(launcher.launches) == 1
    assert loser.lines[0].status is LineStatus.PENDING
    assert (loser.lines[0].failure_reason or "").startswith(PATH_OWNERSHIP_CONFLICT)

    monitor.release.set()
    await asyncio.wait_for(asyncio.gather(*drives), timeout=10)
    assert len(launcher.launches) == 1


@pytest.mark.asyncio
async def test_the_guard_takes_the_store_lock_inside_the_process_lock() -> None:
    from contextlib import asynccontextmanager

    events: list[str] = []

    class _LockingStore(InMemoryDispatchStore):
        @asynccontextmanager
        async def path_claim_lock(self):
            events.append("db-lock")
            try:
                yield
            finally:
                events.append("db-unlock")

    ownership = PathOwnership(_LockingStore())
    async with ownership.guard():
        events.append("inside")

    assert events == ["db-lock", "inside", "db-unlock"]


@pytest.mark.asyncio
async def test_model_selection_runs_outside_the_claim_guard(tmp_path) -> None:
    from src.tools.auto_dispatch import path_ownership

    store = InMemoryDispatchStore()
    held_during_select: list[bool] = []

    class _Watching(QuotaAwareModelSelector):
        async def select(self, line, used_models, **kwargs):
            held_during_select.append(path_ownership._process_lock().locked())
            return await super().select(line, used_models, **kwargs)

    line = _line(status=LineStatus.PENDING)
    dispatch = _dispatch("solo", line, state=DispatchState.SUBMITTED)
    await store.create(dispatch)
    machine = DispatchMachine(
        Collaborators(
            planner=_OneLinePlanner(line),
            selector=_Watching(path_ownership=PathOwnership(store)),
            launcher=RecordingLauncher(),
            monitor=ScriptedMonitor(),
            reviewer=AlwaysPassReviewer(),
            merger=NoopMerger(),
            gate=FileApprovalGate(tmp_path / "proposed"),
            outcomes=InMemoryOutcomeStore(),
            store=store,
        )
    )

    await machine.run(dispatch)

    assert held_during_select and not any(held_during_select)
