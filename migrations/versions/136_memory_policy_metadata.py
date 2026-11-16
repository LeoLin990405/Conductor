"""Migration 136: retain bounded memory policy metadata on proposals."""

from alembic import op

revision = "136"
down_revision = "135"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "ADD CONSTRAINT ck_memory_proposal_metadata_object "
        "CHECK (jsonb_typeof(metadata) = 'object')"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "DROP CONSTRAINT IF EXISTS ck_memory_proposal_metadata_object"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals DROP COLUMN IF EXISTS metadata"
    )
