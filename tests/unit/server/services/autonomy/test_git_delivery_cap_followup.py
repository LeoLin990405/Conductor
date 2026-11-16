"""N56 follow-up: listing reads scale with paths; evidence reads are trimmed.

The 1 MB cap stopped every Ticket on a repository past about 10k files,
because status/ls-files/ls-tree output grows with the number of paths. The
listing forms now get a larger fixed cap, the index listing is parsed as it
streams, and a review diff past its cap comes back as a marked prefix.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.server.services.autonomy import git_delivery
from src.server.services.autonomy.git_delivery import (
    GitDeliveryError,
    _NulRecords,
    _stream_git_output,
    capture_git_snapshot,
    git_command_runner,
    run_isolated_git_readonly,
    run_isolated_git_readonly_prefix,
)

SMALL = git_delivery._GIT_OUTPUT_LIMIT_BYTES
BIG = git_delivery._GIT_LISTING_OUTPUT_LIMIT_BYTES
LISTINGS = [
    ["ls-tree", "-r", "-l", "-z", "HEAD"],
    ["ls-files", "--stage", "-z"],
    ["ls-files", "--others", "--exclude-standard", "-z"],
    ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all"],
    ["status", "--porcelain", "--untracked-files=all", "--ignore-submodules=all"],
    ["worktree", "list", "--porcelain"],
    ["diff", "--name-only", "--no-ext-diff", "--no-textconv", "main...feature", "--"],
]


def _fake_runner(stdout: bytes, calls: list | None = None):
    def runner(argv, *, cwd, timeout, env):
        if calls is not None:
            calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")

    return runner


def test_the_listing_cap_is_a_named_constant_above_the_small_cap() -> None:
    assert SMALL == 1024 * 1024
    assert BIG == 64 * 1024 * 1024


@pytest.mark.parametrize("args", LISTINGS)
def test_a_listing_over_one_megabyte_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    monkeypatch.setattr(git_delivery, "_filter_names", lambda _cwd: ())
    listing = b"100644 blob " + b"a" * 40 + b" 9\tsrc/some/file.py\0"
    stdout = listing * (2 * SMALL // len(listing))
    assert len(stdout) > SMALL

    with git_command_runner(_fake_runner(stdout)):
        result = run_isolated_git_readonly(str(tmp_path), args)

    assert result.returncode == 0
    assert result.stdout == stdout


@pytest.mark.parametrize("args", LISTINGS[:2])
def test_a_listing_over_the_listing_cap_still_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    monkeypatch.setattr(git_delivery, "_filter_names", lambda _cwd: ())
    stdout = b"x" * (BIG + 1)

    with git_command_runner(_fake_runner(stdout)):
        with pytest.raises(GitDeliveryError, match=f"{BIG}-byte limit"):
            run_isolated_git_readonly(str(tmp_path), args)


def test_content_reads_keep_the_small_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(git_delivery, "_filter_names", lambda _cwd: ())
    stdout = b"x" * (SMALL + 1)

    with git_command_runner(_fake_runner(stdout)):
        for args in (
            ["diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD"],
            ["diff", "--patch-with-stat", "--no-color", "--no-ext-diff",
             "--no-textconv", "main...feature", "--"],
            ["rev-parse", "HEAD"],
        ):
            with pytest.raises(GitDeliveryError, match=f"{SMALL}-byte limit"):
                run_isolated_git_readonly(str(tmp_path), args)


def test_an_explicit_cap_on_a_listing_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(git_delivery, "_filter_names", lambda _cwd: ())

    with git_command_runner(_fake_runner(b"x" * 2048)):
        with pytest.raises(GitDeliveryError, match="1024-byte limit"):
            run_isolated_git_readonly(
                str(tmp_path), ["ls-tree", "-r", "-l", "-z", "HEAD"], output_limit=1024
            )


def test_a_real_listing_over_one_megabyte_streams_through_the_isolated_reader(
    tmp_path: Path,
) -> None:
    # The streamed path (no injected runner), with a listing well over 1 MB.
    source = (
        "import os\n"
        f"for _ in range({3 * SMALL // 4096}): os.write(1, b'r' * 4095 + b'\\0')\n"
    )
    result = _stream_git_output(
        [sys.executable, "-c", source],
        cwd=str(tmp_path),
        env={},
        timeout=10,
        output_limit=git_delivery._effective_output_limit(
            ["ls-tree", "-r", "-l", "-z", "HEAD"], SMALL
        ),
    )

    assert result.returncode == 0
    assert len(result.stdout) == 3 * SMALL // 4096 * 4096


def test_a_streamed_sink_gets_records_split_across_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(git_delivery, "_GIT_OUTPUT_READ_BYTES", 7)
    records: list[bytes] = []
    parser = _NulRecords(records.append)
    source = (
        "import os\n"
        "os.write(1, b'first-record\\0second\\0')\n"
        "os.write(1, b'third-without-terminator')\n"
    )

    result = _stream_git_output(
        [sys.executable, "-c", source],
        cwd=str(tmp_path),
        env={},
        timeout=5,
        stdout_sink=parser.feed,
    )
    parser.finish()

    assert result.returncode == 0
    assert result.stdout == b"", "a sink means the listing is not also held whole"
    assert records == [b"first-record", b"second", b"third-without-terminator"]


def test_an_unterminated_record_past_the_record_cap_fails() -> None:
    parser = _NulRecords(lambda _record: None)

    with pytest.raises(GitDeliveryError):
        parser.feed(b"x" * (git_delivery._GIT_RECORD_LIMIT_BYTES + 1))


def test_a_streamed_sink_still_counts_toward_the_cap(tmp_path: Path) -> None:
    source = "import os\nwhile True: os.write(1, b'x\\0' * 4096)\n"

    with pytest.raises(GitDeliveryError, match="4096-byte limit"):
        _stream_git_output(
            [sys.executable, "-c", source],
            cwd=str(tmp_path),
            env={},
            timeout=5,
            output_limit=4096,
            stdout_sink=_NulRecords(lambda _record: None).feed,
        )


def test_a_prefix_read_stops_git_at_the_cap_and_marks_the_cut(tmp_path: Path) -> None:
    pid_path = tmp_path / "child.pid"
    source = (
        "import os\n"
        f"open({str(pid_path)!r}, 'w').write(str(os.getpid()))\n"
        "while True: os.write(1, b'+line\\n' * 4096)\n"
    )

    result = _stream_git_output(
        [sys.executable, "-c", source],
        cwd=str(tmp_path),
        env={},
        timeout=5,
        output_limit=10_000,
        truncate=True,
    )

    assert isinstance(result, git_delivery._TruncatedGitOutput)
    assert len(result.stdout) == 10_000
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_path.read_text(encoding="utf-8")), 0)


def test_the_public_prefix_read_trims_instead_of_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(git_delivery, "_filter_names", lambda _cwd: ())
    diff_args = [
        "diff", "--patch-with-stat", "--no-color", "--no-ext-diff",
        "--no-textconv", "main...feature", "--",
    ]

    with git_command_runner(_fake_runner(b"d" * (2 * SMALL))):
        cut = run_isolated_git_readonly_prefix(str(tmp_path), diff_args, limit=SMALL)
    with git_command_runner(_fake_runner(b"small diff")):
        whole = run_isolated_git_readonly_prefix(str(tmp_path), diff_args, limit=SMALL)

    assert cut.truncated and cut.returncode is None
    assert cut.stdout == b"d" * SMALL
    assert not whole.truncated and whole.returncode == 0
    assert whole.stdout == b"small diff"


def test_the_prefix_read_keeps_the_allowlist(tmp_path: Path) -> None:
    with pytest.raises(GitDeliveryError):
        run_isolated_git_readonly_prefix(str(tmp_path), ["log", "-p"], limit=1024)


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def test_a_snapshot_of_a_repository_with_listings_over_one_megabyte(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Cap Test")
    _git(repo, "config", "user.email", "cap@example.invalid")
    _git(repo, "config", "core.hooksPath", os.devnull)
    tracked = repo / ("t" * 100)
    tracked.mkdir()
    # ~240 bytes per ls-files --stage record: 4600 records is over 1 MB.
    count = 4600
    for index in range(count):
        (tracked / f"{index:05}-{'n' * 120}.txt").write_bytes(b"")
    (repo / "dirty.txt").write_text("before\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "many files")
    untracked = repo / ("u" * 100)
    untracked.mkdir()
    for index in range(count):
        (untracked / f"{index:05}-{'m' * 120}.txt").write_bytes(b"")
    (repo / "dirty.txt").write_text("after\n", encoding="utf-8")

    sizes: dict[str, int] = {}

    def runner(argv, *, cwd, timeout, env):
        result = subprocess.run(
            argv, cwd=cwd, timeout=timeout, env=env, capture_output=True, check=False
        )
        for name in ("ls-files", "status"):
            if name in argv:
                sizes[name] = len(result.stdout)
        return result

    with git_command_runner(runner):
        snapshot = capture_git_snapshot(str(repo))

    assert sizes["ls-files"] > SMALL
    assert sizes["status"] > SMALL
    # Every untracked path plus the modified tracked one is a snapshot entry.
    assert len(snapshot.entries) == count + 1
    dirty_id = hashlib.sha256(b"dirty.txt").hexdigest()
    status, _fingerprint, index_id = snapshot.entries[dirty_id]
    assert status.strip() == "M"
    assert index_id != hashlib.sha256(b"").hexdigest(), "its index entry was kept"
