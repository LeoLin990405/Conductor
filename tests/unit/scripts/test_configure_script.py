"""``make config`` asks only about things the application uses, and points only at text that exists.

These tests run the real script against a copy of the files it edits, with the
programs it probes (docker, lsof, openssl) replaced by stand-ins, so nothing
touches the operator's machine or accounts.
"""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "configure.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="the wizard needs bash and python3",
)

_STAND_INS = {
    # The pre-flight asks whether Docker runs and whether a port is taken.
    "docker": "exit 1",
    "lsof": "exit 1",
    "openssl": 'echo "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"',
}


def _copy_tree(dest: Path) -> Path:
    """Only the files the wizard reads or edits, laid out as the script expects."""
    (dest / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, dest / "scripts" / "configure.sh")
    for name in (".env.example", "agent_config.yaml"):
        shutil.copy(ROOT / name, dest / name)
    for rel in ("src/llms/manifest", "src/tools/manifest"):
        shutil.copytree(ROOT / rel, dest / rel)
    return dest


def _stand_ins(dest: Path) -> Path:
    bin_dir = dest / "bin"
    bin_dir.mkdir()
    for name, body in _STAND_INS.items():
        path = bin_dir / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def _run_wizard(tmp_path: Path, answers: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    tree = _copy_tree(tmp_path / "tree")
    bin_dir = _stand_ins(tmp_path)
    # The stand-ins come first; python3 is the one program the wizard really needs.
    python_dir = Path(shutil.which("python3") or "/usr/bin/python3").parent
    env = {
        "PATH": f"{bin_dir}:{python_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path),
        "LANG": "C",
    }
    proc = subprocess.run(
        ["bash", str(tree / "scripts" / "configure.sh")],
        input=answers,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    return proc, tree


def test_the_script_is_valid_bash():
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_every_readme_section_the_wizard_names_exists():
    """``README > Some Heading`` must be a heading of README.md."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    headings = {
        re.sub(r"^[^\w]+", "", line.lstrip("#").strip()).strip()
        for line in readme.splitlines()
        if line.startswith("#")
    }
    pointers = re.findall(r"README > ([^.\"\n]+)", SCRIPT.read_text(encoding="utf-8"))
    # A pointer may run on into the sentence ("... for details"), so a heading that
    # opens it, ending at a word boundary, is the section it names.
    missing = [
        name.strip()
        for name in pointers
        if not any(name.strip() == h or name.strip().startswith(h + " ") for h in headings if h)
    ]
    assert not missing, f"configure.sh points at README sections that do not exist: {missing}"


def test_the_wizard_runs_to_the_end_on_its_defaults(tmp_path):
    proc, tree = _run_wizard(tmp_path, "\n" * 40)

    assert proc.returncode == 0, proc.stderr
    env_text = (tree / ".env").read_text(encoding="utf-8")
    assert "BYOK_ENCRYPTION_KEY=0123456789abcdef" in env_text
    assert "SANDBOX_PROVIDER=docker" in env_text
    assert "COMPOSE_PROFILES=infra" in env_text
    assert "Done" in proc.stdout

