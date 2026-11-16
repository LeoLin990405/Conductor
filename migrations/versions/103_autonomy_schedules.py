"""Migration 103: recurring Super-agent tasks.

Scheduled work is a Source of Tickets, not a second execution path: a due row
creates a manual Ticket and then advances its own cron clock. Keeping it in the
autonomy schema means every run shows up in Inbox/Tasks/Runs like any other.
"""

from alembic import op

revision = "103"
down_revision = "102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_schedules (
            schedule_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            prompt TEXT NOT NULL,
            cron_expression TEXT NOT NULL,
            timezone TEXT NOT NULL DEFAULT 'UTC',
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            worker_provider TEXT NOT NULL DEFAULT '',
            worker_model TEXT NOT NULL DEFAULT '',
            next_run_at TIMESTAMPTZ NOT NULL,
            last_run_at TIMESTAMPTZ,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_schedules_due "
        "ON autonomy_schedules (enabled, next_run_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_schedules_user "
        "ON autonomy_schedules (user_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS autonomy_schedules")
