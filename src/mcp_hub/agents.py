"""Per-agent config paths. Fixed relative to a HOME the caller passes in.

Nothing here reads the process home. A path that leaves that HOME, including
through a symlink, is refused before any read or write.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .errors import McpHubError
from .model import AGENT_IDS

# container is the JSON key path of the server map. TOML agents use the
# [mcp_servers.NAME] table family instead. An adapter module under
# ``mcp_hub.adapters`` may supply its own projection (``native``), extra
# representability checks (``cannot``), and, for a format that is neither JSON
# nor TOML, a ``codec`` (see ``mcp_hub.adapters.base.Codec``).
@dataclass(frozen=True)
class AgentSpec:
    agent: str
    relative: tuple[str, ...]
    fmt: str  # "json" | "toml" | "codec"
    container: tuple[str, ...]
    style: str
    native: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    cannot: Callable[[str, dict[str, Any], set[str]], str | None] | None = None
    has_env: bool = True
    codec: Any = None
    # Native body -> neutral field names, for import and drift. JSON agents
    # whose keys already match (command/args/url/env/type) leave it unset.
    generic: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    # Extra rule for the key written into the file (after ``name_in_file``),
    # e.g. dsh's 32-character serverName. A key that fails it is skipped for
    # this agent instead of failing the whole edit.
    key_pattern: Any = None
    # Turn an owned native body off, for an app whose off switch is not an
    # ``enabled`` field (Antigravity's ``disabled: true``). Unset means the
    # generic ``enabled = false`` rule, or delete when the body has no
    # ``enabled`` field.
    disable: Callable[[dict[str, Any]], dict[str, Any]] | None = None


def _spec(
    agent: str,
    *relative: str,
    fmt: str,
    container: tuple[str, ...] = (),
    style: str = "",
) -> AgentSpec:
    return AgentSpec(
        agent=agent,
        relative=relative,
        fmt=fmt,
        container=container,
        style=style or agent,
    )


def _adapter_specs() -> tuple[AgentSpec, ...]:
    from .adapters import ADAPTER_SPECS

    return ADAPTER_SPECS


SPECS: dict[str, AgentSpec] = {
    spec.agent: spec
    for spec in (
        _spec("claude", ".claude.json", fmt="json", container=("mcpServers",)),
        _spec("cursor", ".cursor", "mcp.json", fmt="json", container=("mcpServers",)),
        _spec(
            "kimi", ".kimi-code", "mcp.json", fmt="json", container=("mcpServers",)
        ),
        _spec("codex", ".codex", "config.toml", fmt="toml"),
        _spec("grok", ".grok", "config.toml", fmt="toml"),
        _spec("stepfun", ".stepcode", "config.toml", fmt="toml"),
        _spec(
            "minimax",
            ".minimax",
            "mcp.json",
            fmt="json",
            container=("mcpServers",),
        ),
        _spec(
            "zcode",
            ".zcode",
            "cli",
            "config.json",
            fmt="json",
            container=("mcp", "servers"),
        ),
        _spec(
            "opencode",
            ".config",
            "opencode",
            "opencode.json",
            fmt="json",
            container=("mcp",),
        ),
    )
    + _adapter_specs()
}

if set(SPECS) != set(AGENT_IDS):  # pragma: no cover - import-time wiring check
    raise RuntimeError("mcp_hub SPECS and AGENT_IDS disagree")


def agent_path(home: Path, agent: str) -> Path:
    spec = SPECS.get(agent)
    if spec is None:
        raise McpHubError(f"unknown agent {agent!r}")
    return home.joinpath(*spec.relative)


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def assert_no_escape(home: Path, path: Path) -> None:
    """Refuse a path that is not lexically inside ``home`` or that symlinks out.

    The check does not follow a symlink to read or write it. A symlink config
    is refused even when its target stays inside home: writing through it would
    still be following it.
    """
    if not isinstance(home, Path) or not isinstance(path, Path):
        raise McpHubError("path must be a filesystem path")
    if not path.is_absolute():
        path = home / path
    try:
        rel = path.relative_to(home)
    except ValueError as exc:
        raise McpHubError("path is outside home") from exc
    if any(part in {"..", ""} for part in rel.parts):
        raise McpHubError("path escapes home")
    home_real = home.resolve()
    current = home
    for part in rel.parts:
        if part in {".", ".."}:
            raise McpHubError("path escapes home")
        current = current / part
        try:
            is_link = current.is_symlink()
        except OSError as exc:
            raise McpHubError("refusing to follow symlink") from exc
        if not is_link:
            continue
        try:
            raw = Path(os.readlink(current))
        except OSError as exc:
            raise McpHubError("refusing to follow symlink") from exc
        target = raw if raw.is_absolute() else (current.parent / raw)
        try:
            resolved = target.resolve()
        except OSError as exc:
            raise McpHubError("refusing to follow symlink") from exc
        if not _within(home_real, resolved):
            raise McpHubError(
                f"refusing to follow symlink that points outside home: {part}"
            )
        raise McpHubError(f"refusing to follow symlink: {part}")


def hub_dir(home: Path) -> Path:
    """``<home>/.conductor/mcp``, created mode 0700. Never follows a symlink."""
    base = home / ".conductor"
    mcp = base / "mcp"
    assert_no_escape(home, base)
    base.mkdir(exist_ok=True)
    assert_no_escape(home, mcp)
    mcp.mkdir(exist_ok=True)
    try:
        os.chmod(mcp, 0o700)
    except OSError as exc:
        raise McpHubError(f"could not protect {mcp.name}: {exc.strerror or exc}") from exc
    return mcp


def known_agents() -> tuple[str, ...]:
    return AGENT_IDS
