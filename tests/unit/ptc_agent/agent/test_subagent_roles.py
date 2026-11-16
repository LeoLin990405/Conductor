"""Which roles get a subagent is the definition's to say, and the registry applies it.

Subagents are generalist-only now that the Chief of Staff/Home surface is retired.
"""

from ptc_agent.agent.subagent_registry import BUILTIN_SUBAGENTS, SubagentRegistry
from src.config.runtime.agent import SubagentConfig

ALL = list(BUILTIN_SUBAGENTS)


def _roster(registry: SubagentRegistry, role: str | None) -> list[str]:
    return [defn.name for defn in registry.get_enabled(ALL, role=role)]


def test_the_generalist_roster_keeps_it():
    assert _roster(SubagentRegistry(), "generalist") == ALL


def test_a_lookup_with_no_role_keeps_every_enabled_subagent():
    assert _roster(SubagentRegistry(), None) == ALL


def test_a_yaml_override_keeps_the_builtins_roles():
    """The YAML cannot say which roles get a subagent, so reshaping a
    custom subagent keeps it generalist-owned."""
    registry = SubagentRegistry(
        user_definitions={"research-helper": SubagentConfig(description="mine")}
    )
    assert registry.get("research-helper").description == "mine"
    assert "research-helper" not in _roster(registry, "chief_of_staff")


def test_a_new_yaml_subagent_serves_the_generalist():
    registry = SubagentRegistry(
        user_definitions={"ops": SubagentConfig(description="ops helper")}
    )
    assert [d.name for d in registry.get_enabled(["ops"], role="generalist")] == ["ops"]
