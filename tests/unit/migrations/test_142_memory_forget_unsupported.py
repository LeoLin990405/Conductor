"""142 lets a forget notice say that a provider does not support the operation.

The unit tests read the statements off a mock ``op``, so no SQL runs here and
no database is touched. What is pinned is the shape of the revision: it
follows 141, it is the only head, the chain from it down to the base is
continuous, and its downgrade drops the reason column -- and the CHECK on it
-- before it rewrites the rows the previous build cannot read and before it
restores the CHECK that would refuse them. The vocabulary the reason CHECK
admits is the forget one -- the three codes a remote can answer a forget
with -- and not the contract's ``/related`` reasons.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

_ROOT = Path(__file__).resolve().parents[3]
_VERSIONS = _ROOT / "migrations" / "versions"

#: The check constraint migration 133 created with the table.
_STATUS_CHECK = "memory_forget_provider_notices_status_check"

#: The check constraint 142 adds on the reason it also adds.
_REASON_CHECK = "memory_forget_provider_notices_reason_check"

#: The reason column, as the revision adds it.
_REASON_COLUMN = (
    "ALTER TABLE memory_forget_provider_notices"
    " ADD COLUMN IF NOT EXISTS memfw_reason TEXT"
)

#: The forget reasons, spelled as the CHECK spells them: the vocabulary is
#: the forget one, and this revision has nothing to add to it.
_FORGET_REASON_VOCABULARY = (
    "'no_forget_endpoint', 'not_supported', 'remote_rejected'"
)

#: The contract's ``/related`` reasons. They say why one memory sits next to
#: another; they are not forget reasons, and the CHECK must not name them.
_RELATED_REASONS = (
    "link",
    "related_field",
    "shared_tag",
    "shared_course",
    "shared_entity",
    "lexical",
    "semantic",
    "section",
)

_REASON_CHECK_SQL = (
    f"ALTER TABLE memory_forget_provider_notices"
    f" DROP CONSTRAINT IF EXISTS {_REASON_CHECK},"
    f" ADD CONSTRAINT {_REASON_CHECK} CHECK ("
    f" memfw_reason IS NULL OR memfw_reason IN ( {_FORGET_REASON_VOCABULARY} ) )"
)


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "migration_142", _VERSIONS / "142_memory_forget_unsupported.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events: list[str] = []
    op = MagicMock()
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    monkeypatch.setattr(module, "op", op)
    return module, events


def _script() -> ScriptDirectory:
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "migrations"))
    return ScriptDirectory.from_config(config)


def test_it_follows_141_in_a_linear_chain(migration):
    module, _events = migration
    assert (module.revision, module.down_revision) == ("142", "141")


def test_the_head_is_uniquely_146():
    """One head, and it is revision 146.

    Two branches that each add the next number leave two heads, and
    ``alembic upgrade head`` then refuses to start the server.
    """
    script = _script()

    assert script.get_heads() == ["151"]
    assert script.get_revision("151").down_revision == "150"
    assert script.get_revision("150").down_revision == "149"
    assert script.get_revision("149").down_revision == "148"
    assert script.get_revision("146").down_revision == "145"
    assert script.get_revision("145").down_revision == "144"
    assert script.get_revision("144").down_revision == "143"


def test_the_revision_chain_is_continuous():
    """Every revision is the parent of the next one, down to the base.

    A gap in the chain would leave a database that cannot be upgraded past
    it, so the walk is checked link by link rather than by count.
    """
    script = _script()
    revisions = list(script.walk_revisions("base", "heads"))

    assert len(revisions) == len(list(_VERSIONS.glob("*.py")))
    assert revisions[0].revision == "151"
    assert revisions[-1].revision == "001"
    assert revisions[-1].down_revision is None
    for revision, parent in zip(revisions, revisions[1:]):
        assert revision.down_revision == parent.revision, revision.revision


def test_upgrade_widens_the_status_check_with_unsupported(migration):
    module, events = migration

    module.upgrade()

    assert events == [
        "SET LOCAL lock_timeout = '5s'",
        f"ALTER TABLE memory_forget_provider_notices DROP CONSTRAINT IF EXISTS "
        f"{_STATUS_CHECK}, ADD CONSTRAINT {_STATUS_CHECK} CHECK (status IN "
        "( 'deleted', 'read_only', 'unavailable', 'not_configured', "
        "'unsupported' ))",
        _REASON_COLUMN,
        _REASON_CHECK_SQL,
    ]


def test_upgrade_adds_the_reason_column_and_its_check(migration):
    """The ruling has a column of its own, guarded by the forget vocabulary.

    ``memfw_reason`` is nullable because a ruling outside the vocabulary must
    not cost the notice its row, and the CHECK is the forget vocabulary
    verbatim: a database that has applied 142 refuses a reason no forget ever
    makes, which is the same boundary the write path enforces before it gets
    there.
    """
    module, events = migration

    module.upgrade()

    assert _REASON_COLUMN in events
    assert _REASON_CHECK_SQL in events
    # The column is added before the CHECK that reads it, or the CHECK has
    # nothing to attach to.
    assert events.index(_REASON_COLUMN) < events.index(_REASON_CHECK_SQL)


def test_the_reason_check_names_the_forget_codes_and_not_the_related_ones(
    migration,
):
    """The vocabulary is the forget one, all three of it.

    The contract's ``/related`` reasons -- ``link``, ``lexical`` and the rest --
    say why one memory sits next to another. They have nothing to do with a
    forget: a CHECK admitting them would accept a ruling no forget ever makes
    while refusing ``no_forget_endpoint``, which is the one this whole column
    exists to record.
    """
    module, events = migration

    module.upgrade()

    check = next(
        statement
        for statement in events
        if _REASON_CHECK in statement and "CHECK" in statement
    )
    for code in _FORGET_REASON_VOCABULARY.split(", "):
        assert code in check
    for related in _RELATED_REASONS:
        assert related not in check


def test_downgrade_drops_the_column_before_it_rewrites_the_rows(migration):
    """The order is the whole point of the downgrade.

    The previous build cannot read ``unsupported``. The rows that carry it
    become ``unavailable`` first; restoring the CHECK before that would fail
    on them. The reason column goes first of all: dropping it drops the CHECK
    on it with it, so the rewrite is not constrained by the new vocabulary.
    """
    module, events = migration

    module.downgrade()

    assert events == [
        "SET LOCAL lock_timeout = '5s'",
        "ALTER TABLE memory_forget_provider_notices"
        " DROP COLUMN IF EXISTS memfw_reason",
        "UPDATE memory_forget_provider_notices SET status = 'unavailable' "
        "WHERE status = 'unsupported'",
        f"ALTER TABLE memory_forget_provider_notices DROP CONSTRAINT IF EXISTS "
        f"{_STATUS_CHECK}, ADD CONSTRAINT {_STATUS_CHECK} CHECK (status IN "
        "( 'deleted', 'read_only', 'unavailable', 'not_configured' ))",
    ]
    drop = next(
        index
        for index, sql in enumerate(events)
        if sql.startswith("ALTER") and "DROP COLUMN" in sql
    )
    update = next(index for index, sql in enumerate(events) if sql.startswith("UPDATE"))
    alter = next(
        index
        for index, sql in enumerate(events)
        if sql.startswith("ALTER") and _STATUS_CHECK in sql
    )
    assert drop < update < alter


def test_the_historical_migration_133_is_not_touched():
    """The CHECK of 133 stays as it was written; 142 adds the new status.

    The reason is 142's column, not 133's: widening the table that 133 created
    would have put the vocabulary in two places, and the earlier revision is
    not this one's to edit.
    """
    text = (_VERSIONS / "133_memory_forget.py").read_text(encoding="utf-8")

    assert "'deleted', 'read_only', 'unavailable', 'not_configured'" in text
    assert "unsupported" not in text
    assert "memfw_reason" not in text
