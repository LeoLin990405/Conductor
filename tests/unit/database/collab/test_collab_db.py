"""Collab persistence: digest-only tokens, CAS leases and audited calls."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.collab.types import (
    AlreadyClaimedError,
    ChannelFullError,
    InvalidSchemaError,
    ScopeDeniedError,
    SelfReviewError,
)
from src.server.database.collab import (
    apps as app_db,
    audit as audit_db,
    collab_transaction,
    handoffs as handoff_db,
    leases as lease_db,
    messages as message_db,
    reviews as review_db,
)

_APPS = "src.server.database.collab.apps"
_AUDIT = "src.server.database.collab.audit"
_LEASES = "src.server.database.collab.leases"
_HANDOFFS = "src.server.database.collab.handoffs"
_MESSAGES = "src.server.database.collab.messages"
_REVIEWS = "src.server.database.collab.reviews"
_TRANSACTIONS = "src.server.database.collab.transactions"

_DIGEST = "a" * 64
_TICKET = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_TARGET = f"t:{_TICKET}"
_LEASE = "11111111-1111-1111-1111-111111111111"
_RECOVERABLE = "t.status IN ('running', 'waiting', 'blocked')"
_DEDUP_ENV = "CONDUCTOR_DISPATCH_DEDUP_MODE"


class _Tx:
    def __init__(self):
        self.exceptions = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exceptions.append(exc)
        return False


class _Cursor:
    def __init__(self, *, one=None, all_rows=None, rowcount=1, raise_on=None):
        self.one = list(one or [])
        self.all_rows = list(all_rows or [])
        self.rowcount = rowcount
        self.raise_on = raise_on
        self.calls: list[tuple[str, tuple | None]] = []

    async def execute(self, sql, params=None):
        self.calls.append((" ".join(str(sql).split()), params))
        if self.raise_on and self.raise_on in str(sql):
            raise RuntimeError("audit insert failed")

    async def fetchone(self):
        return self.one.pop(0) if self.one else None

    async def fetchall(self):
        return list(self.all_rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, cursor):
        self.cur = cursor
        self.transactions = 0
        self.tx = _Tx()

    def cursor(self, row_factory=None):
        return self.cur

    def transaction(self):
        self.transactions += 1
        return self.tx

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _app_row(**overrides):
    row = {
        "app_id": "claude-code",
        "user_id": "u1",
        "name": "Claude Code",
        "token_sha256": _DIGEST,
        "scopes": ["read", "claim"],
        "can_write": True,
        "max_leases": 1,
        "enabled": True,
        "revoked_at": None,
        "last_used_at": None,
        "created_at": None,
        "updated_at": None,
    }
    row.update(overrides)
    return row


def _lease_row(**overrides):
    row = {
        "lease_id": _LEASE,
        "app_id": "claude-code",
        "agent": "claude-opus",
        "target_ref": _TARGET,
        "target_kind": "ticket",
        "ticket_id": _TICKET,
        "line_id": "",
        "revision": 2,
        "state": "active",
        "worktree_path": "/tmp/wt",
        "worktree_branch": "conductor/ticket/x",
        "worktree_base_sha": "b" * 40,
        "heartbeat_at": None,
        "expires_at": None,
        "max_lease_until": None,
        "created_at": None,
        "ended_at": None,
        "end_reason": "",
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_app_token_lookup_uses_only_the_sha256_digest():
    cur = _Cursor(one=[_app_row()])
    with patch(f"{_APPS}.get_db_connection", return_value=_Conn(cur)):
        row = await app_db.app_for_token_hash(_DIGEST)
    assert row["app_id"] == "claude-code"
    assert row["scopes"] == ["read", "claim"]
    sql, params = cur.calls[0]
    assert "token_sha256 = %s" in sql
    assert "revoked_at IS NULL" in sql
    assert "enabled = TRUE" in sql
    assert params == (_DIGEST,)


@pytest.mark.asyncio
async def test_create_app_stores_the_digest_and_never_the_plaintext():
    token = "a-plaintext-token-that-must-not-be-stored"
    digest = app_db.hash_token(token)
    cur = _Cursor(one=[_app_row(token_sha256=digest, scopes=["read"])])
    with patch(f"{_APPS}.get_db_connection", return_value=_Conn(cur)):
        row = await app_db.create_app(
            user_id="u1",
            app_id="claude-code",
            token_sha256=digest,
        )
    assert row["scopes"] == ["read"]
    sql, params = cur.calls[0]
    assert params[3] == digest
    assert token not in str(params)
    assert "token_sha256" in sql


def test_create_app_refuses_a_plaintext_token_digest():
    with pytest.raises(ValueError):
        app_db.validate_token_sha256("not-a-digest")


@pytest.mark.asyncio
async def test_update_scopes_and_revoke_keep_the_app_digest_untouched():
    cur = _Cursor(one=[_app_row(scopes=["read", "report"]), _app_row()])
    conn = _Conn(cur)
    with patch(f"{_APPS}.get_db_connection", return_value=conn):
        updated = await app_db.update_scopes("claude-code", ["read", "report"])
        revoked = await app_db.revoke_app("claude-code")
    assert updated["scopes"] == ["read", "report"]
    assert revoked["app_id"] == "claude-code"
    assert "SET scopes = %s" in cur.calls[0][0]
    assert "revoked_at = COALESCE" in cur.calls[1][0]
    assert len(cur.calls) == 2


@pytest.mark.asyncio
async def test_audit_hashes_parameters_and_derives_the_status():
    cur = _Cursor(
        one=[{"audit_id": "a1", "app_id": "claude-code", "code": "rate_limited"}]
    )
    with patch(f"{_AUDIT}.get_db_connection", return_value=_Conn(cur)):
        row = await audit_db.record_audit(
            tool="task_claim",
            app_id="claude-code",
            target=_TARGET,
            params={"text": "ghp_" + "Z" * 30},
            code="rate_limited",
            rate_limited=True,
        )
    assert row["code"] == "rate_limited"
    _sql, params = cur.calls[0]
    assert params[4] and len(params[4]) == 64
    assert "ghp_" not in str(params)
    assert params[6] == 429
    assert params[7] is True


@pytest.mark.asyncio
async def test_audit_rejects_a_malformed_digest():
    with pytest.raises(ValueError):
        await audit_db.record_audit(tool="x", params_sha256="bad")


@pytest.mark.asyncio
async def test_insert_lease_uses_on_conflict_for_the_live_target():
    cur = _Cursor(one=[_lease_row()])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(cur)):
        row = await lease_db.insert_lease(
            app_id="claude-code",
            agent="claude-opus",
            target_ref=_TARGET,
            target_kind="ticket",
            revision=2,
            expires_at=None,
            max_lease_until=None,
            ticket_id=_TICKET,
        )
    assert row["lease_id"].startswith("1111")
    sql, params = cur.calls[0]
    assert "ON CONFLICT DO NOTHING" in sql
    assert params[0] == "claude-code"
    assert params[2] == _TARGET


@pytest.mark.asyncio
async def test_claim_ticket_lease_is_one_transaction_with_a_revision_cas():
    cur = _Cursor(
        one=[
            {"ticket_id": _TICKET, "revision": 3},
            _lease_row(revision=3),
        ]
    )
    conn = _Conn(cur)
    with patch(f"{_LEASES}.get_db_connection", return_value=conn):
        row = await lease_db.claim_ticket_lease(
            ticket_id=_TICKET,
            app_id="claude-code",
            agent="claude-opus",
            revision=2,
        )
    assert row["revision"] == 3
    assert conn.transactions == 1
    ticket_sql, ticket_params = cur.calls[0]
    assert "UPDATE autonomy_tickets" in ticket_sql
    assert "revision = %s" in ticket_sql and "status = 'queued'" in ticket_sql
    assert ticket_params == (_TICKET, 2)
    insert_sql, insert_params = cur.calls[1]
    assert "INSERT INTO collab_leases" in insert_sql
    assert insert_params[4] == 3


@pytest.mark.asyncio
async def test_claim_ticket_lease_returns_none_when_the_revision_is_stale():
    cur = _Cursor(one=[None])
    conn = _Conn(cur)
    with patch(f"{_LEASES}.get_db_connection", return_value=conn):
        assert (
            await lease_db.claim_ticket_lease(
                ticket_id=_TICKET, app_id="claude-code", revision=1
            )
            is None
        )
    assert len(cur.calls) == 1
    assert conn.transactions == 1


@pytest.mark.asyncio
async def test_claim_ticket_lease_rolls_back_when_the_live_insert_loses():
    cur = _Cursor(one=[{"ticket_id": _TICKET, "revision": 2}, None])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(cur)):
        with pytest.raises(AlreadyClaimedError):
            await lease_db.claim_ticket_lease(
                ticket_id=_TICKET, app_id="claude-code", revision=1
            )


@pytest.mark.asyncio
async def test_heartbeat_and_release_are_fenced_by_the_app():
    cur = _Cursor(one=[_lease_row(), _lease_row(state="released")])
    conn = _Conn(cur)
    with patch(f"{_LEASES}.get_db_connection", return_value=conn):
        beat = await lease_db.heartbeat_lease(_LEASE, app_id="claude-code")
        released = await lease_db.release_lease(
            _LEASE, app_id="claude-code", reason="done"
        )
    assert beat["state"] == "active"
    assert released["state"] == "released"
    heartbeat_sql, heartbeat_params = cur.calls[0]
    assert "LEAST" in heartbeat_sql and "app_id = %s" in heartbeat_sql
    # The persisted deadline is authority: a late heartbeat cannot move it.
    assert "expires_at > NOW()" in heartbeat_sql
    assert "max_lease_until > NOW()" in heartbeat_sql
    assert heartbeat_params[1:] == (_LEASE, "claude-code")
    release_sql, release_params = cur.calls[1]
    assert "state = %s" in release_sql and "end_reason = %s" in release_sql
    assert release_params[0] == "released"


@pytest.mark.asyncio
async def test_a_late_heartbeat_does_not_resurrect_an_expired_lease():
    cur = _Cursor(one=[None])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(cur)):
        assert await lease_db.heartbeat_lease(_LEASE, app_id="claude-code") is None
    sql, _params = cur.calls[0]
    assert "expires_at > NOW()" in sql


@pytest.mark.asyncio
async def test_release_and_revocation_requeue_the_ticket_in_the_same_statement():
    cur = _Cursor(one=[_lease_row(state="released")])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(cur)):
        released = await lease_db.release_lease(
            _LEASE, app_id="claude-code", reason="out_of_quota"
        )
    assert released["state"] == "released"
    sql, params = cur.calls[0]
    assert "UPDATE autonomy_tickets" in sql
    assert "status = 'queued'" in sql
    assert params == ("released", "out_of_quota", _LEASE, "claude-code")

    # A completed lease normally leaves a ``ready`` Ticket, which is outside
    # the recoverable states; the shared requeue still applies so a completed
    # lease whose Ticket never reached ``ready`` is not stranded.
    completed_cur = _Cursor(one=[_lease_row(state="completed")])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(completed_cur)):
        await lease_db.release_lease(
            _LEASE, app_id="claude-code", state="completed"
        )
    completed_sql, completed_params = completed_cur.calls[0]
    assert completed_params[0] == "completed"
    assert _RECOVERABLE in completed_sql
    assert "'ready'" not in completed_sql

    app_cur = _Cursor(all_rows=[_lease_row(state="released")])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(app_cur)):
        app_rows = await lease_db.release_app_leases("claude-code")
    assert app_rows
    app_sql = app_cur.calls[0][0]
    assert "UPDATE autonomy_tickets" in app_sql
    assert "status = 'queued'" in app_sql
    assert "revoked" in app_cur.calls[0][1]

    expire_cur = _Cursor(one=[_lease_row(state="expired")])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(expire_cur)):
        await lease_db.expire_lease(_LEASE)
    expire_sql = expire_cur.calls[0][0]
    assert "UPDATE autonomy_tickets" in expire_sql
    assert "status = 'queued'" in expire_sql


def _assert_requeue_is_fenced(sql: str, alias: str) -> None:
    """A stale lease must not requeue a newer owner's Ticket."""
    statement = " ".join(str(sql).split())
    assert f"t.revision = {alias}.revision" in statement
    assert "t.worker_lease_id IS NULL" in statement
    assert "t.worker_run_id IS NULL" in statement


@pytest.mark.asyncio
async def test_a_stale_app_lease_cannot_requeue_a_reassigned_daemon_claim():
    """Reassignment moves the Ticket to a new generation before the sweep.

    The old app lease was issued at revision 7. A reassignment followed by a
    daemon claim advances the Ticket revision and sets ``worker_lease_id``, so
    every release/expiry path must match the lease generation and require that
    no foreign owner holds the Ticket; matching ``ticket_id`` alone would put
    the new owner's running work back in the queue.
    """
    stale = _lease_row(revision=7, state="released")

    release_cur = _Cursor(one=[stale])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(release_cur)):
        await lease_db.release_lease(_LEASE, app_id="claude-code")
    _assert_requeue_is_fenced(release_cur.calls[0][0], "r")

    expire_cur = _Cursor(one=[_lease_row(revision=7, state="expired")])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(expire_cur)):
        await lease_db.expire_lease(_LEASE)
    _assert_requeue_is_fenced(expire_cur.calls[0][0], "e")

    app_cur = _Cursor(all_rows=[stale])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(app_cur)):
        await lease_db.release_app_leases("claude-code")
    _assert_requeue_is_fenced(app_cur.calls[0][0], "r")

    sweep_cur = _Cursor(all_rows=[{"lease_id": "l1", "requeued": 0}])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(sweep_cur)):
        await lease_db.sweep_expired_leases(limit=10)
    _assert_requeue_is_fenced(sweep_cur.calls[0][0], "e")

    handoff_cur = _Cursor(all_rows=[{"handoff_id": "h1", "released": 0}])
    with patch(f"{_HANDOFFS}.get_db_connection", return_value=_Conn(handoff_cur)):
        await handoff_db.expire_handoffs(limit=5)
    _assert_requeue_is_fenced(handoff_cur.calls[0][0], "r")


@pytest.mark.asyncio
async def test_claim_and_audit_join_one_caller_transaction():
    cur = _Cursor(
        one=[
            {"ticket_id": _TICKET, "revision": 3},
            _lease_row(revision=3),
            {"audit_id": "a1", "app_id": "claude-code", "code": "ok"},
        ]
    )
    conn = _Conn(cur)
    with patch(f"{_TRANSACTIONS}.get_db_connection", return_value=conn):
        async with collab_transaction() as db:
            lease = await lease_db.claim_ticket_lease(
                ticket_id=_TICKET,
                app_id="claude-code",
                revision=2,
                conn=db,
            )
            row = await audit_db.record_audit(
                tool="task_claim",
                app_id="claude-code",
                target=_TARGET,
                conn=db,
            )
    assert lease["revision"] == 3
    assert row["audit_id"] == "a1"
    # One transaction, opened by the caller; neither helper checked out a
    # second connection or committed on its own.
    assert conn.transactions == 1
    assert len(cur.calls) == 3
    assert "INSERT INTO collab_audit" in cur.calls[-1][0]


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_the_claim_transaction():
    cur = _Cursor(
        one=[{"ticket_id": _TICKET, "revision": 3}, _lease_row(revision=3)],
        raise_on="INSERT INTO collab_audit",
    )
    conn = _Conn(cur)
    with patch(f"{_TRANSACTIONS}.get_db_connection", return_value=conn):
        with pytest.raises(RuntimeError, match="audit insert failed"):
            async with collab_transaction() as db:
                await lease_db.claim_ticket_lease(
                    ticket_id=_TICKET,
                    app_id="claude-code",
                    revision=2,
                    conn=db,
                )
                await audit_db.record_audit(
                    tool="task_claim", app_id="claude-code", conn=db
                )
    assert conn.transactions == 1
    assert conn.tx.exceptions and isinstance(conn.tx.exceptions[0], RuntimeError)


@pytest.mark.asyncio
async def test_scope_change_joins_the_callers_transaction_and_rolls_back_with_audit():
    cur = _Cursor(
        one=[_app_row(scopes=["read", "report"])],
        raise_on="INSERT INTO collab_audit",
    )
    conn = _Conn(cur)
    with patch(f"{_TRANSACTIONS}.get_db_connection", return_value=conn):
        with pytest.raises(RuntimeError, match="audit insert failed"):
            async with collab_transaction() as db:
                updated = await app_db.update_scopes(
                    "claude-code", ["read", "report"], conn=db
                )
                assert updated["scopes"] == ["read", "report"]
                await audit_db.record_audit(
                    tool="apps.scopes", app_id="claude-code", conn=db
                )
    # No second transaction: the scope UPDATE is in the caller's boundary and
    # the failed audit insert aborts that boundary.
    assert conn.transactions == 1
    assert conn.tx.exceptions and isinstance(conn.tx.exceptions[0], RuntimeError)
    assert "UPDATE collab_apps" in cur.calls[0][0]
    assert "INSERT INTO collab_audit" in cur.calls[1][0]


@pytest.mark.asyncio
async def test_message_send_joins_the_callers_transaction_and_rolls_back_with_audit():
    cur = _Cursor(
        one=[{"unread": 0}, {"message_id": "m1", "text": "hi"}],
        raise_on="INSERT INTO collab_audit",
    )
    conn = _Conn(cur)
    with patch(f"{_TRANSACTIONS}.get_db_connection", return_value=conn):
        with pytest.raises(RuntimeError, match="audit insert failed"):
            async with collab_transaction() as db:
                sent = await message_db.send_message(
                    from_app_id="claude-code",
                    to_target="app:codex",
                    text="hi",
                    conn=db,
                )
                assert sent["message_id"] == "m1"
                await audit_db.record_audit(
                    tool="agent_ask", app_id="claude-code", conn=db
                )
    assert conn.transactions == 1
    assert conn.tx.exceptions and isinstance(conn.tx.exceptions[0], RuntimeError)
    assert "pg_advisory_xact_lock" in cur.calls[0][0]
    assert "INSERT INTO collab_messages" in cur.calls[2][0]
    assert "INSERT INTO collab_audit" in cur.calls[3][0]


@pytest.mark.asyncio
async def test_review_verdict_joins_the_callers_transaction_and_rolls_back_with_audit():
    cur = _Cursor(
        one=[
            {
                "author_app_id": "claude-code",
                "reviewer_target": "any",
                "state": "pending",
            },
            {"review_id": "r1", "state": "completed"},
        ],
        raise_on="INSERT INTO collab_audit",
    )
    conn = _Conn(cur)
    with patch(f"{_TRANSACTIONS}.get_db_connection", return_value=conn):
        with pytest.raises(RuntimeError, match="audit insert failed"):
            async with collab_transaction() as db:
                reviewed = await review_db.submit_review(
                    review_id="r1",
                    reviewer_app_id="codex",
                    verdict={"passed": True, "blockers": [], "verdict": "ok"},
                    conn=db,
                )
                assert reviewed["state"] == "completed"
                await audit_db.record_audit(
                    tool="review_submit", app_id="codex", conn=db
                )
    assert conn.transactions == 1
    assert conn.tx.exceptions and isinstance(conn.tx.exceptions[0], RuntimeError)
    assert "UPDATE collab_reviews" in cur.calls[1][0]
    assert "INSERT INTO collab_audit" in cur.calls[2][0]


@pytest.mark.asyncio
async def test_the_expiry_sweep_requeues_the_ticket_in_the_same_statement():
    cur = _Cursor(all_rows=[{"lease_id": "l1", "requeued": 1}])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(cur)):
        rows = await lease_db.sweep_expired_leases(limit=10)
    assert rows == [{"lease_id": "l1", "requeued": 1}]
    sql, params = cur.calls[0]
    assert "state = 'expired'" in sql
    assert "UPDATE autonomy_tickets" in sql
    assert "status = 'queued'" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert params == (10,)


@pytest.mark.asyncio
async def test_handoff_create_keeps_only_a_conversation_reference():
    cur = _Cursor(one=[{"handoff_id": "h1", "state": "pending"}])
    with patch(f"{_HANDOFFS}.get_db_connection", return_value=_Conn(cur)):
        row = await handoff_db.create_handoff(
            lease_id=_LEASE,
            from_app_id="claude-code",
            reason="end_of_session",
            expires_at=None,
            conversation_ref={"app": "claude-code", "session_id": "session-1"},
        )
    assert row["handoff_id"] == "h1"
    sql, params = cur.calls[0]
    assert "conversation_ref" in sql
    assert "session_id" in str(params)
    with pytest.raises(ValueError):
        await handoff_db.create_handoff(
            lease_id="l1",
            from_app_id="claude-code",
            reason="stuck",
            expires_at=None,
            conversation_ref={"app": "x", "session_id": "s", "body": "secret"},
        )


@pytest.mark.asyncio
async def test_accept_handoff_locks_and_transfers_the_lease():
    cur = _Cursor(
        one=[
            {"handoff_id": "h1", "lease_id": "l1", "expires_at": None},
            {"handoff_id": "h1", "lease_id": "l1", "state": "accepted"},
            _lease_row(lease_id="l1", app_id="codex", state="active"),
        ]
    )
    conn = _Conn(cur)
    with patch(f"{_HANDOFFS}.get_db_connection", return_value=conn):
        result = await handoff_db.accept_handoff(
            handoff_id="h1", app_id="codex", agent="gpt-5-codex"
        )
    assert result["handoff"]["state"] == "accepted"
    assert result["lease"]["app_id"] == "codex"
    assert conn.transactions == 1
    select_sql, select_params = cur.calls[0]
    assert "FOR UPDATE OF h, l" in select_sql
    assert "l.expires_at > NOW()" in select_sql
    assert "l.max_lease_until > NOW()" in select_sql
    assert select_params == ("h1", "app:codex", "codex")
    lease_sql = cur.calls[2][0]
    assert "UPDATE collab_leases" in lease_sql
    assert "expires_at > NOW()" in lease_sql
    assert "max_lease_until > NOW()" in lease_sql
    assert cur.calls[2][1][0] == "codex"


@pytest.mark.asyncio
async def test_a_late_handoff_is_refused_at_every_transition():
    """The sweep may not have run yet; persisted deadlines still decide.

    ``NOW()`` guards on the handoff-pending transition, acceptance and transfer
    keep an expired lease from being extended or transferred before the sweep
    observes it.
    """
    pending_cur = _Cursor(one=[None])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(pending_cur)):
        assert (
            await lease_db.set_handoff_pending(
                _LEASE,
                app_id="claude-code",
                expires_at=datetime.now(timezone.utc),
            )
            is None
        )
    pending_sql = pending_cur.calls[0][0]
    assert "expires_at > NOW()" in pending_sql
    assert "max_lease_until > NOW()" in pending_sql

    accept_cur = _Cursor(one=[None])
    with patch(f"{_HANDOFFS}.get_db_connection", return_value=_Conn(accept_cur)):
        assert (
            await handoff_db.accept_handoff(handoff_id="h1", app_id="codex") is None
        )
    accept_sql = accept_cur.calls[0][0]
    assert "l.expires_at > NOW()" in accept_sql
    assert "l.max_lease_until > NOW()" in accept_sql
    assert len(accept_cur.calls) == 1

    transfer_cur = _Cursor(one=[None])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(transfer_cur)):
        assert (
            await lease_db.transfer_lease(
                _LEASE, from_app_id="claude-code", to_app_id="codex"
            )
            is None
        )
    transfer_sql = transfer_cur.calls[0][0]
    assert "expires_at > NOW()" in transfer_sql
    assert "max_lease_until > NOW()" in transfer_sql


@pytest.mark.asyncio
async def test_expired_handoffs_release_their_pending_lease():
    cur = _Cursor(all_rows=[{"handoff_id": "h1", "released": 1}])
    with patch(f"{_HANDOFFS}.get_db_connection", return_value=_Conn(cur)):
        rows = await handoff_db.expire_handoffs(limit=5)
    assert rows[0]["released"] == 1
    sql, params = cur.calls[0]
    assert "state = 'expired'" in sql
    assert "handoff_expired" in sql
    assert "UPDATE autonomy_tickets" in sql
    assert "status = 'queued'" in sql
    assert params == (5,)


@pytest.mark.asyncio
async def test_message_send_checks_the_channel_capacity_under_a_lock():
    cur = _Cursor(one=[{"unread": 0}, {"message_id": "m1", "text": "hi"}])
    conn = _Conn(cur)
    with patch(f"{_MESSAGES}.get_db_connection", return_value=conn):
        row = await message_db.send_message(
            from_app_id="claude-code",
            to_target="app:codex",
            text="hi",
        )
    assert row["message_id"] == "m1"
    assert row["untrusted"] is True
    assert conn.transactions == 1
    assert "pg_advisory_xact_lock" in cur.calls[0][0]
    assert "COUNT(*)" in cur.calls[1][0]
    assert "INSERT INTO collab_messages" in cur.calls[2][0]


@pytest.mark.asyncio
async def test_message_send_refuses_a_full_channel_before_inserting():
    cur = _Cursor(one=[{"unread": 32}])
    with patch(f"{_MESSAGES}.get_db_connection", return_value=_Conn(cur)):
        with pytest.raises(ChannelFullError):
            await message_db.send_message(
                from_app_id="claude-code", to_target="app:codex", text="hi"
            )
    assert not any("INSERT INTO collab_messages" in sql for sql, _ in cur.calls)


@pytest.mark.asyncio
async def test_inbox_peek_does_not_consume_but_read_does():
    message = {
        "message_id": "m1",
        "from_app_id": "claude-code",
        "from_agent": "claude",
        "to_target": "app:codex",
        "text": "hi",
        "about_ref": "",
        "reply_to": "",
        "read_at": None,
        "created_at": None,
    }
    peek_cur = _Cursor(all_rows=[message])
    with patch(f"{_MESSAGES}.get_db_connection", return_value=_Conn(peek_cur)):
        assert await message_db.inbox(to_target="app:codex", peek=True)
    assert not any("UPDATE collab_messages" in sql for sql, _ in peek_cur.calls)

    read_cur = _Cursor(all_rows=[message])
    with patch(f"{_MESSAGES}.get_db_connection", return_value=_Conn(read_cur)):
        rows = await message_db.inbox(to_target="app:codex")
    assert rows[0]["untrusted"] is True
    assert any("UPDATE collab_messages" in sql for sql, _ in read_cur.calls)


@pytest.mark.asyncio
async def test_reviews_refuse_self_review_by_app_id():
    with pytest.raises(SelfReviewError):
        await review_db.create_review(
            requester_app_id="claude-code",
            author_app_id="claude-code",
            target_ref=_TARGET,
            commit_sha="abc1234",
            reviewer="app:claude-code",
        )


@pytest.mark.asyncio
async def test_create_review_stores_the_canonical_reviewer_and_commit(monkeypatch):
    monkeypatch.setenv(_DEDUP_ENV, "off")
    cur = _Cursor(
        one=[
            {
                "review_id": "r1",
                "reviewer_target": "app:codex",
                "state": "pending",
            }
        ]
    )
    with patch(f"{_REVIEWS}.get_db_connection", return_value=_Conn(cur)):
        row = await review_db.create_review(
            requester_app_id="claude-code",
            author_app_id="claude-code",
            target_ref=_TARGET,
            commit_sha="abc1234",
            reviewer="app:codex",
        )
    assert row["review_id"] == "r1"
    _sql, params = cur.calls[0]
    assert params[2] == "app:codex"
    assert params[3] == "codex"
    assert params[4] == _TARGET


@pytest.mark.asyncio
async def test_submit_review_rechecks_the_author_before_the_update():
    cur = _Cursor(
        one=[
            {
                "author_app_id": "claude-code",
                "reviewer_target": "app:codex",
                "state": "pending",
            }
        ]
    )
    with patch(f"{_REVIEWS}.get_db_connection", return_value=_Conn(cur)):
        with pytest.raises(SelfReviewError):
            await review_db.submit_review(
                review_id="r1",
                reviewer_app_id="claude-code",
                verdict={"passed": True, "blockers": [], "verdict": "ok"},
            )
    assert len(cur.calls) == 1


@pytest.mark.asyncio
async def test_submit_review_refuses_another_apps_assignment():
    cur = _Cursor(
        one=[
            {
                "author_app_id": "claude-code",
                "reviewer_target": "app:codex",
                "state": "pending",
            }
        ]
    )
    with patch(f"{_REVIEWS}.get_db_connection", return_value=_Conn(cur)):
        with pytest.raises(ScopeDeniedError):
            await review_db.submit_review(
                review_id="r1",
                reviewer_app_id="gemini",
                verdict={"passed": True, "blockers": [], "verdict": "ok"},
            )
    assert len(cur.calls) == 1


@pytest.mark.asyncio
async def test_submit_review_writes_a_completed_verdict_for_an_any_reviewer():
    cur = _Cursor(
        one=[
            {
                "author_app_id": "claude-code",
                "reviewer_target": "any",
                "state": "pending",
            },
            {
                "review_id": "r1",
                "state": "completed",
                "verdict": {"passed": True, "blockers": [], "verdict": "ok"},
            },
        ]
    )
    with patch(f"{_REVIEWS}.get_db_connection", return_value=_Conn(cur)):
        row = await review_db.submit_review(
            review_id="r1",
            reviewer_app_id="codex",
            verdict={"passed": True, "blockers": [], "verdict": "ok"},
        )
    assert row["state"] == "completed"
    sql, params = cur.calls[1]
    assert "state = 'completed'" in sql
    assert "author_app_id <> %s" in sql
    assert params[1] == "codex"


def test_review_verdict_shape_is_closed():
    with pytest.raises(InvalidSchemaError):
        review_db._validate_verdict({"passed": True, "verdict": "ok"})


# --- review de-duplication (same target, commit and reviewer) ------------

_EXISTING_REVIEW = {"review_id": "r-old", "state": "completed", "verdict": None}
_NEW_REVIEW = {"review_id": "r-new", "state": "pending", "verdict": None}


async def _create(cur, **extra):
    with patch(f"{_REVIEWS}.get_db_connection", return_value=_Conn(cur)):
        return await review_db.create_review(
            requester_app_id="codex",
            author_app_id="codex",
            target_ref=_TARGET,
            commit_sha="abc1234",
            reviewer="app:claude-code",
            **extra,
        )


@pytest.mark.asyncio
async def test_create_review_off_mode_never_looks_for_a_duplicate(monkeypatch):
    monkeypatch.setenv(_DEDUP_ENV, "off")
    cur = _Cursor(one=[_NEW_REVIEW])
    row = await _create(cur)
    assert row["review_id"] == "r-new"
    assert len(cur.calls) == 1 and "INSERT INTO collab_reviews" in cur.calls[0][0]


@pytest.mark.asyncio
async def test_create_review_shadow_mode_logs_and_still_inserts(monkeypatch, caplog):
    monkeypatch.delenv(_DEDUP_ENV, raising=False)
    cur = _Cursor(one=[_EXISTING_REVIEW, _NEW_REVIEW])
    with caplog.at_level("INFO", logger=_REVIEWS):
        row = await _create(cur)
    assert row["review_id"] == "r-new" and "deduplicated" not in row
    lock, select, insert = cur.calls
    assert "pg_advisory_xact_lock" in lock[0]
    assert select[0].startswith("SELECT")
    assert "state IN ('pending', 'completed')" in select[0]
    assert select[1] == ("codex", _TARGET, "abc1234", "app:claude-code")
    assert "INSERT INTO collab_reviews" in insert[0]
    assert "would reuse" in caplog.text and "r-old" in caplog.text


@pytest.mark.asyncio
async def test_create_review_enforce_mode_returns_the_existing_review(monkeypatch):
    monkeypatch.setenv(_DEDUP_ENV, "enforce")
    cur = _Cursor(one=[_EXISTING_REVIEW, _NEW_REVIEW])
    row = await _create(cur)
    assert row["review_id"] == "r-old" and row["deduplicated"] is True
    assert not any("INSERT" in sql for sql, _ in cur.calls)


@pytest.mark.asyncio
async def test_create_review_enforce_mode_force_inserts_anyway(monkeypatch):
    monkeypatch.setenv(_DEDUP_ENV, "enforce")
    cur = _Cursor(one=[_EXISTING_REVIEW, _NEW_REVIEW])
    row = await _create(cur, force=True)
    assert row["review_id"] == "r-new" and "deduplicated" not in row
    assert "INSERT INTO collab_reviews" in cur.calls[2][0]


@pytest.mark.asyncio
async def test_create_review_enforce_mode_inserts_without_a_match(monkeypatch):
    monkeypatch.setenv(_DEDUP_ENV, "enforce")
    cur = _Cursor(one=[None, _NEW_REVIEW])
    row = await _create(cur)
    assert row["review_id"] == "r-new"


def test_review_verdict_accepts_the_optional_n59_keys():
    clean = review_db._validate_verdict(
        {
            "passed": False,
            "blockers": ["tests missing"],
            "verdict": "needs work",
            "status": "inconclusive",
            "needed_evidence": ["pytest output"],
        }
    )
    assert clean["status"] == "inconclusive"
    assert clean["needed_evidence"] == ["pytest output"]
    plain = review_db._validate_verdict({"passed": True, "blockers": [], "verdict": "ok"})
    assert set(plain) == {"passed", "blockers", "verdict"}


@pytest.mark.parametrize(
    "extra",
    [
        {"status": "maybe"},
        {"status": 1},
        {"needed_evidence": "logs"},
        {"needed_evidence": [3]},
        {"needed_evidence": ["x" * 501]},
        {"confidence": 0.9},
    ],
)
def test_review_verdict_still_rejects_bad_optional_keys(extra):
    with pytest.raises(InvalidSchemaError):
        review_db._validate_verdict(
            {"passed": True, "blockers": [], "verdict": "ok", **extra}
        )


# --- P0 fix: atomic dedup, requester in the key, off keeps main ----------

_STRICT_ENV = "CONDUCTOR_DISPATCH_REVIEW_STRICT_MODE"


class _OrderedTx(_Tx):
    def __init__(self, events):
        super().__init__()
        self.events = events

    async def __aenter__(self):
        self.events.append("begin")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.events.append("commit" if exc is None else "rollback")
        return False


class _OrderedCursor(_Cursor):
    def __init__(self, events, **kwargs):
        super().__init__(**kwargs)
        self.events = events

    async def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        self.events.append(text.split(" ", 2)[0] + (" lock" if "advisory" in text else ""))
        await super().execute(sql, params)


async def _create_ordered(mode, monkeypatch, *, requester="codex", one=None, conn=None):
    monkeypatch.setenv(_DEDUP_ENV, mode)
    events: list[str] = []
    cur = _OrderedCursor(events, one=one if one is not None else [None, _NEW_REVIEW])
    db = _Conn(cur)
    db.tx = _OrderedTx(events)
    with patch(f"{_REVIEWS}.get_db_connection", return_value=db):
        row = await review_db.create_review(
            requester_app_id=requester,
            author_app_id="codex",
            target_ref=_TARGET,
            commit_sha="abc1234",
            reviewer="app:claude-code",
            conn=conn,
        )
    return row, cur, db, events


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shadow", "enforce"])
async def test_create_review_locks_before_the_select_in_one_transaction(mode, monkeypatch):
    _row, cur, db, events = await _create_ordered(mode, monkeypatch)
    assert events == ["begin", "SELECT lock", "SELECT", "INSERT", "commit"]
    assert db.transactions == 1
    lock_sql, lock_params = cur.calls[0]
    assert "pg_advisory_xact_lock" in lock_sql
    assert "codex" in lock_params[0] and _TARGET in lock_params[0]
    assert "abc1234" in lock_params[0] and "app:claude-code" in lock_params[0]


@pytest.mark.asyncio
async def test_create_review_with_caller_conn_uses_the_callers_transaction(monkeypatch):
    sentinel = object()
    _row, _cur, db, events = await _create_ordered("enforce", monkeypatch, conn=sentinel)
    assert db.transactions == 0
    assert events == ["SELECT lock", "SELECT", "INSERT"]


@pytest.mark.asyncio
async def test_create_review_off_mode_takes_no_lock_and_no_transaction(monkeypatch):
    _row, cur, db, events = await _create_ordered("off", monkeypatch, one=[_NEW_REVIEW])
    assert events == ["INSERT"]
    assert db.transactions == 0
    assert not any("advisory" in sql for sql, _ in cur.calls)


@pytest.mark.asyncio
async def test_create_review_dedup_key_includes_the_requester(monkeypatch):
    _r1, cur_a, _db, _e = await _create_ordered("enforce", monkeypatch, requester="codex")
    _r2, cur_b, _db, _e = await _create_ordered("enforce", monkeypatch, requester="kimi")
    assert cur_a.calls[0][1] != cur_b.calls[0][1]
    select_a, select_b = cur_a.calls[1], cur_b.calls[1]
    assert "requester_app_id = %s" in select_a[0]
    assert select_a[1][0] == "codex" and select_b[1][0] == "kimi"


@pytest.mark.asyncio
async def test_create_review_other_requester_is_not_deduplicated(monkeypatch):
    # The SELECT filters on the requester, so the existing row of "codex" is
    # not found for "kimi" and a new review is inserted.
    row, cur, _db, _e = await _create_ordered(
        "enforce", monkeypatch, requester="kimi", one=[None, _NEW_REVIEW]
    )
    assert row["review_id"] == "r-new" and "deduplicated" not in row
    assert "INSERT INTO collab_reviews" in cur.calls[2][0]


_N59_VERDICT = {
    "passed": False,
    "blockers": [],
    "verdict": "unsure",
    "status": "inconclusive",
    "needed_evidence": ["ci log"],
}


@pytest.mark.parametrize("extra", [{"status": "pass"}, {"needed_evidence": ["x"]}])
def test_review_verdict_off_mode_rejects_the_n59_keys_like_main(extra, monkeypatch):
    monkeypatch.setenv(_STRICT_ENV, "off")
    with pytest.raises(InvalidSchemaError) as info:
        review_db._validate_verdict({"passed": True, "blockers": [], "verdict": "ok", **extra})
    assert str(info.value) == "review verdict must contain exactly passed, blockers and verdict"


def test_review_verdict_off_mode_keeps_the_plain_shape(monkeypatch):
    monkeypatch.setenv(_STRICT_ENV, "off")
    clean = review_db._validate_verdict({"passed": True, "blockers": ["a"], "verdict": "ok"})
    assert clean == {"passed": True, "blockers": ["a"], "verdict": "ok"}
    with pytest.raises(InvalidSchemaError) as info:
        review_db._validate_verdict({"passed": True, "blockers": "a", "verdict": "ok"})
    assert str(info.value) == "review blockers must be a list of at most 20 items"


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_review_verdict_shadow_and_enforce_accept_the_n59_keys(mode, monkeypatch):
    monkeypatch.setenv(_STRICT_ENV, mode)
    clean = review_db._validate_verdict(dict(_N59_VERDICT))
    assert clean["status"] == "inconclusive" and clean["needed_evidence"] == ["ci log"]


def test_the_database_suite_runs_with_a_temporary_home(
    isolated_collab_db_home, tmp_path
):
    assert Path.home() == isolated_collab_db_home
    assert tmp_path in Path.home().parents


# --- recovery of non-running Tickets -------------------------------------
#
# A non-done ``task_report`` keeps the lease but moves the Ticket to
# ``waiting`` or ``blocked``. Every path that ends the lease must return such
# a Ticket to ``queued``; matching only ``running`` leaves it unclaimable.


async def _every_recovery_statement() -> dict[str, tuple[str, str]]:
    statements: dict[str, tuple[str, str]] = {}

    cur = _Cursor(one=[_lease_row(state="released")])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(cur)):
        await lease_db.release_lease(_LEASE, app_id="claude-code")
    statements["release"] = (cur.calls[0][0], "r")

    cur = _Cursor(one=[_lease_row(state="expired")])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(cur)):
        await lease_db.expire_lease(_LEASE)
    statements["expire"] = (cur.calls[0][0], "e")

    cur = _Cursor(all_rows=[_lease_row(state="released")])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(cur)):
        await lease_db.release_app_leases("claude-code")
    statements["revoke"] = (cur.calls[0][0], "r")

    cur = _Cursor(all_rows=[{"lease_id": "l1", "requeued": 1}])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(cur)):
        await lease_db.sweep_expired_leases(limit=10)
    statements["sweep"] = (cur.calls[0][0], "e")

    cur = _Cursor(all_rows=[{"handoff_id": "h1", "released": 1}])
    with patch(f"{_HANDOFFS}.get_db_connection", return_value=_Conn(cur)):
        await handoff_db.expire_handoffs(limit=5)
    statements["handoff_timeout"] = (cur.calls[0][0], "r")

    cur = _Cursor(one=[{"handoff_id": "h1", "state": "cancelled", "requeued": 1}])
    with patch(f"{_HANDOFFS}.get_db_connection", return_value=_Conn(cur)):
        await handoff_db.decline_handoff("h1", app_id="codex")
    statements["handoff_decline"] = (cur.calls[0][0], "r")
    return statements


@pytest.mark.asyncio
async def test_every_recovery_path_requeues_waiting_and_blocked_tickets():
    statements = await _every_recovery_statement()
    assert set(statements) == {
        "release",
        "expire",
        "revoke",
        "sweep",
        "handoff_timeout",
        "handoff_decline",
    }
    for name, (sql, alias) in statements.items():
        assert "status = 'queued'" in sql, name
        assert _RECOVERABLE in sql, name
        assert "t.status = 'running'" not in sql, name
        # The earlier generation/ownership fence is kept on every path.
        _assert_requeue_is_fenced(sql, alias)


@pytest.mark.asyncio
async def test_recovery_never_requeues_ready_or_terminal_tickets():
    assert lease_db.RECOVERABLE_TICKET_STATES == ("running", "waiting", "blocked")
    statements = await _every_recovery_statement()
    for name, (sql, _alias) in statements.items():
        requeue = sql.split("UPDATE autonomy_tickets", 1)[1].split("RETURNING", 1)[0]
        for state in (
            "queued",
            "ready",
            "completed",
            "cancelled",
            "failed",
            "archived",
            "awaiting_clarification",
        ):
            assert f"'{state}'" not in requeue.replace("'queued', revision", ""), (
                name,
                state,
            )


@pytest.mark.asyncio
async def test_a_report_advances_the_lease_generation_with_the_ticket():
    """Reports move the Ticket revision; the lease must follow it.

    Otherwise the generation fence (``t.revision = lease.revision``) refuses
    every later requeue and the ``waiting``/``blocked`` Ticket is stranded.
    """
    cur = _Cursor(one=[_lease_row(revision=3, ticket_status="waiting")])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(cur)):
        row = await lease_db.record_lease_report(
            _LEASE, app_id="claude-code", ticket_status="waiting"
        )
    assert row["revision"] == 3 and row["ticket_status"] == "waiting"
    sql, params = cur.calls[0]
    assert "FOR UPDATE" in sql
    assert "state = 'active'" in sql
    assert "expires_at > NOW()" in sql and "max_lease_until > NOW()" in sql
    assert _RECOVERABLE in sql
    assert "t.worker_lease_id IS NULL" in sql and "t.worker_run_id IS NULL" in sql
    assert "SET revision = m.revision" in sql
    assert "THEN 'completed'" in sql and "'report_done'" in sql
    assert params[:3] == (_LEASE, "claude-code", "waiting")

    for refused in ("running", "queued", "completed", "cancelled"):
        with pytest.raises(ValueError):
            await lease_db.record_lease_report(
                _LEASE, app_id="claude-code", ticket_status=refused
            )


@pytest.mark.asyncio
async def test_a_late_or_foreign_report_changes_nothing():
    cur = _Cursor(one=[None])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(cur)):
        assert (
            await lease_db.record_lease_report(
                _LEASE, app_id="codex", ticket_status="blocked"
            )
            is None
        )
    sql = cur.calls[0][0]
    assert "app_id = %s" in sql
    # The Ticket and lease updates join the locked, live lease row only.
    assert "FROM held h" in sql
    assert "'handoff_pending'" not in sql


@pytest.mark.asyncio
async def test_expire_lease_honours_the_persisted_deadline():
    """A caller's stale clock must not expire a lease a heartbeat renewed."""
    cur = _Cursor(one=[None])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(cur)):
        assert await lease_db.expire_lease(_LEASE) is None
    assert "(expires_at <= NOW() OR max_lease_until <= NOW())" in cur.calls[0][0]


@pytest.mark.asyncio
async def test_the_sweep_also_expires_leases_past_their_ceiling():
    cur = _Cursor(all_rows=[])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(cur)):
        await lease_db.sweep_expired_leases(limit=10)
    assert "(expires_at <= NOW() OR max_lease_until <= NOW())" in cur.calls[0][0]


@pytest.mark.asyncio
async def test_insert_lease_clamps_the_deadline_to_the_ceiling():
    now = datetime.now(timezone.utc)
    cur = _Cursor(one=[_lease_row()])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(cur)):
        await lease_db.insert_lease(
            app_id="claude-code",
            target_ref=_TARGET,
            target_kind="ticket",
            revision=1,
            expires_at=now,
            max_lease_until=now,
        )
    sql, params = cur.calls[0]
    assert "LEAST(%s::timestamptz, %s::timestamptz)" in sql
    assert params[-3:] == (now, now, now)


@pytest.mark.asyncio
async def test_claim_refuses_a_ticket_with_a_daemon_lease():
    """Recovery fences on ``worker_lease_id IS NULL``; a claim must too.

    Otherwise an app can claim a Ticket that still carries a stale daemon
    lease id, and no recovery path can ever return it to ``queued``.
    """
    cur = _Cursor(one=[None])
    with patch(f"{_LEASES}.get_db_connection", return_value=_Conn(cur)):
        assert (
            await lease_db.claim_ticket_lease(
                ticket_id=_TICKET, app_id="claude-code", revision=1
            )
            is None
        )
    assert "worker_lease_id IS NULL" in cur.calls[0][0]


@pytest.mark.asyncio
async def test_a_handoff_is_only_recorded_for_the_current_pending_holder():
    cur = _Cursor(one=[None])
    with patch(f"{_HANDOFFS}.get_db_connection", return_value=_Conn(cur)):
        assert (
            await handoff_db.create_handoff(
                lease_id=_LEASE, from_app_id="claude-code", reason="stuck"
            )
            is None
        )
    sql, params = cur.calls[0]
    assert "FROM collab_leases l" in sql
    assert "l.app_id = %s" in sql
    assert "l.state = 'handoff_pending'" in sql
    assert "p.state = 'pending'" in sql
    assert params[-2:] == (_LEASE, "claude-code")


@pytest.mark.asyncio
async def test_stale_handoffs_cannot_act_on_a_lease_that_changed_hands():
    accept_cur = _Cursor(one=[None])
    with patch(f"{_HANDOFFS}.get_db_connection", return_value=_Conn(accept_cur)):
        await handoff_db.accept_handoff(handoff_id="h1", app_id="codex")
    assert "l.app_id = h.from_app_id" in accept_cur.calls[0][0]

    expire_cur = _Cursor(all_rows=[])
    with patch(f"{_HANDOFFS}.get_db_connection", return_value=_Conn(expire_cur)):
        await handoff_db.expire_handoffs(limit=5)
    assert "l.app_id = e.from_app_id" in expire_cur.calls[0][0]

    decline_cur = _Cursor(one=[None])
    with patch(f"{_HANDOFFS}.get_db_connection", return_value=_Conn(decline_cur)):
        await handoff_db.decline_handoff("h1", app_id="codex")
    assert "l.app_id = d.from_app_id" in decline_cur.calls[0][0]


@pytest.mark.asyncio
async def test_cancelling_a_handoff_gives_the_lease_back_to_its_holder():
    """A cancelled handoff must not leave the lease in ``handoff_pending``.

    Nobody can accept a cancelled handoff, and a pending lease cannot report,
    so the holder would be wedged until the ceiling.
    """
    cur = _Cursor(one=[{"handoff_id": "h1", "state": "cancelled", "restored": 1}])
    with patch(f"{_HANDOFFS}.get_db_connection", return_value=_Conn(cur)):
        row = await handoff_db.cancel_handoff("h1", app_id="claude-code")
    assert row["state"] == "cancelled"
    sql, params = cur.calls[0]
    assert "UPDATE collab_leases l SET state = 'active'" in sql
    assert "l.state = 'handoff_pending'" in sql
    assert "l.app_id = c.from_app_id" in sql
    assert "l.expires_at > NOW()" in sql and "l.max_lease_until > NOW()" in sql
    assert params == ("h1", "claude-code", "claude-code")


@pytest.mark.asyncio
async def test_declining_a_handoff_returns_the_task_to_the_router():
    cur = _Cursor(one=[{"handoff_id": "h1", "state": "cancelled", "requeued": 1}])
    with patch(f"{_HANDOFFS}.get_db_connection", return_value=_Conn(cur)):
        row = await handoff_db.decline_handoff("h1", app_id="codex")
    assert row["requeued"] == 1
    sql, params = cur.calls[0]
    assert "to_target IN (%s, %s)" in sql
    assert "'handoff_declined'" in sql
    assert "l.state = 'handoff_pending'" in sql
    assert params == ("h1", "app:codex", "codex")
