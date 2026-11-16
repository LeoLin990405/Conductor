#!/usr/bin/env python3
"""Check that the hardening document's file and symbol references still resolve.

A hardening doc that names a file or a symbol is a promise to the reader: "the
loopback bind lives in ``src/conductor_cli/ops_runtime.py``". The promise rots
silently -- a module gets renamed, a helper moves, a policy file is dropped --
and the doc keeps reading as if it were still true. This walks the backticked
references in a markdown file and resolves each one against the working tree:

* a directory-qualified path (``src/conductor_cli/ops_runtime.py``) must exist;
* a file-anchored symbol (``src/config/settings.py:get_allowed_origins()``)
  must be defined at module level in that file;
* a dotted symbol (``ops_runtime.daemon_argv``, ``Settings.backend_host``)
  must resolve to a module attribute or a class member somewhere in the repo;
* a bare call (``backend_argv()``) must be a function or class some module
  or class in the repo defines -- the parentheses are what make a single word
  a claim about code rather than prose;
* a line anchor (``src/server.py:12``, ``src/server.py:12-20``,
  ``src/server.py#L12``) must name a file that has those lines;
* a ``CONDUCTOR_*`` environment name, a ``--flag`` or a short ``-c`` must be
  spelled as a string literal somewhere in first-party, non-test Python code;
* a dot name (``.venv``, ``.gitignore``) must be a file or directory at the
  repository root, or such a string literal.

A span with spaces in it (``server.py --host 127.0.0.1``) is checked word by
word, and a code fence that never closes is reported, since it would hide
every claim after it.

Resolution is purely static: the target files are parsed with ``ast`` and never
imported, so the checker cannot run project code, cannot start a server, and
cannot be defeated by an import side effect. Only the standard library is used,
so it runs with a bare ``python`` on any checkout.

A reference resolves only through a *binding*: a name the module actually
binds, an import it actually performs, a base class it actually inherits. A
sibling file that happens to share a name is not a binding, and neither is an
unrelated class of the same name -- resolving through those would make the
checker agree with a doc that is wrong.

Usage:
    python scripts/check_hardening.py [MARKDOWN] [--repo-root ROOT]
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

# --- markdown scanning -------------------------------------------------

# A fence opener: up to three spaces, then three or more of one fence
# character. The run's character and length are what a closer must match.
FENCE_OPEN_RE = re.compile(r"^ {0,3}(?P<run>`{3,}|~{3,})(?P<info>.*)$")
BLANK_LINE_RE = re.compile(r"\n[ \t]*\n")
WRAPPED_DOT_RE = re.compile(r"(?<=\w)\.[ \t]*\n[ \t]*(?=[A-Za-z_])")
# A call written with arguments ("`backend_argv(settings)`") is the same
# claim as the bare call, so the arguments are dropped before classifying.
CALL_ARGS_RE = re.compile(r"\([^()]*\)")
DOTTED_NAME_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
BARE_NAME_RE = re.compile(r"^[A-Za-z_]\w*$")
# "file.py:12", "file.py:12-20", "file.py:12:5" -- a line, a range, a column.
LINE_SPEC_RE = re.compile(r"^(?P<start>\d+)(?:-(?P<end>\d+))?(?::\d+)?$")
# "file.py#L12", "file.py#L12-L20" -- the forge's spelling of the same.
HASH_LINE_SPEC_RE = re.compile(r"^L(?P<start>\d+)(?:-L?(?P<end>\d+))?$")
# The names the code reads from the environment and the options it parses.
# Neither is a Python binding, so they resolve against string literals.
ENV_NAME_RE = re.compile(r"^(?:CONDUCTOR|GIT)_[A-Z0-9_]+$")
# A single word that reads as code rather than English: snake_case (or a
# leading underscore) or CamelCase. "`missing`" and "`Settings`" stay prose.
SYMBOL_LIKE_RE = re.compile(r"^(?=.*[A-Za-z])(?:\w*_\w*|[A-Za-z]*[a-z][A-Z]\w*|[A-Z][a-z0-9]+[A-Z]\w*)$")
# An inline link or image, "[text](target)" or "[text](target \"title\")".
INLINE_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\(\s*<?(?P<target>[^)\s>]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
# A reference-style link definition, "[label]: target".
LINK_DEFINITION_RE = re.compile(r"^ {0,3}\[[^\]\n]+\]:\s*<?(?P<target>[^\s>]+)>?", re.MULTILINE)
URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
FLAG_RE = re.compile(r"^--[A-Za-z0-9][A-Za-z0-9-]*$")
# A short option, "-c" or "-z": one letter, checked like a long one.
SHORT_FLAG_RE = re.compile(r"^-[A-Za-z]$")
# A dot name, ".venv" or ".gitignore": a file or directory the code names.
DOT_NAME_RE = re.compile(r"^\.[A-Za-z0-9_][\w.-]*$")

# A token that ends in one of these is a file name rather than a dotted symbol.
FILE_SUFFIXES = frozenset(
    {
        ".cfg",
        ".css",
        ".env",
        ".html",
        ".ini",
        ".js",
        ".json",
        ".jsx",
        ".lock",
        ".md",
        ".proto",
        ".py",
        ".pyi",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".yaml",
        ".yml",
    }
)

# The daemon's policy files live on the *device*, under ~/.config/conductor/,
# not in the repo -- the repo ships "*.example.yaml" twins instead. A doc that
# names one of them by its bare file name is talking about the device's copy,
# so resolving it against the working tree would report a file that was never
# here in the first place.
DEVICE_LOCAL_FILENAMES = frozenset(
    {
        "daemon_policy.yaml",
        "daemon_trusted_keys.json",
        "workspaces-cli.json",
    }
)

# Directories that never hold first-party source.
SKIP_DIRS = frozenset(
    {
        ".eggs",
        ".git",
        ".mypy_cache",
        ".next",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "site-packages",
        "venv",
    }
)

# --- reference model ---------------------------------------------------


@dataclass(frozen=True)
class Reference:
    """One backticked token the doc claims points at something real."""

    raw: str
    line: int = 0
    kind: str = "path"  # "path" | "symbol" | "anchored" | "call" | "lines" | "literal" | "name" | "dotname"
    path: str = ""  # repo-relative file path, for "path", "anchored" and "lines"
    link: bool = False  # a Markdown link: the path is relative to the document
    chain: tuple[str, ...] = ()  # dotted names; for "literal", the literal itself
    lines: tuple[int, int] = (0, 0)  # first and last line, for "lines"


@dataclass(frozen=True)
class Unresolved:
    """A reference the working tree cannot back up."""

    line: int
    raw: str
    reason: str


@dataclass(frozen=True)
class Binding:
    """A name a module binds, and -- for an import -- where it came from.

    The import fields are what let an import descend into the module it
    actually names, rather than into any file that happens to share the name.

    "bound" is the name the importing scope gets; "origin" is the name the
    name has in the module it came from. The two differ when the import is
    aliased: "from pkg.base import Base as Alias" binds "Alias" here and
    names "Base" over there.
    """

    node: ast.AST
    kind: str  # "class" | "def" | "value" | "import"
    bound: str = ""  # the name this binding introduces in its scope
    module: str = ""  # dotted module an import came from
    origin: str = ""  # the name imported out of that module
    level: int = 0  # relative-import depth (1 is ".")


@dataclass(frozen=True)
class Target:
    """Something a binding can be descended into: a class or a module."""

    kind: str  # "class" | "module"
    path: Path
    node: ast.ClassDef | None = None


def extract_references(text: str) -> list[Reference]:
    """Pull every reference out of the markdown's inline code spans."""
    references, _problems = scan_document(text)
    return references


def scan_document(text: str) -> tuple[list[Reference], list[Unresolved]]:
    """The references in a document, and the structural problems that hide some.

    Fenced blocks are skipped: they show configuration and refusal messages,
    not claims about the repository. A fence closes only on a run of its own
    character at least as long as the one that opened it, as Markdown renders
    it; a fence that never closes would hide every claim after it, so it is
    reported rather than silently treated as the end of the document.
    """
    problems: list[Unresolved] = []
    prose: list[str] = []
    fence: tuple[str, int, int, str] | None = None  # char, length, line, opener
    for lineno, line in enumerate(text.splitlines(), start=1):
        match = FENCE_OPEN_RE.match(line)
        if fence is None:
            # A backtick fence's info string may not hold a backtick; such a
            # line is an inline span, not a fence.
            if match and not (match["run"][0] == "`" and "`" in match["info"]):
                run = match["run"]
                fence = (run[0], len(run), lineno, line.strip())
                prose.append("")
                continue
            prose.append(line)
            continue
        char, length, _opened, _opener = fence
        if match and match["run"][0] == char and len(match["run"]) >= length and not match["info"].strip():
            fence = None
        # A fenced line is blank prose, so no span can reach across it.
        prose.append("")
    if fence is not None:
        _char, _length, opened, opener = fence
        problems.append(
            Unresolved(
                opened,
                opener,
                "unclosed code fence: every line after it is hidden from this check",
            )
        )

    references: list[Reference] = []
    joined = "\n".join(prose)
    # Links are read from the prose with the code spans blanked out, so a
    # "[x](y)" quoted inside a span is not a link.
    masked = list(joined)
    for start, end, content in _code_spans(joined):
        lineno = joined.count("\n", 0, start) + 1
        for reference in classify_span(content):
            references.append(replace(reference, line=lineno))
        for index in range(start, end):
            if masked[index] != "\n":
                masked[index] = " "
    masked_text = "".join(masked)
    for pattern in (INLINE_LINK_RE, LINK_DEFINITION_RE):
        for match in pattern.finditer(masked_text):
            reference = classify_link(match["target"])
            if reference is not None:
                lineno = masked_text.count("\n", 0, match.start()) + 1
                references.append(replace(reference, line=lineno))
    references.sort(key=lambda reference: reference.line)
    return references, problems


def classify_link(target: str) -> Reference | None:
    """What a Markdown link's target claims: a file, and maybe lines in it.

    A URL or an in-page anchor is not a repository claim. Anything else is a
    path relative to the document, as a forge renders it, so the file it
    names must exist -- and a "#L12" anchor must be a line the file has.
    """
    if not target or target.startswith("#") or URL_SCHEME_RE.match(target):
        return None
    path, _, fragment = target.partition("#")
    path = path.partition("?")[0]
    if not path:
        return None
    match = HASH_LINE_SPEC_RE.match(fragment) if fragment else None
    if match:
        reference = _line_reference(target, path, match)
        return replace(reference, link=True)
    return Reference(raw=target, kind="path", path=path, link=True)


def _code_spans(text: str) -> Iterable[tuple[int, int, str]]:
    """Yield each inline code span: where it starts and ends, and its content.

    A span opens on a run of backticks and closes on the next run of exactly
    the same length, so "``a ` b``" is one span. It may wrap onto the next
    line of its paragraph but never across a blank line, which is where
    Markdown ends a paragraph; an opener with no closer is literal text.
    """
    index = 0
    size = len(text)
    while index < size:
        if text[index] != "`":
            index += 1
            continue
        run_end = index
        while run_end < size and text[run_end] == "`":
            run_end += 1
        length = run_end - index
        cursor = run_end
        closing: tuple[int, int] | None = None
        while cursor < size:
            if text[cursor] != "`":
                cursor += 1
                continue
            end = cursor
            while end < size and text[end] == "`":
                end += 1
            if end - cursor == length:
                closing = (cursor, end)
                break
            cursor = end
        if closing is None:
            index = run_end
            continue
        content = text[run_end : closing[0]]
        if BLANK_LINE_RE.search(content):
            index = run_end
            continue
        yield index, closing[1], content
        index = closing[1]


def classify_span(content: str) -> list[Reference]:
    """Every reference one code span makes.

    A span with spaces in it ("`server.py --host 127.0.0.1`", "`` x ``") is
    still a set of claims: each whitespace-separated word is classified on
    its own, so a renamed module inside a command line is not hidden by the
    command around it. The raw text of each reference is the word it came
    from, so a failure names exactly what broke.
    """
    # A dotted name the paragraph wrapped after its dot is still one name.
    content = WRAPPED_DOT_RE.sub(".", content)
    text = CALL_ARGS_RE.sub("()", " ".join(content.split()))
    out: list[Reference] = []
    for word in text.split():
        # "`git_check: unsupported_runtime`" -- the colon is punctuation.
        reference = classify(word.rstrip(":,;"))
        if reference is not None:
            out.append(reference)
    return out


def _line_reference(raw: str, path: str, match: re.Match[str]) -> Reference:
    start = int(match["start"])
    end = int(match["end"]) if match["end"] else start
    return Reference(raw=raw, kind="lines", path=path, lines=(start, end))


def classify(raw: str) -> Reference | None:
    """Decide what a backticked token refers to, or that it refers to nothing."""
    raw = raw.strip()
    if not raw:
        return None
    # Device-local (~/.config/...) and absolute paths are outside the repo.
    if raw.startswith(("~", "/")):
        return None
    if "://" in raw:
        return None
    # An option the code parses, with or without a value: "--policy",
    # "--host=127.0.0.1".
    flag = raw.partition("=")[0]
    if FLAG_RE.match(flag):
        return Reference(raw=raw, kind="literal", chain=(flag,))
    if SHORT_FLAG_RE.match(raw):
        return Reference(raw=raw, kind="literal", chain=(raw,))
    if DOT_NAME_RE.match(raw) and Path(raw).suffix not in FILE_SUFFIXES:
        # ".venv" names a file the repo has or one the code spells out; a
        # dotted file such as ".env.example" is still a path below.
        return Reference(raw=raw, kind="dotname", chain=(raw,))
    # Other dashes, assignments and words with spaces are not references.
    if raw.startswith("-") or "=" in raw or any(char.isspace() for char in raw):
        return None
    if ENV_NAME_RE.match(raw):
        return Reference(raw=raw, kind="literal", chain=(raw,))

    token = raw.removesuffix("()")
    if "\\" in token:
        # A Windows spelling of a repository path.
        token = token.replace("\\", "/")
    while token.startswith("./"):
        token = token[2:]
    if not token:
        return None

    if "#" in token:
        left, _, right = token.partition("#")
        match = HASH_LINE_SPEC_RE.match(right)
        if match and (left.endswith(".py") or "/" in left or Path(left).suffix in FILE_SUFFIXES):
            return _line_reference(raw, left, match)

    if ":" in token:
        # "path/to/file.py:symbol" -- anchored at a specific file. The file
        # names where the symbol lives, so the symbol may be a single name.
        left, _, right = token.partition(":")
        if not (left.endswith(".py") or "/" in left or Path(left).suffix in FILE_SUFFIXES):
            return None
        if IDENTIFIER_RE.match(right):
            return Reference(raw=raw, kind="anchored", path=left, chain=tuple(right.split(".")))
        match = LINE_SPEC_RE.match(right)
        if match:
            return _line_reference(raw, left, match)
        # Some other anchor: the file it is anchored in must still exist.
        return Reference(raw=raw, kind="path", path=left)

    if "/" in token:
        return Reference(raw=raw, kind="path", path=token)

    if Path(token).suffix in FILE_SUFFIXES:
        if token in DEVICE_LOCAL_FILENAMES:
            return None
        # A bare file name is a path relative to the repository root.
        return Reference(raw=raw, kind="path", path=token)

    if DOTTED_NAME_RE.match(token):
        return Reference(raw=raw, kind="symbol", chain=tuple(token.split(".")))

    if raw.endswith("()") and BARE_NAME_RE.match(token):
        # A single word is prose ("`missing`", "`ok`"), but a single word
        # written as a call names a callable, so it is checked like one.
        return Reference(raw=raw, kind="call", chain=(token,))

    if raw == token and SYMBOL_LIKE_RE.match(token):
        # A single snake_case or CamelCase word is a name the code is
        # claimed to have, even without a module to qualify it.
        return Reference(raw=raw, kind="name", chain=(token,))

    return None


# --- static resolution -------------------------------------------------


class Repo:
    """A repository indexed for static lookup, without importing any of it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._python_files: list[Path] | None = None
        self._modules: dict[str, list[Path]] | None = None
        self._classes: dict[str, list[Path]] | None = None
        self._callables: dict[str, list[Path]] | None = None
        self._literals: dict[str, bool] = {}
        self._texts: dict[Path, str] = {}
        self._trees: dict[Path, ast.Module | None] = {}
        self._scopes: dict[Path, dict[str, Binding]] = {}

    # -- indexing -------------------------------------------------------

    def python_files(self) -> list[Path]:
        if self._python_files is None:
            found: list[Path] = []
            for path in self.root.rglob("*.py"):
                if any(part in SKIP_DIRS for part in path.relative_to(self.root).parts[:-1]):
                    continue
                found.append(path)
            found.sort()
            self._python_files = found
        return self._python_files

    def modules_by_name(self) -> dict[str, list[Path]]:
        """Map a dotted module name to the files that could be it.

        A doc names a module by its dotted path ("conductor_cli.ops_runtime")
        or by its bare stem ("ops_runtime"), and a package by its directory
        name ("pkg" for pkg/__init__.py). Every file is therefore indexed
        under each name it could answer to: the path relative to the repo
        root, the path relative to each package root above it, and the bare
        name. Several files may share a name; the caller tries them all and
        accepts the first that resolves.
        """
        if self._modules is None:
            mapping: dict[str, list[Path]] = {}
            for path in self.python_files():
                for name in self._names_for(path):
                    mapping.setdefault(name, []).append(path)
            self._modules = mapping
        return self._modules

    def _names_for(self, path: Path) -> list[str]:
        """The dotted names a file may be referred to by."""
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            return []
        parts = list(relative.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            # A package answers to its directory name.
            parts = parts[:-1]
        if not parts:
            return []
        names = {".".join(parts)}
        for index in range(len(parts) - 1, 0, -1):
            ancestor = self.root.joinpath(*parts[:index])
            if (ancestor / "__init__.py").is_file():
                names.add(".".join(parts[index:]))
        names.add(parts[-1])
        return sorted(names)

    def classes_by_name(self) -> dict[str, list[Path]]:
        """Map a class name to the files that may define it.

        A regex over the raw text is a cheap pre-filter; the AST confirms it
        later, so a class name quoted inside a docstring never counts.
        """
        if self._classes is None:
            pattern = re.compile(r"^class\s+(\w+)", re.MULTILINE)
            mapping: dict[str, list[Path]] = {}
            for path in self.python_files():
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for name in set(pattern.findall(text)):
                    mapping.setdefault(name, []).append(path)
            self._classes = mapping
        return self._classes

    def callables_by_name(self) -> dict[str, list[Path]]:
        """Map a function or class name to the files that may define it.

        Like classes_by_name this is only a text pre-filter; the AST decides.
        """
        if self._callables is None:
            pattern = re.compile(r"^\s*(?:async\s+def|def|class)\s+(\w+)", re.MULTILINE)
            mapping: dict[str, list[Path]] = {}
            for path in self.python_files():
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for name in set(pattern.findall(text)):
                    mapping.setdefault(name, []).append(path)
            self._callables = mapping
        return self._callables

    def spells_literal(self, literal: str) -> bool:
        """Whether first-party, non-test Python code has that exact string constant.

        The raw text is a cheap pre-filter, so only files that mention the
        literal at all are parsed; the AST decides, so a mention inside a
        longer string or a comment never counts.
        """
        if literal not in self._literals:
            self._literals[literal] = any(
                self._has_constant(path, literal)
                for path in self.python_files()
                if not _is_test_file(path.relative_to(self.root))
            )
        return self._literals[literal]

    def _has_constant(self, path: Path, literal: str) -> bool:
        if literal not in self._text(path):
            return False
        tree = self.tree(path)
        if tree is None:
            return False
        return any(
            isinstance(node, ast.Constant) and node.value == literal for node in ast.walk(tree)
        )

    def defines_name(self, name: str) -> bool:
        """Whether non-test code defines or assigns that name, or has a module by it.

        Only module and class bodies count. An import counts too, so a
        standard-library name the code uses ("SimpleHTTPRequestHandler")
        resolves; a first-party name that is only imported is defined where
        it is imported from, or that import is what is broken.
        """
        for path in self.modules_by_name().get(name, []):
            if not _is_test_file(path.relative_to(self.root)):
                return True
        for path in self.python_files():
            if _is_test_file(path.relative_to(self.root)) or name not in self._text(path):
                continue
            tree = self.tree(path)
            if tree is None:
                continue
            scopes = [self.module_scope(path)]
            scopes.extend(
                _class_members(node) for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
            )
            if any(
                scope.get(name) is not None
                for scope in scopes
            ):
                return True
        return False

    def _text(self, path: Path) -> str:
        if path not in self._texts:
            try:
                self._texts[path] = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                self._texts[path] = ""
        return self._texts[path]

    def defines_callable(self, path: Path, name: str) -> bool:
        """Whether the file defines that function or class at module or class level.

        A function nested inside another function is a local, not something a
        reader can call, so only module bodies and class bodies count.
        """
        tree = self.tree(path)
        if tree is None:
            return False
        scopes = [self.module_scope(path)]
        scopes.extend(_class_members(node) for node in ast.walk(tree) if isinstance(node, ast.ClassDef))
        return any(
            (binding := scope.get(name)) is not None and binding.kind in {"def", "class"}
            for scope in scopes
        )

    # -- parsing --------------------------------------------------------

    def tree(self, path: Path) -> ast.Module | None:
        if path not in self._trees:
            try:
                source = path.read_text(encoding="utf-8")
                self._trees[path] = ast.parse(source, filename=str(path))
            except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
                self._trees[path] = None
        return self._trees[path]

    def module_scope(self, path: Path) -> dict[str, Binding]:
        """The names a module binds at import time."""
        if path not in self._scopes:
            tree = self.tree(path)
            scope: dict[str, Binding] = {}
            if tree is not None:
                _collect_module_names(tree.body, scope)
            self._scopes[path] = scope
        return self._scopes[path]

    def find_class(self, path: Path, name: str) -> ast.ClassDef | None:
        """The class of that name defined in that file, if there is one."""
        tree = self.tree(path)
        if tree is None:
            return None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == name:
                return node
        return None

    def submodule_file(self, package: Path, name: str) -> Path | None:
        """The submodule of a package, if the package has one by that name."""
        if package.name != "__init__.py":
            return None
        directory = package.parent
        module = directory / f"{name}.py"
        if module.is_file():
            return module
        nested = directory / name / "__init__.py"
        if nested.is_file():
            return nested
        return None

    def import_files(self, binding: Binding, owner: Path) -> list[Path]:
        """The files an import names.

        A relative import is resolved against the importing file's package; an
        absolute one against the module index. Either way the result is the
        module the import actually names -- never a file that merely shares
        its name.
        """
        if binding.level:
            package_dir = owner.parent
            for _ in range(binding.level - 1):
                package_dir = package_dir.parent
            parts = binding.module.split(".") if binding.module else []
            if not parts:
                # "from . import name" -- the package's own __init__.
                init = package_dir / "__init__.py"
                return [init] if init.is_file() else []
            base = package_dir.joinpath(*parts)
            files: list[Path] = []
            module_file = base.with_suffix(".py")
            if module_file.is_file():
                files.append(module_file)
            package_file = base / "__init__.py"
            if package_file.is_file():
                files.append(package_file)
            return files

        files = self.modules_by_name().get(binding.module, [])
        if files:
            return files
        # A module or package the index missed, addressed by its path.
        if binding.module:
            base = self.root.joinpath(*binding.module.split("."))
            module_file = base.with_suffix(".py")
            if module_file.is_file():
                return [module_file]
            package_file = base / "__init__.py"
            if package_file.is_file():
                return [package_file]
        return []

    def relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)


def _is_test_file(relative: Path) -> bool:
    """Whether a repository-relative file is test code rather than the product."""
    name = relative.name
    return (
        "tests" in relative.parts[:-1]
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


def _bind_target(node: ast.AST, scope: dict[str, Binding]) -> None:
    """Record the name(s) an assignment target binds."""
    if isinstance(node, ast.Name):
        scope[node.id] = Binding(node, "value")
    elif isinstance(node, (ast.Tuple, ast.List)):
        for element in node.elts:
            _bind_target(element, scope)


def _import_binding(node: ast.Import | ast.ImportFrom, alias: ast.alias) -> Binding:
    """Describe what one imported name binds.

    The name the importing scope gets and the name it has in the source
    module differ when the import is aliased, so the source is looked up by
    the name the source knows it by.
    """
    if isinstance(node, ast.ImportFrom):
        return Binding(
            alias,
            "import",
            bound=alias.asname or alias.name,
            module=node.module or "",
            origin=alias.name,
            level=node.level,
        )
    if alias.asname:
        # "import a.b as x" binds x to the module a.b.
        return Binding(alias, "import", bound=alias.asname, module=alias.name, origin="")
    # "import a.b" binds the package "a". "b" is a submodule below it, and
    # is reached as an attribute of that package rather than folded into the
    # bound name -- "a.b" is the package's attribute, not the package itself.
    parts = alias.name.split(".")
    return Binding(alias, "import", bound=parts[0], module=parts[0], origin="")


def _collect_module_names(body: Sequence[ast.stmt], scope: dict[str, Binding]) -> None:
    """Collect the names a module body binds.

    Control-flow statements are walked because a binding inside "if" or
    "except" is still a module attribute; function bodies are not walked
    because a local is not.
    """
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope[node.name] = Binding(node, "def")
        elif isinstance(node, ast.ClassDef):
            scope[node.name] = Binding(node, "class")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                _bind_target(target, scope)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            _bind_target(node.target, scope)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                scope[name] = _import_binding(node, alias)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            _bind_target(node.target, scope)
            _collect_module_names(node.body, scope)
            _collect_module_names(node.orelse, scope)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    _bind_target(item.optional_vars, scope)
            _collect_module_names(node.body, scope)
        elif isinstance(node, (ast.If, ast.While)):
            _collect_module_names(node.body, scope)
            _collect_module_names(node.orelse, scope)
        elif isinstance(node, ast.Try):
            _collect_module_names(node.body, scope)
            for handler in node.handlers:
                if handler.name:
                    scope[handler.name] = Binding(handler, "value")
                _collect_module_names(handler.body, scope)
            _collect_module_names(node.orelse, scope)
            _collect_module_names(node.finalbody, scope)
        elif isinstance(node, ast.Match):
            for case in node.cases:
                _collect_module_names(case.body, scope)


def _class_members(node: ast.ClassDef) -> dict[str, Binding]:
    """The names a class body binds: fields, methods and nested classes."""
    members: dict[str, Binding] = {}
    for statement in node.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            members[statement.name] = Binding(statement, "def")
        elif isinstance(statement, ast.ClassDef):
            members[statement.name] = Binding(statement, "class")
        elif isinstance(statement, ast.Assign):
            for target in statement.targets:
                _bind_target(target, members)
        elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
            _bind_target(statement.target, members)
        elif isinstance(statement, (ast.Import, ast.ImportFrom)):
            for alias in statement.names:
                name = alias.asname or alias.name.split(".")[0]
                members[name] = _import_binding(statement, alias)
        elif isinstance(statement, (ast.For, ast.AsyncFor)):
            _bind_target(statement.target, members)
            members.update(_class_members_of_body(statement.body))
        elif isinstance(statement, (ast.If, ast.While)):
            members.update(_class_members_of_body(statement.body))
            members.update(_class_members_of_body(statement.orelse))
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            for item in statement.items:
                if item.optional_vars is not None:
                    _bind_target(item.optional_vars, members)
            members.update(_class_members_of_body(statement.body))
        elif isinstance(statement, ast.Try):
            members.update(_class_members_of_body(statement.body))
            for handler in statement.handlers:
                members.update(_class_members_of_body(handler.body))
            members.update(_class_members_of_body(statement.orelse))
            members.update(_class_members_of_body(statement.finalbody))
    return members


def _class_members_of_body(body: Sequence[ast.stmt]) -> dict[str, Binding]:
    members: dict[str, Binding] = {}
    _collect_module_names(body, members)
    return members


def _join_candidates(candidates: Sequence[tuple[str, str]]) -> str:
    """Render every candidate that failed, so no file is silently dropped."""
    if len(candidates) == 1:
        path, reason = candidates[0]
        return f"{path}: {reason}"
    listed = ", ".join(path for path, _ in candidates)
    reasons = sorted({reason for _, reason in candidates})
    return f"{'; '.join(reasons)} (checked {listed})"


class Checker:
    """Resolve references against a repository, statically."""

    def __init__(self, repo: Repo, document_dir: Path | None = None) -> None:
        self.repo = repo
        # A Markdown link is relative to the document that holds it.
        self.document_dir = document_dir if document_dir is not None else repo.root

    def _file_for(self, reference: Reference) -> Path:
        base = self.document_dir if reference.link else self.repo.root
        return base / reference.path

    # -- public ---------------------------------------------------------

    def check(self, references: Iterable[Reference]) -> list[Unresolved]:
        """Return the references the repository cannot back up."""
        seen: set[tuple[str, bool]] = set()
        unresolved: list[Unresolved] = []
        for reference in references:
            key = (reference.raw, reference.link)
            if key in seen:
                continue
            seen.add(key)
            reason = self.reason_for(reference)
            if reason is not None:
                unresolved.append(Unresolved(reference.line, reference.raw, reason))
        return unresolved

    def reason_for(self, reference: Reference) -> str | None:
        """Return why a reference fails to resolve, or None when it does."""
        if reference.kind == "path":
            return self._reason_for_path(reference)
        if reference.kind == "anchored":
            return self._reason_for_anchored(reference)
        if reference.kind == "call":
            return self._reason_for_call(reference)
        if reference.kind == "lines":
            return self._reason_for_lines(reference)
        if reference.kind == "literal":
            return self._reason_for_literal(reference)
        if reference.kind == "name":
            return self._reason_for_name(reference)
        if reference.kind == "dotname":
            return self._reason_for_dot_name(reference)
        return self._reason_for_symbol(reference)

    # -- paths ----------------------------------------------------------

    def _reason_for_path(self, reference: Reference) -> str | None:
        target = self._file_for(reference)
        if reference.path.endswith("/"):
            # A trailing slash claims a directory.
            if target.is_dir():
                return None
            return f"no such directory: {reference.path}"
        if target.is_file():
            return None
        return f"no such file: {reference.path}"

    def _reason_for_lines(self, reference: Reference) -> str | None:
        """A line anchor names a file, and lines that file actually has."""
        target = self._file_for(reference)
        if not target.is_file():
            return f"no such file: {reference.path}"
        start, end = reference.lines
        try:
            count = len(target.read_bytes().splitlines())
        except OSError as exc:
            return f"cannot read {reference.path}: {exc}"
        if start < 1 or end < start:
            return f"{reference.path}: line range {start}-{end} is empty"
        if end > count:
            return f"{reference.path}: line {end} is past the end of the file ({count} lines)"
        return None

    def _reason_for_name(self, reference: Reference) -> str | None:
        """A bare code-looking name must be something first-party code has.

        That is a name a module or class body defines or assigns, a module of
        that name, or -- for a status or key the code spells out, such as
        "policy_refused" -- an exact string constant. Tests are not counted.
        """
        (name,) = reference.chain
        if self.repo.defines_name(name) or self.repo.spells_literal(name):
            return None
        return f"no definition, module or string constant named '{name}' in the source tree"

    def _reason_for_dot_name(self, reference: Reference) -> str | None:
        """A dot name is a file or directory the repository has, or the code spells.

        ".gitignore" is a file at the root; ".venv" or ".pytest_cache" is a
        directory name the code matches by string. Tests are not counted.
        """
        (name,) = reference.chain
        if (self.repo.root / name).exists() or self.repo.spells_literal(name):
            return None
        return f"no file and no string constant named '{name}' in the source tree"

    def _reason_for_literal(self, reference: Reference) -> str | None:
        """An environment name or an option must be spelled out in the code.

        The code reads "CONDUCTOR_WORKSPACE_POLICY" and parses "--policy" by
        string, so the claim holds only if some first-party module carries
        that exact string. Tests are not counted: a test that still spells a
        removed option would otherwise keep the claim alive.
        """
        (literal,) = reference.chain
        if self.repo.spells_literal(literal):
            return None
        if literal.startswith("-"):
            return f"no option '{literal}' is spelled in the source tree"
        return f"no environment variable '{literal}' is read in the source tree"

    # -- symbols --------------------------------------------------------

    def _reason_for_call(self, reference: Reference) -> str | None:
        (name,) = reference.chain
        for path in self.repo.callables_by_name().get(name, []):
            if self.repo.defines_callable(path, name):
                return None
        return f"no function or class named '{name}'"

    def _reason_for_anchored(self, reference: Reference) -> str | None:
        target = self.repo.root / reference.path
        if not target.is_file():
            return f"no such file: {reference.path}"
        if not reference.chain:
            return None
        reason = self._resolve_in_scope(
            self.repo.module_scope(target), list(reference.chain), target
        )
        if reason is None:
            return None
        return f"{reference.path}: {reason}"

    def _reason_for_symbol(self, reference: Reference) -> str | None:
        chain = reference.chain
        if not chain:
            return None
        head, rest = chain[0], chain[1:]

        # A dotted chain names a module by however much of it is the module's
        # own path: "pkg.mod.helper" is the module "pkg.mod" and the attribute
        # "helper". So the longest prefixes are tried first, and the matched
        # prefix is consumed before the rest is resolved inside it.
        candidates: list[tuple[str, str]] = []
        for length in range(len(chain), 0, -1):
            prefix = ".".join(chain[:length])
            for path in self.repo.modules_by_name().get(prefix, []):
                reason = self._resolve_in_scope(
                    self.repo.module_scope(path), list(chain[length:]), path
                )
                if reason is None:
                    return None
                candidates.append((self.repo.relative(path), f"module: {reason}"))

        # A bare name can match more than one class ("Settings" is defined in
        # two packages), so every candidate is tried and every failure is
        # reported -- naming only one file would point the reader at the wrong
        # module.
        class_hits = self.repo.classes_by_name().get(head, [])
        for path in class_hits:
            node = self.repo.find_class(path, head)
            if node is None:
                continue
            members = self._members_with_bases(node, path, frozenset())
            reason = self._resolve_in_scope(
                members, list(rest), path, allow_submodules=False
            )
            if reason is None:
                return None
            candidates.append((self.repo.relative(path), f"class '{head}': {reason}"))

        if not candidates:
            return f"no module or class named '{head}'"
        return _join_candidates(candidates)

    # -- scope resolution ------------------------------------------------

    def _resolve_in_scope(
        self,
        scope: dict[str, Binding],
        chain: Sequence[str],
        owner: Path,
        allow_submodules: bool = True,
        resolving: frozenset[tuple[str, str]] = frozenset(),
    ) -> str | None:
        """Resolve a dotted chain against the names a scope binds.

        A name resolves only if the scope binds it. The binding decides what
        comes next: a class descends into its members, an import descends
        into the module it names, and anything else ends the chain.
        """
        if not chain:
            return None
        head, rest = chain[0], chain[1:]
        binding = scope.get(head)
        if binding is None:
            # A package exposes its submodules as attributes, so "pkg.sub" is
            # a real access even when __init__ does not import it. A plain
            # module does not: a sibling file beside it is not its attribute.
            # A class body is not a module scope either, so its members never
            # reach for a submodule.
            submodule = self.repo.submodule_file(owner, head) if allow_submodules else None
            if submodule is None:
                return f"no attribute named '{head}'"
            return self._resolve_in_scope(
                self.repo.module_scope(submodule), list(rest), submodule, resolving=resolving
            )
        if not rest:
            return None
        targets = self._targets(binding, owner, resolving)
        if not targets:
            return f"'{head}' is not a class"
        reasons: list[str] = []
        for target in targets:
            reason = self._resolve_in_target(target, rest, resolving)
            if reason is None:
                return None
            reasons.append(reason)
        return reasons[0]

    def _resolve_in_target(
        self,
        target: Target,
        chain: Sequence[str],
        resolving: frozenset[tuple[str, str]],
    ) -> str | None:
        if target.kind == "module":
            return self._resolve_in_scope(
                self.repo.module_scope(target.path), list(chain), target.path, resolving=resolving
            )
        assert target.node is not None
        members = self._members_with_bases(target.node, target.path, frozenset())
        return self._resolve_in_scope(
            members, list(chain), target.path, allow_submodules=False, resolving=resolving
        )

    def _targets(
        self,
        binding: Binding,
        owner: Path,
        resolving: frozenset[tuple[str, str]] = frozenset(),
    ) -> list[Target]:
        """What a binding can be descended into: a class or a module."""
        if binding.kind == "class":
            assert isinstance(binding.node, ast.ClassDef)
            return [Target("class", owner, binding.node)]
        if binding.kind != "import":
            return []
        key = (self.repo.relative(owner), binding.bound)
        if key in resolving:
            # A circular reexport: stop here rather than recursing forever.
            return []
        return self._import_targets(binding, owner, resolving | {key})

    def _import_targets(
        self,
        binding: Binding,
        owner: Path,
        resolving: frozenset[tuple[str, str]],
    ) -> list[Target]:
        """Where an import points: the module it names, or a name inside it."""
        out: list[Target] = []
        for file in self.repo.import_files(binding, owner):
            if not binding.origin:
                out.append(Target("module", file))
                continue
            inner = self.repo.module_scope(file).get(binding.origin)
            if inner is not None:
                out.extend(self._targets(inner, file, resolving))
                continue
            # "from pkg import sub" -- the name may be a submodule of the
            # package rather than something the package binds itself.
            submodule = self.repo.submodule_file(file, binding.origin)
            if submodule is not None:
                out.append(Target("module", submodule))
        return out

    # -- inheritance ------------------------------------------------------

    def _members_with_bases(
        self,
        node: ast.ClassDef,
        owner: Path,
        seen: frozenset[tuple[str, str]],
    ) -> dict[str, Binding]:
        """A class's own members plus the ones it inherits.

        A base is resolved through the binding the class actually inherits
        from, so a class that inherits "Base" picks up the Base its module
        imported -- not an unrelated class that happens to share the name.
        """
        key = (self.repo.relative(owner), node.name)
        if key in seen:
            return {}
        members = _class_members(node)
        next_seen = seen | {key}
        for base in node.bases:
            for base_path, base_node in self._base_classes(base, owner):
                for name, binding in self._members_with_bases(base_node, base_path, next_seen).items():
                    members.setdefault(name, binding)
        return members

    def _base_classes(
        self,
        base: ast.expr,
        owner: Path,
    ) -> list[tuple[Path, ast.ClassDef]]:
        """The classes a base expression actually refers to."""
        if isinstance(base, ast.Name):
            binding = self.repo.module_scope(owner).get(base.id)
            if binding is None:
                return []
            found: list[tuple[Path, ast.ClassDef]] = []
            for target in self._targets(binding, owner):
                if target.kind == "class" and target.node is not None:
                    found.append((target.path, target.node))
            return found
        if isinstance(base, ast.Attribute):
            # "module.Base" -- resolve the module, then the class in it.
            if not isinstance(base.value, ast.Name):
                return []
            binding = self.repo.module_scope(owner).get(base.value.id)
            if binding is None:
                return []
            found = []
            for target in self._targets(binding, owner):
                if target.kind != "module":
                    continue
                inner = self.repo.find_class(target.path, base.attr)
                if inner is not None:
                    found.append((target.path, inner))
            return found
        return []


# --- command line ------------------------------------------------------


def default_repo_root() -> Path:
    """The repository this script lives in."""
    return Path(__file__).resolve().parent.parent


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_hardening.py",
        description="Resolve a hardening document's file and symbol references.",
    )
    parser.add_argument(
        "markdown",
        nargs="?",
        default="docs/HARDENING.md",
        help="the document to check (default: docs/HARDENING.md, relative to the repo root)",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="the repository to resolve against (default: the repository this script is in)",
    )
    args = parser.parse_args(argv)

    root = Path(args.repo_root).resolve() if args.repo_root else default_repo_root()
    document = Path(args.markdown)
    if not document.is_absolute():
        document = root / document

    if not document.is_file():
        print(f"check_hardening: no such document: {document}", file=sys.stderr)
        return 2

    try:
        text = document.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"check_hardening: cannot read {document}: {exc}", file=sys.stderr)
        return 2

    references, problems = scan_document(text)
    checker = Checker(Repo(root), document.parent)
    unresolved = problems + checker.check(references)

    try:
        shown = document.relative_to(root).as_posix()
    except ValueError:
        shown = str(document)

    if unresolved:
        print(
            f"check_hardening: {len(unresolved)} unresolved reference(s) in {shown}",
            file=sys.stderr,
        )
        for item in unresolved:
            print(f"  {shown}:{item.line}: `{item.raw}` -- {item.reason}", file=sys.stderr)
        return 1

    print(f"check_hardening: {len(references)} reference(s) in {shown} resolve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
