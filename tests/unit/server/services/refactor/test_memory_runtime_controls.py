from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.server.services import features
from src.server.services.refactor import memory_runtime_controls


@pytest.mark.asyncio
async def test_memory_control_fails_closed_for_unknown_feature(monkeypatch) -> None:
    monkeypatch.setattr(memory_runtime_controls, "MEMORY_FEATURE_KEY", "missing")
    enabled = AsyncMock(return_value=False)
    monkeypatch.setattr(features, "user_feature_enabled", enabled)

    assert await memory_runtime_controls.automatic_memory_enabled("user-1") is False
    enabled.assert_awaited_once_with("user-1", "missing")


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [True, False])
async def test_memory_control_uses_user_override(monkeypatch, flag) -> None:
    enabled = AsyncMock(return_value=flag)
    monkeypatch.setattr(features, "user_feature_enabled", enabled)

    assert await memory_runtime_controls.automatic_memory_enabled("user-1") is flag
    enabled.assert_awaited_once_with("user-1", "memory")


@pytest.mark.asyncio
async def test_memory_control_fails_closed_for_missing_owner(monkeypatch) -> None:
    enabled = AsyncMock()
    monkeypatch.setattr(features, "user_feature_enabled", enabled)

    assert await memory_runtime_controls.automatic_memory_enabled(" ") is False
    enabled.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_control_fails_closed_when_override_lookup_fails(monkeypatch) -> None:
    enabled = AsyncMock(side_effect=RuntimeError("preference read failed"))
    monkeypatch.setattr(features, "user_feature_enabled", enabled)

    assert await memory_runtime_controls.automatic_memory_enabled("user-1") is False
    enabled.assert_awaited_once_with("user-1", "memory")
