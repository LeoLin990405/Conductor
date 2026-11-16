"""scripts/cond forwards every verb the ops CLI defines; Makefile dev/web targets.

The wrapper runs against a stub interpreter (CONDUCTOR_PYTHON) in a temporary
checkout, so no backend starts and the Super-agent CLI is never opened.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from src.conductor_cli.ops_cli import FORWARDED_VERBS, build_parser

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "scripts" / "cond"
# The wrapper maps these aliases to the verb it hands the ops CLI.
ALIASES = {"start": "up", "stop": "down"}


def ops_verbs() -> list[str]:
    parser = build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(action.choices)
    raise AssertionError("ops CLI has no subcommands")


def _run_wrapper(tmp_path: Path, *args: str) -> tuple[int, list[str], str]:
    log = tmp_path / "argv.log"
    log.unlink(missing_ok=True)
    stub = tmp_path / "python-stub"
    stub.write_text(
        "#!/bin/sh\n" f'printf "%s\\n" "$*" >> "{log}"\n' "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "CONDUCTOR_PYTHON": str(stub),
        "CONDUCTOR_REPO_ROOT": str(repo),
        "CONDUCTOR_BACKEND_PORT": "54399",
    }
    result = subprocess.run(
        ["bash", str(WRAPPER), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result.returncode, calls, result.stderr


def test_ops_cli_defines_setup_and_agents() -> None:
    verbs = ops_verbs()
    assert {"setup", "agents", "up", "down", "restart", "doctor"} <= set(verbs)


@pytest.mark.parametrize("verb", ops_verbs())
def test_wrapper_forwards_every_ops_verb(tmp_path: Path, verb: str) -> None:
    code, calls, stderr = _run_wrapper(tmp_path, verb, "--json")
    expected = ALIASES.get(verb, verb)
    # One call, straight to the ops CLI. A verb that is not forwarded runs
    # `up` first and then looks for the Super-agent CLI.
    assert calls == [f"-m src.conductor_cli.ops_cli {expected} --json"], stderr
    assert code == 0, stderr


def test_wrapper_forwards_workspace_to_the_conductor_cli(tmp_path: Path) -> None:
    """``cond workspace`` reaches the registry, not stack startup + the TUI."""
    code, calls, stderr = _run_wrapper(tmp_path, "workspace", "list", "--json")
    assert calls == ["-m src.conductor_cli.cli workspace list --json"], stderr
    assert code == 0, stderr


@pytest.mark.parametrize("verb", sorted(FORWARDED_VERBS))
def test_wrapper_forwards_operator_cli_verbs_to_the_conductor_cli(tmp_path: Path, verb: str) -> None:
    """``cond apps`` reaches ``conductor apps``, not stack startup + the TUI."""
    code, calls, stderr = _run_wrapper(tmp_path, verb, "list", "--json")
    assert calls == [f"-m src.conductor_cli.cli {verb} list --json"], stderr
    assert code == 0, stderr


def test_forwarded_verbs_are_conductor_commands_not_ops_verbs() -> None:
    from src.conductor_cli.cli import build_parser as build_conductor_parser

    conductor: set[str] = set()
    for action in build_conductor_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            conductor = set(action.choices)
    assert FORWARDED_VERBS <= conductor
    assert not FORWARDED_VERBS & set(ops_verbs())


def test_unknown_words_still_go_to_the_super_agent(tmp_path: Path) -> None:
    code, calls, stderr = _run_wrapper(tmp_path, "-p", "hello")
    assert calls == ["-m src.conductor_cli.ops_cli up"]
    assert code == 1 and "ptc-agent" in stderr


def _make_n(*args: str) -> str:
    if shutil.which("make") is None:
        pytest.skip("make is not installed")
    result = subprocess.run(
        ["make", "-n", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_make_dev_binds_loopback_by_default() -> None:
    assert "server.py --host 127.0.0.1 --reload" in _make_n("dev")
    assert "--host 0.0.0.0" in _make_n("dev", "DEV_HOST=0.0.0.0")


def test_make_web_builds_web_dist() -> None:
    out = _make_n("web")
    assert "cd web && pnpm build" in out
