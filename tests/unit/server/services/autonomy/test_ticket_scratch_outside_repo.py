"""Ticket scratch lives in the execution runtime's writable area, outside Git."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from src.llms.native_cli.isolation import prepare_process, worker_isolation
from src.llms.native_cli.process import CliInvocation, run_process
from src.server.services.autonomy.git_delivery import (
    git_command_runner,
    uncommitted_git_summary,
)
from src.server.services.autonomy.worker import (
    TICKET_SCRATCH_PLACEHOLDER,
    bind_ticket_scratch_prompt,
    ensure_ticket_scratch,
    ticket_scratch_dir,
    ticket_scratch_for_prompt,
    ticket_scratch_host_root,
    worker_prompt,
)


@pytest.fixture(autouse=True)
def _trusted_temp_repo_runner():
    """Keep filesystem fixtures local; production never uses this host runner."""

    def runner(argv, *, cwd, timeout, env):
        return subprocess.run(
            argv,
            cwd=cwd,
            timeout=timeout,
            env=env,
            capture_output=True,
            check=False,
        )

    with git_command_runner(runner):
        yield


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Test Worker")
    _git(path, "config", "user.email", "worker@example.invalid")
    (path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-q", "-m", "baseline")
    return path


def _isolation_available() -> bool:
    system = os.uname().sysname
    if system == "Darwin":
        return bool(shutil.which("sandbox-exec"))
    if system == "Linux":
        return bool(shutil.which("bwrap"))
    return False


def test_unisolated_prompt_uses_scratch_placeholder_not_host_autonomy_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolation = tmp_path / "isolation"
    home = tmp_path / "conductor-home"
    monkeypatch.setenv("CONDUCTOR_ISOLATION_ROOT", str(isolation))
    monkeypatch.setenv("CONDUCTOR_HOME", str(home))
    ticket_id = "abcdef12-3456-7890-abcd-ef1234567890"

    assert ticket_scratch_for_prompt(ticket_id) == TICKET_SCRATCH_PLACEHOLDER
    # Host fallback must not be <isolation-root>/autonomy/<ticket>.
    assert ticket_scratch_dir(ticket_id) == (home / "autonomy" / "abcdef12").resolve()
    assert ticket_scratch_host_root() == home.resolve()

    prompt = worker_prompt(
        {"ticket_id": ticket_id, "title": "Scratch", "scope": "Write notes"},
        {"config": {}},
    )
    assert TICKET_SCRATCH_PLACEHOLDER in prompt
    assert f"`{TICKET_SCRATCH_PLACEHOLDER}/`" in prompt
    assert ".orchestrator/autonomy/" not in prompt
    assert str(isolation / "autonomy") not in prompt


def test_bind_uses_explicit_docker_home_without_host_mkdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "conductor-home"))
    ticket_id = "12345678-aaaa-bbbb-cccc-ddddeeeeffff"
    prompt = worker_prompt(
        {"ticket_id": ticket_id, "title": "Docker", "scope": "Scratch"},
        {"config": {}},
    )

    bound, scratch = bind_ticket_scratch_prompt(
        prompt, ticket_id, home="/home/worker", create=False
    )

    assert scratch == Path("/home/worker/autonomy/12345678")
    assert str(scratch) in bound
    assert TICKET_SCRATCH_PLACEHOLDER not in bound
    assert not scratch.exists()


def test_host_fallback_scratch_writes_leave_clean_repo_delivery_git_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unisolated fallback still keeps delivery git clean (outside the repo)."""
    monkeypatch.delenv("CONDUCTOR_ISOLATION_ROOT", raising=False)
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "conductor-home"))
    workspace = _repo(tmp_path / "clean-repo")
    ticket_id = "59abcdef-0000-4000-8000-000000000059"

    assert uncommitted_git_summary(str(workspace)) == ""

    scratch = ensure_ticket_scratch(ticket_id)
    assert workspace.resolve() not in scratch.resolve().parents
    (scratch / "notes.md").write_text("worker notes\n", encoding="utf-8")
    pgdata = scratch / "pgdata"
    pgdata.mkdir()
    (pgdata / "PG_VERSION").write_text("16\n", encoding="utf-8")

    assert uncommitted_git_summary(str(workspace)) == ""
    bound, bound_scratch = bind_ticket_scratch_prompt(
        worker_prompt(
            {"ticket_id": ticket_id, "title": "Done", "scope": "Done"},
            {"config": {}},
        ),
        ticket_id,
    )
    assert bound_scratch == scratch
    assert str(scratch) in bound


@pytest.mark.skipif(not _isolation_available(), reason="OS worker isolation unavailable")
@pytest.mark.asyncio
async def test_isolated_scratch_is_writable_and_keeps_delivery_git_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: scratch must be under private HOME, not <root>/autonomy."""
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / "conductor-home"))
    monkeypatch.setenv("CONDUCTOR_ISOLATION_ROOT", str(tmp_path / "isolation-env"))
    workspace = _repo(tmp_path / "workspace")
    isolation_root = tmp_path / "worker-state"
    ticket_id = "iso12345-0000-4000-8000-000000000001"

    prompt = worker_prompt(
        {"ticket_id": ticket_id, "title": "Isolated", "scope": "Write scratch"},
        {"config": {}},
        native=True,
    )
    assert TICKET_SCRATCH_PLACEHOLDER in prompt

    with worker_isolation(
        workspace_id="workspace-scratch",
        root=isolation_root,
        workdir=workspace,
        env_names=frozenset(),
    ) as isolation:
        bound, scratch = bind_ticket_scratch_prompt(prompt, ticket_id)
        assert scratch == (isolation.home / "autonomy" / ticket_id[:8]).resolve()
        assert scratch.is_dir()
        assert isolation_root.resolve() in scratch.resolve().parents
        assert scratch.resolve().is_relative_to(isolation.home.resolve())
        assert not scratch.resolve().is_relative_to(workspace.resolve())
        assert str(scratch) in bound
        assert TICKET_SCRATCH_PLACEHOLDER not in bound

        marker = scratch / "from-sandbox.txt"
        script = f'printf isolated > "{marker}"'
        result = await run_process(
            CliInvocation(argv=("/bin/sh", "-c", script), cwd=str(workspace))
        )
        assert result.returncode == 0
        assert marker.read_text(encoding="utf-8") == "isolated"

        # The rejected layout (isolation-root/autonomy/…) is outside the
        # sandbox write set and must fail while private-home scratch works.
        rejected = isolation_root / "autonomy" / ticket_id[:8] / "denied.txt"
        rejected.parent.mkdir(parents=True, exist_ok=True)
        denied = await run_process(
            CliInvocation(
                argv=("/bin/sh", "-c", f'printf no > "{rejected}"'),
                cwd=str(workspace),
            ),
            check=False,
        )
        assert denied.returncode != 0
        assert not rejected.exists()

        assert uncommitted_git_summary(str(workspace)) == ""


@pytest.mark.skipif(not _isolation_available(), reason="OS worker isolation unavailable")
def test_prepare_process_env_homes_ticket_scratch(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ticket_id = "prep9999-0000-4000-8000-000000000002"
    with worker_isolation(
        workspace_id="workspace-prep",
        root=tmp_path / "state",
        workdir=workspace,
        env_names=frozenset(),
    ) as isolation:
        scratch = ensure_ticket_scratch(ticket_id)
        argv, env, cwd = prepare_process(("/bin/true",), str(workspace), None)
        assert env is not None
        assert env["HOME"] == str(isolation.home)
        assert str(scratch).startswith(env["HOME"] + os.sep)
        assert cwd == str(workspace.resolve())
        assert argv  # sandbox wrapper present when isolation is available
