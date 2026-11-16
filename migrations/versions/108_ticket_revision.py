"""Migration 108: a Ticket revision for mutable context caching.

The revision changes on every Ticket/Input/Report write and lets a read cache
validate a cached Ticket detail without using updated_at (whose report ordering
semantics are separate).
"""

from alembic import op

revision = "108"
down_revision = "107"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE autonomy_tickets "
        "ADD COLUMN IF NOT EXISTS revision BIGINT NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE autonomy_tickets DROP COLUMN IF EXISTS revision")
