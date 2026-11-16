"""N77: a human request queued while a drive runs is applied promptly.

The service queues an unblock, reassign or adjudication in
``gates.human_reassigns`` while a drive owns the dispatch. The drive used to
read that queue only at the top of its gated loop, after the monitor had
waited for every running line and after every review of the pass. It now also
reads it after each monitor poll and after each single review. The refusal
checks are unchanged, so a request for a line that is running is still
refused, and each request is applied once.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus
from src.tools.auto_dispatch.dev import RoundRobinSelector
from src.tools.auto_dispatch.gates import REVIEW_UNPARSEABLE
from src.tools.auto_dispatch.machine import pending_human_reassigns, queue_human_request
from src.tools.auto_dispatch.execution import _parse_verdict
from src.tools.auto_dispatch.types import RunStatus, Verdict
from tests.unit.server.services.test_dispatch_r2_stale_result import Copies, _service
from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle as _machine_bundle
from tests.unit.tools.auto_dispatch.test_machine_gates import _line as _machine_line
from tests.unit.tools.auto_dispatch.test_machine_gates import _repo_with_lines
from tests.unit.tools.auto_dispatch.test_review_unparseable import PASS, PROSE_AFTER
from tests.unit.tools.auto_dispatch.test_w3b_fix1 import _settings

pytestmark = pytest.mark.asyncio

WAIT = 5.0


class ScriptReviewer:
    """Unparseable for ``a`` while ``park_a`` is set; a held review for ``hold``."""

    def __init__(self) -> None:
        self.park_a = True
        self.calls: list[tuple[str, str]] = []
        self.hold: str | None = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.on_call = None

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.calls.append((line_id, model))
        if self.on_call is not None:
            await self.on_call(line_id)
        if line_id == "a" and self.park_a:
            return _parse_verdict(PROSE_AFTER)
        if line_id == self.hold:
            self.entered.set()
            await self.release.wait()
        return _parse_verdict(PASS)


class HeldMonitor:
    """Runs of the ``held`` lines stay incomplete until ``release`` is set."""

    def __init__(self, *held: str) -> None:
        self.held = set(held)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def poll(self, run_id: str) -> RunStatus:
        if any(f":{line_id}:" in run_id for line_id in self.held):
            if not self.release.is_set():
                self.entered.set()
                await asyncio.sleep(0.005)
                return RunStatus(output="working", complete=False, tokens=1)
        return RunStatus(output=f"done {run_id}", complete=True, tokens=8)

    async def cancel(self, run_id: str) -> None:
        del run_id


async def _parked_a(tmp_path: Path, *later: str):
    """Line ``a`` review-parked; the ``later`` lines waited on it and never ran."""
    repo = _repo_with_lines(tmp_path / "repo", "a", *later)
    reviewer = ScriptReviewer()
    models = {"b": "kimi", "c": "codex"}
    collab, machine, _commands, _trees = _machine_bundle(
        tmp_path,
        [
            _machine_line("a", assigned_model="kimi", write_scope=("a/",)),
            *(
                _machine_line(
                    line_id,
                    assigned_model=models[line_id],
                    depends_on=("a",),
                    write_scope=(f"{line_id}/",),
                )
                for line_id in later
            ),
        ],
        reviewer=reviewer,
        settings=_settings(tmp_path, repo),
        selector=RoundRobinSelector(("kimi", "codex")),
    )
    store = Copies()
    collab.store = store
    dispatch = await store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )
    parked = await machine.run(dispatch)
    line = parked.line("a")
    assert line is not None
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == REVIEW_UNPARSEABLE
    assert all(parked.line(line_id).status is LineStatus.ASSIGNED for line_id in later)
    # The later lines no longer wait on ``a``, so the next drive runs them
    # while ``a`` stays parked.
    row = store.rows["d1"]
    for line_id in later:
        row.line(line_id).depends_on = ()
    reviewer.park_a = False
    service = _service(tmp_path, store, lambda _dispatch: machine)
    service._gate_runner = lambda: collab.gates
    resumed: list[str] = []

    async def record_resume(dispatch: Dispatch) -> Dispatch:
        resumed.append(dispatch.id)
        return dispatch

    service._resume = record_resume
    return collab, machine, reviewer, store, service, resumed


async def _until(check, *, timeout: float = WAIT) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not await check():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


def _human_audit(row: Dispatch, line_id: str) -> list[dict[str, object]]:
    audit = ((row.gates or {}).get("scheduler") or {}).get("audit") or []
    return [
        item
        for item in audit
        if isinstance(item, dict)
        and item.get("line_id") == line_id
        and str(item.get("kind", "")).startswith("human_")
    ]


async def test_an_unblock_is_applied_while_another_line_is_still_monitored(
    tmp_path: Path,
) -> None:
    collab, machine, reviewer, store, service, resumed = await _parked_a(tmp_path, "b")
    monitor = HeldMonitor("b")
    collab.monitor = monitor
    a_reviews = len([call for call in reviewer.calls if call[0] == "a"])

    drive = asyncio.create_task(service._drive("d1"))
    await asyncio.wait_for(monitor.entered.wait(), timeout=WAIT)
    row = await store.get("d1")
    assert row.line("b").status is LineStatus.RUNNING
    assert row.line("a").status is LineStatus.BLOCKED

    await service.unblock("d1", "a", note="review again")
    queued = await store.get("d1")
    assert set(pending_human_reassigns(queued)) == {"a"}

    async def applied() -> bool:
        current = await store.get("d1")
        return (
            current.line("a").status is LineStatus.RUNNING
            and not pending_human_reassigns(current)
        )

    # Applied while ``b`` is still being polled, not after it settles.
    await _until(applied)
    assert not monitor.release.is_set()
    current = await store.get("d1")
    assert current.line("b").status is LineStatus.RUNNING
    assert current.line("a").failure_reason is None
    audit = _human_audit(current, "a")
    assert [(item["kind"], item["applied_by"], item["result"]) for item in audit] == [
        ("human_unblock", "drive", "applied")
    ]

    monitor.release.set()
    await asyncio.wait_for(drive, timeout=WAIT)
    after = await store.get("d1")
    assert after.line("a").status is LineStatus.PASSED
    assert after.line("b").status is LineStatus.PASSED
    assert after.state is DispatchState.APPROVAL
    # Applied once: one audit entry, one fresh review of the same work, and
    # ``a``'s author was never launched again.
    assert len(_human_audit(after, "a")) == 1
    assert len([call for call in reviewer.calls if call[0] == "a"]) == a_reviews + 1
    assert [line_id for line_id, _m, _s in collab.launcher.launches].count("a") == 1
    assert [line_id for line_id, _m, _s in collab.launcher.launches].count("b") == 1
    assert pending_human_reassigns(after) == {}
    assert not machine._abandoned
    assert resumed == []


async def test_a_reassign_queued_during_one_review_is_applied_before_the_next(
    tmp_path: Path,
) -> None:
    collab, machine, reviewer, store, service, _resumed = await _parked_a(
        tmp_path, "b", "c"
    )
    machine._config.max_parallel_lines = 2
    attempts = (await store.get("d1")).line("a").attempts
    reviewer.hold = "b"
    seen_at_c: list[tuple[LineStatus, str | None, int, bool]] = []

    async def on_call(line_id: str) -> None:
        if line_id == "c" and not seen_at_c:
            row = await store.get("d1")
            line = row.line("a")
            seen_at_c.append(
                (
                    line.status,
                    line.assigned_model,
                    line.attempts,
                    bool(pending_human_reassigns(row)),
                )
            )

    reviewer.on_call = on_call
    drive = asyncio.create_task(service._drive("d1"))
    await asyncio.wait_for(reviewer.entered.wait(), timeout=WAIT)
    row = await store.get("d1")
    # ``b`` and ``c`` both finished; ``b`` is under review, ``c`` waits for it.
    assert row.line("b").status is LineStatus.RUNNING
    assert row.line("c").status is LineStatus.RUNNING
    assert not any(call[0] == "c" for call in reviewer.calls)

    await service.reassign("d1", "a", "stepfun")
    assert set(pending_human_reassigns(await store.get("d1"))) == {"a"}
    reviewer.release.set()
    await asyncio.wait_for(drive, timeout=WAIT)

    # The reassign was applied right after ``b``'s review, before ``c``'s.
    assert seen_at_c == [(LineStatus.ASSIGNED, "stepfun", attempts + 1, False)]
    after = await store.get("d1")
    line_a = after.line("a")
    assert line_a.status is LineStatus.PASSED
    assert line_a.assigned_model == "stepfun"
    assert line_a.attempts == attempts + 1
    assert after.state is DispatchState.APPROVAL
    audit = _human_audit(after, "a")
    assert [(item["kind"], item["applied_by"], item["result"]) for item in audit] == [
        ("human_reassign", "drive", "applied")
    ]
    relaunched = [model for line_id, model, _s in collab.launcher.launches if line_id == "a"]
    assert relaunched == ["kimi", "stepfun"]
    assert pending_human_reassigns(after) == {}


async def test_a_request_for_a_running_line_is_still_refused(tmp_path: Path) -> None:
    collab, machine, _reviewer, store, service, _resumed = await _parked_a(tmp_path, "b")
    monitor = HeldMonitor("b")
    collab.monitor = monitor

    drive = asyncio.create_task(service._drive("d1"))
    await asyncio.wait_for(monitor.entered.wait(), timeout=WAIT)
    # The service refuses it up front, as before.
    with pytest.raises(ValueError, match="^drive_active$"):
        await service.reassign("d1", "b", "stepfun")
    # A request that reaches the queue anyway is refused by the drive, and
    # the running line is left exactly as it is.
    row = store.rows["d1"]
    queue_human_request(row, row.line("b"), {"model": "stepfun"})
    before = (await store.get("d1")).line("b")

    async def handled() -> bool:
        return not pending_human_reassigns(await store.get("d1"))

    await _until(handled)
    current = await store.get("d1")
    line_b = current.line("b")
    assert (line_b.status, line_b.assigned_model, line_b.attempts, line_b.current_run) == (
        before.status,
        before.assigned_model,
        before.attempts,
        before.current_run,
    )
    audit = _human_audit(current, "b")
    assert [(item["result"], item.get("reason")) for item in audit] == [
        ("refused", "drive_active")
    ]

    monitor.release.set()
    await asyncio.wait_for(drive, timeout=WAIT)
    after = await store.get("d1")
    assert after.line("b").status is LineStatus.PASSED
    assert after.line("b").assigned_model == "kimi"
    assert [line_id for line_id, _m, _s in collab.launcher.launches].count("b") == 1
    assert len(_human_audit(after, "b")) == 1
    assert not machine._abandoned
