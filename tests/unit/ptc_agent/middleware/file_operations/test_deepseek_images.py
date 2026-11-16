"""Outgoing image offload for DeepSeek routes."""

import base64

import pytest
from langchain_core.messages import HumanMessage

from ptc_agent.agent.middleware.file_operations.deepseek_images import (
    DeepSeekImageFilesMiddleware,
)
from src.llms.deepseek_files import DeepSeekFileRef

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 8
DATA_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


class _Model:
    metadata = {"provider_route": "deepseek"}


class _Request:
    def __init__(self, messages, route="deepseek"):
        self.messages = messages
        self.model = _Model()
        self.model.metadata = {"provider_route": route}

    def override(self, *, messages):
        return _Request(messages, self.model.metadata["provider_route"])


class _Cache:
    enabled = False
    client = None


def _image_message():
    return HumanMessage(
        content=[{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": DATA_URL}}]
    )


@pytest.fixture
def uploaded(monkeypatch):
    calls = []

    async def fake_upload(**kwargs):
        calls.append(kwargs)
        return DeepSeekFileRef(file_id="file_9", bytes=len(kwargs["data"]), filename="x.png")

    monkeypatch.setattr(
        "ptc_agent.agent.middleware.file_operations.deepseek_images.upload_image",
        fake_upload,
    )
    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client", lambda: _Cache()
    )
    # Preparation has its own tests; the wire test pins the block shape, so
    # keep the bytes identical here.
    monkeypatch.setattr(
        "ptc_agent.agent.middleware.file_operations.deepseek_images.prepare_image",
        lambda data: (data, "image/png"),
    )
    return calls


@pytest.mark.asyncio
async def test_a_data_url_image_becomes_a_files_reference(uploaded):
    async def resolver(provider):
        assert provider == "deepseek"
        return ("x-api-key", "sk-key")

    middleware = DeepSeekImageFilesMiddleware(model_name=None, credential_resolver=resolver)
    seen = {}

    async def handler(request):
        seen["request"] = request
        return "ok"

    await middleware.awrap_model_call(_Request([_image_message()]), handler)
    block = seen["request"].messages[0].content[1]
    assert block == {"type": "image", "source": {"type": "file", "file_id": "file_9"}}
    assert uploaded[0]["auth_header"] == "x-api-key"
    assert uploaded[0]["data"] == PNG


@pytest.mark.asyncio
async def test_a_non_deepseek_route_is_left_alone(uploaded):
    async def resolver(provider):
        raise AssertionError("a non-DeepSeek route must not resolve credentials")

    middleware = DeepSeekImageFilesMiddleware(model_name=None, credential_resolver=resolver)
    request = _Request([_image_message()], route="anthropic")
    seen = {}

    async def handler(req):
        seen["request"] = req
        return "ok"

    await middleware.awrap_model_call(request, handler)
    assert seen["request"] is request
    assert uploaded == []


@pytest.mark.asyncio
async def test_an_upload_failure_keeps_the_inline_block(uploaded, monkeypatch):
    async def failing(**kwargs):
        raise RuntimeError("no files api")

    monkeypatch.setattr(
        "ptc_agent.agent.middleware.file_operations.deepseek_images.upload_image",
        failing,
    )

    async def resolver(provider):
        return ("x-api-key", "sk-key")

    middleware = DeepSeekImageFilesMiddleware(model_name=None, credential_resolver=resolver)
    seen = {}

    async def handler(req):
        seen["request"] = req
        return "ok"

    await middleware.awrap_model_call(_Request([_image_message()]), handler)
    assert seen["request"].messages[0].content[1]["type"] == "image_url"


@pytest.mark.asyncio
async def test_no_credential_means_no_rewrite(uploaded):
    async def resolver(provider):
        return None

    middleware = DeepSeekImageFilesMiddleware(model_name=None, credential_resolver=resolver)
    seen = {}

    async def handler(req):
        seen["request"] = req
        return "ok"

    request = _Request([_image_message()])
    await middleware.awrap_model_call(request, handler)
    assert seen["request"] is request
    assert uploaded == []


@pytest.mark.asyncio
async def test_the_oldest_images_are_omitted_over_the_count_budget(uploaded, monkeypatch):
    from ptc_agent.agent.middleware.file_operations import deepseek_images as mod

    monkeypatch.setattr(mod, "MAX_REQUEST_IMAGES", 1)

    async def resolver(provider):
        return ("x-api-key", "sk-key")

    middleware = DeepSeekImageFilesMiddleware(model_name=None, credential_resolver=resolver)
    messages = [_image_message(), _image_message()]
    seen = {}

    async def handler(req):
        seen["request"] = req
        return "ok"

    await middleware.awrap_model_call(_Request(messages), handler)
    first = seen["request"].messages[0].content[1]
    second = seen["request"].messages[1].content[1]
    assert first["type"] == "text" and "省略" in first["text"]
    assert second["type"] == "image" and second["source"]["type"] == "file"
    assert len(uploaded) == 1
