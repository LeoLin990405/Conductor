"""N37 harness-eval follow-ups: import noise, wall budget, symlinks, resume, torn files."""

from __future__ import annotations

import errno
import fcntl
import json
import subprocess
import sys

import pytest

from src.tools.auto_dispatch.harness_eval import runner as runner_mod
from src.tools.auto_dispatch.harness_eval import safe_io
from src.tools.auto_dispatch.harness_eval.budget import Ledger
from src.tools.auto_dispatch.harness_eval.report import (
    ISOLATION_NOTE,
    append_record,
    load_records,
)
from src.tools.auto_dispatch.harness_eval.runner import execute

from harness_eval_fakes import (
    REPO_ROOT,
    RecordingRunner,
    ScriptedBackend,
    ScriptedCommands,
    World,
    budget_spec,
    build_world,
    dummy_script,
    make_record,
    pass_reply,
)


def _factory(backend: ScriptedBackend):
    def factory(_repo):
        return backend

    return factory


async def _live(world: World, runner: RecordingRunner | None = None, **kwargs) -> int:
    return await execute(
        world.experiment,
        live=True,
        confirm_runs=2,
        script=dummy_script(world.root / "launch.sh"),
        runner=runner or RecordingRunner(),
        review_backend_factory=_factory(ScriptedBackend(replies=[pass_reply()])),
        command_runner=ScriptedCommands(),
        **kwargs,
    )


async def _planned_run8s(world: World) -> list[str]:
    assert await execute(world.experiment) == 0
    plan = json.loads((world.output / "plan.json").read_text(encoding="utf-8"))
    return [item["run8"] for item in plan["runs"]]


def _rows(world: World) -> list[dict]:
    return [
        json.loads(line)
        for line in (world.output / "runs.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# --- import side effect ------------------------------------------------------


def test_the_script_prints_no_import_time_log_lines() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "harness_eval.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert result.returncode == 0
    assert result.stdout.startswith("usage: harness_eval")
    assert "Generating tool module" not in result.stdout
    assert "Generating tool module" not in result.stderr


# --- wall-time budget --------------------------------------------------------


def test_a_run_is_admitted_only_when_its_worst_case_fits_the_wall(tmp_path) -> None:
    clock = {"now": 0.0}
    budget = budget_spec(max_wall_minutes=2, worker_timeout_seconds=60)
    ledger = Ledger(tmp_path / "ledger.jsonl", budget, clock=lambda: clock["now"])
    assert ledger.reserve("run", "aaaaaaa1", "stepfun").ok
    clock["now"] = 61.0  # 61s elapsed + 60s worst case > 120s
    refused = ledger.reserve("run", "aaaaaaa2", "stepfun")
    assert refused.ok is False
    assert refused.reason == "max_wall_minutes"
    clock["now"] = 60.0  # exactly fits
    assert ledger.reserve("run", "aaaaaaa3", "stepfun").ok


def test_a_worker_timeout_longer_than_the_wall_refuses_the_first_run(tmp_path) -> None:
    budget = budget_spec(max_wall_minutes=1, worker_timeout_seconds=90)
    ledger = Ledger(tmp_path / "ledger.jsonl", budget, clock=lambda: 0.0)
    refused = ledger.reserve("run", "aaaaaaa1", "stepfun")
    assert refused.reason == "max_wall_minutes"
    # A review has no known worst case and is still admitted.
    assert ledger.reserve("review", "aaaaaaa1", "codex").ok


# --- torn last lines ---------------------------------------------------------


def test_a_torn_last_ledger_line_is_skipped_and_the_next_append_is_whole(tmp_path) -> None:
    path = tmp_path / "ledger.jsonl"
    budget = budget_spec(max_runs=4)
    Ledger(path, budget, clock=lambda: 1.0).reserve("run", "aaaaaaa1", "stepfun")
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"event": "reserve", "kind": "ru')
    resumed = Ledger(path, budget, clock=lambda: 2.0)
    assert resumed.state().runs_reserved == 1
    assert resumed.reserve("run", "aaaaaaa2", "stepfun").ok
    reopened = Ledger(path, budget, clock=lambda: 3.0)
    assert [item["run8"] for item in reopened.events] == ["aaaaaaa1", "aaaaaaa2"]


def test_a_torn_last_runs_line_is_skipped(tmp_path) -> None:
    append_record(tmp_path, make_record(run8="aaaaaaa1"))
    with (tmp_path / "runs.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"run8": "bbbb')
    append_record(tmp_path, make_record(run8="aaaaaaa3"))
    assert [item.run8 for item in load_records(tmp_path)] == ["aaaaaaa1", "aaaaaaa3"]


@pytest.mark.asyncio
async def test_a_resume_with_a_torn_ledger_still_writes_the_report(tmp_path) -> None:
    world = build_world(tmp_path)
    assert await _live(world) == 0
    with (world.output / "ledger.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"event": "sett')
    (world.output / "report.json").unlink()
    assert await _live(world) == 0
    assert (world.output / "report.json").is_file()


@pytest.mark.asyncio
async def test_an_oserror_saving_worker_diff_still_writes_the_report(tmp_path) -> None:
    world = build_world(tmp_path)
    run8s = await _planned_run8s(world)
    for run8 in run8s:
        # A directory where the file should go: writing it raises EISDIR.
        (world.output / "runs" / run8 / "worker.diff").mkdir(parents=True)
    runner = RecordingRunner()
    assert await _live(world, runner) == 0
    assert runner.calls == 2
    assert (world.output / "report.json").is_file()
    assert {row["worker_status"] for row in _rows(world)} == {"committed"}


# --- symlinks ----------------------------------------------------------------


def test_safe_io_refuses_a_symlinked_leaf_and_parent(tmp_path) -> None:
    root = tmp_path / "out"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    (root / "leaf").symlink_to(outside)
    with pytest.raises(OSError):
        safe_io.write_text(root, root / "leaf", "x")
    (root / "parent").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(safe_io.UnsafePathError):
        safe_io.write_text(root, root / "parent" / "outside.txt", "x")
    assert outside.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.asyncio
async def test_worker_diff_and_g2_log_symlinks_are_not_written_through(tmp_path) -> None:
    world = build_world(tmp_path)
    run8s = await _planned_run8s(world)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    for run8 in run8s:
        run_dir = world.output / "runs" / run8
        run_dir.mkdir(parents=True)
        (run_dir / "worker.diff").symlink_to(outside)
        for index in range(3):
            (run_dir / f"g2-{index}.log").symlink_to(outside)
    assert await _live(world) == 0
    assert outside.read_text(encoding="utf-8") == "keep\n"
    assert (world.output / "report.json").is_file()


@pytest.mark.asyncio
async def test_a_symlinked_closure_file_is_a_setup_failure(tmp_path) -> None:
    world = build_world(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    repos = world.output / "repos"
    repos.mkdir(parents=True)
    (repos / f"{world.base[:12]}.closure").symlink_to(outside)
    runner = RecordingRunner()
    assert await _live(world, runner) == 5
    assert runner.calls == 0
    assert outside.read_text(encoding="utf-8") == "keep\n"
    assert (world.output / "report.json").is_file()


@pytest.mark.asyncio
async def test_a_symlinked_repos_runs_parent_is_a_setup_failure(tmp_path) -> None:
    world = build_world(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (world.output / "repos").mkdir(parents=True)
    (world.output / "repos" / "runs").symlink_to(elsewhere, target_is_directory=True)
    runner = RecordingRunner()
    assert await _live(world, runner) == 5
    assert runner.calls == 0
    assert list(elsewhere.iterdir()) == []
    assert {row["worker_status"] for row in _rows(world)} == {"setup_failed"}


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [False, True])
async def test_a_symlinked_live_lock_is_refused(tmp_path, live: bool) -> None:
    world = build_world(tmp_path)
    world.output.mkdir(parents=True)
    target = tmp_path / "planted.lock"
    (world.output / "live.lock").symlink_to(target)
    if live:
        code = await _live(world)
    else:
        code = await execute(world.experiment)
    assert code == 2
    assert not target.exists()
    assert not (world.output / "plan.json").exists()


# --- dry run lock ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_dry_run_takes_live_lock(tmp_path) -> None:
    world = build_world(tmp_path)
    world.output.mkdir(parents=True)
    with (world.output / "live.lock").open("a", encoding="utf-8") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        code = await execute(world.experiment)
    assert code == 2
    assert not (world.output / "plan.json").exists()
    assert await execute(world.experiment) == 0
    assert (world.output / "plan.json").is_file()


# --- setup_failed on resume --------------------------------------------------


@pytest.mark.asyncio
async def test_a_transient_setup_failure_is_retried_on_resume(tmp_path, monkeypatch) -> None:
    world = build_world(tmp_path)
    real = runner_mod.prepare_run_repo

    def full_disk(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(runner_mod, "prepare_run_repo", full_disk)
    first = RecordingRunner()
    assert await _live(world, first) == 5
    assert first.calls == 0
    assert {row["worker_status"] for row in _rows(world)} == {"setup_failed"}

    monkeypatch.setattr(runner_mod, "prepare_run_repo", real)
    second = RecordingRunner()
    assert await _live(world, second) == 0
    assert second.calls == 2
    statuses = {item.run8: item.worker_status for item in load_records(world.output)}
    assert set(statuses.values()) == {"committed"}
    report = json.loads((world.output / "report.json").read_text(encoding="utf-8"))
    assert report["unscored"] == []


# --- report states the isolation limit ---------------------------------------


@pytest.mark.asyncio
async def test_the_report_states_there_is_no_os_sandbox(tmp_path) -> None:
    world = build_world(tmp_path)
    assert await _live(world) == 0
    report = json.loads((world.output / "report.json").read_text(encoding="utf-8"))
    assert report["isolation"] == {"os_sandbox": False, "note": ISOLATION_NOTE}
    markdown = (world.output / "report.md").read_text(encoding="utf-8")
    assert ISOLATION_NOTE in markdown
    assert "OS sandbox" in markdown
