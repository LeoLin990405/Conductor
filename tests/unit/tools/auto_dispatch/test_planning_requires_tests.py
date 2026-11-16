"""N46: a code line's acceptance runs its own tests, or the plan is refused.

Seen live (R8c line-0-r1-3): G2 was a ruff check and an import check, so
``focused_tests_passed`` covered nothing and the line was reviewed as tested.
Plan validation now refuses a code line whose acceptance runs no test, and one
whose test command does not name the test files the line changes or its task
names. The refusal is correctable: the model is told the rule once and asked for
the whole plan again, and a plan that still breaks it parks as a planner
failure.
"""

from __future__ import annotations

import json

import pytest

from src.tools.auto_dispatch.check_modes import ENFORCE, REQUIRE_TESTS_ENV

from src.server.services.autonomy.roles import AgentState, RolePolicy
from src.tools.auto_dispatch import (
    AlwaysPassReviewer,
    Collaborators,
    Dispatch,
    DispatchMachine,
    FileApprovalGate,
    InMemoryDispatchStore,
    InMemoryOutcomeStore,
    NoopMerger,
    RecordingLauncher,
    RoundRobinSelector,
    ScriptedMonitor,
)
from src.tools.auto_dispatch.planner_reason import (
    is_planner_failed,
)
from src.tools.auto_dispatch.planning import (
    ACCEPTANCE_MISSING_TEST,
    ACCEPTANCE_TEST_DIRECTORY,
    LLMPlanner,
    PlanRejection,
    PlanResult,
    is_test_command,
    is_test_path,
    validate_plan,
    writes_code,
)
from src.tools.auto_dispatch.planning import test_command_targets as command_targets


@pytest.fixture(autouse=True)
def _enforce_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests pin the line's enforce behaviour; check_modes covers the rest (N58)."""
    monkeypatch.setenv(REQUIRE_TESTS_ENV, ENFORCE)


POLICY = RolePolicy(developer_pool=("kimi", "grok", "cursor", "stepfun"))


def _agents(*names: str, capacity: int = 1) -> tuple[AgentState, ...]:
    return tuple(AgentState(provider=name, capacity=capacity) for name in names)


def _line(
    name: str,
    *,
    acceptance: list[str] | None = None,
    write_scope: list[str] | None = None,
    task: str | None = None,
) -> dict:
    return {
        "name": name,
        "task": task if task is not None else f"Change src/{name}.py.",
        "depends_on": [],
        "write_scope": write_scope if write_scope is not None else [f"src/{name}.py"],
        "declared_paths": write_scope if write_scope is not None else [f"src/{name}.py"],
        "acceptance": acceptance
        if acceptance is not None
        else [f"cmd: pytest -q tests/unit/test_{name}.py"],
        "risk_bearing": False,
    }


def _payload(*lines: dict) -> dict:
    return {
        "schema_version": 1,
        "spec": {"title": "t", "body": "## Goal\nx\n\n## Acceptance\n- y"},
        "lines": list(lines),
    }


def _validate(*lines: dict):
    # N73 S4: the deployment switch is record-only, so the rule itself is
    # pinned through the explicit argument.
    return validate_plan(
        _payload(*lines), policy=POLICY, agents=_agents("kimi"), require_tests_mode=ENFORCE
    )


# -- what counts as a test file, a test command and a code line --------------


@pytest.mark.parametrize(
    "path",
    [
        "tests/unit/test_x.py",
        "./tests/unit/test_x.py",
        "tests/unit/test_*.py",
        "src/pkg/test_x.py",
        "src/pkg/x_test.py",
        "web/src/App.test.tsx",
        "web/src/App.spec.ts",
        "web/__tests__/App.tsx",
        "spec/models/user_spec.rb",
        "conftest.py",
        "src/pkg/conftest.py",
    ],
)
def test_a_test_file_is_recognised(path):
    assert is_test_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "src/x.py",
        "src/pkg/module.py",
        "docs/plan.md",
        "tests",
        "test",
        "tests/unit",
        "docs/tests/notes.md",
        "src/contest.py",
        # A helper the tests beside it load, not a test of its own: it is
        # exercised by a command naming one of those tests.
        "tests/unit/server/app/x.py",
        "tests/unit/pkg/helper.py",
        "tests/unit/pkg/__init__.py",
    ],
)
def test_what_is_not_a_test_file(path):
    assert not is_test_path(path)


@pytest.mark.parametrize(
    "entry",
    [
        "cmd: pytest -q tests/unit/test_x.py",
        "cmd: uv run pytest -q tests/unit/test_x.py",
        "cmd: python -m pytest -q tests/unit/test_x.py",
        "cmd: env -i python3 -m pytest -q tests/unit/test_x.py",
        "cmd: vitest run",
        "cmd: pnpm test",
        "cmd: go test ./...",
        "cmd: cargo test --quiet",
        "cmd: pytest -q -k one tests/unit/test_x.py",
    ],
)
def test_a_test_command_is_recognised(entry):
    assert is_test_command(entry)


@pytest.mark.parametrize(
    "entry",
    [
        "cmd: ruff check src/x.py",
        "cmd: python -c 'import x'",
        "cmd: ruff check tests/pytest",
        "the tests pass",
        "pytest tests/unit passes",
        "cmd: pytest 'unterminated",
        "cmdx: pytest tests/unit",
        # A mode that reports about the runner and exits zero: it runs no
        # test, so an acceptance list built from one covers nothing.
        "cmd: pytest --version",
        "cmd: pytest -V",
        "cmd: pytest --version tests/unit/test_x.py",
        "cmd: pytest --help",
        "cmd: pytest -h",
        "cmd: python -m pytest --collect-only tests/unit/test_x.py",
        "cmd: pytest --co -q tests/unit/test_x.py",
        "cmd: pytest --fixtures",
        "cmd: pytest --markers",
        "cmd: cargo test --no-run",
        "cmd: vitest --version",
    ],
)
def test_what_is_not_a_test_command(entry):
    assert not is_test_command(entry)


@pytest.mark.parametrize(
    ("entry", "targets"),
    [
        ("cmd: pytest -q tests/unit/test_x.py", ("tests/unit/test_x.py",)),
        (
            "cmd: pytest -q tests/unit/test_x.py::test_one",
            ("tests/unit/test_x.py",),
        ),
        (
            "cmd: pytest -q -k one tests/unit/test_x.py",
            ("tests/unit/test_x.py",),
        ),
        (
            "cmd: pytest -q --rootdir . tests/unit/test_x.py",
            ("tests/unit/test_x.py",),
        ),
        ("cmd: pnpm test web/src/App.test.tsx", ("web/src/App.test.tsx",)),
        ("cmd: pytest --pyargs pkg.mod", ()),
        ("cmd: pytest -q", ()),
        ("cmd: ruff check tests/unit", ()),
        ("the tests pass", ()),
        # A mode that runs no test names no test either.
        ("cmd: pytest --collect-only tests/unit/test_x.py", ()),
    ],
)
def test_the_test_files_a_command_names(entry, targets):
    assert command_targets(entry) == targets


@pytest.mark.parametrize(
    ("scope", "code"),
    [
        (["src/x.py"], True),
        (["src/pkg/module.py", "tests/unit/test_x.py"], True),
        (["docs/plan.md"], False),
        (["docs/plan.md", "docs/other.rst"], False),
        (["docs/*.md"], False),
        (["docs/plan.md", "src/x.py"], True),
        ([], False),
        (["README"], True),
    ],
)
def test_whether_a_write_scope_changes_code(scope, code):
    assert writes_code(scope) is code


# -- the rule itself ---------------------------------------------------------


def test_a_code_line_whose_acceptance_runs_no_test_is_refused():
    result = _validate(
        _line(
            "one",
            acceptance=[
                "cmd: ruff check src/one.py",
                "cmd: python -c 'import one'",
            ],
        )
    )
    assert result == PlanRejection(ACCEPTANCE_MISSING_TEST, "one")
    assert result.code == "acceptance_missing_test:one"


def test_a_human_readable_assertion_is_not_a_test_command():
    result = _validate(_line("one", acceptance=["the tests pass"]))
    assert result.code == "acceptance_missing_test:one"


def test_a_command_running_another_lines_test_file_is_refused():
    result = _validate(
        _line(
            "one",
            write_scope=["src/one.py", "tests/unit/test_one.py"],
            acceptance=["cmd: pytest -q tests/unit/test_other.py"],
        )
    )
    assert result == PlanRejection(
        ACCEPTANCE_MISSING_TEST, "one:tests/unit/test_one.py"
    )
    assert result.code == "acceptance_missing_test:one:tests/unit/test_one.py"


def test_a_command_covering_the_changed_test_file_is_accepted():
    result = _validate(
        _line(
            "one",
            write_scope=["src/one.py", "tests/unit/test_one.py"],
            acceptance=[
                "cmd: ruff check src/one.py",
                "cmd: pytest -q tests/unit/test_one.py",
            ],
        )
    )
    assert isinstance(result, PlanResult)
    assert not result.assignment.unassigned


def test_a_lint_command_alongside_a_test_command_is_accepted():
    result = _validate(
        _line(
            "one",
            acceptance=[
                "cmd: ruff check src/one.py",
                "cmd: pytest -q tests/unit/test_one.py",
            ],
        )
    )
    assert isinstance(result, PlanResult)


def test_a_command_naming_the_file_under_a_glob_scope_is_accepted():
    result = _validate(
        _line(
            "one",
            write_scope=["src/one.py", "tests/unit/test_*.py"],
            acceptance=["cmd: pytest -q tests/unit/test_one.py"],
        )
    )
    assert isinstance(result, PlanResult)


def test_a_non_python_runner_naming_the_changed_test_file_is_accepted():
    result = _validate(
        _line(
            "one",
            write_scope=["web/src/App.tsx", "web/src/App.test.tsx"],
            acceptance=["cmd: pnpm test web/src/App.test.tsx"],
        )
    )
    assert isinstance(result, PlanResult)


def test_a_line_that_changes_no_test_file_needs_any_test_command():
    result = _validate(_line("one", acceptance=["cmd: pytest --pyargs one"]))
    assert isinstance(result, PlanResult)


def test_a_docs_only_line_needs_no_test_command():
    result = _validate(
        _line(
            "one",
            write_scope=["docs/plan.md"],
            acceptance=["the guide renders"],
        )
    )
    assert isinstance(result, PlanResult)
    assert not result.assignment.unassigned


def test_a_test_directory_acceptance_keeps_its_own_reason():
    """N27 runs first: a directory run is the more specific reason."""
    result = _validate(
        _line(
            "one",
            write_scope=["src/one.py", "tests/unit/test_one.py"],
            acceptance=["cmd: pytest tests/unit"],
        )
    )
    assert result == PlanRejection(ACCEPTANCE_TEST_DIRECTORY, "one")


# -- a mode that runs no test -------------------------------------------------


def test_a_command_that_only_reports_about_the_runner_is_refused():
    """``pytest --version`` exits zero, so it would pass having run nothing."""
    result = _validate(
        _line(
            "one",
            write_scope=["src/one.py", "tests/unit/test_one.py"],
            acceptance=["cmd: pytest --version tests/unit/test_one.py"],
        )
    )
    assert result == PlanRejection(ACCEPTANCE_MISSING_TEST, "one")


def test_a_collect_only_command_does_not_cover_the_file_it_names():
    result = _validate(
        _line(
            "one",
            write_scope=["src/one.py", "tests/unit/test_one.py"],
            acceptance=["cmd: pytest --collect-only tests/unit/test_one.py"],
        )
    )
    # It runs no test at all, so the line is refused for running none.
    assert result == PlanRejection(ACCEPTANCE_MISSING_TEST, "one")


def test_a_reporting_mode_beside_a_real_run_is_accepted():
    result = _validate(
        _line(
            "one",
            write_scope=["src/one.py", "tests/unit/test_one.py"],
            acceptance=[
                "cmd: pytest --collect-only tests/unit/test_one.py",
                "cmd: pytest -q tests/unit/test_one.py",
            ],
        )
    )
    assert isinstance(result, PlanResult)


# -- the test files the task names -------------------------------------------


def test_a_test_file_the_task_names_has_to_be_covered():
    """The scope is the package directory; the task names the file in it."""
    result = _validate(
        _line(
            "one",
            task="Add tests/unit/pkg/test_one.py for the new helper.",
            write_scope=["src/one.py", "tests/unit/pkg"],
            acceptance=["cmd: pytest -q tests/unit/test_other.py"],
        )
    )
    assert result == PlanRejection(
        ACCEPTANCE_MISSING_TEST, "one:tests/unit/pkg/test_one.py"
    )


def test_a_command_naming_the_file_the_task_names_is_accepted():
    result = _validate(
        _line(
            "one",
            task="Add tests/unit/pkg/test_one.py for the new helper.",
            write_scope=["src/one.py", "tests/unit/pkg"],
            acceptance=["cmd: pytest -q tests/unit/pkg/test_one.py"],
        )
    )
    assert isinstance(result, PlanResult)


def test_a_directory_scope_names_no_test_file_of_its_own():
    """``tests/unit/pkg`` is where the file will live, not a test itself."""
    result = _validate(
        _line(
            "one",
            write_scope=["src/one.py", "tests/unit/pkg"],
            acceptance=["cmd: pytest -q tests/unit/pkg/test_one.py"],
        )
    )
    assert isinstance(result, PlanResult)


@pytest.mark.parametrize(
    "task",
    [
        "Cover tests/unit/pkg/test_one.py::test_new.",
        "Cover tests/unit/pkg/test_one.py:12.",
        "Cover `tests/unit/pkg/test_one.py`, then review it.",
        "Cover (tests/unit/pkg/test_one.py).",
    ],
)
def test_a_task_path_keeps_its_selector_and_punctuation_out_of_the_way(task):
    result = _validate(
        _line(
            "one",
            task=task,
            write_scope=["src/one.py", "tests/unit/pkg"],
            acceptance=["cmd: pytest -q tests/unit/pkg/test_one.py"],
        )
    )
    assert isinstance(result, PlanResult), task


def test_a_task_word_that_is_not_a_path_names_no_test_file():
    result = _validate(
        _line(
            "one",
            task="Make the test_one helper honest about what it asserts.",
            write_scope=["src/one.py"],
            acceptance=["cmd: pytest -q tests/unit/test_one.py"],
        )
    )
    assert isinstance(result, PlanResult)


def test_a_task_naming_a_document_is_not_a_test_file():
    result = _validate(
        _line(
            "one",
            task="Update docs/tests/guide.md and src/one.py.",
            write_scope=["docs/tests/guide.md", "src/one.py"],
            acceptance=["cmd: pytest -q tests/unit/test_one.py"],
        )
    )
    assert isinstance(result, PlanResult)


# -- a conftest is covered by the tests that load it -------------------------


def test_a_conftest_is_covered_by_a_test_under_its_own_directory():
    result = _validate(
        _line(
            "fixtures",
            write_scope=["tests/unit/conftest.py"],
            acceptance=[
                "cmd: pytest -q tests/unit/tools/auto_dispatch/test_planning_requires_tests.py"
            ],
        )
    )
    assert isinstance(result, PlanResult)


def test_a_conftest_is_not_covered_by_a_test_outside_its_directory():
    result = _validate(
        _line(
            "fixtures",
            write_scope=["tests/unit/conftest.py"],
            acceptance=["cmd: pytest -q tests/integration/test_y.py"],
        )
    )
    assert result == PlanRejection(
        ACCEPTANCE_MISSING_TEST, "fixtures:tests/unit/conftest.py"
    )


def test_the_repository_conftest_is_loaded_by_every_test():
    result = _validate(
        _line(
            "fixtures",
            write_scope=["conftest.py"],
            acceptance=["cmd: pytest -q tests/unit/test_one.py"],
        )
    )
    assert isinstance(result, PlanResult)


def test_a_helper_the_tests_load_is_not_a_test_file_of_its_own():
    result = _validate(
        _line(
            "one",
            write_scope=["src/one.py", "tests/unit/server/app/fixture.py"],
            acceptance=["cmd: pytest -q tests/unit/server/app/test_one.py"],
        )
    )
    assert isinstance(result, PlanResult)


# -- the corrected-planner retry --------------------------------------------


def _replies(*bodies: str):
    prompts: list[str] = []

    async def complete(prompt: str, system: str) -> str:
        prompts.append(prompt)
        return bodies[min(len(prompts) - 1, len(bodies) - 1)]

    return complete, prompts


def _repeating(body: str):
    prompts: list[str] = []

    async def complete(prompt: str, system: str) -> str:
        prompts.append(prompt)
        return body

    return complete, prompts


def _json(*lines: dict) -> str:
    return json.dumps(_payload(*lines))


@pytest.mark.asyncio
async def test_n73_a_plan_that_runs_no_test_is_accepted_and_recorded_not_refused():
    """N73 S4: the env switch is record-only; enforce no longer refuses the plan.

    The finding is still recorded (G2 re-runs the rule on the line and puts
    it in the review evidence); the planner is not asked again.
    """
    untested = _line("one", acceptance=["cmd: ruff check src/one.py"])
    complete, prompts = _replies(_json(untested))
    planner = LLMPlanner(complete, policy=POLICY, agents=_agents("kimi"))
    lines = await planner.plan("goal")
    assert [line.name for line in lines] == ["one"]
    assert len(prompts) == 1


@pytest.mark.asyncio
async def test_n73_the_rule_still_refuses_when_asked_explicitly():
    result = validate_plan(
        _payload(_line("one", acceptance=["cmd: ruff check src/one.py"])),
        policy=POLICY,
        agents=_agents("kimi"),
        require_tests_mode=ENFORCE,
    )
    assert result.code == "acceptance_missing_test:one"


async def _asleep(_seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_n73_an_untested_line_no_longer_parks_the_dispatch_at_planning(tmp_path):
    complete, _prompts = _repeating(
        _json(_line("one", acceptance=["cmd: ruff check src/one.py"]))
    )
    planner = LLMPlanner(complete, policy=POLICY, agents=_agents("kimi"))
    collaborators = Collaborators(
        planner=planner,
        selector=RoundRobinSelector(),
        launcher=RecordingLauncher(),
        monitor=ScriptedMonitor(),
        reviewer=AlwaysPassReviewer(),
        merger=NoopMerger(),
        gate=FileApprovalGate(tmp_path / "proposed"),
        outcomes=InMemoryOutcomeStore(),
        store=InMemoryDispatchStore(),
    )
    machine = DispatchMachine(collaborators, sleep=_asleep)
    dispatch = await collaborators.store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )
    parked = await machine.run(dispatch)
    assert parked.lines and not is_planner_failed(parked)
