"""Operator notification events: five conditions, de-duplicated.

The macOS channel, the badge route, and the sweep wiring live in their own
test modules. These tests never start osascript and never open a database.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.server.database.autonomy import notifications as notification_db
from src.server.services.autonomy import notifications as notes
from src.server.services.autonomy.notifications import (
    DAEMON_DEAD_ENV,
    DEFAULT_DAEMON_DEAD_SECONDS,
    DEFAULT_STUCK_SECONDS,
    KIND_DEAD_DAEMON,
    KIND_FAILURE,
    KIND_NEEDS_USER,
    KIND_STUCK,
    KIND_WORKER_QUOTA,
    PAGE_SIZE,
    STUCK_THRESHOLD_ENV,
    MemoryNotificationStore,
)


def _stuck_row(ticket_id: str, **extra: object) -> dict:
    row = {
        "ticket_id": ticket_id,
        "user_id": "user-a",
        "status": "running",
        "title": "Ship the badge",
        "age_seconds": 3600,
    }
    row.update(extra)
    return row


def _needs_row(**extra: object) -> dict:
    row = {
        "ticket_id": "ticket-need",
        "user_id": "user-a",
        "status": "blocked",
        "title": "Deploy",
        "attention": {"reason": "clarify", "question": "Which region?"},
    }
    row.update(extra)
    return row


class RecordingStore(MemoryNotificationStore):
    def __init__(self) -> None:
        super().__init__()
        self.retains: list[tuple[str, set[str]]] = []

    async def retain(self, kind: str, active_keys: set[str]) -> None:
        self.retains.append((kind, set(active_keys)))
        await super().retain(kind, active_keys)


def _channel(banners: list[tuple[str, str]]):
    def send(title: str, body: str) -> bool:
        banners.append((title, body))
        return True

    return send


def test_five_conditions_build_distinct_deduped_events():
    needs = notes.needs_user_event(_needs_row())
    failed = notes.failure_event(
        {
            "ticket_id": "ticket-fail",
            "user_id": "user-a",
            "status": "failed",
            "title": "Ship it",
            "error": "secret stack TOKEN",
        }
    )
    stuck = notes.stuck_event(_stuck_row("a:b", age_seconds=120))
    seen = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    dead = notes.dead_daemon_event(
        {
            "daemon_id": "daemon-1",
            "user_id": "user-a",
            "name": "laptop",
            "provider": "claude",
            "last_seen_at": seen,
            "age_seconds": 180,
        }
    )
    quota = notes.worker_quota_event(
        {
            "ticket_id": "ticket-q",
            "user_id": "user-a",
            "status": "blocked",
            "worker_provider": "Codex",
            "attention": {"reason": "quota_exhausted", "provider": "Codex"},
        }
    )
    assert needs is not None and failed is not None
    assert stuck is not None and dead is not None and quota is not None
    kinds = {needs.kind, failed.kind, stuck.kind, dead.kind, quota.kind}
    assert kinds == {
        KIND_NEEDS_USER,
        KIND_FAILURE,
        KIND_STUCK,
        KIND_DEAD_DAEMON,
        KIND_WORKER_QUOTA,
    }
    assert "TOKEN" not in failed.body
    assert "secret" not in failed.body
    assert needs.body == "Which region?"
    assert stuck.dedup_key == "notify:stuck:a_b:running"
    assert "2 minutes" in stuck.body
    assert dead.dedup_key == notes.dedup_key(
        KIND_DEAD_DAEMON, "daemon-1", seen.isoformat()
    )
    assert seen.isoformat().replace(":", "_") in dead.dedup_key
    assert quota.dedup_key == notes.dedup_key(KIND_WORKER_QUOTA, "user-a", "codex")
    assert notes.worker_quota_event(
        {
            "ticket_id": "other-ticket",
            "user_id": "user-a",
            "status": "waiting",
            "attention": {"reason": "quota_retries_exhausted", "provider": "CODEX"},
        }
    ).dedup_key == quota.dedup_key


# The confirmation copy the clarification gate and the prompt refiner store
# on a Ticket they park in awaiting_clarification.
_CLARIFY_QUESTION = "Please confirm the clarified request before this Ticket starts."
_REFINE_QUESTION = "Please confirm the refined request before this Ticket starts."
_REFINE_HOLD_QUESTION = "Please confirm this request before its Ticket starts."


def _confirmation_row(ticket_id: str, question: str, **extra: object) -> dict:
    row = {
        "ticket_id": ticket_id,
        "user_id": "user-a",
        "status": "awaiting_clarification",
        "title": "Needs a confirmation",
        "attention": {"question": question},
    }
    row.update(extra)
    return row


def test_clarification_and_refinement_need_the_user():
    """Both confirmation flows park the question on awaiting_clarification."""
    clarify = notes.needs_user_event(
        _confirmation_row("ticket-clarify", _CLARIFY_QUESTION, title="Unclear request")
    )
    refine = notes.needs_user_event(
        _confirmation_row(
            "ticket-refine",
            _REFINE_QUESTION,
            title="Vague request",
            mandate={"refine": {"mode": "always", "degraded": False}},
        )
    )
    held = notes.needs_user_event(
        _confirmation_row(
            "ticket-hold",
            _REFINE_HOLD_QUESTION,
            title="Unattended request",
            mandate={"refine": {"mode": "always", "degraded": False, "held": True}},
        )
    )
    assert clarify is not None and refine is not None and held is not None
    assert clarify.kind == refine.kind == held.kind == KIND_NEEDS_USER
    assert clarify.body == _CLARIFY_QUESTION
    assert refine.body == _REFINE_QUESTION
    assert held.body == _REFINE_HOLD_QUESTION
    assert len({clarify.dedup_key, refine.dedup_key, held.dedup_key}) == 3
    # A confirmation is not a stall. The stuck builder must not adopt the status.
    assert (
        notes.stuck_event(
            _confirmation_row("ticket-clarify", _CLARIFY_QUESTION, age_seconds=4000)
        )
        is None
    )
    assert notes.needs_user_event(
        _confirmation_row(
            "ticket-clarify",
            _CLARIFY_QUESTION,
            mandate={"kind": "main_agent_root", "orchestration_only": True},
        )
    ) is None
    assert (
        notes.needs_user_event(
            _confirmation_row(
                "ticket-clarify",
                _CLARIFY_QUESTION,
                attention={"reason": "quota_exhausted", "question": _CLARIFY_QUESTION},
            )
        )
        is None
    )


@pytest.mark.asyncio
async def test_confirmation_flows_reach_the_badge():
    store = MemoryNotificationStore()
    banners: list[tuple[str, str]] = []
    rows = [
        _confirmation_row("ticket-clarify", _CLARIFY_QUESTION, title="Unclear request"),
        _confirmation_row(
            "ticket-refine",
            _REFINE_QUESTION,
            title="Vague request",
            mandate={"refine": {"mode": "vague", "degraded": False}},
        ),
        _confirmation_row(
            "ticket-hold",
            _REFINE_HOLD_QUESTION,
            title="Unattended request",
            mandate={"refine": {"mode": "vague", "held": True}},
        ),
    ]
    created = await notes.check_needs_user(
        loader=_loader(rows),
        store=store,
        channel=_channel(banners),
    )
    assert created == 3
    assert await store.unread_count("user-a") == 3
    assert {body for _title, body in banners} == {
        _CLARIFY_QUESTION,
        _REFINE_QUESTION,
        _REFINE_HOLD_QUESTION,
    }
    again = await notes.check_needs_user(
        loader=_loader(rows),
        store=store,
        channel=_channel(banners),
    )
    assert again == 0
    assert await store.unread_count("user-a") == 3


def test_builders_skip_roots_quota_confusion_and_empty_identity():
    root = {"mandate": {"kind": "main_agent_root", "orchestration_only": True}}
    assert notes.needs_user_event(_needs_row(**root)) is None
    assert notes.stuck_event(_stuck_row("t", **root)) is None
    assert (
        notes.failure_event(
            {
                "ticket_id": "t",
                "user_id": "user-a",
                "status": "failed",
                "mandate": {"orchestration_only": True},
            }
        )
        is None
    )
    assert (
        notes.needs_user_event(
            _needs_row(attention={"reason": "quota_exhausted", "question": "wait"})
        )
        is None
    )
    assert notes.stuck_event(_stuck_row("t", user_id="  ")) is None
    assert notes.dead_daemon_event({"daemon_id": "d", "user_id": ""}) is None
    assert (
        notes.worker_quota_event(
            {
                "user_id": "user-a",
                "attention": {"reason": "quota_exhausted"},
            }
        )
        is None
    )
    assert notes.failure_event(_stuck_row("t", status="running")) is None


def test_thresholds_default_disable_and_fall_back():
    assert notes.stuck_threshold_seconds({}) == DEFAULT_STUCK_SECONDS == 1800
    assert notes.daemon_dead_seconds({}) == DEFAULT_DAEMON_DEAD_SECONDS == 120
    for off in ("0", "off", "false", "no", "disabled", "", "  ", "-3"):
        assert notes.stuck_threshold_seconds({STUCK_THRESHOLD_ENV: off}) is None
        assert notes.daemon_dead_seconds({DAEMON_DEAD_ENV: off}) is None
    assert notes.stuck_threshold_seconds({STUCK_THRESHOLD_ENV: "nope"}) == 1800
    assert notes.stuck_threshold_seconds({STUCK_THRESHOLD_ENV: "45"}) == 45
    assert notes.daemon_dead_seconds({DAEMON_DEAD_ENV: "15"}) == 15


@pytest.mark.asyncio
async def test_emit_dedupes_and_banners_only_the_first_time():
    store = MemoryNotificationStore()
    banners: list[tuple[str, str]] = []
    row = _stuck_row("ticket-1")
    assert await notes.notify_stuck(row, store=store, channel=_channel(banners)) is True
    assert await notes.notify_stuck(row, store=store, channel=_channel(banners)) is False
    assert banners == [("Ticket is stuck", banners[0][1])]
    assert await store.unread_count("user-a") == 1
    assert await store.unread_count("someone-else") == 0


@pytest.mark.asyncio
async def test_a_failed_banner_still_keeps_the_event():
    store = MemoryNotificationStore()

    def refuse(title: str, body: str) -> bool:
        return False

    event = notes.stuck_event(_stuck_row("ticket-1"))
    assert event is not None
    assert await notes.emit_event(event, store=store, channel=refuse) is True
    assert await store.unread_count("user-a") == 1


@pytest.mark.asyncio
async def test_checks_emit_each_kind_and_cap_banners():
    store = RecordingStore()
    banners: list[tuple[str, str]] = []
    rows = [_stuck_row(f"ticket-{index}") for index in range(5)]
    created = await notes.check_stuck_tickets(
        loader=_loader(rows),
        environ={STUCK_THRESHOLD_ENV: "45"},
        store=store,
        channel=_channel(banners),
    )
    assert created == 5
    assert len(banners) == notes.MAX_BANNERS_PER_PASS == 3
    assert await store.unread_count("user-a") == 5
    assert store.retains[-1][0] == KIND_STUCK
    assert len(store.retains[-1][1]) == 5

    await notes.check_needs_user(
        loader=_loader([_needs_row()]),
        store=store,
        channel=_channel(banners),
    )
    await notes.check_failures(
        loader=_loader(
            [
                {
                    "ticket_id": "ticket-fail",
                    "user_id": "user-a",
                    "status": "failed",
                    "title": "Ship it",
                    "error": "boom",
                }
            ]
        ),
        store=store,
        channel=_channel(banners),
    )
    await notes.check_dead_daemons(
        loader=_loader(
            [
                {
                    "daemon_id": "daemon-1",
                    "user_id": "user-a",
                    "name": "laptop",
                    "provider": "claude",
                    "last_seen_at": "never-stamp",
                    "age_seconds": 200,
                }
            ]
        ),
        environ={DAEMON_DEAD_ENV: "15"},
        store=store,
        channel=_channel(banners),
    )
    await notes.check_worker_quota(
        loader=_loader(
            [
                {
                    "ticket_id": "q1",
                    "user_id": "user-a",
                    "status": "blocked",
                    "attention": {"reason": "quota_exhausted", "provider": "grok"},
                },
                {
                    "ticket_id": "q2",
                    "user_id": "user-a",
                    "status": "waiting",
                    "attention": {"reason": "quota_retries_exhausted", "provider": "Grok"},
                },
            ]
        ),
        store=store,
        channel=_channel(banners),
    )
    assert await store.unread_count("user-a") == 9


def _loader(rows, seen: dict | None = None):
    async def load(*args):
        if seen is not None:
            seen["args"] = args
        return list(rows)

    return load


@pytest.mark.asyncio
async def test_stuck_and_dead_loaders_receive_the_threshold():
    stuck_seen: dict = {}
    dead_seen: dict = {}
    store = RecordingStore()
    channel = _channel([])
    assert (
        await notes.check_stuck_tickets(
            loader=_loader([_stuck_row("t")], stuck_seen),
            environ={STUCK_THRESHOLD_ENV: "45"},
            store=store,
            channel=channel,
        )
        == 1
    )
    assert stuck_seen["args"] == (45,)
    assert (
        await notes.check_dead_daemons(
            loader=_loader(
                [
                    {
                        "daemon_id": "d",
                        "user_id": "user-a",
                        "provider": "claude",
                        "age_seconds": 30,
                    }
                ],
                dead_seen,
            ),
            environ={},
            store=store,
            channel=channel,
        )
        == 1
    )
    assert dead_seen["args"] == (DEFAULT_DAEMON_DEAD_SECONDS,)


@pytest.mark.asyncio
async def test_retain_clears_the_badge_when_the_condition_ends_and_a_later_one_returns():
    store = RecordingStore()
    banners: list[tuple[str, str]] = []
    row = _stuck_row("ticket-1")
    assert (
        await notes.check_stuck_tickets(
            loader=_loader([row]),
            environ={},
            store=store,
            channel=_channel(banners),
        )
        == 1
    )
    assert (
        await notes.check_stuck_tickets(
            loader=_loader([]),
            environ={},
            store=store,
            channel=_channel(banners),
        )
        == 0
    )
    assert await store.unread_count("user-a") == 0
    assert store.retains[-1] == (KIND_STUCK, set())
    assert (
        await notes.check_stuck_tickets(
            loader=_loader([row]),
            environ={},
            store=store,
            channel=_channel(banners),
        )
        == 1
    )
    assert len(banners) == 2


@pytest.mark.asyncio
async def test_retain_is_skipped_when_the_scan_is_partial_or_the_check_is_off():
    store = RecordingStore()
    kept = notes.stuck_event(_stuck_row("kept"))
    assert kept is not None
    await store.add(kept)
    channel = _channel([])

    async def explode(_threshold: int):
        raise RuntimeError("scan down")

    assert (
        await notes.check_stuck_tickets(
            loader=explode,
            environ={},
            store=store,
            channel=channel,
        )
        == 0
    )
    assert store.retains == []

    async def not_a_list(_threshold: int):
        return {"rows": []}

    assert (
        await notes.check_stuck_tickets(
            loader=not_a_list,
            environ={},
            store=store,
            channel=channel,
        )
        == 0
    )
    assert store.retains == []

    await notes.check_stuck_tickets(
        loader=_loader([_stuck_row("fresh"), "not-a-row"]),
        environ={},
        store=store,
        channel=channel,
    )
    assert store.retains == []

    called = {"yes": False}

    async def should_not_run(_threshold: int):
        called["yes"] = True
        return []

    assert (
        await notes.check_stuck_tickets(
            loader=should_not_run,
            environ={STUCK_THRESHOLD_ENV: "off"},
            store=store,
            channel=channel,
        )
        == 0
    )
    assert called["yes"] is False
    assert store.retains == []
    assert await store.unread_count("user-a") >= 1


@pytest.mark.asyncio
async def test_a_closed_pool_does_not_scan_or_clear(monkeypatch):
    monkeypatch.setattr(notes, "database_is_open", lambda: False)
    listed = AsyncMock()
    monkeypatch.setattr(notification_db, "list_stuck_tickets", listed)
    monkeypatch.setattr(notification_db, "list_dead_daemons", listed)
    store = RecordingStore()
    kept = notes.dead_daemon_event(
        {"daemon_id": "d", "user_id": "user-a", "provider": "claude", "age_seconds": 90}
    )
    assert kept is not None
    await store.add(kept)
    assert await notes.check_dead_daemons(store=store, channel=_channel([])) == 0
    assert await notes.check_stuck_tickets(store=store, channel=_channel([])) == 0
    listed.assert_not_awaited()
    assert store.retains == []
    assert await store.unread_count("user-a") == 1


@pytest.mark.asyncio
async def test_unread_count_ignores_process_memory_when_the_pool_is_closed(monkeypatch):
    monkeypatch.setattr(notes, "database_is_open", lambda: False)
    event = notes.stuck_event(_stuck_row("ticket-1"))
    assert event is not None
    await notes._memory_store.add(event)
    try:
        assert await notes.unread_count("user-a") == 0
    finally:
        notes._memory_store._events.pop(event.dedup_key, None)


def _overflow_rows(kind: str, count: int) -> list[dict]:
    rows: list[dict] = []
    for index in range(count):
        user_id = "last-user" if index == count - 1 else "user-a"
        if kind == "stuck":
            rows.append(_stuck_row(f"t-{index}", user_id=user_id))
        elif kind == "needs_user":
            rows.append(
                _needs_row(
                    ticket_id=f"t-{index}",
                    user_id=user_id,
                    attention={"reason": "clarify", "question": f"Question {index}?"},
                )
            )
        elif kind == "failure":
            rows.append(
                {
                    "ticket_id": f"t-{index}",
                    "user_id": user_id,
                    "status": "failed",
                    "title": "Ship it",
                    "error": f"error {index}",
                }
            )
        elif kind == "dead_daemon":
            rows.append(
                {
                    "daemon_id": f"daemon-{index}",
                    "user_id": user_id,
                    "name": f"host-{index}",
                    "provider": "claude",
                    "age_seconds": 400,
                }
            )
        else:
            rows.append(
                {
                    "ticket_id": f"t-{index}",
                    "user_id": user_id,
                    "status": "blocked",
                    "attention": {
                        "reason": "quota_exhausted",
                        "provider": f"provider-{index}",
                    },
                }
            )
    return rows


def _stale_event(kind: str) -> notes.NotificationEvent | None:
    if kind == "stuck":
        return notes.stuck_event(_stuck_row("gone", user_id="stale-user"))
    if kind == "needs_user":
        return notes.needs_user_event(
            _needs_row(
                ticket_id="gone",
                user_id="stale-user",
                attention={"reason": "clarify", "question": "Gone?"},
            )
        )
    if kind == "failure":
        return notes.failure_event(
            {
                "ticket_id": "gone",
                "user_id": "stale-user",
                "status": "failed",
                "title": "Old",
                "error": "old",
            }
        )
    if kind == "dead_daemon":
        return notes.dead_daemon_event(
            {
                "daemon_id": "gone",
                "user_id": "stale-user",
                "provider": "claude",
                "age_seconds": 900,
            }
        )
    return notes.worker_quota_event(
        {
            "user_id": "stale-user",
            "status": "blocked",
            "attention": {"reason": "quota_exhausted", "provider": "retired"},
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "lister", "check"),
    [
        ("stuck", "list_stuck_tickets", notes.check_stuck_tickets),
        ("needs_user", "list_needs_user", notes.check_needs_user),
        ("failure", "list_failures", notes.check_failures),
        ("dead_daemon", "list_dead_daemons", notes.check_dead_daemons),
        ("quota", "list_quota_blocked", notes.check_worker_quota),
    ],
)
async def test_scans_walk_every_page_and_then_clear_stale_alerts(
    monkeypatch, kind, lister, check
):
    """202 rows: the first page is 200, so a scan that repeats it misses the last user."""
    count = PAGE_SIZE + 2
    rows = _overflow_rows(kind, count)
    calls: list[tuple[int, int]] = []

    async def listed(**kwargs):
        offset = int(kwargs["offset"])
        limit = int(kwargs["limit"])
        calls.append((offset, limit))
        return rows[offset : offset + limit]

    monkeypatch.setattr(notes, "database_is_open", lambda: True)
    monkeypatch.setattr(notification_db, lister, listed)
    store = RecordingStore()
    stale = _stale_event(kind)
    assert stale is not None
    await store.add(stale)
    channel = _channel([])
    kwargs = {"store": store, "channel": channel}
    if kind in {"stuck", "dead_daemon"}:
        kwargs["environ"] = {}

    created = await check(**kwargs)
    assert created == count
    assert calls == [(0, PAGE_SIZE), (PAGE_SIZE, PAGE_SIZE)]
    assert await store.unread_count("last-user") == 1
    assert await store.unread_count("stale-user") == 0
    assert store.retains[-1][0] == {
        "stuck": KIND_STUCK,
        "needs_user": KIND_NEEDS_USER,
        "failure": KIND_FAILURE,
        "dead_daemon": KIND_DEAD_DAEMON,
        "quota": KIND_WORKER_QUOTA,
    }[kind]

    created_again = await check(**kwargs)
    assert created_again == 0
    assert await store.unread_count("last-user") == 1
    assert await store.unread_count("stale-user") == 0
    assert calls[-2:] == [(0, PAGE_SIZE), (PAGE_SIZE, PAGE_SIZE)]


@pytest.mark.asyncio
async def test_a_failed_later_page_keeps_alerts_it_has_not_seen(monkeypatch):
    rows = _overflow_rows("dead_daemon", PAGE_SIZE + 2)

    async def listed(**kwargs):
        if int(kwargs["offset"]):
            raise RuntimeError("page down")
        return rows[: int(kwargs["limit"])]

    monkeypatch.setattr(notes, "database_is_open", lambda: True)
    monkeypatch.setattr(notification_db, "list_dead_daemons", listed)
    store = RecordingStore()
    stale = _stale_event("dead_daemon")
    assert stale is not None
    await store.add(stale)

    assert (
        await notes.check_dead_daemons(
            store=store, channel=_channel([]), environ={}
        )
        == PAGE_SIZE
    )
    assert store.retains == []
    assert await store.unread_count("last-user") == 0
    assert await store.unread_count("stale-user") == 1


@pytest.mark.asyncio
async def test_banners_past_the_cap_are_delivered_on_the_next_pass():
    questions = [f"Question {index}?" for index in range(4)]
    rows = [
        _needs_row(
            ticket_id=f"need-{index}",
            attention={"reason": "clarify", "question": question},
        )
        for index, question in enumerate(questions)
    ]
    store = MemoryNotificationStore()
    banners: list[tuple[str, str]] = []
    channel = _channel(banners)
    assert (
        await notes.check_needs_user(loader=_loader(rows), store=store, channel=channel)
        == 4
    )
    assert [body for _title, body in banners] == questions[:3]
    assert (
        await notes.check_needs_user(loader=_loader(rows), store=store, channel=channel)
        == 0
    )
    assert [body for _title, body in banners] == questions
    await notes.check_needs_user(loader=_loader(rows), store=store, channel=channel)
    assert [body for _title, body in banners] == questions


# ---------------------------------------------------------------------------
# SQL shape. The runner must not claim these rows, and the badge is per user.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notification_rows_are_finished_effects(mock_db_connection, mock_cursor):
    mock_cursor.fetchone = AsyncMock(return_value=("effect-1",))
    assert (
        await notification_db.insert_notification(
            user_id="user-a",
            dedup_key="notify:stuck:t:running",
            payload={"notification_kind": "stuck", "read": False},
        )
        is True
    )
    sql, params = mock_cursor.execute.await_args.args
    assert "INSERT INTO autonomy_effects" in sql
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in sql
    assert "'done'" in sql
    assert params[0] == "user-a"
    assert params[1] == notification_db.EFFECT_KIND == "user_notification"
    assert params[2] == "notify:stuck:t:running"

    mock_cursor.execute.reset_mock()
    assert (
        await notification_db.insert_notification(
            user_id="  ",
            dedup_key="notify:stuck:t:running",
            payload={},
        )
        is False
    )
    mock_cursor.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_unread_count_and_retain_sql(mock_db_connection, mock_cursor):
    mock_cursor.fetchone = AsyncMock(return_value=(3,))
    assert await notification_db.count_unread("user-a") == 3
    sql, params = mock_cursor.execute.await_args.args
    assert "COUNT(*)" in sql
    assert "user_id = %s" in sql
    assert "payload->>'read'" in sql
    assert "LIKE" not in sql
    assert params == ("user-a", "user_notification")

    mock_cursor.rowcount = 2
    assert await notification_db.retain_notifications("needs_user", []) == 2
    sql, params = mock_cursor.execute.await_args.args
    assert "DELETE FROM autonomy_effects" in sql
    assert "payload->>'notification_kind' = %s" in sql
    assert "ANY(%s)" in sql
    assert "LIKE" not in sql
    assert params == ("user_notification", "needs_user", [])


@pytest.mark.asyncio
async def test_scan_sql_uses_the_interval_and_skips_root_tickets(
    mock_db_connection, mock_cursor
):
    mock_cursor.fetchall = AsyncMock(return_value=[])
    await notification_db.list_stuck_tickets(min_seconds=1800, limit=10)
    sql, params = mock_cursor.execute.await_args.args
    assert "INTERVAL '1 second'" in sql
    assert "OFFSET %s" in sql
    assert "ticket_id ASC" in sql
    assert "main_agent_root" in sql
    assert "orchestration_only" in sql
    assert "queued" in sql and "blocked" in sql
    assert params == (1800, None, 10, 0)

    await notification_db.list_dead_daemons(min_seconds=120, limit=10)
    sql, params = mock_cursor.execute.await_args.args
    assert "COALESCE(last_seen_at, created_at)" in sql
    assert "INTERVAL '1 second'" in sql
    assert "OFFSET %s" in sql
    assert "daemon_id ASC" in sql
    assert params == (120, 10, 0)

    await notification_db.list_needs_user(limit=10)
    sql, params = mock_cursor.execute.await_args.args
    assert "quota_exhausted" in sql
    assert "quota_retries_exhausted" in sql
    assert "main_agent_root" in sql
    assert "awaiting_clarification" in sql
    assert "queued" in sql and "blocked" in sql
    assert "OFFSET %s" in sql
    assert params == (10, 0)

    await notification_db.list_failures(limit=10)
    sql, params = mock_cursor.execute.await_args.args
    assert "status = 'failed'" in sql
    assert "OFFSET %s" in sql
    assert params == (None, None, 10, 0)

    await notification_db.list_quota_blocked(limit=10)
    sql, params = mock_cursor.execute.await_args.args
    assert "quota_exhausted" in sql
    assert "main_agent_root" in sql
    assert "OFFSET %s" in sql
    assert params == (10, 0)
