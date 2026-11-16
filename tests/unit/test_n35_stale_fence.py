"""N35 finding 9: a fence left by a SIGKILLed cond is lifted once its owner is gone.

Fake pids and probes only; every file lives under ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from src.conductor_cli import drain_owner
from src.conductor_cli.drain import drain_path, is_drained, read_drain, set_drain
from src.conductor_cli.drain_owner import (
    clear_stale_fence,
    drained_live,
    set_owned_drain,
)
from src.conductor_cli.ops_cli import STOP_FENCE_REASON, main
from src.tools.auto_dispatch.drain import DrainAwareLauncher
from tests.unit.test_n35_cond_stop_fence import _fenced

DEAD = 999_991
LIVE = 999_992


def _alive(pid: int) -> bool:
    return pid == LIVE


def _start(pid: int) -> str | None:
    return "Sat Nov 14 10:00:00 2026" if pid == LIVE else None


def _write(home, payload) -> None:
    path = drain_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _cond_fence(home, pid: int, start: str = "") -> None:
    _write(
        home,
        {
            "set_at": "2026-11-14T10:00:00+00:00",
            "reason": STOP_FENCE_REASON.format(action="stop"),
            "owner_pid": pid,
            "owner_start": start,
        },
    )


def test_a_dead_owners_fence_is_cleared(tmp_path) -> None:
    _cond_fence(tmp_path, DEAD)
    removed = clear_stale_fence(tmp_path, alive=_alive, start_of=_start)
    assert removed is not None and removed["owner_pid"] == DEAD
    assert not is_drained(tmp_path)


def test_a_live_owners_fence_stays(tmp_path) -> None:
    _cond_fence(tmp_path, LIVE, _start(LIVE) or "")
    assert clear_stale_fence(tmp_path, alive=_alive, start_of=_start) is None
    assert drained_live(tmp_path, alive=_alive, start_of=_start)


def test_a_reused_pid_does_not_keep_the_fence(tmp_path) -> None:
    _cond_fence(tmp_path, LIVE, "Fri Nov 13 09:00:00 2026")
    assert clear_stale_fence(tmp_path, alive=_alive, start_of=_start) is not None
    assert not is_drained(tmp_path)


def test_an_unreadable_start_time_keeps_a_live_owners_fence(tmp_path) -> None:
    _cond_fence(tmp_path, LIVE, "Fri Nov 13 09:00:00 2026")
    assert clear_stale_fence(tmp_path, alive=_alive, start_of=lambda _p: None) is None
    assert is_drained(tmp_path)


@pytest.mark.parametrize(
    "payload",
    [
        {"set_at": "", "reason": "deploy prep"},
        {"set_at": "", "reason": "deploy prep", "owner_pid": DEAD},
        {"set_at": "", "reason": STOP_FENCE_REASON.format(action="stop")},
        {"set_at": "", "reason": "cond stop: stop requested", "owner_pid": "x"},
    ],
    ids=["operator", "operator-with-pid", "cond-reason-no-pid", "bad-pid"],
)
def test_an_operator_drain_is_never_cleared(tmp_path, payload) -> None:
    _write(tmp_path, payload)
    assert clear_stale_fence(tmp_path, alive=_alive, start_of=_start) is None
    assert drained_live(tmp_path, alive=_alive, start_of=_start)


def test_cond_drain_writes_no_owner(tmp_path) -> None:
    set_drain(tmp_path, STOP_FENCE_REASON.format(action="stop"))
    assert clear_stale_fence(tmp_path, alive=lambda _p: False) is None
    assert is_drained(tmp_path)


def test_set_owned_drain_records_pid_and_start(tmp_path) -> None:
    set_owned_drain(tmp_path, "cond restart --drain", pid=LIVE, start_of=_start)
    raw = json.loads(drain_path(tmp_path).read_text(encoding="utf-8"))
    assert raw["owner_pid"] == LIVE and raw["owner_start"] == _start(LIVE)
    assert read_drain(tmp_path)["reason"] == "cond restart --drain"


def test_the_real_probe_sees_this_process(tmp_path) -> None:
    set_owned_drain(tmp_path, "cond restart --drain")
    assert clear_stale_fence(tmp_path) is None, "our own live fence was removed"


@pytest.fixture
def fake_probes(monkeypatch):
    monkeypatch.setattr(drain_owner, "pid_alive", _alive)
    monkeypatch.setattr(drain_owner, "process_start", _start)


def test_the_dispatch_guard_treats_a_dead_fence_as_clear(tmp_path, fake_probes) -> None:
    _cond_fence(tmp_path, DEAD)
    calls: list[str] = []

    class Inner:
        async def launch(self, line, **_kw):
            calls.append("launch")
            return "ok"

    async def no_sleep(_s: float) -> None:
        raise AssertionError("the guard held on a dead owner's fence")

    guard = DrainAwareLauncher(Inner(), home=tmp_path, sleep=no_sleep)
    assert asyncio.run(guard.launch(object())) == "ok"
    assert calls == ["launch"]
    assert not is_drained(tmp_path)


def test_the_dispatch_guard_holds_on_a_live_fence(tmp_path, fake_probes) -> None:
    _cond_fence(tmp_path, LIVE, _start(LIVE) or "")
    guard = DrainAwareLauncher(object(), home=tmp_path)
    assert guard._drained() is True


def test_a_later_cond_run_clears_a_dead_fence(tmp_path, monkeypatch) -> None:
    fake, _ = _fenced(tmp_path, monkeypatch)
    monkeypatch.setattr(drain_owner, "pid_alive", _alive)
    monkeypatch.setattr(drain_owner, "process_start", _start)
    _cond_fence(fake.home, DEAD)
    main(["status"], system=fake)
    assert not is_drained(fake.home)


def test_a_later_cond_run_keeps_live_and_operator_drains(tmp_path, monkeypatch) -> None:
    fake, _ = _fenced(tmp_path, monkeypatch)
    monkeypatch.setattr(drain_owner, "pid_alive", _alive)
    monkeypatch.setattr(drain_owner, "process_start", _start)
    _cond_fence(fake.home, LIVE, _start(LIVE) or "")
    main(["status"], system=fake)
    assert is_drained(fake.home)
    set_drain(fake.home, "deploy prep")
    main(["status"], system=fake)
    assert read_drain(fake.home)["reason"] == "deploy prep"


def test_the_stop_fence_records_its_owner(tmp_path, monkeypatch) -> None:
    fake, _ = _fenced(tmp_path, monkeypatch)
    seen: list[dict] = []
    real_scan = fake.processes

    def scan() -> str:
        if drain_path(fake.home).exists():
            seen.append(json.loads(drain_path(fake.home).read_text(encoding="utf-8")))
        return real_scan()

    monkeypatch.setattr(fake, "processes", scan)
    assert main(["down"], system=fake) == 0
    assert seen, "the fence was on during the launch scan"
    assert seen[0]["owner_pid"] == os.getpid()
    assert seen[0]["reason"] == STOP_FENCE_REASON.format(action="stop")
    assert not is_drained(fake.home)


def test_the_stop_fence_replaces_a_dead_owners_fence(tmp_path, monkeypatch) -> None:
    fake, _ = _fenced(tmp_path, monkeypatch)
    monkeypatch.setattr(drain_owner, "pid_alive", _alive)
    monkeypatch.setattr(drain_owner, "process_start", _start)
    _cond_fence(fake.home, DEAD)
    assert main(["restart"], system=fake) == 0
    assert fake.drained_at_scan and all(fake.drained_at_scan)
    assert not is_drained(fake.home), "a dead cond's fence outlived the restart"
