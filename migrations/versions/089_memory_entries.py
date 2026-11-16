"""Migration 089: a relational projection over agent memory for tiers/retrieval/audit.

Memory content stays in the LangGraph ``BaseStore`` (the source of truth). This
table projects one row per stored entry so the refactor can: enforce five tiers
(user/project/workspace/session/task), retrieve by full text, keep lifecycle
status (active/superseded/proposed/rejected), audit where an entry came from,
and record which entries a turn received. The projection is rebuilt from the
store by an idempotent backfill job and repaired by it if a dual write fails —
the store write is never blocked by this table.

``index_text`` is the entry's index line (what the agent sees in its baseline),
not the full detail content; keeping it here is what makes retrieval possible
without copying full memory bodies into relational storage. ``search`` is a
generated tsvector over the key and index line: Postgres full-text, no new
extension, no SQLite dependency. Chinese tokenization under ``simple`` is
coarse (no spaces means whole-string tokens); Phase 2 validates it against the
memory eval set and may add a trigram index if the cases fail.

Each statement commits on its own, so an upgrade cut short leaves some of them
applied with 088 still recorded: run it again, which is safe.

Rollback: ``alembic downgrade 088``. Drops the projection and its data; memory
content in the store is untouched.
"""

from alembic import op

revision = "089"
down_revision = "088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
        CREATE TABLE IF NOT EXISTS memory_entries (
            entry_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL,
            tier TEXT NOT NULL
                CHECK (tier IN ('user', 'project', 'workspace', 'session', 'task')),
            namespace TEXT[] NOT NULL,
            entry_key TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            size INTEGER NOT NULL DEFAULT 0,
            -- The index line the agent sees, searchable; the body stays in the store.
            index_text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'superseded', 'proposed', 'rejected')),
            provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
            source_thread_id UUID,
            source_response_id UUID,
            search TSVECTOR GENERATED ALWAYS AS (
                to_tsvector('simple'::regconfig, entry_key || ' ' || index_text)
            ) STORED,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, tier, entry_key, status)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_memory_entries_search
        ON memory_entries USING gin (search)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_memory_entries_user_tier
        ON memory_entries (user_id, tier, status)
    """)
    # The injection audit points at entries; lookup by the turn that injected them
    # needs the source ids indexed too.
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_memory_entries_source
        ON memory_entries (source_thread_id)
        WHERE source_thread_id IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS memory_entries")
