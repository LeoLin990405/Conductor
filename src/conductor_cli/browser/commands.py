"""Local CLI commands for the managed browser owner."""

from __future__ import annotations

import argparse
import json
import os
import stat
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..client import CLIError
from .service import MAX_INPUT_BYTES, parse_flow
from .transport import browser_command, close_owner, validate_agent


def register_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    browser = commands.add_parser("browser", help="control an isolated local agent browser")
    actions = browser.add_subparsers(dest="browser_command", required=True)

    native_commands = actions.add_parser("commands", help="list native browser commands and site adapters")
    native_commands.add_argument("--agent", required=True)
    native_commands.set_defaults(handler=handle_native)

    native_help = actions.add_parser("help", help="show one native browser command schema")
    native_help.add_argument("name", nargs="?")
    native_help.add_argument("--agent", required=True)
    native_help.set_defaults(handler=handle_native)

    native_site = actions.add_parser("site", help="run a registered site adapter action")
    native_site.add_argument("site")
    native_site.add_argument("action")
    native_site.add_argument("url")
    native_site.add_argument("--args", default="{}", help="adapter arguments as a JSON object")
    native_site.add_argument("--agent", required=True)
    native_site.set_defaults(handler=handle_native)

    native_read = actions.add_parser("read", help="read a URL with a matching adapter or semantic snapshot")
    native_read.add_argument("url")
    native_read.add_argument("--site", help="require a named registered adapter")
    native_read.add_argument("--agent", required=True)
    native_read.set_defaults(handler=handle_native)

    native_eval = actions.add_parser("eval", help="evaluate page JavaScript for metadata only")
    native_eval.add_argument("script")
    native_eval.add_argument("--enabled", action="store_true", required=True, help="explicitly enable evaluation for this call")
    native_eval.add_argument("--agent", required=True)
    native_eval.set_defaults(handler=handle_native)

    native_network = actions.add_parser("network", help="capture metadata for one navigation")
    native_network.add_argument("url")
    native_network.add_argument("--enabled", action="store_true", required=True, help="explicitly enable capture for this call")
    native_network.add_argument("--agent", required=True)
    native_network.set_defaults(handler=handle_native)

    open_parser = actions.add_parser("open", help="start the managed headless browser")
    open_parser.add_argument("--agent", required=True)
    open_parser.set_defaults(handler=handle_open)

    snapshot = actions.add_parser("snapshot", help="return the current semantic page tree")
    snapshot.add_argument("--agent", required=True)
    snapshot.set_defaults(handler=handle_snapshot)

    run = actions.add_parser("run", help="execute a bounded JSON or YAML flow")
    run.add_argument("--agent", required=True)
    run.add_argument("--input", required=True, type=Path, help="flow file containing a steps list")
    run.add_argument("--format", choices=("json", "yaml"), default="json", help="response format")
    run.set_defaults(handler=handle_run)

    import_login = actions.add_parser("import-login", help="import only allowlisted login storage")
    import_login.add_argument("--agent", required=True)
    import_login.add_argument("--state", required=True, type=Path, help="private Playwright storage-state JSON")
    import_login.add_argument("--domain", required=True, action="append", help="exact DNS domain; repeat for each domain")
    import_login.set_defaults(handler=handle_import_login)

    close = actions.add_parser("close", help="close Chromium and stop its owner process")
    close.add_argument("--agent", required=True)
    close.set_defaults(handler=handle_close)

    install = actions.add_parser("install-skill", help="install the agent-browser skill without overwriting files")
    install.add_argument("--source", type=Path, help="skill source directory")
    install.add_argument("--target", type=Path, action="append", help="complete destination path; repeat as needed")
    install.set_defaults(handler=handle_install_skill)


def _read_flow(path: Path) -> tuple[str, str | None]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise CLIError("Could not safely open the flow file") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_INPUT_BYTES:
            raise CLIError("Flow file must be a regular file no larger than 256 KB")
        data = os.read(descriptor, MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise CLIError("Flow file is too large")
    finally:
        os.close(descriptor)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise CLIError("Flow file must use UTF-8") from None
    suffix = path.suffix.lower()
    input_format = "json" if suffix == ".json" else "yaml" if suffix in {".yaml", ".yml"} else None
    return text, input_format


def handle_open(args: argparse.Namespace, client: Any = None) -> dict[str, Any]:
    del client
    browser_command(args.agent, {"command": "ping"}, start=True)
    return {"status": "open", "agent": args.agent}


def handle_native(args: argparse.Namespace, client: Any = None) -> dict[str, Any]:
    del client
    validate_agent(args.agent)
    command = args.browser_command
    if command in {"commands", "help"}:
        from .service import discovery_response

        return discovery_response(command, getattr(args, "name", None))
    request: dict[str, Any] = {"command": command}
    if command == "help" and args.name is not None:
        request["name"] = args.name
    elif command == "site":
        try:
            adapter_args = json.loads(args.args)
        except json.JSONDecodeError as exc:
            raise CLIError("--args must be valid JSON") from exc
        if not isinstance(adapter_args, dict):
            raise CLIError("--args must be a JSON object")
        request.update({"site": args.site, "action": args.action, "url": args.url, "args": adapter_args})
    elif command == "read":
        request["url"] = args.url
        if args.site:
            request["site"] = args.site
    elif command == "eval":
        request.update({"script": args.script, "enabled": args.enabled})
    elif command == "network":
        request.update({"url": args.url, "enabled": args.enabled})
    return browser_command(args.agent, request, start=True, timeout=25.0)


def handle_snapshot(args: argparse.Namespace, client: Any = None) -> dict[str, Any]:
    del client
    return browser_command(args.agent, {"command": "snapshot"})


def handle_run(args: argparse.Namespace, client: Any = None) -> dict[str, Any]:
    del client
    text, input_format = _read_flow(args.input.expanduser().absolute())
    steps = parse_flow(text, format=input_format)
    result = browser_command(args.agent, {"command": "run", "steps": steps}, timeout=70.0)
    if any(step.get("status") == "error" for step in result.get("steps", [])):
        result["exit_code"] = 1
    return result


def handle_import_login(args: argparse.Namespace, client: Any = None) -> dict[str, Any]:
    del client
    return browser_command(
        args.agent,
        {"command": "import-login", "path": str(args.state.expanduser().absolute()), "domains": args.domain},
        timeout=20.0,
    )


def handle_close(args: argparse.Namespace, client: Any = None) -> dict[str, Any]:
    del client
    return close_owner(args.agent)


def handle_install_skill(args: argparse.Namespace, client: Any = None) -> dict[str, Any]:
    del client
    from .install import install_skills

    result = install_skills(args.source, args.target)
    payload = asdict(result)
    if result.failed:
        payload["exit_code"] = 1
    return payload
