"""Nothing in ``conductor_cli`` may import ``src.*``.

The wheel that ships ``conductor`` contains ``src/conductor_cli`` and not
``src/llms``: the native-CLI adapters, the session manager and the failure
classifier are the server's business. A ``conductor_cli`` module that
imports ``src.llms`` works in a checkout and breaks in an installation --
and it breaks in the two worst ways there are:

* a module-level import dies with ``ModuleNotFoundError`` before the command
  can say anything at all;
* a lazy import inside a ``try``/``except ImportError`` is swallowed, and the
  command keeps running with **no data** and reports a healthy machine.

This test is the guard against both. It reads the source of every module in
the package, so it fails on an import that is only there in a branch.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: The root of the repository: this file is ``<root>/tests/unit/conductor_cli/``.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The package the wheel ships.
PACKAGE_ROOT = REPO_ROOT / "src" / "conductor_cli"


def _python_files() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _imported_modules(tree: ast.AST) -> list[str]:
    """Every module name the tree imports."""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # A relative import (``from . import x``) has no module name and
            # can never reach outside the package.
            if node.level:
                continue
            if node.module:
                names.append(node.module)
    return names


def _dynamic_imports(tree: ast.AST) -> list[str]:
    """Every module name the tree imports at runtime."""
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not isinstance(function, ast.Attribute):
            continue
        if function.attr not in {"import_module"}:
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.append(first.value)
    return names


def test_the_package_has_modules_to_check() -> None:
    # A guard that checks nothing is not a guard.
    assert len(_python_files()) > 10


@pytest.mark.parametrize(
    "path", _python_files(), ids=lambda path: str(path.relative_to(PACKAGE_ROOT))
)
def test_no_module_in_the_package_imports_src(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    offenders = [
        name
        for name in _imported_modules(tree) + _dynamic_imports(tree)
        if name == "src" or name.startswith("src.")
    ]

    assert not offenders, (
        f"{path.relative_to(REPO_ROOT)} imports {offenders}: the wheel that "
        "ships conductor_cli does not ship src.llms"
    )


#: Top-level names a checkout puts on ``sys.path`` (the editable install adds
#: ``<repo>/src``) but the wheel does not ship: ``src/utils`` and ``src/config``
#: are importable as bare ``utils`` and ``config`` only in a checkout.
_CHECKOUT_ONLY_TOP_LEVEL = ("utils", "config")


@pytest.mark.parametrize(
    "path", _python_files(), ids=lambda path: str(path.relative_to(PACKAGE_ROOT))
)
def test_no_module_imports_checkout_only_top_level_packages(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    offenders = [
        name
        for name in _imported_modules(tree) + _dynamic_imports(tree)
        if any(name == top or name.startswith(top + ".") for top in _CHECKOUT_ONLY_TOP_LEVEL)
    ]

    assert not offenders, (
        f"{path.relative_to(REPO_ROOT)} imports {offenders}: the wheel that "
        "ships conductor_cli has no top-level utils or config package"
    )


def test_the_package_does_not_reach_for_the_native_cli_package() -> None:
    # The same rule for the package by name: a module may not pull in the
    # native-CLI layer through any path.
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name in _imported_modules(tree) + _dynamic_imports(tree):
            assert not name.startswith("src.llms"), (
                f"{path.relative_to(REPO_ROOT)} imports {name}"
            )
