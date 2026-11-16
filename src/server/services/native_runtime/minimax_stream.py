"""Incremental MiniMax Code ``--output-format stream-json`` parser."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Mapping

from src.llms.native_cli.base import NativeCliError, NativeCliUsage
from src.llms.native_cli.process import stream_process
from src.server.services.native_runtime.contracts import NativeEvent, chunk_text


def _tool_payload(item: Mapping[str, Any]) -> tuple[str | None, str | None, Mapping[str, Any]]:
    tool = item.get("toolCall")
    if not isinstance(tool, Mapping):
        return None, None, {}
    call_id = str(tool.get("id") or item.get("id") or "") or None
    name = str(tool.get("name") or tool.get("tool") or "tool")
    args: Mapping[str, Any] = {}
    for key in ("input", "arguments", "args"):
        if isinstance(tool.get(key), Mapping):
            args = tool[key]
            break
    return call_id, name, args


def parse_minimax_line(line: str) -> list[NativeEvent]:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []
    if not isinstance(event, Mapping):
        return []
    kind = event.get("type")
    session_id = str(event.get("sessionId") or "") or None
    if kind in {"session.started", "session.resumed"}:
        return [NativeEvent(kind="session_started", session_id=session_id)]
    if kind == "turn.completed":
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
    if kind not in {"item.started", "item.updated", "item.completed"}:
        return []
    item = event.get("item")
    if not isinstance(item, Mapping):
        return []
    item_type = str(item.get("type") or "")
    if item_type in {"agent_message", "assistant_message"}:
        if kind != "item.completed":
            return []
        text = str(item.get("content") or item.get("text") or "")
        return [NativeEvent(kind="text_delta", text=chunk) for chunk in chunk_text(text)]
    if item_type == "reasoning":
        if kind != "item.completed":
            return []
        text = str(item.get("content") or "")
        return [NativeEvent(kind="thinking_delta", text=chunk) for chunk in chunk_text(text)]
    if item_type == "tool_call":
        call_id, name, args = _tool_payload(item)
        tool = item.get("toolCall")
        status = str(tool.get("status") if isinstance(tool, Mapping) else "") or ""
        if kind == "item.started" or status in {"started", "running", "in_progress"}:
            return [
                NativeEvent(
                    kind="tool_started",
                    tool_name=name,
                    tool_call_id=call_id,
                    data={"id": call_id, "name": name, "args": dict(args)},
                )
            ]
        if kind == "item.completed" or status in {"completed", "succeeded"}:
            tool_map = tool if isinstance(tool, Mapping) else {}
            content = tool_map.get("output") or tool_map.get("result") or status or "completed"
            return [
                NativeEvent(
                    kind="tool_result",
                    tool_name=name,
                    tool_call_id=call_id,
                    data={
                        "id": call_id,
                        "name": name,
                        "content": str(content)[:20000],
                        "status": "success",
                    },
                )
            ]
    return []


async def stream_minimax_turn(
    adapter: Any,
    prompt: str,
    *,
    session_id: str | None,
    cwd: str | None,
) -> AsyncIterator[NativeEvent]:
    effective_cwd = cwd if cwd is not None else getattr(adapter, "_cwd", None)
    write_mcp = getattr(adapter, "_write_project_mcp", None)
    restore_mcp = getattr(adapter, "_restore_project_mcp", None)
    if write_mcp is not None:
        write_mcp(effective_cwd)
    try:
        invocation = adapter.build_invocation(
            prompt, session_id=session_id, cwd=cwd, stream=True
        )
        active_session = session_id
        saw_text = False
        final_text = ""
        saw_event = False
        async for line in stream_process(invocation):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, Mapping) and raw.get("type") == "exec.completed":
                result = raw.get("result")
                if isinstance(result, Mapping):
                    final_text = str(result.get("output") or result.get("answer") or "")
                    active_session = str(result.get("sessionId") or active_session or "") or active_session
            for event in parse_minimax_line(line):
                saw_event = True
                if event.session_id:
                    active_session = event.session_id
                if event.kind == "text_delta" and event.text:
                    saw_text = True
                yield event
    finally:
        if restore_mcp is not None:
            restore_mcp()
    if not saw_event:
        raise NativeCliError("mcode returned no machine-readable events")
    if not saw_text and final_text:
        yield NativeEvent(kind="text_delta", text=final_text)
    yield NativeEvent(kind="turn_completed", session_id=active_session)
