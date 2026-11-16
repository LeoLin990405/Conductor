"""Named skill bundles (presets) and per-project/per-line skill sets.

A *preset* is a named list of skill names, stored in the repo as
``presets/<name>.json``. Presets reference skills that already exist in the
central repo — creating one does not fetch anything.

A *project skill set* scopes that idea to one working copy: stored under the
repo as ``projects/<project>.json`` with per-line (branch-line) overrides and
an optional role filter (``plan`` / ``dev`` / ``review``). A skill may carry
``roles`` in its manifest record; a set with a role only exposes skills that
list that role, or that list none (unrestricted skills apply everywhere).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import (
    SkillsError,
    atomic_write_json,
    discover_skills,
    load_manifest,
    require_valid_name,
)

PRESET_DIRNAME = "presets"
PROJECT_DIRNAME = "projects"
ROLES = ("plan", "dev", "review")

MAX_BUNDLE_NAME = 64
BUNDLE_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _valid_bundle_name(name: str) -> bool:
    return bool(name) and len(name) <= MAX_BUNDLE_NAME and bool(BUNDLE_RE.match(name))


@dataclass(frozen=True)
class Preset:
    name: str
    skills: tuple[str, ...]
    description: str = ""
    # Optional MCP server ids this bundle expects. Empty when the file has no
    # ``mcp`` key, so presets written before the field still load.
    mcp: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": self.name,
            "skills": list(self.skills),
            "description": self.description,
        }
        if self.mcp:
            body["mcp"] = list(self.mcp)
        return body


def presets_dir(repo: Path) -> Path:
    return repo / PRESET_DIRNAME


def projects_dir(repo: Path) -> Path:
    return repo / PROJECT_DIRNAME


def _require_bundle_name(name: str) -> str:
    if not isinstance(name, str) or not _valid_bundle_name(name):
        raise SkillsError(
            f"invalid preset/project name {name!r}: expected lowercase words "
            f"joined by '-' (max {MAX_BUNDLE_NAME} chars)"
        )
    return name


def _read_object(path: Path) -> dict[str, Any] | None:
    import json

    try:
        with path.open("r", encoding="utf-8") as stream:
            body = json.load(stream)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise SkillsError(f"{path}: not valid JSON ({exc.msg})") from exc
    except OSError as exc:
        raise SkillsError(f"{path}: {exc.strerror or exc}") from exc
    if not isinstance(body, dict):
        raise SkillsError(f"{path}: expected a JSON object")
    return body


def _write_object(path: Path, body: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, body)


def _mcp_from(body: dict[str, Any]) -> tuple[str, ...]:
    raw = body.get("mcp", ())
    if not raw:
        return ()
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str))


def list_presets(repo: Path) -> list[Preset]:
    directory = presets_dir(repo)
    if not directory.is_dir():
        return []
    out: list[Preset] = []
    for child in sorted(directory.iterdir(), key=lambda p: p.name):
        if not child.name.endswith(".json"):
            continue
        name = child.name[: -len(".json")]
        if not _valid_bundle_name(name):
            continue
        body = _read_object(child) or {}
        skills = body.get("skills", [])
        out.append(
            Preset(
                name=name,
                skills=tuple(s for s in skills if isinstance(s, str)),
                description=body.get("description", "") if isinstance(body.get("description"), str) else "",
                mcp=_mcp_from(body),
            )
        )
    return out


def get_preset(repo: Path, name: str) -> Preset | None:
    _require_bundle_name(name)
    body = _read_object(presets_dir(repo) / f"{name}.json")
    if body is None:
        return None
    skills = body.get("skills", [])
    return Preset(
        name=name,
        skills=tuple(s for s in skills if isinstance(s, str)),
        description=body.get("description", "") if isinstance(body.get("description"), str) else "",
        mcp=_mcp_from(body),
    )


def save_preset(
    repo: Path,
    name: str,
    skills: list[str],
    *,
    description: str = "",
    mcp: list[str] | tuple[str, ...] | None = None,
) -> Preset:
    _require_bundle_name(name)
    cleaned: list[str] = []
    for skill in skills:
        require_valid_name(skill)
        if skill not in cleaned:
            cleaned.append(skill)
    if not cleaned:
        raise SkillsError("a preset needs at least one skill")
    available = {e.name for e in discover_skills(repo)}
    missing = [s for s in cleaned if s not in available]
    if missing:
        raise SkillsError(
            "unknown skills: " + ", ".join(missing) + "; import them into the central repo first"
        )
    mcp_clean: list[str] = []
    for server in mcp or ():
        if not isinstance(server, str):
            raise SkillsError(f"mcp entry must be a string, got {server!r}")
        if server not in mcp_clean:
            mcp_clean.append(server)
    preset = Preset(
        name=name,
        skills=tuple(cleaned),
        description=description,
        mcp=tuple(mcp_clean),
    )
    body: dict[str, Any] = {"skills": cleaned, "description": description}
    if mcp_clean:
        body["mcp"] = mcp_clean
    _write_object(presets_dir(repo) / f"{name}.json", body)
    return preset


def delete_preset(repo: Path, name: str) -> bool:
    _require_bundle_name(name)
    path = presets_dir(repo) / f"{name}.json"
    if not path.exists():
        return False
    path.unlink()
    return True


# --- per-project / per-line sets -------------------------------------------


@dataclass(frozen=True)
class SkillSet:
    project: str
    line: str
    skills: tuple[str, ...]
    role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "line": self.line,
            "skills": list(self.skills),
            "role": self.role,
        }


def _project_file(repo: Path, project: str) -> Path:
    _require_bundle_name(project)
    return projects_dir(repo) / f"{project}.json"


def set_project_skills(
    repo: Path,
    project: str,
    skills: list[str],
    *,
    line: str = "default",
    role: str | None = None,
) -> SkillSet:
    """Record the skill list for ``project`` (optionally per line and role)."""
    if role is not None and role not in ROLES:
        raise SkillsError(f"unknown role {role!r}; expected one of {', '.join(ROLES)}")
    if line != "default" and not _valid_bundle_name(line):
        raise SkillsError(
            f"invalid line name {line!r}: expected lowercase words joined by '-'"
        )
    cleaned: list[str] = []
    for skill in skills:
        require_valid_name(skill)
        if skill not in cleaned:
            cleaned.append(skill)
    available = {e.name for e in discover_skills(repo)}
    missing = [s for s in cleaned if s not in available]
    if missing:
        raise SkillsError(
            "unknown skills: " + ", ".join(missing) + "; import them first"
        )
    path = _project_file(repo, project)
    body = _read_object(path) or {"lines": {}}
    lines = body.setdefault("lines", {})
    if not isinstance(lines, dict):
        raise SkillsError(f"{path}: 'lines' is malformed")
    record: dict[str, Any] = {"skills": cleaned}
    if role:
        record["role"] = role
    lines[line] = record
    _write_object(path, body)
    return SkillSet(project=project, line=line, skills=tuple(cleaned), role=role)


def get_project_skills(repo: Path, project: str, *, line: str = "default") -> SkillSet | None:
    body = _read_object(_project_file(repo, project))
    if body is None:
        return None
    lines = body.get("lines", {})
    if not isinstance(lines, dict):
        return None
    record = lines.get(line) or lines.get("default") or {}
    if not isinstance(record, dict):
        return None
    skills = record.get("skills", [])
    role = record.get("role")
    return SkillSet(
        project=project,
        line=line,
        skills=tuple(s for s in skills if isinstance(s, str)),
        role=role if isinstance(role, str) else None,
    )


def effective_skills(
    repo: Path, project: str, *, line: str = "default", role: str | None = None
) -> list[str]:
    """Resolve the skills a run should load, honoring the role filter.

    Order: project line → project default line → empty. When a role is given
    (either from the argument or the stored set), skills whose manifest entry
    lists ``roles`` are kept only when the role appears; skills with no
    ``roles`` marker always apply.
    """
    record = get_project_skills(repo, project, line=line)
    if record is None and line != "default":
        record = get_project_skills(repo, project, line="default")
    skills = list(record.skills) if record else []
    effective_role = role or (record.role if record else None)
    if not effective_role:
        return skills
    manifest = load_manifest(repo)
    out: list[str] = []
    for name in skills:
        entry_roles = manifest.get(name, {}).get("roles") or []
        if not entry_roles or effective_role in entry_roles:
            out.append(name)
    return out


def list_projects(repo: Path) -> list[dict[str, Any]]:
    directory = projects_dir(repo)
    if not directory.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for child in sorted(directory.iterdir(), key=lambda p: p.name):
        if not child.name.endswith(".json"):
            continue
        project = child.name[: -len(".json")]
        if not _valid_bundle_name(project):
            continue
        body = _read_object(child) or {}
        lines = body.get("lines", {})
        out.append(
            {
                "project": project,
                "lines": sorted(k for k in lines if isinstance(k, str)),
            }
        )
    return out
