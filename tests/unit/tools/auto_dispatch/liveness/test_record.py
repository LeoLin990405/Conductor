"""The liveness record lives in the existing gates JSON and resumes a restart."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.tools.auto_dispatch.liveness import record as records


NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def test_a_new_record_starts_pending_with_the_whole_budget() -> None:
    item = records.new_record("no_new_commit", "R-EXIT", budget=4, at=NOW)
    assert item.reason == "no_new_commit"
    assert item.group == "R-EXIT"
    assert item.budget_left == 4
    assert item.active
    assert item.outcome == records.PENDING
    assert item.next_at is None
    assert item.history == ()


def test_each_step_is_numbered_and_moves_the_schedule() -> None:
    item = records.new_record("no_new_commit", "R-EXIT", budget=4, at=NOW)
    later = NOW + timedelta(minutes=5)
    item = item.with_step(
        "reassign",
        at=later,
        result=records.APPLIED,
        detail="new author picked",
        next_at=later + timedelta(minutes=10),
        next_action="reassign",
    )

    assert len(item.history) == 1
    assert item.history[0].number == 1
    assert item.history[0].step == 1
    assert item.history[0].action == "reassign"
    assert item.history[0].reason == "no_new_commit"
    assert item.history[0].result == records.APPLIED
    assert item.history[0].detail == "new author picked"
    assert item.budget_left == 3
    assert item.next_action == "reassign"
    assert item.next_at == records.format_time(later + timedelta(minutes=10))


def test_step_history_is_bounded_but_the_total_spend_is_not() -> None:
    item = records.new_record("review_unavailable", "R-REVIEW", budget=4, at=NOW)
    for _ in range(1000):
        item = item.with_step("review", at=NOW, result=records.APPLIED)

    assert len(item.history) == records.HISTORY_MAX
    assert item.spent == 1000
    assert item.shadowed == 0
    assert item.budget_left == 0
    # The newest attempts are the ones kept.
    assert item.history[-1].number == 1000
    assert item.history[0].number == 1000 - records.HISTORY_MAX + 1


def test_shadow_history_is_bounded_but_never_spends_the_budget() -> None:
    item = records.new_record("review_unavailable", "R-REVIEW", budget=4, at=NOW)
    for _ in range(1000):
        item = item.with_step("review", at=NOW, result=records.SHADOW)

    assert len(item.history) == records.HISTORY_MAX
    assert item.spent == 0
    assert item.shadowed == 1000
    assert item.recorded == 1000
    assert item.budget_left == 4
    assert item.history[-1].number == 1000
    assert item.history[0].number == 1000 - records.HISTORY_MAX + 1


def test_loading_a_huge_legacy_shadow_history_does_not_spend_the_budget() -> None:
    raw = {
        "reason": "review_unavailable",
        "group": "R-REVIEW",
        "parked_at": "2026-11-12T12:00:00+00:00",
        "updated_at": "2026-11-12T12:00:00+00:00",
        "budget": 4,
        "steps": [
            {
                "step": number,
                "action": "review",
                "reason": "review_unavailable",
                "at": "2026-11-12T12:00:00+00:00",
                "result": "shadow",
            }
            for number in range(1, 1001)
        ],
    }

    item = records.LivenessRecord.from_dict(raw)

    assert len(item.history) == records.HISTORY_MAX
    # The old ``spent`` counted shadow entries too. On reload those become
    # observations, so switching to enforce keeps the whole budget.
    assert item.spent == 0
    assert item.shadowed == 1000
    assert item.budget_left == 4
    # Older records used ``steps``; ``history`` is the new key and both are
    # readable by the record itself.
    assert item.steps is item.history


def test_shadow_observations_do_not_consume_the_enforcement_budget() -> None:
    item = records.new_record("no_new_commit", "R-EXIT", budget=4, at=NOW)
    for _ in range(4):
        item = item.with_step("reassign", at=item.updated_at, result=records.SHADOW)

    assert item.spent == 0
    assert item.shadowed == 4
    assert item.recorded == 4
    assert item.budget_left == 4
    assert item.history[-1].number == 4

    # The split survives a round trip through the gates snapshot, so the
    # process that switches the mode starts from the same budget.
    reloaded = records.LivenessRecord.from_dict(item.to_dict())
    assert reloaded == item
    assert reloaded.shadowed == 4
    assert reloaded.budget_left == 4

    transitioned = reloaded.with_step(
        "reassign", at=NOW, result=records.APPLIED
    )
    assert transitioned.spent == 1
    assert transitioned.shadowed == 4
    assert transitioned.budget_left == 3
    assert transitioned.history[-1].number == 5

    persisted = records.LivenessRecord.from_dict(transitioned.to_dict())
    assert persisted == transitioned
    assert persisted.spent == 1
    assert persisted.shadowed == 4
    assert persisted.budget_left == 3


def test_a_repark_keeps_shadow_history_out_of_the_enforcement_budget() -> None:
    first = records.new_record("no_new_commit", "R-EXIT", budget=4, at=NOW)
    for _ in range(3):
        first = first.with_step("reassign", at=NOW, result=records.SHADOW)
    first = first.with_step("reassign", at=NOW, result=records.APPLIED)

    second = records.new_record(
        "review_unavailable", "R-REVIEW", budget=4, at=NOW, previous=first
    )

    assert second.spent == 1
    assert second.shadowed == 3
    assert second.budget_left == 3
    # The new episode continues the recorded numbering rather than reusing a
    # number already spent on observations.
    third = second.with_step("review", at=NOW, result=records.SHADOW)
    assert third.spent == 1
    assert third.shadowed == 4
    assert third.budget_left == 3
    assert third.history[-1].number == 5


def test_a_legacy_mixed_history_charges_only_visible_enforcement_steps() -> None:
    raw = {
        "reason": "review_unavailable",
        "group": "R-REVIEW",
        "parked_at": "2026-11-12T12:00:00+00:00",
        "updated_at": "2026-11-12T12:00:00+00:00",
        "budget": 4,
        "spent": 3,
        "episode": 1,
        "history": [
            {
                "step": 1,
                "action": "review",
                "reason": "review_unavailable",
                "at": "2026-11-12T12:00:00+00:00",
                "result": "shadow",
            },
            {
                "step": 2,
                "action": "review",
                "reason": "review_unavailable",
                "at": "2026-11-12T12:00:00+00:00",
                "result": "shadow",
            },
            {
                "step": 3,
                "action": "review",
                "reason": "review_unavailable",
                "at": "2026-11-12T12:00:00+00:00",
                "result": "applied",
            },
        ],
    }

    item = records.LivenessRecord.from_dict(raw)

    assert item.spent == 1
    assert item.shadowed == 2
    assert item.budget_left == 3


def test_a_new_parking_episode_keeps_the_history_and_the_spend() -> None:
    first = records.new_record("no_new_commit", "R-EXIT", budget=4, at=NOW)
    first = first.with_step(
        "reassign", at=NOW, result=records.APPLIED, detail="first author"
    )
    first = first.with_step(
        "reassign", at=NOW, result=records.REFUSED, detail="no second author"
    )
    before = first.to_dict()

    second = records.new_record(
        "review_unavailable", "R-REVIEW", budget=4, at=NOW, previous=first
    )

    # The budget is per line, and the audit trail survives the re-park.
    assert second.spent == 2
    assert second.budget_left == 2
    assert second.episode == 2
    assert [step.action for step in second.history] == ["reassign", "reassign"]
    assert [step.detail for step in second.history] == [
        "first author",
        "no second author",
    ]
    assert {step.episode for step in second.history} == {1}
    assert first.to_dict() == before
    # The old episode survives as a summary even though the record now points
    # at the new reason, so the report can count both parks.
    assert len(second.episodes) == 1
    assert second.episodes[0].reason == "no_new_commit"
    assert second.episodes[0].group == "R-EXIT"
    assert second.episodes[0].steps_applied == 1
    assert second.episodes[0].outcome == records.PENDING
    # Numbering continues across episodes instead of restarting at 1.
    third = second.with_step("review", at=NOW, result=records.APPLIED)
    assert third.history[-1].number == 3
    assert third.history[-1].episode == 2
    # Only the current episode's applied steps belong to the current episode.
    assert third.steps_applied == 1
    assert third.episodes[-1].steps_applied == 1


def test_applied_steps_survive_history_eviction_serialization_and_repark() -> None:
    item = records.new_record("no_new_commit", "R-EXIT", budget=4, at=NOW)
    item = item.with_step("reassign", at=NOW, result=records.APPLIED)
    for _ in range(records.HISTORY_MAX):
        item = item.with_step("reassign", at=NOW, result=records.SHADOW)

    # The applied step is the oldest one, so the bounded trail no longer
    # shows it. The episode total must still be 1.
    assert item.spent == 1
    assert item.steps_applied == 1
    assert all(step.result != records.APPLIED for step in item.history)
    assert item.to_dict()["steps_applied"] == 1

    reloaded = records.LivenessRecord.from_dict(item.to_dict())
    assert reloaded == item
    assert reloaded.steps_applied == 1

    parked = records.new_record(
        "review_unavailable", "R-REVIEW", budget=4, at=NOW, previous=reloaded
    )
    assert parked.episodes[-1].steps_applied == 1
    # The new episode starts its own count; the archived total stays put.
    assert parked.steps_applied == 0

    # A payload written before the counter existed can still carry the applied
    # step past the trim window. Count it before dropping the oldest steps.
    legacy = {
        "reason": "no_new_commit",
        "group": "R-EXIT",
        "parked_at": "2026-11-12T12:00:00+00:00",
        "updated_at": "2026-11-12T12:00:00+00:00",
        "budget": 4,
        "spent": 1,
        "shadowed": records.HISTORY_MAX,
        "episode": 1,
        "history": [
            {
                "step": 1,
                "action": "reassign",
                "reason": "no_new_commit",
                "at": "2026-11-12T12:00:00+00:00",
                "result": "applied",
                "episode": 1,
            },
            *[
                {
                    "step": number,
                    "action": "reassign",
                    "reason": "no_new_commit",
                    "at": "2026-11-12T12:00:00+00:00",
                    "result": "shadow",
                    "episode": 1,
                }
                for number in range(2, records.HISTORY_MAX + 2)
            ],
        ],
    }
    loaded = records.LivenessRecord.from_dict(legacy)
    assert len(loaded.history) == records.HISTORY_MAX
    assert all(step.result != records.APPLIED for step in loaded.history)
    assert loaded.steps_applied == 1


def test_a_repark_keeps_the_smaller_budget_including_zero() -> None:
    first = records.new_record("no_new_commit", "R-EXIT", budget=4, at=NOW)
    first = first.with_step("reassign", at=NOW, result=records.APPLIED)

    # Cap 4 with one step spent, then a re-park at cap 1, must not keep the
    # old remainder of three.
    tightened = records.new_record(
        "no_new_commit", "R-EXIT", budget=1, at=NOW, previous=first
    )
    assert tightened.budget == 1
    assert tightened.spent == 1
    assert tightened.budget_left == 0

    # A cap that is still above spent shrinks the ceiling and keeps the rest.
    partial = records.new_record(
        "no_new_commit", "R-EXIT", budget=3, at=NOW, previous=first
    )
    assert partial.budget == 3
    assert partial.spent == 1
    assert partial.budget_left == 2

    # A looser request cannot renew the line.
    widened = records.new_record(
        "no_new_commit", "R-EXIT", budget=8, at=NOW, previous=first
    )
    assert widened.budget == 4
    assert widened.spent == 1
    assert widened.budget_left == 3

    # Zero is a real cap, not a missing one, on either side of the re-park.
    zeroed = records.new_record(
        "no_new_commit", "R-EXIT", budget=0, at=NOW, previous=first
    )
    assert zeroed.budget == 0
    assert zeroed.spent == 1
    assert zeroed.budget_left == 0

    parked_at_zero = records.new_record(
        "handoff_incomplete", "R-HUMAN", budget=0, at=NOW
    )
    raised = records.new_record(
        "no_new_commit", "R-EXIT", budget=4, at=NOW, previous=parked_at_zero
    )
    assert raised.budget == 0
    assert raised.spent == 0
    assert raised.budget_left == 0


def test_a_repark_cannot_clear_a_human_latch() -> None:
    first = records.new_record("no_new_commit", "R-EXIT", budget=4, at=NOW)
    first = first.mark_needs_human("exhausted", at=NOW)
    before = first.to_dict()

    second = records.new_record(
        "no_new_commit", "R-EXIT", budget=4, at=NOW, previous=first
    )

    # Re-parking must not make a line that is waiting on a person active again,
    # and it must not stamp a second notification.
    assert second.needs_human
    assert second.notified_at == first.notified_at
    assert second.human_reason == "exhausted"
    assert second.human_at == first.human_at
    assert second.outcome == records.NEEDS_HUMAN
    assert not second.active
    assert not second.notification_due
    assert second.episodes[0].needs_human
    assert first.to_dict() == before


def test_a_repark_keeps_the_handling_stamp_of_a_latched_line() -> None:
    first = records.new_record("handoff_incomplete", "R-HUMAN", budget=0, at=NOW)
    first = first.mark_needs_human("no handoff", at=NOW)
    handled = first.mark_human_handled(at=NOW + timedelta(minutes=20))

    second = records.new_record(
        "handoff_incomplete", "R-HUMAN", budget=0, at=NOW, previous=handled
    )

    assert second.human_handled_at == records.format_time(NOW + timedelta(minutes=20))
    assert second.notified_at == handled.notified_at
    assert not second.notification_due


def test_episode_summaries_are_bounded_newest_last() -> None:
    item = records.new_record("no_new_commit", "R-EXIT", budget=4, at=NOW)
    for number in range(records.MAX_EPISODES + 5):
        item = item.finish(records.RECOVERED, at=NOW)
        item = records.new_record(
            f"reason-{number}", "R-EXIT", budget=4, at=NOW, previous=item
        )

    assert len(item.episodes) == records.MAX_EPISODES
    assert item.episodes[-1].reason == f"reason-{records.MAX_EPISODES + 3}"
    # The newest park is the live record, not an archived summary.
    assert item.reason == f"reason-{records.MAX_EPISODES + 4}"


def test_a_recovered_episode_is_written_to_the_summary_with_its_outcome() -> None:
    first = records.new_record("no_new_commit", "R-EXIT", budget=4, at=NOW)
    first = first.with_step("split", at=NOW, result=records.APPLIED)
    first = first.finish(records.RECOVERED, at=NOW + timedelta(minutes=2))
    second = records.new_record(
        "review_unparseable", "R-REVIEW", budget=4, at=NOW, previous=first
    )

    episode = second.episodes[0]
    assert episode.outcome == records.RECOVERED
    assert episode.steps_applied == 1
    assert episode.resolved_at == records.format_time(NOW + timedelta(minutes=2))


def test_the_budget_cannot_go_negative() -> None:
    item = records.new_record("review_unparseable", "R-REVIEW", budget=1, at=NOW)
    item = item.with_step("review", at=NOW, result=records.APPLIED)
    assert item.budget_left == 0
    assert item.exhausted


def test_human_is_a_one_way_latch_and_notifies_only_once() -> None:
    item = records.new_record("handoff_incomplete", "R-HUMAN", budget=0, at=NOW)
    first = item.mark_needs_human("the handoff has no commit", at=NOW)
    second = first.mark_needs_human("a different reason", at=NOW + timedelta(minutes=1))

    assert second is first
    assert second.needs_human
    assert second.human_reason == "the handoff has no commit"
    # The first call stamped the single notification; the second could not
    # move it, so a repeated sweep cannot page the person again.
    assert first.notified_at == records.format_time(NOW)
    assert second.notified_at == first.notified_at
    assert not second.notification_due
    assert first.notification_due is False


def test_mark_human_alias_keeps_the_same_latch() -> None:
    item = records.new_record("handoff_incomplete", "R-HUMAN", budget=0, at=NOW)
    first = item.mark_human("no handoff", at=NOW)
    assert first == item.mark_needs_human("no handoff", at=NOW)
    assert first.mark_human("again", at=NOW) is first


def test_a_person_can_be_recorded_as_having_handled_the_line_once() -> None:
    item = records.new_record("handoff_incomplete", "R-HUMAN", budget=0, at=NOW)
    item = item.mark_needs_human("no handoff", at=NOW)
    handled = item.mark_human_handled(at=NOW + timedelta(minutes=20))
    assert handled.human_handled_at == records.format_time(NOW + timedelta(minutes=20))
    assert handled.mark_human_handled(at=NOW + timedelta(hours=2)) is handled


def test_mark_notified_is_idempotent_and_notification_due_tracks_it() -> None:
    raw = records.new_record(
        "handoff_incomplete", "R-HUMAN", budget=0, at=NOW
    ).to_dict()
    raw.pop("notified_at", None)
    raw["needs_human"] = True
    raw["human_reason"] = "no handoff"
    item = records.LivenessRecord.from_dict(raw)
    assert item.notification_due

    notified = item.mark_notified(at=NOW)
    assert notified.notified_at == records.format_time(NOW)
    assert not notified.notification_due
    assert notified.mark_notified(at=NOW + timedelta(hours=1)) is notified


def test_finish_clears_the_schedule_and_rejects_an_unknown_outcome() -> None:
    item = records.new_record("no_new_commit", "R-EXIT", budget=4, at=NOW)
    closed = item.finish(records.RECOVERED, at=NOW + timedelta(minutes=3))
    assert closed.outcome == records.RECOVERED
    assert closed.next_at is None
    assert not closed.active
    with pytest.raises(ValueError):
        item.finish("done", at=NOW)


def test_a_human_finish_keeps_the_latch() -> None:
    item = records.new_record("handoff_incomplete", "R-HUMAN", budget=0, at=NOW)
    closed = item.finish(records.NEEDS_HUMAN, at=NOW)
    assert closed.needs_human
    assert closed.human_at == records.format_time(NOW)


def test_the_record_round_trips_through_the_gates_snapshot() -> None:
    item = records.new_record("no_new_commit", "R-EXIT", budget=4, at=NOW)
    item = item.with_step(
        "reassign", at=NOW, result=records.SHADOW, detail="would reassign"
    )
    item = item.finish(records.MANAGED, at=NOW)
    snapshot: dict[str, object] = {"line_id": "line-0"}
    stored = records.store(snapshot, item)

    loaded = records.load(stored)
    assert loaded == item
    assert loaded is not None
    assert loaded.to_dict() == stored[records.LIVENESS_KEY]
    assert "history" in stored[records.LIVENESS_KEY]  # type: ignore[operator]

    cleared = records.clear(stored)
    assert records.load(cleared) is None
    assert cleared == {"line_id": "line-0"}


def test_store_and_clear_never_mutate_the_caller_snapshot() -> None:
    item = records.new_record("no_new_commit", "R-EXIT", budget=4, at=NOW)
    snapshot: dict[str, object] = {"line_id": "line-0"}
    before = snapshot.copy()

    stored = records.store(snapshot, item)

    assert snapshot == before
    assert records.LIVENESS_KEY not in snapshot
    assert stored is not snapshot
    assert records.load(stored) == item

    stored_before = stored.copy()
    cleared = records.clear(stored)

    assert stored == stored_before
    assert records.LIVENESS_KEY in stored
    assert cleared == before
    assert cleared is not stored


def test_a_detached_clear_still_works_when_no_record_exists() -> None:
    snapshot: dict[str, object] = {"line_id": "line-0"}
    cleared = records.clear(snapshot)
    assert cleared == snapshot
    assert cleared is not snapshot


def test_a_missing_or_invalid_record_loads_as_none() -> None:
    assert records.load(None) is None
    assert records.load({}) is None
    assert records.load({"liveness": {"group": "R-EXIT"}}) is None
    assert records.load({"liveness": "not-a-record"}) is None


def test_parse_time_accepts_iso_z_epoch_and_naive_values() -> None:
    assert records.parse_time("2026-11-12T12:00:00Z") == NOW
    assert records.parse_time(NOW.timestamp()) == NOW
    assert records.parse_time(NOW.replace(tzinfo=None)) == NOW
    assert records.parse_time("not-a-time") is None
    assert records.parse_time(None) is None


def test_format_time_rejects_an_unparsable_string_instead_of_storing_it() -> None:
    assert records.format_time("not-a-time") is None
    assert records.format_time("2026-11-12T12:00:00Z") == records.format_time(NOW)


def test_a_record_survives_json_like_data_with_optional_fields_absent() -> None:
    raw = {
        "reason": "review_unparseable",
        "group": "R-REVIEW",
        "parked_at": "2026-11-12T12:00:00+00:00",
        "updated_at": "2026-11-12T12:00:00+00:00",
        "budget": 4,
        "steps": [
            {
                "step": 1,
                "action": "review",
                "reason": "review_unparseable",
                "at": "2026-11-12T12:00:00+00:00",
                "result": "applied",
            }
        ],
        "outcome": "pending",
    }
    item = records.LivenessRecord.from_dict(raw)
    assert item.budget_left == 3
    assert item.history[0].number == 1
    assert item.steps_applied == 1
    assert item.next_at is None


def test_module_level_helpers_do_not_mutate_the_record() -> None:
    item = records.new_record("no_new_commit", "R-EXIT", budget=4, at=NOW)
    before = item.to_dict()
    stepped = records.add_step(item, "reassign", at=NOW, result=records.APPLIED)
    human = records.mark_needs_human(stepped, "no author", at=NOW)
    assert item.to_dict() == before
    assert stepped.history[0].number == 1
    assert human.needs_human
    assert human.notified_at == records.format_time(NOW)
