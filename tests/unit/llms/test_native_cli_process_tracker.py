"""Native CLI process tracking backs the daemon's stop acknowledgement."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time

import pytest

from src.llms.native_cli.base import CliInvocation
from src.llms.native_cli.process import (
    ProcessTracker,
    _descendants,
    _kill_tree,
    run_process,
    track_processes,
)

# A leader that starts its own session and a child that starts yet another
# one, mirroring a CLI daemonizing a tool: the child leaves the leader's
# process group, so a group kill never reaches it.
_DETACHED_LEADER = """
import subprocess, sys, time
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    start_new_session=True,
)
print(child.pid, flush=True)
time.sleep(60)
"""


async def _spawn_leader_with_detached_child() -> tuple[asyncio.subprocess.Process, int]:
    leader = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _DETACHED_LEADER,
        start_new_session=True,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    child_pid = int(await leader.stdout.readline())
    return leader, child_pid


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


async def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


@pytest.mark.asyncio
async def test_run_process_records_and_reaps_its_child():
    tracker = ProcessTracker()
    with track_processes(tracker):
        await run_process(
            CliInvocation(
                argv=(sys.executable, "-c", "print('ok')"),
                timeout_seconds=30,
            )
        )

    evidence = tracker.evidence()
    assert tracker.gone() is True
    assert len(evidence["processes"]) == 1
    process = evidence["processes"][0]
    assert process["exited"] is True
    assert process["alive"] is False
    assert process["pgid"] == process["pid"]


def test_a_live_leader_or_group_is_not_reported_gone():
    tracker = ProcessTracker()
    tracker.spawned(os.getpid(), pgid=os.getpgrp())

    assert tracker.gone() is False
    assert tracker.evidence()["processes"][0]["alive"] is True

    # Reaping the leader is not enough: a surviving group means children
    # (MCP servers or tools) may still be running.
    tracker.exited(os.getpid())
    assert tracker.gone() is False


def test_tracking_is_scoped_to_the_current_task_context():
    tracker = ProcessTracker()
    with track_processes(tracker):
        assert tracker.gone() is True
    assert tracker.evidence()["processes"] == []


posix_only = pytest.mark.skipif(
    os.name != "posix", reason="detached sessions are POSIX-only"
)


@posix_only
@pytest.mark.requires_ps
@pytest.mark.asyncio
async def test_kill_tree_contains_and_tracks_a_detached_descendant():
    """A setsid child must die with the tree and show up in the evidence."""
    leader, child_pid = await _spawn_leader_with_detached_child()
    tracker = ProcessTracker()
    tracker.spawned(leader.pid)
    try:
        _kill_tree(leader, tracker)
        await leader.wait()
        tracker.exited(leader.pid)

        assert await _wait_until(lambda: not _pid_alive(child_pid)), (
            "detached descendant survived the tree kill"
        )
        assert await _wait_until(tracker.gone)
        pids = [row["pid"] for row in tracker.evidence()["processes"]]
        assert pids[0] == leader.pid
        assert child_pid in pids
    finally:
        _kill_pid(child_pid)
        _kill_pid(leader.pid)


@posix_only
@pytest.mark.requires_ps
@pytest.mark.asyncio
async def test_a_live_detached_descendant_blocks_gone_until_it_dies():
    """gone() must not claim a verified stop while a detached child runs.

    Regression for the review finding: the leader and its original group were
    the only things checked, so a setsid child that outlived a cancellation
    still produced process_gone=true.
    """
    leader, child_pid = await _spawn_leader_with_detached_child()
    tracker = ProcessTracker()
    tracker.spawned(leader.pid)
    try:
        captured = tracker.capture_descendants(leader.pid)
        # The child runs in its own session, which the leader's group kill
        # cannot reach.
        assert (child_pid, child_pid) in captured
        assert tracker.capture_descendants(leader.pid) == []

        os.killpg(leader.pid, signal.SIGKILL)
        await leader.wait()
        tracker.exited(leader.pid)

        assert _pid_alive(child_pid)
        assert tracker.gone() is False
        rows = {row["pid"]: row for row in tracker.evidence()["processes"]}
        assert rows[child_pid]["alive"] is True

        _kill_pid(child_pid)
        assert await _wait_until(lambda: not _pid_alive(child_pid))
        assert await _wait_until(tracker.gone)
    finally:
        _kill_pid(child_pid)
        _kill_pid(leader.pid)


@posix_only
@pytest.mark.requires_ps
@pytest.mark.asyncio
async def test_cancelling_run_process_kills_and_tracks_the_detached_child():
    """The cancellation path passes its tracker into the tree kill."""
    tracker = ProcessTracker()
    with track_processes(tracker):
        task = asyncio.create_task(
            run_process(
                CliInvocation(
                    argv=(sys.executable, "-c", _DETACHED_LEADER), timeout_seconds=30
                )
            )
        )
    child_pid: int | None = None
    try:
        assert await _wait_until(lambda: bool(tracker.evidence()["processes"]))
        leader_pid = int(tracker.evidence()["processes"][0]["pid"])
        assert await _wait_until(lambda: _descendants(leader_pid) != [])
        child_pid = _descendants(leader_pid)[0][0]

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await _wait_until(lambda: not _pid_alive(child_pid))
        assert await _wait_until(tracker.gone)
        pids = [row["pid"] for row in tracker.evidence()["processes"]]
        assert child_pid in pids
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if child_pid is not None:
            _kill_pid(child_pid)
