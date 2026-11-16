"""
Skills module — registry, middleware, and helpers for dynamic tool loading.

This module provides:
- SkillDefinition: Dataclass for defining loadable skills
- SKILL_REGISTRY: Registry of all available skills
- SkillsMiddleware: Unified middleware for skill discovery and tool gating
- Helper functions for skill management
"""

from ptc_agent.agent.middleware.skills.content import (
    SkillPrefixResult,
    SkillRequest,
    build_skill_content,
    compute_already_loaded,
    load_skill_content,
    resolve_skill_dir,
)
from src.server.services.skills_core.discovery import SkillMetadata
from src.server.services.skills_core.lock import (
    SkillLockEntry,
    SkillsLockFile,
)
from src.server.services.skills_core.registry import (
    SKILL_REGISTRY,
    SUBAGENT_WITHHELD_SKILLS,
    SkillDefinition,
    build_effective_skill_registry,
    get_command_to_skill_map,
    get_skill,
    get_skill_registry,
    list_skills,
    resolve_disabled_skills,
    skills_outside_sandbox,
)
from ptc_agent.agent.middleware.skills.middleware import (
    SkillsMiddleware,
)

__all__ = [
    "SkillDefinition",
    "SkillLockEntry",
    "SkillMetadata",
    "SkillsLockFile",
    "SKILL_REGISTRY",
    "SUBAGENT_WITHHELD_SKILLS",
    "SkillPrefixResult",
    "SkillRequest",
    "SkillsMiddleware",
    "build_effective_skill_registry",
    "build_skill_content",
    "compute_already_loaded",
    "get_command_to_skill_map",
    "get_skill",
    "get_skill_registry",
    "list_skills",
    "load_skill_content",
    "resolve_skill_dir",
    "resolve_disabled_skills",
    "skills_outside_sandbox",
]
