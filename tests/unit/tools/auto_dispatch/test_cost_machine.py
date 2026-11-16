"""N30 v1: the machine writes one cost-ledger entry per attempt (gates["cost"]).

Offline and in-process: scripted launchers, the fake Stage-Gate bundle from
test_machine_gates, a fake wall clock, and no CodexBar snapshot (read_quota is
replaced). The ledger keeps numbers and stable codes only: no log text.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Collection
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.llms.native_cli.base import NativeCliUsage
from src.llms.native_cli.failure import FailureClass, classify_failure
from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus
from src.tools.auto_dispatch import cost
from src.tools.auto_dispatch import execution
from src.tools.auto_dispatch.dev import RoundRobinSelector
from src.tools.auto_dispatch.execution import (
    LaunchResult,
    ScriptLauncher,
    WorkerLaunchFailure,
)
from src.tools.auto_dispatch.protocols import ModelRole
from src.tools.auto_dispatch.types import Line, RunStatus, Verdict
from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle, _line

BASE = "b" * 40
H1 = "1" * 40
H2 = "2" * 40
SENTINEL = "N30-SENTINEL-7f3a9c-do-not-store"
MISSING = "MISSING_CREDENTIAL: no API key"
#: The real reader, kept before the autouse fixture below replaces it.
REAL_READ_QUOTA = cost.read_quota


@pytest.fixture(autouse=True)
def _no_codexbar(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read the real HOME's CodexBar snapshot; a fixed reading instead."""
    monkeypatch.delenv("CONDUCTOR_DISPATCH_COST_LEDGER", raising=False)
    monkeypatch.setattr(cost, "read_quota", lambda model, **_kw: None)


class Workers:
    """``launch_worker`` that plays one outcome per call for each model."""

    def __init__(self, script: dict[str, list[tuple[object, ...]]]) -> None:
        self.script = {model: list(steps) for model, steps in script.items()}
        self.calls: list[tuple[str, str]] = []

    def failures(self, model: str) -> int:
        return sum(1 for _line_id, name in self.calls if name == model)

    async def launch(self, line, model, skills):
        raise AssertionError("the recovery machine uses launch_worker")

    async def launch_worker(self, line, model, skills, *, session_id, session_mode, handoff_path):
        self.calls.append((line.id, model))
        steps = self.script[model]
        step = steps.pop(0) if len(steps) > 1 else steps[0]
        kind = step[0]
        run_id = line.current_run or f"run-{len(self.calls)}"
        if kind == "ok":
            _kind, start, head, usage = step
            return LaunchResult(
                run_id,
                session_id,
                f"committed {SENTINEL}",
                usage=usage or NativeCliUsage(),
                start_head=start,
                head=head,
            )
        output = str(step[1])
        if kind == "rate_limited":
            failure_class, reason = FailureClass.RATE_LIMITED, "worker_rate_limited"
            code = 75
        else:
            classified = classify_failure(model, output, exit_code=79)
            failure_class, reason, code = classified.failure_class, classified.reason_code, 79
        failure = WorkerLaunchFailure(
            run_id=run_id,
            exit_code=code,
            output=output,
            session_id=None,
            retry_after_seconds=0,
            failure_class=failure_class,
            reason_code=reason,
        )
        failure.start_head = BASE
        failure.head = BASE
        raise failure


class MoveAfter(RoundRobinSelector):
    """Keeps a rate-limited author until it has failed ``after`` times, then moves to ``to``."""

    def __init__(self, workers: Workers, *, author: str, to: str, after: int) -> None:
        super().__init__(pool=(author, to, "grok"))
        self.workers = workers
        self.author = author
        self.to = to
        self.after = after

    async def select(
        self,
        line: Line,
        used_models: list[str],
        *,
        role: ModelRole = ModelRole.WORK,
        exclude: Collection[str] = (),
    ) -> str | None:
        if role is ModelRole.WORK and self.author in exclude:
            if self.workers.failures(self.author) < self.after:
                return None
            return self.to
        return await super().select(line, used_models, role=role, exclude=exclude)


class BlockOnce:
    """The first review asks for a fix; every later review passes."""

    def __init__(self) -> None:
        self.reviewed: list[tuple[str, str]] = []

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.reviewed.append((line_id, model))
        if len(self.reviewed) == 1:
            return Verdict.blocked(["add a regression test"])
        return Verdict.ok(f"reviewed by {model}")


class SentinelMonitor:
    """Every poll finishes the run, with the sentinel in its log tail."""

    async def poll(self, run_id: str) -> RunStatus:
        return RunStatus(output=f"log {SENTINEL} {MISSING}", complete=True, tokens=250)

    async def cancel(self, run_id: str) -> None:
        return None


class Wall:
    """The Stage-Gate clock, moving one second each time it is read."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def _handoff(*_args: object, **_kwargs: object) -> dict[str, object]:
    return {
        "branch": "feature/line-x",
        "commit": "abc123",
        "files": ("a.py",),
        "test_command": "pytest",
        "results": "pass",
        "known_limitations": "none",
    }


def _dispatch(dispatch_id: str, model: str) -> Dispatch:
    line = _line("line-x", assigned_model=model, status=LineStatus.ASSIGNED)
    line.branch = f"feature/auto-dispatch-{dispatch_id}/line-line-x"
    line.base_commit = BASE
    return Dispatch(
        id=dispatch_id,
        goal="ship the line",
        user_id="u",
        created_at="now",
        lines=[line],
        state=DispatchState.LAUNCHING,
    )


async def _run_1302(tmp_path: Path) -> tuple[Dispatch, Workers]:
    """glm is rate limited three times, moves to kimi, kimi works + one fix, merged."""
    usage = NativeCliUsage(input_tokens=1200, output_tokens=300)
    workers = Workers(
        {
            "glm": [("rate_limited", "HTTP 429 rate limited")],
            "kimi": [("ok", BASE, H1, usage), ("ok", H1, H2, None)],
        }
    )
    selector = MoveAfter(workers, author="glm", to="kimi", after=3)
    collaborators, machine, _commands, _trees = _bundle(
        tmp_path, [], launcher=workers, reviewer=BlockOnce(), selector=selector
    )
    collaborators.gates.handoff_facts = _handoff
    heads = [BASE, BASE, BASE, BASE, H1, H2, H2, H2]
    collaborators.gates._revision = lambda _line: heads[min(len(workers.calls), len(heads) - 1)]
    collaborators.gates._clock = Wall()
    result = await machine.run(_dispatch("d-n30-1302", "glm"))
    return result, workers


def test_1302_rate_limited_glm_then_kimi_work_and_fix_is_one_merged_line(
    tmp_path: Path,
) -> None:
    result, workers = asyncio.run(_run_1302(tmp_path))

    assert [model for _line_id, model in workers.calls] == ["glm", "glm", "glm", "kimi", "kimi"]
    assert "line-x" in result.gates["integration"]["merged_lines"]
    entries = result.gates["cost"]["lines"]["line-x"]
    assert [(e["model"], e["kind"], e["result"]) for e in entries] == [
        ("glm", "work", "failed"),
        ("glm", "work", "failed"),
        ("glm", "work", "failed"),
        ("kimi", "work", "ok"),
        ("kimi", "fix", "ok"),
    ]
    assert len({e["run_id"] for e in entries}) == 5
    glm = entries[:3]
    assert {e["failure_class"] for e in glm} == {"rate_limited"}
    assert all(e["reason_code"] for e in glm)
    work, fix = entries[3], entries[4]
    # The launch adopted the run's wall start; the poll completion ended it.
    assert work["started_at"] and work["ended_at"] and work["wall_secs"] > 0
    assert (work["tokens_in"], work["tokens_out"], work["token_basis"]) == (1200, 300, "cli")
    # An empty usage is not a CLI count: the estimate is the size // 4 tokens.
    assert (fix["tokens_in"], fix["token_basis"], fix["tokens_est"]) == (None, "estimate", 16)
    assert fix["fix_round"] >= 1

    report = cost.dispatch_cost_report(result)
    assert report["basis"] == "ledger"
    uses = [(a["model"], a["use"]) for a in report["attempts"]["line-x"]]
    assert uses.count(("glm", "waste")) == 3
    assert ("kimi", "useful") in uses and ("kimi", "rework") in uses
    by_model = {row["model"]: row for row in report["by_model"]}
    assert by_model["glm"]["cost_per_merged_line_wall_secs"] is None
    waste = {row["class"]: row for row in report["waste_by_class"]}
    assert waste["rate_limited"]["attempts"] == 3
    assert SENTINEL not in json.dumps(result.gates)
    assert SENTINEL not in json.dumps(report)


def test_missing_credential_at_launch_is_an_entry_without_a_start_and_no_log_text(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[Dispatch, Workers]:
        workers = Workers(
            {
                "deepseek": [("fail", f"{MISSING}\n{SENTINEL}")],
                "kimi": [("ok", BASE, H1, None)],
            }
        )
        selector = RoundRobinSelector(pool=("deepseek", "kimi", "grok"))
        collaborators, machine, _commands, _trees = _bundle(
            tmp_path, [], launcher=workers, selector=selector, monitor=SentinelMonitor()
        )
        collaborators.gates.handoff_facts = _handoff
        collaborators.gates._clock = Wall()
        return await machine.run(_dispatch("d-n30-cred", "deepseek")), workers

    result, workers = asyncio.run(scenario())

    assert ("line-x", "deepseek") in workers.calls
    entries = result.gates["cost"]["lines"]["line-x"]
    failed = [e for e in entries if e["model"] == "deepseek"]
    assert failed
    for entry in failed:
        assert entry["started_at"] is None
        assert entry["wall_secs"] == 0
        assert entry["result"] == "failed"
        # failure.py classifies the DSH marker (N30, Planner ruling 4).
        assert entry["failure_class"] == "auth_failed"
        assert entry["reason_code"] == "missing_credential"
        # size // 4 of the transient output, never the output itself.
        assert entry["tokens_est"] == len(f"{MISSING}\n{SENTINEL}") // 4
    report = cost.dispatch_cost_report(result)
    waste = {row["class"] for row in report["waste_by_class"]}
    assert "auth_failed" in waste
    infra = {row["class"]: row["infrastructure"] for row in report["waste_by_class"]}
    assert infra["auth_failed"] is True
    for blob in (json.dumps(result.gates), json.dumps(report)):
        assert SENTINEL not in blob
        assert MISSING not in blob
        assert "no API key" not in blob


def test_sentinel_in_a_successful_run_log_never_reaches_the_gates(tmp_path: Path) -> None:
    async def scenario() -> Dispatch:
        workers = Workers({"kimi": [("ok", BASE, H1, None)]})
        collaborators, machine, _commands, _trees = _bundle(
            tmp_path, [], launcher=workers, monitor=SentinelMonitor()
        )
        collaborators.gates.handoff_facts = _handoff
        collaborators.gates._clock = Wall()
        return await machine.run(_dispatch("d-n30-sentinel", "kimi"))

    result = asyncio.run(scenario())

    entries = result.gates["cost"]["lines"]["line-x"]
    assert [(e["model"], e["result"], e["tokens_est"]) for e in entries] == [("kimi", "ok", 250)]
    assert SENTINEL not in json.dumps(result.gates)
    assert SENTINEL not in json.dumps(cost.dispatch_cost_report(result))


def test_ledger_off_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONDUCTOR_DISPATCH_COST_LEDGER", "off")
    reads: list[str] = []
    monkeypatch.setattr(cost, "read_quota", lambda model, **_kw: reads.append(model))

    result, workers = asyncio.run(_run_1302(tmp_path))

    assert len(workers.calls) == 5
    assert "line-x" in result.gates["integration"]["merged_lines"]
    assert "cost" not in result.gates
    assert reads == []
    assert cost.dispatch_cost_report(result)["basis"] == "legacy"


def test_quota_readings_around_the_launch_go_into_the_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readings = iter([41.0, 44.0])

    def read(model: str, **_kw: object) -> dict[str, object]:
        return {
            "provider": "kimi",
            "used_pct": next(readings, 44.0),
            "resets_at": "2026-11-14T12:00:00+00:00",
            "updated_at": None,
            "read_at": None,
        }

    monkeypatch.setattr(cost, "read_quota", read)

    async def scenario() -> Dispatch:
        workers = Workers({"kimi": [("ok", BASE, H1, None)]})
        collaborators, machine, _commands, _trees = _bundle(tmp_path, [], launcher=workers)
        collaborators.gates.handoff_facts = _handoff
        return await machine.run(_dispatch("d-n30-quota", "kimi"))

    result = asyncio.run(scenario())

    (entry,) = result.gates["cost"]["lines"]["line-x"]
    assert entry["quota"]["provider"] == "kimi"
    assert entry["quota"]["before_pct"] == 41.0
    assert entry["quota"]["after_pct"] == 44.0


def test_a_classified_launch_failure_keeps_the_cli_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "launch.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher = ScriptLauncher(tmp_path / "runs", script=script, cwd=tmp_path)

    async def execute(argv, line, run_id, *, allow_dirty, before_spawn):
        return 75, tmp_path, "HTTP 429 rate limited", None

    async def head(_line, _cwd):
        return None

    monkeypatch.setattr(launcher, "_execute_worker", execute)
    monkeypatch.setattr(launcher, "_branch_head", head)
    monkeypatch.setattr(
        execution,
        "_session_metadata",
        lambda _p, _o, requested: (requested, NativeCliUsage(input_tokens=11, output_tokens=7)),
    )
    line = Line(id="line-a", name="line-a", task="t", acceptance=("cmd: true",), current_run="r-1")

    with pytest.raises(WorkerLaunchFailure) as raised:
        asyncio.run(
            launcher.launch_worker(
                line, "glm", [], session_id=None, session_mode=None, handoff_path=None
            )
        )

    assert raised.value.failure_class is FailureClass.RATE_LIMITED
    assert raised.value.usage == NativeCliUsage(input_tokens=11, output_tokens=7)


def test_a_stage_gate_run_never_opens_the_real_codexbar_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review fix 1: the ledger's quota read follows the call-time path, which
    the unit conftest points at tmp_path; the operator's HOME is never read."""
    from src.tools.auto_dispatch import headroom_codexbar

    monkeypatch.setattr(cost, "read_quota", REAL_READ_QUOTA)
    opened: list[Path] = []
    real_reader = headroom_codexbar._read_snapshot

    def spy(path: Path):
        opened.append(Path(path))
        return real_reader(path)

    monkeypatch.setattr(headroom_codexbar, "_read_snapshot", spy)

    async def scenario() -> Dispatch:
        workers = Workers({"kimi": [("ok", BASE, H1, None)]})
        collaborators, machine, _commands, _trees = _bundle(tmp_path, [], launcher=workers)
        collaborators.gates.handoff_facts = _handoff
        return await machine.run(_dispatch("d-n30-home", "kimi"))

    result = asyncio.run(scenario())

    assert "cost" in result.gates
    assert opened, "the ledger read a quota at launch"
    real = Path.home() / ".conductor" / "usage" / "codexbar-snapshot.json"
    assert all(path == tmp_path / "no-snapshot.json" for path in opened)
    assert real not in opened
    assert headroom_codexbar.DEFAULT_SNAPSHOT_PATH not in opened


def test_quota_reads_add_no_await_point_to_the_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review fix 2: before-reading ahead of the reservation, after-reading on
    the writer inside the adopt step, and the reader is synchronous."""
    import inspect

    from src.tools.auto_dispatch.machine import DispatchMachine

    assert not inspect.iscoroutinefunction(DispatchMachine._cost_quota)
    order: list[str] = []

    def read(model: str, **_kw: object) -> None:
        order.append("read")
        return None

    monkeypatch.setattr(cost, "read_quota", read)
    reserve = DispatchMachine._reserve_run
    adopt = DispatchMachine._apply_launch_done

    async def spy_reserve(self, dispatch, line):
        order.append("reserve")
        return await reserve(self, dispatch, line)

    async def spy_adopt(self, event, dispatch, line):
        order.append("adopt")
        await adopt(self, event, dispatch, line)
        order.append("adopted")

    monkeypatch.setattr(DispatchMachine, "_reserve_run", spy_reserve)
    monkeypatch.setattr(DispatchMachine, "_apply_launch_done", spy_adopt)

    async def scenario() -> Dispatch:
        workers = Workers({"kimi": [("ok", BASE, H1, None)]})
        collaborators, machine, _commands, _trees = _bundle(tmp_path, [], launcher=workers)
        collaborators.gates.handoff_facts = _handoff
        return await machine.run(_dispatch("d-n30-order", "kimi"))

    asyncio.run(scenario())

    first = order[: order.index("adopted") + 1]
    assert first == ["read", "reserve", "adopt", "read", "adopted"]
