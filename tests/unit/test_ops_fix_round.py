"""Codex BLOCK regressions for the ops fix round. Fakes only."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.conductor_cli.ops_cli import cmd_foreground, foreground_spec
from src.conductor_cli.ops_cli_update import update_clis
from src.conductor_cli.ops_runtime import (
    Component,
    pid_path,
    prepare,
    start_backend,
    status_map,
)
from src.conductor_cli.ops_support import Completed, OpsError, load_settings
from src.conductor_cli.ops_upgrade import backup_database, upgrade
from src.server.app.ready import probe_daemon_heartbeats
from tests.unit.test_cond_cli_update import (
    HelpFails,
    _provider_started,
    _rewrite_daemon,
)
from tests.unit.test_cond_ops import FakeSystem, _layout


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if any(
            mark in name.upper()
            for mark in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
        ):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CONDUCTOR_CLI_UPDATE_INTERVAL_HOURS", raising=False)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")


def test_ready_provider_starts_while_other_cli_is_still_updating(
    tmp_path, monkeypatch
) -> None:
    home = _layout(tmp_path, monkeypatch)
    _rewrite_daemon(home, "grok")
    entered, release, grok_finished, grok_started = [
        threading.Event() for _ in range(4)
    ]

    class SlowOther(FakeSystem):
        def run(self, argv, **kw):
            if argv[:2] == ["brew", "upgrade"]:
                entered.set()
                if not release.wait(3):
                    raise OpsError("test barrier timed out")
            result = super().run(argv, **kw)
            if Path(argv[0]).name == "grok" and "--help" in argv:
                grok_finished.set()
            return result

        def popen(self, argv, **kw):
            if "--provider" in argv and argv[argv.index("--provider") + 1] == "grok":
                grok_started.set()
            return super().popen(argv, **kw)

    fake = SlowOther()
    system, settings, fleet = prepare(fake)
    errors: list[str] = []

    def worker() -> None:
        try:
            from src.conductor_cli.ops_runtime import start_all

            start_all(system, settings, fleet)
        except Exception as exc:
            errors.append(repr(exc))

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(2)
        finished_before_release = grok_finished.wait(0.3)
        started_before_release = grok_started.wait(0.3)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert finished_before_release and started_before_release


def test_post_install_missing_binary_keeps_daemon_down(tmp_path, monkeypatch) -> None:
    home = _layout(tmp_path, monkeypatch)
    _rewrite_daemon(home, "claude")

    class MissingAfterInstall(FakeSystem):
        def __init__(self) -> None:
            super().__init__()
            self.updated = False

        def run(self, argv, **kw):
            if argv == ["brew", "upgrade", "--cask", "claude-code"]:
                self.updated = True
                return Completed(0)
            if argv == ["claude", "--version"]:
                if self.updated:
                    raise OpsError("could not run claude: FileNotFoundError")
                return Completed(0, "1.0.0\n")
            return super().run(argv, **kw)

    fake = MissingAfterInstall()
    system, settings, fleet = prepare(fake)
    from src.conductor_cli.ops_runtime import start_all

    start_all(system, settings, fleet)
    assert not _provider_started(fake, "claude")


def test_pin_survives_invalid_global_interval(tmp_path, monkeypatch) -> None:
    home = _layout(tmp_path, monkeypatch)
    (home / ".conductor" / "clis.toml").write_text(
        '[codex]\npin = "1.2.3"\n', encoding="utf-8"
    )
    monkeypatch.setenv("CONDUCTOR_CLI_UPDATE_INTERVAL_HOURS", "not-a-number")
    fake = FakeSystem()
    system, settings, _fleet = prepare(fake)
    rows = update_clis(system, settings, only="codex", force=True)
    assert ["brew", "upgrade", "--cask", "codex"] not in fake.commands, rows


def test_broken_cli_remains_blocked_when_it_is_skipped(tmp_path, monkeypatch) -> None:
    home = _layout(tmp_path, monkeypatch)
    _rewrite_daemon(home, "claude")
    fake = HelpFails({"claude"})
    system, settings, fleet = prepare(fake)
    rows = update_clis(system, settings, only="claude", force=True)
    assert rows[0]["result"] == "broken"
    from src.conductor_cli.ops_cli_update import daemon_is_blocked

    rows = update_clis(system, settings, only="claude", dry_run=True, force=True)
    assert rows[0]["result"] == "dry-run"
    assert daemon_is_blocked("claude", rows, settings)
    (home / ".conductor" / "clis.toml").write_text(
        "[claude]\nskip = true\n", encoding="utf-8"
    )
    from src.conductor_cli.ops_runtime import start_all

    start_all(system, settings, fleet)
    assert not _provider_started(fake, "claude")


def test_concurrent_start_all_serializes(
    tmp_path, monkeypatch
) -> None:
    """Two full start_all calls must not sit in port_open together.

    start_backend itself still must not hold the process lock across the
    probe; that is test_concurrent_start_spawns_one_backend.
    """
    _layout(tmp_path, monkeypatch)
    state = {"inside": 0, "max": 0}
    guard = threading.Lock()

    class Together(FakeSystem):
        def port_open(self, host, port, timeout=0.3):
            with guard:
                state["inside"] += 1
                state["max"] = max(state["max"], state["inside"])
            try:
                time.sleep(0.05)
                return False
            finally:
                with guard:
                    state["inside"] -= 1

    fake = Together()
    system, settings, fleet = prepare(fake)
    monkeypatch.setattr(
        "src.conductor_cli.ops_cli_update.update_clis",
        lambda *args, **kwargs: [],
    )
    errors: list[str] = []

    def start() -> None:
        try:
            from src.conductor_cli.ops_runtime import start_all

            start_all(system, settings, fleet)
        except Exception as exc:
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not any(thread.is_alive() for thread in threads), errors
    assert state["max"] == 1, state
    spawned = [
        argv
        for argv in fake.commands
        if any(str(part).endswith("/server.py") for part in argv)
    ]
    assert len(spawned) == 1, {"spawned": len(spawned), "errors": errors}


def test_concurrent_start_spawns_one_backend(tmp_path, monkeypatch) -> None:
    _layout(tmp_path, monkeypatch)
    barrier = threading.Barrier(2)

    class Together(FakeSystem):
        def port_open(self, host, port, timeout=0.3):
            barrier.wait(timeout=3)
            return False

    fake = Together()
    system, settings, _fleet = prepare(fake)
    errors: list[str] = []

    def start() -> None:
        try:
            start_backend(system, settings)
        except Exception as exc:
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    spawned = [
        argv
        for argv in fake.commands
        if any(str(part).endswith("/server.py") for part in argv)
    ]
    assert len(spawned) == 1, {"spawned": len(spawned), "errors": errors}


def test_failed_backup_preserves_previous_valid_copy(tmp_path, monkeypatch) -> None:
    _layout(tmp_path, monkeypatch)
    system, settings, _fleet = prepare(FakeSystem())
    settings.backup_keep = 1
    settings.backup_dir.mkdir(parents=True)
    old = settings.backup_dir / "conductor-20000101T000000.dump"
    old.write_bytes(b"known-good-backup")
    os.utime(old, (1, 1))
    system.fail.add("pg_dump")
    with pytest.raises(OpsError):
        backup_database(system, settings)
    assert old.exists()

    system.fail.discard("pg_dump")
    system.fail.add("pg_restore")
    with pytest.raises(OpsError):
        backup_database(system, settings)
    assert old.exists()
    leftovers = [
        path for path in settings.backup_dir.glob("conductor-*.dump") if path != old
    ]
    assert leftovers == []


class Headed(FakeSystem):
    def __init__(self) -> None:
        super().__init__()
        self.ready_calls = 0

    def popen(self, argv, **kw):
        pid = super().popen(argv, **kw)
        self.procs[pid]["git_head"] = self.head
        return pid

    def http_get(self, url, timeout):
        if url.endswith("/ready"):
            self.ready_calls += 1
            self.urls.append(url)
            if self.ready_calls == 1:
                return 503, '{"status":"not_ready"}'
            return 200, '{"status":"ready"}'
        return super().http_get(url, timeout)


def test_upgrade_ready_failure_restarts_the_previous_runtime(
    tmp_path, monkeypatch
) -> None:
    _layout(tmp_path, monkeypatch)
    system = Headed()
    settings = load_settings()
    from src.conductor_cli.ops_fleet import load_fleet

    fleet = load_fleet(settings)
    with pytest.raises(OpsError, match="ready check failed"):
        upgrade(system, settings, fleet, ref="v9")
    assert system.head == "abc1234def"
    alive = [system.procs[pid] for pid in system.alive_pids]
    assert alive
    assert all(proc["git_head"] == "abc1234def" for proc in alive)
    assert not any(argv[:2] == ["pg_restore", "--clean"] for argv in system.commands)
    assert system.ready_calls >= 2


def test_upgrade_partial_restart_does_not_leave_the_failed_runtime(
    tmp_path, monkeypatch
) -> None:
    _layout(tmp_path, monkeypatch)
    system = Headed()
    settings = load_settings()
    from src.conductor_cli.ops_fleet import load_fleet
    import src.conductor_cli.ops_runtime as runtime

    fleet = load_fleet(settings)
    calls = {"web": 0}
    real = runtime.start_web

    def wrapped(system, settings):
        calls["web"] += 1
        if calls["web"] > 1:
            raise OpsError("web did not restart")
        return real(system, settings)

    monkeypatch.setattr(runtime, "start_web", wrapped)
    with pytest.raises(OpsError, match="web did not restart"):
        upgrade(system, settings, fleet, ref="v9")
    assert system.head == "abc1234def"
    v9_alive = [
        pid for pid in system.alive_pids if system.procs[pid].get("git_head") == "v9"
    ]
    assert v9_alive == []
    assert not any(argv[:2] == ["pg_restore", "--clean"] for argv in system.commands)


def test_launchd_foreground_loads_keychain_and_is_visible(
    tmp_path, monkeypatch
) -> None:
    _layout(tmp_path, monkeypatch)
    settings = load_settings()
    settings.daemons_file.write_text(
        """
[backend]
port = 54321
[web]
port = 54322
mode = "preview"
[[daemon]]
provider = "cursor"
id = "cursor-1"
keychain = { CURSOR_API_KEY = "svc:acct" }
""".strip()
        + "\n",
        encoding="utf-8",
    )
    token = settings.token_dir / "cursor-1.token"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("fake-daemon-token\n", encoding="utf-8")
    os.chmod(token, 0o600)
    monkeypatch.setattr(
        "src.conductor_cli.ops_runtime._keychain_lookup",
        lambda _system, _settings: lambda service, account: "fake-keychain-value",
    )
    _argv, env, _cwd = foreground_spec(settings, "daemon", "cursor", "cursor-1")
    assert env["CURSOR_API_KEY"] == "fake-keychain-value"
    assert env["CONDUCTOR_WORKTREE_ROOT"] == str(settings.worktree_root)
    backend = pid_path(settings, Component("backend"))
    backend.parent.mkdir(parents=True, exist_ok=True)
    runtime = FakeSystem()
    monkeypatch.setattr("src.conductor_cli.ops_cli.RealSystem", lambda: runtime)
    backend_pid = runtime.popen(
        [str(settings.python), str(settings.repo / "server.py")],
        cwd=settings.repo,
        env={"PATH": "/usr/bin"},
        log_path=settings.log_dir / "backend.log",
        max_bytes=1024,
        backups=1,
    )
    backend.write_text(f"{backend_pid}\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def _exec(file, argv, env):
        captured["argv"] = list(argv)
        captured["env"] = dict(env)

    monkeypatch.setattr(os, "execvpe", _exec)
    assert cmd_foreground(settings, "daemon", "cursor", "cursor-1") == 1
    pid_file = pid_path(settings, Component("daemon", "cursor", "cursor-1"))
    assert pid_file.is_file()
    assert (settings.heartbeat_dir / "cursor-1.json").is_file()
    assert settings.registry_file.is_file()
    assert captured["env"]["CURSOR_API_KEY"] == "fake-keychain-value"  # type: ignore[index]
    fake = FakeSystem()
    pid = int(pid_file.read_text(encoding="utf-8"))
    fake.procs[pid] = {"argv": captured["argv"], "cwd": str(settings.repo)}
    fake.alive_pids.add(pid)
    from src.conductor_cli.ops_fleet import load_fleet

    report = status_map(fake, settings, load_fleet(settings))
    assert report["daemons"][0]["running"] is True


def test_launchd_foreground_refuses_a_broken_cli_and_a_missing_backend(
    tmp_path, monkeypatch
) -> None:
    _layout(tmp_path, monkeypatch)
    settings = load_settings()
    settings.daemons_file.write_text(
        """
[backend]
port = 54321
[web]
port = 54322
mode = "preview"
[[daemon]]
provider = "cursor"
id = "cursor-1"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    token = settings.token_dir / "cursor-1.token"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("fake-daemon-token\n", encoding="utf-8")
    os.chmod(token, 0o600)
    with pytest.raises(OpsError, match="backend is not running"):
        cmd_foreground(settings, "daemon", "cursor", "cursor-1")
    runtime = HelpFails({"cursor-agent"})
    monkeypatch.setattr("src.conductor_cli.ops_cli.RealSystem", lambda: runtime)
    backend = pid_path(settings, Component("backend"))
    backend.parent.mkdir(parents=True, exist_ok=True)
    backend_pid = runtime.popen(
        [str(settings.python), str(settings.repo / "server.py")],
        cwd=settings.repo,
        env={"PATH": "/usr/bin"},
        log_path=settings.log_dir / "backend.log",
        max_bytes=1024,
        backups=1,
    )
    backend.write_text(f"{backend_pid}\n", encoding="utf-8")
    monkeypatch.setattr(os, "execvpe", lambda *_args: None)
    with pytest.raises(OpsError, match="broken"):
        cmd_foreground(settings, "daemon", "cursor", "cursor-1")


def test_configured_worktree_root_reaches_daemon(tmp_path, monkeypatch) -> None:
    _layout(tmp_path, monkeypatch)
    wanted = str(tmp_path / "operator-worktrees")
    monkeypatch.setenv("CONDUCTOR_WORKTREE_ROOT", wanted)
    fake = FakeSystem()
    system, settings, fleet = prepare(fake)
    assert str(settings.worktree_root) == wanted
    from src.conductor_cli.ops_runtime import start_all

    start_all(system, settings, fleet)
    matched = [env for env in fake.popen_env if env.get("CONDUCTOR_DAEMON_TOKEN")]
    assert matched
    assert all(env.get("CONDUCTOR_WORKTREE_ROOT") == wanted for env in matched)


def test_ready_requires_heartbeats_for_configured_daemons(
    tmp_path, monkeypatch
) -> None:
    _layout(tmp_path, monkeypatch)
    settings = load_settings()
    settings.daemons_file.write_text(
        """
[backend]
port = 54321
[web]
port = 54322
mode = "preview"
[[daemon]]
provider = "codex"
id = "expected-daemon"
[[daemon]]
provider = "cursor"
id = "retired-daemon"
retired = true
""".strip()
        + "\n",
        encoding="utf-8",
    )
    settings.heartbeat_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CONDUCTOR_HEARTBEAT_TTL", "120")
    missing = probe_daemon_heartbeats()
    assert missing["ok"] is False
    assert "expected-daemon" in missing["missing"]
    assert "retired-daemon" not in missing.get("missing", [])

    fresh = settings.heartbeat_dir / "expected-daemon.json"
    fresh.write_text(
        json.dumps({"id": "expected-daemon", "at": datetime.now(UTC).isoformat()}),
        encoding="utf-8",
    )
    assert probe_daemon_heartbeats()["ok"] is True

    stale = datetime.now(UTC) - timedelta(seconds=500)
    fresh.write_text(
        json.dumps({"id": "expected-daemon", "at": stale.isoformat()}), encoding="utf-8"
    )
    aged = probe_daemon_heartbeats()
    assert aged["ok"] is False
    assert "expected-daemon" in aged["stale"]

    fresh.write_text("{", encoding="utf-8")
    bad = probe_daemon_heartbeats()
    assert bad["ok"] is False
    assert "expected-daemon" in bad["unreadable"]
