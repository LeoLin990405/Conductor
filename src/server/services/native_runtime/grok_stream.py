"""Incremental Grok ``--output-format streaming-json`` parser."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Mapping

from src.llms.native_cli.base import resumable_session_id
from src.llms.native_cli.process import stream_process
from src.server.services.native_runtime.contracts import NativeEvent, chunk_text


def parse_grok_stream_line(line: str) -> list[NativeEvent]:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []
    if not isinstance(event, Mapping):
        return []
    kind = str(event.get("type") or "")
    if kind == "text":
        return [
            NativeEvent(kind="text_delta", text=chunk)
            for chunk in chunk_text(str(event.get("data") or ""))
        ]
    if kind == "thought":
        return [
            NativeEvent(kind="thinking_delta", text=chunk)
            for chunk in chunk_text(str(event.get("data") or ""))
        ]
    if kind == "tool_call":
        call_id = str(event.get("toolCallId") or "") or None
        name = str(event.get("toolName") or event.get("title") or "tool")
        raw = event.get("rawInput")
        return [NativeEvent(
            kind="tool_started",
            tool_name=name,
            tool_call_id=call_id,
            data={
                "id": call_id,
                "name": name,
                "args": dict(raw) if isinstance(raw, Mapping) else {},
            },
        )]
    if kind == "tool_call_update":
        status = str(event.get("status") or "")
        if status not in {"completed", "failed", "cancelled"}:
            return []
        call_id = str(event.get("toolCallId") or "") or None
        content = event.get("content")
        return [NativeEvent(
            kind="tool_result",
            tool_call_id=call_id,
            data={
                "id": call_id,
                "content": str(content or event.get("rawOutput") or "")[:20000],
                "status": "error" if status == "failed" else "success",
            },
        )]
    if kind == "end":
        usage = event.get("usage")
        usage = usage if isinstance(usage, Mapping) else {}
        events: list[NativeEvent] = []
        if usage:
            events.append(NativeEvent(
                kind="usage",
                session_id=resumable_session_id(event.get("sessionId")),
                data={
                    "input_tokens": int(usage.get("input_tokens") or 0),
                    "output_tokens": int(usage.get("output_tokens") or 0),
                    "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
                    "cache_write_tokens": int(usage.get("cache_creation_input_tokens") or 0),
                    "total_tokens": int(usage.get("total_tokens") or 0),
                },
            ))
        events.append(NativeEvent(
            kind="turn_completed",
            session_id=resumable_session_id(event.get("sessionId")),
        ))
        return events
    return []


async def stream_grok_turn(
    adapter: Any,
    prompt: str,
    *,
    session_id: str | None,
    cwd: str | None,
) -> AsyncIterator[NativeEvent]:
    invocation = adapter.build_invocation(
        prompt, session_id=session_id, cwd=cwd, stream=True
    )
    # Only an id Grok created is carried forward; a legacy sentinel is not one.
    active_session = resumable_session_id(session_id)
    saw_completion = False
    async for line in stream_process(invocation):
        for event in parse_grok_stream_line(line):
            if event.session_id:
                active_session = event.session_id
            if event.kind == "turn_completed":
                saw_completion = True
            yield event
    if not saw_completion:
        yield NativeEvent(kind="turn_completed", session_id=active_session)
