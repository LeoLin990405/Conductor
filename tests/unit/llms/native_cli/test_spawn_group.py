"""The child-owning helpers that clients share with ``stream_process``.

``spawn_group`` starts a child that leads its own session and survives a cancel
landing during the start; ``kill_child_group`` and ``terminate_child`` stop it
for good. The sites that use them have their own tests
(``test_spawn_cancel_sites.py``); these pin the helpers' contract.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time

import pytest

from src.llms.native_cli import process as process_mod
from src.llms.native_cli.process import (
    kill_child_group,
    spawn_group,
    terminate_child,
)


class _Child:
    """A leader that records signals and exits on SIGTERM only if it is told to."""

    pid = 424243

    def __init__(self, *, exits_on_terminate: bool) -> None:
        self.returncode: int | None = None
        self._exited = asyncio.Event()
        self._exits_on_terminate = exits_on_terminate
        self.terminate_calls = 0
        self.kill_calls = 0

    def exit(self, code: int) -> None:
        self.returncode = code
        self._exited.set()

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self._exits_on_terminate:
            self.exit(-15)

    def kill(self) -> None:
        self.kill_calls += 1
        self.exit(-9)

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode


@pytest.fixture
def groups(monkeypatch):
    """Records every group signal instead of sending it."""
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process_mod.os, "killpg", lambda pgid, sig: sent.append((pgid, sig))
    )
    monkeypatch.setattr(process_mod, "_descendants", lambda pid: [])
    return sent


@pytest.mark.asyncio
async def test_spawn_group_starts_the_child_in_its_own_session(monkeypatch):
    seen: dict = {}
    child = _Child(exits_on_terminate=True)

    async def fake_exec(*argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    assert await spawn_group("agent", "--flag", cwd="/work") is child
    assert seen["argv"] == ("agent", "--flag")
    assert seen["kwargs"] == {"cwd": "/work", "start_new_session": True}


@pytest.mark.asyncio
async def test_spawn_group_refuses_a_caller_that_would_share_our_group(monkeypatch):
    async def fake_exec(*argv, **kwargs):
        pytest.fail("a child that cannot be group-killed must not be started")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(TypeError):
        await spawn_group("agent", start_new_session=False)


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
def test_kill_child_group_signals_exactly_the_group_the_child_leads(groups):
    child = _Child(exits_on_terminate=True)

    kill_child_group(child)

    assert groups == [(child.pid, signal.SIGKILL)]
    assert child.kill_calls == 0


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
def test_kill_child_group_never_signals_the_callers_own_group(groups):
    child = _Child(exits_on_terminate=True)
    child.pid = os.getpgrp()  # what a recycled leader pid could look like

    kill_child_group(child)

    assert groups == []
    assert child.kill_calls == 1  # falls back to the one process it owns


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
def test_kill_child_group_on_a_dead_child_is_harmless(monkeypatch):
    def gone(pgid, sig):
        raise ProcessLookupError(pgid)

    monkeypatch.setattr(process_mod.os, "killpg", gone)
    child = _Child(exits_on_terminate=True)
    child.exit(0)

    kill_child_group(child)  # no group, no leader: nothing raised


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_terminate_child_sweeps_the_group_after_a_graceful_exit(groups):
    """A leader that exits on SIGTERM can still leave tools of its own behind."""
    child = _Child(exits_on_terminate=True)

    await terminate_child(child, grace=1.0)

    assert child.terminate_calls == 1
    assert groups == [(child.pid, signal.SIGKILL)]


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_terminate_child_kills_the_group_of_a_leader_that_ignores_sigterm(
    groups,
):
    child = _Child(exits_on_terminate=False)

    started = time.monotonic()
    await terminate_child(child, grace=0.05)

    assert time.monotonic() - started < 2
    assert child.terminate_calls == 1
    assert groups == [(child.pid, signal.SIGKILL)]


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_cancel_during_the_grace_wait_cannot_skip_the_group_kill(groups):
    child = _Child(exits_on_terminate=False)
    seen: dict = {}

    async def stop() -> None:
        try:
            await terminate_child(child, grace=30.0)
        except asyncio.CancelledError:
            seen["kills"] = list(groups)  # what the cancelled frame already sees
            raise

    task = asyncio.create_task(stop())
    for _ in range(10):
        await asyncio.sleep(0)
    assert child.terminate_calls == 1
    assert groups == []  # still inside the grace period

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen["kills"] == [(child.pid, signal.SIGKILL)]


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_terminate_child_leaves_a_group_alone_once_its_leader_was_reaped(groups):
    """A client closed long after its agent died would aim at a free group id."""
    child = _Child(exits_on_terminate=True)
    child.exit(0)  # reaped before the close; its pid may name anyone by now

    await terminate_child(child, grace=1.0)

    assert groups == []
    assert child.terminate_calls == child.kill_calls == 0


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_cancelled_group_spawn_takes_no_ps_snapshot(groups, monkeypatch):
    """The snapshot blocks the event loop; only an owner with a tracker wants it."""
    snapshots: list[int] = []
    monkeypatch.setattr(
        process_mod, "_descendants", lambda pid: snapshots.append(pid) or []
    )
    gate = asyncio.Event()
    child = _Child(exits_on_terminate=True)

    async def fake_exec(*argv, **kwargs):
        await gate.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    task = asyncio.create_task(spawn_group("agent"))
    for _ in range(10):
        await asyncio.sleep(0)

    task.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert groups == [(child.pid, signal.SIGKILL)]
    assert snapshots == []


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


async def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.requires_ps
@pytest.mark.asyncio
async def test_a_real_descendant_dies_when_a_busy_loop_delays_the_cancelled_start():
    # Same shape as the stream_process test: the shell forks its descendant
    # while the loop is too busy to hand the child back, then the start is
    # cancelled. Unique duration so only this test's process matches.
    marker = f"sleep {6000 + os.getpid() % 1000}.{time.monotonic_ns() % 997:03d}"

    task = asyncio.create_task(
        spawn_group("/bin/sh", "-c", f"{marker} & wait", stdout=subprocess.DEVNULL)
    )
    try:
        await asyncio.sleep(0)
        time.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await _wait_until(lambda: _marker_processes(marker) == [])
    finally:
        for pid in _marker_processes(marker):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.requires_ps
@pytest.mark.asyncio
async def test_a_real_terminate_child_takes_the_leaders_descendants_with_it():
    marker = f"sleep {5000 + os.getpid() % 1000}.{time.monotonic_ns() % 997:03d}"
    child = await spawn_group("/bin/sh", "-c", f"{marker} & wait")
    try:
        assert await _wait_until(lambda: bool(_marker_processes(marker)))
        assert os.getpgid(child.pid) == child.pid != os.getpgrp()

        await terminate_child(child, grace=3.0)

        assert await _wait_until(lambda: _marker_processes(marker) == [])
        await asyncio.wait_for(child.wait(), timeout=3)
    finally:
        for pid in _marker_processes(marker):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


@pytest.mark.asyncio
async def test_spawn_group_registers_the_child_when_a_tracker_is_active(monkeypatch):
    from src.llms.native_cli.process import ProcessTracker, track_processes

    child = _Child(exits_on_terminate=True)

    async def fake_exec(*argv, **kwargs):
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    tracker = ProcessTracker()
    with track_processes(tracker):
        assert await spawn_group("agent") is child

    evidence = tracker.evidence()["processes"]
    assert evidence and evidence[0]["pid"] == child.pid
