"""The launch script enforces the workspace sandbox policy before a worker runs.

A policy that is only exported is not a boundary. ``read-only`` cannot be
honoured by a WORK worker (the line must write its checkout), so the launch is
refused with EXIT_BAD_ASSIGNMENT (66) instead of silently starting the worker.
``workspace-write`` is the supported write policy and still starts it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "dispatch_launch.sh"


def _fake_kimi(tmp_path: Path) -> Path:
    binary = tmp_path / "kimi"
    marker = tmp_path / "started"
    binary.write_text(
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$@\" > {str(marker)!r}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


def _run(tmp_path: Path, policy: str, binary: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "CONDUCTOR_DISPATCH_SANDBOX_POLICY": policy,
        "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR": str(tmp_path / "log"),
        "NATIVE_CLI_KIMI_BIN": str(binary),
    }
    return subprocess.run(
        ["bash", str(SCRIPT), "kimi", "do the work"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_read_only_policy_refuses_to_start_a_write_worker(tmp_path):
    binary = _fake_kimi(tmp_path)

    result = _run(tmp_path, "read-only", binary)

    assert result.returncode == 66, result.stderr
    assert "read-only" in result.stderr
    assert not (tmp_path / "started").exists()


def test_an_unknown_policy_is_refused_instead_of_widening_the_sandbox(tmp_path):
    binary = _fake_kimi(tmp_path)

    result = _run(tmp_path, "read-write-everything", binary)

    assert result.returncode == 66, result.stderr
    assert "unsupported sandbox policy" in result.stderr
    assert not (tmp_path / "started").exists()


def test_workspace_write_policy_still_starts_the_worker(tmp_path):
    binary = _fake_kimi(tmp_path)

    result = _run(tmp_path, "workspace-write", binary)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "started").exists()
