from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.server.services.native_runtime.acp_runtime import (
    AcpNativeRuntime,
    _mcp_servers,
)


def test_mcp_servers_convert_stdio_and_http():
    servers = _mcp_servers([
        {
            "name": "local",
            "command": "uv",
            "args": ["run", "python", "x.py"],
            "env": {"TOKEN": "t"},
        },
        {
            "name": "remote",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer t"},
        },
    ])
    assert servers[0]["command"].endswith("/uv")
    assert servers[0]["env"] == [{"name": "TOKEN", "value": "t"}]
    assert servers[1]["type"] == "http"
    assert servers[1]["headers"] == [{"name": "Authorization", "value": "Bearer t"}]


def test_acp_update_mapping_covers_text_tool_and_usage():
    runtime = AcpNativeRuntime(provider="dsh", argv=["dsh"], broker_thread_id="t")
    text = runtime._update_events({
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": "hello"},
    })
    assert [event.kind for event in text] == ["text_delta"]

    started = runtime._update_events({
        "sessionUpdate": "tool_call",
        "toolCallId": "c1",
        "title": "bash",
        "rawInput": {"command": "echo hi"},
        "status": "pending",
    })
    assert started[0].kind == "tool_started"
    assert started[0].data["args"] == {"command": "echo hi"}

    done = runtime._update_events({
        "sessionUpdate": "tool_call_update",
        "toolCallId": "c1",
        "title": "bash",
        "status": "completed",
        "rawOutput": "hi",
    })
    assert done[0].kind == "tool_result"
    assert done[0].data["status"] == "success"

    usage = runtime._update_events({
        "sessionUpdate": "usage_update",
        "used": 123,
        "size": 1000,
    })
    assert usage[0].data["total_tokens"] == 123


def test_acp_permission_decision_prefers_allow_and_reject_options():
    runtime = AcpNativeRuntime(provider="dsh", argv=["dsh"], broker_thread_id="t")
    params = {
        "options": [
            {"optionId": "yes", "name": "Allow", "kind": "allow_once"},
            {"optionId": "no", "name": "Reject", "kind": "reject_once"},
        ]
    }
    approve = runtime._decision_result(
        {"x": {"decisions": [{"type": "approve"}]}}, params
    )
    assert approve["outcome"]["optionId"] == "yes"
    reject = runtime._decision_result(
        {"x": {"decisions": [{"type": "reject"}]}}, params
    )
    assert reject["outcome"]["optionId"] == "no"


def test_acp_env_upgrades_status_capabilities(monkeypatch):
    from src.server.services.native_runtime import registry

    monkeypatch.setenv("NATIVE_CLI_ACP", "deepseek-dsh")
    status = registry.status_for("deepseek-dsh")
    assert status.capabilities.approvals is True
    assert status.capabilities.tool_events is True
    monkeypatch.setenv("NATIVE_CLI_ACP", "")
    status = registry.status_for("deepseek-dsh")
    assert status.capabilities.approvals is False


@pytest.mark.asyncio
async def test_a_replacement_process_initializes_and_resumes_the_session():
    """Closing the transport keeps the session id and drops the client.

    The next ensure is a new process. It must initialize and session/resume
    that id before any prompt. Returning because the id is already set would
    send the notice to a process that never resumed the session.
    """
    runtime = AcpNativeRuntime(provider="acp", argv=["acp"], cwd="/tmp/work")
    first = MagicMock()
    first.start = AsyncMock()
    first.request = AsyncMock(
        side_effect=[
            {"protocolVersion": 1},
            {"sessionId": "sess-kept"},
        ]
    )
    second_calls: list[tuple[str, dict]] = []
    second = MagicMock()
    second.start = AsyncMock()

    async def request(method, params, timeout=None):
        second_calls.append((method, dict(params)))
        if method == "initialize":
            return {"protocolVersion": 1}
        if method == "session/resume":
            return {"sessionId": params["sessionId"]}
        return {}

    second.request = request
    with patch(
        "src.server.services.native_runtime.acp_runtime.AcpClient",
        side_effect=[first, second],
    ):
        await runtime._ensure("/tmp/work", None)
        assert runtime._session_id == "sess-kept"
        runtime._client = None
        await runtime._ensure("/tmp/work", "sess-kept")

    assert [call[0] for call in second_calls[:2]] == ["initialize", "session/resume"]
    assert second_calls[1][1]["sessionId"] == "sess-kept"


@pytest.mark.asyncio
async def test_the_same_acp_process_does_not_initialize_again():
    """A live client that already has the session is not resumed twice."""
    runtime = AcpNativeRuntime(provider="acp", argv=["acp"], cwd="/tmp/work")
    client = MagicMock()
    client.start = AsyncMock()
    client.request = AsyncMock(
        side_effect=[
            {"protocolVersion": 1},
            {"sessionId": "sess-kept"},
        ]
    )
    with patch(
        "src.server.services.native_runtime.acp_runtime.AcpClient",
        return_value=client,
    ):
        await runtime._ensure("/tmp/work", None)
        await runtime._ensure("/tmp/work", "sess-kept")

    assert client.request.await_count == 2
