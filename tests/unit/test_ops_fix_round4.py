"""Ops follow-ups after the cond merge: optional MCP registry, CodexBar plist, migrate dry-run."""

from __future__ import annotations

import stat

from src.conductor_cli.ops_cli import main
from src.conductor_cli.ops_support import load_settings
from src.server.app.ready import ReadyProbes, evaluate, probe_mcp_registry
from tests.unit.test_cond_ops import FakeSystem, _layout

_V1_SCRIPT = """#!/bin/bash
if [ "$p" = "deepseek-dsh" ]; then
  . "$HOME/.config/deepseek/env"
fi
export CODEX_HOME="$HOME/.codex" DSH_HOME="$HOME/.dsh"
export CURSOR_API_KEY=do-not-copy-this-value
"""


def _ok() -> dict[str, object]:
    return {"ok": True, "detail": "ok"}


def _backend_probes(mcp) -> ReadyProbes:
    return ReadyProbes(
        database=_ok,
        redis=_ok,
        mcp_registry=mcp,
        dispatch_config=_ok,
        daemon_heartbeats=_ok,
    )


def test_missing_mcp_registry_is_not_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_MCP_REGISTRY", str(tmp_path / "missing" / "servers.toml"))
    assert probe_mcp_registry() == {"ok": True, "detail": "not_configured"}
    status, body = evaluate(_backend_probes(probe_mcp_registry), scope="backend")
    assert status == 200
    assert body["status"] == "ready"
    assert body["checks"]["mcp_registry"] == {"ok": True, "detail": "not_configured"}


def test_malformed_mcp_registry_fails_ready(tmp_path, monkeypatch):
    path = tmp_path / "servers.toml"
    path.write_text("[[[not toml", encoding="utf-8")
    monkeypatch.setenv("CONDUCTOR_MCP_REGISTRY", str(path))
    result = probe_mcp_registry()
    assert result == {"ok": False, "detail": "unreadable"}
    status, body = evaluate(_backend_probes(probe_mcp_registry), scope="backend")
    assert status == 503
    assert body["status"] == "not_ready"
    assert body["checks"]["mcp_registry"] == result


def test_readable_mcp_registry_is_ok(tmp_path, monkeypatch):
    path = tmp_path / "servers.toml"
    path.write_text("[servers]\n", encoding="utf-8")
    monkeypatch.setenv("CONDUCTOR_MCP_REGISTRY", str(path))
    assert probe_mcp_registry() == {"ok": True, "detail": "ok"}
    status, body = evaluate(_backend_probes(probe_mcp_registry), scope="backend")
    assert status == 200
    assert body["checks"]["mcp_registry"]["ok"] is True


def test_render_codexbar_writes_only_the_output_plist(tmp_path, monkeypatch, capsys):
    home = _layout(tmp_path, monkeypatch)
    launchd = home / "LaunchAgents"
    launchd.mkdir(parents=True)
    out = tmp_path / "generated" / "com.conductor.codexbar-snapshot.plist"
    out.parent.mkdir()
    fake = FakeSystem()
    assert main(["launchd", "render-codexbar", "--output", str(out)], system=fake) == 0
    printed = capsys.readouterr().out.strip()
    assert printed == str(out)
    text = out.read_text(encoding="utf-8")
    settings = load_settings()
    assert str(settings.python) in text
    assert f"{settings.repo}/scripts/codexbar_snapshot.py" in text
    assert "default-workspace/AlphaCN" not in text
    assert "/Users/you/Library/LaunchAgents" not in text
    assert "com.conductor.codexbar-snapshot" in text
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert fake.commands == []
    assert list(launchd.iterdir()) == []


def test_render_codexbar_stdout_does_not_write_launchagents(tmp_path, monkeypatch, capsys):
    home = _layout(tmp_path, monkeypatch)
    fake = FakeSystem()
    assert main(["launchd", "render-codexbar"], system=fake) == 0
    text = capsys.readouterr().out
    assert "com.conductor.codexbar-snapshot" in text
    assert str(load_settings().python) in text
    assert fake.commands == []
    assert not (home / "LaunchAgents" / "com.conductor.codexbar-snapshot.plist").exists()


def test_daemons_migrate_is_a_dry_run_unless_apply(tmp_path, monkeypatch, capsys):
    _layout(tmp_path, monkeypatch)
    script = tmp_path / "v1-daemon.sh"
    script.write_text(_V1_SCRIPT, encoding="utf-8")
    listing = tmp_path / "v1-daemons.txt"
    listing.write_text("cursor cursor-id\ncodex codex-id\n# comment\n", encoding="utf-8")
    output = tmp_path / "written" / "daemons.toml"
    fake = FakeSystem()
    assert (
        main(
            [
                "daemons",
                "migrate",
                "--script",
                str(script),
                "--list",
                str(listing),
                "--output",
                str(output),
            ],
            system=fake,
        )
        == 0
    )
    dry = capsys.readouterr().out
    assert not output.exists()
    assert "[[daemon]]" in dry
    assert 'provider = "cursor"' in dry
    assert "do-not-copy-this-value" not in dry
    assert "migrated" not in dry
    assert (
        main(
            [
                "daemons",
                "migrate",
                "--script",
                str(script),
                "--list",
                str(listing),
                "--output",
                str(output),
                "--apply",
            ],
            system=fake,
        )
        == 0
    )
    applied = capsys.readouterr().out
    assert output.is_file()
    assert f"migrated 2 daemons to {output}" in applied
    written = output.read_text(encoding="utf-8")
    assert "do-not-copy-this-value" not in written
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
