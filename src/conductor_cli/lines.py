"""``conductor lines`` — worktrees, branches, model and state."""

from __future__ import annotations

import subprocess
from collections import deque
from pathlib import Path
from typing import Any

from .client import CLIError


def worktree_root() -> Path:
    """Root that holds one worktree per line.

    ``CONDUCTOR_WORKTREE_ROOT`` wins. ``CONDUCTOR_WT_ROOT`` remains the legacy alias.
    """
    from .worktree import conductor_worktree_root

    # Relative roots are resolved once here; children inherit an absolute
    # path and cannot accidentally resolve it a second time against
    # themselves (fleet -> fleet/fleet).
    return Path(conductor_worktree_root()).expanduser().resolve()


def _git(path: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise CLIError(f"Could not run git for {path}: {exc}") from exc
    if result.returncode:
        raise CLIError(
            f"git {' '.join(args)} failed in {path} (exit {result.returncode})"
        )
    return result.stdout.strip()


def _is_worktree(path: Path) -> bool:
    return path.is_dir() and (path / ".git").exists()


def _worktrees(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        (child for child in root.iterdir() if _is_worktree(child)),
        key=lambda child: child.name,
    )


def _branch(path: Path) -> str:
    current = _git(path, "branch", "--show-current")
    if current:
        return current
    return _git(path, "rev-parse", "--short", "HEAD")


def _state(path: Path) -> str:
    return "dirty" if _git(path, "status", "--porcelain") else "clean"


def _newest_log(root: Path, name: str) -> Path | None:
    """Newest ``logs/<model>--<line>[--<prompt>].log`` for this line, if any."""
    logs_dir = root / "logs"
    if not logs_dir.is_dir():
        return None
    newest: Path | None = None
    newest_mtime = -1.0
    try:
        candidates = list(logs_dir.iterdir())
    except OSError:
        return None
    for candidate in candidates:
        if not candidate.is_file() or candidate.suffix != ".log":
            continue
        parts = candidate.stem.split("--")
        if len(parts) < 2 or parts[1] != name:
            continue
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        if mtime > newest_mtime:
            newest, newest_mtime = candidate, mtime
    return newest


def _model(root: Path, name: str) -> str | None:
    log = _newest_log(root, name)
    if log is None:
        return None
    parts = log.stem.split("--")
    return parts[0] or None


def _ahead(root: Path, path: Path) -> int | None:
    try:
        return int(_git(path, "rev-list", "--count", "integrate/r2..HEAD"))
    except CLIError:
        return None


def require_line(root: Path, name: str) -> Path:
    """Resolve a line name to a worktree directly under ``root``.

    Absolute paths, separators and ``..`` are rejected so a caller can never
    leave the fleet root (merge runs tests and ``git merge`` at this path).
    """
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\0" in name
        or Path(name).is_absolute()
    ):
        available = ", ".join(p.name for p in _worktrees(root)) or "(none)"
        raise CLIError(f"Unknown line {name!r}. Available: {available}")
    path = root / name
    try:
        if path.resolve().parent != root.resolve():
            available = ", ".join(p.name for p in _worktrees(root)) or "(none)"
            raise CLIError(f"Unknown line {name!r}. Available: {available}")
    except OSError:
        pass
    if not _is_worktree(path):
        available = ", ".join(p.name for p in _worktrees(root)) or "(none)"
        raise CLIError(f"Unknown line {name!r}. Available: {available}")
    return path


def _entry(root: Path, path: Path) -> dict[str, Any]:
    return {
        "name": path.name,
        "branch": _branch(path),
        "model": _model(root, path.name),
        "state": _state(path),
    }


def handle_list(args: Any, _client: Any = None) -> dict[str, Any]:
    del args
    root = worktree_root()
    return {"root": str(root), "lines": [_entry(root, path) for path in _worktrees(root)]}


def handle_show(args: Any, _client: Any = None) -> dict[str, Any]:
    root = worktree_root()
    path = require_line(root, args.line)
    log = _newest_log(root, path.name)
    return {
        **_entry(root, path),
        "path": str(path),
        "head": _git(path, "rev-parse", "HEAD"),
        "ahead_of_integrate_r2": _ahead(root, path),
        "last_log": str(log) if log else None,
    }


def handle_logs(args: Any, _client: Any = None) -> dict[str, Any]:
    root = worktree_root()
    require_line(root, args.line)
    log = _newest_log(root, args.line)
    if log is None:
        raise CLIError(f"No logs found for line {args.line!r} under {root / 'logs'}")
    if args.lines < 0:
        raise CLIError("--lines must be zero or greater")
    try:
        if args.lines == 0:
            tail: list[str] = []
        else:
            with log.open(encoding="utf-8", errors="replace") as stream:
                tail = list(deque(stream, maxlen=args.lines))
    except OSError as exc:
        raise CLIError(f"Could not read log file {log}: {exc}") from exc
    return {"file": str(log), "lines": tail}
