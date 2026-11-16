"""Per-run Git object quarantine for daemon Ticket Workers (N67).

A Ticket Worker commits in a linked worktree whose object store is shared
with ``main`` and every other branch. Granting ``<common>/objects`` writable
lets a Worker rewrite or delete packs behind every other ref. Instead the
Worker runs with ``GIT_OBJECT_DIRECTORY`` pointing at a fresh per-run
directory under ``<common>/conductor-objects/`` and the real store as a
read-only alternate; only that directory is granted writable. After the run
the host moves the new objects into the real store with
``pack-objects | index-pack --strict`` (hash checked, never overwrites an
object), checks them and the Ticket tip, and removes the quarantine.

A quarantine that cannot be moved is kept (never deleted with objects a ref
may need), offered to the next Worker of the same checkout as a read-only
alternate, and retried at the next prepare. The mechanism mirrors the
dispatch launcher's deepseek quarantine (``scripts/dispatch_launch.sh``,
N72-2/N72-3): same directory, same fsck severities, same Git hardening.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

QUARANTINE_PARENT = "conductor-objects"
# Daemon quarantines carry their own prefix so a daemon never sweeps a
# quarantine of a live dispatch launch (``<admin>-<stamp>``) and vice versa.
DAEMON_PREFIX = "daemon-"
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_RETRIES = 3
_GIT_TIMEOUT_SECONDS = 600.0
_ADMIN_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_STAMP_RE = re.compile(r"^[0-9]{1,24}p[0-9]{1,10}$")
_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_FSCK_KEY_RE = re.compile(r"^fsck\.([A-Za-z]+)$")
# Legacy-format findings that real histories carry; every path-unsafe check
# (hasDot, hasDotgit, fullPathname, ...) stays an error.
_FSCK_BASE = ("zeroPaddedFilemode=warn", "largePathname=warn")
# The only variables the Worker environment may receive from here.
WORKER_GIT_ENV_NAMES = frozenset(
    {"GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG_PARAMETERS"}
)
# ``GIT_CONFIG_PARAMETERS`` rather than ``GIT_CONFIG_COUNT``/``KEY_<n>``:
# credential scrubs in some CLIs drop any name containing ``KEY``.
_WORKER_CONFIG_PARAMETERS = "'gc.auto'='0' 'maintenance.auto'='false'"


class ObjectQuarantineError(RuntimeError):
    """The quarantine could not be prepared safely."""


@dataclass(frozen=True)
class ObjectQuarantine:
    common: Path
    path: Path
    # Earlier quarantines of this checkout that are still kept; the Worker
    # reads them as alternates so a tip that lives there stays reachable.
    kept: tuple[Path, ...] = ()
    notes: tuple[str, ...] = field(default=())

    def worker_env(self) -> dict[str, str]:
        alternates = [str(self.common / "objects"), *(str(p) for p in self.kept)]
        return {
            "GIT_OBJECT_DIRECTORY": str(self.path),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": os.pathsep.join(alternates),
            "GIT_CONFIG_PARAMETERS": _WORKER_CONFIG_PARAMETERS,
        }


@dataclass(frozen=True)
class MigrationResult:
    ok: bool
    reason: str = ""
    moved: int = 0


def _int_setting(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def max_bytes() -> int:
    return _int_setting("CONDUCTOR_DAEMON_QUARANTINE_MAX_BYTES", DEFAULT_MAX_BYTES)


def max_retries() -> int:
    return _int_setting("CONDUCTOR_DAEMON_QUARANTINE_MAX_RETRIES", DEFAULT_MAX_RETRIES)


def _git_binary() -> str:
    binary = shutil.which("git", path=os.defpath)
    if binary is None:
        raise ObjectQuarantineError("Git is unavailable")
    return str(Path(binary).absolute())


def _host_env(object_dir: Path | None = None) -> dict[str, str]:
    """A fixed environment: nothing inherited can aim Git elsewhere."""
    env = {
        "PATH": os.defpath,
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    if object_dir is not None:
        env["GIT_OBJECT_DIRECTORY"] = str(object_dir)
    return env


def _store_argv(common: Path, *args: str) -> list[str]:
    # No multi-pack-index or commit-graph: the quarantine is Worker-written,
    # and only loose objects and pack/idx pairs are read from it.
    return [
        _git_binary(),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.multiPackIndex=false",
        "-c",
        "core.commitGraph=false",
        f"--git-dir={common}",
        *args,
    ]


def _run(
    common: Path,
    args: Sequence[str],
    *,
    object_dir: Path | None = None,
    stdin: bytes = b"",
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        _store_argv(common, *args),
        input=stdin,
        capture_output=True,
        env=_host_env(object_dir),
        cwd=str(common),
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )


def _owned_real_dir(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.getuid()


def _parent(common: Path) -> Path:
    parent = common / QUARANTINE_PARENT
    try:
        metadata = parent.lstat()
    except FileNotFoundError:
        parent.mkdir(mode=0o700)
        metadata = parent.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise ObjectQuarantineError("the object quarantine parent is not a private directory")
    return parent


def _attempts_path(path: Path) -> Path:
    return path.with_name(path.name + ".attempts")


def _attempts(path: Path) -> int:
    try:
        raw = _attempts_path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return 0
    return int(raw) if raw.isdigit() else 0


def _live_path(path: Path) -> Path:
    return path.with_name(path.name + ".live")


def mark_live(path: Path, process_groups: Sequence[int]) -> None:
    """Record the process groups still running when ``path`` was kept.

    A later sweep never moves or removes the quarantine while any of them
    has a member: a straggler could still write into it.
    """
    groups = sorted({int(g) for g in process_groups if int(g) > 1})
    try:
        _live_path(path).write_text(json.dumps(groups), encoding="utf-8")
    except OSError:
        pass


def _still_live(path: Path) -> bool:
    try:
        raw = json.loads(_live_path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    if not isinstance(raw, list) or not raw:
        # Kept under a straggler whose group is unknown: stay live until an
        # operator removes the marker.
        return True
    for group in raw:
        try:
            os.killpg(int(group), 0)
        except ProcessLookupError:
            continue
        except (OSError, TypeError, ValueError):
            return True
        return True
    try:
        _live_path(path).unlink()
    except OSError:
        pass
    return False


def _record_failure(path: Path) -> None:
    try:
        _attempts_path(path).write_text(f"{_attempts(path) + 1}\n", encoding="utf-8")
    except OSError:
        pass


def _holds_unsafe_entry(path: Path) -> str:
    """A symlink, device, fifo or socket anywhere under ``path``; '' if none."""
    for current, dirs, files in os.walk(path, followlinks=False):
        for name in (*dirs, *files):
            try:
                mode = (Path(current) / name).lstat().st_mode
            except OSError:
                return "an entry could not be inspected"
            if stat.S_ISLNK(mode):
                return "it holds a symlink"
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                return "it holds a special file"
    return ""


def _remove_tree(path: Path) -> bool:
    """Remove a quarantine even when the Worker cleared its permissions."""

    def _retry(function, target, _exc):  # noqa: ANN001
        try:
            # Never follow a link the Worker may have swapped in.
            os.chmod(os.path.dirname(target), 0o700, follow_symlinks=False)
            if os.path.isdir(target) and not os.path.islink(target):
                os.chmod(target, 0o700, follow_symlinks=False)
            function(target)
        except (OSError, NotImplementedError):
            pass

    try:
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
        else:
            os.chmod(path, 0o700, follow_symlinks=False)
            shutil.rmtree(path, onexc=_retry)
    except OSError:
        return False
    for side in (_attempts_path(path), _live_path(path)):
        try:
            side.unlink(missing_ok=True)
        except OSError:
            pass
    return not path.exists() and not path.is_symlink()


_LOOSE_DIR_RE = re.compile(r"^[0-9a-f]{2}$")
_LOOSE_NAME_RE = re.compile(r"^(?:[0-9a-f]{38}|[0-9a-f]{62})$")
_PACK_NAME_RE = re.compile(r"^pack-[0-9a-f]{40,64}\.(?:pack|idx)$")


def _drop_non_objects(path: Path) -> None:
    """Remove everything but loose objects and pack/idx files.

    Index files host Git would otherwise parse (multi-pack-index,
    commit-graph, ``.rev``/``.bitmap``/``.promisor``/``.keep``, alternates)
    and temporary files are Worker-written; none is needed to move objects.
    """
    for entry in list(path.iterdir()):
        mode = entry.lstat().st_mode
        if stat.S_ISDIR(mode) and _LOOSE_DIR_RE.fullmatch(entry.name):
            for child in list(entry.iterdir()):
                if not (
                    _LOOSE_NAME_RE.fullmatch(child.name)
                    and stat.S_ISREG(child.lstat().st_mode)
                ):
                    _remove_entry(child)
        elif stat.S_ISDIR(mode) and entry.name == "pack":
            for child in list(entry.iterdir()):
                if not (
                    _PACK_NAME_RE.fullmatch(child.name)
                    and stat.S_ISREG(child.lstat().st_mode)
                ):
                    _remove_entry(child)
        else:
            _remove_entry(entry)


def _remove_entry(entry: Path) -> None:
    if entry.is_dir() and not entry.is_symlink():
        shutil.rmtree(entry)
    else:
        entry.unlink()


def _fsck_spec(common: Path) -> str:
    """The deepseek launcher's severities; the repository's own ``fsck.<id>`` win."""
    items = list(_FSCK_BASE)
    result = _run(common, ["config", "--get-regexp", r"^fsck\.[A-Za-z]+$"])
    if result.returncode == 0:
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            key, _, value = line.partition(" ")
            match = _FSCK_KEY_RE.fullmatch(key)
            if (
                match
                and match.group(1).lower() != "skiplist"
                and value.strip().lower() in {"error", "warn", "ignore"}
            ):
                items.append(f"{match.group(1)}={value.strip().lower()}")
    return ",".join(items)


def _missing(common: Path, listing: bytes) -> list[str]:
    result = _run(common, ["cat-file", "--batch-check"], stdin=listing)
    if result.returncode != 0:
        raise ObjectQuarantineError("the object store could not be checked")
    return [
        line.split(" ", 1)[0]
        for line in result.stdout.decode("utf-8", "replace").splitlines()
        if line.endswith(" missing")
    ]


def migrate(common: Path, path: Path, *, tips: Sequence[str] = ()) -> MigrationResult:
    """Move every object of ``path`` into ``<common>/objects``; keep on failure.

    ``tips`` (object ids) are checked in the real store afterwards: only the
    current run's quarantine checks the tip, a leftover is judged by its own
    objects. Nothing is removed unless every listed object is present in the
    real store.
    """
    try:
        if not path.exists() and not path.is_symlink():
            if not _tips_present(common, tips):
                return MigrationResult(False, "the Ticket tip is missing from the object store")
            return MigrationResult(True)
        if not _owned_real_dir(path):
            return MigrationResult(False, "the object quarantine is not a private directory")
        unsafe = _holds_unsafe_entry(path)
        if unsafe:
            return MigrationResult(False, f"the object quarantine is unsafe: {unsafe}")
        try:
            _drop_non_objects(path)
        except OSError:
            return MigrationResult(False, "the object quarantine could not be cleaned")
        listed = _run(
            common,
            ["cat-file", "--batch-all-objects", "--batch-check=%(objectname)"],
            object_dir=path,
        )
        if listed.returncode != 0:
            return MigrationResult(False, "the object quarantine could not be listed")
        listing = listed.stdout
        moved = 0
        if listing.strip() and _missing(common, listing):
            moved = len(listing.split())
            # Before the first process starts: nothing may be left blocked.
            strict = f"--strict={_fsck_spec(common)}"
            pack = subprocess.Popen(  # noqa: S603 - fixed argv, fixed env
                _store_argv(common, "pack-objects", "-q", "--stdout"),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=_host_env(path),
                cwd=str(common),
            )
            index = subprocess.Popen(  # noqa: S603 - fixed argv, fixed env
                _store_argv(
                    common,
                    "index-pack",
                    "--stdin",
                    strict,
                    f"--max-input-size={max_bytes()}",
                ),
                stdin=pack.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=_host_env(),
                cwd=str(common),
            )
            assert pack.stdin is not None and pack.stdout is not None
            pack.stdout.close()
            try:
                pack.stdin.write(listing)
            except BrokenPipeError:
                pass
            finally:
                pack.stdin.close()
            deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
            try:
                index_status = index.wait(timeout=_GIT_TIMEOUT_SECONDS)
                pack_status = pack.wait(timeout=max(1.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                for process in (pack, index):
                    process.kill()
                    process.wait()
                return MigrationResult(False, "moving the quarantined objects timed out")
            if pack_status != 0 or index_status != 0:
                return MigrationResult(False, "the quarantined objects could not be moved")
            if _missing(common, listing):
                return MigrationResult(False, "quarantined objects are missing after the move")
        if not _tips_present(common, tips):
            return MigrationResult(False, "the Ticket tip is missing from the object store")
        if not _remove_tree(path):
            # Every object is in the store already; a leftover is harmless
            # and the next prepare drops it.
            return MigrationResult(True, "the object quarantine could not be removed", moved)
        return MigrationResult(True, moved=moved)
    except (OSError, subprocess.SubprocessError, ObjectQuarantineError) as exc:
        return MigrationResult(False, f"the object quarantine could not be moved ({type(exc).__name__})")


def _tips_present(common: Path, tips: Sequence[str]) -> bool:
    return all(_has_commit(common, tip) for tip in tips if tip)


def _has_commit(common: Path, oid: str) -> bool:
    if not _OID_RE.fullmatch(oid):
        return False
    result = _run(common, ["cat-file", "-t", oid])
    return result.returncode == 0 and result.stdout.strip() == b"commit"


def _admin_of(name: str) -> str:
    """The admin name a daemon quarantine directory belongs to, or ''."""
    if not name.startswith(DAEMON_PREFIX):
        return ""
    admin, _, stamp = name[len(DAEMON_PREFIX) :].rpartition("-")
    if admin and _STAMP_RE.fullmatch(stamp) and _ADMIN_NAME_RE.fullmatch(admin):
        return admin
    return ""


def _stamp_order(name: str) -> tuple[int, int]:
    stamp = name.rpartition("-")[2]
    created, _, pid = stamp.partition("p")
    return int(created), int(pid)


def _leftovers(parent: Path, admin_name: str) -> list[Path]:
    rows = [entry for entry in parent.iterdir() if _admin_of(entry.name) == admin_name]
    # Oldest first by creation stamp (an mtime a straggler can move): a
    # newer quarantine may hold objects that build on an older one.
    return sorted(rows, key=lambda entry: _stamp_order(entry.name))


def _orphan_admin_names(common: Path, parent: Path) -> set[str]:
    """Admin names of daemon quarantines whose checkout is gone."""
    names: set[str] = set()
    worktrees = common / "worktrees"
    for entry in parent.iterdir():
        admin = _admin_of(entry.name)
        if admin and not (worktrees / admin).exists():
            names.add(admin)
    return names


def _sweep(common: Path, parent: Path, admin_name: str) -> tuple[list[Path], list[str]]:
    kept: list[Path] = []
    notes: list[str] = []
    for leftover in _leftovers(parent, admin_name):
        if _still_live(leftover):
            notes.append(f"object quarantine {leftover} kept: its Worker is still running")
        elif _attempts(leftover) >= max_retries():
            # Not offered as an alternate any more: an operator decides.
            notes.append(f"object quarantine {leftover} needs an operator")
            continue
        else:
            result = migrate(common, leftover)
            if result.ok:
                continue
            _record_failure(leftover)
            notes.append(f"object quarantine {leftover} kept: {result.reason}")
        if _owned_real_dir(leftover):
            kept.append(leftover)
    return kept, notes


def prepare(common: Path, admin: Path) -> ObjectQuarantine:
    """Create this run's quarantine after retrying the checkout's leftovers.

    ``common`` and ``admin`` are captured by the caller before the Worker
    runs (a Worker can rewrite ``<worktree>/.git``).
    """
    if admin.parent != common / "worktrees" or not _ADMIN_NAME_RE.fullmatch(admin.name):
        raise ObjectQuarantineError("object quarantine needs a linked worktree")
    if not _owned_real_dir(common / "objects"):
        raise ObjectQuarantineError("the object store is not a private directory")
    parent = _parent(common)
    # One sweep at a time per repository: two Tickets starting together
    # would otherwise move (and count a failure for) the same leftover.
    lock_fd = os.open(
        parent / ".daemon-sweep.lock",
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        kept, notes = _sweep(common, parent, admin.name)
        for orphan in sorted(_orphan_admin_names(common, parent) - {admin.name}):
            # Checkout removed while its quarantine was kept: move it now or
            # keep it for the operator (a live straggler is skipped).
            _, orphan_notes = _sweep(common, parent, orphan)
            notes.extend(orphan_notes)
        # No '-' in the stamp: the admin name is everything before the last one.
        stamp = f"{time.time_ns()}p{os.getpid()}"
        path = parent / f"{DAEMON_PREFIX}{admin.name}-{stamp}"
        path.mkdir(mode=0o700)
    finally:
        os.close(lock_fd)
    return ObjectQuarantine(
        common=common, path=path.resolve(), kept=tuple(kept), notes=tuple(notes)
    )
