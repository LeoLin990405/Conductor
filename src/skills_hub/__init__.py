"""Unified skill management for the Conductor agent fleet.

One central repo (default ``~/.conductor/skills``), imports from git/local/zip,
per-agent install adapters, presets, per-project/per-line skill sets with
roles, snapshots with rollback, and the ``conductor skills`` CLI subcommands
(:mod:`src.conductor_cli.skills_cli`).
"""

from .adapters import AgentDirs, LinkMode, UnknownAgentError, normalize_link_mode
from .config import SkillsConfig, load_config
from .core import (
    SkillEntry,
    SkillsError,
    discover_skills,
    get_skill,
)
from .presets import Preset, SkillSet
from .adopt import apply_adopt, plan_adopt
from .status import status_report
from .updates import backup_repo, check_updates, update_skill

__all__ = [
    "AgentDirs",
    "LinkMode",
    "Preset",
    "SkillEntry",
    "SkillSet",
    "SkillsConfig",
    "SkillsError",
    "UnknownAgentError",
    "discover_skills",
    "get_skill",
    "load_config",
    "normalize_link_mode",
    "plan_adopt",
    "apply_adopt",
    "status_report",
    "check_updates",
    "update_skill",
    "backup_repo",
]
