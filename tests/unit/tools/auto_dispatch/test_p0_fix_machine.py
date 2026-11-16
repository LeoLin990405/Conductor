"""P0 review fixes on the machine side.

1. Silent worker enforce: a clean ``empty_final_answer`` run whose branch head
   moved since launch did its work; it is recorded, never reassigned. Unknown
   movement is recorded only too.
2. Review dedup: the key carries a digest of the notes and ruling material the
   reviewer is sent, so a ruling reissue of the same head is reviewed again.
3. ``DispatchService.start`` dedup: the check and the create are one step.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from src.server.services.autonomy.stage_gate import Stage
from src.tools.auto_dispatch import InMemoryDispatchStore, LineStatus, p0_hooks
from src.tools.auto_dispatch.dedup import MODE_ENV as DEDUP_MODE_ENV
from src.tools.auto_dispatch.dev import RecordingLauncher
from src.tools.auto_dispatch.execution import LaunchResult
from src.tools.auto_dispatch.machine import (
    HEAD_UNDER_REVIEW,
    RULING_REISSUE_HEAD,
    _load_state,
    _store_state,
)
from src.tools.auto_dispatch.rulings import RULING_CHECK
from src.tools.auto_dispatch.silent_worker import (
    KIND_EMPTY_FINAL,
    MINUTES_ENV,
    MODE_ENV as SILENT_MODE_ENV,
    RECORD_KEY,
    SilentFinding,
)
from src.tools.auto_dispatch.types import Verdict

from .n76_fakes import make_dispatch, make_gates, make_line, make_machine, stored
from .test_machine import _collaborators, _dispatch, _machine
from .test_p0_dedup_wiring import _Service
from .test_p0_silent_wiring import UnmovedLauncher, _SilentOnce


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.delenv(SILENT_MODE_ENV, raising=False)
    monkeypatch.delenv(MINUTES_ENV, raising=False)
    monkeypatch.delenv(DEDUP_MODE_ENV, raising=False)
    monkeypatch.setenv("CONDUCTOR_DISPATCH_INBOX_DIR", str(tmp_path / "inbox"))


# ------------------------------------------------- 1. silent + head moved


def _empty_final() -> SilentFinding:
    return SilentFinding(kind=KIND_EMPTY_FINAL, elapsed_seconds=5.0, threshold_seconds=900.0)


class _HeadLauncher(RecordingLauncher):
    """A finished run that reports its branch head before and after."""

    def __init__(self, start: str, head: str) -> None:
        super().__init__()
        self.start, self.head = start, head

    async def launch_worker(self, line, model, skills, **_kwargs) -> LaunchResult:
        run_id = await self.launch(line, model, skills)
        return LaunchResult(
            run_id=run_id, session_id=None, output="", start_head=self.start, head=self.head
        )


def _cell(result) -> dict:
    return (result.gates.get(p0_hooks.UNGATED_SILENT_KEY) or {}).get("line-0") or {}


@pytest.mark.asyncio
async def test_enforce_clean_exit_with_commits_and_empty_log_is_not_reassigned(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(SILENT_MODE_ENV, "enforce")
    monitor = _SilentOnce(_empty_final(), complete=True)
    collaborators = _collaborators(
        tmp_path, monitor=monitor, launcher=_HeadLauncher("a1a1a1", "b2b2b2")
    )
    machine, _recorder = _machine(collaborators)
    result = await machine.run(await collaborators.store.create(_dispatch()))

    assert monitor.silenced, "the scripted silent run was never polled"
    line0 = [m for line_id, m, _s in collaborators.launcher.launches if line_id == "line-0"]
    assert len(line0) == 1
    line = result.line("line-0")
    assert line.attempts == 0 and line.status is LineStatus.PASSED
    assert monitor.cancelled == []
    entry = _cell(result)[RECORD_KEY]["runs"][monitor.silenced[0]]
    assert entry["mode"] == "enforce"
    assert entry[p0_hooks.SILENT_ENFORCE_SKIPPED] == p0_hooks.HEAD_MOVED


@pytest.mark.asyncio
async def test_enforce_empty_log_without_commits_is_reassigned(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(SILENT_MODE_ENV, "enforce")
    monitor = _SilentOnce(_empty_final(), complete=True)
    # The launcher records where each run left the branch: unmoved.
    launcher = UnmovedLauncher()
    collaborators = _collaborators(tmp_path, monitor=monitor, launcher=launcher)
    machine, _recorder = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())
    launcher.rows.append(dispatch)
    result = await machine.run(dispatch)

    line0 = [m for line_id, m, _s in collaborators.launcher.launches if line_id == "line-0"]
    assert len(line0) == 2 and line0[0] != line0[1]
    assert result.line("line-0").attempts == 1
    assert monitor.cancelled == monitor.silenced
    entry = _cell(result)[RECORD_KEY]["runs"][monitor.silenced[0]]
    assert p0_hooks.SILENT_ENFORCE_SKIPPED not in entry


@pytest.mark.asyncio
async def test_enforce_empty_log_with_unknown_head_is_recorded_only(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(SILENT_MODE_ENV, "enforce")
    monitor = _SilentOnce(_empty_final(), complete=True)
    collaborators = _collaborators(tmp_path, monitor=monitor)
    machine, _recorder = _machine(collaborators)
    result = await machine.run(await collaborators.store.create(_dispatch()))

    assert len(collaborators.launcher.launches) == 3
    assert result.line("line-0").attempts == 0
    entry = _cell(result)[RECORD_KEY]["runs"][monitor.silenced[0]]
    assert entry[p0_hooks.SILENT_ENFORCE_SKIPPED] == p0_hooks.HEAD_UNKNOWN


def test_run_head_moved_reads_the_attempts_newest_record() -> None:
    runs = [
        {"author": "kimi", "attempt": 0, "start": "a", "head": "a"},
        {"author": "kimi", "attempt": 0, "start": "a", "head": "b"},
        {"author": "codex", "attempt": 1, "start": "b", "head": "b"},
        {"author": "glm", "attempt": 2, "start": "", "head": "c"},
    ]
    assert p0_hooks.run_head_moved(runs, attempt=0, author="kimi") is True
    assert p0_hooks.run_head_moved(runs, attempt=1, author="codex") is False
    assert p0_hooks.run_head_moved(runs, attempt=1, author="kimi") is None
    assert p0_hooks.run_head_moved(runs, attempt=2, author="glm") is None
    assert p0_hooks.run_head_moved([], attempt=0, author="kimi") is None


# ------------------------------------------------- 2. review dedup digest


class _Counting:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def review_line(self, line_id: str, model: str, *, notes: str = "") -> Verdict:
        self.calls.append((line_id, model, notes))
        return Verdict(passed=False, blockers=["RULING M1: not met"])


async def _review_setup(tmp_path):
    gates = make_gates(tmp_path)
    reviewer = _Counting()
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
async def test_enforce_a_ruling_reissue_of_the_same_head_is_reviewed_again(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(DEDUP_MODE_ENV, "enforce")
    machine, reviewer, dispatch, line, snap = await _review_setup(tmp_path)
    await machine._review_line(line, "codex", dispatch)

    # The same head comes back with the ruling reissued and a new check.
    snap[RULING_REISSUE_HEAD] = "c0ffee"
    snap[RULING_CHECK] = {"complete": True, "missing": []}
    await machine._review_line(line, "codex", dispatch)
    assert len(reviewer.calls) == 2

    # Nothing changed since: that verdict is reused.
    await machine._review_line(line, "codex", dispatch)
    assert len(reviewer.calls) == 2


def test_review_head_key_digests_the_material_sent() -> None:
    assert p0_hooks.review_head_key("c0ffee") == "c0ffee"
    assert p0_hooks.review_head_key("", "notes") == ""
    plain = p0_hooks.review_head_key("c0ffee", "", {"other": 1})
    one = p0_hooks.review_head_key("c0ffee", "Unconfirmed rulings: M1")
    two = p0_hooks.review_head_key("c0ffee", "Unconfirmed rulings: M2")
    checked = p0_hooks.review_head_key("c0ffee", "", {RULING_CHECK: {"complete": False}})
    assert plain == "c0ffee"
    assert len({one, two, checked, plain}) == 4
    assert one == p0_hooks.review_head_key("c0ffee", "Unconfirmed rulings: M1")


# ------------------------------------------------- 3. atomic start dedup


class _YieldingStore(InMemoryDispatchStore):
    """Yields to the loop inside list and create, as a database store does."""

    async def list(self, **kwargs):
        await asyncio.sleep(0)
        rows = await super().list(**kwargs)
        await asyncio.sleep(0)
        return rows

    async def create(self, dispatch):
        await asyncio.sleep(0)
        return await super().create(dispatch)


@pytest.mark.asyncio
async def test_enforce_concurrent_starts_with_the_same_key_create_one_dispatch(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(DEDUP_MODE_ENV, "enforce")
    harness = _Service(tmp_path, monkeypatch)
    store = _YieldingStore()
    harness.service._store = store

    first, second = await asyncio.gather(
        harness.service.start("fix issue 59", "u1", source_ref="#59"),
        harness.service.start("fix issue 59", "u1", source_ref="issue:59"),
    )

    assert first.id == second.id
    assert len(store.rows) == 1


@pytest.mark.asyncio
async def test_start_lock_uses_the_store_lock_and_skips_without_a_ref(monkeypatch) -> None:
    monkeypatch.setenv(DEDUP_MODE_ENV, "enforce")
    entered: list[str] = []

    class _Store:
        def path_claim_lock(self):
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def _held():
                entered.append("db")
                yield

            return _held()

    async with p0_hooks.start_lock("u1", "#59", _Store()):
        pass
    async with p0_hooks.start_lock("u1", None, _Store()):
        pass
    monkeypatch.setenv(DEDUP_MODE_ENV, "off")
    async with p0_hooks.start_lock("u1", "#59", _Store()):
        pass
    assert entered == ["db"]
