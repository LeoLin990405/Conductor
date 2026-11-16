#!/usr/bin/env python3
"""Regenerate ``src/conductor_cli/schema_snapshot.json`` from a scratch database at Alembic head.

    .venv/bin/python scripts/generate_schema_snapshot.py --maintenance "host=/tmp/pg dbname=template1"

``--maintenance`` is a libpq connection string or URL for a database used only
to CREATE and DROP the scratch database (``TEST_DB_MAINTENANCE`` if omitted;
there is no default). The scratch database is created ``TEMPLATE template0``:
``template1`` can carry local objects or an ``alembic_version`` row, and the
snapshot must record only what the migrations build. It is migrated with
``alembic upgrade head``, its public tables and columns are written, and it is
dropped again, whatever happened.

Every connection is checked by ``src/conductor_cli/db_guard.py``: the
maintenance and scratch connections directly, and Alembic's own connections
(SQLAlchemy, then migration 001's pool) through ``psycopg_tripwire``. A
database that resolves to ``postgres``, or that cannot be resolved, is refused
before libpq runs.

Alembic is handed ``postgresql+psycopg:///<scratch>`` and nothing else. The
host, port, user, password and other parameters travel as the libpq
environment variables of the maintenance connection, so no credential is ever
spliced into a URL (SQLAlchemy renders spaces raw and query values with ``+``,
which libpq would read differently).
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.conductor_cli import db_guard  # noqa: E402
from src.conductor_cli.schema_drift import (  # noqa: E402
    SNAPSHOT_PATH,
    read_catalog,
    render_snapshot,
    snapshot_document,
)

MAINTENANCE_ENV = "TEST_DB_MAINTENANCE"
CLONE_TEMPLATE = "template0"
_SCRATCH_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_RESERVED = {"postgres", "template0", "template1"}

Connector = Callable[..., Any]
Migrate = Callable[[str, Mapping[str, str]], None]


class SnapshotError(RuntimeError):
    """The snapshot could not be generated safely. Nothing was migrated."""


def scratch_name(requested: str | None = None) -> str:
    name = requested or f"conductor_schema_snapshot_{uuid.uuid4().hex[:12]}"
    if _SCRATCH_NAME.fullmatch(name) is None or name in _RESERVED:
        raise SnapshotError(
            "the scratch database must be a new lower-case identifier, "
            "not postgres or a template"
        )
    return name


def script_head(root: Path = ROOT) -> str:
    """The single revision ``alembic upgrade head`` applies. Reads files only."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise SnapshotError(f"expected one alembic head, found {sorted(heads)}")
    return heads[0]


def libpq_environment(conninfo: str) -> dict[str, str]:
    """The maintenance parameters, except ``dbname``, as libpq environment variables.

    The variable names come from libpq's own option table, not from a copy.
    """
    from psycopg import pq

    params = db_guard.parse_conninfo(conninfo)
    envvars = {
        opt.keyword.decode(): opt.envvar.decode()
        for opt in pq.Conninfo.get_defaults()
        if opt.envvar
    }
    out: dict[str, str] = {}
    for key, value in params.items():
        if key == "dbname":
            continue
        envvar = envvars.get(key)
        if envvar is None:
            raise SnapshotError(f"the maintenance parameter {key!r} has no libpq environment variable")
        out[envvar] = value
    return out


@contextlib.contextmanager
def patched_environ(values: Mapping[str, str]) -> Iterator[None]:
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def alembic_upgrade(url: str, environ: Mapping[str, str]) -> None:
    """``alembic upgrade head`` on ``url`` with every psycopg connect guarded."""
    from alembic import command
    from alembic.config import Config

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    # env.py's fileConfig would replace this process's loggers.
    config.config_file_name = None
    with patched_environ(environ), db_guard.psycopg_tripwire():
        # The URL Alembic gets resolves under the same environment it runs in.
        db_guard.checked_conninfo(url.replace("postgresql+psycopg://", "postgresql://", 1))
        command.upgrade(config, "head")


def generate(
    maintenance: str,
    *,
    output: Path = SNAPSHOT_PATH,
    database: str | None = None,
    connector: Connector | None = None,
    migrate: Migrate = alembic_upgrade,
    head: str | None = None,
) -> dict[str, object]:
    """Create, migrate, read, write, drop. ``connector`` and ``migrate`` are injectable."""
    from psycopg import sql

    scratch = scratch_name(database)
    # Both connection strings are checked before anything is opened.
    admin_info = db_guard.checked_conninfo(maintenance)
    scratch_info = db_guard.checked_conninfo(maintenance, dbname=scratch)
    if db_guard.effective_dbname(admin_info).casefold() == scratch:
        raise SnapshotError("the maintenance database and the scratch database must differ")
    expected = head or script_head()
    environ = libpq_environment(maintenance)

    admin = db_guard.guarded_connect(admin_info, connector=connector, autocommit=True)
    try:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(scratch), sql.Identifier(CLONE_TEMPLATE)
            )
        )
        try:
            migrate(f"postgresql+psycopg:///{scratch}", environ)
            conn = db_guard.guarded_connect(scratch_info, connector=connector)
            try:
                catalog, revision = read_catalog(conn)
            finally:
                conn.close()
            if revision != expected:
                raise SnapshotError(f"migrated revision {revision} is not the script head {expected}")
            document = snapshot_document(catalog, revision)
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_name(output.name + ".tmp")
            temporary.write_text(render_snapshot(document), encoding="utf-8")
            temporary.replace(output)
            return document
        finally:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(scratch))
            )
    finally:
        admin.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--maintenance",
        default=os.environ.get(MAINTENANCE_ENV),
        help=f"libpq conninfo or URL of the CREATE/DROP database (default: ${MAINTENANCE_ENV}); postgres is refused",
    )
    parser.add_argument("--database", default=None, help="scratch database name (default: generated)")
    parser.add_argument("--output", type=Path, default=SNAPSHOT_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.maintenance:
        print(f"pass --maintenance or set {MAINTENANCE_ENV}", file=sys.stderr)
        return 2
    try:
        document = generate(args.maintenance, output=args.output, database=args.database)
    except (SnapshotError, db_guard.DatabaseRefused) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    tables = document["tables"]
    print(f"wrote {args.output}: revision {document['revision']}, {len(tables)} tables")  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
