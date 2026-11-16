"""Migration 144: retain the full Worker report body."""

from alembic import op

revision = "144"
down_revision = "143"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE autonomy_ticket_reports "
        "ADD COLUMN IF NOT EXISTS body TEXT"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE autonomy_ticket_reports "
        "DROP COLUMN IF EXISTS body"
    )
