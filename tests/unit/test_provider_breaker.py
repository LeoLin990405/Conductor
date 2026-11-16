"""State-machine contract tests for the local daemon provider breaker."""

from __future__ import annotations

import pytest

from src.conductor_cli.provider_breaker import ProviderCircuitBreaker


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_breaker_opens_only_after_configured_provider_failures():
    clock = FakeClock()
    breaker = ProviderCircuitBreaker(failure_threshold=2, monotonic=clock)

    first = breaker.acquire()
    assert first is not None
    breaker.record_failure(first)
    assert breaker.status()["state"] == "closed"

    second = breaker.acquire()
    assert second is not None
    breaker.record_failure(second)

    assert breaker.status()["state"] == "open"
    assert breaker.status()["consecutive_failures"] == 2
    assert breaker.acquire() is None


def test_breaker_state_is_independent_per_provider_instance():
    clock = FakeClock()
    codex = ProviderCircuitBreaker(failure_threshold=1, monotonic=clock)
    claude = ProviderCircuitBreaker(failure_threshold=1, monotonic=clock)

    permit = codex.acquire()
    assert permit is not None
    codex.record_failure(permit)

    assert codex.status()["state"] == "open"
    assert claude.status()["state"] == "closed"
    assert claude.acquire() is not None


def test_open_delay_uses_exponential_backoff_cap_and_injected_jitter():
    clock = FakeClock()
    breaker = ProviderCircuitBreaker(
        failure_threshold=1,
        reset_timeout=4,
        backoff_max=10,
        jitter=0.25,
        monotonic=clock,
        random_fn=lambda: 1.0,
    )

    def fail_once() -> None:
        permit = breaker.acquire()
        assert permit is not None
        breaker.record_failure(permit)

    fail_once()
    assert breaker.status()["retry_in_seconds"] == 5.0
    clock.advance(5.0)
    probe = breaker.acquire()
    assert probe is not None and probe.probe
    breaker.record_failure(probe)
    # The second open doubles the base (8s) before applying +25% jitter.
    assert breaker.status()["retry_in_seconds"] == 10.0

    clock.advance(10.0)
    probe = breaker.acquire()
    assert probe is not None and probe.probe
    breaker.record_failure(probe)
    # The final delay stays within the configured cap after jitter.
    assert breaker.status()["retry_in_seconds"] == 10.0


def test_half_open_allows_one_probe_and_recovers_after_success_threshold():
    clock = FakeClock()
    breaker = ProviderCircuitBreaker(
        failure_threshold=1,
        reset_timeout=2,
        jitter=0,
        recovery_success_threshold=2,
        monotonic=clock,
    )
    permit = breaker.acquire()
    assert permit is not None
    breaker.record_failure(permit)
    assert breaker.acquire() is None

    clock.advance(2)
    probe = breaker.acquire()
    assert probe is not None and probe.probe
    assert breaker.status()["state"] == "half_open"
    assert breaker.acquire() is None

    breaker.record_success(probe)
    assert breaker.status()["state"] == "half_open"
    assert breaker.status()["recovery_successes"] == 1
    next_probe = breaker.acquire()
    assert next_probe is not None and next_probe.probe
    breaker.record_success(next_probe)

    assert breaker.status()["state"] == "closed"
    assert breaker.status()["consecutive_failures"] == 0
    assert breaker.status()["open_count"] == 0
    assert breaker.acquire() is not None


def test_released_half_open_probe_is_fenced_and_immediately_eligible():
    clock = FakeClock()
    breaker = ProviderCircuitBreaker(
        failure_threshold=1, reset_timeout=10, jitter=0, monotonic=clock
    )
    permit = breaker.acquire()
    assert permit is not None
    breaker.record_failure(permit)
    clock.advance(10)

    probe = breaker.acquire()
    assert probe is not None and probe.probe
    breaker.release(probe)

    assert breaker.status()["state"] == "half_open"
    assert breaker.status()["consecutive_failures"] == 1
    assert breaker.status()["half_open_in_flight"] is False
    retry_probe = breaker.acquire()
    assert retry_probe is not None and retry_probe.probe
    breaker.record_success(probe)  # a late result from the released reservation is ignored
    assert breaker.status()["state"] == "half_open"
    breaker.record_success(retry_probe)
    assert breaker.status()["state"] == "closed"


def test_released_empty_probe_preserves_prior_recovery_successes():
    clock = FakeClock()
    breaker = ProviderCircuitBreaker(
        failure_threshold=1,
        reset_timeout=2,
        jitter=0,
        recovery_success_threshold=2,
        monotonic=clock,
    )
    initial = breaker.acquire()
    assert initial is not None
    breaker.record_failure(initial)
    clock.advance(2)

    first_probe = breaker.acquire()
    assert first_probe is not None and first_probe.probe
    breaker.record_success(first_probe)
    assert breaker.status()["recovery_successes"] == 1

    empty_claim_probe = breaker.acquire()
    assert empty_claim_probe is not None and empty_claim_probe.probe
    breaker.release(empty_claim_probe)
    assert breaker.status()["recovery_successes"] == 1
    next_probe = breaker.acquire()
    assert next_probe is not None and next_probe.probe
    breaker.record_success(next_probe)

    assert breaker.status()["state"] == "closed"


def test_late_success_from_old_generation_cannot_close_newly_opened_breaker():
    clock = FakeClock()
    breaker = ProviderCircuitBreaker(failure_threshold=1, monotonic=clock)
    slow = breaker.acquire()
    failing = breaker.acquire()
    assert slow is not None and failing is not None

    breaker.record_failure(failing)
    breaker.record_success(slow)

    assert breaker.status()["state"] == "open"
    assert breaker.acquire() is None


def test_success_clears_closed_state_failures_before_threshold():
    clock = FakeClock()
    breaker = ProviderCircuitBreaker(failure_threshold=2, monotonic=clock)
    failed = breaker.acquire()
    assert failed is not None
    breaker.record_failure(failed)
    succeeded = breaker.acquire()
    assert succeeded is not None
    breaker.record_success(succeeded)

    assert breaker.status()["state"] == "closed"
    assert breaker.status()["consecutive_failures"] == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"failure_threshold": 0},
        {"recovery_success_threshold": 0},
        {"reset_timeout": 0},
        {"reset_timeout": 11, "backoff_max": 10},
        {"backoff_max": 0},
        {"jitter": -0.1},
        {"jitter": 1.1},
    ],
)
def test_invalid_breaker_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        ProviderCircuitBreaker(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reset_timeout": float("nan")},
        {"reset_timeout": float("inf")},
        {"backoff_max": float("inf")},
        {"jitter": float("nan")},
    ],
)
def test_nonfinite_breaker_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        ProviderCircuitBreaker(**kwargs)
