"""Completed vendor messages are delivered as explicit progressive chunks."""

from __future__ import annotations

import json

from src.server.services.native_runtime.codex_stream import parse_codex_line
from src.server.services.native_runtime.contracts import chunk_text


def test_chunk_text_is_lossless_and_bounded():
    text = "x" * 75
    chunks = chunk_text(text, 32)
    assert "".join(chunks) == text
    assert [len(chunk) for chunk in chunks] == [32, 32, 11]


def test_codex_completed_message_becomes_multiple_text_deltas():
    events = parse_codex_line(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "i1",
                    "type": "agent_message",
                    "text": "A" * 40,
                },
            }
        )
    )
    assert [event.kind for event in events] == ["text_delta", "text_delta"]
    assert "".join(event.text for event in events) == "A" * 40


def test_empty_message_yields_no_event():
    assert parse_codex_line(
        json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": ""}}
        )
    ) == []
