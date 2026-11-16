"""Daemon live output against a real PostgreSQL (migration 140 applied).

Run with ``TEST_DB_HOST/TEST_DB_PORT/TEST_DB_NAME/TEST_DB_USER/TEST_DB_PASSWORD``
pointing at a migrated scratch database and ``pytest -m integration``. The default
suite deselects it.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio

from tests.integration.db_guard import require_test_database_name

psycopg = pytest.importorskip("psycopg")

pytestmark = pytest.mark.integration


def _dsn() -> str:
    return (
        f"host={os.getenv('TEST_DB_HOST', 'localhost')} "
        f"port={os.getenv('TEST_DB_PORT', '5432')} "
        f"dbname={require_test_database_name()} "
        f"user={os.getenv('TEST_DB_USER', 'postgres')} "
        f"password={os.getenv('TEST_DB_PASSWORD', 'postgres')} "
        f"sslmode={os.getenv('TEST_DB_SSLMODE', 'prefer')}"
    )


@pytest_asyncio.fixture
async def pg(monkeypatch):
    from src.server.database.autonomy import daemon_output as output_db
    from src.server.services.autonomy import daemon_output as store

    try:
        probe = await psycopg.AsyncConnection.connect(_dsn(), autocommit=True)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"postgres unavailable: {exc}")
    async with probe.cursor() as cur:
        await cur.execute("SELECT to_regclass('autonomy_daemon_output_sessions')")
        if (await cur.fetchone())[0] is None:
            pytest.skip("migration 140 is not applied to this database")
    daemon_id, ticket_id, lease_id = (str(uuid.uuid4()) for _ in range(3))
    user = f"it-{uuid.uuid4().hex[:8]}"
    async with probe.cursor() as cur:
        await cur.execute(
            "INSERT INTO autonomy_daemons (daemon_id, user_id, name, token_hash, provider) "
            "VALUES (%s, %s, 'it', %s, 'codex')",
            (daemon_id, user, uuid.uuid4().hex),
        )
        await cur.execute(
            "INSERT INTO autonomy_tickets (ticket_id, user_id, workspace_id, status, "
            "worker_daemon_id, worker_lease_id, worker_lease_expires_at) "
            "VALUES (%s, %s, 'ws', 'running', %s, %s, NOW() + INTERVAL '1 hour')",
            (ticket_id, user, daemon_id, lease_id),
        )
        await cur.execute(
            "INSERT INTO autonomy_daemon_claims (claim_id, daemon_id, ticket_id, lease_id) "
            "VALUES (%s, %s, %s, %s)",
            (str(uuid.uuid4()), daemon_id, ticket_id, lease_id),
        )

    @asynccontextmanager
    async def connection(conn=None):
        async with await psycopg.AsyncConnection.connect(_dsn(), autocommit=True) as c:
            yield c

    monkeypatch.setattr(output_db, "get_db_connection", connection)
    try:
        yield SimpleNamespace(store=store, daemon=daemon_id, ticket=ticket_id, lease=lease_id, probe=probe)
    finally:
        async with probe.cursor() as cur:
            await cur.execute("DELETE FROM autonomy_tickets WHERE ticket_id=%s", (ticket_id,))
            await cur.execute("DELETE FROM autonomy_daemons WHERE daemon_id=%s", (daemon_id,))
        await probe.close()


async def _post(pg, events, *, epoch=None, seq=1, lease=None):
    return await pg.store.append_output(
        pg.daemon, pg.ticket, lease or pg.lease, "run", epoch, seq, events
    )


@pytest.mark.asyncio
async def test_fence_taint_and_reads_on_real_postgres(pg):
    first = await _post(pg, [{"kind": "text", "text": "hello there\n"}])
    e = first.epoch  # drawn from the database-wide sequence
    assert (first.stored, first.head) == (True, f"{e}.1")
    nxt = await _post(pg, [{"kind": "text", "text": "prefix Bearer \n"}], epoch=e, seq=2)
    assert nxt.stored is True
    bad = await _post(pg, [{"kind": "text", "text": "abcdef0123456789xyz\n"}], epoch=e, seq=3)
    assert bad.stored is False and bad.tainted is True
    replay = await _post(pg, [{"kind": "text", "text": "x\n"}], epoch=e, seq=3)
    assert replay.code == "duplicate_output_batch"
    page = await pg.store.read_output(pg.ticket, None)
    kinds = [e["data"]["kind"] for e in page.events]
    assert kinds == ["text", "text", "output_reset"]
    assert "abcdef" not in "".join(str(e["data"].get("text", "")) for e in page.events)
    realloc = await _post(pg, [{"kind": "text", "text": "again\n"}], epoch=None, seq=1)
    assert realloc.epoch > e and realloc.stored is True
    stale = await pg.store.read_output(pg.ticket, page.cursor)
    assert stale.reset == "new_epoch" and stale.epoch == realloc.epoch


@pytest.mark.asyncio
async def test_concurrent_writers_serialize_on_the_row_lock(pg):
    first = await _post(pg, [{"kind": "text", "text": "start\n"}])
    results = await asyncio.gather(
        *[
            _post(pg, [{"kind": "text", "text": f"w{n}\n"}], epoch=first.epoch, seq=2)
            for n in range(10)
        ]
    )
    assert sum(1 for r in results if r.stored) == 1
    assert sum(1 for r in results if r.code == "duplicate_output_batch") == 9
    async with pg.probe.cursor() as cur:
        await cur.execute(
            "SELECT array_agg(seq ORDER BY seq) FROM autonomy_daemon_output_events "
            "WHERE ticket_id=%s",
            (pg.ticket,),
        )
        assert (await cur.fetchone())[0] == [1, 2]


@pytest.mark.asyncio
async def test_revoked_lease_and_cascade_delete(pg):
    other = str(uuid.uuid4())
    refused = await _post(pg, [{"kind": "text", "text": "x\n"}], lease=other)
    assert refused.code == "stale_lease"
    await _post(pg, [{"kind": "text", "text": "x\n"}])
    async with pg.probe.cursor() as cur:
        await cur.execute("DELETE FROM autonomy_tickets WHERE ticket_id=%s", (pg.ticket,))
        await cur.execute(
            "SELECT count(*) FROM autonomy_daemon_output_events WHERE ticket_id=%s",
            (pg.ticket,),
        )
        assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_epoch_is_never_reissued_after_the_session_is_deleted(pg):
    first = await _post(pg, [{"kind": "text", "text": "prefix Bearer \n"}])
    watcher = await pg.store.read_output(pg.ticket, None)
    async with pg.probe.cursor() as cur:
        await cur.execute(
            "DELETE FROM autonomy_daemon_output_sessions WHERE ticket_id=%s",
            (pg.ticket,),
        )
    again = await _post(pg, [{"kind": "text", "text": "tail\n"}])
    assert again.epoch > first.epoch
    page = await pg.store.read_output(pg.ticket, watcher.cursor)
    assert page.reset == "new_epoch"


@pytest.mark.asyncio
async def test_a_revoked_claim_stops_writes_and_a_daemon_stop_taints(pg):
    first = await _post(pg, [{"kind": "text", "text": "clean\n"}])
    stop = await _post(pg, [{"kind": "stop"}], epoch=first.epoch, seq=2)
    assert stop.tainted is True
    async with pg.probe.cursor() as cur:
        await cur.execute(
            "UPDATE autonomy_daemon_claims SET revoked_at=NOW() WHERE ticket_id=%s",
            (pg.ticket,),
        )
    refused = await _post(pg, [{"kind": "tool", "tools": ["bash"]}], epoch=first.epoch, seq=3)
    assert refused.code == "stale_lease"


@pytest.mark.asyncio
async def test_an_in_flight_revoke_blocks_the_append_and_then_refuses_it(pg):
    first = await _post(pg, [{"kind": "text", "text": "clean\n"}])
    revoker = await psycopg.AsyncConnection.connect(_dsn())  # explicit transaction
    try:
        await revoker.execute(
            "UPDATE autonomy_daemon_claims SET revoked_at=NOW() WHERE ticket_id=%s",
            (pg.ticket,),
        )
        task = asyncio.create_task(
            _post(pg, [{"kind": "text", "text": "late\n"}], epoch=first.epoch, seq=2)
        )
        await asyncio.sleep(0.4)
        assert not task.done(), "the append must wait for the uncommitted revoke"
        await revoker.commit()
        result = await asyncio.wait_for(task, 5)
    finally:
        await revoker.close()
    assert result.code == "stale_lease" and result.stored is False
    page = await pg.store.read_output(pg.ticket, None)
    assert [e["data"]["text"] for e in page.events] == ["clean\n"]


@pytest.mark.asyncio
async def test_expiry_removes_whole_old_logs_and_never_punches_holes(pg):
    from src.server.database.autonomy import daemon_output as output_db

    await _post(pg, [{"kind": "text", "text": "keep me\n"}])
    async with pg.probe.cursor() as cur:
        rows = [
            # epoch 90: partly old, partly fresh -> must stay intact
            (90, 1, "2 days"), (90, 2, "2 days"), (90, 3, "0 days"),
            # epoch 91: entirely old -> removed as a whole
            (91, 1, "2 days"), (91, 2, "3 days"),
        ]
        for epoch, seq, age in rows:
            await cur.execute(
                "INSERT INTO autonomy_daemon_output_events "
                "(ticket_id, epoch, seq, data, created_at) "
                "VALUES (%s, %s, %s, '{}'::jsonb, NOW() - %s::interval)",
                (pg.ticket, epoch, seq, age),
            )
    await output_db.expire_old_logs(50)
    async with pg.probe.cursor() as cur:
        await cur.execute(
            "SELECT epoch, array_agg(seq ORDER BY seq) FROM autonomy_daemon_output_events "
            "WHERE ticket_id=%s GROUP BY epoch ORDER BY epoch",
            (pg.ticket,),
        )
        remaining = {epoch: seqs for epoch, seqs in await cur.fetchall()}
    assert remaining[90] == [1, 2, 3]
    assert 91 not in remaining
