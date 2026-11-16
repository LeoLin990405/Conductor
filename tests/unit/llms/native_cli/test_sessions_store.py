"""The flat session map: what it keeps, and how two writers share it.

The map is the runner's memory of which provider session a conversation
continues in. It is a flat ``{key: session_id}`` file, and it is written by
more than one program: a dispatcher and a worker may both hold it, and each
of them must find the other's keys when it writes.

These tests hold three things at once: the map keeps its shape and its
privacy, a write keeps the other writer's keys, and a write that fails leaves
the cache no further ahead of the disk than it was. On top of that, a map
that cannot be read is never mistaken for an empty map: only a file that is
not there is empty, and a write that cannot read the map is refused.
"""

from __future__ import annotations

import inspect
import json
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from src.llms.native_cli.sessions import NativeCliSessionStore, SessionStoreUnavailable


REPO_ROOT = Path(__file__).resolve().parents[4]

KEY_A = "thread:a:kimi"
KEY_B = "thread:b:kimi"


def _run_threads(targets) -> None:
    """Run the callables at the same time, and report what any of them raised.

    A writer that waits for the lock forever would hang the whole run, so
    every thread is given a deadline and a thread that is still alive when
    the deadline passes fails the test.
    """
    errors: list[BaseException] = []

    def _wrapped(target):
        def _inner() -> None:
            try:
                target()
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        return _inner

    threads = [threading.Thread(target=_wrapped(target)) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    alive = [thread for thread in threads if thread.is_alive()]
    assert not alive, "a writer is still waiting: the lock did not go through"
    if errors:
        raise errors[0]


class _Counter:
    """A writer that adds to a counter kept in the flat map.

    This is the shape of a manager that records a turn: read the count that
    is there, add to it, write it back. The count only adds up when the read
    and the write happen under the same lock, which is what the store's lock
    is for.
    """

    def __init__(self, store: NativeCliSessionStore, key: str) -> None:
        self._store = store
        self._key = key

    def add(self, amount: int) -> None:
        with self._store.locked():
            current = int(self._store.get(self._key) or "0")
            # The gap between the read and the write is where a writer
            # without the lock loses the other writer's count.
            time.sleep(0.001)
            self._store.set(self._key, str(current + amount))


# ----------------------------------------------------------------------
# two writers, one file


def test_two_writers_of_different_keys_keep_both_keys(tmp_path) -> None:
    path = tmp_path / "sessions.json"
    first = NativeCliSessionStore(path)
    second = NativeCliSessionStore(path)
    barrier = threading.Barrier(2)

    def _write(store: NativeCliSessionStore, key: str, value: str) -> None:
        for _ in range(25):
            barrier.wait(timeout=10)
            store.set(key, value)

    _run_threads(
        [
            lambda: _write(first, KEY_A, "sess-a"),
            lambda: _write(second, KEY_B, "sess-b"),
        ]
    )

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {KEY_A: "sess-a", KEY_B: "sess-b"}


def test_two_writers_adding_to_the_same_key_add_up(tmp_path) -> None:
    path = tmp_path / "sessions.json"
    first = _Counter(NativeCliSessionStore(path), "turns")
    second = _Counter(NativeCliSessionStore(path), "turns")
    tokens = threading.Barrier(2)

    def _add(counter: _Counter) -> None:
        tokens.wait(timeout=10)
        counter.add(1)

    _run_threads([lambda: _add(first), lambda: _add(second)])

    written = json.loads(path.read_text(encoding="utf-8"))
    # Two turns, not one: neither writer lost the other's count.
    assert written["turns"] == "2"


def test_two_writers_adding_to_the_same_key_add_up_their_tokens(tmp_path) -> None:
    path = tmp_path / "sessions.json"
    first = _Counter(NativeCliSessionStore(path), "tokens")
    second = _Counter(NativeCliSessionStore(path), "tokens")
    start = threading.Barrier(2)

    def _add(counter: _Counter) -> None:
        start.wait(timeout=10)
        counter.add(100)

    _run_threads([lambda: _add(first), lambda: _add(second)])

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["tokens"] == "200"


def test_two_writers_of_the_same_key_leave_one_entry(tmp_path) -> None:
    path = tmp_path / "sessions.json"
    first = NativeCliSessionStore(path)
    second = NativeCliSessionStore(path)
    barrier = threading.Barrier(2)

    def _write(store: NativeCliSessionStore, value: str) -> None:
        for _ in range(25):
            barrier.wait(timeout=10)
            store.set(KEY_A, value)

    _run_threads(
        [
            lambda: _write(first, "sess-a"),
            lambda: _write(second, "sess-b"),
        ]
    )

    written = json.loads(path.read_text(encoding="utf-8"))
    assert list(written) == [KEY_A]
    assert written[KEY_A] in {"sess-a", "sess-b"}


_CHILD = """
import sys

sys.path.insert(0, {root!r})

from src.llms.native_cli.sessions import NativeCliSessionStore

store = NativeCliSessionStore({path!r})
for _ in range(30):
    store.set({key!r}, {value!r})
"""


def test_two_processes_of_different_keys_keep_both_keys(tmp_path) -> None:
    path = tmp_path / "sessions.json"
    children = []
    for key, value in ((KEY_A, "sess-a"), (KEY_B, "sess-b")):
        program = _CHILD.format(
            root=str(REPO_ROOT), path=str(path), key=key, value=value
        )
        children.append(
            subprocess.Popen(
                [sys.executable, "-c", program], cwd=str(REPO_ROOT)
            )
        )
    for child in children:
        assert child.wait(timeout=60) == 0

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {KEY_A: "sess-a", KEY_B: "sess-b"}


# ----------------------------------------------------------------------
# the lock


def test_the_lock_lives_next_to_the_map(tmp_path) -> None:
    path = tmp_path / "sessions.json"
    store = NativeCliSessionStore(path)

    assert store.lock_path == path.parent / ".sessions.json.lock"

    store.set(KEY_A, "sess-a")

    assert store.lock_path.exists()
    assert stat.S_IMODE(store.lock_path.stat().st_mode) == 0o600


def test_the_lock_is_reentrant(tmp_path) -> None:
    store = NativeCliSessionStore(tmp_path / "sessions.json")
    entered = threading.Event()

    def _hold() -> None:
        with store.locked():
            entered.set()
            with store.locked():
                # The inner block must not wait for the outer one.
                store.set(KEY_A, "sess-a")

    worker = threading.Thread(target=_hold)
    worker.start()
    worker.join(timeout=30)
    assert not worker.is_alive(), "the nested lock waited for itself"
    assert entered.is_set()

    assert store.get(KEY_A) == "sess-a"
    assert json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8")) == {
        KEY_A: "sess-a"
    }


def test_a_write_inside_the_lock_does_not_wait_for_itself(tmp_path) -> None:
    # This is how the worker session manager uses the store: it holds the
    # lock for a step that touches more than one file, and writes the flat
    # map inside that step.
    path = tmp_path / "sessions.json"
    store = NativeCliSessionStore(path)
    with store.locked():
        store.set(KEY_A, "sess-a")

    assert json.loads(path.read_text(encoding="utf-8")) == {KEY_A: "sess-a"}


def test_a_second_store_of_the_same_file_does_not_wait_for_itself(tmp_path) -> None:
    # Two stores over one file are two readers of one lock. The second one
    # must find the lock already held rather than wait for it: ``flock``
    # belongs to the open file description, so a second descriptor for the
    # same file would block against the first even in this very process.
    path = tmp_path / "sessions.json"
    first = NativeCliSessionStore(path)
    second = NativeCliSessionStore(path)

    with first.locked():
        with second.locked():
            second.set(KEY_A, "sess-a")

    assert json.loads(path.read_text(encoding="utf-8")) == {KEY_A: "sess-a"}


def test_the_lock_is_released_after_the_block(tmp_path) -> None:
    path = tmp_path / "sessions.json"
    store = NativeCliSessionStore(path)
    with store.locked():
        pass

    # The lock is free again: another writer takes it without waiting.
    other = NativeCliSessionStore(path)
    with other.locked():
        other.set(KEY_A, "sess-a")

    assert json.loads(path.read_text(encoding="utf-8")) == {KEY_A: "sess-a"}


# ----------------------------------------------------------------------
# the shape of the map is not changed


def test_a_key_written_by_another_process_survives_a_write(tmp_path) -> None:
    path = tmp_path / "sessions.json"
    path.write_text(
        json.dumps({KEY_B: "sess-b"}) + "\n", encoding="utf-8"
    )
    store = NativeCliSessionStore(path)

    store.set(KEY_A, "sess-a")

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {KEY_B: "sess-b", KEY_A: "sess-a"}


def test_the_map_is_still_a_flat_map_of_strings(tmp_path) -> None:
    path = tmp_path / "sessions.json"
    store = NativeCliSessionStore(path)

    store.set(KEY_A, "sess-a")

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {KEY_A: "sess-a"}
    assert all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in written.items()
    )


def test_the_map_is_still_private(tmp_path) -> None:
    path = tmp_path / "sessions.json"
    store = NativeCliSessionStore(path)

    store.set(KEY_A, "sess-a")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_cache_holds_what_the_disk_holds_after_a_write(tmp_path) -> None:
    path = tmp_path / "sessions.json"
    store = NativeCliSessionStore(path)

    store.set(KEY_A, "sess-a")

    # A store that starts now reads the same file.
    assert NativeCliSessionStore(path).get(KEY_A) == "sess-a"
    assert NativeCliSessionStore(path).keys() == (KEY_A,)


def test_get_set_clear_and_keys_still_behave(tmp_path) -> None:
    store = NativeCliSessionStore(tmp_path / "sessions.json")

    assert store.get(KEY_A) is None
    assert store.keys() == ()

    store.set(KEY_A, "sess-a")
    assert store.get(KEY_A) == "sess-a"
    assert store.keys() == (KEY_A,)

    store.clear(KEY_A)
    assert store.get(KEY_A) is None
    assert store.keys() == ()


def test_the_keys_are_sorted(tmp_path) -> None:
    store = NativeCliSessionStore(tmp_path / "sessions.json")

    store.set("thread:z:kimi", "sess-z")
    store.set("thread:a:kimi", "sess-a")
    store.set("thread:m:kimi", "sess-m")

    assert store.keys() == ("thread:a:kimi", "thread:m:kimi", "thread:z:kimi")


def test_a_clear_leaves_the_other_keys_alone(tmp_path) -> None:
    path = tmp_path / "sessions.json"
    store = NativeCliSessionStore(path)
    store.set(KEY_A, "sess-a")
    store.set(KEY_B, "sess-b")

    store.clear(KEY_A)

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {KEY_B: "sess-b"}


def test_a_file_that_cannot_be_read_raises(tmp_path) -> None:
    # Only a file that is not there is an empty map. A file that is there
    # and cannot be parsed is an error: a reader that reported it as empty
    # would say "there is no session here" about a map that holds one.
    path = tmp_path / "sessions.json"
    path.write_text("{not json at all", encoding="utf-8")
    store = NativeCliSessionStore(path)

    with pytest.raises(SessionStoreUnavailable):
        store.get(KEY_A)
    with pytest.raises(SessionStoreUnavailable):
        store.keys()


def test_a_write_over_a_file_that_cannot_be_read_writes_nothing(tmp_path) -> None:
    # The write path is not the read path. A file that is there and cannot
    # be read is NOT an empty map: a writer that read it as empty would put
    # its own one key over the file and lose every key of every other
    # writer. So the write is refused and the file is left exactly as it was.
    path = tmp_path / "sessions.json"
    original = "{not json at all"
    path.write_text(original, encoding="utf-8")
    store = NativeCliSessionStore(path)
    before = path.stat().st_mtime_ns

    with pytest.raises(SessionStoreUnavailable):
        store.set(KEY_A, "sess-a")

    assert path.read_text(encoding="utf-8") == original
    assert path.stat().st_mtime_ns == before


def test_a_read_error_does_not_lose_the_other_keys(tmp_path) -> None:
    # A write is based on what is on the disk, and a read that fails must
    # not be mistaken for an empty disk: the write would then hold one key
    # where the file held two. The read is not tried again, so a failed
    # read stops the write and the file keeps every key it had.
    path = tmp_path / "sessions.json"
    original = json.dumps({KEY_A: "old-a", KEY_B: "keep-b"}) + "\n"
    path.write_text(original, encoding="utf-8")
    store = NativeCliSessionStore(path)
    store.get(KEY_A)  # warm the cache, so the write really reads the disk
    before = path.stat().st_mtime_ns

    real_read_text = Path.read_text
    reads = [0]

    def fail_one_read(candidate, *args, **kwargs):
        if candidate == path:
            reads[0] += 1
            if reads[0] == 2:
                raise PermissionError("injected one-shot read failure")
        return real_read_text(candidate, *args, **kwargs)

    Path.read_text = fail_one_read
    try:
        with pytest.raises(SessionStoreUnavailable):
            store.set(KEY_A, "new-a")
    finally:
        Path.read_text = real_read_text

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {KEY_A: "old-a", KEY_B: "keep-b"}
    assert path.stat().st_mtime_ns == before


def test_a_write_that_cannot_read_the_map_at_all_writes_nothing(tmp_path) -> None:
    # When the map cannot be read at all -- not even on the first try --
    # the write does not happen. The file keeps every key it had, down to
    # the byte and the mtime, and the store does not pretend the map was
    # empty.
    path = tmp_path / "sessions.json"
    original = json.dumps({KEY_A: "old-a", KEY_B: "keep-b"}) + "\n"
    path.write_text(original, encoding="utf-8")
    store = NativeCliSessionStore(path)
    before = path.stat().st_mtime_ns

    real_read_text = Path.read_text

    def always_fail(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("injected read failure")
        return real_read_text(candidate, *args, **kwargs)

    Path.read_text = always_fail
    try:
        with pytest.raises(SessionStoreUnavailable):
            store.set(KEY_A, "new-a")
    finally:
        Path.read_text = real_read_text

    # Both keys of both writers are still there, the content is the same
    # byte for byte, and the file was not even touched.
    assert path.read_text(encoding="utf-8") == original
    assert json.loads(path.read_text(encoding="utf-8")) == {
        KEY_A: "old-a",
        KEY_B: "keep-b",
    }
    assert path.stat().st_mtime_ns == before
    # The aborted write left no temp file of its own behind.
    assert not list(tmp_path.glob(".sessions-*.json"))


def test_a_write_that_cannot_read_the_map_because_of_an_os_error_writes_nothing(
    tmp_path,
) -> None:
    # The same for a plain OSError, which is not a PermissionError: any
    # other OSError is just as unable to read the map.
    path = tmp_path / "sessions.json"
    original = json.dumps({KEY_A: "old-a", KEY_B: "keep-b"}) + "\n"
    path.write_text(original, encoding="utf-8")
    store = NativeCliSessionStore(path)
    before = path.stat().st_mtime_ns

    real_read_text = Path.read_text

    def always_fail(candidate, *args, **kwargs):
        if candidate == path:
            raise OSError("injected read failure")
        return real_read_text(candidate, *args, **kwargs)

    Path.read_text = always_fail
    try:
        with pytest.raises(SessionStoreUnavailable):
            store.set(KEY_A, "new-a")
    finally:
        Path.read_text = real_read_text

    assert path.read_text(encoding="utf-8") == original
    assert json.loads(path.read_text(encoding="utf-8")) == {
        KEY_A: "old-a",
        KEY_B: "keep-b",
    }
    assert path.stat().st_mtime_ns == before


def test_a_failed_read_is_not_retried(tmp_path) -> None:
    # The write is refused on the first failure. A read that is tried again
    # would be a second chance to write over a map that nobody can read.
    path = tmp_path / "sessions.json"
    original = json.dumps({KEY_A: "old-a"}) + "\n"
    path.write_text(original, encoding="utf-8")
    store = NativeCliSessionStore(path)

    real_read_text = Path.read_text
    reads = []

    def always_fail(candidate, *args, **kwargs):
        if candidate == path:
            reads.append(candidate)
            raise PermissionError("injected read failure")
        return real_read_text(candidate, *args, **kwargs)

    Path.read_text = always_fail
    try:
        with pytest.raises(SessionStoreUnavailable):
            store.set(KEY_A, "new-a")
    finally:
        Path.read_text = real_read_text

    assert len(reads) == 1
    assert path.read_text(encoding="utf-8") == original


def test_a_file_that_cannot_be_opened_raises(tmp_path) -> None:
    # A file that cannot be opened is not an empty map either: the reader
    # says the store is unavailable instead of saying there is no session.
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({KEY_A: "sess-a"}) + "\n", encoding="utf-8")
    store = NativeCliSessionStore(path)
    path.chmod(0o000)
    try:
        try:
            readable = store.get(KEY_A)
        except SessionStoreUnavailable:
            readable = None
        if readable == "sess-a":  # the owner may read it anyway
            pytest.skip("this user can read the file regardless of its mode")
        with pytest.raises(SessionStoreUnavailable):
            store.get(KEY_A)
        with pytest.raises(SessionStoreUnavailable):
            store.keys()
    finally:
        path.chmod(0o600)


def test_a_missing_file_is_still_an_empty_map(tmp_path) -> None:
    # A file that is not there has nothing to lose, so it is the one empty
    # state: a reader finds no session, and a write creates the file.
    path = tmp_path / "sessions.json"
    store = NativeCliSessionStore(path)

    assert store.get(KEY_A) is None
    assert store.keys() == ()

    store.set(KEY_A, "sess-a")

    assert json.loads(path.read_text(encoding="utf-8")) == {KEY_A: "sess-a"}
    assert store.get(KEY_A) == "sess-a"


# ----------------------------------------------------------------------
# a write that fails


def test_the_write_method_still_takes_one_argument() -> None:
    parameters = list(
        inspect.signature(NativeCliSessionStore._save).parameters.values()
    )

    assert [parameter.name for parameter in parameters] == ["self", "data"]


def test_a_failed_write_leaves_the_cache_where_the_disk_is(tmp_path) -> None:
    path = tmp_path / "sessions.json"
    store = NativeCliSessionStore(path)
    store.set(KEY_A, "sess-old")

    def refuse_legacy(_):
        raise OSError("injected legacy persistence failure")

    store._save = refuse_legacy
    with pytest.raises(OSError):
        store.set(KEY_A, "sess-new")

    # The cache is not ahead of the disk: the old value is still the truth.
    assert store.get(KEY_A) == "sess-old"
    assert json.loads(path.read_text(encoding="utf-8")) == {KEY_A: "sess-old"}


def test_a_failed_clear_keeps_the_old_session_id(tmp_path) -> None:
    # This is the rotation: the worker session state is written, and the
    # flat map that points at the old session is dropped. When that drop
    # fails, the map must still name the old session, or the next turn would
    # resume a session the state file no longer holds.
    path = tmp_path / "sessions.json"
    store = NativeCliSessionStore(path)
    store.set(KEY_A, "sess-old")

    def refuse_legacy(_):
        raise OSError("injected legacy persistence failure")

    store._save = refuse_legacy
    with pytest.raises(OSError):
        store.clear(KEY_A)

    assert store.get(KEY_A) == "sess-old"
    assert json.loads(path.read_text(encoding="utf-8")) == {KEY_A: "sess-old"}


def test_a_write_after_a_failed_one_merges_onto_the_disk(tmp_path) -> None:
    path = tmp_path / "sessions.json"
    store = NativeCliSessionStore(path)
    store.set(KEY_A, "sess-old")

    save = store._save

    def refuse_legacy(_):
        raise OSError("injected legacy persistence failure")

    store._save = refuse_legacy
    with pytest.raises(OSError):
        store.clear(KEY_A)

    store._save = save
    store.set(KEY_B, "sess-b")

    # The merge is onto what is on disk, not onto the stale cache.
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written == {KEY_A: "sess-old", KEY_B: "sess-b"}
