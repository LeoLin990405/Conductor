"""``BYOK_ENCRYPTION_KEY`` unset: a typed error naming the setting, while the
generic 500 handler keeps its fixed body (R11) for anything that reaches it."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.server.app.error_handlers import PUBLIC_500_DETAIL, unhandled_exception_handler
from src.server.database import encryption


def test_missing_key_raises_typed_error_naming_the_setting(monkeypatch):
    monkeypatch.delenv("BYOK_ENCRYPTION_KEY", raising=False)
    with pytest.raises(encryption.EncryptionKeyMissingError) as caught:
        encryption.get_encryption_key()
    assert isinstance(caught.value, RuntimeError)  # old ``except RuntimeError`` still works
    assert "BYOK_ENCRYPTION_KEY" in str(caught.value)
    assert caught.value.code == "byok_key_missing"
    assert caught.value.setting == "BYOK_ENCRYPTION_KEY"
    assert not encryption.encryption_configured()


def test_set_key_is_returned(monkeypatch):
    monkeypatch.setenv("BYOK_ENCRYPTION_KEY", "fake-test-key")
    assert encryption.get_encryption_key() == "fake-test-key"
    assert encryption.encryption_configured()


def test_missing_key_message_holds_no_value():
    assert "BYOK_ENCRYPTION_KEY" in encryption.ENCRYPTION_KEY_MISSING_MESSAGE


def test_generic_500_handler_body_stays_fixed_for_the_typed_error(monkeypatch):
    monkeypatch.delenv("BYOK_ENCRYPTION_KEY", raising=False)
    app = FastAPI()
    app.add_exception_handler(Exception, unhandled_exception_handler)

    @app.get("/boom")
    async def boom() -> None:
        encryption.get_encryption_key()

    response = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert set(body) == {"detail", "correlation_id"}
    assert body["detail"] == PUBLIC_500_DETAIL
    assert "BYOK" not in response.text and "environment variable" not in response.text
