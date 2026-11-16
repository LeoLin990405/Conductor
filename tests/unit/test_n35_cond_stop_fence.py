"""N35: cond holds new dispatch launches while it stops the backend; doctor orphans.

Fakes only (the ``ProcFakeSystem`` of the N24 tests): nothing lists, signals
or stops a real process, and every file lives under ``tmp_path``.
"""

from __future__ import annotations

import json

from src.conductor_cli.drain import is_drained, read_drain, set_drain
from src.conductor_cli.ops_cli import main
from src.conductor_cli.ops_dispatch import orphan_processes
from src.conductor_cli.ops_support import load_settings
from tests.unit.test_cond_dispatch_orphans import (
    ProcFakeSystem,
    _add_launch,
    _script,
)
from tests.unit.test_cond_ops import _layout


class FencedFake(ProcFakeSystem):
    """Records whether the drain flag was set at every process-table scan."""

    def __init__(self) -> None:
        super().__init__()
        self.home = None
        self.drained_at_scan: list[bool] = []
        self.on_first_sleep = None

    def processes(self) -> str:
        if self.home is not None:
            self.drained_at_scan.append(is_drained(self.home))
        return super().processes()

    def sleep(self, seconds: float) -> None:
        hook, self.on_first_sleep = self.on_first_sleep, None
        super().sleep(seconds)
        if hook is not None:
            hook()


def _fenced(tmp_path, monkeypatch) -> tuple[FencedFake, object]:
    home = _layout(tmp_path, monkeypatch)
    for name in (
        "CONDUCTOR_DISPATCH_LAUNCH_SCRIPT",
        "CONDUCTOR_DISPATCH_REPO",
        "CONDUCTOR_DISPATCH_WAIT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    fake = FencedFake()
    assert main(["up"], system=fake) == 0
    script = _script(home)
    fake.home = load_settings().conductor_home
    fake.drained_at_scan.clear()
    return fake, script


def test_restart_scans_launches_with_new_launches_held(tmp_path, monkeypatch) -> None:
    fake, _script_path = _fenced(tmp_path, monkeypatch)
    assert main(["restart"], system=fake) == 0
    assert fake.drained_at_scan, "the launch check ran"
    assert all(fake.drained_at_scan), "a launch could start between check and stop"
    assert not is_drained(fake.home), "the fence is lifted after the restart"


def test_a_launch_that_starts_during_the_grace_is_waited_for(
    tmp_path, monkeypatch
) -> None:
    fake, script = _fenced(tmp_path, monkeypatch)
    # It passed its drain check just before the fence and spawns a moment later.
    fake.on_first_sleep = lambda: _add_launch(fake, script, ends_at=fake.now + 5)
    assert main(["restart"], system=fake) == 0
    when, still_running = fake.backend_stops[0]
    assert still_running == [], "the backend stopped beside a fresh launch"
    assert when >= 5


def test_force_stops_a_launch_that_starts_during_the_grace(tmp_path, monkeypatch) -> None:
    fake, script = _fenced(tmp_path, monkeypatch)
    fake.on_first_sleep = lambda: _add_launch(fake, script)
    assert main(["restart", "--force"], system=fake) == 0
    assert [pgid for pgid, _sig in fake.signals] == [7000]
    assert 7000 not in fake.backend_stops[0][1]


def test_down_fences_and_lifts_the_fence(tmp_path, monkeypatch) -> None:
    fake, _ = _fenced(tmp_path, monkeypatch)
    assert main(["down"], system=fake) == 0
    assert fake.drained_at_scan and all(fake.drained_at_scan)
    assert not is_drained(fake.home)


def test_a_refused_stop_lifts_the_fence(tmp_path, monkeypatch) -> None:
    fake, script = _fenced(tmp_path, monkeypatch)
    _add_launch(fake, script)
    assert main(["down", "--wait-timeout", "5"], system=fake) == 1
    assert fake.backend_stops == []
    assert not is_drained(fake.home)


def test_an_operator_drain_is_kept(tmp_path, monkeypatch) -> None:
    fake, _ = _fenced(tmp_path, monkeypatch)
    set_drain(fake.home, "deploy prep")
    assert main(["restart"], system=fake) == 0
    assert read_drain(fake.home)["reason"] == "deploy prep"
    # An existing drain needs no grace scan.
    assert fake.now < 2


def test_a_drain_set_during_the_stop_is_kept(tmp_path, monkeypatch) -> None:
    fake, _ = _fenced(tmp_path, monkeypatch)
    fake.on_first_sleep = lambda: set_drain(fake.home, "operator, meanwhile")
    assert main(["restart"], system=fake) == 0
    assert read_drain(fake.home)["reason"] == "operator, meanwhile"


def test_no_wait_still_fences_without_a_grace(tmp_path, monkeypatch) -> None:
    fake, _ = _fenced(tmp_path, monkeypatch)
    assert main(["down", "--no-wait"], system=fake) == 0
    assert fake.drained_at_scan and all(fake.drained_at_scan)
    assert fake.now < 1


def test_upgrade_is_fenced(tmp_path, monkeypatch) -> None:
    fake, _ = _fenced(tmp_path, monkeypatch)
    assert main(["upgrade", "--ref", "HEAD"], system=fake) == 0
    assert fake.drained_at_scan and all(fake.drained_at_scan)
    assert not is_drained(fake.home)


# --- cond doctor: orphan_processes ---------------------------------------------


def test_doctor_flags_a_young_orphaned_dispatch_launch(tmp_path, monkeypatch, capsys) -> None:
    home = _layout(tmp_path, monkeypatch)
    monkeypatch.delenv("CONDUCTOR_ORPHAN_SECONDS", raising=False)
    wt_root = tmp_path / "worktrees"
    line = wt_root / "line-1"
    line.mkdir(parents=True)
    monkeypatch.setenv("CONDUCTOR_WORKTREE_ROOT", str(wt_root))
    script = _script(home)
    fake = ProcFakeSystem()
    # 24 minutes old, as on 2026-11-10: well under the 6 h threshold.
    fake.add(103, f"/bin/bash {script} minimax task", ppid=1, age=24 * 60, cwd=str(line))
    fake.add(202, "claude -p young", ppid=1, age=24 * 60, cwd=str(line))
    fake.add(204, f"/bin/bash {script} minimax task", ppid=4242, age=60, cwd=str(line))
    found = orphan_processes(fake, load_settings())
    assert [(row["pid"], row["kind"]) for row in found] == [(103, "dispatch launch")]

    main(["doctor", "--json"], system=fake)
    report = json.loads(capsys.readouterr().out)
    check = next(row for row in report["checks"] if row["name"] == "orphan_processes")
    assert check["warn"] is True
    assert [row["pid"] for row in check["processes"]] == [103]
    assert "dispatch launches at any age" in check["detail"]
    assert fake.signals == []
