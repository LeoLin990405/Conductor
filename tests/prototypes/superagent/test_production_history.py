"""The production slot rebuilds store rows through the runtime's message helpers."""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, ToolMessage

from prototypes.superagent.production.adapter import _history_from_store


def _rows(*items: tuple[str, str]) -> list[dict]:
    return [{"seq": i, "kind": kind, "text": text} for i, (kind, text) in enumerate(items, 1)]


def _pairs(messages) -> tuple[list[str], list[str]]:
    declared = [c["id"] for m in messages if isinstance(m, AIMessage) for c in m.tool_calls]
    answered = [m.tool_call_id for m in messages if isinstance(m, ToolMessage)]
    return declared, answered


def test_an_orphan_tool_result_row_is_not_replayed():
    messages = _history_from_store(_rows(("user", "hi"), ("tool_result", "stray"), ("token", "ok")))
    assert not any(isinstance(m, ToolMessage) for m in messages)
    assert not any(getattr(m, "tool_call_id", None) == "tool_result" for m in messages)


def test_tool_call_rows_pair_one_to_one_with_unique_ids():
    call = json.dumps({"name": "autonomy_read", "arguments": {"ticket_id": "t-1"}})
    messages = _history_from_store(
        _rows(
            ("user", "hi"),
            ("tool_call", call),
            ("tool_result", "row"),
            ("tool_call", call),
            ("tool_refused", "autonomy_read"),
            ("tool_call", call),
            ("user", "next"),
        )
    )
    declared, answered = _pairs(messages)
    assert len(declared) == 3 and len(set(declared)) == 3
    assert "autonomy_read" not in declared
    assert sorted(declared) == sorted(answered)
    refused = [m for m in messages if isinstance(m, ToolMessage) and m.status == "error"]
    assert [m.content for m in refused][0] == "refused: autonomy_read"
