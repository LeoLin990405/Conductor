"""N31 parallel lines and N25a provider skips.

- N31: independent ready lines of one dispatch run at the same time, up to
  ``max_parallel_lines``; the same provider beyond its capacity, or
  overlapping write scopes, never run together; a dependency still waits.
  Each line gets its own reserved run, and a restart in the middle resumes
  each line once.
- N25a: a provider that is rate limited or out of quota is skipped for new
  launches until its reset; a line that has not run yet goes to the next
  eligible developer, and the skip is recorded for ``dispatch status``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.llms.native_cli.failure import FailureClass
from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus, Verdict
from src.tools.auto_dispatch.execution import LaunchResult, WorkerLaunchFailure
from src.tools.auto_dispatch.machine import (
    PROVIDER_SKIPS,
    REVIEW_RETRY,
    MachineConfig,
    _scopes_overlap,
)
from tests.unit.server.services.test_dispatch_r2_stale_result import Copies, _service
from tests.unit.tools.auto_dispatch.test_gate_recovery import _bundle, _line

pytestmark = pytest.mark.asyncio

BASE = "b" * 40
HEAD = "1" * 40


def _handoff(*_args: object, **_kwargs: object) -> dict[str, object]:
    return {
        "branch": "feature/x",
        "commit": "abc123",
        "files": ("x.py",),
        "test_command": "pytest",
        "results": "ok",
        "known_limitations": "none",
    }


class OverlapLauncher:
    """Each worker waits (bounded) until ``expect`` workers are in at once."""

    def __init__(self, expect: int = 2, *, script: dict[str, list[object]] | None = None) -> None:
        self.expect = expect
        self.inside = 0
        self.peak = 0
        self.calls: list[tuple[str, str]] = []
        self.tasks: list[str] = []
        self.run_ids: list[str] = []
        self.script = {key: list(value) for key, value in (script or {}).items()}
        self._crowd = asyncio.Event()

    async def launch(self, line, model, skills):
        raise AssertionError("the recovery machine uses launch_worker")

    async def launch_worker(self, line, model, skills, *, session_id, session_mode, handoff_path, **_kw):
        self.calls.append((line.id, model))
        self.tasks.append(line.task)
        run_id = line.current_run
        assert run_id, "a worker never starts without its reserved run"
        self.run_ids.append(run_id)
        steps = self.script.get(line.id) or []
        step = steps.pop(0) if steps else None
        if isinstance(step, BaseException):
            raise step
        self.inside += 1
        self.peak = max(self.peak, self.inside)
        if self.inside >= self.expect:
            self._crowd.set()
        try:
            await asyncio.wait_for(self._crowd.wait(), timeout=0.5)
        except TimeoutError:
            pass
        finally:
            self.inside -= 1
        return LaunchResult(run_id, session_id, "committed", start_head=BASE, head=HEAD)


async def _setup(tmp_path: Path, lines, *, launcher, parallel: int = 2, reviewer=None):
    store = Copies()
    overrides = {"store": store, "launcher": launcher}
    if reviewer is not None:
        overrides["reviewer"] = reviewer
    _collab, machine, _commands, gates = _bundle(tmp_path, [], **overrides)
    machine._config = MachineConfig(max_parallel_lines=parallel)
    gates.handoff_facts = _handoff
    gates._revisions = lambda _line: None
    row = Dispatch(
        id="par",
        goal="test",
        user_id="u",
        created_at="now",
        state=DispatchState.ASSIGNED,
        lines=lines,
    )
    gates.prepare(row)
    await store.create(row)
    service = _service(tmp_path, store, lambda _dispatch: machine)
    service._gate_runner = lambda: gates

    async def no_schedule(dispatch: Dispatch) -> Dispatch:
        return dispatch

    service._resume = no_schedule
    return store, machine, gates, service, row


def _assigned(line_id: str, model: str, scope: tuple[str, ...], **extra):
    return _line(
        line_id,
        status=LineStatus.ASSIGNED,
        assigned_model=model,
        base_commit=BASE,
        write_scope=scope,
        **extra,
    )


async def _drive(service, dispatch_id: str) -> None:
    await asyncio.wait_for(service._drive(dispatch_id), timeout=20)


# -- N31 ------------------------------------------------------------------------


async def test_independent_lines_run_at_the_same_time(tmp_path: Path) -> None:
    launcher = OverlapLauncher(expect=2)
    store, _machine, _gates, service, row = await _setup(
        tmp_path,
        [_assigned("a", "kimi", ("src/a/",)), _assigned("b", "glm", ("src/b/",))],
        launcher=launcher,
    )
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert launcher.peak == 2
    assert {line.status for line in after.lines} == {LineStatus.PASSED}
    # Each line had its own reserved run; nothing was shared or launched twice.
    assert len(launcher.run_ids) == len(set(launcher.run_ids)) == 2
    assert after.gates["scheduler"]["parallel"]["limit"] == 2


async def test_max_parallel_one_keeps_the_serial_invariant(tmp_path: Path) -> None:
    launcher = OverlapLauncher(expect=2)
    store, _machine, _gates, service, row = await _setup(
        tmp_path,
        [_assigned("a", "kimi", ("src/a/",)), _assigned("b", "glm", ("src/b/",))],
        launcher=launcher,
        parallel=1,
    )
    await _drive(service, row.id)
    assert launcher.peak == 1
    assert {line.status for line in (await store.get(row.id)).lines} == {LineStatus.PASSED}


@pytest.mark.parametrize(
    ("models", "scopes"),
    [
        (("kimi", "kimi"), (("src/a/",), ("src/b/",))),  # same provider, capacity 1
        (("kimi", "glm"), (("src/shared/",), ("src/shared/x.py",))),  # overlapping scope
        (("kimi", "glm"), ((), ("src/b/",))),  # an empty scope may write anywhere
    ],
)
async def test_conflicting_lines_do_not_run_together(tmp_path: Path, models, scopes) -> None:
    launcher = OverlapLauncher(expect=2)
    store, _machine, _gates, service, row = await _setup(
        tmp_path,
        [
            _assigned("a", models[0], scopes[0]),
            _assigned("b", models[1], scopes[1]),
        ],
        launcher=launcher,
    )
    await _drive(service, row.id)
    assert launcher.peak == 1
    assert {line.status for line in (await store.get(row.id)).lines} == {LineStatus.PASSED}


async def test_a_dependency_still_waits(tmp_path: Path) -> None:
    launcher = OverlapLauncher(expect=2)
    store, _machine, _gates, service, row = await _setup(
        tmp_path,
        [
            _assigned("a", "kimi", ("src/a/",)),
            _assigned("b", "glm", ("src/b/",), depends_on=("a",)),
        ],
        launcher=launcher,
    )
    await _drive(service, row.id)
    assert launcher.peak == 1
    assert [call[0] for call in launcher.calls] == ["a", "b"]


async def test_a_review_retry_on_one_line_does_not_hold_the_other(tmp_path: Path) -> None:
    class Down:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def review_line(self, line_id: str, model: str) -> Verdict:
            self.calls.append(line_id)
            if line_id == "a" and self.calls.count("a") <= 2:
                raise RuntimeError("reviewer down")
            return Verdict.ok("ok")

    reviewer = Down()
    launcher = OverlapLauncher(expect=2)
    store, _machine, _gates, service, row = await _setup(
        tmp_path,
        [_assigned("a", "kimi", ("src/a/",)), _assigned("b", "glm", ("src/b/",))],
        launcher=launcher,
        reviewer=reviewer,
    )
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert {line.status for line in after.lines} == {LineStatus.PASSED}
    assert reviewer.calls.count("a") == 3
    assert REVIEW_RETRY not in after.gates["lines"]["a"]


async def test_a_restart_in_the_middle_resumes_each_line_once(tmp_path: Path) -> None:
    """Two lines reserved and in ``implementing`` when the process died."""
    launcher = OverlapLauncher(expect=2)
    store, machine, gates, service, row = await _setup(
        tmp_path,
        [_assigned("a", "kimi", ("src/a/",)), _assigned("b", "glm", ("src/b/",))],
        launcher=launcher,
    )

    def mid_flight(r: Dispatch) -> Dispatch:
        for line in r.lines:
            snap = gates.line_snapshot(r, line)
            machine_state = dict(snap.get("machine") or {})
            machine_state.update(
                {
                    "task_id": line.id,
                    "stage": "implementing",
                    "author_provider": line.assigned_model,
                    "developer_provider": line.assigned_model,
                }
            )
            snap["machine"] = machine_state
            line.current_run = f"par:{line.id}:0:died"
        r.state = DispatchState.LAUNCHING
        return r

    await service.apply_mutation(row.id, mid_flight)
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert {line.status for line in after.lines} == {LineStatus.PASSED}
    assert sorted(call[0] for call in launcher.calls) == ["a", "b"]
    assert launcher.peak == 2


def test_scope_overlap_rules() -> None:
    assert _scopes_overlap(("src/a/",), ("src/a/b.py",))
    assert _scopes_overlap(("src/**/*.py",), ("src/x/y.py",))
    assert not _scopes_overlap(("src/a/",), ("src/ab/",))
    assert not _scopes_overlap(("web/",), ("src/",))
    assert _scopes_overlap((), ("src/",))


# -- N25a -----------------------------------------------------------------------


def _limited(run_id: str, *, failure_class=FailureClass.RATE_LIMITED, reason="worker_rate_limited", retry_after=120):
    return WorkerLaunchFailure(
        run_id=run_id,
        exit_code=75,
        output="rate limited",
        session_id=None,
        retry_after_seconds=retry_after,
        failure_class=failure_class,
        reason_code=reason,
    )


class FailFirst(OverlapLauncher):
    def __init__(self, failure_for: dict[str, object]) -> None:
        super().__init__(expect=1)
        self.failure_for = dict(failure_for)

    async def launch_worker(self, line, model, skills, **kwargs):
        failure = self.failure_for.pop(line.id, None)
        if failure is not None:
            self.calls.append((line.id, model))
            raise failure(line.current_run) if callable(failure) else failure
        return await super().launch_worker(line, model, skills, **kwargs)


async def test_a_rate_limited_provider_is_skipped_for_new_launches(tmp_path: Path) -> None:
    launcher = FailFirst({"a": _limited})
    store, machine, _gates, service, row = await _setup(
        tmp_path,
        [_assigned("a", "glm", ("src/a/",)), _assigned("b", "glm", ("src/b/",), depends_on=("a",))],
        launcher=launcher,
        parallel=1,
    )
    await _drive(service, row.id)
    after = await store.get(row.id)
    skip = after.gates[PROVIDER_SKIPS]["glm"]
    assert skip["reason"] == "rate_limited"
    assert skip["source"] == "retry_after"
    # a moved off glm to the next developer (N25(a)); b had not run and went
    # to someone else before its launch.
    a_models = [model for line_id, model in launcher.calls if line_id == "a"]
    assert a_models[0] == "glm" and len(a_models) == 2 and a_models[1] != "glm"
    b_models = [model for line_id, model in launcher.calls if line_id == "b"]
    assert b_models and b_models[0] != "glm"
    assert after.line("b").assigned_model == b_models[0]
    audit = [
        item
        for item in after.gates["scheduler"]["audit"]
        if item.get("kind") == "provider_skipped"
    ]
    assert audit and audit[-1]["line_id"] == "b" and audit[-1]["replacement"] == b_models[0]
    assert any(item["line_id"] == "a" and item.get("moved") for item in audit)
    assert {line.status for line in after.lines} == {LineStatus.PASSED}


async def test_zcode_plan_quota_is_recorded_with_its_reason(tmp_path: Path) -> None:
    def zcode(run_id: str):
        return _limited(
            run_id,
            failure_class=FailureClass.QUOTA_EXHAUSTED,
            reason="zcode_plan_quota_exhausted",
            retry_after=None,
        )

    launcher = FailFirst({"a": zcode})
    store, _machine, _gates, service, row = await _setup(
        tmp_path, [_assigned("a", "zcode", ("src/a/",))], launcher=launcher, parallel=1
    )
    await _drive(service, row.id)
    skip = (await store.get(row.id)).gates[PROVIDER_SKIPS]["zcode"]
    assert skip["reason"] == "zcode_plan_quota_exhausted"
    assert skip["source"] == "backoff"
    assert skip["until"] > 0


async def test_an_expired_skip_does_not_move_a_line(tmp_path: Path) -> None:
    launcher = OverlapLauncher(expect=1)
    store, machine, _gates, service, row = await _setup(
        tmp_path, [_assigned("a", "glm", ("src/a/",))], launcher=launcher, parallel=1
    )

    def expired(r: Dispatch) -> Dispatch:
        r.gates[PROVIDER_SKIPS] = {
            "glm": {"reason": "rate_limited", "until": 1.0, "source": "backoff", "at": "x"}
        }
        return r

    await service.apply_mutation(row.id, expired)
    await _drive(service, row.id)
    assert launcher.calls == [("a", "glm")]


async def test_a_provider_busy_in_another_dispatch_does_not_take_a_line_task(
    tmp_path: Path, monkeypatch
) -> None:
    """The scheduler looks at the launcher's process-wide provider slots first."""
    import src.tools.auto_dispatch.execution as execution

    class Capped(OverlapLauncher):
        def capacity_for(self, provider: str) -> int:
            return 1

    busy = {"kimi"}
    monkeypatch.setattr(
        execution, "provider_slots_free", lambda provider, capacity=None: provider not in busy
    )
    launcher = Capped(expect=1)
    store, _machine, _gates, service, row = await _setup(
        tmp_path,
        [_assigned("a", "kimi", ("src/a/",)), _assigned("b", "glm", ("src/b/",))],
        launcher=launcher,
    )

    original = launcher.launch_worker

    async def free_kimi_after_glm(line, model, skills, **kwargs):
        result = await original(line, model, skills, **kwargs)
        if model == "glm":
            busy.clear()
        return result

    launcher.launch_worker = free_kimi_after_glm
    await _drive(service, row.id)
    # a (kimi) comes first in DAG order, but kimi's slot was taken elsewhere.
    assert [call[0] for call in launcher.calls] == ["b", "a"]
    assert {line.status for line in (await store.get(row.id)).lines} == {LineStatus.PASSED}


@pytest.mark.parametrize("implementing", [False, True])
async def test_a_ready_line_waiting_for_a_busy_slot_after_a_restart_launches(
    tmp_path: Path, implementing: bool
) -> None:
    """Another dispatch holds the provider's only slot; the line waits, never ERROR.

    After a restart the line is ASSIGNED in ``implementing`` and counts as
    active itself, which must not stop the drive from launching it.
    """
    from src.tools.auto_dispatch.execution import _provider_gate

    class Capped(OverlapLauncher):
        def capacity_for(self, provider: str) -> int:
            return 1

    launcher = Capped(expect=1)
    store, _machine, gates, service, row = await _setup(
        tmp_path, [_assigned("a", "kimi", ("src/a/",))], launcher=launcher
    )
    if implementing:

        def mid(r: Dispatch) -> Dispatch:
            line = r.lines[0]
            snap = gates.line_snapshot(r, line)
            state = dict(snap.get("machine") or {})
            state.update(
                {
                    "task_id": line.id,
                    "stage": "implementing",
                    "author_provider": "kimi",
                    "developer_provider": "kimi",
                }
            )
            snap["machine"] = state
            line.current_run = "par:a:0:died"
            r.state = DispatchState.LAUNCHING
            return r

        await service.apply_mutation(row.id, mid)
    gate = _provider_gate("kimi")
    assert gate.try_acquire(1)  # another dispatch's kimi worker holds the slot

    async def release_later() -> None:
        await asyncio.sleep(0.3)
        gate.release()

    releaser = asyncio.create_task(release_later())
    await _drive(service, row.id)
    await releaser
    after = await store.get(row.id)
    assert after.state is not DispatchState.ERROR, after.message
    assert after.line("a").status is LineStatus.PASSED
    assert launcher.calls == [("a", "kimi")]
