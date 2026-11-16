"""Docker-isolated cloud Worker runtime: command construction and lifecycle.

All Docker interaction is faked; nothing here needs a live daemon or a real
container. The live-engine smoke is scripts/cloud_worker_smoke.py.
"""

import asyncio
import json
import os
import shutil
import stat
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.server.services.autonomy import (
    cloud_runtime,
    docker_worker_runtime as runtime,
)


#: The autouse fixture replaces the sweep on the module; the tests of the sweep
#: itself need the real one.
_REAL_SWEEP_LEFTOVERS = runtime.sweep_leftovers


def _settings(**overrides):
    """Default settings; /bin/sh stands in for a real Docker binary."""
    settings = runtime.DockerWorkerSettings.from_env(
        {"AUTONOMY_CLOUD_WORKER_DOCKER_BIN": "/bin/sh"}
    )
    return replace(settings, **overrides)


class _FakeSyncRunner:
    """Records argv and returns programmed (returncode, stdout, stderr) rows."""

    def __init__(self, results):
        self.calls: list[tuple[str, ...]] = []
        self._results = list(results)

    def __call__(self, argv, *, timeout_seconds=30.0):
        self.calls.append(tuple(argv))
        row = self._results.pop(0)
        if isinstance(row, BaseException):
            raise row
        returncode, stdout, stderr = row
        return runtime.DockerCommandResult(
            argv=tuple(argv), returncode=returncode, stdout=stdout, stderr=stderr
        )


@pytest.fixture(autouse=True)
def _clear_verify_cache():
    runtime.clear_verification_cache()
    try:
        yield
    finally:
        runtime.clear_verification_cache()


@pytest.fixture(autouse=True)
def _isolated_host(monkeypatch, tmp_path):
    """No test touches the real home, and none sweeps a real engine."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(runtime, "sweep_leftovers", MagicMock())
    monkeypatch.setattr(runtime, "_last_sweep", None)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_defaults_are_locked_down():
    settings = runtime.DockerWorkerSettings.from_env({})

    assert settings.image == "conductor-cloud-worker:latest"
    assert settings.provider == "codex"
    assert settings.network == "none"
    assert settings.env_names == ()
    assert settings.set_env == ()
    assert settings.mounts == ()
    assert settings.memory == "2g"
    assert float(settings.cpus) == 1.5
    assert settings.pids_limit == 512
    assert settings.timeout_seconds == 3600
    assert settings.user == f"{__import__('os').getuid()}:{__import__('os').getgid()}"
    assert settings.stage_root.is_absolute()
    # An unscoped sweep is the product default; scoping is an opt-in narrowing.
    assert settings.sweep_label is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("AUTONOMY_CLOUD_WORKER_IMAGE", "evil; rm -rf /"),
        ("AUTONOMY_CLOUD_WORKER_IMAGE", "image with space"),
        ("AUTONOMY_CLOUD_WORKER_NETWORK", "host"),
        ("AUTONOMY_CLOUD_WORKER_NETWORK", "container:other"),
        ("AUTONOMY_CLOUD_WORKER_MEMORY", "lots"),
        ("AUTONOMY_CLOUD_WORKER_CPUS", "0"),
        ("AUTONOMY_CLOUD_WORKER_CPUS", "99"),
        ("AUTONOMY_CLOUD_WORKER_PIDS_LIMIT", "1"),
        ("AUTONOMY_CLOUD_WORKER_TIMEOUT", "1"),
        ("AUTONOMY_CLOUD_WORKER_PROVIDER", "shell"),
        ("AUTONOMY_CLOUD_WORKER_USER", "root"),
        ("AUTONOMY_CLOUD_WORKER_ENV", "CONDUCTOR_DAEMON_TOKEN"),
        ("AUTONOMY_CLOUD_WORKER_ENV", "not-an-env-name"),
        ("AUTONOMY_CLOUD_WORKER_ENV", "HOME"),
        ("AUTONOMY_CLOUD_WORKER_SET_ENV", "CONDUCTOR_API_KEY=x"),
        ("AUTONOMY_CLOUD_WORKER_SET_ENV", "TMPDIR=/somewhere"),
        ("AUTONOMY_CLOUD_WORKER_MOUNTS", "/host:/var/run/docker.sock"),
        ("AUTONOMY_CLOUD_WORKER_MOUNTS", "/host:/workspace/sub"),
        ("AUTONOMY_CLOUD_WORKER_ROOT", "relative/path"),
        ("AUTONOMY_CLOUD_WORKER_SWEEP_LABEL", "no-value"),
        ("AUTONOMY_CLOUD_WORKER_SWEEP_LABEL", "key="),
        ("AUTONOMY_CLOUD_WORKER_SWEEP_LABEL", "=value"),
        ("AUTONOMY_CLOUD_WORKER_SWEEP_LABEL", "key=va lue"),
        ("AUTONOMY_CLOUD_WORKER_SWEEP_LABEL", 'key=v"} {{.ID}}'),
    ],
)
def test_unsafe_configuration_is_refused_at_resolve_time(key, value):
    with pytest.raises(runtime.DockerWorkerRuntimeUnavailable):
        runtime.DockerWorkerSettings.from_env({key: value})


def test_env_allowlist_and_literal_values_are_parsed():
    settings = runtime.DockerWorkerSettings.from_env(
        {
            "AUTONOMY_CLOUD_WORKER_ENV": "FOO, BAR",
            "AUTONOMY_CLOUD_WORKER_SET_ENV": "CODEX_HOME=/codex-home,EMPTY=",
        }
    )

    assert settings.env_names == ("FOO", "BAR")
    assert settings.set_env == (("CODEX_HOME", "/codex-home"), ("EMPTY", ""))


def test_mount_parser_requires_absolute_host_and_allows_read_only(monkeypatch):
    settings = runtime.DockerWorkerSettings.from_env(
        {"AUTONOMY_CLOUD_WORKER_MOUNTS": "/Users/you/.codex:/codex-home:ro"}
    )
    assert settings.mounts == (("/Users/you/.codex", "/codex-home", True),)

    with pytest.raises(runtime.DockerWorkerRuntimeUnavailable):
        runtime.DockerWorkerSettings.from_env(
            {"AUTONOMY_CLOUD_WORKER_MOUNTS": "relative:/codex-home:ro"}
        )
    with pytest.raises(runtime.DockerWorkerRuntimeUnavailable):
        runtime.DockerWorkerSettings.from_env(
            {"AUTONOMY_CLOUD_WORKER_MOUNTS": "/tmp:/"}
        )
    with pytest.raises(runtime.DockerWorkerRuntimeUnavailable):
        runtime.DockerWorkerSettings.from_env(
            {"AUTONOMY_CLOUD_WORKER_MOUNTS": "/tmp:/workspace"}
        )
    with pytest.raises(runtime.DockerWorkerRuntimeUnavailable):
        runtime.DockerWorkerSettings.from_env(
            {"AUTONOMY_CLOUD_WORKER_MOUNTS": "/tmp:/workspace/sub"}
        )


def test_forwarded_env_only_reads_listed_names(monkeypatch):
    monkeypatch.setenv("ALLOWED_VALUE", "forwarded")
    monkeypatch.setenv("UNLISTED_VALUE", "hidden")
    settings = _settings(env_names=("ALLOWED_VALUE",))

    env = runtime.resolve_container_env(settings)

    assert env == {"ALLOWED_VALUE": "forwarded"}
    assert "UNLISTED_VALUE" not in env
    assert "PATH" not in env
    assert "HOME" not in env


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def _argv(settings=None, **overrides):
    settings = settings or _settings()
    kwargs = {
        "name": "worker-1",
        "workspace": "/tmp/stage",
        "child_argv": ("codex", "exec", "-"),
        "env_vars": {},
    }
    kwargs.update(overrides)
    return runtime.build_container_argv(settings, **kwargs)


def test_container_argv_is_a_hardened_disposable_run():
    argv = _argv()

    assert argv[0] == "/bin/sh"
    assert "--rm" in argv
    name_index = argv.index("--name")
    assert argv[name_index + 1] == "worker-1"
    assert argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv
    assert "--init" in argv
    tmpfs_mounts = [argv[i + 1] for i, item in enumerate(argv) if item == "--tmpfs"]
    assert any("size=512m" in mount for mount in tmpfs_mounts)
    assert any(mount.startswith("/home/worker:") for mount in tmpfs_mounts)
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert argv[argv.index("--pids-limit") + 1] == "512"
    assert argv[argv.index("--memory") + 1] == "2g"
    # Swap would otherwise default to as much again as memory.
    assert argv[argv.index("--memory-swap") + 1] == "2g"
    assert argv[argv.index("--cpus") + 1] == "1.5"
    assert "-i" in argv
    # Exactly one bind mount: the private workspace staging tree.
    assert argv.count("--mount") == 1
    assert "type=bind,source=/tmp/stage,target=/workspace" in argv
    assert argv.count("-v") == 0
    assert argv[argv.index("-w") + 1] == "/workspace"
    assert "/var/run/docker.sock" not in " ".join(argv)
    assert argv[-4:] == ("conductor-cloud-worker:latest", "codex", "exec", "-")


def test_container_carries_an_expiry_label_beyond_its_lifetime():
    before = time.time()
    argv = _argv(lifetime_seconds=100)
    after = time.time()

    label = argv[argv.index("--label") + 1]
    key, _, value = label.partition("=")
    assert key == runtime._LABEL_DEADLINE
    # lifetime + grace: the Worker's own limit applies first, the sweep last.
    assert (
        before + 100 + runtime._REAP_GRACE_SECONDS - 1
        <= int(value)
        <= after + 100 + runtime._REAP_GRACE_SECONDS + 1
    )


@pytest.mark.parametrize(
    ("path", "mount"),
    [
        ("/Users/you/stage", "type=bind,source=/Users/you/stage,target=/workspace"),
        # A comma would start a new option; a quote must be doubled inside quotes.
        (
            "/Users/you/a,b",
            'type=bind,"source=/Users/you/a,b",target=/workspace',
        ),
        (
            '/Users/you/say "hi"',
            'type=bind,"source=/Users/you/say ""hi""",target=/workspace',
        ),
        (
            "/Users/you/a,readonly",
            'type=bind,"source=/Users/you/a,readonly",target=/workspace',
        ),
    ],
)
def test_the_workspace_mount_is_csv_quoted(path, mount):
    argv = _argv(workspace=path)
    assert argv[argv.index("--mount") + 1] == mount


def test_container_argv_uses_bridge_only_when_configured():
    argv = _argv(_settings(network="bridge"))
    assert argv[argv.index("--network") + 1] == "bridge"


def test_container_argv_mounts_workspace_read_only_for_speculative_runs():
    argv = _argv(read_only_workspace=True)
    mount = argv[argv.index("--mount") + 1]
    assert mount.endswith(",readonly")


def test_container_argv_keeps_forwarded_values_out_of_host_argv(monkeypatch):
    monkeypatch.setenv("FOO", "forwarded-value")
    settings = _settings(
        env_names=("FOO",), set_env=(("CODEX_HOME", "/codex-home"),)
    )
    env_vars = runtime.resolve_container_env(settings)
    argv = list(
        _argv(
            settings,
            env_vars=env_vars,
            env_file="/private/env-file",
        )
    )
    joined = " ".join(argv)
    env_values = [argv[index + 1] for index, item in enumerate(argv) if item == "-e"]

    assert "HOME=/home/worker" in env_values
    assert "TMPDIR=/tmp" in env_values
    # Codex refuses to build its helper aliases under the temp dir.
    assert not runtime._CONTAINER_HOME.startswith("/tmp")
    # Credential/config values travel in the env file, never on the host argv.
    assert "forwarded-value" not in joined
    assert "CODEX_HOME" not in joined
    assert "--env-file" in argv
    assert argv[argv.index("--env-file") + 1] == "/private/env-file"
    assert not any(value.startswith("PATH=") for value in env_values)


def test_env_file_transport_rejects_unrepresentable_values():
    with pytest.raises(runtime.DockerWorkerRuntimeUnavailable, match="newlines"):
        runtime._env_file_text({"TOKEN": "line-1\nline-2"})


def test_env_file_is_private_and_written_outside_the_mount(tmp_path):
    path = runtime._write_env_file(tmp_path / "root", {"TOKEN": "s3cret"})

    try:
        assert path.read_text() == "TOKEN=s3cret\n"
        assert path.stat().st_mode & 0o077 == 0
        assert path.parent == tmp_path / "root"
    finally:
        runtime._unlink_env_file(path)
    assert not path.exists()


def test_container_argv_adds_operator_mounts_only():
    settings = _settings(mounts=(("/Users/you/.codex", "/codex-home", True),))
    argv = _argv(settings)
    mounts = [argv[index + 1] for index, item in enumerate(argv) if item == "-v"]

    assert mounts == ["/Users/you/.codex:/codex-home:ro"]


def test_the_images_own_entrypoint_is_kept_unless_the_caller_overrides_it():
    assert "--entrypoint" not in _argv()

    argv = _argv(entrypoint="/bin/sh", child_argv=("-c", "true"))
    image = argv.index("conductor-cloud-worker:latest")
    assert argv[argv.index("--entrypoint") + 1] == "/bin/sh"
    # It is a docker run option, so it has to come before the image.
    assert argv.index("--entrypoint") < image
    assert argv[image + 1 :] == ("-c", "true")


def test_the_probe_runs_its_own_shell_not_the_images_entrypoint():
    settings = _settings()

    argv = runtime.isolation_probe_argv(
        settings, name="probe-1", workspace="/tmp/stage"
    )

    image = argv.index(settings.image)
    assert argv[argv.index("--entrypoint") + 1] == "/bin/sh"
    assert argv[image + 1] == "-c"
    assert argv[image + 2] == runtime.build_isolation_probe(settings)
    assert len(argv) == image + 3
    assert "type=bind,source=/tmp/stage,target=/workspace" in argv


def test_probe_argv_has_no_workspace_mount():
    argv = _argv(workspace=None, child_argv=("sh", "-c", "true"))
    assert "--mount" not in argv
    assert "type=bind" not in " ".join(argv)


def test_worker_argv_is_the_codex_one_shot_contract():
    assert runtime.build_worker_argv(_settings()) == [
        "codex",
        "exec",
        "--ignore-user-config",
        "--config",
        "project_doc_max_bytes=0",
        "--json",
        "--skip-git-repo-check",
        "--sandbox",
        "danger-full-access",
        "-C",
        "/workspace",
        "-",
    ]


@pytest.mark.asyncio
async def test_materialized_non_git_stages_do_not_lock_or_write_git_baselines(
    monkeypatch, tmp_path
):
    import asyncio
    from src.server.services.native_runtime import workspace as workspace_module

    real_stage_workspace = runtime._stage_workspace
    workspace, runner, _cleanup, _publish, _stage = _patch_launch(
        monkeypatch, tmp_path, _CODEX_STDOUT
    )
    monkeypatch.setattr(runtime, "_stage_workspace", real_stage_workspace)
    stages = []

    async def materialize_normal_workspace(*, cwd, **_kwargs):
        stage = Path(cwd)
        stages.append(stage)
        (stage / "tracked.txt").write_text("materialized file\n", encoding="utf-8")
        assert not (stage / ".git").exists()

    monkeypatch.setattr(workspace_module, "materialize_workspace", materialize_normal_workspace)
    both_workers_started = asyncio.Event()
    worker_starts = 0

    async def wait_for_both_workers(*_args, **_kwargs):
        nonlocal worker_starts
        worker_starts += 1
        if worker_starts == 2:
            both_workers_started.set()
        await asyncio.wait_for(both_workers_started.wait(), timeout=1)
        return runtime.DockerCommandResult(
            argv=("docker",), returncode=0, stdout=_CODEX_STDOUT, stderr=""
        )

    runner.worker_callback = wait_for_both_workers
    async def launch(ticket_id):
        return await runtime.launch(
            {
            "ticket_id": ticket_id,
            "user_id": "u1",
            "workspace_id": "same-workspace",
            "worker_thread_id": f"thread-{ticket_id}",
            "worker_run_id": f"run-{ticket_id}",
            "attempts": 1,
            "revision": 1,
            "status": "running",
            "mandate": {},
            },
            "do work",
        )

    reports = await asyncio.wait_for(
        asyncio.gather(launch("ticket-a"), launch("ticket-b")), timeout=3
    )

    assert worker_starts == 2
    assert len(stages) == 2 and stages[0] != stages[1]
    assert all(not (stage / ".git").exists() for stage in stages)
    assert all(report["state"] == "ready" for report in reports)
    assert all(report["git_check"] == "unsupported_runtime" for report in reports)
    assert all("git_check: unsupported_runtime" in report["summary"] for report in reports)
    assert not runner.git_failures


@pytest.mark.asyncio
async def test_docker_does_not_write_git_baseline_mandates(monkeypatch, tmp_path):
    workspace, runner, _cleanup, _publish, _stage = _patch_launch(
        monkeypatch, tmp_path, _CODEX_STDOUT
    )
    update_ticket = AsyncMock(side_effect=RuntimeError("database unavailable"))
    monkeypatch.setattr(
        "src.server.database.autonomy.tickets.update_ticket", update_ticket
    )

    report = await runtime.launch(
        {
            "ticket_id": "ticket-docker-no-git",
            "user_id": "u1",
            "workspace_id": "workspace-no-git",
            "worker_thread_id": "thread-no-git",
            "worker_run_id": "run-no-git",
            "attempts": 1,
            "revision": 1,
            "status": "running",
            "mandate": {},
        },
        "go",
    )

    assert runner.worker_argv
    assert report["state"] == "ready"
    assert report["git_check"] == "unsupported_runtime"
    update_ticket.assert_not_awaited()
    assert not runner.git_failures


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


_HARDENED_MOUNTINFO = (
    "36 35 0:51 / / ro,relatime - overlay overlay ro,lowerdir=/l\n"
    "40 36 0:52 / /tmp rw,nosuid,nodev,noexec - tmpfs tmpfs rw,size=524288k\n"
)
_UID, _GID = os.getuid(), os.getgid()
#: What /proc/self/status says inside a hardened container, with the identity
#: of the process running the test (the probe runs as it).
_HARDENED_STATUS = (
    "Name:\tsh\nCapEff:\t0000000000000000\nCapBnd:\t0000000000000000\nNoNewPrivs:\t1\n"
    f"Uid:\t{_UID}\t{_UID}\t{_UID}\t{_UID}\nGid:\t{_GID}\t{_GID}\t{_GID}\t{_GID}\n"
)


_POSIX_SH = pytest.mark.skipif(
    os.name != "posix" or shutil.which("sh") is None or os.getuid() == 0,
    reason="the probe script needs a POSIX shell and a non-root test user",
)


@pytest.fixture(
    params=[name for name in ("sh", "dash", "bash") if shutil.which(name)]
)
def shell(request):
    """The container's /bin/sh is dash, a laptop's is bash: the script must suit both."""
    return request.param


def _run_isolation_probe(
    tmp_path,
    shell="sh",
    *,
    mountinfo=_HARDENED_MOUNTINFO,
    status=_HARDENED_STATUS,
    env=None,
    **kwargs,
):
    """Run the real probe script against fixture /proc and cgroup files."""
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True, exist_ok=True)
    (proc / "self" / "mountinfo").write_text(mountinfo)
    (proc / "self" / "status").write_text(status)
    script = runtime._isolation_probe_script(
        str(proc), cgroup=str(tmp_path / "cgroup"), **kwargs
    )
    return subprocess.run(
        [shell, "-c", script], capture_output=True, text=True, check=False, env=env
    )


@_POSIX_SH
@pytest.mark.parametrize(
    ("mountinfo", "status", "failure"),
    [
        (_HARDENED_MOUNTINFO, _HARDENED_STATUS, None),
        # Writing to "/" fails for any non-root uid, so only the mount table
        # can tell a read-only root from a merely unprivileged one; a read-only
        # mount elsewhere must not satisfy the check.
        (
            _HARDENED_MOUNTINFO.replace("/ ro,relatime", "/ rw,relatime")
            + "41 36 0:53 / /data ro - ext4 /dev/x ro\n",
            _HARDENED_STATUS,
            "root filesystem is writable",
        ),
        (
            _HARDENED_MOUNTINFO,
            _HARDENED_STATUS.replace(
                "CapBnd:\t0000000000000000", "CapBnd:\t00000000a80425fb"
            ),
            "capabilities were not dropped",
        ),
        (
            _HARDENED_MOUNTINFO,
            _HARDENED_STATUS.replace("NoNewPrivs:\t1", "NoNewPrivs:\t0"),
            "no-new-privileges is not set",
        ),
    ],
)
def test_isolation_probe_reads_the_state_the_flags_change(
    tmp_path, shell, mountinfo, status, failure
):
    result = _run_isolation_probe(
        tmp_path, shell, mountinfo=mountinfo, status=status
    )

    if failure is None:
        assert result.returncode == 0, result.stderr
        assert "isolation-ok" in result.stdout
    else:
        assert result.returncode != 0
        assert failure in result.stderr
        assert "isolation-ok" not in result.stdout


def _write_cgroup(tmp_path, **files):
    cgroup = tmp_path / "cgroup"
    for relative, value in files.items():
        target = cgroup / relative.replace("__", "/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)


@_POSIX_SH
def test_isolation_probe_checks_the_user_the_container_runs_as(tmp_path, shell):
    me = f"{os.getuid()}:{os.getgid()}"

    assert _run_isolation_probe(tmp_path, shell, user=me).returncode == 0
    wrong = _run_isolation_probe(
        tmp_path, shell, user=f"{os.getuid() + 1}:{os.getgid()}"
    )
    assert wrong.returncode != 0
    assert "not running as the configured user" in wrong.stderr
    with pytest.raises(ValueError):
        runtime._isolation_probe_script(user="1000:1000; touch /pwned")


@_POSIX_SH
def test_isolation_probe_refuses_the_root_group(tmp_path, shell):
    """A non-root uid in group 0 reaches root-group files; the user is not enough."""
    status = _HARDENED_STATUS.replace(
        f"Gid:\t{_GID}\t{_GID}\t{_GID}\t{_GID}", "Gid:\t0\t0\t0\t0"
    )

    result = _run_isolation_probe(tmp_path, shell, status=status, user=f"{_UID}:0")

    assert result.returncode != 0
    assert "runs in the root group" in result.stderr


@_POSIX_SH
@pytest.mark.parametrize(
    ("kind", "files", "failure"),
    [
        ("pids", {"pids.max": "512\n"}, None),
        ("pids", {"pids__pids.max": "256\n"}, None),
        ("pids", {"pids.max": "513\n"}, "PID limit is looser than configured"),
        # cgroup v2 says "max" for an unlimited group: the engine did not apply it.
        ("pids", {"pids.max": "max\n"}, "PID limit is not enforced"),
        # No controller: the limit could not have been applied.
        ("pids", {}, "PID limit is not enforced"),
        ("memory", {"memory.max": "2147483648\n"}, None),
        # cgroup v1: the memory limit alone says nothing about swap; the
        # memory+swap limit must be recorded and equal to it.
        (
            "memory",
            {
                "memory__memory.limit_in_bytes": "2147483648\n",
                "memory__memory.memsw.limit_in_bytes": "2147483648\n",
            },
            None,
        ),
        (
            "memory",
            {
                "memory__memory.limit_in_bytes": "2147483648\n",
                "memory__memory.memsw.limit_in_bytes": "9223372036854771712\n",
            },
            "swap is not disabled",
        ),
        (
            "memory",
            {
                "memory__memory.limit_in_bytes": "2147483648\n",
                "memory__memory.memsw.limit_in_bytes": "4294967296\n",
            },
            "swap is not disabled",
        ),
        (
            "memory",
            {"memory__memory.limit_in_bytes": "2147483648\n"},
            "swap is not disabled",
        ),
        (
            "memory",
            {
                "memory__memory.limit_in_bytes": "2147483648\n",
                "memory__memory.memsw.limit_in_bytes": "max\n",
            },
            "swap is not disabled",
        ),
        (
            "memory",
            {
                "memory__memory.limit_in_bytes": "2147483648\n",
                "memory__memory.memsw.limit_in_bytes": "",
            },
            "swap is not disabled",
        ),
        ("memory", {"memory.max": "4294967296\n"}, "memory limit is looser"),
        ("memory", {"memory.max": "max\n"}, "memory limit is not enforced"),
        # cgroup v1's "unlimited" is a huge number, not a word.
        (
            "memory",
            {"memory__memory.limit_in_bytes": "9223372036854771712\n"},
            "memory limit is looser",
        ),
        (
            "memory",
            {"memory.max": "2147483648\n", "memory.swap.max": "2147483648\n"},
            "swap is not disabled",
        ),
        ("memory", {"memory.max": "2147483648\n", "memory.swap.max": "0\n"}, None),
    ],
)
def test_isolation_probe_reads_the_limits_the_engine_applied(
    tmp_path, shell, kind, files, failure
):
    _write_cgroup(tmp_path, **files)
    limit = {"pids_limit": 512} if kind == "pids" else {"memory_bytes": 2 * 1024**3}

    result = _run_isolation_probe(tmp_path, shell, **limit)

    if failure is None:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert failure in result.stderr


@_POSIX_SH
def test_isolation_probe_needs_a_writable_empty_workspace(tmp_path, shell):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mounted = _HARDENED_MOUNTINFO + f"42 36 0:54 / {workspace} rw,relatime - fuse x rw\n"

    ok = _run_isolation_probe(
        tmp_path, shell, mountinfo=mounted, workspace=str(workspace)
    )
    assert ok.returncode == 0, ok.stderr
    assert list(workspace.iterdir()) == []  # the probe file is removed again

    # A directory that merely exists in the image is not the bind mount.
    unmounted = _run_isolation_probe(tmp_path, shell, workspace=str(workspace))
    assert "workspace is not a bind mount" in unmounted.stderr

    (workspace / "left-over.txt").write_text("x")
    dirty = _run_isolation_probe(
        tmp_path, shell, mountinfo=mounted, workspace=str(workspace)
    )
    assert "unexpected workspace content" in dirty.stderr

    (workspace / "left-over.txt").unlink()
    workspace.chmod(0o500)
    try:
        readonly = _run_isolation_probe(
            tmp_path, shell, mountinfo=mounted, workspace=str(workspace)
        )
    finally:
        workspace.chmod(0o700)
    assert readonly.returncode != 0
    assert "workspace is not writable by the worker user" in readonly.stderr


# -- the probe fails closed -------------------------------------------------
#
# Every shape below is a check that used to be satisfied by *nothing*: a command
# that failed and printed nothing, a record that was missing, a line that did
# not parse. An empty answer must never be a passing one.


def _stub_command(tmp_path, name, body):
    """A fake ``name`` found first on PATH."""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    script = fake_bin / name
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return {"PATH": f"{fake_bin}:{os.environ['PATH']}"}


@_POSIX_SH
@pytest.mark.parametrize("field", ["Uid", "Gid"])
@pytest.mark.parametrize(
    "line",
    [
        None,  # no such line
        "{f}:\t",  # a line with nothing on it
        "{f}:\tzzz\t1\t1\t1",  # not a number
        "{f}:\t1000\t1000",  # too few ids
        "{f}:\t1000\t1000\t1000\t1000\t1000",  # too many
    ],
)
def test_the_probe_fails_when_it_cannot_read_the_uid_or_gid(
    tmp_path, shell, field, line
):
    """An empty answer is not "not root"; the ids come from the kernel's record."""
    lines = [l for l in _HARDENED_STATUS.splitlines() if not l.startswith(f"{field}:")]
    if line is not None:
        lines.append(line.format(f=field))
    status = "\n".join(lines) + "\n"

    result = _run_isolation_probe(
        tmp_path, shell, status=status, user=f"{_UID}:{_GID}"
    )

    assert result.returncode != 0
    assert f"cannot read the {field.lower()}" in result.stderr
    assert "isolation-ok" not in result.stdout


@_POSIX_SH
@pytest.mark.parametrize(
    "ids", ["0\t0\t0\t0", "0\t{u}\t{u}\t{u}", "{u}\t0\t{u}\t{u}", "{u}\t{u}\t{u}\t0"]
)
def test_the_probe_refuses_a_process_with_any_root_uid(tmp_path, shell, ids):
    status = _HARDENED_STATUS.replace(
        f"Uid:\t{_UID}\t{_UID}\t{_UID}\t{_UID}", "Uid:\t" + ids.format(u=_UID)
    )

    result = _run_isolation_probe(tmp_path, shell, status=status)

    assert result.returncode != 0
    assert "runs as root" in result.stderr


def _liars_only_on_path(tmp_path):
    """A PATH holding nothing but utilities that say what a hostile image would."""
    for name in ("grep", "id", "cat", "ls", "awk", "sed", "head", "tr"):
        _stub_command(tmp_path, name, "echo 0000000000000000; exit 0")
    return {"PATH": str(tmp_path / "fake-bin")}


@_POSIX_SH
@pytest.mark.parametrize(
    ("status", "failure"),
    [
        (
            _HARDENED_STATUS.replace(
                "CapBnd:\t0000000000000000", "CapBnd:\t00000000a80425fb"
            ),
            "capabilities were not dropped",
        ),
        (
            _HARDENED_STATUS.replace("NoNewPrivs:\t1", "NoNewPrivs:\t0"),
            "no-new-privileges is not set",
        ),
    ],
)
def test_a_lying_grep_cannot_make_bad_state_pass(tmp_path, shell, status, failure):
    """An image-supplied ``grep`` that always exits 0 used to pass these fixtures."""
    result = _run_isolation_probe(
        tmp_path, shutil.which(shell), status=status, env=_liars_only_on_path(tmp_path)
    )

    assert result.returncode != 0
    assert failure in result.stderr
    assert "isolation-ok" not in result.stdout


@_POSIX_SH
def test_a_lying_cat_cannot_make_an_unbounded_swap_limit_pass(tmp_path, shell):
    _write_cgroup(
        tmp_path,
        **{"memory.max": "2147483648\n", "memory.swap.max": "2147483648\n"},
    )

    result = _run_isolation_probe(
        tmp_path,
        shutil.which(shell),
        env=_liars_only_on_path(tmp_path),
        memory_bytes=2 * 1024**3,
    )

    assert result.returncode != 0
    assert "swap is not disabled" in result.stderr


@_POSIX_SH
def test_the_probe_runs_with_nothing_on_path_but_rm(tmp_path, shell):
    """Every check is a shell builtin or a read of a /proc or cgroup file; ``rm`` is
    the one external command (the shell has no way to delete the probe file)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mounted = _HARDENED_MOUNTINFO + f"42 36 0:54 / {workspace} rw,relatime - fuse x rw\n"
    only_rm = tmp_path / "only-rm"
    only_rm.mkdir()
    (only_rm / "rm").symlink_to(shutil.which("rm"))
    _write_cgroup(
        tmp_path,
        **{
            "pids.max": "512\n",
            "memory.max": "2147483648\n",
            "memory.swap.max": "0\n",
        },
    )

    result = _run_isolation_probe(
        tmp_path,
        shutil.which(shell),
        mountinfo=mounted,
        workspace=str(workspace),
        user=f"{_UID}:{_GID}",
        pids_limit=512,
        memory_bytes=2 * 1024**3,
        env={"PATH": str(only_rm)},
    )

    assert result.returncode == 0, result.stderr
    assert "isolation-ok" in result.stdout


@_POSIX_SH
@pytest.mark.parametrize(
    "mountinfo",
    [
        None,  # no record at all
        "",
        "this is not a mountinfo line\n",
        "36 35\n",  # too few fields to name a mount point
        "36 35 0:51 / /tmp ro - tmpfs tmpfs ro\n",  # a read-only mount, but not of /
        # The mount of / that is writable.
        _HARDENED_MOUNTINFO.replace("/ ro,relatime", "/ rw,relatime"),
        # Two mounts of /: one read-only does not excuse a writable one.
        _HARDENED_MOUNTINFO + "50 36 0:60 / / rw,relatime - overlay overlay rw\n",
        # "ro" as a substring of an option is not the option.
        _HARDENED_MOUNTINFO.replace("/ ro,relatime", "/ rw,errors=remount-ro"),
    ],
)
def test_the_probe_fails_without_a_read_only_root_mount_record(
    tmp_path, shell, mountinfo
):
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "self" / "status").write_text(_HARDENED_STATUS)
    if mountinfo is not None:
        (proc / "self" / "mountinfo").write_text(mountinfo)
    script = runtime._isolation_probe_script(
        str(proc), cgroup=str(tmp_path / "cgroup")
    )

    result = subprocess.run(
        [shell, "-c", script], capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert "root filesystem is writable" in result.stderr
    assert "isolation-ok" not in result.stdout


@_POSIX_SH
@pytest.mark.parametrize("field", ["CapBnd", "NoNewPrivs"])
@pytest.mark.parametrize("shape", ["missing-file", "empty", "no-line", "garbage"])
def test_the_probe_fails_without_a_parsable_capability_or_privilege_record(
    tmp_path, shell, field, shape
):
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "self" / "mountinfo").write_text(_HARDENED_MOUNTINFO)
    lines = [
        line
        for line in _HARDENED_STATUS.splitlines()
        if not line.startswith(f"{field}:")
    ]
    if shape == "garbage":
        lines.append(f"{field}:\tzzzz")
    status = {
        "missing-file": None,
        "empty": "",
        "no-line": "\n".join(lines) + "\n",
        "garbage": "\n".join(lines) + "\n",
    }[shape]
    if status is not None:
        (proc / "self" / "status").write_text(status)
    script = runtime._isolation_probe_script(
        str(proc), cgroup=str(tmp_path / "cgroup")
    )

    result = subprocess.run(
        [shell, "-c", script], capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert "isolation-ok" not in result.stdout
    # With no usable status file at all the identity check is the first to fail.
    if shape in {"missing-file", "empty"}:
        expected = "cannot read the uid"
    elif field == "CapBnd":
        expected = "capabilities were not dropped"
    else:
        expected = "no-new-privileges is not set"
    assert expected in result.stderr


@_POSIX_SH
def test_the_probe_fails_on_an_unreadable_swap_record(tmp_path, shell):
    _write_cgroup(tmp_path, **{"memory.max": "2147483648\n", "memory.swap.max": "0\n"})
    swap = tmp_path / "cgroup" / "memory.swap.max"
    swap.chmod(0o000)
    try:
        result = _run_isolation_probe(tmp_path, shell, memory_bytes=2 * 1024**3)
    finally:
        swap.chmod(0o600)

    assert result.returncode != 0
    assert "swap is not disabled" in result.stderr


@_POSIX_SH
def test_the_probe_fails_on_an_unreadable_v1_swap_record(tmp_path, shell):
    _write_cgroup(
        tmp_path,
        **{
            "memory__memory.limit_in_bytes": "2147483648\n",
            "memory__memory.memsw.limit_in_bytes": "2147483648\n",
        },
    )
    memsw = tmp_path / "cgroup" / "memory" / "memory.memsw.limit_in_bytes"
    memsw.chmod(0o000)
    try:
        result = _run_isolation_probe(tmp_path, shell, memory_bytes=2 * 1024**3)
    finally:
        memsw.chmod(0o600)

    assert result.returncode != 0
    assert "swap is not disabled" in result.stderr


@_POSIX_SH
@pytest.mark.parametrize(
    "record",
    [
        # The finding's example: right root field, wrong mount point.
        "41 36 0:53 {ws} /elsewhere rw,relatime - ext4 /dev/x rw\n",
        # The path only appears as the mount source.
        "41 36 0:53 / /elsewhere rw,relatime - ext4 {ws} rw\n",
        # A parent of the path is mounted, not the path itself.
        "41 36 0:53 / {parent} rw,relatime - ext4 /dev/x rw\n",
        # A longer path that merely starts with it.
        "41 36 0:53 / {ws}-other rw,relatime - ext4 /dev/x rw\n",
        "not a record\n",
    ],
)
def test_the_probe_wants_the_workspace_itself_as_the_mount_point(
    tmp_path, shell, record
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mounted = _HARDENED_MOUNTINFO + record.format(
        ws=workspace, parent=workspace.parent
    )

    result = _run_isolation_probe(
        tmp_path, shell, mountinfo=mounted, workspace=str(workspace)
    )

    assert result.returncode != 0
    assert "workspace is not a bind mount" in result.stderr
    assert "isolation-ok" not in result.stdout


@_POSIX_SH
@pytest.mark.parametrize(
    ("mode", "failure"),
    [
        # Writable but not readable: a glob sees an empty directory that is not.
        (0o300, "workspace cannot be listed"),
        (0o600, "workspace is not writable by the worker user"),
        (0o200, "workspace is not writable by the worker user"),
    ],
)
def test_the_probe_fails_when_the_workspace_cannot_be_listed(
    tmp_path, shell, mode, failure
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hidden.txt").write_text("x")
    mounted = _HARDENED_MOUNTINFO + f"42 36 0:54 / {workspace} rw,relatime - fuse x rw\n"
    workspace.chmod(mode)
    try:
        result = _run_isolation_probe(
            tmp_path, shell, mountinfo=mounted, workspace=str(workspace)
        )
    finally:
        workspace.chmod(0o700)

    assert result.returncode != 0
    assert failure in result.stderr
    assert "isolation-ok" not in result.stdout


@pytest.mark.parametrize(
    ("memory", "expected"),
    [
        ("2g", 2 * 1024**3),
        ("512m", 512 * 1024**2),
        ("1.5g", 1610612736),
        ("64k", 65536),
        ("4096", 4096),
        ("10B", 10),
    ],
)
def test_memory_limit_bytes_uses_dockers_binary_units(memory, expected):
    assert runtime.memory_limit_bytes(memory) == expected


def test_the_probe_command_checks_what_the_settings_ask_for():
    script = runtime.build_isolation_probe(_settings(user="501:20", pids_limit=64))

    assert '"501:20"' in script
    assert "-le 64" in script
    assert f"-le {2 * 1024**3}" in script
    assert "/workspace/.isolation-probe" in script


def _patch_settings(monkeypatch, settings):
    monkeypatch.setattr(
        runtime.DockerWorkerSettings, "from_env", staticmethod(lambda env=None: settings)
    )


def _verify_with(monkeypatch, settings, results):
    runner = _FakeSyncRunner(results)
    _patch_settings(monkeypatch, replace(settings, runner=runner))
    monkeypatch.setattr(runtime, "_rm_container_sync", lambda *_a, **_k: True)
    return runtime.verify_docker_runtime(), runner


def test_verification_passes_engine_image_isolation_and_cli(monkeypatch):
    (available, detail), runner = _verify_with(
        monkeypatch,
        _settings(),
        [
            (0, "29.5.2", ""),
            (0, "sha256:abc123", ""),
            (0, "isolation-ok", ""),
            (0, "codex-cli 0.162.1", ""),
        ],
    )

    assert available is True
    assert "docker 29.5.2" in detail
    assert "isolation probe ok" in detail
    assert "worker CLI codex present" in detail
    assert len(runner.calls) == 4
    # Every command after the engine query runs the hardened container.
    for argv in runner.calls[2:]:
        assert "--read-only" in argv
        assert argv[argv.index("--network") + 1] == "none"


def test_verification_runs_the_probe_under_its_own_shell_and_the_cli_under_the_image(
    monkeypatch,
):
    (available, _detail), runner = _verify_with(
        monkeypatch,
        _settings(),
        [
            (0, "29.5.2", ""),
            (0, "sha256:abc123", ""),
            (0, "isolation-ok\n", ""),
            (0, "codex-cli 0.162.1", ""),
        ],
    )

    assert available is True
    probe, cli = runner.calls[2], runner.calls[3]
    assert probe[probe.index("--entrypoint") + 1] == "/bin/sh"
    # The version check goes through the image the way a real launch does.
    assert "--entrypoint" not in cli
    assert cli[-2:] == ("codex", "--version")


@pytest.mark.parametrize(
    "probe_stdout",
    [
        "",  # an entrypoint that swallowed the probe and exited 0
        "\n",
        "hello from the image's entrypoint\n",
        "isolation-ok but not by itself\n",
        "isolation-ok\nand then something else\n",
    ],
)
def test_verification_needs_the_probes_success_line_not_just_exit_zero(
    monkeypatch, probe_stdout
):
    (available, detail), runner = _verify_with(
        monkeypatch,
        _settings(),
        [
            (0, "29.5.2", ""),
            (0, "sha256:abc123", ""),
            (0, probe_stdout, ""),
            (0, "codex-cli 0.162.1", ""),
        ],
    )

    assert available is False
    assert "isolation probe failed" in detail
    assert "success line" in detail
    assert len(runner.calls) == 3  # the CLI check was never reached


@pytest.mark.parametrize(
    "cli_stdout",
    [
        "isolation-ok\n",  # an image without the CLI whose entrypoint prints this
        "isolation-ok codex --version\n",  # an entrypoint that echoes its arguments
        "hello 1.2.3\n",  # a version of something else
        "mycodexish 1.2.3\n",  # the name as part of a longer word
        "codex\n",  # the name without a version
    ],
)
def test_verification_needs_the_cli_to_identify_itself_with_a_version(
    monkeypatch, cli_stdout
):
    (available, detail), _runner = _verify_with(
        monkeypatch,
        _settings(),
        [
            (0, "29.5.2", ""),
            (0, "sha256:abc123", ""),
            (0, "isolation-ok", ""),
            (0, cli_stdout, ""),
        ],
    )

    assert available is False
    assert "worker CLI 'codex' is not available" in detail
    assert "does not identify" in detail


@pytest.mark.parametrize(
    "cli_stdout",
    ["codex-cli 0.166.0\n", "WARNING: x\ncodex-cli 0.0.0-smoke-stub\n", "Codex 2.1\n"],
)
def test_verification_accepts_the_cli_naming_itself_and_a_version(
    monkeypatch, cli_stdout
):
    (available, _detail), _runner = _verify_with(
        monkeypatch,
        _settings(),
        [
            (0, "29.5.2", ""),
            (0, "sha256:abc123", ""),
            (0, "isolation-ok", ""),
            (0, cli_stdout, ""),
        ],
    )

    assert available is True


@pytest.mark.parametrize("cli_stdout", ["", "  \n"])
def test_verification_needs_the_cli_to_print_something(monkeypatch, cli_stdout):
    (available, detail), _runner = _verify_with(
        monkeypatch,
        _settings(),
        [
            (0, "29.5.2", ""),
            (0, "sha256:abc123", ""),
            (0, "isolation-ok", ""),
            (0, cli_stdout, ""),
        ],
    )

    assert available is False
    assert "worker CLI 'codex' is not available" in detail
    assert "printed nothing" in detail


# -- verification removes its own containers, and only then its directory ---


def _verify_with_real_removal(monkeypatch, tmp_path, results):
    """Verification with the real remover, fed by the same fake engine."""
    monkeypatch.setattr(runtime.time, "sleep", lambda _s: None)
    runner = _FakeSyncRunner(results)
    root = tmp_path / "root"
    _patch_settings(monkeypatch, _settings(runner=runner, stage_root=root))
    return runtime.verify_docker_runtime(), runner, root


_ENGINE = [(0, "29.5.2", ""), (0, "sha256:abc123", "")]
_RM_REFUSED = (1, "", "Error response from daemon: cannot remove")


def test_a_probe_container_that_cannot_be_removed_keeps_its_directory(
    monkeypatch, tmp_path
):
    """A timed-out probe may still be running and writing to the directory."""
    purge = MagicMock(side_effect=AssertionError("purge over a live probe"))
    monkeypatch.setattr(runtime, "_purge_tree", purge)

    (available, detail), runner, root = _verify_with_real_removal(
        monkeypatch,
        tmp_path,
        [
            *_ENGINE,
            runtime.DockerWorkerRuntimeUnavailable("Docker command timed out"),
            *[_RM_REFUSED] * runtime._RM_ATTEMPTS,
        ],
    )

    assert available is False
    purge.assert_not_called()
    left = [e for e in root.iterdir() if e.name.startswith("probe-")]
    assert sorted(e.name for e in left)[0] + ".owner" == sorted(e.name for e in left)[1]
    assert len(left) == 2  # the directory, still owned, and its marker
    removals = [c for c in runner.calls if c[1:3] == ("rm", "-f")]
    assert len(removals) == runtime._RM_ATTEMPTS


def test_a_passing_probe_whose_container_cannot_be_removed_still_fails_verification(
    monkeypatch, tmp_path
):
    purge = MagicMock(side_effect=AssertionError("purge over a live probe"))
    monkeypatch.setattr(runtime, "_purge_tree", purge)

    (available, detail), _runner, root = _verify_with_real_removal(
        monkeypatch,
        tmp_path,
        [*_ENGINE, (0, "isolation-ok\n", ""), *[_RM_REFUSED] * runtime._RM_ATTEMPTS],
    )

    assert available is False
    assert "could not be removed" in detail
    assert "left in place" in detail
    purge.assert_not_called()
    assert any(e.name.startswith("probe-") for e in root.iterdir())


def test_a_probe_container_removal_is_retried_before_its_directory_goes(
    monkeypatch, tmp_path
):
    (available, _detail), runner, root = _verify_with_real_removal(
        monkeypatch,
        tmp_path,
        [
            *_ENGINE,
            (0, "isolation-ok\n", ""),
            _RM_REFUSED,
            (0, "", ""),  # the retry removes it
            (0, "codex-cli 0.166.0", ""),
            (0, "", ""),  # rm of the version-check container
        ],
    )

    assert available is True
    assert list(root.iterdir()) == []


def test_a_probe_container_that_is_already_gone_counts_as_removed(
    monkeypatch, tmp_path
):
    (available, _detail), _runner, root = _verify_with_real_removal(
        monkeypatch,
        tmp_path,
        [
            *_ENGINE,
            (0, "isolation-ok\n", ""),
            (1, "", "Error response from daemon: No such container: x"),
            (0, "codex-cli 0.166.0", ""),
            (1, "", "Error response from daemon: No such container: y"),
        ],
    )

    assert available is True
    assert list(root.iterdir()) == []


def test_a_version_check_container_that_cannot_be_removed_fails_verification(
    monkeypatch, tmp_path
):
    (available, detail), _runner, _root = _verify_with_real_removal(
        monkeypatch,
        tmp_path,
        [
            *_ENGINE,
            (0, "isolation-ok\n", ""),
            (0, "", ""),
            (0, "codex-cli 0.166.0", ""),
            *[_RM_REFUSED] * runtime._RM_ATTEMPTS,
        ],
    )

    assert available is False
    assert "could not be removed" in detail


def test_verification_detail_warns_that_bridge_is_unfiltered(monkeypatch):
    (_available, detail), _runner = _verify_with(
        monkeypatch,
        _settings(network="bridge"),
        [(0, "29.5.2", ""), (0, "sha256:abc", ""), (0, "isolation-ok", ""), (0, "codex-cli 1.0.0", "")],
    )

    assert "network bridge (unfiltered egress" in detail
    assert "host loopback" in detail


def test_verification_binds_a_fresh_directory_from_the_staging_root(
    monkeypatch, tmp_path
):
    """A root the engine cannot bind must fail verification, not every Ticket."""
    settings = _settings(stage_root=tmp_path / "root")
    seen = {}

    class _Runner(_FakeSyncRunner):
        def __call__(self, argv, *, timeout_seconds=30.0):
            if "--mount" in argv:
                mount = argv[argv.index("--mount") + 1]
                source = mount.split("source=", 1)[1].split(",target=", 1)[0]
                seen["source"] = Path(source)
                seen["existed"] = Path(source).is_dir()
                seen["empty"] = not any(Path(source).iterdir())
                seen["script"] = argv[-1]
            return super().__call__(argv, timeout_seconds=timeout_seconds)

    runner = _Runner(
        [(0, "29.5.2", ""), (0, "sha256:abc", ""), (0, "isolation-ok", ""), (0, "codex-cli 1.0.0", "")]
    )
    _patch_settings(monkeypatch, replace(settings, runner=runner))
    monkeypatch.setattr(runtime, "_rm_container_sync", lambda *_a, **_k: True)

    assert runtime.verify_docker_runtime()[0] is True

    assert seen["existed"] and seen["empty"]
    assert seen["source"].parent == tmp_path / "root"
    assert not seen["source"].exists()  # the probe directory is removed again
    assert "/workspace/.isolation-probe" in seen["script"]
    assert list((tmp_path / "root").iterdir()) == []


def test_verification_fails_when_the_engine_cannot_bind_the_staging_root(
    monkeypatch,
):
    (available, detail), _runner = _verify_with(
        monkeypatch,
        _settings(),
        [
            (0, "29.5.2", ""),
            (0, "sha256:abc", ""),
            (
                125,
                "",
                'invalid mount config for type "bind": bind source path does not exist: /tmp/x',
            ),
        ],
    )

    assert available is False
    assert "container isolation probe failed" in detail
    assert "bind source path does not exist" in detail


def test_verification_refuses_a_group_accessible_staging_root(monkeypatch, tmp_path):
    root = tmp_path / "shared"
    root.mkdir(mode=0o755)
    root.chmod(0o755)

    (available, detail), runner = _verify_with(
        monkeypatch,
        _settings(stage_root=root),
        [(0, "29.5.2", ""), (0, "sha256:abc", "")],
    )

    assert available is False
    assert "group/world accessible" in detail
    assert len(runner.calls) == 2  # no probe container was started


def test_a_verdict_is_cached_per_staging_root(monkeypatch, tmp_path):
    first = _settings(stage_root=tmp_path / "a")
    second = _settings(stage_root=tmp_path / "b")

    assert first.cache_key() != second.cache_key()


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ([(1, "", "Cannot connect to the Docker daemon")], "Docker engine unavailable"),
        ([(0, "29.5.2", ""), (1, "", "No such image")], "is not available"),
        (
            [(0, "29.5.2", ""), (0, "sha256:abc", ""), (125, "", "denied")],
            "isolation probe failed",
        ),
        (
            [(0, "29.5.2", ""), (0, "sha256:abc", ""), (0, "isolation-ok", ""), (127, "", "codex: not found")],
            "worker CLI 'codex' is not available",
        ),
    ],
)
def test_verification_failures_fail_closed(monkeypatch, results, expected):
    (available, detail), _runner = _verify_with(
        monkeypatch, _settings(), results
    )

    assert available is False
    assert expected in detail


def test_verification_is_cached(monkeypatch):
    settings = replace(_settings())
    runner = _FakeSyncRunner(
        [(0, "29.5.2", ""), (0, "sha256:abc", ""), (0, "isolation-ok", ""), (0, "codex-cli 1.0.0", "")]
    )
    _patch_settings(monkeypatch, replace(settings, runner=runner))
    monkeypatch.setattr(runtime, "_rm_container_sync", lambda *_a, **_k: True)

    assert runtime.verify_docker_runtime()[0] is True
    assert runtime.verify_docker_runtime()[0] is True
    assert len(runner.calls) == 4  # not 8: the second call used the cache


def test_verification_never_raises(monkeypatch):
    def _explode():
        raise RuntimeError("unexpected")

    monkeypatch.setattr(runtime.DockerWorkerSettings, "from_env", staticmethod(_explode))

    available, detail = runtime.verify_docker_runtime()

    assert available is False
    assert "runtime configuration failed" in detail


# ---------------------------------------------------------------------------
# Launch lifecycle
# ---------------------------------------------------------------------------


def _patch_launch(monkeypatch, tmp_path, stdout, returncode=0, stderr=""):
    workspace = tmp_path / "stage"
    workspace.mkdir()
    stage = AsyncMock(return_value=workspace)
    monkeypatch.setattr(runtime, "_stage_workspace", stage)
    publish = AsyncMock()
    monkeypatch.setattr(runtime, "_publish_workspace_if_safe", publish)
    monkeypatch.setattr(runtime, "_workspace_metadata", AsyncMock(return_value={}))
    monkeypatch.setattr(runtime, "_rm_container_sync", lambda *_a, **_k: True)
    default_worker_result = runtime.DockerCommandResult(
        argv=("docker",), returncode=returncode, stdout=stdout, stderr=stderr
    )
    command_order = []

    def fake_docker_sync(argv, *, timeout_seconds):
        argv = tuple(argv)
        command_order.append(argv)
        marker = argv.index("/workspace")
        command = argv[marker + 2 :]
        if command[:2] != ("env", "-i"):
            return subprocess.CompletedProcess(argv, 1, b"", b"unexpected docker exec")
        env = {}
        position = 2
        while position < len(command) and "=" in command[position]:
            key, value = command[position].split("=", 1)
            env[key] = value
            position += 1
        git_args = list(command[position:])
        git_args[0] = shutil.which("git")
        mount = runner.create_argv[runner.create_argv.index("--mount") + 1]
        git_workspace = Path(mount.split("source=", 1)[1].split(",target=", 1)[0])
        git_args = [str(workspace) if part == "/workspace" else part for part in git_args]
        git_args = [str(git_workspace) if part == str(workspace) else part for part in git_args]
        completed = subprocess.run(
            git_args,
            cwd=git_workspace,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
        if completed.returncode:
            runner.git_failures.append(
                (tuple(git_args[1:]), completed.stderr.decode("utf-8", "replace"))
            )
        return subprocess.CompletedProcess(
            argv,
            completed.returncode,
            completed.stdout.replace(os.fsencode(git_workspace), b"/workspace"),
            completed.stderr.replace(os.fsencode(git_workspace), b"/workspace"),
        )

    monkeypatch.setattr(runtime, "_run_docker_sync", fake_docker_sync)

    async def fake_docker(argv, *, stdin_text=None, timeout_seconds=60.0):
        argv = tuple(argv)
        if not (len(argv) > 1 and argv[1] == "exec" and "env" in argv):
            command_order.append(argv)
        if len(argv) > 1 and argv[1] == "create":
            runner.create_argv = argv
            return runtime.DockerCommandResult(argv, 0, "container-id\n", "")
        if len(argv) > 1 and argv[1] == "start":
            return runtime.DockerCommandResult(argv, 0, "container-id\n", "")
        if len(argv) > 1 and argv[1] == "exec":
            marker = argv.index("/workspace")
            command = argv[marker + 2 :]
            if command[:1] == ("/bin/sh",):
                return runtime.DockerCommandResult(argv, 0, "", "")
            if command[:2] == ("env", "-i"):
                completed = fake_docker_sync(argv, timeout_seconds=timeout_seconds)
                return runtime.DockerCommandResult(
                    argv,
                    completed.returncode,
                    completed.stdout.decode("utf-8", "replace"),
                    completed.stderr.decode("utf-8", "replace"),
                )
            runner.worker_argv = argv
            runner.worker_stdin = stdin_text
            if runner.worker_exception is not None:
                raise runner.worker_exception
            if runner.worker_callback is not None:
                return await runner.worker_callback(argv, stdin_text=stdin_text)
            return default_worker_result
        return runtime.DockerCommandResult(argv, 0, "", "")

    runner = AsyncMock(side_effect=fake_docker)
    runner.worker_exception = None
    runner.worker_callback = None
    runner.worker_argv = ()
    runner.create_argv = ()
    runner.worker_stdin = None
    runner.git_failures = []
    runner.command_order = command_order
    monkeypatch.setattr(runtime, "_run_command_async", runner)
    cleanup = AsyncMock()
    monkeypatch.setattr(runtime, "_rm_container", cleanup)
    verify = lambda: (True, "verified")  # noqa: E731 - tiny local stub
    monkeypatch.setattr(runtime, "verify_docker_runtime", verify)
    _patch_settings(monkeypatch, _settings(stage_root=tmp_path / "stage-root"))
    return workspace, runner, cleanup, publish, stage


_CODEX_STDOUT = "\n".join(
    [
        '{"type":"thread.started","thread_id":"t-1"}',
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": json.dumps(
                        {
                            "status": "done",
                            "summary": "Implemented the change.",
                            "artifacts": {"commits": [], "files": [], "pr": None},
                            "verification": [],
                            "next": "Send the Ticket to review.",
                            "handbacks": [],
                        }
                    ),
                },
            }
        ),
        '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}',
    ]
)


@pytest.mark.asyncio
async def test_launch_runs_the_container_and_returns_a_parsed_report(
    monkeypatch, tmp_path
):
    workspace, runner, cleanup, publish, stage = _patch_launch(
        monkeypatch, tmp_path, _CODEX_STDOUT
    )
    ticket = {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"}

    report = await runtime.launch(ticket, "do the work")

    argv = runner.worker_argv
    assert argv[0] == "/bin/sh"
    # The real Worker keeps the image's own entrypoint and command behaviour.
    assert "--entrypoint" not in argv
    assert runner.worker_stdin == "do the work"
    assert report["state"] == "ready"
    assert "Implemented the change." in report["summary"]
    cleanup.assert_awaited_once()
    stage.assert_awaited_once()
    publish.assert_awaited_once()
    assert not workspace.exists()  # staging tree removed in finally


@pytest.mark.asyncio
async def test_launch_returns_the_runner_command_shape(monkeypatch, tmp_path):
    _workspace, runner, _cleanup, _publish, _stage = _patch_launch(
        monkeypatch, tmp_path, _CODEX_STDOUT
    )
    await runtime.launch(
        {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"}, "go"
    )
    create_argv = runner.create_argv
    assert create_argv[create_argv.index("--network") + 1] == "none"
    assert "--read-only" in create_argv
    assert "type=bind,source=" + str(_workspace) in " ".join(create_argv)


@pytest.mark.asyncio
async def test_docker_git_check_is_unsupported_and_container_teardown_follows_worker(
    monkeypatch, tmp_path
):
    _workspace, runner, _cleanup, _publish, _stage = _patch_launch(
        monkeypatch, tmp_path, _CODEX_STDOUT
    )

    async def remove(_settings, name):
        runner.command_order.append(("docker", "rm", name))
        return True

    monkeypatch.setattr(runtime, "_rm_container", remove)
    report = await runtime.launch(
        {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "stable-ws"},
        "go",
    )

    worker_index = runner.command_order.index(runner.worker_argv)
    remove_index = next(
        index for index, command in enumerate(runner.command_order) if command[1] == "rm"
    )
    version_probe = next(
        command
        for command in runner.command_order
        if "env" in command and command[-1:] == ("--version",)
    )
    probe_index = runner.command_order.index(version_probe)
    assert probe_index < worker_index < remove_index
    assert report["state"] == "ready"
    assert report["git_check"] == "unsupported_runtime"
    assert "shared Git delivery check" in report["summary"]
    assert runner.create_argv[runner.create_argv.index("--entrypoint") + 1] == "/bin/sh"


def test_docker_git_runner_rejects_filter_equals_without_running_host_git(
    monkeypatch, tmp_path
):
    from src.server.services.autonomy.git_delivery import GitDeliveryError

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    docker_git = runtime._docker_git_runner(_settings(), "container-name", workspace)
    docker_sync = MagicMock()
    monkeypatch.setattr(runtime, "_run_docker_sync", docker_sync)
    env = {
        "PATH": "/bin:/usr/bin",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "filter.bad=name.smudge",
        "GIT_CONFIG_VALUE_0": "",
    }

    with pytest.raises(GitDeliveryError, match="safely"):
        docker_git(
            ("/usr/bin/git", "-C", str(workspace), "status"),
            cwd=str(workspace),
            timeout=2,
            env=env,
        )

    docker_sync.assert_not_called()


def test_docker_git_runner_rejects_paths_outside_the_workspace_mount(
    monkeypatch, tmp_path
):
    from src.server.services.autonomy.git_delivery import GitDeliveryError

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    docker_git = runtime._docker_git_runner(_settings(), "container-name", workspace)
    docker_sync = MagicMock()
    monkeypatch.setattr(runtime, "_run_docker_sync", docker_sync)

    with pytest.raises(GitDeliveryError, match="safely"):
        docker_git(
            ("/usr/bin/git", "-C", str(outside), "status"),
            cwd=str(workspace),
            timeout=2,
            env={"PATH": "/bin:/usr/bin"},
        )

    docker_sync.assert_not_called()


@pytest.mark.asyncio
async def test_launch_timeout_kills_cleans_up_and_raises(monkeypatch, tmp_path):
    _workspace, runner, cleanup, _publish, _stage = _patch_launch(
        monkeypatch, tmp_path, ""
    )
    runner.worker_exception = asyncio.TimeoutError()

    with pytest.raises(runtime.DockerWorkerRuntimeError, match="timed out"):
        await runtime.launch(
            {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"}, "go"
        )

    cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_launch_nonzero_exit_fails_closed_with_sanitized_detail(
    monkeypatch, tmp_path
):
    _workspace, _runner, cleanup, _publish, _stage = _patch_launch(
        monkeypatch, tmp_path, "", returncode=2, stderr="codex failed\nsecond line"
    )

    with pytest.raises(runtime.DockerWorkerRuntimeError, match="exited 2") as raised:
        await runtime.launch(
            {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"}, "go"
        )

    cleanup.assert_awaited_once()
    # The Worker's output is in the one form made for it, one line ...
    assert _ticket_text(raised.value) == (
        cloud_runtime.launch_failure_prefix("docker")
        + "isolated cloud Worker exited 2: codex failed second line"
    )
    # ... and in no other: not the message, nor what is logged of it.
    assert str(raised.value) == "isolated cloud Worker exited 2"
    assert "codex failed" not in repr(raised.value)


@pytest.mark.asyncio
async def test_launch_refuses_an_unverified_runtime_before_staging(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(runtime, "verify_docker_runtime", lambda: (False, "no engine"))
    stage = AsyncMock()
    monkeypatch.setattr(runtime, "_stage_workspace", stage)

    with pytest.raises(runtime.DockerWorkerRuntimeUnavailable, match="no engine"):
        await runtime.launch(
            {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"}, "go"
        )

    stage.assert_not_awaited()


@pytest.mark.asyncio
async def test_launch_refuses_an_empty_prompt(monkeypatch, tmp_path):
    _patch_launch(monkeypatch, tmp_path, _CODEX_STDOUT)

    with pytest.raises(runtime.DockerWorkerRuntimeError, match="prompt is empty"):
        await runtime.launch(
            {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"}, "  "
        )


# ---------------------------------------------------------------------------
# Report grammar and registration
# ---------------------------------------------------------------------------


def test_marker_only_report_waits_for_a_completion_report():
    parsed = runtime.parse_worker_report("done\nAUTONOMY_STATE: ready")

    assert parsed["state"] == "waiting"
    assert parsed["completion"] is None


def test_install_registers_verifier_and_launcher_once(monkeypatch):
    monkeypatch.setattr(cloud_runtime, "ISOLATED_RUNTIME_VERIFIERS", {})
    monkeypatch.setattr(cloud_runtime, "ISOLATED_RUNTIME_LAUNCHERS", {})

    runtime.install()
    runtime.install()

    assert cloud_runtime.ISOLATED_RUNTIME_VERIFIERS["docker"] is runtime.verify_docker_runtime
    assert cloud_runtime.ISOLATED_RUNTIME_LAUNCHERS["docker"] is runtime.launch

@pytest.mark.asyncio
async def test_launch_refuses_a_non_private_staging_root(monkeypatch, tmp_path):
    root = tmp_path / "shared"
    root.mkdir(mode=0o755)
    monkeypatch.setattr(runtime, "verify_docker_runtime", lambda: (True, "verified"))
    monkeypatch.setattr(runtime, "_workspace_metadata", AsyncMock(return_value={}))
    _patch_settings(monkeypatch, _settings(stage_root=root))

    with pytest.raises(runtime.DockerWorkerRuntimeError, match="group/world accessible"):
        await runtime.launch(
            {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"}, "go"
        )


@pytest.mark.asyncio
async def test_launch_redacts_forwarded_values_from_failure_detail(
    monkeypatch, tmp_path
):
    _workspace, _runner, _cleanup, _publish, _stage = _patch_launch(
        monkeypatch,
        tmp_path,
        "",
        returncode=2,
        stderr="auth failed for supersecret-token",
    )
    _patch_settings(
        monkeypatch,
        _settings(
            set_env=(("TOKEN", "supersecret-token"),),
            stage_root=tmp_path / "redact-root",
        ),
    )

    with pytest.raises(runtime.DockerWorkerRuntimeError) as raised:
        await runtime.launch(
            {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"}, "go"
        )

    message = _ticket_text(raised.value)
    assert "supersecret-token" not in message
    assert "***" in message

@pytest.mark.asyncio
async def test_launch_refuses_a_ticket_for_a_different_provider(monkeypatch, tmp_path):
    _patch_launch(monkeypatch, tmp_path, _CODEX_STDOUT)

    with pytest.raises(runtime.DockerWorkerRuntimeError, match="does not match"):
        await runtime.launch(
            {
                "ticket_id": "ticket-1",
                "user_id": "u1",
                "workspace_id": "ws-1",
                "worker_provider": "claude-code",
            },
            "go",
        )

@pytest.mark.asyncio
async def test_launch_forwards_the_ticket_model(monkeypatch, tmp_path):
    _workspace, runner, _cleanup, _publish, _stage = _patch_launch(
        monkeypatch, tmp_path, _CODEX_STDOUT
    )

    await runtime.launch(
        {
            "ticket_id": "ticket-1",
            "user_id": "u1",
            "workspace_id": "ws-1",
            "worker_model": "gpt-5.6-terra",
        },
        "go",
    )

    argv = runner.worker_argv
    assert argv[argv.index("--model") + 1] == "gpt-5.6-terra"


@pytest.mark.asyncio
async def test_launch_refuses_an_unsafe_ticket_model(monkeypatch, tmp_path):
    _patch_launch(monkeypatch, tmp_path, _CODEX_STDOUT)

    with pytest.raises(runtime.DockerWorkerRuntimeUnavailable, match="invalid"):
        await runtime.launch(
            {
                "ticket_id": "ticket-1",
                "user_id": "u1",
                "workspace_id": "ws-1",
                "worker_model": "bad model; rm -rf /",
            },
            "go",
        )


@pytest.mark.asyncio
async def test_read_tail_keeps_the_end_of_a_long_stream():
    reader = asyncio.StreamReader()
    reader.feed_data(b"x" * 200_000 + b"FINAL")
    reader.feed_eof()

    tail = await runtime._read_tail(reader, 64)

    assert tail == b"x" * 59 + b"FINAL"
    assert len(tail) == 64

# ---------------------------------------------------------------------------
# Publication fence and conflict checks
# ---------------------------------------------------------------------------


def _publication_db(
    monkeypatch, *, metadata, upserts=(), removed=(), owner=None
):
    """Fake transaction/connection for the publication path; returns mocks."""
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock

    cursor = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=[])
    cursor.fetchone = AsyncMock(
        return_value=owner
        or {
            "status": "running",
            "worker_thread_id": "thread-1",
            "worker_run_id": "run-1",
        }
    )
    cursor_cm = MagicMock()
    cursor_cm.__aenter__ = AsyncMock(return_value=cursor)
    cursor_cm.__aexit__ = AsyncMock(return_value=False)
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=False)
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cursor_cm)
    conn.transaction = MagicMock(return_value=transaction)

    @asynccontextmanager
    async def _open():
        yield conn

    get_metadata = AsyncMock(return_value=dict(metadata))
    upsert = AsyncMock(return_value=len(upserts))
    delete = AsyncMock()
    collect = MagicMock(return_value=(list(upserts), list(removed)))
    monkeypatch.setattr(
        "src.server.database.pool.get_db_connection", _open
    )
    monkeypatch.setattr(
        "src.server.database.workspace_file.get_file_metadata_for_sync",
        get_metadata,
    )
    monkeypatch.setattr(
        "src.server.database.workspace_file.bulk_upsert_files", upsert
    )
    monkeypatch.setattr(
        "src.server.database.workspace_file.delete_file_rows", delete
    )
    monkeypatch.setattr(
        "src.server.services.native_runtime.workspace.collect_changed_files",
        collect,
    )
    return {"conn": conn, "cursor": cursor, "upsert": upsert, "delete": delete}


_ACTIVE = {
    "status": "running",
    "worker_thread_id": "thread-1",
    "worker_run_id": "run-1",
}


def _publication_ticket():
    return {
        "ticket_id": "ticket-1",
        "workspace_id": "ws-1",
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-1",
    }


@pytest.mark.asyncio
async def test_publication_is_fenced_to_the_active_run(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.server.database.autonomy.tickets.get_ticket",
        AsyncMock(return_value={"status": "cancelled", "worker_thread_id": None}),
    )
    # The fence runs before any connection is opened.
    monkeypatch.setattr(
        "src.server.database.pool.get_db_connection",
        lambda: (_ for _ in ()).throw(AssertionError("must not open a connection")),
    )

    with pytest.raises(runtime.DockerWorkerRuntimeError, match="left this isolated"):
        await runtime._publish_workspace_if_safe(
            ticket=_publication_ticket(), workspace=tmp_path, baseline={}
        )


@pytest.mark.asyncio
async def test_publication_refuses_when_the_manifest_changed(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.server.database.autonomy.tickets.get_ticket",
        AsyncMock(return_value=dict(_ACTIVE)),
    )
    mocks = _publication_db(
        monkeypatch, metadata={"b.txt": {"content_hash": "new"}}
    )

    with pytest.raises(runtime.DockerWorkerRuntimeError, match="workspace changed"):
        await runtime._publish_workspace_if_safe(
            ticket=_publication_ticket(),
            workspace=tmp_path,
            baseline={"a.txt": {"content_hash": "old"}},
        )

    mocks["upsert"].assert_not_awaited()
    mocks["delete"].assert_not_awaited()


@pytest.mark.asyncio
async def test_publication_rechecks_ticket_ownership_inside_the_transaction(
    monkeypatch, tmp_path
):
    # The pre-transaction read sees the active line; a stop wins the ticket
    # row lock first, so the in-transaction owner read must abort the write.
    monkeypatch.setattr(
        "src.server.database.autonomy.tickets.get_ticket",
        AsyncMock(return_value=dict(_ACTIVE)),
    )
    mocks = _publication_db(
        monkeypatch,
        metadata={"a.txt": {"content_hash": "same"}},
        upserts=[{"file_path": "a.txt"}],
        owner={"status": "failed", "worker_thread_id": "thread-1", "worker_run_id": "run-1"},
    )

    with pytest.raises(runtime.DockerWorkerRuntimeError, match="left this isolated"):
        await runtime._publish_workspace_if_safe(
            ticket=_publication_ticket(),
            workspace=tmp_path,
            baseline={"a.txt": {"content_hash": "same"}},
        )

    mocks["upsert"].assert_not_awaited()


@pytest.mark.asyncio
async def test_publication_commits_upserts_only_and_never_deletes(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "src.server.database.autonomy.tickets.get_ticket",
        AsyncMock(return_value=dict(_ACTIVE)),
    )
    metadata = {
        "a.txt": {"content_hash": "same"},
        "legacy-link": {"kind": "symlink"},
        "cold.bin": {"blob_sha256": "abc"},
    }
    mocks = _publication_db(
        monkeypatch,
        metadata=metadata,
        upserts=[{"file_path": "a.txt"}],
        removed=["legacy-link", "cold.bin"],
    )

    written = await runtime._publish_workspace_if_safe(
        ticket=_publication_ticket(),
        workspace=tmp_path,
        baseline=dict(metadata),
    )

    assert written == 1
    statements = [call.args[0] for call in mocks["cursor"].execute.await_args_list]
    assert any("LOCK TABLE workspace_files" in str(sql) for sql in statements)
    mocks["upsert"].assert_awaited_once()
    assert mocks["upsert"].await_args.args[0] == "ws-1"
    assert mocks["upsert"].await_args.kwargs["conn"] is mocks["conn"]
    # The staging copy is lossy; a missing path must never delete a manifest row.
    mocks["delete"].assert_not_awaited()


# ---------------------------------------------------------------------------
# Mount canonicalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    ["//workspace", "//etc/x", "//var/run/x", "/tmp/../workspace", "/workspace/sub"],
)
def test_mount_targets_are_canonicalized_before_the_reserved_check(target):
    with pytest.raises(runtime.DockerWorkerRuntimeUnavailable, match="reserved"):
        runtime.DockerWorkerSettings.from_env(
            {"AUTONOMY_CLOUD_WORKER_MOUNTS": f"/host:{target}:ro"}
        )


def test_a_double_slash_non_reserved_target_is_emitted_canonically():
    settings = runtime.DockerWorkerSettings.from_env(
        {"AUTONOMY_CLOUD_WORKER_MOUNTS": "/Users/you/.codex://codex-home:ro"}
    )
    argv = _argv(settings)
    mounts = [argv[index + 1] for index, item in enumerate(argv) if item == "-v"]

    assert mounts == ["/Users/you/.codex:/codex-home:ro"]

@pytest.mark.asyncio
async def test_launch_refuses_when_the_manifest_moves_during_materialization(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "stage"
    workspace.mkdir()
    monkeypatch.setattr(runtime, "_stage_workspace", AsyncMock(return_value=workspace))
    monkeypatch.setattr(
        runtime,
        "_workspace_metadata",
        AsyncMock(side_effect=[{"a.txt": {"content_hash": "v1"}}, {"a.txt": {"content_hash": "v2"}}]),
    )
    runner = AsyncMock()
    monkeypatch.setattr(runtime, "_run_command_async", runner)
    cleanup = AsyncMock()
    monkeypatch.setattr(runtime, "_rm_container", cleanup)
    monkeypatch.setattr(runtime, "_publish_workspace_if_safe", AsyncMock())
    _patch_settings(monkeypatch, _settings(stage_root=tmp_path / "root"))
    monkeypatch.setattr(runtime, "verify_docker_runtime", lambda: (True, "verified"))

    with pytest.raises(runtime.DockerWorkerRuntimeError, match="materialized"):
        await runtime.launch(
            {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"},
            "go",
        )

    runner.assert_not_awaited()
    cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_launch_redacts_short_forwarded_values(monkeypatch, tmp_path):
    _workspace, _runner, _cleanup, _publish, _stage = _patch_launch(
        monkeypatch, tmp_path, "", returncode=2, stderr="bad token q7"
    )
    _patch_settings(
        monkeypatch,
        _settings(
            # Two characters: the previous implementation only redacted >= 4.
            set_env=(("TOKEN", "q7"),),
            stage_root=tmp_path / "redact-short-root",
        ),
    )

    with pytest.raises(runtime.DockerWorkerRuntimeError) as raised:
        await runtime.launch(
            {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"},
            "go",
        )

    assert " q7" not in _ticket_text(raised.value)
    assert "***" in _ticket_text(raised.value)

# -- the failure detail never carries a forwarded value, however it was split --
#
# Redaction used to run on the raw output and ``_sanitize`` then turned tabs,
# newlines and runs of spaces into single spaces, which could put back a value
# the Worker had split ("alpha<TAB>beta" for "alpha beta"). The rule now: the
# final, normalized text is what is redacted, before it is cut to length.


def _ticket_text(error) -> str:
    """The one form of a failed launch that carries the Worker's output.

    For the runtime's exception that is the output after the runtime's own
    words; for ``CloudWorkerLaunchError`` it is what the Ticket gets.
    """
    if isinstance(error, cloud_runtime.CloudWorkerLaunchError):
        return cloud_runtime.ticket_error(error)
    return error.operator_message


async def _failed_launch_detail(
    monkeypatch, tmp_path, value, *, stdout="", stderr="", tag="r"
):
    _patch_launch(monkeypatch, tmp_path, stdout, returncode=7, stderr=stderr)
    _patch_settings(
        monkeypatch,
        _settings(
            set_env=(("TOKEN", value),), stage_root=tmp_path / f"redact-{tag}-root"
        ),
    )
    with pytest.raises(runtime.DockerWorkerRuntimeError) as raised:
        await runtime.launch(
            {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"}, "go"
        )
    return _ticket_text(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", ["stderr", "stdout"])
@pytest.mark.parametrize(
    ("value", "emitted"),
    [
        ("alpha beta", "auth failed: alpha\tbeta"),
        ("alpha beta", "auth failed: alpha\nbeta"),
        ("alpha beta", "auth failed: alpha\rbeta"),
        ("alpha beta", "auth failed: alpha\r\nbeta"),
        ("alpha beta", "auth failed: alpha      beta"),
        ("alpha beta", "auth failed: alpha \t\r\n beta"),
        ("alpha beta", "auth failed: alpha\x07beta"),
        ("alpha beta", "auth failed: alpha\x1bbeta"),
        # a value that itself holds a tab or a doubled space
        ("alpha\tbeta", "auth failed: alpha beta"),
        ("alpha\tbeta", "auth failed: alpha\n\nbeta"),
        ("alpha  beta", "auth failed: alpha\tbeta"),
        # the unsplit forms still go
        ("alpha beta", "auth failed: alpha beta"),
        ("alpha\tbeta", "auth failed: alpha\tbeta"),
    ],
)
async def test_a_value_the_worker_split_is_not_put_back_together_in_the_detail(
    monkeypatch, tmp_path, stream, value, emitted
):
    message = await _failed_launch_detail(
        monkeypatch, tmp_path, value, **{stream: emitted}
    )

    assert "alpha" not in message
    assert "beta" not in message
    assert "***" in message


@pytest.mark.asyncio
async def test_a_value_cut_in_half_by_the_length_limit_leaves_no_prefix(
    monkeypatch, tmp_path
):
    """The limit is applied after redaction, so it cannot leave half a value."""
    # 'alpha' starts four characters before the limit: the cut would keep 'alph'
    emitted = (
        "x" * (runtime._FAILURE_DETAIL_LIMIT - 5)
        + " "
        + "alpha\tbeta"
        + " and the rest of the output"
    )

    message = await _failed_launch_detail(
        monkeypatch, tmp_path, "alpha beta", stderr=emitted
    )

    assert "alph" not in message
    assert "***" in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "emitted",
    [
        "ecret-token: auth failed",  # a tail capture that began inside the value
        "\ufffdcret-token: auth failed",  # ... inside a multi-byte character
        "auth failed for supersecr",  # a capture that ended inside it
    ],
)
async def test_a_fragment_the_capture_cut_at_either_end_of_the_output_is_redacted(
    monkeypatch, tmp_path, emitted
):
    message = await _failed_launch_detail(
        monkeypatch, tmp_path, "supersecret-token", stderr=emitted
    )

    for fragment in ("cret-token", "ecret-token", "supersecr", "secret"):
        assert fragment not in message
    assert "***" in message


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # the rule: at the very start or end of the text, a piece of a value of
        # at least runtime._MIN_FRAGMENT characters goes; shorter ones do not
        ("oken rest", "*** rest"),
        ("ken rest", "ken rest"),
        ("rest secr", "rest ***"),
        ("rest sec", "rest sec"),
        # in the middle of the text only a whole value goes
        ("a oken b secr c", "a oken b secr c"),
        ("a secret-token b", "a *** b"),
    ],
)
def test_the_fragment_rule_is_exactly_the_documented_one(text, expected):
    assert runtime._MIN_FRAGMENT == 4
    assert runtime._sanitize(text, redact={"T": "secret-token"}) == expected


def test_overlapping_values_are_redacted_longest_first():
    redacted = runtime._sanitize("x abcdef y", redact={"A": "abc", "B": "abcdef"})

    assert redacted == "x *** y"


# -- the final guard: whatever the passes did, no value is in the final string --
#
# Redaction, normalization, the cut, the ellipsis, the replacement marker and
# the later truncation in cloud_runtime can each combine with their neighbours.
# So the guarantee does not rest on their order: the string that is finally
# raised is checked against every forwarded value, and withheld if one is in it.


async def _failed_final(
    monkeypatch,
    tmp_path,
    values,
    *,
    stdout="",
    stderr="",
    via="runtime",
    caplog=None,
):
    """The final error message of a Worker that exited 7, as the caller sees it.

    ``via="cloud"`` goes through cloud_runtime.launch_cloud_worker, the real
    ``_shown`` truncation and the logging of the failure included.
    """
    _patch_launch(monkeypatch, tmp_path, stdout, returncode=7, stderr=stderr)
    _patch_settings(
        monkeypatch,
        _settings(
            set_env=tuple((f"V{i}", value) for i, value in enumerate(values)),
            stage_root=tmp_path / "final-root",
        ),
    )
    ticket = {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"}
    if via == "runtime":
        with pytest.raises(runtime.DockerWorkerRuntimeError) as raised:
            await runtime.launch(ticket, "go")
        message = _ticket_text(raised.value)
    else:
        monkeypatch.setattr(
            cloud_runtime, "get_autonomy_cloud_worker_runtime", lambda: "docker"
        )
        monkeypatch.setattr(
            cloud_runtime, "ISOLATED_RUNTIME_VERIFIERS", {"docker": lambda: (True, "ok")}
        )
        monkeypatch.setattr(
            cloud_runtime, "ISOLATED_RUNTIME_LAUNCHERS", {"docker": runtime.launch}
        )
        with pytest.raises(cloud_runtime.CloudWorkerLaunchError) as raised:
            await cloud_runtime.launch_cloud_worker(ticket, "go")
        message = _ticket_text(raised.value)
        if caplog is not None:
            # logger.exception wrote the failure, its chained cause included
            for value in values:
                assert value not in caplog.text
    _failed_final.error = raised.value
    return message


def _no_value_or_piece_of_it(message, value, *, window=4):
    assert value not in message
    pieces = {value[i : i + window] for i in range(len(value) - window + 1)}
    assert not {piece for piece in pieces if piece in message}


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["runtime", "cloud"])
async def test_a_replacement_that_recreates_another_value_is_withheld(
    monkeypatch, tmp_path, caplog, via
):
    """B's normalized form is replaced by '***', which turns the text into A."""
    a, b = "prefix***suffix", "x  y"
    with caplog.at_level("DEBUG"):
        message = await _failed_final(
            monkeypatch,
            tmp_path,
            [a, b],
            stderr="prefixx\tysuffix",
            via=via,
            caplog=caplog,
        )

    assert a not in message and b not in message
    assert "exited 7" in message
    assert runtime._WITHHELD in message
    assert "prefix" not in message.split("exited 7:", 1)[1].replace(runtime._WITHHELD, "")


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["runtime", "cloud"])
@pytest.mark.parametrize("stream", ["stderr", "stdout"])
async def test_a_cut_that_appends_the_ellipsis_cannot_complete_a_value(
    monkeypatch, tmp_path, caplog, via, stream
):
    """TOKEN ends with the ellipsis the cut appends: 'TAIL…' appears only because of it."""
    token = "TAIL" + runtime._ELLIPSIS
    emitted = "x" * (runtime._FAILURE_DETAIL_LIMIT - 4) + "TAIL extra"
    with caplog.at_level("DEBUG"):
        message = await _failed_final(
            monkeypatch,
            tmp_path,
            [token],
            via=via,
            caplog=caplog,
            **{stream: emitted},
        )

    assert token not in message
    assert runtime._WITHHELD in message


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["runtime", "cloud"])
async def test_the_later_truncation_in_cloud_runtime_cannot_complete_a_value(
    monkeypatch, tmp_path, caplog, via
):
    """A value of 'TAIL...' with TAIL ending where cloud_runtime used to cut at 300."""
    prefix = "isolated cloud Worker exited 7: "
    emitted = "x" * (300 - len(prefix) - 4) + "TAIL" + " and the rest of the output" * 5
    with caplog.at_level("DEBUG"):
        message = await _failed_final(
            monkeypatch, tmp_path, ["TAIL..."], stderr=emitted, via=via, caplog=caplog
        )

    assert "TAIL..." not in message


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["runtime", "cloud"])
@pytest.mark.parametrize("marker", [runtime._ELLIPSIS, "...", "***"])
async def test_a_value_equal_to_a_marker_the_runtime_writes_is_withheld(
    monkeypatch, tmp_path, via, marker
):
    """The redaction marker and the ellipsis are text the runtime itself adds."""
    emitted = "a " + marker + " b " + "y" * 600

    message = await _failed_final(
        monkeypatch, tmp_path, [marker], stderr=emitted, via=via
    )

    assert marker not in message
    assert "exited 7" in message


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["runtime", "cloud"])
async def test_values_that_are_prefixes_of_each_other_leave_no_remainder(
    monkeypatch, tmp_path, via
):
    message = await _failed_final(
        monkeypatch,
        tmp_path,
        ["abc", "abcdef"],
        stderr="the token was abcdef today",
        via=via,
    )

    assert "def" not in message
    assert "***" in message


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["runtime", "cloud"])
@pytest.mark.parametrize("stream", ["stderr", "stdout"])
@pytest.mark.parametrize("value", ["ZQXJ-secret-42", "ZQXJ secret 42"])
async def test_a_cut_at_any_position_around_a_value_leaves_no_piece_of_it(
    monkeypatch, tmp_path, via, stream, value
):
    """Sweep the length cut across the value's neighbourhood, on both sides of it."""
    limit = runtime._FAILURE_DETAIL_LIMIT
    for offset in range(-len(value) - 3, 4):
        emitted = "x" * (limit + offset) + " " + value.replace(" ", "\t") + " tail"
        message = await _failed_final(
            monkeypatch, tmp_path, [value], via=via, **{stream: emitted}
        )

        _no_value_or_piece_of_it(message, value)
        assert "exited 7" in message


_MARK = "WORKER-OUTPUT-7Q"
_RUNTIME_WORDS = "isolated cloud Worker exited 7"
_CLOUD_WORDS = "OS-isolated cloud Worker runtime 'docker' failed: "
_LIMIT = runtime._FAILURE_DETAIL_LIMIT


def _emitted_forms(error, caplog):
    """Every form in which a failed launch is emitted, as a caller can build it."""
    forms = {}
    chain = [("", error)]
    if error.__cause__ is not None:
        chain.append(("cause ", error.__cause__))
    for label, exc in chain:
        qualified = f"{type(exc).__module__}.{type(exc).__qualname__}"
        forms[f"{label}str"] = str(exc)
        forms[f"{label}repr"] = repr(exc)
        forms[f"{label}name: str"] = f"{type(exc).__name__}: {exc}"
        forms[f"{label}qualified name: str"] = f"{qualified}: {exc}"
        forms[f"{label}args"] = " ".join(map(str, exc.args))
    forms["log at ERROR with exc_info"] = caplog.text
    ticket = cloud_runtime.ticket_error(error)
    forms["ticket error"] = ticket
    forms["ticket error[:1000]"] = ticket[:1000]
    forms["ticket summary[:2000]"] = ticket[:2000]
    return forms


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("token", "output", "withheld"),
    [
        # round 5: the cloud prefix completes the value
        ("failed: " + _RUNTIME_WORDS + ": ok", "ok", True),
        # round 5: the class name the traceback adds completes it
        ("DockerWorkerRuntimeError: " + _RUNTIME_WORDS + ": ok", "ok", False),
        (
            "src.server.services.autonomy.docker_worker_runtime."
            "DockerWorkerRuntimeError: " + _RUNTIME_WORDS + ": ok",
            "ok",
            False,
        ),
        (
            "CloudWorkerLaunchError: " + _CLOUD_WORDS + _RUNTIME_WORDS + ": ok",
            "ok",
            False,
        ),
        # a value spanning the fixed words and the Worker's text ("ok" is too short
        # for the end-fragment rule, so only the final check can catch it)
        ("exited 7: ok", "ok", True),
        ("'docker' failed: " + _RUNTIME_WORDS + ": ok", "ok", True),
        # the same, with Worker text long enough for the end-fragment rule: the
        # detail is redacted to '***' there, so nothing needs withholding
        ("exited 7: " + _MARK, _MARK, False),
        # a value spanning the Worker's text and what a cut appends
        (
            _MARK + runtime._ELLIPSIS,
            "x" * (_LIMIT - len(_MARK)) + _MARK + " more",
            True,
        ),
        # a value that is the withheld sentence itself, or only Worker text
        (runtime._WITHHELD, _MARK, False),
        (runtime._WITHHELD, runtime._WITHHELD, False),
        (_MARK, "see " + _MARK + " here", False),
        # two values that assemble a third, and one that is the redaction marker
        ("x  y", "prefixx\tysuffix", True),
        ("***", "a *** b", True),
    ],
)
async def test_no_emitted_form_of_a_failed_launch_holds_a_forwarded_value(
    monkeypatch, tmp_path, caplog, token, output, withheld
):
    """Whatever a caller adds around the exception, the value is not in any form.

    Checked on each final form: str, repr, the class-name-prefixed and
    module-qualified forms, the args, the chained cause, what the logger wrote
    with its traceback, and the Ticket error with its slices. The value is
    forwarded alongside a second one that the first output would assemble.
    """
    with caplog.at_level("DEBUG"):
        await _failed_final(
            monkeypatch,
            tmp_path,
            [token, "prefix***suffix"],
            stderr=output,
            via="cloud",
        )
    forms = _emitted_forms(_failed_final.error, caplog)

    for name, form in forms.items():
        assert token not in form, name
        assert "prefix***suffix" not in form, name
    ticket = forms["ticket error"]
    assert ticket.startswith(_CLOUD_WORDS + _RUNTIME_WORDS + ": ")
    assert (runtime._WITHHELD in ticket) is withheld


@pytest.mark.asyncio
async def test_the_worker_output_is_in_the_ticket_error_and_in_no_other_form(
    monkeypatch, tmp_path, caplog
):
    """Nothing but the Ticket error carries the detail, so nothing can recombine it."""
    with caplog.at_level("DEBUG"):
        await _failed_final(
            monkeypatch, tmp_path, ["unrelated-value"], stderr=_MARK, via="cloud"
        )
    forms = _emitted_forms(_failed_final.error, caplog)

    assert forms["ticket error"] == _CLOUD_WORDS + _RUNTIME_WORDS + ": " + _MARK
    assert forms["ticket error[:1000]"] == forms["ticket error"]
    for name, form in forms.items():
        if not name.startswith("ticket"):
            assert _MARK not in form, name
    assert str(_failed_final.error) == _CLOUD_WORDS + _RUNTIME_WORDS
    assert _RUNTIME_WORDS in caplog.text  # the failure is still logged, by its words


@pytest.mark.asyncio
async def test_the_runtime_exception_keeps_the_detail_out_of_every_form_but_one(
    monkeypatch, tmp_path
):
    await _failed_final(monkeypatch, tmp_path, ["unrelated-value"], stderr=_MARK)
    error = _failed_final.error

    assert str(error) == _RUNTIME_WORDS
    assert _MARK not in repr(error) and _MARK not in f"{type(error).__name__}: {error}"
    assert error.operator_message == _CLOUD_WORDS + f"{_RUNTIME_WORDS}: {_MARK}"


@pytest.mark.parametrize(
    "value",
    [
        "exited 7: ok",  # spans the fixed words and the Worker's text
        "Worker exited 7: ok",
        "7: ok",
    ],
)
def test_the_exemption_covers_only_occurrences_inside_the_fixed_words(value):
    values = {"V": value}
    fixed = _RUNTIME_WORDS + ": "

    # evaluated on the string that is finally emitted, with its real prefix
    assert runtime._leaks(fixed + "ok", values, safe_prefix=len(fixed))
    # the same words wholly inside the fixed part are not a leak, however long
    # the fixed part in front of the Worker's text is
    longer = "OS-isolated cloud Worker runtime 'docker' failed: " + fixed
    assert not runtime._leaks(fixed + "fine", {"V": "exited 7"}, safe_prefix=len(fixed))
    assert not runtime._leaks(
        longer + "fine", {"V": "exited 7"}, safe_prefix=len(longer)
    )
    assert runtime._leaks(longer + "ok", values, safe_prefix=len(longer))


# -- nothing the Worker wrote leaves the runtime in an exception either ---------
#
# The failed-exit path screens the output it quotes. Everything else the
# runtime does with what a Worker produced (parsing its output and usage,
# reading its files, publishing them) can raise an exception that quotes it:
# int('<value>') in a usage field, a UnicodeEncodeError that holds a file name,
# a database error that names a path. Any exception from those stages becomes a
# fixed-message error, raised outside the handler so it chains nothing, and
# logged by stage and exception type name only.

_SENTINEL = "SYNTHETIC-FORWARDED-VALUE-7Q"
_PARSE_FAILED = (
    "isolated cloud Worker result could not be read: parsing the Worker's output"
)
_REPORT_FAILED = (
    "isolated cloud Worker result could not be read: parsing the Worker's report"
)
_PUBLISH_FAILED = (
    "isolated cloud Worker result could not be published: "
    "reading and storing the Worker's file changes"
)
_MANIFEST_FAILED = (
    "isolated cloud Worker workspace could not be prepared: "
    "reading the workspace manifest"
)
_MATERIALIZE_FAILED = (
    "isolated cloud Worker workspace could not be prepared: "
    "materializing the workspace"
)
_UNEXPECTED = "isolated cloud Worker runtime failed unexpectedly"
_JSONL_BAD_USAGE = "\n".join(
    [
        '{"type":"thread.started","thread_id":"t-1"}',
        '{"type":"item.completed","item":{"type":"agent_message",'
        '"text":"done\\nAUTONOMY_STATE: ready"}}',
        '{"type":"turn.completed","usage":{"input_tokens":"%s"}}' % _SENTINEL,
    ]
)


def _chain(error):
    """The exception and everything it chains to, by cause and by context."""
    seen, queue = [], [error]
    while queue:
        exc = queue.pop()
        if exc is None or any(exc is other for other in seen):
            continue
        seen.append(exc)
        queue += [exc.__cause__, exc.__context__]
    return seen


def _assert_no_value_anywhere(error, caplog, value):
    for exc in _chain(error):
        for form in (str(exc), repr(exc), " ".join(map(str, exc.args))):
            assert value not in form, type(exc).__name__
    assert value not in caplog.text
    assert value not in cloud_runtime.ticket_error(error)
    # what is chained is the runtime's own fixed error, itself chained to nothing
    inner = error.__cause__
    assert isinstance(inner, runtime.DockerWorkerRuntimeError)
    assert inner.__cause__ is None
    assert inner.__context__ is None


async def _post_run_failure(
    monkeypatch,
    tmp_path,
    caplog,
    *,
    value=_SENTINEL,
    stdout=_CODEX_STDOUT,
    returncode=0,
    stderr="",
    arrange=None,
):
    """A launch through launch_cloud_worker, with ``value`` forwarded to the Worker."""
    _patch_launch(monkeypatch, tmp_path, stdout, returncode=returncode, stderr=stderr)
    _patch_settings(
        monkeypatch,
        _settings(set_env=(("TOKEN", value),), stage_root=tmp_path / "post-run-root"),
    )
    if arrange is not None:
        arrange()
    monkeypatch.setattr(
        cloud_runtime, "get_autonomy_cloud_worker_runtime", lambda: "docker"
    )
    monkeypatch.setattr(
        cloud_runtime, "ISOLATED_RUNTIME_VERIFIERS", {"docker": lambda: (True, "ok")}
    )
    monkeypatch.setattr(
        cloud_runtime, "ISOLATED_RUNTIME_LAUNCHERS", {"docker": runtime.launch}
    )
    ticket = {
        "ticket_id": "ticket-1",
        "user_id": "u1",
        "workspace_id": "ws-1",
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-1",
    }
    with caplog.at_level("DEBUG"):
        with pytest.raises(cloud_runtime.CloudWorkerLaunchError) as raised:
            await cloud_runtime.launch_cloud_worker(ticket, "go")
    return raised.value


@pytest.mark.asyncio
async def test_a_usage_field_that_quotes_a_value_does_not_carry_it_out(
    monkeypatch, tmp_path, caplog
):
    """int('<value>') is a ValueError that holds the value; the Worker exited 0."""
    error = await _post_run_failure(
        monkeypatch, tmp_path, caplog, stdout=_JSONL_BAD_USAGE
    )

    _assert_no_value_anywhere(error, caplog, _SENTINEL)
    assert str(error).endswith(_PARSE_FAILED)
    assert cloud_runtime.ticket_error(error).endswith(_PARSE_FAILED)
    # logged by stage and exception type name, not by the exception
    record = next(
        r for r in caplog.records if "parsing the Worker's output" in r.getMessage()
    )
    assert record.getMessage().endswith("(ValueError)")
    assert record.exc_info is None


@pytest.mark.asyncio
async def test_a_file_name_the_database_cannot_encode_does_not_carry_the_value_out(
    monkeypatch, tmp_path, caplog
):
    """A Linux Worker's b'FORWARDED-123\\xff' file: the manifest takes the
    surrogate-escaped name and psycopg's UnicodeEncodeError keeps it in repr and
    args. Run through the real publication code with a failing upsert."""
    real_publish = runtime._publish_workspace_if_safe
    try:
        "FORWARDED-123\udcff".encode("utf-8")
    except UnicodeEncodeError as encoding_error:
        db_error = encoding_error
    assert "FORWARDED-123" in repr(db_error) and "FORWARDED-123" in str(db_error.args)

    def arrange():
        monkeypatch.setattr(runtime, "_publish_workspace_if_safe", real_publish)
        monkeypatch.setattr(
            "src.server.database.autonomy.tickets.get_ticket",
            AsyncMock(return_value=dict(_ACTIVE)),
        )
        mocks = _publication_db(
            monkeypatch, metadata={}, upserts=[{"file_path": "FORWARDED-123\udcff"}]
        )
        mocks["upsert"].side_effect = db_error

    error = await _post_run_failure(
        monkeypatch, tmp_path, caplog, value="FORWARDED-123", arrange=arrange
    )

    _assert_no_value_anywhere(error, caplog, "FORWARDED-123")
    assert str(error).endswith(_PUBLISH_FAILED)
    record = next(
        r for r in caplog.records if "file changes" in r.getMessage()
    )
    assert record.getMessage().endswith("(UnicodeEncodeError)")
    assert record.exc_info is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "kind", "expected"),
    [
        ("_workspace_metadata", "async", _MANIFEST_FAILED),
        ("_stage_workspace", "async", _MATERIALIZE_FAILED),
        ("_parse_provider_stdout", "sync", _PARSE_FAILED),
        ("parse_worker_report", "sync", _REPORT_FAILED),
        ("_publish_workspace_if_safe", "async", _PUBLISH_FAILED),
        # cleanup in the launch's own finally, outside every stage
        ("_remove_staging_tree", "sync", _UNEXPECTED),
    ],
)
@pytest.mark.parametrize("exception", [Exception, ValueError, OSError, UnicodeError])
async def test_every_stage_that_touches_what_a_worker_produced_raises_a_fixed_error(
    monkeypatch, tmp_path, caplog, target, kind, expected, exception
):
    def arrange():
        failing = (
            AsyncMock(side_effect=exception(_SENTINEL))
            if kind == "async"
            else MagicMock(side_effect=exception(_SENTINEL))
        )
        monkeypatch.setattr(runtime, target, failing)

    error = await _post_run_failure(monkeypatch, tmp_path, caplog, arrange=arrange)

    _assert_no_value_anywhere(error, caplog, _SENTINEL)
    assert str(error).endswith(expected)
    assert cloud_runtime.ticket_error(error).endswith(expected)
    assert all(
        r.exc_info is None for r in caplog.records if "[cloud-worker]" in r.getMessage()
    )


@pytest.mark.asyncio
async def test_the_runtimes_own_fixed_errors_pass_through_unchanged_and_unchained(
    monkeypatch, tmp_path, caplog
):
    own = runtime.DockerWorkerRuntimeError(
        "the Ticket left this isolated Worker run; workspace changes were not published"
    )
    own.__context__ = ValueError(_SENTINEL)  # even a stray context is dropped

    def arrange():
        monkeypatch.setattr(
            runtime, "_publish_workspace_if_safe", AsyncMock(side_effect=own)
        )

    error = await _post_run_failure(monkeypatch, tmp_path, caplog, arrange=arrange)

    assert str(error).endswith(str(own))
    _assert_no_value_anywhere(error, caplog, _SENTINEL)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "kind", "exception"),
    [
        ("_publish_workspace_if_safe", "async", asyncio.CancelledError),
        ("_parse_provider_stdout", "sync", KeyboardInterrupt),
        ("_stage_workspace", "async", SystemExit),
        ("_workspace_metadata", "async", asyncio.CancelledError),
    ],
)
async def test_cancellation_and_interpreter_exits_still_propagate_untouched(
    monkeypatch, tmp_path, target, kind, exception
):
    _patch_launch(monkeypatch, tmp_path, _CODEX_STDOUT)
    failing = (
        AsyncMock(side_effect=exception())
        if kind == "async"
        else MagicMock(side_effect=exception())
    )
    monkeypatch.setattr(runtime, target, failing)

    with pytest.raises(exception):
        await runtime.launch(
            {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"}, "go"
        )


# -- a cancelled launch still tears its container down -------------------------
#
# The removal retries with a backoff between attempts. A second cancellation
# during that wait used to interrupt it: one attempt, the container left, and
# the staging tree deleted or kept on a wrong belief about it.

_REAL_RM_CONTAINER = runtime._rm_container
_REAL_SLEEP = asyncio.sleep


class _TeardownBench:
    """The Worker run never ends; ``docker rm`` answers from a script; the backoff
    between attempts waits for the test."""

    def __init__(self, monkeypatch, tmp_path, *, rm_results, run="hang"):
        self.rm_calls = []
        self.run_started = asyncio.Event()
        self.backoff = asyncio.Event()
        self.release = asyncio.Event()
        results = list(rm_results)
        self.workspace, _runner, _cleanup, _publish, _stage = _patch_launch(
            monkeypatch, tmp_path, _CODEX_STDOUT
        )
        monkeypatch.setattr(runtime, "_rm_container", _REAL_RM_CONTAINER)

        async def command(argv, *, stdin_text=None, timeout_seconds=60.0):
            if argv[1] == "create":
                return _result(stdout="container-id\n")
            if argv[1] == "start":
                return _result(stdout="container-id\n")
            if argv[1] == "exec" and argv[-3:] == ("/bin/sh", "-c", "true"):
                return _result()
            if argv[1] == "exec":
                self.run_started.set()
                if run == "timeout":
                    raise asyncio.TimeoutError()
                await asyncio.Event().wait()
            self.rm_calls.append(argv[-1])
            returncode, stderr = results.pop(0) if results else (1, "refused")
            return runtime.DockerCommandResult(
                argv=tuple(argv), returncode=returncode, stderr=stderr
            )

        async def sleep(_seconds):
            self.backoff.set()
            await self.release.wait()

        monkeypatch.setattr(runtime, "_run_command_async", command)
        monkeypatch.setattr(runtime.asyncio, "sleep", sleep)

    async def started(self):
        await asyncio.wait_for(self.run_started.wait(), 5)

    async def in_backoff(self):
        await asyncio.wait_for(self.backoff.wait(), 5)

    @staticmethod
    async def let_cancellation_arrive():
        for _ in range(5):
            await _REAL_SLEEP(0)


_TEARDOWN_TICKET = {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"}
_OK = (0, "")
_REFUSED = (1, "Error response from daemon: cannot remove")


@pytest.mark.asyncio
async def test_a_second_cancellation_during_the_backoff_does_not_stop_the_retry(
    monkeypatch, tmp_path
):
    bench = _TeardownBench(monkeypatch, tmp_path, rm_results=[_REFUSED, _OK])
    task = asyncio.create_task(runtime.launch(_TEARDOWN_TICKET, "go"))
    await bench.started()
    task.cancel()
    await bench.in_backoff()  # the first removal failed; the retry is waiting
    task.cancel()
    await bench.let_cancellation_arrive()
    assert not task.done(), "teardown was interrupted by the second cancellation"
    bench.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(bench.rm_calls) == 2  # retried, and to success
    assert not bench.workspace.exists()  # confirmed gone, so the tree is removed


@pytest.mark.asyncio
async def test_a_container_that_survives_the_bound_is_named_and_the_tree_stays(
    monkeypatch, tmp_path, caplog
):
    bench = _TeardownBench(monkeypatch, tmp_path, rm_results=[_REFUSED] * 10)
    monkeypatch.setattr(runtime, "_TEARDOWN_BOUND_SECONDS", 0.3)
    task = asyncio.create_task(runtime.launch(_TEARDOWN_TICKET, "go"))
    await bench.started()
    task.cancel()
    await bench.in_backoff()
    task.cancel()  # and the backoff never ends within the bound

    with caplog.at_level("ERROR"):
        with pytest.raises(asyncio.CancelledError) as raised:
            await task

    name = bench.rm_calls[0]
    notes = " ".join(getattr(raised.value, "__notes__", ()))
    assert f"container {name} could not be removed" in notes  # survivor named
    assert bench.workspace.exists()  # not purged under a container that may live
    assert "bound" in caplog.text
    bench.release.set()


@pytest.mark.asyncio
async def test_a_cancellation_during_teardown_after_a_timeout_is_not_swallowed(
    monkeypatch, tmp_path
):
    bench = _TeardownBench(
        monkeypatch, tmp_path, rm_results=[_REFUSED, _OK], run="timeout"
    )
    task = asyncio.create_task(runtime.launch(_TEARDOWN_TICKET, "go"))
    await bench.in_backoff()  # timed out; the removal's retry is waiting
    task.cancel()
    await bench.let_cancellation_arrive()
    assert not task.done()
    bench.release.set()

    with pytest.raises(asyncio.CancelledError):  # not the timeout error
        await task

    assert len(bench.rm_calls) == 2
    assert not bench.workspace.exists()


@pytest.mark.asyncio
async def test_an_uninterrupted_teardown_is_unchanged(monkeypatch, tmp_path):
    bench = _TeardownBench(monkeypatch, tmp_path, rm_results=[_OK], run="timeout")
    bench.release.set()

    with pytest.raises(runtime.DockerWorkerRuntimeError, match="timed out"):
        await runtime.launch(_TEARDOWN_TICKET, "go")

    assert len(bench.rm_calls) == 1
    assert not bench.workspace.exists()


@pytest.mark.asyncio
async def test_a_cancellation_during_the_staging_purge_does_not_stop_the_purge(
    monkeypatch, tmp_path
):
    """The purge runs in a thread, which a cancelled await does not interrupt."""
    import threading

    entered, release, finished = (threading.Event() for _ in range(3))
    _patch_launch(monkeypatch, tmp_path, _CODEX_STDOUT)

    def slow_purge(path, **_kwargs):
        entered.set()
        release.wait(10)
        finished.set()
        return True

    monkeypatch.setattr(runtime, "_remove_staging_tree", slow_purge)
    task = asyncio.create_task(runtime.launch(_TEARDOWN_TICKET, "go"))
    await asyncio.to_thread(entered.wait, 5)

    task.cancel()
    task.cancel()
    release.set()
    await asyncio.to_thread(finished.wait, 5)

    assert finished.is_set()
    with pytest.raises(asyncio.CancelledError):
        await task


# -- nothing reachable from an exception holds the forwarded environment --------
#
# A message free of a value is no use if the exception can still be asked for
# it: through an attribute, a closure of a function it carries, a default
# argument, a traceback frame's locals, the arguments of a log record. These
# walk everything reachable, bounded and cycle-safe, and fail on any string or
# bytes that holds a sentinel. The one sanctioned place for the Worker's own
# (screened) text is the Ticket error, ``operator_message``, which is skipped
# for the Worker sentinels only: the forwarded values may not be in it either.


def _reachable_texts(*roots, depth=14):
    import functools
    import logging
    import types

    seen, found = set(), []

    def visit(obj, level):
        if level > depth or id(obj) in seen:
            return
        seen.add(id(obj))
        if isinstance(obj, (str, bytes, bytearray)):
            found.append(obj)
        elif obj is None or isinstance(
            obj, (int, float, complex, type, types.ModuleType)
        ):
            return
        elif isinstance(obj, dict):
            for key, value in obj.items():
                visit(key, level + 1)
                visit(value, level + 1)
        elif isinstance(obj, (list, tuple, set, frozenset)):
            for item in obj:
                visit(item, level + 1)
        elif isinstance(obj, types.CellType):
            try:
                visit(obj.cell_contents, level + 1)
            except ValueError:
                return
        elif isinstance(obj, types.FunctionType):
            for part in (
                obj.__closure__,
                obj.__defaults__,
                obj.__kwdefaults__,
                obj.__dict__,
            ):
                visit(part or (), level + 1)
        elif isinstance(obj, types.MethodType):
            visit(obj.__self__, level + 1)
            visit(obj.__func__, level + 1)
        elif isinstance(obj, functools.partial):
            visit((obj.func, obj.args, obj.keywords), level + 1)
        elif isinstance(obj, types.TracebackType):
            if obj.tb_frame.f_code.co_filename != __file__:  # the caller's: outside
                visit(dict(obj.tb_frame.f_locals), level + 1)
            visit(obj.tb_next, level + 1)
        elif isinstance(obj, BaseException):
            for part in (
                obj.args,
                vars(obj),
                obj.__cause__,
                obj.__context__,
                getattr(obj, "__notes__", ()),
                obj.__traceback__,
            ):
                visit(part, level + 1)
        elif isinstance(obj, logging.LogRecord):
            visit(vars(obj), level + 1)
        elif hasattr(obj, "__dict__"):
            visit(vars(obj), level + 1)

    for root in roots:
        visit(root, 0)
    return found


def _sanctioned_ids(*errors):
    return {
        id(exc.operator_message)
        for error in errors
        for exc in _chain(error)
        if getattr(exc, "operator_message", None)
    }


def _assert_unreachable(error, caplog, *, forwarded, worker=()):
    sanctioned = _sanctioned_ids(error)
    everything = _reachable_texts(error, *caplog.records)
    for text in everything:
        flat = text if isinstance(text, str) else bytes(text).decode("utf-8", "replace")
        for value in forwarded:
            assert value not in flat
        if id(text) not in sanctioned:
            for value in worker:
                assert value not in flat
    # the walk really is walking: it reaches the Ticket error and the log text
    assert any("failed" in (t if isinstance(t, str) else "") for t in everything)


@pytest.mark.asyncio
async def test_the_scanner_would_find_a_value_in_a_closure_or_a_default(
    monkeypatch, tmp_path, caplog
):
    """The scanner itself: a value hidden where the old design kept it is found."""
    secret = "SCANNER-CONTROL-VALUE"

    def hide(value=secret):
        return lambda: value

    carrier = RuntimeError("fixed")
    carrier.keeper = hide()
    assert any(secret in text for text in _reachable_texts(carrier))
    assert any(secret in text for text in _reachable_texts(hide))
    assert any(
        secret in text
        for text in _reachable_texts(
            __import__("functools").partial(print, secret=secret)
        )
    )


@pytest.mark.asyncio
async def test_a_failed_exit_leaves_no_forwarded_value_reachable_from_the_error_or_logs(
    monkeypatch, tmp_path, caplog
):
    error = await _post_run_failure(
        monkeypatch,
        tmp_path,
        caplog,
        value="FWD-9Z-SECRET",
        stdout="WORKER-STDOUT-7Q",
        stderr="WORKER-STDERR-7Q",
        returncode=7,
    )

    assert "WORKER-STDERR-7Q" in cloud_runtime.ticket_error(error)  # the one place
    _assert_unreachable(
        error,
        caplog,
        forwarded=["FWD-9Z-SECRET"],
        worker=["WORKER-STDERR-7Q", "WORKER-STDOUT-7Q"],
    )
    # nothing callable hangs off either exception
    for exc in _chain(error):
        assert not any(callable(v) for v in vars(exc).values())


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["usage", "publication", "timeout", "refusal"])
async def test_no_other_failure_leaves_a_value_reachable_either(
    monkeypatch, tmp_path, caplog, scenario
):
    kwargs = {"value": _SENTINEL}
    if scenario == "usage":
        kwargs["stdout"] = _JSONL_BAD_USAGE
    elif scenario == "publication":
        real_publish = runtime._publish_workspace_if_safe
        try:
            "FORWARDED-123\udcff".encode("utf-8")
        except UnicodeEncodeError as encoding_error:
            db_error = encoding_error

        def arrange():
            monkeypatch.setattr(runtime, "_publish_workspace_if_safe", real_publish)
            monkeypatch.setattr(
                "src.server.database.autonomy.tickets.get_ticket",
                AsyncMock(return_value=dict(_ACTIVE)),
            )
            mocks = _publication_db(
                monkeypatch,
                metadata={},
                upserts=[{"file_path": "FORWARDED-123\udcff"}],
            )
            mocks["upsert"].side_effect = db_error

        kwargs.update(value="FORWARDED-123", arrange=arrange)
    elif scenario == "timeout":
        kwargs["arrange"] = lambda: monkeypatch.setattr(
            runtime, "_run_command_async", AsyncMock(side_effect=asyncio.TimeoutError())
        )
    else:
        kwargs["arrange"] = lambda: monkeypatch.setattr(
            runtime, "verify_docker_runtime", lambda: (False, "no engine")
        )

    error = await _post_run_failure(monkeypatch, tmp_path, caplog, **kwargs)

    _assert_unreachable(
        error, caplog, forwarded=[kwargs["value"]], worker=["AUTONOMY_STATE: ready"]
    )


@pytest.mark.asyncio
async def test_a_cancellation_leaves_no_forwarded_value_reachable(
    monkeypatch, tmp_path, caplog
):
    _patch_launch(monkeypatch, tmp_path, _CODEX_STDOUT)
    _patch_settings(
        monkeypatch,
        _settings(
            set_env=(("TOKEN", "FWD-9Z-SECRET"),), stage_root=tmp_path / "cancel-root"
        ),
    )
    monkeypatch.setattr(
        runtime, "_stage_workspace", AsyncMock(side_effect=asyncio.CancelledError())
    )

    with pytest.raises(asyncio.CancelledError) as raised:
        await runtime.launch(
            {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"}, "go"
        )

    texts = _reachable_texts(raised.value, *caplog.records)
    assert not any("FWD-9Z-SECRET" in t for t in texts if isinstance(t, str))


# -- a container that could not be removed is reported on every failing path ----


async def _failing_launch_with_a_stuck_container(
    monkeypatch, tmp_path, caplog, **kwargs
):
    holder = {}
    also = kwargs.pop("also", ())

    def arrange():
        cleanup = AsyncMock(return_value=False)  # rm -f never confirmed
        monkeypatch.setattr(runtime, "_rm_container", cleanup)
        holder["cleanup"] = cleanup
        for hook in also:
            hook()

    error = await _post_run_failure(
        monkeypatch, tmp_path, caplog, arrange=arrange, **kwargs
    )
    name = holder["cleanup"].await_args.args[1]
    return error, name


_STUCK = "could not be removed and is left for the expiry sweep"


async def _timeout_worker_exec(argv, **_kwargs):
    if argv[1] == "create":
        return _result(stdout="container-id\n")
    if argv[1] == "start":
        return _result(stdout="container-id\n")
    if argv[1] == "exec" and argv[-3:] == ("/bin/sh", "-c", "true"):
        return _result()
    raise asyncio.TimeoutError()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stdout", "returncode", "stderr"),
    [
        ("", 7, "the Worker failed"),  # a failed exit
        (_JSONL_BAD_USAGE, 0, ""),  # a run that exited 0 and could not be read
    ],
)
async def test_a_container_that_could_not_be_removed_is_in_the_ticket_error(
    monkeypatch, tmp_path, caplog, stdout, returncode, stderr
):
    error, name = await _failing_launch_with_a_stuck_container(
        monkeypatch, tmp_path, caplog, stdout=stdout, returncode=returncode, stderr=stderr
    )

    ticket = cloud_runtime.ticket_error(error)
    assert f"container {name} {_STUCK}" in ticket
    assert ticket.count(_STUCK) == 1
    assert name in str(error)  # fixed words only: in the message as well


@pytest.mark.asyncio
async def test_a_timeout_with_a_stuck_container_says_so_once(
    monkeypatch, tmp_path, caplog
):
    error, name = await _failing_launch_with_a_stuck_container(
        monkeypatch,
        tmp_path,
        caplog,
        also=[
            lambda: monkeypatch.setattr(runtime, "_run_command_async", AsyncMock(side_effect=_timeout_worker_exec))
        ],
    )

    ticket = cloud_runtime.ticket_error(error)
    assert "timed out" in ticket
    assert ticket.count(_STUCK) == 1 and name in ticket


@pytest.mark.asyncio
async def test_a_removed_container_adds_nothing_to_the_ticket_error(
    monkeypatch, tmp_path, caplog
):
    error = await _post_run_failure(
        monkeypatch, tmp_path, caplog, stderr="boom", returncode=7
    )

    assert _STUCK not in cloud_runtime.ticket_error(error)


# -- no traceback that leaves the runtime holds a frame with the Worker's data --
#
# An exception carries its traceback, the traceback carries the frames it
# passed through, and a frame carries its locals: the Worker's stdout, the
# forwarded environment. A message free of both is no use if the LogRecord's
# exc_info or a TracebackException(capture_locals=True) can still reach them.


def _render_with_locals(error) -> str:
    """Everything a handler that captures locals could print for ``error``.

    The frames of this test file are the caller's, outside the runtime, and
    hold the test's own arguments; every frame below them is the runtime's or
    its callees'.
    """
    import traceback

    def runtime_frames(tb):
        for frame, lineno in traceback.walk_tb(tb):
            if frame.f_code.co_filename != __file__:
                yield frame, lineno

    parts = []
    for exc in _chain(error):
        summary = traceback.StackSummary.extract(
            runtime_frames(exc.__traceback__), capture_locals=True
        )
        parts += summary.format()
        parts += [repr(type(exc)), repr(exc), str(exc.args)]
    return "\n".join(parts)


def _assert_no_frame_holds(error, caplog, *values):
    rendered = [_render_with_locals(error)]
    for record in caplog.records:
        if record.exc_info and record.exc_info[1] is not None:
            rendered.append(_render_with_locals(record.exc_info[1]))
    for text in rendered:
        for value in values:
            assert value not in text


@pytest.mark.asyncio
async def test_no_frame_of_a_failed_usage_parse_holds_the_workers_data(
    monkeypatch, tmp_path, caplog
):
    error = await _post_run_failure(
        monkeypatch, tmp_path, caplog, stdout=_JSONL_BAD_USAGE
    )

    # the sentinel is the forwarded value and also what the Worker wrote
    _assert_no_frame_holds(error, caplog, _SENTINEL, "AUTONOMY_STATE: ready")


@pytest.mark.asyncio
async def test_no_frame_of_a_failed_publication_holds_the_workers_data(
    monkeypatch, tmp_path, caplog
):
    real_publish = runtime._publish_workspace_if_safe
    try:
        "FORWARDED-123\udcff".encode("utf-8")
    except UnicodeEncodeError as encoding_error:
        db_error = encoding_error

    def arrange():
        monkeypatch.setattr(runtime, "_publish_workspace_if_safe", real_publish)
        monkeypatch.setattr(
            "src.server.database.autonomy.tickets.get_ticket",
            AsyncMock(return_value=dict(_ACTIVE)),
        )
        mocks = _publication_db(
            monkeypatch, metadata={}, upserts=[{"file_path": "FORWARDED-123\udcff"}]
        )
        mocks["upsert"].side_effect = db_error

    error = await _post_run_failure(
        monkeypatch, tmp_path, caplog, value="FORWARDED-123", arrange=arrange
    )

    _assert_no_frame_holds(error, caplog, "FORWARDED-123", "AUTONOMY_STATE: ready")


@pytest.mark.asyncio
async def test_no_frame_of_a_failed_exit_holds_the_workers_stderr_or_the_environment(
    monkeypatch, tmp_path, caplog
):
    error = await _post_run_failure(
        monkeypatch,
        tmp_path,
        caplog,
        value="FWD-9Z-SECRET",
        stdout="WORKER-STDOUT-7Q",
        stderr="WORKER-STDERR-7Q",
        returncode=7,
    )

    # the Ticket error still carries the (screened) output ...
    assert "WORKER-STDERR-7Q" in cloud_runtime.ticket_error(error)
    # ... and no frame anyone can walk to holds it, or the environment
    _assert_no_frame_holds(
        error, caplog, "WORKER-STDERR-7Q", "WORKER-STDOUT-7Q", "FWD-9Z-SECRET"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["timeout", "refusal"])
async def test_no_frame_of_a_refusal_or_a_timeout_holds_the_forwarded_environment(
    monkeypatch, tmp_path, caplog, how
):
    def arrange():
        if how == "timeout":
            monkeypatch.setattr(
                runtime,
                "_run_command_async",
                AsyncMock(side_effect=asyncio.TimeoutError()),
            )
        else:
            monkeypatch.setattr(
                runtime, "verify_docker_runtime", lambda: (False, "no engine")
            )

    error = await _post_run_failure(
        monkeypatch, tmp_path, caplog, value="FWD-9Z-SECRET", arrange=arrange
    )

    _assert_no_frame_holds(error, caplog, "FWD-9Z-SECRET")


@pytest.mark.asyncio
async def test_cancellation_leaves_the_runtime_with_its_frames_cleared(
    monkeypatch, tmp_path
):
    """Still the same CancelledError, propagated untouched; only its frames are emptied."""
    _patch_launch(monkeypatch, tmp_path, _CODEX_STDOUT)
    _patch_settings(
        monkeypatch,
        _settings(
            set_env=(("TOKEN", "FWD-9Z-SECRET"),), stage_root=tmp_path / "cancel-root"
        ),
    )
    marker = asyncio.CancelledError("cancelled")
    monkeypatch.setattr(runtime, "_stage_workspace", AsyncMock(side_effect=marker))

    with pytest.raises(asyncio.CancelledError) as raised:
        await runtime.launch(
            {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"}, "go"
        )

    assert raised.value is marker
    assert "FWD-9Z-SECRET" not in _render_with_locals(raised.value)


def test_the_guard_looks_for_the_raw_normalized_and_collapsed_forms():
    values = {"A": "one\ttwo", "B": "three  four"}

    assert runtime._leaks("saw one\ttwo here", values)  # raw
    assert runtime._leaks("saw one two here", values)  # normalized
    assert runtime._leaks("saw three four here", values)  # whitespace-collapsed
    assert runtime._leaks("saw one two", values)
    assert not runtime._leaks("saw one three two four", values)


def test_the_guard_ignores_whitespace_only_values_and_the_fixed_prefix():
    prefix = "isolated cloud Worker exited 7: "

    assert not runtime._leaks(prefix + "ok", {"A": " ", "B": "\t"}, safe_prefix=len(prefix))
    # a value that only occurs in the runtime's own fixed text is not a leak ...
    assert not runtime._leaks(prefix + "ok", {"A": "7"}, safe_prefix=len(prefix))
    # ... but one that reaches into the Worker's text is
    assert runtime._leaks(prefix + "7 ok", {"A": "7"}, safe_prefix=len(prefix))
    assert runtime._leaks(prefix + "ok", {"A": "7: ok"}, safe_prefix=len(prefix))


@pytest.mark.asyncio
async def test_an_exception_that_is_not_the_runtimes_is_still_bounded_by_cloud_runtime(
    monkeypatch,
):
    monkeypatch.setattr(
        cloud_runtime, "get_autonomy_cloud_worker_runtime", lambda: "docker"
    )
    monkeypatch.setattr(
        cloud_runtime, "ISOLATED_RUNTIME_VERIFIERS", {"docker": lambda: (True, "ok")}
    )

    async def boom(_ticket, _prompt):
        raise RuntimeError("y" * 500)

    monkeypatch.setattr(cloud_runtime, "ISOLATED_RUNTIME_LAUNCHERS", {"docker": boom})

    with pytest.raises(cloud_runtime.CloudWorkerLaunchError) as raised:
        await cloud_runtime.launch_cloud_worker({}, "go")

    assert str(raised.value).endswith("y" * 300 + "...")


@pytest.mark.asyncio
async def test_staging_snapshot_is_removed_when_materialization_is_cancelled(
    monkeypatch, tmp_path
):
    settings = _settings(stage_root=tmp_path / "stage-root")

    async def _cancelled(**_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        "src.server.services.native_runtime.workspace.materialize_workspace",
        _cancelled,
    )

    with pytest.raises(asyncio.CancelledError):
        await runtime._stage_workspace(
            {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"},
            settings,
        )

    assert list((tmp_path / "stage-root").iterdir()) == []


def _slow_materializer(monkeypatch):
    """A materializer whose writer thread runs until the test lets it go."""
    import threading

    gate = SimpleNamespace(
        started=threading.Event(), release=threading.Event(), wrote=threading.Event()
    )

    async def materializer(*, workspace_id, cwd, user_id):
        def blocking():
            gate.started.set()
            gate.release.wait(10)
            target = Path(cwd) / "late" / "written-by-the-materializer.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("late")
            gate.wrote.set()

        await asyncio.to_thread(blocking)
        return 1

    monkeypatch.setattr(
        "src.server.services.native_runtime.workspace.materialize_workspace",
        materializer,
    )
    return gate


_STAGE_TICKET = {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"}


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_the_materializer_before_cleaning_up(
    monkeypatch, tmp_path
):
    """A second cancel used to be swallowed and the stage removed under the writer,
    which then recreated it."""
    gate = _slow_materializer(monkeypatch)
    root = tmp_path / "stage-root"
    task = asyncio.create_task(
        runtime._stage_workspace(_STAGE_TICKET, _settings(stage_root=root))
    )
    await asyncio.to_thread(gate.started.wait, 5)

    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done(), "cleanup ran while the materializer could still write"
    assert any(root.iterdir())

    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert gate.wrote.is_set()
    # Removed after the writer finished, so nothing could recreate it.
    assert list(root.iterdir()) == []


@pytest.mark.asyncio
async def test_a_materializer_that_never_finishes_leaves_the_stage_in_place(
    monkeypatch, tmp_path, caplog
):
    gate = _slow_materializer(monkeypatch)
    monkeypatch.setattr(runtime, "_MATERIALIZE_GRACE_SECONDS", 0.3)
    root = tmp_path / "stage-root"
    task = asyncio.create_task(
        runtime._stage_workspace(_STAGE_TICKET, _settings(stage_root=root))
    )
    await asyncio.to_thread(gate.started.wait, 5)
    try:
        with caplog.at_level("WARNING"):
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        entries = [e for e in root.iterdir() if not e.name.endswith(".owner")]
        assert len(entries) == 1  # not removed from under the writer
        assert "left in place" in caplog.text
    finally:
        gate.release.set()
        await asyncio.to_thread(gate.wrote.wait, 5)


# ---------------------------------------------------------------------------
# Staging cleanup, container removal and leftovers
# ---------------------------------------------------------------------------


def _hostile_tree(root: Path) -> Path:
    """What a Worker can leave: a directory chmod 000 with bytes inside it."""
    tree = root / "stage-1"
    inner = tree / "trap" / "inner"
    inner.mkdir(parents=True)
    (inner / "big.bin").write_bytes(b"x" * 4096)
    (tree / "ok.txt").write_text("fine")
    inner.chmod(0o000)
    (tree / "trap").chmod(0o000)
    return tree


def test_a_tree_the_worker_made_unreadable_is_still_removed(tmp_path):
    tree = _hostile_tree(tmp_path)

    shutil.rmtree(tree, ignore_errors=True)
    assert tree.exists(), "plain rmtree silently leaves it: the bug being fixed"

    assert runtime._remove_staging_tree(tree) is True
    assert not tree.exists()


def test_cleanup_does_not_follow_a_symlink_out_of_the_tree(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("host data")
    outside.chmod(0o500)
    tree = tmp_path / "stage"
    tree.mkdir()
    (tree / "link").symlink_to(outside)
    try:
        assert runtime._remove_staging_tree(tree) is True
        # A followed link would have restored write access here and deleted it.
        assert (outside / "keep.txt").read_text() == "host data"
        assert stat.S_IMODE(outside.stat().st_mode) == 0o500
    finally:
        outside.chmod(0o700)


def test_cleanup_reports_a_tree_it_could_not_remove(tmp_path, caplog):
    tree = _hostile_tree(tmp_path)
    try:
        # Nothing is touched while the container may still be running.
        with caplog.at_level("WARNING"):
            assert runtime._remove_staging_tree(tree, container_gone=False) is False
        assert "left in place" in caplog.text
        assert tree.exists()
        assert stat.S_IMODE((tree / "trap").stat().st_mode) == 0
        assert (tree / "ok.txt").read_text() == "fine"
    finally:
        runtime._remove_staging_tree(tree)


def _result(returncode=0, stdout="", stderr=""):
    return runtime.DockerCommandResult(
        argv=("docker",), returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture
def _no_backoff(monkeypatch):
    slept = []

    async def _sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(runtime.asyncio, "sleep", _sleep)
    return slept


@pytest.mark.asyncio
async def test_container_removal_is_retried_until_it_is_confirmed(
    monkeypatch, _no_backoff
):
    runner = AsyncMock(
        side_effect=[_result(1, "", "daemon busy"), asyncio.TimeoutError(), _result()]
    )
    monkeypatch.setattr(runtime, "_run_command_async", runner)

    assert await runtime._rm_container(_settings(), "conductor-cloud-worker-abc") is True
    assert runner.await_count == 3
    assert _no_backoff == [1.0, 2.0]


@pytest.mark.asyncio
async def test_a_container_that_is_already_gone_counts_as_removed(monkeypatch):
    runner = AsyncMock(
        return_value=_result(1, "", "Error response from daemon: No such container: x")
    )
    monkeypatch.setattr(runtime, "_run_command_async", runner)

    assert await runtime._rm_container(_settings(), "conductor-cloud-worker-abc") is True
    assert runner.await_count == 1


@pytest.mark.asyncio
async def test_a_container_that_cannot_be_removed_is_reported_loudly(
    monkeypatch, caplog, _no_backoff
):
    runner = AsyncMock(return_value=_result(1, "", "permission denied"))
    monkeypatch.setattr(runtime, "_run_command_async", runner)

    with caplog.at_level("ERROR"):
        removed = await runtime._rm_container(_settings(), "conductor-cloud-worker-abc")

    assert removed is False
    assert runner.await_count == runtime._RM_ATTEMPTS
    assert "conductor-cloud-worker-abc could not be removed" in caplog.text
    assert "expiry sweep" in caplog.text


@pytest.mark.asyncio
async def test_launch_says_so_when_a_timed_out_container_survives(
    monkeypatch, tmp_path
):
    workspace, runner, cleanup, _publish, _stage = _patch_launch(
        monkeypatch, tmp_path, ""
    )
    runner.worker_exception = asyncio.TimeoutError()
    cleanup.return_value = False
    (workspace / "result.txt").write_text("the Worker may still be writing")
    removals = []
    real_remove = runtime._remove_staging_tree
    monkeypatch.setattr(
        runtime,
        "_remove_staging_tree",
        lambda path, **kw: removals.append(kw) or real_remove(path, **kw),
    )
    repair = MagicMock()
    monkeypatch.setattr(runtime, "_purge_tree", repair)

    with pytest.raises(runtime.DockerWorkerRuntimeError) as raised:
        await runtime.launch(
            {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"}, "go"
        )

    assert "timed out" in str(raised.value)
    assert "could not be removed" in str(raised.value)
    # The Worker may still be writing: its tree is left exactly where it is,
    # for the sweep to remove once the container is gone.
    assert removals == [{"container_gone": False}]
    repair.assert_not_called()
    assert (workspace / "result.txt").read_text() == "the Worker may still be writing"


@pytest.mark.asyncio
async def test_launch_repairs_and_removes_a_tree_after_a_confirmed_removal(
    monkeypatch, tmp_path
):
    workspace, _runner, _cleanup, _publish, _stage = _patch_launch(
        monkeypatch, tmp_path, _CODEX_STDOUT
    )
    (workspace / "trap" / "inner").mkdir(parents=True)
    (workspace / "trap" / "inner" / "f").write_text("x")
    (workspace / "trap" / "inner").chmod(0)
    (workspace / "trap").chmod(0)

    report = await runtime.launch(
        {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"}, "go"
    )

    assert report["state"] == "ready"
    assert not workspace.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mandate", [["not-a-dict"], "speculative", 7])
async def test_a_malformed_mandate_is_refused_before_any_secret_is_written(
    monkeypatch, tmp_path, mandate
):
    _patch_launch(monkeypatch, tmp_path, _CODEX_STDOUT)
    root = tmp_path / "secret-root"
    _patch_settings(
        monkeypatch,
        _settings(stage_root=root, set_env=(("TOKEN", "s3cret-value"),)),
    )

    with pytest.raises(runtime.DockerWorkerRuntimeError, match="mandate is malformed"):
        await runtime.launch(
            {
                "ticket_id": "ticket-1",
                "user_id": "u1",
                "workspace_id": "ws-1",
                "mandate": mandate,
            },
            "go",
        )

    assert not root.exists() or list(root.iterdir()) == []


@pytest.mark.asyncio
async def test_the_env_file_is_removed_when_staging_fails(monkeypatch, tmp_path):
    _patch_launch(monkeypatch, tmp_path, _CODEX_STDOUT)
    root = tmp_path / "secret-root"
    _patch_settings(
        monkeypatch,
        _settings(stage_root=root, set_env=(("TOKEN", "s3cret-value"),)),
    )
    monkeypatch.setattr(
        runtime, "_workspace_metadata", AsyncMock(side_effect=RuntimeError("db down"))
    )

    # The stage's own error is replaced by a fixed one (its text is not trusted
    # to be free of what the workspace holds); the env file goes either way.
    with pytest.raises(
        runtime.DockerWorkerRuntimeError, match="reading the workspace manifest"
    ) as raised:
        await runtime.launch(
            {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"}, "go"
        )

    assert "db down" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert list(root.iterdir()) == []


@pytest.mark.asyncio
async def test_a_speculative_ticket_is_mounted_read_only_and_never_published(
    monkeypatch, tmp_path
):
    _workspace, runner, _cleanup, publish, _stage = _patch_launch(
        monkeypatch, tmp_path, _CODEX_STDOUT
    )

    await runtime.launch(
        {
            "ticket_id": "ticket-1",
            "user_id": "u1",
            "workspace_id": "ws-1",
            "mandate": {"speculative": True},
        },
        "go",
    )

    assert runner.create_argv[
        runner.create_argv.index("--mount") + 1
    ].endswith(",readonly")
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_launch_sweeps_leftovers_once_the_runtime_is_verified(
    monkeypatch, tmp_path
):
    _patch_launch(monkeypatch, tmp_path, _CODEX_STDOUT)
    sweep = MagicMock()
    monkeypatch.setattr(runtime, "sweep_leftovers", sweep)
    ticket = {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"}

    await runtime.launch(ticket, "go")
    await runtime.launch(ticket, "go")  # inside the interval: no second sweep

    assert sweep.call_count == 1

    monkeypatch.setattr(runtime, "_last_sweep", time.monotonic() - 61)
    await runtime.launch(ticket, "go")
    assert sweep.call_count == 2


@pytest.mark.asyncio
async def test_a_refused_launch_does_not_sweep(monkeypatch):
    sweep = MagicMock()
    monkeypatch.setattr(runtime, "sweep_leftovers", sweep)
    monkeypatch.setattr(runtime, "verify_docker_runtime", lambda: (False, "no engine"))

    with pytest.raises(runtime.DockerWorkerRuntimeUnavailable):
        await runtime.launch(
            {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"}, "go"
        )

    sweep.assert_not_called()


# -- the expiry sweep -------------------------------------------------------


def _listing(*rows):
    return (0, "".join(f"{name} {deadline}\n" for name, deadline in rows), "")


def test_the_sweep_removes_only_expired_labelled_worker_containers():
    runner = _FakeSyncRunner(
        [
            _listing(
                ("conductor-cloud-worker-aaaa1111", 1_000),  # expired: reaped
                ("conductor-cloud-worker-bbbb2222", 9_999_999_999),  # still live
                ("not-ours-but-labelled", 1_000),  # label alone is not enough
                ("conductor-cloud-worker-cccc3333", "garbage"),  # unparsable
            ),
            (0, "conductor-cloud-worker-aaaa1111", ""),
        ]
    )
    settings = _settings(runner=runner)

    reaped = runtime.reap_expired_containers(settings, now=2_000)

    assert reaped == ["conductor-cloud-worker-aaaa1111"]
    assert runner.calls[0][1:4] == ("ps", "-a", "--filter")
    assert f"label={runtime._LABEL_DEADLINE}" in runner.calls[0]
    assert runner.calls[1][1:] == ("rm", "-f", "conductor-cloud-worker-aaaa1111")
    assert len(runner.calls) == 2


def test_a_container_that_survives_the_sweep_is_not_reported_removed(caplog):
    runner = _FakeSyncRunner(
        [_listing(("conductor-cloud-worker-aaaa1111", 1)), (1, "", "busy")]
    )

    with caplog.at_level("ERROR"):
        assert runtime.reap_expired_containers(_settings(runner=runner), now=5) == []

    assert "survived" in caplog.text


def test_the_sweep_fails_visibly_when_the_engine_cannot_list():
    runner = _FakeSyncRunner([(1, "", "daemon down")])

    with pytest.raises(runtime.DockerWorkerRuntimeUnavailable, match="daemon down"):
        runtime.reap_expired_containers(_settings(runner=runner))


_SMOKE_LABEL = "conductor.cloud-worker-smoke=run1234"


def _scoped_listing(*rows):
    """``docker ps`` rows as a scoped sweep asks for them: name, deadline, scope value."""
    return (
        0,
        "".join(f"{name} {deadline} {scope}\n" for name, deadline, scope in rows),
        "",
    )


def test_a_scoped_sweep_never_removes_a_foreign_expired_container():
    """The smoke shares an engine with a real backend: expired is not the same as owned."""
    runner = _FakeSyncRunner(
        [
            _scoped_listing(
                ("conductor-cloud-worker-aaaa1111", 1_000, "run1234"),  # owned
                ("conductor-cloud-worker-bbbb2222", 1_000, ""),  # foreign, no label
                ("conductor-cloud-worker-cccc3333", 1_000, "otherrun"),  # another run
            ),
            (0, "conductor-cloud-worker-aaaa1111", ""),
        ]
    )
    settings = _settings(runner=runner, sweep_label=_SMOKE_LABEL)

    reaped = runtime.reap_expired_containers(settings, now=2_000)

    assert reaped == ["conductor-cloud-worker-aaaa1111"]
    removals = [call[-1] for call in runner.calls if call[1:3] == ("rm", "-f")]
    assert removals == ["conductor-cloud-worker-aaaa1111"]
    # The daemon is asked for the owned set only, and the answer is checked again
    # here, so a daemon that ignored the filter still cannot widen the sweep.
    listing = runner.calls[0]
    assert f"label={runtime._LABEL_DEADLINE}" in listing
    assert f"label={_SMOKE_LABEL}" in listing


def test_a_scoped_sweep_through_sweep_leftovers_leaves_foreign_containers_alone(
    tmp_path,
):
    runner = _FakeSyncRunner(
        [
            _scoped_listing(
                ("conductor-cloud-worker-aaaa1111", 1_000, "run1234"),
                ("conductor-cloud-worker-bbbb2222", 1_000, ""),
            ),
            (0, "conductor-cloud-worker-aaaa1111", ""),
            (0, "", ""),  # which staging entries still have a container
        ]
    )
    settings = _settings(
        runner=runner, sweep_label=_SMOKE_LABEL, stage_root=tmp_path / "root"
    )

    _REAL_SWEEP_LEFTOVERS(settings)

    removed = [call[-1] for call in runner.calls if call[1:3] == ("rm", "-f")]
    assert removed == ["conductor-cloud-worker-aaaa1111"]


def test_an_unscoped_sweep_keeps_the_product_behaviour():
    runner = _FakeSyncRunner(
        [
            _listing(("conductor-cloud-worker-aaaa1111", 1_000)),
            (0, "", ""),
        ]
    )

    reaped = runtime.reap_expired_containers(_settings(runner=runner), now=2_000)

    assert reaped == ["conductor-cloud-worker-aaaa1111"]
    assert not any("cloud-worker-smoke" in part for part in runner.calls[0])


def test_the_sweep_scope_is_read_from_the_environment():
    settings = runtime.DockerWorkerSettings.from_env(
        {"AUTONOMY_CLOUD_WORKER_SWEEP_LABEL": _SMOKE_LABEL}
    )

    assert settings.sweep_label == _SMOKE_LABEL


def test_sweep_leftovers_survives_a_failing_half(monkeypatch, caplog):
    staging = MagicMock()
    monkeypatch.setattr(runtime, "sweep_stale_staging", staging)
    monkeypatch.setattr(
        runtime,
        "_reap_expired",
        MagicMock(side_effect=runtime.DockerWorkerRuntimeUnavailable("engine down")),
    )

    with caplog.at_level("WARNING"):
        _REAL_SWEEP_LEFTOVERS(_settings())

    staging.assert_called_once()
    assert "container sweep failed" in caplog.text


# -- stale staging entries --------------------------------------------------


def test_stale_staging_entries_are_swept_and_fresh_ones_kept(tmp_path):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    stale_env = root / f"env-{'a' * 32}.env"
    runtime._claim_entry(root, stale_env.name)
    stale_env.write_text("TOKEN=secret\n")
    runtime._claim_entry(root, "ticket01-0123456789ab")
    stale_dir = _hostile_tree(root).rename(root / "ticket01-0123456789ab")
    runtime._claim_entry(root, "probe-ab12_cd3")
    probe_dir = root / "probe-ab12_cd3"
    probe_dir.mkdir()
    foreign = root / "my-notes"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("not ours")
    settings = _settings(stage_root=root, timeout_seconds=100)

    # Entries younger than timeout + 2 * grace are a live run's: left alone.
    assert runtime.sweep_stale_staging(settings, trees_with_container=frozenset()) == []
    assert stale_env.exists() and stale_dir.exists()

    later = time.time() + 100 + 2 * runtime._REAP_GRACE_SECONDS + 5
    removed = runtime.sweep_stale_staging(
        settings, now=later, trees_with_container=frozenset()
    )

    assert sorted(removed) == sorted([stale_env.name, stale_dir.name, probe_dir.name])
    assert not stale_env.exists() and not stale_dir.exists()
    # Names that are not the runtime's own are never touched.
    assert (foreign / "keep.txt").read_text() == "not ours"


def test_staging_sweep_tolerates_a_missing_root(tmp_path):
    settings = _settings(stage_root=tmp_path / "never-created")

    assert runtime.sweep_stale_staging(settings, trees_with_container=frozenset()) == []


# -- staging is repaired only once no Worker can still be running ----------


def _stale_root(tmp_path, monkeypatch):
    """A staging root holding a stale env file and a hostile tree, both swept by age."""
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    stale_env = root / f"env-{'a' * 32}.env"
    runtime._claim_entry(root, stale_env.name)
    stale_env.write_text("TOKEN=secret\n")
    runtime._claim_entry(root, "ticket01-0123456789ab")
    stale_dir = _hostile_tree(root).rename(root / "ticket01-0123456789ab")
    # A grace this negative puts every entry past the age cutoff at once.
    monkeypatch.setattr(runtime, "_REAP_GRACE_SECONDS", -1000.0)
    return root, stale_env, stale_dir


def _forbid_repair(monkeypatch):
    repair = MagicMock(side_effect=AssertionError("repair walk over a live tree"))
    monkeypatch.setattr(runtime, "_purge_tree", repair)
    return repair


def test_a_tree_is_left_in_place_while_a_worker_may_still_run(
    tmp_path, monkeypatch, caplog
):
    root, stale_env, stale_dir = _stale_root(tmp_path, monkeypatch)
    settings = _settings(stage_root=root, timeout_seconds=100)
    real_repair = runtime._purge_tree
    repair = _forbid_repair(monkeypatch)
    try:
        with caplog.at_level("WARNING"):
            removed = runtime.sweep_stale_staging(settings, trees_with_container=None)

        # The credentials file has no tree to walk and goes either way.
        assert removed == [stale_env.name]
        assert not stale_env.exists()
        # The tree is untouched: still chmod 000, nothing chmodded, nothing deleted.
        assert stale_dir.exists()
        assert stat.S_IMODE((stale_dir / "trap").stat().st_mode) == 0
        repair.assert_not_called()
        assert "left in place" in caplog.text
    finally:
        real_repair(stale_dir)


def test_the_sweep_leaves_staging_alone_when_an_expired_container_survives(
    tmp_path, monkeypatch, caplog
):
    root, _env, stale_dir = _stale_root(tmp_path, monkeypatch)
    runner = _FakeSyncRunner(
        [_listing(("conductor-cloud-worker-aaaa1111", 1)), (1, "", "daemon busy")]
    )
    settings = _settings(runner=runner, stage_root=root, timeout_seconds=100)
    real_repair = runtime._purge_tree
    repair = _forbid_repair(monkeypatch)
    try:
        with caplog.at_level("WARNING"):
            _REAL_SWEEP_LEFTOVERS(settings)

        assert stale_dir.exists()
        assert stat.S_IMODE((stale_dir / "trap").stat().st_mode) == 0
        repair.assert_not_called()
    finally:
        real_repair(stale_dir)


def test_the_sweep_leaves_staging_alone_when_it_cannot_list_containers(
    tmp_path, monkeypatch
):
    root, _env, stale_dir = _stale_root(tmp_path, monkeypatch)
    runner = _FakeSyncRunner([(1, "", "daemon down")])
    settings = _settings(runner=runner, stage_root=root, timeout_seconds=100)
    real_repair = runtime._purge_tree
    repair = _forbid_repair(monkeypatch)
    try:
        _REAL_SWEEP_LEFTOVERS(settings)

        assert stale_dir.exists()
        repair.assert_not_called()
    finally:
        real_repair(stale_dir)


def test_the_sweep_repairs_staging_once_the_containers_are_confirmed_gone(
    tmp_path, monkeypatch
):
    root, stale_env, stale_dir = _stale_root(tmp_path, monkeypatch)
    runner = _FakeSyncRunner(
        [
            _listing(("conductor-cloud-worker-aaaa1111", 1)),
            (0, "", ""),  # rm -f
            (0, "", ""),  # no container is left for any staging entry
        ]
    )
    settings = _settings(runner=runner, stage_root=root, timeout_seconds=100)

    _REAL_SWEEP_LEFTOVERS(settings)

    assert not stale_dir.exists()
    assert not stale_env.exists()


def test_a_tree_is_repaired_when_there_is_no_container_to_remove(
    tmp_path, monkeypatch
):
    root, _env, stale_dir = _stale_root(tmp_path, monkeypatch)
    runner = _FakeSyncRunner([(0, "", ""), (0, "", "")])
    settings = _settings(runner=runner, stage_root=root, timeout_seconds=100)

    _REAL_SWEEP_LEFTOVERS(settings)

    assert not stale_dir.exists()


def _tree_label_listing(*names):
    return (0, "".join(f"{name}\n" for name in names), "")


def test_a_tree_whose_container_still_exists_is_left_whatever_the_current_timeout(
    tmp_path, monkeypatch
):
    """The tree is judged by its own container, not by today's configuration.

    An earlier Worker ran with a 3600 s limit; the operator has since set 10 s.
    Its container has not expired, so nothing is removed, and the sweep must
    not read the shorter limit as licence to delete the tree under it.
    """
    root, stale_env, stale_dir = _stale_root(tmp_path, monkeypatch)
    runner = _FakeSyncRunner(
        [
            _listing(("conductor-cloud-worker-aaaa1111", 9_999_999_999)),  # unexpired
            _tree_label_listing(stale_dir.name),
        ]
    )
    settings = _settings(runner=runner, stage_root=root, timeout_seconds=10)
    real_purge = runtime._purge_tree
    purge = _forbid_repair(monkeypatch)
    try:
        _REAL_SWEEP_LEFTOVERS(settings)

        assert stat.S_IMODE((stale_dir / "trap").stat().st_mode) == 0
        purge.assert_not_called()
        assert not any(call[1:3] == ("rm", "-f") for call in runner.calls)
    finally:
        real_purge(stale_dir)


def test_a_tree_with_no_container_is_removed_once_it_is_old_enough(
    tmp_path, monkeypatch
):
    root, stale_env, stale_dir = _stale_root(tmp_path, monkeypatch)
    settings = _settings(stage_root=root, timeout_seconds=10)

    removed = runtime.sweep_stale_staging(
        settings, trees_with_container=frozenset({"someone-else-0123456789ab"})
    )

    assert sorted(removed) == sorted([stale_env.name, stale_dir.name])
    assert not stale_dir.exists()


def test_a_tree_whose_launch_has_not_started_its_container_yet_is_protected(
    tmp_path,
):
    """Staging exists before the container does: only age can tell that launch apart."""
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    runtime._claim_entry(root, "ticket01-0123456789ab")
    young = root / "ticket01-0123456789ab"
    young.mkdir()
    (young / "seed.txt").write_text("being materialized")
    settings = _settings(stage_root=root, timeout_seconds=10)

    assert runtime.sweep_stale_staging(
        settings, trees_with_container=frozenset()
    ) == []
    assert (young / "seed.txt").exists()

    later = time.time() + 2 * runtime._REAP_GRACE_SECONDS + 5
    assert runtime.sweep_stale_staging(
        settings, now=later, trees_with_container=frozenset()
    ) == [young.name]


def test_the_sweep_leaves_trees_alone_when_it_cannot_ask_which_have_containers(
    tmp_path, monkeypatch
):
    root, _env, stale_dir = _stale_root(tmp_path, monkeypatch)
    runner = _FakeSyncRunner([(0, "", ""), (1, "", "daemon went away")])
    settings = _settings(runner=runner, stage_root=root, timeout_seconds=100)
    real_purge = runtime._purge_tree
    purge = _forbid_repair(monkeypatch)
    try:
        _REAL_SWEEP_LEFTOVERS(settings)

        assert stale_dir.exists()
        purge.assert_not_called()
    finally:
        real_purge(stale_dir)


def test_every_container_is_labelled_with_the_staging_entry_it_mounts():
    label = f"{runtime._LABEL_STAGE}=ticket01-0123456789ab"
    argv = _argv(workspace="/home/you/root/ticket01-0123456789ab")
    assert label in argv

    probe = _argv(workspace="/home/you/root/probe-ab12_cd3")
    assert f"{runtime._LABEL_STAGE}=probe-ab12_cd3" in probe

    # A workspace the sweep would never touch gets no claim on a name.
    assert not any(runtime._LABEL_STAGE in part for part in _argv(workspace="/tmp/stage"))
    assert not any(
        runtime._LABEL_STAGE in part for part in _argv(workspace=None)
    )


def test_the_listing_of_trees_in_use_asks_every_container_in_any_state():
    runner = _FakeSyncRunner([_tree_label_listing("a-0123456789ab", "probe-ab12cd34", "")])

    names = runtime._trees_with_containers(_settings(runner=runner))

    assert names == frozenset({"a-0123456789ab", "probe-ab12cd34"})
    call = runner.calls[0]
    assert call[1:3] == ("ps", "-a")
    assert f"label={runtime._LABEL_STAGE}" in call


def _swap_for_symlink(monkeypatch, victim: Path, target: Path):
    """Replace ``victim`` with a symlink to ``target`` the moment the walk first
    looks at it: the Worker winning the race between the check and the chmod."""
    swapped = []

    def swap():
        if swapped:
            return
        swapped.append(True)
        victim.chmod(0o700)
        for child in victim.iterdir():
            child.unlink()
        victim.rmdir()
        victim.symlink_to(target)

    def is_victim(path) -> bool:
        return isinstance(path, (str, os.PathLike)) and Path(path).name == victim.name

    def watching(real):
        def inner(path, *args, **kwargs):
            try:
                return real(path, *args, **kwargs)
            finally:
                if is_victim(path):
                    swap()

        return inner

    # Whichever primitive the walk looks at the victim with, the swap follows it.
    monkeypatch.setattr(os, "lstat", watching(os.lstat))
    monkeypatch.setattr(os, "stat", watching(os.stat))
    real_open = os.open

    def opening(path, flags, mode=0o777, *, dir_fd=None):
        try:
            return real_open(path, flags, mode, dir_fd=dir_fd)
        finally:
            if is_victim(path):
                swap()

    monkeypatch.setattr(os, "open", opening)
    return swapped


def test_a_directory_swapped_for_a_symlink_mid_walk_is_not_followed(
    tmp_path, monkeypatch
):
    """Check-then-chmod by path follows a link the Worker put in the dir's place."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("host data")
    outside.chmod(0o500)
    tree = tmp_path / "stage"
    victim = tree / "victim"
    victim.mkdir(parents=True)
    (victim / "f").write_text("x")
    victim.chmod(0o000)
    swapped = _swap_for_symlink(monkeypatch, victim, outside)
    try:
        # The link is removed as a link; its target is never chmodded or emptied.
        assert runtime._remove_staging_tree(tree) is True

        assert swapped, "the race was never exercised"
        assert not tree.exists()
        assert stat.S_IMODE(outside.stat().st_mode) == 0o500
        assert (outside / "keep.txt").read_text() == "host data"
    finally:
        outside.chmod(0o700)


def test_the_purge_repairs_every_depth_without_following_links(tmp_path):
    tree = _hostile_tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("host data")
    outside.chmod(0o500)
    (tree / "ok.txt").chmod(0o000)  # a file mode is not the walk's business
    (tree / "link").symlink_to(outside)
    try:
        assert runtime._remove_staging_tree(tree) is True

        assert not tree.exists()
        assert stat.S_IMODE(outside.stat().st_mode) == 0o500
        assert (outside / "keep.txt").read_text() == "host data"
    finally:
        outside.chmod(0o700)


def _deep_tree(root: Path, depth: int) -> Path:
    """``depth`` nested directories, built relative to descriptors (a path that
    long cannot be named). The deepest holds a file and is chmod 000, and so
    is one from the middle: a Worker's trap that sits far below the root."""
    root.mkdir()
    top = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    cur, middle = top, None
    for level in range(depth):
        os.mkdir("d", dir_fd=cur)
        nxt = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=cur)
        if level == depth // 2:
            middle = nxt
        elif cur != top and cur != middle:
            os.close(cur)
        cur = nxt
    os.close(os.open("big", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=cur))
    os.fchmod(cur, 0)
    os.fchmod(middle, 0)
    for fd in {top, cur, middle}:
        os.close(fd)
    return root


def test_a_hostile_directory_far_below_the_root_is_still_removed(tmp_path):
    """A depth bound left such a tree on the host, and every later sweep re-walked it."""
    tree = _deep_tree(tmp_path / "stage", 300)

    assert runtime._remove_staging_tree(tree) is True

    assert not os.path.lexists(tree)


def test_the_purge_needs_neither_recursion_nor_a_descriptor_per_level(tmp_path):
    import resource
    import sys
    import traceback

    tree = _deep_tree(tmp_path / "stage", 400)
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    in_use = len(os.listdir("/dev/fd"))
    recursion = sys.getrecursionlimit()
    # Far less headroom than the tree is deep, on both counts.
    resource.setrlimit(resource.RLIMIT_NOFILE, (in_use + 16, hard))
    sys.setrecursionlimit(len(traceback.extract_stack()) + 80)
    try:
        removed = runtime._remove_staging_tree(tree)
    finally:
        sys.setrecursionlimit(recursion)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))

    assert removed is True
    assert not os.path.lexists(tree)


def test_the_purge_stops_when_the_way_back_up_is_not_the_directory_it_left(
    tmp_path, caplog
):
    """It returns to a parent through ``..`` and checks whose directory that is."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("not the Worker's")
    tree = tmp_path / "stage"
    (tree / "a" / "b").mkdir(parents=True)
    (tree / "a" / "b" / "f").write_text("x")
    real_open = os.open

    def opening(path, flags, mode=0o777, *, dir_fd=None):
        if path == "..":
            return real_open(elsewhere, flags)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    with caplog.at_level("WARNING"):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "open", opening)
            assert runtime._remove_staging_tree(tree) is False

    assert (elsewhere / "keep.txt").read_text() == "not the Worker's"
    assert tree.exists()
    assert "could not be removed" in caplog.text


# -- the sweep deletes only what this runtime created ---------------------------
#
# A name of the right shape and an age are not evidence of ownership: a staging
# root that is shared holds other things. Every entry the runtime creates has a
# marker beside it in the (private) staging root, outside the bind-mounted
# workspace a Worker can write, and the sweep considers nothing without one.

_OLD = 10 * 24 * 3600  # far past any age gate


def _old_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    return root, _settings(stage_root=root, timeout_seconds=100)


def _sweep_far_in_the_future(settings):
    return runtime.sweep_stale_staging(
        settings, now=time.time() + _OLD, trees_with_container=frozenset()
    )


def test_an_unrelated_directory_of_the_right_shape_is_never_deleted(tmp_path):
    """The reviewer's notes-abcdef123456: old, no container, a name the sweep knows."""
    root, settings = _old_root(tmp_path)
    notes = root / "notes-abcdef123456"
    notes.mkdir()
    (notes / "keep.txt").write_text("somebody's notes")
    stray_env = root / f"env-{'b' * 32}.env"
    stray_env.write_text("not ours either")
    stray_probe = root / "probe-abcd1234"
    stray_probe.mkdir()

    assert _sweep_far_in_the_future(settings) == []

    assert (notes / "keep.txt").read_text() == "somebody's notes"
    assert stray_env.read_text() == "not ours either"
    assert stray_probe.is_dir()


def test_what_the_runtime_created_is_swept_with_its_marker(tmp_path):
    root, settings = _old_root(tmp_path)
    runtime._claim_entry(root, "ticket01-0123456789ab")
    (root / "ticket01-0123456789ab").mkdir()
    env = f"env-{'c' * 32}.env"
    runtime._claim_entry(root, env)
    (root / env).write_text("TOKEN=x\n")

    removed = _sweep_far_in_the_future(settings)

    assert sorted(removed) == sorted(["ticket01-0123456789ab", env])
    assert list(root.iterdir()) == []  # the markers go with their entries


def test_a_marker_is_private_and_made_once(tmp_path):
    marker = runtime._claim_entry(tmp_path, "ticket01-0123456789ab")

    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        runtime._claim_entry(tmp_path, "ticket01-0123456789ab")


@pytest.mark.parametrize(
    "forgery",
    [
        "conductor-cloud-worker-owner v1\nentry=someone-else-0123456789ab\ntoken=" + "a" * 32 + "\n",
        "another-program-owner v1\nentry=ticket01-0123456789ab\ntoken=" + "a" * 32 + "\n",
        "conductor-cloud-worker-owner v1\nentry=ticket01-0123456789ab\ntoken=short\n",
        "conductor-cloud-worker-owner v1\nentry=ticket01-0123456789ab\n",
        "conductor-cloud-worker-owner v1\nentry=ticket01-0123456789ab\ntoken="
        + "a" * 32
        + "\nextra=1\n",
        "",
        "x" * 4096,
    ],
)
def test_a_marker_that_does_not_say_this_entry_is_not_ownership(tmp_path, forgery):
    root, settings = _old_root(tmp_path)
    entry = root / "ticket01-0123456789ab"
    entry.mkdir()
    marker = root / "ticket01-0123456789ab.owner"
    marker.write_text(forgery)
    marker.chmod(0o600)

    assert _sweep_far_in_the_future(settings) == []

    assert entry.is_dir()


def test_a_marker_that_is_a_symlink_is_not_ownership(tmp_path):
    root, settings = _old_root(tmp_path)
    real = runtime._claim_entry(tmp_path, "ticket01-0123456789ab")  # valid, elsewhere
    entry = root / "ticket01-0123456789ab"
    entry.mkdir()
    (root / "ticket01-0123456789ab.owner").symlink_to(real)

    assert _sweep_far_in_the_future(settings) == []

    assert entry.is_dir()
    assert real.exists()  # and the link's target was not followed to be deleted


def test_a_marker_owned_by_another_user_is_not_ownership(tmp_path, monkeypatch):
    root, settings = _old_root(tmp_path)
    runtime._claim_entry(root, "ticket01-0123456789ab")
    entry = root / "ticket01-0123456789ab"
    entry.mkdir()
    monkeypatch.setattr(runtime, "_current_uid", lambda: os.getuid() + 1)

    assert _sweep_far_in_the_future(settings) == []

    assert entry.is_dir()


def test_a_marker_that_others_can_read_is_not_ownership(tmp_path):
    root, settings = _old_root(tmp_path)
    marker = runtime._claim_entry(root, "ticket01-0123456789ab")
    entry = root / "ticket01-0123456789ab"
    entry.mkdir()
    marker.chmod(0o644)

    assert _sweep_far_in_the_future(settings) == []

    assert entry.is_dir()


def test_an_entry_that_is_not_the_kind_the_marker_was_made_for_is_left(tmp_path):
    """A marker for a directory name does not license deleting a symlink or a file."""
    root, settings = _old_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("host data")
    runtime._claim_entry(root, "ticket01-0123456789ab")
    (root / "ticket01-0123456789ab").symlink_to(outside)
    runtime._claim_entry(root, "ticket02-0123456789ab")
    (root / "ticket02-0123456789ab").write_text("a file, not a tree")

    assert _sweep_far_in_the_future(settings) == []

    assert (outside / "keep.txt").read_text() == "host data"
    assert (root / "ticket02-0123456789ab").is_file()


def test_a_marker_placed_inside_the_workspace_a_worker_can_write_counts_for_nothing(
    tmp_path,
):
    root, settings = _old_root(tmp_path)
    entry = root / "ticket01-0123456789ab"
    entry.mkdir()
    # what a Worker can do: write inside its workspace, a perfect forgery
    (entry / "ticket01-0123456789ab.owner").write_text(
        "conductor-cloud-worker-owner v1\nentry=ticket01-0123456789ab\ntoken="
        + "a" * 32
        + "\n"
    )
    (entry / "ticket01-0123456789ab.owner").chmod(0o600)

    assert _sweep_far_in_the_future(settings) == []

    assert entry.is_dir()


def test_a_stale_marker_whose_entry_is_gone_is_removed_and_only_a_valid_one(
    tmp_path, monkeypatch
):
    root, settings = _old_root(tmp_path)
    runtime._claim_entry(root, "ticket01-0123456789ab")  # entry never made / gone
    other = runtime._claim_entry(root, "ticket02-0123456789ab")
    other.chmod(0o644)  # not a valid marker
    (root / "ticket03-0123456789ab.owner").write_text("not a marker at all")
    (root / "unrelated.owner").write_text("somebody else's file")

    removed = _sweep_far_in_the_future(settings)

    assert removed == ["ticket01-0123456789ab.owner"]
    assert sorted(p.name for p in root.iterdir()) == [
        "ticket02-0123456789ab.owner",
        "ticket03-0123456789ab.owner",
        "unrelated.owner",
    ]


def test_a_young_marker_is_left_for_the_launch_that_has_not_made_its_entry_yet(
    tmp_path,
):
    root, settings = _old_root(tmp_path)
    marker = runtime._claim_entry(root, "ticket01-0123456789ab")

    assert runtime.sweep_stale_staging(settings, trees_with_container=frozenset()) == []

    assert marker.exists()


def test_removing_an_entry_releases_its_marker_only_once_it_is_gone(tmp_path):
    root, _ = _old_root(tmp_path)
    runtime._claim_entry(root, "ticket01-0123456789ab")
    tree = _hostile_tree(root).rename(root / "ticket01-0123456789ab")

    assert runtime._remove_staging_tree(tree, container_gone=False) is False
    assert (root / "ticket01-0123456789ab.owner").exists()  # still owned

    assert runtime._remove_staging_tree(tree) is True
    assert list(root.iterdir()) == []


def test_a_directory_the_runtime_never_claimed_loses_nothing_to_its_own_cleanup(
    tmp_path,
):
    """The smoke removes its private directories with the same walk."""
    private = tmp_path / "private"
    private.mkdir()
    (tmp_path / "private.owner").write_text("somebody else's file")

    assert runtime._remove_staging_tree(private) is True

    assert (tmp_path / "private.owner").read_text() == "somebody else's file"


@pytest.mark.asyncio
async def test_staging_claims_what_it_creates_and_a_failed_creation_claims_nothing(
    monkeypatch, tmp_path
):
    async def materializer(*, workspace_id, cwd, user_id):
        return 1

    monkeypatch.setattr(
        "src.server.services.native_runtime.workspace.materialize_workspace",
        materializer,
    )
    root = tmp_path / "stage-root"
    stage = await runtime._stage_workspace(_STAGE_TICKET, _settings(stage_root=root))

    assert (root / f"{stage.name}.owner").is_file()
    assert runtime._owns(root, stage.name)

    real_mkdir = Path.mkdir

    def refuse(self, *args, **kwargs):
        if self.name.startswith("ticket-1"):
            raise OSError("no space")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", refuse)
    before = sorted(p.name for p in root.iterdir())
    with pytest.raises(OSError):
        await runtime._stage_workspace(_STAGE_TICKET, _settings(stage_root=root))
    assert sorted(p.name for p in root.iterdir()) == before


def test_the_env_file_is_claimed_while_it_exists_and_released_with_it(tmp_path):
    root = tmp_path / "root"
    path = runtime._write_env_file(root, {"TOKEN": "x"})

    assert runtime._owns(root, path.name)

    runtime._unlink_env_file(path)

    assert list(root.iterdir()) == []


def test_verification_claims_its_probe_directory_and_releases_it(
    monkeypatch, tmp_path
):
    seen = {}

    class _Runner(_FakeSyncRunner):
        def __call__(self, argv, *, timeout_seconds=30.0):
            if "--mount" in argv:
                mount = argv[argv.index("--mount") + 1]
                source = Path(mount.split("source=", 1)[1].split(",target=", 1)[0])
                seen["claimed"] = runtime._owns(source.parent, source.name)
            return super().__call__(argv, timeout_seconds=timeout_seconds)

    runner = _Runner(
        [
            (0, "29.5.2", ""),
            (0, "sha256:abc", ""),
            (0, "isolation-ok", ""),
            (0, "", ""),
            (0, "codex-cli 1.0.0", ""),
            (0, "", ""),
        ]
    )
    root = tmp_path / "root"
    _patch_settings(monkeypatch, replace(_settings(stage_root=root), runner=runner))
    monkeypatch.setattr(runtime.time, "sleep", lambda _s: None)

    assert runtime.verify_docker_runtime()[0] is True

    assert seen["claimed"] is True
    assert list(root.iterdir()) == []


# -- the publication budget -------------------------------------------------


def _write(path: Path, size: int, byte: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(byte * size)


def test_budget_counts_only_changed_files(tmp_path):
    import hashlib

    _write(tmp_path / "unchanged.bin", 4096)
    _write(tmp_path / "changed.txt", 10)
    previous = {
        "unchanged.bin": {"content_hash": hashlib.sha256(b"x" * 4096).hexdigest()}
    }

    # A large workspace the Worker barely touched is not a problem.
    runtime._enforce_publication_budget(tmp_path, previous)


def test_budget_refuses_too_many_bytes_before_anything_is_read_whole(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(runtime, "_MAX_PUBLISH_BYTES", 1024)
    _write(tmp_path / "a.bin", 600)
    _write(tmp_path / "b.bin", 600)

    with pytest.raises(runtime.DockerWorkerRuntimeError, match="changed too much"):
        runtime._enforce_publication_budget(tmp_path, {})


def test_budget_refuses_too_many_files(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "_MAX_PUBLISH_FILES", 3)
    for index in range(4):
        _write(tmp_path / "d" / f"{index}.txt", 1)

    with pytest.raises(runtime.DockerWorkerRuntimeError, match="changed too much"):
        runtime._enforce_publication_budget(tmp_path, {})


def test_budget_ignores_what_publication_ignores(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "_MAX_PUBLISH_BYTES", 100)
    _write(tmp_path / "node_modules" / "pkg" / "big.js", 500)
    _write(tmp_path / ".git" / "objects" / "pack", 500)
    _write(tmp_path / "src" / "node_modules" / "deep.js", 500)
    (tmp_path / "link").symlink_to(tmp_path / "src")
    _write(tmp_path / "ok.txt", 10)

    runtime._enforce_publication_budget(tmp_path, {})


@pytest.mark.asyncio
async def test_publication_checks_the_budget_before_reading_files(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "src.server.database.autonomy.tickets.get_ticket",
        AsyncMock(return_value=dict(_ACTIVE)),
    )
    mocks = _publication_db(monkeypatch, metadata={}, upserts=[{"file_path": "a"}])
    monkeypatch.setattr(runtime, "_MAX_PUBLISH_BYTES", 10)
    _write(tmp_path / "huge.bin", 100)
    collect = MagicMock()
    monkeypatch.setattr(
        "src.server.services.native_runtime.workspace.collect_changed_files", collect
    )

    with pytest.raises(runtime.DockerWorkerRuntimeError, match="changed too much"):
        await runtime._publish_workspace_if_safe(
            ticket=_publication_ticket(), workspace=tmp_path, baseline={}
        )

    collect.assert_not_called()
    mocks["upsert"].assert_not_awaited()
