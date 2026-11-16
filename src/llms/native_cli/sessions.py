"""Persistent native-agent session ids.

A provider CLI caches the prompt prefix of a *session*. Reusing the id is
therefore the difference between paying full price every turn and reading the
cached prefix. The mapping is keyed by Conductor's own identity (for example
``thread:<thread_id>:deepseek-dsh``), so a later turn of the same conversation
resumes the same CLI session.

Session ids are not secrets, but they are conversation pointers: the file is
private (0600) and contains no provider credentials or prompt text.

Two programs may hold the same map at once -- a dispatcher and a worker, say.
A write that took the cached copy and put it back would erase the other
writer's key, so every write takes the store's lock, re-reads the map from
disk, applies its own key alone, and writes the result back atomically.

Only a map that is not there is an empty map. A map that is there and cannot
be read -- or that does not hold a map -- is an error, and it comes back as
:class:`SessionStoreUnavailable`. A reader must not report an empty map for a
file it could not read, and a writer that read it as empty would put its own
one key over the map and lose every key of every other writer.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Awaitable, Iterator, Mapping

from src.llms.native_cli.base import NativeCliResult

try:  # pragma: no cover - POSIX only, and this project is POSIX
    import fcntl
except ImportError:  # pragma: no cover - a platform without flock
    fcntl = None  # type: ignore[assignment]


DEFAULT_SESSION_PATH = Path.home() / ".cache" / "conductor" / "native-cli" / "sessions.json"


class SessionStoreUnavailable(OSError):
    """The session store could not be read, so nothing may be written to it."""


class _FileLock:
    """The lock of one file, shared by every holder of it in this process.

    ``flock`` belongs to the open file description, not to the process: a
    second, independent descriptor for the same file blocks against the first
    *even inside the same process*, so a program that locked its file twice
    would wait for itself. One entry per file keeps one descriptor and one
    depth counter for all of them, and the guard keeps two threads of this
    process out of each other's critical section.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.guard = threading.RLock()
        self.depth = 0
        self.fd: int | None = None


_LOCKS: dict[str, _FileLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> _FileLock:
    """The process-wide lock entry of ``path``.

    Two stores that point at the same file must share one entry, or the
    second one would block on a lock this process already holds.
    """
    try:
        key = str(path.resolve())
    except OSError:  # pragma: no cover - a path that cannot be resolved
        key = str(path)
    with _LOCKS_GUARD:
        entry = _LOCKS.get(key)
        if entry is None:
            entry = _FileLock(path)
            _LOCKS[key] = entry
        return entry


def _take_lock(path: Path) -> int | None:
    """Take the file lock, or return None on a platform without ``flock``.

    A lock that cannot be taken is not skipped quietly: the file must not be
    written by a writer that could not serialise itself against the others.
    """
    if fcntl is None:  # pragma: no cover - a platform without flock
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - a file system without permissions
        pass
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _drop_lock(fd: int | None) -> None:
    """Release a lock that :func:`_take_lock` took."""
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class NativeCliSessionStore:
    """Tiny JSON-backed map; atomic writes, no third-party dependency."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_SESSION_PATH
        self._data: dict[str, str] | None = None
        # How deep this store is inside its own lock. The counter is what
        # makes the lock re-entrant: a second block in the same store finds
        # the lock already held and does not take it again.
        self._lock_depth = 0

    # ------------------------------------------------------------------
    # the lock

    @property
    def lock_path(self) -> Path:
        """The file the store's lock lives on.

        The lock is a file of its own next to the map, so the atomic write of
        the map never replaces the lock. Its name is the map's name with a
        dot in front and ``.lock`` behind it, the same way the worker
        session state names its own.
        """
        return self.path.parent / f".{self.path.name}.lock"

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the store's lock for the duration of the block.

        A caller that has to write more than one file as one step -- the
        worker session state and the flat map that goes with it -- takes the
        lock here and writes inside the block. The store's own writes then
        find the lock already held and do not take it a second time: ``flock``
        belongs to the open file description, so a second descriptor for the
        same file would block against the first even in this very process.

        The block is re-entrant. Only the outermost block takes the lock and
        only the outermost block releases it, and entering it twice in one
        thread does not wait for itself.

        A caller must reach the lock through this method. Locking the file
        by hand and then calling :meth:`set` would deadlock the process
        against itself.
        """
        entry = _lock_for(self.lock_path)
        entry.guard.acquire()
        try:
            if entry.depth == 0:
                entry.fd = _take_lock(entry.path)
            entry.depth += 1
            self._lock_depth += 1
            try:
                if self._lock_depth == 1:
                    # The lock is what makes this view the current one: the
                    # map is re-read from disk, so a caller that reads inside
                    # the block sees what the other writers have left there.
                    self._data = self._read_disk()
                yield
            finally:
                self._lock_depth -= 1
                entry.depth -= 1
                if entry.depth == 0:
                    fd, entry.fd = entry.fd, None
                    _drop_lock(fd)
        finally:
            entry.guard.release()

    # ------------------------------------------------------------------
    # reading and writing

    def _read_disk(self) -> dict[str, str]:
        """The map as it is on disk right now.

        Only a file that is not there is an empty map -- there is nothing
        to lose. A file that *is* there and cannot be read is an **error**
        and not an empty map, and it comes back as
        :class:`SessionStoreUnavailable`. A reader that reported it as empty
        would say "there is no session here" about a map that holds one,
        and a writer that read it as empty would put its own one key over
        the map and lose every key of every other writer.
        """
        return self._read_strict()

    def _read_strict(self) -> dict[str, str]:
        """The map as it is on disk, or an error.

        A file that is not there is an empty map: there is nothing to lose.
        A file that *is* there and cannot be read -- a ``PermissionError``,
        any other ``OSError``, a file whose content is not valid JSON, or a
        file that does not hold a map at all -- is an error, and it is
        raised as :class:`SessionStoreUnavailable`.

        The read is tried once. A failed read is a verdict on the file and
        not a hiccup to ride out: the write that is based on this read must
        not go ahead on a second chance, because the second chance is
        another chance to write over a map that nobody can read.
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            raise SessionStoreUnavailable(
                f"the session map at {self.path} is not valid JSON"
            ) from exc
        except UnicodeDecodeError as exc:
            raise SessionStoreUnavailable(
                f"the session map at {self.path} is not valid UTF-8"
            ) from exc
        except OSError as exc:
            raise SessionStoreUnavailable(
                f"could not read the session map at {self.path}"
            ) from exc
        try:
            items = raw.items()
        except AttributeError as exc:
            raise SessionStoreUnavailable(
                f"the session map at {self.path} does not hold a map"
            ) from exc
        return {
            str(key): str(value)
            for key, value in items
            if isinstance(key, str) and isinstance(value, str) and value
        }

    def _load(self) -> dict[str, str]:
        if self._data is not None:
            return self._data
        data = self._read_disk()
        self._data = data
        return data

    def get(self, key: str) -> str | None:
        return self._load().get(key)

    def set(self, key: str, session_id: str) -> None:
        """Bind ``key`` to ``session_id``, leaving every other key alone.

        The write happens under the store's lock, and the map it writes is
        the one on disk, not the one this store happens to have cached: the
        key of another writer that saved in the meantime must survive.

        The map is read the strict way: a file that is there and cannot be
        read is an error and not an empty map, so a read failure stops the
        write -- as :class:`SessionStoreUnavailable` -- instead of replacing
        the map with this one key. The read is not retried: the write is
        refused on the first failure, and the file keeps its content and its
        mtime.
        """
        with self.locked():
            data = self._read_strict()
            data[key] = session_id
            self._save(data)
            # The cache is refreshed only once the file holds the new value:
            # a write that failed must not leave the cache ahead of the disk.
            self._data = data

    def clear(self, key: str) -> None:
        """Drop ``key``, leaving every other key alone."""
        with self.locked():
            data = self._read_strict()
            if key in data:
                del data[key]
                self._save(data)
            # As in :meth:`set`, the cache follows the disk and not the other
            # way round: a write that failed leaves the old value in place.
            self._data = data

    def snapshot(self) -> dict[str, str]:
        """The whole map as it is on disk, read the strict way.

        This is the read a caller does before it works out what the map will
        look like afterwards: a file that is there and cannot be read is an
        error and not an empty map, so the caller stops before it writes
        anything. The cache is not consulted and not filled -- the answer is
        what the disk holds right now, not what this store remembers.

        The caller that holds the store's lock while it reads and writes
        sees a map that cannot change under it, so the content it works out
        from this snapshot is the content it may write.
        """
        return self._read_strict()

    def write_snapshot(self, data: Mapping[str, str]) -> None:
        """Write the whole map, atomically, under the lock.

        The map is written as it is given: this is how a caller that has
        already read the map writes back what it worked out from that read,
        without a second read in between. A read that fails between the two
        would otherwise leave the two files disagreeing, so the caller is
        the one that has to decide when the read happened.

        The caller must hold the store's lock across its read and this write,
        or another writer may change the map in between.
        """
        with self.locked():
            payload = {str(key): str(value) for key, value in data.items()}
            self._save(payload)
            # The cache follows the disk and not the other way round: a write
            # that failed leaves the old value in place.
            self._data = payload

    def keys(self) -> tuple[str, ...]:
        """The keys the map holds.

        The map could answer "what is the session for this key?" for a long
        time without ever being able to say "which keys are there?". A caller
        that lists what it manages needs the second answer as much as the
        first: a session written by an older version of the code lives only
        in this map, and a reader that only walks the newer file never sees
        it.
        """
        return tuple(sorted(self._load()))

    def _save(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".sessions-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass


async def run_with_session(
    adapter: Any,
    prompt: str,
    *,
    session_key: str,
    store: NativeCliSessionStore | None = None,
    cwd: str | None = None,
    on_complete: Callable[[NativeCliResult], Awaitable[None]] | None = None,
) -> NativeCliResult:
    """Run a prompt, resuming the CLI session bound to ``session_key``."""
    sessions = store or NativeCliSessionStore()
    result = await adapter.run(
        prompt, session_id=sessions.get(session_key), cwd=cwd
    )
    if result.session_id:
        sessions.set(session_key, result.session_id)
    if on_complete is not None:
        await on_complete(result)
    return result
