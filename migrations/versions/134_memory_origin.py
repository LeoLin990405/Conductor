"""Migration 134: preserve source origin and evidence metadata for proposals.

Existing proposals are conservatively classified as assistant inference. The
projection already stores normalized metadata in its JSONB column.
"""

from alembic import op

revision = "134"
down_revision = "133"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'assistant_inference'"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "ADD COLUMN IF NOT EXISTS evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "ADD COLUMN IF NOT EXISTS evidence_count INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "ADD COLUMN IF NOT EXISTS counts_as_evidence BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "ADD CONSTRAINT ck_memory_proposal_origin "
        "CHECK (origin IN ('user', 'tool', 'web', 'assistant_inference'))"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "ADD CONSTRAINT ck_memory_proposal_confidence "
        "CHECK (confidence >= 0 AND confidence <= 1 "
        "AND (origin <> 'assistant_inference' OR confidence <= 0.5))"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "ADD CONSTRAINT ck_memory_proposal_evidence_refs "
        "CHECK (jsonb_typeof(evidence_refs) = 'array')"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "ADD CONSTRAINT ck_memory_proposal_evidence_count "
        "CHECK (evidence_count >= 0 AND (origin = 'user' OR evidence_count = 0))"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "ADD CONSTRAINT ck_memory_proposal_counts_as_evidence "
        "CHECK (counts_as_evidence = (origin = 'user' AND evidence_count > 0))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "DROP CONSTRAINT IF EXISTS ck_memory_proposal_counts_as_evidence"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "DROP CONSTRAINT IF EXISTS ck_memory_proposal_evidence_count"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "DROP CONSTRAINT IF EXISTS ck_memory_proposal_evidence_refs"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "DROP CONSTRAINT IF EXISTS ck_memory_proposal_confidence"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "DROP CONSTRAINT IF EXISTS ck_memory_proposal_origin"
    )
    op.execute(
        "ALTER TABLE agent_memory_proposals "
        "DROP COLUMN IF EXISTS counts_as_evidence, "
        "DROP COLUMN IF EXISTS evidence_count, "
        "DROP COLUMN IF EXISTS evidence_refs, "
        "DROP COLUMN IF EXISTS confidence, "
        "DROP COLUMN IF EXISTS origin"
    )
