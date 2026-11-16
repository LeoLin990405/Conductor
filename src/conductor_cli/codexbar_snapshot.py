"""Export a small, non-identifying CodexBar quota snapshot for local routing."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat as stat_module
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .provider_usage import provider_kind

PROVIDERS = (
    "codex",
    "claude",
    "cursor",
    "zai",
    "minimax",
    "manus",
    "kimi",
    "mimo",
    "doubao",
    "deepseek",
    "stepfun",
    "grok",
)
DEFAULT_OUTPUT = Path.home() / ".conductor" / "usage" / "codexbar-snapshot.json"
DEFAULT_DEEPSEEK_ENV = Path.home() / ".config" / "deepseek" / "env"
_DEEPSEEK_URL = "https://api.deepseek.com/user/balance"
_MAX_ENV_BYTES = 16_384
_MAX_HTTP_BYTES = 65_536
_MAX_CREDITS = 1_000_000_000_000.0
_CREDIT_UNITS = {"credit", "credits"}
# The CodexBar release these parsing rules were last checked against, and the
# output keys that release emits (names only; values are never kept). When
# CodexBar updates upstream, `cond doctor` reports a version mismatch or any key
# outside these sets as drift, so the rules here get re-validated and the
# fixtures in tests/fixtures/codexbar/ get refreshed (see docs/CODEXBAR.md).
VALIDATED_CODEXBAR_VERSION = "0.84.0"
KNOWN_RESULT_KEYS = frozenset({
    "credits", "diagnostic", "error", "pace", "provider", "rateWindowLabels",
    "resetCredits", "source", "usage", "version",
})
KNOWN_USAGE_KEYS = frozenset({
    "accountEmail", "accountOrganization", "codexResetCredits", "dataConfidence",
    "details", "extraRateWindows", "identity", "loginMethod", "planObservation",
    "primary", "providerCost", "secondary", "tertiary", "updatedAt",
})
KNOWN_WINDOW_KEYS = frozenset({
    "quotaLimit", "quotaUnit", "resetDescription", "resetsAt", "usedPercent", "windowMinutes",
})
# Below this many credits a provider is not treated as able to keep working.
MIN_SPENDABLE_CREDITS = 1.0
KNOWN_CREDITS_KEYS = frozenset({
    "balanceIsWorkspace", "balanceReadSucceeded", "creditsAvailable", "events",
    "remaining", "updatedAt",
})
_MAX_DRIFT_KEYS = 20
_VERSION_PREFIX = "CodexBar "
# Cursor bills per usage window; flag it while any active window is nearly spent.
USAGE_WARNING_PERCENT = 80.0
USAGE_WARNING_FLAG = "approaching_limit"


def _canonical_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None


def _numeric_percent(value: Any) -> int | float | None:
    if type(value) not in (int, float):
        return None
    if value < 0 or value > 100 or (type(value) is float and not math.isfinite(value)):
        return None
    return value


def _windows(usage: dict[str, Any]) -> list[dict[str, Any]] | None:
    result: list[dict[str, Any]] = []
    for key in ("primary", "secondary", "tertiary"):
        raw = usage.get(key)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            return None
        reset = None
        if "resetsAt" in raw and raw["resetsAt"] is not None:
            reset = _canonical_timestamp(raw["resetsAt"])
            if reset is None:
                return None
        if "usedPercent" not in raw:
            return None
        used = _numeric_percent(raw["usedPercent"])
        if used is None:
            return None
        # The role tells the reader which windows gate the whole plan
        # (primary/secondary) and which only meter one bucket of it
        # (tertiary, extra), e.g. Cursor's "Third Party" models.
        window: dict[str, Any] = {"used_percent": used, "role": key}
        if reset is not None:
            window["resets_at_utc"] = reset
        result.append(window)

    extras = usage.get("extraRateWindows")
    if extras is not None:
        if not isinstance(extras, list) or len(extras) > 40:
            return None
        for extra in extras:
            if not isinstance(extra, dict):
                return None
            raw = extra.get("window")
            if raw is None:
                return None
            if not isinstance(raw, dict):
                return None
            reset = None
            if "resetsAt" in raw and raw["resetsAt"] is not None:
                reset = _canonical_timestamp(raw["resetsAt"])
                if reset is None:
                    return None
            if "usedPercent" not in raw:
                return None
            used = _numeric_percent(raw["usedPercent"])
            if used is None:
                return None
            window = {"used_percent": used, "role": "extra"}
            if reset is not None:
                window["resets_at_utc"] = reset
            result.append(window)
    if len(result) > 40:
        return None
    return result or None


def _finite_nonnegative(value: Any, *, maximum: float) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and 0 <= value <= maximum
    except OverflowError:
        return False


def _credits_allowance(usage: dict[str, Any]) -> dict[str, Any] | None:
    """Extract an absolute credits allowance from structured fields only.

    CodexBar reports credits either as a top-level ``credits`` number or as a
    rate-window ``quotaLimit`` whose ``quotaUnit`` is credits. Identity text
    such as loginMethod is never parsed for numbers. Raises ValueError when a
    credits field is present but malformed, so callers fail closed.
    """
    candidates: list[int | float] = []
    if "credits" in usage:
        direct = usage["credits"]
        if not _finite_nonnegative(direct, maximum=_MAX_CREDITS):
            raise ValueError("credits must be a finite nonnegative number")
        candidates.append(direct)
    for slot in ("primary", "secondary", "tertiary"):
        window = usage.get(slot)
        if not isinstance(window, dict):
            continue
        limit_present = "quotaLimit" in window
        unit_present = "quotaUnit" in window
        if not limit_present and not unit_present:
            continue
        unit = window.get("quotaUnit")
        limit = window.get("quotaLimit")
        if not isinstance(unit, str) or not unit.strip():
            raise ValueError("quotaUnit must be a nonempty string")
        if unit.strip().lower() not in _CREDIT_UNITS:
            continue
        if not _finite_nonnegative(limit, maximum=_MAX_CREDITS):
            raise ValueError("quotaLimit must be a finite nonnegative number")
        candidates.append(limit)
    if not candidates:
        return None
    return {"total": max(candidates)}


def _credits_remaining(result: dict[str, Any]) -> int | float | None:
    """Spendable credits beyond the plan windows (Codex "Extra usage").

    CodexBar reports them in the result's own ``credits`` block. Only a balance
    the CLI read successfully and marks as available counts; anything else is
    treated as no credits, so a malformed block never creates headroom.
    """
    block = result.get("credits")
    if not isinstance(block, dict):
        return None
    if block.get("creditsAvailable") is not True or block.get("balanceReadSucceeded") is not True:
        return None
    remaining = block.get("remaining")
    if not _finite_nonnegative(remaining, maximum=_MAX_CREDITS) or remaining < MIN_SPENDABLE_CREDITS:
        return None
    return remaining


def _schema_drift(result: dict[str, Any]) -> list[str]:
    """Key names this CodexBar output has that the validated rules do not know.

    Only names are reported (``usage.newField``); values never leave the CLI
    result. A non-empty list means the parsing rules need re-validating.
    """
    unknown: set[str] = set()
    unknown.update(f"result.{key}" for key in result if key not in KNOWN_RESULT_KEYS)
    usage = result.get("usage")
    if isinstance(usage, dict):
        unknown.update(f"usage.{key}" for key in usage if key not in KNOWN_USAGE_KEYS)
        for slot in ("primary", "secondary", "tertiary"):
            window = usage.get(slot)
            if isinstance(window, dict):
                unknown.update(
                    f"window.{key}" for key in window if key not in KNOWN_WINDOW_KEYS
                )
    credits = result.get("credits")
    if isinstance(credits, dict):
        unknown.update(f"credits.{key}" for key in credits if key not in KNOWN_CREDITS_KEYS)
    names = sorted(key for key in unknown if len(key) <= 80)
    return names[:_MAX_DRIFT_KEYS]


def _codexbar_version(codexbar: str, timeout: float) -> str | None:
    """``X.Y.Z`` from ``codexbar --version``, or None when it cannot be read."""
    try:
        completed = subprocess.run(
            [codexbar, "--version"], capture_output=True, text=True,
            timeout=min(timeout, 10.0), check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None
    first = (completed.stdout or "").strip().splitlines()[:1]
    if completed.returncode != 0 or not first or not first[0].startswith(_VERSION_PREFIX):
        return None
    version = first[0][len(_VERSION_PREFIX):].strip()
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)*([-+][0-9A-Za-z.]+)?", version) or len(version) > 32:
        return None
    return version


def _usage_warning(
    provider: str, windows: list[dict[str, Any]], now: datetime
) -> str | None:
    """Flag Cursor once any active window is nearly spent, so the reminder
    fires before the plan hard-stops mid-session."""
    if provider != "cursor":
        return None
    for window in windows:
        used = window.get("used_percent")
        if type(used) not in (int, float) or used < USAGE_WARNING_PERCENT:
            continue
        reset_value = window.get("resets_at_utc")
        reset = _canonical_timestamp(reset_value) if reset_value is not None else None
        now_text = now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        if reset_value is None or (reset is not None and reset > now_text):
            return USAGE_WARNING_FLAG
    return None


def _single_result(payload: Any, provider: str) -> dict[str, Any] | None:
    if isinstance(payload, list):
        matches = [
            item for item in payload
            if isinstance(item, dict)
            and item.get("provider", provider) == provider
        ]
        return matches[0] if len(matches) == 1 else None
    if isinstance(payload, dict):
        # Some CLI versions return one provider result directly, others wrap it.
        if isinstance(payload.get("usage"), dict) or "error" in payload:
            reported = payload.get("provider", provider)
            return payload if reported == provider else None
        rows = payload.get("providers")
        if isinstance(rows, list):
            return _single_result(rows, provider)
    return None


def _provider_row(payload: Any, provider: str) -> dict[str, Any]:
    if provider_kind(provider) == "balance":
        return _unknown_balance_row(provider)
    row: dict[str, Any] = {"provider": provider, "kind": "quota", "status": "unknown", "windows": []}
    result = _single_result(payload, provider)
    if result is None or "error" in result:
        return row
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return row
    updated_at = _canonical_timestamp(usage.get("updatedAt"))
    windows = _windows(usage)
    if updated_at is None or windows is None:
        return row
    row.update(status="ok", updated_at=updated_at, windows=windows)
    drift = _schema_drift(result)
    if drift:
        row["schema_drift"] = drift
    credits_remaining = _credits_remaining(result)
    if credits_remaining is not None:
        row["credits_remaining"] = credits_remaining
    return row


def _credits_row(payload: Any, provider: str) -> dict[str, Any]:
    """Parse a credits-billed provider: percentage windows plus any allowance.

    Headroom still comes from the standard windows (that is the CodexBar
    contract); the credits block only records an absolute allowance when the
    CLI reports one structurally. Rows without windows stay unknown, since a
    credits-only observation gives no percentage headroom.
    """
    row: dict[str, Any] = {"provider": provider, "kind": "credits", "status": "unknown", "windows": []}
    result = _single_result(payload, provider)
    if result is None or "error" in result:
        return row
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return row
    updated_at = _canonical_timestamp(usage.get("updatedAt"))
    windows = _windows(usage)
    if updated_at is None or windows is None:
        return row
    try:
        credits = _credits_allowance(usage)
    except ValueError:
        return row
    row.update(status="ok", updated_at=updated_at, windows=windows)
    if credits is not None:
        row["credits"] = credits
    drift = _schema_drift(result)
    if drift:
        row["schema_drift"] = drift
    return row


def _read_credits_provider(codexbar: str, provider: str, timeout: float) -> dict[str, Any]:
    command = [codexbar, "usage", "--provider", provider, "--format", "json", "--no-color"]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return {"provider": provider, "kind": "credits", "status": "unknown", "windows": []}
    if completed.returncode != 0:
        return {"provider": provider, "kind": "credits", "status": "unknown", "windows": []}
    try:
        payload = json.loads(completed.stdout, parse_constant=lambda _: None)
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        return {"provider": provider, "kind": "credits", "status": "unknown", "windows": []}
    return _credits_row(payload, provider)


def _unknown_balance_row(provider: str = "deepseek") -> dict[str, Any]:
    return {"provider": provider, "kind": "balance", "status": "unknown"}


def _env_key(path: Path) -> str | None:
    """Read only the documented key from a small dotenv-style file, without evaluation."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    try:
        fd = os.open(path, flags)
        info = os.fstat(fd)
        if not stat_module.S_ISREG(info.st_mode) or info.st_size > _MAX_ENV_BYTES:
            return None
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(fd, min(4096, _MAX_ENV_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_ENV_BYTES:
                return None
            chunks.append(chunk)
        raw_bytes = b"".join(chunks)
        if len(raw_bytes) > _MAX_ENV_BYTES:
            return None
        raw = raw_bytes.decode("utf-8")
    except (OSError, UnicodeError):
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    value: str | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        name, separator, candidate = stripped.partition("=")
        if not separator or name.strip() != "DEEPSEEK_API_KEY":
            continue
        candidate = candidate.strip()
        if candidate[:1] in ("\"", "'"):
            quote = candidate[0]
            end = candidate.find(quote, 1)
            if end < 0 or candidate[end + 1 :].strip() and not candidate[end + 1 :].lstrip().startswith("#"):
                continue
            candidate = candidate[1:end]
        else:
            candidate = candidate.split(" #", 1)[0].rstrip()
        if candidate and len(candidate) <= 4096 and not any(char in candidate for char in "\r\n\x00"):
            value = candidate
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _parse_deepseek_balance(payload: Any) -> tuple[dict[str, Any] | None, bool | None]:
    if not isinstance(payload, dict) or "error" in payload:
        return None, None
    available = payload.get("is_available")
    if type(available) is not bool:
        return None, None
    infos = payload.get("balance_infos")
    if not isinstance(infos, list) or not infos or len(infos) > 40:
        return None, available
    choices: dict[str, set[int | float]] = {}
    for item in infos:
        if not isinstance(item, dict):
            continue
        currency = item.get("currency")
        raw_amount = item.get("total_balance")
        if not isinstance(currency, str) or not currency or len(currency) > 12:
            continue
        if isinstance(raw_amount, str):
            if len(raw_amount) > 64 or not raw_amount.strip():
                continue
            try:
                amount: Any = float(raw_amount.strip())
            except ValueError:
                continue
        else:
            amount = raw_amount
        if type(amount) not in (int, float):
            continue
        try:
            finite = math.isfinite(amount)
        except OverflowError:
            finite = False
        if not finite:
            continue
        choices.setdefault(currency, set()).add(amount)
    valid = {currency: values for currency, values in choices.items() if len(values) == 1}
    if "CNY" in choices and len(choices["CNY"]) != 1:
        return None, available
    if "CNY" in valid:
        amount = next(iter(valid["CNY"]))
        return {"amount": amount, "currency": "CNY"}, available
    if len(valid) != 1:
        return None, available
    currency, values = next(iter(valid.items()))
    return {"amount": next(iter(values)), "currency": currency}, available


def _read_deepseek_balance(
    env_file: Path | None = None,
    *,
    timeout: float = 10.0,
    opener: Any = None,
) -> dict[str, Any]:
    row = _unknown_balance_row()
    if timeout <= 0 or not math.isfinite(timeout):
        return row
    key = _env_key(Path(env_file) if env_file is not None else DEFAULT_DEEPSEEK_ENV)
    if key is None:
        return row
    request = urllib.request.Request(
        _DEEPSEEK_URL,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        method="GET",
    )
    try:
        active_opener = opener or urllib.request.build_opener(_NoRedirect)
        response = active_opener.open(request, timeout=timeout)
        with response:
            if getattr(response, "status", 200) != 200:
                return row
            raw = response.read(_MAX_HTTP_BYTES + 1)
        if len(raw) > _MAX_HTTP_BYTES:
            return row
        payload = json.loads(raw, parse_constant=lambda _: None)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, TypeError, RecursionError):
        return row
    balance, available = _parse_deepseek_balance(payload)
    if balance is None:
        return row
    if available is False and balance["amount"] > 0:
        return row
    row.update(status="ok", balance=balance)
    return row


def _read_provider(codexbar: str, provider: str, timeout: float) -> dict[str, Any]:
    command = [codexbar, "usage", "--provider", provider, "--format", "json", "--no-color"]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return {"provider": provider, "status": "unknown", "windows": []}
    if completed.returncode != 0:
        return {"provider": provider, "status": "unknown", "windows": []}
    try:
        payload = json.loads(completed.stdout, parse_constant=lambda _: None)
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        return {"provider": provider, "status": "unknown", "windows": []}
    return _provider_row(payload, provider)


def collect_snapshot(
    *, codexbar: str = "codexbar", timeout: float = 40.0,
    deepseek_env_file: Path | None = None, deepseek_opener: Any = None,
) -> dict[str, Any]:
    """Collect one bounded CLI result per provider, without retaining raw output."""
    if timeout <= 0 or not math.isfinite(timeout):
        raise ValueError("timeout must be a positive finite number")
    generated_at = datetime.now(timezone.utc)
    rows = []
    for provider in PROVIDERS:
        kind = provider_kind(provider)
        if kind == "balance":
            if provider == "deepseek":
                rows.append(_read_deepseek_balance(deepseek_env_file, timeout=min(timeout, 10.0), opener=deepseek_opener))
            else:
                rows.append(_unknown_balance_row(provider))
            continue
        if kind == "credits":
            rows.append(_read_credits_provider(codexbar, provider, timeout))
            continue
        row = _read_provider(codexbar, provider, timeout)
        row["kind"] = kind
        warning = _usage_warning(provider, row.get("windows") or [], generated_at)
        if warning is not None and row.get("status") == "ok":
            row["usage_warning"] = warning
        rows.append(row)
    generated = generated_at.isoformat(timespec="seconds").replace("+00:00", "Z")
    for row in rows:
        if row.get("kind") == "balance" and row.get("status") == "ok":
            row["updated_at"] = generated
    return {
        "updatedAt": generated,
        "codexbar_version": _codexbar_version(codexbar, timeout),
        "validated_codexbar_version": VALIDATED_CODEXBAR_VERSION,
        "providers": rows,
    }


def write_snapshot(payload: dict[str, Any], path: Path) -> None:
    """Atomically replace a snapshot with a private, same-directory file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            # fdopen owns the descriptor once constructed; close only if still open.
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        os.replace(temp_name, destination)
        temp_name = None
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def refresh_snapshot(
    path: Path = DEFAULT_OUTPUT,
    *,
    codexbar: str = "codexbar",
    timeout: float = 40.0,
) -> dict[str, Any]:
    payload = collect_snapshot(codexbar=codexbar, timeout=timeout)
    write_snapshot(payload, path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a sanitized CodexBar usage snapshot.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--codexbar", default="codexbar")
    parser.add_argument("--timeout", type=float, default=40.0)
    parser.add_argument("--deepseek-env-file", type=Path, default=DEFAULT_DEEPSEEK_ENV)
    args = parser.parse_args(argv)
    try:
        payload = collect_snapshot(
            codexbar=args.codexbar,
            timeout=args.timeout,
            deepseek_env_file=args.deepseek_env_file,
        )
        write_snapshot(payload, args.output)
    except Exception:
        print("Could not write CodexBar usage snapshot.")
        return 1
    safe_summary = {
        "updatedAt": payload["updatedAt"],
        "providers": [
            {"provider": row["provider"], "kind": row["kind"], "status": row["status"]}
            for row in payload["providers"]
        ],
    }
    print(json.dumps(safe_summary, ensure_ascii=True, separators=(",", ":")))
    return 0
