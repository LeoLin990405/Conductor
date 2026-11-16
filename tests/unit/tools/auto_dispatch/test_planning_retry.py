"""Planner replies are not deterministic: robust JSON extraction and retries.

Seen live (self-iteration round 1): a brace quoted in prose before the JSON
broke the first-to-last-brace slice, a long task string came back with a broken
escape, and once the reply parsed to an empty lines list. Each parked the
dispatch as planner_failed.
"""

from __future__ import annotations

import json

import pytest

from src.tools.auto_dispatch.planning import (
    PLANNER_MAX_ATTEMPTS,
    PLANNER_RETRY_NOTE,
    LLMPlanner,
    _extract_json,
)
from src.tools.auto_dispatch.protocols import DispatchError

pytestmark = pytest.mark.asyncio


def _plan(lines: list[dict] | None = None) -> str:
    if lines is None:
        lines = [
            {
                "name": "one",
                "task": "Fix one thing.",
                "depends_on": [],
                "write_scope": ["src/one.py"],
                "declared_paths": ["src/one.py"],
                "acceptance": ["cmd: pytest -q tests/unit/one.py"],
                "risk_bearing": False,
            }
        ]
    return json.dumps(
        {
            "schema_version": 1,
            "spec": {"title": "Do it", "body": "## Goal\nShip it.\n\n## Acceptance\n- Tests pass."},
            "lines": lines,
        }
    )


def _scripted(*replies: str):
    prompts: list[str] = []

    async def complete(prompt: str, system: str) -> str:
        prompts.append(prompt)
        return replies[min(len(prompts) - 1, len(replies) - 1)]

    return complete, prompts


def test_extract_skips_braces_in_prose_before_the_json():
    raw = "Use a quantifier like [0-9a-f]{32} here.\n" + _plan() + "\nDone {ok}."
    assert _extract_json(raw)["lines"][0]["name"] == "one"


def test_extract_prefers_the_object_with_lines():
    raw = json.dumps({"note": "first"}) + "\n" + _plan()
    assert "lines" in _extract_json(raw)


def test_extract_reads_a_fenced_block():
    assert "lines" in _extract_json("```json\n" + _plan() + "\n```")


def test_extract_without_a_lines_object_is_a_parse_failure():
    with pytest.raises(DispatchError, match="invalid JSON"):
        _extract_json('text {"a": 1} more {"b": 2}')


def test_a_broken_escape_never_falls_back_to_a_nested_object():
    raw = '{"schema_version": 1, "spec": {"title": "t", "body": "b"}, "lines": [{"name": "x", "task": "bad \\q escape"}]}'
    with pytest.raises(DispatchError, match="invalid JSON"):
        _extract_json(raw)


def test_the_outer_plan_wins_over_an_example_in_the_prose():
    example = '{"lines": []}'
    raw = "For example " + example + " is empty. The plan:\n" + _plan()
    assert len(_extract_json(raw)["lines"]) == 1


async def test_a_broken_escape_reply_is_retried():
    bad = '{"schema_version": 1, "spec": {"title": "t", "body": "b"}, "lines": [{"name": "x", "task": "bad \\q"}]}'
    complete, prompts = _scripted(bad, _plan())
    lines = await LLMPlanner(complete).plan("Fix one thing.")
    assert len(lines) == 1 and len(prompts) == 2


def test_extract_without_json_keeps_the_error():
    with pytest.raises(DispatchError, match="no JSON object"):
        _extract_json("no json here")
    with pytest.raises(DispatchError, match="invalid JSON"):
        _extract_json('{"broken": "unterminated}')


async def test_an_unparseable_reply_is_retried_with_a_correction():
    complete, prompts = _scripted('{"schema_version": 1, "spec": "broken', _plan())
    lines = await LLMPlanner(complete).plan("Fix one thing.")
    assert [line.id for line in lines] and len(prompts) == 2
    assert PLANNER_RETRY_NOTE in prompts[1] and PLANNER_RETRY_NOTE not in prompts[0]


async def test_an_empty_lines_reply_is_retried():
    complete, prompts = _scripted(_plan(lines=[]), _plan())
    lines = await LLMPlanner(complete).plan("Fix one thing.")
    assert len(lines) == 1 and len(prompts) == 2


async def test_retries_stop_after_the_limit():
    complete, prompts = _scripted("not json at all")
    with pytest.raises(DispatchError, match="no JSON object"):
        await LLMPlanner(complete).plan("Fix one thing.")
    assert len(prompts) == PLANNER_MAX_ATTEMPTS


async def test_a_content_rejection_is_not_retried():
    too_many = [
        {
            "name": f"l{i}",
            "task": "x",
            "depends_on": [],
            "write_scope": [f"src/l{i}.py"],
            "declared_paths": [f"src/l{i}.py"],
            "acceptance": ["cmd: pytest -q"],
            "risk_bearing": False,
        }
        for i in range(20)
    ]
    complete, prompts = _scripted(_plan(lines=too_many))
    with pytest.raises(DispatchError, match="too_many_lines"):
        await LLMPlanner(complete).plan("Fix everything.")
    assert len(prompts) == 1
