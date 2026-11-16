"""Outgoing-request image offload for DeepSeek routes.

DeepSeek's adapter prefers Files API references over inline base64 for images
(the official harness uploads each retained image, with inline as fallback).
Our checkpoint stores base64 image blocks because providers differ in what they
accept there; this middleware rewrites the *outgoing* request only, so a thread
that later switches models still has portable history.

Only DeepSeek's routes are touched. The upload needs the route's credential, so
the agent build injects a resolver; targets without one keep inline blocks and
behave exactly as before.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import structlog
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse

from src.llms.deepseek_files import MAX_UPLOAD_BYTES, cache_key, upload_image
from src.llms.deepseek_image_tokens import prepare_image

logger = structlog.get_logger(__name__)

DEEPSEEK_PROVIDERS = frozenset({"deepseek", "deepseek-account"})
DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"
_CACHE_TTL_SECONDS = 7 * 24 * 3600
# The official harness caps one request at 600 retained images / 128 MiB and
# drops the oldest first, leaving a model-visible placeholder in their place.
MAX_REQUEST_IMAGES = 600
MAX_REQUEST_IMAGE_BYTES = 128 * 1024 * 1024
_OMITTED_PLACEHOLDER = "[较早的图片已因本次请求的图片预算被省略]"


def _decode_data_url(block: Any) -> tuple[str, bytes] | None:
    """``(media_type, bytes)`` for a data-URL image block, else None."""
    if not isinstance(block, dict) or block.get("type") != "image_url":
        return None
    envelope = block.get("image_url")
    url = envelope.get("url") if isinstance(envelope, dict) else None
    if not isinstance(url, str) or not url.startswith("data:") or ";base64," not in url:
        return None
    header, data = url[len("data:"):].split(";base64,", 1)
    media_type = header.split(";")[0] or "image/png"
    try:
        raw = base64.b64decode(data, validate=True)
    except Exception:
        return None
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        return None
    return media_type, raw


class DeepSeekImageFilesMiddleware(AgentMiddleware):
    """Rewrite data-URL image blocks into DeepSeek Files references.

    The rewrite is per model call and never mutates the checkpoint, so a later
    model switch replays the original portable blocks.
    """

    def __init__(
        self,
        *,
        model_name: str | None,
        credential_resolver: Callable[[str], Awaitable[tuple[str, str] | None]],
    ) -> None:
        super().__init__()
        self.model_name = model_name
        self.credential_resolver = credential_resolver
        self._memory_cache: dict[str, str] = {}

    def _provider(self, request: ModelRequest) -> str | None:
        route = getattr(request.model, "metadata", None)
        route = route.get("provider_route") if isinstance(route, dict) else None
        if isinstance(route, str) and route.split("@", 1)[0] in DEEPSEEK_PROVIDERS:
            return route.split("@", 1)[0]
        if self.model_name:
            from src.llms.llm import LLM

            entry = LLM.get_model_config().get_model_config(self.model_name) or {}
            provider = entry.get("provider")
            if provider in DEEPSEEK_PROVIDERS:
                return provider
        return None

    async def _cached_file_id(
        self, auth_header: str, token: str, media_type: str, raw: bytes
    ) -> str | None:
        # Re-encode at the provider-priced dimensions first; the official
        # harness uploads exactly these bytes (JPEG ladder / WebP for alpha).
        try:
            prepared, prepared_media_type = await asyncio.to_thread(prepare_image, raw)
        except Exception as exc:
            logger.info("[deepseek-images] image preparation failed; keeping inline", error=str(exc))
            return None
        raw, media_type = prepared, prepared_media_type
        key = cache_key(DEEPSEEK_BASE_URL, auth_header, token, raw)
        if key in self._memory_cache:
            return self._memory_cache[key]
        try:
            from src.utils.cache.redis_cache import get_cache_client

            cache = get_cache_client()
            if cache.enabled and cache.client:
                hit = await cache.client.get(key)
                if hit:
                    value = hit.decode() if isinstance(hit, bytes) else str(hit)
                    self._memory_cache[key] = value
                    return value
        except Exception:
            pass
        try:
            ref = await upload_image(
                base_url=DEEPSEEK_BASE_URL,
                auth_header=auth_header,
                token=token,
                data=raw,
                media_type=media_type,
                filename=f"{hashlib.sha256(raw).hexdigest()[:12]}.png",
            )
        except Exception as exc:
            logger.info("[deepseek-images] upload failed; keeping inline block", error=str(exc))
            return None
        self._memory_cache[key] = ref.file_id
        try:
            from src.utils.cache.redis_cache import get_cache_client

            cache = get_cache_client()
            if cache.enabled and cache.client:
                await cache.client.set(key, ref.file_id, ex=_CACHE_TTL_SECONDS)
        except Exception:
            pass
        return ref.file_id

    def _over_budget_positions(self, messages: Sequence[Any]) -> set[tuple[int, int]]:
        """Oldest-first image positions to omit when the request exceeds budgets."""
        occurrences: list[tuple[int, int, int]] = []
        for message_index, message in enumerate(messages):
            content = getattr(message, "content", None)
            if not isinstance(content, list):
                continue
            for block_index, block in enumerate(content):
                decoded = _decode_data_url(block)
                if decoded is not None:
                    occurrences.append((message_index, block_index, len(decoded[1])))
        total_bytes = sum(size for _, _, size in occurrences)
        excess = max(0, len(occurrences) - MAX_REQUEST_IMAGES)
        omitted: set[tuple[int, int]] = set()
        for message_index, block_index, size in occurrences:
            if excess > 0 or total_bytes > MAX_REQUEST_IMAGE_BYTES:
                omitted.add((message_index, block_index))
                excess = max(0, excess - 1)
                total_bytes -= size
            else:
                break
        return omitted

    async def _rewrite(
        self, messages: Sequence[Any], auth_header: str, token: str
    ) -> list[Any] | None:
        changed = False
        omitted = self._over_budget_positions(messages)
        rewritten: list[Any] = []
        for message_index, message in enumerate(messages):
            content = getattr(message, "content", None)
            if not isinstance(content, list):
                rewritten.append(message)
                continue
            blocks: list[Any] = []
            for block_index, block in enumerate(content):
                if (message_index, block_index) in omitted:
                    blocks.append({"type": "text", "text": _OMITTED_PLACEHOLDER})
                    changed = True
                    continue
                decoded = _decode_data_url(block)
                if decoded is None:
                    blocks.append(block)
                    continue
                media_type, raw = decoded
                file_id = await self._cached_file_id(auth_header, token, media_type, raw)
                if file_id is None:
                    blocks.append(block)
                    continue
                blocks.append(
                    {"type": "image", "source": {"type": "file", "file_id": file_id}}
                )
                changed = True
            rewritten.append(
                message.model_copy(update={"content": blocks}) if blocks != content else message
            )
        return rewritten if changed else None

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        provider = self._provider(request)
        if provider is None:
            return await handler(request)
        credential = await self.credential_resolver(provider)
        if credential is None:
            return await handler(request)
        rewritten = await self._rewrite(request.messages, *credential)
        if rewritten is None:
            return await handler(request)
        return await handler(request.override(messages=rewritten))

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        # Uploads are async; the sync path keeps inline blocks (safe fallback).
        return handler(request)
