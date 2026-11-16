"""Startup binds the dispatch coordinator with the startup LLM service.

Without it, live dispatch planning raised "No LLM service is configured".
No server is started; the binding function runs against a stub app.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from src.server.app import setup
from src.server.services import dispatch as dispatch_module
from src.server.services.dispatch import (
    DispatchService,
    bind_process_coordinator,
    process_coordinator,
)


class StubLLMService:
    """Never called: these tests only check the wiring."""


@pytest.fixture(autouse=True)
def _clear_coordinator():
    previous = process_coordinator()
    previous_wakeup = dispatch_module._wakeup_service
    yield
    bind_process_coordinator(previous)
    dispatch_module._wakeup_service = previous_wakeup


def test_the_bound_coordinator_carries_the_startup_llm_service() -> None:
    llm = StubLLMService()
    app = SimpleNamespace(state=SimpleNamespace(llm_service=llm))

    coordinator = setup.bind_dispatch_coordinator(app)

    assert process_coordinator() is coordinator
    assert coordinator.llm_service is llm
    assert coordinator.service()._llm is llm


def test_a_missing_llm_service_still_binds_a_coordinator() -> None:
    app = SimpleNamespace(state=SimpleNamespace())

    coordinator = setup.bind_dispatch_coordinator(app)

    assert process_coordinator() is coordinator
    assert coordinator.llm_service is None
    assert coordinator.service()._llm is None


def test_the_lifespan_binds_through_the_wiring_function() -> None:
    source = inspect.getsource(setup)
    assert "bind_dispatch_coordinator(app)" in source
    assert "bind_process_coordinator(DispatchCoordinator())" not in source


@pytest.mark.asyncio
async def test_the_wakeup_service_carries_the_llm_service(monkeypatch) -> None:
    async def no_open(self) -> None:
        return None

    monkeypatch.setattr(DispatchService, "open", no_open)
    dispatch_module._wakeup_service = None
    llm = StubLLMService()
    setup.bind_dispatch_coordinator(SimpleNamespace(state=SimpleNamespace(llm_service=llm)))

    await dispatch_module.start_wakeup()

    assert dispatch_module._wakeup_service is not None
    assert dispatch_module._wakeup_service._llm is llm
