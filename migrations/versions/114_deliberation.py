"""Deliberation tables for multi-agent ticket planning.

thread_key is plain text. No deliberation table references conversation_threads.
"""

from alembic import op

revision = "114"
down_revision = "113"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_deliberations (
            deliberation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ticket_id UUID NOT NULL
                REFERENCES autonomy_tickets(ticket_id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            objective TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN (
                    'draft', 'running', 'consensus', 'decided', 'failed', 'cancelled'
                )),
            max_rounds INTEGER NOT NULL DEFAULT 3
                CHECK (max_rounds BETWEEN 1 AND 10),
            round_index INTEGER NOT NULL DEFAULT 0,
            consensus_plan JSONB,
            decision_reason TEXT NOT NULL DEFAULT '',
            thread_key TEXT NOT NULL DEFAULT '',
            planner_agent_key TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_deliberations_ticket "
        "ON autonomy_deliberations (ticket_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_deliberations_user "
        "ON autonomy_deliberations (user_id, status)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_autonomy_deliberations_active_ticket "
        "ON autonomy_deliberations (ticket_id) "
        "WHERE status IN ('draft', 'running')"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_deliberation_participants (
            participant_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            deliberation_id UUID NOT NULL
                REFERENCES autonomy_deliberations(deliberation_id) ON DELETE CASCADE,
            agent_key TEXT NOT NULL,
            role TEXT NOT NULL
                CHECK (role IN ('participant', 'planner')),
            adapter TEXT NOT NULL DEFAULT 'mock'
                CHECK (adapter IN ('mock', 'cli')),
            daemon_id UUID
                REFERENCES autonomy_daemons(daemon_id) ON DELETE SET NULL,
            display_name TEXT NOT NULL DEFAULT '',
            UNIQUE (deliberation_id, agent_key)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_deliberation_rounds (
            round_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            deliberation_id UUID NOT NULL
                REFERENCES autonomy_deliberations(deliberation_id) ON DELETE CASCADE,
            round_index INTEGER NOT NULL,
            kind TEXT NOT NULL
                CHECK (kind IN ('propose', 'critique')),
            status TEXT NOT NULL
                CHECK (status IN ('open', 'closed')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            closed_at TIMESTAMPTZ,
            UNIQUE (deliberation_id, round_index)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_deliberation_messages (
            message_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            deliberation_id UUID NOT NULL
                REFERENCES autonomy_deliberations(deliberation_id) ON DELETE CASCADE,
            round_id UUID NOT NULL
                REFERENCES autonomy_deliberation_rounds(round_id) ON DELETE CASCADE,
            participant_id UUID NOT NULL
                REFERENCES autonomy_deliberation_participants(participant_id)
                ON DELETE CASCADE,
            round_index INTEGER NOT NULL,
            agent_key TEXT NOT NULL,
            kind TEXT NOT NULL
                CHECK (kind IN (
                    'proposal', 'critique', 'revision', 'synthesis', 'dissent'
                )),
            body TEXT NOT NULL DEFAULT '',
            plan JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_deliberation_messages_deliberation "
        "ON autonomy_deliberation_messages (deliberation_id, created_at)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_deliberation_votes (
            vote_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            deliberation_id UUID NOT NULL
                REFERENCES autonomy_deliberations(deliberation_id) ON DELETE CASCADE,
            round_id UUID NOT NULL
                REFERENCES autonomy_deliberation_rounds(round_id) ON DELETE CASCADE,
            participant_id UUID NOT NULL
                REFERENCES autonomy_deliberation_participants(participant_id)
                ON DELETE CASCADE,
            round_index INTEGER NOT NULL,
            agent_key TEXT NOT NULL,
            ballot TEXT NOT NULL
                CHECK (ballot IN ('agree', 'dissent')),
            comment TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (round_id, participant_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_deliberation_dissents (
            dissent_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            deliberation_id UUID NOT NULL
                REFERENCES autonomy_deliberations(deliberation_id) ON DELETE CASCADE,
            participant_id UUID NOT NULL
                REFERENCES autonomy_deliberation_participants(participant_id)
                ON DELETE CASCADE,
            round_id UUID
                REFERENCES autonomy_deliberation_rounds(round_id) ON DELETE SET NULL,
            round_index INTEGER NOT NULL,
            agent_key TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_deliberation_steps (
            step_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            deliberation_id UUID NOT NULL
                REFERENCES autonomy_deliberations(deliberation_id) ON DELETE CASCADE,
            step_key TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            prompt TEXT NOT NULL DEFAULT '',
            assigned_agent TEXT NOT NULL,
            depends_on JSONB NOT NULL DEFAULT '[]'::jsonb,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN (
                    'pending', 'ready', 'running', 'completed', 'failed', 'skipped'
                )),
            daemon_id UUID
                REFERENCES autonomy_daemons(daemon_id) ON DELETE SET NULL,
            child_ticket_id UUID,
            UNIQUE (deliberation_id, step_key)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS autonomy_deliberation_steps")
    op.execute("DROP TABLE IF EXISTS autonomy_deliberation_dissents")
    op.execute("DROP TABLE IF EXISTS autonomy_deliberation_votes")
    op.execute("DROP TABLE IF EXISTS autonomy_deliberation_messages")
    op.execute("DROP TABLE IF EXISTS autonomy_deliberation_rounds")
    op.execute("DROP TABLE IF EXISTS autonomy_deliberation_participants")
    op.execute("DROP TABLE IF EXISTS autonomy_deliberations")
