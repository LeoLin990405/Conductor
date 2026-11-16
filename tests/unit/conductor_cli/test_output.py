"""Envelope schema, exit codes, version, and shell completion."""

from __future__ import annotations

import json
import re
import subprocess
from argparse import ArgumentParser
from typing import Any

import httpx
import pytest

from src.conductor_cli import cli
from src.conductor_cli.client import APIClient, APIError, CLIError, ServerError
from src.conductor_cli.output import (
    API_VERSION,
    build_completion,
    build_envelope,
    cli_version,
    command_label,
)

ENVELOPE_KEYS = {"api_version", "command", "timestamp", "data", "errors", "exit_code"}
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@pytest.fixture(autouse=True)
def _no_operator_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The command must not read the operator's own token file.

    ``APIClient`` reads the token of the installation it runs in when it is
    built without one, and that token lives in the operator's home. A test
    that runs a command therefore depends on the credentials of whoever
    runs the tests: on a machine whose token file is missing the command
    works, and on one whose token file has loose permissions the command
    refuses -- so the test fails on a machine that has nothing to do with
    what it is testing. "No token" is the state of a machine that has not
    been set up yet, and that is the state these tests mean.
    """
    monkeypatch.setattr("src.conductor_cli.client.read_token", lambda: None)


def _parse_args(argv: list[str]):
    return cli.build_parser().parse_args(argv)


def test_envelope_shape_and_timestamp() -> None:
    envelope = build_envelope("tickets list", data={"ok": True})
    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["api_version"] == API_VERSION == "v1"
    assert envelope["command"] == "tickets list"
    assert envelope["data"] == {"ok": True}
    assert envelope["errors"] is None
    assert envelope["exit_code"] == 0
    assert TIMESTAMP_RE.match(envelope["timestamp"])


def test_envelope_errors_and_exit_code() -> None:
    envelope = build_envelope(
        "agents fleet",
        data=None,
        errors=[{"code": "server_error", "message": "HTTP 503 from Conductor"}],
        exit_code=3,
    )
    assert envelope["data"] is None
    assert envelope["errors"][0]["code"] == "server_error"
    assert envelope["exit_code"] == 3


def test_command_label_flattens_subcommands() -> None:
    assert command_label(_parse_args(["tickets", "create", "--workspace", "w", "--prompt", "p"])) == "tickets create"
    assert command_label(_parse_args(["daemons", "status", "d1"])) == "daemons status"
    assert command_label(_parse_args(["status"])) == "status"
    assert command_label(_parse_args(["completion", "bash"])) == "completion"


def test_parser_covers_every_top_level_command() -> None:
    tree_actions = [
        action
        for action in cli.build_parser()._actions  # noqa: SLF001
        if hasattr(action, "choices") and isinstance(getattr(action, "choices", None), dict)
    ]
    assert tree_actions
    commands = set(tree_actions[0].choices)
    for required in (
        "version",
        "completion",
        "lines",
        "dispatch",
        "review",
        "merge",
        "deploy",
        "agents",
        "models",
        "memory",
        "skills",
        "tickets",
        "sources",
        "daemons",
        "usage",
        "status",
        "health",
        "logs",
        "auth",
        "deliberation",
        "rollback",
        "install-shim",
        "github",
    ):
        assert required in commands, f"missing command {required}"


def test_version_command_reports_distribution(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["version", "--json"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["command"] == "version"
    assert envelope["data"]["name"] == "conductor"
    assert envelope["data"]["version"] == cli_version()
    assert re.match(r"^\d+\.\d+\.\d+", envelope["data"]["version"]) or envelope["data"]["version"].endswith("+unknown")


def test_version_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["--version"])
    assert raised.value.code == 0
    assert "conductor" in capsys.readouterr().out


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_scripts_cover_commands(shell: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["completion", shell]) == 0
    script = capsys.readouterr().out
    assert "conductor" in script
    for command in (
        "tickets",
        "models",
        "agents",
        "usage",
        "version",
        "completion",
        "lines",
        "dispatch",
        "review",
        "merge",
        "memory",
        "skills",
    ):
        assert command in script
    assert "GH_TOKEN" not in script
    if shell == "bash":
        assert "complete -F _conductor conductor" in script
        subprocess.run(["bash", "-n"], input=script, text=True, check=True, capture_output=True)
    elif shell == "zsh":
        assert script.startswith("#compdef conductor")
        subprocess.run(["zsh", "-n"], input=script, text=True, check=True, capture_output=True)
    else:
        assert "__fish_use_subcommand" in script


def test_completion_json_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["completion", "bash", "--json"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["command"] == "completion"
    assert envelope["data"]["shell"] == "bash"
    assert "complete -F _conductor conductor" in envelope["data"]["script"]


def test_completion_rejects_unknown_shell() -> None:
    with pytest.raises(SystemExit) as raised:
        cli.build_parser().parse_args(["completion", "tcsh"])
    assert raised.value.code == 2


def test_build_completion_walks_nested_subparsers() -> None:
    parser = ArgumentParser()
    top = parser.add_subparsers(dest="command", required=True)
    group = top.add_parser("group")
    group_sub = group.add_subparsers(dest="group_command", required=True)
    group_sub.add_parser("child")
    script = build_completion("bash", parser)
    assert "group" in script
    assert "child" in script


def test_client_maps_status_codes_to_exit_classes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ok":
            return httpx.Response(200, json={"ok": True})
        if request.url.path.startswith("/bad"):
            return httpx.Response(404, json={"detail": "nope"})
        return httpx.Response(503, json={"detail": "down"})

    client = APIClient("http://127.0.0.1:8000", token="", transport=httpx.MockTransport(handler))
    try:
        assert client.get("/ok") == {"ok": True}
        with pytest.raises(APIError) as bad:
            client.get("/bad")
        assert bad.value.status_code == 404
        assert isinstance(bad.value, CLIError)
        with pytest.raises(ServerError, match="HTTP 503"):
            client.get("/down")
    finally:
        client.close()


def test_server_error_exits_three_with_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(**kwargs: Any) -> APIClient:
        raise ServerError("Could not reach Conductor at http://127.0.0.1:8000: refused")

    monkeypatch.setattr(cli, "APIClient", boom)
    assert cli.main(["agents", "list", "--json"]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    envelope = json.loads(captured.err)
    assert envelope["exit_code"] == 3
    assert envelope["errors"][0]["code"] == "server_error"
    assert envelope["data"] is None
    assert envelope["command"] == "agents list"


def test_cli_error_exits_one_with_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CONDUCTOR_URL", "not-a-url")
    assert cli.main(["agents", "list", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    envelope = json.loads(captured.err)
    assert envelope["exit_code"] == 1
    assert envelope["errors"][0]["code"] == "cli_error"


def test_parse_error_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["definitely-not-a-command"])
    assert raised.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_keyboard_interrupt_exits_130(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(args: Any, client: Any) -> Any:
        raise KeyboardInterrupt

    def parser_with_interrupted_handler() -> ArgumentParser:
        parser = ArgumentParser(prog="conductor")
        parser.add_argument("--url")
        parser.add_argument("--json", action="store_true")
        sub = parser.add_subparsers(dest="command", required=True)
        agents = sub.add_parser("agents")
        agents_sub = agents.add_subparsers(dest="agents_command", required=True)
        listing = agents_sub.add_parser("list")
        listing.set_defaults(handler=interrupted)
        return parser

    monkeypatch.setattr(cli, "build_parser", parser_with_interrupted_handler)
    assert cli.main(["agents", "list", "--json"]) == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    envelope = json.loads(captured.err)
    assert envelope["exit_code"] == 130
    assert envelope["errors"][0]["code"] == "interrupted"
