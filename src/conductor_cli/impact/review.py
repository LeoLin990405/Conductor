"""Review helper: flag changed symbols whose callers were left untouched and untested."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .analyze import (
    Catalog,
    Edge,
    Graph,
    call_owner,
    direct_edges,
    impact_of_symbols,
    import_mentions_file,
    load_catalog,
    module_receiver,
    scope_chain,
)
from .extract import extract_path
from .index import ImpactIndex
from .model import (
    CONF_REMOVED,
    Call,
    Import,
    ImpactError,
    Symbol,
    is_test_path,
    language_for_path,
    local_name,
    module_keys,
    symbol_public,
)

_REV = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@^{}~+-]*$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_AMBIGUOUS_EDGE = 0.25
_TRACKED_KINDS = frozenset({"function", "method", "class"})
_GIT_SIMPLE_ESCAPE = {
    ord("\\"): ord("\\"),
    ord('"'): ord('"'),
    ord("a"): 0x07,
    ord("b"): 0x08,
    ord("f"): 0x0C,
    ord("n"): 0x0A,
    ord("r"): 0x0D,
    ord("t"): 0x09,
    ord("v"): 0x0B,
}


@dataclass
class DiffFile:
    old_path: str | None = None
    new_path: str | None = None
    old_lines: set[int] = field(default_factory=set)
    new_lines: set[int] = field(default_factory=set)


class GitReader(Protocol):
    def diff(self, spec: str) -> str: ...

    def show(self, rev: str, path: str) -> bytes | None: ...

    def merge_base(self, base: str, head: str) -> str: ...


class SubprocessGit:
    """Read-only git diff and blob lookup. Does not fetch or write."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def diff(self, spec: str) -> str:
        spec = parse_range(spec)
        if "..." in spec:
            args = [
                "diff",
                "--unified=0",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                "--find-renames",
                spec,
            ]
        else:
            base, head = spec.split("..", 1)
            args = [
                "diff",
                "--unified=0",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                "--find-renames",
                base,
                head,
            ]
        proc = self._run(args)
        if proc.returncode != 0:
            message = proc.stderr.decode("utf-8", "replace").strip()
            raise ImpactError(message[:400] or "git diff failed")
        return proc.stdout.decode("utf-8", "replace")

    def show(self, rev: str, path: str) -> bytes | None:
        if not _REV.match(rev):
            raise ImpactError(f"invalid revision: {rev}")
        if path.startswith("-") or "\x00" in path:
            raise ImpactError("invalid path")
        proc = self._run(["show", f"{rev}:{path}"])
        if proc.returncode != 0:
            return None
        return proc.stdout

    def merge_base(self, base: str, head: str) -> str:
        for rev in (base, head):
            if not _REV.match(rev):
                raise ImpactError(f"invalid revision: {rev}")
        proc = self._run(["merge-base", base, head])
        if proc.returncode != 0:
            message = proc.stderr.decode("utf-8", "replace").strip()
            raise ImpactError(message[:400] or "git merge-base failed")
        sha = proc.stdout.decode("utf-8", "replace").strip()
        if not _REV.match(sha):
            raise ImpactError("git merge-base failed")
        return sha

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[bytes]:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C",
        }
        return subprocess.run(
            ["git", "-C", str(self.root), "-c", "core.quotepath=true", *args],
            check=False,
            capture_output=True,
            env=env,
        )


def parse_range(spec: str) -> str:
    raw = spec.strip()
    if "..." in raw:
        base, head = raw.split("...", 1)
        dotted = "..."
    elif ".." in raw:
        base, head = raw.split("..", 1)
        dotted = ".."
    else:
        raise ImpactError("review range must look like base..head")
    for part in (base, head):
        if not _REV.match(part):
            raise ImpactError(f"invalid revision: {part}")
    return f"{base}{dotted}{head}"


def parse_unified_diff(text: str) -> list[DiffFile]:
    files: list[DiffFile] = []
    current: DiffFile | None = None
    old_line = 0
    new_line = 0
    in_hunk = False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current = DiffFile()
            files.append(current)
            in_hunk = False
            continue
        if current is None:
            continue
        # `--- counter;` and `+++ counter;` are deleted and added statements.
        # Headers appear only before the first hunk of a file.
        if not in_hunk and line.startswith("--- "):
            current.old_path = _clean_diff_path(line[4:])
            continue
        if not in_hunk and line.startswith("+++ "):
            current.new_path = _clean_diff_path(line[4:])
            continue
        match = _HUNK.match(line)
        if match:
            old_line = int(match.group(1))
            new_line = int(match.group(3))
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+"):
            current.new_lines.add(new_line)
            new_line += 1
            continue
        if line.startswith("-"):
            current.old_lines.add(old_line)
            old_line += 1
            continue
        if line.startswith("\\"):
            continue
        if line.startswith(" "):
            old_line += 1
            new_line += 1
    return files


def review_range(
    root: Path,
    spec: str,
    *,
    index: ImpactIndex | None = None,
    depth: int = 5,
    git: GitReader | None = None,
) -> dict[str, object]:
    """Flag changed symbols with callers that were neither edited nor covered by a test edit."""
    spec = parse_range(spec)
    git = git or SubprocessGit(root)
    diff_files = [item for item in parse_unified_diff(git.diff(spec)) if _is_source_diff(item)]
    index = index or ImpactIndex(root)
    stats = index.update()
    catalog = load_catalog(index)
    base, head = _split_range(spec)
    # git diff A...B compares the merge base with B, not the tip of A.
    old_rev = git.merge_base(base, head) if "..." in spec else base
    old_symbols = _symbols_by_side(diff_files, old_rev, git, old=True)
    new_symbols = _symbols_by_side(diff_files, head, git, old=False)
    changed = _changed_symbols(diff_files, old_symbols, new_symbols)
    by_new = {item.new_path: item for item in diff_files if item.new_path}
    changed_paths = {
        path
        for item in diff_files
        for path in (item.old_path, item.new_path)
        if path
    }
    flags: list[dict[str, object]] = []
    ambiguous: list[dict[str, object]] = []
    changed_public: list[dict[str, object]] = []
    graph = catalog.graph()
    for symbol, removed in changed:
        if symbol.kind not in _TRACKED_KINDS:
            continue
        public = symbol_public(symbol)
        public["removed"] = removed
        changed_public.append(public)
        live = None if removed else catalog.by_qual.get(symbol.qualified_name)
        subject = live or symbol
        edges = [] if removed else direct_edges(catalog, subject.qualified_name)
        if removed:
            edges = _removed_edges(catalog, symbol)
        untouched: list[dict[str, object]] = []
        for edge in edges:
            if _edge_touched(catalog, edge, by_new, old_symbols):
                continue
            caller = catalog.by_qual.get(edge.caller) if edge.caller else None
            untouched.append(
                {
                    "qualified_name": caller.qualified_name if caller is not None else "",
                    "name": caller.name if caller is not None else "",
                    "path": edge.path,
                    "line": caller.start_line if caller is not None else edge.call_line,
                    "confidence": edge.confidence,
                }
            )
        extra_ambiguous = _ambiguous_for(catalog, graph, subject, by_new, old_symbols)
        ambiguous.extend(extra_ambiguous)
        if not untouched and extra_ambiguous:
            for item in extra_ambiguous:
                untouched.append(
                    {
                        "qualified_name": item["caller"],
                        "name": "",
                        "path": item["path"],
                        "line": item["line"],
                        "confidence": _AMBIGUOUS_EDGE,
                    }
                )
        report = impact_of_symbols(catalog, [subject], depth=depth)
        test_paths = {str(item["path"]) for item in report["affected_tests"]}
        tested = bool(test_paths & changed_paths) or _symbol_file_is_test_and_changed(subject, changed_paths)
        if untouched and not tested:
            confidence = min(float(item["confidence"]) for item in untouched)
            flags.append(
                {
                    "symbol": subject.qualified_name,
                    "path": subject.path,
                    "line": subject.start_line,
                    "kind": subject.kind,
                    "removed": removed,
                    "tested": False,
                    "confidence": confidence,
                    "untouched_callers": untouched,
                }
            )
    confidence = min((float(item["confidence"]) for item in flags), default=1.0)
    if ambiguous:
        confidence = min(confidence, 0.5)
    return {
        "version": 1,
        "mode": "review",
        "range": spec,
        "confidence": round(confidence, 4),
        "changed_files": sorted(changed_paths),
        "changed_symbols": changed_public,
        "flags": flags,
        "ambiguous": ambiguous,
        "index": stats,
    }


def _symbol_file_is_test_and_changed(symbol: Symbol, changed_paths: set[str]) -> bool:
    return is_test_path(symbol.path) and symbol.path in changed_paths


def _edge_touched(
    catalog: Catalog,
    edge: Edge,
    by_new: dict[str, DiffFile],
    old_symbols: dict[str, list[Symbol]],
) -> bool:
    diff = by_new.get(edge.path)
    if diff is None:
        return False
    caller = catalog.by_qual.get(edge.caller) if edge.caller else None
    if caller is not None:
        if _edits_symbol(caller, catalog.by_path.get(caller.path, []), diff.new_lines):
            return True
        # A pure deletion has no added lines. The caller's old span is what overlaps them.
        previous = _same_symbol(old_symbols.get(diff.old_path or "", []), caller)
        return previous is not None and _edits_symbol(
            previous, old_symbols.get(diff.old_path or "", []), diff.old_lines
        )
    return edge.call_line in diff.new_lines


def _edits_symbol(symbol: Symbol, symbols: list[Symbol], lines: set[int]) -> bool:
    """Whether a changed line belongs to this symbol's own body.

    A nested def sits inside its parent's span, so deleting or editing the
    child also overlaps the parent. That overlap is not an edit of the parent:
    when `inner()` disappears and `outer()` keeps its unchanged
    `return inner()`, the caller was left alone. Lines belonging to a nested
    tracked symbol are removed from the parent's own lines.
    """
    if symbol.start_line in lines:
        return True
    own = {line for line in lines if symbol.start_line <= line <= symbol.end_line}
    for other in symbols:
        if other is symbol or other.kind not in _TRACKED_KINDS or not _contains(symbol, other):
            continue
        own.difference_update(range(other.start_line, other.end_line + 1))
    return bool(own)


def _ambiguous_for(
    catalog: Catalog,
    graph: Graph,
    symbol: Symbol,
    by_new: dict[str, DiffFile],
    old_symbols: dict[str, list[Symbol]],
) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for item in graph.ambiguous:
        if symbol.qualified_name not in {candidate.qualified_name for candidate in item.candidates}:
            continue
        edge = Edge(symbol.qualified_name, item.caller, item.path, item.line, item.line, item.line, 0.25)
        if _edge_touched(catalog, edge, by_new, old_symbols):
            continue
        public = {
            "name": item.name,
            "path": item.path,
            "caller": item.caller,
            "line": item.line,
            "reason": "name collision",
            "candidates": [symbol_public(candidate) for candidate in item.candidates],
        }
        found.append(public)
    return found


def _removed_edges(catalog: Catalog, symbol: Symbol) -> list[Edge]:
    found: list[Edge] = []
    seen: set[tuple[str, int, str, str, str]] = set()
    for call in catalog.calls:
        if call.callee_name not in _removed_names(catalog, call.path, symbol):
            continue
        identity = (call.path, call.line, call.callee_name, call.receiver, call.caller)
        if identity in seen:
            continue
        if not _mentions_removed(catalog, call, symbol):
            continue
        seen.add(identity)
        owner = call_owner(call.caller)
        caller = catalog.by_qual.get(owner) if owner else None
        found.append(
            Edge(
                symbol.qualified_name,
                owner,
                call.path,
                caller.start_line if caller is not None else call.line,
                caller.end_line if caller is not None else call.line,
                call.line,
                CONF_REMOVED,
                call.callee_name,
                call.receiver,
            )
        )
    return found


def _removed_names(catalog: Catalog, path: str, symbol: Symbol) -> set[str]:
    """Spellings a file can call the removed symbol by, including import aliases."""
    names = {symbol.name}
    for imp in catalog.imports_by_path.get(path, []):
        # `from lib import helper as h` and `import { helper as h }`.
        if imp.kind in {"from", "named"} and imp.name == symbol.name and imp.alias:
            names.add(imp.alias)
        # `import helper from './lib'` binds the removed default export.
        if imp.kind == "default" and symbol.exported == "default":
            bound = local_name(imp)
            if bound:
                names.add(bound)
    return names


def _mentions_removed(catalog: Catalog, call: Call, symbol: Symbol) -> bool:
    if call.receiver_kind in {"self", "cls", "this"}:
        return (
            symbol.kind == "method"
            and bool(symbol.parent)
            and call.enclosing_class == symbol.parent
        )
    if not call.receiver:
        return _bare_call_mentions_removed(catalog, call, symbol)
    if symbol.kind == "method" and symbol.parent and call.receiver_kind in {"ident", "ctor"}:
        # `Foo.bar()` and `Foo().bar()` reach the method through its class, not
        # through a module. The class can be local or imported by name.
        owner = _ident_class(catalog, call, call.receiver)
        if owner is not None:
            return owner.qualified_name == symbol.parent
    if call.receiver_kind not in {"ident", "attr"}:
        return False
    if symbol.kind not in {"function", "class"} or symbol.parent:
        return False
    files, names = module_receiver(catalog, call)
    if symbol.path in files:
        return True
    keys = set(module_keys(symbol.path))
    return any(name in keys for name in names)


def _ident_class(catalog: Catalog, call: Call, name: str) -> Symbol | None:
    """Resolve a bare receiver to a class in the call's scope chain.

    Within one scope, Python's last binding wins regardless of whether it is
    an import, parameter, function, or class.
    """
    # Module-level statements see only the imports executed above them.
    scoped = catalog.scoped_bindings(call.path, call.caller, _call_position(call))
    for parent in scope_chain(call.caller, call.path):
        winner = _winning_scope_binding(catalog, call, parent, name)
        if winner is None:
            continue
        if isinstance(winner, Symbol):
            return winner if winner.kind == "class" else None
        if winner.kind == "star":
            bound = [
                item
                for item in _star_import_symbols(catalog, winner, name)
                if item.kind == "class"
            ]
        else:
            binding = scoped.get(parent, {}).get(name)
            bound = (
                []
                if binding is None
                else [item for item in binding.symbols if item.kind == "class"]
            )
        if len(bound) != 1:
            return None
        return bound[0]
    return None


def _bare_call_mentions_removed(catalog: Catalog, call: Call, symbol: Symbol) -> bool:
    """Resolve a bare call only through its lexical scopes and their imports."""
    path = call.path
    chain = scope_chain(call.caller, path)
    for parent in chain:
        winner = _winning_scope_binding(catalog, call, parent, call.callee_name, symbol)
        if isinstance(winner, Symbol):
            return False
        if isinstance(winner, Import):
            return _import_names_removed(catalog, winner, symbol)

    if path != symbol.path or call.callee_name != symbol.name:
        return False
    if not symbol.parent:
        return True
    # A nested function keeps its bare name for calls lexically inside its
    # parent's scope. A method never does: only a receiver reaches it.
    if symbol.kind == "function" and symbol.parent in chain:
        return True
    # A class-body statement resolves a bare name against the namespace the
    # earlier `def` filled. Deleting it leaves the class-body call undefined.
    return symbol.kind in {"method", "class"} and call.caller == f"{symbol.parent}#body"


def _call_position(call: Call) -> tuple[int, int] | None:
    """The statement position executed immediately, if the call is immediate.

    Module-level statements and class bodies run in order (columns order
    same-line statements); calls in functions and methods are deferred.
    """
    if not call.caller or call.caller.endswith("#body"):
        return (call.line, call.column)
    return None


def _winning_scope_binding(
    catalog: Catalog,
    call: Call,
    parent: str,
    name: str,
    removed: Symbol | None = None,
) -> Symbol | Import | None:
    # Immediate statements execute top to bottom: a binding written after them
    # cannot be what they resolved (columns order same-line statements). Calls
    # inside a function or method are deferred and may run later.
    position = _call_position(call)
    candidates: list[tuple[tuple[int, int], int, Symbol | Import]] = []
    removed_star_imports: list[tuple[tuple[int, int], int, Import]] = []
    for order, item in enumerate(catalog.by_parent.get((call.path, parent), [])):
        if item.name == name and item.kind in {"function", "class", "parameter"}:
            key = (item.start_line, item.column)
            if position is not None and key > position:
                continue
            candidates.append((key, order, item))
    offset = len(candidates)
    for order, imp in enumerate(catalog.imports_by_path.get(call.path, []), offset):
        if imp.scope != parent or imp.kind in {"side_effect", "type"}:
            continue
        key = (imp.line, imp.column)
        if position is not None and key > position:
            continue
        if imp.kind == "star":
            provides_name = bool(_star_import_symbols(catalog, imp, name))
            if not provides_name:
                if removed is not None and _import_names_removed(catalog, imp, removed):
                    removed_star_imports.append((key, order, imp))
                continue
        elif local_name(imp) != name:
            continue
        candidates.append((key, order, imp))
    if candidates:
        return max(candidates, key=lambda item: item[:2])[2]
    if removed_star_imports:
        return max(removed_star_imports, key=lambda item: item[:2])[2]
    return None


def _star_import_symbols(catalog: Catalog, imp: Import, name: str) -> list[Symbol]:
    if not name or name.startswith("_"):
        return []
    return [
        item
        for path in catalog.resolve_files(imp)
        for item in catalog.by_parent.get((path, ""), [])
        if item.name == name and item.kind in {"function", "class"}
    ]


def _import_names_removed(catalog: Catalog, imp: Import, symbol: Symbol) -> bool:
    if symbol.parent:
        return False
    if imp.kind == "star":
        names_symbol = not symbol.name.startswith("_")
    elif imp.kind in {"from", "named"}:
        names_symbol = imp.name == symbol.name
    elif imp.kind == "default":
        names_symbol = symbol.exported == "default"
    else:
        names_symbol = False
    return names_symbol and import_mentions_file(catalog, imp, symbol.path)


def _changed_symbols(
    diff_files: list[DiffFile],
    old_symbols: dict[str, list[Symbol]],
    new_symbols: dict[str, list[Symbol]],
) -> list[tuple[Symbol, bool]]:
    selected: dict[str, tuple[Symbol, bool]] = {}
    for item in diff_files:
        new_list = [
            symbol
            for symbol in new_symbols.get(item.new_path or "", [])
            if symbol.kind in _TRACKED_KINDS
        ]
        old_list = [
            symbol
            for symbol in old_symbols.get(item.old_path or "", [])
            if symbol.kind in _TRACKED_KINDS
        ]
        new_overlap = _innermost(
            [symbol for symbol in new_list if _overlaps(symbol, item.new_lines)],
            item.new_lines,
        )
        old_overlap = _innermost(
            [symbol for symbol in old_list if _overlaps(symbol, item.old_lines)],
            item.old_lines,
        )
        new_by_suffix = {_suffix(symbol): symbol for symbol in new_list}
        for symbol in new_overlap:
            selected[symbol.qualified_name] = (symbol, False)
        for symbol in old_overlap:
            match = new_by_suffix.get(_suffix(symbol))
            if match is None:
                selected[symbol.qualified_name] = (symbol, True)
            else:
                selected[match.qualified_name] = (match, False)
    return list(selected.values())


def _symbols_by_side(
    diff_files: list[DiffFile], rev: str, git: GitReader, *, old: bool
) -> dict[str, list[Symbol]]:
    found: dict[str, list[Symbol]] = {}
    for item in diff_files:
        path = item.old_path if old else item.new_path
        if not path or path in found:
            continue
        found[path] = _symbols_from_blob(git, rev, path)
    return found


def _same_symbol(symbols: list[Symbol], caller: Symbol) -> Symbol | None:
    suffix = _suffix(caller)
    matches = [item for item in symbols if _suffix(item) == suffix]
    if len(matches) == 1:
        return matches[0]
    same_kind = [item for item in matches if item.kind == caller.kind]
    if len(same_kind) == 1:
        return same_kind[0]
    return None


def _symbols_from_blob(git: GitReader, rev: str, path: str | None) -> list[Symbol]:
    if path is None or language_for_path(path) is None:
        return []
    blob = git.show(rev, path)
    if blob is None:
        return []
    return list(extract_path(path, blob).symbols)


def _innermost(symbols: list[Symbol], lines: set[int]) -> list[Symbol]:
    """Keep a symbol when a changed line is its own, not only inside a nested one.

    A nested function sits inside its parent's line span, so a one-line edit of
    the child also overlaps the parent. That overlap is not a change to the
    parent. An edit of the parent's own body still is, even when the child
    changes in the same diff. A changed `def` or `class` line is always the
    parent's own line.
    """
    kept: list[Symbol] = []
    for symbol in symbols:
        if symbol.start_line in lines:
            kept.append(symbol)
            continue
        own = {line for line in lines if symbol.start_line <= line <= symbol.end_line}
        for other in symbols:
            if other is symbol or not _contains(symbol, other):
                continue
            own.difference_update(range(other.start_line, other.end_line + 1))
        if own:
            kept.append(symbol)
    return kept


def _contains(outer: Symbol, inner: Symbol) -> bool:
    return inner.parent == outer.qualified_name or inner.parent.startswith(outer.qualified_name + ".")


def _overlaps(symbol: Symbol, lines: set[int]) -> bool:
    return any(symbol.start_line <= line <= symbol.end_line for line in lines)


def _suffix(symbol: Symbol) -> str:
    return symbol.qualified_name.split("::", 1)[1]


def _is_source_diff(item: DiffFile) -> bool:
    for path in (item.old_path, item.new_path):
        if path and language_for_path(path):
            return True
    return False


def _split_range(spec: str) -> tuple[str, str]:
    if "..." in spec:
        base, head = spec.split("...", 1)
    else:
        base, head = spec.split("..", 1)
    return base, head


def _clean_diff_path(raw: str) -> str | None:
    text = raw.strip().split("\t", 1)[0]
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        # Git quotes the whole `a/` path. Decode before stripping the prefix,
        # or `café.py` stays `a/caf\303\251.py` and matches no blob.
        text = _unescape_git_c_quote(text[1:-1])
    if text.startswith(("a/", "b/")):
        text = text[2:]
    if text == "/dev/null":
        return None
    return text or None


def _unescape_git_c_quote(body: str) -> str:
    """Decode the body of a git C-quoted path.

    Non-ASCII bytes are ``\\ooo`` octal escapes. ``\\`` and ``\\"`` are the
    only other escapes a path needs. The result is the filesystem path.
    """
    data = body.encode("utf-8")
    out = bytearray()
    index = 0
    while index < len(data):
        current = data[index]
        if current != ord("\\") or index + 1 >= len(data):
            out.append(current)
            index += 1
            continue
        nxt = data[index + 1]
        if ord("0") <= nxt <= ord("7"):
            value = 0
            count = 0
            index += 1
            while index < len(data) and count < 3 and ord("0") <= data[index] <= ord("7"):
                value = (value << 3) + (data[index] - ord("0"))
                index += 1
                count += 1
            out.append(value & 0xFF)
            continue
        out.append(_GIT_SIMPLE_ESCAPE.get(nxt, nxt))
        index += 2
    return out.decode("utf-8", "surrogateescape")
