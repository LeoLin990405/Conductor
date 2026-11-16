"""stream_process: process-group cleanup and stdout discontinuity reporting.

The live-output channel built on ``stream_process`` is only safe if (a) no
descendant of the CLI outlives a cancelled or timed-out run and (b) the caller
is told whenever bytes of the child's stdout were not delivered whole. These
tests drive the real generator against fake process objects so every ordering
is deterministic; one test at the end uses a real shell child.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time

import pytest

from src.llms.native_cli import process as process_mod
from src.llms.native_cli.base import CliInvocation, NativeCliError
from src.llms.native_cli.process import (
    ProcessTracker,
    stream_process,
    track_processes,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeReader:
    """A child pipe: scripted chunks, then EOF or held open by a descendant."""

    def __init__(
        self,
        chunks: list[bytes] | None = None,
        *,
        held_open: bool = False,
        error: Exception | None = None,
    ) -> None:
        self._chunks = list(chunks or [])
        self._held_open = held_open
        self._error = error
        self.cancelled = False

    async def read(self, n: int = -1) -> bytes:
        try:
            # A real pipe read suspends; without this the stdin feeder task
            # would be cancelled before it ever ran.
            await asyncio.sleep(0)
            if self._chunks:
                return self._chunks.pop(0)
            if self._error is not None:
                raise self._error
            if self._held_open:
                await asyncio.Event().wait()
            return b""
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class FakeStdin:
    def __init__(self, *, blocked: bool = False) -> None:
        self._blocked = blocked
        self.written = b""
        self.closed = False
        self.drain_cancelled = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        if self._blocked:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.drain_cancelled = True
                raise

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    pid = 424242

    def __init__(
        self,
        *,
        stdout: FakeReader,
        stderr: FakeReader | None = None,
        stdin: FakeStdin | None = None,
        returncode: int | None = None,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr if stderr is not None else FakeReader()
        self.stdin = stdin if stdin is not None else FakeStdin()
        self.returncode = returncode
        self._exited = asyncio.Event()
        if returncode is not None:
            self._exited.set()
        self.kill_calls = 0
        self.wait_raises = False

    def exit(self, code: int) -> None:
        self.returncode = code
        self._exited.set()

    def kill(self) -> None:
        self.kill_calls += 1
        self.exit(-9)

    async def wait(self) -> int:
        if self.wait_raises:
            raise RuntimeError("wait failed")
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode


class KillSpy:
    """Stands in for ``_kill_tree``; records calls, optionally fails."""

    def __init__(
        self, *, raises: Exception | None = None, exits_process: bool = True
    ) -> None:
        self.calls: list[tuple[FakeProcess, ProcessTracker | None]] = []
        self._raises = raises
        self._exits_process = exits_process

    def __call__(
        self, process: FakeProcess, tracker: ProcessTracker | None = None
    ) -> None:
        self.calls.append((process, tracker))
        if self._raises is not None:
            raise self._raises
        if self._exits_process:
            process.exit(-9)


class SpyTracker(ProcessTracker):
    def __init__(self) -> None:
        super().__init__()
        self.spawned_pids: list[int] = []
        self.exited_pids: list[int] = []

    def spawned(self, pid: int, pgid: int | None = None) -> None:
        self.spawned_pids.append(pid)
        super().spawned(pid, pgid)

    def exited(self, pid: int) -> None:
        self.exited_pids.append(pid)
        super().exited(pid)


# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def install(monkeypatch):
    """Make ``stream_process`` spawn the given fake instead of a real child."""

    def _install(proc: FakeProcess) -> FakeProcess:
        async def fake_exec(*argv, **kwargs):
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(
            process_mod, "prepare_process", lambda argv, cwd, env: (argv, None, cwd)
        )
        return proc

    return _install


@pytest.fixture
def kill_spy(monkeypatch):
    spy = KillSpy()
    monkeypatch.setattr(process_mod, "_kill_tree", spy)
    return spy


@pytest.fixture
def small_cap(monkeypatch):
    monkeypatch.setattr(process_mod, "_MAX_STDOUT", 64)
    return 64


def _invocation(timeout: float = 30.0) -> CliInvocation:
    return CliInvocation(argv=("fake-cli",), stdin="prompt", timeout_seconds=timeout)


async def _settle(iterations: int = 25) -> None:
    """Let every runnable task reach its next real suspension point."""
    for _ in range(iterations):
        await asyncio.sleep(0)


async def _consume(
    tracker: ProcessTracker | None,
    lines: list[str],
    *,
    timeout: float = 30.0,
    **kwargs,
) -> None:
    with track_processes(tracker):
        async for line in stream_process(_invocation(timeout), **kwargs):
            lines.append(line)


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Cleanup on abnormal exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stdout_held", [False, True], ids=["stdout-eof", "stdout-held"])
async def test_cancel_after_leader_exit_kills_the_group_once(
    install, kill_spy, stdout_held
):
    """The leader already exited (returncode set) but a descendant still holds
    stderr (and maybe stdout): cancellation must still kill the group."""
    stderr = FakeReader(held_open=True)
    stdin = FakeStdin(blocked=True)
    proc = install(
        FakeProcess(
            stdout=FakeReader([b"one\n"], held_open=stdout_held),
            stderr=stderr,
            stdin=stdin,
            returncode=0,
        )
    )
    tracker = SpyTracker()
    lines: list[str] = []
    task = asyncio.create_task(_consume(tracker, lines))
    await _settle()
    assert lines == ["one"]
    assert not task.done()
    assert kill_spy.calls == []

    await _cancel(task)
    await _settle()

    assert len(kill_spy.calls) == 1
    assert kill_spy.calls[0][0] is proc
    assert kill_spy.calls[0][1] is tracker
    assert stderr.cancelled
    assert stdin.drain_cancelled
    assert stdin.closed
    assert tracker.spawned_pids == [proc.pid]
    assert tracker.exited_pids == [proc.pid]


@pytest.mark.asyncio
async def test_aclose_after_leader_exit_kills_the_group(install, kill_spy):
    stderr = FakeReader(held_open=True)
    stdin = FakeStdin(blocked=True)
    proc = install(
        FakeProcess(
            stdout=FakeReader([b"one\ntwo\n"], held_open=True),
            stderr=stderr,
            stdin=stdin,
            returncode=0,
        )
    )
    tracker = SpyTracker()
    with track_processes(tracker):
        gen = stream_process(_invocation())
        assert await gen.__anext__() == "one"
        assert kill_spy.calls == []
        await gen.aclose()
    await _settle()

    assert len(kill_spy.calls) == 1
    assert kill_spy.calls[0][0] is proc
    assert stderr.cancelled
    assert stdin.drain_cancelled
    assert tracker.exited_pids == [proc.pid]


@pytest.mark.asyncio
@pytest.mark.parametrize("stdout_held", [False, True], ids=["stdout-eof", "stdout-held"])
async def test_deadline_with_stderr_held_after_leader_exit_kills_and_times_out(
    install, kill_spy, stdout_held
):
    stderr = FakeReader(held_open=True)
    proc = install(
        FakeProcess(
            stdout=FakeReader([b"one\n"], held_open=stdout_held),
            stderr=stderr,
            stdin=FakeStdin(blocked=True),
            returncode=0,
        )
    )
    tracker = SpyTracker()
    lines: list[str] = []
    started = time.monotonic()
    with pytest.raises(NativeCliError, match="timed out"):
        await asyncio.wait_for(_consume(tracker, lines, timeout=0.2), timeout=5)
    elapsed = time.monotonic() - started
    await _settle()

    assert elapsed < 2.0
    assert lines == ["one"]
    assert len(kill_spy.calls) == 1
    assert kill_spy.calls[0][0] is proc
    assert stderr.cancelled
    assert tracker.exited_pids == [proc.pid]


@pytest.mark.asyncio
async def test_unexpected_exception_after_leader_exit_kills_the_group(
    install, kill_spy
):
    stderr = FakeReader(held_open=True)
    proc = install(
        FakeProcess(
            stdout=FakeReader([b"one\n"], error=ConnectionResetError("pipe broke")),
            stderr=stderr,
            returncode=0,
        )
    )
    tracker = SpyTracker()
    lines: list[str] = []
    with pytest.raises(ConnectionResetError):
        await _consume(tracker, lines)
    await _settle()

    assert lines == ["one"]
    assert len(kill_spy.calls) == 1
    assert stderr.cancelled
    assert tracker.exited_pids == [proc.pid]


@pytest.mark.asyncio
async def test_kill_failure_does_not_skip_cleanup_or_mask_cancellation(
    install, monkeypatch
):
    spy = KillSpy(raises=RuntimeError("kill failed"))
    monkeypatch.setattr(process_mod, "_kill_tree", spy)
    stderr = FakeReader(held_open=True)
    stdin = FakeStdin(blocked=True)
    proc = install(
        FakeProcess(
            stdout=FakeReader(held_open=True), stderr=stderr, stdin=stdin
        )  # leader still running
    )
    tracker = SpyTracker()
    task = asyncio.create_task(_consume(tracker, []))
    await _settle()
    assert not task.done()

    await _cancel(task)  # CancelledError, not the RuntimeError from the kill
    await _settle()

    assert len(spy.calls) == 1
    assert proc.kill_calls == 1  # fell back to killing the leader alone
    assert stderr.cancelled
    assert stdin.drain_cancelled
    assert tracker.exited_pids == [proc.pid]


@pytest.mark.asyncio
async def test_wait_failure_does_not_skip_cleanup_or_mask_cancellation(
    install, kill_spy
):
    stderr = FakeReader(held_open=True)
    stdin = FakeStdin(blocked=True)
    proc = install(
        FakeProcess(stdout=FakeReader(held_open=True), stderr=stderr, stdin=stdin)
    )
    proc.wait_raises = True
    tracker = SpyTracker()
    task = asyncio.create_task(_consume(tracker, []))
    await _settle()
    assert not task.done()

    await _cancel(task)
    await _settle()

    assert len(kill_spy.calls) == 1
    assert stderr.cancelled
    assert stdin.drain_cancelled
    assert tracker.exited_pids == [proc.pid]


@pytest.mark.asyncio
async def test_second_cancellation_during_cleanup_leaves_nothing_behind(
    install, monkeypatch
):
    """The group kill and the task cancellations happen before the first
    await of the cleanup, so a second cancel while reaping cannot skip them."""
    spy = KillSpy(exits_process=False)  # reaping then hangs on process.wait()
    monkeypatch.setattr(process_mod, "_kill_tree", spy)
    stderr = FakeReader(held_open=True)
    stdin = FakeStdin(blocked=True)
    proc = install(
        FakeProcess(stdout=FakeReader(held_open=True), stderr=stderr, stdin=stdin)
    )
    tracker = SpyTracker()
    task = asyncio.create_task(_consume(tracker, []))
    await _settle()

    task.cancel()
    await _settle()
    assert not task.done()  # parked in the cleanup's process.wait()
    assert len(spy.calls) == 1
    assert stderr.cancelled
    assert stdin.drain_cancelled

    await _cancel(task)
    assert len(spy.calls) == 1
    assert tracker.exited_pids == [proc.pid]


@pytest.mark.asyncio
async def test_nonzero_exit_still_raises_with_stderr_detail(install, kill_spy):
    """A leader that exited non-zero with stderr fully drained is a completed
    run, not an abnormal exit: unchanged error, no group kill."""
    proc = install(
        FakeProcess(
            stdout=FakeReader([b"one\n"]),
            stderr=FakeReader([b"nope\n"]),
            returncode=7,
        )
    )
    tracker = SpyTracker()
    lines: list[str] = []
    with pytest.raises(NativeCliError, match="exited 7: nope"):
        await _consume(tracker, lines)

    assert lines == ["one"]
    assert kill_spy.calls == []
    assert tracker.exited_pids == [proc.pid]


# ---------------------------------------------------------------------------
# Clean run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_run_yields_lines_in_order_and_never_reports(
    install, kill_spy, monkeypatch
):
    monkeypatch.setattr(
        process_mod, "redact_diagnostic", lambda text, env=None: text
    )
    stdin = FakeStdin()
    proc = install(
        FakeProcess(
            stdout=FakeReader([b"a\nb", b"\nc\n", b"d"]),
            stdin=stdin,
            returncode=0,
        )
    )
    reasons: list[str] = []
    tracker = SpyTracker()
    lines: list[str] = []
    await _consume(tracker, lines, on_discontinuity=reasons.append)

    assert lines == ["a", "b", "c", "d"]  # in order, unterminated tail included
    assert reasons == []
    assert kill_spy.calls == []
    assert stdin.written == b"prompt"
    assert stdin.closed
    assert tracker.spawned_pids == [proc.pid]
    assert tracker.exited_pids == [proc.pid]


# ---------------------------------------------------------------------------
# Discontinuity reporting (cap shrunk to 64 bytes by ``small_cap``)
# ---------------------------------------------------------------------------


async def _run_events(
    install, chunks: list[bytes], *, callback=None, with_callback: bool = True
) -> tuple[list[tuple[str, str]], FakeProcess]:
    """Run to completion, returning interleaved ('line'|'gap', value) events."""
    proc = install(FakeProcess(stdout=FakeReader(chunks), returncode=0))
    events: list[tuple[str, str]] = []

    def default_callback(reason: str) -> None:
        events.append(("gap", reason))

    kwargs = {}
    if with_callback:
        if callback is None:
            kwargs["on_discontinuity"] = default_callback
        else:

            def wrapped(reason: str) -> None:
                events.append(("gap", reason))
                callback(reason)

            kwargs["on_discontinuity"] = wrapped
    with track_processes(None):
        async for line in stream_process(_invocation(), **kwargs):
            events.append(("line", line))
    return events, proc


BIG = b"X" * 100


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        pytest.param(
            [b"a\n" + BIG + b"\nb\n"],
            [("line", "a"), ("gap", "oversized_record"), ("line", "b")],
            id="single-chunk",
        ),
        pytest.param(
            [b"a\n" + b"X" * 40, b"X" * 60 + b"\nb\n"],
            [("line", "a"), ("gap", "oversized_record"), ("line", "b")],
            id="split-but-newline-arrives-with-the-overflow",
        ),
    ],
)
async def test_newline_terminated_oversized_record_is_reported_before_next_record(
    install, small_cap, chunks, expected
):
    events, _ = await _run_events(install, chunks)
    assert events == expected


@pytest.mark.asyncio
async def test_unterminated_oversized_buffer_is_reported_once_per_record(
    install, small_cap
):
    # 400 bytes of one record arrive in four chunks, then its newline, then a
    # fresh record: one report, and the skipped tail is not a record.
    chunks = [b"a\n", BIG, BIG, BIG, BIG + b"tail-of-big\nnext\n"]
    events, _ = await _run_events(install, chunks)
    assert events == [
        ("line", "a"),
        ("gap", "oversized_buffer"),
        ("line", "next"),
    ]


@pytest.mark.asyncio
async def test_skipped_remainder_is_not_mistaken_for_a_fresh_record(
    install, small_cap
):
    events, _ = await _run_events(install, [BIG, b"tail-of-big\nfresh\n"])
    assert events == [("gap", "oversized_buffer"), ("line", "fresh")]


@pytest.mark.asyncio
async def test_record_boundary_exactly_at_a_chunk_edge_after_overflow(
    install, small_cap
):
    # The overflowed record's newline is the first byte of the next chunk.
    events, _ = await _run_events(install, [BIG, b"\nfresh\n"])
    assert events == [("gap", "oversized_buffer"), ("line", "fresh")]


@pytest.mark.asyncio
async def test_two_oversized_records_are_two_reports(install, small_cap):
    chunks = [BIG, b"\nok\n", BIG, b"\n"]
    events, _ = await _run_events(install, chunks)
    assert events == [
        ("gap", "oversized_buffer"),
        ("line", "ok"),
        ("gap", "oversized_buffer"),
    ]


@pytest.mark.asyncio
async def test_eof_during_oversized_skip_is_reported(install, small_cap):
    events, _ = await _run_events(install, [b"a\n", BIG, BIG])
    assert events == [
        ("line", "a"),
        ("gap", "oversized_buffer"),
        ("gap", "oversized_record_at_eof"),
    ]


@pytest.mark.asyncio
async def test_record_at_exactly_the_cap_is_delivered_not_reported(
    install, small_cap
):
    exact = b"y" * small_cap
    events, _ = await _run_events(install, [exact + b"\n" + exact])
    assert events == [("line", exact.decode()), ("line", exact.decode())]


@pytest.mark.asyncio
async def test_invalid_utf8_is_reported_before_its_record_is_delivered(install):
    events, _ = await _run_events(
        install, [b"fine\n", b"pass\xffword: x\n", b"after\n"]
    )
    assert events == [
        ("line", "fine"),
        ("gap", "invalid_utf8"),
        ("line", "pass\ufffdword: x"),
        ("line", "after"),
    ]


@pytest.mark.asyncio
async def test_invalid_utf8_in_an_unterminated_final_record_is_reported(install):
    events, _ = await _run_events(install, [b"fine\n", b"tail\xc3"])
    assert events == [
        ("line", "fine"),
        ("gap", "invalid_utf8"),
        ("line", "tail\ufffd"),
    ]


@pytest.mark.asyncio
async def test_valid_multibyte_text_split_across_chunks_is_not_a_discontinuity(
    install,
):
    snowman = "\u2603 caf\u00e9".encode()
    events, _ = await _run_events(install, [snowman[:2], snowman[2:] + b"\n"])
    assert events == [("line", "\u2603 caf\u00e9")]


@pytest.mark.asyncio
async def test_raising_callback_does_not_break_iteration_or_leak_the_process(
    install, small_cap, kill_spy
):
    attempts: list[str] = []

    def explode(reason: str) -> None:
        attempts.append(reason)
        raise RuntimeError("callback bug")

    events, proc = await _run_events(
        install,
        [b"a\n" + BIG + b"\nb\n"],
        callback=explode,
    )

    assert [e for e in events if e[0] == "line"] == [("line", "a"), ("line", "b")]
    assert attempts  # it was called (and swallowed)
    assert kill_spy.calls == []  # the run completed normally


@pytest.mark.asyncio
async def test_failed_report_is_offered_again_before_the_stream_ends(
    install, small_cap
):
    state = {"calls": 0}

    def flaky(reason: str) -> None:
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("transient")

    events, _ = await _run_events(
        install, [b"a\n" + BIG + b"\nb\n"], callback=flaky
    )

    assert [e for e in events if e[0] == "gap"] == [
        ("gap", "oversized_record"),
        ("gap", "oversized_record"),
    ]
    assert [e for e in events if e[0] == "line"] == [("line", "a"), ("line", "b")]


@pytest.mark.asyncio
async def test_raising_callback_still_cleans_up_on_cancel(install, monkeypatch):
    """A swallowed callback error must not change cancellation cleanup."""
    monkeypatch.setattr(process_mod, "_MAX_STDOUT", 64)
    spy = KillSpy()
    monkeypatch.setattr(process_mod, "_kill_tree", spy)
    stderr = FakeReader(held_open=True)
    proc = install(
        FakeProcess(
            stdout=FakeReader([BIG + b"\nok\n"], held_open=True),
            stderr=stderr,
            returncode=0,
        )
    )

    def explode(reason: str) -> None:
        raise ValueError("boom")

    tracker = SpyTracker()
    lines: list[str] = []
    task = asyncio.create_task(_consume(tracker, lines, on_discontinuity=explode))
    await _settle()
    assert lines == ["ok"]

    await _cancel(task)
    await _settle()
    assert len(spy.calls) == 1
    assert stderr.cancelled
    assert tracker.exited_pids == [proc.pid]


@pytest.mark.asyncio
async def test_no_callback_is_a_noop_for_oversized_output(install, small_cap):
    events, _ = await _run_events(
        install,
        [b"a\n" + BIG + b"\n", BIG, b"\nb\n", BIG],
        with_callback=False,
    )
    assert events == [("line", "a"), ("line", "b")]


# ---------------------------------------------------------------------------
# One real child
# ---------------------------------------------------------------------------


def _running(pid: int) -> bool:
    """Alive and not merely a zombie awaiting an init that does not reap."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_real_descendant_is_killed_when_consumer_closes_after_leader_exit():
    # The shell exits at once; the backgrounded sleep inherits the stdout and
    # stderr pipes, so the stream stays open with the leader already reaped.
    invocation = CliInvocation(
        argv=("/bin/sh", "-c", "sleep 30 & echo $!; exit 0"), timeout_seconds=30.0
    )
    gen = stream_process(invocation)
    pid: int | None = None
    try:
        pid = int(await asyncio.wait_for(gen.__anext__(), timeout=5))
        await asyncio.sleep(0.3)  # let the shell exit
        assert _running(pid)
        await gen.aclose()
        deadline = time.monotonic() + 3
        while _running(pid) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert not _running(pid)
    finally:
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


@pytest.mark.asyncio
async def test_a_redaction_is_reported_before_the_redacted_record(install, monkeypatch):
    monkeypatch.setattr(
        "src.llms.native_cli.process.redact_diagnostic",
        lambda text, env: text.replace("password", "[redacted]"),
    )
    events, _ = await _run_events(
        install, [b"clean line\n", b"the password: x\n", b"tail password\n", b"after\n"]
    )
    assert events == [
        ("line", "clean line"),
        ("gap", "redacted"),
        ("line", "the [redacted]: x"),
        ("gap", "redacted"),
        ("line", "tail [redacted]"),
        ("line", "after"),
    ]


@pytest.mark.asyncio
async def test_an_unchanged_record_is_not_reported_as_redacted(install, monkeypatch):
    monkeypatch.setattr(
        "src.llms.native_cli.process.redact_diagnostic", lambda text, env: text
    )
    events, _ = await _run_events(install, [b"a\n", b"b"])
    assert events == [("line", "a"), ("line", "b")]
