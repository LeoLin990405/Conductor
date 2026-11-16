"""The N76 policy table is frozen data; the local YAML may only tighten it."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from src.tools.auto_dispatch.liveness.policy import (
    DEFAULT_GROUPS,
    DEFAULT_MAX_STEPS,
    DEFAULT_POLICY,
    DEFAULT_REASON_GROUPS,
    HUMAN,
    MAX_STEPS_ENV,
    PolicyError,
    R_EXIT,
    R_QUOTA,
    R_REPLAN,
    R_REVIEW,
    R_SPLIT,
    Step,
    default_policy,
    load_policy,
    max_steps,
    tighten,
)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "liveness_policy.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _actions(steps: tuple[Step, ...]) -> tuple[str, ...]:
    return tuple(step.action for step in steps)


def test_the_frozen_table_has_the_prd_groups_and_order() -> None:
    assert set(DEFAULT_GROUPS) == {
        R_EXIT,
        R_REVIEW,
        R_QUOTA,
        R_REPLAN,
        R_SPLIT,
        HUMAN,
    }
    assert _actions(DEFAULT_GROUPS[R_EXIT]) == ("reassign", "reassign", "split")
    assert _actions(DEFAULT_GROUPS[R_REVIEW]) == ("review", "review", "review")
    assert _actions(DEFAULT_GROUPS[R_QUOTA]) == ("wait", "review")
    assert _actions(DEFAULT_GROUPS[R_REPLAN]) == ("reassign", "split")
    assert _actions(DEFAULT_GROUPS[R_SPLIT]) == ("split",)
    assert DEFAULT_GROUPS[HUMAN] == ()
    assert [step.budget for step in DEFAULT_GROUPS[R_REVIEW]] == [60, 300, 900]
    assert DEFAULT_GROUPS[R_QUOTA][0].budget == 24 * 60 * 60


def test_a_step_is_exactly_an_action_budget_tuple() -> None:
    step = Step("review", 60)
    assert tuple(step) == ("review", 60)
    assert step._fields == ("action", "budget")
    action, budget = step
    assert action == "review"
    assert budget == 60


@pytest.mark.parametrize(
    ("reason", "group"),
    [
        ("no_new_commit", R_EXIT),
        ("uncommitted_changes", R_EXIT),
        ("watchdog_timeout", R_EXIT),
        ("g2_no_commands_risk_bearing", R_EXIT),
        ("g2_revision_moved", R_EXIT),
        ("focused_tests_exhausted", R_EXIT),
        ("fix_launch_failed", R_EXIT),
        ("review_unavailable", R_REVIEW),
        ("review_unparseable", R_REVIEW),
        ("no_eligible_reviewer", R_REVIEW),
        ("reviewers_quota_exhausted", R_QUOTA),
        ("replan_parked", R_REPLAN),
        ("replan_uncombined", R_REPLAN),
        ("replan_base_unresolved", R_REPLAN),
        ("replan_exhausted", R_REPLAN),
        ("handoff_incomplete", HUMAN),
        ("reviewer_conflict_scope", HUMAN),
        ("reviewer_conflict_security", HUMAN),
        ("liveness_inconsistent", HUMAN),
        ("no_secondary_reviewer", R_QUOTA),
        ("no_eligible_worker", R_QUOTA),
        ("git_evidence_unavailable", R_SPLIT),
        ("author_quota_exhausted", HUMAN),
    ],
)
def test_every_prd_section_3_reason_has_a_group(reason: str, group: str) -> None:
    assert DEFAULT_REASON_GROUPS[reason] == group


def test_the_reason_map_never_names_an_unknown_group() -> None:
    assert set(DEFAULT_REASON_GROUPS.values()) <= set(DEFAULT_GROUPS)


@pytest.mark.parametrize(
    "reason",
    ["no_secondary_reviewer", "no_eligible_worker"],
)
def test_transient_capacity_shortages_wait_before_splitting(reason: str) -> None:
    # PRD §4.4: reviewer / worker unavailability is capacity, not a permanent
    # failure, so it waits on the R-QUOTA timer instead of splitting at once.
    assert DEFAULT_POLICY.plan_for(reason) == DEFAULT_GROUPS[R_QUOTA]
    assert DEFAULT_POLICY.plan_for(reason)[0].action == "wait"


def test_an_unknown_reason_falls_back_to_human_not_an_invented_step() -> None:
    policy = default_policy()
    assert policy.group_for("a_reason_this_table_never_saw") == HUMAN
    assert policy.plan_for("a_reason_this_table_never_saw") == ()


def test_max_steps_defaults_for_absent_invalid_and_nonpositive_values() -> None:
    assert max_steps({}) == DEFAULT_MAX_STEPS == 4
    assert max_steps({MAX_STEPS_ENV: ""}) == 4
    assert max_steps({MAX_STEPS_ENV: "not-a-number"}) == 4
    assert max_steps({MAX_STEPS_ENV: "0"}) == 4
    assert max_steps({MAX_STEPS_ENV: "-1"}) == 4
    assert max_steps({MAX_STEPS_ENV: "2"}) == 2
    assert max_steps({MAX_STEPS_ENV: " 3 "}) == 3


def test_default_policy_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MAX_STEPS_ENV, "2")
    assert default_policy().max_steps == 2
    monkeypatch.setenv(MAX_STEPS_ENV, "nope")
    assert default_policy().max_steps == DEFAULT_MAX_STEPS


def test_default_policy_has_a_plan_capped_by_max_steps() -> None:
    policy = default_policy(max_steps=2)
    assert policy.plan_for("no_new_commit") == DEFAULT_GROUPS[R_EXIT][:2]
    assert len(DEFAULT_GROUPS[R_EXIT]) == 3


def test_a_missing_file_yields_the_defaults(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"
    assert load_policy(missing) == DEFAULT_POLICY
    assert load_policy(home=tmp_path) == DEFAULT_POLICY


def test_the_loader_reads_the_default_home_path(tmp_path: Path) -> None:
    conductor = tmp_path / ".conductor"
    conductor.mkdir()
    (conductor / "liveness_policy.yaml").write_text("max_steps: 2\n", encoding="utf-8")

    policy = load_policy(home=tmp_path)

    assert policy.max_steps == 2


def test_a_root_file_cannot_shadow_the_documented_conductor_path(
    tmp_path: Path,
) -> None:
    conductor = tmp_path / ".conductor"
    conductor.mkdir()
    (conductor / "liveness_policy.yaml").write_text(
        "groups:\n  R-EXIT: HUMAN\n", encoding="utf-8"
    )
    (tmp_path / "liveness_policy.yaml").write_text("max_steps: 4\n", encoding="utf-8")

    policy = load_policy(home=tmp_path, environ={})

    # The ~/.conductor override wins; the stray root file is not read at all.
    assert policy.plan_for("no_new_commit") == ()
    assert policy.group_for("no_new_commit") == R_EXIT


def test_a_root_file_alone_is_not_read_as_the_policy(tmp_path: Path) -> None:
    (tmp_path / "liveness_policy.yaml").write_text(
        "groups:\n  R-EXIT: HUMAN\n", encoding="utf-8"
    )

    assert load_policy(home=tmp_path, environ={}) == DEFAULT_POLICY


def test_a_yaml_lowering_max_steps_is_honoured(tmp_path: Path) -> None:
    path = _write(tmp_path, "max_steps: 2\n")

    policy = load_policy(path)

    assert policy.max_steps == 2
    assert policy.plan_for("no_new_commit") == DEFAULT_GROUPS[R_EXIT][:2]


def test_a_yaml_raising_max_steps_is_rejected_and_defaults_stay(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, "max_steps: 5\n")

    with pytest.raises(PolicyError):
        load_policy(path)

    assert load_policy() == DEFAULT_POLICY
    assert DEFAULT_POLICY.max_steps == 4


@pytest.mark.parametrize("body", ["max_steps: 2.5\n", "max_steps: -1\n"])
def test_a_yaml_with_an_invalid_max_steps_is_rejected(
    tmp_path: Path, body: str
) -> None:
    with pytest.raises(PolicyError):
        load_policy(_write(tmp_path, body))


def test_a_yaml_may_drop_steps(tmp_path: Path) -> None:
    path = _write(tmp_path, "groups:\n  R-EXIT:\n    steps: [reassign]\n")

    policy = load_policy(path)

    assert _actions(policy.groups[R_EXIT]) == ("reassign",)
    assert policy.group_for("no_new_commit") == R_EXIT


def test_a_yaml_may_drop_a_middle_step(tmp_path: Path) -> None:
    # Tighten-only means fewer rows, not only a prefix: the reviewer may keep
    # the first reassign and the split while dropping the second reassign.
    path = _write(
        tmp_path,
        "groups:\n  R-EXIT:\n    steps:\n      - reassign\n      - split\n",
    )

    policy = load_policy(path)

    assert _actions(policy.groups[R_EXIT]) == ("reassign", "split")


def test_a_yaml_may_shrink_a_step_budget(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        (
            "groups:\n"
            "  R-REVIEW:\n"
            "    steps:\n"
            "      - action: review\n"
            "        budget: 30\n"
        ),
    )

    policy = load_policy(path)

    assert policy.groups[R_REVIEW] == (Step("review", 30),)


def test_a_yaml_string_step_keeps_the_default_budget(tmp_path: Path) -> None:
    path = _write(tmp_path, "groups:\n  R-REVIEW:\n    steps: [review]\n")

    assert load_policy(path).groups[R_REVIEW] == (Step("review", 60),)


def test_a_yaml_may_drop_the_leading_repeated_review_step(tmp_path: Path) -> None:
    # The reviewer may drop review@60 and keep the later retries. Matching the
    # suffix must not compare review@300 against the removed review@60 row.
    path = _write(
        tmp_path,
        (
            "groups:\n"
            "  R-REVIEW:\n"
            "    steps:\n"
            "      - action: review\n"
            "        budget: 300\n"
            "      - action: review\n"
            "        budget: 900\n"
        ),
    )

    policy = load_policy(path)

    assert policy.groups[R_REVIEW] == DEFAULT_GROUPS[R_REVIEW][1:]


def test_a_repeated_action_budget_may_not_exceed_the_largest_default(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        (
            "groups:\n"
            "  R-REVIEW:\n"
            "    steps:\n"
            "      - action: review\n"
            "        budget: 1000\n"
        ),
    )

    with pytest.raises(PolicyError):
        load_policy(path)


def test_a_yaml_budgets_alone_tighten_every_default_step(tmp_path: Path) -> None:
    path = _write(tmp_path, "groups:\n  R-REVIEW:\n    budgets: [30, 100, 200]\n")

    policy = load_policy(path)

    assert policy.groups[R_REVIEW] == (
        Step("review", 30),
        Step("review", 100),
        Step("review", 200),
    )


def test_a_yaml_budgets_apply_after_steps_are_dropped(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        (
            "groups:\n"
            "  R-REVIEW:\n"
            "    steps: [review, review]\n"
            "    budgets: [30, 100]\n"
        ),
    )

    policy = load_policy(path)

    assert policy.groups[R_REVIEW] == (
        Step("review", 30),
        Step("review", 100),
    )


@pytest.mark.parametrize(
    "body",
    [
        # More budgets than steps.
        "groups:\n  R-REVIEW:\n    budgets: [30, 100]\n",
        # A budget above the matching default would loosen the table.
        "groups:\n  R-REVIEW:\n    budgets: [30, 100, 1000]\n",
        # A bare number is not a budget list.
        "groups:\n  R-REVIEW:\n    budgets: 30\n",
    ],
)
def test_a_yaml_budgets_alone_are_validated(tmp_path: Path, body: str) -> None:
    with pytest.raises(PolicyError):
        load_policy(_write(tmp_path, body))


def test_a_yaml_raising_a_budget_is_rejected_and_defaults_stay(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        (
            "groups:\n"
            "  R-REVIEW:\n"
            "    steps:\n"
            "      - action: review\n"
            "        budget: 1000\n"
        ),
    )

    with pytest.raises(PolicyError):
        load_policy(path)

    assert DEFAULT_GROUPS[R_REVIEW] == (
        Step("review", 60),
        Step("review", 300),
        Step("review", 900),
    )


def test_a_yaml_adding_a_step_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "groups:\n  R-EXIT:\n    steps: [reassign, reassign, split, split]\n",
    )

    with pytest.raises(PolicyError):
        load_policy(path)


def test_a_yaml_reordering_or_renaming_a_step_is_rejected(tmp_path: Path) -> None:
    reordered = _write(
        tmp_path,
        "groups:\n  R-REPLAN:\n    steps: [split, reassign]\n",
    )
    with pytest.raises(PolicyError):
        load_policy(reordered)

    renamed = _write(
        tmp_path,
        "groups:\n  R-EXIT:\n    steps: [reassign, reassign, review]\n",
    )
    with pytest.raises(PolicyError):
        load_policy(renamed)


def test_a_yaml_cannot_add_an_automatic_step_to_human(tmp_path: Path) -> None:
    path = _write(tmp_path, "groups:\n  HUMAN:\n    steps: [review]\n")

    with pytest.raises(PolicyError):
        load_policy(path)


def test_a_yaml_may_turn_a_group_human(tmp_path: Path) -> None:
    path = _write(tmp_path, "groups:\n  R-EXIT:\n    human: true\n")

    policy = load_policy(path)

    assert policy.groups[R_EXIT] == ()
    assert policy.plan_for("no_new_commit") == ()


def test_a_yaml_may_turn_a_group_human_by_dropping_every_step(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, "groups:\n  R-EXIT:\n    steps: []\n")

    policy = load_policy(path)

    assert policy.groups[R_EXIT] == ()


def test_a_yaml_may_turn_a_reason_human(tmp_path: Path) -> None:
    path = _write(tmp_path, "reasons:\n  no_new_commit: HUMAN\n")

    policy = load_policy(path)

    assert policy.group_for("no_new_commit") == HUMAN
    assert policy.plan_for("no_new_commit") == ()


def test_a_yaml_cannot_move_a_human_reason_to_an_automatic_group(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, "reasons:\n  handoff_incomplete: R-EXIT\n")

    with pytest.raises(PolicyError):
        load_policy(path)

    assert DEFAULT_REASON_GROUPS["handoff_incomplete"] == HUMAN


def test_a_yaml_moving_an_automatic_reason_to_another_automatic_group_is_refused(
    tmp_path: Path,
) -> None:
    # The PRD only approves moving a reason to HUMAN; changing which automatic
    # rows it runs would be a new action and must not be smuggled in.
    path = _write(tmp_path, "reasons:\n  no_new_commit: R-REVIEW\n")

    with pytest.raises(PolicyError):
        load_policy(path)


def test_a_yaml_with_an_unknown_group_or_reason_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        load_policy(_write(tmp_path, "groups:\n  R-NOPE:\n    steps: []\n"))
    with pytest.raises(PolicyError):
        load_policy(_write(tmp_path, "reasons:\n  no_such_reason: HUMAN\n"))


def test_a_yaml_with_an_unknown_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        load_policy(_write(tmp_path, "unknown_key: 1\n"))
    with pytest.raises(PolicyError):
        load_policy(_write(tmp_path, "groups:\n  R-EXIT:\n    invented: true\n"))


def test_a_non_mapping_yaml_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        load_policy(_write(tmp_path, "- not\n- a map\n"))


def test_an_unparsable_yaml_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        load_policy(_write(tmp_path, "groups: [unclosed\n"))


def test_an_empty_yaml_yields_the_defaults(tmp_path: Path) -> None:
    assert load_policy(_write(tmp_path, "")) == DEFAULT_POLICY
    assert load_policy(_write(tmp_path, "max_steps:\n")) == DEFAULT_POLICY


def test_the_loader_never_writes_the_yaml_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "max_steps: 2\ngroups:\n  R-EXIT:\n    steps: [reassign]\n")
    before = path.read_bytes()
    before_hash = hashlib.sha256(before).hexdigest()
    before_mtime = path.stat().st_mtime_ns

    load_policy(path)

    assert path.read_bytes() == before
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
    assert path.stat().st_mtime_ns == before_mtime


def test_the_defaults_are_immutable_and_survive_a_rejected_override(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError):
        DEFAULT_GROUPS[R_EXIT][0] = Step("review", 0)  # type: ignore[index]
    with pytest.raises(TypeError):
        DEFAULT_GROUPS[R_EXIT] = ()  # type: ignore[index]

    with pytest.raises(PolicyError):
        load_policy(_write(tmp_path, "max_steps: 99\n"))

    assert DEFAULT_GROUPS[R_EXIT] == (
        Step("reassign", 1),
        Step("reassign", 1),
        Step("split", 1),
    )


def test_tighten_accepts_the_raw_mapping_directly() -> None:
    policy = tighten(default_policy(), {"max_steps": 1})

    assert policy.max_steps == 1
    assert policy.plan_for("no_new_commit") == (Step("reassign", 1),)


def test_policy_aliases_expose_the_table_and_reason_map() -> None:
    policy = default_policy()

    assert policy.table is policy.groups
    assert policy.reasons is policy.reason_groups
    assert policy[R_EXIT] == DEFAULT_GROUPS[R_EXIT]
    assert R_EXIT in policy
    assert set(iter(policy)) == set(DEFAULT_GROUPS)
