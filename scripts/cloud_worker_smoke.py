#!/usr/bin/env python3
"""Live smoke for the Docker OS-isolated cloud Worker runtime.

The unit suite fakes every Docker call, so it cannot notice a container left
behind by a killed client, a bind mount that does not reach the host, or an
isolation probe that passes whether or not the flags took effect. This script
runs the runtime's own product code (``verify_docker_runtime``, the
``AUTONOMY_CLOUD_WORKER_RUNTIME`` switch and ``launch``) against the real local
Docker engine and asserts what only a live engine can show.

Only the three seams that need Postgres are replaced: the workspace manifest
read, the materializer that copies the manifest into the staging directory and
the fenced publication transaction. Everything else, including staging,
hardened argv construction, ``docker run``, timeout/cancel teardown and the
staging cleanup, is the product path. The publication stand-in records what the
real transaction would have been handed (the host staging tree, and the
changed files the product's own ``collect_changed_files`` finds in it).

By default a throwaway stub ``codex`` image is built from a local base image
(no vendor CLI, no credentials, no network). ``--image`` instead checks a real
Worker image (``conductor-cloud-worker:latest``): verification, that the CLI
still accepts the argv the runtime builds, and that a launch without
credentials fails closed. The script never reads ``.env`` files, never opens a
socket of its own, and prints only non-secret facts. Exit status is non-zero
when any check fails.

    python scripts/cloud_worker_smoke.py                       # stub image
    python scripts/cloud_worker_smoke.py --image conductor-cloud-worker:latest

What this run owns, and only that, is ever removed. Every ``docker run`` the
runtime issues goes through a one-line wrapper that adds this run's label, so
the leak checks and the cleanup see the containers the run created and leave a
real Worker launched meanwhile (by the backend, or another smoke run) alone.
The runtime's own expiry sweep, which a launch runs, is scoped the same way
(``AUTONOMY_CLOUD_WORKER_SWEEP_LABEL``): it may only remove expired containers
carrying this run's label, never another Worker that merely expired. A decoy
the run creates and removes itself checks that.
The fixture, the stub build context and the staging root are private
directories created inside ``--workdir`` / ``--stage-parent`` (or the defaults)
and removed as a whole. SIGINT, SIGTERM and SIGHUP run that cleanup; SIGKILL
cannot, and leaves the labelled container, the stub image and the directories:
``docker ps -aq --filter label=conductor.cloud-worker-smoke`` finds them.

The staging parent must be a path the Docker engine can bind mount. On macOS
that means under the home directory (colima and Docker Desktop do not share
``/tmp``), which is why it defaults to ``~/.cache/conductor``. It must already
exist: the run creates no parent, since it would not remove one, and says so
and stops if the default is missing. With ``--keep`` the images and directories
are left on purpose and listed as kept.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

# A developer's .env belongs to the backend, not to this process; stay hermetic.
# Assigned, not defaulted: an inherited PYTHON_DOTENV_DISABLED=false would
# otherwise let importing src.config load the file. Nothing above imports src.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

try:  # the product's config loader is chatty at debug level; the smoke prints verdicts
    import structlog

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING)
    )
except ImportError:  # pragma: no cover - structlog is a product dependency
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CONTAINER_PREFIX = "conductor-cloud-worker-"
STUB_TAG_PREFIX = "conductor-cloud-worker-smoke"
ENV_PREFIX = "AUTONOMY_CLOUD_WORKER_"
#: Label key added to every container this run starts; its value is the run token.
RUN_LABEL = "conductor.cloud-worker-smoke"
#: Label of the one container the sweep-scope scenario creates for itself: an
#: expired Worker container that is not this run's, so cleanup can find it.
DECOY_LABEL = "conductor.cloud-worker-smoke-decoy"

#: The mounts Docker itself adds to every container, whatever the runtime asks.
ENGINE_FILE_MOUNTS = frozenset({"/etc/hosts", "/etc/hostname", "/etc/resolv.conf"})

# The stub reports what the hardened container looks like from the inside as one
# Codex-shaped JSONL message, then ends with a completion report message. Its
# first prompt line selects the behaviour so a failing or hanging Worker needs
# no second image.
STUB_CODEX = r"""#!/bin/sh
# Stub of the Codex CLI for scripts/cloud_worker_smoke.py; it is not a vendor CLI.
if [ "$1" = "--version" ]; then
  echo "codex-cli 0.0.0-smoke-stub"
  exit 0
fi
if [ "$1" != "exec" ]; then
  echo "stub: unsupported invocation" >&2
  exit 64
fi
args="$*"
prompt=$(cat)
mode=$(printf '%s\n' "$prompt" | sed -n 's/^SMOKE_MODE=//p' | head -n 1)
case "$mode" in
  fail)
    echo "stub: simulated worker failure" >&2
    exit 7 ;;
  garbage)
    echo "this is not codex jsonl"
    exit 0 ;;
  sleep)
    echo "stub: waiting to be stopped" >&2
    exec sleep 300 ;;
  reach)
    # What can this container's network reach? The caller listens on the host's
    # loopback and names the port; the default gateway comes from the kernel's
    # routing table (hex, little endian).
    port=$(printf '%s\n' "$prompt" | sed -n 's/^SMOKE_PORT=//p' | head -n 1)
    nets=$(ls /sys/class/net 2>/dev/null | tr '\n' ',' | sed 's/,$//')
    gw_hex=$(awk '$2=="00000000" {print $3; exit}' /proc/net/route 2>/dev/null)
    gw=""
    if [ -n "$gw_hex" ]; then
      gw="$((0x$(echo "$gw_hex" | cut -c7-8))).$((0x$(echo "$gw_hex" | cut -c5-6))).$((0x$(echo "$gw_hex" | cut -c3-4))).$((0x$(echo "$gw_hex" | cut -c1-2)))"
    fi
    reached=""
    for host in host.docker.internal host.lima.internal $gw; do
      if timeout 3 bash -c "exec 3<>/dev/tcp/$host/$port" 2>/dev/null; then
        reached="$reached${reached:+,}$host"
      fi
    done
    printf '{"type":"thread.started","thread_id":"smoke-thread"}\n'
    printf '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"SMOKE_REACH nets=%s gateway=%s reached=%s"}}\n' "$nets" "$gw" "$reached"
    printf '{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"{\\"status\\":\\"done\\",\\"summary\\":\\"Smoke stub finished.\\",\\"artifacts\\":{\\"commits\\":[],\\"files\\":[],\\"pr\\":null},\\"verification\\":[],\\"next\\":\\"\\",\\"handbacks\\":[]}"}}\n'
    printf '{"type":"turn.completed","usage":{"input_tokens":1,"cached_input_tokens":0,"output_tokens":1}}\n'
    exit 0 ;;
  trap)
    # A Worker that makes its tree impossible to delete as the host user: a
    # directory chmod 000 with bytes inside. It still reports normally.
    mkdir -p /workspace/trap/inner
    head -c 65536 /dev/zero > /workspace/trap/inner/big.bin
    chmod 000 /workspace/trap/inner
    chmod 000 /workspace/trap ;;
esac

uid=$(id -u)
gid=$(id -g)
if (echo "written by the stub worker uid=$uid" > /workspace/worker-out.txt) 2>/dev/null; then
  ws_write=yes
else
  ws_write=no
fi
# /var/tmp is world-writable on the image's own filesystem: a canary that only a
# read-only root can deny (root-owned directories are denied to a non-root uid
# either way).
if (: > /var/tmp/.smoke-canary) 2>/dev/null; then
  rm -f /var/tmp/.smoke-canary
  canary=writable
else
  canary=denied
fi
root_opts=$(sed -n 's/^[0-9]* [0-9]* [0-9]*:[0-9]* [^ ]* \/ \([^ ]*\) .*/\1/p' /proc/self/mountinfo | head -n 1)
rootfs=${root_opts%%,*}
rw_mounts=""
rw_tmpfs=""
while read -r _id _parent _dev _root mp opts rest; do
  case "$opts" in rw|rw,*) ;; *) continue ;; esac
  fstype=${rest#*- }
  fstype=${fstype%% *}
  case "$fstype" in
    tmpfs) rw_tmpfs="$rw_tmpfs${rw_tmpfs:+,}$mp" ;;
    proc|sysfs|devpts|mqueue|cgroup|cgroup2|devtmpfs) ;;
    *) rw_mounts="$rw_mounts${rw_mounts:+,}$mp" ;;
  esac
done < /proc/self/mountinfo
if [ -e /var/run/docker.sock ]; then sock=present; else sock=absent; fi
nets=$(ls /sys/class/net 2>/dev/null | tr '\n' ',' | sed 's/,$//')
if [ -z "${SMOKE_HOST_ONLY+x}" ]; then host_env=absent; else host_env=present; fi
fwd_sha=$(printf '%s' "$SMOKE_FORWARDED" | sha256sum | cut -c1-12)
lit_sha=$(printf '%s' "$SMOKE_LITERAL" | sha256sum | cut -c1-12)
env_names=$(env | sed 's/=.*//' | sort | tr '\n' ',' | sed 's/,$//')
caps=$(sed -n 's/^CapBnd:[[:space:]]*//p' /proc/self/status)
nnp=$(sed -n 's/^NoNewPrivs:[[:space:]]*//p' /proc/self/status)
seed_sha=$(sha256sum /workspace/seed.txt 2>/dev/null | cut -c1-12)

facts="SMOKE_FACTS uid=$uid gid=$gid home=$HOME tmpdir=$TMPDIR xdg=$XDG_CONFIG_HOME pwd=$(pwd)"
facts="$facts ws_write=$ws_write canary=$canary rootfs=$rootfs rw_mounts=$rw_mounts rw_tmpfs=$rw_tmpfs"
facts="$facts sock=$sock nets=$nets host_env=$host_env fwd_sha=$fwd_sha lit_sha=$lit_sha"
facts="$facts caps=$caps nnp=$nnp seed_sha=$seed_sha env_names=$env_names"
printf '{"type":"thread.started","thread_id":"smoke-thread"}\n'
printf '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"%s\\nSMOKE_ARGS %s"}}\n' "$facts" "$args"
printf '{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"{\\"status\\":\\"done\\",\\"summary\\":\\"Smoke stub finished.\\",\\"artifacts\\":{\\"commits\\":[],\\"files\\":[],\\"pr\\":null},\\"verification\\":[],\\"next\\":\\"\\",\\"handbacks\\":[]}"}}\n'
printf '{"type":"turn.completed","usage":{"input_tokens":1,"cached_input_tokens":0,"output_tokens":1}}\n'
"""


# ---------------------------------------------------------------------------
# Result bookkeeping
# ---------------------------------------------------------------------------


class Results:
    """Collects PASS/FAIL lines; any failure makes the process exit non-zero."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []
        self.notes: list[tuple[str, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, bool(ok), detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f": {detail}" if detail else ""))
        return bool(ok)

    def info(self, name: str, detail: str) -> None:
        """A fact about this engine that is reported, not judged."""
        self.notes.append((name, detail))
        print(f"INFO  {name}: {detail}")

    @property
    def failed(self) -> list[str]:
        return [name for name, ok, _ in self.rows if not ok]


def _brief(exc: BaseException, limit: int = 240) -> str:
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text[:limit]}"


# ---------------------------------------------------------------------------
# Docker observation (independent of the runtime under test)
# ---------------------------------------------------------------------------


class Docker:
    def __init__(self, binary: str) -> None:
        self.binary = binary

    def run(
        self, *args: str, timeout: float = 120.0
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def containers_labelled(
        self, token: str, *, label: str = RUN_LABEL, running_only: bool = False
    ) -> set[str]:
        """Every container this run started, found by its label and nothing else."""
        args = [
            "ps",
            "--filter",
            f"label={label}={token}",
            "--format",
            "{{.Names}}",
        ]
        if not running_only:
            args.insert(1, "-a")
        proc = self.run(*args)
        if proc.returncode != 0:
            raise RuntimeError(f"docker ps failed: {proc.stderr.strip()[:200]}")
        return {line.strip() for line in proc.stdout.splitlines() if line.strip()}

    def image_exists(self, image: str) -> bool:
        return (
            self.run("image", "inspect", image, "--format", "{{.Id}}").returncode == 0
        )


def process_arguments_hide(
    values: Sequence[str],
    *,
    expect: Sequence[str],
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[bool, str]:
    """Whether the host's process list shows this run's launch and none of ``values``.

    Absence only means something if the list was actually read: a ``ps`` that
    fails, prints nothing, or does not show the launch itself proves nothing and
    fails. ``expect`` is what only this run's ``docker run`` carries (its
    unique label value and its container's name), all of which must appear: a
    listing that shows some other Worker started by the same code observed
    nothing of ours. The values are never put in the detail.
    """
    try:
        proc = run(
            ["ps", "-axww", "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"ps could not be run ({type(exc).__name__})"
    if proc.returncode != 0 or not proc.stdout.strip():
        return False, f"ps failed or listed nothing (exit {proc.returncode})"
    if not all(witness in proc.stdout for witness in expect):
        return False, "ps did not show the launch itself, so absence proves nothing"
    if any(value in proc.stdout for value in values):
        return False, "a forwarded value appears in the process arguments"
    return True, "ps showed the launch, and neither value"


@contextlib.contextmanager
def runtime_env(**overrides: str) -> Iterator[None]:
    """Replace every AUTONOMY_CLOUD_WORKER_* variable for the duration.

    The runtime reads ``os.environ`` on each call, so this is how a scenario
    configures it; a developer's own settings never leak into a scenario.
    """
    saved = {k: v for k, v in os.environ.items() if k.startswith(ENV_PREFIX)}
    for key in saved:
        del os.environ[key]
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key in [k for k in os.environ if k.startswith(ENV_PREFIX)]:
            del os.environ[key]
        os.environ.update(saved)


class Watcher:
    """Samples the engine while a launch runs, to prove a container existed."""

    def __init__(self, docker: Docker, token: str) -> None:
        self.docker = docker
        self.token = token
        self.seen: set[str] = set()
        self._task: asyncio.Task[None] | None = None

    async def _poll(self) -> None:
        while True:
            try:
                names = await asyncio.to_thread(
                    self.docker.containers_labelled, self.token
                )
            except Exception:  # noqa: BLE001 - observation must not end the run
                names = set()
            self.seen |= {n for n in names if "-probe-" not in n}
            await asyncio.sleep(0.4)

    async def __aenter__(self) -> "Watcher":
        self._task = asyncio.create_task(self._poll())
        return self

    async def __aexit__(self, *_exc: object) -> None:
        assert self._task is not None
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task


@contextlib.contextmanager
def patched(target: Any, name: str, value: Any) -> Iterator[None]:
    """Set an attribute of a product module for the duration of a scenario."""
    saved = getattr(target, name)
    setattr(target, name, value)
    try:
        yield
    finally:
        setattr(target, name, saved)


@contextlib.contextmanager
def captured_errors(logger: logging.Logger) -> Iterator[list[str]]:
    """Collect ERROR-level messages of a product logger instead of printing them."""
    messages: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.ERROR:
                messages.append(record.getMessage())

    handler = Collect()
    saved = logger.propagate
    logger.addHandler(handler)
    logger.propagate = False
    try:
        yield messages
    finally:
        logger.removeHandler(handler)
        logger.propagate = saved


def write_docker_wrapper(directory: Path, real_docker: str, token: str) -> Path:
    """A ``docker`` that labels every container the runtime starts with this run.

    The runtime is told to use it as its Docker binary, so what the run created
    is told apart from anyone else's by label rather than by guesswork. It also
    fails the next N ``rm -f conductor-cloud-worker-<hex>`` calls when the
    counter file says so, which is how the smoke shows a container removal that
    does not work.
    """
    path = directory / "docker"
    counter = directory / "rm-failures"
    quoted = shlex.quote(real_docker)
    path.write_text(
        "#!/bin/sh\n"
        "# Written by scripts/cloud_worker_smoke.py; not part of the product.\n"
        'if [ "$1" = "rm" ] && [ "$2" = "-f" ]; then\n'
        '  case "$3" in conductor-cloud-worker-[0-9a-f]*)\n'
        f"    left=$(cat {shlex.quote(str(counter))} 2>/dev/null || echo 0)\n"
        '    if [ "$left" -gt 0 ] 2>/dev/null; then\n'
        f"      echo $((left - 1)) > {shlex.quote(str(counter))}\n"
        '      echo "smoke: simulated rm failure for $3" >&2\n'
        "      exit 1\n"
        "    fi ;;\n"
        "  esac\n"
        "fi\n"
        'if [ "$1" = "run" ]; then\n'
        "  shift\n"
        f'  exec {quoted} run --label {RUN_LABEL}={token} "$@"\n'
        "fi\n"
        f'exec {quoted} "$@"\n'
    )
    path.chmod(0o700)
    return path


# ---------------------------------------------------------------------------
# The smoke
# ---------------------------------------------------------------------------


class Smoke:
    """One run. ``args.workdir`` and ``args.stage_root`` are private directories
    the run created for itself; ``cleanup`` removes them whole."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.results = Results()
        self.docker = Docker(args.docker_bin)
        self.token = secrets.token_hex(4)
        self.workdir: Path = args.workdir
        self.stage_root: Path = args.stage_root
        self.fixture = self.workdir / "workspace-fixture"
        self.image = args.image or f"{STUB_TAG_PREFIX}:{self.token}"
        self.stub_mode = args.image is None
        self.forwarded_value = "forwarded-" + secrets.token_hex(8)
        self.literal_value = "literal-" + secrets.token_hex(8)
        self.published: list[dict[str, Any]] = []
        self.seed_text = "seed file owned by the throwaway workspace\n"
        #: What the runtime is told to execute as ``docker``: the labelling
        #: wrapper when the real binary can be found, else the name as given
        #: (the runtime then fails closed, which is what a bad path should do).
        self.runtime_docker_bin = args.docker_bin
        self.bin_dir: Path | None = None
        self.extra_dirs: list[Path] = []
        #: Images beyond the stub that this run built (and removes again).
        self.extra_images: list[str] = []
        #: Expiry sweeps refused for not being scoped to this run.
        self.refused_sweeps = 0

        from src.server.services.autonomy import cloud_runtime
        from src.server.services.autonomy import docker_worker_runtime as runtime

        self.cloud_runtime = cloud_runtime
        self.rt = runtime

    def install_docker_wrapper(self) -> None:
        name = self.args.docker_bin
        real = name if "/" in name else shutil.which(name)
        if not real or not os.access(real, os.X_OK):
            return
        self.bin_dir = Path(tempfile.mkdtemp(prefix="cloud-worker-smoke-bin-"))
        if any(ch.isspace() for ch in str(self.bin_dir)):
            raise RuntimeError(
                f"the temp directory path {self.bin_dir} contains whitespace, which "
                "the runtime refuses in a Docker binary path; set TMPDIR to a path without"
            )
        self.runtime_docker_bin = str(
            write_docker_wrapper(self.bin_dir, real, self.token)
        )

    def guard_sweeps(self) -> None:
        """Let no expiry sweep reach Docker unless it is scoped to this run.

        The scenarios configure the scope through the environment; this is the
        backstop under them, so a scenario or a code path that forgot to would
        be refused before it listed, let alone removed, anyone else's container
        on a shared engine. A refusal is counted and fails the run.
        """
        real = self.rt._reap_expired
        expected = f"{RUN_LABEL}={self.token}"

        def scoped_only(settings: Any, *, now: float | None = None) -> Any:
            if settings.sweep_label != expected:
                self.refused_sweeps += 1
                raise RuntimeError("refused an expiry sweep not scoped to this run")
            return real(settings, now=now)

        self.rt._reap_expired = scoped_only

    def remove_probe_dir(self, directory: Path, container: str) -> None:
        """Remove a probe container, then its directory only if it is confirmed gone."""
        removal = self.docker.run("rm", "-f", container, timeout=30)
        gone = (
            removal.returncode == 0
            or "no such container" in (removal.stderr + removal.stdout).lower()
        )
        self.rt._remove_staging_tree(directory, container_gone=gone)

    def set_rm_failures(self, count: int) -> None:
        """Make the next ``count`` removals of a Worker container fail."""
        assert self.bin_dir is not None
        (self.bin_dir / "rm-failures").write_text(f"{count}\n")

    # -- environment ------------------------------------------------------

    def env(self, **extra: str) -> contextlib.AbstractContextManager[None]:
        base = {
            "AUTONOMY_CLOUD_WORKER_RUNTIME": "docker",
            "AUTONOMY_CLOUD_WORKER_DOCKER_BIN": self.runtime_docker_bin,
            "AUTONOMY_CLOUD_WORKER_IMAGE": self.image,
            "AUTONOMY_CLOUD_WORKER_PROVIDER": "codex",
            "AUTONOMY_CLOUD_WORKER_NETWORK": "none",
            "AUTONOMY_CLOUD_WORKER_ROOT": str(self.stage_root),
            "AUTONOMY_CLOUD_WORKER_TIMEOUT": "120",
            "AUTONOMY_CLOUD_WORKER_ENV": "SMOKE_FORWARDED",
            "AUTONOMY_CLOUD_WORKER_SET_ENV": f"SMOKE_LITERAL={self.literal_value}",
            # Every expiry sweep the runtime runs, at a launch or when a scenario
            # calls it, may only touch containers carrying this run's label.
            "AUTONOMY_CLOUD_WORKER_SWEEP_LABEL": f"{RUN_LABEL}={self.token}",
        }
        base.update(extra)
        return runtime_env(**base)

    def ticket(self, **over: Any) -> dict[str, Any]:
        ticket = {
            "ticket_id": "smoke" + secrets.token_hex(6),
            "user_id": "smoke-user",
            "workspace_id": "smoke-workspace",
            "worker_provider": "codex",
            "worker_model": "smoke-model",
            "worker_thread_id": "smoke-thread",
            "worker_run_id": "smoke-run",
            "mandate": {},
        }
        ticket.update(over)
        return ticket

    # -- fixture workspace and the three Postgres seams -------------------

    def _baseline_manifest(self) -> dict[str, dict[str, Any]]:
        manifest: dict[str, dict[str, Any]] = {}
        for path in sorted(self.fixture.rglob("*")):
            if path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                manifest[path.relative_to(self.fixture).as_posix()] = {
                    "content_hash": digest
                }
        return manifest

    @contextlib.contextmanager
    def database_seams(self) -> Iterator[None]:
        from src.server.services.native_runtime import workspace as native_workspace

        rt = self.rt
        manifest = self._baseline_manifest()
        smoke = self

        async def workspace_metadata(_workspace_id: str) -> dict[str, dict[str, Any]]:
            return dict(manifest)

        async def materialize(
            *, workspace_id: str, cwd: str | Path, user_id: str
        ) -> int:
            del workspace_id, user_id
            # File by file: copying the fixture root's metadata onto the staging
            # root would hide whether the runtime created it private.
            for rel in manifest:
                target = Path(cwd) / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((smoke.fixture / rel).read_bytes())
            return len(manifest)

        async def publish(
            *, ticket: dict[str, Any], workspace: Path, baseline: dict[str, Any]
        ) -> int:
            smoke.published.append(smoke._observe_staging(ticket, workspace, baseline))
            return 0

        saved = (
            rt._workspace_metadata,
            rt._publish_workspace_if_safe,
            native_workspace.materialize_workspace,
        )
        rt._workspace_metadata = workspace_metadata  # type: ignore[assignment]
        rt._publish_workspace_if_safe = publish  # type: ignore[assignment]
        native_workspace.materialize_workspace = materialize  # type: ignore[assignment]
        try:
            yield
        finally:
            (
                rt._workspace_metadata,
                rt._publish_workspace_if_safe,
                native_workspace.materialize_workspace,
            ) = saved

    def _observe_staging(
        self, ticket: dict[str, Any], workspace: Path, baseline: dict[str, Any]
    ) -> dict[str, Any]:
        """What the host sees at the moment the real transaction would run."""
        from src.server.services.native_runtime.workspace import collect_changed_files

        upserts, _removed = collect_changed_files(workspace, baseline)
        out = workspace / "worker-out.txt"
        env_files = sorted(p for p in self.stage_root.glob("env-*.env"))
        trap = workspace / "trap"
        return {
            "trap_mode": trap.stat().st_mode & 0o777 if trap.exists() else None,
            "workspace": workspace,
            "workspace_mode": workspace.stat().st_mode & 0o777,
            "workspace_parent": workspace.parent,
            "changed": sorted(u["file_path"] for u in upserts),
            "out_text": out.read_text() if out.is_file() else None,
            "out_uid": out.stat().st_uid if out.is_file() else None,
            "seed_text": (workspace / "seed.txt").read_text()
            if (workspace / "seed.txt").is_file()
            else None,
            "env_file_modes": [p.stat().st_mode & 0o777 for p in env_files],
            "ticket_id": ticket.get("ticket_id"),
        }

    # -- helpers -----------------------------------------------------------

    def stage_entries(self) -> list[str]:
        if not self.stage_root.exists():
            return []
        return sorted(p.name for p in self.stage_root.iterdir())

    def owned_containers(self) -> set[str]:
        """Containers this run started that still exist (any state)."""
        return self.docker.containers_labelled(self.token)

    def facts_from(self, text: str) -> tuple[dict[str, str], str]:
        facts: dict[str, str] = {}
        args = ""
        for line in text.splitlines():
            if line.startswith("SMOKE_FACTS "):
                for pair in line.split()[1:]:
                    key, _, value = pair.partition("=")
                    facts[key] = value
            elif line.startswith("SMOKE_ARGS "):
                args = line[len("SMOKE_ARGS ") :]
        return facts, args

    def assert_clean(self, label: str) -> None:
        leftovers = self.owned_containers()
        self.results.check(
            f"{label}: no container left behind",
            not leftovers,
            "none" if not leftovers else f"left {sorted(leftovers)}",
        )
        for name in leftovers:  # only containers carrying this run's label
            self.docker.run("rm", "-f", name, timeout=30)
        entries = self.stage_entries()
        self.results.check(
            f"{label}: staging root empty (workspace copy and env file removed)",
            not entries,
            "empty" if not entries else f"left {entries}",
        )

    # -- scenarios ---------------------------------------------------------

    def build_stub_image(self) -> bool:
        context = self.workdir / "stub-image"
        context.mkdir(parents=True, exist_ok=True)
        (context / "codex").write_text(STUB_CODEX)
        (context / "Dockerfile").write_text(
            f"FROM {self.args.stub_base}\n"
            "COPY codex /usr/local/bin/codex\n"
            "RUN chmod 0755 /usr/local/bin/codex\n"
        )
        started = time.monotonic()
        proc = self.docker.run(
            "build", "-q", "-t", self.image, str(context), timeout=600
        )
        ok = proc.returncode == 0
        self.results.check(
            "stub image builds from a local base",
            ok,
            f"{self.image} from {self.args.stub_base} in {time.monotonic() - started:.1f}s"
            if ok
            else proc.stderr.strip()[-240:],
        )
        return ok

    def scenario_image_hygiene(self) -> None:
        proc = self.docker.run(
            "image", "inspect", self.image, "--format", "{{json .Config.Env}}"
        )
        names: list[str] = []
        if proc.returncode == 0:
            names = [e.split("=", 1)[0] for e in json.loads(proc.stdout or "[]")]
        suspicious = [
            n
            for n in names
            if any(
                w in n.upper()
                for w in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
            )
        ]
        self.results.check(
            "image bakes no credential-shaped environment variable",
            proc.returncode == 0 and not suspicious,
            f"{len(names)} image env names, none credential-shaped"
            if not suspicious
            else f"credential-shaped names: {suspicious}",
        )

    def scenario_verify(self) -> None:
        rt = self.rt
        with self.env():
            rt.clear_verification_cache()
            ok, detail = rt.verify_docker_runtime()
        self.results.check(
            "verify_docker_runtime accepts the image",
            ok
            and "isolation probe ok" in detail
            and "worker CLI codex present" in detail,
            detail,
        )
        with self.env(AUTONOMY_CLOUD_WORKER_IMAGE=f"{STUB_TAG_PREFIX}:does-not-exist"):
            rt.clear_verification_cache()
            ok, detail = rt.verify_docker_runtime()
        self.results.check(
            "verify_docker_runtime refuses a missing image",
            not ok and "is not available" in detail,
            detail[:160],
        )
        if self.stub_mode:
            # The base image has no codex binary: the CLI check must say so.
            with self.env(AUTONOMY_CLOUD_WORKER_IMAGE=self.args.stub_base):
                rt.clear_verification_cache()
                ok, detail = rt.verify_docker_runtime()
            self.results.check(
                "verify_docker_runtime refuses an image without the Worker CLI",
                not ok and "worker CLI 'codex' is not available" in detail,
                detail[:160],
            )
        self.assert_clean("verify")

    def scenario_probe_has_teeth(self) -> None:
        """The isolation probe must fail when a hardening flag is missing.

        Run the runtime's own probe command with its own argv builder, then once
        more with one control removed at a time. A probe that still passes would
        let an unhardened container verify as isolated.
        """
        rt = self.rt
        with self.env():
            settings = rt.DockerWorkerSettings.from_env()
        # control -> (argv flags and their widths that implement it, why the
        # probe must fail without it). Failing for any other reason would not
        # show that the probe looks at the control.
        controls: dict[str, tuple[list[tuple[str, int]], str]] = {
            "--read-only": ([("--read-only", 1)], "root filesystem is writable"),
            "--cap-drop": ([("--cap-drop", 2)], "capabilities were not dropped"),
            "--security-opt": ([("--security-opt", 2)], "no-new-privileges is not set"),
            "--user": ([("--user", 2)], "runs as root"),
            "--pids-limit": ([("--pids-limit", 2)], "PID limit is not enforced"),
            "--memory": (
                [("--memory", 2), ("--memory-swap", 2)],
                "memory limit is not enforced",
            ),
            "--memory-swap": ([("--memory-swap", 2)], "swap is not disabled"),
            "the workspace bind mount": (
                [("--mount", 2)],
                "workspace is not a bind mount",
            ),
        }

        def run_probe(
            drop: str | None, *, read_only_workspace: bool = False
        ) -> subprocess.CompletedProcess[str]:
            bind = Path(tempfile.mkdtemp(prefix="probe-", dir=self.stage_root))
            name = f"{CONTAINER_PREFIX}probe-smoke-{secrets.token_hex(4)}"
            argv = list(
                rt.isolation_probe_argv(
                    settings,
                    name=name,
                    workspace=str(bind),
                    read_only_workspace=read_only_workspace,
                )
            )
            for flag, width in controls[drop][0] if drop else []:
                index = argv.index(flag)
                del argv[index : index + width]
            try:
                return subprocess.run(
                    argv, capture_output=True, text=True, timeout=60, check=False
                )
            finally:
                self.remove_probe_dir(bind, name)

        full = run_probe(None)
        self.results.check(
            "isolation probe passes with every hardening flag and a bound workspace",
            full.returncode == 0 and "isolation-ok" in full.stdout,
            f"exit {full.returncode}"
            + (f" {full.stderr.strip()[:120]}" if full.returncode else ""),
        )
        for control, (_flags, reason) in controls.items():
            weakened = run_probe(control)
            self.results.check(
                f"isolation probe fails without {control}",
                weakened.returncode != 0 and reason in weakened.stderr,
                f"exit {weakened.returncode}: "
                + (weakened.stderr.strip().splitlines() or [""])[-1][:100],
            )
        readonly = run_probe(None, read_only_workspace=True)
        self.results.check(
            "isolation probe fails when the workspace is not writable by the Worker",
            readonly.returncode != 0 and "workspace is not writable" in readonly.stderr,
            f"exit {readonly.returncode}: "
            + (readonly.stderr.strip().splitlines() or [""])[-1][:100],
        )
        self.assert_clean("probe controls")

    def scenario_entrypoint_swallows_probe(self) -> None:
        """An image whose ENTRYPOINT ignores or echoes its arguments must not verify.

        ``ENTRYPOINT ["/bin/true"]`` turns ``sh -c <probe>`` into ``true sh -c
        <probe>``: exit 0, no output, the probe never ran. The probe now names
        its own shell, so it still runs and passes; the version check goes
        through the image as a launch does and refuses it. So does an
        ``ENTRYPOINT ["echo", "isolation-ok"]`` image, which prints the probe's
        own success line and then echoes ``codex --version`` back: output that
        does not identify the CLI. (An image that lies on purpose is outside
        what verification can see.)
        """
        rt = self.rt
        variants = (
            ("swallow", '["/bin/true"]', "printed nothing"),
            ("echo", '["echo", "isolation-ok"]', "does not identify"),
        )
        for tag, entrypoint, reason in variants:
            image = f"{STUB_TAG_PREFIX}:{self.token}-{tag}"
            context = self.workdir / f"{tag}-image"
            context.mkdir(parents=True, exist_ok=True)
            (context / "Dockerfile").write_text(
                f"FROM {self.image}\nENTRYPOINT {entrypoint}\n"
            )
            self.extra_images.append(image)
            built = self.docker.run(
                "build", "-q", "-t", image, str(context), timeout=600
            )
            if not self.results.check(
                f"entrypoint {tag}: a variant image with ENTRYPOINT {entrypoint} builds",
                built.returncode == 0,
                image if built.returncode == 0 else built.stderr.strip()[-200:],
            ):
                continue
            with self.env(AUTONOMY_CLOUD_WORKER_IMAGE=image):
                settings = rt.DockerWorkerSettings.from_env()
                bind = Path(tempfile.mkdtemp(prefix="probe-", dir=self.stage_root))
                name = f"{CONTAINER_PREFIX}probe-smoke-{secrets.token_hex(4)}"
                try:
                    argv = rt.isolation_probe_argv(
                        settings, name=name, workspace=str(bind)
                    )
                    probe = subprocess.run(
                        argv, capture_output=True, text=True, timeout=60, check=False
                    )
                finally:
                    self.remove_probe_dir(bind, name)
                self.results.check(
                    f"entrypoint {tag}: the probe still runs and passes under its own shell",
                    probe.returncode == 0 and probe.stdout.strip() == "isolation-ok",
                    f"exit {probe.returncode} stdout={probe.stdout.strip()[:40]!r}",
                )
                rt.clear_verification_cache()
                ok, detail = rt.verify_docker_runtime()
            self.results.check(
                f"entrypoint {tag}: verification refuses the image ({reason})",
                not ok and reason in detail,
                detail[:160],
            )
            self.assert_clean(f"entrypoint {tag}")

    async def scenario_launch_ok(self) -> None:
        rt = self.rt
        prompt = "SMOKE_MODE=ok\nReply with OK.\n"
        ticket = self.ticket()
        self.published.clear()
        started = time.monotonic()
        with self.env(), self.database_seams():
            os.environ["SMOKE_FORWARDED"] = self.forwarded_value
            os.environ["SMOKE_HOST_ONLY"] = "host-only-" + secrets.token_hex(4)
            rt.clear_verification_cache()
            try:
                isolation = (
                    await self.cloud_runtime.resolve_cloud_worker_isolation_async()
                )
                self.results.check(
                    "switch resolves enabled, verified and launch-wired",
                    isolation.launch_allowed,
                    f"requested={isolation.requested} enabled={isolation.enabled} "
                    f"verified={isolation.verified} wired={isolation.launch_wired}",
                )
                report = await self.cloud_runtime.launch_cloud_worker(ticket, prompt)
            finally:
                os.environ.pop("SMOKE_FORWARDED", None)
                os.environ.pop("SMOKE_HOST_ONLY", None)
            settings = rt.DockerWorkerSettings.from_env()
        self.results.check(
            "launch through the switch returns a ready report",
            report.get("state") == "ready"
            and str(report.get("container", "")).startswith(CONTAINER_PREFIX),
            f"state={report.get('state')} container={report.get('container')} "
            f"in {time.monotonic() - started:.1f}s",
        )
        facts, args = self.facts_from(str(report.get("text", "")))
        self.results.check(
            "stub reported its facts", bool(facts), f"{len(facts)} facts"
        )
        expected_args = " ".join(
            rt.build_worker_argv(settings, model="smoke-model")[1:]
        )
        self.results.check(
            "Worker CLI received the runtime's one-shot argv (prompt on stdin)",
            args == expected_args,
            args,
        )
        self.results.check(
            "Worker runs as the host uid:gid, not root",
            facts.get("uid") == str(os.getuid())
            and facts.get("gid") == str(os.getgid())
            and facts.get("uid") != "0",
            f"uid={facts.get('uid')} gid={facts.get('gid')}",
        )
        self.results.check(
            "capability bounding set emptied and no-new-privileges set",
            set(facts.get("caps", "x")) == {"0"} and facts.get("nnp") == "1",
            f"CapBnd={facts.get('caps')} NoNewPrivs={facts.get('nnp')}",
        )
        self.results.check(
            "root filesystem is read-only (mount option and write canary)",
            facts.get("rootfs") == "ro" and facts.get("canary") == "denied",
            f"rootfs={facts.get('rootfs')} /var/tmp canary={facts.get('canary')}",
        )
        rw = {m for m in facts.get("rw_mounts", "").split(",") if m}
        self.results.check(
            "the workspace bind mount is the only writable mount",
            "/workspace" in rw and rw <= {"/workspace"} | ENGINE_FILE_MOUNTS,
            f"writable non-tmpfs mounts: {sorted(rw)}",
        )
        # /dev, /dev/shm and the /proc masks are the engine's own; the rest is the runtime's.
        tmpfs = {
            m
            for m in facts.get("rw_tmpfs", "").split(",")
            if m and m not in {"/dev", "/dev/shm"} and not m.startswith("/proc/")
        }
        self.results.check(
            "scratch is tmpfs-only: /tmp and the Worker HOME (outside /tmp), nothing else",
            tmpfs == {"/tmp", "/home/worker"},
            f"writable tmpfs mounts besides the engine's: {sorted(tmpfs)}",
        )
        self.results.check(
            "no Docker socket, no network interface but loopback",
            facts.get("sock") == "absent" and facts.get("nets") == "lo",
            f"socket={facts.get('sock')} interfaces={facts.get('nets')}",
        )
        names = set(facts.get("env_names", "").split(","))
        self.results.check(
            "container environment: managed names only, host environment absent",
            facts.get("host_env") == "absent"
            and not any(n.startswith("CONDUCTOR_") for n in names)
            and facts.get("home") == "/home/worker"
            and facts.get("tmpdir") == "/tmp"
            and facts.get("xdg") == "/home/worker/.config"
            and facts.get("pwd") == "/workspace",
            f"HOME={facts.get('home')} PWD={facts.get('pwd')} names={sorted(names)}",
        )
        sha = lambda value: hashlib.sha256(value.encode()).hexdigest()[:12]  # noqa: E731
        self.results.check(
            "operator-listed variables arrive (allowlisted host value and literal)",
            facts.get("fwd_sha") == sha(self.forwarded_value)
            and facts.get("lit_sha") == sha(self.literal_value),
            "both digests match",
        )
        seen = self.published[-1] if self.published else {}
        self.results.check(
            "staged workspace reached the container (seed file readable)",
            facts.get("seed_sha")
            == hashlib.sha256(self.seed_text.encode()).hexdigest()[:12],
            "seed digest matches",
        )
        self.results.check(
            "file written inside the container appears in the host staging copy",
            seen.get("out_text") == f"written by the stub worker uid={os.getuid()}\n"
            and seen.get("out_uid") == os.getuid(),
            f"worker-out.txt owner uid={seen.get('out_uid')}",
        )
        self.results.check(
            "seed file in the staging copy is unmodified; only the Worker's file changed",
            seen.get("seed_text") == self.seed_text
            and seen.get("changed") == ["worker-out.txt"],
            f"changed={seen.get('changed')}",
        )
        staged = seen.get("workspace")
        self.results.check(
            "staging directory is private and lives under the staging root",
            bool(staged)
            and seen.get("workspace_mode") == 0o700
            and Path(staged).parent == self.stage_root,
            f"mode={oct(seen.get('workspace_mode') or 0)}",
        )
        self.results.check(
            "forwarded credentials travel in a 0600 env file, then it is removed",
            seen.get("env_file_modes") == [0o600]
            and not list(self.stage_root.glob("env-*.env")),
            f"modes during run={[oct(m) for m in seen.get('env_file_modes', [])]}",
        )
        self.results.check(
            "staging directory removed after the launch",
            bool(staged) and not Path(staged).exists(),
            "gone" if staged and not Path(staged).exists() else "still present",
        )
        self.assert_clean("launch ok")

    async def scenario_launch_read_only(self) -> None:
        self.published.clear()
        ticket = self.ticket(mandate={"speculative": True})
        with self.env(), self.database_seams():
            os.environ["SMOKE_FORWARDED"] = self.forwarded_value
            try:
                report = await self.rt.launch(ticket, "SMOKE_MODE=ok\n")
            finally:
                os.environ.pop("SMOKE_FORWARDED", None)
        facts, _args = self.facts_from(str(report.get("text", "")))
        rw = {m for m in facts.get("rw_mounts", "").split(",") if m}
        self.results.check(
            "speculative Ticket: workspace mounted read-only, write denied",
            facts.get("ws_write") == "no" and "/workspace" not in rw,
            f"write={facts.get('ws_write')} writable non-tmpfs mounts={sorted(rw)}",
        )
        self.results.check(
            "speculative Ticket: nothing is published",
            not self.published,
            f"publication calls={len(self.published)}",
        )
        self.assert_clean("launch read-only")

    async def scenario_launch_failures(self) -> None:
        rt = self.rt
        cases: list[tuple[str, str, Callable[[BaseException], bool]]] = [
            (
                "Worker exits non-zero",
                "SMOKE_MODE=fail\n",
                lambda e: (
                    isinstance(e, rt.DockerWorkerRuntimeError) and "exited 7" in str(e)
                ),
            ),
            (
                "Worker prints no machine-readable events",
                "SMOKE_MODE=garbage\n",
                lambda e: not isinstance(e, asyncio.CancelledError),
            ),
        ]
        for label, prompt, matches in cases:
            self.published.clear()
            error: BaseException | None = None
            with self.env(), self.database_seams():
                os.environ["SMOKE_FORWARDED"] = self.forwarded_value
                try:
                    await rt.launch(self.ticket(), prompt)
                except Exception as exc:  # noqa: BLE001 - the failure is the subject
                    error = exc
                finally:
                    os.environ.pop("SMOKE_FORWARDED", None)
            self.results.check(
                f"failing launch ({label}) raises and publishes nothing",
                error is not None and matches(error) and not self.published,
                _brief(error) if error else "no error raised",
            )
            self.assert_clean(f"failing launch ({label})")

        # Verification and validation failures must not stage a single byte.
        before = self.owned_containers()
        for label, env_over, ticket_over, wanted in [
            (
                "image missing",
                {"AUTONOMY_CLOUD_WORKER_IMAGE": f"{STUB_TAG_PREFIX}:does-not-exist"},
                {},
                "is not available",
            ),
            (
                "model rejected",
                {},
                {"worker_model": "bad model; rm -rf /"},
                "invalid isolated Worker model",
            ),
            ("provider mismatch", {}, {"worker_provider": "claude"}, "does not match"),
        ]:
            error = None
            with self.env(**env_over), self.database_seams():
                rt.clear_verification_cache()
                try:
                    await rt.launch(self.ticket(**ticket_over), "SMOKE_MODE=ok\n")
                except Exception as exc:  # noqa: BLE001
                    error = exc
            self.results.check(
                f"refused launch ({label}) raises before any container or staging",
                error is not None
                and wanted in str(error)
                and self.owned_containers() == before,
                _brief(error) if error else "no error raised",
            )
        self.assert_clean("refused launches")

    async def scenario_timeout(self) -> None:
        rt = self.rt
        error: BaseException | None = None
        started = time.monotonic()
        async with Watcher(self.docker, self.token) as watch:
            with self.env(AUTONOMY_CLOUD_WORKER_TIMEOUT="10"), self.database_seams():
                try:
                    await rt.launch(self.ticket(), "SMOKE_MODE=sleep\n")
                except Exception as exc:  # noqa: BLE001
                    error = exc
        elapsed = time.monotonic() - started
        self.results.check(
            "timeout: the launch fails closed at the wall-clock limit",
            isinstance(error, rt.DockerWorkerRuntimeError)
            and "timed out after 10s" in str(error),
            f"{_brief(error) if error else 'no error'} after {elapsed:.1f}s",
        )
        self.results.check(
            "timeout: a Worker container really was running (the check is meaningful)",
            bool(watch.seen),
            f"observed {sorted(watch.seen)}",
        )
        self.assert_clean("timeout")

    async def scenario_cancel(self) -> None:
        """Stop/switch cancels the launch task; the container must not outlive it."""
        rt = self.rt
        ticket = self.ticket()
        with self.env(), self.database_seams():
            os.environ["SMOKE_FORWARDED"] = self.forwarded_value
            try:
                async with Watcher(self.docker, self.token):
                    task = asyncio.create_task(rt.launch(ticket, "SMOKE_MODE=sleep\n"))
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline and not task.done():
                        running = await asyncio.to_thread(self._running_workers)
                        if running:
                            break
                        await asyncio.sleep(0.5)
                    running = await asyncio.to_thread(self._running_workers)
                    self.results.check(
                        "cancel: a Worker container is running before the cancel",
                        bool(running) and not task.done(),
                        f"running={sorted(running)}",
                    )
                    staging_during = self.stage_entries()
                    markers = [n for n in staging_during if n.endswith(".owner")]
                    entries = [n for n in staging_during if not n.endswith(".owner")]
                    self.results.check(
                        "cancel: one staging directory and one env file exist while it runs, each with its ownership marker",
                        len(entries) == 2
                        and sum(n.startswith("env-") for n in entries) == 1
                        and sorted(markers) == sorted(f"{n}.owner" for n in entries),
                        f"{len(entries)} entries, {len(markers)} markers",
                    )
                    hidden, detail = process_arguments_hide(
                        [self.forwarded_value, self.literal_value],
                        expect=[f"{RUN_LABEL}={self.token}", *sorted(running)],
                    )
                    self.results.check(
                        "cancel: forwarded and literal values are absent from host process arguments",
                        hidden,
                        detail,
                    )
                    started = time.monotonic()
                    task.cancel()
                    cancelled = False
                    try:
                        await asyncio.wait_for(task, timeout=60)
                    except asyncio.CancelledError:
                        cancelled = True
                    except Exception as exc:  # noqa: BLE001
                        cancelled = False
                        self.results.check(
                            "cancel: task ends with CancelledError", False, _brief(exc)
                        )
                    elapsed = time.monotonic() - started
                    self.results.check(
                        "cancel: teardown completes before the cancelled launch returns",
                        cancelled,
                        f"{elapsed:.1f}s",
                    )
            finally:
                os.environ.pop("SMOKE_FORWARDED", None)
        self.assert_clean("cancel")

    async def scenario_hostile_tree(self) -> None:
        """A Worker that chmod 000s its tree must not leave bytes on the host."""
        self.published.clear()
        with self.env(), self.database_seams():
            os.environ["SMOKE_FORWARDED"] = self.forwarded_value
            try:
                report = await self.rt.launch(self.ticket(), "SMOKE_MODE=trap\n")
            finally:
                os.environ.pop("SMOKE_FORWARDED", None)
        seen = self.published[-1] if self.published else {}
        trap = Path(seen.get("workspace", self.stage_root)) / "trap"
        self.results.check(
            "unremovable tree: the Worker did leave a chmod 000 directory (the check is meaningful)",
            report.get("state") == "ready" and seen.get("trap_mode") == 0,
            f"state={report.get('state')} trap mode={oct(seen.get('trap_mode') or 0)} ({trap.name})",
        )
        self.assert_clean("unremovable tree")

    async def scenario_rm_failure(self) -> None:
        """A failed ``docker rm -f`` is retried, then left to the expiry sweep."""
        rt = self.rt
        for failures, label in (
            (1, "one failed removal"),
            (rt._RM_ATTEMPTS, "every removal fails"),
        ):
            self.set_rm_failures(failures)
            error: BaseException | None = None
            # A past expiry stands in for the hours a real deadline takes.
            with (
                patched(rt, "_REAP_GRACE_SECONDS", -1000.0),
                self.env(AUTONOMY_CLOUD_WORKER_TIMEOUT="10"),
                self.database_seams(),
                captured_errors(rt.logger) as logged,
            ):
                try:
                    await rt.launch(self.ticket(), "SMOKE_MODE=sleep\n")
                except Exception as exc:  # noqa: BLE001
                    error = exc
                survivors = self.owned_containers()
                self.set_rm_failures(0)
                if failures == 1:
                    self.results.check(
                        f"{label}: the retry removes the container; the Ticket still fails on the timeout",
                        isinstance(error, rt.DockerWorkerRuntimeError)
                        and "timed out" in str(error)
                        and "could not be removed" not in str(error)
                        and not survivors,
                        f"{_brief(error, 120) if error else 'no error'}; left {sorted(survivors)}",
                    )
                else:
                    self.results.check(
                        f"{label}: the error says so and the container is left for the sweep",
                        isinstance(error, rt.DockerWorkerRuntimeError)
                        and "could not be removed" in str(error)
                        and len(survivors) == 1,
                        f"{_brief(error, 160) if error else 'no error'}; left {sorted(survivors)}",
                    )
                    self.results.check(
                        f"{label}: the failure is logged at ERROR level, not debug",
                        any("could not be removed" in m for m in logged),
                        f"{len(logged)} error record(s)",
                    )
                    in_place = [
                        n
                        for n in self.stage_entries()
                        if not n.startswith("env-") and not n.endswith(".owner")
                    ]
                    self.results.check(
                        f"{label}: the staging copy is left in place while the container is not confirmed gone",
                        len(in_place) == 1,
                        f"{len(in_place)} staging copy(ies)",
                    )
                    left_trees_before_reap = [] if len(in_place) == 1 else in_place
                    settings = rt.DockerWorkerSettings.from_env()
                    reaped = await asyncio.to_thread(
                        rt.reap_expired_containers, settings
                    )
                    self.results.check(
                        "the expiry sweep removes the survivor by its label",
                        set(reaped) == survivors and not self.owned_containers(),
                        f"reaped {sorted(reaped)}",
                    )
                    await asyncio.to_thread(rt.sweep_leftovers, settings)
                    self.results.check(
                        f"{label}: the staging copy waited for the container, and the sweep then removed it",
                        not left_trees_before_reap and not self.stage_entries(),
                        f"staging copies left while the container survived: {left_trees_before_reap}"
                        if left_trees_before_reap
                        else "gone after the sweep",
                    )
            self.assert_clean(f"rm failure ({label})")

    def scenario_sweep_scope(self) -> None:
        """The expiry sweep removes this run's containers and nothing else.

        The engine is shared with real Workers, so "expired" must not be enough
        to be removed. A decoy the run creates itself stands in for one: a
        container named and labelled like a Worker whose deadline passed long
        ago, but without this run's label. The unscoped query a backend uses
        would list it (checked below, read-only); the scoped sweep must not.
        Nothing here ever runs an unscoped sweep.
        """
        rt = self.rt
        name = f"{CONTAINER_PREFIX}decoy-{secrets.token_hex(4)}"
        created = self.docker.run(
            "create",
            "--name",
            name,
            "--label",
            f"{rt._LABEL_DEADLINE}=1",
            "--label",
            f"{DECOY_LABEL}={self.token}",
            self.image,
            "true",
        )
        if not self.results.check(
            "sweep scope: an expired decoy Worker container (not this run's) is created",
            created.returncode == 0,
            created.stderr.strip()[:160] if created.returncode else name,
        ):
            return
        try:
            listed = self.docker.run(
                "ps",
                "-a",
                "--filter",
                f"label={rt._LABEL_DEADLINE}",
                "--filter",
                f"label={DECOY_LABEL}={self.token}",
                "--format",
                "{{.Names}}",
            )
            self.results.check(
                "sweep scope: the deadline-label query alone would list the decoy (the check is meaningful)",
                listed.returncode == 0 and name in listed.stdout.split(),
                f"listed={listed.stdout.split()}",
            )
            with self.env():
                settings = rt.DockerWorkerSettings.from_env()
                reaped = rt.reap_expired_containers(settings)
                rt.sweep_leftovers(settings)
            still_there = self.docker.containers_labelled(self.token, label=DECOY_LABEL)
            self.results.check(
                "sweep scope: the label-scoped sweep leaves the decoy alone",
                settings.sweep_label == f"{RUN_LABEL}={self.token}"
                and name not in reaped
                and still_there == {name},
                f"scope={settings.sweep_label} reaped={sorted(reaped)} decoy present={name in still_there}",
            )
        finally:
            removal = self.docker.run("rm", "-f", name, timeout=30)
            gone = self.docker.containers_labelled(self.token, label=DECOY_LABEL)
            self.results.check(
                "sweep scope: the decoy is removed by the run that created it",
                removal.returncode == 0 and not gone,
                "gone" if not gone else f"left {sorted(gone)}",
            )
        self.assert_clean("sweep scope")

    def scenario_stale_sweep(self) -> None:
        """Entries a killed backend leaves are swept; foreign names are not."""
        rt = self.rt
        stale_env = self.stage_root / f"env-{secrets.token_hex(16)}.env"
        stale_dir = self.stage_root / f"smk00001-{secrets.token_hex(6)}"
        foreign = self.stage_root / "not-the-runtimes"
        foreign.mkdir()
        # A name of the shape the sweep knows, old, with no container, that the
        # runtime did not create: nothing says it is ours, so it is never touched.
        lookalike = self.stage_root / f"notes-{secrets.token_hex(6)}"
        lookalike.mkdir()
        (lookalike / "keep.txt").write_text("not the runtime's\n")

        def make_stale_entries() -> None:
            # What the runtime itself writes beside every entry it creates.
            for name in (stale_env.name, stale_dir.name):
                if not (self.stage_root / f"{name}.owner").exists():
                    rt._claim_entry(self.stage_root, name)
            stale_env.write_text("SMOKE_STALE=1\n")
            stale_env.chmod(0o600)
            if not stale_dir.exists():
                (stale_dir / "trap" / "inner").mkdir(parents=True)
                (stale_dir / "trap" / "inner" / "f").write_text("x")
                (stale_dir / "trap" / "inner").chmod(0)
                (stale_dir / "trap").chmod(0)

        def tree_mode() -> int:
            return (stale_dir / "trap").stat().st_mode & 0o777

        make_stale_entries()
        with self.env():
            settings = rt.DockerWorkerSettings.from_env()
        fresh = rt.sweep_stale_staging(settings, trees_with_container=frozenset())
        self.results.check(
            "staging sweep leaves entries younger than the grace period alone",
            fresh == [] and stale_env.exists() and stale_dir.exists(),
            f"removed {fresh}",
        )
        # A real container that exists for the tree, in any state, keeps it.
        decoy = f"{CONTAINER_PREFIX}decoy-{secrets.token_hex(4)}"
        with patched(rt, "_REAP_GRACE_SECONDS", -settings.timeout_seconds):
            # The engine could not be asked: nothing proves the Worker is gone,
            # so the tree is neither chmodded nor deleted. The env file has
            # nothing to walk and goes anyway.
            unknown = rt.sweep_stale_staging(settings, trees_with_container=None)
            self.results.check(
                "staging sweep leaves a chmod 000 tree in place when it cannot tell whether a Worker still runs",
                unknown == [stale_env.name]
                and not stale_env.exists()
                and stale_dir.exists()
                and tree_mode() == 0,
                f"removed {unknown}, tree mode={oct(tree_mode())}",
            )
            make_stale_entries()
            created = self.docker.run(
                "create",
                "--name",
                decoy,
                "--label",
                f"{rt._LABEL_STAGE}={stale_dir.name}",
                "--label",
                f"{DECOY_LABEL}={self.token}",
                self.image,
                "true",
            )
            try:
                self.results.check(
                    "stale sweep: a container for the stale tree is created (it carries the tree's name)",
                    created.returncode == 0,
                    decoy if created.returncode == 0 else created.stderr.strip()[:160],
                )
                rt.sweep_leftovers(settings)
                self.results.check(
                    "staging sweep leaves the tree of a container that still exists, however old, and removes the env file",
                    stale_dir.exists() and tree_mode() == 0 and not stale_env.exists(),
                    f"tree present={stale_dir.exists()} mode={oct(tree_mode()) if stale_dir.exists() else '-'}",
                )
            finally:
                self.docker.run("rm", "-f", decoy, timeout=30)
            make_stale_entries()
            rt.sweep_leftovers(settings)
        self.results.check(
            "staging sweep removes a stale env file and a chmod 000 tree once no container exists for it, nothing else",
            foreign.exists() and not stale_env.exists() and not stale_dir.exists(),
            f"foreign entry kept={foreign.exists()}",
        )
        self.results.check(
            "staging sweep leaves an old lookalike (right shape, no ownership marker) alone, and the markers are gone with their entries",
            (lookalike / "keep.txt").is_file()
            and not list(self.stage_root.glob("*.owner")),
            f"lookalike kept={lookalike.exists()}",
        )
        foreign.rmdir()
        shutil.rmtree(lookalike)
        self.assert_clean("stale sweep")

    async def scenario_staging_roots(self) -> None:
        """Verification must judge the staging root the way a launch will use it."""
        rt = self.rt
        awkward = self.stage_root.parent / f'{self.stage_root.name}-a,b "c"'
        awkward.mkdir(mode=0o700)
        self.extra_dirs.append(awkward)
        self.published.clear()
        with self.env(AUTONOMY_CLOUD_WORKER_ROOT=str(awkward)), self.database_seams():
            os.environ["SMOKE_FORWARDED"] = self.forwarded_value
            rt.clear_verification_cache()
            ok, detail = rt.verify_docker_runtime()
            try:
                report = await rt.launch(self.ticket(), "SMOKE_MODE=ok\n")
            except Exception as exc:  # noqa: BLE001
                report = {"state": _brief(exc)}
            finally:
                os.environ.pop("SMOKE_FORWARDED", None)
        self.results.check(
            "a staging root with a comma and quotes in its path verifies and launches",
            ok and report.get("state") == "ready" and not list(awkward.iterdir()),
            detail[:100] if not ok else f"state={report.get('state')}",
        )
        # The system temp directory is not shared with the engine on macOS
        # (colima, Docker Desktop) and is on a plain Linux engine.
        outside = Path(tempfile.mkdtemp(prefix="cloud-worker-smoke-tmp-"))
        self.extra_dirs.append(outside)
        with self.env(AUTONOMY_CLOUD_WORKER_ROOT=str(outside)), self.database_seams():
            rt.clear_verification_cache()
            ok, detail = rt.verify_docker_runtime()
            error: BaseException | None = None
            if not ok:
                try:
                    await rt.launch(self.ticket(), "SMOKE_MODE=ok\n")
                except Exception as exc:  # noqa: BLE001
                    error = exc
        if ok:
            self.results.info(
                "system temp directory as staging root",
                f"the engine can bind {outside.parent}; nothing to refuse on this engine",
            )
        else:
            self.results.check(
                "a staging root the engine cannot bind fails verification, and a launch refuses before staging",
                "isolation probe failed" in detail
                and isinstance(error, rt.DockerWorkerRuntimeUnavailable)
                and not list(outside.iterdir()),
                detail[:140],
            )
        self.assert_clean("staging roots")

    async def scenario_bridge(self) -> None:
        """Report what ``--network bridge`` gives a Worker; this is the engine's property.

        A listener on the host's loopback stands in for Postgres, Redis or the
        backend. The Worker tries it by the names and addresses a VM-backed
        engine gives its containers for the host. Nothing here is judged: the
        runtime cannot filter egress, and the answer depends on the engine.
        """
        rt = self.rt
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        listener.settimeout(0.3)
        port = listener.getsockname()[1]
        stop = threading.Event()
        accepted = 0

        def serve() -> None:
            nonlocal accepted
            while not stop.is_set():
                try:
                    conn, _addr = listener.accept()
                except OSError:
                    continue
                accepted += 1
                conn.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            self.published.clear()
            error: BaseException | None = None
            report: dict[str, Any] = {}
            with (
                self.env(AUTONOMY_CLOUD_WORKER_NETWORK="bridge"),
                self.database_seams(),
            ):
                os.environ["SMOKE_FORWARDED"] = self.forwarded_value
                rt.clear_verification_cache()
                try:
                    report = await rt.launch(
                        self.ticket(), f"SMOKE_MODE=reach\nSMOKE_PORT={port}\n"
                    )
                except Exception as exc:  # noqa: BLE001
                    error = exc
                finally:
                    os.environ.pop("SMOKE_FORWARDED", None)
        finally:
            stop.set()
            thread.join(timeout=5)
            listener.close()
        text = str(report.get("text", ""))
        line = next(
            (ln for ln in text.splitlines() if ln.startswith("SMOKE_REACH")), ""
        )
        facts = dict(pair.split("=", 1) for pair in line.split()[1:] if "=" in pair)
        nets = {n for n in facts.get("nets", "").split(",") if n}
        self.results.check(
            "bridge mode: the switch changes the Worker's network namespace (an interface besides lo)",
            error is None and bool(nets - {"lo"}),
            _brief(error) if error else f"interfaces={sorted(nets)}",
        )
        reached = facts.get("reached", "")
        self.results.info(
            "bridge mode reaches a listener on the host's loopback",
            (
                f"YES via {reached} ({accepted} connection(s) accepted): a Worker with bridge "
                "egress can reach host services bound to 127.0.0.1 on this engine"
                if reached
                else f"no (gateway {facts.get('gateway') or 'unknown'}, {accepted} accepted)"
            ),
        )
        self.assert_clean("bridge")

    def _running_workers(self) -> set[str]:
        names = self.docker.containers_labelled(self.token, running_only=True)
        return {n for n in names if "-probe-" not in n}

    async def scenario_switch_off(self) -> None:
        """Default OFF: an empty switch value refuses and starts nothing."""
        before = self.owned_containers()
        error: BaseException | None = None
        with self.env(AUTONOMY_CLOUD_WORKER_RUNTIME=""), self.database_seams():
            isolation = await self.cloud_runtime.resolve_cloud_worker_isolation_async()
            try:
                await self.cloud_runtime.launch_cloud_worker(
                    self.ticket(), "SMOKE_MODE=ok\n"
                )
            except Exception as exc:  # noqa: BLE001
                error = exc
        self.results.check(
            "switch off: cloud launch is refused with the default error and no container",
            not isolation.enabled
            and isinstance(error, self.cloud_runtime.CloudWorkerLaunchError)
            and "Cloud Worker execution is disabled" in str(error)
            and self.owned_containers() == before,
            _brief(error) if error else "no error raised",
        )
        self.assert_clean("switch off")

    # -- real-image mode ---------------------------------------------------

    async def scenario_real_image(self) -> None:
        rt = self.rt
        with self.env():
            settings = rt.DockerWorkerSettings.from_env()
        flags = sorted(
            {
                a
                for a in rt.build_worker_argv(settings, model="m")
                if a.startswith("-") and len(a) > 1
            }
        )
        name = f"{CONTAINER_PREFIX}probe-smoke-{secrets.token_hex(4)}"
        argv = rt.build_container_argv(
            settings,
            name=name,
            workspace=None,
            child_argv=("codex", "exec", "--help"),
            env_vars={},
            include_stdin=False,
        )
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=60, check=False
            )
        finally:
            self.docker.run("rm", "-f", name, timeout=30)
        help_text = proc.stdout + proc.stderr
        missing = [f for f in flags if f not in help_text]
        self.results.check(
            "real CLI still accepts every flag the runtime passes to `codex exec`",
            proc.returncode == 0 and not missing,
            f"flags={flags} missing={missing}" if missing else f"flags={flags}",
        )
        version_name = f"{CONTAINER_PREFIX}probe-smoke-{secrets.token_hex(4)}"
        version = self.docker.run(
            "run",
            "--rm",
            "--name",
            version_name,
            "--label",
            f"{RUN_LABEL}={self.token}",
            self.image,
            "codex",
            "--version",
        )
        self.results.check(
            "`docker run --rm <image> codex --version` as documented",
            version.returncode == 0 and bool(version.stdout.strip()),
            version.stdout.strip()[:80],
        )
        # Under the runtime's own HOME and TMPDIR the CLI must be able to build
        # its PATH helpers; it warns on stderr when HOME sits beneath TMPDIR.
        home_name = f"{CONTAINER_PREFIX}probe-smoke-{secrets.token_hex(4)}"
        home_argv = rt.build_container_argv(
            settings,
            name=home_name,
            workspace=None,
            child_argv=("codex", "--version"),
            env_vars={},
            include_stdin=False,
        )
        try:
            home = subprocess.run(
                home_argv, capture_output=True, text=True, timeout=60, check=False
            )
        finally:
            self.docker.run("rm", "-f", home_name, timeout=30)
        self.results.check(
            "CLI runs under the runtime's HOME/TMPDIR without a PATH-alias warning",
            home.returncode == 0 and "PATH aliases" not in home.stderr,
            "no warning"
            if "PATH aliases" not in home.stderr
            else home.stderr.strip()[:160],
        )
        error: BaseException | None = None
        started = time.monotonic()
        with self.env(AUTONOMY_CLOUD_WORKER_TIMEOUT="30"), self.database_seams():
            rt.clear_verification_cache()
            try:
                await rt.launch(self.ticket(), "Reply with OK.\n")
            except Exception as exc:  # noqa: BLE001
                error = exc
        self.results.check(
            "launch without credentials or network fails closed (exit or wall-clock limit) and publishes nothing",
            error is not None and not self.published,
            f"{_brief(error, 200) if error else 'no error raised'} after {time.monotonic() - started:.1f}s",
        )
        self.assert_clean("real image, no credentials")

    # -- orchestration -----------------------------------------------------

    async def run(self) -> None:
        self.guard_sweeps()
        self.install_docker_wrapper()
        print(
            f"engine: {self.docker.run('version', '--format', '{{.Server.Version}}').stdout.strip()}"
            f"  image: {self.image}  stage root: {self.stage_root}"
        )
        print(
            "containers are tracked by the label "
            f"{RUN_LABEL}={self.token}; anything else on the engine is left alone"
        )
        self.fixture.mkdir(parents=True, exist_ok=True)
        (self.fixture / "seed.txt").write_text(self.seed_text)
        (self.fixture / "docs").mkdir(exist_ok=True)
        (self.fixture / "docs" / "notes.md").write_text("# throwaway workspace\n")

        if self.stub_mode:
            if not self.build_stub_image():
                return
        elif not self.docker.image_exists(self.image):
            self.results.check(
                "worker image is present", False, f"{self.image} not found"
            )
            return
        self.scenario_image_hygiene()
        self.scenario_verify()
        if self.stub_mode:
            self.scenario_probe_has_teeth()
            self.scenario_entrypoint_swallows_probe()
            await self.scenario_launch_ok()
            await self.scenario_launch_read_only()
            await self.scenario_launch_failures()
            await self.scenario_hostile_tree()
            await self.scenario_timeout()
            await self.scenario_cancel()
            await self.scenario_rm_failure()
            self.scenario_sweep_scope()
            self.scenario_stale_sweep()
            await self.scenario_staging_roots()
            await self.scenario_bridge()
            await self.scenario_switch_off()
        else:
            await self.scenario_real_image()
        self.results.check(
            "every expiry sweep this run triggered was scoped to its own label",
            self.refused_sweeps == 0,
            f"{self.refused_sweeps} refused",
        )

    def cleanup(self) -> list[str]:
        """Remove what this run created and nothing else; return what is not gone.

        That is the containers carrying its label, its stub images, and the
        private directories it made for itself (each removed whole). Each is
        looked at again afterwards, and one that cannot be looked at counts as
        not gone: an engine that will not answer must not read as a clean
        machine. The strings returned never carry environment values.
        """
        problems: list[str] = []
        try:
            containers_gone = self._remove_containers(problems)
            self._remove_images(problems)
            self._remove_directories(problems, containers_gone=containers_gone)
        except Exception as exc:  # noqa: BLE001 - a cleanup bug is a finding too
            problems.append(f"cleanup itself failed ({_brief(exc)})")
        return problems

    def retained(self) -> list[str]:
        """What ``--keep`` deliberately leaves behind: images and directories."""
        if not self.args.keep:
            return []
        images = (*self.extra_images, self.image) if self.stub_mode else ()
        directories = (self.stage_root, self.workdir, self.bin_dir, *self.extra_dirs)
        return [f"image {name}" for name in images] + [
            f"directory {path}" for path in directories if path is not None
        ]

    def _owned_or_decoy_containers(self) -> set[str]:
        return self.owned_containers() | self.docker.containers_labelled(
            self.token, label=DECOY_LABEL
        )

    def _remove_containers(self, problems: list[str]) -> bool:
        """Remove this run's containers; True only if none is left, verified."""
        try:
            for name in sorted(self._owned_or_decoy_containers()):
                self.docker.run("rm", "-f", name, timeout=30)
            left = self._owned_or_decoy_containers()
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            problems.append(
                "containers of this run could not be listed or removed "
                f"({_brief(exc)}); `docker ps -a --filter label={RUN_LABEL}={self.token}` "
                "shows what is left"
            )
            return False
        problems.extend(
            f"container {name} is still there after rm -f" for name in sorted(left)
        )
        return not left

    def _remove_images(self, problems: list[str]) -> None:
        if not self.stub_mode or self.args.keep:
            return
        for image in (*self.extra_images, self.image):
            try:
                self.docker.run("rmi", image, timeout=60)
                listing = self.docker.run("images", "-q", image, timeout=30)
            except (OSError, subprocess.SubprocessError) as exc:
                problems.append(
                    f"image {image} could not be removed or checked ({_brief(exc)})"
                )
                continue
            if listing.returncode != 0:
                problems.append(
                    f"image {image} could not be checked (docker images exit {listing.returncode})"
                )
            elif listing.stdout.strip():
                problems.append(f"image {image} is still there after rmi")

    def _remove_directories(
        self, problems: list[str], *, containers_gone: bool
    ) -> None:
        """Remove the run's private directories, but only once its containers are gone.

        A staging root is bind-mounted into the run's Workers, so while one may
        still be alive nothing under it is repaired or deleted: a Worker that
        is still running can rewrite the tree under the walk. The directories
        stay, named, for the operator.
        """
        if self.args.keep:
            return
        for directory in (
            self.stage_root,
            self.workdir,
            self.bin_dir,
            *self.extra_dirs,
        ):
            if directory is None:
                continue
            if not containers_gone:
                problems.append(
                    f"directory {directory} left in place: this run's containers "
                    "are not confirmed removed"
                )
                continue
            remove_tree(directory)
            try:
                left = os.path.lexists(directory)
            except OSError:
                left = True
            if left:
                problems.append(f"directory {directory} is still there after removal")


def remove_tree(path: Path) -> bool:
    """Remove a tree a Worker may have made unreadable, with the runtime's own walk.

    The same descriptor-relative walk the runtime cleans its staging trees with
    (one implementation, not a second one to keep in step), so a link a Worker
    swaps in underneath it is unlinked and never chmodded through. Call it only
    once the containers that mounted the tree are confirmed removed. True when
    nothing is left.
    """
    from src.server.services.autonomy import docker_worker_runtime as runtime

    return runtime._remove_staging_tree(path)


def _forbid_sockets() -> None:
    """The smoke talks to Docker through its CLI; it has no business on the network.

    Importing product modules must not reach a database or cache either, so a
    stray connection attempt fails loudly instead of touching a real service.
    """
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def refuse(self: socket.socket, address: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError("cloud_worker_smoke must not open network connections")
        return real_connect(self, address)

    def refuse_ex(self: socket.socket, address: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError("cloud_worker_smoke must not open network connections")
        return real_connect_ex(self, address)

    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.socket.connect_ex = refuse_ex  # type: ignore[method-assign]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--image",
        help="check an existing Worker image (verification, CLI flags, credential-less "
        "launch) instead of building the throwaway stub image",
    )
    parser.add_argument(
        "--stub-base",
        default="ubuntu:24.04",
        help="local base image for the stub (default: %(default)s; never pulled by this script "
        "when already present)",
    )
    parser.add_argument("--docker-bin", default="docker")
    parser.add_argument(
        "--workdir",
        type=Path,
        help="an existing directory to create the run's private scratch directory in "
        "(fixture workspace, stub build context); default: the system temp directory",
    )
    parser.add_argument(
        "--stage-parent",
        type=Path,
        help="an existing directory the engine can bind mount, to create the run's "
        "private staging root in; default: ~/.cache/conductor, which must already "
        "exist (it is never created)",
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the stub image and directories"
    )
    return parser.parse_args(argv)


def _private_directory(parent: Path | None, *, hint: str = "") -> Path:
    """A new 0700 directory for this run; ``parent`` must already exist.

    No parent is ever created: the run removes what it made and nothing else,
    so a parent it made would stay behind. ``hint`` says what to do instead.
    """
    if parent is not None and not parent.is_dir():
        raise SystemExit(f"{parent} is not an existing directory{hint}")
    return Path(tempfile.mkdtemp(prefix="cloud-worker-smoke-", dir=parent)).resolve()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.ERROR)
    _forbid_sockets()
    # SIGTERM and SIGHUP end the run the way Ctrl-C does, through the cleanup
    # below. SIGKILL cannot be caught; see the module docstring.
    for name in ("SIGTERM", "SIGHUP"):
        signal.signal(getattr(signal, name), signal.default_int_handler)
    private: list[Path] = []
    try:
        args.workdir = _private_directory(args.workdir)
        private.append(args.workdir)
        stage_parent = args.stage_parent
        args.stage_root = _private_directory(
            stage_parent or Path.home() / ".cache" / "conductor",
            hint=(
                "; create it, or pass --stage-parent DIR (an existing directory the "
                "Docker engine can bind mount)"
                if stage_parent is None
                else ""
            ),
        )
        private.append(args.stage_root)
        smoke = Smoke(args)
    except BaseException:
        for directory in private:
            remove_tree(directory)
        raise
    started = time.monotonic()
    try:
        asyncio.run(smoke.run())
    except BaseException as exc:  # noqa: BLE001 - report, then still clean up
        smoke.results.check("smoke ran to completion", False, _brief(exc))
    finally:
        # A second signal must not abandon the cleanup half way.
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            signal.signal(getattr(signal, name), signal.SIG_IGN)
        problems = smoke.cleanup()
    if problems:
        for problem in problems:
            smoke.results.check(f"cleanup: {problem}", False)
    elif smoke.args.keep:
        # --keep retains images and directories on purpose: say what was
        # verified (the containers) and list the rest as kept, never as gone.
        smoke.results.check("cleanup: this run's containers are gone (verified)", True)
        smoke.results.info("kept by --keep", "; ".join(smoke.retained()))
    else:
        smoke.results.check(
            "cleanup: every container, image and directory of this run is gone (verified)",
            True,
        )
    failed = smoke.results.failed
    print(
        f"{len(smoke.results.rows) - len(failed)} passed, {len(failed)} failed, "
        f"{len(smoke.results.notes)} informational, in {time.monotonic() - started:.0f}s; "
        f"cleanup problems: {len(problems)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
