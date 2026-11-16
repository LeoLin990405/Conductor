"""Migration 140: durable live-output sessions for local daemon Workers.

A local daemon posts the vendor CLI's visible text to the server while it runs
and watchers tail it. Credential safety of that text depends on *continuity*:
the server must know, atomically with every event it stores, what text has
already been shown. Keeping that state in a cache beside the events let the two
diverge (a lost guard, an expired lock, a deleted stream) and let a watcher
join a prefix it already holds to a suffix from a different stream. 140 moves
both into one transaction:

``autonomy_daemon_output_sessions`` -- one row per Ticket. ``epoch`` names the
current stream generation (the server allocates it; a daemon only echoes it),
``next_seq`` / ``last_batch`` fence the event sequence and the daemon's batch
counter, ``guard_tail`` / ``guard_state`` hold the bounded scan context, and
``tainted`` is a one-way switch: once set, no text is ever stored again for the
epoch. The row is locked ``FOR UPDATE`` for every append, so concurrent workers
serialize on it instead of on process memory.

``autonomy_daemon_output_events`` -- the retained event log, keyed by
``(ticket_id, epoch, seq)``. The foreign key makes an event without its session
(and therefore without its guard) impossible, and the primary key makes a
duplicate sequence impossible.

``autonomy_daemon_output_epoch_seq`` -- a database-wide sequence that issues
epochs. An epoch is never taken from the session row's own counter: a session
that is deleted and recreated (or a Ticket row replaced) must not hand out an
epoch a watcher already holds, or that watcher could join a prefix it received
to a suffix of a different stream with nothing to tell it apart.

Both tables are transient live-view state, not an audit trail: ``downgrade``
drops them and the sequence.

140 follows the current single head 139; no earlier revision is renumbered.
"""

from alembic import op

revision = "140"
down_revision = "139"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("CREATE SEQUENCE IF NOT EXISTS autonomy_daemon_output_epoch_seq")
    op.execute("""
        CREATE TABLE IF NOT EXISTS autonomy_daemon_output_sessions (
            ticket_id UUID PRIMARY KEY
                REFERENCES autonomy_tickets(ticket_id) ON DELETE CASCADE,
            epoch BIGINT NOT NULL DEFAULT 1 CHECK (epoch >= 1),
            lease_id UUID,
            run_id TEXT NOT NULL DEFAULT '',
            next_seq BIGINT NOT NULL DEFAULT 0 CHECK (next_seq >= 0),
            last_batch BIGINT NOT NULL DEFAULT 0 CHECK (last_batch >= 0),
            tainted BOOLEAN NOT NULL DEFAULT FALSE,
            taint_reason TEXT NOT NULL DEFAULT '',
            guard_tail TEXT NOT NULL DEFAULT ''
                CHECK (char_length(guard_tail) <= 2560),
            guard_state JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS autonomy_daemon_output_events (
            ticket_id UUID NOT NULL
                REFERENCES autonomy_daemon_output_sessions(ticket_id)
                ON DELETE CASCADE,
            epoch BIGINT NOT NULL CHECK (epoch >= 1),
            seq BIGINT NOT NULL CHECK (seq >= 1),
            data JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (ticket_id, epoch, seq)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_autonomy_daemon_output_events_created
            ON autonomy_daemon_output_events (created_at)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS autonomy_daemon_output_events")
    op.execute("DROP TABLE IF EXISTS autonomy_daemon_output_sessions")
    op.execute("DROP SEQUENCE IF EXISTS autonomy_daemon_output_epoch_seq")
