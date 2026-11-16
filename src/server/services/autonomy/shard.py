"""Configurable Router sharding above a load threshold.

A single Router session is the default. Once a user's pending Eval load
crosses ``AUTONOMY_ROUTER_SHARD_THRESHOLD``, candidate dispatch splits across
separate Router sessions (shards) so one long-lived thread is not the only
throughput path. Shard assignment is a stable hash of the batch key, so a
retry after a crash lands on the same shard's session.
"""

from __future__ import annotations

import hashlib
import math
import os

DEFAULT_SHARD_THRESHOLD = 32
DEFAULT_MAX_SHARDS = 4


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    if not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 1 else default


def router_shard_threshold() -> int:
    """Pending-Eval count above which one user's dispatch starts sharding."""
    return _positive_int_env(
        "AUTONOMY_ROUTER_SHARD_THRESHOLD", DEFAULT_SHARD_THRESHOLD
    )


def router_max_shards() -> int:
    """Hard cap on Router sessions one user's dispatch may spread across."""
    return _positive_int_env("AUTONOMY_ROUTER_MAX_SHARDS", DEFAULT_MAX_SHARDS)


def router_shard_count(
    pending_evals: int,
    *,
    threshold: int | None = None,
    max_shards: int | None = None,
) -> int:
    """Shards needed for a load: 1 at or below the threshold, else ceil capped."""
    threshold = router_shard_threshold() if threshold is None else max(1, threshold)
    max_shards = router_max_shards() if max_shards is None else max(1, max_shards)
    if pending_evals <= threshold:
        return 1
    return min(max_shards, math.ceil(pending_evals / threshold))


def router_shard_for(key: str, shard_count: int) -> int:
    """Stable shard for one batch key; 0 whenever sharding is off."""
    if shard_count <= 1:
        return 0
    digest = hashlib.sha256(str(key).encode()).digest()
    return int.from_bytes(digest[:4], "big") % shard_count
