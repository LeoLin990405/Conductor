"""Planner: JSON parsing, line shape and the failures that stop a dispatch."""

from __future__ import annotations

import pytest

from src.server.services.autonomy.roles import RolePolicy
from src.tools.auto_dispatch.planning import LLMPlanner
from src.tools.auto_dispatch.protocols import DispatchError


def _planner(reply: str) -> LLMPlanner:
    async def complete(prompt: str, system: str) -> str:
        return reply

    return LLMPlanner(complete)


async def test_plan_parses_lines_with_ids_and_tasks() -> None:
    planner = _planner(
        _plan(
            _line("survey", "Read src/core/fs.py"),
            _line("implement", "Add the async handler"),
        )
    )
    lines = await planner.plan("make fs async")
    assert [line.id for line in lines] == ["line-0", "line-1"]
    assert [line.name for line in lines] == ["survey", "implement"]
    assert lines[0].task == "Read src/core/fs.py"
    # plan() writes the role plan's choice back, so the state machine's _assign
    # skips the line instead of sending it through the selector again.
    assert lines[0].assigned_model in RolePolicy.default().developer_pool
    assert lines[1].assigned_model in RolePolicy.default().developer_pool
    assert lines[0].status.value == "PENDING"


async def test_plan_strips_markdown_fences() -> None:
    planner = _planner('```json\n' + _plan(_line("a", "do a")) + '\n```')
    lines = await planner.plan("goal")
    assert [line.name for line in lines] == ["a"]


@pytest.mark.parametrize(
    "reply",
    [
        "I cannot help with that",
        "{not json",
        '{"lines": []}',
        '{"lines": [{"name": "a"}]}',
        '["line"]',
    ],
)
async def test_plan_failures_raise_dispatch_error(reply: str) -> None:
    planner = _planner(reply)
    with pytest.raises(DispatchError):
        await planner.plan("goal")


async def test_plan_rejects_more_lines_than_the_cap() -> None:
    # A plan past the cap is a plan failure, not a silent truncation: cutting a
    # line would drop work the planner asked for.
    reply = _plan(*[_line(f"n{i}", f"task {i}") for i in range(12)])
    with pytest.raises(DispatchError, match="too_many_lines:12"):
        await _planner(reply).plan("goal")


async def test_plan_requires_a_goal() -> None:
    with pytest.raises(DispatchError):
        await _planner('{"lines": [{"name": "a", "task": "b"}]}').plan("   ")

pytestmark = pytest.mark.asyncio



def _plan(*lines: dict[str, object], **top: object) -> str:
    """A planner reply in the v1 schema the prompt asks for."""
    import json

    payload: dict[str, object] = {
        "schema_version": 1,
        "spec": {
            "title": "Do the work",
            "body": "## Goal\nShip it.\n\n## Acceptance\n- The focused tests pass.",
        },
        "lines": list(lines),
    }
    payload.update(top)
    return json.dumps(payload)


def _line(name: str, task: str, **over: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "name": name,
        "task": task,
        "depends_on": [],
        "write_scope": [f"src/{name}.py"],
        "declared_paths": [f"src/{name}.py"],
        "acceptance": [f"cmd: pytest -q tests/unit/{name}.py"],
        "risk_bearing": False,
    }
    entry.update(over)
    return entry


def _one_line(name: str, task: str) -> str:
    return _plan(_line(name, task))


async def test_a_task_at_the_byte_limit_is_accepted_and_one_byte_over_is_a_plan_failure() -> None:
    from src.tools.auto_dispatch.planning import MAX_LINE_TASK_BYTES

    lines = await _planner(_one_line("a", "x" * MAX_LINE_TASK_BYTES)).plan("goal")
    assert len(lines[0].task) == MAX_LINE_TASK_BYTES
    with pytest.raises(DispatchError, match="line_too_large:task"):
        await _planner(_one_line("a", "x" * (MAX_LINE_TASK_BYTES + 1))).plan("goal")


async def test_the_limits_are_in_bytes_not_characters() -> None:
    # 2,001 four-byte characters are 8,004 bytes: over the limit though far fewer
    # than 8,000 characters. A character count would let this through.
    from src.tools.auto_dispatch.planning import MAX_LINE_TASK_BYTES

    task = "\U0001f600" * (MAX_LINE_TASK_BYTES // 4 + 1)
    assert len(task) < MAX_LINE_TASK_BYTES < len(task.encode())
    with pytest.raises(DispatchError, match="line_too_large:task"):
        await _planner(_one_line("a", task)).plan("goal")
    fits = "\U0001f600" * (MAX_LINE_TASK_BYTES // 4)
    assert (await _planner(_one_line("a", fits)).plan("goal"))[0].task == fits


async def test_a_name_over_its_limit_is_a_plan_failure() -> None:
    from src.tools.auto_dispatch.planning import MAX_LINE_NAME_BYTES

    assert await _planner(_one_line("n" * MAX_LINE_NAME_BYTES, "do it")).plan("goal")
    with pytest.raises(DispatchError, match="line_too_large:name"):
        await _planner(_one_line("n" * (MAX_LINE_NAME_BYTES + 1), "do it")).plan("goal")


async def test_a_task_no_process_argument_can_carry_is_a_plan_failure() -> None:
    # json.loads turns \ud800 into a lone surrogate, which cannot be encoded.
    reply = _plan(_line("a", "do \ud800 it"))
    with pytest.raises(DispatchError, match="invalid_text:task") as caught:
        await _planner(reply).plan("goal")
    assert "ud800" not in str(caught.value)


async def test_a_plan_failure_for_size_does_not_repeat_the_task() -> None:
    secret = "SECRET-TASK-TEXT-" + "x" * 9_000
    with pytest.raises(DispatchError) as caught:
        await _planner(_one_line("a", secret)).plan("goal")
    assert "SECRET" not in str(caught.value)


async def test_a_task_or_name_with_a_nul_byte_is_a_plan_failure() -> None:
    for reply in (
        _plan(_line("a", "do a\u0000b")),
        _plan(_line("a\u0000b", "do it")),
    ):
        with pytest.raises(DispatchError, match="invalid_text") as caught:
            await _planner(reply).plan("goal")
        assert "u0000" not in str(caught.value)
