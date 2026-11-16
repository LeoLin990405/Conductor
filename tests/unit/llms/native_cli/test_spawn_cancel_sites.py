"""A cancel during spawn must not leave a client's child unowned.

``stream_process`` already holds a cancel that lands while its child is being
started and kills the child's group before the cancel propagates
(``test_process_spawn_cancel.py``). The other places that start a long-lived or
tool-running child have the same window: asyncio hands back the ``Process`` a
few loop turns after the fork, a cancel in between closes the transport, which
signals the leader only, and the object that was about to own the child never
saw its pid. Each site below now starts its child through ``spawn_group`` and
stops it through ``kill_child_group`` / ``terminate_child``.

The spawn is faked at ``asyncio.create_subprocess_exec`` with a stand-in that
stays pending until the test releases it; group signals are recorded at
``os.killpg``. Everything between them is the real code.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from src.llms.native_cli import process as process_mod
from src.llms.native_cli.acp import AcpClient
from src.llms.native_cli.base import NativeCliError
from src.server.services.agent_scanner import CommandError
from src.server.services.deliberation.errors import DeliberationError

FAKE_PID = 424243


class _Stdin:
    def __init__(self) -> None:
        self.closed = False
        # What the child does when its stdin reaches EOF; none by default.
        self.on_close: Callable[[], None] | None = None

    def write(self, data: bytes) -> None:
        del data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True
        if self.on_close is not None:
            self.on_close()


class _FakeProcess:
    """What a spawn hands back: a pid, pipes, and a leader that dies when killed."""

    pid = FAKE_PID

    def __init__(self) -> None:
        self.returncode: int | None = None
        self._exited = asyncio.Event()
        self.stdin = _Stdin()
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.terminate_calls = 0
        self.kill_calls = 0
        self.communicate_hangs = False

    def exit(self, code: int) -> None:
        if self.returncode is None:
            self.returncode = code
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._exited.set()

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.exit(-signal.SIGTERM)

    def kill(self) -> None:
        self.kill_calls += 1
        self.exit(-signal.SIGKILL)

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    async def communicate(self, payload: bytes | None = None) -> tuple[bytes, bytes]:
        del payload
        if self.communicate_hangs:
            await self._exited.wait()  # until it is killed, like the real one
            return b"", b""
        self.exit(0)
        return b"BALLOT: agree\nCOMMENT: ok\n", b""


class _World:
    """The fake spawn plus the record of every group signal."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.gate.set()
        self.error: BaseException | None = None
        self.exit_on_spawn: int | None = None
        self.process: _FakeProcess | None = None
        self.spawn_kwargs: dict[str, Any] = {}
        self.killpg_calls: list[tuple[int, int]] = []

    def hold(self) -> None:
        self.gate.clear()

    def release(self) -> None:
        self.gate.set()

    async def create_subprocess_exec(self, *argv: str, **kwargs: Any):
        self.spawn_kwargs = kwargs
        await self.gate.wait()
        if self.error is not None:
            raise self.error
        self.process = _FakeProcess()
        if self.exit_on_spawn is not None:
            self.process.exit(self.exit_on_spawn)
        return self.process

    def killpg(self, pgid: int, sig: int) -> None:
        self.killpg_calls.append((pgid, sig))
        if (
            self.process is not None
            and pgid == self.process.pid
            and self.process.returncode is None
        ):
            self.process.exit(-int(sig))
            return
        raise ProcessLookupError(pgid)


def _prepare(world: _World, site: "Site") -> None:
    """A one-shot's fake child finishes by itself; a client's stays up."""
    if site.one_shot:
        world.exit_on_spawn = 0


@pytest.fixture
def world(monkeypatch):
    fake = _World()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake.create_subprocess_exec)
    monkeypatch.setattr(process_mod.os, "killpg", fake.killpg)
    monkeypatch.setattr(process_mod, "_descendants", lambda pid: [])
    return fake


async def _settle(iterations: int = 25) -> None:
    for _ in range(iterations):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# The sites. ``start`` runs the site's own entry point up to the moment it owns
# the child (or, for a one-shot, to the end of its run); ``owner_has_child``
# says what "owns it" means there; ``stop`` is its cleanup path, if it has one.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Site:
    name: str
    start: Callable[[_World, Path, pytest.MonkeyPatch], Awaitable[Any]]
    owner_has_child: Callable[[Any, _FakeProcess], bool]
    # None where a missing binary is reported as a result, not an exception.
    missing: tuple[type[BaseException], str] | None
    stop: Callable[[Any], Awaitable[None]] | None = None
    # A one-shot's child runs to its own end; a client's stays until stopped.
    one_shot: bool = False


async def _acp_start(world, tmp_path, monkeypatch):
    client = AcpClient(["fake-acp"])
    await client.start()
    return client


async def _acp_stop(client):
    await client.close()


async def _codex_client_start(world, tmp_path, monkeypatch):
    from src.server.services.native_runtime.codex_app_server import _AppServerClient

    client = _AppServerClient(["fake-codex", "app-server"], cwd=None)
    await client.start()
    return client


async def _codex_client_stop(client):
    await client.close()


def _zcode_runtime(monkeypatch, tmp_path):
    from src.llms.native_cli import zcode
    from src.server.services.native_runtime import zcode_approval

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(zcode, "_LOCK_PATH", tmp_path / "zcode.lock")
    monkeypatch.setattr(
        zcode_approval, "ensure_approval_binary", lambda binary: Path(binary)
    )
    binary = tmp_path / "zcode"
    binary.write_text("fake zcode binary")
    return zcode_approval.ZCodeApprovalRuntime(
        binary=str(binary), cwd=str(tmp_path), broker_thread_id="thread-spawn-cancel"
    )


async def _zcode_start(world, tmp_path, monkeypatch):
    runtime = _zcode_runtime(monkeypatch, tmp_path)
    await runtime._start("hi", None, str(tmp_path / "w"))
    return runtime


async def _zcode_stop(runtime):
    await runtime.cancel()


async def _stepfun_start(world, tmp_path, monkeypatch):
    from src.server.services.native_runtime.stepfun_rpc import StepFunRpcRuntime

    runtime = StepFunRpcRuntime(
        binary="fake-step", cwd=str(tmp_path), broker_thread_id="thread-spawn-cancel"
    )
    await runtime._start("hi", None, None)
    return runtime


async def _stepfun_stop(runtime):
    await runtime.cancel()


async def _deliberation_start(world, tmp_path, monkeypatch):
    from src.server.services.deliberation.cli_adapter import LocalCliParticipantAdapter

    adapter = LocalCliParticipantAdapter(agent_key="codex", binary="fake-codex")
    return await adapter._exec(["fake-codex", "exec", "-"], "prompt")


async def _dispatch_start(world, tmp_path, monkeypatch):
    from src.tools.auto_dispatch.execution import _run_command

    return await _run_command(
        ["fake-launch", "model", "task"], tmp_path, tmp_path / "runs" / "line.log"
    )


async def _feishu_start(world, tmp_path, monkeypatch):
    from src.server.services.autonomy import lark_cli

    monkeypatch.setattr(lark_cli, "_binary", lambda: "fake-lark")
    return await lark_cli._run_lark(["im", "+chats-list"], timeout=45)


async def _scanner_start(world, tmp_path, monkeypatch):
    from src.server.services.agent_scanner import _default_runner

    return await _default_runner(["ps", "-Ao", "pid="], 5.0)


SITES = [
    Site(
        "acp_client",
        _acp_start,
        lambda client, process: client.pid == process.pid,
        (NativeCliError, "not found"),
        _acp_stop,
    ),
    Site(
        "codex_app_server_client",
        _codex_client_start,
        lambda client, process: client._proc is process,
        (FileNotFoundError, ""),
        _codex_client_stop,
    ),
    Site(
        "zcode_approval",
        _zcode_start,
        lambda runtime, process: runtime._process is process,
        (NativeCliError, "not found"),
        _zcode_stop,
    ),
    Site(
        "stepfun_rpc",
        _stepfun_start,
        lambda runtime, process: runtime._process is process,
        (NativeCliError, "not found"),
        _stepfun_stop,
    ),
    Site(
        "deliberation_cli",
        _deliberation_start,
        lambda result, process: "BALLOT" in result,
        (DeliberationError, "cli_unavailable"),
        None,
        one_shot=True,
    ),
    Site(
        "auto_dispatch_line",
        _dispatch_start,
        lambda result, process: result == 0,
        (FileNotFoundError, ""),
        None,
        one_shot=True,
    ),
    Site(
        "feishu_cli",
        _feishu_start,
        lambda result, process: result[0] == 0 and "BALLOT" in result[1],
        None,
        None,
        one_shot=True,
    ),
    Site(
        "agent_scanner",
        _scanner_start,
        lambda result, process: "BALLOT" in result,
        (CommandError, "could not start"),
        None,
        one_shot=True,
    ),
]

EVERY_SITE = pytest.mark.parametrize("site", SITES, ids=[site.name for site in SITES])
CLIENT_SITE = pytest.mark.parametrize(
    "site",
    [site for site in SITES if not site.one_shot],
    ids=[site.name for site in SITES if not site.one_shot],
)


# ---------------------------------------------------------------------------
# Cancel during spawn.
# ---------------------------------------------------------------------------


@EVERY_SITE
@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_cancel_during_spawn_kills_the_group_before_it_propagates(
    world, site, tmp_path, monkeypatch
):
    world.hold()
    seen: dict = {}

    async def run() -> None:
        try:
            await site.start(world, tmp_path, monkeypatch)
        except asyncio.CancelledError:
            # What anything reading the child's state would see by now.
            seen["killed"] = list(world.killpg_calls)
            seen["dead"] = world.process is not None and world.process.returncode
            raise

    task = asyncio.create_task(run())
    await _settle()
    task.cancel()
    await _settle()
    assert not task.done()  # held until the child exists, so it can be killed

    world.release()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen["killed"] == [(FAKE_PID, signal.SIGKILL)]
    assert seen["dead"] == -signal.SIGKILL
    assert world.spawn_kwargs.get("start_new_session") is True


@EVERY_SITE
@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_repeated_cancels_during_spawn_do_not_end_the_caller_early(
    world, site, tmp_path, monkeypatch
):
    """A caller that ended on the second cancel would leave the child unowned."""
    world.hold()
    task = asyncio.create_task(site.start(world, tmp_path, monkeypatch))
    await _settle()
    for _ in range(3):
        task.cancel()
        await _settle()
        assert not task.done()
    assert world.killpg_calls == []

    world.release()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert world.killpg_calls == [(FAKE_PID, signal.SIGKILL)]


@EVERY_SITE
@pytest.mark.asyncio
async def test_a_spawn_that_fails_while_cancelled_has_nothing_to_kill(
    world, site, tmp_path, monkeypatch
):
    world.hold()
    world.error = FileNotFoundError("nope")
    unretrieved: list[dict] = []
    asyncio.get_running_loop().set_exception_handler(
        lambda _loop, context: unretrieved.append(context)
    )
    task = asyncio.create_task(site.start(world, tmp_path, monkeypatch))
    await _settle()
    task.cancel()
    world.release()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _settle()

    assert world.killpg_calls == []
    assert unretrieved == []  # no "Task exception was never retrieved"


# ---------------------------------------------------------------------------
# The normal path is unchanged.
# ---------------------------------------------------------------------------


@EVERY_SITE
@pytest.mark.asyncio
async def test_the_normal_start_hands_the_child_to_its_owner_and_kills_nothing(
    world, site, tmp_path, monkeypatch
):
    _prepare(world, site)
    result = await site.start(world, tmp_path, monkeypatch)
    try:
        assert world.process is not None
        assert site.owner_has_child(result, world.process)
        assert world.spawn_kwargs.get("start_new_session") is True
        assert world.killpg_calls == []
        assert world.process.terminate_calls == world.process.kill_calls == 0
    finally:
        if site.stop is not None:
            await asyncio.wait_for(site.stop(result), timeout=5)


@pytest.mark.asyncio
async def test_a_missing_lark_cli_is_still_reported_as_a_result(
    world, tmp_path, monkeypatch
):
    world.error = FileNotFoundError("nope")

    code, stdout, stderr = await _feishu_start(world, tmp_path, monkeypatch)

    assert (code, stdout) == (127, "")
    assert "nope" in stderr
    assert world.killpg_calls == []


@pytest.mark.parametrize(
    "site",
    [site for site in SITES if site.missing is not None],
    ids=[site.name for site in SITES if site.missing is not None],
)
@pytest.mark.asyncio
async def test_a_missing_binary_still_fails_the_way_it_did(
    world, site, tmp_path, monkeypatch
):
    world.error = FileNotFoundError("nope")
    error_type, message = site.missing

    with pytest.raises(error_type) as caught:
        await site.start(world, tmp_path, monkeypatch)

    assert message in str(caught.value) or message == getattr(
        caught.value, "code", None
    )
    assert world.killpg_calls == []


# ---------------------------------------------------------------------------
# Cleanup paths kill the group, not just the leader.
# ---------------------------------------------------------------------------


@CLIENT_SITE
@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_stopping_a_client_kills_the_group_the_leader_started(
    world, site, tmp_path, monkeypatch
):
    _prepare(world, site)
    result = await site.start(world, tmp_path, monkeypatch)
    assert world.process is not None

    # Bounded: a client that cannot kill its fake would wait for it forever.
    await asyncio.wait_for(site.stop(result), timeout=5)

    assert (FAKE_PID, signal.SIGKILL) in world.killpg_calls
    assert world.process.returncode is not None


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_runtime_that_never_stops_its_agent_gracefully_is_killed_by_group(
    world, tmp_path, monkeypatch
):
    """``_finish`` waits for the CLI to leave by itself, then kills its group."""
    realwait = asyncio.wait_for

    async def short(awaitable, timeout):
        return await realwait(awaitable, timeout=0.05)

    for start, finish_owner in (
        (_zcode_start, lambda runtime: runtime._finish()),
        (_stepfun_start, lambda runtime: runtime._finish()),
    ):
        world.killpg_calls.clear()
        runtime = await start(world, tmp_path, monkeypatch)
        monkeypatch.setattr(asyncio, "wait_for", short)
        try:
            # Bounded: a runtime that cannot kill its fake would wait forever.
            await realwait(finish_owner(runtime), timeout=5)
        finally:
            monkeypatch.setattr(asyncio, "wait_for", realwait)

        assert world.killpg_calls == [(FAKE_PID, signal.SIGKILL)]


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_runtime_cancel_never_looks_the_group_up_through_the_pid(
    world, tmp_path, monkeypatch
):
    """``getpgid(pid)`` of a reaped leader can name anyone's group, ours included."""
    monkeypatch.setattr(
        os,
        "getpgid",
        lambda pid: pytest.fail("looked up the group of a possibly recycled pid"),
    )
    for start in (_zcode_start, _stepfun_start):
        world.killpg_calls.clear()
        runtime = await start(world, tmp_path, monkeypatch)
        await asyncio.wait_for(runtime.cancel(), timeout=5)
        assert world.killpg_calls == [(FAKE_PID, signal.SIGKILL)]


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_runtime_cancel_never_signals_this_servers_own_group(
    world, tmp_path, monkeypatch
):
    runtime = await _stepfun_start(world, tmp_path, monkeypatch)
    assert world.process is not None
    world.process.pid = os.getpgrp()  # a recycled leader pid, as seen from here

    await asyncio.wait_for(runtime.cancel(), timeout=5)

    assert world.killpg_calls == []  # refused; fell back to the leader alone
    assert world.process.kill_calls == 1


# ---------------------------------------------------------------------------
# Codex: the process was only owned once its handshake was done.
# ---------------------------------------------------------------------------


def _codex_runtime():
    from src.server.services.native_runtime.codex_app_server import (
        CodexAppServerRuntime,
    )

    return CodexAppServerRuntime(binary="fake-codex", cwd=None)


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_cancel_during_the_codex_handshake_does_not_leak_the_app_server(
    world,
):
    runtime = _codex_runtime()
    task = asyncio.create_task(runtime._ensure_client())
    await _settle()
    assert world.process is not None  # spawned; the app server never answers
    assert not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runtime._client is None  # never became usable
    assert (FAKE_PID, signal.SIGKILL) in world.killpg_calls
    assert world.process.returncode is not None


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_failed_codex_handshake_does_not_leak_the_app_server(world):
    runtime = _codex_runtime()
    task = asyncio.create_task(runtime._ensure_client())
    await _settle()
    assert world.process is not None
    # The app server is alive and refuses `initialize`.
    world.process.stdout.feed_data(b'{"id":1,"error":{"message":"refused"}}\n')

    with pytest.raises(NativeCliError, match="refused"):
        await asyncio.wait_for(task, timeout=5)

    assert runtime._client is None
    assert (FAKE_PID, signal.SIGKILL) in world.killpg_calls
    assert world.process.returncode is not None


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_cancel_during_the_codex_spawn_leaves_no_half_started_client(world):
    world.hold()
    runtime = _codex_runtime()
    task = asyncio.create_task(runtime._ensure_client())
    await _settle()
    task.cancel()
    world.release()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runtime._client is None
    assert world.killpg_calls == [(FAKE_PID, signal.SIGKILL)]


# ---------------------------------------------------------------------------
# One-shots: the CLI is killed by group on timeout and on cancel.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_deliberation_cli_that_times_out_is_killed_by_group(world, monkeypatch):
    from src.server.services.deliberation.cli_adapter import LocalCliParticipantAdapter
    from src.server.services.deliberation.errors import DeliberationError

    world.exit_on_spawn = None
    original = _FakeProcess.__init__

    def hanging(self):
        original(self)
        self.communicate_hangs = True

    monkeypatch.setattr(_FakeProcess, "__init__", hanging)
    adapter = LocalCliParticipantAdapter(
        agent_key="codex", binary="fake-codex", timeout=0.05
    )

    with pytest.raises(DeliberationError) as caught:
        await adapter._exec(["fake-codex"], "prompt")

    assert caught.value.code == "cli_timeout"
    assert world.killpg_calls == [(FAKE_PID, signal.SIGKILL)]


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_cancelled_deliberation_cli_is_killed_by_group(world, monkeypatch):
    from src.server.services.deliberation.cli_adapter import LocalCliParticipantAdapter

    world.exit_on_spawn = None
    original = _FakeProcess.__init__

    def hanging(self):
        original(self)
        self.communicate_hangs = True

    monkeypatch.setattr(_FakeProcess, "__init__", hanging)
    adapter = LocalCliParticipantAdapter(agent_key="codex", binary="fake-codex")

    task = asyncio.create_task(adapter._exec(["fake-codex"], "prompt"))
    await _settle()
    assert world.process is not None and not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert world.killpg_calls == [(FAKE_PID, signal.SIGKILL)]


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_cancelled_dispatch_line_is_killed_by_group_and_leaves_no_exit_mark(
    world, tmp_path
):
    from src.tools.auto_dispatch.execution import _run_command

    world.exit_on_spawn = None  # the line is still running
    log = tmp_path / "runs" / "line.log"
    task = asyncio.create_task(
        _run_command(["fake-launch", "model", "task"], tmp_path, log)
    )
    await _settle()
    assert world.process is not None and not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Cancel sends TERM, waits, then KILL. The group must still die, and a
    # cancelled line must not look finished.
    assert world.killpg_calls == [
        (FAKE_PID, signal.SIGTERM),
        (FAKE_PID, signal.SIGKILL),
    ]
    assert not log.with_suffix(".exit").exists()


@pytest.mark.asyncio
async def test_a_finished_dispatch_line_records_its_exit_code(world, tmp_path):
    from src.tools.auto_dispatch.execution import _run_command

    world.exit_on_spawn = 3
    log = tmp_path / "runs" / "line.log"

    assert await _run_command(["fake-launch"], tmp_path, log) == 3

    assert log.with_suffix(".exit").read_text() == "3"
    assert world.killpg_calls == []


# ---------------------------------------------------------------------------
# More one-shots: a cancelled call must not leave its child running.
# ---------------------------------------------------------------------------


def _hang_communicate(monkeypatch) -> None:
    original = _FakeProcess.__init__

    def hanging(self):
        original(self)
        self.communicate_hangs = True

    monkeypatch.setattr(_FakeProcess, "__init__", hanging)


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_cancelled_lark_call_kills_the_cli_by_group(
    world, tmp_path, monkeypatch
):
    """The 45 s timer dies with the cancelled call; nothing else would end it."""
    _hang_communicate(monkeypatch)
    task = asyncio.create_task(_feishu_start(world, tmp_path, monkeypatch))
    await _settle()
    assert world.process is not None and not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert world.killpg_calls == [(FAKE_PID, signal.SIGKILL)]


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_lark_call_that_times_out_is_killed_by_group(
    world, tmp_path, monkeypatch
):
    """``lark-cli`` is a node shim: a leader-only kill leaves what it started."""
    from src.server.services.autonomy import lark_cli

    _hang_communicate(monkeypatch)
    monkeypatch.setattr(lark_cli, "_binary", lambda: "fake-lark")

    code, _, stderr = await lark_cli._run_lark(["im"], timeout=0.05)

    assert code == 124 and "超时" in stderr
    assert world.killpg_calls == [(FAKE_PID, signal.SIGKILL)]


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_cancelled_scanner_command_is_killed_by_group(
    world, tmp_path, monkeypatch
):
    _hang_communicate(monkeypatch)
    task = asyncio.create_task(_scanner_start(world, tmp_path, monkeypatch))
    await _settle()
    assert world.process is not None and not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert world.killpg_calls == [(FAKE_PID, signal.SIGKILL)]


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_scanner_command_that_times_out_is_killed_by_group(
    world, monkeypatch
):
    from src.server.services.agent_scanner import _default_runner

    _hang_communicate(monkeypatch)

    with pytest.raises(CommandError, match="timed out"):
        await _default_runner(["lsof"], 0.05)

    assert world.killpg_calls == [(FAKE_PID, signal.SIGKILL)]


@pytest.mark.asyncio
async def test_a_prompt_that_cannot_be_encoded_is_refused_before_the_cli_starts(
    world,
):
    """Encoding it after the start left a tool-running CLI with no one to kill it."""
    from src.server.services.deliberation.cli_adapter import LocalCliParticipantAdapter

    adapter = LocalCliParticipantAdapter(agent_key="codex", binary="fake-codex")

    with pytest.raises(UnicodeEncodeError):
        await adapter._exec(["fake-codex", "exec", "-"], "\ud800")

    assert world.process is None  # nothing was started


# ---------------------------------------------------------------------------
# A stop that lands while the child is still being started, from another task.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_closing_an_acp_client_while_it_starts_leaves_no_child(world):
    world.hold()
    client = AcpClient(["fake-acp"])
    task = asyncio.create_task(client.start())
    await _settle()

    await client.close()  # the child is not handed back yet: nothing to stop
    world.release()
    with pytest.raises(NativeCliError, match="closed"):
        await asyncio.wait_for(task, timeout=5)

    assert world.killpg_calls == [(FAKE_PID, signal.SIGKILL)]
    assert world.process is not None and world.process.returncode is not None
    assert client.pid is None  # it never took the child


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_closing_a_codex_client_while_it_starts_leaves_no_child(world):
    from src.server.services.native_runtime.codex_app_server import _AppServerClient

    world.hold()
    client = _AppServerClient(["fake-codex", "app-server"], cwd=None)
    task = asyncio.create_task(client.start())
    await _settle()

    await client.close()
    world.release()
    with pytest.raises(NativeCliError, match="closed"):
        await asyncio.wait_for(task, timeout=5)

    assert world.killpg_calls == [(FAKE_PID, signal.SIGKILL)]
    assert client._proc is None


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_cancelling_the_codex_runtime_during_its_handshake_kills_the_app_server(
    world,
):
    """``cancel`` is the stop path a worker's stop request takes, from its own task."""
    runtime = _codex_runtime()
    task = asyncio.create_task(runtime._ensure_client())
    await _settle()
    assert world.process is not None and not task.done()  # answering `initialize`

    await runtime.cancel()

    assert (FAKE_PID, signal.SIGKILL) in world.killpg_calls
    assert world.process.returncode is not None
    task.cancel()  # the handshake can never finish; do not wait out its timeout
    with pytest.raises((asyncio.CancelledError, NativeCliError)):
        await asyncio.wait_for(task, timeout=5)
    assert runtime._client is None


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_cancelling_the_acp_runtime_while_its_agent_starts_kills_it(
    world, tmp_path
):
    world.hold()
    runtime = _acp_runtime(tmp_path)
    task = asyncio.create_task(_drain(runtime.stream_turn("hi")))
    await _settle()

    await runtime.cancel()
    world.release()
    # A clean refusal, not an AttributeError on the client `cancel` dropped.
    with pytest.raises(NativeCliError):
        await asyncio.wait_for(task, timeout=5)

    assert world.killpg_calls == [(FAKE_PID, signal.SIGKILL)]
    assert world.process is not None and world.process.returncode is not None


# ---------------------------------------------------------------------------
# A runtime's own cleanup: a task cancel or an abandoned stream reaches the group
# kill in every lane, not only through an explicit ``cancel()``.
# ---------------------------------------------------------------------------


async def _drain(stream) -> None:
    async for _ in stream:
        pass


def _acp_runtime(tmp_path):
    from src.server.services.native_runtime.acp_runtime import AcpNativeRuntime

    return AcpNativeRuntime(
        provider="kimi",
        argv=["fake-acp"],
        cwd=str(tmp_path),
        broker_thread_id="thread-lane-cancel",
    )


def _stepfun_runtime(tmp_path):
    from src.server.services.native_runtime.stepfun_rpc import StepFunRpcRuntime

    return StepFunRpcRuntime(
        binary="fake-step", cwd=str(tmp_path), broker_thread_id="thread-lane-cancel"
    )


@pytest.mark.parametrize("lane", ["acp", "stepfun", "zcode"])
@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_cancelled_turn_kills_its_agents_group_in_every_lane(
    world, lane, tmp_path, monkeypatch
):
    runtime = {
        "acp": lambda: _acp_runtime(tmp_path),
        "stepfun": lambda: _stepfun_runtime(tmp_path),
        "zcode": lambda: _zcode_runtime(monkeypatch, tmp_path),
    }[lane]()
    task = asyncio.create_task(_drain(runtime.stream_turn("hi")))
    await _settle()
    assert world.process is not None and not task.done()  # the agent is mid-turn

    # ZCode and StepFun give the agent a few seconds to leave by itself first.
    realwait = asyncio.wait_for

    async def short(awaitable, timeout):
        return await realwait(awaitable, timeout=0.05)

    monkeypatch.setattr(asyncio, "wait_for", short)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await realwait(task, timeout=5)
    finally:
        monkeypatch.setattr(asyncio, "wait_for", realwait)

    assert (FAKE_PID, signal.SIGKILL) in world.killpg_calls
    assert world.process.returncode is not None


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_an_abandoned_stepfun_stream_kills_its_agents_group(
    world, tmp_path, monkeypatch
):
    """The graph closes the stream when it stops early, e.g. on an error event."""
    runtime = _stepfun_runtime(tmp_path)
    stream = runtime.stream_turn("hi")
    first = asyncio.create_task(stream.__anext__())
    await _settle()
    assert world.process is not None
    delta = (
        b'{"type":"message_update","assistantMessageEvent":'
        b'{"type":"text_delta","delta":"hello"}}\n'
    )
    world.process.stdout.feed_data(delta)
    assert (await asyncio.wait_for(first, timeout=5)).kind == "text_delta"

    realwait = asyncio.wait_for

    async def short(awaitable, timeout):
        return await realwait(awaitable, timeout=0.05)

    monkeypatch.setattr(asyncio, "wait_for", short)
    try:
        await realwait(stream.aclose(), timeout=5)
    finally:
        monkeypatch.setattr(asyncio, "wait_for", realwait)

    assert world.killpg_calls == [(FAKE_PID, signal.SIGKILL)]
    assert runtime._process is None


_AGENT_END = b'{"type":"agent_end"}\n'
_CONFIRM = (
    b'{"type":"extension_ui_request","method":"confirm","id":"c1","title":"Write"}\n'
)
_APPROVE = {"interrupt-id": {"decisions": [{"type": "approve"}]}}


async def _until_stepfun_completes(world, runtime):
    """Run a StepFun turn whose agent reports ``agent_end``; return the stream.

    The stream has yielded ``turn_completed`` and has not yet been asked for
    the next event, which is where the graph's loop ends it.
    """
    stream = runtime.stream_turn("hi")
    first = asyncio.create_task(stream.__anext__())
    await _settle()
    assert world.process is not None
    world.process.stdout.feed_data(_AGENT_END)
    assert (await asyncio.wait_for(first, timeout=5)).kind == "turn_completed"
    return stream


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_completed_stepfun_turn_lets_the_agent_leave_when_its_stdin_closes(
    world, tmp_path
):
    """``step --mode rpc`` idles on stdin after a turn; EOF is how it is told to go."""
    runtime = _stepfun_runtime(tmp_path)
    stream = await _until_stepfun_completes(world, runtime)
    process = world.process
    assert process is not None
    process.stdin.on_close = lambda: process.exit(0)

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(stream.__anext__(), timeout=5)

    assert process.stdin.closed
    assert process.returncode == 0  # it left by itself
    assert world.killpg_calls == []
    assert runtime._process is None
    assert runtime._stderr_task is None


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_completed_stepfun_turn_kills_an_agent_that_does_not_leave(
    world, tmp_path, monkeypatch
):
    runtime = _stepfun_runtime(tmp_path)
    stream = await _until_stepfun_completes(world, runtime)

    realwait = asyncio.wait_for

    async def short(awaitable, timeout):
        return await realwait(awaitable, timeout=0.05)

    monkeypatch.setattr(asyncio, "wait_for", short)
    try:
        with pytest.raises(StopAsyncIteration):
            await realwait(stream.__anext__(), timeout=5)
    finally:
        monkeypatch.setattr(asyncio, "wait_for", realwait)

    assert world.killpg_calls == [(FAKE_PID, signal.SIGKILL)]
    assert world.process is not None and world.process.returncode is not None
    assert runtime._process is None


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_stepfun_turn_that_completes_after_an_approval_ends_its_agent(
    world, tmp_path
):
    """The agent that parked for an approval is the one a resume has to end."""
    from src.server.services.native_runtime.approvals import get_approval_broker

    runtime = _stepfun_runtime(tmp_path)
    stream = runtime.stream_turn("hi")
    first = asyncio.create_task(stream.__anext__())
    await _settle()
    process = world.process
    assert process is not None
    process.stdout.feed_data(_CONFIRM)
    assert (await asyncio.wait_for(first, timeout=5)).kind == "approval_requested"
    await stream.aclose()  # the graph closes a paused stream
    try:
        assert runtime._process is process and process.returncode is None  # parked
        assert not process.stdin.closed

        process.stdin.on_close = lambda: process.exit(0)
        process.stdout.feed_data(_AGENT_END)
        kinds = [
            event.kind
            async for event in runtime.resume_turn(_APPROVE)
        ]
    finally:
        get_approval_broker().clear("thread-lane-cancel")

    assert kinds == ["turn_completed"]
    assert process.stdin.closed
    assert process.returncode == 0
    assert world.killpg_calls == []
    assert runtime._process is None


@pytest.mark.parametrize("start", [_zcode_start, _stepfun_start])
@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_cancel_during_the_grace_wait_of_finish_still_kills_the_group(
    world, start, tmp_path, monkeypatch
):
    runtime = await start(world, tmp_path, monkeypatch)
    task = asyncio.create_task(runtime._finish())
    await _settle()
    assert not task.done() and world.killpg_calls == []  # inside the grace wait

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert world.killpg_calls == [(FAKE_PID, signal.SIGKILL)]
    assert runtime._process is None  # nothing is left holding a dead handle


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_cancelled_zcode_finish_still_gives_the_lock_and_the_folder_back(
    world, tmp_path, monkeypatch
):
    import fcntl

    from src.llms.native_cli import zcode

    runtime = await _zcode_start(world, tmp_path, monkeypatch)
    task = asyncio.create_task(runtime._finish())
    await _settle()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert not runtime._permission_dir.exists()
    descriptor = os.open(zcode._LOCK_PATH, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)  # not held any more
    finally:
        os.close(descriptor)


# ---------------------------------------------------------------------------
# A handle held past its child's life must not signal a group id that is free.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("start", [_zcode_start, _stepfun_start])
@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_cancelling_a_runtime_whose_agent_already_exited_signals_no_group(
    world, start, tmp_path, monkeypatch
):
    """A parked runtime can be held for hours; its agent's pid is free by then."""
    runtime = await start(world, tmp_path, monkeypatch)
    assert world.process is not None
    world.process.exit(1)  # died while parked; asyncio reaped it

    await asyncio.wait_for(runtime.cancel(), timeout=5)

    assert world.killpg_calls == []


# ---------------------------------------------------------------------------
# Real processes: a stand-in cannot prove a descendant dies.
# ---------------------------------------------------------------------------


def _marker_processes(marker: str) -> list[int]:
    output = subprocess.run(
        ["ps", "-e", "-o", "pid=,stat=,command="],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    found = []
    for line in output.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and marker in parts[2] and not parts[1].startswith("Z"):
            found.append(int(parts[0]))
    return found


async def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def _reap(marker: str) -> None:
    for pid in _marker_processes(marker):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _marker(base: int) -> str:
    return f"sleep {base + os.getpid() % 1000}.{time.monotonic_ns() % 997:03d}"


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.requires_ps
@pytest.mark.asyncio
async def test_closing_a_real_acp_client_kills_what_its_agent_started():
    marker = _marker(4000)
    client = AcpClient(["/bin/sh", "-c", f"{marker} & wait"])
    await client.start()
    try:
        assert await _wait_until(lambda: bool(_marker_processes(marker)))

        await client.close()

        assert await _wait_until(lambda: _marker_processes(marker) == [])
    finally:
        _reap(marker)


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.requires_ps
@pytest.mark.asyncio
async def test_closing_a_real_codex_client_kills_what_its_app_server_started():
    from src.server.services.native_runtime.codex_app_server import _AppServerClient

    marker = _marker(3000)
    client = _AppServerClient(["/bin/sh", "-c", f"{marker} & wait"], cwd=None)
    await client.start()
    try:
        assert client._proc is not None
        # Its own session: the group a close kills is not this server's.
        assert os.getpgid(client._proc.pid) == client._proc.pid != os.getpgrp()
        assert await _wait_until(lambda: bool(_marker_processes(marker)))

        await client.close()

        assert await _wait_until(lambda: _marker_processes(marker) == [])
    finally:
        _reap(marker)


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.requires_ps
@pytest.mark.asyncio
async def test_a_real_acp_start_cancelled_under_a_busy_loop_leaves_no_descendant():
    marker = _marker(2000)
    client = AcpClient(["/bin/sh", "-c", f"{marker} & wait"])
    task = asyncio.create_task(client.start())
    try:
        await asyncio.sleep(0)
        time.sleep(0.05)  # the shell forks its descendant while the loop is busy
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await _wait_until(lambda: _marker_processes(marker) == [])
    finally:
        _reap(marker)


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.requires_ps
@pytest.mark.asyncio
async def test_a_real_acp_start_closed_from_another_task_leaves_no_descendant():
    marker = _marker(1000)
    client = AcpClient(["/bin/sh", "-c", f"{marker} & wait"])
    task = asyncio.create_task(client.start())
    try:
        await asyncio.sleep(0)  # the child is forked; its Process is not handed back
        await client.close()

        with pytest.raises(NativeCliError, match="closed"):
            await asyncio.wait_for(task, timeout=5)
        assert await _wait_until(lambda: _marker_processes(marker) == [])
    finally:
        _reap(marker)


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.requires_ps
@pytest.mark.asyncio
async def test_a_real_acp_turn_cancelled_while_it_waits_leaves_no_descendant(tmp_path):
    from src.server.services.native_runtime.acp_runtime import AcpNativeRuntime

    marker = _marker(7000)
    runtime = AcpNativeRuntime(
        provider="kimi",
        argv=["/bin/sh", "-c", f"{marker} & wait"],
        cwd=str(tmp_path),
        broker_thread_id="thread-real-acp-cancel",
    )
    task = asyncio.create_task(_drain(runtime.stream_turn("hi")))  # waits on `initialize`
    try:
        assert await _wait_until(lambda: bool(_marker_processes(marker)))

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)

        assert await _wait_until(lambda: _marker_processes(marker) == [])
        assert runtime._client is None
    finally:
        _reap(marker)


def _shell_agent(tmp_path: Path, name: str, body: str) -> Path:
    """An executable stand-in for an agent CLI: the runtime runs it as the binary."""
    script = tmp_path / name
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return script


def _real_stepfun_runtime(tmp_path: Path, script: Path):
    from src.server.services.native_runtime.stepfun_rpc import StepFunRpcRuntime

    return StepFunRpcRuntime(
        binary=str(script), cwd=str(tmp_path), broker_thread_id="thread-real-stepfun"
    )


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.requires_ps
@pytest.mark.asyncio
async def test_a_real_stepfun_turn_that_completes_ends_an_agent_that_lingers(
    tmp_path, monkeypatch
):
    """The RPC server does not exit by itself once its turn is over."""
    marker = _marker(5000)
    script = _shell_agent(
        tmp_path,
        "step-lingers",
        f"{marker} &\nprintf '%s\\n' '{_AGENT_END.decode().strip()}'\nwait\n",
    )
    runtime = _real_stepfun_runtime(tmp_path, script)
    realwait = asyncio.wait_for

    async def short(awaitable, timeout):
        return await realwait(awaitable, timeout=0.2)

    monkeypatch.setattr(asyncio, "wait_for", short)  # the 5 s grace, shortened
    try:
        events = []
        async for event in runtime.stream_turn("hi"):
            events.append(event.kind)
        monkeypatch.setattr(asyncio, "wait_for", realwait)

        assert events == ["turn_completed"]
        assert runtime._process is None
        assert await _wait_until(lambda: _marker_processes(marker) == [])
    finally:
        monkeypatch.setattr(asyncio, "wait_for", realwait)
        await runtime.cancel()
        _reap(marker)


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_real_stepfun_turn_that_completes_lets_an_agent_exit_on_eof(tmp_path):
    """Like ``step --mode rpc``, which exits at once when its stdin closes."""
    done = tmp_path / "left-by-itself"
    script = _shell_agent(
        tmp_path,
        "step-exits-on-eof",
        f"printf '%s\\n' '{_AGENT_END.decode().strip()}'\n"
        f"while read -r line; do :; done\n"
        f"printf left > '{done}'\n",
    )
    runtime = _real_stepfun_runtime(tmp_path, script)
    started = time.monotonic()
    try:
        events = [event.kind async for event in runtime.stream_turn("hi")]

        # Well inside the 5 s grace: it went when told to, it was not killed.
        assert time.monotonic() - started < 3
        assert events == ["turn_completed"]
        assert done.read_text() == "left"
        assert runtime._process is None
    finally:
        await runtime.cancel()


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.requires_ps
@pytest.mark.asyncio
async def test_a_real_acp_turn_that_completes_closes_its_agent_and_what_it_started(
    tmp_path,
):
    """A normal turn ends in ``close()``: the agent idles after its answer."""
    from src.server.services.native_runtime.acp_runtime import AcpNativeRuntime

    marker = _marker(6000)
    script = _shell_agent(
        tmp_path,
        "acp-agent",
        f"{marker} &\n"
        "read l; printf '%s\\n' "
        "'{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"protocolVersion\":1}}'\n"
        "read l; printf '%s\\n' "
        "'{\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{\"sessionId\":\"s1\"}}'\n"
        "read l\n"
        "printf '%s\\n' '{\"jsonrpc\":\"2.0\",\"method\":\"session/update\",\"params\":"
        "{\"update\":{\"sessionUpdate\":\"agent_message_chunk\","
        "\"content\":{\"type\":\"text\",\"text\":\"hi\"}}}}'\n"
        # The answer follows its text a moment later, as a real agent's does.
        "sleep 0.2\n"
        "printf '%s\\n' "
        "'{\"jsonrpc\":\"2.0\",\"id\":3,\"result\":{\"stopReason\":\"end_turn\"}}'\n"
        "wait\n",
    )
    runtime = AcpNativeRuntime(
        provider="kimi",
        argv=[str(script)],
        cwd=str(tmp_path),
        broker_thread_id="thread-real-acp-complete",
    )
    try:
        events = [event.kind async for event in runtime.stream_turn("hi")]

        assert events == ["text_delta", "turn_completed"]
        assert runtime._client is None
        assert await _wait_until(lambda: _marker_processes(marker) == [])
    finally:
        await runtime.cancel()
        _reap(marker)


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.requires_ps
@pytest.mark.asyncio
async def test_a_real_codex_turn_that_completes_closes_its_app_server_and_what_it_started(
    tmp_path,
):
    from src.server.services.native_runtime.codex_app_server import (
        CodexAppServerRuntime,
    )

    marker = _marker(8000)
    script = _shell_agent(
        tmp_path,
        "codex-app-server",
        f"{marker} &\n"
        "read l; printf '%s\\n' '{\"id\":1,\"result\":{}}'\n"
        "read l\n"
        "read l; printf '%s\\n' '{\"id\":2,\"result\":{\"thread\":{\"id\":\"t1\"}}}'\n"
        "read l; printf '%s\\n' '{\"id\":3,\"result\":{}}'\n"
        "printf '%s\\n' '{\"method\":\"item/agentMessage/delta\","
        "\"params\":{\"itemId\":\"i1\",\"delta\":\"hi\"}}'\n"
        "printf '%s\\n' '{\"method\":\"turn/completed\","
        "\"params\":{\"turn\":{\"status\":\"completed\"}}}'\n"
        "wait\n",
    )
    runtime = CodexAppServerRuntime(
        binary=str(script), cwd=str(tmp_path), broker_thread_id="thread-real-codex"
    )
    try:
        events = [event.kind async for event in runtime.stream_turn("hi")]

        assert events == ["text_delta", "turn_completed"]
        assert runtime._client is None
        assert await _wait_until(lambda: _marker_processes(marker) == [])
    finally:
        await runtime.cancel()
        _reap(marker)


def _recording_spawn(monkeypatch, module) -> list[Any]:
    """Keep the real ``Process`` a runtime started, to read how it ended."""
    started: list[Any] = []
    real = module.spawn_group

    async def spawn(*argv: str, **kwargs: Any):
        process = await real(*argv, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(module, "spawn_group", spawn)
    return started


def _zcode_lock_is_free() -> bool:
    import fcntl

    from src.llms.native_cli import zcode

    descriptor = os.open(zcode._LOCK_PATH, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("agent_leaves", [True, False])
@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_completed_zcode_turn_gives_the_agent_its_grace_then_kills_the_group(
    world, agent_leaves, tmp_path, monkeypatch
):
    """Its turn ended when the output closed; the CLI may or may not have left."""
    runtime = _zcode_runtime(monkeypatch, tmp_path)
    task = asyncio.create_task(_drain(runtime.stream_turn("hi")))
    await _settle()
    process = world.process
    assert process is not None and not task.done()

    realwait = asyncio.wait_for

    async def short(awaitable, timeout):
        return await realwait(awaitable, timeout=0.05)

    monkeypatch.setattr(asyncio, "wait_for", short)
    try:
        if agent_leaves:
            process.exit(0)
        else:
            process.stdout.feed_eof()  # the output is closed, the CLI still runs
        await realwait(task, timeout=5)
    finally:
        monkeypatch.setattr(asyncio, "wait_for", realwait)

    assert world.killpg_calls == ([] if agent_leaves else [(FAKE_PID, signal.SIGKILL)])
    assert process.returncode is not None
    assert runtime._process is None
    assert not runtime._permission_dir.exists()
    assert _zcode_lock_is_free()


@pytest.mark.skipif(os.name != "posix", reason="needs process groups")
@pytest.mark.asyncio
async def test_a_real_zcode_turn_that_completes_waits_for_the_cli_and_does_not_kill_it(
    tmp_path, monkeypatch
):
    from src.server.services.native_runtime import zcode_approval

    started = _recording_spawn(monkeypatch, zcode_approval)
    runtime = _zcode_runtime(monkeypatch, tmp_path)
    # The runtime was built over a placeholder file; make it a CLI that prints
    # a line, outlives its output for a moment, and exits on its own.
    script = Path(runtime._source_binary)
    script.write_text("#!/bin/sh\nprintf '%s\\n' '{}'\nsleep 0.3\n")
    script.chmod(0o755)

    events = [event async for event in runtime.stream_turn("hi")]

    assert events == []
    assert len(started) == 1 and started[0].returncode == 0  # left, not killed
    assert runtime._process is None
    assert not runtime._permission_dir.exists()
    assert _zcode_lock_is_free()
