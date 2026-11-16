"""Shared fixtures for the MCP hub tests.

``isolated_home`` points ``HOME`` at a fresh tmp dir and proves the operator's
real HOME is never written:

* a write guard wraps the filesystem mutators (``open`` in a write mode,
  ``os.replace``/``rename``/``unlink``/``mkdir``/``rmdir``/``symlink``/
  ``chmod``) and fails the test on any target under the real HOME;
* after the test, the hub-owned directories there (``.conductor/mcp``,
  ``.conductor/skills``) keep their ``lstat`` fingerprint.

Only ``lstat`` is used on the real HOME; nothing there is read or written.
Live app configs (``.claude.json`` and friends) are not fingerprinted because
the running apps rewrite them; the write guard covers them instead.
"""

from __future__ import annotations

import builtins
import io
import os
import pwd
from pathlib import Path

import pytest


def real_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


_HUB_OWNED = (
    (".conductor", "mcp"),
    (".conductor", "skills"),
    (".conductor", "skills-hub.toml"),
)


def _fingerprint(home: Path) -> dict[str, tuple[int, int] | None]:
    out: dict[str, tuple[int, int] | None] = {}
    for rel in _HUB_OWNED:
        path = home.joinpath(*rel)
        try:
            st = path.lstat()
        except OSError:
            out[str(path)] = None
            continue
        out[str(path)] = (st.st_size, st.st_mtime_ns)
    return out


def _under(path: object, root: str) -> bool:
    """True for ``root`` itself or an app-config area under it.

    App configs live in dot-directories and ``Library``. Plain directories
    (a checkout under ``~/src``, say) are not app config, so the repo the
    tests run from can still write its own caches.
    """
    if isinstance(path, int):
        return False
    try:
        text = os.path.abspath(os.fsdecode(path))
    except TypeError:
        return False
    if text == root:
        return True
    if not text.startswith(root + os.sep):
        return False
    first = text[len(root) + 1 :].split(os.sep, 1)[0]
    return first.startswith(".") or first == "Library"


# Refused real-HOME writes of the running test. Only the guard's own self-test
# clears it after provoking refusals on purpose.
GUARD_TOUCHED: list[str] = []


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    real = real_home()
    roots = {str(real), str(real.resolve())}
    home = tmp_path / "home"
    home.mkdir()
    assert not any(_under(home.resolve(), root) for root in roots)
    before = _fingerprint(real)
    touched = GUARD_TOUCHED
    touched.clear()

    def check(*paths: object) -> None:
        for path in paths:
            if any(_under(path, root) for root in roots):
                touched.append(os.fsdecode(path))
                raise AssertionError("write under the real HOME refused by test guard")

    real_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            check(file)
        return real_open(file, mode, *args, **kwargs)

    def wrap(name: str, positions: tuple[int, ...]):
        original = getattr(os, name)

        def guarded(*args, **kwargs):
            check(*(args[i] for i in positions if i < len(args)))
            return original(*args, **kwargs)

        monkeypatch.setattr(os, name, guarded)

    real_os_open = os.open
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def guarded_os_open(path, flags, *args, **kwargs):
        if flags & write_flags:
            check(path)
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    # A symlink's first argument is link text, not a path being written.
    for name, positions in (
        ("replace", (0, 1)),
        ("rename", (0, 1)),
        ("unlink", (0,)),
        ("remove", (0,)),
        ("mkdir", (0,)),
        ("rmdir", (0,)),
        ("symlink", (1,)),
        ("chmod", (0,)),
    ):
        wrap(name, positions)
    monkeypatch.setenv("HOME", str(home))
    yield home
    monkeypatch.undo()
    assert not touched, f"real HOME write attempted: {touched}"
    assert _fingerprint(real) == before, "a hub-owned dir in the real HOME changed"
