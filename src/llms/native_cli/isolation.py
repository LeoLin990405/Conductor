"""OS-backed process boundary for workspace-scoped native workers.

The worker may use the public network for provider APIs, and files or
sockets inside its own sandbox mounts. Loopback TCP is denied on macOS
unless the workspace policy lists that port. Postgres (5432), Redis (6379),
the Conductor backend (8000), and the Docker API (2375/2376) stay closed
on macOS by default, and the Docker unix socket stays closed with every
other host unix socket. The Seatbelt address filter is ``localhost`` — the
profile language rejects a numeric host — and that filter matches
127.0.0.1 and ::1.

Linux does not enforce that deny. ``bwrap`` is started without
``--unshare-net``, so the worker shares the host network namespace: every
loopback port is reachable, and so are abstract-namespace unix sockets
(X11 ``@/tmp/.X11-unix/X0``, and some D-Bus and systemd endpoints).
``localhost_tcp_ports`` is ignored on Linux. Filesystem unix sockets
outside the sandbox mounts are not mounted. Tracked follow-up: a network
namespace plus an egress proxy for public traffic and the policy's
loopback ports, failing closed if that proxy is unavailable. Do not claim
a Linux loopback or abstract-socket deny until that lands.

This boundary does not contain a kernel exploit or the host administrator.
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from stat import S_ISREG
from typing import Any, Iterator, Mapping

from src.llms.native_cli import exec_guard as _exec_guard
from src.llms.native_cli.base import NativeCliUnavailable
from src.llms.native_cli.review_scratch import current_review_scratch
from src.utils.system_paths import canonical_system_path

logger = logging.getLogger(__name__)

_CONTEXT: contextvars.ContextVar["WorkerIsolation | None"] = contextvars.ContextVar(
    "native_cli_worker_isolation", default=None
)
# The only variables ``worker_isolation(git_env=...)`` may set (N67).
WORKER_GIT_ENV_NAMES = frozenset(
    {"GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG_PARAMETERS"}
)
# The only variables ``worker_isolation(test_db_env=...)`` may set (N19).
WORKER_TEST_DB_ENV_NAMES = frozenset(
    {"TEST_DB_HOST", "TEST_DB_PORT", "TEST_DB_NAME", "TEST_DB_USER", "TEST_DB_PASSWORD"}
)
_ALWAYS_DENY = {"CONDUCTOR_DAEMON_TOKEN", "CONDUCTOR_API_KEY", "CONDUCTOR_AUTH_TOKEN"}
_RESERVED_NAMES = {
    "HOME",
    "TMPDIR",
    "PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "BASH_ENV",
    "ENV",
    "NODE_OPTIONS",
    "RUBYOPT",
    "PERL5OPT",
    # ZCode receives these path-only overrides through a checked internal
    # channel. A workspace policy must not turn them into arbitrary env vars.
    "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE",
    "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE",
    # OpenCode's adapter-owned config directory follows the same checked
    # internal-channel rule; workspace policy cannot inject an arbitrary path.
    "OPENCODE_CONFIG_DIR",
}
_ZCODE_CONFIG_PATH_ENV = frozenset(
    {"ZCODE_BUILTIN_PROVIDER_CONFIG_FILE", "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"}
)
# Mount targets that are never a workspace root or runtime read path: the daemon
# user's home itself, and credential stores beneath it (whole subtree — a
# subdirectory of ~/.ssh is no safer than ~/.ssh).
_CREDENTIAL_DIR_NAMES = frozenset(
    {".ssh", ".aws", ".gnupg", ".keychain", ".keychains", "Keychains"}
)
_CASE_FOLDED_CREDENTIAL_NAMES = frozenset(
    name.casefold() for name in _CREDENTIAL_DIR_NAMES
)
# Env-name components that declare a value a credential. Redaction only applies
# to these: ordinary configuration values (account ids, "1", "true") must reach
# provider output intact or valid JSON becomes unparseable.
_CREDENTIAL_NAME_PARTS = frozenset(
    {
        "KEY",
        "TOKEN",
        "SECRET",
        "PASS",
        "PASSWD",
        "PASSWORD",
        "CRED",
        "CREDS",
        "CREDENTIAL",
        "CREDENTIALS",
        "AUTH",
        "OAUTH",
        "BEARER",
        "COOKIE",
        "SESSION",
        "PRIVATE",
        "PAT",
    }
)
# Stores that sit below home at a known depth. A mount may not cover the store
# nor any ancestor of it: mounting ~/Library exposes ~/Library/Keychains even
# though no path component of the mount itself is a credential name.
_CREDENTIAL_TARGETS = tuple((name,) for name in _CREDENTIAL_DIR_NAMES) + (
    ("Library", "Keychains"),
)
_EXACT_FORBIDDEN_ROOTS = frozenset(
    {Path("/"), Path("/Users"), Path("/home"), Path("/opt/homebrew")}
)
# Resolver files the worker namespace must see or glibc has no DNS. Missing
# files are skipped: some hosts ship only a subset. The rest of /etc stays
# outside the namespace.
_LINUX_RESOLVER_CONFIG = (
    "/etc/resolv.conf",
    "/etc/host.conf",
    "/etc/hosts",
    "/etc/nsswitch.conf",
)
# Trust stores. Debian and RHEL/Fedora layouts are both listed. Whichever of
# these paths exist are bind-mounted; if none exist, TLS cannot be verified
# and the launch fails closed instead of shipping a worker with an empty store.
_LINUX_TRUST_STORES = (
    "/etc/ssl/certs",
    "/etc/ca-certificates",
    "/etc/pki/ca-trust",
    "/etc/pki/tls/certs",
    "/etc/pki/tls/cert.pem",
)
_LINUX_NETWORK_CONFIG = _LINUX_RESOLVER_CONFIG + _LINUX_TRUST_STORES
# Host services the macOS profile keeps closed on loopback unless a workspace
# policy names the port. Seatbelt accepts only "*" or "localhost" as the
# host, and "localhost" matches 127.0.0.1 and ::1, so these numbers are the
# contract tests pin rather than numeric literals in the profile. Linux does
# not apply the list: bwrap shares the host network namespace.
_HOST_SERVICE_PORTS = (5432, 6379, 8000, 2375, 2376)
# Directory names that may be bind-mounted when a trust-store entry symlinks
# out of the store. Anything else (a symlink to /etc/passwd, for example) is
# left unmounted rather than widening the namespace.
_TRUST_ANCHOR_NAMES = frozenset(
    {
        "ca-certificates",
        "ca-trust",
        "certs",
        "directory-hash",
        "extracted",
        "openssl",
        "pem",
        "pki",
        "ssl",
        "tls",
    }
)
# Directory names safe to mount whole. ``ssl``, ``pki``, and ``tls`` also name
# parents of private-key dirs (``/etc/ssl/private``, ``/etc/pki/tls/private``),
# so a symlink into those trees binds the bundle file, not the parent.
_TRUST_BUNDLE_DIRS = frozenset(
    {
        "ca-certificates",
        "ca-trust",
        "certs",
        "directory-hash",
        "extracted",
        "openssl",
        "pem",
    }
)


@dataclass(frozen=True)
class WorkerIsolation:
    workspace_id: str
    root: Path
    workdir: Path
    env_names: frozenset[str]
    runtime_read_paths: tuple[Path, ...] = ()
    localhost_tcp_ports: tuple[int, ...] = ()
    # Local opt-ins (workspace policy): macOS login-keychain lookup for CLIs
    # that keep OAuth there, and extra writable CLI state directories.
    keychain: bool = False
    runtime_write_paths: tuple[Path, ...] = ()
    cfprefs: bool = False
    fsevents: bool = False
    # A run that must not change the device: the outer sandbox keeps the
    # workspace readable but not writable, while private CLI state (home and
    # tmp, and the policy's runtime_write_paths) stays writable.
    read_only: bool = False
    # The full configured workspace root (the policy root), which may be a
    # parent of the execution cwd. Read-only write-grant checks use it, so
    # picking a subdirectory does not narrow the workspace the check covers.
    workspace_root: Path | None = None
    # ZCode's account profile key depends on the local username.
    user_info: bool = False
    # Single files the run may create, replace or remove, and nothing beside
    # them (a linked worktree's own Git ref, reflog and admin files). macOS
    # Seatbelt matches each one as an exact ``literal`` path; no other
    # platform can grant a file that is replaced by rename, so a non-empty
    # tuple is refused there.
    runtime_write_files: tuple[Path, ...] = ()
    # Fixed Git object-store variables for a linked worktree run (N67): the
    # per-run object quarantine, the real store as a read-only alternate and
    # auto gc/maintenance off. Only ``WORKER_GIT_ENV_NAMES`` are accepted.
    git_env: tuple[tuple[str, str], ...] = ()
    # Fixed TEST_DB_* values of a throwaway test PG for this run (N19).
    test_db_env: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    # N19 scratch exec guard: off | shadow | enforce (see exec_guard.py).
    exec_guard: str = "off"
    # Executables that appeared in the scratch areas during this context;
    # filled when the context closes (shadow and enforce).
    exec_guard_findings: list[_exec_guard.ScratchExecutable] = field(
        default_factory=list, compare=False, hash=False, repr=False
    )

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def exec_guard_roots(self) -> tuple[Path, ...]:
        return _exec_guard.scratch_exec_roots(
            self.workdir, self.home, self.workspace_root
        )


def current_worker_isolation() -> WorkerIsolation | None:
    return _CONTEXT.get()


def approved_env_value(name: str) -> str | None:
    """Return a credential only when the local workspace policy names it."""
    isolation = current_worker_isolation()
    if isolation is not None and name not in isolation.env_names:
        return None
    return os.environ.get(name)


def _is_credential_name(name: str) -> bool:
    parts = {part.upper() for part in str(name).split("_") if part}
    return bool(parts & _CREDENTIAL_NAME_PARTS)


def private_home() -> Path:
    isolation = current_worker_isolation()
    return isolation.home if isolation is not None else Path.home()


def _forbidden_temp_roots() -> tuple[Path, ...]:
    """Resolved system temp trees; /tmp and /var/tmp are symlinks into /private on macOS."""
    roots: list[Path] = []
    for candidate in (Path("/tmp"), Path("/var/tmp")):
        try:
            roots.append(candidate.resolve())
        except OSError:
            roots.append(candidate)
    return tuple(dict.fromkeys(roots))


def _daemon_home() -> Path | None:
    candidates: list[Path] = []
    env_home = os.environ.get("HOME", "").strip()
    if env_home:
        candidates.append(Path(env_home))
    try:
        candidates.append(Path.home())
    except (OSError, RuntimeError):
        pass
    for candidate in candidates:
        try:
            return candidate.expanduser().resolve()
        except (OSError, RuntimeError):
            continue
    return None


def _unsafe_mount_root(path: Path) -> str | None:
    """Why `path` must not be bind-mounted into a worker, or None when it may be."""
    if any(_path_eq(forbidden, path) for forbidden in _EXACT_FORBIDDEN_ROOTS):
        return "a system directory"
    if any(_path_eq(temp, path) for temp in _forbidden_temp_roots()):
        return "the system temp tree"
    home = _daemon_home()
    if home is None:
        return None
    remainder = _descendant_parts(home, path)
    if remainder is None:
        return None
    if not remainder:
        return "the daemon user home directory"
    if _case_insensitive(path):
        parts = tuple(part.casefold() for part in remainder)
        credential_names = _CASE_FOLDED_CREDENTIAL_NAMES
    else:
        parts = remainder
        credential_names = _CREDENTIAL_DIR_NAMES
    if any(part in credential_names for part in parts):
        return "a credential directory"
    if any(
        _path_contains(path, home.joinpath(*target)) for target in _CREDENTIAL_TARGETS
    ):
        return "a credential directory"
    return None


def _same_existing_path(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def _existing_ancestor(path: Path) -> Path | None:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return current


def _case_insensitive(path: Path) -> bool:
    """Probe whether the volume holding `path` resolves names case-insensitively.

    os.pathconf("PC_CASE_SENSITIVE") is unusable here: the name is missing from
    os.pathconf_names on macOS, so it raises ValueError, and a missing path raises
    OSError — both used to fail open. Instead, walk to the nearest existing
    ancestor and check whether a case-swapped spelling names the same file.
    Inconclusive probes fail closed (True): overlap and unsafe-root checks must
    not silently widen a worker's mounts on a volume we cannot classify.
    A name that case-swap leaves unchanged (e.g. "123") proves nothing about
    the volume, so the probe keeps walking up until it finds a name that does.
    """
    probe = _existing_ancestor(path)
    if probe is None:
        return True
    while probe.parent != probe:
        swapped = probe.parent / probe.name.swapcase()
        if swapped != probe:
            try:
                swapped_stat = swapped.stat()
                probe_stat = probe.stat()
            except FileNotFoundError:
                return False
            except OSError:
                return True
            return (swapped_stat.st_dev, swapped_stat.st_ino) == (
                probe_stat.st_dev,
                probe_stat.st_ino,
            )
        probe = probe.parent
    return True


def _normalized_spelling(path: Path) -> str:
    """NFC spelling of a path.

    APFS (like HFS+ before it) matches a directory against either its NFC or
    NFD spelling, so two differently spelled Paths can name one directory and
    inode-identity checks only catch it when the path already exists. Every
    string-level comparison below normalizes first, treating the spellings as
    equal; on volumes that really distinguish them (Linux ext4) this only
    denies a launch, which is the fail-closed direction for these checks.
    Case folding is not applied here.
    """
    return unicodedata.normalize("NFC", str(path))


def _comparison_spelling(path: Path, *, fold_case: bool) -> str:
    """NFC spelling, casefolded only on a case-insensitive volume.

    ``casefold()`` can emit a non-NFC string (Turkish İ becomes i plus a
    combining dot). Normalizing again after the fold keeps two case variants
    on one key. A case-sensitive filesystem keeps the NFC spelling alone, so
    ``Cafe`` and ``cafe`` stay distinct on Linux.
    """
    spelling = _normalized_spelling(path)
    if not fold_case:
        return spelling
    return unicodedata.normalize("NFC", spelling.casefold())


def _path_eq(first: Path, second: Path) -> bool:
    """Same path, allowing inode identity, Unicode spelling, and volume case."""
    if first == second:
        return True
    if _same_existing_path(first, second):
        return True
    fold_case = _case_insensitive(first) or _case_insensitive(second)
    return _comparison_spelling(first, fold_case=fold_case) == _comparison_spelling(
        second, fold_case=fold_case
    )


def _path_contains(ancestor: Path, descendant: Path) -> bool:
    """True when `descendant` is `ancestor` or lies beneath it, volume-case-aware."""
    if _path_eq(ancestor, descendant):
        return True
    fold_case = _case_insensitive(descendant) or _case_insensitive(ancestor)
    folded = _comparison_spelling(ancestor, fold_case=fold_case)
    return any(
        _comparison_spelling(parent, fold_case=fold_case) == folded
        for parent in descendant.parents
    )


def _descendant_parts(ancestor: Path, descendant: Path) -> tuple[str, ...] | None:
    """Components of `descendant` below `ancestor`, keeping the caller's spelling.

    Returns () when the paths are equal, None when `ancestor` does not contain
    `descendant`. Compared through `_path_eq` so a case-variant spelling of the
    ancestor still matches on a case-insensitive volume.
    """
    for candidate in (descendant, *descendant.parents):
        if _path_eq(ancestor, candidate):
            if candidate == descendant:
                return ()
            return descendant.relative_to(candidate).parts
    return None


def _paths_overlap(first: Path, second: Path) -> bool:
    """True when one path contains the other, case-aware on each volume.

    APFS (default) resolves names case-insensitively but Path.resolve() keeps
    the caller's spelling, so endpoint inode identity alone is not enough:
    containment must compare case-folded on volumes that probe case-insensitive,
    including for paths that do not exist yet (probes walk to the nearest
    existing ancestor). Probes that cannot classify a volume fail closed as
    case-insensitive, which only denies the launch.
    """
    return _path_contains(first, second) or _path_contains(second, first)


def paths_overlap_canonical(first: str | Path, second: str | Path) -> bool:
    """Overlap where ``first`` is an already-resolved canonical authority path.

    ``first`` is never dereferenced again: a pinned authority path keeps the
    location its bytes were loaded from even if the file or an ancestor is
    later replaced with a symlink. The mount (``second``) is resolved to its
    current canonical path for comparison.
    """
    return _paths_overlap(
        Path(first).expanduser(),
        Path(second).expanduser().resolve(strict=False),
    )


def paths_overlap(first: str | Path, second: str | Path) -> bool:
    """Public case-aware containment probe (one path contains the other).

    Used by the daemon to check an authority file against every mount a
    Worker can write: APFS resolves names case-insensitively while
    ``Path.resolve`` keeps the caller's spelling, so plain string containment
    misses aliases that name the same directory.
    """
    return _paths_overlap(
        Path(first).expanduser().resolve(strict=False),
        Path(second).expanduser().resolve(strict=False),
    )


def _ensure_private_directory(path: Path) -> None:
    path = canonical_system_path(path)
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise NativeCliUnavailable(
            "workspace isolation directory cannot follow symlinks"
        )
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise NativeCliUnavailable(
            "workspace isolation directory must be private to the daemon user"
        )


def _canonical_runtime_write_path(path: Path) -> Path:
    """Normalize fixed root-owned macOS temp aliases before symlink checks."""
    path = path.expanduser()
    if not path.is_absolute():
        raise NativeCliUnavailable("runtime write path must be absolute")
    if len(path.parts) > 1 and path.parts[1] in {"tmp", "var"}:
        alias = Path("/") / path.parts[1]
        if alias.is_symlink():
            metadata = alias.lstat()
            if metadata.st_uid != 0:
                raise NativeCliUnavailable("runtime write path alias is unsafe")
            path = alias.resolve(strict=True).joinpath(*path.parts[2:])
    return path


def _prepare_runtime_write_paths(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    """Validate policy write grants, creating only missing final directories."""
    validated: list[Path] = []
    for raw_path in paths:
        path = _canonical_runtime_write_path(raw_path)
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise NativeCliUnavailable("runtime write path cannot follow symlinks")
        path = path.resolve(strict=False)
        if (reason := _unsafe_mount_root(path)) is not None:
            raise NativeCliUnavailable(f"runtime write path is unsafe: {reason}")
        if path.exists():
            if not path.is_dir():
                raise NativeCliUnavailable("runtime write path is not a directory")
            if path.stat().st_uid != os.getuid():
                raise NativeCliUnavailable(
                    "runtime write path is not owned by the daemon user"
                )
        else:
            parent = path.parent
            if not parent.is_dir():
                raise NativeCliUnavailable("runtime write path parent is unavailable")
            parent_stat = parent.stat()
            parent_is_root_sticky = (
                parent_stat.st_uid == 0 and parent_stat.st_mode & 0o1000
            )
            if parent_stat.st_uid != os.getuid() and not parent_is_root_sticky:
                raise NativeCliUnavailable(
                    "runtime write path parent is not owned by the daemon user"
                )
        validated.append(path)

    prepared: list[Path] = []
    for path in validated:
        if not path.exists():
            logger.warning("creating missing policy runtime write path %s", path)
            try:
                path.mkdir(mode=0o700)
            except OSError:
                raise NativeCliUnavailable(
                    "configured runtime write path could not be created"
                ) from None
            metadata = path.stat()
            if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                raise NativeCliUnavailable("created runtime write path is not private")
        prepared.append(path)
    return tuple(prepared)


def _prepare_runtime_write_files(
    paths: tuple[str, ...],
    *,
    state_root: Path,
    read_only: bool,
    workspace_root: Path,
) -> tuple[Path, ...]:
    """Validate per-file write grants; nothing is created here.

    Each grant names one file. The file need not exist yet, but its parent
    must be an existing directory owned by the daemon user, no component may
    be a symlink, and an existing entry must be a regular file of ours.
    """
    if not paths:
        return ()
    if platform.system() != "Darwin":
        raise NativeCliUnavailable(
            "per-file runtime write grants need the macOS sandbox; "
            "this platform can only grant whole directories"
        )
    validated: list[Path] = []
    for raw in paths:
        path = _canonical_runtime_write_path(Path(raw))
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise NativeCliUnavailable("runtime write file cannot follow symlinks")
        if path.resolve(strict=False) != path:
            raise NativeCliUnavailable("runtime write file path is not canonical")
        if (reason := _unsafe_mount_root(path)) is not None:
            raise NativeCliUnavailable(f"runtime write file is unsafe: {reason}")
        if _paths_overlap(path, state_root):
            raise NativeCliUnavailable("runtime write file overlaps worker state")
        if read_only and _paths_overlap(path, workspace_root):
            raise NativeCliUnavailable(
                "read-only workspace run cannot hold a write grant to the workspace"
            )
        parent = path.parent
        if not parent.is_dir() or parent.stat().st_uid != os.getuid():
            raise NativeCliUnavailable(
                "runtime write file parent is not a directory owned by the daemon user"
            )
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            metadata = None
        if metadata is not None:
            if not S_ISREG(metadata.st_mode):
                raise NativeCliUnavailable("runtime write file is not a regular file")
            if metadata.st_uid != os.getuid():
                raise NativeCliUnavailable(
                    "runtime write file is not owned by the daemon user"
                )
        validated.append(path)
    return tuple(dict.fromkeys(validated))


def secure_private_write(path: str | Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write beneath the isolated HOME using openat and O_NOFOLLOW at each step."""
    isolation = current_worker_isolation()
    if isolation is None:
        raise NativeCliUnavailable("secure private write requires worker isolation")
    target = Path(path)
    try:
        relative = target.resolve(strict=False).relative_to(isolation.home.resolve())
    except ValueError:
        raise NativeCliUnavailable(
            "worker file path is outside workspace home"
        ) from None
    parts = relative.parts
    if not parts or any(part in {".", ".."} for part in parts):
        raise NativeCliUnavailable("worker file path is invalid")
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    current = os.open(isolation.home, directory_flags)
    temporary_name = f".codex-write-{os.urandom(16).hex()}"
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, directory_flags, dir_fd=current)
            except FileNotFoundError:
                os.mkdir(part, mode=0o700, dir_fd=current)
                child = os.open(part, directory_flags, dir_fd=current)
            os.close(current)
            current = child
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary_name, flags, mode, dir_fd=current)
        try:
            os.fchmod(descriptor, mode)
            view = memoryview(data)
            while view:
                view = view[os.write(descriptor, view) :]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(
            temporary_name,
            parts[-1],
            src_dir_fd=current,
            dst_dir_fd=current,
        )
    except OSError as exc:
        try:
            os.unlink(temporary_name, dir_fd=current)
        except OSError:
            pass
        raise NativeCliUnavailable(
            "cannot safely write workspace-scoped CLI state"
        ) from exc
    finally:
        os.close(current)


def _localhost_tcp_ports(ports: object) -> tuple[int, ...]:
    """Fail closed unless `ports` is a sequence of TCP port numbers.

    Bool is rejected: it is an ``int`` subclass, and ``True`` must not become
    an allow rule for port 1.
    """
    if isinstance(ports, (str, bytes)) or not isinstance(ports, (list, tuple)):
        raise NativeCliUnavailable("localhost TCP allowlist is invalid")
    if any(type(port) is not int or not 1 <= port <= 65535 for port in ports):
        raise NativeCliUnavailable("localhost TCP allowlist is invalid")
    return tuple(dict.fromkeys(ports))


@contextmanager
def worker_isolation(
    *,
    workspace_id: str,
    root: str | Path,
    workdir: str | Path,
    env_names: set[str] | frozenset[str],
    runtime_read_paths: tuple[str, ...] = (),
    localhost_tcp_ports: tuple[int, ...] = (),
    keychain: bool = False,
    runtime_write_paths: tuple[str, ...] = (),
    cfprefs: bool = False,
    fsevents: bool = False,
    read_only: bool = False,
    workspace_root: str | Path | None = None,
    user_info: bool = False,
    runtime_write_files: tuple[str, ...] = (),
    git_env: Mapping[str, str] | None = None,
    exec_guard: str | None = None,
    test_db_env: Mapping[str, str] | None = None,
) -> Iterator[WorkerIsolation]:
    """Bind one daemon Ticket to private state and a local explicit env allowlist.

    ``exec_guard`` (``off``/``shadow``/``enforce``; default from
    ``CONDUCTOR_WORKER_EXEC_GUARD``, else ``shadow``) controls whether files
    under the workspace and HOME scratch areas may be executed (N19).

    ``test_db_env`` carries the ``TEST_DB_*`` values of a throwaway test PG
    (``worker_test_pg.ThrowawayPg.env()``); only those names are accepted, and
    a loopback ``TEST_DB_PORT`` is added to ``localhost_tcp_ports``.

    ``runtime_write_files`` grants single files (macOS only, emitted as exact
    ``literal`` rules); ``runtime_write_paths`` grants whole directories.

    ``read_only`` keeps the workspace readable but not writable for the whole
    child process tree; a run the local policy declares read-only must not be
    able to change the workspace even when the provider CLI cannot enforce it
    itself.
    """
    if not workspace_id.strip():
        raise NativeCliUnavailable("workspace isolation requires a workspace id")
    names = frozenset(
        str(name)
        for name in env_names
        if str(name).isidentifier()
        and str(name) not in _ALWAYS_DENY | _RESERVED_NAMES
        and not str(name).startswith(("LD_", "DYLD_", "CONDUCTOR_"))
    )
    raw_base = canonical_system_path(Path(root).expanduser())
    if any(part.is_symlink() for part in (raw_base, *raw_base.parents)):
        raise NativeCliUnavailable(
            "workspace isolation state path cannot follow symlinks"
        )
    base = raw_base.resolve()
    workspace = Path(workdir).expanduser().resolve()
    if (reason := _unsafe_mount_root(workspace)) is not None:
        raise NativeCliUnavailable(f"workspace directory is unsafe: {reason}")
    if _paths_overlap(base, workspace):
        raise NativeCliUnavailable("workspace state and source roots must be disjoint")
    _ensure_private_directory(base)
    identity = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()
    private = base / identity
    _ensure_private_directory(private)
    home = private / "home"
    _ensure_private_directory(home)
    _ensure_private_directory(private / "tmp")
    if not workspace.is_dir():
        raise NativeCliUnavailable("workspace directory is unavailable")
    runtime = tuple(Path(path).expanduser().resolve() for path in runtime_read_paths)
    for path in runtime:
        if (reason := _unsafe_mount_root(path)) is not None:
            raise NativeCliUnavailable(f"runtime read path is unsafe: {reason}")
    full_workspace = (
        Path(workspace_root).expanduser().resolve()
        if workspace_root is not None
        else workspace
    )
    raw_writes = tuple(_canonical_runtime_write_path(Path(p)) for p in runtime_write_paths)
    writes = tuple(path.resolve(strict=False) for path in raw_writes)
    for path in writes:
        if (reason := _unsafe_mount_root(path)) is not None:
            raise NativeCliUnavailable(f"runtime write path is unsafe: {reason}")
        if _paths_overlap(path, base):
            raise NativeCliUnavailable("runtime write path overlaps worker state")
        if read_only and _paths_overlap(path, full_workspace):
            # A read-only run must not reach the workspace through any mount,
            # including an extra runtime write grant named by the workspace
            # policy. Aliases and ancestor spellings count: the overlap probe
            # is case-aware and resolves symlinks.
            raise NativeCliUnavailable(
                "read-only workspace run cannot hold a write grant to the workspace"
            )
    writes = _prepare_runtime_write_paths(raw_writes)
    write_files = _prepare_runtime_write_files(
        tuple(str(path) for path in runtime_write_files),
        state_root=base,
        read_only=bool(read_only),
        workspace_root=full_workspace,
    )
    fixed_git_env = tuple(sorted((git_env or {}).items()))
    for name, value in fixed_git_env:
        if name not in WORKER_GIT_ENV_NAMES or "\0" in str(value) or not str(value):
            raise NativeCliUnavailable("worker Git environment is not an object quarantine")
    fixed_test_db = tuple(sorted((test_db_env or {}).items()))
    for name, value in fixed_test_db:
        if name not in WORKER_TEST_DB_ENV_NAMES or "\0" in str(value) or not str(value):
            raise NativeCliUnavailable("worker test database environment is invalid")
    ports = tuple(localhost_tcp_ports)
    db = dict(fixed_test_db)
    if "TEST_DB_PORT" in db and db.get("TEST_DB_HOST") in {"127.0.0.1", "localhost", "::1"}:
        try:
            ports = (*ports, int(db["TEST_DB_PORT"]))
        except ValueError:
            raise NativeCliUnavailable(
                "worker test database environment is invalid"
            ) from None
    isolation = WorkerIsolation(
        workspace_id=workspace_id,
        root=private,
        workdir=workspace,
        env_names=names,
        runtime_read_paths=runtime,
        localhost_tcp_ports=_localhost_tcp_ports(ports),
        keychain=bool(keychain),
        runtime_write_paths=writes,
        cfprefs=bool(cfprefs),
        fsevents=bool(fsevents),
        read_only=bool(read_only),
        workspace_root=full_workspace,
        user_info=bool(user_info),
        runtime_write_files=write_files,
        git_env=tuple((str(k), str(v)) for k, v in fixed_git_env),
        exec_guard=_exec_guard.normalize_mode(exec_guard),
        test_db_env=tuple((str(k), str(v)) for k, v in fixed_test_db),
    )
    guard_roots = isolation.exec_guard_roots
    before = (
        _exec_guard.snapshot_executables(guard_roots)
        if isolation.exec_guard != "off"
        else {}
    )
    token = _CONTEXT.set(isolation)
    try:
        yield isolation
    finally:
        _CONTEXT.reset(token)
        if isolation.exec_guard != "off":
            try:
                found = _exec_guard.new_executables(
                    before, _exec_guard.snapshot_executables(guard_roots)
                )
                isolation.exec_guard_findings.extend(found)
                _exec_guard.report_new_executables(
                    found,
                    mode=isolation.exec_guard,
                    workspace_id=workspace_id,
                    blocked=isolation.exec_guard == "enforce"
                    and platform.system() == "Darwin",
                )
            except Exception:  # noqa: BLE001 - a report must not fail the Ticket
                logger.warning("worker_exec_guard_scan_failed", exc_info=True)


def read_workspace_policy(
    path: str | Path, workspace_id: str, isolation_root: str | Path | None = None
) -> dict[str, object]:
    """Read trusted local workspace roots and env names (never credential values)."""
    policy_path = canonical_system_path(Path(path).expanduser())
    descriptor: int | None = None
    try:
        if any(parent.is_symlink() for parent in (policy_path, *policy_path.parents)):
            raise NativeCliUnavailable("workspace policy cannot be a symlink")
        policy_path = policy_path.resolve()
        # fstat the descriptor we parse from, so the mode/owner check cannot be
        # separated from the bytes that are read (a swap between check and read
        # would otherwise pass an unchecked file).
        descriptor = os.open(policy_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        stat = os.fstat(descriptor)
        if stat.st_mode & 0o077:
            raise NativeCliUnavailable(
                "workspace env policy must have mode 0600 or stricter"
            )
        if stat.st_uid != os.getuid():
            raise NativeCliUnavailable(
                "workspace env policy must be owned by the daemon user"
            )
        handle = os.fdopen(descriptor, "r", encoding="utf-8")
        descriptor = None
        with handle:
            data = json.loads(handle.read())
    except NativeCliUnavailable:
        raise
    except (OSError, ValueError) as exc:
        raise NativeCliUnavailable("cannot read local workspace env policy") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    row = data.get(workspace_id) if isinstance(data, dict) else None
    if not isinstance(row, dict) or not isinstance(row.get("root"), str):
        raise NativeCliUnavailable("workspace has no local isolation policy")
    names = row.get("env", [])
    reads = row.get("runtime_read_paths", [])
    writes = row.get("runtime_write_paths", [])
    if (
        not isinstance(names, list)
        or not isinstance(reads, list)
        or not isinstance(writes, list)
        or any(not isinstance(path, str) for path in writes)
    ):
        raise NativeCliUnavailable("workspace isolation policy is invalid")
    raw_root = canonical_system_path(Path(row["root"]).expanduser())
    if any(part.is_symlink() for part in (raw_root, *raw_root.parents)):
        raise NativeCliUnavailable("workspace root cannot follow symlinks")
    root = raw_root.resolve()
    if (reason := _unsafe_mount_root(root)) is not None:
        raise NativeCliUnavailable(f"workspace root is unsafe: {reason}")
    if not root.is_dir():
        raise NativeCliUnavailable("workspace root is unavailable")
    roots_by_id = {
        key: Path(value.get("root", "")).expanduser().resolve()
        for key, value in data.items()
        if isinstance(value, dict) and isinstance(value.get("root"), str)
    }
    roots = list(roots_by_id.values())
    for candidate in roots:
        if (reason := _unsafe_mount_root(candidate)) is not None:
            raise NativeCliUnavailable(f"workspace root is unsafe: {reason}")
    for other_id, other in roots_by_id.items():
        if other_id == workspace_id:
            continue
        if _paths_overlap(root, other):
            raise NativeCliUnavailable("workspace roots overlap in the local policy")
    state_root = Path(isolation_root).expanduser().resolve() if isolation_root else None
    if any(_paths_overlap(policy_path, candidate) for candidate in roots):
        raise NativeCliUnavailable("workspace policy must be outside workspace roots")
    if state_root and any(_paths_overlap(state_root, candidate) for candidate in roots):
        raise NativeCliUnavailable(
            "workspace isolation state must be outside workspace roots"
        )
    if state_root and _paths_overlap(state_root, policy_path):
        raise NativeCliUnavailable(
            "workspace policy and isolation state must be disjoint"
        )
    runtime_paths = tuple(
        Path(path).expanduser().resolve() for path in reads if isinstance(path, str)
    )
    if len(runtime_paths) != len(reads) or any(
        not path.exists() for path in runtime_paths
    ):
        raise NativeCliUnavailable("configured native CLI runtime path is unavailable")
    if any(
        (reason := _unsafe_mount_root(path)) is not None
        or any(_paths_overlap(path, workspace) for workspace in roots)
        or (state_root and _paths_overlap(path, state_root))
        or _paths_overlap(path, policy_path)
        for path in runtime_paths
    ):
        reason = reason or "runtime read path overlaps a workspace"
        raise NativeCliUnavailable(f"runtime read path is unsafe: {reason}")
    runtime_writes = tuple(
        _canonical_runtime_write_path(Path(path)) for path in writes
    )
    for runtime_path in runtime_writes:
        if (reason := _unsafe_mount_root(runtime_path)) is not None:
            raise NativeCliUnavailable(f"runtime write path is unsafe: {reason}")
        if any(_paths_overlap(runtime_path, workspace) for workspace in roots):
            raise NativeCliUnavailable("runtime write path overlaps a workspace")
        if (state_root and _paths_overlap(runtime_path, state_root)) or _paths_overlap(
            runtime_path, policy_path
        ):
            raise NativeCliUnavailable("runtime write path overlaps daemon state")
    raw_ports = row.get("localhost_tcp_ports", [])
    try:
        localhost_tcp_ports = _localhost_tcp_ports(raw_ports)
    except NativeCliUnavailable:
        raise NativeCliUnavailable("workspace isolation policy is invalid") from None
    return {
        "root": str(root),
        "env": {
            name for name in names if isinstance(name, str) and name.isidentifier()
        },
        "runtime_read_paths": tuple(str(path) for path in runtime_paths),
        "localhost_tcp_ports": localhost_tcp_ports,
        "keychain": row.get("keychain") is True,
        "cfprefs": row.get("cfprefs") is True,
        "fsevents": row.get("fsevents") is True,
        "user_info": row.get("user_info") is True,
        "runtime_write_paths": tuple(writes),
    }


def _minimal_env(
    overrides: Mapping[str, str] | None, isolation: WorkerIsolation
) -> dict[str, str]:
    env = {
        key: str(value)
        for key, value in (overrides or {}).items()
        if key in isolation.env_names
    }
    for key in isolation.env_names:
        if key not in env and key in os.environ:
            env[key] = os.environ[key]
    for name in _ZCODE_CONFIG_PATH_ENV:
        raw_path = (overrides or {}).get(name)
        if raw_path is None:
            continue
        path = Path(str(raw_path))
        try:
            resolved = path.resolve(strict=True)
            stat = resolved.stat()
        except (OSError, RuntimeError, ValueError) as exc:
            raise NativeCliUnavailable(
                "ZCode provider configuration file is unavailable"
            ) from exc
        if not path.is_absolute() or not resolved.is_file():
            raise NativeCliUnavailable(
                "ZCode provider configuration path is invalid"
            )
        if name == "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE":
            if not any(
                _path_contains(root, resolved)
                for root in isolation.runtime_read_paths
            ):
                raise NativeCliUnavailable(
                    "ZCode built-in provider config is outside runtime read paths"
                )
        elif not _path_contains(isolation.home.resolve(), resolved) or stat.st_mode & 0o077:
            raise NativeCliUnavailable(
                "ZCode personal provider config is outside private worker home"
            )
        env[name] = str(resolved)
    raw_opencode_dir = (overrides or {}).get("OPENCODE_CONFIG_DIR")
    if raw_opencode_dir is not None:
        path = Path(str(raw_opencode_dir))
        try:
            resolved = path.resolve(strict=True)
            stat = resolved.stat()
        except (OSError, RuntimeError, ValueError) as exc:
            raise NativeCliUnavailable(
                "OpenCode configuration directory is unavailable"
            ) from exc
        if (
            not path.is_absolute()
            or not resolved.is_dir()
            or not _path_contains(isolation.home.resolve(), resolved)
            or stat.st_mode & 0o077
        ):
            raise NativeCliUnavailable(
                "OpenCode configuration directory is outside private worker home or is not private"
            )
        env["OPENCODE_CONFIG_DIR"] = str(resolved)
    # Last, so a policy env name or an override cannot replace them.
    env.update(dict(isolation.git_env))
    env.update(dict(isolation.test_db_env))
    tmpdir = isolation.root / "tmp"
    env.update({"HOME": str(isolation.home), "TMPDIR": str(tmpdir)})
    _ensure_private_directory(Path(env["TMPDIR"]))
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    runtime_dirs = sorted(
        {
            str(path if path.is_dir() else path.parent)
            for path in isolation.runtime_read_paths
        }
    )
    if runtime_dirs:
        env["PATH"] += ":" + ":".join(runtime_dirs)
    return env


def credential_values(
    overrides: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Credential values visible to redaction, longest first.

    Only values whose env name declares a credential are returned. Callers
    that must keep context across messages (the daemon's live-output filter)
    use the same set so a value split between two messages cannot escape.
    """
    isolation = current_worker_isolation()
    if isolation is None:
        return ()
    values = {
        os.environ[key]
        for key in isolation.env_names
        if _is_credential_name(key) and os.environ.get(key)
    }
    values.update(
        str(value)
        for name, value in (overrides or {}).items()
        if _is_credential_name(str(name))
    )
    return tuple(sorted((value for value in values if value), key=len, reverse=True))


def redact_diagnostic(text: str, overrides: Mapping[str, str] | None) -> str:
    """Scrub credential values from CLI output before it is parsed or logged.

    Only values whose env name declares a credential (``*_API_KEY``,
    ``*_TOKEN``, ``*_SECRET``, …) are redacted. Ordinary configuration values
    (account numbers, ports, ``1``/``true``) pass through untouched: replacing
    every ``1`` in provider stdout corrupts JSON usage fields before the
    adapter can parse them. Operators must name credential vars accordingly.
    """
    for value in credential_values(overrides):
        text = text.replace(value, "[redacted]")
    return text


def redact_value(value: Any, overrides: Mapping[str, str] | None = None) -> Any:
    if isinstance(value, str):
        return redact_diagnostic(value, overrides)
    if isinstance(value, list):
        return [redact_value(item, overrides) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, overrides) for item in value)
    if isinstance(value, dict):
        return {key: redact_value(item, overrides) for key, item in value.items()}
    return value


_STANDARD_CLT = "/Library/Developer/CommandLineTools"


@functools.lru_cache(maxsize=1)
def selected_developer_dir() -> tuple[str, ...]:
    """Apple's selected developer directory, validated, or ``()``.

    macOS's /usr/bin/git is an xcrun shim that reads the active developer
    directory. Resolve it only through the fixed system ``xcode-select`` with
    a system PATH; never consult the caller's PATH. Only the standard Command
    Line Tools location or an /Applications/Xcode*.app developer directory is
    accepted. Cached: profile building must not spawn xcode-select each time.
    """
    selector = Path("/usr/bin/xcode-select")
    if not selector.is_file():
        return ()
    try:
        result = subprocess.run(
            [str(selector), "-p"],
            capture_output=True,
            timeout=2,
            check=False,
            env={"PATH": os.defpath},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if result.returncode != 0:
        return ()
    try:
        developer = Path(
            result.stdout.decode("utf-8", errors="strict").strip()
        ).resolve()
    except (OSError, UnicodeError, ValueError):
        return ()
    valid_xcode = (
        len(developer.parts) >= 5
        and developer.parts[1] == "Applications"
        and developer.parts[-2:] == ("Contents", "Developer")
        and developer.parts[-3].startswith("Xcode")
        and developer.parts[-3].endswith(".app")
    )
    if developer == Path(_STANDARD_CLT) or valid_xcode:
        return (str(developer),)
    return ()


def developer_dir_read_paths() -> tuple[str, ...]:
    """Read paths the macOS xcrun shims (/usr/bin/git) need in a sandbox.

    The validated selected developer directory plus the standard Command
    Line Tools directory when it exists, de-duplicated.
    """
    paths = list(selected_developer_dir())
    if Path(_STANDARD_CLT).is_dir() and _STANDARD_CLT not in paths:
        paths.append(_STANDARD_CLT)
    return tuple(paths)


def _mac_profile(
    workdir: Path,
    home: Path,
    tmp: Path,
    localhost_tcp_ports: tuple[int, ...] = (),
    read_only: bool = False,
    network_disabled: bool = False,
    extra_runtime_read_paths: tuple[Path, ...] = (),
) -> str:
    def literal(path: Path) -> str:
        # Seatbelt does not decode JSON \uXXXX escapes to the on-disk byte path:
        # a workspace named "café" profiled as "caf\u00e9" lost all file access.
        # Emit the UTF-8 spelling (NFC/NFD both match on APFS); json.dumps only
        # for the quoting.
        return json.dumps(str(path), ensure_ascii=False)

    quoted = [literal(path) for path in (workdir, home, tmp)]
    read_rules = " ".join(
        f"(subpath {literal(path)})"
        for path in dict.fromkeys(
            (
                *(Path(p) for p in developer_dir_read_paths()),
                *_runtime_reads(),
                *extra_runtime_read_paths,
            )
        )
    )
    sandbox_unix = f"(subpath {quoted[0]}) (subpath {quoted[1]}) (subpath {quoted[2]})"
    ports = _localhost_tcp_ports(tuple(localhost_tcp_ports))
    current = current_worker_isolation()
    extra: list[str] = []
    if current is not None and current.runtime_write_paths:
        full = current.workspace_root or Path(str(workdir))
        granted = [
            p
            for p in current.runtime_write_paths
            if not read_only or not _paths_overlap(p, full)
        ]
        if granted:
            w = " ".join(f"(subpath {literal(p)})" for p in granted)
            extra.append(f"(allow file-read* file-write* {w})")
    if current is not None and current.runtime_write_files:
        full = current.workspace_root or Path(str(workdir))
        files = [
            p
            for p in current.runtime_write_files
            if not read_only or not _paths_overlap(p, full)
        ]
        if files:
            # One exact path each: a subpath rule on a file name would also
            # match anything a Worker creates below it (a directory swapped
            # in for the ref).
            w = " ".join(f"(literal {literal(p)})" for p in files)
            extra.append(f"(allow file-read* file-write* {w})")
    if current is not None and current.cfprefs:
        # Read-only preference lookup (codex loads managed preferences).
        extra.append(
            '(allow mach-lookup (global-name "com.apple.cfprefsd.daemon") '
            '(global-name "com.apple.cfprefsd.agent"))'
        )
        extra.append("(allow user-preference-read)")
        extra.append(
            '(allow ipc-posix-shm-read-data (ipc-posix-name-regex #"^apple\\.cfprefs\\."))'
        )
        extra.append('(allow file-read* (subpath "/Library/Managed Preferences"))')
    if current is not None and current.fsevents:
        # Node fs.watch on macOS needs the FSEvents service (kimi-code).
        extra.append('(allow mach-lookup (global-name "com.apple.FSEvents"))')
    if current is not None and current.user_info:
        # ZCode uses the caller's local username to decrypt its account profile.
        extra.append(
            '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))'
        )
    if current is not None and current.exec_guard == "enforce":
        # N19: no exec of files under the scratch/download areas. This rule
        # is emitted last so it overrides the earlier ``(allow process-exec)``.
        exec_deny = _exec_guard.seatbelt_exec_deny_rule(
            current.exec_guard_roots, literal
        )
    else:
        exec_deny = ""
    if current is not None and current.keychain:
        # Login keychain only: securityd lookup services plus the login
        # keychain database file. No other ~/Library access.
        kc = Path.home() / "Library" / "Keychains"
        extra.append(
            '(allow mach-lookup (global-name "com.apple.SecurityServer") '
            '(global-name "com.apple.securityd.xpc") '
            '(global-name "com.apple.security.agent") '
            '(global-name "com.apple.trustd") (global-name "com.apple.trustd.agent"))'
        )
        extra.append(
            f"(allow file-read* (literal {literal(kc / 'login.keychain-db')}) (literal {literal(kc)}))"
        )
    return " ".join(
        [
            "(version 1)",
            "(deny default)",
            "(allow process-fork)",
            "(allow process-exec)",
            "(allow sysctl-read)",
            "(allow file-read-metadata)",
            '(allow file-read* (literal "/"))',
            '(allow file-read* (subpath "/System/Library") (subpath "/System/Volumes/Preboot/Cryptexes/OS") (subpath "/usr/bin") (subpath "/usr/sbin") (subpath "/usr/lib") (subpath "/usr/libexec") (subpath "/usr/share") (subpath "/bin") (subpath "/sbin") (subpath "/Library/Apple/System/Library") '
            + read_rules
            + f" (subpath {quoted[0]}) (subpath {quoted[1]})"
            + ")",
            f"(allow file-read* (subpath {quoted[0]}) (subpath {quoted[1]}) (subpath {quoted[2]}))",
            # A read-only run keeps the workspace readable but takes the write
            # grant away: private CLI state (home/tmp) stays writable, because
            # the runtime has to keep its own cache and session files there.
            (
                f"(allow file-write* (subpath {quoted[1]}) (subpath {quoted[2]}))"
                if read_only
                else f"(allow file-write* (subpath {quoted[0]}) (subpath {quoted[1]}) (subpath {quoted[2]}))"
            ),
            # Sink and entropy devices every CLI runtime expects: without these
            # a plain `>/dev/null` or a TLS stack's entropy read is an EPERM.
            '(allow file-read* (literal "/dev/null") (literal "/dev/zero") (literal "/dev/urandom") (literal "/dev/random"))',
            # System timezone database (claude-code spins without it).
            '(allow file-read* (literal "/private/etc/localtime") (subpath "/private/var/db/timezone"))',
            '(allow file-write* (literal "/dev/null"))',
            # Public network stays open for provider APIs. Loopback is denied,
            # then re-opened only for ports the workspace policy named.
            # Seatbelt accepts only "*" or "localhost" as an address host;
            # "localhost" matches 127.0.0.1 and ::1. A numeric host is rejected
            # by sandbox-exec, so Postgres, Redis, the backend, and Docker's
            # TCP ports are covered by this one deny unless listed below.
            *([] if network_disabled else [
                "(allow network-outbound)",
                "(allow network-inbound)",
                '(deny network-outbound (remote ip "localhost:*"))',
                '(deny network-inbound (local ip "localhost:*"))',
            ]),
            # Seatbelt mediates Unix sockets only through network rules, and a
            # broad grant reaches same-user services — ssh-agent, the Docker
            # socket, credential agents — with no network hop. Deny outbound
            # connects and re-allow only the sandbox mounts plus mDNSResponder,
            # which every macOS DNS lookup dials. The Docker socket is not one
            # of those paths. Any unix-socket deny also breaks connect() to
            # socket files the sandbox itself created, so a worker's internal
            # IPC must use a pre-existing socket path in these mounts.
            # A localhost TCP allow placed before the unix-socket deny does not
            # match under sandbox-exec; the port allows stay last so they win
            # for those ports only. Loopback TCP is not an open fallback.
            *([] if network_disabled else [
                "(deny network-outbound (to unix-socket))",
                f"(allow network-outbound (to unix-socket {sandbox_unix}))",
                '(allow network-outbound (to unix-socket (path "/private/var/run/mDNSResponder")))',
            ]),
            *([] if network_disabled else [
                f'(allow network-outbound (remote tcp "localhost:{port}"))'
                for port in ports
            ]),
            *extra,
            *([exec_deny] if exec_deny else []),
        ]
    )


def _runtime_reads() -> tuple[Path, ...]:
    isolation = current_worker_isolation()
    return isolation.runtime_read_paths if isolation else ()


def _network_config_bind(path: Path) -> tuple[Path, Path] | None:
    """Read-only (source, target) bind for a resolver file, or None.

    Sources are opened by bwrap in the outer namespace, so a symlinked file
    (resolv.conf -> /run/systemd/resolve/... is the common case) still works
    when its real target is bound at the canonical /etc path the libraries read.
    """
    if not path.exists():
        return None
    source = path.resolve() if path.is_symlink() else path
    return (source, path)


def _resolved_bind_source(path: Path, base: Path | None) -> Path:
    """Follow a trust path's own symlink chain, including a symlinked parent.

    ``Path.is_symlink`` is only the final component, so ``/etc/ssl`` ->
    ``/etc/pki/tls`` left ``/etc/ssl/certs`` unresolved. Parents above
    ``base`` (``/var`` -> ``/private/var`` on macOS) are not part of the
    store and must not rewrite the path the tests compare.
    """
    check = path
    while True:
        try:
            linked = check.is_symlink()
        except OSError:
            return path
        if linked:
            # A symlink at the volume root (/etc -> /private/etc on macOS) is
            # how the host already opens the path. Resolving it rewrites the
            # bind source and is not a trust-store hop.
            if check.parent == Path("/"):
                break
            return path.resolve()
        if base is not None and check == base:
            break
        if check.parent == check:
            break
        check = check.parent
    return path


def _namespace_path(path: Path, base: Path | None) -> Path:
    """Path the worker opens. A fixture root is stripped so the target is /etc/..."""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    if base is None:
        return resolved
    try:
        relative = resolved.relative_to(base.resolve())
    except (OSError, ValueError):
        return resolved
    return Path("/") / relative


def _covered_by(path: Path, roots: list[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            root_resolved = root.resolve()
        except OSError:
            continue
        if resolved == root_resolved:
            return True
        try:
            resolved.relative_to(root_resolved)
            return True
        except ValueError:
            continue
    return False


def _is_private_key_path(path: Path) -> bool:
    """True when a path component is a key directory, not macOS ``/private``.

    ``Path.resolve`` rewrites ``/var`` to ``/private/var``. That volume prefix
    is not ``/etc/ssl/private`` or ``/etc/pki/tls/private``.
    """
    parts = path.parts
    for index, part in enumerate(parts):
        if part != "private":
            continue
        if index == 1 and parts[0] == "/":
            continue
        return True
    return False


def _trust_anchor(resolved: Path) -> Path | None:
    """Directory or file worth mounting for one symlink target.

    A hashed cert name points at ``.../extracted/pem/<bundle>``. Mounting
    that ``pem`` directory covers the siblings. A target outside the trust
    names (``/etc/passwd``) is not a store and is skipped. A private-key
    path is skipped too: ``/etc/ssl`` and ``/etc/pki/tls`` hold ``private/``
    beside the public bundles, so those parents are not mounted whole.
    """
    if not resolved.exists() or _is_private_key_path(resolved):
        return None
    if not (set(resolved.parts) & _TRUST_ANCHOR_NAMES):
        return None
    if resolved.is_dir() and resolved.name in _TRUST_BUNDLE_DIRS:
        return resolved
    parent = resolved.parent
    if parent.name in _TRUST_BUNDLE_DIRS and not _is_private_key_path(parent):
        return parent
    if not resolved.is_dir():
        return resolved
    return None


def _escaped_trust_anchors(
    directory: Path, covered: list[Path], *, depth: int
) -> list[Path]:
    """Symlink targets inside ``directory`` that no current bind covers.

    Fedora and RHEL point ``/etc/ssl/certs`` at ``/etc/pki/tls/certs``, and
    the files there point on at ``/etc/pki/ca-trust/extracted/...`` or at a
    resolved ``/usr/share/pki/...`` copy. Resolving only the top symlink
    leaves those targets out of the namespace, and TLS verification fails.
    One extra directory level reaches ``directory-hash`` entries.
    """
    found: list[Path] = []
    if depth < 0 or not directory.is_dir():
        return found
    try:
        children = list(directory.iterdir())
    except OSError:
        return found
    for child in children:
        try:
            if child.is_symlink():
                anchor = _trust_anchor(child.resolve())
                if anchor is None:
                    continue
                if _covered_by(anchor, covered) or _covered_by(anchor, found):
                    continue
                found.append(anchor)
            elif child.is_dir():
                found.extend(
                    _escaped_trust_anchors(child, covered + found, depth=depth - 1)
                )
        except OSError:
            continue
    return found


def collect_trust_store_binds(root: Path | None = None) -> list[tuple[Path, Path]]:
    """Read-only binds for the host trust store, or fail closed.

    Debian and RHEL/Fedora paths are both accepted. Only entries that exist
    under ``root`` (the real host when ``root`` is None) are returned. The
    namespace target stays the canonical ``/etc/...`` path libraries open.
    A symlink is bound from its fully resolved source, and symlink entries
    inside a store are mounted too when they leave the store (the
    ``ca-trust/extracted`` chain). No usable store means the worker cannot
    verify provider TLS, so the launch is refused.
    """
    binds: list[tuple[Path, Path]] = []
    base = None if root is None else Path(root)
    for name in _LINUX_TRUST_STORES:
        host_path = Path(name) if base is None else base / name.lstrip("/")
        try:
            if not host_path.exists():
                continue
            source = _resolved_bind_source(host_path, base)
            if not source.exists():
                continue
        except OSError:
            continue
        binds.append((source, Path(name)))
    if not binds:
        raise NativeCliUnavailable("Linux trust store is unavailable")
    covered = [source for source, _target in binds]
    extra: list[tuple[Path, Path]] = []
    for source, _target in binds:
        for anchor in _escaped_trust_anchors(source, covered, depth=2):
            namespace = _namespace_path(anchor, base)
            if len(namespace.parts) < 3:
                continue
            if any(namespace == target for _source, target in binds + extra):
                continue
            if _covered_by(anchor, covered):
                continue
            extra.append((anchor, namespace))
            covered.append(anchor)
    binds.extend(extra)
    return binds


def prepare_process(
    argv: tuple[str, ...],
    cwd: str | None,
    overrides: Mapping[str, str] | None,
    *,
    network_disabled: bool = False,
    read_only_override: bool | None = None,
    extra_runtime_read_paths: tuple[str, ...] = (),
) -> tuple[tuple[str, ...], dict[str, str] | None, str | None]:
    """Wrap a child in the platform boundary; missing enforcement fails closed."""
    isolation = current_worker_isolation()
    if isolation is None:
        if network_disabled:
            raise NativeCliUnavailable(
                "network-disabled process requires workspace isolation"
            )
        if overrides is None:
            return argv, None, cwd
        # Same order as ``process.merged_env`` (which imports this module):
        # caller env, then a cross-review scratch, then adapter overrides.
        scratch = current_review_scratch()
        scratch_env = scratch.env() if scratch is not None else {}
        return argv, {**os.environ, **scratch_env, **overrides}, cwd
    workdir = isolation.workdir
    read_only = isolation.read_only if read_only_override is None else read_only_override
    if not workdir.is_dir():
        raise NativeCliUnavailable("workspace directory is unavailable")
    env = _minimal_env(overrides, isolation)
    tmp = Path(env["TMPDIR"])
    system = platform.system()
    if system == "Darwin":
        sandbox_exec = shutil.which("sandbox-exec")
        if not sandbox_exec:
            raise NativeCliUnavailable("macOS workspace sandbox is unavailable")
        profile = _mac_profile(
            workdir,
            isolation.home,
            tmp,
            isolation.localhost_tcp_ports,
            read_only,
            network_disabled,
            tuple(Path(path).expanduser().resolve() for path in extra_runtime_read_paths),
        )
        return (sandbox_exec, "-p", profile, "--", *argv), env, str(workdir)
    if system == "Linux":
        if isolation.runtime_write_files:
            # A bind mount cannot carry a single file Git replaces by rename.
            raise NativeCliUnavailable(
                "per-file runtime write grants need the macOS sandbox"
            )
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise NativeCliUnavailable(
                "Linux workspace namespace sandbox is unavailable"
            )
        # No --unshare-net by default. The worker shares the host network namespace, so
        # loopback TCP and abstract-namespace unix sockets stay reachable.
        # localhost_tcp_ports is a macOS Seatbelt control and is not applied
        # here. A later change can add a network namespace only together with
        # an egress proxy, and must fail closed when that proxy is missing.
        wrapped = [
            bwrap,
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--cap-drop",
            "ALL",
            "--die-with-parent",
            "--new-session",
        ]
        if network_disabled:
            wrapped.append("--unshare-net")
        system_paths = (
            "/usr/bin",
            "/usr/sbin",
            "/usr/lib",
            "/usr/lib64",
            "/usr/libexec",
            "/usr/share",
            "/bin",
            "/sbin",
            "/lib",
            "/lib64",
        )
        readonly_paths = {
            Path(path) for path in system_paths if Path(path).exists()
        } | set(isolation.runtime_read_paths) | {
            Path(path).expanduser().resolve() for path in extra_runtime_read_paths
        }
        for path in readonly_paths:
            if not path.exists():
                raise NativeCliUnavailable(
                    "configured native CLI runtime path is unavailable"
                )
        parent_dirs = {
            parent
            for path in readonly_paths
            for parent in path.parents
            if str(parent) not in {"/", "/tmp"}
        }
        for parent in sorted(
            parent_dirs, key=lambda item: (len(item.parts), str(item))
        ):
            wrapped.extend(("--dir", str(parent)))
        for path in sorted(readonly_paths, key=str):
            wrapped.extend(("--ro-bind", str(path), str(path)))
        network_binds = [
            bind
            for name in _LINUX_RESOLVER_CONFIG
            if (bind := _network_config_bind(Path(name))) is not None
        ]
        network_binds.extend(collect_trust_store_binds())
        for parent in sorted(
            {
                parent
                for _, target in network_binds
                for parent in target.parents
                if str(parent) != "/"
            },
            key=lambda item: (len(item.parts), str(item)),
        ):
            wrapped.extend(("--dir", str(parent)))
        for source, target in network_binds:
            wrapped.extend(("--ro-bind", str(source), str(target)))
        wrapped.extend(("--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"))
        mount_paths = (workdir, isolation.home, tmp)
        parents = {
            parent
            for target in mount_paths
            for parent in target.parents
            if str(parent) != "/"
        }
        for parent in sorted(
            parents - {Path("/tmp")}, key=lambda item: (len(item.parts), str(item))
        ):
            wrapped.extend(("--dir", str(parent)))
        if read_only:
            # Keep the workspace readable but not writable for a read-only run.
            wrapped.extend(("--ro-bind", str(workdir), str(workdir)))
        else:
            wrapped.extend(("--bind", str(workdir), str(workdir)))
        writable_paths = tuple(
            path
            for path in isolation.runtime_write_paths
            if not read_only
            or not _paths_overlap(path, isolation.workspace_root or workdir)
        )
        for path in writable_paths:
            if not path.is_dir():
                raise NativeCliUnavailable(
                    "configured native CLI write path is unavailable"
                )
            wrapped.extend(("--bind", str(path), str(path)))
        wrapped.extend(("--bind", str(isolation.home), str(isolation.home)))
        wrapped.extend(("--bind", str(tmp), str(tmp), "--chdir", str(workdir)))
        wrapped.extend(("--", *argv))
        return tuple(wrapped), env, str(workdir)
    raise NativeCliUnavailable("workspace OS isolation is unsupported on this platform")
