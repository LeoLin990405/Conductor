"""lark_cli keeps credentials in lark-cli and builds bounded typed commands.

Moved from tests/unit/ptc_agent/agent/tools/test_feishu_cli.py in R9b L05.
"""

import ast
import asyncio
from pathlib import Path

import pytest

from src.server.services.autonomy import lark_cli

REPO_ROOT = Path(__file__).resolve().parents[5]


def test_read_messages_builds_a_bounded_user_scoped_command():
    args = lark_cli._build_args(
        "read_messages",
        chat_id="oc_123",
        query="",
        start="2026-10-28T09:00:00+08:00",
        end="2026-10-28T15:00:00+08:00",
        limit=30,
        text="",
        identity="",
    )
    assert args[:2] == ["im", "+chat-messages-list"]
    assert ["--chat-id", "oc_123"] == args[2:4]
    assert "--format" in args and "json" in args
    assert ["--page-size", "30"] == args[args.index("--page-size"):args.index("--page-size") + 2]
    assert "--order" in args and "desc" in args
    assert ["--start", "2026-10-28T09:00:00+08:00"] == args[
        args.index("--start"):args.index("--start") + 2
    ]
    assert ["--as", "user"] == args[-2:]


def test_search_chats_requires_a_group_keyword():
    with pytest.raises(ValueError, match="query"):
        lark_cli._build_args(
            "search_chats",
            chat_id="", query="", start="", end="", limit=20, text="", identity="",
        )


def test_send_message_carries_text_but_no_credentials():
    args = lark_cli._build_args(
        "send_message",
        chat_id="oc_123",
        query="",
        start="",
        end="",
        limit=20,
        text="收盘快照",
        identity="bot",
    )
    assert args[:2] == ["im", "+messages-send"]
    assert "--text" in args and "收盘快照" in args
    assert ["--as", "bot"] == args[-2:]
    assert not any("secret" in part.lower() for part in args)


def test_missing_binary_returns_install_hint(monkeypatch):
    monkeypatch.setenv("FEISHU_CLI_BIN", "/definitely/not/lark-cli")
    returncode, _stdout, stderr = asyncio.run(lark_cli._run_lark(["whoami"], timeout=1))
    assert returncode == 127
    assert "npm install -g @larksuite/cli" in stderr


def test_the_feishu_source_does_not_import_ptc_agent():
    """R9b L05: the Router's Feishu Source runs lark-cli without src/ptc_agent."""
    for rel in ("src/server/services/autonomy/feishu.py", "src/server/services/autonomy/lark_cli.py"):
        tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
        modules = [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ] + [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ]
        assert not [m for m in modules if m.split(".")[0] == "ptc_agent" or m.startswith("src.ptc_agent")], rel


def test_the_tool_uses_the_same_helpers():
    from ptc_agent.agent.tools import feishu_cli

    assert feishu_cli._build_args is lark_cli._build_args
    assert feishu_cli._run_lark is lark_cli._run_lark
