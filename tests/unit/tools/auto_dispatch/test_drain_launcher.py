"""N64: the drain-aware launcher holds new Worker starts while drained.

No process, no clock: the drain predicate and the hold's sleep are injected,
and the machine runs on the dev doubles.
"""

from __future__ import annotations

import inspect

import pytest

from src.conductor_cli.drain import clear_drain, set_drain
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
)
from src.tools.auto_dispatch.drain import DrainAwareLauncher, drain_aware
from src.tools.auto_dispatch.types import Line

from .fakes import Recorder

pytestmark = pytest.mark.asyncio


class Flag:
    """A drain flag that clears itself after ``polls`` holds."""

    def __init__(self, polls: int) -> None:
        self.polls = polls
        self.sleeps: list[float] = []

    def drained(self) -> bool:
        return self.polls > 0

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.polls -= 1


class WorkerLauncher:
    """A recovery-aware launcher: ``launch_worker`` with ``before_spawn``."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.cancelled: list[str] = []

    async def launch(self, line: Line, model: str, skills: list[str]) -> str:
        self.events.append(f"launch:{line.id}")
        return "run-legacy"

    async def launch_worker(
        self,
        line: Line,
        model: str,
        skills: list[str],
        *,
        session_id: str | None,
        session_mode: object | None,
        handoff_path: object | None,
        before_spawn=None,
    ) -> str:
        self.events.append(f"prepare:{line.id}")
        if before_spawn is not None:
            await before_spawn()
        self.events.append(f"spawn:{line.id}")
        return "run-1"

    async def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)

    def capacity_for(self, provider: str) -> int:
        return 3


def _line() -> Line:
    return Line(id="line-0", name="line 0", task="do it")


async def test_passes_straight_through_when_not_drained() -> None:
    events: list[str] = []
    flag = Flag(0)
    launcher = DrainAwareLauncher(
        WorkerLauncher(events), drained=flag.drained, sleep=flag.sleep
    )

    assert await launcher.launch(_line(), "codex", []) == "run-legacy"
    assert (
        await launcher.launch_worker(
            _line(), "codex", [], session_id=None, session_mode=None, handoff_path=None
        )
        == "run-1"
    )
    assert events == ["launch:line-0", "prepare:line-0", "spawn:line-0"]
    assert flag.sleeps == []


async def test_holds_a_launch_while_drained_then_continues() -> None:
    events: list[str] = []
    flag = Flag(3)

    async def sleep(seconds: float) -> None:
        # Nothing has started while the hold is polling.
        assert events == []
        await flag.sleep(seconds)

    launcher = DrainAwareLauncher(
        WorkerLauncher(events), drained=flag.drained, sleep=sleep, poll_seconds=2
    )

    assert await launcher.launch(_line(), "codex", []) == "run-legacy"
    assert flag.sleeps == [2, 2, 2]
    assert events == ["launch:line-0"]


async def test_a_drain_set_during_preparation_holds_the_spawn() -> None:
    events: list[str] = []
    state = {"drained": False, "sleeps": 0}

    class Inner(WorkerLauncher):
        async def launch_worker(self, line, model, skills, **kwargs):
            # The operator drains while the worktree is being prepared.
            state["drained"] = True
            return await super().launch_worker(line, model, skills, **kwargs)

    async def sleep(seconds: float) -> None:
        state["sleeps"] += 1
        assert events == ["prepare:line-0"]
        state["drained"] = False

    fenced: list[str] = []

    async def before_spawn() -> None:
        fenced.append("fence")

    launcher = DrainAwareLauncher(
        Inner(events), drained=lambda: bool(state["drained"]), sleep=sleep
    )
    await launcher.launch_worker(
        _line(),
        "codex",
        [],
        session_id=None,
        session_mode=None,
        handoff_path=None,
        before_spawn=before_spawn,
    )

    assert state["sleeps"] == 1
    # The machine's own fence still runs, after the hold.
    assert fenced == ["fence"]
    assert events == ["prepare:line-0", "spawn:line-0"]


async def test_monitoring_and_other_attributes_are_not_held() -> None:
    events: list[str] = []
    inner = WorkerLauncher(events)
    launcher = DrainAwareLauncher(inner, drained=lambda: True)

    # cancel (the Monitor's canceller) and capacity_for do not wait on drain.
    await launcher.cancel("run-9")
    assert inner.cancelled == ["run-9"]
    assert launcher.capacity_for("codex") == 3
    assert launcher.inner is inner


def test_only_the_inner_launchers_methods_are_exposed() -> None:
    launcher = DrainAwareLauncher(RecordingLauncher(), drained=lambda: False)

    # The machine probes these with getattr: a legacy launcher has none.
    assert getattr(launcher, "launch_worker", None) is None
    assert getattr(launcher, "request_self_report", None) is None
    assert getattr(launcher, "pin_base_commit", None) is None


def test_signatures_are_copied_for_keyword_probes() -> None:
    legacy = DrainAwareLauncher(RecordingLauncher(), drained=lambda: False)
    worker = DrainAwareLauncher(WorkerLauncher([]), drained=lambda: False)

    # The machine passes before_spawn only when the callable accepts it.
    assert "before_spawn" not in inspect.signature(legacy.launch).parameters
    assert "before_spawn" in inspect.signature(worker.launch_worker).parameters
    assert drain_aware(worker) is worker


async def test_reads_the_flag_file_under_conductor_home(tmp_path) -> None:
    events: list[str] = []
    home = tmp_path / "conductor-home"
    set_drain(home, "deploy")

    async def sleep(seconds: float) -> None:
        clear_drain(home)

    launcher = DrainAwareLauncher(WorkerLauncher(events), home=home, sleep=sleep)
    await launcher.launch(_line(), "codex", [])

    assert events == ["launch:line-0"]


async def test_a_drained_dispatch_waits_and_then_finishes_without_failing(
    tmp_path,
) -> None:
    """The machine sees a slow launch, not a failure: no line ends FAILED."""
    inner = RecordingLauncher()
    holds: list[int] = []
    flag = {"left": 4}

    async def sleep(seconds: float) -> None:
        # Nothing was launched while drained.
        assert inner.launches == []
        holds.append(1)
        flag["left"] -= 1

    collaborators = Collaborators(
        planner=FixedPlanner(),
        selector=RoundRobinSelector(),
        launcher=DrainAwareLauncher(
            inner, drained=lambda: flag["left"] > 0, sleep=sleep
        ),
        monitor=ScriptedMonitor(),
        reviewer=AlwaysPassReviewer(),
        merger=NoopMerger(),
        gate=FileApprovalGate(tmp_path / "proposed"),
        outcomes=InMemoryOutcomeStore(),
        store=InMemoryDispatchStore(),
    )
    machine = DispatchMachine(collaborators, sleep=Recorder([]))
    dispatch = await collaborators.store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )

    parked = await machine.run(dispatch)

    assert len(holds) == 4
    assert parked.state is DispatchState.APPROVAL
    assert [line.status for line in parked.lines] == [LineStatus.PASSED] * 3
    assert all(line.failure_reason is None for line in parked.lines)
    assert len(inner.launches) == 3


# --- N64 hardening: fence after a hold, busy markers, cancel / restart --------


@pytest.fixture(autouse=True)
def _private_conductor_home(tmp_path, monkeypatch):
    """Busy markers default to ``$CONDUCTOR_HOME/busy``: keep them in tmp."""
    home = tmp_path / "conductor-home"
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    return home


async def _launch_worker(launcher, before_spawn=None):
    return await launcher.launch_worker(
        _line(),
        "codex",
        [],
        session_id=None,
        session_mode=None,
        handoff_path=None,
        before_spawn=before_spawn,
    )


async def test_a_held_launch_runs_the_fence_before_preparing() -> None:
    events: list[str] = []
    flag = Flag(2)

    async def before_spawn() -> None:
        events.append("fence")

    launcher = DrainAwareLauncher(
        WorkerLauncher(events), drained=flag.drained, sleep=flag.sleep
    )
    await _launch_worker(launcher, before_spawn)

    # Once before the worktree is prepared (it was held), once at the spawn.
    assert events == ["fence", "prepare:line-0", "fence", "spawn:line-0"]


async def test_a_drive_that_lost_ownership_while_held_prepares_nothing() -> None:
    events: list[str] = []
    flag = Flag(1)

    class LostOwnership(Exception):
        pass

    async def before_spawn() -> None:
        raise LostOwnership

    launcher = DrainAwareLauncher(
        WorkerLauncher(events), drained=flag.drained, sleep=flag.sleep
    )
    with pytest.raises(LostOwnership):
        await _launch_worker(launcher, before_spawn)
    assert events == []


async def test_an_unheld_launch_runs_the_fence_only_at_the_spawn() -> None:
    events: list[str] = []

    async def before_spawn() -> None:
        events.append("fence")

    launcher = DrainAwareLauncher(WorkerLauncher(events), drained=lambda: False)
    await _launch_worker(launcher, before_spawn)
    assert events == ["prepare:line-0", "fence", "spawn:line-0"]


async def test_a_launch_holds_a_busy_marker_only_past_the_hold(
    _private_conductor_home,
) -> None:
    from src.conductor_cli.activity import busy_counts

    home = _private_conductor_home
    flag = Flag(2)
    seen: dict[str, object] = {}

    async def sleep(seconds: float) -> None:
        # Held: no marker, or restart --drain would wait on this launch.
        seen.setdefault("held", busy_counts(home))
        await flag.sleep(seconds)

    class Inner(WorkerLauncher):
        async def launch(self, line, model, skills):
            seen["running"] = busy_counts(home)
            return await super().launch(line, model, skills)

    launcher = DrainAwareLauncher(
        Inner([]), drained=flag.drained, sleep=sleep, home=home
    )
    await launcher.launch(_line(), "codex", [])

    assert seen == {"held": {}, "running": {"launch": 1}}
    assert busy_counts(home) == {}


async def test_the_spawn_hold_drops_the_busy_marker(_private_conductor_home) -> None:
    from src.conductor_cli.activity import busy_counts

    home = _private_conductor_home
    events: list[str] = []
    state = {"drained": False}
    seen: list[dict[str, int]] = []

    class Inner(WorkerLauncher):
        async def launch_worker(self, line, model, skills, **kwargs):
            seen.append(busy_counts(home))
            state["drained"] = True  # drained during preparation
            return await super().launch_worker(line, model, skills, **kwargs)

    async def sleep(seconds: float) -> None:
        seen.append(busy_counts(home))
        state["drained"] = False

    async def before_spawn() -> None:
        seen.append(busy_counts(home))

    launcher = DrainAwareLauncher(
        Inner(events), drained=lambda: state["drained"], sleep=sleep, home=home
    )
    await _launch_worker(launcher, before_spawn)

    # preparing: marked; spawn hold: not marked; fence after it: marked again.
    assert seen == [{"launch": 1}, {}, {"launch": 1}]
    assert busy_counts(home) == {}


async def test_the_marker_is_removed_when_the_launch_fails(
    _private_conductor_home,
) -> None:
    from src.conductor_cli.activity import busy_counts

    class Boom(WorkerLauncher):
        async def launch(self, line, model, skills):
            raise RuntimeError("spawn failed")

    launcher = DrainAwareLauncher(
        Boom([]), drained=lambda: False, home=_private_conductor_home
    )
    with pytest.raises(RuntimeError):
        await launcher.launch(_line(), "codex", [])
    assert busy_counts(_private_conductor_home) == {}


async def test_a_cancelled_held_launch_exits_without_launching(
    _private_conductor_home,
) -> None:
    import asyncio

    from src.conductor_cli.activity import busy_counts

    events: list[str] = []
    holding = asyncio.Event()

    async def sleep(seconds: float) -> None:
        holding.set()
        await asyncio.Event().wait()  # held until cancelled

    inner = WorkerLauncher(events)
    launcher = DrainAwareLauncher(
        inner, drained=lambda: True, sleep=sleep, home=_private_conductor_home
    )
    task = asyncio.create_task(_launch_worker(launcher))
    await holding.wait()

    # cancel() (the Monitor's canceller) passes through while a launch is held.
    await launcher.cancel("run-7")
    assert inner.cancelled == ["run-7"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == []
    assert busy_counts(_private_conductor_home) == {}


async def test_a_held_launch_runs_once_in_the_next_backend(tmp_path) -> None:
    """Restart while held: the old process's hold dies, the new one launches."""
    import asyncio

    home = tmp_path / "flag-home"
    set_drain(home, "deploy")
    events: list[str] = []
    holding = asyncio.Event()

    async def sleep(seconds: float) -> None:
        holding.set()
        await asyncio.sleep(0)

    old = DrainAwareLauncher(WorkerLauncher(events), home=home, sleep=sleep)
    task = asyncio.create_task(old.launch(_line(), "codex", []))
    await holding.wait()
    task.cancel()  # the backend stops
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == []

    clear_drain(home)
    new = DrainAwareLauncher(WorkerLauncher(events), home=home, sleep=sleep)
    assert await new.launch(_line(), "codex", []) == "run-legacy"
    assert events == ["launch:line-0"]
