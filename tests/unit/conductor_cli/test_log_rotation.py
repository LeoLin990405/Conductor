"""Cond rotates backend, web, and daemon logs by size before opening them."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

from src.conductor_cli.log_rotation import (
    DEFAULT_BACKUP_COUNT,
    DEFAULT_MAX_BYTES,
    log_limits,
    pump_log,
    rotate_log,
)
from src.conductor_cli.ops_runtime import Component, _start_process, pid_path
from src.conductor_cli.ops_support import load_settings


def test_rotate_keeps_a_bounded_history_and_drops_the_oldest(tmp_path: Path) -> None:
    path = tmp_path / "backend.log"
    path.write_text("a" * 20, encoding="utf-8")
    assert rotate_log(path, max_bytes=10, backup_count=2) is True
    assert not path.exists()
    assert (tmp_path / "backend.log.1").read_text(encoding="utf-8") == "a" * 20

    path.write_text("b" * 20, encoding="utf-8")
    assert rotate_log(path, max_bytes=10, backup_count=2) is True
    path.write_text("c" * 20, encoding="utf-8")
    assert rotate_log(path, max_bytes=10, backup_count=2) is True
    assert (tmp_path / "backend.log.1").read_text(encoding="utf-8") == "c" * 20
    assert (tmp_path / "backend.log.2").read_text(encoding="utf-8") == "b" * 20
    assert not (tmp_path / "backend.log.3").exists()


def test_a_short_log_and_a_disabled_limit_stay_put(tmp_path: Path) -> None:
    path = tmp_path / "web.log"
    path.write_text("small", encoding="utf-8")
    assert rotate_log(path, max_bytes=10, backup_count=2) is False
    assert path.read_text(encoding="utf-8") == "small"
    path.write_text("x" * 20, encoding="utf-8")
    assert rotate_log(path, max_bytes=0, backup_count=2) is False
    assert rotate_log(path, max_bytes=10, backup_count=0) is False
    assert path.read_text(encoding="utf-8") == "x" * 20
    assert rotate_log(tmp_path / "missing.log", max_bytes=1, backup_count=1) is False


def test_a_log_at_the_limit_rotates(tmp_path: Path) -> None:
    path = tmp_path / "daemon.log"
    path.write_text("1234567890", encoding="utf-8")
    assert rotate_log(path, max_bytes=10, backup_count=1) is True
    assert (tmp_path / "daemon.log.1").read_text(encoding="utf-8") == "1234567890"


def test_a_lower_backup_count_drops_archives_above_it(tmp_path: Path) -> None:
    path = tmp_path / "backend.log"
    for index in range(1, 7):
        (tmp_path / f"backend.log.{index}").write_text(f"old-{index}", encoding="utf-8")
    path.write_text("n" * 20, encoding="utf-8")
    assert rotate_log(path, max_bytes=10, backup_count=2) is True
    assert (tmp_path / "backend.log.1").read_text(encoding="utf-8") == "n" * 20
    assert (tmp_path / "backend.log.2").read_text(encoding="utf-8") == "old-1"
    for index in range(3, 7):
        assert not (tmp_path / f"backend.log.{index}").exists()


def test_limits_come_from_the_environment(tmp_path: Path) -> None:
    path = tmp_path / "backend.log"
    path.write_text("x" * 30, encoding="utf-8")
    assert rotate_log(
        path,
        env={"CONDUCTOR_LOG_MAX_BYTES": "10", "CONDUCTOR_LOG_BACKUP_COUNT": "1"},
    )
    assert (tmp_path / "backend.log.1").read_text(encoding="utf-8") == "x" * 30
    assert log_limits({"CONDUCTOR_LOG_MAX_BYTES": "nope"}) == (
        DEFAULT_MAX_BYTES,
        DEFAULT_BACKUP_COUNT,
    )
    assert log_limits({"CONDUCTOR_LOG_BACKUPS": "4"})[1] == 4
    assert (
        log_limits(
            {"CONDUCTOR_LOG_BACKUPS": "4", "CONDUCTOR_LOG_BACKUP_COUNT": "2"}
        )[1]
        == 2
    )
    assert log_limits({"CONDUCTOR_LOG_MAX_BYTES": "0"})[0] == 0


class _Open:
    """Records the log popen was given. Does not rotate it."""

    def __init__(self) -> None:
        self.max_bytes = None
        self.backups = None
        self.log_path: Path | None = None

    def port_open(self, host: str, port: int, timeout: float = 0.3) -> bool:
        return False

    def popen(self, argv, *, cwd, env, log_path, max_bytes, backups) -> int:
        self.max_bytes = max_bytes
        self.backups = backups
        self.log_path = log_path
        log_path.write_text("opened\n", encoding="utf-8")
        return 50


def test_backend_web_and_daemon_logs_rotate_when_opened(tmp_path: Path) -> None:
    settings = load_settings(
        {
            "HOME": str(tmp_path),
            "CONDUCTOR_HOME": str(tmp_path / "conductor"),
            "CONDUCTOR_REPO_ROOT": str(tmp_path / "repo"),
            "CONDUCTOR_LOG_MAX_BYTES": "10",
            "CONDUCTOR_LOG_BACKUP_COUNT": "2",
        }
    )
    assert settings.log_max_bytes == 10
    assert settings.log_backups == 2
    settings.log_dir.mkdir(parents=True)
    cases = (
        (Component("backend"), settings.log_dir / "backend.log"),
        (Component("web"), settings.log_dir / "web.log"),
        (
            Component("daemon", "codex", "local"),
            settings.log_dir / "daemon-codex-local.log",
        ),
    )
    for component, path in cases:
        path.write_text("z" * 30, encoding="utf-8")
        system = _Open()
        _start_process(
            system, settings, component, ["echo"], tmp_path, {}, "marker"
        )
        archive = Path(f"{path}.1")
        assert archive.read_text(encoding="utf-8") == "z" * 30
        assert system.log_path == path
        assert system.max_bytes == 10
        assert system.backups == 2
        assert path.read_text(encoding="utf-8") == "opened\n"


# Writes one full chunk, waits until that chunk has been archived, then
# writes a short tail. The tail is how the test sees that the same process
# kept writing after the rotation. The tail stays in the pipe until the
# archive exists, so a late pump cannot rotate the chunk and the tail as
# one file. The wait is the same one the test uses.
_LIVE_WAIT_SECONDS = 20
_LIVE_WRITER = f"""
import os, sys, time
from pathlib import Path
log = Path(os.environ["CONDUCTOR_TEST_LOG"])
sys.stdout.buffer.write(b"z" * 80)
sys.stdout.buffer.flush()
archive = Path(str(log) + ".1")
deadline = time.time() + {_LIVE_WAIT_SECONDS}
while time.time() < deadline and not archive.exists():
    time.sleep(0.05)
if not archive.exists():
    raise SystemExit(0)
sys.stdout.buffer.write(b"TAIL!!")
sys.stdout.buffer.flush()
time.sleep(60)
"""


def test_pump_rotates_a_short_write_before_the_writer_closes(tmp_path: Path) -> None:
    """A pipe that stays open still rotates once the file crosses the limit.

    ``read(size)`` on that pipe waits until ``size`` bytes arrive or the
    writer closes. 80 bytes never fill a 64 KiB read, so the archive would
    be missing for as long as the writer stayed alive.
    """
    read_fd, write_fd = os.pipe()
    path = tmp_path / "backend.log"
    failures: list[BaseException] = []

    def _run() -> None:
        source = os.fdopen(read_fd, "rb", buffering=0)
        try:
            pump_log(source, path, max_bytes=10, backup_count=2)
        except BaseException as exc:
            failures.append(exc)
        finally:
            source.close()

    thread = threading.Thread(target=_run)
    thread.start()
    try:
        assert os.write(write_fd, b"z" * 80) == 80
        archive = tmp_path / "backend.log.1"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (
            archive.is_file() and archive.read_bytes() == b"z" * 80
        ):
            time.sleep(0.02)
        assert archive.read_bytes() == b"z" * 80
        assert os.write(write_fd, b"TAIL!!") == 6
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if path.read_bytes() == b"TAIL!!":
                    break
            except FileNotFoundError:
                pass
            time.sleep(0.02)
        assert path.read_bytes() == b"TAIL!!"
        assert path.stat().st_size < 80
    finally:
        os.close(write_fd)
        thread.join(5)
    assert failures == []
    assert not thread.is_alive()


def test_running_backend_web_and_daemon_logs_rotate_without_a_restart(
    tmp_path: Path,
) -> None:
    from src.conductor_cli.ops_support import RealSystem

    class _System(RealSystem):
        # The suite forbids sockets. Nothing is listening; the probe would
        # only refuse a second backend, which this start is not.
        def port_open(self, host: str, port: int, timeout: float = 0.3) -> bool:
            return False

    settings = load_settings(
        {
            "HOME": str(tmp_path),
            "CONDUCTOR_HOME": str(tmp_path / "conductor"),
            "CONDUCTOR_REPO_ROOT": str(tmp_path / "repo"),
            "CONDUCTOR_LOG_MAX_BYTES": "10",
            "CONDUCTOR_LOG_BACKUP_COUNT": "2",
            "CONDUCTOR_BACKEND_PORT": "43123",
            "CONDUCTOR_WEB_PORT": "43124",
        }
    )
    settings.log_dir.mkdir(parents=True)
    system = _System()
    cases = (
        (Component("backend"), settings.log_dir / "backend.log"),
        (Component("web"), settings.log_dir / "web.log"),
        (
            Component("daemon", "codex", "local"),
            settings.log_dir / "daemon-codex-local.log",
        ),
    )
    started: list[int] = []
    try:
        for component, path in cases:
            env = {"PATH": os.environ.get("PATH", "/usr/bin"), "CONDUCTOR_TEST_LOG": str(path)}
            pid = _start_process(
                system,
                settings,
                component,
                [sys.executable, "-S", "-c", _LIVE_WRITER],
                tmp_path,
                env,
                "marker",
            )
            started.append(pid)
            archive = Path(f"{path}.1")
            deadline = time.monotonic() + _LIVE_WAIT_SECONDS
            while time.monotonic() < deadline:
                # The rename and the new file land as two steps. A read in
                # between raises, and that is the pump still rotating, not a
                # finished log.
                try:
                    current = path.read_bytes() if path.is_file() else b""
                except FileNotFoundError:
                    current = b""
                if archive.is_file() and current == b"TAIL!!":
                    break
                time.sleep(0.05)
            assert system.alive(pid)
            recorded = pid_path(settings, component).read_text(encoding="utf-8")
            assert recorded.splitlines()[0] == str(pid)
            assert archive.read_bytes() == b"z" * 80
            assert path.read_bytes() == b"TAIL!!"
            assert path.stat().st_size < 80
    finally:
        for pid in started:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if all(not system.alive(pid) for pid in started):
                break
            time.sleep(0.05)
        for pump in list(getattr(system, "_log_pumps", ())):
            if pump.poll() is None:
                pump.kill()
            pump.wait(timeout=5)


class _ChunkSource:
    """A pipe stand-in with no file descriptor: one chunk per read, then EOF."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.reads = 0

    def read1(self, _size: int) -> bytes:
        self.reads += 1
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def test_pump_survives_a_failed_write_and_writes_later_chunks(
    tmp_path: Path, monkeypatch
) -> None:
    import errno

    from src.conductor_cli import log_rotation

    real_write = os.write
    failures = {"left": 2}

    def flaky_write(fd, data):
        if b"DROP" in bytes(data) and failures["left"] > 0:
            failures["left"] -= 1
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr(log_rotation.os, "write", flaky_write)
    path = tmp_path / "backend.log"
    source = _ChunkSource([b"one\n", b"DROP-a\n", b"DROP-b\n", b"two\n", b"three\n"])
    pump_log(source, path, max_bytes=0, backup_count=0)
    monkeypatch.undo()
    assert source.reads == 6  # every chunk and the EOF read
    assert path.read_bytes() == b"one\ntwo\nthree\n"


def test_pump_survives_a_failed_rotation(tmp_path: Path, monkeypatch) -> None:
    from src.conductor_cli import log_rotation

    real_rotate = log_rotation.rotate_log
    calls = {"n": 0}

    def flaky_rotate(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OSError("rename failed")
        return real_rotate(*args, **kwargs)

    monkeypatch.setattr(log_rotation, "rotate_log", flaky_rotate)
    path = tmp_path / "backend.log"
    source = _ChunkSource([b"a" * 12, b"b" * 12, b"c" * 12, b"TAIL"])
    pump_log(source, path, max_bytes=10, backup_count=2)
    assert source.reads == 5
    assert calls["n"] == 3
    # Two rotations failed, so the third moved all three chunks at once.
    assert (tmp_path / "backend.log.1").read_bytes() == b"a" * 12 + b"b" * 12 + b"c" * 12
    assert path.read_bytes() == b"TAIL"


def test_pump_survives_a_failed_reopen(tmp_path: Path, monkeypatch) -> None:
    from src.conductor_cli import log_rotation

    path = tmp_path / "backend.log"
    real_open = os.open
    state = {"fail": 0}

    def flaky_open(target, flags, mode=0o777, *args, **kwargs):
        if str(target) == str(path) and state["fail"] > 0:
            state["fail"] -= 1
            raise PermissionError(13, "Permission denied")
        return real_open(target, flags, mode, *args, **kwargs)

    class _Source(_ChunkSource):
        def read1(self, size: int) -> bytes:
            chunk = super().read1(size)
            if chunk == b"x" * 12:
                # Rotation after this chunk re-opens, and so does the next chunk.
                state["fail"] = 2
            return chunk

    monkeypatch.setattr(log_rotation.os, "open", flaky_open)
    source = _Source([b"x" * 12, b"lost\n", b"kept\n"])
    pump_log(source, path, max_bytes=10, backup_count=2)
    monkeypatch.undo()
    assert source.reads == 4
    assert (tmp_path / "backend.log.1").read_bytes() == b"x" * 12
    assert path.read_bytes() == b"kept\n"


def test_pump_process_drains_the_pipe_and_exits_zero_when_the_log_is_unwritable(
    tmp_path: Path,
) -> None:
    """The writer never sees a broken pipe, even if no byte can be logged."""
    from src.conductor_cli.log_rotation import spawn_log_pump

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file", encoding="utf-8")
    path = blocker / "backend.log"
    pump, write_fd = spawn_log_pump(path, max_bytes=10, backup_count=2)
    try:
        payload = b"y" * 4096
        for _ in range(64):  # 256 KiB, past any pipe buffer
            assert os.write(write_fd, payload) == len(payload)
    finally:
        os.close(write_fd)
    assert pump.wait(timeout=30) == 0
    assert not path.exists()
