"""N45: ``gates.schema_revision`` and load-time migrations.

Each migration is tested on the row the old code really left. Rev 1: a line
parked ``review_unavailable`` before N42 (four prod lines on 2026-11-11) has
no ``review_parked`` record and Stage-Gate still at the review stage, so
``reassign`` answered ``stage_gate_order``. After the migration the row has
the N42 shape: the record and a pause from the review stage.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from src.tools.auto_dispatch import Dispatch, LineStatus
from src.tools.auto_dispatch.approval import _is_gated
from src.tools.auto_dispatch.gates_schema import (
    CURRENT_REVISION,
    LEGACY_REVIEW_PARK,
    MIGRATIONS,
    MIGRATIONS_KEY,
    SCHEMA_REVISION_KEY,
    is_gates_snapshot,
    migrate_dispatch_gates,
    schema_revision,
)
from src.tools.auto_dispatch.machine import REVIEW_PARKED
from tests.unit.server.services.test_dispatch_legacy_unblock import (
    _legacy_review_park,
    _parked_by_outage,
)

pytestmark = pytest.mark.asyncio


def _pre_revision(row: Dispatch) -> Dispatch:
    """A row from before revisions existed: no ``schema_revision`` key."""
    row.gates.pop(SCHEMA_REVISION_KEY, None)
    row.gates.pop(MIGRATIONS_KEY, None)
    return row


def _stamp() -> str:
    return "2026-11-14T00:00:00+00:00"


@pytest.mark.parametrize("blocked_from_review", [False, True])
async def test_rev1_gives_a_legacy_review_park_its_marker_and_reassign_works(
    tmp_path: Path, blocked_from_review: bool
) -> None:
    store, _launcher, _reviewer, service, parked = await _parked_by_outage(tmp_path)
    settled = parked.gates["scheduler"]["settled_lines"][-1]["run_id"]
    await service.apply_mutation(
        parked.id,
        lambda row: _pre_revision(
            _legacy_review_park(row, blocked_from_review=blocked_from_review)
        ),
    )
    legacy = copy.deepcopy(await store.get(parked.id))
    assert schema_revision(legacy.gates) == 0
    assert REVIEW_PARKED not in legacy.gates["lines"]["a"]

    applied = migrate_dispatch_gates(legacy, now=_stamp)

    assert [(item["revision"], item["name"], item["lines"]) for item in applied] == [
        (1, LEGACY_REVIEW_PARK, ["a"])
    ]
    assert schema_revision(legacy.gates) == CURRENT_REVISION
    snap = legacy.gates["lines"]["a"]
    record = snap[REVIEW_PARKED]
    assert record["run_id"] == settled
    assert record["attempt"] == legacy.line("a").attempts
    assert record["author"] == "deepseek"
    assert record["stage"] == "primary_review"
    assert snap["machine"]["stage"] == "blocked"
    assert snap["machine"]["blocked_from"] == "primary_review"
    # The line itself is untouched: still parked, same reason.
    assert legacy.line("a").status is LineStatus.BLOCKED
    assert legacy.line("a").failure_reason == "review_unavailable"
    # Idempotent: a second load changes nothing.
    before = copy.deepcopy(legacy.gates)
    assert migrate_dispatch_gates(legacy, now=_stamp) == []
    assert legacy.gates == before

    # The migrated row is an ordinary N42 park: reassign is a legal swap now.
    await service.apply_mutation(parked.id, lambda row: legacy)
    reassigned = await service.reassign(parked.id, "a", "stepfun")
    assert reassigned.line("a").assigned_model == "stepfun"


async def test_rev1_leaves_a_park_without_a_settled_run(tmp_path: Path) -> None:
    store, _launcher, _reviewer, _service, parked = await _parked_by_outage(tmp_path)
    row = _pre_revision(_legacy_review_park(copy.deepcopy(parked)))
    row.gates["scheduler"]["settled_lines"] = []
    machine_before = copy.deepcopy(row.gates["lines"]["a"]["machine"])

    applied = migrate_dispatch_gates(row, now=_stamp)

    assert applied[0]["lines"] == []
    assert REVIEW_PARKED not in row.gates["lines"]["a"]
    assert row.gates["lines"]["a"]["machine"] == machine_before
    # Stamped anyway: the migration ran and found nothing to do.
    assert schema_revision(row.gates) == CURRENT_REVISION


async def test_current_new_and_foreign_rows_are_not_migrated(tmp_path: Path) -> None:
    _store, _launcher, _reviewer, _service, parked = await _parked_by_outage(tmp_path)
    # A row the current code wrote is stamped current.
    assert schema_revision(parked.gates) == CURRENT_REVISION
    assert migrate_dispatch_gates(copy.deepcopy(parked)) == []
    # A newer revision this code does not know is left as it is.
    newer = copy.deepcopy(parked)
    newer.gates[SCHEMA_REVISION_KEY] = CURRENT_REVISION + 5
    assert migrate_dispatch_gates(newer) == []
    assert newer.gates[SCHEMA_REVISION_KEY] == CURRENT_REVISION + 5
    # Not a gate snapshot at all.
    empty = copy.deepcopy(parked)
    empty.gates = {}
    assert migrate_dispatch_gates(empty) == []
    assert empty.gates == {}


async def test_a_failing_migration_does_not_stamp_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, _launcher, _reviewer, _service, parked = await _parked_by_outage(tmp_path)
    row = _pre_revision(copy.deepcopy(parked))

    def boom(*_args):
        raise RuntimeError("bad row")

    monkeypatch.setitem(MIGRATIONS, 1, (LEGACY_REVIEW_PARK, boom))
    assert migrate_dispatch_gates(row) == []
    assert schema_revision(row.gates) == 0


async def test_gate_runner_migrates_an_old_row_when_it_reads_it(tmp_path: Path) -> None:
    _store, _launcher, _reviewer, service, parked = await _parked_by_outage(tmp_path)
    row = _pre_revision(_legacy_review_park(copy.deepcopy(parked)))

    service._gate_runner().line_snapshot(row, row.line("a"))

    assert schema_revision(row.gates) == CURRENT_REVISION
    assert REVIEW_PARKED in row.gates["lines"]["a"]


async def test_readers_accept_every_known_version(tmp_path: Path) -> None:
    _store, _launcher, _reviewer, _service, parked = await _parked_by_outage(tmp_path)
    assert is_gates_snapshot(parked.gates) and _is_gated(parked)
    parked.gates = {"schema_version": "something_else"}
    assert not is_gates_snapshot(parked.gates) and not _is_gated(parked)
