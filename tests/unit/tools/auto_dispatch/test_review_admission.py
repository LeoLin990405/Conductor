"""Review-backlog admission: queue once waiting and in-review lines reach the cap."""

from __future__ import annotations

import pytest

from src.tools.auto_dispatch.review_admission import (
    LAUNCH,
    QUEUE,
    REVIEW_BACKLOG_MAX_ENV,
    REVIEW_BACKLOG_REASON,
    ReviewAdmission,
    decide_review_admission,
)

_ENV = "CONDUCTOR_DISPATCH_REVIEW_BACKLOG_MAX"


def test_contract_names() -> None:
    assert REVIEW_BACKLOG_MAX_ENV == _ENV
    assert REVIEW_BACKLOG_REASON == "review_backlog"
    assert LAUNCH == "launch"
    assert QUEUE == "queue"


def test_launches_while_the_count_is_under_the_default_cap() -> None:
    # Primary concurrency 2 → default cap 4. Three lines still fit.
    decision = decide_review_admission(3, 2, env={})

    assert decision == ReviewAdmission(decision="launch", reason=None, limit=4)


def test_queues_with_review_backlog_once_the_count_reaches_the_cap() -> None:
    decision = decide_review_admission(4, 2, env={})

    assert decision == ReviewAdmission(
        decision="queue",
        reason="review_backlog",
        limit=4,
    )


def test_stays_queued_above_the_cap() -> None:
    decision = decide_review_admission(5, 2, env={})

    assert decision.decision == "queue"
    assert decision.reason == "review_backlog"
    assert decision.limit == 4


def test_default_cap_is_twice_primary_concurrency() -> None:
    assert decide_review_admission(1, 1, env={}) == ReviewAdmission("launch", None, 2)
    assert decide_review_admission(2, 1, env={}) == ReviewAdmission(
        "queue", "review_backlog", 2
    )
    assert decide_review_admission(5, 3, env={}) == ReviewAdmission("launch", None, 6)
    assert decide_review_admission(6, 3, env={}) == ReviewAdmission(
        "queue", "review_backlog", 6
    )


def test_zero_concurrency_reaches_the_cap_at_an_empty_queue() -> None:
    decision = decide_review_admission(0, 0, env={})

    assert decision == ReviewAdmission("queue", "review_backlog", 0)


def test_positive_env_replaces_the_default_cap() -> None:
    tighter = {_ENV: "3"}
    assert decide_review_admission(2, 2, env=tighter) == ReviewAdmission("launch", None, 3)
    assert decide_review_admission(3, 2, env=tighter) == ReviewAdmission(
        "queue", "review_backlog", 3
    )

    looser = {_ENV: "9"}
    assert decide_review_admission(8, 2, env=looser) == ReviewAdmission("launch", None, 9)
    assert decide_review_admission(9, 2, env=looser) == ReviewAdmission(
        "queue", "review_backlog", 9
    )


def test_positive_env_overrides_a_zero_concurrency_default() -> None:
    env = {_ENV: "2"}

    assert decide_review_admission(1, 0, env=env) == ReviewAdmission("launch", None, 2)
    assert decide_review_admission(2, 0, env=env) == ReviewAdmission(
        "queue", "review_backlog", 2
    )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "0",
        "00",
        "+0",
        "-1",
        "-2",
        "nope",
        "2.5",
        "4.0",
        "true",
        "0x4",
        "+",
    ],
)
def test_invalid_or_non_positive_env_falls_back_to_twice_concurrency(raw: str) -> None:
    decision = decide_review_admission(3, 2, env={_ENV: raw})

    assert decision == ReviewAdmission("launch", None, 4)


def test_surrounding_whitespace_and_an_explicit_plus_are_a_positive_int() -> None:
    assert decide_review_admission(4, 2, env={_ENV: "  5 "}) == ReviewAdmission(
        "launch", None, 5
    )
    assert decide_review_admission(5, 2, env={_ENV: "+5"}) == ReviewAdmission(
        "queue", "review_backlog", 5
    )


def test_passed_env_is_not_mutated_and_hides_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_ENV, "1")
    env: dict[str, str] = {}

    decision = decide_review_admission(0, 2, env=env)

    assert env == {}
    assert decision == ReviewAdmission("launch", None, 4)


def test_omitted_env_reads_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "1")

    decision = decide_review_admission(1, 8)

    assert decision == ReviewAdmission("queue", "review_backlog", 1)


def test_same_inputs_return_an_equal_decision() -> None:
    env = {_ENV: "4"}

    assert decide_review_admission(4, 2, env=env) == decide_review_admission(4, 2, env=env)


@pytest.mark.parametrize(
    ("waiting", "concurrency"),
    [(-1, 1), (1, -1), (True, 1), (0, False), (1.5, 1), (1, "2")],
)
def test_rejects_counts_that_are_not_nonnegative_integers(
    waiting: object, concurrency: object
) -> None:
    with pytest.raises(ValueError):
        decide_review_admission(waiting, concurrency, env={})  # type: ignore[arg-type]
