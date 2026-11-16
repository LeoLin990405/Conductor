"""Migration 107: mark a Ticket Input handed back to Eval."""

from alembic import op

revision = "107"
down_revision = "106"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE autonomy_ticket_inputs "
        "ADD COLUMN IF NOT EXISTS handed_back BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE autonomy_ticket_inputs DROP COLUMN IF EXISTS handed_back")
