"""CodexBar-backed headroom for the auto-dispatch selector.

Implements the ``HeadroomPort`` protocol (``src/tools/auto_dispatch/selection.py``):
how much of a model's quota is left, as a fraction in [0, 1].

The source is Conductor's own CodexBar snapshot, the file written by
``src/conductor_cli/codexbar_snapshot.py``. This module never calls the
``codexbar`` CLI and never reaches the network: it reads one local file.

Two rules decide the answer, and both err towards "there is headroom":

* a Worker with no reading is not a Worker without quota. A missing file, an
  unreadable file, a provider row that was observed longer ago than the
  freshness limit or dated in the future, a provider with no window, and an
  unknown model all return ``1.0`` and one debug line. A selector that skipped
  a model because nobody had read its usage would silently drop workers off
  the roster.
* the smallest remaining window wins. A provider whose 5-hour window is spent
  but whose weekly window is wide is still nearly out of quota, so the reading
  is ``1 - max(used_percent) / 100`` over that provider's windows.

The windows are read the way ``native_runtime.usage_headroom`` reads them, so
the two readers ``default_headroom`` combines cannot disagree about whether a
Worker is spent:

* a row whose ``status`` is not ok (the writer's ``unknown`` for a provider it
  could not read) is no reading, not an empty quota;
* a window whose reset time has passed no longer limits anything;
* a full ``tertiary``/``extra`` window meters one bucket of the plan (Cursor's
  "Third Party" models), so it makes the Worker low, never spent;
* a full plan window on a provider with ``credits_remaining`` left is low, not
  spent: work keeps running on the credits. Whether *review* may spend them is
  not decided here; ``plan_status`` reports the full plan and N63's selector
  rule plus ``CONDUCTOR_DISPATCH_ALLOW_PAID_CREDITS`` decide.

The snapshot is read for provider ids, row status, window percentages, window
roles and reset times, and whether credits remain. No account e-mail or token
is read, logged or returned, and the credit amount itself is never surfaced:
only "some are left" leaves this module.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# A snapshot older than this is not trusted: the windows it reports describe a
# quota state that has since moved on.
DEFAULT_MAX_AGE_SECONDS = 30 * 60

# How long a read is reused before the file is read again. The selector asks
# for the headroom of every eligible model, so the snapshot is read once per
# window rather than once per model.
DEFAULT_REFRESH_SECONDS = 30.0

# The snapshot file is small and is not expected to grow without bound.
_MAX_BYTES = 262_144

# Worker name -> CodexBar provider id. CodexBar spells GLM "zai"; the rest
# match the Worker name. CodexBar also knows providers Conductor has no Worker
# for (manus, mimo); those are simply never asked about.
PROVIDER_BY_WORKER: dict[str, str] = {
    "kimi": "kimi",
    "glm": "zai",
    "grok": "grok",
    "cursor": "cursor",
    "stepfun": "stepfun",
    "deepseek": "deepseek",
    "minimax": "minimax",
    "doubao": "doubao",
    "codex": "codex",
    "claude": "claude",
}

DEFAULT_SNAPSHOT_PATH = Path.home() / ".conductor" / "usage" / "codexbar-snapshot.json"

# Row statuses that carry a reading; the same set ``usage_headroom`` accepts.
# A row with no status comes from an older snapshot and is read as before.
_READABLE_STATUSES = frozenset({None, "ok", "success", "partial"})

# Windows that meter one bucket of a plan, not the plan itself; the same roles
# ``usage_headroom._BUCKET_ROLES`` names.
BUCKET_ROLES = frozenset({"tertiary", "extra"})

# What a "low" Worker reads as. It is the figure the selector gives the
# snapshot's ``low`` state (``selection._SNAPSHOT_FRACTION``), so a full bucket
# or a full plan on credits ranks the same through either reader.
LOW_HEADROOM = 0.25


def _worker_name(model: str) -> str:
    """The Worker a name refers to, whatever spelling the caller used.

    ``roles.canonical_provider`` is the one alias table: ``zcode``, ``zhipu``,
    ``zai`` and ``glm`` are one Worker and reach the same ``zai`` row. Without
    the fold an alias would look like a Worker nobody has a reading for, which
    answers 1.0 -- full quota -- for a Worker that is in fact nearly spent.
    """
    from src.server.services.autonomy.roles import canonical_provider

    return canonical_provider(model)


def _now(now: datetime | None) -> datetime | None:
    """The current instant in UTC, or None when the injected clock is unusable.

    A clock that cannot be normalised is not something to argue about: the
    adapter answers 1.0 rather than raising into the selector.
    """
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    try:
        return now.astimezone(timezone.utc)
    except Exception:
        # Same rule as the row timestamps: a clock that cannot be normalised is
        # unknown data, not an error for the caller.
        return None


def _timestamp(value: Any) -> datetime | None:
    """Parse the snapshot's ISO-8601 timestamp, or None when it is unusable.

    Any failure to parse or to normalise the value to UTC makes the row
    *unknown data*, never an error for the caller: a damaged timestamp is not
    a reason to interrupt the selector.
    """
    if not isinstance(value, str) or not value or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        # Normalising to UTC can overflow for an extreme offset: a timestamp at
        # the very edge of the calendar has no representable UTC instant, and
        # the arithmetic raises rather than returning something. That is a
        # damaged timestamp, not a reason to interrupt the caller.
        return parsed.astimezone(timezone.utc)
    except Exception:
        # The rule is fail open for *any* failure to parse or normalise a
        # timestamp, OverflowError included. The body above is three lines of
        # parsing, so nothing else can hide in here.
        return None


def _fresh_observation(value: Any, *, now: datetime, max_age_seconds: int) -> bool:
    """Whether a provider row's observation is usable.

    A row counts only when it was observed **now or earlier**. The native-agent
    routing allows a minute of clock skew; this adapter does not: a row dated
    in the future is unknown data, not fresh data, so it is skipped and the
    provider answers 1.0.
    """
    observed = _timestamp(value)
    if observed is None:
        return False
    age = now - observed
    return timedelta(0) <= age <= timedelta(seconds=max_age_seconds)


def _used_percent(value: Any) -> float | None:
    """One window's used percentage, or None when it is not a usable number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value:  # NaN
        return None
    if value < 0:
        return None
    return float(min(value, 100.0))


def _read_snapshot(path: Path) -> dict[str, Any] | None:
    """Read the snapshot file defensively, or None when it cannot be used."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
            return None
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(fd, min(65536, _MAX_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_BYTES:
                return None
            chunks.append(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _provider_rows(
    payload: dict[str, Any], *, now: datetime, max_age_seconds: int
) -> dict[str, dict[str, Any]]:
    """Index the snapshot's readable provider rows by provider id.

    A row is kept only when its **own** observation time is fresh. The file's
    ``updatedAt`` says when the file was written; the row's ``updated_at`` says
    when that provider's quota was actually seen. Only the second one says
    whether the numbers are still worth acting on: CodexBar writes the file on
    its own refresh interval, so a file written a minute ago can carry a
    reading that is hours old. A row whose status says the read failed is
    dropped too: its windows, if any, are not a measurement.
    """
    rows: dict[str, dict[str, Any]] = {}
    providers = payload.get("providers")
    if not isinstance(providers, list):
        return rows
    for row in providers:
        if not isinstance(row, dict):
            continue
        provider = row.get("provider")
        if not isinstance(provider, str) or not provider:
            continue
        status = row.get("status")
        if not isinstance(status, (str, type(None))) or status not in _READABLE_STATUSES:
            continue
        observed = row.get("updated_at", row.get("observed_at_utc"))
        if not _fresh_observation(observed, now=now, max_age_seconds=max_age_seconds):
            continue
        windows = row.get("windows")
        if not isinstance(windows, list):
            windows = []
        rows[provider] = {
            "windows": [w for w in windows if isinstance(w, dict)],
            "credits": _has_credits(row.get("credits_remaining")),
        }
    return rows


def _has_credits(value: Any) -> bool:
    """Whether paid credits are left; the same test ``usage_headroom`` applies."""
    return type(value) in (int, float) and value == value and value > 0


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _reading(
    windows: list[dict[str, Any]], *, now: datetime, credits: bool
) -> tuple[float, dict[str, Any]] | None:
    """Remaining fraction and plan status of one row, or None when it has no window.

    Plan windows (primary, secondary, or no role in an older snapshot) give
    ``1 - max(used) / 100``; with credits left that is never below
    ``LOW_HEADROOM``, because the Worker keeps working on the credits. Bucket
    windows can pull the reading down to ``LOW_HEADROOM`` but no further. A
    window whose reset time has passed, or whose reset time cannot be read, is
    skipped, as ``usage_headroom`` skips it.
    """
    plan_used: list[float] = []
    bucket_used: list[float] = []
    plan_resets: list[datetime] = []
    for window in windows:
        used = _used_percent(window.get("used_percent"))
        if used is None:
            continue
        raw_reset = window.get("resets_at_utc", window.get("resetsAt"))
        reset = None
        if raw_reset is not None:
            reset = _timestamp(raw_reset)
            if reset is None or reset <= now:
                continue
        if window.get("role") in BUCKET_ROLES:
            bucket_used.append(used)
            continue
        plan_used.append(used)
        if used >= 100.0 and reset is not None:
            plan_resets.append(reset)
    if not plan_used and not bucket_used:
        return None
    remaining = 1.0
    if plan_used:
        remaining = 1.0 - max(plan_used) / 100.0
        if credits:
            remaining = max(remaining, LOW_HEADROOM)
    if bucket_used:
        remaining = min(remaining, max(1.0 - max(bucket_used) / 100.0, LOW_HEADROOM))
    plan_full = bool(plan_used) and max(plan_used) >= 100.0
    status = {
        "plan_full": plan_full,
        "credits": credits,
        "resets_at": _iso_utc(max(plan_resets)) if plan_full and plan_resets else None,
    }
    return remaining, status


class CodexBarHeadroom:
    """Headroom read from Conductor's CodexBar snapshot file.

    ``remaining(model)`` is the fraction of that Worker's quota left. It is
    1.0 whenever the snapshot does not say otherwise: the selector treats a
    model without headroom as out of quota, and "we did not read it" is not the
    same as "it is used up".
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
        now: datetime | None = None,
        clock: Any = None,
    ) -> None:
        self._path = (
            Path(path).expanduser() if path is not None else DEFAULT_SNAPSHOT_PATH
        )
        self._max_age_seconds = max_age_seconds
        self._refresh_seconds = refresh_seconds
        self._now = now
        self._clock = clock if clock is not None else time.monotonic
        self._cache: dict[str, tuple[float, dict[str, Any]]] | None = None
        self._cache_at: float | None = None

    async def remaining(self, model: str) -> float:
        reading = await self._reading_for(model)
        return 1.0 if reading is None else reading[0]

    async def plan_status(self, model: str) -> dict | None:
        """``{"plan_full", "credits", "resets_at"}`` from this file, or None.

        The same shape ``UsageSnapshotHeadroom.plan_status`` returns, so the
        N63 review rule sees a full plan even when only this reader has a
        fresh row for it. Without it a Worker this reader floors to "low" on
        credits would review on those credits with the flag off.
        """
        reading = await self._reading_for(model)
        return None if reading is None else dict(reading[1])

    async def _reading_for(self, model: str) -> tuple[float, dict[str, Any]] | None:
        provider = PROVIDER_BY_WORKER.get(_worker_name(model))
        if provider is None:
            logger.debug("headroom: %r is not a known Worker, assuming full", model)
            return None
        readings = await self._readings()
        reading = readings.get(provider)
        if reading is None:
            logger.debug(
                "headroom: %s has no fresh usable window in the snapshot "
                "(reason=no-fresh-window), assuming full",
                provider,
            )
        return reading

    async def _readings(self) -> dict[str, tuple[float, dict[str, Any]]]:
        """Remaining fraction and plan status per provider, from the fresh snapshot."""
        moment = self._clock()
        if (
            self._cache is not None
            and self._cache_at is not None
            and moment - self._cache_at < self._refresh_seconds
        ):
            return self._cache
        readings = self._read_snapshot()
        self._cache = readings
        self._cache_at = moment
        return readings

    def _read_snapshot(self) -> dict[str, tuple[float, dict[str, Any]]]:
        payload = _read_snapshot(self._path)
        if payload is None:
            logger.debug(
                "headroom: no usable CodexBar snapshot (reason=unreadable), assuming full"
            )
            return {}
        # Freshness is decided per provider row, never from the snapshot's root
        # timestamp. The producer fixes ``updatedAt`` when collection starts
        # and then reads the providers one by one, so a root that is already
        # older than the window can still sit next to a row that is fresh.
        # Judging the root would throw that fresh row away.
        now = _now(self._now)
        if now is None:
            logger.debug(
                "headroom: the clock cannot be used (reason=unusable-clock), "
                "assuming full"
            )
            return {}
        readings: dict[str, tuple[float, dict[str, Any]]] = {}
        for provider, row in _provider_rows(
            payload,
            now=now,
            max_age_seconds=self._max_age_seconds,
        ).items():
            reading = _reading(row["windows"], now=now, credits=row["credits"])
            if reading is not None:
                readings[provider] = reading
        return readings


# How long one drive trusts a plan status it already read (N63-1). A review
# pick asks the same model's plan status several times (the headroom rule,
# then the park's blockers), and each ask merges two readers, one of which
# reads a file. Within this window every ask of one drive gets the same
# answer, so one pick cannot see the plan full and then not full; past it a
# long drive still notices a plan that reset.
DEFAULT_PLAN_STATUS_TTL_SECONDS = 300.0


class PlanStatusCache:
    """A per-drive memo of ``plan_status`` in front of any headroom port.

    ``remaining`` is passed through unchanged (the selector's own readers
    already cache it). ``plan_status`` is read once per model and kept for
    ``ttl_seconds``; an answer of ``None`` is kept too, so a model no reader
    knows is not re-read on every ask. A port without ``plan_status`` answers
    ``None``, as the selector expects. A reader that raises is not cached:
    the error goes to the caller, and the next ask reads again. Each caller
    gets a copy, so a caller that edits the dict cannot change the memo.

    Build one per drive (``DispatchService._machine``): the memo is
    deliberately not process-wide.
    """

    def __init__(
        self,
        inner: Any,
        *,
        ttl_seconds: float = DEFAULT_PLAN_STATUS_TTL_SECONDS,
        clock: Any = None,
    ) -> None:
        self.inner = inner
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock if clock is not None else time.monotonic
        self._memo: dict[str, tuple[float, dict | None]] = {}

    async def remaining(self, model: str) -> float:
        return await self.inner.remaining(model)

    async def plan_status(self, model: str) -> dict | None:
        key = str(model or "").strip().lower()
        moment = self._clock()
        cached = self._memo.get(key)
        if cached is not None and moment - cached[0] < self._ttl_seconds:
            return None if cached[1] is None else dict(cached[1])
        reader = getattr(self.inner, "plan_status", None)
        status = await reader(model) if reader is not None else None
        kept = dict(status) if isinstance(status, dict) else None
        self._memo[key] = (moment, kept)
        return None if kept is None else dict(kept)

    def __getattr__(self, name: str) -> Any:
        # Anything else the selector or a test reads (``ports``, ``resolved``)
        # is the wrapped port's.
        if name == "inner":
            raise AttributeError(name)
        return getattr(self.inner, name)
