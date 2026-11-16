"""Operator notification events for the Super-agent.

Five conditions emit an event: a Ticket that needs the user, a failure, a
Ticket stuck past a threshold, a dead daemon, and a worker that is out of
quota. The same condition does not emit twice. When the condition clears,
the event leaves the inbox, so the badge is the number of conditions that
are still true, and a later recurrence can notify again.

A scan walks every page. Stopping at the first page would leave later
rows undiscovered on every pass, and would also keep stale alerts, because
a partial page must not delete rows it did not see. A pass shows at most a
few banners; the ones past that cap stay pending and the next pass shows
them. They are not dropped when the inbox row is de-duplicated.

The threshold for a stuck Ticket is ``CONDUCTOR_TICKET_STUCK_SECONDS``
(default 1800) for a queued, waiting, or blocked Ticket, and
``CONDUCTOR_TICKET_STUCK_RUNNING_SECONDS`` (default 7200, the watchdog's
running stall) for a running one, whose clock is ``started_at``. The quiet
time for a dead daemon is ``CONDUCTOR_DAEMON_DEAD_SECONDS`` (default 120,
the same window as the readiness heartbeat). A failure is an alert for
``CONDUCTOR_FAILURE_NOTIFY_SECONDS`` after it happened (default 86400), so
the failure history is not one banner per old Ticket. ``0``, ``off``,
``false``, ``no`` or an empty value turns that check (or, for the running
threshold, running Tickets) off. Any other non-number keeps the default.

These checks are live by default: the autonomy scheduler pass, which the
app lifespan starts, runs them every pass; nothing runs on import.

While the app-data pool is open, events are rows in ``autonomy_effects``
(see ``database/autonomy/notifications.py``). The scheduler is the single
writer; every worker reads the same rows for the badge. When the pool is
not open, a process-local store de-duplicates for tests. That fallback is
not shared across workers.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.server.database import pool
from src.server.database.autonomy import notifications as notification_db
from src.server.services.autonomy.notifier_macos import deliver as macos_deliver

logger = logging.getLogger(__name__)

KIND_NEEDS_USER = "needs_user"
KIND_FAILURE = "failure"
KIND_STUCK = "stuck"
KIND_DEAD_DAEMON = "dead_daemon"
KIND_WORKER_QUOTA = "worker_quota"

STUCK_THRESHOLD_ENV = "CONDUCTOR_TICKET_STUCK_SECONDS"
STUCK_RUNNING_ENV = "CONDUCTOR_TICKET_STUCK_RUNNING_SECONDS"
DAEMON_DEAD_ENV = "CONDUCTOR_DAEMON_DEAD_SECONDS"
FAILURE_WINDOW_ENV = "CONDUCTOR_FAILURE_NOTIFY_SECONDS"
DEFAULT_STUCK_SECONDS = 30 * 60
# The watchdog's RUNNING_STALL_SECONDS. A Worker run is measured from its
# start, so the queued/waiting threshold would call every 30-minute run stuck.
DEFAULT_STUCK_RUNNING_SECONDS = 2 * 60 * 60
DEFAULT_DAEMON_DEAD_SECONDS = 120
DEFAULT_FAILURE_WINDOW_SECONDS = 24 * 60 * 60
# After the macOS banner fails (osascript missing, no GUI session, a hang
# until the timeout), stop calling it for this long. Every pass retrying a
# 5-second timeout per pending banner would stall the scheduler pass.
BANNER_BACKOFF_SECONDS = 60.0

# One pass must not post a banner per historical failure. The rest stay
# pending and a later pass shows them; the inbox row is not the banner.
MAX_BANNERS_PER_PASS = 3
# Page size for every scan. A full page is continued, not treated as the end.
PAGE_SIZE = 200

_OFF = frozenset({"", "0", "off", "false", "no", "disabled"})
# Silence in a state that should still be moving. A Ticket parked on a
# confirmation question is not a stall: that status is needs-user below.
_STUCK_STATUSES = frozenset({"queued", "waiting", "running", "blocked"})
# The clarification gate and the prompt refiner store the confirmation
# question on awaiting_clarification. Leaving it out drops those Tickets
# from the inbox and the badge.
_NEEDS_USER_STATUSES = _STUCK_STATUSES | {"awaiting_clarification"}
_QUOTA_REASONS = frozenset({"quota_exhausted", "quota_retries_exhausted"})

Loader = Callable[..., Awaitable[list[Any]]]
Channel = Callable[[str, str], Any]


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    """One operator alert. ``dedup_key`` is stable while the condition holds."""

    kind: str
    dedup_key: str
    user_id: str
    title: str
    body: str
    subject_id: str


class NotificationStore:
    """Inbox of events. ``add`` is false when the dedup key is already there."""

    async def add(self, event: NotificationEvent) -> bool:
        raise NotImplementedError

    async def unread_count(self, user_id: str) -> int:
        raise NotImplementedError

    async def retain(self, kind: str, active_keys: set[str]) -> None:
        raise NotImplementedError

    async def pending_banners(self, kind: str, limit: int) -> list[NotificationEvent]:
        """Events of this kind that are stored and have not been shown."""
        raise NotImplementedError

    async def mark_bannered(self, dedup_key: str) -> None:
        """The banner was shown. A later pass must not show it again."""
        raise NotImplementedError


class MemoryNotificationStore(NotificationStore):
    """Process-local inbox. Used when the app-data pool is not open."""

    def __init__(self) -> None:
        self._events: dict[str, NotificationEvent] = {}
        # Insertion order. A capped pass leaves the tail here for the next one.
        self._unbannered: list[str] = []

    async def add(self, event: NotificationEvent) -> bool:
        if event.dedup_key in self._events:
            return False
        self._events[event.dedup_key] = event
        if event.dedup_key not in self._unbannered:
            self._unbannered.append(event.dedup_key)
        return True

    async def unread_count(self, user_id: str) -> int:
        return sum(1 for event in self._events.values() if event.user_id == user_id)

    async def retain(self, kind: str, active_keys: set[str]) -> None:
        stale = [
            key
            for key, event in self._events.items()
            if event.kind == kind and key not in active_keys
        ]
        for key in stale:
            del self._events[key]
        self._unbannered = [key for key in self._unbannered if key in self._events]

    async def pending_banners(self, kind: str, limit: int) -> list[NotificationEvent]:
        pending: list[NotificationEvent] = []
        for key in self._unbannered:
            event = self._events.get(key)
            if event is None or event.kind != kind:
                continue
            pending.append(event)
            if len(pending) >= limit:
                break
        return pending

    async def mark_bannered(self, dedup_key: str) -> None:
        self._unbannered = [key for key in self._unbannered if key != dedup_key]


class EffectNotificationStore(NotificationStore):
    """Inbox backed by ``autonomy_effects`` rows the runner will not claim."""

    async def add(self, event: NotificationEvent) -> bool:
        return await notification_db.insert_notification(
            user_id=event.user_id,
            dedup_key=event.dedup_key,
            payload={
                "notification_kind": event.kind,
                "title": event.title,
                "body": event.body,
                "subject_id": event.subject_id,
                "read": False,
                "bannered": False,
            },
        )

    async def unread_count(self, user_id: str) -> int:
        return await notification_db.count_unread(user_id)

    async def retain(self, kind: str, active_keys: set[str]) -> None:
        await notification_db.retain_notifications(kind, sorted(active_keys))

    async def pending_banners(self, kind: str, limit: int) -> list[NotificationEvent]:
        rows = await notification_db.list_pending_banners(kind, limit)
        pending: list[NotificationEvent] = []
        for row in rows:
            key = str(row.get("dedup_key") or "").strip()
            user_id = str(row.get("user_id") or "").strip()
            if not key or not user_id:
                continue
            pending.append(
                NotificationEvent(
                    kind=str(row.get("notification_kind") or kind),
                    dedup_key=key,
                    user_id=user_id,
                    title=str(row.get("title") or ""),
                    body=str(row.get("body") or ""),
                    subject_id=str(row.get("subject_id") or ""),
                )
            )
        return pending

    async def mark_bannered(self, dedup_key: str) -> None:
        await notification_db.mark_notification_bannered(dedup_key)


_memory_store = MemoryNotificationStore()
_effect_store = EffectNotificationStore()
_store_override: NotificationStore | None = None


def database_is_open() -> bool:
    """Whether this process already has an open app-data pool.

    Looking at the cache does not create a pool. A pool that was constructed
    and not opened is still closed, so unit tests that never start the server
    do not scan.
    """
    cache = getattr(pool, "_conversation_db_pool_cache", None)
    if not isinstance(cache, dict):
        return False
    return any(not getattr(item, "closed", True) for item in cache.values())


def active_store() -> NotificationStore:
    """The inbox writers and the badge share. An override is for tests."""
    if _store_override is not None:
        return _store_override
    if database_is_open():
        return _effect_store
    return _memory_store


@contextmanager
def override_store(store: NotificationStore) -> Iterator[NotificationStore]:
    """Point the badge and the default emitter at ``store`` for this block."""
    global _store_override
    previous = _store_override
    _store_override = store
    try:
        yield store
    finally:
        _store_override = previous


def _seconds_setting(environ: Any | None, name: str, default: int) -> int | None:
    """Positive seconds, the default when the value is missing or garbage, or None when off."""
    source = os.environ if environ is None else environ
    if name not in source:
        return default
    text = str(source.get(name) or "").strip().lower()
    if text in _OFF:
        return None
    try:
        value = int(text)
    except ValueError:
        return default
    return value if value > 0 else None


def stuck_threshold_seconds(environ: Any | None = None) -> int | None:
    """How long a Ticket may sit before it is stuck. None turns the check off."""
    return _seconds_setting(environ, STUCK_THRESHOLD_ENV, DEFAULT_STUCK_SECONDS)


def stuck_running_seconds(environ: Any | None = None) -> int | None:
    """How long a Ticket may run before it is stuck. None leaves running out."""
    return _seconds_setting(
        environ, STUCK_RUNNING_ENV, DEFAULT_STUCK_RUNNING_SECONDS
    )


def failure_window_seconds(environ: Any | None = None) -> int | None:
    """How long a failure stays an alert. None turns the failure check off."""
    return _seconds_setting(
        environ, FAILURE_WINDOW_ENV, DEFAULT_FAILURE_WINDOW_SECONDS
    )


def daemon_dead_seconds(environ: Any | None = None) -> int | None:
    """How long a daemon may be quiet before it is dead. None turns the check off."""
    return _seconds_setting(environ, DAEMON_DEAD_ENV, DEFAULT_DAEMON_DEAD_SECONDS)


def dedup_key(kind: str, *parts: object) -> str:
    """A stable key. Colons in the parts cannot look like another kind prefix."""
    safe = [str(part).replace(":", "_") for part in parts]
    return ":".join(["notify", kind, *safe])


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _text(value: object, limit: int = 180) -> str:
    flattened = " ".join(str(value or "").split())
    return flattened[:limit]


def _attention(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("attention")
    return dict(value) if isinstance(value, dict) else {}


def _user_id(row: dict[str, Any]) -> str:
    return str(row.get("user_id") or "").strip()


def _subject(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        text = str(row.get(key) or "").strip()
        if text:
            return text
    return ""


def _root_ticket(row: dict[str, Any]) -> bool:
    mandate = row.get("mandate")
    if not isinstance(mandate, dict):
        return False
    return bool(
        mandate.get("orchestration_only") or mandate.get("kind") == "main_agent_root"
    )


def _label(row: dict[str, Any], *ident_keys: str) -> str:
    ident = _subject(row, *ident_keys)[:8]
    title = _text(row.get("title") or row.get("name") or row.get("summary"), 80)
    if ident and title:
        return f"{ident} {title}"
    return title or ident or "unknown"


def _duration(row: dict[str, Any]) -> str:
    try:
        seconds = int(row.get("age_seconds"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "a while"
    if seconds <= 0:
        return "a while"
    minutes = max(1, seconds // 60)
    unit = "minute" if minutes == 1 else "minutes"
    return f"{minutes} {unit}"


def _stamp(value: object) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    text = str(value or "").strip()
    return text or "never"


def needs_user_event(row: dict[str, Any]) -> NotificationEvent | None:
    """A Ticket whose attention is waiting on a person.

    ``awaiting_clarification`` counts: both confirmation flows park the
    question on that status, and the query returns those rows.
    """
    if _root_ticket(row):
        return None
    user_id = _user_id(row)
    ticket_id = _subject(row, "ticket_id")
    attention = _attention(row)
    if not user_id or not ticket_id or not attention:
        return None
    if str(attention.get("reason") or "") in _QUOTA_REASONS:
        return None
    if str(row.get("status") or "") not in _NEEDS_USER_STATUSES:
        return None
    question = _text(attention.get("question"))
    body = question or f"Ticket {_label(row, 'ticket_id')} needs you."
    return NotificationEvent(
        kind=KIND_NEEDS_USER,
        dedup_key=dedup_key(KIND_NEEDS_USER, ticket_id, _fingerprint(question)),
        user_id=user_id,
        title="Ticket needs you",
        body=body,
        subject_id=ticket_id,
    )


def failure_event(row: dict[str, Any]) -> NotificationEvent | None:
    """A Ticket that failed. The body does not repeat the raw error."""
    if _root_ticket(row) or str(row.get("status") or "") != "failed":
        return None
    user_id = _user_id(row)
    ticket_id = _subject(row, "ticket_id")
    if not user_id or not ticket_id:
        return None
    error = _text(row.get("error"), 240)
    return NotificationEvent(
        kind=KIND_FAILURE,
        dedup_key=dedup_key(KIND_FAILURE, ticket_id, _fingerprint(error)),
        user_id=user_id,
        title="Ticket failed",
        body=f"Ticket {_label(row, 'ticket_id')} failed.",
        subject_id=ticket_id,
    )


def stuck_event(row: dict[str, Any]) -> NotificationEvent | None:
    """A Ticket that has been open, and quiet, past the stuck threshold."""
    if _root_ticket(row):
        return None
    status = str(row.get("status") or "")
    user_id = _user_id(row)
    ticket_id = _subject(row, "ticket_id")
    if status not in _STUCK_STATUSES or not user_id or not ticket_id:
        return None
    if str(_attention(row).get("reason") or "") in _QUOTA_REASONS:
        # Parked on quota: that is the worker-quota alert, and the quota
        # waker requeues it. Two alerts for one park would double the badge.
        return None
    return NotificationEvent(
        kind=KIND_STUCK,
        dedup_key=dedup_key(KIND_STUCK, ticket_id, status),
        user_id=user_id,
        title="Ticket is stuck",
        body=(
            f"Ticket {_label(row, 'ticket_id')} has been {status} "
            f"for {_duration(row)}."
        ),
        subject_id=ticket_id,
    )


def dead_daemon_event(row: dict[str, Any]) -> NotificationEvent | None:
    """A daemon that stopped heartbeating."""
    user_id = _user_id(row)
    daemon_id = _subject(row, "daemon_id")
    if not user_id or not daemon_id:
        return None
    provider = _text(row.get("provider"), 64) or "unknown"
    seen = _stamp(row.get("last_seen_at"))
    return NotificationEvent(
        kind=KIND_DEAD_DAEMON,
        dedup_key=dedup_key(KIND_DEAD_DAEMON, daemon_id, seen),
        user_id=user_id,
        title="Worker daemon is down",
        body=(
            f"Daemon {_label(row, 'daemon_id')} ({provider}) has not been "
            f"seen for {_duration(row)}."
        ),
        subject_id=daemon_id,
    )


def worker_quota_event(row: dict[str, Any]) -> NotificationEvent | None:
    """One event per user and provider while that provider is out of quota."""
    if _root_ticket(row):
        return None
    user_id = _user_id(row)
    attention = _attention(row)
    if str(attention.get("reason") or "") not in _QUOTA_REASONS:
        return None
    provider = _text(
        attention.get("provider") or row.get("worker_provider") or row.get("provider"),
        64,
    )
    if not user_id or not provider:
        return None
    return NotificationEvent(
        kind=KIND_WORKER_QUOTA,
        dedup_key=dedup_key(KIND_WORKER_QUOTA, user_id, provider.casefold()),
        user_id=user_id,
        title="Worker is out of quota",
        body=f"Worker {provider} is out of quota.",
        subject_id=provider.casefold(),
    )


def _collect(
    rows: list[Any],
    build: Callable[[dict[str, Any]], NotificationEvent | None],
) -> tuple[list[NotificationEvent], bool]:
    events: list[NotificationEvent] = []
    seen: set[str] = set()
    complete = True
    for row in rows:
        if not isinstance(row, dict):
            complete = False
            continue
        event = build(row)
        if event is None:
            # Not this condition (a ``null`` attention, a root Ticket, a row
            # with no user). No alert was ever keyed from it, so it cannot
            # hold one back. Marking the scan incomplete here would turn off
            # retain for good while that row exists, and the badge would
            # never fall for any Ticket of this kind.
            continue
        if event.dedup_key in seen:
            continue
        seen.add(event.dedup_key)
        events.append(event)
    return events, complete


_banner_backoff_until = 0.0


def _banner_backing_off() -> bool:
    return time.monotonic() < _banner_backoff_until


def _back_off_banners() -> None:
    global _banner_backoff_until
    _banner_backoff_until = time.monotonic() + BANNER_BACKOFF_SECONDS


async def _banner(event: NotificationEvent, channel: Channel | None) -> bool:
    """Show one banner. False means a later pass should try this event again.

    The default macOS channel backs off after a failure, so a broken or
    hanging osascript costs one timeout per backoff window, not one per
    pending banner per pass. An injected channel is called every time.
    """
    default = channel is None
    if default and _banner_backing_off():
        return False
    send = macos_deliver if default else channel
    try:
        result = await asyncio.to_thread(send, event.title, event.body)
    except Exception:  # noqa: BLE001 - the inbox row is already the record
        logger.exception(
            "[autonomy] notification banner failed kind=%s subject=%s",
            event.kind,
            event.subject_id,
        )
        if default:
            _back_off_banners()
        return False
    if result is False:
        logger.warning(
            "[autonomy] notification banner was not delivered kind=%s subject=%s",
            event.kind,
            event.subject_id,
        )
        if default:
            _back_off_banners()
        return False
    return True


async def _drain_banners(
    target: NotificationStore,
    kind: str,
    channel: Channel | None,
) -> None:
    """Show up to the cap, oldest pending first. The rest wait for the next pass."""
    try:
        pending = await target.pending_banners(kind, MAX_BANNERS_PER_PASS)
    except Exception:  # noqa: BLE001 - the inbox row is already the record
        logger.exception("[autonomy] notification banner queue failed kind=%s", kind)
        return
    for event in pending:
        if not await _banner(event, channel):
            continue
        try:
            await target.mark_bannered(event.dedup_key)
        except Exception:  # noqa: BLE001 - an unmarked banner is retried next pass
            logger.exception(
                "[autonomy] notification banner mark failed kind=%s subject=%s",
                event.kind,
                event.subject_id,
            )


async def _sync(
    kind: str,
    rows: list[Any],
    build: Callable[[dict[str, Any]], NotificationEvent | None],
    *,
    scan_complete: bool,
    store: NotificationStore | None,
    channel: Channel | None,
) -> int:
    events, rows_complete = _collect(rows, build)
    target = store or active_store()
    created = 0
    for event in events:
        try:
            fresh = await target.add(event)
        except Exception:  # noqa: BLE001 - one row must not drop the rest
            logger.exception(
                "[autonomy] notification write failed kind=%s subject=%s",
                event.kind,
                event.subject_id,
            )
            rows_complete = False
            continue
        if not fresh:
            continue
        created += 1
        logger.info(
            "[autonomy] notification kind=%s subject=%s user=%s",
            event.kind,
            event.subject_id,
            event.user_id,
        )
    # Retain before banners so a condition that just ended is not shown.
    # An unfinished scan must not delete rows from a page it did not read.
    # A stuck Ticket stays in this set only while the scan still returns it.
    # The watchdog attention write keeps that Ticket's updated_at, so an
    # unresolved stall is still here and a banner waiting past the cap is
    # not deleted.
    if scan_complete and rows_complete:
        try:
            await target.retain(kind, {event.dedup_key for event in events})
        except Exception:  # noqa: BLE001 - a failed retain leaves the old badge
            logger.exception("[autonomy] notification retain failed kind=%s", kind)
    await _drain_banners(target, kind, channel)
    return created


def _page_token(row: Any) -> str:
    if isinstance(row, dict):
        return str(
            row.get("ticket_id") or row.get("daemon_id") or row.get("user_id") or ""
        )
    return str(row)


async def _scan_pages(
    fetch_page: Callable[[int, int], Awaitable[Any]],
) -> tuple[list[Any], bool]:
    """Read every page. The bool is false when a later page was not read.

    A full page is not the end of the table. The next pass must not request
    the same window, or a row past it stays invisible and a stale alert can
    never be cleared. A page that repeats means the lister ignored the
    offset; stopping there avoids a loop and leaves retain off.
    """
    collected: list[Any] = []
    offset = 0
    seen: set[tuple[str, ...]] = set()
    while True:
        try:
            page = await fetch_page(offset, PAGE_SIZE)
        except Exception:  # noqa: BLE001 - the next pass retries the scan
            logger.exception("[autonomy] notification scan failed")
            return collected, False
        if not isinstance(page, list):
            return collected, False
        collected.extend(page)
        if len(page) < PAGE_SIZE:
            return collected, True
        identity = tuple(_page_token(row) for row in page)
        if identity in seen:
            logger.error(
                "[autonomy] notification scan repeated a page at offset %s",
                offset,
            )
            return collected, False
        seen.add(identity)
        offset += PAGE_SIZE


async def _load_rows(loader: Loader, *args: object) -> list[Any] | None:
    """The whole scan from a test loader, or None when that call failed."""
    try:
        rows = await loader(*args)
    except Exception:  # noqa: BLE001 - the next pass retries the scan
        logger.exception("[autonomy] notification scan failed")
        return None
    if not isinstance(rows, list):
        return None
    return rows


async def _paged_or_loaded(
    loader: Loader | None,
    load_args: tuple[object, ...],
    fetch_page: Callable[[int, int], Awaitable[Any]],
) -> tuple[list[Any], bool] | None:
    """Rows plus whether the scan saw the last page.

    None means this process should not scan: the pool is closed and no test
    loader was injected, or the loader itself failed. A database page error
    still returns the pages already read, marked incomplete, so those alerts
    are recorded without deleting ones from a page that was not read.
    """
    if loader is not None:
        rows = await _load_rows(loader, *load_args)
        if rows is None:
            return None
        return rows, True
    if not database_is_open():
        return None
    return await _scan_pages(fetch_page)


async def emit_event(
    event: NotificationEvent,
    *,
    store: NotificationStore | None = None,
    channel: Channel | None = None,
) -> bool:
    """Record one event and banner it when it is new. False when de-duplicated."""
    target = store or active_store()
    fresh = await target.add(event)
    if fresh and await _banner(event, channel):
        try:
            await target.mark_bannered(event.dedup_key)
        except Exception:  # noqa: BLE001 - an unmarked banner is retried next pass
            logger.exception(
                "[autonomy] notification banner mark failed kind=%s subject=%s",
                event.kind,
                event.subject_id,
            )
    return fresh


async def _emit_built(
    event: NotificationEvent | None,
    **kwargs: Any,
) -> bool:
    if event is None:
        return False
    return await emit_event(event, **kwargs)


async def notify_needs_user(row: dict[str, Any], **kwargs: Any) -> bool:
    return await _emit_built(needs_user_event(row), **kwargs)


async def notify_failure(row: dict[str, Any], **kwargs: Any) -> bool:
    return await _emit_built(failure_event(row), **kwargs)


async def notify_stuck(row: dict[str, Any], **kwargs: Any) -> bool:
    return await _emit_built(stuck_event(row), **kwargs)


async def notify_dead_daemon(row: dict[str, Any], **kwargs: Any) -> bool:
    return await _emit_built(dead_daemon_event(row), **kwargs)


async def notify_worker_quota(row: dict[str, Any], **kwargs: Any) -> bool:
    return await _emit_built(worker_quota_event(row), **kwargs)


async def unread_count(user_id: str) -> int:
    """Unread alerts for one user, from the durable inbox.

    A closed pool is not an inbox the other workers can see, so the badge is
    0 instead of a process-local dict. Tests point the count at a store with
    ``override_store``.
    """
    if _store_override is not None:
        return await _store_override.unread_count(str(user_id or ""))
    if not database_is_open():
        return 0
    return await _effect_store.unread_count(str(user_id or ""))


async def _check(
    kind: str,
    build: Callable[[dict[str, Any]], NotificationEvent | None],
    *,
    loader: Loader | None,
    load_args: tuple[object, ...],
    fetch_page: Callable[[int, int], Awaitable[Any]],
    store: NotificationStore | None,
    channel: Channel | None,
) -> int:
    scanned = await _paged_or_loaded(loader, load_args, fetch_page)
    if scanned is None:
        return 0
    rows, scan_complete = scanned
    return await _sync(
        kind,
        rows,
        build,
        scan_complete=scan_complete,
        store=store,
        channel=channel,
    )


async def check_stuck_tickets(
    *,
    loader: Loader | None = None,
    environ: Any | None = None,
    store: NotificationStore | None = None,
    channel: Channel | None = None,
) -> int:
    """Emit stuck-Ticket events past the configured threshold. Returns how many were new."""
    threshold = stuck_threshold_seconds(environ)
    if threshold is None:
        return 0
    running = stuck_running_seconds(environ)

    async def fetch_page(offset: int, limit: int) -> list[Any]:
        return await notification_db.list_stuck_tickets(
            min_seconds=threshold,
            running_seconds=running,
            limit=limit,
            offset=offset,
        )

    return await _check(
        KIND_STUCK,
        stuck_event,
        loader=loader,
        load_args=(threshold,),
        fetch_page=fetch_page,
        store=store,
        channel=channel,
    )


async def check_needs_user(
    *,
    loader: Loader | None = None,
    store: NotificationStore | None = None,
    channel: Channel | None = None,
) -> int:
    """Emit events for Tickets that need a person. Returns how many were new."""

    async def fetch_page(offset: int, limit: int) -> list[Any]:
        return await notification_db.list_needs_user(limit=limit, offset=offset)

    return await _check(
        KIND_NEEDS_USER,
        needs_user_event,
        loader=loader,
        load_args=(),
        fetch_page=fetch_page,
        store=store,
        channel=channel,
    )


async def check_failures(
    *,
    loader: Loader | None = None,
    environ: Any | None = None,
    store: NotificationStore | None = None,
    channel: Channel | None = None,
) -> int:
    """Emit events for Tickets that failed inside the window. Returns how many were new.

    A failure older than the window leaves the inbox on this pass.
    """
    window = failure_window_seconds(environ)
    if window is None:
        return 0

    async def fetch_page(offset: int, limit: int) -> list[Any]:
        return await notification_db.list_failures(
            limit=limit, offset=offset, within_seconds=window
        )

    return await _check(
        KIND_FAILURE,
        failure_event,
        loader=loader,
        load_args=(),
        fetch_page=fetch_page,
        store=store,
        channel=channel,
    )


async def check_dead_daemons(
    *,
    loader: Loader | None = None,
    environ: Any | None = None,
    store: NotificationStore | None = None,
    channel: Channel | None = None,
) -> int:
    """Emit events for daemons quiet past the configured threshold."""
    threshold = daemon_dead_seconds(environ)
    if threshold is None:
        return 0

    async def fetch_page(offset: int, limit: int) -> list[Any]:
        return await notification_db.list_dead_daemons(
            min_seconds=threshold, limit=limit, offset=offset
        )

    return await _check(
        KIND_DEAD_DAEMON,
        dead_daemon_event,
        loader=loader,
        load_args=(threshold,),
        fetch_page=fetch_page,
        store=store,
        channel=channel,
    )


async def check_worker_quota(
    *,
    loader: Loader | None = None,
    store: NotificationStore | None = None,
    channel: Channel | None = None,
) -> int:
    """Emit events for workers parked on an exhausted quota."""

    async def fetch_page(offset: int, limit: int) -> list[Any]:
        return await notification_db.list_quota_blocked(limit=limit, offset=offset)

    return await _check(
        KIND_WORKER_QUOTA,
        worker_quota_event,
        loader=loader,
        load_args=(),
        fetch_page=fetch_page,
        store=store,
        channel=channel,
    )
