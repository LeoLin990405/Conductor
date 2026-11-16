"""Role tool definitions and fake-handler dispatch for the Python loop."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from langchain_core.messages import AIMessageChunk, BaseMessage

from prototypes.superagent.contract import (
    FAKE_TOOL_HANDLERS,
    autonomy_tool_names,
    is_shell_or_file_tool,
    scripted_tool_call_text,
)
from prototypes.superagent.p_python.messages import append_tool_exchange


def offered_tool_definitions(role: str) -> list[dict[str, str]]:
    """Anthropic-shaped tool defs offered on every model request for ``role``."""

    return [{"name": name} for name in autonomy_tool_names(role)]


def tool_request_from_chunk(message: AIMessageChunk) -> tuple[str, dict[str, Any]] | None:
    """Pull the first tool request off a langchain stream chunk, if any."""

    for call in message.tool_calls or []:
        name = str(call.get("name") or "")
        if not name:
            continue
        args = call.get("args") or {}
        if isinstance(args, str):
            args = json.loads(args) if args.strip() else {}
        return name, dict(args)
    for call in message.tool_call_chunks or []:
        name = str(call.get("name") or "")
        if not name:
            continue
        args = call.get("args") or {}
        if isinstance(args, str):
            args = json.loads(args) if args.strip() else {}
        return name, dict(args)
    return None


def handle_tool_request(
    name: str,
    arguments: Mapping[str, Any],
    *,
    role: str,
    messages: list[BaseMessage],
    emit: Callable[..., None],
) -> None:
    """Refuse shell/file/out-of-role tools; otherwise run ``FAKE_TOOL_HANDLERS``."""

    allowed = set(autonomy_tool_names(role))
    if is_shell_or_file_tool(name) or name not in allowed:
        emit("tool_refused", name)
        append_tool_exchange(
            messages,
            name=name,
            call_text=None,
            result_text=f"refused:{name}",
            refused=True,
        )
        return
    handler = FAKE_TOOL_HANDLERS.get(name)
    if handler is None:
        raise RuntimeError(f"no fake handler for {name}")
    call_text = scripted_tool_call_text(name, arguments)
    result = handler(arguments)
    emit("tool_call", call_text)
    emit("tool_result", result)
    append_tool_exchange(
        messages,
        name=name,
        call_text=call_text,
        result_text=result,
        refused=False,
    )
