"""The liveness report is a pure summary plus two read-only loaders.

The fixture below is an inline dispatch export: one dispatch reached APPROVAL,
one aborted with a recovered park, a human park, a split-off line, and a
blocked line whose liveness record was never written. That is enough to pin
every PRD 4.6 number without a database, a network, or a clock.
"""

from __future__ import annotations

import builtins
import importlib.util
import json
import sys
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "liveness_report.py"
SPEC = importlib.util.spec_from_file_location("liveness_report", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
report_script = importlib.util.module_from_spec(SPEC)
# dataclasses resolves annotations through the module registry during exec.
sys.modules[SPEC.name] = report_script
SPEC.loader.exec_module(report_script)

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def _recovered_record() -> dict[str, object]:
    """Parked for five minutes, one applied reassign, then recovered."""
    return {
        "reason": "no_new_commit",
        "group": "R-EXIT",
        "parked_at": "2026-11-12T10:00:00+00:00",
        "updated_at": "2026-11-12T10:05:00+00:00",
        "budget": 4,
        "spent": 1,
        "episode": 1,
        "steps": [
            {
                "step": 1,
                "episode": 1,
                "action": "reassign",
                "reason": "no_new_commit",
                "at": "2026-11-12T10:01:00+00:00",
                "result": "applied",
            }
        ],
        "outcome": "recovered",
    }


def _human_record() -> dict[str, object]:
    """Parked for twenty minutes; a person handled it ten minutes in."""
    return {
        "reason": "review_unparseable",
        "group": "R-REVIEW",
        "parked_at": "2026-11-12T11:00:00+00:00",
        "updated_at": "2026-11-12T11:20:00+00:00",
        "budget": 4,
        "spent": 1,
        "episode": 1,
        "steps": [
            {
                "step": 1,
                "episode": 1,
                "action": "review",
                "reason": "review_unparseable",
                "at": "2026-11-12T11:01:00+00:00",
                "result": "applied",
            }
        ],
        "outcome": "needs_human",
        "needs_human": True,
        "human_reason": "the review never parsed",
        "human_at": "2026-11-12T11:10:00+00:00",
        "notified_at": "2026-11-12T11:10:00+00:00",
        "human_handled_at": "2026-11-12T11:20:00+00:00",
    }


def _dispatches() -> list[dict[str, object]]:
    return [
        {
            "id": "d1",
            "state": "APPROVAL",
            "created_at": "2026-11-12T08:00:00+00:00",
            "lines": [
                {"id": "line-0", "status": "PASSED", "failure_reason": None},
            ],
            "gates": {
                "approval": {"result": "pass"},
                "lines": {
                    "line-0": {
                        "machine": {"stage": "awaiting_human"},
                        "liveness": _recovered_record(),
                    }
                },
            },
        },
        {
            "id": "d2",
            "state": "ABORT",
            "created_at": "2026-11-12T09:00:00+00:00",
            "lines": [
                {
                    "id": "line-1",
                    "status": "BLOCKED",
                    "failure_reason": "review_unparseable",
                },
                {
                    "id": "line-2",
                    "status": "FAILED",
                    "failure_reason": "split_off",
                },
                {
                    "id": "line-3",
                    "status": "BLOCKED",
                    "failure_reason": "no_new_commit",
                },
            ],
            "gates": {
                "lines": {
                    "line-1": {
                        "machine": {"stage": "blocked"},
                        "review_retry": {"delay_seconds": 60},
                        "liveness": _human_record(),
                    },
                    "line-2": {"machine": {"stage": "primary_review"}},
                }
            },
        },
    ]


def test_summarize_answers_every_prd_46_question() -> None:
    report = report_script.summarize(_dispatches(), now=NOW)

    assert report["dispatches"] == {
        "total": 2,
        "approval": 1,
        "approval_ratio": 0.5,
    }
    parking = report["parking"]
    assert parking["total"] == 3
    assert parking["by_reason_counts"] == {
        "no_new_commit": 2,
        "review_unparseable": 1,
    }
    # line-3 has no record, so it counts but contributes no duration.
    assert parking["by_reason"]["no_new_commit"] == {
        "count": 2,
        "average_seconds": 300.0,
        "attempted": 1,
        "recovered": 1,
        "success_ratio": 1.0,
        "automatic": {
            "attempted": 1,
            "recovered": 1,
            "success_ratio": 1.0,
        },
    }
    # The human park runs from parked_at to the handling time, 20 minutes.
    assert parking["by_reason"]["review_unparseable"]["average_seconds"] == 1200.0
    assert parking["by_reason"]["review_unparseable"]["automatic"] == {
        "attempted": 1,
        "recovered": 0,
        "success_ratio": 0.0,
    }
    assert parking["average_seconds"] == 750.0
    assert parking["automatic"] == {
        "attempted": 2,
        "recovered": 1,
        "success_ratio": 0.5,
    }
    assert report["needs_human"] == {
        "total": 1,
        "open": 0,
        "average_seconds_to_handle": 600,
    }
    assert report["split_off"]["lines"] == 1
    assert report["review"] == {
        "unparseable": 1,
        "scheduled_retries": 1,
        "average_wait_seconds": 60.0,
    }


def test_a_blocked_line_without_a_record_is_still_counted() -> None:
    report = report_script.summarize(_dispatches(), now=NOW)
    assert report["parking"]["by_reason_counts"]["no_new_commit"] == 2


def test_a_rejected_dispatch_still_counts_as_having_reached_approval() -> None:
    # A rejection is written only after G6 opens; ``state`` then moves to
    # ABORT and the approval result becomes ``fail``. The dispatch still
    # reached APPROVAL, which is what the PRD denominator asks about.
    rejected = {
        "id": "d5",
        "state": "ABORT",
        "gates": {
            "approval": {
                "gate": "G6",
                "result": "fail",
                "token_present": True,
                "at": "2026-11-12T09:30:00+00:00",
            }
        },
    }

    report = report_script.summarize([rejected], now=NOW)

    assert report["dispatches"]["approval"] == 1
    assert report["dispatches"]["approval_ratio"] == 1.0


def test_a_dispatch_that_never_opened_g6_is_not_counted() -> None:
    # Every dispatch starts with this approval shell, so its presence alone
    # must not be mistaken for having reached APPROVAL.
    running = {
        "id": "d6",
        "state": "RUNNING",
        "gates": {
            "approval": {
                "gate": "G6",
                "result": "pending",
                "token_present": False,
                "at": None,
            }
        },
    }

    report = report_script.summarize([running], now=NOW)

    assert report["dispatches"]["approval"] == 0


def test_a_line_g6_snapshot_also_proves_approval_was_reached() -> None:
    row = {
        "id": "d7",
        "state": "ABORT",
        "gates": {
            "lines": {
                "line-0": {
                    "gates": {
                        "G6": {
                            "result": "fail",
                            "reason": "human_rejected",
                            "at": "2026-11-12T09:30:00+00:00",
                        }
                    }
                }
            }
        },
    }

    assert report_script.summarize([row], now=NOW)["dispatches"]["approval"] == 1


def test_each_earlier_parking_episode_keeps_its_own_reason() -> None:
    first = {
        "episode": 1,
        "reason": "no_new_commit",
        "parked_at": "2026-11-12T10:00:00+00:00",
        "resolved_at": "2026-11-12T11:00:00+00:00",
        "outcome": "recovered",
        "steps_applied": 1,
    }
    current = {
        "reason": "review_unparseable",
        "parked_at": "2026-11-12T11:30:00+00:00",
        "updated_at": "2026-11-12T11:30:00+00:00",
        "episode": 2,
        "episodes": [first],
        "outcome": "pending",
        "steps": [],
    }
    row = {
        "id": "d",
        "lines": [{"id": "line-0", "status": "BLOCKED"}],
        "gates": {"lines": {"line-0": {"liveness": current}}},
    }

    report = report_script.summarize([row], now=NOW)

    assert report["parking"]["total"] == 2
    assert report["parking"]["by_reason_counts"] == {
        "no_new_commit": 1,
        "review_unparseable": 1,
    }
    assert report["parking"]["by_reason"]["no_new_commit"]["average_seconds"] == 3600
    assert report["parking"]["by_reason"]["review_unparseable"]["average_seconds"] == 1800
    # The applied step belongs to the old episode, not to the current reason.
    assert report["parking"]["by_reason"]["review_unparseable"]["attempted"] == 0


def test_failed_automatic_attempts_count_against_the_success_rate() -> None:
    # The L1 producer records a refused/error attempt in ``history`` and
    # ``spent`` while ``steps_applied`` stays zero. One successful recovery
    # plus one failed attempt is 1/2, not 1/1.
    common = {"reason": "no_new_commit", "spent": 1}
    rows = [
        {
            "id": "d",
            "lines": [
                {
                    "id": "ok",
                    "liveness": dict(common, steps_applied=1, outcome="recovered"),
                },
                {
                    "id": "err",
                    "liveness": dict(
                        common,
                        steps_applied=0,
                        outcome="needs_human",
                        history=[{"episode": 1, "result": "error"}],
                    ),
                },
            ],
        }
    ]

    report = report_script.summarize(rows, now=NOW)

    expected = {"attempted": 2, "recovered": 1, "success_ratio": 0.5}
    assert report["parking"]["automatic"] == expected
    assert report["parking"]["by_reason"]["no_new_commit"]["automatic"] == expected


def test_a_refused_step_under_the_older_steps_key_is_an_attempt() -> None:
    row = {
        "id": "d",
        "lines": [
            {
                "id": "line-0",
                "liveness": {
                    "reason": "no_new_commit",
                    "spent": 1,
                    "episode": 1,
                    "steps": [{"episode": 1, "action": "reassign", "result": "refused"}],
                    "outcome": "needs_human",
                },
            }
        ],
    }

    automatic = report_script.summarize([row], now=NOW)["parking"]["automatic"]

    assert automatic == {"attempted": 1, "recovered": 0, "success_ratio": 0.0}


def test_spent_budget_counts_when_the_history_entry_was_evicted() -> None:
    # ``history`` is bounded; ``spent`` is monotonic. A single-episode record
    # that spent budget without applying anything has a failed attempt even
    # when the trail no longer holds the step.
    row = {
        "id": "d",
        "lines": [
            {
                "id": "line-0",
                "liveness": {
                    "reason": "no_new_commit",
                    "spent": 2,
                    "episode": 1,
                    "outcome": "needs_human",
                },
            }
        ],
    }

    automatic = report_script.summarize([row], now=NOW)["parking"]["automatic"]

    assert automatic == {"attempted": 1, "recovered": 0, "success_ratio": 0.0}


def test_evicted_attempt_stays_unattributed_across_episodes() -> None:
    # One real error, then a re-park whose bounded history is only shadows.
    # ``spent`` proves the error happened, but not which stay lost the step.
    # Dropping it lets one later recovery report 100%. Naming the oldest
    # empty stay is a guess.
    record = {
        "reason": "review_unparseable",
        "episode": 2,
        "spent": 1,
        "history": [{"episode": 2, "result": "shadow"}] * 64,
        "episodes": [
            {"episode": 1, "reason": "no_new_commit", "steps_applied": 0}
        ],
    }
    failed = {"id": "d", "lines": [{"id": "l", "liveness": record}]}
    parking = report_script.summarize([failed], now=NOW)["parking"]
    assert parking["automatic"] == {
        "attempted": 1,
        "recovered": 0,
        "success_ratio": 0.0,
        "unattributed": 1,
    }
    assert parking["by_reason"]["no_new_commit"]["attempted"] == 0
    assert parking["by_reason"]["review_unparseable"]["attempted"] == 0

    rows = [
        {
            "id": "d",
            "lines": [
                {"id": "l", "liveness": record},
                {
                    "id": "ok",
                    "liveness": {
                        "reason": "no_new_commit",
                        "steps_applied": 1,
                        "outcome": "recovered",
                    },
                },
            ],
        }
    ]
    recovered = report_script.summarize(rows, now=NOW)["parking"]["automatic"]
    assert recovered == {
        "attempted": 2,
        "recovered": 1,
        "success_ratio": 0.5,
        "unattributed": 1,
    }


def test_evicted_attempt_is_not_a_recovered_episodes_success() -> None:
    # The producer can close an earlier stay as recovered with no applied
    # step, then park again. The trimmed error is not that recovery.
    record = {
        "reason": "review_unparseable",
        "episode": 2,
        "spent": 1,
        "history": [{"episode": 2, "result": "shadow"}] * 64,
        "episodes": [
            {
                "episode": 1,
                "reason": "no_new_commit",
                "outcome": "recovered",
                "steps_applied": 0,
            }
        ],
    }
    parking = report_script.summarize(
        [{"id": "d", "lines": [{"id": "l", "liveness": record}]}],
        now=NOW,
    )["parking"]
    assert parking["automatic"]["recovered"] == 0
    assert parking["automatic"]["success_ratio"] != 1
    assert parking["by_reason"]["no_new_commit"]["attempted"] == 0
    assert parking["by_reason"]["no_new_commit"]["recovered"] == 0
    assert parking["by_reason"]["no_new_commit"]["success_ratio"] is None


def test_evicted_step_does_not_add_an_episode_when_every_stay_attempted() -> None:
    # Two recovered stays, each with an applied step, and one evicted error.
    # The error belonged to one of those two stays, so the denominator stays
    # at two episodes: an extra "attempt" would report 66.7% instead of 100%.
    record = {
        "reason": "review_unparseable",
        "episode": 2,
        "spent": 3,
        "steps_applied": 1,
        "outcome": "recovered",
        "history": [{"episode": 2, "result": "shadow"}] * 64,
        "episodes": [
            {
                "episode": 1,
                "reason": "no_new_commit",
                "steps_applied": 1,
                "outcome": "recovered",
            }
        ],
    }
    parking = report_script.summarize(
        [{"id": "d", "lines": [{"id": "l", "liveness": record}]}],
        now=NOW,
    )["parking"]
    assert parking["automatic"] == {
        "attempted": 2,
        "recovered": 2,
        "success_ratio": 1.0,
    }


def test_evicted_step_cannot_belong_to_a_stay_newer_than_the_kept_trail() -> None:
    # Episode 1: error, 63 shadows, error; the first error fell out of the
    # 64-entry tail. Episode 2 took no real step. History drops its oldest
    # entries first, so the lost step is from episode 1, not the empty
    # episode 2: one attempted stay, not two.
    history = (
        [{"episode": 1, "result": "shadow"}] * 63
        + [{"episode": 1, "result": "error"}]
    )
    record = {
        "reason": "review_unparseable",
        "episode": 2,
        "spent": 2,
        "outcome": "needs_human",
        "history": history,
        "episodes": [{"episode": 1, "reason": "no_new_commit", "outcome": "recovered"}],
    }
    parking = report_script.summarize(
        [{"id": "d", "lines": [{"id": "l", "liveness": record}]}],
        now=NOW,
    )["parking"]
    assert parking["automatic"] == {
        "attempted": 1,
        "recovered": 1,
        "success_ratio": 1.0,
    }


def test_shadow_observations_are_not_attempts() -> None:
    row = {
        "id": "d",
        "lines": [
            {
                "id": "line-0",
                "liveness": {
                    "reason": "no_new_commit",
                    "spent": 0,
                    "shadowed": 1,
                    "episode": 1,
                    "history": [{"episode": 1, "result": "shadow"}],
                    "outcome": "pending",
                },
            }
        ],
    }

    automatic = report_script.summarize([row], now=NOW)["parking"]["automatic"]

    assert automatic == {"attempted": 0, "recovered": 0, "success_ratio": None}


def test_a_failed_step_in_an_earlier_episode_keeps_its_own_reason() -> None:
    first = {
        "episode": 1,
        "reason": "no_new_commit",
        "parked_at": "2026-11-12T10:00:00+00:00",
        "resolved_at": "2026-11-12T11:00:00+00:00",
        "outcome": "needs_human",
        "steps_applied": 0,
    }
    current = {
        "reason": "review_unparseable",
        "parked_at": "2026-11-12T11:30:00+00:00",
        "episode": 2,
        "episodes": [first],
        "outcome": "pending",
        "history": [{"episode": 1, "result": "error"}],
    }
    row = {
        "id": "d",
        "lines": [{"id": "line-0", "status": "BLOCKED"}],
        "gates": {"lines": {"line-0": {"liveness": current}}},
    }

    report = report_script.summarize([row], now=NOW)

    assert report["parking"]["by_reason"]["no_new_commit"]["attempted"] == 1
    assert report["parking"]["by_reason"]["review_unparseable"]["attempted"] == 0


def test_re_parking_does_not_count_the_same_handoff_twice() -> None:
    # The record producer copies the same needs_human latch and human_at into
    # the episode list and the current episode when the line re-parks.
    handoff = {
        "reason": "handoff_incomplete",
        "needs_human": True,
        "human_at": "2026-11-12T10:05:00+00:00",
    }
    current = {**handoff, "episode": 2, "episodes": [dict(handoff)]}
    row = {
        "id": "d",
        "lines": [{"id": "line-0", "status": "BLOCKED"}],
        "gates": {"lines": {"line-0": {"liveness": current}}},
    }

    report = report_script.summarize([row], now=NOW)

    # Two parking stays are preserved...
    assert report["parking"]["total"] == 2
    assert report["parking"]["by_reason_counts"] == {"handoff_incomplete": 2}
    # ...but they are one request to one person, open once.
    assert report["needs_human"] == {
        "total": 1,
        "open": 1,
        "average_seconds_to_handle": None,
    }


def test_distinct_handoffs_on_one_line_stay_separate() -> None:
    handled = {
        "reason": "handoff_incomplete",
        "needs_human": True,
        "human_at": "2026-11-12T09:00:00+00:00",
        "human_handled_at": "2026-11-12T09:10:00+00:00",
    }
    open_handoff = {
        "reason": "review_unparseable",
        "needs_human": True,
        "human_at": "2026-11-12T10:00:00+00:00",
    }
    row = {
        "id": "d",
        "lines": [{"id": "line-0", "status": "BLOCKED"}],
        "gates": {
            "lines": {
                "line-0": {
                    "liveness": {
                        **open_handoff,
                        "episode": 2,
                        "episodes": [handled],
                    }
                }
            }
        },
    }

    report = report_script.summarize([row], now=NOW)

    assert report["parking"]["total"] == 2
    assert report["needs_human"] == {
        "total": 2,
        "open": 1,
        "average_seconds_to_handle": 600.0,
    }


def test_handled_handoff_stops_the_archived_episode_clock() -> None:
    # The archived copy keeps the latch but not ``human_handled_at``. Once the
    # current copy records handling, that stay must stop too, or the average
    # keeps growing after the person is done.
    def at(hour: str) -> str:
        return f"2026-11-12T{hour}Z"

    handoff = {"needs_human": True, "human_at": at("10:05")}
    archived = dict(
        handoff,
        episode=1,
        reason="handoff_incomplete",
        parked_at=at("10:00"),
        resolved_at=at("10:05"),
    )
    current = dict(
        handoff,
        episode=2,
        reason="handoff_incomplete",
        parked_at=at("11:00"),
        human_handled_at=at("12:00"),
        episodes=[archived],
    )
    row = {"id": "d", "lines": [{"id": "l", "liveness": current}]}
    averages = [
        report_script.summarize([row], now=report_script.parse_time(at(hour)))[
            "parking"
        ]["average_seconds"]
        for hour in ("13:00", "14:00")
    ]
    # 10:00→12:00 and 11:00→12:00. Handling, not the re-park stamp, ends both.
    assert averages == [5400.0, 5400.0]


def test_retry_wait_is_the_time_already_elapsed() -> None:
    # delay_seconds is the whole scheduled retry. One minute into a 15 minute
    # delay, the observed wait is 60 seconds, and it stops growing at the deadline.
    raw = {
        "at": "2026-11-12T10:00:00Z",
        "retry_at": "2026-11-12T10:15:00Z",
        "delay_seconds": 900,
    }
    started = report_script.parse_time("2026-11-12T10:01:00Z")
    assert report_script._review_wait_seconds(raw, now=started) == 60
    finished = report_script.parse_time("2026-11-12T10:20:00Z")
    assert report_script._review_wait_seconds(raw, now=finished) == 900


def test_a_retry_inside_a_stage_gate_visit_counts_once() -> None:
    # The retry is the tail of the same visit. Averaging it with the visit
    # (300s and 2100s) would report 1200s for a review that has waited 35 minutes.
    snap = {
        "machine": {"stage": "primary_review"},
        "events": [
            {
                "from_stage": "focused_tests",
                "to_stage": "primary_review",
                "timestamp": "2026-11-12T10:00Z",
            }
        ],
        "review_retry": {
            "at": "2026-11-12T10:30Z",
            "retry_at": "2026-11-12T10:45Z",
            "delay_seconds": 900,
        },
    }
    review = report_script.summarize(
        [{"id": "d", "lines": [{"id": "l"}], "gates": {"lines": {"l": snap}}}],
        now=report_script.parse_time("2026-11-12T10:35Z"),
    )["review"]
    assert review["average_wait_seconds"] == 2100
    assert review["scheduled_retries"] == 1


def test_a_line_waiting_in_primary_review_counts_the_stage_gate_clock() -> None:
    snap = {
        "machine": {"stage": "primary_review"},
        "events": [
            {
                "from_stage": "focused_tests",
                "to_stage": "primary_review",
                "timestamp": "2026-11-12T10:00:00Z",
            }
        ],
    }
    review = report_script.summarize(
        [
            {
                "id": "d",
                "lines": [{"id": "l", "status": "RUNNING"}],
                "gates": {"lines": {"l": snap}},
            }
        ],
        now=report_script.parse_time("2026-11-12T11:00:00Z"),
    )["review"]
    assert review == {
        "unparseable": 0,
        "scheduled_retries": 0,
        "average_wait_seconds": 3600.0,
    }


def test_review_cell_closes_the_wait_when_the_leave_event_is_gone() -> None:
    # The bounded event log can drop the transition out of review. The cell's
    # ``at`` is the finish, so the wait is not "still open until now".
    snap = {
        "machine": {"stage": "fixing"},
        "events": [
            {
                "from_stage": "focused_tests",
                "to_stage": "primary_review",
                "timestamp": "2026-11-12T10:00:00Z",
            }
        ],
        "gates": {
            "primary_review": {
                "result": "fail",
                "reason": "review_blocked",
                "reviewer": "codex",
                "at": "2026-11-12T10:20:00Z",
            }
        },
    }
    review = report_script.summarize(
        [
            {
                "id": "d",
                "lines": [{"id": "l", "status": "RUNNING"}],
                "gates": {"lines": {"l": snap}},
            }
        ],
        now=report_script.parse_time("2026-11-12T11:00:00Z"),
    )["review"]
    assert review["average_wait_seconds"] == 1200.0
    assert review["scheduled_retries"] == 0


def test_empty_report_has_null_ratios_not_a_division_error() -> None:
    report = report_script.summarize([], now=NOW)

    assert report["dispatches"]["approval_ratio"] is None
    assert report["parking"]["by_reason"] == {}
    assert report["parking"]["average_seconds"] is None
    assert report["parking"]["automatic"]["success_ratio"] is None
    assert report["needs_human"]["average_seconds_to_handle"] is None
    assert report["review"]["average_wait_seconds"] is None


def test_follow_up_files_are_checked_when_the_directory_is_given(
    tmp_path: Path,
) -> None:
    (tmp_path / "d2-line-2.md").write_text("# follow up\n", encoding="utf-8")

    report = report_script.summarize(_dispatches(), now=NOW, followups_dir=tmp_path)

    followups = report["split_off"]["followups"]
    assert followups["expected"] == 1
    assert followups["present"] == 1
    assert followups["missing"] == []


def test_split_completion_is_read_from_outcomes_without_a_line_row() -> None:
    dispatch = {
        "id": "d3",
        "state": "ABORT",
        "lines": [],
        "gates": {},
        "outcomes": {
            "split_off": [
                {"line_id": "line-8", "completed": True},
                {"line_id": "line-9", "status": "pending"},
            ]
        },
    }

    report = report_script.summarize([dispatch], now=NOW)

    assert report["split_off"]["lines"] == 2
    followups = report["split_off"]["followups"]
    assert followups["expected"] == 2
    assert followups["completed"] == 1
    assert followups["pending"] == 1
    assert followups["unknown"] == 0


def test_a_split_line_is_not_counted_twice() -> None:
    dispatch = {
        "id": "d4",
        "state": "ABORT",
        "lines": [
            {"id": "line-1", "status": "FAILED", "failure_reason": "split_off"}
        ],
        "gates": {},
        "outcomes": {"split_off": ["line-1"]},
    }

    report = report_script.summarize([dispatch], now=NOW)

    assert report["split_off"]["lines"] == 1


def test_render_text_mentions_the_five_questions() -> None:
    text = report_script.render_text(report_script.summarize(_dispatches(), now=NOW))

    assert "Dispatches: 1/2 reached APPROVAL (50%)" in text
    assert "Parking: 3 across 2 reason(s)" in text
    assert "Needs human: 1 total, 0 open" in text
    assert "Split off: 1 line(s)" in text
    assert "review_unparseable: 1" in text


def test_load_json_accepts_an_array_wrapped_export_or_jsonl(
    tmp_path: Path,
) -> None:
    array = tmp_path / "array.json"
    array.write_text(json.dumps([{"id": "d1"}]), encoding="utf-8")
    assert report_script.load_json(array) == [{"id": "d1"}]

    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(
        json.dumps({"dispatches": [{"dispatch_id": "d2"}]}), encoding="utf-8"
    )
    assert report_script.load_json(wrapped) == [{"dispatch_id": "d2"}]

    lines = tmp_path / "rows.jsonl"
    lines.write_text('{"id": "d3"}\n{"id": "d4"}\n', encoding="utf-8")
    assert report_script.load_json(lines) == [{"id": "d3"}, {"id": "d4"}]


def test_load_json_rejects_an_object_with_no_dispatch_list(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"nope": 1}), encoding="utf-8")

    with pytest.raises(ValueError):
        report_script.load_json(path)


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.closed = False

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        self.closed = True


def test_api_loader_uses_one_plain_get(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[urllib.request.Request] = []
    response = _FakeResponse(_dispatches())

    def fake_urlopen(request: urllib.request.Request, timeout: float):
        calls.append(request)
        assert timeout == 3.0
        return response

    monkeypatch.setattr(report_script.urllib.request, "urlopen", fake_urlopen)

    rows = report_script.load_api(
        "http://127.0.0.1:8000/api/v1/dispatch/status/active",
        token="test-token",
        timeout=3.0,
    )

    assert len(calls) == 1
    request = calls[0]
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.get_header("Authorization") == "Bearer test-token"
    assert request.get_header("Accept") == "application/json"
    assert rows == _dispatches()
    assert response.closed is True


def test_the_cli_prints_json_from_a_mocked_get(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[str] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float):
        seen.append(request.get_method())
        return _FakeResponse(_dispatches())

    monkeypatch.setattr(report_script.urllib.request, "urlopen", fake_urlopen)

    code = report_script.main(
        ["--api", "http://example.test/dispatches", "--json", "--until", "2100-01-01"]
    )

    assert code == 0
    assert seen == ["GET"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["dispatches"]["total"] == 2
    assert payload["parking"]["by_reason_counts"]["review_unparseable"] == 1
    assert payload["window"]["since"] is None


def test_the_cli_prints_json_from_a_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "dispatches.json"
    path.write_text(json.dumps(_dispatches()), encoding="utf-8")

    code = report_script.main(["--from-json", str(path), "--json", "--until", "2100-01-01"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dispatches"]["total"] == 2
    assert payload["window"]["since"] is None


def test_report_never_writes_a_file_or_a_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both sources are reads: no open-for-write, no POST, no DB.

    ``--from-json`` must only read the export, and ``--api`` must only GET it.
    Patching the write entry points makes any accidental write loud; the fake
    urlopen makes any accidental POST a test failure, not a network call.
    """
    path = tmp_path / "dispatches.json"
    path.write_text(json.dumps(_dispatches()), encoding="utf-8")

    def forbidden_write(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"the report tried to write: {args!r} {kwargs!r}")

    real_open = builtins.open
    real_path_open = Path.open

    def guarded_open(file: object, mode: str = "r", *args: object, **kwargs: object):
        if any(flag in mode for flag in "wax+"):
            raise AssertionError(f"the report opened {file!r} for writing ({mode!r})")
        return real_open(file, mode, *args, **kwargs)

    def guarded_path_open(self: Path, mode: str = "r", *args: object, **kwargs: object):
        if any(flag in mode for flag in "wax+"):
            raise AssertionError(f"the report opened {self} for writing ({mode!r})")
        return real_path_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(Path, "write_text", forbidden_write)
    monkeypatch.setattr(Path, "write_bytes", forbidden_write)
    monkeypatch.setattr(Path, "mkdir", forbidden_write)
    monkeypatch.setattr(Path, "unlink", forbidden_write)

    methods: list[str] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float):
        methods.append(request.get_method())
        return _FakeResponse(_dispatches())

    monkeypatch.setattr(report_script.urllib.request, "urlopen", fake_urlopen)

    assert report_script.main(["--from-json", str(path), "--json"]) == 0
    from_file = capsys.readouterr()
    assert from_file.err == ""
    assert json.loads(from_file.out)["dispatches"]["total"] == 2

    assert report_script.main(["--api", "http://example.test/dispatches", "--json"]) == 0
    from_api = capsys.readouterr()
    assert from_api.err == ""
    assert json.loads(from_api.out)["dispatches"]["total"] == 2
    assert methods == ["GET"]


def test_days_window_filters_old_dispatches() -> None:
    rows = _dispatches()
    recent = report_script._filter_window(
        rows,
        since=NOW - timedelta(days=3),
        until=NOW + timedelta(days=1),
    )
    assert len(recent) == 2

    old = report_script._filter_window(
        rows,
        since=NOW,
        until=NOW + timedelta(days=1),
    )
    assert old == []


def test_bare_api_is_marked_active_only_and_window_reports_unplaced_rows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The active list's summaries carry no created_at/updated_at, and finished
    # dispatches never appear there. Neither may pass as a windowed baseline.
    seen: list[str] = []
    rows = [
        {"id": "d", "state": "RUNNING", "gates": {}, "lines": []},
        {"id": "e", "state": "APPROVAL", "gates": {}, "lines": []},
    ]

    def fake_urlopen(request: urllib.request.Request, timeout: float):
        seen.append(request.full_url)
        return _FakeResponse({"dispatches": rows})

    monkeypatch.setattr(report_script.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.delenv("CONDUCTOR_API_URL", raising=False)

    assert report_script.main(["--api", "--days", "1", "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert seen == ["http://127.0.0.1:8000/api/v1/dispatch/status/active"]
    assert payload["scope"] == report_script.ACTIVE_ONLY
    assert payload["window"]["unplaced"] == 2
    assert "no created_at/updated_at" in captured.err

    assert report_script.main(["--api", "--days", "1"]) == 0
    text = capsys.readouterr().out
    assert "active dispatches only" in text
    assert "2 row(s) without a timestamp" in text


def test_an_explicit_api_url_without_a_window_has_no_scope_note(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        report_script.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse(_dispatches()),
    )

    assert report_script.main(["--api", "http://example.test/dispatches", "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert "scope" not in payload
    assert "unplaced" not in payload["window"]
    assert captured.err == ""

