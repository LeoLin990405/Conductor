"""The collaboration wire schemas are closed draft 2020-12 documents."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from src.collab import schemas

_EXPECTED = {
    "agent_ask",
    "agent_inbox",
    "agent_inbox_result",
    "empty_input",
    "envelope",
    "handoff_accept",
    "handoff_accept_result",
    "handoff_list",
    "handoff_list_result",
    "handoff_request",
    "handoff_request_result",
    "lease_input",
    "lease_state",
    "message_result",
    "review_list",
    "review_list_result",
    "review_request",
    "review_request_result",
    "review_submit",
    "review_submit_result",
    "task_claim",
    "task_claim_result",
    "task_create",
    "task_get",
    "task_get_result",
    "task_list",
    "task_list_result",
    "task_release",
    "task_report",
    "task_report_result",
    "task_view",
    "whoami_result",
}
_TICKET = "t:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_COMMIT = "a" * 40


def _closed_nodes(schema):
    for node in schemas.object_nodes(schema):
        assert node.get("additionalProperties") is False, node


def _refs(value):
    if isinstance(value, dict):
        if isinstance(value.get("$ref"), str):
            yield value["$ref"]
        for item in value.values():
            yield from _refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _refs(item)


def test_every_json_schema_is_present_and_closed():
    assert set(schemas.SCHEMAS) == _EXPECTED
    assert set(schemas.SCHEMA_FILES.values()) == {
        f"{name}.json" for name in _EXPECTED
    }
    schemas.assert_schemas_are_draft_2020_12()
    for name, schema in schemas.SCHEMAS.items():
        assert schema["$schema"] == schemas.DRAFT_2020_12, name
        assert schema["additionalProperties"] is False, name
        Draft202012Validator.check_schema(schema)
        _closed_nodes(schema)


def test_the_envelope_data_uses_closed_per_tool_schemas():
    envelope = schemas.SCHEMAS["envelope"]
    data = envelope["properties"]["data"]
    assert "anyOf" in data
    refs = set(_refs(data))
    # Every non-empty payload is checked by a result schema from this package;
    # the one literal object is the closed empty object used by refusals.
    assert refs
    assert refs <= {schema["$id"] for schema in schemas.SCHEMAS.values()}
    for ref in refs:
        name = ref.rsplit("/", 1)[-1].removesuffix(".json")
        assert name in schemas.SCHEMAS, ref
        assert schemas.SCHEMAS[name]["additionalProperties"] is False, ref


def test_an_envelope_data_payload_is_closed_at_every_level():
    good = {
        "ok": True,
        "code": "ok",
        "data": {
            "ref": _TICKET,
            "kind": "ticket",
            "title": "A ticket",
            "status": "queued",
            "claimable": True,
            "revision": 1,
        },
        "untrusted_fields": [],
        "redacted": 0,
    }
    assert schemas.is_valid("envelope", good)
    bad = dict(good, data={"unexpected": True})
    with pytest.raises(ValidationError):
        schemas.validate("envelope", bad)


def test_the_json_files_are_the_loaded_source_of_truth():
    for name, schema in schemas.SCHEMAS.items():
        path = schemas.schema_file(name)
        assert path.name == schemas.SCHEMA_FILES[name]
        assert json.loads(path.read_text(encoding="utf-8")) == schema


def test_unknown_properties_are_rejected_at_the_root():
    with pytest.raises(ValidationError):
        schemas.validate(
            "task_create",
            {
                "title": "A task",
                "body": "Do it",
                "repo": "owner/repo",
                "unexpected": True,
            },
        )


def test_unknown_properties_are_rejected_inside_a_nested_report():
    with pytest.raises(ValidationError):
        schemas.validate(
            "task_report",
            {
                "lease_id": "lease-1",
                "report": {
                    "status": "done",
                    "summary": "done",
                    "artifacts": {
                        "commits": [_COMMIT],
                        "files": ["src/x.py"],
                        "pr": None,
                        "unexpected": "nope",
                    },
                    "verification": [{"ran": "pytest", "result": "passed"}],
                    "next": "",
                    "handbacks": [],
                },
            },
        )


def test_valid_claim_and_report_match_the_closed_schemas():
    schemas.validate_tool_input("task_claim", {"ref": _TICKET, "revision": 3})
    schemas.validate_tool_input(
        "task_report",
        {
            "lease_id": "lease-1",
            "report": {
                "status": "done",
                "summary": "fixed it",
                "artifacts": {
                    "commits": [_COMMIT],
                    "files": ["src/x.py"],
                    "pr": None,
                },
                "verification": [{"ran": "pytest", "result": "passed"}],
                "next": "",
                "handbacks": [],
            },
        },
    )
    assert schemas.is_valid("task_view", {
        "ref": _TICKET,
        "kind": "ticket",
        "title": "A ticket",
        "status": "queued",
        "claimable": True,
        "revision": 1,
    })


def test_bounded_patterns_reject_an_invalid_commit_and_unknown_verdict_field():
    with pytest.raises(ValidationError):
        schemas.validate_tool_input(
            "review_request", {"lease_id": "lease-1", "commit": "NOT-A-SHA"}
        )
    with pytest.raises(ValidationError):
        schemas.validate_tool_input(
            "review_submit",
            {
                "review_id": "review-1",
                "verdict": {
                    "passed": True,
                    "blockers": [],
                    "verdict": "ok",
                    "extra": True,
                },
            },
        )


def test_tool_schema_tables_are_complete_and_unknown_tools_are_refused():
    assert set(schemas.TOOL_INPUT_SCHEMAS) == {
        "whoami",
        "task_list",
        "task_get",
        "task_create",
        "task_claim",
        "task_heartbeat",
        "task_release",
        "task_report",
        "handoff_request",
        "handoff_list",
        "handoff_accept",
        "agent_ask",
        "agent_inbox",
        "review_request",
        "review_list",
        "review_submit",
    }
    assert all(name in schemas.SCHEMAS for name in schemas.TOOL_INPUT_SCHEMAS.values())
    assert all(name in schemas.SCHEMAS for name in schemas.TOOL_OUTPUT_SCHEMAS.values())
    with pytest.raises(ValueError):
        schemas.validate_tool_input("not_a_tool", {})


def test_the_suite_runs_with_a_temporary_home(isolated_collab_home, tmp_path):
    assert Path.home() == isolated_collab_home
    assert tmp_path in Path.home().parents
