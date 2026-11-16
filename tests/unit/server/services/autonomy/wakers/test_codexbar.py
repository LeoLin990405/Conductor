"""CodexBar wakes: exhaust, low, and reset are different decisions."""

from datetime import datetime, timezone

from src.server.services.autonomy.wakers.quota_history import (
    QUOTA_GATE_KINDS,
    QUOTA_HISTORY_KINDS,
    collapse_provider_kinds,
)
from src.server.services.autonomy.wakers.codexbar import (
    CodexBarWaker,
    quota_transitions,
)

_WHEN = "2026-11-10T00:00:00+00:00"


def _kinds(events):
    return [(event.provider, event.kind) for event in events]


def test_disabled_waker_drops_the_snapshot():
    waker = CodexBarWaker(enabled=False)
    assert waker.collect({"provider": "glm", "kind": "quota_exhausted"}) == []


def test_exhaust_uses_the_worker_provider_id():
    events = quota_transitions(
        {},
        {"zai": "exhausted"},
        exhausted=set(),
        observed_at=_WHEN,
    )
    assert _kinds(events) == [("glm", "quota_exhausted")]
    assert events[0].idempotency_key.startswith("waker:codexbar:quota_exhausted:glm:")


def test_same_state_is_not_emitted_again():
    events = quota_transitions(
        {"glm": "quota_exhausted"},
        {"zcode": "exhausted"},
        exhausted={"glm"},
        observed_at=_WHEN,
    )
    assert events == []


def test_low_does_not_clear_or_reset_exhaustion():
    events = quota_transitions(
        {"glm": "quota_exhausted"},
        {"glm": "low"},
        exhausted={"glm"},
        observed_at=_WHEN,
    )
    assert _kinds(events) == [("glm", "quota_low")]


def test_available_without_a_prior_exhaust_is_not_a_reset():
    events = quota_transitions(
        {"glm": "quota_low"},
        {"glm": "available"},
        exhausted=set(),
        observed_at=_WHEN,
    )
    assert events == []


def test_available_while_the_provider_is_still_exhausted_is_a_reset():
    events = quota_transitions(
        {"glm": "quota_low"},
        {"glm": "available"},
        exhausted={"zai"},
        observed_at=_WHEN,
    )
    assert _kinds(events) == [("glm", "quota_reset")]


def test_leaving_exhausted_is_a_reset_even_before_the_gate_set_updates():
    events = quota_transitions(
        {"deepseek": "quota_exhausted"},
        {"deepseek-dsh": "available"},
        exhausted=set(),
        observed_at=_WHEN,
    )
    assert _kinds(events) == [("deepseek", "quota_reset")]


def test_unknown_and_unchanged_providers_are_skipped():
    events = quota_transitions(
        {"kimi": "available"},
        {"kimi": "available", "glm": "unknown"},
        exhausted=set(),
        observed_at=_WHEN,
    )
    assert events == []


def test_snapshot_collect_canonicalizes_both_sides():
    events = CodexBarWaker(enabled=True).collect(
        {
            "previous": {"zcode": "available"},
            "states": {"zai": "exhausted"},
            "observed_at": _WHEN,
        }
    )
    assert _kinds(events) == [("glm", "quota_exhausted")]


def test_low_is_history_and_not_a_claim_gate_clear():
    assert "quota_low" in QUOTA_HISTORY_KINDS
    assert "quota_low" not in QUOTA_GATE_KINDS


def test_a_later_reset_under_another_spelling_clears_exhaustion():
    created = datetime(2026, 11, 10, tzinfo=timezone.utc)
    kinds = collapse_provider_kinds(
        [
            {
                "provider": "zai",
                "kind": "quota_exhausted",
                "observed_at": "2026-11-10T01:00:00+00:00",
                "created_at": created,
                "effect_id": "a",
            },
            {
                "provider": "glm",
                "kind": "quota_reset",
                "observed_at": "2026-11-10T02:00:00+00:00",
                "created_at": created,
                "effect_id": "b",
            },
        ]
    )
    assert kinds == {"glm": "quota_reset"}
    assert "glm" not in {
        name for name, kind in kinds.items() if kind == "quota_exhausted"
    }
