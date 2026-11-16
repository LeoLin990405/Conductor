from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import pytest

from src.conductor_cli.browser import owner


@pytest.mark.asyncio
async def test_signal_during_startup_cancels_driver_and_closes_owner_lock_without_listener(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    driver: FakeDriver
    lock_fd: int | None = None
    signal_handlers: dict[int, object] = {}
    removed_signals: list[int] = []

    class FakeLoop:
        def add_signal_handler(self, signal_number: int, callback) -> None:
            signal_handlers[signal_number] = callback

        def remove_signal_handler(self, signal_number: int) -> None:
            removed_signals.append(signal_number)
            signal_handlers.pop(signal_number, None)

    class FakeDriver:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.closed = False

        async def start(self, _profile: Path) -> None:
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()

        async def close(self) -> None:
            self.closed = True

    driver = FakeDriver()

    def claim_lock(_profile: Path) -> int:
        nonlocal lock_fd
        lock_fd = os.open(profile / "owner.lock", os.O_RDWR | os.O_CREAT, 0o600)
        return lock_fd

    async def no_listener(*_args, **_kwargs):
        raise AssertionError("shutdown during startup must not create a listener")

    actual_wait = asyncio.wait

    async def interrupt_startup(tasks, *, return_when):
        await driver.started.wait()
        callback = signal_handlers[signal.SIGTERM]
        callback()
        return await actual_wait(tasks, return_when=return_when)

    monkeypatch.setattr(owner, "_claim_lock", claim_lock)
    monkeypatch.setattr(owner, "_socket_path", lambda _profile: tmp_path / "fake.sock")
    monkeypatch.setattr(owner, "PlaywrightDriver", lambda: driver)
    monkeypatch.setattr(owner.asyncio, "get_running_loop", lambda: FakeLoop())
    monkeypatch.setattr(owner.asyncio, "wait", interrupt_startup)
    monkeypatch.setattr(owner.asyncio, "start_unix_server", no_listener)
    monkeypatch.setattr(owner.os, "umask", lambda _mask: 0)

    await owner._serve(profile)

    assert driver.started.is_set()
    assert driver.cancelled.is_set()
    assert driver.closed
    assert set(removed_signals) == {signal.SIGTERM, signal.SIGINT}
    assert lock_fd is not None
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(lock_fd)
