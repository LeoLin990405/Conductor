"""Bounded subprocess output for isolated Git delivery commands."""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.server.services.autonomy import git_delivery
from src.server.services.autonomy.git_delivery import (
    GitDeliveryError,
    _stream_git_output,
    git_command_runner,
    run_isolated_git_mutation,
    run_isolated_git_readonly,
)


def _run_python(
    tmp_path: Path,
    source: str,
    *,
    output_limit: int = git_delivery._GIT_OUTPUT_LIMIT_BYTES,
    timeout: float = 5,
):
    return _stream_git_output(
        [sys.executable, "-c", source],
        cwd=str(tmp_path),
        env={},
        timeout=timeout,
        output_limit=output_limit,
    )


def test_streamed_output_keeps_completed_process_contract(tmp_path: Path) -> None:
    result = _run_python(
        tmp_path,
        "import os; os.write(1, b'abc'); os.write(2, b'def')",
        output_limit=6,
    )

    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 0
    assert result.stdout == b"abc"
    assert result.stderr == b"def"


def test_output_limit_is_combined_across_stdout_and_stderr(tmp_path: Path) -> None:
    with pytest.raises(GitDeliveryError, match="5-byte limit"):
        _run_python(
            tmp_path,
            "import os; os.write(1, b'abc'); os.write(2, b'def')",
            output_limit=5,
        )


def test_overflow_kills_the_child_instead_of_waiting_for_it(tmp_path: Path) -> None:
    pid_path = tmp_path / "child.pid"
    source = (
        "import os\n"
        f"open({str(pid_path)!r}, 'w').write(str(os.getpid()))\n"
        "while True: os.write(1, b'x' * 65536)\n"
    )

    with pytest.raises(GitDeliveryError, match="1024-byte limit"):
        _run_python(tmp_path, source, output_limit=1024, timeout=5)

    pid = int(pid_path.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_timeout_kills_the_child(tmp_path: Path) -> None:
    pid_path = tmp_path / "child.pid"
    source = (
        "import os, time\n"
        f"open({str(pid_path)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        _run_python(tmp_path, source, timeout=0.1)

    pid = int(pid_path.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_descendant_holding_output_pipes_cannot_extend_the_deadline(
    tmp_path: Path,
) -> None:
    source = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(60)'])\n"
    )
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        _run_python(tmp_path, source, output_limit=1024, timeout=0.1)

    assert time.monotonic() - started < 1


@pytest.mark.parametrize(
    "function",
    [
        git_delivery._run_git,
        _stream_git_output,
        run_isolated_git_readonly,
        run_isolated_git_mutation,
    ],
)
def test_output_limit_is_keyword_only_with_a_default(function) -> None:
    parameter = inspect.signature(function).parameters["output_limit"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default == git_delivery._GIT_OUTPUT_LIMIT_BYTES


def test_injected_runner_signature_and_public_result_stay_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(git_delivery, "_filter_names", lambda _cwd: ())
    calls: list[tuple[object, ...]] = []

    def runner(argv, *, cwd, timeout, env):
        calls.append((argv, cwd, timeout, env))
        return subprocess.CompletedProcess(argv, 0, stdout=b"abc", stderr=b"d")

    with git_command_runner(runner):
        result = run_isolated_git_readonly(
            str(tmp_path), ["rev-parse", "HEAD"], output_limit=4
        )
        assert result.returncode == 0
        assert result.stdout == b"abc"
        assert result.stderr == b"d"

        with pytest.raises(GitDeliveryError, match="3-byte limit"):
            run_isolated_git_readonly(
                str(tmp_path), ["rev-parse", "HEAD"], output_limit=3
            )

    assert len(calls) == 2
    assert all(call[1] == str(tmp_path) for call in calls)


def test_public_ls_tree_streams_with_bounded_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **git_delivery._git_env(),
        "HOME": str(home),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": os.devnull,
    }

    def git(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            env=env,
        )

    git("init", "-q")
    git("config", "user.email", "bounded@example.invalid")
    git("config", "user.name", "Bounded Test")
    for index in range(64):
        (repo / f"file-{index:03}.txt").write_text("contents\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "initial")
    expected = git("ls-tree", "-r", "-l", "-z", "HEAD").stdout
    assert len(expected) > 4096

    monkeypatch.setattr(git_delivery, "_git_runtime_read_paths", lambda: ())
    monkeypatch.setattr(
        "src.llms.native_cli.isolation.current_worker_isolation", lambda: object()
    )

    def prepare_process(argv, cwd, env, **_kwargs):
        return tuple(argv), dict(env), cwd

    monkeypatch.setattr(
        "src.llms.native_cli.isolation.prepare_process", prepare_process
    )
    monkeypatch.setattr(git_delivery, "_GIT_OUTPUT_READ_BYTES", 64)
    real_read = os.read
    bytes_read = 0

    def counted_read(fd: int, size: int) -> bytes:
        nonlocal bytes_read
        chunk = real_read(fd, size)
        bytes_read += len(chunk)
        return chunk

    # R4-2's driver discovery (``git config`` queries) streams through the
    # same runner; resolve it first so only the ls-tree read is counted.
    drivers = git_delivery._filter_names(str(repo))
    monkeypatch.setattr(git_delivery, "_filter_names", lambda _cwd: drivers)
    monkeypatch.setattr(git_delivery.os, "read", counted_read)
    token = git_delivery._GIT_COMMAND_RUNNER.set(None)
    try:
        result = run_isolated_git_readonly(
            str(repo),
            ["ls-tree", "-r", "-l", "-z", "HEAD"],
            output_limit=len(expected),
        )
        assert result.stdout == expected
        assert bytes_read == len(expected)

        bytes_read = 0
        output_limit = 1024
        with pytest.raises(GitDeliveryError, match="1024-byte limit"):
            run_isolated_git_readonly(
                str(repo),
                ["ls-tree", "-r", "-l", "-z", "HEAD"],
                output_limit=output_limit,
            )
        assert output_limit <= bytes_read <= output_limit + 64
    finally:
        git_delivery._GIT_COMMAND_RUNNER.reset(token)
