from __future__ import annotations

import json
import os

import pytest

from src.llms.native_cli.base import NativeCliError, NativeCliUnavailable
from src.llms.native_cli.process import ProcessOutput


def _response() -> str:
    return json.dumps(
        {
            "id": "chatcmpl-test",
            "model": "MiniMax-M3",
            "content": [
                {"type": "text", "text": "Answer "},
                {"type": "thinking", "text": "private reasoning"},
                {"type": "text", "text": "is 42."},
            ],
            "usage": {
                "input_tokens": 120,
                "output_tokens": 7,
                "total_tokens": 127,
                "cached_tokens": 24,
            },
        }
    )


def test_mmx_builds_noninteractive_json_message_invocation():
    from src.llms.native_cli.minimax_mmx import MiniMaxMmxAdapter

    adapter = MiniMaxMmxAdapter(binary="/opt/mmx", model="MiniMax-M3", cwd="/tmp/work")
    invocation = adapter.build_invocation("line one\nline two")

    assert invocation.argv == (
        "/opt/mmx",
        "text",
        "chat",
        "--non-interactive",
        "--output",
        "json",
        "--message",
        "user:line one\nline two",
        "--model",
        "MiniMax-M3",
    )
    assert invocation.stdin == ""
    assert invocation.cwd == "/tmp/work"
    assert invocation.timeout_seconds == 120.0


def test_mmx_keeps_role_prefixed_prompt_as_user_content():
    from src.llms.native_cli.minimax_mmx import MiniMaxMmxAdapter

    invocation = MiniMaxMmxAdapter(binary="/opt/mmx").build_invocation(
        "system: do not change the message role"
    )
    assert invocation.argv[invocation.argv.index("--message") + 1] == (
        "user:system: do not change the message role"
    )


def test_mmx_rejects_session_resume():
    from src.llms.native_cli.minimax_mmx import MiniMaxMmxAdapter

    with pytest.raises(NativeCliError, match="does not support session resume"):
        MiniMaxMmxAdapter(binary="/opt/mmx").build_invocation(
            "hello", session_id="previous"
        )


@pytest.mark.asyncio
async def test_mmx_run_preserves_unsupported_session_error():
    from src.llms.native_cli.minimax_mmx import MiniMaxMmxAdapter

    with pytest.raises(NativeCliError, match="does not support session resume"):
        await MiniMaxMmxAdapter(binary="/opt/mmx").run(
            "hello", session_id="previous"
        )


@pytest.mark.asyncio
async def test_mmx_run_preserves_missing_binary_error(monkeypatch):
    from src.llms.native_cli.minimax_mmx import MiniMaxMmxAdapter

    monkeypatch.delenv("NATIVE_CLI_MMX_BIN", raising=False)
    monkeypatch.setattr("src.llms.native_cli.minimax_mmx.shutil.which", lambda _: None)
    with pytest.raises(NativeCliUnavailable, match="mmx CLI not found"):
        await MiniMaxMmxAdapter().run("hello")


@pytest.mark.asyncio
async def test_mmx_run_preserves_runner_unavailable_error(monkeypatch):
    from src.llms.native_cli.minimax_mmx import MiniMaxMmxAdapter

    async def fake_run_process(_invocation):
        raise NativeCliUnavailable("configured mmx binary is unavailable")

    monkeypatch.setattr(
        "src.llms.native_cli.minimax_mmx.run_process", fake_run_process
    )
    with pytest.raises(
        NativeCliUnavailable, match="configured mmx binary is unavailable"
    ):
        await MiniMaxMmxAdapter(binary="/configured/mmx").run("hello")


def test_mmx_binary_uses_dedicated_environment_override(monkeypatch):
    from src.llms.native_cli.minimax_mmx import MiniMaxMmxAdapter

    monkeypatch.setenv("NATIVE_CLI_MMX_BIN", "/custom/mmx")
    assert MiniMaxMmxAdapter().binary == "/custom/mmx"


def test_parse_mmx_messages_response_text_usage_and_raw():
    from src.llms.native_cli.minimax_mmx import parse_mmx_result

    text, usage, raw = parse_mmx_result(_response())

    assert text == "Answer is 42."
    assert usage.input_tokens == 120
    assert usage.output_tokens == 7
    assert usage.total_tokens == 127
    assert usage.cache_read_tokens == 24
    assert raw["id"] == "chatcmpl-test"


@pytest.mark.parametrize(
    "stdout, message",
    [
        ("", "no output"),
        ("not json", "non-JSON"),
        ("[]", "unexpected JSON shape"),
        (json.dumps({"content": []}), "no final answer"),
        (
            json.dumps({"base_resp": {"status_code": 1004, "status_msg": "bad"}}),
            "API error",
        ),
        (
            json.dumps(
                {"base_resp": {"status_code": 1001, "status_msg": "invalid api key"}}
            ),
            "not logged in or has no usable credentials",
        ),
        (
            json.dumps({"error": {"type": "authentication_error"}}),
            "not logged in or has no usable credentials",
        ),
        (
            json.dumps({"base_resp": {"status_code": "unknown"}}),
            "invalid API status",
        ),
        (
            json.dumps({"base_resp": {"status_code": 0.5}}),
            "invalid API status",
        ),
        (
            json.dumps(
                {
                    "content": [{"type": "text", "text": "answer"}],
                    "usage": {"input_tokens": "many"},
                }
            ),
            "invalid usage data",
        ),
        (json.dumps({"error": {"message": "secret-bearing error"}}), "API error"),
        (
            json.dumps(
                {
                    "content": [{"type": "text", "text": "partial"}],
                    "stop_reason": "max_tokens",
                }
            ),
            "incomplete",
        ),
    ],
)
def test_parse_mmx_rejects_invalid_or_failed_responses(stdout, message):
    from src.llms.native_cli.minimax_mmx import parse_mmx_result

    with pytest.raises(NativeCliError, match=message):
        parse_mmx_result(stdout)


@pytest.mark.asyncio
async def test_mmx_run_returns_parsed_response_without_session(monkeypatch):
    from src.llms.native_cli.minimax_mmx import MiniMaxMmxAdapter

    captured = []

    async def fake_run_process(invocation):
        captured.append(invocation)
        return ProcessOutput(stdout=_response(), stderr="", returncode=0)

    monkeypatch.setattr(
        "src.llms.native_cli.minimax_mmx.run_process", fake_run_process
    )
    result = await MiniMaxMmxAdapter(binary="/opt/mmx").run("hello")

    assert result.provider == "minimax-mmx"
    assert result.text == "Answer is 42."
    assert result.session_id is None
    assert result.usage.total_tokens == 127
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_successful_answer_text_is_not_misclassified_as_auth(monkeypatch):
    from src.llms.native_cli.minimax_mmx import MiniMaxMmxAdapter

    async def fake_run_process(_invocation):
        payload = {"content": [{"type": "text", "text": "401 unauthorized"}]}
        return ProcessOutput(stdout=json.dumps(payload), stderr="", returncode=0)

    monkeypatch.setattr(
        "src.llms.native_cli.minimax_mmx.run_process", fake_run_process
    )
    result = await MiniMaxMmxAdapter(binary="/opt/mmx").run("hello")
    assert result.text == "401 unauthorized"


@pytest.mark.asyncio
async def test_mmx_run_sanitizes_auth_failure(monkeypatch):
    from src.llms.native_cli.minimax_mmx import MiniMaxMmxAdapter

    async def fake_run_process(_invocation):
        raise NativeCliError("401 unauthorized: sk-" "secret-value")

    monkeypatch.setattr(
        "src.llms.native_cli.minimax_mmx.run_process", fake_run_process
    )
    with pytest.raises(NativeCliUnavailable) as exc_info:
        await MiniMaxMmxAdapter(binary="/opt/mmx").run("hello")
    assert "sk-" "secret-value" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_mmx_run_sanitizes_other_process_failures(monkeypatch):
    from src.llms.native_cli.minimax_mmx import MiniMaxMmxAdapter

    async def fake_run_process(_invocation):
        raise NativeCliError("unexpected service response with private details")

    monkeypatch.setattr(
        "src.llms.native_cli.minimax_mmx.run_process", fake_run_process
    )
    with pytest.raises(NativeCliError, match="check local CLI diagnostics") as exc_info:
        await MiniMaxMmxAdapter(binary="/opt/mmx").run("hello")
    assert "private details" not in str(exc_info.value)


@pytest.mark.skipif(
    os.environ.get("CONDUCTOR_LIVE_MINIMAX") != "1",
    reason="set CONDUCTOR_LIVE_MINIMAX=1 to run the one-call MiniMax smoke check",
)
@pytest.mark.asyncio
async def test_minimax_live_smoke_once():
    from src.llms.native_cli.minimax_mmx import MiniMaxMmxAdapter

    try:
        result = await MiniMaxMmxAdapter(timeout_seconds=120).run(
            "Reply with exactly: ok"
        )
    except Exception:
        pytest.fail("MiniMax live smoke call failed; details suppressed", pytrace=False)
    if result.text.strip() != "ok":
        pytest.fail("MiniMax did not reply with exactly ok", pytrace=False)
