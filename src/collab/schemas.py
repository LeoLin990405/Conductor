"""Loading and validation for the JSON files in ``src/collab/schema``.

The JSON files are the source of truth: an MCP server or CLI written in another
runtime can read them without importing Python. This module only loads,
meta-validates and applies them, so there is no second copy to drift.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry, Resource

from src.collab.types import InvalidSchemaError

DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
DRAFT = DRAFT_2020_12
SCHEMA_DIR = Path(__file__).with_name("schema")

#: Tool name -> input schema. The empty schema is a real, closed object: a
#: caller cannot smuggle an unexpected field into a no-argument tool.
TOOL_INPUT_SCHEMAS: dict[str, str] = {
    "whoami": "empty_input",
    "task_list": "task_list",
    "task_get": "task_get",
    "task_create": "task_create",
    "task_claim": "task_claim",
    "task_heartbeat": "lease_input",
    "task_release": "task_release",
    "task_report": "task_report",
    "handoff_request": "handoff_request",
    "handoff_list": "handoff_list",
    "handoff_accept": "handoff_accept",
    "agent_ask": "agent_ask",
    "agent_inbox": "agent_inbox",
    "review_request": "review_request",
    "review_list": "review_list",
    "review_submit": "review_submit",
}

#: Tool name -> output schema, for MCP tool descriptors and route responses.
TOOL_OUTPUT_SCHEMAS: dict[str, str] = {
    "whoami": "whoami_result",
    "task_list": "task_list_result",
    "task_get": "task_get_result",
    "task_create": "task_view",
    "task_claim": "task_claim_result",
    "task_heartbeat": "lease_state",
    "task_release": "lease_state",
    "task_report": "task_report_result",
    "handoff_request": "handoff_request_result",
    "handoff_list": "handoff_list_result",
    "handoff_accept": "handoff_accept_result",
    "agent_ask": "message_result",
    "agent_inbox": "agent_inbox_result",
    "review_request": "review_request_result",
    "review_list": "review_list_result",
    "review_submit": "review_submit_result",
}


def object_nodes(schema: object) -> list[dict[str, Any]]:
    """Every object schema under ``schema``, including the root."""
    found: list[dict[str, Any]] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return found


def _load_schemas() -> dict[str, dict[str, Any]]:
    paths = sorted(SCHEMA_DIR.glob("*.json"))
    if not paths:
        raise SchemaError(f"no collaboration schemas under {SCHEMA_DIR}")
    loaded: dict[str, dict[str, Any]] = {}
    for path in paths:
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SchemaError(f"cannot read {path.name}: {exc}") from exc
        if not isinstance(schema, dict):
            raise SchemaError(f"{path.name} is not a JSON object")
        if schema.get("$schema") != DRAFT_2020_12:
            raise SchemaError(f"{path.name} is not draft 2020-12")
        if schema.get("additionalProperties") is not False:
            raise SchemaError(f"{path.name} is not a closed object")
        Draft202012Validator.check_schema(schema)
        loaded[path.stem] = schema
    return loaded


SCHEMAS: dict[str, dict[str, Any]] = _load_schemas()
SCHEMA_FILES: dict[str, str] = {name: f"{name}.json" for name in SCHEMAS}

#: ``$ref`` targets are absolute ``$id`` URLs, so one registry serves every
#: schema and the envelope can compose the closed per-tool result schemas
#: without copying them (and without a second source of truth to drift).
_REGISTRY = Registry().with_resources(
    (schema["$id"], Resource.from_contents(schema))
    for schema in SCHEMAS.values()
    if isinstance(schema.get("$id"), str)
)

_FORMAT_CHECKER = FormatChecker()
_VALIDATORS: dict[str, Draft202012Validator] = {
    name: Draft202012Validator(
        schema, registry=_REGISTRY, format_checker=_FORMAT_CHECKER
    )
    for name, schema in SCHEMAS.items()
}

#: Public per-schema constants (``TASK_CREATE_SCHEMA`` and friends) for
#: callers that want one document without a lookup.
for _schema_name, _schema_document in SCHEMAS.items():
    globals()[f"{_schema_name.upper()}_SCHEMA"] = _schema_document

#: Kept as a short name for callers that build their own mapping.
SCHEMA_BY_TOOL = TOOL_INPUT_SCHEMAS


def schema_for(name: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(name, dict):
        return name
    try:
        return SCHEMAS[str(name)]
    except KeyError as exc:
        raise InvalidSchemaError(f"unknown collaboration schema: {name}") from exc


def schema_file(name: str) -> Path:
    try:
        filename = SCHEMA_FILES[str(name)]
    except KeyError as exc:
        raise InvalidSchemaError(f"unknown collaboration schema: {name}") from exc
    return SCHEMA_DIR / filename


def validator_for(name: str | dict[str, Any]) -> Draft202012Validator:
    if isinstance(name, dict):
        return Draft202012Validator(
            name, registry=_REGISTRY, format_checker=_FORMAT_CHECKER
        )
    try:
        return _VALIDATORS[str(name)]
    except KeyError as exc:
        raise InvalidSchemaError(f"unknown collaboration schema: {name}") from exc


def validate(name: str | dict[str, Any], instance: Any) -> None:
    """Raise the first ``jsonschema`` error; never repair or coerce."""
    errors = iter_errors(name, instance)
    if errors:
        raise errors[0]


def iter_errors(name: str | dict[str, Any], instance: Any) -> list[ValidationError]:
    validator = validator_for(name)
    return sorted(
        validator.iter_errors(instance),
        key=lambda error: tuple(str(part) for part in error.path),
    )


def is_valid(name: str | dict[str, Any], instance: Any) -> bool:
    try:
        return validator_for(name).is_valid(instance)
    except Exception:
        return False


def validate_tool_input(tool: str, instance: Any) -> None:
    try:
        schema_name = TOOL_INPUT_SCHEMAS[str(tool)]
    except KeyError as exc:
        raise InvalidSchemaError(f"unknown collaboration tool: {tool}") from exc
    validate(schema_name, instance)


def validate_tool_output(tool: str, instance: Any) -> None:
    try:
        schema_name = TOOL_OUTPUT_SCHEMAS[str(tool)]
    except KeyError as exc:
        raise InvalidSchemaError(f"unknown collaboration tool: {tool}") from exc
    validate(schema_name, instance)


def check_schemas() -> None:
    """Meta-validate every schema; called by tests and startup diagnostics."""
    for name, schema in SCHEMAS.items():
        Draft202012Validator.check_schema(schema)
        if schema.get("$schema") != DRAFT_2020_12:
            raise SchemaError(f"{name} is not draft 2020-12")


def assert_schemas_are_draft_2020_12() -> None:
    """Raise ``SchemaError`` unless every schema is draft 2020-12 and closed.

    The envelope used to exempt ``data`` because it varies by tool. It now
    composes the closed ``*_result`` schemas through ``anyOf``/``$ref`` (plus a
    closed empty object for refusals), so no object node is left open: a tool's
    payload is checked by that tool's own schema, and an unknown key is refused
    at whichever level it appears.
    """
    for name, schema in SCHEMAS.items():
        Draft202012Validator.check_schema(schema)
        if schema.get("$schema") != DRAFT_2020_12:
            raise SchemaError(f"{name} is not draft 2020-12")
        for node in object_nodes(schema):
            if node.get("additionalProperties") is not False:
                raise SchemaError(f"{name} has an open object schema")


#: Aliases used by different front ends.
validate_schema = validate
validate_payload = validate_tool_input


__all__ = [
    "DRAFT",
    "DRAFT_2020_12",
    "SCHEMAS",
    "SCHEMA_BY_TOOL",
    "SCHEMA_DIR",
    "SCHEMA_FILES",
    "TOOL_INPUT_SCHEMAS",
    "TOOL_OUTPUT_SCHEMAS",
    "assert_schemas_are_draft_2020_12",
    "check_schemas",
    "is_valid",
    "iter_errors",
    "object_nodes",
    "schema_file",
    "schema_for",
    "validate",
    "validate_payload",
    "validate_schema",
    "validate_tool_input",
    "validate_tool_output",
    "validator_for",
]
