"""N61: the lifespan shutdown waits briefly for background judgments."""

from __future__ import annotations

import asyncio
import inspect

import pytest

from src.server.app import setup
from src.server.services.autonomy import judgment_hooks
from src.tools.auto_dispatch import completion_judgment


def test_the_lifespan_drains_judgments_after_yield() -> None:
    source = inspect.getsource(setup.lifespan)
    after_yield = source.split("yield  # Server is running", 1)[1]
    assert "await drain_judgments()" in after_yield


@pytest.mark.asyncio
async def test_drain_judgments_awaits_both_drains(monkeypatch) -> None:
    calls: list[tuple[str, float]] = []

    async def hooks_drain(timeout=None):
        calls.append(("hooks", timeout))

    async def u2_drain(timeout=5.0):
        calls.append(("u2", timeout))

    monkeypatch.setattr(judgment_hooks, "drain_pending", hooks_drain)
    monkeypatch.setattr(completion_judgment, "drain", u2_drain)
    await setup.drain_judgments(0.5)
    assert calls == [("hooks", 0.5), ("u2", 0.5)]


@pytest.mark.asyncio
async def test_drain_judgments_never_breaks_shutdown(monkeypatch) -> None:
    reached: list[str] = []

    async def broken(timeout=None):
        raise RuntimeError("drain broke")

    async def hangs(timeout=None):
        reached.append("u2")
        await asyncio.Event().wait()

    monkeypatch.setattr(judgment_hooks, "drain_pending", broken)
    monkeypatch.setattr(completion_judgment, "drain", hangs)
    await asyncio.wait_for(setup.drain_judgments(0.01), timeout=5)
    assert reached == ["u2"]


def test_judgments_drain_after_the_autonomy_scheduler_stops() -> None:
    """The watchdog and Router run in the scheduler's effect runner: drain after it."""
    after_yield = inspect.getsource(setup.lifespan).split("yield  # Server is running", 1)[1]
    assert after_yield.count("await drain_judgments()") == 1
    assert after_yield.index("await stop_autonomy_scheduler()") < after_yield.index(
        "await drain_judgments()"
    )
