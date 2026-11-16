"""Incremental DSH NDJSON → normalized native events."""

from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator, Mapping

from src.llms.native_cli.base import NativeCliError, NativeCliUsage
from src.llms.native_cli.process import stream_process
from src.server.services.native_runtime.contracts import NativeEvent, chunk_text

_TIMEOUT_ERROR = re.compile(r"native CLI timed out after (\d+)s\Z")


def _tool_input(event: Mapping[str, Any]) -> Mapping[str, Any]:
    value = event.get("input")
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and value:
        return {"raw": value[:4000]}
    return {}


def parse_dsh_line(line: str) -> list[NativeEvent]:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []
    if not isinstance(event, Mapping):
        return []
    kind = event.get("type")
    if kind == "session":
        return [
            NativeEvent(
                kind="session_started",
                session_id=str(event.get("sessionId") or "") or None,
            )
        ]
    if kind == "text":
        text = str(event.get("text") or "")
        return [
            NativeEvent(kind="text_delta", text=chunk) for chunk in chunk_text(text)
        ]
    if kind == "thinking":
        text = str(event.get("text") or "")
        return [
            NativeEvent(kind="thinking_delta", text=chunk) for chunk in chunk_text(text)
        ]
    if kind == "tool_call":
        call_id = str(event.get("callId") or "") or None
        return [
            NativeEvent(
                kind="tool_started",
                tool_name=str(event.get("tool") or "tool"),
                tool_call_id=call_id,
                data={
                    "id": call_id,
                    "name": str(event.get("tool") or "tool"),
                    "args": dict(_tool_input(event)),
                },
            )
        ]
    if kind == "tool_result":
        call_id = str(event.get("callId") or "") or None
        status = str(event.get("status") or "completed")
        content = event.get("result")
        return [
            NativeEvent(
                kind="tool_result",
                tool_call_id=call_id,
                data={
                    "id": call_id,
                    "content": str(content)[:20000],
                    "status": "error" if status == "failed" else "success",
                },
            )
        ]
    if kind == "status" and event.get("phase") == "step_end":
        usage = NativeCliUsage.from_mapping(event.get("usage"))
        return [
            NativeEvent(
                kind="usage",
                data={
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cache_read_tokens": usage.cache_read_tokens,
                    "cache_write_tokens": usage.cache_write_tokens,
                    "total_tokens": usage.total_tokens,
                },
            )
        ]
    return []


async def stream_dsh_turn(
    adapter: Any,
    prompt: str,
    *,
    session_id: str | None,
    cwd: str | None,
) -> AsyncIterator[NativeEvent]:
    invocation = adapter.build_invocation(prompt, session_id=session_id, cwd=cwd)
    active_session = session_id
    saw_text = False
    final_text = ""
    saw_event = False
    try:
        async for line in stream_process(invocation):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, Mapping):
                continue
            if raw.get("type") == "error":
                raise NativeCliError("dsh reported an error")
            if raw.get("type") == "final":
                final_text = str(raw.get("text") or "")
                continue
            for event in parse_dsh_line(line):
                saw_event = True
                if event.session_id:
                    active_session = event.session_id
                if event.kind == "text_delta" and event.text:
                    saw_text = True
                yield event
    except NativeCliError as exc:
        timeout = _TIMEOUT_ERROR.fullmatch(str(exc))
        if timeout:
            raise NativeCliError(
                f"dsh CLI timed out after {timeout.group(1)}s"
            ) from None
        raise NativeCliError("dsh CLI failed") from None
    if not saw_event:
        raise NativeCliError("dsh returned no machine-readable events")
    if not saw_text and final_text:
        yield NativeEvent(kind="text_delta", text=final_text)
    yield NativeEvent(kind="turn_completed", session_id=active_session)
