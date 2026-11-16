"""N13: Docker Git probes match the host delivery check's hardening.

Fake-Docker regressions go through ``launch`` against a local Git repository.
Parity assertions compare the argv/env the host delivery path actually sends
(via ``git_command_runner``) with the in-container version probe ``launch``
runs — not helper output against helper-derived constants.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from src.server.services.autonomy import docker_worker_runtime as runtime
from src.server.services.autonomy.git_delivery import (
    GitDeliveryError,
    git_command_runner,
    uncommitted_git_summary,
)
from tests.unit.server.services.autonomy.test_docker_worker_runtime import (
    _CODEX_STDOUT,
    _patch_launch,
    _settings,
)


#: Literal pins the delivery check must send. Kept as literals so a helper that
#: drifts from ``_run_git`` cannot make this file pass against itself.
_EXPECTED_PIN_SETTINGS = {
    "core.fsmonitor=false",
    "core.hooksPath=/dev/null",
    "core.untrackedCache=false",
    "core.excludesFile=/dev/null",
    "core.attributesFile=/dev/null",
    "core.fileMode=true",
    "core.symlinks=true",
    "core.autocrlf=false",
    "core.ignorecase=false",
    "core.checkStat=default",
    "core.trustctime=true",
    "core.ignoreStat=false",
    "core.useReplaceRefs=false",
    "diff.external=",
    "submodule.recurse=false",
    "gc.auto=0",
    "maintenance.auto=false",
    "commit.gpgSign=false",
    "merge.verifySignatures=false",
    "diff.ignoreSubmodules=none",
    "diff.relative=false",
    "status.showUntrackedFiles=all",
    "status.relativePaths=true",
    "status.renames=true",
    "core.quotePath=true",
}
_EXPECTED_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_NO_REPLACE_OBJECTS": "1",
}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*")


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(path: Path) -> Path:
    """A real Git workspace the Docker staging tree can mount."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Test Worker")
    _git(path, "config", "user.email", "worker@example.invalid")
    # A setting the probe must override via command-scope pins.
    _git(path, "config", "core.fileMode", "false")
    _git(path, "config", "core.hooksPath", str(path / "hooks"))
    (path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-q", "-m", "baseline")
    (path / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    return path


def _parse_env_i(argv: tuple[str, ...] | list[str]) -> tuple[dict[str, str], list[str]]:
    """Split ``docker exec … env -i KEY=VAL… <git argv>`` into env and git argv."""
    argv = list(argv)
    env_at = argv.index("env")
    assert argv[env_at + 1] == "-i"
    env: dict[str, str] = {}
    index = env_at + 2
    while index < len(argv) and _ENV_ASSIGN.match(argv[index]):
        key, _, value = argv[index].partition("=")
        env[key] = value
        index += 1
    assert index < len(argv) and Path(argv[index]).name == "git"
    return env, argv[index:]


def _version_probe_commands(command_order: list) -> list[tuple[str, ...]]:
    return [
        command
        for command in command_order
        if isinstance(command, tuple)
        and "env" in command
        and command[-1:] == ("--version",)
        and any(Path(part).name == "git" for part in command)
    ]


def _pin_sequence(argv: list[str] | tuple[str, ...]) -> list[str]:
    """The ordered ``-c`` settings of one Git argv."""
    return [str(argv[i + 1]) for i, part in enumerate(argv[:-1]) if part == "-c"]


def _fixed_env(env: dict[str, str]) -> dict[str, str]:
    """The env minus per-repository filter overrides (the probe names no repo)."""
    return {
        key: value
        for key, value in env.items()
        if not key.startswith(("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    }


def _assert_hardened(argv: list[str] | tuple[str, ...], env: dict[str, str]) -> None:
    assert Path(str(argv[0])).name == "git"
    assert _EXPECTED_PIN_SETTINGS <= set(argv)
    for key, value in _EXPECTED_ENV.items():
        assert env.get(key) == value, f"{key}={env.get(key)!r}"


def test_host_delivery_check_sends_canonical_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observe the real host delivery path, not the helper's own return value."""
    workspace = _repo(tmp_path / "host-delivery")
    observed: list[tuple[list[str], dict[str, str]]] = []

    def capture(argv, *, cwd, timeout, env):
        observed.append((list(argv), dict(env)))
        return subprocess.run(
            argv,
            cwd=cwd,
            timeout=timeout,
            env=env,
            capture_output=True,
            check=False,
        )

    with git_command_runner(capture):
        summary = uncommitted_git_summary(str(workspace))

    assert summary is not None
    assert "tracked.txt" in summary
    assert observed
    for argv, env in observed:
        _assert_hardened(argv, env)


@pytest.mark.asyncio
async def test_launch_version_probe_matches_host_delivery_hardening(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Production ``launch`` probe must carry the same pins/env as host delivery."""
    workspace, runner, _cleanup, _publish, _stage = _patch_launch(
        monkeypatch, tmp_path, _CODEX_STDOUT
    )
    _repo(workspace)

    host_observed: list[tuple[list[str], dict[str, str]]] = []

    def capture(argv, *, cwd, timeout, env):
        host_observed.append((list(argv), dict(env)))
        return subprocess.run(
            argv,
            cwd=cwd,
            timeout=timeout,
            env=env,
            capture_output=True,
            check=False,
        )

    with git_command_runner(capture):
        uncommitted_git_summary(str(workspace))

    report = await runtime.launch(
        {"ticket_id": "ticket-n13", "user_id": "u1", "workspace_id": "ws-n13"},
        "go",
    )

    probes = _version_probe_commands(runner.command_order)
    assert probes, "launch must run the hardened in-container git --version probe"
    probe_env, probe_argv = _parse_env_i(probes[0])
    assert probe_argv[-1] == "--version"
    _assert_hardened(probe_argv, probe_env)

    assert host_observed, "host delivery path must have run"
    host_argv, host_env = host_observed[0]
    _assert_hardened(host_argv, host_env)

    # Parity on the actual argv/env: every host invocation and the probe carry
    # the identical ordered pin list and the identical fixed environment.
    for argv, env in host_observed:
        assert _pin_sequence(argv) == _pin_sequence(probe_argv)
        assert _fixed_env(env) == _fixed_env(probe_env)
    assert set(_pin_sequence(probe_argv)) >= _EXPECTED_PIN_SETTINGS
    for key in _EXPECTED_ENV:
        assert host_env[key] == probe_env[key] == _EXPECTED_ENV[key]
    # The probe is ``git <pins> --version`` and nothing else.
    assert probe_argv[1:-1] == [
        item for pin in _pin_sequence(probe_argv) for item in ("-c", pin)
    ]

    assert report["git_check"] == "unsupported_runtime"
    assert "shared Git delivery check" in report["summary"]
    assert "The Worker image has no usable Git." not in report["summary"]


@pytest.mark.asyncio
async def test_launch_reports_missing_git_when_version_probe_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace, runner, _cleanup, _publish, _stage = _patch_launch(
        monkeypatch, tmp_path, _CODEX_STDOUT
    )
    _repo(workspace)

    # Only the docker boundary is faked: the production probe, runner and
    # _in_container_git run, and the image answers the probe with "not found".
    fake_sync = runtime._run_docker_sync

    def image_without_git(argv, *, timeout_seconds):
        if tuple(argv)[-1:] == ("--version",):
            runner.command_order.append(tuple(argv))
            return subprocess.CompletedProcess(argv, 127, b"", b"git: not found")
        return fake_sync(argv, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(runtime, "_run_docker_sync", image_without_git)

    report = await runtime.launch(
        {"ticket_id": "ticket-n13", "user_id": "u1", "workspace_id": "ws-n13"},
        "go",
    )

    assert _version_probe_commands(runner.command_order)
    assert report["git_check"] == "unsupported_runtime"
    assert "The Worker image has no usable Git." in report["summary"]
    # No verdict the live report path cannot persist is ever emitted.
    assert report.get("git_check") not in {"delivered", "uncommitted_changes", "unknown"}


def test_docker_git_runner_runs_the_host_argv_and_refuses_unhardened_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The runner executes exactly what the host path built, mapped to /workspace.

    It never re-applies pins of its own: an argv that did not come from the
    shared builder is refused before any docker exec.
    """
    workspace = _repo(tmp_path / "workspace")
    host_observed: list[tuple[list[str], dict[str, str]]] = []

    def capture(argv, *, cwd, timeout, env):
        host_observed.append((list(argv), dict(env)))
        return subprocess.run(
            argv,
            cwd=cwd,
            timeout=timeout,
            env=env,
            capture_output=True,
            check=False,
        )

    with git_command_runner(capture):
        uncommitted_git_summary(str(workspace))

    seen: list[list[str]] = []

    def fake_docker_sync(argv, *, timeout_seconds):
        seen.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, b"ok\n", b"")

    monkeypatch.setattr(runtime, "_run_docker_sync", fake_docker_sync)
    docker_git = runtime._docker_git_runner(_settings(), "container-name", workspace)

    status = next(item for item in host_observed if "status" in item[0])
    host_argv, host_env = status
    result = docker_git(tuple(host_argv), cwd=str(workspace), timeout=2, env=host_env)

    assert result.returncode == 0
    docker_env, docker_argv = _parse_env_i(seen[-1])
    expected = ["git", *host_argv[1:]]
    expected[expected.index("-C") + 1] = "/workspace"
    assert docker_argv == expected
    assert docker_env == {key: str(value) for key, value in host_env.items()}

    calls = len(seen)
    for argv, env in (
        # No pins at all.
        (("/usr/bin/git", "-C", str(workspace), "status", "--porcelain"), {"PATH": "/bin"}),
        # Pins present, but a later -c could outrank one.
        ((*host_argv[:-1], "-c", "core.fileMode=false", host_argv[-1]), host_env),
        # Pins present, but system config re-enabled.
        (tuple(host_argv), {**host_env, "GIT_CONFIG_NOSYSTEM": "0"}),
        # A host path the container cannot see.
        ((*host_argv[:-1], "--git-dir", str(workspace / ".git"), host_argv[-1]), host_env),
    ):
        with pytest.raises(GitDeliveryError):
            docker_git(argv, cwd=str(workspace), timeout=2, env=env)
    assert len(seen) == calls


def test_docker_git_runner_refuses_config_slots_that_enable_a_program(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """RV12: only the hardening's driver overrides may ride in GIT_CONFIG_*.

    Before, only ``filter.*`` slots were checked, so a ``merge.*.driver`` or
    ``core.fsmonitor`` slot passed straight into the container.
    """
    from src.server.services.autonomy.git_delivery import (
        GitDrivers,
        hardened_git_invocation,
    )

    workspace = _repo(tmp_path / "workspace")
    seen: list[list[str]] = []

    def fake_docker_sync(argv, *, timeout_seconds):
        seen.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, b"ok\n", b"")

    monkeypatch.setattr(runtime, "_run_docker_sync", fake_docker_sync)
    docker_git = runtime._docker_git_runner(_settings(), "container-name", workspace)
    argv, env = hardened_git_invocation(
        "git",
        ("-C", str(workspace), "status"),
        drivers=GitDrivers(merges=("m",), diffs=("d",)),
    )

    assert docker_git(argv, cwd=str(workspace), timeout=2, env=env).returncode == 0
    calls = len(seen)
    for key, value in (
        ("merge.m.driver", "touch /tmp/x"),
        ("core.fsmonitor", "touch /tmp/x"),
        ("diff.d.textconv", "cat"),
    ):
        smuggled = {**env, "GIT_CONFIG_KEY_0": key, "GIT_CONFIG_VALUE_0": value}
        with pytest.raises(GitDeliveryError):
            docker_git(argv, cwd=str(workspace), timeout=2, env=smuggled)
    assert len(seen) == calls
