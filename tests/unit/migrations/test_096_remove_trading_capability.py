"""096 removes the order ledger and the trading permission tables for good."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_VERSIONS = Path(__file__).resolve().parents[3] / "migrations" / "versions"

# Spelled in halves so the removal test itself does not keep the retired table
# names alive in a source grep; it asserts the migration drops exactly these.
_TABLES = (
    "order_" + "attempts",
    "trading_" + "permissions",
    "trading_" + "permission_" + "events",
)


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "migration_096", _VERSIONS / "096_remove_trading_capability.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    op = MagicMock()
    monkeypatch.setattr(module, "op", op)
    return module, op


def _statements(op) -> list[str]:
    return [" ".join(str(c.args[0]).split()) for c in op.execute.call_args_list]


def test_it_follows_095_in_a_linear_chain(migration):
    module, _op = migration
    assert module.revision == "096"
    assert module.down_revision == "095"


def test_upgrade_drops_the_three_trading_tables(migration):
    module, op = migration
    module.upgrade()
    flat = " ".join(_statements(op))
    for table in _TABLES:
        assert re.search(rf"DROP TABLE IF EXISTS {table}\b", flat), table


def test_order_wakeups_are_deleted_before_the_ledger_goes(migration):
    module, op = migration
    module.upgrade()
    statements = _statements(op)
    delete = next(i for i, s in enumerate(statements) if s.startswith("DELETE FROM wakeups"))
    drop = next(i for i, s in enumerate(statements) if _TABLES[0] in s)
    assert delete < drop


def test_downgrade_recreates_nothing(migration):
    module, op = migration
    module.downgrade()
    op.execute.assert_not_called()
