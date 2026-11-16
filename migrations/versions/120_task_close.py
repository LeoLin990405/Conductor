"""Record when a person closes a task.

Close is the task interaction's sign-off. It is separate from worker
completion: a running task is cancelled and stamped closed, and a finished
task is stamped closed without rewriting its outcome.

Revision 119 is the channel-reply approval migration. This stamp follows
that revision so the chain stays linear.
"""

from alembic import op

revision = "120"
down_revision = "119"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE autonomy_tickets ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE autonomy_tickets "
        "ADD COLUMN IF NOT EXISTS close_reason TEXT NOT NULL DEFAULT ''"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE autonomy_tickets DROP COLUMN IF EXISTS close_reason")
    op.execute("ALTER TABLE autonomy_tickets DROP COLUMN IF EXISTS closed_at")
