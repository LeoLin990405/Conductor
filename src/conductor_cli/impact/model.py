"""Shared facts for the change-impact index."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

# 4 records class-body calls on a ``Qual#body`` scope so they see that body's imports.
SCHEMA_VERSION = 7
MAX_DEPTH = 20
MAX_FILE_BYTES = 1_500_000
DECAY = 0.85

# Direct, unique edges. Lower values are less certain static matches.
CONF_LOCAL = 0.95
CONF_IMPORT = 0.90
CONF_SELF = 0.93
CONF_METHOD = 0.88
CONF_MODULE_ATTR = 0.90
CONF_STAR = 0.60
CONF_GLOBAL = 0.55
CONF_CTOR = 0.88
CONF_IMPORT_FILE = 0.45
CONF_TEST_IMPORT = 0.70
CONF_TEST_NAME = 0.40
CONF_REMOVED = 0.70
AMBIGUOUS_CAP = 0.5

EXCLUDE_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".conductor",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        "coverage",
        "htmlcov",
        "site-packages",
        ".tox",
        ".nox",
        "target",
    }
)
_SOURCE_ROOTS = frozenset({"src", "lib", "libs"})
_TS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


class ImpactError(ValueError):
    """The impact query could not be answered from the local index."""


@dataclass(frozen=True)
class Symbol:
    path: str
    name: str
    qualified_name: str
    kind: str
    start_line: int
    end_line: int
    parent: str
    exported: str = ""
    column: int = 0


@dataclass(frozen=True)
class Call:
    path: str
    caller: str
    callee: str
    callee_name: str
    receiver: str
    receiver_kind: str
    line: int
    enclosing_class: str
    column: int = 0


@dataclass(frozen=True)
class Import:
    path: str
    module: str
    name: str
    alias: str
    kind: str
    line: int
    level: int = 0
    scope: str = ""
    column: int = 0


@dataclass(frozen=True)
class FileFacts:
    language: str
    symbols: tuple[Symbol, ...]
    calls: tuple[Call, ...]
    imports: tuple[Import, ...]


def language_for_path(path: str) -> str | None:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix == ".tsx":
        return "tsx"
    if suffix == ".ts":
        return "typescript"
    return None


def is_test_path(path: str) -> bool:
    pure = PurePosixPath(path)
    name = pure.name
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    if name.endswith((".test.ts", ".test.tsx", ".test.js", ".test.jsx")):
        return True
    if name.endswith((".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx")):
        return True
    return any(part in {"tests", "__tests__"} for part in pure.parts[:-1])


def qualified(path: str, scope: list[str], name: str) -> str:
    body = ".".join([*scope, name])
    return f"{path}::{body}"


def parent_qual(path: str, scope: list[str]) -> str:
    if not scope:
        return ""
    return f"{path}::{'.'.join(scope)}"


def module_keys(path: str) -> list[str]:
    """Python module names a file can be imported as."""
    pure = PurePosixPath(path)
    if pure.suffix != ".py":
        return []
    if pure.name == "__init__.py":
        parts = pure.parts[:-1]
    else:
        parts = pure.with_suffix("").parts
    if not parts:
        return []
    keys = [".".join(parts)]
    if parts[0] in _SOURCE_ROOTS and len(parts) > 1:
        keys.append(".".join(parts[1:]))
    return keys


def package_parts(path: str) -> list[str]:
    pure = PurePosixPath(path)
    if pure.name == "__init__.py":
        return list(pure.parts[:-1])
    return list(pure.parent.parts)


def local_name(imp: Import) -> str:
    return imp.alias or imp.name


def symbol_public(symbol: Symbol) -> dict[str, object]:
    return {
        "qualified_name": symbol.qualified_name,
        "name": symbol.name,
        "path": symbol.path,
        "line": symbol.start_line,
        "kind": symbol.kind,
    }
