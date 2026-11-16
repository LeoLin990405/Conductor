"""Persist round provider groups and continuation ancestry."""

from alembic import op

revision = "118"
down_revision = "117"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE autonomy_deliberations ADD COLUMN IF NOT EXISTS "
        "parent_deliberation_id UUID REFERENCES autonomy_deliberations(deliberation_id) "
        "ON DELETE SET NULL"
    )
    op.execute(
        "ALTER TABLE autonomy_deliberation_rounds ADD COLUMN IF NOT EXISTS "
        "provider_groups JSONB NOT NULL DEFAULT '[]'::jsonb"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE autonomy_deliberation_rounds DROP COLUMN IF EXISTS provider_groups"
    )
    op.execute(
        "ALTER TABLE autonomy_deliberations DROP COLUMN IF EXISTS parent_deliberation_id"
    )
