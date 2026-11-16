"""N80 L4: the ``app_lease`` owner, its consistency rows and its R-EXIT group."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest

from src.server.services.autonomy.stage_gate import Stage
from src.tools.auto_dispatch import app_lines
from src.tools.auto_dispatch.liveness import consistency, owner, policy
from src.tools.auto_dispatch.types import LineStatus

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
T = NOW.timestamp()
REASONS = (app_lines.REASON_EXPIRED, app_lines.REASON_RELEASED)


def _lease(**overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "app_id": "app-alpha",
        "state": "active",
        "expires_at": T + 120.0,
        "max_lease_until": T + 3600.0,
    }
    raw.update(overrides)
    return raw


def _snap(**overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {"status": "ASSIGNED", "updated_at": NOW.isoformat()}
    raw.update(overrides)
    return raw


def _decide(snapshot: dict[str, object]) -> owner.OwnerDecision:
    return owner.decide_owner(snapshot, NOW, env={})


def test_owner_type_is_in_the_vocabulary() -> None:
    assert owner.OWNER_APP_LEASE == "app_lease"
    assert owner.OWNER_APP_LEASE in owner.OWNERS


def test_live_lease_owns_the_line_until_the_lease_expiry() -> None:
    decision = _decide(_snap(app_lease=_lease()))
    assert decision.owner == owner.OWNER_APP_LEASE
    assert decision.deadline == NOW + timedelta(seconds=120)
    assert not decision.expired
    assert not owner.needs_recovery(decision, NOW)
    assert decision.detail == "app-alpha"


def test_deadline_is_the_earlier_of_expiry_and_max_lease() -> None:
    decision = _decide(_snap(app_lease=_lease(expires_at=T + 900, max_lease_until=T + 60)))
    assert decision.owner == owner.OWNER_APP_LEASE
    assert decision.deadline == NOW + timedelta(seconds=60)


def test_expired_lease_needs_recovery() -> None:
    decision = _decide(_snap(app_lease=_lease(expires_at=T - 1.0)))
    assert decision.owner == owner.OWNER_APP_LEASE
    assert decision.expired
    assert owner.needs_recovery(decision)
    assert owner.needs_recovery(decision, NOW)


def test_max_lease_past_expires_even_with_a_fresh_heartbeat() -> None:
    decision = _decide(_snap(app_lease=_lease(expires_at=T + 120, max_lease_until=T)))
    assert decision.owner == owner.OWNER_APP_LEASE
    assert decision.expired
    assert owner.needs_recovery(decision, NOW)


def test_reported_state_counts_as_live() -> None:
    decision = _decide(_snap(app_lease=_lease(state="reported")))
    assert decision.owner == owner.OWNER_APP_LEASE
    assert not decision.expired


def test_lease_comes_after_human_and_queued_but_before_a_live_worker() -> None:
    worker = {"id": "run-1", "alive": True, "started_at": NOW.isoformat()}
    assert _decide(_snap(app_lease=_lease(), current_run=worker)).owner == (
        owner.OWNER_APP_LEASE
    )
    assert _decide(_snap(app_lease=_lease(), needs_human=True)).owner == (
        owner.OWNER_NEEDS_HUMAN
    )
    queued = {"pending": True, "requested_at": NOW.isoformat()}
    assert _decide(_snap(app_lease=_lease(), queued_request=queued)).owner == (
        owner.OWNER_QUEUED_REQUEST
    )
    terminal = _decide(_snap(status="PASSED", app_lease=_lease()))
    assert terminal.terminal


_PLAIN = [
    _snap(),
    _snap(current_run={"id": "run-1", "alive": True, "started_at": NOW.isoformat()}),
    _snap(review={"state": "running", "started_at": NOW.isoformat()}),
    _snap(liveness={"reason": "no_new_commit", "next_at": (NOW + timedelta(minutes=5)).isoformat()}),
    _snap(queued_request={"pending": True, "requested_at": NOW.isoformat()}),
    _snap(needs_human=True),
]


@pytest.mark.parametrize(
    "bad",
    [
        _lease(state="expired"),
        _lease(state="released"),
        _lease(state=None),
        _lease(expires_at="soon", max_lease_until=None),
        _lease(expires_at=True, max_lease_until=False),
        _lease(expires_at=float("nan"), max_lease_until=None),
        "active",
        None,
    ],
)
@pytest.mark.parametrize("base", _PLAIN)
def test_malformed_or_dead_lease_is_ignored(base, bad) -> None:
    with_bad = dict(base, app_lease=bad)
    assert _decide(with_bad) == _decide(base)
    # The same snapshot with a live lease differs wherever the lease outranks
    # the base owner, which proves the comparison above is not vacuous.
    assert _decide(dict(_PLAIN[0], app_lease=_lease())).owner == owner.OWNER_APP_LEASE


def test_one_malformed_time_falls_back_to_the_valid_one() -> None:
    decision = _decide(_snap(app_lease=_lease(expires_at="x", max_lease_until=T + 30)))
    assert decision.owner == owner.OWNER_APP_LEASE
    assert decision.deadline == NOW + timedelta(seconds=30)


def test_snapshots_without_a_lease_keep_their_owners() -> None:
    expected = [
        owner.OWNER_NONE,
        owner.OWNER_WORKER_RUN,
        owner.OWNER_SCHEDULED_REVIEW,
        owner.OWNER_RECOVERY_TIMER,
        owner.OWNER_QUEUED_REQUEST,
        owner.OWNER_NEEDS_HUMAN,
    ]
    assert [_decide(s).owner for s in _PLAIN] == expected
    snap = _snap(app_lease=_lease())
    before = copy.deepcopy(snap)
    assert _decide(snap).owner == owner.OWNER_APP_LEASE
    assert snap == before


@pytest.mark.parametrize("reason", REASONS)
def test_consistency_rows_are_blocked(reason) -> None:
    assert consistency.REASON_STAGES[reason] == frozenset({Stage.BLOCKED})
    assert reason in consistency.KNOWN_REASONS
    assert reason in consistency.CONSISTENCY_TABLE
    assert isinstance(consistency.check(LineStatus.BLOCKED, reason, Stage.BLOCKED), consistency.Ok)


@pytest.mark.parametrize("reason", REASONS)
def test_consistency_assigned_stage_is_not_ok_but_repairable(reason) -> None:
    result = consistency.check(LineStatus.BLOCKED, reason, Stage.ASSIGNED)
    assert not isinstance(result, consistency.Ok)
    assert not result.consistent
    # A known park reason is repaired with the block event, not handed to a person.
    assert isinstance(result, consistency.Repair)


@pytest.mark.parametrize("reason", REASONS)
def test_policy_groups_both_reasons_as_r_exit(reason) -> None:
    assert policy.DEFAULT_REASON_GROUPS[reason] == policy.R_EXIT
    assert policy.default_policy().group_for(reason) == "R-EXIT"
