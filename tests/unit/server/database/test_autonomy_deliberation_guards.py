"""Guarded Postgres transitions for cancellation and settled task DAGs."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from src.server.database.autonomy import deliberations


class FakeCursor:
    def __init__(
        self,
        *,
        update_rowcount: int = 1,
        update_rowcounts: list[int] | None = None,
        fail_on: str | None = None,
    ):
        self.update_rowcount = update_rowcount
        self.update_rowcounts = list(update_rowcounts or [])
        self.fail_on = fail_on
        self.executions: list[tuple[str, tuple]] = []
        self.rowcount = 0

    async def execute(self, sql: str, params=()):
        normalized = " ".join(sql.split())
        self.executions.append((normalized, tuple(params)))
        if self.fail_on and self.fail_on in normalized:
            raise RuntimeError("simulated step insert failure")
        if normalized.startswith("UPDATE autonomy_deliberations"):
            self.rowcount = (
                self.update_rowcounts.pop(0)
                if self.update_rowcounts
                else self.update_rowcount
            )
        else:
            self.rowcount = 1


class FakeConnection:
    def __init__(self, cursor: FakeCursor):
        self.fake_cursor = cursor
        self.transaction_entries = 0
        self.transaction_exits: list[type[BaseException] | None] = []

    @asynccontextmanager
    async def cursor(self, **_kwargs):
        yield self.fake_cursor

    @asynccontextmanager
    async def transaction(self):
        self.transaction_entries += 1
        error_type = None
        try:
            yield
        except BaseException as exc:
            error_type = type(exc)
            raise
        finally:
            self.transaction_exits.append(error_type)


def _patch_connection(monkeypatch, connection: FakeConnection) -> None:
    @asynccontextmanager
    async def get_connection():
        yield connection

    monkeypatch.setattr(deliberations, "get_db_connection", get_connection)


def _step() -> dict:
    return {
        "step_id": "step-1",
        "step_key": "research",
        "title": "Research",
        "prompt": "Find evidence",
        "assigned_agent": "alpha",
        "depends_on": [],
        "status": "pending",
        "daemon_id": None,
        "child_ticket_id": None,
    }


@pytest.mark.asyncio
async def test_finalize_losing_cancel_race_does_not_write_task_steps(monkeypatch):
    cursor = FakeCursor(update_rowcount=0)
    connection = FakeConnection(cursor)
    _patch_connection(monkeypatch, connection)

    settled = await deliberations.PostgresDeliberationStore().finalize(
        "delib-1",
        status="consensus",
        round_index=1,
        consensus_plan={"summary": "agreed", "steps": []},
        decision_reason="all participants agreed",
        steps=[_step()],
    )

    assert settled is False
    assert connection.transaction_entries == 1
    assert len(cursor.executions) == 1
    assert "WHERE deliberation_id=%s AND status='running'" in cursor.executions[0][0]


@pytest.mark.asyncio
async def test_finalize_commits_decision_and_steps_in_one_transaction(monkeypatch):
    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    _patch_connection(monkeypatch, connection)

    settled = await deliberations.PostgresDeliberationStore().finalize(
        "delib-1",
        status="consensus",
        round_index=1,
        consensus_plan={"summary": "agreed", "steps": []},
        decision_reason="all participants agreed",
        steps=[_step()],
    )

    sql = [statement for statement, _params in cursor.executions]
    assert settled is True
    assert connection.transaction_entries == 1
    assert connection.transaction_exits == [None]
    assert sql[0].startswith("UPDATE autonomy_deliberations SET status=%s")
    assert sql[1] == "DELETE FROM autonomy_deliberation_steps WHERE deliberation_id=%s"
    assert sql[2].startswith("INSERT INTO autonomy_deliberation_steps")


@pytest.mark.asyncio
async def test_step_insert_error_exits_finalize_transaction_with_error(monkeypatch):
    cursor = FakeCursor(fail_on="INSERT INTO autonomy_deliberation_steps")
    connection = FakeConnection(cursor)
    _patch_connection(monkeypatch, connection)

    with pytest.raises(RuntimeError, match="simulated step insert failure"):
        await deliberations.PostgresDeliberationStore().finalize(
            "delib-1",
            status="consensus",
            round_index=1,
            consensus_plan={"summary": "agreed", "steps": []},
            decision_reason="all participants agreed",
            steps=[_step()],
        )

    assert connection.transaction_entries == 1
    assert connection.transaction_exits == [RuntimeError]


@pytest.mark.asyncio
async def test_cancel_and_failure_updates_are_guarded_to_running_states(monkeypatch):
    cursor = FakeCursor(update_rowcount=0)
    connection = FakeConnection(cursor)
    _patch_connection(monkeypatch, connection)
    store = deliberations.PostgresDeliberationStore()

    assert await store.request_cancel("delib-1") is False
    assert "status IN ('draft', 'running')" in cursor.executions[-1][0]
    assert await store.fail_running("delib-1", "late provider error") is False
    assert "AND status='running'" in cursor.executions[-1][0]
    assert cursor.executions[-1][1][0] == "late provider error"


@pytest.mark.asyncio
async def test_stale_recovery_uses_running_status_and_heartbeat_cutoff(monkeypatch):
    cursor = FakeCursor(update_rowcount=1)
    connection = FakeConnection(cursor)
    _patch_connection(monkeypatch, connection)

    assert await deliberations.PostgresDeliberationStore().fail_stale_running(
        "delib-1", stale_seconds=1800
    )

    sql, params = cursor.executions[0]
    assert "WHERE deliberation_id=%s AND status='running'" in sql
    assert "updated_at < NOW() - (%s * INTERVAL '1 second')" in sql
    assert params == ("delib-1", 1800)


@pytest.mark.asyncio
async def test_finalize_loses_after_stale_recovery_claims_running_row(monkeypatch):
    cursor = FakeCursor(update_rowcounts=[1, 0])
    connection = FakeConnection(cursor)
    _patch_connection(monkeypatch, connection)
    store = deliberations.PostgresDeliberationStore()

    assert await store.fail_stale_running("delib-1", stale_seconds=1800)
    settled = await store.finalize(
        "delib-1",
        status="consensus",
        round_index=1,
        consensus_plan={"summary": "agreed", "steps": []},
        decision_reason="all participants agreed",
        steps=[_step()],
    )

    assert not settled
    assert len(cursor.executions) == 2
    assert "updated_at < NOW() - (%s * INTERVAL '1 second')" in cursor.executions[0][0]
    assert "WHERE deliberation_id=%s AND status='running'" in cursor.executions[1][0]
