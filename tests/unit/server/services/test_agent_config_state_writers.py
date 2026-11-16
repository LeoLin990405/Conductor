"""Every writer of ``setup.agent_config`` also records it with set_agent_config.

R9b L11 gave services their own store (``agent_config_state``) so they no
longer import the app. A script that assigns only ``setup.agent_config`` leaves
the services reading ``None``: no built-in MCP names, a default model of "",
``SubagentsUnavailable``. Two scripts did exactly that.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
_ASSIGNS_SETUP_CONFIG = re.compile(r"\bsetup(?:_module)?\.agent_config\s*=(?!=)")


def _writers() -> list[Path]:
    found = []
    for root in ("src", "scripts", "mcp_servers"):
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if _ASSIGNS_SETUP_CONFIG.search(path.read_text(encoding="utf-8")):
                found.append(path)
    return found


def test_the_scan_finds_the_known_writers() -> None:
    names = {path.relative_to(REPO_ROOT).as_posix() for path in _writers()}
    assert {"scripts/utils/_thread_job.py", "scripts/ops/thin_message_snapshots.py"} <= names


def test_every_setup_agent_config_writer_also_sets_the_service_store() -> None:
    missing = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in _writers()
        if "set_agent_config(" not in path.read_text(encoding="utf-8")
    ]
    assert not missing, f"assign setup.agent_config without set_agent_config: {missing}"


def test_setup_records_the_loaded_config_in_the_service_store() -> None:
    source = (REPO_ROOT / "src" / "server" / "app" / "setup.py").read_text(encoding="utf-8")
    assert "set_agent_config(agent_config)" in source
