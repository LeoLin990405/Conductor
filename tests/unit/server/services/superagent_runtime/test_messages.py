"""Regression coverage for superagent_runtime.messages blockers."""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.server.services.superagent_runtime.messages import (
    build_model_messages,
    build_system_message,
    content_text,
    iter_content_deltas,
    iter_message_deltas,
    memory_block,
    prepare_history,
    trim_history,
)


def test_memory_is_system_prompt_section_before_user() -> None:
    """PRD §4.5.2: recalled memory is a system section, not a HumanMessage."""

    memory = "user prefers concise replies"
    messages = build_model_messages(
        system_prompt="You are the Router.",
        tool_names=["autonomy_route", "autonomy_reply"],
        memory_text=memory,
        history=[HumanMessage(content="earlier")],
        user_text="route this",
        history_turns=4,
        history_chars=48_000,
    )

    assert isinstance(messages[0], SystemMessage)
    system = messages[0].content
    assert isinstance(system, str)
    assert system.startswith("You are the Router.")
    assert "tools=autonomy_route,autonomy_reply" in system
    assert "## Memory\nuser prefers concise replies" in system
    # Prompt, then tools, then memory — memory sits in the system message.
    assert system.index("You are the Router.") < system.index("tools=")
    assert system.index("tools=") < system.index("## Memory")
    # No separate memory HumanMessage; only history + current user follow.
    assert not any(
        isinstance(message, HumanMessage)
        and (message.additional_kwargs or {}).get("role") == "memory"
        for message in messages
    )
    assert [type(message).__name__ for message in messages] == [
        "SystemMessage",
        "HumanMessage",
        "HumanMessage",
    ]
    assert messages[1].content == "earlier"
    assert messages[-1].content == "route this"


def test_empty_memory_omits_system_section() -> None:
    assert memory_block("") == ""
    assert memory_block("   ") == ""
    system = build_system_message("prompt", ["alpha"], memory_text="")
    assert "## Memory" not in system.content
    assert "tools=alpha" in system.content


def test_iter_content_deltas_keeps_repeated_identical_blocks() -> None:
    blocks = [
        {"type": "text", "text": "ha"},
        {"type": "text", "text": "ha"},
        {"type": "thinking", "thinking": "mm"},
        {"type": "thinking", "thinking": "mm"},
    ]
    assert list(iter_content_deltas(blocks)) == [
        ("text", "ha"),
        ("text", "ha"),
        ("thinking", "mm"),
        ("thinking", "mm"),
    ]
    assert content_text(blocks) == "haha"


def test_iter_message_deltas_preserves_repeated_content_blocks() -> None:
    message = AIMessage(
        content=[
            {"type": "text", "text": "ha"},
            {"type": "text", "text": "ha"},
        ]
    )
    assert "".join(text for kind, text in iter_message_deltas(message) if kind == "text") == (
        "haha"
    )


def test_iter_message_deltas_parses_structured_reasoning_kwargs() -> None:
    summary = "chain of thought"
    message = SimpleNamespace(
        content="",
        content_blocks=None,
        additional_kwargs={
            "reasoning_content": {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": summary}],
            }
        },
    )
    deltas = list(iter_message_deltas(message))
    assert deltas == [("thinking", summary)]
    assert not any("{" in text or "summary" in text for _, text in deltas)


def test_iter_message_deltas_skips_kwargs_duplicate_of_content_summary() -> None:
    summary = "already in content"
    message = AIMessage(
        content=[{"type": "reasoning", "summary": [{"text": summary}]}],
        additional_kwargs={
            "reasoning_content": {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": summary}],
            }
        },
    )
    assert list(iter_message_deltas(message)) == [("thinking", summary)]


def test_strip_redacted_thinking_and_trim_counts_retained_payload() -> None:
    payload = "x" * 50_000
    history = [
        HumanMessage(content="hi"),
        AIMessage(
            content=[
                {"type": "redacted_thinking", "data": payload},
                {"type": "text", "text": "ok"},
            ]
        ),
    ]
    prepared = prepare_history(history)
    assert isinstance(prepared[1], AIMessage)
    assert prepared[1].content == [{"type": "text", "text": "ok"}]
    assert payload not in str(prepared[1].content)

    trimmed = trim_history(prepared, history_turns=4, history_chars=100)
    assert trimmed == prepared
    # Budget is enforced against retained bytes: a leftover redacted payload
    # must not survive prepare_history, and a huge answer text must be dropped.
    oversized = [
        HumanMessage(content="q"),
        AIMessage(content="y" * 50_000),
    ]
    assert trim_history(oversized, history_turns=4, history_chars=100) == []


def test_trim_history_counts_unstripped_thinking_payload() -> None:
    """If reasoning remains on a message, history_chars still sees its length."""

    history = [
        HumanMessage(content="q"),
        AIMessage(
            content=[
                {"type": "redacted_thinking", "data": "z" * 50_000},
                {"type": "text", "text": "ok"},
            ]
        ),
    ]
    # Bypass prepare_history to assert the char counter itself.
    assert trim_history(history, history_turns=4, history_chars=100) == []


def test_strip_reasoning_removes_redacted_thinking_blocks() -> None:
    message = AIMessage(
        content=[
            {"type": "redacted_thinking", "data": "secret" * 100},
            {"type": "thinking", "thinking": "plan"},
            {"type": "text", "text": "done"},
        ],
        additional_kwargs={"reasoning_content": "hidden", "keep": 1},
    )
    (stripped,) = prepare_history([message])
    assert stripped.content == [{"type": "text", "text": "done"}]
    assert stripped.additional_kwargs == {"keep": 1}


def test_repair_tool_pairs_answers_lost_calls_and_drops_orphans() -> None:
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from src.server.services.superagent_runtime.messages import (
        INTERRUPTED_TOOL_RESULT,
        prepare_history,
    )

    history = [
        ToolMessage(content="orphan-first", tool_call_id="nobody", name="x"),
        HumanMessage(content="q"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "a", "args": {}, "id": "c1", "type": "tool_call"},
                {"name": "b", "args": {}, "id": "c2", "type": "tool_call"},
            ],
        ),
        ToolMessage(content="ok", tool_call_id="c2", name="b"),
        ToolMessage(content="dup", tool_call_id="c2", name="b"),
        HumanMessage(content="next"),
    ]
    out = prepare_history(history)
    assert [type(m).__name__ for m in out] == [
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "ToolMessage",
        "HumanMessage",
    ]
    assert out[2].tool_call_id == "c2" and out[2].content == "ok"
    assert (out[3].tool_call_id, out[3].status, out[3].content) == (
        "c1",
        "error",
        INTERRUPTED_TOOL_RESULT,
    )


def test_thinking_only_assistant_message_is_dropped_not_sent_empty() -> None:
    history = [
        HumanMessage(content="q"),
        AIMessage(content=[{"type": "thinking", "thinking": "hmm"}]),
        AIMessage(content="", additional_kwargs={"reasoning_content": "hmm"}),
        AIMessage(content="answer"),
    ]
    out = prepare_history(history)
    assert [m.content for m in out] == ["q", "answer"]


def test_invalid_tool_call_keeps_its_patched_tool_message() -> None:
    # PTC's PatchToolCallsMiddleware answers an invalid_tool_call with a
    # ToolMessage; dropping it leaves a call with no result (provider 400).
    from langchain_core.messages import ToolMessage

    history = [
        HumanMessage(content="hi"),
        AIMessage(
            content="",
            invalid_tool_calls=[
                {"name": "f", "args": "{bad", "id": "i1", "error": "x", "type": "invalid_tool_call"}
            ],
        ),
        ToolMessage(content="error", tool_call_id="i1", name="f", status="error"),
        AIMessage(content="ok"),
    ]
    out = prepare_history(history)
    assert [type(m).__name__ for m in out] == ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage"]
    assert out[1].invalid_tool_calls[0]["id"] == "i1"
    assert out[2].tool_call_id == "i1" and out[2].content == "error"


def test_unanswered_invalid_tool_call_gets_an_error_result() -> None:
    history = [
        HumanMessage(content="hi"),
        AIMessage(
            content="",
            invalid_tool_calls=[
                {"name": "f", "args": "{bad", "id": "i2", "error": "x", "type": "invalid_tool_call"}
            ],
        ),
        HumanMessage(content="next"),
    ]
    out = prepare_history(history)
    assert out[2].tool_call_id == "i2" and out[2].status == "error"
