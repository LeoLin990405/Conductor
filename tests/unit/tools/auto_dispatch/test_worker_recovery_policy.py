from datetime import UTC, datetime
import pytest

from src.llms.native_cli.failure import (
    CONFIRMATION_REQUIRED_PROMPT_NOTE,
    TURN_BUDGET_EXHAUSTED_PROMPT_NOTE,
    FailureClass,
)
from src.tools.auto_dispatch.worker_recovery import Action, _retry_delay, decide


NOW = datetime(2026, 11, 8, tzinfo=UTC)


@pytest.mark.parametrize(
    ("failure_class", "expected"),
    [
        (FailureClass.RATE_LIMITED, "rate_limited"),
        ("overloaded", "overloaded"),
        ("network", "network"),
        ("timeout", "timeout"),
        ("quota_exhausted", Action("reassign_fallback", reason="worker_quota_exhausted")),
        ("auth_failed", Action("park", reason="worker_auth_failed")),
        ("context_exhausted", Action("rotate_session")),
        (
            "confirmation_required",
            Action(
                "retry_same",
                reason="worker_confirmation_required",
                prompt_note=CONFIRMATION_REQUIRED_PROMPT_NOTE,
            ),
        ),
        (
            "turn_budget_exhausted",
            Action(
                "retry_same",
                reason="worker_turn_budget_exhausted",
                prompt_note=TURN_BUDGET_EXHAUSTED_PROMPT_NOTE,
            ),
        ),
        ("crash", Action("retry_fresh_once")),
        ("unknown", Action("retry_fresh_once")),
    ],
)
def test_decides_for_every_failure_class(failure_class, expected):
    action = decide(failure_class, None, {}, False, NOW)
    if expected in {"rate_limited", "overloaded", "network", "timeout"}:
        assert action.kind == "retry_same"
        assert 10 <= action.delay_seconds < 11
    else:
        assert action == expected


def test_retry_backoff_uses_number_of_retries_already_used():
    one_used = decide("overloaded", None, {"overloaded": 1}, False, NOW)
    two_used = decide("network", None, {"network": 2}, False, NOW)
    assert one_used.kind == "retry_same"
    assert 20 <= one_used.delay_seconds < 21
    assert two_used.kind == "retry_same"
    assert 40 <= two_used.delay_seconds < 41
    assert decide("rate_limited", None, {"rate_limited": 3}, False, NOW) == Action(
        "park", reason="rate_limited_retries_exhausted"
    )


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [(30, 30), (0, 0), (-5, 0), (900, 600), (12.5, 12.5)],
)
def test_retry_after_takes_precedence_and_is_capped(retry_after, expected):
    action = decide("rate_limited", retry_after, {"rate_limited": 2}, False, NOW)
    assert action == Action("retry_same", expected)


def test_retry_jitter_is_stable_for_same_inputs_and_changes_with_time():
    first = _retry_delay("network", 2, NOW)
    assert _retry_delay("network", 2, NOW) == first
    later = _retry_delay("network", 2, NOW.replace(day=9))
    assert first != later
    assert 40 <= first < 41
    assert 40 <= later < 41


def test_retry_delay_can_add_jitter_above_the_600_second_base():
    delay = _retry_delay("network", 6, NOW)
    assert 600 <= delay < 601


def test_retry_after_does_not_bypass_exhausted_retry_limit():
    assert decide("network", 5, {"network": 3}, False, NOW) == Action(
        "park", reason="network_retries_exhausted"
    )


def test_fix_round_quota_parks_without_reassigning_author():
    assert decide("quota_exhausted", None, {}, True, NOW) == Action(
        "park", reason="author_quota_exhausted"
    )


def test_context_rotation_stops_after_three_rotations():
    assert decide("context_exhausted", None, {"context_exhausted": 2}, False, NOW) == Action(
        "rotate_session"
    )
    assert decide("context_exhausted", None, {"context_exhausted": 3}, False, NOW) == Action(
        "park", reason="context_exhausted_retries_exhausted"
    )


def test_fix_round_crash_exhaustion_parks_with_original_author_contract():
    assert decide("crash", None, {"crash": 1}, True, NOW) == Action(
        "park", reason="fix_launch_failed"
    )


def test_crash_and_unknown_share_single_fresh_retry_budget():
    assert decide("unknown", None, {"crash": 1}, False, NOW) == Action(
        "reassign_fallback", reason="fresh_retry_exhausted"
    )
    assert decide("crash", None, {"unknown": 1}, False, NOW) == Action(
        "reassign_fallback", reason="fresh_retry_exhausted"
    )


def test_accepts_enum_keys_for_attempt_counts():
    action = decide(
        FailureClass.OVERLOADED,
        None,
        {FailureClass.OVERLOADED: 2},
        False,
        NOW,
    )
    assert action.kind == "retry_same"
    assert 40 <= action.delay_seconds < 41


@pytest.mark.parametrize(
    ("failure_class", "attempts_by_class", "retry_after"),
    [
        ("diagnostic text with token=secret", {}, None),
        ("overloaded", {"overloaded": -1}, None),
        ("overloaded", {"overloaded": 1.5}, None),
        ("overloaded", {}, float("nan")),
        ("overloaded", {}, "30"),
    ],
)
def test_rejects_untrusted_labels_and_invalid_policy_inputs(
    failure_class, attempts_by_class, retry_after
):
    with pytest.raises(ValueError):
        decide(failure_class, retry_after, attempts_by_class, False, NOW)


def test_action_is_immutable():
    action = Action("retry_same")
    with pytest.raises(AttributeError):
        action.kind = "park"


def test_confirmation_refusal_retries_once_with_a_note_then_parks():
    """N39: one retry told to avoid confirmation, on its own budget, then park."""
    first = decide("confirmation_required", None, {"crash": 1}, False, NOW)
    assert first.kind == "retry_same"
    assert first.delay_seconds == 0
    assert first.prompt_note == CONFIRMATION_REQUIRED_PROMPT_NOTE
    second = decide("confirmation_required", None, {"confirmation_required": 1}, True, NOW)
    assert second == Action("park", reason="worker_confirmation_required")
    assert second.prompt_note is None
    # It does not spend the crash budget a later crash still has.
    assert decide("crash", None, {"confirmation_required": 1}, False, NOW) == Action(
        "retry_fresh_once"
    )


def test_a_spent_turn_budget_continues_the_session_then_parks_for_a_split():
    """Grok's ``max turns reached`` (2026-11-13) is no login failure: the same
    Worker continues in its session twice, with no backoff, and only then
    parks with a reason the liveness table splits."""
    for used in (0, 1):
        action = decide(
            FailureClass.TURN_BUDGET_EXHAUSTED,
            None,
            {"turn_budget_exhausted": used},
            False,
            NOW,
        )
        assert action.kind == "retry_same"
        assert action.delay_seconds == 0
        assert action.reason != "worker_auth_failed"
        assert action.prompt_note == TURN_BUDGET_EXHAUSTED_PROMPT_NOTE
    # Other classes' counts do not spend the turn budget's own allowance.
    assert decide(
        "turn_budget_exhausted", None, {"crash": 1, "context_exhausted": 3}, True, NOW
    ).kind == "retry_same"
    assert decide(
        "turn_budget_exhausted", None, {"turn_budget_exhausted": 2}, False, NOW
    ) == Action("park", reason="turn_budget_exhausted_retries_exhausted")
