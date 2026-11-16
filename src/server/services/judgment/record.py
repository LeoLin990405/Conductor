"""One record per judgment: the calibration sample (N61 4.1, 4.4).

A record never holds the material. ``state_digest`` keeps a sha256 and a
length per state key, which is enough to join and de-duplicate samples.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import os
import queue
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

RECORDS_ENV = "CONDUCTOR_JUDGMENT_RECORDS"


def digest_state(state: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """``{key: {"sha256": hex, "len": n}}``: hash and length, never the text."""
    out: dict[str, dict[str, Any]] = {}
    for key, value in state.items():
        text = value if isinstance(value, str) else str(value)
        out[str(key)] = {
            "sha256": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(),
            "len": len(text),
        }
    return out


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JudgmentRecord:
    use: str
    version: str
    mode: str
    question_ids: list[str]
    status: str
    question_status: dict[str, str]
    answers: dict[str, Any]
    probabilities: dict[str, dict[str, float]]
    latency_ms: int
    backend: str
    reason: str
    action: str
    state_digest: dict[str, dict[str, Any]]
    ts: str = field(default_factory=utc_now)
    # Extras beyond the contract: the join key for the offline eval (a
    # ticket or line id; never material) and the backend's token usage when
    # it reports any. ``None`` usage means unknown, not zero cost.
    subject: str = ""
    usage: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = str(self.status)
        data["question_status"] = {
            str(k): str(v) for k, v in self.question_status.items()
        }
        return data


class Recorder(Protocol):
    def write(self, record: JudgmentRecord) -> None:
        """Store one record. Must never raise."""
        ...


class MemoryRecorder:
    """Keeps records in a list. For tests."""

    def __init__(self) -> None:
        self.records: list[JudgmentRecord] = []

    def write(self, record: JudgmentRecord) -> None:
        self.records.append(record)


class NullRecorder:
    """Drops every record."""

    def write(self, record: JudgmentRecord) -> None:
        return None


class JsonlRecorder:
    """Appends one JSON line per record. Never raises.

    The directory is created on first write with mode 0700 (and so is any
    missing parent; existing directories are left alone); the file is
    created with mode 0600. Inside a running event loop the append runs on a
    single background *daemon* thread, in order, so the loop never waits on
    the disk and a stuck disk can never hold up interpreter exit; at most
    ``max_pending`` writes queue up and further records are dropped with a
    warning. Outside a loop the append is synchronous. ``flush()`` (sync) and
    ``drain()`` (async) wait for queued writes, each bounded by a timeout.
    """

    def __init__(self, path: Path, *, max_pending: int = 1000) -> None:
        self.path = Path(path)
        self.max_pending = max(1, int(max_pending))
        self._lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: set[concurrent.futures.Future[None]] = set()
        self._queue: queue.SimpleQueue[tuple[str, concurrent.futures.Future[None]]] = (
            queue.SimpleQueue()
        )
        self._thread: threading.Thread | None = None

    def write(self, record: JudgmentRecord) -> None:
        try:
            line = json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True)
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                self._append(line)
                return
            self._submit(line)
        except Exception:
            logger.warning(
                "[judgment] could not write a record to %s", self.path, exc_info=True
            )

    def flush(self, timeout: float | None = 5.0) -> None:
        """Block until queued writes finish (or ``timeout``). Never raises."""
        try:
            pending = self._snapshot()
            if pending:
                concurrent.futures.wait(pending, timeout=timeout)
        except Exception:
            logger.warning("[judgment] could not flush %s", self.path, exc_info=True)

    async def drain(self, timeout: float | None = 5.0) -> None:
        """Await queued writes without blocking the loop, up to ``timeout``. Never raises."""
        try:
            pending = self._snapshot()
            if not pending:
                return
            await asyncio.wait(
                [asyncio.wrap_future(f) for f in pending], timeout=timeout
            )
        except Exception:
            logger.warning("[judgment] could not drain %s", self.path, exc_info=True)

    def _snapshot(self) -> list[concurrent.futures.Future[None]]:
        with self._pending_lock:
            return list(self._pending)

    def _submit(self, line: str) -> None:
        with self._pending_lock:
            if len(self._pending) >= self.max_pending:
                logger.warning(
                    "[judgment] record queue full (%d); dropping a record", self.max_pending
                )
                return
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._worker, name="judgment-record", daemon=True
                )
                self._thread.start()
            future: concurrent.futures.Future[None] = concurrent.futures.Future()
            self._pending.add(future)
        future.add_done_callback(self._done)
        self._queue.put((line, future))

    def _worker(self) -> None:
        while True:
            line, future = self._queue.get()
            try:
                self._append_safely(line)
            finally:
                future.set_result(None)

    def _done(self, future: concurrent.futures.Future[None]) -> None:
        with self._pending_lock:
            self._pending.discard(future)

    def _append_safely(self, line: str) -> None:
        try:
            self._append(line)
        except Exception:
            logger.warning(
                "[judgment] could not write a record to %s", self.path, exc_info=True
            )

    def _append(self, line: str) -> None:
        data = (line + "\n").encode("utf-8")
        with self._lock:
            _make_private_dirs(self.path.parent)
            # O_RDWR, not O_WRONLY: the torn-line check reads the last byte.
            flags = os.O_CREAT | os.O_APPEND | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
            fd = os.open(self.path, flags, 0o600)
            try:
                if _ends_mid_line(fd):
                    # A torn earlier append: end it so this record starts on
                    # its own line instead of merging with the fragment.
                    data = b"\n" + data
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
            finally:
                os.close(fd)


def _ends_mid_line(fd: int) -> bool:
    """True when the file is non-empty and its last byte is not a newline."""
    size = os.fstat(fd).st_size
    if size <= 0:
        return False
    return os.pread(fd, 1, size - 1) != b"\n"


def _make_private_dirs(directory: Path) -> None:
    """Create ``directory`` and its missing parents with mode 0700.

    Directories that already exist keep their mode.
    """
    if directory.is_dir():
        return
    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for path in reversed(missing):
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            continue
        os.chmod(path, 0o700)  # mkdir's mode is masked by the umask


def default_records_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    override = str(env.get(RECORDS_ENV, "") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".conductor" / "judgment" / "records.jsonl"


_SHARED: dict[Path, JsonlRecorder] = {}
_SHARED_LOCK = threading.Lock()


def default_recorder(environ: Mapping[str, str] | None = None) -> Recorder:
    """The shared ``JsonlRecorder`` at ``$CONDUCTOR_JUDGMENT_RECORDS`` or the home default.

    One instance (and one writer thread) per path for the whole process, so
    repeated ``default_port()`` calls never pile up threads.
    """
    path = default_records_path(environ)
    with _SHARED_LOCK:
        recorder = _SHARED.get(path)
        if recorder is None:
            recorder = _SHARED[path] = JsonlRecorder(path)
        return recorder


async def drain_shared(timeout: float = 3.0) -> None:
    """Await every shared recorder's queued writes, bounded by ``timeout``. Never raises."""
    try:
        with _SHARED_LOCK:
            recorders = list(_SHARED.values())
        for recorder in recorders:
            await recorder.drain(timeout)
    except Exception:
        logger.warning("[judgment] could not drain the shared recorders", exc_info=True)
