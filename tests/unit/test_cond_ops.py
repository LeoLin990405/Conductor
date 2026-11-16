"""cond up/down/status/restart/upgrade/doctor/clean/launchd with fakes.

Nothing here connects to 127.0.0.1:8000. Subprocesses are the fake system or
git commands bound to the pytest temporary directory.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.conductor_cli.deploy_launchd import install_units
from src.conductor_cli.ops_cli import foreground_spec, main
from src.conductor_cli.ops_fleet import DaemonSpec, migrate_v1, provider_environment
from src.conductor_cli.ops_runtime import pid_path, Component
from src.conductor_cli.ops_support import Completed, OpsError, load_settings, rotate_log
from src.server.app.ready import ReadyProbes, evaluate, set_probes

SECRET = "do-not-leak-cursor-key"
TOKEN = "do-not-leak-daemon-token"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.setenv("CONDUCTOR_FORBID_PORT_8000", "1")
    for name in list(os.environ):
        if name.endswith(
            ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")
        ) and not name.startswith("CONDUCTOR_"):
            monkeypatch.delenv(name, raising=False)


class FakeSystem:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.popen_env: list[dict[str, str]] = []
        self.urls: list[str] = []
        self.ports: list[int] = []
        self.procs: dict[int, dict[str, object]] = {}
        self.alive_pids: set[int] = set()
        self.next_pid = 41000
        self.now = 0.0
        self.head = "abc1234def"
        self.fail: set[str] = set()
        self.security_value = "keychain-secret-value"
        self.open_ports: set[int] = set()

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: dict | None = None,
        timeout: float | None = None,
    ) -> Completed:
        self.commands.append(list(argv))
        name = Path(argv[0]).name
        if name in self.fail or (len(argv) > 1 and argv[1] in self.fail):
            return Completed(1, "", f"{name} failed")
        if name == "git":
            return self._git(argv)
        if name == "pg_dump":
            path = Path(argv[argv.index("--file") + 1])
            path.write_bytes(b"PGDMP-fake")
            return Completed(0, "", "")
        if name == "pg_restore":
            return Completed(0, "TOC\n", "")
        if name == "alembic":
            return Completed(0, "rev-test (head)\n", "")
        if name == "security":
            return Completed(0, self.security_value + "\n", "")
        if name in {"uv", "pnpm", "launchctl"}:
            return Completed(0, "", "")
        return Completed(0, "", "")

    def _git(self, argv: list[str]) -> Completed:
        if argv[1] == "-C":
            args = argv[3:]
            worktree = Path(argv[2])
        else:
            args = argv[1:]
            worktree = None
        if args[:2] == ["rev-parse", "HEAD"] or args[:2] == ["rev-parse", "--verify"]:
            return Completed(0, self.head + "\n", "")
        if args[:1] == ["fetch"]:
            return Completed(0, "", "")
        if args[:2] == ["checkout", "--detach"]:
            self.head = args[-1]
            return Completed(0, "", "")
        if args[:2] == ["status", "--porcelain"]:
            return Completed(0, "", "")
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return Completed(0, "", "")
        if args[:2] == ["rev-parse", "--git-common-dir"] and worktree is not None:
            return Completed(0, str(worktree) + "\n", "")
        if args[:2] == ["worktree", "remove"]:
            Path(args[-1]).rmdir() if Path(args[-1]).is_dir() else None
            return Completed(0, "", "")
        return Completed(0, "", "")

    def popen(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        log_path: Path,
        max_bytes: int,
        backups: int,
    ) -> int:
        self.commands.append(list(argv))
        self.popen_env.append(dict(env))
        pid = self.next_pid
        self.next_pid += 1
        self.procs[pid] = {"argv": list(argv), "cwd": str(cwd)}
        self.alive_pids.add(pid)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        return pid

    def alive(self, pid: int) -> bool:
        return pid in self.alive_pids

    def command(self, pid: int) -> str:
        proc = self.procs.get(pid) or {}
        argv = proc.get("argv") or []
        return " ".join(str(part) for part in argv)

    def cwd(self, pid: int) -> str:
        proc = self.procs.get(pid) or {}
        return str(proc.get("cwd") or "")

    def terminate(self, pid: int) -> None:
        self.alive_pids.discard(pid)

    def kill(self, pid: int) -> None:
        self.alive_pids.discard(pid)

    def http_get(self, url: str, timeout: float) -> tuple[int, str]:
        self.urls.append(url)
        if ":8000" in url:
            raise OpsError("refusing to contact port 8000")
        backend = any("server.py" in self.command(pid) for pid in self.alive_pids)
        if not backend:
            return 503, '{"status":"not_ready"}'
        path = url.split("?", 1)[0]
        if path.endswith("/ready"):
            return 200, '{"status":"ready"}'
        return 200, '{"status":"healthy"}'

    def port_open(self, host: str, port: int, timeout: float = 0.3) -> bool:
        self.ports.append(port)
        if port == 8000:
            raise OpsError("refusing to contact port 8000")
        return port in self.open_ports


def _layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    port: int = 54321,
    web: int = 54322,
) -> Path:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    (repo / "web").mkdir(parents=True)
    (repo / "server.py").write_text("print('server')\n", encoding="utf-8")
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    (repo / ".env").write_text(
        "DB_HOST=127.0.0.1\nDB_PORT=54329\nDB_USER=op\nDB_NAME=conductor\nDB_PASSWORD=db-secret\n",
        encoding="utf-8",
    )
    conductor = home / ".conductor"
    conductor.mkdir(parents=True)
    secrets = conductor / "secrets.env"
    secrets.write_text(
        f"CURSOR_API_KEY={SECRET}\nCODEX_HOME={home / '.codex'}\n",
        encoding="utf-8",
    )
    os.chmod(secrets, 0o600)
    (conductor / "daemons.toml").write_text(
        f"""
[backend]
port = {port}

[web]
port = {web}
mode = "preview"

[[daemon]]
provider = "codex"
id = "daemon-1"
env = ["CODEX_HOME"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    policy = home / ".conductor-daemon" / "workspaces-cli.json"
    policy.parent.mkdir(parents=True)
    policy.write_text("{}\n", encoding="utf-8")
    os.chmod(policy, 0o600)
    token = home / ".config" / "conductor" / "daemons" / "daemon-1.token"
    token.parent.mkdir(parents=True)
    token.write_text(TOKEN + "\n", encoding="utf-8")
    os.chmod(token, 0o600)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CONDUCTOR_HOME", str(conductor))
    monkeypatch.setenv("CONDUCTOR_REPO_ROOT", str(repo))
    monkeypatch.setenv("CONDUCTOR_BACKEND_PORT", str(port))
    monkeypatch.setenv("CONDUCTOR_WEB_PORT", str(web))
    monkeypatch.setenv("CONDUCTOR_LAUNCHD_DIR", str(home / "LaunchAgents"))
    monkeypatch.setenv("CONDUCTOR_LAUNCHD_DOMAIN", "gui/test")
    monkeypatch.delenv("CONDUCTOR_BASE_URL", raising=False)
    return home


def _assert_no_port_8000(fake: FakeSystem) -> None:
    for argv in fake.commands:
        if "--port" in argv:
            assert argv[argv.index("--port") + 1] != "8000"
        assert "127.0.0.1:8000" not in " ".join(argv)
    assert all(":8000" not in url for url in fake.urls)
    assert 8000 not in fake.ports


def _assert_no_secret(text: str) -> None:
    assert SECRET not in text
    assert TOKEN not in text
    assert "db-secret" not in text
    assert "keychain-secret-value" not in text


def test_lifecycle_up_status_restart_upgrade_down(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _layout(tmp_path, monkeypatch)
    fake = FakeSystem()
    assert main(["up"], system=fake) == 0
    assert main(["status"], system=fake) == 0
    assert main(["restart"], system=fake) == 0
    assert main(["upgrade", "--ref", "HEAD"], system=fake) == 0
    assert main(["down"], system=fake) == 0
    assert not fake.alive_pids
    _assert_no_port_8000(fake)
    captured = capsys.readouterr()
    _assert_no_secret(captured.out + captured.err)
    flat = [" ".join(argv) for argv in fake.commands]
    blob = "\n".join(flat)
    assert "server.py --host 127.0.0.1" in blob
    assert "--host 127.0.0.1 --port 54322" in blob or "--port 54322" in blob
    for needle in (
        "pg_dump",
        "pg_restore",
        "git fetch",
        "git checkout --detach HEAD",
        "uv sync",
        "alembic upgrade head",
        "pnpm install",
        "pnpm build",
    ):
        assert needle in blob
    assert "launchctl" not in blob
    daemon_envs = [env for env in fake.popen_env if env.get("CONDUCTOR_DAEMON_TOKEN")]
    assert daemon_envs
    assert all("CURSOR_API_KEY" not in env for env in daemon_envs)
    assert all(env.get("CONDUCTOR_DAEMON_TOKEN") == TOKEN for env in daemon_envs)


def test_upgrade_failure_rolls_code_back_without_restoring_db(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _layout(tmp_path, monkeypatch)
    fake = FakeSystem()
    fake.fail.add("uv")
    assert main(["upgrade", "--ref", "v9"], system=fake) == 1
    blob = "\n".join(" ".join(argv) for argv in fake.commands)
    assert "git checkout --detach abc1234def" in blob
    assert "--clean" not in blob
    _assert_no_secret(capsys.readouterr().err)
    _assert_no_port_8000(fake)


def test_upgrade_restore_db_flag_restores_the_backup(
    tmp_path: Path, monkeypatch
) -> None:
    _layout(tmp_path, monkeypatch)

    class FailForwardSync(FakeSystem):
        """The upgrade sync fails. The rollback sync must still run."""

        def __init__(self) -> None:
            super().__init__()
            self._syncs = 0

        def run(self, argv, **kwargs):
            if argv[:2] == ["uv", "sync"]:
                self._syncs += 1
                if self._syncs == 1:
                    self.commands.append(list(argv))
                    return Completed(1, "", "uv failed")
            return super().run(argv, **kwargs)

    fake = FailForwardSync()
    assert main(["upgrade", "--ref", "v9", "--restore-db"], system=fake) == 1
    assert any(argv[:2] == ["pg_restore", "--clean"] for argv in fake.commands)
    assert any(
        argv[:4] == ["git", "checkout", "--detach", "abc1234def"]
        for argv in fake.commands
    )


def test_secrets_must_be_mode_600_and_are_not_echoed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    home = _layout(tmp_path, monkeypatch)
    secrets = home / ".conductor" / "secrets.env"
    os.chmod(secrets, 0o644)
    fake = FakeSystem()
    assert main(["up"], system=fake) == 1
    captured = capsys.readouterr()
    assert "mode 600" in captured.err
    _assert_no_secret(captured.out + captured.err)
    assert fake.popen_env == [] or all(
        SECRET not in env.values() for env in fake.popen_env
    )


def test_refuses_zshenv_and_keeps_only_allow_listed_provider_env(
    tmp_path: Path, monkeypatch
) -> None:
    home = _layout(tmp_path, monkeypatch)
    zshenv = home / ".zshenv"
    zshenv.write_text(f"CURSOR_API_KEY={SECRET}\n", encoding="utf-8")
    settings = load_settings()
    daemon = DaemonSpec(
        "cursor", "cur-1", env=["CURSOR_API_KEY"], env_files=["~/.zshenv"]
    )
    with pytest.raises(OpsError, match="zshenv"):
        provider_environment(settings, daemon)
    deepseek = home / ".config" / "deepseek" / "env"
    deepseek.parent.mkdir(parents=True)
    deepseek.write_text(
        "DEEPSEEK_API_KEY=deepseek-secret\nOTHER_SECRET=nope\n",
        encoding="utf-8",
    )
    os.chmod(deepseek, 0o600)
    settings.sensitive.clear()
    loaded = provider_environment(
        settings,
        DaemonSpec("deepseek-dsh", "dsh-1", env=["DEEPSEEK_API_KEY", "DSH_HOME"]),
    )
    assert loaded["DEEPSEEK_API_KEY"] == "deepseek-secret"
    assert "OTHER_SECRET" not in loaded
    assert loaded["DSH_HOME"].endswith(".dsh")
    assert SECRET not in loaded.values()


def test_keychain_reference_is_not_printed(tmp_path: Path, monkeypatch, capsys) -> None:
    home = _layout(tmp_path, monkeypatch)
    (home / ".conductor" / "daemons.toml").write_text(
        """
[backend]
port = 54321
[web]
port = 54322
mode = "preview"
[[daemon]]
provider = "cursor"
id = "daemon-1"
env = ["CURSOR_API_KEY"]
keychain = { CURSOR_API_KEY = "conductor-cursor:account" }
""".strip()
        + "\n",
        encoding="utf-8",
    )
    fake = FakeSystem()
    assert main(["up"], system=fake) == 0
    assert any(
        argv[:3] == ["security", "find-generic-password", "-s"]
        for argv in fake.commands
    )
    envs = [env for env in fake.popen_env if env.get("CURSOR_API_KEY")]
    assert envs and envs[0]["CURSOR_API_KEY"] == "keychain-secret-value"
    _assert_no_secret(capsys.readouterr().out + capsys.readouterr().err)


def test_launchd_install_renders_without_bootstrap_and_enable_uses_fake(
    tmp_path, monkeypatch, capsys
) -> None:
    home = _layout(tmp_path, monkeypatch)
    launchd = home / "LaunchAgents"
    old = launchd / "com.leo.conductor.server.plist"
    launchd.mkdir()
    old.write_text("old", encoding="utf-8")
    fake = FakeSystem()
    assert main(["launchd", "install"], system=fake) == 0
    assert fake.commands == []
    assert not old.exists()
    assert (launchd / "com.leo.conductor.server.plist.migrated").is_file()
    snapshot = (launchd / "com.conductor.codexbar-snapshot.plist").read_text(encoding="utf-8")
    assert str(load_settings().python) in snapshot
    assert "default-workspace/AlphaCN" not in snapshot
    assert "/Users/you/Library/LaunchAgents" not in snapshot
    plist = (launchd / "com.conductor.supervisor.plist").read_text(encoding="utf-8")
    assert "<string>com.conductor.supervisor</string>" in plist
    assert "<string>up</string>" in plist
    assert "<string>--foreground</string>" in plist
    assert "com.leo.conductor" not in plist
    assert SECRET not in plist
    assert "<false/>" in plist
    assert list(launchd.glob("com.conductor.daemon.*.plist")) == []
    assert not (launchd / "com.conductor.backend.plist").exists()
    assert not (launchd / "com.conductor.web.plist").exists()
    settings = load_settings()
    argv, _env, _cwd = foreground_spec(settings, "backend", "", "")
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert "--port" in argv and argv[argv.index("--port") + 1] == "54321"
    assert main(["launchd", "enable"], system=fake) == 0
    assert any(
        argv[:3] == ["launchctl", "bootstrap", "gui/test"] for argv in fake.commands
    )
    assert all(
        "/Users/you/Library/LaunchAgents" not in " ".join(argv)
        for argv in fake.commands
    )
    _assert_no_secret(capsys.readouterr().out)
    written = install_units(fake, settings)
    assert written


def test_migrate_imports_names_not_secrets_or_zshenv(tmp_path: Path) -> None:
    script = tmp_path / "v1-daemon.sh"
    script.write_text(
        """#!/bin/bash
if [ "$p" = "deepseek-dsh" ]; then
  . "$HOME/.config/deepseek/env"
fi
if [ "$p" = "cursor" ]; then
  eval "$(zsh -c 'source ~/.zshenv; printf CURSOR_API_KEY=%q' \\"$CURSOR_API_KEY\\")"
fi
export CODEX_HOME="$HOME/.codex" KIMI_CODE_HOME="$HOME/.kimi-code" DSH_HOME="$HOME/.dsh"
export CURSOR_API_KEY=do-not-copy-this-value
""",
        encoding="utf-8",
    )
    listing = tmp_path / "v1-daemons.txt"
    listing.write_text(
        "cursor cursor-id\ncodex codex-id\ndeepseek-dsh dsh-id\n# comment\n",
        encoding="utf-8",
    )
    output = tmp_path / "daemons.toml"
    fleet = migrate_v1(script, listing, output)
    text = output.read_text(encoding="utf-8")
    assert "do-not-copy-this-value" not in text
    assert "zshenv" not in text
    providers = {daemon.provider for daemon in fleet.daemons}
    assert providers == {"cursor", "codex", "deepseek-dsh"}
    cursor = next(daemon for daemon in fleet.daemons if daemon.provider == "cursor")
    assert "CURSOR_API_KEY" in cursor.env
    deepseek = next(
        daemon for daemon in fleet.daemons if daemon.provider == "deepseek-dsh"
    )
    assert "~/.config/deepseek/env" in deepseek.env_files
    assert "DSH_HOME" in deepseek.env
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_doctor_checks_web_port_daemons_and_policy_files(
    tmp_path, monkeypatch, capsys
) -> None:
    home = _layout(tmp_path, monkeypatch, web=5170)
    monkeypatch.setenv("CONDUCTOR_WEB_PORT", "5170")
    policy = home / ".config" / "conductor" / "daemon_policy.yaml"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("name: test\n", encoding="utf-8")
    os.chmod(policy, 0o600)
    from src.conductor_cli.daemon_grant import SigningKey

    key = SigningKey.generate("doctor-fixture")
    trusted = home / ".config" / "conductor" / "daemon_trusted_keys.json"
    trusted.write_text(
        json.dumps(
            {
                "format": 1,
                "keys": [{"kid": key.kid, "public_key": key.public_b64()}],
            }
        ),
        encoding="utf-8",
    )
    os.chmod(trusted, 0o600)
    fake = FakeSystem()
    fake.open_ports.add(5170)
    settings = load_settings()
    from src.conductor_cli.ops_fleet import load_fleet

    fleet = load_fleet(settings)
    component = Component("daemon", fleet.daemons[0].provider, fleet.daemons[0].ident)
    pid_file = pid_path(settings, component)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text("41000\n", encoding="utf-8")
    fake.procs[41000] = {
        "argv": ["python", "scripts/conductor_local_daemon.py", "--provider", "codex"],
        "cwd": str(settings.repo),
    }
    fake.alive_pids.add(41000)
    from src.conductor_cli.deploy_launchd import render_codexbar_plist

    plist_path = home / "LaunchAgents" / "com.conductor.codexbar-snapshot.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(render_codexbar_plist(settings), encoding="utf-8")
    from src.conductor_cli.codexbar_snapshot import VALIDATED_CODEXBAR_VERSION

    snapshot_path = settings.conductor_home / "usage" / "codexbar-snapshot.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(
        json.dumps({"codexbar_version": VALIDATED_CODEXBAR_VERSION, "providers": []}),
        encoding="utf-8",
    )
    assert main(["doctor", "--json"], system=fake) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert 5170 in fake.ports
    assert 8000 not in fake.ports
    os.chmod(policy, 0o644)
    assert main(["doctor"], system=fake) == 1
    text = capsys.readouterr().out
    assert "FAIL" in text
    assert "name: test" not in text


def test_doctor_rejects_a_codexbar_job_on_the_main_clone(
    tmp_path, monkeypatch, capsys
) -> None:
    home = _layout(tmp_path, monkeypatch, web=5170)
    monkeypatch.setenv("CONDUCTOR_WEB_PORT", "5170")
    monkeypatch.setenv("CONDUCTOR_MAIN_CLONE_MARKER", "default-workspace/Conductor")
    for relative in (
        ".config/conductor/daemon_policy.yaml",
        ".config/conductor/daemon_trusted_keys.json",
    ):
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
        os.chmod(path, 0o600)
    clone = "/Users/example/Documents/deepseek-harness/default-workspace/Conductor"
    plist = home / "LaunchAgents" / "com.conductor.codexbar-snapshot.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.conductor.codexbar-snapshot</string>
<key>ProgramArguments</key>
<array>
<string>"""
        + clone
        + """/.venv/bin/python</string>
<string>"""
        + clone
        + """/scripts/codexbar_snapshot.py</string>
</array>
</dict></plist>
""",
        encoding="utf-8",
    )
    fake = FakeSystem()
    fake.open_ports.add(5170)
    assert main(["doctor"], system=fake) == 1
    text = capsys.readouterr().out
    assert "codexbar_snapshot" in text
    assert "main clone" in text


def test_clean_lists_and_apply_skips_unmerged_and_dirty(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    root = tmp_path / "wts"
    root.mkdir()
    home.mkdir()
    repo.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
    )
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, env=env)
    (repo / "README").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "base"], check=True, env=env
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", str(root / "merged"), "HEAD"],
        check=True,
        env=env,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "-b",
            "dirty",
            str(root / "dirty"),
            "HEAD",
        ],
        check=True,
        env=env,
    )
    (root / "dirty" / "local.txt").write_text("dirty\n", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "-b",
            "ahead",
            str(root / "ahead"),
            "HEAD",
        ],
        check=True,
        env=env,
    )
    (root / "ahead" / "new.txt").write_text("new\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(root / "ahead"), "add", "new.txt"], check=True, env=env
    )
    subprocess.run(
        ["git", "-C", str(root / "ahead"), "commit", "-m", "ahead"], check=True, env=env
    )
    broken = root / "broken"
    broken.mkdir()
    (broken / ".git").write_text(
        "gitdir: /nonexistent/conductor-gitdir\n", encoding="utf-8"
    )

    conductor = home / ".conductor"
    run = conductor / "run"
    run.mkdir(parents=True)
    now = datetime.now(UTC)
    (run / "registrations.json").write_text(
        json.dumps(
            {
                "daemons": [
                    {
                        "id": "stale",
                        "provider": "codex",
                        "pid": os.getpid(),
                        "heartbeat": (now - timedelta(days=3)).isoformat(),
                    },
                    {
                        "id": "fresh",
                        "provider": "grok",
                        "pid": os.getpid(),
                        "heartbeat": now.isoformat(),
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    (run / "dead.pid").write_text("99999999\n", encoding="utf-8")
    (run / "live.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CONDUCTOR_HOME", str(conductor))
    monkeypatch.setenv("CONDUCTOR_REPO_ROOT", str(repo))
    monkeypatch.setenv("CONDUCTOR_WORKTREE_ROOT", str(root))

    from src.conductor_cli.clean import apply_clean, clean_report
    from src.conductor_cli.ops_support import RealSystem

    system = RealSystem()
    settings = load_settings()
    report = clean_report(system, settings, root=root, base_ref="main", now=now)
    classes = {Path(item["path"]).name: item["class"] for item in report["worktrees"]}
    assert classes["merged"] == "merged_clean"
    assert classes["dirty"] == "uncommitted"
    assert classes["ahead"] == "unmerged"
    assert classes["broken"] == "broken"
    stale_ids = {item["id"] for item in report["stale_daemons"]}
    assert "stale" in stale_ids
    assert "fresh" not in stale_ids
    dead_names = {Path(item["path"]).name for item in report["dead_processes"]}
    assert "dead.pid" in dead_names
    assert "live.pid" not in dead_names

    applied = apply_clean(system, settings, report, base_ref="main")
    assert str(root / "merged") in applied["removed"]
    assert not (root / "merged").exists()
    assert (root / "dirty" / "local.txt").is_file()
    assert (root / "ahead").is_dir()
    assert (root / "broken").is_dir()
    assert str(root / "dirty") in applied["skipped"]
    assert str(root / "ahead") in applied["skipped"]
    saved = json.loads((run / "registrations.json").read_text(encoding="utf-8"))
    assert [row["id"] for row in saved["daemons"]] == ["fresh"]
    assert not (run / "dead.pid").exists()
    assert (run / "live.pid").is_file()


def test_log_rotation_keeps_a_bounded_history(tmp_path: Path) -> None:
    path = tmp_path / "backend.log"
    path.write_text("a" * 20, encoding="utf-8")
    rotate_log(path, max_bytes=10, backups=2)
    assert not path.exists()
    assert (tmp_path / "backend.log.1").read_text(encoding="utf-8") == "a" * 20
    (tmp_path / "backend.log").write_text("b" * 20, encoding="utf-8")
    rotate_log(tmp_path / "backend.log", max_bytes=10, backups=2)
    assert (tmp_path / "backend.log.1").read_text(encoding="utf-8") == "b" * 20
    assert (tmp_path / "backend.log.2").read_text(encoding="utf-8") == "a" * 20


def test_ready_reports_each_check_and_fails_closed() -> None:
    set_probes(
        ReadyProbes(
            database=lambda: {"ok": True},
            redis=lambda: {"ok": True},
            mcp_registry=lambda: {"ok": True},
            dispatch_config=lambda: {"ok": True},
            daemon_heartbeats=lambda: {"ok": True, "stale": []},
        )
    )
    status, body = evaluate()
    assert status == 200
    assert body["status"] == "ready"
    assert set(body["checks"]) == {
        "database",
        "redis",
        "mcp_registry",
        "dispatch_config",
        "daemon_heartbeats",
    }
    set_probes(
        ReadyProbes(
            database=lambda: {"ok": True},
            redis=lambda: {"ok": False, "detail": "down"},
            mcp_registry=lambda: {"ok": True},
            dispatch_config=lambda: {"ok": True},
            daemon_heartbeats=lambda: {"ok": True},
        )
    )
    status, body = evaluate()
    assert status == 503
    assert body["status"] == "not_ready"
    assert body["checks"]["redis"]["detail"] == "down"
    set_probes(None)


def test_ready_route_uses_injected_probes() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.server.app.ready import router

    set_probes(
        ReadyProbes(
            database=lambda: {"ok": False, "detail": "not_configured"},
            redis=lambda: {"ok": True},
            mcp_registry=lambda: {"ok": True},
            dispatch_config=lambda: {"ok": True},
            daemon_heartbeats=lambda: {"ok": True},
        )
    )
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["database"]["detail"] == "not_configured"
    set_probes(None)


def test_ready_backend_scope_skips_daemon_heartbeats() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.server.app.ready import router

    called = {"n": 0}

    def heartbeat() -> dict[str, object]:
        called["n"] += 1
        return {
            "ok": False,
            "detail": "missing",
            "missing": ["daemon-1"],
            "stale": [],
            "unreadable": [],
        }

    set_probes(
        ReadyProbes(
            database=lambda: {"ok": True, "detail": "ok"},
            redis=lambda: {"ok": True, "detail": "ok"},
            mcp_registry=lambda: {"ok": True, "detail": "ok"},
            dispatch_config=lambda: {"ok": True, "detail": "ok"},
            daemon_heartbeats=heartbeat,
        )
    )
    try:
        status, body = evaluate(scope="backend")
        assert status == 200
        assert called["n"] == 0
        assert "daemon_heartbeats" not in body["checks"]
        app = FastAPI()
        app.include_router(router)
        scoped = TestClient(app).get("/ready", params={"scope": "backend"})
        assert scoped.status_code == 200
        assert called["n"] == 0
        assert "daemon_heartbeats" not in scoped.json()["checks"]
        full = TestClient(app).get("/ready")
        assert full.status_code == 503
        assert called["n"] == 1
        assert full.json()["checks"]["daemon_heartbeats"]["missing"] == ["daemon-1"]
    finally:
        set_probes(None)


def test_cond_stop_alias_and_help_do_not_touch_port_8000(tmp_path, monkeypatch) -> None:
    _layout(tmp_path, monkeypatch)
    fake = FakeSystem()
    assert main(["stop"], system=fake) == 0
    _assert_no_port_8000(fake)
    script = Path(__file__).resolve().parents[2] / "scripts" / "cond"
    result = subprocess.run(
        [str(script), "--help"], check=False, capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "cond stop" in result.stdout
    assert "cond status" in result.stdout


def test_doctor_on_a_fake_system_never_opens_a_database(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The layout names DB_NAME=conductor; a system without a connector still connects to nothing."""
    import psycopg

    calls: list[object] = []

    def connect(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))
        raise AssertionError("doctor opened a database")

    monkeypatch.setattr(psycopg, "connect", connect)
    _layout(tmp_path, monkeypatch)
    main(["doctor"], system=FakeSystem())
    assert "WARN: schema_drift (skipped: no database connector)" in capsys.readouterr().out
    assert calls == []


def test_doctor_reports_schema_drift_through_the_system_connector(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from psycopg.conninfo import conninfo_to_dict

    from src.conductor_cli.schema_drift import PUBLIC_SCHEMA_SQL, REVISION_SQL, load_snapshot

    snapshot = load_snapshot()
    live = {table: list(columns) for table, columns in snapshot.tables.items()}
    dropped_table = sorted(live)[-1]
    del live[dropped_table]
    live["alembic_version"] = ["version_num"]
    live["stray_table"] = ["id"]

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class Conn:
        def execute(self, query):
            if query == PUBLIC_SCHEMA_SQL:
                return Result([(t, c) for t, cols in live.items() for c in cols])
            assert query == REVISION_SQL
            return Result([(snapshot.revision,)])

        def close(self):
            pass

    class DatabaseSystem(FakeSystem):
        def __init__(self) -> None:
            super().__init__()
            self.connects: list[dict[str, str]] = []

        def connect_database(self, conninfo: str) -> Conn:
            self.connects.append(conninfo_to_dict(conninfo))
            return Conn()

    monkeypatch.delenv("DB_NAME", raising=False)
    monkeypatch.delenv("PGSERVICE", raising=False)
    _layout(tmp_path, monkeypatch)
    fake = DatabaseSystem()
    assert main(["doctor"], system=fake) == 1
    text = capsys.readouterr().out
    assert f"FAIL: schema_drift (missing table {dropped_table})" in text
    assert "WARN: schema_drift (extra table stray_table (not in the snapshot))" in text
    assert "db-secret" not in text
    assert [(c["dbname"], c["host"], c["port"]) for c in fake.connects] == [("conductor", "127.0.0.1", "54329")]

    # The same layout pointed at postgres never reaches the connector.
    monkeypatch.setenv("DB_NAME", "postgres")
    fake = DatabaseSystem()
    main(["doctor"], system=fake)
    text = capsys.readouterr().out
    assert "WARN: schema_drift (skipped: the backend database is named postgres" in text
    assert "FAIL: schema_drift" not in text
    assert fake.connects == []
