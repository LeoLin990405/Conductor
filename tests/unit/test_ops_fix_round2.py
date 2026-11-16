"""Ported Codex probes for upgrade rollback, lifecycle, foreground, health, and ready.

Assertions match the external probes. They fail on the previous behavior.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.conductor_cli import ops_cli, ops_cli_update, ops_runtime
from src.conductor_cli.ops_cli import cmd_foreground
from src.conductor_cli.ops_fleet import load_fleet
from src.conductor_cli.ops_support import Completed, OpsError, load_settings
from src.conductor_cli.ops_upgrade import upgrade
from src.server.app.ready import probe_daemon_heartbeats
from tests.unit.test_cond_cli_update import (
    HelpFails,
    _provider_started,
    _rewrite_daemon,
)
from tests.unit.test_cond_ops import FakeSystem, _layout


@pytest.fixture(autouse=True)
def isolated_probe_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith("CONDUCTOR_") or any(
            mark in name.upper()
            for mark in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "CREDENTIAL")
        ):
            monkeypatch.delenv(name, raising=False)


class TaggedSystem(FakeSystem):
    def __init__(self) -> None:
        super().__init__()
        self.ready_calls = 0
        self.restored = False
        self.ready_requires_restore = False
        self.ready_after_restore = False

    def run(self, argv, **kwargs):
        result = super().run(argv, **kwargs)
        repo = Path(kwargs.get("cwd") or ".")
        if argv[:2] == ["uv", "sync"]:
            (repo / ".venv" / "bound-version").write_text(self.head)
        if argv[:2] == ["pnpm", "build"]:
            dist = repo / "dist"
            dist.mkdir(parents=True, exist_ok=True)
            (dist / "bound-version").write_text(self.head)
        if argv[:2] == ["pg_restore", "--clean"]:
            self.restored = True
        return result

    def popen(self, argv, *, cwd, **kwargs):
        pid = super().popen(argv, cwd=cwd, **kwargs)
        repo = Path(cwd)
        dep = repo / ".venv" / "bound-version"
        artifact = repo / "web" / "dist" / "bound-version"
        self.procs[pid]["dep_version"] = dep.read_text() if dep.exists() else "missing"
        self.procs[pid]["artifact_version"] = (
            artifact.read_text() if artifact.exists() else "missing"
        )
        self.procs[pid]["git_head"] = self.head
        return pid

    def http_get(self, url, timeout):
        if url.endswith("/ready"):
            self.ready_calls += 1
            self.urls.append(url)
            if self.restored or (
                self.head == "abc1234def" and not self.ready_requires_restore
            ):
                if self.restored:
                    self.ready_after_restore = True
                return 200, '{"status":"ready"}'
            return 503, '{"status":"not_ready"}'
        return super().http_get(url, timeout)


def _prepare(tmp_path, monkeypatch, system: TaggedSystem):
    _layout(tmp_path, monkeypatch)
    settings = load_settings()
    settings.web_mode = "static"
    fleet = load_fleet(settings)
    return settings, fleet


def _plant_backend(fake: FakeSystem, settings) -> int:
    pid = fake.popen(
        [str(settings.python), str(settings.repo / "server.py")],
        cwd=settings.repo,
        env={"PATH": "/usr/bin"},
        log_path=settings.log_dir / "backend.log",
        max_bytes=1024,
        backups=1,
    )
    path = ops_runtime.pid_path(settings, ops_runtime.Component("backend"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n", encoding="utf-8")
    return pid


def test_failed_v9_readiness_restores_code_but_keeps_old_bound_runtime(
    tmp_path, monkeypatch
):
    system = TaggedSystem()
    settings, fleet = _prepare(tmp_path, monkeypatch, system)
    with pytest.raises(OpsError, match="ready check failed"):
        upgrade(system, settings, fleet, ref="v9")
    alive = [system.procs[pid] for pid in system.alive_pids]
    assert system.head == "abc1234def"
    assert alive
    assert all(proc["git_head"] == "abc1234def" for proc in alive)
    observed = [(proc["dep_version"], proc["artifact_version"]) for proc in alive]
    print(f"rollback alive process dependency/artifact versions: {observed}")
    assert all(proc["dep_version"] == "abc1234def" for proc in alive)
    assert all(proc["artifact_version"] == "abc1234def" for proc in alive)
    assert sum(argv[:2] == ["uv", "sync"] for argv in system.commands) >= 2
    assert sum(argv[:2] == ["pnpm", "build"] for argv in system.commands) >= 2
    assert not any(argv[:2] == ["pg_restore", "--clean"] for argv in system.commands)


def test_restore_db_happens_after_readiness_and_leaves_runtime_stopped(
    tmp_path, monkeypatch
):
    system = TaggedSystem()
    system.ready_requires_restore = True
    settings, fleet = _prepare(tmp_path, monkeypatch, system)
    with pytest.raises(OpsError, match="ready check failed"):
        upgrade(system, settings, fleet, ref="v9", restore_db=True)
    restore_positions = [
        i
        for i, argv in enumerate(system.commands)
        if argv[:2] == ["pg_restore", "--clean"]
    ]
    ready_count = sum(url.endswith("/ready") for url in system.urls)
    assert restore_positions
    assert ready_count >= 2
    assert system.restored
    print(f"alive processes after DB restore: {sorted(system.alive_pids)}")
    print(f"ready returned after DB restore: {system.ready_after_restore}")
    assert system.alive_pids
    assert system.ready_after_restore


def test_down_serializes_with_inflight_up(tmp_path, monkeypatch):
    _layout(tmp_path, monkeypatch)
    fake, entered, release, down_done = (
        FakeSystem(),
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    system, settings, fleet = ops_runtime.prepare(fake)
    events, errors = [], []

    def blocked_updates(system, settings, **kwargs):
        events.append("updates-entered")
        entered.set()
        assert release.wait(3)
        row = {"name": "codex", "result": "updated"}
        kwargs["on_finished"](row)
        events.append("updates-finished")
        return [row]

    monkeypatch.setattr(ops_cli_update, "update_clis", blocked_updates)

    def up():
        try:
            ops_runtime.start_all(system, settings, fleet)
        except Exception as exc:
            errors.append(repr(exc))

    def down():
        try:
            ops_runtime.stop_all(system, settings, fleet)
            events.append("down-returned")
        except Exception as exc:
            errors.append(repr(exc))
        finally:
            down_done.set()

    up_thread = threading.Thread(target=up)
    down_thread = threading.Thread(target=down)
    up_thread.start()
    assert entered.wait(2)
    down_thread.start()
    returned_before_update = down_done.wait(0.2)
    release.set()
    up_thread.join(5)
    down_thread.join(5)
    assert not errors and not up_thread.is_alive() and not down_thread.is_alive()
    alive = {pid: fake.command(pid) for pid in fake.alive_pids}
    print(
        {
            "events": events,
            "down_returned_before_update": returned_before_update,
            "alive": alive,
        }
    )
    assert events.index("updates-entered") < events.index("updates-finished")
    assert events.index("updates-finished") < events.index("down-returned")
    assert not returned_before_update and not alive


def test_callback_after_down_does_not_start_a_daemon(tmp_path, monkeypatch):
    _layout(tmp_path, monkeypatch)
    fake = FakeSystem()
    system, settings, fleet = ops_runtime.prepare(fake)
    saved: list = []

    def capture_updates(system, settings, **kwargs):
        saved.append(kwargs["on_finished"])
        return [{"name": "codex", "result": "updated"}]

    monkeypatch.setattr(ops_cli_update, "update_clis", capture_updates)
    ops_runtime.start_all(system, settings, fleet)
    ops_runtime.stop_all(system, settings, fleet)
    assert saved
    assert not fake.alive_pids
    saved[0]({"name": "codex", "result": "updated"})
    assert not fake.alive_pids


def test_up_foreground_supervises_without_exec(tmp_path, monkeypatch, capsys):
    _layout(tmp_path, monkeypatch)
    monkeypatch.setenv("CONDUCTOR_OPS_NO_EXEC", "1")
    fake = FakeSystem()
    executed: list[object] = []
    monkeypatch.setattr(os, "execvpe", lambda *args: executed.append(args))
    assert ops_cli.main(["up", "--foreground"], system=fake) == 0
    assert executed == []
    payload = json.loads(capsys.readouterr().out)
    assert payload["supervisor"] is True
    assert payload["pids"]["backend"] in fake.alive_pids
    assert any("daemon.py" in fake.command(pid) for pid in fake.alive_pids)


def test_stale_backend_pid_never_counts_as_ready(tmp_path, monkeypatch):
    _layout(tmp_path, monkeypatch)
    fake = FakeSystem()
    system, settings, fleet = ops_runtime.prepare(fake)
    path = ops_runtime.pid_path(settings, ops_runtime.Component("backend"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("99999999\n", encoding="utf-8")

    def stale_backend(*_args, **_kwargs) -> int:
        return 99999999

    monkeypatch.setattr(ops_runtime, "start_backend", stale_backend)
    with pytest.raises(OpsError, match="backend is not ready"):
        ops_runtime.start_all(system, settings, fleet)
    assert fake.alive_pids == set()


def test_foreground_login_updates_before_daemon_exec(tmp_path, monkeypatch):
    _layout(tmp_path, monkeypatch)
    fake = FakeSystem()
    _system, settings, _fleet = ops_runtime.prepare(fake)
    events = []
    monkeypatch.setattr(ops_cli, "RealSystem", lambda: fake)

    def updates(*args, **kwargs):
        events.append("updated")
        return [{"name": "codex", "result": "updated"}]

    monkeypatch.setattr(ops_cli_update, "update_clis", updates)
    monkeypatch.setattr(ops_cli_update, "startup_updates", updates)
    monkeypatch.setattr(
        os,
        "execvpe",
        lambda file, argv, env: events.append(
            "daemon-exec" if "--provider" in argv else "backend-exec"
        ),
    )
    ops_cli.cmd_foreground(settings, "backend", "", "")
    ops_cli.cmd_foreground(settings, "daemon", "codex", "daemon-1")
    print({"events": events})
    assert events[0] == "backend-exec"
    assert "updated" in events and events.index("updated") < events.index("daemon-exec")


def test_foreground_rejects_stale_backend_pid(tmp_path, monkeypatch):
    _layout(tmp_path, monkeypatch)
    fake = FakeSystem()
    _system, settings, _fleet = ops_runtime.prepare(fake)
    monkeypatch.setattr(ops_cli, "RealSystem", lambda: fake)
    path = ops_runtime.pid_path(settings, ops_runtime.Component("backend"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("99999999\n")
    monkeypatch.setattr(os, "execvpe", lambda *args: None)
    with pytest.raises(OpsError, match="backend"):
        ops_cli.cmd_foreground(settings, "daemon", "codex", "daemon-1")


def test_foreground_rejects_live_backend_that_is_not_ready(tmp_path, monkeypatch):
    _layout(tmp_path, monkeypatch)
    fake = FakeSystem()
    _system, settings, _fleet = ops_runtime.prepare(fake)
    monkeypatch.setattr(ops_cli, "RealSystem", lambda: fake)
    _plant_backend(fake, settings)

    def http_get(url, timeout):
        if str(url).endswith("/ready"):
            return 503, '{"status":"not_ready"}'
        return FakeSystem.http_get(fake, url, timeout)

    fake.http_get = http_get  # type: ignore[method-assign]
    executed: list[str] = []
    monkeypatch.setattr(os, "execvpe", lambda *_args: executed.append("exec"))
    with pytest.raises(OpsError, match="backend"):
        cmd_foreground(settings, "daemon", "codex", "daemon-1")
    assert executed == []


def test_foreground_accepts_live_owned_backend_that_is_ready(tmp_path, monkeypatch):
    _layout(tmp_path, monkeypatch)
    fake = FakeSystem()
    _system, settings, _fleet = ops_runtime.prepare(fake)
    monkeypatch.setattr(ops_cli, "RealSystem", lambda: fake)
    _plant_backend(fake, settings)
    executed: list[str] = []
    monkeypatch.setattr(
        os,
        "execvpe",
        lambda *_args: executed.append("exec"),
    )
    assert cmd_foreground(settings, "daemon", "codex", "daemon-1") == 1
    assert executed == ["exec"]


def test_post_install_version_timeout_keeps_daemon_down(tmp_path, monkeypatch):
    home = _layout(tmp_path, monkeypatch)
    _rewrite_daemon(home, "claude")

    class PostInstallTimeout(FakeSystem):
        updated = False

        def run(self, argv, **kwargs):
            if argv == ["brew", "upgrade", "--cask", "claude-code"]:
                self.commands.append(list(argv))
                self.updated = True
                return Completed(0)
            if argv == ["claude", "--version"]:
                if self.updated:
                    raise OpsError("timed out running claude")
                return Completed(0, "1.0.0\n")
            return super().run(argv, **kwargs)

    fake = PostInstallTimeout()
    system, settings, fleet = ops_runtime.prepare(fake)
    ops_runtime.start_all(system, settings, fleet)
    row = next(
        row
        for row in ops_cli_update.latest_results(settings)
        if row["name"] == "claude"
    )
    print({"claude_result": row, "daemon_started": _provider_started(fake, "claude")})
    assert row["result"] == "broken"
    assert ops_cli_update.persisted_health(settings)["claude"] == "broken"
    assert not _provider_started(fake, "claude")


def test_all_retired_heartbeats_are_ignored(tmp_path, monkeypatch):
    _layout(tmp_path, monkeypatch)
    _system, settings, _fleet = ops_runtime.prepare(FakeSystem())
    settings.daemons_file.write_text(
        '[[daemon]]\nprovider = "codex"\nid = "retired-only"\nretired = true\n'
    )
    settings.heartbeat_dir.mkdir(parents=True, exist_ok=True)
    (settings.heartbeat_dir / "retired-only.json").write_text(
        json.dumps(
            {
                "id": "retired-only",
                "at": (datetime.now(UTC) - timedelta(seconds=500)).isoformat(),
            }
        )
    )
    result = probe_daemon_heartbeats()
    print(result)
    assert result["ok"] is True


def test_missing_fleet_file_still_scans_heartbeats(tmp_path, monkeypatch):
    _layout(tmp_path, monkeypatch)
    _system, settings, _fleet = ops_runtime.prepare(FakeSystem())
    settings.daemons_file.unlink()
    settings.heartbeat_dir.mkdir(parents=True, exist_ok=True)
    stale_at = (datetime.now(UTC) - timedelta(seconds=500)).isoformat()
    (settings.heartbeat_dir / "stray.json").write_text(json.dumps({"at": stale_at}))
    result = probe_daemon_heartbeats()
    assert result["ok"] is False
    assert "stray" in result["stale"]


def test_non_object_heartbeat_is_unreadable_not_exception(tmp_path, monkeypatch):
    _layout(tmp_path, monkeypatch)
    _system, settings, _fleet = ops_runtime.prepare(FakeSystem())
    settings.heartbeat_dir.mkdir(parents=True, exist_ok=True)
    (settings.heartbeat_dir / "daemon-1.json").write_text("[]")
    result = probe_daemon_heartbeats()
    assert result["ok"] is False and "daemon-1" in result["unreadable"]
    (settings.heartbeat_dir / "daemon-1.json").write_text('{"id": "daemon-1"}')
    missing_time = probe_daemon_heartbeats()
    assert missing_time["ok"] is False and "daemon-1" in missing_time["unreadable"]
    (settings.heartbeat_dir / "daemon-1.json").write_text('{"at": "not-a-time"}')
    invalid_time = probe_daemon_heartbeats()
    assert invalid_time["ok"] is False and "daemon-1" in invalid_time["unreadable"]


def test_skipped_broken_cli_still_reported_by_doctor(tmp_path, monkeypatch):
    home = _layout(tmp_path, monkeypatch)
    _rewrite_daemon(home, "claude")
    fake = HelpFails({"claude"})
    system, settings, fleet = ops_runtime.prepare(fake)
    assert (
        ops_cli_update.update_clis(system, settings, only="claude", force=True)[0][
            "result"
        ]
        == "broken"
    )
    (home / ".conductor" / "clis.toml").write_text("[claude]\nskip = true\n")
    ops_cli_update.update_clis(system, settings, only="claude", force=True)
    assert ops_cli_update.daemon_is_blocked("claude", [], settings)
    from src.conductor_cli.doctor import doctor_report

    report = doctor_report(system, settings)
    status = ops_runtime.status_map(system, settings, fleet)
    text = ops_runtime.format_status(status)
    print(
        {
            "persisted_health": ops_cli_update.persisted_health(settings),
            "cli_rows": status["clis"],
            "doctor_cli_checks": [
                row for row in report["checks"] if row.get("name") == "cli"
            ],
            "status_text": text,
        }
    )
    assert any(
        row.get("name") == "cli"
        and row.get("cli") == "claude"
        and row.get("ok") is False
        for row in report["checks"]
    )
    claude = next(row for row in status["clis"] if row.get("name") == "claude")
    assert claude["result"] == "skipped"
    assert claude["health"] == "broken"
    assert "skipped" in text and "broken" in text


def test_unverified_rollback_does_not_clear_broken_health(tmp_path, monkeypatch):
    home = _layout(tmp_path, monkeypatch)
    _rewrite_daemon(home, "grok")

    class BrokenAfterRollback(FakeSystem):
        damaged = False

        def run(self, argv, **kwargs):
            if argv == ["npm", "i", "-g", "@xai-official/grok@latest"]:
                self.commands.append(list(argv))
                self.damaged = True
                return Completed(0)
            if argv == ["npm", "i", "-g", "@xai-official/grok@1.0.0"]:
                self.commands.append(list(argv))
                # Installer reports success, but has not restored the executable.
                return Completed(0)
            if argv == ["grok", "--version"]:
                if self.damaged:
                    raise OpsError("could not run grok: FileNotFoundError")
                return Completed(0, "1.0.0\n")
            return super().run(argv, **kwargs)

    fake = BrokenAfterRollback()
    system, settings, fleet = ops_runtime.prepare(fake)
    ops_runtime.start_all(system, settings, fleet)
    row = next(
        row for row in ops_cli_update.latest_results(settings) if row["name"] == "grok"
    )
    print(
        {
            "grok_result": row,
            "persisted_health": ops_cli_update.persisted_health(settings),
            "daemon_started": _provider_started(fake, "grok"),
            "cli_remains_missing": fake.damaged,
        }
    )
    assert row["result"] == "broken"
    assert ops_cli_update.persisted_health(settings)["grok"] == "broken"
    assert not _provider_started(fake, "grok")


def test_doctor_shows_broken_health_beside_a_skipped_attempt(tmp_path, monkeypatch):
    home = _layout(tmp_path, monkeypatch)
    _rewrite_daemon(home, "claude")
    fake = HelpFails({"claude"})
    system, settings, _fleet = ops_runtime.prepare(fake)
    assert (
        ops_cli_update.update_clis(system, settings, only="claude", force=True)[0]["result"]
        == "broken"
    )
    (home / ".conductor" / "clis.toml").write_text("[claude]\nskip = true\n")
    ops_cli_update.update_clis(system, settings, only="claude", force=True)
    from src.conductor_cli.doctor import doctor_report, format_doctor

    text = format_doctor(doctor_report(system, settings))
    assert "health=broken" in text
    assert "attempt=skipped" in text


def test_missing_fleet_bad_heartbeat_is_unreadable(tmp_path, monkeypatch):
    _layout(tmp_path, monkeypatch)
    _system, settings, _fleet = ops_runtime.prepare(FakeSystem())
    settings.daemons_file.unlink()
    settings.heartbeat_dir.mkdir(parents=True, exist_ok=True)
    (settings.heartbeat_dir / "stray.json").write_text("[]")
    result = probe_daemon_heartbeats()
    assert result["ok"] is False
    assert result["detail"] != "stale"
    assert "stray" in result["unreadable"]
    assert "stray" not in result["stale"]


def test_fleet_missing_differs_from_none_active(tmp_path, monkeypatch):
    _layout(tmp_path, monkeypatch)
    _system, settings, _fleet = ops_runtime.prepare(FakeSystem())
    settings.daemons_file.unlink()
    missing = probe_daemon_heartbeats()
    assert missing["detail"] == "fleet_missing"
    settings.daemons_file.write_text(
        '[[daemon]]\nprovider = "codex"\nid = "old"\nretired = true\n',
        encoding="utf-8",
    )
    idle = probe_daemon_heartbeats()
    assert idle["ok"] is True
    assert idle["detail"] == "none_active"


def _rollback_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("rollback ")]


class _ForwardUvFails(FakeSystem):
    """The upgrade sync fails once. Later syncs, including rollback, succeed."""

    def __init__(self) -> None:
        super().__init__()
        self._syncs = 0

    def run(self, argv, **kwargs):  # noqa: ANN001
        if argv[:2] == ["uv", "sync"]:
            self._syncs += 1
            if self._syncs == 1:
                self.commands.append(list(argv))
                return Completed(1, "", "forward sync failed")
        return super().run(argv, **kwargs)


class _RollbackSyncLeaks(FakeSystem):
    def __init__(self) -> None:
        super().__init__()
        self._syncs = 0

    def run(self, argv, **kwargs):  # noqa: ANN001
        if argv[:2] == ["uv", "sync"]:
            self._syncs += 1
            self.commands.append(list(argv))
            if self._syncs == 1:
                return Completed(1, "", "forward sync failed")
            return Completed(1, "", "db-secret leaked")
        return super().run(argv, **kwargs)


class _RollbackCheckoutFails(FakeSystem):
    def __init__(self) -> None:
        super().__init__()
        self._syncs = 0

    def run(self, argv, **kwargs):  # noqa: ANN001
        if argv[:4] == ["git", "checkout", "--detach", "abc1234def"]:
            self.commands.append(list(argv))
            return Completed(1, "", "checkout failed")
        if argv[:2] == ["uv", "sync"]:
            self._syncs += 1
            self.commands.append(list(argv))
            return Completed(1, "", "forward sync failed")
        return super().run(argv, **kwargs)


def test_successful_rollback_logs_every_step(tmp_path, monkeypatch, capsys):
    home = _layout(tmp_path, monkeypatch)
    fake = _ForwardUvFails()
    assert ops_cli.main(["upgrade", "--ref", "v9"], system=fake) == 1
    captured = capsys.readouterr()
    log = (home / ".conductor" / "logs" / "cond.log").read_text(encoding="utf-8")
    expected = [
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
    assert _rollback_lines(log) == expected
    assert _rollback_lines(captured.err) == expected
    assert "forward sync failed" in captured.err
    assert "rollback:" in captured.err
    assert not any(argv[:2] == ["pg_restore", "--clean"] for argv in fake.commands)


def test_restore_db_rollback_logs_that_the_restore_ran(tmp_path, monkeypatch, capsys):
    home = _layout(tmp_path, monkeypatch)
    fake = _ForwardUvFails()
    assert ops_cli.main(["upgrade", "--ref", "v9", "--restore-db"], system=fake) == 1
    log = (home / ".conductor" / "logs" / "cond.log").read_text(encoding="utf-8")
    lines = _rollback_lines(log)
    assert "rollback restore-db start" in lines
    assert "rollback restore-db done ran" in lines
    assert "rollback ready done code=200" in lines
    assert lines.index("rollback restore-db done ran") < lines.index("rollback runtime start")
    assert any(argv[:2] == ["pg_restore", "--clean"] for argv in fake.commands)
    err = capsys.readouterr().err
    assert "forward sync failed" in err
    assert "rollback ready done code=200" in err


def test_rollback_log_is_redacted_and_stops_at_the_first_failure(
    tmp_path, monkeypatch, capsys
):
    home = _layout(tmp_path, monkeypatch)
    leaked = _RollbackSyncLeaks()
    assert ops_cli.main(["upgrade", "--ref", "v9"], system=leaked) == 1
    log = (home / ".conductor" / "logs" / "cond.log").read_text(encoding="utf-8")
    err = capsys.readouterr().err
    assert "db-secret" not in log
    assert "db-secret" not in err
    assert "***" in log
    assert "rollback sync failed" in log
    assert "rollback rebuild start" not in log
    assert "forward sync failed" in err

    home = _layout(tmp_path / "checkout", monkeypatch)
    blocked = _RollbackCheckoutFails()
    assert ops_cli.main(["upgrade", "--ref", "v9"], system=blocked) == 1
    log = (home / ".conductor" / "logs" / "cond.log").read_text(encoding="utf-8")
    assert "rollback checkout failed" in log
    assert "rollback sync start" not in log
    assert blocked._syncs == 1


class _LayeredReady(FakeSystem):
    """HTTP /ready goes through the real heartbeat probe, not a constant 200."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, int, bool]] = []

    def popen(self, argv, *, cwd, **kwargs):  # noqa: ANN001
        pid = super().popen(argv, cwd=cwd, **kwargs)
        if any("conductor_local_daemon.py" in str(part) for part in argv):
            self.events.append(("daemon", pid, False))
        return pid

    def http_get(self, url, timeout):  # noqa: ANN001
        from src.server.app.ready import evaluate

        self.urls.append(url)
        path = url.split("?", 1)[0]
        if path.endswith("/health"):
            self.events.append(("health", 200, False))
            return 200, '{"status":"healthy"}'
        if not path.endswith("/ready"):
            return 200, '{"status":"healthy"}'
        scope = "backend" if "scope=backend" in url else ""
        code, body = evaluate(scope=scope)
        text = json.dumps(body)
        present = self._heartbeat().is_file()
        self.events.append(("backend-ready" if scope else "full-ready", code, present))
        return code, text

    def _heartbeat(self) -> Path:
        return Path(os.environ["CONDUCTOR_HOME"]) / "run" / "heartbeats" / "daemon-1.json"


def test_cold_start_waits_for_backend_scope_before_daemons(tmp_path, monkeypatch):
    from src.server.app.ready import ReadyProbes, evaluate, set_probes

    _layout(tmp_path, monkeypatch)

    def ok() -> dict[str, object]:
        return {"ok": True, "detail": "ok"}

    set_probes(
        ReadyProbes(
            database=ok,
            redis=ok,
            mcp_registry=ok,
            dispatch_config=ok,
            daemon_heartbeats=probe_daemon_heartbeats,
        )
    )
    try:
        cold, cold_body = evaluate()
        assert cold == 503
        assert "daemon-1" in cold_body["checks"]["daemon_heartbeats"]["missing"]
        backend, backend_body = evaluate(scope="backend")
        assert backend == 200
        assert "daemon_heartbeats" not in backend_body["checks"]
        fake = _LayeredReady()
        assert ops_cli.main(["up"], system=fake) == 0
        kinds = [kind for kind, _code, _seen in fake.events]
        assert kinds.index("backend-ready") < kinds.index("daemon")
        assert kinds.index("daemon") < kinds.index("full-ready")
        full = next(item for item in fake.events if item[0] == "full-ready")
        assert full[1] == 200
        assert full[2] is True
        backend_event = next(item for item in fake.events if item[0] == "backend-ready")
        assert backend_event[1] == 200
        assert backend_event[2] is False
    finally:
        set_probes(None)


class _FullReadyTimesOut(FakeSystem):
    def http_get(self, url, timeout):  # noqa: ANN001
        self.urls.append(url)
        path = url.split("?", 1)[0]
        if path.endswith("/health") or "scope=backend" in url:
            backend = any("server.py" in self.command(pid) for pid in self.alive_pids)
            if not backend and path.endswith("/health"):
                return 503, '{"status":"not_ready"}'
            if "scope=backend" in url:
                return 200, '{"status":"ready","checks":{"database":{"ok":true}}}'
            return 200, '{"status":"healthy"}'
        if path.endswith("/ready"):
            heartbeat = Path(os.environ["CONDUCTOR_HOME"]) / "run" / "heartbeats" / "daemon-1.json"
            if heartbeat.is_file():
                heartbeat.unlink()
            body = {
                "status": "not_ready",
                "checks": {
                    "daemon_heartbeats": {
                        "ok": False,
                        "missing": ["daemon-1"],
                        "stale": [],
                        "unreadable": [],
                    }
                },
            }
            return 503, json.dumps(body)
        return super().http_get(url, timeout)


def test_full_ready_timeout_names_the_daemon_and_leaves_it_up(
    tmp_path, monkeypatch, capsys
):
    _layout(tmp_path, monkeypatch)
    monkeypatch.setenv("CONDUCTOR_READY_TIMEOUT", "0.2")
    fake = _FullReadyTimesOut()
    assert ops_cli.main(["up"], system=fake) == 1
    err = capsys.readouterr().err
    assert "full /ready timed out" in err
    assert "daemon-1" in err
    assert any("conductor_local_daemon.py" in " ".join(argv) for argv in fake.commands)
    assert any(
        "conductor_local_daemon.py" in fake.command(pid) for pid in fake.alive_pids
    )
    assert any("scope=backend" in url for url in fake.urls)


def test_supervisor_reaps_real_children_without_kill_zero(tmp_path, monkeypatch):
    import sys
    import time

    from src.conductor_cli.ops_cli import _supervise
    from src.conductor_cli.ops_support import RealSystem

    _layout(tmp_path, monkeypatch)
    system = RealSystem()
    settings = load_settings()
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")}
    log = settings.log_dir / "child.log"

    def spawn(script: str) -> int:
        return system.popen(
            [sys.executable, "-c", script],
            cwd=settings.repo,
            env=env,
            log_path=log,
            max_bytes=10000,
            backups=1,
        )

    killed: list[int] = []
    real_kill = os.kill

    def spy(pid: int, sig: int) -> None:
        if sig == 0:
            killed.append(pid)
        real_kill(pid, sig)

    monkeypatch.setattr(os, "kill", spy)
    backend = spawn("raise SystemExit(0)")
    sibling = spawn("import time; time.sleep(30)")
    started = time.monotonic()
    try:
        code = _supervise(system, settings, {"backend": backend, "web": sibling})
        elapsed = time.monotonic() - started
    finally:
        proc = getattr(system, "_children", {}).get(sibling)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
    assert code == 0
    assert elapsed < 8
    assert killed == []
    assert system.child_poll(backend) == 0

    both = RealSystem()
    first = both.popen(
        [sys.executable, "-c", "raise SystemExit(0)"],
        cwd=settings.repo,
        env=env,
        log_path=settings.log_dir / "both.log",
        max_bytes=10000,
        backups=1,
    )
    second = both.popen(
        [sys.executable, "-c", "raise SystemExit(3)"],
        cwd=settings.repo,
        env=env,
        log_path=settings.log_dir / "both.log",
        max_bytes=10000,
        backups=1,
    )
    started = time.monotonic()
    # No backend key: the supervisor has to wait until every child has exited.
    code = _supervise(both, settings, {"left": first, "right": second})
    assert code == 0
    assert time.monotonic() - started < 8
    assert both.child_poll(first) == 0
    assert both.child_poll(second) == 3
