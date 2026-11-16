"""Narrow hooks that wire the P0 silent / dedup / strict-review modules in.

The pure modules (``silent_worker``, ``dedup``, ``review_strict``) decide;
this module holds the glue the hot files call, so ``machine.py`` and
``execution.py`` only carry one-line hooks. Every hook here is a no-op in
``off`` mode, records only in ``shadow`` (the default) and changes an outcome
only in ``enforce``.

Dedup persistence: in ``enforce`` the ledger lives on the dispatch row under
``gates["dedup"]["entries"]`` (JSON-safe, survives a restart). In ``shadow``
the ledger, and the duplicate counts on it, are kept in memory for the life
of the machine and every duplicate is logged: a shadow drive saves exactly
the rows main saved (the N76 off-mode trace stays byte-identical).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import weakref
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import dedup, review_strict, silent_worker
from .liveness.modes import ENFORCE, OFF
from .review_triage import INCONCLUSIVE, with_status
from .types import Dispatch, Line, LineStatus, Verdict

logger = logging.getLogger(__name__)

#: Row key of the dedup ledger and its records.
DEDUP_KEY = "dedup"
#: Row key of the silent-worker cells of an ungated dispatch (no Stage-Gate).
UNGATED_SILENT_KEY = "silent_worker_lines"
#: A log larger than this is assumed to carry model output (never read whole).
SILENT_READ_CAP = 1 << 20
#: The ``_apply_poll_event`` action for a silent run in enforce.
REASSIGN_SILENT = "reassign_silent"
#: Operator verbs that force a fresh review of the same commit.
_FORCE_AUDIT_KINDS = frozenset({"operator_unblock", "operator_rereview"})
#: Shadow ledgers kept in memory at once (oldest dropped first).
_MAX_MEMORY_DISPATCHES = 64


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------ silent worker


def monitor_silent(
    log_path: Path,
    size: int,
    *,
    complete: bool,
    exit_code: int | None,
    elapsed: float,
    environ: Mapping[str, str] | None = None,
) -> tuple[silent_worker.SilentFinding | None, bool]:
    """``(finding, spoke)`` for one ``LogMonitor`` poll.

    ``spoke`` is True once the log is known to carry model output, so the
    monitor can stop reading it. Off reads nothing; a running log is read only
    once it is past the silence threshold.
    """
    if not silent_worker.should_record(environ=environ):
        return None, False
    if exit_code not in (None, 0):
        return None, False
    if not complete and elapsed < silent_worker.silent_after_seconds(environ):
        return None, False
    if size > SILENT_READ_CAP:
        return None, True
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace") if size else ""
    except OSError:
        return None, False
    if silent_worker.has_assistant_output(text):
        return None, True
    finding = silent_worker.classify_run(
        text,
        complete=complete,
        exit_code=exit_code,
        elapsed_seconds=elapsed,
        environ=environ,
    )
    return finding, False


def silent_cell(dispatch: Dispatch, line: Line, gates: Any | None) -> dict[str, Any]:
    """The snapshot a finding is recorded on: the Stage-Gate cell, or a row cell."""
    if gates is not None:
        return gates.line_snapshot(dispatch, line)
    if not isinstance(dispatch.gates, dict):
        dispatch.gates = {}
    cells = dispatch.gates.setdefault(UNGATED_SILENT_KEY, {})
    if not isinstance(cells, dict):
        cells = {}
        dispatch.gates[UNGATED_SILENT_KEY] = cells
    cell = cells.setdefault(line.id, {})
    if not isinstance(cell, dict):
        cell = {}
        cells[line.id] = cell
    return cell


def note_silent(
    snapshot: dict[str, Any],
    finding: silent_worker.SilentFinding,
    *,
    line_id: str,
    run_id: str | None,
    attempt: int,
    author: str,
    alert: Callable[[str], None] | None = None,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> bool:
    """Record a finding, alert once per run, and say whether to act (enforce)."""
    mode = silent_worker.silent_mode(environ)
    if mode == OFF:
        return False
    silent_worker.record_finding(
        snapshot,
        finding,
        run_id=run_id,
        attempt=attempt,
        author=author,
        mode=mode,
        now=now or _utc_now(),
    )
    if silent_worker.should_alert(snapshot, run_id):
        silent_worker.mark_alerted(snapshot, run_id)
        text = silent_worker.alert_text(line_id, author, finding)
        logger.warning("%s [silent-worker mode=%s]", text, mode)
        if alert is not None:
            alert(text)
    return mode == ENFORCE


#: Why an enforce finding was only recorded (on the run's silent record).
SILENT_ENFORCE_SKIPPED = "enforce_skipped"
HEAD_MOVED = "head_moved"
HEAD_UNKNOWN = "head_unknown"


def run_head_moved(
    runs: list[Mapping[str, Any]] | None, *, attempt: int, author: str
) -> bool | None:
    """Whether this attempt's run moved the line's branch head; None if unknown.

    ``runs`` are the machine's per-branch run records (``start`` / ``head`` as
    the launcher reported them). The newest record of this attempt and author
    decides; no record, or one without both heads, is unknown.
    """
    for run in reversed(runs or []):
        if not isinstance(run, Mapping) or run.get("attempt") != attempt:
            continue
        if author and run.get("author") not in (None, "", author):
            continue
        start, head = run.get("start"), run.get("head")
        if isinstance(start, str) and start and isinstance(head, str) and head:
            return start != head
        return None
    return None


def silent_enforce_allowed(
    snapshot: dict[str, Any],
    finding: silent_worker.SilentFinding,
    runs: list[Mapping[str, Any]] | None,
    *,
    attempt: int,
    author: str,
) -> bool:
    """Whether an enforce finding may cost the attempt (record only otherwise).

    An ``empty_final_answer`` run that moved its branch head did its work
    (like the Ticket side's ``_has_commit``); when the movement cannot be
    told, the finding is only recorded too. The reason is kept on the run's
    silent record.
    """
    if getattr(finding, "kind", "") != silent_worker.KIND_EMPTY_FINAL:
        return True
    moved = run_head_moved(runs, attempt=attempt, author=author)
    if moved is False:
        return True
    cell = snapshot.get(silent_worker.RECORD_KEY) if isinstance(snapshot, dict) else None
    records = cell.get("runs") if isinstance(cell, dict) else None
    record = records.get(cell.get("last")) if isinstance(records, dict) else None
    if isinstance(record, dict):
        record[SILENT_ENFORCE_SKIPPED] = HEAD_MOVED if moved else HEAD_UNKNOWN
    logger.warning(
        "Silent run of attempt %s by %s: %s, recorded only", attempt, author,
        HEAD_MOVED if moved else HEAD_UNKNOWN,
    )
    return False


# -------------------------------------------------------------------- dedup


class DedupBook:
    """Where the dedup ledger lives: the row in enforce, memory in shadow."""

    def __init__(self) -> None:
        self._memory: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _root(dispatch: Dispatch) -> dict[str, Any]:
        if not isinstance(dispatch.gates, dict):
            dispatch.gates = {}
        root = dispatch.gates.setdefault(DEDUP_KEY, {})
        if not isinstance(root, dict):
            root = {}
            dispatch.gates[DEDUP_KEY] = root
        return root

    def ledger(self, dispatch: Dispatch, mode: str) -> dict[str, Any]:
        """The ledger to decide against. Enforce reads and writes the row."""
        root = dispatch.gates.get(DEDUP_KEY) if isinstance(dispatch.gates, dict) else None
        persisted = root.get("entries") if isinstance(root, dict) else None
        if mode == ENFORCE:
            if not isinstance(persisted, dict):
                persisted = {}
                self._root(dispatch)["entries"] = persisted
            return persisted
        memory = self._memory.get(dispatch.id)
        if memory is None:
            while len(self._memory) >= _MAX_MEMORY_DISPATCHES:
                self._memory.pop(next(iter(self._memory)))
            memory = self._memory.setdefault(dispatch.id, {})
        if isinstance(persisted, dict):
            # Entries an enforce drive (or an earlier duplicate) saved still count.
            for key, entry in persisted.items():
                memory.setdefault(key, entry)
        return memory

    def persist(
        self, dispatch: Dispatch, key: str, entry: Mapping[str, Any], mode: str
    ) -> None:
        """Copy one entry to the row; only enforce writes the row."""
        if mode != ENFORCE:
            return
        entries = self._root(dispatch).setdefault("entries", {})
        if isinstance(entries, dict):
            entries[key] = dict(entry)


def launch_key(dispatch: Dispatch, line: Line) -> str | None:
    return dedup.dispatch_key(
        f"{dispatch.id}:{line.id}",
        line.assigned_model or line.id,
        line.base_commit or "*",
    )


def _launch_live(entry: Mapping[str, Any] | None, line: Line) -> bool:
    """A reusable entry only counts while the line itself still shows it."""
    if not isinstance(entry, Mapping):
        return False
    state = entry.get("state")
    if state == dedup.IN_FLIGHT:
        return line.status is LineStatus.RUNNING and bool(line.current_run)
    if state == dedup.DONE:
        return line.status is LineStatus.PASSED
    return False


def launch_check(
    book: DedupBook,
    dispatch: Dispatch,
    line: Line,
    *,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> bool:
    """True when enforce reuses an in-flight or done launch of the same key."""
    mode = dedup.dedup_mode(environ)
    if mode == OFF:
        return False
    key = launch_key(dispatch, line)
    if key is None:
        return False
    moment = now or _utc_now()
    ledger = book.ledger(dispatch, mode)
    decision = dedup.decide(ledger, key, mode=mode)
    if decision.would_reuse and _launch_live(decision.existing, line):
        entry = dedup.note_duplicate(ledger, key, decision=decision, now=moment)
        if entry is not None:
            book.persist(dispatch, key, entry, mode)
        logger.warning(
            "Duplicate launch of line %s (%s, mode=%s): %s",
            line.id,
            (decision.existing or {}).get("state"),
            mode,
            "reused" if decision.action == "reuse" else "would reuse",
        )
        return decision.action == "reuse"
    entry = dedup.claim(
        ledger,
        key,
        str(line.attempts),
        now=moment,
        meta={"line": line.id, "author": line.assigned_model or "", "base": line.base_commit or ""},
    )
    book.persist(dispatch, key, entry, mode)
    return False


def launch_settle(
    book: DedupBook,
    dispatch: Dispatch,
    line: Line,
    state: str,
    *,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> None:
    """Settle the line's launch entry ``done`` or ``failed``; unknown keys are left."""
    mode = dedup.dedup_mode(environ)
    if mode == OFF:
        return
    key = launch_key(dispatch, line)
    if key is None:
        return
    ledger = book.ledger(dispatch, mode)
    entry = ledger.get(key)
    if not isinstance(entry, Mapping) or entry.get("state") != dedup.IN_FLIGHT:
        return
    settled = dedup.settle(ledger, key, state, now=now or _utc_now())
    if settled is not None:
        book.persist(dispatch, key, settled, mode)


def _operator_seq(dispatch: Dispatch, line_id: str) -> int:
    """How many operator unblock / rereview entries name this line."""
    root = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    scheduler = root.get("scheduler") if isinstance(root, dict) else None
    audit = scheduler.get("audit") if isinstance(scheduler, dict) else None
    items = audit if isinstance(audit, list) else []
    return sum(
        1
        for item in items
        if isinstance(item, dict)
        and item.get("line_id") == line_id
        and item.get("kind") in _FORCE_AUDIT_KINDS
    )


#: Snapshot keys whose content changes what a reviewer is sent at one head
#: (``rulings.RULING_CHECK``, ``machine.RULING_REISSUE_HEAD``).
_REVIEW_MATERIAL_KEYS = ("ruling_check", "ruling_reissue_head")


def review_head_key(
    head: str, notes: str = "", snapshot: Mapping[str, Any] | None = None
) -> str:
    """``head``, plus a digest of the notes and ruling material the reviewer gets.

    A ruling reissue sends the same head back with new notes or a new ruling
    check: that is a new review, never a reuse of the verdict at that head.
    No material leaves the plain head (the key main's shadow ledgers used).
    """
    if not head:
        return head
    material: dict[str, Any] = {"notes": notes or ""}
    if isinstance(snapshot, Mapping):
        for name in _REVIEW_MATERIAL_KEYS:
            if snapshot.get(name) not in (None, "", {}, []):
                material[name] = snapshot.get(name)
    if material == {"notes": ""}:
        return head
    blob = json.dumps(material, sort_keys=True, default=str, ensure_ascii=False)
    return f"{head}#{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]}"


def review_key_for(line: Line, stage: str, head: str) -> str | None:
    return dedup.review_key(f"{line.id}@{stage}", head)


def usable_verdict(verdict: Verdict) -> bool:
    """A verdict worth reusing: parsed, not strict-inconclusive, not N59 inconclusive."""
    from .execution import verdict_unparseable

    if verdict_unparseable(verdict) or review_strict.is_strict_inconclusive(verdict):
        return False
    return str(getattr(verdict, "status", "") or "") != INCONCLUSIVE


def _verdict_from(result: Mapping[str, Any]) -> Verdict:
    base = Verdict(
        passed=bool(result.get("passed")),
        blockers=[str(item) for item in result.get("blockers") or []],
        verdict=str(result.get("verdict") or ""),
    )
    status = str(result.get("status") or "")
    return with_status(base, {"status": status}) if status else base


def review_reuse(
    book: DedupBook,
    dispatch: Dispatch,
    line: Line,
    *,
    stage: str,
    head: str,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> Verdict | None:
    """The recorded verdict enforce reuses for ``(line, stage, head)``, or None.

    An operator ``unblock`` or ``recover --rereview`` since the verdict was
    recorded forces a fresh review. Shadow records the duplicate and returns
    None, so the reviewer is still asked.
    """
    mode = dedup.dedup_mode(environ)
    if mode == OFF:
        return None
    key = review_key_for(line, stage, head)
    if key is None:
        return None
    ledger = book.ledger(dispatch, mode)
    entry = ledger.get(key)
    if not isinstance(entry, Mapping) or entry.get("state") != dedup.DONE:
        return None
    result = entry.get("result")
    if not isinstance(result, Mapping):
        return None
    forced = _operator_seq(dispatch, line.id) > int(result.get("operator_seq") or 0)
    decision = dedup.decide(ledger, key, force=forced, mode=mode)
    noted = dedup.note_duplicate(ledger, key, decision=decision, now=now or _utc_now())
    if noted is not None:
        book.persist(dispatch, key, noted, mode)
    if decision.action != "reuse":
        if decision.would_reuse:
            logger.warning(
                "Duplicate review of line %s at %s (mode=%s): would reuse", line.id, head, mode
            )
        return None
    logger.warning("Duplicate review of line %s at %s: reusing the recorded verdict", line.id, head)
    return _verdict_from(result)


def review_record(
    book: DedupBook,
    dispatch: Dispatch,
    line: Line,
    verdict: Verdict,
    *,
    stage: str,
    head: str,
    reviewer: str,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> None:
    """Record a usable verdict for ``(line, stage, head)`` as done."""
    mode = dedup.dedup_mode(environ)
    if mode == OFF or not usable_verdict(verdict):
        return
    key = review_key_for(line, stage, head)
    if key is None:
        return
    moment = now or _utc_now()
    ledger = book.ledger(dispatch, mode)
    dedup.claim(ledger, key, reviewer, now=moment, meta={"line": line.id, "stage": stage})
    settled = dedup.settle(
        ledger,
        key,
        dedup.DONE,
        now=moment,
        result={
            "passed": bool(verdict.passed),
            "blockers": [str(item) for item in verdict.blockers],
            "verdict": str(verdict.verdict or ""),
            "status": str(getattr(verdict, "status", "") or ""),
            "reviewer": reviewer,
            "operator_seq": _operator_seq(dispatch, line.id),
        },
    )
    if settled is not None:
        book.persist(dispatch, key, settled, mode)


#: Row key of the ticket/issue a dispatch was started for.
SOURCE_REF_KEY = "source_ref"


def start_key(source_ref: str | None) -> str | None:
    """``DispatchService.start``'s key: the ref, any line, any base.

    The repository HEAD is not known cheaply before the row exists, so the
    base is the wildcard ``*``: one active dispatch per ticket per user.
    """
    if not source_ref:
        return None
    return dedup.dispatch_key(source_ref, "*", "*")


_START_LOCKS: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


@asynccontextmanager
async def start_lock(
    user_id: str,
    source_ref: str | None,
    store: Any,
    *,
    environ: Mapping[str, str] | None = None,
) -> AsyncIterator[None]:
    """Hold the start-dedup check and the row create as one step.

    A process lock per ``(user, start key)``, plus the store's
    ``path_claim_lock`` when it has one (the Postgres advisory lock, so two
    server processes serialise too). No ref, or off, takes no lock.
    """
    ref = dedup.normalize_ref(source_ref) if source_ref else None
    key = start_key(ref)
    if key is None or dedup.dedup_mode(environ) == OFF:
        yield
        return
    name = f"{user_id}\0{key}"
    lock = _START_LOCKS.get(name)
    if lock is None:
        lock = asyncio.Lock()
        _START_LOCKS[name] = lock
    async with lock:
        factory = getattr(store, "path_claim_lock", None)
        if callable(factory):
            async with factory():
                yield
        else:
            yield


def start_duplicate(
    dispatch: Dispatch,
    source_ref: str | None,
    active: list[Dispatch],
    *,
    force: bool = False,
    environ: Mapping[str, str] | None = None,
) -> Dispatch | None:
    """The active dispatch enforce returns instead of ``dispatch``, or None.

    Records the source ref (and, outside off, the key and any duplicate) on
    the new row's gates. ``active`` is the user's non-terminal dispatches.
    """
    ref = dedup.normalize_ref(source_ref) if source_ref else None
    if not ref:
        return None
    if not isinstance(dispatch.gates, dict):
        dispatch.gates = {}
    dispatch.gates[SOURCE_REF_KEY] = ref
    mode = dedup.dedup_mode(environ)
    key = start_key(ref)
    if mode == OFF or key is None:
        return None
    record: dict[str, Any] = {"start_key": key}
    dispatch.gates[DEDUP_KEY] = record
    existing = next(
        (
            row
            for row in active
            if row.id != dispatch.id
            and row.user_id == dispatch.user_id
            and isinstance(row.gates, dict)
            and isinstance(row.gates.get(DEDUP_KEY), dict)
            and row.gates[DEDUP_KEY].get("start_key") == key
        ),
        None,
    )
    if existing is None:
        return None
    reuse = mode == ENFORCE and not force
    logger.warning(
        "Duplicate dispatch start for %s (mode=%s, force=%s): %s %s",
        ref,
        mode,
        bool(force),
        "returning" if reuse else "would return",
        existing.id,
    )
    if reuse:
        return existing
    record.update({"duplicate_of": existing.id, "forced": bool(force), "mode": mode})
    return None


# ------------------------------------------------------------ strict review


def record_strict_attempts(
    snapshot: dict[str, Any] | None,
    attempts: list[Mapping[str, Any]] | None,
    *,
    now: object,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Copy the attempts ``CrossReviewer`` noted onto the line snapshot."""
    if not isinstance(snapshot, dict) or not attempts:
        return
    mode = review_strict.strict_mode(environ)
    if mode == OFF:
        return
    for attempt in attempts:
        review_strict.record_attempt(
            snapshot,
            reviewer=str(attempt.get("reviewer") or ""),
            kind=str(attempt.get("kind") or ""),
            retried=bool(attempt.get("retried")),
            outcome=str(attempt.get("outcome") or ""),
            mode=mode,
            now=now,
        )


def last_strict_kind(snapshot: Mapping[str, Any] | None) -> str:
    """The kind of the newest recorded reviewer reply, ``prose`` when unknown."""
    cell = snapshot.get(review_strict.RECORD_KEY) if isinstance(snapshot, Mapping) else None
    attempts = cell.get("attempts") if isinstance(cell, Mapping) else None
    if isinstance(attempts, list):
        for attempt in reversed(attempts):
            kind = attempt.get("kind") if isinstance(attempt, Mapping) else None
            if kind in (review_strict.EMPTY, review_strict.PROSE):
                return str(kind)
    return review_strict.PROSE


__all__ = [
    "DEDUP_KEY",
    "DedupBook",
    "REASSIGN_SILENT",
    "SILENT_READ_CAP",
    "SOURCE_REF_KEY",
    "UNGATED_SILENT_KEY",
    "last_strict_kind",
    "launch_check",
    "launch_key",
    "launch_settle",
    "monitor_silent",
    "note_silent",
    "record_strict_attempts",
    "run_head_moved",
    "silent_enforce_allowed",
    "review_head_key",
    "review_key_for",
    "review_record",
    "review_reuse",
    "silent_cell",
    "start_duplicate",
    "start_key",
    "start_lock",
    "usable_verdict",
]
