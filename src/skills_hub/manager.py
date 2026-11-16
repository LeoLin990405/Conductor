"""Install/uninstall/sync between the central repo and agent skill dirs.

Safety rules this module enforces:

* **Tracked only.** Uninstall removes an agent entry only when the manifest
  says Conductor put it there. A skill the user installed by hand stays.
* **Refuse real checkout.** Installing into a directory that lives inside a
  git worktree fails: those skills belong to their repo.
* **Snapshot first.** Every mutating pass snapshots the agent directories it
  is about to touch, so any change can be rolled back.
* **Sync never deletes by default.** A tracked skill that disappeared from
  the repo (missing ``SKILL.md``, broken symlink, an operator edit) is
  reported stale and left in place; removing it takes ``uninstall`` or an
  explicit ``sync(prune=True)`` — deletion is never the default a surprise
  sync runs with.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import importer
from .adapters import LinkMode, UnknownAgentError, normalize_link_mode
from .core import (
    SKILL_MD,
    SkillsError,
    discover_skills,
    load_manifest,
    require_valid_name,
    write_manifest,
)
from .importer import copy_skill_tree
from .snapshots import create_snapshot, record_absent_symlink, rollback_snapshot
from .status import _assignment


@dataclass(frozen=True)
class ChangeSet:
    installed: tuple[tuple[str, str], ...] = ()  # (agent, skill)
    uninstalled: tuple[tuple[str, str], ...] = ()
    skipped: tuple[tuple[str, str, str], ...] = ()  # (agent, skill, reason)
    stale: tuple[tuple[str, str], ...] = ()  # tracked, but gone from the repo
    snapshot: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "installed": [{"agent": a, "skill": s} for a, s in self.installed],
            "uninstalled": [{"agent": a, "skill": s} for a, s in self.uninstalled],
            "skipped": [
                {"agent": a, "skill": s, "reason": r} for a, s, r in self.skipped
            ],
            "stale": [{"agent": a, "skill": s} for a, s in self.stale],
            "snapshot": self.snapshot,
        }


def _is_inside_git_worktree(path: Path) -> bool:
    """True when ``path`` sits inside a git worktree or the repo itself.

    ``~/.claude`` is dot-named and rarely a repo, but a user may point an
    override at a checked-out skills collection; writing there would dirty a
    real checkout.
    """
    current = path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return True
    return False


def install(
    repo: Path,
    agent_dirs: list[tuple[str, Path]],
    skills: list[str],
    *,
    mode: LinkMode = "symlink",
) -> ChangeSet:
    """Install named skills into each given agent directory."""
    if mode not in ("symlink", "copy"):
        raise SkillsError(f"unknown link mode {mode!r}")
    resolved = _resolve_skill_names(repo, skills)
    # Pre-flight every target so a bad directory aborts before anything is
    # written: one bad agent should not leave the rest half-installed.
    for _agent, directory in agent_dirs:
        _ensure_agent_dir(directory, create=False)
    stamp = _snapshot(agent_dirs, repo)
    installed: list[tuple[str, str]] = []
    skipped: list[tuple[str, str, str]] = []
    manifest = load_manifest(repo)
    for agent, directory in agent_dirs:
        _ensure_agent_dir(directory)
        for name in resolved:
            target = directory / name
            if target.exists() or target.is_symlink():
                skipped.append((agent, name, "already installed"))
                continue
            source = _central_source(repo, manifest, agent, name)
            if mode == "symlink":
                _relative_symlink(source, target)
            else:
                copy_skill_tree(source, target)
            installed.append((agent, name))
            record = manifest.setdefault(name, {"agents": []})
            agents = record.setdefault("agents", [])
            if agent not in agents:
                agents.append(agent)
    if installed:
        write_manifest(repo, manifest)
    return ChangeSet(installed=tuple(installed), skipped=tuple(skipped), snapshot=stamp)


def _snapshot(agent_dirs: list[tuple[str, Path]], repo: Path) -> str | None:
    if not agent_dirs:
        return None
    return create_snapshot(repo, agent_dirs)


def _latest_snapshot_id(repo: Path) -> str | None:
    from .snapshots import list_snapshots

    items = list_snapshots(repo)
    return items[0]["id"] if items else None


def _resolve_skill_names(repo: Path, skills: list[str]) -> list[str]:
    if not skills:
        raise SkillsError("no skills named; nothing to install")
    available = {entry.name for entry in discover_skills(repo)}
    manifest = load_manifest(repo)
    resolved: list[str] = []
    for name in skills:
        require_valid_name(name)
        if name not in available and _manifest_alias(repo, manifest, name) is None:
            raise SkillsError(
                f"skill {name!r} not found in the central repo; run 'conductor skills import' first"
            )
        resolved.append(name)
    return resolved


def _safe_skill_component(name: str) -> bool:
    return (
        bool(name)
        and name not in {".", ".."}
        and "/" not in name
        and "\\" not in name
        and ".." not in name
    )


def _manifest_alias(
    repo: Path, manifest: dict[str, dict[str, Any]], name: str
) -> str | None:
    """Central directory for a dedup alias that has no ``repo/<name>`` tree."""
    record = manifest.get(name)
    if not isinstance(record, dict):
        return None
    candidates: list[str] = []
    variants = record.get("variants") or []
    if isinstance(variants, list):
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            central_name = variant.get("central_name")
            if isinstance(central_name, str):
                candidates.append(central_name)
    central = record.get("central_name")
    if isinstance(central, str):
        candidates.append(central)
    for chosen in candidates:
        if chosen == name or not _safe_skill_component(chosen):
            continue
        if (repo / chosen / SKILL_MD).is_file():
            return chosen
    return None


def _ensure_agent_dir(directory: Path, *, create: bool = True) -> None:
    if _is_inside_git_worktree(directory):
        raise SkillsError(
            f"refusing to install into {directory}: it is inside a git worktree"
        )
    if create:
        directory.mkdir(parents=True, exist_ok=True)


def _relative_symlink(source: Path, target: Path) -> None:
    relative = os.path.relpath(source, target.parent)
    target.symlink_to(relative)


def _central_source(
    repo: Path, manifest: dict[str, dict[str, Any]], agent: str, name: str
) -> Path:
    """Directory in ``repo`` that this agent should link for ``name``.

    Deduped adopts store a different body under ``variants[].central_name``.
    Sync and install follow that mapping. A record without one uses ``name``.
    """
    record = manifest.get(name) or {}
    central, _digest = _assignment(record, agent, name)
    chosen = central if isinstance(central, str) and central else name
    if not _safe_skill_component(chosen):
        chosen = name
    if not (repo / chosen / SKILL_MD).is_file():
        alias = _manifest_alias(repo, manifest, name)
        if alias is not None:
            chosen = alias
    return repo / chosen


def uninstall(
    repo: Path,
    agent_dirs: list[tuple[str, str | Path]],
    skills: list[str] | None = None,
) -> ChangeSet:
    """Remove Conductor-installed entries from each agent directory.

    Only entries the manifest attributes to Conductor are removed; a name
    that Conductor did not install (or that is absent) is reported as skipped
    and the filesystem is left untouched for it.
    """
    targets = [(agent, Path(directory)) for agent, directory in agent_dirs]
    # Same guard as install, but for every target before anything is removed:
    # a later refused directory must not find earlier skills already deleted.
    for _agent, directory in targets:
        if _is_inside_git_worktree(directory):
            raise SkillsError(
                f"refusing to uninstall from {directory}: it is inside a git worktree"
            )
    _snapshot(agent_dirs, repo)
    manifest = load_manifest(repo)
    uninstalled: list[tuple[str, str]] = []
    skipped: list[tuple[str, str, str]] = []
    dirty = False
    for agent, directory in targets:
        tracked = _tracked_names(manifest, agent, skills)
        if skills:
            # Explicitly named skills Conductor does not track for this agent
            # are reported, so a typo says so instead of silently passing.
            for name in skills:
                if name not in tracked:
                    skipped.append((agent, name, "not installed by conductor"))
        if not tracked:
            continue
        for name in tracked:
            entry = directory / name
            if not entry.exists() and not entry.is_symlink():
                skipped.append((agent, name, "not present"))
                continue
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
            elif entry.is_dir():
                shutil.rmtree(entry)
            uninstalled.append((agent, name))
            record = manifest.get(name)
            if record and agent in record.get("agents", []):
                record["agents"].remove(agent)
                dirty = True
    if dirty:
        _drop_empty_records(manifest)
        write_manifest(repo, manifest)
    return ChangeSet(
        uninstalled=tuple(uninstalled),
        skipped=tuple(skipped),
        snapshot=_latest_snapshot_id(repo),
    )


def _tracked_names(
    manifest: dict[str, dict[str, Any]], agent: str, skills: list[str] | None
) -> list[str]:
    wanted = set(skills) if skills else None
    names = [
        name
        for name, record in sorted(manifest.items())
        if agent in (record.get("agents") or [])
    ]
    if wanted is None:
        return names
    return [name for name in names if name in wanted]


def _drop_empty_records(manifest: dict[str, dict[str, Any]]) -> None:
    for name in [n for n, r in manifest.items() if not (r.get("agents") or [])]:
        manifest.pop(name, None)


def sync(
    repo: Path,
    agent_dirs: list[tuple[str, Path]],
    *,
    mode: LinkMode = "symlink",
    prune: bool = False,
) -> ChangeSet:
    """Reconcile agent directories with the central repo.

    Missing Conductor-tracked skills are installed; nothing else is touched
    by default. Tracked skills that no longer exist in the repo are reported
    as *stale* and left in place — a transiently unreadable or edited repo
    must not make a routine sync delete skills (and the manifest record) that
    a later sync could have restored. ``prune=True`` removes those entries
    instead, dropping the manifest record once no agent references it;
    ``uninstall`` is the explicit path for named removals.
    """
    manifest = load_manifest(repo)
    installed: list[tuple[str, str]] = []
    uninstalled: list[tuple[str, str]] = []
    stale: list[tuple[str, str]] = []
    # Collect the full plan before touching anything so the snapshot covers
    # every directory that will change.
    to_install: list[tuple[str, Path, str, Path]] = []
    to_remove: list[tuple[str, Path, str]] = []
    for agent, directory in agent_dirs:
        if _is_inside_git_worktree(directory):
            raise SkillsError(
                f"refusing to sync {directory}: it is inside a git worktree"
            )
        tracked = _tracked_names(manifest, agent, None)
        for name in tracked:
            source = _central_source(repo, manifest, agent, name)
            ready = (source / SKILL_MD).is_file()
            if not ready:
                if prune:
                    to_remove.append((agent, directory, name))
                else:
                    stale.append((agent, name))
            elif not ((directory / name).exists() or (directory / name).is_symlink()):
                to_install.append((agent, directory, name, source))
    if not to_install and not to_remove:
        return ChangeSet(stale=tuple(stale))
    stamp = _snapshot(agent_dirs, repo)
    if stamp is not None and mode == "symlink":
        for agent, directory, name, source in to_install:
            relative = os.path.relpath(source, (directory / name).parent)
            record_absent_symlink(repo, stamp, agent, name, relative)
    for agent, directory, name in to_remove:
        entry = directory / name
        if entry.is_symlink() or entry.is_file():
            entry.unlink()
        elif entry.is_dir():
            shutil.rmtree(entry)
        record = manifest.get(name)
        if record and agent in (record.get("agents") or []):
            record["agents"].remove(agent)
        uninstalled.append((agent, name))
    if to_remove:
        _drop_empty_records(manifest)
    for agent, directory, name, source in to_install:
        _ensure_agent_dir(directory)
        target = directory / name
        if target.exists() or target.is_symlink():
            continue
        if mode == "symlink":
            _relative_symlink(source, target)
        else:
            copy_skill_tree(source, target)
        installed.append((agent, name))
        record = manifest.setdefault(name, {"agents": []})
        if agent not in (record.get("agents") or []):
            record.setdefault("agents", []).append(agent)
    write_manifest(repo, manifest)
    return ChangeSet(
        installed=tuple(installed),
        uninstalled=tuple(uninstalled),
        stale=tuple(stale),
        snapshot=_latest_snapshot_id(repo),
    )


def rollback(
    repo: Path,
    snapshot_id: str,
    *,
    allowed_dirs: set[Path] | None = None,
) -> dict[str, Any]:
    """Restore agent directories to ``snapshot_id``.

    ``allowed_dirs`` restricts which directories the snapshot may touch; a
    snapshot naming anything outside the set is refused before anything is
    touched. The CLI passes its configured agent directories. Nothing is
    deleted: entries the snapshot never recorded come back in the result as
    ``kept``.
    """
    return rollback_snapshot(repo, snapshot_id, allowed_dirs=allowed_dirs)


def list_installed(repo: Path, agent_dirs: list[tuple[str, Path]]) -> dict[str, Any]:
    """Per-agent inventory: tracked installs, foreign entries, broken links.

    One unreadable or looping agent directory is reported with an ``error``
    field instead of failing the whole inventory — the other agents are still
    worth showing.
    """
    manifest = load_manifest(repo)
    report: dict[str, Any] = {}
    for agent, directory in agent_dirs:
        tracked = set(_tracked_names(manifest, agent, None))
        try:
            report[agent] = _scan_agent_dir(directory, tracked)
        except (OSError, RuntimeError) as exc:
            report[agent] = {
                "dir": str(directory),
                "exists": directory.is_dir(),
                "entries": [],
                "foreign": [],
                "broken_symlinks": [],
                "error": f"could not read directory: {exc}",
            }
    return report


def _scan_agent_dir(directory: Path, tracked: set[str]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    foreign: list[str] = []
    broken: list[str] = []
    if directory.is_dir():
        for child in sorted(directory.iterdir(), key=lambda p: p.name):
            if child.is_symlink():
                resolves = child.resolve().is_dir() and (child / SKILL_MD).is_file()
                entries.append(
                    {
                        "name": child.name,
                        "kind": "symlink",
                        "managed": child.name in tracked,
                        "healthy": resolves,
                    }
                )
                if not resolves:
                    broken.append(child.name)
            elif child.is_dir() and (child / SKILL_MD).is_file():
                entries.append(
                    {
                        "name": child.name,
                        "kind": "copy",
                        "managed": child.name in tracked,
                        "healthy": True,
                    }
                )
            if child.name not in tracked and (child.is_dir() or child.is_symlink()):
                foreign.append(child.name)
    return {
        "dir": str(directory),
        "exists": directory.is_dir(),
        "entries": entries,
        "foreign": sorted(set(foreign)),
        "broken_symlinks": broken,
    }


def import_skills(
    repo: Path, source: str, *, ref: str | None = None, force: bool = False
) -> dict[str, Any]:
    result = importer.import_source(repo, source, ref=ref, force=force)
    return result.to_dict()


__all__ = [
    "ChangeSet",
    "SkillsError",
    "UnknownAgentError",
    "import_skills",
    "install",
    "list_installed",
    "normalize_link_mode",
    "rollback",
    "sync",
    "uninstall",
]
