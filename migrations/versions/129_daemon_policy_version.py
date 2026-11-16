"""Bind a daemon lease to the local policy version it was granted under.

Follows 128_context_scopes_a2a (integrate/r2). The beyond-endpoint
chain owns the migration numbers from 129 on.

The endpoint device holds its own policy; a lease names the version the
daemon reported when it claimed, so a policy the device replaced after the
grant can refuse the lease instead of running it under unseen rules.
"""

from alembic import op

revision = "129"
down_revision = "128"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE autonomy_daemon_claims "
        "ADD COLUMN IF NOT EXISTS policy_version TEXT NOT NULL DEFAULT ''"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE autonomy_daemon_claims DROP COLUMN IF EXISTS policy_version"
    )
