"""The dispatch service: settings, the human decisions, and which adapters run.

Every collaborator is a double and the background drive is recorded rather than
scheduled, so nothing here starts a subprocess, touches the database or waits.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.server.services.dispatch import (
    DispatchService,
    DispatchSettings,
    dispatch_summary,
)
from src.tools.auto_dispatch import (
    Dispatch,
    DispatchState,
    FileApprovalGate,
    InMemoryDispatchStore,
    Line,
    LineStatus,
)
from src.tools.auto_dispatch.dev import (
    AlwaysPassReviewer,
    FixedPlanner,
    NoopMerger,
    RecordingLauncher,
    RoundRobinSelector,
    ScriptedMonitor,
)
from src.tools.auto_dispatch.execution import GitMerger, LogMonitor, ScriptLauncher
from src.tools.auto_dispatch.planning import LLMPlanner
from src.tools.auto_dispatch.selection import QuotaAwareModelSelector

pytestmark = pytest.mark.asyncio


class Harness:
    """A service over fakes, with the drives it asked for and the store it wrote."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.proposed = tmp_path / "proposed"
        self.store = InMemoryDispatchStore()
        self.gate = FileApprovalGate(self.proposed)
        self.scheduled: list[str] = []

        def _schedule(key, factory):
            self.scheduled.append(key)

        monkeypatch.setattr("src.server.services.dispatch._schedule", _schedule)
        self.service = DispatchService(
            DispatchSettings(proposed_dir=self.proposed),
            store=self.store,
            gate=self.gate,
        )

    async def start(self, goal: str, *, lines: int = 0, state=DispatchState.SUBMITTED) -> Dispatch:
        dispatch = await self.service.start(goal, "u1")
        dispatch.state = state
        dispatch.lines = [
            Line(id=f"line-{index}", name=f"n{index}", task=f"task {index}")
            for index in range(lines)
        ]
        return await self.store.save(dispatch)

    def gate_state(self, dispatch_id: str) -> str:
        payload = json.loads((self.proposed / dispatch_id / "gate.json").read_text("utf-8"))
        return str(payload["state"])


@pytest.fixture
def harness(tmp_path, monkeypatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


async def test_settings_default_to_the_dev_adapters() -> None:
    settings = DispatchSettings.from_env({})
    assert settings.adapters == "dev"
    assert settings.stall_seconds == 60.0
    assert settings.timeout_seconds == 600.0
    assert settings.launch_script is None


async def test_settings_read_every_environment_override() -> None:
    settings = DispatchSettings.from_env(
        {
            "CONDUCTOR_DISPATCH_ADAPTERS": "live",
            "CONDUCTOR_DISPATCH_RUNS_DIR": "/tmp/runs",
            "CONDUCTOR_DISPATCH_REPO": "/tmp/repo",
            "CONDUCTOR_DISPATCH_PROPOSED_DIR": "/tmp/proposed",
            "CONDUCTOR_DISPATCH_LAUNCH_SCRIPT": "/tmp/launch.sh",
            "CONDUCTOR_DISPATCH_SKILLS_HUB": "hub",
            "CONDUCTOR_DISPATCH_IMPACT_MAP": "map.json",
            "CONDUCTOR_DISPATCH_STALL_SECONDS": "5",
            "CONDUCTOR_DISPATCH_TIMEOUT_SECONDS": "50",
            "CONDUCTOR_DISPATCH_POLL_SECONDS": "1.5",
        }
    )
    assert settings.adapters == "live"
    assert settings.runs_dir == Path("/tmp/runs")
    assert settings.repo_root == Path("/tmp/repo")
    assert settings.proposed_dir == Path("/tmp/proposed")
    assert settings.launch_script == Path("/tmp/launch.sh")
    assert settings.skills_hub == "hub"
    assert settings.impact_map == "map.json"
    assert (settings.stall_seconds, settings.timeout_seconds) == (5.0, 50.0)
    assert settings.poll_interval_seconds == 1.5


async def test_start_creates_a_submitted_dispatch_and_drives_it(harness: Harness) -> None:
    dispatch = await harness.service.start("ship the thing", "u1")

    assert dispatch.state is DispatchState.SUBMITTED
    assert dispatch.goal == "ship the thing"
    assert dispatch.user_id == "u1"
    assert dispatch.id and dispatch.created_at
    assert harness.scheduled == [f"dispatch-{dispatch.id}"]
    stored = await harness.service.get(dispatch.id)
    assert stored is not None and stored.goal == "ship the thing"


async def test_start_rejects_an_empty_goal(harness: Harness) -> None:
    with pytest.raises(ValueError, match="goal is required"):
        await harness.service.start("   ", "u1")
    assert harness.scheduled == []


async def test_active_lists_everything_that_has_not_terminated(harness: Harness) -> None:
    await harness.start("one")
    await harness.start("two", state=DispatchState.APPROVAL)
    await harness.start("three", state=DispatchState.COMPLETE)
    await harness.start("four", state=DispatchState.ERROR)

    active = await harness.service.active("u1")
    assert [item.goal for item in active] == ["one", "two"]


async def test_pending_approval_asks_the_store_for_the_gate_state(harness: Harness) -> None:
    parked = await harness.start("one", state=DispatchState.APPROVAL)

    pending = await harness.service.pending_approval("u1")
    assert [item.id for item in pending] == [parked.id]


async def test_approve_resolves_the_gate_and_drives_the_merge(harness: Harness) -> None:
    dispatch = await harness.start("one", lines=2, state=DispatchState.APPROVAL)

    approved = await harness.service.approve(dispatch.id, "looks right")

    assert approved.state is DispatchState.APPROVAL
    assert harness.gate_state(dispatch.id) == "APPROVED"
    assert harness.scheduled == [f"dispatch-{dispatch.id}"] * 2


async def test_reject_resolves_the_gate_and_drives_the_abort(harness: Harness) -> None:
    dispatch = await harness.start("one", lines=1, state=DispatchState.APPROVAL)

    rejected = await harness.service.reject(dispatch.id, "too risky")

    assert rejected.state is DispatchState.APPROVAL
    assert harness.gate_state(dispatch.id) == "REJECTED"
    assert harness.scheduled == [f"dispatch-{dispatch.id}"] * 2


async def test_a_second_decision_on_the_same_dispatch_conflicts(harness: Harness) -> None:
    dispatch = await harness.start("one", state=DispatchState.APPROVAL)
    await harness.service.approve(dispatch.id)

    with pytest.raises(ValueError, match="not awaiting approval"):
        await harness.service.reject(dispatch.id)


async def test_approving_a_dispatch_that_is_not_at_the_gate_conflicts(harness: Harness) -> None:
    dispatch = await harness.start("one", lines=1)

    with pytest.raises(ValueError, match="not awaiting approval"):
        await harness.service.approve(dispatch.id)
    assert harness.scheduled == [f"dispatch-{dispatch.id}"]


async def test_reassign_overrides_the_model_and_drives_the_line(harness: Harness) -> None:
    dispatch = await harness.start("one", lines=2, state=DispatchState.REVIEW)
    line = dispatch.line("line-0")
    assert line is not None
    line.assigned_model = "codex"
    line.status = LineStatus.BLOCKED
    line.blockers = ["no tests"]
    await harness.store.save(dispatch)

    reassigned = await harness.service.reassign(dispatch.id, "line-0", "kimi")

    line = reassigned.line("line-0")
    assert line is not None
    assert line.assigned_model == "kimi"
    assert line.attempts == 1
    assert line.status is LineStatus.ASSIGNED
    assert line.blockers == []
    assert harness.scheduled == [f"dispatch-{dispatch.id}"] * 2


async def test_reassign_refuses_a_reserved_planner_or_reviewer(harness: Harness) -> None:
    dispatch = await harness.start("one", lines=1, state=DispatchState.REVIEW)
    line = dispatch.line("line-0")
    assert line is not None
    line.assigned_model = "kimi"
    line.status = LineStatus.BLOCKED
    await harness.store.save(dispatch)

    for reserved in ("claude", "codex"):
        with pytest.raises(ValueError, match="reserved for planning or review"):
            await harness.service.reassign(dispatch.id, "line-0", reserved)

    saved = await harness.store.get(dispatch.id)
    assert saved is not None
    kept = saved.line("line-0")
    assert kept is not None
    assert kept.assigned_model == "kimi"
    assert kept.status is LineStatus.BLOCKED
    assert kept.attempts == 0
    assert kept.assignment_audit is None


async def test_reassign_of_a_reserved_model_records_the_audit_when_allowed(
    harness: Harness,
) -> None:
    from src.server.services.autonomy.roles import RolePolicy, reserved_work_audit

    harness.service._policy = RolePolicy.from_mapping({"allow_reserved_code": True})
    dispatch = await harness.start("one", lines=1, state=DispatchState.REVIEW)
    line = dispatch.line("line-0")
    assert line is not None
    line.assigned_model = "kimi"
    line.status = LineStatus.BLOCKED
    await harness.store.save(dispatch)

    reassigned = await harness.service.reassign(dispatch.id, "line-0", "claude")

    updated = reassigned.line("line-0")
    assert updated is not None
    assert updated.assigned_model == "claude"
    assert updated.assignment_audit == reserved_work_audit("claude")
    assert "allow_reserved_code: claude is reserved" in (updated.assignment_audit or "")


async def test_reassign_rejects_reserved_aliases_and_collapses_the_same_author(
    harness: Harness,
) -> None:
    from src.server.services.autonomy.roles import RolePolicy, reserved_work_audit

    dispatch = await harness.start("one", lines=1, state=DispatchState.REVIEW)
    line = dispatch.line("line-0")
    assert line is not None
    line.assigned_model = "kimi"
    line.status = LineStatus.BLOCKED
    await harness.store.save(dispatch)

    for alias in ("claudedev", "codexrev"):
        with pytest.raises(ValueError, match="reserved for planning or review"):
            await harness.service.reassign(dispatch.id, "line-0", alias)
    saved = await harness.store.get(dispatch.id)
    assert saved is not None
    kept = saved.line("line-0")
    assert kept is not None
    assert kept.assigned_model == "kimi"
    assert kept.assignment_audit is None

    harness.service._policy = RolePolicy.from_mapping({"allow_reserved_code": True})
    reassigned = await harness.service.reassign(dispatch.id, "line-0", "claudedev")
    updated = reassigned.line("line-0")
    assert updated is not None
    assert updated.assigned_model == "claude"
    assert updated.assignment_audit == reserved_work_audit("claude")

    stepped = await harness.service.reassign(dispatch.id, "line-0", "step")
    stepped_line = stepped.line("line-0")
    assert stepped_line is not None
    assert stepped_line.assigned_model == "stepfun"
    assert stepped_line.assignment_audit is None


async def test_reassign_takes_a_replan_exhausted_line(harness: Harness) -> None:
    """Reassign is the way out the replan_exhausted park message names."""
    dispatch = await harness.start("one", lines=1, state=DispatchState.REVIEW)
    line = dispatch.line("line-0")
    assert line is not None
    line.assigned_model = "kimi"
    line.status = LineStatus.BLOCKED
    line.failure_reason = "replan_exhausted"
    line.attempts = 4
    await harness.store.save(dispatch)
    scheduled = list(harness.scheduled)

    reassigned = await harness.service.reassign(dispatch.id, "line-0", "glm")

    updated = reassigned.line("line-0")
    assert updated is not None
    assert updated.assigned_model == "glm"
    assert updated.attempts == 5
    assert updated.status is LineStatus.ASSIGNED
    assert updated.failure_reason is None
    assert updated.current_run is None
    assert harness.scheduled == scheduled + [f"dispatch-{dispatch.id}"]


async def test_reassign_still_allows_a_blocked_line_that_is_not_exhausted(
    harness: Harness,
) -> None:
    dispatch = await harness.start("one", lines=1, state=DispatchState.REVIEW)
    line = dispatch.line("line-0")
    assert line is not None
    line.assigned_model = "kimi"
    line.status = LineStatus.BLOCKED
    line.failure_reason = "watchdog_timeout"
    await harness.store.save(dispatch)
    scheduled = list(harness.scheduled)

    reassigned = await harness.service.reassign(dispatch.id, "line-0", "glm")

    updated = reassigned.line("line-0")
    assert updated is not None
    assert updated.assigned_model == "glm"
    assert updated.attempts == 1
    assert updated.status is LineStatus.ASSIGNED
    assert harness.scheduled == scheduled + [f"dispatch-{dispatch.id}"]


async def test_reassign_rejects_an_unknown_line_or_a_blank_model(harness: Harness) -> None:
    dispatch = await harness.start("one", lines=1)

    with pytest.raises(ValueError, match="has no such line"):
        await harness.service.reassign(dispatch.id, "line-9", "kimi")
    with pytest.raises(ValueError, match="needs a model"):
        await harness.service.reassign(dispatch.id, "line-0", " ")


async def test_an_unknown_dispatch_id_raises(harness: Harness) -> None:
    with pytest.raises(KeyError):
        await harness.service.approve("nope")
    with pytest.raises(KeyError):
        await harness.service.reassign("nope", "line-0", "kimi")


async def test_the_dev_adapters_are_the_default(harness: Harness) -> None:
    dispatch = await harness.start("one", lines=1)
    collaborators = harness.service._machine(dispatch)._c
    assert isinstance(collaborators.planner, FixedPlanner)
    assert isinstance(collaborators.selector, RoundRobinSelector)
    assert isinstance(collaborators.launcher, RecordingLauncher)
    assert isinstance(collaborators.monitor, ScriptedMonitor)
    assert isinstance(collaborators.reviewer, AlwaysPassReviewer)
    assert isinstance(collaborators.merger, NoopMerger)


async def test_the_live_adapters_are_wired_from_the_settings(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    service = DispatchService(
        DispatchSettings(
            adapters="live",
            runs_dir=tmp_path / "runs",
            repo_root=repo,
            proposed_dir=tmp_path / "proposed",
            launch_script=tmp_path / "launch.sh",
            skills_hub="hub",
            impact_map="map.json",
            stall_seconds=5.0,
            timeout_seconds=50.0,
            poll_interval_seconds=1.5,
        ),
        store=InMemoryDispatchStore(),
        gate=FileApprovalGate(tmp_path / "proposed"),
    )
    dispatch = await service.start("one", "u1")
    machine = service._machine(dispatch)
    collaborators = machine._c

    assert isinstance(collaborators.planner, LLMPlanner)
    assert isinstance(collaborators.selector, QuotaAwareModelSelector)
    assert collaborators.selector._stats is collaborators.outcomes
    assert collaborators.selector._policy.planner == ("claude",)
    assert collaborators.selector._policy.primary_reviewer == ("codex",)
    from src.tools.auto_dispatch.outcomes import DispatchRowMemoryPort

    assert isinstance(collaborators.outcomes._port, DispatchRowMemoryPort)
    from src.tools.auto_dispatch.drain import DrainAwareLauncher

    # The live launcher is held by ``cond drain`` (N64) around ScriptLauncher.
    assert isinstance(collaborators.launcher, DrainAwareLauncher)
    assert isinstance(collaborators.launcher.inner, ScriptLauncher)
    assert collaborators.monitor._canceller == collaborators.launcher.inner.cancel
    assert isinstance(collaborators.monitor, LogMonitor)
    from src.tools.auto_dispatch.busy import BusyProxy

    # Merges and reviewer CLIs hold busy markers a drained restart waits for.
    assert isinstance(collaborators.merger, BusyProxy)
    assert isinstance(collaborators.merger.inner, GitMerger)
    assert isinstance(collaborators.reviewer, BusyProxy)
    assert collaborators.launcher._skills_hub == "hub"
    assert collaborators.launcher._impact_map == "map.json"
    assert collaborators.monitor._stall_seconds == 5.0
    assert collaborators.monitor._timeout_seconds == 50.0
    assert machine._config.poll_interval_seconds == 1.5
    # The gate is shared with the merger so a push is checked twice.
    assert collaborators.merger._gate is service._gate


async def test_a_driven_dispatch_parks_at_the_gate_and_completes_on_approval(
    harness: Harness,
) -> None:
    """The whole service path: plan, assign, launch, monitor, review, gate, merge.

    The dev adapters stand in for the ones that are not merged, but the state
    machine, the store round-trip and the approval gate are the real ones.
    """
    dispatch = await harness.start("ship the thing")
    assert harness.scheduled == [f"dispatch-{dispatch.id}"]

    parked = await harness.service._drive(dispatch.id)

    assert parked.state is DispatchState.APPROVAL
    assert parked.approval_token
    assert [line.status for line in parked.lines] == [LineStatus.PASSED] * 3
    assert parked.integrate_branch == f"integrate/auto-dispatch-{parked.id}"
    assert (harness.proposed / parked.id / "summary.md").is_file()
    assert harness.scheduled == [f"dispatch-{dispatch.id}"]
    merged = await harness.service.approve(parked.id, "looks right")
    driven = await harness.service._drive(merged.id)

    assert driven.state is DispatchState.COMPLETE
    assert set(driven.outcomes) == {"line-0", "line-1", "line-2"}
    assert all(outcome.passed for outcome in driven.outcomes.values())
    assert "ready for review" in (driven.message or "")
    # The decision scheduled the merge drive rather than holding the request.
    assert harness.scheduled == [f"dispatch-{dispatch.id}"] * 2


async def test_dispatch_summary_is_the_shape_the_api_renders(harness: Harness) -> None:
    dispatch = await harness.start("ship it", lines=1)
    dispatch.state = DispatchState.MONITORING
    line = dispatch.line("line-0")
    assert line is not None
    line.assigned_model = "codex"
    line.status = LineStatus.RUNNING

    summary = dispatch_summary(dispatch)

    assert summary["id"] == dispatch.id
    assert summary["goal"] == "ship it"
    assert summary["state"] == "MONITORING"
    assert 0 < summary["progress_pct"] < 100
    assert summary["lines"][0]["assigned_model"] == "codex"
    assert summary["outcomes"] == {}
    assert summary["approval_token"] is None
