"""The agent instructions and operator guides name only tests, and test data, that exist.

The docs said a test enforced the agent-docstring lock for as long as the lock was a
61-entry file nothing read: the test, and every tool the lock pinned, had left, and an
edit failed nothing. A sentence that says "a test fails when" is only worth what the
test is, so this ties the sentences of these documents to the files.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# The documents an agent or an operator follows to change or run the repository. Each
# is read whole; a path that one of them states to be absent is written without
# backticks, as prose, so this list needs no exceptions.
DOCUMENTS = (
    "AGENTS.md",
    "CONTRIBUTING.md",
    "docs/operations.md",
    "mcp_servers/AGENT_CONTRACT.md",
    "src/ptc_agent/agent/prompts/templates/AGENTS.md",
    "src/server/AGENTS.md",
    "src/tools/AGENTS.md",
)

# A path under tests/ (any file), or a test module by its bare name, optionally with a
# `::node` suffix.
_BACKTICKED = re.compile(
    r"`(tests/(?:[\w.-]+/)*[\w.-]+\.\w+|(?:[\w.-]+/)*test_\w+\.py)(?:::[\w:\[\]-]+)?`"
)


def _test_modules() -> set[str]:
    return {
        str(path.relative_to(ROOT))
        for base in ("tests", "libs")
        for path in (ROOT / base).rglob("test_*.py")
    }


@pytest.mark.parametrize("document", DOCUMENTS)
def test_every_test_the_document_names_exists(document):
    modules = _test_modules()
    module_names = {Path(path).name for path in modules}
    missing = []
    for number, line in enumerate((ROOT / document).read_text(encoding="utf-8").splitlines(), 1):
        for match in _BACKTICKED.finditer(line):
            named = match.group(1)
            if "/" not in named:
                found = named in module_names
            else:
                found = (ROOT / named).is_file()
            if not found:
                missing.append(f"{document}:{number}: {named}")

    assert not missing, "names a test file that is not in the tree:\n" + "\n".join(missing)
