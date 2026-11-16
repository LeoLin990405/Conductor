"""Migration 135: classify durable memory proposals and their replacement target."""

from alembic import op

revision = "135"
down_revision = "134"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "ADD COLUMN IF NOT EXISTS change_kind TEXT NOT NULL DEFAULT 'long_term'"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "ADD COLUMN IF NOT EXISTS replaces_entry_key TEXT"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals ADD COLUMN IF NOT EXISTS reason TEXT"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "ADD CONSTRAINT ck_memory_proposal_change_kind "
        "CHECK (change_kind IN ('long_term', 'correction'))"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "ADD CONSTRAINT ck_memory_proposal_correction "
        "CHECK (change_kind <> 'correction' OR "
        "(replaces_entry_key IS NOT NULL AND btrim(replaces_entry_key) <> '' "
        "AND reason IS NOT NULL AND btrim(reason) <> ''))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "DROP CONSTRAINT IF EXISTS ck_memory_proposal_correction"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "DROP CONSTRAINT IF EXISTS ck_memory_proposal_change_kind"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "DROP COLUMN IF EXISTS reason, "
        "DROP COLUMN IF EXISTS replaces_entry_key, "
        "DROP COLUMN IF EXISTS change_kind"
    )
