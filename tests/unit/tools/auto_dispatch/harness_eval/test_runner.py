"""Dry-run, the confirm gate, and a forced leak stop the launcher."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.tools.auto_dispatch.execution import ScriptLauncher
from src.tools.auto_dispatch.harness_eval import runner as runner_mod
from src.tools.auto_dispatch.harness_eval.report import append_record
from src.tools.auto_dispatch.harness_eval.runner import execute, regenerate
from src.tools.auto_dispatch.harness_eval.scoring import score_run
from src.tools.auto_dispatch.harness_eval.spec import SpecError, load_experiment
from src.tools.auto_dispatch.harness_eval.variants import LeakError
from src.tools.auto_dispatch.protocols import DispatchError
from src.tools.auto_dispatch.types import Line

from harness_eval_fakes import (
    DISPATCH_ID,
    RecordingRunner,
    ScriptedBackend,
    ScriptedCommands,
    build_world,
    commit_file,
    dummy_script,
    init_repo,
    make_record,
    pass_reply,
)


def _factory(backend: ScriptedBackend):
    def factory(_repo):
        return backend

    return factory


@pytest.mark.asyncio
async def test_dry_run_writes_the_plan_and_launches_nothing(tmp_path, capsys) -> None:
    world = build_world(tmp_path)
    runner = RecordingRunner()
    backend = ScriptedBackend(replies=[pass_reply()])
    commands = ScriptedCommands()
    code = await execute(
        world.experiment,
        runner=runner,
        review_backend_factory=_factory(backend),
        command_runner=commands,
        script=dummy_script(tmp_path / "launch.sh"),
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "planned_runs: 2" in captured.out
    assert "planned_reviews: 2" in captured.out
    assert "skills_unknown: true" in captured.out
    assert (world.output / "plan.json").is_file()
    assert not (world.output / "repos").exists()
    assert not (world.output / "worktrees").exists()
    assert runner.calls == 0
    assert backend.calls == 0
    assert commands.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("confirm", [None, 99])
async def test_live_confirm_gate_launches_nothing(tmp_path, confirm: int | None) -> None:
    world = build_world(tmp_path)
    runner = RecordingRunner()
    backend = ScriptedBackend(replies=[pass_reply()])
    code = await execute(
        world.experiment,
        live=True,
        confirm_runs=confirm,
        runner=runner,
        review_backend_factory=_factory(backend),
        command_runner=ScriptedCommands(),
        script=dummy_script(tmp_path / "launch.sh"),
    )
    assert code == 2
    assert runner.calls == 0
    assert backend.calls == 0
    assert not (world.output / "repos").exists()
    assert not (world.output / "worktrees").exists()


@pytest.mark.asyncio
async def test_leak_check_stops_before_launch(tmp_path, monkeypatch) -> None:
    world = build_world(tmp_path)
    runner = RecordingRunner()

    def boom(*_args, **_kwargs) -> None:
        raise LeakError("leak_check_failed")

    monkeypatch.setattr(runner_mod, "assert_no_leak", boom)
    code = await execute(
        world.experiment,
        live=True,
        confirm_runs=2,
        runner=runner,
        review_backend_factory=_factory(ScriptedBackend(replies=[pass_reply()])),
        command_runner=ScriptedCommands(),
        script=dummy_script(tmp_path / "launch.sh"),
    )
    assert code == 4
    assert runner.calls == 0
    report = json.loads((world.output / "report.json").read_text(encoding="utf-8"))
    assert report["proposal_only"] is True
    assert {item["worker_status"] for item in _runs(world)} == {"leak_check_failed"}


@pytest.mark.asyncio
async def test_regenerate_rebuilds_without_launching(tmp_path) -> None:
    world = build_world(tmp_path)
    await execute(world.experiment, script=dummy_script(tmp_path / "launch.sh"))
    plan = json.loads((world.output / "plan.json").read_text(encoding="utf-8"))
    for item in plan["runs"]:
        append_record(
            world.output,
            make_record(
                run8=item["run8"],
                sample_key=item["sample_key"],
                sample_index=item["sample_index"],
                variant_id=item["variant_id"],
                model=item["model"],
                repeat=item["repeat"],
                reviewer_model=item["reviewer_model"],
                completed=True,
                score=1.0,
                worker_status="committed",
            ),
        )
    assert await regenerate(world.output) == 0
    assert (world.output / "report.md").is_file()
    assert await regenerate(tmp_path / "absent") == 2


def test_purge_refuses_a_symlink_onto_the_source_repo(tmp_path) -> None:
    world = build_world(tmp_path)
    experiment = load_experiment(world.experiment)
    world.output.mkdir()
    (world.output / "repos").symlink_to(world.repo, target_is_directory=True)
    marker = world.repo / "README"
    assert marker.is_file()
    with pytest.raises(SpecError) as caught:
        runner_mod._purge(experiment)
    assert caught.value.field == "output_dir"
    assert marker.is_file()
    assert world.repo.is_dir()
    assert (world.output / "repos").is_symlink()


@pytest.mark.asyncio
async def test_real_launcher_keeps_launch_artifacts_inside_output_dir(
    tmp_path, monkeypatch
) -> None:
    world = build_world(
        tmp_path,
        variants=[
            {"id": "baseline", "agents_md": {"mode": "keep"}, "skills": {"mode": "keep"}},
        ],
        baseline_repeats=1,
        max_runs=1,
        max_reviews=1,
        lines=[f"{DISPATCH_ID}:line-0"],
    )
    live = tmp_path / "live-launch"
    live.mkdir()
    monkeypatch.setenv("CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR", str(live))
    script = _fake_worker(tmp_path / "fake-worker.sh")
    code = await execute(
        world.experiment,
        live=True,
        confirm_runs=1,
        review_backend_factory=_factory(ScriptedBackend(replies=[pass_reply()])),
        command_runner=ScriptedCommands(),
        script=script,
    )
    assert code == 0
    log_dir = (world.output / "launch-logs").resolve()
    for name in ("worker.prompt", "worker.log", "worker.fifo"):
        assert (log_dir / name).is_file()
    assert Path((log_dir / "where.txt").read_text(encoding="utf-8").strip()) == log_dir
    # Logs stay in output_dir, but the serial lock is the live launcher's.
    lock_dir = (log_dir / "lock-dir.txt").read_text(encoding="utf-8").strip()
    assert Path(lock_dir) == live / "locks"
    assert list(live.iterdir()) == []
    assert os.environ["CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR"] == str(live)
    rows = _runs(world)
    assert {item["worker_status"] for item in rows} == {"committed"}


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"CONDUCTOR_DISPATCH_SERIAL_LOCK_DIR": "/l/explicit", "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR": "/l/log"}, "/l/explicit"),
        ({"CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR": "/l/log", "XDG_STATE_HOME": "/l/xdg"}, "/l/log/locks"),
        ({"XDG_STATE_HOME": "/l/xdg", "HOME": "/l/home"}, "/l/xdg/conductor/dispatch-launch/locks"),
        ({"HOME": "/l/home"}, "/l/home/.local/state/conductor/dispatch-launch/locks"),
    ],
)
def test_live_serial_lock_dir_matches_the_launcher_default(environ, expected) -> None:
    assert runner_mod.live_serial_lock_dir(environ) == Path(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "status", "reason"),
    [(124, "timeout", "timeout"), (127, "env_error", "env_error")],
)
async def test_launch_classifies_timeout_and_missing_cli(tmp_path, code: int, status: str, reason: str) -> None:
    repo = init_repo(tmp_path / "repo")
    base = commit_file(repo, "README", "base\n", "base")
    dispatch_id = "eval-abcd1234"
    line = Line(
        id="s0",
        name="s0",
        task="Add note.txt with one line of text.",
        branch=f"feature/auto-dispatch-{dispatch_id}/line-s0",
        acceptance=("cmd: true",),
        write_scope=("note.txt",),
        base_commit=base,
        current_run="abcd1234",
    )
    script = tmp_path / "launch.sh"
    script.write_text(f"#!/bin/sh\nexit {code}\n", encoding="utf-8")
    script.chmod(0o700)
    launcher = ScriptLauncher(
        tmp_path / "runs",
        cwd=repo,
        worktrees_dir=tmp_path / "worktrees",
        dispatch_id=dispatch_id,
        script=script,
    )
    got, exit_code, tokens = await runner_mod._launch(launcher, line, "stepfun", 30)
    assert got == status
    assert exit_code == code
    assert tokens == 0
    score, why = score_run(
        worker_status=got,
        g2_status="not_run",
        review_status="not_run",
        preexisting_failure=False,
    )
    assert score is None
    assert why == reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "said", "status"),
    [
        (75, "", "rate_limited"),
        (76, "", "quota_exhausted"),
        (77, "", "auth_failed"),
        (79, "error: service unavailable (overloaded)", "overloaded"),
        (79, "error: connection reset by peer", "network"),
    ],
)
async def test_launch_infrastructure_classes_stay_unscored(
    tmp_path, code: int, said: str, status: str
) -> None:
    repo = init_repo(tmp_path / "repo")
    base = commit_file(repo, "README", "base\n", "base")
    dispatch_id = "eval-abcd1234"
    line = Line(
        id="s0",
        name="s0",
        task="Add note.txt with one line of text.",
        branch=f"feature/auto-dispatch-{dispatch_id}/line-s0",
        acceptance=("cmd: true",),
        write_scope=("note.txt",),
        base_commit=base,
        current_run="abcd1234",
    )
    script = tmp_path / "launch.sh"
    script.write_text(f"#!/bin/sh\necho '{said}'\nexit {code}\n", encoding="utf-8")
    script.chmod(0o700)
    launcher = ScriptLauncher(
        tmp_path / "runs",
        cwd=repo,
        worktrees_dir=tmp_path / "worktrees",
        dispatch_id=dispatch_id,
        script=script,
    )
    got, exit_code, _tokens = await runner_mod._launch(launcher, line, "stepfun", 30)
    assert (got, exit_code) == (status, code)
    score, why = score_run(
        worker_status=got,
        g2_status="not_run",
        review_status="not_run",
        preexisting_failure=False,
    )
    assert (score, why) == (None, status)


def test_a_real_worker_crash_still_scores_zero() -> None:
    assert runner_mod._failure_status(79, "crash") == "worker_failed"
    assert runner_mod._failure_status(78, "context_exhausted") == "worker_failed"
    assert runner_mod._failure_status(1) == "worker_failed"


@pytest.mark.asyncio
async def test_launch_that_never_starts_is_unscored(tmp_path) -> None:
    repo = init_repo(tmp_path / "repo")
    base = commit_file(repo, "README", "base\n", "base")
    dispatch_id = "eval-abcd1234"
    line = Line(
        id="s0",
        name="s0",
        task="Add note.txt with one line of text.",
        branch=f"feature/auto-dispatch-{dispatch_id}/line-s0",
        acceptance=("cmd: true",),
        write_scope=("note.txt",),
        base_commit=base,
        current_run="abcd1234",
    )
    launcher = ScriptLauncher(
        tmp_path / "runs",
        cwd=repo,
        worktrees_dir=tmp_path / "worktrees",
        dispatch_id=dispatch_id,
        script=tmp_path / "missing-launch.sh",
    )
    got, exit_code, _tokens = await runner_mod._launch(launcher, line, "stepfun", 30)
    assert (got, exit_code) == ("env_error", None)
    score, why = score_run(
        worker_status=got,
        g2_status="not_run",
        review_status="not_run",
        preexisting_failure=False,
    )
    assert (score, why) == (None, "env_error")


@pytest.mark.asyncio
async def test_launch_git_evidence_failure_is_unscored_not_a_crash() -> None:
    class _Broken:
        async def launch_worker(self, *_args, **_kwargs):
            raise DispatchError("Git evidence for line s0 is unavailable")

    line = Line(id="s0", name="s0", task="t", branch="b")
    got, exit_code, _tokens = await runner_mod._launch(_Broken(), line, "stepfun", 30)
    assert (got, exit_code) == ("env_error", None)


def _fake_worker(path: Path) -> Path:
    path.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'log_dir="${CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR:-}"\n'
        'if [ -z "$log_dir" ]; then\n'
        '  echo "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR is unset" >&2\n'
        "  exit 97\n"
        "fi\n"
        ': > "$log_dir/worker.prompt"\n'
        ': > "$log_dir/worker.log"\n'
        ': > "$log_dir/worker.fifo"\n'
        'printf "%s\\n" "$log_dir" > "$log_dir/where.txt"\n'
        'printf "%s\\n" "${CONDUCTOR_DISPATCH_SERIAL_LOCK_DIR:-}" > "$log_dir/lock-dir.txt"\n'
        "printf 'marker\\n' > harness-eval-marker.txt\n"
        "git add -- harness-eval-marker.txt\n"
        "git -c user.name=worker -c user.email=worker@invalid "
        "-c commit.gpgsign=false -c core.hooksPath=/dev/null commit -q -m marker\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _runs(world) -> list[dict]:
    path = world.output / "runs.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_an_explicit_serial_lock_dir_reaches_the_launcher(tmp_path, monkeypatch) -> None:
    world = build_world(
        tmp_path,
        variants=[
            {"id": "baseline", "agents_md": {"mode": "keep"}, "skills": {"mode": "keep"}},
        ],
        baseline_repeats=1,
        max_runs=1,
        max_reviews=1,
        lines=[f"{DISPATCH_ID}:line-0"],
    )
    monkeypatch.setenv("CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR", str(tmp_path / "this-shell"))
    backend_locks = tmp_path / "backend-locks"
    code = await execute(
        world.experiment,
        live=True,
        confirm_runs=1,
        review_backend_factory=_factory(ScriptedBackend(replies=[pass_reply()])),
        command_runner=ScriptedCommands(),
        script=_fake_worker(tmp_path / "fake-worker.sh"),
        serial_lock_dir=backend_locks,
    )
    assert code == 0
    log_dir = (world.output / "launch-logs").resolve()
    assert Path((log_dir / "lock-dir.txt").read_text(encoding="utf-8").strip()) == backend_locks
