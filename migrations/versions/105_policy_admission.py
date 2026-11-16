"""Migration 105: the Policy's admission table.

The admission table decides what a listed/unlisted sender may start in a
receiving scope. It is part of the Policy version so every Ticket records the
strength it was accepted under.
"""

from alembic import op

revision = "105"
down_revision = "104"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE autonomy_policies "
        "ADD COLUMN IF NOT EXISTS admission JSONB NOT NULL DEFAULT '{}'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE autonomy_policies DROP COLUMN IF EXISTS admission")
