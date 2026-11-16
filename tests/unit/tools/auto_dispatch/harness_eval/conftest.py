"""Fakes and a tiny local git repo for harness-eval tests.

No provider CLI is started. Git talks only to repositories under tmp_path.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.tools.auto_dispatch.gates import CommandResult
from src.tools.auto_dispatch.harness_eval.types import BudgetSpec, RunRecord
from src.tools.auto_dispatch.harness_eval.variants import git_env

REPO_ROOT = Path(__file__).resolve().parents[5]
DISPATCH_ID = "zzhist9k"
LINE_ID = "line-0"
EXPERIMENT_ID = "hxtrial"
VARIANT_ID = "short"
MODEL = "stepfun"
REVIEWER = "codex"
VERDICT_TOKEN = "UNIQUE-VERDICT-TOKEN"
BLOCKER_TOKEN = "SECRET-BLOCKER-TEXT"
WORKER_TOKEN = "WORKER-STDOUT-SECRET"
SHORT_AGENTS = "Use the shorter checklist.\n"


def git(repo: Path, *args: str, check: bool = True) -> str:
    """Run git in ``repo`` with no inherited git config."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        env=git_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"git {args[0] if args else ''} failed: {detail}")
    return result.stdout.strip()


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Dispatch Test")
    git(path, "config", "user.email", "dispatch@example.invalid")
    git(path, "config", "commit.gpgsign", "false")
    return path


def commit_file(repo: Path, name: str, content: str, message: str) -> str:
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    git(repo, "add", "--", name)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def source_fingerprint(repo: Path) -> tuple[str, str]:
    refs = git(repo, "for-each-ref")
    status = git(repo, "status", "--porcelain", "--untracked-files=all")
    return refs, status


def make_source(path: Path) -> tuple[Path, str, str]:
    """A two-commit repo. The second commit is the answer that must not leak."""
    repo = init_repo(path)
    base = commit_file(repo, "README", "base\n", "base")
    commit_file(repo, "AGENTS.md", "Original agents file.\n", "agents")
    # The eval replays the first commit. Move the agents file onto that commit
    # by rebuilding: base already exists; the answer commit is the one after
    # the agents file so the base used by samples is the agents commit, and a
    # later answer commit must stay out of the eval object database.
    base = git(repo, "rev-parse", "HEAD")
    answer = commit_file(repo, "answer.txt", "the answer\n", "answer")
    return repo, base, answer


def line_dict(
    *,
    line_id: str = LINE_ID,
    task: str = "Add note.txt with one line of text.",
    acceptance: tuple[str, ...] = ("cmd: true",),
    write_scope: tuple[str, ...] = ("note.txt",),
    base_commit: str = "",
    risk_bearing: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": line_id,
        "name": line_id,
        "task": task,
        "acceptance": list(acceptance),
        "write_scope": list(write_scope),
        "risk_bearing": risk_bearing,
    }
    if base_commit:
        payload["base_commit"] = base_commit
    return payload


def dispatch_document(
    base: str,
    *,
    dispatch_id: str = DISPATCH_ID,
    lines: list[dict[str, Any]] | None = None,
    gates_base: str | None = None,
    outcomes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "dispatch_id": dispatch_id,
        "id": dispatch_id,
        "goal": "Ship the note.",
        "lines": lines
        if lines is not None
        else [line_dict(base_commit=base)],
        "outcomes": outcomes
        if outcomes is not None
        else {
            LINE_ID: {
                "line_id": LINE_ID,
                "model": "other",
                "passed": False,
                "reviewer_verdict": VERDICT_TOKEN,
            }
        },
    }
    if gates_base is not None:
        document["gates"] = {"base_commit": gates_base}
    return document


def experiment_document(
    *,
    repo: Path,
    output_dir: Path,
    source: str = "samples/dispatches.json",
    variants: list[dict[str, Any]] | None = None,
    models: list[str] | None = None,
    reviewer: list[str] | None = None,
    repeats: int = 1,
    baseline_repeats: int = 1,
    max_runs: int = 8,
    max_models: int = 1,
    max_reviews: int = 8,
    max_wall_minutes: int = 30,
    worker_timeout_seconds: int = 120,
    select: str = "explicit",
    lines: list[str] | None = None,
    seed: int = 1,
    experiment_id: str = EXPERIMENT_ID,
    samples_max: int = 3,
    require_cmd_acceptance: bool = True,
    exclude_risk_bearing: bool = True,
) -> dict[str, Any]:
    if variants is None:
        variants = [
            {"id": "baseline", "agents_md": {"mode": "keep"}, "skills": {"mode": "keep"}},
            {
                "id": VARIANT_ID,
                "agents_md": {"mode": "replace", "file": "variants/short/AGENTS.md"},
                "skills": {"mode": "keep"},
            },
        ]
    samples: dict[str, Any] = {
        "source": source,
        "select": select,
        "max": samples_max,
        "seed": seed,
        "require_cmd_acceptance": require_cmd_acceptance,
        "exclude_risk_bearing": exclude_risk_bearing,
    }
    if lines is not None:
        samples["lines"] = lines
    return {
        "schema": "conductor.harness_eval/v1",
        "id": experiment_id,
        "repo": str(repo),
        "samples": samples,
        "variants": variants,
        "baseline": "baseline",
        "models": models or [MODEL],
        "repeats": repeats,
        "baseline_repeats": baseline_repeats,
        "reviewer": {"models": reviewer or [REVIEWER], "fixed_per_sample": True},
        "budget": {
            "max_runs": max_runs,
            "max_models": max_models,
            "max_reviews": max_reviews,
            "max_wall_minutes": max_wall_minutes,
            "worker_timeout_seconds": worker_timeout_seconds,
        },
        "output_dir": str(output_dir),
    }


def write_tree(
    root: Path,
    document: dict[str, Any],
    dispatches: list[dict[str, Any]],
    *,
    agents: str = SHORT_AGENTS,
) -> Path:
    """Write an experiment directory. Returns the experiment file path."""
    exp = root / "exp"
    (exp / "samples").mkdir(parents=True)
    (exp / "variants" / "short").mkdir(parents=True)
    (exp / "variants" / "short" / "AGENTS.md").write_text(agents, encoding="utf-8")
    (exp / "samples" / "dispatches.json").write_text(
        json.dumps(dispatches) + "\n", encoding="utf-8"
    )
    path = exp / "experiment.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


@dataclass
class RecordingRunner:
    """In-process stand-in for the launch script. Records argv, never execs it."""

    action: str = "commit"
    calls: int = 0
    argvs: list[list[str]] = field(default_factory=list)

    async def __call__(self, argv: list[str], cwd: Path, log_path: Path) -> int:
        self.calls += 1
        self.argvs.append(list(argv))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(WORKER_TOKEN + "\n", encoding="utf-8")
        if self.action == "commit":
            _commit_note(cwd)
            return 0
        if self.action == "dirty":
            (cwd / "note.txt").write_text("dirty\n", encoding="utf-8")
            return 0
        if self.action == "fail":
            return 1
        if self.action == "rate":
            return 75
        if self.action == "hang":
            await asyncio.sleep(30)
            return 0
        return 0


def _commit_note(cwd: Path) -> None:
    env = git_env(
        ("GIT_AUTHOR_NAME", "worker"),
        ("GIT_AUTHOR_EMAIL", "worker@invalid"),
        ("GIT_COMMITTER_NAME", "worker"),
        ("GIT_COMMITTER_EMAIL", "worker@invalid"),
    )
    (cwd / "note.txt").write_text("worker note\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "note.txt"], cwd=cwd, env=env, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "note"],
        cwd=cwd,
        env=env,
        check=True,
    )


@dataclass
class ScriptedBackend:
    """Review backend. Each reply is consumed once; leftovers repeat the last."""

    replies: list[str] = field(default_factory=list)
    calls: int = 0
    prompts: list[str] = field(default_factory=list)

    async def review(self, model: str, prompt: str) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        if not self.replies:
            return "not json at all"
        if len(self.replies) == 1:
            return self.replies[0]
        return self.replies.pop(0)


@dataclass
class ScriptedCommands:
    """G2 command runner. ``codes`` is cycled when the script is shorter."""

    codes: list[int] = field(default_factory=lambda: [0])
    calls: int = 0
    argvs: list[list[str]] = field(default_factory=list)

    async def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout: float,
        log_path: Path,
    ) -> CommandResult:
        code = self.codes[self.calls % len(self.codes)]
        self.calls += 1
        self.argvs.append(list(argv))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"exit {code}\n", encoding="utf-8")
        # 124 is the focused-test timeout exit. GateRunner only keeps the
        # g2_timeout reason when timed_out is set.
        return CommandResult(exit_code=code, duration_ms=3, timed_out=code == 124)


def pass_reply() -> str:
    return json.dumps({"passed": True, "blockers": [], "verdict": "ok"})


def block_reply() -> str:
    return json.dumps(
        {"passed": False, "blockers": [BLOCKER_TOKEN], "verdict": "blocked"}
    )


def budget_spec(**overrides: int) -> BudgetSpec:
    raw = {
        "max_runs": 4,
        "max_models": 2,
        "max_reviews": 4,
        "max_wall_minutes": 10,
        "worker_timeout_seconds": 60,
    }
    raw.update(overrides)
    return BudgetSpec(**raw)


def make_record(**overrides: Any) -> RunRecord:
    raw: dict[str, Any] = {
        "run8": "aaaaaaaa",
        "sample_key": f"{DISPATCH_ID}:{LINE_ID}",
        "sample_index": 0,
        "variant_id": "baseline",
        "model": MODEL,
        "repeat": 0,
        "reviewer_model": REVIEWER,
        "worker_status": "committed",
        "completed": True,
        "score": 0.5,
    }
    raw.update(overrides)
    return RunRecord(**raw)


def dummy_script(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    return path


@dataclass
class World:
    root: Path
    repo: Path
    base: str
    answer: str
    output: Path
    experiment: Path
    document: dict[str, Any]


def build_world(
    tmp_path: Path,
    *,
    dispatches: list[dict[str, Any]] | None = None,
    **experiment_kwargs: Any,
) -> World:
    repo, base, answer = make_source(tmp_path / "repo")
    output = tmp_path / "out"
    rows = dispatches if dispatches is not None else [dispatch_document(base)]
    lines = experiment_kwargs.pop(
        "lines",
        [f"{row.get('dispatch_id') or row.get('id')}:{item['id']}" for row in rows for item in row["lines"]],
    )
    document = experiment_document(
        repo=repo,
        output_dir=output,
        lines=lines,
        **experiment_kwargs,
    )
    path = write_tree(tmp_path, document, rows)
    return World(
        root=tmp_path,
        repo=repo,
        base=base,
        answer=answer,
        output=output,
        experiment=path,
        document=document,
    )
