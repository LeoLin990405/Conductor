"""Review queue and review/merge audit persistence.

The fake cursor mirrors the psycopg3 async surface the module uses, so these
tests pin the SQL, its parameters, and the same-transaction audit write without
a live Postgres.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from src.server.database.autonomy import reviews

NOW = datetime.now(UTC)


class _Cursor:
    def __init__(self, fetchone_results=None, fetchall_results=None):
        self.fetchone_results = iter(fetchone_results or [])
        self.fetchall_results = iter(fetchall_results or [])
        self.executions: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, params=()):
        self.executions.append((" ".join(sql.split()), tuple(params)))

    async def fetchone(self):
        return next(self.fetchone_results)

    async def fetchall(self):
        return next(self.fetchall_results)


class _Connection:
    def __init__(self, cursor: _Cursor):
        self.fake_cursor = cursor
        self.transactions = 0

    @asynccontextmanager
    async def cursor(self, **_kwargs):
        yield self.fake_cursor

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        yield self


def _patch_connection(monkeypatch, cursor: _Cursor) -> _Connection:
    connection = _Connection(cursor)

    @asynccontextmanager
    async def get_connection():
        yield connection

    monkeypatch.setattr(reviews, "get_db_connection", get_connection)
    return connection


def _row(**overrides) -> dict[str, Any]:
    base = {
        "review_id": "11111111-1111-1111-1111-111111111111",
        "user_id": "u1",
        "line": "feat/line",
        "reviewer": "codexrev",
        "reviewer_provider": "codex",
        "state": "queued",
        "verdict": None,
        "head_sha": "",
        "note": "",
        "created_at": NOW,
        "completed_at": None,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_create_review_queues_and_audits_in_one_transaction(monkeypatch):
    cursor = _Cursor(fetchone_results=[_row(head_sha="a" * 40)])
    connection = _patch_connection(monkeypatch, cursor)

    out = await reviews.create_review(
        user_id="u1",
        line="feat/line",
        reviewer="codexrev",
        head_sha="a" * 40,
    )

    assert connection.transactions == 1
    review_sql, review_params = cursor.executions[0]
    audit_sql, audit_params = cursor.executions[1]
    assert review_sql.startswith("INSERT INTO autonomy_review_records (")
    assert "user_id, line, reviewer, reviewer_provider, head_sha" in review_sql
    assert review_params == ("u1", "feat/line", "codexrev", "codex", "a" * 40)
    assert audit_sql.startswith("INSERT INTO autonomy_review_audit (")
    assert audit_params[0] == "u1"
    assert audit_params[1] == "review_requested"
    assert out["review_id"] == "11111111-1111-1111-1111-111111111111"
    assert out["state"] == "queued"
    assert out["head_sha"] == "a" * 40


def test_merge_started_is_an_audit_action():
    assert "merge_started" in reviews.AUDIT_ACTIONS


def test_audit_rejects_unknown_action_and_outcome():
    import asyncio

    async def run() -> None:
        cursor = _Cursor()
        with pytest.raises(ValueError, match="audit action"):
            await reviews.insert_audit(
                cursor, user_id="u1", action="deleted_everything"
            )
        with pytest.raises(ValueError, match="audit outcome"):
            await reviews.insert_audit(
                cursor, user_id="u1", action="merge_refused", outcome="maybe"
            )
        assert cursor.executions == []

    asyncio.run(run())


@pytest.mark.asyncio
async def test_list_reviews_is_user_scoped_and_filters_line(monkeypatch):
    cursor = _Cursor(fetchall_results=[[_row()]])
    _patch_connection(monkeypatch, cursor)

    out = await reviews.list_reviews("u1", line="feat/line", limit=7)

    sql, params = cursor.executions[0]
    assert "WHERE user_id = %s AND line = %s" in sql
    assert "ORDER BY created_at DESC, review_id DESC" in sql
    assert params == ("u1", "feat/line", 7)
    assert [row["review_id"] for row in out] == [_row()["review_id"]]


@pytest.mark.asyncio
async def test_list_reviews_without_line_has_no_line_filter(monkeypatch):
    cursor = _Cursor(fetchall_results=[[]])
    _patch_connection(monkeypatch, cursor)

    await reviews.list_reviews("u1", limit=10)

    sql, params = cursor.executions[0]
    assert "WHERE user_id = %s" in sql
    assert "line = %s" not in sql
    assert params == ("u1", 10)


@pytest.mark.asyncio
async def test_complete_review_cas_update_and_audit(monkeypatch):
    completed = _row(
        state="completed",
        verdict="merge",
        head_sha="a" * 40,
        completed_at=NOW,
    )
    cursor = _Cursor(fetchone_results=[completed])
    connection = _patch_connection(monkeypatch, cursor)

    out = await reviews.complete_review(
        "u1", "rid", verdict="merge", note="looks good"
    )

    assert connection.transactions == 1
    update_sql, update_params = cursor.executions[0]
    assert "SET state = 'completed'" in update_sql
    assert "SET state = 'completed', verdict = %s, note = %s, completed_at = NOW()" in update_sql
    assert "WHERE review_id = %s AND user_id = %s AND state = 'queued'" in update_sql
    assert update_params == ("merge", "looks good", "rid", "u1")
    audit_sql, audit_params = cursor.executions[1]
    assert audit_sql.startswith("INSERT INTO autonomy_review_audit (")
    assert audit_params[1] == "review_completed"
    assert out == completed


@pytest.mark.asyncio
async def test_complete_review_returns_none_when_already_finished(monkeypatch):
    cursor = _Cursor(fetchone_results=[None])
    _patch_connection(monkeypatch, cursor)

    out = await reviews.complete_review("u1", "rid", verdict="block")

    assert out is None
    assert len(cursor.executions) == 1


@pytest.mark.asyncio
async def test_complete_review_rejects_unknown_verdict(monkeypatch):
    cursor = _Cursor()
    _patch_connection(monkeypatch, cursor)

    with pytest.raises(ValueError, match="verdict"):
        await reviews.complete_review("u1", "rid", verdict="lgtm")

    assert cursor.executions == []


@pytest.mark.asyncio
async def test_list_audit_filters_line_and_action_and_clamps_query(monkeypatch):
    audit = {
        "audit_id": "22222222-2222-2222-2222-222222222222",
        "user_id": "u1",
        "action": "merge_refused",
        "line": "feat/line",
        "reviewer": "",
        "target_branch": "integrate/r2",
        "outcome": "refused",
        "detail": {"code": "tests_failed"},
        "created_at": NOW,
    }
    cursor = _Cursor(fetchall_results=[[audit]])
    _patch_connection(monkeypatch, cursor)

    out = await reviews.list_audit(
        "u1", line="feat/line", action="merge_refused", limit=5
    )

    sql, params = cursor.executions[0]
    assert "WHERE user_id = %s AND line = %s AND action = %s" in sql
    assert params == ("u1", "feat/line", "merge_refused", 5)
    assert out[0]["audit_id"] == "22222222-2222-2222-2222-222222222222"
    assert out[0]["detail"] == {"code": "tests_failed"}
