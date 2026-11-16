"""Add owner-scoped memory lineage and identifier/hash-only forget tombstones."""

from alembic import op

revision = "133"
down_revision = "132"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE memory_entries ADD COLUMN IF NOT EXISTS source_entry_id UUID"
    )
    op.execute("ALTER TABLE memory_entries ADD COLUMN IF NOT EXISTS identity_hash TEXT")
    op.execute("""
        UPDATE memory_entries
           SET identity_hash = md5(jsonb_build_array(tier, namespace, entry_key)::text)
         WHERE identity_hash IS NULL
    """)
    op.execute("ALTER TABLE memory_entries ALTER COLUMN identity_hash SET NOT NULL")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memory_entries_owner_source"
        " ON memory_entries (user_id, source_entry_id)"
        " WHERE source_entry_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memory_entries_owner_identity"
        " ON memory_entries (user_id, identity_hash)"
    )
    op.execute("""
        CREATE TABLE IF NOT EXISTS memory_forget_tombstones (
            user_id UUID NOT NULL,
            entry_id UUID NOT NULL,
            root_entry_id UUID NOT NULL,
            identity_hash TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            reason TEXT NOT NULL CHECK (reason IN (
                'user_request', 'correction', 'duplicate', 'other'
            )),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, entry_id)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memory_forget_identity"
        " ON memory_forget_tombstones (user_id, identity_hash)"
    )
    op.execute("""
        CREATE TABLE IF NOT EXISTS memory_forget_provider_notices (
            user_id UUID NOT NULL,
            entry_id UUID NOT NULL,
            provider TEXT NOT NULL CHECK (length(provider) <= 80),
            status TEXT NOT NULL CHECK (status IN (
                'deleted', 'read_only', 'unavailable', 'not_configured'
            )),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, entry_id, provider)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS memory_forget_provider_notices")
    op.execute("DROP TABLE IF EXISTS memory_forget_tombstones")
    op.execute("DROP INDEX IF EXISTS ix_memory_entries_owner_source")
    op.execute("DROP INDEX IF EXISTS ix_memory_entries_owner_identity")
    op.execute("ALTER TABLE memory_entries DROP COLUMN IF EXISTS identity_hash")
    op.execute("ALTER TABLE memory_entries DROP COLUMN IF EXISTS source_entry_id")
