"""Shared fakes for the N76 state-machine wiring tests.

Only the liveness sweep, evidence and approval helpers need collaborators;
these builders keep every test's dispatch real (a store row, a GateRunner
snapshot) without reaching a subprocess or the network.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from src.server.services.autonomy.stage_gate import Stage
from src.tools.auto_dispatch import (
    AlwaysPassReviewer,
    Collaborators,
    Dispatch,
    DispatchMachine,
    FileApprovalGate,
    InMemoryDispatchStore,
    InMemoryOutcomeStore,
    LineStatus,
    MachineConfig,
    NoopMerger,
    RecordingLauncher,
    RoundRobinSelector,
    ScriptedMonitor,
)
from src.tools.auto_dispatch.gates import GateRunner, GateSettings
from src.tools.auto_dispatch.machine import _load_state, _store_state
from src.tools.auto_dispatch.types import Line


class Recorder:
    """A sleep that records what it was asked to wait for."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def make_line(line_id: str = "line-0", **extra) -> Line:
    acceptance = extra.pop("acceptance", ("cmd: pytest -q tests/unit/x.py", "returns 200"))
    return Line(
        id=line_id,
        name=extra.pop("name", line_id),
        task=extra.pop("task", f"do {line_id}"),
        acceptance=acceptance,
        **extra,
    )


def make_dispatch(*lines: Line, dispatch_id: str = "d1") -> Dispatch:
    return Dispatch(
        id=dispatch_id,
        goal="ship it",
        user_id="u",
        created_at="now",
        lines=list(lines),
        integrate_branch=f"integrate/auto-dispatch-{dispatch_id}",
    )


def make_gates(tmp_path: Path) -> GateRunner:
    return GateRunner(
        GateSettings(runs_dir=tmp_path / "runs", repo_root=tmp_path / "repo")
    )


def make_machine(
    tmp_path: Path,
    *,
    gates: GateRunner | None = None,
    selector=None,
    reviewer=None,
    config: MachineConfig | None = None,
    store=None,
) -> tuple[DispatchMachine, Collaborators, Recorder]:
    recorder = Recorder()
    collaborators = Collaborators(
        planner=None,  # type: ignore[arg-type]
        selector=selector or RoundRobinSelector(),
        launcher=RecordingLauncher(),
        monitor=ScriptedMonitor(),
        reviewer=reviewer or AlwaysPassReviewer(),
        merger=NoopMerger(),
        gate=FileApprovalGate(tmp_path / "proposed"),
        outcomes=InMemoryOutcomeStore(),
        store=store or InMemoryDispatchStore(),
        gates=gates,
    )
    machine = DispatchMachine(collaborators, config=config, sleep=recorder)
    return machine, collaborators, recorder


async def stored(
    collaborators: Collaborators, dispatch: Dispatch
) -> Dispatch:
    return await collaborators.store.create(dispatch)


def park_line(
    gates: GateRunner,
    dispatch: Dispatch,
    line: Line,
    reason: str,
    *,
    stage: Stage = Stage.BLOCKED,
) -> dict[str, object]:
    """Write the line and gate state a park leaves, without running a drive."""
    line.status = LineStatus.BLOCKED
    line.failure_reason = reason
    line.current_run = None
    snapshot = gates.line_snapshot(dispatch, line)
    state = _load_state(snapshot)
    state = replace(state, stage=stage, blocked_from=stage.value)
    _store_state(snapshot, state)
    snapshot["reason"] = reason
    return snapshot
