"""Last assistant message in Step ``--mode json`` stdout.

``parse_step_result`` and ``parse_step_text`` stay the answer parsers.
``final_message_from_json`` only reports the last assistant text, and a
JSON-mode run exposes that value without changing argv.
"""

import json

import pytest

from src.llms.native_cli.base import NativeCliResult, NativeCliUnavailable, NativeCliUsage
from src.llms.native_cli.process import ProcessOutput
from src.llms.native_cli.stepfun import (
    StepFunAdapter,
    StepFunJsonResult,
    final_message_from_json,
    parse_step_result,
    parse_step_text,
)


def _assistant(text: str, *, role: str = "assistant") -> dict:
    return {
        "role": role,
        "content": [{"type": "text", "text": text}],
    }


def test_missing_or_unreadable_stdout_returns_none():
    assert final_message_from_json("") is None
    assert final_message_from_json("  \n") is None
    assert final_message_from_json("not json") is None
    assert final_message_from_json("null") is None
    assert final_message_from_json("[]") is None
    assert final_message_from_json('{"type":"session","id":"s1"}') is None
    # Deltas are not a finished assistant message.
    assert final_message_from_json(json.dumps({
        "type": "message_update",
        "assistantMessageEvent": {"type": "text_delta", "delta": "hello"},
    })) is None
    # A result record is the answer parser's input, not an assistant message.
    assert final_message_from_json(
        '{"type":"result","result":"from-result"}'
    ) is None


def test_last_assistant_text_wins_and_skips_non_text_parts():
    earlier = {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": "Hello "},
                {"type": "toolCall", "name": "bash", "arguments": {"command": "ls"}},
                "world",
            ],
        },
    }
    tool_only = {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "content": [{"type": "toolCall", "name": "bash", "arguments": {}}],
        },
    }
    later = {
        "type": "turn_end",
        "message": {"role": "assistant", "content": "final answer"},
    }
    stdout = "\n".join(json.dumps(item) for item in (earlier, tool_only, later))

    assert final_message_from_json(stdout) == "final answer"
    assert final_message_from_json(json.dumps([earlier, tool_only])) == "Hello world"
    assert final_message_from_json(json.dumps(_assistant("only"))) == "only"


def test_agent_end_uses_the_last_assistant_in_the_transcript():
    stdout = json.dumps({
        "type": "agent_end",
        "messages": [
            {"role": "user", "content": "question"},
            _assistant("first"),
            {"role": "tool", "content": [{"type": "text", "text": "tool output"}]},
            {"role": "assistant", "content": ""},
            _assistant("second"),
        ],
    })

    assert final_message_from_json(stdout) == "second"


def test_pretty_printed_message_and_valid_ndjson():
    pretty = """
    {
      "type": "message_end",
      "message": {"role": "assistant", "text": "kept"}
    }
    """
    assert final_message_from_json(pretty) == "kept"

    stdout = "\n".join([
        '{"error":{"code":"unauthorized","message":"private"}}',
        "",
        json.dumps({"role": "assistant", "text": "kept"}),
        '{"role":"user","content":"no"}',
    ])
    assert final_message_from_json(stdout) == "kept"
    # The answer parser still classifies an explicit error object. This helper
    # does not: it never raises and never becomes that parser.
    with pytest.raises(NativeCliUnavailable):
        parse_step_text('{"error":{"code":"unauthorized","message":"private"}}')


def test_truncated_json_does_not_salvage_a_nested_assistant_line():
    truncated = "{\n\"messages\": [\n{\"role\":\"assistant\",\"content\":\"stale verdict\"}\n"
    assert final_message_from_json(truncated) is None
    mixed = "\n".join([
        "not-json",
        json.dumps({"role": "assistant", "content": "stale verdict"}),
    ])
    assert final_message_from_json(mixed) is None


def test_helper_is_deterministic_and_leaves_answer_parsers_alone():
    stdout = "\n".join([
        '{"type":"session","id":"session-1"}',
        json.dumps({
            "type": "message_end",
            "message": {"role": "assistant", "content": "from-assistant"},
        }),
        '{"type":"result","result":"from-result","usage":{"input_tokens":2}}',
    ])

    assert final_message_from_json(stdout) == final_message_from_json(stdout)
    answer, session, usage, _payload = parse_step_result(stdout)
    assert answer == "from-result"
    assert session == "session-1"
    assert usage == NativeCliUsage(input_tokens=2)
    assert parse_step_text("  concise answer\n") == "concise answer"


def test_text_invocation_argv_is_unchanged():
    invocation = StepFunAdapter(
        binary="/opt/step", timeout_seconds=37, model="step-3.7-flash"
    ).build_text_invocation("say hello", cwd="/tmp/isolated")

    assert invocation.argv == (
        "/opt/step",
        "-p",
        "--no-session",
        "--no-tools",
        "--no-extensions",
        "--no-approve",
        "--model",
        "step-3.7-flash",
        "--",
        "say hello",
    )


def test_json_invocation_argv_is_unchanged():
    invocation = StepFunAdapter(binary="/opt/step").build_invocation(
        "inspect project", session_id="prior-session"
    )

    assert invocation.argv[:3] == ("/opt/step", "--mode", "json")
    assert invocation.argv[-2:] == ("--session-id", "prior-session")
    assert "--no-session" not in invocation.argv
    assert "--no-tools" not in invocation.argv


@pytest.mark.asyncio
async def test_json_mode_run_exposes_final_message_and_keeps_the_parsed_answer(monkeypatch):
    stdout = "\n".join([
        '{"type":"session","id":"session-9"}',
        json.dumps({
            "type": "message_end",
            "message": {"role": "assistant", "content": "from-assistant"},
        }),
        '{"type":"result","result":"from-result","sessionId":"session-9",'
        '"usage":{"input_tokens":4}}',
    ])
    seen = []

    async def fake_run_process(invocation):
        seen.append(invocation)
        return ProcessOutput(stdout=stdout, stderr="", returncode=0)

    monkeypatch.setattr("src.llms.native_cli.stepfun.run_process", fake_run_process)
    result = await StepFunAdapter(binary="/opt/step").run(
        "continue", session_id="session-1"
    )

    assert isinstance(result, StepFunJsonResult)
    assert result.final_message == "from-assistant"
    assert result.final_message == final_message_from_json(stdout)
    assert result.text == "from-result"
    assert result.session_id == "session-9"
    assert result.usage == NativeCliUsage(input_tokens=4)
    assert seen[0].argv[:3] == ("/opt/step", "--mode", "json")
    assert seen[0].argv[-2:] == ("--session-id", "session-1")


@pytest.mark.asyncio
async def test_json_mode_run_exposes_none_when_the_stream_has_no_assistant(monkeypatch):
    async def fake_run_process(_invocation):
        return ProcessOutput(
            stdout='[{"type":"result","result":"continued","sessionId":"session-2"}]',
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("src.llms.native_cli.stepfun.run_process", fake_run_process)
    result = await StepFunAdapter(binary="/opt/step", read_only=False).run("edit")

    assert isinstance(result, StepFunJsonResult)
    assert result.final_message is None
    assert result.text == "continued"
    assert result.session_id == "session-2"


@pytest.mark.asyncio
async def test_text_mode_run_does_not_expose_a_final_message(monkeypatch):
    seen = []

    async def fake_run_process(invocation):
        seen.append(invocation)
        return ProcessOutput(stdout="  ok\n", stderr="", returncode=0)

    monkeypatch.setattr("src.llms.native_cli.stepfun.run_process", fake_run_process)
    result = await StepFunAdapter(binary="/opt/step").run(
        "Reply with exactly: ok", cwd="/tmp/isolated"
    )

    assert type(result) is NativeCliResult
    assert not isinstance(result, StepFunJsonResult)
    assert result.text == "ok"
    assert result.session_id is None
    assert result.usage == NativeCliUsage()
    assert seen[0].argv[:4] == ("/opt/step", "-p", "--no-session", "--no-tools")
    assert "--mode" not in seen[0].argv
