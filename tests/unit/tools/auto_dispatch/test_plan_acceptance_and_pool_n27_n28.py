"""N27 and N28: acceptance commands name test files; the plan fits the pool."""

from __future__ import annotations

import json

import pytest

from src.server.services.autonomy.roles import AgentState, RolePolicy
from src.tools.auto_dispatch.planner_prompt import PLANNER_SYSTEM_PROMPT, build_planner_prompt
from src.tools.auto_dispatch.planning import (
    ACCEPTANCE_TEST_DIRECTORY,
    EXCEEDS_DEVELOPER_POOL,
    LLMPlanner,
    PlanRejection,
    PlanResult,
    developer_places,
    runs_test_directory,
    validate_plan,
)
from src.tools.auto_dispatch.protocols import DispatchError
from src.tools.auto_dispatch.types import Line



@pytest.mark.parametrize(
    "entry",
    [
        "cmd: pytest tests/unit",
        "cmd: pytest -q tests/unit/",
        "cmd: python -m pytest -q tests",
        "cmd: .venv/bin/python -m pytest -x tests/unit/tools",
        "cmd: uv run pytest tests/unit/test_x.py tests/integration",
        "cmd: pytest -q",
        "cmd: LANG=C pytest -k foo",
        "  cmd: pytest tests/unit",
        "cmd: env -i python3.14 -m pytest -q tests/unit",
        "cmd: uv run --frozen pytest tests/unit",
        "cmd: pytest --log-cli-level INFO tests/unit",
        "cmd: python -W ignore -m pytest tests",
        "cmd: python -X dev -m pytest tests",
        "cmd: python3 -W ignore -X dev -I -m pytest -q tests/unit",
    ],
)
def test_a_pytest_run_of_a_directory_or_the_suite_is_detected(entry):
    assert runs_test_directory(entry)


@pytest.mark.parametrize(
    "entry",
    [
        "cmd: pytest -q tests/unit/test_x.py",
        "cmd: python -m pytest tests/unit/test_x.py::test_one -k 'a or b'",
        "cmd: pytest -m 'not slow' tests/unit/test_x.py",
        "cmd: ruff check src/x.py",
        "pytest tests/unit passes",
        "cmd: pytest 'unterminated",
        "cmd: pytest --log-cli-level INFO tests/x.py",
        "cmd: pytest --pyargs pkg.mod",
        "cmd: ruff check tests/pytest",
        "cmd: ruff check tests/pytest/test_x.py tests/unit",
        "cmd: python scripts/check.py -m pytest",
        "cmd: python -c 'print(1)' -m pytest tests",
        "cmd: python -W ignore -m pytest tests/unit/test_x.py",
        " cmdx: pytest tests/unit",
    ],
)
def test_a_named_test_file_or_other_check_is_not_a_directory(entry):
    assert not runs_test_directory(entry)


def _line(name: str, *, acceptance: str | None = None, depends_on=()) -> dict:
    return {
        "name": name,
        "task": f"Change src/{name}.py.",
        "depends_on": list(depends_on),
        "write_scope": [f"src/{name}.py"],
        "declared_paths": [f"src/{name}.py"],
        "acceptance": [acceptance or f"cmd: pytest -q tests/unit/test_{name}.py"],
        "risk_bearing": False,
    }


def _payload(*lines: dict) -> dict:
    return {
        "schema_version": 1,
        "spec": {"title": "t", "body": "## Goal\nx\n\n## Acceptance\n- y"},
        "lines": list(lines),
    }


POLICY = RolePolicy(developer_pool=("kimi", "grok", "cursor", "stepfun"))


def _agents(*names: str, capacity: int = 1) -> tuple[AgentState, ...]:
    return tuple(AgentState(provider=name, capacity=capacity) for name in names)


def test_validator_rejects_an_acceptance_command_on_a_test_directory():
    result = validate_plan(
        _payload(_line("one", acceptance="cmd: pytest tests/unit")),
        policy=POLICY,
        agents=_agents("kimi"),
    )
    assert result == PlanRejection(ACCEPTANCE_TEST_DIRECTORY, "one")
    assert result.code == "acceptance_test_directory:one"


# ----------------------------------------------------------- N28: pool limit


def test_validator_rejects_more_lines_than_the_pool_has_places():
    result = validate_plan(
        _payload(*[_line(f"l{i}") for i in range(5)]),
        policy=POLICY,
        agents=_agents("kimi", "grok", "cursor", "stepfun"),
    )
    assert isinstance(result, PlanRejection)
    assert result.reason == EXCEEDS_DEVELOPER_POOL
    assert result.code == "exceeds_developer_pool:5>4"


def test_dependent_lines_count_because_assignment_staffs_them_up_front():
    chain = [_line("l0")] + [_line(f"l{i}", depends_on=[f"l{i - 1}"]) for i in range(1, 3)]
    result = validate_plan(_payload(*chain), policy=POLICY, agents=_agents("kimi", "grok"))
    assert isinstance(result, PlanRejection)
    assert result.code == "exceeds_developer_pool:3>2"


def test_a_plan_that_fits_the_pool_is_accepted_and_fully_assigned():
    result = validate_plan(
        _payload(*[_line(f"l{i}") for i in range(4)]),
        policy=POLICY,
        agents=_agents("kimi", "grok", "cursor", "stepfun"),
    )
    assert isinstance(result, PlanResult)
    assert not result.assignment.unassigned


def test_places_follow_capacity_activity_and_the_pool():
    assert developer_places(_agents("kimi", capacity=3), POLICY, limit=8) == 3
    busy = (AgentState(provider="kimi", capacity=2, active=1),)
    assert developer_places(busy, POLICY, limit=8) == 1
    # A model outside the developer pool adds no place.
    assert developer_places(_agents("kimi", "codex"), POLICY, limit=8) == 1
    assert developer_places(_agents("kimi", capacity=5), POLICY, limit=2) == 2


def test_the_prompt_states_how_many_developers_are_available():
    prompt = build_planner_prompt("ship it", {"kimi": 1, "grok": 1}, developers=2)
    assert "Developers available: 2." in prompt
    assert "At most 2 line(s)" in prompt
    assert "exceeds_developer_pool" in prompt
    assert "never a whole test directory" in prompt
    assert "whole test directory" in PLANNER_SYSTEM_PROMPT
    # Without a count the roster's capacities are summed.
    assert "Developers available: 3." in build_planner_prompt("x", {"kimi": 2, "grok": 1})


@pytest.mark.asyncio
async def test_planner_retries_an_oversized_plan_with_the_pool_limit():
    replies = [
        json.dumps(_payload(*[_line(f"l{i}") for i in range(3)])),
        json.dumps(_payload(_line("l0"), _line("l1"))),
    ]
    prompts: list[str] = []

    async def complete(prompt: str, system: str) -> str:
        prompts.append(prompt)
        return replies[len(prompts) - 1]

    planner = LLMPlanner(complete, policy=POLICY, agents=_agents("kimi", "grok"))
    lines = await planner.plan("goal")
    assert len(lines) == 2
    assert "Developers available: 2." in prompts[0]
    assert "exceeds_developer_pool:3>2" in prompts[1]
    assert "at most 2 line(s)" in prompts[1]


def _repeating(reply: str):
    prompts: list[str] = []

    async def complete(prompt: str, system: str) -> str:
        prompts.append(prompt)
        return reply

    return complete, prompts


@pytest.mark.asyncio
async def test_an_oversized_plan_gets_one_retry_and_parks_under_its_own_code():
    complete, prompts = _repeating(json.dumps(_payload(*[_line(f"l{i}") for i in range(3)])))
    planner = LLMPlanner(complete, policy=POLICY, agents=_agents("kimi"))
    with pytest.raises(DispatchError, match="exceeds_developer_pool:3>1") as caught:
        await planner.plan("goal")
    assert caught.value.reason == EXCEEDS_DEVELOPER_POOL
    assert len(prompts) == 2


@pytest.mark.asyncio
async def test_a_directory_acceptance_gets_one_retry_and_parks_under_its_own_code():
    reply = json.dumps(_payload(_line("one", acceptance="cmd: pytest tests/unit")))
    complete, prompts = _repeating(reply)
    planner = LLMPlanner(complete, policy=POLICY, agents=_agents("kimi"))
    with pytest.raises(DispatchError, match="acceptance_test_directory:one") as caught:
        await planner.plan("goal")
    assert caught.value.reason == ACCEPTANCE_TEST_DIRECTORY
    assert len(prompts) == 2
    assert "whole test directory" in prompts[1]


def test_g2_and_the_validator_read_the_same_cmd_prefix():
    from src.tools.auto_dispatch.gates import acceptance_commands

    line = Line(id="l", name="l", task="t", acceptance=("cmd: pytest tests/unit",))
    assert acceptance_commands(line, None) == [["pytest", "tests/unit"]]
    assert runs_test_directory(line.acceptance[0])
    assert acceptance_commands(Line(id="l", name="l", task="t", acceptance=("cmdx: y",)), None) == []
