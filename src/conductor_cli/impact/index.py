"""Incremental sqlite index of symbols, calls, and imports.

A file is parsed again only when its size or mtime changes and the content
hash differs. The database lives at ``<root>/.conductor/impact/index.sqlite``
unless a test or ``--index`` selects another path.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .extract import extract_path
from .model import (
    EXCLUDE_DIRS,
    MAX_FILE_BYTES,
    SCHEMA_VERSION,
    Call,
    FileFacts,
    ImpactError,
    Import,
    Symbol,
    language_for_path,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    language TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL,
    content_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS symbols (
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    parent TEXT NOT NULL,
    exported TEXT NOT NULL,
    start_column INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS calls (
    path TEXT NOT NULL,
    caller TEXT NOT NULL,
    callee TEXT NOT NULL,
    callee_name TEXT NOT NULL,
    receiver TEXT NOT NULL,
    receiver_kind TEXT NOT NULL,
    line INTEGER NOT NULL,
    enclosing_class TEXT NOT NULL,
    column INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS imports (
    path TEXT NOT NULL,
    module TEXT NOT NULL,
    name TEXT NOT NULL,
    alias TEXT NOT NULL,
    kind TEXT NOT NULL,
    line INTEGER NOT NULL,
    level INTEGER NOT NULL,
    scope TEXT NOT NULL,
    column INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_qual ON symbols(qualified_name);
CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path);
CREATE INDEX IF NOT EXISTS idx_calls_name ON calls(callee_name);
CREATE INDEX IF NOT EXISTS idx_calls_path ON calls(path);
CREATE INDEX IF NOT EXISTS idx_imports_path ON imports(path);
"""


class ImpactIndex:
    def __init__(self, root: Path, db_path: Path | None = None) -> None:
        self.root = root.resolve()
        # An explicit path is the caller's choice. The default path is not:
        # a symlink out of the checkout would be opened and, on a version
        # mismatch, have its tables dropped.
        self._guard_default_path = db_path is None
        self.db_path = db_path if db_path is not None else (
            self.root / ".conductor" / "impact" / "index.sqlite"
        )

    def update(self) -> dict[str, int]:
        """Refresh the index from the working tree. Returns parse counts."""
        stats = {"parsed": 0, "skipped": 0, "removed": 0, "unchanged_hash": 0}
        discovered: set[str] = set()
        with self._connect() as conn:
            for path in iter_source_files(self.root):
                rel = path.relative_to(self.root).as_posix()
                discovered.add(rel)
                self._refresh_file(conn, path, rel, stats)
            rows = conn.execute("SELECT path FROM files").fetchall()
            for row in rows:
                if row["path"] not in discovered:
                    _delete_path(conn, row["path"])
                    stats["removed"] += 1
        stats["files"] = len(discovered)
        return stats

    def load_facts(self) -> tuple[list[str], list[Symbol], list[Call], list[Import]]:
        with self._connect() as conn:
            files = [row["path"] for row in conn.execute("SELECT path FROM files ORDER BY path")]
            symbols = [
                Symbol(
                    row["path"],
                    row["name"],
                    row["qualified_name"],
                    row["kind"],
                    row["start_line"],
                    row["end_line"],
                    row["parent"],
                    row["exported"],
                    row["start_column"],
                )
                for row in conn.execute(
                    "SELECT path, name, qualified_name, kind, start_line, end_line, parent, exported, start_column "
                    "FROM symbols ORDER BY path, start_line, start_column, qualified_name"
                )
            ]
            calls = [
                Call(
                    row["path"],
                    row["caller"],
                    row["callee"],
                    row["callee_name"],
                    row["receiver"],
                    row["receiver_kind"],
                    row["line"],
                    row["enclosing_class"],
                    row["column"],
                )
                for row in conn.execute(
                    "SELECT path, caller, callee, callee_name, receiver, receiver_kind, line, enclosing_class, column "
                    "FROM calls ORDER BY path, line, column"
                )
            ]
            imports = [
                Import(
                    row["path"],
                    row["module"],
                    row["name"],
                    row["alias"],
                    row["kind"],
                    row["line"],
                    row["level"],
                    row["scope"],
                    row["column"],
                )
                for row in conn.execute(
                    "SELECT path, module, name, alias, kind, line, level, scope, column "
                    "FROM imports ORDER BY path, line, column"
                )
            ]
        return files, symbols, calls, imports

    def file_row(self, rel: str) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM files WHERE path = ?", (rel,)).fetchone()
            return dict(row) if row is not None else None

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._guard_default_path:
            _reject_symlink_escape(self.root, self.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._ensure_schema(conn)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        conn.executescript(
            "DROP TABLE IF EXISTS symbols; DROP TABLE IF EXISTS calls; "
            "DROP TABLE IF EXISTS imports; DROP TABLE IF EXISTS files;"
        )
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()

    def _refresh_file(
        self, conn: sqlite3.Connection, path: Path, rel: str, stats: dict[str, int]
    ) -> None:
        stat = path.stat()
        size = stat.st_size
        if size > MAX_FILE_BYTES:
            # The previous parse is for a smaller file. Leaving it would keep
            # deleted symbols after this file grows past the limit.
            _delete_path(conn, rel)
            stats["skipped"] += 1
            return
        row = conn.execute(
            "SELECT mtime_ns, size, content_hash FROM files WHERE path = ?", (rel,)
        ).fetchone()
        if row is not None and row["mtime_ns"] == stat.st_mtime_ns and row["size"] == size:
            stats["skipped"] += 1
            return
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if row is not None and row["content_hash"] == digest:
            conn.execute(
                "UPDATE files SET mtime_ns = ?, size = ? WHERE path = ?",
                (stat.st_mtime_ns, size, rel),
            )
            stats["unchanged_hash"] += 1
            stats["skipped"] += 1
            return
        facts = extract_path(rel, data)
        _replace_file(conn, rel, facts, stat.st_mtime_ns, size, digest)
        stats["parsed"] += 1


def _reject_symlink_escape(root: Path, db_path: Path) -> None:
    """Refuse a default index opened through any symlink.

    ``sqlite3.connect`` follows the link, and schema setup then drops every
    table when ``user_version`` differs. Even a link that stays inside the
    checkout can point at another database, so the default path must be a real
    directory chain and a real file; an explicit ``--index`` is the caller's
    choice and is not guarded.
    """
    root_resolved = root.resolve()
    current = db_path if db_path.is_absolute() else root_resolved / db_path
    try:
        relative = current.relative_to(root_resolved)
    except ValueError:
        raise ImpactError(
            "refusing to open the impact index through a symlink that leaves the repository"
        ) from None
    landed = root_resolved
    for part in relative.parts:
        landed = landed / part
        if landed.is_symlink():
            raise ImpactError(
                "refusing to open the impact index through a symlink; pass an explicit --index"
            )


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def iter_source_files(root: Path):
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [
            name
            for name in dirnames
            if name not in EXCLUDE_DIRS
            and not name.startswith(".")
            and not (current / name).is_symlink()
        ]
        for name in filenames:
            if name.startswith("."):
                continue
            path = current / name
            if path.is_symlink() or not path.is_file():
                continue
            if language_for_path(name) is None:
                continue
            yield path


def _replace_file(
    conn: sqlite3.Connection,
    rel: str,
    facts: FileFacts,
    mtime_ns: int,
    size: int,
    digest: str,
) -> None:
    _delete_path(conn, rel)
    conn.execute(
        "INSERT INTO files(path, language, mtime_ns, size, content_hash) VALUES (?, ?, ?, ?, ?)",
        (rel, facts.language, mtime_ns, size, digest),
    )
    conn.executemany(
        "INSERT INTO symbols(path, name, qualified_name, kind, start_line, end_line, parent, exported, start_column) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                item.path,
                item.name,
                item.qualified_name,
                item.kind,
                item.start_line,
                item.end_line,
                item.parent,
                item.exported,
                item.column,
            )
            for item in facts.symbols
        ],
    )
    conn.executemany(
        "INSERT INTO calls(path, caller, callee, callee_name, receiver, receiver_kind, line, enclosing_class, column) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                item.path,
                item.caller,
                item.callee,
                item.callee_name,
                item.receiver,
                item.receiver_kind,
                item.line,
                item.enclosing_class,
                item.column,
            )
            for item in facts.calls
        ],
    )
    conn.executemany(
        "INSERT INTO imports(path, module, name, alias, kind, line, level, scope, column) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                item.path,
                item.module,
                item.name,
                item.alias,
                item.kind,
                item.line,
                item.level,
                item.scope,
                item.column,
            )
            for item in facts.imports
        ],
    )


def _delete_path(conn: sqlite3.Connection, rel: str) -> None:
    for table in ("symbols", "calls", "imports", "files"):
        conn.execute(f"DELETE FROM {table} WHERE path = ?", (rel,))
