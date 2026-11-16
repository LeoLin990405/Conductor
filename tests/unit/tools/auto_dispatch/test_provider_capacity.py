"""Process-wide per-provider concurrency cap for ScriptLauncher worker launches."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

import src.tools.auto_dispatch.execution as execution_module
from src.tools.auto_dispatch import LineStatus
from src.tools.auto_dispatch.execution import (
    DEFAULT_PROVIDER_CAPACITY,
    ScriptLauncher,
    provider_capacity_from_env,
    provider_slots_free,
)
from src.tools.auto_dispatch.types import Line
from tests.unit.tools.auto_dispatch.test_launcher_live_runs import (  # noqa: F401
    DISPATCH_ID,
    _owned_line,
    _repo,
    _temporary_home,
)
from tests.unit.tools.auto_dispatch.test_machine_gates import _line

pytestmark = pytest.mark.asyncio

WAIT = 2.0


class _Recorder:
    """A fake runner shared by launchers; each call blocks until released."""

    def __init__(self) -> None:
        self.entered: list[str] = []
        self.exited: list[str] = []
        self.active = 0
        self.max_active = 0
        self.entered_event: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}
        self.fail: set[str] = set()

    def event(self, key: str) -> asyncio.Event:
        return self.entered_event.setdefault(key, asyncio.Event())

    def gate(self, key: str) -> asyncio.Event:
        return self.release.setdefault(key, asyncio.Event())

    async def __call__(self, argv: list[str], cwd: Path, log_path: Path) -> int:
        key = argv[2]  # the task text names the line
        self.entered.append(key)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.event(key).set()
        try:
            await self.gate(key).wait()
            if key in self.fail:
                raise RuntimeError(f"runner failed for {key}")
            return 0
        finally:
            self.active -= 1
            self.exited.append(key)


def _launcher(
    tmp_path: Path,
    runner: _Recorder,
    *,
    capacity: dict[str, int] | None = None,
    cwd: Path | None = None,
    name: str = "a",
) -> ScriptLauncher:
    script = tmp_path / "launch.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    state = tmp_path / f"state-{name}"
    return ScriptLauncher(
        state / "runs",
        script=script,
        cwd=cwd or tmp_path,
        worktrees_dir=state / "worktrees",
        dispatch_id=DISPATCH_ID,
        runner=runner,
        provider_capacity=capacity if capacity is not None else {},
    )


def _plain_line(line_id: str, model: str = "kimi") -> Line:
    """A branchless line: runs in the launcher cwd, no worktree."""
    line = _line(line_id, assigned_model=model, status=LineStatus.ASSIGNED, task=line_id)
    line.branch = ""
    return line


def _start(launcher: ScriptLauncher, line: Line, model: str, **kwargs) -> asyncio.Task:
    return asyncio.create_task(
        launcher.launch_worker(
            line, model, [], session_id=None, session_mode=None, handoff_path=None, **kwargs
        )
    )


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)


# -- env parsing ---------------------------------------------------------------


async def test_env_parsing_lowercases_and_skips_invalid(caplog) -> None:
    env = {
        "CONDUCTOR_DISPATCH_PROVIDER_CAPACITY": " Grok=1, KIMI = 2 ,glm=0,bad,x=y,=3,,deepseek=4"
    }
    with caplog.at_level(logging.WARNING, logger=execution_module.logger.name):
        parsed = provider_capacity_from_env(env)
    assert parsed == {"grok": 1, "kimi": 2, "deepseek": 4}
    warned = " ".join(record.getMessage() for record in caplog.records)
    for bad in ("glm=0", "bad", "x=y", "=3"):
        assert bad in warned
    assert provider_capacity_from_env({}) == {}
    assert DEFAULT_PROVIDER_CAPACITY == 1


async def test_env_parsing_reads_os_environ(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CONDUCTOR_DISPATCH_PROVIDER_CAPACITY", "grok=3")
    assert provider_capacity_from_env() == {"grok": 3}
    launcher = ScriptLauncher(tmp_path / "runs", cwd=tmp_path)
    assert launcher.capacity_for("Grok") == 3
    assert launcher.capacity_for("kimi") == DEFAULT_PROVIDER_CAPACITY


async def test_constructor_rejects_invalid_capacity(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ScriptLauncher(tmp_path / "runs", cwd=tmp_path, provider_capacity={"kimi": 0})
    launcher = ScriptLauncher(tmp_path / "runs", cwd=tmp_path, provider_capacity={" KIMI ": 2})
    assert launcher.capacity_for("kimi") == 2


# -- the cap -------------------------------------------------------------------


async def test_capacity_one_serialises_same_provider_across_launchers(tmp_path: Path) -> None:
    recorder = _Recorder()
    first = _launcher(tmp_path, recorder, name="a")
    second = _launcher(tmp_path, recorder, name="b")
    one = _start(first, _plain_line("one"), "kimi")
    await asyncio.wait_for(recorder.event("one").wait(), WAIT)
    two = _start(second, _plain_line("two"), "KIMI")
    await _settle()

    assert recorder.entered == ["one"]
    assert provider_slots_free("kimi") is False

    recorder.gate("one").set()
    await asyncio.wait_for(one, WAIT)
    await asyncio.wait_for(recorder.event("two").wait(), WAIT)
    assert recorder.exited == ["one"]
    recorder.gate("two").set()
    await asyncio.wait_for(two, WAIT)

    assert recorder.max_active == 1
    assert recorder.entered == ["one", "two"]
    assert provider_slots_free("kimi") is True


async def test_capacity_two_lets_same_provider_overlap(tmp_path: Path) -> None:
    recorder = _Recorder()
    launcher = _launcher(tmp_path, recorder, capacity={"kimi": 2})
    one = _start(launcher, _plain_line("one"), "kimi")
    two = _start(launcher, _plain_line("two"), "kimi")
    await asyncio.wait_for(recorder.event("one").wait(), WAIT)
    await asyncio.wait_for(recorder.event("two").wait(), WAIT)

    assert recorder.max_active == 2
    assert provider_slots_free("kimi") is False

    three = _start(launcher, _plain_line("three"), "kimi")
    await _settle()
    assert "three" not in recorder.entered

    recorder.gate("one").set()
    await asyncio.wait_for(recorder.event("three").wait(), WAIT)
    for key in ("two", "three"):
        recorder.gate(key).set()
    await asyncio.wait_for(asyncio.gather(one, two, three), WAIT)
    assert recorder.max_active == 2


async def test_different_providers_run_concurrently(tmp_path: Path) -> None:
    recorder = _Recorder()
    launcher = _launcher(tmp_path, recorder)
    kimi = _start(launcher, _plain_line("k", "kimi"), "kimi")
    grok = _start(launcher, _plain_line("g", "grok"), "grok")
    await asyncio.wait_for(recorder.event("k").wait(), WAIT)
    await asyncio.wait_for(recorder.event("g").wait(), WAIT)

    assert recorder.max_active == 2
    assert provider_slots_free("glm") is True

    recorder.gate("k").set()
    recorder.gate("g").set()
    await asyncio.wait_for(asyncio.gather(kimi, grok), WAIT)


async def test_legacy_launch_uses_the_same_gate(tmp_path: Path) -> None:
    recorder = _Recorder()
    launcher = _launcher(tmp_path, recorder)
    legacy = asyncio.create_task(launcher.launch(_plain_line("legacy"), "kimi", []))
    await asyncio.wait_for(recorder.event("legacy").wait(), WAIT)
    worker = _start(launcher, _plain_line("worker"), "kimi")
    await _settle()
    assert recorder.entered == ["legacy"]
    recorder.gate("legacy").set()
    await asyncio.wait_for(recorder.event("worker").wait(), WAIT)
    recorder.gate("worker").set()
    await asyncio.wait_for(asyncio.gather(legacy, worker), WAIT)
    assert recorder.max_active == 1


# -- release paths ---------------------------------------------------------------


async def test_slot_released_when_runner_raises(tmp_path: Path) -> None:
    recorder = _Recorder()
    launcher = _launcher(tmp_path, recorder)
    recorder.fail.add("boom")
    boom = _start(launcher, _plain_line("boom"), "kimi")
    await asyncio.wait_for(recorder.event("boom").wait(), WAIT)
    nxt = _start(launcher, _plain_line("next"), "kimi")
    await _settle()
    assert "next" not in recorder.entered

    recorder.gate("boom").set()
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(boom, WAIT)
    await asyncio.wait_for(recorder.event("next").wait(), WAIT)
    recorder.gate("next").set()
    await asyncio.wait_for(nxt, WAIT)


async def test_slot_released_on_cancellation_of_holder_and_waiter(tmp_path: Path) -> None:
    recorder = _Recorder()
    launcher = _launcher(tmp_path, recorder)
    holder = _start(launcher, _plain_line("holder"), "kimi")
    await asyncio.wait_for(recorder.event("holder").wait(), WAIT)
    waiter = _start(launcher, _plain_line("waiter"), "kimi")
    third = _start(launcher, _plain_line("third"), "kimi")
    await _settle()

    # Cancel a queued waiter: it must leave the queue without a slot.
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    # Cancel the running holder: its slot goes to the next waiter.
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder
    await asyncio.wait_for(recorder.event("third").wait(), WAIT)
    assert "waiter" not in recorder.entered
    recorder.gate("third").set()
    await asyncio.wait_for(third, WAIT)
    assert provider_slots_free("kimi") is True


async def test_before_spawn_rechecked_after_waiting(tmp_path: Path) -> None:
    recorder = _Recorder()
    launcher = _launcher(tmp_path, recorder)
    holder = _start(launcher, _plain_line("holder"), "kimi")
    await asyncio.wait_for(recorder.event("holder").wait(), WAIT)

    class EpochMoved(Exception):
        pass

    calls: list[str] = []
    epoch_moved = False

    async def fence() -> None:
        calls.append("fence")
        if epoch_moved:
            raise EpochMoved

    fenced = _start(launcher, _plain_line("fenced"), "kimi", before_spawn=fence)
    await _settle()
    assert calls == ["fence"]  # checked once before queueing
    assert "fenced" not in recorder.entered

    epoch_moved = True
    recorder.gate("holder").set()
    with pytest.raises(EpochMoved):
        await asyncio.wait_for(fenced, WAIT)
    await asyncio.wait_for(holder, WAIT)

    assert calls == ["fence", "fence"]
    assert recorder.entered == ["holder"]
    assert provider_slots_free("kimi") is True


async def test_live_counts_with_a_real_line_worktree(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    recorder = _Recorder()
    launcher = _launcher(tmp_path, recorder, cwd=repo)
    line = _owned_line(base)
    task = asyncio.create_task(
        launcher.launch(line, "kimi", [])
    )
    key = launcher.argv(line, "kimi", [])[2]
    await asyncio.wait_for(recorder.event(key).wait(), WAIT * 5)
    assert provider_slots_free("kimi") is False
    assert provider_slots_free("kimi", capacity=2) is True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_gates_are_per_event_loop(tmp_path: Path) -> None:
    """A slot held on one loop does not block a launch on another loop."""
    recorder = _Recorder()
    launcher = _launcher(tmp_path, recorder)
    holder = _start(launcher, _plain_line("holder"), "kimi")
    await asyncio.wait_for(recorder.event("holder").wait(), WAIT)

    def other_loop() -> int:
        other = _Recorder()
        other_launcher = _launcher(tmp_path, other, name="other")

        async def run() -> int:
            other.gate("x").set()
            await asyncio.wait_for(
                other_launcher.launch_worker(
                    _plain_line("x"), "kimi", [], session_id=None, session_mode=None,
                    handoff_path=None,
                ),
                WAIT,
            )
            return len(other.entered)

        return asyncio.run(run())

    assert await asyncio.to_thread(other_loop) == 1
    recorder.gate("holder").set()
    await asyncio.wait_for(holder, WAIT)
