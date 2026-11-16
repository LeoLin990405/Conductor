"""L2: the real push from the integration worktree, and per-dispatch workers.

The push goes to a bare repository in ``tmp_path``; nothing leaves the
machine. The planner and selector tests use fixed replies and fake roster.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from src.server.services.autonomy.roles import AgentState, RolePolicy
from src.server.services.dispatch_delivery import PUSHED, DispatchDelivery
from src.tools.auto_dispatch.approval import FileApprovalGate
from src.tools.auto_dispatch.execution import GitMerger, safe_push_remote
from src.tools.auto_dispatch.planner_reason import PlannerFailure
from src.tools.auto_dispatch.planning import (
    FileSpecArchive,
    LLMPlanner,
    validate_plan,
)
from src.tools.auto_dispatch.protocols import MergeConflictError
from src.tools.auto_dispatch.selection import (
    WORKER_NOT_ALLOWED,
    QuotaAwareModelSelector,
    UnlimitedHeadroom,
)
from src.tools.auto_dispatch.types import Dispatch, DispatchState, Line, LineStatus

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "planner_output_v1.json"


def _git(cwd: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "dispatch@example.com")
    _git(repo, "config", "user.name", "Dispatch Test")
    _git(repo, "config", "core.hooksPath", "/dev/null")
    _git(repo, "config", "core.fsmonitor", "false")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "remote", "add", "origin", str(remote))
    # The integrate branch exists, merged, as the machine leaves it.
    _git(repo, "branch", "integrate/auto-dispatch-d-push")
    return repo, remote


async def _approved(gate: FileApprovalGate) -> Dispatch:
    dispatch = Dispatch(
        id="d-push",
        goal="ship",
        user_id="u",
        created_at="now",
        integrate_branch="integrate/auto-dispatch-d-push",
        lines=[Line(id="line-0", name="n", task="t", status=LineStatus.PASSED)],
    )
    token = await gate.request_approval(dispatch)
    await gate.resolve(token, True)
    dispatch.approval_token = token
    dispatch.state = DispatchState.COMPLETE
    return dispatch


async def test_delivery_pushes_from_the_integration_worktree_and_leaves_the_checkout(
    tmp_path, checkout
) -> None:
    repo, remote = checkout
    gate = FileApprovalGate(tmp_path / "proposed")
    dispatch = await _approved(gate)
    worktrees = tmp_path / "worktrees"

    record = await DispatchDelivery(
        gate=gate,
        repo_root_for=lambda _d: repo,
        worktrees_dir_for=lambda _d: worktrees,
        reveal=None,
    ).deliver(dispatch)

    assert record["state"] == PUSHED, record
    assert _git(remote, "branch", "--list", dispatch.integrate_branch).strip()
    # The shared checkout never switched branches, and no worktree is left.
    assert _git(repo, "branch", "--show-current") == "main"
    listed = _git(repo, "worktree", "list", "--porcelain")
    assert listed.count("worktree ") == 1


async def test_push_refuses_a_remote_git_could_read_as_an_option(tmp_path, checkout) -> None:
    repo, _remote = checkout
    gate = FileApprovalGate(tmp_path / "proposed")
    dispatch = await _approved(gate)
    merger = GitMerger(repo, dispatch, gate=gate, worktrees_dir=tmp_path / "worktrees")
    for bad in ("--upload-pack=touch /tmp/x", "ext::sh -c id", "https://u:p@github.com/a/b"):
        assert not safe_push_remote(bad)
        with pytest.raises(MergeConflictError, match="unsafe remote"):
            await merger.push(dispatch.integrate_branch, dispatch.approval_token, remote=bad)
    assert safe_push_remote("https://github.com/acme/widgets.git")
    assert safe_push_remote("git@github.com:acme/widgets.git")


async def test_push_redacts_the_credential_from_gits_error(tmp_path, checkout) -> None:
    repo, _remote = checkout
    gate = FileApprovalGate(tmp_path / "proposed")
    dispatch = await _approved(gate)
    secret = "ghp_" "L2REDACTME0123456789abcdef"

    async def runner(argv, cwd):
        if argv[:1] == ["push"]:
            return 128, "", f"fatal: could not read Username: {secret}"
        return await _real(argv, cwd)

    from src.tools.auto_dispatch.execution import _git_command

    async def _real(argv, cwd):
        return await _git_command(argv, cwd)

    merger = GitMerger(
        repo, dispatch, gate=gate, worktrees_dir=tmp_path / "worktrees", runner=runner
    )
    with pytest.raises(MergeConflictError) as caught:
        await merger.push(
            dispatch.integrate_branch, dispatch.approval_token, secrets=(secret,)
        )
    assert secret not in str(caught.value)


# -- per-dispatch workers -----------------------------------------------------------


async def test_the_selector_only_picks_an_allowed_worker() -> None:
    policy = RolePolicy.default()
    pool = [name for name in policy.developer_pool if policy.may_write_code(name)]
    chosen = pool[-1]
    selector = QuotaAwareModelSelector(
        UnlimitedHeadroom(), policy=policy, allowed_workers=[chosen]
    )
    line = Line(id="a", name="a", task="t", status=LineStatus.ASSIGNED)
    assert await selector.select(line, []) == chosen
    assert await selector.select(line, [], exclude=[chosen]) is None
    assert selector.work_refusal == WORKER_NOT_ALLOWED


def _agents() -> list[AgentState]:
    return [AgentState(provider="kimi", capacity=4), AgentState(provider="glm", capacity=4)]


async def test_the_planner_places_lines_only_on_allowed_workers(tmp_path) -> None:
    reply = FIXTURE.read_text(encoding="utf-8")

    async def complete(prompt: str, system: str) -> str:
        return reply

    planner = LLMPlanner(
        complete, policy=RolePolicy.default(), agents=_agents(), allowed_workers=["glm"]
    )
    lines = await planner.plan("add a dispatch status summary endpoint")
    assert {line.assigned_model for line in lines} == {"glm"}

    payload = json.loads(reply)
    outcome = validate_plan(
        payload, policy=RolePolicy.default(), agents=_agents(), allowed_workers=["glm"]
    )
    assert {item.provider for item in outcome.assignment.assignments} == {"glm"}

    nobody = LLMPlanner(
        complete, policy=RolePolicy.default(), agents=_agents(), allowed_workers=["minimax"]
    )
    with pytest.raises(PlannerFailure):
        await nobody.plan("add a dispatch status summary endpoint")


async def test_an_archived_plan_is_reloaded_on_resume_without_the_model(tmp_path) -> None:
    reply = FIXTURE.read_text(encoding="utf-8")
    calls: list[str] = []
    stamped: list[str] = []

    async def complete(prompt: str, system: str) -> str:
        calls.append(prompt)
        return reply

    async def on_archived(ref: str) -> None:
        stamped.append(ref)

    archive = FileSpecArchive(tmp_path / "specs")
    first = LLMPlanner(
        complete,
        policy=RolePolicy.default(),
        archive=archive,
        on_archived=on_archived,
    )
    planned = await first.plan("add a dispatch status summary endpoint")
    (ref,) = stamped
    assert ref.startswith("spec:") and len(calls) == 1

    resumed = LLMPlanner(
        complete, policy=RolePolicy.default(), archive=archive, resume_spec_ref=ref
    )
    lines = await resumed.plan(ref)
    assert len(calls) == 1, "the model is not asked again"
    assert [line.to_dict() for line in lines] == [line.to_dict() for line in planned]

    # A goal that only looks like a reference is planned as a goal.
    stranger = LLMPlanner(complete, policy=RolePolicy.default(), archive=archive)
    await stranger.plan(ref)
    assert len(calls) == 2
    assert await archive.load("spec:../../etc") is None
