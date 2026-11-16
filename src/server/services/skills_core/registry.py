"""
Skill registry for dynamic tool loading.

This module defines the registry of available skills. A skill loads when the
agent reads its SKILL.md or a turn requests it; any tools it gates are
pre-registered but hidden until then.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Callable, Literal

from src.config.features import is_feature_enabled_system
from src.config.settings import get_workflow_orchestration_config


@dataclass
class SkillDefinition:
    """Definition of a loadable skill.

    Attributes:
        name: Unique skill identifier
        description: Human-readable description of what the skill does
        tool_names: Names of the tools this skill hides until it loads. The
            agent build registers each tool (RunWorkflow is a per-thread
            factory tool that can't be instantiated at import time); the skill
            only controls its visibility.
        switched_tools: Names of tools the agent binds without loading this
            skill but drops when the skill is switched off. Never hidden
            until the skill loads; listed with the skill so the Plugins page
            shows what its switch removes.
        skill_md_path: Where the agent reaches SKILL.md, not where the repo
            keeps it. A sandbox-relative suffix, matched against the reads
            the agent makes under ``.agents/skills/``, which the sync
            flattens every source into. A shipped skill's files live in the
            bundle that declares it; that path never appears here.
        hidden: Activated only programmatically (via additionalContext), so
            it stays out of listings, the manifest and the sandbox.
        system_gate: Deployment kill switch for skills owned by a config section
            rather than the feature catalog. False drops the skill everywhere.
        source_dir: Absolute host directory containing ``<name>/SKILL.md`` for
            user-tier skills. It lets the loader read the body without any
            ``skill_dirs`` search. None for platform skills.
        origin: Whose content this is. "user" entries get trust framing in the
            manifest and the user-tier wire shape in the API.
        requires: Names of registry skills this one reads at runtime. A
            required skill survives a disable of its own for as long as one
            skill declaring it is not itself disabled (see
            ``resolve_disabled_skills``).
    """

    name: str
    description: str
    tool_names: tuple[str, ...] = ()
    switched_tools: tuple[str, ...] = ()
    skill_md_path: str | None = None
    hidden: bool = False
    command: str | None = None
    # Feature-flag key (src/config/features.py) owning this skill; the skill
    # drops out of every accessor while the feature's system default is off.
    # Per-user resolution happens at agent build (SkillsMiddleware injection).
    feature: str | None = None
    system_gate: Callable[[], bool] | None = None
    source_dir: str | None = None
    origin: Literal["platform", "user"] = "platform"
    requires: tuple[str, ...] = ()

    def listed_tool_names(self) -> list[str]:
        """Every tool this skill's switch governs, for listings."""
        return [*self.tool_names, *self.switched_tools]


def _run_workflow_enabled() -> bool:
    return get_workflow_orchestration_config().enabled


def _is_enabled(
    skill: SkillDefinition, feature_resolver: Callable[[str], bool] | None = None
) -> bool:
    """Availability gate: skills whose deployment switch or owning feature is
    off drop out of every accessor (listings, lookups, sandbox sync).

    ``feature_resolver`` defaults to the system gate, the no-user-context
    default these accessors run under. The agent build injects a per-user
    resolver (via ``get_skill_registry``) so a user's opt-in/out is honored
    when skills are assembled for that build. ``system_gate`` is a deployment
    kill switch, so no resolver overrides it.
    """
    if skill.system_gate is not None and not skill.system_gate():
        return False
    if skill.feature is None:
        return True
    resolve = feature_resolver or is_feature_enabled_system
    return resolve(skill.feature)


# Skills no subagent gets. A skill whose tools only the main agent binds is
# listed here, so a subagent's manifest, its preloads and the names a subagent
# setting may prefer all leave it out. Empty today: every remaining skill is
# one a subagent may load.
SUBAGENT_WITHHELD_SKILLS: frozenset[str] = frozenset()

# Registry of all available skills
# Skills are pre-registered at agent creation but tools are hidden until loaded
SKILL_REGISTRY: dict[str, SkillDefinition] = {
    "research-conventions": SkillDefinition(
        name="research-conventions",
        description=(
            "The evidence, judgement and intake rules every research "
            "deliverable follows. Read before the first deliverable of a "
            "research task; when a number has no source; when two sources "
            "disagree; when a figure may be stale; when deciding what to ask "
            "the user; before any conclusion or recommendation."
        ),
        skill_md_path="skills/research-conventions/SKILL.md",
    ),
    "run-workflow": SkillDefinition(
        name="run-workflow",
        # Keep in sync with the `description:` in
        # plugins/conductor_service/skills/run-workflow/SKILL.md
        # frontmatter (locked by a unit test). RunWorkflow itself is a per-thread
        # factory tool registered in agent.py, so it's gated by name here.
        description=(
            "Orchestrate parallel subagent pipelines from a JavaScript workflow "
            "script. Fan out work across many items (records, documents, findings) "
            "then synthesize, or run a saved workflow by name. "
            "Unlocks the RunWorkflow tool."
        ),
        tool_names=("RunWorkflow",),
        skill_md_path="skills/run-workflow/SKILL.md",
        system_gate=_run_workflow_enabled,
    ),
    "conductor-doc": SkillDefinition(
        name="conductor-doc",
        # Keep in sync with the `description:` in
        # plugins/conductor_service/skills/conductor-doc/SKILL.md
        # frontmatter (locked by a unit test).
        description=(
            "How the platform under you works: the computer and its "
            "workspaces, files and what survives a restart, conversation "
            "transcripts, saved tool results, memory, chat apps, and adding "
            "skills, MCP servers, connectors or plugins. Read it when the "
            "user points back at an earlier conversation, when something is "
            "missing or behaves unexpectedly, before adding any of those, "
            "and when the user asks how the platform works and you don't "
            "know."
        ),
        # No `command`: the prompt points into its reference files, and a
        # user has no reason to invoke it.
        skill_md_path="skills/conductor-doc/SKILL.md",
    ),
    "pdf": SkillDefinition(
        name="pdf",
        description="Read, fill and build PDFs: inspect structure and fonts, extract text and tables with pdfplumber and poppler, fill and flatten AcroForms with pypdf, create with reportlab, merge, split and encrypt with qpdf, and verify by rendering the page and looking at it",
        skill_md_path="skills/pdf/SKILL.md",
    ),
    "docx": SkillDefinition(
        name="docx",
        description="Word documents a human will review and edit: build with python-docx, edit an existing file in place with tracked changes and comment threads, render, validate",
        skill_md_path="skills/docx/SKILL.md",
    ),
    "pptx": SkillDefinition(
        name="pptx",
        description="PowerPoint decks built with pptxgenjs and native charts, existing decks edited in place with python-pptx, then rendered to images and audited for overflow, overlap, bounds and typography before delivery",
        skill_md_path="skills/pptx/SKILL.md",
    ),
    "xlsx": SkillDefinition(
        name="xlsx",
        description="Excel workbooks with live formulas: build or edit .xlsx models with openpyxl, recalculate with IronCalc or LibreOffice, audit conventions, profile messy uploads, render for review",
        skill_md_path="skills/xlsx/SKILL.md",
    ),
    "self-improve": SkillDefinition(
        name="self-improve",
        description="Report issues and propose fixes to improve your own capabilities when you encounter errors or limitations",
        skill_md_path="skills/self-improve/SKILL.md",
        command="report-issue",
    ),
    "inline-widget": SkillDefinition(
        name="inline-widget",
        description="Inline HTML widgets: charts, dashboards, data tables rendered directly in the chat via ShowWidget",
        skill_md_path="skills/inline-widget/SKILL.md",
    ),
    "interactive-dashboard": SkillDefinition(
        name="interactive-dashboard",
        description="Interactive web dashboards: live tables, filters and drill-downs, served via preview URL",
        skill_md_path="skills/interactive-dashboard/SKILL.md",
        command="dashboard",
    ),
    "html-report": SkillDefinition(
        name="html-report",
        description="Self-contained styled HTML reports written to the task directory: PDF-exportable research documents with inline data, charts, and theme-aware CSS",
        skill_md_path="skills/html-report/SKILL.md",
        command="html-report",
    ),
    "ui-design": SkillDefinition(
        name="ui-design",
        description="Design-quality reference for research visual output: typography, color, composition, and avoiding generic AI aesthetics",
        skill_md_path="skills/ui-design/SKILL.md",
    ),
}

# Built-ins only the retired Flash agent loaded. Threads it started still name
# them, and no build has a SKILL.md for them.
RETIRED_SKILLS = frozenset({"secretary", "automation"})


def build_user_skill_definitions(
    specs: Sequence[Any],
    *,
    source_dir: str | None,
    workspace_source_dir: str | None = None,
) -> dict[str, SkillDefinition]:
    """Build registry entries for a user's uploaded skills.

    ``specs`` is duck-typed (needs ``.name``/``.description``, optional
    ``.command``/``.workspace_scoped``) so the server's spec dataclass never
    has to be imported here. Every entry is docs-only (no tools) and carries
    ``skill_md_path="skills/<name>/SKILL.md"`` so auto-load on ``Read`` of
    ``.agents/skills/<name>/SKILL.md`` matches with no extra code.

    The two tiers read from different host dirs (see
    ``load_user_skill_bundle``); a spec whose tier has no dir is skipped
    rather than pointed at the other one's.
    """
    out: dict[str, SkillDefinition] = {}
    for spec in specs:
        root = (
            workspace_source_dir
            if getattr(spec, "workspace_scoped", False)
            else source_dir
        )
        if root is None:
            continue
        out[spec.name] = SkillDefinition(
            name=spec.name,
            description=spec.description,
            skill_md_path=f"skills/{spec.name}/SKILL.md",
            command=getattr(spec, "command", None) or spec.name,
            origin="user",
            source_dir=root,
        )
    return out


def resolve_disabled_skills(disabled: Iterable[str]) -> frozenset[str]:
    """Drop from *disabled* every skill another, undisabled skill declares
    in ``requires``.

    A shared layer like ``research-conventions`` carries no command of its
    own and is reached only through the skills that mandate reading it, so
    switching it off while one of them is on would point that skill's first
    mandated read at a file the sandbox sync never uploads. The dependent's
    own switch is the one that takes the layer away.
    """
    blocked = frozenset(disabled)
    if not blocked:
        return blocked
    return blocked - {
        required
        for name, skill in SKILL_REGISTRY.items()
        if name not in blocked
        for required in skill.requires
    }


def build_effective_skill_registry(
    *,
    feature_resolver: Callable[[str], bool] | None = None,
    disabled_skills: Iterable[str] = (),
    user_skills: Sequence[Any] = (),
    user_skill_dir: str | None = None,
    workspace_skill_dir: str | None = None,
) -> dict[str, SkillDefinition]:
    """Assemble the per-build registry every agent surface consumes.

    Feature gating, minus the user's builtin disables (as
    ``resolve_disabled_skills`` reads them), plus the user's uploaded
    skills. The one assembly path shared by the agent build, the subagent
    compiler and the server's per-turn checks, so a gate applied in one
    can't be missed in another.
    """
    registry = get_skill_registry(feature_resolver=feature_resolver)
    for name in resolve_disabled_skills(disabled_skills):
        registry.pop(name, None)
    registry.update(
        build_user_skill_definitions(
            user_skills,
            source_dir=user_skill_dir,
            workspace_source_dir=workspace_skill_dir,
        )
    )
    return registry


def get_skill_registry(
    *,
    feature_resolver: Callable[[str], bool] | None = None,
) -> dict[str, SkillDefinition]:
    """Get the enabled skills.

    Args:
        feature_resolver: Optional per-user feature gate. Defaults to the
              system gate; the agent build passes the build's own
              ``feature_enabled`` so opt-in/out drops the skill for that user.

    Returns:
        Dict of skill name to SkillDefinition for enabled skills
    """
    return {
        name: skill
        for name, skill in SKILL_REGISTRY.items()
        if _is_enabled(skill, feature_resolver)
    }


def skills_outside_sandbox() -> frozenset[str]:
    """Built-in skills the sandbox does not carry, even when their directory
    is on disk.

    One gated off (``_is_enabled``) has nothing to offer there, and a hidden
    one reaches the agent only as a body injected from the host. The sandbox
    sync leaves these out and a summary does not send the agent back to them.
    A name the registry does not know (a user's skill) is never in it.
    """
    return frozenset(
        name
        for name, skill in SKILL_REGISTRY.items()
        if skill.hidden or not _is_enabled(skill)
    )


def get_skill(skill_name: str) -> SkillDefinition | None:
    """Get an enabled skill definition by name, or None."""
    skill = SKILL_REGISTRY.get(skill_name)
    if skill is None or not _is_enabled(skill):
        return None
    return skill


def get_command_to_skill_map() -> dict[str, str]:
    """Map slash command names to skill names.

    Returns a dict where keys are command strings (e.g. "3-statement-model")
    and values are skill names (e.g. "3-statements"). Only includes enabled
    skills that have a non-None command field.
    """
    return {
        skill.command: name
        for name, skill in SKILL_REGISTRY.items()
        if skill.command and _is_enabled(skill)
    }


def list_skills() -> list[dict[str, Any]]:
    """List available skills with their metadata.

    Hidden skills are excluded from listings (they can only be activated
    programmatically via additionalContext).

    Returns:
        List of skill info dicts with name, description, and tool count
    """
    return [
        {
            "name": skill.name,
            "description": skill.description,
            "tool_count": len(skill.listed_tool_names()),
            "tools": skill.listed_tool_names(),
            "command": skill.command,
        }
        for skill in SKILL_REGISTRY.values()
        if _is_enabled(skill) and not skill.hidden
    ]
