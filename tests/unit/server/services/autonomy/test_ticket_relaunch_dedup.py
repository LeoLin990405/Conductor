"""A re-queued Ticket is not claimed again for a run its line already finished."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.server.services.autonomy import launch_dedup as dedup
from src.server.services.autonomy import worker
from tests.conftest import create_test_app

_KEY = dedup.LAUNCH_DEDUP_MANDATE_KEY
#: The work token of a Ticket with no scope, goal, input or quota requeue.
W0 = dedup.work_token({}, "")


def _entry(lease, *, provider="glm", model="glm-5", base="abc", ticket="t1", work=W0):
    key = {"subject": f"ticket:{ticket}", "provider": provider, "model": model, "base": base}
    if work is not None:
        key["work"] = work
    return {"lease_id": lease, "key": key, "at": "2026-11-14T00:00:00+00:00"}


def _claimed(*, lease="l-new", provider="glm", model="glm-5", base="abc", record=None):
    mandate = {"source_id": "s1"}
    if record is not None:
        mandate[_KEY] = record
    return {
        "ticket_id": "t1",
        "user_id": "u1",
        "status": "running",
        "lease_id": lease,
        "worker_provider": provider,
        "worker_model": model,
        "worktree_base_sha": base,
        "worker_lease_heartbeat_at": "hb",
        "revision": 7,
        "mandate": mandate,
    }


# --- pure rules -------------------------------------------------------------


def _relaunch(
    entries, outcomes, *, base="abc", provider="glm", model="glm-5", mode="enforce",
    force=False, live=(), work=W0, pending_input=False,
):
    key = dedup.ticket_launch_key(
        {"ticket_id": "t1", "worker_provider": provider, "worker_model": model, "worktree_base_sha": base},
        work=work,
    )
    return dedup.decide_relaunch(
        key, entries, outcomes=outcomes, live_leases=live, current_lease="l-new", mode=mode,
        force=force, pending_input=pending_input,
    )


def test_a_finished_run_on_the_same_line_and_base_is_a_duplicate():
    decision = _relaunch([_entry("l-old")], {"l-old": {"state": "ready"}})
    assert decision.duplicate and decision.skip
    assert decision.existing_state == "done" and decision.existing_ticket_id == "l-old"


def test_a_live_earlier_lease_is_in_flight():
    decision = _relaunch([_entry("l-old")], {}, live=("l-old",))
    assert decision.skip and decision.existing_state == "in_flight"


@pytest.mark.parametrize(
    "outcome", [None, {"state": "failed"}, {"state": "blocked"}, {"state": "waiting"}]
)
def test_an_unfinished_or_failed_run_relaunches(outcome):
    assert not _relaunch([_entry("l-old")], {"l-old": outcome}).duplicate


def test_a_switched_line_or_moved_base_is_a_new_key():
    done = {"l-old": {"state": "ready"}}
    assert not _relaunch([_entry("l-old")], done, provider="grok", model="grok-4").duplicate
    assert not _relaunch([_entry("l-old")], done, base="def").duplicate


def test_an_entry_recorded_before_the_pin_inherits_it_but_an_unpinned_claim_never_matches_done():
    done = {"l-old": {"state": "ready"}}
    assert _relaunch([_entry("l-old", base="")], done).skip
    assert not _relaunch([_entry("l-old", base="")], done, base="").duplicate
    assert not _relaunch([_entry("l-old")], done, base="").duplicate


def test_shadow_launches_force_launches_off_ignores():
    done = {"l-old": {"state": "ready"}}
    shadow = _relaunch([_entry("l-old")], done, mode="shadow")
    assert shadow.duplicate and not shadow.skip
    forced = _relaunch([_entry("l-old")], done, force=True)
    assert forced.duplicate and not forced.skip and forced.forced
    assert not _relaunch([_entry("l-old")], done, mode="off").duplicate


def test_record_adds_the_launch_spends_force_and_keeps_history():
    mandate = {_KEY: {"force_next": True, "launches": [_entry("l-old")], "history": []}}
    decision = _relaunch([_entry("l-old")], {"l-old": {"state": "ready"}}, mode="shadow")
    record = dedup.record_relaunch(mandate, decision, lease_id="l-new")
    assert "force_next" not in record
    assert [e["lease_id"] for e in record["launches"]] == ["l-old", "l-new"]
    assert record["last"]["would_dedupe"] is True
    skipped = dedup.record_relaunch(
        mandate, _relaunch([_entry("l-old")], {"l-old": {"state": "ready"}}), lease_id="l-new"
    )
    assert [e["lease_id"] for e in skipped["launches"]] == ["l-old"]
    assert skipped["last"]["action"] == "skip"


def test_with_force_next_sets_the_flag_without_dropping_the_record():
    out = dedup.with_force_next({"a": 1, _KEY: {"launches": [_entry("l-old")]}})
    assert out["a"] == 1 and out[_KEY]["force_next"] is True
    assert dedup.force_requested(out)
    assert out[_KEY]["launches"][0]["lease_id"] == "l-old"


# --- claim hook -------------------------------------------------------------


@pytest.fixture
def claim_env(monkeypatch):
    class Lock:
        async def __aenter__(self):
            return True

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(worker, "_launch_lock", lambda: Lock())
    monkeypatch.setattr(worker, "_worker_slot_available", AsyncMock(return_value=True))
    daemons = "src.server.database.autonomy.daemons"
    monkeypatch.setattr(f"{daemons}.recover_expired_daemon_leases", AsyncMock(return_value=0))
    monkeypatch.setattr(f"{daemons}.resume_ticket", AsyncMock(return_value=None))
    env = {
        "claim": AsyncMock(),
        "release": AsyncMock(return_value=True),
        "outcomes": AsyncMock(return_value={}),
        "work": AsyncMock(return_value=("", False)),
        "mandate_write": AsyncMock(
            side_effect=lambda tid, key, **kw: {"mandate": {key: kw["set_fields"]}, "revision": 8}
        ),
        "update": AsyncMock(return_value={"ticket_id": "t1", "status": "completed"}),
    }
    monkeypatch.setattr(f"{daemons}.claim_ticket", env["claim"])
    monkeypatch.setattr(f"{daemons}.release_daemon_claim", env["release"])
    monkeypatch.setattr(worker, "_claim_report_outcomes", env["outcomes"])
    monkeypatch.setattr(worker, "_claim_work_state", env["work"])
    monkeypatch.setattr(worker.ticket_db, "update_mandate_record", env["mandate_write"])
    monkeypatch.setattr(worker.ticket_db, "update_ticket", env["update"])
    return env


@pytest.mark.asyncio
async def test_off_returns_the_claim_untouched(monkeypatch, claim_env):
    monkeypatch.setenv(dedup.ENV_MODE, "off")
    claimed = _claimed(record={"launches": [_entry("l-old")]})
    claim_env["claim"].return_value = claimed
    claim_env["outcomes"].return_value = {"l-old": {"state": "ready"}}
    assert await worker.claim_daemon_ticket("d1") is claimed
    claim_env["outcomes"].assert_not_awaited()
    claim_env["mandate_write"].assert_not_awaited()
    claim_env["release"].assert_not_awaited()


@pytest.mark.asyncio
async def test_shadow_records_would_dedupe_and_launches(monkeypatch, claim_env):
    monkeypatch.delenv(dedup.ENV_MODE, raising=False)
    claim_env["claim"].return_value = _claimed(record={"launches": [_entry("l-old")]})
    claim_env["outcomes"].return_value = {"l-old": {"state": "ready"}}
    result = await worker.claim_daemon_ticket("d1")
    assert result is not None and result["lease_id"] == "l-new"
    assert result["revision"] == 8
    claim_env["outcomes"].assert_awaited_once_with("t1", ["l-old"])
    claim_env["release"].assert_not_awaited()
    record = claim_env["mandate_write"].call_args.kwargs["set_fields"]
    assert record["last"]["would_dedupe"] is True and record["last"]["mode"] == "shadow"
    assert [e["lease_id"] for e in record["launches"]] == ["l-old", "l-new"]
    assert claim_env["mandate_write"].call_args.kwargs["expected_status"] == "running"


@pytest.mark.asyncio
async def test_enforce_gives_the_duplicate_claim_back_and_keeps_the_finished_run(monkeypatch, claim_env):
    monkeypatch.setenv(dedup.ENV_MODE, "enforce")
    claim_env["claim"].return_value = _claimed(record={"launches": [_entry("l-old")]})
    claim_env["outcomes"].return_value = {"l-old": {"state": "ready"}}
    assert await worker.claim_daemon_ticket("d1") is None
    claim_env["release"].assert_awaited_once_with("d1", "t1", "l-new", heartbeat_at="hb")
    update = claim_env["update"].call_args.kwargs
    assert update["expected_status"] == "queued" and update["status"] == "completed"
    record = claim_env["mandate_write"].call_args.kwargs["set_fields"]
    assert record["last"]["action"] == "skip"
    assert [e["lease_id"] for e in record["launches"]] == ["l-old"]


@pytest.mark.asyncio
async def test_enforce_launches_when_the_claim_cannot_be_given_back(monkeypatch, claim_env):
    monkeypatch.setenv(dedup.ENV_MODE, "enforce")
    claimed = _claimed(record={"launches": [_entry("l-old")]})
    claim_env["claim"].return_value = claimed
    claim_env["outcomes"].return_value = {"l-old": {"state": "ready"}}
    claim_env["release"].return_value = False
    assert await worker.claim_daemon_ticket("d1") is claimed
    claim_env["update"].assert_not_awaited()


@pytest.mark.asyncio
async def test_enforce_honours_an_operator_force(monkeypatch, claim_env):
    monkeypatch.setenv(dedup.ENV_MODE, "enforce")
    claim_env["claim"].return_value = _claimed(
        record={"launches": [_entry("l-old")], "force_next": True}
    )
    claim_env["outcomes"].return_value = {"l-old": {"state": "ready"}}
    result = await worker.claim_daemon_ticket("d1")
    assert result is not None
    claim_env["release"].assert_not_awaited()
    kwargs = claim_env["mandate_write"].call_args.kwargs
    assert kwargs["remove_fields"] == ("force_next",)
    assert kwargs["set_fields"]["last"]["forced"] is True


@pytest.mark.asyncio
async def test_enforce_relaunches_a_switched_line(monkeypatch, claim_env):
    monkeypatch.setenv(dedup.ENV_MODE, "enforce")
    claim_env["claim"].return_value = _claimed(
        provider="grok", model="grok-4", record={"launches": [_entry("l-old")]}
    )
    claim_env["outcomes"].return_value = {"l-old": {"state": "ready"}}
    assert await worker.claim_daemon_ticket("d1") is not None
    claim_env["release"].assert_not_awaited()


@pytest.mark.asyncio
async def test_a_replayed_claim_is_not_recorded_twice(monkeypatch, claim_env):
    monkeypatch.setenv(dedup.ENV_MODE, "enforce")
    claimed = _claimed(record={"launches": [_entry("l-new")]})
    claim_env["claim"].return_value = claimed
    assert await worker.claim_daemon_ticket("d1") is claimed
    claim_env["mandate_write"].assert_not_awaited()


@pytest.mark.asyncio
async def test_a_dedup_failure_never_costs_the_launch(monkeypatch, claim_env):
    monkeypatch.setenv(dedup.ENV_MODE, "enforce")
    claimed = _claimed(record={"launches": [_entry("l-old")]})
    claim_env["claim"].return_value = claimed
    claim_env["outcomes"].side_effect = RuntimeError("db down")
    assert await worker.claim_daemon_ticket("d1") is claimed


# --- operator retry ---------------------------------------------------------


@pytest_asyncio.fixture
async def client():
    from src.server.app.autonomy import router

    app = create_test_app(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _retry(client, monkeypatch, mode):
    from datetime import datetime, timezone
    from unittest.mock import patch

    now = datetime.now(timezone.utc)
    if mode is None:
        monkeypatch.delenv(dedup.ENV_MODE, raising=False)
    else:
        monkeypatch.setenv(dedup.ENV_MODE, mode)
    row = {
        "ticket_id": "33333333-3333-4333-8333-333333333333",
        "user_id": "test-user-123",
        "workspace_id": "w1",
        "title": "Fix",
        "scope": "Fix",
        "status": "failed",
        "mandate": {"source_id": "s1"},
        "attempts": 2,
        "summary": "",
        "error": "",
        "origin_thread_key": "thread-1",
        "created_at": now,
        "updated_at": now,
    }
    update = AsyncMock(return_value={**row, "status": "queued"})
    enqueue = AsyncMock(return_value=True)
    with (
        patch("src.server.app.autonomy.features.user_feature_enabled", new=AsyncMock(return_value=True)),
        patch("src.server.app.autonomy.tickets_db.get_ticket", new=AsyncMock(return_value=row)),
        patch("src.server.app.autonomy.ownership.revoked_mandate_reason", new=AsyncMock(return_value=None)),
        patch("src.server.app.autonomy.tickets_db.update_ticket", update),
        patch("src.server.database.autonomy.effects.enqueue_effect", enqueue),
    ):
        resp = await client.post(f"/api/v1/autonomy/tickets/{row['ticket_id']}/retry")
    assert resp.status_code == 200, resp.text
    return update, enqueue


@pytest.mark.asyncio
async def test_retry_forces_the_next_launch(client, monkeypatch):
    update, enqueue = await _retry(client, monkeypatch, None)
    assert dedup.force_requested(update.call_args.kwargs["mandate"])
    assert enqueue.call_args.kwargs["payload"]["force"] is True
    assert dedup.is_forced(enqueue.call_args.kwargs["payload"])


@pytest.mark.asyncio
async def test_retry_off_is_unchanged(client, monkeypatch):
    update, enqueue = await _retry(client, monkeypatch, "off")
    assert "mandate" not in update.call_args.kwargs
    assert enqueue.call_args.kwargs["payload"] == {"ticket_id": "33333333-3333-4333-8333-333333333333"}


# --- deliberate re-runs: new work is a new key -------------------------------


def test_new_work_is_a_new_key():
    done = {"l-old": {"state": "ready"}}
    assert not _relaunch([_entry("l-old")], done, work="other").duplicate
    # An entry without a work token never matches a key that has one.
    assert not _relaunch([_entry("l-old", work=None)], done).duplicate
    assert not _relaunch([_entry("l-old")], done, live=("l-old",), work="other").duplicate


def test_a_pending_input_always_launches():
    decision = _relaunch([_entry("l-old")], {"l-old": {"state": "ready"}}, pending_input=True)
    assert not decision.duplicate and not decision.skip


def test_the_work_token_moves_with_scope_goal_input_and_quota_requeue():
    base = {"scope": "Fix", "mandate": {"goal": "Fix it"}}
    token = dedup.work_token(base, "i1")
    assert dedup.work_token(dict(base), "i1") == token
    # A clarification rewrites the scope and the goal.
    assert dedup.work_token({**base, "scope": "Fix the parser"}, "i1") != token
    assert dedup.work_token({**base, "mandate": {"goal": "Fix the parser"}}, "i1") != token
    # An answer, a follow-up or an attached trigger is a new input row.
    assert dedup.work_token(base, "i2") != token
    # A quota requeue counts up the waker's retries.
    requeued = {**base, "mandate": {"goal": "Fix it", "quota_waker": {"retries": 1}}}
    assert dedup.work_token(requeued, "i1") != token
    # Records the de-dup itself writes are not work.
    recorded = {**base, "mandate": {"goal": "Fix it", _KEY: {"launches": []}}}
    assert dedup.work_token(recorded, "i1") == token


@pytest.mark.asyncio
async def test_enforce_relaunches_after_a_clarification(monkeypatch, claim_env):
    """The clarification path rewrites scope/goal and inserts an answer input."""
    monkeypatch.setenv(dedup.ENV_MODE, "enforce")
    claimed = _claimed(record={"launches": [_entry("l-old")]})
    claimed["scope"] = "Fix the parser, not the lexer"
    claimed["mandate"]["goal"] = "Fix the parser, not the lexer"
    claim_env["claim"].return_value = claimed
    claim_env["outcomes"].return_value = {"l-old": {"state": "ready"}}
    claim_env["work"].return_value = ("answer-input", False)
    assert await worker.claim_daemon_ticket("d1") is not None
    claim_env["release"].assert_not_awaited()
    claim_env["update"].assert_not_awaited()
    record = claim_env["mandate_write"].call_args.kwargs["set_fields"]
    assert record["launches"][-1]["key"]["work"] == dedup.work_token(claimed, "answer-input")


@pytest.mark.asyncio
async def test_enforce_relaunches_for_a_follow_up_input(monkeypatch, claim_env):
    monkeypatch.setenv(dedup.ENV_MODE, "enforce")
    claim_env["claim"].return_value = _claimed(record={"launches": [_entry("l-old")]})
    claim_env["outcomes"].return_value = {"l-old": {"state": "ready"}}
    claim_env["work"].return_value = ("follow-up-input", False)
    assert await worker.claim_daemon_ticket("d1") is not None
    claim_env["release"].assert_not_awaited()
    claim_env["update"].assert_not_awaited()


@pytest.mark.asyncio
async def test_enforce_never_completes_a_ticket_with_pending_input(monkeypatch, claim_env):
    monkeypatch.setenv(dedup.ENV_MODE, "enforce")
    claim_env["claim"].return_value = _claimed(record={"launches": [_entry("l-old")]})
    claim_env["outcomes"].return_value = {"l-old": {"state": "ready"}}
    # Same latest input as the finished run, but it was never delivered.
    claim_env["work"].return_value = ("", True)
    assert await worker.claim_daemon_ticket("d1") is not None
    claim_env["release"].assert_not_awaited()
    claim_env["update"].assert_not_awaited()


@pytest.mark.asyncio
async def test_enforce_relaunches_after_a_quota_requeue(monkeypatch, claim_env):
    monkeypatch.setenv(dedup.ENV_MODE, "enforce")
    claimed = _claimed(record={"launches": [_entry("l-old")]})
    claimed["mandate"]["quota_waker"] = {"retries": 1, "state": "requeued"}
    claim_env["claim"].return_value = claimed
    claim_env["outcomes"].return_value = {"l-old": {"state": "ready"}}
    assert await worker.claim_daemon_ticket("d1") is not None
    claim_env["release"].assert_not_awaited()


@pytest.mark.asyncio
async def test_the_same_work_still_dedupes(monkeypatch, claim_env):
    monkeypatch.setenv(dedup.ENV_MODE, "enforce")
    claimed = _claimed(record={"launches": [_entry("l-old", work=dedup.work_token({}, "i1"))]})
    claim_env["claim"].return_value = claimed
    claim_env["outcomes"].return_value = {"l-old": {"state": "ready"}}
    claim_env["work"].return_value = ("i1", False)
    assert await worker.claim_daemon_ticket("d1") is None
    claim_env["release"].assert_awaited_once()


# --- shadow is neutral --------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_record_leaves_the_revision_alone(monkeypatch, claim_env):
    monkeypatch.delenv(dedup.ENV_MODE, raising=False)
    claim_env["claim"].return_value = _claimed(record={"launches": [_entry("l-old")]})
    claim_env["outcomes"].return_value = {"l-old": {"state": "ready"}}
    await worker.claim_daemon_ticket("d1")
    assert claim_env["mandate_write"].call_args.kwargs["touch"] is False


@pytest.mark.asyncio
async def test_enforce_launch_record_still_moves_the_revision(monkeypatch, claim_env):
    monkeypatch.setenv(dedup.ENV_MODE, "enforce")
    claim_env["claim"].return_value = _claimed(record={"launches": []})
    await worker.claim_daemon_ticket("d1")
    assert claim_env["mandate_write"].call_args.kwargs["touch"] is True


class _Cursor:
    def __init__(self, sink):
        self.sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params):
        self.sink.append((sql, params))

    async def fetchone(self):
        return None


class _Conn:
    def __init__(self, sink):
        self.sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def cursor(self, **_kw):
        return _Cursor(self.sink)


@pytest.mark.asyncio
@pytest.mark.parametrize("touch", [True, False])
async def test_update_mandate_record_touch_controls_the_revision_bump(monkeypatch, touch):
    from src.server.database.autonomy import tickets as tickets_db

    sink: list = []
    monkeypatch.setattr(tickets_db, "get_db_connection", lambda: _Conn(sink))
    kwargs = {} if touch else {"touch": False}
    await tickets_db.update_mandate_record("t1", "launch_dedup", set_fields={"a": 1}, **kwargs)
    [(sql, _params)] = sink
    assert ("revision = revision + 1" in sql) is touch
    assert ("updated_at = NOW()" in sql) is touch


# --- resume after cancel --------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "enforce", "off"])
async def test_resume_forces_the_next_daemon_claim(monkeypatch, mode):
    from src.server.services.autonomy import ownership

    if mode is None:
        monkeypatch.delenv(dedup.ENV_MODE, raising=False)
    else:
        monkeypatch.setenv(dedup.ENV_MODE, mode)
    row = {
        "ticket_id": "t1",
        "user_id": "u1",
        "status": "cancelled",
        "worker_daemon_id": "d1",
        "mandate": {"source_id": "s1", _KEY: {"launches": [_entry("l-old")]}},
        "revision": 4,
    }
    update = AsyncMock(return_value={**row, "status": "queued"})
    monkeypatch.setattr(ownership, "_owned_ticket", AsyncMock(return_value=row))
    monkeypatch.setattr(ownership, "_settle_daemon_release", AsyncMock(return_value=row))
    monkeypatch.setattr(ownership, "_refuse_if_mandate_revoked", AsyncMock())
    monkeypatch.setattr(ownership, "_reenqueue_launch", AsyncMock())
    monkeypatch.setattr(ownership.ticket_db, "update_ticket", update)
    await ownership.resume_ticket(user_id="u1", ticket_id="t1")
    kwargs = update.call_args.kwargs
    assert kwargs["expected_revision"] == 4
    if mode == "off":
        assert "mandate" not in kwargs
    else:
        assert dedup.force_requested(kwargs["mandate"])
        assert kwargs["mandate"]["source_id"] == "s1"
        assert kwargs["mandate"][_KEY]["launches"][0]["lease_id"] == "l-old"
