"""Migration 102: a Ticket's chosen Worker runtime.

A manual Super-agent task may name a PTC model or a native CLI provider. The
binding lives on the Ticket so a retry or restart resumes the same runtime.
"""

from alembic import op

revision = "102"
down_revision = "101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE autonomy_tickets "
        "ADD COLUMN IF NOT EXISTS worker_provider TEXT NOT NULL DEFAULT ''"
    )
    op.execute(
        "ALTER TABLE autonomy_tickets "
        "ADD COLUMN IF NOT EXISTS worker_model TEXT NOT NULL DEFAULT ''"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE autonomy_tickets DROP COLUMN IF EXISTS worker_model")
    op.execute("ALTER TABLE autonomy_tickets DROP COLUMN IF EXISTS worker_provider")
