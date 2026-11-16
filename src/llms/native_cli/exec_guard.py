"""Scratch-area exec guard for isolated daemon Workers (N19).

An isolated Worker once downloaded a VM tarball into its workspace scratch
area (``.orchestrator/autonomy/<ticket>/``), unpacked it and ran the binaries.
This module decides which directories count as *scratch or download areas*
and what the guard does about executables found there.

``CONDUCTOR_WORKER_EXEC_GUARD`` selects the mode, read once when a Ticket's
isolation context opens:

- ``off``: nothing is checked.
- ``shadow`` (default): log only. The directories are snapshotted when the
  isolation context opens and scanned again when it closes; every executable
  regular file that appeared or changed there is logged and recorded on the
  isolation object. Nothing is blocked.
- ``enforce``: on macOS the Seatbelt profile denies ``process-exec*`` of any
  file under those directories (direct exec and use as a ``#!`` interpreter
  are both refused with EPERM). The shadow scan still runs, so a blocked
  download is still reported. Linux ``bwrap`` has no per-mount ``noexec``
  option, so on Linux ``enforce`` only reports (the same as ``shadow``).

The deny is path based. It does not stop a Worker that copies or moves a
binary out of the scratch areas before running it, a binary interpreted by an
allowed interpreter (``sh file``, ``python file``), or a binary downloaded
straight into another workspace directory. Network egress is not restricted
here; the Linux network namespace plus egress proxy is still an open item.

Legitimate runs keep working: ``.venv/bin``, ``node_modules/.bin`` and every
other workspace path outside the scratch areas are not covered.
"""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

logger = logging.getLogger(__name__)

EXEC_GUARD_ENV = "CONDUCTOR_WORKER_EXEC_GUARD"
EXEC_GUARD_MODES = ("off", "shadow", "enforce")
DEFAULT_EXEC_GUARD_MODE = "shadow"
# Workspace-relative scratch area of the autonomy runtime (the N19 incident
# path) and HOME-relative Ticket scratch / download areas.
WORKSPACE_SCRATCH_DIRS = (".orchestrator",)
HOME_SCRATCH_DIRS = ("autonomy", "Downloads")
# Bound one scan so a huge scratch tree cannot stall a Ticket's teardown.
MAX_SCAN_ENTRIES = 20_000

_EXEC_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH

FileKey = tuple[int, int, int, int]


def exec_guard_mode(env: Mapping[str, str] | None = None) -> str:
    """Return the configured mode; an unknown value falls back to the default."""
    source = os.environ if env is None else env
    raw = str(source.get(EXEC_GUARD_ENV, "") or "").strip().lower()
    if not raw:
        return DEFAULT_EXEC_GUARD_MODE
    if raw not in EXEC_GUARD_MODES:
        logger.warning(
            "worker_exec_guard_mode_invalid value=%r default=%s",
            raw,
            DEFAULT_EXEC_GUARD_MODE,
        )
        return DEFAULT_EXEC_GUARD_MODE
    return raw


def normalize_mode(mode: str | None) -> str:
    if mode is None:
        return exec_guard_mode()
    value = str(mode).strip().lower()
    if value not in EXEC_GUARD_MODES:
        raise ValueError(f"exec guard mode must be one of {EXEC_GUARD_MODES}")
    return value


def scratch_exec_roots(
    workdir: Path, home: Path, workspace_root: Path | None = None
) -> tuple[Path, ...]:
    """Directories whose files a guarded Worker may not execute."""
    roots: list[Path] = []
    for base in dict.fromkeys(p for p in (workdir, workspace_root) if p is not None):
        roots.extend(Path(base) / name for name in WORKSPACE_SCRATCH_DIRS)
    roots.extend(Path(home) / name for name in HOME_SCRATCH_DIRS)
    return tuple(dict.fromkeys(roots))


def seatbelt_exec_deny_rule(
    roots: Iterable[Path], literal: Callable[[Path], str]
) -> str:
    """One Seatbelt rule; it must come after ``(allow process-exec)``."""
    subpaths = " ".join(f"(subpath {literal(Path(root))})" for root in roots)
    return f"(deny process-exec* {subpaths})" if subpaths else ""


@dataclass(frozen=True)
class ScratchExecutable:
    path: str
    size: int


def snapshot_executables(
    roots: Iterable[Path], *, limit: int = MAX_SCAN_ENTRIES
) -> dict[str, FileKey]:
    """Map every executable regular file under ``roots`` to an identity key.

    Symlinks are neither followed nor reported: exec through a symlink is
    checked by Seatbelt against the resolved file.
    """
    found: dict[str, FileKey] = {}
    seen = 0
    stack = [Path(root) for root in roots]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    seen += 1
                    if seen > limit:
                        logger.warning(
                            "worker_exec_guard_scan_truncated limit=%d root=%s",
                            limit,
                            current,
                        )
                        return found
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        stack.append(Path(entry.path))
                    elif stat.S_ISREG(info.st_mode) and info.st_mode & _EXEC_BITS:
                        found[entry.path] = (
                            info.st_dev,
                            info.st_ino,
                            info.st_size,
                            info.st_mtime_ns,
                        )
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
        except OSError:
            continue
    return found


def new_executables(
    before: Mapping[str, FileKey], after: Mapping[str, FileKey]
) -> tuple[ScratchExecutable, ...]:
    """Executables that appeared, or were replaced/rewritten, since ``before``."""
    return tuple(
        ScratchExecutable(path=path, size=key[2])
        for path, key in sorted(after.items())
        if before.get(path) != key
    )


def report_new_executables(
    findings: tuple[ScratchExecutable, ...],
    *,
    mode: str,
    workspace_id: str,
    blocked: bool,
) -> None:
    if not findings:
        return
    shown = ", ".join(item.path for item in findings[:10])
    more = "" if len(findings) <= 10 else f" (+{len(findings) - 10} more)"
    logger.warning(
        "worker_exec_guard_scratch_executables mode=%s workspace=%s count=%d "
        "blocked=%s paths=%s%s",
        mode,
        workspace_id,
        len(findings),
        "yes" if blocked else "no",
        shown,
        more,
    )
