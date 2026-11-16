"""The one-shot launcher stays a valid, self-describing bash entry point."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "cond"


def test_cond_is_executable_and_has_valid_bash_syntax():
    assert SCRIPT.is_file()
    assert SCRIPT.stat().st_mode & 0o111
    result = subprocess.run(["bash", "-n", str(SCRIPT)], check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_cond_help_names_stop_and_status():
    result = subprocess.run([str(SCRIPT), "--help"], check=False, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert "cond stop" in result.stdout
    assert "cond status" in result.stdout
