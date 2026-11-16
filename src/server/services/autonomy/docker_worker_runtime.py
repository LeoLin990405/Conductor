"""Docker-backed OS-isolated runtime for cloud autonomous Workers.

A cloud Worker normally runs through the shared PTC/native runtime, which has
no per-workspace OS boundary. This module supplies the first real boundary: a
disposable container that runs a vendor-native Worker CLI one-shot against a
private copy of the Ticket's workspace.

Security contract (verified before a launch, and enforced by argv):

* **Disposable container.** A fresh ``docker run --rm`` container per attempt,
  removed again in ``finally`` even when the client was killed or timed out.
  Removal is retried and confirmed; every container carries an expiry label so
  one that still survives (or whose backend was killed) is reaped by the next
  launch's sweep, which also clears staging copies and env files nothing
  removed. That sweep is engine-wide unless ``AUTONOMY_CLOUD_WORKER_SWEEP_LABEL``
  confines it to containers carrying one extra label. A staging tree is
  removed only when no container for it exists on the engine (each carries the
  name of the entry it mounts), never because of today's timeout, and a tree
  whose container is not confirmed removed is left untouched.
* **Workspace bind (+ linked-worktree Git metadata).** The private staging
  directory is the primary bind mount. When that directory is a linked Git
  worktree (``.git`` is a ``gitdir:`` file), the runtime also bind-mounts the
  worktree's gitdir and the common Git directory at the absolute paths the
  ``.git`` file names, so ``git`` inside the container can follow them. The
  Docker socket, the operator's home, and any other host path are never
  mounted. The verification probe binds a fresh empty directory under the
  same staging root, so a root the engine cannot mount fails verification
  instead of every Ticket.
* **No host environment or credentials by default.** The container gets only
  the explicit non-secret base env (``HOME``/``TMPDIR``) plus names the
  operator deliberately lists in ``AUTONOMY_CLOUD_WORKER_ENV`` and literals in
  ``AUTONOMY_CLOUD_WORKER_SET_ENV``. The host environment is never forwarded.
* **No network by default.** ``--network none`` unless the operator
  deliberately sets ``AUTONOMY_CLOUD_WORKER_NETWORK=bridge`` (a worker CLI
  needs public egress to call its provider). ``bridge`` is unfiltered egress:
  on a VM-backed engine (Docker Desktop, colima) it also reaches services on
  the host's loopback through the engine's host gateway. This runtime cannot
  prevent that; see ``docs/CLOUD-WORKERS.md``.
* **Read-only root.** ``--read-only`` root filesystem with a bounded tmpfs for
  ``/tmp``; writable workspace mount is the only persistent surface.
* **Least privilege and limits.** ``--cap-drop ALL``,
  ``--security-opt no-new-privileges``, a non-root ``--user`` (never uid or gid
  0), memory (swap included), CPU, PID and output limits, and a wall-clock
  timeout. The probe reads the kernel's record of the flags that change
  observable state (read-only root, capability bounding set, no_new_privs,
  user, workspace write access, memory and PID cgroup limits); ``--cpus`` is
  not probed.
* **Fail closed.** Engine, image, isolation flags and the worker CLI are
  verified before a launch; any verification or launch failure raises, and the
  caller settles the Ticket as failed. There is no fallback to the shared PTC
  runtime. Exit status 0 is never taken as proof: the probe runs under an
  explicit ``/bin/sh`` entrypoint (an image ENTRYPOINT cannot swallow it) and
  must print its success line, every probe check fails on a missing or empty
  answer, and the CLI version check must name the CLI with a version. The
  probe reads /proc and cgroup files with shell builtins, not the image's
  utilities. All of this checks a trusted image's configuration; an image that
  lies on purpose cannot be told apart from an honest one.

The image is deployment-supplied: it must contain the worker CLI named by
``AUTONOMY_CLOUD_WORKER_PROVIDER``. Credentials are an explicit operator
decision (see ``docs/CLOUD-WORKERS.md``); no credential material is read here.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import posixpath
import re
import secrets
import shutil
import signal
import stat
import subprocess
import time
import traceback
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

logger = logging.getLogger(__name__)

RUNTIME_ID = "docker"

_WORKSPACE_MOUNT = "/workspace"
#: A private tmpfs outside the temp dir: Codex refuses to create its helper
#: aliases (apply_patch and friends) beneath TMPDIR, so a HOME under /tmp left
#: the real CLI warning that it could not build its PATH.
_CONTAINER_HOME = "/home/worker"
_TMPFS_SIZE = "512m"
_VERIFY_TTL_SECONDS = 15.0
_MAX_CAPTURE_BYTES = 8 * 1024 * 1024
_MAX_STDERR_BYTES = 256 * 1024
_DEFAULT_POLL_SECONDS = 1.0


def _docker_create_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """Turn the hardened one-shot ``run`` command into a persistent container."""
    result = list(argv)
    try:
        run_at = result.index("run")
    except ValueError as exc:
        raise DockerWorkerRuntimeError("container command is malformed") from exc
    result[run_at] = "create"
    result = [part for part in result if part != "--rm"]
    return tuple(result)


def _docker_exec_argv(
    settings: DockerWorkerSettings,
    name: str,
    command: Sequence[str],
    *,
    interactive: bool = False,
) -> tuple[str, ...]:
    argv = [settings.docker_bin, "exec"]
    if interactive:
        argv.append("-i")
    argv.extend(("--user", settings.user, "--workdir", _WORKSPACE_MOUNT, name))
    argv.extend(str(part) for part in command)
    return tuple(argv)


def _docker_git_runner(
    settings: DockerWorkerSettings, name: str, workspace: Path
):
    """Run Git inside the mounted staging tree, with no host fallback.

    The runner does not harden anything itself: it accepts only an argv/env
    built by ``git_delivery.hardened_git_invocation`` (the one
    implementation the host checks use too) and refuses any other. Only
    ``-C`` paths are translated to the ``/workspace`` mount; any other host
    path in the argv (a ``--git-dir``, a ``--file``) would name nothing inside
    the container, so it is refused and the check reports unknown instead of
    answering about a different tree.
    """
    from src.server.services.autonomy.git_delivery import (
        GitDeliveryError,
        is_hardened_git_invocation,
    )

    root = workspace.resolve()

    def run(argv, *, cwd: str, timeout: float, env: Mapping[str, str]):
        try:
            host_cwd = Path(cwd).resolve(strict=True)
            if not host_cwd.is_relative_to(root):
                raise ValueError("cwd outside workspace mount")
            mapped_argv: list[str] = []
            index = 0
            while index < len(argv):
                value = str(argv[index])
                if value == "-C":
                    if index + 1 >= len(argv):
                        raise ValueError("missing git -C path")
                    path = Path(str(argv[index + 1])).resolve(strict=True)
                    if not path.is_relative_to(root):
                        raise ValueError("git -C outside workspace mount")
                    mapped_argv.extend(("-C", str(PurePosixPath(_WORKSPACE_MOUNT).joinpath(*path.relative_to(root).parts))))
                    index += 2
                    continue
                if index > 0 and (
                    value.startswith("/")
                    or value.split("=", 1)[0] in {"--git-dir", "--work-tree", "--file"}
                ):
                    raise ValueError("host path cannot be mapped into the container")
                mapped_argv.append(value)
                index += 1
            if not mapped_argv or Path(mapped_argv[0]).name != "git":
                raise ValueError("unexpected executable")
            if not is_hardened_git_invocation(mapped_argv, env):
                raise ValueError("Git invocation was not built by the delivery hardening")
            mapped_argv[0] = "git"

            clean_env: dict[str, str] = {}
            for key, value in env.items():
                if key != "PATH" and not key.startswith("GIT_"):
                    raise ValueError("unexpected environment key")
                clean_env[str(key)] = str(value)
            exec_argv = _docker_exec_argv(
                settings,
                name,
                ("env", "-i", *(f"{key}={clean_env[key]}" for key in sorted(clean_env)), *mapped_argv),
            )
            result = _run_docker_sync(exec_argv, timeout_seconds=timeout)
            stdout = result.stdout.replace(os.fsencode(_WORKSPACE_MOUNT), os.fsencode(root))
            stderr = result.stderr.replace(os.fsencode(_WORKSPACE_MOUNT), os.fsencode(root))
            return subprocess.CompletedProcess(
                argv,
                result.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        except GitDeliveryError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed at the Git boundary
            raise GitDeliveryError(
                "Git workspace status is unknown because it could not be checked safely."
            ) from exc

    return run


async def _in_container_git(
    settings: DockerWorkerSettings,
    name: str,
    workspace: Path,
    operation: Callable[[], Any],
) -> Any:
    """Run one Git read inside the Worker container, with no host fallback."""
    from src.server.services.autonomy.git_delivery import git_command_runner

    runner = _docker_git_runner(settings, name, workspace)

    def run() -> Any:
        with git_command_runner(runner):
            return operation()

    return await asyncio.to_thread(run)


async def _container_git_available(
    settings: DockerWorkerSettings, name: str, workspace: Path
) -> bool:
    """Whether the Worker image carries a Git the probes can run.

    The outcome only selects the ``unsupported_runtime`` reason the report
    gives; a Docker Worker never reports a delivery verdict. The probe argv
    and env come from ``hardened_git_invocation``, the builder every host
    check uses, and ``_docker_git_runner`` refuses anything else.
    """
    from src.server.services.autonomy.git_delivery import (
        GitDeliveryError,
        hardened_git_invocation,
    )

    argv, env = hardened_git_invocation("git", ("--version",))

    def operation() -> int:
        runner = _docker_git_runner(settings, name, workspace)
        result = runner(argv, cwd=str(workspace), timeout=10.0, env=env)
        return result.returncode

    try:
        return await _in_container_git(settings, name, workspace, operation) == 0
    except GitDeliveryError:
        return False
    except Exception:  # noqa: BLE001 - a probe must not fail the Worker's run
        return False

#: Every container carries the epoch second after which it is a leak. A killed
#: backend leaves no process to remove its container, so the deadline has to
#: live on the container itself for a later launch to find it.
_LABEL_DEADLINE = "conductor.cloud-worker.deadline"
#: The staging entry a container mounts, by name. A sweep decides whether a
#: tree is still in use from the containers that exist, not from a clock and
#: today's configuration: a tree whose container is still on the engine, in any
#: state, is not ours to touch.
_LABEL_STAGE = "conductor.cloud-worker.stage"
#: The shapes of the runtime's own entry names. A name of this shape is only a
#: candidate for the sweep: it must also carry an ownership marker (``_owns``).
_STAGE_ENV_RE = re.compile(r"env-[0-9a-f]{32}\.env")
_STAGE_TREE_RES = (
    re.compile(r"[A-Za-z0-9_.-]{1,8}-[0-9a-f]{12}"),
    re.compile(r"probe-[A-Za-z0-9_]{8}"),
)
_CONTAINER_NAME_RE = re.compile(r"conductor-cloud-worker-[a-z0-9][a-z0-9-]{0,80}")
#: Slack beyond the Worker's wall-clock limit before something counts as leaked:
#: materialization and publication run outside that limit.
_REAP_GRACE_SECONDS = 900.0
_SWEEP_INTERVAL_SECONDS = 60.0
#: Lifetime assumed for the short verification containers.
_PROBE_LIFETIME_SECONDS = 120.0
_RM_ATTEMPTS = 3
#: Bounds on what one publication may read into backend memory. The container's
#: memory limit does not cover the bind mount, so a Worker can write far more.
_MAX_PUBLISH_FILES = 5_000
_MAX_PUBLISH_BYTES = 128 * 1024 * 1024

#: The runtime ids this build can launch. A provider CLI must exist inside the
#: image. Adding a provider here means adding its one-shot argv contract too.
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")


def _codex_argv(workdir: str, model: str | None) -> list[str]:
    argv = [
        "codex",
        "exec",
        "--ignore-user-config",
        "--config",
        "project_doc_max_bytes=0",
        "--json",
        "--skip-git-repo-check",
        # The container is the outer boundary; Codex's own Linux sandbox
        # cannot nest inside a read-only rootfs reliably, exactly as the local
        # daemon passes outer_sandboxed=True.
        "--sandbox",
        "danger-full-access",
    ]
    if model:
        argv.extend(("--model", model))
    argv.extend(("-C", workdir, "-"))
    return argv


_PROVIDER_ARGV: dict[str, Callable[[str, str | None], list[str]]] = {
    "codex": _codex_argv,
}

_IMAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}")
_RUNTIME_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_MEMORY_RE = re.compile(r"[0-9]{1,5}(?:\.[0-9]{1,3})?[bkmgBKMG]?")
_USER_RE = re.compile(r"[0-9]{1,8}:[0-9]{1,8}")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_URL_UNSAFE_IMAGE_RE = re.compile(r"[\s;`$(){}<>|&'\"\\]")
#: ``key=value`` of a Docker label; both halves are confined to characters that
#: need no quoting in a ``--filter`` or a Go template.
_SWEEP_LABEL_RE = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})=([A-Za-z0-9._-]{1,128})"
)



class DockerWorkerRuntimeError(RuntimeError):
    """The isolated runtime could not run the Worker; the message is safe.

    The message, ``repr`` and traceback of an exception never hold what the
    Worker wrote. When the failure has output worth showing, ``operator_message``
    is set: the finished Ticket error, the fixed words followed by the screened
    output, already checked against the Worker's forwarded values (see
    ``_worker_failure``). It is only a string. Nothing on an exception that
    leaves the runtime (attribute, closure, default argument, note) refers to
    the forwarded values or to the raw output.
    """

    operator_message: str | None = None


class DockerWorkerRuntimeUnavailable(DockerWorkerRuntimeError):
    """The runtime failed its verification and must not launch anything."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _env(env: Mapping[str, str], name: str, default: str = "") -> str:
    return str(env.get(name, default)).strip()


@dataclass(frozen=True)
class DockerWorkerSettings:
    """Operator-supplied runtime configuration, validated at resolve time."""

    docker_bin: str
    image: str
    provider: str
    network: str
    memory: str
    cpus: str
    pids_limit: int
    timeout_seconds: float
    env_names: tuple[str, ...]
    set_env: tuple[tuple[str, str], ...]
    mounts: tuple[tuple[str, str, bool], ...]
    stage_root: Path
    user: str
    #: ``key=value`` of a Docker label that narrows the expiry sweep to the
    #: containers carrying it. None (the default) sweeps every expired Worker
    #: container on the engine, which is right for a backend that owns its
    #: engine and wrong for a harness sharing one with other Workers.
    sweep_label: str | None = None
    #: Injected command runner for tests; None means the real Docker client.
    runner: Callable[..., "DockerCommandResult"] | None = field(
        default=None, compare=False
    )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "DockerWorkerSettings":
        raw = os.environ if env is None else env
        docker_bin = _env(raw, "AUTONOMY_CLOUD_WORKER_DOCKER_BIN", "docker")
        image = _env(
            raw, "AUTONOMY_CLOUD_WORKER_IMAGE", "conductor-cloud-worker:latest"
        )
        provider = _env(raw, "AUTONOMY_CLOUD_WORKER_PROVIDER", "codex").lower()
        network = _env(raw, "AUTONOMY_CLOUD_WORKER_NETWORK", "none").lower()
        memory = _env(raw, "AUTONOMY_CLOUD_WORKER_MEMORY", "2g")
        cpus = _env(raw, "AUTONOMY_CLOUD_WORKER_CPUS", "1.5")
        pids = _env(raw, "AUTONOMY_CLOUD_WORKER_PIDS_LIMIT", "512")
        timeout = _env(raw, "AUTONOMY_CLOUD_WORKER_TIMEOUT", "3600")
        stage_root = _env(
            raw,
            "AUTONOMY_CLOUD_WORKER_ROOT",
            str(Path.home() / ".cache" / "conductor" / "cloud-workers"),
        )
        user = _env(
            raw,
            "AUTONOMY_CLOUD_WORKER_USER",
            f"{os.getuid()}:{os.getgid()}",
        )
        sweep_label = _env(raw, "AUTONOMY_CLOUD_WORKER_SWEEP_LABEL") or None

        if (
            not docker_bin
            or "\x00" in docker_bin
            or any(ch.isspace() for ch in docker_bin)
        ):
            raise DockerWorkerRuntimeUnavailable("invalid Docker CLI path")
        if not _IMAGE_RE.fullmatch(image) or _URL_UNSAFE_IMAGE_RE.search(image):
            raise DockerWorkerRuntimeUnavailable("invalid worker image reference")
        if provider not in _PROVIDER_ARGV:
            raise DockerWorkerRuntimeUnavailable(
                f"unsupported isolated Worker provider {provider!r}; "
                f"supported: {sorted(_PROVIDER_ARGV)}"
            )
        if network not in {"none", "bridge"}:
            raise DockerWorkerRuntimeUnavailable(
                "AUTONOMY_CLOUD_WORKER_NETWORK must be 'none' or 'bridge'"
            )
        if not _MEMORY_RE.fullmatch(memory):
            raise DockerWorkerRuntimeUnavailable("invalid worker memory limit")
        try:
            cpus_value = float(cpus)
        except ValueError as exc:
            raise DockerWorkerRuntimeUnavailable("invalid worker CPU limit") from exc
        if not 0 < cpus_value <= 8:
            raise DockerWorkerRuntimeUnavailable(
                "worker CPU limit must be > 0 and <= 8"
            )
        try:
            pids_limit = int(pids)
        except ValueError as exc:
            raise DockerWorkerRuntimeUnavailable("invalid worker PID limit") from exc
        if not 16 <= pids_limit <= 65_536:
            raise DockerWorkerRuntimeUnavailable("worker PID limit out of range")
        try:
            timeout_seconds = float(timeout)
        except ValueError as exc:
            raise DockerWorkerRuntimeUnavailable("invalid worker timeout") from exc
        if not 10 <= timeout_seconds <= 86_400:
            raise DockerWorkerRuntimeUnavailable("worker timeout out of range")
        if not _USER_RE.fullmatch(user):
            raise DockerWorkerRuntimeUnavailable("invalid worker user id pair")
        if sweep_label is not None and not _SWEEP_LABEL_RE.fullmatch(sweep_label):
            raise DockerWorkerRuntimeUnavailable(
                "AUTONOMY_CLOUD_WORKER_SWEEP_LABEL must be a label key=value"
            )

        if any(ch in stage_root for ch in ("\x00", "\n", "\r")):
            raise DockerWorkerRuntimeUnavailable(
                "worker staging root contains a control character"
            )
        stage_root_path = Path(stage_root).expanduser()
        if not stage_root_path.is_absolute():
            raise DockerWorkerRuntimeUnavailable("worker staging root must be absolute")

        env_names = _parse_env_names(_env(raw, "AUTONOMY_CLOUD_WORKER_ENV"))
        set_env = _parse_set_env(_env(raw, "AUTONOMY_CLOUD_WORKER_SET_ENV"))
        mounts = _parse_mounts(_env(raw, "AUTONOMY_CLOUD_WORKER_MOUNTS"))

        return cls(
            docker_bin=docker_bin,
            image=image,
            provider=provider,
            network=network,
            memory=memory,
            cpus=cpus,
            pids_limit=pids_limit,
            timeout_seconds=timeout_seconds,
            env_names=env_names,
            set_env=set_env,
            mounts=mounts,
            stage_root=stage_root_path,
            user=user,
            sweep_label=sweep_label,
        )

    def cache_key(self) -> tuple[Any, ...]:
        return (
            self.docker_bin,
            self.image,
            self.provider,
            self.network,
            self.memory,
            self.cpus,
            self.pids_limit,
            self.user,
            self.mounts,
            self.env_names,
            tuple(name for name, _value in self.set_env),
            # Verification binds a directory under the staging root, so a
            # verdict for one root says nothing about another.
            str(self.stage_root),
        )


_MANAGED_ENV_NAMES = frozenset({"HOME", "TMPDIR", "XDG_CONFIG_HOME", "PATH"})


def _parse_env_names(raw: str) -> tuple[str, ...]:
    names: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if not name:
            continue
        if not _ENV_NAME_RE.fullmatch(name):
            raise DockerWorkerRuntimeUnavailable(
                f"invalid environment name in AUTONOMY_CLOUD_WORKER_ENV: {name!r}"
            )
        if name.startswith("CONDUCTOR_"):
            raise DockerWorkerRuntimeUnavailable(
                "server credentials cannot be forwarded into a cloud Worker"
            )
        if name in _MANAGED_ENV_NAMES:
            raise DockerWorkerRuntimeUnavailable(
                f"{name} is managed by the isolated runtime"
            )
        if name not in names:
            names.append(name)
    if len(names) > 32:
        raise DockerWorkerRuntimeUnavailable("too many forwarded environment names")
    return tuple(names)


def _parse_set_env(raw: str) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for part in raw.split(","):
        entry = part.strip()
        if not entry:
            continue
        name, _, value = entry.partition("=")
        name = name.strip()
        if not _ENV_NAME_RE.fullmatch(name):
            raise DockerWorkerRuntimeUnavailable(
                f"invalid literal environment name: {name!r}"
            )
        if name.startswith("CONDUCTOR_"):
            raise DockerWorkerRuntimeUnavailable(
                "server credentials cannot be forwarded into a cloud Worker"
            )
        if name in _MANAGED_ENV_NAMES:
            raise DockerWorkerRuntimeUnavailable(
                f"{name} is managed by the isolated runtime"
            )
        if len(pairs) >= 32:
            raise DockerWorkerRuntimeUnavailable("too many literal environment values")
        pairs.append((name, value.strip()))
    return tuple(pairs)


def _parse_mounts(raw: str) -> tuple[tuple[str, str, bool], ...]:
    """Parse operator mounts. The operator is trusted: this checks shape, not content.

    It refuses reserved container targets and any path that *names*
    ``docker.sock``. It does not look inside a mounted directory, so a parent of
    the socket (``/var/run``, a home directory, ``/``) is accepted and exposes
    whatever it contains. Mount only narrow, read-only credential directories.
    """
    mounts: list[tuple[str, str, bool]] = []
    for part in raw.split(","):
        entry = part.strip()
        if not entry:
            continue
        pieces = entry.split(":")
        if len(pieces) < 2 or len(pieces) > 3:
            raise DockerWorkerRuntimeUnavailable(
                "worker mounts must be host:container[:ro]"
            )
        host, container = pieces[0].strip(), pieces[1].strip()
        read_only = len(pieces) == 3 and pieces[2].strip() == "ro"
        if len(pieces) == 3 and pieces[2].strip() not in {"ro", "rw"}:
            raise DockerWorkerRuntimeUnavailable("worker mount mode must be ro or rw")
        if not host:
            raise DockerWorkerRuntimeUnavailable("worker mount source must be absolute")
        host_path = Path(host).expanduser()
        if not host_path.is_absolute():
            raise DockerWorkerRuntimeUnavailable("worker mount source must be absolute")
        host = str(host_path)
        if not container.startswith("/"):
            raise DockerWorkerRuntimeUnavailable(
                "worker mount target must be an absolute subpath"
            )
        container_path = "/" + posixpath.normpath(container).lstrip("/")
        if container_path == "/":
            raise DockerWorkerRuntimeUnavailable(
                "worker mount target must be an absolute subpath"
            )
        for protected in (_WORKSPACE_MOUNT, "/proc", "/sys", "/dev", "/etc", "/run"):
            if container_path == protected or container_path.startswith(
                protected + "/"
            ):
                raise DockerWorkerRuntimeUnavailable(
                    f"worker mount target is reserved: {container!r}"
                )
        if container_path == "/var" or container_path.startswith("/var/run"):
            raise DockerWorkerRuntimeUnavailable(
                f"worker mount target is reserved: {container!r}"
            )
        if "docker.sock" in host or "docker.sock" in container_path:
            raise DockerWorkerRuntimeUnavailable(
                "the Docker socket cannot be mounted into a cloud Worker"
            )
        if len(mounts) >= 8:
            raise DockerWorkerRuntimeUnavailable("too many worker mounts")
        mounts.append((host, container_path, read_only))
    return tuple(mounts)


# ---------------------------------------------------------------------------
# Docker command execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DockerCommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _docker_client_env() -> dict[str, str]:
    """The Docker *client's* environment: no host secrets, explicit daemon."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    # An operator-specified daemon endpoint is configuration, not a secret.
    for key in ("DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _run_docker_sync(
    argv: Sequence[str], *, timeout_seconds: float
) -> subprocess.CompletedProcess[bytes]:
    """Run one synchronous Docker client command with the scrubbed client env."""
    return subprocess.run(
        tuple(argv),
        cwd=None,
        env=_docker_client_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        check=False,
    )


#: What a cut appends. It is text the runtime itself writes, next to the
#: Worker's, so the final guard must see it (see ``_leaks``).
_ELLIPSIS = "…"
#: How much of a failed Worker's output the Ticket error shows, before the
#: ellipsis; with the fixed words in front of it the whole message stays under
#: the 300 characters cloud_runtime allowed, so it is never cut a second time.
_FAILURE_DETAIL_LIMIT = 260
#: What an error says of a container that survived rm -f (the name is ours).
_STUCK_NOTE = "; container {name} could not be removed and is left for the expiry sweep"
#: Shown instead of the output when a forwarded value would be in it.
_WITHHELD = "output withheld: it would have shown a value given to the Worker"


def _bounded(text: str, *, limit: int = 4_000) -> str:
    return text if len(text) <= limit else text[:limit] + _ELLIPSIS


#: The shortest piece of a forwarded value that is still redacted when it sits
#: at the very start or end of the output (see ``_redact_values``).
_MIN_FRAGMENT = 4


def _normalize(text: str) -> str:
    """One line: control characters become spaces and runs of whitespace one."""
    clean = "".join(
        ch if ch.isprintable() and ch not in "\r\n\t" else " " for ch in str(text)
    )
    return " ".join(clean.split())


def _sanitize(
    text: str, *, limit: int = 400, redact: Mapping[str, str] | None = None
) -> str:
    """One-line, bounded operator-facing detail; never carries argv values.

    ``redact`` is a Worker's forwarded environment. Redaction is the last
    transformation that can change what the text says: it runs on the raw text
    and again on the normalized text (normalizing is what turns a value the
    Worker split with a tab or a newline back into the value), and the cut to
    ``limit`` comes after both, so it cannot leave half a value behind.
    """
    raw = str(text)
    if redact:
        raw = _redact_values(raw, redact)
    clean = _normalize(raw)
    if redact:
        clean = _redact_values(clean, redact, normalized=True)
    return _bounded(clean, limit=limit)


def _redact_values(
    text: str, values: Mapping[str, str], *, normalized: bool = False
) -> str:
    """Replace forwarded environment values so a CLI diagnostic cannot leak one.

    Longest value first, so a value that contains another is not left as the
    remainder of the shorter one. With ``normalized`` the text is already
    ``_normalize``d, so each value is compared in its normalized form, and a
    piece of a value that sits at the very start or end of the text is
    replaced too: the output is captured as its last bytes, so it can begin
    inside a value (or inside a multi-byte character of one), and a
    capture that ended early can stop inside one. The rule is exact: a piece
    of at least ``_MIN_FRAGMENT`` characters at either end of the text goes,
    shorter pieces and pieces in the middle of the text stay. Redaction is
    exact-match; a Worker that re-encodes a value (base64, escapes) is not
    caught, and it holds the credential and writes its own report regardless.
    """
    redacted = str(text)
    candidates = (
        _normalize(value) if normalized else str(value) for value in values.values()
    )
    for value in sorted({v for v in candidates if v.strip()}, key=len, reverse=True):
        redacted = redacted.replace(value, "***")
    if normalized:
        for value in sorted(
            {_normalize(v) for v in values.values()}, key=len, reverse=True
        ):
            redacted = _redact_fragments(redacted, value)
    return redacted


def _leaks(text: str, values: Mapping[str, str], *, safe_prefix: int = 0) -> bool:
    """Whether any forwarded value, whole, is in ``text`` beyond its first
    ``safe_prefix`` characters (the runtime's own fixed words).

    This is the check the redaction passes cannot replace: it is made on the
    string that is finally raised, after every transformation, so it does not
    care how the string was built or whether a replacement, the ellipsis or a
    neighbour recombined into a value. Each value is looked for as given, in
    its ``_normalize``d form and with only its whitespace collapsed; a
    whitespace-only value is not a secret and is skipped.
    """
    forms = {
        form
        for value in values.values()
        for form in (str(value), _normalize(value), " ".join(str(value).split()))
        if form.strip()
    }
    for candidate in {text, _normalize(text)}:
        for form in forms:
            start = candidate.find(form)
            while start != -1:
                if start + len(form) > safe_prefix:
                    return True
                start = candidate.find(form, start + 1)
    return False


def _worker_failure(
    returncode: int, output: str, values: Mapping[str, str], stuck: Sequence[str]
) -> DockerWorkerRuntimeError:
    """The error for a Worker that exited non-zero.

    Its message is the exit code, and a container that could not be removed
    (``stuck``), in fixed words: nothing the Worker wrote, so every form built
    from the exception (``str``, ``repr``, a traceback header, a log line, a
    class-name prefix, a slice) is free of Worker output and cannot assemble a
    forwarded value out of it. The output is placed, redacted, one line and cut,
    after the fixed words the Ticket error starts with (the prefix
    ``cloud_runtime`` uses, so the string is final), and that whole string is
    checked against the forwarded values at this one place: if one is in it
    beyond the fixed words, the output is replaced by a fixed sentence that
    holds no part of it. Only the finished string is kept on the exception;
    the values and the raw output are not, in a closure or anywhere else.
    """
    from src.server.services.autonomy import cloud_runtime

    detail = _sanitize(
        output or "no output", limit=_FAILURE_DETAIL_LIMIT, redact=values
    )
    message = f"isolated cloud Worker exited {returncode}" + "".join(
        _STUCK_NOTE.format(name=name) for name in stuck
    )
    fixed = cloud_runtime.launch_failure_prefix(RUNTIME_ID) + message + ": "
    text = fixed + detail
    if _leaks(text, values, safe_prefix=len(fixed)):
        text = fixed + _WITHHELD
    failure = DockerWorkerRuntimeError(message)
    failure.operator_message = text
    return failure


def _redact_fragments(text: str, value: str) -> str:
    """Replace a cut piece of ``value`` at the start or the end of ``text``."""
    if len(value) <= _MIN_FRAGMENT:
        return text
    # U+FFFD is what decoding leaves of a character the capture cut in two.
    lead = len(text) - len(text.lstrip("\ufffd"))
    for size in range(len(value) - 1, _MIN_FRAGMENT - 1, -1):
        if text.startswith(value[-size:], lead):
            text = text[:lead] + "***" + text[lead + size :]
            break
    for size in range(len(value) - 1, _MIN_FRAGMENT - 1, -1):
        if text.endswith(value[:size]):
            text = text[:-size] + "***"
            break
    return text


def _read_capped_sync(stream, limit: int) -> bytes:
    """Drain a pipe, retaining only its first ``limit`` bytes."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(65_536)
        if not chunk:
            break
        if total < limit:
            chunks.append(chunk[: limit - total])
            total += len(chunks[-1])
    return b"".join(chunks)


def _run_command_sync(
    argv: Sequence[str], *, timeout_seconds: float = 30.0
) -> DockerCommandResult:
    import threading

    try:
        process = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_docker_client_env(),
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise DockerWorkerRuntimeUnavailable("Docker CLI is not installed") from exc
    except OSError as exc:
        raise DockerWorkerRuntimeUnavailable(
            f"Docker CLI could not be executed ({type(exc).__name__})"
        ) from exc
    held: dict[str, bytes] = {}

    def _drain(name: str, stream) -> None:
        try:
            held[name] = _read_capped_sync(stream, _MAX_STDERR_BYTES)
        except Exception:  # noqa: BLE001 - a closed pipe is not a failure here
            held[name] = b""

    readers = [
        threading.Thread(target=_drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=_drain, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _kill_pid(process.pid)
        process.wait()
        for reader in readers:
            reader.join(timeout=5.0)
        raise DockerWorkerRuntimeUnavailable("Docker command timed out") from exc
    for reader in readers:
        reader.join(timeout=5.0)
    return DockerCommandResult(
        argv=tuple(argv),
        returncode=returncode,
        stdout=held.pop("stdout", b"").decode("utf-8", "replace"),
        stderr=held.pop("stderr", b"").decode("utf-8", "replace"),
    )


def _kill_pid(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def _read_tail(stream: asyncio.StreamReader | None, limit: int) -> bytes:
    """Drain a pipe completely, retaining only its last ``limit`` bytes.

    Draining must continue after the cap or the child blocks on a full pipe;
    a transcript-shaped CLI reports its result at the end, so the tail is the
    part worth keeping.
    """
    if stream is None:
        return b""
    tail = bytearray()
    while True:
        chunk = await stream.read(65_536)
        if not chunk:
            break
        tail.extend(chunk)
        if len(tail) > limit:
            del tail[: len(tail) - limit]
    return bytes(tail)


async def _feed_stdin(
    process: asyncio.subprocess.Process, stdin_text: str | None
) -> None:
    if process.stdin is None:
        return
    try:
        if stdin_text:
            process.stdin.write(stdin_text.encode("utf-8"))
            await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        try:
            process.stdin.close()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass


async def _run_command_async(
    argv: Sequence[str],
    *,
    stdin_text: str | None = None,
    timeout_seconds: float = 60.0,
) -> DockerCommandResult:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE
        if stdin_text is not None
        else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_docker_client_env(),
        start_new_session=True,
        close_fds=True,
    )
    readers = [
        asyncio.create_task(_feed_stdin(process, stdin_text)),
        asyncio.create_task(_read_tail(process.stdout, _MAX_CAPTURE_BYTES)),
        asyncio.create_task(_read_tail(process.stderr, _MAX_STDERR_BYTES)),
    ]
    try:
        _feed, stdout_bytes, stderr_bytes = await asyncio.wait_for(
            asyncio.gather(*readers), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        for reader in readers:
            reader.cancel()
        _kill_process_tree(process)
        await process.wait()
        raise
    except BaseException:
        for reader in readers:
            reader.cancel()
        _kill_process_tree(process)
        await process.wait()
        raise
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        # Pipes can close while the process itself never exits; a bounded
        # re-kill keeps the caller from hanging past its own deadline.
        _kill_process_tree(process)
        await process.wait()
    return DockerCommandResult(
        argv=tuple(argv),
        returncode=process.returncode or 0,
        stdout=stdout_bytes.decode("utf-8", "replace"),
        stderr=stderr_bytes.decode("utf-8", "replace"),
    )


async def _start_worker_container(
    settings: DockerWorkerSettings, argv: Sequence[str], name: str
) -> None:
    try:
        create = await _run_command_async(
            _docker_create_argv(argv), timeout_seconds=30.0
        )
    except asyncio.TimeoutError as exc:
        raise DockerWorkerRuntimeError("isolated cloud Worker container could not be created") from exc
    if create.returncode != 0 or not create.stdout.strip():
        raise DockerWorkerRuntimeError("isolated cloud Worker container could not be created")
    try:
        started = await _run_command_async(
            (settings.docker_bin, "start", name), timeout_seconds=30.0
        )
    except asyncio.TimeoutError as exc:
        raise DockerWorkerRuntimeError("isolated cloud Worker container could not be started") from exc
    if started.returncode != 0:
        raise DockerWorkerRuntimeError("isolated cloud Worker container could not be started")
    deadline = time.monotonic() + 10.0
    while True:
        try:
            ready = await _run_command_async(
                _docker_exec_argv(settings, name, ("/bin/sh", "-c", "true")),
                timeout_seconds=5.0,
            )
        except asyncio.TimeoutError as exc:
            raise DockerWorkerRuntimeError("isolated cloud Worker container did not become ready") from exc
        if ready.returncode == 0:
            return
        if time.monotonic() >= deadline:
            raise DockerWorkerRuntimeError("isolated cloud Worker container did not become ready")
        await asyncio.sleep(0.1)


async def _run_worker_exec(
    settings: DockerWorkerSettings,
    name: str,
    model: str | None,
    prompt: str,
    timeout_seconds: float,
) -> DockerCommandResult:
    return await _run_command_async(
        _docker_exec_argv(
            settings,
            name,
            build_worker_argv(settings, model=model),
            interactive=True,
        ),
        stdin_text=prompt,
        timeout_seconds=timeout_seconds,
    )


def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _removal_confirmed(result: DockerCommandResult) -> bool:
    """``rm -f`` succeeded, or the container was already gone (``--rm`` won)."""
    if result.returncode == 0:
        return True
    return "no such container" in f"{result.stderr} {result.stdout}".lower()


async def _rm_container(settings: DockerWorkerSettings, name: str) -> bool:
    """Remove a Worker container; True only once it is confirmed gone.

    A killed ``docker run`` client does not stop its container, and the
    container may hold credentials and network access, so one failed ``rm -f``
    must not be the end of it: retry, and say so loudly when it cannot be
    done. The container's expiry label lets the next launch's sweep finish the
    job.
    """
    for attempt in range(_RM_ATTEMPTS):
        try:
            result = await _run_command_async(
                (settings.docker_bin, "rm", "-f", name), timeout_seconds=10.0
            )
        except Exception:  # noqa: BLE001 - a hung client is retried like a refusal
            result = None
        if result is not None and _removal_confirmed(result):
            return True
        if attempt + 1 < _RM_ATTEMPTS:
            await asyncio.sleep(1.0 + attempt)
    logger.error(
        "[cloud-worker] container %s could not be removed after %d attempts; "
        "it is left for the expiry sweep",
        name,
        _RM_ATTEMPTS,
    )
    return False


def _ensure_stage_root(settings: DockerWorkerSettings) -> Path:
    """The staging root, created private; anything looser is refused.

    Shared by staging and verification so both judge the same directory.
    """
    root = settings.stage_root
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        loose = root.stat().st_mode & 0o077
    except OSError as exc:
        raise DockerWorkerRuntimeUnavailable(
            f"cloud Worker staging root cannot be used ({type(exc).__name__})"
        ) from exc
    if loose:
        raise DockerWorkerRuntimeUnavailable(
            "cloud Worker staging root is group/world accessible; "
            "restrict it to the backend user (0700)"
        )
    return root


#: Flags that open a real directory and nothing else: a symlink is refused
#: instead of followed, and a FIFO a Worker planted cannot block the open.
_DIR_OPEN_FLAGS = (
    os.O_RDONLY
    | os.O_DIRECTORY
    | os.O_NOFOLLOW
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def _open_directory(name: str, dir_fd: int | None) -> int | None:
    """Open ``name`` as a directory without following a symlink, or None.

    A directory the Worker made unreadable is chmodded through its parent's
    descriptor with ``follow_symlinks=False``: should the Worker swap a link in
    between the failed open and the chmod, the chmod acts on the link, or is
    refused where the platform cannot do that, and never on its target.
    """
    for attempt in range(2):
        try:
            return os.open(name, _DIR_OPEN_FLAGS, dir_fd=dir_fd)
        except PermissionError:
            if attempt:
                return None
            try:
                os.chmod(name, 0o700, dir_fd=dir_fd, follow_symlinks=False)
            except (OSError, NotImplementedError, ValueError):
                return None
        except OSError:  # a file, a link, gone, or otherwise not a directory
            return None
    return None


def _identity(fd: int) -> tuple[int, int]:
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def _purge_contents(top: int) -> bool:
    """Make everything below the directory open as ``top`` writable and delete it.

    Takes ownership of ``top``. Depth-first and iterative, relative to a single
    open descriptor: it goes down by opening a child of the current directory,
    and back up by opening ``..`` and checking that the result is the very
    directory it left (device and inode), so no depth of tree can exhaust the
    stack or the process's descriptor limit, and no entry is ever reached
    through a path. Links, files and special files are unlinked, never followed.
    Returns False if the way back up led somewhere else; nothing more is
    touched then.
    """
    cur = top
    try:
        try:
            os.fchmod(cur, 0o700)
        except OSError:
            pass
        frames: list[tuple[tuple[int, int], list[str], str]] = [
            (_identity(cur), _entries(cur), "")
        ]
        while True:
            names = frames[-1][1]
            if names:
                name = names.pop()
                child = _open_directory(name, cur)
                if child is None:
                    try:  # a file or link; a directory it cannot open stays (EISDIR/EPERM)
                        os.unlink(name, dir_fd=cur)
                    except OSError:
                        pass
                    continue
                try:
                    try:
                        os.fchmod(child, 0o700)
                    except OSError:
                        pass
                    frames.append((_identity(child), _entries(child), name))
                except OSError:
                    os.close(child)
                    continue
                os.close(cur)
                cur = child
                continue
            if len(frames) == 1:
                return True
            _identity_of_child, _names, name = frames.pop()
            parent = os.open("..", _DIR_OPEN_FLAGS, dir_fd=cur)
            os.close(cur)
            cur = parent
            if _identity(cur) != frames[-1][0]:
                return False
            try:
                os.rmdir(name, dir_fd=cur)
            except OSError:
                pass
    except OSError:
        return False
    finally:
        os.close(cur)


def _entries(fd: int) -> list[str]:
    try:
        return os.listdir(fd)
    except OSError:
        return []


def _purge_tree(root: Path) -> None:
    """Make every directory under ``root`` accessible again and delete the tree.

    The Worker runs as the host user, so it can ``chmod 000`` a directory it
    created, and ``rmtree`` then leaves the tree (and its bytes) on the host
    without an error; it can also nest directories deeper than a recursive
    ``rmtree`` or a walk holding one descriptor per level can follow. Only call
    this once the container is confirmed gone: the walk opens nothing it could
    follow out of the tree, so a link swapped in underneath it is unlinked, not
    followed, but it still assumes nothing keeps rewriting the tree.
    """
    top = _open_directory(str(root), None)
    if top is not None and not _purge_contents(top):
        logger.warning(
            "[cloud-worker] staging copy %s: the walk stopped because its way back "
            "up did not lead to the directory it left",
            root.name,
        )
    try:
        os.rmdir(root)
    except OSError:
        pass


def _remove_staging_tree(path: Path, *, container_gone: bool = True) -> bool:
    """Delete a tree a Worker may have filled; True when nothing is left.

    Unless its container is confirmed gone the tree is not touched at all: a
    Worker that may still be running can rewrite it under the walk. It stays,
    with a warning, for the sweep to remove once the container is. A failure is
    logged and left for the sweep rather than raised: by now the Ticket's
    outcome is decided and cleanup must not change it.
    """
    if not container_gone:
        logger.warning(
            "[cloud-worker] staging copy %s left in place: its container could not "
            "be confirmed removed; the sweep removes it once the container is gone",
            path.name,
        )
        return False
    try:
        if path.is_symlink():
            path.unlink()
        else:
            _purge_tree(path)
    except OSError:
        pass
    if os.path.lexists(path):
        logger.warning(
            "[cloud-worker] staging copy %s could not be removed; the sweep will retry",
            path.name,
        )
        return False
    _release_entry(path)
    return True


# ---------------------------------------------------------------------------
# Isolation argv
# ---------------------------------------------------------------------------


def _mount_field(key: str, value: str) -> str:
    """One ``--mount`` field. The option string is CSV, so a comma or quote in a
    path (the operator's staging root) must be quoted, not interpolated."""
    field_text = f"{key}={value}"
    if any(ch in field_text for ch in (",", '"')):
        return '"' + field_text.replace('"', '""') + '"'
    return field_text


def _read_git_metadata_text(path: Path) -> str:
    """Read a small regular Git metadata file without following symlinks.

    ``O_NONBLOCK`` keeps a FIFO a prior Worker planted from blocking the open
    (and the server event loop) before the regular-file check can refuse it.
    """
    fd = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise OSError("unexpected Git metadata file")
        return os.read(fd, 4097).decode("utf-8", errors="strict").strip()
    finally:
        os.close(fd)


def linked_worktree_git_mounts(
    workspace: str | Path,
) -> tuple[tuple[str, str], ...]:
    """Bind mounts that make a linked worktree's Git metadata reachable.

    A linked worktree stores only a ``gitdir:`` pointer in ``.git``. Inside the
    container the workspace is remounted at ``/workspace``, so that pointer
    (absolute, or relative from ``--relative-paths`` /
    ``worktree.useRelativePaths``) and the common directory it names would miss
    unless the host Git directories are bind-mounted where in-container Git will
    open them. Returns ``(source, target)`` pairs — ``target`` is that
    container path — or an empty tuple when the workspace is not a linked
    worktree.
    """
    root = Path(workspace)
    pointer = root / ".git"
    try:
        pointer_info = pointer.lstat()
    except OSError:
        return ()
    if stat.S_ISLNK(pointer_info.st_mode):
        raise DockerWorkerRuntimeError(
            "linked worktree .git entry must not be a symlink"
        )
    if stat.S_ISDIR(pointer_info.st_mode):
        # Ordinary checkout: ``.git`` already lives inside the workspace mount.
        return ()
    if not stat.S_ISREG(pointer_info.st_mode):
        raise DockerWorkerRuntimeError(
            "linked worktree .git entry is missing or invalid"
        )
    try:
        workspace_root = root.resolve(strict=True)
        pointer_text = _read_git_metadata_text(pointer)
        if not pointer_text.startswith("gitdir: "):
            raise ValueError("invalid gitdir pointer")
        raw_gitdir = Path(pointer_text[8:])
        # Source is the real host directory. Target is the path Git opens
        # inside the container: the absolute pointer as written, or the same
        # relative pointer resolved against ``/workspace``.
        if raw_gitdir.is_absolute():
            gitdir_target = os.path.normpath(str(raw_gitdir))
            gitdir = Path(gitdir_target).resolve(strict=True)
        else:
            gitdir = (workspace_root / raw_gitdir).resolve(strict=True)
            gitdir_target = os.path.normpath(
                str(PurePosixPath(_WORKSPACE_MOUNT).joinpath(*raw_gitdir.parts))
            )
        if not gitdir.is_dir():
            raise ValueError("gitdir is not a directory")
        common_record = _read_git_metadata_text(gitdir / "commondir")
        raw_common = Path(common_record)
        # Follow ``commondir`` from the mounted gitdir so a relative ``../..``
        # still lands on the common-dir bind.
        if raw_common.is_absolute():
            common_target = os.path.normpath(str(raw_common))
            common_dir = Path(common_target).resolve(strict=True)
        else:
            common_dir = (gitdir / raw_common).resolve(strict=True)
            common_target = os.path.normpath(
                str(PurePosixPath(gitdir_target).joinpath(*raw_common.parts))
            )
        if not common_dir.is_dir():
            raise ValueError("common dir is not a directory")
        backlink = _read_git_metadata_text(gitdir / "gitdir")
        raw_backlink = Path(backlink)
        backlink_path = (
            raw_backlink.resolve()
            if raw_backlink.is_absolute()
            else (gitdir / raw_backlink).resolve()
        )
        if (
            not gitdir.is_relative_to(common_dir / "worktrees")
            or backlink_path != pointer.resolve()
        ):
            raise ValueError("unregistered linked worktree")
    except (OSError, UnicodeError, ValueError) as exc:
        raise DockerWorkerRuntimeError(
            "linked worktree Git metadata could not be mounted safely"
        ) from exc
    mounts: list[tuple[str, str]] = [(str(common_dir), common_target)]
    if gitdir != common_dir:
        mounts.append((str(gitdir), gitdir_target))
    return tuple(mounts)


def _stage_label(workspace: str | None) -> list[str]:
    """The label tying a container to its staging entry, when it has one the
    sweep would recognise (an entry it could never sweep needs no protection)."""
    if workspace is None:
        return []
    name = Path(workspace).name
    if not any(pattern.fullmatch(name) for pattern in _STAGE_TREE_RES):
        return []
    return ["--label", f"{_LABEL_STAGE}={name}"]


def _isolation_flags(
    settings: DockerWorkerSettings,
    *,
    name: str,
    workspace: str | None,
    read_only_workspace: bool,
    lifetime_seconds: float = _PROBE_LIFETIME_SECONDS,
) -> list[str]:
    deadline = int(time.time() + lifetime_seconds + _REAP_GRACE_SECONDS)
    flags = [
        "run",
        "--rm",
        "--init",
        "--name",
        name,
        "--label",
        f"{_LABEL_DEADLINE}={deadline}",
        *_stage_label(workspace),
        "--network",
        settings.network,
        "--read-only",
        "--tmpfs",
        f"/tmp:rw,nosuid,nodev,noexec,size={_TMPFS_SIZE}",
        # The Worker CLI expects a writable HOME; on the read-only rootfs it is
        # its own tmpfs, separate from the workspace mount and from /tmp.
        "--tmpfs",
        f"{_CONTAINER_HOME}:rw,nosuid,nodev,size=256m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        settings.user,
        "--pids-limit",
        str(settings.pids_limit),
        "--memory",
        settings.memory,
        # Without this the engine allows as much swap again as memory.
        "--memory-swap",
        settings.memory,
        "--cpus",
        settings.cpus,
    ]
    if workspace is not None:
        mount = ",".join(
            (
                "type=bind",
                _mount_field("source", workspace),
                f"target={_WORKSPACE_MOUNT}",
            )
        )
        if read_only_workspace:
            mount += ",readonly"
        flags.extend(("--mount", mount))
        # Linked worktrees keep Git metadata outside the working tree; bind
        # those paths where in-container ``git`` will open them (the absolute
        # ``.git`` pointer, or the ``/workspace``-relative path for relative
        # pointers).
        for source, target in linked_worktree_git_mounts(workspace):
            git_mount = ",".join(
                (
                    "type=bind",
                    _mount_field("source", source),
                    _mount_field("target", target),
                )
            )
            if read_only_workspace:
                git_mount += ",readonly"
            flags.extend(("--mount", git_mount))
    return flags


def build_container_argv(
    settings: DockerWorkerSettings,
    *,
    name: str,
    workspace: str | None,
    child_argv: Sequence[str],
    env_vars: Mapping[str, str] | None = None,
    env_file: str | None = None,
    read_only_workspace: bool = False,
    workdir: str = _WORKSPACE_MOUNT,
    include_stdin: bool = True,
    lifetime_seconds: float = _PROBE_LIFETIME_SECONDS,
    entrypoint: str | None = None,
) -> tuple[str, ...]:
    """Build the hardened ``docker run`` argv for one Worker attempt.

    Only the non-secret base environment is carried on the command line.
    Explicit operator environment (which may include credentials) travels
    through a private ``--env-file`` so its values never enter the host process
    argument list. ``lifetime_seconds`` is how long the caller expects the
    container to live; the expiry label derived from it is what a later sweep
    reaps by. The image's own ENTRYPOINT is kept (a Worker launch relies on
    it) unless ``entrypoint`` replaces it, which the isolation probe does so
    that nothing in the image can stand in for the probe's shell.
    """
    forwarded = {str(k): str(v) for k, v in (env_vars or {}).items()}
    if forwarded and not env_file:
        raise ValueError("forwarded container environment requires an env file")
    argv = [
        settings.docker_bin,
        *_isolation_flags(
            settings,
            name=name,
            workspace=workspace,
            read_only_workspace=read_only_workspace,
            lifetime_seconds=lifetime_seconds,
        ),
    ]
    if include_stdin:
        argv.append("-i")
    if env_file:
        argv.extend(("--env-file", env_file))
    argv.extend(("-w", workdir))
    base_env = {
        "HOME": _CONTAINER_HOME,
        "TMPDIR": "/tmp",
        "XDG_CONFIG_HOME": f"{_CONTAINER_HOME}/.config",
    }
    for key, value in base_env.items():
        argv.extend(("-e", f"{key}={value}"))
    for host, container, read_only in settings.mounts:
        suffix = ":ro" if read_only else ""
        argv.extend(("-v", f"{host}:{container}{suffix}"))
    if entrypoint is not None:
        argv.extend(("--entrypoint", entrypoint))
    argv.append(settings.image)
    argv.extend(str(part) for part in child_argv)
    return tuple(argv)


def _env_file_text(env_vars: Mapping[str, str]) -> str:
    """Render an env file, refusing values the format cannot carry safely."""
    lines = []
    for name, value in env_vars.items():
        text = str(value)
        if any(ch in text for ch in ("\n", "\r", "\x00")):
            raise DockerWorkerRuntimeUnavailable(
                f"environment value for {name} cannot contain newlines"
            )
        lines.append(f"{name}={text}")
    return "\n".join(lines) + ("\n" if lines else "")


#: Ownership. A name of the right shape and an age do not show that this runtime
#: made an entry: a staging root that is shared holds other things. Each entry
#: it creates gets a marker beside it, ``<entry>.owner``, in the private staging
#: root itself, so outside the bind-mounted workspace a Worker can write. The
#: sweep considers an entry only if its marker is there, is a regular file of
#: this user that nobody else can read, and names the entry.
_OWNER_SUFFIX = ".owner"
_OWNER_HEADER = "conductor-cloud-worker-owner v1"
_OWNER_TOKEN_RE = re.compile(r"token=[0-9a-f]{32}")
_OWNER_MAX_BYTES = 256
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _current_uid() -> int:
    return os.getuid()


def _claim_entry(root: Path, name: str) -> Path:
    """Record that this runtime is about to create ``name`` in ``root``.

    Made before the entry, ``O_EXCL`` and never through a link, 0600, with a
    random token: a crash between the two leaves a marker with no entry, which
    the sweep removes, never an entry with no marker.
    """
    marker = root / (name + _OWNER_SUFFIX)
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(
                f"{_OWNER_HEADER}\nentry={name}\ntoken={secrets.token_hex(16)}\n"
            )
    except BaseException:
        try:
            marker.unlink()
        except OSError:
            pass
        raise
    return marker


def _owns(root: Path, name: str) -> bool:
    """Whether ``root`` holds a valid marker, made by this user, for ``name``."""
    marker = root / (name + _OWNER_SUFFIX)
    try:
        fd = os.open(marker, os.O_RDONLY | os.O_NONBLOCK | _NOFOLLOW)
    except OSError:  # missing, or a link (O_NOFOLLOW)
        return False
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != _current_uid()
            or info.st_mode & 0o077
            or info.st_size > _OWNER_MAX_BYTES
        ):
            return False
        text = os.read(fd, _OWNER_MAX_BYTES + 1).decode("utf-8", "replace")
    except OSError:
        return False
    finally:
        os.close(fd)
    lines = text.splitlines()
    return (
        len(lines) == 3
        and lines[0] == _OWNER_HEADER
        and lines[1] == f"entry={name}"
        and _OWNER_TOKEN_RE.fullmatch(lines[2]) is not None
    )


def _release_entry(path: Path) -> None:
    """Remove the marker of an entry that is gone; a file that is not a valid
    marker for it is left alone."""
    if os.path.lexists(path) or not _owns(path.parent, path.name):
        return
    try:
        (path.parent / (path.name + _OWNER_SUFFIX)).unlink()
    except OSError:
        pass


def _write_env_file(directory: Path, env_vars: Mapping[str, str]) -> Path:
    """Write a 0600 env file outside the workspace mount, never in argv."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"env-{uuid.uuid4().hex}.env"
    _claim_entry(directory, path.name)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_env_file_text(env_vars))
    except BaseException:
        _unlink_env_file(path)
        raise
    return path


def _unlink_env_file(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass
    _release_entry(path)


def validate_worker_model(model: str | None) -> str | None:
    """A Ticket model may only be forwarded as one validated CLI argument."""
    value = str(model or "").strip()
    if not value:
        return None
    if not _MODEL_RE.fullmatch(value) or ".." in value:
        raise DockerWorkerRuntimeUnavailable(f"invalid isolated Worker model {value!r}")
    return value


def build_worker_argv(
    settings: DockerWorkerSettings, model: str | None = None
) -> list[str]:
    """The one-shot worker CLI command run inside the container."""
    builder = _PROVIDER_ARGV.get(settings.provider)
    if builder is None:  # pragma: no cover - from_env rejects unknown providers
        raise DockerWorkerRuntimeUnavailable("unsupported isolated Worker provider")
    return builder(_WORKSPACE_MOUNT, validate_worker_model(model))


def resolve_container_env(settings: DockerWorkerSettings) -> dict[str, str]:
    """Only explicitly allowlisted host values plus literal configuration."""
    env: dict[str, str] = {}
    for name in settings.env_names:
        value = os.environ.get(name)
        if value:
            env[name] = value
    for name, value in settings.set_env:
        env[name] = value
    return env


_MEMORY_UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
#: The last line a passing probe prints on stdout; a verifier that did not see
#: it has no evidence that the probe ran.
_PROBE_OK = "isolation-ok"


def memory_limit_bytes(memory: str) -> int:
    """The byte count Docker derives from a ``--memory`` value (binary units)."""
    match = re.fullmatch(r"([0-9.]+)([bkmgBKMG]?)", memory)
    if match is None:
        raise ValueError(f"invalid memory limit {memory!r}")
    return int(float(match.group(1)) * _MEMORY_UNITS[match.group(2).lower()])


def _cgroup_limit_check(label: str, files: Sequence[str], at_most: int) -> str:
    """Shell that fails unless the cgroup records a numeric limit <= ``at_most``.

    The first readable file wins (cgroup v2, then v1) and is left in ``$found``,
    its value in ``$limit``. No readable file, or the v2 word ``max``, means the
    engine did not apply the limit it was asked for (a missing controller
    silently drops ``--memory`` and ``--pids-limit``), so the limit is not
    proven and the probe fails. The value is read with the shell's own ``read``.
    """
    candidates = " ".join(f'"{path}"' for path in files)
    return (
        "limit=missing; found=; "
        f"for f in {candidates}; do "
        'if [ -r "$f" ]; then found=$f; limit=unreadable; '
        'read -r limit < "$f" || [ -n "$limit" ] || limit=unreadable; break; fi; '
        "done; "
        'case "$limit" in ""|*[!0-9]*) '
        f'fail "{label} limit is not enforced (cgroup reports $limit)";; esac; '
        f'test "$limit" -le {at_most} || fail "{label} limit is looser than configured"'
    )


#: Shell functions the probe defines. ``mount_options MOUNTPOINT FILE`` prints
#: the option list of every mount at MOUNTPOINT, one per line (fields 5 and 6
#: of a mountinfo record; neither can contain a space), and fails if FILE
#: cannot be read. ``status_field NAME FILE`` prints the rest of the first
#: ``NAME:`` line of a /proc status file, and fails if there is none. Both use
#: only ``read``, and the ``if`` keeps a loop's own status at 0.
_PROBE_FUNCTIONS = (
    "mount_options() { "
    "while read -r _id _parent _dev _root mountpoint options _rest; do "
    'if [ "$mountpoint" = "$1" ]; then printf \'%s\\n\' "$options"; fi; '
    'done < "$2"; }',
    "status_field() { "
    "while read -r key value; do "
    'if [ "$key" = "$1:" ]; then printf \'%s\\n\' "$value"; return 0; fi; '
    'done < "$2"; return 1; }',
)


def _isolation_probe_script(
    proc: str = "/proc",
    workspace: str | None = None,
    *,
    user: str | None = None,
    pids_limit: int | None = None,
    memory_bytes: int | None = None,
    cgroup: str = "/sys/fs/cgroup",
) -> str:
    """Shell that reads the kernel's record of what the isolation flags did.

    Writing to ``/`` proves nothing: a non-root uid is denied there with or
    without ``--read-only``, and a non-root process has no effective
    capabilities with or without ``--cap-drop ALL``, so an unhardened
    container would still verify. The mount table, the capability *bounding*
    set and ``no_new_privs`` are the state the flags actually change, and the
    cgroup files are where an engine records the limits it really applied.
    ``workspace`` (a bind mount) must be writable by the configured user and
    empty. Every path is a parameter only so a unit test can feed fixture
    files to the real script.

    Every check fails closed. A value a command could not produce, a record
    that is missing and a line that does not parse are all failures: no check
    is a comparison that an empty answer satisfies, so each answer is captured
    with its own exit status first.

    The script runs under the image's own ``/bin/sh``, but asks the image for
    nothing else: it reads ``/proc`` and cgroup files with ``read`` and tests
    them with shell builtins, instead of ``id``, ``grep``, ``cat`` or ``ls``
    that the image supplies and could make say anything. The one external
    command left is ``rm``, which deletes the probe file (the shell cannot).
    That narrows what an unusual image can fake; it is no defence against one
    that lies on purpose, whose ``sh`` and ``rm`` are its own.
    """
    status = f"{proc}/self/status"
    mountinfo = f"{proc}/self/mountinfo"
    steps = [
        "set -eu",
        'fail() { echo "isolation probe: $1" >&2; exit 1; }',
        *_PROBE_FUNCTIONS,
        # Uid and Gid list real, effective, saved and filesystem ids; none of
        # them may be 0, and the effective one is the process's identity.
        f'uids=$(status_field Uid "{status}") || fail "cannot read the uid"',
        "set -- $uids",
        'test "$#" -eq 4 || fail "cannot read the uid"',
        'for id in "$@"; do case "$id" in ""|*[!0-9]*) fail "cannot read the uid";; '
        'esac; test "$id" -ne 0 || fail "runs as root"; done',
        "uid=$2",
    ]
    if user is not None:
        if not _USER_RE.fullmatch(user):
            raise ValueError(f"invalid worker user {user!r}")
        steps += [
            f'gids=$(status_field Gid "{status}") || fail "cannot read the gid"',
            "set -- $gids",
            'test "$#" -eq 4 || fail "cannot read the gid"',
            'for id in "$@"; do case "$id" in ""|*[!0-9]*) fail "cannot read the gid";; '
            "esac; done",
            "gid=$2",
            f'test "$uid:$gid" = "{user}" || fail "not running as the configured user"',
            'for id in $gids; do test "$id" -ne 0 || fail "runs in the root group"; done',
        ]
    steps += [
        'test -w /tmp || fail "/tmp is not writable"',
        # Every mount of / must be read-only and there must be one: a record
        # that is missing, empty or unparsable proves nothing.
        f'roots=$(mount_options / "{mountinfo}") || fail "root filesystem is '
        'writable or its mount record is unreadable"',
        'test -n "$roots" || fail "root filesystem is writable (no mount record for /)"',
        'for opts in $roots; do case ",$opts," in *,ro,*) ;; '
        '*) fail "root filesystem is writable";; esac; done',
        f'capbnd=$(status_field CapBnd "{status}") || fail "capabilities were not dropped"',
        'case "$capbnd" in ""|*[!0]*) fail "capabilities were not dropped";; esac',
        f'nnp=$(status_field NoNewPrivs "{status}") || fail "no-new-privileges is not set"',
        'test "$nnp" = 1 || fail "no-new-privileges is not set"',
    ]
    if pids_limit is not None:
        steps.append(
            _cgroup_limit_check(
                "PID",
                (f"{cgroup}/pids.max", f"{cgroup}/pids/pids.max"),
                int(pids_limit),
            )
        )
    if memory_bytes is not None:
        steps.append(
            _cgroup_limit_check(
                "memory",
                (f"{cgroup}/memory.max", f"{cgroup}/memory/memory.limit_in_bytes"),
                int(memory_bytes),
            )
        )
        # Swap, from the same cgroup the memory limit came from ($found, with
        # its limit in $limit). cgroup v1 records memory+swap in a sibling
        # file, which must exist, be numeric and not exceed the memory limit
        # (it cannot be below it): --memory-swap equal to --memory. cgroup v2
        # records swap alone, which must be 0. Where cgroup v2 publishes no
        # swap file at all the engine has no swap accounting to ask; that is
        # accepted, but a file that is there and cannot be read is not.
        steps.append(
            'case "$found" in */memory.limit_in_bytes) '
            'msw="${found%/*}/memory.memsw.limit_in_bytes"; '
            '[ -e "$msw" ] || fail "swap is not disabled (no memory+swap limit is recorded)"; '
            "memsw=unreadable; "
            'read -r memsw < "$msw" || [ -n "$memsw" ] || memsw=unreadable; '
            'case "$memsw" in ""|*[!0-9]*) '
            'fail "swap is not disabled (memory+swap limit is $memsw)";; esac; '
            'test "$memsw" -le "$limit" || fail "swap is not disabled '
            '(memory+swap limit exceeds the memory limit)";; '
            "*) "
            'swapfile="${found%/*}/memory.swap.max"; '
            'if [ -e "$swapfile" ]; then swap=unreadable; '
            'read -r swap < "$swapfile" || [ -n "$swap" ] || swap=unreadable; '
            'test "$swap" = 0 || fail "swap is not disabled"; fi;; esac'
        )
    if workspace is not None:
        probe_file = f"{workspace}/.isolation-probe"
        steps += [
            # The image can contain the directory (``-w`` makes it), so only the
            # mount table shows that a bind mount is really there: the path
            # must be a mount *point* (field 5), not a substring of any record.
            f'mounts=$(mount_options "{workspace}" "{mountinfo}") '
            '|| fail "workspace is not a bind mount"',
            'test -n "$mounts" || fail "workspace is not a bind mount"',
            # A subshell: dash exits at once on a failed redirection of the
            # ``:`` builtin, which would skip the message.
            f'( : > "{probe_file}" ) 2>/dev/null'
            ' || fail "workspace is not writable by the worker user"',
            f'rm -f "{probe_file}" || fail "the probe file cannot be removed"',
            # A glob over a directory that cannot be read matches nothing, which
            # looks empty: so it must be readable and searchable first.
            f'[ -r "{workspace}" ] && [ -x "{workspace}" ] '
            '|| fail "workspace cannot be listed"',
            f'for entry in "{workspace}"/* "{workspace}"/.[!.]* "{workspace}"/..?*; do '
            'if [ -e "$entry" ] || [ -L "$entry" ]; then '
            'fail "unexpected workspace content"; fi; done',
        ]
    steps.append(f"echo {_PROBE_OK}")
    return "; ".join(steps)


def build_isolation_probe(
    settings: DockerWorkerSettings, *, workspace: str | None = _WORKSPACE_MOUNT
) -> str:
    """The probe command for ``settings``: its user and limits are what it checks."""
    return _isolation_probe_script(
        workspace=workspace,
        user=settings.user,
        pids_limit=settings.pids_limit,
        memory_bytes=memory_limit_bytes(settings.memory),
    )


#: The probe is run by this shell, named explicitly, so an image whose
#: ENTRYPOINT ignores its arguments cannot answer for it.
_PROBE_SHELL = "/bin/sh"


def isolation_probe_argv(
    settings: DockerWorkerSettings,
    *,
    name: str,
    workspace: str | None,
    read_only_workspace: bool = False,
) -> tuple[str, ...]:
    """The ``docker run`` that executes the isolation probe, as verification
    runs it; the live smoke builds its probe from the same function."""
    return build_container_argv(
        settings,
        name=name,
        workspace=workspace,
        child_argv=("-c", build_isolation_probe(settings)),
        env_vars={},
        read_only_workspace=read_only_workspace,
        entrypoint=_PROBE_SHELL,
    )


def _identifies_cli(provider: str, output: str) -> bool:
    """A line that names the provider as a word and carries a version number.

    ``codex-cli 0.166.0`` does; ``isolation-ok``, an entrypoint echoing
    ``codex --version`` back, ``mycodexish 1.2.3`` and a bare ``codex`` do not.
    A lying image can still print such a line; this checks configuration, not
    honesty.
    """
    name = rf"(?<![A-Za-z0-9_]){re.escape(provider)}(?![A-Za-z0-9_])"
    return any(
        re.search(name, line, re.IGNORECASE) and re.search(r"\d+(\.\d+)+", line)
        for line in output.splitlines()
    )


def _probe_reported_success(result: DockerCommandResult) -> bool:
    """Exit status 0 is not proof: only the probe's own last line is."""
    lines = result.stdout.strip().splitlines()
    return result.returncode == 0 and bool(lines) and lines[-1].strip() == _PROBE_OK


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

_verification_cache: dict[tuple[Any, ...], tuple[float, bool, str]] = {}


def clear_verification_cache() -> None:
    """Forget cached verification results (operator change or tests)."""
    _verification_cache.clear()


def verify_docker_runtime() -> tuple[bool, str]:
    """Prove Docker, the image, the isolation flags and the worker CLI work.

    Runs bounded Docker commands (no Worker launch) and caches the verdict for
    ``_VERIFY_TTL_SECONDS``. Any failure is a closed result, never an exception.
    """
    try:
        settings = DockerWorkerSettings.from_env()
    except DockerWorkerRuntimeUnavailable as exc:
        return False, _sanitize(exc)
    except Exception as exc:  # noqa: BLE001 - verification never raises
        logger.exception("[cloud-worker] docker runtime settings failed")
        return False, f"runtime configuration failed ({type(exc).__name__})"
    key = settings.cache_key()
    cached = _verification_cache.get(key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _VERIFY_TTL_SECONDS:
        return cached[1], cached[2]
    try:
        available, detail = _verify_uncached(settings)
    except DockerWorkerRuntimeUnavailable as exc:
        available, detail = False, _sanitize(exc)
    except Exception as exc:  # noqa: BLE001 - verification never raises
        logger.exception("[cloud-worker] docker runtime verification failed")
        available, detail = False, f"verification failed ({type(exc).__name__})"
    _verification_cache[key] = (now, available, detail)
    return available, detail


def _verify_uncached(settings: DockerWorkerSettings) -> tuple[bool, str]:
    docker_bin = _resolve_docker_bin(settings)
    run = settings.runner or _run_command_sync

    engine = run(
        (docker_bin, "version", "--format", "{{.Server.Version}}"),
        timeout_seconds=15.0,
    )
    if engine.returncode != 0:
        return False, "Docker engine unavailable: " + _sanitize(
            engine.stderr or engine.stdout or "no output"
        )
    engine_version = _sanitize(engine.stdout, limit=80)
    if not engine_version:
        return False, "Docker engine reported no server version"

    image = run(
        (docker_bin, "image", "inspect", settings.image, "--format", "{{.Id}}"),
        timeout_seconds=15.0,
    )
    if image.returncode != 0:
        return False, (
            f"worker image {settings.image!r} is not available: "
            + _sanitize(image.stderr or image.stdout or "no output")
        )
    image_id = _sanitize(image.stdout, limit=80)

    # The probe binds a fresh directory from the staging root, exactly as a
    # launch will: a root the engine cannot mount (outside colima's shared
    # paths, or a path its mount syntax cannot carry) would otherwise verify
    # and then fail every Ticket.
    stage_root = _ensure_stage_root(settings)
    probe_dir = stage_root / f"probe-{secrets.token_hex(4)}"
    _claim_entry(stage_root, probe_dir.name)
    try:
        probe_dir.mkdir(mode=0o700)
    except BaseException:
        _release_entry(probe_dir)
        raise
    probe_name = f"conductor-cloud-worker-probe-{uuid.uuid4().hex[:10]}"
    probe_argv = isolation_probe_argv(
        settings, name=probe_name, workspace=str(probe_dir)
    )
    probe_gone = False
    try:
        probe = run(probe_argv, timeout_seconds=30.0)
    finally:
        # A probe that timed out may still be running and writing to its
        # directory, so the directory goes only once the container is confirmed
        # gone; otherwise it stays (the sweep removes it after the container).
        probe_gone = _rm_container_sync(run, docker_bin, probe_name)
        _remove_staging_tree(probe_dir, container_gone=probe_gone)
    if not probe_gone:
        return False, (
            f"verification container {probe_name} could not be removed; its "
            "staging directory was left in place"
        )
    if probe.returncode != 0:
        return False, (
            "container isolation probe failed: "
            + _sanitize(probe.stderr or probe.stdout or "no output")
        )
    if not _probe_reported_success(probe):
        return False, (
            "container isolation probe failed: exit 0 without the probe's "
            "success line, so there is no evidence it ran"
        )

    cli_name = f"conductor-cloud-worker-probe-{uuid.uuid4().hex[:10]}"
    cli_argv = build_container_argv(
        settings,
        name=cli_name,
        workspace=None,
        child_argv=tuple(build_worker_argv(settings)[:1]) + ("--version",),
        env_vars={},
    )
    cli_gone = False
    try:
        cli = run(cli_argv, timeout_seconds=30.0)
    finally:
        cli_gone = _rm_container_sync(run, docker_bin, cli_name)
    if not cli_gone:
        return False, f"verification container {cli_name} could not be removed"
    if cli.returncode != 0:
        return False, (
            f"worker CLI {settings.provider!r} is not available in image "
            f"{settings.image!r}: " + _sanitize(cli.stderr or cli.stdout or "no output")
        )
    # An entrypoint that swallows its arguments exits 0 and prints nothing, or
    # echoes them; the output has to be the CLI naming itself with a version.
    if not cli.stdout.strip():
        return False, (
            f"worker CLI {settings.provider!r} is not available in image "
            f"{settings.image!r}: --version printed nothing"
        )
    if not _identifies_cli(settings.provider, cli.stdout):
        return False, (
            f"worker CLI {settings.provider!r} is not available in image "
            f"{settings.image!r}: the --version output does not identify it"
        )

    network = settings.network
    if network == "bridge":
        # The reason string is what an operator reads when checking the switch.
        network += " (unfiltered egress; on a VM-backed engine it reaches host loopback services)"
    detail = (
        f"docker {engine_version}; image {image_id[:12]}; "
        f"network {network}; isolation probe ok; "
        f"worker CLI {settings.provider} present"
    )
    return True, detail


def _rm_container_sync(
    run: Callable[..., DockerCommandResult], docker_bin: str, name: str
) -> bool:
    """Remove a verification container; True only once it is confirmed gone.

    The same retry-and-confirm rule as a Worker container: a container that
    outlived a timeout may still be running, and nothing that depends on it
    being gone (its bind-mounted directory) may proceed on a guess.
    """
    for attempt in range(_RM_ATTEMPTS):
        try:
            result = run((docker_bin, "rm", "-f", name), timeout_seconds=10.0)
        except Exception:  # noqa: BLE001 - a hung client is retried like a refusal
            result = None
        if result is not None and _removal_confirmed(result):
            return True
        if attempt + 1 < _RM_ATTEMPTS:
            time.sleep(1.0 + attempt)
    logger.error(
        "[cloud-worker] verification container %s could not be removed after %d "
        "attempts; it is left for the expiry sweep",
        name,
        _RM_ATTEMPTS,
    )
    return False


def _resolve_docker_bin(settings: DockerWorkerSettings) -> str:
    if "/" in settings.docker_bin:
        path = Path(settings.docker_bin)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise DockerWorkerRuntimeUnavailable(
                "configured Docker CLI is not an executable file"
            )
        return str(path)
    found = shutil.which(settings.docker_bin)
    if not found:
        raise DockerWorkerRuntimeUnavailable("Docker CLI is not installed")
    return found


# ---------------------------------------------------------------------------
# Worker report parsing (the completion contract)
# ---------------------------------------------------------------------------


def parse_worker_report(
    text: str, *, speculative: bool = False, raw_text: str | None = None
) -> dict[str, Any]:
    """Settle the Worker's final message as a completion report.

    This runtime's private staging copy has no shared Git delivery check and
    runs no tests, so neither check can confirm or refute the claim; a missing
    or invalid report waits for a person, never reads as success. ``raw_text``
    is the whole output, kept as the body when the report is missing.
    """
    from src.server.services.autonomy.completion import settle_native_completion

    outcome = settle_native_completion(
        text, speculative=speculative, raw_text=raw_text
    )
    return {
        "state": outcome["state"],
        "summary": outcome["summary"],
        "question": outcome["question"] or "",
        "body": outcome["body"],
        "attention_reason": outcome["attention_reason"],
        "completion": outcome["completion"],
        "reconciled": outcome["reconciled"],
    }


def _parse_provider_stdout(provider: str, stdout: str) -> tuple[str, str]:
    """The Worker's output text and its final message (the completion report).

    Codex can send progress messages before its final one: the output text
    joins them all, and only the last agent message is the report. A Codex
    turn that failed without any message is a Worker failure, not a missing
    report. Other CLIs pass stdout through as both.
    """
    if provider == "codex":
        from src.llms.native_cli.codex import (
            codex_final_agent_message,
            codex_turn_completed,
            parse_codex_events,
        )

        text, _session_id, _usage, events = parse_codex_events(stdout)
        final = codex_final_agent_message(events)
        if not final and not codex_turn_completed(events):
            raise DockerWorkerRuntimeError(
                "isolated cloud Worker produced no final message"
            )
        return text, final
    text = stdout.strip()
    return text, text


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------


#: How long a cancelled launch waits for the thread that is filling its staging
#: directory. The thread cannot be stopped, only waited for.
_MATERIALIZE_GRACE_SECONDS = 120.0


def _discard_stage(stage: Path) -> None:
    shutil.rmtree(stage, ignore_errors=True)
    _release_entry(stage)


def _leave_unfinished(stage: Path) -> None:
    logger.warning(
        "[cloud-worker] staging copy %s left in place: its materializer did not "
        "finish; the sweep removes it once no container exists for it",
        stage.name,
    )


async def _materializer_finished(task: "asyncio.Task[Any]") -> bool:
    """Wait, bounded, for the materializer; further cancellations do not end the wait.

    A cancelled launch must not delete the directory while the materializer
    can still write into it (the writer would recreate it afterwards), so a
    second or third cancellation is absorbed here and the wait goes on until
    the grace period is over. Returns whether the task is finished; it does not
    cancel it, and the caller re-raises the cancellation itself.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _MATERIALIZE_GRACE_SECONDS
    while not task.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        try:
            await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            continue
    if not task.cancelled():
        task.exception()  # retrieved: a failed materialization is not reported twice
    return True


#: How long teardown may take in all: every removal attempt and the pauses
#: between them (three attempts of up to 10 s with 1 s and 2 s pauses come to
#: about 33 s). Further cancellations do not shorten it; if it runs out with the
#: container still there, that is said.
_TEARDOWN_BOUND_SECONDS = 60.0


async def _remove_container_protected(
    settings: DockerWorkerSettings, name: str
) -> tuple[bool, bool]:
    """Remove the Worker container, however many times the launch is cancelled.

    The removal (all its attempts and the backoff between them) runs as its own
    task and is waited for through further cancellations, up to
    ``_TEARDOWN_BOUND_SECONDS``. Returns whether the container is confirmed
    gone, and whether a cancellation arrived meanwhile and was absorbed (the
    caller raises it afterwards, so none is lost).
    """
    loop = asyncio.get_running_loop()
    removal = asyncio.ensure_future(_rm_container(settings, name))
    deadline = loop.time() + _TEARDOWN_BOUND_SECONDS
    interrupted = False
    while not removal.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait({removal}, timeout=remaining)
        except asyncio.CancelledError:
            interrupted = True
    if not removal.done():
        removal.cancel()
        removal.add_done_callback(lambda task: task.cancelled() or task.exception())
        logger.error(
            "[cloud-worker] container %s teardown ran past its %.0f s bound; it is "
            "left for the expiry sweep",
            name,
            _TEARDOWN_BOUND_SECONDS,
        )
        return False, interrupted
    try:
        return bool(removal.result()), interrupted
    except BaseException:  # noqa: BLE001 - a removal that raised did not confirm
        return False, interrupted


async def _stage_workspace(
    ticket: dict[str, Any], settings: DockerWorkerSettings
) -> Path:
    from src.server.services.native_runtime.workspace import materialize_workspace

    ticket_id = str(ticket.get("ticket_id") or "")
    user_id = str(ticket.get("user_id") or "")
    workspace_id = str(ticket.get("workspace_id") or "")
    if not ticket_id or not user_id or not workspace_id:
        raise DockerWorkerRuntimeError("ticket is missing its workspace identity")
    root = _ensure_stage_root(settings)
    stage = root / f"{ticket_id[:8]}-{uuid.uuid4().hex[:12]}"
    _claim_entry(root, stage.name)
    try:
        stage.mkdir(mode=0o700)
    except BaseException:
        _release_entry(stage)
        raise
    materialize = asyncio.create_task(
        materialize_workspace(workspace_id=workspace_id, cwd=stage, user_id=user_id)
    )
    try:
        await asyncio.shield(materialize)
    except asyncio.CancelledError:
        # Cancellation does not stop the sync materializer thread: the
        # directory goes only once it is confirmed finished, however many more
        # times the launch is cancelled meanwhile. If it cannot be confirmed,
        # the directory stays for the sweep and this cancellation still
        # propagates.
        if await _materializer_finished(materialize):
            _discard_stage(stage)
        else:
            _leave_unfinished(stage)
        raise
    except BaseException:
        if not materialize.done():
            materialize.cancel()
        if materialize.done():
            _discard_stage(stage)
        else:
            _leave_unfinished(stage)
        raise
    return stage


async def _workspace_metadata(workspace_id: str) -> dict[str, dict[str, Any]]:
    from src.server.database.workspace_file import get_file_metadata_for_sync

    return await get_file_metadata_for_sync(workspace_id)


async def _publish_workspace_if_safe(
    *,
    ticket: dict[str, Any],
    workspace: Path,
    baseline: dict[str, dict[str, Any]],
) -> int:
    """Publish created/modified files into the same, unchanged active run.

    Publication is non-destructive on purpose: the staging copy is a lossy
    snapshot (symlinks, directory rows, unresolved/oversized blobs are not
    materialized), so treating a missing staging path as a deletion would
    destroy manifest rows the Worker never saw. Deletions are therefore never
    published; only upserts are, inside one transaction that locks the
    workspace rows, re-reads the manifest and refuses when it differs from the
    snapshot the Worker started from.
    """
    from src.server.database.autonomy import tickets as ticket_db
    from src.server.database.pool import get_db_connection
    from src.server.database.workspace_file import (
        bulk_upsert_files,
        get_file_metadata_for_sync,
    )
    from src.server.services.native_runtime.workspace import collect_changed_files

    from psycopg.rows import dict_row

    ticket_id = str(ticket.get("ticket_id") or "")
    workspace_id = str(ticket.get("workspace_id") or "")
    thread_id = str(ticket.get("worker_thread_id") or "")
    run_id = str(ticket.get("worker_run_id") or "")
    current_ticket = await ticket_db.get_ticket(ticket_id)
    if (
        not current_ticket
        or str(current_ticket.get("status") or "") != "running"
        or str(current_ticket.get("worker_thread_id") or "") != thread_id
        or str(current_ticket.get("worker_run_id") or "") != run_id
    ):
        raise DockerWorkerRuntimeError(
            "the Ticket left this isolated Worker run; workspace changes were "
            "not published"
        )
    # Hash/scan outside the transaction so the table lock below only covers the
    # conditional re-read and the write, not filesystem work.
    before_transaction = await _workspace_metadata(workspace_id)
    await asyncio.to_thread(
        _enforce_publication_budget, Path(workspace), before_transaction
    )
    upserts, _removed = await asyncio.to_thread(
        collect_changed_files, Path(workspace), before_transaction
    )
    async with get_db_connection() as conn, conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            # The Ticket owns this publication atomically with the write: a
            # stop/switch that wins the row lock first makes this predicate
            # fail; one that arrives later waits for this transaction.
            await cur.execute(
                "SELECT status, worker_thread_id, worker_run_id "
                "FROM autonomy_tickets WHERE ticket_id = %s FOR UPDATE",
                (ticket_id,),
            )
            owner = await cur.fetchone()
            if (
                not owner
                or str(owner.get("status") or "") != "running"
                or str(owner.get("worker_thread_id") or "") != thread_id
                or str(owner.get("worker_run_id") or "") != run_id
            ):
                raise DockerWorkerRuntimeError(
                    "the Ticket left this isolated Worker run; workspace "
                    "changes were not published"
                )
        async with conn.cursor() as cur:
            # Row locks alone cannot stop a concurrent INSERT of a new path,
            # so serialize against every workspace_files writer with a table
            # lock for the duration of this short publication transaction.
            await cur.execute("LOCK TABLE workspace_files IN SHARE ROW EXCLUSIVE MODE")
            await cur.execute(
                "SELECT file_path FROM workspace_files WHERE workspace_id = %s "
                "FOR UPDATE",
                (workspace_id,),
            )
            await cur.fetchall()
        current_metadata = await get_file_metadata_for_sync(workspace_id, conn=conn)
        if current_metadata != baseline:
            raise DockerWorkerRuntimeError(
                "the workspace changed during the isolated Worker run; its "
                "changes were not published"
            )
        if upserts:
            await bulk_upsert_files(workspace_id, upserts, conn=conn)
    return len(upserts)


# ---------------------------------------------------------------------------
# Leftovers: what a crash or a failed removal leaves behind
# ---------------------------------------------------------------------------

_last_sweep: float | None = None


@dataclass(frozen=True)
class _Reaping:
    """What one expiry pass did: containers removed, and expired ones that stayed."""

    reaped: tuple[str, ...]
    survivors: tuple[str, ...]


def reap_expired_containers(
    settings: DockerWorkerSettings, *, now: float | None = None
) -> list[str]:
    """The names ``_reap_expired`` removed; survivors are logged, not returned."""
    return list(_reap_expired(settings, now=now).reaped)


def _reap_expired(
    settings: DockerWorkerSettings, *, now: float | None = None
) -> _Reaping:
    """Force-remove Worker containers whose expiry label has passed.

    Covers what no running process can: a backend killed mid-run leaves its
    container (and any credentials in its environment) running, and a
    ``docker rm`` that kept failing leaves one behind. An expired container
    that survives ``rm -f`` is reported, because its Worker may still be
    writing to a staging tree.

    With ``settings.sweep_label`` set only containers carrying that label are
    candidates. The daemon is asked for them, and the label value in each row
    is checked again here, so a container that is merely expired but not
    labelled as the caller's is never removed.
    """
    moment = time.time() if now is None else now
    docker_bin = _resolve_docker_bin(settings)
    run = settings.runner or _run_command_sync
    scope_key, _, scope_value = (settings.sweep_label or "").partition("=")
    template = '{{.Names}} {{.Label "' + _LABEL_DEADLINE + '"}}'
    filters = ["--filter", f"label={_LABEL_DEADLINE}"]
    if settings.sweep_label:
        template += ' {{.Label "' + scope_key + '"}}'
        filters += ["--filter", f"label={settings.sweep_label}"]
    listing = run(
        (docker_bin, "ps", "-a", *filters, "--format", template),
        timeout_seconds=15.0,
    )
    if listing.returncode != 0:
        raise DockerWorkerRuntimeUnavailable(
            "could not list Worker containers: "
            + _sanitize(listing.stderr or listing.stdout or "no output")
        )
    reaped: list[str] = []
    survivors: list[str] = []
    for line in listing.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name, deadline_text, *scope = parts
        if not _CONTAINER_NAME_RE.fullmatch(name):
            continue
        if settings.sweep_label and scope != [scope_value]:
            continue
        try:
            deadline = float(deadline_text)
        except ValueError:
            continue
        if deadline >= moment:
            continue
        removal = run((docker_bin, "rm", "-f", name), timeout_seconds=30.0)
        if _removal_confirmed(removal):
            reaped.append(name)
            logger.warning("[cloud-worker] removed expired Worker container %s", name)
        else:
            survivors.append(name)
            logger.error(
                "[cloud-worker] expired Worker container %s survived rm -f", name
            )
    return _Reaping(tuple(reaped), tuple(survivors))


def _trees_with_containers(settings: DockerWorkerSettings) -> frozenset[str]:
    """Names of the staging entries a container on the engine was started for.

    A container exists, in any state, until it is removed, and carries the
    name of the entry it mounts. Not narrowed by ``sweep_label``: asking is
    harmless, and a tree is in use whoever started its container.
    """
    docker_bin = _resolve_docker_bin(settings)
    run = settings.runner or _run_command_sync
    listing = run(
        (
            docker_bin,
            "ps",
            "-a",
            "--filter",
            f"label={_LABEL_STAGE}",
            "--format",
            '{{.Label "' + _LABEL_STAGE + '"}}',
        ),
        timeout_seconds=15.0,
    )
    if listing.returncode != 0:
        raise DockerWorkerRuntimeUnavailable(
            "could not list the containers that use staging entries: "
            + _sanitize(listing.stderr or listing.stdout or "no output")
        )
    return frozenset(
        line.strip() for line in listing.stdout.splitlines() if line.strip()
    )


def sweep_stale_staging(
    settings: DockerWorkerSettings,
    *,
    now: float | None = None,
    trees_with_container: Collection[str] | None,
) -> list[str]:
    """Remove staging copies and env files nothing removed (a killed backend).

    Only what this runtime created is ever a candidate: an entry needs its
    ownership marker (see ``_owns``), whatever its name and age, so a staging
    root that holds anything else loses nothing. Entries from a version that
    wrote no markers are therefore left alone; the docs say how to remove them.

    Two further things must hold before a workspace copy goes. It is older than
    twice the reap grace: the container does not exist yet while a launch is
    still materializing the workspace, so only age can tell that launch apart,
    and the window is the materialization, not the Worker's limit, so it does
    not follow the configured timeout (a limit lowered since the run began must
    not make a running Worker's tree look abandoned). And its container is gone:
    ``trees_with_container`` is the names of the entries a container still
    exists for, or None when the engine could not be asked, and a tree in that
    set, or any tree when it is None, stays where it is with a warning, since
    its Worker may still be rewriting it. Env files need no walk and are
    removed on age alone. A marker whose entry is gone is removed once it is
    old enough that no launch can be about to create the entry.
    """
    moment = time.time() if now is None else now
    cutoff = moment - 2 * _REAP_GRACE_SECONDS
    root = settings.stage_root
    try:
        entries = list(os.scandir(root))
    except OSError:
        return []
    removed: list[str] = []
    for entry in entries:
        name = entry.name
        if name.endswith(_OWNER_SUFFIX):
            if _sweep_orphan_marker(root, name, entry, cutoff):
                removed.append(name)
            continue
        is_env = _STAGE_ENV_RE.fullmatch(name) is not None
        if not is_env and not any(
            pattern.fullmatch(name) for pattern in _STAGE_TREE_RES
        ):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if info.st_ctime > cutoff or info.st_uid != _current_uid():
            continue
        if not _owns(root, name):
            continue
        path = Path(entry.path)
        if stat.S_ISDIR(info.st_mode) and not is_env:
            if trees_with_container is None or name in trees_with_container:
                logger.warning(
                    "[cloud-worker] stale staging copy %s left in place: %s",
                    name,
                    "the engine could not be asked which containers exist"
                    if trees_with_container is None
                    else "a container for it still exists",
                )
                continue
            gone = _remove_staging_tree(path)
        elif stat.S_ISREG(info.st_mode) and is_env:
            _unlink_env_file(path)
            gone = not os.path.lexists(path)
        else:  # not the kind of entry its marker was made for
            continue
        if gone:
            removed.append(name)
            logger.warning("[cloud-worker] removed stale staging entry %s", name)
    return removed


def _sweep_orphan_marker(
    root: Path, name: str, entry: "os.DirEntry[str]", cutoff: float
) -> bool:
    """Remove the marker of an entry that no longer exists, if it is ours and old."""
    entry_name = name[: -len(_OWNER_SUFFIX)]
    if _STAGE_ENV_RE.fullmatch(entry_name) is None and not any(
        pattern.fullmatch(entry_name) for pattern in _STAGE_TREE_RES
    ):
        return False
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    if info.st_ctime > cutoff or os.path.lexists(root / entry_name):
        return False
    if not _owns(root, entry_name):
        return False
    try:
        (root / name).unlink()
    except OSError:
        return False
    logger.warning("[cloud-worker] removed stale staging marker %s", name)
    return True


def sweep_leftovers(settings: DockerWorkerSettings) -> None:
    """Best-effort: containers first (their Worker may still be writing), then files.

    Staging trees are removed only for entries with no container left on the
    engine. If the container pass could not finish (a failed listing, or an
    expired container that survived ``rm -f``) or the engine cannot say which
    containers exist, nothing proves a Worker is gone, so no tree is touched.
    """
    in_use: frozenset[str] | None = None
    try:
        if not _reap_expired(settings).survivors:
            in_use = _trees_with_containers(settings)
    except Exception as exc:  # noqa: BLE001 - housekeeping never blocks a launch
        logger.warning("[cloud-worker] container sweep failed (%s)", type(exc).__name__)
    try:
        sweep_stale_staging(settings, trees_with_container=in_use)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[cloud-worker] staging sweep failed (%s)", type(exc).__name__)


async def _sweep_leftovers(settings: DockerWorkerSettings) -> None:
    """Run the sweep at most once a minute per process.

    The clock only rate-limits idempotent cleanup; no decision reads it.
    """
    global _last_sweep
    now = time.monotonic()
    if _last_sweep is not None and now - _last_sweep < _SWEEP_INTERVAL_SECONDS:
        return
    _last_sweep = now
    await asyncio.to_thread(sweep_leftovers, settings)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _enforce_publication_budget(
    root: Path, previous: Mapping[str, Mapping[str, Any]]
) -> None:
    """Refuse a publication that would load too much into backend memory.

    ``collect_changed_files`` reads each file whole and keeps every changed one
    until the upsert. The container's memory limit does not cover its bind
    mount, so a Worker can write far more than that. Files are hashed in
    blocks here, so the check itself holds almost nothing. It skips what
    ``collect_changed_files`` skips (symlinks, oversize files, runtime state).
    """
    from src.server.services.native_runtime.workspace import (
        _MAX_FILE_BYTES,
        _skip_relative,
    )

    files = 0
    total = 0
    pending = [(root, PurePosixPath())]
    while pending:
        directory, rel_dir = pending.pop()
        try:
            with os.scandir(directory) as scan:
                entries = list(scan)
        except OSError:
            continue
        for entry in entries:
            rel = rel_dir / entry.name
            if _skip_relative(rel):
                continue
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append((Path(entry.path), rel))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                size = entry.stat(follow_symlinks=False).st_size
                if size > _MAX_FILE_BYTES:
                    continue
                prior = previous.get(rel.as_posix())
                if prior is not None and prior.get("content_hash") == _sha256_file(
                    Path(entry.path)
                ):
                    continue
            except OSError:
                continue
            files += 1
            total += size
            if files > _MAX_PUBLISH_FILES or total > _MAX_PUBLISH_BYTES:
                raise DockerWorkerRuntimeError(
                    "the isolated Worker changed too much to publish "
                    f"(limit {_MAX_PUBLISH_FILES} files or "
                    f"{_MAX_PUBLISH_BYTES // (1024 * 1024)} MiB); its changes "
                    "were not published"
                )


def _scrub(error: BaseException) -> None:
    """Empty the frames an error passed through, along its whole chain.

    A traceback keeps its frames alive and a frame keeps its locals: the
    Worker's stdout, the forwarded environment. A log record's ``exc_info`` or
    a ``TracebackException(capture_locals=True)`` reaches them through the
    error however clean its message is. Called on every error that leaves the
    runtime, once the frames it passed through have finished; a frame that is
    still running (the caller's, ``launch``'s own) cannot be cleared and holds
    nothing of the Worker's.
    """
    seen: set[int] = set()
    pending: list[BaseException | None] = [error]
    while pending:
        exc = pending.pop()
        if exc is None or id(exc) in seen:
            continue
        seen.add(id(exc))
        traceback.clear_frames(exc.__traceback__)
        pending += [exc.__cause__, exc.__context__]


def _stage_failure(
    kind: str,
    stage: str,
    type_name: str | None,
    own: DockerWorkerRuntimeError | None,
) -> DockerWorkerRuntimeError:
    """The error to raise for a stage that failed, built outside any handler.

    ``own`` is an error this module raised itself, whose message is fixed: it
    is kept, with whatever it chained to dropped. Anything else is replaced by
    a fixed message naming the stage, and only the stage and the exception's
    type name are logged (no traceback, no message): such an exception was
    raised while handling what a Worker produced, and quotes it.
    """
    if own is not None:
        own.__cause__ = None
        own.__context__ = None
        own.__suppress_context__ = True
        return own
    logger.error("[cloud-worker] %s failed (%s)", stage, type_name)
    return DockerWorkerRuntimeError(f"isolated cloud Worker {kind}: {stage}")


_PREPARING = "workspace could not be prepared"
_READING = "result could not be read"
_PUBLISHING = "result could not be published"


async def _in_stage(kind: str, stage: str, operation: Callable[[], Any]) -> Any:
    """Await ``operation()``, turning any ordinary exception into a fixed error.

    ``CancelledError``, ``KeyboardInterrupt`` and ``SystemExit`` are not
    ``Exception``s and pass through untouched. The replacement is raised after
    the ``try`` statement, not inside a handler, so it carries no ``__context__``
    and no ``__cause__``.
    """
    type_name: str | None = None
    own: DockerWorkerRuntimeError | None = None
    try:
        return await operation()
    except DockerWorkerRuntimeError as exc:
        own = exc
    except Exception as exc:  # noqa: BLE001 - quotes what the Worker produced
        type_name = type(exc).__name__
        _scrub(exc)
    raise _stage_failure(kind, stage, type_name, own)


def _in_stage_sync(kind: str, stage: str, operation: Callable[[], Any]) -> Any:
    """``_in_stage`` for a stage that is not a coroutine."""
    type_name: str | None = None
    own: DockerWorkerRuntimeError | None = None
    try:
        return operation()
    except DockerWorkerRuntimeError as exc:
        own = exc
    except Exception as exc:  # noqa: BLE001 - quotes what the Worker produced
        type_name = type(exc).__name__
        _scrub(exc)
    raise _stage_failure(kind, stage, type_name, own)


async def launch(ticket: dict[str, Any], prompt: str) -> dict[str, Any]:
    """Run one ticket in a disposable isolated container and return its report.

    Raises ``DockerWorkerRuntimeError`` on any verification or execution
    failure, and nothing else that is not a ``BaseException`` the caller must
    see (cancellation, interpreter exit): an exception raised anywhere in the
    run, including while handling what the Worker produced or while cleaning
    up, is replaced by a fixed message and never carries Worker-derived text.
    The caller settles the Ticket; nothing here falls back to the shared runtime.
    """
    type_name: str | None = None
    own: DockerWorkerRuntimeError | None = None
    stuck: list[str] = []
    try:
        return await _launch(ticket, prompt, stuck)
    except DockerWorkerRuntimeError as exc:
        own = exc
    except Exception as exc:  # noqa: BLE001 - nothing may escape that quotes the Worker
        type_name = type(exc).__name__
        _scrub(exc)
    except BaseException as exc:
        # Cancellation, KeyboardInterrupt, SystemExit: the same object goes on,
        # but not with the Worker's data in the frames it carries, and saying
        # (in fixed words) if its container could not be removed.
        _scrub(exc)
        for name in stuck:
            exc.add_note(_STUCK_NOTE.format(name=name).lstrip("; "))
        raise
    if own is None:
        logger.error("[cloud-worker] the run failed unexpectedly (%s)", type_name)
        own = DockerWorkerRuntimeError(
            "isolated cloud Worker runtime failed unexpectedly"
        )
    for name in stuck:
        # A container that survived rm -f may still hold the Worker's
        # credentials and network: every failure says so, in fixed words (the
        # name is ours), so the Ticket error is the one place that tells.
        if name not in str(own):
            own.args = (f"{own}{_STUCK_NOTE.format(name=name)}",)
    _scrub(own)
    raise own


async def _launch(
    ticket: dict[str, Any], prompt: str, stuck: list[str]
) -> dict[str, Any]:
    settings = DockerWorkerSettings.from_env()
    available, detail = await asyncio.to_thread(verify_docker_runtime)
    if not available:
        raise DockerWorkerRuntimeUnavailable(
            f"isolated cloud Worker runtime is not available: {detail}"
        )
    if not prompt.strip():
        raise DockerWorkerRuntimeError("isolated cloud Worker prompt is empty")
    ticket_provider = str(ticket.get("worker_provider") or "").strip().lower()
    if ticket_provider and ticket_provider != settings.provider:
        raise DockerWorkerRuntimeError(
            f"Ticket provider {ticket_provider!r} does not match the isolated "
            f"Worker provider {settings.provider!r}"
        )
    # Validate everything that can fail before anything is written, so a bad
    # model/env/mandate value leaves no env file or snapshot behind.
    model = validate_worker_model(str(ticket.get("worker_model") or "").strip() or None)
    mandate = ticket.get("mandate") or {}
    if not isinstance(mandate, dict):
        raise DockerWorkerRuntimeError("the Ticket mandate is malformed")
    read_only = bool(mandate.get("speculative"))
    container_env = resolve_container_env(settings)
    _ensure_stage_root(settings)
    await _sweep_leftovers(settings)
    env_file: Path | None = None
    workspace: Path | None = None
    delivery_unsupported = True
    container_has_git = False
    name: str | None = None
    #: False from the moment a container may exist until its removal is
    #: confirmed. Only a confirmed-gone Worker makes the staging tree safe to
    #: chmod and delete.
    container_gone = True
    try:
        if container_env:
            env_file = _write_env_file(settings.stage_root, container_env)
        workspace_id = str(ticket["workspace_id"])
        # The staging copy and its baseline must describe one version: capture
        # before materialization, then require the manifest unchanged after it,
        # otherwise a concurrent update would be "restored" from older bytes.
        baseline_before = await _in_stage(
            _PREPARING,
            "reading the workspace manifest",
            lambda: _workspace_metadata(workspace_id),
        )
        workspace = await _in_stage(
            _PREPARING,
            "materializing the workspace",
            lambda: _stage_workspace(ticket, settings),
        )
        baseline = await _in_stage(
            _PREPARING,
            "reading the workspace manifest",
            lambda: _workspace_metadata(workspace_id),
        )
        if baseline != baseline_before:
            raise DockerWorkerRuntimeError(
                "the workspace changed while the isolated snapshot was being "
                "materialized; no container was started"
            )
        name = f"conductor-cloud-worker-{uuid.uuid4().hex[:16]}"
        argv = build_container_argv(
            settings,
            name=name,
            workspace=str(workspace),
            child_argv=("-c", "while :; do sleep 3600; done"),
            env_vars=container_env,
            env_file=str(env_file) if env_file else None,
            read_only_workspace=read_only,
            lifetime_seconds=settings.timeout_seconds,
            include_stdin=False,
            entrypoint="/bin/sh",
        )
        # Advertise scratch under the container HOME tmpfs (writable); do not
        # mkdir that path on the API host.
        from src.server.services.autonomy.worker import bind_ticket_scratch_prompt

        container_prompt, _ = bind_ticket_scratch_prompt(
            prompt,
            ticket["ticket_id"],
            home=_CONTAINER_HOME,
            create=False,
        )
        container_gone = False
        timed_out: asyncio.TimeoutError | None = None
        try:
            await _start_worker_container(settings, argv, name)
            # Same pinned Git configuration as the host probes. The outcome
            # selects the unsupported_runtime reason below; a shared staging
            # repository is still required before a real delivery check can run.
            container_has_git = await _container_git_available(
                settings, name, workspace
            )
            result = await _run_worker_exec(
                settings, name, model, container_prompt, settings.timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            timed_out = exc
        except BaseException:
            # Cancelled (again and again), or anything else: the container
            # still goes first, protected from further cancellation, and the
            # exception in flight continues afterwards.
            container_gone, absorbed = await _remove_container_protected(settings, name)
            if not container_gone:
                stuck.append(name)
            if absorbed:
                raise asyncio.CancelledError()
            raise
        if timed_out is not None:
            container_gone, cancelled = await _remove_container_protected(settings, name)
            if not container_gone:
                stuck.append(name)
            if cancelled:
                raise asyncio.CancelledError()
            raise DockerWorkerRuntimeError(
                f"isolated cloud Worker timed out after {settings.timeout_seconds:.0f}s"
                + ("" if container_gone else _STUCK_NOTE.format(name=name))
            ) from timed_out
        if result.returncode != 0:
            container_gone, cancelled = await _remove_container_protected(settings, name)
            if not container_gone:
                stuck.append(name)
            if cancelled:
                raise asyncio.CancelledError()
            raise _worker_failure(
                result.returncode,
                result.stderr or result.stdout or "no output",
                container_env,
                stuck,
            )
        text, final_text = _in_stage_sync(
            _READING,
            "parsing the Worker's output",
            lambda: _parse_provider_stdout(settings.provider, result.stdout),
        )
        # An empty final message is a missing completion report: it settles
        # as waiting through the same contract as any other invalid report.
        report = _in_stage_sync(
            _READING,
            "parsing the Worker's report",
            lambda: parse_worker_report(
                final_text, speculative=read_only, raw_text=text
            ),
        )
        if delivery_unsupported:
            report["git_check"] = "unsupported_runtime"
            if not container_has_git:
                reason = "The Worker image has no usable Git."
            else:
                reason = (
                    "Isolated cloud Worker completed the Ticket without a "
                    "shared Git delivery check."
                )
            prior = str(report.get("summary") or "").strip()
            report["summary"] = (
                f"git_check: unsupported_runtime. {reason}"
                + (f" {prior}" if prior else "")
            )
        container_gone, cancelled = await _remove_container_protected(settings, name)
        if not container_gone:
            stuck.append(name)
        if cancelled:
            raise asyncio.CancelledError()
        if not container_gone:
            report["state"] = "blocked"
            report["body"] = (report.get("body") or "") + _STUCK_NOTE.format(name=name)
        if not read_only and container_gone:
            await _in_stage(
                _PUBLISHING,
                "reading and storing the Worker's file changes",
                lambda: _publish_workspace_if_safe(
                    ticket=ticket, workspace=workspace, baseline=baseline
                ),
            )
        return {**report, "text": text, "container": name}
    finally:
        cancellation_absorbed = False
        if not container_gone and name is not None:
            container_gone, absorbed = await _remove_container_protected(settings, name)
            if not container_gone and name not in stuck:
                stuck.append(name)
            cancellation_absorbed = absorbed
        _unlink_env_file(env_file)
        if workspace is not None:
            await asyncio.to_thread(
                _remove_staging_tree, workspace, container_gone=container_gone
            )
        if cancellation_absorbed:
            raise asyncio.CancelledError()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def install() -> None:
    """Register the Docker runtime and its launch path with the switch.

    Idempotent against the module-level registries so a server that imports
    the switch again, or a test that swaps the registry, can re-register.
    """
    from src.server.services.autonomy import cloud_runtime

    if RUNTIME_ID in cloud_runtime.ISOLATED_RUNTIME_VERIFIERS:
        return
    cloud_runtime.register_cloud_worker_runtime(
        RUNTIME_ID, verify_docker_runtime, launch
    )
