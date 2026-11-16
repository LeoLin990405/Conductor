"""A temporary HOME for the collaboration tests, plus a real-HOME tripwire.

The protocol tests must never read or write the operator's ``~/.conductor``,
``~/.config`` or keychain-adjacent files. These helpers point every
home-derived path at ``tmp_path`` and snapshot the real HOME before and after
the test, so a stray write fails loudly instead of silently touching the
machine that runs the suite.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

#: Direct children whose *content* must stay fixed. A stray home-derived write
#: lands in one of these stores. Other pre-existing children are only checked
#: for creation, removal and type changes: a live CLI daemon (``~/.grokbot``,
#: for example) rewrites files inside its own state directory while the suite
#: runs, and watching every child's mtime turned that unrelated activity into a
#: flaky failure. High-churn caches (``.cache``, ``.local``) are also left to
#: the name/type check; the collaboration contract only persists under
#: ``.conductor`` and XDG config, not in a cache.
#:
#: Watched stores are walked recursively. A directory's own ``mtime`` does not
#: move when an existing file below it is overwritten, so direct-child metadata
#: alone cannot see that write; every descendant's size/time is part of the
#: signature instead.
_WATCHED_CONTENT = frozenset(
    {
        ".aws",
        ".conductor",
        ".config",
        ".docker",
        ".git-credentials",
        ".gitconfig",
        ".gnupg",
        ".kube",
        ".netrc",
        ".ssh",
    }
)

#: Subtrees that a live daemon or package manager rewrites continuously. They
#: are stepped over while walking a watched store so a legitimate background
#: write cannot masquerade as test leakage. Everything else below a watched
#: store is part of the signature, including existing files whose parent
#: directory metadata does not move when their bytes change.
_UNWATCHED_NAMES = frozenset(
    {
        "autonomy",
        "busy",
        "cache",
        "cacheddata",
        "cachestorage",
        "code cache",
        "dispatch",
        "gpucache",
        "logs",
        "node_modules",
        "run",
        "service worker",
        "superagent-sessions",
        "usage",
        "worktrees",
    }
)


#: Top-level suffixes that other live tools create and delete on their own
#: schedule (Claude Code writes ``~/.claude.json.lock`` around every config
#: save). The collaboration contract never writes a top-level lock or temp
#: file, so these transient siblings are left out of the name check, and the
#: root directory's own mtime (which moves with them) is not compared either;
#: every other created or removed name still fails.
_TRANSIENT_TOP_LEVEL_SUFFIXES = (".lock", ".tmp")


def _is_transient_top_level(name: str) -> bool:
    lowered = name.casefold()
    return lowered.endswith(_TRANSIENT_TOP_LEVEL_SUFFIXES) or ".tmp." in lowered


def _content_signature(info: os.stat_result) -> tuple[int, ...]:
    """Metadata that moves when an existing file's bytes or layout change."""
    return (info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _watched_entries(child: Path) -> list[tuple[str, tuple[int, ...]]]:
    """Walk one watched store without following symlinks.

    Every path is returned relative to HOME, so a nested overwrite names the
    exact file that changed in the assertion message. Unreadable or vanishing
    entries are skipped: the walk must not turn an unrelated permission error
    into a false leak.
    """
    entries: list[tuple[str, tuple[int, ...]]] = []
    pending: list[tuple[str, Path]] = [("", child)]
    while pending:
        relative, current = pending.pop()
        try:
            info = current.lstat()
        except OSError:
            continue
        name = child.name if not relative else f"{child.name}/{relative}"
        entries.append((name, _content_signature(info)))
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            continue
        try:
            with os.scandir(current) as children:
                for item in children:
                    if item.name.casefold() in _UNWATCHED_NAMES:
                        continue
                    nested = item.name if not relative else f"{relative}/{item.name}"
                    pending.append((nested, Path(item.path)))
        except OSError:
            continue
    return entries


def real_home() -> Path:
    """The operator's HOME, resolved before the fixture monkeypatches it."""
    return Path(os.environ.get("HOME") or "~").expanduser().resolve()


def snapshot(home: Path) -> tuple[object, ...]:
    """Stat the real HOME and walk every watched credential/config store.

    Names are always compared, so creating ``~/.conductor`` (or any new child)
    after the snapshot still fails. Watched stores contribute every descendant,
    not only their direct children, so overwriting an existing nested file
    cannot hide behind an unchanged parent-directory mtime. Other top-level
    children keep the cheap name/type check described above.
    """
    if not home.exists():
        return ()
    try:
        root = home.stat()
        children = sorted(home.iterdir(), key=lambda item: item.name)
    except OSError:
        return ("unreadable",)
    entries: list[tuple[str, tuple[int, ...]]] = []
    for child in children:
        if _is_transient_top_level(child.name):
            continue
        if child.name in _WATCHED_CONTENT:
            entries.extend(_watched_entries(child))
            continue
        try:
            info = child.lstat()
        except OSError:
            continue
        entries.append((child.name, (info.st_mode,)))
    entries.sort(key=lambda item: item[0])
    return (root.st_mode, tuple(entries))


def isolate(tmp_path: Path, monkeypatch) -> tuple[Path, Path, tuple[object, ...]]:
    """Point HOME/XDG/CONDUCTOR_HOME at ``tmp_path`` and capture the real HOME."""
    original = real_home()
    before = snapshot(original)

    home = tmp_path.resolve() / "home"
    config = home / ".config"
    cache = home / ".cache"
    state = home / ".local" / "state"
    data = home / ".local" / "share"
    conductor = home / ".conductor"
    for directory in (config, cache, state, data, conductor):
        directory.mkdir(parents=True, mode=0o700)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("CONDUCTOR_HOME", str(conductor))

    # ``Path.home`` reads HOME on POSIX; assert the isolation before any test
    # body runs so a path resolved at import time cannot leak the real HOME.
    assert Path.home() == home
    return home, original, before


def assert_untouched(original: Path, before: tuple[object, ...]) -> None:
    after = snapshot(original)
    if after == before:
        return
    if len(before) == 2 and len(after) == 2:
        old = {entry[0]: entry[1:] for entry in before[1]}
        new = {entry[0]: entry[1:] for entry in after[1]}
        changed = sorted(
            name
            for name in set(old) | set(new)
            if old.get(name) != new.get(name)
        )
    else:
        changed = ["<snapshot shape changed>"]
    raise AssertionError(
        "a collaboration test changed the real HOME at "
        f"{original}: {changed}; set HOME/XDG_*/CONDUCTOR_HOME to tmp_path first"
    )


__all__ = ["assert_untouched", "isolate", "real_home", "snapshot"]
