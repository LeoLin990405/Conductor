"""MiMo Code CLI contracts and an explicitly opted-in live smoke call."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import pytest

from src.llms.native_cli.base import NativeCliError, NativeCliUnavailable
from src.llms.native_cli.mimo import MiMoCodeAdapter, parse_mimo_stream
from src.llms.native_cli.process import ProcessOutput


def _event(kind: str, **fields: object) -> str:
    return json.dumps({"type": kind, **fields})


def test_invocation_uses_flash_json_and_prompt_as_a_positional_argument():
    adapter = MiMoCodeAdapter(binary="/opt/mimo")

    invocation = adapter.build_invocation("Reply exactly: ok")

    assert invocation.argv[:4] == ("/opt/mimo", "run", "--format", "json")
    assert invocation.argv[invocation.argv.index("--model") + 1] == "xiaomi/mimo-v2.6-flash"
    assert invocation.argv[-2:] == ("--", "Reply exactly: ok")
    assert invocation.timeout_seconds == 120.0


def test_explicit_model_and_session_are_preserved():
    adapter = MiMoCodeAdapter(binary="/opt/mimo", model="mimo-v2.6-pro")

    invocation = adapter.build_invocation("continue", session_id="session-1")

    assert invocation.argv[invocation.argv.index("--model") + 1] == "xiaomi/mimo-v2.6-pro"
    assert invocation.argv[invocation.argv.index("--session") + 1] == "session-1"


def test_already_qualified_model_is_preserved():
    invocation = MiMoCodeAdapter(
        binary="/opt/mimo", model="custom/mimo-v2.6-flash"
    ).build_invocation("hello")

    assert invocation.argv[invocation.argv.index("--model") + 1] == "custom/mimo-v2.6-flash"


def test_parser_returns_latest_assistant_text_and_usage():
    stdout = "\n".join(
        [
            _event("text", sessionID="session-1", part={"text": "intermediate"}),
            _event("text", sessionID="session-1", part={"text": "ok"}),
            _event(
                "step_finish",
                sessionID="session-1",
                part={"tokens": {"input": 8, "output": 1, "total": 9}},
            ),
        ]
    )

    text, session_id, usage, events = parse_mimo_stream(stdout)

    assert (text, session_id) == ("ok", "session-1")
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (8, 1, 9)
    assert len(events) == 3


def test_parser_maps_authentication_error_without_echoing_provider_message():
    stdout = _event(
        "error",
        error={"data": {"message": "Invalid API Key: sensitive-detail"}},
    )

    with pytest.raises(NativeCliUnavailable, match="no usable credentials") as exc:
        parse_mimo_stream(stdout)

    assert "sensitive-detail" not in str(exc.value)


@pytest.mark.asyncio
async def test_run_sanitizes_cli_diagnostics_and_maps_auth(monkeypatch):
    async def fail(_invocation):
        raise NativeCliError("native CLI exited 1: HTTP status 401 secret-looking-detail")

    monkeypatch.setattr("src.llms.native_cli.mimo.run_process", fail)

    with pytest.raises(NativeCliUnavailable, match="no usable credentials") as exc:
        await MiMoCodeAdapter(binary="/opt/mimo").run("hello")

    assert "secret-looking-detail" not in str(exc.value)


@pytest.mark.asyncio
async def test_run_keeps_timeout_identifiable(monkeypatch):
    async def timeout(_invocation):
        raise NativeCliError("native CLI timed out after 90s")

    monkeypatch.setattr("src.llms.native_cli.mimo.run_process", timeout)

    with pytest.raises(NativeCliError, match="timed out after 90s"):
        await MiMoCodeAdapter(binary="/opt/mimo", timeout_seconds=90).run("hello")


@pytest.mark.asyncio
async def test_run_sanitizes_non_auth_process_failure(monkeypatch):
    async def fail(_invocation):
        raise NativeCliError("native CLI exited 1: private provider response")

    monkeypatch.setattr("src.llms.native_cli.mimo.run_process", fail)

    with pytest.raises(NativeCliError, match="check local CLI diagnostics") as exc:
        await MiMoCodeAdapter(binary="/opt/mimo").run("hello")

    assert "private provider response" not in str(exc.value)


@pytest.mark.asyncio
async def test_run_preserves_missing_executable_classification(monkeypatch):
    async def missing(_invocation):
        raise NativeCliUnavailable("native CLI not found: /opt/mimo")

    monkeypatch.setattr("src.llms.native_cli.mimo.run_process", missing)

    with pytest.raises(NativeCliUnavailable, match="native CLI not found"):
        await MiMoCodeAdapter(binary="/opt/mimo").run("hello")


@pytest.mark.asyncio
async def test_run_returns_parsed_result(monkeypatch):
    stdout = _event("text", sessionID="session-1", part={"text": "ok"})

    async def succeed(_invocation):
        return ProcessOutput(stdout=stdout, stderr="", returncode=0)

    monkeypatch.setattr("src.llms.native_cli.mimo.run_process", succeed)
    result = await MiMoCodeAdapter(binary="/opt/mimo").run("hello")

    assert result.provider == "mimo"
    assert result.text == "ok"
    assert result.session_id == "session-1"


@pytest.mark.asyncio
async def test_live_mimo_smoke_once_when_explicitly_enabled(tmp_path, capsys):
    if os.environ.get("CONDUCTOR_LIVE_MIMO") != "1":
        pytest.skip("set CONDUCTOR_LIVE_MIMO=1 to make the single MiMo live call")

    started = time.monotonic()
    safe_cwd = tmp_path / "empty-mimo-workspace"
    safe_cwd.mkdir()
    adapter = MiMoCodeAdapter(
        timeout_seconds=90,
        cwd=str(safe_cwd),
        model="mimo-v2.6-flash",
        pure=True,
    )
    try:
        result = await adapter.run("Reply exactly with: ok")
    except Exception as exc:  # Never let CLI diagnostics enter pytest output.
        pytest.fail(f"MiMo live smoke failed ({type(exc).__name__})", pytrace=False)

    if result.text.strip() != "ok":
        pytest.fail("MiMo live smoke returned an unexpected answer", pytrace=False)
    duration = time.monotonic() - started
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"MIMO_LIVE_SMOKE_RESULT=ok utc={stamp} duration_seconds={duration:.3f}")
    assert capsys.readouterr().out.startswith("MIMO_LIVE_SMOKE_RESULT=ok")
