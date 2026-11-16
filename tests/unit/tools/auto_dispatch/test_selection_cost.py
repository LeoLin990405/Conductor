"""N30: cost per merged line as a soft, default-off selector input (WORK only)."""

from __future__ import annotations

import math

import pytest

from src.server.services.autonomy.roles import ROLE_POLICY_ENV
from src.tools.auto_dispatch.pool import eligible_models
from src.tools.auto_dispatch.protocols import ModelRole
from src.tools.auto_dispatch.selection import (
    QuotaAwareModelSelector,
    cost_penalties,
)
from src.tools.auto_dispatch.types import Line

LINE = Line(id="line-0", name="survey", task="read the code")
LARGE = Line(id="line-1", name="big", task="rewrite", estimated_size="large")


@pytest.fixture(autouse=True)
def _default_role_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ROLE_POLICY_ENV, raising=False)


class Headroom:
    def __init__(self, remaining: dict[str, float] | None = None) -> None:
        self.remaining_map = remaining or {}

    async def remaining(self, model: str) -> float:
        return self.remaining_map.get(model, 1.0)


class Stats:
    def __init__(self, rates: dict[str, float]) -> None:
        self.rates = rates

    async def success_rate(self, model: str) -> float | None:
        return self.rates.get(model)


class Costs:
    def __init__(self, costs: dict[str, float | None], *, fail: bool = False) -> None:
        self.costs = costs
        self.fail = fail
        self.calls: list[str] = []

    async def cost_per_merged_line(self, model: str) -> float | None:
        self.calls.append(model)
        if self.fail:
            raise RuntimeError("store down")
        return self.costs.get(model)


def _roster() -> list[str]:
    return list(eligible_models("work"))


async def _picks(selector: QuotaAwareModelSelector, n: int, line: Line = LINE) -> list:
    return [await selector.select(line, []) for _ in range(n)]


SCENARIOS = [
    ({}, {}),
    ({"glm": 0.9, "kimi": 0.4}, {}),
    ({"grok": 1.0}, {"kimi": 0.0}),
    ({"glm": 0.5, "grok": 0.5}, {"glm": 0.3}),
]


@pytest.mark.parametrize(("rates", "remaining"), SCENARIOS)
@pytest.mark.parametrize("line", [LINE, LARGE])
@pytest.mark.asyncio
async def test_off_keeps_todays_order(rates, remaining, line) -> None:
    n = len(_roster()) * 2
    baseline = QuotaAwareModelSelector(Headroom(remaining), Stats(rates))
    expected = await _picks(baseline, n, line)

    no_port = QuotaAwareModelSelector(Headroom(remaining), Stats(rates), cost_weight=0.15)
    zero = Costs({m: 10.0 * (i + 1) for i, m in enumerate(_roster())})
    zero_weight = QuotaAwareModelSelector(
        Headroom(remaining), Stats(rates), cost_stats=zero, cost_weight=0.0
    )
    assert await _picks(no_port, n, line) == expected
    assert await _picks(zero_weight, n, line) == expected
    # Off means the port is never consulted.
    assert zero.calls == []


@pytest.mark.asyncio
async def test_on_prefers_the_cheaper_of_two_tied_models() -> None:
    roster = _roster()
    first, second = roster[0], roster[1]
    rates = {first: 0.8, second: 0.8}
    off = QuotaAwareModelSelector(Headroom(), Stats(rates))
    assert await off.select(LINE, []) == first

    costs = Costs({first: 2000.0, second: 500.0})
    on = QuotaAwareModelSelector(Headroom(), Stats(rates), cost_stats=costs, cost_weight=0.15)
    assert await on.select(LINE, []) == second
    assert set(costs.calls) >= {first, second}


@pytest.mark.asyncio
async def test_assignments_still_lead() -> None:
    roster = _roster()
    first, second = roster[0], roster[1]
    rates = {first: 0.8, second: 0.8}
    costs = Costs({first: 2000.0, second: 500.0})
    on = QuotaAwareModelSelector(Headroom(), Stats(rates), cost_stats=costs, cost_weight=0.5)
    picks = await _picks(on, 2)
    # The cheap model goes first, then the dear one still gets the next line.
    assert picks == [second, first]


@pytest.mark.asyncio
async def test_model_without_enough_samples_is_unaffected() -> None:
    roster = _roster()
    first, second = roster[0], roster[1]
    rates = {first: 0.8, second: 0.8}
    # Only one model has data: the median is its own cost, so the penalty is 0.
    costs = Costs({second: 500.0})
    on = QuotaAwareModelSelector(Headroom(), Stats(rates), cost_stats=costs, cost_weight=0.5)
    assert await on.select(LINE, []) == first


@pytest.mark.asyncio
async def test_expensive_model_is_never_excluded() -> None:
    roster = _roster()
    dear = roster[0]
    headroom = Headroom({model: 0.0 for model in roster if model != dear})
    costs = Costs({dear: 1e9})
    on = QuotaAwareModelSelector(headroom, Stats({}), cost_stats=costs, cost_weight=0.5)
    assert await on.select(LINE, []) == dear


@pytest.mark.asyncio
async def test_dear_model_still_chosen_among_many_when_rate_is_higher() -> None:
    roster = _roster()
    first, second = roster[0], roster[1]
    # A big success-rate lead beats the bounded cost penalty (+-weight each).
    rates = {first: 1.0, second: 0.1}
    costs = Costs({first: 1e6, second: 1.0})
    on = QuotaAwareModelSelector(Headroom(), Stats(rates), cost_stats=costs, cost_weight=0.15)
    assert await on.select(LINE, []) == first


@pytest.mark.asyncio
async def test_failing_port_is_treated_as_no_data() -> None:
    baseline = QuotaAwareModelSelector(Headroom(), Stats({}))
    expected = await _picks(baseline, 3)
    on = QuotaAwareModelSelector(
        Headroom(), Stats({}), cost_stats=Costs({}, fail=True), cost_weight=0.15
    )
    assert await _picks(on, 3) == expected


@pytest.mark.asyncio
async def test_review_path_ignores_cost() -> None:
    costs = Costs({m: 1.0 for m in eligible_models("review")})
    baseline = QuotaAwareModelSelector()
    on = QuotaAwareModelSelector(cost_stats=costs, cost_weight=0.5)
    a = await baseline.select(LINE, ["kimi"], role=ModelRole.REVIEW)
    b = await on.select(LINE, ["kimi"], role=ModelRole.REVIEW)
    assert a == b
    assert costs.calls == []


def test_weight_is_clamped() -> None:
    selector = QuotaAwareModelSelector(cost_stats=Costs({}), cost_weight=9.0)
    assert selector._cost_weight == 0.5
    assert QuotaAwareModelSelector(cost_weight=-1.0)._cost_weight == 0.0


def test_cost_penalties_formula() -> None:
    got = cost_penalties({"a": 100.0, "b": 400.0, "c": 25.0, "d": None}, 0.2)
    # median of 25, 100, 400 is 100
    assert got["a"] == 0.0
    assert math.isclose(got["b"], 0.2)  # log2(4) = 2, clamped to 1
    assert math.isclose(got["c"], -0.2)
    assert "d" not in got
    assert cost_penalties({"a": 1.0}, 0.0) == {}
    assert cost_penalties({"a": 0.0, "b": -3.0}, 0.2) == {}
