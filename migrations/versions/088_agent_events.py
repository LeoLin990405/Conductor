"""Migration 088: an append-only event stream for agent/tool/permission events.

Phase 1 needs one normalized event stream the UI timeline, the audit trail and
the differential harness can all read, instead of scraping SSE frames or three
overlapping tables (``provenance_records``, ``hook_outbox``, ``replay_facts``).
Rows are append-only by convention: no updates, no deletes in application code.
Retention is a deferred job (age/count), not a cascade.

The event *type* is validated in Python (``contracts/refactor.py``,
``EVENT_TYPES``) rather than a database CHECK: the vocabulary is expected to grow
as the refactor lands, and a CHECK would force a migration for every new type
while a typo is already caught before the insert. Payloads are redacted at write
time — no prompts, keys or full tool results (store ids/hashes and read content
through the provenance tables).

Each statement commits on its own, so an upgrade cut short leaves some of them
applied with 087 still recorded: run it again, which is safe.

Rollback: ``alembic downgrade 087``. Drops the table and its data (Phase-1-only
addition).
"""

from alembic import op

revision = "088"
down_revision = "087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS agent_events (
            event_id BIGSERIAL PRIMARY KEY,
            user_id UUID NOT NULL,
            thread_id UUID,
            response_id UUID,
            task_run_id UUID,
            seq INTEGER NOT NULL,
            type TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    # Timeline reads: one thread's events in append order.
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_agent_events_thread
        ON agent_events (thread_id, event_id)
    """)
    # Replay/audit: one run's events by sequence. A response's stream is the
    # natural unit for a diff fixture; partial so unscoped events do not collide.
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_events_seq
        ON agent_events (response_id, seq)
        WHERE response_id IS NOT NULL
    """)
    # Retention sweep and per-user audit.
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_agent_events_user_created
        ON agent_events (user_id, created_at)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_events")
