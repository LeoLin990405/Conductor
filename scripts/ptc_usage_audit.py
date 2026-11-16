"""Static import graph of which ``src/ptc_agent`` code Conductor actually reaches.

Read-only. Python files are parsed with :mod:`ast`; nothing in the audited tree
is imported or executed. TypeScript under ``web/src`` is scanned as text.

Module resolution
    Absolute imports resolve as ``src.*`` (the repository root on
    ``sys.path``) and as bare top-level packages under each source root
    (``src/`` first, then ``libs/ptc-cli/``), so ``ptc_agent``, ``server`` and
    ``ptc_cli`` all resolve. Relative imports resolve from the importing file.
    Every ``import`` and ``from`` counts, including function-local imports and
    imports under ``if TYPE_CHECKING:``.

Package initializers
    Loading ``a.b.c`` runs ``a/__init__.py`` and ``a/b/__init__.py`` first, so
    the walk follows those edges too. Each entry module also starts with the
    initializers of its own ancestor packages (``src/tools/auto_dispatch/x.py``
    runs ``src/__init__.py``, ``src/tools/__init__.py`` and
    ``src/tools/auto_dispatch/__init__.py``). A directory without
    ``__init__.py`` that holds modules is a namespace package; importing it, or
    a name from it, still runs the initializers of the packages above it.

Measures per entry group
    *Reached*: every edge above, through any module (what can run).
    *Loaded at import*: only imports that run when a module loads, i.e. not
    inside a function body and not under ``if TYPE_CHECKING:``.
    *Imported directly*: target modules the group's own entry files import.
    *Own need*: those plus what they load without leaving the target, with
    and without the target's package initializers (the second number is what
    would load if every ``__init__`` in the target were empty).
    *Gateways*: for each reached target module, the last module outside the
    target on its shortest chain; counted per gateway.

Entry groups (paths relative to ``--root``)
    dispatch        src/tools/auto_dispatch/**, src/server/services/dispatch.py,
                    src/server/app/dispatch.py
    router_ticket   src/server/services/autonomy/**, src/tools/autonomy/**,
                    src/server/app/autonomy.py
    cli             src/conductor_cli/** (``cond`` runs ops_cli.py via
                    ``scripts/cond``; ``conductor`` is the console script in
                    pyproject.toml), plus any ``-m`` module ``scripts/cond`` names
    native_worker   src/llms/native_cli/**, src/server/services/native_runtime/**
    web_api         every src/server module whose FastAPI route matches a
                    request made by code in web/src (tests excluded)
    tui             libs/ptc-cli/ptc_cli/** (the forked interactive TUI)

Web requests
    Calls such as ``api.get('/api/v1/x/${id}')``, ``fetch(...)`` and the SSE
    helpers are read from ``web/src``. A ``${...}`` becomes a path parameter
    ``{}`` unless every value the expression can take is empty or a query
    string. Route decorators are evaluated from string constants, including an
    ``APIRouter(prefix=...)`` defined or imported elsewhere. Image URLs minted
    by a server ``*icon_url*`` function and loaded through ``<img src>`` count
    as requests of that route.

Output
    The CLI prints a plain-text summary. ``--write PATH`` also writes the
    markdown tables into PATH between the generated-section markers, keeping
    any hand-written text outside them; ``--check PATH`` exits 1 when that
    section is stale. Files that fail to parse are skipped and reported.

Usage:
    python scripts/ptc_usage_audit.py
    python scripts/ptc_usage_audit.py --write docs/plans/next/ptc-audit.md
    python scripts/ptc_usage_audit.py --check docs/plans/next/ptc-audit.md
    python scripts/ptc_usage_audit.py --root FIXTURE --target src/ptc_agent
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

SOURCE_ROOTS = ("src", "libs/ptc-cli")
GROUPS = ("dispatch", "router_ticket", "cli", "native_worker", "web_api", "tui")
GROUP_TITLES = {
    "dispatch": "Dispatch path",
    "router_ticket": "Router and Ticket path",
    "cli": "cond / conductor CLIs",
    "native_worker": "Native worker runtime",
    "web_api": "Web console API routers",
    "tui": "TUI (libs/ptc-cli)",
}
GEN_START = "<!-- ptc-audit:generated:start -->"
GEN_END = "<!-- ptc-audit:generated:end -->"
DEFAULT_SPLIT_LOC = 5000
DEFAULT_MAX_DEPTH = 3

_ROUTE_METHODS = frozenset(
    {
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "options",
        "api_route",
        "websocket",
    }
)
_HTTP_CALL = re.compile(
    r"(?:\bfetch|\bstreamStatusEvents|\bstreamFetch|\bpostSSEStream|\bEventSource"
    r"|\.(?:get|post|put|patch|delete|head|options|request))\b"
)
_QUERY_BINDING = re.compile(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([^;\n]+)")
_JS_STRING = re.compile(r"""(['"`])((?:\\.|(?!\1).)*)\1""")
_JS_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_COND_MODULE = re.compile(r"(?:^|[\s\"'])-m\s+([A-Za-z_][\w.]*)")
_CLIENT_SPEC = re.compile(
    r"""(?:import|export)\s+(?:type\s+)?(?:[^'";]*?\sfrom\s+)?['"]([^'"]+)['"]"""
)
_CLIENT_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


# --------------------------------------------------------------------------
# Report data


@dataclass(frozen=True)
class Entry:
    """One module a walk starts from."""

    group: str
    path: str


@dataclass(frozen=True)
class Chain:
    """Import path from an entry module to a target module."""

    group: str
    hops: tuple[str, ...]

    def render(self) -> str:
        return " -> ".join(self.hops)


@dataclass(frozen=True)
class Row:
    """One subpackage row of the target package."""

    name: str
    loc: int
    files: int
    reached_files: int
    own_files: int
    groups: tuple[str, ...]
    direct_groups: tuple[str, ...]
    chains: tuple[Chain, ...]
    direct_importers: tuple[str, ...]


@dataclass(frozen=True)
class GroupResult:
    """Everything one entry group reaches."""

    group: str
    entries: tuple[str, ...]
    reached: tuple[str, ...]
    loaded: tuple[str, ...]
    gateways: tuple[tuple[str, int], ...]
    own: tuple[str, ...]
    named: tuple[str, ...]
    direct: tuple[tuple[str, tuple[str, ...]], ...]
    per_entry: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class RouteMatch:
    request: str
    server: str
    clients: tuple[str, ...]


@dataclass(frozen=True)
class Unmatched:
    request: str
    clients: tuple[str, ...]


@dataclass(frozen=True)
class SyntaxFailure:
    path: str
    message: str
    lineno: int | None


@dataclass(frozen=True)
class Report:
    target: str
    total_loc: int
    total_files: int
    rows: tuple[Row, ...]
    groups: tuple[GroupResult, ...]
    reached_any: tuple[str, ...]
    own_any: tuple[str, ...]
    unreached_modules: tuple[str, ...]
    outside_importers: tuple[tuple[str, tuple[str, ...]], ...]
    unreached_importers: tuple[tuple[str, tuple[str, ...]], ...]
    route_matches: tuple[RouteMatch, ...]
    unmatched: tuple[Unmatched, ...]
    syntax_errors: tuple[SyntaxFailure, ...]
    dynamic: tuple[tuple[str, tuple[str, ...]], ...] = ()
    shipped: tuple[tuple[str, tuple[str, ...]], ...] = ()
    split_loc: int = DEFAULT_SPLIT_LOC
    max_depth: int = DEFAULT_MAX_DEPTH
    extra: dict[str, object] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Parsing and module resolution


@dataclass
class _Parsed:
    rel: str
    path: Path
    tree: ast.Module | None
    error: str | None = None
    lineno: int | None = None


@dataclass(frozen=True)
class _Resolved:
    """A module file, a package directory, or both.

    ``file`` is None for a namespace package. ``top`` bounds the package
    initializers an absolute import runs (the first package of the name).
    """

    file: Path | None
    directory: Path | None
    top: Path


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _loc(path: Path) -> int:
    text = path.read_text(encoding="utf-8", errors="replace")
    return sum(1 for line in text.splitlines() if line.strip())


def _skip_dir(path: Path) -> bool:
    return any(part in {"__pycache__", "node_modules", ".venv"} for part in path.parts)


class _Tree:
    """Parsed Python sources and import resolution for one checkout."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.src = root / "src"
        self.roots = [root / name for name in SOURCE_ROOTS if (root / name).is_dir()]
        self.parsed: dict[str, _Parsed] = {}
        for base in self.roots:
            for path in sorted(base.rglob("*.py")):
                rel_inside = path.relative_to(base)
                if _skip_dir(rel_inside):
                    continue
                if base != self.src and rel_inside.parts[0] in {"tests", "test"}:
                    continue
                self._parse(path)

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _parse(self, path: Path) -> None:
        rel = self.rel(path)
        try:
            tree = ast.parse(
                path.read_text(encoding="utf-8", errors="replace"), filename=rel
            )
        except (SyntaxError, ValueError) as exc:
            self.parsed[rel] = _Parsed(
                rel,
                path,
                None,
                getattr(exc, "msg", None) or str(exc),
                getattr(exc, "lineno", None),
            )
            return
        self.parsed[rel] = _Parsed(rel, path, tree)

    def base_of(self, path: Path) -> Path | None:
        for base in self.roots:
            if path == base or _within(path, base):
                return base
        return None

    @staticmethod
    def _lookup(parent: Path, name: str) -> tuple[Path | None, Path | None] | None:
        """A package ``__init__.py`` beats a module file, which beats a namespace dir."""
        directory = parent / name
        init = directory / "__init__.py"
        if init.is_file():
            return init, directory
        module = parent / f"{name}.py"
        if module.is_file():
            return module, None
        if directory.is_dir():
            return None, directory
        return None

    def _walk_parts(
        self, cursor: Path, parts: tuple[str, ...], top: Path
    ) -> _Resolved | None:
        file: Path | None = None
        directory: Path | None = None
        for index, part in enumerate(parts):
            found = self._lookup(cursor, part)
            if found is None:
                return None
            file, directory = found
            if directory is not None:
                cursor = directory
            elif index != len(parts) - 1:
                return None
        target = file or directory
        if target is None or self.base_of(target) is None:
            return None
        return _Resolved(file, directory, top)

    def resolve_absolute(self, name: str) -> _Resolved | None:
        parts = tuple(name.split("."))
        if not parts or not parts[0]:
            return None
        if parts[0] == "src":
            if not self.src.is_dir():
                return None
            if len(parts) == 1:
                init = self.src / "__init__.py"
                return _Resolved(init if init.is_file() else None, self.src, self.src)
            return self._walk_parts(self.src, parts[1:], self.src)
        for base in self.roots:
            if self._lookup(base, parts[0]) is not None:
                return self._walk_parts(base, parts, base / parts[0])
        return None

    def resolve_relative(
        self, file: Path, level: int, module: str | None
    ) -> _Resolved | None:
        bound = self.base_of(file)
        if bound is None:
            return None
        base = file.parent
        for _ in range(level - 1):
            base = base.parent
        if base != bound and not _within(base, bound):
            return None
        if not module:
            init = base / "__init__.py"
            return _Resolved(init if init.is_file() else None, base, base)
        return self._walk_parts(base, tuple(module.split(".")), base)

    def child(self, directory: Path, name: str) -> Path | None:
        found = self._lookup(directory, name)
        if found is None:
            return None
        return found[0]


def _inits_above(named: Path, top: Path, *, include_top: bool) -> list[Path]:
    """Package initializers Python runs before ``named`` (a file or a dir's virtual init)."""
    current = named.parent.parent if named.name == "__init__.py" else named.parent
    found: list[Path] = []
    while current == top or _within(current, top):
        if current == top and not include_top:
            break
        init = current / "__init__.py"
        if init.is_file() and init != named:
            found.append(init)
        if current == top:
            break
        current = current.parent
    return found


# --------------------------------------------------------------------------
# Import graph


def _mentions_file(expr: ast.expr) -> bool:
    """True when ``expr`` is built from ``__file__`` (a path next to the module)."""
    return any(
        isinstance(node, ast.Name) and node.id == "__file__" for node in ast.walk(expr)
    )


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _import_nodes(tree: ast.Module):
    """Yield ``(import node, deferred)``.

    ``deferred`` is True inside a function or lambda body and inside an
    ``if TYPE_CHECKING:`` block: such imports do not run when the module loads.
    Class bodies run at load time.
    """
    stack: list[tuple[ast.AST, bool]] = [(tree, False)]
    while stack:
        node, deferred = stack.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node, deferred
            continue
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            stack.extend((child, True) for child in reversed(node.body))
            stack.extend((child, deferred) for child in reversed(node.orelse))
            continue
        inner = deferred or isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        )
        stack.extend(
            (child, inner) for child in reversed(list(ast.iter_child_nodes(node)))
        )


class _Graph:
    def __init__(self, tree: _Tree) -> None:
        self.tree = tree
        self.explicit: dict[str, set[str]] = {rel: set() for rel in tree.parsed}
        self.walk: dict[str, set[str]] = {rel: set() for rel in tree.parsed}
        self.load: dict[str, set[str]] = {rel: set() for rel in tree.parsed}
        self.dynamic: dict[str, set[str]] = {}
        self.shipped: dict[str, set[str]] = {}
        self._eager = True
        self._assigns: dict[str, dict[str, ast.expr]] = {}
        self._bindings: dict[str, dict[str, tuple[str, str]]] = {}
        self._routers: dict[str, dict[str, ast.expr | None]] = {}
        for rel, item in tree.parsed.items():
            if item.tree is None:
                continue
            self._assigns[rel] = _module_assigns(item.tree)
            self._routers[rel] = _router_prefixes(item.tree)
            self._collect(item)
        for rel, item in tree.parsed.items():
            if item.tree is not None:
                self._bindings[rel] = self._module_bindings(item)
        # Reachability also follows dynamic imports and shipped files; the
        # static graphs (walk, explicit, load) do not.
        self.reach: dict[str, set[str]] = {
            rel: edges | self.dynamic.get(rel, set()) | self.shipped.get(rel, set())
            for rel, edges in self.walk.items()
        }

    # -- edges ---------------------------------------------------------------

    def _add(self, importer: str, named: Path | None, inits: list[Path]) -> None:
        if named is not None:
            rel = self.tree.rel(named)
            if rel != importer and rel in self.tree.parsed:
                self.explicit[importer].add(rel)
                self.walk[importer].add(rel)
                if self._eager:
                    self.load[importer].add(rel)
        for init in inits:
            rel = self.tree.rel(init)
            if rel != importer and rel in self.tree.parsed:
                self.walk[importer].add(rel)
                if self._eager:
                    self.load[importer].add(rel)

    def _inits(self, named: Path, resolved: _Resolved, *, relative: bool) -> list[Path]:
        # A relative import runs from inside an already-loaded package, so the
        # base package's own initializer is not a new edge.
        return _inits_above(named, resolved.top, include_top=not relative)

    def _follow(
        self, importer: str, resolved: _Resolved, names: list[str], *, relative: bool
    ) -> None:
        children: list[Path] = []
        attribute = not names  # ``import a.b`` loads ``a.b`` itself
        if resolved.directory is not None:
            for name in names:
                child = (
                    None if name == "*" else self.tree.child(resolved.directory, name)
                )
                if child is None:
                    attribute = True
                else:
                    children.append(child)
        else:
            attribute = True
        for child in children:
            self._add(importer, child, self._inits(child, resolved, relative=relative))
        if not attribute:
            return
        if resolved.file is not None:
            self._add(
                importer,
                resolved.file,
                self._inits(resolved.file, resolved, relative=relative),
            )
        elif resolved.directory is not None:
            # Namespace package: no module to name, but the parents still run.
            virtual = resolved.directory / "__init__.py"
            self._add(importer, None, self._inits(virtual, resolved, relative=relative))

    def _collect(self, item: _Parsed) -> None:
        assert item.tree is not None
        for node, deferred in _import_nodes(item.tree):
            self._eager = not deferred
            if isinstance(node, ast.Import):
                for alias in node.names:
                    resolved = self.tree.resolve_absolute(alias.name)
                    if resolved is not None:
                        self._follow(item.rel, resolved, [], relative=False)
            elif isinstance(node, ast.ImportFrom):
                names = [alias.name for alias in node.names]
                if node.level:
                    resolved = self.tree.resolve_relative(
                        item.path, node.level, node.module
                    )
                    relative = True
                elif node.module:
                    resolved = self.tree.resolve_absolute(node.module)
                    relative = False
                else:
                    continue
                if resolved is not None:
                    self._follow(item.rel, resolved, names, relative=relative)

        self._eager = True
        self._collect_strings(item)

    def _collect_strings(self, item: _Parsed) -> None:
        """Module names passed to ``import_module`` and files shipped by path.

        In a module that calls ``import_module`` or ``find_spec``, a string
        constant naming a module (``"a.b"`` or ``"a.b:Attr"``) is a dynamic
        import. ``Path(...).with_name("x")`` and ``... / "dir" / "x.py"`` that
        name a file or directory next to the module are files copied into a
        sandbox and run there. Both are deferred edges: reached, not loaded.
        """
        assert item.tree is not None
        calls = {
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else getattr(node.func, "id", "")
            for node in ast.walk(item.tree)
            if isinstance(node, ast.Call)
        }
        if calls & {"import_module", "find_spec"}:
            for node in ast.walk(item.tree):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                name = node.value.split(":", 1)[0]
                if not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)+", name):
                    continue
                resolved = self.tree.resolve_absolute(name)
                if resolved is None or resolved.file is None:
                    continue
                rel = self.tree.rel(resolved.file)
                if rel != item.rel and rel in self.tree.parsed:
                    self.dynamic.setdefault(item.rel, set()).add(rel)
        here = item.path.parent
        inner = {
            id(node.left)
            for node in ast.walk(item.tree)
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
        }
        for node in ast.walk(item.tree):
            pieces: list[str] = []
            if id(node) in inner:
                continue  # part of a longer ``a / "b" / "c"`` chain
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "with_name"
            ):
                if (
                    node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and _mentions_file(node.func.value)
                ):
                    pieces = [node.args[0].value]
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                cursor: ast.expr = node
                while isinstance(cursor, ast.BinOp) and isinstance(cursor.op, ast.Div):
                    right = cursor.right
                    if not (
                        isinstance(right, ast.Constant) and isinstance(right.value, str)
                    ):
                        pieces = []
                        break
                    pieces.insert(0, right.value)
                    cursor = cursor.left
                if not _mentions_file(cursor):
                    pieces = []
            if not pieces:
                continue
            found = here.joinpath(*pieces)
            files = (
                sorted(found.rglob("*.py"))
                if found.is_dir()
                else [found]
                if found.suffix == ".py"
                else []
            )
            for path in files:
                if not path.is_file() or _skip_dir(path):
                    continue
                rel = self.tree.rel(path)
                if rel != item.rel and rel in self.tree.parsed:
                    self.shipped.setdefault(item.rel, set()).add(rel)

    # -- string constants for routes ----------------------------------------

    def _module_bindings(self, item: _Parsed) -> dict[str, tuple[str, str]]:
        assert item.tree is not None
        bindings: dict[str, tuple[str, str]] = {}
        for node in item.tree.body:
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level:
                resolved = self.tree.resolve_relative(
                    item.path, node.level, node.module
                )
            elif node.module:
                resolved = self.tree.resolve_absolute(node.module)
            else:
                continue
            if resolved is None or resolved.file is None:
                continue
            source = self.tree.rel(resolved.file)
            if source not in self.tree.parsed:
                continue
            for alias in node.names:
                if alias.name != "*":
                    bindings.setdefault(
                        alias.asname or alias.name, (source, alias.name)
                    )
        return bindings

    def eval_str(
        self, rel: str, expr: ast.expr, depth: int = 8, seen: set | None = None
    ) -> str | None:
        seen = set() if seen is None else seen
        if depth <= 0:
            return None
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return expr.value
        if isinstance(expr, ast.Name):
            key = (rel, expr.id)
            if key in seen:
                return None
            seen.add(key)
            assigned = self._assigns.get(rel, {}).get(expr.id)
            if assigned is not None:
                return self.eval_str(rel, assigned, depth - 1, seen)
            binding = self._bindings.get(rel, {}).get(expr.id)
            if binding is None:
                return None
            return self.eval_str(binding[0], ast.Name(id=binding[1]), depth - 1, seen)
        if isinstance(expr, ast.JoinedStr):
            parts: list[str] = []
            for value in expr.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                elif isinstance(value, ast.FormattedValue):
                    got = self.eval_str(rel, value.value, depth - 1, seen)
                    if got is None:
                        return None
                    parts.append(got)
                else:
                    return None
            return "".join(parts)
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            left = self.eval_str(rel, expr.left, depth - 1, seen)
            right = self.eval_str(rel, expr.right, depth - 1, seen)
            if left is None or right is None:
                return None
            return left + right
        return None

    def route_pattern(self, rel: str, expr: ast.expr, depth: int = 8) -> str | None:
        """Like :meth:`eval_str`, but an unknown name or f-string piece becomes ``{}``."""
        if depth <= 0:
            return None
        if isinstance(expr, ast.Constant):
            return expr.value if isinstance(expr.value, str) else None
        if isinstance(expr, ast.Name):
            got = self.eval_str(rel, expr, depth)
            return "{}" if got is None else got
        if isinstance(expr, ast.JoinedStr):
            parts: list[str] = []
            for value in expr.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                elif isinstance(value, ast.FormattedValue):
                    got = self.route_pattern(rel, value.value, depth - 1)
                    parts.append("{}" if got is None else got)
                else:
                    return None
            return "".join(parts)
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            left = self.route_pattern(rel, expr.left, depth - 1)
            right = self.route_pattern(rel, expr.right, depth - 1)
            if left is None or right is None:
                return None
            return left + right
        if isinstance(expr, ast.Attribute):
            return "{}"
        return None

    def prefix_of(self, rel: str, name: str, depth: int = 8) -> str | None:
        """Static ``APIRouter(prefix=...)`` for router ``name`` visible in ``rel``."""
        for _ in range(depth):
            routers = self._routers.get(rel, {})
            if name in routers:
                expr = routers[name]
                return "" if expr is None else self.eval_str(rel, expr)
            binding = self._bindings.get(rel, {}).get(name)
            if binding is None:
                return None
            rel, name = binding
        return None


def _module_assigns(tree: ast.Module) -> dict[str, ast.expr]:
    assigned: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                assigned.setdefault(target.id, value)
    return assigned


def _router_prefixes(tree: ast.Module) -> dict[str, ast.expr | None]:
    prefixes: dict[str, ast.expr | None] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not isinstance(value, ast.Call):
            continue
        func = value.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name not in {"APIRouter", "FastAPI"}:
            continue
        prefix = next((kw.value for kw in value.keywords if kw.arg == "prefix"), None)
        for target in targets:
            if isinstance(target, ast.Name):
                prefixes.setdefault(target.id, prefix)
    return prefixes


# --------------------------------------------------------------------------
# Server routes


def _normalize_route(path: str) -> str:
    path = path.split("?", 1)[0].split("#", 1)[0].strip()
    if not path.startswith("/"):
        return ""
    path = re.sub(r"\{[^{}]*\}", "{}", path)
    path = re.sub(r"/+", "/", path)
    if len(path) > 1:
        path = path.rstrip("/")
    return path


def _join_route(prefix: str, path: str) -> str:
    prefix = prefix.rstrip("/")
    if path in {"", "/"}:
        return prefix or "/"
    if not path.startswith("/"):
        path = "/" + path
    return prefix + path


def _server_routes(graph: _Graph) -> dict[str, set[str]]:
    """Normalized route paths declared by each ``src/server`` module."""
    declared: dict[str, set[str]] = {}
    for rel, item in graph.tree.parsed.items():
        if item.tree is None or not rel.startswith("src/server/"):
            continue
        paths: set[str] = set()
        for node in ast.walk(item.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                if not (
                    isinstance(func, ast.Attribute) and func.attr in _ROUTE_METHODS
                ):
                    continue
                if not isinstance(func.value, ast.Name):
                    continue
                path_expr = (
                    decorator.args[0]
                    if decorator.args
                    else next(
                        (kw.value for kw in decorator.keywords if kw.arg == "path"),
                        None,
                    )
                )
                if path_expr is None:
                    continue
                prefix = graph.prefix_of(rel, func.value.id)
                route = graph.eval_str(rel, path_expr)
                if prefix is None or route is None:
                    continue
                normalized = _normalize_route(_join_route(prefix, route))
                if normalized:
                    paths.add(normalized)
        if paths:
            declared[rel] = paths
    return declared


def _minted_icon_routes(graph: _Graph) -> set[str]:
    """Paths returned by ``src/server`` functions whose name contains ``icon_url``."""
    found: set[str] = set()
    for rel, item in graph.tree.parsed.items():
        if item.tree is None or not rel.startswith("src/server/"):
            continue
        for node in ast.walk(item.tree):
            if (
                not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                or "icon_url" not in node.name
            ):
                continue
            for child in ast.walk(node):
                if not isinstance(child, ast.Return) or child.value is None:
                    continue
                stack = [child.value]
                while stack:
                    expr = stack.pop()
                    if isinstance(expr, ast.IfExp):
                        stack.extend([expr.body, expr.orelse])
                        continue
                    pattern = graph.route_pattern(rel, expr)
                    if pattern and "/api/" in pattern:
                        normalized = _normalize_route(pattern)
                        if normalized:
                            found.add(normalized)
    return found


# --------------------------------------------------------------------------
# Web client requests (TypeScript scanned as text)


def _strip_ts_comments(text: str) -> str:
    out: list[str] = []
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        nxt = text[index + 1] if index + 1 < length else ""
        if char == "/" and nxt == "/":
            while index < length and text[index] != "\n":
                index += 1
            continue
        if char == "/" and nxt == "*":
            end = text.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        if char in "'\"`":
            end = _skip_string(text, index)
            out.append(text[index:end])
            index = end
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _skip_string(text: str, start: int) -> int:
    quote, index = text[start], start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if quote == "`" and text.startswith("${", index):
            depth, index = 1, index + 2
            while index < len(text) and depth:
                if text[index] in "'\"`":
                    index = _skip_string(text, index)
                    continue
                depth += {"{": 1, "}": -1}.get(text[index], 0)
                index += 1
            continue
        if text[index] == quote:
            return index + 1
        index += 1
    return len(text)


def _code_chars(expr: str):
    """Yield ``(index, char)`` outside JavaScript string literals."""
    quote, escape = None, False
    for index, char in enumerate(expr):
        if quote:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
            continue
        yield index, char


def _depth(char: str, depth: int) -> int:
    if char in "([{":
        return depth + 1
    if char in ")]}":
        return max(0, depth - 1)
    return depth


def _is_ternary(expr: str, index: int) -> bool:
    nxt = expr[index + 1] if index + 1 < len(expr) else ""
    prev = expr[index - 1] if index else ""
    return nxt not in {"?", "."} and prev != "?"


def _split_ternary(expr: str) -> tuple[str, str] | None:
    """Split ``COND ? THEN : ELSE`` at top level. ``??`` and ``?.`` are not ternaries."""
    question, depth = None, 0
    for index, char in _code_chars(expr):
        depth = _depth(char, depth)
        if char == "?" and depth == 0 and _is_ternary(expr, index):
            question = index
            break
    if question is None:
        return None
    depth = nested = 0
    for index, char in _code_chars(expr):
        if index <= question:
            continue
        depth = _depth(char, depth)
        if depth:
            continue
        if char == "?" and _is_ternary(expr, index):
            nested += 1
        elif char == ":":
            if not nested:
                return expr[question + 1 : index], expr[index + 1 :]
            nested -= 1
    return None


def _unwrap(expr: str) -> str:
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        depth, closes_early = 0, False
        for index, char in _code_chars(expr):
            depth = _depth(char, depth)
            if depth == 0 and index != len(expr) - 1:
                closes_early = True
                break
        if closes_early:
            break
        expr = expr[1:-1].strip()
    return expr


def _query_only(expr: str) -> bool:
    """True when every value ``expr`` produces is ``''`` or starts with ``?`` / ``#``.

    ``lazy ? '?lazy=1' : ''`` qualifies; ``id ?? ''`` does not (the value is a
    path segment and ``''`` is only its fallback).
    """
    expr = _unwrap(expr)
    match = _JS_STRING.fullmatch(expr)
    if match is not None:
        body = match.group(2)
        return body == "" or body.startswith(("?", "#"))
    split = _split_ternary(expr)
    return split is not None and _query_only(split[0]) and _query_only(split[1])


def _query_names(text: str) -> frozenset[str]:
    """Bindings in a file whose every assignment is query-only."""
    verdict: dict[str, bool] = {}
    for match in _QUERY_BINDING.finditer(text):
        name = match.group(1)
        verdict[name] = verdict.get(name, True) and _query_only(match.group(2))
    return frozenset(name for name, ok in verdict.items() if ok)


def _read_literal(text: str, start: int, query_names: frozenset[str]) -> str:
    """The string literal at ``start``; template ``${...}`` pieces become ``{}`` or ``''``."""
    quote, index, out = text[start], start + 1, []
    while index < len(text):
        char = text[index]
        if char == "\\":
            out.append(text[index + 1 : index + 2])
            index += 2
            continue
        if char == quote:
            break
        if quote == "`" and text.startswith("${", index):
            depth, inner_start, index = 1, index + 2, index + 2
            while index < len(text) and depth:
                if text[index] in "'\"`":
                    index = _skip_string(text, index)
                    continue
                depth += {"{": 1, "}": -1}.get(text[index], 0)
                index += 1
            inner = text[inner_start : index - 1].strip()
            dropped = (
                _JS_IDENT.fullmatch(inner) and inner in query_names
            ) or _query_only(inner)
            out.append("" if dropped else "{}")
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _extract_path(value: str) -> str | None:
    for marker in ("/api/", "/api?", "/health", "/ready", "/v1/"):
        found = value.find(marker)
        if found >= 0:
            return value[found:].split()[0]
    stripped = value.strip()
    if stripped.startswith("/") and not stripped.startswith("//"):
        return stripped.split()[0]
    return None


def client_routes(text: str) -> set[str]:
    """Normalized routes requested by one TypeScript source text."""
    code = _strip_ts_comments(text)
    names = _query_names(code)
    routes: set[str] = set()
    for match in _HTTP_CALL.finditer(code):
        index = match.end()
        while index < len(code) and code[index] in " \t\r\n":
            index += 1
        if index < len(code) and code[index] == "<":
            depth = 0
            while index < len(code):
                depth += {"<": 1, ">": -1}.get(code[index], 0)
                index += 1
                if depth == 0:
                    break
            while index < len(code) and code[index] in " \t\r\n":
                index += 1
        if index >= len(code) or code[index] != "(":
            continue
        index += 1
        while index < len(code) and code[index] in " \t\r\n":
            index += 1
        if index >= len(code) or code[index] not in "'\"`":
            continue
        path = _extract_path(_read_literal(code, index, names))
        if path is None:
            continue
        normalized = _normalize_route(path)
        if normalized:
            routes.add(normalized)
    return routes


def _client_sources(root: Path) -> dict[str, str]:
    web = root / "web" / "src"
    if not web.is_dir():
        return {}
    texts: dict[str, str] = {}
    for path in sorted(web.rglob("*")):
        if not path.is_file() or path.suffix not in _CLIENT_SUFFIXES or _skip_dir(path):
            continue
        if "__tests__" in path.parts or ".test." in path.name or ".spec." in path.name:
            continue
        texts[path.relative_to(root).as_posix()] = path.read_text(
            encoding="utf-8", errors="replace"
        )
    return texts


def _resolve_client_spec(root: Path, importer: str, spec: str) -> str | None:
    if spec.startswith("@/"):
        base = root / "web" / "src" / spec[2:]
    elif spec.startswith("."):
        base = (root / importer).parent / spec
    else:
        return None
    candidates = [base]
    for suffix in _CLIENT_SUFFIXES:
        candidates += [base.parent / f"{base.name}{suffix}", base / f"index{suffix}"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve().relative_to(root).as_posix()
    return None


def _icon_clients(root: Path, texts: dict[str, str]) -> set[str]:
    """Files that build ``${baseURL}${...icon_url}`` image URLs, and ``<img>`` users of them."""
    builders = set()
    for rel, text in texts.items():
        code = _strip_ts_comments(text)
        if re.search(r"`[^`]*\$\{[^}]*baseURL[^}]*\}[^`]*icon_url[^`]*`", code, re.S):
            builders.add(rel)
    clients = set(builders)
    for rel, text in texts.items():
        if rel in clients or not re.search(
            r"<img\b[^>]*\bsrc\s*=\s*\{[^}]*\.src\b", text
        ):
            continue
        specs = _CLIENT_SPEC.findall(_strip_ts_comments(text))
        if any(_resolve_client_spec(root, rel, spec) in builders for spec in specs):
            clients.add(rel)
    return clients


def _requested_routes(
    root: Path, graph: _Graph, declared: dict[str, set[str]]
) -> dict[str, set[str]]:
    texts = _client_sources(root)
    requested: dict[str, set[str]] = {}
    for rel, text in texts.items():
        for route in client_routes(text):
            requested.setdefault(route, set()).add(rel)
    served = set().union(*declared.values()) if declared else set()
    minted = _minted_icon_routes(graph) & served
    if minted:
        clients = _icon_clients(root, texts)
        if clients:
            for route in minted:
                requested.setdefault(route, set()).update(clients)
    return requested


# --------------------------------------------------------------------------
# Entries, rows and the walk


def _under(rel: str, prefix: str) -> bool:
    return rel == prefix or rel.startswith(prefix.rstrip("/") + "/")


def _module_rel(tree: _Tree, module: str) -> str | None:
    resolved = tree.resolve_absolute(module)
    if resolved is None or resolved.file is None:
        return None
    rel = tree.rel(resolved.file)
    return rel if rel in tree.parsed else None


def _discover_entries(root: Path, tree: _Tree, web_servers: set[str]) -> list[Entry]:
    found: dict[tuple[str, str], Entry] = {}

    def add(group: str, rel: str | None) -> None:
        if rel is not None and rel in tree.parsed:
            found.setdefault((group, rel), Entry(group, rel))

    for rel in tree.parsed:
        if _under(rel, "src/tools/auto_dispatch") or rel in {
            "src/server/services/dispatch.py",
            "src/server/app/dispatch.py",
        }:
            add("dispatch", rel)
        if (
            _under(rel, "src/server/services/autonomy")
            or _under(rel, "src/tools/autonomy")
            or rel == "src/server/app/autonomy.py"
        ):
            add("router_ticket", rel)
        if _under(rel, "src/llms/native_cli") or _under(
            rel, "src/server/services/native_runtime"
        ):
            add("native_worker", rel)
        if _under(rel, "libs/ptc-cli/ptc_cli"):
            add("tui", rel)
        if _under(rel, "src/conductor_cli"):
            add("cli", rel)
    cond = root / "scripts" / "cond"
    if cond.is_file():
        for module in _COND_MODULE.findall(
            cond.read_text(encoding="utf-8", errors="replace")
        ):
            add("cli", _module_rel(tree, module))
    project = root / "pyproject.toml"
    if project.is_file():
        try:
            scripts = (
                tomllib.loads(project.read_text(encoding="utf-8"))
                .get("project", {})
                .get("scripts", {})
            )
        except tomllib.TOMLDecodeError:
            scripts = {}
        for name, spec in scripts.items():
            if isinstance(spec, str) and name.startswith("conductor"):
                add("cli", _module_rel(tree, spec.split(":", 1)[0].strip()))
    for rel in web_servers:
        add("web_api", rel)
    order = {group: index for index, group in enumerate(GROUPS)}
    return sorted(found.values(), key=lambda entry: (order[entry.group], entry.path))


def _dir_loc(base: Path) -> dict[Path, int]:
    totals: dict[Path, int] = {}
    for path in base.rglob("*.py"):
        if _skip_dir(path.relative_to(base)):
            continue
        lines = _loc(path)
        current = path.parent
        while current == base or _within(current, base):
            totals[current] = totals.get(current, 0) + lines
            if current == base:
                break
            current = current.parent
    return totals


def _row_namer(root: Path, target: str, split_loc: int, max_depth: int):
    """Map a target module path to its row name.

    A directory becomes one row unless it has more than ``split_loc``
    non-empty lines and sits above ``max_depth``; then its subdirectories get
    their own rows and its loose modules share a ``dir/*.py`` row.
    """
    base = root / target
    totals = _dir_loc(base) if base.is_dir() else {}
    prefix = target.rstrip("/") + "/"

    def name(rel: str) -> str | None:
        if not rel.startswith(prefix):
            return None
        parts = rel[len(prefix) :].split("/")
        current = base
        for depth, part in enumerate(parts[:-1], start=1):
            child = current / part
            label = child.relative_to(base).as_posix()
            if depth >= max_depth or totals.get(child, 0) <= split_loc:
                return label
            current = child
        parent = current.relative_to(base).as_posix()
        return "*.py" if parent == "." else f"{parent}/*.py"

    return name, totals


def _entry_inits(tree: _Tree, path: Path) -> list[Path]:
    """Initializers of the packages that contain an entry module."""
    base = tree.base_of(path)
    if base is None:
        return []
    return _inits_above(path, base, include_top=True)


def _starts(tree: _Tree, root: Path, paths: list[str]) -> list[tuple[str, str | None]]:
    """Entry modules plus the package initializers that run before each of them."""
    starts: list[tuple[str, str | None]] = [(path, None) for path in paths]
    for path in paths:
        starts += [(tree.rel(init), path) for init in _entry_inits(tree, root / path)]
    return starts


def _bfs(
    starts: list[tuple[str, str | None]], edges: dict[str, set[str]]
) -> dict[str, str | None]:
    parent: dict[str, str | None] = {}
    queue: deque[str] = deque()
    for node, via in starts:
        if node not in parent:
            parent[node] = via
            queue.append(node)
    while queue:
        node = queue.popleft()
        for nxt in sorted(
            edges.get(node, ()), key=lambda rel: (rel.endswith("__init__.py"), rel)
        ):
            if nxt not in parent:
                parent[nxt] = node
                queue.append(nxt)
    return parent


def _own_closure(
    graph: _Graph, prefix: str, direct: set[str], *, inits: bool
) -> set[str]:
    """Target modules that importing ``direct`` loads, without leaving the target.

    With ``inits`` the walk includes the package initializers Python runs
    (the ancestors of each directly imported module, and those of every later
    import). Without it, only modules named by import statements count: what
    would load if every package ``__init__`` in the target were empty.
    """
    source = graph.walk if inits else graph.explicit
    edges = {
        rel: {n for n in nxt if n.startswith(prefix)}
        for rel, nxt in source.items()
        if rel.startswith(prefix)
    }
    starts: list[tuple[str, str | None]] = [(rel, None) for rel in sorted(direct)]
    if inits:
        top = graph.tree.root / prefix.rstrip("/")
        for rel in sorted(direct):
            for init in _inits_above(graph.tree.root / rel, top, include_top=True):
                starts.append((graph.tree.rel(init), rel))
    return set(_bfs(starts, edges))


def _into(
    edges: dict[str, set[str]], prefix: str
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Edges whose destination is in the target, sorted."""
    found = []
    for rel, nxt in sorted(edges.items()):
        hits = tuple(sorted(n for n in nxt if n.startswith(prefix)))
        if hits:
            found.append((rel, hits))
    return tuple(found)


def _hops(parent: dict[str, str | None], node: str) -> tuple[str, ...]:
    chain: list[str] = []
    cursor: str | None = node
    while cursor is not None and cursor not in chain:
        chain.append(cursor)
        cursor = parent.get(cursor)
    return tuple(reversed(chain))


def audit(
    root: Path,
    target: str = "src/ptc_agent",
    *,
    split_loc: int = DEFAULT_SPLIT_LOC,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> Report:
    """Walk ``root`` and report how ``target`` is reached. Reads files only."""
    root = root.resolve()
    if Path(target).is_absolute():
        target = Path(target).resolve().relative_to(root).as_posix()
    target = target.strip("/")
    prefix = target + "/"
    tree = _Tree(root)
    graph = _Graph(tree)
    in_target = sorted(rel for rel in tree.parsed if rel.startswith(prefix))
    row_of, totals = _row_namer(root, target, split_loc, max_depth)

    declared = _server_routes(graph)
    requested = _requested_routes(root, graph, declared)
    matches: list[RouteMatch] = []
    for server, paths in declared.items():
        for path in paths & requested.keys():
            matches.append(RouteMatch(path, server, tuple(sorted(requested[path]))))
    matches.sort(key=lambda item: (item.request, item.server))
    matched = {item.request for item in matches}
    unmatched = tuple(
        Unmatched(path, tuple(sorted(requested[path])))
        for path in sorted(set(requested) - matched)
    )
    entries = _discover_entries(root, tree, {item.server for item in matches})

    groups: list[GroupResult] = []
    chains_by_row: dict[str, dict[str, Chain]] = {}
    groups_by_module: dict[str, set[str]] = {}
    own_any: set[str] = set()
    reached_any: set[str] = set()
    direct_groups_by_module: dict[str, set[str]] = {}
    for group in GROUPS:
        members = [entry.path for entry in entries if entry.group == group]
        per_entry: list[tuple[str, int]] = []
        for path in members:
            parent = _bfs(_starts(tree, root, [path]), graph.reach)
            per_entry.append((path, sum(1 for rel in parent if rel.startswith(prefix))))
        parent = _bfs(_starts(tree, root, members), graph.reach)
        reached = {rel for rel in parent if rel.startswith(prefix)}
        loaded = {
            rel
            for rel in _bfs(_starts(tree, root, members), graph.load)
            if rel.startswith(prefix)
        }
        best: dict[str, Chain] = {}
        gateways: dict[str, int] = {}
        for rel in sorted(reached):
            chain = Chain(group, _hops(parent, rel))
            row = row_of(rel)
            if row is not None and (
                row not in best
                or (len(chain.hops), chain.hops) < (len(best[row].hops), best[row].hops)
            ):
                best[row] = chain
            outside_hops = [hop for hop in chain.hops if not hop.startswith(prefix)]
            if outside_hops:
                gateways[outside_hops[-1]] = gateways.get(outside_hops[-1], 0) + 1
        direct: dict[str, set[str]] = {}
        for path in members:
            for imported in graph.explicit.get(path, ()):
                if imported.startswith(prefix):
                    direct.setdefault(imported, set()).add(path)
        own = _own_closure(graph, prefix, set(direct), inits=True)
        named = _own_closure(graph, prefix, set(direct), inits=False)
        for rel in direct:
            direct_groups_by_module.setdefault(rel, set()).add(group)
        for row, chain in best.items():
            chains_by_row.setdefault(row, {})[group] = chain
        for rel in reached:
            groups_by_module.setdefault(rel, set()).add(group)
        reached_any |= reached
        own_any |= own
        groups.append(
            GroupResult(
                group=group,
                entries=tuple(members),
                reached=tuple(sorted(reached)),
                loaded=tuple(sorted(loaded)),
                gateways=tuple(
                    sorted(gateways.items(), key=lambda item: (-item[1], item[0]))
                ),
                own=tuple(sorted(own)),
                named=tuple(sorted(named)),
                direct=tuple(
                    sorted((rel, tuple(sorted(src))) for rel, src in direct.items())
                ),
                per_entry=tuple(per_entry),
            )
        )

    outside: dict[str, set[str]] = {}
    for importer, imports in graph.explicit.items():
        if importer.startswith(prefix):
            continue
        for imported in imports:
            if imported.startswith(prefix):
                outside.setdefault(imported, set()).add(importer)

    row_files: dict[str, list[str]] = {}
    for rel in in_target:
        name = row_of(rel)
        if name is not None:
            row_files.setdefault(name, []).append(rel)
    base = root / target
    rows: list[Row] = []
    for name in sorted(row_files, key=lambda item: (item.replace("*.py", ""), item)):
        files = row_files[name]
        if name.endswith("*.py"):
            loc = sum(_loc(root / rel) for rel in files)
        else:
            loc = totals.get(base / name, 0)
        importers = sorted({imp for rel in files for imp in outside.get(rel, ())})
        found_groups = sorted(
            {g for rel in files for g in groups_by_module.get(rel, ())},
            key=GROUPS.index,
        )
        direct_groups = sorted(
            {g for rel in files for g in direct_groups_by_module.get(rel, ())},
            key=GROUPS.index,
        )
        rows.append(
            Row(
                name=name,
                loc=loc,
                files=len(files),
                reached_files=sum(1 for rel in files if rel in reached_any),
                own_files=sum(1 for rel in files if rel in own_any),
                groups=tuple(found_groups),
                direct_groups=tuple(direct_groups),
                chains=tuple(
                    chains_by_row.get(name, {})[g]
                    for g in found_groups
                    if g in chains_by_row.get(name, {})
                ),
                direct_importers=tuple(importers),
            )
        )

    everything = set(
        _bfs(_starts(tree, root, [entry.path for entry in entries]), graph.reach)
    )
    unreached_importers = tuple(
        (rel, tuple(sorted(item for item in imports if item.startswith(prefix))))
        for rel, imports in sorted(graph.explicit.items())
        if rel.startswith("src/server/")
        and rel not in everything
        and any(item.startswith(prefix) for item in imports)
    )
    failures = tuple(
        SyntaxFailure(item.rel, item.error or "syntax error", item.lineno)
        for item in sorted(tree.parsed.values(), key=lambda item: item.rel)
        if item.error is not None
    )
    return Report(
        target=target,
        total_loc=totals.get(base, 0),
        total_files=len(in_target),
        rows=tuple(rows),
        groups=tuple(groups),
        reached_any=tuple(sorted(reached_any)),
        own_any=tuple(sorted(own_any)),
        unreached_modules=tuple(rel for rel in in_target if rel not in reached_any),
        outside_importers=tuple(
            sorted((rel, tuple(sorted(src))) for rel, src in outside.items())
        ),
        unreached_importers=unreached_importers,
        route_matches=tuple(matches),
        unmatched=unmatched,
        syntax_errors=failures,
        dynamic=_into(graph.dynamic, prefix),
        shipped=_into(graph.shipped, prefix),
        split_loc=split_loc,
        max_depth=max_depth,
    )


# --------------------------------------------------------------------------
# Rendering


def _short(rel: str, target: str) -> str:
    prefix = target + "/"
    return rel[len(prefix) :] if rel.startswith(prefix) else rel


def _modules_by_row(
    report: Report, modules: tuple[str, ...]
) -> list[tuple[str, list[str]]]:
    """Group target module paths under their row, keeping row order."""
    row_of: dict[str, str] = {}
    for row in report.rows:
        base = row.name[: -len("*.py")] if row.name.endswith("*.py") else row.name + "/"
        row_of[row.name] = base
    grouped: dict[str, list[str]] = {}
    for rel in modules:
        short = _short(rel, report.target)
        best = ""
        for name, base in row_of.items():
            loose = name.endswith("*.py")
            if loose and short.startswith(base) and "/" not in short[len(base) :]:
                best = name
                break
            if not loose and short.startswith(base) and len(base) > len(best):
                best = name
        grouped.setdefault(best or "?", []).append(short)
    order = [row.name for row in report.rows]
    return [(name, grouped[name]) for name in order if name in grouped]


def headline(report: Report) -> list[str]:
    reached_rows = sum(1 for row in report.rows if row.reached_files)
    lines = [
        f"target: {report.target} ({report.total_files} modules, {report.total_loc} non-empty lines)",
        f"rows: {len(report.rows)} (split above {report.split_loc} lines, depth <= {report.max_depth}),"
        f" {reached_rows} reached",
        f"modules reached by any entry: {len(report.reached_any)}/{report.total_files};"
        f" reached by no entry: {len(report.unreached_modules)}",
        f"modules the entry groups' own imports need: {len(report.own_any)}/{report.total_files}",
        "modules imported by name from outside the package: "
        f"{len(report.outside_importers)}/{report.total_files}",
    ]
    for group in report.groups:
        lines.append(
            f"{group.group}: {len(group.entries)} entry modules; reached {len(group.reached)},"
            f" loaded at import {len(group.loaded)},"
            f" own need {len(group.own)} ({len(group.named)} without package inits),"
            f" imported directly {len(group.direct)}"
        )
    lines += [
        f"server modules importing the target that no entry reaches: {len(report.unreached_importers)}",
        f"web routes matched: {len(report.route_matches)}; web requests with no server route:"
        f" {len(report.unmatched)}",
        "dynamic imports into the target (import_module strings):"
        f" {sum(len(t) for _, t in report.dynamic)} from {len(report.dynamic)} modules;"
        f" files shipped into a sandbox by path: {sum(len(t) for _, t in report.shipped)}"
        f" from {len(report.shipped)} modules",
        f"syntax errors skipped: {len(report.syntax_errors)}",
    ]
    return lines


def render_summary(report: Report) -> str:
    """Plain-text summary the CLI prints."""
    lines = [
        "PTC usage audit",
        *headline(report),
        "",
        "rows (loc, files, reached, own need, importers; groups):",
    ]
    for row in report.rows:
        lines.append(
            f"  {row.name}: {row.loc} loc, {row.files} files, {row.reached_files} reached,"
            f" {row.own_files} own, {len(row.direct_importers)} importers;"
            f" reached by {', '.join(row.groups) or '-'}; imported directly by {', '.join(row.direct_groups) or '-'}"
        )
        for chain in row.chains:
            lines.append(f"    {chain.group}: {chain.render()}")
    for group in report.groups:
        lines += ["", f"{group.group} reaches {len(group.reached)} modules:"]
        for row, modules in _modules_by_row(report, group.reached):
            lines.append(f"  {row}: {', '.join(modules)}")
        lines.append(f"{group.group} own need ({len(group.own)}):")
        for row, modules in _modules_by_row(report, group.own):
            lines.append(f"  {row}: {', '.join(modules)}")
        lines.append(
            f"{group.group} gateways (last outside module on the chain: target modules):"
        )
        lines += [f"  {rel}: {count}" for rel, count in group.gateways] or ["  none"]
        deferred = sorted(set(group.reached) - set(group.loaded))
        lines.append(
            f"{group.group} reached only through deferred imports ({len(deferred)}):"
        )
        for row, modules in _modules_by_row(report, tuple(deferred)):
            lines.append(f"  {row}: {', '.join(modules)}")
        lines.append(
            f"{group.group} own need without package inits ({len(group.named)}):"
        )
        for row, modules in _modules_by_row(report, group.named):
            lines.append(f"  {row}: {', '.join(modules)}")
        lines.append(f"{group.group} entry modules importing the target directly:")
        for rel, importers in group.direct:
            lines.append(f"  {_short(rel, report.target)} <- {', '.join(importers)}")
    lines += ["", "reached by no entry:"]
    lines += [f"  {rel}" for rel in report.unreached_modules] or ["  none"]
    lines += ["", "dynamic imports and shipped files:"]
    lines += [
        f"  dynamic {rel} -> {', '.join(_short(i, report.target) for i in hits)}"
        for rel, hits in report.dynamic
    ]
    lines += [
        f"  shipped {rel} -> {', '.join(_short(i, report.target) for i in hits)}"
        for rel, hits in report.shipped
    ]
    lines += ["", "server modules importing the target that no entry reaches:"]
    for rel, imports in report.unreached_importers:
        lines.append(
            f"  {rel} -> {', '.join(_short(item, report.target) for item in imports)}"
        )
    if not report.unreached_importers:
        lines.append("  none")
    lines += ["", "web route mapping:"]
    lines += [
        f"  {item.request} -> {item.server} ({', '.join(item.clients)})"
        for item in report.route_matches
    ]
    lines += ["", "web requests with no server route:"]
    lines += [
        f"  {item.request} ({', '.join(item.clients)})" for item in report.unmatched
    ] or ["  none"]
    lines += ["", "syntax errors skipped:"]
    lines += [
        f"  {item.path}: {item.message}"
        + ("" if item.lineno is None else f" (line {item.lineno})")
        for item in report.syntax_errors
    ] or ["  none"]
    return "\n".join(lines) + "\n"


def _code(text: str) -> str:
    return f"`{text}`"


def render_markdown(report: Report) -> str:
    """The generated section of the report (deterministic; no absolute paths)."""
    t = report.target
    out = [
        GEN_START,
        "",
        "### Headline numbers",
        "",
        "```text",
        *headline(report),
        "```",
        "",
    ]
    out += [
        "### Subpackages",
        "",
        f"LOC is non-empty lines. A row is a directory of `{t}`; a directory over "
        f"{report.split_loc} lines is split into its subdirectories plus a `*.py` row for its "
        "loose modules. *Reached* counts modules any entry group reaches (Python import "
        "semantics, through any server module). *Own* counts modules an entry group's own "
        "files import by name, plus what those load without leaving the package. *Importers* "
        "counts modules anywhere outside the package that import the row by name. *Direct "
        "groups* are the entry groups whose own files import the row.",
        "",
        "| Row | LOC | Files | Reached | Own | Importers | Reached by | Direct groups |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in report.rows:
        out.append(
            f"| `{row.name}` | {row.loc} | {row.files} | {row.reached_files} | {row.own_files}"
            f" | {len(row.direct_importers)} | {', '.join(row.groups) or '-'}"
            f" | {', '.join(row.direct_groups) or '-'} |"
        )
    out += [
        "",
        "### Example chain per row and entry group",
        "",
        "Shortest chain (fewest hops) per group.",
        "",
    ]
    for row in report.rows:
        if not row.chains:
            continue
        out.append(f"- `{row.name}`")
        for chain in row.chains:
            out.append(f"  - {chain.group}: {_code(chain.render())}")
    out += ["", "### Every reached module, per entry group", ""]
    for group in report.groups:
        out += [
            f"#### {GROUP_TITLES[group.group]} (`{group.group}`)",
            "",
            f"{len(group.entries)} entry modules; {len(group.reached)} target modules reached, "
            f"{len(group.loaded)} of them loaded at import; "
            f"own need {len(group.own)} ({len(group.named)} if the target's package `__init__` files "
            f"were empty); {len(group.direct)} imported directly by the entry modules.",
            "",
            "<details><summary>Entry modules and their counts</summary>",
            "",
        ]
        out += [f"- `{path}`: {count}" for path, count in group.per_entry] or ["- none"]
        out += [
            "",
            "</details>",
            "",
            "Gateways (last module outside the target on each shortest chain: target modules reached through it):",
            "",
        ]
        out += [f"- `{rel}`: {count}" for rel, count in group.gateways] or ["- none"]
        out += [
            "",
            "Imported directly by the entry modules (target module <- importers):",
            "",
        ]
        out += [
            f"- `{_short(rel, t)}` <- " + ", ".join(_code(item) for item in importers)
            for rel, importers in group.direct
        ] or ["- none"]
        out += ["", "Own need without package inits, by row:", ""]
        out += [
            f"- `{row}`: " + ", ".join(modules)
            for row, modules in _modules_by_row(report, group.named)
        ]
        if not group.named:
            out.append("- none")
        out += [
            "",
            "<details><summary>Own need with package inits, by row</summary>",
            "",
        ]
        out += [
            f"- `{row}`: " + ", ".join(modules)
            for row, modules in _modules_by_row(report, group.own)
        ]
        if not group.own:
            out.append("- none")
        out += ["", "</details>"]
        deferred = tuple(sorted(set(group.reached) - set(group.loaded)))
        out += [
            "",
            f"<details><summary>Reached only through deferred imports ({len(deferred)})</summary>",
            "",
        ]
        out += [
            f"- `{row}`: " + ", ".join(modules)
            for row, modules in _modules_by_row(report, deferred)
        ]
        if not deferred:
            out.append("- none")
        out += [
            "",
            "</details>",
            "",
            "<details><summary>All reached modules, by row</summary>",
            "",
        ]
        out += [
            f"- `{row}`: " + ", ".join(modules)
            for row, modules in _modules_by_row(report, group.reached)
        ]
        if not group.reached:
            out.append("- none")
        out += ["", "</details>", ""]
    out += ["### Target modules reached by no entry", ""]
    out += [f"- `{_short(rel, t)}`" for rel in report.unreached_modules] or ["- none"]
    out += ["", "### Dynamic imports and shipped files", ""]
    out += [
        "Reached, never loaded at import: `import_module` strings and files copied into a sandbox.",
        "",
    ]
    out += [
        f"- dynamic `{rel}` -> " + ", ".join(_code(_short(item, t)) for item in hits)
        for rel, hits in report.dynamic
    ]
    out += [
        f"- shipped `{rel}` -> " + ", ".join(_code(_short(item, t)) for item in hits)
        for rel, hits in report.shipped
    ]
    if not report.dynamic and not report.shipped:
        out.append("- none")
    out += ["", "### Server modules that import the target and no entry reaches", ""]
    out += [
        f"- `{rel}` -> " + ", ".join(_code(_short(item, t)) for item in imports)
        for rel, imports in report.unreached_importers
    ] or ["- none"]
    out += [
        "",
        "### Direct importers per target module",
        "",
        "<details><summary>Modules outside the package that import each target module by name</summary>",
        "",
        "| Target module | Count | Importers |",
        "| --- | ---: | --- |",
    ]
    for rel, importers in report.outside_importers:
        out.append(
            f"| `{_short(rel, t)}` | {len(importers)} | "
            + ", ".join(_code(i) for i in importers)
            + " |"
        )
    out += [
        "",
        "</details>",
        "",
        "### Web route mapping",
        "",
        "| Request | Server module | Requested from |",
        "| --- | --- | --- |",
    ]
    for item in report.route_matches:
        out.append(
            f"| `{item.request}` | `{item.server}` | "
            + ", ".join(_code(c) for c in item.clients)
            + " |"
        )
    out += ["", "Requests with no server route in this repository:", ""]
    out += [
        f"- `{item.request}` from " + ", ".join(_code(c) for c in item.clients)
        for item in report.unmatched
    ] or ["- none"]
    out += ["", "### Syntax errors skipped", ""]
    out += [
        f"- `{item.path}`: {item.message}"
        + ("" if item.lineno is None else f" (line {item.lineno})")
        for item in report.syntax_errors
    ] or ["- none"]
    out += ["", GEN_END]
    return "\n".join(out) + "\n"


def merge_into(existing: str | None, generated: str) -> str:
    """Replace the generated section of ``existing``, or return ``generated`` alone."""
    if existing and GEN_START in existing and GEN_END in existing:
        head, rest = existing.split(GEN_START, 1)
        tail = rest.split(GEN_END, 1)[1]
        return head + generated.rstrip("\n") + tail
    return generated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument(
        "--target",
        default="src/ptc_agent",
        help="package directory, relative to --root",
    )
    parser.add_argument("--split-loc", type=int, default=DEFAULT_SPLIT_LOC)
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--write", type=Path, help="write the generated section into this markdown file"
    )
    action.add_argument(
        "--check", type=Path, help="exit 1 if this file's generated section is stale"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="print only the headline numbers"
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir():
        print(f"error: root not found: {root}", file=sys.stderr)
        return 2
    target_dir = (
        Path(args.target) if Path(args.target).is_absolute() else root / args.target
    )
    if not target_dir.is_dir():
        print(f"error: target not found: {args.target}", file=sys.stderr)
        return 2
    report = audit(
        root, args.target, split_loc=args.split_loc, max_depth=args.max_depth
    )
    sys.stdout.write(
        "\n".join(["PTC usage audit", *headline(report)]) + "\n"
        if args.quiet
        else render_summary(report)
    )
    generated = render_markdown(report)
    if args.check is not None:
        current = (
            args.check.read_text(encoding="utf-8") if args.check.is_file() else None
        )
        if current is None or merge_into(current, generated) != current:
            print(f"stale: {args.check} (rerun with --write)", file=sys.stderr)
            return 1
        return 0
    if args.write is not None:
        current = (
            args.write.read_text(encoding="utf-8") if args.write.is_file() else None
        )
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(merge_into(current, generated), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
