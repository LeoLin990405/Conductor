"""Incremental ZCode ``--output-format stream-json`` parser.

ZCode's headless stream is a sequence of newline-delimited session events. It
is the supported liveness surface for the standalone CLI; the app-server
protocol is separate and its account-model catalog is not materialized in the
standalone process.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, AsyncIterator, Mapping

from src.llms.native_cli.process import stream_process
from src.llms.native_cli.zcode import ZCodeTurnScope
from src.server.services.native_runtime.contracts import NativeEvent, chunk_text


def parse_zcode_stream_line(line: str) -> list[NativeEvent]:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []
    if not isinstance(event, Mapping):
        return []
    kind = str(event.get("type") or "")
    payload = event.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    session_id = str(event.get("sessionId") or "") or None

    if kind == "model.streaming":
        stream_kind = str(payload.get("kind") or "")
        text = str(payload.get("delta") or "")
        if stream_kind == "text_delta" and text:
            return [
                NativeEvent(kind="text_delta", text=chunk, session_id=session_id)
                for chunk in chunk_text(text)
            ]
        if stream_kind == "reasoning_delta" and text:
            return [
                NativeEvent(kind="thinking_delta", text=chunk, session_id=session_id)
                for chunk in chunk_text(text)
            ]
        if stream_kind in {"tool_input_start", "tool_call"}:
            call_id = str(payload.get("toolCallId") or "") or None
            name = str(payload.get("toolName") or "tool")
            args = payload.get("input")
            return [
                NativeEvent(
                    kind="tool_started",
                    session_id=session_id,
                    tool_name=name,
                    tool_call_id=call_id,
                    data={
                        "id": call_id,
                        "name": name,
                        "args": dict(args) if isinstance(args, Mapping) else {},
                    },
                )
            ]
        return []

    if kind == "tool.updated":
        update_kind = str(payload.get("kind") or "")
        call_id = str(payload.get("toolCallId") or "") or None
        name = str(payload.get("toolName") or "tool")
        if update_kind == "started":
            args = payload.get("input")
            return [
                NativeEvent(
                    kind="tool_started",
                    session_id=session_id,
                    tool_name=name,
                    tool_call_id=call_id,
                    data={
                        "id": call_id,
                        "name": name,
                        "args": dict(args) if isinstance(args, Mapping) else {},
                    },
                )
            ]
        if update_kind == "result":
            result = payload.get("result")
            result = result if isinstance(result, Mapping) else {}
            content = str(result.get("content") or "")
            return [
                NativeEvent(
                    kind="tool_result",
                    session_id=session_id,
                    tool_name=name,
                    tool_call_id=call_id,
                    data={
                        "id": call_id,
                        "name": name,
                        "content": content[:20000],
                        "status": "success" if result.get("success", True) else "error",
                    },
                )
            ]
        return []

    if kind == "permission.requested":
        request_id = str(payload.get("requestId") or "")
        tool_name = str(payload.get("toolName") or "tool")
        return [
            NativeEvent(
                kind="approval_requested",
                session_id=session_id,
                tool_name=tool_name,
                text=str(payload.get("reason") or "ZCode is waiting for approval"),
                data={
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "input": payload.get("input") or {},
                    "reason": payload.get("reason") or "",
                    "risk_level": payload.get("riskLevel") or "",
                    "options": payload.get("options") or [],
                },
            )
        ]

    if kind == "permission.resolved":
        return []

    if kind == "turn.completed":
        usage = payload.get("usage")
        usage = usage if isinstance(usage, Mapping) else {}
        result_type = str(payload.get("resultType") or "")
        events: list[NativeEvent] = []
        if usage:
            events.append(
                NativeEvent(
                    kind="usage",
                    session_id=session_id,
                    data={
                        "input_tokens": int(usage.get("inputTokens") or 0),
                        "output_tokens": int(usage.get("outputTokens") or 0),
                        "cache_read_tokens": int(usage.get("cacheReadTokens") or 0),
                        "cache_write_tokens": int(usage.get("cacheWriteTokens") or 0),
                        "total_tokens": int(usage.get("totalTokens") or 0),
                    },
                )
            )
        if result_type and result_type not in {"success", "cancelled"}:
            events.append(
                NativeEvent(
                    kind="error",
                    session_id=session_id,
                    text=str(payload.get("response") or result_type),
                )
            )
            return events
        events.append(NativeEvent(kind="turn_completed", session_id=session_id))
        return events

    return []


def _unguarded(adapter: Any):
    """A turn scope for an adapter that has no shared config files to hold."""

    @asynccontextmanager
    async def _context(prompt, *, session_id, cwd, stream, started_at=None):
        timeout = float(getattr(adapter, "timeout_seconds", 300.0))
        began = time.monotonic() if started_at is None else started_at
        yield ZCodeTurnScope(
            invocation=adapter.build_invocation(
                prompt, session_id=session_id, cwd=cwd, stream=stream
            ),
            deadline=began + timeout,
            timeout_seconds=timeout,
        )

    return _context


async def stream_zcode_turn(
    adapter: Any,
    prompt: str,
    *,
    session_id: str | None,
    cwd: str | None,
) -> AsyncIterator[NativeEvent]:
    started = time.monotonic()
    scope = getattr(adapter, "turn_scope", None)
    if scope is None:
        # Adapters without shared config files have nothing to hold.
        scope = _unguarded(adapter)
    # One budget covers the wait for the provider lock and the run itself.
    # The scope owns the deadline, so a queued turn cannot take twice the
    # timeout it was given by spending one budget on the lock and another
    # on the process.
    async with scope(
        prompt, session_id=session_id, cwd=cwd, stream=True, started_at=started
    ) as turn:
        invocation = replace(turn.invocation, timeout_seconds=turn.remaining())
        saw_completion = False
        active_session = session_id
        async for line in stream_process(invocation):
            for event in parse_zcode_stream_line(line):
                if event.session_id:
                    active_session = event.session_id
                if event.kind == "turn_completed":
                    saw_completion = True
                    if event.session_id is None and active_session:
                        event = NativeEvent(
                            kind="turn_completed", session_id=active_session
                        )
                yield event
        if not saw_completion:
            yield NativeEvent(
                kind="turn_completed", session_id=active_session
            )
