"""Persisted Super-agent TUI sessions.

``cond --continue`` reopens the most recent session and ``/resume`` lists the
others. A session is one JSON file under ``$CONDUCTOR_HOME/superagent-sessions``
(override with ``CONDUCTOR_SUPERAGENT_SESSIONS``); it holds the conversation
transcript, the Home workspace, the chosen daemon, the router model shown in
the status bar, and the last Ticket so a restart can reattach to it.

Writes are atomic (temp file + rename) and private (0700 directory, 0600
file); reads tolerate a corrupt or half-written session instead of failing the
whole TUI. The store is never given a user-supplied path segment: ``load``
validates the id shape, so a session id cannot escape the directory.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SESSION_VERSION = 1
MAX_SESSIONS = 50
_SESSION_ID = re.compile(r"^[0-9a-f]{8,32}$")
_TITLE_LIMIT = 120
_ATTACHMENT_LIMIT = 40


def default_session_root() -> Path:
    """Where TUI sessions live; env-overridable for tests and isolation."""
    override = os.environ.get("CONDUCTOR_SUPERAGENT_SESSIONS")
    if override:
        return Path(override).expanduser()
    home = Path(os.environ.get("CONDUCTOR_HOME", str(Path.home() / ".conductor")))
    return home.expanduser() / "superagent-sessions"


@dataclass
class SessionRecord:
    """One resumable TUI session."""

    session_id: str
    title: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    workspace_id: str = ""
    daemon_id: str = ""
    daemon_label: str = ""
    last_ticket_id: str = ""
    router_model: str = ""
    transcript: list[dict[str, Any]] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)
    version: int = SESSION_VERSION

    @property
    def short_id(self) -> str:
        return self.session_id[:8]

    def touch(self, *, now: float | None = None) -> None:
        self.updated_at = time.time() if now is None else now

    def to_json(self) -> dict[str, Any]:
        return {
            "version": SESSION_VERSION,
            "session_id": self.session_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "workspace_id": self.workspace_id,
            "daemon_id": self.daemon_id,
            "daemon_label": self.daemon_label,
            "last_ticket_id": self.last_ticket_id,
            "router_model": self.router_model,
            "transcript": self.transcript,
            "attachments": self.attachments,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "SessionRecord":
        transcript = raw.get("transcript")
        attachments = raw.get("attachments")
        rows = transcript if isinstance(transcript, list) else []
        files = attachments if isinstance(attachments, list) else []
        return cls(
            session_id=str(raw.get("session_id") or ""),
            title=str(raw.get("title") or "")[:_TITLE_LIMIT],
            created_at=_as_float(raw.get("created_at")),
            updated_at=_as_float(raw.get("updated_at")),
            workspace_id=str(raw.get("workspace_id") or ""),
            daemon_id=str(raw.get("daemon_id") or ""),
            daemon_label=str(raw.get("daemon_label") or ""),
            last_ticket_id=str(raw.get("last_ticket_id") or ""),
            router_model=str(raw.get("router_model") or ""),
            transcript=[item for item in rows if isinstance(item, dict)],
            attachments=[str(item) for item in files if str(item)][:_ATTACHMENT_LIMIT],
            version=SESSION_VERSION,
        )


def _as_float(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) and number >= 0 else 0.0


def new_session_id() -> str:
    return uuid.uuid4().hex[:16]


class SessionStore:
    """Read/write session files under one private directory."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_session_root()).expanduser()

    # -- paths / permissions ----------------------------------------------

    def _ensure_root(self) -> None:
        # A symlinked session directory would redirect private transcripts to
        # another location; require a real directory (or create one).
        if self.root.is_symlink():
            raise OSError(f"refusing a symlinked session directory: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise OSError(f"session path is not a directory: {self.root}")
        try:
            self.root.chmod(0o700)
        except OSError:
            pass

    def path_for(self, session_id: str) -> Path:
        if not _SESSION_ID.match(session_id):
            raise ValueError(f"invalid session id: {session_id!r}")
        return self.root / f"{session_id}.json"

    # -- lifecycle ---------------------------------------------------------

    def new(self, *, title: str = "") -> SessionRecord:
        now = time.time()
        record = SessionRecord(
            session_id=new_session_id(),
            title=(title or "super-agent")[:_TITLE_LIMIT],
            created_at=now,
            updated_at=now,
        )
        return record

    def save(self, record: SessionRecord) -> Path:
        self._ensure_root()
        record.touch()
        record.title = record.title[:_TITLE_LIMIT]
        target = self.path_for(record.session_id)
        if target.is_symlink():
            raise OSError(f"refusing to write through a symlinked session file: {target}")
        payload = json.dumps(record.to_json(), ensure_ascii=False, indent=2)
        # mkstemp creates a fresh O_EXCL file: a predictable temp name in a
        # shared directory would be an easy symlink target to truncate.
        fd, temp_name = tempfile.mkstemp(
            dir=str(self.root), prefix=f".{record.session_id}-", suffix=".tmp"
        )
        temp = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
            os.replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)
        self._prune()
        return target

    def load(self, session_id: str) -> SessionRecord | None:
        if self.root.is_symlink():
            # Reading through a symlinked root would use transcripts from a
            # directory this store did not create.
            return None
        try:
            target = self.path_for(session_id)
        except ValueError:
            return None
        if target.is_symlink() or not target.is_file():
            return None
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        if "transcript" in raw and not isinstance(raw["transcript"], list):
            return None
        if "attachments" in raw and not isinstance(raw["attachments"], list):
            return None
        try:
            record = SessionRecord.from_json(raw)
        except (TypeError, ValueError, KeyError):
            # A shape this version cannot read is a skipped session, not a
            # crash in the resume path.
            return None
        # The filename is the authoritative id: an embedded id that does not
        # match it (or is not a valid id at all) must not make saving crash.
        record.session_id = session_id
        return record

    def list_sessions(self) -> list[SessionRecord]:
        """Every readable session, newest first (a corrupt file is skipped)."""
        if not self.root.is_dir():
            return []
        records: list[SessionRecord] = []
        for path in self.root.glob("*.json"):
            record = self.load(path.stem)
            if record is not None:
                records.append(record)
        records.sort(key=lambda item: item.updated_at, reverse=True)
        return records

    def latest(self) -> SessionRecord | None:
        sessions = self.list_sessions()
        return sessions[0] if sessions else None

    def delete(self, session_id: str) -> bool:
        try:
            target = self.path_for(session_id)
        except ValueError:
            return False
        if target.is_file():
            target.unlink(missing_ok=True)
            return True
        return False

    def _prune(self) -> None:
        sessions = self.list_sessions()
        for record in sessions[MAX_SESSIONS:]:
            self.delete(record.session_id)


__all__ = [
    "MAX_SESSIONS",
    "SESSION_VERSION",
    "SessionRecord",
    "SessionStore",
    "default_session_root",
    "new_session_id",
]
