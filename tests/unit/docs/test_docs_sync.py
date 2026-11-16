"""Operator docs stay aligned with the migration head, the CLI, and source names.

A sentence that says the head is 140 keeps being followed after the chain has
moved, and a `conductor` example keeps naming a command the parser no longer
builds. The values compared here are read from the tree at test time: the
head from `migrations/versions`, command names from `add_parser` in
`src/conductor_cli/cli.py` (and the helpers that file calls to register
commands), and env tokens from the product source. Nothing in those sets is
hard-coded.

The env scan includes `src/`, the operator launch scripts under `scripts/`,
and `libs/ptc-cli/` because the docs name daemon and launcher variables those
files define (`CONDUCTOR_DAEMON_TOKEN_STREAM`,
`CONDUCTOR_DISPATCH_SERIAL_PROVIDERS`). An env name none of those files knows
is still stale.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_CLI = _ROOT / "src" / "conductor_cli" / "cli.py"
_VERSIONS = _ROOT / "migrations" / "versions"

_HEAD_DOCS = (
    "docs/operations.md",
    "docs/CLI-SUPERAGENT.md",
    "docs/REVIEW-MERGE-API.md",
)
_COMMAND_DOCS = _HEAD_DOCS + ("docs/COND.md",)
_ENV_DOCS = (
    "docs/ports/auto-dispatch.md",
    "docs/operations.md",
    "docs/ports/worker-isolation.md",
)
_INSTALL_DOCS = (
    "docs/COND.md",
    "docs/CLI-SUPERAGENT.md",
)

# Present-tense claims of the schema head. "head is <rev>" is always a claim
# about this tree, even when the same line names a commit. A printed head is
# the output of a run, so `run on <commit>` governs only the rest of that
# sentence; a later sentence on the line is still a current claim.
_CURRENT_HEAD = re.compile(
    r"(?i)\b(?:current\s+|schema\s+)?head\s+is\s+`?(\d{3,})`?\b"
    r"|\bprints?\s+(?:the\s+)?(?:current\s+)?(?:single\s+)?head\s+`?(\d{3,})`?\b"
    r"|\bprints?\s+`?(\d{3,})\s*\(head\)`?"
)
_RUN_ON_COMMIT = re.compile(r"(?i)\brun on\s+`?[0-9a-f]{7,40}`?")
_CONDUCTOR_CMD = re.compile(r"(?<![\w./~-])conductor\s+([a-z][a-z0-9-]*)")
_ENV_TOKEN = re.compile(r"\b(?:CONDUCTOR|NATIVE_CLI|AUTONOMY|DSH|PTC)_[A-Z0-9_]+\b")
# A path whose segment is int-r2: ~/dir/int-r2, /dir/int-r2, ./int-r2,
# ../int-r2, dir/int-r2, or a relative path that starts there
# (`int-r2/scripts/cond`). A bare "int-r2" word has no slash and is not a path.
_INT_R2_INSTALL_PATH = re.compile(
    r"(?<![\w.-])"
    r"(?:"
    r"(?:~/|\.{1,2}/|/)(?:[\w.-]+/)*int-r2(?:/[\w.-]*)*"
    r"|"
    r"(?:[\w.-]+/)+int-r2(?:/[\w.-]*)*"
    r"|"
    r"int-r2(?:/[\w.-]*)+"
    r")"
)
# Markdown under the source roots is documentation, not source: a README that
# names a stale variable must not vouch for the same name in another doc.
_SKIP_SRC_SUFFIXES = {".md", ".rst", ".txt", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".otf", ".pyc", ".so", ".whl"}
_SOURCE_ROOTS = (
    _ROOT / "src",
    _ROOT / "libs" / "ptc-cli",
    _ROOT / "scripts",
)


def _text(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _stale(doc: str, tokens: list[str]) -> str:
    return "; ".join(f"{doc} stale token {token}" for token in tokens)


def _string_literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value:
        return node.value
    return None


def _down_revisions(node: ast.AST | None) -> list[str]:
    if node is None or (isinstance(node, ast.Constant) and node.value is None):
        return []
    single = _string_literal(node)
    if single is not None:
        return [single]
    if isinstance(node, (ast.Tuple, ast.List)):
        found: list[str] = []
        for item in node.elts:
            text = _string_literal(item)
            if text:
                found.append(text)
        return found
    return []


def _migration_head() -> str:
    """The revision no other file names as ``down_revision``.

    Reads the migration sources as text. It does not import them and it does
    not open a database.
    """
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in sorted(_VERSIONS.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found: dict[str, ast.AST] = {}
        for node in tree.body:
            targets: list[ast.AST] = []
            value: ast.AST | None = None
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
                value = node.value
            if value is None:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                    found[target.id] = value
        revision = _string_literal(found.get("revision"))
        if revision is None:
            continue
        revisions.add(revision)
        parents.update(_down_revisions(found.get("down_revision")))
    heads = sorted(revisions - parents)
    assert len(heads) == 1, f"migrations/versions heads {heads}"
    return heads[0]


def _recorded_print_spans(line: str) -> list[tuple[int, int]]:
    """Spans of a head print that is the output of a run on a named commit.

    The anchor is `run on <sha>` and it governs only until the sentence or
    table cell ends. A current instruction that merely shares the line is
    outside those spans.
    """
    spans: list[tuple[int, int]] = []
    for anchor in _RUN_ON_COMMIT.finditer(line):
        rest = line[anchor.end() :]
        stop = len(rest)
        for index, char in enumerate(rest):
            if char in ".|":
                stop = index
                break
        window = rest[:stop]
        for match in _CURRENT_HEAD.finditer(window):
            # Group 1 states what the head is, so a commit pin does not cover
            # it. The print groups are the recorded output of that run.
            if match.group(1) is not None:
                continue
            spans.append((anchor.end() + match.start(), anchor.end() + match.end()))
    return spans


def _stated_heads(text: str) -> list[str]:
    stated: list[str] = []
    for line in text.splitlines():
        hidden = _recorded_print_spans(line)
        for match in _CURRENT_HEAD.finditer(line):
            if any(match.start() < end and match.end() > start for start, end in hidden):
                continue
            rev = next(group for group in match.groups() if group)
            if rev not in stated:
                stated.append(rev)
    return stated


def _loop_bindings(node: ast.For) -> dict[str, set[str]]:
    if not isinstance(node.target, ast.Tuple) or not isinstance(node.iter, (ast.Tuple, ast.List)):
        return {}
    names = [elt.id for elt in node.target.elts if isinstance(elt, ast.Name)]
    if len(names) != len(node.target.elts):
        return {}
    bound = {name: set() for name in names}
    for elt in node.iter.elts:
        slots = elt.elts if isinstance(elt, (ast.Tuple, ast.List)) else [elt]
        for name, slot in zip(names, slots, strict=False):
            text = _string_literal(slot)
            if text:
                bound[name].add(text)
    return {name: values for name, values in bound.items() if values}


def _arg_strings(node: ast.AST, bindings: dict[str, set[str]]) -> set[str]:
    literal = _string_literal(node)
    if literal is not None:
        return {literal}
    if isinstance(node, ast.Name):
        return set(bindings.get(node.id, ()))
    return set()


class _CommandCollector(ast.NodeVisitor):
    """Top-level ``add_parser`` names, plus helpers called with that subparser."""

    def __init__(self, live: set[str], imports: dict[str, tuple[str, int, str]]) -> None:
        self.live = live
        self.imports = imports
        self.bindings: dict[str, set[str]] = {}
        self.found: set[str] = set()
        self.follow: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_For(self, node: ast.For) -> None:
        saved = dict(self.bindings)
        self.bindings.update(_loop_bindings(node))
        self.generic_visit(node)
        self.bindings = saved

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "add_parser"
            and isinstance(func.value, ast.Name)
            and func.value.id in self.live
            and node.args
        ):
            self.found |= _arg_strings(node.args[0], self.bindings)
        elif isinstance(func, ast.Name) and func.id in self.imports:
            if any(isinstance(arg, ast.Name) and arg.id in self.live for arg in node.args):
                self.follow.append(func.id)
        self.generic_visit(node)


def _import_map(tree: ast.AST) -> dict[str, tuple[str, int, str]]:
    found: dict[str, tuple[str, int, str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        for alias in node.names:
            found[alias.asname or alias.name] = (node.module, node.level or 0, alias.name)
    return found


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not defined")


def _module_file(module: str, level: int) -> Path | None:
    if level < 1:
        return None
    base = _CLI.parent
    for _ in range(level - 1):
        base = base.parent
    path = base.joinpath(*module.split(".")).with_suffix(".py")
    return path if path.is_file() else None


def _subparsers_name(fn: ast.FunctionDef) -> str:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        value = node.value
        if not isinstance(target, ast.Name) or not isinstance(value, ast.Call):
            continue
        func = value.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "add_subparsers"
            and isinstance(func.value, ast.Name)
            and func.value.id == "parser"
        ):
            return target.id
    raise AssertionError("cli.py does not build a command subparser")


def _names_in_function(fn: ast.FunctionDef, param: str, imports: dict[str, tuple[str, int, str]]) -> _CommandCollector:
    collector = _CommandCollector({param}, imports)
    for stmt in fn.body:
        collector.visit(stmt)
    return collector


def _cli_commands() -> set[str]:
    source = _CLI.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = _import_map(tree)
    build = _find_function(tree, "build_parser")
    subparsers = _subparsers_name(build)
    collector = _names_in_function(build, subparsers, imports)
    names = set(collector.found)
    for local in collector.follow:
        module, level, qualname = imports[local]
        path = _module_file(module, level)
        if path is None:
            continue
        helper_tree = ast.parse(path.read_text(encoding="utf-8"))
        helper = _find_function(helper_tree, qualname)
        param = helper.args.args[0].arg
        names |= _names_in_function(helper, param, {}).found
    assert names, "cli.py add_parser calls yielded no commands"
    return names


def _src_env_tokens() -> set[str]:
    """Env names the source tree defines or reads.

    `src/` is the product source; the launch scripts and the CLI library are
    operator source too, and the docs legitimately name variables that only
    they define. A name no source file mentions is still stale.
    """
    tokens: set[str] = set()
    for root in _SOURCE_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if "tests" in path.parts:
                continue
            if path.suffix.lower() in _SKIP_SRC_SUFFIXES:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            tokens.update(_ENV_TOKEN.findall(text))
    assert tokens, "the source tree has no env tokens"
    return tokens


def _environment_table_names(text: str) -> set[str]:
    """Env names in the Variable column of the ``### Environment`` table.

    A mention in the prose, in the header, or in another row's description is
    not a row: the check is for a documented variable, not for the string.
    """
    match = re.search(r"^### Environment\s*$", text, re.MULTILINE)
    assert match, "auto-dispatch.md has no Environment section"
    rest = text[match.end() :]
    nxt = re.search(r"^#{1,6} ", rest, re.MULTILINE)
    section = rest[: nxt.start()] if nxt else rest
    names: set[str] = set()
    for line in section.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells:
            continue
        names.update(_ENV_TOKEN.findall(cells[0]))
    return names


@pytest.mark.parametrize("doc", _HEAD_DOCS)
def test_stated_head_is_the_derived_head(doc: str) -> None:
    head = _migration_head()
    stated = _stated_heads(_text(doc))
    wrong = [rev for rev in stated if rev != head]
    assert not wrong, _stale(doc, wrong)


@pytest.mark.parametrize(
    "sentence",
    (
        "Run on the backend host: the head is 140.",
        "Run on the backend host at `aa3ab87b2`: the head is 140.",
        "Measured on `aa3ab87b2`. The head is 140.",
        "Run on `aa3ab87b2`. Confirm `alembic heads` prints `140 (head)`.",
    ),
)
def test_historical_markers_cannot_hide_a_current_head(sentence: str) -> None:
    """A commit id or "run on" on the same line does not drop a current claim."""
    doc = "docs/operations.md"
    stated = _stated_heads(_text(doc) + "\n" + sentence)
    assert "140" in stated, f"{doc} stale token 140"


@pytest.mark.skip(reason="docs/RELEASE-READINESS.md is not in the public snapshot")
def test_commit_pinned_run_print_is_not_a_current_head() -> None:
    """A run pinned to a commit does not count, and does not hide the next claim."""
    line = next(
        row
        for row in _text("docs/RELEASE-READINESS.md").splitlines()
        if "run on `aa3ab87b2` prints `" in row
    )
    assert _stated_heads(line) == []
    stated = _stated_heads(line + " The head is 139.")
    assert "140" not in stated
    assert "139" in stated, "docs/RELEASE-READINESS.md stale token 139"


@pytest.mark.parametrize("doc", _COMMAND_DOCS)
def test_conductor_commands_are_registered(doc: str) -> None:
    commands = _cli_commands()
    mentioned = []
    for match in _CONDUCTOR_CMD.finditer(_text(doc)):
        name = match.group(1)
        if name not in mentioned:
            mentioned.append(name)
    missing = [name for name in mentioned if name not in commands]
    assert not missing, _stale(doc, missing)


def _alias_of(token: str) -> str | None:
    """The other spelling of a renamed runtime variable.

    The runtime variables move from ``PTC_*`` to ``CONDUCTOR_*`` and the old
    name stays readable for a release. The reader that does it builds the
    alias from the new name (``"PTC_" + name[len("CONDUCTOR_"):]``), so the old
    spelling need not appear in the source as a literal, and a doc that still
    names it (or names only the new one) is not stale while either spelling is
    in the source.
    """
    for old, new in (("PTC_", "CONDUCTOR_"), ("CONDUCTOR_", "PTC_")):
        if token.startswith(old):
            return new + token[len(old) :]
    return None


def _env_known(token: str, known: set[str]) -> bool:
    if token in known:
        return True
    alias = _alias_of(token)
    return alias is not None and alias in known


@pytest.mark.parametrize("doc", _ENV_DOCS)
def test_named_env_vars_exist_in_source(doc: str) -> None:
    known = _src_env_tokens()
    named = []
    for match in _ENV_TOKEN.finditer(_text(doc)):
        token = match.group(0)
        if token not in named:
            named.append(token)
    missing = [token for token in named if not _env_known(token, known)]
    assert not missing, _stale(doc, missing)


def test_env_alias_accepts_either_spelling_of_a_renamed_variable() -> None:
    """A PTC_* name and its CONDUCTOR_* rename count as one variable."""
    known = {"CONDUCTOR_CONFIG_FILE", "PTC_THEME"}
    assert _env_known("PTC_CONFIG_FILE", known)
    assert _env_known("CONDUCTOR_THEME", known)
    assert not _env_known("CONDUCTOR_NO_SUCH_SETTING", known)
    assert not _env_known("NATIVE_CLI_CONFIG_FILE", known)


def test_unknown_env_name_in_a_doc_is_reported() -> None:
    """The scan is not vacuous: a made-up name in a doc table is stale."""
    known = _src_env_tokens()
    text = _text("docs/operations.md") + "\n| `CONDUCTOR_R7_NOT_A_SETTING` | unset |\n"
    named = set(_ENV_TOKEN.findall(text))
    assert "CONDUCTOR_R7_NOT_A_SETTING" in {t for t in named if not _env_known(t, known)}


@pytest.mark.parametrize("doc", ("docs/ports/auto-dispatch.md",))
def test_auto_dispatch_env_table_lists_dispatch_repo(doc: str) -> None:
    names = _environment_table_names(_text(doc))
    assert "CONDUCTOR_DISPATCH_REPO" in names, (
        f"{doc} stale token CONDUCTOR_DISPATCH_REPO"
    )


@pytest.mark.parametrize("doc", ("docs/ports/auto-dispatch.md",))
def test_auto_dispatch_env_table_ignores_a_prose_mention(doc: str) -> None:
    """Dropping the row but naming it in another cell is still missing."""
    text = _text(doc)
    without_row = "\n".join(
        line
        for line in text.splitlines()
        if not (line.lstrip().startswith("|") and "CONDUCTOR_DISPATCH_REPO" in line)
    )
    prose = without_row + (
        "\n\nThe planner reads CONDUCTOR_DISPATCH_REPO.\n"
        "| `CONDUCTOR_OTHER` | unset | mentions CONDUCTOR_DISPATCH_REPO here |\n"
    )
    assert "CONDUCTOR_DISPATCH_REPO" not in _environment_table_names(prose)


@pytest.mark.parametrize("doc", _INSTALL_DOCS)
def test_no_int_r2_install_path(doc: str) -> None:
    paths: list[str] = []
    for match in _INT_R2_INSTALL_PATH.finditer(_text(doc)):
        path = match.group(0)
        if path not in paths:
            paths.append(path)
    assert not paths, _stale(doc, paths)
