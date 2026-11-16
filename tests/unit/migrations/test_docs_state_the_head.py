"""The documents that name the schema head name the real one.

README.md said "head 127" long after the chain reached 139 because nothing tied
the sentence to the files. This does: when a revision is added, these fail until
the sentences are updated (MIGRATIONS.md lists the other pins).
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_ROOT = Path(__file__).resolve().parents[3]


def _head() -> tuple[str, str]:
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "migrations"))
    (head,) = ScriptDirectory.from_config(config).get_heads()
    (file,) = (_ROOT / "migrations" / "versions").glob(f"{head}_*.py")
    return head, file.stem


def test_readme_names_the_head_revision_file():
    head, stem = _head()
    assert stem in (_ROOT / "README.md").read_text(encoding="utf-8"), (
        f"README.md must name the schema head {stem}"
    )


def test_readme_zh_names_the_head_revision_file():
    head, stem = _head()
    assert stem in (_ROOT / "README.zh-CN.md").read_text(encoding="utf-8"), (
        f"README.zh-CN.md must name the schema head {stem}"
    )


def test_migrations_md_names_the_head_and_its_file():
    head, stem = _head()
    text = (_ROOT / "MIGRATIONS.md").read_text(encoding="utf-8")
    assert f"**{head}**" in text, f"MIGRATIONS.md must state the head **{head}**"
    assert f"{stem}.py" in text, f"MIGRATIONS.md must name {stem}.py"


def test_no_document_states_a_head_other_than_the_current_one():
    """The sentences that say "the head is N" are found and each must say the real N.

    A stale sentence cannot hide next to a current file name: every statement of
    this shape is compared, in every document listed.
    """
    import re

    head, _ = _head()
    shapes = {
        "README.md": [
            r"当前 head 是 revision `(\d+)`",
            r"alembic\s+single head (\d+)",
            r"single head `?(\d+)`?",
            # The English README (Development section) names the head and its file.
            r"single head, `(\d+)_[a-z0-9_]+`",
            r"(?i)\bthe head is `?(\d+)\b",
        ],
        "README.zh-CN.md": [
            r"只有一个 head：`(\d+)_[a-z0-9_]+`",
            r"当前 head 是 revision `(\d+)`",
        ],
        "MIGRATIONS.md": [
            r"the head is \*\*(\d+)\*\*",
            r"head \*\*(\d+)\*\*",
            r"schema head \(repo\): \*\*(\d+)\*\*",
        ],
    }
    found = 0
    for name, patterns in shapes.items():
        text = (_ROOT / name).read_text(encoding="utf-8")
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                found += 1
                assert match.group(1) == head, (
                    f"{name} says head {match.group(1)}, the chain ends at {head}: "
                    f"{match.group(0)!r}"
                )
    assert found >= 3, "the sentences this test checks were not found; update its shapes"
