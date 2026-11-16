"""Guard: no source file imports a module RA-3 deleted.

The retirement moved or removed the PTC chat machinery. A stale import would
only surface when the server boots (or when a rarely hit route runs), so the
guard reads every Python source file and the filesystem itself.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

#: Module paths that must no longer resolve or be imported anywhere.
DELETED_MODULES = (
    "ptc_agent.agent.subagents",
    "ptc_agent.agent.middleware.background_subagent",
    "ptc_agent.agent.memo",
    "src.server.services.report_back",
    "src.server.services.subagent_liveness",
    "src.server.services.subagent_run_coordinator",
    "src.server.services.background_registry_store",
    "src.server.services.memo_binary_storage",
)

#: On-disk paths the retirement removed outright.
DELETED_PATHS = (
    "src/ptc_agent/agent/subagents",
    "src/ptc_agent/agent/middleware/background_subagent",
    "src/ptc_agent/agent/memo",
    "src/server/services/report_back",
    "src/server/services/subagent_liveness.py",
    "src/server/services/subagent_run_coordinator.py",
    "src/server/services/background_registry_store.py",
    "src/server/services/memo_binary_storage.py",
)

_SCANNED_DIRS = ("src", "tests", "libs", "scripts")


def _python_sources():
    for directory in _SCANNED_DIRS:
        base = ROOT / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts or path == Path(__file__).resolve():
                continue
            yield path


def test_deleted_paths_stay_deleted():
    existing = [rel for rel in DELETED_PATHS if (ROOT / rel).exists()]
    assert not existing, f"RA-3 paths came back: {existing}"


def test_deleted_modules_do_not_resolve():
    still_importable = [
        name for name in DELETED_MODULES if importlib.util.find_spec(name) is not None
    ]
    assert not still_importable, f"deleted modules still importable: {still_importable}"


def test_no_source_mentions_a_deleted_module():
    offenders: list[str] = []
    for path in _python_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for name in DELETED_MODULES:
            if name in text:
                offenders.append(f"{path.relative_to(ROOT)}: {name}")
    assert not offenders, "stale RA-3 imports:\n" + "\n".join(offenders)
