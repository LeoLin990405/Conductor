"""Local commands for listing and installing bundled agent skills."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .catalog import AGENT_ADAPTERS, SKILL_CATALOG, get_adapters, get_skill, repository_root


def register_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    hub = commands.add_parser("skills-hub", help="discover and install bundled agent skills")
    actions = hub.add_subparsers(dest="skills_hub_command", required=True)

    listing = actions.add_parser("list", help="list bundled skills and supported agent CLIs")
    listing.set_defaults(handler=handle_list)

    install = actions.add_parser("install", help="install a bundled skill without overwriting files")
    install.add_argument("skill", choices=tuple(item.name for item in SKILL_CATALOG))
    install.add_argument("--agent", action="append", choices=tuple(item.name for item in AGENT_ADAPTERS))
    install.add_argument("--source", type=Path, help=argparse.SUPPRESS)
    install.set_defaults(handler=handle_install)


def handle_list(args: argparse.Namespace, client: Any = None) -> dict[str, Any]:
    del args, client
    return {
        "skills": [asdict(skill) for skill in SKILL_CATALOG],
        "adapters": [asdict(adapter) for adapter in AGENT_ADAPTERS],
    }


def handle_install(args: argparse.Namespace, client: Any = None) -> dict[str, Any]:
    del client
    from ..browser.install import install_skills

    try:
        skill = get_skill(args.skill)
        adapters = get_adapters(tuple(args.agent) if args.agent else None)
    except ValueError as exc:
        return {"installed": (), "already_present": (), "failed": (("catalog", str(exc)),), "exit_code": 1}

    source = args.source
    if source is None:
        bundled = Path(__file__).resolve().parents[1] / "browser" / "skill"
        source = bundled if bundled.is_dir() else repository_root() / skill.source_directory
    targets = tuple(adapter.destination(Path.home(), skill.name) for adapter in adapters)
    result = install_skills(source, targets)
    payload = asdict(result)
    if result.failed:
        payload["exit_code"] = 1
    return payload
