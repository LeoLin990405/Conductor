import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BANNED_TERMS = re.compile(r"finance|stock|trading|portfolio|ticker", re.IGNORECASE)
GENERATED_BYTECODE_SUFFIXES = {".pyc", ".pyo"}


def _is_generated_bytecode(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix in GENERATED_BYTECODE_SUFFIXES


def test_generated_bytecode_is_excluded():
    assert _is_generated_bytecode(Path("scripts/__pycache__/profile.cpython-314.pyc"))
    assert _is_generated_bytecode(Path("scripts/profile.pyo"))
    assert not _is_generated_bytecode(Path("scripts/profile.py"))
    assert not _is_generated_bytecode(Path("SKILL.md"))


def test_plugin_skills_have_no_finance_examples():
    violations = []
    for skills_root in sorted((REPO_ROOT / "plugins").glob("*/skills")):
        for path in sorted(skills_root.rglob("*")):
            if not path.is_file() or path.name.upper().startswith(("LICENSE", "NOTICE")):
                continue
            if _is_generated_bytecode(path):
                continue
            text = path.read_bytes().decode("utf-8", errors="ignore")
            # A word split across a string concatenation ("tick" + "er")
            # is the same word; join adjacent literals before matching.
            text = re.sub(r"([\"'])\s*\+\s*\1", "", text)
            for line_number, line in enumerate(text.splitlines(), start=1):
                if BANNED_TERMS.search(line):
                    violations.append(f"{path.relative_to(REPO_ROOT)}:{line_number}: {line.strip()}")

    assert not violations, "Domain-specific examples remain in plugin skills:\n" + "\n".join(violations)
