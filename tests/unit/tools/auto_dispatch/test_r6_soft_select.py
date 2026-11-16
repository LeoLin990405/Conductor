"""R6 L4: soft-weighted sampling x headroom x owner preference."""

from __future__ import annotations

import random

from src.tools.auto_dispatch.soft_select import (
    Candidate,
    preference_weight,
    selector_mode,
    smoothed_rate,
    soft_weights,
    weighted_pick,
)


def test_mode_unset_is_off_without_a_registry_and_unknown_is_shadow() -> None:
    # Review fix 3: no stored registry -> off (fails on 3025c108e: shadow).
    assert selector_mode({}) == "off"
    assert selector_mode({"CONDUCTOR_DISPATCH_SELECTOR_MODE": "ENFORCE"}) == "enforce"
    assert selector_mode({"CONDUCTOR_DISPATCH_SELECTOR_MODE": "bogus"}) == "shadow"


def test_cold_start_prior_gives_untried_models_weight() -> None:
    assert smoothed_rate(0, 0) == 0.5
    result = soft_weights([Candidate("a", 9, 10), Candidate("new", 0, 0)], temp=0.5)
    assert 0 < result.weights["new"] < result.weights["a"]


def test_headroom_multiplies_weight() -> None:
    full = soft_weights([Candidate("a", 5, 10, 1.0), Candidate("b", 5, 10, 1.0)])
    low = soft_weights([Candidate("a", 5, 10, 1.0), Candidate("b", 5, 10, 0.1)])
    assert full.weights["a"] == full.weights["b"]
    assert low.weights["b"] < low.weights["a"]


def test_zero_headroom_everywhere_falls_back_to_rates() -> None:
    result = soft_weights([Candidate("a", 5, 10, 0.0), Candidate("b", 1, 10, 0.0)])
    assert result.weights["a"] > result.weights["b"]
    assert abs(sum(result.weights.values()) - 1.0) < 1e-9


def test_owner_preference_is_a_gentle_boost() -> None:
    assert preference_weight("a", ("a", "b")) > preference_weight("b", ("a", "b")) > 1.0
    assert preference_weight("z", ("a",)) == 1.0


def test_sampling_is_deterministic_with_a_seed_and_follows_weights() -> None:
    pool = [Candidate("good", 18, 20), Candidate("bad", 2, 20)]
    rng = random.Random(7)
    picks = [weighted_pick(pool, rng=rng).choice for _ in range(300)]
    assert picks.count("good") > picks.count("bad") > 0
    again = [weighted_pick(pool, rng=random.Random(7)).choice for _ in range(1)]
    assert again == picks[:1]


def test_empty_pool_picks_nothing() -> None:
    assert weighted_pick([], rng=random.Random(1)).choice is None
