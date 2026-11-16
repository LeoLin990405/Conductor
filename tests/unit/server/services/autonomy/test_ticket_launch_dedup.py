"""Worker launches de-duplicate by (ticket or issue, line, base commit)."""

from __future__ import annotations

import logging
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy import launch_dedup as dedup
from src.server.services.autonomy.host import AutonomyToolHost

_MOD = "src.server.services.autonomy.host"
_THREAD = "github:acme/repo#59"


def _ticket(ticket_id, status, *, provider="glm", model="glm-5", base="", thread=_THREAD, **extra):
    row = {
        "ticket_id": ticket_id,
        "user_id": "u-1",
        "status": status,
        "origin_thread_key": thread,
        "worker_provider": provider,
        "worker_model": model,
        "worktree_base_sha": base,
        "mandate": {},
    }
    row.update(extra)
    return row


# --- pure rules -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, "shadow"), ("", "shadow"), ("off", "off"), ("shadow", "shadow"), ("enforce", "enforce")],
)
def test_mode_reads_exact_spellings(raw, expected):
    env = {} if raw is None else {dedup.ENV_MODE: raw}
    assert dedup.dedup_mode(env) == expected


@pytest.mark.parametrize("raw", ["ENFORCE", "on", " enforce", "true"])
def test_unknown_mode_is_shadow_with_a_warning(raw, caplog):
    with caplog.at_level(logging.WARNING, logger=dedup.__name__):
        assert dedup.dedup_mode({dedup.ENV_MODE: raw}) == "shadow"
    assert dedup.ENV_MODE in caplog.text


def test_key_uses_issue_line_and_base():
    key = dedup.launch_key(_ticket("t-1", "queued", provider=" GLM ", model="GLM-5", base="abc"))
    assert key == dedup.LaunchKey(f"issue:{_THREAD}", "glm", "glm-5", "abc")
    bare = dedup.launch_key(_ticket("t-2", "queued", thread=""))
    assert bare.subject == "ticket:t-2"


def test_force_is_only_an_explicit_true():
    assert dedup.is_forced({"force": True})
    assert not dedup.is_forced({"force": "yes"})
    assert not dedup.is_forced({})
    assert not dedup.is_forced(None)


def test_in_flight_and_done_priors_match_failed_does_not():
    key = dedup.LaunchKey(f"issue:{_THREAD}", "glm", "glm-5", "abc")
    for status, state in (("running", "in_flight"), ("queued", "in_flight"), ("completed", "done")):
        decision = dedup.decide(key, [_ticket("old", status, base="abc")], mode="enforce")
        assert decision.duplicate and decision.skip
        assert decision.existing_ticket_id == "old"
        assert decision.existing_state == state
    for status in ("failed", "cancelled", "archived"):
        assert not dedup.decide(key, [_ticket("old", status)], mode="enforce").duplicate
    closed = _ticket("old", "running", closed_at="2026-11-14T00:00:00Z")
    assert not dedup.decide(key, [closed], mode="enforce").duplicate


def test_other_line_or_other_base_is_not_a_duplicate():
    key = dedup.LaunchKey(f"issue:{_THREAD}", "glm", "glm-5", "aaa")
    assert not dedup.decide(key, [_ticket("old", "running", provider="grok", model="grok-4")], mode="enforce").duplicate
    assert not dedup.decide(key, [_ticket("old", "running", base="bbb")], mode="enforce").duplicate
    exact = dedup.decide(key, [_ticket("old", "running", base="aaa")], mode="enforce")
    assert exact.duplicate and exact.base_match == "exact"
    unpinned = dedup.decide(key, [_ticket("old", "running")], mode="enforce")
    assert unpinned.duplicate and unpinned.base_match == "unpinned"


def test_unpinned_base_never_matches_a_done_prior():
    """A new request on a finished issue is not blocked."""
    unpinned = dedup.launch_key({}, provider="glm", model="glm-5", thread_key=_THREAD)
    assert not dedup.decide(unpinned, [_ticket("old", "completed", base="abc")], mode="enforce").duplicate
    pinned = dedup.LaunchKey(f"issue:{_THREAD}", "glm", "glm-5", "abc")
    assert not dedup.decide(pinned, [_ticket("old", "completed")], mode="enforce").duplicate


def test_in_flight_prior_wins_over_done_and_self_is_excluded():
    key = dedup.launch_key({}, provider="glm", model="glm-5", thread_key=_THREAD)
    decision = dedup.decide(
        dedup.LaunchKey(key.subject, "glm", "glm-5", "abc"),
        [_ticket("done", "completed", base="abc"), _ticket("live", "running")],
        mode="enforce",
    )
    assert decision.existing_ticket_id == "live"
    assert not dedup.decide(
        key, [_ticket("me", "running")], mode="enforce", exclude_ticket_id="me"
    ).duplicate


def test_shadow_launches_enforce_skips_force_launches_off_ignores():
    key = dedup.launch_key({}, provider="glm", model="glm-5", thread_key=_THREAD)
    priors = [_ticket("old", "running")]
    shadow = dedup.decide(key, priors, mode="shadow")
    assert shadow.duplicate and not shadow.skip
    assert shadow.record()["would_dedupe"] is True
    forced = dedup.decide(key, priors, mode="enforce", force=True)
    assert forced.duplicate and not forced.skip
    assert forced.record()["would_dedupe"] is False and forced.record()["forced"] is True
    assert not dedup.decide(key, priors, mode="off").duplicate


def test_record_history_is_bounded_and_counts_would_dedupe():
    key = dedup.LaunchKey("issue:x", "glm", "glm-5")
    mandate: dict = {}
    for _ in range(dedup.MAX_HISTORY + 5):
        entry = dedup.DedupDecision("shadow", "launch", True, key, existing_ticket_id="old").record()
        mandate = {dedup.LAUNCH_DEDUP_MANDATE_KEY: dedup.merge_record(mandate, entry)}
    record = mandate[dedup.LAUNCH_DEDUP_MANDATE_KEY]
    assert len(record["history"]) == dedup.MAX_HISTORY
    assert record["would_dedupe_count"] == dedup.MAX_HISTORY
    assert record["version"] == 1 and record["last"]["existing_ticket_id"] == "old"


@pytest.mark.asyncio
async def test_thread_check_ignores_other_users_and_other_lines():
    rows = [
        _ticket("mine-other-line", "running", provider="grok", model="grok-4"),
        {**_ticket("theirs", "running"), "user_id": "u-2"},
        _ticket("mine", "running"),
    ]
    get_ticket = AsyncMock(side_effect=lambda tid: next(r for r in rows if r["ticket_id"] == tid))
    decision = await dedup.check_thread_launch(
        thread_key=_THREAD, user_id="u-1", provider="glm", model="glm-5", mode="enforce",
        list_thread=AsyncMock(return_value=rows), get_ticket=get_ticket,
    )
    assert decision.existing_ticket_id == "mine" and decision.existing_state == "in_flight"
    get_ticket.assert_awaited_once_with("mine")


# --- Router accept hook -----------------------------------------------------


def _route_patches(*, priors, decide, enqueue, get_ticket=None, extra=()):
    trigger = {
        "trigger_id": "tr-9",
        "source_id": "s-1",
        "user_id": "u-1",
        "thread_key": _THREAD,
        "actor_login": "alice",
        "actor_is_bot": False,
        "addressed": True,
        "text": "please fix #59",
    }
    source = {
        "source_id": "s-1",
        "user_id": "u-1",
        "kind": "github",
        "config": {
            "workspace_id": "w-1",
            "allowed_logins": ["alice"],
            "worker_provider": "glm",
            "worker_model": "glm-5",
            "worker_daemon_id": "daemon-1",
        },
    }
    by_id = {row["ticket_id"]: row for row in priors}
    return [
        patch(f"{_MOD}.eval_db.active_eval_for_trigger", AsyncMock(return_value={"eval_id": "eval-9"})),
        patch(f"{_MOD}.trigger_db.get_trigger", AsyncMock(return_value=trigger)),
        patch(f"{_MOD}.source_db.get_source_any_user", AsyncMock(return_value=source)),
        patch(
            f"{_MOD}.policy.current_policy",
            AsyncMock(return_value={"accept_mode": "explicit", "version": "v1", "rules": []}),
        ),
        patch(f"{_MOD}.clarify_enabled", return_value=False),
        patch(f"{_MOD}.gate", AsyncMock(return_value="passed")),
        patch(f"{_MOD}.hold_gate", AsyncMock(return_value="passed")),
        patch(f"{_MOD}._record_router_route_decision", AsyncMock()),
        patch(f"{_MOD}.eval_db.decide_eval", decide),
        patch(f"{_MOD}.enqueue_effect", enqueue),
        patch(f"{_MOD}.ticket_db.list_tickets_for_thread", AsyncMock(return_value=list(priors))),
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            get_ticket or AsyncMock(side_effect=lambda tid, **_: by_id.get(tid)),
        ),
        *extra,
    ]


async def _accept(monkeypatch, mode, **kwargs):
    if mode is None:
        monkeypatch.delenv(dedup.ENV_MODE, raising=False)
    else:
        monkeypatch.setenv(dedup.ENV_MODE, mode)
    with ExitStack() as stack:
        for patcher in _route_patches(**kwargs):
            stack.enter_context(patcher)
        return await AutonomyToolHost(user_id="u-1", thread_id="router").route(
            trigger_id="tr-9", decision="accept", title="Fix #59", scope="Fix"
        )


def _new_ticket_decide():
    return AsyncMock(return_value={"eval_id": "eval-9", "ticket_id": "t-new"})


@pytest.mark.asyncio
async def test_off_is_unchanged_and_reads_nothing(monkeypatch):
    decide, enqueue = _new_ticket_decide(), AsyncMock(return_value=True)
    get_ticket = AsyncMock(side_effect=AssertionError("off must not read priors"))
    result = await _accept(
        monkeypatch, "off", priors=[_ticket("t-old", "running")], decide=decide,
        enqueue=enqueue, get_ticket=get_ticket,
    )
    assert result["decision"] == "accept" and result["ticket_id"] == "t-new"
    assert dedup.LAUNCH_DEDUP_MANDATE_KEY not in decide.call_args.kwargs["ticket"]["mandate"]
    enqueue.assert_awaited_once()
    assert enqueue.call_args.kwargs["idempotency_key"] == "autonomy:launch:t-new"


@pytest.mark.asyncio
async def test_shadow_default_records_would_dedupe_and_still_launches(monkeypatch, caplog):
    decide, enqueue = _new_ticket_decide(), AsyncMock(return_value=True)
    with caplog.at_level(logging.INFO, logger=dedup.__name__):
        result = await _accept(
            monkeypatch, None, priors=[_ticket("t-old", "queued", base="abc")],
            decide=decide, enqueue=enqueue,
        )
    assert result["decision"] == "accept" and result["ticket_id"] == "t-new"
    record = decide.call_args.kwargs["ticket"]["mandate"][dedup.LAUNCH_DEDUP_MANDATE_KEY]
    assert record["last"]["would_dedupe"] is True
    assert record["last"]["existing_ticket_id"] == "t-old"
    assert record["last"]["mode"] == "shadow"
    assert record["last"]["key"] == {
        "subject": f"issue:{_THREAD}", "provider": "glm", "model": "glm-5", "base": "",
    }
    enqueue.assert_awaited_once()
    assert "launch dedup" in caplog.text


@pytest.mark.asyncio
async def test_shadow_without_a_prior_writes_no_record(monkeypatch):
    decide, enqueue = _new_ticket_decide(), AsyncMock(return_value=True)
    await _accept(
        monkeypatch, "shadow",
        priors=[_ticket("t-other", "running", provider="grok", model="grok-4")],
        decide=decide, enqueue=enqueue,
    )
    assert dedup.LAUNCH_DEDUP_MANDATE_KEY not in decide.call_args.kwargs["ticket"]["mandate"]
    enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_enforce_attaches_to_the_in_flight_ticket_instead_of_a_new_one(monkeypatch):
    decide = AsyncMock(return_value={"eval_id": "eval-9"})
    enqueue = AsyncMock(return_value=True)
    add_sub = AsyncMock()
    add_input = AsyncMock(return_value={"input_id": "in-1"})
    live = _ticket("t-live", "running", worker_thread_id="wt-1")
    result = await _accept(
        monkeypatch, "enforce", priors=[live], decide=decide, enqueue=enqueue,
        extra=(
            patch(f"{_MOD}.ticket_db.add_subscription", add_sub),
            patch(f"{_MOD}.ticket_db.add_input", add_input),
        ),
    )
    assert result == {"decision": "attach", "ticket_id": "t-live"}
    assert decide.call_args.kwargs["decision"] == "attach"
    assert "ticket" not in decide.call_args.kwargs
    enqueue.assert_awaited_once()
    assert enqueue.call_args.kwargs["kind"] == "deliver_input"


@pytest.mark.asyncio
async def test_enforce_launches_a_new_request_on_a_finished_issue(monkeypatch):
    decide, enqueue = _new_ticket_decide(), AsyncMock(return_value=True)
    record = AsyncMock(side_effect=AssertionError("a done prior is not touched"))
    result = await _accept(
        monkeypatch, "enforce", priors=[_ticket("t-done", "completed", base="abc")],
        decide=decide, enqueue=enqueue,
        extra=(patch(f"{_MOD}.ticket_db.update_mandate_record", record),),
    )
    assert result["decision"] == "accept" and result["ticket_id"] == "t-new"
    assert dedup.LAUNCH_DEDUP_MANDATE_KEY not in decide.call_args.kwargs["ticket"]["mandate"]
    enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_enforce_launches_a_new_line(monkeypatch):
    decide, enqueue = _new_ticket_decide(), AsyncMock(return_value=True)
    result = await _accept(
        monkeypatch, "enforce",
        priors=[_ticket("t-grok", "running", provider="grok", model="grok-4")],
        decide=decide, enqueue=enqueue,
    )
    assert result["decision"] == "accept"
    enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_lookup_failure_launches_as_before(monkeypatch):
    decide, enqueue = _new_ticket_decide(), AsyncMock(return_value=True)
    result = await _accept(
        monkeypatch, "enforce", priors=[_ticket("t-old", "running")], decide=decide,
        enqueue=enqueue, get_ticket=AsyncMock(side_effect=RuntimeError("db down")),
    )
    assert result["decision"] == "accept"
    enqueue.assert_awaited_once()
