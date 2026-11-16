"""A-D2-1: the too-few-messages fallback can emit a blocked signal (flag on)."""

from __future__ import annotations

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from ptc_agent.agent.middleware.compaction.compact import Summarizer
from ptc_agent.agent.middleware.compaction.middleware import CompactionMiddleware


def _middleware() -> CompactionMiddleware:
    mw = CompactionMiddleware(
        Summarizer(GenericFakeChatModel(messages=iter([AIMessage("summary")])), limit=100_000, counter=len),
        token_threshold=1,
        keep_messages=100,
        backend=None,
        workspace_id=None,
    )
    mw._transcript_target = lambda: None  # type: ignore[method-assign]
    return mw


def _request(mw: CompactionMiddleware) -> ModelRequest:
    return ModelRequest(
        model=mw._summarizer.model,
        messages=[HumanMessage("q", id="h1"), AIMessage("a", id="a1")],
        system_message=None,
        tool_choice=None,
        tools=[],
        response_format=None,
        state={},
        runtime=None,
    )


async def _run(mw: CompactionMiddleware, signals: list[tuple[str, str, dict]]):
    mw._emit_context_signal = lambda action, signal, **kw: signals.append((action, signal, kw))  # type: ignore[method-assign]

    async def handler(req):
        return ModelResponse(result=[AIMessage("done")])

    await mw.awrap_model_call(_request(mw), handler)
    return signals


@pytest.mark.asyncio
async def test_flag_on_emits_blocked_and_keeps_the_fallback(monkeypatch):
    monkeypatch.setenv("REFACTOR_COMPACTION_EVENTS_ENABLED", "1")
    signals = await _run(_middleware(), [])
    assert signals
    action, signal, payload = signals[-1]
    assert (action, signal) == ("summarize", "blocked")
    assert payload["reason"] == "too_few_messages"
    assert payload["token_count"] >= 1
    assert "prompt" not in payload


@pytest.mark.asyncio
async def test_flag_off_emits_nothing(monkeypatch):
    monkeypatch.delenv("REFACTOR_COMPACTION_EVENTS_ENABLED", raising=False)
    signals = await _run(_middleware(), [])
    assert signals == []
