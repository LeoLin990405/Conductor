"""Canonical spelling for the fixed, root-owned macOS system aliases.

On macOS ``/tmp``, ``/var`` and ``/etc`` are root-owned symlinks to
``/private/tmp``, ``/private/var`` and ``/private/etc``; ``$TMPDIR`` and
``tempfile.gettempdir()`` use the ``/var/folders/...`` spelling. Fail-closed
"no symlink component" checks would refuse every such path, so callers
rewrite only that leading alias before running the unchanged check. Any
other symlink (in particular a user-controlled one deeper in the path) is
left in place for the check to catch.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Collection
from pathlib import Path

#: The only first components that may be rewritten, each to ``/private/<name>``.
SYSTEM_ALIASES: frozenset[str] = frozenset({"tmp", "var", "etc"})


def canonical_system_path(
    path: str | os.PathLike[str],
    *,
    _aliases: Collection[str] = SYSTEM_ALIASES,
    _lstat: Callable[[str], os.stat_result] = os.lstat,
    _readlink: Callable[[str], str] = os.readlink,
) -> Path:
    """Rewrite a leading root-owned macOS alias (``/tmp`` etc.) to ``/private/...``.

    The rewrite happens only when the first component after ``/`` is a known
    alias, ``/<name>`` is a symlink owned by root, and its target is exactly
    ``/private/<name>`` (absolute, or the relative ``private/<name>`` macOS
    ships). Everything else is returned unchanged; the whole path is never
    resolved. The keyword-only ``_`` parameters exist for tests.
    """
    candidate = Path(path)
    parts = candidate.parts
    if len(parts) < 2 or parts[0] != "/" or parts[1] not in _aliases:
        return candidate
    name = parts[1]
    alias = f"/{name}"
    try:
        info = _lstat(alias)
        if not stat.S_ISLNK(info.st_mode) or info.st_uid != 0:
            return candidate
        target = _readlink(alias)
    except OSError:
        return candidate
    expected = f"/private/{name}"
    if target.rstrip("/") not in {expected, expected[1:]}:
        return candidate
    return Path(expected).joinpath(*parts[2:])

