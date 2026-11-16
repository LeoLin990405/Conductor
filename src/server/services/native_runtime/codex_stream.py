"""Incremental Codex JSONL → normalized native events."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Mapping

from src.llms.native_cli.base import NativeCliError, NativeCliUsage
from src.llms.native_cli.process import stream_process
from src.server.services.native_runtime.contracts import NativeEvent, chunk_text

_TOOL_ITEM_TYPES = frozenset(
    {
        "command_execution",
        "mcp_tool_call",
        "function_call",
        "collab_tool_call",
        "tool_call",
    }
)


def _tool_name(item: Mapping[str, Any]) -> str:
    return str(item.get("tool") or item.get("name") or item.get("type") or "tool")


def _tool_args(item: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("arguments", "args", "input", "prompt"):
        value = item.get(key)
        if isinstance(value, Mapping):
            return value
        if isinstance(value, str) and value:
            return {"raw": value[:4000]}
    return {}


def parse_codex_line(line: str) -> list[NativeEvent]:
    """One JSONL line to zero or more normalized events.

    Codex emits item.started/item.completed for tool calls and completed
    agent_message items; it does not stream text tokens. This gives the UI
    live tool activity and each completed message as it lands, while the final
    AIMessage remains the persisted projection.
    """
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []
    if not isinstance(event, Mapping):
        return []
    kind = event.get("type")
    if kind == "thread.started":
        sid = str(event.get("thread_id") or "") or None
        return [NativeEvent(kind="session_started", session_id=sid)]
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
    if kind not in ("item.started", "item.completed"):
        return []
    item = event.get("item")
    if not isinstance(item, Mapping):
        return []
    item_type = str(item.get("type") or "")
    item_id = str(item.get("id") or "") or None
    if item_type == "agent_message":
        if kind == "item.completed":
            text = str(item.get("text") or "")
            # The item id marks message boundaries: a Worker's completion
            # report is its last message only.
            return [
                NativeEvent(kind="text_delta", text=chunk, data={"item_id": item_id or ""})
                for chunk in chunk_text(text)
            ]
        return []
    if item_type in _TOOL_ITEM_TYPES:
        if kind == "item.started":
            return [
                NativeEvent(
                    kind="tool_started",
                    tool_name=_tool_name(item),
                    tool_call_id=item_id,
                    data={
                        "name": _tool_name(item),
                        "id": item_id,
                        "args": dict(_tool_args(item)),
                    },
                )
            ]
        status = str(item.get("status") or "completed")
        content = (
            item.get("output")
            or item.get("result")
            or item.get("text")
            or item.get("error")
            or status
        )
        return [
            NativeEvent(
                kind="tool_result",
                tool_name=_tool_name(item),
                tool_call_id=item_id,
                data={
                    "id": item_id,
                    "name": _tool_name(item),
                    "content": str(content)[:20000],
                    "status": "error" if status == "failed" else "success",
                },
            )
        ]
    return []


async def stream_codex_turn(adapter: Any, prompt: str, *, session_id: str | None, cwd: str | None) -> AsyncIterator[NativeEvent]:
    invocation = adapter.build_invocation(prompt, session_id=session_id, cwd=cwd)
    # P0: an adapter built to isolate personal memory runs under its own
    # private CODEX_HOME here too, not only through ``adapter.run``.
    from src.llms.native_cli.codex import CodexExecAdapter

    isolated_home = None
    if isinstance(adapter, CodexExecAdapter):
        invocation, isolated_home = adapter.isolated_invocation(invocation)
    active_session = session_id
    saw_event = False
    try:
        async for line in stream_process(invocation):
            for event in parse_codex_line(line):
                saw_event = True
                if event.session_id:
                    active_session = event.session_id
                yield event
    finally:
        if isolated_home is not None:
            from src.llms.native_cli.personal_memory import remove_isolated_codex_home

            remove_isolated_codex_home(isolated_home)
    if not saw_event:
        raise NativeCliError("codex returned no machine-readable events")
    yield NativeEvent(kind="turn_completed", session_id=active_session)
