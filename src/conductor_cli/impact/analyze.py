"""Resolve calls and report the blast radius of a file or symbol."""

from __future__ import annotations

import posixpath
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from .index import ImpactIndex
from .model import (
    AMBIGUOUS_CAP,
    CONF_CTOR,
    CONF_GLOBAL,
    CONF_IMPORT,
    CONF_IMPORT_FILE,
    CONF_LOCAL,
    CONF_METHOD,
    CONF_MODULE_ATTR,
    CONF_SELF,
    CONF_STAR,
    CONF_TEST_IMPORT,
    CONF_TEST_NAME,
    DECAY,
    MAX_DEPTH,
    Call,
    Import,
    ImpactError,
    Symbol,
    is_test_path,
    local_name,
    module_keys,
    package_parts,
    symbol_public,
)

_TS_KINDS = frozenset({"named", "default", "namespace", "side_effect", "type"})
_SIMPLE_KINDS = frozenset({"function", "class"})


@dataclass(frozen=True)
class Binding:
    symbols: tuple[Symbol, ...] = ()
    modules: tuple[str, ...] = ()
    module_names: tuple[str, ...] = ()
    line: int = 0
    column: int = 0


@dataclass(frozen=True)
class Edge:
    callee: str
    caller: str
    path: str
    line: int
    end_line: int
    call_line: int
    confidence: float
    callee_name: str = ""
    receiver: str = ""


@dataclass(frozen=True)
class Ambiguity:
    path: str
    caller: str
    line: int
    name: str
    candidates: tuple[Symbol, ...]


@dataclass(frozen=True)
class Graph:
    reverse: dict[str, list[Edge]]
    ambiguous: tuple[Ambiguity, ...]


class _Unique:
    def __init__(self, symbol: Symbol, confidence: float) -> None:
        self.symbol = symbol
        self.confidence = confidence


class _Ambiguous:
    def __init__(self, symbols: list[Symbol]) -> None:
        self.symbols = symbols


class Catalog:
    def __init__(
        self,
        files: list[str],
        symbols: list[Symbol],
        calls: list[Call],
        imports: list[Import],
    ) -> None:
        self.files = files
        self.symbols = symbols
        self.calls = calls
        self.imports = imports
        self.by_qual = {item.qualified_name: item for item in symbols}
        self.by_name: dict[str, list[Symbol]] = defaultdict(list)
        self.by_path: dict[str, list[Symbol]] = defaultdict(list)
        self.by_parent: dict[tuple[str, str], list[Symbol]] = defaultdict(list)
        for item in symbols:
            self.by_name[item.name].append(item)
            self.by_path[item.path].append(item)
            self.by_parent[(item.path, item.parent)].append(item)
        self.imports_by_path: dict[str, list[Import]] = defaultdict(list)
        for item in imports:
            self.imports_by_path[item.path].append(item)
        self.module_to_files: dict[str, list[str]] = defaultdict(list)
        for path in files:
            for key in module_keys(path):
                self.module_to_files[key].append(path)
        self._file_set = set(files)
        self._graph: Graph | None = None
        self._binding_cache: dict[
            tuple[str, str, tuple[int, int] | None],
            tuple[dict[str, Binding], tuple[str, ...]],
        ] = {}
        self._scoped_cache: dict[
            tuple[str, str, tuple[int, int] | None], dict[str, dict[str, Binding]]
        ] = {}
        self._file_cache: dict[tuple[str, str, int, str, str], tuple[str, ...]] = {}

    def graph(self) -> Graph:
        if self._graph is None:
            self._graph = _build_graph(self)
        return self._graph

    def bindings(
        self, path: str, caller: str, at: tuple[int, int] | None = None
    ) -> tuple[dict[str, Binding], tuple[str, ...]]:
        key = (path, caller, at)
        cached = self._binding_cache.get(key)
        if cached is not None:
            return cached
        chain = scope_chain(caller, path)
        visible = set(chain)
        by_scope: dict[str, dict[str, Binding]] = {}
        stars: list[str] = []
        for imp in self.imports_by_path.get(path, []):
            if imp.kind == "type" or imp.scope not in visible:
                continue
            # An immediate statement only sees imports executed above it; a
            # later import of the same name cannot claim the call.
            if at is not None and (imp.line, imp.column) > at:
                continue
            if imp.kind == "star":
                stars.extend(self.resolve_files(imp))
                continue
            if imp.kind == "side_effect":
                continue
            bound = local_name(imp)
            if not bound:
                continue
            by_scope.setdefault(imp.scope, {})[bound] = replace(
                _binding_for(self, imp), line=imp.line, column=imp.column
            )
        # Outer scopes first, so a function-local import hides a module import.
        names: dict[str, Binding] = {}
        for scope in reversed(chain):
            names.update(by_scope.get(scope, {}))
        found = (names, tuple(dict.fromkeys(stars)))
        self._binding_cache[key] = found
        self._scoped_cache[key] = by_scope
        return found

    def scoped_bindings(
        self, path: str, caller: str, at: tuple[int, int] | None = None
    ) -> dict[str, dict[str, Binding]]:
        self.bindings(path, caller, at)
        return self._scoped_cache[(path, caller, at)]

    def resolve_files(self, imp: Import) -> tuple[str, ...]:
        key = (imp.kind, imp.module, imp.level, imp.path, imp.name)
        cached = self._file_cache.get(key)
        if cached is not None:
            return cached
        if imp.kind in _TS_KINDS or imp.module.startswith("."):
            found = tuple(_resolve_ts(self, imp))
        else:
            found = tuple(_resolve_python_module(self, imp.module, imp.level, imp.path))
        self._file_cache[key] = found
        return found


def impact_report(
    catalog: Catalog,
    root: Path,
    targets: list[str],
    *,
    depth: int = 5,
    index_stats: dict[str, int] | None = None,
) -> dict[str, object]:
    """Return affected functions, files, tests, and ambiguous name collisions."""
    depth = max(1, min(depth, MAX_DEPTH))
    seeds, ambiguous_targets = _resolve_targets(catalog, root, targets)
    if not seeds:
        note = (
            "name collision; pass a file or a qualified name such as path::symbol"
            if ambiguous_targets
            else "no functions, methods, or classes in the selected files"
        )
        return {
            "version": 1,
            "mode": "impact",
            "confidence": 0.0 if ambiguous_targets else 1.0,
            "targets": [],
            "ambiguous": ambiguous_targets,
            "affected_functions": [],
            "affected_files": [],
            "affected_tests": [],
            "index": index_stats or {},
            "note": note,
        }
    report = _impact_of(catalog, seeds, depth=depth)
    if ambiguous_targets:
        report["ambiguous"] = [*ambiguous_targets, *report["ambiguous"]]
        report["confidence"] = min(float(report["confidence"]), AMBIGUOUS_CAP)
    report["index"] = index_stats or {}
    report["mode"] = "impact"
    return report


def load_catalog(index: ImpactIndex) -> Catalog:
    files, symbols, calls, imports = index.load_facts()
    return Catalog(files, symbols, calls, imports)


def impact_of_symbols(catalog: Catalog, symbols: list[Symbol], *, depth: int = 1) -> dict[str, object]:
    """Blast radius of symbols that are already resolved."""
    return _impact_of(catalog, symbols, depth=max(1, min(depth, MAX_DEPTH)))


def direct_edges(catalog: Catalog, qualified_name: str) -> list[Edge]:
    return list(catalog.graph().reverse.get(qualified_name, []))


def call_owner(caller: str) -> str:
    """Symbol a call belongs to.

    Class-body execution is stored as ``Qual#body`` so resolution can see that
    body's imports. The class itself is the caller the report names.
    """
    if caller.endswith("#body"):
        return caller[: -len("#body")]
    return caller


def _impact_of(catalog: Catalog, seeds: list[Symbol], *, depth: int) -> dict[str, object]:
    graph = catalog.graph()
    affected: dict[str, dict[str, object]] = {}
    module_hits: list[dict[str, object]] = []
    queue: deque[tuple[str, int]] = deque()
    queued: set[str] = set()
    for seed in _dedupe(seeds):
        affected[seed.qualified_name] = _function_row(seed, 1.0, 0, "target")
        queue.append((seed.qualified_name, 0))
        queued.add(seed.qualified_name)
    while queue:
        qual, current = queue.popleft()
        if current >= depth:
            continue
        for edge in graph.reverse.get(qual, []):
            confidence = round(edge.confidence * (DECAY ** current), 4)
            if not edge.caller:
                module_hits.append(
                    {
                        "path": edge.path,
                        "line": edge.line,
                        "confidence": confidence,
                        "reason": f"module-level call to {qual}",
                    }
                )
                continue
            symbol = catalog.by_qual.get(edge.caller)
            if symbol is None:
                continue
            previous = affected.get(symbol.qualified_name)
            if previous is None or confidence > float(previous["confidence"]):
                affected[symbol.qualified_name] = _function_row(
                    symbol, confidence, current + 1, f"calls {qual}"
                )
            if symbol.qualified_name not in queued:
                queued.add(symbol.qualified_name)
                queue.append((symbol.qualified_name, current + 1))

    functions = sorted(
        affected.values(),
        key=lambda item: (-float(item["confidence"]), str(item["path"]), int(item["line"])),
    )
    files = _affected_files(catalog, seeds, functions, module_hits)
    tests = _affected_tests(catalog, seeds, functions, module_hits, files)
    interesting = {str(item["qualified_name"]) for item in functions}
    ambiguous = [
        _ambiguity_public(item)
        for item in graph.ambiguous
        if {symbol.qualified_name for symbol in item.candidates} & interesting
    ]
    direct = [float(item["confidence"]) for item in functions if int(item["depth"]) >= 1]
    confidence = min(direct) if direct else 1.0
    if ambiguous:
        confidence = min(confidence, AMBIGUOUS_CAP)
    return {
        "version": 1,
        "confidence": round(confidence, 4),
        "targets": [_target_row(seed) for seed in _dedupe(seeds)],
        "ambiguous": ambiguous,
        "affected_functions": functions,
        "affected_files": files,
        "affected_tests": tests,
    }


def _function_row(symbol: Symbol, confidence: float, depth: int, reason: str) -> dict[str, object]:
    return {
        "qualified_name": symbol.qualified_name,
        "name": symbol.name,
        "path": symbol.path,
        "line": symbol.start_line,
        "kind": symbol.kind,
        "confidence": confidence,
        "depth": depth,
        "reason": reason,
    }


def _target_row(symbol: Symbol) -> dict[str, object]:
    row = symbol_public(symbol)
    row["input"] = symbol.qualified_name
    return row


def _affected_files(
    catalog: Catalog,
    seeds: list[Symbol],
    functions: list[dict[str, object]],
    module_hits: list[dict[str, object]],
) -> list[dict[str, object]]:
    found: dict[str, dict[str, object]] = {}

    def keep(path: str, confidence: float, reason: str) -> None:
        previous = found.get(path)
        if previous is None or confidence > float(previous["confidence"]):
            found[path] = {"path": path, "confidence": confidence, "reason": reason}

    for item in functions:
        keep(str(item["path"]), float(item["confidence"]), f"defines {item['qualified_name']}")
    for item in module_hits:
        keep(str(item["path"]), float(item["confidence"]), str(item["reason"]))
    for seed in seeds:
        for path, confidence, reason in _importers(catalog, seed):
            keep(path, confidence, reason)
    return sorted(found.values(), key=lambda item: (-float(item["confidence"]), str(item["path"])))


def _affected_tests(
    catalog: Catalog,
    seeds: list[Symbol],
    functions: list[dict[str, object]],
    module_hits: list[dict[str, object]],
    files: list[dict[str, object]],
) -> list[dict[str, object]]:
    found: dict[str, dict[str, object]] = {}

    def keep(path: str, qual: str, line: int, confidence: float, reason: str) -> None:
        previous = found.get(path)
        if previous is None or confidence > float(previous["confidence"]):
            found[path] = {
                "path": path,
                "qualified_name": qual,
                "line": line,
                "confidence": confidence,
                "reason": reason,
            }

    for item in functions:
        qual = str(item["qualified_name"])
        path = str(item["path"])
        if is_test_path(path) or str(item["name"]).startswith("test_"):
            keep(path, qual, int(item["line"]), float(item["confidence"]), str(item["reason"]))
    for item in module_hits:
        path = str(item["path"])
        if is_test_path(path):
            keep(path, "", int(item["line"]), float(item["confidence"]), str(item["reason"]))
    for item in files:
        path = str(item["path"])
        if is_test_path(path):
            keep(path, "", 1, float(item["confidence"]), str(item["reason"]))
    for seed in seeds:
        stem = PurePosixPath(seed.path).stem
        if stem == "__init__":
            stem = PurePosixPath(seed.path).parent.name
        names = {
            f"test_{stem}.py",
            f"{stem}_test.py",
            f"{stem}.test.ts",
            f"{stem}.test.tsx",
            f"{stem}.spec.ts",
            f"{stem}.spec.tsx",
        }
        for path in catalog.files:
            if PurePosixPath(path).name in names and is_test_path(path):
                keep(path, "", 1, CONF_TEST_NAME, f"test file name matches {stem}")
    return sorted(found.values(), key=lambda item: (-float(item["confidence"]), str(item["path"])))


def _importers(catalog: Catalog, symbol: Symbol) -> list[tuple[str, float, str]]:
    found: list[tuple[str, float, str]] = []
    for path, imports in catalog.imports_by_path.items():
        if path == symbol.path:
            continue
        for imp in imports:
            if imp.kind == "type" or symbol.path not in catalog.resolve_files(imp):
                continue
            specific = _import_names_symbol(imp, symbol)
            broad = imp.kind in {"module", "namespace", "side_effect", "star"}
            if not specific and not broad:
                continue
            if is_test_path(path):
                confidence = CONF_TEST_IMPORT
            else:
                confidence = CONF_IMPORT_FILE
            reason = (
                f"imports {symbol.qualified_name}"
                if specific
                else f"imports module of {symbol.qualified_name}"
            )
            found.append((path, confidence, reason))
            break
    return found


def _import_names_symbol(imp: Import, symbol: Symbol) -> bool:
    # An import binds module-level names only; a method or nested symbol is
    # reached through its parent, never imported by its own name.
    if symbol.parent:
        return False
    if imp.kind == "star":
        return True
    if imp.kind in {"from", "named"} and imp.name == symbol.name:
        return True
    if imp.kind == "default" and symbol.exported == "default":
        return True
    return False


def _resolve_targets(
    catalog: Catalog, root: Path, targets: list[str]
) -> tuple[list[Symbol], list[dict[str, object]]]:
    seeds: list[Symbol] = []
    ambiguous: list[dict[str, object]] = []
    indexed = set(catalog.files)
    for raw in targets:
        if "::" in raw:
            symbol = catalog.by_qual.get(raw)
            if symbol is None:
                raise ImpactError(f"unknown symbol: {raw}")
            seeds.append(symbol)
            continue
        files = _match_files(root, raw, indexed)
        if files:
            for rel in files:
                seeds.extend(
                    item
                    for item in catalog.by_path.get(rel, [])
                    if item.kind in {"function", "method", "class"}
                )
            continue
        matches = [
            item
            for item in catalog.by_name.get(raw, [])
            if item.kind in {"function", "method", "class"}
        ]
        if len(matches) == 1:
            seeds.append(matches[0])
        elif not matches:
            raise ImpactError(f"unknown file or symbol: {raw}")
        else:
            ordered = sorted(matches, key=lambda item: (item.path, item.start_line, item.qualified_name))
            ambiguous.append(
                {
                    "input": raw,
                    "reason": "name collision",
                    "candidates": [symbol_public(item) for item in ordered],
                }
            )
    return _dedupe(seeds), ambiguous


def _match_files(root: Path, raw: str, indexed: set[str]) -> list[str]:
    path = Path(raw)
    candidates: list[str] = []
    if path.is_absolute():
        try:
            candidates.append(path.resolve().relative_to(root).as_posix())
        except ValueError:
            return []
    else:
        candidates.append(PurePosixPath(raw).as_posix())
        try:
            candidates.append((Path.cwd() / path).resolve().relative_to(root).as_posix())
        except ValueError:
            pass
    for rel in candidates:
        if rel in indexed:
            return [rel]
        if (root / rel).is_dir():
            prefix = rel.rstrip("/") + "/"
            return sorted(item for item in indexed if item.startswith(prefix))
    return []


def _dedupe(symbols: list[Symbol]) -> list[Symbol]:
    seen: set[str] = set()
    ordered: list[Symbol] = []
    for symbol in symbols:
        if symbol.qualified_name in seen:
            continue
        seen.add(symbol.qualified_name)
        ordered.append(symbol)
    return ordered


def _build_graph(catalog: Catalog) -> Graph:
    reverse: dict[str, list[Edge]] = defaultdict(list)
    ambiguous: list[Ambiguity] = []
    for call in catalog.calls:
        resolved = _resolve_call(catalog, call)
        if resolved is None:
            continue
        if isinstance(resolved, _Ambiguous):
            if resolved.symbols:
                ambiguous.append(
                    Ambiguity(
                        call.path,
                        call_owner(call.caller),
                        call.line,
                        call.callee_name,
                        tuple(resolved.symbols),
                    )
                )
            continue
        owner = call_owner(call.caller)
        caller = catalog.by_qual.get(owner) if owner else None
        reverse[resolved.symbol.qualified_name].append(
            Edge(
                callee=resolved.symbol.qualified_name,
                caller=owner,
                path=call.path,
                line=caller.start_line if caller is not None else call.line,
                end_line=caller.end_line if caller is not None else call.line,
                call_line=call.line,
                confidence=resolved.confidence,
                callee_name=call.callee_name,
                receiver=call.receiver,
            )
        )
    return Graph(reverse, tuple(ambiguous))


def _binding_position(call: Call) -> tuple[int, int] | None:
    """The statement position whose bindings have executed, if immediate.

    A module-level statement and a class body execute in order, so only the
    bindings above them count (columns order same-line statements). A call
    inside a function or method is deferred: it may run after every module or
    class statement.
    """
    if not call.caller or call.caller.endswith("#body"):
        return (call.line, call.column)
    return None


def _latest_binding(call: Call, hits, binding):
    """Pick the latest binding in one scope, honoring statement order.

    Definitions and imports both bind a name; the last one executed wins.
    Immediate statements only see bindings above them (columns order same-line
    statements), while deferred calls see the final binding in each scope.
    """
    position = _binding_position(call)
    definitions = [
        item
        for item in hits
        if position is None or (item.start_line, item.column) <= position
    ]
    if (
        binding is not None
        and position is not None
        and (binding.line, binding.column) > position
    ):
        binding = None
    latest_def = max(
        definitions, key=lambda item: (item.start_line, item.column), default=None
    )
    if latest_def is None:
        return None, binding
    if binding is None:
        return latest_def, None
    if (binding.line, binding.column) >= (latest_def.start_line, latest_def.column):
        return None, binding
    return latest_def, None


def _resolve_call(catalog: Catalog, call: Call) -> _Unique | _Ambiguous | None:
    _, stars = catalog.bindings(call.path, call.caller, _binding_position(call))
    if call.receiver_kind in {"self", "cls", "this"}:
        return _resolve_self(catalog, call)
    if call.receiver_kind == "ctor":
        classes = _resolve_simple(catalog, call, call.receiver, frozenset({"class"}), stars)
        return _methods_of(catalog, classes, call.callee_name, CONF_CTOR)
    if call.receiver_kind in {"ident", "attr"}:
        return _resolve_attribute(catalog, call)
    return _resolve_simple(catalog, call, call.callee_name, _SIMPLE_KINDS, stars)


def _resolve_self(catalog: Catalog, call: Call) -> _Unique | _Ambiguous | None:
    if not call.enclosing_class:
        return None
    hits = [
        item
        for item in catalog.by_parent.get((call.path, call.enclosing_class), [])
        if item.name == call.callee_name and item.kind == "method"
    ]
    if len(hits) == 1:
        return _Unique(hits[0], CONF_SELF)
    if len(hits) > 1:
        return _Ambiguous(hits)
    return None


def _resolve_attribute(catalog: Catalog, call: Call) -> _Unique | _Ambiguous | None:
    if call.receiver_kind == "ident":
        classes, module_paths = _ident_receiver(catalog, call)
    else:
        classes = []
        module_paths = _files_for_receiver(catalog, call)
    methods: list[Symbol] = []
    for item in classes:
        methods.extend(_methods_named(catalog, item, call.callee_name))
    functions: list[Symbol] = []
    for path in module_paths:
        functions.extend(
            item
            for item in catalog.by_parent.get((path, ""), [])
            if item.name == call.callee_name and item.kind in _SIMPLE_KINDS
        )
    if methods and not functions:
        if len(methods) == 1:
            return _Unique(methods[0], CONF_METHOD)
        return _Ambiguous(methods)
    if functions and not methods:
        if len(functions) == 1:
            return _Unique(functions[0], CONF_MODULE_ATTR)
        return _Ambiguous(functions)
    if methods and functions:
        return _Ambiguous([*methods, *functions])
    return None


def _methods_of(
    catalog: Catalog,
    classes: _Unique | _Ambiguous | None,
    name: str,
    confidence: float,
) -> _Unique | _Ambiguous | None:
    if classes is None:
        return None
    symbols = [classes.symbol] if isinstance(classes, _Unique) else classes.symbols
    methods: list[Symbol] = []
    for item in symbols:
        methods.extend(_methods_named(catalog, item, name))
    if len(methods) == 1:
        return _Unique(methods[0], confidence)
    if len(methods) > 1:
        return _Ambiguous(methods)
    return None


def _methods_named(catalog: Catalog, symbol: Symbol, name: str) -> list[Symbol]:
    return [
        item
        for item in catalog.by_parent.get((symbol.path, symbol.qualified_name), [])
        if item.name == name and item.kind == "method"
    ]


def _resolve_simple(
    catalog: Catalog,
    call: Call,
    name: str,
    kinds: frozenset[str],
    stars: tuple[str, ...],
) -> _Unique | _Ambiguous | None:
    if not name:
        return None
    # A name in an inner scope hides every outer def and import. A function-local
    # import therefore wins over a module-level function of the same name.
    scoped = catalog.scoped_bindings(call.path, call.caller, _binding_position(call))
    for parent in scope_chain(call.caller, call.path):
        hits = [
            item
            for item in catalog.by_parent.get((call.path, parent), [])
            if item.name == name and item.kind in kinds
        ]
        binding = scoped.get(parent, {}).get(name)
        chosen_def, chosen_binding = _latest_binding(call, hits, binding)
        if chosen_def is not None:
            same_name = [item for item in hits if item.start_line == chosen_def.start_line]
            if len(same_name) == 1:
                return _Unique(chosen_def, CONF_LOCAL)
            return _Ambiguous(same_name)
        if chosen_binding is not None:
            return _resolve_binding(chosen_binding, kinds)
        if hits or binding is not None:
            return None
        if _parameter_hides(catalog, call.path, parent, name):
            return None
    starred: list[Symbol] = []
    for path in stars:
        starred.extend(
            item
            for item in catalog.by_parent.get((path, ""), [])
            if item.name == name and item.kind in kinds
        )
    if len(starred) == 1:
        return _Unique(starred[0], CONF_STAR)
    if len(starred) > 1:
        return _Ambiguous(starred)
    hits = [
        item
        for item in catalog.by_name.get(name, [])
        if item.parent == "" and item.kind in kinds
    ]
    if len(hits) == 1:
        return _Unique(hits[0], CONF_GLOBAL)
    if len(hits) > 1:
        return _Ambiguous(hits)
    return None


def _resolve_binding(binding: Binding, kinds: frozenset[str]) -> _Unique | _Ambiguous | None:
    symbols = [item for item in binding.symbols if item.kind in kinds]
    if len(symbols) == 1 and not binding.modules:
        return _Unique(symbols[0], CONF_IMPORT)
    if len(symbols) > 1 or (symbols and binding.modules):
        return _Ambiguous(symbols)
    # The name is bound here even when the target is outside the index.
    return None


def _ident_receiver(catalog: Catalog, call: Call) -> tuple[list[Symbol], list[str]]:
    """Resolve `Name.attr` from the inside of the scope chain outward.

    A class or function defined in a scope hides an import of that name in the
    same scope and every scope outside it. A function-local import still hides
    a module-level class, because the inner scope is visited first.
    """
    name = call.receiver
    scoped = catalog.scoped_bindings(call.path, call.caller, _binding_position(call))
    for parent in scope_chain(call.caller, call.path):
        defined = [
            item
            for item in catalog.by_parent.get((call.path, parent), [])
            if item.name == name and item.kind in {"function", "class"}
        ]
        binding = scoped.get(parent, {}).get(name)
        chosen_def, chosen_binding = _latest_binding(call, defined, binding)
        if chosen_def is not None:
            return [chosen_def] if chosen_def.kind == "class" else [], []
        if chosen_binding is not None:
            classes = [item for item in chosen_binding.symbols if item.kind == "class"]
            return classes, list(chosen_binding.modules)
        if defined or binding is not None:
            return [], []
        if _parameter_hides(catalog, call.path, parent, name):
            return [], []
    return [], []


def module_receiver(catalog: Catalog, call: Call) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Module files and dotted names a receiver refers to.

    The file tuple is empty after that module is deleted. The dotted name
    still identifies it. A class, function, or parameter that hides the
    receiver is not a module.
    """
    if call.receiver_kind == "ident":
        return _ident_module_binding(catalog, call)
    if call.receiver_kind == "attr":
        return _attr_module_binding(catalog, call)
    return (), ()


def import_mentions_file(catalog: Catalog, imp: Import, path: str) -> bool:
    """Whether ``imp`` refers to ``path``, including after that file is deleted."""
    if imp.kind == "type":
        return False
    resolved = catalog.resolve_files(imp)
    if resolved:
        return path in resolved
    if imp.kind in _TS_KINDS or imp.module.startswith("."):
        return path in _ts_candidates(imp)
    key = _python_module_key(imp.module, imp.level, imp.path)
    return bool(key) and key in set(module_keys(path))


def _ident_module_binding(catalog: Catalog, call: Call) -> tuple[tuple[str, ...], tuple[str, ...]]:
    name = call.receiver
    scoped = catalog.scoped_bindings(call.path, call.caller, _binding_position(call))
    for parent in scope_chain(call.caller, call.path):
        defined = [
            item
            for item in catalog.by_parent.get((call.path, parent), [])
            if item.name == name and item.kind in {"function", "class", "parameter"}
        ]
        binding = scoped.get(parent, {}).get(name)
        chosen_def, chosen_binding = _latest_binding(call, defined, binding)
        if chosen_def is not None:
            return (), ()
        if chosen_binding is None:
            if defined or binding is not None:
                return (), ()
            continue
        binding = chosen_binding
        if any(item.kind == "class" for item in binding.symbols):
            return (), ()
        # A from-import of a function is not a module. A submodule import has
        # module files, or only a dotted name once that file is gone.
        if binding.symbols and not binding.modules:
            return (), ()
        return tuple(binding.modules), tuple(binding.module_names)
    return (), ()


def _attr_module_binding(catalog: Catalog, call: Call) -> tuple[tuple[str, ...], tuple[str, ...]]:
    root = call.receiver.split(".", 1)[0]
    binding = _scoped_root_binding(catalog, call, root)
    if binding is None or any(item.kind == "class" for item in binding.symbols):
        return (), ()
    files = tuple(_files_for_receiver(catalog, call))
    for mod_name in binding.module_names:
        if call.receiver == mod_name or call.receiver.startswith(mod_name + "."):
            return files, (call.receiver,)
    return files, ()


def local_hides(catalog: Catalog, call: Call, name: str) -> bool:
    """True when a def or parameter in a nearer scope than any import owns `name`."""
    scoped = catalog.scoped_bindings(call.path, call.caller, _binding_position(call))
    for parent in scope_chain(call.caller, call.path):
        defined = [
            item
            for item in catalog.by_parent.get((call.path, parent), [])
            if item.name == name and item.kind in {"function", "class"}
        ]
        binding = scoped.get(parent, {}).get(name)
        chosen_def, chosen_binding = _latest_binding(call, defined, binding)
        if chosen_def is not None:
            return True
        if chosen_binding is not None:
            return False
        if defined or binding is not None:
            return True
        if _parameter_hides(catalog, call.path, parent, name):
            return True
    return False


def _parameter_hides(catalog: Catalog, path: str, parent: str, name: str) -> bool:
    if not parent or not name:
        return False
    return any(
        item.kind == "parameter" and item.name == name
        for item in catalog.by_parent.get((path, parent), [])
    )


def _scoped_root_binding(catalog: Catalog, call: Call, name: str) -> Binding | None:
    """Import binding of a receiver's root name, after nearer scopes hide it.

    A def hides every import of that name. A function-local import hides a
    parameter in the same scope. A parameter hides imports further out, so
    ``def run(pkg): return pkg.lib.helper()`` is not a call through ``import pkg.lib``.
    """
    scoped = catalog.scoped_bindings(call.path, call.caller, _binding_position(call))
    for parent in scope_chain(call.caller, call.path):
        defined = [
            item
            for item in catalog.by_parent.get((call.path, parent), [])
            if item.name == name and item.kind in {"function", "class"}
        ]
        binding = scoped.get(parent, {}).get(name)
        chosen_def, chosen_binding = _latest_binding(call, defined, binding)
        if chosen_def is not None:
            return None
        if chosen_binding is not None:
            return chosen_binding
        if defined or binding is not None:
            return None
        if _parameter_hides(catalog, call.path, parent, name):
            return None
    return None


def _files_for_receiver(catalog: Catalog, call: Call) -> list[str]:
    if call.receiver_kind != "attr":
        return []
    root = call.receiver.split(".", 1)[0]
    binding = _scoped_root_binding(catalog, call, root)
    if binding is None:
        return []
    for mod_name in binding.module_names:
        if call.receiver == mod_name:
            return list(binding.modules)
        prefix = mod_name + "."
        if call.receiver.startswith(prefix):
            return list(catalog.module_to_files.get(call.receiver, []))
    return []


def _binding_for(catalog: Catalog, imp: Import) -> Binding:
    files = catalog.resolve_files(imp)
    if imp.kind in {"module", "namespace"}:
        return Binding((), files, (imp.module,) if imp.module else ())
    if imp.kind == "default":
        symbols: list[Symbol] = []
        for path in files:
            defaults = [
                item
                for item in catalog.by_parent.get((path, ""), [])
                if item.exported == "default"
            ]
            if defaults:
                symbols.extend(defaults)
                continue
            top = [
                item
                for item in catalog.by_parent.get((path, ""), [])
                if item.kind in _SIMPLE_KINDS
            ]
            if len(top) == 1:
                symbols.extend(top)
        return Binding(tuple(symbols), (), ())
    if imp.kind in {"from", "named"}:
        symbols = []
        for path in files:
            symbols.extend(
                item
                for item in catalog.by_parent.get((path, ""), [])
                if item.name == imp.name and item.kind in _SIMPLE_KINDS
            )
        modules: list[str] = []
        module_names: list[str] = []
        if imp.kind == "from" and imp.name:
            sub_name = _submodule_name(imp)
            if sub_name:
                modules.extend(catalog.module_to_files.get(sub_name, []))
                module_names.append(sub_name)
        return Binding(tuple(symbols), tuple(modules), tuple(module_names))
    return Binding()


def _submodule_name(imp: Import) -> str:
    if imp.level:
        base = package_parts(imp.path)
        drop = imp.level - 1
        if drop > len(base):
            return ""
        parts = list(base[: len(base) - drop])
        if imp.module:
            parts.extend(imp.module.split("."))
        if imp.name:
            parts.append(imp.name)
        return ".".join(parts)
    if imp.module and imp.name:
        return f"{imp.module}.{imp.name}"
    return ""


def _resolve_python_module(catalog: Catalog, module: str, level: int, importer: str) -> list[str]:
    key = _python_module_key(module, level, importer)
    if not key:
        return []
    return list(catalog.module_to_files.get(key, []))


def _python_module_key(module: str, level: int, importer: str) -> str:
    if level:
        base = package_parts(importer)
        drop = level - 1
        if drop > len(base):
            return ""
        parts = list(base[: len(base) - drop])
        if module:
            parts.extend(module.split("."))
        return ".".join(parts)
    return module


def _resolve_ts(catalog: Catalog, imp: Import) -> list[str]:
    return [path for path in _ts_candidates(imp) if path in catalog._file_set]


def _ts_candidates(imp: Import) -> list[str]:
    spec = imp.module
    if not spec.startswith("."):
        return []
    base = PurePosixPath(imp.path).parent.as_posix()
    if base == ".":
        joined = posixpath.normpath(spec)
    else:
        joined = posixpath.normpath(posixpath.join(base, spec))
    if joined.startswith("../") or joined == "..":
        return []
    return _source_candidates(joined)


def _source_candidates(joined: str) -> list[str]:
    stem = joined
    for suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return [
        stem + ".ts",
        stem + ".tsx",
        stem + ".py",
        stem + "/index.ts",
        stem + "/index.tsx",
        stem + "/index.py",
    ]


def scope_chain(caller: str, path: str) -> list[str]:
    """Enclosing scopes of `caller`, innermost first; the module scope is last."""
    if not caller or "::" not in caller:
        return [""]
    _, qual = caller.split("::", 1)
    parts = [part for part in qual.split(".") if part]
    chain: list[str] = []
    for index in range(len(parts), -1, -1):
        if index == 0:
            chain.append("")
        else:
            chain.append(f"{path}::{'.'.join(parts[:index])}")
    return chain


def _ambiguity_public(item: Ambiguity) -> dict[str, object]:
    ordered = sorted(item.candidates, key=lambda symbol: (symbol.path, symbol.start_line, symbol.qualified_name))
    return {
        "name": item.name,
        "path": item.path,
        "caller": item.caller,
        "line": item.line,
        "reason": "name collision",
        "candidates": [symbol_public(symbol) for symbol in ordered],
    }
