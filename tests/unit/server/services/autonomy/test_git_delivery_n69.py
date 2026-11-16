"""N69: the tool-cache allowlist only covers what a cache really holds.

Before this change a file anywhere under a cache-named directory read as a
tool cache: a forged ``src/__pycache__/evil.py`` or ``src/node_modules/x.js``
read delivered-clean. A ``.tox`` or ``venv`` with its own ``*`` ``.gitignore``
made every delivery unknown, a moved submodule HEAD read clean, and the caches
a run created were never shown. Fake repositories run through a plain host
runner.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.server.services.autonomy.git_delivery import (
    GitSnapshot,
    capture_git_snapshot,
    git_command_runner,
    safe_git_workspace_summary,
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


def _repo(path: Path, gitignore: str | None = None) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Test Worker")
    _git(path, "config", "user.email", "worker@example.invalid")
    (path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    (path / "src").mkdir()
    (path / "src" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    if gitignore is not None:
        (path / ".gitignore").write_text(gitignore, encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "baseline")
    return path


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _summary(workspace: Path, during, setup=None) -> str:
    if setup is not None:
        setup(workspace)
    baseline = capture_git_snapshot(str(workspace))
    during(workspace)
    summary = safe_git_workspace_summary(str(workspace), baseline)
    assert summary is not None
    return summary


# --- forged code in cache-named directories is work ----------------------


@pytest.mark.parametrize(
    "forged",
    [
        "src/__pycache__/evil.py",
        "src/__pycache__/evil.pth",
        "src/__pycache__/nested/mod.cpython-313.pyc",
        "src/__pycache__/.gitignore",
        "src/.pytest_cache/evil.py",
        "src/.mypy_cache/3.13/evil.pth",
        ".ruff_cache/hook.sh",
        "src/node_modules/evil.js",
        "src/.venv/lib/site-packages/evil.pth",
        "src/.tox/py/evil.py",
    ],
)
def test_forged_code_in_a_cache_named_directory_is_uncommitted(
    tmp_path: Path, forged: str
) -> None:
    summary = _summary(_repo(tmp_path / "w"), lambda p: _write(p / forged))

    assert summary.startswith("Uncommitted")
    assert forged in summary


def test_forged_code_a_cache_gitignore_hides_is_still_found(tmp_path: Path) -> None:
    def during(p: Path) -> None:
        _write(p / ".pytest_cache" / ".gitignore", "*\n")
        _write(p / ".pytest_cache" / "v" / "cache" / "nodeids", "[]")
        _write(p / ".pytest_cache" / "conftest.py", "evil")

    summary = _summary(_repo(tmp_path / "w"), during)

    assert summary.startswith("Uncommitted")
    assert ".pytest_cache/conftest.py" in summary
    assert "nodeids" not in summary.split("Tool caches")[0]


def test_a_rogue_gitignore_in_pycache_does_not_hide_code(tmp_path: Path) -> None:
    def during(p: Path) -> None:
        _write(p / "src" / "__pycache__" / ".gitignore", "*\n")
        _write(p / "src" / "__pycache__" / "evil.py", "evil")

    summary = _summary(_repo(tmp_path / "w"), during)

    assert summary.startswith("Uncommitted")
    assert "src/__pycache__/evil.py" in summary


# --- what caches really hold stays out of the way -----------------------


def test_real_cache_contents_are_not_work(tmp_path: Path) -> None:
    def during(p: Path) -> None:
        _write(p / "src" / "__pycache__" / "mod.cpython-313.pyc", "pyc")
        _write(p / ".pytest_cache" / ".gitignore", "*\n")
        _write(p / ".pytest_cache" / "CACHEDIR.TAG", "tag")
        _write(p / "src" / ".mypy_cache" / "3.13" / "mod.data.json", "{}")
        _write(p / ".ruff_cache" / "0.15.14" / "1234567890", "bin")
        _write(p / ".venv" / "lib" / "site-packages" / "pkg.pth", "import os")
        _write(p / "node_modules" / "pkg" / "index.js", "js")
        _write(p / "web" / "package.json", "{}")
        _write(p / "web" / "node_modules" / "pkg" / "index.js", "js")
        _write(p / "tools" / "pyproject.toml", "[project]\n")
        _write(p / "tools" / ".venv" / "lib" / "x.py", "x")

    workspace = _repo(tmp_path / "w")
    baseline = capture_git_snapshot(str(workspace))
    during(workspace)

    # The manifests beside the nested environments are new files: work.
    summary = safe_git_workspace_summary(str(workspace), baseline)
    assert summary is not None
    rows = summary.split("git diff --stat")[0]
    assert "web/package.json" in rows
    assert "tools/pyproject.toml" in rows
    assert "index.js" not in rows
    assert ".pyc" not in rows
    assert ".venv" not in rows
    current = capture_git_snapshot(str(workspace))
    assert current.tool_caches == (
        ".pytest_cache", ".ruff_cache", ".venv", "node_modules",
        "src/.mypy_cache", "src/__pycache__", "tools/.venv", "web/node_modules",
    )


@pytest.mark.parametrize("name", [".tox", "venv"])
def test_a_self_ignoring_environment_that_predates_the_run_is_not_unknown(
    tmp_path: Path, name: str
) -> None:
    def setup(p: Path) -> None:
        _write(p / name / ".gitignore", "*\n")
        _write(p / name / "pyvenv.cfg", "home = /usr\n")
        _write(p / name / "lib" / "x.py", "x")

    summary = _summary(
        _repo(tmp_path / "w"),
        lambda p: _write(p / name / "lib" / "y.py", "y"),
        setup=setup,
    )

    assert summary == ""


def test_a_venv_without_pyvenv_cfg_is_an_ordinary_directory(tmp_path: Path) -> None:
    summary = _summary(_repo(tmp_path / "w"), lambda p: _write(p / "venv" / "evil.py"))

    assert summary.startswith("Uncommitted")
    assert "venv/evil.py" in summary


# --- the caches a run created are shown -----------------------------------


def test_new_tool_caches_are_listed_apart_from_work(tmp_path: Path) -> None:
    def setup(p: Path) -> None:
        _write(p / ".venv" / "lib" / "x.py", "x")

    def during(p: Path) -> None:
        _write(p / "src" / "__pycache__" / "mod.cpython-313.pyc", "pyc")
        _write(p / "src" / "new.py", "work")

    summary = _summary(_repo(tmp_path / "w"), during, setup=setup)

    assert summary.startswith("Uncommitted")
    work, _, caches = summary.partition("Tool caches created during the run")
    assert "src/new.py" in work
    assert "src/__pycache__" in caches
    assert ".venv" not in caches


def test_new_tool_caches_survive_a_persisted_baseline(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "w")
    _write(workspace / ".venv" / "lib" / "x.py", "x")
    baseline = GitSnapshot.from_record(capture_git_snapshot(str(workspace)).to_record())
    assert baseline is not None
    _write(workspace / "node_modules" / "pkg" / "index.js", "js")

    current = capture_git_snapshot(str(workspace))

    assert current.new_tool_caches(baseline) == ("node_modules",)
    record = capture_git_snapshot(str(workspace)).to_record()
    assert all(".venv" not in item for item in record["tool_cache_ids"])
    # An old record without the ids still loads; every cache is then new.
    record.pop("tool_cache_ids")
    old = GitSnapshot.from_record(record)
    assert old is not None and old.tool_cache_ids is None
    assert current.new_tool_caches(old) == (".venv", "node_modules")
    assert GitSnapshot.from_record({**record, "tool_cache_ids": ["nope"]}) is None


# --- a submodule whose checked-out commit moved is a change ---------------


def test_a_moved_submodule_head_is_uncommitted(tmp_path: Path) -> None:
    upstream = _repo(tmp_path / "upstream")
    superproject = _repo(tmp_path / "super")
    _git(
        superproject, "-c", "protocol.file.allow=always",
        "submodule", "add", "-q", str(upstream), "vend",
    )
    _git(superproject, "commit", "-q", "-m", "submodule")

    def during(p: Path) -> None:
        checkout = p / "vend"
        _write(checkout / "tracked.txt", "moved\n")
        _git(checkout, "commit", "-q", "-am", "moved")

    summary = _summary(superproject, during)

    assert summary.startswith("Uncommitted")
    assert "vend" in summary


def test_a_second_submodule_move_after_a_first_is_still_a_change(tmp_path: Path) -> None:
    upstream = _repo(tmp_path / "upstream")
    superproject = _repo(tmp_path / "super")
    _git(
        superproject, "-c", "protocol.file.allow=always",
        "submodule", "add", "-q", str(upstream), "vend",
    )
    _git(superproject, "commit", "-q", "-m", "submodule")
    checkout = superproject / "vend"

    def move(text: str) -> None:
        _write(checkout / "tracked.txt", text)
        _git(checkout, "commit", "-q", "-am", text)

    summary = _summary(superproject, lambda p: move("second\n"), setup=lambda p: move("first\n"))

    assert summary.startswith("Uncommitted")
    assert "vend" in summary


def test_a_nested_virtualenv_that_predates_the_run_is_not_unknown(tmp_path: Path) -> None:
    def setup(p: Path) -> None:
        _write(p / "svc" / "requirements.txt", "requests\n")
        _git(p, "add", "svc/requirements.txt")
        _git(p, "commit", "-q", "-m", "svc")
        # uv: pyvenv.cfg and a self-ignoring .gitignore.
        _write(p / "svc" / ".venv" / ".gitignore", "*\n")
        _write(p / "svc" / ".venv" / "pyvenv.cfg", "home = /usr\n")
        _write(p / "svc" / ".venv" / "lib" / "x.py", "x")
        _write(p / "other" / ".venv" / "pyvenv.cfg", "home = /usr\n")

    summary = _summary(
        _repo(tmp_path / "w"),
        lambda p: _write(p / "other" / ".venv" / "lib" / "y.py", "y"),
        setup=setup,
    )

    assert summary == ""
