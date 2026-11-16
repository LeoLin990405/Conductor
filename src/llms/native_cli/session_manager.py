"""Worker session bookkeeping: when a CLI session must be replaced, and what the next one needs.

A native agent CLI keeps one conversation per session id, and that conversation
grows with every turn. When the provider's context window fills up the session
stops being useful: the CLI answers with a context error, or with an empty
conversation that lost the thread. This module owns the decision to replace such
a session *before* it dies, and the handover note the replacement session starts
from.

Two properties make the handover safe to put on disk:

* its system section is generated here, never by a model. A deterministic
  summary of paths and git facts cannot hallucinate, and it cannot leak the
  prompt or a credential, because it never holds either. There is deliberately
  no parameter anywhere in this module that could carry prompt text.
* the old session id is archived, never deleted. A rotation must not lose the
  pointer to work that already happened.

Before a proactive rotation the old session may add its own account of the
work. That arrives here already validated against a fixed schema and already
sanitized (see ``worker_report``), is appended after the system section under
the ``worker_reported`` label, and is never written to the state file.

The manager keeps its own richer state in its own file
(``~/.cache/conductor/native-cli/worker-sessions.json``) and reads the current
session id of a line through the existing ``NativeCliSessionStore``, whose flat
``{key: session_id}`` map stays exactly as it is: an installation that already
has a ``sessions.json`` keeps working, and the file is never rewritten in
another shape.
"""

from __future__ import annotations

import json
import logging
import os
import re
import hashlib
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from src.llms.native_cli.base import NativeCliUsage
from src.llms.native_cli.failure import redact_credentials
from src.llms.native_cli.identity import canonical_worker_id, merge_worker_records
from src.llms.native_cli.registry import context_window_tokens
from src.llms.native_cli.sessions import NativeCliSessionStore, SessionStoreUnavailable
from src.llms.native_cli.worker_report import WorkerReport, worker_report_lines

logger = logging.getLogger(__name__)

try:  # pragma: no cover - POSIX only, and this project is POSIX
    import fcntl
except ImportError:  # pragma: no cover - a platform without flock
    fcntl = None  # type: ignore[assignment]


DEFAULT_STATE_PATH = (
    Path.home() / ".cache" / "conductor" / "native-cli" / "worker-sessions.json"
)
DEFAULT_HANDOFF_DIR = Path.home() / ".cache" / "conductor" / "native-cli" / "handoffs"

#: Fraction of the configured context window at which a line is rotated. The
#: margin is what the PRD asks for: rotate *before* the next turn starts, so the
#: turn that would have overflowed the window runs on a fresh session instead.
ROTATION_THRESHOLD = 0.8

#: How many times one line may rotate before it is parked. The third rotation
#: is the last one; the attempt after it parks the line.
MAX_ROTATIONS = 3

#: The reason code a parked line carries.
ROTATION_EXHAUSTED_REASON = "context_rotation_exhausted"

#: The failure class that asks for a rotation right away.
CONTEXT_EXHAUSTED = "context_exhausted"

#: The failure class that says the provider's quota is used up.
QUOTA_EXHAUSTED = "quota_exhausted"

# The reason a rotation happened. Kept short and stable: it names a class or a
# trigger, never a diagnostic.
ACTIVE_ROTATION = "active_rotation"

#: The worker itself asked for a fresh session (N29): its turn ended with a
#: valid handoff request. Counted against ``MAX_ROTATIONS`` like any other.
WORKER_REQUESTED = "worker_requested"

#: The sources a stored context estimate may name (mirrors
#: ``context_budget.ESTIMATE_SOURCES``; this layer does not import tools).
_ESTIMATE_SOURCES = frozenset({"occupancy", "reported", "log_bytes", "launches"})

#: What became of the self-report a rotation asked for. Codes only: the state
#: file never holds what the worker said.
SELF_REPORT_STATUSES = frozenset(
    {
        "ok",  # the report matched the schema and is in the note
        "timeout",  # the old session did not answer within the bound
        "failed",  # the turn ended with an error
        "invalid",  # the answer was not one report that matched the schema
        "oversize",  # the answer was over the size bound
        "unsupported",  # the provider cannot resume the old session
        "not_asked",  # a passive rotation: the old session cannot answer
        "dropped",  # the rotation was interrupted before the note was written
    }
)

_STATE_VERSION = 1


@dataclass(frozen=True)
class SessionRecord:
    """What the manager knows about one worker session."""

    session_id: str
    created_at: str
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    last_failure_class: str | None = None
    rotations: int = 0
    # When the last failure happened, and in which order it happened. The
    # session's own age cannot say: a failure on an older session can happen
    # after a failure on a newer one, and the latest failure is the one a
    # caller needs to see.
    last_failure_at: str | None = None
    last_failure_seq: int = 0
    # How many turns the session had when it failed. A turn after that is a
    # turn that worked, which is what says a quota failure is over.
    turns_at_failure: int = 0
    # How many turns the **worker** has completed, counted across its lines,
    # its sessions and its rotations. The count only goes up: a rotation does
    # not reset it, and neither does a new session. It says how much the
    # worker has achieved, not how much the session in front of us has.
    success_seq: int = 0
    # The success count at the moment the quota ran out. A worker is only
    # out of quota while it has not managed a turn since then, so one good
    # turn after the failure is what makes the worker a recovered one again.
    quota_failed_at_seq: int | None = None
    # Whether a rotation of this line was started and never finished. The
    # intent is on the disk, and the work it describes is still to be done.
    pending_rotation: bool = False


@dataclass(frozen=True)
class SessionLineage:
    """The Conductor-minted ids of a line's session generations (N29).

    ``first`` never changes, ``previous`` is the generation before
    ``current`` (None for the first one), and ``generation`` starts at 1. The
    ids are ``uuid4().hex`` and have nothing to do with the provider's own
    session id, which many providers do not have.
    """

    first: str
    previous: str | None
    current: str
    generation: int


def _lineage_from(data: Any, prefix: str = "lineage_") -> SessionLineage | None:
    """A lineage read from a line (or an intent), or None when absent or malformed."""
    if not isinstance(data, Mapping):
        return None
    first = data.get(f"{prefix}first")
    current = data.get(f"{prefix}current")
    previous = data.get(f"{prefix}previous")
    generation = data.get("session_generation")
    if not isinstance(first, str) or not first:
        return None
    if not isinstance(current, str) or not current:
        return None
    if previous is not None and (not isinstance(previous, str) or not previous):
        return None
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        return None
    return SessionLineage(
        first=first, previous=previous, current=current, generation=generation
    )


def _lineage_fields(lineage: SessionLineage) -> dict[str, Any]:
    return {
        "lineage_first": lineage.first,
        "lineage_previous": lineage.previous,
        "lineage_current": lineage.current,
        "session_generation": lineage.generation,
    }


def _estimate_fields(estimate: Any) -> dict[str, Any] | None:
    """The stored form of a context estimate, or None when it is malformed.

    Accepts a ``ContextEstimate``-like object or a Mapping (its ``to_dict()``)
    duck-typed, so this module does not depend on the tools layer. Never
    raises.
    """
    try:
        if isinstance(estimate, Mapping):
            get = estimate.get
        else:
            def get(name: str, default: Any = None) -> Any:
                return getattr(estimate, name, default)

        used = get("used_tokens")
        window = get("window_tokens")
        assumed = get("window_assumed")
        source = get("source")
        drives = get("drives_rotation")
        for value in (used, window):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
        if not isinstance(assumed, bool) or not isinstance(drives, bool):
            return None
        if not isinstance(source, str) or source not in _ESTIMATE_SOURCES:
            return None
        return {
            "used_tokens": int(used),
            "window_tokens": int(window),
            "window_assumed": assumed,
            "source": source,
            "drives_rotation": drives,
        }
    except Exception:
        return None


@dataclass(frozen=True)
class RotationResult:
    """What a rotation attempt did."""

    rotated: bool
    session_id: str | None
    handoff_path: Path | None
    reason_code: str | None
    parked: bool = False


# The credential shapes themselves -- a key that names a secret, an
# ``Authorization:`` header, a ``Bearer`` value -- are handled by the one
# function the whole project shares, ``redact_credentials``. This module keeps
# no second set of those rules: a handover note and a failure signature must
# never disagree about what a credential is.
#
# There is deliberately no second "unnamed secret" rule here either. A rule
# that takes every long run of characters would take the long segments of a
# path with it -- a macOS temporary directory is a long run of letters and
# digits -- and the note is supposed to carry the task file and the report
# path. The one shape that needs a rule of its own, an AWS access key id, has
# a fixed form, so it gets a fixed rule in ``failure.py`` instead.


_CREDENTIAL_KEY_RE = re.compile(
    r"(api[ _-]?key|token|secret|password|passwd|authorization|auth[ _-]?code"
    r"|bearer|cookie|session[ _-]?key|account[ _-]?id|access[ _-]?key"
    r"|private[ _-]?key|client[ _-]?secret|credential)",
    re.IGNORECASE,
)


def _slug(key: str) -> str:
    """A file-name-safe form of a session key, unique per key.

    The readable part alone is not enough: two different keys can reduce to
    the same text once the punctuation is folded away -- "thread:a:b:kimi" and
    "thread:a-b:kimi" both become "thread-a-b-kimi" -- and they would then
    write over each other's handover notes. A short hash of the untouched
    key is appended, so the name still reads well and still tells the two
    apart.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", key).strip("-")
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    return f"{slug or 'session'}-{digest}"


class WorkerSessionManager:
    """Decides when a worker session is replaced, and hands the next one its context.

    The manager is told about a line's turns and failures, and answers three
    questions for the caller: is this line due for a rotation, what does the
    current session look like, and what should the next session be told.

    The provider's context window comes from the registry
    (``context_window_tokens``). A provider without a configured window only
    rotates passively, when a failure is classified as ``context_exhausted``.
    """

    def __init__(
        self,
        *,
        path: str | Path | None = None,
        store: NativeCliSessionStore | None = None,
        clock: Callable[[], float] | None = None,
        provider: str | None = None,
        worktree: str | Path | None = None,
        baseline: str | None = None,
        task_file: str | Path | None = None,
        report: str | Path | None = None,
        handoff_dir: str | Path | None = None,
        read_only: bool = False,
    ) -> None:
        """Build a manager.

        ``path`` is the manager's own state file, ``store`` the existing flat
        ``{key: session_id}`` map whose current session ids are read (and whose
        entry is dropped when a line rotates, so the next turn starts a fresh
        provider session), and ``clock`` the source of ``created_at``.

        The remaining arguments describe the work the handover reports on:
        ``provider`` (when the session key does not name it), ``worktree`` (the
        git checkout the worker works in), ``baseline`` (the commit the line
        started from), ``task_file``, ``report`` and ``handoff_dir``.

        ``read_only`` makes a manager that only reads. A reader does not
        finish an interrupted rotation when it opens the file, and it does
        not write: a status command that reports on the workers must not
        change what it reports on. The mutable path -- the one that finishes
        the pending work -- is what the runner uses.
        """
        self._path = Path(path) if path else DEFAULT_STATE_PATH
        self._store = store if store is not None else NativeCliSessionStore()
        self._clock = clock if clock is not None else time.time
        self._provider = provider
        self._worktree = Path(worktree) if worktree else Path.cwd()
        self._baseline = baseline
        self._task_file = str(task_file) if task_file else ""
        self._report = str(report) if report else ""
        self._handoff_dir = Path(handoff_dir) if handoff_dir else DEFAULT_HANDOFF_DIR
        # Whether this manager may write. A read-only manager reports on the
        # workers without changing them: it does not finish a pending
        # rotation when it opens the file, and it refuses a write.
        self._read_only = bool(read_only)
        self._state: dict[str, Any] | None = None
        # The worker-level records: the counters that belong to a provider
        # rather than to one of its lines (see _worker_records).
        self._worker_state: dict[str, dict[str, Any]] = {}
        # The worker records this manager changed since it last wrote the
        # file. Only these are written back over it, so the count of a
        # worker another manager saved in the meantime is not undone.
        self._pending_workers: dict[str, dict[str, Any]] = {}
        # The lines this manager changed since it last wrote the file. Only
        # these are written back over the file, so a line another manager
        # saved in the meantime is not erased by this manager's write.
        self._dirty: set[str] = set()
        # The lines this manager has rebuilt from the file's latest contents
        # and is about to write. A write is a delta on top of what is on the
        # disk, never a stale snapshot put over it.
        self._pending: dict[str, dict[str, Any]] = {}
        # The content the two files were read at when the current change
        # began, and the content they will have when it is written. The two
        # files are read ONCE, before anything is written, and the content
        # each will have is worked out from that read. A file that turns
        # unreadable after the read cannot stop the change half way, because
        # nothing is read again between the two writes.
        self._disk: dict[str, Any] | None = None
        self._legacy: dict[str, str] | None = None
        # How deep this manager is inside its own state file's lock. The lock
        # is re-entrant, because a write holds it while it calls another
        # write of the same manager.
        self._lock_depth = 0
        # This manager's own count of the failures it has recorded. It breaks
        # a tie between two failures at the same instant.
        self._failure_seq = 0
        # A rotation that was interrupted left its intent on the disk. The
        # work is finished before this manager does anything else -- unless
        # this manager only reads, because a reader that changes the state
        # it reports on is not a reader.
        if not self._read_only:
            self._roll_forward_all()

    @property
    def path(self) -> Path:
        """The manager's own state file."""
        return self._path

    @property
    def store(self) -> NativeCliSessionStore:
        """The legacy flat session map the manager reads and rotates."""
        return self._store

    @property
    def handoff_dir(self) -> Path:
        """Where the handover notes are written."""
        return self._handoff_dir

    # ------------------------------------------------------------------
    # reading

    def current(self, key: str) -> SessionRecord | None:
        """The line's current session, or None when the line has no session.

        A line the manager has no record of is answered from the legacy session
        store, so a session that was created before the manager existed is still
        visible. Such a session has no recorded age, which is said with an empty
        ``created_at`` rather than with a made-up timestamp.
        """
        return self._record(key, self._load().get(key))

    def usage_ratio(self, key: str) -> float | None:
        """Share of the provider's window this session has used, or None.

        None means "unknown": the provider has no configured window, so there is
        nothing to be a share of. The ratio counts input *and* output tokens:
        both are context the provider has to hold for the next turn. The cache
        counters are left out on purpose — they are the provider's own
        accounting of a prefix it may serve again, and they differ per vendor,
        so adding them would make the ratio incomparable between providers.
        """
        window = self._window(key)
        if not window:
            return None
        input_tokens, output_tokens = self._counters(key)
        return (input_tokens + output_tokens) / window

    def should_rotate(self, key: str) -> bool:
        """Whether the line should be rotated before its next turn.

        Two triggers: the session used up its share of the provider's window
        (active rotation), or a failure was classified as ``context_exhausted``
        (passive rotation). A parked line never rotates again, and a provider
        without a configured window never rotates on usage alone.
        """
        line = self._load().get(key) or {}
        if line.get("parked"):
            return False
        if line.get("context_exhausted"):
            return True
        return self._usage_due(key)

    def proactive_rotation_due(self, key: str) -> bool:
        """Whether the line is due for an active rotation before its next turn.

        Only the usage trigger counts. A ``context_exhausted`` line is the
        passive case: its session has already failed and cannot be asked for
        a self-report, so it is rotated by the failure path instead.
        """
        line = self._load().get(key) or {}
        if line.get("parked") or line.get("context_exhausted"):
            return False
        return self._usage_due(key)

    def _usage_due(self, key: str) -> bool:
        """The usage trigger: a driving estimate when stored, else the legacy ratio.

        N29: a stored estimate with ``drives_rotation`` true and a positive
        window decides on its own; any other line keeps the reported-usage
        path exactly as before.
        """
        estimate = self.context_estimate(key)
        if (
            estimate is not None
            and estimate.get("drives_rotation")
            and int(estimate.get("window_tokens") or 0) > 0
        ):
            return (
                int(estimate["used_tokens"]) / int(estimate["window_tokens"])
                >= ROTATION_THRESHOLD
            )
        return self._window_used_up(key)

    def context_estimate(self, key: str) -> dict[str, Any] | None:
        """The line's last stored context estimate (a copy), or None."""
        line = self._load().get(key) or {}
        stored = _estimate_fields(line.get("context_estimate"))
        return dict(stored) if stored is not None else None

    def lineage(self, key: str) -> SessionLineage | None:
        """The line's lineage ids, or None when the line has none yet. Read only."""
        return _lineage_from(self._load().get(key) or {})

    def _window_used_up(self, key: str) -> bool:
        window = self._window(key)
        if not window:
            return False
        ratio = self.usage_ratio(key)
        return ratio is not None and ratio >= ROTATION_THRESHOLD

    def handoff_path(self, key: str) -> Path | None:
        """The handover note of the line's current session, if it has one."""
        line = self._load().get(key) or {}
        path = line.get("handoff_path")
        return Path(str(path)) if path else None

    def archived_session_ids(self, key: str) -> tuple[str, ...]:
        """The session ids this line rotated away from, oldest first.

        A rotation archives the old id: it is kept so nothing that happened in
        that session becomes unreachable, and it is never deleted.
        """
        line = self._load().get(key) or {}
        archived = line.get("archived")
        if not isinstance(archived, list):
            return ()
        return tuple(
            str(entry.get("session_id"))
            for entry in archived
            if isinstance(entry, dict) and entry.get("session_id")
        )

    def legacy_keys(self) -> tuple[str, ...]:
        """The keys the legacy flat session map knows about.

        A line that was started before this manager existed lives only in the
        legacy map. The map has no way to list itself, so the manager asks it:
        without this, a caller that enumerates the lines it knows about would
        never see a session that predates the manager, even though
        :meth:`current` can answer for it.
        """
        try:
            keys = self._store.keys()
        except Exception:  # pragma: no cover - a store that cannot answer
            return ()
        return tuple(str(key) for key in keys)

    # ------------------------------------------------------------------
    # writing

    def _refuse_write(self) -> None:
        """Refuse a write on a manager that only reads.

        A read-only manager exists so that a status command can report on
        the workers without changing them. The refusal is loud: a caller that
        wants to change the state has to build a manager that may.
        """
        if self._read_only:
            raise PermissionError(
                "this session manager is read-only: it does not change the state"
            )

    def record_turn(
        self,
        key: str,
        *,
        session_id: str | None = None,
        usage: NativeCliUsage | None = None,
        estimate: Any = None,
        worker_requested: bool = False,
    ) -> SessionRecord:
        """Record one completed turn of the line's session.

        The turn is added to what is on the disk, not to what this manager
        happens to remember: two managers that work on the same line must not
        undo each other's turns.

        ``estimate`` (N29) is a ``ContextEstimate``-like object or Mapping; it
        is stored as ``context_estimate`` when well formed and ignored
        otherwise. Reported ``usage`` accumulates exactly as before either
        way. ``worker_requested`` says the turn ended in a worker-requested
        handoff: such a turn does not prove the new session works, so it does
        not reset the consecutive rotation count.
        """
        self._refuse_write()
        with self._change():
            self._roll_forward(key)
            line = self._disk_line(key)
            legacy_map = dict(self._legacy or {})
            if session_id:
                line["session_id"] = str(session_id)
                # The legacy map is the one the runner reads, so it must not
                # keep pointing at a session this manager has replaced. The
                # map that will be written is worked out here and written
                # after the rich state, so the two files cannot end up out
                # of step: a lost legacy write would make the runner resume
                # a session that has already been used, while a lost state
                # write only miscounts a turn.
                legacy_map[key] = str(session_id)
                # A rotation archives the old session before the provider has
                # minted the replacement, so that event is still pending. This
                # turn is what names the new session, and the event is updated
                # in the same commit as the turn.
                self._complete_pending_archive(line, str(session_id))
            elif not line.get("session_id"):
                legacy = legacy_map.get(key)
                if legacy:
                    line["session_id"] = legacy
            if not line.get("session_id"):
                # A turn happened, so a session exists even when the caller has no
                # provider id for it yet. The provider's own id replaces this one
                # as soon as it is known.
                line["session_id"] = self._new_session_id()
            line.setdefault("created_at", self._now())
            # The rotation count is a count of CONSECUTIVE rotations: it says how
            # many times the session had to be replaced without a single turn
            # getting through. The first turn that completes in the new session
            # is proof that the new session works, so the count starts again --
            # otherwise a worker that rotated once in March and worked all April
            # would still be one rotation away from being parked.
            first_turn = int(line.get("turns") or 0) == 0
            line["turns"] = int(line.get("turns") or 0) + 1
            if (
                first_turn
                and not worker_requested
                and int(line.get("rotations") or 0) > 0
            ):
                line["rotations"] = 0
            stored_estimate = _estimate_fields(estimate) if estimate is not None else None
            if stored_estimate is not None:
                line["context_estimate"] = stored_estimate
            if usage is not None:
                line["input_tokens"] = int(line.get("input_tokens") or 0) + int(
                    usage.input_tokens
                )
                line["output_tokens"] = int(line.get("output_tokens") or 0) + int(
                    usage.output_tokens
                )
            # The success counter belongs to the worker, not to the line. A
            # worker is one provider, and a turn that gets through on one of
            # its lines says the worker is working again -- so the counter is
            # the worker's, and it lives in the worker's own record in the
            # state file. The counter only goes up, and a rotation does not
            # reset it: it says how much the worker has achieved, not how
            # much the session in front of us has.
            worker = self._worker_name(key, line)
            record = self._worker_record(key, line)
            record["success_seq"] = int(record.get("success_seq") or 0) + 1
            self._pending_workers[worker] = record
            self._drop_line_counters(line)
            if not line.get("provider"):
                line["provider"] = self._provider_from_key(key)
            # Phase two: the rich state first, then the legacy map. Both are
            # written from what was worked out in phase one, so a read that
            # fails cannot leave the two files disagreeing.
            self._commit(key, line)
            if legacy_map != (self._legacy or {}):
                self._write_legacy(legacy_map)
        return self._record(key, line)

    def record_failure(self, key: str, failure_class: str) -> SessionRecord:
        """Record the class of the failure this line just hit.

        Only the class is kept. A diagnostic may carry a token, an account id or
        an email, and the class is all a decision needs.

        The class is stored as its plain name. A caller may hand over the
        enum member -- ``FailureClass.CONTEXT_EXHAUSTED`` -- and ``str()`` of a
        string enum is its qualified name, not its value, so the name that
        reaches the disk would be "FailureClass.CONTEXT_EXHAUSTED" and every
        reader that checks the class against the vocabulary would drop it.

        A failure only touches the fields that are about failures. The turn
        counters stay as the disk has them, so a manager that held an old
        snapshot cannot undo another manager's turns.
        """
        self._refuse_write()
        with self._change():
            self._roll_forward(key)
            line = self._disk_line(key)
            resolved = str(getattr(failure_class, "value", failure_class))
            line["last_failure_class"] = resolved
            line["last_failure_at"] = self._now(microseconds=True)
            line["last_failure_seq"] = self._next_failure_seq()
            line["turns_at_failure"] = int(line.get("turns") or 0)
            if resolved == CONTEXT_EXHAUSTED:
                # The trigger is kept apart from the class, so a rotation that
                # handled the failure does not leave the line rotating forever.
                line["context_exhausted"] = True
            if resolved == QUOTA_EXHAUSTED:
                # The worker is out of quota as of the turn it had reached,
                # so a later turn says the quota came back. The number is the
                # worker's, not the line's: the worker is the thing that runs
                # out of quota, and one line of it succeeding says the whole
                # worker is working again. The other classes do not touch the
                # two counters.
                worker = self._worker_name(key, line)
                record = self._worker_record(key, line)
                record["success_seq"] = int(record.get("success_seq") or 0)
                record["quota_failed_at_seq"] = int(record.get("success_seq") or 0)
                self._pending_workers[worker] = record
                self._drop_line_counters(line)
            if not line.get("session_id"):
                # A failure happened on a session, so the line has one even when
                # the caller never named it.
                line["session_id"] = (self._legacy or {}).get(key) or self._new_session_id()
            if not line.get("provider"):
                line["provider"] = self._provider_from_key(key)
            # Phase two: the rich state. A failure never changes the legacy
            # map, so nothing is written there. It also does not settle a
            # pending rotation event: a failure is not the session that
            # replaced the old one, and only a completed turn names that
            # session.
            self._commit(key, line)
        return self._record(key, line)

    def rotate(
        self,
        key: str,
        *,
        reason: str,
        worker_report: WorkerReport | None = None,
        self_report: str = "not_asked",
        self_report_bytes: int = 0,
    ) -> RotationResult:
        """Replace the line's session, archiving the old one.

        The old session id is archived, not deleted. The handover note is
        written next to it and the caller passes that note's path in the new
        session's first message. A line that has already rotated
        ``MAX_ROTATIONS`` times is parked instead of rotated again, with the
        reason code ``context_rotation_exhausted``.

        ``worker_report`` is the old session's validated, sanitized
        self-report; it is appended to the note as the ``worker_reported``
        section. ``self_report`` and ``self_report_bytes`` say what became of
        the request, and are recorded with the archived session. The report
        itself is never written to the state file.

        The rotation is done in four steps, and what it is about to do is
        written down before the first of them:

        1. the intent is recorded on the disk;
        2. the old session is archived and the handover note is written;
        3. the two stores name the new session;
        4. the intent is cleared.

        Any of them may fail. The intent is what makes the failure
        recoverable: the next operation on the line finishes the work that is
        left, the same way every time.
        """
        self._refuse_write()
        with self._change():
            self._roll_forward(key)
            line = self._disk_line(key)
            legacy_map = dict(self._legacy or {})
            if line.get("parked"):
                return self._parked_result()
            rotations = int(line.get("rotations") or 0)
            if rotations >= MAX_ROTATIONS:
                line["parked"] = True
                line["parked_reason"] = ROTATION_EXHAUSTED_REASON
                self._commit(key, line)
                return self._parked_result()

            old_session_id = str(line.get("session_id") or legacy_map.get(key) or "")
            # The generation counts the rotations of this line and never goes
            # down. The rotation count starts again after the first successful
            # turn, and a handover note that reused the number would write
            # over the previous one.
            generation = int(line.get("handoff_generation") or 0) + 1
            new_session_id = self._new_session_id()
            handoff = self._handoff_path_for(key, generation)

            # Step 1: the intent, on the disk, before anything is done.
            #
            # The reason that goes on the disk is the **category code**, not
            # the caller's text: the caller may have passed anything, and the
            # state file is a file on a disk. The same code the handover note
            # carries is the one the intent carries, so a rotation that is
            # finished later writes the same thing the note says.
            reason_code = self._rotation_reason(reason)
            report_status = self._self_report_status(self_report)
            report_bytes = max(0, int(self_report_bytes or 0))
            # N29: the lineage the new session will carry is decided here and
            # written into the intent, so a roll-forward applies the same id.
            # A line without a lineage gets a generation-1 id first.
            base = _lineage_from(line) or self._fresh_lineage()
            target = SessionLineage(
                first=base.first,
                previous=base.current,
                current=uuid.uuid4().hex,
                generation=base.generation + 1,
            )
            line["handoff_generation"] = generation
            line["rotation_pending"] = {
                "key": key,
                "old_id": old_session_id,
                "new_id": new_session_id,
                "archive_path": str(handoff),
                "reason": reason_code,
                "rotations": rotations + 1,
                "self_report": report_status,
                "self_report_bytes": report_bytes,
                "lineage": {
                    "lineage_first": target.first,
                    "lineage_previous": target.previous,
                    "lineage_current": target.current,
                    "session_generation": target.generation,
                },
            }
            self._commit(key, line)

            # Step 2: the old session is kept in the line's history and the
            # handover note is written.
            self._write_handoff(
                key, line, reason=reason_code, path=handoff, worker_report=worker_report
            )
            # The line keeps the local placeholder as its session id, and the
            # intent above keeps it too, so an interrupted rotation can still
            # finish. The archive is the event, and that event must not store
            # the placeholder: the next session stays pending until a turn
            # names the provider's id.
            self._archive(
                line,
                old_session_id,
                reason=reason_code,
                new_session_id=None,
                new_session_pending=True,
                self_report=report_status,
                self_report_bytes=report_bytes,
            )
            self._tag_archive_lineage(line, old_session_id, base)
            line["handoff_path"] = str(handoff)
            self._commit(key, line)

            # Step 3: the two stores name the new session. The rich state is
            # written first and the legacy map second, both from what phase
            # one read: the map is the one the runner reads, and a lost
            # legacy write would leave it pointing at a session that has
            # just been archived.
            legacy_map.pop(key, None)
            line["session_id"] = new_session_id
            line["created_at"] = self._now()
            line["turns"] = 0
            line["input_tokens"] = 0
            line["output_tokens"] = 0
            line["rotations"] = rotations + 1
            line["context_exhausted"] = False
            line.update(_lineage_fields(target))
            line.pop("context_estimate", None)
            self._commit(key, line)
            if legacy_map != (self._legacy or {}):
                self._write_legacy(legacy_map)

            # Step 4: the intent has been carried out and is cleared.
            line.pop("rotation_pending", None)
            self._commit(key, line)
            return RotationResult(
                rotated=True,
                session_id=new_session_id,
                handoff_path=handoff,
                reason_code=None,
            )

    def clear_parked(self, key: str) -> bool:
        """Unpark a line so an operator retry or recover can rotate it again.

        Clears ``parked`` and ``parked_reason`` and starts the rotation count
        again from 0, so the next ``rotate()`` rotates instead of parking and
        ``should_rotate``/``proactive_rotation_due`` are no longer held back by
        the old flag. An interrupted rotation is finished first, like every
        other write. Returns whether anything changed: a line that is not
        parked (or that the manager has no record of) is left as it is and
        nothing is written.
        """
        self._refuse_write()
        with self._change():
            self._roll_forward(key)
            line = self._disk_line(key)
            if not line.get("parked") and "parked_reason" not in line:
                return False
            line.pop("parked", None)
            line.pop("parked_reason", None)
            line["rotations"] = 0
            self._commit(key, line)
        return True

    def is_parked(self, key: str) -> bool:
        """Whether the line is parked (read only)."""
        line = self._load().get(key) or {}
        return bool(line.get("parked"))

    def ensure_lineage(self, key: str) -> SessionLineage:
        """The line's lineage, created (generation 1) when it has none yet."""
        self._refuse_write()
        with self._change():
            self._roll_forward(key)
            line = self._disk_line(key)
            existing = _lineage_from(line)
            if existing is not None:
                return existing
            lineage = self._fresh_lineage()
            line.update(_lineage_fields(lineage))
            if not line.get("provider"):
                line["provider"] = self._provider_from_key(key)
            self._commit(key, line)
        return lineage

    # ------------------------------------------------------------------
    # internals

    @staticmethod
    def _fresh_lineage() -> SessionLineage:
        lineage_id = uuid.uuid4().hex
        return SessionLineage(
            first=lineage_id, previous=None, current=lineage_id, generation=1
        )

    @staticmethod
    def _tag_archive_lineage(
        line: dict[str, Any], old_session_id: str, lineage: SessionLineage | None
    ) -> None:
        """Record the archived session's lineage id and generation, once."""
        if lineage is None or not old_session_id:
            return
        archived = line.get("archived")
        if not isinstance(archived, list):
            return
        for index, entry in enumerate(archived):
            if isinstance(entry, dict) and entry.get("session_id") == old_session_id:
                if "lineage_current" not in entry:
                    archived[index] = {
                        **entry,
                        "lineage_current": lineage.current,
                        "session_generation": lineage.generation,
                    }
                return

    def _parked_result(self) -> RotationResult:
        return RotationResult(
            rotated=False,
            session_id=None,
            handoff_path=None,
            reason_code=ROTATION_EXHAUSTED_REASON,
            parked=True,
        )

    def _new_session_id(self) -> str:
        """A local id for the session that replaces the archived one.

        Only the provider can mint a real session id, and it does so on the next
        turn. Until then the line needs an id that is not the archived one, so
        the manager marks the generation locally; the next ``record_turn`` with
        the provider's id replaces it.
        """
        return f"rot-{uuid.uuid4().hex[:12]}"

    def _window(self, key: str) -> int | None:
        """The context window of the provider this line runs on."""
        if self._provider:
            return context_window_tokens(self._provider)
        line = self._load().get(key) or {}
        provider = line.get("provider")
        if not provider:
            provider = self._provider_from_key(key)
        return context_window_tokens(str(provider)) if provider else None

    def _provider_from_key(self, key: str) -> str:
        """The provider a session key names.

        Conductor's own session keys end with the provider
        (``thread:<thread>:<provider>``, or ``thread:<thread>:<lane>:<provider>``
        for a side lane), so the last segment names the CLI. A key that names
        nothing the registry knows resolves to no window, which is the same as
        a provider without one.
        """
        return key.rsplit(":", 1)[-1].strip().lower()

    def _worker_name(self, key: str, line: Mapping[str, Any]) -> str:
        """The worker a line belongs to.

        A worker is a provider: the lines that name the same provider are the
        lines of one worker. The line says which provider it is when it has
        one written down, and the key names it otherwise.

        The name is then put through the one identity a worker has: ``step``
        and ``stepfun`` are two spellings of one provider, and counting them
        as two workers would give one provider two sets of counters and two
        verdicts about its quota.
        """
        declared = line.get("provider")
        if isinstance(declared, str) and declared.strip():
            return canonical_worker_id(declared)
        return canonical_worker_id(self._provider_from_key(key))

    def _drop_line_counters(self, line: dict[str, Any]) -> None:
        """Take the counters off a line: they belong to the worker now.

        A file written before the worker records existed keeps the counters
        on its lines, and they are still read as the worker's -- but they
        are not written back, so the worker's own record is the one place
        they live.
        """
        line.pop("success_seq", None)
        line.pop("quota_failed_at_seq", None)

    def _worker_record(self, key: str, line: Mapping[str, Any]) -> dict[str, Any]:
        """The worker's own record: the counters that belong to the provider.

        The record is read from the disk, not from this manager's cache: a
        write has to be based on what the other writers have left there.
        Inside a change it comes from the one read the change made.
        """
        worker = self._worker_name(key, line)
        record = self._current_workers().get(worker)
        return dict(record) if isinstance(record, dict) else {}

    def _counters(self, key: str) -> tuple[int, int]:
        """The line's cumulative input and output tokens."""
        line = self._load().get(key)
        if not isinstance(line, dict):
            return (0, 0)
        return (
            int(line.get("input_tokens") or 0),
            int(line.get("output_tokens") or 0),
        )

    def _record(self, key: str, line: Any) -> SessionRecord | None:
        if not isinstance(line, dict):
            line = {}
        session_id = line.get("session_id")
        if not session_id:
            session_id = self._store.get(key)
        if not session_id:
            return None
        # The two counters belong to the worker, so they are read from the
        # worker's own record -- not from the line.
        worker = self._worker_state.get(self._worker_name(key, line)) or {}
        return SessionRecord(
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

    def _disk_line(self, key: str) -> dict[str, Any]:
        """The line as the file holds it right now.

        The manager's own cache is only good for reading. A write always
        starts from what the file holds, so a line another manager saved in
        the meantime is added to instead of being put over by this manager's
        older copy.

        The file is read the strict way: a file that is there and cannot be
        read is an error and not an empty state, so a transient read failure
        stops the write instead of rebuilding the line from nothing.

        Inside a change the line comes from the one read the change made, so
        the change works from a single view of the file and no read can
        happen between the two writes.
        """
        line = self._current_disk()["lines"].get(key)
        return dict(line) if isinstance(line, dict) else {}

    def _ensure(self, key: str) -> dict[str, Any]:
        """The line's own state, created if this is the first thing known of it."""
        line = self._disk_line(key)
        if not line.get("provider"):
            line["provider"] = self._provider_from_key(key)
        self._dirty.add(key)
        return line

    def _begin_change(self) -> None:
        """Phase one: read and validate both files before either is written.

        The rich state and the legacy map are each read once, strictly. A
        file that is there and cannot be read is an error and not an empty
        state, so this raises before anything has been written: the change
        stops with both files exactly as they were.

        Everything this change writes is then worked out from these two
        reads, and the two files are written from what was worked out --
        rich first, legacy second. Nothing is read again in between, so a
        file that goes unreadable after this point cannot leave the two
        files disagreeing.
        """
        self._disk = self._read_disk()
        self._legacy = self._store.snapshot()

    def _ensure_change(self) -> None:
        """Begin the change if it has not begun yet."""
        if self._disk is None or self._legacy is None:
            self._begin_change()

    @contextmanager
    def _change(self) -> Iterator[None]:
        """One change: read and validate both files, then write them.

        Phase one reads the rich state and the legacy map, strictly and once
        each, and nothing is written while that happens. Phase two writes
        the rich state and then the legacy map, both from what phase one
        worked out. A read that fails in phase one stops the change with
        both files exactly as they were.

        The two locks are held for the whole change, so the content that was
        read is the content that may be written: no other writer can change
        either file in between.
        """
        with self._write_lock():
            self._begin_change()
            try:
                yield
            finally:
                # Outside a change every read is a fresh one again.
                self._disk = None
                self._legacy = None

    def _current_disk(self) -> dict[str, Any]:
        """The state file as this change read it, or a fresh read.

        Inside a change the answer is the one the change read in phase one,
        so the whole change works from a single view of the file. Outside a
        change the file is read again, so a reader never sees a stale view.
        """
        if self._disk is not None:
            return self._disk
        return self._read_disk()

    def _current_workers(self) -> dict[str, dict[str, Any]]:
        """The worker records as this change read them."""
        return self._worker_records(self._current_disk())

    def _write_legacy(self, data: Mapping[str, str]) -> None:
        """Phase two for the legacy map: write the content that was worked out.

        The map is written as it was computed from the phase-one read, so
        there is no read between the two writes and no chance of the two
        files ending up out of step.
        """
        self._ensure_change()
        payload = dict(data)
        self._store.write_snapshot(payload)
        self._legacy = payload

    def _commit(self, key: str, line: dict[str, Any]) -> None:
        """Write one line back, on top of what the file holds right now."""
        self._ensure_change()
        self._pending[key] = line
        self._dirty.add(key)
        try:
            self._save()
        except BaseException:
            # A write that failed leaves nothing behind to be written later:
            # the line is rebuilt from the disk by the next operation anyway.
            # The worker records are dropped for the same reason -- a record
            # that was never written must not be written later either, or it
            # would put an old count over the new one a second manager saved
            # in the meantime. The cached view goes with them, so the retry
            # rebuilds from the disk and not from what failed.
            self._pending.pop(key, None)
            self._pending_workers.clear()
            self._state = None
            self._worker_state = {}
            raise

    def _complete_pending_archive(self, line: dict[str, Any], session_id: str) -> None:
        """Fill the newest rotation event that is still waiting for a session.

        The archive is written before the provider has minted an id, so the
        event only says that the next session is pending. The newest such
        entry is the rotation this turn belongs to. The id and the cleared
        flag are left on ``line`` for the caller to write in the same commit
        as the turn. A failure never calls this.
        """
        sid = str(session_id or "")
        # The local placeholder is not a provider session. Leave the event pending.
        if not sid or sid.startswith("rot-"):
            return
        archived = line.get("archived")
        if not isinstance(archived, list):
            return
        for index in range(len(archived) - 1, -1, -1):
            current = archived[index]
            if not isinstance(current, dict) or not current.get("new_session_pending"):
                continue
            updated = dict(current)
            updated["new_session_id"] = sid
            updated["new_session_pending"] = False
            archived[index] = updated
            return

    def _archive(
        self,
        line: dict[str, Any],
        old_session_id: str,
        *,
        reason: str,
        new_session_id: str | None = "",
        new_session_pending: bool = False,
        self_report: str = "not_asked",
        self_report_bytes: int = 0,
    ) -> None:
        """Keep the old session in the line's history, exactly once.

        What goes into the history is a **code**, never a text: the caller
        may have passed anything, and the state file is a file on a disk. The
        reason is mapped through the same whitelist the handover note uses,
        so a free text reason reads as ``unknown`` and the file says only
        that the rotation happened for a reason the program knows. The
        failure class of the turn is kept next to it, so the history says
        *what* happened without saying anything the caller said.

        The entry is also the rotation's event: what became of the
        self-report, how many bytes of it reached the note, and which
        session replaced this one. The replacement is written only when it
        is already a provider session id. A rotation that still holds the
        local ``rot-`` placeholder passes no id and
        ``new_session_pending=True``: the event records that the next
        session is not known yet, and the placeholder is never stored.
        ``record_turn`` fills the id in later. A ``rot-`` id passed here is
        treated as still pending and is not written.
        """
        if not old_session_id or str(old_session_id).startswith("rot-"):
            return
        archived = line.setdefault("archived", [])
        if not isinstance(archived, list):
            archived = []
            line["archived"] = archived
        for entry in archived:
            if isinstance(entry, dict) and entry.get("session_id") == old_session_id:
                return
        entry: dict[str, Any] = {
            "session_id": old_session_id,
            "reason": self._rotation_reason(reason),
            "rotated_at": self._now(),
            "self_report": self._self_report_status(self_report),
            "self_report_bytes": max(0, int(self_report_bytes or 0)),
        }
        successor = str(new_session_id or "")
        pending = bool(new_session_pending)
        # The local placeholder names a session the provider has not minted.
        # It belongs on the line and on the intent, never on the event.
        if successor.startswith("rot-"):
            successor = ""
            pending = True
        if pending:
            entry["new_session_pending"] = True
        elif successor:
            entry["new_session_id"] = successor
        failure_class = str(line.get("last_failure_class") or "").strip()
        if failure_class:
            entry["failure_class"] = failure_class
        archived.append(entry)

    @staticmethod
    def _self_report_status(status: str) -> str:
        """A self-report status code; anything else reads as ``unknown``."""
        text = str(status or "").strip().lower()
        return text if text in SELF_REPORT_STATUSES else "unknown"

    def _roll_forward(self, key: str) -> None:
        """Finish a rotation that was interrupted.

        A rotation writes down what it is about to do before it does it. If it
        was interrupted -- the disk was full, the process was killed -- the
        intent is still on the disk, and the next operation on the line
        finishes that work before it does anything else.

        Every step is idempotent, so finishing the work twice is the same as
        finishing it once: a session that is already archived is not archived
        again, a session that is already the new one is not changed again, the
        legacy map is only touched while it still names the old session, and
        the intent is cleared at the end.
        """
        self._ensure_change()
        line = self._disk_line(key)
        pending = line.get("rotation_pending")
        if not isinstance(pending, dict):
            return
        old_id = str(pending.get("old_id") or "")
        new_id = str(pending.get("new_id") or "")
        archive_path = str(pending.get("archive_path") or "")
        reason = str(pending.get("reason") or ACTIVE_ROTATION)
        rotations = int(pending.get("rotations") or 0)
        report_status = self._self_report_status(
            str(pending.get("self_report") or "not_asked")
        )
        report_bytes = int(pending.get("self_report_bytes") or 0)
        # N29: the lineage the intent decided on. An intent written before
        # lineage existed carries none, and the line's lineage is left alone.
        target = _lineage_from(pending.get("lineage"))
        if target is not None and target.previous is not None:
            archived_lineage: SessionLineage | None = SessionLineage(
                first=target.first,
                previous=None,
                current=target.previous,
                generation=max(1, target.generation - 1),
            )
        else:
            archived_lineage = None

        # The handover note and the archive. The self-report was never put on
        # the disk, so a note written here has the system section only.
        if archive_path:
            path = Path(archive_path)
            if not path.exists():
                self._write_handoff(key, line, reason=reason, path=path)
                if report_status == "ok":
                    report_status, report_bytes = "dropped", 0
        # The intent may still name the local placeholder so the line can be
        # finished. That placeholder is not written into the event: the
        # archive stays pending until a turn records the provider's id. A
        # successor that is already a real id is kept, as it was.
        placeholder = new_id.startswith("rot-")
        self._archive(
            line,
            old_id,
            reason=reason,
            new_session_id=None if placeholder else new_id,
            new_session_pending=placeholder,
            self_report=report_status,
            self_report_bytes=report_bytes,
        )
        self._tag_archive_lineage(line, old_id, archived_lineage)
        if archive_path:
            line["handoff_path"] = archive_path
        self._commit(key, line)

        # The two stores name the new session. The legacy map is only cleared
        # while it still names the archived session: a map that names another
        # session belongs to a turn that came after the rotation. The map
        # that will be written is worked out here and written after the rich
        # state, so the two files cannot end up out of step.
        legacy_map = dict(self._legacy or {})
        if old_id and legacy_map.get(key) == old_id:
            legacy_map.pop(key, None)
        if line.get("session_id") != new_id:
            line["session_id"] = new_id
            line["created_at"] = self._now()
            line["turns"] = 0
            line["input_tokens"] = 0
            line["output_tokens"] = 0
            line["rotations"] = rotations
            line["context_exhausted"] = False
            line.pop("context_estimate", None)
        if target is not None and line.get("lineage_current") != target.current:
            line.update(_lineage_fields(target))
            line.pop("context_estimate", None)
        self._commit(key, line)
        if legacy_map != (self._legacy or {}):
            self._write_legacy(legacy_map)

        # The intent has been carried out.
        line.pop("rotation_pending", None)
        self._commit(key, line)

    def _roll_forward_all(self) -> None:
        """Finish every rotation that was interrupted, when the manager opens.

        A process that was killed in the middle of a rotation leaves the
        intent on the disk. The manager that opens the file afterwards
        finishes the work before it does anything else, so a line is never
        left half-rotated.
        """
        try:
            lines = self._read_disk_lines()
        except OSError:  # pragma: no cover - a file that cannot be read
            return
        for key, line in lines.items():
            if not isinstance(line, dict):
                continue
            if not isinstance(line.get("rotation_pending"), dict):
                continue
            try:
                with self._change():
                    self._roll_forward(key)
            except OSError:
                # The disk is still not able to take the write. The intent
                # stays on the disk, so the next attempt tries again.
                logger.warning(
                    "worker session rotation left unfinished: key=%s",
                    key,
                )

    def _read_disk(self) -> dict[str, Any]:
        """The whole state file as it is on the disk right now.

        Only a file that is **not there** is an empty state -- there is
        nothing to lose. A file that *is* there and cannot be read is an
        **error** and not an empty state: a writer that read it as empty
        would put its own line over the file and lose every other line of
        every other worker.

        So a read error stops the write instead of eating the state, and
        the reader is told that it does not know rather than being handed
        a state that looks empty.

        The file holds two things: the **lines** (one per session key) and
        the **worker records** (the counters that belong to a provider, see
        :meth:`_worker_records`).
        """
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"lines": {}, "workers": {}}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            # A file that is there but is not readable is an error and not an
            # empty state. A file that is not valid UTF-8 is the same kind of
            # problem, and it must raise the same error as the legacy store
            # does: UnicodeDecodeError is a ValueError, so letting it escape
            # would break the promise that a caller catching OSError still
            # catches this one.
            raise SessionStoreUnavailable(
                f"could not read the worker session state at {self._path}"
            ) from exc
        if not isinstance(raw, dict):
            raise SessionStoreUnavailable(
                f"the worker session state at {self._path} is not a session state"
            )
        lines = raw.get("lines")
        workers = raw.get("workers")
        return {
            "lines": lines if isinstance(lines, dict) else {},
            "workers": workers if isinstance(workers, dict) else {},
        }

    def _read_disk_lines(self) -> dict[str, Any]:
        """The lines the state file holds right now."""
        return self._read_disk()["lines"]

    def _read_disk_workers(self) -> dict[str, Any]:
        """The worker-level records the state file holds right now."""
        return self._worker_records(self._read_disk())

    def _worker_records(self, disk: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """The worker-level records, with the ones an older file lacks derived.

        The counters that belong to a worker -- the success counter and the
        generation its quota failure was recorded at -- are stored in the
        worker's own record. A file written before those records existed
        keeps the counters on its lines, and they are read as the worker's:
        the transition costs no state.

        The records are keyed by the **canonical** worker id. A file written
        before the identity was normalised may hold a record for ``step`` and
        one for ``stepfun``: those are two spellings of one provider, so
        their counters add up and the newer quota marker wins. Without that
        the provider's history would be split in two, and the half that
        looked smaller would decide whether the worker is out of quota.
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
            # says "no marker" by leaving the field out, and it does not
            # fill up with nulls as the file is written again and again.
            if record.get("quota_failed_at_seq") is None:
                record.pop("quota_failed_at_seq", None)

        # The old file kept the counters on the lines, spelled literally.
        # The migration is fail open, and it reads nothing it cannot prove:
        # the successes of every line with the same literal spelling add up,
        # the order of the lines does not matter, and no quota state is
        # judged from records that carry no comparable ordering -- an old
        # quota marker is dropped, so a migrated worker reads as not
        # exhausted (fail open, like the headroom policy). After this one
        # pass the counters live on the worker record only.
        successes: dict[str, int] = {}
        for key, line in disk.get("lines", {}).items():
            if not isinstance(line, dict):
                continue
            legacy_success = int(line.get("success_seq") or 0)
            if not legacy_success and line.get("quota_failed_at_seq") is None:
                # Nothing of an older file on this line: no record is made
                # for the worker, so the file does not grow empty records.
                continue
            spelling = str(line.get("provider") or self._provider_from_key(key)
                           or "").strip().lower()
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

    def _load(self) -> dict[str, Any]:
        if self._state is not None:
            return self._state
        disk = self._read_disk()
        self._state = disk["lines"]
        self._worker_state = self._worker_records(disk)
        return self._state

    def _lock_path(self) -> Path:
        """The file this manager's own lock lives on.

        The lock is a file of its own next to the state, so the atomic write
        of the state never replaces it.
        """
        return self._path.parent / f".{self._path.name}.lock"

    def _same_lock_as_the_store(self) -> bool:
        """Whether this manager's lock and the store's lock are one file.

        They are one file when the manager's own state and the legacy flat
        map are the same file. Two descriptors for one file in one process
        block each other, so the manager must not take both.
        """
        try:
            return self._lock_path().resolve() == self._store.lock_path.resolve()
        except OSError:  # pragma: no cover - a path that cannot be resolved
            return self._lock_path() == self._store.lock_path

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the state file's lock while it is read and written.

        Two managers may work on the same file -- a dispatcher and a status
        command, say -- and a write that is not serialised lets one of them
        erase the other's line. The lock is a separate file next to the
        state, so it is never replaced by the atomic write of the state
        itself.

        The lock is re-entrant: a write holds it while it calls another write
        of the same manager, and the second entry must not wait for itself.
        """
        if fcntl is None:  # pragma: no cover - a platform without flock
            yield
            return
        self._lock_depth += 1
        if self._lock_depth > 1:
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        lock_path = self._lock_path()
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:  # pragma: no cover - a directory that cannot be made
            self._lock_depth -= 1
            yield
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
                self._lock_depth -= 1

    @contextmanager
    def _write_lock(self) -> Iterator[None]:
        """Hold both locks while the two stores are written together.

        The manager's own state and the legacy flat map are written as one
        thing: a reader must never see a turn that the legacy map does not
        know about yet, or a session that the legacy map points at while the
        state has already forgotten it. The legacy map's lock is the one the
        two stores share, so it is held for the whole of both writes.

        The two locks are always taken in this order -- the state file's own
        lock first, the legacy map's second -- so two managers cannot hold
        one each and wait for the other.

        When the two files are the same file, only one lock is taken. ``flock``
        belongs to the open file description, so a second descriptor for the
        same file waits for the first *inside this very process*: the manager
        would wait for itself.
        """
        if self._same_lock_as_the_store():
            with self._store.locked():
                yield
            return
        with self._locked():
            with self._store.locked():
                yield

    def _save(self) -> None:
        """Write the manager's own state, atomically and privately.

        The file is the manager's own, so it may hold more than the legacy map's
        flat ``{key: session_id}``; the legacy map is never written from here.

        The file is re-read under a lock and only the lines this manager
        changed are written over it. A manager that wrote its whole cached
        copy would erase the lines another manager saved in the meantime --
        two managers that each save one key would leave only the second key
        on disk.

        The re-read happens in the phase-one read of the change, not here:
        the content this writes is worked out from what was read before
        anything was written, and nothing is read between the rich write and
        the legacy write. A file that goes unreadable after that read
        therefore cannot leave the two files disagreeing.

        The **worker records** are merged the same way: the counters that
        belong to a provider are their own thing in the file, and a manager
        that wrote its whole cached copy of them would undo the count of
        another manager that wrote the same worker in the meantime.
        """
        with self._write_lock():
            self._ensure_change()
            disk = self._disk
            if disk is None:  # pragma: no cover - _ensure_change sets it
                disk = self._read_disk()
            merged = dict(disk["lines"])
            for key, line in self._pending.items():
                merged[key] = line
            workers = self._worker_records(disk)
            for worker, record in self._pending_workers.items():
                workers[worker] = record
            for line in merged.values():
                # The counters live in the worker's record now, so the copies
                # an older file kept on its lines are not written back.
                if isinstance(line, dict):
                    line.pop("success_seq", None)
                    line.pop("quota_failed_at_seq", None)
            data = {
                "version": _STATE_VERSION,
                "lines": merged,
                "workers": workers,
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=self._path.parent, prefix=".worker-sessions-", suffix=".json"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
                    handle.write("\n")
                os.chmod(tmp, 0o600)
                os.replace(tmp, self._path)
            finally:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
            # What is on disk is now what this manager knows, so the next
            # write merges against the same lines -- and against the same
            # read, so nothing is read again between the two writes.
            self._disk = data
            self._state = merged
            self._worker_state = workers
            self._pending.clear()
            self._pending_workers.clear()
            self._dirty.clear()

    def _now(self, *, microseconds: bool = False) -> str:
        if microseconds:
            # A failure's time has to order failures, and two failures inside
            # the same second would otherwise be indistinguishable.
            return datetime.fromtimestamp(self._clock(), tz=timezone.utc).isoformat(
                timespec="microseconds"
            )
        return datetime.fromtimestamp(self._clock(), tz=timezone.utc).isoformat(
            timespec="seconds"
        )

    def _next_failure_seq(self) -> int:
        """The next number in this manager's order of failures.

        The number is a tie-breaker between two failures that happened at the
        same instant, and it is kept per line so that it survives a restart.
        Two managers have their own counting, which is why the timestamp is
        the primary order and this number only breaks a tie.
        """
        self._failure_seq = int(self._failure_seq) + 1
        return self._failure_seq

    # ------------------------------------------------------------------
    # the handover note

    def _handoff_path_for(self, key: str, generation: int) -> Path:
        """Where the handover note of this rotation is written.

        The number is the line's generation: it only goes up, so a second
        rotation never writes over the note of the first one.
        """
        return self._handoff_dir / f"{_slug(key)}-{generation:02d}.md"

    def _write_handoff(
        self,
        key: str,
        line: dict[str, Any],
        *,
        reason: str,
        path: Path,
        worker_report: WorkerReport | None = None,
    ) -> Path:
        """Write the handover note of the session that is being replaced."""
        content = self._handoff_text(key, line, reason=reason, worker_report=worker_report)
        self._handoff_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=self._handoff_dir, prefix=".handoff-", suffix=".md"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
        return path

    def _handoff_text(
        self,
        key: str,
        line: dict[str, Any],
        *,
        reason: str,
        worker_report: WorkerReport | None = None,
    ) -> str:
        """The handover note, as a pure function of the manager's inputs.

        No timestamp and no counter goes in: two calls with the same inputs
        produce the same note, so a note can be compared, diffed and trusted.

        The system section comes first and is the same with or without a
        self-report. A ``worker_report`` adds the ``worker_reported`` section
        after it; the credential rule below covers that section too.

        **Every piece of external metadata is assembled first and cleaned
        once, at the exit.** The branch, the paths and the reason come from
        outside the program, so any of them can carry a credential -- a
        branch may be named ``feature/api_key=<fake>``. Cleaning each field
        where it is built leaves a gap: a field added later would be written
        straight to the file. Cleaning the whole text once means there is
        exactly one place where the question "is this a credential?" is
        asked, and nothing reaches the file without passing it.
        """
        branch = self._sanitize_ref(self._git("rev-parse", "--abbrev-ref", "HEAD"))
        head = self._git("rev-parse", "HEAD")
        summary = self._diff_summary(self._baseline or "")
        commits = self._commits(self._baseline or "")
        lines = [
            "# 交接摘要（Conductor 自动生成）",
            "",
            f"- 任务文件: {self._task_file or '（未配置）'}",
            f"- 分支: {branch or '（未知）'}",
            f"- HEAD: {head or '（未知）'}",
            f"- 最近一次报告: {self._report or '（无）'}",
            f"- 轮换原因: {self._rotation_reason(reason)}",
            "",
            "## 相对基线的改动",
            "",
            f"{summary or '（无改动）'}。接手前请在 worktree 里自行运行 "
            "`git diff --stat` 查看涉及的文件路径。",
            "",
            "## 已完成的提交",
            "",
            "```",
            commits or "（无提交）",
            "```",
            "",
            "## 下一步",
            "",
            "继续执行任务文件",
            "",
        ]
        if worker_report is not None:
            lines.extend(worker_report_lines(worker_report))
        # The structural whitelist is the first line of defense -- the system
        # section holds hashes, summary numbers and fixed wording only, and a
        # worker section was sanitized and checked against its schema before
        # it got here -- and the credential rule over the whole note stays as
        # the second one.
        return redact_credentials("\n".join(lines))

    def _git(self, *args: str) -> str:
        """One git answer, or nothing when git cannot answer it.

        The handover must not depend on git being installed, on the checkout
        existing, or on the answer being an error: a missing fact is written as
        a missing fact.
        """
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=str(self._worktree),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        if completed.returncode != 0:
            return ""
        return completed.stdout.strip()

    @staticmethod
    def _sanitize_ref(text: str) -> str:
        """Keep the shape of a git ref, and redact every segment that reads
        like it carries more than a name.

        A ref is the worker's own text, so it is whitelisted instead of
        blacklisted: characters outside ``[A-Za-z0-9._-]`` are dropped, and a
        ``/``-segment that carries ``=``, ``:`` or ``@`` -- or that matches a
        credential key -- is replaced whole by ``<redacted>``. The slashes
        are kept, so the ref keeps its shape; a segment that only loses
        characters keeps the ones that are left.
        """
        segments = []
        for segment in str(text or "").split("/"):
            if "=" in segment or ":" in segment or "@" in segment:
                segments.append("<redacted>")
                continue
            cleaned = "".join(
                char for char in segment if char.isalnum() or char in "._-"
            )
            if cleaned and _CREDENTIAL_KEY_RE.search(cleaned):
                segments.append("<redacted>")
            elif cleaned:
                segments.append(cleaned)
        return "/".join(segments)

    #: The category codes the note may carry as a rotation reason. Every one
    #: of them is a code this program defines, so the note says what happened
    #: and not what the caller said.
    _ROTATION_REASON_CODES = frozenset(
        {
            ROTATION_EXHAUSTED_REASON,
            CONTEXT_EXHAUSTED,
            QUOTA_EXHAUSTED,
            ACTIVE_ROTATION,
            WORKER_REQUESTED,
        }
    )

    @staticmethod
    def _rotation_reason(reason: str) -> str:
        """The rotation reason, as a fixed category code.

        Only a category code this program knows is written; anything else --
        and any free text a caller may have strung together -- is replaced by
        ``unknown``. The rule is a **whitelist**, not a shape: a shape would
        let ``quota_ghp_abc123def456`` through, because it looks exactly like
        a code, and a credential that looks like a code is still a
        credential.
        """
        text = str(reason or "").strip().lower()
        if text in WorkerSessionManager._ROTATION_REASON_CODES:
            return text
        return "unknown"

    def _diff_summary(self, baseline: str) -> str:
        """How much the line changed since the baseline, as numbers only.

        ``git diff --shortstat`` prints one line, and it prints only the
        numbers: how many files changed, how many insertions, how many
        deletions. It never prints a path, so a file whose *name* carries a
        credential cannot carry it into the note -- which is the whole point
        of the v1 narrowing. (The full ``--stat`` prints a line per file,
        and a line per file is a path: looking for the summary line in that
        output means deciding by the *name* of a file, so a file called
        ``changed.md`` would have its path taken for the summary.)

        The paths are left to the next session, which runs ``git diff
        --stat`` itself in the same worktree.
        """
        target = baseline or "HEAD"
        return self._git("diff", "--shortstat", target).strip()

    def _commits(self, baseline: str) -> str:
        """The hashes of the commits the line finished since the baseline.

        The log carries no subjects: a subject is whatever the worker wrote,
        and the handoff has no free text in it. One hash per line.
        """
        if baseline:
            output = self._git("log", "--format=%H", f"{baseline}..HEAD")
        else:
            output = self._git("log", "--format=%H", "-n", "20")
        return "\n".join(line for line in output.splitlines() if line.strip())
