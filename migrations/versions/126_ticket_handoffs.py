"""Handoff manifests: verified file handoffs between agents.

A handoff is one agent handing files to another through a durable record
stored with the Ticket: paths relative to the workspace root, sizes and sha256
checksums, plus producer/consumer/purpose. The consumer re-hashes every file
before use and the row settles as verified or rejected, never both.
"""

from alembic import op

revision = "126"
down_revision = "125"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_ticket_handoffs (
            handoff_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ticket_id UUID NOT NULL
                REFERENCES autonomy_tickets(ticket_id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            producer TEXT NOT NULL,
            consumer TEXT NOT NULL,
            purpose TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'registered'
                CHECK (status IN ('registered', 'verified', 'rejected')),
            files JSONB NOT NULL DEFAULT '[]'::jsonb,
            verify_error TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            verified_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_handoffs_ticket "
        "ON autonomy_ticket_handoffs (ticket_id, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_handoffs_user "
        "ON autonomy_ticket_handoffs (user_id, updated_at DESC)"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS autonomy_ticket_handoffs")
