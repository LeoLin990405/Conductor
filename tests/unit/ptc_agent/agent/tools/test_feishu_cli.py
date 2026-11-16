"""The FeishuCLI tool renders lark-cli output; argv/run helpers are tested in
tests/unit/server/services/autonomy/test_lark_cli.py (R9b L05)."""

import json

import pytest

from ptc_agent.agent.tools import feishu_cli


def test_requires_the_explicit_feature_flag(monkeypatch):
    monkeypatch.delenv("FEISHU_CLI_ENABLED", raising=False)
    assert feishu_cli.feishu_cli_enabled() is False
    monkeypatch.setenv("FEISHU_CLI_ENABLED", "on")
    assert feishu_cli.feishu_cli_enabled() is True


@pytest.mark.asyncio
async def test_tool_reports_not_configured_without_exposing_cli_details(monkeypatch):
    async def fake_run(args, *, timeout):
        assert args == ["whoami"]
        return (
            1,
            "",
            json.dumps({
                "ok": False,
                "error": {"type": "config", "subtype": "not_configured", "message": "not configured"},
            }),
        )

    monkeypatch.setattr(feishu_cli, "_run_lark", fake_run)
    result = await feishu_cli.create_feishu_cli_tool().ainvoke({"action": "status"})
    assert "not configured" in result
    assert "config init --new" in result


@pytest.mark.asyncio
async def test_tool_returns_parsed_json(monkeypatch):
    async def fake_run(args, *, timeout):
        assert args == ["im", "+chat-list", "--format", "json", "--page-size", "2", "--types", "group", "--as", "user"]
        return 0, json.dumps({"ok": True, "data": {"items": [{"chat_id": "oc_1"}]}}), ""

    monkeypatch.setattr(feishu_cli, "_run_lark", fake_run)
    result = await feishu_cli.create_feishu_cli_tool().ainvoke({"action": "list_chats", "limit": 2})
    assert '"chat_id": "oc_1"' in result


@pytest.mark.asyncio
async def test_tool_rejects_an_invalid_action_before_running():
    result = await feishu_cli.create_feishu_cli_tool().ainvoke({"action": "delete_everything"})
    assert "参数错误" in result
    assert "status" in result
