"""Unit tests for the local tree-sitter change-impact index and CLI."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from src.conductor_cli import cli
from src.conductor_cli.impact.analyze import impact_report, load_catalog
from src.conductor_cli.impact.extract import extract_source
from src.conductor_cli.impact.index import ImpactIndex
from src.conductor_cli.impact.model import ImpactError
from src.conductor_cli.impact.review import (
    parse_range,
    parse_unified_diff,
    review_range,
)

LIB = '''\
def helper():
    return 1

def other():
    return helper()
'''

APP = '''\
from pkg.lib import helper

def main():
    return helper()
'''

TEST_LIB = '''\
from pkg.lib import helper

def test_helper():
    assert helper() == 1
'''

DIFF_HELPER = '''\
diff --git a/pkg/lib.py b/pkg/lib.py
--- a/pkg/lib.py
+++ b/pkg/lib.py
@@ -2 +2 @@
-    return 1
+    return 2
'''

DIFF_HELPER_AND_TEST = DIFF_HELPER + '''\
diff --git a/tests/test_lib.py b/tests/test_lib.py
--- a/tests/test_lib.py
+++ b/tests/test_lib.py
@@ -4 +4 @@
-    assert helper() == 1
+    assert helper() == 2
'''


def write_repo(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def index_repo(root: Path, files: dict[str, str]) -> tuple[ImpactIndex, object]:
    write_repo(root, files)
    index = ImpactIndex(root, root / "impact.sqlite")
    index.update()
    return index, load_catalog(index)


class FakeGit:
    def __init__(
        self,
        diff_text: str,
        blobs: dict[tuple[str, str], bytes],
        merge_bases: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self.diff_text = diff_text
        self.blobs = blobs
        self.merge_bases = merge_bases or {}
        self.specs: list[str] = []
        self.shown: list[tuple[str, str]] = []

    def diff(self, spec: str) -> str:
        self.specs.append(parse_range(spec))
        return self.diff_text

    def show(self, rev: str, path: str) -> bytes | None:
        self.shown.append((rev, path))
        return self.blobs.get((rev, path))

    def merge_base(self, base: str, head: str) -> str:
        found = self.merge_bases.get((base, head))
        if found is None:
            raise ImpactError(f"no merge base for {base}...{head}")
        return found


def _qualifiers(report: dict, key: str = "affected_functions") -> set[str]:
    return {item["qualified_name"] for item in report[key]}


def test_python_extracts_functions_imports_and_calls() -> None:
    facts = extract_source(
        "pkg/app.py",
        b"from .helper import helper\n\n"
        b"def main():\n"
        b"    return helper()\n",
        "python",
    )
    names = {item.name: item for item in facts.symbols}
    assert names["main"].start_line == 3
    assert names["main"].end_line == 4
    assert names["main"].qualified_name == "pkg/app.py::main"
    assert facts.imports[0].module == "helper"
    assert facts.imports[0].level == 1
    assert facts.imports[0].alias == "helper"
    assert [(item.caller, item.callee_name, item.line) for item in facts.calls] == [
        ("pkg/app.py::main", "helper", 4)
    ]


def test_getattr_subscript_and_callback_arguments_are_not_call_edges() -> None:
    source = '''\
def caller(name):
    getattr(mod, "helper")()
    obj[name]()

def mount():
    add_route("/users", users)

def helper():
    return 1

def users():
    return "ok"
'''
    facts = extract_source("pkg/dyn.py", source.encode(), "python")
    callees = {(item.caller, item.callee_name) for item in facts.calls}
    assert ("pkg/dyn.py::caller", "getattr") in callees
    assert ("pkg/dyn.py::caller", "helper") not in callees
    assert ("pkg/dyn.py::caller", "users") not in callees
    assert ("pkg/dyn.py::mount", "users") not in callees
    assert ("pkg/dyn.py::mount", "add_route") in callees


def test_self_calls_stay_on_the_enclosing_class() -> None:
    source = '''\
class A:
    def run(self):
        return 1

class B:
    def run(self):
        return 2

    def go(self):
        return self.run()
'''
    facts = extract_source("pkg/classes.py", source.encode(), "python")
    go = next(item for item in facts.calls if item.caller.endswith("::B.go"))
    assert go.callee_name == "run"
    assert go.receiver_kind == "self"
    assert go.enclosing_class == "pkg/classes.py::B"


def test_typescript_imports_arrow_functions_and_jsx() -> None:
    button = extract_source(
        "button.tsx",
        b'export function Button() {\n  return "ok";\n}\n',
        "tsx",
    )
    assert button.symbols[0].exported == "named"
    assert button.symbols[0].name == "Button"
    panel = extract_source(
        "panel.tsx",
        b'import { Button } from "./button";\n'
        b"export const Arrow = () => 1;\n"
        b"export function Panel() {\n"
        b"  return <Button />;\n"
        b"}\n",
        "tsx",
    )
    assert panel.imports[0].name == "Button"
    assert panel.imports[0].module == "./button"
    assert {item.name for item in panel.symbols} == {"Arrow", "Panel"}
    jsx = [item for item in panel.calls if item.callee_name == "Button"]
    assert jsx[0].caller == "panel.tsx::Panel"
    assert all(item.callee_name != "div" for item in panel.calls)


def test_incremental_index_uses_mtime_then_hash(tmp_path: Path) -> None:
    path = tmp_path / "pkg" / "lib.py"
    path.parent.mkdir()
    path.write_text(LIB)
    index = ImpactIndex(tmp_path, tmp_path / ".conductor" / "impact" / "index.sqlite")
    first = index.update()
    assert first["parsed"] == 1
    assert first["files"] == 1
    assert index.db_path == tmp_path / ".conductor" / "impact" / "index.sqlite"
    second = index.update()
    assert second["parsed"] == 0
    assert second["skipped"] == 1
    digest = index.file_row("pkg/lib.py")["content_hash"]
    path.write_text(LIB.replace("return 1", "return 2"))
    third = index.update()
    assert third["parsed"] == 1
    assert index.file_row("pkg/lib.py")["content_hash"] != digest
    path.write_text(LIB.replace("return 1", "return 2"))
    stamped = path.stat()
    os.utime(path, ns=(stamped.st_mtime_ns + 5_000_000_000, stamped.st_mtime_ns + 5_000_000_000))
    fourth = index.update()
    assert fourth["parsed"] == 0
    assert fourth["unchanged_hash"] == 1
    path.unlink()
    fifth = index.update()
    assert fifth["removed"] == 1
    assert index.file_row("pkg/lib.py") is None


def test_impact_reports_callers_files_tests_and_confidence(tmp_path: Path) -> None:
    index, catalog = index_repo(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/lib.py": LIB,
            "pkg/app.py": APP,
            "tests/test_lib.py": TEST_LIB,
        },
    )
    del index
    report = impact_report(catalog, tmp_path, ["pkg/lib.py::helper"])
    assert _qualifiers(report) >= {
        "pkg/lib.py::helper",
        "pkg/lib.py::other",
        "pkg/app.py::main",
        "tests/test_lib.py::test_helper",
    }
    by_name = {item["qualified_name"]: item for item in report["affected_functions"]}
    assert by_name["pkg/lib.py::other"]["confidence"] == 0.95
    assert by_name["pkg/app.py::main"]["confidence"] == 0.9
    assert {item["path"] for item in report["affected_files"]} >= {
        "pkg/lib.py",
        "pkg/app.py",
        "tests/test_lib.py",
    }
    assert any(item["path"] == "tests/test_lib.py" for item in report["affected_tests"])
    assert report["confidence"] == 0.9
    assert report["ambiguous"] == []


def test_relative_import_and_transitive_decay(tmp_path: Path) -> None:
    _, catalog = index_repo(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/helper.py": "def helper():\n    return 1\n",
            "pkg/app.py": "from .helper import helper\n\ndef main():\n    return helper()\n",
            "pkg/top.py": "from .app import main\n\ndef top():\n    return main()\n",
        },
    )
    report = impact_report(catalog, tmp_path, ["helper"])
    by_name = {item["qualified_name"]: item for item in report["affected_functions"]}
    assert by_name["pkg/app.py::main"]["confidence"] == 0.9
    assert by_name["pkg/top.py::top"]["confidence"] == round(0.9 * 0.85, 4)


def test_name_collision_is_listed_and_not_followed(tmp_path: Path) -> None:
    _, catalog = index_repo(
        tmp_path,
        {
            "pkg/a.py": "def helper():\n    return 1\n",
            "pkg/b.py": "def helper():\n    return 2\n",
            "pkg/c.py": "def run():\n    return helper()\n",
        },
    )
    collided = impact_report(catalog, tmp_path, ["helper"])
    assert collided["confidence"] == 0.0
    assert collided["affected_functions"] == []
    assert len(collided["ambiguous"][0]["candidates"]) == 2
    specific = impact_report(catalog, tmp_path, ["pkg/a.py::helper"])
    assert "pkg/c.py::run" not in _qualifiers(specific)
    assert specific["ambiguous"][0]["name"] == "helper"
    assert specific["confidence"] == 0.5


def test_self_method_does_not_cross_classes(tmp_path: Path) -> None:
    _, catalog = index_repo(
        tmp_path,
        {
            "pkg/classes.py": '''\
class A:
    def run(self):
        return 1

class B:
    def run(self):
        return 2

    def go(self):
        return self.run()
''',
        },
    )
    report = impact_report(catalog, tmp_path, ["pkg/classes.py::B.run"])
    assert "pkg/classes.py::B.go" in _qualifiers(report)
    assert "pkg/classes.py::A.run" not in _qualifiers(report)
    other = impact_report(catalog, tmp_path, ["pkg/classes.py::A.run"])
    assert "pkg/classes.py::B.go" not in _qualifiers(other)


def test_dynamic_edges_do_not_affect_the_named_function(tmp_path: Path) -> None:
    _, catalog = index_repo(
        tmp_path,
        {
            "pkg/dyn.py": '''\
def caller(name):
    getattr(mod, "helper")()
    obj[name]()

def mount():
    add_route("/users", users)

def helper():
    return 1

def users():
    return "ok"
''',
        },
    )
    helper = impact_report(catalog, tmp_path, ["pkg/dyn.py::helper"])
    users = impact_report(catalog, tmp_path, ["pkg/dyn.py::users"])
    assert _qualifiers(helper) == {"pkg/dyn.py::helper"}
    assert _qualifiers(users) == {"pkg/dyn.py::users"}


def test_tsx_component_use_is_a_caller(tmp_path: Path) -> None:
    _, catalog = index_repo(
        tmp_path,
        {
            "button.tsx": 'export function Button() {\n  return "ok";\n}\n',
            "panel.tsx": (
                'import { Button } from "./button";\n'
                "export function Panel() {\n"
                "  return <Button />;\n"
                "}\n"
            ),
        },
    )
    report = impact_report(catalog, tmp_path, ["button.tsx::Button"])
    match = next(item for item in report["affected_functions"] if item["name"] == "Panel")
    assert match["confidence"] == 0.9


def test_module_attribute_call_and_star_import(tmp_path: Path) -> None:
    _, catalog = index_repo(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/lib.py": "def helper():\n    return 1\n",
            "pkg/via_module.py": "import pkg.lib\n\ndef main():\n    return pkg.lib.helper()\n",
            "pkg/via_star.py": "from pkg.lib import *\n\ndef star():\n    return helper()\n",
        },
    )
    report = impact_report(catalog, tmp_path, ["pkg/lib.py::helper"])
    by_name = {item["qualified_name"]: item for item in report["affected_functions"]}
    assert by_name["pkg/via_module.py::main"]["confidence"] == 0.9
    assert by_name["pkg/via_star.py::star"]["confidence"] == 0.6


def test_decorator_reference_is_a_caller(tmp_path: Path) -> None:
    _, catalog = index_repo(
        tmp_path,
        {
            "pkg/deco.py": "def helper(fn):\n    return fn\n\n@helper\ndef caller():\n    return 1\n",
        },
    )
    report = impact_report(catalog, tmp_path, ["pkg/deco.py::helper"])
    assert "pkg/deco.py::caller" in _qualifiers(report)


def test_diff_parser_tracks_added_and_removed_lines() -> None:
    files = parse_unified_diff(DIFF_HELPER)
    assert files[0].old_path == "pkg/lib.py"
    assert files[0].new_path == "pkg/lib.py"
    assert files[0].old_lines == {2}
    assert files[0].new_lines == {2}
    added = parse_unified_diff(
        "diff --git a/pkg/new.py b/pkg/new.py\n"
        "--- /dev/null\n"
        "+++ b/pkg/new.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def created():\n"
        "+    return 1\n"
    )
    assert added[0].old_path is None
    assert added[0].new_lines == {1, 2}
    with pytest.raises(ImpactError):
        parse_range("not-a-range")
    with pytest.raises(ImpactError):
        parse_range("--all..HEAD")


def test_review_flags_untouched_callers_until_a_test_changes(tmp_path: Path) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/lib.py": LIB,
        "pkg/app.py": APP,
        "tests/test_lib.py": TEST_LIB,
    }
    write_repo(tmp_path, files)
    blobs = {("head", rel): content.encode() for rel, content in files.items()}
    blobs.update({("base", rel): content.encode() for rel, content in files.items()})
    flagged = review_range(
        tmp_path,
        "base..head",
        index=ImpactIndex(tmp_path, tmp_path / "impact.sqlite"),
        git=FakeGit(DIFF_HELPER, blobs),
    )
    assert flagged["flags"]
    flag = next(item for item in flagged["flags"] if item["symbol"] == "pkg/lib.py::helper")
    callers = {item["qualified_name"] for item in flag["untouched_callers"]}
    assert "pkg/lib.py::other" in callers
    assert "pkg/app.py::main" in callers
    assert flag["tested"] is False

    clear = review_range(
        tmp_path,
        "base..head",
        index=ImpactIndex(tmp_path, tmp_path / "impact-tested.sqlite"),
        git=FakeGit(DIFF_HELPER_AND_TEST, blobs),
    )
    assert all(item["symbol"] != "pkg/lib.py::helper" for item in clear["flags"])


def test_text_report_lists_sections(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from src.conductor_cli.impact.cli import render_result

    _, catalog = index_repo(tmp_path, {"pkg/lib.py": LIB, "pkg/app.py": APP})
    report = impact_report(catalog, tmp_path, ["pkg/lib.py::helper"])
    render_result(report, json_mode=False)
    text = capsys.readouterr().out
    assert "affected functions:" in text
    assert "pkg/app.py::main" in text
    assert "ambiguous:" in text
    assert "(none)" in text


def test_cli_json_stays_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    write_repo(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/lib.py": LIB,
            "pkg/app.py": APP,
            "tests/test_lib.py": TEST_LIB,
        },
    )

    def fail_client(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("impact must not open an API client")

    monkeypatch.setattr(cli, "APIClient", fail_client)
    code = cli.main(
        [
            "impact",
            "--root",
            str(tmp_path),
            "--index",
            str(tmp_path / "impact.sqlite"),
            "pkg/lib.py::helper",
            "--json",
        ]
    )
    assert code == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["api_version"] == "v1"
    assert envelope["command"] == "impact"
    payload = envelope["data"]
    assert payload["mode"] == "impact"
    assert "pkg/app.py::main" in {item["qualified_name"] for item in payload["affected_functions"]}
    assert "affected_tests" in payload
    assert "ambiguous" in payload


def test_function_local_import_shadows_module_definition(tmp_path: Path) -> None:
    _, catalog = index_repo(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "def helper():\n    return 1\n",
            "pkg/b.py": '''\
def helper():
    return 0

def caller():
    from pkg.a import helper
    return helper()

def outer():
    from pkg.a import helper

    def inner():
        return helper()

def uses_local():
    return helper()
''',
        },
    )
    imported = impact_report(catalog, tmp_path, ["pkg/a.py::helper"])
    local = impact_report(catalog, tmp_path, ["pkg/b.py::helper"])
    imported_by = {item["qualified_name"]: item for item in imported["affected_functions"]}
    local_by = {item["qualified_name"]: item for item in local["affected_functions"]}
    assert imported_by["pkg/b.py::caller"]["confidence"] == 0.9
    assert imported_by["pkg/b.py::outer.inner"]["confidence"] == 0.9
    assert "pkg/b.py::uses_local" not in imported_by
    assert "pkg/b.py::caller" not in local_by
    assert "pkg/b.py::outer.inner" not in local_by
    assert local_by["pkg/b.py::uses_local"]["confidence"] == 0.95


def test_module_definition_still_hides_module_import(tmp_path: Path) -> None:
    _, catalog = index_repo(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "def helper():\n    return 1\n",
            "pkg/b.py": '''\
from pkg.a import helper

def helper():
    return 0

def caller():
    return helper()
''',
        },
    )
    imported = impact_report(catalog, tmp_path, ["pkg/a.py::helper"])
    local = impact_report(catalog, tmp_path, ["pkg/b.py::helper"])
    assert "pkg/b.py::caller" not in _qualifiers(imported)
    by_name = {item["qualified_name"]: item for item in local["affected_functions"]}
    assert by_name["pkg/b.py::caller"]["confidence"] == 0.95


def test_arrow_and_function_expression_defaults_record_calls() -> None:
    facts = extract_source(
        "run.ts",
        b"function helper() { return 1; }\n"
        b"function helper2() { return 2; }\n"
        b"const run = (x = helper()) => x;\n"
        b"const expr = function (x = helper2()) { return x; };\n"
        b"function declared(x = helper()) { return x; }\n"
        b"function outer() {\n"
        b"  const nested = (x = helper()) => x;\n"
        b"  return use((x = helper2()) => x);\n"
        b"}\n",
        "typescript",
    )
    calls = {(item.caller, item.callee_name) for item in facts.calls}
    assert ("run.ts::run", "helper") in calls
    assert ("run.ts::expr", "helper2") in calls
    assert ("run.ts::declared", "helper") in calls
    assert ("run.ts::outer.nested", "helper") in calls
    # An anonymous arrow has no symbol. Its default stays on the enclosing function.
    assert ("run.ts::outer", "helper2") in calls
    assert ("run.ts::outer", "use") in calls
    assert ("", "helper") not in calls
    assert ("run.ts::outer", "helper") not in calls


def test_typescript_default_makes_the_function_and_its_callers_affected(tmp_path: Path) -> None:
    _, catalog = index_repo(
        tmp_path,
        {
            "helper.ts": "export function helper() {\n  return 1;\n}\n",
            "run.ts": (
                'import { helper } from "./helper";\n'
                "export const run = (x = helper()) => x;\n"
            ),
            "app.ts": (
                'import { run } from "./run";\n'
                "export function caller() {\n"
                "  return run();\n"
                "}\n"
            ),
        },
    )
    report = impact_report(catalog, tmp_path, ["helper.ts::helper"])
    by_name = {item["qualified_name"]: item for item in report["affected_functions"]}
    assert by_name["run.ts::run"]["confidence"] == 0.9
    assert by_name["app.ts::caller"]["confidence"] == round(0.9 * 0.85, 4)


def test_python_defaults_stay_in_the_enclosing_scope() -> None:
    facts = extract_source(
        "app.py",
        b"def helper():\n"
        b"    return 1\n\n"
        b"def run(x=helper()):\n"
        b"    return x\n\n"
        b"def outer():\n"
        b"    def run(x=helper()):\n"
        b"        return x\n",
        "python",
    )
    calls = {(item.caller, item.callee_name) for item in facts.calls}
    assert ("", "helper") in calls
    assert ("app.py::outer", "helper") in calls
    assert ("app.py::run", "helper") not in calls
    assert ("app.py::outer.run", "helper") not in calls


def test_oversized_file_drops_previous_facts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.conductor_cli.impact import index as impact_index

    monkeypatch.setattr(impact_index, "MAX_FILE_BYTES", 80)
    path = tmp_path / "pkg" / "lib.py"
    path.parent.mkdir()
    small = "def helper():\n    return 1\n\ndef other():\n    return helper()\n"
    large = "def replacement():\n    return 1\n" + ("# pad the file past the limit\n" * 4)
    assert len(small.encode()) <= 80
    assert len(large.encode()) > 80
    path.write_text(small)
    index = ImpactIndex(tmp_path, tmp_path / "impact.sqlite")
    index.update()
    assert {item.name for item in index.load_facts()[1]} == {"helper", "other"}
    path.write_text(large)
    skipped = index.update()
    assert skipped["skipped"] == 1
    assert skipped["parsed"] == 0
    files, symbols, calls, imports = index.load_facts()
    assert files == []
    assert symbols == []
    assert calls == []
    assert imports == []
    assert index.file_row("pkg/lib.py") is None
    again = index.update()
    assert again["parsed"] == 0
    assert index.load_facts()[1] == []
    path.write_text("def replacement():\n    return 1\n")
    restored = index.update()
    assert restored["parsed"] == 1
    assert {item.name for item in index.load_facts()[1]} == {"replacement"}


DIFF_DELETE_ONLY_CALLER = """\
diff --git a/pkg/app.py b/pkg/app.py
--- a/pkg/app.py
+++ b/pkg/app.py
@@ -4 +3,0 @@ def main():
-    debug = True
diff --git a/pkg/lib.py b/pkg/lib.py
--- a/pkg/lib.py
+++ b/pkg/lib.py
@@ -2 +2 @@ def helper():
-    return 1
+    return 2
"""

DIFF_REMOVE_METHOD = """\
diff --git a/pkg/box.py b/pkg/box.py
--- a/pkg/box.py
+++ b/pkg/box.py
@@ -2,3 +1,0 @@ class Box:
-    def helper(self):
-        return 1
-
"""

DIFF_DELETE_HELPER = """\
diff --git a/pkg/lib.py b/pkg/lib.py
--- a/pkg/lib.py
+++ b/pkg/lib.py
@@ -1,3 +0,0 @@
-def helper():
-    return 1
-
"""


def test_caller_edited_only_by_deletion_is_touched(tmp_path: Path) -> None:
    lib_old = "def helper():\n    return 1\n\ndef other():\n    return helper()\n"
    lib_new = "def helper():\n    return 2\n\ndef other():\n    return helper()\n"
    app_old = "from pkg.lib import helper\n\ndef main():\n    debug = True\n    return helper()\n"
    app_new = "from pkg.lib import helper\n\ndef main():\n    return helper()\n"
    write_repo(tmp_path, {"pkg/__init__.py": "", "pkg/lib.py": lib_new, "pkg/app.py": app_new})
    blobs = {
        ("base", "pkg/lib.py"): lib_old.encode(),
        ("head", "pkg/lib.py"): lib_new.encode(),
        ("base", "pkg/app.py"): app_old.encode(),
        ("head", "pkg/app.py"): app_new.encode(),
    }
    report = review_range(
        tmp_path,
        "base..head",
        index=ImpactIndex(tmp_path, tmp_path / "impact.sqlite"),
        git=FakeGit(DIFF_DELETE_ONLY_CALLER, blobs),
    )
    flag = next(item for item in report["flags"] if item["symbol"] == "pkg/lib.py::helper")
    callers = {item["qualified_name"] for item in flag["untouched_callers"]}
    assert "pkg/lib.py::other" in callers
    assert "pkg/app.py::main" not in callers


def test_removed_method_flags_untouched_self_callers(tmp_path: Path) -> None:
    old = '''\
class Box:
    def helper(self):
        return 1

    def go(self):
        return self.helper()

class Other:
    def go(self):
        return self.helper()
'''
    new = '''\
class Box:
    def go(self):
        return self.helper()

class Other:
    def go(self):
        return self.helper()
'''
    write_repo(tmp_path, {"pkg/box.py": new})
    blobs = {("base", "pkg/box.py"): old.encode(), ("head", "pkg/box.py"): new.encode()}
    report = review_range(
        tmp_path,
        "base..head",
        index=ImpactIndex(tmp_path, tmp_path / "impact.sqlite"),
        git=FakeGit(DIFF_REMOVE_METHOD, blobs),
    )
    flag = next(item for item in report["flags"] if item["symbol"] == "pkg/box.py::Box.helper")
    assert flag["removed"] is True
    callers = {item["qualified_name"] for item in flag["untouched_callers"]}
    assert callers == {"pkg/box.py::Box.go"}


def test_three_dot_review_loads_old_symbols_from_merge_base(tmp_path: Path) -> None:
    merge = "def helper():\n    return 1\n\ndef other():\n    return helper()\n"
    tip = "def unrelated():\n    return 0\n\n" + merge
    head = "def other():\n    return helper()\n"
    app = "from pkg.lib import helper\n\ndef main():\n    return helper()\n"
    write_repo(tmp_path, {"pkg/__init__.py": "", "pkg/lib.py": head, "pkg/app.py": app})
    blobs = {
        ("mb", "pkg/lib.py"): merge.encode(),
        ("main", "pkg/lib.py"): tip.encode(),
        ("feature", "pkg/lib.py"): head.encode(),
        ("mb", "pkg/app.py"): app.encode(),
        ("main", "pkg/app.py"): app.encode(),
        ("feature", "pkg/app.py"): app.encode(),
    }
    git = FakeGit(DIFF_DELETE_HELPER, blobs, {("main", "feature"): "mb"})
    report = review_range(
        tmp_path,
        "main...feature",
        index=ImpactIndex(tmp_path, tmp_path / "impact.sqlite"),
        git=git,
    )
    assert ("mb", "pkg/lib.py") in git.shown
    assert ("main", "pkg/lib.py") not in git.shown
    changed = {item["qualified_name"]: item for item in report["changed_symbols"]}
    assert changed["pkg/lib.py::helper"]["removed"] is True
    assert "pkg/lib.py::unrelated" not in changed
    flag = next(item for item in report["flags"] if item["symbol"] == "pkg/lib.py::helper")
    callers = {item["qualified_name"] for item in flag["untouched_callers"]}
    assert "pkg/lib.py::other" in callers
    assert "pkg/app.py::main" in callers


def test_real_git_review_reads_only_the_fixture_repo(tmp_path: Path) -> None:
    files = {
        "pkg/lib.py": "def helper():\n    return 1\n\ndef other():\n    return helper()\n",
    }
    write_repo(tmp_path, files)
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "impact@example.com")
    _git(tmp_path, "config", "user.name", "Impact Test")
    _git(tmp_path, "add", "pkg/lib.py")
    _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-m", "base")
    lib = tmp_path / "pkg" / "lib.py"
    lib.write_text("def helper():\n    return 2\n\ndef other():\n    return helper()\n")
    _git(tmp_path, "add", "pkg/lib.py")
    _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-m", "change helper")
    report = review_range(
        tmp_path,
        "HEAD~1..HEAD",
        index=ImpactIndex(tmp_path, tmp_path / "impact.sqlite"),
    )
    assert any(item["qualified_name"] == "pkg/lib.py::helper" for item in report["changed_symbols"])
    assert any(item["symbol"] == "pkg/lib.py::helper" for item in report["flags"])
    assert any(
        caller["qualified_name"] == "pkg/lib.py::other"
        for flag in report["flags"]
        for caller in flag["untouched_callers"]
    )


OUTER_WITH_INNER = """\
def outer():
    before = 1
    def inner():
        return 1
    return inner() + before

def caller():
    return outer()
"""

DIFF_OUTER_AND_INNER = """\
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -2 +2 @@
-    before = 1
+    before = 2
@@ -4 +4 @@
-        return 1
+        return 2
"""

DIFF_INNER_ONLY = """\
diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -4 +4 @@
-        return 1
+        return 2
"""


def test_editing_enclosing_body_and_nested_function_keeps_the_outer(tmp_path: Path) -> None:
    new = OUTER_WITH_INNER.replace("before = 1", "before = 2").replace("        return 1", "        return 2")
    write_repo(tmp_path, {"app.py": new})
    blobs = {
        ("base", "app.py"): OUTER_WITH_INNER.encode(),
        ("head", "app.py"): new.encode(),
    }
    report = review_range(
        tmp_path,
        "base..head",
        index=ImpactIndex(tmp_path, tmp_path / "impact.sqlite"),
        git=FakeGit(DIFF_OUTER_AND_INNER, blobs),
    )
    changed = {item["qualified_name"] for item in report["changed_symbols"]}
    assert "app.py::outer" in changed
    assert "app.py::outer.inner" in changed
    flag = next(item for item in report["flags"] if item["symbol"] == "app.py::outer")
    callers = {item["qualified_name"] for item in flag["untouched_callers"]}
    assert "app.py::caller" in callers


def test_editing_only_a_nested_function_does_not_mark_the_enclosing_one(tmp_path: Path) -> None:
    new = OUTER_WITH_INNER.replace("        return 1", "        return 2")
    write_repo(tmp_path, {"app.py": new})
    blobs = {
        ("base", "app.py"): OUTER_WITH_INNER.encode(),
        ("head", "app.py"): new.encode(),
    }
    report = review_range(
        tmp_path,
        "base..head",
        index=ImpactIndex(tmp_path, tmp_path / "impact.sqlite"),
        git=FakeGit(DIFF_INNER_ONLY, blobs),
    )
    changed = {item["qualified_name"] for item in report["changed_symbols"]}
    assert changed == {"app.py::outer.inner"}
    assert all(item["symbol"] != "app.py::outer" for item in report["flags"])


def test_local_class_hides_imported_class_on_attribute_call(tmp_path: Path) -> None:
    _, catalog = index_repo(
        tmp_path,
        {
            "a.py": "class Worker:\n    @staticmethod\n    def run():\n        return 1\n",
            "b.py": (
                "from a import Worker\n"
                "class Worker:\n"
                "    @staticmethod\n"
                "    def run():\n"
                "        return 2\n"
                "def caller():\n"
                "    return Worker.run()\n"
            ),
            "c.py": (
                "from a import Worker\n"
                "def caller():\n"
                "    return Worker.run()\n"
                "def hides():\n"
                "    from a import Worker\n"
                "    return Worker.run()\n"
            ),
            "d.py": (
                "class Worker:\n"
                "    @staticmethod\n"
                "    def run():\n"
                "        return 0\n"
                "def caller():\n"
                "    from a import Worker\n"
                "    return Worker.run()\n"
            ),
        },
    )
    imported = impact_report(catalog, tmp_path, ["a.py::Worker.run"])
    local = impact_report(catalog, tmp_path, ["b.py::Worker.run"])
    imported_names = _qualifiers(imported)
    assert "b.py::caller" not in imported_names
    assert "c.py::caller" in imported_names
    assert "c.py::hides" in imported_names
    assert "d.py::caller" in imported_names
    by_name = {item["qualified_name"]: item for item in local["affected_functions"]}
    assert by_name["b.py::caller"]["confidence"] == 0.88
    assert "c.py::caller" not in _qualifiers(local)


def test_dotted_import_binds_the_package_not_the_submodule(tmp_path: Path) -> None:
    facts = extract_source(
        "app.py",
        b"import a.b\nimport a.b as b\n",
        "python",
    )
    recorded = {(item.module, item.alias, item.kind) for item in facts.imports}
    assert ("a", "a", "module") in recorded
    assert ("a.b", "", "module") in recorded
    assert ("a.b", "b", "module") in recorded
    _, catalog = index_repo(
        tmp_path,
        {
            "a/__init__.py": "def foo():\n    return 1\n",
            "a/b.py": "def foo():\n    return 2\n",
            "app.py": (
                "import a.b\n"
                "def call_pkg():\n"
                "    return a.foo()\n"
                "def call_sub():\n"
                "    return a.b.foo()\n"
            ),
            "alias.py": (
                "import a.b as b\n"
                "def call_alias():\n"
                "    return b.foo()\n"
            ),
        },
    )
    package = impact_report(catalog, tmp_path, ["a/__init__.py::foo"])
    submodule = impact_report(catalog, tmp_path, ["a/b.py::foo"])
    package_names = _qualifiers(package)
    submodule_names = _qualifiers(submodule)
    assert "app.py::call_pkg" in package_names
    assert "app.py::call_sub" not in package_names
    assert "app.py::call_sub" in submodule_names
    assert "alias.py::call_alias" in submodule_names
    assert "app.py::call_pkg" not in submodule_names
    assert "alias.py::call_alias" not in package_names
    by_name = {item["qualified_name"]: item for item in package["affected_functions"]}
    assert by_name["app.py::call_pkg"]["confidence"] == 0.9


def test_real_git_three_dot_review_uses_merge_base(tmp_path: Path) -> None:
    lib = tmp_path / "pkg" / "lib.py"
    write_repo(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/lib.py": "def helper():\n    return 1\n\ndef other():\n    return helper()\n",
            "pkg/app.py": "from pkg.lib import helper\n\ndef main():\n    return helper()\n",
        },
    )
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "impact@example.com")
    _git(tmp_path, "config", "user.name", "Impact Test")
    _git(tmp_path, "add", "pkg")
    _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-m", "base")
    _git(tmp_path, "checkout", "-b", "feature")
    lib.write_text("def other():\n    return helper()\n")
    _git(tmp_path, "add", "pkg/lib.py")
    _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-m", "delete helper")
    _git(tmp_path, "checkout", "main")
    lib.write_text("def unrelated():\n    return 0\n\n" + lib.read_text())
    _git(tmp_path, "add", "pkg/lib.py")
    _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-m", "add unrelated")
    _git(tmp_path, "checkout", "feature")
    report = review_range(
        tmp_path,
        "main...feature",
        index=ImpactIndex(tmp_path, tmp_path / "impact.sqlite"),
    )
    changed = {item["qualified_name"]: item for item in report["changed_symbols"]}
    assert changed["pkg/lib.py::helper"]["removed"] is True
    assert "pkg/lib.py::unrelated" not in changed
    flag = next(item for item in report["flags"] if item["symbol"] == "pkg/lib.py::helper")
    callers = {item["qualified_name"] for item in flag["untouched_callers"]}
    assert "pkg/lib.py::other" in callers
    assert "pkg/app.py::main" in callers


LIB_HELPER_AND_WRAPPER = """\
def helper():
    return 1

def wrapper(x):
    return x
"""

LIB_WRAPPER_ONLY = """\
def wrapper(x):
    return x
"""

DIFF_DELETE_HELPER_KEEP_WRAPPER = """\
diff --git a/pkg/lib.py b/pkg/lib.py
--- a/pkg/lib.py
+++ b/pkg/lib.py
@@ -1,3 +0,0 @@
-def helper():
-    return 1
-
"""

DIFF_DELETE_LIB_MODULE = """\
diff --git a/pkg/lib.py b/pkg/lib.py
deleted file mode 100644
--- a/pkg/lib.py
+++ /dev/null
@@ -1,5 +0,0 @@
-def helper():
-    return 1
-
-def wrapper(x):
-    return x
"""


def test_removed_function_flags_untouched_module_attribute_callers(tmp_path: Path) -> None:
    alias = "import pkg.lib as lib\n\ndef main():\n    return lib.helper()\n"
    dotted = "import pkg.lib\n\ndef main():\n    return pkg.lib.helper()\n"
    write_repo(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/lib.py": LIB_WRAPPER_ONLY,
            "pkg/alias.py": alias,
            "pkg/dotted.py": dotted,
        },
    )
    blobs = {
        ("base", "pkg/lib.py"): LIB_HELPER_AND_WRAPPER.encode(),
        ("head", "pkg/lib.py"): LIB_WRAPPER_ONLY.encode(),
        ("base", "pkg/alias.py"): alias.encode(),
        ("head", "pkg/alias.py"): alias.encode(),
        ("base", "pkg/dotted.py"): dotted.encode(),
        ("head", "pkg/dotted.py"): dotted.encode(),
    }
    report = review_range(
        tmp_path,
        "base..head",
        index=ImpactIndex(tmp_path, tmp_path / "impact.sqlite"),
        git=FakeGit(DIFF_DELETE_HELPER_KEEP_WRAPPER, blobs),
    )
    flag = next(item for item in report["flags"] if item["symbol"] == "pkg/lib.py::helper")
    assert flag["removed"] is True
    callers = {item["qualified_name"] for item in flag["untouched_callers"]}
    assert callers == {"pkg/alias.py::main", "pkg/dotted.py::main"}
    assert flag["confidence"] == 0.7


def test_deleted_module_flags_surviving_importers(tmp_path: Path) -> None:
    app = "from pkg.lib import helper\n\ndef main():\n    return helper()\n"
    other = "from pkg.missing import helper\n\ndef side():\n    return helper()\n"
    write_repo(tmp_path, {"pkg/__init__.py": "", "pkg/app.py": app, "pkg/other.py": other})
    blobs = {
        ("base", "pkg/lib.py"): LIB_HELPER_AND_WRAPPER.encode(),
        ("base", "pkg/app.py"): app.encode(),
        ("head", "pkg/app.py"): app.encode(),
        ("base", "pkg/other.py"): other.encode(),
        ("head", "pkg/other.py"): other.encode(),
    }
    report = review_range(
        tmp_path,
        "base..head",
        index=ImpactIndex(tmp_path, tmp_path / "impact.sqlite"),
        git=FakeGit(DIFF_DELETE_LIB_MODULE, blobs),
    )
    changed = {item["qualified_name"]: item for item in report["changed_symbols"]}
    assert changed["pkg/lib.py::helper"]["removed"] is True
    assert changed["pkg/lib.py::wrapper"]["removed"] is True
    flag = next(item for item in report["flags"] if item["symbol"] == "pkg/lib.py::helper")
    callers = {item["qualified_name"] for item in flag["untouched_callers"]}
    assert callers == {"pkg/app.py::main"}


def test_resolved_call_on_the_same_line_does_not_hide_a_removed_call(tmp_path: Path) -> None:
    app = "from pkg.lib import wrapper, helper\n\ndef main():\n    return wrapper(helper())\n"
    write_repo(
        tmp_path,
        {"pkg/__init__.py": "", "pkg/lib.py": LIB_WRAPPER_ONLY, "pkg/app.py": app},
    )
    blobs = {
        ("base", "pkg/lib.py"): LIB_HELPER_AND_WRAPPER.encode(),
        ("head", "pkg/lib.py"): LIB_WRAPPER_ONLY.encode(),
        ("base", "pkg/app.py"): app.encode(),
        ("head", "pkg/app.py"): app.encode(),
    }
    report = review_range(
        tmp_path,
        "base..head",
        index=ImpactIndex(tmp_path, tmp_path / "impact.sqlite"),
        git=FakeGit(DIFF_DELETE_HELPER_KEEP_WRAPPER, blobs),
    )
    flag = next(item for item in report["flags"] if item["symbol"] == "pkg/lib.py::helper")
    callers = {item["qualified_name"] for item in flag["untouched_callers"]}
    assert callers == {"pkg/app.py::main"}
    assert all(item["symbol"] != "pkg/lib.py::wrapper" for item in report["flags"])


def test_git_c_quoted_paths_decode_octal_quotes_and_backslashes() -> None:
    files = parse_unified_diff(
        'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
        '--- "a/caf\\303\\251.py"\n'
        '+++ "b/caf\\303\\251.py"\n'
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )
    assert files[0].old_path == "café.py"
    assert files[0].new_path == "café.py"
    assert files[0].old_lines == {1}
    assert files[0].new_lines == {1}
    quoted = parse_unified_diff(
        'diff --git "a/say \\"hi\\".py" "b/say \\"hi\\".py"\n'
        '--- "a/say \\"hi\\".py"\n'
        '+++ "b/back\\\\slash.py"\n'
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )
    assert quoted[0].old_path == 'say "hi".py'
    assert quoted[0].new_path == "back\\slash.py"


def test_quoted_unicode_diff_matches_the_working_tree(tmp_path: Path) -> None:
    rel = "café.py"
    old = "def helper():\n    return 1\n\ndef other():\n    return helper()\n"
    new = "def helper():\n    return 2\n\ndef other():\n    return helper()\n"
    diff = (
        'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
        '--- "a/caf\\303\\251.py"\n'
        '+++ "b/caf\\303\\251.py"\n'
        "@@ -2 +2 @@ def helper():\n"
        "-    return 1\n"
        "+    return 2\n"
    )
    write_repo(tmp_path, {rel: new})
    blobs = {("base", rel): old.encode(), ("head", rel): new.encode()}
    report = review_range(
        tmp_path,
        "base..head",
        index=ImpactIndex(tmp_path, tmp_path / "impact.sqlite"),
        git=FakeGit(diff, blobs),
    )
    assert any(item["qualified_name"] == "café.py::helper" for item in report["changed_symbols"])
    flag = next(item for item in report["flags"] if item["symbol"] == "café.py::helper")
    callers = {item["qualified_name"] for item in flag["untouched_callers"]}
    assert callers == {"café.py::other"}


def test_real_git_review_decodes_quoted_unicode_paths(tmp_path: Path) -> None:
    rel = "café.py"
    write_repo(tmp_path, {rel: "def helper():\n    return 1\n\ndef other():\n    return helper()\n"})
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "impact@example.com")
    _git(tmp_path, "config", "user.name", "Impact Test")
    _git(tmp_path, "add", rel)
    _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-m", "base")
    (tmp_path / rel).write_text("def helper():\n    return 2\n\ndef other():\n    return helper()\n")
    _git(tmp_path, "add", rel)
    _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-m", "change helper")
    report = review_range(
        tmp_path,
        "HEAD~1..HEAD",
        index=ImpactIndex(tmp_path, tmp_path / "impact.sqlite"),
    )
    assert any(item["qualified_name"] == "café.py::helper" for item in report["changed_symbols"])
    flag = next(item for item in report["flags"] if item["symbol"] == "café.py::helper")
    callers = {item["qualified_name"] for item in flag["untouched_callers"]}
    assert callers == {"café.py::other"}


def test_parameter_shadows_imported_name(tmp_path: Path) -> None:
    _, catalog = index_repo(
        tmp_path,
        {
            "helpers.py": "def parse():\n    return 1\n",
            "other.py": "def parse():\n    return 2\n",
            "worker.py": "class Worker:\n    @staticmethod\n    def run():\n        return 1\n",
            "app.py": (
                "from helpers import parse\n"
                "def run(parse):\n"
                "    return parse()\n"
                "def nested(parse):\n"
                "    def inner():\n"
                "        return parse()\n"
                "    return inner()\n"
                "def keeps():\n"
                "    return parse()\n"
                "def rebound(parse):\n"
                "    from other import parse\n"
                "    return parse()\n"
                "def outer():\n"
                "    def defaulted(parse=parse()):\n"
                "        return parse()\n"
                "    return defaulted\n"
            ),
            "use.py": (
                "from worker import Worker\n"
                "def run(Worker):\n"
                "    return Worker.run()\n"
            ),
            "ts.ts": (
                'import { parse } from "./helpers";\n'
                "export function run(parse) { return parse(); }\n"
                "export const arrow = (parse) => parse();\n"
                "export const bare = parse => parse();\n"
                "export const expr = function (parse) { return parse(); }\n"
                "export const calls = (x = parse()) => x;\n"
            ),
        },
    )
    imported = impact_report(catalog, tmp_path, ["helpers.py::parse"])
    names = _qualifiers(imported)
    assert "app.py::keeps" in names
    assert "app.py::outer" in names
    assert "ts.ts::calls" in names
    for hidden in (
        "app.py::run",
        "app.py::nested",
        "app.py::nested.inner",
        "app.py::rebound",
        "app.py::outer.defaulted",
        "ts.ts::run",
        "ts.ts::arrow",
        "ts.ts::bare",
        "ts.ts::expr",
    ):
        assert hidden not in names
    by_name = {item["qualified_name"]: item for item in imported["affected_functions"]}
    assert by_name["app.py::keeps"]["confidence"] == 0.9
    rebound = impact_report(catalog, tmp_path, ["other.py::parse"])
    assert "app.py::rebound" in _qualifiers(rebound)
    assert "app.py::keeps" not in _qualifiers(rebound)
    worker = impact_report(catalog, tmp_path, ["worker.py::Worker.run"])
    assert "use.py::run" not in _qualifiers(worker)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


# --- binding regressions (rr4) -------------------------------------------


def _facts(path, source):
    from src.conductor_cli.impact.extract import extract_path

    return extract_path(path, source.encode())


def _catalog_of(files):
    from src.conductor_cli.impact.analyze import Catalog

    symbols, calls, imports = [], [], []
    for path, source in files.items():
        facts = _facts(path, source)
        symbols += facts.symbols
        calls += facts.calls
        imports += facts.imports
    return Catalog(list(files), symbols, calls, imports)


def test_destructured_ts_parameter_hides_import():
    catalog = _catalog_of(
        {
            "a.ts": "export function parse() {}\n",
            "b.ts": "import { parse } from './a';\nexport function f({ parse }) { return parse(); }\n"
            "export function g([parse]) { return parse(); }\n"
            "export function h({ x: parse = 1, ...rest }) { return parse(); }\n",
        }
    )
    assert catalog.graph().reverse.get("a.ts::parse", []) == []


def test_class_body_import_does_not_shadow_module_import_in_method():
    from src.conductor_cli.impact.analyze import direct_edges

    catalog = _catalog_of(
        {
            "first.py": "def helper():\n    pass\n",
            "alt.py": "def helper():\n    pass\n",
            "m.py": "from first import helper\n\nclass C:\n    from alt import helper\n\n"
            "    def run(self):\n        return helper()\n",
        }
    )
    assert [e.caller for e in direct_edges(catalog, "first.py::helper")] == ["m.py::C.run"]
    assert direct_edges(catalog, "alt.py::helper") == []


def _removal_flags(old, new, extra=None, diff=None, primary="lib.py"):
    from pathlib import Path

    from src.conductor_cli.impact.review import review_range

    facts = {primary: _facts(primary, new)}
    for path, source in (extra or {}).items():
        facts[path] = _facts(path, source)

    class Index:
        def update(self):
            return {}

        def load_facts(self):
            return (
                list(facts),
                [s for f in facts.values() for s in f.symbols],
                [c for f in facts.values() for c in f.calls],
                [i for f in facts.values() for i in f.imports],
            )

    class Git:
        def diff(self, spec):
            return diff or (
                f"diff --git a/{primary} b/{primary}\n"
                f"--- a/{primary}\n"
                f"+++ b/{primary}\n"
                "@@ -1,3 +0,0 @@\n"
                "-def helper():\n"
                "-    return 1\n"
                "-\n"
            )

        def show(self, rev, path):
            return (old if rev == "base" else new).encode() if path == primary else None

        def merge_base(self, base, head):
            return base

    result = review_range(Path.cwd(), "base..head", index=Index(), git=Git())
    return [(f["symbol"], [c["qualified_name"] for c in f["untouched_callers"]]) for f in result["flags"]]


_OLD = "def helper():\n    return 1\n\ndef main(helper):\n    return helper()\n"
_NEW = "def main(helper):\n    return helper()\n"


def test_deleted_function_ignores_same_file_parameter_shadow():
    assert _removal_flags(_OLD, _NEW) == []


def test_deleted_function_ignores_repository_global_name_fallback():
    flags = _removal_flags(
        "def helper():\n    return 1\n\ndef use():\n    return helper()\n",
        "def use():\n    return helper()\n",
        {"unrelated.py": "def helper():\n    return 2\n"},
    )
    assert flags == [("lib.py::helper", ["lib.py::use"])]


def test_deleted_method_respects_parameter_and_import_class_shadowing():
    old = (
        "class Foo:\n"
        "    def bar(self):\n"
        "        return 1\n"
        "\n"
        "from external import Foo\n"
        "\n"
        "def parameter(Foo):\n"
        "    return Foo.bar(None)\n"
        "\n"
        "def imported():\n"
        "    return Foo.bar(None)\n"
    )
    new = (
        "class Foo:\n"
        "    pass\n"
        "\n"
        "from external import Foo\n"
        "\n"
        "def parameter(Foo):\n"
        "    return Foo.bar(None)\n"
        "\n"
        "def imported():\n"
        "    return Foo.bar(None)\n"
    )
    diff = (
        "diff --git a/lib.py b/lib.py\n"
        "--- a/lib.py\n"
        "+++ b/lib.py\n"
        "@@ -2,2 +2 @@\n"
        "-    def bar(self):\n"
        "-        return 1\n"
        "+    pass\n"
    )
    assert _removal_flags(old, new, diff=diff) == []


def test_deleted_same_file_function_respects_import_rebinding():
    flags = _removal_flags(
        (
            "def helper():\n"
            "    return 1\n"
            "\n"
            "from math import sqrt as helper\n"
            "\n"
            "def use():\n"
            "    return helper(4)\n"
        ),
        "from math import sqrt as helper\n\ndef use():\n    return helper(4)\n",
    )
    assert flags == []


def test_deleted_imported_function_ignores_later_same_scope_function():
    flags = _removal_flags(
        "def helper():\n    return 1\n",
        "",
        {
            "user.py": (
                "from lib import helper\n"
                "\n"
                "def helper():\n"
                "    return 2\n"
                "\n"
                "def use():\n"
                "    return helper()\n"
            )
        },
    )
    assert flags == []


def test_deleted_method_resolves_class_after_unresolved_import():
    old = (
        "from ext import Foo\n"
        "\n"
        "class Foo:\n"
        "    def bar(self):\n"
        "        return 1\n"
        "\n"
        "def use():\n"
        "    return Foo.bar(None)\n"
    )
    new = (
        "from ext import Foo\n"
        "\n"
        "class Foo:\n"
        "    pass\n"
        "\n"
        "def use():\n"
        "    return Foo.bar(None)\n"
    )
    diff = (
        "diff --git a/lib.py b/lib.py\n"
        "--- a/lib.py\n"
        "+++ b/lib.py\n"
        "@@ -4,2 +4 @@\n"
        "-    def bar(self):\n"
        "-        return 1\n"
        "+    pass\n"
    )
    assert _removal_flags(old, new, diff=diff) == [
        ("lib.py::Foo.bar", ["lib.py::use"])
    ]


def test_deleted_method_resolves_class_after_same_scope_parameter():
    old = (
        "def use(Foo):\n"
        "    class Foo:\n"
        "        def bar(self):\n"
        "            return 1\n"
        "    return Foo.bar(None)\n"
    )
    new = (
        "def use(Foo):\n"
        "    class Foo:\n"
        "        pass\n"
        "    return Foo.bar(None)\n"
    )
    diff = (
        "diff --git a/lib.py b/lib.py\n"
        "--- a/lib.py\n"
        "+++ b/lib.py\n"
        "@@ -3,2 +3 @@\n"
        "-        def bar(self):\n"
        "-            return 1\n"
        "+        pass\n"
    )
    assert _removal_flags(old, new, diff=diff) == [
        ("lib.py::use.Foo.bar", ["lib.py::use"])
    ]


def test_deleted_same_file_function_respects_star_imported_name():
    old = (
        "def helper():\n"
        "    return 1\n"
        "\n"
        "from other import *\n"
        "\n"
        "def use():\n"
        "    return helper()\n"
    )
    new = "from other import *\n\ndef use():\n    return helper()\n"
    assert _removal_flags(old, new, {"other.py": "def helper():\n    return 2\n"}) == []
    assert _removal_flags(old, new, {"other.py": "def unrelated():\n    return 2\n"}) == [
        ("lib.py::helper", ["lib.py::use"])
    ]


def test_deleted_star_import_name_does_not_override_earlier_local_function():
    flags = _removal_flags(
        "def helper():\n    return 1\n",
        "",
        {
            "user.py": (
                "def helper():\n"
                "    return 2\n"
                "\n"
                "from lib import *\n"
                "\n"
                "def use():\n"
                "    return helper()\n"
            )
        },
    )
    assert flags == []


def test_deleted_star_import_name_does_not_override_earlier_explicit_import():
    flags = _removal_flags(
        "def helper():\n    return 1\n",
        "",
        {
            "other.py": "def helper():\n    return 2\n",
            "user.py": (
                "from other import helper\n"
                "from lib import *\n"
                "\n"
                "def use():\n"
                "    return helper()\n"
            ),
        },
    )
    assert flags == []


def test_deleted_private_name_is_not_bound_by_star_import():
    flags = _removal_flags(
        "def _helper():\n    return 1\n",
        "",
        {"user.py": "from lib import *\n\ndef use():\n    return _helper()\n"},
    )
    assert flags == []


def test_deleted_function_flags_aliased_importer():
    flags = _removal_flags(
        "def helper():\n    return 1\n",
        "",
        {"user.py": "from lib import helper as h\n\ndef go():\n    return h()\n"},
    )
    assert flags == [("lib.py::helper", ["user.py::go"])]


def test_deleted_function_importer_with_parameter_shadow_not_flagged():
    flags = _removal_flags(
        "def helper():\n    return 1\n",
        "",
        {"user.py": "from lib import helper\n\ndef go(helper):\n    return helper()\n"},
    )
    assert flags == []


def test_deleted_method_ignores_unrelated_bare_call_with_same_name():
    old = (
        "from thirdparty import bar\n"
        "\n"
        "class Foo:\n"
        "    def bar(self):\n"
        "        return 1\n"
        "\n"
        "def use():\n"
        "    return bar()\n"
    )
    new = (
        "from thirdparty import bar\n"
        "\n"
        "class Foo:\n"
        "    pass\n"
        "\n"
        "def use():\n"
        "    return bar()\n"
    )
    diff = (
        "diff --git a/lib.py b/lib.py\n"
        "--- a/lib.py\n"
        "+++ b/lib.py\n"
        "@@ -4,2 +4,1 @@\n"
        "-    def bar(self):\n"
        "-        return 1\n"
        "+    pass\n"
    )
    assert _removal_flags(old, new, diff=diff) == []


def test_deleted_nested_function_flags_only_enclosing_scope_callers():
    old = (
        "def outer():\n"
        "    def inner():\n"
        "        return 1\n"
        "    def sibling():\n"
        "        return inner()\n"
        "    return sibling()\n"
        "\n"
        "def use():\n"
        "    return inner()\n"
    )
    new = (
        "def outer():\n"
        "    def sibling():\n"
        "        return inner()\n"
        "    return sibling()\n"
        "\n"
        "def use():\n"
        "    return inner()\n"
    )
    diff = (
        "diff --git a/lib.py b/lib.py\n"
        "--- a/lib.py\n"
        "+++ b/lib.py\n"
        "@@ -2,2 +1,0 @@\n"
        "-    def inner():\n"
        "-        return 1\n"
    )
    assert _removal_flags(old, new, diff=diff) == [("lib.py::outer.inner", ["lib.py::outer.sibling"])]


def test_deleted_class_body_call_is_flagged():
    # A bare call in a class body resolves against the namespace the earlier
    # def filled; removing the def makes class creation raise NameError.
    old = (
        "class Box:\n"
        "    def helper():\n"
        "        return 1\n"
        "\n"
        "    value = helper()\n"
    )
    new = "class Box:\n\n    value = helper()\n"
    diff = (
        "diff --git a/lib.py b/lib.py\n"
        "--- a/lib.py\n"
        "+++ b/lib.py\n"
        "@@ -2,2 +1,0 @@\n"
        "-    def helper():\n"
        "-        return 1\n"
    )
    assert _removal_flags(old, new, diff=diff) == [("lib.py::Box.helper", ["lib.py::Box"])]


def test_deleted_nested_function_flags_unchanged_enclosing_caller():
    # Deleting inner() changes lines inside outer()'s span, but outer() itself
    # keeps its unchanged `return inner()`, so outer() is still a caller.
    old = (
        "def outer():\n"
        "    def inner():\n"
        "        return 1\n"
        "    return inner()\n"
        "\n"
        "def caller():\n"
        "    return outer()\n"
    )
    new = "def outer():\n    return inner()\n\ndef caller():\n    return outer()\n"
    diff = (
        "diff --git a/lib.py b/lib.py\n"
        "--- a/lib.py\n"
        "+++ b/lib.py\n"
        "@@ -2,2 +1,0 @@\n"
        "-    def inner():\n"
        "-        return 1\n"
    )
    assert _removal_flags(old, new, diff=diff) == [("lib.py::outer.inner", ["lib.py::outer"])]


def test_deleted_class_qualified_method_is_flagged():
    old = (
        "class Foo:\n"
        "    def bar(self):\n"
        "        return 1\n"
        "\n"
        "def use():\n"
        "    return Foo.bar()\n"
    )
    new = "class Foo:\n    pass\n\ndef use():\n    return Foo.bar()\n"
    diff = (
        "diff --git a/lib.py b/lib.py\n"
        "--- a/lib.py\n"
        "+++ b/lib.py\n"
        "@@ -2,2 +1 @@\n"
        "-    def bar(self):\n"
        "-        return 1\n"
        "+    pass\n"
    )
    assert _removal_flags(old, new, diff=diff) == [("lib.py::Foo.bar", ["lib.py::use"])]


def test_deleted_function_ignores_import_bound_in_another_function():
    # `from lib import helper` inside local() binds nothing for other(), whose
    # helper() comes from the module-level thirdparty import.
    flags = _removal_flags(
        "def helper():\n    return 1\n",
        "",
        {
            "user.py": (
                "from thirdparty import helper\n"
                "\n"
                "def local():\n"
                "    from lib import helper\n"
                "    return helper()\n"
                "\n"
                "def other():\n"
                "    return helper()\n"
            )
        },
    )
    assert flags == [("lib.py::helper", ["user.py::local"])]


def test_import_of_same_name_does_not_name_a_method():
    catalog = _catalog_of(
        {
            "lib.py": "class Foo:\n    def bar(self):\n        return 1\n",
            "user.py": "from lib import bar\n\ndef go():\n    return bar()\n",
        }
    )
    report = impact_report(catalog, Path.cwd(), ["lib.py::Foo.bar"])
    assert "user.py" not in {item["path"] for item in report["affected_files"]}


def test_git_diff_disables_textconv(tmp_path):
    from src.conductor_cli.impact.review import SubprocessGit

    git = SubprocessGit(tmp_path)
    seen = []

    class Proc:
        returncode = 0
        stdout = b""
        stderr = b""

    git._run = lambda args: (seen.append(args), Proc())[1]
    git.diff("a..b")
    git.diff("a...b")
    assert all("--no-textconv" in args for args in seen) and len(seen) == 2


def test_parameter_shadows_dotted_module_receiver() -> None:
    catalog = _catalog_of(
        {
            "pkg/lib.py": "def helper():\n    pass\n",
            "app.py": "import pkg.lib\ndef run(pkg):\n    return pkg.lib.helper()\n",
            "keep.py": "import pkg.lib\ndef run():\n    return pkg.lib.helper()\n",
            "rebind.py": "import pkg.lib\ndef run(pkg):\n    import pkg.lib\n    return pkg.lib.helper()\n",
            "nested.py": "import pkg.lib\ndef run(pkg):\n    def inner():\n        return pkg.lib.helper()\n    return inner\n",
        }
    )
    callers = {edge.caller for edge in catalog.graph().reverse.get("pkg/lib.py::helper", [])}
    assert "app.py::run" not in callers
    assert "nested.py::run.inner" not in callers
    assert callers == {"keep.py::run", "rebind.py::run"}
    rebound = next(edge for edge in catalog.graph().reverse["pkg/lib.py::helper"] if edge.caller == "rebind.py::run")
    assert rebound.confidence == 0.9


def test_deleted_dotted_receiver_respects_parameter_shadow() -> None:
    from pathlib import Path

    from src.conductor_cli.impact.review import review_range

    old = "def helper():\n    return 1\n"
    new = ""
    shadowed = "import pkg.lib\ndef run(pkg):\n    return pkg.lib.helper()\n"
    plain = "import pkg.lib\ndef main():\n    return pkg.lib.helper()\n"
    files = {
        "pkg/lib.py": new,
        "pkg/__init__.py": "",
        "app.py": shadowed,
        "keep.py": plain,
    }
    catalog = _catalog_of(files)

    class Index:
        def update(self):
            return {}

        def load_facts(self):
            return (
                catalog.files,
                catalog.symbols,
                catalog.calls,
                catalog.imports,
            )

    class Git:
        def diff(self, spec):
            return (
                "diff --git a/pkg/lib.py b/pkg/lib.py\n"
                "--- a/pkg/lib.py\n"
                "+++ b/pkg/lib.py\n"
                "@@ -1,2 +0,0 @@\n"
                "-def helper():\n"
                "-    return 1\n"
            )

        def show(self, rev, path):
            if path != "pkg/lib.py":
                return None
            return (old if rev == "base" else new).encode()

        def merge_base(self, base, head):
            return base

    result = review_range(Path("."), "base..head", index=Index(), git=Git())
    flag = next(item for item in result["flags"] if item["symbol"] == "pkg/lib.py::helper")
    assert [item["qualified_name"] for item in flag["untouched_callers"]] == ["keep.py::main"]


def test_class_body_call_sees_class_body_import() -> None:
    from src.conductor_cli.impact.analyze import direct_edges

    catalog = _catalog_of(
        {
            "first.py": "def helper():\n    pass\n",
            "alt.py": "def helper():\n    pass\n",
            "app.py": "from first import helper\nclass C:\n    from alt import helper\n    value = helper()\n",
            "only.py": "class C:\n    from alt import helper\n    value = helper()\n",
            "later.py": (
                "from first import helper\n"
                "class C:\n"
                "    from alt import helper\n"
                "    f = lambda: helper()\n"
                "    xs = [helper() for _ in range(1)]\n"
                "    def run(self, x=helper()):\n"
                "        return helper()\n"
            ),
        }
    )
    alt = {edge.caller for edge in direct_edges(catalog, "alt.py::helper")}
    assert alt == {"app.py::C", "only.py::C", "later.py::C"}
    first = {(edge.caller, edge.path) for edge in direct_edges(catalog, "first.py::helper")}
    assert ("later.py::C.run", "later.py") in first
    assert ("", "later.py") in first
    assert ("app.py::C", "app.py") not in first
    assert all(not caller.endswith("#body") for caller, _path in first)
    module_hits = [edge for edge in direct_edges(catalog, "first.py::helper") if edge.caller == ""]
    assert len(module_hits) == 2


def test_deleted_typescript_alias_flags_untouched_caller() -> None:
    from pathlib import Path

    from src.conductor_cli.impact.review import review_range

    old = "export function helper() { return 1; }\n"
    new = ""
    app = "import { helper as h } from './lib';\nexport function run() { return h(); }\n"
    catalog = _catalog_of({"lib.ts": new, "app.ts": app})
    assert catalog.graph().reverse.get("lib.ts::helper", []) == []

    class Index:
        def update(self):
            return {}

        def load_facts(self):
            return catalog.files, catalog.symbols, catalog.calls, catalog.imports

    class Git:
        def diff(self, spec):
            return (
                "diff --git a/lib.ts b/lib.ts\n"
                "--- a/lib.ts\n"
                "+++ b/lib.ts\n"
                "@@ -1 +0,0 @@\n"
                "-export function helper() { return 1; }\n"
            )

        def show(self, rev, path):
            if path != "lib.ts":
                return None
            return (old if rev == "base" else new).encode()

        def merge_base(self, base, head):
            return base

    result = review_range(Path("."), "base..head", index=Index(), git=Git())
    assert len(result["changed_symbols"]) == 1
    assert result["changed_symbols"][0]["removed"] is True
    flag = result["flags"][0]
    assert flag["symbol"] == "lib.ts::helper"
    assert [item["qualified_name"] for item in flag["untouched_callers"]] == ["app.ts::run"]


def test_hunk_lines_that_look_like_headers_stay_in_the_hunk() -> None:
    files = parse_unified_diff(
        "diff --git a/lib.ts b/lib.ts\n"
        "--- a/lib.ts\n"
        "+++ b/lib.ts\n"
        "@@ -2 +2 @@\n"
        "--- counter;\n"
        "+++ counter;\n"
    )
    assert len(files) == 1
    assert files[0].old_path == "lib.ts"
    assert files[0].new_path == "lib.ts"
    assert files[0].old_lines == {2}
    assert files[0].new_lines == {2}


def test_default_index_rejects_symlink_that_leaves_the_repository(tmp_path: Path) -> None:
    import sqlite3

    outside = tmp_path / "outside.sqlite"
    conn = sqlite3.connect(outside)
    conn.execute("CREATE TABLE files (original TEXT)")
    conn.execute("INSERT INTO files VALUES ('keep')")
    conn.commit()
    conn.close()

    repo = tmp_path / "repo"
    impact = repo / ".conductor" / "impact"
    impact.mkdir(parents=True)
    (impact / "index.sqlite").symlink_to(outside)
    with pytest.raises(ImpactError, match="symlink"):
        ImpactIndex(repo).update()
    conn = sqlite3.connect(outside)
    row = conn.execute("SELECT original FROM files").fetchone()
    conn.close()
    assert row == ("keep",)

    repo2 = tmp_path / "repo2"
    linked = tmp_path / "linked"
    linked.mkdir()
    repo2.mkdir()
    (repo2 / ".conductor").symlink_to(linked)
    with pytest.raises(ImpactError, match="symlink"):
        ImpactIndex(repo2).update()
    assert not (linked / "impact").exists()


def test_default_index_refuses_an_internal_symlink(tmp_path: Path) -> None:
    from src.conductor_cli.impact.index import ImpactIndex
    from src.conductor_cli.impact.model import ImpactError

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    # The target is a sibling database INSIDE the repository.
    internal = repo / "other.sqlite"
    internal.write_text("", encoding="utf-8")
    link_dir = repo / ".conductor" / "impact"
    link_dir.mkdir(parents=True)
    (link_dir / "index.sqlite").symlink_to(internal)

    index = ImpactIndex(repo)
    with pytest.raises(ImpactError, match="symlink"):
        index.update()

    real = repo / "explicit.sqlite"
    ImpactIndex(repo, real).update()
    assert real.is_file()

def test_removed_default_export_flags_default_import_caller():
    diff = (
        "diff --git a/lib.ts b/lib.ts\n"
        "--- a/lib.ts\n"
        "+++ b/lib.ts\n"
        "@@ -1,3 +0,0 @@\n"
        "-export default function helper() {\n"
        "-  return 1;\n"
        "-}\n"
    )
    flags = _removal_flags(
        "export default function helper() {\n  return 1;\n}\n",
        "export const other = 2;\n",
        {"app.ts": "import h from './lib';\nh();\n"},
        diff=diff,
        primary="lib.ts",
    )
    assert [entry[0] for entry in flags] == ["lib.ts::helper"]


def test_module_call_before_a_later_import_keeps_the_earlier_binding():
    # A module-level statement executes in order: the later import cannot claim
    # the earlier call. (A call inside a function may run after every module
    # statement, so its textual position is not binding order.)
    flags = _removal_flags(
        "def helper():\n    return 1\n",
        "",
        {
            "user.py": (
                "from lib import helper\n"
                "helper()\n"
                "from other import helper\n"
            ),
            "other.py": "def helper():\n    return 2\n",
        },
    )
    assert flags == [("lib.py::helper", [""])]


def test_render_result_strips_terminal_controls(capsys: pytest.CaptureFixture[str]):
    from src.conductor_cli.impact.cli import render_result

    result = {
        "version": 1,
        "mode": "impact",
        "confidence": 0.9,
        "targets": [
            {
                "input": "\x1b]52;c;ZXZpbA==\x07pkg/lib.py\revil",
                "path": "pkg/lib.py",
                "line": 1,
            }
        ],
        "affected_functions": [],
        "affected_files": [],
        "affected_tests": [],
        "ambiguous": [],
    }
    render_result(result, json_mode=False)
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "\x07" not in out
    assert "\r" not in out


def test_live_resolution_honors_module_statement_order(tmp_path: Path) -> None:
    from src.conductor_cli.impact.analyze import Catalog
    from src.conductor_cli.impact.extract import extract_path

    def catalog_of(files: dict[str, str]) -> Catalog:
        symbols, calls, imports = [], [], []
        for path, source in files.items():
            facts = extract_path(path, source.encode())
            symbols += facts.symbols
            calls += facts.calls
            imports += facts.imports
        return Catalog(files=list(files), symbols=symbols, calls=calls, imports=imports)

    # The call runs before the later local definition, so it binds the import.
    before_definition = catalog_of(
        {
            "lib.py": "def helper():\n    return 1\n",
            "app.py": "from lib import helper\nhelper()\ndef helper():\n    return 2\n",
        }
    )
    report = impact_report(before_definition, tmp_path, ["lib.py::helper"])
    assert "app.py" in {item["path"] for item in report["affected_files"]}

    # A later import overrides an earlier definition for the call below it.
    after_definition = catalog_of(
        {
            "lib.py": "def helper():\n    return 1\n",
            "app.py": "def helper():\n    return 2\nfrom lib import helper\nhelper()\n",
        }
    )
    report = impact_report(after_definition, tmp_path, ["lib.py::helper"])
    assert "app.py" in {item["path"] for item in report["affected_files"]}


def test_live_function_body_binding_prefers_a_later_module_import(tmp_path: Path) -> None:
    from src.conductor_cli.impact.analyze import Catalog
    from src.conductor_cli.impact.extract import extract_path

    def catalog_of(files: dict[str, str]) -> Catalog:
        symbols, calls, imports = [], [], []
        for path, source in files.items():
            facts = extract_path(path, source.encode())
            symbols += facts.symbols
            calls += facts.calls
            imports += facts.imports
        return Catalog(files=list(files), symbols=symbols, calls=calls, imports=imports)

    # `run` executes after the module import, so its helper() is the import.
    catalog = catalog_of(
        {
            "lib.py": "def helper():\n    return 1\n",
            "app.py": (
                "def helper():\n"
                "    return 2\n"
                "def run():\n"
                "    return helper()\n"
                "from lib import helper\n"
            ),
        }
    )
    report = impact_report(catalog, tmp_path, ["lib.py::helper"])
    assert "app.py" in {item["path"] for item in report["affected_files"]}


def test_live_receiver_binding_prefers_a_later_module_import(tmp_path: Path) -> None:
    from src.conductor_cli.impact.analyze import Catalog
    from src.conductor_cli.impact.extract import extract_path

    files = {
        "lib.py": "def helper():\n    return 1\n",
        "app.py": (
            "class lib:\n"
            "    pass\n"
            "import lib\n"
            "def run():\n"
            "    return lib.helper()\n"
        ),
    }
    symbols, calls, imports = [], [], []
    for path, source in files.items():
        facts = extract_path(path, source.encode())
        symbols += facts.symbols
        calls += facts.calls
        imports += facts.imports
    catalog = Catalog(files=list(files), symbols=symbols, calls=calls, imports=imports)
    report = impact_report(catalog, tmp_path, ["lib.py::helper"])
    assert "app.py" in {item["path"] for item in report["affected_files"]}


def test_removed_anonymous_default_export_flags_default_import_caller():
    diff = (
        "diff --git a/lib.ts b/lib.ts\n"
        "--- a/lib.ts\n"
        "+++ b/lib.ts\n"
        "@@ -1,3 +0,0 @@\n"
        "-export default function () {\n"
        "-  return 1;\n"
        "-}\n"
    )
    flags = _removal_flags(
        "export default function () {\n  return 1;\n}\n",
        "export const other = 2;\n",
        {"app.ts": "import h from './lib';\nh();\n"},
        diff=diff,
        primary="lib.ts",
    )
    assert [entry[0] for entry in flags] == ["lib.ts::default"]


def test_same_line_import_order_decides_the_deleted_call_binding():
    flags = _removal_flags(
        "def helper():\n    return 1\n",
        "",
        {
            "user.py": "from lib import helper; helper(); from other import helper\n",
            "other.py": "def helper():\n    return 2\n",
        },
    )
    assert flags == [("lib.py::helper", [""])]


def test_same_line_module_receiver_alias_keeps_the_earlier_binding():
    flags = _removal_flags(
        "def helper():\n    return 1\n",
        "",
        {
            "app.py": "import lib as alias; alias.helper(); import other as alias\n",
            "other.py": "def helper():\n    return 2\n",
        },
    )
    assert flags == [("lib.py::helper", [""])]


def test_class_body_call_sees_only_imports_above_it(tmp_path: Path) -> None:
    from src.conductor_cli.impact.analyze import Catalog
    from src.conductor_cli.impact.extract import extract_path

    files = {
        "a.py": "def helper():\n    return 1\n",
        "b.py": "def helper():\n    return 2\n",
        "app.py": (
            "from a import helper\n"
            "class Box:\n"
            "    value = helper()\n"
            "from b import helper\n"
        ),
    }
    symbols, calls, imports = [], [], []
    for path, source in files.items():
        facts = extract_path(path, source.encode())
        symbols += facts.symbols
        calls += facts.calls
        imports += facts.imports
    catalog = Catalog(files=list(files), symbols=symbols, calls=calls, imports=imports)

    report = impact_report(catalog, tmp_path, ["a.py::helper"])
    assert "app.py::Box" in {
        item["qualified_name"] for item in report["affected_functions"]
    }


def test_anonymous_default_export_owns_its_body_calls(tmp_path: Path) -> None:
    from src.conductor_cli.impact.analyze import Catalog
    from src.conductor_cli.impact.extract import extract_path

    source = (
        "export function helper() { return 1; }\n"
        "export default () => main();\n"
        "function main() { return helper(); }\n"
    )
    facts = extract_path("lib.ts", source.encode())
    catalog = Catalog(
        files=["lib.ts"],
        symbols=list(facts.symbols),
        calls=list(facts.calls),
        imports=list(facts.imports),
    )

    report = impact_report(catalog, tmp_path, ["lib.ts::helper"])
    names = {item["qualified_name"] for item in report["affected_functions"]}
    assert "lib.ts::default" in names
    assert "lib.ts::main" in names


def test_same_line_aliased_imports_keep_their_statement_positions():
    flags = _removal_flags(
        "def helper():\n    return 1\n",
        "",
        {
            "app.py": "from lib import helper as run; run(); from other import run; run()\n",
            "other.py": "def helper():\n    return 2\n",
        },
    )
    assert flags == [("lib.py::helper", [""])]


def test_anonymous_default_signature_owns_defaults_and_parameters(tmp_path: Path) -> None:
    from src.conductor_cli.impact.analyze import Catalog
    from src.conductor_cli.impact.extract import extract_path

    def catalog_of(source: str) -> Catalog:
        facts = extract_path("lib.ts", source.encode())
        return Catalog(
            files=["lib.ts"],
            symbols=list(facts.symbols),
            calls=list(facts.calls),
            imports=list(facts.imports),
        )

    # A default expression call belongs to the synthetic default symbol.
    with_default = catalog_of(
        "export function helper() { return 1; }\n"
        "export default (x = helper()) => x;\n"
    )
    names = {
        item["qualified_name"]
        for item in impact_report(with_default, tmp_path, ["lib.ts::helper"])[
            "affected_functions"
        ]
    }
    assert "lib.ts::default" in names

    # A parameter of the synthetic function shadows the module name.
    shadowed = catalog_of(
        "export function helper() { return 1; }\n"
        "export default (helper) => helper();\n"
    )
    names = {
        item["qualified_name"]
        for item in impact_report(shadowed, tmp_path, ["lib.ts::helper"])[
            "affected_functions"
        ]
    }
    assert names == {"lib.ts::helper"}


def test_anonymous_default_generator_is_a_symbol_with_its_own_scope(tmp_path: Path) -> None:
    from src.conductor_cli.impact.analyze import Catalog
    from src.conductor_cli.impact.extract import extract_path

    def catalog_of(source: str) -> Catalog:
        facts = extract_path("lib.ts", source.encode())
        return Catalog(
            files=["lib.ts"],
            symbols=list(facts.symbols),
            calls=list(facts.calls),
            imports=list(facts.imports),
        )

    anonymous = catalog_of(
        "export function helper() { return 1; }\n"
        "export default function* () { yield helper(); }\n"
    )
    assert any(symbol.name == "default" for symbol in anonymous.symbols)
    affected = {
        item["qualified_name"]
        for item in impact_report(anonymous, tmp_path, ["lib.ts::helper"])[
            "affected_functions"
        ]
    }
    assert "lib.ts::default" in affected

    shadowed = catalog_of(
        "export function helper() { return 1; }\n"
        "export default function* (helper) { yield helper(); }\n"
    )
    names = {
        item["qualified_name"]
        for item in impact_report(shadowed, tmp_path, ["lib.ts::helper"])[
            "affected_functions"
        ]
    }
    assert names == {"lib.ts::helper"}


def test_removed_anonymous_default_generator_flags_default_import_caller():
    diff = (
        "diff --git a/lib.ts b/lib.ts\n"
        "--- a/lib.ts\n"
        "+++ b/lib.ts\n"
        "@@ -1,3 +0,0 @@\n"
        "-export default function* () {\n"
        "-  yield 1;\n"
        "-}\n"
    )
    flags = _removal_flags(
        "export default function* () {\n  yield 1;\n}\n",
        "export const other = 2;\n",
        {"app.ts": "import h from './lib';\nh();\n"},
        diff=diff,
        primary="lib.ts",
    )
    assert [entry[0] for entry in flags] == ["lib.ts::default"]


def test_named_default_generator_defaults_run_at_call_time(tmp_path: Path) -> None:
    from src.conductor_cli.impact.analyze import Catalog
    from src.conductor_cli.impact.extract import extract_path

    for source in (
        "export function helper() { return 1; }\n"
        "export default function* run(x = helper()) { yield x; }\n",
        "export function helper() { return 1; }\n"
        "export default async function* run(x = helper()) { yield x; }\n",
    ):
        facts = extract_path("lib.ts", source.encode())
        catalog = Catalog(
            files=["lib.ts"],
            symbols=list(facts.symbols),
            calls=list(facts.calls),
            imports=list(facts.imports),
        )
        names = {
            item["qualified_name"]
            for item in impact_report(catalog, tmp_path, ["lib.ts::helper"])[
                "affected_functions"
            ]
        }
        # The default expression is a call from the generator itself.
        assert "lib.ts::run" in names, source


def test_older_schema_index_rebuilds_instead_of_reusing_stale_facts(
    tmp_path: Path,
) -> None:
    import sqlite3

    from src.conductor_cli.impact.index import ImpactIndex

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    source = (
        "export function helper() { return 1; }\n"
        "export default function* run(x = helper()) { yield x; }\n"
    )
    (repo / "lib.ts").write_text(source, encoding="utf-8")
    db_path = repo / "impact.sqlite"
    index = ImpactIndex(repo, db_path)
    index.update()

    # Pretend the file was cached by the previous schema: facts for unchanged
    # files must not survive a version bump.
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA user_version = 6")

    stats = index.update()
    _files, symbols, calls, _imports = index.load_facts()
    assert stats["parsed"] >= 1
    assert any(symbol.name == "run" for symbol in symbols)
    assert any(call.caller == "lib.ts::run" for call in calls)
