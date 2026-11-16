"""Owner and deadline decisions for one unfinished dispatch line (N76 §4.1)."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest

from src.tools.auto_dispatch.liveness.owner import (
    DEFAULT_QUEUED_TIMEOUT_SECONDS,
    DEFAULT_REVIEW_TIMEOUT_SECONDS,
    DEFAULT_WORKER_TIMEOUT_SECONDS,
    LIVENESS_KEY,
    NEEDS_HUMAN,
    NONE,
    OWNER_KINDS,
    OWNERS,
    QUEUED_REQUEST,
    RECOVERY_TIMER,
    SCHEDULED_REVIEW,
    WORKER_RUN,
    OwnerDecision,
    decide_owner,
    format_time,
    needs_recovery,
    parse_time,
)

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def _snapshot(**overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "id": "line-0",
        "status": "BLOCKED",
        "current_run": None,
    }
    raw.update(overrides)
    return raw


def _liveness(**overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "reason": "no_new_commit",
        "group": "R-EXIT",
        "parked_at": "2026-11-12T11:55:00+00:00",
        "updated_at": "2026-11-12T11:55:00+00:00",
        "budget": 4,
        "spent": 1,
        "history": [],
        "outcome": "pending",
    }
    raw.update(overrides)
    return raw


def test_contract_names_are_the_prd_vocabulary() -> None:
    assert OWNERS == {
        "worker_run",
        "scheduled_review",
        "recovery_timer",
        "queued_request",
        "needs_human",
        "none",
        # N80 L4 (n80-collab-protocol.md §7): an app's lease on a dispatch line.
        "app_lease",
    }
    assert OWNER_KINDS is OWNERS
    assert WORKER_RUN == "worker_run"
    assert SCHEDULED_REVIEW == "scheduled_review"
    assert RECOVERY_TIMER == "recovery_timer"
    assert QUEUED_REQUEST == "queued_request"
    assert NEEDS_HUMAN == "needs_human"
    assert NONE == "none"
    assert LIVENESS_KEY == "liveness"
    assert DEFAULT_WORKER_TIMEOUT_SECONDS == 600
    assert DEFAULT_REVIEW_TIMEOUT_SECONDS == 900
    assert DEFAULT_QUEUED_TIMEOUT_SECONDS == 600


@pytest.mark.parametrize("status", ["PASSED", "FAILED", "CANCELLED", "COMPLETED"])
def test_a_terminal_line_has_no_owner_and_is_not_ownerless(status: str) -> None:
    decision = decide_owner(_snapshot(status=status), NOW, env={})

    assert decision.owner == NONE
    assert decision.terminal
    assert not decision.ownerless
    assert decision.deadline is None
    assert not needs_recovery(decision)


def test_a_blocked_line_with_no_owner_is_ownerless() -> None:
    decision = decide_owner(_snapshot(), NOW, env={})

    assert decision == OwnerDecision(
        owner=NONE,
        ownerless=True,
        reason="no owner",
    )
    assert not decision.terminal
    assert not decision.expired
    assert needs_recovery(decision)


def test_a_live_worker_run_owns_the_line_until_its_deadline() -> None:
    deadline = NOW + timedelta(minutes=30)
    decision = decide_owner(
        _snapshot(
            current_run="run-1",
            run_alive=True,
            worker_deadline=deadline.isoformat(),
        ),
        NOW,
        env={},
    )

    assert decision.owner == WORKER_RUN
    assert decision.deadline == deadline
    assert not decision.expired
    assert not decision.ownerless
    assert not needs_recovery(decision)


def test_a_worker_start_derives_the_worker_timeout() -> None:
    decision = decide_owner(
        _snapshot(
            current_run="run-1",
            run_alive=True,
            run_started_at=NOW.isoformat(),
        ),
        NOW,
        env={},
    )

    assert decision.deadline == NOW + timedelta(seconds=600)
    assert not decision.expired


def test_a_worker_started_at_is_read_from_the_line_snapshot() -> None:
    decision = decide_owner(
        _snapshot(
            current_run="run-1",
            run_alive=True,
            worker_started_at=NOW.isoformat(),
        ),
        NOW,
        env={},
    )

    assert decision.deadline == NOW + timedelta(seconds=600)


def test_the_worker_timeout_environment_is_read_and_invalid_values_fall_back() -> None:
    snapshot = _snapshot(
        current_run="run-1",
        run_alive=True,
        run_started_at=NOW.isoformat(),
    )

    shortened = decide_owner(snapshot, NOW, env={"CONDUCTOR_DISPATCH_TIMEOUT_SECONDS": "120"})
    invalid = decide_owner(snapshot, NOW, env={"CONDUCTOR_DISPATCH_TIMEOUT_SECONDS": "nope"})

    assert shortened.deadline == NOW + timedelta(seconds=120)
    assert invalid.deadline == NOW + timedelta(seconds=600)


def test_a_run_id_whose_launch_died_is_not_an_owner() -> None:
    decision = decide_owner(
        _snapshot(current_run="run-1", run_alive=False),
        NOW,
        env={},
    )

    assert decision.owner == NONE
    assert decision.ownerless
    assert needs_recovery(decision)


def test_a_stale_run_id_does_not_mask_a_recovery_timer() -> None:
    next_at = NOW + timedelta(hours=1)
    decision = decide_owner(
        _snapshot(
            current_run="stale",
            run_started_at=NOW.isoformat(),
            liveness=_liveness(next_at=next_at.isoformat(), next_action="reassign"),
        ),
        NOW,
        env={},
    )

    assert decision.owner == RECOVERY_TIMER
    assert decision.deadline == next_at
    assert not decision.expired
    assert not decision.ownerless


def test_a_bare_pid_is_identity_not_liveness_evidence() -> None:
    decision = decide_owner(
        _snapshot(current_run={"id": "run-1", "pid": 4242}),
        NOW,
        env={},
    )

    assert decision.owner == NONE
    assert decision.ownerless
    assert needs_recovery(decision)


def test_a_launch_that_only_carries_a_pid_is_not_alive() -> None:
    decision = decide_owner(
        _snapshot(current_run="run-1", launch={"pid": 4242}),
        NOW,
        env={},
    )

    assert decision.owner == NONE
    assert decision.ownerless


@pytest.mark.parametrize(
    "snapshot",
    [
        {"current_run": {"id": "run-1", "alive": True}},
        {"current_run": "run-1", "launch": {"state": "running"}},
    ],
)
def test_positive_launch_liveness_evidence_still_owns_the_line(
    snapshot: dict[str, object],
) -> None:
    decision = decide_owner(
        _snapshot(
            status="RUNNING",
            worker_deadline=(NOW + timedelta(minutes=5)).isoformat(),
            **snapshot,
        ),
        NOW,
        env={},
    )

    assert decision.owner == WORKER_RUN
    assert not decision.expired


def test_a_live_run_without_deadline_evidence_is_expired_but_not_ownerless() -> None:
    decision = decide_owner(
        _snapshot(current_run="run-1", run_alive=True),
        NOW,
        env={},
    )

    assert decision.owner == WORKER_RUN
    assert decision.deadline is None
    # A deadline invented from ``now`` would move forward every tick; the
    # sweep must recover this run instead.
    assert decision.expired
    assert not decision.ownerless
    assert needs_recovery(decision)


def test_a_scheduled_review_owns_the_line_until_next_at() -> None:
    next_at = NOW + timedelta(seconds=60)
    decision = decide_owner(
        _snapshot(
            status="RUNNING",
            review_retry={"next_at": next_at.isoformat(), "attempts": 1},
        ),
        NOW,
        env={},
    )

    assert decision.owner == SCHEDULED_REVIEW
    assert decision.deadline == next_at
    assert not decision.expired
    assert not needs_recovery(decision)


def test_a_past_scheduled_review_is_reported_expired() -> None:
    next_at = NOW - timedelta(seconds=1)
    decision = decide_owner(
        _snapshot(review_retry={"next_at": next_at.isoformat()}),
        NOW,
        env={},
    )

    assert decision.owner == SCHEDULED_REVIEW
    assert decision.deadline == next_at
    assert decision.expired
    assert needs_recovery(decision)


def test_a_running_review_derives_the_review_timeout_from_its_start() -> None:
    decision = decide_owner(
        _snapshot(
            status="REVIEWING",
            review_running=True,
            review_started_at=NOW.isoformat(),
        ),
        NOW,
        env={},
    )

    assert decision.owner == SCHEDULED_REVIEW
    assert decision.deadline == NOW + timedelta(seconds=900)
    assert not decision.expired


def test_a_running_review_ignores_a_stale_scheduled_retry() -> None:
    decision = decide_owner(
        _snapshot(
            status="REVIEWING",
            review_running=True,
            review_retry={"next_at": (NOW - timedelta(minutes=5)).isoformat()},
            review_started_at=NOW.isoformat(),
        ),
        NOW,
        env={},
    )

    assert decision.deadline == NOW + timedelta(seconds=900)
    assert not decision.expired


def test_a_running_review_uses_its_active_mapping_not_a_stale_retry() -> None:
    decision = decide_owner(
        _snapshot(
            status="REVIEWING",
            review={"running": True, "started_at": NOW.isoformat()},
            review_retry={"next_at": (NOW - timedelta(minutes=5)).isoformat()},
        ),
        NOW,
        env={},
    )

    assert decision.owner == SCHEDULED_REVIEW
    assert decision.deadline == NOW + timedelta(
        seconds=DEFAULT_REVIEW_TIMEOUT_SECONDS
    )
    assert decision.deadline_text == "2026-11-12T12:15:00+00:00"
    assert not decision.expired
    assert not needs_recovery(decision)


def test_a_running_review_uses_its_own_deadline_over_a_stale_retry() -> None:
    deadline = NOW + timedelta(minutes=3)
    decision = decide_owner(
        _snapshot(
            status="REVIEWING",
            review={
                "state": "running",
                "started_at": NOW.isoformat(),
                "deadline": deadline.isoformat(),
            },
            review_retry={"next_at": (NOW - timedelta(minutes=5)).isoformat()},
        ),
        NOW,
        env={},
    )

    assert decision.owner == SCHEDULED_REVIEW
    assert decision.deadline == deadline
    assert not decision.expired


def test_the_review_timeout_environment_is_read() -> None:
    decision = decide_owner(
        _snapshot(
            status="REVIEWING",
            review_running=True,
            review_started_at=NOW.isoformat(),
        ),
        NOW,
        env={"CONDUCTOR_DISPATCH_REVIEW_TIMEOUT_SECONDS": "120"},
    )

    assert decision.deadline == NOW + timedelta(seconds=120)


def test_a_recovery_timer_owns_the_line_until_next_at() -> None:
    next_at = NOW + timedelta(minutes=2)
    decision = decide_owner(
        _snapshot(
            liveness=_liveness(
                next_at=next_at.isoformat(),
                next_action="reassign",
            )
        ),
        NOW,
        env={},
    )

    assert decision.owner == RECOVERY_TIMER
    assert decision.deadline == next_at
    assert decision.reason == "reassign"
    assert not decision.expired


def test_a_past_recovery_timer_is_reported_expired() -> None:
    decision = decide_owner(
        _snapshot(
            liveness=_liveness(
                next_at=(NOW - timedelta(seconds=1)).isoformat(),
                next_action="review",
            )
        ),
        NOW,
        env={},
    )

    assert decision.owner == RECOVERY_TIMER
    assert decision.expired
    assert needs_recovery(decision)


def test_a_queued_request_owns_the_line_with_a_derived_deadline() -> None:
    decision = decide_owner(
        _snapshot(
            queued_request={
                "id": "request-1",
                "state": "queued",
                "requested_at": NOW.isoformat(),
            }
        ),
        NOW,
        env={},
    )

    assert decision.owner == QUEUED_REQUEST
    assert decision.deadline == NOW + timedelta(seconds=600)
    assert not decision.expired


def test_a_queued_request_uses_an_explicit_deadline() -> None:
    deadline = NOW + timedelta(minutes=1)
    decision = decide_owner(
        _snapshot(queued_request={"id": "request-1", "deadline": deadline.isoformat()}),
        NOW,
        env={},
    )

    assert decision.owner == QUEUED_REQUEST
    assert decision.deadline == deadline


def test_a_boolean_queued_request_reads_the_top_level_start() -> None:
    decision = decide_owner(
        _snapshot(queued_request=True, queued_at=NOW.isoformat()),
        NOW,
        env={},
    )

    assert decision.owner == QUEUED_REQUEST
    assert decision.deadline == NOW + timedelta(seconds=600)
    assert not decision.expired


def test_a_completed_queued_request_is_not_an_owner() -> None:
    decision = decide_owner(
        _snapshot(queued_request={"id": "request-1", "state": "done"}),
        NOW,
        env={},
    )

    assert decision.owner == NONE
    assert decision.ownerless


def test_needs_human_latches_with_no_deadline() -> None:
    decision = decide_owner(
        _snapshot(
            liveness=_liveness(
                needs_human=True,
                human_reason="handoff_incomplete",
                next_at=(NOW + timedelta(minutes=5)).isoformat(),
                outcome="needs_human",
            )
        ),
        NOW,
        env={},
    )

    assert decision.owner == NEEDS_HUMAN
    assert decision.deadline is None
    assert not decision.expired
    assert not decision.ownerless
    assert decision.reason == "handoff_incomplete"
    assert not needs_recovery(decision)


def test_a_top_level_needs_human_latch_is_read_too() -> None:
    decision = decide_owner(
        _snapshot(needs_human=True, human_reason="reviewer_conflict_scope"),
        NOW,
        env={},
    )

    assert decision.owner == NEEDS_HUMAN
    assert decision.deadline is None
    assert decision.reason == "reviewer_conflict_scope"


def test_a_string_needs_human_latch_supplies_the_reason() -> None:
    decision = decide_owner(
        _snapshot(liveness=_liveness(needs_human="handoff_incomplete")),
        NOW,
        env={},
    )

    assert decision.owner == NEEDS_HUMAN
    assert decision.reason == "handoff_incomplete"
    assert decision.deadline is None


def test_precedence_human_then_request_then_worker_then_review_then_timer() -> None:
    deadline = NOW + timedelta(minutes=30)
    snapshot = _snapshot(
        status="RUNNING",
        current_run="run-1",
        run_alive=True,
        worker_deadline=deadline.isoformat(),
        review_running=True,
        review_started_at=NOW.isoformat(),
        review_retry={"next_at": deadline.isoformat()},
        queued_request={"id": "request-1", "requested_at": NOW.isoformat()},
        liveness=_liveness(
            needs_human=True,
            human_reason="handoff_incomplete",
            next_at=deadline.isoformat(),
        ),
    )

    assert decide_owner(snapshot, NOW, env={}).owner == NEEDS_HUMAN

    snapshot["liveness"] = _liveness(next_at=deadline.isoformat())
    assert decide_owner(snapshot, NOW, env={}).owner == QUEUED_REQUEST

    del snapshot["queued_request"]
    assert decide_owner(snapshot, NOW, env={}).owner == WORKER_RUN

    snapshot["run_alive"] = False
    assert decide_owner(snapshot, NOW, env={}).owner == SCHEDULED_REVIEW

    snapshot["review_running"] = False
    del snapshot["review_retry"]
    decision = decide_owner(snapshot, NOW, env={})
    assert decision.owner == RECOVERY_TIMER

    del snapshot["liveness"]
    decision = decide_owner(snapshot, NOW, env={})
    assert decision.owner == NONE
    assert decision.ownerless


def test_the_snapshot_is_never_mutated() -> None:
    snapshot = _snapshot(
        current_run="run-1",
        run_alive=True,
        liveness=_liveness(
            needs_human=True,
            human_reason="handoff_incomplete",
            history=[{"step": 1, "action": "reassign"}],
        ),
    )
    before = copy.deepcopy(snapshot)

    decide_owner(snapshot, NOW, env={})

    assert snapshot == before


def test_now_accepts_an_iso_string() -> None:
    deadline = NOW + timedelta(minutes=5)
    decision = decide_owner(
        _snapshot(review_retry={"next_at": deadline.isoformat()}),
        NOW.isoformat(),
        env={},
    )

    assert decision.deadline == deadline
    assert not decision.expired


def test_an_unknown_status_is_treated_as_unfinished() -> None:
    decision = decide_owner(_snapshot(status="MYSTERY"), NOW, env={})

    assert decision.owner == NONE
    assert decision.ownerless
    assert not decision.terminal


def test_same_inputs_return_an_equal_decision() -> None:
    snapshot = _snapshot(
        current_run="run-1",
        run_alive=True,
        run_started_at=NOW.isoformat(),
    )

    assert decide_owner(snapshot, NOW, env={}) == decide_owner(snapshot, NOW, env={})


def test_a_past_deadline_is_reported_expired() -> None:
    decision = decide_owner(
        _snapshot(
            current_run="run-1",
            run_alive=True,
            worker_deadline=(NOW - timedelta(seconds=1)).isoformat(),
        ),
        NOW,
        env={},
    )

    assert decision.owner == WORKER_RUN
    assert decision.deadline == NOW - timedelta(seconds=1)
    assert decision.expired
    assert needs_recovery(decision)


def test_needs_recovery_never_takes_a_human_or_terminal_line() -> None:
    human = OwnerDecision(owner=NEEDS_HUMAN, reason="handoff_incomplete")
    terminal = OwnerDecision(owner=NONE, terminal=True)

    assert not needs_recovery(human)
    assert not needs_recovery(terminal)
    assert needs_recovery(OwnerDecision(owner=NONE, ownerless=True))


def test_to_dict_is_json_safe() -> None:
    decision = decide_owner(
        _snapshot(
            liveness=_liveness(
                next_at=(NOW + timedelta(minutes=2)).isoformat(),
                next_action="reassign",
            )
        ),
        NOW,
        env={},
    )

    assert decision.to_dict() == {
        "owner": RECOVERY_TIMER,
        "deadline": format_time(NOW + timedelta(minutes=2)),
        "expired": False,
        "ownerless": False,
        "reason": "reassign",
    }


def test_time_helpers_accept_epoch_and_iso_values() -> None:
    assert parse_time(NOW.timestamp()) == NOW
    assert parse_time(NOW.isoformat().replace("+00:00", "Z")) == NOW
    assert parse_time("not a time") is None
    assert format_time(NOW) == "2026-11-12T12:00:00+00:00"
