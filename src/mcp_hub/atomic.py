"""Atomic replace that leaves the destination untouched if we crash first.

Bytes go to a temp file in the same directory. ``os.replace`` is the only
publish step, so a failure injected before that call keeps the original
bytes. An existing file keeps its permission bits; a new file is 0600.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

from .agents import assert_no_escape
from .errors import McpHubError, PublishConflict

# Tests inject a failure by replacing os.replace, or by setting this hook.
before_replace_hook = None


def file_mode(path: Path, *, force_private: bool) -> int:
    if force_private:
        return 0o600
    try:
        if path.exists() and not path.is_symlink():
            return path.lstat().st_mode & 0o777
    except OSError as exc:
        raise McpHubError(f"could not stat {path.name}: {exc.strerror or exc}") from exc
    return 0o600


def atomic_write(
    path: Path,
    data: bytes,
    *,
    mode: int,
    home: Path | None = None,
    expected: bytes | None = None,
    expect_absent: bool = False,
) -> None:
    """Write ``data`` via temp file + ``os.replace``. ``mode`` is applied exactly.

    When ``expected`` or ``expect_absent`` is set, the destination is compared
    again after ``before_replace_hook`` and immediately before ``os.replace``.
    A mismatch raises :class:`PublishConflict` and leaves the destination bytes
    in place.
    """
    if home is not None:
        assert_no_escape(home, path)
    elif path.is_symlink():
        raise McpHubError("refusing to follow symlink")
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise McpHubError(
            f"could not create {parent.name}: {exc.strerror or exc}"
        ) from exc
    fd = -1
    tmp_path: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=parent
        )
        tmp_path = Path(tmp_name)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fchmod(fd, mode)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        hook = before_replace_hook
        if hook is not None:
            hook()
        if expected is not None or expect_absent:
            if not _destination_matches(path, expected, expect_absent=expect_absent):
                raise PublishConflict("config changed before publish")
        if home is not None:
            assert_no_escape(home, path)
        os.replace(tmp_path, path)
        tmp_path = None
    except Exception:
        if fd >= 0:
            os.close(fd)
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise
    try:
        os.chmod(path, mode)
    except OSError as exc:
        raise McpHubError(f"could not set mode on {path.name}") from exc


def atomic_delete(
    path: Path,
    *,
    expected: bytes,
    home: Path | None = None,
) -> None:
    """Unlink ``path`` only when it still holds ``expected`` after the hook.

    The hook runs first, then the bytes are compared again. A mismatch raises
    :class:`PublishConflict` and leaves the path in place, including a file
    the hook just wrote.
    """
    if home is not None:
        assert_no_escape(home, path)
    hook = before_replace_hook
    if hook is not None:
        hook()
    if home is not None:
        assert_no_escape(home, path)
    if not _destination_matches(path, expected, expect_absent=False):
        raise PublishConflict("config changed before publish")
    path.unlink()


def _destination_matches(
    path: Path, expected: bytes | None, *, expect_absent: bool
) -> bool:
    """True when ``path`` is still the bytes apply read. Never follows a link."""
    try:
        if path.is_symlink():
            return False
        if expect_absent:
            return not path.exists()
        if expected is None or not path.is_file():
            return False
        return path.read_bytes() == expected
    except OSError:
        return False
