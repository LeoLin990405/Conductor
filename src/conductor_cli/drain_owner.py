"""Cond-owned drain flags: who set the fence, and whether it is still alive.

``cond down`` / ``stop`` / ``restart`` / ``upgrade`` set the drain flag while
they stop the backend and clear it again on every exit path they can see. A
SIGKILL is not one of them: the flag then stays set and dispatch holds every
launch until someone runs ``cond undrain`` (N35, review finding 9).

So cond writes its fence with the owning pid and, when it can read it, that
process's start time (a pid can be reused). A later reader -- any ``cond``
command, the dispatch-side drain guard -- removes a cond-owned flag whose
owner is gone. A flag without an owner pid (``cond drain``, a hand-written
file, an older cond) or with a reason cond does not write is never removed.

Stdlib-only, like ``drain``: the server imports it and ``conductor_cli`` may
not import ``src.*``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .drain import drain_path, is_drained

#: The reasons cond itself writes (``ops_cli.STOP_FENCE_REASON`` and
#: ``restart --drain``). Only these are ever removed automatically.
_COND_REASON = re.compile(r"^cond (?:stop|restart|upgrade): stop requested$")
_COND_REASONS = frozenset({"cond restart --drain"})

PidAlive = Callable[[int], bool]
StartOf = Callable[[int], str | None]


def is_cond_reason(reason: str) -> bool:
    return reason in _COND_REASONS or bool(_COND_REASON.match(reason))


def pid_alive(pid: int) -> bool:
    """True when ``pid`` names a process (also one we may not signal)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Unknown: keep the fence rather than release launches.
        return True
    return True


def process_start(pid: int) -> str | None:
    """A token for ``pid``'s start time, or None when it cannot be read.

    Linux reads ``/proc``; elsewhere one ``ps`` call. It is only asked while
    the owner pid is alive, so the common dead-owner check stays a ``kill 0``.
    """
    if pid <= 0:
        return None
    stat = Path(f"/proc/{pid}/stat")
    if sys.platform.startswith("linux"):
        try:
            text = stat.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        # Field 22 (starttime) counts after the ``(comm)`` field.
        fields = text.rsplit(")", 1)[-1].split()
        return fields[19] if len(fields) > 19 else None
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    token = " ".join(out.stdout.split())
    return token or None


def set_owned_drain(
    home: Path | None,
    reason: str,
    *,
    pid: int | None = None,
    start_of: StartOf | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Write the drain flag with this process as its owner (atomic rename).

    ``read_drain`` ignores the extra keys, so every reader still sees a
    plain drain with ``set_at`` and ``reason``.
    """
    owner = os.getpid() if pid is None else int(pid)
    path = drain_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "set_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "reason": " ".join(reason.split())[:200],
        "owner_pid": owner,
        "owner_start": (start_of or process_start)(owner) or "",
    }
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return payload


def _owner(payload: object) -> tuple[int, str] | None:
    """(pid, start token) of a cond-owned flag; None for any other flag."""
    if not isinstance(payload, dict):
        return None
    if not is_cond_reason(str(payload.get("reason") or "")):
        return None
    raw = payload.get("owner_pid")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return None
    return raw, str(payload.get("owner_start") or "")


def owner_gone(
    pid: int,
    start: str,
    *,
    alive: PidAlive | None = None,
    start_of: StartOf | None = None,
) -> bool:
    """True when the owner is dead, or its pid now names another process."""
    alive = alive or pid_alive
    start_of = start_of or process_start
    if not alive(pid):
        return True
    if not start:
        return False
    now = start_of(pid)
    return now is not None and now != start


def clear_stale_fence(
    home: Path | None = None,
    *,
    alive: PidAlive | None = None,
    start_of: StartOf | None = None,
) -> dict[str, object] | None:
    """Remove a cond-owned flag whose owner is gone; return what was removed.

    The file is read again just before the unlink and left alone when it
    changed meanwhile (a new fence, an operator's ``cond drain``).
    """
    path = drain_path(home)
    try:
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text)
    except (OSError, ValueError):
        return None
    owner = _owner(payload)
    if owner is None or not owner_gone(*owner, alive=alive, start_of=start_of):
        return None
    try:
        if path.read_text(encoding="utf-8") != text:
            return None
        path.unlink()
    except OSError:
        return None
    return payload


def drained_live(
    home: Path | None = None,
    *,
    alive: PidAlive | None = None,
    start_of: StartOf | None = None,
) -> bool:
    """``is_drained``, with a dead owner's fence removed (counts as clear)."""
    clear_stale_fence(home, alive=alive, start_of=start_of)
    return is_drained(home)
