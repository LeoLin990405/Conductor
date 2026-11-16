"""Onboarding: detect CLIs, write agents.toml, hand login to the CLI itself."""

from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path

from src.conductor_cli.ops_cli import main
from src.conductor_cli.onboarding import agents_path, load_agents
from tests.unit.test_cond_ops import FakeSystem, _assert_no_secret, _layout

_TERMINAL_LEAK = "SECRET-OUTPUT-SHOULD-NOT-LEAK"


class TerminalSystem(FakeSystem):
    def __init__(self):
        super().__init__()
        self.status_code = 0
        self.terminal_envs = []

    def run_terminal(self, argv, *, env=None, cwd=None, timeout=None):
        self.commands.append(list(argv))
        self.terminal_envs.append(dict(env or {}))
        code = self.status_code if argv[-1:] == ["status"] else 0
        from src.conductor_cli.ops_support import Completed

        return Completed(code, _TERMINAL_LEAK, "")


def test_setup_writes_agents_toml_mode_600(tmp_path: Path, monkeypatch, capsys):
    home = _layout(tmp_path, monkeypatch)
    fake = TerminalSystem()
    assert main(["setup", "--write", "--json"], system=fake) == 0
    payload = json.loads(capsys.readouterr().out)
    path = Path(payload["path"])
    assert path == home / ".conductor" / "agents.toml"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text(encoding="utf-8")
    _assert_no_secret(text)
    assert "CURSOR_API_KEY" not in text
    from src.conductor_cli.ops_support import load_settings

    agents = load_agents(load_settings())
    assert agents
    assert all(row.mode == "official" and row.enabled for row in agents.values())
    assert any("--version" in command for command in fake.commands)


def test_set_backend_records_env_names_not_values(tmp_path: Path, monkeypatch, capsys):
    _layout(tmp_path, monkeypatch)
    fake = TerminalSystem()
    assert main(["setup", "--write"], system=fake) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "agents",
                "set",
                "claude",
                "--mode",
                "custom",
                "--backend-note",
                "DeepSeek (user configured)",
                "--backend-vendor",
                "deepseek",
                "--passthrough-env",
                "ANTHROPIC_BASE_URL",
                "--passthrough-env",
                "ANTHROPIC_MODEL",
            ],
            system=fake,
        )
        == 0
    )
    from src.conductor_cli.ops_support import load_settings

    row = load_agents(load_settings())["claude"]
    assert row.mode == "custom"
    assert row.passthrough_env == ["ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"]
    text = agents_path(load_settings()).read_text(encoding="utf-8")
    assert "sk-" not in text
    _assert_no_secret(text)


def test_rejected_passthrough_is_not_echoed(tmp_path: Path, monkeypatch, capsys):
    _layout(tmp_path, monkeypatch)
    fake = TerminalSystem()
    secret_value = "ANTHROPIC_API_KEY=sk-" "do-not-print-this"
    code = main(
        ["agents", "set", "claude", "--passthrough-env", secret_value],
        system=fake,
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "sk-" "do-not-print-this" not in err
    assert secret_value not in err
    assert "variable names" in err


def test_login_uses_cli_command_and_skips_secrets_file(
    tmp_path: Path, monkeypatch, capsys
):
    home = _layout(tmp_path, monkeypatch)
    secrets = home / ".conductor" / "secrets.env"
    os.chmod(secrets, 0o000)
    fake = TerminalSystem()
    try:
        code = main(["agents", "login", "claude", "--json"], system=fake)
    finally:
        os.chmod(secrets, 0o600)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["argv"][:2] == ["claude", "auth"]
    assert payload["argv"][-1] == "login"
    assert payload["for_worker"] is False
    _assert_no_secret(json.dumps(payload))
    assert _TERMINAL_LEAK not in json.dumps(payload)
    assert fake.commands[0] == ["claude", "auth", "login"]
    assert len(fake.commands) >= 2
    assert fake.commands[1][-1] == "status"


def test_login_then_status_reports_status_code_without_leaking_output(
    tmp_path: Path, monkeypatch, capsys
):
    _layout(tmp_path, monkeypatch)
    fake = TerminalSystem()
    fake.status_code = 1
    code = main(["agents", "login", "claude", "--json"], system=fake)
    captured = capsys.readouterr()
    assert fake.commands[0] == ["claude", "auth", "login"]
    assert len(fake.commands) >= 2
    assert fake.commands[1][-1] == "status"
    assert _TERMINAL_LEAK not in captured.out
    assert _TERMINAL_LEAK not in captured.err
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["code"] == 1
    assert _TERMINAL_LEAK not in json.dumps(payload)
    _assert_no_secret(captured.out + captured.err)
    assert code == 1


def test_worker_login_uses_isolated_home(tmp_path: Path, monkeypatch, capsys):
    home = _layout(tmp_path, monkeypatch)
    worker = tmp_path / "worker-home"
    fake = TerminalSystem()
    marker = home / ".claude" / "keep-me"
    marker.parent.mkdir()
    marker.write_text("operator", encoding="utf-8")
    assert (
        main(
            [
                "agents",
                "login",
                "grok",
                "--for-worker",
                "--worker-home",
                str(worker),
                "--json",
            ],
            system=fake,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["for_worker"] is True
    assert payload["home"] == str(worker.resolve())
    assert payload["argv"] == ["grok", "login"]
    assert _TERMINAL_LEAK not in json.dumps(payload)
    _assert_no_secret(json.dumps(payload))
    assert fake.terminal_envs
    assert all(
        env.get("HOME") == str(worker.resolve()) for env in fake.terminal_envs
    )
    assert marker.read_text(encoding="utf-8") == "operator"
    assert not (home / ".grok").exists() or list((home / ".grok").iterdir()) == []


def test_codex_worker_login_is_refused(tmp_path: Path, monkeypatch, capsys):
    _layout(tmp_path, monkeypatch)
    code = main(
        [
            "agents",
            "login",
            "codex",
            "--for-worker",
            "--worker-home",
            str(tmp_path / "w"),
        ],
        system=TerminalSystem(),
    )
    assert code == 1
    assert "developer_instructions" in capsys.readouterr().err


def test_backend_note_round_trips_line_feed_and_carriage_return(
    tmp_path: Path, monkeypatch, capsys
):
    _layout(tmp_path, monkeypatch)
    fake = TerminalSystem()
    note = "line1\nline2\rline3"
    assert (
        main(
            ["agents", "set", "claude", "--backend-note", note],
            system=fake,
        )
        == 0
    )
    capsys.readouterr()
    from src.conductor_cli.ops_support import load_settings

    path = agents_path(load_settings())
    text = path.read_text(encoding="utf-8")
    _assert_no_secret(text)
    parsed = tomllib.loads(text)
    assert parsed["agents"]["claude"]["backend_note"] == note


def test_passthrough_env_allows_api_key_name_and_hides_assignment(
    tmp_path: Path, monkeypatch, capsys
):
    _layout(tmp_path, monkeypatch)
    fake = TerminalSystem()
    code = main(
        ["agents", "set", "claude", "--passthrough-env", "FOO=secret"],
        system=fake,
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "secret" not in err
    _assert_no_secret(err)
    assert (
        main(
            ["agents", "set", "claude", "--passthrough-env", "ANTHROPIC_API_KEY"],
            system=fake,
        )
        == 0
    )
    from src.conductor_cli.ops_support import load_settings

    text = agents_path(load_settings()).read_text(encoding="utf-8")
    _assert_no_secret(text)
    parsed = tomllib.loads(text)
    assert parsed["agents"]["claude"]["passthrough_env"] == ["ANTHROPIC_API_KEY"]


def test_setup_write_requires_force_when_agents_toml_exists(
    tmp_path: Path, monkeypatch, capsys
):
    home = _layout(tmp_path, monkeypatch)
    fake = TerminalSystem()
    path = home / ".conductor" / "agents.toml"
    note = "custom-backend-should-not-survive"
    original = (
        "# Conductor agent roster. No secret values.\n"
        "\n"
        "[agents.claude]\n"
        "enabled = true\n"
        'mode = "custom"\n'
        f'backend_note = "{note}"\n'
    ).encode()
    path.write_bytes(original)
    os.chmod(path, 0o600)
    code = main(["setup", "--write"], system=fake)
    captured = capsys.readouterr()
    assert code == 2
    assert "pass --force" in captured.err
    _assert_no_secret(captured.out + captured.err)
    assert path.read_bytes() == original

    assert main(["setup", "--write", "--force"], system=fake) == 0
    backup = path.with_name("agents.toml.bak")
    assert backup.is_file()
    assert note.encode() in backup.read_bytes()
    rewritten = path.read_text(encoding="utf-8")
    assert note not in rewritten
    _assert_no_secret(rewritten)
    tomllib.loads(rewritten)


def test_symlink_agents_toml_write_leaves_target_unchanged(
    tmp_path: Path, monkeypatch, capsys
):
    home = _layout(tmp_path, monkeypatch)
    fake = TerminalSystem()
    conductor = home / ".conductor"
    target = conductor / "agents-target.toml"
    original = b'[agents.claude]\nenabled = false\nmode = "official"\n'
    target.write_bytes(original)
    os.chmod(target, 0o640)
    link = conductor / "agents.toml"
    link.symlink_to(target)
    previous = os.readlink(link)
    mode = stat.S_IMODE(target.stat().st_mode)
    main(
        ["agents", "set", "claude", "--backend-note", "should-not-follow-symlink"],
        system=fake,
    )
    capsys.readouterr()
    assert target.read_bytes() == original
    assert stat.S_IMODE(target.stat().st_mode) == mode
    assert link.is_symlink()
    assert os.readlink(link) == previous
    assert link.resolve() == target.resolve()


def test_detect_does_not_open_port_8000(tmp_path: Path, monkeypatch, capsys):
    from tests.unit.test_cond_ops import _assert_no_port_8000

    _layout(tmp_path, monkeypatch)
    fake = TerminalSystem()
    assert main(["setup", "--json"], system=fake) == 0
    capsys.readouterr()
    _assert_no_port_8000(fake)
    for argv in fake.commands:
        assert argv[-1] != "login"
        assert argv[-1] in {"--version", "status"}
