"""Feasibility check before a new dispatch line launches (N76 L4).

Review admission looks at lines already waiting for review or already in
review. Once that count reaches the backlog cap, another line would only
wait in a queue the primary reviewers are not sized to drain, so the caller
holds it and tries again later. The cap is
``CONDUCTOR_DISPATCH_REVIEW_BACKLOG_MAX``. A missing, non-integer, or
non-positive setting falls back to twice the primary review concurrency.
This is a capacity check, not a permission gate: it does not decide whether
the line may exist, only whether starting it now is feasible.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

#: Operator cap on lines waiting for review plus lines already in review.
REVIEW_BACKLOG_MAX_ENV = "CONDUCTOR_DISPATCH_REVIEW_BACKLOG_MAX"
#: Recorded on a queue decision. A launch carries no reason.
REVIEW_BACKLOG_REASON = "review_backlog"
LAUNCH = "launch"
QUEUE = "queue"

_DEFAULT_MULTIPLIER = 2


@dataclass(frozen=True, slots=True)
class ReviewAdmission:
    """A launch, or a queue that records why the line waited.

    ``limit`` is the cap the decision used, after the environment fallback.
    """

    decision: str
    reason: str | None
    limit: int


def _nonnegative_int(value: object, label: str) -> int:
    """Reject bools: they are ints, and ``False`` would silently mean zero."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _positive_setting(raw: object) -> int | None:
    """A positive decimal integer, or None when the setting cannot be used.

    Operator text is untrusted. Anything that is not a positive integer —
    blank, a float, a word, zero, a negative — is ignored so the caller
    keeps the concurrency default instead of blocking or opening by accident.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = int(text, 10)
    except ValueError:
        return None
    if parsed <= 0:
        return None
    return parsed


def _backlog_limit(primary_concurrency: int, env: Mapping[str, str]) -> int:
    """The review-backlog cap for this concurrency and environment."""
    concurrency = _nonnegative_int(primary_concurrency, "primary_concurrency")
    configured = _positive_setting(env.get(REVIEW_BACKLOG_MAX_ENV))
    if configured is None:
        return concurrency * _DEFAULT_MULTIPLIER
    return configured


def decide_review_admission(
    waiting_or_in_review: int,
    primary_concurrency: int,
    env: Mapping[str, str] = os.environ,
) -> ReviewAdmission:
    """Queue a new line once review work has reached the backlog cap.

    ``waiting_or_in_review`` is lines already waiting for review plus lines
    already in review. The line under consideration is not part of that
    count. ``primary_concurrency`` is how many primary reviews can run at
    once. ``env`` is read and not modified; pass a mapping in tests.
    """
    waiting = _nonnegative_int(waiting_or_in_review, "waiting_or_in_review")
    limit = _backlog_limit(primary_concurrency, env)
    # The pipeline is full once the count reaches the cap. The line being
    # considered is not part of waiting_or_in_review, so equality already waits.
    if waiting >= limit:
        return ReviewAdmission(
            decision=QUEUE,
            reason=REVIEW_BACKLOG_REASON,
            limit=limit,
        )
    return ReviewAdmission(decision=LAUNCH, reason=None, limit=limit)
