"""N80 L4: dispatch lines an app may claim, machine side (n80-collab-protocol.md §5, §7).

A line marked ``app_claimable`` in ``CONDUCTOR_COLLAB_MODE=full`` is held for
an app: the machine starts no Worker for it, waits for the app's report and
puts a ``done`` report into G2 as the line's completion claim. A lease that
runs out parks the line for N76 R-EXIT. Outside ``full`` nothing changes.
"""

from __future__ import annotations

import copy
import time
from pathlib import Path

import pytest

from src.server.services.collab import lines as collab_lines
from src.tools.auto_dispatch import LineStatus, app_lines
from src.tools.auto_dispatch.liveness import policy as liveness_policy
from tests.unit.tools.auto_dispatch.test_parallel_lines_n31 import (
    OverlapLauncher,
    _assigned,
    _drive,
    _setup,
)

pytestmark = pytest.mark.asyncio

HEAD = "c" * 40


class Launcher(OverlapLauncher):
    def __init__(self) -> None:
        super().__init__(expect=1)
        self.prepared: list[str] = []
        self.facts_reads: list[str] = []
        self.branch: dict = {"tip": HEAD, "paths": ["src/a/x.py"], "dirty": False}

    async def prepare_app_worktree(self, line):
        self.prepared.append(line.id)
        return {"path": "/work/demo/line-" + line.id, "push_guard": True}

    async def app_branch_facts(self, line, base):
        """Conductor's own read of the branch: by default, the reported head."""
        self.facts_reads.append(line.id)
        return dict(self.branch)


def _clean_git(_line, _item, _head):
    return (("src/a/x.py",), False, True)


def _claim(row, line_id: str = "a", app_id: str = "claude-code", now: float | None = None):
    outcome = collab_lines.claim_line(
        row,
        line_id,
        app_id=app_id,
        agent="opus",
        can_write=True,
        enabled=True,
        live_leases=0,
        max_leases=1,
        now=time.time() if now is None else now,
    )
    assert outcome.ok, outcome.code
    return outcome


def _report(row, line_id: str = "a", app_id: str = "claude-code", status: str = "done"):
    outcome = collab_lines.report_line(
        row,
        line_id,
        app_id=app_id,
        status=status,
        head=HEAD,
        summary="did it",
        files=("src/a/x.py",),
        now=time.time(),
        git_facts=_clean_git,
    )
    assert outcome.ok, outcome.code
    return outcome


async def _machine(tmp_path: Path, lines, launcher=None):
    launcher = launcher or Launcher()
    store, machine, gates, service, row = await _setup(
        tmp_path, lines, launcher=launcher, parallel=1
    )
    return store, machine, gates, service, row, launcher


def _claimable(line_id: str = "a", **extra):
    line = _assigned(line_id, "kimi", (f"src/{line_id}/",), **extra)
    line.app_claimable = True
    return line


async def test_a_claimed_line_runs_no_worker_and_its_report_enters_g2(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", "full")
    store, machine, gates, service, row, launcher = await _machine(tmp_path, [_claimable()])
    sleeps: list[float] = []

    async def app_works_while_the_drive_waits(delay: float) -> None:
        # The service writes into the stored row, as an app's claim and
        # report would arrive through the collab API while the drive runs.
        sleeps.append(delay)
        stored = await store.get(row.id)
        cell = app_lines.cell(stored, "a")
        if cell is None or cell.get("state") == app_lines.OPEN:
            _claim(stored)
        elif cell.get("state") == app_lines.ACTIVE and cell.get("worktree"):
            _report(stored)
        await store.save(stored)

    machine._sleep = app_works_while_the_drive_waits
    await _drive(service, row.id)
    after = await store.get(row.id)
    line = after.line("a")
    assert [call for call in launcher.calls if call[0] == "a"] == []
    assert launcher.prepared == ["a"]
    assert sleeps and all(delay == app_lines.POLL_SECONDS for delay in sleeps)
    assert line.status is LineStatus.PASSED
    assert line.assigned_model == "app:claude-code"
    cell = app_lines.cell(after, "a")
    assert cell["state"] == app_lines.ADOPTED and cell["worktree"] == "/work/demo/line-a"
    snap = after.gates["lines"]["a"]
    machine_state = snap["machine"]
    assert machine_state["developer_provider"] == "app:claude-code"
    assert "app:claude-code" in machine_state["contributors"]
    # Review went to a native reviewer, never to the app (H4).
    assert machine_state["primary_reviewer"] and not str(
        machine_state["primary_reviewer"]
    ).startswith("app:")
    # G2 ran on the reported head (Conductor ran it; the app's word is not enough).
    assert snap["gates"]["G2"]["result"] in ("pass", "skipped")


async def test_an_expired_lease_parks_the_line_for_r_exit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", "full")
    store, machine, gates, service, row, launcher = await _machine(tmp_path, [_claimable()])
    seeded = await store.get(row.id)
    _claim(seeded, now=time.time() - 2 * app_lines.LEASE_SECONDS)
    await store.save(seeded)
    await _drive(service, row.id)
    after = await store.get(row.id)
    line = after.line("a")
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == app_lines.REASON_EXPIRED
    assert liveness_policy.DEFAULT_POLICY.group_for(line.failure_reason) == liveness_policy.R_EXIT
    assert line.assigned_model == "app:claude-code"
    assert any("Previous holder app:claude-code" in item for item in line.blockers)
    assert app_lines.cell(after, "a")["state"] == app_lines.EXPIRED
    assert [call for call in launcher.calls if call[0] == "a"] == []


async def test_a_report_other_than_done_gives_the_line_back(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", "full")
    store, machine, gates, service, row, launcher = await _machine(tmp_path, [_claimable()])
    seeded = await store.get(row.id)
    _claim(seeded)
    app_lines.cell(seeded, "a")["worktree"] = "/work/demo/line-a"
    _report(seeded, status="blocked")
    await store.save(seeded)
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert after.line("a").failure_reason == app_lines.REASON_RELEASED
    assert app_lines.cell(after, "a")["state"] == app_lines.RELEASED


async def test_nobody_claims_within_the_window_so_a_worker_takes_it(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", "full")
    monkeypatch.setenv(app_lines.WINDOW_ENV, "0")
    store, machine, gates, service, row, launcher = await _machine(tmp_path, [_claimable()])
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert [model for line_id, model in launcher.calls if line_id == "a"] == ["kimi"]
    assert after.line("a").status is LineStatus.PASSED
    assert app_lines.cell(after, "a")["state"] == app_lines.LAPSED


@pytest.mark.parametrize("mode", [None, "off", "shadow", "docs"])
async def test_outside_full_a_marked_line_drives_exactly_as_an_unmarked_one(
    tmp_path: Path, monkeypatch, mode
) -> None:
    if mode is None:
        monkeypatch.delenv("CONDUCTOR_COLLAB_MODE", raising=False)
    else:
        monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", mode)
    marked = await _machine(tmp_path / "marked", [_claimable()])
    plain = await _machine(tmp_path / "plain", [_assigned("a", "kimi", ("src/a/",))])
    reads: list[int] = []
    for store, _machine_, _gates, service, row, _launcher in (marked, plain):
        real_get = store.get

        async def counting(dispatch_id, _real=real_get):
            reads.append(1)
            return await _real(dispatch_id)

        store.get = counting
        await _drive(service, row.id)
    m_after = await marked[0].get(marked[4].id)
    p_after = await plain[0].get(plain[4].id)
    assert marked[5].calls == plain[5].calls
    assert marked[5].tasks == plain[5].tasks
    assert app_lines.GATE_KEY not in m_after.gates
    m_lines = [line.to_dict() for line in m_after.lines]
    p_lines = [line.to_dict() for line in p_after.lines]
    for raw in (*m_lines, *p_lines):
        raw.pop("current_run", None)
        raw.pop("log_tail", None)
    assert m_lines[0].pop("app_claimable") is True
    assert m_lines == p_lines
    m_gates = copy.deepcopy(m_after.gates)
    p_gates = copy.deepcopy(p_after.gates)
    assert sorted(m_gates) == sorted(p_gates)
    assert sorted(m_gates["lines"]["a"]) == sorted(p_gates["lines"]["a"])


async def test_leaving_full_expires_a_live_lease(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", "full")
    store, machine, gates, service, row, launcher = await _machine(tmp_path, [_claimable()])
    seeded = await store.get(row.id)
    _claim(seeded)
    await store.save(seeded)
    monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", "off")
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert after.line("a").failure_reason == app_lines.REASON_EXPIRED
    assert app_lines.cell(after, "a")["state"] == app_lines.EXPIRED


async def test_the_line_side_pack_shares_the_m2c_generator(tmp_path: Path, monkeypatch) -> None:
    from src.tools.auto_dispatch import handoff_pack

    monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", "full")
    _store, _machine_, _gates, _service, row, _launcher = await _machine(tmp_path, [_claimable()])
    _claim(row)
    pack = app_lines.line_pack(row, row.line("a"), app_lines.cell(row, "a"))
    assert handoff_pack.pack_problem(pack) is None
    assert pack["producer"] == "conductor"
    assert pack["system"]["conductor"]["predecessor"] == "app:claude-code"
    assert pack["system"]["conductor"]["predecessor_kind"] == "app"
    assert set(pack["system"]) >= set(handoff_pack.SYSTEM_FIELDS)


@pytest.fixture(autouse=True)
def _line_claims_routed(monkeypatch):
    """Line claims need ``modes.LINE_CLAIMS_ENABLED`` as well as ``full`` (review fix 7)."""
    from src.collab import modes

    monkeypatch.setattr(modes, "LINE_CLAIMS_ENABLED", True)


# -- review fixes (L4 FIX round) ---------------------------------------------


async def test_a_marked_line_behind_a_blocked_dependency_parks_instead_of_polling(
    tmp_path: Path, monkeypatch
) -> None:
    """Fix 5: a held line must never keep the drive from its blocked park."""
    monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", "full")
    a = _assigned("a", "kimi", ("src/a/",))
    a.status = LineStatus.BLOCKED
    a.failure_reason = "replan_exhausted"
    b = _claimable("b", depends_on=("a",))
    store, machine, gates, service, row, launcher = await _machine(tmp_path, [a, b])
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) > 20:
            raise RuntimeError("the drive polled for an app instead of parking")

    machine._sleep = sleep
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert app_lines.POLL_SECONDS not in sleeps
    assert after.line("b").status is LineStatus.ASSIGNED
    assert app_lines.cell(after, "b") is None


async def test_without_line_claim_routing_full_mode_holds_nothing(tmp_path: Path, monkeypatch) -> None:
    """Fix 7: no app can reach a line claim while LINE_CLAIMS_ENABLED is off."""
    from src.collab import modes
    from src.tools.auto_dispatch import planner_prompt

    monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", "full")
    monkeypatch.setattr(modes, "LINE_CLAIMS_ENABLED", False)
    assert not app_lines.lines_enabled()
    store, machine, gates, service, row, launcher = await _machine(tmp_path, [_claimable()])
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert [model for line_id, model in launcher.calls if line_id == "a"] == ["kimi"]
    assert app_lines.GATE_KEY not in after.gates
    assert not planner_prompt.app_claims_enabled()


def _race(service, store, row_id: str, when_state: str, inject) -> list[bool]:
    """Write ``inject`` into the stored row just before the drive saves ``when_state``.

    That is the service's claim or heartbeat landing after the drive decided
    a step from the cells it had, and before that step's write.
    """
    real = service.drive_mutation
    fired: list[bool] = []

    async def racing(dispatch_id, epoch0, fn):
        if not fired:
            try:
                peek = fn(copy.deepcopy(store.rows[dispatch_id]))
            except Exception:  # noqa: BLE001 - a guarded step refused the peek
                peek = None
            item = app_lines.cell(peek, "a") if peek is not None else None
            if item is not None and item.get("state") == when_state:
                fired.append(True)
                stored = store.rows[dispatch_id]
                inject(stored)
        return await real(dispatch_id, epoch0, fn)

    service.drive_mutation = racing
    return fired


def _app_completes(store, row_id: str):
    async def sleep(_delay: float) -> None:
        stored = await store.get(row_id)
        item = app_lines.cell(stored, "a")
        if item and item.get("state") == app_lines.ACTIVE and item.get("worktree"):
            _report(stored)
            await store.save(stored)

    return sleep


async def test_a_claim_written_while_the_drive_lapses_the_line_wins(
    tmp_path: Path, monkeypatch
) -> None:
    """Fix 6: never two authors on one branch (lapse vs a concurrent claim)."""
    monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", "full")
    store, machine, gates, service, row, launcher = await _machine(tmp_path, [_claimable()])
    seeded = await store.get(row.id)
    # The claim window ran out just now: the drive decides to lapse the line.
    app_lines.mark(seeded, "a", app_lines.OPEN, now=time.time() - app_lines.DEFAULT_WINDOW_SECONDS - 1)
    await store.save(seeded)

    def claim_at_the_deadline(stored) -> None:
        # The service accepted the claim a moment before the deadline.
        app_lines.claim(stored, stored.line("a"), app_id="claude-code", now=time.time())

    fired = _race(service, store, row.id, app_lines.LAPSED, claim_at_the_deadline)
    machine._sleep = _app_completes(store, row.id)
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert fired
    assert [call for call in launcher.calls if call[0] == "a"] == []
    assert after.line("a").assigned_model == "app:claude-code"
    assert after.line("a").status is LineStatus.PASSED


async def test_a_heartbeat_written_while_the_drive_expires_the_lease_wins(
    tmp_path: Path, monkeypatch
) -> None:
    """Fix 6: a late heartbeat is not overwritten by ``expired``."""
    monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", "full")
    store, machine, gates, service, row, launcher = await _machine(tmp_path, [_claimable()])
    seeded = await store.get(row.id)
    _claim(seeded, now=time.time() - 2 * app_lines.LEASE_SECONDS)
    app_lines.cell(seeded, "a")["worktree"] = "/work/demo/line-a"
    await store.save(seeded)

    def heartbeat(stored) -> None:
        item = app_lines.cell(stored, "a")
        item["expires_at"] = time.time() + app_lines.LEASE_SECONDS
        item["max_lease_until"] = time.time() + app_lines.MAX_LEASE_SECONDS

    fired = _race(service, store, row.id, app_lines.EXPIRED, heartbeat)
    machine._sleep = _app_completes(store, row.id)
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert fired
    assert after.line("a").failure_reason != app_lines.REASON_EXPIRED
    assert after.line("a").status is LineStatus.PASSED


@pytest.mark.parametrize(
    ("branch", "why"),
    [
        ({"tip": "d" * 40, "paths": ["src/a/x.py"], "dirty": False}, "tip_moved_after_report"),
        ({"tip": HEAD, "paths": ["src/a/x.py", "src/server/app/setup.py"], "dirty": False}, "out_of_scope"),
        ({"tip": HEAD, "paths": ["src/a/x.py"], "dirty": True}, "worktree_dirty"),
        ({}, "branch_facts_unknown"),
    ],
)
async def test_a_report_enters_g2_only_for_the_branch_conductor_reads(
    tmp_path: Path, monkeypatch, branch, why
) -> None:
    """Fix 8: commits after the report, or outside the scope, never reach G2."""
    monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", "full")
    launcher = Launcher()
    launcher.branch = branch
    store, machine, gates, service, row, launcher = await _machine(
        tmp_path, [_claimable()], launcher=launcher
    )
    seeded = await store.get(row.id)
    _claim(seeded)
    app_lines.cell(seeded, "a")["worktree"] = "/work/demo/line-a"
    _report(seeded)
    await store.save(seeded)
    await _drive(service, row.id)
    after = await store.get(row.id)
    line = after.line("a")
    assert launcher.facts_reads == ["a"]
    assert line.failure_reason == app_lines.REASON_RELEASED
    cell = app_lines.cell(after, "a")
    assert cell["state"] == app_lines.RELEASED and cell["end_reason"] == why
    assert (after.gates["lines"]["a"].get("gates") or {}).get("G2", {}).get("result") in (None, "", "pending")


async def test_the_claimed_worktree_records_its_push_guard(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", "full")
    store, machine, gates, service, row, launcher = await _machine(tmp_path, [_claimable()])
    seeded = await store.get(row.id)
    _claim(seeded)
    await store.save(seeded)
    machine._sleep = _app_completes(store, row.id)
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert app_lines.cell(after, "a")["push_guard"] is True


async def test_a_refused_assign_gives_the_line_back(tmp_path: Path, monkeypatch) -> None:
    """Fix 10: the DAG_READY ``assign`` to the app must be accepted to adopt."""
    from src.tools.auto_dispatch.machine import DispatchMachine

    monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", "full")
    real = DispatchMachine._advance

    def refusing(self, dispatch, line, kind, **fields):
        if kind == "assign" and str(fields.get("provider", "")).startswith("app:"):
            fields = {**fields, "provider": ""}  # Stage-Gate refuses: provider_required
        return real(self, dispatch, line, kind, **fields)

    monkeypatch.setattr(DispatchMachine, "_advance", refusing)
    store, machine, gates, service, row, launcher = await _machine(tmp_path, [_claimable()])
    seeded = await store.get(row.id)
    _claim(seeded)
    app_lines.cell(seeded, "a")["worktree"] = "/work/demo/line-a"
    _report(seeded)
    await store.save(seeded)
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert after.line("a").failure_reason == app_lines.REASON_RELEASED
    assert app_lines.cell(after, "a")["end_reason"] == "stage_gate_refused"
