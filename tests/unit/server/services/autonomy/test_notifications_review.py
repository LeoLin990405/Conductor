"""Review regressions for operator notifications (R7b-2 L4).

No database, no osascript: the DB listers are faked and the channel is a
recorder, except where the default macOS channel's guard is the subject.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.server.database.autonomy import notifications as notification_db
from src.server.services.autonomy import notifications as notes
from src.server.services.autonomy import notifier_macos
from src.server.services.autonomy.notifications import (
    DEFAULT_FAILURE_WINDOW_SECONDS,
    DEFAULT_STUCK_RUNNING_SECONDS,
    FAILURE_WINDOW_ENV,
    STUCK_RUNNING_ENV,
    MemoryNotificationStore,
)


def _channel(banners: list[tuple[str, str]]):
    def send(title: str, body: str) -> bool:
        banners.append((title, body))
        return True

    return send


def _loader(rows):
    async def load(*_args):
        return list(rows)

    return load


@pytest.mark.asyncio
async def test_a_row_that_is_not_the_condition_does_not_freeze_the_badge():
    """A ``null`` attention row used to switch retain off for good."""
    store = MemoryNotificationStore()
    answered = {
        "ticket_id": "ticket-old",
        "user_id": "user-a",
        "status": "blocked",
        "title": "Deploy",
        "attention": {"question": "Which region?"},
    }
    channel = _channel([])
    assert await notes.check_needs_user(
        loader=_loader([answered]), store=store, channel=channel
    ) == 1
    assert await store.unread_count("user-a") == 1

    # The question was answered; the only row left has a JSON-null attention
    # (``attention IS NOT NULL`` in SQL, ``{}`` here) and builds no event.
    leftover = dict(answered, ticket_id="ticket-other", attention=None)
    await notes.check_needs_user(
        loader=_loader([leftover]), store=store, channel=channel
    )
    assert await store.unread_count("user-a") == 0


def test_a_quota_park_is_not_also_a_stuck_ticket():
    row = {
        "ticket_id": "ticket-q",
        "user_id": "user-a",
        "status": "blocked",
        "age_seconds": 99999,
        "attention": {"reason": "quota_exhausted", "provider": "codex"},
    }
    assert notes.stuck_event(row) is None
    assert notes.worker_quota_event(row) is not None


@pytest.mark.asyncio
async def test_running_tickets_use_their_own_longer_stuck_threshold(monkeypatch):
    seen: dict = {}

    async def listed(*, min_seconds, limit, offset=0, running_seconds=None):
        seen.update(min_seconds=min_seconds, running_seconds=running_seconds)
        return []

    monkeypatch.setattr(notes, "database_is_open", lambda: True)
    monkeypatch.setattr(notification_db, "list_stuck_tickets", listed)
    store = MemoryNotificationStore()
    await notes.check_stuck_tickets(environ={}, store=store, channel=_channel([]))
    assert seen == {
        "min_seconds": notes.DEFAULT_STUCK_SECONDS,
        "running_seconds": DEFAULT_STUCK_RUNNING_SECONDS,
    }
    assert DEFAULT_STUCK_RUNNING_SECONDS > notes.DEFAULT_STUCK_SECONDS

    await notes.check_stuck_tickets(
        environ={STUCK_RUNNING_ENV: "off"}, store=store, channel=_channel([])
    )
    assert seen["running_seconds"] is None


@pytest.mark.asyncio
async def test_stuck_sql_measures_running_separately_and_skips_quota(
    mock_db_connection, mock_cursor
):
    mock_cursor.fetchall = AsyncMock(return_value=[])
    await notification_db.list_stuck_tickets(
        min_seconds=1800, running_seconds=7200, limit=10
    )
    sql, params = mock_cursor.execute.await_args.args
    assert "status <> 'running'" in sql
    assert "COALESCE(started_at, updated_at)" in sql
    assert "quota_exhausted" in sql
    assert params == (1800, 7200, 10, 0)


@pytest.mark.asyncio
async def test_failures_are_scanned_inside_a_window(monkeypatch):
    """Without a window the first pass bannered every historical failure."""
    seen: dict = {}

    async def listed(*, limit, offset=0, within_seconds=None):
        seen["within_seconds"] = within_seconds
        return []

    monkeypatch.setattr(notes, "database_is_open", lambda: True)
    monkeypatch.setattr(notification_db, "list_failures", listed)
    store = MemoryNotificationStore()
    await notes.check_failures(environ={}, store=store, channel=_channel([]))
    assert seen["within_seconds"] == DEFAULT_FAILURE_WINDOW_SECONDS

    await notes.check_failures(
        environ={FAILURE_WINDOW_ENV: "3600"}, store=store, channel=_channel([])
    )
    assert seen["within_seconds"] == 3600

    seen.clear()
    assert (
        await notes.check_failures(
            environ={FAILURE_WINDOW_ENV: "off"}, store=store, channel=_channel([])
        )
        == 0
    )
    assert seen == {}


@pytest.mark.asyncio
async def test_failure_sql_bounds_the_window(mock_db_connection, mock_cursor):
    mock_cursor.fetchall = AsyncMock(return_value=[])
    await notification_db.list_failures(limit=10, within_seconds=86400)
    sql, params = mock_cursor.execute.await_args.args
    assert "updated_at >= NOW()" in sql
    assert params == (86400, 86400, 10, 0)


@pytest.mark.asyncio
async def test_a_failing_macos_banner_backs_off_instead_of_retrying_each_event(
    monkeypatch,
):
    calls: list[str] = []

    def broken(title: str, body: str) -> bool:
        calls.append(title)
        return False

    monkeypatch.setattr(notes, "macos_deliver", broken)
    monkeypatch.setattr(notes, "_banner_backoff_until", 0.0)
    store = MemoryNotificationStore()
    rows = [
        {
            "ticket_id": f"ticket-{index}",
            "user_id": "user-a",
            "status": "failed",
            "title": "Ship",
        }
        for index in range(3)
    ]
    # channel=None is the default macOS channel (faked above).
    await notes.check_failures(loader=_loader(rows), environ={}, store=store)
    await notes.check_failures(loader=_loader(rows), environ={}, store=store)
    assert len(calls) == 1
    # The rows stay pending and are shown once the backoff ends.
    assert len(await store.pending_banners(notes.KIND_FAILURE, 10)) == 3


def test_default_runner_never_starts_osascript_under_pytest(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("osascript must not run in a test")

    monkeypatch.setattr(notifier_macos.subprocess, "run", refuse)
    assert notifier_macos.deliver("Title", "Body", platform="darwin") is None


def test_the_macos_switch_turns_banners_off():
    assert notifier_macos.banners_enabled({}) is True
    assert notifier_macos.banners_enabled({"CONDUCTOR_NOTIFY_MACOS": "off"}) is False
    assert notifier_macos.banners_enabled({"CONDUCTOR_NOTIFY_MACOS": "0"}) is False
    assert notifier_macos.banners_enabled({"PYTEST_CURRENT_TEST": "x"}) is False


def test_ticket_text_cannot_break_out_of_the_applescript_literal():
    seen: dict = {}

    def runner(argv):
        seen["argv"] = list(argv)
        return 0

    title = 'x" & (do shell script "touch /tmp/pwn") & "'
    body = 'line1\n" with title "spoof\\'
    assert (
        notifier_macos.deliver(title, body, runner=runner, platform="darwin")
        is True
    )
    argv = seen["argv"]
    assert argv[:2] == ["osascript", "-e"] and len(argv) == 3
    script = argv[2]
    # Every quote that came from the Ticket text is escaped.
    inner = script[len('display notification "') : -1]
    stripped = inner.replace('\\\\', "").replace('\\"', "")
    assert stripped.count('"') == 2  # only the literal's own close/open pair
    assert "\n" not in script
