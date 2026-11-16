"""The local skill catalog and allowlisted agent CLI destinations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentAdapter:
    """Map a supported agent CLI to its conventional skill directory."""

    name: str
    label: str
    skill_root: str

    def destination(self, home: Path, skill_name: str) -> Path:
        return home / self.skill_root / skill_name


@dataclass(frozen=True)
class SkillRecord:
    """Metadata for one skill shipped with Conductor."""

    name: str
    description: str
    source_directory: str


AGENT_ADAPTERS: tuple[AgentAdapter, ...] = (
    AgentAdapter("claude", "Claude Code", ".claude/skills"),
    AgentAdapter("codex", "Codex", ".codex/skills"),
    AgentAdapter("cursor", "Cursor", ".cursor/skills"),
    AgentAdapter("opencode", "OpenCode", ".config/opencode/skills"),
    AgentAdapter("agents", "Agent Skills convention", ".agents/skills"),
)

SKILL_CATALOG: tuple[SkillRecord, ...] = (
    SkillRecord(
        "agent-browser",
        "Use Conductor's managed browser and site adapters for bounded page inspection.",
        "skills/agent-browser",
    ),
)


def get_skill(name: str) -> SkillRecord:
    for skill in SKILL_CATALOG:
        if skill.name == name:
            return skill
    raise ValueError(f"Unknown skill: {name}")


def get_adapters(names: tuple[str, ...] | None = None) -> tuple[AgentAdapter, ...]:
    if names is None:
        return AGENT_ADAPTERS
    by_name = {adapter.name: adapter for adapter in AGENT_ADAPTERS}
    unknown = tuple(name for name in names if name not in by_name)
    if unknown:
        raise ValueError(f"Unsupported agent adapter: {unknown[0]}")
    return tuple(by_name[name] for name in dict.fromkeys(names))


def repository_root() -> Path:
    """Return the checkout root for source installs; wheels use bundled data."""
    return Path(__file__).resolve().parents[3]
