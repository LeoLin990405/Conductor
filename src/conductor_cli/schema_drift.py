"""Compare the live public tables and columns with the committed Alembic-head snapshot.

``schema_snapshot.json`` is written by ``scripts/generate_schema_snapshot.py``
from a scratch database migrated to head. ``cond doctor`` reads the database
the backend uses (``DB_*`` in the environment, then the checkout ``.env``, with
the backend's defaults) and reports each drifted table or ``table.column`` as
its own line: FAIL when the database lacks
something the head expects, WARN for an extra table or column.

Every connection goes through ``db_guard``: the effective database is resolved
the way libpq resolves it, and ``postgres`` is refused before the connector is
reached. The connector is injected, so unit tests never open a socket.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import db_guard

SNAPSHOT_PATH = Path(__file__).resolve().parent / "schema_snapshot.json"
CHECK_NAME = "schema_drift"
# Enough to name the problem without flooding the terminal after a bad restore.
MAX_DETAIL_LINES = 40

# Public base tables only. Views and other schemas are not part of the
# migration contract the snapshot records.
PUBLIC_SCHEMA_SQL = """
SELECT t.table_name, c.column_name
FROM information_schema.tables AS t
LEFT JOIN information_schema.columns AS c
  ON c.table_schema = t.table_schema
 AND c.table_name = t.table_name
WHERE t.table_schema = 'public'
  AND t.table_type = 'BASE TABLE'
ORDER BY t.table_name, c.ordinal_position
"""
REVISION_SQL = "SELECT version_num FROM alembic_version"

# Read-only session with short timeouts: doctor must not hang or write.
DOCTOR_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=5000"

Catalog = dict[str, list[str]]
Connect = Callable[[str], Any]


@dataclass(frozen=True)
class Snapshot:
    revision: str
    tables: Catalog


def snapshot_document(catalog: Mapping[str, Sequence[str]], revision: str) -> dict[str, object]:
    """The committed document: the head revision and sorted public columns."""
    if not isinstance(revision, str) or not revision.strip():
        raise ValueError("schema snapshot revision is required")
    tables = _normalize(catalog)
    if not tables:
        raise ValueError("schema snapshot catalog is empty")
    return {
        "revision": revision,
        "tables": {name: sorted(columns) for name, columns in sorted(tables.items())},
    }


def render_snapshot(document: Mapping[str, object]) -> str:
    return json.dumps(document, indent=2, sort_keys=False) + "\n"


def load_snapshot(path: Path | None = None) -> Snapshot:
    """The committed snapshot. Raises ``ValueError`` or ``OSError`` when unusable."""
    payload = json.loads((path or SNAPSHOT_PATH).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("schema snapshot must be an object")
    revision = payload.get("revision")
    tables = payload.get("tables")
    if not isinstance(tables, Mapping):
        raise ValueError("schema snapshot must contain tables")
    document = snapshot_document(tables, revision)  # type: ignore[arg-type]
    return Snapshot(revision=str(document["revision"]), tables=document["tables"])  # type: ignore[arg-type]


def _normalize(catalog: object) -> dict[str, set[str]]:
    if not isinstance(catalog, Mapping):
        raise ValueError("schema catalog must map tables to columns")
    out: dict[str, set[str]] = {}
    for table, columns in catalog.items():
        if not isinstance(table, str) or not table:
            raise ValueError("table name must be a non-empty string")
        if isinstance(columns, (str, bytes)) or not isinstance(columns, Sequence):
            raise ValueError(f"columns of {table} must be a list of names")
        names = set()
        for column in columns:
            if not isinstance(column, str) or not column:
                raise ValueError(f"column of {table} must be a non-empty string")
            names.add(column)
        out[table] = names
    return out


def rows_to_catalog(rows: Sequence[Sequence[object]]) -> Catalog:
    """Table to sorted columns from ``PUBLIC_SCHEMA_SQL`` rows. A table with no columns stays."""
    tables: dict[str, set[str]] = {}
    for row in rows:
        table, column = row[0], row[1]
        if not isinstance(table, str) or not table:
            continue
        names = tables.setdefault(table, set())
        if isinstance(column, str) and column:
            names.add(column)
    return {name: sorted(columns) for name, columns in sorted(tables.items())}


def read_catalog(conn: Any) -> tuple[Catalog, str | None]:
    """Public catalog and Alembic revision over an open connection."""
    catalog = rows_to_catalog(conn.execute(PUBLIC_SCHEMA_SQL).fetchall())
    revision: str | None = None
    if "alembic_version" in catalog:
        rows = conn.execute(REVISION_SQL).fetchall()
        if len(rows) == 1 and isinstance(rows[0][0], str):
            revision = rows[0][0]
    return catalog, revision


def diff_catalogs(live: Mapping[str, Sequence[str]], expected: Mapping[str, Sequence[str]]) -> list[str]:
    """One line per drifted table or ``table.column``, in a stable order.

    Columns compare as ``(table, column)`` pairs, so a dotted name cannot cancel
    another. A missing table is one line; its columns are implied.
    """
    actual = _normalize(live)
    wanted = _normalize(expected)
    lines = [f"missing table {name}" for name in sorted(set(wanted) - set(actual))]
    lines += [f"extra table {name}" for name in sorted(set(actual) - set(wanted))]
    for table in sorted(set(wanted) & set(actual)):
        lines += [f"missing column {table}.{c}" for c in sorted(wanted[table] - actual[table])]
        lines += [f"extra column {table}.{c}" for c in sorted(actual[table] - wanted[table])]
    return lines


def _check(ok: bool, detail: str, **extra: object) -> dict[str, object]:
    return {"name": CHECK_NAME, "ok": ok, "detail": detail, **extra}


def drift_checks(
    live: Mapping[str, Sequence[str]], revision: str | None, snapshot: Snapshot
) -> list[dict[str, object]]:
    """FAIL for what the migrations expect and the database lacks; WARN for extras.

    A missing table or column, or a revision other than the snapshot's, means
    the database is not at the head the code expects. A table or column the
    snapshot does not name is usually something an operator added, so it is a
    WARN line. FAIL lines come first so the cap never hides one behind a WARN.
    """
    lines: list[str] = []
    if revision != snapshot.revision:
        lines.append(f"alembic revision {revision or 'none'}, snapshot expects {snapshot.revision}")
    lines += diff_catalogs(live, snapshot.tables)
    if not lines:
        return [_check(True, f"matches snapshot at revision {snapshot.revision}")]
    failures = [line for line in lines if not line.startswith("extra ")]
    extras = [line for line in lines if line.startswith("extra ")]
    checks = [_check(False, line) for line in failures]
    checks += [_check(True, f"{line} (not in the snapshot)", warn=True) for line in extras]
    if len(checks) > MAX_DETAIL_LINES:
        hidden = checks[MAX_DETAIL_LINES:]
        checks = checks[:MAX_DETAIL_LINES]
        hidden_fail = any(not item["ok"] for item in hidden)
        summary = f"{len(hidden)} more drift lines not shown"
        checks.append(_check(not hidden_fail, summary, **({} if hidden_fail else {"warn": True})))
    return checks


def _file_values(repo: Path) -> Mapping[str, object]:
    try:
        from dotenv import dotenv_values
    except ImportError as exc:
        raise OSError("python-dotenv is required to read the checkout .env") from exc
    values = dotenv_values(repo / ".env")
    return values if isinstance(values, Mapping) else {}


def doctor_conninfo(repo: Path, environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    """The backend database as a libpq keyword string, plus the password to redact.

    Mirrors ``src/server/database/pool.py``: ``DB_*`` from the environment, then
    the checkout ``.env``, then the backend defaults (``DB_NAME`` defaults to
    ``postgres``, which the guard refuses). ``make_conninfo`` quotes every
    value, so credentials are never spliced into a URL.
    """
    from psycopg.conninfo import make_conninfo

    env = os.environ if environ is None else environ
    file_values = _file_values(repo)

    def pick(name: str, default: str) -> str:
        for value in (env.get(name), file_values.get(name)):
            if isinstance(value, str) and value:
                return value
        return default

    password = pick("DB_PASSWORD", "postgres")
    conninfo = make_conninfo(
        "",
        host=pick("DB_HOST", "localhost"),
        port=pick("DB_PORT", "5432"),
        user=pick("DB_USER", "postgres"),
        password=password,
        dbname=pick("DB_NAME", "postgres"),
        sslmode=pick("DB_SSLMODE", "prefer"),
        connect_timeout="5",
        options=DOCTOR_OPTIONS,
    )
    return conninfo, password


def _refused(exc: db_guard.DatabaseRefused) -> dict[str, object]:
    """A clean skip. A deployment whose database is named postgres lands here by design."""
    if exc.reason == "postgres":
        detail = (
            "skipped: the backend database is named postgres, which doctor never opens; "
            "the check runs once DB_NAME names a dedicated database"
        )
    else:
        detail = f"skipped: {exc}"
    return _check(True, detail, warn=True)


def schema_checks(
    repo: Path,
    connect: Connect | None,
    *,
    environ: Mapping[str, str] | None = None,
    os_user: db_guard.OsUser | None = None,
    snapshot_path: Path | None = None,
    sensitive: list[str] | None = None,
) -> list[dict[str, object]]:
    """Doctor checks for schema drift. ``connect`` receives the checked conninfo only.

    A refused or unresolvable database is a WARN, not a FAIL: the check did not
    read a catalog, and the rest of the report still stands. The local
    production deployment uses a database named postgres, so this is the
    expected result there until that database is renamed. Driver errors are
    reduced to their class name because their text can quote the connection.
    """
    try:
        snapshot = load_snapshot(snapshot_path)
    except (OSError, ValueError, UnicodeError):
        return [_check(False, "snapshot unreadable")]
    if connect is None:
        return [_check(True, "skipped: no database connector", warn=True)]
    try:
        conninfo, password = doctor_conninfo(repo, environ)
    except OSError as exc:
        return [_check(False, f"database settings unreadable ({exc})")]
    if sensitive is not None:
        sensitive.append(password)
    try:
        checked = db_guard.checked_conninfo(conninfo, environ=environ, os_user=os_user)
    except db_guard.DatabaseRefused as exc:
        return [_refused(exc)]
    try:
        conn = connect(checked)
        try:
            live, revision = read_catalog(conn)
        finally:
            close = getattr(conn, "close", None)
            if callable(close):
                close()
    except db_guard.DatabaseRefused as exc:
        return [_refused(exc)]
    except Exception as exc:  # noqa: BLE001 - any driver failure is one check
        return [_check(False, f"live schema unreadable ({type(exc).__name__})")]
    try:
        return drift_checks(live, revision, snapshot)
    except ValueError:
        return [_check(False, "live schema unreadable (malformed catalog)")]
