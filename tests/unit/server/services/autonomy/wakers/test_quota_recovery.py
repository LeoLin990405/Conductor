"""Quota reset must launch again, and a stale or raced write must not land."""

from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from uuid import UUID

import pytest

import src.server.services.autonomy.wakers.quota_store as tickets_mod
from src.server.services.autonomy.wakers.quota_store import (
    apply_quota_schedule,
    quota_observation_rank_beats,
    quota_schedule_fence,
)
from src.server.services.autonomy.wakers.codexbar import quota_transitions
from src.server.services.autonomy.effects import AutonomyEffectRunner
from src.server.services.autonomy.wakers.runtime import waker_effect
from src.server.services.autonomy.executor import ensure_launch_effects
from src.server.services.autonomy.models import (
    EFFECT_LAUNCH_WORKER,
    EffectRecord,
)
from src.server.services.autonomy.wakers.base import EFFECT_WAKE
from src.server.services.autonomy.wakers.base import WakeEvent
from src.server.services.autonomy.wakers.scheduling import (
    DbQuotaStore,
    WorkItem,
    quota_launch_key,
)
from src.server.services.autonomy.worker import launch_worker_effect

# Captured before the autouse fixture replaces it with a no-op.
_REAL_QUOTA_PROVIDER_LOCK = tickets_mod.quota_provider_lock


def _obj(value):
    return value.obj if hasattr(value, "obj") else value


@pytest.fixture(autouse=True)
def _noop_quota_provider_lock(monkeypatch):
    @asynccontextmanager
    async def _lock(provider, **kwargs):
        yield

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_store.quota_provider_lock",
        _lock,
    )


class MemoryEffects:
    def __init__(self) -> None:
        self.pending: list[dict] = []
        self.known: set[str] = set()
        self.acked: list[str] = []

    def add(self, row: dict) -> bool:
        key = row["idempotency_key"]
        if key in self.known:
            return False
        self.known.add(key)
        self.pending.append(row)
        return True

    async def park_exhausted_effects(self, *, max_attempts: int) -> list[dict]:
        return []

    async def claim_due_effects(
        self, *, limit: int, lease_seconds: float, max_attempts: int
    ) -> list[dict]:
        claimed, self.pending = self.pending[:limit], self.pending[limit:]
        return claimed

    async def ack_effect(self, *, effect_id: str, attempts: int) -> bool:
        self.acked.append(effect_id)
        return True

    async def nack_effect(self, **kwargs) -> bool:
        raise AssertionError(kwargs)

    async def dead_letter_effect(self, **kwargs) -> bool:
        raise AssertionError(kwargs)

    async def defer_effect(self, **kwargs) -> bool:
        raise AssertionError(kwargs)


class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self.conn = conn
        self.rowcount = 0
        self._row = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, sql, params=None):
        text = " ".join(sql.split())
        if "UPDATE autonomy_tickets" in text:
            self._update(params)
            return
        if "INSERT INTO autonomy_effects" in text:
            self._insert(params)
            return
        raise AssertionError(text)

    def _update(self, params) -> None:
        (
            ledger,
            status,
            replace_flag,
            attention,
            target,
            expected_status,
            expected_revision,
            replace_again,
            reasons,
            provider_gate,
            wake_effect_a,
            wake_effect_join,
            kinds_rank,
            spellings_rank,
            wake_effect_b,
            kinds_stamp,
            spellings_stamp,
            observed_at,
        ) = params
        assert replace_flag is replace_again
        assert wake_effect_a == wake_effect_join == wake_effect_b
        assert "quota_exhausted" in reasons
        assert "quota_exhausted" in kinds_rank
        assert "quota_reset" in kinds_rank
        assert kinds_rank == kinds_stamp
        assert list(spellings_rank) == list(spellings_stamp)
        self.conn.last_spellings = list(spellings_rank)
        self.conn.last_observed_at = str(observed_at or "")
        wake_effect = str(wake_effect_a or "")
        allowed = {str(item).lower() for item in spellings_rank}
        latest = ""
        latest_rank = ("", "", "")
        write_rank = (str(observed_at or ""), "", wake_effect)
        if provider_gate:
            for entry in self.conn.observations:
                if len(entry) == 2:
                    name, stamp = entry
                    created, effect = "", ""
                else:
                    name, stamp, created, effect = entry
                if str(name).lower() not in allowed:
                    continue
                rank = (str(stamp or ""), str(created or ""), str(effect or ""))
                if rank > latest_rank:
                    latest_rank = rank
                    latest = str(stamp or "")
            me = self.conn.effect_ranks.get(wake_effect)
            if me is not None:
                write_rank = me
        live = self.conn.ticket
        matched = quota_schedule_fence(
            status=str(live["status"]),
            revision=int(live["revision"]),
            attention=live.get("attention"),
            expected_status=expected_status,
            expected_revision=int(expected_revision),
            replace_attention=bool(replace_flag),
            observed_at=str(observed_at or ""),
            latest_observed_at=latest,
            write_rank=write_rank if wake_effect else None,
            latest_rank=latest_rank if wake_effect else None,
        )
        matched = matched and str(live["ticket_id"]) == str(target)
        self.rowcount = 1 if matched else 0
        self._row = None
        if not matched:
            return
        live["status"] = status or live["status"]
        live["revision"] = int(live["revision"]) + 1
        mandate = dict(live.get("mandate") or {})
        mandate["quota_waker"] = dict(_obj(ledger))
        live["mandate"] = mandate
        if replace_flag:
            live["attention"] = _obj(attention)

    def _insert(self, params) -> None:
        user_id, kind, key, payload, _due = params
        assert kind == "launch_worker"
        inserted = self.conn.effects.add(
            {
                "effect_id": "effect-launch",
                "user_id": user_id,
                "kind": kind,
                "idempotency_key": key,
                "payload": dict(_obj(payload)),
                "attempts": 1,
            }
        )
        self.rowcount = 1 if inserted else 0
        self._row = ("new",) if inserted else None

    async def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, ticket: dict, effects: MemoryEffects) -> None:
        self.ticket = ticket
        self.effects = effects
        # (provider, observed_at) or (provider, observed_at, created_at, effect_id)
        self.observations: list[tuple] = []
        self.effect_ranks: dict[str, tuple[str, str, str]] = {}
        self.last_spellings: list[str] = []
        self.last_observed_at = ""

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def cursor(self, row_factory=None):
        return _Cursor(self)


def _ticket(ticket_id: str) -> dict:
    return {
        "ticket_id": ticket_id,
        "user_id": "user-1",
        "workspace_id": "ws-1",
        "title": "Parked",
        "status": "blocked",
        "attempts": 0,
        "revision": 4,
        "attention": {
            "question": "Parked because the glm quota is exhausted.",
            "reason": "quota_exhausted",
        },
        "mandate": {
            "quota_waker": {
                "reason": "quota_exhausted",
                "retries": 0,
                "state": "parked",
                "last_event_key": "old",
                "last_observed_at": "",
            }
        },
        "worker_provider": "glm",
        "worker_daemon_id": None,
        "worker_thread_id": None,
        "worker_run_id": None,
        "closed_at": None,
        "line_id": "",
    }


def _wake(ticket_id: str) -> tuple[WakeEvent, EffectRecord]:
    event = WakeEvent(
        source="codexbar",
        kind="quota_reset",
        idempotency_key="waker:codexbar:quota_reset:glm:recover",
        provider="glm",
        observed_at="2026-11-10T04:00:00+00:00",
        user_id="user-1",
    )
    effect = EffectRecord(
        effect_id="effect-wake",
        user_id="user-1",
        kind=EFFECT_WAKE,
        idempotency_key=event.idempotency_key,
        payload=event.as_payload(),
        attempts=1,
    )
    return event, effect


@asynccontextmanager
async def _db(conn):
    yield conn


def test_quota_schedule_sql_fences_revision_and_human_questions():
    source = inspect.getsource(apply_quota_schedule)
    assert "revision = %s" in source
    assert "attention->>'question'" in source
    assert "attention->>'reason'" in source
    assert "launch_worker" in source
    assert "FROM autonomy_effects" in source
    assert "COALESCE(payload->>'observed_at', '') > %s" in source
    assert quota_schedule_fence(
        status="queued",
        revision=4,
        attention=None,
        expected_status="queued",
        expected_revision=4,
        replace_attention=True,
        observed_at="2026-11-10T04:00:00+00:00",
        latest_observed_at="2026-11-10T05:00:00+00:00",
    ) is False
    assert quota_schedule_fence(
        status="queued",
        revision=4,
        attention=None,
        expected_status="queued",
        expected_revision=4,
        replace_attention=True,
        observed_at="2026-11-10T05:00:00+00:00",
        latest_observed_at="2026-11-10T05:00:00+00:00",
    ) is True
    assert quota_schedule_fence(
        status="blocked",
        revision=4,
        attention={"question": "Which branch?", "reason": "clarification"},
        expected_status="blocked",
        expected_revision=4,
        replace_attention=True,
    ) is False
    assert quota_schedule_fence(
        status="blocked",
        revision=5,
        attention={"question": "Parked.", "reason": "quota_exhausted"},
        expected_status="blocked",
        expected_revision=4,
        replace_attention=True,
    ) is False
    assert quota_schedule_fence(
        status="blocked",
        revision=4,
        attention={"question": "Parked.", "reason": "quota_exhausted"},
        expected_status="blocked",
        expected_revision=4,
        replace_attention=True,
    ) is True


@pytest.mark.asyncio
async def test_missing_revision_does_not_touch_the_database(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("database")

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_store.get_db_connection", boom
    )
    assert (
        await apply_quota_schedule(
            str(UUID(int=1)),
            expected_status="blocked",
            status="queued",
            attention=None,
            replace_attention=True,
            ledger={},
            expected_revision=None,
        )
        is False
    )
    assert (
        await apply_quota_schedule(
            str(UUID(int=1)),
            expected_status="blocked",
            status="queued",
            attention=None,
            replace_attention=True,
            ledger={},
            expected_revision=True,  # type: ignore[arg-type]
        )
        is False
    )


@pytest.mark.asyncio
async def test_reset_launches_through_the_real_effect_path(monkeypatch):
    ticket_id = str(UUID(int=7))
    live = _ticket(ticket_id)
    planned = deepcopy(live)
    effects = MemoryEffects()
    attempt_key = f"autonomy:launch:{ticket_id}:attempt:1"
    effects.known.add(attempt_key)
    event, wake = _wake(ticket_id)
    effects.add(
        {
            "effect_id": wake.effect_id,
            "user_id": wake.user_id,
            "kind": wake.kind,
            "idempotency_key": wake.idempotency_key,
            "payload": wake.payload,
            "attempts": wake.attempts,
        }
    )
    conn = _Conn(live, effects)

    async def list_items(*args, **kwargs):
        return [deepcopy(planned)]

    async def horizon(provider):
        return event.observed_at

    async def get_ticket(requested):
        assert requested == ticket_id
        return live

    launched: list[str] = []

    async def launch(ticket, requested, effect):
        assert ticket["status"] == "queued"
        assert ticket["attempts"] == 0
        launched.append(effect.idempotency_key)
        live["status"] = "completed"

    async def allowed():
        return True

    async def slot():
        return True

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_store.list_quota_work_items", list_items
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_history.latest_quota_observed_at", horizon
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_store.get_db_connection",
        lambda _ignored=None: _db(conn),
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.worker.ticket_db.get_ticket", get_ticket
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.worker._launch_isolated_cloud_worker", launch
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.worker._worker_slot_available", slot
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.cloud_runtime.cloud_worker_launch_allowed_async",
        allowed,
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.settings.get_autonomy_waker_codexbar_enabled", lambda: True
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.settings.get_autonomy_waker_repobar_enabled", lambda: False
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.settings.get_autonomy_waker_max_quota_retries", lambda: 3
    )

    runner = AutonomyEffectRunner(store=effects)
    runner.register(EFFECT_WAKE, waker_effect)
    runner.register(EFFECT_LAUNCH_WORKER, launch_worker_effect)
    assert await runner.run_once() == 1
    await runner.wait_idle()
    quota_key = quota_launch_key(ticket_id, event.idempotency_key)
    assert live["status"] == "queued"
    assert live["attempts"] == 0
    assert live["attention"] is None
    assert live["revision"] == 5
    assert quota_key in effects.known
    assert any(row["idempotency_key"] == quota_key for row in effects.pending)

    swept: list[str] = []

    class _Sweep:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return self

        async def execute(self, sql, params=None):
            assert "attempt:" not in sql
            assert "status = 'queued'" in sql

        async def fetchall(self):
            return [(ticket_id, "user-1", live["attempts"])]

    @asynccontextmanager
    async def pool(conn=None):
        yield _Sweep()

    async def enqueue(**kwargs):
        key = kwargs["idempotency_key"]
        swept.append(key)
        return effects.add(
            {
                "effect_id": "effect-sweep",
                "user_id": kwargs["user_id"],
                "kind": kwargs["kind"],
                "idempotency_key": key,
                "payload": dict(kwargs.get("payload") or {}),
                "attempts": 1,
            }
        )

    monkeypatch.setattr("src.server.database.pool.get_db_connection", pool)
    monkeypatch.setattr(
        "src.server.database.autonomy.effects.enqueue_effect", enqueue
    )
    assert await ensure_launch_effects() == 0
    assert swept == [attempt_key]
    assert attempt_key not in {row["idempotency_key"] for row in effects.pending}

    assert await runner.run_once() == 1
    await runner.wait_idle()
    assert launched == [quota_key]
    assert live["status"] == "completed"
    assert attempt_key not in launched
    assert "effect-launch" in effects.acked


@pytest.mark.asyncio
async def test_a_raced_human_question_is_not_cleared(monkeypatch):
    ticket_id = str(UUID(int=8))
    planned = _ticket(ticket_id)
    live = deepcopy(planned)
    live["revision"] = 5
    live["attention"] = {"question": "Which branch?", "reason": "clarification"}
    effects = MemoryEffects()
    conn = _Conn(live, effects)
    _event, wake = _wake(ticket_id)

    loads = {"count": 0}

    async def list_items(*args, **kwargs):
        loads["count"] += 1
        # First plan is against the pre-race snapshot; retries see the
        # human question that landed on the live row.
        if loads["count"] == 1:
            return [deepcopy(planned)]
        return [deepcopy(live)]

    async def horizon(provider):
        return "2026-11-10T04:00:00+00:00"

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_store.list_quota_work_items", list_items
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_history.latest_quota_observed_at", horizon
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_store.get_db_connection",
        lambda _ignored=None: _db(conn),
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.settings.get_autonomy_waker_codexbar_enabled", lambda: True
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.settings.get_autonomy_waker_repobar_enabled", lambda: False
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.settings.get_autonomy_waker_max_quota_retries", lambda: 3
    )
    await waker_effect(wake)
    assert live["status"] == "blocked"
    assert live["revision"] == 5
    assert live["attention"]["question"] == "Which branch?"
    assert effects.pending == []
    assert loads["count"] >= 2


@pytest.mark.asyncio
async def test_a_newer_reset_makes_an_older_exhaust_stale(monkeypatch):
    ticket_id = str(UUID(int=9))
    row = _ticket(ticket_id)
    row["status"] = "queued"
    row["attention"] = None
    row["mandate"] = {}

    async def list_items(*args, **kwargs):
        return [row]

    async def horizon(provider):
        return "2026-11-10T05:00:00+00:00"

    async def write(*args, **kwargs):
        raise AssertionError("stale quota write")

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_store.list_quota_work_items", list_items
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_history.latest_quota_observed_at", horizon
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_store.apply_quota_schedule", write
    )
    event = WakeEvent(
        source="codexbar",
        kind="quota_exhausted",
        idempotency_key="waker:late-exhaust",
        provider="glm",
        observed_at="2026-11-10T04:00:00+00:00",
        user_id="user-1",
    )
    effect = EffectRecord(
        effect_id="effect-late",
        user_id="user-1",
        kind=EFFECT_WAKE,
        idempotency_key=event.idempotency_key,
        payload=event.as_payload(),
        attempts=1,
    )
    from src.server.services.autonomy.wakers import apply_waker_effect

    actions = await apply_waker_effect(effect, codexbar_enabled=True)
    assert actions[0].record["reason"] == "stale_quota_event"
    assert actions[0].writes is False
    assert row["status"] == "queued"


@pytest.mark.asyncio
async def test_a_skipped_reset_fences_the_older_exhaust(monkeypatch):
    """A newer reset can leave a queued Ticket's revision untouched.

    The exhaust was planned against that revision. Once the reset is the
    stored provider observation, the write fence has to reject the exhaust
    or the Ticket stays parked: the next available snapshot emits no reset.
    """
    ticket_id = str(UUID(int=11))
    live = _ticket(ticket_id)
    live["status"] = "queued"
    live["attention"] = None
    live["mandate"] = {}
    live["revision"] = 4
    effects = MemoryEffects()
    conn = _Conn(live, effects)

    async def list_items(*args, **kwargs):
        return [deepcopy(live)]

    async def horizon(provider):
        assert provider == "glm"
        # Older than both wakes, so planning does not treat either as stale.
        return "2026-11-10T03:00:00+00:00"

    @asynccontextmanager
    async def connect(_ignored=None):
        # Installed at save time, after the exhaust was planned.
        conn.observations = [("zai", "2026-11-10T05:00:00+00:00")]
        yield conn

    async def outranks(provider, effect_id):
        return bool(conn.observations)

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_store.list_quota_work_items", list_items
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_history.latest_quota_observed_at", horizon
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_history.quota_observation_outranks_effect",
        outranks,
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_store.get_db_connection", connect
    )

    reset = WakeEvent(
        source="codexbar",
        kind="quota_reset",
        idempotency_key="waker:reset-first",
        provider="glm",
        observed_at="2026-11-10T05:00:00+00:00",
        user_id="user-1",
    )
    from src.server.services.autonomy.wakers import apply_waker_effect

    skipped = await apply_waker_effect(
        EffectRecord(
            effect_id="effect-reset",
            user_id="user-1",
            kind=EFFECT_WAKE,
            idempotency_key=reset.idempotency_key,
            payload=reset.as_payload(),
            attempts=1,
        ),
        codexbar_enabled=True,
    )
    assert skipped[0].writes is False
    assert skipped[0].record["reason"] == "not_parked"
    assert live["status"] == "queued"
    assert live["revision"] == 4

    exhaust = WakeEvent(
        source="codexbar",
        kind="quota_exhausted",
        idempotency_key="waker:late-exhaust",
        provider="glm",
        observed_at="2026-11-10T04:00:00+00:00",
        user_id="user-1",
    )
    planned = await apply_waker_effect(
        EffectRecord(
            effect_id="effect-exhaust",
            user_id="user-1",
            kind=EFFECT_WAKE,
            idempotency_key=exhaust.idempotency_key,
            payload=exhaust.as_payload(),
            attempts=1,
        ),
        codexbar_enabled=True,
    )
    assert planned[0].record["reason"] == "stale_quota_event"
    assert planned[0].writes is False
    assert "zai" in conn.last_spellings
    assert conn.last_observed_at == "2026-11-10T04:00:00+00:00"
    assert live["status"] == "queued"
    assert live["revision"] == 4
    assert live["attention"] is None
    assert effects.pending == []
    assert quota_transitions(
        {"glm": "quota_reset"},
        {"glm": "available"},
        exhausted=set(),
        observed_at="2026-11-10T06:00:00+00:00",
    ) == []


@pytest.mark.asyncio
async def test_a_bad_quota_cursor_does_not_rescan(monkeypatch):
    async def list_items(*args, **kwargs):
        raise AssertionError("rescanned")

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_store.list_quota_work_items", list_items
    )
    event = WakeEvent(
        source="codexbar",
        kind="quota_exhausted",
        idempotency_key="waker:cursor",
        provider="glm",
        quota_after_ticket_id="not-a-uuid",
    )
    assert await DbQuotaStore().load(event) == []


@pytest.mark.asyncio
async def test_provider_lock_serializes_reset_skip_before_older_exhaust(monkeypatch):
    """A newer reset that skips a queued Ticket must win over a late exhaust.

    Without the provider lock the exhaust can plan against an older horizon,
    then park after the reset effect is already the latest observation.
    """
    ticket_id = str(UUID(int=21))
    row = {
        "ticket_id": ticket_id,
        "user_id": "user-1",
        "status": "queued",
        "mandate": {},
        "attention": None,
        "worker_provider": "glm",
        "line_id": "",
        "revision": 2,
    }
    horizon = {"value": "2026-11-10T03:00:00+00:00"}
    order: list[str] = []
    held = {"owner": ""}

    @asynccontextmanager
    async def lock(provider, **kwargs):
        assert provider == "glm"
        assert held["owner"] == ""
        held["owner"] = provider
        try:
            yield
        finally:
            held["owner"] = ""

    class _Store:
        def __init__(self) -> None:
            self.continuation_after = ""
            self.saved: list = []

        async def load(self, event):
            order.append(f"load:{event.kind}:{event.observed_at}")
            from src.server.services.autonomy.wakers.scheduling import (
                with_quota_horizon,
                work_item_from_ticket,
            )

            return [with_quota_horizon(work_item_from_ticket(row), horizon["value"])]

        async def save(self, action):
            order.append(f"save:{action.action}")
            self.saved.append(action)
            if action.action == "park":
                row["status"] = action.to_status
                row["revision"] = int(row["revision"]) + 1
                row["attention"] = action.attention
                row["mandate"] = {"quota_waker": dict(action.ledger)}
            return True

    store = _Store()
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_store.quota_provider_lock",
        lock,
    )
    from src.server.services.autonomy.wakers import apply_waker_effect

    reset = WakeEvent(
        source="codexbar",
        kind="quota_reset",
        idempotency_key="waker:reset-lock",
        provider="glm",
        observed_at="2026-11-10T05:00:00+00:00",
        user_id="user-1",
    )
    exhaust = WakeEvent(
        source="codexbar",
        kind="quota_exhausted",
        idempotency_key="waker:exhaust-lock",
        provider="glm",
        observed_at="2026-11-10T04:00:00+00:00",
        user_id="user-1",
    )

    async def run(event: WakeEvent):
        return await apply_waker_effect(
            EffectRecord(
                effect_id=f"effect-{event.kind}",
                user_id="user-1",
                kind=EFFECT_WAKE,
                idempotency_key=event.idempotency_key,
                payload=event.as_payload(),
                attempts=1,
            ),
            store=store,
            codexbar_enabled=True,
        )

    skipped = await run(reset)
    assert skipped[0].writes is False
    assert skipped[0].record["reason"] == "not_parked"
    # The reset effect is now the provider horizon for later applies.
    horizon["value"] = reset.observed_at
    late = await run(exhaust)
    assert late[0].writes is False
    assert late[0].record["reason"] == "stale_quota_event"
    assert row["status"] == "queued"
    assert [item.action for item in store.saved] == []
    assert order == [
        "load:quota_reset:2026-11-10T05:00:00+00:00",
        "load:quota_exhausted:2026-11-10T04:00:00+00:00",
    ]


@pytest.mark.asyncio
async def test_a_failed_quota_save_reloads_and_retries(monkeypatch):
    ticket_id = str(UUID(int=22))
    item = WorkItem(
        target_id=ticket_id,
        provider="glm",
        status="blocked",
        retries=0,
        parked_reason="quota_exhausted",
        last_event_key="old",
        revision=4,
        user_id="user-1",
    )
    refreshed = replace(item, revision=5)

    class _Store:
        def __init__(self) -> None:
            self.continuation_after = ""
            self.loads = 0
            self.saves = 0

        async def load(self, event):
            self.loads += 1
            if self.loads == 1:
                return [item]
            assert event.ticket_id == ticket_id
            return [refreshed]

        async def save(self, action):
            self.saves += 1
            if self.saves == 1:
                assert action.expected_revision == 4
                return False
            assert action.expected_revision == 5
            assert action.action == "requeue"
            return True

    store = _Store()
    from src.server.services.autonomy.wakers import apply_waker_effect

    actions = await apply_waker_effect(
        EffectRecord(
            effect_id="effect-retry",
            user_id="user-1",
            kind=EFFECT_WAKE,
            idempotency_key="waker:retry-reset",
            payload=WakeEvent(
                source="codexbar",
                kind="quota_reset",
                idempotency_key="waker:retry-reset",
                provider="glm",
                observed_at="2026-11-10T05:00:00+00:00",
                user_id="user-1",
            ).as_payload(),
            attempts=1,
        ),
        store=store,
        codexbar_enabled=True,
    )
    assert store.loads == 2
    assert store.saves == 2
    assert actions[0].action == "requeue"
    assert actions[0].expected_revision == 5


@pytest.mark.asyncio
async def test_a_second_failed_quota_save_stays_retryable():
    ticket_id = str(UUID(int=23))
    item = WorkItem(
        target_id=ticket_id,
        provider="glm",
        status="blocked",
        retries=0,
        parked_reason="quota_exhausted",
        last_event_key="old",
        revision=4,
        user_id="user-1",
    )

    class _Store:
        def __init__(self) -> None:
            self.continuation_after = ""
            self.saves = 0

        async def load(self, event):
            return [replace(item, revision=4 + self.saves)]

        async def save(self, action):
            self.saves += 1
            return False

    from src.server.services.autonomy.wakers import apply_waker_effect

    with pytest.raises(RuntimeError, match="lost a race after retry"):
        await apply_waker_effect(
            EffectRecord(
                effect_id="effect-retry-2",
                user_id="user-1",
                kind=EFFECT_WAKE,
                idempotency_key="waker:retry-2",
                payload=WakeEvent(
                    source="codexbar",
                    kind="quota_reset",
                    idempotency_key="waker:retry-2",
                    provider="glm",
                    observed_at="2026-11-10T05:00:00+00:00",
                    user_id="user-1",
                ).as_payload(),
                attempts=1,
            ),
            store=_Store(),
            codexbar_enabled=True,
        )


@pytest.mark.asyncio
async def test_targeted_retry_preserves_page_continuation(monkeypatch):
    first = str(UUID(int=30))
    second = str(UUID(int=31))
    page = [
        WorkItem(
            target_id=first,
            provider="glm",
            status="queued",
            revision=1,
            user_id="user-1",
        ),
        WorkItem(
            target_id=second,
            provider="glm",
            status="queued",
            revision=1,
            user_id="user-1",
        ),
    ]

    class _Store:
        def __init__(self) -> None:
            self.continuation_after = ""
            self.loads = 0
            self.saved = []

        async def load(self, event):
            self.loads += 1
            if self.loads == 1:
                self.continuation_after = "page-cursor"
                return list(page)
            # Targeted retry must not clear the page cursor.
            assert event.ticket_id == first
            return [replace(page[0], revision=2)]

        async def save(self, action):
            self.saved.append(action)
            if len(self.saved) == 1:
                return False
            return True

    enqueued = []

    async def enqueue(*, user_id, idempotency_key, payload):
        enqueued.append(payload.get("quota_after_ticket_id"))
        return True

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.runtime.enqueue_waker_effect", enqueue
    )
    store = _Store()
    from src.server.services.autonomy.wakers import apply_waker_effect

    await apply_waker_effect(
        EffectRecord(
            effect_id="effect-page",
            user_id="user-1",
            kind=EFFECT_WAKE,
            idempotency_key="waker:page-preserve",
            payload=WakeEvent(
                source="codexbar",
                kind="quota_exhausted",
                idempotency_key="waker:page-preserve",
                provider="glm",
                observed_at="2026-11-10T05:00:00+00:00",
                user_id="user-1",
            ).as_payload(),
            attempts=1,
        ),
        store=store,
        codexbar_enabled=True,
    )
    assert store.continuation_after == "page-cursor"
    assert enqueued == ["page-cursor"]


def test_equal_timestamp_reset_outranks_older_exhaust():
    reset = ("2026-11-10T05:00:00+00:00", "2026-11-10T05:00:01+00:00", "effect-reset")
    exhaust = ("2026-11-10T05:00:00+00:00", "2026-11-10T05:00:00+00:00", "effect-exhaust")
    assert quota_observation_rank_beats(reset, exhaust)
    assert not quota_schedule_fence(
        status="queued",
        revision=1,
        attention=None,
        expected_status="queued",
        expected_revision=1,
        replace_attention=True,
        write_rank=exhaust,
        latest_rank=reset,
    )


@pytest.mark.asyncio
async def test_same_timestamp_reset_makes_exhaust_stale_at_plan_time(monkeypatch):
    ticket_id = str(UUID(int=24))
    item = WorkItem(
        target_id=ticket_id,
        provider="glm",
        status="queued",
        revision=1,
        user_id="user-1",
    )

    class _Store:
        def __init__(self) -> None:
            self.continuation_after = ""
            self.observation_stale = True
            self.saved = []

        async def load(self, event):
            assert event.effect_id == "effect-exhaust-same"
            return [item]

        async def save(self, action):
            self.saved.append(action)
            return True

    from src.server.services.autonomy.wakers import apply_waker_effect

    actions = await apply_waker_effect(
        EffectRecord(
            effect_id="effect-exhaust-same",
            user_id="user-1",
            kind=EFFECT_WAKE,
            idempotency_key="waker:same-ts-exhaust",
            payload=WakeEvent(
                source="codexbar",
                kind="quota_exhausted",
                idempotency_key="waker:same-ts-exhaust",
                provider="glm",
                observed_at="2026-11-10T05:00:00+00:00",
                user_id="user-1",
            ).as_payload(),
            attempts=1,
        ),
        store=_Store(),
        codexbar_enabled=True,
    )
    assert actions[0].writes is False
    assert actions[0].record["reason"] == "stale_quota_event"


@pytest.mark.asyncio
async def test_continuation_keeps_origin_rank_against_same_timestamp_reset(
    monkeypatch,
):
    """Page-2 exhaust must not outrank a same-timestamp reset via created_at.

    A full page enqueues a continuation under a new effect id. Without
    preserving the origin rank, that later created_at beats a reset that
    already won on the first page and parks queued Tickets again.
    """
    origin_id = str(UUID(int=50))
    page_cursor = str(UUID(int=51))
    enqueued: list[dict] = []

    async def enqueue(*, user_id, idempotency_key, payload):
        enqueued.append(dict(payload))
        return True

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.runtime.enqueue_waker_effect", enqueue
    )

    class _Store:
        def __init__(self) -> None:
            self.continuation_after = page_cursor
            self.saved = []
            self.effect_ids: list[str] = []

        async def load(self, event):
            self.effect_ids.append(event.ranking_effect_id())
            return [
                WorkItem(
                    target_id=str(UUID(int=52)),
                    provider="glm",
                    status="queued",
                    revision=1,
                    user_id="user-1",
                )
            ]

        async def save(self, action):
            self.saved.append(action)
            return True

    store = _Store()
    from src.server.services.autonomy.wakers import apply_waker_effect

    await apply_waker_effect(
        EffectRecord(
            effect_id=origin_id,
            user_id="user-1",
            kind=EFFECT_WAKE,
            idempotency_key="waker:page1",
            payload=WakeEvent(
                source="codexbar",
                kind="quota_exhausted",
                idempotency_key="waker:page1",
                provider="glm",
                observed_at="2026-11-10T05:00:00+00:00",
                user_id="user-1",
            ).as_payload(),
            attempts=1,
        ),
        store=store,
        codexbar_enabled=True,
    )
    assert enqueued
    assert enqueued[0]["quota_rank_effect_id"] == origin_id
    assert enqueued[0]["quota_after_ticket_id"] == page_cursor

    # A same-timestamp reset outranks the origin. The continuation must fence
    # against the origin id, not its own later-created row.
    continuation = WakeEvent.from_payload(enqueued[0])
    assert continuation.quota_rank_effect_id == origin_id
    assert continuation.ranking_effect_id() == origin_id

    class _StaleStore:
        def __init__(self) -> None:
            self.continuation_after = ""
            self.observation_stale = True
            self.saved = []
            self.loaded_rank = ""

        async def load(self, event):
            self.loaded_rank = event.ranking_effect_id()
            return [
                WorkItem(
                    target_id=str(UUID(int=53)),
                    provider="glm",
                    status="queued",
                    revision=1,
                    user_id="user-1",
                )
            ]

        async def save(self, action):
            self.saved.append(action)
            return True

    stale = _StaleStore()
    actions = await apply_waker_effect(
        EffectRecord(
            effect_id=str(UUID(int=99)),
            user_id="user-1",
            kind=EFFECT_WAKE,
            idempotency_key=enqueued[0]["idempotency_key"],
            payload=enqueued[0],
            attempts=1,
        ),
        store=stale,
        codexbar_enabled=True,
    )
    assert stale.loaded_rank == origin_id
    assert actions[0].writes is False
    assert actions[0].record["reason"] == "stale_quota_event"
    assert stale.saved == []


def test_latest_quota_rows_sql_skips_continuations():
    from src.server.services.autonomy.wakers import quota_history

    source = inspect.getsource(quota_history._latest_quota_rows)
    assert "quota_after_ticket_id" in source
    assert "COALESCE(payload->>'quota_after_ticket_id', '') = ''" in source


@pytest.mark.asyncio
async def test_continuation_enqueue_reuses_held_provider_lock(monkeypatch):
    """Apply holds the provider lock; continuation enqueue must not re-lock.

    Production walks ``_apply_quota`` → ``enqueue_waker_effect`` →
    ``quota_provider_lock``. A second session advisory lock waits until the
    first unlocks, so a full page never enqueues the next cursor. The lock is
    reentrant on this task; one physical acquire must cover both steps.
    """
    physical_acquires: list[str] = []
    inserts: list[str] = []
    held_by: dict[str, object] = {}

    class _Cursor:
        def __init__(self, session: "_Session") -> None:
            self.session = session
            self._row = (False,)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql, params=None):
            key = params[0] if params else ""
            provider = str(key).removeprefix("quota-waker:")
            if "pg_try_advisory_lock" in sql:
                owner = held_by.get(provider)
                if owner is None or owner is self.session:
                    held_by[provider] = self.session
                    if owner is None:
                        physical_acquires.append(provider)
                    self._row = (True,)
                else:
                    # Another session already holds it — the old nested-lock bug.
                    self._row = (False,)
                return
            if "pg_advisory_unlock" in sql:
                if held_by.get(provider) is self.session:
                    held_by.pop(provider, None)
                self._row = (True,)

        async def fetchone(self):
            return self._row

    class _Session:
        def cursor(self):
            return _Cursor(self)

        async def close(self):
            return None

    async def open_session():
        return _Session()

    @asynccontextmanager
    async def real_lock_fast(provider, **kwargs):
        async with _REAL_QUOTA_PROVIDER_LOCK(provider, timeout_seconds=0.2):
            yield

    monkeypatch.setattr(tickets_mod, "open_session_connection", open_session)
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_store.quota_provider_lock",
        real_lock_fast,
    )

    async def fake_enqueue_effect(**kwargs):
        inserts.append(kwargs["idempotency_key"])
        return True

    monkeypatch.setattr(
        "src.server.database.autonomy.effects.enqueue_effect",
        fake_enqueue_effect,
    )

    page_ids = [str(UUID(int=index)) for index in range(40, 42)]

    class _Store:
        def __init__(self) -> None:
            self.continuation_after = ""

        async def load(self, event):
            self.continuation_after = page_ids[0]
            return [
                WorkItem(
                    target_id=page_ids[0],
                    provider="glm",
                    status="queued",
                    revision=1,
                    user_id="user-1",
                )
            ]

        async def save(self, action):
            return True

    from src.server.services.autonomy.wakers import apply_waker_effect

    await apply_waker_effect(
        EffectRecord(
            effect_id="effect-continuation-lock",
            user_id="user-1",
            kind=EFFECT_WAKE,
            idempotency_key="waker:continuation-lock",
            payload=WakeEvent(
                source="codexbar",
                kind="quota_exhausted",
                idempotency_key="waker:continuation-lock",
                provider="glm",
                observed_at="2026-11-10T05:00:00+00:00",
                user_id="user-1",
            ).as_payload(),
            attempts=1,
        ),
        store=_Store(),
        codexbar_enabled=True,
    )
    assert physical_acquires == ["glm"]
    assert inserts
    assert inserts[0].endswith(f":after:{page_ids[0]}")


@pytest.mark.asyncio
async def test_quota_provider_lock_is_held_across_concurrent_enqueue_and_apply(
    monkeypatch,
):
    """Enqueue and apply must not overlap for one provider.

    A reset insert that lands during an exhaust UPDATE's statement snapshot
    is invisible to the observed_at fence. Holding the same lock on both
    paths makes that interleaving wait.
    """
    mutex = asyncio.Lock()
    order: list[str] = []
    hold_apply = asyncio.Event()
    enqueue_started = asyncio.Event()

    @asynccontextmanager
    async def lock(provider, **kwargs):
        assert provider == "glm"
        await mutex.acquire()
        order.append(f"acquire:{provider}")
        try:
            yield
        finally:
            order.append(f"release:{provider}")
            mutex.release()

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_store.quota_provider_lock",
        lock,
    )

    class _Store:
        def __init__(self) -> None:
            self.continuation_after = ""

        async def load(self, event):
            return [
                WorkItem(
                    target_id=str(UUID(int=25)),
                    provider="glm",
                    status="queued",
                    revision=1,
                    user_id="user-1",
                )
            ]

        async def save(self, action):
            order.append("apply-save-enter")
            hold_apply.set()
            await enqueue_started.wait()
            await asyncio.sleep(0)
            order.append("apply-save-exit")
            return True

    inserted = []

    async def fake_enqueue_effect(**kwargs):
        order.append("enqueue-insert")
        inserted.append(kwargs["idempotency_key"])
        return True

    monkeypatch.setattr(
        "src.server.database.autonomy.effects.enqueue_effect",
        fake_enqueue_effect,
    )

    from src.server.services.autonomy.wakers.runtime import enqueue_waker_effect
    from src.server.services.autonomy.wakers import apply_waker_effect

    async def run_apply():
        await apply_waker_effect(
            EffectRecord(
                effect_id="effect-exhaust-concurrent",
                user_id="user-1",
                kind=EFFECT_WAKE,
                idempotency_key="waker:concurrent-exhaust",
                payload=WakeEvent(
                    source="codexbar",
                    kind="quota_exhausted",
                    idempotency_key="waker:concurrent-exhaust",
                    provider="glm",
                    observed_at="2026-11-10T04:00:00+00:00",
                    user_id="user-1",
                ).as_payload(),
                attempts=1,
            ),
            store=_Store(),
            codexbar_enabled=True,
        )

    async def run_enqueue():
        await hold_apply.wait()
        enqueue_started.set()
        await enqueue_waker_effect(
            user_id="user-1",
            idempotency_key="waker:concurrent-reset",
            payload={
                "source": "codexbar",
                "kind": "quota_reset",
                "provider": "glm",
                "observed_at": "2026-11-10T05:00:00+00:00",
            },
        )

    await asyncio.gather(run_apply(), run_enqueue())
    assert order == [
        "acquire:glm",
        "apply-save-enter",
        "apply-save-exit",
        "release:glm",
        "acquire:glm",
        "enqueue-insert",
        "release:glm",
    ]
    assert inserted == ["waker:concurrent-reset"]


@pytest.mark.asyncio
async def test_real_provider_lock_blocks_reset_publication_during_exhaust_update(
    monkeypatch,
):
    """Two tasks, the real advisory-lock wrapper, fake per-session locks.

    The exhaust apply parks the Ticket while its UPDATE is in flight; a reset
    published from another task must wait for that write to finish instead
    of landing inside the UPDATE's snapshot. Unlike the asyncio.Lock test
    above, this drives ``quota_provider_lock`` itself (try-lock loop, one
    session per holder), so a nested-hold or context leak bug would show.
    """
    held_by: dict[str, object] = {}
    order: list[str] = []

    class _Cursor:
        def __init__(self, session) -> None:
            self.session = session
            self._row = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, sql, params=None):
            key = str(params[0])
            if "pg_try_advisory_lock" in sql:
                owner = held_by.get(key)
                if owner is None or owner is self.session:
                    held_by[key] = self.session
                    self._row = (True,)
                else:
                    self._row = (False,)
                return
            if "pg_advisory_unlock" in sql:
                if held_by.get(key) is self.session:
                    held_by.pop(key, None)
                self._row = (True,)

        async def fetchone(self):
            return self._row

    class _Session:
        def cursor(self):
            return _Cursor(self)

        async def close(self):
            return None

    async def open_session():
        return _Session()

    monkeypatch.setattr(tickets_mod, "open_session_connection", open_session)
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_store.quota_provider_lock",
        _REAL_QUOTA_PROVIDER_LOCK,
    )

    update_running = asyncio.Event()
    reset_attempted = asyncio.Event()

    class _Store:
        continuation_after = ""

        async def load(self, event):
            return [
                WorkItem(
                    target_id=str(UUID(int=60)),
                    provider="glm",
                    status="queued",
                    revision=1,
                    user_id="user-1",
                )
            ]

        async def save(self, action):
            order.append("exhaust-update-start")
            update_running.set()
            await reset_attempted.wait()
            # Give the reset task several turns to (wrongly) get in.
            for _ in range(5):
                await asyncio.sleep(0.01)
            order.append("exhaust-update-commit")
            return True

    async def fake_enqueue_effect(**kwargs):
        order.append("reset-insert")
        return True

    monkeypatch.setattr(
        "src.server.database.autonomy.effects.enqueue_effect", fake_enqueue_effect
    )

    from src.server.services.autonomy.wakers import apply_waker_effect
    from src.server.services.autonomy.wakers.runtime import enqueue_waker_effect

    async def run_apply():
        await apply_waker_effect(
            EffectRecord(
                effect_id=str(UUID(int=61)),
                user_id="user-1",
                kind=EFFECT_WAKE,
                idempotency_key="waker:real-lock-exhaust",
                payload=WakeEvent(
                    source="codexbar",
                    kind="quota_exhausted",
                    idempotency_key="waker:real-lock-exhaust",
                    provider="zai",
                    observed_at="2026-11-10T05:00:00+00:00",
                    user_id="user-1",
                ).as_payload(),
                attempts=1,
            ),
            store=_Store(),
            codexbar_enabled=True,
        )

    async def run_reset():
        await update_running.wait()
        reset_attempted.set()
        await enqueue_waker_effect(
            user_id="user-1",
            idempotency_key="waker:real-lock-reset",
            payload={
                "source": "codexbar",
                "kind": "quota_reset",
                # Another spelling of the same provider still shares the lock.
                "provider": "glm",
                "observed_at": "2026-11-10T05:00:00+00:00",
            },
        )

    await asyncio.wait_for(asyncio.gather(run_apply(), run_reset()), timeout=5)
    assert order == ["exhaust-update-start", "exhaust-update-commit", "reset-insert"]
    assert held_by == {}
