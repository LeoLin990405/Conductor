"""R9b L06: the store backends live in ``src.server.services.store_backends``
and depend on a file backend only through ``FileBackend``."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest
from langgraph.store.memory import InMemoryStore

import src.server.services.store_backends as store_backends
from src.server.services.store_backends.context import CallContext
from src.server.services.store_backends.langgraph_store import StoreBackend
from src.server.services.store_backends.protocol import FileBackend

PACKAGE_DIR = Path(store_backends.__file__).parent
MOVED = ("db_json_route", "db_json_folder", "langgraph_store", "store_cache", "results")
PREFIX = "/home/workspace/.agents/user/memory/"


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


@pytest.mark.parametrize("name", MOVED)
def test_moved_module_lives_in_the_new_package(name):
    module = importlib.import_module(f"src.server.services.store_backends.{name}")
    assert Path(module.__file__).parent == PACKAGE_DIR


@pytest.mark.parametrize("path", sorted(PACKAGE_DIR.glob("*.py")), ids=lambda p: p.name)
def test_package_never_imports_the_sandbox_backend(path):
    imports = _imports(path)
    assert not any("SandboxBackend" in name for name in imports)
    assert not any(name.startswith("ptc_agent.agent.backends.sandbox") for name in imports)
    # grep_render is the one core/sandbox helper still borrowed (pure text
    # formatting; GrepLine identity is shared with the sandbox grep).
    sandbox = {n for n in imports if n.startswith("ptc_agent.core.sandbox")}
    assert sandbox <= {
        "ptc_agent.core.sandbox.grep_render",
        *(f"ptc_agent.core.sandbox.grep_render.{n}" for n in (
            "_GREP_WINDOWS_PER_LINE", "GREP_LINE_CHARS", "GrepLine", "cut_grep_line"
        )),
    }


def test_call_context_is_reexported_as_the_same_object():
    from ptc_agent.core.sandbox import livefs_mount

    assert livefs_mount.CallContext is CallContext
    assert isinstance(livefs_mount.CallContext(thread_id="t"), CallContext)


@pytest.mark.parametrize(
    ("module", "names"),
    [
        ("db_json_route", ("DbJsonRoute", "DbJsonFile", "UserDataValidationError", "README_FILE")),
        ("db_json_folder", ("DbJsonFolderRoute",)),
        ("langgraph_store", ("StoreBackend", "ReadOnlyStoreError", "user_memory_namespace", "MAX_CONTENT_BYTES")),
        ("results", ("EditTextResult", "WriteTextResult", "GrepAnswer")),
    ],
)
def test_old_paths_are_shims_over_the_same_objects(module, names):
    old = importlib.import_module(f"ptc_agent.agent.backends.{module}")
    new = importlib.import_module(f"src.server.services.store_backends.{module}")
    assert "R9b shim" in (old.__doc__ or "")
    for name in names:
        assert getattr(old, name) is getattr(new, name)


def test_store_cache_has_no_old_path():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("ptc_agent.agent.backends.store_cache")
    from ptc_agent.agent.backends import RequestScopedStoreCache
    from src.server.services.store_backends.store_cache import RequestScopedStoreCache as moved

    assert RequestScopedStoreCache is moved


def test_sandbox_backend_satisfies_file_backend():
    from ptc_agent.agent.backends.sandbox import SandboxBackend

    for member in ("normalize_path", "virtualize_path", "validate_path", "filesystem_config"):
        assert hasattr(SandboxBackend, member)
    assert isinstance(SandboxBackend.filesystem_config, property)


class _Files:
    def normalize_path(self, path: str) -> str:
        return path if path.startswith("/") else f"/home/workspace/{path}"

    def virtualize_path(self, path: str) -> str:
        return path.removeprefix("/home/workspace")

    def validate_path(self, path: str) -> bool:
        return True

    @property
    def filesystem_config(self) -> str:
        return "config"


def test_store_backend_delegates_path_rules_to_a_file_backend():
    files = _Files()
    assert isinstance(files, FileBackend)
    backend = StoreBackend(
        store=InMemoryStore(), namespace_factory=lambda: ("u", "memory"),
        root_prefix=PREFIX, sandbox_backend=files,
    )
    assert backend.normalize_path("a.md") == "/home/workspace/a.md"
    assert backend.virtualize_path(PREFIX + "a.md") == "/.agents/user/memory/a.md"
    assert backend.filesystem_config == "config"
    assert backend.read_only_view("no")._sandbox is files


@pytest.mark.asyncio
async def test_store_backend_runs_without_a_file_backend():
    backend = StoreBackend(
        store=InMemoryStore(), namespace_factory=lambda: ("u", "memory"), root_prefix=PREFIX
    )
    assert backend.normalize_path(PREFIX + "a.md") == PREFIX + "a.md"
    assert backend.virtualize_path(PREFIX + "a.md") == PREFIX + "a.md"
    assert backend.validate_path(PREFIX + "a.md")
    assert not backend.validate_path("/etc/passwd")
    assert backend.filesystem_config is None
    await backend.awrite_text(PREFIX + "a.md", "hello")
    assert await backend.aread_text(PREFIX + "a.md") == "hello"
