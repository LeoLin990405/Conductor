"""Migration 109: BYOM local daemons.

A daemon holds a hashed bearer token and pulls queued Tickets that name it.
The Ticket keeps execution local; the cloud only stores state and reports.
"""

from alembic import op

revision = "109"
down_revision = "108"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_daemons (
            daemon_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            token_hash TEXT NOT NULL UNIQUE,
            provider TEXT NOT NULL DEFAULT '',
            last_seen_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_daemons_user "
        "ON autonomy_daemons (user_id, created_at)"
    )
    op.execute(
        "ALTER TABLE autonomy_tickets ADD COLUMN IF NOT EXISTS worker_daemon_id UUID "
        "REFERENCES autonomy_daemons(daemon_id) ON DELETE SET NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE autonomy_tickets DROP COLUMN IF EXISTS worker_daemon_id")
    op.execute("DROP TABLE IF EXISTS autonomy_daemons")
