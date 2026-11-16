"""A-D2-1: flag-on cancellation adds a distinct cancelled signal, keeps error."""

from __future__ import annotations

import asyncio

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from ptc_agent.agent.middleware.compaction.compact import Summarizer
from ptc_agent.agent.middleware.compaction.middleware import CompactionMiddleware


def _middleware(monkeypatch) -> CompactionMiddleware:
    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(Summarizer, "compact", cancelled)
    mw = CompactionMiddleware(
        Summarizer(GenericFakeChatModel(messages=iter([AIMessage("summary")])), limit=100_000, counter=len),
        token_threshold=1,
        keep_messages=1,
        backend=None,
        workspace_id=None,
    )
    mw._transcript_target = lambda: None  # type: ignore[method-assign]
    return mw


def _request(mw: CompactionMiddleware) -> ModelRequest:
    return ModelRequest(
        model=mw._summarizer.model,
        messages=[
            HumanMessage("q1", id="h1"), AIMessage("a1", id="a1"),
            HumanMessage("q2", id="h2"), AIMessage("a2", id="a2"),
        ],
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

    with pytest.raises(asyncio.CancelledError):
        await mw.awrap_model_call(_request(mw), handler)
    return signals


@pytest.mark.asyncio
async def test_flag_on_adds_cancelled_before_error(monkeypatch):
    monkeypatch.setenv("REFACTOR_COMPACTION_EVENTS_ENABLED", "1")
    signals = await _run(_middleware(monkeypatch), [])
    kinds = [(action, signal) for action, signal, _ in signals]
    assert ("summarize", "start") in kinds
    assert ("summarize", "cancelled") in kinds
    assert ("summarize", "error") in kinds
    assert kinds.index(("summarize", "cancelled")) < kinds.index(("summarize", "error"))


@pytest.mark.asyncio
async def test_flag_off_keeps_only_error(monkeypatch):
    monkeypatch.delenv("REFACTOR_COMPACTION_EVENTS_ENABLED", raising=False)
    signals = await _run(_middleware(monkeypatch), [])
    kinds = [(action, signal) for action, signal, _ in signals]
    assert ("summarize", "cancelled") not in kinds
    assert ("summarize", "error") in kinds
