"""The drain flag: while it is set, dispatch starts no new Worker launch.

``cond drain`` writes the flag and ``cond undrain`` removes it. The dispatch
side (``src.tools.auto_dispatch.drain``) reads it before every launch and fix
round. Both sides import this module, so the path is decided in one place. It
is stdlib-only on purpose: the server imports it, and ``conductor_cli`` may not
import ``src.*`` (see ``tests/unit/conductor_cli/test_no_src_imports.py``).

The flag lives under ``CONDUCTOR_HOME`` because that is what ``cond`` passes
to the backend it starts, so the CLI and the backend of one checkout agree on
the file without any extra setting.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

DRAIN_FILE = "drain.json"


def conductor_home(env: Mapping[str, str] | None = None) -> Path:
    """``CONDUCTOR_HOME``, else ``~/.conductor``; the same rule as ``load_settings``."""
    env = os.environ if env is None else env
    raw = env.get("CONDUCTOR_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    home = Path(env.get("HOME", str(Path.home()))).expanduser()
    return home / ".conductor"


def drain_path(home: Path | None = None) -> Path:
    return (conductor_home() if home is None else home) / DRAIN_FILE


def read_drain(home: Path | None = None) -> dict[str, object] | None:
    """The flag's content, or ``None`` when not drained.

    A flag that exists but cannot be parsed still counts as drained: a
    half-written or hand-edited file must not let launches through. So does
    anything else at the path that is not a readable UTF-8 file (a dangling
    symlink, a directory, bytes that are not UTF-8): readers fail closed.
    """
    path = drain_path(home)
    unreadable: dict[str, object] = {"set_at": "", "reason": "unreadable drain flag"}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # A dangling symlink raises this too; only a missing path is "off".
        return unreadable if os.path.lexists(path) else None
    except (OSError, ValueError):
        # OSError: a directory, no permission. ValueError: not UTF-8.
        return unreadable
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict):
        return {"set_at": "", "reason": "unparsable drain flag"}
    return {
        "set_at": str(payload.get("set_at") or ""),
        "reason": str(payload.get("reason") or ""),
    }


def is_drained(home: Path | None = None) -> bool:
    return read_drain(home) is not None


def set_drain(
    home: Path | None = None, reason: str = "", *, now: datetime | None = None
) -> dict[str, object]:
    """Write the flag atomically (temp file + rename) and return its content."""
    path = drain_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "set_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "reason": " ".join(reason.split())[:200],
    }
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return dict(payload)


def clear_drain(home: Path | None = None) -> bool:
    """Remove the flag. True when one was removed."""
    try:
        drain_path(home).unlink()
    except FileNotFoundError:
        return False
    return True
