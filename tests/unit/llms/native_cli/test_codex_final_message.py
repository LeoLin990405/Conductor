"""A Codex turn's completion report is its last agent message only."""

import asyncio
import json

import pytest

from src.llms.native_cli import codex
from src.llms.native_cli.base import NativeCliError, NativeCliNoFinalAnswer
from src.llms.native_cli.process import ProcessOutput


def _message(text: str, item_id: str) -> dict:
    return {
        "type": "item.completed",
        "item": {"id": item_id, "type": "agent_message", "text": text},
    }


def test_final_agent_message_skips_progress_and_other_items() -> None:
    events = [
        {"type": "thread.started", "thread_id": "t"},
        _message("Working on it.", "a"),
        {"type": "item.completed", "item": {"type": "command_execution"}},
        _message('{"status": "done"}', "b"),
        _message("   ", "c"),
        {"type": "turn.completed", "usage": {}},
    ]

    assert codex.codex_final_agent_message(events) == '{"status": "done"}'
    joined, *_ = codex.parse_codex_events("\n".join(json.dumps(e) for e in events))
    assert joined == 'Working on it.\n{"status": "done"}'


def test_final_agent_message_is_empty_without_one() -> None:
    assert codex.codex_final_agent_message([{"type": "turn.completed"}]) == ""


@pytest.mark.parametrize(
    ("kinds", "completed"),
    [
        (["thread.started", "turn.completed"], True),
        (["thread.started"], False),
        (["thread.started", "error", "turn.completed"], False),
        (["thread.started", "turn.failed"], False),
    ],
)
def test_turn_completed_needs_a_clean_completion(kinds, completed) -> None:
    assert codex.codex_turn_completed([{"type": kind} for kind in kinds]) is completed


def _adapter_with_stdout(monkeypatch, events) -> codex.CodexExecAdapter:
    stdout = "\n".join(json.dumps(event) for event in events)

    async def fake_run_process(_invocation):
        return ProcessOutput(stdout=stdout, stderr="", returncode=0)

    monkeypatch.setattr(codex, "run_process", fake_run_process)
    return codex.CodexExecAdapter(binary="/fake/codex")


def test_a_completed_turn_without_a_message_raises_no_final_answer(monkeypatch) -> None:
    adapter = _adapter_with_stdout(
        monkeypatch, [{"type": "thread.started", "thread_id": "t"}, {"type": "turn.completed"}]
    )

    with pytest.raises(NativeCliNoFinalAnswer, match="codex produced no final answer"):
        asyncio.run(adapter.run("prompt", cwd="/work"))


def test_a_failed_turn_without_a_message_keeps_the_plain_error(monkeypatch) -> None:
    adapter = _adapter_with_stdout(
        monkeypatch,
        [{"type": "thread.started", "thread_id": "t"}, {"type": "turn.failed"}],
    )

    with pytest.raises(NativeCliError, match="codex produced no final answer") as raised:
        asyncio.run(adapter.run("prompt", cwd="/work"))
    assert not isinstance(raised.value, NativeCliNoFinalAnswer)
