"""Migration 106: count consecutive Eval expiries per Thread.

The Router gets two retries; the third expiry records the Eval unavailable and
stops automatically re-evaluating the same batch. A Decision resets the count.
"""

from alembic import op

revision = "106"
down_revision = "105"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE autonomy_threads "
        "ADD COLUMN IF NOT EXISTS eval_expiries INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE autonomy_threads DROP COLUMN IF EXISTS eval_expiries")
