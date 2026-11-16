"""Migration 144 adds a nullable full Worker report body."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest

_ROOT = Path(__file__).resolve().parents[3]
_VERSIONS = _ROOT / "migrations" / "versions"


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "migration_144", _VERSIONS / "144_ticket_report_body.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    events: list[str] = []
    op = MagicMock()
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    monkeypatch.setattr(module, "op", op)
    return module, events


def test_it_follows_143_in_a_linear_chain(migration):
    module, _events = migration
    assert (module.revision, module.down_revision) == ("144", "143")
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["151"]
    assert script.get_revision("151").down_revision == "150"
    assert script.get_revision("150").down_revision == "149"
    assert script.get_revision("144").down_revision == "143"


def test_upgrade_adds_an_untouched_nullable_body_column(migration):
    module, events = migration
    module.upgrade()
    assert events == [
        "SET LOCAL lock_timeout = '5s'",
        "ALTER TABLE autonomy_ticket_reports ADD COLUMN IF NOT EXISTS body TEXT",
    ]


def test_downgrade_removes_only_the_body_column(migration):
    module, events = migration
    module.downgrade()
    assert events == [
        "SET LOCAL lock_timeout = '5s'",
        "ALTER TABLE autonomy_ticket_reports DROP COLUMN IF EXISTS body",
    ]
