"""Pure recovery decisions for classified native Worker failures."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from math import isfinite

from src.llms.native_cli.failure import (
    CONFIRMATION_REQUIRED_PROMPT_NOTE,
    CONFIRMATION_REQUIRED_REASON,
    TOOL_BLOCKED_REASON,
    TURN_BUDGET_EXHAUSTED_PROMPT_NOTE,
    TURN_BUDGET_EXHAUSTED_REASON,
)


@dataclass(frozen=True)
class Action:
    kind: str
    delay_seconds: float = 0
    reason: str | None = None
    # Text the retried Worker's prompt must carry. Only a confirmation
    # refusal sets it: the caller adds it to the next prompt so the Worker
    # does not try the refused command again.
    prompt_note: str | None = None


_FAILURE_CLASSES = frozenset(
    {
        "rate_limited",
        "overloaded",
        "network",
        "timeout",
        "quota_exhausted",
        "auth_failed",
        "context_exhausted",
        "turn_budget_exhausted",
        "confirmation_required",
        "tool_blocked",
        "crash",
        "unknown",
    }
)
# A local deadline used to be classified as network, and it still retries
# the same session. The count is its own, so a deadline is not a transport
# failure and does not spend the network budget.
_RETRY_SAME = frozenset({"rate_limited", "overloaded", "network", "timeout"})
_MAX_RETRIES = 3
_MAX_DELAY_SECONDS = 600.0
_BASE_DELAY_SECONDS = 10.0
# How many times a Worker that spent its turn budget is continued in the same
# session before the line parks for a human to split it.
_MAX_TURN_BUDGET_CONTINUES = 2


def _class_name(failure_class: object) -> str:
    if isinstance(failure_class, Enum):
        failure_class = failure_class.value
    if not isinstance(failure_class, str) or failure_class not in _FAILURE_CLASSES:
        raise ValueError("failure_class must be a known worker failure class")
    return failure_class


def _count(attempts_by_class: Mapping[object, object], name: str) -> int:
    """Read a nonnegative retry count, accepting enum and string keys."""
    value = attempts_by_class.get(name, 0)
    if value == 0:
        for key, candidate in attempts_by_class.items():
            if isinstance(key, Enum) and key.value == name:
                value = candidate
                break
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("attempt counts must be nonnegative integers")
    return value


def _retry_after(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("retry_after must be a finite number of seconds")
    delay = float(value)
    if not isfinite(delay):
        raise ValueError("retry_after must be a finite number of seconds")
    return min(_MAX_DELAY_SECONDS, max(0.0, delay))


def _retry_delay(failure_class: str, retries: int, now: datetime) -> float:
    base = min(_MAX_DELAY_SECONDS, _BASE_DELAY_SECONDS * (2**retries))
    seed = f"{failure_class}:{retries}:{now.isoformat(timespec='microseconds')}".encode()
    fraction = int.from_bytes(hashlib.sha256(seed).digest()[:8], "big") / 2**64
    return base + fraction


def decide(
    failure_class: object,
    retry_after: float | None,
    attempts_by_class: Mapping[object, object],
    is_fix_round: bool,
    now: datetime,
) -> Action:
    """Choose a bounded recovery action without mutating dispatch state."""
    name = _class_name(failure_class)
    retry_after_delay = _retry_after(retry_after)

    if name == "quota_exhausted":
        if is_fix_round:
            return Action("park", reason="author_quota_exhausted")
        return Action("reassign_fallback", reason="worker_quota_exhausted")
    if name == "auth_failed":
        return Action("park", reason="worker_auth_failed")
    if name == "confirmation_required":
        # The CLI's guard refused a command nobody could confirm (N39). The
        # login, the quota and the model are fine, so the same Worker gets
        # one more turn in the same session, told to avoid such commands.
        # The count is the class's own: it spends neither the crash budget
        # nor a reassign. A second refusal parks for a human.
        if _count(attempts_by_class, name) >= 1:
            return Action("park", reason=CONFIRMATION_REQUIRED_REASON)
        return Action(
            "retry_same",
            reason=CONFIRMATION_REQUIRED_REASON,
            prompt_note=CONFIRMATION_REQUIRED_PROMPT_NOTE,
        )
    if name == "tool_blocked":
        # The CLI's own safety gate ended the run on a tool call it will not
        # run unattended. The login, the quota and the model are fine, but the
        # same CLI would block the same kind of call again, so the line goes
        # to another Worker. A fix round keeps its author and parks instead.
        if is_fix_round:
            return Action("park", reason=TOOL_BLOCKED_REASON)
        return Action("reassign_fallback", reason=TOOL_BLOCKED_REASON)
    if name == "turn_budget_exhausted":
        # The CLI stopped at its own --max-turns: every request was answered,
        # so this is neither a login nor a quota problem and must not park as
        # one. The same Worker continues in the same session, told where it
        # stopped. The count is the class's own; past it the line is too big
        # for one run and parks for a human to split.
        if _count(attempts_by_class, name) >= _MAX_TURN_BUDGET_CONTINUES:
            return Action("park", reason=f"{name}_retries_exhausted")
        return Action(
            "retry_same",
            reason=TURN_BUDGET_EXHAUSTED_REASON,
            prompt_note=TURN_BUDGET_EXHAUSTED_PROMPT_NOTE,
        )
    if name == "context_exhausted":
        rotations = _count(attempts_by_class, name)
        if rotations >= _MAX_RETRIES:
            return Action("park", reason=f"{name}_retries_exhausted")
        return Action("rotate_session")
    if name in _RETRY_SAME:
        retries = _count(attempts_by_class, name)
        if retries >= _MAX_RETRIES:
            return Action("park", reason=f"{name}_retries_exhausted")
        delay = retry_after_delay
        if delay is None:
            delay = _retry_delay(name, retries, now)
        return Action("retry_same", delay_seconds=delay)

    # A crash and an unknown failure share the one fresh-session attempt so
    # switching labels cannot accidentally grant a second fresh retry.
    fresh_retries = _count(attempts_by_class, "crash") + _count(
        attempts_by_class, "unknown"
    )
    if fresh_retries == 0:
        return Action("retry_fresh_once")
    if is_fix_round:
        return Action("park", reason="fix_launch_failed")
    return Action("reassign_fallback", reason="fresh_retry_exhausted")
