"""N57: a line whose worker exited before G2 ever ran can be reassigned.

The real case: the worker exits with ``uncommitted_changes`` (or
``no_new_commit``) before G2 runs, so the line's G2 cell is still the blank
``pending`` shell. Once the work is committed on the line branch, ``dispatch
reassign`` answered ``handoff_incomplete``: the handoff read the pending cell
as a G2 record, pinned the commit to ``unknown`` and so had no changed files.
``unblock`` answered ``already_at_checkpoint``, so there was no way out.

The handoff for such a line now records ``test_command`` as
``none (exited before G2)`` and uses the branch diff (head commit, commit
count and changed files) as its evidence. A branch with nothing committed past
base still refuses with ``handoff_incomplete``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.tools.auto_dispatch import LineStatus
from src.tools.auto_dispatch.machine import (
    PRE_G2_TEST_COMMAND,
    WORKER_EXITED,
    _reassign_handoff,
)
from src.tools.auto_dispatch.operator import unblock_refusal
from tests.unit.server.services.test_dispatch_unwedge import (
    ScriptedLauncher,
    _drive,
    _setup,
)
from tests.unit.tools.auto_dispatch.test_gate_recovery import _line

pytestmark = pytest.mark.asyncio

BRANCH = "dispatch/line-a"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(repo),
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        },
    )
    return done.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README").write_text("base\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", BRANCH)
    return repo, base


def _commit_on_branch(repo: Path, count: int) -> str:
    _git(repo, "checkout", "-q", BRANCH)
    for index in range(count):
        (repo / f"work{index}.py").write_text(f"x = {index}\n")
        _git(repo, "add", f"work{index}.py")
        _git(repo, "commit", "-q", "-m", f"work {index}")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return head


async def _parked(tmp_path: Path, outcome: str = "dirty"):
    _repo_path, base = _repo(tmp_path)
    launcher = ScriptedLauncher({"a": [outcome]})
    line = _line(
        "a",
        status=LineStatus.ASSIGNED,
        assigned_model="deepseek",
        base_commit=base,
        branch=BRANCH,
    )
    store, _launcher, gates, service, row = await _setup(
        tmp_path, [line], launcher=launcher
    )
    # Read the real Git facts, not the suite's complete fake handoff.
    del gates.handoff_facts
    await _drive(service, row.id)
    parked = await store.get(row.id)
    assert parked is not None
    return store, launcher, gates, service, parked, _repo_path


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [("dirty", "uncommitted_changes"), ("no_commit", "no_new_commit")],
)
async def test_a_pre_g2_exit_is_reassignable_once_the_work_is_committed(
    tmp_path: Path, outcome: str, reason: str
) -> None:
    store, launcher, gates, service, parked, repo = await _parked(tmp_path, outcome)
    line = parked.line("a")
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == reason
    snap = parked.gates["lines"]["a"]
    assert snap["gates"]["G2"]["result"] == "pending"
    assert WORKER_EXITED in snap
    assert unblock_refusal(parked, line) == "already_at_checkpoint"

    head = _commit_on_branch(repo, 2)
    reassigned = await service.reassign(parked.id, "a", "stepfun")
    moved = reassigned.line("a")
    assert moved.status is LineStatus.ASSIGNED
    assert moved.assigned_model == "stepfun"
    assert moved.attempts == 1
    snap = reassigned.gates["lines"]["a"]
    machine = snap["machine"]
    assert machine["developer_provider"] == "stepfun"
    assert snap["handoff_ref"]
    # The outgoing author made the branch commits: never its reviewer.
    assert "deepseek" in machine.get("contributors", [])

    built = await _reassign_handoff(reassigned, moved, gates)
    assert built.complete
    assert built.test_command == PRE_G2_TEST_COMMAND
    assert built.commit == head
    assert set(built.files) == {"work0.py", "work1.py"}
    assert "2 commit(s)" in built.results
    assert head[:12] in built.results


@pytest.mark.parametrize("outcome", ["dirty", "no_commit"])
async def test_a_pre_g2_exit_with_nothing_committed_still_refuses(
    tmp_path: Path, outcome: str
) -> None:
    _store, _launcher, _gates, service, parked, _repo_path = await _parked(tmp_path, outcome)
    with pytest.raises(ValueError, match="^handoff_incomplete$"):
        await service.reassign(parked.id, "a", "stepfun")
    after = await _store.get(parked.id)
    assert after.line("a").assigned_model == "deepseek"
    assert after.line("a").attempts == 0


async def test_the_liveness_r_exit_reassign_takes_the_same_pre_g2_handoff(
    tmp_path: Path,
) -> None:
    """``recover``/the sweep's R-EXIT row reassigns the line the same way.

    Before N57 the swap was refused as ``handoff_incomplete`` and the step was
    recorded as "no eligible author could be assigned".
    """
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock

    from src.tools.auto_dispatch import DispatchMachine, MachineConfig
    from src.tools.auto_dispatch.liveness import modes
    from src.tools.auto_dispatch.liveness import record as liveness_record
    from src.tools.auto_dispatch.operator import apply_recover

    store, _launcher, gates, service, parked, repo = await _parked(tmp_path)
    head = _commit_on_branch(repo, 1)
    line = parked.line("a")
    # ``dispatch recover`` on the parked line: the sweep runs its R-EXIT row.
    assert apply_recover(parked, line) is None
    await store.save(parked)
    machine = DispatchMachine(
        service._machine_factory(parked)._c,
        config=MachineConfig(liveness_mode=modes.ENFORCE),
    )
    machine._select_with_retry = AsyncMock(return_value="stepfun")
    await machine._sweep_liveness(
        parked, now=datetime.now(timezone.utc) + timedelta(minutes=1)
    )
    saved = await store.get(parked.id)
    moved = saved.line("a")
    snap = saved.gates["lines"]["a"]
    last = liveness_record.load(snap).history[-1]
    assert (last.action, last.result) == ("reassign", liveness_record.APPLIED), last
    assert moved.assigned_model == "stepfun"
    assert moved.attempts == 1
    assert snap["machine"]["developer_provider"] == "stepfun"
    # R-EXIT relaunches on a fresh branch from base (unchanged by N57); the
    # outgoing author's committed branch is kept, never rewritten.
    assert moved.branch != BRANCH
    assert _git(repo, "rev-parse", BRANCH) == head
