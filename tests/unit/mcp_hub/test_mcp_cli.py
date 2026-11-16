"""``conductor mcp`` — CLI wiring over mcp_hub. HOME is always the tmp home."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.conductor_cli.cli import main


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.delenv("CONDUCTOR_URL", raising=False)
    monkeypatch.delenv("CONDUCTOR_SKILLS_REPO", raising=False)
    monkeypatch.delenv("CONDUCTOR_SKILLS_CONFIG", raising=False)
    return root


def _loads(text: str) -> dict | list | str:
    body = text.strip()
    if not body:
        return ""
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def run(argv: list[str]) -> tuple[int, dict | list | str, dict | list | str]:
    import contextlib
    import io

    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv)
    return code, _loads(stdout.getvalue()), _loads(stderr.getvalue())


def _add(home: Path, *extra: str) -> tuple[int, dict | list | str, dict | list | str]:
    del home
    return run(
        [
            "mcp",
            "add",
            "--name",
            "search",
            "--transport",
            "stdio",
            "--command",
            "echo",
            "--env",
            "TOKEN",
            *extra,
            "--json",
        ]
    )


def test_add_is_a_dry_run_until_apply(home: Path) -> None:
    code, envelope, err = _add(home)

    assert code == 0 and err == ""
    assert envelope["command"] == "mcp add"
    assert envelope["exit_code"] == 0
    assert envelope["errors"] is None
    data = envelope["data"]
    assert data["dry_run"] is True
    assert data["server"]["env"] == {"TOKEN": "${TOKEN}"}
    assert not (home / ".conductor" / "mcp" / "servers.toml").exists()

    code, envelope, err = _add(home, "--apply")
    assert code == 0 and err == ""
    assert envelope["data"]["dry_run"] is False
    stored = (home / ".conductor" / "mcp" / "servers.toml").read_text(encoding="utf-8")
    assert 'TOKEN = "${TOKEN}"' in stored


def test_add_rejects_env_values(home: Path) -> None:
    secret = "super-secret-value"
    code, out, envelope = run(
        [
            "mcp",
            "add",
            "--name",
            "search",
            "--command",
            "echo",
            "--env",
            f"TOKEN={secret}",
            "--json",
        ]
    )

    assert code == 2 and out == ""
    assert envelope["exit_code"] == 2
    assert envelope["errors"][0]["code"] == "usage"
    assert "secrets must be references" in envelope["errors"][0]["message"]
    rendered = json.dumps(envelope)
    assert secret not in rendered
    assert not (home / ".conductor" / "mcp" / "servers.toml").exists()


def test_list_and_status_do_not_print_env_literals(home: Path) -> None:
    assert _add(home, "--apply")[0] == 0
    code, listed, err = run(["mcp", "list", "--json"])
    assert code == 0 and err == ""
    assert listed["data"]["servers"][0]["name"] == "search"
    assert listed["data"]["servers"][0]["env"] == {"TOKEN": "${TOKEN}"}

    code, status, err = run(["mcp", "status", "--json"])
    assert code == 0 and err == ""
    assert status["command"] == "mcp status"
    assert status["data"]["servers"][0]["env"] == {"TOKEN": "${TOKEN}"}
    assert "claude" in status["data"]["agents"]


def test_disable_enable_and_remove_require_apply(home: Path) -> None:
    assert _add(home, "--apply")[0] == 0
    registry = home / ".conductor" / "mcp" / "servers.toml"
    before = registry.read_text(encoding="utf-8")

    code, envelope, _err = run(["mcp", "disable", "search", "--json"])
    assert code == 0
    assert envelope["data"] == {"dry_run": True, "name": "search", "enabled": False}
    assert registry.read_text(encoding="utf-8") == before

    code, envelope, _err = run(["mcp", "disable", "search", "--apply", "--json"])
    assert code == 0 and envelope["data"]["dry_run"] is False
    assert "enabled = false" in registry.read_text(encoding="utf-8")

    code, _envelope, _err = run(["mcp", "enable", "search", "--apply", "--json"])
    assert code == 0
    assert "enabled = true" in registry.read_text(encoding="utf-8")

    code, envelope, _err = run(["mcp", "remove", "search", "--json"])
    assert code == 0 and envelope["data"]["dry_run"] is True
    assert "search" in registry.read_text(encoding="utf-8")

    code, envelope, _err = run(["mcp", "remove", "search", "--apply", "--json"])
    assert code == 0 and envelope["data"] == {"dry_run": False, "removed": "search"}
    assert "search" not in registry.read_text(encoding="utf-8")


def test_check_prints_tool_count_and_elapsed_without_env(home: Path) -> None:
    assert _add(home, "--apply")[0] == 0
    code, envelope, err = run(["mcp", "check", "search", "--json"])

    assert code == 0 and err == ""
    data = envelope["data"]
    assert data["status"] == "error"
    assert data["tool_count"] is None
    assert isinstance(data["elapsed_ms"], int)
    assert data["lines"] == [f"search: tool_count=None elapsed_ms={data['elapsed_ms']}"]
    assert "env" not in data
    assert "TOKEN" not in json.dumps(data)


def test_check_unknown_server_is_runtime(home: Path) -> None:
    code, out, envelope = run(["mcp", "check", "missing", "--json"])
    assert code == 1 and out == ""
    assert envelope["exit_code"] == 1
    assert envelope["errors"][0]["code"] == "cli_error"
    assert "missing" in envelope["errors"][0]["message"]


def test_import_dry_run_does_not_keep_secrets(home: Path) -> None:
    secret = "super-secret-value"
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "search": {
                        "command": "echo",
                        "args": [],
                        "env": {"TOKEN": secret},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    code, envelope, err = run(["mcp", "import", "--json"])

    assert code == 0 and err == ""
    assert envelope["data"]["dry_run"] is True
    imported = envelope["data"]["imported"]["search"]
    assert imported["env"] == {"TOKEN": "${TOKEN}"}
    assert secret not in json.dumps(envelope)
    assert not (home / ".conductor" / "mcp" / "servers.toml").exists()

    code, envelope, err = run(["mcp", "import", "--apply", "--json"])
    assert code == 0 and err == ""
    assert envelope["data"]["dry_run"] is False
    stored = (home / ".conductor" / "mcp" / "servers.toml").read_text(encoding="utf-8")
    assert secret not in stored
    assert "${TOKEN}" in stored


def test_diff_and_apply_are_dry_until_apply_then_rollback(home: Path) -> None:
    assert _add(home, "--apply")[0] == 0
    code, diff, err = run(["mcp", "diff", "--json"])
    assert code == 0 and err == ""
    assert diff["command"] == "mcp diff"
    assert diff["data"]["claude"]["unified_diff"]
    assert not (home / ".claude.json").exists()

    code, preview, err = run(["mcp", "apply", "--json"])
    assert code == 0 and err == ""
    assert "applied" not in preview["data"]
    assert not (home / ".claude.json").exists()

    code, applied, err = run(["mcp", "apply", "--apply", "--json"])
    assert code == 0 and err == ""
    assert applied["data"]["applied"] is True
    snapshot = applied["data"]["snapshot_id"]
    assert snapshot
    written = (home / ".claude.json").read_text(encoding="utf-8")
    assert "echo" in written
    assert "TOKEN" not in written or "${TOKEN}" in written

    written_bytes = (home / ".claude.json").read_bytes()
    code, preview, err = run(["mcp", "rollback", snapshot, "--json"])
    assert code == 0 and err == ""
    assert preview["data"]["dry_run"] is True
    assert preview["data"]["snapshot_id"] == snapshot
    assert (home / ".claude.json").read_bytes() == written_bytes

    code, rolled, err = run(["mcp", "rollback", snapshot, "--apply", "--json"])
    assert code == 0 and err == ""
    assert rolled["data"]["snapshot_id"] == snapshot
    assert not (home / ".claude.json").exists()


def test_missing_names_exit_2(home: Path) -> None:
    code, out, envelope = run(["mcp", "add", "--json"])
    assert code == 2 and out == ""
    assert envelope["errors"][0]["message"] == "add requires --name"

    code, out, envelope = run(["mcp", "remove", "--json"])
    assert code == 2
    assert envelope["command"] == "mcp remove"
    assert "server name" in envelope["errors"][0]["message"]
