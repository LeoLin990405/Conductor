"""R3: ignore rules a Worker controls, tool caches, submodules, env allowlist.

Fake repositories in ``tmp_path`` run through a plain host runner. Each
hiding case read as delivered-clean before this change; each tool-cache
case read as uncommitted or unknown, blocking deliveries that only ran
tests or installed dependencies.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.server.services.autonomy import git_delivery
from src.server.services.autonomy.git_delivery import (
    capture_git_snapshot,
    clear_ticket_git_baseline,
    git_command_runner,
    hardened_git_invocation,
    is_hardened_git_invocation,
    safe_git_workspace_summary,
    ticket_git_baseline,
)

_UNKNOWN = "Git workspace status is unknown"


@pytest.fixture(autouse=True)
def _host_runner():
    def runner(argv, *, cwd, timeout, env):
        return subprocess.run(
            argv, cwd=cwd, timeout=timeout, env=env, capture_output=True, check=False
        )

    with git_command_runner(runner):
        yield


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Test Worker")
    _git(path, "config", "user.email", "worker@example.invalid")
    (path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    (path / "sub").mkdir()
    (path / "sub" / "keep.txt").write_text("k\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "baseline")
    return path


def _write(path: Path, text: str, mode: str = "w") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8") as handle:
        handle.write(text)


def _many(directory: Path, count: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (directory / f"f{index}.py").write_text("x", encoding="utf-8")


def _verdict(workspace: Path, during, setup=None) -> str:
    if setup is not None:
        setup(workspace)
    baseline = capture_git_snapshot(str(workspace))
    during(workspace)
    summary = safe_git_workspace_summary(str(workspace), baseline)
    assert summary is not None
    return summary


# --- X9: a directory info/exclude hides is still walked -----------------


def test_self_ignoring_dir_also_hidden_by_info_exclude_is_reported(
    tmp_path: Path,
) -> None:
    def during(p: Path) -> None:
        _write(p / ".git" / "info" / "exclude", "/zz/\n", "a")
        _write(p / "zz" / ".gitignore", "*\n")
        _write(p / "zz" / "x.py", "x")

    summary = _verdict(_repo(tmp_path / "w"), during)
    assert summary.startswith("Uncommitted")
    assert "zz/x.py" in summary


# --- X1 / X2: an uncommitted .gitignore that predates the run ------------


def test_preexisting_untracked_gitignore_is_unknown(tmp_path: Path) -> None:
    summary = _verdict(
        _repo(tmp_path / "w"),
        lambda p: _write(p / "sub" / "secret.py", "evil\n"),
        setup=lambda p: _write(p / "sub" / ".gitignore", "secret.py\n"),
    )
    assert summary.startswith(_UNKNOWN)


def test_preexisting_dirty_tracked_gitignore_is_unknown(tmp_path: Path) -> None:
    def setup(p: Path) -> None:
        _write(p / ".gitignore", "x\n")
        _git(p, "add", ".gitignore")
        _git(p, "commit", "-q", "-m", "gitignore")
        _write(p / ".gitignore", "x\nsecret.py\n")

    summary = _verdict(
        _repo(tmp_path / "w"), lambda p: _write(p / "secret.py", "evil\n"), setup=setup
    )
    assert summary.startswith(_UNKNOWN)


# --- tool caches: reported apart, never block delivery -------------------


def test_self_ignoring_venv_over_the_hidden_limit_is_a_tool_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(git_delivery, "_HIDDEN_ROW_LIMIT", 5)
    workspace = _repo(tmp_path / "w")
    _many(workspace / ".venv" / "lib", 20)
    _write(workspace / ".venv" / ".gitignore", "*\n")

    baseline = capture_git_snapshot(str(workspace))
    _many(workspace / ".venv" / "lib" / "newpkg", 3)

    assert safe_git_workspace_summary(str(workspace), baseline) == ""
    assert baseline.tool_caches == (".venv",)


def test_caches_created_during_the_run_are_tool_caches_not_work(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path / "w")
    # N69: a nested node_modules is a cache beside the manifest that owns it.
    _write(workspace / "web" / "package.json", "{}\n")
    baseline = capture_git_snapshot(str(workspace))
    _write(workspace / ".pytest_cache" / ".gitignore", "*\n")
    _write(workspace / ".pytest_cache" / "v" / "cache" / "nodeids", "[]")
    # Untracked and not ignored at all: still a cache by name.
    _write(workspace / "sub" / "__pycache__" / "keep.cpython-313.pyc", "pyc")
    _write(workspace / "web" / "node_modules" / "pkg" / "index.js", "js")

    assert safe_git_workspace_summary(str(workspace), baseline) == ""
    current = capture_git_snapshot(str(workspace))
    assert current.tool_caches == (
        ".pytest_cache", "sub/__pycache__", "web/node_modules",
    )


def test_work_outside_tool_caches_stays_strict(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "w")
    _write(workspace / "node_modules" / "vendored.js", "v1\n")
    _git(workspace, "add", "-f", "node_modules/vendored.js")
    _git(workspace, "commit", "-q", "-m", "vendored")
    baseline = capture_git_snapshot(str(workspace))
    _write(workspace / "node_modules" / "vendored.js", "v2\n")
    _write(workspace / "sub" / "__pycache__.py", "not a cache dir\n")

    summary = safe_git_workspace_summary(str(workspace), baseline)
    assert summary.startswith("Uncommitted")
    assert "node_modules/vendored.js" in summary
    assert "sub/__pycache__.py" in summary


def test_hidden_limit_is_unknown_for_that_check_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(git_delivery, "_HIDDEN_ROW_LIMIT", 5)
    workspace = _repo(tmp_path / "w")
    _write(workspace / ".git" / "info" / "exclude", "/hidden/\n", "a")
    _many(workspace / "hidden", 10)
    clear_ticket_git_baseline("r3-limit")

    assert ticket_git_baseline("r3-limit", str(workspace)) == (None, True)
    for child in (workspace / "hidden").iterdir():
        child.unlink()
    snapshot, unavailable = ticket_git_baseline("r3-limit", str(workspace))
    clear_ticket_git_baseline("r3-limit")

    assert not unavailable
    assert snapshot is not None


# --- submodule checkout: its gitdir's core.worktree is normal -------------


def test_submodule_checkout_is_checked_not_unknown(tmp_path: Path) -> None:
    upstream = _repo(tmp_path / "upstream")
    superproject = _repo(tmp_path / "super")
    _git(
        superproject, "-c", "protocol.file.allow=always",
        "submodule", "add", "-q", str(upstream), "vend",
    )
    _git(superproject, "commit", "-q", "-m", "submodule")
    checkout = superproject / "vend"

    summary = _verdict(checkout, lambda p: _write(p / "tracked.txt", "edit\n"))

    assert summary.startswith("Uncommitted")
    assert "tracked.txt" in summary


# --- env allowlist: only the builder's own keys --------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"GIT_DIR": "/elsewhere"},
        {"GIT_WORK_TREE": "/elsewhere"},
        {"GIT_EXTERNAL_DIFF": "/evil"},
        {"GIT_INDEX_FILE": "/elsewhere/index"},
        {"GIT_OBJECT_DIRECTORY": "/elsewhere/objects"},
        {"GIT_ALTERNATE_OBJECT_DIRECTORIES": "/elsewhere/objects"},
        {"GIT_CONFIG_KEY_7": "merge.x.driver", "GIT_CONFIG_VALUE_7": ""},
        {"GIT_EXEC_PATH": "/evil"},
        {"GIT_SSH_COMMAND": "/evil"},
        {"LD_PRELOAD": "/evil.so"},
    ],
)
def test_hardened_check_refuses_any_env_key_the_builder_did_not_emit(extra) -> None:
    argv, env = hardened_git_invocation("git", ("status",))
    assert is_hardened_git_invocation(argv, env)
    assert not is_hardened_git_invocation(argv, {**env, **extra})


@pytest.mark.parametrize(
    "arg", ["--config-env=core.hooksPath=GIT_X", "--config-env", "--exec-path=/evil"]
)
def test_hardened_check_refuses_config_env_and_exec_path_args(arg) -> None:
    argv, env = hardened_git_invocation("git", ("--version",))
    assert not is_hardened_git_invocation((*argv[:-1], arg, argv[-1]), env)
