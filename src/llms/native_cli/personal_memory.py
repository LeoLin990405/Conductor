"""Keep the operator's personal agent memory out of delegated CLI sessions.

A reviewer or Ticket worker runs the operator's own CLI login, and with it,
by default, the operator's personal state: Codex's ``memories`` feature
(``$CODEX_HOME/memories/memory_summary.md`` is injected into every session),
the global ``$CODEX_HOME/AGENTS.md``, MCP servers from ``config.toml``, and
Claude Code's auto-memory. None of that belongs to the task being delegated.

Codex: the memory-off flags turn the feature off for one run, and an isolated
``CODEX_HOME`` keeps the login (``auth.json`` is symlinked, never read) and
the transcript store (``sessions`` is symlinked) while leaving out memories,
global instructions, skills, rules, history and every MCP server.

Claude Code: auto-memory is turned off through its documented env switch; the
reviewer's ``--safe-mode`` already drops CLAUDE.md, skills, hooks and MCP.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any

# ``--disable memories`` is ``-c features.memories=false``; the two
# ``memories.*`` keys stop reading and writing memories even if a later
# config layer turns the feature back on.
CODEX_MEMORY_OFF_ARGS: tuple[str, ...] = (
    "--disable",
    "memories",
    "-c",
    "memories.use_memories=false",
    "-c",
    "memories.generate_memories=false",
)

CLAUDE_MEMORY_OFF_ENV: dict[str, str] = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}

ISOLATED_CODEX_HOME_PREFIX = "conductor-codex-home-"
#: Where isolated homes live when not configured: Conductor's own state
#: directory, which no Worker sandbox can write ($TMPDIR and the workspace can).
ENV_CODEX_HOMES_DIR = "CONDUCTOR_CODEX_HOMES_DIR"
#: An isolated home older than this is a leftover of a crashed run.
STALE_HOME_SECONDS = 6 * 60 * 60

# Non-secret top-level keys an isolated home may carry forward. Anything else
# (``mcp_servers``, ``projects``, ``model_providers``, ``developer_instructions``,
# ``experimental_instructions_file``, profiles, ...) stays behind.
_CARRIED_KEYS = (
    "model",
    "model_reasoning_effort",
    "model_provider",
    "cli_auth_credentials_store",
    "forced_login_method",
    "preferred_auth_method",
)

# Keyring entries are keyed by the CODEX_HOME path, so a new home cannot
# reach them; ``auto`` resolves to the keyring wherever one exists.
_PATH_KEYED_STORES = frozenset({"keyring", "auto"})
# A custom provider needs its ``model_providers`` table, which may carry
# headers; such a home is not isolated (the memory-off flags still apply).
_BUILTIN_PROVIDERS = frozenset({"openai", "oss", "ollama", "lmstudio"})

# Linked, never opened or copied.
_LINKED_FILES = ("auth.json",)
_LINKED_DIRS = ("sessions",)


def codex_memory_off_args() -> tuple[str, ...]:
    return CODEX_MEMORY_OFF_ARGS


def claude_memory_off_env() -> dict[str, str]:
    return dict(CLAUDE_MEMORY_OFF_ENV)


def source_codex_home() -> Path:
    """The CODEX_HOME the CLI would use: the approved env value, else ``~/.codex``."""
    from src.llms.native_cli.isolation import approved_env_value, private_home

    configured = approved_env_value("CODEX_HOME")
    if configured:
        return Path(configured).expanduser()
    return private_home() / ".codex"


def _toml_value(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        )
        return f'"{escaped}"'
    return None


def _carried_settings(source_home: Path) -> dict[str, Any] | None:
    """The allowlisted settings of the source config, or None when isolation is unsafe."""
    config = source_home / "config.toml"
    if not config.is_file():
        return {}
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    carried = {key: data[key] for key in _CARRIED_KEYS if key in data}
    if carried.get("cli_auth_credentials_store") in _PATH_KEYED_STORES:
        return None
    provider = carried.get("model_provider")
    if provider is not None and provider not in _BUILTIN_PROVIDERS:
        return None
    return carried


def _isolated_config(carried: dict[str, Any]) -> str:
    lines = []
    for key in _CARRIED_KEYS:
        if key not in carried:
            continue
        rendered = _toml_value(carried[key])
        if rendered is not None:
            lines.append(f"{key} = {rendered}")
    lines.extend(
        [
            "",
            "[features]",
            "memories = false",
            "",
            "[memories]",
            "use_memories = false",
            "generate_memories = false",
            "",
        ]
    )
    return "\n".join(lines)


def prepare_isolated_codex_home(source_home: Path, dest_parent: Path) -> Path | None:
    """A fresh private CODEX_HOME under ``dest_parent`` with login but no personal memory.

    Returns None when a separate home would lose the login (a path-keyed
    keyring store) or cannot be built safely; the caller then relies on the
    memory-off flags alone.
    """
    source_home = Path(source_home).expanduser()
    carried = _carried_settings(source_home)
    if carried is None:
        return None
    dest_parent = Path(dest_parent)
    dest_parent.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix=ISOLATED_CODEX_HOME_PREFIX, dir=dest_parent))
    try:
        os.chmod(home, 0o700)
        for name in _LINKED_FILES:
            source = source_home / name
            if source.is_file():
                os.symlink(source.absolute(), home / name)
        for name in _LINKED_DIRS:
            source = source_home / name
            if source.is_dir():
                os.symlink(source.absolute(), home / name, target_is_directory=True)
        config = home / "config.toml"
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_isolated_config(carried))
    except BaseException:
        remove_isolated_codex_home(home)
        raise
    return home


def remove_isolated_codex_home(home: Path | None) -> None:
    """Delete an isolated home; its symlinks are removed, never followed.

    Nothing is ever copied or moved out of it. A token Codex refreshed by
    replacing the ``auth.json`` link is lost with the home (planner ruling,
    2026-11-14: a lost refresh costs a ``codex login``; writing a file found
    in the home over the operator's login would let whoever can write there
    substitute the credential).
    """
    if home is None:
        return
    shutil.rmtree(home, ignore_errors=True)


def _is_isolated_home(path: Path) -> bool:
    """A real directory with the isolation prefix (never a link to somewhere else)."""
    if not path.name.startswith(ISOLATED_CODEX_HOME_PREFIX):
        return False
    try:
        return not path.is_symlink() and path.is_dir()
    except OSError:
        return False


def sweep_stale_isolated_codex_homes(
    parent: Path, *, max_age_seconds: float = STALE_HOME_SECONDS, now: float | None = None
) -> int:
    """Remove isolated homes a crashed run left behind; returns how many.

    Only real directories with the isolation prefix are removed, and only
    once they are older than
    ``max_age_seconds`` so a run in another process keeps its home. The
    sweep deletes and nothing else: it never copies anything out.
    """
    current = time.time() if now is None else now
    removed = 0
    try:
        candidates = list(Path(parent).glob(f"{ISOLATED_CODEX_HOME_PREFIX}*"))
    except OSError:
        return 0
    for path in candidates:
        try:
            age = current - os.lstat(path).st_mtime
        except OSError:
            continue
        if age < max_age_seconds or not _is_isolated_home(path):
            continue
        remove_isolated_codex_home(path)
        if not path.exists():
            removed += 1
    return removed


#: Each Conductor process keeps its homes in a folder of its own, named
#: ``proc-<pid>-<random>``, and holds an exclusive ``flock`` on the folder's
#: ``.owner.lock`` for as long as it lives. The base folder is shared by every
#: Conductor process on the machine (prod and dev servers), so a sweep may
#: only remove a folder whose lock it can take: its owner is gone.
PROCESS_DIR_PREFIX = "proc-"
OWNER_LOCK = ".owner.lock"

_PROCESS_DIRS: dict[str, tuple[int, Path, int]] = {}
_SWEPT: set[str] = set()


def _try_lock(path: Path) -> int | None:
    """An fd holding an exclusive flock on ``path``, or None when another process holds it."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def process_home_dir(base: Path) -> Path | None:
    """This process's own private folder under ``base``, locked for its lifetime."""
    key = str(Path(base))
    pid = os.getpid()
    known = _PROCESS_DIRS.get(key)
    if known is not None and known[0] == pid and known[1].is_dir():
        return known[1]
    try:
        folder = Path(
            tempfile.mkdtemp(prefix=f"{PROCESS_DIR_PREFIX}{pid}-", dir=Path(base))
        )
        os.chmod(folder, 0o700)
    except OSError:
        return None
    fd = _try_lock(folder / OWNER_LOCK)
    if fd is None:
        shutil.rmtree(folder, ignore_errors=True)
        return None
    # The fd stays open (and locked) until this process exits.
    _PROCESS_DIRS[key] = (pid, folder, fd)
    return folder


def sweep_dead_process_dirs(
    base: Path, *, max_age_seconds: float = STALE_HOME_SECONDS, now: float | None = None
) -> int:
    """Remove the folders of Conductor processes that are gone; returns how many.

    A ``proc-*`` folder goes only when its owner lock can be taken (the owner
    exited, so the kernel dropped its lock). A folder with no lock file yet is
    one being created, and is left unless it is older than
    ``max_age_seconds``. Isolated homes of the earlier flat layout directly in
    ``base`` are removed by age. Deletes only; nothing is copied out.
    """
    base = Path(base)
    own = {str(entry[1]) for entry in _PROCESS_DIRS.values() if entry[0] == os.getpid()}
    current = time.time() if now is None else now
    removed = 0
    try:
        entries = list(base.iterdir())
    except OSError:
        return 0
    for path in entries:
        if str(path) in own or path.is_symlink() or not path.is_dir():
            continue
        if path.name.startswith(PROCESS_DIR_PREFIX):
            lock = path / OWNER_LOCK
            if lock.exists() and not lock.is_symlink():
                fd = _try_lock(lock)
                if fd is None:
                    continue  # its owner is alive
                try:
                    shutil.rmtree(path, ignore_errors=True)
                finally:
                    os.close(fd)
            else:
                try:
                    if current - os.lstat(path).st_mtime < max_age_seconds:
                        continue
                except OSError:
                    continue
                shutil.rmtree(path, ignore_errors=True)
            if not path.exists():
                removed += 1
    removed += sweep_stale_isolated_codex_homes(
        base, max_age_seconds=max_age_seconds, now=now
    )
    return removed


def sweep_once(base: Path) -> None:
    """The startup sweep: once per process and base folder."""
    key = f"{os.getpid()}:{Path(base)}"
    if key in _SWEPT:
        return
    _SWEPT.add(key)
    sweep_dead_process_dirs(Path(base))


def isolated_codex_home_parent() -> Path | None:
    """Where this run's isolated home goes, or None to rely on the memory-off flags.

    Never under ``$TMPDIR`` or a workspace: a Worker's sandbox can write
    there, and a writable home would let it swap the ``auth.json`` link or
    the config for its own. The default is Conductor's state directory
    (``$CONDUCTOR_CODEX_HOMES_DIR``, else ``$CONDUCTOR_HOME/codex-homes``,
    else ``~/.conductor/codex-homes``), kept private (0700, owned by us).

    Under a WorkerIsolation the CLI itself runs inside the outer sandbox,
    which only grants its own private areas; the CLI then uses the worker's
    private home rather than the operator's, so the flags are enough there.
    """
    from src.llms.native_cli.isolation import current_worker_isolation

    if current_worker_isolation() is not None:
        return None
    configured = os.environ.get(ENV_CODEX_HOMES_DIR, "").strip()
    if configured:
        parent = Path(configured).expanduser()
    else:
        conductor_home = os.environ.get("CONDUCTOR_HOME", "").strip()
        base = Path(conductor_home).expanduser() if conductor_home else Path.home() / ".conductor"
        parent = base / "codex-homes"
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = os.lstat(parent)
    except OSError:
        return None
    if (
        os.path.islink(parent)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        return None
    sweep_once(parent)
    return process_home_dir(parent)
