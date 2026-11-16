"""The review verdict schema, checked without repairing a mismatch.

A structured review reply is usable only when it matches this document: a
boolean ``passed``, ``blockers`` as a list of strings, and a string
``verdict``, with no other keys. That is the reply shape the review prompts
already ask for (``execution.py``) and the text parser reads, so a schema
reply and a text reply carry the same fields. Callers that get None fall
back to the reply text. Dropping a key, coercing a type, or turning a
non-string blocker into a string would hide that the reply did not match,
so this module never rewrites the value it was given.

Codex sends ``--output-schema`` as a strict JSON schema, so every object in
the file closes ``additionalProperties`` and requires all of its properties.

N59 added ``status`` (``pass|fail|inconclusive``) and ``needed_evidence``.
The schema file requires them, as strict structured output must, but a
reply in the pre-N59 three-key shape still validates: the N59 keys are
optional *for validation* (``N59_OPTIONAL_KEYS``). When present they are
checked like any other property; an unknown status is still a mismatch.
"""

from __future__ import annotations

import json
from pathlib import Path

# The file a structured-output CLI is pointed at. Validation reads this same
# document, so the check cannot drift from the schema on disk.
VERDICT_SCHEMA_PATH = Path(__file__).with_name("verdict.schema.json")

#: Keys a reviewer that predates N59 does not send. Absent is fine; wrong is not.
N59_OPTIONAL_KEYS = ("status", "needed_evidence")


def load_verdict_schema() -> dict[str, object]:
    """The verdict schema document. A non-object file is a packaging error."""
    loaded = json.loads(VERDICT_SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{VERDICT_SCHEMA_PATH.name} must contain a JSON object")
    return loaded


def validate_verdict(obj: object) -> dict[str, object] | None:
    """``obj`` when it matches the loaded verdict schema, otherwise None.

    The same object is returned. A mismatch is not rewritten into a valid one.
    """
    if not isinstance(obj, dict):
        return None
    if not _matches(_without_absent_n59_keys(load_verdict_schema(), obj), obj):
        return None
    return obj


def _without_absent_n59_keys(schema: dict[str, object], obj: dict) -> dict[str, object]:
    """The root schema with the N59 keys that ``obj`` leaves out no longer required."""
    required = schema.get("required")
    if not isinstance(required, list):
        return schema
    absent = {key for key in N59_OPTIONAL_KEYS if key not in obj}
    if not absent:
        return schema
    relaxed = dict(schema)
    relaxed["required"] = [name for name in required if name not in absent]
    return relaxed


def _matches(schema: object, value: object) -> bool:
    """Hand-rolled JSON Schema check for the keywords this verdict file uses.

    Unknown keywords and types fail closed. Nothing is coerced.
    """
    if not isinstance(schema, dict):
        return False
    kind = schema.get("type")
    if kind == "object":
        return _matches_object(schema, value)
    if kind == "array":
        return _matches_array(schema, value)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "string":
        if not isinstance(value, str):
            return False
        allowed = schema.get("enum")
        if allowed is None:
            return True
        return isinstance(allowed, list) and value in allowed
    return False


def _matches_object(schema: dict[str, object], value: object) -> bool:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        return False
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        return False
    if any(not isinstance(name, str) or name not in properties for name in required):
        return False
    if any(name not in value for name in required):
        return False
    additional = schema.get("additionalProperties", True)
    for key in value:
        if key in properties:
            if not _matches(properties[key], value[key]):
                return False
            continue
        # additionalProperties false rejects the key. A schema here checks the
        # extra value. Anything else fails closed, and the object is not trimmed.
        if additional is True:
            continue
        if additional is False or not _matches(additional, value[key]):
            return False
    return True


def _matches_array(schema: dict[str, object], value: object) -> bool:
    if not isinstance(value, list):
        return False
    if "items" not in schema:
        return True
    item_schema = schema["items"]
    return all(_matches(item_schema, item) for item in value)
