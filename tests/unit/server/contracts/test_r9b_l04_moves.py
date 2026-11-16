"""R9b L04: the contracts and small modules live outside src/ptc_agent.

The old ptc_agent paths are re-export shims, so both spellings must name the
very same objects (an isinstance check or a frozenset identity must not split).
"""

import importlib

import pytest

MOVES = [
    ("ptc_agent.memory_policy_metadata", "src.server.contracts.memory_policy_metadata",
     ["MemoryPolicyMetadataError", "normalize_memory_policy_metadata", "memory_content_is_recallable"]),
    ("ptc_agent.memory_tool_policy", "src.server.contracts.memory_tool_policy",
     ["MemoryToolPolicySnapshot", "ToolIdentifier", "MEMORY_ENTRY_KINDS", "snapshot_from_authority_rows"]),
    ("ptc_agent.agent.roles", "src.server.contracts.agent_roles",
     ["AgentRole", "ALL_ROLES", "LEGACY_ROLES", "normalize_role"]),
    ("ptc_agent.agent.prompts.guidance", "src.llms.guidance",
     ["VALID_GUIDANCE", "DEFAULT_GUIDANCE", "PromptGuidance", "resolve_prompt_guidance",
      "guidance_template_vars"]),
]


@pytest.mark.parametrize(("old", "new", "names"), MOVES)
def test_the_shim_reexports_the_moved_objects(old, new, names):
    old_mod = importlib.import_module(old)
    new_mod = importlib.import_module(new)
    for name in names:
        assert getattr(old_mod, name) is getattr(new_mod, name), name


@pytest.mark.parametrize(
    ("new", "symbol"),
    [
        ("src.server.contracts.memory_policy_metadata", "normalize_memory_policy_metadata"),
        ("src.server.contracts.memory_tool_policy", "MemoryToolPolicySnapshot"),
        ("src.server.contracts.agent_roles", "normalize_role"),
        ("src.llms.guidance", "resolve_prompt_guidance"),
    ],
)
def test_the_real_definition_lives_at_the_new_path(new, symbol):
    assert getattr(importlib.import_module(new), symbol).__module__ == new


def test_prompts_package_still_exports_guidance():
    from ptc_agent.agent.prompts import resolve_prompt_guidance

    from src.llms.guidance import resolve_prompt_guidance as moved

    assert resolve_prompt_guidance is moved


def test_agent_limits_live_in_contracts_and_shim_reexports() -> None:
    from ptc_agent.agent import limits as old
    from src.server.contracts import agent_limits as new

    assert old.UNBOUNDED_RECURSION_LIMIT == new.UNBOUNDED_RECURSION_LIMIT
    assert old.is_refusal is new.is_refusal
    assert old.pending_read is new.pending_read
    assert old.is_pending_read is new.is_pending_read
    assert old.REFUSAL_KEY == new.REFUSAL_KEY
