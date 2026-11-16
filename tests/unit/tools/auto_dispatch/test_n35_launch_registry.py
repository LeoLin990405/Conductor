"""N35: launch registry (pgid per run) and adopt-or-stop before a relaunch.

A fake process probe stands in for the OS: nothing here signals a real
process. Records live under ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal

import pytest

from src.tools.auto_dispatch import launch_registry as reg
from src.tools.auto_dispatch.protocols import LaunchError


class FakeProbe(reg.ProcessProbe):
    def __init__(self) -> None:
        self.leaders: dict[int, tuple[int, str]] = {}  # pid -> (pgid, start)
        self.groups: set[int] = set()
        self.signals: list[tuple[int, int]] = []
        self.ignore_term: set[int] = set()
        self.immortal: set[int] = set()

    def run(self, pid: int, start: str = "Fri Nov 14 10:00:00 2026") -> None:
        self.leaders[pid] = (pid, start)
        self.groups.add(pid)

    def pgid(self, pid: int) -> int | None:
        entry = self.leaders.get(pid)
        return entry[0] if entry else None

    def start_time(self, pid: int) -> str:
        entry = self.leaders.get(pid)
        return entry[1] if entry else ""

    def group_alive(self, pgid: int) -> bool:
        return pgid in self.groups

    def signal_group(self, pgid: int, sig: int) -> None:
        self.signals.append((pgid, sig))
        if pgid not in self.groups:
            raise ProcessLookupError(pgid)
        if pgid in self.immortal or (sig == signal.SIGTERM and pgid in self.ignore_term):
            return
        self.groups.discard(pgid)
        self.leaders = {p: v for p, v in self.leaders.items() if v[0] != pgid}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _settle(tmp_path, probe, mode, *, head="e" * 40, **kw):
    clock = Clock()

    async def head_reader():
        return head

    return asyncio.run(
        reg.settle_earlier_launch(
            tmp_path,
            "line-1-abc",
            mode=mode,
            probe=probe,
            head_reader=head_reader,
            sleep=clock.sleep,
            clock=clock,
            **kw,
        )
    )


def _spawned(tmp_path, probe, pid=7000):
    probe.run(pid)
    return reg.record_spawn(
        tmp_path,
        "line-1-abc",
        pid=pid,
        cwd="/wt/line-1",
        line_id="1",
        dispatch_id="d1",
        start_head="a" * 40,
        probe=probe,
    )


def test_spawn_records_the_process_group_privately(tmp_path) -> None:
    probe = FakeProbe()
    record = _spawned(tmp_path, probe)
    path = reg.record_path(tmp_path, "line-1-abc")
    assert path.parent == tmp_path / "launches"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    data = json.loads(path.read_text())
    assert data["pgid"] == 7000 and data["pid"] == 7000
    assert data["leader_start"] and data["start_head"] == "a" * 40
    assert reg.read_record(tmp_path, "line-1-abc") == record


def test_exit_143_records_the_killed_runs_end_head(tmp_path) -> None:
    probe = FakeProbe()
    _spawned(tmp_path, probe)
    reg.record_exit(tmp_path, "line-1-abc", exit_code=143, end_head="b" * 40)
    assert reg.interrupted_run_heads(tmp_path, "line-1-abc") == ("a" * 40, "b" * 40)


def test_a_clean_exit_is_not_an_interrupted_run(tmp_path) -> None:
    probe = FakeProbe()
    _spawned(tmp_path, probe)
    reg.record_exit(tmp_path, "line-1-abc", exit_code=0, end_head="b" * 40)
    assert reg.interrupted_run_heads(tmp_path, "line-1-abc") is None


def test_no_record_means_nothing_to_settle(tmp_path) -> None:
    assert _settle(tmp_path, FakeProbe(), "enforce").action == "none"


def test_enforce_stops_a_live_earlier_launch_before_relaunching(tmp_path) -> None:
    probe = FakeProbe()
    _spawned(tmp_path, probe)
    result = _settle(tmp_path, probe, "enforce")
    assert result.action == "stopped"
    assert probe.signals == [(7000, signal.SIGTERM)]
    record = reg.read_record(tmp_path, "line-1-abc")
    assert record.ended and record.killed and record.ended_by == "relaunch"
    assert record.end_head == "e" * 40
    assert reg.interrupted_run_heads(tmp_path, "line-1-abc") == ("a" * 40, "e" * 40)


def test_enforce_kills_a_launch_that_ignores_term(tmp_path) -> None:
    probe = FakeProbe()
    _spawned(tmp_path, probe)
    probe.ignore_term.add(7000)
    assert _settle(tmp_path, probe, "enforce").action == "stopped"
    assert probe.signals == [(7000, signal.SIGTERM), (7000, signal.SIGKILL)]


def test_enforce_refuses_when_the_launch_survives_kill(tmp_path) -> None:
    probe = FakeProbe()
    _spawned(tmp_path, probe)
    probe.immortal.add(7000)
    with pytest.raises(LaunchError):
        _settle(tmp_path, probe, "enforce")


def test_enforce_adopts_a_launch_that_finishes_in_the_wait(tmp_path) -> None:
    probe = FakeProbe()
    _spawned(tmp_path, probe)
    clock = Clock()

    async def finishing_sleep(seconds: float) -> None:
        clock.now += seconds
        if clock.now >= 3:
            probe.groups.discard(7000)
            probe.leaders.pop(7000, None)

    async def head_reader():
        return "c" * 40

    result = asyncio.run(
        reg.settle_earlier_launch(
            tmp_path, "line-1-abc", mode="enforce", probe=probe,
            head_reader=head_reader, adopt_wait=10, sleep=finishing_sleep, clock=clock,
        )
    )
    assert result.action == "adopted"
    assert probe.signals == []
    assert reg.read_record(tmp_path, "line-1-abc").end_head == "c" * 40


def test_shadow_only_reports_a_live_earlier_launch(tmp_path) -> None:
    probe = FakeProbe()
    _spawned(tmp_path, probe)
    assert _settle(tmp_path, probe, "shadow").action == "seen"
    assert probe.signals == []
    assert not reg.read_record(tmp_path, "line-1-abc").ended


def test_off_never_reads_the_record(tmp_path) -> None:
    probe = FakeProbe()
    _spawned(tmp_path, probe)
    assert _settle(tmp_path, probe, "off").action == "off"
    assert probe.signals == []


def test_a_launch_that_ended_unwatched_gets_its_end_head(tmp_path) -> None:
    probe = FakeProbe()
    _spawned(tmp_path, probe)
    probe.groups.clear()
    probe.leaders.clear()
    result = _settle(tmp_path, probe, "shadow")
    assert result.action == "ended_unwatched"
    assert reg.interrupted_run_heads(tmp_path, "line-1-abc") == ("a" * 40, "e" * 40)


def test_a_reused_pid_is_never_signalled(tmp_path) -> None:
    probe = FakeProbe()
    _spawned(tmp_path, probe)
    # The launch ended; a new session leader got the same pid later.
    probe.run(7000, start="Fri Nov 14 11:30:00 2026")
    result = _settle(tmp_path, probe, "enforce")
    assert result.action == "ended_unwatched"
    assert probe.signals == []


def test_members_left_after_the_leader_exit_are_stopped(tmp_path) -> None:
    probe = FakeProbe()
    _spawned(tmp_path, probe)
    probe.leaders.clear()  # leader gone, Worker still in group 7000
    assert _settle(tmp_path, probe, "enforce").action == "stopped"
    assert probe.signals == [(7000, signal.SIGTERM)]


def test_an_unverifiable_leader_blocks_enforce_and_is_not_signalled(tmp_path) -> None:
    probe = FakeProbe()
    probe.run(7000, start="")
    reg.record_spawn(tmp_path, "line-1-abc", pid=7000, probe=probe)
    with pytest.raises(reg.EarlierLaunchRunning):
        _settle(tmp_path, probe, "enforce")
    assert _settle(tmp_path, probe, "shadow").action == "unverified"
    assert probe.signals == []


def test_own_group_is_never_signalled(tmp_path) -> None:
    probe = FakeProbe()
    own = os.getpgrp()
    probe.run(own)
    reg.record_spawn(tmp_path, "line-1-abc", pid=own, probe=probe)
    with pytest.raises(LaunchError):
        _settle(tmp_path, probe, "enforce")
    assert probe.signals == []


def test_mode_defaults_to_shadow_and_rejects_unknown_values() -> None:
    assert reg.adopt_mode({}) == "shadow"
    assert reg.adopt_mode({reg.MODE_ENV: "ENFORCE"}) == "enforce"
    assert reg.adopt_mode({reg.MODE_ENV: "yes"}) == "shadow"
    assert reg.adopt_wait_seconds({reg.ADOPT_WAIT_ENV: "nan"}) == 0.0
    assert reg.adopt_wait_seconds({reg.ADOPT_WAIT_ENV: "30"}) == 30.0


def test_a_symlinked_record_is_neither_read_nor_followed(tmp_path) -> None:
    probe = FakeProbe()
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("{}")
    (tmp_path / "launches").mkdir(mode=0o700)
    reg.record_path(tmp_path, "line-1-abc").symlink_to(elsewhere)
    assert reg.read_record(tmp_path, "line-1-abc") is None
    probe.run(7000)
    reg.record_spawn(tmp_path, "line-1-abc", pid=7000, probe=probe)
    assert elsewhere.read_text() == "{}"


def test_an_unsafe_run_id_gets_a_hashed_file_name(tmp_path) -> None:
    probe = FakeProbe()
    probe.run(7000)
    reg.record_spawn(tmp_path, "../../etc/x", pid=7000, probe=probe)
    path = reg.record_path(tmp_path, "../../etc/x")
    assert path.parent == tmp_path / "launches" and path.name.startswith("h-")
    assert reg.read_record(tmp_path, "../../etc/x").pid == 7000


def test_the_real_probe_sees_this_process() -> None:
    probe = reg.ProcessProbe()
    assert probe.pgid(os.getpid()) == os.getpgrp()
    assert probe.group_alive(os.getpgrp())
