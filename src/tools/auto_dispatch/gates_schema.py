"""The ``dispatch_gates_v1`` snapshot's revision and its load-time migrations (N45).

New park markers stranded lines parked by older code: after #93, four prod
lines parked ``review_unavailable`` before N42 had no ``review_parked``
record, so ``unblock`` said ``line_not_wedged`` and ``reassign`` said
``stage_gate_order`` (#94 taught ``unblock`` that one shape by hand). A row
now carries ``gates.schema_revision``; a row below :data:`CURRENT_REVISION`
is upgraded when it is loaded, one migration per revision, so the rest of the
code sees only the current shape.

The family string stays ``dispatch_gates_v1``. Bumping it would make the code
it replaces (a rollback) treat every new row as foreign and reset its gates
(``_ensure_root`` rebuilds a snapshot whose version it does not know), and
readers that compare the string would stop seeing gated rows. The revision
is an extra key older code keeps and ignores.

Rules for a migration:

* it is idempotent and only adds what newer code would have written; it never
  deletes a record or moves a line's status;
* it is pure apart from the dispatch it is given (no Git, no clock other than
  the stamp it is passed, no I/O), so loading a row stays cheap;
* it ships with a test that builds the legacy row the old code really left.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .types import Dispatch, Line, LineStatus

logger = logging.getLogger(__name__)

#: The snapshot family. Unchanged by revisions; see the module docstring.
GATES_SCHEMA_VERSION = "dispatch_gates_v1"
#: Versions a reader treats as a gate snapshot.
KNOWN_SCHEMA_VERSIONS = frozenset({GATES_SCHEMA_VERSION})
SCHEMA_REVISION_KEY = "schema_revision"
#: The audit of applied migrations on the snapshot root.
MIGRATIONS_KEY = "schema_migrations"
_MAX_AUDIT = 20

#: Rev 1 (N45): a pre-N42 ``review_unavailable`` park gets its ``review_parked``.
LEGACY_REVIEW_PARK = "legacy_review_park"

_REVIEW_STAGES = ("primary_review", "secondary_review")


def is_gates_snapshot(gates: object) -> bool:
    """True for a gate snapshot of a known version (any revision)."""
    return isinstance(gates, dict) and gates.get("schema_version") in KNOWN_SCHEMA_VERSIONS


def schema_revision(gates: object) -> int:
    """The snapshot's revision; ``0`` for a row written before revisions existed."""
    if not isinstance(gates, dict):
        return 0
    value = gates.get(SCHEMA_REVISION_KEY)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _settled_run(root: dict[str, Any], line: Line) -> str | None:
    """The last run of the line's current attempt the scheduler recorded as finished."""
    scheduler = root.get("scheduler")
    settled = scheduler.get("settled_lines") if isinstance(scheduler, dict) else None
    found: str | None = None
    for item in settled if isinstance(settled, list) else []:
        if (
            isinstance(item, dict)
            and item.get("line_id") == line.id
            and item.get("attempt") == line.attempts
            and isinstance(item.get("run_id"), str)
            and item["run_id"]
        ):
            found = item["run_id"]
    return found


def _migrate_legacy_review_park(dispatch: Dispatch, root: dict[str, Any], stamp: str) -> list[str]:
    """Rev 1: write the ``review_parked`` record a pre-N42 review park lacks.

    The old code blocked the line ``review_unavailable`` and left Stage-Gate
    at the review stage (or a later pause blocked it from there), with no
    ``review_parked`` / ``review_retry`` record. N42 code writes the record
    (the settled run, attempt and author) and blocks Stage-Gate from the
    review stage. This writes the same, so ``unblock`` resumes the review
    and ``reassign`` is a legal swap from the pause. A line whose current
    attempt has no settled run is left as it is: there is nothing to resume.
    """
    from src.server.services.autonomy.stage_gate import Event, Stage, advance

    from .machine import (
        REVIEW_PARKED,
        REVIEW_RETRY,
        REVIEW_UNAVAILABLE,
        _load_state,
        _remember_event,
        _store_state,
    )

    lines = root.get("lines")
    if not isinstance(lines, dict):
        return []
    changed: list[str] = []
    for line in dispatch.lines:
        snap = lines.get(line.id)
        if not isinstance(snap, dict):
            continue
        if line.failure_reason != REVIEW_UNAVAILABLE or line.current_run:
            continue
        if line.status not in (LineStatus.BLOCKED, LineStatus.FAILED):
            continue
        if snap.get(REVIEW_PARKED) is not None or snap.get(REVIEW_RETRY) is not None:
            continue
        state = _load_state(snap)
        at_review = state.stage.value in _REVIEW_STAGES
        from_review = state.stage is Stage.BLOCKED and state.blocked_from in _REVIEW_STAGES
        if not (at_review or from_review):
            continue
        run_id = _settled_run(root, line)
        if run_id is None:
            continue
        author = line.assigned_model or ""
        if at_review:
            transition = advance(
                state, Event(kind="block", timestamp=stamp, provider=author)
            )
            if not transition.accepted:
                logger.warning(
                    "gates migration %s: Stage-Gate refused block on %s: %s",
                    LEGACY_REVIEW_PARK,
                    line.id,
                    transition.reason,
                )
                continue
            _remember_event(snap, transition)
            _store_state(snap, transition.state)
        snap[REVIEW_PARKED] = {
            "run_id": run_id,
            "attempt": line.attempts,
            "author": author,
            "stage": state.stage.value if at_review else state.blocked_from,
            "failed": [],
            "migrated": LEGACY_REVIEW_PARK,
        }
        changed.append(line.id)
    return changed


#: Revision -> (name, migration). Revision N upgrades a row from N-1 to N.
MIGRATIONS: dict[int, tuple[str, Callable[[Dispatch, dict[str, Any], str], list[str]]]] = {
    1: (LEGACY_REVIEW_PARK, _migrate_legacy_review_park),
}
CURRENT_REVISION = max(MIGRATIONS)


def stamp_current(root: dict[str, Any]) -> None:
    """Mark a snapshot the current code just created: nothing to migrate."""
    root[SCHEMA_REVISION_KEY] = CURRENT_REVISION


def migrate_dispatch_gates(
    dispatch: Dispatch, *, now: Callable[[], str] | None = None
) -> list[dict[str, Any]]:
    """Upgrade ``dispatch.gates`` to :data:`CURRENT_REVISION` in place.

    Returns what was applied (empty when the row is current, not a gate
    snapshot, or from a newer revision this code does not know: such a row
    is left untouched). A migration that raises is logged and stops the
    upgrade at the revision before it, so the row is never stamped past a
    migration that did not run.
    """
    root = dispatch.gates
    if not is_gates_snapshot(root):
        return []
    revision = schema_revision(root)
    if revision >= CURRENT_REVISION:
        return []
    stamp = (now or _now)()
    applied: list[dict[str, Any]] = []
    for number in range(revision + 1, CURRENT_REVISION + 1):
        name, migration = MIGRATIONS[number]
        try:
            changed = migration(dispatch, root, stamp)
        except Exception as exc:  # noqa: BLE001 - loading a row must not fail
            logger.warning(
                "gates migration %s failed on dispatch %s: %s",
                name,
                dispatch.id,
                type(exc).__name__,
            )
            break
        root[SCHEMA_REVISION_KEY] = number
        entry = {"revision": number, "name": name, "lines": changed, "at": stamp}
        applied.append(entry)
    # Only a migration that changed a line is audited; stamping a row that
    # needed nothing (every row on its first load) leaves no entry.
    changed = [entry for entry in applied if entry["lines"]]
    if changed:
        audit = root.get(MIGRATIONS_KEY)
        audit = list(audit) if isinstance(audit, list) else []
        audit.extend(changed)
        root[MIGRATIONS_KEY] = audit[-_MAX_AUDIT:]
    return applied


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "CURRENT_REVISION",
    "GATES_SCHEMA_VERSION",
    "KNOWN_SCHEMA_VERSIONS",
    "LEGACY_REVIEW_PARK",
    "MIGRATIONS",
    "MIGRATIONS_KEY",
    "SCHEMA_REVISION_KEY",
    "is_gates_snapshot",
    "migrate_dispatch_gates",
    "schema_revision",
    "stamp_current",
]
