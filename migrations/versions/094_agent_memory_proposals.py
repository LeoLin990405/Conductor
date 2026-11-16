"""Migration 094: a reviewable identity/status for memory proposals.

The D5 proposal channel first landed as jsonb inside finished subagent contracts.
Review needs identity and lifecycle, which a jsonb array cannot express: this
table records each extracted proposal once (idempotent per task run + type +
text hash) with `proposed` -> `approved`/`rejected`. The contract jsonb stays the
audit copy; this is the queue the review surface and the store-writing approval
route operate on.

`text_hash` exists so the uniqueness check cannot hit the btree size limit on a
long proposal text. Approval writes the LangGraph store (source of truth) and the
projection follows; rejection writes nothing but the status.

Each statement commits on its own, so an upgrade cut short leaves the table
applied with 093 still recorded: run it again, which is safe.

Rollback: ``alembic downgrade 093``. Drops the table; contract jsonb is untouched.
"""

from alembic import op

revision = "094"
down_revision = "093"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS agent_memory_proposals (
            proposal_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL,
            thread_id UUID NOT NULL,
            task_run_id UUID NOT NULL,
            proposal_type TEXT NOT NULL
                CHECK (proposal_type IN ('user', 'feedback', 'project', 'reference')),
            proposal_key TEXT NOT NULL,
            text TEXT NOT NULL,
            text_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed'
                CHECK (status IN ('proposed', 'approved', 'rejected')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            decided_at TIMESTAMPTZ,
            UNIQUE (task_run_id, proposal_type, text_hash)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_agent_memory_proposals_user_status
        ON agent_memory_proposals (user_id, status, created_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_memory_proposals")
