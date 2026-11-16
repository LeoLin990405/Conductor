from __future__ import annotations

import importlib


def test_router_snapshot_migration_upgrade_and_downgrade_are_reversible(monkeypatch):
    migration = importlib.import_module("migrations.versions.123_router_snapshot_metrics")
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    assert migration.revision == "123"
    assert migration.down_revision == "122"

    migration.upgrade()
    upgrade_sql = "\n".join(statements)
    assert "CREATE TABLE autonomy_router_baseline_snapshots" in upgrade_sql
    assert "CREATE TABLE autonomy_router_decision_metrics" in upgrade_sql
    assert "decision_id UUID PRIMARY KEY" in upgrade_sql
    assert "eval_id UUID NOT NULL UNIQUE" in upgrade_sql
    assert "decision_status" not in upgrade_sql
    assert "dispatch_status IN ('reserved', 'submitted', 'failed')" in upgrade_sql
    assert "(user_id, stable_decision_key, decided_at DESC)" in upgrade_sql
    assert "WHERE route_decision IS NOT NULL" in upgrade_sql
    assert "OLD.config IS DISTINCT FROM NEW.config" in upgrade_sql
    assert "OLD.state" not in upgrade_sql
    assert "OLD.admission IS DISTINCT FROM NEW.admission" in upgrade_sql
    assert "CREATE TRIGGER autonomy_sources_router_snapshot_invalidate" in upgrade_sql

    statements.clear()
    migration.downgrade()
    downgrade_sql = "\n".join(statements)
    assert "DROP TABLE IF EXISTS autonomy_router_decision_metrics" in downgrade_sql
    assert "DROP TABLE IF EXISTS autonomy_router_baseline_snapshots" in downgrade_sql
    assert "DROP TRIGGER IF EXISTS autonomy_sources_router_snapshot_invalidate" in downgrade_sql
