"""Official CLI login manager: it drives the vendor command, not OAuth itself."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.server.services.native_runtime import login as login_module


@pytest.mark.requires_pty
def test_cursor_login_finishes_when_the_official_cli_reports_signed_in(
    monkeypatch, tmp_path: Path
):
    marker = tmp_path / "signed-in"
    script = tmp_path / "cursor-agent"
    script.write_text(
        """#!/bin/sh
if [ "$1" = "status" ]; then
  if [ -f "%s" ]; then
    printf '{"isAuthenticated": true}'
  else
    printf '{"isAuthenticated": false}'
  fi
  exit 0
fi
if [ "$1" = "login" ]; then
  printf 'Open a browser and navigate to https://example.test/login?challenge=x\\n'
  touch "%s"
  sleep 5
  exit 0
fi
exit 1
"""
        % (marker, marker)
    )
    script.chmod(0o755)
    monkeypatch.setattr(login_module, "_binary_for", lambda provider: str(script))
    monkeypatch.setattr(login_module, "_SPECS", dict(login_module._SPECS))
    session = login_module.login_manager.start("cursor")
    assert session["status"] in {"waiting", "authenticated"}
    deadline = time.time() + 8
    current = session
    while time.time() < deadline:
        current = login_module.login_manager.get(session["session_id"])
        if current["status"] != "waiting":
            break
        time.sleep(0.2)
    assert current["auth_url"] == "https://example.test/login?challenge=x"
    assert current["status"] == "authenticated"


@pytest.mark.requires_pty
def test_claude_login_relays_the_official_authorization_code(monkeypatch, tmp_path: Path):
    marker = tmp_path / "signed-in"
    script = tmp_path / "claude"
    script.write_text(
        """#!/bin/sh
if [ "$1" = "auth" ] && [ "$2" = "status" ]; then
  if [ -f "%s" ]; then
    printf '{"loggedIn": true}'
  else
    printf '{"loggedIn": false}'
  fi
  exit 0
fi
if [ "$1" = "auth" ] && [ "$2" = "login" ]; then
  printf 'Opening browser to sign in…\\n'
  printf 'Paste code here if prompted > '
  read code
  printf 'verified\\n'
  touch "%s"
  exit 0
fi
exit 1
"""
        % (marker, marker)
    )
    script.chmod(0o755)
    monkeypatch.setattr(login_module, "_binary_for", lambda provider: str(script))
    session = login_module.login_manager.start("claude-code")
    assert session["status"] == "waiting"
    deadline = time.time() + 5
    current = session
    while time.time() < deadline:
        current = login_module.login_manager.get(session["session_id"])
        if current["requires_input"]:
            break
        time.sleep(0.1)
    assert current["requires_input"] is True
    login_module.login_manager.submit_code(session["session_id"], "the-code")
    deadline = time.time() + 8
    while time.time() < deadline:
        current = login_module.login_manager.get(session["session_id"])
        if current["status"] == "authenticated":
            break
        time.sleep(0.2)
    assert current["status"] == "authenticated"


def test_kimi_and_zcode_credentials_are_native_login_signals(monkeypatch, tmp_path: Path):
    import json
    from src.server.services.native_runtime import login

    monkeypatch.setenv("HOME", str(tmp_path))
    assert login._probe_kimi("/opt/kimi") is False
    assert login._probe_zcode("/opt/zcode") is False

    kimi = tmp_path / ".kimi-code" / "credentials" / "kimi-code.json"
    kimi.parent.mkdir(parents=True)
    kimi.write_text(json.dumps({"access_token": "a", "refresh_token": "r"}))
    assert login._probe_kimi("/opt/kimi") is True

    zcode = tmp_path / ".zcode" / "v2" / "credentials.json"
    zcode.parent.mkdir(parents=True)
    zcode.write_text(json.dumps({"zcodejwttoken": "jwt"}))
    assert login._probe_zcode("/opt/zcode") is True


def test_codex_status_is_read_from_the_official_cli(monkeypatch, tmp_path: Path):
    from src.server.services.native_runtime import login

    script = tmp_path / "codex"
    script.write_text(
        """#!/bin/sh
if [ "$1" = "login" ] && [ "$2" = "status" ]; then
  printf 'Logged in using ChatGPT\\n'
  exit 0
fi
exit 1
"""
    )
    script.chmod(0o755)
    assert login._probe_codex(str(script)) is True
