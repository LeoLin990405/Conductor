"""DAG assignment: dispatch lines in, a deterministic plan out."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.server.services.autonomy.role_plan import AssignmentPlan
from src.server.services.autonomy.roles import AgentState, RolePolicy
from src.tools.auto_dispatch.selection import assign_dag, dag_agents_from_policy
from src.tools.auto_dispatch.types import Line


def _line(
    index: int,
    *,
    depends_on: tuple[str, ...] = (),
    write_scope: tuple[str, ...] = (),
) -> Line:
    """One work line; the default scope keeps two lines from colliding."""
    return Line(
        id=f"line-{index}",
        name=f"line-{index}",
        task=f"do thing {index}",
        depends_on=depends_on,
        write_scope=write_scope or (f"src/line{index}.py",),
    )


def test_a_two_line_dag_is_assigned_in_dependency_order() -> None:
    policy = RolePolicy.default()
    lines = [
        _line(0, write_scope=("src/server/services/model.py",)),
        _line(
            1,
            depends_on=("line-0",),
            write_scope=("src/server/app/route.py",),
        ),
    ]

    plan = assign_dag(lines, dag_agents_from_policy(policy), policy=policy)

    assert plan.unassigned == ()
    assert [item.task_id for item in plan.assignments] == ["line-0", "line-1"]
    assert all(item.status == "assigned" for item in plan.assignments)
    assert {item.provider for item in plan.assignments} <= set(policy.developer_pool)


def test_a_reserved_role_never_receives_a_work_task() -> None:
    policy = RolePolicy.default()
    reserved = set(policy.planner) | set(policy.primary_reviewer)
    agents = (
        AgentState(provider=policy.planner[0], capacity=1),
        AgentState(provider=policy.primary_reviewer[0], capacity=1),
        *dag_agents_from_policy(policy),
    )
    lines = [_line(0), _line(1), _line(2)]

    plan = assign_dag(lines, agents, policy=policy)

    assert len(plan.assignments) == len(lines)
    assert plan.unassigned == ()
    for item in plan.assignments:
        assert item.provider not in reserved
        assert item.provider in policy.developer_pool


def test_capacity_of_one_leaves_the_seventh_line_unassigned() -> None:
    lines = [_line(index) for index in range(7)]
    agents = (AgentState(provider="kimi", capacity=1),)

    plan = assign_dag(lines, agents)

    # One free place: the first line takes it and every later line is refused.
    assert [item.task_id for item in plan.assignments] == ["line-0"]
    reasons = {item.task_id: item.reason for item in plan.unassigned}
    assert list(reasons) == [f"line-{index}" for index in range(1, 7)]
    assert reasons["line-6"] == "no_capacity"


def test_one_free_place_per_pool_member_refuses_the_next_line() -> None:
    # One free place per pool member: the line past the roster size has nowhere
    # to go. Derived from the roster so adding a developer does not break it.
    from src.server.services.autonomy.roles import RolePolicy

    pool_size = len(RolePolicy.default().developer_pool)
    lines = [_line(index) for index in range(pool_size + 1)]

    plan = assign_dag(lines, dag_agents_from_policy())

    assert [item.task_id for item in plan.assignments] == [
        f"line-{index}" for index in range(pool_size)
    ]
    assert [(item.task_id, item.reason) for item in plan.unassigned] == [
        (f"line-{pool_size}", "no_capacity")
    ]


def test_an_unassigned_dependency_blocks_its_child() -> None:
    lines = [_line(0), _line(1, depends_on=("line-0",))]
    # The only developer cannot take work, so line-0 is never placed.
    agents = (AgentState(provider="kimi", available=False),)

    plan = assign_dag(lines, agents)

    reasons = {item.task_id: item.reason for item in plan.unassigned}
    assert plan.assignments == ()
    assert reasons["line-0"] == "no_capacity"
    assert reasons["line-1"] == "dependency_unassigned:line-0"


def test_overlapping_write_scopes_leave_the_second_line_unassigned() -> None:
    lines = [
        _line(0, write_scope=("src/server/services/model.py",)),
        _line(1, write_scope=("src/server/services/model.py",)),
    ]

    plan = assign_dag(lines, dag_agents_from_policy())

    assert [item.task_id for item in plan.assignments] == ["line-0"]
    assert [item.task_id for item in plan.unassigned] == ["line-1"]
    assert plan.unassigned[0].reason.startswith("write_scope_conflict:")
    assert plan.unassigned[0].reason == "write_scope_conflict:line-0"


def test_dag_agents_from_policy_mirrors_the_developer_pool() -> None:
    policy = RolePolicy(developer_pool=("kimi", "glm", "stepfun"))

    agents = dag_agents_from_policy(policy)

    assert [agent.provider for agent in agents] == ["kimi", "glm", "stepfun"]
    assert all(agent.capacity == 1 for agent in agents)
    assert dag_agents_from_policy() == tuple(
        AgentState(provider=name, capacity=1)
        for name in RolePolicy.default().developer_pool
    )


def test_an_empty_plan_has_no_assignments_and_no_unassigned() -> None:
    plan = assign_dag(iter([]), dag_agents_from_policy())

    assert plan.assignments == ()
    assert plan.unassigned == ()


def test_a_root_level_glob_line_conflicts_with_a_root_file_line() -> None:
    # The assignment stage sees the same overlap rule the validator does, so a
    # root-level glob line and a concrete root-file line cannot both run.
    from src.server.services.autonomy.roles import RolePolicy

    plan = assign_dag(
        [
            _line(0, write_scope=("*.py",)),
            _line(1, write_scope=("foo.py",)),
        ],
        dag_agents_from_policy(),
        policy=RolePolicy.default(),
    )

    assert [(item.task_id, item.reason) for item in plan.unassigned] == [
        ("line-1", "write_scope_conflict:line-0")
    ]


def test_a_dotted_root_spelling_conflicts_with_a_nested_path() -> None:
    from src.server.services.autonomy.roles import RolePolicy

    plan = assign_dag(
        [
            _line(0, write_scope=("././",)),
            _line(1, write_scope=("src/a.py",)),
        ],
        dag_agents_from_policy(),
        policy=RolePolicy.default(),
    )

    assert [(item.task_id, item.reason) for item in plan.unassigned] == [
        ("line-1", "write_scope_conflict:line-0")
    ]


_VECTORS = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "fixtures"
        / "write_scope_overlap_vectors_v1.json"
    ).read_text(encoding="utf-8")
)


@pytest.mark.parametrize("first, second", _VECTORS["overlap"])
def test_an_overlapping_pair_is_split_between_developers(first: str, second: str) -> None:
    # The assignment stage reads the same vectors the validator does: an
    # overlapping pair must not both be placed.
    plan = assign_dag(
        [
            _line(0, write_scope=(first,)),
            _line(1, write_scope=(second,)),
        ],
        dag_agents_from_policy(),
    )

    placed = [item.task_id for item in plan.assignments]
    assert len(placed) == 1
    assert [item.task_id for item in plan.unassigned] == [
        f"line-{1 - int(placed[0].split('-')[-1])}"
    ]


@pytest.mark.parametrize("first, second", _VECTORS["disjoint"])
def test_a_disjoint_pair_is_assigned_to_two_developers(first: str, second: str) -> None:
    plan = assign_dag(
        [
            _line(0, write_scope=(first,)),
            _line(1, write_scope=(second,)),
        ],
        dag_agents_from_policy(),
    )

    assert [item.task_id for item in plan.assignments] == ["line-0", "line-1"]
    assert plan.unassigned == ()


#: Whitespace around a scope: leading, trailing, or both. Every check runs on
#: the trimmed spelling, so none of these may launder an escaping path.
_SCOPE_PADDINGS: tuple[tuple[str, str], ...] = (
    ("", ""),
    (" ", ""),
    ("  ", ""),
    ("\t", ""),
    ("", " "),
    ("", "\t"),
    ("", "\n"),
    (" \t\n", ""),
    ("", " \t\n"),
    (" ", " "),
)

#: Scope bodies that are invalid whatever surrounds them.
_ESCAPING_BODIES: tuple[str, ...] = ("src/../x.py", "/etc/x")

#: Reasons a scope that trims to nothing may carry: the planner's precedence
#: names the more specific ``empty_write_scope``, while the assignment stage
#: reports its own refusals as ``invalid_write_scope``.
_EMPTY_SCOPE_REASONS: tuple[str, ...] = (
    "empty_write_scope:line-0",
    "invalid_write_scope:line-0",
)


class TestAssignDagRejectsInvalidScopes:
    """A scope that escapes the repository is never assigned or claimed.

    ``assign_dag`` is a public entry point, so it re-checks the scopes it is
    handed instead of trusting the planner's validator. This class replaces
    ``test_an_escaping_or_absolute_scope_is_never_assigned``, which asserted
    only that an invalid scope produced no *conflict*: that held because the
    scope never became a claim, so it passed just as happily while the
    escaping line itself was assigned to a developer and written into the
    archive. An invalid scope now leaves its line unassigned with a reason.
    """

    @pytest.mark.parametrize(
        "scope", _VECTORS["rejected"] + _VECTORS["rejected_whitespace"]
    )
    def test_a_rejected_scope_leaves_its_line_unassigned(self, scope: str) -> None:
        plan = assign_dag(
            [
                _line(0, write_scope=(scope,)),
                _line(1, write_scope=("src/a.py",)),
            ],
            dag_agents_from_policy(),
        )

        # The safe line keeps its place; the escaping one is never placed.
        assert [item.task_id for item in plan.assignments] == ["line-1"]
        reasons = {item.task_id: item.reason for item in plan.unassigned}
        if scope.strip():
            assert reasons["line-0"] == "invalid_write_scope:line-0"
        else:
            assert reasons["line-0"] in _EMPTY_SCOPE_REASONS

    @pytest.mark.parametrize("body", _ESCAPING_BODIES)
    @pytest.mark.parametrize("leading, trailing", _SCOPE_PADDINGS)
    def test_surrounding_whitespace_does_not_launder_a_scope(
        self, leading: str, trailing: str, body: str
    ) -> None:
        # Trim first, then check that same string: " /etc/x" is the absolute
        # path "/etc/x" and "src/.. " is the escaping "src/..".
        plan = assign_dag(
            [
                _line(0, write_scope=(f"{leading}{body}{trailing}",)),
                _line(1, write_scope=("src/a.py",)),
            ],
            dag_agents_from_policy(),
        )

        assert [item.task_id for item in plan.assignments] == ["line-1"]
        reasons = {item.task_id: item.reason for item in plan.unassigned}
        assert reasons["line-0"] == "invalid_write_scope:line-0"

    @pytest.mark.parametrize("scope", ("", " ", "\t", "\n", " \t\n "))
    def test_a_blank_scope_is_refused(self, scope: str) -> None:
        # A blank scope declares nothing, so the line cannot be placed at all:
        # it is a refusal, not a line with no claim to compare against.
        plan = assign_dag(
            [
                _line(0, write_scope=(scope,)),
                _line(1, write_scope=("src/a.py",)),
            ],
            dag_agents_from_policy(),
        )

        assert [item.task_id for item in plan.assignments] == ["line-1"]
        reasons = {item.task_id: item.reason for item in plan.unassigned}
        assert reasons["line-0"] in _EMPTY_SCOPE_REASONS

    @pytest.mark.parametrize("padding", _SCOPE_PADDINGS)
    def test_a_padded_but_valid_scope_is_still_assigned(
        self, padding: tuple[str, str]
    ) -> None:
        # The defence trims before it checks, so padding around a valid scope
        # is not itself a reason to refuse the line.
        leading, trailing = padding
        plan = assign_dag(
            [_line(0, write_scope=(f"{leading}src/a.py{trailing}",))],
            dag_agents_from_policy(),
        )

        assert [item.task_id for item in plan.assignments] == ["line-0"]
        assert plan.unassigned == ()


def test_validate_write_scope_trims_before_it_judges() -> None:
    # assign_dag shares the planner's scope check, so the check must do its own
    # trimming: a caller that hands it " /etc/x" must not get an assignment.
    from src.tools.auto_dispatch.planning import validate_write_scope

    assert validate_write_scope(" /etc/x") is False
    assert validate_write_scope("\t../outside.py ") is False
    assert validate_write_scope(" C:\\x") is False
    assert validate_write_scope(" ~/secret") is False
    assert validate_write_scope("  src/a.py\t") is True
    assert validate_write_scope("src/a.py") is True


def test_a_padded_invalid_scope_is_refused_by_the_helper_alone() -> None:
    # The defence-in-depth path must not depend on the validator having already
    # trimmed: assign_dag hands the raw stored spelling straight to the helper.
    from src.server.services.autonomy.roles import RolePolicy

    plan = assign_dag(
        [_line(0, write_scope=("  /etc/x  ",))],
        dag_agents_from_policy(),
        policy=RolePolicy.default(),
    )

    assert plan.assignments == ()
    assert [(item.task_id, item.reason) for item in plan.unassigned] == [
        ("line-0", "invalid_write_scope:line-0")
    ]


#: The three reason families a refused write-scope line may carry, before the
#: ``:<line_id>`` suffix ``assign_dag`` appends to each of them.
_SCOPE_REFUSAL_FAMILIES: tuple[str, ...] = (
    "empty_write_scope",
    "invalid_text",
    "invalid_write_scope",
)

#: Every write scope that must refuse its whole line: the empty tuple, each
#: single rejected item, and each mixed-blank list from the shared vectors.
_REFUSED_SCOPES: tuple[tuple[str, ...], ...] = (
    (),
    *tuple((item,) for item in _VECTORS["rejected_items"]),
    *tuple(tuple(scopes) for scopes in _VECTORS["mixed_blank_positions"]),
)


def _scope_id(scope: tuple[str, ...]) -> str:
    """A readable pytest id for one refused write-scope tuple."""
    return "[{}]".format(", ".join(repr(part) for part in scope))


def _refused_line(scope: tuple[str, ...]) -> Line:
    """One line carrying exactly ``scope``; ``_line`` cannot make an empty one."""
    if scope:
        return _line(0, write_scope=scope)
    return Line(id="line-0", name="line-0", task="do thing 0", write_scope=())


class TestAssignDagRejectsEveryBadItem:
    """Every bad write-scope item refuses its whole line at ``assign_dag``.

    ``assign_dag`` is a public entry point, so a caller that builds its own
    ``Line`` objects gets the same refusal the planner validator gives: the one
    ``check_scope_item`` check runs here too, and a line is refused as a whole
    -- no item silently dropped -- with the first failing item's reason.

    The no-archive property (a refused line writes no SPEC.md / plan.json) is
    proven at the planner layer in ``test_planning_dag.py``. This layer cannot
    reach the archive, so it proves the fact that write depends on: a refused
    line is never assigned and never becomes a claim.
    """

    def test_an_empty_write_scope_tuple_is_refused(self) -> None:
        # ``_line`` substitutes a default scope for an empty one, so the empty
        # tuple is built directly: a line that declares nothing to write has
        # nowhere to run and is refused rather than placed with an empty claim.
        plan = assign_dag([_refused_line(())], dag_agents_from_policy())

        assert plan.assignments == ()
        assert [(row.task_id, row.reason) for row in plan.unassigned] == [
            ("line-0", "empty_write_scope:line-0")
        ]

    @pytest.mark.parametrize("item", _VECTORS["rejected_items"])
    def test_every_rejected_item_refuses_its_line(self, item: str) -> None:
        # The reason is the item's own: a blank trims to nothing, a NUL is not
        # text, and any other spelling is a path that escapes the tree.
        plan = assign_dag([_refused_line((item,))], dag_agents_from_policy())

        reasons = {row.task_id: row.reason for row in plan.unassigned}
        if not item.strip():
            assert reasons["line-0"] == "empty_write_scope:line-0"
        elif "\x00" in item:
            assert reasons["line-0"] == "invalid_text:line-0"
        else:
            assert reasons["line-0"] == "invalid_write_scope:line-0"

    @pytest.mark.parametrize("scopes", _VECTORS["mixed_blank_positions"])
    def test_a_blank_member_refuses_the_whole_line_and_claims_nothing(
        self, scopes: list[str]
    ) -> None:
        # A blank member refuses the whole line; the valid scope beside it is
        # not silently kept and placed. Because the line is refused it claims
        # nothing, so a later line may take the very scope it carried.
        valid = [text.strip() for text in scopes if text.strip()]
        lines = [_line(0, write_scope=tuple(scopes))]
        lines += [_line(i + 1, write_scope=(text,)) for i, text in enumerate(valid)]

        plan = assign_dag(lines, dag_agents_from_policy())

        reasons = {row.task_id: row.reason for row in plan.unassigned}
        assigned_ids = [row.task_id for row in plan.assignments]
        assert reasons["line-0"] == "empty_write_scope:line-0"
        assert "line-0" not in assigned_ids
        assert assigned_ids == [f"line-{i + 1}" for i in range(len(valid))]

    def test_a_padded_valid_scope_is_assigned_and_claimed_trimmed(self) -> None:
        # The trimmed spelling is what is stored and claimed: a padded scope and
        # its trimmed spelling are the same claim, so a second line carrying the
        # trimmed spelling conflicts instead of running alongside the padded one.
        plan = assign_dag(
            [
                _line(0, write_scope=("  src/a.py\t",)),
                _line(1, write_scope=("src/a.py",)),
            ],
            dag_agents_from_policy(),
        )

        assert [row.task_id for row in plan.assignments] == ["line-0"]
        assert [(row.task_id, row.reason) for row in plan.unassigned] == [
            ("line-1", "write_scope_conflict:line-0")
        ]

    def _assert_no_assignment(self, plan: AssignmentPlan) -> None:
        """A refused line is never assigned, so it never reaches the archive.

        The archive write is a planner-layer side effect, proven absent in
        ``test_planning_dag.py``; this layer sees only the plan, so it proves
        the fact that write depends on: nothing is assigned, and the refusal
        names one of the three write-scope reason families.
        """
        assert plan.assignments == ()
        assert plan.unassigned
        for row in plan.unassigned:
            assert row.reason.split(":", 1)[0] in _SCOPE_REFUSAL_FAMILIES

    @pytest.mark.parametrize("scope", _REFUSED_SCOPES, ids=_scope_id)
    def test_a_refused_scope_is_never_assigned_or_claimed(
        self, scope: tuple[str, ...]
    ) -> None:
        # One refused line alone: nothing is placed, so nothing is claimed and
        # nothing is written to the archive (the archive half is proven at the
        # planner layer; see the class docstring).
        plan = assign_dag([_refused_line(scope)], dag_agents_from_policy())

        self._assert_no_assignment(plan)


def _work(
    line_id: str,
    *,
    scope: tuple[str, ...] = (),
    depends_on: tuple[str, ...] = (),
) -> Line:
    return Line(
        id=line_id,
        name=line_id,
        task=f"do {line_id}",
        write_scope=scope or (f"src/{line_id}.py",),
        depends_on=depends_on,
    )


def _kept(
    plan: AssignmentPlan, ids: set[str]
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    return (
        tuple(item for item in plan.assignments if item.task_id in ids),
        tuple(item for item in plan.unassigned if item.task_id in ids),
    )


class TestFilteredParentDoesNotPoisonAnUnrelatedLine:
    """A refused parent marks only its dependents, never an unrelated line.

    Before the fix, ``assign_dag`` dropped P and left C depending on an id the
    role planner had never been given. ``_topological_order`` then returned
    ``missing_dependency`` for every remaining task, so U was unassigned with
    that reason even though U does not depend on P. U alone was assigned.
    """

    def _assert_graph(
        self,
        lines: list[Line],
        keep: set[str],
        blocked: dict[str, str],
        refused: dict[str, str],
        policy: RolePolicy | None = None,
    ) -> None:
        resolved = policy or RolePolicy.default()
        agents = dag_agents_from_policy(resolved)
        plan = assign_dag(lines, agents, policy=resolved)
        again = assign_dag(lines, dag_agents_from_policy(resolved), policy=resolved)
        assert plan == again

        alone = assign_dag(
            [line for line in lines if line.id in keep],
            dag_agents_from_policy(resolved),
            policy=resolved,
        )
        assert _kept(plan, keep) == (alone.assignments, alone.unassigned)

        reasons = {item.task_id: item.reason for item in plan.unassigned}
        for line_id, ancestor in blocked.items():
            assert reasons[line_id] == f"missing_dependency:{ancestor}"
            assert line_id not in {item.task_id for item in plan.assignments}
        for line_id, reason in refused.items():
            assert reasons[line_id] == reason
        extra = set(reasons) - set(blocked) - set(refused)
        assert extra == {item.task_id for item in alone.unassigned}

    def test_an_unrelated_line_is_not_marked_missing_dependency(self) -> None:
        # This is the review's graph. It failed before the fix: U came back
        # unassigned with the bare reason ``missing_dependency``.
        lines = [
            _work("P", scope=("/etc/passwd",)),
            _work("C", depends_on=("P",)),
            _work("U"),
        ]

        self._assert_graph(
            lines,
            keep={"U"},
            blocked={"C": "P"},
            refused={"P": "invalid_write_scope:P"},
        )

    def test_a_chain_names_the_filtered_ancestor_not_the_child(self) -> None:
        lines = [
            _work("P", scope=("/etc/passwd",)),
            _work("C", depends_on=("P",)),
            _work("D", depends_on=("C",)),
            _work("U"),
        ]

        self._assert_graph(
            lines,
            keep={"U"},
            blocked={"C": "P", "D": "P"},
            refused={"P": "invalid_write_scope:P"},
        )

    def test_a_diamond_names_the_filtered_ancestor(self) -> None:
        lines = [
            _work("P", scope=("/etc/passwd",)),
            _work("A", depends_on=("P",)),
            _work("B", depends_on=("P",)),
            _work("D", depends_on=("A", "B")),
            _work("U"),
        ]

        self._assert_graph(
            lines,
            keep={"U"},
            blocked={"A": "P", "B": "P", "D": "P"},
            refused={"P": "invalid_write_scope:P"},
        )

    def test_two_independent_illegal_parents_stay_apart(self) -> None:
        # The first depends_on entry wins when a line can see two refused
        # parents, so M and N are deterministic and not a set intersection.
        lines = [
            _work("P1", scope=("/etc/passwd",)),
            _work("P2", scope=("../outside",)),
            _work("C1", depends_on=("P1",)),
            _work("C2", depends_on=("P2",)),
            _work("M", depends_on=("P1", "P2")),
            _work("N", depends_on=("P2", "P1")),
            _work("U"),
        ]

        self._assert_graph(
            lines,
            keep={"U"},
            blocked={"C1": "P1", "C2": "P2", "M": "P1", "N": "P2"},
            refused={
                "P1": "invalid_write_scope:P1",
                "P2": "invalid_write_scope:P2",
            },
        )

    def test_unrelated_lines_match_the_plan_without_the_filtered_parent(self) -> None:
        # One seat. U still takes it, and V is still ``no_capacity``, exactly
        # as they are when P and C are not in the graph at all.
        policy = RolePolicy(developer_pool=("kimi",))
        lines = [
            _work("P", scope=("/etc/passwd",)),
            _work("C", depends_on=("P",)),
            _work("U"),
            _work("V"),
        ]

        self._assert_graph(
            lines,
            keep={"U", "V"},
            blocked={"C": "P"},
            refused={"P": "invalid_write_scope:P"},
            policy=policy,
        )

    def test_a_blocked_child_does_not_conflict_with_an_unrelated_line(self) -> None:
        # C and U name the same scope. C depends on filtered P, so C is not
        # assignable and the overlap is not a conflict. U is assigned.
        lines = [
            _work("P", scope=("/etc/passwd",)),
            _work("C", scope=("src/shared.py",), depends_on=("P",)),
            _work("U", scope=("src/shared.py",)),
        ]

        self._assert_graph(
            lines,
            keep={"U"},
            blocked={"C": "P"},
            refused={"P": "invalid_write_scope:P"},
        )
        plan = assign_dag(lines, dag_agents_from_policy(), policy=RolePolicy.default())
        assert all("write_scope_conflict" not in item.reason for item in plan.unassigned)


def test_two_lines_with_the_same_id_are_not_assigned() -> None:
    lines = [_work("dup"), _work("dup")]

    plan = assign_dag(lines, dag_agents_from_policy())

    assert plan.assignments == ()
    assert {item.reason for item in plan.unassigned} == {"duplicate_task_id:dup"}


def test_two_of_three_lines_with_the_same_id_reject_the_whole_plan() -> None:
    lines = [_work("dup"), _work("keep"), _work("dup")]

    plan = assign_dag(lines, dag_agents_from_policy())

    assert plan.assignments == ()
    assert [item.task_id for item in plan.unassigned] == ["dup", "keep", "dup"]
    assert {item.reason for item in plan.unassigned} == {"duplicate_task_id:dup"}


def test_a_repeated_id_wins_over_an_illegal_scope() -> None:
    # The illegal twin used to be filtered and the legal twin dropped with it.
    # The repeated id is the refusal, and nothing is claimed.
    lines = [_work("dup", scope=("/etc/passwd",)), _work("dup")]

    plan = assign_dag(lines, dag_agents_from_policy())

    assert plan.assignments == ()
    assert {item.reason for item in plan.unassigned} == {"duplicate_task_id:dup"}
