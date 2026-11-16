"""Live-path acceptance: fakes only, local git, no provider CLI."""

from __future__ import annotations

import json
import time

import pytest

from src.tools.auto_dispatch.harness_eval.runner import execute
from src.tools.auto_dispatch.harness_eval.spec import load_experiment

from harness_eval_fakes import (
    BLOCKER_TOKEN,
    DISPATCH_ID,
    EXPERIMENT_ID,
    MODEL,
    VARIANT_ID,
    VERDICT_TOKEN,
    WORKER_TOKEN,
    RecordingRunner,
    ScriptedBackend,
    ScriptedCommands,
    World,
    build_world,
    dummy_script,
    pass_reply,
    source_fingerprint,
)


def _factory(backend: ScriptedBackend):
    def factory(_repo):
        return backend

    return factory


async def _execute(world: World, **kwargs) -> int:
    script = kwargs.pop("script", None) or dummy_script(world.root / "launch.sh")
    return await execute(world.experiment, script=script, **kwargs)


def _records(world: World) -> list[dict]:
    return [
        json.loads(line)
        for line in (world.output / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def _visible(argv: list[str]) -> str:
    return "\n".join(argv[2:])


@pytest.mark.asyncio
async def test_live_replay_is_blinded_and_leaves_the_source_untouched(tmp_path) -> None:
    world = build_world(tmp_path)
    before = source_fingerprint(world.repo)
    runner = RecordingRunner()
    backend = ScriptedBackend(replies=[pass_reply()])
    commands = ScriptedCommands()
    code = await _execute(
        world,
        live=True,
        confirm_runs=2,
        purge=True,
        runner=runner,
        review_backend_factory=_factory(backend),
        command_runner=commands,
    )
    assert code == 0, _records(world) if (world.output / "runs.jsonl").exists() else "no runs"
    assert runner.calls == 2
    assert backend.calls == 2
    assert commands.calls == 4
    assert source_fingerprint(world.repo) == before
    assert not (world.output / "repos").exists()
    assert not (world.output / "worktrees").exists()
    for argv in runner.argvs:
        assert argv[1] == MODEL
        visible = _visible(argv)
        assert DISPATCH_ID not in visible
        assert VARIANT_ID not in visible
        assert MODEL not in visible
        assert EXPERIMENT_ID not in visible
        assert VERDICT_TOKEN not in visible
        assert "feature/auto-dispatch-eval-" in visible
    for prompt in backend.prompts:
        assert "note.txt" in prompt
        assert "diff --git a/AGENTS.md" not in prompt
        assert BLOCKER_TOKEN not in prompt
    report = json.loads((world.output / "report.json").read_text(encoding="utf-8"))
    markdown = (world.output / "report.md").read_text(encoding="utf-8")
    runs = (world.output / "runs.jsonl").read_text(encoding="utf-8")
    assert report["proposal_only"] is True
    assert report["source"] == "offline-harness-eval"
    assert report["skills_unknown"] is True
    assert report["incomplete"] is False
    assert {item["mean_score"] for item in report["variants"]} == {1.0}
    for secret in (WORKER_TOKEN, VERDICT_TOKEN, BLOCKER_TOKEN):
        assert secret not in markdown
        assert secret not in runs
        assert secret not in json.dumps(report)
    assert all(item["worker_status"] == "committed" for item in _records(world))
    assert all(item["g2_status"] == "pass" for item in _records(world))
    assert all(item["review_passed"] is True for item in _records(world))


@pytest.mark.asyncio
async def test_unparseable_review_is_excluded_from_the_mean(tmp_path) -> None:
    world = build_world(tmp_path)
    backend = ScriptedBackend(replies=["this is not a verdict"])
    code = await _execute(
        world,
        live=True,
        confirm_runs=2,
        runner=RecordingRunner(),
        review_backend_factory=_factory(backend),
        command_runner=ScriptedCommands(),
    )
    assert code == 0
    assert backend.calls == 4
    rows = _records(world)
    assert {item["unscored_reason"] for item in rows} == {"review_unparseable"}
    assert all(item["score"] is None for item in rows)
    report = json.loads((world.output / "report.json").read_text(encoding="utf-8"))
    assert all(item["n_scored"] == 0 for item in report["variants"])
    assert BLOCKER_TOKEN not in (world.output / "report.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_preexisting_failure_scores_the_review_only(tmp_path) -> None:
    world = build_world(tmp_path)
    backend = ScriptedBackend(replies=[pass_reply()])
    code = await _execute(
        world,
        live=True,
        confirm_runs=2,
        runner=RecordingRunner(),
        review_backend_factory=_factory(backend),
        command_runner=ScriptedCommands(codes=[1]),
    )
    assert code == 0
    assert backend.calls == 2
    rows = _records(world)
    assert {item["score"] for item in rows} == {0.5}
    assert {item["preexisting_failure"] for item in rows} == {True}
    assert {item["base_g2_status"] for item in rows} == {"fail"}


@pytest.mark.asyncio
async def test_base_env_error_is_unscored_and_skips_review(tmp_path) -> None:
    world = build_world(tmp_path)
    backend = ScriptedBackend(replies=[pass_reply()])
    code = await _execute(
        world,
        live=True,
        confirm_runs=2,
        runner=RecordingRunner(),
        review_backend_factory=_factory(backend),
        command_runner=ScriptedCommands(codes=[127]),
    )
    assert code == 0
    assert backend.calls == 0
    rows = _records(world)
    assert {item["unscored_reason"] for item in rows} == {"env_error"}
    assert {item["score"] for item in rows} == {None}


@pytest.mark.asyncio
async def test_rate_limit_and_no_commit_do_not_call_review(tmp_path) -> None:
    limited = build_world(tmp_path / "limited")
    backend = ScriptedBackend(replies=[pass_reply()])
    code = await _execute(
        limited,
        live=True,
        confirm_runs=2,
        runner=RecordingRunner(action="rate"),
        review_backend_factory=_factory(backend),
        command_runner=ScriptedCommands(),
    )
    assert code == 0
    assert backend.calls == 0
    assert {item["worker_status"] for item in _records(limited)} == {"rate_limited"}

    quiet = build_world(tmp_path / "quiet")
    quiet_backend = ScriptedBackend(replies=[pass_reply()])
    code = await _execute(
        quiet,
        live=True,
        confirm_runs=2,
        runner=RecordingRunner(action="noop"),
        review_backend_factory=_factory(quiet_backend),
        command_runner=ScriptedCommands(),
    )
    assert code == 0
    assert quiet_backend.calls == 0
    rows = _records(quiet)
    assert {item["worker_status"] for item in rows} == {"no_new_commit"}
    assert {item["score"] for item in rows} == {0.0}


@pytest.mark.asyncio
async def test_planned_runs_above_the_cap_are_rejected_before_launch(tmp_path) -> None:
    world = build_world(tmp_path, baseline_repeats=3, max_runs=3)
    experiment = load_experiment(world.experiment)
    assert experiment.budget.max_runs == 3
    runner = RecordingRunner()
    code = await _execute(
        world,
        live=True,
        confirm_runs=4,
        runner=runner,
        review_backend_factory=_factory(ScriptedBackend(replies=[pass_reply()])),
        command_runner=ScriptedCommands(),
    )
    assert code == 2
    assert runner.calls == 0
    assert not (world.output / "repos").exists()


@pytest.mark.asyncio
async def test_unsettled_reserve_limits_the_resume(tmp_path) -> None:
    world = build_world(
        tmp_path,
        variants=[
            {"id": "baseline", "agents_md": {"mode": "keep"}, "skills": {"mode": "keep"}},
        ],
        baseline_repeats=2,
        max_runs=2,
        max_reviews=2,
        lines=[f"{DISPATCH_ID}:line-0"],
    )
    world.output.mkdir()
    (world.output / "ledger.jsonl").write_text(
        json.dumps(
            {
                "at": time.time(),
                "event": "reserve",
                "kind": "run",
                "model": MODEL,
                "run8": "spentslot",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    runner = RecordingRunner()
    code = await _execute(
        world,
        live=True,
        confirm_runs=2,
        runner=runner,
        review_backend_factory=_factory(ScriptedBackend(replies=[pass_reply()])),
        command_runner=ScriptedCommands(),
    )
    assert code == 3
    assert runner.calls == 1
    report = json.loads((world.output / "report.json").read_text(encoding="utf-8"))
    assert report["incomplete"] is True
    assert report["proposal_only"] is True
    statuses = [item["worker_status"] for item in _records(world)]
    assert statuses.count("budget_skipped") == 1
    assert "committed" in statuses


@pytest.mark.asyncio
async def test_rerun_skips_completed_work_and_rejects_a_changed_file(tmp_path) -> None:
    world = build_world(tmp_path)
    runner = RecordingRunner()
    kwargs = {
        "live": True,
        "confirm_runs": 2,
        "runner": runner,
        "review_backend_factory": _factory(ScriptedBackend(replies=[pass_reply()])),
        "command_runner": ScriptedCommands(),
    }
    assert await _execute(world, **kwargs) == 0
    assert runner.calls == 2
    assert await _execute(world, **kwargs) == 0
    assert runner.calls == 2

    document = json.loads(world.experiment.read_text(encoding="utf-8"))
    document["samples"]["seed"] = 2
    world.experiment.write_text(json.dumps(document), encoding="utf-8")
    code = await _execute(world, **kwargs)
    assert code == 2
    assert runner.calls == 2


def _one_variant_world(tmp_path, **kwargs):
    options = {
        "variants": [
            {"id": "baseline", "agents_md": {"mode": "keep"}, "skills": {"mode": "keep"}},
        ],
        "baseline_repeats": 1,
        "max_runs": 1,
        "max_reviews": 2,
        "lines": [f"{DISPATCH_ID}:line-0"],
    }
    options.update(kwargs)
    return build_world(tmp_path, **options)


@pytest.mark.asyncio
async def test_g2_timeout_stays_unscored(tmp_path) -> None:
    world = _one_variant_world(tmp_path)
    backend = ScriptedBackend(replies=[pass_reply()])
    code = await _execute(
        world,
        live=True,
        confirm_runs=1,
        runner=RecordingRunner(),
        review_backend_factory=_factory(backend),
        command_runner=ScriptedCommands(codes=[124]),
    )
    assert code == 0
    assert backend.calls == 0
    rows = _records(world)
    assert {item["g2_status"] for item in rows} == {"timeout"}
    assert {item["base_g2_status"] for item in rows} == {"timeout"}
    assert {item["score"] for item in rows} == {None}
    assert {item["unscored_reason"] for item in rows} == {"timeout"}


@pytest.mark.asyncio
async def test_review_retry_reserves_every_backend_call(tmp_path) -> None:
    allowed = _one_variant_world(tmp_path / "allowed", max_reviews=2)
    retry = ScriptedBackend(replies=["not a verdict", pass_reply()])
    assert await _execute(
        allowed,
        live=True,
        confirm_runs=1,
        runner=RecordingRunner(),
        review_backend_factory=_factory(retry),
        command_runner=ScriptedCommands(),
    ) == 0
    assert retry.calls == 2
    assert _review_reserves(allowed) == 2
    assert {item["score"] for item in _records(allowed)} == {1.0}

    blocked = _one_variant_world(tmp_path / "blocked", max_reviews=1)
    once = ScriptedBackend(replies=["not a verdict", pass_reply()])
    assert await _execute(
        blocked,
        live=True,
        confirm_runs=1,
        runner=RecordingRunner(),
        review_backend_factory=_factory(once),
        command_runner=ScriptedCommands(),
    ) == 3
    assert once.calls == 1
    assert _review_reserves(blocked) == 1
    rows = _records(blocked)
    assert {item["unscored_reason"] for item in rows} == {"budget_skipped"}
    assert {item["score"] for item in rows} == {None}
    assert {item["completed"] for item in rows} == {False}
    blocked_report = json.loads((blocked.output / "report.json").read_text(encoding="utf-8"))
    assert blocked_report["incomplete"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["task", "acceptance", "base"])
async def test_resume_rejects_changed_inputs_under_the_same_key(tmp_path, field: str) -> None:
    world = _one_variant_world(tmp_path)
    runner = RecordingRunner()
    kwargs = {
        "live": True,
        "confirm_runs": 1,
        "runner": runner,
        "review_backend_factory": _factory(ScriptedBackend(replies=[pass_reply()])),
        "command_runner": ScriptedCommands(),
    }
    assert await _execute(world, **kwargs) == 0
    assert runner.calls == 1
    export = world.experiment.parent / "samples" / "dispatches.json"
    rows = json.loads(export.read_text(encoding="utf-8"))
    line = rows[0]["lines"][0]
    if field == "task":
        line["task"] = "Write a different note than the stored run."
    elif field == "acceptance":
        line["acceptance"] = ["cmd: false"]
    else:
        line["base_commit"] = world.answer
    export.write_text(json.dumps(rows), encoding="utf-8")
    assert await _execute(world, **kwargs) == 2
    assert runner.calls == 1


def _review_reserves(world: World) -> int:
    count = 0
    for line in (world.output / "ledger.jsonl").read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        if item.get("event") == "reserve" and item.get("kind") == "review":
            count += 1
    return count


def _git_out(cwd, *args: str) -> str:
    import subprocess

    from src.tools.auto_dispatch.harness_eval.variants import git_env

    return subprocess.run(
        ["git", *args], cwd=cwd, env=git_env(), capture_output=True, text=True, check=True
    ).stdout


class _PeekingRunner(RecordingRunner):
    """Records what refs and commits each worker can see, then commits."""

    def __init__(self) -> None:
        super().__init__()
        self.refs: list[set[str]] = []
        self.commits: list[set[str]] = []
        self.made: list[str] = []

    async def __call__(self, argv, cwd, log_path) -> int:
        self.refs.append(set(_git_out(cwd, "for-each-ref", "--format=%(refname)").split()))
        self.commits.append(set(_git_out(cwd, "rev-list", "--all").split()))
        code = await super().__call__(argv, cwd, log_path)
        self.made.append(_git_out(cwd, "rev-parse", "HEAD").strip())
        return code


@pytest.mark.asyncio
async def test_each_run_sees_only_its_own_harness_and_no_other_run(tmp_path) -> None:
    world = build_world(tmp_path)
    runner = _PeekingRunner()
    code = await _execute(
        world,
        live=True,
        confirm_runs=2,
        runner=runner,
        review_backend_factory=_factory(ScriptedBackend(replies=[pass_reply()])),
        command_runner=ScriptedCommands(),
    )
    assert code == 0, _records(world)
    assert runner.calls == 2
    for refs in runner.refs:
        harness = {ref for ref in refs if ref.startswith("refs/heads/h/")}
        workers = {ref for ref in refs if ref.startswith("refs/heads/feature/")}
        assert len(harness) == 1, refs
        assert len(workers) == 1, refs
        assert refs == harness | workers
    # The second worker cannot reach the first worker's commit, and neither
    # can reach the later answer commit.
    assert runner.made[0] not in runner.commits[1]
    assert all(world.answer not in seen for seen in runner.commits)
    for row in _records(world):
        diff = world.output / "runs" / row["run8"] / "worker.diff"
        assert "note.txt" in diff.read_text(encoding="utf-8")


class _AnswerFetchingRunner(RecordingRunner):
    """A worker that pulls the later answer commit from the source repo."""

    def __init__(self, source) -> None:
        super().__init__()
        self.source = source

    async def __call__(self, argv, cwd, log_path) -> int:
        code = await super().__call__(argv, cwd, log_path)
        _git_out(cwd, "fetch", "-q", "--no-tags", f"file://{self.source}", "main")
        _git_out(
            cwd,
            "-c", "user.name=w", "-c", "user.email=w@invalid", "-c", "commit.gpgsign=false",
            "merge", "-q", "--no-edit", "FETCH_HEAD",
        )
        return code


@pytest.mark.asyncio
async def test_a_worker_that_pulls_a_later_commit_fails_the_leak_check(tmp_path) -> None:
    world = build_world(tmp_path)
    before = source_fingerprint(world.repo)
    code = await _execute(
        world,
        live=True,
        confirm_runs=2,
        runner=_AnswerFetchingRunner(world.repo),
        review_backend_factory=_factory(ScriptedBackend(replies=[pass_reply()])),
        command_runner=ScriptedCommands(),
    )
    assert code == 4
    rows = _records(world)
    assert {item["worker_status"] for item in rows} == {"leak_check_failed"}
    assert all(item["score"] is None for item in rows)
    assert source_fingerprint(world.repo) == before


@pytest.mark.asyncio
async def test_reviewer_and_template_roots_stay_at_the_base(tmp_path) -> None:
    world = build_world(tmp_path)
    seen: list[tuple[str, str]] = []
    backend = ScriptedBackend(replies=[pass_reply()])

    def factory(repo):
        head = _git_out(repo, "rev-parse", "HEAD").strip()
        seen.append((head, (repo / "AGENTS.md").read_text(encoding="utf-8")))
        return backend

    code = await _execute(
        world,
        live=True,
        confirm_runs=2,
        runner=RecordingRunner(),
        review_backend_factory=factory,
        command_runner=ScriptedCommands(),
    )
    assert code == 0, _records(world)
    assert seen and all(item == (world.base, "Original agents file.\n") for item in seen)
    template = world.output / "repos" / world.base[:12]
    assert _git_out(template, "rev-parse", "HEAD").strip() == world.base
    assert (template / "AGENTS.md").read_text(encoding="utf-8") == "Original agents file.\n"


@pytest.mark.asyncio
async def test_a_setup_failure_is_unscored_and_still_writes_the_report(tmp_path) -> None:
    # The base tracks AGENTS.md as a directory, so the replace variant
    # cannot write the file. That is a local setup failure, not a leak.
    from harness_eval_fakes import commit_file, dispatch_document, git, write_tree

    world = build_world(tmp_path / "w")
    git(world.repo, "rm", "-q", "--", "AGENTS.md")
    commit_file(world.repo, "AGENTS.md/inner.md", "nested\n", "agents dir")
    base = git(world.repo, "rev-parse", "HEAD")
    path = write_tree(tmp_path / "x", world.document, [dispatch_document(base)])
    runner = RecordingRunner()
    code = await execute(
        path,
        live=True,
        confirm_runs=2,
        script=dummy_script(tmp_path / "launch.sh"),
        runner=runner,
        review_backend_factory=_factory(ScriptedBackend(replies=[pass_reply()])),
        command_runner=ScriptedCommands(),
    )
    # Every planned run failed setup, so nothing was measured (N37): exit 5.
    assert code == 5
    assert runner.calls == 0
    rows = [
        json.loads(line)
        for line in (world.output / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {item["worker_status"] for item in rows} == {"setup_failed"}
    assert {item["unscored_reason"] for item in rows} == {"setup_failed"}
    report = json.loads((world.output / "report.json").read_text(encoding="utf-8"))
    assert report["incomplete"] is False
    assert len(report["unscored"]) == 2


@pytest.mark.asyncio
async def test_a_second_live_run_on_the_same_output_dir_is_refused(tmp_path) -> None:
    import fcntl

    world = build_world(tmp_path)
    world.output.mkdir(parents=True)
    runner = RecordingRunner()
    with (world.output / "live.lock").open("a", encoding="utf-8") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        code = await _execute(
            world,
            live=True,
            confirm_runs=2,
            runner=runner,
            review_backend_factory=_factory(ScriptedBackend(replies=[pass_reply()])),
            command_runner=ScriptedCommands(),
        )
    assert code == 2
    assert runner.calls == 0
    assert not (world.output / "ledger.jsonl").exists()
