import pytest

from src.server.contracts.memory_tool_policy import (
    MemoryToolPolicyError,
    denied_tools_as_json,
    normalize_denied_tools,
    normalize_memory_tool_policy_metadata,
    normalize_validity_window,
)


def test_normalizes_only_explicit_exact_tool_ids():
    assert denied_tools_as_json(
        [
            {"type": "tool", "name": "Bash"},
            {"type": "mcp", "server": "inventory", "tool": "delete_item"},
            {"type": "tool", "name": "Bash"},
        ]
    ) == [
        {"type": "tool", "name": "Bash"},
        {"type": "mcp", "server": "inventory", "tool": "delete_item"},
    ]


@pytest.mark.parametrize(
    "value",
    [
        "never delete files",
        [{"name": "Bash"}],
        [{"type": "tool", "name": "Bash", "extra": True}],
        [{"type": "tool", "name": "bash\n"}],
        [{"type": "mcp", "server": "tools.inventory", "tool": ""}],
    ],
)
def test_rejects_prose_aliases_and_malformed_ids(value):
    with pytest.raises(MemoryToolPolicyError):
        normalize_denied_tools(value)


def test_denial_list_is_bounded():
    with pytest.raises(MemoryToolPolicyError):
        normalize_denied_tools([{"type": "tool", "name": "Bash"}] * 65)


def test_approved_validity_window_is_parsed_and_bounded():
    assert normalize_validity_window("2026-01-01", "2027-01-01") == (
        "2026-01-01T00:00:00+00:00",
        "2027-01-01T00:00:00+00:00",
    )
    with pytest.raises(MemoryToolPolicyError):
        normalize_validity_window("2027-01-01", "2026-01-01")
    with pytest.raises(MemoryToolPolicyError):
        normalize_validity_window("9999-01-01", None)


def test_constraint_proposal_metadata_keeps_typed_ids_and_validity():
    assert normalize_memory_tool_policy_metadata(
        {
            "metadata": {
                "kind": "constraint",
                "denied_tools": [{"type": "tool", "name": "Bash"}],
                "valid_until": "2027-01-01",
            }
        }
    ) == {
        "kind": "constraint",
        "denied_tools": [{"type": "tool", "name": "Bash"}],
        "valid_until": "2027-01-01T00:00:00+00:00",
    }


def test_constraint_metadata_conflicts_or_denials_without_kind_fail_closed():
    with pytest.raises(MemoryToolPolicyError, match="conflicting kind"):
        normalize_memory_tool_policy_metadata(
            {
                "kind": "constraint",
                "metadata": {"kind": "preference"},
            }
        )
    with pytest.raises(MemoryToolPolicyError, match="kind=constraint"):
        normalize_memory_tool_policy_metadata(
            {"denied_tools": [{"type": "tool", "name": "Bash"}]}
        )
