"""release_daemon_claim: an unlaunchable claim is given back, fenced on its live lease.

Fake connection only: it records the SQL and answers the Ticket update with a
chosen rowcount, so no database is reached.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from src.server.database.autonomy import daemons


class _Cursor:
    def __init__(self, log: list, rowcount: int) -> None:
        self.log = log
        self._rowcount = rowcount
        self.rowcount = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=()):
        self.log.append((" ".join(sql.split()), tuple(params)))
        self.rowcount = self._rowcount if len(self.log) == 1 else 1


class _Conn:
    def __init__(self, log: list, rowcount: int) -> None:
        self.log = log
        self.rowcount = rowcount

    @asynccontextmanager
    async def transaction(self):
        yield

    def cursor(self, row_factory=None):
        return _Cursor(self.log, self.rowcount)


def _patch(monkeypatch, rowcount: int) -> list:
    log: list = []

    @asynccontextmanager
    async def connect():
        yield _Conn(log, rowcount)

    monkeypatch.setattr(daemons, "get_db_connection", connect)
    return log


@pytest.mark.asyncio
async def test_release_requeues_on_the_live_lease_and_revokes_the_claim(monkeypatch):
    log = _patch(monkeypatch, rowcount=1)
    assert await daemons.release_daemon_claim("d-1", "t-1", "l-1") is True
    ticket_sql, ticket_params = log[0]
    assert "status='queued'" in ticket_sql
    assert "attempts=GREATEST(attempts - 1, 0)" in ticket_sql
    assert "worker_lease_id=NULL" in ticket_sql
    # Fenced on this daemon's live lease of a running Ticket.
    for fence in (
        "worker_daemon_id=%s",
        "status='running'",
        "worker_lease_id=%s",
        "worker_lease_expires_at>clock_timestamp()",
    ):
        assert fence in ticket_sql.split("WHERE", 1)[1]
    assert ticket_params == ("t-1", "d-1", "l-1")
    # A claim with a report receipt reached the daemon: never given back.
    assert "report_result IS NOT NULL" in ticket_sql.split("NOT EXISTS", 1)[1]
    assert "worker_lease_heartbeat_at IS NOT DISTINCT FROM" not in ticket_sql
    claim_sql, claim_params = log[1]
    assert "UPDATE autonomy_daemon_claims SET revoked_at=NOW()" in claim_sql
    assert "report_result IS NULL" in claim_sql
    assert claim_params == ("l-1", "d-1", "t-1")


@pytest.mark.asyncio
async def test_release_of_a_moved_claim_writes_nothing_else(monkeypatch):
    log = _patch(monkeypatch, rowcount=0)
    assert await daemons.release_daemon_claim("d-1", "t-1", "l-1") is False
    assert len(log) == 1


@pytest.mark.asyncio
async def test_release_is_fenced_on_the_heartbeat_read_with_the_claim(monkeypatch):
    log = _patch(monkeypatch, rowcount=1)
    assert await daemons.release_daemon_claim("d-1", "t-1", "l-1", heartbeat_at="hb") is True
    ticket_sql, ticket_params = log[0]
    # A heartbeat since the claim means the daemon holds it.
    assert "worker_lease_heartbeat_at IS NOT DISTINCT FROM %s" in ticket_sql
    assert ticket_params == ("t-1", "d-1", "l-1", "hb")
