"""N56 follow-up: a cross-review diff over the Git output cap is trimmed, not failed."""

from __future__ import annotations

import os
import subprocess

import pytest

from src.server.services.autonomy import git_delivery
from src.server.services.autonomy.git_delivery import git_command_runner
from src.tools.auto_dispatch.execution import (
    REVIEW_PROMPT_BUDGET_BYTES,
    CrossReviewer,
    branch_evidence,
)
from src.tools.auto_dispatch.types import Dispatch, Line

pytestmark = pytest.mark.asyncio

BRANCH = "feature/auto-dispatch-abc123/line-line-0"


class Backend:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def review(self, model: str, prompt: str) -> str:
        self.prompts.append(prompt)
        return '{"passed": true}'


def _git(cwd, *args: str) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, env=env)


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


def _dispatch() -> Dispatch:
    dispatch = Dispatch(id="abc123", goal="g", user_id="u", created_at="now")
    dispatch.lines = [Line(id="line-0", name="n0", task="t", branch=BRANCH)]
    return dispatch


async def _prompt(repo) -> str:
    backend = Backend()
    await CrossReviewer(backend, branch_evidence(_dispatch(), repo)).review_line(
        "line-0", "claude"
    )
    return backend.prompts[0]


async def test_a_branch_diff_over_the_git_cap_is_trimmed_with_a_marker(repo) -> None:
    _git(repo, "checkout", "-q", "-b", BRANCH, "main")
    line = "added line of a very large generated change set\n"
    big = line * (2 * git_delivery._GIT_OUTPUT_LIMIT_BYTES // len(line))
    (repo / "generated.txt").write_text(big, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "big change")
    _git(repo, "checkout", "-q", "main")

    prompt = await _prompt(repo)

    assert "generated.txt" in prompt, "the stat and the start of the patch are shown"
    assert "+added line of a very large generated change set" in prompt
    assert "[diff truncated: more than" in prompt
    assert len(prompt.encode("utf-8")) <= REVIEW_PROMPT_BUDGET_BYTES


async def test_the_review_diff_read_stops_at_the_prompt_budget(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(git_delivery, "_filter_names", lambda _cwd: ())
    served: list[int] = []

    def runner(argv, *, cwd, timeout, env):
        stdout = b"+x\n" * (2 * git_delivery._GIT_OUTPUT_LIMIT_BYTES)
        served.append(len(stdout))
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")

    with git_command_runner(runner):
        prompt = await _prompt(repo)

    assert served, "the diff went through the isolated Git boundary"
    assert "[diff truncated: more than" in prompt
    assert len(prompt.encode("utf-8")) <= REVIEW_PROMPT_BUDGET_BYTES


async def test_a_small_branch_diff_is_shown_whole(repo) -> None:
    _git(repo, "checkout", "-q", "-b", BRANCH, "main")
    (repo / "handler.py").write_text("def handle():\n    return 42\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "small change")
    _git(repo, "checkout", "-q", "main")

    prompt = await _prompt(repo)

    assert "+    return 42" in prompt
    assert "truncated" not in prompt
