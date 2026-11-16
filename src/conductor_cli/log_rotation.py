"""Size-based rotation for the backend, web, and daemon logs.

Startup rotation renames a log the previous process left behind. A running
process does not keep that descriptor: it writes to a pipe, and a pump
appends to the log and rotates when the file crosses the limit, without
restarting the process.

``CONDUCTOR_LOG_MAX_BYTES`` defaults to 10 MiB. ``CONDUCTOR_LOG_BACKUP_COUNT``
defaults to 5. ``CONDUCTOR_LOG_BACKUPS`` is the older name and applies only
when the count is unset. A non-positive integer turns rotation off. A value
that is not an integer falls back to the default so a bad setting cannot
stop cond from starting. ``path.1`` is the newest archive. Archives numbered
past the count are removed, including ones a higher count left behind.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5


def _env_int(env: Mapping[str, str], name: str) -> int | None:
    raw = env.get(name)
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def log_limits(env: Mapping[str, str] | None = None) -> tuple[int, int]:
    """``(max_bytes, backup_count)`` from the environment."""
    source = os.environ if env is None else env
    max_bytes = _env_int(source, "CONDUCTOR_LOG_MAX_BYTES")
    if max_bytes is None:
        max_bytes = DEFAULT_MAX_BYTES
    backup_count = _env_int(source, "CONDUCTOR_LOG_BACKUP_COUNT")
    if backup_count is None:
        backup_count = _env_int(source, "CONDUCTOR_LOG_BACKUPS")
    if backup_count is None:
        backup_count = DEFAULT_BACKUP_COUNT
    return max_bytes, backup_count


def rotate_log(
    path: Path,
    *,
    max_bytes: int | None = None,
    backup_count: int | None = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Rename ``path`` when it has reached ``max_bytes``. Return whether it moved."""
    if max_bytes is None or backup_count is None:
        resolved_max, resolved_count = log_limits(env)
        if max_bytes is None:
            max_bytes = resolved_max
        if backup_count is None:
            backup_count = resolved_count
    if max_bytes <= 0 or backup_count <= 0 or not path.is_file():
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size < max_bytes:
        return False
    for index in range(backup_count, 1, -1):
        src = Path(f"{path}.{index - 1}")
        dst = Path(f"{path}.{index}")
        if src.is_file():
            src.replace(dst)
    path.replace(Path(f"{path}.1"))
    _prune_archives(path, backup_count)
    return True


def _prune_archives(path: Path, backup_count: int) -> None:
    """Remove ``path.N`` for every N above ``backup_count``.

    The shift above only moves archives inside the current count. A count
    that used to be higher leaves the older files in place, and those stay
    readable after the operator lowers the limit. Anything past the count
    goes, whatever its number.
    """
    prefix = f"{path.name}."
    directory = path.parent
    try:
        names = list(directory.iterdir())
    except OSError:
        return
    for candidate in names:
        if not candidate.name.startswith(prefix):
            continue
        suffix = candidate.name[len(prefix) :]
        if not suffix.isdigit() or int(suffix) <= backup_count:
            continue
        try:
            candidate.unlink()
        except OSError:
            continue


def _source_fd(source) -> int | None:
    fileno = getattr(source, "fileno", None)
    if fileno is None:
        return None
    try:
        fd = fileno()
    except (OSError, ValueError):
        return None
    if isinstance(fd, int) and fd >= 0:
        return fd
    return None


def _read_chunk(source, size: int) -> bytes | None:
    """One chunk from ``source``. ``b""`` is end of file and ``None`` is retry.

    ``BufferedReader.read1`` reports a non-blocking empty read as ``b""``,
    which is the same value as end of file. The writer is still alive at
    that moment (it has not closed the pipe), so stopping there drops the
    rest of the log and the file never rotates. A blocking ``os.read``
    returns as soon as any bytes are present and returns ``b""`` only when
    the writer has gone.
    """
    fd = _source_fd(source)
    if fd is not None:
        try:
            os.set_blocking(fd, True)
        except OSError:
            fd = None
    if fd is None:
        read1 = getattr(source, "read1", None)
        reader = read1 if read1 is not None else getattr(source, "read", None)
        if reader is None:
            return b""
        try:
            chunk = reader(size)
        except (InterruptedError, BlockingIOError):
            return None
        if chunk is None:
            return None
        return bytes(chunk)
    while True:
        try:
            return os.read(fd, size)
        except InterruptedError:
            continue
        except BlockingIOError:
            time.sleep(0.01)


def _open_log(path: Path) -> int:
    """The append descriptor for ``path``, or -1 if it cannot be opened now."""
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        return -1


def _close_quietly(fd: int) -> None:
    if fd < 0:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _write_all(fd: int, chunk: bytes) -> None:
    view = memoryview(chunk)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def pump_log(
    source,
    path: Path,
    *,
    max_bytes: int,
    backup_count: int,
) -> None:
    """Append ``source`` to ``path``, rotating whenever the file reaches the limit.

    The caller owns ``source``. This function owns the log descriptor, so a
    rename moves the bytes the writer just flushed and the next write opens
    the new file. A non-positive limit only appends.

    The pump never stops reading before end-of-file. If the log cannot be
    written (a full disk, a permission change, a failed rename or re-open),
    the bytes of that chunk are dropped and the log is opened again on the
    next chunk. A pump that died here would leave the child writing to a
    pipe with no reader: its next write to stdout or stderr fails with a
    broken pipe, and a child that does not expect that crashes.
    """
    fd = _open_log(path)
    try:
        while True:
            chunk = _read_chunk(source, 64 * 1024)
            if chunk is None:
                continue
            if chunk == b"":
                return
            if fd < 0:
                fd = _open_log(path)
                if fd < 0:
                    continue
            try:
                _write_all(fd, chunk)
            except OSError:
                # The rest of this chunk is dropped. The descriptor may be
                # the problem (a file removed under it), so open it again.
                _close_quietly(fd)
                fd = -1
                continue
            if max_bytes <= 0 or backup_count <= 0:
                continue
            try:
                size = os.fstat(fd).st_size
            except OSError:
                continue
            if size < max_bytes:
                continue
            _close_quietly(fd)
            fd = -1
            try:
                rotate_log(path, max_bytes=max_bytes, backup_count=backup_count)
            except OSError:
                # The live file keeps growing past the limit; the next
                # chunk that finds it over the limit tries again.
                pass
            fd = _open_log(path)
    finally:
        _close_quietly(fd)


def spawn_log_pump(
    path: Path,
    *,
    max_bytes: int,
    backup_count: int,
) -> tuple[subprocess.Popen[bytes], int]:
    """Start the pump in its own session. Return ``(process, write_fd)``.

    The write end is the child's stdout. The parent closes it after the
    child has inherited a copy. The pump does not inherit the write end, so
    it sees end-of-file when the child exits. Closing both ends on failure
    leaves no pipe behind.

    The pump is this file, started with ``-S``. ``python -m
    conductor_cli.log_rotation`` imports the package first, and that import
    pulls in the HTTP client before a single log byte is read. On a busy
    machine that startup outlives the writer that is waiting for the
    archive, so the live file never rotates.
    """
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, True)
    script = str(Path(__file__).resolve())
    try:
        pump = subprocess.Popen(
            [
                sys.executable,
                "-S",
                script,
                "pump",
                str(path),
                str(int(max_bytes)),
                str(int(backup_count)),
            ],
            stdin=read_fd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            pass_fds=(read_fd,),
        )
    except OSError:
        os.close(read_fd)
        os.close(write_fd)
        raise
    os.close(read_fd)
    return pump, write_fd


def _pump_main(argv: list[str]) -> int:
    if len(argv) != 4:
        return 2
    path = Path(argv[1])
    try:
        max_bytes = int(argv[2])
        backup_count = int(argv[3])
    except ValueError:
        return 2
    # Drop the buffered wrapper before the first read. Its read1 turns
    # "no bytes yet" into an empty result, and the pump would exit while
    # the writer is still open.
    source = sys.stdin.buffer
    detach = getattr(source, "detach", None)
    if detach is not None:
        source = detach()
    pump_log(source, path, max_bytes=max_bytes, backup_count=backup_count)
    return 0


if __name__ == "__main__":
    # ``python -S <this file> pump <path> <max> <count>``
    if len(sys.argv) > 1 and sys.argv[1] == "pump":
        raise SystemExit(_pump_main(sys.argv[1:]))
    raise SystemExit(2)
