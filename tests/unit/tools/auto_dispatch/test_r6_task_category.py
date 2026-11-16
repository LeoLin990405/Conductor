"""R6 L4: task categories are file areas from the write scope."""

from __future__ import annotations

from src.tools.auto_dispatch.task_category import GENERAL, category_for_paths, line_category, path_bucket
from src.tools.auto_dispatch.types import Line


def test_path_buckets() -> None:
    assert path_bucket("src/tools/auto_dispatch/machine.py") == "src/tools/auto_dispatch"
    assert path_bucket("src/tools/auto_dispatch/**") == "src/tools/auto_dispatch"
    assert path_bucket("src/tools/x.py") == "src/tools"
    assert path_bucket("src/llms/native_cli/isolation.py") == "src/llms/native_cli"
    assert path_bucket("web/src/App.tsx") == "web"
    assert path_bucket("./docs/a.md") == "docs"
    assert path_bucket("README.md") == "root"
    assert path_bucket("**") == GENERAL


def test_most_common_bucket_wins_and_ties_are_stable() -> None:
    assert category_for_paths(["web/a.ts", "web/b.ts", "docs/x.md"]) == "web"
    assert category_for_paths(["web/a.ts", "docs/x.md"]) == "docs"
    assert category_for_paths([]) == GENERAL


def test_line_category_reads_write_scope() -> None:
    line = Line(id="l", name="l", task="t", write_scope=("src/tools/auto_dispatch/gates.py",))
    assert line_category(line) == "src/tools/auto_dispatch"
    assert line_category(Line(id="l", name="l", task="t")) == GENERAL
