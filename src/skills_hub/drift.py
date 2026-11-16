"""Read-only skills drift report, built on :func:`status.status_report`.

Per agent directory:

* ``missing``: skills the hub's manifest assigns to this agent that the
  directory does not have;
* ``different``: hub installs whose link target or copied bytes no longer
  match the central skill;
* ``extra``: skills in the directory the hub did not install (manual);
* ``not_installed``: central skills this agent does not have at all
  (informational: the catalog does not say every agent needs every skill);
* ``fragile``: entries that are symlinks into a temporary or ``draft-*``
  workspace directory (reported by name only, the target is not followed);
* ``legacy``: old directories for this agent that still exist.

Nothing is written. Symlinks are read with ``readlink`` only.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .adapters import AgentDirs, UnknownAgentError
from .core import discover_skills
from .status import status_report

_DRAFT_SEGMENT = re.compile(r"^draft-[A-Za-z0-9._-]+$")
_FRAGILE_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")


def drift_report(
    home: Path,
    repo: Path,
    agent_dirs: Sequence[tuple[str, Path]],
) -> dict[str, Any]:
    status = status_report(home, repo, agent_dirs)
    try:
        central = sorted(entry.name for entry in discover_skills(repo))
    except Exception:  # noqa: BLE001 - an unreadable repo just means no catalog
        central = []
    builtin = AgentDirs(home)
    agents: dict[str, Any] = {}
    for agent, directory in agent_dirs:
        info = status["agents"].get(agent, {"dir": str(directory), "skills": []})
        rows = info.get("skills", [])
        present = {row["name"] for row in rows if row.get("how") != "missing"}
        row: dict[str, Any] = {
            "dir": info.get("dir", str(directory)),
            "missing": sorted(r["name"] for r in rows if r.get("how") == "missing"),
            "different": sorted(
                r["name"]
                for r in rows
                if r.get("drift") and r.get("how") in {"link", "copy"}
            ),
            "extra": sorted(r["name"] for r in rows if r.get("how") == "manual"),
            "not_installed": [name for name in central if name not in present],
            "fragile": _fragile_links(Path(info.get("dir", str(directory)))),
            "legacy": _legacy_dirs(builtin, agent),
        }
        if info.get("error"):
            row["error"] = info["error"]
        agents[agent] = row
    return {
        "repo": str(repo),
        "central_skills": len(central),
        "agents": agents,
        "lines": _lines(agents),
    }


def _fragile_links(directory: Path) -> list[str]:
    found: list[str] = []
    try:
        children = sorted(directory.iterdir(), key=lambda p: p.name)
    except OSError:
        return found
    for child in children:
        if not child.is_symlink():
            continue
        try:
            target = os.readlink(child)
        except OSError:
            continue
        if target.startswith(_FRAGILE_PREFIXES) or any(
            _DRAFT_SEGMENT.match(part) for part in target.split("/")
        ):
            found.append(child.name)
    return found


def _legacy_dirs(builtin: AgentDirs, agent: str) -> list[str]:
    try:
        entry = builtin.get(agent)
    except UnknownAgentError:
        return []
    out: list[str] = []
    for path in entry.legacy:
        try:
            path.lstat()
        except OSError:
            continue
        out.append(str(path))
    return out


def _lines(agents: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for agent, row in agents.items():
        parts = [
            f"{key}={len(row[key])}"
            for key in ("missing", "different", "extra", "fragile", "legacy")
            if row[key]
        ]
        if row.get("error"):
            parts.append("error")
        lines.append(f"{agent}: " + (", ".join(parts) if parts else "in sync"))
    return lines
