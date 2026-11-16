"""Feishu Source really spawns the configured lark-cli and parses its JSON."""

from __future__ import annotations

import json
import stat

import pytest

from src.server.services.autonomy import feishu

_FAKE = r'''#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
log = os.environ.get("FAKE_LARK_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(args) + "\n")
if args[:2] == ["whoami"]:
    print(json.dumps({"user": {"name": "fake-bot"}}))
elif args[:2] == ["im", "+chat-messages-list"]:
    print(json.dumps({"data": {"items": [
        {"message_id": "om_2", "create_time": "2000",
         "sender": {"id": "ou_alice", "sender_type": "user"},
         "body": {"content": json.dumps({"text": "second"})}},
        {"message_id": "om_1", "create_time": "1000",
         "sender": {"id": "ou_alice", "sender_type": "user"},
         "body": {"content": json.dumps({"text": "first"})}},
    ]}}))
elif args[:2] == ["im", "+messages-send"]:
    print(json.dumps({"code": 0}))
else:
    print(json.dumps({"code": 0}))
'''


@pytest.fixture
def fake_lark(tmp_path, monkeypatch):
    binary = tmp_path / "lark-cli"
    binary.write_text(_FAKE, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "argv.log"
    monkeypatch.setenv("FEISHU_CLI_BIN", str(binary))
    monkeypatch.setenv("FAKE_LARK_LOG", str(log))
    return log


@pytest.mark.asyncio
async def test_poll_spawns_the_cli_and_sets_the_first_cursor(fake_lark):
    drafts, state = await feishu.poll_feishu_messages(
        source_id="s1",
        user_id="u1",
        config={"chat_id": "oc_1", "agent_open_id": "ou_bot"},
        state={},
    )
    assert drafts == []
    assert state["cursor"] == "2000"
    argv = [json.loads(line) for line in fake_lark.read_text().splitlines()]
    assert argv[0][:2] == ["im", "+chat-messages-list"]
    assert "--chat-id" in argv[0]
    assert argv[0][argv[0].index("--chat-id") + 1] == "oc_1"


@pytest.mark.asyncio
async def test_send_spawns_the_cli_with_the_chat_and_text(fake_lark):
    await feishu.send_feishu_message({"chat_id": "oc_1"}, "done")
    argv = [json.loads(line) for line in fake_lark.read_text().splitlines()]
    sent = argv[-1]
    assert sent[:2] == ["im", "+messages-send"]
    assert sent[sent.index("--chat-id") + 1] == "oc_1"
    assert sent[sent.index("--text") + 1] == "done"


@pytest.mark.asyncio
async def test_check_spawns_whoami(fake_lark):
    checked = await feishu.check_feishu_source({"chat_id": "oc_1"})
    assert checked["repository"] == "oc_1"
    argv = [json.loads(line) for line in fake_lark.read_text().splitlines()]
    assert argv[-1] == ["whoami"]
