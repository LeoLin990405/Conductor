"""Migration 115: prompt clarification gate for autonomy Tickets.

An unclear prompt parks its Ticket in ``awaiting_clarification`` with a stored
draft (expanded request, assumptions, open questions) until the user confirms
or corrects it; only then does the Ticket go back to ``queued`` and launch a
Worker. The scheduler claims ``queued`` rows only, so the new status holds
execution by itself and widening the status CHECK is the whole change to
``autonomy_tickets``. The partial unique index keeps at most one active
clarification per Ticket so a retried gate never asks twice.

Lock discipline follows 052: the CHECK swap is catalog work done NOT VALID
under a short lock, validation runs afterwards in an autocommit block where
it blocks neither readers nor writers. Every existing row satisfies the
widened CHECK it replaces.

``downgrade`` is a redeploy of the previous build: it must not meet an
``awaiting_clarification`` row, so those Tickets go back to ``queued`` before
the old eight-value CHECK is restored and validated.
"""

from alembic import op

revision = "115"
down_revision = "114"
branch_labels = None
depends_on = None

_STATUS_CHECK = "autonomy_tickets_status_check"
_ACTIVE_INDEX = "uq_autonomy_clarifications_active"
_USER_INDEX = "idx_autonomy_clarifications_user"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # ADD CONSTRAINT has no IF NOT EXISTS; the DROP makes this re-runnable
    # after an abort in the autocommit block below.
    op.execute(f"""
        ALTER TABLE autonomy_tickets
            DROP CONSTRAINT IF EXISTS {_STATUS_CHECK},
            ADD CONSTRAINT {_STATUS_CHECK} CHECK (status IN (
                'queued', 'running', 'waiting', 'ready', 'blocked',
                'failed', 'completed', 'cancelled', 'awaiting_clarification'
            )) NOT VALID
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS autonomy_ticket_clarifications (
            clarification_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ticket_id UUID NOT NULL
                REFERENCES autonomy_tickets(ticket_id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            original_prompt TEXT NOT NULL DEFAULT '',
            clarity_score DOUBLE PRECISION NOT NULL DEFAULT 0,
            expanded_request TEXT NOT NULL DEFAULT '',
            assumptions JSONB NOT NULL DEFAULT '[]'::jsonb,
            open_questions JSONB NOT NULL DEFAULT '[]'::jsonb,
            status TEXT NOT NULL DEFAULT 'awaiting'
                CHECK (status IN ('awaiting', 'confirmed', 'dismissed')),
            confirmed_text TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            resolved_at TIMESTAMPTZ
        )
    """)
    op.execute(f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {_ACTIVE_INDEX}
            ON autonomy_ticket_clarifications (ticket_id)
            WHERE status = 'awaiting'
    """)
    op.execute(f"""
        CREATE INDEX IF NOT EXISTS {_USER_INDEX}
            ON autonomy_ticket_clarifications (user_id)
    """)

    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = 0")
        # Every existing row already satisfies the wider CHECK it replaces.
        op.execute(
            f"ALTER TABLE autonomy_tickets VALIDATE CONSTRAINT {_STATUS_CHECK}"
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # The indexes go down with the table.
    op.execute("DROP TABLE IF EXISTS autonomy_ticket_clarifications")
    # Back to a vocabulary the previous build can read before it validates.
    op.execute("""
        UPDATE autonomy_tickets SET status = 'queued'
        WHERE status = 'awaiting_clarification'
    """)
    op.execute(f"""
        ALTER TABLE autonomy_tickets
            DROP CONSTRAINT IF EXISTS {_STATUS_CHECK},
            ADD CONSTRAINT {_STATUS_CHECK} CHECK (status IN (
                'queued', 'running', 'waiting', 'ready', 'blocked',
                'failed', 'completed', 'cancelled'
            )) NOT VALID
    """)

    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = 0")
        op.execute(
            f"ALTER TABLE autonomy_tickets VALIDATE CONSTRAINT {_STATUS_CHECK}"
        )
