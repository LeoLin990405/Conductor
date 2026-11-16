"""Import skills into the central repo from a git URL, local folder or zip.

Nothing here talks to the network: a git URL is fetched through ``git clone
--depth 1`` (the only subprocess the module runs), local folders are read in
place and zips are extracted into a temp directory first. Skills already in
the repo are only replaced after a snapshot (see :mod:`.snapshots`) — the
caller wires that in. A forced replacement validates and fully stages the
new copy inside the repo before the old directory is touched, so a failed or
over-budget import leaves the previous skill in place.

Symlinks are always preserved as symlinks, never followed: a link pointing
outside the skill must not drag another tree into the repo or past the size
budget.

Error text is scrubbed of credential-shaped values before it is raised: git
echoes the remote it was given — ``user:pass@host`` and all — back on an
authentication failure, and an import URL can carry a token.

Layout note: a source may be either a *bundle* (a directory containing
several skill folders, e.g. a cloned repo) or a *single skill* (a directory
that itself carries ``SKILL.md``). Both are detected, so the same command
imports a one-off skill and a whole collection.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .core import (
    SKILL_MD,
    SkillsError,
    is_git_url,
    is_zip_path,
    redact_source,
    require_valid_name,
    write_manifest,
    load_manifest,
)
from .updates import local_tracking

MAX_SKILL_BYTES = 64 * 1024 * 1024
MAX_SKILL_FILES = 5000

# A failed clone quotes the remote URL back in git's stderr, and an import
# URL may carry its credentials in that URL: ``user:pass@host``, a
# ``?token=…`` query parameter, a token as the userinfo. Nothing this module
# puts in an error message may print one, so every string that came from
# outside the repo is scrubbed first. The shapes mirror
# ``server/utils/error_sanitization.py`` so both surfaces redact alike; they
# are duplicated rather than imported because the CLI path must not pull the
# server's dependencies in.
_URL_USERINFO_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^@/\s]+@")
_URL_SECRET_QUERY_RE = re.compile(
    r"(?i)([?&](?:key|api[-_]?key|apikey|token|access[-_]?token|secret"
    r"|password|passwd|credential|sig|signature)=)[^&\s\"']+"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer|token|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_TOKEN_SHAPE_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|glpat-[A-Za-z0-9_-]{16,}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{12,}|sk-[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{16,})\b"
)
REDACTED = "[REDACTED]"


def redact(text: str) -> str:
    """Scrub credential-shaped values out of text shown to a user or logged.

    A URL's userinfo goes whole — a username can *be* the token — while
    query-string secrets, bearer values and token-shaped words are masked in
    place. Host, path and the surrounding words survive: the operator still
    needs to see *which* remote failed.
    """
    text = _URL_USERINFO_RE.sub(r"\1", text)
    text = _URL_SECRET_QUERY_RE.sub(rf"\1{REDACTED}", text)
    text = _BEARER_RE.sub(rf"\1 {REDACTED}", text)
    return _TOKEN_SHAPE_RE.sub(REDACTED, text)


def copy_skill_tree(source: Path, target: Path) -> None:
    """Copy a skill directory preserving symlinks as symlinks.

    Used for import staging, copy-mode installs and snapshot restores:
    ``symlinks=True`` means a link is copied as a link, so the copy can never
    follow a link into a tree outside the skill (and cannot loop).
    """
    shutil.copytree(source, target, symlinks=True, ignore=_ignore_junk)


@dataclass(frozen=True)
class ImportResult:
    imported: tuple[str, ...]
    skipped: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"imported": list(self.imported), "skipped": list(self.skipped)}


def import_source(
    repo: Path, source: str, *, ref: str | None = None, force: bool = False
) -> ImportResult:
    """Import skills from ``source`` into ``repo``.

    ``force`` replaces an existing skill of the same name; without it the
    existing one is kept and the name reported as skipped.
    """
    if not isinstance(source, str) or not source.strip():
        raise SkillsError("import source must be a non-empty string")
    source = source.strip()
    with tempfile.TemporaryDirectory(prefix="conductor-skills-") as tmp:
        root = Path(tmp)
        if is_git_url(source):
            fetched = _clone(source, root, ref)
        elif is_zip_path(source):
            fetched = _unzip(source, root)
        else:
            fetched = Path(source).expanduser().resolve()
            if not fetched.is_dir():
                raise SkillsError(
                    f"local import source is not a directory: {redact(source)}"
                )
        return _absorb(repo, fetched, source, ref, force)


def _clone(source: str, dest: Path, ref: str | None) -> Path:
    """Shallow-clone ``source``; the network is only touched here."""
    target = dest / "src"
    cmd = ["git", "clone", "--depth", "1", "--quiet"]
    if ref:
        cmd += ["--branch", ref]
    cmd += ["--", source, str(target)]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            env={
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "/bin/echo",
                "SSH_ASKPASS": "/bin/echo",
                "GIT_CONFIG_NOSYSTEM": "1",
                "HOME": str(dest),
                "PATH": _git_path_env(),
            },
        )
    except FileNotFoundError as exc:
        raise SkillsError("git executable not found; cannot clone") from exc
    except subprocess.TimeoutExpired as exc:
        raise SkillsError("git clone timed out after 300s") from exc
    if completed.returncode != 0:
        # git echoes the remote it was given (credentials included) on an
        # auth failure; the last line is the summary, and it is scrubbed
        # before it can reach a log or a CLI error.
        detail = redact((completed.stderr or "").strip()).splitlines()
        raise SkillsError(
            f"git clone failed: {detail[-1] if detail else 'unknown error'}"
        )
    if not target.is_dir():
        raise SkillsError("git clone produced no checkout")
    return target


def _git_path_env() -> str:
    return os.environ.get("PATH", "/usr/bin:/bin")


def _unzip(source: str, dest: Path) -> Path:
    path = Path(source).expanduser()
    if not path.is_file():
        raise SkillsError(f"zip import source not found: {redact(source)}")
    target = dest / "src"
    target.mkdir(parents=True)
    total = 0
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) > MAX_SKILL_FILES:
                raise SkillsError(f"zip contains too many entries ({len(names)})")
            for info in archive.infolist():
                _reject_zip_traversal(info.filename)
                if info.is_dir():
                    continue
                total += info.file_size
                if total > MAX_SKILL_BYTES:
                    raise SkillsError("zip exceeds the 64 MiB skill budget")
            archive.extractall(target)
    except zipfile.BadZipFile as exc:
        raise SkillsError(f"not a valid zip archive: {redact(source)}") from exc
    return target


def _reject_zip_traversal(name: str) -> None:
    candidate = Path(name)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise SkillsError(f"zip entry escapes the archive: {redact(name)}")


def _iter_skill_dirs(root: Path) -> Iterator[Path]:
    """Yield skill directories under ``root`` (depth ≤ 2).

    Depth 1 covers a bundle of skills (``root/<skill>/SKILL.md``); depth 2
    covers the common GitHub zip habit of wrapping everything in one folder
    (``root/<wrapper>/<skill>/SKILL.md``). Deeper trees are not scanned — a
    skill that buries its ``SKILL.md`` three levels down is not one the fleet
    CLIs would find either.
    """
    if (root / SKILL_MD).is_file():
        yield root
        return
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return
    for child in children:
        if child.name.startswith("."):
            continue
        if not child.is_dir() or child.is_symlink():
            continue
        if (child / SKILL_MD).is_file():
            yield child
            continue
        for grandchild in sorted(child.iterdir(), key=lambda p: p.name):
            if grandchild.name.startswith("."):
                continue
            if grandchild.is_dir() and not grandchild.is_symlink():
                if (grandchild / SKILL_MD).is_file():
                    yield grandchild


def _absorb(
    repo: Path, fetched: Path, source: str, ref: str | None, force: bool
) -> ImportResult:
    repo.mkdir(parents=True, exist_ok=True)
    imported: list[str] = []
    skipped: list[str] = []
    manifest = load_manifest(repo)
    for skill_dir in _iter_skill_dirs(fetched):
        name = skill_dir.name
        try:
            require_valid_name(name)
        except Exception:
            skipped.append(name)
            continue
        target = repo / name
        if target.exists() and not force:
            skipped.append(name)
            continue
        # Budget and symlink safety first: a --force replace must not delete
        # the existing skill before the new one is known to be importable.
        _check_budget(skill_dir)
        # Validate and stage the full replacement before the existing skill
        # is touched: an over-budget or failed copy must leave the central
        # skill exactly as it was.
        staged = _stage_in_repo(repo, name, skill_dir)
        try:
            _replace_target(staged, target)
        finally:
            _remove_existing(staged)
        tracked = local_tracking(skill_dir, ceiling=fetched) or {}
        record = {
            "source": redact_source(source),
            "source_ref": ref,
            "agents": list(manifest.get(name, {}).get("agents", [])),
            **tracked,
        }
        digest = _install_content_hash(target, repo)
        if digest is not None:
            record["content_hash"] = digest
        manifest[name] = record
        imported.append(name)
    if imported:
        write_manifest(repo, manifest)
    return ImportResult(imported=tuple(imported), skipped=tuple(skipped))


def _install_content_hash(target: Path, repo: Path) -> str | None:
    """Hash recorded at install, or None when the tree cannot be hashed in place.

    A skill may keep a symlink whose target sits outside the repo. That link
    is part of the import, and hashing it must not fail the import.
    """
    from .adopt import content_hash

    try:
        return content_hash(target, allowed=(repo,))
    except SkillsError:
        return None


def _stage_in_repo(repo: Path, name: str, skill_dir: Path) -> Path:
    """Copy ``skill_dir`` into a staging directory inside ``repo``.

    Staging on the same filesystem makes the later rename atomic; on any
    failure the staging directory is removed so no partial copy is left.
    """
    staged = repo / f".stage-{name}-{os.getpid()}"
    _remove_existing(staged)
    try:
        copy_skill_tree(skill_dir, staged)
    except BaseException:
        _remove_existing(staged)
        raise
    return staged


def _replace_target(staged: Path, target: Path) -> None:
    """Rename ``staged`` into place, keeping the previous content recoverable.

    Both renames are same-filesystem moves: at every instant either the old
    or the new content exists at ``target`` — never nothing.
    """
    if not (target.exists() or target.is_symlink()):
        os.replace(staged, target)
        return
    backup = target.with_name(f".{target.name}.old-{os.getpid()}")
    os.replace(target, backup)
    try:
        os.replace(staged, target)
    except BaseException:
        os.replace(backup, target)
        raise
    _remove_existing(backup)


def _ignore_junk(directory: str, entries: list[str]) -> list[str]:
    ignored = {".git", ".DS_Store", "__pycache__", ".venv"}
    return [e for e in entries if e in ignored]


def _remove_existing(target: Path) -> None:
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)


def _check_budget(skill_dir: Path) -> None:
    """Enforce the file-count and byte budgets without following symlinks.

    ``os.walk(followlinks=False)`` never descends into a linked directory, so
    a symlink pointing at a large tree outside the skill cannot smuggle
    content past the check (the copy preserves links instead of following
    them, so what is counted is exactly what lands in the repo). A link is
    counted but not sized: its target is never copied, so it adds no bytes.
    """
    total = 0
    count = 0
    for dirpath, dirnames, filenames in os.walk(skill_dir, followlinks=False):
        for entry in (*dirnames, *filenames):
            count += 1
            if count > MAX_SKILL_FILES:
                raise SkillsError(
                    f"skill {skill_dir.name!r} has more than {MAX_SKILL_FILES} files"
                )
            path = Path(dirpath) / entry
            if path.is_symlink():
                continue
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError as exc:
                    raise SkillsError(
                        f"could not size {skill_dir.name!r}: {exc.strerror or exc}"
                    ) from exc
                if total > MAX_SKILL_BYTES:
                    raise SkillsError(
                        f"skill {skill_dir.name!r} exceeds the "
                        f"{MAX_SKILL_BYTES // (1024 * 1024)} MiB budget"
                    )


def remove_skill(repo: Path, name: str) -> None:
    """Remove a skill folder from the repo.

    The manifest entry is *kept* (with its ``agents`` list) so the next sync
    knows those agent entries are now stale and should be removed too;
    :func:`src.skills_hub.manager.sync` drops the record once no agent still
    references the skill.
    """
    require_valid_name(name)
    target = repo / name
    if target.exists() or target.is_symlink():
        _remove_existing(target)


def iter_source_skills(root: Path) -> Iterable[Path]:
    """Public wrapper for tests and the sync path."""
    return _iter_skill_dirs(root)
