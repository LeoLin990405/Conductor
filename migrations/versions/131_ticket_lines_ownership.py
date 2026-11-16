"""Migration 131: ticket lines, the ownership audit trail, and the archived status.

The final allocation is 128 (sa-a2a, already on integrate/r2) -> 129 -> 130
(beyond-endpoint daemon policy version and lease grants) -> 131 (this line).
The branch-local tree may not carry 130 until the endpoint line lands; the
branch is re-synced onto that merge and re-tested before integration, so the
chain stays linear with one head. See MIGRATIONS.md.

A *line* is one self-iteration line of work: a named branch/worktree pair that
Tickets group under for the line dashboard. A Ticket's owner stays the single
``worker_daemon_id`` column (NULL means the backend scheduler, "cloud"); this
migration adds no second owner column, only the durable history that makes the
exactly-one-owner rule auditable: ``autonomy_ticket_events`` records claims and
the stop/resume/reassign/archive operations.

``archived`` widens ``autonomy_tickets`` status as a terminal state, using the
115 pattern: the CHECK swap is catalog work done NOT VALID under a short lock,
validation runs afterwards in an autocommit block where it blocks neither
readers nor writers. Every existing row satisfies the wider CHECK it replaces.

``downgrade`` is a redeploy of the previous build: it must not meet a row the
old status vocabulary cannot read, so ``archived`` Tickets become ``cancelled``
before the previous CHECK is restored and validated.

``downgrade`` has no autocommit block, unlike ``upgrade``. ``alembic downgrade``
runs a whole command in one transaction and a block commits what the command has
undone so far (140 down to 132 and this revision's own update), so a command that
failed after the block left ``alembic_version`` at 131 over a half-undone schema.
The block let the validating scan run outside the transaction that holds the
exclusive lock on ``autonomy_tickets``. This downgrade takes that lock anyway
(dropping ``line_id`` holds it until the command commits), so validating in the
transaction lengthens the hold by one scan: 0.1 to 0.2 s on a scratch table of a
million Tickets, one measurement (MIGRATIONS.md). The block's plain ``SET
lock_timeout = 0`` also outlived it, so the drops of 130, 129 and 128 that came
next, which set no timeout of their own, waited without limit; 127, the next
downgrade that sets one, bounded what followed. They now inherit the 5 s set at
the top of this ``downgrade``, and a wait over that fails the command. Nothing a
downgrade above 115 does commits, so the command is rolled back whole. The 5 s
starts at the first ``SET LOCAL`` a command reaches; MIGRATIONS.md lists which
downgrades issue one, and a command that starts below 131 has none until it
reaches the first of them.
"""

from alembic import op

revision = "131"
down_revision = "130"
branch_labels = None
depends_on = None

_STATUS_CHECK = "autonomy_tickets_status_check"
_LINE_FK = "autonomy_tickets_line_id_fkey"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS autonomy_lines (
            line_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            branch TEXT NOT NULL DEFAULT '',
            worktree_path TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'archived')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_autonomy_lines_user_name
            ON autonomy_lines (user_id, name)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_autonomy_lines_user
            ON autonomy_lines (user_id, status, updated_at DESC)
    """)
    op.execute("""
        ALTER TABLE autonomy_tickets
            ADD COLUMN IF NOT EXISTS line_id UUID
    """)
    # ADD CONSTRAINT has no IF NOT EXISTS; the DROP makes this re-runnable
    # after an abort anywhere above.
    op.execute(f"""
        ALTER TABLE autonomy_tickets
            DROP CONSTRAINT IF EXISTS {_LINE_FK},
            ADD CONSTRAINT {_LINE_FK}
                FOREIGN KEY (line_id) REFERENCES autonomy_lines(line_id)
                ON DELETE SET NULL
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_autonomy_tickets_line
            ON autonomy_tickets (line_id)
            WHERE line_id IS NOT NULL
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS autonomy_ticket_events (
            event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ticket_id UUID NOT NULL
                REFERENCES autonomy_tickets(ticket_id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL
                CHECK (action IN (
                    'claimed', 'stopped', 'resumed', 'reassigned', 'archived'
                )),
            from_owner TEXT NOT NULL DEFAULT '',
            to_owner TEXT NOT NULL DEFAULT '',
            detail JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_autonomy_ticket_events_ticket
            ON autonomy_ticket_events (ticket_id, created_at)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_autonomy_ticket_events_user
            ON autonomy_ticket_events (user_id, created_at)
    """)
    op.execute(f"""
        ALTER TABLE autonomy_tickets
            DROP CONSTRAINT IF EXISTS {_STATUS_CHECK},
            ADD CONSTRAINT {_STATUS_CHECK} CHECK (status IN (
                'queued', 'running', 'waiting', 'ready', 'blocked',
                'failed', 'completed', 'cancelled', 'awaiting_clarification',
                'archived'
            )) NOT VALID
    """)

    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = 0")
        # Every existing row already satisfies the wider CHECK it replaces.
        op.execute(
            f"ALTER TABLE autonomy_tickets VALIDATE CONSTRAINT {_STATUS_CHECK}"
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        UPDATE autonomy_tickets SET status = 'cancelled'
        WHERE status = 'archived'
    """)
    # Added validated, in this transaction: see the module docstring.
    op.execute(f"""
        ALTER TABLE autonomy_tickets
            DROP CONSTRAINT IF EXISTS {_STATUS_CHECK},
            ADD CONSTRAINT {_STATUS_CHECK} CHECK (status IN (
                'queued', 'running', 'waiting', 'ready', 'blocked',
                'failed', 'completed', 'cancelled', 'awaiting_clarification'
            ))
    """)
    # The indexes go down with their tables.
    op.execute("DROP TABLE IF EXISTS autonomy_ticket_events")
    op.execute(f"ALTER TABLE autonomy_tickets DROP CONSTRAINT IF EXISTS {_LINE_FK}")
    op.execute("ALTER TABLE autonomy_tickets DROP COLUMN IF EXISTS line_id")
    op.execute("DROP TABLE IF EXISTS autonomy_lines")
