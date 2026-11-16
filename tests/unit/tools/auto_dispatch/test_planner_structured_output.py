"""The Claude Code planner asks the CLI to enforce the plan's JSON shape.

Long plans often came back with a broken escape, and the only object that then
decoded was a tiny example, so the plan "had no lines" (self-iteration round 1).
--json-schema makes the CLI return valid JSON of the plan's shape.
"""

from __future__ import annotations

import json

from src.tools.auto_dispatch.claude_planner import ClaudeCodeComplete, parse_result_envelope
from src.tools.auto_dispatch.planner_prompt import PLAN_JSON_SCHEMA
from src.tools.auto_dispatch.planning import validate_plan


def test_argv_carries_the_schema_only_when_set():
    with_schema = ClaudeCodeComplete(cwd="/tmp", json_schema=PLAN_JSON_SCHEMA).argv("sys")
    index = with_schema.index("--json-schema")
    assert json.loads(with_schema[index + 1]) == PLAN_JSON_SCHEMA
    assert "--json-schema" not in ClaudeCodeComplete(cwd="/tmp").argv("sys")


def test_structured_output_wins_over_result_text():
    plan = {"schema_version": 1, "spec": {"title": "t", "body": "b"}, "lines": []}
    envelope = {"type": "result", "subtype": "success", "result": "prose {", "structured_output": plan}
    assert json.loads(parse_result_envelope(json.dumps(envelope))) == plan


def test_without_structured_output_the_result_text_is_used():
    envelope = {"type": "result", "subtype": "success", "result": '{"a": 1}'}
    assert parse_result_envelope(json.dumps(envelope)) == '{"a": 1}'


def test_schema_fields_match_what_validate_plan_requires():
    line = PLAN_JSON_SCHEMA["properties"]["lines"]["items"]
    assert set(line["required"]) == {
        "name", "task", "depends_on", "write_scope", "declared_paths", "acceptance",
        "risk_bearing",
    }
    # estimated_size is the one optional field: it is a known property the
    # validator accepts, but a plan is not rejected for leaving it out.
    assert set(line["properties"]) == set(line["required"]) | {"estimated_size"}
    assert set(PLAN_JSON_SCHEMA["required"]) == {"schema_version", "spec", "lines"}


def test_a_schema_shaped_plan_validates():
    payload = {
        "schema_version": 1,
        "spec": {"title": "Do it", "body": "## Goal\nShip.\n\n## Acceptance\n- Tests pass."},
        "lines": [
            {
                "name": "one",
                "task": "Fix src/one.py.",
                "depends_on": [],
                "write_scope": ["src/one.py"],
                "declared_paths": ["src/one.py"],
                "acceptance": ["cmd: pytest -q tests/unit/one.py"],
                "risk_bearing": False,
            }
        ],
    }
    outcome = validate_plan(payload, max_lines=8)
    assert hasattr(outcome, "lines") and len(outcome.lines) == 1
