"""Dispatch workspace registry: validation, git lookup and CRUD.

The database module owns the rows; this module is where a path becomes a repo
root, an id becomes a remote, and a ``fetch_before_start`` runs. A dispatch
started with ``--workspace`` resolves its checkout here, so neither the API nor
the CLI has to know how a workspace is stored.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

from src.server.database.autonomy import dispatch_workspaces as workspace_db

logger = logging.getLogger(__name__)

#: A fetch before start is bounded; a hung remote must not hold the request.
DEFAULT_FETCH_TIMEOUT_SECONDS = 60.0
#: Stored text is bounded so a row stays small and a UI cannot be flooded.
MAX_NAME_LENGTH = 120
MAX_PATH_LENGTH = 4096
MAX_REMOTE_LENGTH = 2048
MAX_POLICY_LENGTH = 256
MAX_WORKER_LENGTH = 64
_OUTPUT_LIMIT = 4000

#: Where a registered remote that is not ``origin`` is fetched to.
WORKSPACE_FETCH_NAMESPACE = "dispatch-workspace"
#: Command-line settings for the pre-start fetch: no hooks, no fsmonitor
#: daemon, and no ``ext::`` transport, whatever the checkout configured.
_FETCH_HARDENING = (
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "protocol.ext.allow=never",
)

#: Fields a caller may pass to ``update_workspace``.
_UPDATE_FIELDS = ("name", "path", "git_remote", "sandbox_policy", "default_worker")


class WorkspaceNotFound(KeyError):
    """No workspace with that id or name for this user."""


class WorkspaceNameTaken(ValueError):
    """The user already has a workspace with this name."""


class WorkspaceInvalid(ValueError):
    """The workspace fields do not describe a usable checkout."""


def _clean(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text[:limit]


def normalize_path(path: str) -> str:
    """An absolute repo root, checked to exist as a directory.

    ``resolve`` follows symlinks, so two spellings of the same checkout are one
    path; the existence check catches a moved or unmounted repository at
    registration rather than at the first dispatch.
    """
    raw = str(path or "").strip()
    if not raw:
        raise WorkspaceInvalid("a workspace path is required")
    if len(raw) > MAX_PATH_LENGTH:
        raise WorkspaceInvalid(
            f"a workspace path is longer than {MAX_PATH_LENGTH} characters"
        )
    expanded = Path(os.path.expanduser(raw))
    try:
        resolved = expanded.resolve(strict=False)
    except OSError as exc:
        raise WorkspaceInvalid(f"cannot resolve the workspace path: {exc}") from exc
    if not resolved.exists():
        raise WorkspaceInvalid(f"the workspace path does not exist: {resolved}")
    if not resolved.is_dir():
        raise WorkspaceInvalid(f"the workspace path is not a directory: {resolved}")
    return str(resolved)


def _normalize_name(name: str) -> str:
    cleaned = _clean(name, MAX_NAME_LENGTH)
    if not cleaned:
        raise WorkspaceInvalid("a workspace name is required")
    return cleaned


_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]+@")


def strip_userinfo(remote: str) -> str:
    """The remote without a ``user:token@`` part (L2 review: never store a secret).

    Only URL forms carry one (``https://user:token@host/...``); an scp-style
    ``git@host:path`` keeps its user, which is a login name, not a secret.
    """
    return _USERINFO.sub(r"\1", str(remote or ""))


def redact_git_output(text: str) -> str:
    """Git output with any URL credential removed, before it is logged or returned."""
    from src.server.services.autonomy.security import redact_external_text

    return redact_external_text(_USERINFO.sub(r"\1[REDACTED]@", str(text or "")))


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    """A registry row as the API returns it: an older row's credential is removed."""
    if not isinstance(row, dict) or not row.get("git_remote"):
        return row
    cleaned = dict(row)
    cleaned["git_remote"] = strip_userinfo(cleaned["git_remote"])
    return cleaned


def _normalize_remote(remote: str) -> str:
    """A remote URL, never a Git option.

    ``git fetch <remote>`` parses a leading ``-`` as an option, so a stored
    ``--upload-pack=...`` would make git run that command. The value is stored
    for a later fetch, which is why the refusal is here and repeated where the
    fetch runs: an older row must not become executable either.
    """
    cleaned = strip_userinfo(_clean(remote, MAX_REMOTE_LENGTH))
    if cleaned.startswith("-"):
        raise WorkspaceInvalid("a workspace remote must not start with '-'")
    return cleaned


def _normalize_policy(policy: str) -> str:
    return _clean(policy, MAX_POLICY_LENGTH)


def _normalize_worker(worker: str) -> str:
    return _clean(worker, MAX_WORKER_LENGTH).lower()


def _validate_id(workspace_id: str) -> str:
    try:
        return str(uuid.UUID(str(workspace_id)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise WorkspaceInvalid("invalid workspace id") from exc


async def _row(user_id: str | None, workspace_id: str) -> dict[str, Any]:
    """Load a row, resolving a UUID id or a name; ownership is checked when given."""
    if user_id is not None:
        try:
            normalized = _validate_id(workspace_id)
        except WorkspaceInvalid:
            normalized = None
        row = (
            await workspace_db.get_workspace(user_id, normalized)
            if normalized is not None
            else None
        )
        if row is not None:
            return row
        row = await workspace_db.get_workspace_by_name(user_id, str(workspace_id))
    else:
        try:
            row = await workspace_db.get_workspace_by_id(_validate_id(workspace_id))
        except WorkspaceInvalid:
            row = None
    if row is None:
        raise WorkspaceNotFound(workspace_id)
    return _public_row(row)


async def detect_git_remote(path: str) -> str:
    """The checkout's ``origin`` URL, or ``""`` when git cannot name one.

    Never raises: a workspace may be a plain directory while the operator is
    setting it up, and a missing remote is a valid configuration.
    """
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            path,
            "remote",
            "get-url",
            "origin",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10.0)
    except (OSError, asyncio.TimeoutError):
        return ""
    except asyncio.CancelledError:
        if process is not None:
            process.kill()
            await process.wait()
        raise
    if process.returncode != 0:
        return ""
    return stdout.decode("utf-8", errors="replace").strip()[:MAX_REMOTE_LENGTH]


async def create_workspace(
    *,
    user_id: str,
    name: str,
    path: str,
    git_remote: str | None = None,
    sandbox_policy: str = "",
    default_worker: str = "",
) -> dict[str, Any]:
    """Register a checkout; an omitted remote is discovered from ``origin``."""
    resolved = normalize_path(path)
    fields = {
        "name": _normalize_name(name),
        "path": resolved,
        "git_remote": _normalize_remote(git_remote or ""),
        "sandbox_policy": _normalize_policy(sandbox_policy),
        "default_worker": _normalize_worker(default_worker),
    }
    if git_remote is None:
        fields["git_remote"] = _normalize_remote(await detect_git_remote(resolved))
    try:
        return _public_row(await workspace_db.create_workspace(user_id=user_id, **fields))
    except workspace_db.WorkspaceNameTaken as exc:
        raise WorkspaceNameTaken(fields["name"]) from exc


async def list_workspaces(user_id: str) -> list[dict[str, Any]]:
    return [_public_row(row) for row in await workspace_db.list_workspaces(user_id)]


async def get_workspace(user_id: str, workspace_id: str) -> dict[str, Any]:
    return await _row(user_id, workspace_id)


async def update_workspace(
    user_id: str, workspace_id: str, **fields: Any
) -> dict[str, Any]:
    """Change only the fields supplied; ``None`` means "leave it alone"."""
    changes: dict[str, Any] = {}
    for key in _UPDATE_FIELDS:
        value = fields.get(key)
        if value is None:
            continue
        if key == "name":
            changes[key] = _normalize_name(value)
        elif key == "path":
            changes[key] = normalize_path(value)
        elif key == "git_remote":
            changes[key] = _normalize_remote(value)
        elif key == "sandbox_policy":
            changes[key] = _normalize_policy(value)
        elif key == "default_worker":
            changes[key] = _normalize_worker(value)
    resolved_id = _validate_id((await _row(user_id, workspace_id))["workspace_id"])
    if not changes:
        return await _row(user_id, resolved_id)
    try:
        updated = await workspace_db.update_workspace(user_id, resolved_id, **changes)
    except workspace_db.WorkspaceNameTaken as exc:
        raise WorkspaceNameTaken(changes.get("name") or resolved_id) from exc
    if updated is None:
        raise WorkspaceNotFound(workspace_id)
    return _public_row(updated)


async def delete_workspace(user_id: str, workspace_id: str) -> None:
    row = await _row(user_id, workspace_id)
    if not await workspace_db.delete_workspace(user_id, row["workspace_id"]):
        raise WorkspaceNotFound(workspace_id)


async def workspace_repo(
    workspace_id: str, *, user_id: str | None = None
) -> dict[str, Any]:
    """Resolve a workspace id to its repo root and git remote.

    ``user_id`` is optional so recovery code can resolve an id it already
    trusts; the API and CLI pass it so a caller cannot read another user's path.
    The remote falls back to the checkout's ``origin`` when the row has none.
    """
    row = await _row(user_id, workspace_id)
    repo_root = str(Path(row["path"]).expanduser().resolve(strict=False))
    remote = str(row.get("git_remote") or "").strip()
    if not remote:
        remote = await detect_git_remote(repo_root)
    remote = strip_userinfo(remote)
    return {
        "workspace_id": row["workspace_id"],
        "name": row["name"],
        "path": row["path"],
        "repo_root": repo_root,
        "git_remote": remote,
        "sandbox_policy": row.get("sandbox_policy") or "",
        "default_worker": row.get("default_worker") or "",
    }


#: Name the task uses for the same lookup; kept as an alias so either reads.
repo_root_and_remote = workspace_repo
resolve_repo = workspace_repo


async def repo_root_for(workspace_id: str, *, user_id: str | None = None) -> str:
    return (await workspace_repo(workspace_id, user_id=user_id))["repo_root"]


async def git_remote_for(workspace_id: str, *, user_id: str | None = None) -> str:
    return (await workspace_repo(workspace_id, user_id=user_id))["git_remote"]


async def fetch_before_start(
    workspace_id: str,
    *,
    user_id: str | None = None,
    timeout: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run ``git fetch`` in the workspace before a dispatch starts.

    A failed fetch is returned, not raised: the caller decides whether a stale
    checkout may still start. A missing workspace is still an error. The output
    is bounded because a remote's chatter can be arbitrarily long.
    """
    repo = await workspace_repo(workspace_id, user_id=user_id)
    repo_root = repo["repo_root"]
    remote = repo["git_remote"]
    result: dict[str, Any] = {
        "workspace_id": repo["workspace_id"],
        "repo_root": repo_root,
        "remote": remote,
        "fetched": False,
        "returncode": None,
        "stdout": "",
        "stderr": "",
    }
    if remote.startswith("-"):
        # Defense in depth: a row written before the validation (or by hand)
        # must not turn into a git option when the fetch starts.
        result["stderr"] = "refusing a workspace remote that starts with '-'"
        logger.warning(
            "refusing option-like git remote for workspace %s",
            repo["workspace_id"],
        )
        return result
    argv = ["git"]
    for item in _FETCH_HARDENING:
        argv.extend(["-c", item])
    argv.extend(["-C", repo_root, "fetch", "--prune", "--no-recurse-submodules", "--"])
    # Fetch the named remote so ``refs/remotes/origin/*`` move; a URL alone
    # only writes FETCH_HEAD. A registered remote other than origin gets an
    # explicit refspec into its own namespace. ``--`` keeps either an operand.
    from src.server.services.dispatch_delivery import origin_url

    origin = strip_userinfo(await origin_url(Path(repo_root)))
    if not remote or (origin and strip_userinfo(remote) == origin):
        argv.append("origin")
    else:
        argv.extend([remote, f"+refs/heads/*:refs/remotes/{WORKSPACE_FETCH_NAMESPACE}/*"])
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        result["stderr"] = f"cannot run git: {exc}"[:_OUTPUT_LIMIT]
        return result
    except asyncio.CancelledError:
        if process is not None:
            process.kill()
            await process.wait()
        raise
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=max(1.0, float(timeout))
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        result["stderr"] = f"git fetch timed out after {timeout:g}s"
        return result
    result["returncode"] = process.returncode
    result["stdout"] = redact_git_output(
        stdout.decode("utf-8", errors="replace").strip()
    )[:_OUTPUT_LIMIT]
    result["stderr"] = redact_git_output(
        stderr.decode("utf-8", errors="replace").strip()
    )[:_OUTPUT_LIMIT]
    result["fetched"] = process.returncode == 0
    if not result["fetched"]:
        logger.warning(
            "git fetch failed for workspace %s at %s: %s",
            repo["workspace_id"],
            repo_root,
            result["stderr"],
        )
    return result


__all__ = [
    "WorkspaceInvalid",
    "WorkspaceNameTaken",
    "WorkspaceNotFound",
    "create_workspace",
    "delete_workspace",
    "detect_git_remote",
    "fetch_before_start",
    "get_workspace",
    "git_remote_for",
    "list_workspaces",
    "normalize_path",
    "repo_root_and_remote",
    "repo_root_for",
    "resolve_repo",
    "update_workspace",
    "workspace_repo",
]
