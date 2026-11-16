"""Redaction is the only thing standing between an event payload and a permanent
row. These cases pin the blunt policy: secret-shaped keys disappear wholesale,
long strings are truncated with a kept length, nesting is capped, NUL is
stripped, and unknown objects degrade to strings instead of failing later.
"""

from __future__ import annotations

from src.server.services.refactor.redaction import (
    MAX_STRING_LENGTH,
    REDACTED,
    TRUNCATED,
    redact_event_payload,
)


def test_secret_shaped_keys_are_replaced_wholesale():
    payload = {
        "api_key": "sk-real",
        "Authorization": "Bearer x",
        "refresh_token": "rt",
        "user_password": "hunter2",
        "cookie": "a=b",
        "safe": "kept",
    }
    assert redact_event_payload(payload) == {
        "api_key": REDACTED,
        "Authorization": REDACTED,
        "refresh_token": REDACTED,
        "user_password": REDACTED,
        "cookie": REDACTED,
        "safe": "kept",
    }


def test_nested_secrets_in_lists_and_dicts_are_redacted():
    payload = {
        "steps": [
            {"tool": "http", "headers": {"authorization": "Bearer x"}},
            {"tool": "bash", "args": {"api_key": "k"}},
        ],
        "count": 2,
    }
    redacted = redact_event_payload(payload)
    assert redacted["steps"][0]["headers"]["authorization"] == REDACTED
    assert redacted["steps"][1]["args"]["api_key"] == REDACTED
    assert redacted["count"] == 2


def test_long_strings_are_truncated_and_length_is_kept():
    text = "x" * (MAX_STRING_LENGTH + 100)
    redacted = redact_event_payload({"output": text})
    assert redacted["output"].startswith("x" * MAX_STRING_LENGTH)
    assert redacted["output"].endswith(f"{TRUNCATED}<len={len(text)}>")


def test_short_strings_survive():
    assert redact_event_payload({"summary": "done"})["summary"] == "done"


def test_depth_is_capped():
    payload: dict = {}
    cursor = payload
    for _ in range(10):
        child: dict = {}
        cursor["next"] = child
        cursor = child
    redacted = redact_event_payload(payload)
    # Walk the redacted copy: it must not be deeper than the cap + 1 leaf.
    depth = 0
    node = redacted
    while isinstance(node, dict) and "next" in node:
        node = node["next"]
        depth += 1
    assert depth <= 5
    assert node == TRUNCATED


def test_nul_bytes_are_stripped():
    assert redact_event_payload("a\x00b") == "ab"
    assert redact_event_payload({"k\x00": "v\x00"}) == {"k": "v"}


def test_unknown_objects_become_strings():
    class Marker:
        def __str__(self) -> str:
            return "marker"

    assert redact_event_payload({"obj": Marker()}) == {"obj": "marker"}


def test_scalars_are_untouched():
    for value in (None, True, False, 0, 1, -2.5):
        assert redact_event_payload(value) == value
