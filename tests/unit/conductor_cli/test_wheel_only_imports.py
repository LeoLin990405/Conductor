"""conductor_cli and the TUI import with only the wheels' packages on sys.path.

A checkout's editable install puts ``<repo>/src`` on ``sys.path``, so a bare
``import utils`` or ``import config`` works there and nowhere else: the
``conductor`` wheel ships only the packages listed under
``[tool.hatch.build.targets.wheel]`` in pyproject.toml, and the TUI wheel
(libs/ptc-cli) ships ``ptc_cli``. This copies exactly those packages into a
temporary site directory, hides the checkout, and imports from there.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

_PROBE = r"""
import importlib, sys
site, repo = sys.argv[1], sys.argv[2]
src = repo + "/src"
# Hide the checkout: its src/ (from the editable .pth), the repo root, and the
# editable finders that map ptc_cli / conductor_cli back into the checkout.
sys.path[:] = [p for p in sys.path if p not in ("", ".", repo, src) and not p.startswith(src + "/")]
sys.path.insert(0, site)
sys.meta_path[:] = [f for f in sys.meta_path if "editable" not in type(f).__module__.lower()]
for name in list(sys.modules):
    if name.split(".")[0] in {"src", "utils", "config", "ptc_agent", "conductor_cli", "ptc_cli"}:
        del sys.modules[name]

from datetime import datetime, timezone

for name in sys.argv[3:]:
    module = importlib.import_module(name)
    assert module.__file__.startswith(site), (name, module.__file__)

from conductor_cli.usage import _configured_balance_thresholds, _provider_headroom
from conductor_cli.codexbar_snapshot import PROVIDERS

_provider_headroom({"provider": "manus"}, datetime.now(timezone.utc))
_configured_balance_thresholds()
assert PROVIDERS
for top in ("src", "utils", "config"):
    assert top not in sys.modules, top + " was imported"
print("ok")
"""


def _wheel_site(tmp_path: Path) -> Path:
    site = tmp_path / "site"
    site.mkdir()
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    packages = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for package in packages:
        source = REPO_ROOT / package
        shutil.copytree(source, site / source.name, ignore=ignore)
    shutil.copytree(REPO_ROOT / "libs" / "ptc-cli" / "ptc_cli", site / "ptc_cli", ignore=ignore)
    return site


def test_conductor_cli_and_tui_import_from_the_wheels_alone(tmp_path: Path) -> None:
    site = _wheel_site(tmp_path)
    modules = [
        "conductor_cli.cli",
        "conductor_cli.usage",
        "conductor_cli.codexbar_snapshot",
        "conductor_cli.provider_usage",
        "conductor_cli.config_files",
        "ptc_agent.utils.provider_usage",
        "ptc_cli.commands.slash",
        "ptc_cli.input.completers",
    ]
    result = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, str(site), str(REPO_ROOT), *modules],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("ok")


def test_tui_sandbox_path_constants_match_the_server() -> None:
    """ptc_cli keeps a copy of the constants it reads; they must not drift."""
    from ptc_cli import sandbox_paths
    from src.utils import paths

    for name in (
        "DEFAULT_SANDBOX_ROOT",
        "AGENT_SYSTEM_DIRS",
        "LEGACY_ROOT_DIRS",
        "HIDDEN_DIR_NAMES",
        "ALWAYS_HIDDEN_PATH_SEGMENTS",
        "ALWAYS_HIDDEN_BASENAMES",
        "ALWAYS_HIDDEN_SUFFIXES",
    ):
        assert getattr(sandbox_paths, name) == getattr(paths, name), name
