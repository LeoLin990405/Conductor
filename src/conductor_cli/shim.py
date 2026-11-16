"""Install the small, reversible shell launcher for the Python entry point."""

from __future__ import annotations

import os
import shlex
import stat
import sys
import tempfile
from pathlib import Path

from .client import CLIError

_PATH_BLOCK_BEGIN = "# >>> conductor CLI PATH >>>"
_PATH_BLOCK_END = "# <<< conductor CLI PATH <<<"


def _atomic_write(path: Path, contents: str, mode: int = 0o755) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if path.is_symlink():
        raise CLIError(f"Refusing to replace a symlink: {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(contents)
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _add_zsh_path(directory: Path) -> None:
    zshenv = Path.home() / ".zshenv"
    if zshenv.is_symlink():
        raise CLIError(f"Refusing to edit symlinked shell configuration: {zshenv}")
    original = zshenv.read_text(encoding="utf-8") if zshenv.exists() else ""
    block = f"{_PATH_BLOCK_BEGIN}\nexport PATH={shlex.quote(str(directory))}:${{PATH:-}}\n{_PATH_BLOCK_END}"
    if _PATH_BLOCK_BEGIN in original and _PATH_BLOCK_END in original:
        start = original.index(_PATH_BLOCK_BEGIN)
        end = original.index(_PATH_BLOCK_END, start) + len(_PATH_BLOCK_END)
        if original[start:end] == block:
            return
        original = original[:start].rstrip("\n") + ("\n" if original[:start] else "") + original[end:].lstrip("\n")
    if original.count(_PATH_BLOCK_BEGIN) > 0 or original.count(_PATH_BLOCK_END) > 0:
        raise CLIError(f"Unmatched Conductor PATH block markers in {zshenv}")
    if zshenv.exists():
        backup = zshenv.with_name(".zshenv.pre-conductor")
        if not backup.exists():
            backup.write_bytes(zshenv.read_bytes())
            backup.chmod(stat.S_IMODE(zshenv.stat().st_mode))
        mode = stat.S_IMODE(zshenv.stat().st_mode)
    else:
        mode = 0o600
    _atomic_write(zshenv, original + ("\n" if original and not original.endswith("\n") else "") + block + "\n", mode)


def _repository_root() -> Path:
    configured = os.getenv("CONDUCTOR_REPO")
    candidates = [Path(configured).expanduser()] if configured else [Path.cwd(), *Path.cwd().parents, *Path(__file__).resolve().parents]
    for candidate in candidates:
        if (candidate / "pyproject.toml").is_file() and (candidate / "scripts" / "conductor_local_daemon.py").is_file():
            return candidate.resolve()
    raise CLIError("Could not find the Conductor checkout; set CONDUCTOR_REPO before installing the shim")


def install_shim(destination: str | None = None) -> Path:
    """Install a `conductor` launcher without sudo or interpreter discovery."""
    if destination:
        path = Path(destination).expanduser()
    else:
        home_local = Path.home() / ".local" / "bin"
        candidates = [home_local, Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
        path = next((directory / "conductor" for directory in candidates if directory.is_dir() and os.access(directory, os.W_OK)), home_local / "conductor")
    if path.is_symlink():
        raise CLIError(f"Refusing to replace a symlink: {path}")
    if path.exists() and not path.is_file():
        raise CLIError(f"Shim destination is not a regular file: {path}")
    python = sys.executable
    repository = _repository_root()
    source_path = repository / "src"
    contents = (
        "#!/bin/sh\n"
        "# Managed by conductor install-shim.\n"
        f"export CONDUCTOR_REPO={shlex.quote(str(repository))}\n"
        f"export PYTHONPATH={shlex.quote(str(source_path))}${{PYTHONPATH:+:$PYTHONPATH}}\n"
        f"exec {shlex.quote(python)} -m conductor_cli \"$@\"\n"
    )
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise CLIError(f"Could not safely inspect shim destination: {path}") from exc
        if "# Managed by conductor install-shim." not in existing:
            raise CLIError(f"Refusing to replace an existing command: {path}")
        if existing == contents:
            _add_zsh_path(path.parent)
            return path
    _atomic_write(path, contents, 0o755)
    _add_zsh_path(path.parent)
    return path
