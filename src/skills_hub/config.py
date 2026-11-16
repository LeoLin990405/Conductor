"""Effective configuration for the skills hub (env → config files → defaults).

JSON is ``<home>/.config/conductor/config.json`` (``CONFIG_FILENAME``).
``TOML_CONFIG_FILENAME`` (``skills-hub.toml``) is an optional overlay at
``<home>/.conductor/skills-hub.toml``, read from the same home ``load_config``
already takes. Only its ``[agents]`` table is applied: those agent ids
override the JSON config, and every JSON setting the TOML file does not set
still applies. Unknown TOML keys are ignored. Relative agent paths must still
resolve inside the home; absolute paths stay allowed.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .adapters import AgentDirs, LinkMode, normalize_link_mode

CONFIG_FILENAME = "config.json"
# Optional overlay at ``<home>/.conductor/skills-hub.toml``. Only the
# ``[agents]`` table is read; each id it sets wins over the same id in the
# JSON config. A missing file is an empty overlay, not an error.
TOML_CONFIG_FILENAME = "skills-hub.toml"
CONFIG_ENV = "CONDUCTOR_SKILLS_CONFIG"
REPO_ENV = "CONDUCTOR_SKILLS_REPO"
REPO_DIRNAME = "skills"

# Bounds for config-file controlled lists. Both only shape which directories
# Conductor may write into, so they guard against a fat-fingered config, not
# an adversary.
MAX_EXTRA_DIRS = 16
MAX_AGENT_OVERRIDES = 32


class SkillsConfigError(Exception):
    """The configuration on disk is unusable; the message is safe to print."""


def default_repo(home: str | Path | None = None) -> Path:
    base = Path(home).expanduser() if home is not None else Path.home()
    return base / ".conductor" / REPO_DIRNAME

def config_file_path(home: str | Path | None = None) -> Path:
    base = Path(home).expanduser() if home is not None else Path.home()
    return base / ".config" / "conductor" / CONFIG_FILENAME


def toml_config_path(home: str | Path | None = None) -> Path:
    """Path of the optional ``TOML_CONFIG_FILENAME`` overlay for this home."""
    base = Path(home).expanduser() if home is not None else Path.home()
    return base / ".conductor" / TOML_CONFIG_FILENAME

def _repo_from_env(environ: dict[str, str] | None = None) -> Path | None:
    env = os.environ if environ is None else environ
    raw = env.get(REPO_ENV, "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise SkillsConfigError(f"{REPO_ENV} must be an absolute path: {raw!r}")
    return path


def _read_config_object(path: Path) -> dict[str, Any]:
    import json

    try:
        if path.stat().st_size > 1024 * 1024:
            raise SkillsConfigError(f"{path}: config exceeds 1 MiB")
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise SkillsConfigError(f"{path}: not valid JSON ({exc.msg})") from exc
    except OSError as exc:
        raise SkillsConfigError(f"{path}: {exc.strerror or exc}") from exc
    if not isinstance(data, dict):
        raise SkillsConfigError(f"{path}: expected a JSON object")
    return data


def _read_toml_agents(path: Path) -> dict[str, Any]:
    """Return the ``[agents]`` table from the TOML overlay, or {} if absent.

    Any top-level key other than ``agents`` is ignored. A present ``agents``
    value that is not a table, or a document that is not valid TOML, is an
    error. The caller runs the result through the same home escape check as
    the JSON ``agents`` object.
    """
    import tomllib

    try:
        if path.stat().st_size > 1024 * 1024:
            raise SkillsConfigError(f"{path}: config exceeds 1 MiB")
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise SkillsConfigError(f"{path}: not valid TOML ({exc})") from exc
    except OSError as exc:
        raise SkillsConfigError(f"{path}: {exc.strerror or exc}") from exc
    if not isinstance(data, dict):
        raise SkillsConfigError(f"{path}: expected a TOML table")
    agents = data.get("agents", {})
    if agents is None:
        return {}
    if not isinstance(agents, dict):
        raise SkillsConfigError(f"{path}: 'agents' must be a table")
    return agents


def _expand(raw: str, home: Path | None, confine: bool, *, what: str) -> Path:
    """``~`` means ``home`` when the config was loaded for one.

    ``Path.expanduser`` reads the process HOME, so a config loaded for a
    rehearsal HOME would otherwise point back at the real one. With
    ``confine`` (an explicit ``--home``) an absolute path, or ``~user``, that
    leaves ``home`` is refused.
    """
    text = raw.strip()
    if home is not None and (text == "~" or text.startswith("~/")):
        path = Path(os.path.normpath(home / text[2:])) if text != "~" else home
    elif home is not None and confine and text.startswith("~"):
        raise SkillsConfigError(f"{what}: '~user' paths are refused with --home")
    else:
        path = Path(text).expanduser()
    if confine and home is not None and path.is_absolute():
        normal = Path(os.path.normpath(path))
        if not normal.is_relative_to(Path(os.path.normpath(home))):
            raise SkillsConfigError(f"{what} resolves outside --home: {raw!r}")
        path = normal
    return path


def _resolve_repo(
    values: dict[str, Any],
    config_path: Path,
    env_repo: Path | None,
    home_path: Path | None,
    confine: bool = False,
) -> Path:
    if env_repo is not None:
        return env_repo
    repo = values.get("repo")
    if repo is None:
        return default_repo(home_path)
    if not isinstance(repo, str) or not repo.strip():
        raise SkillsConfigError(f"{config_path}: 'repo' must be a non-empty string")
    expanded = _expand(repo, home_path, confine, what=f"{config_path}: 'repo'")
    if not expanded.is_absolute():
        expanded = (config_path.parent / expanded).resolve()
        if confine and home_path is not None and not expanded.is_relative_to(
            Path(home_path).resolve()
        ):
            raise SkillsConfigError(f"{config_path}: 'repo' resolves outside --home")
    return expanded


def _resolve_link_mode(values: dict[str, Any], config_path: Path) -> LinkMode:
    raw = values.get("default_link_mode", "symlink")
    try:
        return normalize_link_mode(raw)
    except ValueError as exc:
        raise SkillsConfigError(f"{config_path}: {exc}") from exc


def _resolve_extra_dirs(
    values: dict[str, Any],
    config_path: Path,
    home: Path | None = None,
    confine: bool = False,
) -> tuple[Path, ...]:
    raw = values.get("extra_agent_dirs", [])
    if not isinstance(raw, list):
        raise SkillsConfigError(f"{config_path}: 'extra_agent_dirs' must be a list")
    if len(raw) > MAX_EXTRA_DIRS:
        raise SkillsConfigError(
            f"{config_path}: 'extra_agent_dirs' accepts at most {MAX_EXTRA_DIRS} entries"
        )
    dirs: list[Path] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise SkillsConfigError(
                f"{config_path}: 'extra_agent_dirs' entries must be non-empty strings"
            )
        dirs.append(
            _expand(item, home, confine, what=f"{config_path}: 'extra_agent_dirs'")
        )
    return tuple(dirs)


def _resolve_agent_dirs(
    values: dict[str, Any],
    config_path: Path,
    home: Path | None,
    confine: bool = False,
) -> dict[str, Path]:
    raw = values.get("agents", {})
    if not isinstance(raw, dict):
        raise SkillsConfigError(f"{config_path}: 'agents' must be an object")
    if len(raw) > MAX_AGENT_OVERRIDES:
        raise SkillsConfigError(
            f"{config_path}: 'agents' accepts at most {MAX_AGENT_OVERRIDES} overrides"
        )
    # A relative override is anchored at the effective home — the explicit
    # ``home`` argument or the process HOME when none was given. Leaving it
    # relative here would make uninstall/sync operate on
    # ``cwd/<relative>/<skill>`` from wherever the CLI happened to run. The
    # joined path is normalized and must stay inside the home: a ``..``
    # component would otherwise point every delete path (uninstall, prune,
    # rollback) outside it, and ``allowed_dirs`` compares this same value so
    # it would not catch the escape either.
    base = Path(os.path.normpath(home if home is not None else Path.home()))
    resolved: dict[str, Path] = {}
    for agent, directory in raw.items():
        if not isinstance(agent, str) or not agent.strip():
            raise SkillsConfigError(f"{config_path}: agent keys must be non-empty")
        if not isinstance(directory, str) or not directory.strip():
            raise SkillsConfigError(
                f"{config_path}: 'agents.{agent}' must be a non-empty string"
            )
        path = _expand(
            directory, home, confine, what=f"{config_path}: 'agents.{agent}'"
        )
        if path.is_absolute():
            resolved[agent] = Path(os.path.normpath(path))
            continue
        joined = Path(os.path.normpath(base / path))
        if not joined.is_relative_to(base):
            raise SkillsConfigError(
                f"{config_path}: 'agents.{agent}' resolves outside the home "
                f"directory: {directory!r}"
            )
        resolved[agent] = joined
    return resolved


@dataclass(frozen=True)
class SkillsConfig:
    """Resolved settings for one skills-hub invocation.

    ``repo`` is where skills live centrally; ``agent_dir`` maps each fleet CLI
    to the directory it reads skills from; ``default_link_mode`` picks how
    installs materialize (symlink or copy).
    """

    repo: Path
    config_path: Path
    default_link_mode: LinkMode = "symlink"
    extra_agent_dirs: tuple[Path, ...] = ()
    agent_overrides: Mapping[str, Path] = field(default_factory=dict)
    config_file_values: Mapping[str, Any] = field(default_factory=dict)
    # The HOME this config was loaded for, when the caller named one. Agent
    # dirs resolve under it even when the repo is not <home>/.conductor/skills,
    # so a --home rehearsal never falls back to the process HOME.
    home: Path | None = None

    def agent_dir(self, agent: str) -> Path:
        override = self.agent_overrides.get(agent)
        if override is not None:
            return override
        return self._agent_dirs.dir_for(agent)

    @property
    def _agent_dirs(self) -> AgentDirs:
        return AgentDirs(self._home)

    @property
    def _home(self) -> Path | None:
        if self.home is not None:
            return self.home
        # The repo default is <home>/.conductor/skills; recover home from it
        # only when it matches that shape (an env override or custom config
        # repo means the config was built for a different home layout, in
        # which case agent dirs fall back to the process HOME).
        parts = self.repo.parts
        if len(parts) >= 2 and parts[-2] == ".conductor" and parts[-1] == "skills":
            return Path(*parts[:-2])
        return None

    @property
    def agents(self) -> list[str]:
        """All known agent ids, overrides included."""
        names = set(self._agent_dirs.names())
        names.update(self.agent_overrides)
        return sorted(names)


def load_config(
    home: str | Path | None = None,
    environ: dict[str, str] | None = None,
    *,
    confine: bool = False,
) -> SkillsConfig:
    """Settings for ``home`` (default: the process HOME).

    ``confine`` is for an explicit ``--home`` rehearsal: every configured
    path must stay inside that home.
    """
    home_path = Path(home).expanduser() if home is not None else None
    config_path = (
        home_path / ".config" / "conductor" / CONFIG_FILENAME
        if home_path is not None
        else config_file_path()
    )
    if environ is None and os.environ.get(CONFIG_ENV):
        # An explicit config file wins over the default location when both a
        # custom HOME and the env var are set.
        candidate = Path(os.environ[CONFIG_ENV]).expanduser()
        if not candidate.is_absolute():
            raise SkillsConfigError(f"{CONFIG_ENV} must be an absolute path")
        config_path = candidate
    values = _read_config_object(config_path)
    toml_path = (
        home_path / ".conductor" / TOML_CONFIG_FILENAME
        if home_path is not None
        else toml_config_path()
    )
    # JSON loads first. TOML replaces only the agent ids it sets, so a key
    # present in both files takes the TOML path and every other JSON agent
    # (and every non-agent JSON setting) stays.
    agent_overrides = _resolve_agent_dirs(values, config_path, home_path, confine)
    toml_agents = _read_toml_agents(toml_path)
    if toml_agents:
        agent_overrides.update(
            _resolve_agent_dirs({"agents": toml_agents}, toml_path, home_path, confine)
        )
    env_repo = _repo_from_env(environ)
    return SkillsConfig(
        repo=_resolve_repo(values, config_path, env_repo, home_path, confine),
        config_path=config_path,
        default_link_mode=_resolve_link_mode(values, config_path),
        extra_agent_dirs=_resolve_extra_dirs(values, config_path, home_path, confine),
        agent_overrides=agent_overrides,
        config_file_values=values,
        home=home_path,
    )
