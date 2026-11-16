"""Task categories for per-category success statistics (R6 L4, N5).

A line's category is the file area it writes: the most common bucket over its
``write_scope`` (or declared paths). Buckets are coarse on purpose so a
category collects enough outcomes to mean something:

* ``src/<pkg>/<sub>`` for paths under ``src/`` (``src/tools/auto_dispatch``);
* the first path component otherwise (``web``, ``docs``, ``tests``, ``scripts``);
* ``general`` when the line names no paths.

The value is recorded on each :class:`Outcome` so success rates can be split
by category x model without reading the task text.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

GENERAL = "general"
_SRC_DEPTH = 3


def path_bucket(path: str) -> str:
    """The category bucket one path falls in."""
    text = str(path or "").strip().lstrip("./").replace("\\", "/")
    parts = [part for part in text.split("/") if part and part not in {"**", "*"}]
    # Glob segments end the meaningful prefix.
    clean: list[str] = []
    for part in parts:
        if any(ch in part for ch in "*?["):
            break
        clean.append(part)
    if not clean:
        return GENERAL
    if clean[0] == "src":
        # A file belongs to its directory's bucket; directories cap at depth 3.
        dirs = clean[:-1] if "." in clean[-1] else clean
        return "/".join(dirs[:_SRC_DEPTH]) if len(dirs) > 1 else "src"
    if len(clean) == 1 and "." in clean[0]:
        return "root"
    return clean[0]


def category_for_paths(paths: Iterable[str]) -> str:
    counts = Counter(path_bucket(path) for path in paths if str(path or "").strip())
    counts.pop(GENERAL, None)
    if not counts:
        return GENERAL
    # Most common bucket; ties go to the lexically first for determinism.
    best = max(counts.values())
    return sorted(name for name, count in counts.items() if count == best)[0]


def line_category(line: Any) -> str:
    """The category of a dispatch line (``write_scope`` first, then declared paths)."""
    scope = tuple(getattr(line, "write_scope", ()) or ())
    if not scope:
        scope = tuple(getattr(line, "declared_paths", ()) or ())
    return category_for_paths(scope)
