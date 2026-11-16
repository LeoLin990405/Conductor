"""Optional throwaway PostgreSQL for one isolated Worker run (N19).

A Worker that needs integration tests has no prepared database, and one
Worker downloaded a VM to boot its own. The daemon can instead start a
disposable cluster on the host (``initdb`` + ``pg_ctl``, the same shape the
local gate scripts use), hand the Worker the ``TEST_DB_*`` variables the
integration tests already read, and remove the cluster when the run ends.

Opt-in only: ``CONDUCTOR_WORKER_TEST_PG`` must be ``on`` (``1``/``true``/
``yes``). Default ``off``; any other value also means off (with a warning),
so a typo never fails Worker runs that do not use the feature.

The daemon is async: use ``async_throwaway_pg``, which runs the blocking
``initdb``/``pg_ctl``/``createdb`` steps in a thread so lease heartbeats keep
running, and ``throwaway_pg_base_dir`` for a path that cannot escape the
isolation root whatever the ticket id is.

The cluster listens on ``127.0.0.1`` and a free port only, uses
``scram-sha-256`` with a random per-run password, has no unix socket (a long
isolation path would overflow the socket path limit), and keeps its data and
log under ``base_dir``, which is removed with it. The
database is never named ``postgres``. Commands run through an injectable
``runner`` so tests need no real PostgreSQL.

Hand ``ThrowawayPg.env()`` to ``worker_isolation(test_db_env=...)``: it sets
the variables in every child and opens the port in the macOS Seatbelt
profile, which denies loopback TCP by default.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    AsyncIterator,
    Callable,
    ContextManager,
    Iterator,
    Mapping,
    Protocol,
    Sequence,
    TypeVar,
)

logger = logging.getLogger(__name__)
_T = TypeVar("_T")

TEST_PG_ENV = "CONDUCTOR_WORKER_TEST_PG"
TEST_PG_BIN_ENV = "CONDUCTOR_WORKER_TEST_PG_BIN"
TEST_DB_ENV_NAMES = (
    "TEST_DB_HOST",
    "TEST_DB_PORT",
    "TEST_DB_NAME",
    "TEST_DB_USER",
    "TEST_DB_PASSWORD",
)
_TRUE = {"1", "on", "true", "yes"}
_FALSE = {"", "0", "off", "false", "no"}
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9_-]")
_PATH_STEM_MAX = 40
_RESERVED_DB_NAMES = {"postgres", "template0", "template1"}
DEFAULT_USER = "conductor_worker"
START_TIMEOUT_SECONDS = 60.0


class ThrowawayPgError(RuntimeError):
    """The throwaway cluster could not be started; no TEST_DB_* are injected."""


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class PgRunner(Protocol):
    def __call__(
        self, argv: Sequence[str], *, env: Mapping[str, str], timeout: float
    ) -> RunResult: ...


def subprocess_runner(
    argv: Sequence[str], *, env: Mapping[str, str], timeout: float
) -> RunResult:
    completed = subprocess.run(  # noqa: S603 - argv is built from fixed binaries
        list(argv),
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return RunResult(completed.returncode, completed.stdout, completed.stderr)


def free_localhost_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def worker_test_pg_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    raw = str(source.get(TEST_PG_ENV, "") or "").strip().lower()
    if raw in _TRUE:
        return True
    if raw not in _FALSE:
        # Unknown means off: the feature is opt-in and must never fail runs.
        logger.warning("%s=%r is not on/off; treating it as off", TEST_PG_ENV, raw[:40])
    return False


def throwaway_pg_base_dir(root: str | Path, ticket_id: str) -> Path:
    """``<root>/pg/<safe ticket>-<hash>``: one path component, never ``..``.

    The ticket id comes from the server; only ``[A-Za-z0-9_-]`` survive, the
    stem is bounded, and a hash of the raw id keeps distinct ids distinct.
    """
    raw = str(ticket_id)
    stem = _UNSAFE_PATH_CHARS.sub("_", raw)[:_PATH_STEM_MAX].strip("_-") or "ticket"
    digest = hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return Path(root) / "pg" / f"{stem}-{digest}"


def database_name_for(ticket_id: str) -> str:
    """``conductor_worker_<ticket8>``; refuses anything that is not a test DB."""
    stem = re.sub(r"[^a-z0-9_]", "_", str(ticket_id).lower())[:8].strip("_")
    name = f"conductor_worker_{stem}" if stem else "conductor_worker"
    return require_test_database_name(name)


def require_test_database_name(name: str) -> str:
    value = str(name).strip()
    if value.lower() in _RESERVED_DB_NAMES:
        raise ThrowawayPgError(f"refusing to provision database {value!r}")
    if not _IDENT.fullmatch(value):
        raise ThrowawayPgError(f"database name {value!r} is not a plain identifier")
    return value


@dataclass(frozen=True)
class ThrowawayPg:
    data_dir: Path
    log_file: Path
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)
    bin_dir: Path | None = None

    def env(self) -> dict[str, str]:
        """The ``TEST_DB_*`` values to inject into the Worker environment."""
        return {
            "TEST_DB_HOST": self.host,
            "TEST_DB_PORT": str(self.port),
            "TEST_DB_NAME": self.name,
            "TEST_DB_USER": self.user,
            "TEST_DB_PASSWORD": self.password,
        }


def _binary(name: str, bin_dir: Path | None) -> str:
    if bin_dir is not None:
        candidate = Path(bin_dir) / name
        if not candidate.is_file():
            raise ThrowawayPgError(f"PostgreSQL binary {name} not found in {bin_dir}")
        return str(candidate)
    found = shutil.which(name)
    if not found:
        raise ThrowawayPgError(f"PostgreSQL binary {name} is not on PATH")
    return found


def _resolve_bin_dir(bin_dir: str | Path | None) -> Path | None:
    if bin_dir is not None:
        return Path(bin_dir)
    raw = os.environ.get(TEST_PG_BIN_ENV, "").strip()
    return Path(raw).expanduser() if raw else None


def _base_env() -> dict[str, str]:
    # No PG* variables from the daemon: they could point the tools at a real
    # server or database.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PG") and key in {"PATH", "LANG", "LC_ALL", "TMPDIR"}
    }
    env.setdefault("PATH", "/usr/bin:/bin")
    env.setdefault("LANG", "en_US.UTF-8")
    return env


def _check(result: RunResult, step: str) -> None:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()[-1:]
        raise ThrowawayPgError(
            f"throwaway PG {step} failed (exit {result.returncode})"
            + (f": {detail[0][:300]}" if detail else "")
        )


def start_throwaway_pg(
    base_dir: str | Path,
    *,
    ticket_id: str,
    runner: PgRunner = subprocess_runner,
    port_picker: Callable[[], int] = free_localhost_port,
    bin_dir: str | Path | None = None,
    user: str = DEFAULT_USER,
    timeout: float = START_TIMEOUT_SECONDS,
) -> ThrowawayPg:
    """Create, start and seed one cluster under a fresh ``base_dir``."""
    base = Path(base_dir)
    if base.exists() and any(base.iterdir()):
        raise ThrowawayPgError("throwaway PG base directory must be empty")
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(base, 0o700)
    if not _IDENT.fullmatch(user) or user.lower() == "postgres":
        raise ThrowawayPgError(f"refusing PG role {user!r}")
    name = database_name_for(ticket_id)
    bins = _resolve_bin_dir(bin_dir)
    initdb, pg_ctl, createdb = (
        _binary("initdb", bins),
        _binary("pg_ctl", bins),
        _binary("createdb", bins),
    )
    data_dir = base / "data"
    log_file = base / "postgres.log"
    password = secrets.token_urlsafe(24)
    pwfile = base / "pwfile"
    fd = os.open(pwfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(password + "\n")
    env = _base_env()
    try:
        _check(
            runner(
                [
                    initdb,
                    "-D", str(data_dir),
                    "-U", user,
                    "-E", "UTF8",
                    "--auth=scram-sha-256",
                    f"--pwfile={pwfile}",
                    "--no-instructions",
                ],
                env=env,
                timeout=timeout,
            ),
            "initdb",
        )
    finally:
        pwfile.unlink(missing_ok=True)
    port = int(port_picker())
    options = (
        f"-c listen_addresses=127.0.0.1 -p {port} -c unix_socket_directories='' "
        "-c fsync=off -c full_page_writes=off -c synchronous_commit=off"
    )
    _check(
        runner(
            [pg_ctl, "-D", str(data_dir), "-l", str(log_file), "-o", options,
             "-w", "-t", str(int(timeout)), "start"],
            env=env,
            timeout=timeout + 5,
        ),
        "start",
    )
    pg = ThrowawayPg(
        data_dir=data_dir,
        log_file=log_file,
        host="127.0.0.1",
        port=port,
        name=name,
        user=user,
        password=password,
        bin_dir=bins,
    )
    try:
        _check(
            runner(
                [createdb, "-h", pg.host, "-p", str(port), "-U", user, name],
                env={**env, "PGPASSWORD": password},
                timeout=timeout,
            ),
            "createdb",
        )
    except BaseException:
        stop_throwaway_pg(pg, runner=runner)
        raise
    return pg


def stop_throwaway_pg(
    pg: ThrowawayPg, *, runner: PgRunner = subprocess_runner, timeout: float = 30.0
) -> bool:
    """Stop the cluster (immediate mode). Returns False when pg_ctl failed."""
    try:
        pg_ctl = _binary("pg_ctl", pg.bin_dir)
    except ThrowawayPgError:
        return False
    result = runner(
        [pg_ctl, "-D", str(pg.data_dir), "-m", "immediate", "-w", "stop"],
        env=_base_env(),
        timeout=timeout,
    )
    return result.returncode == 0


@contextmanager
def throwaway_pg(
    base_dir: str | Path,
    *,
    ticket_id: str,
    runner: PgRunner = subprocess_runner,
    port_picker: Callable[[], int] = free_localhost_port,
    bin_dir: str | Path | None = None,
    enabled: bool | None = None,
) -> Iterator[ThrowawayPg | None]:
    """Yield a running cluster, or ``None`` when the opt-in is off.

    The cluster is stopped and ``base_dir`` removed on exit, also on error.
    """
    if not (worker_test_pg_enabled() if enabled is None else enabled):
        yield None
        return
    base = Path(base_dir)
    if base.exists() and any(base.iterdir()):
        # Checked here too: the cleanup below must never remove foreign files.
        raise ThrowawayPgError("throwaway PG base directory must be empty")
    try:
        pg = start_throwaway_pg(
            base,
            ticket_id=ticket_id,
            runner=runner,
            port_picker=port_picker,
            bin_dir=bin_dir,
        )
    except BaseException:
        shutil.rmtree(base, ignore_errors=True)
        raise
    try:
        yield pg
    finally:
        stop_throwaway_pg(pg, runner=runner)
        shutil.rmtree(base, ignore_errors=True)


@asynccontextmanager
async def async_throwaway_pg(
    base_dir: str | Path,
    *,
    ticket_id: str,
    runner: PgRunner = subprocess_runner,
    port_picker: Callable[[], int] = free_localhost_port,
    bin_dir: str | Path | None = None,
    enabled: bool | None = None,
) -> AsyncIterator[ThrowawayPg | None]:
    """``throwaway_pg`` for async callers: start and stop run in a thread.

    ``initdb``, ``pg_ctl`` and ``createdb`` can each block for up to a minute;
    on the event loop they would stall lease heartbeats. When the caller is
    cancelled mid-start, the start thread is still awaited (shielded) so a
    cluster it brings up is stopped rather than orphaned.
    """
    if not (worker_test_pg_enabled() if enabled is None else enabled):
        yield None
        return
    base = Path(base_dir)
    if base.exists() and any(base.iterdir()):
        raise ThrowawayPgError("throwaway PG base directory must be empty")

    def _start() -> ThrowawayPg:
        return start_throwaway_pg(
            base,
            ticket_id=ticket_id,
            runner=runner,
            port_picker=port_picker,
            bin_dir=bin_dir,
        )

    def _cleanup(pg: ThrowawayPg | None) -> None:
        try:
            if pg is not None:
                stop_throwaway_pg(pg, runner=runner)
        finally:
            shutil.rmtree(base, ignore_errors=True)

    start = asyncio.ensure_future(asyncio.to_thread(_start))
    try:
        pg = await asyncio.shield(start)
    except BaseException:
        # Cancelled while the thread runs: wait for it (even through repeated
        # cancels), then stop whatever it started.
        while not start.done():
            try:
                await asyncio.shield(start)
            except BaseException:  # noqa: BLE001 - outcome is read from start below
                continue
        started: ThrowawayPg | None = None
        if not start.cancelled() and start.exception() is None:
            started = start.result()
        await asyncio.shield(asyncio.to_thread(_cleanup, started))
        raise
    try:
        yield pg
    finally:
        await asyncio.shield(asyncio.to_thread(_cleanup, pg))


@asynccontextmanager
async def entered_in_async(cm: ContextManager[_T]) -> AsyncIterator[_T]:
    """Enter a sync context manager inside an ``async with`` item list.

    Lets ``async with async_throwaway_pg(...) as pg, entered_in_async(
    worker_isolation(...pg...))`` keep one block; the sync manager's enter
    and exit run inline exactly as under a plain ``with``.
    """
    with cm as value:
        yield value
