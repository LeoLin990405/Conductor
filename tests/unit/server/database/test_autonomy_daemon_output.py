"""SQL contract for daemon output sessions: one transaction, row lock, order."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.server.database.autonomy import daemon_output as output_db


class _Tx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.in_tx = True
        return self

    async def __aexit__(self, exc_type, *rest):
        self.conn.in_tx = False
        self.conn.rolled_back = exc_type is not None
        return False


class _Cursor:
    def __init__(self, conn, rows):
        self.conn = conn
        self.rows = list(rows)
        self.calls = []

    async def execute(self, sql, params=None):
        assert self.conn.in_tx, "every statement must run inside the transaction"
        self.calls.append((" ".join(sql.split()), params))
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise RuntimeError("injected")

    async def executemany(self, sql, seq):
        assert self.conn.in_tx
        self.calls.append((" ".join(sql.split()), list(seq)))
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise RuntimeError("injected")

    async def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    async def fetchall(self):
        return list(self.rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, rows, fail_on=None):
        self.in_tx = False
        self.rolled_back = False
        self.fail_on = fail_on
        self.cur = _Cursor(self, rows)

    def cursor(self, row_factory=None):
        return self.cur

    def transaction(self):
        return _Tx(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _session_row():
    return {
        "ticket_id": "t",
        "epoch": 1,
        "lease_id": None,
        "run_id": "",
        "next_seq": 0,
        "last_batch": 0,
        "tainted": False,
        "taint_reason": "",
        "guard_tail": "",
        "guard_state": {},
    }


def _plan(*, write=True, purge=False, trim=0, rows=()):
    return SimpleNamespace(
        write=write,
        purge_older_epochs=purge,
        trim_through=trim,
        rows=list(rows),
        session={
            "epoch": 2,
            "lease_id": "lease",
            "run_id": "run",
            "next_seq": 3,
            "last_batch": 1,
            "tainted": False,
            "taint_reason": "",
            "guard_tail": "hello ",
            "guard_state": {"v": 1},
        },
    )


@pytest.mark.asyncio
async def test_commit_batch_runs_lease_lock_decide_and_writes_in_one_transaction():
    conn = _Conn([(1,), (1,), _session_row(), None])
    decided = []

    def decide(row, epoch):
        decided.append(row)
        return _plan(rows=[(1, {"kind": "text", "text": "a\n"}), (2, {"kind": "tool"})], trim=1)

    with patch.object(output_db, "get_db_connection", return_value=conn):
        live, plan = await output_db.commit_batch(
            daemon_id="d", ticket_id="t", lease_id="l", allocate=False, decide=decide
        )

    assert live is True and plan.write is True
    assert decided and decided[0]["epoch"] == 1
    statements = [sql for sql, _ in conn.cur.calls]
    order = [
        "SET LOCAL lock_timeout",
        "SET LOCAL statement_timeout",
        "FROM autonomy_daemon_claims c",
        "FROM autonomy_tickets t",
        "INSERT INTO autonomy_daemon_output_sessions",
        "FOR UPDATE",
        "UPDATE autonomy_daemon_output_sessions SET",
        "INSERT INTO autonomy_daemon_output_events",
        "DELETE FROM autonomy_daemon_output_events WHERE ticket_id=%s AND epoch=%s AND seq<=%s",
    ]
    positions = [
        next(i for i, sql in enumerate(statements) if marker in sql) for marker in order
    ]
    assert positions == sorted(positions)
    assert conn.rolled_back is False


@pytest.mark.asyncio
async def test_commit_batch_stops_at_a_stale_lease_before_touching_the_session():
    for rows in ([None], [(1,), None]):  # revoked claim; ticket no longer leased
        conn = _Conn(rows)
        with patch.object(output_db, "get_db_connection", return_value=conn):
            live, plan = await output_db.commit_batch(
                daemon_id="d",
                ticket_id="t",
                lease_id="l",
                allocate=False,
                decide=lambda row, epoch: 1 / 0,
            )
        assert (live, plan) == (False, None)
        assert not any("output_sessions" in sql for sql, _ in conn.cur.calls)


@pytest.mark.asyncio
async def test_commit_batch_without_a_write_skips_every_mutation_after_the_lock():
    conn = _Conn([(1,), (1,), _session_row()])
    with patch.object(output_db, "get_db_connection", return_value=conn):
        live, plan = await output_db.commit_batch(
            daemon_id="d",
            ticket_id="t",
            lease_id="l",
            allocate=False,
            decide=lambda row, epoch: _plan(write=False),
        )
    assert live is True and plan.write is False
    assert not any(sql.startswith(("UPDATE", "DELETE")) for sql, _ in conn.cur.calls)
    assert not any("INSERT INTO autonomy_daemon_output_events" in s for s, _ in conn.cur.calls)


@pytest.mark.asyncio
async def test_commit_batch_purges_older_epochs_when_an_epoch_is_allocated():
    conn = _Conn([(1,), (1,), _session_row(), {"nextval": 9}])
    seen = []
    with patch.object(output_db, "get_db_connection", return_value=conn):
        await output_db.commit_batch(
            daemon_id="d",
            ticket_id="t",
            lease_id="l",
            allocate=True,
            decide=lambda row, epoch: seen.append(epoch) or _plan(purge=True),
        )
    assert seen == [9]  # the epoch comes from the database sequence
    assert any("nextval('autonomy_daemon_output_epoch_seq')" in s for s, _ in conn.cur.calls)
    deletes = [(sql, p) for sql, p in conn.cur.calls if sql.startswith("DELETE")]
    assert any("epoch<%s" in sql and p == ("t", 2) for sql, p in deletes)


@pytest.mark.asyncio
async def test_the_write_path_never_collects_expired_logs():
    # A backlog must not slow or fail a run's first batch: collection belongs to
    # the scheduler pass, so neither an allocation nor a plain append runs it.
    for allocate, extra in ((True, [{"nextval": 3}]), (False, [])):
        conn = _Conn([(1,), (1,), _session_row(), *extra])
        with patch.object(output_db, "get_db_connection", return_value=conn):
            await output_db.commit_batch(
                daemon_id="d",
                ticket_id="t",
                lease_id="l",
                allocate=allocate,
                decide=lambda row, epoch: _plan(purge=allocate),
            )
        statements = [sql for sql, _ in conn.cur.calls]
        assert not any("autonomy_daemon_output_gc" in sql for sql in statements)
        assert not any(
            sql.startswith("WITH RECURSIVE logs") for sql in statements
        ), allocate


@pytest.mark.asyncio
async def test_expiry_takes_the_transaction_lock_then_collects_one_batch():
    conn = _Conn([(True,), (7,)])
    with patch.object(output_db, "get_db_connection", return_value=conn):
        collected = await output_db.expire_old_logs(25)
    assert collected == 7
    statements = [(sql, params) for sql, params in conn.cur.calls]
    assert statements[0][0].startswith("SET LOCAL lock_timeout")
    assert statements[1][0].startswith("SET LOCAL statement_timeout")
    lock_sql, lock_params = statements[2]
    assert "pg_try_advisory_xact_lock" in lock_sql
    assert lock_params == ("autonomy_daemon_output_gc",)
    collect_sql, collect_params = statements[3]
    assert collect_sql == " ".join(output_db.EXPIRE_SQL.split())
    assert collect_params == (25,)
    assert conn.rolled_back is False


@pytest.mark.asyncio
async def test_expiry_yields_to_a_collector_that_holds_the_lock():
    conn = _Conn([(False,)])
    with patch.object(output_db, "get_db_connection", return_value=conn):
        assert await output_db.expire_old_logs() == 0
    assert not any("RECURSIVE" in sql for sql, _ in conn.cur.calls)


@pytest.mark.asyncio
async def test_commit_batch_failure_propagates_so_the_transaction_rolls_back():
    conn = _Conn([(1,), (1,), _session_row()], fail_on="INSERT INTO autonomy_daemon_output_events")
    with patch.object(output_db, "get_db_connection", return_value=conn):
        with pytest.raises(RuntimeError):
            await output_db.commit_batch(
                daemon_id="d",
                ticket_id="t",
                lease_id="l",
                allocate=False,
                decide=lambda row, epoch: _plan(rows=[(1, {"kind": "text"})]),
            )
    assert conn.rolled_back is True


@pytest.mark.asyncio
async def test_a_decide_error_rolls_back_before_any_write():
    conn = _Conn([(1,), (1,), _session_row()])

    def decide(row, epoch):
        raise ValueError("bad plan")

    with patch.object(output_db, "get_db_connection", return_value=conn):
        with pytest.raises(ValueError):
            await output_db.commit_batch(
                daemon_id="d", ticket_id="t", lease_id="l", allocate=False, decide=decide
            )
    assert conn.rolled_back is True
    assert not any(sql.startswith("UPDATE") for sql, _ in conn.cur.calls)


@pytest.mark.asyncio
async def test_reads_are_keyed_by_epoch_and_ordered():
    conn = _Conn([{"seq": 4, "data": {"kind": "text"}}])
    conn.in_tx = True
    with patch.object(output_db, "get_db_connection", return_value=conn):
        rows = await output_db.read_events("t", 3, 2, 10)
    assert rows == [(4, {"kind": "text"})]
    sql, params = conn.cur.calls[0]
    assert "epoch=%s" in sql and "seq>%s" in sql and "ORDER BY seq" in sql
    assert params == ("t", 3, 2, 10)


@pytest.mark.asyncio
async def test_the_watcher_session_read_leaves_the_guard_state_out():
    conn = _Conn([{"ticket_id": "t", "epoch": 3, "lease_id": "l", "next_seq": 4}])
    conn.in_tx = True
    with patch.object(output_db, "get_db_connection", return_value=conn):
        row = await output_db.read_session("t")
    assert row["next_seq"] == 4
    sql, _ = conn.cur.calls[0]
    assert "epoch" in sql and "lease_id" in sql and "next_seq" in sql
    assert "guard_tail" not in sql and "guard_state" not in sql


def test_expiry_removes_whole_logs_never_single_rows():
    sql = " ".join(output_db.EXPIRE_SQL.split())
    assert "ctid" not in sql
    # Whole (ticket, epoch) logs, judged by the newest row of each: the filter
    # runs before the LIMIT so live logs cannot crowd out expired ones.
    assert "DELETE FROM autonomy_daemon_output_events e USING due WHERE" in sql
    assert "e.ticket_id=due.ticket_id AND e.epoch=due.epoch" in sql
    assert "ORDER BY e.seq DESC LIMIT 1) < NOW() - INTERVAL '1 day' LIMIT %s" in sql
    # An idle pass is one probe of the created_at index.
    assert "WHERE EXISTS (SELECT 1 FROM autonomy_daemon_output_events WHERE created_at <" in sql


def test_the_lease_fence_locks_the_claim_exclusively_before_the_ticket():
    import inspect

    claim = output_db.CLAIM_LOCK_SQL
    ticket = output_db.TICKET_LOCK_SQL
    # A share lock on the claim lets overlapping appends starve a revoke; a key
    # share lock never makes the revoke wait, which breaks the fence.
    assert "FOR NO KEY UPDATE OF c" in claim and "revoked_at IS NULL" in claim
    assert "FOR SHARE" not in claim and "KEY SHARE" not in claim
    assert "FOR SHARE OF t" in ticket and "worker_lease_expires_at>clock_timestamp()" in ticket
    source = inspect.getsource(output_db.commit_batch)
    assert source.index("CLAIM_LOCK_SQL") < source.index("TICKET_LOCK_SQL")


def test_migration_140_chains_after_139_and_defines_the_fenced_tables():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[4]
        / "migrations/versions/140_daemon_output_sessions.py"
    ).read_text()
    assert 'revision = "140"' in source and 'down_revision = "139"' in source
    for fragment in (
        "autonomy_daemon_output_sessions",
        "autonomy_daemon_output_events",
        "PRIMARY KEY (ticket_id, epoch, seq)",
        "tainted BOOLEAN NOT NULL DEFAULT FALSE",
        "guard_tail TEXT",
        "guard_state JSONB",
        "ON DELETE CASCADE",
        "CREATE SEQUENCE IF NOT EXISTS autonomy_daemon_output_epoch_seq",
        "DROP SEQUENCE IF EXISTS autonomy_daemon_output_epoch_seq",
        "DROP TABLE IF EXISTS autonomy_daemon_output_events",
    ):
        assert fragment in source, fragment
