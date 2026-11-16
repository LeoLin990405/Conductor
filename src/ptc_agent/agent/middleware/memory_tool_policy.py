"""Enforce the frozen memory tool-denial policy before a tool executes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from collections.abc import Iterable, Mapping
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from ptc_agent.memory_tool_policy import MemoryToolPolicySnapshot


class MemoryToolPolicyMiddleware(AgentMiddleware):
    """Refuse exact registered tools and close generic execution bypasses."""

    def __init__(
        self, snapshot: MemoryToolPolicySnapshot, tools: Iterable[Any] = ()
    ) -> None:
        self._snapshot = snapshot
        self._stamps: dict[str, dict[str, Any]] = {}
        for tool in tools:
            metadata = getattr(tool, "metadata", None)
            stamp = metadata.get("direct_mcp") if isinstance(metadata, Mapping) else None
            if isinstance(stamp, Mapping):
                self._stamps[str(getattr(tool, "name", ""))] = dict(stamp)

    def _refusal(self, request: ToolCallRequest) -> ToolMessage:
        name = str(request.tool_call.get("name") or "")
        content = (
            "Refused: memory tool policy is unavailable, so tool execution is blocked. "
            "Retry when policy access is restored."
            if self._snapshot.deny_all
            else (
                "Refused: an owner-approved memory constraint blocks this tool. "
                "Use an allowed tool or ask the owner to update the approved constraint."
            )
        )
        return ToolMessage(
            content=content,
            tool_call_id=request.tool_call.get("id"),
            name=name,
            status="error",
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        name = str(request.tool_call.get("name") or "")
        if self._snapshot.blocks(name, self._stamps.get(name)):
            return self._refusal(request)
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        name = str(request.tool_call.get("name") or "")
        if self._snapshot.blocks(name, self._stamps.get(name)):
            return self._refusal(request)
        return await handler(request)
