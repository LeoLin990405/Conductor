"""Explicit context scopes and an authenticated agent-to-agent protocol.

A Ticket always runs inside one context scope. The migration gives every
existing Ticket its own ``isolated`` scope so previously implicit sharing is
closed by default; a shared scope is then an explicit, policy-bearing grant
that additional Tickets join. A2A agents authenticate with a hashed bearer
token and their messages are stored with provenance, audience, allowed
actions and an expiry before they are delivered as context-only Inputs.
"""

from alembic import op

revision = "128"
down_revision = "127"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_context_scopes (
            scope_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL
                CHECK (kind IN ('isolated', 'shared')),
            shared_with_user_ids TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
            policy JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, name)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_context_scopes_shared "
        "ON autonomy_context_scopes USING GIN (shared_with_user_ids)"
    )
    op.execute(
        "ALTER TABLE autonomy_tickets ADD COLUMN IF NOT EXISTS context_scope_id UUID "
        "REFERENCES autonomy_context_scopes(scope_id) ON DELETE SET NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_tickets_context_scope "
        "ON autonomy_tickets (context_scope_id)"
    )

    # Closed by default: every pre-existing Ticket gets its own isolated scope.
    op.execute(
        """
        INSERT INTO autonomy_context_scopes (user_id, name, kind)
        SELECT user_id, 'isolated:' || ticket_id::text, 'isolated'
        FROM autonomy_tickets
        ON CONFLICT (user_id, name) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE autonomy_tickets t
        SET context_scope_id = s.scope_id
        FROM autonomy_context_scopes s
        WHERE s.user_id = t.user_id
          AND s.name = 'isolated:' || t.ticket_id::text
          AND t.context_scope_id IS NULL
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_a2a_agents (
            agent_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id TEXT NOT NULL,
            ticket_id UUID NOT NULL UNIQUE
                REFERENCES autonomy_tickets(ticket_id) ON DELETE CASCADE,
            name TEXT NOT NULL DEFAULT '',
            token_hash TEXT NOT NULL UNIQUE,
            last_seen_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_a2a_agents_ticket "
        "ON autonomy_a2a_agents (ticket_id, created_at)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_a2a_messages (
            message_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id TEXT NOT NULL,
            sender_agent_id UUID
                REFERENCES autonomy_a2a_agents(agent_id) ON DELETE SET NULL,
            sender_ticket_id UUID NOT NULL
                REFERENCES autonomy_tickets(ticket_id) ON DELETE CASCADE,
            recipient_ticket_id UUID NOT NULL
                REFERENCES autonomy_tickets(ticket_id) ON DELETE CASCADE,
            scope_id UUID
                REFERENCES autonomy_context_scopes(scope_id) ON DELETE SET NULL,
            message_key TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            intent TEXT NOT NULL
                CHECK (intent IN ('inform', 'request_review', 'handoff_request')),
            subject TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            allowed_actions TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
            provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
            issued_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            status TEXT NOT NULL DEFAULT 'delivered'
                CHECK (status IN ('delivered', 'rejected')),
            rejection TEXT NOT NULL DEFAULT '',
            input_id UUID
                REFERENCES autonomy_ticket_inputs(input_id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (sender_ticket_id, message_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_a2a_messages_recipient "
        "ON autonomy_a2a_messages (recipient_ticket_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_a2a_messages_scope "
        "ON autonomy_a2a_messages (scope_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS autonomy_a2a_messages")
    op.execute("DROP TABLE IF EXISTS autonomy_a2a_agents")
    op.execute("DROP INDEX IF EXISTS idx_autonomy_tickets_context_scope")
    op.execute("ALTER TABLE autonomy_tickets DROP COLUMN IF EXISTS context_scope_id")
    op.execute("DROP TABLE IF EXISTS autonomy_context_scopes")
