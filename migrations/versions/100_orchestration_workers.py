"""Migration 100: durable PTC worker ledger.

Subagent workers already live in ``subagent_tasks``; this table gives native CLI
workers (and future direct workers) the same durable identity so a restart or a
different backend worker can still render the PTC orchestration snapshot.
"""

from alembic import op

revision = "100"
down_revision = "099"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS orchestration_workers (
            id TEXT PRIMARY KEY,
            thread_id UUID NOT NULL
                REFERENCES conversation_threads(conversation_thread_id)
                ON DELETE CASCADE,
            kind TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            parent_id TEXT NOT NULL DEFAULT '',
            depends_on JSONB NOT NULL DEFAULT '[]'::jsonb,
            prompt_preview TEXT NOT NULL DEFAULT '',
            result_preview TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            meta JSONB NOT NULL DEFAULT '{}'::jsonb,
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_orchestration_workers_thread "
        "ON orchestration_workers (thread_id, started_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_orchestration_workers_status "
        "ON orchestration_workers (thread_id, status)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS orchestration_workers")
