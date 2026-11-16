"""Expired Router Evals release their Thread and requeue their Triggers."""

from unittest.mock import AsyncMock, patch

import pytest

from src.server.database.autonomy import evals as eval_db


class _Cursor:
    def __init__(self):
        self.sql = ""
        self.fetchone = AsyncMock(return_value=(3,))

    async def execute(self, sql, params=None):
        self.sql = sql

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self):
        self.cur = _Cursor()

    def cursor(self, row_factory=None):
        return self.cur

    def transaction(self):
        return _Tx()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ExpiryCursor:
    def __init__(self):
        self.sql: list[str] = []
        self.params: list = []
        self.rows = [
            {"eval_id": "e1", "thread_key": "t1", "trigger_ids": ["trg1"]}
        ]

    async def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))
        self.params.append(params)

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return {"eval_expiries": 3}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ExpiryConn:
    def __init__(self):
        self.cur = _ExpiryCursor()

    def cursor(self, row_factory=None):
        return self.cur

    def transaction(self):
        return _Tx()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_expiry_retries_twice_then_leaves_the_batch_unavailable():
    conn = _ExpiryConn()
    with patch(
        "src.server.database.autonomy.evals.get_db_connection", return_value=conn
    ):
        expired_ids = await eval_db.expire_stale_evals()
    assert expired_ids == ["e1"]
    sql = " ".join(conn.cur.sql)
    assert "SET status = 'unavailable'" in sql
    assert "eval_expiries + 1 < 3" in sql
    assert "ELSE pending_trigger_ids" in sql
    assert "UPDATE autonomy_evals SET reason" in sql
    assert ("router unavailable after 3 attempts", "e1") in conn.cur.params
    assert "evaluating_eval_id = NULL" in sql


def _thread_update(statements: list[str]) -> str:
    matches = [statement for statement in statements if "UPDATE autonomy_threads" in statement]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.asyncio
async def test_null_deadline_older_than_five_minutes_is_selected():
    conn = _ExpiryConn()
    with patch(
        "src.server.database.autonomy.evals.get_db_connection", return_value=conn
    ):
        await eval_db.expire_stale_evals()
    sql = " ".join(conn.cur.sql)
    assert "status IN ('queued', 'delivered')" in sql
    assert "deadline_at IS NOT NULL AND deadline_at < NOW()" in sql
    assert (
        "deadline_at IS NULL AND started_at < NOW() - INTERVAL '5 minutes'" in sql
    )


@pytest.mark.asyncio
async def test_thread_pointer_cleared_when_pointed_eval_is_no_longer_active():
    conn = _ExpiryConn()
    with patch(
        "src.server.database.autonomy.evals.get_db_connection", return_value=conn
    ):
        await eval_db.expire_stale_evals()
    release = _thread_update(conn.cur.sql)
    assert "thread_key = %s" in release
    assert "evaluating_eval_id = NULL" in release
    assert "evaluating_eval_id = %s" in release
    assert "evaluating_eval_id IS NULL" in release
    assert "evaluating_eval_id IS NOT NULL" in release
    assert "NOT EXISTS" in release
    assert "pointed.status IN ('queued', 'delivered')" in release
    assert "eval_expiries + 1 < 3" in release
    assert "ELSE pending_trigger_ids" in release


@pytest.mark.asyncio
async def test_expire_one_uses_the_same_release_policy_and_is_idempotent():
    class Cursor(_ExpiryCursor):
        def __init__(self, first):
            super().__init__()
            self._fetchone = iter(first)

        async def fetchone(self):
            return next(self._fetchone)

        async def fetchall(self):
            return []

    class Conn(_ExpiryConn):
        def __init__(self, first):
            super().__init__()
            self.cur = Cursor(first)

    stale = _ExpiryConn()
    one = Conn(
        [
            {"eval_id": "e1", "thread_key": "t1", "trigger_ids": ["trg1"]},
            {"eval_expiries": 3},
        ]
    )
    gone = Conn([None])
    with patch(
        "src.server.database.autonomy.evals.get_db_connection", return_value=stale
    ):
        await eval_db.expire_stale_evals()
    with patch(
        "src.server.database.autonomy.evals.get_db_connection", return_value=one
    ):
        expired = await eval_db.expire_one("e1")
    with patch(
        "src.server.database.autonomy.evals.get_db_connection", return_value=gone
    ):
        again = await eval_db.expire_one("e1")

    assert expired is True
    assert again is False
    opening = one.cur.sql[0]
    assert "WHERE eval_id = %s" in opening
    assert "status IN ('queued', 'delivered')" in opening
    assert "deadline_at < NOW()" not in opening
    assert "SET status = 'unavailable'" in opening
    assert _thread_update(one.cur.sql) == _thread_update(stale.cur.sql)
    assert "eval_expiries + 1 < 3" in _thread_update(one.cur.sql)
    assert ("router unavailable after 3 attempts", "e1") in one.cur.params
    assert gone.cur.sql
    assert all("UPDATE autonomy_threads" not in statement for statement in gone.cur.sql)


@pytest.mark.asyncio
async def test_policy_change_discards_and_requeues_active_evals():
    from src.server.database.autonomy.evals import discard_evals_for_user

    conn = _Conn()
    with patch(
        "src.server.database.autonomy.evals.get_db_connection", return_value=conn
    ):
        count = await discard_evals_for_user("u1", reason="policy changed")
    assert count == 3
    sql = " ".join(conn.cur.sql.split())
    assert "status = 'unavailable'" in sql
    assert "evaluating_eval_id = NULL" in sql
    assert "pending_trigger_ids" in sql


@pytest.mark.asyncio
async def test_queued_eval_recovery_query_detects_missing_router_effect_references():
    class Cursor:
        def __init__(self):
            self.sql = ""

        async def execute(self, sql, params=None):
            self.sql = " ".join(sql.split())

        async def fetchall(self):
            return [{"eval_id": "eval-1", "user_id": "user-1"}]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Conn:
        def __init__(self):
            self.cur = Cursor()

        def cursor(self, row_factory=None):
            return self.cur

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    conn = Conn()
    with patch(
        "src.server.database.autonomy.evals.get_db_connection", return_value=conn
    ):
        rows = await eval_db.list_queued_evals_without_effect(limit=7)
    assert rows == [{"eval_id": "eval-1", "user_id": "user-1"}]
    assert "e.status = 'queued'" in conn.cur.sql
    assert "x.payload -> 'eval_ids'" in conn.cur.sql
    assert "x.payload ->> 'eval_id'" in conn.cur.sql


@pytest.mark.asyncio
async def test_accept_inserts_the_daemon_assignment_with_the_ticket():
    class Cursor(_Cursor):
        def __init__(self):
            super().__init__()
            del self.fetchone
            self.calls = []
            self.rows = iter(
                [
                    {
                        "eval_id": "e1",
                        "thread_key": "t1",
                        "trigger_ids": ["trg1"],
                    },
                    {"scope_id": "scope1"},
                    {"ticket_id": "ticket1"},
                ]
            )

        async def execute(self, sql, params=None):
            self.calls.append((" ".join(sql.split()), params))

        async def fetchone(self):
            return next(self.rows)

    class Conn(_Conn):
        def __init__(self):
            self.cur = Cursor()

    conn = Conn()
    with patch(
        "src.server.database.autonomy.evals.get_db_connection", return_value=conn
    ):
        result = await eval_db.decide_eval(
            eval_id="e1",
            decision="accept",
            reason="approved",
            ticket={
                "user_id": "u1",
                "workspace_id": "w1",
                "origin_trigger_id": "trg1",
                "origin_thread_key": "t1",
                "worker_daemon_id": "daemon1",
            },
        )
    assert result["ticket_id"] == "ticket1"
    insert_sql, insert_params = conn.cur.calls[3]
    assert "worker_daemon_id" in insert_sql
    assert "daemon1" in insert_params


@pytest.mark.asyncio
async def test_router_batch_lookup_reads_member_ids_from_persisted_jsonb_payload():
    class Cursor:
        def __init__(self):
            self.sql = ""
            self.params = None

        async def execute(self, sql, params=None):
            self.sql = " ".join(sql.split())
            self.params = params

        async def fetchall(self):
            return [
                {
                    "idempotency_key": "autonomy:router-batch:abc",
                    "eval_ids": ["eval-a", "eval-b"],
                }
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Conn:
        def __init__(self):
            self.cur = Cursor()

        def cursor(self, row_factory=None):
            return self.cur

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    conn = Conn()
    with patch(
        "src.server.database.autonomy.evals.get_db_connection", return_value=conn
    ):
        rows = await eval_db.router_effect_batches(["eval-a", "eval-b"])

    assert rows == [
        {"idempotency_key": "autonomy:router-batch:abc", "eval_ids": ["eval-a", "eval-b"]}
    ]
    assert "jsonb_array_elements_text" in conn.cur.sql
    assert "ANY(%s::text[])" in conn.cur.sql
    assert conn.cur.params == (["eval-a", "eval-b"],)
