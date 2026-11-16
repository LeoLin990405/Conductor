"""Adapt ``ScriptedFakeChatModel`` to langchain-core ``BaseChatModel``.

The turn loop talks only to ``BaseChatModel.astream`` / ``bind_tools``. The
scripted double stays behind this boundary so a production chat model can
replace it without changing the loop. Dict conversion for the scripted
backend happens here, not in the turn loop.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from pydantic import ConfigDict, Field

from prototypes.superagent.contract import ScriptedChunk, ScriptedFakeChatModel
from prototypes.superagent.p_python.messages import messages_to_scripted_dicts


class ScriptedLangChainChatModel(BaseChatModel):
    """langchain-core chat model whose backend is the contract's scripted double."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Kept off the serialized payload; the harness injects the double per test.
    scripted: ScriptedFakeChatModel = Field(exclude=True)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    @property
    def provider_name(self) -> str:
        return getattr(self.scripted, "provider_name", "scripted")

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        """Bind the role's tool definitions onto later ``astream`` calls."""

        del tool_choice
        return self.bind(tools=list(tools), **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager
        chunks = list(self._stream_scripted(messages, **kwargs))
        if not chunks:
            message: AIMessage = AIMessage(content="")
        else:
            merged = chunks[0]
            for chunk in chunks[1:]:
                merged = merged + chunk
            message = AIMessage(
                content=merged.content,
                tool_calls=list(merged.tool_calls or []),
                additional_kwargs=dict(merged.additional_kwargs or {}),
            )
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        del stop, run_manager
        yield from self._stream_scripted(messages, **kwargs)

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """One model round. Yields each scripted chunk before asking for the next."""

        del stop, run_manager
        tools = kwargs.get("tools")
        dicts = messages_to_scripted_dicts(messages)
        async for chunk in self.scripted.stream(dicts, tools=tools):
            yield ChatGenerationChunk(message=_chunk_to_ai_message(chunk))

    def _stream_scripted(
        self, messages: list[BaseMessage], **kwargs: Any
    ) -> Iterator[ChatGenerationChunk]:
        tools = kwargs.get("tools")
        dicts = messages_to_scripted_dicts(messages)
        self.scripted.open_round(dicts, tools)
        while (chunk := self.scripted.next_chunk()) is not None:
            yield ChatGenerationChunk(message=_chunk_to_ai_message(chunk))


def _chunk_to_ai_message(chunk: ScriptedChunk) -> AIMessageChunk:
    tool_calls: list[dict[str, Any]] = []
    tool_call_chunks: list[dict[str, Any]] = []
    if chunk.tool_name:
        args = dict(chunk.tool_args or {})
        # ``tool_calls`` keep a dict; ``tool_call_chunks.args`` is a JSON string.
        args_json = json.dumps(args, ensure_ascii=False, sort_keys=True)
        tool_calls.append(
            {
                "name": chunk.tool_name,
                "args": args,
                "id": chunk.tool_name,
                "type": "tool_call",
            }
        )
        tool_call_chunks.append(
            {
                "name": chunk.tool_name,
                "args": args_json,
                "id": chunk.tool_name,
                "index": 0,
                "type": "tool_call_chunk",
            }
        )
    return AIMessageChunk(
        content=chunk.text or "",
        tool_calls=tool_calls,
        tool_call_chunks=tool_call_chunks,
    )
