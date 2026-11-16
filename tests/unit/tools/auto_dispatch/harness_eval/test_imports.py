"""The eval package does not import the server, the database, or the outcome store."""

from __future__ import annotations

import ast
from pathlib import Path

from harness_eval_fakes import REPO_ROOT

_PACKAGE = REPO_ROOT / "src" / "tools" / "auto_dispatch" / "harness_eval"
_TESTS = Path(__file__).resolve().parent
_FORBIDDEN = ("src.server.database", "src.server.services", "MemoryP0OutcomeStore")


def _imported_names(tree: ast.AST) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            found.append((module, node.lineno))
            found.extend((alias.name, node.lineno) for alias in node.names)
    return found


def test_package_source_does_not_import_server_or_the_outcome_store() -> None:
    offenders: list[str] = []
    for path in sorted(_PACKAGE.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for name, lineno in _imported_names(tree):
            if any(name == item or name.startswith(item + ".") for item in _FORBIDDEN):
                offenders.append(f"{path.name}:{lineno}:{name}")
        for item in _FORBIDDEN:
            if item in text:
                offenders.append(f"{path.name}:text:{item}")
    assert offenders == []


def test_script_imports_the_review_backend_only_inside_the_live_factory() -> None:
    path = REPO_ROOT / "scripts" / "harness_eval.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_level: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_level.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module_level.append(node.module or "")
    assert all(not name.startswith("src.server") for name in module_level)
    nested = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "src.server.services.dispatch"
        and any(alias.name == "NativeReviewBackend" for alias in node.names)
    ]
    assert len(nested) == 1
    assert isinstance(getattr(nested[0], "col_offset", 0), int)
    # The import sits in a function, not at column 0 of the module body.
    assert nested[0].col_offset > 0


def test_new_tests_leave_sockets_disabled() -> None:
    needle = "enable" + "_socket"
    for path in _TESTS.rglob("*.py"):
        assert needle not in path.read_text(encoding="utf-8")
