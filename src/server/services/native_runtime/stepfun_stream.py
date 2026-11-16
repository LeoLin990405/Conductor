"""Incremental StepFun ``step --mode json`` event parser."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Mapping

from src.llms.native_cli.process import stream_process
from src.server.services.native_runtime.contracts import NativeEvent, chunk_text


def _message_text(message: Any) -> str:
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        str(part.get("text") or "")
        for part in content
        if isinstance(part, Mapping) and part.get("type") == "text"
    )


def parse_stepfun_line(line: str) -> list[NativeEvent]:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []
    if not isinstance(event, Mapping):
        return []
    kind = str(event.get("type") or "")
    if kind == "session":
        session_id = str(event.get("id") or "") or None
        return [NativeEvent(kind="session_started", session_id=session_id)] if session_id else []
    if kind == "message_update":
        update = event.get("assistantMessageEvent")
        update = update if isinstance(update, Mapping) else {}
        delta = str(update.get("delta") or "")
        update_kind = str(update.get("type") or "")
        if update_kind == "text_delta" and delta:
            return [NativeEvent(kind="text_delta", text=chunk) for chunk in chunk_text(delta)]
        if update_kind == "thinking_delta" and delta:
            return [NativeEvent(kind="thinking_delta", text=chunk) for chunk in chunk_text(delta)]
        return []
    if kind == "tool_execution_start":
        call_id = str(event.get("toolCallId") or event.get("id") or "") or None
        name = str(event.get("toolName") or event.get("name") or "tool")
        args = event.get("args") or event.get("input") or {}
        return [NativeEvent(
            kind="tool_started",
            tool_name=name,
            tool_call_id=call_id,
            data={"id": call_id, "name": name, "args": dict(args) if isinstance(args, Mapping) else {}},
        )]
    if kind == "tool_execution_end":
        call_id = str(event.get("toolCallId") or event.get("id") or "") or None
        name = str(event.get("toolName") or event.get("name") or "tool")
        result = event.get("result") or event.get("output") or ""
        return [NativeEvent(
            kind="tool_result",
            tool_name=name,
            tool_call_id=call_id,
            data={"id": call_id, "name": name, "content": str(result)[:20000], "status": "success"},
        )]
    if kind in {"turn_end", "agent_end"}:
        message = event.get("message")
        if not isinstance(message, Mapping) and isinstance(event.get("messages"), list):
            assistants = [m for m in event["messages"] if isinstance(m, Mapping) and m.get("role") == "assistant"]
            message = assistants[-1] if assistants else {}
        usage = message.get("usage") if isinstance(message, Mapping) else {}
        usage = usage if isinstance(usage, Mapping) else {}
        events: list[NativeEvent] = []
        if kind == "turn_end" and usage:
            events.append(NativeEvent(kind="usage", data={
                "input_tokens": int(usage.get("input") or 0),
                "output_tokens": int(usage.get("output") or 0),
                "cache_read_tokens": int(usage.get("cacheRead") or 0),
                "cache_write_tokens": int(usage.get("cacheWrite") or 0),
                "total_tokens": int(usage.get("totalTokens") or 0),
            }))
        if kind == "agent_end":
            events.append(NativeEvent(kind="turn_completed"))
        return events
    return []


async def stream_stepfun_turn(
    adapter: Any,
    prompt: str,
    *,
    session_id: str | None,
    cwd: str | None,
) -> AsyncIterator[NativeEvent]:
    invocation = adapter.build_invocation(prompt, session_id=session_id, cwd=cwd)
    active_session = session_id
    saw_completion = False
    async for line in stream_process(invocation):
        for event in parse_stepfun_line(line):
            if event.session_id:
                active_session = event.session_id
            if event.kind == "turn_completed":
                saw_completion = True
                event = NativeEvent(kind="turn_completed", session_id=active_session)
            yield event
    if not saw_completion:
        yield NativeEvent(kind="turn_completed", session_id=active_session)
