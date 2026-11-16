"""Migration 127: durable subscription end and Thread handoff records.

A subscription now has a lifecycle instead of being live-ness recomputed from
its Ticket's status: ``ended_at``/``end_reason`` mark the end ('unsubscribed'
by the Worker, 'reassigned' by the Router, or 'ticket_ended' when the Ticket
reaches a terminal status). Writers that settle a Ticket also end its
subscriptions, so intake's "route to a live subscriber" rule can trust the
row. Ended rows are kept for audit rather than deleted.

``autonomy_handoffs`` is the append-only ledger a reassignment writes: which
Thread moved from which Ticket to which, why, and the handed-off state
(previous owner's latest report and the undelivered input ids that moved).
The new owner surfaces it in its shared context and ticket reads.

Existing subscriptions of already-terminal Tickets are backfilled as ended
so the new live-row rule matches the behavior those Tickets already had.

Lock discipline follows 052/115: the CHECK is added NOT VALID under a short
lock, validation runs afterwards in an autocommit block where it blocks
neither readers nor writers. Every existing row (all NULL) satisfies it.

``downgrade`` drops the ledger and columns; the previous build recomputed
live subscribers from Ticket status, so no data repair is needed on the way
down.
"""

from alembic import op

revision = "127"
down_revision = "126"
branch_labels = None
depends_on = None

_REASON_CHECK = "autonomy_subscriptions_end_reason_check"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        ALTER TABLE autonomy_subscriptions
            ADD COLUMN IF NOT EXISTS ended_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS end_reason TEXT
    """)
    # ADD CONSTRAINT has no IF NOT EXISTS; the DROP makes this re-runnable
    # after an abort in the autocommit block below.
    op.execute(f"""
        ALTER TABLE autonomy_subscriptions
            DROP CONSTRAINT IF EXISTS {_REASON_CHECK},
            ADD CONSTRAINT {_REASON_CHECK} CHECK (end_reason IS NULL OR end_reason IN (
                'unsubscribed', 'reassigned', 'ticket_ended'
            )) NOT VALID
    """)
    # Subscriptions of Tickets that already reached a terminal status end now;
    # their ended_at backfills from the Ticket's own end when it has one.
    op.execute("""
        UPDATE autonomy_subscriptions s
           SET ended_at = COALESCE(t.ended_at, t.updated_at, NOW()),
               end_reason = 'ticket_ended'
          FROM autonomy_tickets t
         WHERE s.ticket_id = t.ticket_id
           AND s.ended_at IS NULL
           AND t.status IN ('completed', 'cancelled')
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS autonomy_handoffs (
            handoff_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            thread_key TEXT NOT NULL,
            user_id TEXT NOT NULL,
            from_ticket_id UUID
                REFERENCES autonomy_tickets(ticket_id) ON DELETE SET NULL,
            to_ticket_id UUID
                REFERENCES autonomy_tickets(ticket_id) ON DELETE SET NULL,
            reason TEXT NOT NULL DEFAULT '',
            state JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_autonomy_handoffs_thread
            ON autonomy_handoffs (thread_key)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_autonomy_handoffs_to_ticket
            ON autonomy_handoffs (to_ticket_id)
    """)

    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = 0")
        # Every existing row already satisfies the CHECK it replaces.
        op.execute(
            f"ALTER TABLE autonomy_subscriptions VALIDATE CONSTRAINT {_REASON_CHECK}"
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS autonomy_handoffs")
    op.execute(
        f"ALTER TABLE autonomy_subscriptions DROP CONSTRAINT IF EXISTS {_REASON_CHECK}"
    )
    op.execute("""
        ALTER TABLE autonomy_subscriptions
            DROP COLUMN IF EXISTS end_reason,
            DROP COLUMN IF EXISTS ended_at
    """)
