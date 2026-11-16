"""Migration 146: retain the isolated Worker's structured Git check result."""

from alembic import op

revision = "146"
down_revision = "145"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE autonomy_ticket_reports "
        "ADD COLUMN IF NOT EXISTS git_check TEXT "
        "CHECK (git_check IS NULL OR git_check = 'unsupported_runtime')"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE autonomy_ticket_reports DROP COLUMN IF EXISTS git_check"
    )
