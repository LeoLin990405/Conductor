"""Remember the signed grant for a daemon lease, and when it was revoked.

Follows 129_daemon_policy_version. The endpoint chain is 129 (local policy
version) -> 130 (signed lease grants).

The endpoint daemon runs a lease only after the real server's signature
checks out, then asks whether that lease is still live. `lease_grant` is
the signed payload (key id, nonce, expiry, daemon identity, policy version).
The column is not named `grant`: that word is reserved in Postgres, and an
unquoted column of that name is a syntax error. `revoked_at` is how the
server withdraws the lease. A daemon that cannot read the answer refuses it.
"""

from alembic import op

revision = "130"
down_revision = "129"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE autonomy_daemon_claims "
        "ADD COLUMN IF NOT EXISTS lease_grant JSONB"
    )
    op.execute(
        "ALTER TABLE autonomy_daemon_claims "
        "ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ"
    )
    # One nonce per grant. A second lease cannot reuse a signature the
    # device may already have accepted.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "idx_autonomy_daemon_claims_lease_grant_nonce "
        "ON autonomy_daemon_claims ((lease_grant->>'nonce')) "
        "WHERE lease_grant IS NOT NULL "
        "AND COALESCE(lease_grant->>'nonce', '') <> ''"
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS idx_autonomy_daemon_claims_lease_grant_nonce"
    )
    op.execute(
        "ALTER TABLE autonomy_daemon_claims DROP COLUMN IF EXISTS revoked_at"
    )
    op.execute(
        "ALTER TABLE autonomy_daemon_claims DROP COLUMN IF EXISTS lease_grant"
    )
