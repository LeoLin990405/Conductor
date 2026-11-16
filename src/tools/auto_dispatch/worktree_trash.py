"""Remove a gate worktree in O(1) and delete its files in the background.

``git worktree remove --force`` on a worktree with a large ``node_modules``
(47k files) took longer than the 5 s Git boundary, which left half-deleted
worktrees behind. The next ``add_detached`` on that path then refused it as
"not ours" and the run was reported as a G2 failure.

Instead the worktree directory is renamed into a trash directory on the same
filesystem (atomic, so the path is free at once), ``git worktree prune`` drops
the now-missing registration, and the files are deleted later on a daemon
thread. Nothing here raises: a failed delete is only a leftover the next
startup sweep removes.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

#: Directory name of the trash under the dispatch worktrees root.
TRASH_DIRNAME = ".conductor-trash"

_pending: set[threading.Thread] = set()
_pending_lock = threading.Lock()


def trash_dir_for(path: Path, trash_root: Path | None = None) -> Path:
    """The trash directory for a worktree at ``path``.

    ``trash_root`` (the dispatch worktrees root's trash, set by the gate
    runner) wins; otherwise a trash beside the worktree is used. Either sits
    on the worktree's filesystem in the normal layout, so a rename is atomic.
    """
    if trash_root is not None:
        return Path(trash_root)
    return Path(path).parent / TRASH_DIRNAME


def move_to_trash(path: Path, trash_root: Path) -> Path | None:
    """Rename ``path`` into ``trash_root``; None when that is not possible.

    Only a real directory is moved (never a symlink). An ``OSError`` such as
    ``EXDEV`` (another filesystem) returns None so the caller falls back.
    """
    try:
        source = Path(path)
        if source.is_symlink() or not source.is_dir():
            return None
        root = Path(trash_root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = root / f"{uuid.uuid4().hex}-{source.name}"
        os.rename(source, target)
        return target
    except OSError:
        logger.warning("worktree could not be moved to trash")
        return None


def _delete(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001 - background cleanup must never raise
        logger.warning("trashed worktree could not be deleted")


def delete_later(trash_path: Path) -> None:
    """Delete ``trash_path`` on a daemon thread. Never raises, never blocks."""

    def _run() -> None:
        try:
            _delete(Path(trash_path))
        finally:
            with _pending_lock:
                _pending.discard(threading.current_thread())

    thread = threading.Thread(
        target=_run, name="conductor-worktree-trash", daemon=True
    )
    with _pending_lock:
        _pending.add(thread)
    try:
        thread.start()
    except Exception:  # noqa: BLE001 - e.g. RuntimeError: can't start new thread
        with _pending_lock:
            _pending.discard(thread)
        logger.warning("background worktree delete could not start")


def wait_pending(timeout: float | None = None) -> bool:
    """Wait for background deletions (test hook). True when none is left."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        with _pending_lock:
            threads = list(_pending)
        if not threads:
            return True
        for thread in threads:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if thread.ident is None:
                # Registered but not started yet (``delete_later`` adds it
                # first); joining it would raise.
                time.sleep(0.01)
                continue
            thread.join(remaining)
        if deadline is not None and time.monotonic() >= deadline:
            with _pending_lock:
                return not _pending


_swept: set[str] = set()
_swept_lock = threading.Lock()


def sweep_trash_once(trash_root: Path) -> bool:
    """Start one background sweep of ``trash_root`` per process. Never blocks.

    Called from constructors that run on the event loop (a GateRunner per
    dispatch), so the listing and every delete run on a daemon thread, and a
    root already swept in this process is skipped. True when a sweep started.
    """
    key = os.path.abspath(str(trash_root))
    with _swept_lock:
        if key in _swept:
            return False
        _swept.add(key)

    def _run() -> None:
        try:
            root = Path(key)
            if root.is_symlink() or not root.is_dir():
                return
            entries = list(root.iterdir())
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_dir() and not entry.is_symlink():
                    delete_later(entry)
                else:
                    entry.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001 - sweep is best effort
                continue

    try:
        threading.Thread(target=_run, name="conductor-trash-sweep", daemon=True).start()
    except Exception:  # noqa: BLE001 - a sweep that cannot start is a leftover
        logger.warning("trash sweep could not start")
        return False
    return True


def is_locked(porcelain: str, path: Path) -> bool:
    """True when ``git worktree list --porcelain`` marks ``path`` locked.

    A locked worktree must not be renamed away: ``git worktree prune`` keeps
    a locked entry, so its files would be deleted under a registration that
    every later ``remove``/``add`` then refuses as "missing but locked".
    Unreadable output counts as locked (the caller falls back to Git).
    """
    try:
        target = os.path.realpath(str(path))
    except (OSError, ValueError):
        return True
    current: str | None = None
    for raw in str(porcelain or "").splitlines():
        if raw.startswith("worktree "):
            current = raw[len("worktree "):]
        elif raw == "":
            current = None
        elif current is not None and (raw == "locked" or raw.startswith("locked ")):
            try:
                if os.path.realpath(current) == target:
                    return True
            except (OSError, ValueError):
                return True
    return False


def sweep_trash(trash_root: Path) -> int:
    """Delete every leftover under ``trash_root``. Never raises; returns count."""
    count = 0
    try:
        root = Path(trash_root)
        if root.is_symlink() or not root.is_dir():
            return 0
        entries = list(root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 - sweep is best effort
            continue
        count += 1
    return count
