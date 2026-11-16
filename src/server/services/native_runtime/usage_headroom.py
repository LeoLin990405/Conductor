"""Read bounded, non-identifying quota snapshots for automatic routing."""

from __future__ import annotations

import asyncio
import json
import os
import stat as stat_module
from datetime import datetime, timedelta, timezone
from typing import Any

from src.config.settings import get_native_runtime_usage_headroom_config
from src.utils.provider_usage import (
    DEFAULT_LOW_BALANCE_THRESHOLDS,
    PROVIDER_KINDS,
    balance_state,
    provider_kind,
)

# Snapshot rows are keyed by the native-runtime provider id (``zcode``,
# ``deepseek-dsh``) when the usage plane knows that id, else by the dispatch
# name (``claude``, ``doubao``). The alias table in ``autonomy.roles`` is the
# single place that knows which spellings are one CLI, so a row spelled
# ``glm``/``zai``/``zhipu``, ``dsh``, ``claudedev`` or ``arkcli`` lands on the
# same key as its CLI and the conflicting-alias merge below sees every reading.
_MAX_FUTURE_SKEW = timedelta(seconds=60)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def _fresh(value: Any, *, now: datetime, max_age_seconds: int) -> bool:
    observed = _timestamp(value)
    if observed is None:
        return False
    age = now - observed
    return -_MAX_FUTURE_SKEW <= age <= timedelta(seconds=max_age_seconds)


def _canonical_provider(value: Any) -> str | None:
    """The one snapshot key for a row's provider, or None when unsupported.

    Without one fold for every reader, a fresh exhausted row spelled by a
    vendor alias (``dsh``, ``claudedev``, ``arkcli``) was dropped here and the
    provider looked wide open to routing and assignment.
    """
    if not isinstance(value, str) or len(value) > 40:
        return None
    from src.server.services.autonomy.roles import (
        canonical_provider,
        runtime_provider_id,
    )

    canonical = canonical_provider(value)
    for key in (runtime_provider_id(canonical), canonical):
        if key in PROVIDER_KINDS:
            return key
    return None


# Windows that only meter one bucket of a plan (CodexBar ``tertiary`` and
# ``extraRateWindows``, e.g. Cursor's "Third Party" models). A full bucket makes
# the provider low, never exhausted: the plan itself still has room.
_BUCKET_ROLES = frozenset({"tertiary", "extra"})


class _WindowScan:
    """What the active windows of one row say, before it becomes a state."""

    __slots__ = ("values", "plan_full", "bucket_full", "malformed", "plan_resets")

    def __init__(self) -> None:
        self.values: list[float] = []
        self.plan_full = False
        self.bucket_full = False
        self.malformed = False
        # Reset times of the full plan windows that have not reset yet.
        self.plan_resets: list[datetime] = []


def _scan_windows(windows: list[Any], *, now: datetime) -> _WindowScan:
    scan = _WindowScan()
    scan.malformed = len(windows) > 40
    for window in windows[:40]:
        if not isinstance(window, dict):
            if window is not None:
                scan.malformed = True
            continue
        if "usedPercent" not in window and "used_percent" not in window:
            continue
        used = window.get("usedPercent", window.get("used_percent"))
        if type(used) not in (int, float) or not 0 <= used <= 100:
            scan.malformed = True
            continue
        reset_value = window.get("resetsAt", window.get("resets_at_utc"))
        reset = None
        if reset_value is not None:
            reset = _timestamp(reset_value)
            if reset is None:
                scan.malformed = True
                continue
            if reset <= now:
                continue
        scan.values.append(float(used))
        if used >= 100:
            if window.get("role") in _BUCKET_ROLES:
                scan.bucket_full = True
            else:
                scan.plan_full = True
                if reset is not None:
                    scan.plan_resets.append(reset)
    return scan


def _has_credits(credits_remaining: Any) -> bool:
    return type(credits_remaining) in (int, float) and credits_remaining > 0


def _window_state(
    windows: list[Any], *, now: datetime, credits_remaining: Any = None
) -> str | None:
    """available, low or exhausted from the active windows.

    A full plan window (primary/secondary, or a window with no role in an older
    snapshot) is exhausted unless the provider still has spendable credits, in
    which case it is low: work keeps running and is billed to the credits.
    """
    scan = _scan_windows(windows, now=now)
    if not scan.values:
        return None
    if scan.plan_full:
        return "low" if _has_credits(credits_remaining) else "exhausted"
    if scan.malformed:
        return None
    return "low" if scan.bucket_full else "available"


def _provider_entries(payload: Any) -> list[tuple[Any, ...]]:
    """Project supported snapshot shapes to provider, time, windows, status."""
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if isinstance(entries, list):
        projected = []
        for entry in entries[:100]:
            if not isinstance(entry, dict):
                continue
            windows = [entry.get(key) for key in ("primary", "secondary", "tertiary")]
            extra = entry.get("extraRateWindows")
            if isinstance(extra, list):
                windows.extend(
                    item.get("window") if isinstance(item, dict) else None
                    for item in extra[:40]
                )
            projected.append((entry.get("provider"), entry.get("updatedAt"), windows, "ok"))
        return projected

    rows = payload.get("providers") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    projected = []
    for row in rows[:100]:
        if not isinstance(row, dict):
            continue
        provider = row.get("provider", row.get("provider_id"))
        observed = row.get("updated_at", row.get("observed_at_utc"))
        status = row.get("status")
        windows = row.get("windows")
        if not isinstance(windows, list):
            metrics = row.get("metrics")
            windows = [
                {"used_percent": metric.get("value"), "resets_at_utc": metric.get("resets_at_utc")}
                for metric in metrics[:40]
                if isinstance(metric, dict)
                and metric.get("kind") == "quota_used_percent"
                and metric.get("unit") == "percent"
            ] if isinstance(metrics, list) else []
        projected.append((provider, observed, windows, status, row))
    return projected


def _now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc)


def _read_snapshot_payload(path: str, *, max_bytes: int) -> Any | None:
    """The parsed JSON of one regular, bounded, unchanging local file, or None.

    A configured path may start with "~": the tracked config carries the
    generic home-relative path, so the machine it runs on decides where the
    snapshot actually is. Symlinks, non-regular files, oversized files and a
    file that changes while it is read all answer None.
    """
    path = os.path.expanduser(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
        try:
            file_stat = os.fstat(fd)
            if not stat_module.S_ISREG(file_stat.st_mode) or file_stat.st_size > max_bytes:
                return None
            chunks = []
            size = 0
            while True:
                chunk = os.read(fd, min(65536, max_bytes + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    return None
                chunks.append(chunk)
            after = os.fstat(fd)
            if (file_stat.st_dev, file_stat.st_ino, file_stat.st_size, file_stat.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ):
                return None
        finally:
            os.close(fd)
        return json.loads(b"".join(chunks))
    except (OSError, ValueError, TypeError, RecursionError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def read_usage_headroom(
    path: str,
    *,
    now: datetime | None = None,
    max_age_seconds: int = 900,
    max_bytes: int = 262144,
    low_balance_thresholds: dict[str, dict[str, float]] | None = None,
) -> dict[str, str]:
    """Return fresh provider states from one explicitly configured local file."""
    current = _now(now)
    if not isinstance(path, str) or not path or max_age_seconds <= 0 or max_bytes <= 0:
        return {}
    payload = _read_snapshot_payload(path, max_bytes=max_bytes)
    if payload is None:
        return {}

    states: dict[str, str] = {}
    thresholds = (
        DEFAULT_LOW_BALANCE_THRESHOLDS
        if low_balance_thresholds is None
        else low_balance_thresholds
    )
    for entry in _provider_entries(payload):
        provider_value, observed, windows, status = entry[:4]
        row = entry[4] if len(entry) > 4 else None
        provider = _canonical_provider(provider_value)
        if provider is None:
            continue
        declared_kind = row.get("kind") if isinstance(row, dict) else None
        # Classify by the folded key: ``dsh`` is DeepSeek's balance row, not a
        # quota row with no windows that would be dropped.
        kind = provider_kind(provider, declared_kind)
        if status not in (None, "ok", "success", "partial"):
            if kind == "balance":
                state = "unknown"
            else:
                continue
        elif not _fresh(observed, now=current, max_age_seconds=max_age_seconds):
            if kind == "balance":
                state = "unknown"
            else:
                continue
        elif kind == "balance":
            balance = row.get("balance") if isinstance(row, dict) else None
            state = balance_state(provider, balance, thresholds)
        else:
            state = _window_state(
                windows if isinstance(windows, list) else [],
                now=current,
                credits_remaining=row.get("credits_remaining") if isinstance(row, dict) else None,
            )
            if state is None:
                continue
        if state not in {"available", "low", "exhausted", "unknown"}:
            continue
        prior = states.get(provider)
        # Conflicting aliases never let uncertain evidence create availability.
        rank = {"available": 0, "unknown": 1, "low": 2, "exhausted": 3}
        states[provider] = max((prior, state), key=lambda item: rank[item]) if prior else state
    return states


async def load_usage_headroom(config: Any = None, *, now: datetime | None = None) -> dict[str, str]:
    """Load the configured snapshot off the event loop; disabled mode does no I/O."""
    settings = config or get_native_runtime_usage_headroom_config()
    if settings is None or not getattr(settings, "enabled", False):
        return {}
    path = getattr(settings, "snapshot_path", None)
    if not isinstance(path, str) or not path.strip():
        return {}
    return await asyncio.to_thread(
        read_usage_headroom,
        path,
        now=now,
        max_age_seconds=int(getattr(settings, "max_age_seconds", 900)),
        max_bytes=int(getattr(settings, "max_bytes", 262144)),
        low_balance_thresholds=getattr(
            settings, "low_balance_thresholds", DEFAULT_LOW_BALANCE_THRESHOLDS
        ),
    )


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def read_usage_plan_status(
    path: str,
    *,
    now: datetime | None = None,
    max_age_seconds: int = 900,
    max_bytes: int = 262144,
) -> dict[str, dict[str, Any]]:
    """Per provider: is the plan itself full, are paid credits left, and when it resets.

    A companion to :func:`read_usage_headroom`, whose four-state contract is
    unchanged. ``plan_full`` is the same condition ``_window_state`` uses: a
    primary, secondary or role-less window at 100% that has not reset (a full
    ``tertiary``/``extra`` bucket is not the plan). ``credits`` is
    ``credits_remaining > 0``. ``resets_at`` is the latest reset among the full
    plan windows, as an ISO UTC string, or None. Only fresh rows whose status
    is ok are read, and only quota rows with at least one active window;
    balance rows have no plan windows and are left out. Aliases of one
    provider are merged so that any full plan counts.
    """
    current = _now(now)
    if not isinstance(path, str) or not path or max_age_seconds <= 0 or max_bytes <= 0:
        return {}
    payload = _read_snapshot_payload(path, max_bytes=max_bytes)
    if payload is None:
        return {}

    statuses: dict[str, dict[str, Any]] = {}
    latest: dict[str, datetime] = {}
    for entry in _provider_entries(payload):
        provider_value, observed, windows, status = entry[:4]
        row = entry[4] if len(entry) > 4 else None
        provider = _canonical_provider(provider_value)
        if provider is None:
            continue
        declared_kind = row.get("kind") if isinstance(row, dict) else None
        if provider_kind(provider_value, declared_kind) == "balance":
            continue
        if status not in (None, "ok", "success", "partial"):
            continue
        if not _fresh(observed, now=current, max_age_seconds=max_age_seconds):
            continue
        scan = _scan_windows(windows if isinstance(windows, list) else [], now=current)
        if not scan.values:
            continue
        credits = _has_credits(
            row.get("credits_remaining") if isinstance(row, dict) else None
        )
        prior = statuses.get(provider)
        merged = {
            "plan_full": scan.plan_full or bool(prior and prior["plan_full"]),
            "credits": credits or bool(prior and prior["credits"]),
            "resets_at": None,
        }
        if scan.plan_full and scan.plan_resets:
            newest = max(scan.plan_resets)
            if provider not in latest or newest > latest[provider]:
                latest[provider] = newest
        if provider in latest:
            merged["resets_at"] = _iso_utc(latest[provider])
        statuses[provider] = merged
    return statuses


async def load_usage_plan_status(
    config: Any = None, *, now: datetime | None = None
) -> dict[str, dict[str, Any]]:
    """Load :func:`read_usage_plan_status` off the event loop; disabled mode does no I/O."""
    settings = config or get_native_runtime_usage_headroom_config()
    if settings is None or not getattr(settings, "enabled", False):
        return {}
    path = getattr(settings, "snapshot_path", None)
    if not isinstance(path, str) or not path.strip():
        return {}
    return await asyncio.to_thread(
        read_usage_plan_status,
        path,
        now=now,
        max_age_seconds=int(getattr(settings, "max_age_seconds", 900)),
        max_bytes=int(getattr(settings, "max_bytes", 262144)),
    )
