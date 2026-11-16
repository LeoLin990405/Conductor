"""``collab tasks --claimable`` is the default made explicit; ``--all`` widens it.

Pure argument mapping: no network, keychain or HOME is touched.
"""

from __future__ import annotations

import argparse

import pytest

from src.conductor_cli import collab_cli


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    collab_cli.register_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(["collab", "tasks", *argv, "--app", "codex"])


def test_default_sends_no_claimable_and_server_default_is_claimable_only() -> None:
    tool, payload = collab_cli.tool_input(_parse())
    assert tool == "task_list"
    assert "claimable" not in payload


def test_claimable_flag_is_explicit_true() -> None:
    _, payload = collab_cli.tool_input(_parse("--claimable"))
    assert payload == {"claimable": True}


def test_all_flag_sends_false() -> None:
    _, payload = collab_cli.tool_input(_parse("--all"))
    assert payload == {"claimable": False}


def test_claimable_and_all_are_mutually_exclusive(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        _parse("--claimable", "--all")
    assert exc.value.code == 2
    assert "not allowed with" in capsys.readouterr().err
