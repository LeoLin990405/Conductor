"""Router session sharding and the bounded peer Worker channel."""

from alembic import op

revision = "122"
down_revision = "121"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE autonomy_router_sessions "
        "ADD COLUMN IF NOT EXISTS shard INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE autonomy_router_sessions "
        "DROP CONSTRAINT IF EXISTS autonomy_router_sessions_pkey"
    )
    op.execute(
        "ALTER TABLE autonomy_router_sessions "
        "ADD CONSTRAINT autonomy_router_sessions_pkey PRIMARY KEY (user_id, shard)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_peer_messages (
            message_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id TEXT NOT NULL,
            sender_ticket_id TEXT NOT NULL,
            receiver_ticket_id TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            read_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_autonomy_peer_messages_receiver "
        "ON autonomy_peer_messages (receiver_ticket_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS autonomy_peer_messages")
    # Collapse to one session per user before the single-column key returns.
    op.execute(
        "DELETE FROM autonomy_router_sessions WHERE (user_id, shard) NOT IN "
        "(SELECT user_id, MIN(shard) FROM autonomy_router_sessions "
        " GROUP BY user_id)"
    )
    op.execute(
        "ALTER TABLE autonomy_router_sessions "
        "DROP CONSTRAINT IF EXISTS autonomy_router_sessions_pkey"
    )
    op.execute(
        "ALTER TABLE autonomy_router_sessions DROP COLUMN IF EXISTS shard"
    )
    op.execute(
        "ALTER TABLE autonomy_router_sessions "
        "ADD CONSTRAINT autonomy_router_sessions_pkey PRIMARY KEY (user_id)"
    )
