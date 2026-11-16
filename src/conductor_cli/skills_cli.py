"""``conductor skills`` — manage the central skill repo and fleet installs.

Subcommands do not talk to the Conductor server. ``import <git-url>`` clones
with git. ``backup --remote`` pushes with git argv and does not fetch.

Every subcommand takes ``--home <dir>`` (default: the real HOME) so a run can
be rehearsed on a copy. Commands that write into agent skill directories
(``install``, ``uninstall``, ``sync``, ``rollback``, ``adopt``) only preview
unless ``--apply`` is passed.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from skills_hub.adapters import AgentDirs
from skills_hub.adopt import apply_adopt, plan_adopt
from skills_hub.config import SkillsConfig, SkillsConfigError, load_config
from skills_hub.core import SkillsError, discover_skills
from skills_hub.drift import drift_report
from skills_hub.manager import (
    import_skills,
    install,
    list_installed,
    rollback,
    sync,
    uninstall,
)
from skills_hub.status import status_report
from skills_hub.updates import backup_repo, check_updates, update_skill
from skills_hub.presets import (
    ROLES,
    delete_preset,
    effective_skills,
    get_preset,
    list_presets,
    list_projects,
    save_preset,
    set_project_skills,
)
from skills_hub.snapshots import list_snapshots
from skills_hub.seed import seed_fleet_rules
from .client import CLIError
from .output import build_envelope, command_label, envelope_json


def register_parser(subparsers: argparse._SubParsersAction) -> None:
    skills = subparsers.add_parser(
        "skills", help="manage the central skill repo and per-agent installs"
    )
    skills.set_defaults(handler=_handle, local=True)
    commands = skills.add_subparsers(dest="skills_command", required=True)

    listing = commands.add_parser("list", help="list skills in the central repo")
    listing.add_argument("--agent", action="append", help="limit to these agents")
    listing.add_argument(
        "--installed", action="store_true", help="show what each agent has installed"
    )

    import_cmd = commands.add_parser(
        "import", help="import skills from a git URL, local folder or zip"
    )
    import_cmd.add_argument("source", help="git URL, local directory, or .zip file")
    import_cmd.add_argument("--ref", help="git branch or tag to clone")
    import_cmd.add_argument(
        "--force", action="store_true", help="replace existing skills of the same name"
    )

    install_cmd = commands.add_parser(
        "install", help="install skills into agent skill directories"
    )
    install_cmd.add_argument(
        "skills", nargs="+", help="skill names (or 'preset:<name>')"
    )
    install_cmd.add_argument(
        "--agent", action="append", help="target agent (repeatable; default: all)"
    )
    install_cmd.add_argument(
        "--mode", choices=("symlink", "copy"), help="install mode (default from config)"
    )

    uninstall_cmd = commands.add_parser(
        "uninstall", help="remove Conductor-installed skills from agents"
    )
    uninstall_cmd.add_argument(
        "skills", nargs="*", help="skill names (default: every Conductor install)"
    )
    uninstall_cmd.add_argument("--agent", action="append", help="limit to these agents")

    preset = commands.add_parser("preset", help="manage named skill bundles")
    preset_commands = preset.add_subparsers(dest="preset_command", required=True)
    preset_list = preset_commands.add_parser("list", help="list presets")
    preset_list.set_defaults(preset_action="list")
    preset_show = preset_commands.add_parser("show", help="show one preset")
    preset_show.add_argument("name")
    preset_show.set_defaults(preset_action="show")
    preset_save = preset_commands.add_parser("save", help="create or replace a preset")
    preset_save.add_argument("name")
    preset_save.add_argument("skills", nargs="+")
    preset_save.add_argument("--description", default="")
    preset_save.set_defaults(preset_action="save")
    preset_delete = preset_commands.add_parser("delete", help="delete a preset")
    preset_delete.add_argument("name")
    preset_delete.set_defaults(preset_action="delete")

    project = commands.add_parser(
        "project", help="per-project (and per-line) skill sets"
    )
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_show = project_commands.add_parser("show", help="show a project's sets")
    project_show.add_argument("project")
    project_show.add_argument("--line", default="default")
    project_show.add_argument("--role", choices=ROLES)
    project_show.set_defaults(project_action="show")
    project_set = project_commands.add_parser("set", help="set a project's skills")
    project_set.add_argument("project")
    project_set.add_argument("skills", nargs="*")
    project_set.add_argument("--line", default="default")
    project_set.add_argument("--role", choices=ROLES)
    project_set.set_defaults(project_action="set")
    project_list = project_commands.add_parser("list", help="list projects")
    project_list.set_defaults(project_action="list")

    sync_cmd = commands.add_parser(
        "sync", help="reconcile agent directories with the central repo"
    )
    sync_cmd.add_argument("--agent", action="append", help="limit to these agents")
    sync_cmd.add_argument(
        "--mode", choices=("symlink", "copy"), help="install mode (default from config)"
    )
    sync_cmd.add_argument(
        "--prune",
        action="store_true",
        help="remove agent entries whose skill left the central repo "
        "(default: report them as stale and leave them in place)",
    )

    rollback_cmd = commands.add_parser(
        "rollback", help="restore agent directories to a snapshot"
    )
    rollback_cmd.add_argument(
        "snapshot_id", nargs="?", help="snapshot id (default: most recent)"
    )

    seed_cmd = commands.add_parser(
        "seed-fleet-rules", help="ensure the built-in fleet-rules skill exists"
    )
    seed_cmd.add_argument(
        "--force", action="store_true", help="overwrite an existing fleet-rules skill"
    )

    adopt_cmd = commands.add_parser(
        "adopt",
        help="plan bringing agent-local skills into the central repo (dry-run unless --apply)",
    )
    adopt_cmd.add_argument(
        "--apply",
        action="store_true",
        help="write the plan (default is a dry-run)",
    )
    _json_option(adopt_cmd)

    status_cmd = commands.add_parser(
        "status", help="show how each agent's skills relate to the central repo"
    )
    _json_option(status_cmd)

    check_cmd = commands.add_parser(
        "check", help="report how far each git skill is behind its local upstream"
    )
    _json_option(check_cmd)

    update_cmd = commands.add_parser("update", help="fast-forward one skill")
    update_cmd.add_argument("name", nargs="?", help="skill name")
    update_cmd.add_argument(
        "--force",
        action="store_true",
        help="snapshot local edits, then overwrite an imported copy",
    )
    _json_option(update_cmd)

    backup_cmd = commands.add_parser(
        "backup", help="copy the central repo and push it to a git remote"
    )
    backup_cmd.add_argument(
        "--remote",
        help=(
            "local directory, local bare repo, or git URL "
            "(a bare repo or URL is pushed; a URL is not fetched)"
        ),
    )
    _json_option(backup_cmd)

    drift_cmd = commands.add_parser(
        "drift",
        help="read-only: per agent, missing/different/extra skills, fragile links, legacy dirs",
    )
    _json_option(drift_cmd)

    for name in _AGENT_WRITERS:
        commands.choices[name].add_argument(
            "--apply",
            action="store_true",
            help="write agent skill directories (default is a preview)",
        )
    _add_home_everywhere(skills)


# Commands that write agent skill directories. ``adopt`` has its own --apply.
_AGENT_WRITERS = ("install", "uninstall", "sync", "rollback")


def _add_home_everywhere(parser: argparse.ArgumentParser) -> None:
    """``--home`` on every (sub)command. SUPPRESS keeps nested defaults apart."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                child.add_argument(
                    "--home",
                    dest="skills_home",
                    default=argparse.SUPPRESS,
                    help="HOME to use (default: the real HOME); rehearse on a copy",
                )
                _add_home_everywhere(child)


def _resolve_home(args: argparse.Namespace) -> tuple[Path, bool]:
    raw = getattr(args, "skills_home", None)
    if raw is None or not str(raw).strip():
        return Path.home(), False
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.is_dir():
        raise CLIError("--home must be an existing directory")
    return path, True


def _handle(args: argparse.Namespace) -> dict[str, Any] | int:
    command = args.skills_command
    home, explicit = _resolve_home(args)
    try:
        # An explicit --home is a rehearsal: the env overrides that could point
        # back at the real config or repo are ignored.
        config = load_config(
            home, environ={} if explicit else None, confine=explicit
        )
    except SkillsConfigError as exc:
        raise CLIError(str(exc)) from exc
    agent_dirs = _agent_dirs(
        config, args.agent if hasattr(args, "agent") else None, home
    )
    repo = config.repo
    if command in _AGENT_WRITERS and not getattr(args, "apply", False):
        return _preview(command, args, repo, agent_dirs, config)
    mode = getattr(args, "mode", None) or config.default_link_mode

    try:
        if command == "list":
            return _list(repo, agent_dirs, args)
        if command == "import":
            return import_skills(repo, args.source, ref=args.ref, force=args.force)
        if command == "install":
            return install(
                repo, agent_dirs, _expand_skills(repo, args.skills), mode=mode
            ).to_dict()
        if command == "uninstall":
            return uninstall(repo, agent_dirs, args.skills or None).to_dict()
        if command == "preset":
            return _preset(repo, args)
        if command == "project":
            return _project(repo, args)
        if command == "sync":
            return sync(repo, agent_dirs, mode=mode, prune=args.prune).to_dict()
        if command == "rollback":
            snapshot_id = args.snapshot_id
            if snapshot_id is None:
                found = list_snapshots(repo)
                if not found:
                    raise SkillsError("no snapshots to roll back to")
                snapshot_id = found[0]["id"]
            # A snapshot may only ever touch the directories this config
            # knows as agent skill dirs; anything else is refused.
            allowed = {config.agent_dir(agent) for agent in config.agents}
            result = rollback(repo, snapshot_id, allowed_dirs=allowed)
            return result
        if command == "seed-fleet-rules":
            return seed_fleet_rules(repo, force=args.force).to_dict()
        if command == "adopt":
            return _adopt(home, repo, agent_dirs, mode=mode, apply=args.apply)
        if command == "status":
            return status_report(home, repo, agent_dirs)
        if command == "drift":
            return drift_report(home, repo, agent_dirs)
        if command == "check":
            return check_updates(repo)
        if command == "update":
            if not args.name:
                return _usage_exit(args, "update requires a skill name")
            return update_skill(repo, args.name, force=bool(args.force))
        if command == "backup":
            remote = (args.remote or "").strip()
            if not remote:
                return _usage_exit(args, "backup requires --remote")
            return backup_repo(repo, remote)
    except SkillsError as exc:
        raise CLIError(str(exc)) from exc
    raise CLIError(f"unknown skills command {command!r}")


def _json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print the result as JSON")


def _usage_exit(args: argparse.Namespace, message: str) -> int:
    """Validation failure: the JSON envelope on stderr and exit code 2."""
    envelope = build_envelope(
        command_label(args),
        data=None,
        errors=[{"code": "usage", "message": message}],
        exit_code=2,
    )
    print(envelope_json(envelope), file=sys.stderr)
    return 2


def _preview(
    command: str,
    args: argparse.Namespace,
    repo: Path,
    agent_dirs: list[tuple[str, Path]],
    config: SkillsConfig,
) -> dict[str, Any]:
    """What a writing command would touch. Reads nothing beyond the args."""
    body: dict[str, Any] = {
        "dry_run": True,
        "command": command,
        "repo": str(repo),
        "agents": [{"agent": agent, "dir": str(path)} for agent, path in agent_dirs],
        "hint": f"re-run with --apply to write ({command})",
    }
    if command in {"install", "uninstall"}:
        body["skills"] = list(getattr(args, "skills", None) or [])
    if command in {"install", "sync"}:
        body["mode"] = getattr(args, "mode", None) or config.default_link_mode
    if command == "sync":
        body["prune"] = bool(args.prune)
    if command == "rollback":
        body["snapshot_id"] = args.snapshot_id
        body["agents"] = [
            {"agent": agent, "dir": str(config.agent_dir(agent))}
            for agent in config.agents
        ]
    return body


def _adopt(
    home: Path,
    repo: Path,
    agent_dirs: list[tuple[str, Path]],
    *,
    mode: str,
    apply: bool,
) -> dict[str, Any]:
    if not apply:
        plan = plan_adopt(home, repo, agent_dirs).to_dict()
        plan["dry_run"] = True
        return plan
    applied = apply_adopt(home, repo, agent_dirs, link_mode=mode)
    applied["dry_run"] = False
    return applied


def _agent_dirs(
    config: SkillsConfig, requested: list[str] | None, home: Path
) -> list[tuple[str, Path]]:
    """Built-ins plus config overrides. TOML wins inside ``config.agent_dir``."""
    builtin = AgentDirs(home)
    known = list(builtin.names())
    for name in sorted(config.agent_overrides):
        if name not in known:
            known.append(name)
    if not requested:
        names = known
    else:
        names = []
        for name in requested:
            if name not in known:
                raise CLIError(
                    f"unknown agent {name!r}; known agents: {', '.join(known)}"
                )
            names.append(name)
    return [(name, config.agent_dir(name)) for name in names]


def _list(
    repo: Path, agent_dirs: list[tuple[str, Path]], args: argparse.Namespace
) -> dict[str, Any]:
    entries = [e.to_dict() for e in discover_skills(repo)]
    out: dict[str, Any] = {
        "repo": str(repo),
        "skills": entries,
        "targets": [
            {"agent": agent, "dir": str(directory)} for agent, directory in agent_dirs
        ],
    }
    if args.installed:
        out["agents"] = list_installed(repo, agent_dirs)
    return out


def _expand_skills(repo: Path, skills: Sequence[str]) -> list[str]:
    """Expand ``preset:<name>`` references into their skill lists."""
    expanded: list[str] = []
    for item in skills:
        if item.startswith("preset:"):
            name = item[len("preset:") :]
            preset = get_preset(repo, name)
            if preset is None:
                raise SkillsError(f"unknown preset {name!r}")
            expanded.extend(preset.skills)
        else:
            expanded.append(item)
    if not expanded:
        raise SkillsError("no skills named; nothing to install")
    return expanded


def _preset(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    action = args.preset_action
    if action == "list":
        return {"presets": [p.to_dict() for p in list_presets(repo)]}
    if action == "show":
        preset = get_preset(repo, args.name)
        if preset is None:
            raise SkillsError(f"unknown preset {args.name!r}")
        return preset.to_dict()
    if action == "save":
        return save_preset(
            repo, args.name, list(args.skills), description=args.description
        ).to_dict()
    if action == "delete":
        return {"deleted": delete_preset(repo, args.name)}
    raise SkillsError(f"unknown preset action {action!r}")


def _project(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    action = args.project_action
    if action == "list":
        return {"projects": list_projects(repo)}
    if action == "show":
        skills = effective_skills(repo, args.project, line=args.line, role=args.role)
        return {
            "project": args.project,
            "line": args.line,
            "role": args.role,
            "skills": skills,
        }
    if action == "set":
        if not args.skills:
            raise SkillsError(
                "no skills named; use 'skills project set <name> <skill...>'"
            )
        return set_project_skills(
            repo, args.project, list(args.skills), line=args.line, role=args.role
        ).to_dict()
    raise SkillsError(f"unknown project action {action!r}")
