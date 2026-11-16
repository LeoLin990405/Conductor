"""N30 v1: what each merged line cost, per attempt and per model.

The ledger is a *record of facts* kept in ``dispatch.gates["cost"]``: one entry
per author attempt (work round or fix round), keyed by ``run_id`` so a second
write of the same run updates the entry instead of appending one. Nothing is
attributed when it is written; every metric (line outcome, use bucket, waste
class, cost per merged line) is computed when the report is read.

Shape (``docs/plans/next/n30-line-cost.md`` section 4.1)::

    gates["cost"] = {"v": 1,
                     "lines": {line_id: [entry, ...]},   # at most 64 per line
                     "truncated": {line_id: n}}          # entries dropped

No model output is ever stored or reported: no log tail, prompt, diff, review
text or CLI text. A failure is a ``failure_class`` (a ``FailureClass`` value)
and a ``reason_code`` that is redacted and reduced to one stable code token.
The CodexBar reading keeps only a provider id, percentages and timestamps.

Everything here is a pure function over plain dicts and ``Dispatch`` objects,
except :func:`read_quota` (reads the local CodexBar snapshot file) and
:class:`CostStats` (reads the dispatch store, cached).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import headroom_codexbar
from .types import Dispatch, DispatchState, Line, LineStatus, Outcome

logger = logging.getLogger(__name__)

LEDGER_VERSION = 1
MAX_ATTEMPTS_PER_LINE = 64
QUOTA_NOTE = "approximate, account-wide (CodexBar)"
LEDGER_ENV = "CONDUCTOR_DISPATCH_COST_LEDGER"

# Planner ruling 3: our own environment problems are not charged to a model in
# the selector. They are still reported, flagged ``"infrastructure": true``.
INFRA_FAILURE_CLASSES: frozenset[str] = frozenset({"auth_failed"})
INFRA_REASON_CODES: frozenset[str] = frozenset(
    {
        "missing_credential",
        "review_unparseable",
        "serial_lock_timeout",
        "serial_lock_wait_exceeded",
    }
)

#: P0 strict review: an inconclusive park is our infrastructure only once
#: ``CONDUCTOR_DISPATCH_REVIEW_STRICT_MODE=enforce``; ``off``/``shadow`` keep
#: the pre-P0 set, so cost-per-merged-line (a selector input) is unchanged.
ENFORCE_INFRA_REASON_CODES: frozenset[str] = frozenset({"review_inconclusive"})


def infra_reason_codes() -> frozenset[str]:
    """The reason codes not charged to a model under the current strict mode."""
    from .review_strict import strict_mode

    if strict_mode() == "enforce":
        return INFRA_REASON_CODES | ENFORCE_INFRA_REASON_CODES
    return INFRA_REASON_CODES


# A CodexBar reading older than this (relative to when it was read) does not
# describe the attempt it brackets.
QUOTA_FRESH_SECONDS = 900

USE_USEFUL = "useful"
USE_REWORK = "rework"
USE_WASTE = "waste"

WASTE_SUPERSEDED = "superseded"
WASTE_ABANDONED = "abandoned"
WASTE_UNKNOWN = "unknown"
#: An attempt that has started and not ended yet (an addition to PRD 4.2, so a
#: running attempt is not reported as an ``unknown`` failure).
WASTE_IN_PROGRESS = "in_progress"

_MAX_REASON_CODE = 64
_CODE_RE = re.compile(r"[a-z0-9_.:-]+")
_CODE_TRIM = ".:-"

_OFF_VALUES = frozenset({"off", "0", "false", "no", "disabled"})

_KINDS = frozenset({"work", "fix"})
_RESULTS = frozenset({"ok", "failed"})


def _failure_class_values() -> frozenset[str]:
    try:
        from src.llms.native_cli.failure import FailureClass
    except Exception:  # pragma: no cover - the module is part of this repo
        return frozenset()
    return frozenset(item.value for item in FailureClass)


_FAILURE_CLASSES = _failure_class_values()


def _redact(value: str) -> str:
    try:
        from src.llms.native_cli.failure import redact_credentials
    except Exception:  # pragma: no cover - the module is part of this repo
        return value
    try:
        return str(redact_credentials(value))
    except Exception:
        return ""


def _canonical(model: Any) -> str:
    """One spelling per Worker (``zai``/``zcode`` -> ``glm``)."""
    name = str(model or "").strip()
    if not name:
        return ""
    try:
        from src.server.services.autonomy.roles import canonical_provider

        return canonical_provider(name)
    except Exception:  # pragma: no cover - the module is part of this repo
        return name.lower()


# --------------------------------------------------------------------------
# Switch


def ledger_enabled(env: Mapping[str, str] | None = None) -> bool:
    """``CONDUCTOR_DISPATCH_COST_LEDGER=on|off``; on unless clearly off."""
    source = os.environ if env is None else env
    raw = str(source.get(LEDGER_ENV, "") or "").strip().lower()
    return raw not in _OFF_VALUES


# --------------------------------------------------------------------------
# Small normalisers


def _iso(value: datetime | None) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    try:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def _parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _count(value: Any) -> int | None:
    """A non-negative int, or None for anything else (bools included)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value < 0:  # NaN, negative
        return None
    try:
        return int(value)
    except (OverflowError, ValueError):
        return None


def normalize_reason_code(value: Any) -> str | None:
    """A stable code token for ``value``, or None.

    The value is redacted first, then lowercased. Only the leading code token
    survives (``[a-z0-9_.:-]``, at most 64 characters, trailing ``.:-``
    trimmed), so a free-text diagnostic such as ``"MISSING_CREDENTIAL: no API
    key"`` is stored as ``missing_credential`` and the rest of the text is
    dropped.
    """
    if value is None:
        return None
    if hasattr(value, "value") and isinstance(value.value, str):
        value = value.value
    text = _redact(str(value)).strip().lower()
    if not text:
        return None
    match = _CODE_RE.match(text)
    if match is None:
        return None
    code = match.group(0)[:_MAX_REASON_CODE].rstrip(_CODE_TRIM)
    return code or None


def _failure_class(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "value") and isinstance(value.value, str):
        value = value.value
    text = str(value).strip().lower()
    if not text:
        return None
    if _FAILURE_CLASSES and text not in _FAILURE_CLASSES:
        return "unknown"
    return text


# --------------------------------------------------------------------------
# Ledger writes


def _ledger(gates: dict) -> dict:
    ledger = gates.get("cost")
    if (
        not isinstance(ledger, dict)
        or not isinstance(ledger.get("lines"), dict)
        or not isinstance(ledger.get("truncated"), dict)
    ):
        ledger = {"v": LEDGER_VERSION, "lines": {}, "truncated": {}}
        gates["cost"] = ledger
    ledger["v"] = LEDGER_VERSION
    return ledger


def _line_entries(ledger: dict, line_id: str) -> list:
    lines = ledger["lines"]
    entries = lines.get(line_id)
    if not isinstance(entries, list):
        entries = []
        lines[line_id] = entries
    return entries


def _find(entries: list, run_id: str) -> dict | None:
    for entry in entries:
        if isinstance(entry, dict) and entry.get("run_id") == run_id:
            return entry
    return None


def _blank_entry(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "attempt": 0,
        "model": "",
        "role": "author",
        "kind": "work",
        "fix_round": 0,
        "started_at": None,
        "ended_at": None,
        "wall_secs": None,
        "tokens_in": None,
        "tokens_out": None,
        "tokens_est": 0,
        "token_basis": "estimate",
        "result": None,
        "failure_class": None,
        "reason_code": None,
        "quota": None,
    }


def _append(ledger: dict, line_id: str, entry: dict) -> None:
    entries = _line_entries(ledger, line_id)
    entries.append(entry)
    overflow = len(entries) - MAX_ATTEMPTS_PER_LINE
    if overflow > 0:
        del entries[:overflow]
        truncated = ledger["truncated"]
        current = _count(truncated.get(line_id)) or 0
        truncated[line_id] = current + overflow


def record_attempt_start(
    gates: dict,
    line_id: str,
    *,
    run_id: str,
    attempt: int,
    model: str,
    kind: str,
    fix_round: int,
    started_at: datetime | None,
    quota_before: dict | None = None,
    role: str = "author",
) -> dict:
    """Open (or re-open) the ledger entry for ``run_id``; returns the entry.

    Idempotent per ``run_id``: an existing entry is updated in place. When a
    line passes ``MAX_ATTEMPTS_PER_LINE`` entries the oldest is dropped and
    ``truncated[line_id]`` counts it.
    """
    ledger = _ledger(gates)
    line_id = str(line_id)
    run_id = str(run_id)
    entries = _line_entries(ledger, line_id)
    entry = _find(entries, run_id)
    if entry is None:
        entry = _blank_entry(run_id)
        _append(ledger, line_id, entry)
    entry["attempt"] = _count(attempt) or 0
    entry["model"] = _canonical(model)
    entry["role"] = str(role or "author")
    entry["kind"] = kind if kind in _KINDS else "work"
    entry["fix_round"] = _count(fix_round) or 0
    entry["started_at"] = _iso(started_at)
    if quota_before is not None:
        entry["quota_before"] = _clean_reading(quota_before)
    return entry


def record_attempt_end(
    gates: dict,
    line_id: str,
    *,
    run_id: str,
    ended_at: datetime,
    result: str,
    failure_class: str | None = None,
    reason_code: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    tokens_est: int = 0,
    quota_after: dict | None = None,
    model: str | None = None,
    attempt: int | None = None,
    kind: str | None = None,
    fix_round: int | None = None,
) -> dict:
    """Complete the entry for ``run_id`` (creating it if no start was written).

    An attempt that failed before it started (``MISSING_CREDENTIAL`` at
    launch) gets an entry with ``started_at=None`` and ``wall_secs=0``. An
    entry whose start time is unknown (the process restarted) keeps
    ``wall_secs=None``.
    """
    ledger = _ledger(gates)
    line_id = str(line_id)
    run_id = str(run_id)
    entries = _line_entries(ledger, line_id)
    entry = _find(entries, run_id)
    created = entry is None
    if entry is None:
        entry = _blank_entry(run_id)
        _append(ledger, line_id, entry)
    if model is not None:
        entry["model"] = _canonical(model)
    if attempt is not None:
        entry["attempt"] = _count(attempt) or 0
    if kind is not None:
        entry["kind"] = kind if kind in _KINDS else "work"
    if fix_round is not None:
        entry["fix_round"] = _count(fix_round) or 0

    entry["ended_at"] = _iso(ended_at)
    started = _parse_iso(entry.get("started_at"))
    ended = _parse_iso(entry["ended_at"])
    if created:
        entry["wall_secs"] = 0
    elif started is not None and ended is not None:
        entry["wall_secs"] = max(0, int((ended - started).total_seconds()))
    else:
        entry["wall_secs"] = None

    t_in = _count(tokens_in)
    t_out = _count(tokens_out)
    entry["tokens_in"] = t_in
    entry["tokens_out"] = t_out
    entry["tokens_est"] = _count(tokens_est) or 0
    entry["token_basis"] = "cli" if (t_in is not None or t_out is not None) else "estimate"

    entry["result"] = result if result in _RESULTS else "failed"
    entry["failure_class"] = _failure_class(failure_class)
    entry["reason_code"] = normalize_reason_code(reason_code)

    before = entry.pop("quota_before", None)
    after = _clean_reading(quota_after) if quota_after is not None else None
    entry["quota"] = quota_delta(before, after)
    return entry


# --------------------------------------------------------------------------
# CodexBar readings


def _clean_reading(reading: Any) -> dict | None:
    """Only the keys a reading may carry; never account, e-mail or money."""
    if not isinstance(reading, dict):
        return None
    provider = reading.get("provider")
    if not isinstance(provider, str):
        return None
    if reading.get("balance") is True:
        out: dict[str, Any] = {"provider": provider, "balance": True}
        if isinstance(reading.get("read_at"), str):
            out["read_at"] = reading["read_at"]
        return out
    used = reading.get("used_pct")
    return {
        "provider": provider,
        "used_pct": headroom_codexbar._used_percent(used),
        "resets_at": reading.get("resets_at") if isinstance(reading.get("resets_at"), str) else None,
        "updated_at": reading.get("updated_at") if isinstance(reading.get("updated_at"), str) else None,
        "read_at": reading.get("read_at") if isinstance(reading.get("read_at"), str) else None,
    }


def _primary_window(windows: Any) -> dict | None:
    if not isinstance(windows, list):
        return None
    plain = [w for w in windows if isinstance(w, dict)]
    for window in plain:
        if window.get("role") == "primary":
            return window
    for window in plain:
        if window.get("role") is None:
            return window
    return None


def default_snapshot_path() -> Path:
    """The CodexBar snapshot path, resolved at call time (not at import).

    ``headroom_codexbar.DEFAULT_SNAPSHOT_PATH`` is bound to ``Path.home()``
    when that module loads; resolving here means a changed HOME (and the unit
    tests' autouse override of this function) is honoured.
    """
    return Path.home() / ".conductor" / "usage" / "codexbar-snapshot.json"


def read_quota(
    model: str, *, path: Path | None = None, now: datetime | None = None
) -> dict | None:
    """The primary-window reading for ``model``'s provider, or None.

    Reads the CodexBar snapshot file (never the CLI, never the network) and
    the Worker -> provider table of ``headroom_codexbar``. Returns
    ``{"provider", "used_pct", "resets_at", "updated_at", "read_at"}``, or
    ``{"provider", "balance": True, "read_at"}`` for a balance provider. Never
    raises.
    """
    try:
        provider = headroom_codexbar.PROVIDER_BY_WORKER.get(_canonical(model))
        if provider is None:
            return None
        snapshot = Path(path) if path is not None else default_snapshot_path()
        payload = headroom_codexbar._read_snapshot(snapshot)
        if payload is None:
            return None
        moment = now if now is not None else datetime.now(timezone.utc)
        read_at = _iso(moment)
        rows = payload.get("providers")
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict) or row.get("provider") != provider:
                continue
            if row.get("kind") == "balance":
                return {"provider": provider, "balance": True, "read_at": read_at}
            status = row.get("status")
            if status not in headroom_codexbar._READABLE_STATUSES:
                return None
            window = _primary_window(row.get("windows"))
            if window is None:
                return None
            used = headroom_codexbar._used_percent(window.get("used_percent"))
            if used is None:
                return None
            reset = headroom_codexbar._timestamp(
                window.get("resets_at_utc", window.get("resetsAt"))
            )
            updated = headroom_codexbar._timestamp(
                row.get("updated_at", row.get("observed_at_utc"))
            )
            return {
                "provider": provider,
                "used_pct": used,
                "resets_at": _iso(reset),
                "updated_at": _iso(updated),
                "read_at": read_at,
            }
        return None
    except Exception:
        logger.debug("cost: CodexBar reading failed", exc_info=True)
        return None


def _fresh(reading: dict) -> bool:
    updated = _parse_iso(reading.get("updated_at"))
    read = _parse_iso(reading.get("read_at"))
    if updated is None or read is None:
        return False
    age = (read - updated).total_seconds()
    return 0 <= age <= QUOTA_FRESH_SECONDS


def quota_delta(before: dict | None, after: dict | None) -> dict | None:
    """``{"provider","before_pct","after_pct","delta_pct","note"}`` or None.

    ``delta_pct`` is filled only when both readings are fresh (within 900 s of
    when they were read), share one ``resets_at`` and the snapshot moved on
    between them. Otherwise ``note`` says why: ``stale``, ``window_reset``,
    ``unchanged``, ``no_snapshot`` or ``balance_provider``.
    """
    before = _clean_reading(before) if before is not None else None
    after = _clean_reading(after) if after is not None else None
    if before is None and after is None:
        return None
    provider = (after or before or {}).get("provider")
    before_pct = before.get("used_pct") if before else None
    after_pct = after.get("used_pct") if after else None
    out: dict[str, Any] = {
        "provider": provider,
        "before_pct": before_pct,
        "after_pct": after_pct,
        "delta_pct": None,
        "note": "ok",
    }
    if (before and before.get("balance")) or (after and after.get("balance")):
        out["before_pct"] = None
        out["after_pct"] = None
        out["note"] = "balance_provider"
        return out
    if before is None or after is None or before_pct is None or after_pct is None:
        out["note"] = "no_snapshot"
        return out
    if not (_fresh(before) and _fresh(after)):
        out["note"] = "stale"
        return out
    if before.get("resets_at") != after.get("resets_at"):
        out["note"] = "window_reset"
        return out
    b_upd = _parse_iso(before.get("updated_at"))
    a_upd = _parse_iso(after.get("updated_at"))
    if b_upd is None or a_upd is None or not a_upd > b_upd:
        out["note"] = "unchanged"
        return out
    out["delta_pct"] = round(after_pct - before_pct, 3)
    return out


# --------------------------------------------------------------------------
# Reading a dispatch


def _as_dispatch(dispatch: Any) -> Any:
    if isinstance(dispatch, dict):
        return Dispatch.from_dict(dispatch)
    return dispatch


def _gates(dispatch: Any) -> dict:
    gates = getattr(dispatch, "gates", None)
    return gates if isinstance(gates, dict) else {}


def _gate_line(dispatch: Any, line_id: str) -> dict:
    lines = _gates(dispatch).get("lines")
    snap = lines.get(line_id) if isinstance(lines, dict) else None
    return snap if isinstance(snap, dict) else {}


def _merged_ids(dispatch: Any) -> set[str]:
    integration = _gates(dispatch).get("integration")
    merged = integration.get("merged_lines") if isinstance(integration, dict) else None
    if not isinstance(merged, list):
        return set()
    return {str(item) for item in merged}


def _status_value(status: Any) -> str:
    return str(getattr(status, "value", status) or "")


# A line that finished well. ``passed`` is a PASSED line of a COMPLETE
# dispatch that was never merged (dev mode delivers without an integration
# merge); it is not still ``open``.
SUCCESS_OUTCOMES = frozenset({"merged", "passed"})


def line_outcome(dispatch: Any, line: Any) -> str:
    """``merged`` | ``passed`` | ``blocked`` | ``failed`` | ``open`` (PRD 4.2 order)."""
    dispatch = _as_dispatch(dispatch)
    line_id = str(getattr(line, "id", line))
    if line_id in _merged_ids(dispatch):
        return "merged"
    snap = _gate_line(dispatch, line_id)
    machine = snap.get("machine")
    stage = machine.get("stage") if isinstance(machine, dict) else None
    if snap.get("phase") == "merged" or stage == "merged":
        return "merged"
    status = _status_value(getattr(line, "status", ""))
    if status == LineStatus.BLOCKED.value:
        return "blocked"
    if status == LineStatus.FAILED.value:
        return "failed"
    if (
        status == LineStatus.PASSED.value
        and _status_value(getattr(dispatch, "state", "")) == DispatchState.COMPLETE.value
    ):
        return "passed"
    return "open"


def _ledger_lines(dispatch: Any) -> dict | None:
    ledger = _gates(dispatch).get("cost")
    if isinstance(ledger, dict) and isinstance(ledger.get("lines"), dict):
        return ledger["lines"]
    return None


def _basis(dispatch: Any) -> str:
    return "ledger" if _ledger_lines(dispatch) is not None else "legacy"


def _legacy_entries(dispatch: Any, line: Line) -> list[dict]:
    """Attempts rebuilt from ``worker_recovery`` events and ``outcomes``.

    Only counts and classes are known: wall time and tokens are ``None``.
    """
    out: list[dict] = []
    recovery = _gates(dispatch).get("worker_recovery")
    entry = recovery.get(line.id) if isinstance(recovery, dict) else None
    events = entry.get("events") if isinstance(entry, dict) else None
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        out.append(
            {
                "run_id": str(event.get("run_id") or ""),
                "attempt": _count(event.get("attempt")) or 0,
                "model": _canonical(event.get("author")),
                "role": "author",
                "kind": "work",
                "fix_round": 0,
                "started_at": None,
                "ended_at": None,
                "wall_secs": None,
                "tokens_in": None,
                "tokens_out": None,
                "tokens_est": None,
                "token_basis": None,
                "result": "failed",
                "failure_class": _failure_class(event.get("failure_class")),
                "reason_code": normalize_reason_code(event.get("reason")),
                "quota": None,
            }
        )
    outcome = getattr(dispatch, "outcomes", {}).get(line.id)
    if isinstance(outcome, Outcome):
        out.append(
            {
                "run_id": "",
                "attempt": 0,
                "model": _canonical(outcome.model),
                "role": "author",
                "kind": "work",
                "fix_round": 0,
                "started_at": None,
                "ended_at": None,
                "wall_secs": None,
                "tokens_in": None,
                "tokens_out": None,
                "tokens_est": None,
                "token_basis": None,
                "result": "ok",
                "failure_class": None,
                "reason_code": None,
                "quota": None,
            }
        )
    return out


def _entries(dispatch: Any, line: Line, basis: str) -> list[dict]:
    if basis == "legacy":
        return _legacy_entries(dispatch, line)
    lines = _ledger_lines(dispatch) or {}
    raw = lines.get(line.id)
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, dict):
            entry = dict(item)
            entry.pop("quota_before", None)
            out.append(entry)
    return out


def _line_reason(dispatch: Any, line: Line) -> str | None:
    snap = _gate_line(dispatch, line.id)
    return normalize_reason_code(snap.get("reason")) or normalize_reason_code(
        line.failure_reason
    )


def _is_infra(entry: dict) -> bool:
    return (
        entry.get("failure_class") in INFRA_FAILURE_CLASSES
        or entry.get("reason_code") in infra_reason_codes()
    )


def _classify(dispatch: Any, line: Line, outcome: str, entries: list[dict]) -> list[dict]:
    """Add ``use`` and ``waste_class`` to each entry (PRD 4.2)."""
    author = _canonical(line.assigned_model)
    aborted = _status_value(getattr(dispatch, "state", "")) == DispatchState.ABORT.value
    succeeded = outcome in SUCCESS_OUTCOMES
    line_reason = _line_reason(dispatch, line) if not succeeded else None
    useful_taken = False
    out = []
    for entry in entries:
        row = dict(entry)
        mine = bool(author) and row.get("model") == author
        ok = row.get("result") == "ok"
        use = USE_WASTE
        if succeeded and mine and ok:
            if not useful_taken and row.get("kind") != "fix":
                use = USE_USEFUL
                useful_taken = True
            else:
                use = USE_REWORK
        waste_class = None
        if use == USE_WASTE:
            if row.get("result") is None and row.get("ended_at") is None:
                waste_class = WASTE_IN_PROGRESS
            else:
                waste_class = (
                    row.get("failure_class")
                    or row.get("reason_code")
                    or (WASTE_SUPERSEDED if succeeded and not mine else None)
                    or (WASTE_ABANDONED if aborted else None)
                    or line_reason
                    or WASTE_UNKNOWN
                )
        row["use"] = use
        row["waste_class"] = waste_class
        out.append(row)
    return out


def _add(total: int | None, value: Any) -> int | None:
    number = _count(value)
    if number is None:
        return total
    return number if total is None else total + number


def _cli_tokens(entry: dict) -> int | None:
    if entry.get("token_basis") != "cli":
        return None
    return (_count(entry.get("tokens_in")) or 0) + (_count(entry.get("tokens_out")) or 0)


def _fix_rounds(dispatch: Any, line: Line, entries: list[dict], basis: str) -> int:
    if basis == "ledger":
        rounds = {e.get("fix_round") for e in entries if e.get("kind") == "fix"}
        if rounds:
            return len(rounds)
    snap = _gate_line(dispatch, line.id)
    return _count(snap.get("fix_round")) or 0


class _ModelAcc:
    __slots__ = (
        "attempts", "lines", "merged", "merged_ledger", "spent_wall",
        "spent_cli", "spent_est", "waste_wall", "waste_attempts", "wall_ledger",
    )

    def __init__(self) -> None:
        self.attempts = 0
        self.lines = 0
        self.merged = 0
        self.merged_ledger = 0
        self.spent_wall: int | None = None
        self.spent_cli: int | None = None
        self.spent_est: int | None = None
        self.waste_wall: int | None = None
        self.waste_attempts = 0
        self.wall_ledger = 0


class _WasteAcc:
    __slots__ = ("attempts", "wall", "cli", "est", "by_model")

    def __init__(self) -> None:
        self.attempts = 0
        self.wall: int | None = None
        self.cli: int | None = None
        self.est: int | None = None
        self.by_model: dict[str, int] = {}


class _Acc:
    """Running totals over one or many dispatches."""

    def __init__(self) -> None:
        self.models: dict[str, _ModelAcc] = {}
        self.waste: dict[str, _WasteAcc] = {}
        self.fix_total = 0
        self.merged = 0

    def model(self, name: str) -> _ModelAcc:
        acc = self.models.get(name)
        if acc is None:
            acc = _ModelAcc()
            self.models[name] = acc
        return acc

    def add_line(self, outcome: str, author: str, rows: list[dict], fix_rounds: int, basis: str) -> None:
        touched = {row.get("model") or "" for row in rows}
        if author:
            touched.add(author)
        touched.discard("")
        for name in touched:
            self.model(name).lines += 1
        if outcome == "merged":
            self.merged += 1
            self.fix_total += fix_rounds
            if author:
                acc = self.model(author)
                acc.merged += 1
                if basis == "ledger":
                    acc.merged_ledger += 1
        for row in rows:
            name = row.get("model") or ""
            if not name:
                continue
            acc = self.model(name)
            acc.attempts += 1
            acc.spent_wall = _add(acc.spent_wall, row.get("wall_secs"))
            acc.spent_cli = _add(acc.spent_cli, _cli_tokens(row))
            acc.spent_est = _add(acc.spent_est, row.get("tokens_est"))
            if row["use"] == USE_WASTE:
                acc.waste_attempts += 1
                acc.waste_wall = _add(acc.waste_wall, row.get("wall_secs"))
                cls = row.get("waste_class") or WASTE_UNKNOWN
                waste = self.waste.get(cls)
                if waste is None:
                    waste = _WasteAcc()
                    self.waste[cls] = waste
                waste.attempts += 1
                waste.wall = _add(waste.wall, row.get("wall_secs"))
                waste.cli = _add(waste.cli, _cli_tokens(row))
                waste.est = _add(waste.est, row.get("tokens_est"))
                waste.by_model[name] = waste.by_model.get(name, 0) + 1

    def by_model(self) -> list[dict]:
        rows = []
        for name in sorted(self.models):
            acc = self.models[name]
            cost = None
            if acc.merged_ledger and acc.spent_wall is not None:
                cost = acc.spent_wall / acc.merged_ledger
            if acc.spent_wall:
                share = (acc.waste_wall or 0) / acc.spent_wall
            elif acc.attempts:
                share = acc.waste_attempts / acc.attempts
            else:
                share = None
            rows.append(
                {
                    "model": name,
                    "attempts": acc.attempts,
                    "lines": acc.lines,
                    "merged": acc.merged,
                    "spent_wall_secs": acc.spent_wall,
                    "spent_tokens_cli": acc.spent_cli,
                    "spent_tokens_est": acc.spent_est,
                    "cost_per_merged_line_wall_secs": cost,
                    "merge_yield": (acc.merged / acc.lines) if acc.lines else None,
                    "waste_share": share,
                }
            )
        return rows

    def waste_by_class(self) -> list[dict]:
        rows = []
        infra_codes = infra_reason_codes()
        for cls in sorted(self.waste, key=lambda c: (-self.waste[c].attempts, c)):
            acc = self.waste[cls]
            rows.append(
                {
                    "class": cls,
                    "attempts": acc.attempts,
                    "wall_secs": acc.wall,
                    "tokens_cli": acc.cli,
                    "tokens_est": acc.est,
                    "by_model": dict(sorted(acc.by_model.items())),
                    "infrastructure": cls in INFRA_FAILURE_CLASSES or cls in infra_codes,
                }
            )
        return rows

    def fix_rounds(self) -> dict:
        return {
            "total": self.fix_total,
            "per_merged_line": (self.fix_total / self.merged) if self.merged else None,
        }


def _line_row(line: Line, outcome: str, rows: list[dict], fix_rounds: int, basis: str) -> dict:
    wall: int | None = None
    cli: int | None = None
    est: int | None = None
    groups: dict[tuple[str, str], dict] = {}
    class_counts: dict[str, int] = {}
    for row in rows:
        wall = _add(wall, row.get("wall_secs"))
        cli = _add(cli, _cli_tokens(row))
        est = _add(est, row.get("tokens_est"))
        if row["use"] != USE_WASTE:
            continue
        cls = row.get("waste_class") or WASTE_UNKNOWN
        class_counts[cls] = class_counts.get(cls, 0) + 1
        key = (row.get("model") or "", cls)
        group = groups.get(key)
        if group is None:
            group = {"model": key[0], "class": cls, "attempts": 0, "wall_secs": None}
            groups[key] = group
        group["attempts"] += 1
        group["wall_secs"] = _add(group["wall_secs"], row.get("wall_secs"))
    dominant = None
    if class_counts:
        # Most attempts first; ties go to the class seen first.
        dominant = max(class_counts, key=lambda c: class_counts[c])
    return {
        "line_id": line.id,
        "outcome": outcome,
        "author": _canonical(line.assigned_model) or None,
        "attempts": len(rows),
        "fix_rounds": fix_rounds,
        "wall_secs": wall if basis == "ledger" else None,
        "tokens_cli": cli if basis == "ledger" else None,
        "tokens_est": est if basis == "ledger" else None,
        "waste_class": dominant,
        "waste": [groups[key] for key in sorted(groups)],
    }


def _walk(dispatch: Any, acc: _Acc) -> tuple[str, dict, list[dict], dict]:
    """Classify one dispatch into ``acc``; returns basis, counts, line rows, attempts."""
    basis = _basis(dispatch)
    counts = {"lines": 0, "merged": 0, "passed": 0, "blocked": 0, "failed": 0, "open": 0}
    line_rows: list[dict] = []
    attempts: dict[str, list[dict]] = {}
    for line in sorted(getattr(dispatch, "lines", []) or [], key=lambda item: item.id):
        outcome = line_outcome(dispatch, line)
        counts["lines"] += 1
        counts[outcome] += 1
        entries = _entries(dispatch, line, basis)
        rows = _classify(dispatch, line, outcome, entries)
        fix_rounds = _fix_rounds(dispatch, line, entries, basis)
        acc.add_line(outcome, _canonical(line.assigned_model), rows, fix_rounds, basis)
        line_rows.append(_line_row(line, outcome, rows, fix_rounds, basis))
        attempts[line.id] = rows
    return basis, counts, line_rows, attempts


def dispatch_cost_report(dispatch: Any) -> dict:
    """The full report for ``GET /{id}/cost`` and ``conductor dispatch cost ID``."""
    dispatch = _as_dispatch(dispatch)
    acc = _Acc()
    basis, counts, line_rows, attempts = _walk(dispatch, acc)
    return {
        "dispatch_id": str(dispatch.id),
        "state": _status_value(dispatch.state),
        "basis": basis,
        "counts": counts,
        "lines": line_rows,
        "attempts": attempts,
        "by_model": acc.by_model(),
        "waste_by_class": acc.waste_by_class(),
        "fix_rounds": acc.fix_rounds(),
        "quota_note": QUOTA_NOTE,
    }


def dispatch_cost_summary(dispatch: Any) -> dict:
    """Line-level only, for ``DispatchSummary.cost`` and ``_public_gates``."""
    dispatch = _as_dispatch(dispatch)
    basis, counts, line_rows, _ = _walk(dispatch, _Acc())
    return {"v": LEDGER_VERSION, "basis": basis, "counts": counts, "lines": line_rows}


def aggregate_cost_report(dispatches: Iterable, *, limit: int | None = None) -> dict:
    """``by_model`` and ``waste_by_class`` over the first ``limit`` dispatches."""
    acc = _Acc()
    bases = {"ledger": 0, "legacy": 0}
    seen = 0
    for dispatch in dispatches:
        if limit is not None and seen >= limit:
            break
        seen += 1
        basis, _, _, _ = _walk(_as_dispatch(dispatch), acc)
        bases[basis] += 1
    return {
        "limit": limit,
        "dispatches": seen,
        "basis": bases,
        "by_model": acc.by_model(),
        "waste_by_class": acc.waste_by_class(),
        "fix_rounds": acc.fix_rounds(),
        "quota_note": QUOTA_NOTE,
    }


def cost_per_merged_line_wall(
    dispatches: Iterable, *, exclude_infra: bool = True
) -> dict[str, tuple[float | None, int]]:
    """model -> (spent wall_secs / merged lines, merged lines).

    Ledger-basis dispatches only (legacy rows have no wall time). With
    ``exclude_infra`` the attempts that failed on our own environment
    (``INFRA_*``) are not charged to the model.
    """
    spent: dict[str, int] = {}
    merged: dict[str, int] = {}
    for raw in dispatches:
        dispatch = _as_dispatch(raw)
        if _basis(dispatch) != "ledger":
            continue
        for line in getattr(dispatch, "lines", []) or []:
            outcome = line_outcome(dispatch, line)
            author = _canonical(line.assigned_model)
            if outcome == "merged" and author:
                merged[author] = merged.get(author, 0) + 1
            for entry in _entries(dispatch, line, "ledger"):
                name = entry.get("model") or ""
                if not name or (exclude_infra and _is_infra(entry)):
                    continue
                spent[name] = spent.get(name, 0) + (_count(entry.get("wall_secs")) or 0)
    out: dict[str, tuple[float | None, int]] = {}
    for name in sorted(set(spent) | set(merged)):
        count = merged.get(name, 0)
        out[name] = ((spent.get(name, 0) / count) if count else None, count)
    return out


class CostStats:
    """``selection.CostStatsPort`` over the dispatch store, cached per ``ttl``.

    ``store`` has ``async list(limit=...) -> list[Dispatch]``. One read per
    ``ttl_seconds`` serves every model and every select. A model with fewer
    than ``min_merged`` merged lines answers None (no penalty).
    """

    def __init__(
        self,
        store: Any,
        *,
        min_merged: int = 3,
        ttl_seconds: float = 60.0,
        limit: int = 500,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._min_merged = min_merged
        self._ttl = ttl_seconds
        self._limit = limit
        self._clock = clock
        self._table: dict[str, tuple[float | None, int]] | None = None
        self._at: float | None = None
        self._lock: asyncio.Lock | None = None

    async def _rows(self) -> dict[str, tuple[float | None, int]]:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            moment = self._clock()
            if (
                self._table is not None
                and self._at is not None
                and moment - self._at < self._ttl
            ):
                return self._table
            try:
                rows = await self._store.list(limit=self._limit)
                table = cost_per_merged_line_wall(rows or [], exclude_infra=True)
            except Exception:
                logger.warning("cost: could not read dispatch costs", exc_info=True)
                table = {}
            self._table = table
            self._at = moment
            return table

    async def cost_per_merged_line(self, model: str) -> float | None:
        table = await self._rows()
        value, merged = table.get(_canonical(model), (None, 0))
        if value is None or merged < self._min_merged:
            return None
        return value
