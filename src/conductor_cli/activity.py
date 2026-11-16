"""Busy markers: the dispatch work a deploy must wait for besides Worker spawns.

``cond restart --drain`` stops new launches, but a G2 run, a reviewer CLI or a
merge that is already under way would be cut off by the restart. Each of those
writes a marker file under ``$CONDUCTOR_HOME/busy/`` while it runs and removes
it when it ends, and ``restart --drain`` waits until no live marker is left.

Files, not an in-process counter: the backend runs ``--workers N``, so a
request asking "are you busy?" lands on one process and cannot see the others.
A marker whose process has died (a crash, a ``kill -9``) is stale and is
ignored and removed by the reader. So is one whose pid now belongs to another
process: the marker records the process start time, and a live pid that
started at a different time is a reused pid, not the marker's writer.

A marker is written to a dot-named temp file and renamed into place, so the
reader never sees a half-written one. A marker that still cannot be parsed
(an older writer, a damaged disk) counts as busy while it is fresh and is
removed once it is older than :data:`UNPARSABLE_GRACE_SECONDS`, so one
leaked file cannot block ``restart --drain`` for ever.

Stdlib only, for the same reason as :mod:`.drain`: the server imports it and
``conductor_cli`` may not import ``src.*``.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from .drain import conductor_home

BUSY_DIR = "busy"

#: The kinds of work a marker names.
G2 = "g2"
G5 = "g5"
REVIEW = "review"
MERGE = "merge"
LAUNCH = "launch"

#: An unparsable marker younger than this is still a sign of work.
UNPARSABLE_GRACE_SECONDS = 60.0

#: ``pid -> start time`` (an opaque token), ``None`` when it cannot be read.
StartTime = Callable[[int], "str | None"]

_TEMP_PREFIX = "."
_TEMP_SUFFIX = ".tmp"


def busy_dir(home: Path | None = None) -> Path:
    return (conductor_home() if home is None else home) / BUSY_DIR


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_start_time(pid: int) -> str | None:
    """When ``pid`` started, as a token that differs for a reused pid.

    Linux: the ``starttime`` field of ``/proc/<pid>/stat`` (clock ticks since
    boot). Elsewhere (macOS): ``ps -o lstart=``. ``None`` when neither can be
    read; the caller then falls back to the pid check alone.
    """
    if pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        try:
            text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
        except OSError:
            return None
        # The command name (field 2) may hold spaces and parentheses.
        fields = text.rpartition(")")[2].split()
        # fields[0] is field 3 (state); starttime is field 22.
        return fields[19] if len(fields) > 19 else None
    try:
        done = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            env={"PATH": "/bin:/usr/bin", "LC_ALL": "C", "LANG": "C"},
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = " ".join(done.stdout.split())
    return value if done.returncode == 0 and value else None


_OWN_START: dict[int, str | None] = {}


def _own_start_time(start_time: StartTime) -> str | None:
    pid = os.getpid()
    if start_time is not process_start_time:
        return start_time(pid)
    # Keyed on the pid so a forked child reads its own.
    if pid not in _OWN_START:
        _OWN_START[pid] = process_start_time(pid)
    return _OWN_START[pid]


def _write_marker(directory: Path, payload: dict[str, object]) -> Path:
    """Write the marker atomically: temp file, then rename into place.

    The temp file is dot-named and ends in ``.tmp``, so the reader's
    ``*.json`` scan never picks it up. On a failure nothing is left behind.
    """
    name = f"{os.getpid()}-{uuid.uuid4().hex}.json"
    final = directory / name
    temp = directory / f"{_TEMP_PREFIX}{name}{_TEMP_SUFFIX}"
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
        os.replace(temp, final)
    except BaseException:
        for leftover in (temp, final):
            with contextlib.suppress(OSError):
                leftover.unlink()
        raise
    return final


@contextlib.contextmanager
def busy(
    kind: str,
    detail: str = "",
    *,
    home: Path | None = None,
    start_time: StartTime = process_start_time,
) -> Iterator[Path | None]:
    """Hold a marker for ``kind`` while the block runs.

    A marker that cannot be written never stops the work it describes: the
    deploy loses one signal, the dispatch keeps running. The marker is
    removed on every exit path.
    """
    path: Path | None = None
    try:
        directory = busy_dir(home)
        directory.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {
            "kind": kind,
            "detail": " ".join(str(detail).split())[:200],
            "pid": os.getpid(),
            "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        own = _own_start_time(start_time)
        if own:
            payload["pid_start"] = own
        path = _write_marker(directory, payload)
    except (OSError, ValueError):
        path = None
    try:
        yield path
    finally:
        if path is not None:
            with contextlib.suppress(OSError):
                path.unlink()


def _age_seconds(path: Path, now: float) -> float | None:
    try:
        return now - path.stat().st_mtime
    except OSError:
        return None


def _stale(payload: dict[str, object], start_time: StartTime) -> bool:
    """The writer is gone: its pid is dead, or now names another process."""
    pid = payload.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or not _alive(pid):
        return True
    recorded = payload.get("pid_start")
    if not isinstance(recorded, str) or not recorded:
        return False  # an older marker: the pid check is all there is
    current = _own_start_time(start_time) if pid == os.getpid() else start_time(pid)
    # Unknown now: keep it (it may be real work); known and different: reused.
    return current is not None and current != recorded


def busy_entries(
    home: Path | None = None,
    *,
    prune: bool = True,
    start_time: StartTime = process_start_time,
    now: float | None = None,
) -> list[dict[str, object]]:
    """The live markers, oldest first. Stale ones are dropped (and removed).

    Each entry names its marker ``path`` so an operator can see (and, if it
    is a leak, remove) the file that holds a deploy.
    """
    directory = busy_dir(home)
    try:
        names = sorted(directory.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError:
        return [
            {
                "kind": "unknown",
                "detail": "busy directory unreadable",
                "pid": -1,
                "path": str(directory),
            }
        ]
    clock = time.time() if now is None else now
    entries: list[dict[str, object]] = []
    for path in names:
        if path.suffix != ".json" or path.name.startswith(_TEMP_PREFIX):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue  # it ended while we looked
        except (OSError, ValueError):
            payload = None
        if not isinstance(payload, dict):
            age = _age_seconds(path, clock)
            if prune and age is not None and age > UNPARSABLE_GRACE_SECONDS:
                # Writes are atomic, so this is a leak, not a write in flight.
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            # A fresh damaged marker: still a sign of work.
            entries.append(
                {"kind": "unknown", "detail": path.name, "pid": -1, "path": str(path)}
            )
            continue
        if _stale(payload, start_time):
            if prune:
                with contextlib.suppress(OSError):
                    path.unlink()
            continue
        entries.append(
            {
                "kind": str(payload.get("kind") or "unknown"),
                "detail": str(payload.get("detail") or ""),
                "pid": payload["pid"],
                "started_at": str(payload.get("started_at") or ""),
                "path": str(path),
            }
        )
    return entries


def count_entries(entries: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        kind = str(entry.get("kind"))
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def busy_counts(home: Path | None = None) -> dict[str, int]:
    """``{kind: live markers}``; empty when idle."""
    return count_entries(busy_entries(home))

