"""N86: a human reassign sticks; no automatic fallback moves the line elsewhere.

N80 (fb82909c) 2026-11-13: ``dispatch reassign line-0 kimi`` answered ok,
status showed kimi ASSIGNED, and ten minutes later the line was RUNNING on
deepseek (attempt 2). The override path is the drive's move off a limited
provider (``DispatchMachine._move_off_limited_provider``): kimi's run failed
rate-limited/quota and the selector seated another developer. A line pinned
by a human reassign now gets its pinned worker back or nobody, so the drive
keeps the same-provider retry policy (or parks) instead of switching.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.server.services.autonomy.roles import RolePolicy
from src.tools.auto_dispatch import human_pin
from src.tools.auto_dispatch.protocols import ModelRole
from src.tools.auto_dispatch.selection import QuotaAwareModelSelector
from src.tools.auto_dispatch.types import Dispatch, Line, LineStatus
from tests.unit.tools.auto_dispatch.test_parallel_lines_n31 import (
    FailFirst,
    _assigned,
    _drive,
    _limited,
    _setup,
)

pytestmark = pytest.mark.asyncio

POLICY = RolePolicy(developer_pool=("kimi", "glm", "stepfun"))


@pytest.fixture
def enforce(monkeypatch):
    monkeypatch.setenv(human_pin.HUMAN_PIN_MODE_ENV, "enforce")


def _row(line: Line) -> Dispatch:
    return Dispatch(id="d", goal="g", user_id="u", created_at="now", lines=[line])


# -- the override path, end to end ----------------------------------------------


async def _pinned_drive(tmp_path: Path):
    launcher = FailFirst({"a": _limited})
    store, machine, _gates, service, row = await _setup(
        tmp_path, [_assigned("a", "kimi", ("src/a/",))], launcher=launcher, parallel=1
    )
    machine._c.selector = QuotaAwareModelSelector(policy=POLICY)
    stored = await store.get(row.id)
    assert human_pin.record_pin(stored, stored.line("a"), "kimi")
    await store.save(stored)
    await _drive(service, row.id)
    return store, launcher, row


async def test_a_pinned_line_is_not_moved_off_its_limited_worker(tmp_path, enforce) -> None:
    store, launcher, row = await _pinned_drive(tmp_path)
    after = await store.get(row.id)
    models = [model for line_id, model in launcher.calls if line_id == "a"]
    # The old code moved the line to glm/stepfun here (N25(a)); a person
    # picked kimi, so kimi is retried after its backoff instead.
    assert models == ["kimi", "kimi"]
    assert after.line("a").assigned_model == "kimi"
    assert after.line("a").status is LineStatus.PASSED
    assert human_pin.pinned_model(after, "a") == "kimi"


async def test_shadow_mode_only_logs_and_keeps_the_old_move(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(human_pin.HUMAN_PIN_MODE_ENV, "shadow")
    store, launcher, row = await _pinned_drive(tmp_path)
    models = [model for line_id, model in launcher.calls if line_id == "a"]
    assert models[0] == "kimi" and models[1] != "kimi"
    # The pin is still recorded, so promoting to enforce needs no new data.
    after = await store.get(row.id)
    assert human_pin.pinned_model(after, "a") == "kimi"


# -- the selector ----------------------------------------------------------------


async def test_the_selector_returns_the_pin_or_nobody(enforce) -> None:
    line = Line(id="L", name="n", task="t", write_scope=("src/a",), assigned_model="kimi")
    row = _row(line)
    human_pin.record_pin(row, line, "kimi")
    selector = QuotaAwareModelSelector(policy=POLICY)
    assert await selector.select(line, [], role=ModelRole.WORK, dispatch=row) == "kimi"
    assert (
        await selector.select(line, [], role=ModelRole.WORK, exclude=("kimi",), dispatch=row)
        is None
    )
    assert selector.work_refusal == human_pin.PINNED_WORKER_UNUSABLE
    # A review pick never takes the work pin's branch.
    selector.work_refusal = None
    await selector.select(line, [], role=ModelRole.REVIEW, dispatch=row)
    assert selector.work_refusal is None


async def test_an_unpinned_line_ranks_as_before(enforce) -> None:
    line = Line(id="L", name="n", task="t", write_scope=("src/a",), assigned_model="kimi")
    selector = QuotaAwareModelSelector(policy=POLICY)
    picked = await selector.select(
        line, [], role=ModelRole.WORK, exclude=("kimi",), dispatch=_row(line)
    )
    assert picked in ("glm", "stepfun")


async def test_off_mode_records_no_pin(monkeypatch) -> None:
    monkeypatch.setenv(human_pin.HUMAN_PIN_MODE_ENV, "off")
    line = Line(id="L", name="n", task="t")
    row = _row(line)
    assert human_pin.record_pin(row, line, "kimi") is False
    assert human_pin.pinned_model(row, "L") is None


async def test_a_queued_pin_counts_only_once_the_drive_applied_it(enforce) -> None:
    line = Line(id="L", name="n", task="t")
    row = _row(line)
    human_pin.record_pin(row, line, "kimi", queued=True)
    assert human_pin.pinned_model(row, "L") is None
    row.gates.setdefault("scheduler", {})["audit"] = [
        {"kind": "human_reassign", "line_id": "L", "model": "kimi", "result": "refused"}
    ]
    assert human_pin.pinned_model(row, "L") is None
    row.gates["scheduler"]["audit"].append(
        {"kind": "human_reassign", "line_id": "L", "model": "kimi", "result": "applied"}
    )
    assert human_pin.pinned_model(row, "L") == "kimi"


async def test_park_pinned_unusable_parks_with_a_reason(enforce) -> None:
    line = Line(id="L", name="n", task="t", status=LineStatus.ASSIGNED, current_run="r1")
    row = _row(line)
    assert human_pin.park_pinned_unusable(row, line, "quota_exhausted") is False
    human_pin.record_pin(row, line, "kimi")
    assert human_pin.park_pinned_unusable(row, line, "quota_exhausted") is True
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == human_pin.PINNED_WORKER_UNUSABLE
    assert line.current_run is None
    assert "kimi" in line.blockers[0] and "quota_exhausted" in line.blockers[0]
    assert row.gates["scheduler"]["audit"][-1]["kind"] == "human_pin_parked"


async def test_the_park_reason_is_a_human_reason() -> None:
    from src.tools.auto_dispatch.liveness import consistency, policy

    assert policy.default_policy().group_for(human_pin.PINNED_WORKER_UNUSABLE) == policy.HUMAN
    assert human_pin.PINNED_WORKER_UNUSABLE in consistency.KNOWN_REASONS


# -- reassign-time refusal -------------------------------------------------------


class Headroom:
    def __init__(self, left: dict[str, float]) -> None:
        self.left = left
        self.reads: list[str] = []

    async def remaining(self, model: str) -> float:
        self.reads.append(model)
        return self.left.get(model, 1.0)


async def test_a_skipped_worker_is_refused_at_reassign_time(enforce) -> None:
    row = _row(Line(id="L", name="n", task="t"))
    row.gates["provider_skips"] = {"kimi": {"until": 2_000.0, "reason": "quota_exhausted"}}
    reason = await human_pin.reassign_headroom_refusal(row, "kimi", None, now=1_000.0)
    assert reason and reason.startswith(human_pin.WORKER_SKIPPED)
    assert "quota_exhausted" in reason
    # Past the reset the worker is fine again.
    assert await human_pin.reassign_headroom_refusal(row, "kimi", None, now=3_000.0) is None


async def test_an_exhausted_worker_is_refused_and_a_factory_is_lazy(enforce) -> None:
    row = _row(Line(id="L", name="n", task="t"))
    port = Headroom({"kimi": 0.0})
    built: list[int] = []

    def factory():
        built.append(1)
        return port

    reason = await human_pin.reassign_headroom_refusal(row, "kimi", factory)
    assert reason and reason.startswith(human_pin.WORKER_NO_HEADROOM)
    assert await human_pin.reassign_headroom_refusal(row, "glm", factory) is None
    assert built == [1, 1]


async def test_shadow_audits_the_refusal_and_lets_the_reassign_through(monkeypatch) -> None:
    monkeypatch.setenv(human_pin.HUMAN_PIN_MODE_ENV, "shadow")
    row = _row(Line(id="L", name="n", task="t"))
    assert await human_pin.reassign_headroom_refusal(row, "kimi", Headroom({"kimi": 0.0})) is None
    audit = row.gates["scheduler"]["audit"]
    assert audit[-1]["kind"] == "human_pin_shadow_refusal"


async def test_off_never_reads_the_quota(monkeypatch) -> None:
    monkeypatch.setenv(human_pin.HUMAN_PIN_MODE_ENV, "off")
    port = Headroom({"kimi": 0.0})
    row = _row(Line(id="L", name="n", task="t"))
    assert await human_pin.reassign_headroom_refusal(row, "kimi", port) is None
    assert port.reads == []
