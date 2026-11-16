"""Report how each agent skill relates to the central repo.

``how`` is ``link`` or ``copy`` when the manifest says this hub installed the
skill, ``manual`` when the agent directory has a skill the manifest does not
list, and ``missing`` when the manifest lists one the directory does not have.

``drift`` is true for a hub link that is broken or whose target tree hash
differs from the central skill it should name, and for a hub copy whose bytes
differ from that central skill. A manual skill is never drift: the hub did
not install it, so there is no central tree to drift from.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .adopt import _abspath, _assert_symlink_stays, content_hash
from .core import SKILL_MD, SkillsError, load_manifest


def _assignment(
    record: Mapping[str, Any], agent: str, name: str
) -> tuple[str | None, str | None]:
    """Central name and recorded hash for ``agent``, if the hub installed it."""
    variants = record.get("variants") or []
    if isinstance(variants, list):
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            agents = variant.get("agents") or []
            if agent in agents:
                central = variant.get("central_name")
                digest = variant.get("hash")
                return (
                    central if isinstance(central, str) else name,
                    digest if isinstance(digest, str) else None,
                )
    agents = record.get("agents") or []
    if agent not in agents:
        return None, None
    central = record.get("central_name")
    digest = record.get("content_hash")
    return (
        central if isinstance(central, str) else name,
        digest if isinstance(digest, str) else None,
    )


def _hash_or_none(path: Path, allowed: Sequence[Path]) -> str | None:
    try:
        return content_hash(path, allowed=allowed)
    except (SkillsError, OSError):
        return None


def _skill_row(
    agent: str,
    name: str,
    path: Path | None,
    repo: Path,
    record: Mapping[str, Any],
) -> dict[str, Any]:
    central_name, recorded = _assignment(record, agent, name)
    installed = central_name is not None
    conflict = bool(record.get("conflict"))
    central_path = repo / central_name if central_name else repo / name
    how = "missing"
    drift = False
    if path is None:
        how = "missing" if installed else "manual"
    elif path.is_symlink():
        how = "link" if installed else "manual"
        broken = not path.exists()
        if how == "link":
            if broken or not (central_path / SKILL_MD).is_file():
                drift = True
            else:
                try:
                    same_target = os.path.realpath(path) == os.path.realpath(central_path)
                except OSError:
                    same_target = False
                target_hash = _hash_or_none(path, (path.parent, repo))
                central_hash = _hash_or_none(central_path, (repo,))
                if target_hash is None or central_hash is None or target_hash != central_hash:
                    drift = True
                elif not same_target and recorded is not None and target_hash != recorded:
                    drift = True
        # A manual symlink is not a hub link, so it is not drift.
    elif path.is_dir() and (path / SKILL_MD).is_file():
        how = "copy" if installed else "manual"
        if how == "copy":
            agent_hash = _hash_or_none(path, (path.parent, repo))
            central_hash = _hash_or_none(central_path, (repo,))
            drift = (
                agent_hash is None
                or central_hash is None
                or agent_hash != central_hash
            )
    else:
        how = "manual"
    return {
        "name": name,
        "how": how,
        "drift": drift,
        "conflict": conflict if installed else False,
    }


def status_report(
    home: Path,
    repo: Path,
    agent_dirs: Sequence[tuple[str, Path]],
) -> dict[str, Any]:
    """JSON-ready per-agent inventory. Reads the manifest and the trees only."""
    repo_path = _abspath(home, repo)
    manifest = load_manifest(repo_path) if repo_path.exists() else {}
    agents: dict[str, Any] = {}
    for agent, directory in agent_dirs:
        directory = _abspath(home, directory)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        if directory.is_dir():
            try:
                children = sorted(directory.iterdir(), key=lambda p: p.name)
            except OSError as exc:
                agents[agent] = {
                    "dir": str(directory),
                    "skills": [],
                    "error": f"could not read directory: {exc}",
                }
                continue
            for child in children:
                if child.name.startswith("."):
                    continue
                is_link = child.is_symlink()
                record = manifest.get(child.name) or {}
                installed = _assignment(record, agent, child.name)[0] is not None
                if is_link:
                    # Do not stat or read a link that leaves the agent dir and the repo.
                    try:
                        _assert_symlink_stays(child, (directory, repo_path))
                    except SkillsError:
                        if installed:
                            rows.append(
                                {
                                    "name": child.name,
                                    "how": "link",
                                    "drift": True,
                                    "conflict": bool(record.get("conflict")),
                                }
                            )
                            seen.add(child.name)
                        continue
                if is_link and not child.exists():
                    if not installed:
                        continue
                    rows.append(_skill_row(agent, child.name, child, repo_path, record))
                    seen.add(child.name)
                    continue
                skill_md = child / SKILL_MD
                if skill_md.is_symlink():
                    try:
                        _assert_symlink_stays(skill_md, (directory, repo_path))
                    except SkillsError:
                        continue
                if not skill_md.is_file():
                    continue
                rows.append(_skill_row(agent, child.name, child, repo_path, record))
                seen.add(child.name)
        for name, record in sorted(manifest.items()):
            if name in seen:
                continue
            if _assignment(record, agent, name)[0] is None:
                continue
            rows.append(_skill_row(agent, name, None, repo_path, record))
        agents[agent] = {"dir": str(directory), "skills": rows}
    return {"agents": agents}
