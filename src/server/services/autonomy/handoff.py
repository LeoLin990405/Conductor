"""Verified file handoffs between agents.

A producer registers a manifest (paths relative to the workspace root, sizes,
sha256 checksums) with the Ticket; the consumer re-hashes every file before
use. Any tampered or missing file rejects the whole handoff — a partial
handoff is never usable. Paths are confined to the workspace root and
secret-looking paths are refused before anything is stored.

All filesystem access goes through ``Path`` objects supplied by the caller, so
tests run against ``tmp_path`` and never a real workspace.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from src.server.database.autonomy import handoffs as handoff_db

# Reading in chunks keeps a large artifact from being slurped into memory.
_HASH_CHUNK = 64 * 1024

# Paths that must never appear in a manifest. A manifest is stored in plain
# JSONB and shown in the API, so anything credential-shaped is refused at
# registration rather than hidden later.
_SECRET_NAME_RE = re.compile(
    r"(^|[-_.])(tokens?|secrets?|passwords?|passwd|passphrase|credentials?|"
    r"api[-_]?keys?)($|[-_.])",
    re.IGNORECASE,
)
_SECRET_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".kdbx",
    ".env",
)
_IDENTITY_BASENAMES = {
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "authorized_keys",
    "known_hosts",
    ".netrc",
    ".htpasswd",
    "credentials",
    "credentials.json",
}
_FORBIDDEN_PARTS = {".git", "node_modules", "__pycache__", ".venv", "venv"}

# A manifest is metadata; caps keep one row from being a file dump.
MAX_FILES = 256
MAX_PURPOSE_CHARS = 500


class HandoffError(Exception):
    """Base error with a display-safe message (never file content)."""


class PathEscapeError(HandoffError):
    """A path is absolute, climbs out of the root, or leaves via a symlink."""


class SecretPathError(HandoffError):
    """A path looks like a credential and must not be named in a manifest."""


class MissingFileError(HandoffError):
    """A manifest path does not exist (or is not a regular file)."""


class ChecksumMismatchError(HandoffError):
    """A file's size or sha256 does not match the manifest."""


class ManifestError(HandoffError):
    """A manifest violates the shape or size caps."""


@dataclass(frozen=True, slots=True)
class HandoffFile:
    path: str
    size: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


def _normalize_rel(path: str) -> PurePosixPath:
    rel = (path or "").strip().replace("\\", "/")
    if not rel:
        raise PathEscapeError("empty path")
    pure = PurePosixPath(rel)
    if pure.is_absolute():
        raise PathEscapeError(f"absolute path {rel!r}")
    parts = pure.parts
    if any(part == ".." for part in parts):
        raise PathEscapeError(f"path escapes the workspace root: {rel!r}")
    if not parts:
        raise PathEscapeError("empty path")
    return PurePosixPath(*parts)


def secret_looking_path(rel: str | PurePosixPath) -> bool:
    """True when a relative path looks like a credential or a repo-internal file."""
    parts = rel.parts if isinstance(rel, PurePosixPath) else _safe_parts(rel)
    lowered = [part.lower() for part in parts]
    for part in lowered:
        if part in _FORBIDDEN_PARTS or part in _IDENTITY_BASENAMES:
            return True
        if part.endswith(_SECRET_SUFFIXES):
            return True
        if _SECRET_NAME_RE.search(part):
            return True
    return False


def _safe_parts(path: str) -> tuple[str, ...]:
    text = (path or "").strip().replace("\\", "/")
    return tuple(part for part in text.split("/") if part not in ("", "."))


def confine_path(root: Path, rel: str) -> Path:
    """Resolve ``root/rel`` and require the result to stay inside ``root``.

    Absolute paths and ``..`` segments are refused by shape; a symlink whose
    target resolves outside the root is refused by containment, so a manifest
    cannot name a file the workspace does not own.
    """
    pure = _normalize_rel(rel)
    if secret_looking_path(pure):
        raise SecretPathError(f"path looks like a secret: {str(pure)!r}")
    root_real = root.resolve()
    candidate = (root / Path(*pure.parts)).resolve()
    try:
        candidate.relative_to(root_real)
    except ValueError as exc:
        raise PathEscapeError(f"path escapes the workspace root: {rel!r}") from exc
    return candidate


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest_entry(root: Path, rel: str) -> dict[str, Any]:
    """Stat and hash one file into a manifest entry."""
    pure = _normalize_rel(rel)
    if secret_looking_path(pure):
        raise SecretPathError(f"path looks like a secret: {str(pure)!r}")
    # Check the link before resolving it: resolve() dereferences, so a symlink
    # that stays inside the root would otherwise be indistinguishable from the
    # file it points at. Manifests name real files, not aliases.
    joined = root / Path(*pure.parts)
    if joined.is_symlink():
        raise PathEscapeError(f"symlink in manifest path: {rel!r}")
    root_real = root.resolve()
    candidate = joined.resolve()
    try:
        candidate.relative_to(root_real)
    except ValueError as exc:
        raise PathEscapeError(f"path escapes the workspace root: {rel!r}") from exc
    if not candidate.exists() or candidate.is_dir():
        raise MissingFileError(f"not a file in the workspace: {rel!r}")
    return {
        "path": pure.as_posix(),
        "size": candidate.stat().st_size,
        "sha256": sha256_file(candidate),
    }


def _validate_entry(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ManifestError("each file entry must be an object")
    path = entry.get("path")
    size = entry.get("size")
    sha = entry.get("sha256")
    if not isinstance(path, str) or not path.strip():
        raise ManifestError("file entry needs a path")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ManifestError(f"file entry {path!r} needs a non-negative integer size")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ManifestError(f"file entry {path!r} needs a lowercase hex sha256")
    # Re-check shape here: a handoff handed to the API is untrusted input.
    pure = _normalize_rel(path)
    if secret_looking_path(pure):
        raise SecretPathError(f"path looks like a secret: {path!r}")
    return {"path": pure.as_posix(), "size": size, "sha256": sha}


def validate_manifest(
    files: list[dict[str, Any]] | None,
    *,
    purpose: str = "",
    producer: str = "",
    consumer: str = "",
) -> list[dict[str, Any]]:
    """Validate a supplied manifest shape; returns normalized entries."""
    if files is None or not isinstance(files, list) or not files:
        raise ManifestError("a handoff needs at least one file entry")
    if len(files) > MAX_FILES:
        raise ManifestError(f"a handoff is capped at {MAX_FILES} files")
    if len(purpose or "") > MAX_PURPOSE_CHARS:
        raise ManifestError(f"purpose is capped at {MAX_PURPOSE_CHARS} characters")
    if not (producer or "").strip() or not (consumer or "").strip():
        raise ManifestError("a handoff needs a producer and a consumer")
    entries = [_validate_entry(entry) for entry in files]
    paths = [entry["path"] for entry in entries]
    if len(set(paths)) != len(paths):
        raise ManifestError("duplicate path in manifest")
    return entries


async def register_handoff(
    root: Path,
    *,
    ticket_id: str,
    user_id: str,
    producer: str,
    consumer: str,
    purpose: str,
    paths: list[str],
) -> dict[str, Any]:
    """Build a manifest from the workspace and store it as one handoff row.

    The manifest is all-or-nothing: any unconfinable, secret-looking, missing
    or non-regular path refuses the whole handoff and nothing is stored.
    """
    if not paths:
        raise ManifestError("a handoff needs at least one file path")
    if len(paths) > MAX_FILES:
        raise ManifestError(f"a handoff is capped at {MAX_FILES} files")
    if len(purpose or "") > MAX_PURPOSE_CHARS:
        raise ManifestError(f"purpose is capped at {MAX_PURPOSE_CHARS} characters")
    if not producer.strip() or not consumer.strip():
        raise ManifestError("a handoff needs a producer and a consumer")
    entries = [build_manifest_entry(root, rel) for rel in paths]
    normalized = validate_manifest(
        entries, purpose=purpose, producer=producer, consumer=consumer
    )
    return await handoff_db.create_handoff(
        ticket_id=ticket_id,
        user_id=user_id,
        producer=producer.strip(),
        consumer=consumer.strip(),
        purpose=purpose.strip(),
        files=normalized,
    )


async def verify_handoff(root: Path, handoff: dict[str, Any]) -> dict[str, Any]:
    """Re-hash every manifest file and settle the handoff row.

    The row settles exactly once (``registered`` → ``verified``/``rejected``),
    so a retry or a stale verify racing a newer one is a no-op that simply
    returns the settled row.
    """
    handoff_id = str(handoff.get("handoff_id") or "")
    status = str(handoff.get("status") or "")
    if not handoff_id:
        raise ManifestError("handoff row has no id")
    if status != "registered":
        return handoff
    error = ""
    outcome = "verified"
    try:
        entries = validate_manifest(
            list(handoff.get("files") or []),
            purpose=str(handoff.get("purpose") or ""),
            producer=str(handoff.get("producer") or ""),
            consumer=str(handoff.get("consumer") or ""),
        )
        for entry in entries:
            pure = _normalize_rel(entry["path"])
            joined = root / Path(*pure.parts)
            if joined.is_symlink():
                raise MissingFileError(
                    f"not a file in the workspace: {entry['path']!r}"
                )
            target = confine_path(root, entry["path"])
            if not target.exists() or target.is_dir():
                raise MissingFileError(
                    f"not a file in the workspace: {entry['path']!r}"
                )
            if target.stat().st_size != entry["size"]:
                raise ChecksumMismatchError(f"size mismatch for {entry['path']!r}")
            if sha256_file(target) != entry["sha256"]:
                raise ChecksumMismatchError(f"sha256 mismatch for {entry['path']!r}")
    except HandoffError as exc:
        outcome = "rejected"
        error = str(exc)
    settled = await handoff_db.settle_handoff(
        handoff_id, status=outcome, verify_error=error
    )
    return settled if settled is not None else handoff
