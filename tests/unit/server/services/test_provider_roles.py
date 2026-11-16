import pytest

from src.utils.provider_roles import (
    PROVIDER_ROLES,
    ProviderRoleError,
    is_code_task,
    provider_task_eligible,
    require_provider_role,
    route_research_provider,
)


def test_manus_role_is_immutable_and_research_only():
    assert PROVIDER_ROLES["manus"] == "research"
    with pytest.raises(TypeError):
        PROVIDER_ROLES["manus"] = "native"


@pytest.mark.parametrize(
    "task",
    [
        "Write a Python script that downloads the report",
        "Run the test suite and explain the failures",
        "How should we implement this parser in the repository?",
        "Think through a refactor plan for the codebase",
        "Write Fibonacci in Python",
        "Create an app",
        "Code a calculator",
        "execute ls",
        "写一个 Python 脚本",
        "编写代码",
        "运行测试",
    ],
)
def test_manus_rejects_code_execution_and_deliberation(task):
    assert is_code_task(task)
    assert not provider_task_eligible("manus", task, role="research")
    with pytest.raises(ProviderRoleError):
        require_provider_role("manus", "research", task)
    assert route_research_provider(task, preferred_provider="manus") is None


def test_manus_accepts_non_coding_research():
    prompt = "Find recent primary sources on battery recycling policy"
    assert provider_task_eligible("manus", prompt, role="research")
    assert require_provider_role("manus", "research", prompt) == "research"


@pytest.mark.parametrize(
    "task",
    [
        "Research code quality metrics from published studies",
        "Compare Python package release dates from official docs",
        "Find papers about code review",
    ],
)
def test_research_topics_about_software_are_not_coding_tasks(task):
    assert not is_code_task(task)
    assert route_research_provider(task) == "manus"


def test_role_guard_rejects_manus_as_native_provider():
    with pytest.raises(ProviderRoleError):
        require_provider_role("manus", "native", "Research recent papers")


@pytest.mark.parametrize(
    "task_kind",
    ["coding", "code_writing", "execution", "deliberation_coding", "implementation", "unknown"],
)
def test_explicit_non_research_task_kinds_are_rejected(task_kind):
    assert not provider_task_eligible(
        "manus", "Research this subject", role="research", task_kind=task_kind
    )
    assert route_research_provider(
        "Research this subject", preferred_provider="manus", task_kind=task_kind
    ) is None
    with pytest.raises(ProviderRoleError):
        require_provider_role(
            "manus", "research", "Research this subject", task_kind=task_kind
        )


def test_explicit_research_task_kind_is_accepted():
    assert route_research_provider(
        "Find primary sources", preferred_provider="manus", task_kind="research"
    ) == "manus"
