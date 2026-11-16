"""The worker session state, read by the CLI on its own.

``conductor workers`` answers "is this worker healthy right now", and the
answer is mostly in one file on disk: the session manager's own state, which
holds one line per session key and one record per worker. The CLI used to
ask the session manager for that answer, which meant importing
``src.llms.native_cli.session_manager`` -- a module the wheel that ships
``conductor`` does not contain. In a checkout the import works and the
command works; in an installation the import fails and the command either
dies or, worse, keeps running with **no session data at all**.

So the CLI reads the file itself. This module is that reader: it parses the
state file, the legacy flat session map next to it, and answers the three
questions ``workers.py`` asks of a session manager:

* which session keys exist (:meth:`WorkerStateReader.legacy_keys` and the
  lines of the state file),
* what the current session of a key looks like
  (:meth:`WorkerStateReader.current`),
* how much of the provider's context window that session has used
  (:meth:`WorkerStateReader.usage_ratio`).

It only reads. A status command that changes the state it reports on is not
a status command, so the reader never finishes an interrupted rotation,
never writes a file and never takes a lock.

Reading the state file has **three** outcomes, and the command says
different things for each of them:

* the file is **missing** -- there is nothing to lose, and a session that
  lives only in the legacy map has used no context;
* the file is **valid** -- its content is reported;
* the file is **unreadable** (broken, a symlink, or over the size cap) --
  the sessions of the legacy map are still listed, and everything about
  the workers in that file is reported as **not known**. A file nobody
  read must not be reported as a file that holds nothing: that would say
  "this worker used no context and never failed" about a state nobody
  managed to look at.

The reader answers the session manager's ``read_only=True`` answers for
every state it can read. A test in ``tests/unit/conductor_cli/`` reads the
same file with both and compares the two answers; that test may import
``src``, because it runs in a checkout.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .worker_registry import (
    canonical_worker_id,
    context_window_tokens,
    merge_worker_records,
)

__all__ = [
    "LEGACY_FILE_NAME",
    "STATE_FILE_NAME",
    "STATE_MISSING",
    "STATE_UNREADABLE",
    "STATE_VALID",
    "SessionState",
    "WorkerStateReader",
    "read_json_file",
]

#: The three outcomes of reading the state file. They are not the same, and
#: the difference is exactly what the command says about a worker:
#:
#: * **missing** -- the file is not there. There is nothing to lose, and a
#:   session that lives only in the legacy map has used no context.
#: * **valid** -- the content was read.
#: * **unreadable** -- the file is there and cannot be used: it is broken,
#:   it is a symlink, or it is bigger than the cap. The command then says
#:   "not known" about the workers in it. It must **not** say "used no
#:   context and never failed": that is a claim about a file nobody read.
STATE_MISSING = "missing"
STATE_VALID = "valid"
STATE_UNREADABLE = "unreadable"

#: The size a state file may have before it is not read at all. The file is
#: untrusted input: it is written by other programs and by other versions of
#: this one, and a listing stays a listing.
_MAX_STATE_BYTES = 1_048_576


def _home() -> Path | None:
    """The user's home directory, or None when there is none.

    The paths below are worked out when the reader is built and not when
    this module is imported: a program that changes ``HOME`` -- a test, a
    sandbox -- must see the directory it changed, not the one the import
    happened to see.
    """
    try:
        return Path.home()
    except (RuntimeError, OSError):
        return None


def _default_path(name: str) -> Path | None:
    home = _home()
    if home is None:
        return None
    return home / ".cache" / "conductor" / "native-cli" / name


#: The name of the session manager's own state file.
STATE_FILE_NAME = "worker-sessions.json"

#: The name of the legacy flat map of session ids, next to the state file.
LEGACY_FILE_NAME = "sessions.json"


def read_json_file(path: Path | str, max_bytes: int) -> dict[str, Any] | None:
    """Read one JSON object from a file, defensively.

    The file is opened read-only without following a symlink and is size
    capped. A file that cannot be used is ``None`` rather than an error: a
    status command with no file still has something to say.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(Path(path), flags)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            return None
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65536, max_bytes + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                return None
            chunks.append(chunk)
        raw = b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_state_file(path: Path, max_bytes: int) -> tuple[str, dict[str, Any] | None]:
    """The state file's content, and which of the three states it is in.

    The file is opened read-only **without following a symlink** and is size
    capped, the way every other read of a file outside the program is. A
    file that is too big is not read: a status command does not need to
    parse an unbounded file to say what it knows.

    The three outcomes are kept apart, because the command says different
    things about a worker for each of them:

    * **missing** -- the file is not there. There is nothing to lose, and a
      session that lives only in the legacy map has used no context.
    * **valid** -- the content was read.
    * **unreadable** -- the file is there and cannot be used. The command
      then knows the sessions of the legacy map and **nothing else**: it
      says "not known" instead of inventing counters.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return STATE_MISSING, None
    except OSError:
        return STATE_UNREADABLE, None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            return STATE_UNREADABLE, None
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65536, max_bytes + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                return STATE_UNREADABLE, None
            chunks.append(chunk)
        raw = b"".join(chunks)
    except OSError:
        return STATE_UNREADABLE, None
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
        return STATE_UNREADABLE, None
    if not isinstance(payload, dict):
        return STATE_UNREADABLE, None
    lines = payload.get("lines")
    workers = payload.get("workers")
    return STATE_VALID, {
        "lines": lines if isinstance(lines, dict) else {},
        "workers": workers if isinstance(workers, dict) else {},
    }


@dataclass(frozen=True)
class SessionState:
    """What the state file says about one worker session.

    The fields are the ones of the session manager's own record: this is the
    same answer, read from the same file, without the manager.
    """

    session_id: str
    created_at: str
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    last_failure_class: str | None = None
    rotations: int = 0
    last_failure_at: str | None = None
    last_failure_seq: int = 0
    turns_at_failure: int = 0
    success_seq: int = 0
    quota_failed_at_seq: int | None = None
    pending_rotation: bool = False


class WorkerStateReader:
    """The session state on disk, read without writing anything.

    The reader is the CLI's own stand-in for the session manager. It reads
    the state file once and answers from that read, so a listing is a
    listing of one moment and not of several.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        legacy_path: Path | str | None = None,
    ) -> None:
        """Build a reader.

        ``path`` is the state file; the default is the one the session
        manager writes. ``legacy_path`` is the flat map of session ids that
        a session started before the manager existed still lives in.

        Both default to the file under the user's home, and **not** to the
        directory of the other one: that is where the session manager keeps
        them, and a reader that looked for the legacy map next to a custom
        state file would report a different set of sessions than the manager
        does.
        """
        self._path = Path(path) if path else _default_path(STATE_FILE_NAME)
        self._legacy_path = (
            Path(legacy_path) if legacy_path else _default_path(LEGACY_FILE_NAME)
        )
        self._disk: dict[str, Any] | None = None
        self._legacy: dict[str, str] | None = None
        self._outcome = STATE_MISSING

    @property
    def path(self) -> Path | None:
        """The state file this reader reads."""
        return self._path

    @property
    def legacy_path(self) -> Path | None:
        """The legacy flat session map this reader reads."""
        return self._legacy_path

    # ------------------------------------------------------------------
    # reading

    def _read_disk(self) -> dict[str, Any]:
        """The whole state file as it is on the disk right now.

        The three outcomes are kept apart: a file that is not there is an
        empty state, and a file that is there and cannot be read is **not**
        an empty state -- it is a state nobody knows, and the command says
        so instead of inventing counters.
        """
        if self._disk is not None:
            return self._disk
        if self._path is None:
            self._outcome = STATE_MISSING
            self._disk = {"lines": {}, "workers": {}}
            return self._disk
        outcome, disk = _read_state_file(self._path, _MAX_STATE_BYTES)
        self._outcome = outcome
        self._disk = disk if disk is not None else {"lines": {}, "workers": {}}
        return self._disk

    @property
    def state_outcome(self) -> str:
        """Which of the three states the state file is in.

        One of :data:`STATE_MISSING`, :data:`STATE_VALID` or
        :data:`STATE_UNREADABLE`. The reader answers "not known" about a
        worker when the state file is unreadable, and this says why.
        """
        self._read_disk()
        return self._outcome

    def _lines(self) -> dict[str, Any]:
        """The lines the state file holds."""
        return self._read_disk()["lines"]

    def _legacy_map(self) -> dict[str, str]:
        """The legacy flat map of session ids, read once.

        A file that is not there is an empty map. A file that is there and
        cannot be read is an empty map as well: the sessions it holds are
        not known, and the command says "not known" about them rather than
        pretending they do not exist.
        """
        if self._legacy is not None:
            return self._legacy
        legacy: dict[str, str] = {}
        if self._legacy_path is not None:
            payload = read_json_file(self._legacy_path, _MAX_STATE_BYTES)
            if payload is not None:
                legacy = {
                    str(key): str(value)
                    for key, value in payload.items()
                    if isinstance(key, str) and isinstance(value, str) and value
                }
        self._legacy = legacy
        return legacy

    def legacy_keys(self) -> tuple[str, ...]:
        """The keys the legacy flat session map knows about.

        A line that was started before the manager existed lives only in the
        legacy map, so a reader that walks only the state file never sees it.

        A map that cannot be read answers with nothing, the way the session
        manager's own store answers when it cannot list itself.
        """
        try:
            return tuple(self._legacy_map())
        except Exception:
            return ()

    def _worker_name(self, key: str, line: Any) -> str:
        """The worker a line belongs to.

        The line says which provider it is when it has one written down,
        and the key names it otherwise. The name is then put through the one
        identity a worker has, so two spellings of one provider are counted
        as one worker.
        """
        declared = line.get("provider") if isinstance(line, dict) else None
        if isinstance(declared, str) and declared.strip():
            return canonical_worker_id(declared)
        return canonical_worker_id(key.rsplit(":", 1)[-1].strip().lower())

    def _provider_from_key(self, key: str) -> str:
        """The provider a session key names.

        Conductor's own session keys end with the provider
        (``thread:<thread>:<provider>``), so the last segment names the CLI.
        """
        return key.rsplit(":", 1)[-1].strip().lower()

    def _worker_records(self, disk: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """The worker-level records, with the ones an older file lacks derived.

        The counters that belong to a worker -- the success counter and the
        generation its quota failure was recorded at -- are stored in the
        worker's own record. A file written before those records existed
        keeps the counters on its lines, and they are read as the worker's.

        The records are keyed by the **canonical** worker id. A file written
        before the identity was normalised may hold a record for ``step`` and
        one for ``stepfun``: those are two spellings of one provider, so
        their counters add up and the newer quota marker wins.

        The migration is fail open: the successes of every line with the
        same literal spelling add up, the order of the lines does not
        matter, and no quota state is judged from records that carry no
        comparable ordering -- an old quota marker is dropped, so a migrated
        worker reads as not exhausted.
        """
        records = merge_worker_records(
            {
                str(name): record
                for name, record in disk.get("workers", {}).items()
                if isinstance(record, dict)
            }
        )
        for record in records.values():
            # A worker that never ran out of quota has no marker. The merge
            # gives every record one, so the null is dropped again: the file
            # says "no marker" by leaving the field out.
            if record.get("quota_failed_at_seq") is None:
                record.pop("quota_failed_at_seq", None)

        successes: dict[str, int] = {}
        for key, line in disk.get("lines", {}).items():
            if not isinstance(line, dict):
                continue
            legacy_success = int(line.get("success_seq") or 0)
            if not legacy_success and line.get("quota_failed_at_seq") is None:
                # Nothing of an older file on this line: no record is made
                # for the worker, so the file does not grow empty records.
                continue
            spelling = str(
                line.get("provider") or self._provider_from_key(key) or ""
            ).strip().lower()
            successes[spelling] = successes.get(spelling, 0) + legacy_success

        legacy_records = merge_worker_records(
            {name: {"success_seq": total} for name, total in successes.items()}
        )
        for worker, record in legacy_records.items():
            target = records.setdefault(worker, {})
            target["success_seq"] = int(target.get("success_seq") or 0) + int(
                record.get("success_seq") or 0
            )
        return records

    def current(self, key: str) -> SessionState | None:
        """The line's current session, or None when the line has no session.

        A line the state file has no record of is answered from the legacy
        session map, so a session that was created before the manager
        existed is still visible. Such a session has no recorded age, which
        is said with an empty ``created_at`` rather than with a made-up
        timestamp.

        A state file that could not be read answers with nothing at all:
        there is no record to report, and a record with empty counters
        would say "this worker used no context and never failed" about a
        file nobody managed to read.
        """
        disk = self._read_disk()
        if self._outcome == STATE_UNREADABLE:
            return None
        line = disk["lines"].get(key)
        if not isinstance(line, dict):
            line = {}
        session_id = line.get("session_id") or self._legacy_map().get(key)
        if not session_id:
            return None
        # The two counters belong to the worker, so they are read from the
        # worker's own record -- not from the line.
        worker = self._worker_records(disk).get(self._worker_name(key, line)) or {}
        return SessionState(
            session_id=str(session_id),
            created_at=str(line.get("created_at") or ""),
            turns=int(line.get("turns") or 0),
            input_tokens=int(line.get("input_tokens") or 0),
            output_tokens=int(line.get("output_tokens") or 0),
            last_failure_class=line.get("last_failure_class"),
            rotations=int(line.get("rotations") or 0),
            last_failure_at=(
                str(line["last_failure_at"]) if line.get("last_failure_at") else None
            ),
            last_failure_seq=int(line.get("last_failure_seq") or 0),
            turns_at_failure=int(line.get("turns_at_failure") or 0),
            success_seq=int(worker.get("success_seq") or 0),
            quota_failed_at_seq=(
                int(worker["quota_failed_at_seq"])
                if worker.get("quota_failed_at_seq") is not None
                else None
            ),
            pending_rotation=isinstance(line.get("rotation_pending"), dict),
        )

    def usage_ratio(self, key: str) -> float | None:
        """Share of the provider's window this session has used, or None.

        None means "unknown": the provider has no configured window, so there
        is nothing to be a share of -- or the state file could not be read,
        so there is nothing to be a share **of**. The ratio counts input
        *and* output tokens: both are context the provider has to hold for
        the next turn.
        """
        disk = self._read_disk()
        if self._outcome == STATE_UNREADABLE:
            return None
        window = self._window(key)
        if not window:
            return None
        line = disk["lines"].get(key)
        if not isinstance(line, dict):
            line = {}
        input_tokens = int(line.get("input_tokens") or 0)
        output_tokens = int(line.get("output_tokens") or 0)
        return (input_tokens + output_tokens) / window

    def _window(self, key: str) -> int | None:
        """The context window of the provider this line runs on."""
        line = self._lines().get(key)
        provider = line.get("provider") if isinstance(line, dict) else None
        if not provider:
            provider = self._provider_from_key(key)
        return context_window_tokens(str(provider)) if provider else None
