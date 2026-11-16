"""Refuse a connection to the ``postgres`` database before libpq is called.

The effective database is computed from what libpq itself parses, never from a
hand-written URL split. ``psycopg.conninfo.make_conninfo`` merges keyword
overrides into the connection string and ``conninfo_to_dict`` hands the result
to libpq's ``PQconninfoParse``. That parser decides every URI question the
earlier hand parsers got wrong: a ``?dbname=`` query that wins over the path, a
``/`` inside a query value, ``#`` that is not a fragment, percent-encoding,
repeated keys, and invalid parameters (``?database=`` is an error, not an alias).

``PQconninfoParse`` does not apply defaults, so the remaining step mirrors
libpq's ``conninfo_add_defaults`` and ``connectOptions2``:

* user: the parsed ``user`` if present, else ``PGUSER`` if set, and an empty
  result becomes the operating-system user (``getpwuid(geteuid())``, not
  ``$USER``);
* dbname: the parsed ``dbname`` if present, else ``PGDATABASE`` if set, and an
  empty result becomes the effective user.

An explicit empty ``dbname`` does not fall through to ``PGDATABASE``; libpq
treats a present key as set. A ``service`` (in the string or ``PGSERVICE``) can
supply a database name from a service file, so it is refused as unresolvable
rather than read. Anything unparsable is refused too. The comparison is
case-insensitive and ignores surrounding space, which refuses a little more
than PostgreSQL would open as ``postgres``, never less.

Callers connect with the exact string that was checked, with the resolved
database written into it, so the guard and the connection cannot read
different parameters.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Iterator, Mapping
from typing import Any

REFUSED_DATABASE = "postgres"

# psycopg.connect keywords that are not libpq connection parameters.
_CONNECT_OPTIONS = frozenset(
    {"autocommit", "prepare_threshold", "context", "row_factory", "cursor_factory"}
)

OsUser = Callable[[], str]
Connector = Callable[..., Any]


class DatabaseRefused(Exception):
    """The connection would open ``postgres`` or cannot be resolved. Nothing was opened.

    ``reason`` is ``"postgres"`` for the named database and ``"unresolvable"`` otherwise.
    """

    def __init__(self, message: str, *, reason: str = "unresolvable") -> None:
        super().__init__(message)
        self.reason = reason


def _os_user() -> str:
    """The name libpq's ``pg_fe_getauthname`` uses: the effective uid's passwd entry."""
    import pwd

    return pwd.getpwuid(os.geteuid()).pw_name


def is_refused_name(name: str) -> bool:
    return name.strip().casefold() == REFUSED_DATABASE


def parse_conninfo(conninfo: str = "", **params: object) -> dict[str, str]:
    """The parameters libpq reads from ``conninfo`` plus keyword overrides.

    Raises ``DatabaseRefused`` when libpq rejects the string.
    """
    from psycopg import Error
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    try:
        merged = make_conninfo(conninfo, **params)  # type: ignore[arg-type]
        parsed = conninfo_to_dict(merged)
    except (Error, TypeError, ValueError) as exc:
        raise DatabaseRefused(
            f"Refusing a connection string libpq cannot parse ({type(exc).__name__})."
        ) from None
    return {key: str(value) for key, value in parsed.items() if value is not None}


def effective_user(
    params: Mapping[str, str],
    *,
    environ: Mapping[str, str] | None = None,
    os_user: OsUser | None = None,
) -> str:
    env = os.environ if environ is None else environ
    if "user" in params:
        user = params["user"]
    elif "PGUSER" in env:
        user = env["PGUSER"]
    else:
        user = ""
    if user:
        return user
    try:
        user = (os_user or _os_user)()
    except (ImportError, KeyError, OSError) as exc:
        raise DatabaseRefused(
            f"Refusing a connection whose user cannot be resolved ({type(exc).__name__})."
        ) from None
    if not isinstance(user, str) or not user:
        raise DatabaseRefused("Refusing a connection whose user cannot be resolved.")
    return user


def effective_dbname(
    conninfo: str = "",
    *,
    environ: Mapping[str, str] | None = None,
    os_user: OsUser | None = None,
    **params: object,
) -> str:
    """The database libpq would open for ``conninfo`` and ``params``.

    Raises ``DatabaseRefused`` when that cannot be decided without guessing.
    """
    env = os.environ if environ is None else environ
    parsed = parse_conninfo(conninfo, **params)
    if "service" in parsed or "PGSERVICE" in env:
        raise DatabaseRefused(
            "Refusing a connection that names a libpq service: "
            "the service file could choose the database."
        )
    if "dbname" in parsed:
        name = parsed["dbname"]
    elif "PGDATABASE" in env:
        name = env["PGDATABASE"]
    else:
        name = ""
    if not name:
        name = effective_user(parsed, environ=env, os_user=os_user)
    return name


def checked_conninfo(
    conninfo: str = "",
    *,
    environ: Mapping[str, str] | None = None,
    os_user: OsUser | None = None,
    **params: object,
) -> str:
    """The merged connection string, after refusing ``postgres``.

    The resolved database is written into the string, so a default libpq would
    otherwise take from the environment at connect time cannot change between
    this check and the connect. Connect with the returned string and nothing
    else.
    """
    from psycopg.conninfo import make_conninfo

    name = effective_dbname(conninfo, environ=environ, os_user=os_user, **params)
    if not name.strip():
        raise DatabaseRefused("Refusing a connection with no resolvable database name.")
    if is_refused_name(name):
        raise DatabaseRefused("Refusing to connect to a database named postgres.", reason="postgres")
    merged = make_conninfo(conninfo, **params)  # type: ignore[arg-type]
    return make_conninfo(merged, dbname=name)


def guarded_connect(
    conninfo: str = "",
    *,
    connector: Connector | None = None,
    environ: Mapping[str, str] | None = None,
    os_user: OsUser | None = None,
    autocommit: bool = False,
    **params: object,
) -> Any:
    """Refuse ``postgres``, then open the checked string with ``connector``.

    ``connector`` defaults to ``psycopg.connect``; tests inject a fake.
    """
    checked = checked_conninfo(conninfo, environ=environ, os_user=os_user, **params)
    if connector is None:
        import psycopg

        connector = psycopg.connect
    return connector(checked, autocommit=autocommit)


@contextlib.contextmanager
def psycopg_tripwire() -> Iterator[None]:
    """Guard every psycopg connect in this process while the block runs.

    Alembic and migration ``001`` open their own connections (SQLAlchemy's
    psycopg dialect, then a psycopg pool). Wrapping ``Connection.connect`` and
    ``AsyncConnection.connect`` checks each of those with the same rule, at the
    point libpq would read the parameters. ``psycopg.connect`` is a module
    alias bound at import time, so it is replaced as well.
    """
    import psycopg

    originals: dict[type, Any] = {}
    module_connect = psycopg.connect

    def wrap(cls: type) -> None:
        original = cls.__dict__["connect"]
        originals[cls] = original

        def connect(target: type, conninfo: str = "", **kwargs: Any) -> Any:
            params = {k: v for k, v in kwargs.items() if k not in _CONNECT_OPTIONS}
            checked_conninfo(conninfo, **params)
            return original.__get__(None, target)(conninfo, **kwargs)

        setattr(cls, "connect", classmethod(connect))

    try:
        wrap(psycopg.Connection)
        wrap(psycopg.AsyncConnection)
        psycopg.connect = psycopg.Connection.connect  # type: ignore[assignment]
        yield
    finally:
        psycopg.connect = module_connect  # type: ignore[assignment]
        for cls, original in originals.items():
            setattr(cls, "connect", original)
