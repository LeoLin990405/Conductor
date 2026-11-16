"""134 stores conservative origin/evidence metadata on queued proposals."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_VERSION = (
    Path(__file__).resolve().parents[3]
    / "migrations"
    / "versions"
    / "134_memory_origin.py"
)


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location("migration_134", _VERSION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    events: list[str] = []
    op = MagicMock()
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    monkeypatch.setattr(module, "op", op)
    return module, events


def test_revision_134_adds_closed_conservative_origin_and_evidence_contract(migration):
    module, events = migration
    assert (module.revision, module.down_revision) == ("134", "133")

    module.upgrade()
    sql = " ".join(events)
    assert "ALTER TABLE agent_memory_proposals" in sql
    assert "origin TEXT NOT NULL DEFAULT 'assistant_inference'" in sql
    assert "confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5" in sql
    assert "evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb" in sql
    assert "evidence_count INTEGER NOT NULL DEFAULT 0" in sql
    assert "counts_as_evidence BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "assistant_inference" in sql and "user" in sql and "tool" in sql and "web" in sql
    assert "confidence >= 0 AND confidence <= 1" in sql
    assert "origin <> 'assistant_inference' OR confidence <= 0.5" in sql
    assert "evidence_count >= 0" in sql
    assert "origin = 'user' OR evidence_count = 0" in sql
    assert "counts_as_evidence = (origin = 'user' AND evidence_count > 0)" in sql
    assert "jsonb_typeof(evidence_refs) = 'array'" in sql
    assert "DROP CONSTRAINT" not in sql
    assert "UNIQUE (task_run_id, proposal_type, text_hash, origin)" not in sql
    assert "GENERATED ALWAYS" not in sql


def test_downgrade_removes_only_added_proposal_metadata(migration):
    module, events = migration
    module.downgrade()
    sql = " ".join(events)
    assert "ALTER TABLE agent_memory_proposals" in sql
    for column in (
        "counts_as_evidence",
        "evidence_count",
        "evidence_refs",
        "confidence",
        "origin",
    ):
        assert f"DROP COLUMN IF EXISTS {column}" in sql
    assert "DROP TABLE agent_memory_proposals" not in sql
    assert "ADD CONSTRAINT" not in sql
