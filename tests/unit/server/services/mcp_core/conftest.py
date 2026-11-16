"""Shared fixtures for tests/unit/server/services/mcp_core."""

import pytest

from src.server.services.mcp_core.registry import clear_global_registry


@pytest.fixture(autouse=True)
def _reset_mcp_global_registry():
    """Module-level _GLOBAL_REGISTRY would otherwise leak across tests."""
    clear_global_registry()
    yield
    clear_global_registry()
