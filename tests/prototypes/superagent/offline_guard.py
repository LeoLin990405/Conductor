"""Keep SuperAgent contract runs off the network and off real databases.

The product store is Postgres. These prototypes are given a process-local
double instead. ``pytest_socket`` only patches the current interpreter, and
libpq connects from C, so a TCP block around one turn still lets an import,
``start``, or a cold-start child open a real database. This guard is installed
for the whole test and, via ``sitecustomize``, for Python children.
"""

from __future__ import annotations

import importlib.machinery
import inspect
import os
import socket
import sys
import warnings
from collections.abc import Callable
from types import ModuleType
from typing import Any

from pytest_socket import SocketBlockedError, disable_socket, enable_socket

# Imported drivers are patched. Later imports of the same entry points are
# patched by the meta-path hook. A pure socket block does not see libpq.
_DB_MODULE_NAMES = frozenset(
    {
        "asyncpg",
        "psycopg",
        "psycopg.generators",
        "psycopg.pq",
        "psycopg2",
        "psycopg_pool",
        "psycopg_pool.pool",
        "psycopg_pool.pool_async",
    }
)

_INSTALLED = False
_FINDER: Any = None
_ORIGINAL_CONNECT: Callable[..., Any] | None = None
_ORIGINALS: list[tuple[Any, str, Any]] = []
_PATCHED: set[tuple[int, str]] = set()


class RealDatabaseRejected(RuntimeError):
    """A prototype tried to open a real database connection."""


def install() -> None:
    """Block new sockets and reject database client entry points.

    Unix datagram and stream sockets stay available so asyncio can build its
    self-pipe. A path that looks like a Postgres socket is still rejected.
    """

    global _INSTALLED
    if _INSTALLED:
        return
    try:
        _install_network_guard()
        _install_db_hook()
        _arm_loaded_modules()
    except Exception:
        uninstall()
        raise
    _INSTALLED = True


def uninstall() -> None:
    """Restore sockets and database entry points for later tests in-process."""

    global _FINDER, _INSTALLED
    if not _INSTALLED and _FINDER is None and not _ORIGINALS:
        _INSTALLED = False
        return
    finder = _FINDER
    _FINDER = None
    if finder is not None and finder in sys.meta_path:
        sys.meta_path.remove(finder)
    while _ORIGINALS:
        obj, name, raw = _ORIGINALS.pop()
        try:
            setattr(obj, name, raw)
        except TypeError:
            continue
    _PATCHED.clear()
    try:
        enable_socket()
    finally:
        _INSTALLED = False


def assert_offline() -> None:
    """Fail when this interpreter can open a socket or a real database."""

    # pytest-socket warns as it raises. The raise is the assertion.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        try:
            socket.socket()
        except SocketBlockedError:
            pass
        else:
            raise AssertionError("TCP socket was created")

    import psycopg
    from psycopg import pq

    # host and connect_timeout bound a missed patch; a real client must raise
    # RealDatabaseRejected before libpq dials.
    dsn = "dbname=postgres host=127.0.0.1 connect_timeout=1"
    for opener in (psycopg.connect, psycopg.Connection.connect, pq.PGconn.connect):
        _expect_database_rejected(opener, dsn)

    try:
        pending = psycopg.AsyncConnection.connect(dsn)
    except RealDatabaseRejected:
        pending = None
    if pending is not None:
        if inspect.iscoroutine(pending):
            pending.close()
        raise AssertionError("async database connect was not rejected")

    import psycopg_pool

    _expect_database_rejected(psycopg_pool.ConnectionPool, dsn)
    _expect_database_rejected(psycopg_pool.AsyncConnectionPool, dsn)

    if hasattr(socket, "AF_UNIX"):
        unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            unix.connect("/tmp/.s.PGSQL.5432")
        except RealDatabaseRejected:
            pass
        else:
            raise AssertionError("postgres unix socket was not rejected")
        finally:
            unix.close()


def _expect_database_rejected(opener: Callable[..., Any], *args: Any) -> None:
    try:
        opener(*args)
    except RealDatabaseRejected:
        return
    raise AssertionError(f"{opener!r} was not rejected")


def _install_network_guard() -> None:
    global _ORIGINAL_CONNECT
    if _ORIGINAL_CONNECT is None:
        _ORIGINAL_CONNECT = socket.socket.connect
    disable_socket(allow_unix_socket=True)
    original = _ORIGINAL_CONNECT

    def connect(self: socket.socket, address: Any) -> Any:
        if _is_postgres_address(address):
            raise RealDatabaseRejected(
                f"real database connection rejected: {address!r}"
            )
        return original(self, address)

    socket.socket.connect = connect  # type: ignore[method-assign]


def _is_postgres_address(address: object) -> bool:
    if isinstance(address, os.PathLike):
        text = os.fspath(address)
    elif isinstance(address, bytes):
        text = address.decode("utf-8", "replace")
    elif isinstance(address, str):
        text = address
    else:
        return False
    lowered = text.replace("\\", "/").lower()
    return ".s.pgsql." in lowered or "/postgresql" in lowered


def _install_db_hook() -> None:
    global _FINDER
    if _FINDER is not None:
        return

    class _Finder:
        def find_spec(
            self,
            fullname: str,
            path: object = None,
            target: object = None,
        ) -> importlib.machinery.ModuleSpec | None:
            if fullname not in _DB_MODULE_NAMES:
                return None
            sys.meta_path.remove(self)
            try:
                spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            finally:
                if self not in sys.meta_path:
                    sys.meta_path.insert(0, self)
            if spec is None or spec.loader is None:
                return None
            if getattr(spec.loader, "_superagent_db_patch", False):
                return spec
            spec.loader = _Loader(spec.loader)
            return spec

    class _Loader:
        _superagent_db_patch = True

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def create_module(self, spec: importlib.machinery.ModuleSpec) -> Any:
            create = getattr(self._inner, "create_module", None)
            if create is None:
                return None
            return create(spec)

        def exec_module(self, module: ModuleType) -> None:
            self._inner.exec_module(module)
            _arm_database_module(module)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    _FINDER = _Finder()
    sys.meta_path.insert(0, _FINDER)


def _arm_loaded_modules() -> None:
    for name in _DB_MODULE_NAMES:
        module = sys.modules.get(name)
        if module is not None:
            _arm_database_module(module)


def _arm_database_module(module: ModuleType) -> None:
    name = module.__name__
    if name == "psycopg":
        _reject_attr(module, "connect")
        connection = getattr(module, "Connection", None)
        if connection is not None:
            _reject_attr(connection, "connect")
        async_connection = getattr(module, "AsyncConnection", None)
        if async_connection is not None:
            _reject_attr(async_connection, "connect")
        return
    if name == "psycopg.pq":
        # ``PGconn.connect`` lives on an immutable extension type. Replace the
        # module attribute; assigning onto the class raises TypeError.
        _reject_pgconn(module)
        return
    if name == "psycopg.generators":
        _reject_attr(module, "connect")
        return
    if name in {"asyncpg", "psycopg2"}:
        _reject_attr(module, "connect")
        return
    for attr in ("ConnectionPool", "AsyncConnectionPool"):
        pool = getattr(module, attr, None)
        if pool is not None:
            _reject_attr(pool, "__init__")
            _reject_attr(pool, "_connect")


def _reject_pgconn(module: ModuleType) -> None:
    key = (id(module), "PGconn")
    if key in _PATCHED or not hasattr(module, "PGconn"):
        return
    raw = module.PGconn

    class _RejectedPGconn:
        """Stands in for libpq's connection type and refuses to dial."""

        @classmethod
        def connect(cls, *_args: Any, **_kwargs: Any) -> None:
            raise RealDatabaseRejected("real database connections are rejected")

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise RealDatabaseRejected("real database connections are rejected")

    _PATCHED.add(key)
    _ORIGINALS.append((module, "PGconn", raw))
    module.PGconn = _RejectedPGconn  # type: ignore[misc]


def _reject_attr(obj: Any, name: str) -> None:
    key = (id(obj), name)
    if key in _PATCHED:
        return
    # Only attributes defined on this object. Inherited names stay with the
    # parent so uninstall puts the original back once, not a copy of the patch.
    try:
        raw = vars(obj)[name]
    except (TypeError, KeyError):
        return
    _PATCHED.add(key)
    _ORIGINALS.append((obj, name, raw))

    def rejected(*_args: Any, **_kwargs: Any) -> None:
        raise RealDatabaseRejected("real database connections are rejected")

    if isinstance(raw, classmethod):
        replacement: Any = classmethod(rejected)
    elif isinstance(raw, staticmethod):
        replacement = staticmethod(rejected)
    else:
        replacement = rejected
    setattr(obj, name, replacement)
