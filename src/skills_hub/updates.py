"""Upstream checks and a local backup of the central skills repo.

Git state is recorded on the existing manifest (``upstream`` and ``commit``).
There is no second index: ``manifest.json`` is already the sidecar
:func:`src.skills_hub.core.load_manifest` reads. ``check_updates`` and
``update_skill`` read only a local path — a GitHub (or any other network)
remote is not fetched. An imported copy has no ``.git``; its update copies
the tree recorded in the manifest and does not walk above that import.

``backup_repo`` commits a filtered tree and pushes it when ``remote`` is a
git remote (a local bare repository, or a network URL passed to ``git``
argv with any userinfo removed). A plain local directory is still a
checkout, not a push. ``*.sqlite``, ``*.sqlite3``, files larger than 100 MiB,
and names matching secret, token, credential, or ``.env`` are left out.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .core import (
    SKILL_MD,
    SkillsError,
    is_git_url,
    load_manifest,
    require_valid_name,
    write_manifest,
)

# "100MB" as MiB. A file of exactly this size is kept; one byte over is not.
MAX_BACKUP_BYTES = 100 * 1024 * 1024

_SENSITIVE_NAME = re.compile(r"secret|token|credential|\.env", re.IGNORECASE)


def _name_excluded(name: str) -> bool:
    lowered = name.lower()
    if lowered.endswith((".sqlite", ".sqlite3")):
        return True
    return _SENSITIVE_NAME.search(name) is not None


def excluded_from_backup(path: Path, *, stat: object | None = None) -> bool:
    """True when a backup must not contain ``path``.

    ``stat``, when passed, supplies ``st_size`` so tests can fake a file over
    ``MAX_BACKUP_BYTES`` without writing one. Name rules apply either way.
    """
    if _name_excluded(path.name):
        return True
    if stat is not None:
        size = getattr(stat, "st_size", None)
        if not isinstance(size, int):
            raise SkillsError("backup size check needs an integer stat.st_size")
        return size > MAX_BACKUP_BYTES
    if path.is_symlink():
        # The link itself is tiny. Callers skip links that leave the repo
        # before copying; measuring the target would read it.
        return False
    try:
        return path.stat().st_size > MAX_BACKUP_BYTES
    except OSError:
        return True


_BACKUP_REF = "refs/heads/skills-backup"
_URL_USERINFO_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^@/\s]+@")


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/echo"
    env["SSH_ASKPASS"] = "/bin/echo"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [
                "git",
                "-C",
                str(cwd),
                "-c",
                "protocol.file.allow=always",
                "-c",
                "safe.directory=*",
                *args,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            env=_git_env(),
        )
    except FileNotFoundError as exc:
        raise SkillsError("git executable not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise SkillsError("git command timed out") from exc


def _scrub_remote(text: str) -> str:
    """Drop URL userinfo so a push error cannot echo an embedded secret."""
    return _URL_USERINFO_RE.sub(r"\1", text)


def _strip_userinfo(url: str) -> str:
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    host = rest.split("/", 1)[0]
    if "@" not in host:
        return url
    return f"{scheme}://{rest.split('@', 1)[1]}"


def _local_upstream(url: str) -> Path | None:
    """A filesystem path, or None when ``url`` would leave the machine.

    Network remotes (https, ssh, ``git@``) are None so callers do not fetch them.
    """
    text = url.strip()
    if not text or is_git_url(text):
        return None
    if "://" in text:
        if not text.startswith("file://"):
            return None
        path = text[len("file://") :]
        # file://localhost/abs and file:///abs both yield an absolute path.
        if path.startswith("localhost/"):
            path = path[len("localhost") :]
        return Path(path)
    return Path(text).expanduser()


def _origin_url(skill_dir: Path) -> str | None:
    result = _git(skill_dir, "remote", "get-url", "origin")
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None


def _head(skill_dir: Path) -> str:
    result = _git(skill_dir, "rev-parse", "HEAD")
    if result.returncode != 0 or not result.stdout.strip():
        raise SkillsError(
            f"{skill_dir.name}: not a git checkout ({result.stderr.strip()})"
        )
    return result.stdout.strip()


def _dirty(skill_dir: Path) -> bool:
    result = _git(skill_dir, "status", "--porcelain")
    if result.returncode != 0:
        raise SkillsError(f"{skill_dir.name}: could not read git status")
    return bool(result.stdout.strip())


def _record_git(
    repo: Path,
    name: str,
    upstream: Path,
    commit: str,
    *,
    content_hash: str | None = None,
) -> None:
    manifest = load_manifest(repo)
    record = dict(manifest.get(name) or {})
    record["upstream"] = str(upstream)
    record["commit"] = commit
    if content_hash:
        record["content_hash"] = content_hash
    record.setdefault("agents", record.get("agents") or [])
    manifest[name] = record
    write_manifest(repo, manifest)


def _upstream_for(repo: Path, name: str, skill_dir: Path) -> Path:
    record = load_manifest(repo).get(name) or {}
    raw = record.get("upstream")
    if not isinstance(raw, str) or not raw.strip():
        raw = _origin_url(skill_dir)
    if not isinstance(raw, str) or not raw.strip():
        raise SkillsError(f"skill {name!r} has no upstream")
    local = _local_upstream(raw)
    if local is None:
        raise SkillsError(f"refusing to fetch skill {name!r} from a network remote")
    local = Path(os.path.abspath(local))
    if not local.exists():
        raise SkillsError(f"local upstream for {name!r} does not exist: {local}")
    return local


def _fetch(skill_dir: Path, upstream: Path) -> None:
    """``git fetch`` of a local path only. The URL is never a network remote."""
    result = _git(skill_dir, "fetch", str(upstream), "HEAD")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SkillsError(f"git fetch of local upstream failed: {detail}")


def _behind(skill_dir: Path) -> int:
    result = _git(skill_dir, "rev-list", "--count", "HEAD..FETCH_HEAD")
    if result.returncode != 0:
        raise SkillsError(f"could not compare {skill_dir.name} with its upstream")
    try:
        return int(result.stdout.strip() or "0")
    except ValueError as exc:
        raise SkillsError(f"unexpected rev-list output for {skill_dir.name}") from exc


def local_tracking(path: Path, *, ceiling: Path | None = None) -> dict[str, str] | None:
    """``upstream`` and ``commit`` when ``path`` is inside a local git checkout.

    The walk stops at ``ceiling`` so a temp import cannot latch onto a parent
    repository. A network origin is omitted: callers must not fetch it later.
    """
    current = Path(os.path.abspath(path))
    stop = Path(os.path.abspath(ceiling)) if ceiling is not None else None
    found: Path | None = None
    while True:
        if (current / ".git").exists():
            found = current
            break
        if stop is not None and current == stop:
            break
        if current.parent == current:
            break
        current = current.parent
    if found is None:
        return None
    url = _origin_url(found)
    if not url:
        return None
    local = _local_upstream(url)
    if local is None or not Path(os.path.abspath(local)).exists():
        return None
    try:
        commit = _head(found)
    except SkillsError:
        return None
    return {"upstream": str(Path(os.path.abspath(local))), "commit": commit}


def _manifest_behind(repo: Path, name: str) -> dict[str, Any] | None:
    """Compare a manifest commit with a local upstream. No fetch, no network.

    Import copies the skill files and drops ``.git``. The recorded local
    upstream is still enough to see that the bare repo moved.
    """
    record = load_manifest(repo).get(name) or {}
    raw = record.get("upstream")
    if not isinstance(raw, str) or not raw.strip():
        return None
    local = _local_upstream(raw)
    if local is None:
        return None
    local = Path(os.path.abspath(local))
    if not local.exists():
        return None
    head = _git(local, "rev-parse", "HEAD")
    if head.returncode != 0 or not head.stdout.strip():
        return None
    new = head.stdout.strip()
    old = record.get("commit") if isinstance(record.get("commit"), str) else ""
    if old and old == new:
        behind = 0
    elif old:
        counted = _git(local, "rev-list", "--count", f"{old}..{new}")
        if counted.returncode != 0:
            behind = 1
        else:
            try:
                behind = int(counted.stdout.strip() or "0")
            except ValueError:
                behind = 1
            if behind == 0 and old != new:
                behind = 1
    else:
        behind = 0 if not new else 1
    return {"name": name, "behind": behind}


def check_updates(repo: Path) -> list[dict[str, Any]]:
    """Fetch each skill's local upstream and report how many commits it is behind.

    A skill that is not its own checkout is still reported when the manifest
    names a local upstream. A network upstream is skipped rather than fetched.
    The manifest gains ``upstream`` and ``commit`` for every git checkout that
    was checked.
    """
    from .core import discover_skills

    reports: list[dict[str, Any]] = []
    if not repo.exists():
        return reports
    for entry in discover_skills(repo):
        try:
            require_valid_name(entry.name)
        except SkillsError:
            continue
        skill_dir = repo / entry.name
        if not (skill_dir / ".git").exists():
            row = _manifest_behind(repo, entry.name)
            if row is not None:
                reports.append(row)
            continue
        try:
            upstream = _upstream_for(repo, entry.name, skill_dir)
        except SkillsError:
            # No local upstream (or a network one). Do not fetch.
            continue
        _fetch(skill_dir, upstream)
        behind = _behind(skill_dir)
        commit = _head(skill_dir)
        _record_git(repo, entry.name, upstream, commit)
        reports.append({"name": entry.name, "behind": behind})
    reports.sort(key=lambda row: row["name"])
    return reports


def update_skill(repo: Path, name: str, *, force: bool = False) -> dict[str, Any]:
    """Fast-forward ``name`` to its local upstream. A dirty worktree is refused.

    An imported copy has no ``.git``. Its manifest already names a local
    upstream, so the update copies that tree. It does not fetch a network
    origin and it does not search parent directories for a checkout. When the
    copy's content hash differs from the hash recorded at install, the update
    is refused with ``local_modified`` unless ``force`` is set. ``force``
    snapshots the current tree, including modes and empty directories, before
    every replace that will happen.
    """
    require_valid_name(name)
    skill_dir = repo / name
    if not (skill_dir / ".git").exists():
        return _update_imported_copy(repo, name, skill_dir, force=force)
    if _dirty(skill_dir):
        raise SkillsError(f"skill {name!r} has a dirty worktree; refusing to update")
    upstream = _upstream_for(repo, name, skill_dir)
    _fetch(skill_dir, upstream)
    merged = _git(skill_dir, "merge", "--ff-only", "FETCH_HEAD")
    if merged.returncode != 0:
        detail = (merged.stderr or merged.stdout).strip()
        raise SkillsError(f"fast-forward refused for {name}: {detail}")
    commit = _head(skill_dir)
    _record_git(repo, name, upstream, commit)
    return {"name": name, "commit": commit, "upstream": str(upstream)}


def _update_imported_copy(
    repo: Path, name: str, skill_dir: Path, *, force: bool = False
) -> dict[str, Any]:
    from .adopt import content_hash
    from .snapshots import assert_skill_tree_restorable, snapshot_skill_content

    record = load_manifest(repo).get(name) or {}
    raw = record.get("upstream")
    if not isinstance(raw, str) or not raw.strip():
        raise SkillsError(f"skill {name!r} is not a git checkout")
    local = _local_upstream(raw)
    if local is None:
        raise SkillsError(f"refusing to fetch skill {name!r} from a network remote")
    local = Path(os.path.abspath(local))
    if not local.is_dir():
        raise SkillsError(f"local upstream for {name!r} does not exist: {local}")
    head = _git(local, "rev-parse", "HEAD")
    if head.returncode != 0 or not head.stdout.strip():
        raise SkillsError(f"local upstream for {name!r} has no commit")
    commit = head.stdout.strip()
    old = record.get("commit") if isinstance(record.get("commit"), str) else ""
    recorded_hash = record.get("content_hash")
    recorded = recorded_hash if isinstance(recorded_hash, str) else ""
    current = ""
    present = skill_dir.is_dir() and not skill_dir.is_symlink()
    if present and recorded:
        current = content_hash(skill_dir, allowed=(repo,))
        if current != recorded and not force:
            raise SkillsError(
                f"local_modified: skill {name!r} differs from the hash recorded at install"
            )
    # Same commit and same content hash means there is nothing to overwrite,
    # including when the recorded hash is absent. Do not snapshot that no-op.
    if old == commit and (skill_dir / SKILL_MD).is_file() and current == recorded:
        return {"name": name, "commit": commit, "upstream": str(local)}
    snapshot_id: str | None = None
    if force and present:
        # Permissions and empty directories do not change ``content_hash``.
        # Every replace that will happen still snapshots the whole tree first.
        assert_skill_tree_restorable(skill_dir)
        snapshot_id = snapshot_skill_content(repo, name)
    tmp = tempfile.TemporaryDirectory(prefix="conductor-skills-update-")
    try:
        source = _checkout_skill(Path(tmp.name), local, name)
        _replace_skill_tree(repo, name, source)
    finally:
        tmp.cleanup()
    _record_git(
        repo,
        name,
        local,
        commit,
        content_hash=content_hash(repo / name, allowed=(repo,)),
    )
    result = {"name": name, "commit": commit, "upstream": str(local)}
    if snapshot_id:
        result["snapshot_id"] = snapshot_id
    return result


def _checkout_skill(parent: Path, upstream: Path, name: str) -> Path:
    checkout = parent / "src"
    result = _git(
        parent,
        "clone",
        "--local",
        "--no-hardlinks",
        "--quiet",
        str(upstream),
        str(checkout),
    )
    if result.returncode != 0 or not checkout.is_dir():
        detail = _scrub_remote((result.stderr or result.stdout).strip())
        raise SkillsError(f"could not read local upstream: {detail}")
    skill = _find_imported_skill(checkout, name)
    if skill is None:
        raise SkillsError(f"skill {name!r} is not in its local upstream")
    return skill


def _find_imported_skill(tree: Path, name: str) -> Path | None:
    """Skill directory inside a checkout. Never walks above ``tree``."""
    root_md = tree / SKILL_MD
    if _skill_file(root_md) and not tree.is_symlink():
        return tree
    direct = tree / name
    if _skill_dir(direct):
        return direct
    try:
        children = list(tree.iterdir())
    except OSError:
        return None
    for child in children:
        if child.name.startswith(".") or child.is_symlink() or not child.is_dir():
            continue
        grand = child / name
        if _skill_dir(grand):
            return grand
    return None


def _skill_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _skill_dir(path: Path) -> bool:
    return not path.is_symlink() and path.is_dir() and _skill_file(path / SKILL_MD)


def _replace_skill_tree(repo: Path, name: str, source: Path) -> None:
    from .importer import (
        _check_budget,
        _remove_existing,
        _replace_target,
        _stage_in_repo,
    )

    _check_budget(source)
    staged = _stage_in_repo(repo, name, source)
    try:
        _replace_target(staged, repo / name)
    finally:
        if staged.exists():
            _remove_existing(staged)


def _backup_dest(remote: str, dest: Path | None) -> Path:
    if dest is not None:
        return Path(dest)
    local = _local_upstream(remote)
    if local is None:
        raise SkillsError(
            "refusing to back up to a network remote; pass a local path or dest"
        )
    return local


def _is_bare_repository(path: Path) -> bool:
    """True when ``path`` itself is a bare repo. Does not walk to a parent."""
    if not path.is_dir():
        return False
    try:
        result = subprocess.run(
            ["git", "--git-dir", str(path), "rev-parse", "--is-bare-repository"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=_git_env(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _head_exists(path: Path) -> bool:
    result = _git(path, "rev-parse", "--verify", "HEAD")
    return result.returncode == 0 and bool(result.stdout.strip())


def _within_repo(path: Path, repo: Path) -> bool:
    try:
        Path(os.path.abspath(os.path.realpath(path))).relative_to(
            Path(os.path.abspath(os.path.realpath(repo)))
        )
    except ValueError:
        return False
    return True


def backup_repo(repo: Path, remote: str, dest: Path | None = None) -> dict[str, Any]:
    """Copy ``repo``, omitting secrets, sqlite, and huge files.

    A local directory that is not already a bare repository receives the
    filtered checkout, as before. A local bare repository, or a network URL
    when ``dest`` is omitted, is a git remote: the filtered commit is pushed
    there with git argv only. Userinfo is stripped before it reaches argv or
    the returned remote. Nothing is fetched or cloned from a network host.
    """
    if not isinstance(remote, str) or not remote.strip():
        raise SkillsError("backup remote must be a non-empty string")
    if not repo.is_dir():
        raise SkillsError(f"skills repo {repo} is not a directory")
    if dest is None:
        local = _local_upstream(remote)
        bare = (
            Path(os.path.abspath(local))
            if local is not None and _is_bare_repository(Path(os.path.abspath(local)))
            else None
        )
        if bare is not None or local is None:
            return _push_backup(repo, remote, bare)
    target = Path(os.path.abspath(_backup_dest(remote, dest)))
    _commit_filtered_tree(repo, target)
    return {"dest": str(target), "remote": remote, "pushed": False}


def _push_backup(repo: Path, remote: str, bare: Path | None) -> dict[str, Any]:
    repo_abs = Path(os.path.abspath(repo))
    if bare is not None and (bare == repo_abs or _within_repo(bare, repo_abs)):
        raise SkillsError("refusing to back up a repo into itself")
    url = str(bare) if bare is not None else _strip_userinfo(remote.strip())
    safe_remote = url if bare is None else remote
    with tempfile.TemporaryDirectory(prefix="conductor-skills-backup-") as tmp:
        target = Path(tmp) / "repo"
        _commit_filtered_tree(repo, target)
        pushed = _git(target, "push", url, f"+HEAD:{_BACKUP_REF}")
        if pushed.returncode != 0:
            detail = _scrub_remote((pushed.stderr or pushed.stdout).strip())
            raise SkillsError(f"could not push backup: {detail}")
    if bare is not None and not _head_exists(bare):
        pointed = _git(bare, "symbolic-ref", "HEAD", _BACKUP_REF)
        if pointed.returncode != 0:
            detail = _scrub_remote((pointed.stderr or pointed.stdout).strip())
            raise SkillsError(f"could not point backup HEAD: {detail}")
    return {
        "dest": str(bare) if bare is not None else url,
        "remote": safe_remote,
        "pushed": True,
    }


def _commit_filtered_tree(repo: Path, target: Path) -> None:
    repo_abs = Path(os.path.abspath(repo))
    target = Path(os.path.abspath(target))
    if target == repo_abs or _within_repo(target, repo_abs):
        raise SkillsError("refusing to back up a repo into itself")
    if target.exists() and any(target.iterdir()):
        raise SkillsError(f"backup destination {target} is not empty")
    target.mkdir(parents=True, exist_ok=True)
    _copy_filtered(repo_abs, target)
    init = _git(target, "init")
    if init.returncode != 0:
        raise SkillsError(f"could not init backup repo: {init.stderr.strip()}")
    add = _git(target, "add", "-A")
    if add.returncode != 0:
        raise SkillsError(f"could not stage backup: {add.stderr.strip()}")
    commit = subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "-c",
            "user.email=backup@localhost",
            "-c",
            "user.name=conductor-skills-backup",
            "commit",
            "--allow-empty",
            "-m",
            "skills backup",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=_git_env(),
    )
    if commit.returncode != 0:
        raise SkillsError(f"could not commit backup: {commit.stderr.strip()}")


def _copy_filtered(repo_abs: Path, target: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(repo_abs, followlinks=False):
        rel_dir = Path(dirpath).relative_to(repo_abs)
        kept_dirs: list[str] = []
        for name in dirnames:
            if name == ".git" or _name_excluded(name):
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs
        destination_dir = target / rel_dir
        destination_dir.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            source = Path(dirpath) / name
            if source.is_symlink():
                if not _within_repo(source, repo_abs):
                    continue
                if excluded_from_backup(source):
                    continue
                (destination_dir / name).symlink_to(os.readlink(source))
                continue
            if not source.is_file():
                continue
            if excluded_from_backup(source):
                continue
            shutil.copy2(source, destination_dir / name)
