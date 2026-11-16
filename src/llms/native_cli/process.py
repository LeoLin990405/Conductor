"""Async subprocess execution for native agent CLIs."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from typing import Any

from src.llms.native_cli.base import (
    CliInvocation,
    NativeCliError,
    NativeCliUnavailable,
    raise_if_auth_failure,
)
from src.llms.native_cli.isolation import prepare_process, redact_diagnostic
from src.llms.native_cli.review_scratch import current_review_scratch

logger = logging.getLogger(__name__)

_MAX_STDOUT = 16 * 1024 * 1024
_MAX_STDERR = 64 * 1024
_READ_CHUNK = 64 * 1024


@dataclass
class SpawnedProcess:
    pid: int
    pgid: int | None
    exited: bool = False


def _descendants(root_pid: int) -> list[tuple[int, int | None]]:
    """Transitive descendants of ``root_pid`` as ``(pid, pgid)`` pairs.

    One ``ps`` snapshot answers the whole walk; a failure degrades to no
    descendants rather than an error, since this runs on a cancellation path.
    The snapshot must be taken while the leader is alive: once it exits,
    children that left the session are reparented away and undiscoverable.
    """
    if os.name != "posix":
        return []
    try:
        output = subprocess.run(
            ["ps", "-e", "-o", "pid=,ppid=,pgid="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    children: dict[int, list[int]] = {}
    pgids: dict[int, int] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
        if pgid > 0:
            pgids[pid] = pgid
    found: list[tuple[int, int | None]] = []
    seen = {root_pid}
    queue = [root_pid]
    while queue:
        for child in children.get(queue.pop(), []):
            if child in seen:
                continue
            seen.add(child)
            # pgid 0 would mean "the caller's own group" in killpg; keep it
            # untracked as a group rather than risk signalling ourselves.
            found.append((child, pgids.get(child)))
            queue.append(child)
    return found


class ProcessTracker:
    """Collects the OS processes an invocation spawned for post-stop proof.

    A native CLI starts in its own session, so its process group id is the
    leader pid. The reaped-leader flag catches the common case; the group check
    catches MCP servers and tools that outlived it. Descendants that left the
    group (``setsid``, ``setpgid``, a job-control shell) escape it, so they are
    captured at kill time and tracked individually — without them a stop could
    be reported verified while a detached child keeps running. A descendant the
    leader's exit reparented before the capture is found by neither, so
    ``gone()`` attests to the leader, its group and what the capture found.
    """

    def __init__(self) -> None:
        self._processes: list[SpawnedProcess] = []

    def spawned(self, pid: int, pgid: int | None = None) -> None:
        # run_process starts the CLI in a new session, so the leader pid is the
        # group id unless a caller supplies a different one.
        default_pgid = pid if os.name == "posix" else None
        self._processes.append(
            SpawnedProcess(pid=pid, pgid=pgid if pgid is not None else default_pgid)
        )

    def capture_descendants(self, pid: int) -> list[tuple[int, int | None]]:
        """Record descendants found while ``pid`` is still alive.

        Returns the newly tracked ``(pid, pgid)`` pairs so the caller can kill
        the groups the leader's own group kill will miss. Only POSIX has
        process groups to walk.
        """
        if os.name != "posix":
            return []
        tracked = {process.pid for process in self._processes}
        captured: list[tuple[int, int | None]] = []
        for descendant_pid, descendant_pgid in _descendants(pid):
            if descendant_pid in tracked:
                continue
            tracked.add(descendant_pid)
            self._processes.append(
                SpawnedProcess(pid=descendant_pid, pgid=descendant_pgid)
            )
            captured.append((descendant_pid, descendant_pgid))
        return captured

    def exited(self, pid: int) -> None:
        for process in self._processes:
            if process.pid == pid:
                process.exited = True

    @staticmethod
    def _leader_alive(process: SpawnedProcess) -> bool:
        if process.exited:
            return False
        try:
            os.kill(process.pid, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True

    @staticmethod
    def _group_alive(process: SpawnedProcess) -> bool:
        if process.pgid is None or os.name != "posix":
            return False
        try:
            os.killpg(process.pgid, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True

    def gone(self) -> bool:
        """Whether every tracked process and its group has exited."""
        return not any(
            self._leader_alive(process) or self._group_alive(process)
            for process in self._processes
        )

    def evidence(self) -> dict[str, object]:
        """Non-secret process identifiers for the server's stop audit row."""
        return {
            "processes": [
                {
                    "pid": process.pid,
                    "pgid": process.pgid,
                    "exited": process.exited,
                    "alive": self._leader_alive(process),
                }
                for process in self._processes
            ]
        }


_PROCESS_TRACKER: ContextVar[ProcessTracker | None] = ContextVar(
    "native_cli_process_tracker", default=None
)


@contextmanager
def track_processes(tracker: ProcessTracker | None) -> Iterator[None]:
    """Attach a tracker to the current task's child invocations."""
    token = _PROCESS_TRACKER.set(tracker)
    try:
        yield
    finally:
        _PROCESS_TRACKER.reset(token)


def _kill_group(pgid: int) -> None:
    """SIGKILL one process group, never the caller's own.

    Raises ``OSError`` (``ProcessLookupError`` and ``PermissionError`` included)
    when there is nothing of ours to signal.
    """
    if pgid <= 1 or pgid == os.getpgrp():
        raise PermissionError(pgid, "refusing to signal this process's own group")
    os.killpg(pgid, signal.SIGKILL)


def kill_child_group(process: asyncio.subprocess.Process) -> None:
    """SIGKILL the group a session-leader child leads; the leader alone if that fails.

    ``start_new_session`` made the leader's pid its group id, and a group id is
    never reissued while the group has a member or the leader is unreaped. Do
    not look the group up with ``getpgid(pid)``: once the leader is reaped that
    pid can name an unrelated process, and its group may be anyone's, this
    process's included. That guarantee ends with the last member: a caller that
    holds the child beyond its life (a parked runtime, a client closed late)
    must check ``returncode`` first, because an empty group's id can by then
    name another group leader's group.
    """
    if os.name == "posix":
        try:
            _kill_group(process.pid)
            return
        except OSError:
            pass
    try:
        process.kill()
    except ProcessLookupError:
        pass


async def terminate_child(process: asyncio.subprocess.Process, *, grace: float) -> None:
    """Stop a child a client object holds: SIGTERM the leader, then SIGKILL its group.

    The leader gets ``grace`` seconds to shut down what it started. The group
    kill follows whether or not it did, because the tools and MCP servers it
    started can outlive it and a bare ``terminate()``/``kill()`` reaches only
    the leader. It sits in ``finally`` so a cancellation landing during the
    grace wait cannot skip it. A leader that was reaped before this call leaves
    nothing to stop: its group id may be free, or another group's by now, and a
    tool it left behind is not worth a kill aimed at a stale id.
    """
    if process.returncode is not None:
        return
    try:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=grace)
        except asyncio.TimeoutError:
            pass
    finally:
        kill_child_group(process)


def _kill_tree(
    process: asyncio.subprocess.Process,
    tracker: ProcessTracker | None = None,
) -> None:
    """Kill the CLI, its session group, and any descendant that left it.

    Native CLIs start MCP servers and tool processes in the same session. A
    bare ``process.kill()`` leaves those children running when a delegation is
    cancelled, so terminate the process group where the platform supports it.
    A child that started its own session escapes even that group, so with a
    tracker attached its descendants are snapshotted first (the leader must be
    alive to find them) and their groups are killed too; the tracker keeps the
    rows so the stop acknowledgement verifies their death, not just the
    leader's.
    """
    if os.name != "posix":
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return
    captured = (
        tracker.capture_descendants(process.pid)
        if tracker is not None
        else _descendants(process.pid)
    )
    leader_pgid = process.pid
    kill_child_group(process)
    # Sessions the leader's group kill cannot reach.
    for descendant_pid, descendant_pgid in captured:
        if descendant_pgid is None or descendant_pgid == leader_pgid:
            continue
        try:
            _kill_group(descendant_pgid)
        except OSError:
            try:
                os.kill(descendant_pid, signal.SIGKILL)
            except OSError:
                pass


# Strong references: a spawn whose caller was cancelled twice has no other owner,
# and a collected task would never run the kill below.
_SPAWNS: set[asyncio.Task] = set()


async def _start(
    tracker: ProcessTracker | None, argv: tuple[str, ...], kwargs: dict[str, Any]
) -> asyncio.subprocess.Process:
    process = await asyncio.create_subprocess_exec(*argv, **kwargs)
    # No await between the Process existing and its pid being recorded.
    if tracker is not None:
        tracker.spawned(process.pid)
    return process


def _record_exit(tracker: ProcessTracker, pid: int, reaped: asyncio.Task) -> None:
    _retrieve_task_result(reaped)
    tracker.exited(pid)


def _kill_orphaned_spawn(
    tracker: ProcessTracker | None,
    spawn: asyncio.Task[asyncio.subprocess.Process],
    *,
    descendants: bool,
) -> None:
    """Take down a child whose caller was cancelled before it received it."""
    if spawn.cancelled() or spawn.exception() is not None:
        return  # nothing started, or asyncio already closed the transport
    process = spawn.result()
    try:
        if descendants:
            _kill_tree(process, tracker)
        else:
            kill_child_group(process)
    except Exception:  # noqa: BLE001
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass
    if tracker is not None:
        reaped = asyncio.ensure_future(process.wait())
        reaped.add_done_callback(partial(_record_exit, tracker, process.pid))


async def _spawn(
    tracker: ProcessTracker | None,
    *argv: str,
    descendants: bool = True,
    **kwargs: Any,
) -> asyncio.subprocess.Process:
    """``create_subprocess_exec`` that a cancellation cannot leave unowned.

    The child is forked at once, but the ``Process`` reaches us only after asyncio
    has wired its pipes, a few loop turns later. A cancellation landing there
    makes asyncio close the transport, which signals the leader alone: the pid
    never reaches the caller or the tracker, descendants already forked keep
    running, and ``gone()`` holds vacuously. So the spawn runs as its own task,
    which ``asyncio.wait`` leaves running when the caller is cancelled, and a
    cancelled caller waits for it to finish and kills the group before the
    cancellation propagates. Further cancellations during that wait are
    absorbed: the spawn is bounded, and ending the caller early would let a stop
    acknowledgement read ``gone()`` before the child is recorded or killed.
    ``descendants`` takes the ``ps`` snapshot that finds what left the group; it
    is a blocking call on the event loop, so only an owner that records the
    result in a tracker asks for it.
    """
    spawn = asyncio.ensure_future(_start(tracker, argv, kwargs))
    _SPAWNS.add(spawn)
    spawn.add_done_callback(_SPAWNS.discard)
    try:
        await asyncio.wait({spawn})
    except asyncio.CancelledError:
        while not spawn.done():
            try:
                await asyncio.wait({spawn})
            except asyncio.CancelledError:
                pass
        # Synchronous, so the group is dead and the pid recorded before the
        # cancellation reaches any frame that could read the tracker.
        _kill_orphaned_spawn(tracker, spawn, descendants=descendants)
        raise
    return spawn.result()


async def spawn_group(
    *argv: str, tracker: ProcessTracker | None = None, **kwargs: Any
) -> asyncio.subprocess.Process:
    """Start a child that leads its own process group, owned from its first instant.

    ``stream_process`` and ``run_process`` hold their child for one call; a
    client object (an ACP or app-server agent, a headless line) holds it for as
    long as it lives, and needs the same guarantee that a cancellation landing
    while the start is in flight kills the group before it propagates. The
    session is fixed here, not left to the caller, because ``kill_child_group``
    aims at the group id that only a session leader has: a child that shares
    this process's group would be un-killable without killing the server.

    When the caller has a :class:`ProcessTracker` in its context, the child is
    registered there and a cancelled start takes the descendant snapshot; a
    daemon's stop verification then covers the app-server's tools as well.
    """
    if tracker is None:
        tracker = _PROCESS_TRACKER.get()
    return await _spawn(
        tracker,
        *argv,
        descendants=tracker is not None,
        start_new_session=True,
        **kwargs,
    )


@dataclass(frozen=True)
class ProcessOutput:
    stdout: str
    stderr: str
    returncode: int


def merged_env(overrides: dict[str, str] | None) -> dict[str, str]:
    """The caller's environment plus explicit adapter overrides.

    The CLI needs the caller's credential/network environment (OAuth files,
    API keys, proxy), while the adapter decides its own home/session variables.
    """
    env = dict(os.environ)
    scratch = current_review_scratch()
    if scratch is not None:
        # A cross-review run's private writable scratch: temp dirs and tool
        # caches go there, never into the read-only repository.
        env.update(scratch.env())
    if overrides:
        env.update({str(k): str(v) for k, v in overrides.items()})
    return env


async def _read_bounded(
    stream: asyncio.StreamReader | None, cap: int, *, keep_tail: bool
) -> bytes:
    """Drain one pipe, keeping at most ``cap`` bytes of it.

    Head mode keeps the first ``cap`` bytes: the diagnostics a CLI prints
    before it fails. Tail mode keeps the last ``cap`` bytes: an event stream
    carries its terminal result at the end, so a head-truncated transcript
    loses the very answer it was supposed to deliver.

    Both modes drain the pipe to EOF, so a chatty child never blocks on a
    full buffer, and neither one buffers the whole output.
    """
    if stream is None:
        return b""
    kept: list[bytes] = []
    kept_bytes = 0
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            break
        if keep_tail:
            kept.append(chunk)
            kept_bytes += len(chunk)
            while len(kept) > 1 and kept_bytes > cap:
                kept_bytes -= len(kept.pop(0))
            if kept_bytes > cap:
                kept[0] = kept[0][-cap:]
                kept_bytes = cap
        elif kept_bytes < cap:
            room = cap - kept_bytes
            kept.append(chunk[:room])
            kept_bytes += min(len(chunk), room)
    return b"".join(kept)


async def _read_bounded_ends(
    stream: asyncio.StreamReader | None, cap: int
) -> bytes:
    """Keep the start and end of diagnostics around a potentially huge bundle line."""
    if stream is None:
        return b""
    marker = b"\n[stderr middle omitted; tail-mid-line=0]\n"
    payload_limit = max(0, cap - len(marker))
    head_limit = payload_limit // 2
    tail_limit = payload_limit - head_limit
    head = bytearray()
    tail = bytearray()
    complete = bytearray()
    total = 0
    overflow = False
    tail_mid_line = False
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if not overflow and len(complete) + len(chunk) <= cap:
            complete.extend(chunk)
            continue
        if not overflow:
            overflow = True
            combined = bytes(complete) + chunk
            head.extend(combined[:head_limit])
            start = max(0, len(combined) - tail_limit)
            tail.extend(combined[start:])
            tail_mid_line = start > 0 and combined[start - 1] != 0x0A
            complete.clear()
            continue
        tail.extend(chunk)
        excess = len(tail) - tail_limit
        if excess > 0:
            tail_mid_line = tail[excess - 1] != 0x0A
            del tail[:excess]
    if not overflow:
        return bytes(complete)
    # A truncated tail may begin in the middle of minified source. The parser
    # drops that fragment before it considers any later complete diagnostic.
    if tail_mid_line:
        marker = marker[:-3] + b"1]\n"
    return bytes(head) + marker + bytes(tail)


async def _feed_stdin(process: asyncio.subprocess.Process, text: str) -> None:
    if process.stdin is None:
        return
    try:
        process.stdin.write(text.encode("utf-8"))
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        # The child exited before it read its input; its output still counts.
        pass
    finally:
        try:
            process.stdin.close()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass


async def _communicate(
    process: asyncio.subprocess.Process,
    stdin_text: str,
    *,
    keep_tail: bool,
    stderr_keep_ends: bool,
) -> tuple[bytes, bytes]:
    """Feed stdin, drain both pipes within their caps and reap the child."""
    # Feed stdin beside the readers: a child may print before it consumes a
    # large prompt, and waiting for the drain first would deadlock on its
    # full stdout pipe.
    readers = [
        asyncio.create_task(_feed_stdin(process, stdin_text)),
        asyncio.create_task(
            _read_bounded(process.stdout, _MAX_STDOUT, keep_tail=keep_tail)
        ),
        asyncio.create_task(
            _read_bounded_ends(process.stderr, _MAX_STDERR)
            if stderr_keep_ends
            else _read_bounded(process.stderr, _MAX_STDERR, keep_tail=False)
        ),
    ]
    try:
        _, stdout_bytes, stderr_bytes = await asyncio.gather(*readers)
    except BaseException:
        for reader in readers:
            reader.cancel()
        raise
    await process.wait()
    return stdout_bytes, stderr_bytes


async def run_process(
    invocation: CliInvocation,
    *,
    check: bool = True,
    keep_tail: bool = False,
    stderr_keep_ends: bool = False,
) -> ProcessOutput:
    """Run one invocation with a hard timeout and kill on cancellation.

    ``check=False`` returns the output of a failed run instead of raising.
    A CLI that exits non-zero can still have produced a transcript that
    explains the failure, so the caller decides what the exit code means.

    ``keep_tail=True`` keeps the last bytes of stdout instead of the first
    ones. A transcript-shaped CLI reports its result at the end of the
    stream, so the head of a long run is the part that can be dropped.
    """
    try:
        argv, child_env, child_cwd = prepare_process(
            invocation.argv, invocation.cwd, invocation.env
        )
        tracker = _PROCESS_TRACKER.get()
        process = await _spawn(
            tracker,
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=child_cwd,
            env=child_env if child_env is not None else merged_env(invocation.env),
            start_new_session=True,
            close_fds=True,
        )
    except FileNotFoundError as exc:
        raise NativeCliUnavailable(
            f"native CLI not found: {invocation.argv[0]}"
        ) from exc
    except PermissionError as exc:
        raise NativeCliUnavailable(
            f"native CLI cannot be executed: {invocation.argv[0]}"
        ) from exc

    try:
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                _communicate(
                    process,
                    invocation.stdin or "",
                    keep_tail=keep_tail,
                    stderr_keep_ends=stderr_keep_ends,
                ),
                timeout=invocation.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            _kill_tree(process, tracker)
            await process.wait()
            raise NativeCliError(
                f"native CLI timed out after {invocation.timeout_seconds:.0f}s"
            ) from exc
        except BaseException:
            _kill_tree(process, tracker)
            await process.wait()
            raise

        stdout = redact_diagnostic(
            stdout_bytes.decode("utf-8", errors="replace"), invocation.env
        )
        stderr = redact_diagnostic(
            stderr_bytes.decode("utf-8", errors="replace"), invocation.env
        )
        if process.returncode and check:
            detail = redact_diagnostic(
                stderr.strip() or stdout.strip() or "no output", invocation.env
            )
            raise_if_auth_failure(invocation.argv[0], detail)
            raise NativeCliError(
                f"native CLI exited {process.returncode}: {detail[:2000]}"
            )
        return ProcessOutput(
            stdout=stdout, stderr=stderr, returncode=process.returncode or 0
        )
    finally:
        if tracker is not None:
            tracker.exited(process.pid)


class _LoggedDiscontinuities:
    """The ``on_discontinuity`` a caller that passed none gets.

    Skipped or altered stdout must never be silent, but most callers have no
    use for the signal. It is logged once per reason per run, with the reason
    and the CLI's basename only: nothing of the skipped bytes.
    """

    def __init__(self, cli: str) -> None:
        self._cli = cli
        self._logged: set[str] = set()

    def __call__(self, reason: str) -> None:
        if reason in self._logged:
            return
        self._logged.add(reason)
        logger.warning(
            "native CLI stdout discontinuity (reason=%s, cli=%s): bytes were "
            "skipped or replaced and no caller is listening",
            reason,
            self._cli,
        )


def _retrieve_task_result(task: asyncio.Task) -> None:
    """Mark a fire-and-forget task's outcome as seen (no "never retrieved" log)."""
    if not task.cancelled():
        task.exception()


class _DiscontinuityReporter:
    """Tells the caller when stdout bytes were skipped instead of delivered.

    A live-output consumer has to hear about a gap before anything that follows
    it, so a callback that raises must neither abort the run (that would leak
    the child) nor silence the report: the failure is logged and the reason is
    offered again ahead of every later record and when stdout ends. The
    reporter can only report, so a consumer that stays unreachable is given up
    on after ``_ATTEMPTS`` tries with an ERROR instead of failing a healthy run;
    stopping its own output first is the consumer's job.
    """

    _ATTEMPTS = 3

    def __init__(self, callback: Callable[[str], None] | None) -> None:
        self._callback = callback
        # reason -> failed attempts so far; insertion-ordered and bounded by the
        # number of distinct reasons.
        self._undelivered: dict[str, int] = {}

    def report(self, reason: str) -> None:
        if self._callback is None:
            return
        # A new occurrence is a new gap, with a fresh budget.
        self._undelivered[reason] = 0
        self.flush()

    def flush(self) -> None:
        """Offer every reason not yet delivered, once each."""
        callback = self._callback
        if callback is None or not self._undelivered:
            return
        pending, self._undelivered = self._undelivered, {}
        for reason, failures in pending.items():
            try:
                callback(reason)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                if failures >= self._ATTEMPTS:
                    logger.error(
                        "native CLI on_discontinuity callback raised %s %d times; "
                        "giving up, the consumer was never told (reason=%s)",
                        type(exc).__name__,
                        failures,
                        reason,
                    )
                    continue
                self._undelivered[reason] = failures
                logger.warning(
                    "native CLI on_discontinuity callback raised %s (reason=%s)",
                    type(exc).__name__,
                    reason,
                )

    def settle(self) -> None:
        """Last chance at the end of stdout: deliver, or spend the budget."""
        while self._undelivered:
            self.flush()


def _redacted_record(
    text: str, env: dict[str, str] | None, reporter: _DiscontinuityReporter
) -> str:
    """Redact credential values out of one record, reporting that it happened.

    A redaction changes what the CLI wrote (it can remove a key word or a value
    fragment), so a live-output caller is told (``redacted``) before the record
    is yielded: continuity judged on the redacted text would be judged on text
    the Worker never produced.
    """
    redacted = redact_diagnostic(text, env)
    if redacted != text:
        reporter.report("redacted")
    return redacted


def _decode_record(raw: bytes, reporter: _DiscontinuityReporter) -> str:
    """Decode one stdout record; replaced bytes are reported as a discontinuity.

    The record is still delivered (the Worker's result path needs it), but text
    in which bytes were replaced is not what the CLI wrote, so a live-output
    caller is told (``invalid_utf8``) before the record is yielded.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        reporter.report("invalid_utf8")
        return raw.decode("utf-8", errors="replace")


async def _abort_stream(
    process: asyncio.subprocess.Process,
    tracker: ProcessTracker | None,
    *tasks: asyncio.Task | None,
) -> None:
    """Tear down a stream that ended abnormally; never raises ``Exception``.

    The group kill and the task cancellations are synchronous and come first,
    so a second cancellation arriving while the leader is reaped cannot skip
    them. The group is killed even when the leader already exited: a
    descendant can still hold the pipes open, and the kill is what stops it.
    Only ``BaseException`` (a second cancel) can escape, and the caller's
    original exception is re-raised by the caller either way.
    """
    try:
        _kill_tree(process, tracker)
    except Exception:  # noqa: BLE001
        # Best effort: at least take the leader down if the tree walk failed.
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass
    finally:
        for task in tasks:
            if task is not None:
                task.cancel()
    try:
        await process.wait()
    except Exception:  # noqa: BLE001
        pass


async def stream_process(
    invocation: CliInvocation,
    *,
    on_discontinuity: Callable[[str], None] | None = None,
) -> AsyncIterator[str]:
    """Run one CLI and yield stdout lines as they arrive.

    Same argv/stdin/timeout rules as :func:`run_process`; the difference is
    liveness for a native turn. stderr is drained concurrently so a verbose
    CLI cannot deadlock on a full pipe.

    Every abnormal exit (cancellation, closing the generator, a timeout, a
    stderr drain that outlives the deadline, any other exception) kills the
    leader's process group and reaps the leader, even when the leader already
    exited: a descendant may still hold the pipes open. A cancellation that lands
    while the process is still being started is held until the start finishes,
    and the group is killed before it propagates. Limits: a descendant that left
    the group (``setsid``, ``setpgid``, a job-control shell's ``set -m``) is
    outside it and is killed only if the process tracker captured it while the
    leader was alive; once the leader has exited it is reparented away and not
    found, and ``ProcessTracker.gone()`` cannot see it; closing the
    event loop cancels the start itself, which reaches only the leader; and a
    group is signalled by id, so one whose members are all gone and whose id was
    since reissued to another group leader of the same user could be hit.

    ``on_discontinuity(reason)`` is called synchronously, before the next
    record is yielded, whenever bytes of the child's stdout are skipped
    instead of delivered whole: ``"oversized_record"`` (a newline-terminated
    record over the transcript cap), ``"oversized_buffer"`` (an unterminated
    record outgrew the cap; reported once, its remainder up to the newline is
    skipped), ``"oversized_record_at_eof"`` (stdout ended inside such a
    record) and ``"invalid_utf8"`` (a record was delivered, but bytes in it
    were replaced while decoding) and ``"redacted"`` (a credential value was
    redacted out of a delivered record). The stream never raises on a
    discontinuity; the caller decides. A raising callback is swallowed and does
    not abort the run: its reason is offered again before each later record and
    after the last one, up to 3 tries in all, then dropped with an ERROR. The
    callback must not raise, and must stop the consumer's output before
    anything that can fail. Nothing is re-offered after a cancellation,
    timeout or closed generator: the consumer sees those itself. Without a
    callback, each reason is logged once per run (content-free) instead of
    passing silently.
    """
    if on_discontinuity is None:
        on_discontinuity = _LoggedDiscontinuities(os.path.basename(invocation.argv[0]))
    try:
        argv, child_env, child_cwd = prepare_process(
            invocation.argv, invocation.cwd, invocation.env
        )
        tracker = _PROCESS_TRACKER.get()
        process = await _spawn(
            tracker,
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=child_cwd,
            env=child_env if child_env is not None else merged_env(invocation.env),
            # A JSONL record can be far larger than asyncio's 64 KiB default
            # StreamReader line limit (a completed message or tool output with
            # a long body). Keep the same total cap as the non-streaming runner
            # so the streaming path cannot fail where run_process succeeded.
            limit=_MAX_STDOUT + _READ_CHUNK,
            start_new_session=True,
            close_fds=True,
        )
    except FileNotFoundError as exc:
        raise NativeCliUnavailable(
            f"native CLI not found: {invocation.argv[0]}"
        ) from exc
    except PermissionError as exc:
        raise NativeCliUnavailable(
            f"native CLI cannot be executed: {invocation.argv[0]}"
        ) from exc

    loop = asyncio.get_running_loop()
    deadline = loop.time() + invocation.timeout_seconds
    reporter = _DiscontinuityReporter(on_discontinuity)
    stderr_task: asyncio.Task[bytes] | None = None
    stdin_task: asyncio.Task[None] | None = None
    try:
        # stderr is diagnostics only and capped at read time: buffering all of
        # it before truncating would let a verbose CLI grow this process
        # without bound.
        stderr_task = asyncio.create_task(
            _read_bounded(process.stderr, _MAX_STDERR, keep_tail=False)
        )
        stderr_task.add_done_callback(_retrieve_task_result)
        # Feed stdin concurrently with reading stdout: a child may write a
        # large transcript before it reads a large prompt, and awaiting the
        # stdin drain first would deadlock on its full stdout pipe
        # (run_process has always fed stdin beside its readers for this
        # reason).
        if process.stdin is not None:
            stdin_task = asyncio.create_task(
                _feed_stdin(process, invocation.stdin or "")
            )
            stdin_task.add_done_callback(_retrieve_task_result)
        # Chunk-based line reader instead of StreamReader.readline(): a valid
        # JSONL record can exceed any fixed line limit, and raising there would
        # turn a run the non-streaming runner accepted into a failure. Records
        # larger than the transcript cap are skipped rather than buffered (the
        # non-streaming runner would only have kept their head), and each skip
        # is reported through ``on_discontinuity``.
        buffer = bytearray()
        # True while the rest of an already-reported oversized record is being
        # discarded up to its newline; that tail is not a record of its own.
        skipping = False
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            chunk = await asyncio.wait_for(
                process.stdout.read(_READ_CHUNK), timeout=remaining
            )
            if not chunk:
                break
            buffer.extend(chunk)
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    break
                if skipping:
                    del buffer[: newline + 1]
                    skipping = False
                    continue
                if newline > _MAX_STDOUT:
                    # A newline-terminated record still over the transcript
                    # cap: skip it like the unterminated case.
                    del buffer[: newline + 1]
                    reporter.report("oversized_record")
                    continue
                raw = bytes(buffer[:newline])
                del buffer[: newline + 1]
                record = _redacted_record(
                    _decode_record(raw, reporter), invocation.env, reporter
                )
                # A gap whose report failed is told again before any record
                # that follows it.
                reporter.flush()
                yield record
            if skipping:
                # Nothing before the next newline is a record: do not keep it.
                buffer.clear()
            elif len(buffer) > _MAX_STDOUT:
                buffer.clear()
                skipping = True
                reporter.report("oversized_buffer")
        if skipping:
            reporter.report("oversized_record_at_eof")
        elif buffer:
            record = _redacted_record(
                _decode_record(bytes(buffer), reporter), invocation.env, reporter
            )
            reporter.flush()
            yield record
        reporter.settle()
        if stdin_task is not None:
            # stdout is closed, so the child is done reading. Waiting (not
            # awaiting the task) keeps an outer cancellation from being
            # mistaken for the feeder's own.
            stdin_task.cancel()
            await asyncio.wait({stdin_task})
        returncode = await asyncio.wait_for(
            process.wait(), timeout=max(0.0, deadline - loop.time())
        )
        # The leader can exit while a descendant keeps stderr open. The run's
        # deadline bounds that drain; on expiry the handler below kills the
        # group before the caller observes a completion.
        stderr_bytes = await asyncio.wait_for(
            stderr_task, timeout=max(0.0, deadline - loop.time())
        )
        stderr = bytes(stderr_bytes)[:_MAX_STDERR].decode("utf-8", errors="replace")
    except asyncio.TimeoutError as exc:
        await _abort_stream(process, tracker, stdin_task, stderr_task)
        raise NativeCliError(
            f"native CLI timed out after {invocation.timeout_seconds:.0f}s"
        ) from exc
    except BaseException:
        await _abort_stream(process, tracker, stdin_task, stderr_task)
        raise
    else:
        # The leader exited and both pipes drained: a finished run, not an
        # abnormal exit, so a failure here reports the exit code only.
        if returncode:
            detail = redact_diagnostic(stderr.strip() or "no output", invocation.env)
            raise_if_auth_failure(invocation.argv[0], detail)
            raise NativeCliError(f"native CLI exited {returncode}: {detail[:2000]}")
    finally:
        if tracker is not None:
            tracker.exited(process.pid)
