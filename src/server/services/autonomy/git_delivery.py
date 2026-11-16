"""Hardened, read-only Git state checks for unfinished Ticket work."""

from __future__ import annotations

import hashlib
import contextlib
import contextvars
import fcntl
import math
import os
import selectors
import shutil
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from src.utils.system_paths import canonical_system_path


class GitDeliveryError(RuntimeError):
    """A Git workspace could not be inspected safely."""


class WorkspaceDeliveryBusy(GitDeliveryError):
    """Another Worker currently owns the workspace completion handoff."""


_TICKET_BASELINE_LOCK = threading.Lock()
_TICKET_BASELINES: dict[str, tuple[str, GitSnapshot | None, bool]] = {}

GitCommandRunner = Callable[..., subprocess.CompletedProcess[bytes]]
_GIT_COMMAND_RUNNER: contextvars.ContextVar[GitCommandRunner | None] = (
    contextvars.ContextVar("autonomy_git_command_runner", default=None)
)


@contextlib.contextmanager
def git_command_runner(runner: GitCommandRunner):
    """Route every Git subprocess in this context through a trusted boundary."""
    token = _GIT_COMMAND_RUNNER.set(runner)
    try:
        yield
    finally:
        _GIT_COMMAND_RUNNER.reset(token)


class WorkspaceDeliveryLock:
    """Cross-process lock shared by all Workers for one canonical workspace."""

    def __init__(
        self,
        cwd: str,
        *,
        timeout: float = 30.0,
        lock_root: str | None = None,
        workspace_root: str | None = None,
    ):
        self.workspace = (
            Path(workspace_root).expanduser().resolve()
            if workspace_root is not None
            else _workspace_identity_root(cwd)
        )
        try:
            bounded_timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("workspace lock timeout must be a finite number") from exc
        if not math.isfinite(bounded_timeout):
            raise ValueError("workspace lock timeout must be a finite number")
        self.timeout = min(max(bounded_timeout, 0.0), 30.0)
        self.lock_root = canonical_system_path(
            lock_root
            or os.environ.get("CONDUCTOR_WORKER_LOCK_ROOT")
            or (Path.home() / ".cache" / "conductor" / "worker-workspace-locks")
        )
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            raise RuntimeError("workspace lock is already held")
        deadline = time.monotonic() + self.timeout
        delay = 0.01
        try:
            while not self._try_acquire():
                if time.monotonic() >= deadline:
                    raise WorkspaceDeliveryBusy(
                        "Git workspace status is unknown because its Worker lock is busy."
                    )
                time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
                delay = min(delay * 2, 0.5)
        except BaseException:
            self.release()
            raise

    def _try_acquire(self) -> bool:
        """Open and try the lock once; async callers never block a thread in a wait loop."""
        if self._fd is None:
            if any(part.is_symlink() for part in (self.lock_root, *self.lock_root.parents)):
                raise GitDeliveryError("Git workspace status is unknown because its Worker lock is unsafe.")
            self.lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            root_info = self.lock_root.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(root_info.st_mode)
                or root_info.st_uid != os.getuid()
                or root_info.st_mode & 0o077
            ):
                raise GitDeliveryError("Git workspace status is unknown because its Worker lock is unsafe.")
            lock_name = hashlib.sha256(os.fsencode(self.workspace)).hexdigest() + ".lock"
            path = self.lock_root / lock_name
            fd = os.open(
                path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            self._fd = fd
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                self.release()
                raise GitDeliveryError("Git workspace status is unknown because it could not be checked safely.")
        if any(part.is_symlink() for part in (self.lock_root, *self.lock_root.parents)):
            self.release()
            raise GitDeliveryError("Git workspace status is unknown because its Worker lock is unsafe.")
        assert self._fd is not None
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        except BaseException:
            self.release()
            raise
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc):
        self.release()


class AsyncWorkspaceDeliveryLock(WorkspaceDeliveryLock):
    async def __aenter__(self):
        import asyncio
        deadline = time.monotonic() + self.timeout
        delay = 0.01
        deferred_release = False
        try:
            while True:
                pending = asyncio.create_task(asyncio.to_thread(self._try_acquire))
                try:
                    acquired = await asyncio.shield(pending)
                except asyncio.CancelledError:
                    def release_after_attempt(task):
                        self.release()
                        if not task.cancelled():
                            task.exception()

                    deferred_release = True
                    if pending.done():
                        release_after_attempt(pending)
                    else:
                        pending.add_done_callback(release_after_attempt)
                    raise
                if acquired:
                    return self
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkspaceDeliveryBusy(
                        "Git workspace status is unknown because its Worker lock is busy."
                    )
                await asyncio.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.5)
        except BaseException:
            if not deferred_release:
                self.release()
            raise

    async def __aexit__(self, *_exc):
        self.release()


def _workspace_identity_root(cwd: str) -> Path:
    path = Path(cwd).expanduser().resolve()
    if not path.is_dir():
        raise GitDeliveryError("Git workspace status is unknown because its directory is unavailable.")
    for parent in (path, *path.parents):
        if os.path.lexists(parent / ".git"):
            return parent
    return path


@dataclass(frozen=True)
class GitSnapshot:
    """A status snapshot with opaque path IDs and content fingerprints."""

    root_id: str | None
    gitdir_id: str | None
    entries: dict[str, tuple[str, str, str]]
    #: Tool-cache directories (``.venv``, ``__pycache__``, ...) holding
    #: untracked or ignored files. They are reported apart from uncommitted
    #: work and never persisted: they are filenames, and they say nothing
    #: about delivery.
    tool_caches: tuple[str, ...] = field(default=(), compare=False)
    #: SHA-256 ids of ``tool_caches``, the only part of them a record keeps,
    #: so a later summary can tell which caches the run created. None for a
    #: record persisted before they were kept: every cache is then new.
    tool_cache_ids: frozenset[str] | None = field(default=None, compare=False)
    #: HEAD when the snapshot was taken; None for an unborn branch or a
    #: non-Git workspace. ``head_checked`` is False for a record persisted
    #: before commit checks existed; ``head_known`` is False when HEAD could
    #: not be read (or was never checked).
    head: str | None = field(default=None, compare=False)
    head_known: bool = field(default=False, compare=False)
    head_checked: bool = field(default=False, compare=False)
    #: The Ticket's delivery origin is unknown (an earlier run's commits
    #: cannot be told apart): the commit check is ``unverified``.
    origin_unknown: bool = field(default=False, compare=False)

    def new_tool_caches(self, baseline: GitSnapshot) -> tuple[str, ...]:
        """The tool caches this snapshot holds that ``baseline`` did not."""
        known = baseline.tool_cache_ids or frozenset()
        return tuple(
            cache for cache in self.tool_caches if _tool_cache_id(cache) not in known
        )

    def to_record(self) -> dict[str, Any]:
        """Return a persistence-safe baseline; filenames and file contents stay out."""
        record: dict[str, Any] = {
            "version": 1,
            "root_id": self.root_id,
            "gitdir_id": self.gitdir_id,
            "entries": {
                key: [status, fingerprint, index_identity]
                for key, (status, fingerprint, index_identity) in self.entries.items()
            },
        }
        if self.tool_cache_ids is not None:
            record["tool_cache_ids"] = sorted(self.tool_cache_ids)
        if self.head_checked:
            record["head"] = self.head
            record["head_known"] = self.head_known
        return record

    @classmethod
    def from_record(cls, value: Any) -> GitSnapshot | None:
        if not isinstance(value, dict) or value.get("version") != 1:
            return None
        root_id = value.get("root_id")
        gitdir_id = value.get("gitdir_id")
        raw_entries = value.get("entries")
        if (root_id is not None and not _is_digest(root_id)) or (
            gitdir_id is not None and not _is_digest(gitdir_id)
        ) or not isinstance(
            raw_entries, dict
        ):
            return None
        if root_id is None and (gitdir_id is not None or raw_entries):
            return None
        head = value.get("head")
        if head is not None and not _is_object_id(head):
            return None
        head_checked = "head" in value
        head_known = value.get("head_known", head_checked)
        if not isinstance(head_known, bool) or (head is not None and not head_known):
            return None
        entries: dict[str, tuple[str, str, str]] = {}
        for key, row in raw_entries.items():
            if (
                isinstance(key, str)
                and _is_digest(key)
                and isinstance(row, list)
                and len(row) == 3
                and all(isinstance(item, str) for item in row)
                and len(row[0]) == 2
                and _is_digest(row[1])
                and _is_digest(row[2])
            ):
                entries[key] = (row[0], row[1], row[2])
            else:
                return None
        raw_cache_ids = value.get("tool_cache_ids")
        tool_cache_ids: frozenset[str] | None = None
        if raw_cache_ids is not None:
            if not isinstance(raw_cache_ids, list) or not all(
                _is_digest(item) for item in raw_cache_ids
            ):
                return None
            tool_cache_ids = frozenset(raw_cache_ids)
        return cls(
            root_id=root_id,
            gitdir_id=gitdir_id,
            entries=entries,
            tool_cache_ids=tool_cache_ids,
            head=head,
            head_known=head_checked and head_known,
            head_checked=head_checked,
        )


def _tool_cache_id(cache: str) -> str:
    return hashlib.sha256(os.fsencode(cache)).hexdigest()


def _is_object_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) in (40, 64)
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _git_env() -> dict[str, str]:
    # ``GIT_NO_REPLACE_OBJECTS``: a ``refs/replace`` entry can swap HEAD for
    # a commit whose tree already holds the staged work, so status would
    # compare against the substitute and call the work committed.
    # ``GIT_ATTR_NOSYSTEM`` keeps the attribute sources to the repository's.
    return {
        "PATH": os.defpath,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def git_probe_env() -> dict[str, str]:
    """The hardened environment every Git probe runs with.

    The Docker runtime hands this to the container so the probe inside the
    container runs with exactly the settings the host runs them with.
    """
    return _git_env()


@dataclass(frozen=True)
class GitDrivers:
    """Every driver name the repository's own configuration defines, by kind.

    Each kind is a program Git runs on the repository's behalf when an
    attribute (``filter=``, ``merge=``, ``diff=``) names it, and any path's
    attributes can name any of them, so all configured names are collected
    and neutralised rather than only the ones a ``.gitattributes`` mentions
    today. A name no configuration defines runs nothing: Git falls back to
    its built-in behaviour for an undefined driver.
    """

    filters: tuple[str, ...] = ()
    merges: tuple[str, ...] = ()
    diffs: tuple[str, ...] = ()


#: The value each driver setting is overridden with, by kind. An empty
#: command is not a program: Git reports it cannot run it (a merge driver's
#: file becomes a conflict) instead of starting anything the Worker chose.
_NEUTRAL_DRIVER_SETTINGS: dict[str, tuple[tuple[str, str], ...]] = {
    "filter": (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false")),
    "merge": (("driver", ""),),
    "diff": (("textconv", ""), ("command", "")),
}


def _driver_name_is_safe(name: str) -> bool:
    return bool(name) and "=" not in name and not any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in name
    )


def _driver_config_values(
    drivers: GitDrivers, *, unsafe_message: str
) -> list[tuple[str, str]]:
    """The configuration that neutralises every named driver.

    A filter, merge or diff driver is a program Git runs for a file, so a
    workspace that names one in its configuration could have Git run it
    during a check or a mutation. Every discovered driver gets its commands
    emptied (and a filter its ``required`` flag turned off), which leaves it
    defined but inert.
    """
    values: list[tuple[str, str]] = []
    for kind, names in (
        ("filter", drivers.filters),
        ("merge", drivers.merges),
        ("diff", drivers.diffs),
    ):
        for name in names:
            if not _driver_name_is_safe(name):
                raise GitDeliveryError(unsafe_message)
            for key, value in _NEUTRAL_DRIVER_SETTINGS[kind]:
                values.append((f"{kind}.{name}.{key}", value))
    return values


def is_neutral_driver_override(key: str, value: str) -> bool:
    """Whether one ``GIT_CONFIG_KEY``/``VALUE`` pair only disables a driver.

    A runner that forwards the hardened environment somewhere else (the
    Docker runtime) accepts nothing else in the environment's config slots,
    so the slots cannot carry a setting that re-enables a program.
    """
    section, _, rest = key.partition(".")
    name, separator, setting = rest.rpartition(".")
    neutral = _NEUTRAL_DRIVER_SETTINGS.get(section.lower())
    if neutral is None or not separator or not _driver_name_is_safe(name):
        return False
    return (setting.lower(), value) in neutral


def _git_env_with_drivers(
    drivers: GitDrivers, *, unsafe_message: str
) -> dict[str, str]:
    """The hardened environment, with every named driver neutralised."""
    env = _git_env()
    values = _driver_config_values(drivers, unsafe_message=unsafe_message)
    if values:
        env["GIT_CONFIG_COUNT"] = str(len(values))
        for index, (key, value) in enumerate(values):
            env[f"GIT_CONFIG_KEY_{index}"] = key
            env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


#: The configuration a check is read against, pinned in the command scope.
#:
#: Git reads the command scope last, so it outranks the system, global,
#: local and worktree scopes. System and global files are also pointed at
#: ``/dev/null``. ``include.path`` / ``includeIf`` cannot be switched off by
#: a flag - released Git has no ``--no-includes`` outside ``git config`` - so
#: a workspace whose local or worktree config holds any include directive is
#: refused before any other Git command runs (see ``_filter_names_with``).
#: Git's own parser decides what is an include, so every spelling Git
#: accepts (``[Include]``, ``[include] # note``, every ``includeIf``
#: condition) is caught, and no overlay or rewritten copy of the repository's
#: metadata is ever written on the host.
#:
#: ``core.fsmonitor`` and ``core.hooksPath`` keep a workspace from running
#: code of its own during a check; ``commit.gpgSign`` and
#: ``merge.verifySignatures`` keep a mutation's merge from starting a signing
#: program. ``core.excludesFile``, ``core.attributesFile`` and
#: ``core.untrackedCache`` keep it from hiding a file the verdict is read
#: from. ``status.showUntrackedFiles`` fixes the untracked-file mode, so a
#: Worker's config cannot decide that the files it wrote are invisible.
#: ``core.fileMode``, ``core.symlinks``, ``core.autocrlf``,
#: ``core.ignorecase``, ``core.checkStat`` and ``core.trustctime`` fix how
#: the worktree is compared with the index, so a Worker cannot make a changed
#: file compare equal - a mode change, a symlink, a line ending, a case-only
#: rename or a same-size rewrite would each be invisible if the workspace
#: could choose the comparison. ``diff.ignoreSubmodules`` and
#: ``diff.relative`` keep a diff from silently dropping paths. The rest pin
#: the spelling of what the status reports, so two checks of one tree
#: compare equal.
#:
#: ``core.ignoreStat`` keeps Git from trusting the index over the worktree
#: (entries a Worker already marked are refused separately). ``core.useReplaceRefs``
#: backs ``GIT_NO_REPLACE_OBJECTS``. ``diff.external`` is emptied so no
#: patch output can start a Worker-chosen program; ``submodule.recurse``
#: keeps a mutation from running Git inside a submodule under the
#: submodule's own, unchecked configuration; ``gc.auto`` and
#: ``maintenance.auto`` keep a mutation from starting background work.
_GIT_PINNED_CONFIG: tuple[tuple[str, str], ...] = (
    ("core.fsmonitor", "false"),
    ("core.hooksPath", "/dev/null"),
    ("core.untrackedCache", "false"),
    ("core.excludesFile", "/dev/null"),
    ("core.attributesFile", "/dev/null"),
    ("core.fileMode", "true"),
    ("core.symlinks", "true"),
    ("core.autocrlf", "false"),
    ("core.ignorecase", "false"),
    ("core.checkStat", "default"),
    ("core.trustctime", "true"),
    ("core.ignoreStat", "false"),
    ("core.useReplaceRefs", "false"),
    ("diff.external", ""),
    # A pager would run an arbitrary program on this host; ``cat`` keeps the
    # output byte-for-byte and runs nothing.
    ("core.pager", "cat"),
    ("submodule.recurse", "false"),
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
    ("commit.gpgSign", "false"),
    ("merge.verifySignatures", "false"),
    ("diff.ignoreSubmodules", "none"),
    ("diff.relative", "false"),
    ("status.showUntrackedFiles", "all"),
    ("status.relativePaths", "true"),
    ("status.renames", "true"),
    ("core.quotePath", "true"),
)

_UNSAFE_STATUS = "Git workspace status is unknown because it could not be checked safely."
_INCLUDES_REFUSED = (
    "Git workspace status is unknown because its Git configuration includes other files."
)


def _git_config_prefix() -> list[str]:
    """The pinned configuration as ``-c`` arguments for one Git invocation."""
    prefix: list[str] = []
    for key, value in _GIT_PINNED_CONFIG:
        prefix.extend(("-c", f"{key}={value}"))
    return prefix


def git_pinned_config_args() -> tuple[str, ...]:
    """The pinned configuration as ``-c`` arguments, in invocation order."""
    return tuple(_git_config_prefix())


def hardened_git_invocation(
    executable: str,
    args: Sequence[str],
    *,
    drivers: GitDrivers | None = None,
    unsafe_message: str = _UNSAFE_STATUS,
) -> tuple[tuple[str, ...], dict[str, str]]:
    """The argv and environment of every Git command a check or mutation runs.

    This is the only place the hardening is assembled. The host read path,
    the host mutation path and the Docker runtime's in-container version
    probe all call it, so a pin added here reaches all of them and none of
    them can drift onto a private copy of the list.
    """
    argv = (executable, *_git_config_prefix(), *(str(arg) for arg in args))
    return argv, _git_env_with_drivers(
        drivers or GitDrivers(), unsafe_message=unsafe_message
    )


def is_hardened_git_invocation(
    argv: Sequence[str], env: Mapping[str, str]
) -> bool:
    """Whether ``argv``/``env`` came from ``hardened_git_invocation``.

    A runner that executes Git somewhere else (the Docker runtime) checks
    this instead of re-applying pins, so there is still one implementation:
    an invocation that skipped it is refused, never silently patched. A later
    ``-c`` could outrank a pin, so none is accepted after the pinned prefix.
    """
    pins = git_pinned_config_args()
    if len(argv) < 1 + len(pins) or tuple(str(a) for a in argv[1 : 1 + len(pins)]) != pins:
        return False
    for arg in (str(arg) for arg in argv[1 + len(pins) :]):
        # ``--config-env`` sets config from another variable, ``--exec-path``
        # picks the helper programs Git runs: both bypass the pins.
        if arg == "-c" or arg.startswith(("--config-env", "--exec-path")):
            return False
    # The environment is exactly what the builder emits: its fixed keys with
    # their values (``PATH`` may differ where Git runs), plus driver
    # overrides in the config slots. Any other key - ``GIT_DIR``,
    # ``GIT_WORK_TREE``, ``GIT_INDEX_FILE``, ``GIT_EXTERNAL_DIFF``,
    # ``GIT_OBJECT_DIRECTORY``, ``GIT_CONFIG_PARAMETERS`` ... - would aim Git
    # at other state or start another program.
    required = _git_env()
    if not all(
        env.get(key) == value for key, value in required.items() if key != "PATH"
    ):
        return False
    allowed = set(required)
    if "GIT_CONFIG_COUNT" in env:
        count_text = env["GIT_CONFIG_COUNT"]
        if not count_text.isdigit() or count_text != str(int(count_text)):
            return False
        allowed.add("GIT_CONFIG_COUNT")
        for index in range(int(count_text)):
            key = env.get(f"GIT_CONFIG_KEY_{index}")
            value = env.get(f"GIT_CONFIG_VALUE_{index}")
            if key is None or value is None or not is_neutral_driver_override(key, value):
                return False
            allowed.update((f"GIT_CONFIG_KEY_{index}", f"GIT_CONFIG_VALUE_{index}"))
    return set(env) <= allowed


def _git_binary() -> str:
    binary = shutil.which("git", path=os.defpath)
    if binary is None:
        raise GitDeliveryError("Git workspace status is unknown because Git is unavailable.")
    return str(Path(binary).absolute())


def _git_runtime_read_paths() -> tuple[str, ...]:
    # macOS's /usr/bin/git is an xcrun shim. Resolve only Apple's selected
    # developer directory through its fixed system tool; never consult PATH or
    # inspect a PATH-selected Git executable's parent directories.
    from src.llms.native_cli.isolation import selected_developer_dir

    return selected_developer_dir()


def _submodule_gitdir(workspace_root: Path) -> Path | None:
    """The gitdir of a submodule checkout, when ``workspace_root`` is one.

    A submodule's ``.git`` file points into its superproject's
    ``.git/modules``; the superproject must be an ancestor of the checkout
    and own a real ``.git`` directory, so a pointer into some other
    repository's metadata is not a submodule.
    """
    pointer = workspace_root / ".git"
    try:
        if not stat.S_ISREG(pointer.lstat().st_mode):
            return None
        text = _read_small_regular(pointer).decode("utf-8", errors="strict").strip()
    except (OSError, UnicodeError):
        return None
    if not text.startswith("gitdir: "):
        return None
    raw = Path(text[8:])
    gitdir = (raw if raw.is_absolute() else workspace_root / raw).resolve()
    if not gitdir.is_dir():
        return None
    for ancestor in workspace_root.parents:
        dot_git = ancestor / ".git"
        if dot_git.is_dir() and not dot_git.is_symlink():
            return gitdir if gitdir.is_relative_to((dot_git / "modules").resolve()) else None
    return None


def _git_metadata_read_paths(cwd: str) -> tuple[str, ...]:
    """Allow only a registered linked worktree's reciprocal Git metadata.

    A submodule checkout gets its own gitdir (inside the superproject's
    ``.git/modules``) only; it has no separate common directory.
    """
    workspace_root = _workspace_identity_root(cwd)
    pointer = workspace_root / ".git"
    try:
        pointer_info = pointer.lstat()
    except OSError as exc:
        raise GitDeliveryError("Git workspace status is unknown because it could not be checked safely.") from exc
    if stat.S_ISDIR(pointer_info.st_mode):
        return ()
    if not stat.S_ISREG(pointer_info.st_mode):
        raise GitDeliveryError("Git workspace status is unknown because it could not be checked safely.")
    submodule = _submodule_gitdir(workspace_root)
    if submodule is not None and not (submodule / "commondir").exists():
        return (str(submodule),)
    try:
        pointer_text = _read_small_regular(pointer).decode("utf-8", errors="strict").strip()
        if not pointer_text.startswith("gitdir: "):
            raise ValueError("invalid gitdir pointer")
        raw_gitdir = Path(pointer_text[8:])
        gitdir = (workspace_root / raw_gitdir).resolve() if not raw_gitdir.is_absolute() else raw_gitdir.resolve()
        common_record = _read_small_regular(gitdir / "commondir").decode("utf-8", errors="strict").strip()
        raw_common = Path(common_record)
        common_dir = (gitdir / raw_common).resolve() if not raw_common.is_absolute() else raw_common.resolve()
        backlink = _read_small_regular(gitdir / "gitdir").decode("utf-8", errors="strict").strip()
        if (
            not gitdir.is_relative_to(common_dir / "worktrees")
            or Path(backlink).resolve() != pointer.resolve()
            or not common_dir.is_dir()
        ):
            raise ValueError("unregistered linked worktree")
    except (OSError, UnicodeError, ValueError) as exc:
        raise GitDeliveryError("Git workspace status is unknown because it could not be checked safely.") from exc
    return (str(gitdir), str(common_dir))


_GIT_OUTPUT_LIMIT_BYTES = 1024 * 1024
#: Cap for the listing reads (status, ls-files, ls-tree, worktree list,
#: name-only diffs). Their output grows with the number of paths, about 100
#: bytes per ``ls-tree -l`` entry, so the 1 MB default stopped every Ticket on
#: a repository past roughly 10k files (N56 follow-up). 64 MB admits about
#: 600k paths and is still a fixed bound: a listing past it fails closed.
_GIT_LISTING_OUTPUT_LIMIT_BYTES = 64 * 1024 * 1024
_GIT_OUTPUT_READ_BYTES = 64 * 1024
#: A NUL-separated record (one path entry) longer than this is not a listing
#: record Git produces for a real path; the incremental parser refuses it
#: rather than buffering an unbounded tail.
_GIT_RECORD_LIMIT_BYTES = 64 * 1024

_GIT_LISTING_COMMANDS = frozenset(
    {
        ("worktree", "list", "--porcelain"),
        ("status", "--porcelain", "--untracked-files=all"),
        ("status", "--porcelain", "--untracked-files=all", "--ignore-submodules=all"),
        ("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all"),
        ("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=dirty"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
        ("ls-files", "--stage", "-z"),
        ("ls-tree", "-r", "-l", "-z", "HEAD"),
        ("diff", "--name-only", "--diff-filter=U"),
    }
)


def _is_listing_command(args: Sequence[str]) -> bool:
    command = tuple(args)
    if command in _GIT_LISTING_COMMANDS:
        return True
    # Every ``ls-files`` form prints one record per path and nothing else:
    # R4-2's index-flag scan (``-v``) and its untracked / ignored expansions
    # (``--others --exclude-per-directory``) scale with the repository the
    # same way the forms above do.
    if command[:1] == ("ls-files",):
        return True
    if _is_committed_paths_command(command):
        return True
    # ``diff --name-only --no-ext-diff --no-textconv <range> --``: paths only.
    return len(command) == 6 and command[:4] == (
        "diff", "--name-only", "--no-ext-diff", "--no-textconv"
    )


#: The collab report's committed-path listing: NUL-separated, every path a
#: commit touched (rename and copy detection off, so a move lists its source
#: as well as its destination, and a deletion is listed).
_COMMITTED_PATHS_FLAGS = (
    "--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv"
)


def _is_committed_paths_command(command: tuple[str, ...]) -> bool:
    """``diff <flags> <base...head> --`` with exactly ``_COMMITTED_PATHS_FLAGS``."""
    return (
        len(command) == 8
        and command[0] == "diff"
        and command[1:6] == _COMMITTED_PATHS_FLAGS
        and command[7] == "--"
        and _safe_revision_range(command[6])
    )


def _effective_output_limit(args: Sequence[str], output_limit: int) -> int:
    """The default small cap, raised for listing reads that scale with paths.

    An explicit cap other than the default is always honoured as given.
    """
    if output_limit == _GIT_OUTPUT_LIMIT_BYTES and _is_listing_command(args):
        return _GIT_LISTING_OUTPUT_LIMIT_BYTES
    return output_limit


class _GitOutputLimitExceeded(Exception):
    pass


class _TruncatedGitOutput(subprocess.CompletedProcess[bytes]):
    """Git was stopped at the cap; ``stdout``/``stderr`` are the bounded prefix."""


@dataclass(frozen=True)
class GitOutputPrefix:
    """At most ``limit`` bytes of one read-only Git command's output.

    ``truncated`` means Git produced more and was killed at the cap, so
    ``returncode`` is None: the prefix is bounded evidence, not a result.
    """

    returncode: int | None
    stdout: bytes
    stderr: bytes
    truncated: bool


class _NulRecords:
    """Split a NUL-terminated stream into records as chunks arrive."""

    def __init__(self, on_record: Callable[[bytes], None]) -> None:
        self._on_record = on_record
        self._tail = b""

    def feed(self, chunk: bytes) -> None:
        *records, self._tail = (self._tail + chunk).split(b"\0")
        if len(self._tail) > _GIT_RECORD_LIMIT_BYTES:
            raise GitDeliveryError(
                "Git workspace status is unknown because it could not be checked safely."
            )
        for record in records:
            if record:
                self._on_record(record)

    def finish(self) -> None:
        if self._tail:
            self._on_record(self._tail)
        self._tail = b""


def _kill_git_process(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        with contextlib.suppress(OSError):
            process.kill()
    process.wait()


def _stream_git_output(
    argv: Sequence[str],
    *,
    cwd: str | None,
    env: dict[str, str],
    timeout: float,
    output_limit: int = _GIT_OUTPUT_LIMIT_BYTES,
    stdout_sink: Callable[[bytes], None] | None = None,
    truncate: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """Read Git's output in bounded chunks, never more than ``output_limit``.

    ``stdout_sink`` takes stdout chunk by chunk instead of keeping it (the
    returned ``stdout`` is then empty), so a listing can be parsed without
    holding it whole. ``truncate`` returns the prefix read so far, marked by
    ``_TruncatedGitOutput``, instead of failing at the cap.
    """
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            assert process.stdout is not None and process.stderr is not None
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                for key, _events in selector.select(remaining):
                    chunk = os.read(key.fileobj.fileno(), _GIT_OUTPUT_READ_BYTES)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    room = output_limit - total
                    overflow = len(chunk) > room
                    if overflow:
                        chunk = chunk[: max(0, room)]
                    total += len(chunk)
                    if key.data == "stdout" and stdout_sink is not None:
                        if chunk:
                            stdout_sink(chunk)
                    else:
                        outputs[key.data].extend(chunk)
                    if overflow:
                        raise _GitOutputLimitExceeded
            returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except _GitOutputLimitExceeded:
        _kill_git_process(process)
        if truncate:
            return _TruncatedGitOutput(
                argv,
                -signal.SIGKILL,
                stdout=bytes(outputs["stdout"]),
                stderr=bytes(outputs["stderr"]),
            )
        raise GitDeliveryError(
            f"Git output exceeded the {output_limit}-byte limit."
        ) from None
    except BaseException:
        _kill_git_process(process)
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    return subprocess.CompletedProcess(
        argv,
        returncode,
        stdout=bytes(outputs["stdout"]),
        stderr=bytes(outputs["stderr"]),
    )


def _check_git_output_limit(
    result: subprocess.CompletedProcess[bytes],
    output_limit: int,
    *,
    stdout_sink: Callable[[bytes], None] | None = None,
    truncate: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """Apply the streaming cap's contract to an injected runner's result."""
    stdout = result.stdout or b""
    stderr = result.stderr or b""
    if len(stdout) + len(stderr) > output_limit:
        if not truncate:
            raise GitDeliveryError(f"Git output exceeded the {output_limit}-byte limit.")
        stderr = stderr[:output_limit]
        return _TruncatedGitOutput(
            result.args,
            -signal.SIGKILL,
            stdout=stdout[: output_limit - len(stderr)],
            stderr=stderr,
        )
    if stdout_sink is not None:
        if stdout:
            stdout_sink(stdout)
        return subprocess.CompletedProcess(
            result.args, result.returncode, stdout=b"", stderr=stderr
        )
    return result


def _run_git(
    cwd: str,
    args: list[str],
    *,
    timeout: float,
    drivers: GitDrivers | None = None,
    output_limit: int = _GIT_OUTPUT_LIMIT_BYTES,
    stdout_sink: Callable[[bytes], None] | None = None,
    truncate: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    # N56: listing commands get the 64 MB cap, everything else 1 MB.
    output_limit = _effective_output_limit(args, output_limit)
    from src.llms.native_cli.isolation import NativeCliUnavailable

    try:
        argv, env = hardened_git_invocation(
            _git_binary(), ["-C", cwd, *args], drivers=drivers
        )
        runner = _GIT_COMMAND_RUNNER.get()
        if runner is not None:
            return _check_git_output_limit(
                runner(argv, cwd=cwd, timeout=timeout, env=env),
                output_limit,
                stdout_sink=stdout_sink,
                truncate=truncate,
            )
        from src.llms.native_cli.isolation import (
            WORKER_GIT_ENV_NAMES,
            current_worker_isolation,
            prepare_process,
            worker_isolation,
        )

        current = current_worker_isolation()
        if current is None:
            workspace = _workspace_identity_root(cwd)
            isolation_root = Path(
                os.environ.get("CONDUCTOR_WORKER_ISOLATION_ROOT")
                or Path.home() / ".cache" / "conductor" / "native-cli" / "git-probe"
            )
            workspace_id = hashlib.sha256(os.fsencode(workspace)).hexdigest()
            context = worker_isolation(
                workspace_id=workspace_id,
                root=isolation_root,
                workdir=workspace,
                env_names=frozenset(),
                read_only=True,
                workspace_root=workspace,
            )
        else:
            context = contextlib.nullcontext(current)
        with context:
            wrapped, isolated_env, isolated_cwd = prepare_process(
                tuple(argv),
                cwd,
                env,
                network_disabled=True,
                read_only_override=True,
                extra_runtime_read_paths=(
                    *_git_runtime_read_paths(),
                    *_git_metadata_read_paths(cwd),
                ),
            )
            if isolated_env is None:
                raise GitDeliveryError(_UNSAFE_STATUS)
            # prepare_process uses the provider allowlist and therefore drops
            # inherited GIT_* state. Restore only fixed scrubbed Git settings.
            # A read-only override still leaves validated non-workspace
            # runtime_write_paths writable; Git receives no network and those
            # paths are only the active policy's explicit grants.
            isolated_env.update(env)
            # A Ticket run's object quarantine (N67) is the Worker's, not the
            # host check's: the host reads the real store only.
            for name in WORKER_GIT_ENV_NAMES:
                isolated_env.pop(name, None)
            return _stream_git_output(
                wrapped,
                cwd=isolated_cwd,
                env=isolated_env,
                timeout=timeout,
                output_limit=output_limit,
                stdout_sink=stdout_sink,
                truncate=truncate,
            )
    except NativeCliUnavailable as exc:
        raise GitDeliveryError(
            "Git workspace status is unknown because its isolation boundary is unavailable."
        ) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitDeliveryError(_UNSAFE_STATUS) from exc


def _run_workspace_git(
    cwd: str,
    args: Sequence[str],
    *,
    timeout: float,
    drivers: GitDrivers | None = None,
    output_limit: int = _GIT_OUTPUT_LIMIT_BYTES,
    stdout_sink: Callable[[bytes], None] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return _run_git(
        cwd,
        list(args),
        timeout=timeout,
        drivers=drivers,
        output_limit=output_limit,
        stdout_sink=stdout_sink,
    )


def _public_git_command_is_safe(command: tuple[str, ...]) -> bool:
    if not command:
        return False
    if command == ("branch", "--show-current"):
        return True
    if command == ("worktree", "list", "--porcelain"):
        return True
    if command in {
        ("rev-parse", "HEAD"),
        ("rev-parse", "--show-toplevel"),
        ("rev-parse", "--path-format=absolute", "--absolute-git-dir", "--git-common-dir"),
    }:
        return True
    # Task 6 needs both review evidence and changed-path evidence across an
    # exact revision range. Keep the flags and positional range in fixed
    # positions so no worker-controlled diff driver, textconv, or pathspec can
    # be selected by the caller.
    if len(command) == 7 and command[0] == "diff":
        if command[1:5] == (
            "--patch-with-stat", "--no-color", "--no-ext-diff", "--no-textconv"
        ) and command[6] == "--" and _safe_revision_range(command[5]):
            return True
    if len(command) == 6 and command[0] == "diff":
        if (
            command[1:4] == ("--name-only", "--no-ext-diff", "--no-textconv")
            and command[5] == "--"
            and _safe_revision_range(command[4])
        ):
            return True
    if _is_committed_paths_command(command):
        return True
    if len(command) == 3 and command[:2] == ("rev-parse", "--verify"):
        return not command[2].startswith("-") and "\x00" not in command[2]
    # Commit counting for the no-new-commit check: only a range of two full
    # object ids, so no ref name, option, or pathspec reaches git.
    if len(command) == 3 and command[:2] == ("rev-list", "--count"):
        parts = command[2].split("..")
        return len(parts) == 2 and all(
            len(part) in (40, 64) and all(ch in "0123456789abcdef" for ch in part)
            for part in parts
        )
    if command in {
        ("status", "--porcelain", "--untracked-files=all"),
        ("status", "--porcelain", "--untracked-files=all", "--ignore-submodules=all"),
        ("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
        ("ls-files", "--stage", "-z"),
        # Blob sizes at HEAD, to bound a dirty-tree diff before reading it.
        ("ls-tree", "-r", "-l", "-z", "HEAD"),
        ("diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD"),
        ("diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", "HEAD"),
        ("diff", "--name-only", "--diff-filter=U"),
        # The commit time of HEAD, so a caller can measure how long the branch
        # has been quiet. One fixed revision and one fixed format: no ref
        # name, option or pathspec reaches git, and no patch is produced.
        ("show", "-s", "--format=%ct", "HEAD"),
    }:
        return True
    # The summary implementation uses this one exact, read-only stats shape;
    # each pathspec is top-anchored and literal. The base is HEAD or one full
    # object id (the frozen HEAD a delivery is measured against): a full
    # object id is no ref name, no option and no pathspec, so it selects
    # nothing HEAD would not.
    return (
        len(command) >= 8
        and command[:5]
        == ("diff", "--stat", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all")
        and (command[5] == "HEAD" or _is_object_id(command[5]))
        and command[6] == "--"
        and all(path.startswith(":(top,literal)") for path in command[7:])
    )


def _safe_revision_range(value: str) -> bool:
    """Accept one conservative `base...revision` token, never a pathspec."""
    if value.count("...") != 1 or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return False
    parts = value.split("...")
    if len(parts) != 2:
        return False
    for ref in parts:
        if not ref or ref.startswith("-") or not all(
            (ch.isascii() and (ch.isalnum() or ch in "._/-"))
            or (not ch.isascii() and ch.isalnum())
            for ch in ref
        ):
            return False
        if (
            ref.startswith("/") or ref.endswith(("/", ".", ".lock"))
            or "//" in ref or ".." in ref or "@{" in ref
            or any(part in {"", ".", ".."} for part in ref.split("/"))
        ):
            return False
    return True


def run_isolated_git_readonly(
    cwd: str,
    args: Sequence[str],
    *,
    timeout: float = 5.0,
    output_limit: int = _GIT_OUTPUT_LIMIT_BYTES,
) -> subprocess.CompletedProcess[bytes]:
    """Run one explicitly allowlisted read-only Git form inside isolation.

    Listing forms called with the default cap get the listing cap instead
    (see ``_GIT_LISTING_OUTPUT_LIMIT_BYTES``); output past the cap fails.
    """
    command = tuple(args)
    if not _public_git_command_is_safe(command):
        raise GitDeliveryError(
            "Git workspace status is unknown because it could not be checked safely."
        )
    return _run_workspace_git(
        cwd,
        command,
        timeout=timeout,
        drivers=_filter_names(cwd),
        output_limit=output_limit,
    )


def run_isolated_git_readonly_prefix(
    cwd: str,
    args: Sequence[str],
    *,
    limit: int,
    timeout: float = 5.0,
) -> GitOutputPrefix:
    """Read at most ``limit`` bytes of one allowlisted read-only Git form.

    For evidence that is trimmed anyway (a cross-review diff): Git is killed
    at the cap and the prefix comes back marked ``truncated`` rather than
    failing the read. A truncated read with no stdout (only stderr) is not
    evidence and fails.
    """
    command = tuple(args)
    if limit <= 0 or not _public_git_command_is_safe(command):
        raise GitDeliveryError(
            "Git workspace status is unknown because it could not be checked safely."
        )
    result = _run_git(
        cwd,
        list(command),
        timeout=timeout,
        drivers=_filter_names(cwd),
        output_limit=limit,
        truncate=True,
    )
    truncated = isinstance(result, _TruncatedGitOutput)
    if truncated and not result.stdout:
        raise GitDeliveryError(f"Git output exceeded the {limit}-byte limit.")
    return GitOutputPrefix(
        returncode=None if truncated else result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        truncated=truncated,
    )


def run_isolated_git_mutation(
    cwd: str,
    args: Sequence[str],
    *,
    repository_root: str,
    worktrees_root: str,
    timeout: float = 5.0,
    output_limit: int = _GIT_OUTPUT_LIMIT_BYTES,
) -> subprocess.CompletedProcess[bytes]:
    """Run a narrow local Git mutation in a writable, network-disabled boundary.

    The only extra writable mount is this repository's canonical Git metadata;
    the selected worktree (or a target parent for worktree add/remove) is the
    sandbox workspace. Push and arbitrary Git commands are never accepted.
    """
    from src.llms.native_cli.isolation import current_worker_isolation

    command = tuple(args)
    repo = Path(repository_root).expanduser().resolve(strict=True)
    root = Path(worktrees_root).expanduser().resolve(strict=True)
    workdir = Path(cwd).expanduser().resolve(strict=True)
    raw_repo = canonical_system_path(repository_root)
    raw_root = canonical_system_path(worktrees_root)
    if (
        not repo.is_dir()
        or not root.is_dir()
        or repo == root
        or repo in root.parents
        or root in repo.parents
        or any(part.is_symlink() for part in (raw_repo, *raw_repo.parents))
        or any(part.is_symlink() for part in (raw_root, *raw_root.parents))
    ):
        raise GitDeliveryError("Git mutation is unavailable because its paths are unsafe.")
    # Anchor every writable metadata grant to the configured repository root.
    # That root is trusted by the caller; its linked-worktree pointer is
    # validated reciprocally before its common directory becomes the anchor.
    repo_pointer = repo / ".git"
    try:
        trusted_metadata = _git_metadata_read_paths(str(repo))
        if trusted_metadata:
            trusted_common = Path(trusted_metadata[1]).resolve(strict=True)
            trusted_gitdir = Path(trusted_metadata[0]).resolve(strict=True)
        else:
            if not repo_pointer.is_dir() or repo_pointer.is_symlink():
                raise ValueError("configured repository metadata is not a directory")
            trusted_common = repo_pointer.resolve(strict=True)
            trusted_gitdir = trusted_common
        metadata = _git_metadata_read_paths(str(workdir))
        if workdir == repo:
            target_metadata = trusted_metadata
        else:
            target_metadata = metadata
            if not target_metadata or Path(target_metadata[1]).resolve(strict=True) != trusted_common:
                raise ValueError("target worktree does not share configured repository metadata")
    except (GitDeliveryError, OSError, ValueError, IndexError) as exc:
        raise GitDeliveryError(
            "Git mutation is unavailable because its repository identity changed."
        ) from exc
    if workdir != repo and not workdir.is_relative_to(root):
        raise GitDeliveryError("Git mutation is unavailable outside the dispatch worktree tree.")
    if current_worker_isolation() is not None:
        raise GitDeliveryError("Git mutation requires its own bounded workspace isolation.")

    if command == ("worktree", "prune"):
        sandbox_workdir = root
        work_tree = repo
    elif command[:2] == ("worktree", "add"):
        if len(command) == 6 and command[:3] == ("worktree", "add", "-b"):
            target_index = 4
        elif len(command) == 5 and command[:3] == ("worktree", "add", "--detach"):
            target_index = 3
        else:
            target_index = 2
        if not _valid_task6_worktree_add(command, target_index, root):
            raise GitDeliveryError("Git mutation command is not allowlisted.")
        target = Path(command[target_index]).resolve(strict=False)
        sandbox_workdir = target.parent
        work_tree = repo
    elif command[:2] == ("worktree", "remove"):
        target_index = 3 if len(command) == 4 and command[2] == "--force" else 2
        if not _valid_task6_worktree_remove(command, target_index, root):
            raise GitDeliveryError("Git mutation command is not allowlisted.")
        target_lexical = Path(command[target_index])
        target = target_lexical.resolve(strict=True)
        if target_lexical.is_symlink() or not target.is_dir():
            raise GitDeliveryError("Git mutation target is not a regular worktree directory.")
        try:
            _git_metadata_read_paths(str(target))
        except GitDeliveryError as exc:
            raise GitDeliveryError("Git mutation target is not a registered worktree.") from exc
        sandbox_workdir = target.parent
        work_tree = repo
    elif len(command) == 4 and command[:2] == ("branch", "-m"):
        if not _safe_git_ref(command[2]) or not _safe_git_ref(command[3]):
            raise GitDeliveryError("Git mutation command is not allowlisted.")
        sandbox_workdir = workdir
        work_tree = workdir
    elif command == ("merge", "--abort"):
        sandbox_workdir = workdir
        work_tree = workdir
    elif (
        len(command) == 4
        and command[:3] == ("merge", "--no-ff", "--no-edit")
        and _safe_git_ref(command[3])
    ):
        sandbox_workdir = workdir
        work_tree = workdir
    else:
        raise GitDeliveryError("Git mutation command is not allowlisted.")

    if not sandbox_workdir.is_dir() or sandbox_workdir.is_symlink():
        raise GitDeliveryError("Git mutation workspace is unavailable or unsafe.")
    trusted_root_branch_rename = (
        sandbox_workdir == repo and command[:2] == ("branch", "-m")
    )
    if (
        sandbox_workdir != root
        and not sandbox_workdir.is_relative_to(root)
        and not trusted_root_branch_rename
    ):
        raise GitDeliveryError("Git mutation workspace is outside the dispatch tree.")
    if work_tree != repo and not work_tree.is_relative_to(root):
        raise GitDeliveryError("Git mutation worktree is outside the dispatch tree.")

    common_dir = trusted_common
    git_dir = (
        Path(target_metadata[0]).resolve(strict=True)
        if target_metadata
        else trusted_gitdir
    )
    if not common_dir.is_dir() or not git_dir.is_dir():
        raise GitDeliveryError("Git mutation metadata is unavailable or unsafe.")

    # The include gate and filter discovery run first, in the read-only
    # boundary: a repository whose config pulls in another file is refused, and
    # every filter, merge and diff driver it names - included ones too - is
    # emptied below, so the merge never starts a repository-chosen program.
    drivers = _filter_names(str(workdir))
    invocation, env = hardened_git_invocation(
        _git_binary(),
        ("--git-dir", str(git_dir), "--work-tree", str(work_tree), *command),
        drivers=drivers,
        unsafe_message="Git mutation is unavailable because its driver configuration is unsafe.",
    )
    from src.llms.native_cli.isolation import (
        NativeCliUnavailable,
        prepare_process,
        worker_isolation,
    )

    mutation_root = Path(
        os.environ.get("CONDUCTOR_WORKER_ISOLATION_ROOT")
        or Path.home() / ".cache" / "conductor" / "auto-dispatch-git-mutations"
    )
    workspace_id = hashlib.sha256(
        os.fsencode(f"{repo}\0{root}\0{sandbox_workdir}")
    ).hexdigest()
    try:
        if current_worker_isolation() is not None:
            raise GitDeliveryError("Git mutation requires its own bounded workspace isolation.")
        # Git runs against the real Git directory, so HEAD, refs, the index
        # and a new worktree's back-pointers land where Git expects them. The
        # only writable grant is the shared common directory; a linked
        # worktree's private gitdir lives inside it (``worktrees/<id>``).
        with worker_isolation(
            workspace_id=workspace_id,
            root=mutation_root,
            workdir=sandbox_workdir,
            env_names=frozenset(),
            runtime_write_paths=(str(common_dir),),
            read_only=False,
            workspace_root=sandbox_workdir,
        ):
            wrapped, isolated_env, isolated_cwd = prepare_process(
                invocation,
                str(sandbox_workdir),
                env,
                network_disabled=True,
                read_only_override=False,
                extra_runtime_read_paths=_git_runtime_read_paths(),
            )
            if isolated_env is None:
                raise GitDeliveryError("Git mutation isolation is unavailable.")
            isolated_env.update(env)
            return _stream_git_output(
                wrapped,
                cwd=isolated_cwd,
                env=isolated_env,
                timeout=timeout,
                output_limit=output_limit,
            )
    except NativeCliUnavailable as exc:
        raise GitDeliveryError("Git mutation isolation is unavailable.") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitDeliveryError("Git mutation could not be completed safely.") from exc


def _safe_git_ref(value: str) -> bool:
    if not value or value.startswith("-") or not all(
        character.isascii() and (character.isalnum() or character in "._/-")
        for character in value
    ):
        return False
    return (
        not value.startswith("/")
        and not value.endswith(("/", ".", ".lock"))
        and "//" not in value
        and ".." not in value
        and "@{" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _valid_task6_worktree_add(
    command: tuple[str, ...], target_index: int, worktrees_root: Path
) -> bool:
    if len(command) == 6 and command[:3] == ("worktree", "add", "-b"):
        if not _safe_git_ref(command[3]) or not _safe_git_ref(command[5]):
            return False
    elif len(command) == 5 and command[:3] == ("worktree", "add", "--detach"):
        if not _safe_git_ref(command[4]):
            return False
    elif len(command) == 4 and command[:2] == ("worktree", "add"):
        if not _safe_git_ref(command[3]):
            return False
    else:
        return False
    path = canonical_system_path(command[target_index])
    if (
        not path.is_absolute()
        or path.is_symlink()
        or any(part.is_symlink() for part in (path.parent, *path.parent.parents))
    ):
        return False
    resolved = path.resolve(strict=False)
    return resolved.is_relative_to(worktrees_root) and resolved != worktrees_root


def _valid_task6_worktree_remove(
    command: tuple[str, ...], target_index: int, worktrees_root: Path
) -> bool:
    if not (
        len(command) == 3 and command[:2] == ("worktree", "remove")
        or len(command) == 4 and command[:3] == ("worktree", "remove", "--force")
    ):
        return False
    path = canonical_system_path(command[target_index])
    return (
        path.is_absolute()
        and not path.is_symlink()
        and not any(part.is_symlink() for part in (path.parent, *path.parent.parents))
        and path.resolve(strict=False).is_relative_to(worktrees_root)
    )


#: Every key of the repository's own config files as Git parses them (a
#: missing ``config.worktree`` lists nothing), with includes left unexpanded, so an ``include.*`` / ``includeif.*`` key shows up as itself.
#: Git lower-cases section names, so ``[Include]`` and every ``includeIf``
#: condition arrive in one spelling.
_INCLUDE_CONFIG_QUERIES: tuple[list[str], ...] = (
    ["config", "--local", "--no-includes", "--null", "--name-only", "--get-regexp", "."],
    ["config", "--worktree", "--no-includes", "--null", "--name-only", "--get-regexp", "."],
)

#: The queries that name every filter, merge and diff driver Git would load.
#: ``--includes`` is deliberate: the include gate has already refused include
#: directives, and a file included between the gate and the command still
#: cannot keep a driver of its own out of the override list.
_DRIVER_KEY_PATTERN = r"^(filter|merge|diff)\."
_FILTER_CONFIG_QUERIES: tuple[list[str], ...] = (
    ["config", "--includes", "--null", "--name-only", "--get-regexp", _DRIVER_KEY_PATTERN],
    ["config", "--worktree", "--includes", "--null", "--name-only", "--get-regexp", _DRIVER_KEY_PATTERN],
)


def _config_keys(run, query: list[str]) -> list[str]:
    """The config keys ``query`` lists; an unused worktree scope lists none."""
    result = run(query, 5)
    if result.returncode not in (0, 1):
        if "--worktree" not in query:
            raise GitDeliveryError(_UNSAFE_STATUS)
        enabled = run(["config", "--local", "--bool", "extensions.worktreeConfig"], 5)
        if enabled.returncode == 1:
            return []
        if enabled.returncode != 0:
            raise GitDeliveryError(_UNSAFE_STATUS)
        if enabled.stdout.strip().lower() != b"true":
            return []
        raise GitDeliveryError(_UNSAFE_STATUS)
    keys: list[str] = []
    for entry in result.stdout.split(b"\0"):
        if not entry:
            continue
        try:
            keys.append(entry.decode("utf-8", errors="strict"))
        except UnicodeDecodeError as exc:
            raise GitDeliveryError(_UNSAFE_STATUS) from exc
    return keys


def _filter_names_with(run, cwd: str) -> GitDrivers:
    """Refuse include directives, then name every driver to neutralise.

    Every public entry point (read-only checks, snapshots, mutations) calls
    this before its first real Git command, so it is the include gate for
    all of them. ``run`` is the caller's Git runner.
    """
    for query in _INCLUDE_CONFIG_QUERIES:
        for key in _config_keys(run, query):
            section = key.partition(".")[0].lower()
            if section in {"include", "includeif"}:
                raise GitDeliveryError(_INCLUDES_REFUSED)
    names: dict[str, set[str]] = {"filter": set(), "merge": set(), "diff": set()}
    for query in _FILTER_CONFIG_QUERIES:
        for key in _config_keys(run, query):
            prefix, _, rest = key.partition(".")
            kind = names.get(prefix.lower())
            if kind is None:
                continue
            # ``merge.renormalize`` / ``diff.external`` have no subsection
            # and name no driver; only ``<kind>.<name>.<setting>`` does.
            driver_name, separator, _setting = rest.rpartition(".")
            if separator and driver_name:
                kind.add(driver_name)
    return GitDrivers(
        filters=tuple(sorted(names["filter"])),
        merges=tuple(sorted(names["merge"])),
        diffs=tuple(sorted(names["diff"])),
    )


def _filter_names(cwd: str) -> GitDrivers:
    def run(args, timeout):
        return _run_git(cwd, list(args), timeout=timeout)

    return _filter_names_with(run, cwd)


def _has_git_marker(cwd: str) -> bool:
    path = Path(cwd).resolve()
    if not path.is_dir():
        raise GitDeliveryError("Git workspace status is unknown because its directory is unavailable.")
    return any((parent / ".git").exists() for parent in (path, *path.parents))


def _root(cwd: str, drivers: GitDrivers) -> str | None:
    if not _has_git_marker(cwd):
        return None
    result = _run_workspace_git(
        cwd, ["rev-parse", "--show-toplevel"], timeout=5, drivers=drivers
    )
    if result.returncode != 0:
        raise GitDeliveryError("Git workspace status is unknown because it could not be checked safely.")
    return os.fsdecode(result.stdout.strip())


def _gitdir_identity(
    cwd: str, root: str, drivers: GitDrivers, *, allow_external: bool
) -> tuple[str, Path, Path]:
    """The workspace's Git-directory identity, its gitdir and common directory."""
    result = _run_workspace_git(
        cwd,
        ["rev-parse", "--path-format=absolute", "--absolute-git-dir", "--git-common-dir"],
        timeout=5,
        drivers=drivers,
    )
    if result.returncode != 0:
        raise GitDeliveryError("Git workspace status is unknown because it could not be checked safely.")
    lines = result.stdout.decode("utf-8", errors="strict").splitlines()
    if len(lines) != 2:
        raise GitDeliveryError("Git workspace status is unknown because it could not be checked safely.")
    gitdir, common_dir = Path(lines[0]).resolve(), Path(lines[1]).resolve()
    # Existing linked worktrees can have a shared common dir outside the
    # workspace. Accept only Git's registered worktree layout with reciprocal
    # pointers. A plain .git file/symlink cannot authorize an arbitrary repo.
    workspace_root = Path(root).resolve()
    if not gitdir.is_relative_to(workspace_root) or not common_dir.is_relative_to(
        workspace_root
    ):
        if not allow_external:
            raise GitDeliveryError("Git workspace status is unknown because it could not be checked safely.")
        if gitdir == common_dir and _submodule_gitdir(workspace_root) == gitdir:
            return hashlib.sha256(
                os.fsencode(gitdir) + b"\0" + os.fsencode(common_dir)
            ).hexdigest(), gitdir, common_dir
        worktrees = common_dir / "worktrees"
        backlink = gitdir / "gitdir"
        pointer = workspace_root / ".git"
        try:
            backlink_text = _read_small_regular(backlink).decode().strip()
            pointer_text = _read_small_regular(pointer).decode().strip()
        except (OSError, UnicodeError) as exc:
            raise GitDeliveryError("Git workspace status is unknown because it could not be checked safely.") from exc
        if (
            not gitdir.is_relative_to(worktrees)
            or Path(backlink_text).resolve() != pointer.resolve()
            or not pointer_text.startswith("gitdir: ")
            or Path(pointer_text[8:]).resolve() != gitdir
        ):
            raise GitDeliveryError("Git workspace status is unknown because it could not be checked safely.")
    return hashlib.sha256(
        os.fsencode(gitdir) + b"\0" + os.fsencode(common_dir)
    ).hexdigest(), gitdir, common_dir


def _read_small_regular(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise OSError("unexpected Git metadata file")
        return os.read(fd, 4097)
    finally:
        os.close(fd)


def _status_rows(cwd: str, drivers: GitDrivers) -> list[tuple[bytes, str]]:
    # ``dirty`` (not ``all``) still reports a submodule whose checked-out
    # commit differs from the recorded one (N69): Git reads the submodule's
    # HEAD itself. What it leaves out - edits inside the submodule's own
    # worktree - would need a Git process run under the submodule's own,
    # unchecked configuration, so they stay out.
    result = _run_workspace_git(
        cwd,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=dirty"],
        timeout=10,
        drivers=drivers,
    )
    if result.returncode != 0:
        raise GitDeliveryError("Git workspace status is unknown because it could not be checked safely.")
    raw = result.stdout.split(b"\0")
    rows: list[tuple[bytes, str]] = []
    index = 0
    while index < len(raw):
        record = raw[index]
        index += 1
        if not record:
            continue
        if len(record) < 4:
            raise GitDeliveryError("Git workspace status is unknown because it could not be checked safely.")
        status = record[:2].decode("ascii", errors="replace")
        path = record[3:]
        rows.append((path, status))
        if "R" in status or "C" in status:
            if index >= len(raw):
                raise GitDeliveryError("Git workspace status is unknown because it could not be checked safely.")
            # Porcelain -z puts the destination first, then the source.
            rows.append((raw[index], "D" if "R" in status else status))
            index += 1
    return rows


def _fingerprint(root: str, relative: bytes) -> str:
    if relative.startswith(b"/") or any(part in (b"", b".", b"..") for part in relative.split(b"/")):
        raise GitDeliveryError("Git workspace status is unknown because it could not be checked safely.")
    root_fd = os.open(
        root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    parent_fd = root_fd
    try:
        components = relative.split(b"/")
        for component in components[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = next_fd
        name = components[-1]
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return "missing"
        digest = hashlib.sha256()
        if stat.S_ISLNK(info.st_mode):
            digest.update(b"symlink\0")
            digest.update(os.fsencode(os.readlink(name, dir_fd=parent_fd)))
        elif stat.S_ISREG(info.st_mode):
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise GitDeliveryError("Git workspace status is unknown because it could not be checked safely.")
                while chunk := os.read(fd, 1024 * 1024):
                    digest.update(chunk)
            finally:
                os.close(fd)
        else:
            digest.update(f"mode:{stat.S_IFMT(info.st_mode)}".encode())
            if stat.S_ISDIR(info.st_mode):
                # A submodule checkout: its checked-out commit is what status
                # reports on (N69), so a second move after a first one is
                # still a change.
                digest.update(
                    _checkout_head_marker(os.path.join(os.fsencode(root), relative))
                )
        return digest.hexdigest()
    except (OSError, ValueError) as exc:
        raise GitDeliveryError("Git workspace status is unknown because it could not be checked safely.") from exc
    finally:
        if parent_fd != root_fd:
            os.close(parent_fd)
        os.close(root_fd)


def _read_small(path: bytes, *, dir_fd: int | None = None, limit: int = 4096) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
    except OSError:
        return b""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return b""
        return os.read(fd, limit)
    finally:
        os.close(fd)


def _checkout_head_marker(checkout: bytes) -> bytes:
    """What a nested checkout's HEAD names, read as files; empty if none.

    No Git process runs in the checkout. A ``.git`` file points at its gitdir;
    HEAD is a commit or a ``ref:``, resolved through a loose ref or the
    packed-refs. Anything unreadable is left out, so an unreadable checkout
    keeps one stable marker. Only a digest of it is kept.
    """
    dotgit = os.path.join(checkout, b".git")
    pointer = _read_small(dotgit)
    if pointer.startswith(b"gitdir:"):
        gitdir = os.path.join(checkout, pointer[len(b"gitdir:"):].strip())
    elif os.path.isdir(dotgit) and not os.path.islink(dotgit):
        gitdir = dotgit
    else:
        return b""
    head = _read_small(os.path.join(gitdir, b"HEAD")).strip()
    marker = b"\0head\0" + head
    ref = head[len(b"ref: "):] if head.startswith(b"ref: ") else b""
    if ref.startswith(b"refs/") and b".." not in ref.split(b"/"):
        common = _read_small(os.path.join(gitdir, b"commondir")).strip()
        bases = [gitdir] + ([os.path.join(gitdir, common)] if common else [])
        value = b""
        for base in bases:
            value = _read_small(os.path.join(base, ref)).strip()
            if value:
                break
        else:
            for base in bases:
                packed = _read_small(os.path.join(base, b"packed-refs"), limit=1 << 20)
                for line in packed.splitlines():
                    if line.endswith(b" " + ref):
                        value = line.partition(b" ")[0]
                        break
                if value:
                    break
        marker += b"\0ref\0" + value
    return marker


#: Files in the worktree that decide what status reports.
_RULE_FILE_NAMES = frozenset({b".gitattributes", b".gitignore"})

#: More hidden paths than this cannot be fingerprinted within a check's
#: budget; the workspace reads as unknown instead.
_HIDDEN_ROW_LIMIT = 10_000

def _nul_records(result: subprocess.CompletedProcess[bytes]) -> list[bytes]:
    if result.returncode != 0:
        raise GitDeliveryError(_UNSAFE_STATUS)
    return [record for record in result.stdout.split(b"\0") if record]


def _refuse_hiding_state(
    root: str, gitdir: Path, common_dir: Path, drivers: GitDrivers
) -> None:
    """Refuse a workspace whose Git state can make changed work read clean.

    Each of these is a way Git itself is told to look away from part of the
    worktree. None can be switched off for one command, so the verdict is
    unknown rather than a clean status Git was told to give.

    * ``core.worktree`` aiming at a different tree than the workspace. A
      submodule's gitdir names its own checkout this way, which is normal.
    * Sparse checkout leaves paths outside its patterns unchecked.
    * An index entry marked assume-unchanged (``git update-index
      --assume-unchanged``, or any entry refreshed under ``core.ignoreStat``)
      or skip-worktree is reported clean whatever the file holds;
      ``ls-files -v`` shows the first as a lower-case tag, the second as
      ``S``.
    * ``info/attributes`` outranks every ``.gitattributes`` and can make a
      changed file normalise to the committed blob (``ident``, ``eol``) once
      it is re-added.
    """
    worktree = _run_workspace_git(
        root, ["config", "--get", "core.worktree"], timeout=5, drivers=drivers
    )
    if worktree.returncode == 0:
        try:
            raw = Path(os.fsdecode(worktree.stdout.rstrip(b"\n")))
            target = (raw if raw.is_absolute() else gitdir / raw).resolve()
        except (OSError, ValueError) as exc:
            raise GitDeliveryError(_UNSAFE_STATUS) from exc
        if target != Path(root).resolve():
            raise GitDeliveryError(_UNSAFE_STATUS)
    elif worktree.returncode != 1:
        raise GitDeliveryError(_UNSAFE_STATUS)
    sparse = _run_workspace_git(
        root,
        ["config", "--bool", "--get", "core.sparseCheckout"],
        timeout=5,
        drivers=drivers,
    )
    if sparse.returncode not in (0, 1) or sparse.stdout.strip() == b"true":
        raise GitDeliveryError(_UNSAFE_STATUS)
    for record in _nul_records(
        _run_workspace_git(root, ["ls-files", "-v", "-z"], timeout=10, drivers=drivers)
    ):
        tag = record[:1]
        if len(record) < 3 or record[1:2] != b" " or tag.islower() or tag == b"S":
            raise GitDeliveryError(_UNSAFE_STATUS)
    attributes = common_dir / "info" / "attributes"
    try:
        info = attributes.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise GitDeliveryError(_UNSAFE_STATUS) from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
        raise GitDeliveryError(_UNSAFE_STATUS)
    try:
        text = attributes.read_bytes()
    except OSError as exc:
        raise GitDeliveryError(_UNSAFE_STATUS) from exc
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(b"#"):
            raise GitDeliveryError(_UNSAFE_STATUS)


def _refuse_hidden_attribute_files(
    root: str,
    index_paths: Mapping[bytes, object],
    status_rows: Sequence[tuple[bytes, str]],
) -> None:
    """Refuse an ignored, untracked ``.gitattributes`` beside tracked files.

    Git reads a directory's ``.gitattributes`` for every path under it
    whether or not the file is ignored, so an ignored one (it is in no status
    row) can make a re-added tracked file normalise to its committed blob.
    Only directories that hold tracked files matter: attributes cannot hide
    a change to an untracked file, which status lists by existence.
    """
    directories: set[bytes] = {b""}
    for path in index_paths:
        parent = path
        while b"/" in parent:
            parent = parent.rpartition(b"/")[0]
            if parent in directories:
                break
            directories.add(parent)
    attributes = {
        path
        for path in index_paths
        if path == b".gitattributes" or path.endswith(b"/.gitattributes")
    }
    _refuse_hidden_attribute_dirs(root, directories, attributes, status_rows)


def _refuse_hidden_attribute_dirs(
    root: str,
    directories: set[bytes],
    tracked_attributes: set[bytes],
    status_rows: Sequence[tuple[bytes, str]],
) -> None:
    """The check above, from the tracked directories and ``.gitattributes``.

    The snapshot streams the index (N56) and keeps only these two sets, so it
    never holds the whole listing.
    """
    listed = {path for path, _status in status_rows}
    for directory in directories:
        candidate = directory + b"/.gitattributes" if directory else b".gitattributes"
        if candidate in tracked_attributes or candidate in listed:
            continue
        if os.path.lexists(os.path.join(os.fsencode(root), candidate)):
            raise GitDeliveryError(_UNSAFE_STATUS)


#: Directory names tools create while a Worker runs tests or installs
#: dependencies. Untracked or ignored files under one, however they are
#: ignored (their own ``*`` ``.gitignore``, a local exclude, or not at all),
#: are reported as tool caches rather than as uncommitted work - but only the
#: files that cache really holds (N69), so a forged module dropped into a
#: cache-named directory is still work:
#:
#: * ``__pycache__``, at any depth: only ``*.pyc`` files directly inside it;
#: * ``.pytest_cache``, ``.ruff_cache``, ``.mypy_cache``, at any depth: any
#:   file that is not code (``_CACHE_CODE_SUFFIXES``);
#: * ``.venv``, ``venv``, ``.tox``, ``node_modules`` hold installed code, so
#:   their place is checked instead: the workspace top level, or beside the
#:   project manifest that owns them (``_ENVIRONMENT_MANIFESTS``), or, for a
#:   nested ``.venv``, with its ``pyvenv.cfg``. A ``venv`` always needs its
#:   ``pyvenv.cfg``, since a source directory may be called that.
#:
#: A tracked file under any of them is still checked like any other.
TOOL_CACHE_DIRECTORY_NAMES: frozenset[str] = frozenset(
    {
        ".pytest_cache", ".ruff_cache", ".mypy_cache", "__pycache__",
        ".venv", "venv", ".tox", "node_modules",
    }
)
_BYTECODE_CACHE = b"__pycache__"
_DATA_CACHE_NAMES = frozenset({b".pytest_cache", b".ruff_cache", b".mypy_cache"})
#: Each environment directory, and the manifests that may own one below the
#: workspace top level.
_PYTHON_MANIFESTS = (
    b"pyproject.toml", b"setup.py", b"setup.cfg", b"requirements.txt",
    b"Pipfile", b"uv.lock", b"poetry.lock",
)
_ENVIRONMENT_MANIFESTS: dict[bytes, tuple[bytes, ...]] = {
    b".venv": _PYTHON_MANIFESTS,
    b"venv": _PYTHON_MANIFESTS,
    b".tox": (b"tox.ini", b"pyproject.toml", b"setup.cfg"),
    b"node_modules": (b"package.json", b"deno.json", b"deno.jsonc"),
}
#: File suffixes a data cache never holds: anything an interpreter, a loader
#: or a shell would run or import.
_CACHE_CODE_SUFFIXES = frozenset(
    {
        b".py", b".pyc", b".pyo", b".pyw", b".pyi", b".pth", b".so", b".dylib",
        b".dll", b".pyd", b".js", b".mjs", b".cjs", b".ts", b".mts", b".cts",
        b".sh", b".bash", b".zsh", b".fish", b".ps1", b".bat", b".cmd", b".exe",
        b".rb", b".pl", b".lua", b".php", b".jar", b".whl", b".egg", b".zip",
    }
)


def _tool_cache_excludes(root: str) -> tuple[str, ...]:
    """Pathspecs that keep the top-level environments out of the extra listings.

    A large ``.venv`` then never has to be walked. Only an environment that
    ``_tool_cache_dir`` would accept is left out (a top-level ``venv`` with
    no ``pyvenv.cfg`` is walked like any directory); everything else a
    listing returns is sorted by ``_tool_cache_dir``.
    """
    return tuple(
        f":(exclude,top,glob){os.fsdecode(name)}/**"
        for name in sorted(_ENVIRONMENT_MANIFESTS)
        if _environment_in_place(root, [name], 0)
    )


def _environment_in_place(root: str, parts: Sequence[bytes], index: int) -> bool:
    """Whether the environment directory ``parts[index]`` is where one belongs."""
    name = parts[index]
    base = os.path.join(os.fsencode(root), *parts[:index]) if index else os.fsencode(root)
    is_venv = os.path.isfile(os.path.join(base, name, b"pyvenv.cfg"))
    if name == b"venv" and not is_venv:
        return False
    if index == 0:
        return True
    if name == b".venv" and is_venv:
        # A virtual environment is on no import path until it is activated,
        # so a nested one needs no manifest beside it.
        return True
    return any(
        os.path.isfile(os.path.join(base, manifest)) for manifest in _ENVIRONMENT_MANIFESTS[name]
    )


def _tool_cache_dir(root: str, path: bytes) -> bytes | None:
    """The tool-cache directory that legitimately holds ``path``, if any.

    ``path`` is a file, or a directory when it ends in ``/`` (an ignored
    directory a ``--directory`` listing collapsed); a collapsed directory is
    a cache only inside an environment, since its files are not seen.
    """
    collapsed = path.endswith(b"/")
    parts = path.rstrip(b"/").split(b"/")
    directories = parts if collapsed else parts[:-1]
    name = b"" if collapsed else parts[-1]
    for index, part in enumerate(directories):
        if part in _ENVIRONMENT_MANIFESTS:
            if _environment_in_place(root, parts, index):
                return b"/".join(parts[: index + 1])
        elif collapsed:
            continue
        elif part == _BYTECODE_CACHE:
            if index == len(directories) - 1 and name.endswith(b".pyc"):
                return b"/".join(parts[: index + 1])
        elif part in _DATA_CACHE_NAMES:
            suffix = os.path.splitext(name)[1].lower()
            if suffix not in _CACHE_CODE_SUFFIXES:
                return b"/".join(parts[: index + 1])
    return None


def _workspace_rows(
    root: str, drivers: GitDrivers
) -> tuple[list[tuple[bytes, str]], tuple[str, ...]]:
    """Status rows, plus every path a non-committed ignore rule hides.

    Status filters untracked files through ``.git/info/exclude`` and through
    every ``.gitignore`` in the worktree, including untracked ones a Worker
    wrote - a directory whose own ``.gitignore`` says ``*`` hides itself and
    everything in it. Committed ignore rules are repository policy; these
    are not, so the files they hide are added back as rows and fingerprinted
    like any other change:

    * files only ``info/exclude`` hides are untracked files status would list
      if per-directory ``.gitignore`` files were the only rule (``ls-files
      --others --exclude-per-directory``), so they come back as ``??``;
    * an ignored ``.gitignore`` that Git still reads hides what is under its
      directory, so every ignored file there comes back as ``!!``. Ignored
      directories are found with per-directory rules only, so a directory
      that ``info/exclude`` hides is still walked and its own ``.gitignore``
      found.

    Untracked rows inside a tool-cache directory are returned separately,
    as the set of cache directories, and never count as work or toward the
    hidden-row limit. ``.gitattributes`` files are handled by
    ``_refuse_hidden_attribute_files`` and by the summary, since what they
    hide is a tracked file's change.
    """
    tool_caches: set[bytes] = set()
    excludes = _tool_cache_excludes(root)

    def keep(path: bytes, status: str) -> bool:
        if status in {"??", "!!"}:
            cache = _tool_cache_dir(root, path)
            if cache is not None:
                tool_caches.add(cache)
                return False
        return True

    rows = [(path, status) for path, status in _status_rows(root, drivers) if keep(path, status)]
    seen = {path for path, _status in rows}
    hidden: list[tuple[bytes, str]] = []

    def add_hidden(path: bytes, status: str) -> None:
        if path in seen or not keep(path, status):
            return
        seen.add(path)
        hidden.append((path, status))
        if len(hidden) > _HIDDEN_ROW_LIMIT:
            raise GitDeliveryError(_UNSAFE_STATUS)

    for path in _nul_records(
        _run_workspace_git(
            root,
            [
                "ls-files", "-z", "--others", "--exclude-per-directory=.gitignore",
                "--", *excludes,
            ],
            timeout=10,
            drivers=drivers,
        )
    ):
        add_hidden(path, "??")
    rogue_dirs: set[bytes] = set()
    for path in _nul_records(
        _run_workspace_git(
            root,
            [
                "ls-files", "-z", "--others", "--ignored",
                "--exclude-per-directory=.gitignore", "--directory",
            ],
            timeout=10,
            drivers=drivers,
        )
    ):
        # Ignored directories arrive collapsed, so this listing stays small
        # and also names the ignored tool caches the report lists. A
        # ``.gitignore`` inside a cache (``.pytest_cache`` writes one) is not
        # work, but what it hides is still walked: a forged module beside
        # it is (N69).
        cached = not keep(path, "!!")
        parent, _, name = path.rstrip(b"/").rpartition(b"/")
        if path.endswith(b"/") or name != b".gitignore":
            continue
        if cached and _tool_cache_dir(root, parent + b"/") is not None:
            # An environment in its place is never walked.
            continue
        rogue_dirs.add(parent)
    for parent in sorted(rogue_dirs):
        if any(
            other != parent and (other == b"" or parent.startswith(other + b"/"))
            for other in rogue_dirs
        ):
            continue
        scope = [":(top,literal)" + os.fsdecode(parent)] if parent else []
        for path in _nul_records(
            _run_workspace_git(
                root,
                [
                    "ls-files", "-z", "--others", "--ignored",
                    "--exclude-per-directory=.gitignore",
                    "--", *scope, *excludes,
                ],
                timeout=10,
                drivers=drivers,
            )
        ):
            add_hidden(path, "!!")
    return rows + hidden, tuple(sorted(os.fsdecode(cache) for cache in tool_caches))


def capture_git_snapshot(
    cwd: str,
    *,
    allow_external: bool = True,
    expected_root_id: str | None = None,
    expected_gitdir_id: str | None = None,
) -> GitSnapshot:
    """Capture a pre-launch snapshot; a non-Git workspace is represented explicitly."""
    if not _has_git_marker(cwd):
        return GitSnapshot(root_id=None, gitdir_id=None, entries={})
    # The include gate runs before any other Git command reads the config.
    drivers = _filter_names(cwd)
    root = _root(cwd, drivers)
    if root is None or not Path(cwd).resolve().is_relative_to(Path(root).resolve()):
        raise GitDeliveryError("Git workspace status is unknown because it could not be checked safely.")
    gitdir_id, gitdir, common_dir = _gitdir_identity(
        cwd, root, drivers, allow_external=allow_external
    )
    root_id = hashlib.sha256(os.fsencode(root)).hexdigest()
    if expected_root_id is not None and (
        root_id != expected_root_id or gitdir_id != expected_gitdir_id
    ):
        raise GitDeliveryError("Git workspace status is unknown because it could not be checked safely.")
    _refuse_hiding_state(root, gitdir, common_dir, drivers)
    status_rows, tool_caches = _workspace_rows(root, drivers)
    # Parse the index listing as it streams (N56) and keep only the records
    # of paths the rows name: on a large repository the full listing is tens
    # of MB, and only the changed paths' index entries are compared. The
    # hidden-attributes check (R4-2) needs every tracked directory and every
    # tracked .gitattributes, which are collected on the way.
    wanted = {path for path, _status in status_rows}
    tracked_dirs: set[bytes] = {b""}
    tracked_attributes: set[bytes] = set()
    index_records: dict[bytes, list[bytes]] = {}

    def index_record(record: bytes) -> None:
        metadata, separator, path = record.partition(b"\t")
        if not separator or not metadata:
            raise GitDeliveryError("Git workspace status is unknown because it could not be checked safely.")
        if path in wanted:
            index_records.setdefault(path, []).append(record)
        if path == b".gitattributes" or path.endswith(b"/.gitattributes"):
            tracked_attributes.add(path)
        parent = path
        while b"/" in parent:
            parent = parent.rpartition(b"/")[0]
            if parent in tracked_dirs:
                break
            tracked_dirs.add(parent)

    parser = _NulRecords(index_record)
    index_result = _run_workspace_git(
        root,
        ["ls-files", "--stage", "-z"],
        timeout=10,
        drivers=drivers,
        stdout_sink=parser.feed,
    )
    if index_result.returncode != 0:
        raise GitDeliveryError("Git workspace status is unknown because it could not be checked safely.")
    parser.finish()
    index_ids = {
        path: hashlib.sha256(b"\0".join(sorted(records))).hexdigest()
        for path, records in index_records.items()
    }
    _refuse_hidden_attribute_dirs(root, tracked_dirs, tracked_attributes, status_rows)

    entries: dict[str, tuple[str, str, str]] = {}
    for path, status in status_rows:
        path_id = hashlib.sha256(path).hexdigest()
        entries[path_id] = (
            status,
            _fingerprint(root, path),
            index_ids.get(path, hashlib.sha256(b"").hexdigest()),
        )
    head, head_known = _snapshot_head(root, drivers)
    return GitSnapshot(
        root_id=root_id,
        gitdir_id=gitdir_id,
        entries=entries,
        tool_caches=tool_caches,
        tool_cache_ids=frozenset(_tool_cache_id(cache) for cache in tool_caches),
        head=head,
        head_known=head_known,
        head_checked=True,
    )


def _snapshot_head(root: str, drivers: GitDrivers) -> tuple[str | None, bool]:
    """HEAD's object id, or None for an unborn branch; (None, False) if unreadable."""
    try:
        result = _run_workspace_git(
            root, ["rev-parse", "--verify", "-q", "HEAD"], timeout=5, drivers=drivers
        )
    except GitDeliveryError:
        return None, False
    output = result.stdout.decode("ascii", errors="replace").strip()
    if result.returncode == 0 and _is_object_id(output):
        return output, True
    if result.returncode == 1 and not output:
        return None, True
    return None, False


@dataclass(frozen=True)
class WorkspaceHead:
    """The branch a workspace is on and the commit that branch points at."""

    branch: str | None = None
    head: str | None = None
    root_id: str | None = None
    #: False only when HEAD could not be read at all. An unborn branch reads
    #: as known with no head: the branch exists and has no commit yet.
    known: bool = False


def resolve_workspace_head(cwd: str) -> WorkspaceHead:
    """The workspace's branch and HEAD, read once with the hardened runner.

    The branch name says which line the workspace is on and the head says
    where that line stood when it was read, so a later check can tell a
    delivery apart from a commit that landed later on the same branch.
    """
    try:
        drivers = _filter_names(cwd)
        root = _root(cwd, drivers)
    except GitDeliveryError:
        return WorkspaceHead()
    if root is None:
        return WorkspaceHead()
    head, head_known = _snapshot_head(root, drivers)
    branch = None
    try:
        result = _run_workspace_git(
            root, ["branch", "--show-current"], timeout=5, drivers=drivers
        )
        if result.returncode == 0:
            branch = result.stdout.decode("utf-8", errors="replace").strip() or None
    except GitDeliveryError:
        branch = None
    return WorkspaceHead(
        branch=branch,
        head=head,
        root_id=hashlib.sha256(os.fsencode(root)).hexdigest(),
        known=head_known,
    )


def ticket_git_baseline(
    ticket_id: str, cwd: str
) -> tuple[GitSnapshot | None, bool]:
    """Reuse one Ticket's first trusted baseline across its retries in a daemon."""
    cwd_id = hashlib.sha256(os.fsencode(os.path.realpath(cwd))).hexdigest()
    with _TICKET_BASELINE_LOCK:
        prior = _TICKET_BASELINES.get(ticket_id)
        if prior is not None and prior[0] == cwd_id:
            return prior[1], prior[2]
    try:
        snapshot = capture_git_snapshot(cwd)
        unavailable = False
    except GitDeliveryError:
        snapshot = None
        unavailable = True
    if unavailable:
        # A transient isolation/configuration failure must not poison every
        # retry for the lifetime of this daemon process.
        return None, True
    with _TICKET_BASELINE_LOCK:
        _TICKET_BASELINES[ticket_id] = (cwd_id, snapshot, False)
        if len(_TICKET_BASELINES) > 4096:
            _TICKET_BASELINES.pop(next(iter(_TICKET_BASELINES)))
    return snapshot, unavailable


def ticket_git_baseline_cached(ticket_id: str, cwd: str) -> bool:
    """Whether this process already holds the Ticket's baseline for ``cwd``."""
    cwd_id = hashlib.sha256(os.fsencode(os.path.realpath(cwd))).hexdigest()
    with _TICKET_BASELINE_LOCK:
        prior = _TICKET_BASELINES.get(ticket_id)
    return prior is not None and prior[0] == cwd_id


def clear_ticket_git_baseline(ticket_id: str) -> None:
    with _TICKET_BASELINE_LOCK:
        _TICKET_BASELINES.pop(ticket_id, None)


def git_workspace_summary(
    cwd: str,
    baseline: GitSnapshot,
    *,
    identity_baseline: GitSnapshot | None = None,
    tip: str | None = None,
    current: GitSnapshot | None = None,
) -> str | None:
    """Return None for a non-Git workspace, empty for no new dirt, else a summary.

    ``tip`` pins the commit the summary is taken against: the frozen HEAD a
    delivery is measured against, instead of the live HEAD. The changed paths
    still come from the live workspace, so the summary describes the work
    that is left, but its diff belongs to the pinned commit.

    ``current`` is the workspace snapshot the summary is built from. A frozen
    delivery passes the snapshot it took together with its freeze, so the
    summary and the frozen HEAD describe the same instant: the summary is
    the delivery's, and a later commit or edit cannot change what it says.
    """
    if tip is not None and not _is_object_id(tip):
        raise GitDeliveryError(_UNSAFE_STATUS)
    # Existing worktrees may use a shared common directory outside their root.
    # Allow it only long enough to compare against the pre-launch identity.
    identity = identity_baseline or baseline
    if current is None:
        current = capture_git_snapshot(
            cwd,
            allow_external=identity.root_id is not None,
            expected_root_id=identity.root_id,
            expected_gitdir_id=identity.gitdir_id,
        )
    if current.root_id is None:
        if baseline.root_id is not None or identity.root_id is not None:
            return "Git workspace status is unknown because it could not be checked safely."
        return None
    if identity.root_id is not None and (
        identity.root_id != current.root_id or identity.gitdir_id != current.gitdir_id
    ):
        raise GitDeliveryError("Git workspace status is unknown because it could not be checked safely.")
    drivers = _filter_names(cwd)
    root = _root(cwd, drivers)
    if root is None:
        return "Git workspace status is unknown because it could not be checked safely."
    previous = baseline.entries
    rows, _tool_caches = _workspace_rows(root, drivers)
    changed = [
        (path, status)
        for path, status in rows
        if current.entries.get(hashlib.sha256(path).hexdigest()) != previous.get(hashlib.sha256(path).hexdigest())
    ]
    # An uncommitted rule file (untracked, or tracked and modified) that
    # predates the run is not reported as a change, yet it decides what the
    # rest of the status shows: a ``.gitattributes`` can make a re-added
    # tracked file normalise to its committed blob (``ident``, ``eol``), a
    # ``.gitignore`` can hide a file the Worker writes beside it. A clean
    # status proves nothing then.
    changed_paths = {path for path, _status in changed}
    if any(
        path.rpartition(b"/")[2] in _RULE_FILE_NAMES and path not in changed_paths
        for path, _status in rows
    ):
        return _UNSAFE_STATUS
    if not changed:
        return ""
    from src.llms.native_cli.isolation import redact_diagnostic
    from src.server.utils.sanitize import sanitize_output_text

    display_changed = changed[:100]
    lines = []
    for path, status in display_changed:
        safe_path = sanitize_output_text(redact_diagnostic(os.fsdecode(path), None), limit=1000)
        lines.append(f"{status} {safe_path}")
    if len(changed) > len(display_changed):
        lines.append(f"… and {len(changed) - len(display_changed)} more changed paths")
    # Include only changed tracked paths in the stat summary. --no-textconv and
    # --no-ext-diff, plus per-filter overrides, prevent configured programs.
    # The base is the live HEAD, or the frozen HEAD the delivery is measured
    # against; both name one commit and no ref, option or pathspec.
    diff_base = "HEAD" if tip is None else tip
    try:
        stats = _run_workspace_git(
            root,
            [
                "diff",
                "--stat",
                "--no-ext-diff",
                "--no-textconv",
                "--ignore-submodules=all",
                diff_base,
                "--",
                *(
                    ":(top,literal)" + os.fsdecode(path)
                    for path, _status in display_changed
                ),
            ],
            timeout=10,
            drivers=drivers,
        )
        stats_text = (
            stats.stdout.decode("utf-8", errors="replace").strip()
            if stats.returncode == 0
            else "(diff stats unavailable; git status still shows uncommitted changes)"
        )
    except GitDeliveryError:
        stats_text = "(diff stats unavailable; git status still shows uncommitted changes)"
    stats_text = sanitize_output_text(redact_diagnostic(stats_text, None), limit=4000)
    return (
        "Uncommitted git changes detected.\n"
        "git status --short:\n"
        + "\n".join(lines)
        + f"\ngit diff --stat {diff_base}:\n"
        + (stats_text or "(no tracked diff stats)")
        + _new_tool_cache_lines(current.new_tool_caches(baseline))
    )


_TOOL_CACHE_REPORT_LIMIT = 20


def _new_tool_cache_lines(caches: Sequence[str]) -> str:
    """The tool caches the run created, listed apart: they are not work."""
    if not caches:
        return ""
    from src.llms.native_cli.isolation import redact_diagnostic
    from src.server.utils.sanitize import sanitize_output_text

    shown = [
        "  " + sanitize_output_text(redact_diagnostic(cache, None), limit=1000)
        for cache in caches[:_TOOL_CACHE_REPORT_LIMIT]
    ]
    if len(caches) > len(shown):
        shown.append(f"  … and {len(caches) - len(shown)} more")
    return "\nTool caches created during the run (not counted as work):\n" + "\n".join(shown)


def uncommitted_git_summary(cwd: str) -> str | None:
    """Compatibility helper that treats all current dirt as new work."""
    try:
        identity = capture_git_snapshot(cwd)
        baseline = GitSnapshot(
            root_id=identity.root_id,
            gitdir_id=identity.gitdir_id,
            entries={},
        )
        return git_workspace_summary(cwd, baseline)
    except GitDeliveryError as exc:
        return str(exc)


def safe_git_workspace_summary(
    cwd: str,
    baseline: GitSnapshot | None,
    *,
    baseline_unavailable: bool = False,
    identity_baseline: GitSnapshot | None = None,
    tip: str | None = None,
    current: GitSnapshot | None = None,
) -> str | None:
    """Share the safe completion result shape across daemon, Docker, and executor."""
    if baseline_unavailable:
        return "Git workspace status is unknown because it could not be checked safely."
    if baseline is None:
        return None
    try:
        return git_workspace_summary(
            cwd,
            baseline,
            identity_baseline=identity_baseline,
            tip=tip,
            current=current,
        )
    except GitDeliveryError:
        return "Git workspace status is unknown because it could not be checked safely."


#: The mandate key that holds a Ticket's delivery origin (see below).
DELIVERY_ORIGIN_KEY = "git_delivery_origin"


def delivery_origin_record(
    baseline: GitSnapshot | None, *, prior_run: bool
) -> dict[str, Any]:
    """The persisted HEAD a Ticket's commit check counts from.

    It is taken with the Ticket's first baseline and kept across retries,
    threads and daemon restarts, so commits an earlier run made still count
    as delivered. A baseline first recorded after an earlier run (a Ticket
    that predates this record) cannot know the true origin: it is recorded
    as unknown, and the commit check then neither confirms nor refutes.
    """
    if (
        prior_run
        or baseline is None
        or baseline.root_id is None
        or not baseline.head_checked
        or not baseline.head_known
    ):
        return {"version": 1, "known": False}
    return {
        "version": 1,
        "known": True,
        "head": baseline.head,
        "root_id": baseline.root_id,
    }


def with_delivery_origin(
    baseline: GitSnapshot | None, origin: Any
) -> GitSnapshot | None:
    """``baseline`` whose commit check counts from the Ticket's origin HEAD.

    Only the HEAD the commit check reads changes; the workspace entries stay
    this run's own. An origin recorded for another checkout (its ``root_id``
    differs) says nothing about this one, so the check is then not made.
    """
    if baseline is None or not isinstance(origin, dict) or origin.get("version") != 1:
        return baseline
    origin_root = origin.get("root_id")
    if origin.get("known") is not True or (
        origin_root is not None and origin_root != baseline.root_id
    ):
        return replace(baseline, origin_unknown=True)
    head = origin.get("head")
    if head is not None and not _is_object_id(head):
        return replace(baseline, origin_unknown=True)
    return replace(baseline, head=head, head_known=True, head_checked=True)


def safe_git_head_commit_time(cwd: str) -> datetime | None:
    """When the checkout's HEAD commit was made, or None when it cannot be read.

    A caller that measures how long a branch has been quiet needs the time of
    the last commit, not only whether one exists: a Worker that committed and
    then went still is quiet again, and the commit is what says when.
    """
    if not cwd or not _has_git_marker(cwd):
        return None
    try:
        completed = run_isolated_git_readonly(
            cwd, ("show", "-s", "--format=%ct", "HEAD"), timeout=5.0
        )
    except (GitDeliveryError, UnicodeError, OSError):
        return None
    if completed.returncode != 0:
        return None
    text = completed.stdout.decode("utf-8", "replace").strip()
    try:
        seconds = int(text)
    except ValueError:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def safe_git_commit_delivery(
    cwd: str,
    baseline: GitSnapshot | None,
    *,
    baseline_unavailable: bool = False,
    tip: str | None = None,
    frozen: bool = False,
) -> str:
    """Whether a commit was delivered since the baseline, as a delivery verdict.

    ``tip`` pins the far end of the counted range to one commit (the frozen
    HEAD of a delivery). The verdict then covers only the commits reachable
    from that commit, instead of whatever the branch points at when the check
    runs; a commit that lands after the freeze is not this delivery's.

    ``frozen`` says the range ends at ``tip`` and nowhere else. A frozen
    ``tip`` of None is a branch that held no commit when the freeze was
    taken: the delivery covered nothing, whatever landed later. Without
    ``frozen`` the range ends at the live HEAD.

    ``delivered``: HEAD moved forward from the baseline (or a first commit
    exists on an unborn branch). Limitation: any commit counts, so a pull or
    rebase that moves HEAD forward with others' commits also reads as
    delivered; the check proves a forward move, not authorship. ``missing``: HEAD did not move forward.
    ``unknown``: the check could not be made safely. ``not_checked``: there
    is no baseline, the workspace is not a Git repository, or the baseline
    predates commit checks; the claim is then neither confirmed nor refuted.
    """
    if baseline_unavailable:
        return "unknown"
    if tip is not None and not _is_object_id(tip):
        return "unknown"
    if baseline is None:
        return "not_checked"
    if baseline.root_id is None:
        return "not_checked"
    if baseline.origin_unknown:
        # A Git checkout whose earlier commits cannot be attributed: the
        # check can neither confirm nor refute, and says so.
        return "unverified"
    if not baseline.head_checked:
        return "not_checked"
    if not baseline.head_known:
        # HEAD was unreadable when the baseline was taken: the verdict is
        # unknown, which refutes a done claim instead of silently passing it.
        return "unknown"
    try:
        current = capture_git_snapshot(
            cwd,
            allow_external=True,
            expected_root_id=baseline.root_id,
            expected_gitdir_id=baseline.gitdir_id,
        )
    except (GitDeliveryError, UnicodeError, OSError):
        return "unknown"
    if current.root_id is None or not current.head_known:
        return "unknown"
    end = tip if frozen else current.head
    if end is None:
        # A frozen branch that held no commit: nothing was delivered, and a
        # commit that lands after the freeze is not this delivery's.
        return "missing"
    if baseline.head is None:
        return "delivered"
    if end == baseline.head:
        return "missing"
    try:
        drivers = _filter_names(cwd)
        root = _root(cwd, drivers)
        if root is None:
            return "unknown"
        ahead = _run_workspace_git(
            root,
            ["rev-list", "--count", f"{baseline.head}..{end}"],
            timeout=5,
            drivers=drivers,
        )
    except (GitDeliveryError, OSError):
        return "unknown"
    count = ahead.stdout.decode("ascii", errors="replace").strip()
    if ahead.returncode != 0 or not count.isdigit():
        return "unknown"
    return "delivered" if int(count) > 0 else "missing"


def apply_git_delivery_guard(
    report: dict[str, Any], git_summary: str | None
) -> dict[str, Any]:
    """Apply the shared body and attention transition for changed Git state."""
    if not git_summary:
        return report
    body = f"{git_summary}\n\n{report.get('body') or report.get('text') or ''}".strip()
    body = bound_git_delivery_body(body)
    report["body"] = body
    if "text" in report:
        report["text"] = body
    if report.get("state") not in {"ready", "dropped"}:
        return report
    if git_summary.startswith("Git workspace status is unknown"):
        report.update(
            {
                "state": "blocked",
                "attention_reason": "git_status_unavailable",
                "question": "Restore Git/workspace access, then retry the Ticket.",
                "summary": f"{report.get('summary') or 'Worker finished.'} Git workspace status is unknown."[:20000],
            }
        )
    else:
        report.update(
            {
                "state": "blocked",
                "attention_reason": "uncommitted_changes",
                "question": "Commit or discard the uncommitted workspace changes, then retry.",
                "summary": f"{report.get('summary') or 'Worker finished.'} The git workspace has uncommitted changes."[:20000],
            }
        )
    return report


_GIT_DELIVERY_BODY_LIMIT = 256 * 1024
_GIT_DELIVERY_BODY_MARKER = "\n[… report truncated …]"


def bound_git_delivery_body(
    body: str, *, limit: int = _GIT_DELIVERY_BODY_LIMIT
) -> str:
    """Bound the complete prefixed report body by UTF-8 bytes for the API."""
    safe = body.encode("utf-8", errors="replace").decode("utf-8")
    encoded = safe.encode("utf-8")
    if len(encoded) <= limit:
        return safe
    marker = _GIT_DELIVERY_BODY_MARKER.encode("utf-8")
    if limit <= len(marker):
        return marker[:limit].decode("utf-8", errors="ignore")
    prefix = encoded[: limit - len(marker)].decode("utf-8", errors="ignore")
    return prefix + _GIT_DELIVERY_BODY_MARKER
