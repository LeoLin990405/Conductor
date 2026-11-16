"""The review verdict schema is closed, and a mismatch is never repaired."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.tools.auto_dispatch import review_schema
from src.tools.auto_dispatch.review_schema import (
    VERDICT_SCHEMA_PATH,
    load_verdict_schema,
    validate_verdict,
)

_SCHEMA_KEYWORDS = {
    "$schema",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
}


def _valid(**overrides: object) -> dict[str, object]:
    verdict: dict[str, object] = {
        "passed": False,
        "blockers": ["src/app.py:4: missing test"],
        "verdict": "one failing assertion",
    }
    verdict.update(overrides)
    return verdict


def _keywords(schema: object) -> set[str]:
    """Schema keywords only. Property names under ``properties`` are not keywords."""
    found: set[str] = set()
    if isinstance(schema, dict):
        found.update(str(key) for key in schema)
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                for sub in value.values():
                    found |= _keywords(sub)
            else:
                found |= _keywords(value)
    elif isinstance(schema, list):
        for item in schema:
            found |= _keywords(item)
    return found


def test_schema_path_points_at_the_verdict_document() -> None:
    assert VERDICT_SCHEMA_PATH == Path(review_schema.__file__).with_name(
        "verdict.schema.json"
    )
    assert VERDICT_SCHEMA_PATH.is_file()


def test_loaded_schema_is_a_closed_verdict_object() -> None:
    schema = load_verdict_schema()
    assert schema == json.loads(VERDICT_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "passed",
        "blockers",
        "verdict",
        "status",
        "needed_evidence",
    ]
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert properties["passed"] == {"type": "boolean"}
    assert properties["verdict"] == {"type": "string"}
    blockers = properties["blockers"]
    assert isinstance(blockers, dict)
    assert blockers["type"] == "array"
    assert blockers["items"] == {"type": "string"}
    assert properties["status"] == {
        "type": "string",
        "enum": ["pass", "fail", "inconclusive"],
    }
    assert properties["needed_evidence"] == {"type": "array", "items": {"type": "string"}}
    assert _keywords(schema) <= _SCHEMA_KEYWORDS


def _objects(schema: object) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            found.append(schema)
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                for sub in value.values():
                    found.extend(_objects(sub))
            elif isinstance(value, (dict, list)):
                found.extend(_objects(value))
    elif isinstance(schema, list):
        for item in schema:
            found.extend(_objects(item))
    return found


def test_schema_is_valid_for_strict_structured_output() -> None:
    """Codex sends the file as a strict schema: every object is closed and
    requires all of its properties, or the API rejects the whole review."""
    objects = _objects(load_verdict_schema())
    assert objects
    for node in objects:
        assert node.get("additionalProperties") is False, node
        properties = node.get("properties")
        assert isinstance(properties, dict) and properties, node
        assert sorted(node.get("required", [])) == sorted(properties), node


def test_schema_matches_the_review_prompt_reply_shape() -> None:
    """The review prompts ask for ``passed`` / ``blockers`` / ``verdict``."""
    from src.tools.auto_dispatch import execution

    source = Path(execution.__file__).read_text(encoding="utf-8")
    assert '"blockers": ["what must change"], "verdict": "one line"' in source
    reply = {"passed": False, "blockers": ["what must change"], "verdict": "one line"}
    assert validate_verdict(reply) is reply


def test_a_matching_verdict_is_returned_unchanged() -> None:
    verdict = _valid()
    assert validate_verdict(verdict) is verdict


def test_an_empty_blocker_list_and_empty_verdict_match() -> None:
    verdict = _valid(passed=True, blockers=[], verdict="")
    assert validate_verdict(verdict) is verdict


def test_an_empty_blocker_string_matches() -> None:
    verdict = _valid(blockers=[""])
    assert validate_verdict(verdict) is verdict


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        "passed",
        1,
        True,
        {"passed": True, "blockers": [], "verdict": "ok", "summary": "pass"},
        {"passed": True, "blockers": []},
        {"blockers": [], "verdict": "ok"},
        {"passed": True, "verdict": "ok"},
        {"passed": True, "blockers": [], "summary": "ok"},
        {"passed": "true", "blockers": [], "verdict": "ok"},
        {"passed": 1, "blockers": [], "verdict": "ok"},
        {"passed": 0, "blockers": [], "verdict": "ok"},
        {"passed": None, "blockers": [], "verdict": "ok"},
        {"passed": False, "blockers": [{"detail": "missing test"}], "verdict": "no"},
        {"passed": False, "blockers": None, "verdict": "no"},
        {"passed": False, "blockers": {}, "verdict": "no"},
        {"passed": False, "blockers": ("missing",), "verdict": "no"},
        {"passed": False, "blockers": [None], "verdict": "no"},
        {"passed": False, "blockers": [[]], "verdict": "no"},
        {"passed": False, "blockers": [1], "verdict": "no"},
        {"passed": False, "blockers": [{}], "verdict": "no"},
        {"passed": True, "blockers": [], "verdict": None},
        {"passed": True, "blockers": [], "verdict": 1},
        {"passed": True, "blockers": [], "verdict": ["text"]},
        {"passed": True, "blockers": [], "verdict": b"ok"},
    ],
)
def test_a_mismatch_is_none(value: object) -> None:
    assert validate_verdict(value) is None


def test_a_mismatch_is_not_rewritten() -> None:
    # An object blocker plus an extra key. Keeping it intact is the point: a
    # repair would look like a real structured verdict.
    original = {
        "passed": True,
        "blockers": [{"detail": "missing test"}],
        "verdict": "no",
        "summary": "pass",
    }
    snapshot = json.loads(json.dumps(original))
    assert validate_verdict(original) is None
    assert original == snapshot


def test_a_non_string_key_is_not_an_object_and_is_left_in_place() -> None:
    original: dict[object, object] = {
        "passed": True,
        "blockers": [],
        "verdict": "ok",
        1: "x",
    }
    snapshot = dict(original)
    assert validate_verdict(original) is None
    assert original == snapshot


def test_validation_reads_the_loaded_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """Changing the file's item type changes what matches; opening the root accepts extras."""
    closed = load_verdict_schema()
    as_objects = json.loads(json.dumps(closed))
    as_objects["properties"]["blockers"]["items"] = {"type": "object"}
    opened = json.loads(json.dumps(closed))
    del opened["additionalProperties"]

    monkeypatch.setattr(review_schema, "load_verdict_schema", lambda: as_objects)
    strings = {"passed": True, "blockers": ["missing test"], "verdict": "ok"}
    objects = {
        "passed": False,
        "blockers": [{"detail": "missing test"}],
        "verdict": "no",
    }
    assert validate_verdict(objects) is objects
    assert validate_verdict(strings) is None

    monkeypatch.setattr(review_schema, "load_verdict_schema", lambda: opened)
    extra = {"passed": True, "blockers": [], "verdict": "ok", "summary": "pass"}
    assert validate_verdict(extra) is extra


def test_the_checker_does_not_delegate_to_a_schema_library() -> None:
    source = Path(review_schema.__file__).read_text(encoding="utf-8")
    assert "jsonschema" not in source
    assert "json_repair" not in source
