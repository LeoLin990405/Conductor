"""Cloud launches and daemon claims share one Worker slot cap (real PostgreSQL).

Run with ``TEST_DB_HOST/TEST_DB_PORT/TEST_DB_NAME/TEST_DB_USER/TEST_DB_PASSWORD``
pointing at a migrated scratch database and ``pytest -m integration``. The default
suite deselects it.
The slot count is global, so every cap below is relative to what already runs in
that database. Only the process edges are faked: the Worker thread service and
the container runtime. The launch, the claim, the slot guard and the effect
queue are the product code on real rows.
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

_RUNNING = (
    "SELECT COUNT(*) FROM autonomy_tickets WHERE status = 'running' "
    "AND mandate->>'orchestration_only' IS DISTINCT FROM 'true'"
)


def _dsn() -> str:
    return (
        f"host={os.getenv('TEST_DB_HOST', 'localhost')} "
        f"port={os.getenv('TEST_DB_PORT', '5432')} "
        f"dbname={require_test_database_name()} "
        f"user={os.getenv('TEST_DB_USER', 'postgres')} "
        f"password={os.getenv('TEST_DB_PASSWORD', 'postgres')} "
        f"sslmode={os.getenv('TEST_DB_SSLMODE', 'prefer')}"
    )


class _Threads:
    """The Worker thread service, which the launch reaches over HTTP."""

    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id
        self.created: list[str] = []
        self.cancelled: list[str] = []
        self._index = 0

    async def create_thread(self, **_kwargs) -> str:
        self._index += 1
        index = self._index
        thread_id = str(uuid.uuid4())
        async with await psycopg.AsyncConnection.connect(
            _dsn(), autocommit=True
        ) as conn:
            await conn.execute(
                "INSERT INTO conversation_threads (conversation_thread_id, "
                "workspace_id, msg_type, current_status, thread_index) "
                "VALUES (%s, %s, 'ptc', 'in_progress', %s)",
                (thread_id, self.workspace_id, index),
            )
        self.created.append(thread_id)
        return thread_id

    async def cancel_thread(self, *, thread_id: str, **_kwargs) -> bool:
        self.cancelled.append(thread_id)
        return True


class _Containers:
    """The isolated runtime: a Worker stays in its container until released."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.release = asyncio.Event()

    async def launch(self, ticket: dict, _prompt: str) -> dict:
        self.started.append(str(ticket["ticket_id"]))
        await self.release.wait()
        return {"state": "ready", "summary": "done", "question": ""}


@pytest_asyncio.fixture
async def pg(monkeypatch):
    from src.server.database.autonomy import daemons as daemons_db
    from src.server.database.autonomy import effects as effects_db
    from src.server.database.autonomy import tickets as tickets_db
    from src.server.services.autonomy import cloud_runtime, worker

    try:
        probe = await psycopg.AsyncConnection.connect(_dsn(), autocommit=True)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"postgres unavailable: {exc}")
    async with probe.cursor() as cur:
        await cur.execute("SELECT to_regclass('autonomy_effects')")
        if (await cur.fetchone())[0] is None:
            pytest.skip("the autonomy schema is not applied to this database")

    async def clear_autonomy_rows() -> None:
        """Remove every autonomy row an earlier test in this session left behind.

        The effect runner claims any due effect, regardless of user, so a pending
        launch effect from an earlier file occupies one of this test's execution
        slots and the "two launches" predicate never becomes true. These tests
        only reason about the rows they create, so start and finish without
        leftovers.
        """
        async with probe.cursor() as cur:
            await cur.execute("DELETE FROM autonomy_effects")
            await cur.execute("DELETE FROM autonomy_tickets")
            await cur.execute("DELETE FROM autonomy_daemons")

    await clear_autonomy_rows()

    user = f"it-{uuid.uuid4().hex[:8]}"
    workspace_id = str(uuid.uuid4())
    daemon_id = str(uuid.uuid4())
    async with probe.cursor() as cur:
        await cur.execute("INSERT INTO users (user_id) VALUES (%s)", (user,))
        await cur.execute(
            "INSERT INTO workspaces (workspace_id, user_id, name) VALUES (%s, %s, 'it')",
            (workspace_id, user),
        )
        await cur.execute(
            "INSERT INTO autonomy_daemons (daemon_id, user_id, name, token_hash, "
            "provider, max_concurrency) VALUES (%s, %s, 'it', %s, 'codex', 32)",
            (daemon_id, user, uuid.uuid4().hex),
        )

    @asynccontextmanager
    async def connection(conn=None):
        async with await psycopg.AsyncConnection.connect(_dsn(), autocommit=True) as c:
            yield c

    for module in (tickets_db, daemons_db, effects_db, worker):
        monkeypatch.setattr(module, "get_db_connection", connection)
    # The recovery sweep imports the pool's accessor when it runs.
    monkeypatch.setattr("src.server.database.pool.get_db_connection", connection)

    threads = _Threads(workspace_id)
    containers = _Containers()
    monkeypatch.setattr(worker, "ProjectDispatcher", lambda: threads)
    monkeypatch.setattr(cloud_runtime, "launch_cloud_worker", containers.launch)

    async def allowed() -> bool:
        return True

    monkeypatch.setattr(cloud_runtime, "cloud_worker_launch_allowed_async", allowed)

    async def prompt(_ticket):
        return "prompt", ""

    monkeypatch.setattr(worker, "_cloud_worker_prompt", prompt)

    async def settle(**_kwargs):
        return None

    monkeypatch.setattr(worker, "_apply_isolated_worker_report", settle)

    async def count() -> int:
        async with probe.cursor() as cur:
            await cur.execute(_RUNNING)
            return int((await cur.fetchone())[0])

    async def ticket(status: str = "queued", *, daemon: bool = False) -> str:
        ticket_id = str(uuid.uuid4())
        async with probe.cursor() as cur:
            if status == "running":
                # A live daemon lease, so no recovery pass hands it back.
                await cur.execute(
                    "INSERT INTO autonomy_tickets (ticket_id, user_id, workspace_id, "
                    "status, worker_daemon_id, worker_lease_id, worker_lease_expires_at) "
                    "VALUES (%s, %s, 'ws', 'running', %s, %s, NOW() + INTERVAL '1 hour')",
                    (ticket_id, user, daemon_id, str(uuid.uuid4())),
                )
            else:
                await cur.execute(
                    "INSERT INTO autonomy_tickets (ticket_id, user_id, workspace_id, "
                    "status, worker_provider, worker_daemon_id) "
                    "VALUES (%s, %s, 'ws', 'queued', 'codex', %s)",
                    (ticket_id, user, daemon_id if daemon else None),
                )
        return ticket_id

    async def row(ticket_id: str) -> dict:
        async with probe.cursor() as cur:
            await cur.execute(
                "SELECT status, worker_thread_id, worker_run_id "
                "FROM autonomy_tickets WHERE ticket_id = %s",
                (ticket_id,),
            )
            status, thread_id, run_id = await cur.fetchone()
        return {"status": status, "thread": thread_id, "run": run_id}

    async def _claimed(where: str, params: tuple):
        """Lease one effect row the way the runner does, and hand it over."""
        from src.server.services.autonomy.models import EffectRecord

        async with probe.cursor() as cur:
            await cur.execute(
                "UPDATE autonomy_effects SET status = 'claimed', attempts = 1, "
                "lease_until = NOW() + INTERVAL '1 hour' "
                f"WHERE kind = 'launch_worker' AND status = 'pending' AND {where} "
                "RETURNING effect_id, payload",
                params,
            )
            rows = await cur.fetchall()
        assert len(rows) == 1, f"expected one pending launch effect, found {len(rows)}"
        effect_id, payload = rows[0]
        return EffectRecord(
            effect_id=str(effect_id), user_id=user, kind="launch_worker",
            idempotency_key="", payload=payload, attempts=1,
        )

    async def launch_effect(ticket_id: str):
        """A launch effect for the ticket, leased as the runner would lease it."""
        key = f"it:{uuid.uuid4()}"
        await effects_db.enqueue_effect(
            user_id=user, kind="launch_worker", idempotency_key=key,
            payload={"ticket_id": ticket_id},
        )
        return await _claimed("idempotency_key = %s", (key,))

    async def launch_effect_pending(ticket_id: str) -> None:
        """A launch effect the way ticket creation enqueues it: pending, unleased."""
        await effects_db.enqueue_effect(
            user_id=user, kind="launch_worker",
            idempotency_key=f"autonomy:launch:{ticket_id}",
            payload={"ticket_id": ticket_id},
        )

    async def cap_of(extra: int) -> int:
        cap = await count() + extra
        monkeypatch.setenv("AUTONOMY_MAX_RUNNING_WORKERS", str(cap))
        return cap

    async def lease_due_launches(ticket_id: str) -> list:
        """Make the ticket's pending launch effects due and lease them all at
        once, as one runner pass would."""
        from src.server.services.autonomy.models import EffectRecord

        async with probe.cursor() as cur:
            await cur.execute(
                "UPDATE autonomy_effects SET due_at = NOW() WHERE user_id = %s "
                "AND kind = 'launch_worker' AND status = 'pending' "
                "AND payload->>'ticket_id' = %s",
                (user, ticket_id),
            )
            await cur.execute(
                "UPDATE autonomy_effects SET status = 'claimed', "
                "attempts = attempts + 1, lease_until = NOW() + INTERVAL '1 hour' "
                "WHERE user_id = %s AND kind = 'launch_worker' "
                "AND status = 'pending' AND payload->>'ticket_id' = %s "
                "RETURNING effect_id, idempotency_key, payload, attempts",
                (user, ticket_id),
            )
            rows = await cur.fetchall()
        return [
            EffectRecord(
                effect_id=str(effect_id), user_id=user, kind="launch_worker",
                idempotency_key=key, payload=payload, attempts=attempts,
            )
            for effect_id, key, payload, attempts in rows
        ]

    async def execute(effect) -> None:
        """Run a leased effect through the runner's own settlement: ack, nack or defer."""
        from src.server.services.autonomy.effects import AutonomyEffectRunner

        runner = AutonomyEffectRunner()
        runner.register("launch_worker", worker.launch_worker_effect)
        await runner._execute(effect)

    async def run_due_launches(ticket_id: str) -> int:
        effects = await lease_due_launches(ticket_id)
        await asyncio.gather(*(execute(effect) for effect in effects))
        return len(effects)

    async def launch_effects(ticket_id: str) -> list[tuple]:
        """Every launch effect row of the ticket: (status, attempts, due later)."""
        async with probe.cursor() as cur:
            await cur.execute(
                "SELECT status, attempts, due_at > NOW() + INTERVAL '5 seconds' "
                "FROM autonomy_effects WHERE user_id = %s "
                "AND kind = 'launch_worker' AND payload->>'ticket_id' = %s "
                "ORDER BY created_at, effect_id",
                (user, ticket_id),
            )
            return await cur.fetchall()

    try:
        yield SimpleNamespace(
            probe=probe, worker=worker, tickets=tickets_db, daemon=daemon_id,
            threads=threads, containers=containers, count=count, ticket=ticket,
            row=row, launch_effect=launch_effect, cap_of=cap_of,
            lease_due_launches=lease_due_launches, execute=execute,
            run_due_launches=run_due_launches, launch_effects=launch_effects,
            launch_effect_pending=launch_effect_pending, user=user,
        )
    finally:
        containers.release.set()
        await asyncio.sleep(0.05)
        await clear_autonomy_rows()
        async with probe.cursor() as cur:
            await cur.execute("DELETE FROM users WHERE user_id = %s", (user,))
        await probe.close()


async def _defers(pg, effect) -> None:
    """The launch must hand back its effect at once, not run a Worker."""
    task = asyncio.create_task(pg.execute(effect))
    done, _ = await asyncio.wait({task}, timeout=10)
    if not done:
        pg.containers.release.set()
        await task
        pytest.fail("the launch started a Worker although no slot was free")
    await task


async def _until(predicate, *, seconds: float = 20.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not await predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out waiting"
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_a_full_pool_keeps_a_cloud_launch_queued_and_defers_its_effect(pg):
    await pg.cap_of(1)
    await pg.ticket("running")  # the only slot
    cloud = await pg.ticket()
    effect = await pg.launch_effect(cloud)

    await _defers(pg, effect)

    assert (await pg.row(cloud))["status"] == "queued"
    assert pg.threads.created == [] and pg.containers.started == []
    # The same row waits: pending again, due later, its attempt given back.
    assert await pg.launch_effects(cloud) == [("pending", 0, True)]


@pytest.mark.asyncio
async def test_a_free_slot_launches_the_cloud_worker_the_deferred_effect_started(pg):
    await pg.cap_of(1)
    occupant = await pg.ticket("running")
    cloud = await pg.ticket()
    await _defers(pg, await pg.launch_effect(cloud))
    assert (await pg.row(cloud))["status"] == "queued"

    # The occupant ends. The deferred effect is due, claims the freed slot, and
    # the Worker reaches its container.
    async with pg.probe.cursor() as cur:
        await cur.execute(
            "UPDATE autonomy_tickets SET status = 'completed' WHERE ticket_id = %s",
            (occupant,),
        )
    (retry,) = await pg.lease_due_launches(cloud)
    run = asyncio.create_task(pg.execute(retry))

    async def in_container() -> bool:
        return cloud in pg.containers.started

    await _until(in_container)
    launched = await pg.row(cloud)
    assert launched["status"] == "running"
    assert launched["thread"] is not None and launched["run"] is not None
    assert await pg.count() == 1
    pg.containers.release.set()
    await asyncio.wait_for(run, 20)
    assert [row[0] for row in await pg.launch_effects(cloud)] == ["done"]


@pytest.mark.asyncio
async def test_a_slot_lost_between_the_check_and_the_write_defers_and_cancels_the_thread(
    pg, monkeypatch
):
    """The pre-check is a courtesy; the write is the gate. Take the last slot
    in the gap and the launch must refuse, clean up and defer, not fail."""
    await pg.cap_of(1)
    cloud = await pg.ticket()
    effect = await pg.launch_effect(cloud)
    real = pg.worker._worker_slot_available

    async def check_then_lose_the_slot() -> bool:
        free = await real()
        await pg.ticket("running")
        return free

    monkeypatch.setattr(pg.worker, "_worker_slot_available", check_then_lose_the_slot)

    await _defers(pg, effect)

    assert (await pg.row(cloud))["status"] == "queued"
    assert pg.containers.started == []
    # The thread that was created for the refused write is cancelled, not leaked.
    assert pg.threads.cancelled == pg.threads.created and len(pg.threads.created) == 1
    assert await pg.launch_effects(cloud) == [("pending", 0, True)]


@pytest.mark.asyncio
@pytest.mark.parametrize("round_number", range(4))
async def test_concurrent_launches_and_claims_never_exceed_the_cap(pg, round_number):
    cap = await pg.cap_of(3)
    clouds = [await pg.ticket() for _ in range(6)]
    for _ in range(6):
        await pg.ticket(daemon=True)
    effects = {cloud: await pg.launch_effect(cloud) for cloud in clouds}

    peak = 0
    watching = True

    async def watch() -> None:
        nonlocal peak
        while watching:
            peak = max(peak, await pg.count())
            await asyncio.sleep(0)

    watcher = asyncio.create_task(watch())
    launches = {
        cloud: asyncio.create_task(pg.execute(effects[cloud])) for cloud in clouds
    }
    claims = asyncio.gather(
        *(pg.worker.claim_daemon_ticket(pg.daemon) for _ in range(6))
    )

    async def settled() -> bool:
        for cloud, task in launches.items():
            if not task.done() and (await pg.row(cloud))["status"] != "running":
                return False
        return claims.done()

    await _until(settled)
    watching = False
    await watcher

    assert peak <= cap, f"{peak} Workers ran at once against a cap of {cap}"
    assert await pg.count() <= cap

    # Contention may have turned a contender away with a slot still free; the
    # deferred effects are what take it. Run each one again, one at a time.
    for cloud in clouds:
        if not launches[cloud].done() or (await pg.row(cloud))["status"] != "queued":
            continue
        (retry,) = await pg.lease_due_launches(cloud)
        launches[cloud] = asyncio.create_task(pg.execute(retry))
        await _until(settled)
    while await pg.worker.claim_daemon_ticket(pg.daemon):
        pass

    assert await pg.count() == cap, "the free slots were not all taken"
    in_containers = [
        cloud for cloud in clouds if (await pg.row(cloud))["status"] == "running"
    ]
    assert sorted(in_containers) == sorted(pg.containers.started)

    pg.containers.release.set()
    await asyncio.wait_for(
        asyncio.gather(*launches.values(), return_exceptions=True), 20
    )


@pytest.mark.asyncio
async def test_every_launch_effect_of_a_waiting_ticket_merges_into_one_retry(pg):
    """A queued Ticket normally has several launch effects: the one made with
    it, the recovery sweep's, one per owner requeue. Waiting for a slot must not
    turn each of them into a retry of its own, or into a new row per retry."""
    from src.server.services.autonomy import executor, ownership

    await pg.cap_of(1)
    await pg.ticket("running")  # the only slot
    cloud = await pg.ticket()
    await pg.launch_effect_pending(cloud)
    assert await executor.ensure_launch_effects() >= 1
    for revision in range(3):
        await ownership._reenqueue_launch(
            {"ticket_id": cloud, "user_id": pg.user, "attempts": 0, "revision": revision}
        )
    assert len(await pg.launch_effects(cloud)) == 5

    for _ in range(4):
        assert await pg.run_due_launches(cloud) >= 1

    effects = await pg.launch_effects(cloud)
    assert len(effects) == 5, "waiting must not add rows"
    assert [row[0] for row in effects].count("pending") == 1, (
        "one launch effect keeps waiting for the Ticket"
    )
    assert (await pg.row(cloud))["status"] == "queued"

    # The one left waiting is the one that launches when a slot frees.
    async with pg.probe.cursor() as cur:
        await cur.execute(
            "UPDATE autonomy_tickets SET status = 'completed' "
            "WHERE user_id = %s AND status = 'running'",
            (pg.user,),
        )
    (survivor,) = await pg.lease_due_launches(cloud)
    run = asyncio.create_task(pg.execute(survivor))

    async def in_container() -> bool:
        return cloud in pg.containers.started

    await _until(in_container)
    assert (await pg.row(cloud))["status"] == "running"
    pg.containers.release.set()
    await asyncio.wait_for(run, 20)


@pytest.mark.asyncio
async def test_an_older_effect_still_running_does_not_end_the_newest_one(pg):
    """A requeue makes a new launch effect while the one for the previous run is
    still tearing down. The old one must not be taken for the waiter: the new
    one is the one that serves the Ticket's latest state, and has to keep going."""
    await pg.cap_of(1)
    await pg.ticket("running")
    cloud = await pg.ticket()
    await pg.launch_effect(cloud)  # leased and left claimed: the old run's effect
    await pg.launch_effect_pending(cloud)
    (newest,) = await pg.lease_due_launches(cloud)

    await _defers(pg, newest)

    assert await pg.launch_effects(cloud) == [
        ("claimed", 1, False),
        ("pending", 0, True),
    ]


@pytest.mark.asyncio
async def test_only_the_holder_of_an_effect_can_defer_it(pg):
    from src.server.database.autonomy import effects as effects_db

    cloud = await pg.ticket()
    effect = await pg.launch_effect(cloud)  # claimed by attempt 1

    assert not await effects_db.defer_effect(
        effect_id=effect.effect_id, attempts=effect.attempts + 1, delay_seconds=10
    )
    assert await pg.launch_effects(cloud) == [("claimed", 1, False)]

    assert await effects_db.defer_effect(
        effect_id=effect.effect_id, attempts=effect.attempts, delay_seconds=10
    )
    assert await pg.launch_effects(cloud) == [("pending", 0, True)]
    # Deferred, it is no longer claimed, so a second deferral changes nothing.
    assert not await effects_db.defer_effect(
        effect_id=effect.effect_id, attempts=effect.attempts, delay_seconds=10
    )


@pytest.mark.asyncio
async def test_waiting_for_a_slot_never_spends_the_effects_attempts(pg):
    """The effect's own attempts are for failures. A launch that waits for a
    long Worker is not one, however many times it looks again."""
    await pg.cap_of(1)
    await pg.ticket("running")
    cloud = await pg.ticket()
    await pg.launch_effect_pending(cloud)

    for _ in range(8):  # more than the six attempts an effect has
        assert await pg.run_due_launches(cloud) == 1

    assert await pg.launch_effects(cloud) == [("pending", 0, True)]
    assert (await pg.row(cloud))["status"] == "queued"


@pytest.mark.asyncio
async def test_a_busy_guard_is_waited_for_a_moment_not_refused(pg):
    """A daemon polling hard holds the guard for milliseconds at a time. A
    launch must not be turned away by that, or it starves while slots are free."""
    from src.server.database.autonomy import tickets as tickets_db

    await pg.cap_of(50)
    queued = [await pg.ticket() for _ in range(12)]
    stop = False

    async def hammer() -> None:
        """A daemon claim loop: try the guard, hold it a moment, try again at once."""
        while not stop:
            async with pg.worker._launch_lock() as acquired:
                if acquired:
                    await asyncio.sleep(0.015)
            await asyncio.sleep(0)

    hammers = [asyncio.create_task(hammer()) for _ in range(4)]
    try:
        for ticket_id in queued:
            updated = await tickets_db.update_ticket(
                ticket_id, expected_status="queued", status="running",
                slot_limit=50,
            )
            assert updated is not None and updated["status"] == "running"
    finally:
        stop = True
        await asyncio.gather(*hammers)


@pytest.mark.asyncio
async def test_a_guard_held_too_long_refuses_after_a_bounded_wait(pg):
    from src.server.database.autonomy import tickets as tickets_db

    await pg.cap_of(5)
    cloud = await pg.ticket()
    holder = await psycopg.AsyncConnection.connect(_dsn(), autocommit=True)
    try:
        await holder.execute("SELECT pg_advisory_lock(%s)", (tickets_db.WORKER_SLOT_LOCK,))
        started = asyncio.get_running_loop().time()
        with pytest.raises(tickets_db.WorkerSlotsFull):
            await asyncio.wait_for(
                tickets_db.update_ticket(
                    cloud, expected_status="queued", status="running", slot_limit=5
                ),
                20,
            )
        waited = asyncio.get_running_loop().time() - started
    finally:
        await holder.close()
    assert 0.3 < waited < 10, f"waited {waited:.2f}s"
    assert (await pg.row(cloud))["status"] == "queued"


@pytest.mark.asyncio
async def test_a_stuck_row_lock_does_not_freeze_the_slot_guard(pg):
    """The guard is taken before the write waits for its row. Another writer
    sitting on that Ticket's row must only delay this launch, boundedly, and
    must not keep every other claim and launch out while it does."""
    from src.server.database.autonomy import tickets as tickets_db

    await pg.cap_of(5)
    stuck, other = await pg.ticket(), await pg.ticket()
    writer = await psycopg.AsyncConnection.connect(_dsn(), autocommit=False)
    try:
        await writer.execute(
            "UPDATE autonomy_tickets SET summary = 'x' WHERE ticket_id = %s", (stuck,)
        )
        started = asyncio.get_running_loop().time()
        with pytest.raises(tickets_db.WorkerSlotsFull):
            await asyncio.wait_for(
                tickets_db.update_ticket(
                    stuck, expected_status="queued", status="running", slot_limit=5
                ),
                20,
            )
        assert asyncio.get_running_loop().time() - started < 10
        updated = await asyncio.wait_for(
            tickets_db.update_ticket(
                other, expected_status="queued", status="running", slot_limit=5
            ),
            20,
        )
        assert updated is not None and updated["status"] == "running"
    finally:
        await writer.rollback()
        await writer.close()


@pytest.mark.asyncio
async def test_the_runner_leases_only_the_launches_it_has_slots_to_run(pg):
    """Through the runner's own claim pass, not a hand-leased row: with every
    execution slot held by a long launch, a further launch effect stays pending
    with its attempts, however often its (never taken) lease would have run out.
    Leased while it waited, it burned its six attempts and was parked dead
    without its handler having run."""
    from src.server.services.autonomy.effects import AutonomyEffectRunner

    await pg.cap_of(3)
    clouds = [await pg.ticket() for _ in range(3)]
    for cloud in clouds:
        await pg.launch_effect_pending(cloud)
    runner = AutonomyEffectRunner(concurrency=2)
    runner.register("launch_worker", pg.worker.launch_worker_effect)

    async def lease_of(cloud: str) -> tuple:
        async with pg.probe.cursor() as cur:
            await cur.execute(
                "SELECT status, attempts, lease_until IS NOT NULL "
                "FROM autonomy_effects WHERE user_id = %s "
                "AND kind = 'launch_worker' AND payload->>'ticket_id' = %s",
                (pg.user, cloud),
            )
            return await cur.fetchone()

    try:
        assert await runner.run_once() == 2

        async def both_in_containers() -> bool:
            return len(pg.containers.started) == 2

        await _until(both_in_containers)
        (waiting,) = [cloud for cloud in clouds if cloud not in pg.containers.started]

        for _ in range(8):  # more than the six attempts an effect has
            # Whatever lease the waiting effect held would be over by now.
            async with pg.probe.cursor() as cur:
                await cur.execute(
                    "UPDATE autonomy_effects SET lease_until = NOW() - INTERVAL '1 second' "
                    "WHERE user_id = %s AND status = 'claimed' "
                    "AND payload->>'ticket_id' = %s",
                    (pg.user, waiting),
                )
            assert await runner.run_once() == 0
            await asyncio.sleep(0.02)

        assert await lease_of(waiting) == ("pending", 0, False)
        assert waiting not in pg.containers.started
        assert (await pg.row(waiting))["status"] == "queued"

        # The two Workers end; the waiting effect is leased once, on a free slot.
        pg.containers.release.set()
        await runner.wait_idle()
        assert await runner.run_once() == 1
        await runner.wait_idle()
        assert await lease_of(waiting) == ("done", 1, False)
        assert sorted(pg.containers.started) == sorted(clouds)
    finally:
        pg.containers.release.set()
        await runner.wait_idle()
