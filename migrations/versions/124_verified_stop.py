"""Record native-daemon stop requests and their acknowledgement evidence."""

from alembic import op

revision = "124"
down_revision = "123"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # The last successful renewal is distinct from worker_lease_expires_at:
    # expiry moves with the lease, while a stop acknowledgement must prove that
    # no heartbeat arrived after the stop request.
    op.execute(
        "ALTER TABLE autonomy_tickets ADD COLUMN IF NOT EXISTS "
        "worker_lease_heartbeat_at TIMESTAMPTZ"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_stop_requests (
            stop_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ticket_id UUID NOT NULL UNIQUE
                REFERENCES autonomy_tickets(ticket_id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            daemon_id UUID REFERENCES autonomy_daemons(daemon_id) ON DELETE SET NULL,
            lease_id UUID,
            mode TEXT NOT NULL DEFAULT 'daemon',
            verification TEXT NOT NULL DEFAULT 'pending',
            requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deadline_at TIMESTAMPTZ NOT NULL,
            acknowledged_at TIMESTAMPTZ,
            verified_at TIMESTAMPTZ,
            escalated_at TIMESTAMPTZ,
            reason TEXT NOT NULL DEFAULT '',
            evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
            attempts INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_stop_requests_pending "
        "ON autonomy_stop_requests (deadline_at) WHERE verification = 'pending'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_stop_requests_user "
        "ON autonomy_stop_requests (user_id, requested_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS autonomy_stop_requests")
    op.execute(
        "ALTER TABLE autonomy_tickets DROP COLUMN IF EXISTS worker_lease_heartbeat_at"
    )
