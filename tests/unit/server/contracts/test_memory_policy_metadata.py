from __future__ import annotations

import pytest

from src.server.contracts.memory_policy_metadata import (
    MAX_POLICY_TEXT_CHARS,
    memory_content_is_recallable,
    normalize_memory_policy_metadata,
)


def test_legacy_metadata_defaults_to_private():
    assert normalize_memory_policy_metadata(None) == {"sensitivity": "private"}


def test_legacy_index_policy_reader_fails_closed_for_malformed_frontmatter():
    assert memory_content_is_recallable("legacy detail without frontmatter")
    assert not memory_content_is_recallable("---\nsensitivity: secret\n---\nbody")
    assert not memory_content_is_recallable("---\nsensitivity: [broken\n---\nbody")


def test_legacy_index_policy_reader_excludes_nonactive_lifecycle():
    assert not memory_content_is_recallable("---\nlifecycle: pending\n---\nbody")
    assert not memory_content_is_recallable("---\nlifecycle: wrong\n---\nbody")
    # A complete but empty frontmatter block is valid metadata (defaults), not
    # a malformed/secret document; both the immediate and blank-line forms are
    # accepted and the body is kept.
    assert memory_content_is_recallable("---\n---\nbody")
    assert memory_content_is_recallable("---\n\n---\nbody")
    assert memory_content_is_recallable("---\n---\n")
    # A delimiter that appears inside a line does not close the metadata block,
    # so a following sensitivity line still applies (fail closed).
    assert not memory_content_is_recallable(
        "---\ndescription: Draft---\nsensitivity: secret\n---\nbody"
    )


def test_normalizes_policy_text_and_closed_quote_pointer():
    metadata = normalize_memory_policy_metadata(
        {
            "sensitivity": "PUBLIC",
            "applies_when": "  before   sending   email ",
            "exceptions": "unless   explicitly   requested",
            "evidence_quote_ref": {
                "message_id": " msg-1 ",
                "start": 12,
                "end": 34,
                "unit": "unicode_codepoint",
            },
        }
    )
    assert metadata == {
        "sensitivity": "public",
        "applies_when": "before sending email",
        "exceptions": "unless explicitly requested",
        "evidence_quote_ref": {
            "message_id": "msg-1",
            "start": 12,
            "end": 34,
            "unit": "unicode_codepoint",
        },
    }


@pytest.mark.parametrize("value", ["secretly", "", None, True])
def test_invalid_sensitivity_fails_closed(value):
    assert normalize_memory_policy_metadata({"sensitivity": value})["sensitivity"] == "secret"


def test_unbounded_condition_fails_closed():
    normalized = normalize_memory_policy_metadata(
        {"applies_when": "x" * (MAX_POLICY_TEXT_CHARS + 1)}
    )
    assert normalized == {"sensitivity": "secret"}


@pytest.mark.parametrize(
    "quote_ref",
    [
        {"message_id": "m", "start": True, "end": 2, "unit": "unicode_codepoint"},
        {"message_id": "m", "start": 4, "end": 4, "unit": "unicode_codepoint"},
        {"message_id": "m", "start": 0, "end": 1, "unit": "utf8_byte"},
        {
            "message_id": "m",
            "start": 0,
            "end": 1,
            "unit": "unicode_codepoint",
            "extra": "closed shape",
        },
    ],
)
def test_malformed_quote_pointer_is_dropped(quote_ref):
    assert normalize_memory_policy_metadata({"evidence_quote_ref": quote_ref}) == {
        "sensitivity": "private"
    }
