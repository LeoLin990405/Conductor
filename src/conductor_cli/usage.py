"""Display fresh local provider usage without loading credentials."""

from __future__ import annotations

import json
import math
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .codexbar_snapshot import USAGE_WARNING_FLAG, USAGE_WARNING_PERCENT

_MAX_AGE_SECONDS = 900
_MAX_FUTURE_SKEW_SECONDS = 60
_MAX_SNAPSHOT_BYTES = 262144
_MAX_PROVIDER_ROWS = 100
_MAX_WINDOWS = 40


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _number(value: Any) -> int | float | None:
    if type(value) is int:
        return value if 0 <= value <= 100 else None
    if type(value) is not float or not math.isfinite(value) or not 0 <= value <= 100:
        return None
    return value


def _provider_headroom(row: Any, now: datetime) -> dict[str, Any]:
    provider = row.get("provider") if isinstance(row, dict) else None
    result: dict[str, Any] = {"provider": provider, "status": "unknown"}
    from .provider_usage import provider_kind

    declared_kind = row.get("kind") if isinstance(row, dict) else None
    if provider_kind(provider, declared_kind) == "balance":
        result["kind"] = "balance"
        return result
    if not isinstance(row, dict) or row.get("status") != "ok":
        return result

    updated_at = _timestamp(row.get("updated_at", row.get("updatedAt")))
    if updated_at is None:
        return result
    age = (now - updated_at).total_seconds()
    if age < -_MAX_FUTURE_SKEW_SECONDS or age > _MAX_AGE_SECONDS:
        return result

    windows = row.get("windows")
    if not isinstance(windows, list) or not windows or len(windows) > _MAX_WINDOWS:
        return result
    remaining: list[int | float] = []
    # The snapshot flag is stamped at collect time. A window can expire while
    # the row is still fresh, so recompute from windows that are still open.
    active_at_limit = False
    for window in windows:
        if not isinstance(window, dict):
            return result
        used = _number(window.get("used_percent", window.get("usedPercent")))
        if used is None:
            return result
        reset_value = window.get("resets_at_utc", window.get("resetsAt"))
        if reset_value is None:
            active = True
        else:
            reset_at = _timestamp(reset_value)
            if reset_at is None:
                return result
            active = reset_at > now
        if not active:
            continue
        remaining.append(100 - used)
        if used >= USAGE_WARNING_PERCENT:
            active_at_limit = True

    if not remaining:
        return result
    result.update(status="ok", remaining_percent=min(remaining))
    if provider == "cursor" and active_at_limit:
        result["usage_warning"] = USAGE_WARNING_FLAG
    return result


def _unknown_result() -> dict[str, Any]:
    from .provider_usage import provider_kind

    return {
        "providers": [
            {
                "provider": name,
                **({"kind": provider_kind(name)} if provider_kind(name) != "quota" else {}),
                "status": "unknown",
            }
            for name in _provider_names()
        ]
    }


def load_headroom(
    path: Path, *, now: datetime | None = None, show_balance: bool = False
) -> dict[str, Any]:
    """Read the requested snapshot and return sanitized quota and balance states."""
    current_time = now or datetime.now(timezone.utc)
    payload: Any = None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_SNAPSHOT_BYTES:
                return _unknown_result()
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = os.read(descriptor, min(65536, _MAX_SNAPSHOT_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_SNAPSHOT_BYTES:
                    return _unknown_result()
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                return _unknown_result()
        finally:
            os.close(descriptor)
        payload = json.loads(b"".join(chunks))
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        payload = None

    rows = payload.get("providers") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        rows = []
    from .provider_usage import PROVIDER_KINDS, canonical_provider

    by_provider: dict[str, list[Any]] = {}
    for row in rows[:_MAX_PROVIDER_ROWS]:
        if isinstance(row, dict) and isinstance(row.get("provider"), str):
            key = canonical_provider(row["provider"])
            if key is not None:
                by_provider.setdefault(key, []).append(row)

    providers: list[dict[str, Any]] = []
    thresholds = _configured_balance_thresholds()
    from .provider_usage import balance_state, provider_kind

    names = list(_provider_names())
    names.extend(
        name for name in by_provider if name in PROVIDER_KINDS and name not in names
    )
    for name in names:
        matches = by_provider.get(name, [])
        if len(matches) == 1:
            row = matches[0]
            declared_kind = row.get("kind", row.get("usage_kind", row.get("billing_kind")))
            kind = provider_kind(name, declared_kind)
            if kind == "balance":
                balance = row.get("balance")
                updated_at = _timestamp(row.get("updated_at", row.get("updatedAt")))
                age = (current_time - updated_at).total_seconds() if updated_at else None
                fresh = age is not None and -_MAX_FUTURE_SKEW_SECONDS <= age <= _MAX_AGE_SECONDS
                state = (
                    balance_state(name, balance, thresholds)
                    if row.get("status") == "ok" and fresh
                    else "unknown"
                )
                result = {"provider": name, "kind": "balance", "status": state}
                if show_balance and state != "unknown" and isinstance(balance, dict):
                    result["balance"] = {
                        "amount": balance["amount"],
                        "currency": balance["currency"],
                    }
                providers.append(result)
            else:
                result = _provider_headroom(row, current_time)
                if kind == "credits":
                    result["kind"] = "credits"
                providers.append(result)
        else:
            known_kind = provider_kind(name)
            if known_kind != "quota":
                providers.append({"provider": name, "kind": known_kind, "status": "unknown"})
            else:
                providers.append({"provider": name, "status": "unknown"})
    return {"providers": providers}


def _configured_balance_thresholds() -> dict[str, Any] | None:
    """Read only the operator's YAML threshold map; never import server settings."""
    try:
        from .config_files import find_config_file, load_yaml_config
        from .provider_usage import normalize_low_balance_thresholds

        path = find_config_file("config.yaml")
        if path is None:
            return None
        payload = load_yaml_config(str(path))
        native = payload.get("native_runtime") if isinstance(payload, dict) else None
        usage = native.get("usage_headroom") if isinstance(native, dict) else None
        thresholds = usage.get("low_balance_thresholds") if isinstance(usage, dict) else None
        if thresholds is None:
            return None
        return normalize_low_balance_thresholds(thresholds)
    except (OSError, ValueError, TypeError):
        return {}


def _provider_names() -> tuple[str, ...]:
    from .codexbar_snapshot import PROVIDERS

    return PROVIDERS


def usage_result(
    *,
    path: Path | None = None,
    no_refresh: bool = False,
    codexbar: str = "codexbar",
    timeout: float = 40.0,
    show_balance: bool = False,
) -> dict[str, Any]:
    """Refresh if requested; expose balance amounts only with explicit opt-in."""
    from .codexbar_snapshot import DEFAULT_OUTPUT, refresh_snapshot

    snapshot_path = path or DEFAULT_OUTPUT
    if not no_refresh:
        refresh_snapshot(snapshot_path, codexbar=codexbar, timeout=timeout)
    return load_headroom(snapshot_path, show_balance=show_balance)


def render_usage(result: dict[str, Any], *, json_mode: bool) -> None:
    import json as json_module

    providers = result.get("providers", [])
    if json_mode:
        print(json_module.dumps(result, ensure_ascii=True, indent=2))
        return
    for row in providers:
        if row.get("status") == "ok":
            percent = row["remaining_percent"]
            suffix = " (credits)" if row.get("kind") == "credits" else ""
            print(f"{row['provider']}: {percent:g}% remaining{suffix}")
            if row.get("usage_warning") == USAGE_WARNING_FLAG:
                print(
                    f"{row['provider']}: warning: usage at or above "
                    f"{USAGE_WARNING_PERCENT:g}% of a billing window"
                )
        elif row.get("status") in {"available", "low", "exhausted"}:
            text = {"available": "balance ok", "low": "balance low", "exhausted": "balance exhausted"}[row["status"]]
            balance = row.get("balance")
            if isinstance(balance, dict):
                suffix = f" {balance['currency']}" if balance.get("currency") else ""
                print(f"{row['provider']}: {text} ({balance['amount']:g}{suffix})")
            else:
                print(f"{row['provider']}: {text}")
        else:
            label = "balance unknown" if row.get("kind") == "balance" else "unknown"
            print(f"{row['provider']}: {label}")
