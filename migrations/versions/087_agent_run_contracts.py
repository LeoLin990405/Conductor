"""Migration 087: per-turn run contracts and structured subagent results.

The refactor records, for every main-agent turn, what contract it ran under
(permission mode, context budget, which memory entries were injected) and, for
every subagent run, a contract version plus a structured result JSON. The parent
merge reads that result instead of parsing a free-text final message, and the
memory indexer reads the injected-memory ids to measure retrieval quality.

``agent_run_contracts`` is new; ``subagent_runs`` gains nullable columns only, so
existing rows keep working and an old reader that selects its old columns is
unaffected. No data is backfilled: runs recorded before the flag was enabled
simply have NULL contract fields (the reader must treat NULL as "pre-contract").

The JSON shapes are pinned by ``src/server/contracts/refactor.py``
(``SubagentResult``). The database stores the JSON, not the schema — validating
it at write time keeps the table honest without a trigger.

Each statement commits on its own, so an upgrade cut short leaves some of them
applied with 086 still recorded: run it again, which is safe.

Rollback: ``alembic downgrade 086``. Drops the new table and the added columns;
structured results recorded in the meantime are lost with them (accepted: they
are a Phase-1-only addition).
"""

from alembic import op

revision = "087"
down_revision = "086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``agent_run_contracts`` FKs the hot ``conversation_responses`` table, and
    # the ALTER on ``subagent_runs`` takes its own lock; bound both.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS agent_run_contracts (
            conversation_response_id UUID PRIMARY KEY
                REFERENCES conversation_responses(conversation_response_id)
                ON DELETE CASCADE,
            contract_version INTEGER NOT NULL DEFAULT 1,
            permission_mode TEXT NOT NULL DEFAULT 'manual'
                CHECK (permission_mode IN ('manual', 'auto', 'skip')),
            model_id TEXT,
            context_budget JSONB NOT NULL DEFAULT '{}'::jsonb,
            injected_memory JSONB NOT NULL DEFAULT '[]'::jsonb,
            verifier_verdict JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    # Empty on creation, but the backfill job (and later readers) scan per user.
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_agent_run_contracts_created
        ON agent_run_contracts (created_at)
    """)
    op.execute("""
        ALTER TABLE subagent_runs
            ADD COLUMN IF NOT EXISTS contract_version INTEGER,
            ADD COLUMN IF NOT EXISTS context_budget JSONB,
            ADD COLUMN IF NOT EXISTS tool_allowlist TEXT[],
            ADD COLUMN IF NOT EXISTS result JSONB,
            ADD COLUMN IF NOT EXISTS verification JSONB
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE subagent_runs
            DROP COLUMN IF EXISTS verification,
            DROP COLUMN IF EXISTS result,
            DROP COLUMN IF EXISTS tool_allowlist,
            DROP COLUMN IF EXISTS context_budget,
            DROP COLUMN IF EXISTS contract_version
    """)
    op.execute("DROP INDEX IF EXISTS ix_agent_run_contracts_created")
    op.execute("DROP TABLE IF EXISTS agent_run_contracts")
