"""Ops fix round 3: adopted processes, a broken unified log, and a malformed plist.

The adopted processes are real orphans. Their parent exits at once, so init
reaps them and the supervising RealSystem has no Popen handle for them.
"""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from src.conductor_cli import ops_cli, ops_runtime
from src.conductor_cli.doctor import _codexbar_snapshot_check
from src.conductor_cli.ops_cli import _supervise
from src.conductor_cli.ops_runtime import Component, adopted_is_ours, pid_path
from src.conductor_cli.ops_support import NOT_OUR_CHILD, RealSystem, load_settings
from tests.unit.test_cond_ops import _layout
from tests.unit.test_ops_fix_round2 import _ForwardUvFails, _RollbackSyncLeaks


@pytest.fixture(autouse=True)
def isolated_probe_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith("CONDUCTOR_") or any(
            mark in name.upper()
            for mark in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "CREDENTIAL")
        ):
            monkeypatch.delenv(name, raising=False)


def _orphan(argv: list[str], cwd: Path) -> int:
    """Start ``argv`` as a grandchild whose parent exits, and return its pid."""
    launcher = (
        "import subprocess, sys\n"
        "child = subprocess.Popen(sys.argv[1:], start_new_session=True,"
        " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print(child.pid)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", launcher, *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return int(out.stdout.strip())


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _stop(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not _gone(pid):
        time.sleep(0.05)


def _sleeper(seconds: float) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _lstart(pid: int) -> str:
    out = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True, check=False
    )
    return " ".join(out.stdout.split())


def _record_pid(settings, pid: int, started: str = "") -> None:
    """The pid file as a launcher writes it: the pid, then its start time."""
    path = pid_path(settings, Component("backend"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n{started}\n" if started else f"{pid}\n", encoding="utf-8")


def _adopt_backend(settings, pid: int, started: str | None = None) -> None:
    _record_pid(settings, pid, _lstart(pid) if started is None else started)


def test_a_started_process_records_its_start_time(tmp_path, monkeypatch):
    _layout(tmp_path, monkeypatch)
    settings = load_settings()
    system = RealSystem()
    component = Component("daemon", "codex", "daemon-1")
    pid = ops_runtime._start_process(
        system,
        settings,
        component,
        _sleeper(5),
        settings.repo,
        {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        "time.sleep",
    )
    try:
        path = pid_path(settings, component)
        assert ops_runtime._read_pid(path) == pid
        assert ops_runtime._read_pid_start(path) == _lstart(pid)
        assert ops_runtime._read_pid_start(path)
    finally:
        child = system._children[pid]
        child.terminate()
        child.wait(timeout=5)


def test_supervisor_keeps_an_adopted_backend_until_it_exits(tmp_path, monkeypatch):
    _layout(tmp_path, monkeypatch)
    settings = load_settings()
    pid = _orphan(_sleeper(1.5), tmp_path)
    try:
        _adopt_backend(settings, pid)
        supervisor = RealSystem()
        assert supervisor.child_poll(pid) is NOT_OUR_CHILD
        started = time.monotonic()
        assert _supervise(supervisor, settings, {"backend": pid}) == 0
        elapsed = time.monotonic() - started
    finally:
        _stop(pid)
    assert elapsed >= 1.0
    assert elapsed < 8
    assert _gone(pid)


def test_supervisor_leaves_on_a_generation_change(tmp_path, monkeypatch):
    _layout(tmp_path, monkeypatch)
    settings = load_settings()
    pid = _orphan(_sleeper(30), tmp_path)
    try:
        _adopt_backend(settings, pid)
        bump = threading.Timer(0.5, ops_runtime._bump_generation, args=(settings,))
        bump.start()
        started = time.monotonic()
        assert _supervise(RealSystem(), settings, {"backend": pid}) == 0
        elapsed = time.monotonic() - started
        bump.join()
        assert not _gone(pid)
    finally:
        _stop(pid)
    assert 0.4 <= elapsed < 3


def test_supervisor_drops_an_adopted_pid_it_cannot_prove(tmp_path, monkeypatch):
    _layout(tmp_path, monkeypatch)
    settings = load_settings()
    pid = _orphan(_sleeper(30), tmp_path)
    try:
        _adopt_backend(settings, pid, started="Mon Jan  1 00:00:00 2001")
        started = time.monotonic()
        assert _supervise(RealSystem(), settings, {"backend": pid}) == 0
        assert time.monotonic() - started < 2
        _record_pid(settings, pid + 1)
        started = time.monotonic()
        assert _supervise(RealSystem(), settings, {"backend": pid}) == 0
        assert time.monotonic() - started < 2
        assert not _gone(pid)
    finally:
        _stop(pid)


def test_supervisor_checks_a_legacy_pid_file_by_command_and_cwd(tmp_path, monkeypatch):
    _layout(tmp_path, monkeypatch)
    settings = load_settings()
    (settings.repo / "server.py").write_text("import time\ntime.sleep(1.5)\n", encoding="utf-8")
    pid = _orphan([sys.executable, str(settings.repo / "server.py")], settings.repo)
    try:
        _record_pid(settings, pid)
        started = time.monotonic()
        assert _supervise(RealSystem(), settings, {"backend": pid}) == 0
        elapsed = time.monotonic() - started
    finally:
        _stop(pid)
    assert 1.0 <= elapsed < 8


class _EmptyStart(RealSystem):
    def start_time(self, pid: int) -> str:
        return ""


class _StartRaises(RealSystem):
    def start_time(self, pid: int) -> str:
        raise OSError("ps missing")


def test_unknown_start_time_does_not_count_as_ours(tmp_path, monkeypatch):
    _layout(tmp_path, monkeypatch)
    settings = load_settings()
    pid = _orphan(_sleeper(30), tmp_path)
    try:
        _adopt_backend(settings, pid)
        assert adopted_is_ours(_EmptyStart(), settings, "backend", pid, check_start=True) is False
        assert adopted_is_ours(_StartRaises(), settings, "backend", pid, check_start=True) is False
        started = time.monotonic()
        assert _supervise(_EmptyStart(), settings, {"backend": pid}) == 0
        assert time.monotonic() - started < 2
        assert not _gone(pid)
    finally:
        _stop(pid)


def _rollback_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("rollback ")]


_EXPECTED_STEPS = [
    "rollback stop start",
    "rollback stop done",
    "rollback checkout start",
    "rollback checkout done",
    "rollback sync start",
    "rollback sync done",
    "rollback rebuild start",
    "rollback rebuild done",
    "rollback restore-db skipped",
    "rollback runtime start",
    "rollback ready done code=200",
    "rollback runtime done",
]


def _after_checkout(fake) -> list[list[str]]:
    commands = [list(argv) for argv in fake.commands]
    back = commands.index(["git", "checkout", "--detach", "abc1234def"])
    return commands[back + 1 :]


def _assert_rolled_back(fake, err: str) -> None:
    assert _rollback_lines(err) == _EXPECTED_STEPS
    later = _after_checkout(fake)
    order = [
        next(i for i, argv in enumerate(later) if argv[:2] == ["uv", "sync"]),
        next(i for i, argv in enumerate(later) if argv[:2] == ["pnpm", "install"]),
        next(i for i, argv in enumerate(later) if argv[:2] == ["pnpm", "build"]),
        next(i for i, argv in enumerate(later) if any("server.py" in part for part in argv)),
    ]
    assert order == sorted(order)
    assert "forward sync failed" in err
    assert "rollback:" in err


def test_full_disk_log_does_not_block_rollback(tmp_path, monkeypatch, capsys):
    _layout(tmp_path, monkeypatch)
    real_open = Path.open

    def full_disk(self, *args, **kwargs):
        if self.name == "cond.log":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", full_disk)
    fake = _ForwardUvFails()
    assert ops_cli.main(["upgrade", "--ref", "v9"], system=fake) == 1
    err = capsys.readouterr().err
    _assert_rolled_back(fake, err)
    assert err.count("unified log unavailable (OSError)") == 2
    assert fake.head == "abc1234def"


def test_unreadable_log_directory_does_not_block_rollback(tmp_path, monkeypatch, capsys):
    _layout(tmp_path, monkeypatch)
    settings = load_settings()
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(settings.log_dir, 0)
    try:
        fake = _ForwardUvFails()
        assert ops_cli.main(["upgrade", "--ref", "v9"], system=fake) == 1
    finally:
        os.chmod(settings.log_dir, 0o700)
    err = capsys.readouterr().err
    _assert_rolled_back(fake, err)
    assert "unified log unavailable (PermissionError)" in err
    assert not (settings.log_dir / "cond.log").exists()


def test_broken_log_keeps_redaction_and_the_first_failure(tmp_path, monkeypatch, capsys):
    _layout(tmp_path, monkeypatch)
    real_open = Path.open

    def full_disk(self, *args, **kwargs):
        if self.name == "cond.log":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", full_disk)
    leaked = _RollbackSyncLeaks()
    assert ops_cli.main(["upgrade", "--ref", "v9"], system=leaked) == 1
    err = capsys.readouterr().err
    assert "db-secret" not in err
    assert "***" in err
    assert "rollback sync failed" in err
    assert "rollback rebuild start" not in err
    assert "forward sync failed" in err


_TRUNCATED_PLIST = b'<?xml version="1.0"?><plist version="1.0"><dict>'


def _policy_files(home: Path) -> None:
    for relative in (
        ".config/conductor/daemon_policy.yaml",
        ".config/conductor/daemon_trusted_keys.json",
    ):
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
        os.chmod(path, 0o600)


def test_truncated_codexbar_plist_is_a_structured_unreadable_check(
    tmp_path, monkeypatch, capsys
):
    from tests.unit.test_cond_ops import FakeSystem

    home = _layout(tmp_path, monkeypatch, web=5170)
    monkeypatch.setenv("CONDUCTOR_WEB_PORT", "5170")
    _policy_files(home)
    settings = load_settings()
    settings.launchd_dir.mkdir(parents=True, exist_ok=True)
    plist = settings.launchd_dir / "com.conductor.codexbar-snapshot.plist"
    plist.write_bytes(_TRUNCATED_PLIST)
    check = _codexbar_snapshot_check(settings)
    assert check["ok"] is False
    assert check["detail"] == "unreadable"
    fake = FakeSystem()
    fake.open_ports.add(5170)
    assert ops_cli.main(["doctor"], system=fake) == 1
    text = capsys.readouterr().out
    assert "FAIL: codexbar_snapshot (unreadable)" in text
    assert "web_port" in text
    assert "daemon_policy" in text
    assert "doctor: failed" in text
    assert ops_cli.main(["doctor", "--json"], system=fake) == 1
    report = json.loads(capsys.readouterr().out)
    names = {item["name"] for item in report["checks"]}
    assert {"web_port", "daemon_policy", "trusted_keys", "codexbar_snapshot"} <= names
