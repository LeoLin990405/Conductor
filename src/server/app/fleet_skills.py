"""Read-only fleet skills inventory for the agent-fleet dashboard.

One endpoint, GET only, behind the same authenticated-identity dependency as
the other dashboard routes: per-agent skill listings (what each fleet CLI has
installed, which entries Conductor manages, which symlinks are broken) plus
the central repo contents. The handler never writes: installs and
uninstalls belong to the CLI (``conductor skills``), which is also where
snapshots and the tracked manifest live. The directories read here are the
same ones the CLI writes, so the dashboard and the CLI cannot disagree about
state.

The response is deliberately less detailed than the CLI's own listing: no
per-skill absolute paths, and no import sources. An import source can be a
git URL with credentials embedded, and every authenticated user on a shared
host can call this endpoint — provenance belongs to the operator's terminal,
not the wire.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from src.skills_hub.config import SkillsConfigError, load_config
from src.skills_hub.core import SkillEntry, SkillsError, discover_skills
from src.skills_hub.manager import list_installed
from src.skills_hub.presets import list_presets, list_projects
from src.server.utils.api import CurrentUserId, handle_api_exceptions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/fleet-skills", tags=["Fleet Skills"])


def _public_skill(entry: SkillEntry) -> dict[str, Any]:
    """The dashboard-facing view of one repo skill.

    Strips ``path`` (an absolute path on the server's filesystem) and
    ``source``/``source_ref`` (an import source may embed a token in its
    URL): the panel needs the name, the description, and where the skill is
    installed — nothing that names the machine or the origin's credentials.
    """
    return {
        "name": entry.name,
        "description": entry.description,
        "installed": list(entry.installed),
    }


def _inventory(agents: list[str] | None) -> dict[str, Any]:
    try:
        config = load_config()
    except SkillsConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    known = config.agents
    if agents:
        unknown = [a for a in agents if a not in known]
        if unknown:
            raise HTTPException(
                status_code=404,
                detail=f"unknown agents requested; known: {', '.join(known)}",
            )
    selected = agents or known
    agent_dirs = [(agent, config.agent_dir(agent)) for agent in selected]
    try:
        repo_skills = [_public_skill(entry) for entry in discover_skills(config.repo)]
        installed = list_installed(config.repo, agent_dirs)
    except SkillsError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "repo": str(config.repo),
        "skills": repo_skills,
        "agents": installed,
        "presets": [p.to_dict() for p in list_presets(config.repo)],
        "projects": list_projects(config.repo),
    }


@router.get("")
@handle_api_exceptions("fleet skills inventory", logger)
async def fleet_skills_inventory(
    _user_id: CurrentUserId,
    agent: list[str] | None = Query(default=None, description="limit to these agents"),
) -> dict[str, Any]:
    """Per-agent skill inventory plus the central repo contents (read-only).

    Requires the same authenticated identity as every other dashboard route.
    The response names the central repo and the per-agent directories so the
    dashboard can show where each CLI reads from; per-skill absolute paths
    and import sources stay server-side.
    """
    return _inventory(agent)
