"""P0 fix: strict review ``off``/``shadow`` leave outcome history as main.

Inconclusive and unparseable review parks are set apart from a model's
success rate only under ``CONDUCTOR_DISPATCH_REVIEW_STRICT_MODE=enforce``.
In every other mode the selector reads the same recorded rate, and picks the
same author, as before P0. The same holds for the cost ledger's infra set,
which feeds ``CostStats`` (a selector input).
"""

from __future__ import annotations

import pytest

from src.server.services.autonomy.roles import ROLE_POLICY_ENV
from src.tools.auto_dispatch import cost
from src.tools.auto_dispatch.outcomes import InMemoryMemoryPort, MemoryP0OutcomeStore
from src.tools.auto_dispatch.selection import QuotaAwareModelSelector
from src.tools.auto_dispatch.soft_select import SELECTOR_MODE_ENV
from src.tools.auto_dispatch.types import Line, Outcome

STRICT_ENV = "CONDUCTOR_DISPATCH_REVIEW_STRICT_MODE"
LINE = Line(id="line-0", name="web", task="t", write_scope=("web/src/App.tsx",))

#: Model "kimi": 1 judged pass, then two parks the reviewer never judged.
#: Model "glm": 1 pass and 1 judged fail.
HISTORY = [
    ("kimi", True, None),
    ("kimi", False, "review_unparseable"),
    ("kimi", False, "review_inconclusive"),
    ("glm", True, None),
    ("glm", False, "tests_failed"),
]


def _main_rate(model: str) -> float:
    """What main computes: every row with ``passed`` counts as a run."""
    rows = [passed for name, passed, _ in HISTORY if name == model]
    return sum(1 for passed in rows if passed) / len(rows)


def _enforce_rate(model: str) -> float:
    rows = [
        passed
        for name, passed, reason in HISTORY
        if name == model and not (not passed and reason in {"review_unparseable", "review_inconclusive"})
    ]
    return sum(1 for passed in rows if passed) / len(rows)


async def _store() -> MemoryP0OutcomeStore:
    store = MemoryP0OutcomeStore(InMemoryMemoryPort())
    outcomes = {
        f"L{i}": Outcome(
            f"L{i}", model, 1, 1, passed, failure_reason=reason, category="web"
        )
        for i, (model, passed, reason) in enumerate(HISTORY)
    }
    await store.record("d1", outcomes)
    return store


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ROLE_POLICY_ENV, raising=False)
    monkeypatch.delenv(SELECTOR_MODE_ENV, raising=False)
    monkeypatch.delenv(STRICT_ENV, raising=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "shadow", None])
async def test_off_and_shadow_selector_rate_and_choice_equal_main(mode, monkeypatch) -> None:
    if mode is not None:
        monkeypatch.setenv(STRICT_ENV, mode)
    store = await _store()
    assert await store.success_rate("kimi") == pytest.approx(_main_rate("kimi"))
    assert await store.success_rate("glm") == pytest.approx(_main_rate("glm"))
    selector = QuotaAwareModelSelector(stats=store)
    assert await selector._recorded_rate("kimi") == pytest.approx(1 / 3)
    # Main ranks glm (0.5) above kimi (1/3): the unjudged parks count as fails.
    assert await selector.select(LINE, []) == "glm"
    assert await store.category_counts("kimi") == (1, 3)
    assert await store.category_counts("kimi", category="web") == (1, 3)
    table = await store.category_table("web")
    assert table["all"]["kimi"] == (1, 3) and table["category"]["kimi"] == (1, 3)
    rollup = await store._port.get(store._collection, "_models")  # type: ignore[attr-defined]
    assert rollup["kimi"] == {"runs": 3, "passed": 1, "by_category": {"web": {"runs": 3, "passed": 1}}}


@pytest.mark.asyncio
async def test_enforce_sets_unjudged_parks_apart(monkeypatch) -> None:
    monkeypatch.setenv(STRICT_ENV, "enforce")
    store = await _store()
    assert await store.success_rate("kimi") == pytest.approx(_enforce_rate("kimi")) == 1.0
    selector = QuotaAwareModelSelector(stats=store)
    assert await selector.select(LINE, []) == "kimi"
    assert await store.category_counts("kimi") == (1, 1)
    table = await store.category_table("web")
    assert table["all"]["kimi"] == (1, 1)
    rollup = await store._port.get(store._collection, "_models")  # type: ignore[attr-defined]
    assert rollup["kimi"]["runs"] == 1 and rollup["kimi"]["inconclusive"] == 2


@pytest.mark.parametrize("mode", ["off", "shadow", None])
def test_cost_infra_set_is_main_outside_enforce(mode, monkeypatch) -> None:
    if mode is not None:
        monkeypatch.setenv(STRICT_ENV, mode)
    assert "review_inconclusive" not in cost.INFRA_REASON_CODES
    assert cost.infra_reason_codes() == cost.INFRA_REASON_CODES
    assert cost._is_infra({"reason_code": "review_inconclusive"}) is False
    assert cost._is_infra({"reason_code": "review_unparseable"}) is True


def test_cost_infra_set_gains_inconclusive_in_enforce(monkeypatch) -> None:
    monkeypatch.setenv(STRICT_ENV, "enforce")
    assert "review_inconclusive" in cost.infra_reason_codes()
    assert cost._is_infra({"reason_code": "review_inconclusive"}) is True
