"""Migration 104: one hidden Watchdog thread per user.

The Watchdog is its own long-lived Session, separate from the Router. It only
needs identity and a thread to receive review turns; the decision tools read
Tickets through the autonomy host.
"""

from alembic import op

revision = "104"
down_revision = "103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_watchdog_sessions (
            user_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            thread_id UUID NOT NULL
                REFERENCES conversation_threads(conversation_thread_id)
                ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS autonomy_watchdog_sessions")
