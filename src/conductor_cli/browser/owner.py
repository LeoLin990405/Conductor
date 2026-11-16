"""Per-agent owner process for the managed headless Chromium instance."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import signal
import socket
import stat
from pathlib import Path
from typing import Any

from ..client import CLIError
from .playwright_driver import PlaywrightDriver
from .service import BrowserCommandError, BrowserService
from .transport import MAX_MESSAGE_BYTES, _socket_path, profile_for_agent


def _claim_lock(profile: Path) -> int:
    path = profile / "owner.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        os.close(descriptor)
        raise CLIError("Browser owner lock is not a private regular file")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        raise CLIError("A browser owner already holds this profile") from None
    return descriptor


def _same_user(writer: asyncio.StreamWriter) -> bool:
    client = writer.get_extra_info("socket")
    if client is None:
        return False
    if hasattr(client, "getpeereid"):
        uid, _gid = client.getpeereid()
        return uid == os.getuid()
    if hasattr(socket, "SO_PEERCRED"):
        credentials = client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _pid, uid, _gid = __import__("struct").unpack("3i", credentials)
        return uid == os.getuid()
    # On platforms without a peer-credential API, the owner-only profile tree
    # and mode-0600 socket are the access boundary.
    return True


async def _serve(profile: Path) -> None:
    os.umask(0o077)
    lock_descriptor = _claim_lock(profile)
    driver = PlaywrightDriver()
    service = BrowserService(driver, profile)
    server: asyncio.AbstractServer | None = None
    stop = asyncio.Event()
    request_lock = asyncio.Lock()
    socket_path = _socket_path(profile)
    try:
        if socket_path.exists():
            metadata = socket_path.lstat()
            if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise CLIError("Refusing an unexpected browser control path")
            socket_path.unlink()
        loop = asyncio.get_running_loop()
        for signal_name in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signal_name, stop.set)
            except (NotImplementedError, RuntimeError):
                pass

        startup = asyncio.create_task(driver.start(profile))
        stopping = asyncio.create_task(stop.wait())
        done, _pending = await asyncio.wait(
            {startup, stopping}, return_when=asyncio.FIRST_COMPLETED
        )
        if stopping in done and stop.is_set():
            startup.cancel()
            try:
                await startup
            except BaseException:
                pass
            return
        stopping.cancel()
        await startup

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                if not _same_user(writer):
                    return
                payload = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if not payload or len(payload) > MAX_MESSAGE_BYTES:
                    response: dict[str, Any] = {"error": "Invalid browser request size"}
                else:
                    try:
                        request = json.loads(payload)
                        if not isinstance(request, dict):
                            raise BrowserCommandError("Invalid browser request")
                        async with request_lock:
                            response = await service.handle(request)
                        should_stop = request.get("command") == "close"
                        if should_stop:
                            await driver.close()
                    except BrowserCommandError as exc:
                        response = {"error": str(exc)}
                        should_stop = False
                    except Exception:
                        response = {"error": "Browser command failed"}
                        should_stop = False
                try:
                    writer.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
                    await writer.drain()
                finally:
                    if payload and 'should_stop' in locals() and should_stop:
                        stop.set()
            except (asyncio.TimeoutError, ConnectionError, OSError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

        server = await asyncio.start_unix_server(handle, path=str(socket_path), limit=MAX_MESSAGE_BYTES)
        os.chmod(socket_path, 0o600)
        async with server:
            await stop.wait()
    finally:
        if server is not None:
            server.close()
            await server.wait_closed()
        await driver.close()
        for signal_name in (signal.SIGTERM, signal.SIGINT):
            try:
                asyncio.get_running_loop().remove_signal_handler(signal_name)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            metadata = socket_path.lstat()
            if stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.getuid():
                socket_path.unlink()
        except FileNotFoundError:
            pass
        os.close(lock_descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=Path)
    args = parser.parse_args(argv)
    profile = profile_for_agent(args.profile.name)
    if args.profile.absolute() != profile.absolute():
        raise CLIError("Invalid managed browser profile path")
    asyncio.run(_serve(profile))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CLIError, OSError, RuntimeError):
        raise SystemExit(1)
