"""Lease-fence lock queueing on a real PostgreSQL (migration 140 applied).

The fence must let a revoke through even while appends keep overlapping on one
claim. That rests on Postgres queueing an exclusive row lock first in, first out
(a share lock does not queue behind a waiting writer), so it is pinned here
rather than in a mock. Run like ``test_daemon_output_postgres.py``.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.integration import test_daemon_output_postgres as base  # noqa: E402

pytestmark = pytest.mark.integration

# The scratch-database fixture and helpers are shared, not copied.
pg = base.pg
_dsn = base._dsn
_post = base._post


@pytest.fixture
def revoker(pg, monkeypatch):
    """``revoke_lease`` on the same autocommit connections the fixture uses."""
    from src.server.database.autonomy import daemons as daemons_db

    @asynccontextmanager
    async def connection(conn=None):
        async with await psycopg.AsyncConnection.connect(_dsn(), autocommit=True) as c:
            yield c

    monkeypatch.setattr(daemons_db, "get_db_connection", connection)

    async def revoke() -> str:
        async with pg.probe.cursor() as cur:
            await cur.execute(
                "SELECT user_id FROM autonomy_daemons WHERE daemon_id=%s", (pg.daemon,)
            )
            user_id = (await cur.fetchone())[0]
        return await daemons_db.revoke_lease(user_id, pg.lease)

    return revoke


@pytest.mark.asyncio
async def test_a_pending_revoke_queues_ahead_of_later_appends(pg, revoker):
    from src.server.database.autonomy import daemon_output as output_db

    first = await _post(pg, [{"kind": "text", "text": "clean\n"}])
    holder = await psycopg.AsyncConnection.connect(_dsn())  # explicit transaction
    late = await psycopg.AsyncConnection.connect(_dsn())
    try:
        # An append in flight: it holds the claim row until it commits.
        await holder.execute(
            output_db.CLAIM_LOCK_SQL, (pg.daemon, pg.ticket, pg.lease)
        )
        revoke = asyncio.create_task(revoker())
        await asyncio.sleep(0.3)
        assert not revoke.done(), "the revoke must wait for the append in flight"
        # A later append queues behind the revoke instead of joining the
        # holder, which is what keeps a continuous stream from starving it.
        await late.execute("SET lock_timeout = '300ms'")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            await late.execute(
                output_db.CLAIM_LOCK_SQL, (pg.daemon, pg.ticket, pg.lease)
            )
        await late.rollback()
        await holder.commit()
        assert await asyncio.wait_for(revoke, 2) == "revoked"
    finally:
        await holder.close()
        await late.close()
    refused = await _post(
        pg, [{"kind": "text", "text": "late\n"}], epoch=first.epoch, seq=2
    )
    assert refused.code == "stale_lease" and refused.stored is False


@pytest.mark.asyncio
async def test_a_revoke_completes_while_appends_keep_overlapping(
    pg, revoker, monkeypatch
):
    from psycopg_pool import AsyncConnectionPool

    from src.server.database.autonomy import daemon_output as output_db

    # A pool, as in production: with a connection per call the appends rarely
    # overlap enough to starve a revoke that is queued behind share locks.
    pool = AsyncConnectionPool(
        _dsn(),
        min_size=8,
        max_size=8,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": None},
    )
    await pool.open(wait=True, timeout=10)

    @asynccontextmanager
    async def pooled(conn=None):
        async with pool.connection(timeout=30) as c:
            yield c

    monkeypatch.setattr(output_db, "get_db_connection", pooled)
    state = {"epoch": None, "seq": 0}

    async def appender() -> str:
        # Each call takes the fence, so six loops keep holders overlapping.
        while True:
            state["seq"] += 1
            result = await _post(
                pg,
                [{"kind": "text", "text": "x\n"}],
                epoch=state["epoch"],
                seq=state["seq"] if state["epoch"] else 1,
            )
            if result.code == "stale_lease":
                return result.code
            if result.epoch and state["epoch"] is None:
                state["epoch"] = result.epoch

    loops = [asyncio.create_task(appender()) for _ in range(6)]
    try:
        await asyncio.sleep(1.0)
        assert await asyncio.wait_for(revoker(), 5) == "revoked"
        codes = await asyncio.wait_for(asyncio.gather(*loops), 10)
    finally:
        for loop in loops:
            loop.cancel()
        await pool.close()
    assert codes == ["stale_lease"] * 6
    async with pg.probe.cursor() as cur:
        await cur.execute("SHOW track_commit_timestamp")
        if (await cur.fetchone())[0] != "on":
            return  # the fence check below needs commit timestamps
        await cur.execute(
            "SELECT count(*) FROM autonomy_daemon_output_events e, "
            "autonomy_daemon_claims c WHERE e.ticket_id=c.ticket_id AND "
            "c.lease_id=%s AND pg_xact_commit_timestamp(e.xmin) > "
            "pg_xact_commit_timestamp(c.xmin)",
            (pg.lease,),
        )
        assert (await cur.fetchone())[0] == 0, "an event committed after the revoke"
