"""Subscription lifecycle SQL: terminal auto-end, guarded unsubscribe, handoffs.

The fake cursor records executed SQL so the durable rules stay pinned without
a database: ending a Ticket ends its subscriptions, unsubscribing and
reassigning use a guarded idempotent UPDATE, reactivation clears the end
columns, input moves keep delivered/handed-back rows behind, inputs the
target already holds are adopted (not stranded), and every live subscriber
query excludes ended rows.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from psycopg.types.json import Json

from src.server.database.autonomy import tickets


class _Cursor:
    def __init__(self, fetchone_results=(), fetchall_results=()):
        self._fetchone = iter(fetchone_results)
        self._fetchall = iter(fetchall_results)
        self.executions: list[tuple[str, tuple]] = []
        self.rowcount = 1

    async def execute(self, sql: str, params=()):
        self.executions.append((" ".join(sql.split()), tuple(params)))

    async def fetchone(self):
        return next(self._fetchone, None)

    async def fetchall(self):
        return next(self._fetchall, [])


class _Connection:
    def __init__(self, cursor: _Cursor):
        self.fake_cursor = cursor

    @asynccontextmanager
    async def cursor(self, **_kwargs):
        yield self.fake_cursor

    @asynccontextmanager
    async def transaction(self):
        yield


def _patch_connection(monkeypatch, cursor: _Cursor) -> None:
    @asynccontextmanager
    async def get_connection(_conn=None):
        yield _Connection(cursor)

    monkeypatch.setattr(tickets, "get_db_connection", get_connection)


def _subscription_updates(cursor: _Cursor) -> list[tuple[str, tuple]]:
    return [
        (sql, params)
        for sql, params in cursor.executions
        if "UPDATE autonomy_subscriptions" in sql
    ]


@pytest.mark.asyncio
async def test_terminal_update_ends_active_subscriptions(monkeypatch):
    cursor = _Cursor(fetchone_results=[{"ticket_id": "t-1"}])
    _patch_connection(monkeypatch, cursor)

    await tickets.update_ticket("t-1", status="completed")

    updates = _subscription_updates(cursor)
    assert len(updates) == 1
    sql, params = updates[0]
    assert "ended_at IS NULL" in sql
    assert "end_reason = %s" in sql
    assert params == (tickets.END_TICKET_ENDED, "t-1")


@pytest.mark.asyncio
async def test_archived_update_ends_active_subscriptions(monkeypatch):
    """Archive is terminal ownership state, so it ends live subscriptions too."""
    cursor = _Cursor(fetchone_results=[{"ticket_id": "t-1", "status": "archived"}])
    _patch_connection(monkeypatch, cursor)

    await tickets.update_ticket("t-1", status="archived")

    updates = _subscription_updates(cursor)
    assert len(updates) == 1
    _sql, params = updates[0]
    assert params == (tickets.END_TICKET_ENDED, "t-1")


@pytest.mark.asyncio
async def test_update_ticket_fences_revision_and_worker_thread(monkeypatch):
    """Ownership's revision fence and the switch line's thread fence both apply."""
    cursor = _Cursor(fetchone_results=[{"ticket_id": "t-1", "status": "running"}])
    _patch_connection(monkeypatch, cursor)

    await tickets.update_ticket(
        "t-1",
        expected_status="running",
        expected_revision=4,
        expected_thread_id="th-1",
        summary="kept",
    )

    sql, params = cursor.executions[0]
    assert "AND revision = %s" in sql
    assert "AND worker_thread_id = %s" in sql
    assert params == ("kept", "t-1", "running", 4, "th-1")


@pytest.mark.asyncio
async def test_non_terminal_update_leaves_subscriptions_alone(monkeypatch):
    cursor = _Cursor(fetchone_results=[{"ticket_id": "t-1"}])
    _patch_connection(monkeypatch, cursor)

    await tickets.update_ticket("t-1", status="waiting", summary="paused")

    assert _subscription_updates(cursor) == []


@pytest.mark.asyncio
async def test_update_ticket_writes_the_audit_event_on_the_update_transaction(monkeypatch):
    cursor = _Cursor(fetchone_results=[{"ticket_id": "t-1", "status": "queued"}])
    _patch_connection(monkeypatch, cursor)

    await tickets.update_ticket(
        "t-1",
        expected_status="cancelled",
        status="queued",
        ended_at=None,
        event={
            "ticket_id": "t-1",
            "user_id": "u1",
            "actor": "user",
            "action": "resumed",
            "from_owner": "d1",
            "to_owner": "cloud",
            "detail": {"from_status": "cancelled"},
        },
    )

    assert len(cursor.executions) == 2
    assert "UPDATE autonomy_tickets" in cursor.executions[0][0]
    assert "INSERT INTO autonomy_ticket_events" in cursor.executions[1][0]
    assert cursor.executions[1][1][3] == "resumed"
    assert _subscription_updates(cursor) == []


@pytest.mark.asyncio
async def test_fenced_update_miss_writes_no_audit_event(monkeypatch):
    cursor = _Cursor(fetchone_results=[None])
    _patch_connection(monkeypatch, cursor)

    missed = await tickets.update_ticket(
        "t-1",
        expected_status="cancelled",
        status="queued",
        event={
            "ticket_id": "t-1",
            "user_id": "u1",
            "actor": "user",
            "action": "resumed",
            "detail": {},
        },
    )

    assert missed is None
    assert len(cursor.executions) == 1


@pytest.mark.asyncio
async def test_fenced_update_miss_does_not_touch_subscriptions(monkeypatch):
    cursor = _Cursor(fetchone_results=[None])
    _patch_connection(monkeypatch, cursor)

    await tickets.update_ticket("t-1", status="completed", expected_status="running")

    assert _subscription_updates(cursor) == []


@pytest.mark.asyncio
async def test_worker_report_ending_ticket_ends_active_subscriptions(monkeypatch):
    """record_worker_report settles a Ticket in the same statement as the
    report insert; a terminal settle must end the Ticket's live subscriptions
    there too, or the migration-127 live-row invariant breaks."""
    cursor = _Cursor(fetchone_results=[{"ticket_id": "t-1"}, {"report_id": "r-1"}])
    _patch_connection(monkeypatch, cursor)

    await tickets.record_worker_report(
        ticket_id="t-1",
        expected_thread_id="th-1",
        state="ready",
        summary="done",
        ticket_fields={"status": "completed", "summary": "done", "ended_at": "ts"},
    )

    updates = _subscription_updates(cursor)
    assert len(updates) == 1
    sql, params = updates[0]
    assert "ended_at IS NULL" in sql
    assert "end_reason = %s" in sql
    assert params == (tickets.END_TICKET_ENDED, "t-1")


@pytest.mark.asyncio
async def test_worker_report_non_terminal_status_leaves_subscriptions_alone(monkeypatch):
    cursor = _Cursor(fetchone_results=[{"ticket_id": "t-1"}, {"report_id": "r-1"}])
    _patch_connection(monkeypatch, cursor)

    await tickets.record_worker_report(
        ticket_id="t-1",
        expected_thread_id="th-1",
        state="blocked",
        summary="stuck",
        question="Which branch?",
        ticket_fields={"status": "blocked", "attention": {"question": "Which branch?"}},
    )

    assert _subscription_updates(cursor) == []


@pytest.mark.asyncio
async def test_worker_report_persists_structured_git_check(monkeypatch):
    cursor = _Cursor(fetchone_results=[{"ticket_id": "t-1"}, {"report_id": "r-1"}])
    _patch_connection(monkeypatch, cursor)

    await tickets.record_worker_report(
        ticket_id="t-1",
        expected_thread_id="th-1",
        state="ready",
        summary="done",
        git_check="unsupported_runtime",
        ticket_fields={"status": "completed", "summary": "done"},
    )

    sql, params = cursor.executions[-1]
    assert "body, git_check" in sql
    assert params[-1] == "unsupported_runtime"


@pytest.mark.asyncio
async def test_worker_report_rejects_unknown_structured_git_check(monkeypatch):
    cursor = _Cursor()
    _patch_connection(monkeypatch, cursor)

    with pytest.raises(ValueError, match="git_check"):
        await tickets.record_worker_report(
            ticket_id="t-1",
            expected_thread_id="th-1",
            state="ready",
            summary="done",
            git_check="passed",
            ticket_fields={"status": "completed", "summary": "done"},
        )
    assert cursor.executions == []


@pytest.mark.asyncio
async def test_stale_worker_report_does_not_touch_subscriptions(monkeypatch):
    """A report fenced out by the guarded UPDATE returns None (the host maps it
    to ignored_switched) and must not end anyone's subscriptions."""
    cursor = _Cursor(fetchone_results=[None])
    _patch_connection(monkeypatch, cursor)

    result = await tickets.record_worker_report(
        ticket_id="t-1",
        expected_thread_id="th-1",
        state="ready",
        summary="late",
        ticket_fields={"status": "completed"},
    )

    assert result is None
    assert _subscription_updates(cursor) == []


@pytest.mark.asyncio
async def test_end_subscription_is_guarded_and_bumps_revision(monkeypatch):
    ended = {"ticket_id": "t-1", "thread_key": "th-1", "kind": "own"}
    cursor = _Cursor(fetchone_results=[ended])
    _patch_connection(monkeypatch, cursor)

    result = await tickets.end_subscription("t-1", "th-1", reason="unsubscribed")

    assert result["kind"] == "own"
    sql, params = cursor.executions[0]
    assert "ended_at IS NULL" in sql
    assert params == ("unsubscribed", "t-1", "th-1")
    assert cursor.executions[1][0] == (
        "UPDATE autonomy_tickets SET revision = revision + 1 WHERE ticket_id = %s"
    )
    assert cursor.executions[1][1] == ("t-1",)


@pytest.mark.asyncio
async def test_end_subscription_miss_does_not_bump_revision(monkeypatch):
    cursor = _Cursor(fetchone_results=[None])
    _patch_connection(monkeypatch, cursor)

    result = await tickets.end_subscription("t-1", "th-1", reason="reassigned")

    assert result is None
    assert len(cursor.executions) == 1


@pytest.mark.asyncio
async def test_end_subscription_rejects_unknown_reasons(monkeypatch):
    cursor = _Cursor()
    _patch_connection(monkeypatch, cursor)

    with pytest.raises(ValueError, match="end reason"):
        await tickets.end_subscription("t-1", "th-1", reason="expired")

    assert cursor.executions == []


@pytest.mark.asyncio
async def test_add_subscription_reactivates_an_ended_row(monkeypatch):
    cursor = _Cursor()
    _patch_connection(monkeypatch, cursor)

    await tickets.add_subscription("t-1", "th-1", kind="context")

    sql, params = cursor.executions[0]
    assert "ON CONFLICT" in sql
    assert "ended_at = NULL" in sql
    assert "end_reason = NULL" in sql
    # A live 'own' stays 'own', but reactivation of an ENDED row takes the
    # requested kind — a context-only re-follow must not resurrect ownership
    # given up by unsubscribe or a handoff.
    assert (
        "WHEN autonomy_subscriptions.ended_at IS NULL"
        " AND autonomy_subscriptions.kind = 'own' THEN 'own'"
    ) in sql
    assert params == ("t-1", "th-1", "context")


@pytest.mark.asyncio
async def test_move_undelivered_inputs_keeps_history_behind(monkeypatch):
    cursor = _Cursor(
        fetchall_results=[[], [{"input_id": "in-1", "trigger_id": "trg-1"}]]
    )
    _patch_connection(monkeypatch, cursor)

    moved = await tickets.move_undelivered_inputs(
        from_ticket_id="from-1", to_ticket_id="to-1", thread_key="th-1"
    )

    assert [m["input_id"] for m in moved] == ["in-1"]
    sql, params = cursor.executions[1]
    for guard in (
        "delivered_at IS NULL",
        "handed_back = FALSE",
        "NOT EXISTS",
        "tr.thread_key = %s",
    ):
        assert guard in sql
    assert params == ("to-1", "th-1", "from-1", "to-1")
    assert cursor.executions[2][0].startswith("UPDATE autonomy_tickets SET revision")


@pytest.mark.asyncio
async def test_move_without_matches_skips_the_revision_bump(monkeypatch):
    cursor = _Cursor(fetchall_results=[[], []])
    _patch_connection(monkeypatch, cursor)

    moved = await tickets.move_undelivered_inputs(
        from_ticket_id="from-1", to_ticket_id="to-1", thread_key="th-1"
    )

    assert moved == []
    assert len(cursor.executions) == 2


@pytest.mark.asyncio
async def test_move_adopts_inputs_the_target_already_holds(monkeypatch):
    """A context subscriber already holds an input row for the same Trigger;
    the actionable payload must take that row over (surviving id) and the
    previous owner's duplicate must be deleted, not left stranded."""
    cursor = _Cursor(
        fetchall_results=[
            [{"input_id": "surviving-9", "trigger_id": "trg-1", "kind": "trigger", "text": "ping"}],
            [],
        ]
    )
    _patch_connection(monkeypatch, cursor)

    moved = await tickets.move_undelivered_inputs(
        from_ticket_id="from-1", to_ticket_id="to-1", thread_key="th-1"
    )

    assert [m["input_id"] for m in moved] == ["surviving-9"]
    merge_sql, merge_params = cursor.executions[0]
    assert merge_sql.startswith("UPDATE autonomy_ticket_inputs tgt")
    assert "FROM autonomy_ticket_inputs src" in merge_sql
    assert "tgt.trigger_id = src.trigger_id" in merge_sql
    assert "delivered_at = NULL" in merge_sql
    assert merge_params == ("from-1", "th-1", "to-1")
    delete_sql, delete_params = cursor.executions[1]
    assert delete_sql.startswith("DELETE FROM autonomy_ticket_inputs")
    assert delete_params == ("from-1", ["trg-1"])
    assert cursor.executions[3][0].startswith("UPDATE autonomy_tickets SET revision")


@pytest.mark.asyncio
async def test_record_handoff_persists_state_and_reason(monkeypatch):
    cursor = _Cursor(fetchone_results=[{"handoff_id": "h-1", "created_at": "now"}])
    _patch_connection(monkeypatch, cursor)

    row = await tickets.record_handoff(
        thread_key="th-1",
        user_id="u-1",
        from_ticket_id="from-1",
        to_ticket_id="to-1",
        reason="better owner",
        state={"latest_report": {"summary": "half"}, "moved_input_ids": ["in-1"]},
    )

    assert row["handoff_id"] == "h-1"
    sql, params = cursor.executions[0]
    assert sql.startswith("INSERT INTO autonomy_handoffs")
    assert params[:5] == ("th-1", "u-1", "from-1", "to-1", "better owner")


@pytest.mark.asyncio
async def test_record_handoff_state_is_json_safe(monkeypatch):
    """Handoff state is assembled from raw report rows: a datetime created_at
    must not abort the bind (and take the moved ownership down with it), and
    NUL bytes must not make Postgres reject the row."""
    from datetime import datetime, timezone
    from uuid import uuid4

    cursor = _Cursor(fetchone_results=[{"handoff_id": "h-1", "created_at": "now"}])
    _patch_connection(monkeypatch, cursor)
    report_id = uuid4()
    stamp = datetime(2026, 11, 2, 12, 30, tzinfo=timezone.utc)
    state = {
        "latest_report": {"report_id": report_id, "created_at": stamp},
        "note": "nul \x00 byte",
    }

    await tickets.record_handoff(
        thread_key="th-1",
        user_id="u-1",
        from_ticket_id="from-1",
        to_ticket_id="to-1",
        reason="better owner",
        state=state,
    )

    bound = cursor.executions[0][1][5]
    assert isinstance(bound, Json)
    encoded = json.loads(bound.dumps(bound.obj))
    assert encoded["latest_report"]["created_at"] == stamp.isoformat()
    assert encoded["latest_report"]["report_id"] == str(report_id)
    assert encoded["note"] == "nul  byte"
    # The caller's tree is not mutated.
    assert state["latest_report"]["created_at"] is stamp


@pytest.mark.asyncio
async def test_list_handoffs_for_ticket_selects_the_inheriting_side(monkeypatch):
    cursor = _Cursor(
        fetchall_results=[
            [
                {
                    "handoff_id": "h-1",
                    "thread_key": "th-1",
                    "from_ticket_id": "from-1",
                    "to_ticket_id": "to-1",
                    "state": {"moved_input_ids": ["in-1"]},
                }
            ]
        ]
    )
    _patch_connection(monkeypatch, cursor)

    rows = await tickets.list_handoffs_for_ticket("to-1")

    assert rows[0]["handoff_id"] == "h-1"
    assert rows[0]["state"] == {"moved_input_ids": ["in-1"]}
    sql, params = cursor.executions[0]
    assert "WHERE to_ticket_id = %s" in sql
    assert params == ("to-1", 10)


@pytest.mark.asyncio
async def test_live_subscriber_queries_exclude_ended_rows(monkeypatch):
    cursor = _Cursor(fetchall_results=[[{"ticket_id": "t-1"}], [], []])
    _patch_connection(monkeypatch, cursor)

    await tickets.list_tickets_for_thread("th-1")
    await tickets.context_subscribers("th-1")
    await tickets.list_subscriptions("t-1", active_only=True)

    thread_sql = cursor.executions[0][0]
    assert "s.ended_at IS NULL" in thread_sql
    context_sql = cursor.executions[1][0]
    assert "s.ended_at IS NULL" in context_sql
    assert "archived" in context_sql
    subs_sql = cursor.executions[2][0]
    assert "ended_at IS NULL" in subs_sql


@pytest.mark.asyncio
async def test_claim_input_delivery_is_guarded_by_ownership(monkeypatch):
    """The delivery claim fences on the current owner and undelivered state:
    a reassignment that moved the row first, or an effect retry after a
    successful delivery, claims nothing."""
    cursor = _Cursor(
        fetchone_results=[{"input_id": "in-1", "ticket_id": "t-1", "delivered_at": "ts-1"}]
    )
    _patch_connection(monkeypatch, cursor)

    claimed = await tickets.claim_input_delivery("in-1", "t-1")

    assert claimed["delivered_at"] == "ts-1"
    sql, params = cursor.executions[0]
    assert "UPDATE autonomy_ticket_inputs" in sql
    assert "delivered_at IS NULL" in sql
    assert params == ("in-1", "t-1")
    assert cursor.executions[1][0] == (
        "UPDATE autonomy_tickets SET revision = revision + 1 WHERE ticket_id = %s"
    )
    assert cursor.executions[1][1] == ("t-1",)


@pytest.mark.asyncio
async def test_claim_input_delivery_miss_claims_nothing(monkeypatch):
    cursor = _Cursor(fetchone_results=[None])
    _patch_connection(monkeypatch, cursor)

    assert await tickets.claim_input_delivery("in-1", "t-1") is None
    assert len(cursor.executions) == 1


@pytest.mark.asyncio
async def test_release_input_delivery_targets_the_exact_claim(monkeypatch):
    """Only a row still carrying our claim timestamp is cleared, so a release
    cannot unset a newer claim made by another path."""
    cursor = _Cursor()
    _patch_connection(monkeypatch, cursor)

    assert await tickets.release_input_delivery("in-1", "t-1", "ts-1") is True
    sql, params = cursor.executions[0]
    assert "SET delivered_at = NULL" in sql
    assert "delivered_at = %s" in sql
    assert params == ("in-1", "t-1", "ts-1")


@pytest.mark.asyncio
async def test_release_input_delivery_miss_is_a_noop(monkeypatch):
    cursor = _Cursor()
    cursor.rowcount = 0
    _patch_connection(monkeypatch, cursor)

    assert await tickets.release_input_delivery("in-1", "t-1", "ts-1") is False
    assert len(cursor.executions) == 1


@pytest.mark.asyncio
async def test_admission_returns_the_prior_status_for_failed_dispatch_restore(
    monkeypatch,
):
    """Admission overwrites the status to admit the dispatch; it must report
    what it replaced so a failed dispatch can put a non-running ticket back
    (the row lock also refuses archived, cancelled and completed tickets)."""
    cursor = _Cursor(
        fetchone_results=[
            {"status": "blocked"},
            {"ticket_id": "t-1", "status": "running"},
        ]
    )
    _patch_connection(monkeypatch, cursor)

    row = await tickets.admit_followup_delivery("t-1")

    assert row["prior_status"] == "blocked"
    select_sql, select_params = cursor.executions[0]
    assert "SELECT status" in select_sql
    assert "FOR UPDATE" in select_sql
    assert select_params == ("t-1",)
    update_sql, update_params = cursor.executions[1]
    assert "SET status = 'running'" in update_sql
    assert "status NOT IN ('completed', 'cancelled', 'archived')" in update_sql
    assert update_params == ("t-1",)


@pytest.mark.asyncio
async def test_discard_soft_ends_subscriptions_like_other_terminal_paths(
    monkeypatch,
):
    """complete_discarded_plan must end subscriptions exactly like
    update_ticket and the daemon report settle (guarded soft-end, row kept):
    a hard DELETE hid the plan's reports from thread history and the
    subscription audit listing."""
    cursor = _Cursor(fetchone_results=[{"ticket_id": "t-1", "status": "completed"}])
    _patch_connection(monkeypatch, cursor)

    result = await tickets.complete_discarded_plan(
        "t-1",
        expected_status="waiting",
        mandate={"speculative": True},
        ended_at="ts",
    )

    assert result is not None
    updates = _subscription_updates(cursor)
    assert len(updates) == 1
    sql, params = updates[0]
    assert "ended_at IS NULL" in sql
    assert "DELETE" not in sql
    assert params == (tickets.END_TICKET_ENDED, "t-1")


@pytest.mark.asyncio
async def test_thread_report_history_joins_ended_subscriptions(monkeypatch):
    """Thread history lists reports through the subscription join; it must
    keep ended rows (a discarded plan's reports stay visible) — only routing
    queries filter on ended_at."""
    cursor = _Cursor(fetchall_results=[[]])
    _patch_connection(monkeypatch, cursor)

    await tickets.list_reports_for_thread("th-1")

    sql = cursor.executions[0][0]
    assert "JOIN autonomy_subscriptions s" in sql
    assert "ended_at IS NULL" not in sql


@pytest.mark.asyncio
async def test_get_ticket_for_update_locks_the_row(monkeypatch):
    cursor = _Cursor(fetchone_results=[None])
    _patch_connection(monkeypatch, cursor)

    await tickets.get_ticket("t-1", for_update=True)

    sql, params = cursor.executions[0]
    assert sql.endswith("FOR UPDATE")
    assert params == ("t-1",)


class _SessionCursor:
    def __init__(self, conn):
        self._conn = conn

    async def execute(self, sql: str, params=()):
        self._conn.executions.append((" ".join(sql.split()), tuple(params)))

    async def fetchone(self):
        return (True,)


class _SessionConnection:
    def __init__(self):
        self.executions: list[tuple[str, tuple]] = []
        self.closed = False

    def cursor(self):
        conn = self

        @asynccontextmanager
        async def _cm():
            yield _SessionCursor(conn)

        return _cm()

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_thread_routing_lock_acquires_and_releases_the_advisory_lock(monkeypatch):
    """Intake and reassignment serialize on one session-level advisory lock
    per Thread; it must be taken before the body runs and released after,
    even when the body raises."""
    session = _SessionConnection()
    monkeypatch.setattr(
        tickets, "open_session_connection", AsyncMock(return_value=session)
    )

    with pytest.raises(RuntimeError, match="boom"):
        async with tickets.thread_routing_lock("th-1"):
            raise RuntimeError("boom")

    statements = [sql for sql, _ in session.executions]
    assert statements == [
        "SELECT pg_try_advisory_lock(hashtextextended(%s, 733))",
        "SELECT pg_advisory_unlock(hashtextextended(%s, 733))",
    ]
    assert session.executions[0][1] == ("th-1",)
    assert session.executions[1][1] == ("th-1",)
    assert session.closed


@pytest.mark.asyncio
async def test_record_worker_report_fences_the_run_id(monkeypatch):
    """A resumed line reuses its thread; the run id must fence old reports."""
    cursor = _Cursor(fetchone_results=[{"ticket_id": "t-1"}, {"report_id": "r-1"}])
    _patch_connection(monkeypatch, cursor)

    await tickets.record_worker_report(
        ticket_id="t-1",
        expected_thread_id="th-1",
        expected_run_id="run-1",
        state="ready",
        summary="done",
        ticket_fields={"status": "completed", "summary": "done"},
    )

    updates = [
        (sql, params)
        for sql, params in cursor.executions
        if "UPDATE autonomy_tickets SET" in sql
    ]
    assert len(updates) == 1
    sql, params = updates[0]
    assert "worker_run_id = %s" in sql
    assert params[-1] == "run-1"


@pytest.mark.asyncio
async def test_update_ticket_can_fence_the_run_id(monkeypatch):
    cursor = _Cursor(fetchone_results=[{"ticket_id": "t-1"}])
    _patch_connection(monkeypatch, cursor)

    await tickets.update_ticket(
        "t-1",
        status="completed",
        expected_thread_id="th-1",
        expected_run_id="run-1",
    )

    updates = [
        (sql, params)
        for sql, params in cursor.executions
        if "UPDATE autonomy_tickets SET" in sql
    ]
    sql, params = updates[0]
    assert "worker_run_id = %s" in sql
    assert params[-1] == "run-1"

@pytest.mark.asyncio
async def test_fail_ticket_with_report_is_one_atomic_transaction(monkeypatch):
    """The transition and its failure report must commit together, so a retried
    effect cannot skip an already-failed Ticket with no report."""
    cursor = _Cursor(fetchone_results=[{"ticket_id": "t-1"}])
    _patch_connection(monkeypatch, cursor)

    updated = await tickets.fail_ticket_with_report(
        "t-1",
        error="runtime exploded",
        summary="runtime exploded",
        expected_revision=7,
        expected_status="queued",
    )

    assert updated is not None
    updates = [
        (sql, params)
        for sql, params in cursor.executions
        if "UPDATE autonomy_tickets SET" in sql
    ]
    reports = [
        (sql, params)
        for sql, params in cursor.executions
        if "INSERT INTO autonomy_ticket_reports" in sql
    ]
    assert len(updates) == 1 and len(reports) == 1
    update_sql, update_params = updates[0]
    assert "status = %s" in update_sql
    assert "revision = revision + 1" in update_sql
    assert "revision = %s" in update_sql
    assert "status = %s" in update_sql.split("WHERE")[1]
    assert update_params[0] == "failed"
    assert update_params[-1] == 7
    report_sql, report_params = reports[0]
    assert "'failed'" in report_sql
    assert report_params == ("t-1", "runtime exploded")


@pytest.mark.asyncio
async def test_fail_ticket_with_report_returns_none_on_a_fence_miss(monkeypatch):
    cursor = _Cursor(fetchone_results=[None])
    _patch_connection(monkeypatch, cursor)

    updated = await tickets.fail_ticket_with_report(
        "t-1", error="x", summary="x", expected_revision=3
    )

    assert updated is None
    assert not [
        sql for sql, _params in cursor.executions if "INSERT INTO autonomy_ticket_reports" in sql
    ]

@pytest.mark.asyncio
async def test_isolated_heartbeat_updates_only_the_heartbeat_field(monkeypatch):
    """A full-mandate replace could resurrect container_started=False, so the
    heartbeat must be an atomic jsonb_set on one field."""
    cursor = _Cursor()
    cursor.rowcount = 1
    _patch_connection(monkeypatch, cursor)

    touched = await tickets.touch_isolated_worker_heartbeat(
        "t-1",
        thread_id="th-1",
        run_id="run-1",
        heartbeat_at="2026-11-05T00:00:00+00:00",
    )

    assert touched is True
    assert len(cursor.executions) == 1
    sql, params = cursor.executions[0]
    assert "jsonb_set" in sql
    assert "{cloud_worker,heartbeat_at}" in sql
    assert "worker_thread_id = %s" in sql
    assert "worker_run_id = %s" in sql
    # Only the heartbeat value is parameterized; the mandate itself is never
    # replaced by a caller-supplied JSON object.
    assert params == ("2026-11-05T00:00:00+00:00", "t-1", "th-1", "run-1")


@pytest.mark.asyncio
async def test_isolated_heartbeat_reports_a_stale_line_as_not_touched(monkeypatch):
    cursor = _Cursor()
    cursor.rowcount = 0
    _patch_connection(monkeypatch, cursor)

    touched = await tickets.touch_isolated_worker_heartbeat(
        "t-1", thread_id="th-1", run_id="run-1", heartbeat_at="now"
    )

    assert touched is False
