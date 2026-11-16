"""Reviewer verdicts and the git-backed merger against a real temp repository."""

from __future__ import annotations

import hashlib
import os
import subprocess

import pytest
from src.server.services.autonomy.git_delivery import GitDeliveryError

from src.tools.auto_dispatch.execution import (
    REVIEW_PROMPT_BUDGET_BYTES,
    CrossReviewer,
    GitMerger,
    branch_evidence,
    line_evidence,
)
from src.tools.auto_dispatch.protocols import (
    ApprovalDecision,
    DispatchError,
    GateNotApprovedError,
    LaunchError,
    MergeConflictError,
)
from src.tools.auto_dispatch.types import Dispatch, Line


class Backend:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[tuple[str, str]] = []

    async def review(self, model: str, prompt: str) -> str:
        self.prompts.append((model, prompt))
        return self.reply


def _reviewer(reply: str) -> tuple[CrossReviewer, Backend]:
    backend = Backend(reply)
    return CrossReviewer(backend, lambda line_id: f"evidence for {line_id}"), backend


async def test_a_pass_verdict_has_no_blockers() -> None:
    reviewer, backend = _reviewer('{"passed": true, "verdict": "looks good"}')
    verdict = await reviewer.review_line("line-0", "claude")
    assert verdict.passed is True
    assert verdict.blockers == []
    assert verdict.verdict == "looks good"
    assert backend.prompts[0][0] == "claude"


async def test_a_block_verdict_carries_its_blockers() -> None:
    reviewer, _ = _reviewer('{"passed": false, "blockers": ["no tests", "wrong file"]}')
    verdict = await reviewer.review_line("line-0", "claude")
    assert verdict.passed is False
    assert verdict.blockers == ["no tests", "wrong file"]


@pytest.mark.parametrize("reply", ["", "not json", '{"passed": "maybe"}'])
async def test_an_unparseable_verdict_blocks_rather_than_passes(reply: str) -> None:
    reviewer, _ = _reviewer(reply)
    verdict = await reviewer.review_line("line-0", "claude")
    assert verdict.passed is False
    assert verdict.blockers


def _git(cwd, *args: str) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, env=env
    )


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "dispatch@example.com")
    _git(tmp_path, "config", "user.name", "Dispatch Test")
    _git(tmp_path, "config", "core.hooksPath", "/dev/null")
    _git(tmp_path, "config", "core.fsmonitor", "false")
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _branch(repo, name: str, filename: str, content: str) -> None:
    _git(repo, "checkout", "-q", "-b", name, "main")
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", f"add {filename}")
    _git(repo, "checkout", "-q", "main")


def _dispatch(repo, branches: list[str]) -> Dispatch:
    dispatch = Dispatch(id="abc123", goal="g", user_id="u", created_at="now")
    dispatch.integrate_branch = "integrate/auto-dispatch-abc123"
    dispatch.lines = [
        Line(id=f"line-{index}", name=f"n{index}", task="t", branch=branch)
        for index, branch in enumerate(branches)
    ]
    return dispatch


async def test_merge_line_lands_the_branch_on_the_integrate_branch(repo) -> None:
    _branch(repo, "feature/auto-dispatch-abc123/line-line-0", "one.txt", "one\n")
    (repo / "base.txt").write_text("operator edit\n", encoding="utf-8")
    (repo / "operator-note.txt").write_text("keep me\n", encoding="utf-8")
    dispatch = _dispatch(repo, ["feature/auto-dispatch-abc123/line-line-0"])
    root_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()
    root_index = subprocess.run(
        ["git", "diff", "--cached", "--binary"], cwd=str(repo), capture_output=True, check=True
    ).stdout
    root_status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout
    merger = GitMerger(repo, dispatch)
    merged = await merger.merge_line("line-0")
    assert merged == "integrate/auto-dispatch-abc123"
    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "main"
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip() == root_head
    assert subprocess.run(
        ["git", "diff", "--cached", "--binary"], cwd=str(repo), capture_output=True, check=True
    ).stdout == root_index
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout == root_status
    assert (repo / "base.txt").read_text(encoding="utf-8") == "operator edit\n"
    assert (repo / "operator-note.txt").read_text(encoding="utf-8") == "keep me\n"
    assert not (repo / "one.txt").exists()
    assert subprocess.run(
        ["git", "show", "integrate/auto-dispatch-abc123:one.txt"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout == "one\n"


async def test_a_conflicting_merge_raises_with_the_conflicting_paths(repo) -> None:
    _branch(repo, "feature/auto-dispatch-abc123/line-line-0", "clash.txt", "from line\n")
    dispatch = _dispatch(repo, ["feature/auto-dispatch-abc123/line-line-0"])
    merger = GitMerger(repo, dispatch)
    (repo / "clash.txt").write_text("from main\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "main touch")
    (repo / "operator-edit.txt").write_text("preserve this\n", encoding="utf-8")
    (repo / "base.txt").write_text("unstaged edit\n", encoding="utf-8")
    root_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()
    before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout
    before_bytes = {path.name: path.read_bytes() for path in repo.iterdir() if path.is_file()}
    with pytest.raises(MergeConflictError) as excinfo:
        await merger.merge_line("line-0")
    assert "clash.txt" in excinfo.value.paths
    # The aborted merge leaves the repository's checkout and dirty state alone.
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout
    assert status == before
    assert {path.name: path.read_bytes() for path in repo.iterdir() if path.is_file()} == before_bytes
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip() == root_head
    assert subprocess.run(
        ["git", "branch", "--show-current"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip() == "main"


LINE_BRANCH = "feature/auto-dispatch-abc123/line-line-0"


async def _reviewed_prompt(repo, dispatch, **options) -> str:
    backend = Backend('{"passed": true}')
    await CrossReviewer(backend, branch_evidence(dispatch, repo, **options)).review_line(
        "line-0", "claude"
    )
    return backend.prompts[0][1]


async def test_the_review_prompt_carries_what_the_branch_adds_to_head(repo) -> None:
    _branch(repo, LINE_BRANCH, "handler.py", "def handle():\n    return 42\n")
    # Work that landed on main after the branch was cut is not the branch's doing.
    (repo / "main_only.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "main moves on")

    prompt = await _reviewed_prompt(repo, _dispatch(repo, [LINE_BRANCH]))

    assert "handler.py" in prompt
    assert "+    return 42" in prompt
    assert "main_only.txt" not in prompt


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


async def test_the_budget_leaves_room_under_the_one_string_limit_linux_enforces() -> None:
    # One argv string of 131,072 bytes or more cannot be started on Linux; the CLI's flags and
    # the environment share the call, so the prompt leaves 32 KiB of it.
    assert REVIEW_PROMPT_BUDGET_BYTES <= 131_072 - 32 * 1024


@pytest.mark.parametrize("char", ["x", "\u754c", "\U0001f600"], ids=["1-byte", "3-byte", "4-byte"])
async def test_a_diff_over_the_budget_is_cut_in_bytes_on_a_character_boundary_and_says_so(
    repo, char
) -> None:
    _branch(repo, LINE_BRANCH, "big.txt", char * 200_000 + "\n")
    full = subprocess.run(
        ["git", "diff", "--patch-with-stat", "--no-color", "--no-ext-diff", "--no-textconv",
         f"HEAD...{LINE_BRANCH}", "--"],
        cwd=str(repo), capture_output=True, encoding="utf-8", check=True,
    ).stdout

    prompt = await _reviewed_prompt(repo, _dispatch(repo, [LINE_BRANCH]))

    assert _bytes(full) > REVIEW_PROMPT_BUDGET_BYTES
    # The diff gets what the rest leaves: the whole prompt ends just under the budget...
    assert REVIEW_PROMPT_BUDGET_BYTES - 1_024 <= _bytes(prompt) <= REVIEW_PROMPT_BUDGET_BYTES
    # ... what is shown is a clean prefix of the diff, never a split character...
    from src.tools.auto_dispatch.execution import _REVIEW_REPLY_REMINDER

    body = prompt.split("adds to HEAD:\n", 1)[1]
    body = body[: -len(_REVIEW_REPLY_REMINDER)].rstrip("\n") if body.endswith(_REVIEW_REPLY_REMINDER) else body
    shown, notice = body.rsplit("\n", 1)
    assert "\ufffd" not in prompt
    assert full.startswith(shown) and "big.txt" in shown
    # ... and the prompt says the diff is not all there.
    assert notice == f"[diff truncated: {_bytes(full)} bytes in all]"


@pytest.mark.parametrize("with_output", [True, False], ids=["output-supplied", "no-output-as-live"])
async def test_the_parts_around_the_diff_are_bounded_so_the_diff_always_has_room(
    repo, with_output
) -> None:
    _branch(repo, LINE_BRANCH, "handler.py", "x" * 50_000 + "\n")
    dispatch = _dispatch(repo, [LINE_BRANCH])
    dispatch.goal = "\U0001f600" * 200_000
    dispatch.lines[0].name = "\u754c" * 10_000
    dispatch.lines[0].task = "\u754c" * 200_000
    backend = Backend('{"passed": true}')
    outputs = {"line-0": "\U0001f600" * 200_000} if with_output else None
    reviewer = CrossReviewer(backend, branch_evidence(dispatch, repo, outputs=outputs))

    await reviewer.review_line("line-0", "claude")

    prompt = backend.prompts[0][1]
    assert _bytes(prompt) <= REVIEW_PROMPT_BUDGET_BYTES
    for part in ("goal", "line name", "task") + (("output",) if with_output else ()):
        assert f"[{part} truncated: " in prompt, part
    # Everything around it at its largest, a 50 KB diff still reaches the reviewer whole.
    assert "x" * 50_000 in prompt and "[diff truncated" not in prompt


async def test_a_goal_the_api_accepts_reaches_the_reviewer_whole(repo) -> None:
    _branch(repo, LINE_BRANCH, "handler.py", "one\n")
    dispatch = _dispatch(repo, [LINE_BRANCH])
    # The longest goal the API takes: 8,000 UTF-8 bytes, four to a character.
    dispatch.goal = "\U0001f600" * 2000

    backend = Backend('{"passed": true}')
    await CrossReviewer(backend, branch_evidence(dispatch, repo)).review_line("line-0", "claude")

    assert "Goal: " + "\U0001f600" * 2000 + "\n" in backend.prompts[0][1]


async def test_an_output_tail_keeps_its_end() -> None:
    dispatch = _dispatch(None, ["feature/x"])

    evidence = line_evidence(dispatch, {"line-0": "a" * 100_000 + "THE-END"})("line-0")

    assert evidence.endswith("THE-END")
    assert "[output truncated: 100007 bytes in all]" in evidence
    assert "a" * 100_000 not in evidence


async def test_an_output_tail_that_was_supplied_keeps_its_heading_and_nothing_is_said_about_its_absence() -> None:
    dispatch = _dispatch(None, ["feature/x"])

    evidence = line_evidence(dispatch, {"line-0": "boom\nFAILED test_x"})("line-0")

    assert evidence.endswith("\nTask: t\nOutput tail:\nboom\nFAILED test_x")
    assert "NOT part of this evidence" not in evidence


@pytest.mark.parametrize(
    "outputs",
    [{}, {"line-0": ""}, {"line-0": " \n\t"}, {"line-1": "another line's output"}],
    ids=["no-entry", "empty", "blank", "another-lines-output"],
)
async def test_without_worker_output_the_slot_says_so_instead_of_an_empty_heading(outputs) -> None:
    # A heading with nothing under it reads as "the worker printed nothing" or "nothing
    # failed". What the live service sends is one line that says the output is not evidence.
    dispatch = _dispatch(None, ["feature/x", "feature/y"])

    evidence = line_evidence(dispatch, outputs)("line-0")

    assert "Output tail" not in evidence
    notice = evidence.rsplit("\n", 1)[-1]
    assert evidence.endswith("\nTask: t\n" + notice), "the notice is the whole slot, on one line"
    for phrase in (
        "Worker output and test results",
        "NOT part of this evidence",
        "not success",
        "not silence",
        "printed nothing",
    ):
        assert phrase in notice, phrase


async def test_the_no_output_line_is_never_the_heaviest_slot() -> None:
    # The byte accounting (``branch_evidence`` gives the diff what the rest leaves, and the
    # "everything at its largest" test above holds a 50 KB diff whole) is measured with the
    # slot full. The line that replaces an empty one must not be what makes it larger.
    dispatch = _dispatch(None, ["feature/x"])

    without = line_evidence(dispatch, {})("line-0")
    full = line_evidence(dispatch, {"line-0": "\U0001f600" * 100_000})("line-0")

    assert _bytes(without) < _bytes(full)


async def test_a_lone_surrogate_in_the_planners_text_is_sent_as_text_not_as_a_crash(repo) -> None:
    _branch(repo, LINE_BRANCH, "handler.py", "one\n")
    dispatch = _dispatch(repo, [LINE_BRANCH])
    dispatch.lines[0].task = "write it\ud800"

    prompt = await _reviewed_prompt(repo, dispatch)

    prompt.encode("utf-8")  # a surrogate cannot be: it would end the reviewer's start in an error
    assert "Task: write it?" in prompt


async def test_a_prompt_over_the_budget_is_refused_before_a_reviewer_is_asked() -> None:
    backend = Backend('{"passed": true}')
    reviewer = CrossReviewer(backend, lambda line_id: "x" * 1000, max_prompt_bytes=500)

    with pytest.raises(DispatchError, match="over the 500-byte budget"):
        await reviewer.review_line("line-0", "claude")

    assert backend.prompts == []


async def test_a_budget_with_no_room_for_the_diff_fails_the_review_instead_of_hiding_it(repo) -> None:
    _branch(repo, LINE_BRANCH, "handler.py", "one\n")
    backend = Backend('{"passed": true}')
    evidence = branch_evidence(_dispatch(repo, [LINE_BRANCH]), repo, budget_bytes=900)

    with pytest.raises(DispatchError, match="no room for its diff"):
        await CrossReviewer(backend, evidence).review_line("line-0", "claude")

    assert backend.prompts == []


@pytest.mark.parametrize("branch", ["feature/never-created", ""])
async def test_a_branch_that_cannot_be_diffed_fails_the_review_instead_of_reviewing_nothing(
    repo, branch
) -> None:
    backend = Backend('{"passed": true}')
    reviewer = CrossReviewer(backend, branch_evidence(_dispatch(repo, [branch]), repo))

    with pytest.raises(DispatchError):
        await reviewer.review_line("line-0", "claude")

    assert backend.prompts == [], "a reviewer must not be asked about code nobody showed it"


async def test_reading_the_diff_runs_none_of_the_repositorys_own_diff_programs(
    repo, tmp_path_factory
) -> None:
    ran = tmp_path_factory.mktemp("drivers")
    driver = ran / "driver.sh"
    driver.write_text(f"#!/bin/sh\ntouch {ran}/ran-$(basename $0)\ncat \"$2\"\n", encoding="utf-8")
    driver.chmod(0o755)
    _git(repo, "config", "diff.external", str(driver))
    _git(repo, "config", "diff.probe.textconv", str(driver))
    (repo / ".gitattributes").write_text("*.txt diff=probe\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "attributes")
    _branch(repo, LINE_BRANCH, "one.txt", "one\n")

    prompt = await _reviewed_prompt(repo, _dispatch(repo, [LINE_BRANCH]))

    assert "+one" in prompt
    assert list(ran.glob("ran-*")) == []


async def test_push_is_refused_until_the_gate_approves(tmp_path, repo) -> None:
    from src.tools.auto_dispatch.approval import FileApprovalGate

    gate = FileApprovalGate(tmp_path / "proposed")
    dispatch = _dispatch(repo, [])
    token = await gate.request_approval(dispatch)
    merger = GitMerger(repo, dispatch, gate=gate)
    assert await gate.decision(token) is ApprovalDecision.PENDING
    with pytest.raises(GateNotApprovedError):
        await merger.push("integrate/auto-dispatch-abc123", token)
    await gate.resolve(token, True)
    # No remote is configured here, so the push itself fails; the point is that
    # it got past the gate.
    with pytest.raises(MergeConflictError):
        await merger.push("integrate/auto-dispatch-abc123", token)


async def test_push_without_a_configured_gate_is_refused(tmp_path) -> None:
    merger = GitMerger(tmp_path, _dispatch(tmp_path, []))
    with pytest.raises(GateNotApprovedError):
        await merger.push("integrate/auto-dispatch-abc123", "any-token")


async def test_set_aside_without_integration_worktree_fails_closed(repo) -> None:
    from src.tools.auto_dispatch.protocols import MergeConflictError

    dispatch = _dispatch(repo, [])
    merger = GitMerger(repo, dispatch)
    with pytest.raises(MergeConflictError, match="missing or unregistered"):
        await merger.set_aside()

pytestmark = pytest.mark.asyncio



@pytest.mark.parametrize(
    ("task", "problem"),
    [
        ("x" * 131_071, None),
        ("x" * 131_072, "longer than the portable limit"),
        ("\U0001f600" * 32_768, "longer than the portable limit"),
        ("do \ud800 it", "no bytes"),
        ("embedded \x00 null", "NUL byte"),
    ],
    ids=["at-the-limit", "one-over", "four-byte-characters-over", "lone-surrogate", "null-byte"],
)
async def test_an_argument_no_process_can_be_started_with_is_refused_before_a_process_exists(
    tmp_path, task, problem
) -> None:
    """The refusal is decided before the spawn: a spawn error cannot say whether
    the child ran, and the machine retries a launch failure. The runner here
    counts its calls, so 'before a process exists' is checked, not assumed."""
    from pathlib import Path

    from src.tools.auto_dispatch.execution import ScriptLauncher

    calls: list[list[str]] = []

    async def runner(argv, cwd, log_path):
        calls.append(argv)
        return 0

    launcher = ScriptLauncher(
        tmp_path / "runs", script=Path("/bin/echo"), cwd=tmp_path, runner=runner
    )
    line = Line(id="line-0", name="n", task=task)
    if problem is None:
        assert await launcher.launch(line, "codex", [])
        assert len(calls) == 1
        return
    with pytest.raises(LaunchError) as caught:
        await launcher.launch(line, "codex", [])
    assert calls == []
    text = str(caught.value)
    assert problem in text and "argument 2" in text and "line-0" in text
    assert "SECRET" not in text and len(text) < 200


async def test_the_refusal_does_not_name_the_model_even_when_the_model_is_the_bad_argument(
    tmp_path,
) -> None:
    from pathlib import Path

    from src.tools.auto_dispatch.execution import ScriptLauncher
    from src.tools.auto_dispatch.protocols import LaunchError

    launcher = ScriptLauncher(tmp_path / "runs", script=Path("/bin/echo"), cwd=tmp_path)
    with pytest.raises(LaunchError) as caught:
        await launcher.launch(Line(id="line-0", name="n", task="t"), "MARKER\x00", [])
    assert "MARKER" not in str(caught.value) and "argument 1" in str(caught.value)


async def test_a_task_over_the_kernel_limit_never_reaches_the_real_spawn(tmp_path) -> None:
    """Through the real _run_command and /bin/echo: no log, no exit marker, no
    process, and a LaunchError that does not repeat the task."""
    from pathlib import Path

    from src.tools.auto_dispatch.execution import ScriptLauncher
    from src.tools.auto_dispatch.protocols import LaunchError

    launcher = ScriptLauncher(tmp_path / "runs", script=Path("/bin/echo"), cwd=tmp_path)
    line = Line(id="line-0", name="n", task="SECRET-" + "x" * (3 * 1024 * 1024))
    with pytest.raises(LaunchError) as caught:
        await launcher.launch(line, "codex", [])
    assert "SECRET" not in str(caught.value)
    assert not (tmp_path / "runs").exists() or not list((tmp_path / "runs").iterdir())


async def test_fake_worker_receives_line_branch_and_commits_in_its_own_worktree(tmp_path):
    import subprocess

    from src.tools.auto_dispatch.execution import ScriptLauncher

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    script = tmp_path / "fake-worker"
    script.write_text(
        "#!/bin/sh\n"
        'case "$2" in *"Default git branch: feature/auto-dispatch-d1/line-line-a"*) ;; *) exit 40 ;; esac\n'
        'case "$2" in *"commit the Ticket changes before finishing"*) ;; *) exit 41 ;; esac\n'
        'test "$(git branch --show-current)" = "feature/auto-dispatch-d1/line-line-a" || exit 42\n'
        'printf "worker result\\n" > result.txt\n'
        'git add result.txt\n'
        'git commit -qm "worker result"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    seen_cwd = []

    async def fake_runner(argv, cwd, log_path):
        seen_cwd.append(cwd)
        result = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, check=False)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(result.stdout + result.stderr, encoding="utf-8")
        return result.returncode

    launcher = ScriptLauncher(
        tmp_path / "state" / "runs",
        script=script,
        cwd=repo,
        worktrees_dir=tmp_path / "state" / "dispatch-worktrees",
        dispatch_id="d1",
        runner=fake_runner,
    )
    line = Line(
        id="line-a",
        name="A",
        task="make the change",
        branch="feature/auto-dispatch-d1/line-line-a",
        base_commit=base,
        current_run="d1:line-a:0:run",
    )
    run_id = await launcher.launch(line, "fake", [])
    assert run_id == line.current_run
    worktree = next((tmp_path / "state" / "dispatch-worktrees").glob("*/*/*/*"))
    assert seen_cwd == [worktree]
    assert (worktree / "result.txt").read_text(encoding="utf-8") == "worker result\n"
    assert subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        capture_output=True, text=True, check=True,
    ).stdout.strip() == "main"
    assert not (repo / "result.txt").exists()


async def test_dirty_fake_worker_is_reported_without_resetting_its_worktree(tmp_path):
    import subprocess

    from src.tools.auto_dispatch.execution import ScriptLauncher
    from src.tools.auto_dispatch.protocols import UncommittedChangesError

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    script = tmp_path / "fake-dirty-worker"
    script.write_text("#!/bin/sh\nprintf dirty > dirty.txt\n", encoding="utf-8")
    script.chmod(0o755)
    state = tmp_path / "state"
    launcher = ScriptLauncher(
        state / "runs",
        script=script,
        cwd=repo,
        worktrees_dir=state / "dispatch-worktrees",
        dispatch_id="d2",
    )
    line = Line(
        id="line-b", name="B", task="change", branch="feature/auto-dispatch-d2/line-line-b",
        base_commit=base, current_run="d2:line-b:0:run",
    )
    with pytest.raises(UncommittedChangesError, match="uncommitted_changes"):
        await launcher.launch(line, "fake", [])
    worktree = next((state / "dispatch-worktrees").glob("*/*/*/*"))
    assert (worktree / "dirty.txt").read_text(encoding="utf-8") == "dirty"
    with pytest.raises(UncommittedChangesError, match="uncommitted_changes"):
        await launcher.launch(line, "fake", [])
    assert (worktree / "dirty.txt").read_text(encoding="utf-8") == "dirty"


async def test_launch_worker_recovery_exit_takes_priority_over_dirty_tree(tmp_path, monkeypatch):
    from src.tools.auto_dispatch.execution import ScriptLauncher, WorkerLaunchFailure
    from src.llms.native_cli.failure import FailureClass

    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", "/dev/null"], check=True)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    script = tmp_path / "fake-rate-limit"
    script.write_text("#!/bin/sh\nprintf dirty > partial.txt\nexit 75\n", encoding="utf-8")
    script.chmod(0o755)
    launcher = ScriptLauncher(
        tmp_path / "runs",
        script=script,
        cwd=repo,
        worktrees_dir=tmp_path / "worktrees",
        dispatch_id="d-rate",
    )
    line = Line(
        id="line-r",
        name="rate retry",
        task="continue",
        branch="feature/auto-dispatch-d-rate/line-line-r",
        base_commit=base,
        current_run="d-rate:line-r:0:one",
    )
    with pytest.raises(WorkerLaunchFailure) as caught:
        await launcher.launch_worker(
            line,
            "kimi",
            [],
            session_id=None,
            session_mode=None,
            handoff_path=None,
        )
    assert caught.value.failure_class is FailureClass.RATE_LIMITED
    path = next((tmp_path / "worktrees").glob("*/*/*/*"))
    assert (path / "partial.txt").read_text(encoding="utf-8") == "dirty"

    line.current_run = "d-rate:line-r:0:two"
    with pytest.raises(WorkerLaunchFailure) as retried:
        await launcher.launch_worker(
            line,
            "kimi",
            [],
            session_id="session-one",
            session_mode="resume",
            handoff_path=None,
            allow_dirty_worktree=True,
        )
    assert retried.value.failure_class is FailureClass.RATE_LIMITED
    assert next((tmp_path / "worktrees").glob("*/*/*/*")) == path
    assert (path / "partial.txt").read_text(encoding="utf-8") == "dirty"


async def test_actual_worker_spawn_strips_git_environment_and_sets_worktree_pwd(
    tmp_path, monkeypatch
):
    from src.tools.auto_dispatch.execution import _run_command

    monkeypatch.setenv("GIT_DIR", str(tmp_path / "host-git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "host-worktree"))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.fsmonitor=touch host-executed'")
    cwd = tmp_path / "worker-cwd"
    cwd.mkdir()
    script = tmp_path / "env-probe"
    script.write_text(
        "#!/bin/sh\nprintf 'PWD=%s\\n' \"$PWD\"\nenv | grep '^GIT_' || true\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    log_path = tmp_path / "run.log"

    code = await _run_command([str(script)], cwd, log_path)
    output = log_path.read_text(encoding="utf-8")
    assert code == 0
    assert f"PWD={cwd.resolve()}" in output
    assert "GIT_" not in output


async def test_host_git_rejects_included_worker_filter_before_status(repo, tmp_path):
    from src.tools.auto_dispatch.execution import ScriptLauncher

    attributes = repo / ".gitattributes"
    attributes.write_text("base.txt filter=evil\n", encoding="utf-8")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-q", "-m", "add filter attribute")
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    line = Line(
        id="line-filter-include",
        name="filter include",
        task="exercise host git filters",
        branch="feature/auto-dispatch-filter-include/line-line-filter-include",
        base_commit=base,
    )
    root = tmp_path.parent / f"{tmp_path.name}-filter-state"
    launcher = ScriptLauncher(
        root / "runs",
        cwd=repo,
        worktrees_dir=root / "worktrees",
        dispatch_id="filter-include",
    )
    worktree = await launcher._prepare_line_worktree(line)
    (worktree / "base.txt").write_text("modified\n", encoding="utf-8")

    included = tmp_path / "included-filter.config"
    sentinel = tmp_path / "host-filter-ran"
    included.write_text(
        "[filter \"evil\"]\n"
        f"\tclean = sh -c 'cat; touch {sentinel}'\n",
        encoding="utf-8",
    )
    _git(repo, "config", "include.path", str(included))
    with pytest.raises(GitDeliveryError, match="Git workspace status is unknown"):
        await launcher._line_git(
            repo.resolve(), worktree,
            ["status", "--porcelain", "--untracked-files=all", "--ignore-submodules=all"],
        )
    assert not sentinel.exists()


async def test_host_git_rejects_altered_worktree_commondir(repo, tmp_path):
    from src.tools.auto_dispatch.execution import ScriptLauncher

    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    line = Line(
        id="line-commondir",
        name="commondir defense",
        task="reject redirected common dir",
        branch="feature/auto-dispatch-commondir/line-line-commondir",
        base_commit=base,
    )
    root = tmp_path.parent / f"{tmp_path.name}-commondir-state"
    launcher = ScriptLauncher(
        root / "runs",
        cwd=repo,
        worktrees_dir=root / "worktrees",
        dispatch_id="commondir",
    )
    worktree = await launcher._prepare_line_worktree(line)
    gitfile = (worktree / ".git").read_text(encoding="utf-8").strip()
    admin = (worktree / gitfile[len("gitdir: ") :]).resolve()
    sentinel = tmp_path / "commondir-filter-ran"
    rogue_common = tmp_path / "rogue-common"
    rogue_common.mkdir()
    (rogue_common / "config").write_text(
        f"[filter \"evil\"]\n\tclean = touch {sentinel}\n", encoding="utf-8"
    )
    (admin / "commondir").write_text(str(rogue_common), encoding="utf-8")

    with pytest.raises(GitDeliveryError, match="Git workspace status is unknown"):
        await launcher._line_git(
            repo.resolve(), worktree,
            ["status", "--porcelain", "--untracked-files=all", "--ignore-submodules=all"],
        )
    assert not sentinel.exists()


async def test_launch_worker_requires_a_new_commit(tmp_path, monkeypatch):
    from src.tools.auto_dispatch.execution import ScriptLauncher
    from src.tools.auto_dispatch.protocols import NoNewCommitError

    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    script = tmp_path / "fake-noop"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    launcher = ScriptLauncher(
        tmp_path / "runs",
        script=script,
        cwd=repo,
        worktrees_dir=tmp_path / "worktrees",
        dispatch_id="d-noop",
    )
    line = Line(
        id="line-n",
        name="noop",
        task="do nothing",
        branch="feature/auto-dispatch-d-noop/line-line-n",
        base_commit=base,
        current_run="d-noop:line-n:0:run",
    )
    with pytest.raises(NoNewCommitError, match="no_new_commit") as caught:
        await launcher.launch_worker(
            line, "kimi", [], session_id=None, session_mode=None, handoff_path=None
        )
    assert caught.value.run_id == line.current_run
    # N34: no commit at all on the branch, so the machine still blocks the line.
    assert caught.value.branch_has_commits is False
    assert subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip() == base


async def test_a_retry_that_commits_nothing_submits_the_work_already_on_the_branch(repo, tmp_path):
    """N49: an empty retry on a branch already ahead of base is a submission.

    The first run committed and this retry found nothing left to change. N34
    parked that as ``no_new_commit`` and a human had to move the line on; N49
    reads a clean tree whose branch already carries the work as submitted.

    A retry, not a review fix: a fix round that leaves the branch where it
    found it has not answered the blockers and still stops
    ``no_new_commit`` (``test_execution_exit_commit.py``).
    """
    from src.tools.auto_dispatch.execution import ScriptLauncher

    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    script = tmp_path / "fake-launcher"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    calls = 0

    async def runner(argv, cwd, log_path):
        nonlocal calls
        calls += 1
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("done\n", encoding="utf-8")
        if calls == 1:
            (cwd / "first-run.txt").write_text("committed\n", encoding="utf-8")
            _git(cwd, "add", "first-run.txt")
            _git(cwd, "commit", "-q", "-m", "first worker commit")
        return 0

    line = Line(
        id="line-retry-commit",
        name="retry commit",
        task="make one commit",
        branch="feature/auto-dispatch-retry-commit/line-line-retry-commit",
        base_commit=base,
        current_run="first-run",
    )
    launcher = ScriptLauncher(
        tmp_path.parent / f"{tmp_path.name}-runs",
        script=script,
        cwd=repo,
        worktrees_dir=tmp_path.parent / f"{tmp_path.name}-worktrees",
        dispatch_id="retry-commit",
        runner=runner,
    )
    first = await launcher.launch_worker(
        line, "kimi", [], session_id=None, session_mode=None, handoff_path=None
    )
    first_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", line.branch],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    line.current_run = "retry-run"
    retry = await launcher.launch_worker(
        line, "kimi", [], session_id=None, session_mode=None, handoff_path=None
    )
    # N34: the launcher reports where each run left the branch, and git
    # counts the first run's commit beyond base_commit.
    assert (first.start_head, first.head) == (base, first_head)
    assert (retry.start_head, retry.head) == (first_head, first_head)
    assert subprocess.run(
        ["git", "-C", str(repo), "rev-parse", line.branch],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip() == first_head


@pytest.mark.parametrize("entrypoint", ["launch", "launch_worker"])
@pytest.mark.parametrize("exit_code", [1, 64, 124, 127])
@pytest.mark.parametrize("dirty", [False, True])
async def test_nonzero_unclassified_exit_retries_before_success_tree_checks(
    repo, tmp_path, entrypoint, exit_code, dirty
):
    from src.tools.auto_dispatch.execution import ScriptLauncher
    from src.tools.auto_dispatch.protocols import LaunchError

    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    script = tmp_path / "fake-nonzero"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)

    async def runner(argv, cwd, log_path):
        if dirty:
            (cwd / "partial.txt").write_text("keep for retry\n", encoding="utf-8")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("cli failed\n", encoding="utf-8")
        return exit_code

    line = Line(
        id="line-nonzero",
        name="nonzero",
        task="fail before commit",
        branch="feature/auto-dispatch-nonzero/line-line-nonzero",
        base_commit=base,
        current_run="run-nonzero",
    )
    launcher = ScriptLauncher(
        tmp_path.parent / f"{tmp_path.name}-runs",
        script=script,
        cwd=repo,
        worktrees_dir=tmp_path.parent / f"{tmp_path.name}-worktrees",
        dispatch_id="nonzero",
        runner=runner,
    )
    with pytest.raises(LaunchError, match=f"exited {exit_code}"):
        if entrypoint == "launch":
            await launcher.launch(line, "kimi", [])
        else:
            await launcher.launch_worker(
                line, "kimi", [], session_id=None, session_mode=None, handoff_path=None
            )
    worktree = next((tmp_path.parent / f"{tmp_path.name}-worktrees").glob("*/*/*/*"))
    assert worktree.exists()
    if dirty:
        assert (worktree / "partial.txt").read_text(encoding="utf-8") == "keep for retry\n"


async def test_reassignment_branch_with_existing_author_commits_is_not_reused(repo, tmp_path):
    from src.tools.auto_dispatch.execution import ScriptLauncher
    from src.tools.auto_dispatch.protocols import LaunchError

    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    branch = "feature/auto-dispatch-reuse/line-line-reuse-a1"
    _git(repo, "branch", branch, base)
    _git(repo, "checkout", "-q", branch)
    (repo / "author-a.txt").write_text("do not inherit\n", encoding="utf-8")
    _git(repo, "add", "author-a.txt")
    _git(repo, "commit", "-q", "-m", "old author commit")
    _git(repo, "checkout", "-q", "main")

    line = Line(
        id="line-reuse",
        name="reassigned",
        task="start from pinned base",
        branch=branch,
        base_commit=base,
        attempts=1,
    )
    launcher = ScriptLauncher(
        tmp_path.parent / f"{tmp_path.name}-runs",
        cwd=repo,
        worktrees_dir=tmp_path.parent / f"{tmp_path.name}-worktrees",
        dispatch_id="reuse",
    )
    with pytest.raises(LaunchError, match="refusing to reuse reassignment branch"):
        await launcher._prepare_line_worktree(line)


async def test_worktree_creation_rejects_preplanted_symlink_under_owned_root(repo, tmp_path):
    from src.tools.auto_dispatch.execution import ScriptLauncher
    from src.tools.auto_dispatch.protocols import LaunchError

    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    dispatch_id = "symlink-root"
    line = Line(
        id="line-symlink-root",
        name="reject symlink root child",
        task="do not escape worktree root",
        branch=f"feature/auto-dispatch-{dispatch_id}/line-line-symlink-root",
        base_commit=base,
    )
    worktree_root = tmp_path.parent / f"{tmp_path.name}-symlink-worktrees"
    worktree_root.mkdir()
    repo_key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:24]
    outside = tmp_path / "outside-worktree-store"
    outside.mkdir()
    (worktree_root / repo_key).symlink_to(outside, target_is_directory=True)
    launcher = ScriptLauncher(
        tmp_path.parent / f"{tmp_path.name}-runs",
        cwd=repo,
        worktrees_dir=worktree_root,
        dispatch_id=dispatch_id,
    )

    with pytest.raises(LaunchError, match="symlink"):
        await launcher._prepare_line_worktree(line)
    assert not list(outside.iterdir())


async def test_dirty_archive_rejects_preplanted_artifact_root_symlink(repo, tmp_path):
    from src.tools.auto_dispatch.execution import ScriptLauncher
    from src.tools.auto_dispatch.types import DispatchState

    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    line = Line(
        id="line-artifact-symlink",
        name="artifact symlink",
        task="retain dirty tree if archive path is redirected",
        branch="feature/auto-dispatch-artifact-symlink/line-line-artifact-symlink",
        base_commit=base,
    )
    dispatch = Dispatch(
        id="artifact-symlink",
        goal="protect artifact root",
        user_id="u",
        created_at="now",
        lines=[line],
        state=DispatchState.ABORT,
    )
    worktrees = tmp_path.parent / f"{tmp_path.name}-artifact-worktrees"
    launcher = ScriptLauncher(
        tmp_path.parent / f"{tmp_path.name}-runs",
        cwd=repo,
        worktrees_dir=worktrees,
        dispatch_id=dispatch.id,
    )
    worktree = await launcher._prepare_line_worktree(line)
    (worktree / "dirty.txt").write_text("preserve this\n", encoding="utf-8")
    outside = tmp_path / "outside-artifacts"
    outside.mkdir()
    (worktrees / "recovery-artifacts").symlink_to(outside, target_is_directory=True)

    errors = await launcher.cleanup_worktrees(dispatch)
    assert errors
    assert worktree.exists()
    assert (worktree / "dirty.txt").read_text(encoding="utf-8") == "preserve this\n"
    assert not list(outside.iterdir())


async def test_terminal_cleanup_archives_dirty_blocked_worktree_before_removal(repo, tmp_path):
    import tarfile

    from src.tools.auto_dispatch.execution import ScriptLauncher
    from src.tools.auto_dispatch.types import DispatchState, LineStatus

    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    line = Line(
        id="line-dirty-terminal",
        name="dirty terminal",
        task="leave edits",
        branch="feature/auto-dispatch-dirty-terminal/line-line-dirty-terminal",
        base_commit=base,
        status=LineStatus.BLOCKED,
    )
    dispatch = Dispatch(
        id="dirty-terminal",
        goal="terminal cleanup",
        user_id="u",
        created_at="now",
        lines=[line],
        state=DispatchState.ABORT,
    )
    root = tmp_path.parent / f"{tmp_path.name}-state"
    launcher = ScriptLauncher(
        root / "runs",
        cwd=repo,
        worktrees_dir=root / "worktrees",
        dispatch_id=dispatch.id,
    )
    worktree = await launcher._prepare_line_worktree(line)
    (worktree / "base.txt").write_text("changed\n", encoding="utf-8")
    (worktree / "untracked.txt").write_text("recover me\n", encoding="utf-8")
    outside = tmp_path / "outside-secret"
    outside.write_text("must not be followed", encoding="utf-8")
    (worktree / "outside-link").symlink_to(outside)

    assert await launcher.cleanup_worktrees(dispatch) == []
    assert not worktree.exists()
    assert subprocess.run(
        ["git", "-C", str(repo), "show-ref", "--verify", f"refs/heads/{line.branch}"],
        capture_output=True,
        text=True,
        check=False,
    ).returncode == 0
    artifact_dirs = list((root / "worktrees" / "recovery-artifacts").iterdir())
    assert len(artifact_dirs) == 1
    artifact = artifact_dirs[0]
    assert "changed" in (artifact / "tracked.patch").read_text(encoding="utf-8")
    with tarfile.open(artifact / "untracked.tar") as archive:
        assert archive.extractfile("untracked.txt").read() == b"recover me\n"
        link = archive.getmember("outside-link")
        assert link.issym()
        assert link.linkname == str(outside)


async def test_terminal_cleanup_removes_all_author_epoch_worktrees(repo, tmp_path):
    from src.tools.auto_dispatch.execution import ScriptLauncher
    from src.tools.auto_dispatch.types import DispatchState, LineStatus

    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    old_branch = "feature/auto-dispatch-epochs/line-line-epochs"
    new_branch = f"{old_branch}-a1"
    line = Line(
        id="line-epochs",
        name="author epochs",
        task="keep work isolated",
        branch=old_branch,
        base_commit=base,
        status=LineStatus.BLOCKED,
    )
    dispatch = Dispatch(
        id="epochs",
        goal="cleanup both author epochs",
        user_id="u",
        created_at="now",
        lines=[line],
        state=DispatchState.ABORT,
    )
    root = tmp_path.parent / f"{tmp_path.name}-epochs-state"
    launcher = ScriptLauncher(
        root / "runs",
        cwd=repo,
        worktrees_dir=root / "worktrees",
        dispatch_id=dispatch.id,
    )
    old_worktree = await launcher._prepare_line_worktree(line)
    (old_worktree / "author-a.txt").write_text("author A\n", encoding="utf-8")
    _git(old_worktree, "add", "author-a.txt")
    _git(old_worktree, "commit", "-q", "-m", "author A work")

    line.branch = new_branch
    line.attempts = 1
    new_worktree = await launcher._prepare_line_worktree(line)
    assert old_worktree.exists() and new_worktree.exists()
    assert await launcher.cleanup_worktrees(dispatch) == []
    assert not old_worktree.exists() and not new_worktree.exists()
    for branch in (old_branch, new_branch):
        assert subprocess.run(
            ["git", "-C", str(repo), "show-ref", "--verify", f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
            check=False,
        ).returncode == 0


async def test_dirty_worktree_archive_rejects_parent_symlink_race(repo, tmp_path):
    from src.tools.auto_dispatch.execution import _archive_dirty_worktree
    from src.tools.auto_dispatch.protocols import LaunchError

    worktree = tmp_path / "archive-worktree"
    worktree.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "proof.txt").write_text("outside secret\n", encoding="utf-8")
    (worktree / "parent").symlink_to(outside, target_is_directory=True)
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    async def fake_git(argv, cwd):
        if argv[0] == "status":
            return 0, "?? parent/proof.txt\n", ""
        if argv[0] == "rev-parse":
            return 0, f"{base}\n", ""
        if argv[0] == "diff":
            return 0, "", ""
        if argv[0] == "ls-files":
            return 0, "parent/proof.txt\0", ""
        return 0, "", ""

    artifact_root = tmp_path / "artifacts"
    with pytest.raises(LaunchError, match="safely snapshot"):
        await _archive_dirty_worktree(
            repo,
            worktree,
            artifact_root,
            fake_git,
            {"dispatch_id": "d", "line_id": "l", "branch": "b"},
        )
    assert (outside / "proof.txt").read_text(encoding="utf-8") == "outside secret\n"
    assert not list(artifact_root.iterdir())


async def test_terminal_cleanup_keeps_worktree_when_archive_fails(repo, tmp_path, monkeypatch):
    import src.tools.auto_dispatch.execution as execution

    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    line = Line(
        id="line-archive-failure",
        name="archive failure",
        task="preserve edits",
        branch="feature/auto-dispatch-archive-failure/line-line-archive-failure",
        base_commit=base,
    )
    dispatch = Dispatch(
        id="archive-failure",
        goal="retain on failed archive",
        user_id="u",
        created_at="now",
        lines=[line],
    )
    root = tmp_path.parent / f"{tmp_path.name}-archive-failure"
    launcher = execution.ScriptLauncher(
        root / "runs",
        cwd=repo,
        worktrees_dir=root / "worktrees",
        dispatch_id=dispatch.id,
    )
    worktree = await launcher._prepare_line_worktree(line)
    (worktree / "dirty.txt").write_text("do not discard\n", encoding="utf-8")

    async def fail_archive(*args, **kwargs):
        raise execution.LaunchError("archive refused")

    monkeypatch.setattr(execution, "_archive_dirty_worktree", fail_archive)
    errors = await launcher.cleanup_worktrees(dispatch)
    assert errors and "LaunchError" in errors[0]
    assert worktree.exists()
    assert (worktree / "dirty.txt").read_text(encoding="utf-8") == "do not discard\n"


async def test_terminal_cleanup_archives_dirty_integration_worktree(repo, tmp_path):
    from src.tools.auto_dispatch.types import DispatchState

    dispatch = _dispatch(repo, [])
    dispatch.state = DispatchState.COMPLETE
    worktrees = tmp_path.parent / f"{tmp_path.name}-integration-worktrees"
    merger = GitMerger(repo, dispatch, worktrees_dir=worktrees)
    await merger._ensure_integrate_branch()
    integration = merger._integration_worktree
    (integration / "base.txt").write_text("integration change\n", encoding="utf-8")

    assert await merger.cleanup_worktree() is None
    assert not integration.exists()
    artifact_dirs = list((worktrees / "recovery-artifacts").iterdir())
    assert len(artifact_dirs) == 1
    assert "integration change" in (
        artifact_dirs[0] / "tracked.patch"
    ).read_text(encoding="utf-8")
    assert subprocess.run(
        ["git", "-C", str(repo), "show-ref", "--verify", f"refs/heads/{dispatch.integrate_branch}"],
        capture_output=True,
        text=True,
        check=False,
    ).returncode == 0


async def test_dispatch_runs_inside_repo_root_are_refused_before_writing(tmp_path):
    import subprocess

    from src.tools.auto_dispatch.execution import ScriptLauncher
    from src.tools.auto_dispatch.protocols import LaunchError

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    script = tmp_path / "fake-worker"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    launcher = ScriptLauncher(
        repo / "runs", script=script, cwd=repo, dispatch_id="d3"
    )
    line = Line(
        id="line-c",
        name="C",
        task="change",
        branch="feature/auto-dispatch-d3/line-line-c",
        base_commit=base,
        current_run="d3:line-c:0:run",
    )
    with pytest.raises(LaunchError, match="runs directory must be outside repo_root"):
        await launcher.launch(line, "fake", [])
    assert not (repo / "runs").exists()


async def test_a_failure_after_the_child_ran_is_not_a_launch_failure_and_is_not_retried(
    tmp_path, monkeypatch
) -> None:
    """The exit marker is written after the child finished. If that write fails the
    work happened; calling it a failed launch would make the machine run the line
    again (nine executions in a reviewer's reproduction of an earlier version)."""
    from pathlib import Path

    from src.tools.auto_dispatch.execution import ScriptLauncher
    from src.tools.auto_dispatch.protocols import LaunchError

    real_write = Path.write_text

    def failing_write(self, *args, **kwargs):
        if self.suffix == ".exit":
            raise OSError(28, "No space left on device")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing_write)
    launcher = ScriptLauncher(tmp_path / "runs", script=Path("/bin/echo"), cwd=tmp_path)
    with pytest.raises(OSError) as caught:
        await launcher.launch(Line(id="l", name="n", task="t"), "codex", [])
    assert not isinstance(caught.value, LaunchError)
    ran = list((tmp_path / "runs").glob("*.log"))
    assert len(ran) == 1 and "t" in ran[0].read_text()


async def test_an_empty_run_on_a_diverged_branch_is_not_ahead_of_base(repo, tmp_path):
    """N34: ``HEAD..base`` must be empty; a branch reset onto other history is not ahead."""
    from src.tools.auto_dispatch.execution import ScriptLauncher
    from src.tools.auto_dispatch.protocols import NoNewCommitError

    def rev(name: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", name],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    base = rev("HEAD")
    branch = "feature/auto-dispatch-diverged/line-line-d"
    # A side history that does not contain base, then the line branch on it.
    _git(repo, "checkout", "-q", "--orphan", "side")
    (repo / "side.txt").write_text("side\n", encoding="utf-8")
    _git(repo, "add", "side.txt")
    _git(repo, "commit", "-q", "-m", "side root")
    _git(repo, "branch", branch)
    _git(repo, "checkout", "-q", "main")
    script = tmp_path / "fake-noop"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)

    async def runner(argv, cwd, log_path):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("done\n", encoding="utf-8")
        return 0

    launcher = ScriptLauncher(
        tmp_path.parent / f"{tmp_path.name}-runs",
        script=script,
        cwd=repo,
        worktrees_dir=tmp_path.parent / f"{tmp_path.name}-worktrees",
        dispatch_id="diverged",
        runner=runner,
    )
    line = Line(
        id="line-d",
        name="diverged",
        task="nothing",
        branch=branch,
        base_commit=base,
        current_run="diverged-run",
    )
    with pytest.raises(NoNewCommitError, match="no_new_commit") as caught:
        await launcher.launch_worker(
            line, "kimi", [], session_id=None, session_mode=None, handoff_path=None
        )
    assert caught.value.branch_has_commits is False
    assert caught.value.head == rev(branch)


async def test_rev_list_count_is_allowed_only_for_two_object_ids():
    from src.server.services.autonomy.git_delivery import _public_git_command_is_safe
    from src.tools.auto_dispatch.execution import _task6_readonly_git_argv

    a, b = "a" * 40, "b" * 64
    for check in (_task6_readonly_git_argv, lambda argv: _public_git_command_is_safe(tuple(argv))):
        assert check(["rev-list", "--count", f"{a}..{b}"])
        for span in (f"{a}...{b}", f"main..{b}", f"{a}..", f"--all..{a}", f"{a}..{b}..{a}", a):
            assert not check(["rev-list", "--count", span]), span
        assert not check(["rev-list", "--count", f"{a}..{b}", "--", "x"])


@pytest.mark.parametrize("ending", ["exit_one", "dirty"])
async def test_a_failed_run_that_committed_reports_its_heads(repo, tmp_path, ending):
    """N34: an exit outside the classified codes, or a dirty tree, still says where HEAD is."""
    from src.tools.auto_dispatch.execution import ScriptLauncher
    from src.tools.auto_dispatch.protocols import LaunchError, UncommittedChangesError

    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    script = tmp_path / "fake-launcher"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)

    async def runner(argv, cwd, log_path):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("done\n", encoding="utf-8")
        (cwd / "work.txt").write_text("work\n", encoding="utf-8")
        _git(cwd, "add", "work.txt")
        _git(cwd, "commit", "-q", "-m", "worker commit")
        if ending == "dirty":
            (cwd / "left-over.txt").write_text("dirty\n", encoding="utf-8")
            return 0
        return 1

    line = Line(
        id="line-f",
        name="failed",
        task="commit then fail",
        branch=f"feature/auto-dispatch-failed-{ending}/line-line-f",
        base_commit=base,
        current_run="failed-run",
    )
    launcher = ScriptLauncher(
        tmp_path.parent / f"{tmp_path.name}-runs",
        script=script,
        cwd=repo,
        worktrees_dir=tmp_path.parent / f"{tmp_path.name}-worktrees",
        dispatch_id=f"failed-{ending}",
        runner=runner,
    )
    expected = UncommittedChangesError if ending == "dirty" else LaunchError
    with pytest.raises(expected) as caught:
        await launcher.launch_worker(
            line, "kimi", [], session_id=None, session_mode=None, handoff_path=None
        )
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", line.branch], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head != base
    assert caught.value.start_head == base
    assert caught.value.head == head
