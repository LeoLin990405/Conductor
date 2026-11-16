"""P0 dedup, wired: line launches, reviews and ``DispatchService.start``.

``off`` is main (no ledger at all); ``shadow`` (the default) keeps the ledger
in memory and logs duplicates without changing a launch, a review call or a
saved row; ``enforce`` keeps the ledger on ``dispatch.gates["dedup"]`` and
reuses an in-flight/done launch, a usable review of the same commit, and an
active dispatch for the same ticket.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.conductor_cli import dispatch as cli_dispatch
from src.server.services.dispatch import DispatchService, DispatchSettings
from src.server.services.autonomy.stage_gate import Stage
from src.tools.auto_dispatch import (
    DispatchState,
    FileApprovalGate,
    InMemoryDispatchStore,
    LineStatus,
    dedup,
    p0_hooks,
)
from src.tools.auto_dispatch.dedup import MODE_ENV
from src.tools.auto_dispatch.machine import HEAD_UNDER_REVIEW, _load_state, _store_state
from src.tools.auto_dispatch.types import Verdict

from .n76_fakes import make_dispatch, make_gates, make_line, make_machine, stored
from .test_machine import _collaborators, _dispatch, _machine
from .test_w3b_actor import _row


@pytest.fixture(autouse=True)
def _default_mode(monkeypatch):
    monkeypatch.delenv(MODE_ENV, raising=False)


# ------------------------------------------------------------------ launches


async def _launched_then_passed(tmp_path, monkeypatch, mode: str):
    """One launch, settled done, the line PASSED: then a second launch request."""
    monkeypatch.setenv(MODE_ENV, mode)
    collaborators, machine = _w3b(tmp_path)
    row = _row(f"dd-{mode}", task="work")
    line = row.lines[0]
    line.base_commit = "abc123"
    await collaborators.store.create(row)
    await machine._launch_line(row, line)
    assert len(collaborators.launcher.launches) == 1
    p0_hooks.launch_settle(machine._dedup_book, row, line, dedup.DONE)
    line.status = LineStatus.PASSED
    line.current_run = None
    await machine._launch_line(row, line)
    return collaborators, machine, row, line


def _w3b(tmp_path):
    from .test_w3b_actor import _machine as w3b_machine

    return w3b_machine(tmp_path)


@pytest.mark.asyncio
async def test_enforce_does_not_relaunch_a_line_already_done_at_that_base(
    tmp_path, monkeypatch
) -> None:
    collaborators, _machine_, row, line = await _launched_then_passed(
        tmp_path, monkeypatch, "enforce"
    )
    assert len(collaborators.launcher.launches) == 1
    assert line.status is LineStatus.PASSED
    entries = row.gates["dedup"]["entries"]
    (entry,) = entries.values()
    assert entry["state"] == dedup.DONE
    assert entry["duplicates"] == 1 and entry["reused"] == 1


@pytest.mark.asyncio
async def test_shadow_relaunches_and_writes_nothing_to_the_row(
    tmp_path, monkeypatch
) -> None:
    collaborators, machine, row, line = await _launched_then_passed(
        tmp_path, monkeypatch, "shadow"
    )
    assert len(collaborators.launcher.launches) == 2
    assert "dedup" not in row.gates
    ledger = machine._dedup_book.ledger(row, "shadow")
    entry = ledger[p0_hooks.launch_key(row, line)]
    assert entry["duplicates"] == 1 and "reused" not in entry


@pytest.mark.asyncio
async def test_off_keeps_no_ledger(tmp_path, monkeypatch) -> None:
    collaborators, machine, row, _line = await _launched_then_passed(
        tmp_path, monkeypatch, "off"
    )
    assert len(collaborators.launcher.launches) == 2
    assert "dedup" not in row.gates
    assert machine._dedup_book.ledger(row, "shadow") == {}


@pytest.mark.asyncio
async def test_enforce_run_claims_and_settles_every_launch(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(MODE_ENV, "enforce")
    collaborators = _collaborators(tmp_path)
    machine, _recorder = _machine(collaborators)
    result = await machine.run(await collaborators.store.create(_dispatch()))

    assert len(collaborators.launcher.launches) == 3
    entries = result.gates["dedup"]["entries"]
    assert len(entries) == 3
    assert {entry["state"] for entry in entries.values()} == {dedup.DONE}


@pytest.mark.asyncio
async def test_a_reassign_settles_the_launch_failed_so_a_retry_runs(
    tmp_path, monkeypatch
) -> None:
    from src.tools.auto_dispatch.dev import ScriptedMonitor
    from src.tools.auto_dispatch.types import RunStatus

    monkeypatch.setenv(MODE_ENV, "enforce")

    class _StallFirst(ScriptedMonitor):
        def __init__(self) -> None:
            super().__init__()
            self.stalled: list[str] = []

        async def poll(self, run_id: str) -> RunStatus:
            if "line-0" in run_id and not self.stalled:
                self.stalled.append(run_id)
                return RunStatus(output="", stalled=True)
            return await super().poll(run_id)

    collaborators = _collaborators(tmp_path, monitor=_StallFirst())
    machine, _recorder = _machine(collaborators)
    result = await machine.run(await collaborators.store.create(_dispatch()))

    entries = result.gates["dedup"]["entries"]
    line0 = [entry for key, entry in entries.items() if "line-0" in key]
    assert sorted(entry["state"] for entry in line0) == [dedup.DONE, dedup.FAILED]
    assert result.line("line-0").status is LineStatus.PASSED


@pytest.mark.asyncio
async def test_a_shadow_run_saves_the_rows_main_saved(tmp_path) -> None:
    collaborators = _collaborators(tmp_path)
    machine, _recorder = _machine(collaborators)
    result = await machine.run(await collaborators.store.create(_dispatch()))
    assert "dedup" not in result.gates
    assert len(collaborators.launcher.launches) == 3


# ------------------------------------------------------------------- reviews


class _Counting:
    def __init__(self, *verdicts: Verdict) -> None:
        self.verdicts = list(verdicts) or [Verdict(passed=False, blockers=["no tests"])]
        self.calls: list[tuple[str, str]] = []

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.calls.append((line_id, model))
        return self.verdicts[min(len(self.calls) - 1, len(self.verdicts) - 1)]


async def _review_setup(tmp_path, *verdicts: Verdict):
    gates = make_gates(tmp_path)
    reviewer = _Counting(*verdicts)
    machine, collaborators, _ = make_machine(tmp_path, gates=gates, reviewer=reviewer)
    line = make_line("line-0", assigned_model="kimi")
    line.status = LineStatus.RUNNING
    line.current_run = "run-1"
    dispatch = await stored(collaborators, make_dispatch(line))
    snap = gates.line_snapshot(dispatch, line)
    _store_state(snap, replace(_load_state(snap), stage=Stage.PRIMARY_REVIEW))
    snap[HEAD_UNDER_REVIEW] = "c0ffee"
    return machine, reviewer, dispatch, line, snap


@pytest.mark.asyncio
async def test_enforce_reuses_a_usable_verdict_for_the_same_commit(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(MODE_ENV, "enforce")
    machine, reviewer, dispatch, line, snap = await _review_setup(tmp_path)

    first = await machine._review_line(line, "codex", dispatch)
    again = await machine._review_line(line, "claude", dispatch)

    assert reviewer.calls == [("line-0", "codex")]
    assert (again.passed, again.blockers) == (first.passed, first.blockers)
    (entry,) = dispatch.gates["dedup"]["entries"].values()
    assert entry["result"]["reviewer"] == "codex"
    assert entry["reused"] == 1

    # A fix round moves the head: a new key, a new review.
    snap[HEAD_UNDER_REVIEW] = "beef00"
    await machine._review_line(line, "claude", dispatch)
    assert reviewer.calls[-1] == ("line-0", "claude")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["operator_unblock", "operator_rereview"])
async def test_an_operator_unblock_or_rereview_forces_a_fresh_review(
    tmp_path, monkeypatch, kind
) -> None:
    monkeypatch.setenv(MODE_ENV, "enforce")
    machine, reviewer, dispatch, line, _snap = await _review_setup(tmp_path)
    await machine._review_line(line, "codex", dispatch)
    dispatch.gates.setdefault("scheduler", {}).setdefault("audit", []).append(
        {"kind": kind, "line_id": line.id}
    )

    await machine._review_line(line, "codex", dispatch)

    assert len(reviewer.calls) == 2
    (entry,) = dispatch.gates["dedup"]["entries"].values()
    assert entry["forced"] == 1
    # The forced review is now the recorded one: a third ask reuses it.
    await machine._review_line(line, "codex", dispatch)
    assert len(reviewer.calls) == 2


@pytest.mark.asyncio
async def test_an_unparseable_verdict_is_never_reused(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(MODE_ENV, "enforce")
    unusable = Verdict(passed=False, blockers=["Reviewer returned no verdict JSON"])
    machine, reviewer, dispatch, line, _snap = await _review_setup(tmp_path, unusable)

    await machine._review_line(line, "codex", dispatch)
    await machine._review_line(line, "codex", dispatch)

    assert len(reviewer.calls) == 2
    assert not (dispatch.gates.get("dedup") or {}).get("entries")


@pytest.mark.asyncio
async def test_shadow_reviews_again_and_counts_the_duplicate_in_memory(tmp_path) -> None:
    machine, reviewer, dispatch, line, _snap = await _review_setup(tmp_path)

    await machine._review_line(line, "codex", dispatch)
    await machine._review_line(line, "codex", dispatch)

    assert len(reviewer.calls) == 2
    assert "dedup" not in dispatch.gates
    (entry,) = machine._dedup_book.ledger(dispatch, "shadow").values()
    assert entry["duplicates"] == 1


@pytest.mark.asyncio
async def test_off_reviews_again_and_records_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(MODE_ENV, "off")
    machine, reviewer, dispatch, line, _snap = await _review_setup(tmp_path)

    await machine._review_line(line, "codex", dispatch)
    await machine._review_line(line, "codex", dispatch)

    assert len(reviewer.calls) == 2
    assert "dedup" not in dispatch.gates
    assert machine._dedup_book.ledger(dispatch, "shadow") == {}


@pytest.mark.asyncio
async def test_the_secondary_review_is_its_own_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(MODE_ENV, "enforce")
    machine, reviewer, dispatch, line, snap = await _review_setup(tmp_path)
    await machine._review_line(line, "codex", dispatch)
    _store_state(snap, replace(_load_state(snap), stage=Stage.SECONDARY_REVIEW))

    await machine._review_line(line, "claude", dispatch)

    assert [model for _line, model in reviewer.calls] == ["codex", "claude"]


# ------------------------------------------------------------ service start


class _Service:
    def __init__(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("src.server.services.dispatch._schedule", lambda key, factory: None)
        self.store = InMemoryDispatchStore()
        self.service = DispatchService(
            DispatchSettings(proposed_dir=tmp_path / "proposed"),
            store=self.store,
            gate=FileApprovalGate(tmp_path / "proposed"),
        )


@pytest.mark.asyncio
async def test_enforce_start_returns_the_active_dispatch_for_the_same_ticket(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(MODE_ENV, "enforce")
    harness = _Service(tmp_path, monkeypatch)
    first = await harness.service.start("fix issue 59", "u1", source_ref="#59")
    again = await harness.service.start("fix it again", "u1", source_ref="Issue #59")

    assert again.id == first.id
    assert len(harness.store.rows) == 1
    assert first.gates["source_ref"] == "issue:59"
    assert first.gates["dedup"]["start_key"] == "dispatch|issue:59|*|*"


@pytest.mark.asyncio
async def test_enforce_start_with_force_or_another_user_or_a_finished_row_is_new(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(MODE_ENV, "enforce")
    harness = _Service(tmp_path, monkeypatch)
    first = await harness.service.start("fix", "u1", source_ref="#59")

    forced = await harness.service.start("fix", "u1", source_ref="#59", force=True)
    other = await harness.service.start("fix", "u2", source_ref="#59")
    assert len({first.id, forced.id, other.id}) == 3
    assert forced.gates["dedup"]["duplicate_of"] == first.id
    assert forced.gates["dedup"]["forced"] is True

    for row in (first, forced):
        row.state = DispatchState.COMPLETE
        await harness.store.save(row)
    later = await harness.service.start("fix", "u1", source_ref="#59")
    assert later.id not in {first.id, forced.id}


@pytest.mark.asyncio
async def test_shadow_start_creates_a_new_row_and_records_the_duplicate(
    tmp_path, monkeypatch
) -> None:
    harness = _Service(tmp_path, monkeypatch)
    first = await harness.service.start("fix", "u1", source_ref="#59")
    again = await harness.service.start("fix", "u1", source_ref="#59")

    assert again.id != first.id
    assert again.gates["dedup"]["duplicate_of"] == first.id
    assert again.gates["dedup"]["mode"] == "shadow"


@pytest.mark.asyncio
async def test_start_without_a_source_ref_is_mains_row(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(MODE_ENV, "enforce")
    harness = _Service(tmp_path, monkeypatch)
    first = await harness.service.start("fix", "u1")
    again = await harness.service.start("fix", "u1")
    assert first.id != again.id
    assert first.gates == {} and again.gates == {}


@pytest.mark.asyncio
async def test_off_start_records_the_ref_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(MODE_ENV, "off")
    harness = _Service(tmp_path, monkeypatch)
    first = await harness.service.start("fix", "u1", source_ref="#59")
    again = await harness.service.start("fix", "u1", source_ref="#59")
    assert first.id != again.id
    assert again.gates == {"source_ref": "issue:59"}


@pytest.mark.asyncio
async def test_the_api_threads_source_ref_and_force(tmp_path, monkeypatch) -> None:
    from src.server.app.dispatch import router
    from tests.conftest import create_test_app

    harness = _Service(tmp_path, monkeypatch)
    service = AsyncMock(wraps=harness.service)
    app = create_test_app(router)
    with patch("src.server.app.dispatch._service", return_value=service):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            response = await http.post(
                "/api/v1/dispatch",
                json={"goal": "fix", "source_ref": "#59", "force": True},
            )
            plain = await http.post("/api/v1/dispatch", json={"goal": "fix"})
    assert response.status_code == 201 and plain.status_code == 201
    kwargs = [call.kwargs for call in service.start.await_args_list]
    assert kwargs[0]["source_ref"] == "#59" and kwargs[0]["force"] is True
    assert "source_ref" not in kwargs[1] and "force" not in kwargs[1]


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def post(self, path: str, json=None):
        self.calls.append(("POST", path, json))
        return {"id": "d1", "state": "SUBMITTED", "lines": []}

    def get(self, path: str, *, params=None):
        return {"id": "d1", "state": "SUBMITTED", "lines": []}


def _cli(*words: str, **flags):
    client = _FakeClient()
    namespace = argparse.Namespace(
        args=list(words),
        note=None,
        force=flags.get("force", False),
        json=False,
        workers=None,
        source_ref=flags.get("source_ref"),
    )
    return client, cli_dispatch._dispatch(namespace, client)


def test_cli_start_sends_source_ref_and_force() -> None:
    client, _ = _cli("start", "fix", "it", source_ref="#59", force=True)
    assert client.calls == [
        ("POST", "/api/v1/dispatch", {"goal": "fix it", "source_ref": "#59", "force": True})
    ]


def test_cli_refuses_source_ref_off_start_and_force_on_a_bare_start() -> None:
    with pytest.raises(cli_dispatch.DispatchUsageError, match="--source-ref goes with"):
        _cli("status", "d1", source_ref="#59")
    with pytest.raises(cli_dispatch.DispatchUsageError, match="--force goes with"):
        _cli("start", "fix", force=True)


def test_the_parser_accepts_source_ref() -> None:
    from src.conductor_cli import cli

    parsed = cli.build_parser().parse_args(
        ["dispatch", "start", "fix", "--source-ref", "#59", "--force"]
    )
    assert parsed.source_ref == "#59" and parsed.force is True
