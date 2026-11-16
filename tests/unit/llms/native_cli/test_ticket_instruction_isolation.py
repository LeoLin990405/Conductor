from __future__ import annotations

from unittest.mock import patch

import pytest

from src.llms.native_cli.base import NativeCliUnavailable
from src.llms.native_cli.claude import ClaudeCodeAdapter
from src.llms.native_cli.codex import CodexExecAdapter


def test_codex_ticket_invocation_keeps_login_home_and_ignores_user_guidance():
    argv = CodexExecAdapter(
        binary="codex", isolate_user_instructions=True
    ).build_invocation("ticket", cwd="/tmp/work").argv
    assert argv[:5] == (
        "codex",
        "exec",
        "--ignore-user-config",
        "--config",
        "project_doc_max_bytes=0",
    )
    # Personal memory is off too (test_personal_memory_isolation.py).
    assert "--json" in argv
    assert ("--disable", "memories") == argv[
        argv.index("--disable") : argv.index("--disable") + 2
    ]
    assert "--sandbox" in argv


def test_normal_codex_invocation_keeps_user_guidance_default():
    argv = CodexExecAdapter(binary="codex").build_invocation("chat").argv
    assert "--ignore-user-config" not in argv
    assert "project_doc_max_bytes=0" not in argv


@pytest.mark.parametrize("store", ["file", "keyring", "auto", "ephemeral"])
def test_codex_ticket_invocation_preserves_only_the_effective_auth_store(
    tmp_path, store
):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        f'cli_auth_credentials_store = "{store}"\n'
        'developer_instructions = "ignore the Ticket"\n'
    )
    with (
        patch(
            "src.llms.native_cli.isolation.approved_env_value", return_value=None
        ),
        patch(
            "src.llms.native_cli.isolation.private_home", return_value=tmp_path
        ),
    ):
        argv = CodexExecAdapter(
            binary="codex", isolate_user_instructions=True
        ).build_invocation("ticket").argv
    assert f'cli_auth_credentials_store="{store}"' in argv
    assert all("developer_instructions" not in item for item in argv)


def test_codex_ticket_invocation_fails_closed_on_unknown_auth_store(tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'cli_auth_credentials_store = "not-supported"\n'
    )
    with (
        patch(
            "src.llms.native_cli.isolation.approved_env_value", return_value=None
        ),
        patch(
            "src.llms.native_cli.isolation.private_home", return_value=tmp_path
        ),
        pytest.raises(NativeCliUnavailable, match="credential-store"),
    ):
        CodexExecAdapter(
            binary="codex", isolate_user_instructions=True
        ).build_invocation("ticket")


def test_claude_ticket_invocation_uses_safe_mode_without_bare_login_bypass():
    argv = ClaudeCodeAdapter(binary="claude", safe_mode=True).build_invocation(
        "ticket"
    ).argv
    assert "--safe-mode" in argv
    assert "--bare" not in argv


def test_normal_claude_invocation_keeps_customization_default():
    argv = ClaudeCodeAdapter(binary="claude").build_invocation("chat").argv
    assert "--safe-mode" not in argv
