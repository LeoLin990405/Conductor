"""The subagent proposal tool is a no-op channel; its vocabulary mirrors the
server contract without the agent layer importing server code."""

from __future__ import annotations

from src.server.contracts.refactor import MEMORY_PROPOSAL_TYPES
from ptc_agent.agent.tools.memory_proposal import PROPOSAL_TYPES, propose_memory


def test_vocabulary_matches_the_server_contract():
    assert PROPOSAL_TYPES == MEMORY_PROPOSAL_TYPES


def test_tool_is_named_and_does_not_write():
    assert propose_memory.name == "ProposeMemory"
    result = propose_memory.invoke({"memory_type": "feedback", "text": "cite sources"})
    assert "recorded" in result
    # Unknown types do not fail the tool; the server drops them at extraction.
    assert "user" in propose_memory.invoke({"memory_type": "weird", "text": "x"})


def test_tool_exposes_bounded_policy_metadata_arguments():
    fields = propose_memory.args_schema.model_fields
    assert {
        "sensitivity",
        "applies_when",
        "exceptions",
        "evidence_quote_ref",
        "kind",
        "denied_tools",
        "valid_from",
        "valid_until",
    } <= set(fields)
    result = propose_memory.invoke(
        {
            "memory_type": "user",
            "text": "Keep local context private.",
            "sensitivity": "secret",
            "applies_when": "inside this account",
            "exceptions": "never share externally",
            "evidence_quote_ref": {
                "message_id": "m1",
                "start": 0,
                "end": 8,
                "unit": "unicode_codepoint",
            },
        }
    )
    assert "reviewed" in result

    constraint = propose_memory.invoke(
        {
            "memory_type": "user",
            "text": "Avoid shell execution.",
            "kind": "constraint",
            "denied_tools": [{"type": "tool", "name": "Bash"}],
        }
    )
    assert "reviewed" in constraint
