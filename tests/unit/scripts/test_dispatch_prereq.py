"""Review/gate launch hold and the sandbox .done fallback.

These lock the contract in scripts/dispatch/prereq.py. A tree with no author
commit, or a commit older than the author prompt, stays not-ready. A marker
the sandbox cannot create in the logs directory is written under the
dispatcher-owned directory instead.
"""

from __future__ import annotations

import errno
import importlib.util
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "dispatch" / "prereq.py"
SPEC = importlib.util.spec_from_file_location("dispatch_prereq", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
prereq = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = prereq
SPEC.loader.exec_module(prereq)


def _git(repo: Path, *args: str, when: int | None = None) -> str:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_CONFIG")
    }
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_AUTHOR_NAME"] = "Tester"
    env["GIT_AUTHOR_EMAIL"] = "tester@example.com"
    env["GIT_COMMITTER_NAME"] = "Tester"
    env["GIT_COMMITTER_EMAIL"] = "tester@example.com"
    if when is not None:
        stamp = datetime.fromtimestamp(when, UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        env["GIT_AUTHOR_DATE"] = stamp
        env["GIT_COMMITTER_DATE"] = stamp
    proc = subprocess.run(
        [
            "git",
            "-c",
            "user.email=tester@example.com",
            "-c",
            "user.name=Tester",
            "-C",
            str(repo),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.stdout.strip()


def _repo(path: Path) -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")


def _commit(path: Path, when: int) -> str:
    (path / "f.txt").write_text(f"{when}\n")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", f"c{when}", when=when)
    return _git(path, "rev-parse", "HEAD")


def _prompt(prompts: Path, name: str, mtime: int, body: str = "do the work\n") -> Path:
    prompts.mkdir(exist_ok=True)
    path = prompts / f"{name}.txt"
    path.write_text(body)
    os.utime(path, (mtime, mtime))
    return path


def test_dev_job_is_ready_without_a_commit(tmp_path: Path) -> None:
    result = prereq.check_prereq(tmp_path, "deadbeef", "tooling-fixes")
    assert result.status == prereq.STATUS_READY
    assert result.ready
    assert result.reason == prereq.REASON_NOT_REVIEW


@pytest.mark.parametrize(
    ("job", "held"),
    [
        ("rv-sa-isolation-fix7", True),
        ("rv2-beyond-router-plan-sync-r4", True),
        ("rr9-sa-a2a", True),
        ("tr-ob-switch", True),
        ("dbrev-ob-switch", True),
        ("review-sa-a2a", True),
        ("cur-skills-hub", True),
        ("gate-stack-r4-r6", True),
        ("gate", True),
        ("gateway", False),
        ("cursor-fix", False),
        ("sa-isolation-fix6", False),
        ("rereview-agent-fleet", False),
    ],
)
def test_review_and_gate_names(job: str, held: bool) -> None:
    assert prereq.is_review_or_gate(job) is held


def test_missing_author_commit_is_not_ready(tmp_path: Path) -> None:
    repo = tmp_path / "sa-isolation"
    prompts = tmp_path / "prompts"
    _repo(repo)
    _commit(repo, 1_700_000_000)
    _prompt(prompts, "sa-isolation-fix7", 1_700_000_100)
    result = prereq.check_prereq(
        repo,
        "0123456789abcdef0123456789abcdef01234567",
        "rv-sa-isolation-fix7",
        prompts_dir=prompts,
    )
    assert result.status == prereq.STATUS_NOT_READY
    assert result.reason == prereq.REASON_COMMIT


def test_commit_older_than_author_prompt_is_not_ready(tmp_path: Path) -> None:
    repo = tmp_path / "wt"
    prompts = tmp_path / "prompts"
    _repo(repo)
    sha = _commit(repo, 1_700_000_000)
    _prompt(prompts, "fix6", 1_700_000_100)
    _prompt(prompts, "rv-fix6", 1_600_000_000)
    result = prereq.check_prereq(repo, sha, "rv-fix6", prompts_dir=prompts)
    assert result.status == prereq.STATUS_NOT_READY
    assert result.reason == prereq.REASON_NOT_NEWER


def test_author_commit_newer_than_author_prompt_is_ready(tmp_path: Path) -> None:
    """The review prompt is written after the commit; it is not the baseline."""
    repo = tmp_path / "wt"
    prompts = tmp_path / "prompts"
    _repo(repo)
    sha = _commit(repo, 1_700_000_200)
    _prompt(prompts, "fix6", 1_700_000_100)
    _prompt(prompts, "rv2-fix6", 1_700_000_900)
    result = prereq.check_prereq(repo, sha, "rv2-fix6", prompts_dir=prompts)
    assert result.status == prereq.STATUS_READY
    assert result.reason == prereq.REASON_READY


def test_same_second_as_the_prompt_is_not_newer(tmp_path: Path) -> None:
    repo = tmp_path / "wt"
    prompts = tmp_path / "prompts"
    _repo(repo)
    sha = _commit(repo, 1_700_000_200)
    _prompt(prompts, "fix6", 1_700_000_200)
    result = prereq.check_prereq(repo, sha, "rv-fix6", prompts_dir=prompts)
    assert result.reason == prereq.REASON_NOT_NEWER


def test_fractional_prompt_mtime_in_the_commit_second_is_not_newer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "wt"
    prompts = tmp_path / "prompts"
    _repo(repo)
    sha = _commit(repo, 1_700_000_200)
    prompt = _prompt(prompts, "fix6", 1_700_000_200)
    real_stat = Path.stat

    def fake_stat(self: Path, *args: object, **kwargs: object) -> object:
        result = real_stat(self, *args, **kwargs)
        if self.resolve() == prompt.resolve():
            return type(
                "Stat", (), {"st_mode": result.st_mode, "st_mtime": 1_700_000_200.9}
            )()
        return result

    monkeypatch.setattr(Path, "stat", fake_stat)
    result = prereq.check_prereq(repo, sha, "rv-fix6", prompts_dir=prompts)
    assert result.reason == prereq.REASON_NOT_NEWER


def test_commit_not_in_head_is_not_ready(tmp_path: Path) -> None:
    repo = tmp_path / "wt"
    prompts = tmp_path / "prompts"
    _repo(repo)
    first = _commit(repo, 1_700_000_200)
    second = _commit(repo, 1_700_000_300)
    _git(repo, "checkout", "-q", first)
    _prompt(prompts, "fix6", 1_700_000_100)
    result = prereq.check_prereq(repo, second, "rv-fix6", prompts_dir=prompts)
    assert result.status == prereq.STATUS_NOT_READY
    assert result.reason == prereq.REASON_NOT_HEAD


def test_gate_uses_author_job_header_not_the_later_gate_prompt(tmp_path: Path) -> None:
    repo = tmp_path / "stack-r4"
    prompts = tmp_path / "prompts"
    _repo(repo)
    sha = _commit(repo, 1_700_000_200)
    _prompt(prompts, "stack-r4-sync-r6", 1_700_000_100)
    _prompt(
        prompts,
        "gate-stack-r4-r6",
        1_700_000_900,
        "Author-Job: stack-r4-sync-r6\nconfirm HEAD\n",
    )
    result = prereq.check_prereq(repo, sha, "gate-stack-r4-r6", prompts_dir=prompts)
    assert result.status == prereq.STATUS_READY
    assert result.reason == prereq.REASON_READY


def test_gate_without_author_baseline_stays_not_ready_when_prompt_is_newer(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "stack-r4"
    prompts = tmp_path / "prompts"
    _repo(repo)
    sha = _commit(repo, 1_700_000_200)
    _prompt(prompts, "gate-stack-r4-r6", 1_700_000_900, "confirm HEAD\n")
    result = prereq.check_prereq(repo, sha, "gate-stack-r4-r6", prompts_dir=prompts)
    assert result.reason == prereq.REASON_NOT_NEWER


def test_author_job_header_missing_file_is_not_ready(tmp_path: Path) -> None:
    repo = tmp_path / "wt"
    prompts = tmp_path / "prompts"
    _repo(repo)
    sha = _commit(repo, 1_700_000_200)
    _prompt(prompts, "fix6", 1_700_000_050)
    _prompt(prompts, "rv-fix6", 1_700_000_100, "Author-Job: does-not-exist\n")
    result = prereq.check_prereq(repo, sha, "rv-fix6", prompts_dir=prompts)
    assert result.reason == prereq.REASON_PROMPT


def test_default_prompts_dir_is_the_fleet_prompts_folder(tmp_path: Path) -> None:
    fleet = tmp_path / "conductor-wt"
    repo = fleet / "wt"
    _repo(repo)
    sha = _commit(repo, 1_700_000_200)
    _prompt(fleet / "prompts", "fix6", 1_700_000_100)
    result = prereq.check_prereq(repo, sha, "rv-fix6")
    assert result.status == prereq.STATUS_READY
    assert result.reason == prereq.REASON_READY


def test_missing_prompt_is_not_ready(tmp_path: Path) -> None:
    repo = tmp_path / "wt"
    _repo(repo)
    sha = _commit(repo, 1_700_000_200)
    result = prereq.check_prereq(repo, sha, "rv-fix6", prompts_dir=tmp_path / "prompts")
    assert result.reason == prereq.REASON_PROMPT


@pytest.mark.parametrize("job", ["rv-fix6", "rv2-fix6", "rr-fix6", "rr9-fix6"])
def test_missing_author_prompt_does_not_fall_back_to_the_review_file(
    tmp_path: Path, job: str
) -> None:
    """The review file is older than the commit, so using it would look ready."""
    repo = tmp_path / "wt"
    prompts = tmp_path / "prompts"
    _repo(repo)
    sha = _commit(repo, 1_700_000_200)
    _prompt(prompts, job, 1_600_000_000, "review the work\n")
    result = prereq.check_prereq(repo, sha, job, prompts_dir=prompts)
    assert result.status == prereq.STATUS_NOT_READY
    assert result.reason == prereq.REASON_PROMPT


@pytest.mark.parametrize("job", ["rv-fix6", "rr9-fix6"])
def test_missing_review_prompt_still_maps_to_the_author_run(
    tmp_path: Path, job: str
) -> None:
    repo = tmp_path / "wt"
    prompts = tmp_path / "prompts"
    _repo(repo)
    sha = _commit(repo, 1_700_000_200)
    _prompt(prompts, "fix6", 1_700_000_100)
    result = prereq.check_prereq(repo, sha, job, prompts_dir=prompts)
    assert result.status == prereq.STATUS_READY
    assert result.reason == prereq.REASON_READY


def test_gate_without_a_prompt_file_is_not_ready(tmp_path: Path) -> None:
    """A gate does not strip, so a same-suffix author file is not the baseline."""
    repo = tmp_path / "wt"
    prompts = tmp_path / "prompts"
    _repo(repo)
    sha = _commit(repo, 1_700_000_200)
    _prompt(prompts, "stack-r4-r6", 1_700_000_100)
    result = prereq.check_prereq(repo, sha, "gate-stack-r4-r6", prompts_dir=prompts)
    assert result.status == prereq.STATUS_NOT_READY
    assert result.reason == prereq.REASON_PROMPT


def test_gate_author_job_names_a_missing_file(tmp_path: Path) -> None:
    """The gate file predates the commit; falling back to it would look ready."""
    repo = tmp_path / "wt"
    prompts = tmp_path / "prompts"
    _repo(repo)
    sha = _commit(repo, 1_700_000_200)
    _prompt(
        prompts,
        "gate-stack-r4-r6",
        1_600_000_000,
        "Author-Job: stack-r4-sync-r6\nconfirm\n",
    )
    result = prereq.check_prereq(repo, sha, "gate-stack-r4-r6", prompts_dir=prompts)
    assert result.status == prereq.STATUS_NOT_READY
    assert result.reason == prereq.REASON_PROMPT


def test_unreadable_prompts_dir_is_not_ready(tmp_path: Path) -> None:
    repo = tmp_path / "wt"
    prompts = tmp_path / "prompts"
    _repo(repo)
    sha = _commit(repo, 1_700_000_200)
    prompts.mkdir()
    prompts.chmod(0)
    try:
        result = prereq.check_prereq(repo, sha, "rv-fix6", prompts_dir=prompts)
    finally:
        prompts.chmod(0o755)
    assert result.status == prereq.STATUS_NOT_READY
    assert result.reason == prereq.REASON_PROMPT


def test_caller_git_dir_does_not_change_the_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "wt"
    decoy = tmp_path / "other"
    prompts = tmp_path / "prompts"
    _repo(repo)
    _repo(decoy)
    sha = _commit(repo, 1_700_000_200)
    decoy_sha = _commit(decoy, 1_600_000_000)
    _prompt(prompts, "fix6", 1_700_000_100)
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    monkeypatch.setenv("GIT_INDEX_FILE", str(decoy / ".git" / "index"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(decoy))
    result = prereq.check_prereq(repo, sha, "rv-fix6", prompts_dir=prompts)
    assert result.status == prereq.STATUS_READY
    assert result.reason == prereq.REASON_READY
    other = prereq.check_prereq(repo, decoy_sha, "rv-fix6", prompts_dir=prompts)
    assert other.status == prereq.STATUS_NOT_READY
    assert other.reason == prereq.REASON_COMMIT


def test_unsafe_review_name_is_not_ready(tmp_path: Path) -> None:
    result = prereq.check_prereq(tmp_path, "HEAD", "rv-foo/../../etc")
    assert result.status == prereq.STATUS_NOT_READY
    assert result.reason == prereq.REASON_BAD_JOB


def test_empty_worktree_is_commit_missing(tmp_path: Path) -> None:
    prompts = tmp_path / "prompts"
    _prompt(prompts, "fix6", 1_700_000_100)
    result = prereq.check_prereq(
        tmp_path / "missing",
        "HEAD",
        "rv-fix6",
        prompts_dir=prompts,
    )
    assert result.reason == prereq.REASON_COMMIT


def test_git_timeout_is_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts = tmp_path / "prompts"
    _prompt(prompts, "fix6", 1_700_000_100)

    def explode(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=30)

    monkeypatch.setattr(prereq.subprocess, "run", explode)
    result = prereq.check_prereq(tmp_path, "HEAD", "rv-fix6", prompts_dir=prompts)
    assert result.status == prereq.STATUS_NOT_READY
    assert result.reason == prereq.REASON_GIT


def test_check_cli_exit_codes(tmp_path: Path) -> None:
    repo = tmp_path / "wt"
    prompts = tmp_path / "prompts"
    _repo(repo)
    old = _commit(repo, 1_700_000_000)
    new = _commit(repo, 1_700_000_200)
    _prompt(prompts, "fix6", 1_700_000_100)
    held = _run_cli(
        "check",
        "--worktree",
        str(repo),
        "--commit",
        old,
        "--job",
        "rv-fix6",
        "--prompts",
        str(prompts),
    )
    assert held.returncode == 3
    assert held.stdout.splitlines() == [
        prereq.STATUS_NOT_READY,
        prereq.REASON_NOT_NEWER,
    ]
    ready = _run_cli(
        "check",
        "--worktree",
        str(repo),
        "--commit",
        new,
        "--job",
        "rv-fix6",
        "--prompts",
        str(prompts),
    )
    assert ready.returncode == 0
    assert ready.stdout.splitlines()[0] == prereq.STATUS_READY


def test_primary_marker_is_created_when_the_logs_dir_is_writable(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "logs" / "dsh--stack-r4--gate-stack-r4-r6.log.done"
    marker.parent.mkdir()
    fallback = tmp_path / "jobs" / ".done"
    written = prereq.write_done_marker(marker, fallback_dir=fallback)
    assert written == marker
    assert marker.is_file()
    assert marker.read_bytes() == b""
    assert not fallback.exists()


def test_eacces_falls_back_to_the_dispatcher_dir(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    marker = logs / "dsh--stack-r4--gate.log.done"
    fallback = tmp_path / "jobs" / ".done"
    logs.chmod(0o555)
    try:
        written = prereq.write_done_marker(marker, fallback_dir=fallback)
    finally:
        logs.chmod(0o755)
    assert written == fallback.resolve() / marker.name
    assert not marker.exists()
    text = written.read_text()
    assert f"primary={marker}\n" in text
    assert "errno=EACCES\n" in text


def test_eperm_from_open_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "logs" / "job.log.done"
    marker.parent.mkdir()
    fallback = tmp_path / "jobs" / ".done"
    real_open = os.open

    def guarded(path: object, flags: int, mode: int = 0o777, **kwargs: object) -> int:
        if os.fspath(path) == os.fspath(marker):
            raise PermissionError(errno.EPERM, "sandbox", os.fspath(path))
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(prereq.os, "open", guarded)
    written = prereq.write_done_marker(marker, fallback_dir=fallback)
    assert written == (fallback / "job.log.done").resolve()
    assert not marker.exists()
    assert "errno=EPERM\n" in written.read_text()
    assert f"primary={marker}\n" in written.read_text()


def test_other_oserrors_do_not_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "logs" / "job.log.done"
    fallback = tmp_path / "jobs" / ".done"
    real_open = os.open

    def guarded(path: object, flags: int, mode: int = 0o777, **kwargs: object) -> int:
        if os.fspath(path) == os.fspath(marker):
            raise OSError(errno.EIO, "io", os.fspath(path))
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(prereq.os, "open", guarded)
    with pytest.raises(OSError) as raised:
        prereq.write_done_marker(marker, fallback_dir=fallback)
    assert raised.value.errno == errno.EIO
    assert not fallback.exists()


def test_missing_logs_directory_does_not_fall_back(tmp_path: Path) -> None:
    marker = tmp_path / "logs" / "job.log.done"
    fallback = tmp_path / "jobs" / ".done"
    with pytest.raises(FileNotFoundError):
        prereq.write_done_marker(marker, fallback_dir=fallback)
    assert not fallback.exists()


def test_primary_symlink_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("keep")
    marker = tmp_path / "logs" / "job.log.done"
    marker.parent.mkdir()
    marker.symlink_to(outside)
    fallback = tmp_path / "jobs" / ".done"
    written = prereq.write_done_marker(marker, fallback_dir=fallback)
    assert written.parent == fallback.resolve()
    assert outside.read_text() == "keep"
    assert "errno=EPERM\n" in written.read_text()


def test_fallback_symlink_is_refused(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    logs.chmod(0o555)
    marker = logs / "job.log.done"
    outside = tmp_path / "outside"
    outside.write_text("keep")
    fallback = tmp_path / "jobs" / ".done"
    fallback.mkdir(parents=True)
    link = fallback / marker.name
    link.symlink_to(outside)
    try:
        with pytest.raises(OSError) as raised:
            prereq.write_done_marker(marker, fallback_dir=fallback)
    finally:
        logs.chmod(0o755)
    assert raised.value.errno == errno.EPERM
    assert outside.read_text() == "keep"


def test_mark_done_cli_prints_the_path(tmp_path: Path) -> None:
    marker = tmp_path / "logs" / "job.log.done"
    marker.parent.mkdir()
    proc = _run_cli(
        "mark-done", "--marker", str(marker), "--fallback", str(tmp_path / "fb")
    )
    assert proc.returncode == 0
    assert Path(proc.stdout.strip()) == marker
    assert marker.is_file()


def test_default_fallback_honors_dispatch_done_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DISPATCH_DONE_DIR", str(tmp_path / "custom"))
    assert prereq.default_fallback_dir() == tmp_path / "custom"
    monkeypatch.delenv("DISPATCH_DONE_DIR")
    assert prereq.default_fallback_dir() == prereq.DEFAULT_FALLBACK_DIR


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_CONFIG")
    }
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
