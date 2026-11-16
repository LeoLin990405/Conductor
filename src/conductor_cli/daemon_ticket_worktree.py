"""Host-local Ticket checkout records for the local daemon.

The daemon creates and removes Ticket worktrees on its own machine, under its
own ``--worktree-root``, from the ``base_sha`` the server pinned at claim time
and the branch the server named. A server absolute path is never followed: a
persisted ``worktree_path`` is reused only when it already exists on this host,
under this root, registered on the Ticket branch.

Two local records fence removal:

* the host ownership record (one per Ticket) stores the server's
  ``worktree_generation`` from the last successful bind, so a stale cleanup of
  an older generation never removes a checkout a newer attempt bound;
* the cleanup journal (one entry per Ticket and generation, so a newer
  attempt's entry never overwrites an older one still owed a report) is
  written only after the server accepted the terminal report or a verified
  stop, and is drained by retrying the removal and reporting its true outcome.
  Records are fsynced (file and directory) before the caller relies on them.

Only the pure parts live here (names, validation, records, the host lock):
the wheel that ships ``conductor_cli`` has no ``src.*``, so the Git
operations, which reuse the dispatch launcher's isolated Git helpers, live in
``scripts/conductor_local_daemon.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import stat
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BASE_BRANCH = "origin/main"
# Only server-named Ticket branches; a persisted path or an existing branch
# outside this namespace is never adopted (user, dispatch or line checkouts).
TICKET_BRANCH_PREFIX = "conductor/ticket/"
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_BASE_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PROTECTED_BRANCHES = frozenset({"main", "master", "HEAD", "trunk", "develop"})
_CLEANUP_BACKOFF_BASE = 10.0
_CLEANUP_BACKOFF_MAX = 600.0
_local_locks: dict[str, asyncio.Lock] = {}


class DaemonTicketWorktreeError(RuntimeError):
    """A daemon-host Ticket worktree could not be prepared or removed safely."""


class TicketBranchDivergedError(DaemonTicketWorktreeError):
    """A resumed Ticket branch no longer carries the base the server pinned.

    The message is operator-facing: it names the branch and the fix. Every
    fix it names keeps the branch's commits (a forced reset onto the base
    would throw the Ticket's work away).
    """

    def __init__(self, ticket_id: str, branch: str, base_sha: str) -> None:
        self.ticket_id = ticket_id
        self.branch = branch
        self.base_sha = base_sha
        super().__init__(
            f"Ticket branch {branch} no longer descends from the pinned base "
            f"{base_sha[:12]}, so this Ticket cannot resume on it. Operator fix, "
            "in the dispatch repository on this daemon host: rebase the branch "
            f"onto the base (git rebase {base_sha} {branch}), then resume the "
            "Ticket; or save its commits under another name first "
            f"(git branch <backup-name> {branch}) and only then delete it "
            f"(git branch -D {branch}) and resume; or close the Ticket and "
            "reopen the work as a new Ticket."
        )


def diverged_branch_question(branch: str) -> str:
    return (
        f"Rebase branch {branch} onto the pinned base in the dispatch repository "
        "on the daemon host (or save its commits under another name before "
        "deleting it, or reopen the work as a new Ticket), then resume this Ticket."
    )


class TicketCheckoutDetachedError(DaemonTicketWorktreeError):
    """The Ticket checkout sits on a detached HEAD instead of its branch.

    Not tampering (HEAD is a plain file naming a commit), but nothing can be
    delivered from it: the operator returns it to the Ticket branch first.
    """

    def __init__(self, ticket_id: str, branch: str, head_sha: str) -> None:
        self.ticket_id = ticket_id
        self.branch = branch
        self.head_sha = head_sha
        super().__init__(
            f"the Ticket checkout is on a detached HEAD at {head_sha[:12]} "
            f"instead of the Ticket branch {branch}"
        )


def detached_head_question(branch: str, head_sha: str) -> str:
    """Operator steps that return a detached checkout to its branch.

    Commits made on the detached HEAD are saved first: ``git switch`` alone
    would leave them reachable only from the reflog.
    """
    return (
        "In the Ticket checkout on the daemon host (git worktree list in the "
        f"dispatch repository shows it), return to the Ticket branch with "
        f"git switch {branch}. To keep commits made on the detached HEAD, save "
        "them before switching (git switch -c <new-name>), then run "
        f"git switch {branch} and git merge <new-name>; or, after switching, "
        f"git merge {head_sha}. Then resume this Ticket."
    )


def detached_head_commit(admin: Path) -> str:
    """The commit a plain detached worktree HEAD names; '' otherwise.

    Only a regular, singly linked HEAD file holding exactly one object id
    counts. A symlinked HEAD or one naming another ref is not a detached
    checkout (``ticket_ref_tamper_reason`` refuses those).
    """
    path = admin / "HEAD"
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            return ""
        head = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return head if _BASE_SHA_RE.fullmatch(head) else ""


def _read_loose_ref(path: Path) -> str | None:
    """A loose ref file's content (stripped), None when absent."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        return None


def _packed_ref_oid(common: Path, refname: str) -> str:
    try:
        metadata = (common / "packed-refs").lstat()
        if not stat.S_ISREG(metadata.st_mode):
            return ""
        text = (common / "packed-refs").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        oid, _, name = line.partition(" ")
        if name.strip() == refname and _BASE_SHA_RE.fullmatch(oid):
            return oid
    return ""


def ticket_ref_oid(common: Path, branch: str) -> str:
    """The object id the Ticket ref names, read from the files, or ''.

    Never resolves a symref: a loose ref that does not hold exactly one
    object id yields '' (``ticket_ref_tamper_reason`` refuses it).
    """
    clean = validate_ticket_branch(branch)
    path = common / "refs" / "heads" / clean
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return _packed_ref_oid(common, f"refs/heads/{clean}")
    except OSError:
        return ""
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        return ""
    try:
        content = _read_loose_ref(path)
    except OSError:
        return ""
    return content if content and _BASE_SHA_RE.fullmatch(content) else ""


def ticket_ref_tamper_reason(
    common: Path,
    admin: Path | None,
    branch: str,
    *,
    allow_detached: bool = False,
    require_ref: bool = False,
) -> str:
    """Why a Worker-touched Ticket ref must not be trusted; '' when it is sound.

    With ``allow_detached`` a plain detached HEAD (a regular file holding an
    object id, see ``detached_head_commit``) is not tampering; the caller
    reports it separately. A HEAD naming any other ref is always refused.

    The sandbox grants the ref, its reflog and the worktree HEAD as exact
    files, but a Worker can still replace a granted name with a symlink (for
    example to ``refs/heads/main``), a directory or a hard link. Host-side Git
    would then follow it. Absent files are fine (a packed or unborn ref).
    """
    clean = validate_ticket_branch(branch)
    ref_path = common / "refs" / "heads" / clean
    checks = [
        ("ref", ref_path),
        ("reflog", common / "logs" / "refs" / "heads" / clean),
    ]
    if admin is not None:
        checks.append(("worktree HEAD", admin / "HEAD"))
    for label, path in checks:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return f"the Ticket branch {label} could not be inspected"
        if stat.S_ISLNK(metadata.st_mode):
            return f"the Ticket branch {label} was replaced by a symlink"
        if stat.S_ISDIR(metadata.st_mode):
            return f"the Ticket branch {label} was replaced by a directory"
        if not stat.S_ISREG(metadata.st_mode):
            return f"the Ticket branch {label} is not a regular file"
        if metadata.st_nlink != 1:
            return f"the Ticket branch {label} is hard-linked to another file"
        if label == "ref":
            # A text symref (``ref: refs/heads/main``) is a regular file too;
            # host Git would follow it to another branch (N68).
            try:
                content = _read_loose_ref(path)
            except OSError:
                return "the Ticket branch ref could not be inspected"
            if content is None or not _BASE_SHA_RE.fullmatch(content):
                return "the Ticket branch ref does not hold a plain object id"
    if require_ref and not ticket_ref_oid(common, clean):
        # Deleted (or never written) after the run: nothing can be delivered,
        # and a recreated ref would not be the Worker's (N68).
        return "the Ticket branch ref is missing"
    if admin is not None:
        try:
            head = (admin / "HEAD").read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return "the worktree HEAD is missing"
        except OSError:
            return "the worktree HEAD could not be inspected"
        if head.strip() != f"ref: refs/heads/{clean}" and not (
            allow_detached and detached_head_commit(admin)
        ):
            return "the worktree HEAD no longer points at the Ticket branch"
    return ""


@dataclass(frozen=True)
class HostTicketWorktree:
    """One Ticket checkout on this daemon host."""

    ticket_id: str
    path: Path
    branch: str
    base_branch: str
    base_sha: str
    # True when this call cut the checkout; False when it reused one (resume).
    created: bool = False


def validate_ticket_branch(branch: str, *, base_branch: str = "") -> str:
    """Accept only a plain, non-protected branch name the server provided.

    The name reaches ``git worktree add -b``; anything that could be read as an
    option, a revision expression or a protected line is refused.
    """
    clean = str(branch or "").strip()
    short_base = str(base_branch or "").strip().removeprefix("origin/")
    if (
        not clean
        or not clean.startswith(TICKET_BRANCH_PREFIX)
        or len(clean) == len(TICKET_BRANCH_PREFIX)
        or not _SAFE_REF_RE.fullmatch(clean)
        or "@{" in clean
        or ".." in clean
        or "//" in clean
        or clean.endswith(("/", ".", ".lock"))
        or any(
            part.startswith(".") or part.endswith(".lock")
            for part in clean.split("/")
        )
        or clean.startswith("refs/")
        or clean in _PROTECTED_BRANCHES
        or (short_base and clean == short_base)
    ):
        raise DaemonTicketWorktreeError("ticket worktree branch is not a safe ref")
    return clean


def validate_base_sha(base_sha: str) -> str:
    clean = str(base_sha or "").strip().lower()
    if not _BASE_SHA_RE.fullmatch(clean):
        raise DaemonTicketWorktreeError("ticket worktree base_sha is not a full object id")
    return clean


def resolve_repo_root(explicit: str | Path | None = None) -> Path | None:
    raw = (
        str(explicit).strip()
        if explicit is not None
        else os.environ.get("CONDUCTOR_DISPATCH_REPO", "").strip()
    )
    if not raw:
        return None
    try:
        path = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DaemonTicketWorktreeError(
            "dispatch repository is not a usable directory"
        ) from exc
    if not path.is_dir():
        raise DaemonTicketWorktreeError("dispatch repository is not a usable directory")
    return path


def resolve_worktrees_root(explicit: str | Path) -> Path:
    path = Path(str(explicit)).expanduser()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path.resolve()


def ticket_worktree_path(
    worktrees_root: Path, repo: Path, ticket_id: str, branch: str
) -> Path:
    repo_key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:24]
    ticket_key = hashlib.sha256(str(ticket_id).encode()).hexdigest()[:24]
    branch_key = hashlib.sha256(str(branch).encode()).hexdigest()[:24]
    return worktrees_root.resolve() / repo_key / "tickets" / ticket_key / branch_key


@asynccontextmanager
async def host_ticket_lock(ticket_id: str) -> AsyncIterator[None]:
    """Serialize prepare+bind and removal for one Ticket on this host."""
    clean = str(ticket_id or "").strip()
    if not clean:
        yield
        return
    lock = _local_locks.setdefault(clean, asyncio.Lock())
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()


def _record_name(ticket_id: str) -> str:
    # Ticket ids come from the server; never let one name a path component.
    return hashlib.sha256(str(ticket_id).encode()).hexdigest()[:32] + ".json"


def _private_dir(worktrees_root: str | Path, name: str) -> Path:
    path = resolve_worktrees_root(worktrees_root) / name
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private_json(target: Path, payload: Mapping[str, Any]) -> None:
    """Atomically replace ``target``; durable once this returns.

    The record fences a removal across a crash, so the bytes and the rename
    are both flushed (file, then directory) before the caller acts on it.
    """
    tmp = target.with_suffix(".tmp")
    data = json.dumps(dict(payload)).encode("utf-8")
    descriptor = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(tmp, target)
    _fsync_directory(target.parent)


def _unlink_durably(target: Path) -> None:
    try:
        target.unlink(missing_ok=True)
        _fsync_directory(target.parent)
    except OSError:
        pass


def _read_json(target: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


# Host ownership: the generation the server returned for this host's bind.


def ownership_path(worktrees_root: str | Path, ticket_id: str) -> Path:
    return _private_dir(worktrees_root, ".ticket-worktree-owners") / _record_name(
        ticket_id
    )


def read_host_ownership(
    worktrees_root: str | Path, ticket_id: str
) -> dict[str, Any] | None:
    return _read_json(ownership_path(worktrees_root, ticket_id))


def write_host_ownership(
    worktrees_root: str | Path,
    *,
    ticket_id: str,
    worktree_path: str,
    lease_id: str,
    generation: int,
) -> dict[str, Any]:
    payload = {
        "ticket_id": ticket_id,
        "worktree_path": str(Path(worktree_path).expanduser().resolve()),
        "lease_id": str(lease_id or ""),
        "generation": int(generation),
    }
    _write_private_json(ownership_path(worktrees_root, ticket_id), payload)
    return payload


def clear_host_ownership(
    worktrees_root: str | Path, ticket_id: str, *, generation: int
) -> None:
    """Drop the ownership record only while it still names ``generation``."""
    current = read_host_ownership(worktrees_root, ticket_id)
    if current is not None and _as_int(current.get("generation")) != int(generation):
        return
    _unlink_durably(ownership_path(worktrees_root, ticket_id))


def cleanup_superseded(
    ownership: Mapping[str, Any] | None, *, worktree_path: str, generation: int
) -> bool:
    """True when a newer bind (or another checkout) owns this Ticket now."""
    if ownership is None:
        return False
    if _as_int(ownership.get("generation")) > int(generation):
        return True
    return str(ownership.get("worktree_path") or "") != str(
        Path(worktree_path).expanduser().resolve()
    )


# Cleanup journal: written only after the server accepted the ending.


def journal_path(
    worktrees_root: str | Path, ticket_id: str, generation: int
) -> Path:
    # Keyed by Ticket and generation: each bound attempt owes its own report.
    key = hashlib.sha256(f"{ticket_id}\0{int(generation)}".encode()).hexdigest()
    return _private_dir(worktrees_root, ".ticket-worktree-cleanup") / (
        key[:32] + ".json"
    )


def write_cleanup_journal(
    worktrees_root: str | Path,
    *,
    ticket_id: str,
    worktree_path: str,
    worktree_generation: int,
    lease_id: str,
    attempts: int = 0,
    next_attempt_at: float = 0.0,
    processes: Any = (),
    branch: str = "",
) -> dict[str, Any]:
    payload = {
        "ticket_id": ticket_id,
        "worktree_path": worktree_path,
        "worktree_generation": int(worktree_generation),
        "lease_id": str(lease_id or ""),
        "attempts": int(attempts),
        "next_attempt_at": float(next_attempt_at),
        # The Worker processes (pid, pgid) a deferred removal waits for, so a
        # later drain (which has no tracker) never removes under a survivor.
        "processes": journal_processes(processes),
        # The Ticket branch, so a removal can tell whether a detached HEAD's
        # commits are on it ('' for entries written before it was recorded).
        "branch": str(branch or ""),
    }
    _write_private_json(
        journal_path(worktrees_root, ticket_id, worktree_generation), payload
    )
    return payload


def defer_cleanup_journal(
    worktrees_root: str | Path, entry: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    attempts = _as_int(entry.get("attempts")) + 1
    delay = min(_CLEANUP_BACKOFF_MAX, _CLEANUP_BACKOFF_BASE * (2 ** min(attempts - 1, 8)))
    return write_cleanup_journal(
        worktrees_root,
        ticket_id=str(entry["ticket_id"]),
        worktree_path=str(entry["worktree_path"]),
        worktree_generation=_as_int(entry.get("worktree_generation")),
        lease_id=str(entry.get("lease_id") or ""),
        attempts=attempts,
        next_attempt_at=(time.time() if now is None else now) + delay,
        processes=entry.get("processes") or (),
        branch=str(entry.get("branch") or ""),
    )


def journal_processes(processes: Any) -> list[dict[str, Any]]:
    """Normalize ``[{pid, pgid, exited}, ...]`` (tracker evidence) for the journal."""
    rows: list[dict[str, Any]] = []
    if not isinstance(processes, (list, tuple)):
        return rows
    for item in processes:
        if not isinstance(item, Mapping):
            continue
        pid = _as_int(item.get("pid"))
        pgid = _as_int(item.get("pgid")) if item.get("pgid") is not None else None
        if pid <= 1:
            continue
        rows.append(
            {
                "pid": pid,
                "pgid": pgid if pgid and pgid > 1 else None,
                "exited": bool(item.get("exited")),
            }
        )
    return rows


def _signal_target_alive(send: Any, target: int) -> bool:
    try:
        send(target, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # Exists but not ours to signal (or unknown): treat as alive.
        return True
    return True


def journal_processes_alive(entry: Mapping[str, Any]) -> bool:
    """Whether any Worker process (or its group) a journal entry names is alive.

    A reaped leader's pid can be reissued, so only its group is checked; a
    group id is not reissued while the group has a member.
    """
    for row in journal_processes(entry.get("processes")):
        if not row["exited"] and _signal_target_alive(os.kill, int(row["pid"])):
            return True
        pgid = row.get("pgid")
        if pgid and os.name == "posix" and pgid != os.getpgrp():
            if _signal_target_alive(os.killpg, int(pgid)):
                return True
    return False


def drop_cleanup_journal(
    worktrees_root: str | Path, ticket_id: str, *, generation: int
) -> None:
    _unlink_durably(journal_path(worktrees_root, ticket_id, generation))


def read_cleanup_journal(
    worktrees_root: str | Path, ticket_id: str, generation: int
) -> dict[str, Any] | None:
    payload = _read_json(journal_path(worktrees_root, ticket_id, generation))
    if (
        payload is None
        or str(payload.get("ticket_id") or "") != str(ticket_id)
        or _as_int(payload.get("worktree_generation")) != int(generation)
    ):
        return None
    return payload


def list_cleanup_journal(worktrees_root: str | Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(
        _private_dir(worktrees_root, ".ticket-worktree-cleanup").glob("*.json")
    ):
        payload = _read_json(path)
        if (
            payload is None
            or not str(payload.get("ticket_id") or "").strip()
            or not str(payload.get("worktree_path") or "").strip()
            or _as_int(payload.get("worktree_generation")) < 1
        ):
            continue
        entries.append(payload)
    return entries


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def base_sha_path(worktree: Path) -> Path:
    return Path(str(worktree) + ".base_sha")


def write_base_sha(worktree: Path, base_sha: str) -> None:
    path = base_sha_path(worktree)
    path.write_text(base_sha.strip() + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def read_base_sha(worktree: Path) -> str:
    try:
        return base_sha_path(worktree).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def clear_base_sha(worktree: Path) -> None:
    try:
        base_sha_path(worktree).unlink()
    except FileNotFoundError:
        pass
