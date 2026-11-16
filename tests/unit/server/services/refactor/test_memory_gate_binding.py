"""The per-user memory gate is read when a consumer calls it.

The consumers of the memory gate used to bind it when they were first
imported, so they consulted whichever version of the gate happened to be
installed at that moment. A consumer imported while the gate was patched kept
that patched value for the life of the process, and the same code then opened
or closed automatic memory depending on which module was imported first.

The gate is resolved at call time instead, so what a consumer does never
depends on import order.
"""

from __future__ import annotations

import importlib
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_the_proposal_queue_reads_the_gate_when_it_calls_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.services.refactor import memory_runtime_controls, proposals

    calls: list[str] = []

    async def gate(user_id: str) -> bool:
        calls.append(user_id)
        return False

    monkeypatch.setattr(memory_runtime_controls, "automatic_memory_enabled", gate)

    assert await proposals.automatic_memory_enabled("user-1") is False
    assert calls == ["user-1"]


@pytest.mark.asyncio
async def test_a_closed_gate_stops_the_proposal_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.services.refactor import memory_runtime_controls, proposals

    gate = AsyncMock(return_value=False)
    monkeypatch.setattr(memory_runtime_controls, "automatic_memory_enabled", gate)
    insert = AsyncMock(return_value=1)
    monkeypatch.setattr(proposals.db_proposals, "insert_proposals", insert)

    count = await proposals.project_proposals(
        user_id="user-1",
        thread_id="thread-1",
        task_run_id="run-1",
        proposals=[{"type": "user", "text": "prefers Chinese"}],
    )

    assert count == 0
    insert.assert_not_awaited()
    gate.assert_awaited_once_with("user-1")


@pytest.mark.asyncio
async def test_the_consolidation_pass_reads_the_gate_when_it_calls_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.services.refactor import (
        memory_consolidation,
        memory_runtime_controls,
    )

    calls: list[str] = []

    async def gate(user_id: str) -> bool:
        calls.append(user_id)
        return True

    monkeypatch.setattr(memory_runtime_controls, "automatic_memory_enabled", gate)

    assert await memory_consolidation.automatic_memory_enabled("user-1") is True
    assert calls == ["user-1"]


@pytest.mark.asyncio
async def test_importing_a_consumer_while_the_gate_is_patched_does_not_freeze_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reported failure: the import order decided what the gate said.

    The consumer is imported while the gate is patched, and the patch is then
    withdrawn. A consumer that binds the gate when it is imported keeps the
    patched value for good, and the gate it consults afterwards is not the
    one the application installed.
    """
    from src.server.services.refactor import memory_runtime_controls, proposals

    patched = AsyncMock(return_value=True)
    monkeypatch.setattr(memory_runtime_controls, "automatic_memory_enabled", patched)
    reloaded = importlib.reload(proposals)
    try:
        monkeypatch.setattr(
            memory_runtime_controls,
            "automatic_memory_enabled",
            AsyncMock(return_value=False),
        )
        assert await reloaded.automatic_memory_enabled("user-1") is False
    finally:
        # Leave the module as the rest of the suite expects to find it.
        monkeypatch.undo()
        importlib.reload(proposals)
