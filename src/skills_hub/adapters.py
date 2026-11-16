"""Per-agent skill directory mapping for the CLI fleet.

Each agent CLI reads skills from a fixed directory under ``$HOME`` (e.g.
Claude Code reads ``~/.claude/skills``). Conductor installs into those
directories and never invents paths of its own: an unknown agent id is an
error rather than a guess.

The registry is data, not logic: adding a fleet CLI means adding one row.
Directory names match what the CLIs themselves document; none of this is
discovered from the filesystem at import time so the module stays cheap and
side-effect free (tests pass a tmp HOME through :class:`AgentDirs`).

The directory table is adapted from xingkongliang/skills-manager (MIT,
Copyright (c) 2026 Tianliang Zhang); the full notice is in
``THIRD_PARTY_NOTICES.md`` at the repository root.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

LinkMode = str  # "symlink" | "copy"

VALID_LINK_MODES = ("symlink", "copy")


class UnknownAgentError(ValueError):
    """No skill directory is registered for this agent id."""


def normalize_link_mode(value: object) -> LinkMode:
    text = str(value if value is not None else "").strip().lower()
    if text not in VALID_LINK_MODES:
        raise ValueError(
            f"link mode must be one of {', '.join(VALID_LINK_MODES)}; got {value!r}"
        )
    return text


@dataclass(frozen=True)
class AgentDir:
    """One fleet CLI and the directory it reads skills from."""

    agent: str
    directory: Path
    aliases: tuple[str, ...] = ()
    # Directories older builds used. Never written; status/drift report a
    # legacy dir that still holds skills so the operator can migrate it.
    legacy: tuple[Path, ...] = ()

    @property
    def label(self) -> str:
        return self.agent

    def resolves(self, agent: str) -> bool:
        return agent == self.agent or agent in self.aliases


def _agent_dirs(home: Path | None = None) -> tuple[AgentDir, ...]:
    base = home if home is not None else Path.home()

    def d(
        agent: str,
        *segments: str,
        aliases: tuple[str, ...] = (),
        legacy: tuple[tuple[str, ...], ...] = (),
    ) -> AgentDir:
        return AgentDir(
            agent=agent,
            directory=base.joinpath(*segments),
            aliases=aliases,
            legacy=tuple(base.joinpath(*parts) for parts in legacy),
        )

    return (
        d("claude", ".claude", "skills", aliases=("claude-code",)),
        d("codex", ".codex", "skills"),
        d("cursor-agent", ".cursor", "skills", aliases=("cursor",)),
        d("grok", ".grok", "skills"),
        # Canonical ids stay kimi / mcode / step. The CLIs read .kimi-code,
        # .minimax, and .stepcode — not .kimi, .mcode, or .step.
        # Kimi also scans the shared ~/.agents/skills after this one.
        d("kimi", ".kimi-code", "skills", legacy=((".kimi", "skills"),)),
        # mcode: $MINIMAX_DATA_DIR/skills (default ~/.minimax); .mavis is the
        # old data dir name. .builtin-skills is seeded by mcode, not ours.
        d(
            "mcode",
            ".minimax",
            "skills",
            aliases=("minimax",),
            legacy=((".mcode", "skills"), (".mavis", "skills")),
        ),
        # step scans ~/.stepcode/agent/skills, ~/.stepcode/skills, then the
        # shared ~/.agents/skills.
        d(
            "step",
            ".stepcode",
            "skills",
            aliases=("stepfun",),
            legacy=((".step", "skills"),),
        ),
        d("zcode", ".zcode", "skills", aliases=("glm", "zhipu")),
        # dsh scans $DSH_HOME/skills (default ~/.dsh) then $DSH_AGENTS_HOME
        # /skills (default ~/.agents). The old hub row named a directory dsh
        # never reads; it is kept as legacy detection only.
        d(
            "deepseek",
            ".dsh",
            "skills",
            aliases=("deepseek-harness", "dsh"),
            legacy=((".config", "conductor", "deepseek", "skills"),),
        ),
        d("opencode", ".config", "opencode", "skills"),
        d("gemini", ".gemini", "skills"),
        d("antigravity", ".gemini", "antigravity", "skills"),
        d("qwen", ".qwen", "skills"),
        d("shared", ".agents", "skills"),
    )


class AgentDirs:
    """Registry of agent skill directories for one HOME."""

    def __init__(self, home: Path | None = None) -> None:
        self._home = home
        self._dirs = _agent_dirs(home)
        self._by_name = {a.agent: a for a in self._dirs}

    def names(self) -> list[str]:
        return [a.agent for a in self._dirs]

    def get(self, agent: str) -> AgentDir:
        key = (agent or "").strip()
        for entry in self._dirs:
            if entry.resolves(key):
                return entry
        raise UnknownAgentError(
            f"unknown agent {agent!r}; known agents: {', '.join(self.names())}"
        )

    def dir_for(self, agent: str) -> Path:
        return self.get(agent).directory

    def resolve_many(self, agents: list[str] | None) -> list[AgentDir]:
        """Expand a target list; ``None``/empty means every known agent."""
        if not agents:
            return list(self._dirs)
        seen: dict[str, AgentDir] = {}
        for agent in agents:
            entry = self.get(agent)
            seen[entry.agent] = entry
        # Preserve registry order for deterministic output.
        return [a for a in self._dirs if a.agent in seen]


# Process-wide default registry (HOME at first use). Code that must respect a
# custom HOME constructs its own AgentDirs instead of using this instance.
AGENT_DIRS = AgentDirs()


def known_agents() -> list[str]:
    return AGENT_DIRS.names()
