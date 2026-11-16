"""A cross-review run's private writable scratch.

A reviewer is one read-only native-CLI turn, so its sandbox denies every write and
tests cannot create temp dirs. The scratch is a 0700 dir outside the repository whose
path every native CLI child gets as TMPDIR / UV_CACHE_DIR / test caches, and for Codex
the only writable root (``--sandbox workspace-write -C <scratch>``). The repo stays
read-only.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from src.llms.native_cli.codex import CodexExecAdapter
from src.llms.native_cli.process import merged_env
from src.llms.native_cli.review_scratch import (
    current_review_scratch,
    review_scratch,
    scratch_prompt_note,
)

KNOBS = (
    "sandbox_workspace_write.network_access=false",
    "sandbox_workspace_write.exclude_slash_tmp=true",
    "sandbox_workspace_write.exclude_tmpdir_env_var=true",
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return tmp_path / "runs" / "review-scratch"


def _value_after(argv: tuple[str, ...], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_scratch_is_private_outside_the_repo_and_removed_on_exit(base, repo) -> None:
    assert current_review_scratch() is None
    with review_scratch(base, repo) as scratch:
        assert current_review_scratch() is scratch
        root = scratch.root
        assert root.is_dir()
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert root.parent == base.resolve()
        assert not root.is_relative_to(repo.resolve())
        for sub in (scratch.tmp, scratch.uv_cache, scratch.pytest_cache, scratch.pycache):
            assert sub.is_dir() and sub.is_relative_to(root)
        env = scratch.env()
        for key in (
            "TMPDIR",
            "TMP",
            "TEMP",
            "UV_CACHE_DIR",
            "PYTHONPYCACHEPREFIX",
            "XDG_CACHE_HOME",
            "npm_config_cache",
        ):
            assert Path(env[key]).is_relative_to(root), key
        assert env["UV_NO_SYNC"] == "1"
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert "no:cacheprovider" in env["PYTEST_ADDOPTS"]
        (scratch.tmp / "junk").write_text("x")
    assert not root.exists()
    assert current_review_scratch() is None


def test_scratch_is_removed_and_reset_when_the_body_raises(base, repo) -> None:
    with pytest.raises(RuntimeError):
        with review_scratch(base, repo) as scratch:
            root = scratch.root
            raise RuntimeError("boom")
    assert not root.exists()
    assert current_review_scratch() is None


def test_each_run_gets_its_own_scratch(base, repo) -> None:
    with review_scratch(base, repo) as a, review_scratch(base, repo) as b:
        assert a.root != b.root


def test_scratch_inside_the_repo_is_refused(repo) -> None:
    with pytest.raises(ValueError, match="overlaps"):
        with review_scratch(repo / ".scratch", repo):
            pass
    assert not (repo / ".scratch").exists()


def test_repo_inside_the_scratch_base_is_refused(tmp_path) -> None:
    base = tmp_path / "scratch-base"
    inner_repo = base / "repo"
    inner_repo.mkdir(parents=True)
    with pytest.raises(ValueError, match="overlaps"):
        with review_scratch(base, inner_repo):
            pass
    assert inner_repo.exists()


def test_overlap_through_a_symlinked_alias_is_refused(tmp_path, repo) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    with pytest.raises(ValueError, match="overlaps"):
        with review_scratch(alias / "scratch", repo):
            pass
    with pytest.raises(ValueError, match="overlaps"):
        with review_scratch(repo / "scratch", alias):
            pass


def test_prompt_note_is_short_and_names_both_paths(base, repo) -> None:
    with review_scratch(base, repo) as scratch:
        note = scratch_prompt_note(scratch)
        assert str(scratch.root) in note
        assert str(scratch.repo) in note
        assert "read-only" in note
        assert "unverified" in note
        assert len(note.encode()) - len(str(scratch.root)) - len(str(scratch.repo)) < 400


def test_merged_env_carries_the_scratch_under_adapter_overrides(base, repo, monkeypatch) -> None:
    monkeypatch.setenv("TMPDIR", "/somewhere/else")
    assert merged_env(None)["TMPDIR"] == "/somewhere/else"
    with review_scratch(base, repo) as scratch:
        env = merged_env({"CODEX_HOME": "/x", "TMP": "/adapter-wins"})
        assert env["TMPDIR"] == str(scratch.tmp)
        assert env["UV_CACHE_DIR"] == str(scratch.uv_cache)
        assert env["CODEX_HOME"] == "/x"
        assert env["TMP"] == "/adapter-wins"
    assert merged_env(None)["TMPDIR"] == "/somewhere/else"


def test_prepare_process_with_overrides_keeps_the_scratch(base, repo, monkeypatch) -> None:
    from src.llms.native_cli.isolation import prepare_process

    monkeypatch.setenv("TMPDIR", "/somewhere/else")
    with review_scratch(base, repo) as scratch:
        # An invocation with adapter env (Codex MCP tokens) skips merged_env.
        _argv, env, _cwd = prepare_process(("codex",), str(repo), {"CODEX_HOME": "/x", "TMP": "/adapter-wins"})
        assert env is not None
        assert env["TMPDIR"] == str(scratch.tmp)
        assert env["UV_CACHE_DIR"] == str(scratch.uv_cache)
        assert env["CODEX_HOME"] == "/x"
        assert env["TMP"] == "/adapter-wins"
    _argv, env, _cwd = prepare_process(("codex",), str(repo), {"CODEX_HOME": "/x"})
    assert env["TMPDIR"] == "/somewhere/else"


def test_codex_reviewer_pins_writable_roots_and_approval(base, repo) -> None:
    adapter = CodexExecAdapter(binary="/opt/fake/codex", cwd=str(repo), read_only=True)
    with review_scratch(base, repo):
        argv = list(adapter.build_invocation("review").argv)
    pairs = {argv[i + 1] for i, item in enumerate(argv[:-1]) if item == "-c"}
    assert "sandbox_workspace_write.writable_roots=[]" in pairs
    assert 'approval_policy="never"' in pairs
    plain = list(adapter.build_invocation("review").argv)
    assert 'approval_policy="never"' not in plain


def test_codex_reviewer_writes_only_to_the_scratch(base, repo) -> None:
    adapter = CodexExecAdapter(binary="/opt/fake/codex", cwd=str(repo), read_only=True)
    with review_scratch(base, repo) as scratch:
        argv = adapter.build_invocation("review").argv
    assert _value_after(argv, "--sandbox") == "workspace-write"
    assert "read-only" not in argv
    for knob in KNOBS:
        i = argv.index(knob)
        assert argv[i - 1] == "-c"
    assert argv.count("-C") == 1
    assert _value_after(argv, "-C") == str(scratch.root)
    assert str(repo) not in argv


def test_codex_argv_is_unchanged_without_a_scratch(base, repo) -> None:
    adapter = CodexExecAdapter(binary="/opt/fake/codex", cwd=str(repo), read_only=True)
    argv = adapter.build_invocation("review").argv
    assert argv == (
        "/opt/fake/codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "-C",
        str(repo),
        "-",
    )


def test_codex_resume_keeps_its_policy_under_a_scratch(base, repo) -> None:
    adapter = CodexExecAdapter(binary="/opt/fake/codex", cwd=str(repo), read_only=True)
    plain = adapter.build_invocation("review", session_id="sess-1").argv
    with review_scratch(base, repo):
        scratched = adapter.build_invocation("review", session_id="sess-1").argv
    assert scratched == plain
    assert "workspace-write" not in scratched


@pytest.mark.parametrize(
    "kwargs",
    [
        {"read_only": False},
        {"outer_sandboxed": True},
        {"approve_for_me": True},
    ],
)
def test_codex_only_a_plain_read_only_turn_uses_the_scratch(base, repo, kwargs) -> None:
    adapter = CodexExecAdapter(binary="/opt/fake/codex", cwd=str(repo), **kwargs)
    plain = adapter.build_invocation("review").argv
    with review_scratch(base, repo):
        assert adapter.build_invocation("review").argv == plain


def test_codex_keeps_read_only_when_its_cwd_overlaps_the_scratch(base, repo) -> None:
    with review_scratch(base, repo) as scratch:
        adapter = CodexExecAdapter(
            binary="/opt/fake/codex", cwd=str(scratch.root.parent), read_only=True
        )
        argv = adapter.build_invocation("review").argv
    assert _value_after(argv, "--sandbox") == "read-only"
    assert "workspace-write" not in argv


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("codex") is None,
    reason="needs macOS seatbelt and the codex CLI",
)
def test_real_codex_sandbox_writes_the_scratch_but_not_the_repo(tmp_path, base, repo) -> None:
    # The fake repo sits under pytest's tmp_path (the *parent's* TMPDIR); the child's
    # TMPDIR is the scratch, so nothing but ``-C`` makes the repo writable.
    codex = shutil.which("codex")
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    with review_scratch(base, repo) as scratch:
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            **scratch.env(),
        }

        def touch(target: Path) -> subprocess.CompletedProcess:
            return subprocess.run(
                [codex, "sandbox", "-P", ":workspace", "-C", str(scratch.root), "--",
                 "/usr/bin/touch", str(target)],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

        ok = touch(scratch.tmp / "probe")
        if not (scratch.tmp / "probe").exists():
            pytest.skip(f"codex sandbox did not run here: {ok.stderr[-300:]!r}")
        denied = touch(repo / "probe")
        repo_written = (repo / "probe").exists()
    if repo_written:
        if repo.resolve().is_relative_to("/private/tmp"):
            pytest.xfail("the :workspace profile grants /tmp, and tmp_path is under /tmp here")
        pytest.fail("the repository was writable from inside the codex sandbox")
    assert denied.returncode != 0
    assert "Operation not permitted" in denied.stderr
