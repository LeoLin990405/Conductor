"""DeepSeek Files API transport, per the official harness adapter."""

import pytest

from src.llms.deepseek_files import (
    DEFAULT_EXPIRY_SECONDS,
    DeepSeekFileRef,
    DeepSeekFilesError,
    files_api_root,
    upload_image,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _Client:
    calls: list[dict] = []
    response = _Response(200, {
        "id": "file_123",
        "type": "file",
        "size_bytes": 24,
        "filename": "chart.png",
        "mime_type": "image/png",
    })

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, *, headers, data, files):
        self.calls.append({"url": url, "headers": headers, "data": data, "files": files})
        return type(self).response


@pytest.fixture
def fake_http(monkeypatch):
    _Client.calls = []
    _Client.response = _Response(200, {
        "id": "file_123",
        "type": "file",
        "size_bytes": len(PNG),
        "filename": "chart.png",
        "mime_type": "image/png",
    })
    monkeypatch.setattr("src.llms.deepseek_files.httpx.AsyncClient", _Client)
    return _Client


def test_the_root_appends_one_version_segment():
    assert files_api_root("https://api.deepseek.com/anthropic") == "https://api.deepseek.com/anthropic/v1"
    assert files_api_root("https://api.deepseek.com/anthropic/v1") == "https://api.deepseek.com/anthropic/v1"
    assert files_api_root("https://api.deepseek.com/anthropic/") == "https://api.deepseek.com/anthropic/v1"


@pytest.mark.asyncio
async def test_upload_sends_documented_headers_and_multipart(fake_http):
    ref = await upload_image(
        base_url="https://api.deepseek.com/anthropic",
        auth_header="x-dsh-auth-token",
        token="acct-token",
        data=PNG,
        media_type="image/png",
        filename="chart.png",
    )
    call = fake_http.calls[0]
    assert call["url"] == "https://api.deepseek.com/anthropic/v1/files"
    assert call["headers"]["x-dsh-auth-token"] == "acct-token"
    assert call["headers"]["anthropic-beta"] == "files-api-2025-04-14"
    assert call["headers"]["anthropic-version"] == "2023-06-01"
    assert call["data"]["expires_after[anchor]"] == "created_at"
    assert call["data"]["expires_after[seconds]"] == str(DEFAULT_EXPIRY_SECONDS)
    assert call["files"]["file"] == ("chart.png", PNG, "image/png")
    assert ref == DeepSeekFileRef(file_id="file_123", bytes=len(PNG), filename="chart.png")
    assert ref.image_block() == {
        "type": "image",
        "source": {"type": "file", "file_id": "file_123"},
    }


@pytest.mark.asyncio
async def test_api_key_route_uses_x_api_key(fake_http):
    await upload_image(
        base_url="https://api.deepseek.com/anthropic",
        auth_header="x-api-key",
        token="sk-key",
        data=PNG,
        media_type="image/png",
        filename="chart.png",
    )
    assert fake_http.calls[0]["headers"]["x-api-key"] == "sk-key"


@pytest.mark.asyncio
async def test_a_401_is_reported_with_its_status(fake_http):
    fake_http.response = _Response(401, {"error": {"message": "invalid token"}})
    with pytest.raises(DeepSeekFilesError) as info:
        await upload_image(
            base_url="https://api.deepseek.com/anthropic",
            auth_header="x-dsh-auth-token",
            token="dead",
            data=PNG,
            media_type="image/png",
            filename="chart.png",
        )
    assert info.value.status == 401


@pytest.mark.asyncio
async def test_an_invalid_upload_response_is_rejected(fake_http):
    fake_http.response = _Response(200, {"id": "", "type": "file"})
    with pytest.raises(DeepSeekFilesError):
        await upload_image(
            base_url="https://api.deepseek.com/anthropic",
            auth_header="x-api-key",
            token="k",
            data=PNG,
            media_type="image/png",
            filename="chart.png",
        )


@pytest.mark.asyncio
async def test_oversize_and_unsupported_media_are_refused_without_a_request(fake_http):
    with pytest.raises(DeepSeekFilesError):
        await upload_image(
            base_url="https://api.deepseek.com/anthropic",
            auth_header="x-api-key",
            token="k",
            data=b"x" * (128 * 1024 * 1024 + 1),
            media_type="image/png",
            filename="big.png",
        )
    with pytest.raises(DeepSeekFilesError):
        await upload_image(
            base_url="https://api.deepseek.com/anthropic",
            auth_header="x-api-key",
            token="k",
            data=PNG,
            media_type="application/pdf",
            filename="doc.pdf",
        )
    assert fake_http.calls == []
