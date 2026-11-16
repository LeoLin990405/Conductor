from __future__ import annotations

import pytest

from src.server.services.autonomy.roles import AgentState, RolePolicy
from src.server.services.autonomy.role_plan import (
    TaskSpec,
    normalize_scope,
    plan_developer_assignments,
    replan_after_failure,
    scopes_overlap,
)


def _agent(provider: str, **kwargs) -> AgentState:
    return AgentState(provider=provider, **kwargs)


def test_tasks_are_assigned_in_dependency_order():
    tasks = [
        TaskSpec("api", capabilities=frozenset({"backend"})),
        TaskSpec(
            "ui",
            capabilities=frozenset({"frontend"}),
            depends_on=("api",),
        ),
    ]
    agents = [
        _agent(
            "kimi",
            capabilities=frozenset({"backend", "frontend"}),
            capacity=2,
        ),
    ]

    plan = plan_developer_assignments(tasks, agents)

    assert [item.task_id for item in plan.assignments] == ["api", "ui"]
    assert plan.provider_of("api") == "kimi"


def test_capacity_is_respected_across_a_plan():
    tasks = [TaskSpec("one"), TaskSpec("two")]
    agents = [_agent("kimi", capacity=1, active=0), _agent("cursor", capacity=1)]

    plan = plan_developer_assignments(tasks, agents)

    assert plan.provider_of("one") == "kimi"
    assert plan.provider_of("two") == "cursor"


def test_a_dependency_that_could_not_be_assigned_blocks_its_child():
    tasks = [
        TaskSpec("backend", capabilities=frozenset({"backend"})),
        TaskSpec("child", depends_on=("backend",)),
    ]
    agents = [_agent("kimi", capabilities=frozenset({"frontend"}))]

    plan = plan_developer_assignments(tasks, agents)

    assert plan.provider_of("backend") == ""
    reasons = {item.task_id: item.reason for item in plan.unassigned}
    assert reasons["child"] == "dependency_unassigned:backend"


def test_reserved_planner_is_never_assigned_to_development():
    tasks = [TaskSpec("implementation")]
    agents = [_agent("claude", success_rate=1.0)]

    plan = plan_developer_assignments(tasks, agents)

    assert plan.assignments == ()
    assert plan.unassigned[0].reason == "no_capacity"


def test_cycle_is_reported_without_assignments():
    tasks = [TaskSpec("a", depends_on=("b",)), TaskSpec("b", depends_on=("a",))]
    agents = [_agent("kimi")]

    plan = plan_developer_assignments(tasks, agents)

    assert plan.assignments == ()
    assert {item.reason for item in plan.unassigned} == {"dependency_cycle"}


def test_missing_dependency_is_reported():
    tasks = [TaskSpec("a", depends_on=("missing",))]
    plan = plan_developer_assignments(tasks, [_agent("kimi")])

    assert plan.assignments == ()
    assert plan.unassigned[0].reason == "missing_dependency"


def test_duplicate_task_id_is_reported():
    tasks = [TaskSpec("a"), TaskSpec("a")]
    plan = plan_developer_assignments(tasks, [_agent("kimi")])

    assert plan.assignments == ()
    assert {item.reason for item in plan.unassigned} == {"duplicate_task_id"}


def test_failed_provider_can_be_excluded_for_a_retry():
    tasks = [TaskSpec("implementation")]
    agents = [
        _agent("kimi", success_rate=1.0),
        _agent("cursor", success_rate=0.5),
    ]

    plan = plan_developer_assignments(tasks, agents, exclude={"kimi"})

    assert plan.provider_of("implementation") == "cursor"


def test_failed_task_is_replanned_without_its_failed_provider():
    tasks = [TaskSpec("one"), TaskSpec("two")]
    agents = [
        _agent("kimi", success_rate=1.0),
        _agent("cursor", capacity=2),
    ]
    original = plan_developer_assignments(tasks, agents)

    replanned = replan_after_failure(
        original,
        tasks,
        agents,
        failed_task_id="one",
        failed_provider="kimi",
    )

    assert replanned.provider_of("one") == "cursor"
    assert replanned.provider_of("two") == "cursor"


def test_priority_orders_ready_tasks_after_dependencies():
    tasks = [
        TaskSpec("low", priority=0),
        TaskSpec("high", priority=10),
        TaskSpec("child", priority=100, depends_on=("low",)),
    ]
    agents = [_agent("kimi", capacity=3)]

    plan = plan_developer_assignments(tasks, agents)

    assert [item.task_id for item in plan.assignments] == ["high", "low", "child"]


def test_plan_audit_shape_contains_no_worker_content():
    plan = plan_developer_assignments(
        [TaskSpec("one"), TaskSpec("two")],
        [_agent("kimi", capacity=1)],
    )

    payload = plan.to_dict()

    assert payload["assignments"] == [
        {"task_id": "one", "provider": "kimi", "status": "assigned"}
    ]
    assert payload["unassigned"] == [
        {"task_id": "two", "reason": "no_capacity"}
    ]


def test_supervisor_mission_chooses_the_reserved_planner():
    from src.server.services.autonomy.role_plan import plan_supervisor_mission

    plan = plan_supervisor_mission(
        [TaskSpec("implementation")],
        [
            _agent("claude", success_rate=1.0),
            _agent("kimi", success_rate=1.0),
        ],
    )

    assert plan.planned is True
    assert plan.planner == "claude"
    assert plan.assignments.provider_of("implementation") == "kimi"


def test_supervisor_mission_reports_a_missing_planner():
    from src.server.services.autonomy.role_plan import plan_supervisor_mission

    plan = plan_supervisor_mission([TaskSpec("implementation")], [_agent("kimi")])

    assert plan.planned is False
    assert plan.planner_reason == "no_planner"
    assert plan.assignments.provider_of("implementation") == "kimi"


def test_supervisor_plan_audit_shape():
    from src.server.services.autonomy.role_plan import plan_supervisor_mission

    plan = plan_supervisor_mission(
        [TaskSpec("implementation")],
        [_agent("claude"), _agent("kimi")],
    )
    payload = plan.to_dict()

    assert payload["planner"] == "claude"
    assert payload["assignments"][0]["provider"] == "kimi"


def test_overlapping_write_scopes_are_not_assigned_in_parallel():
    tasks = [
        TaskSpec("parser-a", write_scope=("src/parser.py",)),
        TaskSpec("parser-b", write_scope=("src/parser.py",)),
    ]
    agents = [_agent("kimi", capacity=2)]

    plan = plan_developer_assignments(tasks, agents)

    assert plan.provider_of("parser-a") == "kimi"
    assert plan.provider_of("parser-b") == ""
    assert "write_scope_conflict:parser-a" in {
        item.reason for item in plan.unassigned
    }


def test_write_scope_path_spelling_is_normalized():
    tasks = [
        TaskSpec("one", write_scope=("./src/parser.py",)),
        TaskSpec("two", write_scope=("src/./parser.py",)),
    ]
    agents = [_agent("kimi", capacity=2)]

    plan = plan_developer_assignments(tasks, agents)

    assert plan.provider_of("one") == "kimi"
    assert plan.provider_of("two") == ""
    assert "write_scope_conflict:one" in {
        item.reason for item in plan.unassigned
    }


def test_scopes_overlap_covers_equality_prefix_and_glob():
    assert scopes_overlap("src/a.py", "src/a.py") is True
    assert scopes_overlap("src/a", "src/a/b.py") is True
    assert scopes_overlap("src/a/b.py", "src/a") is True
    assert scopes_overlap("src/**", "src/x.py") is True
    assert scopes_overlap("tests/**", "tests/unit/a.py") is True


def test_scopes_overlap_normalizes_spelling():
    assert scopes_overlap("./src/a.py", "src/a.py") is True
    assert scopes_overlap("src\\a\\b.py", "src/a/b.py") is True


def test_scopes_overlap_keeps_siblings_and_mistaken_prefixes_apart():
    assert scopes_overlap("src/a.py", "src/b.py") is False
    assert scopes_overlap("src/ab", "src/a/b.py") is False


def test_an_empty_scope_claims_nothing():
    assert scopes_overlap("", "src/a.py") is False
    assert scopes_overlap("src/a.py", "") is False
    assert scopes_overlap("", "") is False


@pytest.mark.parametrize("root", ["**", "*", ".", "./"])
def test_a_root_scope_overlaps_every_path(root):
    # A root scope claims the whole repository, so it touches any other line's
    # files no matter which directory that line names.
    assert scopes_overlap(root, "src/a.py") is True
    assert scopes_overlap("src/a.py", root) is True
    assert scopes_overlap(root, "tests/unit/x.py") is True
    assert scopes_overlap(root, "**") is True


@pytest.mark.parametrize(
    "root_glob, concrete",
    [
        ("*.py", "foo.py"),
        ("foo?.py", "foo1.py"),
        ("[ab].py", "a.py"),
    ],
)
def test_a_root_level_glob_conflicts_with_a_file_it_matches(root_glob, concrete):
    # A glob that starts at the repository root matches the concrete root file
    # it names, so the two overlap.
    assert scopes_overlap(root_glob, concrete) is True
    assert scopes_overlap(concrete, root_glob) is True


@pytest.mark.parametrize(
    "first, second",
    [
        ("*.py", "src/a.py"),
        ("foo?.py", "src/foo1.py"),
        ("[ab].py", "src/a.py"),
        ("src/*.py", "tests/a.py"),
    ],
)
def test_a_root_level_glob_stays_apart_from_a_nested_path(first, second):
    # A root-level glob cannot reach into a directory, so a scope inside one is
    # a different claim.
    assert scopes_overlap(first, second) is False


def test_a_double_star_segment_overlaps_itself_and_any_nested_path():
    # ``**`` matches anything below it. The explicit short-circuit in the walk
    # is a readability guard rather than the only thing that gets there: two
    # wildcard segments continue, and a walk that runs out on one side is an
    # overlap, so the conservative answer holds either way. Both readings are
    # pinned here so neither can be dropped quietly.
    assert scopes_overlap("**/*.py", "**/*.py") is True
    assert scopes_overlap("**/*.py", "src/a.py") is True
    assert scopes_overlap("src/**", "src/**") is True
    assert scopes_overlap("**", "**") is True
    assert scopes_overlap("**/*.py", "docs/deep/nested/x.py") is True


def test_normalisation_folds_dot_segments_and_nothing_else():
    # A validated scope has no ``..`` segment, so the normaliser must not be
    # the thing that decides what an escaping path means.
    assert normalize_scope("././") == "."
    assert normalize_scope("src//a/") == "src/a"
    assert normalize_scope("**") == "."
    assert ".." in normalize_scope("src/../x.py").split("/")


@pytest.mark.parametrize("spelling", ["./", "././", ".", "./././"])
def test_a_dotted_root_spelling_conflicts_with_everything(spelling):
    assert scopes_overlap(spelling, "src/a.py") is True
    assert scopes_overlap("src/a.py", spelling) is True


def test_a_root_scope_is_recorded_as_a_claim():
    policy = RolePolicy(developer_pool=("kimi", "glm"))
    plan = plan_developer_assignments(
        (
            TaskSpec(task_id="root", write_scope=("**",)),
            TaskSpec(task_id="other", write_scope=("src/a.py",)),
        ),
        [_agent("kimi"), _agent("glm")],
        policy=policy,
    )

    assert [(item.task_id, item.reason) for item in plan.unassigned] == [
        ("other", "write_scope_conflict:root")
    ]
    assert [item.task_id for item in plan.assignments] == ["root"]


def test_directory_prefix_write_scopes_conflict():
    tasks = [
        TaskSpec("base", write_scope=("src/a",)),
        TaskSpec("child", write_scope=("src/a/b.py",)),
    ]
    agents = [_agent("kimi", capacity=2)]

    plan = plan_developer_assignments(tasks, agents)

    assert plan.provider_of("base") == "kimi"
    assert plan.provider_of("child") == ""
    assert {item.task_id: item.reason for item in plan.unassigned}["child"] == (
        "write_scope_conflict:base"
    )


def test_glob_prefix_write_scopes_conflict():
    tasks = [
        TaskSpec("model", write_scope=("src/**",)),
        TaskSpec("route", write_scope=("src/app/route.py",)),
    ]
    agents = [_agent("kimi", capacity=2)]

    plan = plan_developer_assignments(tasks, agents)

    assert plan.provider_of("model") == "kimi"
    assert plan.provider_of("route") == ""
    assert {item.task_id: item.reason for item in plan.unassigned}["route"] == (
        "write_scope_conflict:model"
    )


def test_dependent_lines_with_overlapping_scopes_are_refused():
    tasks = [
        TaskSpec("first", write_scope=("src/shared.py",)),
        TaskSpec("second", write_scope=("src/shared.py",), depends_on=("first",)),
    ]
    agents = [_agent("kimi", capacity=2)]

    plan = plan_developer_assignments(tasks, agents)

    assert plan.provider_of("first") == "kimi"
    assert plan.provider_of("second") == ""
    assert {item.task_id: item.reason for item in plan.unassigned}["second"] == (
        "write_scope_conflict:first"
    )


def test_sibling_write_scopes_are_assigned_in_parallel():
    tasks = [
        TaskSpec("a", write_scope=("src/a.py",)),
        TaskSpec("b", write_scope=("src/b.py",)),
    ]
    agents = [_agent("kimi", capacity=2)]

    plan = plan_developer_assignments(tasks, agents)

    assert plan.provider_of("a") == "kimi"
    assert plan.provider_of("b") == "kimi"
    assert plan.unassigned == ()


def test_generator_tasks_survive_a_dependency_cycle():
    tasks = (
        task
        for task in (
            TaskSpec("a", depends_on=("b",)),
            TaskSpec("b", depends_on=("a",)),
        )
    )

    plan = plan_developer_assignments(tasks, [_agent("kimi")])

    assert plan.assignments == ()
    assert {item.task_id for item in plan.unassigned} == {"a", "b"}
    assert {item.reason for item in plan.unassigned} == {"dependency_cycle"}


def test_generator_tasks_survive_a_missing_dependency():
    tasks = (
        task
        for task in (
            TaskSpec("a", depends_on=("missing",)),
            TaskSpec("b"),
        )
    )

    plan = plan_developer_assignments(tasks, [_agent("kimi")])

    assert plan.assignments == ()
    assert {item.task_id for item in plan.unassigned} == {"a", "b"}
    assert {item.reason for item in plan.unassigned} == {"missing_dependency"}
