"""R2d review follow-ups to N48 (replan cap, split base) and N49 (exit without commit).

- B1: a clean tree on an unchanged head is a LaunchResult since N49, and that
  skipped N38's reviewed-heads ledger, so a head the review already refused
  went to review again after a human reassign. The machine now asks the ledger
  for such a run; the legacy ``launch()`` surface keeps N34's guard so the
  machine can ask it there too.
- B2: the commit prompt ran the spawn fence only before it waited for a
  provider slot, so a drain set while it waited did not hold the spawn.
- The commit prompt resumes the run's session, counts its tokens with the
  run's and is bounded by the dispatch run timeout.
- A split line bases on the dependencies it names, never on an unrelated
  sibling; a dependency branch git cannot name parks the line; ancestry is
  asked before an "uncombined" park; the park messages name the operator
  commands that exist.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from src.llms.native_cli.base import NativeCliUsage
from src.tools.auto_dispatch import LineStatus
from src.tools.auto_dispatch import execution as execution_module
from src.tools.auto_dispatch.drain import DrainAwareLauncher
from src.tools.auto_dispatch.execution import (
    DEFAULT_COMMIT_PROMPT_TIMEOUT_SECONDS,
    LaunchResult,
    ScriptLauncher,
    _provider_gate,
    commit_prompt_timeout_from_env,
)
from src.tools.auto_dispatch.gates import GateSettings
from src.tools.auto_dispatch.machine import (
    REPLAN_PARKED_MESSAGE,
    REPLAN_UNCOMBINED,
    REPLAN_UNRESOLVED,
    REPLAN_UNRESOLVED_MESSAGE,
    REVIEWED_HEADS,
    apply_human_reassign,
)
from src.tools.auto_dispatch.protocols import NoNewCommitError, UncommittedChangesError
from tests.unit.tools.auto_dispatch.test_execution_exit_commit import (
    _git,
    _launcher,
    _line,
    _recording_runner,
    _rev,
    _state,
    commit_it,
    leave_dirty,
    nothing,
    repo,  # noqa: F401 -- pytest fixture
)
from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle
from tests.unit.tools.auto_dispatch.test_machine_gates import _line as _gate_line
from tests.unit.tools.auto_dispatch.test_machine_replan_cap import (
    _passed,
    _replan_line,
    _ReplanPlanner,
)
from tests.unit.tools.auto_dispatch.test_n34_committed_retry import (
    BASE,
    H1,
    RecordingReviewer,
    _dispatch,
    _handoff,
    _seed_runs,
)

pytestmark = pytest.mark.asyncio


# --- B1: an unchanged head after a reassign still goes through N38 -----------


class _CleanTreeLauncher:
    """N49's real contract: a clean tree on an ahead branch is a LaunchResult."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    async def launch(self, *args, **kwargs):  # pragma: no cover - not this surface
        raise AssertionError("launch_worker is the surface under test")

    async def launch_worker(
        self, line, model, skills, *, session_id, session_mode, handoff_path,
        fix_round=False, before_spawn=None, **kwargs,
    ):
        self.calls.append((model, fix_round))
        run_id = line.current_run or "r"
        if fix_round:
            error = NoNewCommitError("no_new_commit", run_id=run_id, branch_has_commits=True)
            error.head = H1
            raise error
        return LaunchResult(run_id, session_id, "nothing to do", start_head=H1, head=H1)


async def _reassigned_drive(tmp_path: Path, ledger_heads: list[str], *, submitted: bool):
    reviewer = RecordingReviewer()
    launcher = _CleanTreeLauncher()
    collaborators, machine, commands, _ = _bundle(
        tmp_path, [], launcher=launcher, reviewer=reviewer
    )
    gates = collaborators.gates
    gates.handoff_facts = _handoff
    dispatch = _dispatch("d-b1", model="kimi")
    line = dispatch.line("line-x")
    _seed_runs(gates, dispatch, [{"author": "kimi", "start": BASE, "head": H1, "submitted": submitted}])
    dispatch.gates[REVIEWED_HEADS] = {
        "line-x": {line.branch: {"complete": True, "heads": list(ledger_heads)}}
    }
    snap = gates.line_snapshot(dispatch, line)
    snap["machine"].update(
        stage="replanning",
        replan_options=["resplit_task", "replace_developer"],
        author_provider="kimi",
        developer_provider="kimi",
        contributors=["kimi"],
        fix_rounds=2,
    )
    line.status = LineStatus.BLOCKED
    line.failure_reason = "replan_parked"
    assert await apply_human_reassign(dispatch, line, "stepfun", None, gates) is None
    result = await machine.run(dispatch)
    return result.line("line-x"), reviewer, commands, launcher


async def test_a_reassigned_run_on_a_rejected_head_stops_no_new_commit(tmp_path: Path) -> None:
    """H1 was reviewed and refused; the new holder changes nothing: N38 blocks."""
    out, reviewer, commands, launcher = await _reassigned_drive(tmp_path, [H1], submitted=True)

    assert launcher.calls == [("stepfun", False)]  # not a fix round: N49's reading
    assert out.failure_reason == "no_new_commit"
    assert out.status is LineStatus.BLOCKED
    assert reviewer.reviewed == []  # the refused head was not reviewed again
    assert not commands.calls  # nor sent through G2


async def test_a_reassigned_run_on_an_unreviewed_head_goes_on_to_review(
    tmp_path: Path,
) -> None:
    """The same empty run on a head never submitted is a submission (N38).

    The previous holder committed H1 and then hit a rate limit, so H1 never
    entered G2 or review; the new holder finds nothing left to change.
    """
    out, reviewer, commands, _launcher = await _reassigned_drive(tmp_path, [], submitted=False)

    assert out.failure_reason != "no_new_commit"
    assert reviewer.reviewed  # H1 went on to review
    assert commands.calls  # and through G2


async def test_the_legacy_launch_keeps_the_head_for_the_ledger(repo, tmp_path) -> None:  # noqa: F811
    """``launch()`` returns only a run id, so an empty run must stop with its head."""
    calls: list[list[str]] = []
    launcher = _launcher(
        repo, _state(tmp_path), _recording_runner(calls, [leave_dirty, commit_it, nothing])
    )
    line = _line(repo)
    first = await launcher.launch_worker(
        line, "kimi", [], session_id=None, session_mode=None, handoff_path=None
    )
    line.current_run = "d-n49:line-n:0:legacy"

    with pytest.raises(NoNewCommitError) as caught:
        await launcher.launch(line, "kimi", [])

    assert caught.value.branch_has_commits is True
    assert caught.value.head == first.head


# --- B2: the commit prompt's spawn fence runs again with the slot held -------


async def test_the_commit_prompt_does_not_spawn_while_drained(repo, tmp_path) -> None:  # noqa: F811
    state = _state(tmp_path)
    drained = {"on": False}
    drained_at_spawn: list[bool] = []
    calls: list[list[str]] = []
    inner_runner = _recording_runner(calls, [leave_dirty, commit_it])

    async def runner(argv, cwd, log_path):
        drained_at_spawn.append(drained["on"])
        return await inner_runner(argv, cwd, log_path)

    wrapped = DrainAwareLauncher(
        _launcher(repo, state, runner),
        drained=lambda: drained["on"],
        poll_seconds=0.01,
        busy=lambda kind, detail: contextlib.nullcontext(),
    )
    fences = {"n": 0}
    gate = _provider_gate("kimi")

    async def fence() -> None:
        fences["n"] += 1
        if fences["n"] == 2:
            # The commit prompt's first fence: another line takes the only
            # kimi slot, and the operator drains while the prompt waits.
            assert gate.try_acquire(1)
            drained["on"] = True

            async def other_line_finishes() -> None:
                await asyncio.sleep(0.1)
                gate.release()
                await asyncio.sleep(0.4)
                drained["on"] = False

            asyncio.get_running_loop().create_task(other_line_finishes())

    line = _line(repo)
    result = await asyncio.wait_for(
        wrapped.launch_worker(
            line, "kimi", [], session_id=None, session_mode=None,
            handoff_path=None, before_spawn=fence,
        ),
        # Real git work: generous under a loaded full-suite run.
        30,
    )

    assert drained_at_spawn == [False, False]
    assert fences["n"] >= 3  # fenced again once the slot was held
    assert result.head == _rev(repo, line.branch)


# --- The commit prompt: session, tokens, bound --------------------------------


def _usage_by_log(monkeypatch) -> None:
    """Each turn's log names its tokens; the parser reads them back."""

    def parse(provider, output, requested_id):
        tokens = 0
        for word in output.split():
            if word.startswith("tokens="):
                tokens = int(word.split("=", 1)[1])
        session = "sess-from-cli" if "commit-turn" in output else requested_id
        return session, NativeCliUsage(input_tokens=tokens, output_tokens=1, total_tokens=tokens + 1)

    monkeypatch.setattr(execution_module, "_session_metadata", parse)


def _logging_runner(calls, actions, texts):
    async def runner(argv, cwd, log_path):
        calls.append(list(argv))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(texts[len(calls) - 1], encoding="utf-8")
        return actions[len(calls) - 1](cwd, len(calls))

    return runner


async def test_the_commit_prompt_resumes_the_session_and_counts_its_tokens(
    repo, tmp_path, monkeypatch  # noqa: F811
) -> None:
    _usage_by_log(monkeypatch)
    calls: list[list[str]] = []
    launcher = _launcher(
        repo,
        _state(tmp_path),
        _logging_runner(
            calls,
            [leave_dirty, commit_it],
            ["run tokens=100\n", "commit-turn tokens=7\n"],
        ),
    )
    line = _line(repo)

    result = await launcher.launch_worker(
        line, "cursor", [], session_id="sess-1", session_mode="resume", handoff_path=None
    )

    assert len(calls) == 2
    # The worker that left the tree is asked to commit it, in its own session.
    assert "--session-id=sess-1" in calls[1]
    assert "--session-mode=resume" in calls[1]
    # Both turns' tokens are the run's, and the session the line resumes next
    # is the one the prompt's CLI reported.
    assert result.usage.input_tokens == 107
    assert result.usage.output_tokens == 2
    assert result.tokens == 109
    assert result.session_id == "sess-from-cli"


async def test_a_refused_commit_prompt_still_counts_its_tokens(
    repo, tmp_path, monkeypatch  # noqa: F811
) -> None:
    _usage_by_log(monkeypatch)
    calls: list[list[str]] = []
    launcher = _launcher(
        repo,
        _state(tmp_path),
        _logging_runner(
            calls,
            [leave_dirty, leave_dirty],
            ["run tokens=100\n", "commit-turn tokens=7\n"],
        ),
    )

    with pytest.raises(UncommittedChangesError) as caught:
        await launcher.launch_worker(
            _line(repo), "cursor", [], session_id="sess-1", session_mode="resume",
            handoff_path=None,
        )

    assert caught.value.usage.input_tokens == 107
    assert caught.value.session_id == "sess-from-cli"


async def test_a_provider_that_cannot_resume_gets_no_session_flags(repo, tmp_path) -> None:  # noqa: F811
    calls: list[list[str]] = []
    launcher = ScriptLauncher(
        _state(tmp_path) / "runs",
        script=_launcher(repo, _state(tmp_path), None)._script,
        cwd=repo,
        worktrees_dir=_state(tmp_path) / "dispatch-worktrees",
        dispatch_id="d-n49",
        runner=_recording_runner(calls, [leave_dirty, commit_it]),
    )
    await launcher.launch_worker(
        _line(repo), "stepfun", [], session_id=None, session_mode=None, handoff_path=None
    )

    assert not any(arg.startswith("--session-") for arg in calls[1])


async def test_a_commit_prompt_past_its_bound_keeps_the_tree(repo, tmp_path) -> None:  # noqa: F811
    calls: list[list[str]] = []
    inner = _recording_runner(calls, [leave_dirty, nothing])

    async def runner(argv, cwd, log_path):
        code = await inner(argv, cwd, log_path)
        if len(calls) == 2:
            await asyncio.sleep(300)  # the prompt hangs; only the bound ends it
        return code

    state = _state(tmp_path)
    launcher = ScriptLauncher(
        state / "runs",
        script=_launcher(repo, state, None)._script,
        cwd=repo,
        worktrees_dir=state / "dispatch-worktrees",
        dispatch_id="d-n49",
        runner=runner,
        commit_prompt_timeout=0.2,
    )

    with pytest.raises(UncommittedChangesError):
        await asyncio.wait_for(
            launcher.launch_worker(
                _line(repo), "kimi", [], session_id=None, session_mode=None,
                handoff_path=None,
            ),
            # Real git work: generous under a loaded full-suite run, still far
            # below the hang, so only the 0.2 s bound can end it in time.
            60,
        )
    assert len(calls) == 2
    worktree = next((state / "dispatch-worktrees").glob("*/*/*/*"))
    assert (worktree / "work.txt").read_text(encoding="utf-8") == "the work\n"
    # The slot is free again for the next run.
    assert _provider_gate("kimi").try_acquire(1)
    _provider_gate("kimi").release()


def test_the_commit_prompt_bound_comes_from_the_dispatch_run_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    key = "CONDUCTOR_DISPATCH_TIMEOUT_SECONDS"
    assert commit_prompt_timeout_from_env({key: "42"}) == 42.0
    for bad in ("", "nope", "0", "-5", "nan", "inf"):
        assert commit_prompt_timeout_from_env({key: bad}) == DEFAULT_COMMIT_PROMPT_TIMEOUT_SECONDS
    monkeypatch.setenv(key, "17")
    launcher = ScriptLauncher(tmp_path / "runs", cwd=tmp_path)
    assert launcher._commit_prompt_timeout == 17.0
    assert ScriptLauncher(
        tmp_path / "runs", cwd=tmp_path, commit_prompt_timeout=3
    )._commit_prompt_timeout == 3.0


# --- N48: where a split line starts, and how it parks -------------------------


async def test_a_line_with_no_dependency_bases_on_the_integrate_base(tmp_path: Path) -> None:
    """A passed sibling the line does not wait on is not its base."""
    dispatch = _dispatch("d-n48-nodeps")
    dispatch.lines.append(_passed("line-p", dispatch))
    line = dispatch.line("line-x")
    assert line is not None and not line.depends_on
    collaborators, _machine = await _replan_line(
        tmp_path,
        dispatch,
        line,
        _ReplanPlanner([_gate_line("child-a")]),
        heads={f"{dispatch.branch_prefix}/line-line-p": H1},
    )

    stored = await collaborators.store.get(dispatch.id)
    assert stored.line("line-x-r1-1").base_commit == BASE


async def test_a_dependency_branch_git_cannot_name_parks_the_line(tmp_path: Path) -> None:
    dispatch = _dispatch("d-n48-unresolved")
    dispatch.lines.append(_passed("line-a", dispatch))
    line = dispatch.line("line-x")
    line.depends_on = ("line-a",)
    planner = _ReplanPlanner([_gate_line("child-a")])
    collaborators, _machine = await _replan_line(tmp_path, dispatch, line, planner, heads={})

    assert planner.calls == []  # no split onto a base without line-a
    stored = await collaborators.store.get(dispatch.id)
    parked = stored.line("line-x")
    assert parked.status is LineStatus.BLOCKED
    assert parked.failure_reason == REPLAN_UNRESOLVED
    assert stored.line("line-x-r1-1") is None
    assert "line-a" in parked.blockers[0]
    assert stored.message == REPLAN_UNRESOLVED_MESSAGE.format(
        dispatch_id=dispatch.id, line_id="line-x"
    )
    state = collaborators.gates.line_snapshot(stored, parked)["machine"]
    assert state["stage"] == "replanning"
    # The way out is the reassign the message names.
    collaborators.gates.handoff_facts = _handoff
    assert await apply_human_reassign(stored, parked, "glm", None, collaborators.gates) is None
    assert parked.status is LineStatus.ASSIGNED


async def test_the_park_messages_name_the_real_operator_commands(tmp_path: Path) -> None:
    dispatch = _dispatch("d-n48-wording")
    line = dispatch.line("line-x")
    collaborators, _machine = await _replan_line(
        tmp_path, dispatch, line, _ReplanPlanner([_gate_line("child-a")]), replans=1
    )

    stored = await collaborators.store.get(dispatch.id)
    parked = stored.line("line-x")
    command = f"conductor dispatch reassign {dispatch.id} line-x <model>"
    assert command in stored.message
    assert f"conductor dispatch cancel {dispatch.id}" in stored.message
    assert command in parked.blockers[0]
    assert "resplit it" not in stored.message
    assert stored.message == REPLAN_PARKED_MESSAGE.format(
        dispatch_id=dispatch.id, line_id="line-x"
    )


def _commit(repo_dir: Path, name: str) -> str:
    (repo_dir / name).write_text(name + "\n", encoding="utf-8")
    _git(repo_dir, "add", name)
    _git(repo_dir, "commit", "-q", "-m", name)
    return _rev(repo_dir, "HEAD")


@pytest.fixture
def chain(tmp_path: Path):
    """base -> a -> b on one chain, and c on a branch of its own from base."""
    repo_dir = tmp_path / "git"
    repo_dir.mkdir()
    _git(repo_dir, "init", "-q", "-b", "main")
    _git(repo_dir, "config", "user.email", "dispatch@example.com")
    _git(repo_dir, "config", "user.name", "Dispatch Test")
    _git(repo_dir, "config", "core.hooksPath", "/dev/null")
    base = _commit(repo_dir, "base")
    head_a = _commit(repo_dir, "a")
    head_b = _commit(repo_dir, "b")
    _git(repo_dir, "checkout", "-q", "-b", "side", base)
    head_c = _commit(repo_dir, "c")
    return repo_dir, {"base": base, "a": head_a, "b": head_b, "c": head_c}


async def _replan_on_git(tmp_path: Path, repo_dir: Path, base: str, heads: dict[str, str]):
    """``_replan`` with the gates' repo pointed at a real repository."""
    dispatch = _dispatch("d-n48-ancestry")
    for line_id in heads:
        dispatch.lines.append(_passed(line_id, dispatch))
    line = dispatch.line("line-x")
    line.depends_on = tuple(heads)
    planner = _ReplanPlanner([_gate_line("child-a")])
    settings = GateSettings(runs_dir=tmp_path / "runs", repo_root=repo_dir, g5_command="pytest -q")
    collaborators, machine, _commands, _trees = _bundle(
        tmp_path, [], planner=planner, settings=settings
    )
    gates = collaborators.gates
    await collaborators.store.create(dispatch)
    gates.prepare(dispatch)
    gates._ensure_root(dispatch)["base_commit"] = base
    by_branch = {f"{dispatch.branch_prefix}/line-{key}": value for key, value in heads.items()}
    gates._revisions = lambda other: by_branch.get(other.branch or "")
    snap = gates.line_snapshot(dispatch, line)
    snap["machine"].update(
        stage="replanning",
        replan_options=["resplit_task", "replace_developer"],
        author_provider="kimi",
        developer_provider="kimi",
        contributors=["kimi"],
    )
    await machine._claim_drive()
    try:
        await machine._replan(dispatch, line, ["blocker"], "two_failed_rounds")
    finally:
        machine._release_drive()
    return await collaborators.store.get(dispatch.id)


async def test_dependencies_on_one_chain_base_on_the_head_that_carries_both(
    tmp_path: Path, chain
) -> None:
    repo_dir, commits = chain
    stored = await _replan_on_git(
        tmp_path, repo_dir, commits["base"], {"line-a": commits["a"], "line-b": commits["b"]}
    )

    assert stored.line("line-x").failure_reason != REPLAN_UNCOMBINED
    assert stored.line("line-x-r1-1").base_commit == commits["b"]


async def test_a_dependency_already_in_the_integrate_base_does_not_park(
    tmp_path: Path, chain
) -> None:
    repo_dir, commits = chain
    # line-a's head is already merged into the integrate base (b carries it).
    stored = await _replan_on_git(
        tmp_path, repo_dir, commits["b"], {"line-a": commits["a"], "line-c": commits["c"]}
    )

    assert stored.line("line-x").failure_reason != REPLAN_UNCOMBINED
    assert stored.line("line-x-r1-1").base_commit == commits["c"]


async def test_parallel_dependency_branches_still_park(tmp_path: Path, chain) -> None:
    repo_dir, commits = chain
    stored = await _replan_on_git(
        tmp_path, repo_dir, commits["base"], {"line-b": commits["b"], "line-c": commits["c"]}
    )

    assert stored.line("line-x").failure_reason == REPLAN_UNCOMBINED
    assert stored.line("line-x-r1-1") is None

