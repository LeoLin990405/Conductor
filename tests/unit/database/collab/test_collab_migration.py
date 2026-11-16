"""Migration 150 adds the collaboration tables and keeps one Alembic head."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

_ROOT = Path(__file__).resolve().parents[4]
_VERSIONS = _ROOT / "migrations" / "versions"
_MIGRATION = _VERSIONS / "150_collab_apps.py"
_DOC = _ROOT / "MIGRATIONS.md"

_TABLES = (
    "collab_apps",
    "collab_leases",
    "collab_handoffs",
    "collab_messages",
    "collab_reviews",
    "collab_audit",
)


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location("migration_150", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
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


def _created_columns(table: str) -> list[str]:
    source = _MIGRATION.read_text(encoding="utf-8")
    upgrade = source.split("def upgrade", 1)[1].split("def downgrade", 1)[0]
    body = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\)\s*\"\"\"",
        upgrade,
        re.DOTALL,
    )
    assert body, f"the migration does not create {table}"
    return re.findall(
        r"^\s*(\w+)\s+(?:TEXT|UUID|SMALLINT|BOOLEAN|INTEGER|TIMESTAMPTZ|JSONB)\b",
        body.group(1),
        re.MULTILINE,
    )


def test_it_follows_149_and_is_the_single_head(migration):
    module, _events = migration
    assert (module.revision, module.down_revision) == ("150", "149")
    script = _script()
    assert script.get_heads() == ["151"]
    assert script.get_revision("151").down_revision == "150"
    assert script.get_revision("150").down_revision == "149"
    assert script.get_revision("149").down_revision == "148"
    assert len(list(_VERSIONS.glob("150_*.py"))) == 1


def test_upgrade_starts_with_the_lock_timeout_then_creates_six_tables(migration):
    module, events = migration
    module.upgrade()
    assert events[0] == "SET LOCAL lock_timeout = '5s'"
    sql = " ".join(events)
    for table in _TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql


def test_apps_store_only_a_sha256_digest_and_a_closed_scope_grant(migration):
    module, events = migration
    module.upgrade()
    statement = next(
        event for event in events if "CREATE TABLE IF NOT EXISTS collab_apps" in event
    )
    assert "token_sha256 TEXT NOT NULL UNIQUE" in statement
    assert "token_sha256 ~ '^[0-9a-f]{64}$'" in statement
    assert re.search(r"^\s*token\s", statement, re.MULTILINE) is None
    assert "scopes <@ ARRAY" in statement
    for scope in ("read", "propose", "claim", "report", "ask", "review"):
        assert f"'{scope}'" in statement
    assert "max_leases SMALLINT NOT NULL DEFAULT 1" in statement


def test_a_lease_has_at_most_one_pending_handoff(migration):
    """A stale request cannot be accepted, expired or declined later.

    Without the index two pending handoffs for one lease can coexist, and the
    older one can act on the lease after it has changed hands and come back.
    """
    module, events = migration
    module.upgrade()
    assert (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_collab_handoffs_one_pending "
        "ON collab_handoffs (lease_id) WHERE state = 'pending'"
        in " ".join(events)
    )


def test_leases_have_a_single_live_holder_and_ticket_cas_columns(migration):
    module, events = migration
    module.upgrade()
    sql = " ".join(events)
    assert (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_collab_leases_live_target "
        "ON collab_leases (target_ref) WHERE state IN ('active', 'handoff_pending')"
        in sql
    )
    lease_statement = next(
        event
        for event in events
        if "CREATE TABLE IF NOT EXISTS collab_leases" in event
    )
    for column in (
        "lease_id",
        "app_id",
        "agent",
        "target_ref",
        "target_kind",
        "ticket_id",
        "line_id",
        "revision",
        "state",
        "worktree_path",
        "worktree_branch",
        "worktree_base_sha",
        "heartbeat_at",
        "expires_at",
        "max_lease_until",
        "created_at",
        "ended_at",
        "end_reason",
    ):
        assert f"{column} " in lease_statement


def test_handoffs_store_only_a_conversation_reference(migration):
    module, events = migration
    module.upgrade()
    statement = next(
        event
        for event in events
        if "CREATE TABLE IF NOT EXISTS collab_handoffs" in event
    )
    assert "conversation_ref JSONB" in statement
    assert re.search(r"^\s*session_id\s", statement, re.MULTILINE) is None
    assert "self_report JSONB" in statement
    assert "expires_at TIMESTAMPTZ NOT NULL" in statement


def test_reviews_refuse_self_review_in_storage_too(migration):
    module, events = migration
    module.upgrade()
    sql = " ".join(events)
    assert "collab_reviews_not_self" in sql
    assert "reviewer_app_id IS NULL OR reviewer_app_id <> author_app_id" in sql
    statement = next(
        event
        for event in events
        if "CREATE TABLE IF NOT EXISTS collab_reviews" in event
    )
    assert "commit_sha ~ '^[0-9a-f]{7,40}$'" in statement
    assert "verdict JSONB" in statement


def test_audit_keeps_a_digest_not_the_parameters(migration):
    module, events = migration
    module.upgrade()
    statement = next(
        event
        for event in events
        if "CREATE TABLE IF NOT EXISTS collab_audit" in event
    )
    assert "params_sha256" in statement
    assert "params JSONB" not in statement
    assert "rate_limited BOOLEAN" in statement
    assert "redacted INTEGER" in statement
    assert "duration_ms INTEGER" in statement


def test_downgrade_drops_the_tables_in_reverse_dependency_order(migration):
    module, events = migration
    module.downgrade()
    assert events[0] == "SET LOCAL lock_timeout = '5s'"
    assert events[1:] == [
        "DROP TABLE IF EXISTS collab_audit",
        "DROP TABLE IF EXISTS collab_reviews",
        "DROP TABLE IF EXISTS collab_messages",
        "DROP TABLE IF EXISTS collab_handoffs",
        "DROP TABLE IF EXISTS collab_leases",
        "DROP TABLE IF EXISTS collab_apps",
    ]


def test_the_schema_snapshot_lists_the_new_tables():
    snapshot = json.loads(
        (_ROOT / "src" / "conductor_cli" / "schema_snapshot.json").read_text(
            encoding="utf-8"
        )
    )
    # 151 (R6 role registry) follows; the collab tables stay listed.
    assert int(snapshot["revision"]) >= 150
    for table in _TABLES:
        assert table in snapshot["tables"]
    assert "token_sha256" in snapshot["tables"]["collab_apps"]


def test_migrations_md_names_the_new_head_and_every_created_column():
    rows = [
        line
        for line in _DOC.read_text(encoding="utf-8").splitlines()
        if line.startswith("| 150 |")
    ]
    assert len(rows) == 1, rows
    named = set(re.findall(r"`(\w+)`", rows[0]))
    for table in _TABLES:
        expected = set(_created_columns(table))
        missing = expected - named
        assert not missing, f"MIGRATIONS.md row 150 omits {table} columns {missing}"
