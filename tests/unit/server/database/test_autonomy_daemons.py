import asyncio
import threading
from datetime import UTC
from unittest.mock import patch

import pytest

from src.server.database.autonomy import daemons, schedules


@pytest.mark.asyncio
async def test_active_claim_replays_the_exact_delivered_input_snapshot():
    snapshot = [
        {
            "input_id": "input-1",
            "kind": "answer",
            "text": "Use the main branch.",
            "created_at": None,
        }
    ]
    conn = _Conn(
        [
            {
                "ticket_id": "ticket-1",
                "worker_lease_id": "lease-1",
                "worker_lease_expires_at": None,
                "remaining": 77,
                "input_snapshot": snapshot,
            }
        ]
    )
    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        return_value=conn,
    ):
        replay = await daemons.active_claim("daemon-1", "claim-1")

    assert replay["inputs"] == snapshot
    assert "c.input_snapshot" in conn.cur.calls[0][0]


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Cursor:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.calls = []
        self.rowcount = 1
        self.closed = False

    async def execute(self, sql, params=None):
        if self.closed:
            raise AssertionError("database cursor used after its context closed")
        self.calls.append((" ".join(sql.split()), params))

    async def fetchone(self):
        return next(self.rows)

    async def fetchall(self):
        return []

    async def __aenter__(self):
        self.closed = False
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False


class _Conn:
    def __init__(self, rows):
        self.cur = _Cursor(rows)
        self.transactions = 0

    def cursor(self, row_factory=None):
        return self.cur

    def transaction(self):
        self.transactions += 1
        return _Tx()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ClaimState:
    def __init__(self, daemons, tickets):
        self.daemons = daemons
        self.tickets = tickets
        self.locks = {daemon_id: asyncio.Lock() for daemon_id in daemons}


class _ClaimTx:
    def __init__(self, conn):
        self.conn = conn
        self.held = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        for lock in reversed(self.held):
            lock.release()
        return False


class _ClaimCursor:
    def __init__(self, conn):
        self.conn = conn
        self.row = None

    async def execute(self, sql, params=None):
        from datetime import datetime, timedelta

        normalized = " ".join(sql.split())
        if "FROM autonomy_daemons" in normalized and "FOR UPDATE" in normalized:
            daemon_id = params[0]
            lock = self.conn.state.locks[daemon_id]
            await lock.acquire()
            self.conn.tx.held.append(lock)
            self.row = self.conn.state.daemons.get(daemon_id)
        elif "COUNT(*) AS running_count" in normalized:
            daemon_id, user_id = params
            count = sum(
                ticket["worker_daemon_id"] == daemon_id
                and ticket["user_id"] == user_id
                and ticket["status"] == "running"
                and ticket.get("worker_lease_expires_at") is not None
                and ticket["worker_lease_expires_at"] > datetime.now(UTC)
                for ticket in self.conn.state.tickets
            )
            self.row = {"running_count": count}
            await asyncio.sleep(0)
        elif normalized.startswith("WITH next AS ("):
            (
                daemon_id,
                user_id,
                provider,
                _set_provider,
                lease_id,
                claim_id,
                lease_seconds,
            ) = params
            ticket = next(
                (
                    item
                    for item in self.conn.state.tickets
                    if item["status"] == "queued"
                    and item["worker_daemon_id"] == daemon_id
                    and item["user_id"] == user_id
                    and item["worker_provider"] in ("", provider)
                ),
                None,
            )
            if ticket:
                ticket["status"] = "running"
                ticket["worker_provider"] = ticket["worker_provider"] or provider
                ticket["attempts"] += 1
                ticket["worker_lease_id"] = lease_id
                ticket["worker_lease_claim_id"] = claim_id
                ticket["worker_lease_expires_at"] = datetime.now(UTC) + timedelta(
                    seconds=lease_seconds
                )
            self.row = dict(ticket) if ticket else None
            await asyncio.sleep(0)
        elif "worker_lease_claim_id = %s" in normalized:
            daemon_id, user_id, claim_id = params
            self.row = next(
                (
                    dict(ticket)
                    for ticket in self.conn.state.tickets
                    if ticket["worker_daemon_id"] == daemon_id
                    and ticket["user_id"] == user_id
                    and ticket.get("worker_lease_claim_id") == claim_id
                    and ticket["status"] == "running"
                    and ticket.get("worker_lease_expires_at") is not None
                    and ticket["worker_lease_expires_at"] > datetime.now(UTC)
                ),
                None,
            )
            if self.row is not None:
                self.row["remaining"] = 120
        elif "FROM autonomy_daemon_claims WHERE claim_id=%s" in normalized:
            self.row = None
        elif normalized.startswith("INSERT INTO autonomy_daemon_claims"):
            self.row = None
        elif normalized.startswith("INSERT INTO autonomy_ticket_events"):
            self.row = None
        else:
            raise AssertionError(f"Unexpected claim SQL: {normalized}")

    async def fetchone(self):
        return self.row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ClaimConn:
    def __init__(self, state):
        self.state = state
        self.tx = None

    def transaction(self):
        self.tx = _ClaimTx(self)
        return self.tx

    def cursor(self, row_factory=None):
        return _ClaimCursor(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_daemon_claim_locks_owner_and_binds_all_sql_parameters():
    from datetime import datetime

    ticket = {
        "ticket_id": "t1",
        "worker_daemon_id": "d1",
        "worker_lease_expires_at": datetime.now(UTC),
    }
    conn = _Conn(
        [
            {"daemon_id": "d1", "user_id": "u1", "provider": "codex", "max_concurrency": 2},
            {"running_count": 1},
            None,
            None,
            ticket,
        ]
    )
    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        return_value=conn,
    ):
        claimed = await daemons.claim_ticket("d1")
    assert claimed["ticket_id"] == "t1"
    assert claimed["lease_id"]
    assert "FOR UPDATE" in conn.cur.calls[0][0]
    assert "COUNT(*)" in conn.cur.calls[1][0]
    claim_sql, claim_params = conn.cur.calls[4]
    assert claim_params[:4] == ("d1", "u1", "codex", "codex")
    assert "worker_lease_id" in claim_sql
    assert "worker_daemon_id = %s" in claim_sql
    assert "user_id = %s" in claim_sql
    assert "FOR UPDATE SKIP LOCKED" in claim_sql


@pytest.mark.asyncio
async def test_daemon_resume_only_wakes_for_human_input():
    conn = _Conn(
        [
            {"daemon_id": "d1", "user_id": "u1", "provider": "codex", "max_concurrency": 2},
            None,  # no replay for this claim id
            {"running_count": 0},
            None,  # claim id is unused
            {"ticket_id": "t1"},  # a parked Ticket with a human input
            {"ticket_id": "t1", "worker_lease_expires_at": None},
        ]
    )
    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        return_value=conn,
    ):
        assert await daemons.resume_ticket("d1", 4) is not None

    selects = [sql for sql, _ in conn.cur.calls if "SELECT t.ticket_id" in sql]
    updates = [sql for sql, _ in conn.cur.calls if "UPDATE autonomy_tickets t" in sql]
    assert selects and updates
    # A stored context message must not wake a parked daemon Ticket; only a
    # human trigger or answer may.
    assert "i.kind IN ('trigger', 'answer')" in selects[0]
    assert "i.kind IN ('trigger', 'answer')" in updates[0]


@pytest.mark.asyncio
async def test_daemon_claim_stops_when_a_ticket_is_already_running():
    conn = _Conn(
        [
            {"daemon_id": "d1", "user_id": "u1", "provider": "codex", "max_concurrency": 1},
            {"running_count": 1},
            None,
        ]
    )
    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        return_value=conn,
    ):
        assert await daemons.claim_ticket("d1") is None
    assert len(conn.cur.calls) == 3
    assert "worker_lease_expires_at > clock_timestamp()" in conn.cur.calls[1][0]
    assert not any(sql.startswith("WITH next AS (") for sql, _ in conn.cur.calls)


@pytest.mark.asyncio
async def test_expired_lease_does_not_consume_daemon_capacity():
    from datetime import datetime, timedelta

    tickets = [
        {
            "ticket_id": "expired",
            "worker_daemon_id": "d1",
            "user_id": "u1",
            "worker_provider": "codex",
            "status": "running",
            "attempts": 1,
            "worker_lease_expires_at": datetime.now(UTC) - timedelta(seconds=1),
        },
        {
            "ticket_id": "next",
            "worker_daemon_id": "d1",
            "user_id": "u1",
            "worker_provider": "codex",
            "status": "queued",
            "attempts": 0,
            "worker_lease_expires_at": None,
        },
    ]
    state = _ClaimState(
        {
            "d1": {
                "daemon_id": "d1",
                "user_id": "u1",
                "provider": "codex",
                "max_concurrency": 1,
            }
        },
        tickets,
    )
    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        side_effect=lambda: _ClaimConn(state),
    ):
        claimed = await daemons.claim_ticket("d1")
    assert claimed is not None
    assert claimed["ticket_id"] == "next"


@pytest.mark.asyncio
async def test_claim_retry_returns_existing_lease_even_when_at_capacity():
    from datetime import datetime, timedelta

    claim_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    ticket = {
        "ticket_id": "t1",
        "worker_daemon_id": "d1",
        "user_id": "u1",
        "worker_provider": "codex",
        "status": "running",
        "attempts": 1,
        "worker_lease_id": claim_id,
        "worker_lease_claim_id": claim_id,
        "worker_lease_expires_at": datetime.now(UTC) + timedelta(seconds=60),
    }
    state = _ClaimState(
        {
            "d1": {
                "daemon_id": "d1",
                "user_id": "u1",
                "provider": "codex",
                "max_concurrency": 1,
            }
        },
        [ticket],
    )
    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        side_effect=lambda: _ClaimConn(state),
    ):
        replay = await daemons.claim_ticket("d1", claim_id)
    assert replay is not None
    assert replay["ticket_id"] == "t1"
    assert replay["lease_id"] == claim_id


@pytest.mark.asyncio
async def test_heartbeat_renews_each_live_fence_independently():
    conn = _Conn([("t1",), None])
    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        return_value=conn,
    ):
        result = await daemons.heartbeat(
            "d1",
            [
                {"ticket_id": "t1", "lease_id": "l1"},
                {"ticket_id": "t2", "lease_id": "old"},
            ],
        )
    assert [row["renewed"] for row in result] == [True, False]
    updates = [call for call in conn.cur.calls if "UPDATE autonomy_tickets" in call[0]]
    assert updates[0][1] == (daemons.LEASE_SECONDS, "t1", "d1", "l1")
    assert updates[1][1] == (daemons.LEASE_SECONDS, "t2", "d1", "old")
    assert all("worker_lease_id = %s" in sql for sql, _ in updates)


@pytest.mark.asyncio
async def test_recovery_only_requeues_expired_or_legacy_daemon_leases():
    conn = _Conn([])
    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        return_value=conn,
    ):
        await daemons.recover_expired_daemon_leases()
    sql = conn.cur.calls[0][0]
    assert "worker_daemon_id IS NOT NULL" in sql
    assert "status='running'" in sql
    assert "worker_lease_expires_at <= clock_timestamp()" in sql
    assert "worker_lease_expires_at IS NULL" in sql
    assert "worker_lease_id=NULL" in sql


@pytest.mark.asyncio
async def test_report_replay_returns_receipt_without_touching_a_replacement_attempt():
    conn = _Conn([{"report_result": {"ticket_id": "t1", "state": "ready"}}])
    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        return_value=conn,
    ):
        result = await daemons.commit_daemon_report(
            "d1",
            "t1",
            "old-lease",
            state="ready",
            summary="duplicate",
            question=None,
            transition={"ticket_id": "t1", "state": "ready", "status": "completed"},
        )
    assert result == {"ticket_id": "t1", "state": "ready"}
    assert len(conn.cur.calls) == 1
    assert "FOR UPDATE" in conn.cur.calls[0][0]


@pytest.mark.asyncio
async def test_stale_report_lease_misses_guard_without_inserting_report_or_effect():
    conn = _Conn([{"report_result": None}, None])
    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        return_value=conn,
    ):
        result = await daemons.commit_daemon_report(
            "d1",
            "t1",
            "old-lease",
            state="ready",
            summary="late",
            question=None,
            transition={"ticket_id": "t1", "state": "ready", "status": "completed"},
        )
    assert result is None
    assert len(conn.cur.calls) == 2
    assert "worker_lease_id=%s" in conn.cur.calls[1][0]
    assert "worker_lease_expires_at>clock_timestamp()" in conn.cur.calls[1][0]


@pytest.mark.asyncio
async def test_report_transition_and_relay_effect_share_receipt_transaction():
    conn = _Conn(
        [
            {"report_result": None},
            {"user_id": "u1", "mandate": {}},
            {"report_id": "r-1"},
            ("effect-1",),
        ]
    )
    transition = {
        "ticket_id": "t1",
        "state": "ready",
        "status": "completed",
        "attention": None,
        "ended": True,
        "relay": True,
        "source_id": "",
    }
    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        return_value=conn,
    ):
        result = await daemons.commit_daemon_report(
            "d1",
            "t1",
            "lease-1",
            state="ready",
            summary="done",
            question=None,
            transition=transition,
        )
    assert result == {"ticket_id": "t1", "state": "ready"}
    assert conn.transactions == 1
    sql = [call[0] for call in conn.cur.calls]
    assert any("INSERT INTO autonomy_ticket_reports" in query for query in sql)
    assert any(
        "UPDATE autonomy_effects" not in query
        and "INSERT INTO autonomy_effects" in query
        for query in sql
    )
    assert any(
        "UPDATE autonomy_daemon_claims SET report_result" in query for query in sql
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "Sentinel token=ghp_" "123456789012345678901234567890\n普通正文",
            "Sentinel [REDACTED]\n普通正文",
        ),
        ("ordinary full report body", "ordinary full report body"),
    ],
)
async def test_daemon_report_persists_sanitized_full_body_without_changing_status(
    body, expected
):
    conn = _Conn(
        [
            {"report_result": None},
            {"user_id": "u1", "mandate": {}},
            {"report_id": "r-1"},
            ("effect-1",),
        ]
    )
    transition = {
        "ticket_id": "t1",
        "state": "ready",
        "status": "completed",
        "attention": None,
        "ended": True,
        "relay": True,
        "source_id": "",
    }
    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        return_value=conn,
    ):
        await daemons.commit_daemon_report(
            "d1",
            "t1",
            "lease-1",
            state="ready",
            summary="done",
            question=None,
            body=body,
            transition=transition,
        )

    insert = next(
        call for call in conn.cur.calls if "INSERT INTO autonomy_ticket_reports" in call[0]
    )
    assert insert[1][-1] == expected
    ticket_update = next(
        call for call in conn.cur.calls if "UPDATE autonomy_tickets SET status=%s" in call[0]
    )
    assert ticket_update[1][0] == "completed"


@pytest.mark.asyncio
async def test_daemon_report_caps_utf8_body_at_64k_with_truncation_marker():
    conn = _Conn(
        [
            {"report_result": None},
            {"user_id": "u1", "mandate": {}},
            {"report_id": "r-1"},
            ("effect-1",),
        ]
    )
    transition = {
        "ticket_id": "t1",
        "state": "ready",
        "status": "completed",
        "attention": None,
        "ended": True,
        "relay": True,
        "source_id": "",
    }
    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        return_value=conn,
    ):
        await daemons.commit_daemon_report(
            "d1",
            "t1",
            "lease-1",
            state="ready",
            summary="done",
            question=None,
            body="界" * (64 * 1024),
            transition=transition,
        )

    insert = next(
        call for call in conn.cur.calls if "INSERT INTO autonomy_ticket_reports" in call[0]
    )
    persisted_body = insert[1][-1]
    assert len(persisted_body.encode("utf-8")) <= 64 * 1024
    assert persisted_body.endswith("[… report truncated …]")
    ticket_update = next(
        call for call in conn.cur.calls if "UPDATE autonomy_tickets SET status=%s" in call[0]
    )
    assert ticket_update[1][0] == "completed"


@pytest.mark.asyncio
async def test_daemon_report_sanitizes_body_off_thread_before_opening_database():
    sanitizer_threads = []

    def sanitize(body):
        sanitizer_threads.append(threading.get_ident())
        return f"clean:{body}"

    class OrderedConn(_Conn):
        async def __aenter__(self):
            assert sanitizer_threads
            assert sanitizer_threads[0] != threading.get_ident()
            return await super().__aenter__()

    conn = OrderedConn(
        [
            {"report_result": None},
            {"user_id": "u1", "mandate": {}},
            {"report_id": "r-1"},
        ]
    )
    transition = {
        "ticket_id": "t1",
        "state": "ready",
        "status": "completed",
        "attention": None,
        "ended": True,
        "relay": False,
        "source_id": None,
    }
    with (
        patch("src.server.database.autonomy.daemons._report_body", side_effect=sanitize),
        patch(
            "src.server.database.autonomy.daemons.get_db_connection",
            return_value=conn,
        ),
    ):
        await daemons.commit_daemon_report(
            "d1",
            "t1",
            "lease-1",
            state="ready",
            summary="done",
            question=None,
            body="sensitive body",
            transition=transition,
        )

    insert = next(
        call for call in conn.cur.calls if "INSERT INTO autonomy_ticket_reports" in call[0]
    )
    assert insert[1][-1] == "clean:sensitive body"


@pytest.mark.asyncio
async def test_completed_report_ends_subscriptions_in_the_receipt_transaction():
    conn = _Conn(
        [
            {"report_result": None},
            {"user_id": "u1", "mandate": {}},
            {"report_id": "r-1"},
            ("effect-1",),
        ]
    )
    transition = {
        "ticket_id": "t1",
        "state": "ready",
        "status": "completed",
        "attention": None,
        "ended": True,
        "relay": True,
        "source_id": "",
    }
    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        return_value=conn,
    ):
        await daemons.commit_daemon_report(
            "d1",
            "t1",
            "lease-1",
            state="ready",
            summary="done",
            question=None,
            transition=transition,
        )

    subscription_ends = [
        call for call in conn.cur.calls if "UPDATE autonomy_subscriptions" in call[0]
    ]
    assert len(subscription_ends) == 1
    assert "ended_at IS NULL" in subscription_ends[0][0]
    assert subscription_ends[0][1] == ("ticket_ended", "t1")


@pytest.mark.asyncio
async def test_waiting_report_leaves_subscriptions_active():
    conn = _Conn(
        [
            {"report_result": None},
            {"user_id": "u1", "mandate": {}},
            {"report_id": "r-1"},
            ("effect-1",),
        ]
    )
    transition = {
        "ticket_id": "t1",
        "state": "waiting",
        "status": "waiting",
        "attention": None,
        "ended": False,
        "relay": True,
        "source_id": "",
    }
    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        return_value=conn,
    ):
        await daemons.commit_daemon_report(
            "d1",
            "t1",
            "lease-1",
            state="waiting",
            summary="paused for review",
            question=None,
            transition=transition,
        )

    assert not any("UPDATE autonomy_subscriptions" in call[0] for call in conn.cur.calls)


@pytest.mark.asyncio
async def test_main_agent_child_report_is_receipted_without_router_relay():
    conn = _Conn(
        [
            {"report_result": None},
            {
                "user_id": "u1",
                "mandate": {
                    "kind": "main_agent_child",
                    "orchestration_child": True,
                    "main_agent_run_id": "run-1",
                    "task_id": "task-1",
                },
            },
            {"report_id": "r-1"},
            ("advance-effect-1",),
        ]
    )
    transition = {
        "ticket_id": "ticket-1",
        "state": "ready",
        "status": "completed",
        "attention": None,
        "ended": True,
        "relay": True,
        "source_id": "",
    }
    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        return_value=conn,
    ):
        result = await daemons.commit_daemon_report(
            "d1",
            "ticket-1",
            "lease-1",
            state="ready",
            summary="task complete",
            question=None,
            transition=transition,
        )

    assert result["main_agent_child"] == {
        "run_id": "run-1",
        "user_id": "u1",
        "task_id": "task-1",
    }
    sql = [call[0] for call in conn.cur.calls]
    advance_insert = next(
        call for call in conn.cur.calls if "INSERT INTO autonomy_effects" in call[0]
    )
    assert advance_insert[1][1] == "main_agent_advance"
    assert advance_insert[1][2] == "main-agent:daemon-report:lease-1"
    assert advance_insert[1][3].obj == {
        "run_id": "run-1",
        "user_id": "u1",
    }
    assert not any("relay_report" in query for query in sql)
    assert conn.transactions == 1
    assert any("UPDATE autonomy_daemon_claims SET report_result" in query for query in sql)


@pytest.mark.asyncio
async def test_speculative_daemon_report_stores_discard_evidence_with_report_id():
    conn = _Conn(
        [
            {"report_result": None},
            {"user_id": "u1", "mandate": {"speculative": True, "source_id": "s-1"}},
            {"report_id": "report-9"},
        ]
    )
    transition = {
        "ticket_id": "t1",
        "state": "dropped",
        "speculative": True,
        "status": "completed",
        "summary": "",
        "attention": None,
        "ended": True,
        "relay": False,
        "useful": False,
        "source_id": "",
    }
    mandate_patch = {
        "speculative_outcome": {
            "outcome": "discarded",
            "recorded_at": "2026-11-03T00:00:00+00:00",
            "evidence": {"worker_state": "dropped", "worker_summary": "No confirmed issue"},
        }
    }
    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        return_value=conn,
    ):
        result = await daemons.commit_daemon_report(
            "d1",
            "t1",
            "lease-1",
            state="dropped",
            summary="No confirmed issue",
            question=None,
            transition=transition,
            mandate_patch=mandate_patch,
        )
    assert result["speculative_outcome"] == "discarded"
    assert result["useful"] is False
    update = next(call for call in conn.cur.calls if "UPDATE autonomy_tickets" in call[0])
    stored = update[1][3].obj["speculative_outcome"]
    assert stored["outcome"] == "discarded"
    assert stored["evidence"]["report_id"] == "report-9"
    assert stored["evidence"]["worker_summary"] == "No confirmed issue"
    assert not any("INSERT INTO autonomy_effects" in call[0] for call in conn.cur.calls)


@pytest.mark.asyncio
async def test_daemon_claim_allows_next_ticket_below_its_capacity():
    from datetime import datetime

    ticket = {
        "ticket_id": "next",
        "worker_daemon_id": "d1",
        "worker_lease_expires_at": datetime.now(UTC),
    }
    conn = _Conn([
        {"daemon_id": "d1", "user_id": "u1", "provider": "codex", "max_concurrency": 3},
        {"running_count": 2},
        None,
        None,
        ticket,
    ])
    with patch("src.server.database.autonomy.daemons.get_db_connection", return_value=conn):
        claimed = await daemons.claim_ticket("d1")
    assert claimed is not None
    assert claimed["ticket_id"] == "next"
    assert "FOR UPDATE" in conn.cur.calls[0][0]
    assert "COUNT(*)" in conn.cur.calls[1][0]


@pytest.mark.asyncio
async def test_simultaneous_daemon_claims_respect_locked_capacity_and_refill():
    daemon = {"daemon_id": "d1", "user_id": "u1", "provider": "codex", "max_concurrency": 2}
    other_daemon = {"daemon_id": "d2", "user_id": "u1", "provider": "codex", "max_concurrency": 2}
    tickets = [
        {
            "ticket_id": f"{daemon_id}-t{i}", "worker_daemon_id": daemon_id,
            "user_id": "u1", "worker_provider": "codex", "status": "queued",
            "attempts": 0,
        }
        for daemon_id in ("d1", "d2")
        for i in range(1, 5)
    ]
    state = _ClaimState({"d1": daemon, "d2": other_daemon}, tickets)

    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        side_effect=lambda: _ClaimConn(state),
    ):
        same_daemon = await asyncio.gather(*(daemons.claim_ticket("d1") for _ in range(5)))
        claimed = [row["ticket_id"] for row in same_daemon if row]
        assert len(claimed) == 2
        assert len(set(claimed)) == 2
        assert sum(t["status"] == "running" and t["worker_daemon_id"] == "d1" for t in tickets) == 2

        # Clear d1's slots, then prove independent daemon rows can be claimed
        # concurrently and both remain running.
        for ticket in tickets:
            if ticket["worker_daemon_id"] == "d1" and ticket["status"] == "running":
                ticket["status"] = "completed"
        across_daemons = await asyncio.gather(daemons.claim_ticket("d1"), daemons.claim_ticket("d2"))
        assert {row["worker_daemon_id"] for row in across_daemons if row} == {"d1", "d2"}
        assert sum(t["status"] == "running" for t in tickets) == 2

        completed = next(t for t in tickets if t["ticket_id"] == across_daemons[0]["ticket_id"])
        completed["status"] = "completed"
        refill = await daemons.claim_ticket("d1")
        assert refill is not None
        assert refill["ticket_id"] not in claimed


@pytest.mark.asyncio
async def test_daemon_deletion_refuses_retryable_or_configured_work():
    conn = _Conn([("daemon",), (True,)])
    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        return_value=conn,
    ):
        result = await daemons.delete_daemon("u1", "d1")
    assert result == "in_use"
    assert "FOR UPDATE" in conn.cur.calls[0][0]
    guard_sql = conn.cur.calls[1][0]
    assert "status NOT IN ('completed', 'cancelled', 'archived')" in guard_sql
    assert "autonomy_schedules" in guard_sql
    assert "autonomy_sources" in guard_sql
    assert "autonomy_stop_requests" in guard_sql
    assert conn.cur.calls[1][1] == ("d1", "d1", "d1", "d1")


@pytest.mark.asyncio
async def test_schedule_insert_persists_its_daemon_assignment():
    from datetime import datetime

    schedule_id = "11111111-1111-4111-8111-111111111111"
    row = {"schedule_id": schedule_id, "worker_daemon_id": "daemon1"}

    class Cursor(_Cursor):
        async def fetchone(self):
            return row

    class Conn(_Conn):
        def __init__(self):
            self.cur = Cursor([])

    conn = Conn()
    with patch(
        "src.server.database.autonomy.schedules.get_db_connection",
        return_value=conn,
    ):
        result = await schedules.create_schedule(
            user_id="u1",
            workspace_id="w1",
            title="Daily",
            prompt="Review",
            cron_expression="0 9 * * *",
            timezone="UTC",
            next_run_at=datetime.now(UTC),
            worker_provider="codex",
            worker_daemon_id="daemon1",
        )
    sql, params = conn.cur.calls[0]
    assert "worker_daemon_id" in sql
    assert params[9] == "daemon1"
    assert result["worker_daemon_id"] == "daemon1"


@pytest.mark.asyncio
async def test_commit_daemon_report_rechecks_revocation_under_the_claim_lock():
    """The owner can revoke after the endpoint precheck; the commit must notice."""
    grant = {
        "daemon_id": "daemon-1",
        "policy_version": "abc123",
        "nonce": "nonce-value-is-long-enough",
    }
    claim_row = {
        "report_result": None,
        "daemon_id": "daemon-1",
        "policy_version": "abc123",
        "lease_grant": grant,
        "revoked_at": "2026-01-01T00:00:00Z",
    }
    conn = _Conn([claim_row])
    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        return_value=conn,
    ):
        result = await daemons.commit_daemon_report(
            "daemon-1",
            "ticket-1",
            "lease-1",
            state="ready",
            summary="done",
            question=None,
            transition={"status": "completed", "attention": None, "ended": True},
            reported_daemon_id="daemon-1",
            reported_policy_version="abc123",
        )

    assert result == {"refused": "Lease is revoked"}
    assert all("INSERT" not in str(call[0]) for call in conn.cur.calls)


@pytest.mark.asyncio
async def test_commit_daemon_report_still_accepts_a_matching_unrevoked_binding():
    grant = {
        "daemon_id": "daemon-1",
        "policy_version": "abc123",
        "nonce": "nonce-value-is-long-enough",
    }
    claim_row = {
        "report_result": None,
        "daemon_id": "daemon-1",
        "policy_version": "abc123",
        "lease_grant": grant,
        "revoked_at": None,
    }
    ticket_row = {"user_id": "user-1", "mandate": {}}
    report_row = {"report_id": "report-1"}
    conn = _Conn([claim_row, ticket_row, report_row])
    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        return_value=conn,
    ):
        result = await daemons.commit_daemon_report(
            "daemon-1",
            "ticket-1",
            "lease-1",
            state="ready",
            summary="done",
            question=None,
            transition={
                "status": "completed",
                "attention": None,
                "ended": True,
                "relay": False,
                "source_id": None,
            },
            reported_daemon_id="daemon-1",
            reported_policy_version="abc123",
        )

    assert result is not None and "refused" not in result


@pytest.mark.asyncio
async def test_live_lease_ticket_fences_revoked_claims():
    """A revoked claim must not authorize live output: the lease predicate
    joins the claim row and requires it unrevoked, not just a running ticket."""
    conn = _Conn([{"?column?": 1}])
    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        return_value=conn,
    ):
        assert await daemons.live_lease_ticket("daemon-1", "ticket-1", "lease-1") is True

    sql, params = conn.cur.calls[0]
    assert "JOIN autonomy_daemon_claims" in sql
    assert "c.revoked_at IS NULL" in sql
    assert params == ("ticket-1", "daemon-1", "daemon-1", "lease-1", "daemon-1")
