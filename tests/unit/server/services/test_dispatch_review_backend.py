"""Live-mode cross-review, driven through the real machine with only the process faked.

``NativeReviewBackend`` is the live reviewer. These tests run every model of the
dispatch pool through ``DispatchMachine.run`` -> ``CrossReviewer`` ->
``NativeReviewBackend`` -> the real adapter, and fake only the place a vendor CLI
would be started (``run_process``). What they pin: a reviewer is always the CLI's
read-only mode, and any failure to build or run one ends the dispatch in ERROR with a
fixed sentence instead of escaping ``run`` and leaving the dispatch in REVIEW.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from src.config.models import NativeRuntimeRoutingConfig
from src.llms.native_cli.base import NativeCliError, NativeCliUnavailable
from src.llms.native_cli.process import ProcessOutput
from src.server.services.dispatch import DispatchService, DispatchSettings, NativeReviewBackend
from src.tools.auto_dispatch import (
    Collaborators,
    Dispatch,
    DispatchMachine,
    DispatchState,
    FileApprovalGate,
    InMemoryDispatchStore,
    InMemoryOutcomeStore,
    Line,
    LineStatus,
    MODEL_POOL,
)
from src.tools.auto_dispatch.dev import NoopMerger, RecordingLauncher, ScriptedMonitor
from src.tools.auto_dispatch.execution import CrossReviewer, branch_evidence, line_evidence
from src.tools.auto_dispatch.pool import REVIEW_INELIGIBLE
from src.tools.auto_dispatch.protocols import ModelRole

pytestmark = pytest.mark.asyncio

# What a vendor CLI would have echoed back if the error text were passed on.
LEAK = "SECRET-PROMPT-FRAGMENT-9f3a"

# One adapter module per pool model, and the argv runs that are its read-only mode.
ADAPTER_MODULES = (
    "claude",
    "codex",
    "deepseek",
    "doubao",
    "kimi",
    "minimax",
    "stepfun",
    "zhipu",
)
READ_ONLY_ARGV = {
    "codex": [("--sandbox", "read-only")],
    "claude": [("--restricted",), ("--strict-mcp-config",), ("--tools", "")],
    "stepfun": [("-p", "--no-session", "--no-tools"), ("--no-extensions",), ("--no-approve",)],
    "doubao": [("+chat",)],
}
BINARY_ENV = {
    "NATIVE_CLI_CLAUDE_BIN": "/opt/fake/claude",
    "NATIVE_CLI_CODEX_BIN": "/opt/fake/codex",
    "NATIVE_CLI_DSH_BIN": "/opt/fake/dsh",
    "NATIVE_CLI_DOUBAO_BIN": "/opt/fake/arkcli",
    "NATIVE_CLI_KIMI_BIN": "/opt/fake/kimi",
    "NATIVE_CLI_MINIMAX_BIN": "/opt/fake/mcode",
    "NATIVE_CLI_STEPFUN_BIN": "/opt/fake/step",
    "NATIVE_CLI_ZHIPU_BIN": "/opt/fake/zcode-cli",
}


class OneLinePlanner:
    async def plan(self, goal: str) -> list[Line]:
        return [Line(id="line-0", name="implement", task="write the handler")]


class PinnedReviewer:
    """Works with codex and reviews with the model under test, so only the reviewer varies."""

    def __init__(self, reviewer: str) -> None:
        self._reviewer = reviewer

    async def select(self, line, used_models, *, role=ModelRole.WORK):
        return self._reviewer if role is ModelRole.REVIEW else "codex"


class _OpenHeadroom:
    """Every model looks open, so a test does not read the local quota snapshot."""

    async def remaining(self, model: str) -> float:
        return 1.0


class CliBoundary:
    """The one fake: where a vendor CLI would have been started."""

    def __init__(self) -> None:
        self.invocations: list = []
        self.outcome: ProcessOutput | None = None

    async def __call__(self, invocation, **_kwargs) -> ProcessOutput:
        self.invocations.append(invocation)
        if self.outcome is not None:
            return self.outcome
        raise NativeCliError(f"provider failed and echoed {LEAK}")


@pytest.fixture
def cli(monkeypatch, tmp_path) -> CliBoundary:
    boundary = CliBoundary()
    for module in ADAPTER_MODULES:
        monkeypatch.setattr(f"src.llms.native_cli.{module}.run_process", boundary)
    for name, value in BINARY_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("src.llms.native_cli.zhipu._LOCK_PATH", tmp_path / "zhipu.lock")
    return boundary


def _machine(
    tmp_path: Path, reviewer: str, dispatch: Dispatch, *, evidence_from_git: bool = False
) -> tuple[DispatchMachine, InMemoryDispatchStore]:
    store = InMemoryDispatchStore()
    evidence = (
        branch_evidence(dispatch, tmp_path) if evidence_from_git else line_evidence(dispatch, {})
    )
    collaborators = Collaborators(
        planner=OneLinePlanner(),
        selector=PinnedReviewer(reviewer),
        launcher=RecordingLauncher(),
        monitor=ScriptedMonitor(),
        reviewer=CrossReviewer(NativeReviewBackend(cwd=tmp_path, timeout_seconds=5.0), evidence),
        merger=NoopMerger(),
        gate=FileApprovalGate(tmp_path / "proposed"),
        outcomes=InMemoryOutcomeStore(),
        store=store,
    )
    return DispatchMachine(collaborators), store


async def _drive(tmp_path: Path, reviewer: str, **options) -> Dispatch:
    dispatch = Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    machine, store = _machine(tmp_path, reviewer, dispatch, **options)
    return await machine.run(await store.create(dispatch))


@pytest.mark.parametrize("model", MODEL_POOL)
async def test_a_reviewer_that_fails_ends_the_dispatch_in_error(model, cli, tmp_path) -> None:
    dispatch = await _drive(tmp_path, model)

    # The adapter raised. That is a transient review failure, not ERROR.
    assert dispatch.state is not DispatchState.ERROR
    assert dispatch.state is DispatchState.REVIEW
    assert LEAK not in (dispatch.message or "")
    assert dispatch.approval_token is None
    assert dispatch.gates["review_attempts"]["line-0"] == 1


@pytest.mark.parametrize("model", MODEL_POOL)
async def test_the_reviewer_runs_read_only_or_does_not_run_at_all(model, cli, tmp_path) -> None:
    await _drive(tmp_path, model)

    if model in REVIEW_INELIGIBLE:
        # No read-only mode exists for this CLI, so nothing was started.
        assert cli.invocations == []
        return
    assert len(cli.invocations) == 1
    argv = cli.invocations[0].argv
    for marker in READ_ONLY_ARGV[model]:
        assert any(argv[i : i + len(marker)] == marker for i in range(len(argv))), (marker, argv)
    assert "--approve-for-me" not in argv
    assert "danger-full-access" not in argv
    assert "workspace-write" not in argv


async def test_a_verdict_from_a_read_only_reviewer_reaches_the_gate(cli, tmp_path) -> None:
    verdict = json.dumps({"passed": True, "blockers": [], "verdict": "looks right"})
    cli.outcome = ProcessOutput(
        stdout="\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "t1"}),
                json.dumps(
                    {"type": "item.completed", "item": {"type": "agent_message", "text": verdict}}
                ),
                json.dumps({"type": "turn.completed", "usage": {}}),
            ]
        ),
        stderr="",
        returncode=0,
    )

    dispatch = await _drive(tmp_path, "codex")

    assert dispatch.state is DispatchState.APPROVAL
    assert dispatch.line("line-0").status is LineStatus.PASSED
    argv = cli.invocations[0].argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"


@pytest.fixture
def operator_disables(monkeypatch):
    def disable(*providers: str) -> None:
        monkeypatch.setattr(
            "src.config.settings.get_native_runtime_routing_config",
            lambda: NativeRuntimeRoutingConfig(disabled_providers=list(providers)),
        )

    return disable


@pytest.mark.parametrize(
    ("disabled", "model"),
    [("claude-code", "claude"), ("claude", "claude-code"), ("zcode", "glm"), ("glm", "zcode"), ("codex", "codex")],
)
async def test_a_cli_the_operator_disabled_is_not_started_as_a_reviewer(
    disabled, model, cli, operator_disables
) -> None:
    operator_disables(disabled)

    with pytest.raises(NativeCliUnavailable, match="disabled by operator policy"):
        await NativeReviewBackend(timeout_seconds=5.0).review(model, "review this")

    assert cli.invocations == []


async def test_a_reviewer_the_operator_disabled_ends_the_dispatch_in_error(
    cli, tmp_path, operator_disables
) -> None:
    operator_disables("codex")

    dispatch = await _drive(tmp_path, "codex")

    assert dispatch.state is not DispatchState.ERROR
    assert dispatch.state is DispatchState.REVIEW
    assert dispatch.gates["review_attempts"]["line-0"] == 1
    assert cli.invocations == []


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


async def test_the_live_service_reviews_what_the_branch_changed_and_skips_a_disabled_cli(
    cli, tmp_path, operator_disables
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "dispatch@example.com")
    _git(repo, "config", "user.name", "Dispatch Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature/line-0")
    (repo / "handler.py").write_text("def handle():\n    return 42\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "handler")
    _git(repo, "checkout", "-q", "main")
    dispatch = Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    dispatch.lines = [
        Line(id="line-0", name="implement", task="write the handler", branch="feature/line-0")
    ]
    operator_disables("claude-code")
    service = DispatchService(
        DispatchSettings(adapters="live", repo_root=repo, proposed_dir=tmp_path / "proposed"),
        store=InMemoryDispatchStore(),
        gate=FileApprovalGate(tmp_path / "proposed"),
        # The assertion is which reviewer the policy picks, not the local snapshot.
        headroom=_OpenHeadroom(),
    )

    collaborators = service._machine(dispatch)._c
    picked = await collaborators.selector.select(
        dispatch.lines[0], ["codex"], role=ModelRole.REVIEW
    )
    with pytest.raises(NativeCliError):  # the CLI boundary fails; what it was sent is kept
        await collaborators.reviewer.review_line("line-0", "codex")

    assert picked not in ("claude", None)
    (invocation,) = cli.invocations
    # Codex is sent the prompt on stdin.
    assert "handler.py" in invocation.stdin and "+    return 42" in invocation.stdin


# One argv string may be 131,072 bytes on Linux (MAX_ARG_STRLEN: 32 pages of 4 KiB). The CLI's
# own flags and the environment travel in the same call, so the reviewer's whole prompt is held
# well below that. The number is written out here, not imported, so a change to the source's
# budget has to answer to what the kernel allows and not to itself.
LINUX_ARG_STRLEN = 131_072
SAFE_PROMPT_BYTES = 100_000
REVIEWERS = tuple(model for model in MODEL_POOL if model not in REVIEW_INELIGIBLE)


def _repo_with_a_heavy_branch(root: Path, branch: str) -> Path:
    """A repository whose branch adds one 160 KB line, every character four bytes long."""
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "dispatch@example.com")
    _git(repo, "config", "user.name", "Dispatch Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", branch)
    (repo / "notes.txt").write_text("\U0001f600" * 40_000 + "\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "notes")
    _git(repo, "checkout", "-q", "main")
    return repo


def _live_service_reviewer(repo: Path, tmp_path: Path, dispatch: Dispatch) -> CrossReviewer:
    """The reviewer the live ``DispatchService`` wires for ``dispatch``."""
    service = DispatchService(
        DispatchSettings(adapters="live", repo_root=repo, proposed_dir=tmp_path / "proposed"),
        store=InMemoryDispatchStore(),
        gate=FileApprovalGate(tmp_path / "proposed"),
    )
    return service._machine(dispatch)._c.reviewer


@pytest.mark.parametrize("model", REVIEWERS)
async def test_the_whole_prompt_a_reviewer_is_started_with_fits_one_argv_string(
    model, cli, tmp_path
) -> None:
    # The largest input the API accepts (4,000 characters, here all four-byte ones), a task and
    # a branch in multi-byte text, and a branch whose diff is far over any limit.
    branch = "feature/\u00fc-abc123/line-line-0"
    repo = _repo_with_a_heavy_branch(tmp_path, branch)
    dispatch = Dispatch(id="abc123", goal="\U0001f600" * 4000, user_id="u", created_at="now")
    dispatch.lines = [
        Line(id="line-0", name="\u5b9e\u73b0", task="\u5b9e\u73b0\u5904\u7406\u5668" * 200, branch=branch)
    ]
    reviewer = _live_service_reviewer(repo, tmp_path, dispatch)

    with pytest.raises(NativeCliError):  # the CLI boundary fails; what it was sent is kept
        await reviewer.review_line("line-0", model)

    (invocation,) = cli.invocations
    # Codex takes the prompt on stdin; StepFun, Claude and Doubao as one argv string.
    sent = [*invocation.argv, invocation.stdin]
    sizes = [len(part.encode("utf-8")) for part in sent]
    assert max(sizes) <= SAFE_PROMPT_BYTES < LINUX_ARG_STRLEN, (model, max(sizes))
    prompt = max(sent, key=len)
    assert len(prompt.encode("utf-8")) > 50_000, "the diff was not put to the reviewer at all"
    from src.tools.auto_dispatch.execution import _REVIEW_REPLY_REMINDER

    # The reply contract reminder closes the prompt; the truncation notice is
    # the last line of the evidence before it.
    body = prompt.rstrip()
    assert body.endswith(_REVIEW_REPLY_REMINDER)
    body = body[: -len(_REVIEW_REPLY_REMINDER)].rstrip()
    assert body.rsplit("\n", 1)[-1].startswith("[diff truncated")


async def test_the_live_reviewer_is_shown_the_diff_but_not_the_workers_log_tail(
    cli, tmp_path
) -> None:
    # ``Line.log_tail`` is the last 500 characters of the worker CLI's raw output, and
    # ``LogMonitor`` redacts none of it. The service passes the evidence builder no outputs,
    # so none of it reaches a second vendor's CLI, and the prompt says that it was left out.
    # The docs say so; wiring the tail in must change them, and the redaction question with it.
    branch = "feature/line-0"
    repo = _repo_with_a_heavy_branch(tmp_path, branch)
    dispatch = Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    dispatch.lines = [
        Line(
            id="line-0",
            name="implement",
            task="write the handler",
            branch=branch,
            log_tail="WORKER-TAIL " + "\U0001f600" * 100,
        )
    ]
    reviewer = _live_service_reviewer(repo, tmp_path, dispatch)

    with pytest.raises(NativeCliError):  # the CLI boundary fails; what it was sent is kept
        await reviewer.review_line("line-0", "codex")

    (invocation,) = cli.invocations
    prompt = invocation.stdin  # Codex takes the prompt on stdin
    assert "Goal: ship it\n" in prompt and "Task: write the handler\n" in prompt
    assert f"on branch {branch}\n" in prompt and "notes.txt" in prompt
    assert "WORKER-TAIL" not in prompt
    # Nothing is under an "Output tail:" heading, because there is no heading: a reviewer
    # that read an empty one as "the worker printed nothing" would be told something false.
    # The slot says in one line that the worker's output and test results are not evidence.
    assert "Output tail" not in prompt
    assert (
        "Worker output and test results are NOT part of this evidence; their absence here is "
        "not success and not silence. Do not infer that the worker printed nothing or that "
        f"nothing failed.\nWhat {branch} adds to HEAD:\n"
    ) in prompt


async def test_a_line_whose_diff_cannot_be_read_is_never_put_to_a_reviewer(
    cli, tmp_path, monkeypatch
) -> None:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))

    dispatch = await _drive(not_a_repo, "codex", evidence_from_git=True)

    assert dispatch.state is DispatchState.ERROR
    assert dispatch.message == "The reviewer codex could not review line line-0"
    assert cli.invocations == []


async def test_cancelling_git_evidence_propagates_without_becoming_review_error(tmp_path) -> None:
    dispatch = Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    dispatch.lines = [
        Line(
            id="line-0", name="implement", task="write handler",
            branch="feature/line-0", base_commit="a" * 40,
        )
    ]

    async def cancelled_git(_argv, _cwd):
        raise asyncio.CancelledError

    evidence = branch_evidence(dispatch, tmp_path, runner=cancelled_git)
    with pytest.raises(asyncio.CancelledError):
        await evidence("line-0")


async def test_review_timeout_is_read_from_the_environment() -> None:
    # N74: one fixed 300 s review turn timed codex out on large risk-bearing lines.
    assert DispatchSettings.from_env({}).review_timeout_seconds == 900.0
    settings = DispatchSettings.from_env({"CONDUCTOR_DISPATCH_REVIEW_TIMEOUT_SECONDS": "1234.5"})
    assert settings.review_timeout_seconds == 1234.5


async def test_the_live_reviewer_runs_with_the_configured_review_timeout(cli, tmp_path) -> None:
    # Primary, secondary and conflict reviews all go through this one CrossReviewer.
    dispatch = Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    dispatch.lines = [Line(id="line-0", name="implement", task="write the handler")]
    settings = DispatchSettings.from_env(
        {
            "CONDUCTOR_DISPATCH_ADAPTERS": "live",
            "CONDUCTOR_DISPATCH_REPO": str(tmp_path),
            "CONDUCTOR_DISPATCH_PROPOSED_DIR": str(tmp_path / "proposed"),
            "CONDUCTOR_DISPATCH_REVIEW_TIMEOUT_SECONDS": "1234",
        }
    )
    service = DispatchService(
        settings,
        store=InMemoryDispatchStore(),
        gate=FileApprovalGate(tmp_path / "proposed"),
    )
    reviewer = service._machine(dispatch)._c.reviewer.inner
    backend = reviewer._backend
    assert isinstance(backend, NativeReviewBackend)
    assert backend._timeout_seconds == 1234.0

    with pytest.raises(NativeCliError):
        await backend.review("codex", "review this")
    (invocation,) = cli.invocations
    assert invocation.timeout_seconds == 1234.0
