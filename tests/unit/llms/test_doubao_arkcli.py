from __future__ import annotations

import json
import os

import pytest

from src.llms.native_cli.base import NativeCliError
from src.llms.native_cli.process import ProcessOutput


def test_doubao_builds_text_chat_invocation():
    from src.llms.native_cli.doubao import DoubaoArkCliAdapter

    invocation = DoubaoArkCliAdapter(
        binary="/opt/arkcli", model="doubao-seed-2-1-lite", store=True
    ).build_invocation("line one\nline two")

    assert invocation.argv == (
        "/opt/arkcli",
        "+chat",
        "--format",
        "json",
        "--no-progress",
        "--model",
        "doubao-seed-2-1-lite",
        "--store",
        "--",
        "line one\nline two",
    )
    assert invocation.env == {
        "ARKCLI_NO_UPDATE_NOTIFIER": "1",
        "ARKCLI_CALLER_TYPE": "ai_agent",
        "ARKCLI_CALLER_NAME": "codex",
        "ARKCLI_SKILL_NAME": "arkcli-chat",
    }
    assert invocation.timeout_seconds == 120.0


def test_doubao_parser_reads_flattened_content_and_usage():
    from src.llms.native_cli.doubao import parse_doubao_result

    text, response_id, usage, raw = parse_doubao_result(
        json.dumps(
            {
                "id": "resp-123",
                "content": "answer",
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 2,
                    "total_tokens": 5,
                },
            }
        )
    )

    assert text == "answer"
    assert response_id == "resp-123"
    assert usage.total_tokens == 5
    assert raw["id"] == "resp-123"


@pytest.mark.asyncio
async def test_doubao_run_uses_runner_and_keeps_session_only_when_stored(monkeypatch):
    from src.llms.native_cli.doubao import DoubaoArkCliAdapter

    captured = []

    async def fake_run_process(invocation):
        captured.append(invocation)
        return ProcessOutput(
            stdout=json.dumps({"id": "resp-123", "content": "answer"}),
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("src.llms.native_cli.doubao.run_process", fake_run_process)
    result = await DoubaoArkCliAdapter(binary="/opt/arkcli", store=True).run("hello")

    assert result.provider == "doubao"
    assert result.text == "answer"
    assert result.session_id == "resp-123"
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_doubao_run_sanitizes_process_diagnostics(monkeypatch):
    from src.llms.native_cli.doubao import DoubaoArkCliAdapter

    async def fake_run_process(_invocation):
        raise NativeCliError("provider error with private-token")

    monkeypatch.setattr("src.llms.native_cli.doubao.run_process", fake_run_process)
    with pytest.raises(NativeCliError, match="check local CLI diagnostics") as exc_info:
        await DoubaoArkCliAdapter(binary="/opt/arkcli").run("hello")
    assert "private-token" not in str(exc_info.value)


@pytest.mark.skipif(
    os.environ.get("CONDUCTOR_LIVE_DOUBAO") != "1",
    reason="set CONDUCTOR_LIVE_DOUBAO=1 to run the one-call ArkCLI smoke check",
)
@pytest.mark.asyncio
async def test_doubao_live_smoke_once():
    from src.llms.native_cli.doubao import DoubaoArkCliAdapter

    try:
        result = await DoubaoArkCliAdapter(timeout_seconds=120).run(
            "Reply with exactly: ok"
        )
    except Exception:
        pytest.fail("Doubao live smoke call failed; details suppressed", pytrace=False)
    if result.text.strip() != "ok":
        pytest.fail("Doubao did not reply with exactly ok", pytrace=False)
