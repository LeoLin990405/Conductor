from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION = REPO_ROOT / "migrations" / "versions" / "137_memory_consolidation.py"


def _load():
    spec = importlib.util.spec_from_file_location("migration_137", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_consolidation_migration_is_reversible_and_fences_producer_identity():
    module = _load()
    ddl = MIGRATION.read_text(encoding="utf-8")

    assert module.revision == "137"
    assert module.down_revision == "136"
    assert "CREATE TABLE memory_consolidation_runs" in ddl
    assert "UNIQUE (user_id, run_date, policy_version)" in ddl
    assert "CREATE TABLE IF NOT EXISTS memory_entry_lineage" in ddl
    assert "source_entry_ids UUID[]" in ddl
    assert "cardinality(source_entry_ids) BETWEEN 2 AND 16" in ddl
    assert "producer_kind IS NOT NULL" in ddl
    assert "AND producer_kind = 'memory_consolidation' AND producer_id IS NOT NULL" in ddl
    assert "producer_kind IS NULL AND producer_id IS NULL" in ddl
    assert "CREATE UNIQUE INDEX uq_memory_consolidation_proposal_identity" in ddl
    assert "remove consolidation proposals before downgrading 137" in ddl
    assert "DROP TABLE IF EXISTS memory_entry_lineage" in ddl
