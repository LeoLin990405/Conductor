"""Policy modes decide which events may enter an Eval."""

from src.server.services.autonomy import policy

CONFIG = {"allowed_logins": ["alice"]}


def _trigger(*, addressed: bool, login: str = "alice", bot: bool = False):
    return {
        "actor_login": login,
        "addressed": addressed,
        "actor_is_bot": bot,
    }


def test_explicit_mode_requires_an_addressed_allowed_login():
    assert policy.is_candidate(_trigger(addressed=True), CONFIG, "explicit")
    assert not policy.is_candidate(_trigger(addressed=False), CONFIG, "explicit")
    assert not policy.is_candidate(_trigger(addressed=True, login="mallory"), CONFIG, "explicit")
    assert not policy.is_candidate(
        _trigger(addressed=True, bot=True), CONFIG, "explicit"
    )


def test_observe_and_auto_admit_non_addressed_allowed_logins():
    for mode in ("observe", "auto"):
        assert policy.is_candidate(_trigger(addressed=False), CONFIG, mode)
        assert not policy.is_candidate(
            _trigger(addressed=False, login="mallory"), CONFIG, mode
        )
        assert not policy.is_candidate(
            _trigger(addressed=False, bot=True), CONFIG, mode
        )


def test_an_empty_allowlist_admits_nothing():
    empty = {"allowed_logins": []}
    for mode in ("explicit", "observe", "auto"):
        assert not policy.is_candidate(_trigger(addressed=True), empty, mode)


def test_admission_table_decides_the_strength():
    assert policy.admit(_trigger(addressed=True), CONFIG) == "explicit"
    assert policy.admit(_trigger(addressed=False), CONFIG) == "inferred"
    assert policy.admit(_trigger(addressed=True, login="mallory"), CONFIG) is None
    assert policy.admit(_trigger(addressed=True, bot=True), CONFIG) is None


def test_an_admission_cell_can_grant_explicit_strength_to_an_unlisted_sender():
    admission = {"unlisted_addressed": "explicit"}
    assert policy.admit(_trigger(addressed=True, login="mallory"), CONFIG, admission) == "explicit"
    assert policy.is_candidate(
        _trigger(addressed=True, login="mallory"), CONFIG, "explicit", admission
    )


def test_an_inferred_admission_needs_observe_or_auto():
    admission = {"listed_unaddressed": "inferred"}
    assert not policy.is_candidate(_trigger(addressed=False), CONFIG, "explicit", admission)
    assert policy.is_candidate(_trigger(addressed=False), CONFIG, "observe", admission)
    assert policy.is_candidate(_trigger(addressed=False), CONFIG, "auto", admission)


def test_admission_table_rejects_unknown_cells_and_defaults_the_rest():
    table = policy.admission_table({"listed_addressed": "bogus", "extra": "explicit"})
    assert table["listed_addressed"] == "explicit"
    assert table["unlisted_addressed"] == "ignore"
    assert "extra" not in table


def test_a_bot_needs_the_source_bot_list_and_the_bot_cell():
    trigger = _trigger(addressed=True, login="dependabot[bot]", bot=True)
    assert policy.admit(trigger, {**CONFIG, "bot_logins": ["dependabot[bot]"]}) is None
    admission = {"bot": "inferred"}
    assert (
        policy.admit(
            trigger,
            {**CONFIG, "bot_logins": ["dependabot[bot]"]},
            admission,
        )
        == "inferred"
    )
    assert policy.admit(trigger, CONFIG, admission) is None


def test_revalidation_keeps_a_still_admitted_explicit_mandate():
    ok, reason = policy.revalidate(
        _trigger(addressed=True), CONFIG, None,
        strength="explicit", accept_mode="explicit",
    )
    assert ok and reason == ""


def test_revalidation_revokes_when_the_sender_leaves_the_list():
    ok, reason = policy.revalidate(
        _trigger(addressed=True, login="mallory"), CONFIG, None,
        strength="explicit", accept_mode="explicit",
    )
    assert not ok
    assert "no longer admitted" in reason


def test_revalidation_revokes_inferred_when_the_mode_is_explicit_only():
    ok, reason = policy.revalidate(
        _trigger(addressed=False), CONFIG, None,
        strength="inferred", accept_mode="explicit",
    )
    assert not ok
    assert "no longer accepted" in reason


def test_a_source_can_override_its_admission_cells():
    base = {"unlisted_addressed": "ignore"}
    config = {"allowed_logins": ["alice"], "admission": {"unlisted_addressed": "explicit"}}
    merged = policy.effective_admission(base, config)
    assert merged["unlisted_addressed"] == "explicit"
    assert merged["listed_addressed"] == "explicit"
    assert policy.admit(
        _trigger(addressed=True, login="mallory"), config, merged
    ) == "explicit"
    # An invalid override cannot weaken or strengthen beyond known cells.
    bad = policy.effective_admission(base, {"admission": {"unlisted_addressed": "bogus"}})
    assert bad["unlisted_addressed"] == "ignore"
