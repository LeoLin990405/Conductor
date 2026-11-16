"""R9b L11: the four import hubs stay cut.

* services read the agent config through ``agent_config_state``, so importing
  ``native_runtime.mcp`` no longer loads the FastAPI app module;
* ``import ptc_agent`` (and its ``core``/``agent`` subpackages) loads no config;
* ``SandboxTransientError`` lives in ``src.server.utils`` and the sandbox
  runtime re-exports the same class, so ``database.workspace`` skips the runtime;
* ``src.tools`` no longer re-exports ``TodoWrite`` and ``src.tools.core`` is gone.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.server.services import agent_config_state

REPO_ROOT = Path(__file__).resolve().parents[4]


def _loaded_after(statement: str, modules: list[str]) -> dict[str, bool]:
    code = (
        "import json, sys\n"
        f"{statement}\n"
        f"print(json.dumps({{m: m in sys.modules for m in {modules!r}}}))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_agent_config_state_round_trips(monkeypatch):
    monkeypatch.setattr(agent_config_state, "_agent_config", None)
    assert agent_config_state.get_agent_config() is None
    marker = object()
    agent_config_state.set_agent_config(marker)  # type: ignore[arg-type]
    assert agent_config_state.get_agent_config() is marker
    agent_config_state.set_agent_config(None)
    assert agent_config_state.get_agent_config() is None


@pytest.mark.asyncio
async def test_native_mcp_bridge_reads_the_accessor(monkeypatch):
    from src.server.services.native_runtime import mcp

    monkeypatch.setattr(agent_config_state, "_agent_config", None)
    assert await mcp.collect_builtin_mcp_servers(user_id="u", workspace_id="w") == []


def test_native_runtime_mcp_does_not_load_the_app_setup():
    loaded = _loaded_after(
        "import src.server.services.native_runtime.mcp",
        ["src.server.app.setup", "src.server.services.agent_config_state"],
    )
    assert loaded == {
        "src.server.app.setup": False,
        "src.server.services.agent_config_state": True,
    }


@pytest.mark.parametrize("package", ["ptc_agent", "ptc_agent.core", "ptc_agent.agent"])
def test_ptc_agent_packages_load_no_config(package):
    loaded = _loaded_after(
        f"import {package}",
        ["ptc_agent.config", "src.config.runtime", "config.runtime"],
    )
    assert not any(loaded.values()), loaded


def test_ptc_agent_lazy_names_still_resolve():
    import ptc_agent
    import ptc_agent.agent
    import ptc_agent.core
    from src.config.runtime import AgentConfig, CoreConfig, load_from_files

    assert ptc_agent.AgentConfig is AgentConfig
    assert ptc_agent.CoreConfig is CoreConfig
    assert ptc_agent.agent.load_from_files is load_from_files
    assert ptc_agent.core.CoreConfig is CoreConfig
    with pytest.raises(AttributeError):
        ptc_agent.core.NoSuchName  # noqa: B018


def test_sandbox_transient_error_is_one_class():
    from ptc_agent.core.sandbox import runtime
    from src.server.database import workspace
    from src.server.utils.transient_errors import SandboxTransientError

    assert runtime.SandboxTransientError is SandboxTransientError
    assert workspace.SandboxTransientError is SandboxTransientError
    assert issubclass(runtime.SandboxHostUnavailableError, SandboxTransientError)


def test_app_exception_handler_still_catches_sandbox_transient_error():
    from src.server.app.setup import app
    from src.server.utils.transient_errors import SandboxTransientError

    assert SandboxTransientError in app.exception_handlers


def test_workspace_database_does_not_load_the_sandbox_runtime():
    loaded = _loaded_after(
        "import src.server.database.workspace",
        ["ptc_agent.core.sandbox.runtime"],
    )
    assert loaded == {"ptc_agent.core.sandbox.runtime": False}


def test_src_tools_hub_is_gone():
    import src.tools

    assert not hasattr(src.tools, "TodoWrite")
    assert "TodoWrite" not in src.tools.__all__
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("src.tools.core")
