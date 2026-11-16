"""Fence local daemon attempts with durable expiring leases."""

from alembic import op

revision = "111"
down_revision = "110"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE autonomy_tickets ADD COLUMN IF NOT EXISTS worker_lease_id UUID"
    )
    op.execute(
        "ALTER TABLE autonomy_tickets ADD COLUMN IF NOT EXISTS worker_lease_claim_id UUID"
    )
    op.execute(
        "ALTER TABLE autonomy_tickets ADD COLUMN IF NOT EXISTS worker_lease_expires_at TIMESTAMPTZ"
    )
    op.execute("""CREATE TABLE IF NOT EXISTS autonomy_daemon_claims (
        claim_id UUID PRIMARY KEY, daemon_id UUID NOT NULL REFERENCES autonomy_daemons(daemon_id) ON DELETE CASCADE,
        ticket_id UUID NOT NULL REFERENCES autonomy_tickets(ticket_id) ON DELETE CASCADE,
        lease_id UUID NOT NULL UNIQUE, report_result JSONB,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
    # Legacy rows have no proof of current daemon liveness, so the next
    # scheduler pass requeues them instead of leaving them stuck forever.
    op.execute("""UPDATE autonomy_tickets SET worker_lease_expires_at = NOW() - INTERVAL '1 second'
                WHERE worker_daemon_id IS NOT NULL AND status = 'running'
                  AND worker_lease_id IS NULL""")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_daemon_lease_expiry ON autonomy_tickets (worker_lease_expires_at) WHERE worker_daemon_id IS NOT NULL AND status = 'running'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS autonomy_daemon_claims")
    op.execute("DROP INDEX IF EXISTS idx_autonomy_daemon_lease_expiry")
    op.execute(
        "ALTER TABLE autonomy_tickets DROP COLUMN IF EXISTS worker_lease_expires_at"
    )
    op.execute(
        "ALTER TABLE autonomy_tickets DROP COLUMN IF EXISTS worker_lease_claim_id"
    )
    op.execute("ALTER TABLE autonomy_tickets DROP COLUMN IF EXISTS worker_lease_id")
