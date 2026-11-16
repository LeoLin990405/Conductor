"""A dispatch selected in a workspace executes in that checkout, and keeps it.

The API resolves the registry row once; the dispatch row carries a copy of the
repository, sandbox policy and default worker so a later edit or deletion of
the workspace cannot move a running (or recovered) dispatch.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.server.services.autonomy.roles import RolePolicy
from src.server.services.dispatch import DispatchService, DispatchSettings
from src.tools.auto_dispatch import InMemoryDispatchStore
from src.tools.auto_dispatch.execution import SANDBOX_POLICY_ENV

pytestmark = pytest.mark.asyncio


def _settings(tmp_path) -> DispatchSettings:
    default_repo = tmp_path / "default-repo"
    default_repo.mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()
    return DispatchSettings(
        adapters="live",
        repo_root=default_repo,
        runs_dir=runs,
        proposed_dir=tmp_path / "proposed",
        gates_mode="off",
    )


def _plan_reply(*names: str) -> str:
    payload = {
        "schema_version": 1,
        "spec": {"title": "Ship it", "body": "## Goal\nShip the thing."},
        "lines": [
            {
                "name": name,
                "task": f"Change src/{name}.py.",
                "depends_on": [],
                "write_scope": [f"src/{name}.py"],
                "acceptance": [f"cmd: pytest -q tests/unit/test_{name}.py"],
                "risk_bearing": False,
            }
            for name in names
        ],
    }
    return json.dumps(payload)


async def test_selection_changes_the_execution_repository(tmp_path, monkeypatch):
    workspace_repo = tmp_path / "workspace-repo"
    workspace_repo.mkdir()
    service = DispatchService(_settings(tmp_path), store=InMemoryDispatchStore())
    resume = AsyncMock()
    monkeypatch.setattr(service, "_resume", resume)

    dispatch = await service.start(
        "ship it",
        "u1",
        workspace={
            "workspace_id": "w1",
            "repo_root": str(workspace_repo),
            "sandbox_policy": "workspace-write",
            "default_worker": "kimi",
        },
    )

    resume.assert_awaited_once_with(dispatch)
    stored = await service.get(dispatch.id)
    assert stored is not None
    assert stored.workspace_id == "w1"
    assert stored.repo_root == str(workspace_repo)
    assert stored.sandbox_policy == "workspace-write"
    assert stored.default_worker == "kimi"

    # The same persisted row (the recovery path) builds a machine rooted at the
    # workspace, not at the backend's default checkout.
    machine = service._machine(stored)
    collaborators = machine._c
    launcher = collaborators.launcher.inner
    assert launcher._cwd == workspace_repo
    assert launcher._sandbox_policy == "workspace-write"
    assert launcher._launch_env("kimi")[SANDBOX_POLICY_ENV] == "workspace-write"
    assert collaborators.merger.inner._repo_root == workspace_repo
    # The machine must not own the fallback: it is applied while the live
    # planner is assigning lines, before the machine sees them.
    assert not hasattr(machine._config, "default_worker")
    assert collaborators.planner._default_worker == "kimi"


async def test_no_workspace_keeps_the_backend_repository(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    service = DispatchService(settings, store=InMemoryDispatchStore())
    monkeypatch.setattr(service, "_resume", AsyncMock())

    dispatch = await service.start("ship it", "u1")

    stored = await service.get(dispatch.id)
    assert stored is not None
    assert stored.repo_root == ""
    machine = service._machine(stored)
    assert machine._c.launcher.inner._cwd == settings.repo_root
    assert machine._c.planner._default_worker == ""


class _ExhaustedHeadroom:
    """Every model reads as out of quota; no local snapshot is consulted."""

    async def remaining(self, model: str) -> float:
        return 0.0


async def test_workspace_default_worker_is_applied_by_the_live_planner(
    tmp_path, monkeypatch
):
    """A line the role pool cannot place starts on the workspace default.

    ``LLMPlanner.plan`` used to raise UNASSIGNED before the dispatcher could
    apply the default, so a normal live start with one developer place and two
    lines never launched the second line.
    """
    service = DispatchService(
        _settings(tmp_path),
        store=InMemoryDispatchStore(),
        # The pool reads as exhausted, so the role policy leaves both lines
        # unassigned (quota_exhausted) and only the workspace default can
        # place them. Injected so the test never reads this machine's real
        # quota snapshot, which used to decide whether the fill path ran.
        headroom=_ExhaustedHeadroom(),
    )
    service._policy = RolePolicy(developer_pool=("kimi",))
    monkeypatch.setattr(service, "_resume", AsyncMock())

    async def complete(_prompt: str, _system: str) -> str:
        return _plan_reply("one", "two")

    monkeypatch.setattr(service, "_planner_complete", lambda _user_id: complete)

    dispatch = await service.start(
        "ship it",
        "u1",
        workspace={
            "workspace_id": "w1",
            "repo_root": str(tmp_path / "workspace-repo"),
            "default_worker": "kimi",
        },
    )
    stored = await service.get(dispatch.id)
    assert stored is not None
    machine = service._machine(stored)

    lines = await machine._c.planner.plan("ship it")

    assert [line.assigned_model for line in lines] == ["kimi", "kimi"]


async def test_relative_launch_script_stays_with_the_backend_checkout(
    tmp_path, monkeypatch
):
    """The launcher is infrastructure, not part of the selected workspace.

    The documented ``CONDUCTOR_DISPATCH_LAUNCH_SCRIPT=scripts/dispatch_launch.sh``
    is relative. Replacing repo_root with the workspace must not make the
    Worker look for that script inside the foreign checkout.
    """
    settings = replace(
        _settings(tmp_path), launch_script=Path("scripts/dispatch_launch.sh")
    )
    workspace_repo = tmp_path / "workspace-repo"
    workspace_repo.mkdir()
    service = DispatchService(settings, store=InMemoryDispatchStore())
    monkeypatch.setattr(service, "_resume", AsyncMock())

    dispatch = await service.start(
        "ship it", "u1", workspace={"workspace_id": "w1", "repo_root": str(workspace_repo)}
    )
    stored = await service.get(dispatch.id)
    assert stored is not None
    machine = service._machine(stored)
    launcher = machine._c.launcher.inner

    expected = (settings.repo_root / "scripts/dispatch_launch.sh").resolve()
    assert launcher._cwd == workspace_repo
    assert launcher._script == expected
    assert not str(launcher._script).startswith(str(workspace_repo))
