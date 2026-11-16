"""The in-process token bucket fails closed on every uncertain condition."""

from __future__ import annotations

import pytest

from src.collab import ratelimit
from src.collab.types import RateLimitedError


class _Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _limiter(**limits: int) -> ratelimit.RateLimiter:
    policies = {
        name: ratelimit.RateLimitPolicy(name, limit, 60)
        for name, limit in limits.items()
    }
    return ratelimit.RateLimiter(policies)


def test_token_bucket_spends_then_refills():
    clock = _Clock()
    bucket = ratelimit.TokenBucket(2, 60, clock=clock)
    assert bucket.allow()
    assert bucket.allow()
    assert not bucket.allow()
    clock.value = 30
    assert bucket.allow()
    assert not bucket.allow()
    assert bucket.capacity == 2


def test_token_bucket_denies_invalid_costs_without_opening():
    bucket = ratelimit.TokenBucket(2, 60, clock=_Clock())
    assert not bucket.allow(0)
    assert not bucket.allow(-1)
    assert not bucket.allow(float("inf"))
    assert not bucket.allow("1")  # type: ignore[arg-type]
    assert bucket.closed


def test_token_bucket_is_born_closed_when_the_clock_raises():
    def broken_clock() -> float:
        raise RuntimeError("clock unavailable")

    bucket = ratelimit.TokenBucket(2, 60, clock=broken_clock)
    assert bucket.closed and not bucket.available
    assert not bucket.allow()
    assert bucket.retry_after() > 0


def test_token_bucket_refuses_a_backwards_clock():
    clock = _Clock(10)
    bucket = ratelimit.TokenBucket(2, 60, clock=clock)
    assert bucket.allow()
    clock.value = 9
    assert not bucket.allow()
    assert bucket.retry_after() > 0


def test_invalidate_and_reset_deny_then_refill():
    bucket = ratelimit.TokenBucket(2, 60, clock=_Clock())
    bucket.invalidate()
    assert not bucket.available
    assert not bucket.allow()
    bucket.reset()
    assert bucket.available and bucket.allow()


def test_policy_rejects_non_positive_or_non_finite_limits():
    with pytest.raises(ValueError):
        ratelimit.RateLimitPolicy("bad", 0, 60)
    with pytest.raises(ValueError):
        ratelimit.RateLimitPolicy("bad", True, 60)
    with pytest.raises(ValueError):
        ratelimit.RateLimitPolicy("bad", 1, 0)
    with pytest.raises(ValueError):
        ratelimit.RateLimitPolicy("bad", 1, float("nan"))


def test_read_bucket_denies_after_the_limit():
    limiter = _limiter(read=2)
    assert limiter.allow("claude-code", "task_list")
    assert limiter.allow("claude-code", "task_list")
    decision = limiter.evaluate("claude-code", "task_list")
    assert not decision.allowed
    assert decision.code == "rate_limited"
    assert decision.reason == "limit_exceeded"
    assert decision.retry_after > 0


def test_each_app_has_its_own_buckets():
    limiter = _limiter(read=1)
    assert limiter.allow("claude-code", "task_list")
    assert not limiter.allow("claude-code", "task_list")
    assert limiter.allow("codex", "task_list")


def test_agent_ask_and_task_create_use_their_extra_buckets():
    ask = _limiter(write=100, ask=1)
    assert ask.allow("codex", "agent_ask")
    assert not ask.allow("codex", "agent_ask")

    create = _limiter(write=100, create=1)
    assert create.allow("codex", "task_create")
    assert not create.allow("codex", "task_create")


def test_a_reservation_is_all_or_nothing_across_buckets():
    limiter = _limiter(write=1, create=10)
    assert limiter.allow("codex", "task_create")
    assert not limiter.allow("codex", "task_create")
    create_bucket = limiter._buckets[("codex", "create")]
    # The denied second call was charged to nothing: the write bucket refused
    # before the create bucket spent its second token.
    assert create_bucket.tokens == pytest.approx(9)


def test_unknown_tool_and_missing_app_are_denied():
    limiter = _limiter(read=1)
    assert not limiter.allow("app", "not_a_tool")
    assert limiter.evaluate("app", "not_a_tool").reason == "unknown_tool"
    assert not limiter.allow("", "task_list")
    assert limiter.evaluate("", "task_list").reason == "missing_app_id"


def test_a_partially_configured_limiter_denies_instead_of_skipping_a_bucket():
    # ``agent_ask`` needs both write and ask. A policy table that names only
    # ask must not silently drop the write bucket.
    limiter = _limiter(ask=100)
    decision = limiter.evaluate("codex", "agent_ask")
    assert not decision.allowed
    assert decision.reason == "state_unavailable"

    empty = ratelimit.RateLimiter({})
    assert not empty.allow("codex", "task_list")


def test_a_broken_clock_denies_even_an_otherwise_full_bucket():
    def broken_clock() -> float:
        raise RuntimeError("clock unavailable")

    limiter = ratelimit.RateLimiter(
        {"read": ratelimit.RateLimitPolicy("read", 5, 60)},
        clock=broken_clock,
    )
    decision = limiter.evaluate("app", "task_list")
    assert not decision.allowed
    assert decision.reason == "state_unavailable"
    assert decision.retry_after > 0


def test_consume_raises_the_protocol_error_with_retry_after():
    limiter = _limiter(read=1)
    limiter.consume("app", "task_list")
    with pytest.raises(RateLimitedError) as caught:
        limiter.consume("app", "task_list")
    assert caught.value.retry_after is not None
    assert caught.value.code == "rate_limited"


def test_invalidate_and_reset_cover_one_app_or_all():
    limiter = _limiter(read=1)
    assert limiter.allow("a", "task_list")
    assert limiter.allow("b", "task_list")
    limiter.invalidate("a")
    assert not limiter.allow("a", "task_list")
    assert limiter.allow("b", "task_list") is False  # already spent
    limiter.reset("a")
    assert limiter.allow("a", "task_list")
    limiter.invalidate()
    assert not limiter.allow("a", "task_list")
    assert not limiter.allow("b", "task_list")


def test_tool_policy_table_matches_the_scope_table():
    assert ratelimit.TOOL_POLICIES["task_list"] == ("read",)
    assert ratelimit.TOOL_POLICIES["task_claim"] == ("write",)
    assert ratelimit.TOOL_POLICIES["agent_ask"] == ("write", "ask")
    assert ratelimit.TOOL_POLICIES["task_create"] == ("write", "create")
    assert ratelimit.policies_for_tool("agent_ask") == (
        ratelimit.WRITE_POLICY,
        ratelimit.ASK_POLICY,
    )
    assert dict(ratelimit.tool_policy_names()) == ratelimit.TOOL_POLICIES


def test_default_limiter_is_a_process_singleton_until_reset():
    ratelimit.reset_default_limiter()
    first = ratelimit.get_default_limiter()
    assert ratelimit.get_default_limiter() is first
    ratelimit.reset_default_limiter()
    assert ratelimit.get_default_limiter() is not first
