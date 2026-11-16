"""OpenAI Codex CLI adapter (``codex exec`` / ``codex exec resume``).

Codex's JSONL surface reports ``thread.started`` with the resumable thread id
and ``turn.completed.usage`` with ``cached_input_tokens``. Resuming the same
thread id is the cache lever, exactly as ``--session-id`` is for DSH.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from src.llms.native_cli.base import (
    CliInvocation,
    NativeCliError,
    NativeCliNoFinalAnswer,
    NativeCliResult,
    NativeCliUnavailable,
    NativeCliUsage,
)
from src.llms.native_cli.failure import FailureClass, register_failure_rule
from src.llms.native_cli.personal_memory import (
    CODEX_MEMORY_OFF_ARGS,
    isolated_codex_home_parent,
    prepare_isolated_codex_home,
    remove_isolated_codex_home,
    source_codex_home,
)
from src.llms.native_cli.process import _kill_tree, run_process
from src.llms.native_cli.review_scratch import current_review_scratch, paths_overlap


# Opt-in for ``--output-schema`` on ``codex exec``. Unset stays on today's argv.
CODEX_OUTPUT_SCHEMA_ENV = "CONDUCTOR_CODEX_OUTPUT_SCHEMA"
# Help is local and instant. A hung or missing binary must not stall the turn.
_OUTPUT_SCHEMA_PROBE_TIMEOUT_SECONDS = 2.0
# A descendant holding inherited pipes must not extend failure cleanup.
_OUTPUT_SCHEMA_REAP_TIMEOUT_SECONDS = 0.2
# Oversized last-message files are rejected so stdout remains the fallback.
_FINAL_MESSAGE_LIMIT = 64 * 1024
# Keyed by the resolved executable, not the raw argv string. ``./codex`` in
# two workspaces is two binaries; a False result is cached too so a missing
# CLI does not repeat the timeout on every turn.
_OUTPUT_SCHEMA_SUPPORT: dict[str, bool] = {}
# argv[0] -> cwd of the invocation that will exec it. A help probe handed
# only that argv string still has to run in this directory, or a relative
# path is a different file from the one execution runs.
_EXECUTABLE_CWD: dict[str, str | None] = {}
_OMITTED: object = object()
# Output path -> private schema copy. ``_attach_output_schema`` stays a
# 2-tuple so callers unpack ``(invocation, output_path)``; the copy is
# deleted with the output file.
_SCHEMA_COPIES: dict[str, str] = {}


def _output_schema_opt_in() -> bool:
    value = os.environ.get(CODEX_OUTPUT_SCHEMA_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _probe_pipe_identities(process: subprocess.Popen[str]) -> set[int]:
    """Inode or kernel handle of each pipe end this probe still has open.

    On Linux both ends share one inode. On macOS ``st_ino`` is the
    ``pipe_handle`` of this end; the descendant's end carries it as
    ``pipe_peerhandle``.
    """
    identities: set[int] = set()
    for stream in (process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            info = os.fstat(stream.fileno())
        except (OSError, ValueError):
            continue
        if stat.S_ISFIFO(info.st_mode) and info.st_ino:
            identities.add(info.st_ino)
    return identities


def _kill_holder(pid: int) -> None:
    """SIGKILL one process that holds a probe pipe, and its session if it leads it."""
    if pid <= 1 or pid == os.getpid():
        return
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return
    # Only a group leader's id is killed as a group. A non-leader stays a
    # single pid so a matched fd cannot take down some other session.
    if pgid > 1 and pgid == pid and pgid != os.getpgrp():
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _linux_pipe_holders(identities: set[int]) -> set[int]:
    wanted = {f"pipe:[{ino}]" for ino in identities}
    found: set[int] = set()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit():
            continue
        fd_dir = f"/proc/{entry}/fd"
        try:
            names = os.listdir(fd_dir)
        except OSError:
            continue
        for name in names:
            try:
                target = os.readlink(f"{fd_dir}/{name}")
            except OSError:
                continue
            if target in wanted:
                found.add(int(entry))
                break
    return found


def _darwin_pipe_holders(identities: set[int]) -> set[int]:
    """Pids with a pipe fd whose handle or peer handle is one of ours.

    ``proc_pidfdinfo`` must return exactly the ``pipe_fdinfo`` size this
    layout expects. A mismatch means the private struct moved, and guessing
    would target the wrong pid.
    """
    try:
        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    except OSError:
        return set()

    class _VinfoStat(ctypes.Structure):
        _fields_ = [
            ("vst_dev", ctypes.c_uint32),
            ("vst_mode", ctypes.c_uint16),
            ("vst_nlink", ctypes.c_uint16),
            ("vst_ino", ctypes.c_uint64),
            ("vst_uid", ctypes.c_uint32),
            ("vst_gid", ctypes.c_uint32),
            ("vst_atime", ctypes.c_int64),
            ("vst_atimensec", ctypes.c_int64),
            ("vst_mtime", ctypes.c_int64),
            ("vst_mtimensec", ctypes.c_int64),
            ("vst_ctime", ctypes.c_int64),
            ("vst_ctimensec", ctypes.c_int64),
            ("vst_birthtime", ctypes.c_int64),
            ("vst_birthtimensec", ctypes.c_int64),
            ("vst_size", ctypes.c_int64),
            ("vst_blocks", ctypes.c_int64),
            ("vst_blksize", ctypes.c_int32),
            ("vst_flags", ctypes.c_uint32),
            ("vst_gen", ctypes.c_uint32),
            ("vst_rdev", ctypes.c_uint32),
            ("vst_qspare", ctypes.c_int64 * 2),
        ]

    class _ProcFileInfo(ctypes.Structure):
        _fields_ = [
            ("fi_openflags", ctypes.c_uint32),
            ("fi_status", ctypes.c_uint32),
            ("fi_offset", ctypes.c_int64),
            ("fi_type", ctypes.c_int32),
            ("fi_guardflags", ctypes.c_uint32),
        ]

    class _PipeInfo(ctypes.Structure):
        _fields_ = [
            ("pipe_stat", _VinfoStat),
            ("pipe_handle", ctypes.c_uint64),
            ("pipe_peerhandle", ctypes.c_uint64),
            ("pipe_status", ctypes.c_int),
            ("rfu_1", ctypes.c_int),
        ]

    class _PipeFdInfo(ctypes.Structure):
        _fields_ = [("pfi", _ProcFileInfo), ("pipeinfo", _PipeInfo)]

    class _ProcFdInfo(ctypes.Structure):
        _fields_ = [
            ("proc_fd", ctypes.c_int32),
            ("proc_fdtype", ctypes.c_uint32),
        ]

    lib.proc_listpids.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    lib.proc_listpids.restype = ctypes.c_int
    lib.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    lib.proc_pidinfo.restype = ctypes.c_int
    lib.proc_pidfdinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    lib.proc_pidfdinfo.restype = ctypes.c_int

    # A full buffer is indistinguishable from a short one: libproc returns the
    # byte count it wrote, not the count it still needs. Grow until the result
    # no longer fills the buffer so a late pipe fd is not skipped.
    slots = 256
    pid_buf = (ctypes.c_int * slots)()
    written = lib.proc_listpids(1, 0, pid_buf, ctypes.sizeof(pid_buf))
    while written == ctypes.sizeof(pid_buf) and slots < 65536:
        slots *= 2
        pid_buf = (ctypes.c_int * slots)()
        written = lib.proc_listpids(1, 0, pid_buf, ctypes.sizeof(pid_buf))
    if written <= 0:
        return set()
    count = min(written, ctypes.sizeof(pid_buf)) // ctypes.sizeof(ctypes.c_int)
    pids = [int(pid_buf[index]) for index in range(count) if pid_buf[index] > 0]

    found: set[int] = set()
    info = _PipeFdInfo()
    info_size = ctypes.sizeof(info)
    me = os.getpid()
    fd_item = ctypes.sizeof(_ProcFdInfo)
    for pid in pids:
        if pid == me:
            continue
        fd_cap = 256
        fd_buf = (_ProcFdInfo * fd_cap)()
        nbytes = lib.proc_pidinfo(pid, 1, 0, fd_buf, ctypes.sizeof(fd_buf))
        while nbytes == ctypes.sizeof(fd_buf) and fd_cap < 8192:
            fd_cap *= 2
            fd_buf = (_ProcFdInfo * fd_cap)()
            nbytes = lib.proc_pidinfo(pid, 1, 0, fd_buf, ctypes.sizeof(fd_buf))
        if nbytes <= 0:
            continue
        count = min(nbytes, ctypes.sizeof(fd_buf)) // fd_item
        for index in range(count):
            if fd_buf[index].proc_fdtype != 6:  # PROX_FDTYPE_PIPE
                continue
            got = lib.proc_pidfdinfo(
                pid, int(fd_buf[index].proc_fd), 6, ctypes.byref(info), info_size
            )
            if got != info_size:
                continue
            handle = int(info.pipeinfo.pipe_handle)
            peer = int(info.pipeinfo.pipe_peerhandle)
            if handle in identities or peer in identities:
                found.add(pid)
                break
    return found


def _pipe_holder_pids(identities: set[int]) -> set[int]:
    if not identities or os.name != "posix":
        return set()
    if sys.platform == "darwin":
        return _darwin_pipe_holders(identities)
    if sys.platform == "linux" or os.path.isdir("/proc"):
        return _linux_pipe_holders(identities)
    return set()


def _stop_reparented_pipe_holders(process: subprocess.Popen[str]) -> None:
    """Kill setsid descendants that outlived the leader and still hold its pipes.

    ``_kill_tree`` walks parent pids. Once the leader has exited, a child
    that called ``setsid`` is reparented and that walk finds nothing, and
    the leader's process group does not include it either. ``communicate``
    only reached its timeout because that child still has the write end, so
    the open pipe is the record of who leaked.
    """
    for pid in _pipe_holder_pids(_probe_pipe_identities(process)):
        _kill_holder(pid)


def _is_direct_path(binary: str) -> bool:
    """True when exec treats ``binary`` as a file path, not a PATH search."""
    return os.path.isabs(binary) or "/" in binary


def _effective_child_cwd(cwd: str | None) -> str | None:
    """Directory the child is started in, the same one ``prepare_process`` uses."""
    from src.llms.native_cli.isolation import current_worker_isolation

    isolation = current_worker_isolation()
    if isolation is not None:
        return os.fspath(isolation.workdir)
    return cwd


def _child_command_path(cwd: str | None) -> str:
    """PATH the child searches, relative entries anchored at its cwd.

    ``PATH=bin:...`` is ``<cwd>/bin`` for that child, not the daemon's ``bin``.
    An isolated child does not inherit the daemon PATH; it gets the sandbox
    PATH ``prepare_process`` installs, which is already absolute.
    """
    base = _effective_child_cwd(cwd) or os.getcwd()
    from src.llms.native_cli.isolation import current_worker_isolation

    isolation = current_worker_isolation()
    if isolation is not None:
        # Same value as ``isolation._minimal_env``. Relative daemon entries
        # are not on the sandboxed PATH.
        raw = "/usr/bin:/bin:/usr/sbin:/sbin"
        runtime_dirs = sorted(
            {
                str(path if path.is_dir() else path.parent)
                for path in isolation.runtime_read_paths
            }
        )
        if runtime_dirs:
            raw = os.pathsep.join((raw, *runtime_dirs))
    else:
        raw = os.environ.get("PATH")
        if raw is None:
            try:
                default = os.confstr("CS_PATH")
            except (AttributeError, ValueError, OSError):
                default = os.defpath
            # execvp searches the child cwd before the system default.
            raw = os.pathsep + default
    anchored: list[str] = []
    for entry in raw.split(os.pathsep):
        if entry and os.path.isabs(entry):
            anchored.append(entry)
        else:
            # An empty PATH component is the child cwd.
            anchored.append(os.path.join(base, entry) if entry else base)
    return os.pathsep.join(anchored)


def _executable_identity(binary: str, cwd: str | None) -> str:
    """Absolute path of the file execution will run for this argv and cwd.

    Direct paths are joined to the child cwd. Bare names are resolved on the
    child's PATH, so ``PATH=bin`` in two workspaces is two files. The raw
    string ``codex`` or ``./codex`` is not an identity: a cache hit on it
    would apply one binary's help result to the other.
    """
    base = Path(_effective_child_cwd(cwd) or os.getcwd())
    if _is_direct_path(binary):
        candidate = Path(binary)
        if not candidate.is_absolute():
            candidate = base / candidate
    else:
        found = shutil.which(binary, path=_child_command_path(cwd))
        if not found:
            candidate = base / binary
        else:
            candidate = Path(found)
            # ``shutil.which`` returns a relative PATH hit unchanged. Join it
            # here so the cache key is the child's file, not the daemon's.
            if not candidate.is_absolute():
                candidate = base / candidate
    try:
        return os.fspath(candidate.resolve(strict=False))
    except OSError:
        return os.fspath(candidate)


def _probe_output_schema(binary: str, cwd: str | None | object = _OMITTED) -> bool:
    """True only when ``codex exec --help`` actually lists ``--output-schema``.

    The help child uses the same isolation boundary and the same cwd as the
    turn, so a relative ``argv[0]`` is the executable execution will run and
    an opted-in worker does not inherit unapproved host credentials. Any
    failure (missing binary, non-zero help, timeout, unreadable output, or a
    boundary that refuses the probe) is unsupported. A failed or timed-out
    probe kills its process tree and any reparented descendant still holding
    the probe pipes, closes those pipes, and bounds leader reaping.
    """
    if cwd is _OMITTED:
        cwd = _EXECUTABLE_CWD.get(binary)
    child_cwd = cwd if isinstance(cwd, str) or cwd is None else None
    try:
        from src.llms.native_cli.isolation import prepare_process

        argv, env, launched_cwd = prepare_process(
            (binary, "exec", "--help"), child_cwd, None
        )
        process = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=launched_cwd,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(
                timeout=_OUTPUT_SCHEMA_PROBE_TIMEOUT_SECONDS
            )
        except BaseException:
            try:
                try:
                    _stop_reparented_pipe_holders(process)
                finally:
                    _kill_tree(process)
            finally:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
                process.wait(timeout=_OUTPUT_SCHEMA_REAP_TIMEOUT_SECONDS)
            raise
    except Exception:
        return False
    if process.returncode != 0:
        return False
    help_text = f"{stdout or ''}\n{stderr or ''}"
    return "--output-schema" in help_text


def _binary_supports_output_schema(binary: str, cwd: str | None) -> bool:
    identity = _executable_identity(binary, cwd)
    cached = _OUTPUT_SCHEMA_SUPPORT.get(identity)
    if cached is not None:
        return cached
    supported = _probe_output_schema(binary, cwd)
    _OUTPUT_SCHEMA_SUPPORT[identity] = supported
    return supported


def _review_schema_path() -> str | None:
    """Schema path published by the review-schema line, or None.

    That line owns ``review_schema.py`` and ``verdict.schema.json`` and
    exports ``VERDICT_SCHEMA_PATH``. This adapter does not ship a second
    copy. A missing module or file leaves the exec argv unchanged.
    """
    try:
        from src.tools.auto_dispatch.review_schema import VERDICT_SCHEMA_PATH
    except ImportError:
        return None
    path = os.fspath(VERDICT_SCHEMA_PATH).strip()
    if not path or not os.path.isfile(path):
        return None
    return path


def _unlink_quiet(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _unlink_at_quiet(directory: int, path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(Path(path).name, dir_fd=directory)
    except OSError:
        pass


def _open_isolated_tmp(root: Path, *, create: bool = False) -> int | None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        anchor = os.open(root, flags)
    except OSError:
        return None
    try:
        if create:
            try:
                os.mkdir("tmp", mode=0o700, dir_fd=anchor)
            except FileExistsError:
                pass
            except OSError:
                return None
        try:
            return os.open("tmp", flags, dir_fd=anchor)
        except OSError:
            return None
    finally:
        os.close(anchor)


def _discard_attached_output(
    attach: asyncio.Future[tuple[CliInvocation, str | None]],
) -> None:
    """Discard the files a schema attach made after its run was cancelled."""
    if attach.cancelled() or attach.exception() is not None:
        return
    _discard_schema_output(attach.result()[1])


def _discard_schema_output(output_path: str | None) -> None:
    if output_path is None:
        return
    schema_copy = _SCHEMA_COPIES.pop(output_path, None)
    from src.llms.native_cli.isolation import current_worker_isolation

    isolation = current_worker_isolation()
    if isolation is not None:
        directory = isolation.root / "tmp"
        paths = (output_path, schema_copy)
        if all(path is None or Path(path).parent == directory for path in paths):
            descriptor = _open_isolated_tmp(isolation.root)
            if descriptor is None:
                return
            try:
                for path in paths:
                    _unlink_at_quiet(descriptor, path)
            finally:
                os.close(descriptor)
            return
    _unlink_quiet(output_path)
    _unlink_quiet(schema_copy)


def _with_schema_flags(
    invocation: CliInvocation,
    argv: tuple[str, ...],
    schema_path: str,
    output_path: str,
) -> tuple[CliInvocation, str]:
    return (
        replace(
            invocation,
            argv=(*argv, "--output-schema", schema_path, "-o", output_path),
        ),
        output_path,
    )


def _isolated_schema_paths(root: Path) -> tuple[Path, str, str]:
    directory = root / "tmp"
    token = f"{os.getpid()}-{time.time_ns()}"
    output_path = str(directory / f"conductor-codex-last-{token}.txt")
    schema_dest = str(directory / f"codex-output-schema-{token}.json")
    return directory, output_path, schema_dest


def _copy_schema_to_isolated_tmp(
    schema_path: str, schema_dest: str, directory: int
) -> bool:
    created = False
    source = None
    try:
        source = os.open(schema_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        if not stat.S_ISREG(os.fstat(source).st_mode):
            return False
        destination = os.open(
            Path(schema_dest).name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory,
        )
        created = True
        try:
            os.fchmod(destination, 0o600)
            while data := os.read(source, 64 * 1024):
                view = memoryview(data)
                while view:
                    view = view[os.write(destination, view) :]
        finally:
            os.close(destination)
    except OSError:
        if created:
            _unlink_at_quiet(directory, schema_dest)
        return False
    finally:
        if source is not None:
            os.close(source)
    return True


def _attach_isolated_output_schema(
    invocation: CliInvocation,
    argv: tuple[str, ...],
    schema_path: str,
    root: Path,
) -> tuple[CliInvocation, str | None]:
    """Point ``--output-schema`` and ``-o`` at the worker's private tmp.

    All host mutations are relative to descriptors opened with ``O_NOFOLLOW``.
    A worker replacing ``tmp`` with a symlink therefore cannot redirect the
    schema copy, output creation, or permission changes outside the worker.
    """
    _directory, output_path, schema_dest = _isolated_schema_paths(root)
    directory = _open_isolated_tmp(root, create=True)
    if directory is None:
        return invocation, None
    try:
        try:
            os.fchmod(directory, 0o700)
        except OSError:
            return invocation, None
        try:
            descriptor = os.open(
                Path(output_path).name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory,
            )
            os.close(descriptor)
        except OSError:
            return invocation, None
        if not os.path.isfile(schema_path):
            _unlink_at_quiet(directory, output_path)
            return invocation, None
        if not _copy_schema_to_isolated_tmp(schema_path, schema_dest, directory):
            _unlink_at_quiet(directory, output_path)
            return invocation, None
        _SCHEMA_COPIES[output_path] = schema_dest
        return _with_schema_flags(invocation, argv, schema_dest, output_path)
    finally:
        os.close(directory)


def _attach_output_schema(
    invocation: CliInvocation,
) -> tuple[CliInvocation, str | None]:
    """Append schema flags, or return the invocation unchanged.

    The returned path is where Codex writes the final message (``-o``). The
    caller reads it and deletes it. Unsupported binaries and a missing opt-in
    leave ``argv`` byte-identical to the invocation ``build_invocation``
    returned. Without worker isolation the schema path stays the repo file
    and the output file is a host temp file.
    """
    if not _output_schema_opt_in():
        return invocation, None
    argv = invocation.argv
    if not argv or not _binary_supports_output_schema(argv[0], invocation.cwd):
        return invocation, None
    schema_path = _review_schema_path()
    if schema_path is None:
        return invocation, None
    from src.llms.native_cli.isolation import current_worker_isolation

    isolation = current_worker_isolation()
    if isolation is not None:
        return _attach_isolated_output_schema(
            invocation, argv, schema_path, isolation.root
        )
    try:
        descriptor, output_path = tempfile.mkstemp(
            prefix="conductor-codex-last-", suffix=".txt"
        )
        os.close(descriptor)
    except OSError:
        return invocation, None
    return _with_schema_flags(invocation, argv, schema_path, output_path)


def _read_final_message(
    path: str, overrides: Mapping[str, str] | None = None
) -> str:
    """Read a regular last-message file, rejecting links and oversized output.

    Isolated output is opened relative to the worker's anchored tmp directory,
    so neither the file nor one of its ancestors can redirect the host read.
    Oversized output is rejected whole; the caller retains its stdout answer.

    Process stdout is already redacted before the adapter sees it. This file
    replaces that text, so the same credential redaction runs before the
    return. An empty file stays empty and is not passed through redaction.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    directory = None
    try:
        from src.llms.native_cli.isolation import current_worker_isolation

        isolation = current_worker_isolation()
        if isolation is not None and Path(path).parent == isolation.root / "tmp":
            directory = _open_isolated_tmp(isolation.root)
            if directory is None:
                return ""
            descriptor = os.open(Path(path).name, flags, dir_fd=directory)
        else:
            descriptor = os.open(path, flags)
    except OSError:
        return ""
    finally:
        if directory is not None:
            os.close(directory)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _FINAL_MESSAGE_LIMIT:
            return ""
        data = os.read(descriptor, _FINAL_MESSAGE_LIMIT + 1)
        if len(data) > _FINAL_MESSAGE_LIMIT:
            return ""
    except OSError:
        return ""
    finally:
        os.close(descriptor)
    text = data.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    from src.llms.native_cli.isolation import redact_diagnostic

    return redact_diagnostic(text, overrides)


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _auth_store_override() -> str | None:
    """Read only the documented credential-store enum for this CLI's HOME."""
    from pathlib import Path

    from src.llms.native_cli.isolation import approved_env_value, private_home

    configured_home = approved_env_value("CODEX_HOME")
    codex_home = (
        Path(configured_home).expanduser()
        if configured_home
        else private_home() / ".codex"
    )
    config = codex_home / "config.toml"
    if not config.is_file():
        return None
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        raise NativeCliUnavailable(
            "Codex config could not be read for Ticket authentication isolation"
        ) from None
    value = data.get("cli_auth_credentials_store")
    if value is None:
        return None
    if not isinstance(value, str) or value not in {
        "file",
        "keyring",
        "auto",
        "ephemeral",
    }:
        raise NativeCliUnavailable(
            "Codex credential-store setting is unsupported for Ticket isolation"
        )
    return str(value)


def parse_codex_events(stdout: str) -> tuple[str, str | None, NativeCliUsage, list[Mapping[str, Any]]]:
    """Parse Codex JSONL: final agent messages, thread id and token usage."""
    texts: list[str] = []
    session_id: str | None = None
    usage = NativeCliUsage()
    events: list[Mapping[str, Any]] = []
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
        kind = event.get("type")
        if kind == "thread.started":
            session_id = str(event.get("thread_id") or "") or None
        elif kind == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = str(item.get("text") or "").strip()
                if text:
                    texts.append(text)
        elif kind == "turn.completed":
            usage = NativeCliUsage.from_mapping(event.get("usage"))
    if not events:
        raise NativeCliError("codex returned no machine-readable events")
    return "\n".join(texts), session_id, usage, events


def codex_turn_completed(events: Sequence[Mapping[str, Any]]) -> bool:
    """Whether the Codex turn completed without a turn or stream error.

    A turn without an agent message is only a missing report when the turn
    itself succeeded; a usage limit or a failed turn stays a CLI failure.
    """
    kinds = {event.get("type") for event in events}
    return "turn.completed" in kinds and not kinds & {"turn.failed", "error"}


def no_final_answer_error(
    message: str, events: Sequence[Mapping[str, Any]]
) -> NativeCliError:
    """The error for a Codex turn without a final message, keeping its wording."""
    if codex_turn_completed(events):
        return NativeCliNoFinalAnswer(message)
    return NativeCliError(message)


def codex_final_agent_message(events: Sequence[Mapping[str, Any]]) -> str:
    """The last completed agent message of a Codex turn, or empty.

    ``parse_codex_events`` joins every agent message, progress updates
    included; a completion report is only the final one.
    """
    final = ""
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if isinstance(item, Mapping) and item.get("type") == "agent_message":
            text = str(item.get("text") or "").strip()
            if text:
                final = text
    return final


_OUTPUT_TOOL_ITEM_TYPES = frozenset(
    {
        "command_execution",
        "mcp_tool_call",
        "function_call",
        "collab_tool_call",
        "tool_call",
    }
)


# Record types that carry an ``item``; everything else is metadata by design.
_OUTPUT_ITEM_RECORDS = frozenset({"item.started", "item.updated", "item.completed"})


def parse_codex_output_events(
    line: str, *, on_malformed: Callable[[], None] | None = None
) -> list[dict[str, Any]]:
    """Visible live output from one JSONL line, for a daemon's live channel.

    Only what a person may see: completed agent-message text and tool *names*.
    Tool arguments, tool results and the prompt never appear here, so a
    daemon's best-effort stream cannot leak them. Codex emits completed items
    rather than token deltas, so this is line-incremental progress: tool items
    stream as they start and each completed message arrives whole. The caller
    bounds/chunks the text.

    Two different things yield nothing. A well-formed record of another type
    (thread/turn metadata, an error record) is filtered on purpose. A record
    that is *damaged* is not: a non-blank line that is not a JSON object, a
    record without a string ``type``, or an ``item.*`` record whose ``item`` or
    the fields this function reads have the wrong shape, is corruption, and is
    reported through ``on_malformed`` so the live channel stops text instead of
    skipping what the CLI said quietly.
    """

    def malformed() -> list[dict[str, Any]]:
        if on_malformed is not None:
            on_malformed()
        return []

    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return malformed() if line.strip() else []
    if not isinstance(event, dict):
        return malformed()
    kind = event.get("type")
    if not isinstance(kind, str) or not kind:
        return malformed()
    if kind not in _OUTPUT_ITEM_RECORDS:
        return []
    item = event.get("item")
    if not isinstance(item, dict):
        return malformed()
    item_type = item.get("type")
    if not isinstance(item_type, str):
        return malformed()
    if kind == "item.completed" and item_type == "agent_message":
        text = item.get("text")
        if not isinstance(text, str):
            return malformed()
        return [{"kind": "text", "text": text}] if text else []
    if kind == "item.started" and item_type in _OUTPUT_TOOL_ITEM_TYPES:
        names = [item.get("tool"), item.get("name")]
        if any(value is not None and not isinstance(value, str) for value in names):
            return malformed()
        name = str(names[0] or names[1] or item_type or "tool").strip()[:64]
        return [{"kind": "tool", "tools": [name]}] if name else []
    return []


class CodexExecAdapter:
    provider = "codex"

    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout_seconds: float = 300.0,
        cwd: str | None = None,
        model: str | None = None,
        read_only: bool = True,
        mcp_servers: list[dict[str, Any]] | None = None,
        approve_for_me: bool = False,
        outer_sandboxed: bool = False,
        isolate_user_instructions: bool = False,
        isolate_personal_memory: bool = False,
    ) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds
        self._cwd = cwd
        self._model = model
        self._read_only = read_only
        self._mcp_servers = list(mcp_servers or ())
        self._approve_for_me = approve_for_me
        # When the Conductor daemon already runs Codex inside its own OS
        # sandbox (worker_isolation), Codex's nested sandbox-exec is refused
        # by macOS ("sandbox_apply: Operation not permitted"). The outer
        # isolation is the enforcement boundary, so Codex's own is disabled.
        self._outer_sandboxed = outer_sandboxed
        self._isolate_user_instructions = isolate_user_instructions
        # Reviewers and Ticket workers run without the operator's personal
        # memory, global AGENTS.md or config MCP servers (personal_memory.py).
        self._isolate_personal_memory = (
            isolate_personal_memory or isolate_user_instructions
        )

    @property
    def binary(self) -> str:
        explicit = self._binary or os.environ.get("NATIVE_CLI_CODEX_BIN")
        if explicit:
            return explicit
        found = shutil.which("codex")
        if not found:
            raise NativeCliUnavailable(
                "codex CLI not found; install @openai/codex or set NATIVE_CLI_CODEX_BIN"
            )
        return found

    def _review_scratch_root(
        self, session_id: str | None, cwd: str | None
    ) -> str | None:
        """The current review scratch root, when this run may write only there.

        Only a fresh read-only turn qualifies; a resume keeps its original
        policy, and an outer sandbox or approve-for-me picks its own. A scratch
        that overlaps the repository is ignored so the repo is never writable.
        """
        scratch = current_review_scratch()
        if (
            scratch is None
            or not self._read_only
            or self._outer_sandboxed
            or self._approve_for_me
            or session_id is not None
        ):
            return None
        repo = cwd if cwd is not None else self._cwd
        for other in (scratch.repo, repo):
            if other and paths_overlap(scratch.root, other):
                return None
        return str(scratch.root)

    def build_invocation(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> CliInvocation:
        argv = [self.binary, "exec"]
        if self._isolate_user_instructions:
            # Keep CODEX_HOME available for ChatGPT login, but exclude its
            # user config and automatic AGENTS.md discovery. The Ticket prompt
            # asks the Worker to read repository AGENTS.md explicitly.
            argv.extend(
                [
                    "--ignore-user-config",
                    "--config",
                    "project_doc_max_bytes=0",
                ]
            )
            auth_store = _auth_store_override()
            if auth_store is not None:
                argv.extend(
                    [
                        "--config",
                        f"cli_auth_credentials_store={_toml_string(auth_store)}",
                    ]
                )
        if self._isolate_personal_memory:
            argv.extend(CODEX_MEMORY_OFF_ARGS)
        if session_id:
            argv.extend(["resume", session_id])
        argv.extend(["--json", "--skip-git-repo-check"])
        child_env: dict[str, str] = {}
        for server in self._mcp_servers:
            name = str(server.get("name") or "")
            url = str(server.get("url") or "").strip()
            if name and url:
                headers = {
                    str(k).lower(): str(v)
                    for k, v in (server.get("headers") or {}).items()
                }
                auth = headers.get("authorization", "")
                argv.extend(["-c", f"mcp_servers.{name}.url={_toml_string(url)}"])
                if auth.lower().startswith("bearer ") and len(headers) == 1:
                    token = auth.split(None, 1)[1].strip()
                    env_name = (
                        "CONDUCTOR_MCP_"
                        + "".join(ch if ch.isalnum() else "_" for ch in name).upper()
                        + "_TOKEN"
                    )
                    child_env[env_name] = token
                    argv.extend(
                        [
                            "-c",
                            f"mcp_servers.{name}.bearer_token_env_var={_toml_string(env_name)}",
                        ]
                    )
                elif headers:
                    # Codex can only carry a bearer token, not arbitrary headers.
                    argv = argv[: -2]
                continue
            command = str(server.get("command") or "")
            args = [str(arg) for arg in (server.get("args") or [])]
            if not name or not command:
                continue
            argv.extend(["-c", f"mcp_servers.{name}.command={_toml_string(command)}"])
            cells = ", ".join(_toml_string(arg) for arg in args)
            argv.extend(["-c", f"mcp_servers.{name}.args=[{cells}]"])
            env_map = {
                str(k): str(v) for k, v in (server.get("env") or {}).items()
            }
            if env_map:
                # Pass capability values through the child environment and
                # name them in Codex's env_vars allowlist; the secret itself is
                # not in argv.
                child_env.update(env_map)
                names = ", ".join(_toml_string(k) for k in env_map)
                argv.extend(
                    ["-c", f"mcp_servers.{name}.env_vars=[{names}]"]
                )
        scratch_root = self._review_scratch_root(session_id, cwd)
        if scratch_root is not None:
            # A cross-review with a private scratch: workspace-write makes only
            # the ``-C`` dir (the scratch, outside the repo) writable, and the
            # knobs keep /tmp, $TMPDIR and the network out of the policy. The
            # repository stays readable but not writable.
            argv.extend(
                [
                    "--sandbox",
                    "workspace-write",
                    "-c",
                    "sandbox_workspace_write.network_access=false",
                    "-c",
                    "sandbox_workspace_write.exclude_slash_tmp=true",
                    "-c",
                    "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                    # Pinned so the user's Codex config cannot widen the
                    # reviewer's sandbox or let it ask for an escalation.
                    "-c",
                    "sandbox_workspace_write.writable_roots=[]",
                    "-c",
                    'approval_policy="never"',
                ]
            )
        elif session_id is None:
            # A resumed session keeps the original policy. The standalone
            # NativeAgent tool stays read-only; a native *thread* may opt
            # into workspace-write because it owns the workspace mirror.
            if self._outer_sandboxed:
                argv.extend(["--sandbox", "danger-full-access"])
            elif self._approve_for_me:
                # Automatic review already selects workspace-write; passing an
                # explicit --sandbox as well is refused by Codex as a conflict.
                argv.append("--approve-for-me")
            else:
                argv.extend(
                    ["--sandbox", "read-only" if self._read_only else "workspace-write"]
                )
        if self._model:
            argv.extend(["--model", self._model])
        # ``-C`` is an ``exec`` option, not a ``resume`` option. A resumed
        # session keeps its original cwd; the process cwd below is also the
        # same stable directory, so resume never needs the flag.
        if scratch_root is not None:
            argv.extend(["-C", scratch_root])
        elif session_id is None:
            if cwd:
                argv.extend(["-C", cwd])
            elif self._cwd:
                argv.extend(["-C", self._cwd])
        # ``-`` reads the prompt from stdin; avoids ARG_MAX and keeps the
        # prompt out of the process table.
        argv.append("-")
        invocation_cwd = cwd if cwd is not None else self._cwd
        # Remember where this argv[0] will be exec'd so a help probe that is
        # given only the binary string resolves the same file execution runs.
        _EXECUTABLE_CWD[argv[0]] = invocation_cwd
        return CliInvocation(
            argv=tuple(argv),
            stdin=prompt,
            cwd=invocation_cwd,
            env=child_env or None,
            timeout_seconds=self._timeout_seconds,
        )

    async def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> NativeCliResult:
        invocation = self.build_invocation(prompt, session_id=session_id, cwd=cwd)
        invocation, isolated_home = self.isolated_invocation(invocation)
        try:
            return await self._run_invocation(invocation, session_id)
        finally:
            remove_isolated_codex_home(isolated_home)

    def isolated_invocation(
        self, invocation: CliInvocation
    ) -> tuple[CliInvocation, Path | None]:
        """``invocation`` under a fresh isolated CODEX_HOME when this adapter isolates.

        The caller removes the returned home (``remove_isolated_codex_home``)
        once the process is gone; None means no home was made.
        """
        if not self._isolate_personal_memory:
            return invocation, None
        parent = isolated_codex_home_parent()
        home = prepare_isolated_codex_home(source_codex_home(), parent) if parent else None
        if home is None:
            return invocation, None
        return (
            replace(invocation, env={**(invocation.env or {}), "CODEX_HOME": str(home)}),
            home,
        )

    async def _run_invocation(
        self, invocation: CliInvocation, session_id: str | None
    ) -> NativeCliResult:
        output_path: str | None = None
        if _output_schema_opt_in():
            # The help probe and the schema setup block on a child process
            # and the filesystem; keep them off the event loop. The worker
            # isolation context var is copied into the thread.
            # A cancelled run must still discard what the thread creates, so
            # the thread's result is cleaned up once it lands.
            attach = asyncio.ensure_future(
                asyncio.to_thread(_attach_output_schema, invocation)
            )
            try:
                invocation, output_path = await asyncio.shield(attach)
            except asyncio.CancelledError:
                attach.add_done_callback(_discard_attached_output)
                raise
        started = time.monotonic()
        try:
            output = await run_process(invocation)
            file_text = (
                _read_final_message(output_path, invocation.env)
                if output_path
                else ""
            )
            try:
                text, new_session_id, usage, events = parse_codex_events(output.stdout)
            except NativeCliError:
                # The schema path's answer is the file. A transcript with no
                # events still counts when that file has the final message.
                if not file_text:
                    raise
                text = file_text
                new_session_id = session_id
                usage = NativeCliUsage()
                events = []
            else:
                if file_text:
                    text = file_text
        finally:
            _discard_schema_output(output_path)
        if not text:
            detail = output.stderr.strip() or "no agent message"
            raise no_final_answer_error(
                f"codex produced no final answer: {detail[:1000]}", events
            )
        return NativeCliResult(
            provider=self.provider,
            text=text,
            session_id=new_session_id or session_id,
            usage=usage,
            duration_seconds=time.monotonic() - started,
            events=len(events),
            raw=tuple(events),
        )


# Codex's own wording, read out of the Codex CLI binary: it names its own
# error reasons ("usage limit reached", "context window exceeded") and its own
# error codes, so the classifier does not have to guess at them.
register_failure_rule(
    "codex",
    FailureClass.QUOTA_EXHAUSTED,
    reason_code="codex_usage_limit_reached",
    patterns=(
        "usage limit reached",
        "you've hit your usage limit",
        "quota_exceeded",
        "usage_not_included",
        "workspace_owner_credits_depleted",
        "workspace_owner_usage_limit_reached",
    ),
)
register_failure_rule(
    "codex",
    FailureClass.CONTEXT_EXHAUSTED,
    reason_code="codex_context_budget_exceeded",
    patterns=(
        "prompt exceeded the maximum context budget",
        "context_window_exceeded",
    ),
)
register_failure_rule(
    "codex",
    FailureClass.OVERLOADED,
    reason_code="codex_server_overloaded",
    patterns=("server_overloaded",),
)
register_failure_rule(
    "codex",
    FailureClass.RATE_LIMITED,
    reason_code="codex_rate_limit_exceeded",
    patterns=(
        "rate_limit_exceeded",
        "rate_limit_reached",
    ),
)
