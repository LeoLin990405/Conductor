"""Read-only worker health for ``conductor workers``.

PRD W8 §6 asks for one command that answers "is this worker healthy right
now": how many sessions it has, how much of its context window the current
session has used, what the last failure was, how often the session has been
rotated, and whether the provider is out of quota until when.

Four rules shape the code:

* **Read-only.** The command reads the session manager's state and the
  CodexBar snapshot that is already on disk. It never starts a Worker CLI,
  never calls a provider and never writes. Refreshing the snapshot is
  opt-in (``conductor workers --refresh``) and lives in the CLI handler,
  not here.

* **The CLI stands on its own.** This package is what the wheel ships, and
  the wheel does not ship ``src.llms``: the native-CLI adapters, the
  session manager and the failure classifier are the server's business.
  So nothing here imports ``src.*``. The few facts about a worker that
  this command needs -- which names are one worker, how large their
  context window is -- live in ``conductor_cli.worker_registry``, and the
  session state is read by ``conductor_cli.worker_state`` instead of by
  the session manager. A test in ``tests/unit/conductor_cli/`` holds the
  copies in step with the originals, and another one fails when a
  ``src.`` import appears here again.

* **No untrusted text reaches the terminal.** A provider name, a failure
  class and a reset time all come from files outside the program. A name
  is shown only when Conductor can name it, a failure class only when it
  is one of the fixed classes of PRD W8 §3, and a reset time only when it
  parses as a timestamp. What survives is printed through
  :func:`conductor_cli.terminal_text.plain`, so a control sequence in a
  file cannot reach the terminal either.

* **Injectable inputs.** The session manager and the snapshot source are
  parameters, so a test never touches the real home directory and never
  starts a real ``codexbar`` binary.

What one row means
------------------
A worker is a provider, not a session. The session manager keys its state
by Conductor's session key (``thread:<thread>:<provider>``), so one
provider can hold several sessions; the rows of this command are the
providers, and the sessions of a provider are aggregated into its one row:

* ``sessions`` is how many sessions the manager holds for that provider.
  Both of the manager's stores are counted: its own state file and the
  legacy flat map of session ids, where a session written by an older
  version of the code still lives.
* ``context_used_percent`` is the fullest of those sessions: the one that
  will be rotated first is the one the operator needs to see.
* ``last_failure_class`` is the class of the most recent failure among
  them. The order is the moment the failure happened, not the age of the
  session it happened on: a failure on an older session can come after a
  failure on a newer one. A record that carries no failure time falls
  back to the session's creation, the earliest moment its failure can
  have happened at.
* ``rotations`` is how often the provider's sessions were replaced.
* ``exhausted`` is the provider's quota state. The snapshot's reading is
  the state that counts, and a quota failure that no successful turn has
  covered since still exhausts the worker. That failure is judged by the
  generation it happened in, not by the age of the session: a rotation
  starts a new session, not a new generation, so it neither revives a
  quota failure nor clears one.

``--json`` shape
----------------
``conductor workers --json`` prints exactly this object on stdout, without
the CLI-wide envelope, so a script can read ``workers`` directly::

    {"workers": [{"provider": "kimi",
                  "sessions": 2,
                  "context_used_percent": 42.5,
                  "last_failure_class": "rate_limited",
                  "rotations": 1,
                  "exhausted": false,
                  "reset_at": "2026-11-07T00:00:00Z"}]}

Every value is JSON-serialisable. ``None`` means "not known": a provider
without a configured context window has ``"context_used_percent": null``,
never ``0``, and no value is a ``Path`` or a ``datetime``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .terminal_text import plain
from .worker_registry import canonical_worker_id, context_window_tokens
from .worker_state import STATE_UNREADABLE, WorkerStateReader, read_json_file

__all__ = [
    "FAILURE_CLASSES",
    "read_snapshot_file",
    "render_workers",
    "workers_result",
]

# A snapshot older than this is not trusted: the windows it reports describe
# a quota state that has since moved on. The window matches the one the
# ``usage`` command applies to the same file.
_MAX_AGE_SECONDS = 900
# A snapshot written in the future is a clock problem, not a reading.
_MAX_FUTURE_SKEW_SECONDS = 60
_MAX_SNAPSHOT_BYTES = 262_144
_MAX_STATE_BYTES = 1_048_576
_MAX_PROVIDER_ROWS = 100
_MAX_WINDOWS = 40
_MAX_SESSION_KEYS = 200
_MAX_KEY_LENGTH = 64

# The fixed failure vocabulary of PRD W8 §3. The classifier owns the same
# list; a class outside it is not a class Conductor can act on, so it is
# dropped rather than printed.
FAILURE_CLASSES = frozenset(
    {
        "auth_failed",
        "confirmation_required",
        "context_exhausted",
        "crash",
        "network",
        "timeout",
        "overloaded",
        "quota_exhausted",
        "rate_limited",
        "tool_blocked",
        "turn_budget_exhausted",
        "unknown",
    }
)

# The provider names Conductor knows, as the two files this command reads
# spell them: the CodexBar snapshot (``codexbar_snapshot.PROVIDERS``) and the
# native-CLI registry (``native_cli.registry``). A name outside this set is
# not a worker Conductor can name, so it is not shown: the name is untrusted
# text read from a file on disk.
_KNOWN_WORKER_KEYS = frozenset(
    {
        # CodexBar provider ids.
        "codex",
        "claude",
        "cursor",
        "doubao",
        "deepseek",
        "grok",
        "kimi",
        "manus",
        "mimo",
        "minimax",
        "stepfun",
        "zai",
        # Native-CLI adapter names.
        "arkcli",
        "claude-code",
        "deepseek-dsh",
        "glm",
        "mmx",
        "minimax-mmx",
        "step",
        "zcode",
        "zhipu",
        # Spellings the dispatch layer accepts for the same workers.
        "claudedev",
        "codexrev",
        "dsh",
        "moonshot",
        "openai",
        "openai-codex",
    }
)

# The dispatch identity of each accepted spelling. A worker has one name
# however the two files spell it: CodexBar calls GLM "zai", the registry
# calls it "glm" and "zhipu", and the status line shows one row for it.
# These are the names ``registry.context_window_tokens`` resolves, so the
# window lookup and the listing agree on what a worker is.
#
# The table itself lives in ``src/llms/native_cli/identity.py`` now: the
# session manager counts the same workers, and two tables that disagree
# about what a worker is give one provider two sets of counters and two
# verdicts about its quota. This file asks that module for the identity
# instead of keeping a second copy of the answer.

# A snapshot source: a callable that returns the snapshot payload, or None.
SnapshotSource = Callable[[], Mapping[str, Any] | None]

_HEADERS = (
    "PROVIDER",
    "SESSIONS",
    "CONTEXT",
    "LAST FAILURE",
    "ROTATIONS",
    "PENDING",
    "QUOTA",
    "RESET",
)


def _now(now: datetime | None) -> datetime:
    """The instant the reading is judged against, in UTC."""
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp, or None when the value is not one.

    A value that does not parse is unknown data, never an error: the caller
    shows less rather than failing the whole listing.
    """
    if not isinstance(value, str) or not value or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return None
    try:
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _canonical_timestamp(value: datetime) -> str:
    """The snapshot's own spelling of a UTC instant."""
    moment = value.astimezone(timezone.utc)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _used_percent(value: Any) -> float | None:
    """One window's used percentage, or None when it is not a usable number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(min(value, 100.0))


def _canonical_worker(raw: Any) -> str | None:
    """The worker id for a provider name, or None when the name is unknown.

    The name is untrusted text from a file on disk. It is first reduced to
    safe plain text (so a control sequence cannot hide a known name) and
    then matched against the names Conductor knows. A name outside that set
    names no worker Conductor can act on, and is not shown.
    """
    if not isinstance(raw, str):
        return None
    text = plain(raw).strip().lower()
    if not text or len(text) > _MAX_KEY_LENGTH:
        return None
    if text not in _KNOWN_WORKER_KEYS:
        return None
    return canonical_worker_id(text)


def read_snapshot_file(path: Path | str) -> dict[str, Any] | None:
    """Read an existing CodexBar snapshot file; never refresh it.

    The file is read the way ``usage.py`` reads the same file. A snapshot
    that cannot be used is ``None`` rather than an error: the session state
    is still worth reporting.
    """
    return read_json_file(path, _MAX_SNAPSHOT_BYTES)


def _snapshot_payload(
    snapshot: SnapshotSource | Mapping[str, Any] | None,
    snapshot_path: Path | str | None,
) -> Mapping[str, Any] | None:
    """The snapshot payload, from an injected source or from disk."""
    if snapshot is None:
        from .codexbar_snapshot import DEFAULT_OUTPUT

        path = snapshot_path if snapshot_path is not None else DEFAULT_OUTPUT
        return read_snapshot_file(path)
    if callable(snapshot):
        try:
            payload = snapshot()
        except Exception:
            return None
        return payload if isinstance(payload, Mapping) else None
    return snapshot if isinstance(snapshot, Mapping) else None


def _snapshot_rows(
    payload: Mapping[str, Any] | None,
) -> dict[str, list[dict[str, Any]]]:
    """The snapshot's provider rows, indexed by worker id."""
    rows: dict[str, list[dict[str, Any]]] = {}
    if payload is None:
        return rows
    raw_rows = payload.get("providers")
    if not isinstance(raw_rows, list):
        return rows
    for row in raw_rows[:_MAX_PROVIDER_ROWS]:
        if not isinstance(row, Mapping):
            continue
        worker = _canonical_worker(row.get("provider"))
        if worker is None:
            continue
        rows.setdefault(worker, []).append(dict(row))
    return rows


def _state_lines(manager: Any) -> dict[str, Any]:
    """The manager's own state, read from the file the manager reads.

    The manager answers for one session key at a time and exposes no
    listing of the keys it holds, so its state file is read: it is the
    file the manager itself loads, and its ``lines`` map is keyed by the
    session key. A manager without a readable state contributes no keys:
    the quota state of the snapshot is still reported.
    """
    path = getattr(manager, "path", None)
    if not isinstance(path, (str, Path)):
        return {}
    payload = read_json_file(path, _MAX_STATE_BYTES)
    lines = payload.get("lines") if isinstance(payload, dict) else None
    return lines if isinstance(lines, dict) else {}


def _manager_keys(manager: Any, lines: Mapping[str, Any]) -> list[str]:
    """The session keys the session manager holds.

    Two stores sit behind the manager: its own rich state file and the
    legacy flat map of session ids. A session written by an older version
    of the code lives only in the legacy map, and the manager can still
    answer for it -- but a reader that walks only the rich file never sees
    it. The two are therefore unioned, not chosen between: a key that is in
    both stores is one session and is counted once.

    The union is capped: a state file and a session map are untrusted
    input, and the listing stays a listing.
    """
    if manager is None:
        return []
    keys: list[str] = []
    seen: set[str] = set()

    def _collect(values: Any) -> None:
        if isinstance(values, Mapping):
            values = list(values)
        if not isinstance(values, (list, tuple, set, frozenset)):
            return
        for item in values:
            if isinstance(item, str) and item and item not in seen:
                seen.add(item)
                keys.append(item)

    _collect([key for key in lines if isinstance(key, str)])
    # A manager that lists its own keys is asked as well.
    for name in (
        "legacy_keys",
        "session_keys",
        "providers",
        "keys",
        "known_providers",
        "worker_keys",
    ):
        attribute = getattr(manager, name, None)
        if not callable(attribute):
            continue
        try:
            value = attribute()
        except Exception:
            continue
        _collect(value)
    return keys[:_MAX_SESSION_KEYS]


def _provider_of(key: str, declared: Any) -> str | None:
    """The worker a session key belongs to.

    The state file names the provider of each line. When it does not, the
    key names it: Conductor's session keys end with the provider
    (``thread:<thread>:<provider>``).
    """
    if isinstance(declared, str) and declared.strip():
        worker = _canonical_worker(declared)
        if worker is not None:
            return worker
    segment = key.rsplit(":", 1)[-1].strip().lower()
    return _canonical_worker(segment)


def _current_record(manager: Any, key: str) -> Any | None:
    """The manager's current session record for a key, or None."""
    if manager is None:
        return None
    try:
        record = manager.current(key)
    except Exception:
        return None
    return record


def _usage_ratio(manager: Any, key: str) -> float | None:
    """The current session's share of the context window, or None."""
    if manager is None:
        return None
    try:
        ratio = manager.usage_ratio(key)
    except Exception:
        return None
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
        return None
    value = float(ratio)
    return value if math.isfinite(value) else None


def _rotations(record: Any) -> int:
    """How often the session was rotated; a damaged count is zero."""
    value = getattr(record, "rotations", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _failure_class(record: Any) -> str | None:
    """The last failure's class, when it is one of the fixed classes."""
    value = getattr(record, "last_failure_class", None)
    if not isinstance(value, str):
        return None
    text = plain(value).strip().lower()
    return text if text in FAILURE_CLASSES else None


def _failure_order(record: Any) -> tuple[int, float, int]:
    """The order of one record's last failure.

    The failure's own time is the order: a failure on an older session can
    happen after a failure on a newer one, so the age of the session cannot
    say which of the two came last.

    A record that carries no failure time -- one written by an older
    version -- falls back to the session's creation, which is the earliest
    moment the failure can have happened at. A record with neither is the
    oldest of all: nothing is known about when it failed.
    """
    when = _timestamp(getattr(record, "last_failure_at", None))
    if when is None:
        when = _timestamp(getattr(record, "created_at", ""))
    seq = getattr(record, "last_failure_seq", 0)
    if isinstance(seq, bool) or not isinstance(seq, int):
        seq = 0
    if when is None:
        return (0, 0.0, seq)
    return (1, when.timestamp(), seq)


def _latest_failure(records: list[Any]) -> Any | None:
    """The record whose last failure happened most recently."""
    latest: Any | None = None
    latest_order: tuple[int, float, int] | None = None
    for record in records:
        if _failure_class(record) is None:
            continue
        order = _failure_order(record)
        if latest_order is None or order > latest_order:
            latest, latest_order = record, order
    return latest


def _last_failure(records: list[Any]) -> str | None:
    """The class of the most recent failure among a worker's sessions."""
    latest = _latest_failure(records)
    return _failure_class(latest) if latest is not None else None


def _default_context_windows(provider: str) -> int | None:
    """The context window of a provider, when it has one.

    The windows live in ``conductor_cli.worker_registry``: the wheel that
    ships this command does not ship the native-CLI registry, so the CLI
    keeps its own copy of the few numbers it needs.
    """
    try:
        value = context_window_tokens(provider)
    except Exception:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _context_tokens(
    context_windows: Callable[[str], int | None] | None,
    names: list[str],
) -> int | None:
    """The context window of a worker, looked up under either spelling.

    The registry is keyed by the provider name it knows; the snapshot and
    the session state may spell the same worker differently, so each
    spelling is tried before the window is called unknown.
    """
    lookup = (
        context_windows if context_windows is not None else _default_context_windows
    )
    for name in names:
        if not name:
            continue
        try:
            value = lookup(name)
        except Exception:
            value = None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            continue
        return value
    return None


def _context_percent(
    manager: Any,
    keys: list[str],
    names: list[str],
    context_windows: Callable[[str], int | None] | None,
) -> float | None:
    """The fullest current session's context usage, as a percentage.

    A worker without a configured context window has no percentage to
    report: the answer is unknown, never zero.
    """
    if not keys:
        return None
    if _context_tokens(context_windows, names) is None:
        return None
    ratios = [
        ratio
        for ratio in (_usage_ratio(manager, key) for key in keys)
        if ratio is not None
    ]
    if not ratios:
        return None
    return round(min(max(max(ratios), 0.0), 1.0) * 100, 1)


def _fresh(row: Mapping[str, Any], now: datetime, max_age_seconds: int) -> bool:
    """Whether a provider row is recent enough to act on."""
    observed = _timestamp(
        row.get("updated_at", row.get("updatedAt", row.get("observed_at_utc")))
    )
    if observed is None:
        return False
    age = (now - observed).total_seconds()
    return -_MAX_FUTURE_SKEW_SECONDS <= age <= max_age_seconds


def _quota_state(
    row: Mapping[str, Any] | None,
    now: datetime,
    max_age_seconds: int,
) -> tuple[bool | None, str | None]:
    """The (exhausted, reset_at) state of one snapshot row.

    ``exhausted`` is None when the row cannot be used, so the caller can
    tell "not exhausted" from "nothing is known". The reset time is the
    one of the binding window: the window with the most used percentage,
    the same window the headroom reader lets decide a worker's fate.
    """
    if row is None or row.get("status") != "ok":
        return None, None
    if not _fresh(row, now, max_age_seconds):
        return None, None
    balance = row.get("balance")
    if isinstance(balance, Mapping):
        amount = balance.get("amount")
        if isinstance(amount, (int, float)) and not isinstance(amount, bool):
            return amount <= 0, None
    windows = row.get("windows")
    if not isinstance(windows, list) or not windows:
        return None, None
    best_used: float | None = None
    best_reset: str | None = None
    for window in windows[:_MAX_WINDOWS]:
        if not isinstance(window, Mapping):
            continue
        used = _used_percent(window.get("used_percent", window.get("usedPercent")))
        if used is None:
            continue
        reset_value = window.get("resets_at_utc", window.get("resetsAt"))
        reset_at = _timestamp(reset_value)
        if reset_at is not None and reset_at <= now:
            # The window has already reset; it says nothing about now.
            continue
        if best_used is None or used > best_used:
            best_used = used
            best_reset = (
                _canonical_timestamp(reset_at) if reset_at is not None else None
            )
    if best_used is None:
        return None, None
    return best_used >= 100.0, best_reset


def _default_manager(session_path: Path | str | None) -> Any | None:
    """The session state on disk, or None when it cannot be read.

    The manager is optional: without it the command still reports the
    quota state of every worker the snapshot knows.

    The state is read by ``conductor_cli.worker_state`` and not by the
    native-CLI session manager: the wheel that ships this command does not
    ship ``src.llms``, so an import of the manager would fail in an
    installation -- and the reader that swallowed that failure would report
    a healthy machine with no sessions on it.

    The reader is **read-only**. This command only reports, and the report
    must not change what it reports on: a manager that opens the state file
    finishes a rotation that was interrupted, and a reader that did that
    would write the state, clear the pending intent and leave an archive
    behind -- all of it a side effect of a status query. The runner is the
    path that finishes the pending work.
    """
    try:
        return WorkerStateReader(path=session_path)
    except Exception:
        return None


def _pending_rotation(records: Sequence[Any]) -> bool:
    """Whether one of the worker's lines has a rotation that was not finished.

    A rotation writes down what it is about to do before it does it, so a
    rotation that was interrupted leaves its intent on the disk. The listing
    says so instead of quietly finishing it: this command only reports, and
    the work is finished by the path that uses the sessions.
    """
    for record in records:
        if record is None:
            continue
        if getattr(record, "pending_rotation", False):
            return True
    return False


def _worker_quota_blocked(records: Sequence[Any]) -> bool:
    """Whether the worker is still out of quota.

    The counters belong to the **worker**, not to one of its lines: a worker
    is one provider, and a turn that gets through on any of its lines says
    the worker is working again. So the worker's numbers are the ones its
    lines carry -- every line of a worker holds the same pair -- and the
    worker is blocked while the success counter has not passed the number
    the quota failure was recorded at.

    The failure is judged by the generation it happened in, not by the age
    of the session: a rotation starts a new session but not a new
    generation, so the count only rises and one successful turn after the
    failure says the worker is working again.

    The numbers are read the defensive way: a damaged number says nothing,
    because it cannot invent a quota failure.
    """
    success = 0
    failed_at: int | None = None
    for record in records:
        if record is None:
            continue
        value = getattr(record, "success_seq", 0)
        if not isinstance(value, bool) and isinstance(value, int):
            success = max(success, value)
        value = getattr(record, "quota_failed_at_seq", None)
        if value is None or isinstance(value, bool) or not isinstance(value, int):
            continue
        failed_at = value if failed_at is None else max(failed_at, value)
    if failed_at is None:
        return False
    return success <= failed_at


def _state_unknown(manager: Any) -> bool:
    """Whether the session state could not be read at all.

    A reader that says so answers "not known" about the workers in the
    state file, and the listing says that too: a worker whose state nobody
    could read has **not** used no context and has **not** never failed --
    nobody knows. The managers the tests inject do not have to answer.
    """
    return getattr(manager, "state_outcome", None) == STATE_UNREADABLE


def _worker_row(
    worker: str,
    *,
    keys: list[str],
    names: list[str],
    manager: Any,
    row: Mapping[str, Any] | None,
    context_windows: Callable[[str], int | None] | None,
    now: datetime,
    max_age_seconds: int,
) -> dict[str, Any]:
    """One worker's status."""
    records = [_current_record(manager, key) for key in keys]
    latest = _latest_failure([record for record in records if record is not None])
    failure = _failure_class(latest) if latest is not None else None
    exhausted, reset_at = _quota_state(row, now, max_age_seconds)
    if _worker_quota_blocked(records):
        # PRD W8 §3: a quota failure marks the worker exhausted until the
        # reset time, whether or not the snapshot has caught up with it.
        # The failure is judged by the generation it happened in: one
        # successful turn after it says the worker is working again, and a
        # rotation does not change that -- a rotation starts a new session,
        # not a new generation. The generation is the **worker's**: a turn
        # that gets through on one of the worker's lines says the whole
        # worker is working again.
        exhausted = True
    return {
        "provider": plain(worker),
        "sessions": len(keys),
        "context_used_percent": _context_percent(manager, keys, names, context_windows),
        "last_failure_class": failure,
        # The number of rotations is **not known** when the state file could
        # not be read: zero would say the worker never rotated, and that is
        # a claim about a file nobody managed to read.
        "rotations": (
            None
            if _state_unknown(manager)
            else sum(_rotations(record) for record in records if record is not None)
        ),
        "exhausted": exhausted,
        "reset_at": reset_at,
        "pending_rotation": _pending_rotation(records),
    }


def workers_result(
    *,
    manager: Any | None = None,
    snapshot: SnapshotSource | Mapping[str, Any] | None = None,
    session_path: Path | str | None = None,
    snapshot_path: Path | str | None = None,
    context_windows: Callable[[str], int | None] | None = None,
    now: datetime | None = None,
    max_age_seconds: int = _MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """The worker status payload: ``{"workers": [...]}``.

    A worker is a provider the session manager knows about, unioned with
    the providers in the CodexBar snapshot. ``manager`` and ``snapshot``
    are the injection points: a test passes its own manager and its own
    snapshot payload and never touches the real home directory, a real
    session store or a real ``codexbar`` binary.
    """
    moment = _now(now)
    active = manager if manager is not None else _default_manager(session_path)
    payload = _snapshot_payload(snapshot, snapshot_path)
    rows = _snapshot_rows(payload)
    lines = _state_lines(active)
    session_keys: dict[str, list[str]] = {}
    spellings: dict[str, list[str]] = {}
    for key in _manager_keys(active, lines):
        declared = lines.get(key) if isinstance(lines.get(key), Mapping) else None
        spelling = declared.get("provider") if declared else None
        worker = _provider_of(key, spelling)
        if worker is None:
            continue
        session_keys.setdefault(worker, []).append(key)
        names = spellings.setdefault(worker, [worker])
        if (
            isinstance(spelling, str)
            and spelling.strip()
            and spelling.strip() not in names
        ):
            names.append(spelling.strip())
    workers: list[dict[str, Any]] = []
    for worker in sorted(set(session_keys) | set(rows)):
        matched = rows.get(worker)
        workers.append(
            _worker_row(
                worker,
                keys=session_keys.get(worker, []),
                names=spellings.get(worker, [worker]),
                manager=active,
                row=matched[0] if matched else None,
                context_windows=context_windows,
                now=moment,
                max_age_seconds=max_age_seconds,
            )
        )
    return {"workers": workers}


def _cells(row: Mapping[str, Any]) -> list[str]:
    """The table cells of one worker, as safe plain text."""
    percent = row.get("context_used_percent")
    exhausted = row.get("exhausted")
    rotations = row.get("rotations")
    cells = [
        str(row.get("provider", "")),
        str(row.get("sessions", 0)),
        "unknown" if percent is None else f"{float(percent):g}%",
        str(row.get("last_failure_class") or "-"),
        # A count that is not known is a dash, never a zero: zero would say
        # the worker never rotated.
        "-" if rotations is None else str(rotations),
        # A rotation that was started and never finished is worth seeing in
        # the table too: the JSON has said so since it gained the field, and
        # the table is what a person reads.
        "yes" if row.get("pending_rotation") else "no",
        "unknown" if exhausted is None else ("exhausted" if exhausted else "ok"),
        str(row.get("reset_at") or "-"),
    ]
    return [plain(cell) for cell in cells]


def render_workers(result: Mapping[str, Any], *, json_mode: bool) -> None:
    """Print the worker status as JSON or as an aligned table.

    The JSON is the payload itself, without the CLI-wide envelope, so a
    script can read ``workers`` directly. Both forms are printed through
    the terminal-text sanitiser: the values come from files outside the
    program.
    """
    if json_mode:
        print(plain(json.dumps(result, ensure_ascii=True, indent=2)))
        return
    rows = result.get("workers")
    table = [list(_HEADERS)]
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, Mapping):
                table.append(_cells(row))
    widths = [
        max(len(line[column]) for line in table) for column in range(len(_HEADERS))
    ]
    for line in table:
        cells = "  ".join(cell.ljust(width) for cell, width in zip(line, widths))
        print(cells.rstrip())
