"""Harness writes that never follow a symlink.

The live launch path has no OS sandbox, so a worker can plant a symlink
inside ``output_dir`` (for example ``runs/<run8>/worker.diff`` pointing at a
file outside the eval, or ``repos/runs`` pointing at another tree). Every file
the harness itself writes goes through these helpers: each directory between
``root`` and the file must be a real directory, and the file is opened with
``O_NOFOLLOW`` so a symlink in its place fails instead of being written
through.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW


class UnsafePathError(OSError):
    """A path the harness writes to is, or passes through, a symlink."""


def ensure_real_dir(root: Path, directory: Path) -> Path:
    """Create ``directory`` under ``root``; refuse any symlinked component.

    ``root`` itself must not be a symlink. ``directory`` must be ``root`` or
    lexically inside it. Raises ``UnsafePathError`` (an ``OSError``) when a
    component is a symlink or not a directory.
    """
    root = Path(root)
    directory = Path(directory)
    try:
        parts = directory.relative_to(root).parts
    except ValueError as exc:
        raise UnsafePathError(f"{directory} is not inside {root}") from exc
    if ".." in parts:
        raise UnsafePathError(f"{directory} is not inside {root}")
    current = root
    _require_dir(current, create=True)
    for part in parts:
        current = current / part
        _require_dir(current, create=True)
    return directory


def _require_dir(path: Path, *, create: bool) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(path, 0o755)
        except FileExistsError:
            pass
        info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise UnsafePathError(f"refusing to write through symlink {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafePathError(f"{path} is not a directory")


def write_bytes(root: Path, path: Path, data: bytes) -> None:
    """Replace ``path``'s content. The file and its parents must not be symlinks."""
    path = Path(path)
    ensure_real_dir(root, path.parent)
    try:
        fd = os.open(path, _WRITE_FLAGS | os.O_TRUNC, 0o600)
    except OSError as exc:
        if path.is_symlink():
            raise UnsafePathError(f"refusing to write through symlink {path}") from exc
        raise
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def write_text(root: Path, path: Path, text: str) -> None:
    write_bytes(root, path, text.encode("utf-8"))


def append_line(root: Path, path: Path, line: str) -> None:
    """Append one ``\\n``-terminated line, repairing a torn last line first.

    A process killed mid-append leaves a partial line with no newline; a
    plain append would glue the next record onto it and lose both.
    """
    path = Path(path)
    ensure_real_dir(root, path.parent)
    try:
        fd = os.open(path, _WRITE_FLAGS | os.O_APPEND, 0o600)
    except OSError as exc:
        if path.is_symlink():
            raise UnsafePathError(f"refusing to write through symlink {path}") from exc
        raise
    with os.fdopen(fd, "ab") as handle:
        size = os.fstat(handle.fileno()).st_size
        prefix = b""
        if size:
            with open(path, "rb") as reader:
                reader.seek(size - 1)
                if reader.read(1) != b"\n":
                    prefix = b"\n"
        handle.write(prefix + line.rstrip("\n").encode("utf-8") + b"\n")


def open_lock(root: Path, path: Path) -> int:
    """Open (creating) a lock file without following a symlink. Returns the fd."""
    path = Path(path)
    ensure_real_dir(root, path.parent)
    try:
        return os.open(path, _WRITE_FLAGS | os.O_APPEND, 0o600)
    except OSError as exc:
        if path.is_symlink():
            raise UnsafePathError(f"refusing to lock through symlink {path}") from exc
        raise


def read_bytes(path: Path) -> bytes:
    """Read a regular file without following a symlink at the leaf."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise UnsafePathError(f"{path} is not a regular file")
        return handle.read()


def parse_jsonl(text: str) -> tuple[list[object], int]:
    """Parse JSON lines; skip lines that do not decode. Returns (rows, skipped).

    A torn line is one a crashed append left behind. For the ledger, a torn
    reserve was never followed by a launch (the reserve is written first), so
    dropping it never under-counts a spend that happened.
    """
    rows: list[object] = []
    skipped = 0
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            rows.append(json.loads(raw))
        except ValueError:
            skipped += 1
    return rows, skipped
