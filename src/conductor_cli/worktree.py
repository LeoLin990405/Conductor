"""Validation for Ticket-selected local worktrees."""

from __future__ import annotations

import os
from pathlib import Path

# A copy of ``src.config.models.conductor_worktree_root``. The wheel that
# ships ``conductor`` does not ship ``src``, so this package cannot import it.
WORKTREE_ROOT_DEFAULT = "~/.conductor/worktrees"
DEFAULT_WORKTREE_ROOT = Path(WORKTREE_ROOT_DEFAULT)


def conductor_worktree_root() -> str:
    """Return the worktree root.

    ``CONDUCTOR_WORKTREE_ROOT`` wins. ``CONDUCTOR_WT_ROOT`` is the legacy alias.
    The default is ``~/.conductor/worktrees``.
    """
    for name in ("CONDUCTOR_WORKTREE_ROOT", "CONDUCTOR_WT_ROOT"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return WORKTREE_ROOT_DEFAULT


def configured_worktree_root(explicit: str | None = None) -> str:
    """Use an explicit root, otherwise the ``CONDUCTOR_WORKTREE_ROOT`` setting."""
    if explicit and explicit != WORKTREE_ROOT_DEFAULT:
        return explicit
    return conductor_worktree_root()


def resolve_ticket_worktree(
    path: str, allowed_root: str | Path | None = None
) -> Path:
    """Resolve an existing worktree only when it stays inside the operator's root."""
    if allowed_root is None:
        allowed_root = conductor_worktree_root()
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Ticket cwd must be a non-empty path")
    try:
        root = Path(allowed_root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as exc:
        raise ValueError("worktree root must be an existing directory") from exc
    if not root.is_dir():
        raise ValueError("worktree root must be an existing directory")
    try:
        candidate = Path(path.strip()).expanduser()
    except (OSError, RuntimeError) as exc:
        raise ValueError("Ticket cwd must be a valid path under the worktree root") from exc
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            "Ticket cwd must be an existing directory under the worktree root"
        ) from exc
    if not resolved.is_dir():
        raise ValueError(
            "Ticket cwd must be an existing directory under the worktree root"
        )
    if not resolved.is_relative_to(root):
        raise ValueError("Ticket cwd must be under the configured worktree root")
    return resolved
