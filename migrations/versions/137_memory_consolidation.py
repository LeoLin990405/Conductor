"""Migration 137: durable consolidation claims, proposal audit, and lineage."""

from alembic import op

revision = "137"
down_revision = "136"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "ADD COLUMN producer_kind TEXT, "
        "ADD COLUMN producer_id UUID, "
        "ADD COLUMN source_entry_ids UUID[] NOT NULL DEFAULT '{}'::uuid[], "
        "ADD COLUMN producer_metadata JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "ALTER COLUMN thread_id DROP NOT NULL, "
        "ALTER COLUMN task_run_id DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "ADD CONSTRAINT ck_memory_proposal_producer_identity CHECK ("
        "(producer_kind IS NULL AND producer_id IS NULL "
        " AND thread_id IS NOT NULL AND task_run_id IS NOT NULL "
        " AND cardinality(source_entry_ids) = 0) OR "
        "(producer_kind IS NOT NULL "
        " AND producer_kind = 'memory_consolidation' AND producer_id IS NOT NULL "
        " AND thread_id IS NULL AND task_run_id IS NULL "
        " AND cardinality(source_entry_ids) BETWEEN 2 AND 16))"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "ADD CONSTRAINT ck_memory_proposal_producer_metadata_object "
        "CHECK (jsonb_typeof(producer_metadata) = 'object')"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_memory_consolidation_proposal_identity "
        "ON agent_memory_proposals (producer_kind, producer_id, proposal_type, text_hash) "
        "WHERE producer_kind = 'memory_consolidation'"
    )
    op.execute(
        "CREATE TABLE memory_consolidation_runs ("
        "run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(), "
        "user_id UUID NOT NULL, raw_user_id TEXT NOT NULL, "
        "run_date DATE NOT NULL, policy_version TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'pending' "
        "CHECK (status IN ('pending', 'running', 'complete')), "
        "cursor_key TEXT NOT NULL DEFAULT '', groups_processed INTEGER NOT NULL DEFAULT 0 "
        "CHECK (groups_processed BETWEEN 0 AND 25), "
        "proposals_created INTEGER NOT NULL DEFAULT 0 CHECK (proposals_created >= 0), "
        "attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 6), "
        "lease_until TIMESTAMPTZ, retry_after TIMESTAMPTZ, last_error_code TEXT, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), "
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), "
        "UNIQUE (user_id, run_date, policy_version))"
    )
    op.execute(
        "CREATE INDEX ix_memory_consolidation_runs_retry "
        "ON memory_consolidation_runs (run_date, status, retry_after)"
    )
    # Older memory branches created a narrower lineage table before the
    # consolidation migration landed. Keep the rows, add the missing column and
    # constraints, and only create the table on a fresh database.
    op.execute(
        "CREATE TABLE IF NOT EXISTS memory_entry_lineage ("
        "user_id UUID NOT NULL, entry_id UUID NOT NULL REFERENCES memory_entries(entry_id) "
        "ON DELETE CASCADE, source_entry_id UUID NOT NULL REFERENCES memory_entries(entry_id) "
        "ON DELETE CASCADE, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), "
        "PRIMARY KEY (user_id, entry_id, source_entry_id), "
        "CHECK (entry_id <> source_entry_id))"
    )
    op.execute(
        "ALTER TABLE memory_entry_lineage "
        "ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()"
    )
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'memory_entry_lineage_entry_id_fkey'
            ) THEN
                ALTER TABLE memory_entry_lineage
                    ADD CONSTRAINT memory_entry_lineage_entry_id_fkey
                    FOREIGN KEY (entry_id) REFERENCES memory_entries(entry_id)
                    ON DELETE CASCADE;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'memory_entry_lineage_source_entry_id_fkey'
            ) THEN
                ALTER TABLE memory_entry_lineage
                    ADD CONSTRAINT memory_entry_lineage_source_entry_id_fkey
                    FOREIGN KEY (source_entry_id) REFERENCES memory_entries(entry_id)
                    ON DELETE CASCADE;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'memory_entry_lineage_check'
            ) THEN
                ALTER TABLE memory_entry_lineage
                    ADD CONSTRAINT memory_entry_lineage_check
                    CHECK (entry_id <> source_entry_id);
            END IF;
        END $$;
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memory_entry_lineage_source "
        "ON memory_entry_lineage (user_id, source_entry_id)"
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM agent_memory_proposals "
        "WHERE producer_kind = 'memory_consolidation') THEN "
        "RAISE EXCEPTION 'remove consolidation proposals before downgrading 137'; "
        "END IF; END $$"
    )
    op.execute("DROP TABLE IF EXISTS memory_entry_lineage")
    op.execute("DROP TABLE IF EXISTS memory_consolidation_runs")
    op.execute("DROP INDEX IF EXISTS uq_memory_consolidation_proposal_identity")
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "DROP CONSTRAINT IF EXISTS ck_memory_proposal_producer_metadata_object, "
        "DROP CONSTRAINT IF EXISTS ck_memory_proposal_producer_identity, "
        "ALTER COLUMN thread_id SET NOT NULL, "
        "ALTER COLUMN task_run_id SET NOT NULL, "
        "DROP COLUMN IF EXISTS producer_metadata, "
        "DROP COLUMN IF EXISTS source_entry_ids, "
        "DROP COLUMN IF EXISTS producer_id, "
        "DROP COLUMN IF EXISTS producer_kind"
    )
