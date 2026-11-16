"""Request-image bounds for the Kimi (Moonshot) routes.

The official adapter catalog bounds a request image at 2000x2000 px, 4.5 MiB,
JPEG quality 80. Kimi accepts base64 and file-id images only (no public URLs),
and its Messages endpoint takes base64 image blocks directly.

Like the DeepSeek offload, this rewrites the outgoing request only: the
checkpoint keeps the portable original block, so a later model switch replays it
unchanged.
"""

from __future__ import annotations

import base64
import io
from collections.abc import Awaitable, Callable

import structlog
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from PIL import Image

logger = structlog.get_logger(__name__)

KIMI_PROVIDERS = frozenset({"moonshot", "moonshot-coding", "kimi-oauth", "moonshot-cn", "minimax", "minimax-intl", "minimax-coding", "mimo", "mimo-oauth", "dashscope", "dashscope-intl", "z-ai", "z-ai-cn", "z-ai-coding", "z-ai-oauth", "z-ai-cn-oauth", "z-ai-cn-coding"})
DEFAULT_LIMITS = {
    "max_width": 2000,
    "max_height": 2000,
    "max_bytes": 4718592,
    "jpeg_quality": 80,
}


def _decode(block: object) -> bytes | None:
    if not isinstance(block, dict) or block.get("type") != "image_url":
        return None
    url = (block.get("image_url") or {}).get("url") if isinstance(block.get("image_url"), dict) else None
    if not isinstance(url, str) or ";base64," not in url:
        return None
    try:
        return base64.b64decode(url.split(";base64,", 1)[1], validate=True)
    except Exception:
        return None


def within_limits(data: bytes, limits: dict | None = None) -> bool:
    """Whether the image already fits the provider limits untouched."""
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    if len(data) > limits["max_bytes"]:
        return False
    try:
        with Image.open(io.BytesIO(data)) as image:
            return image.width <= limits["max_width"] and image.height <= limits["max_height"]
    except Exception:
        return False


def constrained_image(data: bytes, limits: dict | None = None) -> tuple[bytes, str]:
    """Resize/encode one image inside the Kimi request limits."""
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        scale = min(
            limits["max_width"] / image.width,
            limits["max_height"] / image.height,
            1.0,
        )
        if scale < 1.0:
            image = image.resize(
                (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                Image.LANCZOS,
            )
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGB")
        for quality in (limits["jpeg_quality"], 70, 60):
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality)
            encoded = buffer.getvalue()
            if len(encoded) <= limits["max_bytes"]:
                return encoded, "image/jpeg"
        return encoded, "image/jpeg"


class KimiImageResizeMiddleware(AgentMiddleware):
    """Clamp outgoing Kimi images to the adapter's documented request limits."""

    def __init__(self, *, model_name: str | None) -> None:
        super().__init__()
        self.model_name = model_name

    def _limits(self, request: ModelRequest) -> dict | None:
        route = getattr(request.model, "metadata", None)
        route = route.get("provider_route") if isinstance(route, dict) else None
        provider = route.split("@", 1)[0] if isinstance(route, str) and route else None
        if provider not in KIMI_PROVIDERS:
            provider = None
            if self.model_name:
                from src.llms.llm import LLM

                entry = LLM.get_model_config().get_model_config(self.model_name) or {}
                candidate = entry.get("provider")
                if candidate in KIMI_PROVIDERS:
                    provider = candidate
        if provider is None:
            return None
        from src.llms.llm import LLM

        return LLM.get_model_config().get_provider_info(provider).get("image_resize") or {}

    def _rewrite(self, request: ModelRequest) -> ModelRequest | None:
        limits = self._limits(request)
        if limits is None:
            return None
        changed = False
        messages = []
        for message in request.messages:
            content = getattr(message, "content", None)
            if not isinstance(content, list):
                messages.append(message)
                continue
            blocks = []
            message_changed = False
            for block in content:
                raw = _decode(block)
                if raw is None or within_limits(raw, limits):
                    blocks.append(block)
                    continue
                try:
                    encoded, media_type = constrained_image(raw, limits)
                except Exception as exc:
                    logger.info("[kimi-images] resize failed; keeping original", error=str(exc))
                    blocks.append(block)
                    continue
                blocks.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{media_type};base64,{base64.b64encode(encoded).decode()}"
                    },
                })
                changed = message_changed = True
            messages.append(
                message.model_copy(update={"content": blocks}) if message_changed else message
            )
        return request.override(messages=messages) if changed else None

    def wrap_model_call(self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]) -> ModelResponse:
        return handler(self._rewrite(request) or request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(self._rewrite(request) or request)
