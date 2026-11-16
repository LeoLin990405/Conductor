"""In-history system-prompt updates for DeepSeek's Messages route.

The official DeepSeek Harness declares ``systemPromptUpdate: in-history`` for
DeepSeek-V4.1-Flash: the endpoint reads the latest system message anywhere in
the conversation as the full effective system prompt. Keeping the *initial*
system prompt at the top level and appending later snapshots after the cached
history is what preserves the KV-cache prefix when the baseline changes
(agent.md, memory, profile). Rewriting the leading system prompt instead
invalidates it from the first token.

This middleware is wire-only. The checkpoint keeps its normal history; the
first system prompt seen per (thread, model) is remembered in-process and the
current one is appended at the end when it changes. A restart forgets the
initial snapshot and simply starts a new epoch — still correct, one cache miss.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import structlog
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

logger = structlog.get_logger(__name__)

#: The route that declared the capability. V4 Pro does not declare it.
IN_HISTORY_MODEL_ID = "deepseek-flash"
_MAX_EPOCHS = 512


def _system_text(message: SystemMessage | None) -> str:
    if message is None:
        return ""
    return "".join(
        block.get("text", "")
        for block in message.content_blocks
        if isinstance(block, dict)
    ).strip()


class DeepSeekInHistorySystemPromptMiddleware(AgentMiddleware):
    """Append changed system prompts in place instead of rewriting the prefix."""

    def __init__(self) -> None:
        super().__init__()
        self._initial: dict[tuple[str, str], str] = {}

    def _identity(self, request: ModelRequest) -> tuple[str, str] | None:
        model = getattr(request.model, "model", None)
        if not isinstance(model, str) or model != IN_HISTORY_MODEL_ID:
            return None
        route = getattr(request.model, "metadata", None)
        route = route.get("provider_route") if isinstance(route, dict) else None
        if isinstance(route, str) and not route.split("@", 1)[0].startswith("deepseek"):
            return None
        configurable = {}
        runtime = getattr(request, "runtime", None)
        config = getattr(runtime, "config", None) if runtime is not None else None
        if isinstance(config, dict):
            configurable = config.get("configurable") or {}
        thread_id = configurable.get("thread_id") if isinstance(configurable, dict) else None
        return (str(thread_id or ""), model)

    def _prepare(self, request: ModelRequest) -> ModelRequest | None:
        identity = self._identity(request)
        if identity is None:
            return None
        current = _system_text(request.system_message)
        if not current:
            return None
        initial = self._initial.get(identity)
        if initial is None:
            if len(self._initial) >= _MAX_EPOCHS:
                self._initial.pop(next(iter(self._initial)))
            self._initial[identity] = current
            return None
        if current == initial:
            return None
        messages: Sequence[Any] = request.messages
        if not messages or not isinstance(messages[-1], (HumanMessage, ToolMessage)):
            # The endpoint wants an update after a human/tool-result turn; keep
            # the ordinary top-level rewrite for any other shape.
            return None
        logger.debug(
            "[deepseek-in-history] appending changed system prompt",
            thread_id=identity[0],
        )
        return request.override(
            system_message=SystemMessage(content=initial),
            messages=[*messages, SystemMessage(content=current)],
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(self._prepare(request) or request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(self._prepare(request) or request)
