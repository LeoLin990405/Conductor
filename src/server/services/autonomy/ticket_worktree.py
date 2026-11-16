"""Per-Ticket linked worktrees: one durable checkout per autonomy Ticket.

The binding (path, branch, repo, host, generation) lives in the Ticket's
``worktree_*`` columns and is written only through the fenced persistence
helpers in :mod:`src.server.database.autonomy.tickets`; the mandate JSON never
carries it, so a whole-mandate write cannot drop or resurrect a checkout.

``worktree_base_sha`` is the durable pin: resolved once on the server, written
first-write-wins, and reused by every later create (including the recovery of
a vanished checkout). ``worktree_generation`` advances on every successful bind
and is the attempt every cleanup fences on.

Server-host checkouts are created and removed here, under
``tickets.ticket_worktree_lock`` so bind and remove+clear never interleave
across server workers. Daemon-host checkouts are never touched by the server:
the daemon creates them on its own host from :func:`daemon_claim_worktree_spec`
and reports cleanup itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import signal
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.config.models import conductor_worktree_root
from src.server.database.autonomy import tickets as ticket_db
from src.tools.auto_dispatch.execution import (
    _archive_dirty_worktree,
    _ensure_owned_directory,
    _registered_worktree,
)
from src.tools.auto_dispatch.protocols import LaunchError
from src.utils.system_paths import canonical_system_path

logger = logging.getLogger(__name__)

_TICKET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_BRANCH_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
DEFAULT_BASE_BRANCH = "origin/main"
HOST_SERVER = "server"
_TICKET_BRANCH_PREFIX = "conductor/ticket/"
HOST_DAEMON = "daemon"
# Statuses that may still own a live checkout (mirrors tickets.OPEN_STATUSES;
# read through the module at call time when it is available).
_OPEN_STATUSES = ("queued", "running", "waiting", "blocked", "awaiting_clarification")
# A Docker run renews its heartbeat every 60 s; a younger beat means the
# container may still be writing into the checkout.
_ISOLATED_HEARTBEAT_LIVE_SECONDS = 180.0
_CLEANUP_BACKOFF_BASE_SECONDS = 30.0
_CLEANUP_BACKOFF_MAX_SECONDS = 3600.0
_RUNNER_WAIT_SECONDS = 30.0
# Removal waits this long for the processes in a checkout to exit before it
# leaves the checkout pending for the retry sweep.
_PROCESS_GONE_GRACE_SECONDS = 5.0
_PROCESS_POLL_SECONDS = 0.2
_PROCESS_PROBE_TIMEOUT_SECONDS = 15.0
_LSOF_CANDIDATES = ("/usr/sbin/lsof", "/usr/bin/lsof", "/sbin/lsof", "/bin/lsof")
_PROC_ROOT = Path("/proc")
# Every server Git call is bounded: a hung Git (a stuck lock, a slow or wedged
# filesystem) is killed and reported as the server being unable to run Git, so
# a claim or a launch never hangs on it. Well under the daemon's 30 s claim
# request timeout, so a claim that builds a worktree spec answers (503) before
# the daemon gives up on it.
_GIT_TIMEOUT_SECONDS = 20.0
# ``worktree add`` checks the whole tree out and runs only on the launch
# path (never inside a claim request), so a large or slow repository gets
# longer before the add is undone and retried (N68).
_GIT_ADD_TIMEOUT_SECONDS = 300.0


class TicketWorktreeError(RuntimeError):
    """A Ticket worktree could not be created, reused, or removed safely."""


class TicketWorktreeBaseError(TicketWorktreeError):
    """This Ticket's base cannot be resolved: its own ref or repository is bad.

    Per-Ticket and permanent until someone changes the Ticket (or its branch):
    retrying the same Ticket gives the same answer, so a caller may fail it.
    """


class TicketCheckoutDetached(TicketWorktreeError):
    """The Ticket checkout sits on a detached HEAD instead of its branch.

    Its commits may be reachable from that HEAD only, so the checkout is kept
    (never moved aside or pruned) until an operator returns it to the branch;
    this matches the daemon's ``TicketCheckoutDetachedError`` (N68).
    """

    def __init__(self, ticket_id: str, branch: str, head_sha: str) -> None:
        self.ticket_id = ticket_id
        self.branch = branch
        self.head_sha = head_sha
        super().__init__(
            f"the Ticket checkout is on a detached HEAD at {head_sha[:12]} instead "
            f"of the Ticket branch {branch}; it is kept. Operator fix on the server: "
            f"save the detached commits (git switch -c <new-name>), then git switch "
            f"{branch} and git merge <new-name>; then resume this Ticket."
        )


class TicketWorktreeUnavailable(TicketWorktreeError):
    """The server cannot resolve any Ticket base right now.

    Server-wide and transient: the configured dispatch repository is missing
    or unreadable, Git is missing or cannot start, or Git timed out. The
    Ticket itself is fine, so a caller must not fail it (retry later instead).
    """


@dataclass(frozen=True, slots=True)
class TicketWorktreeRecord:
    """Frozen handle for one Ticket's linked worktree."""

    path: Path
    branch: str
    base_sha: str
    repo: Path


def ticket_branch(ticket_id: str) -> str:
    """Branch name for a Ticket worktree under ``conductor/ticket/<id>``.

    Unsafe characters are replaced so the name stays a valid Git operand.
    """
    raw = str(ticket_id or "").strip()
    if not raw or not _TICKET_ID_RE.fullmatch(raw):
        raise TicketWorktreeError("ticket_id is missing or unsafe")
    candidate = raw.replace(":", "-")
    if (
        _BRANCH_SAFE_RE.fullmatch(candidate)
        and "//" not in candidate
        and not candidate.endswith(("/", ".", ".lock"))
    ):
        label = candidate
    else:
        label = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return f"conductor/ticket/{label}"


_GITFILE_MAX_BYTES = 4096


def _git_binary() -> str:
    from src.server.services.autonomy.git_delivery import GitDeliveryError
    from src.server.services.autonomy.git_delivery import _git_binary as binary

    try:
        return binary()
    except GitDeliveryError as exc:
        raise TicketWorktreeUnavailable("git is unavailable on the server") from exc


_UNSAFE_FILTER = "ticket worktree has an unsafe filter driver name"


def _hardened_command(
    executable: str, args: list[str], filters: tuple[str, ...] = ()
) -> tuple[tuple[str, ...], dict[str, str]]:
    """git_delivery's shared hardened argv/env, plus the checkout-only addition.

    ``hardened_git_invocation`` assembles the pinned ``-c`` prefix (no
    fsmonitor, no hooks, no gc/maintenance child, fixed comparison settings)
    and the scrubbed environment (no inherited ``GIT_*`` state, no system or
    global config) with every named filter driver neutralised. The one thing
    it does not carry is ``GIT_LFS_SKIP_SMUDGE``: only a checkout (``worktree
    add``) runs a smudge filter, and the shared builder serves read paths and
    the Docker probe whose environment check accepts no extra key, so it is
    added here.
    """
    from src.server.services.autonomy.git_delivery import (
        GitDeliveryError,
        GitDrivers,
        hardened_git_invocation,
    )

    try:
        argv, env = hardened_git_invocation(
            executable,
            args,
            drivers=GitDrivers(filters=tuple(filters)),
            unsafe_message=_UNSAFE_FILTER,
        )
    except GitDeliveryError as exc:
        raise TicketWorktreeError(_UNSAFE_FILTER) from exc
    env = dict(env)
    # Git LFS's smudge filter (configured or not) never downloads or runs.
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    return argv, env


def _git_env(filters: tuple[str, ...] = ()) -> dict[str, str]:
    """The environment every Ticket worktree Git call runs with."""
    return _hardened_command("git", [], filters)[1]


async def _run_git(
    argv: list[str],
    *,
    git_dir: Path | None,
    cwd: Path,
    work_tree: Path | None = None,
    filters: tuple[str, ...] = (),
    timeout: float | None = None,
) -> tuple[int, str, str]:
    """One hardened Git invocation with an explicit ``--git-dir``.

    ``git_dir`` is always derived from the server's own repository (its common
    dir, or ``<common>/worktrees/<name>``), never discovered from a checkout's
    worker-writable ``.git`` file. ``git_dir=None`` is only for discovering the
    server repository's own common dir.
    """
    args: list[str] = []
    if git_dir is not None:
        args.append(f"--git-dir={git_dir}")
    if work_tree is not None:
        args.append(f"--work-tree={work_tree}")
    args.extend(argv)
    command, env = _hardened_command(_git_binary(), args, filters)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            # Own process group, so a timeout also kills anything Git started.
            start_new_session=True,
        )
    except OSError as exc:
        raise TicketWorktreeUnavailable("git could not be started on the server") from exc
    limit = _GIT_TIMEOUT_SECONDS if timeout is None else float(timeout)
    try:
        out, err = await asyncio.wait_for(process.communicate(), limit)
    except BaseException as exc:
        _kill_process_group(process)
        try:
            await asyncio.shield(process.wait())
        except BaseException:  # noqa: BLE001 - the original error is what matters
            pass
        if isinstance(exc, TimeoutError):
            raise TicketWorktreeUnavailable(
                f"git timed out on the server after {limit:g} s"
            ) from None
        raise
    return (
        process.returncode or 0,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


async def _common_dir(repo: Path) -> Path:
    """The server repository's common Git dir (``.git`` for a plain clone)."""
    dot_git = repo / ".git"
    if dot_git.is_dir() and not dot_git.is_symlink():
        return dot_git.resolve()
    # The server's own repository is itself a linked worktree: ask Git once,
    # still hardened. This repository is server-owned, not a Ticket checkout.
    code, out, err = await _run_git(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
        git_dir=None,
        cwd=repo,
    )
    common = Path(out.strip()) if code == 0 and out.strip() else None
    if common is None or not common.is_dir():
        # Git's own text stays in the log: the message can reach a Ticket.
        logger.warning(
            "[autonomy] could not resolve the git dir of %s: %s",
            repo,
            (err.strip() or out.strip())[:500],
        )
        raise TicketWorktreeError("could not resolve the repository's git dir")
    return common.resolve()


def _read_small(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path.name} is not a regular file")
        data = os.read(fd, _GITFILE_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > _GITFILE_MAX_BYTES:
        raise OSError(f"{path.name} is too large")
    return data.decode("utf-8", "strict").strip()


def _admin_dir(common: Path, worktree: Path) -> Path | None:
    """``<common>/worktrees/<name>`` registered for ``worktree``, or None.

    Resolved from the server repository's side only: the admin dir's
    ``gitdir`` back link must name ``<worktree>/.git`` and its ``commondir``
    must lead back to ``common``. The checkout's own ``.git`` file is never
    read, so a Worker that rewrites it cannot redirect the server's Git.
    """
    base = common / "worktrees"
    if not base.is_dir():
        return None
    preferred = base / worktree.name
    try:
        others = sorted(entry for entry in base.iterdir() if entry != preferred)
    except OSError:
        return None
    expected = worktree.resolve()
    for candidate in (preferred, *others):
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        try:
            backlink = Path(_read_small(candidate / "gitdir"))
            if not backlink.is_absolute():
                backlink = candidate / backlink
            if backlink.name != ".git" or backlink.parent.resolve() != expected:
                continue
            raw_common = Path(_read_small(candidate / "commondir"))
            linked = (
                raw_common if raw_common.is_absolute() else candidate / raw_common
            ).resolve()
        except (OSError, UnicodeError, ValueError):
            continue
        if linked == common:
            return candidate
    return None


def _restore_gitfile(worktree: Path, admin: Path) -> None:
    """Point ``<worktree>/.git`` back at its admin dir before ``worktree remove``.

    ``git worktree remove`` refuses a checkout whose ``.git`` no longer points
    back; a Worker could otherwise wedge its cleanup by rewriting it. Only
    called once the Worker is confirmed gone.
    """
    pointer = worktree / ".git"
    try:
        current = _read_small(pointer)
    except (OSError, UnicodeError):
        current = ""
    if current.startswith("gitdir: "):
        target = Path(current[len("gitdir: ") :])
        if not target.is_absolute():
            target = worktree / target
        if target.resolve() == admin.resolve():
            return
    try:
        info = pointer.lstat()
    except FileNotFoundError:
        info = None
    if info is not None:
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(pointer)
        else:
            pointer.unlink()
    fd = os.open(
        pointer, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644
    )
    try:
        os.write(fd, f"gitdir: {admin}\n".encode())
    finally:
        os.close(fd)


@dataclass(frozen=True, slots=True)
class _RepoGit:
    """Hardened Git bound to the server repository's common dir."""

    repo: Path
    common: Path

    async def __call__(
        self,
        argv: list[str],
        cwd: Path | None = None,
        *,
        filters: tuple[str, ...] = (),
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        return await _run_git(
            argv, git_dir=self.common, cwd=self.repo, filters=filters, timeout=timeout
        )


@dataclass(frozen=True, slots=True)
class _CheckoutGit:
    """Hardened Git bound to one checkout's server-resolved admin dir."""

    admin: Path
    worktree: Path
    filters: tuple[str, ...] = ()

    async def __call__(
        self, argv: list[str], cwd: Path | None = None
    ) -> tuple[int, str, str]:
        return await _run_git(
            argv,
            git_dir=self.admin,
            work_tree=self.worktree,
            cwd=self.worktree,
            filters=self.filters,
        )


async def _repo_git(repo: Path) -> _RepoGit:
    return _RepoGit(repo=repo, common=await _common_dir(repo))


async def _checkout_git(repo_git: _RepoGit, worktree: Path) -> _CheckoutGit:
    """Bind Git to ``worktree`` through the server's registration, or raise."""
    admin = _admin_dir(repo_git.common, worktree)
    if admin is None:
        raise TicketWorktreeError("ticket worktree is not registered in its repository")
    git = _CheckoutGit(admin=admin, worktree=worktree)
    return _CheckoutGit(
        admin=admin, worktree=worktree, filters=await _filter_names(git)
    )


async def _filter_names(git: _CheckoutGit) -> tuple[str, ...]:
    """Every filter driver the checkout's effective config defines.

    Reading config (includes and ``config.worktree`` too) executes nothing;
    the names are then overridden so no driver runs.
    """
    return await _config_filter_names(
        git_dir=git.admin, work_tree=git.worktree, cwd=git.worktree
    )


async def _repo_filter_names(git: _RepoGit) -> tuple[str, ...]:
    """Every filter driver the server repository's own config defines."""
    return await _config_filter_names(git_dir=git.common, work_tree=None, cwd=git.repo)


async def _config_filter_names(
    *, git_dir: Path, work_tree: Path | None, cwd: Path
) -> tuple[str, ...]:
    code, out, err = await _run_git(
        ["config", "--includes", "--null", "--name-only", "--get-regexp", r"^filter\."],
        git_dir=git_dir,
        work_tree=work_tree,
        cwd=cwd,
    )
    if code not in (0, 1):
        logger.warning(
            "[autonomy] could not read the ticket worktree config: %s", err.strip()[:500]
        )
        raise TicketWorktreeError("could not read the ticket worktree config")
    names: set[str] = set()
    for key in out.split("\0"):
        if key.lower().startswith("filter."):
            name, separator, _setting = key[len("filter.") :].rpartition(".")
            if separator and name:
                names.add(name)
    return tuple(sorted(names))


def _worktree_root(root: Path | str | None = None) -> Path:
    raw = root if root is not None else conductor_worktree_root()
    return Path(raw).expanduser().resolve()


def ticket_worktree_path(root: Path, repo: Path, ticket_id: str) -> Path:
    """Deterministic server path for one Ticket's checkout of ``repo``."""
    repo_key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:24]
    ticket_key = hashlib.sha256(ticket_id.encode()).hexdigest()[:24]
    return root / repo_key / "tickets" / ticket_key


async def resolve_base_sha(
    repo: Path, base_ref: str = DEFAULT_BASE_BRANCH, *, server_wide: bool = False
) -> str:
    """Resolve ``base_ref`` to a commit, falling back from ``origin/main`` to ``main``.

    A ref that does not resolve is this Ticket's problem
    (``TicketWorktreeBaseError``). An unreadable repository is too, unless it
    is the server-wide dispatch repository (``server_wide``), which no Ticket
    can fix (``TicketWorktreeUnavailable``); so is that repository lacking
    the default base, which every Ticket without its own base would hit. Git
    missing or timing out is always ``TicketWorktreeUnavailable``. Raw Git error text is logged, never
    put in the exception, which callers may relay to a Ticket or its Source.
    """
    ref = str(base_ref or DEFAULT_BASE_BRANCH).strip() or DEFAULT_BASE_BRANCH
    try:
        git = await _repo_git(Path(repo).expanduser().resolve())
    except TicketWorktreeUnavailable:
        raise
    except TicketWorktreeError as exc:
        logger.warning("[autonomy] ticket base repository unreadable: %s", exc)
        if server_wide:
            raise TicketWorktreeUnavailable(
                "the server's dispatch repository is unreadable"
            ) from exc
        raise TicketWorktreeBaseError(
            "the Ticket's repository could not be read"
        ) from exc
    if ref.startswith("-") or any(ch.isspace() or ord(ch) < 0x20 for ch in ref):
        raise TicketWorktreeBaseError(f"base ref {ref!r} is not a valid ref")
    code, out, err = await git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    if code == 0 and out.strip():
        return out.strip()
    if ref == DEFAULT_BASE_BRANCH:
        code, out, err = await git(["rev-parse", "--verify", "--quiet", "main^{commit}"])
        if code == 0 and out.strip():
            return out.strip()
    logger.warning(
        "[autonomy] could not resolve ticket base ref %r: %s", ref, err.strip()[:500]
    )
    if server_wide and ref == DEFAULT_BASE_BRANCH:
        raise TicketWorktreeUnavailable(
            f"the server's dispatch repository has no {ref!r} (or 'main') to base Tickets on"
        )
    raise TicketWorktreeBaseError(f"could not resolve base ref {ref!r}")


async def _branch_tip(git: _RepoGit, branch: str) -> str | None:
    code, out, _ = await git(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]
    )
    return out.strip() if code == 0 and out.strip() else None


async def _descends_from(git: _RepoGit, base: str, tip: str) -> bool:
    code, _, _ = await git(["merge-base", "--is-ancestor", base, tip])
    return code == 0


async def ensure_ticket_worktree(
    repo: Path | str,
    ticket_id: str,
    *,
    base_sha: str,
    root: Path | str | None = None,
) -> TicketWorktreeRecord:
    """Create or reuse the linked worktree for ``ticket_id`` at ``base_sha``.

    ``base_sha`` is the Ticket's durable pin; it is never re-resolved here. An
    existing registration on the Ticket branch is reused as-is (its HEAD may be
    ahead of the base with the Worker's own commits). An absent checkout is
    recreated from the pin; when the Ticket branch still exists (removal keeps
    it, so a completed attempt's commits survive) it is attached only when its
    tip is the pin or descends from it. Any other tip is rejected rather than
    attaching unrelated history; the pin is never redefined from a tip.
    """
    pinned = str(base_sha or "").strip()
    if not pinned or any(ch.isspace() for ch in pinned):
        raise TicketWorktreeError("ticket worktree requires a durable base_sha")
    repo_path = Path(repo).expanduser().resolve()
    if not (repo_path / ".git").exists():
        raise TicketWorktreeError("repo is not a git checkout")
    clean_id = str(ticket_id or "").strip()
    branch = ticket_branch(clean_id)
    worktree_root = _worktree_root(root)
    if worktree_root == repo_path or repo_path in worktree_root.parents:
        raise TicketWorktreeError("ticket worktree root must be outside repo")
    worktree = ticket_worktree_path(worktree_root, repo_path, clean_id)
    try:
        _ensure_owned_directory(
            worktree_root, worktree.parent.relative_to(worktree_root)
        )
    except LaunchError as exc:
        raise TicketWorktreeError(str(exc)) from exc
    git = await _repo_git(repo_path)

    if worktree.exists() or worktree.is_symlink():
        state = await _existing_checkout_state(git, worktree, branch)
        if state == _CHECKOUT_OK:
            return TicketWorktreeRecord(
                path=worktree, branch=branch, base_sha=pinned, repo=repo_path
            )
        if state == _CHECKOUT_FOREIGN:
            raise TicketWorktreeError(
                f"existing worktree for ticket {clean_id!r} is not registered to {branch}"
            )
        if state == _CHECKOUT_DETACHED:
            # Never moved aside: the prune after a move would drop the only
            # thing (the admin HEAD) that reaches its commits (N68).
            admin = _admin_dir(git.common, worktree)
            raise TicketCheckoutDetached(
                clean_id, branch, _detached_commit(admin) if admin else ""
            )
        # Half-built (an interrupted ``worktree add``) or failing the integrity
        # check: never reused. It is discarded (a broken one is moved aside,
        # not deleted) and recreated from the pin below; the branch is kept.
        logger.warning(
            "[autonomy] discarding %s ticket worktree ticket=%s path=%s",
            state,
            clean_id,
            worktree,
        )
        await _discard_checkout(
            git,
            worktree,
            preserve=state in (_CHECKOUT_BROKEN, _CHECKOUT_OTHER_TICKET),
        )
        if worktree.exists() or worktree.is_symlink():
            raise TicketWorktreeError(
                f"could not clear the unusable worktree for ticket {clean_id!r}"
            )

    await git(["worktree", "prune"])
    tip = await _branch_tip(git, branch)
    if tip is None:
        argv = ["worktree", "add", "-b", branch, str(worktree), pinned]
        expected_head = pinned
    elif tip == pinned or await _descends_from(git, pinned, tip):
        argv = ["worktree", "add", str(worktree), branch]
        expected_head = tip
    else:
        raise TicketWorktreeBaseError(
            f"Ticket branch {branch} no longer descends from the Ticket's base "
            f"{pinned} (its tip is {tip}), so the Ticket cannot resume on it. "
            f"An operator can rebase {branch} onto {pinned[:12]} so it descends "
            "from the base; or first save its commits elsewhere (for example "
            f"git branch <backup-name> {branch}) and only then delete it "
            f"(git branch -D {branch}) to start again from the base; or reopen "
            "the work as a new Ticket."
        )
    # ``worktree add`` checks files out: neutralise every filter driver the
    # server repository's config defines, so a Worker-committed
    # .gitattributes on the kept branch cannot make the server run a smudge
    # (or LFS) filter.
    filters = await _repo_filter_names(git)
    try:
        code, _, err = await git(argv, filters=filters, timeout=_GIT_ADD_TIMEOUT_SECONDS)
    except TicketWorktreeError:
        # A timeout (or a kill) mid-add leaves a checkout locked
        # "initializing" with part of the tree written: undo it now so no
        # later ensure can reuse it.
        await _undo_failed_add(
            git, worktree, branch, pinned, created_branch=tip is None
        )
        raise
    if code != 0:
        logger.warning(
            "[autonomy] git worktree add failed ticket=%s: %s",
            clean_id,
            err.strip()[:500],
        )
        await _undo_failed_add(
            git, worktree, branch, pinned, created_branch=tip is None
        )
        raise TicketWorktreeError(f"could not create worktree for ticket {clean_id!r}")
    try:
        checkout = await _checkout_git(git, worktree)
        code, head, _head_err = await checkout(["rev-parse", "HEAD"])
    except TicketWorktreeUnavailable:
        await _undo_failed_add(
            git, worktree, branch, pinned, created_branch=tip is None
        )
        raise
    except TicketWorktreeError:
        code, head = 1, ""
    if code != 0 or head.strip() != expected_head:
        await _undo_failed_add(
            git, worktree, branch, pinned, created_branch=tip is None
        )
        raise TicketWorktreeError(
            f"ticket worktree HEAD {head.strip()[:40]!r} does not match {expected_head}"
        )
    return TicketWorktreeRecord(
        path=worktree, branch=branch, base_sha=pinned, repo=repo_path
    )


_CHECKOUT_OK = "ok"
_CHECKOUT_FOREIGN = "foreign"
_CHECKOUT_HALF_BUILT = "half-built"
_CHECKOUT_BROKEN = "broken"
# On a detached HEAD: kept for the operator, never discarded.
_CHECKOUT_DETACHED = "detached"
# This Ticket's own path, but HEAD was switched to another Ticket's branch: it
# is moved aside so that branch is free for its own Ticket again (N68).
_CHECKOUT_OTHER_TICKET = "other-ticket"
_OID_TEXT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _detached_commit(admin: Path) -> str:
    """The commit a plain detached admin HEAD names; '' otherwise."""
    path = admin / "HEAD"
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            return ""
        head = _read_small(path)
    except (OSError, UnicodeError):
        return ""
    head = head.strip()
    return head if _OID_TEXT_RE.fullmatch(head) else ""


def _is_managed_checkout_path(path: Path, repo: Path) -> bool:
    """Whether ``path`` has the shape of a server Ticket checkout of ``repo``.

    ``{root}/{sha(repo)[:24]}/tickets/{sha(ticket)[:24]}`` with no symlinked
    component: only such a path is ever moved aside on removal, so a stale
    or bad record cannot send a user directory into recovery-artifacts.
    """
    raw = canonical_system_path(path.expanduser())
    if not raw.is_absolute():
        return False
    try:
        if any(part.is_symlink() for part in (raw, *raw.parents)):
            return False
    except OSError:
        return False
    repo_key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:24]
    return (
        raw.parent.name == "tickets"
        and raw.parent.parent.name == repo_key
        and re.fullmatch(r"[0-9a-f]{24}", raw.name) is not None
    )


def _gitfile_present(worktree: Path) -> bool:
    """The checkout still has its ``.git`` file (a removal killed after
    unlinking it leaves a tree Git no longer treats as a checkout)."""
    try:
        return stat.S_ISREG((worktree / ".git").lstat().st_mode)
    except OSError:
        return False
# The lock reason ``git worktree add`` holds until its checkout is complete.
_INITIALIZING_LOCK = "initializing"


def _lock_reason(admin: Path) -> str | None:
    """The ``locked`` reason of a registered checkout, or None when unlocked."""
    lock = admin / "locked"
    if not (lock.exists() or lock.is_symlink()):
        return None
    try:
        return _read_small(lock)
    except (OSError, UnicodeError):
        return ""


async def _registered_branch(git: _RepoGit, worktree: Path) -> str | None:
    """The ``branch`` line ``worktree list`` reports for ``worktree`` ('' if none).

    None when the path is not in the list at all.
    """
    code, output, _ = await git(["worktree", "list", "--porcelain"])
    if code != 0:
        return None
    expected = f"worktree {worktree.resolve()}"
    for block in output.strip().split("\n\n"):
        lines = block.splitlines()
        if expected in lines:
            for line in lines:
                if line.startswith("branch "):
                    return line[len("branch ") :]
            return ""
    return None


async def _existing_checkout_state(git: _RepoGit, worktree: Path, branch: str) -> str:
    """Whether an existing checkout may be reused, and if not, why.

    ``ok``: registered on the Ticket branch, not locked "initializing", HEAD
    resolves, and ``status`` works. ``foreign``: registered to another
    branch (never touched). ``half-built``: an interrupted ``worktree add``
    (locked "initializing"); nothing ever ran in it. ``broken``: anything
    else that fails the check. Git that cannot run (timeout, missing) raises
    ``TicketWorktreeUnavailable`` instead: an unanswered check is not a
    broken checkout, and nothing is discarded on it.
    """
    if worktree.is_symlink() or not worktree.is_dir():
        return _CHECKOUT_BROKEN
    admin = _admin_dir(git.common, worktree)
    if admin is not None and _lock_reason(admin) == _INITIALIZING_LOCK:
        return _CHECKOUT_HALF_BUILT
    registered = await _registered_branch(git, worktree)
    if registered not in (None, "", f"refs/heads/{branch}"):
        if admin is not None and str(registered).startswith(
            f"refs/heads/{_TICKET_BRANCH_PREFIX}"
        ):
            return _CHECKOUT_OTHER_TICKET
        return _CHECKOUT_FOREIGN
    if admin is not None and registered == "" and _detached_commit(admin):
        return _CHECKOUT_DETACHED
    if admin is None or registered != f"refs/heads/{branch}":
        return _CHECKOUT_BROKEN
    if not _gitfile_present(worktree):
        # Half-removed: never reused (moved aside, then recreated).
        return _CHECKOUT_BROKEN
    try:
        checkout = await _checkout_git(git, worktree)
        code, _, _ = await checkout(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"])
        if code != 0:
            return _CHECKOUT_BROKEN
        code, current, _ = await checkout(["branch", "--show-current"])
        if code != 0 or current.strip() != branch:
            return _CHECKOUT_BROKEN
        code, _, _ = await checkout(
            ["status", "--porcelain", "--untracked-files=no", "--ignore-submodules=all"]
        )
        if code != 0:
            return _CHECKOUT_BROKEN
    except TicketWorktreeUnavailable:
        raise
    except TicketWorktreeError:
        return _CHECKOUT_BROKEN
    return _CHECKOUT_OK


async def _best_effort(git: _RepoGit, argv: list[str]) -> None:
    try:
        code, _, err = await git(argv)
    except TicketWorktreeError:
        logger.warning("[autonomy] git %s did not finish", argv[:2], exc_info=True)
        return
    if code != 0:
        logger.info("[autonomy] git %s failed: %s", argv[:2], err.strip()[:300])


def _delete_path(path: Path) -> None:
    """Delete a server-owned checkout path without following a symlink."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


async def _discard_checkout(
    git: _RepoGit, worktree: Path, *, preserve: bool
) -> Path | None:
    """Unlock, remove and prune an unusable checkout at ``worktree``.

    Every Git call uses the server repository's explicit git dir. With
    ``preserve`` the directory is moved into ``recovery-artifacts`` instead
    of deleted, since a Worker may have left uncommitted work in it; a
    half-built checkout (nothing ran in it) is deleted. The Ticket branch is
    never touched here. Returns the recovery path a preserved checkout was
    moved to.
    """
    moved: Path | None = None
    if _admin_dir(git.common, worktree) is not None:
        await _best_effort(git, ["worktree", "unlock", str(worktree)])
    if preserve and worktree.is_dir() and not worktree.is_symlink():
        # worktree = {root}/{repo_key}/tickets/{ticket_key}
        artifact_root = worktree.parents[2] / "recovery-artifacts"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = artifact_root / f"{worktree.name}-unusable-{stamp}"
        try:
            artifact_root.mkdir(parents=True, exist_ok=True)
            os.rename(worktree, target)
            moved = target
            logger.warning(
                "[autonomy] moved an unusable ticket worktree aside to %s", target
            )
        except OSError:
            logger.warning(
                "[autonomy] could not move the unusable ticket worktree aside",
                exc_info=True,
            )
            return None
    else:
        await _best_effort(git, ["worktree", "remove", "--force", "--force", str(worktree)])
        try:
            _delete_path(worktree)
        except OSError:
            logger.warning(
                "[autonomy] could not delete the half-built ticket worktree",
                exc_info=True,
            )
    await _best_effort(git, ["worktree", "prune"])
    return moved


async def _undo_failed_add(
    git: _RepoGit,
    worktree: Path,
    branch: str,
    pinned: str,
    *,
    created_branch: bool,
) -> None:
    """Undo a ``worktree add`` that failed, timed out or produced a wrong HEAD.

    The path did not exist before the add (an existing one goes through the
    integrity check first), so nothing of a Worker's is in it. A branch this
    add created is deleted only while it still points at the pin, so no
    commit is lost. Best effort: whatever is left is caught by the integrity
    check of the next ensure.
    """
    await _discard_checkout(git, worktree, preserve=False)
    if created_branch:
        await _best_effort(git, ["update-ref", "-d", f"refs/heads/{branch}", pinned])


async def remove_ticket_worktree(record: TicketWorktreeRecord) -> Path | None:
    """Archive dirty leftovers and remove the worktree; the branch stays.

    The Ticket branch carries the Worker's commits (the delivery), so removal
    never deletes it; a reopened Ticket reattaches it. Returns the archive path
    when dirty state was preserved, otherwise ``None``. A checkout that is
    already gone counts as removed (the retry of a cleanup that crashed after
    the remove). A path registered to another branch raises. Every Git call
    uses the server-resolved admin dir, never the checkout's ``.git`` file.
    """
    if not isinstance(record, TicketWorktreeRecord):
        raise TicketWorktreeError(
            "remove_ticket_worktree requires a TicketWorktreeRecord"
        )
    repo = Path(record.repo).expanduser().resolve()
    worktree = Path(record.path).expanduser().resolve()
    branch = record.branch
    artifact: Path | None = None

    if not (repo / ".git").exists():
        raise TicketWorktreeError("ticket worktree repo is not a git checkout")
    git = await _repo_git(repo)
    await git(["worktree", "prune"])
    if worktree.exists():
        if not await _registered_worktree(git, repo, worktree, branch):
            admin = _admin_dir(git.common, worktree)
            registered = await _registered_branch(git, worktree)
            if admin is not None and registered == "" and _detached_commit(admin):
                # Keep it: its commits may be reachable from this HEAD only.
                raise TicketCheckoutDetached(
                    branch.removeprefix(_TICKET_BRANCH_PREFIX),
                    branch,
                    _detached_commit(admin),
                )
            movable = _is_managed_checkout_path(Path(record.path), repo)
            if (
                movable
                and registered is None
                and admin is None
                and not worktree.is_symlink()
                and worktree.is_dir()
                and not _gitfile_present(worktree)
            ):
                # A removal killed after ``.git`` was unlinked: the prune above
                # dropped the registration. Move the rest aside (N68) instead
                # of failing every retry until the cleanup cap.
                return await _discard_checkout(git, worktree, preserve=True)
            if movable and admin is not None and str(registered or "").startswith(
                f"refs/heads/{_TICKET_BRANCH_PREFIX}"
            ):
                # HEAD switched to another Ticket's branch: free that branch
                # by moving this checkout aside; no branch is touched (N68).
                return await _discard_checkout(git, worktree, preserve=True)
            raise TicketWorktreeError(
                f"ticket worktree is not registered to branch {branch}"
            )
        checkout = await _checkout_git(git, worktree)
        code, current, err = await checkout(["branch", "--show-current"])
        if code != 0 or current.strip() != branch:
            raise TicketWorktreeError(
                f"ticket worktree branch mismatch: expected {branch}, "
                f"got {current.strip()!r}"
            )
        try:
            _restore_gitfile(worktree, checkout.admin)
        except OSError as exc:
            raise TicketWorktreeError(
                f"could not restore the ticket worktree git file: {exc}"
            ) from exc
        code, dirty, err = await checkout(
            [
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--ignore-submodules=all",
            ]
        )
        if code != 0:
            logger.warning(
                "[autonomy] could not inspect ticket worktree %s: %s",
                worktree,
                err.strip()[:500],
            )
            raise TicketWorktreeError("could not inspect ticket worktree before removal")
        if dirty.strip():
            try:
                # worktree = {root}/{repo_key}/tickets/{ticket_key}
                artifact_root = worktree.parents[2] / "recovery-artifacts"
                artifact = await _archive_dirty_worktree(
                    repo,
                    worktree,
                    artifact_root,
                    checkout,
                    {"branch": branch, "repo": str(repo), "path": str(worktree)},
                )
            except LaunchError as exc:
                raise TicketWorktreeError(str(exc)) from exc
        # Dirty state is archived above; --force only skips Git's own clean
        # check (which would run status again in a child process).
        code, _, err = await git(
            ["worktree", "remove", "--force", str(worktree)],
            filters=checkout.filters,
        )
        if code != 0:
            logger.warning(
                "[autonomy] could not remove ticket worktree %s: %s",
                worktree,
                err.strip()[:500],
            )
            raise TicketWorktreeError("could not remove ticket worktree")
        await git(["worktree", "prune"])
    return artifact


def configured_cwd_is_git_repo(cwd: str | Path | None) -> bool:
    """True when ``cwd`` resolves to a git checkout (dir or linked worktree)."""
    if cwd is None:
        return False
    text = str(cwd).strip()
    if not text:
        return False
    return (Path(text).expanduser() / ".git").exists()


def is_remote_daemon_ticket(ticket: Mapping[str, Any] | None) -> bool:
    """True when a Ticket is bound to a daemon that owns its own host checkout.

    The server must not create or remove that checkout: a server absolute path
    is useless (or harmful) on a remote daemon host.
    """
    if not isinstance(ticket, Mapping):
        return False
    return bool(str(ticket.get("worker_daemon_id") or "").strip())


def _open_statuses() -> tuple[str, ...]:
    return tuple(getattr(ticket_db, "OPEN_STATUSES", _OPEN_STATUSES))


def _is_open(row: Mapping[str, Any]) -> bool:
    return not row.get("closed_at") and str(row.get("status") or "") in _open_statuses()


def orphaned_server_checkout(row: Mapping[str, Any]) -> bool:
    """A server-host checkout left behind when its Ticket moved to a daemon.

    The daemon builds its own checkout on its own host, so the server copy is
    no longer the Ticket's working tree even though the Ticket stays open; it
    must be released, or the daemon's bind is refused for as long as it lives.
    """
    return (
        str(row.get("worktree_host") or "") == HOST_SERVER
        and bool(str(row.get("worktree_path") or "").strip())
        and is_remote_daemon_ticket(row)
    )


def _mandate(ticket: Mapping[str, Any]) -> dict[str, Any]:
    mandate = ticket.get("mandate")
    return dict(mandate) if isinstance(mandate, Mapping) else {}


def _wants_writable_checkout(ticket: Mapping[str, Any]) -> bool:
    mandate = _mandate(ticket)
    return not (mandate.get("speculative") or mandate.get("read_only"))


def _base_branch(ticket: Mapping[str, Any]) -> str:
    return (
        str(
            ticket.get("worktree_base_branch")
            or _mandate(ticket).get("base_branch")
            or DEFAULT_BASE_BRANCH
        ).strip()
        or DEFAULT_BASE_BRANCH
    )


def _server_repo(ticket: Mapping[str, Any]) -> Path | None:
    """The server-side git workspace of a server-run Ticket: its mandate cwd."""
    cwd = str(_mandate(ticket).get("cwd") or "").strip()
    if not configured_cwd_is_git_repo(cwd):
        return None
    return Path(cwd).expanduser().resolve()


def _dispatch_repo(ticket: Mapping[str, Any]) -> tuple[Path | None, bool]:
    """The repository a daemon Ticket's base is resolved from on the server.

    ``CONDUCTOR_DISPATCH_REPO`` is the same repository the daemon creates its
    host checkout from; a server-visible git mandate cwd is the fallback.
    The flag says whether the repository is the server-wide one (whose
    failure is ``TicketWorktreeUnavailable``, never the Ticket's fault).
    """
    raw = os.environ.get("CONDUCTOR_DISPATCH_REPO", "").strip()
    if raw:
        path = Path(raw).expanduser()
        if not configured_cwd_is_git_repo(path):
            raise TicketWorktreeUnavailable(
                "CONDUCTOR_DISPATCH_REPO is not a usable git checkout"
            )
        return path.resolve(), True
    return _server_repo(ticket), False


async def _pin_base(
    ticket_id: str, *, repo: Path, base_branch: str, server_wide: bool = False
) -> dict[str, Any]:
    """Resolve and durably pin the Ticket base; an existing pin always wins."""
    sha = await resolve_base_sha(repo, base_branch, server_wide=server_wide)
    row = await ticket_db.pin_ticket_worktree_base(
        ticket_id, base_sha=sha, base_branch=base_branch
    )
    if row is None:
        raise TicketWorktreeError(f"ticket {ticket_id} disappeared while pinning base_sha")
    if not str(row.get("worktree_base_sha") or "").strip():
        raise TicketWorktreeError(f"ticket {ticket_id} base_sha was not persisted")
    return row


async def daemon_claim_worktree_spec(ticket: Mapping[str, Any]) -> dict[str, Any] | None:
    """Execution-host instructions for a daemon claim, with a durable base.

    Returns None when the Ticket has no git workspace (or is read-only).
    Otherwise the base is resolved on the server once and persisted with
    ``pin_ticket_worktree_base`` before returning, so repeated claims before
    the first bind cannot pick different bases; an existing pin wins.

    Failures are typed so the caller can tell whose they are:
    ``TicketWorktreeBaseError`` when this Ticket's base ref (or its own
    repository, or its id) cannot be resolved, so the Ticket may be failed;
    ``TicketWorktreeUnavailable`` when the server cannot resolve any base
    right now (dispatch repository missing or unreadable, Git missing, Git
    timed out), so the Ticket must stay queued. Anything else (a plain
    ``TicketWorktreeError`` such as the pin not persisting, or a database
    error) is neither. ``worktree_path`` is only ever the claiming daemon's
    own persisted host path: never a server path, and never the path of a
    departed daemon's binding (another host's checkout).
    """
    ticket_id = str(ticket.get("ticket_id") or "").strip()
    if not ticket_id or not _wants_writable_checkout(ticket):
        return None
    try:
        branch = ticket_branch(ticket_id)
    except TicketWorktreeError as exc:
        raise TicketWorktreeBaseError(str(exc)) from exc
    row: Mapping[str, Any] = ticket
    base_sha = str(ticket.get("worktree_base_sha") or "").strip()
    base_branch = _base_branch(ticket)
    if not base_sha:
        repo, server_wide = _dispatch_repo(ticket)
        if repo is None:
            return None
        row = await _pin_base(
            ticket_id, repo=repo, base_branch=base_branch, server_wide=server_wide
        )
        base_sha = str(row["worktree_base_sha"]).strip()
        base_branch = str(row.get("worktree_base_branch") or base_branch).strip()
    host_path = (
        str(row.get("worktree_path") or "")
        if str(row.get("worktree_host") or "") == HOST_DAEMON
        and not departed_daemon_binding(row)
        else ""
    )
    return {
        "ticket_id": ticket_id,
        "branch": branch,
        "base_sha": base_sha,
        "base_branch": base_branch or DEFAULT_BASE_BRANCH,
        "worktree_path": host_path,
        "worktree_generation": int(row.get("worktree_generation") or 0),
    }


def departed_daemon_binding(row: Mapping[str, Any] | None) -> bool:
    """The row holds a daemon-host binding whose daemon no longer owns the Ticket.

    ``worktree_host='daemon'`` with a path, and ``worktree_daemon_id`` IS
    DISTINCT FROM ``worker_daemon_id`` (the Ticket moved to the server, so
    the worker is NULL, or to another daemon). That checkout lives on the
    departed daemon's host, out of the new owner's reach; the new owner's
    bind replaces it with ``replace_generation`` (persistence keeps a note so
    the old daemon's late cleanup report is settled, not refused).
    """
    if not isinstance(row, Mapping):
        return False
    return (
        str(row.get("worktree_host") or "") == HOST_DAEMON
        and bool(str(row.get("worktree_path") or "").strip())
        and str(row.get("worktree_daemon_id") or "") != str(row.get("worker_daemon_id") or "")
    )


def _replace_kwargs(row: Mapping[str, Any] | None) -> dict[str, int]:
    """``replace_generation`` for a bind onto ``row`` (fenced on what was read)."""
    if row is None or not departed_daemon_binding(row):
        return {}
    return {"replace_generation": int(row.get("worktree_generation") or 0)}


def _merge_row(original: Mapping[str, Any], updated: Mapping[str, Any]) -> dict[str, Any]:
    """Updated row first, keeping claim-only fields the row does not carry."""
    merged = dict(updated)
    for key, value in original.items():
        merged.setdefault(key, value)
    return merged


def _bind_refusal(
    row: Mapping[str, Any] | None, expected_path: Path | None = None
) -> str | None:
    """Why a server bind must not be written onto ``row`` (None when allowed).

    A pending cleanup of this very server checkout does not refuse: the
    Ticket was reopened before the retry ran, and the bind takes the checkout
    over (the persistence bind clears pending for the same path and host).
    A departed daemon's binding (pending or not) does not refuse either: the
    bind replaces it, fenced on its generation. A pending cleanup of any
    other checkout still refuses.
    """
    if row is None:
        return "ticket disappeared before its worktree could be bound"
    if is_remote_daemon_ticket(row):
        return "ticket was reassigned to a daemon while binding its worktree"
    if not _is_open(row):
        return "ticket closed before its worktree could be bound"
    if departed_daemon_binding(row):
        return None
    if str(row.get("worktree_host") or "") == HOST_DAEMON and row.get("worktree_path"):
        return "ticket already has a daemon-host checkout"
    if row.get("worktree_cleanup_pending"):
        same_checkout = (
            expected_path is not None
            and str(row.get("worktree_host") or "") == HOST_SERVER
            and str(row.get("worktree_path") or "") == str(expected_path)
        )
        if not same_checkout:
            return "the Ticket's previous checkout is still waiting for cleanup"
    return None


async def prepare_server_ticket_worktree(ticket: Mapping[str, Any]) -> dict[str, Any]:
    """Create (or reuse) and bind the server-host checkout for a server Ticket.

    Returns ``ticket`` unchanged when no server checkout applies: daemon
    Tickets (the daemon creates on its own host), read-only/speculative
    mandates, and mandates without a git cwd. Otherwise returns the bound row
    merged over ``ticket``. Fail-closed: any create, pin, or bind failure
    raises ``TicketWorktreeError`` and a checkout this call created is removed
    again, so a Worker never runs writable work in the shared repository.
    """
    if is_remote_daemon_ticket(ticket) or not _wants_writable_checkout(ticket):
        return dict(ticket)
    repo = _server_repo(ticket)
    if repo is None:
        return dict(ticket)
    ticket_id = str(ticket.get("ticket_id") or "").strip()
    branch = ticket_branch(ticket_id)
    expected_path = ticket_worktree_path(_worktree_root(), repo, ticket_id)
    async with ticket_db.ticket_worktree_lock(ticket_id):
        row = await ticket_db.get_ticket(ticket_id)
        refusal = _bind_refusal(row, expected_path)
        if refusal is not None:
            raise TicketWorktreeError(refusal)
        assert row is not None
        base_sha = str(row.get("worktree_base_sha") or "").strip()
        if not base_sha:
            row = await _pin_base(ticket_id, repo=repo, base_branch=_base_branch(row))
            base_sha = str(row["worktree_base_sha"]).strip()
        bound_path = str(row.get("worktree_path") or "")
        if (
            bound_path
            and bound_path != str(expected_path)
            and not departed_daemon_binding(row)
        ):
            raise TicketWorktreeError(
                f"ticket {ticket_id} is bound to a different checkout {bound_path!r}"
            )
        existed = expected_path.exists()
        record = await ensure_ticket_worktree(
            repo, ticket_id, base_sha=base_sha
        )
        if record.branch != branch:  # pragma: no cover - same derivation
            raise TicketWorktreeError("ticket worktree branch mismatch")
        try:
            bound = None
            for _ in range(2):
                revision = row.get("revision")
                bound = await ticket_db.bind_ticket_worktree(
                    ticket_id,
                    worktree_path=str(record.path),
                    branch=record.branch,
                    base_sha=base_sha,
                    repo=str(record.repo),
                    host=HOST_SERVER,
                    expected_revision=int(revision) if isinstance(revision, int) else None,
                    **_replace_kwargs(row),
                )
                if bound is not None:
                    break
                # A concurrent write moved the revision. Re-check ownership on
                # the fresh row before retrying: a local-to-daemon reassignment
                # or a close must fence this server path out.
                row = await ticket_db.get_ticket(ticket_id)
                refusal = _bind_refusal(row, expected_path)
                if refusal is not None:
                    raise TicketWorktreeError(refusal)
                assert row is not None
            if bound is None:
                raise TicketWorktreeError(
                    f"ticket {ticket_id} kept changing while binding its worktree"
                )
        except Exception:
            if not existed:
                # Still under the lock: no other bind can have claimed this
                # path, and the row does not name it (the bind was refused).
                latest = await ticket_db.get_ticket(ticket_id)
                if str((latest or {}).get("worktree_path") or "") != str(record.path):
                    try:
                        await remove_ticket_worktree(record)
                    except Exception:  # noqa: BLE001 - the bind failure is what matters
                        logger.warning(
                            "[autonomy] could not remove the unbound ticket "
                            "worktree ticket=%s",
                            ticket_id,
                            exc_info=True,
                        )
            raise
    return _merge_row(ticket, bound)


def server_worktree_cwd(ticket: Mapping[str, Any] | None) -> str:
    """The bound server-host checkout path of ``ticket``, or ''."""
    if not isinstance(ticket, Mapping):
        return ""
    if str(ticket.get("worktree_host") or "") != HOST_SERVER:
        return ""
    return str(ticket.get("worktree_path") or "").strip()


def _cleanup_backoff(attempts: int) -> float:
    exponent = max(0, min(int(attempts or 0), 16))
    return min(
        _CLEANUP_BACKOFF_BASE_SECONDS * (2**exponent), _CLEANUP_BACKOFF_MAX_SECONDS
    )


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


async def _thread_has_active_run(thread_id: str) -> bool:
    from src.server.database.runs.lifecycle import get_active_run

    return await get_active_run(thread_id) is not None


# Audit detail key the reassignment writes with the server Worker thread it
# took the Ticket away from (ownership.reassign_ticket). The reassignment
# clears ``worker_thread_id`` from the row, so this is the durable record a
# later retry of the orphaned server checkout checks for a live run.
REASSIGN_FROM_THREAD_KEY = "from_worker_thread_id"


# How far back the reassignment history is read, page by page. A history
# whose reassignment away from the server lies beyond this is unknown.
_REASSIGN_EVENT_PAGE = 100
_REASSIGN_EVENT_PAGES = 10


def _left_the_server(event: Mapping[str, Any]) -> bool:
    detail = event.get("detail")
    if isinstance(detail, Mapping) and str(detail.get(REASSIGN_FROM_THREAD_KEY) or "").strip():
        return True
    return str(event.get("from_owner") or "") == "cloud"


async def _reassigned_server_threads(ticket_id: str) -> set[str] | None:
    """Server Worker threads recorded by this Ticket's reassignments away.

    None means unknown: no reassignment away from the server was found, either
    because none was recorded or because it lies beyond the pages read. An
    orphaned server checkout always has one, so not finding it is never read
    as "no old run".
    """
    from src.server.database.autonomy import lines as line_db

    threads: set[str] = set()
    found = False
    for page in range(_REASSIGN_EVENT_PAGES):
        events = await line_db.list_ticket_events(
            ticket_id, limit=_REASSIGN_EVENT_PAGE, offset=page * _REASSIGN_EVENT_PAGE
        )
        for event in events:
            if event.get("action") != "reassigned" or not _left_the_server(event):
                continue
            found = True
            detail = event.get("detail")
            if isinstance(detail, Mapping):
                thread = str(detail.get(REASSIGN_FROM_THREAD_KEY) or "").strip()
                if thread:
                    threads.add(thread)
        if found or len(events) < _REASSIGN_EVENT_PAGE:
            break
    return threads if found else None


async def _stop_verification(ticket_id: str) -> str | None:
    from src.server.database.autonomy import stops as stop_db

    stop = await stop_db.get_stop(ticket_id)
    return str(stop.get("verification") or "") if stop else None


async def _threads_have_active_run(ticket_id: str, thread_ids: set[str]) -> bool:
    """Whether any of ``thread_ids`` has an in-progress run (unknown = yes)."""
    for thread_id in sorted(thread_ids):
        try:
            if await _thread_has_active_run(thread_id):
                return True
        except Exception:  # noqa: BLE001 - unknown run state is not an end
            logger.warning(
                "[autonomy] could not read the Worker run of ticket=%s",
                ticket_id,
                exc_info=True,
            )
            return True
    return False


async def server_runner_ended(
    row: Mapping[str, Any], *, runner_thread_id: str | None = None
) -> bool:
    """Whether every runner that could write into the server checkout is gone.

    An open Ticket may still run. A cancelled Ticket's run is not over until
    its stop is no longer pending, no daemon lease is live, an isolated Docker
    run's heartbeat has gone quiet, and its Worker thread has no in-progress
    run. ``runner_thread_id`` names the thread of the server run that bound
    the checkout when the row no longer carries it (a reassignment clears
    ``worker_thread_id``); for an orphaned checkout the server threads every
    reassignment recorded are checked too, so a later retry (the sweep has no
    caller thread) still waits for the old run. Unknown is treated as alive.
    """
    orphaned = orphaned_server_checkout(row)
    if _is_open(row) and not orphaned:
        return False
    ticket_id = str(row.get("ticket_id") or "")
    now = datetime.now(timezone.utc)
    threads = {
        str(value).strip()
        for value in (runner_thread_id, row.get("worker_thread_id"))
        if str(value or "").strip()
    }
    if orphaned:
        # The lease, stop and current Worker thread now belong to the daemon
        # run (the reassignment cleared the server thread from the row). The
        # takeover asked the server runner to stop, but its run can still be
        # winding down in the old checkout, so the old thread the caller names
        # is checked, as is an isolated run that may still be heart-beating.
        if _isolated_run_live(row, now):
            return False
        old_threads = {str(runner_thread_id or "").strip()} - {""}
        try:
            recorded = await _reassigned_server_threads(ticket_id)
        except Exception:  # noqa: BLE001 - an unreadable history is not an end
            logger.warning(
                "[autonomy] could not read the reassignment history of ticket=%s",
                ticket_id,
                exc_info=True,
            )
            return False
        if recorded is None:
            # A missing (or out-of-window) reassignment event says nothing
            # about the old server run: unknown, so treated as alive.
            logger.warning(
                "[autonomy] no reassignment away from the server found for the "
                "orphaned checkout of ticket=%s; keeping it",
                ticket_id,
            )
            return False
        old_threads |= recorded
        return not await _threads_have_active_run(ticket_id, old_threads)
    lease_expires = _parse_time(row.get("worker_lease_expires_at"))
    if row.get("worker_lease_id") and (lease_expires is None or lease_expires > now):
        return False
    try:
        if await _stop_verification(ticket_id) == "pending":
            return False
    except Exception:  # noqa: BLE001 - an unreadable stop is not a confirmed end
        logger.warning(
            "[autonomy] could not read the stop of ticket=%s", ticket_id, exc_info=True
        )
        return False
    if _isolated_run_live(row, now):
        return False
    return not await _threads_have_active_run(ticket_id, threads)


def _isolated_run_live(row: Mapping[str, Any], now: datetime) -> bool:
    marker = _mandate(row).get("cloud_worker")
    if isinstance(marker, Mapping) and marker.get("isolated"):
        beat = _parse_time(marker.get("heartbeat_at"))
        if beat is not None and now - beat < timedelta(
            seconds=_ISOLATED_HEARTBEAT_LIVE_SECONDS
        ):
            return True
    return False


def _inside(candidate: str, root: Path) -> bool:
    text = candidate.removesuffix(" (deleted)")
    if not text.startswith("/"):
        return False
    path = Path(text)
    return path == root or root in path.parents


async def _checkout_process_ids(worktree: Path) -> set[int] | None:
    """PIDs on this host whose working directory is inside ``worktree``.

    OS state, not process memory, so every server worker sees the same
    answer. A native Worker CLI (and every tool it starts) runs with its cwd in
    the checkout. ``None`` means unknown (treated as alive): the probe failed,
    or the host has neither ``/proc`` nor ``lsof`` and cannot be probed.
    """
    root = worktree.resolve()
    me = os.getpid()
    found: set[int] = set()
    proc = _PROC_ROOT
    if (proc / "self" / "cwd").exists():
        try:
            entries = [entry for entry in os.listdir(proc) if entry.isdigit()]
        except OSError:
            return None
        for entry in entries:
            try:
                cwd = os.readlink(proc / entry / "cwd")
            except OSError:
                continue  # exited, or another user's process
            if int(entry) != me and _inside(cwd, root):
                found.add(int(entry))
        return found
    lsof = next(
        (path for path in _LSOF_CANDIDATES if os.access(path, os.X_OK)), None
    )
    if lsof is None:
        # Neither /proc nor lsof: the processes cannot be seen, which is not
        # the same as there being none.
        return None
    process = await asyncio.create_subprocess_exec(
        lsof,
        "-w",
        "-n",
        "-P",
        "-d",
        "cwd",
        "-Fpn",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={"PATH": os.defpath},
    )
    try:
        out, _ = await asyncio.wait_for(
            process.communicate(), _PROCESS_PROBE_TIMEOUT_SECONDS
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        return None
    if process.returncode not in (0, 1) or not out:
        return None
    pid: int | None = None
    for line in out.decode("utf-8", "surrogateescape").splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
        elif line.startswith("n") and pid is not None and pid != me:
            if _inside(line[1:], root):
                found.add(pid)
    return found


async def _process_gone_within(worktree: Path, seconds: float) -> bool:
    """Wait up to ``seconds`` for every process in ``worktree`` to exit.

    The run ledger can say a run ended while its CLI process tree is still
    exiting; ``worktree remove --force`` under a live process would delete
    files it is still writing. False keeps the checkout for the retry sweep.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, seconds)
    while True:
        pids = await _checkout_process_ids(worktree)
        if pids is not None and not pids:
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(_PROCESS_POLL_SECONDS)


def _cleanup_attempt_cap() -> int:
    return int(getattr(ticket_db, "MAX_WORKTREE_CLEANUP_ATTEMPTS", 20))


async def _defer_cleanup(
    row: Mapping[str, Any], path: str, generation: int, *, runner_wait: bool = False
) -> bool:
    """Keep the checkout for the retry sweep, fenced on this exact binding.

    A first deferral records pending (host-fenced); a row that is already
    pending is rescheduled, so every failed retry counts toward the cap and
    backs off.
    """
    ticket_id = str(row.get("ticket_id") or "")
    attempts = int(row.get("worktree_cleanup_attempts") or 0)
    if row.get("worktree_cleanup_pending"):
        return await ticket_db.reschedule_worktree_cleanup_pending(
            ticket_id,
            worktree_path=path,
            worktree_generation=generation,
            delay_seconds=_cleanup_backoff(attempts),
        )
    return await ticket_db.record_worktree_cleanup_pending(
        ticket_id,
        worktree_path=path,
        worktree_generation=generation,
        delay_seconds=_RUNNER_WAIT_SECONDS if runner_wait else _cleanup_backoff(attempts),
        host=HOST_SERVER,
    )


def _binding_of(row: Mapping[str, Any]) -> tuple[str, int, str]:
    return (
        str(row.get("worktree_path") or ""),
        int(row.get("worktree_generation") or 0),
        str(row.get("worktree_host") or ""),
    )


async def release_server_ticket_worktree(
    ticket: Mapping[str, Any], *, reason: str, runner_thread_id: str | None = None
) -> str:
    """Remove a server-host checkout once its runner is confirmed ended.

    Returns ``"removed"``, ``"pending"`` (kept for the durable retry sweep) or
    ``"skipped"`` (nothing of this binding to remove: daemon host, no checkout,
    a newer or already-cleared binding, or a Ticket still open and keeping its
    checkout for resume). Validation, removal and clearing all happen under one
    ``ticket_worktree_lock``, fenced on the caller's path and generation (and
    the server host), so a stale cleanup can never remove or clear a newer
    attempt's checkout. Removal also waits for every process in the checkout
    to exit. A failed removal only records pending state, never the binding.
    The Ticket branch is kept.
    """
    ticket_id = str(ticket.get("ticket_id") or "").strip()
    path, generation, host = _binding_of(ticket)
    if not ticket_id or not path or host != HOST_SERVER or generation < 1:
        return "skipped"
    try:
        async with ticket_db.ticket_worktree_lock(ticket_id):
            row = await ticket_db.get_ticket(ticket_id)
            if row is None or _binding_of(row) != (path, generation, host):
                return "skipped"
            if _is_open(row) and not orphaned_server_checkout(row):
                return "skipped"
            if not await server_runner_ended(
                row, runner_thread_id=runner_thread_id
            ) or not await _process_gone_within(
                Path(path), _PROCESS_GONE_GRACE_SECONDS
            ):
                logger.info(
                    "[autonomy] ticket worktree kept until its runner ends "
                    "ticket=%s reason=%s",
                    ticket_id,
                    reason,
                )
                await _defer_cleanup(row, path, generation, runner_wait=True)
                return "pending"
            repo = str(row.get("worktree_repo") or "").strip()
            branch = str(row.get("worktree_branch") or "").strip()
            try:
                if not repo or not branch:
                    raise TicketWorktreeError("ticket worktree binding is incomplete")
                await remove_ticket_worktree(
                    TicketWorktreeRecord(
                        path=Path(path),
                        branch=branch,
                        base_sha=str(row.get("worktree_base_sha") or ""),
                        repo=Path(repo),
                    )
                )
            except Exception:  # noqa: BLE001 - becomes durable pending state
                logger.warning(
                    "[autonomy] ticket worktree removal failed ticket=%s reason=%s",
                    ticket_id,
                    reason,
                    exc_info=True,
                )
                await _defer_cleanup(row, path, generation)
                return "pending"
            await ticket_db.clear_ticket_worktree_binding(
                ticket_id,
                worktree_path=path,
                worktree_generation=generation,
                host=HOST_SERVER,
            )
    except TimeoutError:
        # The lock holder may be binding or removing; the fenced pending write
        # cannot touch a newer binding, so the sweep can retry later.
        await _defer_cleanup(ticket, path, generation)
        return "pending"
    try:
        from src.server.services.autonomy.git_delivery import clear_ticket_git_baseline

        clear_ticket_git_baseline(ticket_id)
    except Exception:  # noqa: BLE001 - in-process cache only
        pass
    return "removed"


async def _cancel_reopened_cleanup(row: Mapping[str, Any]) -> None:
    """A reopened server Ticket keeps its checkout: drop the owed cleanup.

    Without this the row would stay pending forever (release skips open
    Tickets) and its next bind could be refused. The persistence write is
    fenced on the attempt, the server host, open status and ownership.
    """
    ticket_id = str(row.get("ticket_id") or "")
    path, generation, _host = _binding_of(row)
    async with ticket_db.ticket_worktree_lock(ticket_id):
        await ticket_db.cancel_worktree_cleanup_pending(
            ticket_id,
            worktree_path=path,
            worktree_generation=generation,
            host=HOST_SERVER,
        )


async def sweep_ticket_worktree_cleanups(*, limit: int = 20) -> int:
    """Durable retry consumer for pending server-host cleanups.

    Completed and cancelled Tickets leave every other sweep, so the scheduler
    tick drives their pending removals here. A reopened, still server-owned
    Ticket's pending cleanup is cancelled instead of retried. Rows at the
    attempt cap are left for an operator. Returns how many were removed.
    """
    rows = await ticket_db.claim_worktree_cleanup_pending(
        host=HOST_SERVER, limit=limit
    )
    cap = _cleanup_attempt_cap()
    removed = 0
    for row in rows:
        ticket_id = row.get("ticket_id")
        attempts = int(row.get("worktree_cleanup_attempts") or 0)
        if attempts >= cap:
            logger.warning(
                "[autonomy] ticket worktree cleanup gave up after %s attempts ticket=%s",
                attempts,
                ticket_id,
            )
            continue
        try:
            if _is_open(row) and not orphaned_server_checkout(row):
                await _cancel_reopened_cleanup(row)
                continue
            outcome = await release_server_ticket_worktree(row, reason="retry")
        except Exception:  # noqa: BLE001 - one row must not stall the sweep
            logger.warning(
                "[autonomy] ticket worktree cleanup retry failed ticket=%s",
                ticket_id,
                exc_info=True,
            )
            path, generation, _host = _binding_of(row)
            try:
                await ticket_db.reschedule_worktree_cleanup_pending(
                    str(ticket_id),
                    worktree_path=path,
                    worktree_generation=generation,
                    delay_seconds=_cleanup_backoff(attempts),
                )
            except Exception:  # noqa: BLE001 - the claim lease retries it
                logger.warning(
                    "[autonomy] could not reschedule the ticket worktree cleanup "
                    "ticket=%s",
                    ticket_id,
                    exc_info=True,
                )
            continue
        if outcome == "removed":
            removed += 1
    return removed
