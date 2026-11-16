"""Shared errors, the operator-HOME guard, and the atomic writer."""

from __future__ import annotations

import os
import pwd
import secrets
from pathlib import Path

_PROTECTED_TOP = frozenset(
    {
        ".agents",
        ".claude",
        ".codex",
        ".conductor",
        ".config",
        ".cursor",
        ".dsh",
        ".gemini",
        ".grok",
        ".kimi",
        ".kimi-code",
        ".mavis",
        ".mcode",
        ".minimax",
        ".qwen",
        ".step",
        ".stepcode",
        ".zcode",
        "Library",
    }
)


class HubError(ValueError):
    """A refused or invalid instructions-hub operation."""


def operator_home() -> Path:
    """The login HOME, ignoring a patched ``HOME`` environment variable."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def guarded_dir(path: Path | str, *, label: str) -> Path:
    """Resolve ``path`` and refuse the operator HOME and its CLI config trees."""
    resolved = Path(path).expanduser().resolve(strict=False)
    if resolved.exists() and resolved.is_symlink():
        raise HubError(f"{label} must not be a symlink")
    real = operator_home()
    if resolved == real:
        raise HubError(f"{label} refuses the operator HOME")
    try:
        relative = resolved.relative_to(real)
    except ValueError:
        return resolved
    parts = relative.parts
    if not parts:
        raise HubError(f"{label} refuses the operator HOME")
    if parts[0] in _PROTECTED_TOP:
        raise HubError(f"{label} refuses operator config paths")
    if parts[0] == ".config" and len(parts) >= 2 and parts[1] in {"opencode", "conductor"}:
        raise HubError(f"{label} refuses operator config paths")
    return resolved


def stays_inside(base: Path, path: Path) -> bool:
    """Whether ``path`` resolves to a path inside ``base``.

    A symlinked ancestor can point out of the tree, so the check is on the
    resolved path: a literal prefix proves nothing. Both sides are
    resolved, so a symlinked ``base`` compares against its real location.
    """
    try:
        path.resolve(strict=False).relative_to(base.resolve(strict=False))
    except ValueError:
        return False
    return True


def resolve_home(home: Path | str, *, operator: bool, label: str = "--home") -> Path:
    """The one rule every command uses to turn a HOME argument into a path.

    ``operator=False``: ``home`` goes through :func:`guarded_dir`, which
    refuses the operator HOME and its config trees. ``operator=True``:
    ``home`` must be the operator HOME itself; this is the only way any
    command reaches it.
    """
    if not operator:
        return guarded_dir(home, label=label)
    real = operator_home()
    resolved = Path(home).expanduser().resolve(strict=False)
    if resolved != real:
        raise HubError(f"{label}: operator mode only accepts the operator HOME {real}")
    return real


def atomic_write(path: Path, data: str | bytes, *, mode: int = 0o600) -> None:
    """Write ``data`` via a sibling temp file, then replace.

    The temp name carries the pid and a random token, so two writers never
    share one temp file. The temp file is opened exclusively and without
    following symlinks, and it is removed if anything fails before the
    replace.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        try:
            os.fchmod(fd, mode)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
