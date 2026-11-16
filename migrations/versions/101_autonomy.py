"""Migration 101: durable Super-agent autonomy tables.

The autonomy layer is a separate feature from the manual Projects scheduler:
Sources produce Triggers, a Router decides Tickets, Workers execute them, and
every external action is a durable effect/outbox row.  Keeping the tables in
Postgres (rather than the LangGraph store) is what makes the state survive a
worker restart and lets several backend workers claim work safely.
"""

from alembic import op

revision = "101"
down_revision = "100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_sources (
            source_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            config JSONB NOT NULL DEFAULT '{}'::jsonb,
            state JSONB NOT NULL DEFAULT '{}'::jsonb,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_autonomy_sources_repository "
        "ON autonomy_sources (user_id, kind, (config->>'repository')) "
        "WHERE (config->>'repository') IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_sources_user "
        "ON autonomy_sources (user_id, enabled)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_triggers (
            trigger_id TEXT PRIMARY KEY,
            source_id UUID NOT NULL
                REFERENCES autonomy_sources(source_id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            external_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            thread_key TEXT NOT NULL,
            actor_login TEXT NOT NULL DEFAULT '',
            actor_is_bot BOOLEAN NOT NULL DEFAULT FALSE,
            addressed BOOLEAN NOT NULL DEFAULT FALSE,
            text TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (source_id, external_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_triggers_thread "
        "ON autonomy_triggers (thread_key, received_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_triggers_source "
        "ON autonomy_triggers (source_id, received_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_threads (
            thread_key TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            source_id UUID NOT NULL
                REFERENCES autonomy_sources(source_id) ON DELETE CASCADE,
            pending_trigger_ids TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
            evaluating_eval_id UUID,
            meta JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_threads_user "
        "ON autonomy_threads (user_id, updated_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_evals (
            eval_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            thread_key TEXT NOT NULL,
            trigger_ids TEXT[] NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'delivered', 'decided', 'unavailable')),
            decision TEXT
                CHECK (decision IS NULL OR decision IN ('accept', 'attach', 'ignore', 'stop')),
            reason TEXT NOT NULL DEFAULT '',
            policy_version TEXT NOT NULL DEFAULT '',
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            delivered_at TIMESTAMPTZ,
            deadline_at TIMESTAMPTZ,
            decided_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_autonomy_evals_active_thread "
        "ON autonomy_evals (thread_key) "
        "WHERE status IN ('queued', 'delivered')"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_evals_deadline "
        "ON autonomy_evals (status, deadline_at)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_tickets (
            ticket_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN (
                    'queued', 'running', 'waiting', 'ready', 'blocked',
                    'failed', 'completed', 'cancelled'
                )),
            mandate JSONB NOT NULL DEFAULT '{}'::jsonb,
            policy_version TEXT NOT NULL DEFAULT '',
            origin_trigger_id TEXT,
            origin_thread_key TEXT NOT NULL DEFAULT '',
            router_thread_id UUID
                REFERENCES conversation_threads(conversation_thread_id)
                ON DELETE SET NULL,
            worker_thread_id UUID
                REFERENCES conversation_threads(conversation_thread_id)
                ON DELETE SET NULL,
            worker_run_id UUID,
            attempts INTEGER NOT NULL DEFAULT 0,
            attention JSONB,
            summary TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            started_at TIMESTAMPTZ,
            ended_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_tickets_user_status "
        "ON autonomy_tickets (user_id, status, updated_at DESC)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_autonomy_tickets_worker_thread "
        "ON autonomy_tickets (worker_thread_id) "
        "WHERE worker_thread_id IS NOT NULL"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_ticket_inputs (
            input_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ticket_id UUID NOT NULL
                REFERENCES autonomy_tickets(ticket_id) ON DELETE CASCADE,
            trigger_id TEXT,
            kind TEXT NOT NULL DEFAULT 'trigger',
            text TEXT NOT NULL DEFAULT '',
            delivered_at TIMESTAMPTZ,
            read_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_ticket_inputs_ticket "
        "ON autonomy_ticket_inputs (ticket_id, created_at)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_autonomy_ticket_input_trigger "
        "ON autonomy_ticket_inputs (ticket_id, trigger_id) "
        "WHERE trigger_id IS NOT NULL"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_ticket_reports (
            report_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ticket_id UUID NOT NULL
                REFERENCES autonomy_tickets(ticket_id) ON DELETE CASCADE,
            state TEXT NOT NULL
                CHECK (state IN ('progress', 'waiting', 'ready', 'blocked', 'failed', 'dropped')),
            summary TEXT NOT NULL DEFAULT '',
            question TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_ticket_reports_ticket "
        "ON autonomy_ticket_reports (ticket_id, created_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_subscriptions (
            ticket_id UUID NOT NULL
                REFERENCES autonomy_tickets(ticket_id) ON DELETE CASCADE,
            thread_key TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'own'
                CHECK (kind IN ('own', 'context')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (ticket_id, thread_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_subscriptions_thread "
        "ON autonomy_subscriptions (thread_key)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_policies (
            policy_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id TEXT NOT NULL,
            version TEXT NOT NULL,
            rules JSONB NOT NULL DEFAULT '[]'::jsonb,
            accept_mode TEXT NOT NULL DEFAULT 'explicit'
                CHECK (accept_mode IN ('explicit', 'observe', 'auto')),
            is_current BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, version)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_autonomy_policies_current "
        "ON autonomy_policies (user_id) WHERE is_current"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_router_sessions (
            user_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            thread_id UUID NOT NULL
                REFERENCES conversation_threads(conversation_thread_id)
                ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_effects (
            effect_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'claimed', 'done', 'dead')),
            attempts INTEGER NOT NULL DEFAULT 0,
            due_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            lease_until TIMESTAMPTZ,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_effects_due "
        "ON autonomy_effects (status, due_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_effects_lease "
        "ON autonomy_effects (status, lease_until)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_effects_user "
        "ON autonomy_effects (user_id, status)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_outbox (
            outbox_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id TEXT NOT NULL,
            ticket_id UUID
                REFERENCES autonomy_tickets(ticket_id) ON DELETE SET NULL,
            source_id UUID
                REFERENCES autonomy_sources(source_id) ON DELETE SET NULL,
            thread_key TEXT NOT NULL,
            body TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'sent', 'dropped')),
            attempts INTEGER NOT NULL DEFAULT 0,
            due_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            sent_at TIMESTAMPTZ,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_outbox_due "
        "ON autonomy_outbox (status, due_at)"
    )


def downgrade() -> None:
    # Peer messages arrive in 122. They are per-user rows of this feature, so
    # this list drops them too; 122 drops the table first on a full downgrade.
    for table in (
        "autonomy_peer_messages",
        "autonomy_outbox",
        "autonomy_effects",
        "autonomy_router_sessions",
        "autonomy_policies",
        "autonomy_subscriptions",
        "autonomy_ticket_reports",
        "autonomy_ticket_inputs",
        "autonomy_tickets",
        "autonomy_evals",
        "autonomy_threads",
        "autonomy_triggers",
        "autonomy_sources",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
