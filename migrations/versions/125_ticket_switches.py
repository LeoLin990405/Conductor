"""Migration 125: a Ticket's model-switch history (ob-switch).

Each entry records one move of a stuck line to another provider/model.
"""

from alembic import op

revision = "125"
down_revision = "124"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE autonomy_tickets "
        "ADD COLUMN IF NOT EXISTS switches JSONB NOT NULL DEFAULT '[]'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE autonomy_tickets DROP COLUMN IF EXISTS switches")
