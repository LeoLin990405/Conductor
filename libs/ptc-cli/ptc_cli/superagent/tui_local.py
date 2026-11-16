"""Local side-effects the Super-agent TUI can run after permission.

The TUI is a thin client of the autonomy API, but a few commands are local on
purpose: ``/review`` and ``/merge`` shell out to the operator CLI, and a
composer line starting with ``!`` runs a shell command. Both go through here so
the command construction and the environment (``CONDUCTOR_WT_ROOT``) are in one
place, and so a test can replace the runner without a subprocess.

Nothing in this module decides *whether* an action may run; the permission gate
in :mod:`ptc_cli.superagent.tui_permissions` does that before calling in.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from .client import SuperAgentError
from .text import plain

DEFAULT_TIMEOUT_S = 3600.0
SHELL_TIMEOUT_S = 600.0
# Bounded output: a runaway command must not grow the TUI transcript without
# limit (the child still writes to its own pipes; the parent keeps the tail).
MAX_OUTPUT_CHARS = 40_000

Runner = Callable[..., subprocess.CompletedProcess[str]]


def worktree_root(explicit: Path | str | None = None) -> Path:
    """The fleet root the local CLI commands operate in."""
    value = explicit or os.environ.get(
        "CONDUCTOR_WT_ROOT", str(Path.home() / "conductor-wt")
    )
    return Path(value).expanduser().resolve()


def _tail(value: str) -> str:
    text = plain(value).rstrip("\n")
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return "… earlier output truncated …\n" + text[-MAX_OUTPUT_CHARS:]


def run_conductor(
    args: list[str],
    *,
    base_url: str,
    worktree_root_path: Path | str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    runner: Runner = subprocess.run,
) -> str:
    """Run ``python -m conductor_cli --url BASE <args>`` locally.

    Non-zero exit becomes a :class:`SuperAgentError` carrying the child's last
    words, so the TUI shows a message instead of a traceback.
    """
    root = worktree_root(worktree_root_path)
    command = [sys.executable, "-m", "conductor_cli", "--url", base_url, *args]
    child_env = os.environ.copy()
    child_env["CONDUCTOR_WT_ROOT"] = str(root)
    try:
        completed = runner(
            command,
            cwd=str(root) if root.is_dir() else None,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SuperAgentError(
            f"local command timed out after {int(timeout)}s: {' '.join(args)}"
        ) from exc
    except OSError as exc:
        raise SuperAgentError(f"local command could not start: {exc}") from exc
    if completed.returncode:
        detail = (
            str(completed.stderr or "").strip()
            or str(completed.stdout or "").strip()
            or f"command failed with status {completed.returncode}"
        )
        raise SuperAgentError(_tail(detail))
    return _tail(str(completed.stdout or ""))


def run_shell(
    command: str,
    *,
    cwd: Path | str | None = None,
    timeout: float = SHELL_TIMEOUT_S,
    runner: Runner = subprocess.run,
) -> str:
    """Run one operator-typed local shell command.

    The operator typed this line after a permission confirmation, so it runs
    through the shell exactly as typed. Output is tailed and sanitized before
    it reaches the conversation.
    """
    body = str(command or "").strip()
    if not body:
        raise SuperAgentError("empty shell command")
    try:
        completed = runner(
            body,
            shell=True,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SuperAgentError(
            f"shell command timed out after {int(timeout)}s"
        ) from exc
    except OSError as exc:
        raise SuperAgentError(f"shell command could not start: {exc}") from exc
    output = _tail(str(completed.stdout or ""))
    if completed.returncode:
        detail = (
            str(completed.stderr or "").strip()
            or f"exited with status {completed.returncode}"
        )
        raise SuperAgentError(
            (output + "\n" if output else "") + _tail(detail)
        )
    return output


def parse_conductor_args(line: str) -> list[str]:
    """Split a slash-command remainder into CLI arguments (shell-like quotes)."""
    import shlex

    try:
        return shlex.split(line)
    except ValueError as exc:
        raise SuperAgentError(f"could not parse arguments: {exc}") from exc


def describe(args: list[str]) -> str:
    """A short label for the permission prompt."""
    return " ".join(plain(part) for part in args[:4])


__all__: list[Any] = [
    "DEFAULT_TIMEOUT_S",
    "MAX_OUTPUT_CHARS",
    "SHELL_TIMEOUT_S",
    "describe",
    "parse_conductor_args",
    "run_conductor",
    "run_shell",
    "worktree_root",
]
