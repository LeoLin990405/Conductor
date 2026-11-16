"""Outcome recording through the memory-p0 port, and the rate it derives."""

from __future__ import annotations

import pytest

from src.tools.auto_dispatch.outcomes import InMemoryMemoryPort, MemoryP0OutcomeStore
from src.tools.auto_dispatch.types import Outcome


def _outcome(line_id: str, model: str, passed: bool) -> Outcome:
    return Outcome(
        line_id=line_id,
        model=model,
        duration_secs=12,
        tokens=100,
        passed=passed,
        reviewer_verdict="pass" if passed else "blocked",
    )


async def test_outcomes_land_in_the_port_keyed_by_dispatch_and_line() -> None:
    port = InMemoryMemoryPort()
    store = MemoryP0OutcomeStore(port)
    await store.record("d1", {"line-0": _outcome("line-0", "codex", True)})
    row = await port.get("auto_dispatch_outcomes", "d1/line-0")
    assert row is not None
    assert row["model"] == "codex"
    assert row["passed"] is True


async def test_success_rate_is_none_until_a_model_has_a_recorded_run() -> None:
    store = MemoryP0OutcomeStore(InMemoryMemoryPort())
    assert await store.success_rate("codex") is None


async def test_success_rate_aggregates_across_dispatches() -> None:
    store = MemoryP0OutcomeStore(InMemoryMemoryPort())
    await store.record("d1", {"line-0": _outcome("line-0", "codex", True)})
    await store.record("d2", {"line-0": _outcome("line-0", "codex", False)})
    await store.record(
        "d3",
        {"line-0": _outcome("line-0", "kimi", True), "line-1": _outcome("line-1", "kimi", True)},
    )
    assert await store.success_rate("codex") == 0.5
    assert await store.success_rate("kimi") == 1.0


async def test_success_rate_reads_outcomes_already_stored_on_dispatch_rows() -> None:
    from src.tools.auto_dispatch import Dispatch, InMemoryDispatchStore
    from src.tools.auto_dispatch.outcomes import DispatchRowMemoryPort

    store = InMemoryDispatchStore()
    dispatch = Dispatch(id="d1", goal="ship", user_id="u", created_at="now")
    dispatch.outcomes = {
        "line-0": _outcome("line-0", "kimi", True),
        "line-1": _outcome("line-1", "kimi", False),
    }
    await store.create(dispatch)
    outcome_store = MemoryP0OutcomeStore(DispatchRowMemoryPort(store))

    assert await outcome_store.success_rate("kimi") == 0.5
    assert await outcome_store.success_rate("grok") is None
    # Recording the same rows again must not double-count them.
    await outcome_store.record("d1", dispatch.outcomes)
    assert await outcome_store.success_rate("kimi") == 0.5


async def test_a_recorded_rate_feeds_the_selector() -> None:
    from src.tools.auto_dispatch.selection import QuotaAwareModelSelector
    from src.tools.auto_dispatch.types import Line

    store = MemoryP0OutcomeStore(InMemoryMemoryPort())
    await store.record("d1", {"line-0": _outcome("line-0", "deepseek", True)})
    selector = QuotaAwareModelSelector(stats=store)
    assert await selector.select(Line(id="line-1", name="n", task="t"), []) == "deepseek"

pytestmark = pytest.mark.asyncio

