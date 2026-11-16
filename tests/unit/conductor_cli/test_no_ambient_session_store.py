"""No test may lean on the session store's **default** paths.

``src/llms/native_cli/sessions.py`` works out ``DEFAULT_SESSION_PATH`` when
its module is imported, so the constant holds the home of whoever started
the test run -- the **operator's** home, not the test's. A store built
without a path reads that file, and a legacy entry of the same provider
that happens to be there is counted as a session of the worker the test is
looking at. The test then fails on a machine that has used Conductor and
passes on one that has not, which is the worst kind of failure: it is
green exactly where nobody looks.

The same is true of the manager's own state file and of the handover notes
a rotation writes. So a store or a manager in the tests says where it
reads:

* ``NativeCliSessionStore(...)`` is given a path, positionally or by keyword;
* ``WorkerSessionManager(...)`` is given **both** a ``path`` and a ``store``
  -- the state file it reads and the legacy map it merges.

This test is the guard. It reads the source of every test file, so it
fails on a construction that is only there in one branch, and it fails
**before** the run that would read the operator's files.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: The root of the repository: this file is ``<root>/tests/unit/conductor_cli/``.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The tests themselves.
TESTS_ROOT = REPO_ROOT / "tests"

#: The marker a construction has to carry to keep the default on purpose.
MARKER = "# ambient-store-default:"


def _test_files() -> list[Path]:
    """Every python file of the test suite.

    Not only the ``test_*.py`` files: a helper or a ``conftest.py`` can
    build a store just as well.
    """
    return sorted(TESTS_ROOT.rglob("*.py"))


def _aliases(tree: ast.AST, name: str) -> set[str]:
    """Every name a file calls the given class by.

    ``from ... import NativeCliSessionStore as Store`` hides the class
    behind another name, and the guard has to see through it.
    """
    found = {name}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            if alias.name == name:
                found.add(alias.asname or alias.name)
    return found


def _callee(node: ast.Call) -> str:
    """The name of the thing being called, however it is spelled."""
    function = node.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return ""


def _keywords(node: ast.Call) -> set[str]:
    return {keyword.arg for keyword in node.keywords if keyword.arg}


def _dict_keys_by_name(tree: ast.AST) -> dict[str, set[str]]:
    """The keys of every dict literal a name is assigned in the file.

    ``params = {"path": ..., "store": ...}`` followed by
    ``WorkerSessionManager(**params)`` is a construction that says where it
    reads, and this is how the guard sees it.
    """
    keys: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        names = {
            target.id for target in node.targets if isinstance(target, ast.Name)
        }
        literal_keys = {
            key.value
            for key in node.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        for name in names:
            keys.setdefault(name, set()).update(literal_keys)
    return keys


def _kwargs_dict_keys(node: ast.Call, dict_keys: dict[str, set[str]]) -> set[str]:
    """The keys of the dict a ``**kwargs`` call spreads."""
    spread: set[str] = set()
    for keyword in node.keywords:
        if keyword.arg is None and isinstance(keyword.value, ast.Name):
            spread |= dict_keys.get(keyword.value.id, set())
    return spread


def _is_deliberate(lineno: int, lines: list[str]) -> bool:
    """Whether the construction says on purpose that it keeps the default.

    The marker has to be in a **comment** of its own on the line above: a
    marker inside a docstring is not a decision, and one that is easy to
    hide is one nobody will notice.
    """
    number = lineno - 1
    if number < 1 or number > len(lines):
        return False
    text = lines[number - 1].strip()
    if not text.startswith("#") or MARKER not in text:
        return False
    return bool(text.split(MARKER, 1)[1].strip())


def _offenders(tree: ast.AST, lines: list[str]) -> list[str]:
    """Every construction in the file that leans on a default path."""
    dict_keys = _dict_keys_by_name(tree)
    stores = _aliases(tree, "NativeCliSessionStore")
    managers = _aliases(tree, "WorkerSessionManager")
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _callee(node)
        message: str | None = None
        if callee in stores:
            # A path is given positionally or by keyword.
            if not node.args and "path" not in _keywords(node):
                message = f"line {node.lineno}: NativeCliSessionStore() without a path"
        elif callee in managers:
            spread = _kwargs_dict_keys(node, dict_keys)
            named = _keywords(node) | spread
            if "store" not in named:
                message = f"line {node.lineno}: WorkerSessionManager() without a store"
            elif "path" not in named:
                message = f"line {node.lineno}: WorkerSessionManager() without a path"
        if message is None:
            continue
        if _is_deliberate(node.lineno, lines):
            continue
        offenders.append(message)
    return offenders


def test_there_are_tests_to_check() -> None:
    # A guard that checks nothing is not a guard.
    assert len(_test_files()) > 50


def test_no_test_reads_the_default_session_store() -> None:
    """No test file may lean on the session store's default paths.

    The whole tree is scanned in **one** test on purpose: the repository has
    well over a thousand test files, and a scan that is one pytest item per
    file would add well over a thousand items to every run for the same
    coverage. One assertion that names every offender says the same thing.
    """
    offenders: list[str] = []
    for path in _test_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for offender in _offenders(tree, source.splitlines()):
            offenders.append(f"{path.relative_to(REPO_ROOT)} {offender}")

    assert not offenders, (
        "these tests read a default path of the session store, which is the "
        "home of whoever ran them:\n" + "\n".join(offenders)
    )
