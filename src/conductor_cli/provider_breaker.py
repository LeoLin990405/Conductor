"""In-process circuit breaker for a local daemon's configured provider."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from math import isfinite
from typing import Callable


@dataclass(frozen=True)
class Permit:
    generation: int
    probe: bool = False
    reservation: int = 0


class ProviderCircuitBreaker:
    """Gate provider work and back off after consecutive adapter failures."""

    def __init__(
        self,
        failure_threshold: int = 3,
        reset_timeout: float = 30.0,
        backoff_max: float = 300.0,
        jitter: float = 0.2,
        recovery_success_threshold: int = 1,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        random_fn: Callable[[], float] = random.random,
        on_change: Callable[["ProviderCircuitBreaker"], None] | None = None,
    ) -> None:
        if failure_threshold < 1 or recovery_success_threshold < 1:
            raise ValueError("breaker thresholds must be positive")
        if (
            not isfinite(reset_timeout)
            or not isfinite(backoff_max)
            or reset_timeout <= 0
            or backoff_max < reset_timeout
        ):
            raise ValueError("breaker timeouts must be positive and max >= base")
        if not isfinite(jitter) or not 0 <= jitter <= 1:
            raise ValueError("breaker jitter must be between 0 and 1")
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.backoff_max = backoff_max
        self.jitter = jitter
        self.recovery_success_threshold = recovery_success_threshold
        self._monotonic = monotonic
        self._random = random_fn
        self._on_change = on_change
        self._state = "closed"
        self._generation = 0
        self._failures = 0
        self._open_count = 0
        self._retry_at = 0.0
        self._probe_in_flight = False
        self._recovery_successes = 0
        self._reservation_seq = 0
        self._active_permits: set[int] = set()

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change(self)

    def _permit(self, probe: bool) -> Permit:
        self._reservation_seq += 1
        self._active_permits.add(self._reservation_seq)
        return Permit(self._generation, probe=probe, reservation=self._reservation_seq)

    def acquire(self) -> Permit | None:
        now = self._monotonic()
        if self._state == "open":
            if now < self._retry_at:
                return None
            self._state = "half_open"
            self._generation += 1
            self._probe_in_flight = False
            self._recovery_successes = 0
            self._changed()
        if self._state == "half_open":
            if self._probe_in_flight:
                return None
            self._probe_in_flight = True
            permit = self._permit(probe=True)
            self._changed()
            return permit
        return self._permit(probe=False)

    def record_success(self, permit: Permit) -> None:
        if (
            permit.generation != self._generation
            or permit.reservation not in self._active_permits
        ):
            return
        self._active_permits.remove(permit.reservation)
        if self._state == "half_open" and permit.probe:
            self._probe_in_flight = False
            self._recovery_successes += 1
            if self._recovery_successes >= self.recovery_success_threshold:
                self._state = "closed"
                self._generation += 1
                self._active_permits.clear()
                self._failures = 0
                self._open_count = 0
                self._recovery_successes = 0
            self._changed()
            return
        if self._state == "closed":
            self._failures = 0
            self._changed()

    def record_failure(self, permit: Permit) -> None:
        if (
            permit.generation != self._generation
            or permit.reservation not in self._active_permits
        ):
            return
        self._active_permits.remove(permit.reservation)
        if self._state == "half_open" and permit.probe:
            self._open()
            return
        if self._state != "closed":
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._open()
        else:
            self._changed()

    def release(self, permit: Permit) -> None:
        if (
            permit.generation != self._generation
            or permit.reservation not in self._active_permits
        ):
            return
        self._active_permits.remove(permit.reservation)
        if permit.probe and self._state == "half_open":
            self._probe_in_flight = False
            self._changed()

    def _open(self) -> None:
        self._state = "open"
        self._generation += 1
        self._open_count += 1
        exponent = min(self._open_count - 1, 30)
        delay = min(self.backoff_max, self.reset_timeout * (2**exponent))
        delay = min(
            self.backoff_max, delay * (1.0 + (2.0 * self._random() - 1.0) * self.jitter)
        )
        self._retry_at = self._monotonic() + max(0.0, delay)
        self._probe_in_flight = False
        self._recovery_successes = 0
        self._active_permits.clear()
        self._changed()

    def status(self) -> dict[str, int | float | str | bool]:
        retry_in = (
            max(0.0, self._retry_at - self._monotonic())
            if self._state == "open"
            else 0.0
        )
        if self._state == "half_open" and self._probe_in_flight:
            retry_in = 0.1
        return {
            "state": self._state,
            "consecutive_failures": self._failures,
            "failure_threshold": self.failure_threshold,
            "recovery_successes": self._recovery_successes,
            "recovery_success_threshold": self.recovery_success_threshold,
            "open_count": self._open_count,
            "retry_in_seconds": round(retry_in, 3),
            "half_open_in_flight": self._probe_in_flight,
        }
