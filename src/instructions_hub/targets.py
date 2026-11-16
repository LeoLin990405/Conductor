"""Per-CLI instruction file paths.

Agent ids match ``src/skills_hub/adapters.py`` (copied here so this package
does not import or edit ``skills_hub``). Paths and size limits come from
``r2-prds/docs/plans/next/instructions-hub.md``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .core import stays_inside

SIZE_BUDGET = 8192

# A Claude stub names a rendered document instead of holding one, so the hub
# writes that document as a file of its own.
RENDERED_DIR = ".conductor/instructions/rendered"

# Copied from src/skills_hub/adapters.py agent ids (and aliases).
_ALIASES = {
    "claude-code": "claude",
    "cursor": "cursor-agent",
    "minimax": "mcode",
    "stepfun": "step",
    "glm": "zcode",
    "zhipu": "zcode",
    "deepseek-harness": "dsh",
    "deepseek": "dsh",
}


@dataclass(frozen=True)
class Target:
    """One CLI and the global instruction file it reads.

    ``env`` is the variable that stands in for the directory the file is
    read from. It replaces that whole directory prefix, so the file name
    is the only part of the path that survives the override.
    ``env_config_only`` marks a variable that moves the CLI's config
    instead of its instruction file, so it is not consulted for the path.
    """

    name: str
    kind: str  # "managed" | "stub" | "mdc"
    relative: tuple[str, ...]
    env: str | None = None
    env_file: str = "AGENTS.md"
    shared: bool = False
    env_config_only: bool = False
    # Paths older builds of the CLI (or older hub tables) named. Never
    # written; status reports one that exists so it can be migrated.
    legacy: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        _check_override(self.name, self.relative, self.env, self.env_file)


@dataclass(frozen=True)
class CliExtra:
    """A CLI-specific file loaded in addition to the shared AGENTS.md.

    Kimi reads ``$KIMI_CODE_HOME/AGENTS.md`` on top of ``~/.agents/AGENTS.md``.
    DSH reads ``$DSH_HOME/AGENTS.md`` on top of ``$DSH_AGENTS_HOME/AGENTS.md``.
    The shared path and this path are resolved independently.
    """

    relative: tuple[str, ...]
    env: str | None = None
    env_file: str = "AGENTS.md"

    def __post_init__(self) -> None:
        _check_override("cli-extra", self.relative, self.env, self.env_file)


def _check_override(
    name: str, relative: tuple[str, ...], env: str | None, env_file: str
) -> None:
    """An override stands in for the directory, never for the file name.

    The override path is ``<override>/<env_file>``, so the file name has
    to be the one the CLI reads. A variable that may not move the file at
    all is marked config only and is not consulted for the path.
    """
    if not env:
        return
    if env_file != relative[-1]:
        shown = "/".join(relative)
        raise ValueError(
            f"target {name!r} has an override, but it renames the file: "
            f"the CLI reads {shown}, not {env_file}"
        )


# Per-CLI files that hold only that CLI's own text. They are never written
# into the shared .agents/AGENTS.md. The file is always planned, even when a
# profile declares no extra fragments for it.
CLI_EXTRA_FILES: dict[str, CliExtra] = {
    "kimi": CliExtra((".kimi-code", "AGENTS.md"), env="KIMI_CODE_HOME"),
    "dsh": CliExtra((".dsh", "AGENTS.md"), env="DSH_HOME"),
}


TARGETS: tuple[Target, ...] = (
    Target("claude", "stub", (".claude", "CLAUDE.md"), env="CLAUDE_CONFIG_DIR", env_file="CLAUDE.md"),
    Target("codex", "managed", (".codex", "AGENTS.md"), env="CODEX_HOME"),
    # Shared content stays at ~/.agents/AGENTS.md. KIMI_CODE_HOME only moves
    # the CLI-specific file (see CLI_EXTRA_FILES).
    Target("kimi", "managed", (".agents", "AGENTS.md"), shared=True),
    Target("grok", "managed", (".grok", "AGENTS.md"), env="GROK_HOME"),
    # Cursor reads .cursor/rules/*.mdc, and the PRD calls CURSOR_CONFIG_DIR
    # config only: it moves the config, not the rules. An override here
    # would replace the rules directory and report a file Cursor never
    # loads, so the variable is not consulted for the path.
    Target(
        "cursor-agent",
        "mdc",
        (".cursor", "rules", "conductor.mdc"),
        env="CURSOR_CONFIG_DIR",
        env_file="conductor.mdc",
        env_config_only=True,
    ),
    # mcode reads $MINIMAX_DATA_DIR/AGENTS.md (default ~/.minimax); nothing
    # reads ~/.mcode. .mavis is the data dir's old name.
    Target(
        "mcode",
        "managed",
        (".minimax", "AGENTS.md"),
        env="MINIMAX_DATA_DIR",
        legacy=((".mcode", "AGENTS.md"), (".mavis", "AGENTS.md")),
    ),
    # step reads $STEP_CODING_AGENT_DIR/AGENTS.md (default ~/.stepcode/agent);
    # nothing reads ~/.step.
    Target(
        "step",
        "managed",
        (".stepcode", "agent", "AGENTS.md"),
        env="STEP_CODING_AGENT_DIR",
        legacy=((".step", "AGENTS.md"), (".step-harness", "AGENTS.md")),
    ),
    Target("zcode", "managed", (".zcode", "AGENTS.md")),
    Target("dsh", "managed", (".agents", "AGENTS.md"), env="DSH_AGENTS_HOME", shared=True),
    Target("opencode", "managed", (".config", "opencode", "AGENTS.md"), env="OPENCODE_CONFIG_DIR"),
)


def known_targets() -> list[str]:
    return [target.name for target in TARGETS]


def rendered_path(profile: str) -> str:
    """The path a Claude stub names to reach the rendered body of ``profile``."""
    return f"~/{RENDERED_DIR}/{profile}.md"


def rendered_file(profile: str) -> tuple[str, ...]:
    """The home-relative parts of the rendered body file of ``profile``."""
    return (*RENDERED_DIR.split("/"), f"{profile}.md")


def resolve_name(name: str) -> str:
    key = (name or "").strip()
    return _ALIASES.get(key, key)


def get_target(name: str) -> Target:
    key = resolve_name(name)
    for target in TARGETS:
        if target.name == key:
            return target
    raise KeyError(
        f"unknown target {name!r}; known targets: {', '.join(known_targets())}"
    )


def target_path(target: Target, home: Path, environ: dict[str, str] | None = None) -> Path:
    """Return the instruction file for ``target`` under ``home``.

    An override env var wins only when it is set and stays inside ``home``.
    A variable that is config only is not an override and never moves the
    file.
    """
    return _resolve_path(
        home,
        relative=target.relative,
        env=target.env,
        env_file=target.env_file,
        env_config_only=target.env_config_only,
        environ=environ,
    )


def cli_extra_path(
    target: str,
    home: Path,
    environ: dict[str, str] | None = None,
) -> Path:
    """Return the CLI-specific file for ``target`` under ``home``.

    Resolved through that CLI's own env (``DSH_HOME``, ``KIMI_CODE_HOME``),
    independently of the shared AGENTS.md override.
    """
    extra = CLI_EXTRA_FILES[target]
    return _resolve_path(
        home,
        relative=extra.relative,
        env=extra.env,
        env_file=extra.env_file,
        environ=environ,
    )


def _resolve_path(
    home: Path,
    *,
    relative: tuple[str, ...],
    env: str | None,
    env_file: str,
    environ: dict[str, str] | None,
    env_config_only: bool = False,
) -> Path:
    """HOME-relative default, or an override env that stays inside ``home``.

    ``home`` is resolved once, so the default path and the override path
    name the same directory the same way. A target that loads two files
    must not report one of them under a different spelling of the HOME.

    The result must also stay inside the HOME once its ancestors are
    resolved. ``<home>/.agents`` can be a symlink that points elsewhere,
    and a literal prefix under the HOME would not notice that.

    A config only variable is not an override: it moves the CLI's config
    and not its instruction file, so the default path is the only one.
    """
    mapping = os.environ if environ is None else environ
    base = Path(home).expanduser().resolve(strict=False)
    if env and not env_config_only:
        raw = (mapping.get(env) or "").strip()
        if raw:
            override = Path(raw).expanduser()
            if not override.is_absolute():
                override = base / override
            resolved = override.resolve(strict=False)
            try:
                resolved.relative_to(base)
            except ValueError as exc:
                raise ValueError(f"{env} must stay inside the isolated HOME") from exc
            # The override names a directory, whatever it looks like. A
            # suffix is not a file: KIMI_CODE_HOME=<home>/kimi.v1 and
            # DSH_HOME=<home>/dsh.v1 are directories, and the CLI reads
            # <override>/AGENTS.md from them.
            return _inside(base, resolved / env_file, label=env)
    return _inside(base, base.joinpath(*relative), label="/".join(relative))


def _inside(base: Path, path: Path, *, label: str) -> Path:
    """Refuse a path whose resolved ancestors leave the isolated HOME.

    A symlinked directory under the HOME can point anywhere, so the file
    the CLI would read is not the file the hub names.
    """
    if not stays_inside(base, path):
        raise ValueError(f"{label} must stay inside the isolated HOME")
    return path
