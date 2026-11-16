"""MIGRATIONS.md's row for revision 147 names every column 147 adds.

The row once listed five of the twelve columns; the binding columns that a SQL
fence compares were missing. The columns are read from the migration's own
``ADD COLUMN`` clauses, so a column added later must be documented too.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_MIGRATION = _ROOT / "migrations" / "versions" / "147_ticket_worktree.py"
_DOC = _ROOT / "MIGRATIONS.md"


def _added_columns() -> list[str]:
    source = _MIGRATION.read_text(encoding="utf-8")
    upgrade = source.split("def upgrade", 1)[1].split("def downgrade", 1)[0]
    return re.findall(r"ADD COLUMN IF NOT EXISTS (\w+)", upgrade)


def _row_147() -> str:
    rows = [
        line
        for line in _DOC.read_text(encoding="utf-8").splitlines()
        if line.startswith("| 147 |")
    ]
    assert len(rows) == 1, rows
    return rows[0]


def test_the_migration_adds_thirteen_columns():
    assert len(_added_columns()) == 13


def test_the_147_row_names_every_added_column():
    row = _row_147()
    named = set(re.findall(r"`(?:autonomy_tickets\.)?(\w+)`", row))
    missing = [column for column in _added_columns() if column not in named]
    assert not missing, f"MIGRATIONS.md row 147 does not name {missing}"
