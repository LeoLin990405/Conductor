"""cond down/restart settle dispatch launches first; cond doctor reports orphans.

Fakes only: the process table and group signals are a fake system. Nothing
here lists, signals, or stops a real process, and nothing contacts port 8000.
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest

from src.conductor_cli.ops_cli import main
from src.conductor_cli.ops_dispatch import (
    dispatch_config,
    launch_scripts,
    orphan_processes,
    parse_etime,
    parse_ps,
    scan_launches,
)
from src.conductor_cli.ops_support import load_settings
from tests.unit.test_cond_ops import FakeSystem, _assert_no_port_8000, _layout

OWN_GROUP = os.getpgrp()


class ProcFakeSystem(FakeSystem):
    """FakeSystem plus a ps table, group signals, and a cwd per listed pid."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[int, dict[str, object]] = {}
        self.signals: list[tuple[int, int]] = []
        self.stubborn: set[int] = set()
        self.backend_stops: list[tuple[float, list[int]]] = []

    def add(
        self,
        pid: int,
        command: str,
        *,
        ppid: int = 500,
        pgid: int | None = None,
        age: int = 60,
        cwd: str = "/",
        ends_at: float | None = None,
    ) -> None:
        self.rows[pid] = {
            "ppid": ppid,
            "pgid": pid if pgid is None else pgid,
            "age": age,
            "command": command,
            "cwd": cwd,
            "ends_at": ends_at,
        }

    def _expire(self) -> None:
        for pid in [
            pid
            for pid, row in self.rows.items()
            if row["ends_at"] is not None and self.now >= float(row["ends_at"])
        ]:
            del self.rows[pid]

    def processes(self) -> str:
        self._expire()
        lines = []
        for pid, row in self.rows.items():
            age = int(row["age"]) + int(self.now)
            days, rest = divmod(age, 86400)
            hours, rest = divmod(rest, 3600)
            minutes, seconds = divmod(rest, 60)
            etime = f"{days}-{hours:02d}:{minutes:02d}:{seconds:02d}"
            lines.append(f"{pid} {row['ppid']} {row['pgid']} {etime} {row['command']}")
        return "\n".join(lines) + "\n"

    def cwd(self, pid: int) -> str:
        if pid in self.rows:
            return str(self.rows[pid]["cwd"])
        return super().cwd(pid)

    def signal_group(self, pgid: int, sig: int) -> None:
        assert pgid > 1 and pgid != OWN_GROUP
        self.signals.append((pgid, sig))
        members = [pid for pid, row in self.rows.items() if row["pgid"] == pgid]
        if not members:
            raise ProcessLookupError(pgid)
        if sig == signal.SIGTERM and pgid in self.stubborn:
            return
        for pid in members:
            del self.rows[pid]

    def terminate(self, pid: int) -> None:
        if "server.py" in self.command(pid):
            self._expire()
            launches = [p for p, row in self.rows.items() if "dispatch_launch" in str(row["command"])]
            self.backend_stops.append((self.now, launches))
        super().terminate(pid)


def _script(home: Path) -> Path:
    repo = Path(os.environ["CONDUCTOR_REPO_ROOT"])
    path = repo / "scripts" / "dispatch_launch.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n", encoding="utf-8")
    return path


def _running(tmp_path: Path, monkeypatch) -> tuple[ProcFakeSystem, Path]:
    home = _layout(tmp_path, monkeypatch)
    monkeypatch.delenv("CONDUCTOR_DISPATCH_LAUNCH_SCRIPT", raising=False)
    monkeypatch.delenv("CONDUCTOR_DISPATCH_REPO", raising=False)
    monkeypatch.delenv("CONDUCTOR_DISPATCH_WAIT_SECONDS", raising=False)
    fake = ProcFakeSystem()
    assert main(["up"], system=fake) == 0
    return fake, _script(home)


def _backend(fake: ProcFakeSystem) -> int:
    settings = load_settings()
    from src.conductor_cli.ops_runtime import Component, _read_pid, pid_path

    pid = _read_pid(pid_path(settings, Component("backend")))
    assert pid is not None and pid in fake.alive_pids
    return pid


def _add_launch(fake: ProcFakeSystem, script: Path, pid: int = 7000, **kw) -> None:
    kw.setdefault("ppid", _backend(fake))
    fake.add(pid, f"/bin/bash {script} kimi do-the-line-task --skills=x", **kw)
    # The Worker CLI is in the launch's group.
    fake.add(
        pid + 1, "claude -p secret-prompt-text", ppid=pid, pgid=pid, ends_at=kw.get("ends_at")
    )


def _append_env(lines: str) -> Path:
    path = Path(os.environ["CONDUCTOR_REPO_ROOT"]) / ".env"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(lines)
    return path


def test_restart_waits_for_running_dispatch_launch_before_stopping_backend(
    tmp_path, monkeypatch, capsys
) -> None:
    fake, script = _running(tmp_path, monkeypatch)
    _add_launch(fake, script, ends_at=fake.now + 5)
    capsys.readouterr()
    assert main(["restart"], system=fake) == 0
    assert fake.backend_stops, "the backend was restarted"
    when, still_running = fake.backend_stops[0]
    assert still_running == [], "backend stopped while a dispatch launch still ran"
    assert when >= 5
    assert fake.signals == []
    err = capsys.readouterr().err
    assert "waiting up to 600s" in err
    assert "pid=7000" in err
    assert "secret-prompt-text" not in err
    _assert_no_port_8000(fake)


def test_down_refuses_after_wait_timeout_and_keeps_backend(
    tmp_path, monkeypatch, capsys
) -> None:
    fake, script = _running(tmp_path, monkeypatch)
    _add_launch(fake, script)
    capsys.readouterr()
    assert main(["down", "--wait-timeout", "30"], system=fake) == 1
    assert fake.backend_stops == []
    assert any("server.py" in fake.command(pid) for pid in fake.alive_pids)
    assert fake.signals == []
    err = capsys.readouterr().err
    assert "7000" in err and "--force" in err and "--no-wait" in err
    assert fake.now >= 30


def test_wait_timeout_comes_from_the_environment(tmp_path, monkeypatch, capsys) -> None:
    fake, script = _running(tmp_path, monkeypatch)
    monkeypatch.setenv("CONDUCTOR_DISPATCH_WAIT_SECONDS", "12")
    _add_launch(fake, script)
    capsys.readouterr()
    assert main(["stop"], system=fake) == 1
    assert "waiting up to 12s" in capsys.readouterr().err
    assert 12 <= fake.now < 30


def test_force_stops_only_this_deployments_launch_groups(
    tmp_path, monkeypatch, capsys
) -> None:
    fake, script = _running(tmp_path, monkeypatch)
    _add_launch(fake, script)
    fake.stubborn.add(7000)
    # Not ours: another checkout's launcher, a non-leader running our script,
    # a launch in cond's own group, and an unrelated process.
    other = tmp_path / "other" / "scripts" / "dispatch_launch.sh"
    fake.add(7100, f"/bin/bash {other} kimi task")
    fake.add(7200, f"/bin/bash {script} kimi task", pgid=7201)
    fake.add(7201, "sleep 100")
    if OWN_GROUP > 1:
        fake.add(OWN_GROUP, f"/bin/bash {script} kimi task", pgid=OWN_GROUP)
    fake.add(7300, "vim notes.txt")
    capsys.readouterr()
    assert main(["down", "--force"], system=fake) == 0
    assert fake.signals == [(7000, signal.SIGTERM), (7000, signal.SIGKILL)]
    assert 7000 not in fake.rows and 7001 not in fake.rows
    for pid in (7100, 7200, 7201, 7300):
        assert pid in fake.rows
    assert fake.backend_stops and 7000 not in fake.backend_stops[0][1]
    assert not fake.alive_pids
    err = capsys.readouterr().err
    assert "TERM" in err and "KILL" in err


def test_force_term_is_enough_when_the_group_exits(tmp_path, monkeypatch) -> None:
    fake, script = _running(tmp_path, monkeypatch)
    _add_launch(fake, script)
    assert main(["restart", "--force"], system=fake) == 0
    assert fake.signals == [(7000, signal.SIGTERM)]


def test_no_wait_warns_and_keeps_todays_behaviour(tmp_path, monkeypatch, capsys) -> None:
    fake, script = _running(tmp_path, monkeypatch)
    _add_launch(fake, script)
    capsys.readouterr()
    assert main(["down", "--no-wait"], system=fake) == 0
    assert fake.signals == []
    assert fake.backend_stops and fake.backend_stops[0][1] == [7000]
    assert fake.now < 1
    err = capsys.readouterr().err
    assert "warning" in err and "pid=7000" in err and "orphans" in err


def _upgrade_started(fake: ProcFakeSystem) -> bool:
    return any(Path(argv[0]).name in {"pg_dump", "git"} for argv in fake.commands)


def test_upgrade_waits_for_dispatch_launch_before_restarting(
    tmp_path, monkeypatch, capsys
) -> None:
    fake, script = _running(tmp_path, monkeypatch)
    _add_launch(fake, script, ends_at=fake.now + 5)
    capsys.readouterr()
    assert main(["upgrade", "--ref", "HEAD"], system=fake) == 0
    assert fake.backend_stops, "the upgrade restarted the backend"
    assert fake.backend_stops[0][1] == []
    assert fake.backend_stops[0][0] >= 5
    assert fake.signals == []
    assert "waiting up to 600s" in capsys.readouterr().err


def test_upgrade_refuses_before_backup_when_launch_keeps_running(
    tmp_path, monkeypatch, capsys
) -> None:
    fake, script = _running(tmp_path, monkeypatch)
    _add_launch(fake, script)
    fake.commands.clear()
    capsys.readouterr()
    assert main(["upgrade", "--ref", "HEAD", "--wait-timeout", "20"], system=fake) == 1
    assert fake.backend_stops == []
    assert not _upgrade_started(fake)
    assert fake.signals == []
    assert "--force" in capsys.readouterr().err


def test_upgrade_force_and_no_wait(tmp_path, monkeypatch, capsys) -> None:
    fake, script = _running(tmp_path, monkeypatch)
    _add_launch(fake, script)
    assert main(["upgrade", "--ref", "HEAD", "--force"], system=fake) == 0
    assert fake.signals == [(7000, signal.SIGTERM)]
    assert fake.backend_stops and 7000 not in fake.backend_stops[0][1]
    _add_launch(fake, script, pid=8000)  # parent: the upgraded backend
    capsys.readouterr()
    assert main(["upgrade", "--ref", "HEAD", "--no-wait"], system=fake) == 0
    assert fake.signals == [(7000, signal.SIGTERM)]
    assert "warning" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["upgrade", "--force", "--no-wait"], system=fake)


def test_force_and_no_wait_are_exclusive(tmp_path, monkeypatch) -> None:
    fake, _script_path = _running(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        main(["down", "--force", "--no-wait"], system=fake)
    assert exc.value.code == 2


def test_launch_paths_come_from_the_checkout_env(tmp_path, monkeypatch) -> None:
    _layout(tmp_path, monkeypatch)
    custom = tmp_path / "custom launch.sh"
    dispatch_repo = tmp_path / "dispatch-repo"
    _append_env(
        f"CONDUCTOR_DISPATCH_LAUNCH_SCRIPT={custom}\nCONDUCTOR_DISPATCH_REPO={dispatch_repo}\n"
    )
    monkeypatch.delenv("CONDUCTOR_DISPATCH_LAUNCH_SCRIPT", raising=False)
    monkeypatch.delenv("CONDUCTOR_DISPATCH_REPO", raising=False)
    settings = load_settings()
    scripts = launch_scripts(settings)
    assert str(custom) in scripts
    assert str(dispatch_repo / "scripts" / "dispatch_launch.sh") in scripts
    rows = parse_ps(
        f"10 1 10 01:00 {custom} kimi task\n"
        f"20 1 20 01:00 /bin/bash {dispatch_repo}/scripts/dispatch_launch.sh glm t\n"
        f"30 1 30 01:00 /bin/bash {custom}.bak kimi task\n"
    )
    scan = scan_launches(ProcFakeSystem(), settings, rows)
    # Parent init, cwd unknown: launchers, but not tied to this deployment.
    assert scan.ours == []
    assert [launch.pid for launch in scan.foreign] == [10, 20]


def test_shell_value_differing_from_env_warns_and_env_wins(
    tmp_path, monkeypatch, capsys
) -> None:
    fake, _script_path = _running(tmp_path, monkeypatch)
    real = tmp_path / "env-launch.sh"
    other = tmp_path / "shell-launch.sh"
    # load_dotenv follows a symlinked .env; so does cond.
    env_file = Path(os.environ["CONDUCTOR_REPO_ROOT"]) / ".env"
    target = tmp_path / "shared.env"
    target.write_text(
        env_file.read_text(encoding="utf-8") + f"CONDUCTOR_DISPATCH_LAUNCH_SCRIPT={real}\n",
        encoding="utf-8",
    )
    env_file.unlink()
    env_file.symlink_to(target)
    monkeypatch.setenv("CONDUCTOR_DISPATCH_LAUNCH_SCRIPT", str(other))
    backend = _backend(fake)
    fake.add(7000, f"/bin/bash {real} kimi task", ppid=backend, ends_at=fake.now + 4)
    fake.add(7100, f"/bin/bash {other} kimi task", ppid=backend)
    capsys.readouterr()
    assert main(["down"], system=fake) == 0
    err = capsys.readouterr().err
    assert "CONDUCTOR_DISPATCH_LAUNCH_SCRIPT in this shell" in err
    assert "from the checkout .env" in err
    assert "pid=7000" in err and "pid=7100" not in err
    assert fake.backend_stops and fake.backend_stops[0][0] >= 4
    assert 7100 in fake.rows and fake.signals == []


def test_live_dispatch_without_launch_paths_warns(tmp_path, monkeypatch, capsys) -> None:
    fake, _script_path = _running(tmp_path, monkeypatch)
    _append_env("CONDUCTOR_DISPATCH_ADAPTERS=live\n")
    capsys.readouterr()
    assert main(["down"], system=fake) == 0
    assert "dispatch launches cannot be detected" in capsys.readouterr().err
    config = dispatch_config(load_settings(), shell={})
    assert config.live and config.launch_script is None
    _append_env(f"CONDUCTOR_DISPATCH_LAUNCH_SCRIPT={tmp_path / 'l.sh'}\n")
    assert dispatch_config(load_settings(), shell={}).warnings == ()


def test_only_launches_tied_to_this_deployment_are_waited_or_signalled(
    tmp_path, monkeypatch, capsys
) -> None:
    fake, script = _running(tmp_path, monkeypatch)
    wt_root = tmp_path / "worktrees"
    line = wt_root / "line-1"
    line.mkdir(parents=True)
    monkeypatch.setenv("CONDUCTOR_WORKTREE_ROOT", str(wt_root))
    # Our script, but another parent: not ours.
    fake.add(7000, f"/bin/bash {script} kimi task", ppid=500)
    # Left behind by a previous backend in our worktree root: ours.
    fake.add(7100, f"/bin/bash {script} kimi task", ppid=1, cwd=str(line))
    # Parent init, cwd elsewhere: not ours.
    fake.add(7200, f"/bin/bash {script} kimi task", ppid=1, cwd=str(tmp_path))
    capsys.readouterr()
    assert main(["down", "--force"], system=fake) == 0
    assert fake.signals == [(7100, signal.SIGTERM)]
    assert 7000 in fake.rows and 7200 in fake.rows
    err = capsys.readouterr().err
    assert "not ours (left alone): pid=7000" in err
    assert "not ours (left alone): pid=7200" in err


def test_foreign_launch_is_not_waited_for(tmp_path, monkeypatch, capsys) -> None:
    fake, script = _running(tmp_path, monkeypatch)
    fake.add(7000, f"/bin/bash {script} kimi task", ppid=500)
    assert main(["down", "--wait-timeout", "30"], system=fake) == 0
    assert fake.now < 30 and fake.signals == []


def test_kill_rechecks_that_the_leader_is_still_a_launch(
    tmp_path, monkeypatch, capsys
) -> None:
    fake, script = _running(tmp_path, monkeypatch)
    _add_launch(fake, script)
    fake.stubborn.add(7000)
    original = fake.signal_group

    def leader_exits_on_term(pgid: int, sig: int) -> None:
        original(pgid, sig)
        if sig == signal.SIGTERM:
            # The leader exits; a member of its group keeps running.
            fake.rows.pop(7000, None)

    fake.signal_group = leader_exits_on_term
    capsys.readouterr()
    assert main(["down", "--force"], system=fake) == 0
    assert fake.signals == [(7000, signal.SIGTERM)]
    assert 7001 in fake.rows
    assert "not killed" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["inf", "-inf", "nan", "-1", "soon"])
@pytest.mark.parametrize("verb", ["down", "stop", "restart", "upgrade"])
def test_wait_timeout_must_be_finite(tmp_path, monkeypatch, capsys, verb, value) -> None:
    fake, _script_path = _running(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        main([verb, f"--wait-timeout={value}"], system=fake)
    assert exc.value.code == 2
    assert fake.backend_stops == []


def test_parse_etime() -> None:
    assert parse_etime("05") == 5
    assert parse_etime("01:05") == 65
    assert parse_etime("02:01:05") == 7265
    assert parse_etime("7-00:00:01") == 7 * 86400 + 1
    assert parse_etime("bogus") == -1


def test_doctor_warns_about_orphaned_workers_and_signals_nothing(
    tmp_path, monkeypatch, capsys
) -> None:
    home = _layout(tmp_path, monkeypatch)
    monkeypatch.delenv("CONDUCTOR_ORPHAN_SECONDS", raising=False)
    monkeypatch.delenv("CONDUCTOR_DISPATCH_RUNS_DIR", raising=False)
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    monkeypatch.setenv("CONDUCTOR_WORKTREE_ROOT", str(wt_root))
    line = wt_root / "line-1"
    line.mkdir()
    dispatch_wt = home / ".conductor" / "dispatch" / "dispatch-worktrees" / "r" / "d"
    dispatch_wt.mkdir(parents=True)
    script = _script(home)
    week = 7 * 86400
    fake = ProcFakeSystem()
    fake.add(101, "claude -p fix the bug", ppid=1, age=week, cwd=str(line))
    fake.add(102, "/usr/bin/node /opt/bin/codex exec go", ppid=1, age=week, cwd=str(dispatch_wt))
    fake.add(103, f"/bin/bash {script} kimi task", ppid=1, age=week, cwd=str(line))
    fake.add(201, "claude -p elsewhere", ppid=1, age=week, cwd=str(tmp_path))
    fake.add(202, "claude -p young", ppid=1, age=3600, cwd=str(line))
    fake.add(203, "claude", ppid=1, age=week, cwd=str(line))
    fake.add(204, "claude -p has parent", ppid=999, age=week, cwd=str(line))
    settings = load_settings()
    found = orphan_processes(fake, settings)
    assert [row["pid"] for row in found] == [101, 102, 103]
    assert found[0]["kind"] == "claude -p"
    assert found[0]["cwd"] == str(line)
    assert found[0]["age"] == "7d0h"

    assert main(["doctor", "--json"], system=fake) in {0, 1}
    report = json.loads(capsys.readouterr().out)
    check = next(row for row in report["checks"] if row["name"] == "orphan_processes")
    assert check["ok"] is True and check["warn"] is True
    assert check["threshold_seconds"] == 21600
    assert [row["pid"] for row in check["processes"]] == [101, 102, 103]
    main(["doctor"], system=fake)
    text = capsys.readouterr().out
    assert "WARN: orphan_processes" in text
    assert f"pid=101 age=7d0h kind=claude -p cwd={line}" in text
    assert "fix the bug" not in text
    assert fake.signals == []
    assert len(fake.rows) == 7


def test_doctor_orphan_check_is_quiet_without_orphans(tmp_path, monkeypatch, capsys) -> None:
    _layout(tmp_path, monkeypatch)
    fake = ProcFakeSystem()
    main(["doctor", "--json"], system=fake)
    report = json.loads(capsys.readouterr().out)
    check = next(row for row in report["checks"] if row["name"] == "orphan_processes")
    assert check == {
        "name": "orphan_processes",
        "ok": True,
        "warn": False,
        "threshold_seconds": 21600,
        "processes": [],
        "detail": "none",
    }
