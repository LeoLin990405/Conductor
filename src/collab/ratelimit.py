"""In-process token buckets that refuse when their state cannot be trusted.

N80 deliberately does not put collaboration rate limiting in Redis: a Redis
outage must not become an open door. These buckets live in the server process,
and every uncertain condition -- a clock that raises or moves backwards, a
missing bucket, a malformed cost -- denies the request. ``fail closed`` is
therefore a property of the implementation, not a convention for callers.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from src.collab.scopes import TOOL_SCOPES
from src.collab.types import RateLimitedError


@dataclass(frozen=True)
class RateLimitPolicy:
    """One bucket: ``limit`` events per ``window_seconds``."""

    name: str
    limit: int
    window_seconds: float

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or int(self.limit) <= 0:
            raise ValueError("a rate limit must be positive")
        if not math.isfinite(self.window_seconds) or self.window_seconds <= 0:
            raise ValueError("a rate-limit window must be a positive finite number")

    @property
    def rate_per_second(self) -> float:
        return self.limit / self.window_seconds

    @property
    def capacity(self) -> float:
        # A full window's worth is the burst allowance. Keeping capacity equal
        # to limit makes "120/min" mean "120 at once, then refill", which is
        # the reading the PRD's operators expect.
        return float(self.limit)


#: The four buckets from N80 section 8.
POLICIES: dict[str, RateLimitPolicy] = {
    "read": RateLimitPolicy("read", 120, 60),
    "write": RateLimitPolicy("write", 20, 60),
    "ask": RateLimitPolicy("ask", 10, 60),
    "create": RateLimitPolicy("create", 5, 3600),
}
RATE_LIMITS = POLICIES

#: Audit rows for refused calls that never authenticated, per peer address.
#: Such a caller is not attributable and is not charged to any app bucket,
#: so without this a loop of bad requests could grow the audit table without
#: bound. Past the limit the refusal is still answered, just not recorded.
REFUSAL_AUDIT_POLICY = RateLimitPolicy("refusal_audit", 60, 60)
#: Distinct peers with their own refusal bucket; later peers share one.
MAX_REFUSAL_PEERS = 256
_REFUSAL_OVERFLOW_PEER = "*"
READ_POLICY = POLICIES["read"]
WRITE_POLICY = POLICIES["write"]
ASK_POLICY = POLICIES["ask"]
CREATE_POLICY = POLICIES["create"]

#: Every write-shaped tool shares the write bucket; two tools add their own.
TOOL_POLICIES: dict[str, tuple[str, ...]] = {
    tool: ("read",) if scope == "read" else ("write",)
    for tool, scope in TOOL_SCOPES.items()
}
TOOL_POLICIES["task_create"] = ("write", "create")
TOOL_POLICIES["agent_ask"] = ("write", "ask")


@dataclass(frozen=True)
class RateLimitDecision:
    """The answer to one reservation; denial never carries a token value."""

    allowed: bool
    code: str
    retry_after: float = 0.0
    limit: int = 0
    remaining: int = 0
    policy: str = ""
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class _BucketState:
    tokens: float
    updated_at: float


class TokenBucket:
    """A single-process token bucket whose unknown state is a denial."""

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        capacity: float | None = None,
    ) -> None:
        self._policy = RateLimitPolicy("bucket", limit, window_seconds)
        self._capacity = (
            float(capacity) if capacity is not None else self._policy.capacity
        )
        if not math.isfinite(self._capacity) or self._capacity <= 0:
            raise ValueError("bucket capacity must be a positive finite number")
        self._clock = clock
        self._lock = threading.RLock()
        try:
            self._state: _BucketState | None = _BucketState(
                tokens=self._capacity, updated_at=self._now()
            )
        except Exception:
            # A bucket that cannot read its clock is born closed.
            self._state = None

    def _now(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value):
            raise ValueError("clock returned a non-finite value")
        return value

    def _plan(
        self, cost: float, now: float
    ) -> tuple[bool, float, _BucketState | None]:
        """Return (allowed, retry_after, next_state) without mutating."""
        state = self._state
        if state is None:
            return False, self._policy.window_seconds, None
        if not math.isfinite(cost) or cost <= 0 or cost > self._capacity:
            return False, self._policy.window_seconds, None
        if now < state.updated_at:
            # A backwards clock can mint tokens; refuse instead of rewarding it.
            return False, self._policy.window_seconds, None
        elapsed = now - state.updated_at
        tokens = min(
            self._capacity,
            state.tokens + elapsed * self._policy.rate_per_second,
        )
        if tokens + 1e-12 >= cost:
            return True, 0.0, _BucketState(tokens=tokens - cost, updated_at=now)
        missing = cost - tokens
        retry = missing / self._policy.rate_per_second
        return False, max(0.0, retry), None

    def _commit(self, state: _BucketState) -> None:
        self._state = state

    def try_consume(self, cost: float = 1.0) -> bool:
        try:
            with self._lock:
                allowed, _retry, next_state = self._plan(cost, self._now())
                if not allowed or next_state is None:
                    return False
                self._commit(next_state)
                return True
        except Exception:
            with self._lock:
                self._state = None
            return False

    def allow(self, cost: float = 1.0) -> bool:
        return self.try_consume(cost)

    def peek(self, cost: float = 1.0) -> bool:
        """Whether a consume would succeed, without consuming."""
        try:
            with self._lock:
                allowed, _retry, _next = self._plan(cost, self._now())
                return allowed
        except Exception:
            with self._lock:
                self._state = None
            return False

    def retry_after(self, cost: float = 1.0) -> float:
        try:
            with self._lock:
                allowed, retry, _next = self._plan(cost, self._now())
                return 0.0 if allowed else retry
        except Exception:
            with self._lock:
                self._state = None
            return self._policy.window_seconds

    @property
    def available(self) -> bool:
        return self._state is not None

    @property
    def closed(self) -> bool:
        return self._state is None

    @property
    def capacity(self) -> float:
        return self._capacity

    @property
    def tokens(self) -> float | None:
        return None if self._state is None else self._state.tokens

    @property
    def state(self) -> _BucketState | None:
        return self._state

    def invalidate(self) -> None:
        """Mark the bucket's state unavailable; every later call denies."""
        with self._lock:
            self._state = None

    def reset(self) -> None:
        """Refill the bucket; used after an explicit operator reset."""
        try:
            with self._lock:
                self._state = _BucketState(
                    tokens=self._capacity, updated_at=self._now()
                )
        except Exception:
            with self._lock:
                self._state = None


class RateLimiter:
    """One bucket per (app, policy), with an all-or-nothing reservation."""

    def __init__(
        self,
        policies: dict[str, RateLimitPolicy] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        tool_policies: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._policies = dict(POLICIES if policies is None else policies)
        self._clock = clock
        self._tool_policies = dict(
            TOOL_POLICIES if tool_policies is None else tool_policies
        )
        self._buckets: dict[tuple[str, str], TokenBucket] = {}
        self._lock = threading.RLock()
        self._last_clock_error = False

    def _now(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value):
            raise ValueError("clock returned a non-finite value")
        return value

    def policies_for_tool(self, tool: str) -> tuple[RateLimitPolicy, ...]:
        names = self._tool_policies.get(str(tool), ())
        return tuple(self._policies[name] for name in names if name in self._policies)

    def _bucket(self, app_id: str, policy: RateLimitPolicy) -> TokenBucket:
        key = (app_id, policy.name)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(
                policy.limit,
                policy.window_seconds,
                clock=self._clock,
                capacity=policy.capacity,
            )
            self._buckets[key] = bucket
        return bucket

    def evaluate(
        self, app_id: str, tool: str, *, cost: float = 1.0
    ) -> RateLimitDecision:
        """Reserve ``cost`` from every bucket for one call, or reserve none.

        The reservation is decided under the limiter lock. If any bucket is
        missing, closed by an earlier error, or cannot read the clock, no
        bucket is charged and the call is denied.
        """
        if not str(app_id or "").strip():
            return RateLimitDecision(False, "rate_limited", reason="missing_app_id")
        names = self._tool_policies.get(str(tool), ())
        if not names:
            return RateLimitDecision(False, "rate_limited", reason="unknown_tool")
        if any(name not in self._policies for name in names):
            # A partially configured limiter is not permission to skip the
            # missing bucket; deny and let the operator fix the policy.
            return RateLimitDecision(
                False, "rate_limited", reason="state_unavailable"
            )
        policies = tuple(self._policies[name] for name in names)
        try:
            with self._lock:
                # Create buckets first: a new bucket reads the clock when it
                # is born, so taking ``now`` before that would make the first
                # request look like a backwards clock.
                buckets = [(policy, self._bucket(app_id, policy)) for policy in policies]
                try:
                    now = self._now()
                except Exception:
                    self._last_clock_error = True
                    return RateLimitDecision(
                        False,
                        "rate_limited",
                        retry_after=60.0,
                        reason="state_unavailable",
                    )
                planned: list[tuple[TokenBucket, _BucketState]] = []
                retry_after = 0.0
                remaining = 0
                for policy, bucket in buckets:
                    allowed, retry, next_state = bucket._plan(cost, now)
                    if not allowed or next_state is None:
                        return RateLimitDecision(
                            False,
                            "rate_limited",
                            retry_after=retry,
                            limit=policy.limit,
                            remaining=0,
                            policy=policy.name,
                            reason=(
                                "state_unavailable"
                                if not bucket.available
                                else "limit_exceeded"
                            ),
                        )
                    retry_after = max(retry_after, retry)
                    planned.append((bucket, next_state))
                    remaining = int(next_state.tokens)
                # Every bucket can afford the cost: commit together.
                for bucket, next_state in planned:
                    bucket._commit(next_state)
                return RateLimitDecision(
                    True,
                    "ok",
                    retry_after=retry_after,
                    limit=min(policy.limit for policy, _ in buckets),
                    remaining=max(0, remaining),
                    policy=",".join(policy.name for policy, _ in buckets),
                )
        except Exception:
            self._last_clock_error = True
            return RateLimitDecision(
                False,
                "rate_limited",
                retry_after=60.0,
                reason="state_unavailable",
            )

    def allow_refusal_audit(self, peer: str | None) -> bool:
        """Whether one more unauthenticated refusal from ``peer`` gets an audit row.

        Keys are ``peer:<address>``; an app id cannot contain ``:``, so they
        never share a bucket with an app. Unknown bucket state denies, which
        here means one refusal goes unrecorded, never that a call is served.
        """
        address = str(peer or "unknown")[:64]
        policy = REFUSAL_AUDIT_POLICY
        with self._lock:
            key = f"peer:{address}"
            if (key, policy.name) not in self._buckets:
                peers = sum(1 for _key, name in self._buckets if name == policy.name)
                if peers >= MAX_REFUSAL_PEERS:
                    key = f"peer:{_REFUSAL_OVERFLOW_PEER}"
            try:
                return self._bucket(key, policy).try_consume(1.0)
            except Exception:  # noqa: BLE001 - an untrusted bucket records nothing
                return False

    def check(
        self, app_id: str, tool: str, *, cost: float = 1.0
    ) -> RateLimitDecision:
        return self.evaluate(app_id, tool, cost=cost)

    def allow(self, app_id: str, tool: str, *, cost: float = 1.0) -> bool:
        return self.evaluate(app_id, tool, cost=cost).allowed

    def consume(self, app_id: str, tool: str, *, cost: float = 1.0) -> None:
        decision = self.evaluate(app_id, tool, cost=cost)
        if not decision.allowed:
            raise RateLimitedError(
                "the app has exceeded its collaboration rate limit",
                retry_after=decision.retry_after,
            )

    def invalidate(self, app_id: str | None = None) -> None:
        """Simulate lost state (used by shutdown and failure-path tests)."""
        with self._lock:
            if app_id is None:
                for bucket in self._buckets.values():
                    bucket.invalidate()
                return
            for (key, _policy), bucket in self._buckets.items():
                if key == app_id:
                    bucket.invalidate()

    def reset(self, app_id: str | None = None) -> None:
        with self._lock:
            if app_id is None:
                self._buckets.clear()
                return
            for key in [item for item in self._buckets if item[0] == app_id]:
                self._buckets.pop(key, None)


#: One limiter per server process. Tests construct their own instead.
_DEFAULT_LIMITER: RateLimiter | None = None
_DEFAULT_LOCK = threading.Lock()


def get_default_limiter() -> RateLimiter:
    global _DEFAULT_LIMITER
    with _DEFAULT_LOCK:
        if _DEFAULT_LIMITER is None:
            _DEFAULT_LIMITER = RateLimiter()
        return _DEFAULT_LIMITER


def reset_default_limiter() -> None:
    global _DEFAULT_LIMITER
    with _DEFAULT_LOCK:
        _DEFAULT_LIMITER = None


#: Spellings L2 may import.
InProcessRateLimiter = RateLimiter
TokenBucketRateLimiter = RateLimiter


def policies_for_tool(tool: str) -> tuple[RateLimitPolicy, ...]:
    return tuple(POLICIES[name] for name in TOOL_POLICIES.get(tool, ()))


def tool_policy_names() -> Iterable[tuple[str, tuple[str, ...]]]:
    return tuple(sorted(TOOL_POLICIES.items()))


__all__ = [
    "ASK_POLICY",
    "CREATE_POLICY",
    "InProcessRateLimiter",
    "POLICIES",
    "RATE_LIMITS",
    "READ_POLICY",
    "REFUSAL_AUDIT_POLICY",
    "MAX_REFUSAL_PEERS",
    "RateLimitDecision",
    "RateLimitPolicy",
    "RateLimiter",
    "TOOL_POLICIES",
    "TokenBucket",
    "TokenBucketRateLimiter",
    "WRITE_POLICY",
    "get_default_limiter",
    "policies_for_tool",
    "reset_default_limiter",
    "tool_policy_names",
]
