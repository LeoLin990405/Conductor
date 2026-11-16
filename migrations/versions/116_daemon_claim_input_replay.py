"""Persist delivered follow-up inputs in daemon claim receipts for replay."""

from alembic import op

revision = "116"
down_revision = "115"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE autonomy_daemon_claims ADD COLUMN IF NOT EXISTS "
        "input_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE autonomy_daemon_claims DROP COLUMN IF EXISTS input_snapshot"
    )
