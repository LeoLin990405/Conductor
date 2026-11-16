"""R6 L4: the selector's soft pick is shadow by default and enforced on request."""

from __future__ import annotations

import random

import pytest

from src.server.services.autonomy.roles import ROLE_POLICY_ENV
from src.tools.auto_dispatch.outcomes import InMemoryMemoryPort, MemoryP0OutcomeStore
from src.tools.auto_dispatch.pool import eligible_models
from src.tools.auto_dispatch.selection import QuotaAwareModelSelector
from src.tools.auto_dispatch.soft_select import SELECTOR_MODE_ENV
from src.tools.auto_dispatch.types import Line, Outcome


pytestmark = pytest.mark.asyncio

SPECIALIST = "stepfun"
LINE = Line(id="line-0", name="web", task="t", write_scope=("web/src/App.tsx",))


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ROLE_POLICY_ENV, raising=False)
    monkeypatch.delenv(SELECTOR_MODE_ENV, raising=False)


async def _stats() -> MemoryP0OutcomeStore:
    store = MemoryP0OutcomeStore(InMemoryMemoryPort())
    roster = eligible_models("work")
    rows = {}
    # stepfun is excellent on web; everyone else fails there.
    for i in range(20):
        for model in roster:
            rows[f"{model}-{i}"] = Outcome(
                f"{model}-{i}", model, 1, 0, model == SPECIALIST, category="web"
            )
    await store.record("d1", rows)
    return store


async def test_shadow_records_soft_pick_but_returns_main_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SELECTOR_MODE_ENV, "off")
    expected = [await QuotaAwareModelSelector(stats=await _stats()).select(LINE, []) for _ in "ab"]
    monkeypatch.setenv(SELECTOR_MODE_ENV, "shadow")
    selector = QuotaAwareModelSelector(stats=await _stats(), rng=random.Random(3))
    chosen = await selector.select(LINE, [])
    assert chosen == expected[0]
    record = selector.last_soft_choice
    assert record is not None and record["mode"] == "shadow"
    assert record["ordered_choice"] == expected[0]
    assert record["category"] == "web"


async def test_off_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SELECTOR_MODE_ENV, "off")
    selector = QuotaAwareModelSelector(stats=await _stats())
    await selector.select(LINE, [])
    assert selector.last_soft_choice is None and selector.soft_audit == []


async def test_enforce_follows_category_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SELECTOR_MODE_ENV, "enforce")
    selector = QuotaAwareModelSelector(stats=await _stats(), rng=random.Random(11))
    picks = []
    for _ in range(10):
        picks.append(await selector.select(LINE, []))
    # The web specialist dominates the sample (fails on old code: round robin).
    assert picks.count(SPECIALIST) >= 7
    assert len(selector.soft_audit) == 10


async def test_enforce_never_returns_an_excluded_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SELECTOR_MODE_ENV, "enforce")
    selector = QuotaAwareModelSelector(stats=await _stats(), rng=random.Random(5))
    for _ in range(10):
        assert await selector.select(LINE, [], exclude=[SPECIALIST]) != SPECIALIST


async def test_unset_mode_without_a_registry_reads_no_stats() -> None:
    """Review fix 3: an empty registry selects exactly as main, no stats read."""

    class _Counting(MemoryP0OutcomeStore):
        reads = 0

        async def category_table(self, category=None):
            type(self).reads += 1
            return await super().category_table(category)

    store = _Counting(InMemoryMemoryPort())
    selector = QuotaAwareModelSelector(stats=store)
    await selector.select(LINE, [])
    assert _Counting.reads == 0 and selector.last_soft_choice is None
