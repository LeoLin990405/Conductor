"""Spawn-time cancellation and group-kill targeting for the native CLI runners.

A stop acknowledgement vouches for every process the invocation started, so two
holes matter: a cancellation that lands before asyncio hands the child back must
still end up killing the child's group and recording its pid, and the group kill
must only ever aim at the group this module created, never at whatever a
recycled pid happens to name.
"""

from __future__ import annotations

import asyncio
import gc
import os
import signal
import subprocess
import time

import pytest

from src.llms.native_cli import process as process_mod
from src.llms.native_cli.base import CliInvocation, NativeCliUnavailable
from src.llms.native_cli.process import (
    ProcessTracker,
    _kill_tree,
    run_process,
    stream_process,
    track_processes,
)


class _Process:
    """What the spawn hands back: a pid, a leader that dies when killed."""

    pid = 424243

    def __init__(self) -> None:
        self.returncode: int | None = None
        self._exited = asyncio.Event()
        self.kill_calls = 0

    def exit(self, code: int) -> None:
        self.returncode = code
        self._exited.set()

    def kill(self) -> None:
        self.kill_calls += 1
        self.exit(-9)

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode


class _KillSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[_Process, ProcessTracker | None]] = []

    def __call__(
        self, process: _Process, tracker: ProcessTracker | None = None
    ) -> None:
        self.calls.append((process, tracker))
        process.exit(-9)


class _Tracker(ProcessTracker):
    def __init__(self) -> None:
        super().__init__()
        self.spawned_pids: list[int] = []
        self.exited_pids: list[int] = []

    def spawned(self, pid: int, pgid: int | None = None) -> None:
        self.spawned_pids.append(pid)
        super().spawned(pid, pgid)

    def exited(self, pid: int) -> None:
        self.exited_pids.append(pid)
        super().exited(pid)


@pytest.fixture
def slow_spawn(monkeypatch):
    """A spawn that stays pending (the child exists) until the test releases it."""
    state: dict = {
        "gate": asyncio.Event(),
        "process": _Process(),
        "error": None,
        "spy": _KillSpy(),
    }

    async def fake_exec(*argv, **kwargs):
        await state["gate"].wait()
        if state["error"] is not None:
            raise state["error"]
        return state["process"]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        process_mod, "prepare_process", lambda argv, cwd, env: (argv, None, cwd)
    )
    monkeypatch.setattr(process_mod, "_kill_tree", state["spy"])
    return state


def _invocation() -> CliInvocation:
    return CliInvocation(argv=("fake-cli",), stdin="prompt", timeout_seconds=30.0)


async def _drive_stream(tracker: ProcessTracker) -> None:
    with track_processes(tracker):
        async for _ in stream_process(_invocation()):
            pass


async def _drive_run(tracker: ProcessTracker) -> None:
    with track_processes(tracker):
        await run_process(_invocation())


async def _settle(iterations: int = 25) -> None:
    for _ in range(iterations):
        await asyncio.sleep(0)


DRIVERS = pytest.mark.parametrize(
    "driver", [_drive_stream, _drive_run], ids=["stream_process", "run_process"]
)


@DRIVERS
@pytest.mark.asyncio
async def test_cancel_during_spawn_kills_the_group_before_the_cancel_propagates(
    slow_spawn, driver
):
    tracker = _Tracker()
    seen: dict = {}

    async def run() -> None:
        try:
            await driver(tracker)
        except asyncio.CancelledError:
            # What a stop acknowledgement would read at this point.
            seen["rows"] = list(tracker.spawned_pids)
            seen["kills"] = len(slow_spawn["spy"].calls)
            raise

    task = asyncio.create_task(run())
    await _settle()
    task.cancel()
    await _settle()
    assert not task.done()  # held until the child exists, so it can be killed

    slow_spawn["gate"].set()
    with pytest.raises(asyncio.CancelledError):
        await task

    process = slow_spawn["process"]
    assert seen == {"rows": [process.pid], "kills": 1}
    assert slow_spawn["spy"].calls == [(process, tracker)]
    await _settle()
    assert tracker.exited_pids == [process.pid]
    assert tracker.gone() is True


@DRIVERS
@pytest.mark.asyncio
async def test_repeated_cancels_during_spawn_do_not_end_the_caller_early(
    slow_spawn, driver
):
    """The caller must not finish while a forked child is unrecorded.

    A stop acknowledgement reads ``gone()`` the moment the task ends; ending on
    the second cancel would do so before the pid is recorded or the group killed.
    """
    tracker = _Tracker()
    seen: dict = {}

    async def run() -> None:
        try:
            await driver(tracker)
        except asyncio.CancelledError:
            seen["rows"] = list(tracker.spawned_pids)
            seen["kills"] = len(slow_spawn["spy"].calls)
            raise

    task = asyncio.create_task(run())
    await _settle()
    for _ in range(3):
        task.cancel()
        await _settle()
        assert not task.done()
    assert slow_spawn["spy"].calls == []

    slow_spawn["gate"].set()
    with pytest.raises(asyncio.CancelledError):
        await task

    process = slow_spawn["process"]
    assert seen == {"rows": [process.pid], "kills": 1}
    assert slow_spawn["spy"].calls == [(process, tracker)]
    await _settle()
    assert tracker.exited_pids == [process.pid]
    assert tracker.gone() is True


@pytest.mark.asyncio
async def test_a_spawn_that_fails_while_cancelled_has_nothing_to_kill(slow_spawn):
    slow_spawn["error"] = FileNotFoundError("nope")
    loop = asyncio.get_running_loop()
    unretrieved: list[dict] = []
    loop.set_exception_handler(lambda _loop, context: unretrieved.append(context))
    task = asyncio.create_task(_drive_stream(_Tracker()))
    await _settle()
    task.cancel()
    slow_spawn["gate"].set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _settle()
    gc.collect()

    assert slow_spawn["spy"].calls == []
    assert unretrieved == []  # no "Task exception was never retrieved"


@DRIVERS
@pytest.mark.parametrize(
    ("error", "message"),
    [
        (FileNotFoundError("nope"), "not found"),
        (PermissionError("nope"), "cannot be executed"),
    ],
    ids=["missing", "not-executable"],
)
@pytest.mark.asyncio
async def test_a_failed_spawn_still_reports_the_cli_unavailable(
    slow_spawn, driver, error, message
):
    slow_spawn["error"] = error
    slow_spawn["gate"].set()
    tracker = _Tracker()

    with pytest.raises(NativeCliUnavailable, match=message):
        await driver(tracker)

    assert tracker.spawned_pids == []


def _marker_processes(marker: str) -> list[int]:
    """Live (non-zombie) processes whose command line carries the marker."""
    output = subprocess.run(
        ["ps", "-e", "-o", "pid=,stat=,command="],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    found = []
    for line in output.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and marker in parts[2] and not parts[1].startswith("Z"):
            found.append(int(parts[0]))
    return found


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.requires_ps
@DRIVERS
@pytest.mark.asyncio
async def test_a_real_descendant_dies_when_cancelled_while_the_loop_is_busy(driver):
    # A busy loop delays asyncio handing the child back; the shell forks its
    # descendant meanwhile. Unique duration so only this test's process matches.
    marker = f"sleep {7000 + os.getpid() % 1000}.{time.monotonic_ns() % 997:03d}"
    invocation = CliInvocation(
        argv=("/bin/sh", "-c", f"{marker} & wait"), timeout_seconds=30.0
    )
    tracker = ProcessTracker()

    async def consume() -> None:
        with track_processes(tracker):
            if driver is _drive_stream:
                async for _ in stream_process(invocation):
                    pass
            else:
                await run_process(invocation)

    task = asyncio.create_task(consume())
    try:
        await asyncio.sleep(0)
        time.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        deadline = time.monotonic() + 3
        while _marker_processes(marker) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert _marker_processes(marker) == []
        # The leader was recorded, so the stop evidence is not vacuous.
        assert tracker.evidence()["processes"]
    finally:
        for pid in _marker_processes(marker):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


class _StandIn:
    """A process object whose pid may name a stranger after pid reuse."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.kill_calls = 0

    def kill(self) -> None:
        self.kill_calls += 1


@pytest.fixture
def signals(monkeypatch):
    sent: dict[str, list] = {"killpg": [], "kill": []}
    monkeypatch.setattr(
        process_mod.os, "killpg", lambda pgid, sig: sent["killpg"].append((pgid, sig))
    )
    monkeypatch.setattr(
        process_mod.os, "kill", lambda pid, sig: sent["kill"].append((pid, sig))
    )
    monkeypatch.setattr(process_mod, "_descendants", lambda pid: [])
    return sent


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
def test_kill_tree_signals_exactly_the_group_it_created(signals, monkeypatch):
    monkeypatch.setattr(
        process_mod.os,
        "getpgid",
        lambda pid: pytest.fail("looked up the group of a possibly recycled pid"),
    )
    process = _StandIn(4242)

    _kill_tree(process)

    assert signals["killpg"] == [(4242, signal.SIGKILL)]
    assert process.kill_calls == 0


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
def test_kill_tree_never_signals_the_callers_own_group(signals):
    process = _StandIn(os.getpgrp())  # what a recycled leader pid could look like

    _kill_tree(process)

    assert signals["killpg"] == []
    assert process.kill_calls == 1  # falls back to the one process it owns


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
def test_kill_tree_never_signals_the_callers_group_for_a_captured_descendant(
    signals, monkeypatch
):
    own = os.getpgrp()
    monkeypatch.setattr(
        process_mod, "_descendants", lambda pid: [(777, own), (778, 778)]
    )
    process = _StandIn(4242)

    _kill_tree(process)

    assert signals["killpg"] == [(4242, signal.SIGKILL), (778, signal.SIGKILL)]
    assert signals["kill"] == [(777, signal.SIGKILL)]
