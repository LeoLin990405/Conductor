"""Outgoing image bounds for the Kimi routes."""

import base64
import io

import pytest
from langchain_core.messages import HumanMessage
from PIL import Image

from ptc_agent.agent.middleware.file_operations.kimi_images import (
    KimiImageResizeMiddleware,
)


def _data_url(width, height, color=(20, 130, 200)):
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    raw = buffer.getvalue()
    return "data:image/png;base64," + base64.b64encode(raw).decode()


class _Model:
    def __init__(self, route="moonshot"):
        self.metadata = {"provider_route": route}


class _Request:
    def __init__(self, messages, route="moonshot"):
        self.messages = messages
        self.model = _Model(route)

    def override(self, *, messages):
        return _Request(messages, self.model.metadata["provider_route"])


def _message(url):
    return HumanMessage(content=[{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": url}}])


def _block(request):
    return request.messages[0].content[1]["image_url"]["url"]


def test_a_large_image_is_clamped_to_2000px_and_jpeg():
    middleware = KimiImageResizeMiddleware(model_name=None)
    request = _Request([_message(_data_url(3200, 1600))])
    seen = {}

    def handler(req):
        seen["request"] = req
        return "ok"

    middleware.wrap_model_call(request, handler)
    url = _block(seen["request"])
    assert url.startswith("data:image/jpeg;base64,")
    raw = base64.b64decode(url.split(";base64,", 1)[1])
    with Image.open(io.BytesIO(raw)) as image:
        assert image.width <= 2000 and image.height <= 2000


def test_a_small_image_is_left_untouched():
    middleware = KimiImageResizeMiddleware(model_name=None)
    request = _Request([_message(_data_url(100, 100))])
    seen = {}

    def handler(req):
        seen["request"] = req
        return "ok"

    middleware.wrap_model_call(request, handler)
    assert seen["request"] is request


def test_other_routes_are_untouched():
    middleware = KimiImageResizeMiddleware(model_name=None)
    request = _Request([_message(_data_url(3200, 1600))], route="anthropic")
    seen = {}

    def handler(req):
        seen["request"] = req
        return "ok"

    middleware.wrap_model_call(request, handler)
    assert seen["request"] is request


@pytest.mark.asyncio
async def test_the_async_path_also_clamps():
    middleware = KimiImageResizeMiddleware(model_name=None)
    request = _Request([_message(_data_url(3000, 1000))])
    seen = {}

    async def handler(req):
        seen["request"] = req
        return "ok"

    await middleware.awrap_model_call(request, handler)
    assert _block(seen["request"]).startswith("data:image/jpeg;base64,")


def test_minimax_routes_are_covered_by_the_image_bounds():
    middleware = KimiImageResizeMiddleware(model_name=None)
    request = _Request([_message(_data_url(3200, 1600))], route="minimax")
    seen = {}

    def handler(req):
        seen["request"] = req
        return "ok"

    middleware.wrap_model_call(request, handler)
    assert _block(seen["request"]).startswith("data:image/jpeg;base64,")


def test_minimax_intl_route_is_covered_too():
    middleware = KimiImageResizeMiddleware(model_name=None)
    request = _Request([_message(_data_url(3200, 1600))], route="minimax-intl")
    seen = {}

    def handler(req):
        seen["request"] = req
        return "ok"

    middleware.wrap_model_call(request, handler)
    assert _block(seen["request"]).startswith("data:image/jpeg;base64,")


def test_mimo_routes_are_covered_by_the_image_bounds():
    middleware = KimiImageResizeMiddleware(model_name=None)
    for route in ("mimo", "mimo-oauth"):
        request = _Request([_message(_data_url(3200, 1600))], route=route)
        seen = {}

        def handler(req):
            seen["request"] = req
            return "ok"

        middleware.wrap_model_call(request, handler)
        assert _block(seen["request"]).startswith("data:image/jpeg;base64,"), route


def test_zai_routes_are_covered_by_the_image_bounds():
    middleware = KimiImageResizeMiddleware(model_name=None)
    for route in ("z-ai", "z-ai-cn", "z-ai-coding"):
        request = _Request([_message(_data_url(3200, 1600))], route=route)
        seen = {}

        def handler(req):
            seen["request"] = req
            return "ok"

        middleware.wrap_model_call(request, handler)
        assert _block(seen["request"]).startswith("data:image/jpeg;base64,"), route


def test_dashscope_routes_are_covered_by_the_image_bounds():
    middleware = KimiImageResizeMiddleware(model_name=None)
    for route in ("dashscope", "dashscope-intl"):
        request = _Request([_message(_data_url(3200, 1600))], route=route)
        seen = {}

        def handler(req):
            seen["request"] = req
            return "ok"

        middleware.wrap_model_call(request, handler)
        assert _block(seen["request"]).startswith("data:image/jpeg;base64,"), route
