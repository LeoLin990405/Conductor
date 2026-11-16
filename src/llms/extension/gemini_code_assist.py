"""LangChain chat model for Gemini through Gemini Code Assist.

A Google-account OAuth token does not unlock the public Gemini API; it unlocks
Code Assist, the backend Google's own Gemini CLI talks to. That backend speaks
the Gemini ``generateContent`` shape wrapped in an envelope that names the
model, the account's managed project, and a request body — so the ChatOpenAI
classes used for API-key providers cannot reach it.

This module maps LangChain messages/tools onto that wire, both for a one-shot
call and for the SSE stream the agent runs on, and parses the wrapped response
back into ``AIMessage`` / ``AIMessageChunk`` (text, tool calls, usage).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncIterator, Iterator, List, Optional, Sequence

import httpx
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

logger = logging.getLogger(__name__)

CODE_ASSIST_URL = "https://cloudcode-pa.googleapis.com/v1internal"


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") in ("text", "input_text", "output_text"):
                    parts.append(str(block.get("text", "")))
        return "".join(parts)
    return ""


def _gemini_parts(content: Any) -> list[dict]:
    """LangChain message content → Gemini parts (text and data-URL images)."""
    if isinstance(content, str):
        return [{"text": content}] if content else []
    parts: list[dict] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                parts.append({"text": block})
            elif isinstance(block, dict):
                kind = block.get("type")
                if kind in ("text", "input_text", "output_text"):
                    parts.append({"text": str(block.get("text", ""))})
                elif kind in ("image_url", "image"):
                    url = block.get("image_url") or block.get("url") or {}
                    if isinstance(url, dict):
                        url = url.get("url", "")
                    if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
                        header, data = url.split(";base64,", 1)
                        mime = header.removeprefix("data:") or "image/png"
                        parts.append({"inlineData": {"mimeType": mime, "data": data}})
    return parts


def _convert_tools(tools: Sequence[Any]) -> list[dict]:
    declarations = []
    for tool in tools:
        converted = convert_to_openai_tool(tool)
        fn = converted.get("function") or {}
        if not fn.get("name"):
            continue
        declarations.append({
            "name": fn["name"],
            "description": fn.get("description") or "",
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return [{"functionDeclarations": declarations}] if declarations else []


def _contents(messages: Sequence[BaseMessage]) -> tuple[list[dict], str]:
    contents: list[dict] = []
    system_parts: list[str] = []
    tool_names: dict[str, str] = {}

    for message in messages:
        if isinstance(message, SystemMessage):
            text = _text_of(message.content)
            if text:
                system_parts.append(text)
            continue
        if isinstance(message, HumanMessage):
            contents.append({"role": "user", "parts": _gemini_parts(message.content)})
            continue
        if isinstance(message, AIMessage):
            parts = _gemini_parts(message.content)
            for call in (message.tool_calls or []):
                parts.append({
                    "functionCall": {
                        "name": call.get("name"),
                        "args": call.get("args") or {},
                    }
                })
                tool_names[str(call.get("id") or "")] = call.get("name") or ""
            if parts:
                contents.append({"role": "model", "parts": parts})
            continue
        if isinstance(message, ToolMessage):
            name = getattr(message, "name", None) or tool_names.get(str(message.tool_call_id) or "") or "tool"
            raw = message.content
            if isinstance(raw, str):
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = {"result": raw}
            else:
                payload = raw
            if not isinstance(payload, dict):
                payload = {"result": payload}
            contents.append({
                "role": "user",
                "parts": [{"functionResponse": {"name": name, "response": payload}}],
            })
            continue
        # Unknown message kind: send its text as a user turn rather than drop context.
        text = _text_of(message.content)
        if text:
            contents.append({"role": "user", "parts": [{"text": text}]})
    return contents, "\n\n".join(system_parts)


def _parse_response(payload: dict) -> tuple[str, list[dict], dict, str]:
    inner = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    candidates = inner.get("candidates") or []
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    finish_reason = ""
    if candidates:
        candidate = candidates[0]
        finish_reason = candidate.get("finishReason") or ""
        for part in (candidate.get("content") or {}).get("parts") or []:
            if "text" in part:
                text_parts.append(part["text"])
            elif "functionCall" in part:
                call = part["functionCall"]
                tool_calls.append({
                    "name": call.get("name"),
                    "args": call.get("args") or {},
                    "id": f"call_{uuid.uuid4().hex[:12]}",
                    "type": "tool_call",
                })
    usage = inner.get("usageMetadata") or {}
    usage_metadata = {
        "input_tokens": int(usage.get("promptTokenCount") or 0),
        "output_tokens": int(usage.get("candidatesTokenCount") or 0),
        "total_tokens": int(usage.get("totalTokenCount") or 0),
    }
    return "".join(text_parts), tool_calls, usage_metadata, finish_reason


class ChatGeminiCodeAssist(BaseChatModel):
    """Gemini over Code Assist, authenticated by a Google-account OAuth token."""

    model: str
    access_token: str
    project: str = ""
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    tools: List[dict] = Field(default_factory=list)
    profile: dict = Field(default_factory=dict)

    @property
    def _llm_type(self) -> str:
        return "gemini-code-assist"

    @property
    def _identifying_params(self) -> dict:
        return {"model": self.model, "project": self.project}

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ChatGeminiCodeAssist":
        return self.model_copy(update={"tools": _convert_tools(tools)})

    # --- request plumbing -------------------------------------------------

    def _headers(self) -> dict:
        # Antigravity's Code Assist gateway fingerprints the client; these are
        # the same public identifiers its CLI sends for OAuth inference.
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "antigravity/1.0.0",
            "X-Goog-Api-Client": "google-cloud-sdk vscode_cloudshelleditor/0.1",
            "Client-Metadata": (
                '{"ideType":"ANTIGRAVITY","platform":"PLATFORM_UNSPECIFIED",'
                '"pluginType":"GEMINI"}'
            ),
        }

    def _body(self, messages: Sequence[BaseMessage]) -> dict:
        contents, system = _contents(messages)
        request: dict[str, Any] = {"contents": contents}
        if system:
            request["systemInstruction"] = {"parts": [{"text": system}]}
        if self.tools:
            request["tools"] = self.tools
        generation: dict[str, Any] = {}
        if self.temperature is not None:
            generation["temperature"] = self.temperature
        if self.max_tokens is not None:
            generation["maxOutputTokens"] = self.max_tokens
        if generation:
            request["generationConfig"] = generation
        return {
            "model": self.model,
            "project": self.project,
            "user_prompt_id": uuid.uuid4().hex,
            "request": request,
        }

    def _to_result(self, payload: dict) -> ChatResult:
        text, tool_calls, usage, finish_reason = _parse_response(payload)
        message = AIMessage(
            content=text,
            tool_calls=tool_calls,
            usage_metadata=usage,
            response_metadata={"finish_reason": finish_reason, "model_name": self.model},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    # --- sync -------------------------------------------------------------

    def _generate(
        self,
        messages: Sequence[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        url = f"{CODE_ASSIST_URL}:generateContent"
        with httpx.Client(timeout=600.0) as client:
            response = client.post(url, json=self._body(messages), headers=self._headers())
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Gemini Code Assist error ({response.status_code}): {response.text[:400]}"
                )
            return self._to_result(response.json())

    def _stream(
        self,
        messages: Sequence[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        url = f"{CODE_ASSIST_URL}:streamGenerateContent?alt=sse"
        text_seen: list[str] = []
        tool_calls: list[dict] = []
        usage_metadata: dict = {}
        finish_reason = ""
        with httpx.Client(timeout=600.0) as client:
            with client.stream("POST", url, json=self._body(messages), headers=self._headers()) as response:
                if response.status_code >= 400:
                    raise RuntimeError(
                        f"Gemini Code Assist error ({response.status_code}): {response.read().decode()[:400]}"
                    )
                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        payload = json.loads(data)
                    except Exception:
                        continue
                    text, calls, usage, reason = _parse_response(payload)
                    finish_reason = reason or finish_reason
                    if usage.get("total_tokens"):
                        usage_metadata = usage
                    if text:
                        text_seen.append(text)
                        yield ChatGenerationChunk(message=AIMessageChunk(content=text))
                    if calls:
                        tool_calls.extend(calls)
        final = AIMessageChunk(
            content="",
            tool_calls=tool_calls,
            usage_metadata=usage_metadata or None,
            response_metadata={"finish_reason": finish_reason, "model_name": self.model},
        )
        yield ChatGenerationChunk(message=final)

    # --- async ------------------------------------------------------------

    async def _agenerate(
        self,
        messages: Sequence[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        url = f"{CODE_ASSIST_URL}:generateContent"
        async with httpx.AsyncClient(timeout=600.0) as client:
            response = await client.post(url, json=self._body(messages), headers=self._headers())
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Gemini Code Assist error ({response.status_code}): {response.text[:400]}"
                )
            return self._to_result(response.json())

    async def _astream(
        self,
        messages: Sequence[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        url = f"{CODE_ASSIST_URL}:streamGenerateContent?alt=sse"
        tool_calls: list[dict] = []
        usage_metadata: dict = {}
        finish_reason = ""
        async with httpx.AsyncClient(timeout=600.0) as client:
            async with client.stream("POST", url, json=self._body(messages), headers=self._headers()) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode()[:400]
                    raise RuntimeError(f"Gemini Code Assist error ({response.status_code}): {body}")
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        payload = json.loads(data)
                    except Exception:
                        continue
                    text, calls, usage, reason = _parse_response(payload)
                    finish_reason = reason or finish_reason
                    if usage.get("total_tokens"):
                        usage_metadata = usage
                    if text:
                        yield ChatGenerationChunk(message=AIMessageChunk(content=text))
                    if calls:
                        tool_calls.extend(calls)
        yield ChatGenerationChunk(message=AIMessageChunk(
            content="",
            tool_calls=tool_calls,
            usage_metadata=usage_metadata or None,
            response_metadata={"finish_reason": finish_reason, "model_name": self.model},
        ))
