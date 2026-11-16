"""P0 silent worker, wired: LogMonitor detects, the machine records and acts.

``off`` is main (no read, no record, no alert); ``shadow`` (the default)
records the finding on the line and alerts once per run but changes nothing;
``enforce`` costs the attempt as ``worker_no_output`` and reassigns the line
through the existing reassign path.
"""

from __future__ import annotations

import pytest

from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus
from src.tools.auto_dispatch.dev import RecordingLauncher, RoundRobinSelector
from src.tools.auto_dispatch.execution import LogMonitor, WorkerRunStatus
from src.tools.auto_dispatch.liveness import consistency, policy
from src.tools.auto_dispatch.p0_hooks import UNGATED_SILENT_KEY
from src.tools.auto_dispatch.silent_worker import (
    KIND_EMPTY_FINAL,
    KIND_NO_OUTPUT_TIMEOUT,
    MINUTES_ENV,
    MODE_ENV,
    RECORD_KEY,
    WORKER_NO_OUTPUT,
    SilentFinding,
)
from src.tools.auto_dispatch.types import RunStatus

from .fakes import Clock
from .test_machine import _collaborators, _dispatch, _machine
from .test_machine_gates import _bundle, _line, _repo_with_lines
from .test_w3b_fix1 import _settings

CLAUDE_EMPTY = '{"type":"system","subtype":"init","session_id":"s"}\n'
CLAUDE_TEXT = (
    '{"type":"assistant","message":{"role":"assistant",'
    '"content":[{"type":"text","text":"done"}]}}\n'
)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.delenv(MODE_ENV, raising=False)
    monkeypatch.delenv(MINUTES_ENV, raising=False)
    monkeypatch.setenv("CONDUCTOR_DISPATCH_INBOX_DIR", str(tmp_path / "inbox"))


def _write(runs, run_id: str, text: str, *, exit_code: str | None = None) -> None:
    (runs / f"{run_id}.log").write_text(text, encoding="utf-8")
    if exit_code is not None:
        (runs / f"{run_id}.exit").write_text(exit_code, encoding="utf-8")


# ------------------------------------------------------------- LogMonitor


@pytest.mark.asyncio
async def test_monitor_shadow_flags_an_empty_final_answer(tmp_path) -> None:
    _write(tmp_path, "run-1", CLAUDE_EMPTY, exit_code="0")
    status = await LogMonitor(tmp_path, clock=Clock()).poll("run-1")
    assert status.complete is True
    assert isinstance(status.silent, SilentFinding)
    assert status.silent.kind == KIND_EMPTY_FINAL
    assert status.silent.reason == WORKER_NO_OUTPUT


@pytest.mark.asyncio
async def test_monitor_does_not_flag_a_run_with_model_text(tmp_path) -> None:
    _write(tmp_path, "run-1", CLAUDE_EMPTY + CLAUDE_TEXT, exit_code="0")
    status = await LogMonitor(tmp_path, clock=Clock()).poll("run-1")
    assert status.complete is True
    assert status.silent is None


@pytest.mark.asyncio
async def test_monitor_flags_a_running_silent_log_after_the_threshold(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(MINUTES_ENV, "1")
    clock = Clock()
    monitor = LogMonitor(tmp_path, stall_seconds=600, timeout_seconds=6000, clock=clock)
    _write(tmp_path, "run-1", CLAUDE_EMPTY)
    first = await monitor.poll("run-1")
    assert type(first) is RunStatus
    clock.advance(61)
    status = await monitor.poll("run-1")
    assert isinstance(status, WorkerRunStatus)
    assert status.silent is not None and status.silent.kind == KIND_NO_OUTPUT_TIMEOUT
    # Detection only: the monitor does not decide that the run failed.
    assert status.timed_out is False and status.stalled is False and status.complete is False


@pytest.mark.asyncio
async def test_monitor_off_reads_nothing_and_returns_mains_shapes(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(MODE_ENV, "off")
    monkeypatch.setenv(MINUTES_ENV, "1")
    clock = Clock()
    monitor = LogMonitor(tmp_path, stall_seconds=600, timeout_seconds=6000, clock=clock)
    _write(tmp_path, "run-1", CLAUDE_EMPTY)
    await monitor.poll("run-1")
    clock.advance(61)
    running = await monitor.poll("run-1")
    assert type(running) is RunStatus
    _write(tmp_path, "run-2", CLAUDE_EMPTY, exit_code="0")
    done = await monitor.poll("run-2")
    assert done.silent is None


# ---------------------------------------------------------------- machine


class _SilentOnce:
    """The first run of ``line-0`` is silent; everything else completes."""

    def __init__(self, finding: SilentFinding, *, complete: bool) -> None:
        self.finding = finding
        self.complete = complete
        self.silenced: list[str] = []
        self.cancelled: list[str] = []

    async def poll(self, run_id: str) -> RunStatus:
        if "line-0" in run_id and not self.silenced:
            self.silenced.append(run_id)
            return WorkerRunStatus(
                output="",
                complete=self.complete,
                exit_code=0 if self.complete else None,
                silent=self.finding,
            )
        return RunStatus(output=f"done {run_id}", complete=True, tokens=16)

    async def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)


def _empty_final() -> SilentFinding:
    return SilentFinding(kind=KIND_EMPTY_FINAL, elapsed_seconds=5.0, threshold_seconds=900.0)


class UnmovedLauncher(RecordingLauncher):
    """Records, as the real launcher does, that each run left its head unmoved.

    Enforce reassigns an ``empty_final_answer`` only when the run is known not
    to have moved the branch head; the plain dev launcher reports no heads.
    """

    def __init__(self) -> None:
        super().__init__()
        self.rows: list = []

    async def launch(self, line, model, skills, **kwargs) -> str:
        run_id = await super().launch(line, model, skills)
        for row in self.rows:
            if row.line(line.id) is None:
                continue
            root = row.gates.setdefault("worker_recovery", {})
            entry = root.setdefault(
                line.id, {"attempts": {}, "session_id": None, "session_mode": None}
            )
            entry.setdefault("runs", []).append(
                {"author": model, "attempt": line.attempts, "branch": line.branch or "",
                 "start": "a1a1a1", "head": "a1a1a1", "submitted": False}
            )
        return run_id


def _timeout() -> SilentFinding:
    return SilentFinding(
        kind=KIND_NO_OUTPUT_TIMEOUT, elapsed_seconds=960.0, threshold_seconds=900.0
    )


async def _run(
    tmp_path, finding: SilentFinding, *, complete: bool = True, unmoved: bool = False
):
    monitor = _SilentOnce(finding, complete=complete)
    launcher = UnmovedLauncher() if unmoved else None
    collaborators = _collaborators(tmp_path, monitor=monitor, launcher=launcher)
    machine, _recorder = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())
    if launcher is not None:
        launcher.rows.append(dispatch)
    result = await machine.run(dispatch)
    return result, collaborators, monitor


def _cell(result) -> dict:
    return (result.gates.get(UNGATED_SILENT_KEY) or {}).get("line-0") or {}


def _inbox(tmp_path) -> str:
    path = tmp_path / "inbox" / "needs-human.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.mark.asyncio
async def test_shadow_records_and_alerts_once_but_changes_nothing(tmp_path) -> None:
    result, collaborators, monitor = await _run(tmp_path, _empty_final())

    assert monitor.silenced, "the scripted silent run was never polled"
    # Same launches and outcome as main: three lines, one launch each.
    assert [line_id for line_id, _m, _s in collaborators.launcher.launches] == [
        "line-0", "line-1", "line-2"
    ]
    assert result.state is DispatchState.APPROVAL
    assert result.line("line-0").status is LineStatus.PASSED
    assert result.line("line-0").attempts == 0
    record = _cell(result)[RECORD_KEY]
    entry = record["runs"][monitor.silenced[0]]
    assert entry["mode"] == "shadow"
    assert entry["kind"] == KIND_EMPTY_FINAL
    assert entry["alerted"] is True
    assert _inbox(tmp_path).count("finished with no model output") == 1


@pytest.mark.asyncio
async def test_off_records_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(MODE_ENV, "off")
    result, collaborators, _monitor = await _run(tmp_path, _empty_final())

    assert len(collaborators.launcher.launches) == 3
    assert UNGATED_SILENT_KEY not in result.gates
    assert _inbox(tmp_path) == ""


@pytest.mark.asyncio
async def test_enforce_reassigns_an_empty_final_answer(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(MODE_ENV, "enforce")
    result, collaborators, monitor = await _run(tmp_path, _empty_final(), unmoved=True)

    launches = [(line_id, model) for line_id, model, _s in collaborators.launcher.launches]
    line0 = [model for line_id, model in launches if line_id == "line-0"]
    # The silent attempt is spent and another worker gets the line.
    assert len(line0) == 2 and line0[0] != line0[1]
    line = result.line("line-0")
    assert line.attempts == 1
    assert line.status is LineStatus.PASSED
    assert monitor.cancelled == monitor.silenced
    assert _cell(result)[RECORD_KEY]["runs"][monitor.silenced[0]]["mode"] == "enforce"


@pytest.mark.asyncio
async def test_enforce_reassigns_a_no_output_timeout(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(MODE_ENV, "enforce")
    result, collaborators, monitor = await _run(tmp_path, _timeout(), complete=False)

    line0 = [m for line_id, m, _s in collaborators.launcher.launches if line_id == "line-0"]
    assert len(line0) == 2
    assert result.line("line-0").attempts == 1
    assert "no model output for 16 min" in _inbox(tmp_path)


async def _gated(tmp_path, monitor, *, unmoved: bool = False):
    repo = _repo_with_lines(tmp_path / "repo", "line-0")
    launcher = UnmovedLauncher() if unmoved else None
    collaborators, machine, _commands, _trees = _bundle(
        tmp_path,
        [_line("line-0", assigned_model="kimi")],
        monitor=monitor,
        launcher=launcher,
        selector=RoundRobinSelector(("kimi", "codex", "claude")),
        settings=_settings(tmp_path, repo),
    )
    dispatch = await collaborators.store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )
    if launcher is not None:
        launcher.rows.append(dispatch)
    return await machine.run(dispatch), collaborators


@pytest.mark.asyncio
async def test_gated_enforce_charges_the_attempt_as_worker_no_output(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(MODE_ENV, "enforce")
    monitor = _SilentOnce(_empty_final(), complete=True)
    result, collaborators = await _gated(tmp_path, monitor, unmoved=True)

    assert [m for _l, m, _s in collaborators.launcher.launches][:2] == ["kimi", "codex"]
    costs = result.gates["cost"]["lines"]["line-0"]
    assert (costs[0]["model"], costs[0]["result"], costs[0]["reason_code"]) == (
        "kimi", "failed", WORKER_NO_OUTPUT
    )
    record = result.gates["lines"]["line-0"][RECORD_KEY]
    assert record["runs"][monitor.silenced[0]]["mode"] == "enforce"


@pytest.mark.asyncio
async def test_gated_shadow_records_on_the_snapshot_and_charges_ok(tmp_path) -> None:
    monitor = _SilentOnce(_empty_final(), complete=True)
    result, collaborators = await _gated(tmp_path, monitor)

    assert [m for _l, m, _s in collaborators.launcher.launches] == ["kimi"]
    costs = result.gates["cost"]["lines"]["line-0"]
    assert costs[0]["result"] == "ok" and costs[0]["reason_code"] in (None, "")
    record = result.gates["lines"]["line-0"][RECORD_KEY]
    assert record["runs"][monitor.silenced[0]]["mode"] == "shadow"
    assert result.line("line-0").status is LineStatus.PASSED


@pytest.mark.asyncio
async def test_enforce_fails_the_line_with_worker_no_output_when_attempts_run_out(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(MODE_ENV, "enforce")

    class _AlwaysSilent(_SilentOnce):
        async def poll(self, run_id: str) -> RunStatus:
            if "line-0" in run_id:
                self.silenced.append(run_id)
                return WorkerRunStatus(
                    output="", complete=True, exit_code=0, silent=self.finding
                )
            return RunStatus(output="done", complete=True, tokens=1)

    monitor = _AlwaysSilent(_empty_final(), complete=True)
    launcher = UnmovedLauncher()
    collaborators = _collaborators(tmp_path, monitor=monitor, launcher=launcher)
    machine, _recorder = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())
    launcher.rows.append(dispatch)
    result = await machine.run(dispatch)

    line = result.line("line-0")
    assert line.status is LineStatus.FAILED
    assert line.failure_reason == WORKER_NO_OUTPUT
    assert result.state is DispatchState.ABORT


def test_worker_no_output_is_an_r_exit_reason_with_a_consistency_row() -> None:
    assert policy.DEFAULT_REASON_GROUPS[WORKER_NO_OUTPUT] == policy.R_EXIT
    assert WORKER_NO_OUTPUT in consistency.REASON_STAGES
