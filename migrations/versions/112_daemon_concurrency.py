"""Allow each registered local daemon to run multiple Tickets."""

from alembic import op

revision = "112"
down_revision = "111"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE autonomy_daemons ADD COLUMN IF NOT EXISTS "
        "max_concurrency INTEGER NOT NULL DEFAULT 1"
    )
    op.execute(
        "ALTER TABLE autonomy_daemons ADD CONSTRAINT autonomy_daemons_max_concurrency_check "
        "CHECK (max_concurrency BETWEEN 1 AND 32)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE autonomy_daemons DROP CONSTRAINT IF EXISTS "
        "autonomy_daemons_max_concurrency_check"
    )
    op.execute("ALTER TABLE autonomy_daemons DROP COLUMN IF EXISTS max_concurrency")
