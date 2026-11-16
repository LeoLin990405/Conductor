"""R9b L09: MCP core and skills core live under src/server/services.

The old ``ptc_agent`` paths are aliases of the new modules (one module object
per name), so private names, module state and ``mock.patch`` targets stay shared.
"""

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[5]

MOVES = {
    "ptc_agent.core.mcp_registry": "src.server.services.mcp_core.registry",
    "ptc_agent.core.mcp_sanitize": "src.server.services.mcp_core.sanitize",
    "ptc_agent.core.mcp_schema": "src.server.services.mcp_core.schema",
    "ptc_agent.core.mcp_composite": "src.server.services.mcp_core.composite",
    "ptc_agent.core.mcp_diagnostics": "src.server.services.mcp_core.diagnostics",
    "ptc_agent.agent.middleware.skills.discovery": "src.server.services.skills_core.discovery",
    "ptc_agent.agent.middleware.skills.lock": "src.server.services.skills_core.lock",
    "ptc_agent.agent.middleware.skills.registry": "src.server.services.skills_core.registry",
}


@pytest.mark.parametrize(("old", "new"), sorted(MOVES.items()))
def test_old_path_is_alias_of_new_module(old, new):
    new_mod = importlib.import_module(new)
    assert importlib.import_module(old) is new_mod
    assert Path(new_mod.__file__).resolve().is_relative_to(REPO_ROOT / "src" / "server" / "services")


def test_module_state_is_shared_through_alias():
    from ptc_agent.core import mcp_registry as old
    from src.server.services.mcp_core import registry as new

    from types import SimpleNamespace

    sentinel = SimpleNamespace(frozen=True)
    old.set_global_registry(sentinel)
    try:
        assert new.get_global_registry() is sentinel
    finally:
        new.clear_global_registry()


def test_skills_package_reexports_moved_objects():
    import ptc_agent.agent.middleware.skills as pkg
    from src.server.services.skills_core import discovery, lock, registry

    assert pkg.SkillMetadata is discovery.SkillMetadata
    assert pkg.SkillsLockFile is lock.SkillsLockFile
    assert pkg.SKILL_REGISTRY is registry.SKILL_REGISTRY
    assert pkg.get_skill_registry is registry.get_skill_registry


def test_moved_cores_do_not_import_app_or_handlers():
    import ast

    for pkg in ("mcp_core", "skills_core"):
        for py in (REPO_ROOT / "src" / "server" / "services" / pkg).glob("*.py"):
            tree = ast.parse(py.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    mod = node.module
                    assert not mod.startswith(("src.server.app", "src.server.handlers", "ptc_agent")), (
                        py,
                        mod,
                    )
