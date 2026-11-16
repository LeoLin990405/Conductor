"""R9b L08: the runtime config package lives in src/config/runtime."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
MODULES = ("agent", "core", "file_utils", "loaders", "plugins", "utils")

#: Modules whose canonical file lives in the package the conductor wheel ships
#: (conductor_cli needs them there); src/config/runtime keeps an alias.
CANONICAL_ELSEWHERE = {"file_utils": REPO_ROOT / "src" / "conductor_cli" / "config_files.py"}


@pytest.mark.parametrize("name", MODULES)
def test_modules_live_in_src_config_runtime(name: str) -> None:
    module = importlib.import_module(f"src.config.runtime.{name}")
    expected = CANONICAL_ELSEWHERE.get(name, REPO_ROOT / "src" / "config" / "runtime" / f"{name}.py")
    assert Path(module.__file__) == expected


@pytest.mark.parametrize("name", MODULES)
def test_ptc_agent_shim_is_the_same_module_object(name: str) -> None:
    """One live module per name: patches and module state are shared."""
    old = importlib.import_module(f"ptc_agent.config.{name}")
    new = importlib.import_module(f"src.config.runtime.{name}")
    assert old is new
    assert sys.modules[f"ptc_agent.config.{name}"] is new


def test_package_shim_reexports_the_same_objects() -> None:
    import ptc_agent
    import ptc_agent.config as old
    import src.config.runtime as new

    assert old.__all__ == new.__all__
    for name in new.__all__:
        assert getattr(old, name) is getattr(new, name), name
    assert ptc_agent.AgentConfig is new.AgentConfig


def test_src_config_core_reexports_the_runtime_file_utils() -> None:
    from src.config import core
    from src.config.runtime import file_utils

    assert core.find_config_file is file_utils.find_config_file
    assert core.load_yaml_config is file_utils.load_yaml_config
    assert core._clear_file_cache is file_utils.clear_config_cache


def test_file_relative_paths_still_resolve_to_the_repo_root() -> None:
    """``plugins.py`` climbs ``parents[3]``; the subpackage keeps that the repo root.

    Probed in a fresh process: this directory's conftest repoints BUNDLES_DIR.
    """
    probe = (
        "from src.config.runtime.plugins import BUNDLES_DIR\n"
        "from src.server.services.native_runtime import mcp\n"
        "print(BUNDLES_DIR)\n"
        "print(mcp.REPO_ROOT)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    bundles, repo_root = result.stdout.strip().splitlines()[-2:]
    assert Path(bundles) == REPO_ROOT / "plugins"
    assert Path(repo_root) == REPO_ROOT


def test_server_import_loads_one_copy_of_the_runtime_config() -> None:
    probe = (
        "import sys\n"
        "import src.server.app\n"
        "import ptc_agent.config.core, src.config.runtime.core\n"
        "assert ptc_agent.config.core is src.config.runtime.core\n"
        "dupes = [m for m in sys.modules if m == 'config' or m.startswith('config.')]\n"
        "assert not dupes, dupes\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith("ok")


def test_cli_processes_import_it_outside_the_checkout(tmp_path: Path) -> None:
    """The CLI and TUI run from any cwd, where ``src.`` is not importable."""
    probe = (
        "import sys\n"
        "import ptc_agent, ptc_agent.config.core, config.runtime.core\n"
        "assert ptc_agent.config.core is config.runtime.core\n"
        "assert ptc_agent.AgentConfig is config.runtime.AgentConfig\n"
        "from conductor_cli.usage import _configured_balance_thresholds\n"
        "_configured_balance_thresholds()\n"
        "assert 'src' not in sys.modules\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith("ok")


def test_cli_and_tui_imports_outside_the_repo_do_not_load_dotenv(tmp_path) -> None:
    """Before L08 a CLI/TUI process never ran load_dotenv(); the move must not add it."""
    import subprocess
    import sys

    probe = (
        "import dotenv\n"
        "calls = []\n"
        "dotenv.load_dotenv = lambda *a, **k: calls.append((a, k)) or True\n"
        "import ptc_agent\n"
        "from conductor_cli.usage import _configured_balance_thresholds\n"
        "_configured_balance_thresholds()\n"
        "import sys\n"
        "assert 'src.config' not in sys.modules, 'server config loaded'\n"
        "assert calls == [], calls\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")
