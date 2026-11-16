"""Files the host OS drops into a worktree that are never a Worker's change.

Finder writes ``.DS_Store`` into any folder a window opened, including a
dispatch worktree. Git reports it as an untracked file, so a dirty-tree check
that reads ``git status --porcelain`` failed lines (18 sessions in the
2026-11-14 review) for a file nobody wrote. Only *untracked* ``.DS_Store``
entries are noise: a tracked one that changed is still a change.
"""

from __future__ import annotations

from collections.abc import Iterable

NOISE_BASENAMES = frozenset({".DS_Store"})


def is_noise_path(path: str) -> bool:
    """True when ``path`` (repo-relative, ``/``-separated) is OS litter."""
    text = str(path or "").strip().strip('"').rstrip("/")
    if not text:
        return False
    return text.rsplit("/", 1)[-1] in NOISE_BASENAMES


def _porcelain_entry_is_noise(entry: str) -> bool:
    # Porcelain v1: "XY path". Only untracked ("??") entries are ignored.
    return entry.startswith("?? ") and is_noise_path(entry[3:])


def filter_porcelain(text: str) -> str:
    """``git status --porcelain`` (v1, newline form) without untracked noise.

    Every other line is kept byte-for-byte, so ``out.strip() == ""`` still
    means "clean" for callers that test the text.
    """
    if not text:
        return text
    kept = [line for line in text.splitlines(keepends=True) if not _porcelain_entry_is_noise(line.rstrip("\r\n"))]
    return "".join(kept)


def drop_noise_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """Untracked ``paths`` without OS litter, order kept. Do not pass tracked paths."""
    return tuple(path for path in paths if not is_noise_path(path))
