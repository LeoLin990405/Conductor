"""Host-side workspace materialization for native CLI threads.

The Conductor sandbox and a native CLI do not share a filesystem. A native thread
therefore needs an explicit bridge:

* first turn (private cwd): copy the workspace manifest into the CLI cwd;
* after each turn: walk the CLI cwd and upsert changed/new files into the
  manifest, deleting rows the CLI removed.

The bridge is deliberately limited to regular files. Symlinks are skipped
because a CLI cwd must not become a way to read outside the workspace.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from src.server.database.workspace_file import get_files_for_workspace
from src.server.database.workspace_file import (
    bulk_upsert_files,
    delete_file_rows,
    get_file_metadata_for_sync,
    path_fits_manifest,
)
from src.server.services.persistence.resolve import resolve_file_bytes

# Directories whose contents are runtime state, not user deliverables. The
# workspace manifest normally excludes them; a native cwd walk must too.
_SKIP_TOP_LEVEL = frozenset(
    {
        ".agents",
        ".system",
        "_internal",
        "mcp_servers",
        "node_modules",
        "__pycache__",
        # Per-CLI runtime caches; they are not user deliverables.
        ".mimocode",
        ".codex",
        ".claude",
        ".zcode",
    }
)
_SKIP_ANYWHERE = frozenset(
    {".git", ".svn", ".hg", ".venv", "venv", "node_modules", "__pycache__"}
)
_SKIP_ROOT_FILES = frozenset({".mcp.json"})
_MAX_FILE_BYTES = 20 * 1024 * 1024


def _safe_rel_path(raw: str) -> str | None:
    """A manifest path safe to join under cwd, or None for an unsafe one."""
    raw_text = str(raw or "").replace("\\", "/")
    if raw_text.startswith("/") or (len(raw_text) > 1 and raw_text[1] == ":"):
        return None
    normalized = raw_text
    path = PurePosixPath(normalized)
    if not normalized or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return str(path)


def _skip_relative(rel: PurePosixPath) -> bool:
    if not rel.parts:
        return True
    if rel.parts[0] in _SKIP_TOP_LEVEL:
        return True
    if len(rel.parts) == 1 and rel.parts[0] in _SKIP_ROOT_FILES:
        return True
    return any(part in _SKIP_ANYWHERE for part in rel.parts)


def _is_binary(data: bytes) -> bool:
    if b"\x00" in data[:8192]:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _file_record(rel: str, path: Path, data: bytes) -> dict[str, Any]:
    digest = hashlib.sha256(data).hexdigest()
    binary = _is_binary(data)
    mime, _ = mimetypes.guess_type(rel)
    stat = path.stat()
    text = None if binary else data.decode("utf-8")
    # Keep the row within the manifest's normal inline shape; larger binaries
    # are skipped by the walk rather than loaded through Postgres.
    return {
        "file_path": rel,
        "file_name": path.name,
        "file_size": len(data),
        "content_hash": digest,
        "content_text": text,
        "content_binary": data if binary else None,
        "mime_type": mime,
        "is_binary": binary,
        "permissions": oct(stat.st_mode & 0o777),
        "sandbox_modified_at": datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ),
        "kind": "file",
    }


def _write_manifest_files(files: list[dict[str, Any]], cwd: Path, user_id: str) -> int:
    """Write inline bytes; return files requiring async blob resolution."""
    # This function is sync so the few inline files do not need a task hop.
    written = 0
    for row in files:
        rel = _safe_rel_path(row.get("file_path", ""))
        if rel is None or row.get("kind") != "file":
            continue
        if _skip_relative(PurePosixPath(rel)):
            continue
        data = row.get("content_binary")
        if data is None and row.get("content_text") is not None:
            data = str(row["content_text"]).encode("utf-8")
        if data is None:
            continue
        target = cwd / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(data))
        written += 1
    return written


async def materialize_workspace(
    *, workspace_id: str, cwd: str | Path, user_id: str
) -> int:
    """Copy the current workspace manifest into a native CLI cwd.

    Only called for the private default cwd on a first native turn. Explicit
    ``native_cwd`` paths are treated as the user's own working tree and are not
    overwritten from a stale manifest.
    """
    root = Path(cwd)
    root.mkdir(parents=True, exist_ok=True)
    rows = await get_files_for_workspace(
        workspace_id, include_content=True, all_kinds=True
    )
    inline = []
    blob_rows = []
    for row in rows:
        if row.get("kind") != "file":
            continue
        rel = _safe_rel_path(row.get("file_path", ""))
        if rel is None or _skip_relative(PurePosixPath(rel)):
            continue
        if (
            row.get("content_text") is not None
            or row.get("content_binary") is not None
        ):
            inline.append(row)
        else:
            blob_rows.append(row)

    written = await asyncio.to_thread(_write_manifest_files, inline, root, user_id)
    for row in blob_rows:
        rel = _safe_rel_path(row.get("file_path", ""))
        if rel is None or _skip_relative(PurePosixPath(rel)):
            continue
        try:
            data = await resolve_file_bytes(row, user_id=user_id)
        except Exception:
            # A cold blob must not block the native turn; the file will be
            # absent from this cwd and can be materialized after storage
            # recovers.
            continue
        if data is None or len(data) > _MAX_FILE_BYTES:
            continue
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, data)
        written += 1
    return written


def _walk_files(root: Path) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        rel_dir = PurePosixPath(base.relative_to(root).as_posix())
        dirnames[:] = [d for d in dirnames if not _skip_relative(rel_dir / d)]
        for name in filenames:
            path = base / name
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            found.append((rel, path))
    return found


def collect_changed_files(
    root: Path, previous: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Compare a cwd tree with the manifest: (upserts, removed paths)."""
    upserts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rel, path in _walk_files(root):
        if _skip_relative(PurePosixPath(rel)) or not path_fits_manifest(rel):
            continue
        try:
            size = path.stat().st_size
            if size > _MAX_FILE_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        seen.add(rel)
        digest = hashlib.sha256(data).hexdigest()
        prior = previous.get(rel)
        if prior is not None and prior.get("content_hash") == digest:
            continue
        upserts.append(_file_record(rel, path, data))
    removed = [p for p in previous if p not in seen and not _skip_relative(PurePosixPath(p))]
    return upserts, removed


async def sync_workspace_after_turn(
    *, workspace_id: str, cwd: str | Path
) -> tuple[int, int]:
    """Upsert files a native CLI changed and remove rows it deleted."""
    root = Path(cwd)
    if not root.is_dir():
        return 0, 0
    previous = await get_file_metadata_for_sync(workspace_id)
    upserts, removed = await asyncio.to_thread(
        collect_changed_files, root, previous
    )
    if upserts:
        await bulk_upsert_files(workspace_id, upserts)
    if removed:
        await delete_file_rows(workspace_id, removed)
    return len(upserts), len(removed)
