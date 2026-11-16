import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

_V = Path(__file__).resolve().parents[3] / "migrations" / "versions" / "125_ticket_switches.py"


def _load(monkeypatch):
    spec = importlib.util.spec_from_file_location("migration_125", _V)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    op = MagicMock()
    monkeypatch.setattr(m, "op", op)
    return m, op


def test_chain_and_reversible(monkeypatch):
    m, op = _load(monkeypatch)
    assert (m.revision, m.down_revision) == ("125", "124")
    m.upgrade()
    assert "switches JSONB" in op.execute.call_args_list[-1].args[0]
    m.downgrade()
    assert "DROP COLUMN IF EXISTS switches" in op.execute.call_args_list[-1].args[0]
